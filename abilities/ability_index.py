"""AbilityIndex — loads and indexes ability blocks from the filesystem."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from .ability_parser import Block, _parse_block

logger = logging.getLogger(__name__)


class AbilityIndex:
    """Loads and maintains in-memory lookup indices for ability blocks."""

    def __init__(self, base_path: Path) -> None:
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

    def _load_file(self, path: Path) -> None:
        block = _parse_block(path)
        if block.id in self.blocks_by_id:
            logger.warning(
                "Duplicate id '%s' in %s — skipping", block.id, path
            )
            return
        self.blocks_by_id[block.id] = block
        self.ids_by_type.setdefault(block.type, []).append(block.id)
        for tag in block.tags:
            self.ids_by_tag.setdefault(tag, []).append(block.id)

    def get(self, block_id: str) -> Optional[Block]:
        return self.blocks_by_id.get(block_id)

    def find_persona(self, name_or_id: str) -> Optional[Block]:
        if name_or_id.startswith("persona."):
            return self.get(name_or_id)
        return self.get(f"persona.{name_or_id}")

    def stats(self) -> Dict[str, int]:
        return {
            "personas": len(self.ids_by_type.get("persona", [])),
            "rules": len(self.ids_by_type.get("rule", [])),
            "policies": len(self.ids_by_type.get("policy", [])),
            "styles": len(self.ids_by_type.get("style", [])),
            "repos": len(self.ids_by_type.get("repo", [])),
        }
