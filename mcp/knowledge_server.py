"""
savant-knowledge MCP Server

Business & stack knowledge graph — clients, domains, services, libraries,
technologies, insights, issues and more, connected by typed edges.
Thin MCP bridge to the Savant Dashboard Flask API (/api/knowledge/*).
Runs as SSE on port 8094.

Node taxonomy:
  client      — External firms & partners (Fidelity, UBS, Cetera…)
  domain      — Business capability areas (Auth/SSO, Holdings, Offerings…)
  service     — Deployable backend/frontend applications (icn, simonapp…)
  library     — Shared gems / packages (icn-user-acl, icn-entitlements…)
  technology  — Infrastructure & frameworks (Rails, Redis, Kubernetes…)
  insight     — Curated developer knowledge, decisions, lessons learned
  issue       — Known bugs, incidents, and problems
  project     — Repositories and codebases
  concept     — Abstract ideas and patterns
  repo        — Source code repositories
  session     — AI coding session entries

Edge types:
  relates_to, learned_from, applies_to, uses,
  evolved_from, contributed_to, part_of, integrates_with,
  depends_on, built_with
"""

import argparse
import logging
import os
from typing import Any, Optional

import requests
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = os.environ.get("SAVANT_API_BASE", "http://localhost:8090")
REQUEST_TIMEOUT = 60

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("savant-knowledge")

from auth import auth_headers, install_header_capture

# ---------------------------------------------------------------------------
# Entry point args
# ---------------------------------------------------------------------------

_parser = argparse.ArgumentParser(description="savant-knowledge MCP server")
_parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
_parser.add_argument("--port", type=int, default=8094)
_parser.add_argument("--host", default="127.0.0.1")
_args, _ = _parser.parse_known_args()

mcp = FastMCP(
    "savant-knowledge",
    instructions=(
        "BUSINESS & ARCHITECTURE METADATA GRAPH: Use this server to retrieve and store high-level developer insights, "
        "incident issues, system capability domains, partner clients, technology stack info, and session outcomes. "
        "DO NOT use this server to search raw repository source code files, AST definitions, concrete syntax trees (LST), "
        "or code call graphs (use 'savant-context' for those).\n\n"
        "CROSS-REFERENCE WITH CODE INTELLIGENCE MCP ('savant-context'):\n"
        "  - Pivoting from Knowledge to Code:\n"
        "      • When a knowledge node (domain, service, library, repo, project, issue, insight) points to a code entity or touched files:\n"
        "          1. Use `savant-context.research(q=..., repo=...)` for first-pass exploration of the implementation and call graph.\n"
        "          2. Use `savant-context.structure_search(q=..., repo=...)` to pinpoint exact AST class/function declarations.\n"
        "          3. Use `savant-context.get_lossless_tree(repo=..., path=..., start_line=..., end_line=...)` to inspect exact concrete syntax (LST) before editing.\n"
        "          4. Use `savant-context.analyze_code(repo=..., path=..., code=..., diff=...)` to assess code complexity, quality findings, and blast radius.\n"
        "  - Capturing Findings from Code into Knowledge:\n"
        "      • After analyzing or refactoring code with `savant-context` tools, capture durable architectural decisions, tricky bugs, "
        "or reusable patterns here using `store()` (with workspace_id, node_type='insight'|'issue', repo, and files) and publish with `commit_workspace()`.\n\n"
        "NODE TAXONOMY:\n"
        "  client (Fidelity, UBS…), domain (Auth/SSO, Holdings…), service (icn, simonapp…), library (icn-user-acl…), "
        "  technology (Rails, Redis…), insight (curated developer knowledge & decisions), issue (known bugs & problems), "
        "  project (repositories & codebases), concept (abstract patterns), repo (source code repositories), session (AI session entries).\n\n"
        "WORKFLOW:\n"
        "  1. `project_context(workspace_id)` or `search(query)` to discover existing domain context and constraints.\n"
        "  2. `neighbors(node_id)` to traverse relationships.\n"
        "  3. `store(...)` creates staged nodes (requires workspace_id, domain connection, repo/files).\n"
        "  4. `connect(...)` links nodes with typed edges (relates_to, applies_to, uses, depends_on, etc.).\n"
        "  5. `commit_workspace(workspace_id)` or `commit_nodes(...)` publishes staged nodes to the live graph."
    ),
    host=_args.host,
    port=_args.port,
)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request):
    """Liveness endpoint for the dedicated Streamable HTTP listener."""
    return JSONResponse({"status": "ok", "server": "savant-knowledge"})

