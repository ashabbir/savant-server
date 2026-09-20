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
        "PHYSICAL CODEBASE INTELLIGENCE & CONTEXT: Use this server to query actual source code, "
        "concrete syntax trees (LST), abstract syntax trees (AST), code-level dependency graphs (CodeGraph), "
        "and static code analysis. DO NOT use this server for high-level business capability domains, "
        "client partner metadata, cross-system service catalogs, or architectural insights (use 'savant-knowledge' for those).\n\n"
        "WHAT TO USE WHEN BEST — DECISION MATRIX FOR AI AGENTS:\n"
        "  1. `research(q, repo, type='all'|'code'|'memory', limit)` — PRIMARY FIRST-PASS EXPLORATION:\n"
        "     • WHEN: At the start of a task, when exploring unfamiliar features, concepts, or cross-cutting implementations.\n"
        "     • WHY: Combines semantic code search, AST structure match, CodeGraph caller/callee graphs, and memory bank docs in one call.\n"
        "     • OPTIONS: Use type='all' (default) for broad discovery; type='code' (auto-enables CodeGraph) when seeking implementations & call chains; type='memory' for architecture docs only.\n\n"
        "  2. `structure_search(q, repo)` — AST CODE-GRAPH STRUCTURE PINPOINTING:\n"
        "     • WHEN: You know a symbol name, class, function, or method name (e.g. 'SessionManager', 'authenticate_user') and need its exact declaration location.\n"
        "     • WHY: Uses AST index rather than semantic embeddings. Zero semantic noise; directly pinpoints definitions, class hierarchies, and file coordinates.\n\n"
        "  3. `get_lossless_tree(repo, path, start_line, end_line, max_nodes=200)` — LST CONCRETE SYNTAX BEFORE EDITING:\n"
        "     • WHEN: Immediately before reading or editing targeted code lines where exact delimiters, comments, formatting, indentation, and concrete syntax coordinates matter.\n"
        "     • WHY: Lossless Syntax Tree (LST) preserves full fidelity (unlike standard ASTs which strip comments and whitespace). Token-bounded; always specify narrow start_line/end_line ranges on large files.\n\n"
        "  4. `search_lossless_tree(q, repo, limit=10)` — LST EXACT MULTI-REPO CODE MATCHING:\n"
        "     • WHEN: Searching across repositories for exact code patterns, variable usages, or literal syntax constructs with concrete node context.\n"
        "     • WHY: Bounded exact syntax matching with token-efficient AST node boundaries.\n\n"
        "  5. `analyze_code(repo, path, code, diff, symbol, node_type)` — DEEP STATIC CODE ANALYSIS & IMPACT:\n"
        "     • WHEN: Before or after modifying code, during refactoring, or when reviewing submitted code/diffs.\n"
        "     • WHY: Evaluates cyclomatic/cognitive complexity, code quality findings/lints, duplication, maintainability, and CodeGraph blast radius (upstream callers / downstream dependencies).\n"
        "     • USES: Standalone review (code='...'), proposed change validation (repo + path + code='...'), patch validation (repo + path + diff='...'), or post-edit check (repo + path).\n\n"
        "CODEGRAPH & IMPACT ANALYSIS:\n"
        "  - CodeGraph maps callers, callees, imports, and dependencies across files.\n"
        "  - Access via research(include_graph=True) or the impact_surface section returned by research and analyze_code.\n"
        "  - Use it to evaluate blast radius: upstream callers (who breaks if I change this?) and downstream dependencies (what does this rely on?).\n\n"
        "CROSS-REFERENCE WITH KNOWLEDGE GRAPH MCP ('savant-knowledge'):\n"
        "  - Before touching code: Check savant-knowledge.project_context(workspace_id) or savant-knowledge.search(query) for business domain constraints, client quirks, known issues, and architectural decisions.\n"
        "  - After code analysis or refactoring: When new architectural patterns, system gotchas, or reusable lessons are discovered, record them in savant-knowledge.store(content, workspace_id, node_type='insight'|'issue', repo, files) and publish via savant-knowledge.commit_workspace."
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

    WHEN TO USE:
      Use when the symbol name or shape is known (e.g., 'SessionManager', 'authenticate_user')
      and you need exact declaration locations without semantic vector fuzziness. AST search
      directly pinpoints definitions, class hierarchies, and file coordinates.

    WORKFLOW:
      1. Use `structure_search` to locate the exact declaration file and line range.
      2. Call `get_lossless_tree` on that specific file and narrow line range to inspect concrete syntax before editing.

    KNOWLEDGE GRAPH:
      If searching for high-level business capability domains, client partner requirements, or
      architectural concepts rather than AST symbols, use `savant-knowledge.search` instead.
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
def get_lossless_tree(
    repo: str | list[str],
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_nodes: int = 200,
) -> dict:
    """Get exact source, concrete syntax (LST), AST declarations, and CodeGraph coordinates.

    WHEN TO USE:
      Immediately before reading or modifying code where exact indentation, whitespace,
      delimiters, comments, and line ranges matter.

    LST VS AST:
      Standard ASTs discard formatting, comments, and whitespace. The Lossless Syntax Tree (LST)
      preserves 100% concrete syntax fidelity with exact byte/line ranges for safe surgical edits.

    TOKEN EFFICIENCY:
      Results are bounded. Always specify narrow `start_line` and `end_line` ranges for large files
      to keep token cost low.

    CODEGRAPH:
      Returns surrounding CodeGraph coordinates for call and dependency context.
    """
    params = {"repo": ",".join(repo) if isinstance(repo, list) else repo, "path": path,
              "max_nodes": max_nodes}
    if start_line is not None:
        params["start_line"] = start_line
    if end_line is not None:
        params["end_line"] = end_line
    return _get("/api/context/lossless-tree", params)


