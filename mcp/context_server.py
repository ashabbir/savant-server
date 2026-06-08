"""Context MCP server — FastMCP SSE bridge on port 8093.

Proxies 6 tools to the Flask /api/context/* REST API.
Follows the same pattern as workspace (8091) and abilities (8092) servers.

Tools:
  code_search          — Semantic search across indexed repo code
  structure_search     — AST structure search for classes, functions
  analyze_code         — Analyze a class/file before and after changes
  memory_bank_search   — Semantic search within memory bank markdown files
  memory_resources_list — List all memory bank resources (optional repo filter)
  memory_resources_read — Read a specific memory bank resource by URI
  repos_list           — List indexed repos with README excerpts
  repo_status          — Per-repo index status counts
"""

import argparse
import logging
import os
import sys

import requests
from mcp.server.fastmcp import FastMCP

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
        "Semantic code search, AST structure exploration, and memory bank across indexed repositories. "
        "Use code_search(query, repo) for semantic search across repo source code. "
        "Use memory_bank_search(query, repo) for semantic search within memory bank markdown. "
        "Use memory_resources_list(repo) to browse available memory bank files; memory_resources_read(uri) to read one. "
        "Use repos_list() to see all indexed repos with README excerpts; repo_status() for index health. "
        "Use structure_search(query) to find classes, functions, or language elements via substring AST matching. "
        "Use analyze_code(name, repo, path, node_type, diff, code) to get before/after complexity and findings for a class or file. "
        "The response includes a recommendation to refactor with TDD: write failing tests first, implement the smallest fix, then re-run analysis. "
        "All tools accept an optional repo filter (string name or list of names)."
    ),
    host=_args.host,
    port=_args.port,
)

install_header_capture(mcp)


def _get(path: str, params: dict = None) -> dict:
    try:
        r = requests.get(f"{FLASK_URL}{path}", params=params, timeout=30, headers=auth_headers())
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _post(path: str, json: dict = None) -> dict:
    try:
        r = requests.post(f"{FLASK_URL}{path}", json=json, timeout=30, headers=auth_headers())
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# 6 MCP Tools (same signatures as standalone savant-context)
# ---------------------------------------------------------------------------

@mcp.tool()
def code_search(
    q: str = None,
    query: str = None,
    repo: str | list[str] = None,
    limit: int = 10,
    exclude_memory_bank: bool = False,
) -> dict:
    """Semantic code search across indexed repos (optional repo filter)."""
    params = {"q": q or query or "", "limit": limit}
    if repo:
        params["repo"] = ",".join(repo) if isinstance(repo, list) else repo
    if exclude_memory_bank:
        params["exclude_memory_bank"] = "true"
    return _get("/api/context/search", params)


@mcp.tool()
def structure_search(
    q: str = None,
    query: str = None,
    repo: str | list[str] = None,
) -> dict:
    """AST structure search for code (e.g. classes, functions)."""
    effective_query = q or query or ""
    params = {"query": effective_query}
    if repo:
        params["repo"] = ",".join(repo) if isinstance(repo, list) else repo
    return _get("/api/context/ast/search", params)


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
    """Analyze a class/file before and after a diff or new code body."""
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


@mcp.tool()
def memory_bank_search(
    q: str = None,
    query: str = None,
    repo: str | list[str] = None,
    limit: int = 20,
) -> dict:
    """Semantic search within memory bank markdown (optional repo filter)."""
    params = {"q": q or query or "", "limit": limit}
    if repo:
        params["repo"] = ",".join(repo) if isinstance(repo, list) else repo
    return _get("/api/context/memory/search", params)


@mcp.tool()
def memory_resources_list(repo: str | list[str] = None) -> dict:
    """List memory bank resources from DB (optional repo filter)."""
    params = {}
    if repo:
        params["repo"] = ",".join(repo) if isinstance(repo, list) else repo
    return _get("/api/context/memory/list", params)


@mcp.tool()
def memory_resources_read(uri: str) -> dict:
    """Read a memory bank resource by URI."""
    return _get("/api/context/memory/read", {"uri": uri})


@mcp.tool()
def repos_list(filter: str = None, max_length: int = 4096) -> dict:
    """List indexed repos with README excerpts."""
    params = {}
    if filter:
        params["filter"] = filter
    return _get("/api/context/repos", params)


@mcp.tool()
def repo_status() -> dict:
    """List per-repo index status counts."""
    return _get("/api/context/repos/status")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logger.info(f"Context MCP starting on {_args.host}:{_args.port} (transport={_args.transport})")
    logger.info(f"Flask backend: {FLASK_URL}")
    mcp.run(transport=_args.transport)


if __name__ == "__main__":
    main()
