"""
Resolver — composes deterministic prompts from persona + tags + optional repo overlay.

Resolution algorithm:
1. Load persona block → expand includes recursively
2. Load repo overlay (optional) → expand its includes
3. Merge effective tags (user tags ∪ repo tags)
4. Match tags against all rules/policies (exact → prefix → substring → fuzzy ≥ 0.72)
5. Deduplicate by ID, sort by (-priority, type_order, id)
6. Render sections: Persona → Repo Constraints → Rules → Policies & Style
7. Return: composed prompt + manifest (applied IDs, order, SHA-256 hash)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .store import AbilityStore, Block

TYPE_ORDER = {
    "persona": 0,
    "repo": 1,
    "rule": 2,
    "policy": 3,
    "style": 4,
}


@dataclass
class _Selection:
    blocks: Dict[str, Block] = field(default_factory=dict)
    trace: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, block: Block, reason: str, detail: Optional[Dict[str, Any]] = None) -> None:
        previous = self.blocks.get(block.id)
        if not previous or block.priority > previous.priority or (
            block.priority == previous.priority and block.id < previous.id
        ):
            self.blocks[block.id] = block
        item: Dict[str, Any] = {
            "id": block.id,
            "type": block.type,
            "priority": block.priority,
            "reason": reason,
        }
        if detail is not None:
            item["detail"] = detail
        self.trace.append(item)


class Resolver:
    def __init__(self, store: AbilityStore):
        self.store = store

    def resolve(
        self,
        persona: str,
        tags: List[str],
        repo_id: Optional[str] = None,
        include_trace: bool = False,
    ) -> Dict[str, Any]:
        persona_block = self.store.find_persona(persona)
        if not persona_block:
            raise RuntimeError(f"Unknown persona: {persona}")

        selection = _Selection()
        selection.add(persona_block, "persona")
        self._expand_includes(persona_block, selection.add)
        repo_block = self._select_repo(repo_id, selection)
        effective_tags = self._effective_tags(tags, repo_block)
        self._select_tag_matches(effective_tags, selection)
        ordered = sorted(
            selection.blocks.values(),
            key=lambda b: (-b.priority, TYPE_ORDER.get(b.type, 99), b.id),
        )
        response = self._build_response(persona_block, repo_block, ordered)
        if include_trace:
            response["trace"] = selection.trace
        return response

    def _select_repo(self, repo_id: Optional[str], selection: _Selection) -> Optional[Block]:
        if not repo_id:
            return None
        repo_block, detail = self.store.find_repo_fuzzy(str(repo_id))
        if not repo_block or repo_block.type != "repo":
            return None
        selection.add(repo_block, f"repo:{repo_block.id}", detail={"repo_match": detail or {}})
        self._expand_includes(repo_block, selection.add)
        return repo_block

    @staticmethod
    def _effective_tags(tags: List[str], repo_block: Optional[Block]) -> List[str]:
        tag_values = [tags] if isinstance(tags, str) else (tags or [])
        tag_set: Set[str] = {tag.strip() for tag in tag_values if tag and tag.strip()}
        tag_set.update(tag.strip() for tag in (repo_block.tags if repo_block else []) if tag and tag.strip())
        return sorted(tag_set)

    def _select_tag_matches(self, effective_tags: List[str], selection: _Selection) -> None:
        if not effective_tags:
            return
        allowed_types: Set[str] = {"rule", "policy", "style"}
        for block, info in self.store.blocks_with_tags(effective_tags, allowed_types=allowed_types):
            selection.add(block, "tag-match", detail={"effective_tags": effective_tags, "hit": info})
            self._expand_includes(block, selection.add)

    def _build_response(self, persona_block: Block, repo_block: Optional[Block], ordered: List[Block]) -> Dict[str, Any]:
        persona_section = self._render_section("Persona", [persona_block])
        repo_section = self._render_section("Repo Constraints", [repo_block] if repo_block else [])
        others = [b for b in ordered if b.id not in {persona_block.id, repo_block.id if repo_block else ""}]
        rules = [b for b in others if b.type == "rule"]
        policies = [b for b in others if b.type in {"policy", "style"}]
        rules_section = self._render_section("Rules", rules)
        policies_section = self._render_section("Policies & Style", policies)

        prompt = "\n\n".join(s for s in [persona_section, repo_section, rules_section, policies_section] if s)

        applied = {
            "persona": persona_block.id,
            "repo": repo_block.id if repo_block else "",
            "rules": [b.id for b in rules],
            "policies": [b.id for b in policies],
        }

        manifest = {
            "applied": applied,
            "order": [b.id for b in ordered],
            "hash": hashlib.sha256(
                (prompt + "\n" + ",".join(applied.get("rules", []))).encode("utf-8")
            ).hexdigest(),
        }

        # Shape output: expand IDs to body text
        rule_bodies = [self.store.blocks_by_id[r].body if r in self.store.blocks_by_id else r for r in applied["rules"]]
        policy_bodies = [self.store.blocks_by_id[p].body if p in self.store.blocks_by_id else p for p in applied["policies"]]
        repo_body = self.store.blocks_by_id[applied["repo"]].body if applied["repo"] and applied["repo"] in self.store.blocks_by_id else ""

        return {
            "persona": persona_block.body,
            "repo": repo_body,
            "rules": rule_bodies,
            "policies": policy_bodies,
            "prompt": prompt,
            "manifest": manifest,
        }

    def _expand_includes(self, blk: Block, add) -> None:
        """Expand includes depth-first while tolerating cycles and deep chains."""
        visited = {blk.id}
        pending = [(blk, include_id) for include_id in reversed(blk.includes or [])]
        while pending:
            parent, include_id = pending.pop()
            included = self.store.get(include_id)
            if not included:
                raise RuntimeError(f"Unknown include '{include_id}' in {parent.id}")
            if included.id in visited:
                continue
            visited.add(included.id)
            add(included, f"include:{parent.id}", detail={"include_of": parent.id})
            pending.extend((included, child_id) for child_id in reversed(included.includes or []))

    @staticmethod
    def _render_section(title: str, blocks: List[Block]) -> str:
        if not blocks:
            return ""
        parts: List[str] = [f"# {title}"]
        for b in blocks:
            parts.append(f"<!-- {b.id} (priority {b.priority}) -->\n{b.body}".strip())
        return "\n\n".join(parts).strip()
