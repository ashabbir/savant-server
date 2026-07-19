"""Ability block parser and dataclass models."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

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


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [text for item in values if (text := str(item).strip())]


def _normalized_tags(value: Any) -> List[str]:
    return [
        tag.lower()
        for item in _string_list(value)
        for tag in re.split(r"[\s,]+", item)
        if tag
    ]


def _parse_block(path: Path) -> Block:
    content = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(content)
    if not match:
        raise RuntimeError(f"Missing YAML front matter in {path}")
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise RuntimeError(f"YAML front matter must be a mapping in {path}")
    for key in ("id", "type", "tags", "priority"):
        if key not in meta:
            raise RuntimeError(f"Missing required field '{key}' in {path}")
    supersedes = meta.get("supersedes")
    name = meta.get("name")
    return Block(
        id=str(meta["id"]).strip(),
        type=str(meta["type"]).strip(),
        tags=_normalized_tags(meta["tags"]),
        priority=int(meta["priority"]),
        includes=_string_list(meta.get("includes")),
        deprecated=bool(meta.get("deprecated", False)),
        supersedes=str(supersedes) if supersedes else None,
        name=str(name).strip() if name is not None else None,
        aliases=_string_list(meta.get("aliases")),
        body=match.group(2).strip(),
        path=path,
    )
