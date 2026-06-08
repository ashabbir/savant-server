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
from typing import Any, Dict, List, Optional, Set

from .store import AbilityStore, Block

TYPE_ORDER = {
    "persona": 0,
    "repo": 1,
    "rule": 2,
    "policy": 3,
    "style": 4,
}


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

        selected: Dict[str, Block] = {}
        trace: List[Dict[str, Any]] = []

        def add_block(b: Block, reason: str, detail: Optional[Dict[str, Any]] = None) -> None:
            prev = selected.get(b.id)
            if not prev or b.priority > prev.priority or (b.priority == prev.priority and b.id < prev.id):
                selected[b.id] = b
            tr: Dict[str, Any] = {"id": b.id, "type": b.type, "priority": b.priority, "reason": reason}
            if detail is not None:
                tr["detail"] = detail
            trace.append(tr)

        # 1) Persona
        add_block(persona_block, "persona")

        # 2) Persona includes (recursive)
        self._expand_includes(persona_block, add_block)

        # 3) Repo overlay
        repo_block = None
        if repo_id:
            repo_block, repo_detail = self.store.find_repo_fuzzy(str(repo_id))
            if repo_block and repo_block.type != "repo":
                repo_block = None
            if repo_block:
                add_block(repo_block, f"repo:{repo_block.id}", detail={"repo_match": repo_detail or {}})
                self._expand_includes(repo_block, add_block)

        # Effective tags = user tags ∪ repo tags
        effective_tags: List[str] = []
        if tags or (repo_block and repo_block.tags):
            tag_set: Set[str] = {t.strip() for t in (tags or []) if t and t.strip()}
            if repo_block and repo_block.tags:
                for t in repo_block.tags:
                    if t:
                        tag_set.add(t.strip())
            effective_tags = sorted(tag_set)

        # 4) Rules/policies matching effective tags
        allowed_types: Set[str] = {"rule", "policy", "style"}
        matched = self.store.blocks_with_tags(effective_tags, allowed_types=allowed_types) if effective_tags else []
        for blk, info in matched:
            add_block(blk, "tag-match", detail={"effective_tags": effective_tags, "hit": info})
            self._expand_includes(blk, add_block)

        # 5) Deduplicate and order deterministically
        ordered = sorted(
            selected.values(),
            key=lambda b: (-b.priority, TYPE_ORDER.get(b.type, 99), b.id),
        )

        # 6) Render prompt by sections
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

        resp: Dict[str, Any] = {
            "persona": persona_block.body,
            "repo": repo_body,
            "rules": rule_bodies,
            "policies": policy_bodies,
            "prompt": prompt,
            "manifest": manifest,
        }
        if include_trace:
            resp["trace"] = trace
        return resp

    def _expand_includes(self, blk: Block, add) -> None:
        for inc_id in blk.includes or []:
            inc = self.store.get(inc_id)
            if not inc:
                raise RuntimeError(f"Unknown include '{inc_id}' in {blk.id}")
            add(inc, f"include:{blk.id}", detail={"include_of": blk.id})
            self._expand_includes(inc, add)

    @staticmethod
    def _render_section(title: str, blocks: List[Block]) -> str:
        if not blocks:
            return ""
        parts: List[str] = [f"# {title}"]
        for b in blocks:
            parts.append(f"<!-- {b.id} (priority {b.priority}) -->\n{b.body}".strip())
        return "\n\n".join(parts).strip()
