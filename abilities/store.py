"""AbilityStore — compatibility facade for ability index, matcher, validator, and asset CRUD services.

Source of truth: server abilities directory resolved from env / server data.
Each .md file has YAML frontmatter (id, type, tags, priority, includes) + markdown body.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .ability_parser import Block, FRONTMATTER_RE, _normalized_tags, _parse_block, _string_list
from .ability_index import AbilityIndex
from .ability_matcher import AbilityMatcher
from .ability_validator import AbilityValidator
from .ability_assets import AbilityAssetService

__all__ = ["AbilityStore", "Block"]


class AbilityStore:
    """Facade for AbilityIndex, AbilityMatcher, AbilityValidator, and AbilityAssetService."""

    def __init__(self, base_path: Path) -> None:
        self.index = AbilityIndex(base_path)
        self.matcher = AbilityMatcher(self.index)
        self.validator = AbilityValidator(self.index)
        self.assets = AbilityAssetService(self.index)

    @property
    def base_path(self) -> Path:
        return self.index.base_path

    @property
    def blocks_by_id(self) -> Dict[str, Block]:
        return self.index.blocks_by_id

    @blocks_by_id.setter
    def blocks_by_id(self, val: Dict[str, Block]) -> None:
        self.index.blocks_by_id = val

    @property
    def ids_by_tag(self) -> Dict[str, List[str]]:
        return self.index.ids_by_tag

    @ids_by_tag.setter
    def ids_by_tag(self, val: Dict[str, List[str]]) -> None:
        self.index.ids_by_tag = val

    @property
    def ids_by_type(self) -> Dict[str, List[str]]:
        return self.index.ids_by_type

    @ids_by_type.setter
    def ids_by_type(self, val: Dict[str, List[str]]) -> None:
        self.index.ids_by_type = val

    @property
    def include_edges(self) -> Dict[str, List[str]]:
        return self.index.include_edges

    @include_edges.setter
    def include_edges(self, val: Dict[str, List[str]]) -> None:
        self.index.include_edges = val

    def load(self) -> None:
        self.index.load()
        self.validator.check_cycles()

    def _load_file(self, path: Path) -> None:
        self.index._load_file(path)

    def get(self, block_id: str) -> Optional[Block]:
        return self.index.get(block_id)

    def find_persona(self, name_or_id: str) -> Optional[Block]:
        return self.index.find_persona(name_or_id)

    def blocks_with_tags(
        self, tags: List[str] | str, allowed_types: Optional[Set[str]] = None
    ) -> List[Tuple[Block, Dict[str, Any]]]:
        return self.matcher.blocks_with_tags(tags, allowed_types=allowed_types)

    def find_repo_fuzzy(
        self, query: str
    ) -> Tuple[Optional[Block], Optional[Dict[str, Any]]]:
        return self.matcher.find_repo_fuzzy(query)

    def stats(self) -> Dict[str, int]:
        return self.index.stats()

    def validate_includes(self, raise_on_error: bool = True) -> bool:
        return self.validator.validate_includes(raise_on_error=raise_on_error)

    def validate_all(self) -> None:
        self.validator.validate_all()

    def _check_cycles(self) -> None:
        self.validator.check_cycles()

    def get_asset_dict(self, block_id: str) -> Optional[Dict[str, Any]]:
        return self.assets.get_asset_dict(block_id)

    def list_assets_grouped(self) -> Dict[str, List[Dict[str, Any]]]:
        return self.assets.list_assets_grouped()

    def create_asset(
        self,
        asset_type: str,
        asset_id: str,
        tags: List[str],
        priority: int,
        body: str = "",
        includes: Optional[List[str]] = None,
        name: Optional[str] = None,
        aliases: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return self.assets.create_asset(
            asset_type, asset_id, tags, priority, body, includes, name, aliases
        )

    def update_asset(
        self,
        asset_id: str,
        tags: Optional[List[str]] = None,
        priority: Optional[int] = None,
        body: Optional[str] = None,
        includes: Optional[List[str]] = None,
        name: Optional[str] = None,
        aliases: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return self.assets.update_asset(
            asset_id, tags, priority, body, includes, name, aliases
        )

    def delete_asset(self, asset_id: str) -> bool:
        return self.assets.delete_asset(asset_id)

    def append_learned(self, asset_id: str, content: str) -> Dict[str, Any]:
        return self.assets.append_learned(asset_id, content)

    @staticmethod
    def _extract_learned(body: str) -> List[str]:
        return AbilityAssetService._extract_learned(body)

    @staticmethod
    def _id_to_rel_path(asset_id: str, asset_type: str) -> str:
        return AbilityAssetService._id_to_rel_path(asset_id, asset_type)
