"""Lossless source-tree generation for Context.

The legacy Context AST intentionally keeps only declarations.  This module
keeps the complete source together with a compact concrete syntax tree so an
agent can request exact, bounded source context without reparsing a checkout.
"""

from __future__ import annotations

import hashlib
import warnings
from pathlib import Path
from typing import Any


TREE_SCHEMA_VERSION = 1

# Keep this independent from ``Indexer`` so the lossless representation can be
# used by structural CodeGraph jobs without importing the semantic indexer.
EXTENSION_TO_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".mts": "typescript", ".cts": "typescript",
    ".html": "html", ".htm": "html", ".css": "css", ".rb": "ruby",
    ".java": "java", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
    ".cxx": "cpp", ".c": "c", ".h": "c", ".go": "go", ".rs": "rust",
    ".php": "php", ".cs": "c_sharp", ".swift": "swift", ".scala": "scala",
    ".kt": "kotlin", ".kts": "kotlin", ".sh": "bash", ".bash": "bash",
    ".zsh": "bash", ".fish": "bash", ".dart": "dart", ".ex": "elixir",
    ".exs": "elixir", ".erl": "erlang", ".hrl": "erlang", ".lua": "lua",
    ".r": "r", ".sql": "sql", ".m": "objc", ".mm": "objc",
    ".sol": "solidity", ".graphql": "graphql", ".gql": "graphql",
    ".hcl": "hcl", ".tf": "hcl", ".tfvars": "hcl", ".json": "json",
    ".jsonc": "json", ".toml": "toml", ".yml": "yaml", ".yaml": "yaml",
    ".md": "markdown", ".mdx": "markdown",
}


def source_hash(source: str) -> str:
    """Return the exact UTF-8 source fingerprint used for freshness checks."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _point(point: tuple[int, int]) -> list[int]:
    return [point[0] + 1, point[1]]


def _source_only_tree(language: str | None, reason: str | None = None) -> dict[str, Any]:
    tree: dict[str, Any] = {
        "schema_version": TREE_SCHEMA_VERSION,
        "language": language,
        "parser": "source-only",
        "root_id": 0,
        "nodes": [{"id": 0, "parent_id": None, "type": "source_file", "named": True,
                   "start_byte": 0, "end_byte": 0, "start_point": [1, 0], "end_point": [1, 0]}],
    }
    if reason:
        tree["warning"] = reason
    return tree


def _end_point(source: str) -> list[int]:
    """Return Tree-sitter-compatible (one-based line, byte-column) endpoint."""
    final_line = source.rsplit("\n", 1)[-1]
    return [source.count("\n") + 1, len(final_line.encode("utf-8"))]


def build_lossless_tree(file_path: str, source: str) -> dict[str, Any]:
    """Build a compact concrete tree while preserving reconstructible source.

    Tree-sitter intentionally treats whitespace as extras.  The source is
    retained verbatim, and every node keeps byte ranges into it, so comments,
    whitespace, punctuation, and malformed source remain recoverable.
    """
    language_name = EXTENSION_TO_LANGUAGE.get(Path(file_path).suffix.lower())
    artifact: dict[str, Any] = {
        "source_hash": source_hash(source),
        "grammar": language_name or "text",
        "schema_version": TREE_SCHEMA_VERSION,
        "source": source,
    }
    if not language_name:
        artifact["tree"] = _source_only_tree(None, "No Tree-sitter grammar is configured for this extension")
        artifact["tree"]["nodes"][0]["end_byte"] = len(source.encode("utf-8"))
        artifact["tree"]["nodes"][0]["end_point"] = _end_point(source)
        return artifact

    try:
        import tree_sitter_languages
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            parser = tree_sitter_languages.get_parser(language_name)
        tree = parser.parse(source.encode("utf-8"))
    except Exception as exc:  # Lossless source is still useful when parsing is unavailable.
        artifact["tree"] = _source_only_tree(language_name, str(exc))
        artifact["tree"]["nodes"][0]["end_byte"] = len(source.encode("utf-8"))
        artifact["tree"]["nodes"][0]["end_point"] = _end_point(source)
        return artifact

    nodes: list[dict[str, Any]] = []

    def visit(node: Any, parent_id: int | None) -> None:
        node_id = len(nodes)
        nodes.append({
            "id": node_id,
            "parent_id": parent_id,
            "type": node.type,
            "named": bool(node.is_named),
            "start_byte": node.start_byte,
            "end_byte": node.end_byte,
            "start_point": _point(node.start_point),
            "end_point": _point(node.end_point),
            "has_error": bool(node.has_error),
            "is_error": bool(node.is_error),
        })
        for child in node.children:
            visit(child, node_id)

    visit(tree.root_node, None)
    artifact["tree"] = {
        "schema_version": TREE_SCHEMA_VERSION,
        "language": language_name,
        "parser": "tree-sitter",
        "root_id": 0,
        "has_error": bool(tree.root_node.has_error),
        "nodes": nodes,
    }
    return artifact


def bounded_tree(artifact: dict[str, Any], start_line: int | None = None,
                 end_line: int | None = None, max_nodes: int = 500) -> dict[str, Any]:
    """Return an MCP-safe source and node slice without mutating persistence."""
    source = artifact.get("source", "")
    lines = source.splitlines(keepends=True)
    total_lines = max(1, len(lines))
    first = max(1, int(start_line or 1))
    last = min(total_lines, int(end_line or total_lines))
    if last < first:
        first, last = last, first
    byte_start = len("".join(lines[: first - 1]).encode("utf-8"))
    byte_end = len("".join(lines[:last]).encode("utf-8"))
    selected = [node for node in artifact.get("tree", {}).get("nodes", [])
                if node["end_byte"] >= byte_start and node["start_byte"] <= byte_end]
    selected = selected[: max(1, min(int(max_nodes), 2000))]
    return {
        "source_hash": artifact.get("source_hash"),
        "grammar": artifact.get("grammar"),
        "schema_version": artifact.get("schema_version"),
        "source": "".join(lines[first - 1:last]),
        "source_range": {"start_line": first, "end_line": last,
                         "start_byte": byte_start, "end_byte": byte_end},
        "tree": {key: value for key, value in artifact.get("tree", {}).items() if key != "nodes"},
        "nodes": selected,
        "incomplete": len(selected) < len([node for node in artifact.get("tree", {}).get("nodes", [])
                                               if node["end_byte"] >= byte_start and node["start_byte"] <= byte_end]),
    }
