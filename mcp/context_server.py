"""Context MCP server — FastMCP SSE bridge on port 8093.

Proxies 11 tools to the Flask /api/context/* and /api/graphify/* REST APIs.
Follows the same pattern as workspace (8091) and abilities (8092) servers.

Tools:
  code_search           — Semantic search across indexed repo code
  structure_search      — AST structure search for classes, functions
  analyze_code          — Analyze a class/file before and after changes
  memory_bank_search    — Semantic search within memory bank markdown files
  memory_resources_list — List all memory bank resources (optional repo filter)
  memory_resources_read — Read a specific memory bank resource by URI
  repos_list            — List indexed repos with README excerpts
  repo_status           — Per-repo index status counts
  code_graph_search     — Search Graphify relationships and dependencies
  get_code_graph_stats  — Graphify node/edge counts by type
  research              — Preferred AI-facing tool for broad code exploration
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
        "PHYSICAL CODEBASE CONTEXT SEARCH: Use this server to query actual source code, syntax, class structures, "
        "and code-level dependency graphs. DO NOT use this server for high-level business capability domains, "
        "partner clients, deployable service applications, or developer architecture decisions (use 'savant-knowledge' for those).\n"
        "Tools:\n"
        "  - code_search(query, repo): Semantic search across source code.\n"
        "  - structure_search(query): AST structural match (find classes, functions).\n"
        "  - analyze_code(repo, path, uri, name, class_name, symbol, node_type, diff, code): Analyze a class/file before and after changes.\n"
        "  - code_graph_search(query, repo): Look up codebase graph imports, callers, and class dependencies.\n"
        "  - get_code_graph_stats(repo): Get Graphify node and edge counts by type.\n"
        "  - research(query, repo): Preferred AI-facing tool that runs code, structure, memory, and code graph searches together.\n"
        "  - memory_bank_search(query, repo): Semantic search within local repository memory bank markdown files.\n"
        "  - memory_resources_list(repo): List memory bank resources.\n"
        "  - memory_resources_read(uri): Read a specific memory bank resource by URI.\n"
        "  - repos_list(filter): List indexed repos with README excerpts.\n"
        "  - repo_status(): List per-repo index status counts.\n"
        "All tools accept a 'repo' filter to scope lookup to a specific repository."
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
# MCP Tools (same signatures as standalone savant-context)
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


@mcp.tool()
def code_graph_search(
    query: str,
    repo: str = None,
    limit: int = 20,
) -> dict:
    """Search codebase relationships, class hierarchies, and dependencies in the Graphify graph.

    query: The text term to search for in titles or descriptions
    repo: Optional repository/workspace name to search within (recommended).
    limit: Maximum number of results to return (default: 20)
    """
    payload = {"query": query, "limit": limit}
    if repo:
        payload["workspace_id"] = repo
    return _post("/api/graphify/search", payload)


@mcp.tool()
def get_code_graph_stats(
    repo: str,
) -> dict:
    """Get counts of Graphify nodes and edges grouped by type for a repository.

    repo: The name of the repository to get stats for
    """
    return _get("/api/graphify/stats", {"workspace_id": repo})


@mcp.tool()
def research(
    query: str,
    repo: str = None,
    limit: int = 10,
) -> dict:
    """Perform a comprehensive code research task by searching source code, structure, memory banks, and codebase graphs.

    HINT: If you don't know where to start or are unfamiliar with the codebase, start here first.
    This tool fans out across all search types and gives you the broadest orientation in one call.

    query: The search term or concept to research.
    repo: Optional repository name to limit research within.
    limit: Maximum results per search type (default: 10).
    """
    results = {}

    # 1. Semantic source code search
    try:
        results["code_search"] = code_search(query=query, repo=repo, limit=limit)
    except Exception as e:
        results["code_search"] = {"error": str(e)}

    # 2. Memory bank markdown search
    try:
        results["memory_bank_search"] = memory_bank_search(query=query, repo=repo, limit=limit)
    except Exception as e:
        results["memory_bank_search"] = {"error": str(e)}

    # 3. Structure / AST search
    struct_results = {}
    try:
        struct_results = structure_search(query=query, repo=repo)
        results["structure_search"] = struct_results
    except Exception as e:
        results["structure_search"] = {"error": str(e)}

    # 4. Code graph / Graphify search (using matched symbol names or original query as fallback)
    graph_queries = set()
    if struct_results and "results" in struct_results and isinstance(struct_results["results"], list):
        for item in struct_results["results"]:
            if isinstance(item, dict) and item.get("name"):
                graph_queries.add(item["name"])

    # Fallback to the original query if no structure names matched
    if not graph_queries:
        graph_queries.add(query)

    graph_results = {}
    for g_query in sorted(graph_queries):
        try:
            graph_results[g_query] = code_graph_search(query=g_query, repo=repo, limit=limit)
        except Exception as e:
            graph_results[g_query] = {"error": str(e)}

    results["code_graph_search"] = graph_results

    return results


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
