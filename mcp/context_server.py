"""Context MCP server — FastMCP SSE bridge on port 8093.

Proxies agent-facing tools to the Flask /api/context/* REST APIs.
Follows the same pattern as workspace (8091) and abilities (8092) servers.

Tools:
  code_search           — Semantic search across indexed repo code
  structure_search      — AST structure search for classes, functions
  analyze_code          — Analyze a class/file before and after changes
  memory_bank_search    — Semantic search within memory bank markdown files
  research              — Preferred AI-facing tool for broad code exploration
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import logging
import os
import sys
from typing import Literal

import requests
from mcp.server.fastmcp import FastMCP
from context.impact import build_impact_surface

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
logger = logging.getLogger("savant-context-mcp")

# Parse args early so host/port can be passed to FastMCP constructor
_parser = argparse.ArgumentParser(description="Savant Context MCP Server")
_parser.add_argument("--host", default="127.0.0.1")
_parser.add_argument("--port", type=int, default=8093)
_parser.add_argument("--flask-url", default="http://127.0.0.1:8090")
_parser.add_argument("--transport", default="sse", choices=["sse", "stdio"])
_args, _ = _parser.parse_known_args()

# Default Flask URL (overridden by --flask-url)
FLASK_URL = _args.flask_url

# MCP auth — sys.path already includes mcp/ dir
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auth import auth_headers, install_header_capture

mcp = FastMCP(
    "savant-context",
    instructions=(
        "PHYSICAL CODEBASE CONTEXT SEARCH: Use this server to query actual source code, syntax, class structures, "
        "and code-level dependency graphs. DO NOT use this server for high-level business capability domains, "
        "partner clients, deployable service applications, or developer architecture decisions (use 'savant-knowledge' for those).\n\n"
        "AI AGENT GUIDANCE FOR RESEARCH TOOL:\n"
        "  - 'research' is your PRIMARY AND PREFERRED TOOL for exploring code, architecture, and memory banks. "
        "Always use 'research' first when answering codebase questions, looking for implementations, or researching features.\n"
        "  - Set 'q' to your search query (e.g. 'SessionManager', 'authentication JWT', 'database migration').\n"
        "  - Set 'type' based on intent:\n"
        "      • 'all' (default): Best for general exploration. Searches physical source code, AST structure, dependency graph, AND memory bank documentation.\n"
        "      • 'code': Use when specifically looking for source code implementations, classes, functions, and import graphs (excludes memory bank).\n"
        "      • 'memory': Use when looking specifically for architectural decisions, project design docs, or memory bank history (excludes code).\n"
        "  - Scope lookup with 'repo' (e.g., repo='savant-server' or repo=['savant-client', 'savant-server']).\n\n"
        "Tools:\n"
        "  - research(q, repo, type, limit): Primary codebase & memory research tool for AI agents.\n"
        "  - structure_search(q, repo): AST structural match to pinpoint class/function definitions.\n"
        "  - analyze_code(repo, path, uri, name, class_name, symbol, node_type, diff, code): Detailed code analysis tool."
    ),
    host=_args.host,
    port=_args.port,
)

install_header_capture(mcp)


def _get(path: str, params: dict = None) -> dict:
    r = requests.get(f"{FLASK_URL}{path}", params=params, timeout=30, headers=auth_headers())
    r.raise_for_status()
    return r.json()


def _post(path: str, json: dict = None) -> dict:
    r = requests.post(f"{FLASK_URL}{path}", json=json, timeout=30, headers=auth_headers())
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Internal Helper Functions & MCP Tools
# ---------------------------------------------------------------------------

def _truncate_snippet(content: str, query: str, max_lines: int = 15) -> str:
    """Trim a code snippet around the query match to keep tokens concise for AI agents."""
    if not content:
        return ""
    lines = content.splitlines()
    if len(lines) <= max_lines:
        return content

    # Find line index matching query
    query_lower = (query or "").lower()
    match_idx = 0
    for idx, line in enumerate(lines):
        if query_lower and query_lower in line.lower():
            match_idx = idx
            break

    start = max(0, match_idx - (max_lines // 2))
    end = min(len(lines), start + max_lines)
    snippet = lines[start:end]
    
    prefix = [f"... (lines 1-{start})"] if start > 0 else []
    suffix = [f"... ({len(lines) - end} remaining lines)"] if end < len(lines) else []
    return "\n".join(prefix + snippet + suffix)


def _is_test_file(path: str) -> bool:
    """Check if file path belongs to test directories or files."""
    if not path:
        return False
    path_lower = path.lower()
    return (
        "test_" in path_lower
        or "_test" in path_lower
        or "/tests/" in path_lower
        or "/tests_refactored/" in path_lower
        or "/tests_js/" in path_lower
        or "/tests_ui/" in path_lower
    )


def code_search(
    q: str = None,
    query: str = None,
    repo: str | list[str] = None,
    limit: int = 20,
    exclude_memory_bank: bool = False,
    exclude_tests: bool = True,
) -> dict:
    """Find relevant source-code excerpts by meaning across indexed repositories."""
    effective_q = q or query or ""
    params = {"q": effective_q, "limit": limit * 2 if exclude_tests else limit}
    if repo:
        params["repo"] = ",".join(repo) if isinstance(repo, list) else repo
    if exclude_memory_bank:
        params["exclude_memory_bank"] = "true"

    raw = _get("/api/context/search", params)
    if "results" in raw and isinstance(raw["results"], list):
        items = raw["results"]

        # Prioritize production files over test files unless query specifically targets tests
        if exclude_tests and "test" not in effective_q.lower():
            prod_items = [item for item in items if not _is_test_file(item.get("rel_path", ""))]
            test_items = [item for item in items if _is_test_file(item.get("rel_path", ""))]
            items = prod_items + test_items

        items = items[:limit]
        for item in items:
            if "content" in item:
                item["content"] = _truncate_snippet(item["content"], effective_q)
        raw["results"] = items
        raw["result_count"] = len(items)

    return raw


@mcp.tool()
def structure_search(
    q: str = None,
    query: str = None,
    repo: str | list[str] = None,
    exclude_tests: bool = True,
) -> dict:
    """Find code structures such as classes, functions, and methods using AST data.

    Use this when the symbol shape matters more than semantic similarity.
    """
    effective_query = q or query or ""
    params = {"query": effective_query}
    if repo:
        params["repo"] = ",".join(repo) if isinstance(repo, list) else repo

    raw = _get("/api/context/ast/search", params)
    if "results" in raw and isinstance(raw["results"], list):
        items = raw["results"]

        # Prioritize production AST symbols over test AST symbols
        if exclude_tests and "test" not in effective_query.lower():
            prod_items = [item for item in items if not _is_test_file(item.get("rel_path", ""))]
            test_items = [item for item in items if _is_test_file(item.get("rel_path", ""))]
            items = prod_items + test_items

        raw["results"] = items
        raw["result_count"] = len(items)

    return raw


@mcp.tool()
def analyze_code(
    repo: str | list[str] = None,
    path: str = None,
    uri: str = None,
    name: str = None,
    class_name: str = None,
    symbol: str = None,
    node_type: str = None,
    diff: str = None,
    code: str = None,
) -> dict:
    """Analyze a file, class, symbol, code body, or diff for implementation impact.

    Identify the target with repo plus path, URI, name, class_name, or symbol.
    Supply diff for before/after analysis or code for analysis of a new body.
    """
    payload = {}
    if repo:
        payload["repo"] = ",".join(repo) if isinstance(repo, list) else repo
    if path:
        payload["path"] = path
    if uri:
        payload["uri"] = uri
    if name or class_name or symbol:
        payload["name"] = name or class_name or symbol
    if node_type:
        payload["node_type"] = node_type
    if diff:
        payload["diff"] = diff
    if code:
        payload["code"] = code
    return _post("/api/context/analysis", payload)


def memory_bank_search(
    q: str = None,
    query: str = None,
    repo: str | list[str] = None,
    limit: int = 20,
) -> dict:
    """Search repository memory-bank Markdown & documentation files by meaning."""
    effective_q = q or query or ""
    params = {"q": effective_q, "limit": limit}
    if repo:
        params["repo"] = ",".join(repo) if isinstance(repo, list) else repo

    raw = _get("/api/context/memory/search", params)
    
    # Fallback to general vector search filtering markdown files if memory_bank_search is empty
    if not raw.get("results"):
        all_res = code_search(q=effective_q, repo=repo, limit=limit, exclude_memory_bank=False, exclude_tests=False)
        if isinstance(all_res.get("results"), list):
            md_items = [
                item for item in all_res["results"]
                if (item.get("rel_path") or "").endswith((".md", ".mdx", ".markdown"))
                   or item.get("is_memory_bank") == 1
            ]
            raw["results"] = md_items
            raw["result_count"] = len(md_items)

    if "results" in raw and isinstance(raw["results"], list):
        for item in raw["results"]:
            if "content" in item:
                item["content"] = _truncate_snippet(item["content"], effective_q, max_lines=15)

    return raw


def _build_impact_surface(results: dict, top_symbols: list[str], top_files: set[str]) -> dict:
    return build_impact_surface(results, top_symbols, top_files)


@mcp.tool()
def research(
    q: str,
    repo: str | list[str] = None,
    type: Literal["all", "code", "memory"] = "all",
    limit: int = 20,
    exclude_tests: bool = True,
) -> dict:
    """PRIMARY CODE & CONTEXT SEARCH TOOL FOR AI AGENTS.

    AI AGENT INSTRUCTIONS:
      Use this tool as your single entry-point for searching the codebase, code dependencies, and project memory banks.
      Do not attempt to call individual search tools, as research unifies semantic code search, AST structure match,
      CodeGraph dependencies and memory bank markdown search in one call.

    PARAM GUIDANCE FOR AGENTS:
      • q (str, required): The search query concept, symbol name, or topic (e.g. "auth middleware", "SessionDB", "user routes").
      • repo (str | list[str], optional): Limit search scope to specific repository/repositories.
      • type (str, optional): Controls search scope. Must be one of:
          - "all" (default): Comprehensive search across code, AST structure, code graph, and memory bank documentation.
          - "code": Search source code files, AST definitions, and dependency graph (omits memory bank docs).
          - "memory": Search architectural docs and memory bank markdown files only (omits source code).
      • limit (int, optional, default=20): Max result count per section.
      • exclude_tests (bool, optional, default=True): Prioritizes core production source code over test files.

    RETURN STRUCTURE FOR AGENTS:
      Returns a JSON dictionary containing:
        - 'overview': Executive summary of top symbols, files, and match counts.
        - 'impact_surface': Upstream (1 level up) callers/importers and downstream (1 level down) dependencies.
        - 'code_search': High-signal production code snippets.
        - 'structure_search': AST class and function definition lines.
        - 'code_graph_search': Readable CodeGraph dependency and call chains.
        - 'memory_bank_search': Architectural documentation and markdown bank excerpts.
    """
    payload = {
        "q": q,
        "repo": repo,
        "type": type,
        "limit": limit,
        "exclude_tests": exclude_tests,
    }
    return _post("/api/context/research", json=payload)


# Backward-compatible Python alias. Do not expose as an MCP tool.
code_research = research


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logger.info(f"Context MCP starting on {_args.host}:{_args.port} (transport={_args.transport})")
    logger.info(f"Flask backend: {FLASK_URL}")
    mcp.run(transport=_args.transport)


if __name__ == "__main__":
    main()
