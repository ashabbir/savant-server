"""
AbilityStore — loads, indexes, and validates ability blocks from the filesystem.

Source of truth: server abilities directory resolved from env / server data.
Each .md file has YAML frontmatter (id, type, tags, priority, includes) + markdown body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

# Tolerant frontmatter regex: handles BOM, leading whitespace, CRLF
FRONTMATTER_RE = re.compile(
    r"^[\ufeff\s]*---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.DOTALL
)


@dataclass
class Block:
    id: str
    type: str  # persona | rule | policy | style | repo
    tags: List[str]
    priority: int
    includes: List[str] = field(default_factory=list)
    deprecated: bool = False
    supersedes: Optional[str] = None
    name: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    body: str = ""
    path: Optional[Path] = None


class AbilityStore:
    """Loads and indexes ability blocks from a filesystem directory."""

    def __init__(self, base_path: Path):
        self.base_path = Path(base_path)
        self.blocks_by_id: Dict[str, Block] = {}
        self.ids_by_tag: Dict[str, List[str]] = {}
        self.ids_by_type: Dict[str, List[str]] = {}
        self.include_edges: Dict[str, List[str]] = {}

    def load(self) -> None:
        root = self.base_path / "abilities"
        if not root.exists():
            root.mkdir(parents=True, exist_ok=True)
            self.blocks_by_id.clear()
            self.ids_by_tag.clear()
            self.ids_by_type.clear()
            self.include_edges.clear()
            return

        self.blocks_by_id.clear()
        self.ids_by_tag.clear()
        self.ids_by_type.clear()
        self.include_edges.clear()

        categories = ["personas", "rules", "policies", "styles", "repos"]
        for cat in categories:
            base = root / cat
            if not base.exists():
                continue
            for path in base.rglob("*.md"):
                self._load_file(path)

        for bid, blk in self.blocks_by_id.items():
            self.include_edges[bid] = list(blk.includes or [])

        self._check_cycles()

    def _load_file(self, path: Path) -> None:
        content = path.read_text(encoding="utf-8")
        m = FRONTMATTER_RE.match(content)
        if not m:
            raise RuntimeError(f"Missing YAML front matter in {path}")
        meta_raw, body = m.group(1), m.group(2)
        meta = yaml.safe_load(meta_raw) or {}

        required = ["id", "type", "tags", "priority"]
        for key in required:
            if key not in meta:
                raise RuntimeError(f"Missing required field '{key}' in {path}")

        bid = str(meta["id"]).strip()
        if bid in self.blocks_by_id:
            import logging
            logging.getLogger(__name__).warning(f"Duplicate id '{bid}' in {path} — skipping")
            return

        btype = str(meta["type"]).strip()

        # Normalize tags: accept scalar or list, split on commas/whitespace, lowercase
        raw_tags_any = meta.get("tags")
        tags: List[str] = []
        raw_items: List[str] = []
        if raw_tags_any is None:
            raw_items = []
        elif isinstance(raw_tags_any, str):
            raw_items = [raw_tags_any]
        elif isinstance(raw_tags_any, (list, tuple, set)):
            raw_items = [str(x) for x in list(raw_tags_any)]
        else:
            raw_items = [str(raw_tags_any)]
        for t in raw_items:
            parts = re.split(r"[\s,]+", str(t))
            for p in parts:
                p = p.strip()
                if p:
                    tags.append(p.lower())

        priority = int(meta["priority"])
        includes = [str(x).strip() for x in (meta.get("includes") or [])]
        deprecated = bool(meta.get("deprecated", False))
        supersedes = meta.get("supersedes")
        name = str(meta.get("name")).strip() if meta.get("name") is not None else None
        aliases = [str(x).strip() for x in (meta.get("aliases") or [])]

        blk = Block(
            id=bid,
            type=btype,
            tags=tags,
            priority=priority,
            includes=includes,
            deprecated=deprecated,
            supersedes=str(supersedes) if supersedes else None,
            name=name,
            aliases=aliases,
            body=body.strip(),
            path=path,
        )

        self.blocks_by_id[bid] = blk
        self.ids_by_type.setdefault(btype, []).append(bid)
        for t in tags:
            self.ids_by_tag.setdefault(t, []).append(bid)

    # ── Lookup helpers ────────────────────────────────────────────────────

    def get(self, block_id: str) -> Optional[Block]:
        return self.blocks_by_id.get(block_id)

    def find_persona(self, name_or_id: str) -> Optional[Block]:
        if name_or_id.startswith("persona."):
            return self.get(name_or_id)
        return self.get(f"persona.{name_or_id}")

    # ── Tag matching (exact → prefix → substring → fuzzy ≥ 0.72) ─────

    def blocks_with_tags(
        self, tags: List[str], allowed_types: Optional[Set[str]] = None
    ) -> List[Tuple[Block, Dict[str, Any]]]:
        qtags_raw = [str(t).strip() for t in (tags or []) if t and str(t).strip()]
        qtags: List[str] = [t.lower() for t in qtags_raw]
        qtags_norm: List[str] = [self._norm_key(t) for t in qtags]

        seen: Set[str] = set()
        result: List[Tuple[Block, Dict[str, Any]]] = []

        # 1) Exact matches via index
        for i, tag in enumerate(qtags):
            for bid in self.ids_by_tag.get(tag, []):
                if bid in seen:
                    continue
                blk = self.blocks_by_id[bid]
                if allowed_types and blk.type not in allowed_types:
                    continue
                seen.add(bid)
                result.append((blk, {
                    "query_tag": qtags_raw[i],
                    "query_norm": qtags_norm[i],
                    "block_tag": tag,
                    "match_type": "exact",
                    "score": 1.0,
                }))

        # 2) Fuzzy pass
        if qtags_norm:
            from difflib import SequenceMatcher
            THRESH = 0.72

            for bid, blk in self.blocks_by_id.items():
                if bid in seen:
                    continue
                if allowed_types and blk.type not in allowed_types:
                    continue
                btags = list(blk.tags or [])
                btags_norm = [self._norm_key(t) for t in btags]
                matched = False
                best_score = 0.0
                best_qi = best_bi = -1
                best_type = ""
                for bi, bt in enumerate(btags_norm):
                    for qi, q in enumerate(qtags_norm):
                        if not bt or not q:
                            continue
                        if bt == q:
                            matched, best_score = True, 1.0
                            best_qi, best_bi, best_type = qi, bi, "exact"
                            break
                        if bt.startswith(q) or q.startswith(bt):
                            matched = True
                            if 0.94 > best_score:
                                best_score = 0.94
                                best_qi, best_bi, best_type = qi, bi, "prefix"
                        if (q in bt) or (bt in q):
                            matched = True
                            if 0.9 > best_score:
                                best_score = 0.9
                                best_qi, best_bi, best_type = qi, bi, "substring"
                        r = SequenceMatcher(None, q, bt).ratio()
                        if r >= THRESH and r > best_score:
                            matched = True
                            best_score = r
                            best_qi, best_bi, best_type = qi, bi, "fuzzy"
                    if matched and best_score >= THRESH:
                        break
                if matched:
                    seen.add(bid)
                    result.append((blk, {
                        "query_tag": qtags_raw[best_qi] if 0 <= best_qi < len(qtags_raw) else "",
                        "query_norm": qtags_norm[best_qi] if 0 <= best_qi < len(qtags_norm) else "",
                        "block_tag": btags[best_bi] if 0 <= best_bi < len(btags) else "",
                        "block_norm": btags_norm[best_bi] if 0 <= best_bi < len(btags_norm) else "",
                        "match_type": best_type or "fuzzy",
                        "score": float(best_score),
                    }))

        return result

    # ── Repo fuzzy matching ───────────────────────────────────────────────

    @staticmethod
    def _norm_key(s: str) -> str:
        s = (s or "").strip().lower()
        s = re.sub(r"[\s_]+", "-", s)
        s = re.sub(r"-+", "-", s)
        return s

    @staticmethod
    def _compact(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s or "")

    def _repo_keys(self, blk: Block) -> List[str]:
        keys: List[str] = []
        keys.append(blk.id)
        if blk.id.startswith("repo."):
            keys.append(blk.id[len("repo."):])
        if blk.name:
            keys.append(blk.name)
        for a in (blk.aliases or []):
            if a:
                keys.append(a)
        normed: List[str] = []
        for k in list(keys):
            nk = self._norm_key(k)
            if nk not in keys:
                normed.append(nk)
        keys.extend(normed)
        compacted: List[str] = []
        for k in list(keys):
            ck = self._compact(self._norm_key(k))
            if ck and ck not in keys:
                compacted.append(ck)
        keys.extend(compacted)
        seen: Set[str] = set()
        ordered: List[str] = []
        for k in keys:
            if k and k not in seen:
                ordered.append(k)
                seen.add(k)
        return ordered

    def find_repo_fuzzy(
        self, query: str
    ) -> Tuple[Optional[Block], Optional[Dict[str, Any]]]:
        if not query:
            return None, None
        from difflib import SequenceMatcher

        raw = str(query).strip()
        q_norm = self._norm_key(raw)
        q_comp = self._compact(q_norm)

        best_score = -1.0
        best_blk: Optional[Block] = None
        best_detail: Optional[Dict[str, Any]] = None

        for bid in self.ids_by_type.get("repo", []) or []:
            blk = self.blocks_by_id.get(bid)
            if not blk:
                continue
            keys = self._repo_keys(blk)
            score = 0.0
            local_detail = None
            for k in keys:
                kn = self._norm_key(k)
                kc = self._compact(kn)
                if q_norm == kn:
                    score = 1.0
                    local_detail = {"method": "exact", "matched_key": k, "query": query, "score": 1.0}
                    break
                if q_comp and q_comp == kc:
                    if 0.97 > score:
                        score = 0.97
                        local_detail = {"method": "compact", "matched_key": k, "query": query, "score": score}
                if kn.startswith(q_norm) or q_norm.startswith(kn):
                    if 0.94 > score:
                        score = 0.94
                        local_detail = {"method": "prefix", "matched_key": k, "query": query, "score": score}
                r = SequenceMatcher(None, q_norm, kn).ratio()
                if r > score:
                    score = r
                    local_detail = {"method": "fuzzy", "matched_key": k, "query": query, "score": score}
            if score > best_score or (
                abs(score - best_score) < 1e-6
                and best_blk
                and (blk.priority > best_blk.priority or (blk.priority == best_blk.priority and blk.id < best_blk.id))
            ):
                best_score = score
                best_blk = blk
                best_detail = local_detail

        if best_blk and best_score >= 0.6:
            return best_blk, best_detail
        return None, None

    # ── Stats & validation ────────────────────────────────────────────────

    def stats(self) -> Dict[str, int]:
        return {
            "personas": len(self.ids_by_type.get("persona", [])),
            "rules": len(self.ids_by_type.get("rule", [])),
            "policies": len(self.ids_by_type.get("policy", [])),
            "styles": len(self.ids_by_type.get("style", [])),
            "repos": len(self.ids_by_type.get("repo", [])),
        }

    def validate_includes(self, raise_on_error: bool = True) -> bool:
        try:
            self._check_cycles()
            for bid, edges in self.include_edges.items():
                for inc in edges:
                    if inc not in self.blocks_by_id:
                        raise RuntimeError(f"Unknown include '{inc}' referenced by '{bid}'")
            return True
        except Exception:
            if raise_on_error:
                raise
            return False

    def validate_all(self) -> None:
        self.validate_includes(raise_on_error=True)

    def _check_cycles(self) -> None:
        visited: Set[str] = set()
        stack: Set[str] = set()

        def dfs(node: str) -> None:
            if node in stack:
                raise RuntimeError(f"Circular include detected at '{node}'")
            if node in visited:
                return
            visited.add(node)
            stack.add(node)
            for child in self.include_edges.get(node, []):
                if child not in self.blocks_by_id:
                    continue
                dfs(child)
            stack.remove(node)

        for nid in list(self.blocks_by_id.keys()):
            dfs(nid)

    # ── Asset CRUD helpers (for API) ──────────────────────────────────────

    def get_asset_dict(self, block_id: str) -> Optional[Dict[str, Any]]:
        """Return a JSON-serializable dict for an asset."""
        blk = self.get(block_id)
        if not blk:
            return None
        rel_path = ""
        if blk.path:
            try:
                rel_path = str(blk.path.relative_to(self.base_path / "abilities"))
            except ValueError:
                rel_path = str(blk.path)
        learned = self._extract_learned(blk.body)
        return {
            "id": blk.id,
            "type": blk.type,
            "tags": blk.tags,
            "priority": blk.priority,
            "includes": blk.includes,
            "name": blk.name,
            "aliases": blk.aliases,
            "body": blk.body,
            "path": rel_path,
            "learned": learned,
        }

    def list_assets_grouped(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return all assets grouped by type."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for bid in sorted(self.blocks_by_id.keys()):
            blk = self.blocks_by_id[bid]
            entry = self.get_asset_dict(bid)
            if entry:
                grouped.setdefault(blk.type, []).append(entry)
        return grouped

    def create_asset(self, asset_type: str, asset_id: str, tags: List[str],
                     priority: int, body: str, includes: Optional[List[str]] = None,
                     name: Optional[str] = None, aliases: Optional[List[str]] = None) -> Dict[str, Any]:
        """Create a new asset file and reload the store."""
        if asset_id in self.blocks_by_id:
            raise RuntimeError(f"Asset '{asset_id}' already exists")

        # Derive file path from id: persona.engineer -> personas/engineer.md
        rel = self._id_to_rel_path(asset_id, asset_type)
        abs_path = self.base_path / "abilities" / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        frontmatter = {"id": asset_id, "type": asset_type, "tags": tags, "priority": priority}
        if includes:
            frontmatter["includes"] = includes
        if name:
            frontmatter["name"] = name
        if aliases:
            frontmatter["aliases"] = aliases

        content = "---\n" + yaml.dump(frontmatter, default_flow_style=False).strip() + "\n---\n\n" + body.strip() + "\n"
        abs_path.write_text(content, encoding="utf-8")
        self.load()
        return self.get_asset_dict(asset_id) or {"id": asset_id}

    def update_asset(self, asset_id: str, tags: Optional[List[str]] = None,
                     priority: Optional[int] = None, body: Optional[str] = None,
                     includes: Optional[List[str]] = None, name: Optional[str] = None,
                     aliases: Optional[List[str]] = None) -> Dict[str, Any]:
        """Update an existing asset file (full overwrite of provided fields)."""
        blk = self.get(asset_id)
        if not blk or not blk.path:
            raise RuntimeError(f"Asset '{asset_id}' not found")

        new_tags = tags if tags is not None else blk.tags
        new_priority = priority if priority is not None else blk.priority
        new_body = body if body is not None else blk.body
        new_includes = includes if includes is not None else blk.includes
        new_name = name if name is not None else blk.name
        new_aliases = aliases if aliases is not None else blk.aliases

        frontmatter: Dict[str, Any] = {
            "id": asset_id, "type": blk.type, "tags": new_tags, "priority": new_priority,
        }
        if new_includes:
            frontmatter["includes"] = new_includes
        if new_name:
            frontmatter["name"] = new_name
        if new_aliases:
            frontmatter["aliases"] = new_aliases

        content = "---\n" + yaml.dump(frontmatter, default_flow_style=False).strip() + "\n---\n\n" + new_body.strip() + "\n"
        blk.path.write_text(content, encoding="utf-8")
        self.load()
        return self.get_asset_dict(asset_id) or {"id": asset_id}

    def delete_asset(self, asset_id: str) -> bool:
        """Delete an asset file and reload."""
        blk = self.get(asset_id)
        if not blk or not blk.path:
            raise RuntimeError(f"Asset '{asset_id}' not found")
        blk.path.unlink()
        self.load()
        return True

    def append_learned(self, asset_id: str, content: str) -> Dict[str, Any]:
        """Append content to the ## Learned section of an asset."""
        blk = self.get(asset_id)
        if not blk or not blk.path:
            raise RuntimeError(f"Asset '{asset_id}' not found")

        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        bullet = f"- {content.strip()} ({timestamp})"

        file_content = blk.path.read_text(encoding="utf-8")
        if "## Learned" in file_content:
            file_content = file_content.rstrip() + "\n" + bullet + "\n"
        else:
            file_content = file_content.rstrip() + "\n\n## Learned\n\n" + bullet + "\n"

        blk.path.write_text(file_content, encoding="utf-8")
        self.load()
        return self.get_asset_dict(asset_id) or {"id": asset_id}

    # ── Private helpers ───────────────────────────────────────────────────

    @staticmethod
    def _extract_learned(body: str) -> List[str]:
        """Extract bullet items from ## Learned section."""
        idx = body.find("## Learned")
        if idx < 0:
            return []
        section = body[idx + len("## Learned"):]
        items = []
        for line in section.split("\n"):
            line = line.strip()
            if line.startswith("- "):
                items.append(line)
            elif line.startswith("## "):
                break
        return items

    @staticmethod
    def _id_to_rel_path(asset_id: str, asset_type: str) -> str:
        """Convert dot-notation ID to a relative file path.
        e.g. persona.engineer -> personas/engineer.md
             rules.backend.base -> rules/backend/base.md
        """
        type_dirs = {
            "persona": "personas", "rule": "rules", "policy": "policies",
            "style": "styles", "repo": "repos",
        }
        parts = asset_id.split(".")
        # First part is the type prefix — skip it
        if len(parts) > 1 and parts[0] in ("persona", "rule", "rules", "policy", "style", "repo"):
            parts = parts[1:]
        dir_name = type_dirs.get(asset_type, asset_type + "s")
        return dir_name + "/" + "/".join(parts) + ".md"
