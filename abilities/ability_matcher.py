"""AbilityMatcher — handles tag matching and repo fuzzy matching."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple

from .ability_parser import Block, _string_list
from .ability_index import AbilityIndex

FUZZY_TAG_THRESHOLD = 0.72


def _tag_pair_score(query: str, block_tag: str) -> tuple[str, float] | None:
    if not query or not block_tag:
        return None
    if block_tag == query:
        return "exact", 1.0
    if block_tag.startswith(query) or query.startswith(block_tag):
        return "prefix", 0.94
    if query in block_tag or block_tag in query:
        return "substring", 0.9
    score = SequenceMatcher(None, query, block_tag).ratio()
    return ("fuzzy", score) if score >= FUZZY_TAG_THRESHOLD else None


def _best_tag_match(query_tags: list[str], block_tags: list[str]) -> tuple[int, int, str, float] | None:
    best = None
    for block_index, block_tag in enumerate(block_tags):
        for query_index, query_tag in enumerate(query_tags):
            candidate = _tag_pair_score(query_tag, block_tag)
            if candidate and (best is None or candidate[1] > best[3]):
                best = (query_index, block_index, candidate[0], candidate[1])
    return best


class AbilityMatcher:
    """Provides search and matching operations across indexed ability blocks."""

    def __init__(self, index: AbilityIndex) -> None:
        self.index = index

    @staticmethod
    def _block_type_allowed(block: Block, allowed_types: Optional[Set[str]]) -> bool:
        return not allowed_types or block.type in allowed_types

    def _exact_tag_matches(
        self,
        raw_tags: list[str],
        tags: list[str],
        normalized_tags: list[str],
        allowed_types: Optional[Set[str]],
    ) -> tuple[list[Tuple[Block, Dict[str, Any]]], set[str]]:
        matches = []
        seen = set()
        for index, tag in enumerate(tags):
            for block_id in self.index.ids_by_tag.get(tag, []):
                block = self.index.blocks_by_id[block_id]
                if block_id in seen or not self._block_type_allowed(block, allowed_types):
                    continue
                seen.add(block_id)
                matches.append((block, {
                    "query_tag": raw_tags[index],
                    "query_norm": normalized_tags[index],
                    "block_tag": tag,
                    "match_type": "exact",
                    "score": 1.0,
                }))
        return matches, seen

    def _fuzzy_tag_matches(
        self,
        raw_tags: list[str],
        normalized_tags: list[str],
        allowed_types: Optional[Set[str]],
        seen: set[str],
    ) -> list[Tuple[Block, Dict[str, Any]]]:
        matches = []
        for block_id, block in self.index.blocks_by_id.items():
            if block_id in seen or not self._block_type_allowed(block, allowed_types):
                continue
            block_tags = list(block.tags or [])
            normalized_block_tags = [self._norm_key(tag) for tag in block_tags]
            match = _best_tag_match(normalized_tags, normalized_block_tags)
            if match is None:
                continue
            query_index, block_index, match_type, score = match
            matches.append((block, {
                "query_tag": raw_tags[query_index],
                "query_norm": normalized_tags[query_index],
                "block_tag": block_tags[block_index],
                "block_norm": normalized_block_tags[block_index],
                "match_type": match_type,
                "score": float(score),
            }))
        return matches

    def blocks_with_tags(
        self, tags: List[str] | str, allowed_types: Optional[Set[str]] = None
    ) -> List[Tuple[Block, Dict[str, Any]]]:
        qtags_raw = _string_list(tags)
        qtags: List[str] = [t.lower() for t in qtags_raw]
        qtags_norm: List[str] = [self._norm_key(t) for t in qtags]
        exact_matches, seen = self._exact_tag_matches(
            qtags_raw, qtags, qtags_norm, allowed_types
        )
        return exact_matches + self._fuzzy_tag_matches(
            qtags_raw, qtags_norm, allowed_types, seen
        )

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

    def _repo_key_match(
        self, query: str, query_norm: str, query_compact: str, key: str
    ) -> Dict[str, Any]:
        key_norm = self._norm_key(key)
        key_compact = self._compact(key_norm)
        if query_norm == key_norm:
            method, score = "exact", 1.0
        elif query_compact and query_compact == key_compact:
            method, score = "compact", 0.97
        elif key_norm.startswith(query_norm) or query_norm.startswith(key_norm):
            method, score = "prefix", 0.94
        else:
            method, score = "fuzzy", SequenceMatcher(None, query_norm, key_norm).ratio()
        return {"method": method, "matched_key": key, "query": query, "score": score}

    def _best_repo_key_match(
        self, block: Block, query: str, query_norm: str, query_compact: str
    ) -> Dict[str, Any]:
        matches = [
            self._repo_key_match(query, query_norm, query_compact, key)
            for key in self._repo_keys(block)
        ]
        return max(matches, key=lambda detail: detail["score"])

    def find_repo_fuzzy(
        self, query: str
    ) -> Tuple[Optional[Block], Optional[Dict[str, Any]]]:
        if not query:
            return None, None
        raw = str(query).strip()
        q_norm = self._norm_key(raw)
        if not q_norm:
            return None, None
        q_comp = self._compact(q_norm)
        blocks = [
            self.index.blocks_by_id[block_id]
            for block_id in self.index.ids_by_type.get("repo", []) or []
            if block_id in self.index.blocks_by_id
        ]
        candidates = [
            (block, self._best_repo_key_match(block, query, q_norm, q_comp))
            for block in blocks
        ]
        if not candidates:
            return None, None
        block, detail = min(
            candidates,
            key=lambda item: (-item[1]["score"], -item[0].priority, item[0].id),
        )
        return (block, detail) if detail["score"] >= 0.6 else (None, None)
