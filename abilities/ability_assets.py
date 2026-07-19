"""AbilityAssetService — handles CRUD operations and learned sections for ability assets."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .ability_index import AbilityIndex


class AbilityAssetService:
    """Provides high-level CRUD operations for ability asset files."""

    def __init__(self, index: AbilityIndex) -> None:
        self.index = index

    @property
    def base_path(self) -> Path:
        return self.index.base_path

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

    def get_asset_dict(self, block_id: str) -> Optional[Dict[str, Any]]:
        """Return a JSON-serializable dict for an asset."""
        blk = self.index.get(block_id)
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
        for bid in sorted(self.index.blocks_by_id.keys()):
            blk = self.index.blocks_by_id[bid]
            entry = self.get_asset_dict(bid)
            if entry:
                grouped.setdefault(blk.type, []).append(entry)
        return grouped

    def create_asset(self, asset_type: str, asset_id: str, tags: List[str],
                     priority: int, body: str = "", includes: Optional[List[str]] = None,
                     name: Optional[str] = None, aliases: Optional[List[str]] = None) -> Dict[str, Any]:
        """Create a new asset file and reload the index."""
        if asset_id in self.index.blocks_by_id:
            raise RuntimeError(f"Asset '{asset_id}' already exists")

        # Derive file path from id: persona.engineer -> personas/engineer.md
        rel = self._id_to_rel_path(asset_id, asset_type)
        abs_path = self.base_path / "abilities" / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        frontmatter: Dict[str, Any] = {"id": asset_id, "type": asset_type, "tags": tags, "priority": priority}
        if includes:
            frontmatter["includes"] = includes
        if name:
            frontmatter["name"] = name
        if aliases:
            frontmatter["aliases"] = aliases

        content = "---\n" + yaml.dump(frontmatter, default_flow_style=False).strip() + "\n---\n\n" + body.strip() + "\n"
        abs_path.write_text(content, encoding="utf-8")
        self.index.load()
        return self.get_asset_dict(asset_id) or {"id": asset_id}

    def update_asset(self, asset_id: str, tags: Optional[List[str]] = None,
                     priority: Optional[int] = None, body: Optional[str] = None,
                     includes: Optional[List[str]] = None, name: Optional[str] = None,
                     aliases: Optional[List[str]] = None) -> Dict[str, Any]:
        """Update an existing asset file (full overwrite of provided fields)."""
        blk = self.index.get(asset_id)
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
        self.index.load()
        return self.get_asset_dict(asset_id) or {"id": asset_id}

    def delete_asset(self, asset_id: str) -> bool:
        """Delete an asset file and reload."""
        blk = self.index.get(asset_id)
        if not blk or not blk.path:
            raise RuntimeError(f"Asset '{asset_id}' not found")
        blk.path.unlink()
        self.index.load()
        return True

    def append_learned(self, asset_id: str, content: str) -> Dict[str, Any]:
        """Append content to the ## Learned section of an asset."""
        blk = self.index.get(asset_id)
        if not blk or not blk.path:
            raise RuntimeError(f"Asset '{asset_id}' not found")

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        bullet = f"- {content.strip()} ({timestamp})"

        file_content = blk.path.read_text(encoding="utf-8")
        if "## Learned" in file_content:
            file_content = file_content.rstrip() + "\n" + bullet + "\n"
        else:
            file_content = file_content.rstrip() + "\n\n## Learned\n\n" + bullet + "\n"

        blk.path.write_text(file_content, encoding="utf-8")
        self.index.load()
        return self.get_asset_dict(asset_id) or {"id": asset_id}