@mcp.tool()
def search_lossless_tree(
    q: str,
    repo: str | list[str] = None,
    limit: int = 10,
) -> dict:
    """Find exact source matches across repositories with concrete syntax context (LST).

    WHEN TO USE:
      Multi-repository exact syntax pattern matching, variable usage, or literal code snippet
      search with bounded concrete syntax node boundaries.

    WORKFLOW:
      Follow up with `get_lossless_tree` on matching files for detailed range inspection,
      or `analyze_code` to evaluate potential changes.
    """
    params = {"q": q, "limit": limit}
    if repo:
        params["repo"] = ",".join(repo) if isinstance(repo, list) else repo
    return _get("/api/context/lossless-tree/search", params)


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
    """Analyze a file, class, symbol, submitted source, or diff for implementation impact and code quality.

    WHEN TO USE:
      • Standalone review: pass `code='...'` to evaluate complexity, findings, and refactor targets on a snippet or file without repo lookup.
      • Proposed change review: pass `repo` + `path` + `code='...'` to compare proposed source against the indexed file before editing on disk.
      • Patch review: pass `repo` + `path` + `diff='...'` to assess a unified diff.
      • Post-edit verification: pass `repo` + `path` to verify complexity and findings after editing.

    METRICS & FINDINGS:
      Reports cyclomatic/cognitive complexity, code quality findings/lints, duplication, maintainability,
      and before/after deltas. Never executes submitted code or mutates repository files.

    CODEGRAPH IMPACT ANALYSIS:
      Assesses upstream callers and downstream dependencies to map blast radius.

    KNOWLEDGE GRAPH INTEGRATION:
      When this analysis uncovers durable architectural patterns, tricky edge cases, or bug root causes,
      record them in `savant-knowledge.store(content, workspace_id, node_type='insight'|'issue')`.
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
    limit: int = 10,
    exclude_tests: bool = True,
    include_graph: bool | None = None,
) -> dict:
    """PRIMARY CODE & CONTEXT SEARCH TOOL FOR AI AGENTS.

    AI AGENT INSTRUCTIONS:
      Use this tool as your single first-pass entry point for exploring the codebase, code dependencies,
      and project memory banks. It unifies semantic code search, AST structure match, CodeGraph dependencies,
      and memory bank markdown search in one call.

    WHEN TO USE RESEARCH VS OTHER TOOLS:
      • `research`: Start here for any feature exploration, unfamiliar domain, or broad discovery.
      • `structure_search`: Switch to this when you know the exact symbol name and only need AST definitions.
      • `get_lossless_tree`: Switch to this on a specific file and line range to see exact concrete syntax (LST) before editing.
      • `analyze_code`: Run this before/after making edits to verify complexity, lint findings, and CodeGraph blast radius.

    KNOWLEDGE GRAPH INTEGRATION:
      • If seeking high-level business capability domains, client partner requirements, service catalogs,
        or architectural decisions, query `savant-knowledge.project_context` or `savant-knowledge.search` first.
      • After modifying code, record any durable architectural insights or bug discoveries back to
        `savant-knowledge.store(content, workspace_id, node_type='insight'|'issue')`.

    PARAM GUIDANCE FOR AGENTS:
      • q (str, required): The search query concept, symbol name, or topic (e.g. "auth middleware", "SessionDB", "user routes").
      • repo (str | list[str], optional): Limit search scope to specific repository/repositories.
      • type (str, optional): Controls search scope. Must be one of:
          - "all" (default): Comprehensive search across code, AST structure, code graph, and memory bank documentation.
          - "code": Search source code files, AST definitions, and dependency graph (auto-enables CodeGraph; omits memory bank docs).
          - "memory": Search architectural docs and memory bank markdown files only (omits source code).
      • limit (int, optional, default=10, maximum=10): Max result count per section. Keep this low for multi-repo work.
      • exclude_tests (bool, optional, default=True): Prioritizes core production source code over test files.
      • include_graph (bool, optional): Expand CodeGraph results. Defaults to false for 'all' and true for 'code'.

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
        "limit": max(1, min(int(limit), 10)),
        "exclude_tests": exclude_tests,
        "include_graph": (type == "code") if include_graph is None else include_graph,
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