install_header_capture(mcp)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api(method: str, path: str, **kwargs) -> dict | list:
    """Call the Flask API. Raises RuntimeError on failure."""
    url = f"{API_BASE}{path}"
    hdrs = kwargs.pop("headers", {})
    hdrs.update(auth_headers())
    try:
        resp = requests.request(method, url, timeout=REQUEST_TIMEOUT, headers=hdrs, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        raise RuntimeError(
            f"Savant dashboard not running at {API_BASE}. "
            "Start it with: npm run dev (or open Savant.app)"
        )
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        raise RuntimeError(f"API error {e.response.status_code}: {body}")

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search(query: str, node_type: str = "", limit: int = 20) -> dict[str, Any]:
    """Search knowledge nodes by text query. Returns the most relevant matches.

    WHEN TO USE:
      Use to discover high-level business capability domains, client partner requirements,
      services, libraries, technologies, developer insights, and known issues.

    TRANSITION TO SAVANT-CONTEXT:
      When search returns nodes referencing codebases, repositories, or source files:
      • Use `savant-context.research` to explore the actual source code and CodeGraph dependencies.
      • Use `savant-context.structure_search` to locate exact AST class or function declarations.
      • Use `savant-context.get_lossless_tree` for concrete syntax (LST) before editing.
      • Use `savant-context.analyze_code` to evaluate implementation complexity and impact.

    node_type filter (optional): client | domain | service | library | technology | insight | issue | project | concept | repo | session
    limit: max results (default 20, max 100)
    """
    return _api("POST", "/api/knowledge/search", json={
        "query": query,
        "node_type": node_type,
        "limit": min(max(1, limit), 100),
    })


@mcp.tool()
def recent(node_type: str = "", limit: int = 20) -> dict[str, Any]:
    """Get the most recently created/updated knowledge nodes.

    WHEN TO USE:
      Check recent project decisions, newly logged issues, or recently touched domain nodes.
      Follow up with `savant-context` tools when investigating associated source files.

    node_type filter (optional): client | domain | service | library | technology | insight | issue | project | concept | repo | session
    limit: max results (default 20, max 100)
    """
    params: dict[str, Any] = {"limit": min(max(1, limit), 100)}
    if node_type:
        params["node_type"] = node_type
    return _api("GET", "/api/knowledge/recent", params=params)


@mcp.tool()
def project_context(workspace_id: str) -> dict[str, Any]:
    """Get aggregated knowledge context for a workspace.

    Traverses the graph from the workspace project node (depth 2) to return
    connected insights, services, domains, tasks, and notes in one call.
    Use this to onboard an agent to a workspace at the start of a session.

    SAVANT-CONTEXT INTEGRATION:
      After discovering connected services, repositories, and touched files here:
      • Call `savant-context.research(q=..., repo=...)` for source code and CodeGraph exploration.
      • Call `savant-context.structure_search` to pinpoint AST symbols.
      • Call `savant-context.get_lossless_tree` to view exact LST line ranges before making changes.
    """
    return _api("GET", "/api/knowledge/project_context", params={"workspace_id": workspace_id})


@mcp.tool()
def store(
    content: str,
    workspace_id: str,
    node_type: str = "insight",
    graph_type: str = "",
    title: str = "",
    source: str = "note",
    repo: str = "",
    files: str = "",
    connections: str = "",
) -> dict[str, Any]:
    """Store a knowledge node into the graph. The node is created as **staged**
    and must be committed (via commit_workspace) before it appears in the
    default graph view. This enables a review workflow:
    create (staged) -> review -> commit.

    SAVANT-CONTEXT INTEGRATION:
      When recording code changes, refactors, or bug resolutions:
      • Populate `repo` and `files` with the exact paths analyzed via `savant-context.analyze_code` or `savant-context.structure_search`.
      • Store architectural rationale or lessons learned (node_type='insight') so future agents know why code was designed this way.
      • Store tricky bugs and edge-case behaviors (node_type='issue') so future agents avoid repeating defects.

    workspace_id: **Required.** The workspace this node belongs to. Every node
                  must be associated with a workspace for traceability.
    content:      The knowledge to store (decisions, lessons, architecture notes,
                  technical documentation, etc.)
    node_type:    The type of node to create. Determines how the node is
                  categorized and displayed.
                  Valid values: insight | client | domain | service | library |
                  technology | project | concept | repo | session | issue |
                  person | operation | organization
                  Default: 'insight'
    graph_type:   Optional classification for which knowledge graph/namespace
                  this node belongs to. Use this to organize nodes into logical
                  groups beyond node_type. Examples: 'business' (client/partner
                  knowledge), 'technical' (architecture, stack decisions),
                  'operational' (incidents, runbooks), 'onboarding' (new dev
                  guides), or any custom string your team defines.
                  Stored in metadata.graph_type.
    title:        Short label for the node (shown on graph). If omitted, the
                  first line of content (up to 120 chars) is used.
    source:       How this knowledge was captured: 'session' | 'task' | 'note'
                  (default: 'note')
    repo:         Source repo name e.g. 'icn' (optional)
    files:        Comma-separated file paths relevant to this node (optional)
    connections:  JSON array of {node_id, edge_type} to link this node to.
                  e.g. '[{"node_id":"kgn_abc","edge_type":"applies_to"}]'
                  edge_types: relates_to | learned_from | applies_to | uses |
                              evolved_from | contributed_to | part_of |
                              integrates_with | depends_on | built_with
    """
    if not workspace_id or not workspace_id.strip():
        raise RuntimeError(
            "workspace_id is required. Every knowledge node must belong to a "
            "workspace. Use list_workspaces (savant-workspace) to find the "
            "current workspace, or create_workspace to start a new one."
        )
    import json as _json
    files_list = [f.strip() for f in files.split(",") if f.strip()] if files else []
    try:
        connections_list = _json.loads(connections) if connections else []
        if not isinstance(connections_list, list):
            connections_list = []
    except Exception:
        connections_list = []

    return _api("POST", "/api/knowledge/store", json={
        "content": content,
        "node_type": node_type,
        "graph_type": graph_type,
        "title": title,
        "source": source,
        "workspace_id": workspace_id,
        "repo": repo,
        "files": files_list,
        "connections": connections_list,
    })


@mcp.tool()
def update_node(
    node_id: str,
    title: str = "",
    content: str = "",
    node_type: str = "",
    graph_type: str = "",
) -> dict[str, Any]:
    """Update an existing knowledge graph node's properties. Does NOT require a
    workspace — you can edit any node you have the node_id for.

    SAVANT-CONTEXT INTEGRATION:
      Update node content or titles when deeper code exploration via `savant-context`
      reveals new details about system architecture, performance limits, or implementation nuances.

    node_id:    The ID of the node to update (e.g. 'kgn_1234567890_1').
                Use search() or recent() to find node IDs.
    title:      New short label for the node. Leave empty to keep current title.
    content:    New long-form content (markdown). Leave empty to keep current.
    node_type:  Change the node's type classification.
                Valid values: insight | client | domain | service | library |
                technology | project | concept | repo | session | issue
                Leave empty to keep current type.
    graph_type: Change which knowledge graph/namespace this node belongs to.
                Examples: 'business', 'technical', 'operational', 'onboarding',
                or any custom string. Stored in metadata.graph_type.
                Leave empty to keep current graph_type.
    """
    payload: dict[str, Any] = {}
    if title and title.strip():
        payload["title"] = title.strip()
    if content and content.strip():
        payload["content"] = content.strip()
    if node_type and node_type.strip():
        payload["node_type"] = node_type.strip()
    if graph_type and graph_type.strip():
        payload["graph_type"] = graph_type.strip()
    if not payload:
        raise RuntimeError("At least one field (title, content, node_type, or graph_type) must be provided to update.")
    return _api("PUT", f"/api/knowledge/nodes/{node_id}", json=payload)


@mcp.tool()
def connect(
    source_id: str,
    target_id: str,
    edge_type: str = "relates_to",
    label: str = "",
) -> dict[str, Any]:
    """Create a typed edge between two knowledge graph nodes.

    source_id / target_id: node_id values from search() or recent()
    edge_type: relates_to | learned_from | applies_to | uses | evolved_from |
               contributed_to | part_of | integrates_with | depends_on | built_with
    label:     optional human-readable annotation on the edge
    """
    return _api("POST", "/api/knowledge/edges", json={
        "source_id": source_id,
        "target_id": target_id,
        "edge_type": edge_type,
        "label": label,
    })


@mcp.tool()
def disconnect(source_id: str, target_id: str, edge_type: str = "") -> dict[str, Any]:
    """Remove an edge between two nodes.
    If edge_type is given, removes only that edge type; otherwise removes all edges between them.
    """
    return _api("POST", "/api/knowledge/edges/disconnect", json={
        "source_id": source_id,
        "target_id": target_id,
        "edge_type": edge_type,
    })


@mcp.tool()
def neighbors(node_id: str, depth: int = 1, edge_type: str = "") -> dict[str, Any]:
    """Traverse the graph outward from a node and return connected nodes + edges.

    WHEN TO USE:
      Explore connections around a domain, service, technology, or issue.

    TRANSITION TO SAVANT-CONTEXT:
      When traversing leads to connected repos, services, or touched files, switch to:
      • `savant-context.research` for code-level exploration and CodeGraph relationships.
      • `savant-context.structure_search` for AST symbol definitions.
      • `savant-context.get_lossless_tree` for concrete syntax (LST) inspection.

    node_id:   Starting node (use search() or list_concepts() to find IDs)
    depth:     Hops to traverse — 1 (immediate) to 5 (wide neighbourhood). Default 1.
    edge_type: Optional — filter traversal to a specific edge type only.
    """
    params: dict[str, Any] = {"depth": min(max(1, depth), 5)}
    if edge_type:
        params["edge_type"] = edge_type
    return _api("GET", f"/api/knowledge/neighbors/{node_id}", params=params)


@mcp.tool()
def list_concepts(node_type: str = "concept") -> list[dict[str, Any]]:
    """List knowledge graph nodes by type.
    Returns node_id, title, and metadata for each entry.

    node_type: concept | domain | service | client | library | technology | insight | issue | project | repo | session | person | organization | operation
    Defaults to 'concept' for backwards compatibility. Pass node_type='domain' to list all domain nodes.
    """
    return _api("GET", "/api/knowledge/concepts", params={"node_type": node_type})


@mcp.tool()
def list_domains() -> list[dict[str, Any]]:
    """List all domain nodes in the knowledge graph (Auth/SSO, Holdings, Offerings, etc.).
    Returns node_id, title, and metadata for each domain entry.
    """
    return _api("GET", "/api/knowledge/concepts", params={"node_type": "domain"})


@mcp.tool()
def link_workspace(node_id: str, workspace_id: str) -> dict[str, Any]:
    """Link a knowledge graph node to a workspace. The node will appear when filtering by that workspace."""
    return _api("POST", "/api/knowledge/link-workspace", json={"node_id": node_id, "workspace_id": workspace_id})


@mcp.tool()
def unlink_workspace(node_id: str, workspace_id: str) -> dict[str, Any]:
    """Remove a workspace association from a knowledge graph node. The node remains but won't appear in that workspace's filtered view."""
    return _api("POST", "/api/knowledge/unlink-workspace", json={"node_id": node_id, "workspace_id": workspace_id})


@mcp.tool()
def purge_workspace(workspace_id: str) -> dict[str, Any]:
    """
    Purge all knowledge graph nodes belonging to a workspace.
    Nodes exclusive to this workspace are permanently deleted (along with their edges).
    Nodes shared with other workspaces are only unlinked (workspace association removed).
    Returns counts of deleted and unlinked nodes.
    """
    return _api("POST", "/api/knowledge/purge-workspace", json={"workspace_id": workspace_id})


@mcp.tool()
def commit_nodes(node_ids: str) -> dict[str, Any]:
    """
    Commit staged knowledge graph nodes to the main graph.
    Committed nodes are visible in the default graph view.
    node_ids: JSON array of node IDs to commit, e.g. '["kgn_123", "kgn_456"]'
    """
    import json as _json
    ids = _json.loads(node_ids) if isinstance(node_ids, str) else node_ids
    return _api("POST", "/api/knowledge/nodes/commit", json={"node_ids": ids})


@mcp.tool()
def commit_workspace(workspace_id: str) -> dict[str, Any]:
    """
    Commit all staged knowledge graph nodes in a workspace to the main graph.

    Nodes created via store() start as 'staged' (invisible in the default graph
    view). Call this tool to publish them — all staged nodes belonging to the
    workspace become 'committed' and visible. This is the final step in the
    staged creation workflow: store() -> review -> commit_workspace().

    workspace_id: The workspace whose staged nodes should be committed.
    Returns: { committed: true, count: int, workspace_id: str, node_ids: [...] }
    """
    return _api("POST", "/api/knowledge/nodes/commit", json={"workspace_id": workspace_id})


@mcp.tool()
def prune(remove_orphan_nodes: bool = False) -> dict[str, Any]:
    """
    Remove dangling edges and optionally orphaned nodes from the knowledge graph.

    Dangling edges reference nodes that have been deleted. This cleans them up.
    If remove_orphan_nodes=True, also removes nodes with zero connections.
    Returns: { "edges_removed": int, "nodes_removed": int }
    """
    return _api("POST", "/api/knowledge/prune", json={"remove_orphan_nodes": remove_orphan_nodes})


# ---------------------------------------------------------------------------
# SSE ClosedResourceError patch — prevents dropped clients from crashing
# ---------------------------------------------------------------------------
def _patch_sse_transport():
    try:
        from mcp.server.sse import SseServerTransport
        original_handle = SseServerTransport.handle_post_message

        async def _safe_handle(self, scope, receive, send):
            try:
                await original_handle(self, scope, receive, send)
            except Exception as e:
                if "ClosedResource" in type(e).__name__ or "ClosedResource" in str(e):
                    log.debug("SSE client disconnected (harmless)")
                else:
                    raise

        SseServerTransport.handle_post_message = _safe_handle
    except Exception as e:
        log.debug(f"SSE patch skipped: {e}")

_patch_sse_transport()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if _args.transport == "sse":
        log.info(f"Starting savant-knowledge MCP (SSE) on {_args.host}:{_args.port}")
    mcp.run(transport=_args.transport)
