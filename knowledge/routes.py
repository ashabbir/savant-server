"""Flask Blueprint for Knowledge Graph REST API. All routes under /api/knowledge/*."""
import json
import time
import threading
import re
from flask import Blueprint, jsonify, request, g
from utils.auth import admin_required, require_savant_app, ALLOWED_SAVANT_APPS, check_domain_write_access
from db.experiences import ExperienceDB
from db.knowledge_graph import KnowledgeGraphDB
from db.tasks import TaskDB
from db.notes import NoteDB
from db.workspaces import WorkspaceDB

knowledge_bp = Blueprint("knowledge", __name__)

_id_counter = 0
_id_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_NODE_TYPES = {"insight", "client", "domain", "service", "library", "technology",
                    "project", "concept", "repo", "session", "issue", "person",
                    "operation", "organization"}
VALID_EDGE_TYPES = {"relates_to", "learned_from", "applies_to", "uses",
                    "evolved_from", "contributed_to", "part_of", "integrates_with",
                    "depends_on", "built_with"}
MAX_CONTENT_LEN = 20_000
MAX_TITLE_LEN   = 500
MAX_LABEL_LEN   = 200
ID_PATTERN      = re.compile(r'^[\w\-]+$')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gen_id(prefix="exp"):
    global _id_counter
    with _id_lock:
        _id_counter += 1
        return f"{prefix}_{int(time.time() * 1000)}_{_id_counter}"


def _safe_int(val, default: int, min_val: int = 1, max_val: int = 1000) -> int:
    """Parse int from query/body value, clamping to [min_val, max_val]."""
    try:
        return max(min_val, min(int(val), max_val))
    except (TypeError, ValueError):
        return default


def _text_value(value, max_length: int) -> str:
    return "" if value is None else str(value).strip()[:max_length]


def _metadata_value(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _safe_id(val: str) -> str:
    """Sanitize a node/edge ID — alphanumeric, dash, underscore only."""
    val = (val or "").strip()
    if not val or not ID_PATTERN.match(val):
        return ""
    return val[:128]


def _validate_edge_type(edge_type: str) -> str:
    """Return edge_type if valid, else 'relates_to'."""
    return edge_type if edge_type in VALID_EDGE_TYPES else "relates_to"


# ---------------------------------------------------------------------------
# Global Header Guard
# ---------------------------------------------------------------------------

@knowledge_bp.before_request
def check_savant_app_header():
    # Allow health check endpoint without header
    if request.path == "/api/knowledge/health":
        return None
    app_name = (request.headers.get("X-App-Name") or request.headers.get("X-Savant-App") or "").strip().lower()
    if not app_name or app_name not in ALLOWED_SAVANT_APPS:
        return jsonify({
            "error": "Access denied."
        }), 403


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@knowledge_bp.route("/api/knowledge/health", methods=["GET"])
def health():
    node_count = KnowledgeGraphDB.count_nodes()
    return jsonify({"status": "ok", "nodes": node_count})


# ---------------------------------------------------------------------------
# Graph: Nodes
# ---------------------------------------------------------------------------

@knowledge_bp.route("/api/knowledge/nodes", methods=["POST"])
@require_savant_app
def create_node():
    """Create a knowledge graph node.

    Optional graph_type in body is stored in metadata.graph_type.
    """
    data = request.get_json(force=True, silent=True) or {}
    node_type = data.get("node_type", "insight")
    if node_type not in VALID_NODE_TYPES:
        return jsonify({"error": f"invalid node_type '{node_type}'"}), 400

    # Domain Access Check
    user_id = getattr(g, "user_id", "")
    is_domain = (node_type == "domain")
    ok, err = check_domain_write_access(user_id, is_domain_creation=is_domain)
    if not ok:
        return jsonify({"error": err}), 403

    title = (data.get("title") or "").strip()[:MAX_TITLE_LEN]
    if not title:
        return jsonify({"error": "title is required"}), 400
    if "content" in data and len(data["content"]) > MAX_CONTENT_LEN:
        data["content"] = data["content"][:MAX_CONTENT_LEN]
    # Inject graph_type into metadata if provided at top level
    graph_type = (data.pop("graph_type", None) or "").strip()
    if graph_type:
        meta = data.get("metadata", {}) or {}
        if isinstance(meta, str):
            import json as _json
            meta = _json.loads(meta)
        meta["graph_type"] = graph_type
        data["metadata"] = meta
    try:
        node = KnowledgeGraphDB.create_node(data)
        return jsonify(node)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "internal error", "detail": str(e)}), 500


@knowledge_bp.route("/api/knowledge/nodes/<node_id>", methods=["GET"])
def get_node(node_id):
    """Get a node with its edges."""
    if not _safe_id(node_id):
        return jsonify({"error": "not found"}), 404
    node = KnowledgeGraphDB.get_node(node_id)
    if not node:
        return jsonify({"error": "not found"}), 404
    return jsonify(node)


@knowledge_bp.route("/api/knowledge/nodes/<node_id>", methods=["PUT"])
@require_savant_app
def update_node(node_id):
    """Update a node. Accepts graph_type at top level — stored in metadata.graph_type."""
    if not _safe_id(node_id):
        return jsonify({"error": "not found"}), 404

    # Domain Access Check
    user_id = getattr(g, "user_id", "")
    ok, err = check_domain_write_access(user_id, node_id=node_id)
    if not ok:
        return jsonify({"error": err}), 403

    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    updates, validation_error = _normalized_node_updates(data)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    node = KnowledgeGraphDB.update_node(node_id, updates)
    if not node:
        return jsonify({"error": "not found"}), 404
    return jsonify(node)


def _normalized_node_updates(data: dict) -> tuple[dict, str | None]:
    updates = dict(data)
    if "title" in updates:
        updates["title"] = _text_value(updates["title"], MAX_TITLE_LEN)
        if not updates["title"]:
            return updates, "title cannot be empty"
    if "node_type" in updates:
        updates["node_type"] = _text_value(updates["node_type"], 50)
        if updates["node_type"] not in VALID_NODE_TYPES:
            return updates, f"invalid node_type '{updates['node_type']}'"
    if "content" in updates and updates["content"] is not None:
        updates["content"] = _text_value(updates["content"], MAX_CONTENT_LEN)
    graph_type = _text_value(updates.pop("graph_type", None), 100)
    if graph_type:
        metadata = _metadata_value(updates.get("metadata"))
        metadata["graph_type"] = graph_type
        updates["metadata"] = metadata
    return updates, None


@knowledge_bp.route("/api/knowledge/nodes/<node_id>", methods=["DELETE"])
@require_savant_app
def delete_node(node_id):
    """Delete a node and cascade-delete its edges."""
    if not _safe_id(node_id):
        return jsonify({"error": "not found"}), 404

    # Domain Access Check
    user_id = getattr(g, "user_id", "")
    ok, err = check_domain_write_access(user_id, node_id=node_id)
    if not ok:
        return jsonify({"error": err}), 403

    deleted = KnowledgeGraphDB.delete_node(node_id)
    if deleted:
        return jsonify({"deleted": True, "node_id": node_id})
    return jsonify({"error": "not found"}), 404


@knowledge_bp.route("/api/knowledge/prune", methods=["POST"])
@admin_required
@require_savant_app
def prune_graph():
    """Remove dangling edges (and optionally orphaned nodes) from the knowledge graph.

    Body: { "remove_orphan_nodes": bool (default false) }
    Returns: { "edges_removed": int, "nodes_removed": int }
    """
    data = request.get_json(force=True, silent=True) or {}
    remove_orphan_nodes = bool(data.get("remove_orphan_nodes", False))
    try:
        result = KnowledgeGraphDB.prune_graph(remove_orphan_nodes=remove_orphan_nodes)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": "internal error", "detail": str(e)}), 500


@knowledge_bp.route("/api/knowledge/nodes/commit", methods=["POST"])
@admin_required
@require_savant_app
def commit_nodes():
    """Commit staged knowledge graph nodes to the main graph.

    Accepts either:
      - {workspace_id: "..."} to commit all staged nodes in a workspace
      - {node_ids: [...]} to commit specific nodes (backward compat)
    """
    data = request.get_json(force=True)
    workspace_id = data.get("workspace_id", "").strip()
    node_ids = data.get("node_ids", [])

    if workspace_id:
        # Find all staged nodes for this workspace
        all_nodes = KnowledgeGraphDB.list_nodes(include_staged=True, limit=10000)
        staged_ids = [
            n["node_id"] for n in all_nodes
            if n.get("status") == "staged"
            and workspace_id in ((n.get("metadata") or {}).get("workspaces", []))
        ]
        if not staged_ids:
            return jsonify({"committed": True, "count": 0, "workspace_id": workspace_id, "node_ids": []})
        count = KnowledgeGraphDB.commit_nodes(staged_ids)
        return jsonify({"committed": True, "count": count, "workspace_id": workspace_id, "node_ids": staged_ids})
    elif node_ids and isinstance(node_ids, list):
        count = KnowledgeGraphDB.commit_nodes(node_ids)
        return jsonify({"committed": True, "count": count, "node_ids": node_ids})
    else:
        return jsonify({"error": "workspace_id or node_ids required"}), 400


@knowledge_bp.route("/api/knowledge/nodes/uncommit", methods=["POST"])
@admin_required
@require_savant_app
def uncommit_nodes():
    """Move committed knowledge graph nodes back to staged so they leave the main graph.

    Body:
      - {node_ids: [...]} to uncommit specific nodes
      - {workspace_id: "..."} to uncommit all committed nodes in a workspace
    """
    data = request.get_json(force=True, silent=True) or {}
    workspace_id = (data.get("workspace_id") or "").strip()
    node_ids = data.get("node_ids", [])

    if workspace_id:
        all_nodes = KnowledgeGraphDB.list_nodes(include_staged=True, limit=10000)
        committed_ids = [
            n["node_id"] for n in all_nodes
            if n.get("status") == "committed"
            and workspace_id in ((n.get("metadata") or {}).get("workspaces", []))
        ]
        if not committed_ids:
            return jsonify({"uncommitted": True, "count": 0, "workspace_id": workspace_id, "node_ids": []})
        count = KnowledgeGraphDB.uncommit_nodes(committed_ids)
        return jsonify({"uncommitted": True, "count": count, "workspace_id": workspace_id, "node_ids": committed_ids})
    elif node_ids and isinstance(node_ids, list):
        count = KnowledgeGraphDB.uncommit_nodes(node_ids)
        return jsonify({"uncommitted": True, "count": count, "node_ids": node_ids})
    else:
        return jsonify({"error": "workspace_id or node_ids required"}), 400


@knowledge_bp.route("/api/knowledge/nodes/merge", methods=["POST"])
@admin_required
@require_savant_app
def merge_nodes():
    """Merge multiple nodes into one.

    Body: { node_ids: [id1, id2, ...], node_type: "optional" }
    First node_id is the survivor — keeps its title.
    All connections are merged onto the survivor, duplicates removed.
    """
    data = request.get_json(force=True, silent=True) or {}
    node_ids = data.get("node_ids", [])
    if not isinstance(node_ids, list) or len(node_ids) < 2:
        return jsonify({"error": "node_ids must be a list of at least 2 IDs"}), 400
    if len({str(node_id) for node_id in node_ids}) != len(node_ids):
        return jsonify({"error": "node_ids must not contain duplicates"}), 400
    # Validate all IDs
    for nid in node_ids:
        if not _safe_id(str(nid)):
            return jsonify({"error": f"invalid node_id: {nid}"}), 400
    node_type = data.get("node_type", "")
    if node_type and node_type not in VALID_NODE_TYPES:
        return jsonify({"error": f"invalid node_type '{node_type}'"}), 400

    survivor_id = node_ids[0]
    absorbed_ids = node_ids[1:]
    try:
        result = KnowledgeGraphDB.merge_nodes(survivor_id, absorbed_ids, node_type)
        if not result:
            return jsonify({"error": "survivor node not found"}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": "merge failed", "detail": str(e)}), 500


# ---------------------------------------------------------------------------
# Graph: Edges
# ---------------------------------------------------------------------------

@knowledge_bp.route("/api/knowledge/edges", methods=["POST"])
@admin_required
@require_savant_app
def create_edge():
    """Create an edge between two nodes."""
    data = request.get_json(force=True, silent=True) or {}
    source_id = _safe_id(data.get("source_id", ""))
    target_id = _safe_id(data.get("target_id", ""))
    if not source_id or not target_id:
        return jsonify({"error": "source_id and target_id are required"}), 400
    data["source_id"] = source_id
    data["target_id"] = target_id
    data["edge_type"] = _validate_edge_type(data.get("edge_type", "relates_to"))
    try:
        edge = KnowledgeGraphDB.create_edge(data)
        return jsonify(edge)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "internal error", "detail": str(e)}), 500


@knowledge_bp.route("/api/knowledge/edges/<edge_id>", methods=["DELETE"])
@admin_required
@require_savant_app
def delete_edge(edge_id):
    """Delete an edge."""
    if not _safe_id(edge_id):
        return jsonify({"error": "not found"}), 404
    deleted = KnowledgeGraphDB.delete_edge(edge_id)
    if deleted:
        return jsonify({"deleted": True, "edge_id": edge_id})
    return jsonify({"error": "not found"}), 404


@knowledge_bp.route("/api/knowledge/edges/disconnect", methods=["POST"])
@admin_required
@require_savant_app
def disconnect_edge():
    """Remove edge(s) between two nodes."""
    data = request.get_json(force=True)
    source_id = data.get("source_id", "")
    target_id = data.get("target_id", "")
    edge_type = data.get("edge_type", "")
    if not source_id or not target_id:
        return jsonify({"error": "source_id and target_id are required"}), 400
    deleted = KnowledgeGraphDB.delete_edge_by_nodes(source_id, target_id, edge_type)
    return jsonify({"deleted": deleted, "source_id": source_id, "target_id": target_id})


# ---------------------------------------------------------------------------
# Graph: Queries
# ---------------------------------------------------------------------------

@knowledge_bp.route("/api/knowledge/link-workspace", methods=["POST"])
@admin_required
@require_savant_app
def link_to_workspace():
    """Link a node to a workspace by adding workspace_id to metadata.workspaces array."""
    data = request.get_json(force=True)
    node_id = data.get("node_id", "").strip()
    workspace_id = data.get("workspace_id", "").strip()
    if not node_id or not workspace_id:
        return jsonify({"error": "node_id and workspace_id required"}), 400
    node = KnowledgeGraphDB.get_node(node_id)
    if not node:
        return jsonify({"error": "node not found"}), 404
    meta = node.get("metadata", {}) or {}
    ws_list = meta.get("workspaces", []) or []
    if workspace_id not in ws_list:
        ws_list.append(workspace_id)
    meta["workspaces"] = ws_list
    KnowledgeGraphDB.update_node(node_id, {"metadata": meta})
    return jsonify({"linked": True, "node_id": node_id, "workspaces": ws_list})


@knowledge_bp.route("/api/knowledge/unlink-workspace", methods=["POST"])
@admin_required
@require_savant_app
def unlink_workspace():
    """Remove a workspace from node metadata.workspaces array."""
    data = request.get_json(force=True)
    node_id = data.get("node_id", "").strip()
    workspace_id = data.get("workspace_id", "").strip()
    if not node_id or not workspace_id:
        return jsonify({"error": "node_id and workspace_id required"}), 400
    node = KnowledgeGraphDB.get_node(node_id)
    if not node:
        return jsonify({"error": "node not found"}), 404
    meta = node.get("metadata", {}) or {}
    ws_list = meta.get("workspaces", []) or []
    ws_list = [w for w in ws_list if w != workspace_id]
    meta["workspaces"] = ws_list
    KnowledgeGraphDB.update_node(node_id, {"metadata": meta})
    return jsonify({"unlinked": True, "node_id": node_id, "workspaces": ws_list})


@knowledge_bp.route("/api/knowledge/resolve-workspaces", methods=["POST"])
@admin_required
def resolve_workspaces():
    """Resolve workspace IDs to {id, name} pairs for UI display."""
    data = request.get_json(force=True)
    ws_ids = data.get("workspace_ids", [])
    if not isinstance(ws_ids, list):
        return jsonify({"error": "workspace_ids must be an array"}), 400
    results = []
    for wid in ws_ids:
        ws = WorkspaceDB.get_by_id(wid)
        results.append({
            "id": wid,
            "name": ws.get("name", wid) if ws else wid,
            "found": ws is not None,
        })
    return jsonify({"workspaces": results})


@knowledge_bp.route("/api/knowledge/graph", methods=["GET"])
def get_graph():
    """Get graph (nodes + edges) for visualization.

    ?slim=true returns only node_id, node_type, title, status, created_at per node —
    omitting content and metadata — for faster initial load.  Full node data is
    available on demand via GET /api/knowledge/nodes/<node_id>.
    """
    node_type = request.args.get("node_type", "")
    limit = _safe_int(request.args.get("limit", 2000), default=2000, min_val=1, max_val=5000)
    include_staged = request.args.get("include_staged", "").lower() in ("true", "1", "yes")
    slim = request.args.get("slim", "").lower() in ("true", "1", "yes")
    workspace_id = request.args.get("workspace_id", "")
    if workspace_id:
        # Workspace filter always fetches full graph (needs metadata.workspaces to filter)
        graph = KnowledgeGraphDB.get_full_graph(limit=limit, include_staged=include_staged)
        ws_nodes = [n for n in graph["nodes"]
                    if workspace_id in (n.get("metadata") or {}).get("workspaces", [])]
        ws_node_ids = {n["node_id"] for n in ws_nodes}
        ws_edges = [e for e in graph["edges"]
                    if e["source_id"] in ws_node_ids and e["target_id"] in ws_node_ids]
        # If slim, strip content/metadata from the workspace-filtered nodes
        if slim:
            ws_nodes = [
                {k: n[k] for k in ("node_id", "node_type", "title", "status", "created_at") if k in n}
                for n in ws_nodes
            ]
        return jsonify({"nodes": ws_nodes, "edges": ws_edges})
    graph = KnowledgeGraphDB.get_full_graph(node_type=node_type, limit=limit, include_staged=include_staged, slim=slim)
    return jsonify(graph)


@knowledge_bp.route("/api/knowledge/neighbors/<node_id>", methods=["GET"])
def get_neighbors(node_id):
    """Get connected nodes (1-hop or n-hop)."""
    if not _safe_id(node_id):
        return jsonify({"nodes": [], "edges": []})
    depth = _safe_int(request.args.get("depth", 1), default=1, min_val=1, max_val=5)
    edge_type = request.args.get("edge_type", "")
    include_staged = request.args.get("include_staged", "").lower() in ("true", "1", "yes")
    result = KnowledgeGraphDB.get_neighbors(node_id, depth=depth, edge_type=edge_type, include_staged=include_staged)
    return jsonify(result)


@knowledge_bp.route("/api/knowledge/concepts", methods=["GET"])
def list_concepts():
    """List all concept nodes (for autocomplete/tagging)."""
    nodes = KnowledgeGraphDB.list_nodes(node_type="concept", limit=500)
    return jsonify(nodes)


# ---------------------------------------------------------------------------
# Prompt generation
# ---------------------------------------------------------------------------

NODE_ICONS = {
    "insight": "💡", "client": "🏢", "domain": "🗂️", "service": "⚙️",
    "library": "📚", "technology": "🔧", "project": "📁", "concept": "💭",
    "session": "🖥️", "repo": "📦",
}


@knowledge_bp.route("/api/knowledge/prompt", methods=["POST"])
def generate_prompt():
    """Build an AI prompt from a set of KG nodes.

    Body: { node_ids: [str], question: str (optional) }
    Returns: { prompt: str, node_count: int, nodes: [...] }
    """
    data = request.get_json(force=True, silent=True) or {}
    node_ids = data.get("node_ids", [])
    question = (data.get("question") or "").strip()

    if not node_ids:
        return jsonify({"error": "node_ids is required and must be non-empty"}), 400
    if len(node_ids) > 30:
        return jsonify({"error": "Cannot generate prompt for more than 30 nodes"}), 400

    # Fetch each node + its neighbor titles
    found_nodes = []
    # Build a title lookup from graph neighbors
    for nid in node_ids:
        node = KnowledgeGraphDB.get_node(str(nid))
        if not node:
            continue
        # Edges are already attached by get_node()
        connections = []
        for e in node.get("edges", []):
            other_id = e["target_id"] if e["source_id"] == nid else e["source_id"]
            other_node = KnowledgeGraphDB.get_node(other_id)
            other_title = other_node["title"] if other_node else other_id
            connections.append(f"{e['edge_type']} → {other_title}")
        node["_connections"] = connections
        found_nodes.append(node)

    # Build prompt string
    question_line = question if question else "[USER FILLS THIS IN]"
    lines = [
        "You are an expert software engineer. Below is context from the projectx knowledge graph.",
        f"Look at these nodes and answer the following: {question_line}",
        "",
        "--- KNOWLEDGE CONTEXT ---",
        "",
    ]
    for node in found_nodes:
        icon = NODE_ICONS.get(node.get("node_type", ""), "❓")
        lines.append(f"{icon} {node.get('node_type','').upper()}: {node['title']}")
        if node.get("content"):
            lines.append(node["content"][:500])
        if node["_connections"]:
            lines.append(f"Connections: {', '.join(node['_connections'])}")
        lines.append("")

    lines.append(f"Total: {len(found_nodes)} nodes")

    prompt = "\n".join(lines)
    return jsonify({
        "prompt": prompt,
        "node_count": len(found_nodes),
        "nodes": [{"node_id": n["node_id"], "title": n["title"], "node_type": n["node_type"]} for n in found_nodes],
    })




@knowledge_bp.route("/api/knowledge/store", methods=["POST"])
@admin_required
@require_savant_app
def store_experience():
    """Store a curated experience and its optional graph connections."""
    data = request.get_json(force=True, silent=True) or {}
    content = _text_value(data.get("content"), MAX_CONTENT_LEN)
    if not content:
        return jsonify({"error": "content is required"}), 400
    workspace_id = _text_value(data.get("workspace_id"), 200)
    node = KnowledgeGraphDB.create_node(
        _experience_node_payload(data, content, workspace_id)
    )
    _create_experience_connections(node["node_id"], data.get("connections"))
    _store_legacy_experience(node["node_id"], data, content, workspace_id)
    return jsonify(node)


def _experience_node_payload(data: dict, content: str, workspace_id: str) -> dict:
    title = _text_value(data.get("title") or content.split("\n", 1)[0] or "Untitled insight", MAX_TITLE_LEN)
    node_type = _text_value(data.get("node_type") or "insight", 50)
    if node_type not in VALID_NODE_TYPES:
        node_type = "insight"
    metadata = {
        "source": _text_value(data.get("source") or "note", 100),
        "files": data.get("files", []),
        "repo": _text_value(data.get("repo"), 500),
    }
    graph_type = _text_value(data.get("graph_type"), 100)
    if graph_type:
        metadata["graph_type"] = graph_type
    if workspace_id:
        metadata["workspaces"] = [workspace_id]
    return {
        "node_type": node_type,
        "title": title,
        "content": content,
        "metadata": metadata,
        "status": "staged" if workspace_id else "committed",
    }


def _create_experience_connections(source_id: str, connections) -> None:
    if not isinstance(connections, list):
        return
    for connection in connections:
        if not isinstance(connection, dict) or not connection.get("node_id"):
            continue
        try:
            KnowledgeGraphDB.create_edge({
                "source_id": source_id,
                "target_id": connection["node_id"],
                "edge_type": connection.get("edge_type", "relates_to"),
            })
        except Exception:
            continue


def _store_legacy_experience(node_id: str, data: dict, content: str, workspace_id: str) -> None:
    try:
        ExperienceDB.create({
            "experience_id": node_id,
            "content": content,
            "source": data.get("source", "note"),
            "workspace_id": workspace_id,
            "repo": data.get("repo", ""),
            "files": data.get("files", []),
        })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Backward-compatible: search, recent, list, project_context, delete
# ---------------------------------------------------------------------------

@knowledge_bp.route("/api/knowledge/search", methods=["POST"])
def search_experience():
    """Search knowledge by text query (searches graph nodes)."""
    data = request.get_json(force=True, silent=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400

    node_type = data.get("node_type", "")
    limit = _safe_int(data.get("limit", 20), default=20, min_val=1, max_val=100)
    include_staged = str(data.get("include_staged", "")).lower() in ("true", "1", "yes")
    results = KnowledgeGraphDB.search_nodes(query, node_type=node_type, limit=limit, include_staged=include_staged)
    return jsonify({"result": results})


@knowledge_bp.route("/api/knowledge/recent", methods=["GET"])
def recent_experience():
    """Get recent knowledge nodes."""
    node_type = request.args.get("node_type", "")
    limit = _safe_int(request.args.get("limit", 20), default=20, min_val=1, max_val=100)
    include_staged = request.args.get("include_staged", "").lower() in ("true", "1", "yes")
    results = KnowledgeGraphDB.list_nodes(node_type=node_type, limit=limit, include_staged=include_staged)
    return jsonify({"result": results})


@knowledge_bp.route("/api/knowledge/project_context", methods=["GET"])
def project_context():
    """Aggregate project context via graph traversal."""
    workspace_id = request.args.get("workspace_id", "")
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400

    # Find project node for this workspace — project context should see all nodes
    proj_id = f"proj_{workspace_id}"
    context = KnowledgeGraphDB.get_project_context(proj_id, include_staged=True)

    if "error" in context:
        # Fallback: no project node yet, return basic context
        experiences = ExperienceDB.list_by_workspace(workspace_id, limit=30)
        try:
            tasks = TaskDB.list_by_workspace(workspace_id, limit=30)
        except Exception:
            tasks = []
        try:
            notes = NoteDB.list_by_workspace(workspace_id, limit=20)
        except Exception:
            notes = []
        return jsonify({
            "workspace_id": workspace_id,
            "summary": "No project node found. Showing legacy context.",
            "experience_count": len(experiences),
            "task_count": len(tasks),
            "note_count": len(notes),
        })

    return jsonify(context)


@knowledge_bp.route("/api/knowledge/list", methods=["GET"])
def list_experiences():
    """List knowledge nodes (backward-compatible)."""
    node_type = request.args.get("node_type", "")
    limit = _safe_int(request.args.get("limit", 200), default=200, min_val=1, max_val=500)
    include_staged = request.args.get("include_staged", "").lower() in ("true", "1", "yes")
    # Backward compat: workspace_id filter
    workspace_id = request.args.get("workspace_id", "")
    if workspace_id:
        # Filter by metadata.workspaces
        all_nodes = KnowledgeGraphDB.list_nodes(node_type=node_type, limit=5000, include_staged=include_staged)
        ws_nodes = [n for n in all_nodes
                    if workspace_id in (n.get("metadata") or {}).get("workspaces", [])]
        return jsonify(ws_nodes[:limit])
    results = KnowledgeGraphDB.list_nodes(node_type=node_type, limit=limit, include_staged=include_staged)
    return jsonify(results)


@knowledge_bp.route("/api/knowledge/<item_id>", methods=["DELETE"])
@admin_required
@require_savant_app
def delete_item(item_id):
    """Delete a knowledge node (or legacy experience) by ID."""
    # Try graph node first
    deleted = KnowledgeGraphDB.delete_node(item_id)
    if deleted:
        # Also clean up legacy
        try:
            ExperienceDB.delete(item_id)
        except Exception:
            pass
        return jsonify({"deleted": True, "node_id": item_id})
    # Fallback: try legacy experience
    deleted = ExperienceDB.delete(item_id)
    if deleted:
        return jsonify({"deleted": True, "experience_id": item_id})
    return jsonify({"error": "not found"}), 404


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------

@knowledge_bp.route("/api/knowledge/nodes/bulk-delete", methods=["POST"])
@admin_required
@require_savant_app
def bulk_delete_nodes():
    """Delete multiple nodes at once."""
    data = request.get_json(force=True, silent=True) or {}
    node_ids = data.get("node_ids", [])
    if not isinstance(node_ids, list) or not node_ids:
        return jsonify({"error": "node_ids is required (non-empty list)"}), 400
    deleted = 0
    for nid in node_ids:
        if _safe_id(str(nid)) and KnowledgeGraphDB.delete_node(str(nid)):
            deleted += 1
    return jsonify({"deleted": deleted})


@knowledge_bp.route("/api/knowledge/nodes/bulk-link-workspace", methods=["POST"])
@admin_required
@require_savant_app
def bulk_link_workspace():
    """Link multiple nodes to a workspace via metadata.workspaces."""
    data = request.get_json(force=True, silent=True) or {}
    node_ids = data.get("node_ids", [])
    workspace_id = data.get("workspace_id", "").strip()
    if not isinstance(node_ids, list) or not node_ids or not workspace_id:
        return jsonify({"error": "node_ids (list) and workspace_id are required"}), 400
    linked = 0
    for nid in node_ids:
        nid = str(nid).strip()
        node = KnowledgeGraphDB.get_node(nid)
        if not node:
            continue
        meta = node.get("metadata", {}) or {}
        ws_list = meta.get("workspaces", []) or []
        if workspace_id not in ws_list:
            ws_list.append(workspace_id)
            meta["workspaces"] = ws_list
            KnowledgeGraphDB.update_node(nid, {"metadata": meta})
        linked += 1
    return jsonify({"linked": linked})


@knowledge_bp.route("/api/knowledge/edges/bulk", methods=["POST"])
@admin_required
def bulk_create_edges():
    """Create edges from one source to multiple targets."""
    data = request.get_json(force=True, silent=True) or {}
    source_id = _safe_id(data.get("source_id", ""))
    target_ids = data.get("target_ids", [])
    edge_type = _validate_edge_type(data.get("edge_type", "relates_to"))
    if not source_id or not isinstance(target_ids, list) or not target_ids:
        return jsonify({"error": "source_id and target_ids (list) are required"}), 400
    created = 0
    for tid in target_ids:
        tid = _safe_id(str(tid))
        if not tid:
            continue
        try:
            KnowledgeGraphDB.create_edge({
                "source_id": source_id,
                "target_id": tid,
                "edge_type": edge_type,
            })
            created += 1
        except (ValueError, Exception):
            pass
    return jsonify({"created": created})


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------

@knowledge_bp.route("/api/knowledge/export", methods=["GET"])
def export_workspace_kg():
    """Export a workspace's KG as JSON (nodes + edges)."""
    workspace_id = request.args.get("workspace_id", "").strip()
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    graph = KnowledgeGraphDB.get_full_graph(limit=1000)
    ws_nodes = [n for n in graph["nodes"]
                if workspace_id in (n.get("metadata") or {}).get("workspaces", [])]
    ws_node_ids = {n["node_id"] for n in ws_nodes}
    ws_edges = [e for e in graph["edges"]
                if e["source_id"] in ws_node_ids and e["target_id"] in ws_node_ids]
    export_nodes = []
    for n in ws_nodes:
        export_nodes.append({
            "node_type": n["node_type"],
            "title": n["title"],
            "content": n.get("content", ""),
            "metadata": {k: v for k, v in (n.get("metadata") or {}).items() if k != "workspaces"},
        })
    export_edges = []
    id_to_title = {n["node_id"]: n["title"] for n in ws_nodes}
    for e in ws_edges:
        export_edges.append({
            "source_title": id_to_title.get(e["source_id"], ""),
            "target_title": id_to_title.get(e["target_id"], ""),
            "edge_type": e["edge_type"],
            "label": e.get("label", ""),
        })
    return jsonify({"nodes": export_nodes, "edges": export_edges})


@knowledge_bp.route("/api/knowledge/import", methods=["POST"])
@admin_required
def import_workspace_kg():
    """Import nodes+edges into a workspace. Deduplicates by title+type."""
    data = request.get_json(force=True, silent=True) or {}
    workspace_id = data.get("workspace_id", "").strip()
    if not workspace_id:
        return jsonify({"error": "workspace_id is required"}), 400
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])

    existing = KnowledgeGraphDB.list_nodes(limit=5000, include_staged=True)
    existing_index = {}
    for n in existing:
        key = f"{n['node_type']}:{n['title']}"
        existing_index[key] = n

    created_count = 0
    skipped_count = 0
    title_to_id = {}

    for node_data in nodes:
        title = (node_data.get("title") or "").strip()
        ntype = node_data.get("node_type", "insight")
        if not title:
            continue
        key = f"{ntype}:{title}"
        if key in existing_index:
            ex = existing_index[key]
            title_to_id[title] = ex["node_id"]
            meta = ex.get("metadata", {}) or {}
            ws_list = meta.get("workspaces", []) or []
            if workspace_id not in ws_list:
                ws_list.append(workspace_id)
                meta["workspaces"] = ws_list
                KnowledgeGraphDB.update_node(ex["node_id"], {"metadata": meta})
            skipped_count += 1
        else:
            meta = node_data.get("metadata", {}) or {}
            meta["workspaces"] = meta.get("workspaces", []) or []
            if workspace_id not in meta["workspaces"]:
                meta["workspaces"].append(workspace_id)
            try:
                new_node = KnowledgeGraphDB.create_node({
                    "node_type": ntype,
                    "title": title,
                    "content": node_data.get("content", ""),
                    "metadata": meta,
                    "status": "committed",
                })
                title_to_id[title] = new_node["node_id"]
                existing_index[key] = new_node
                created_count += 1
            except (ValueError, Exception):
                skipped_count += 1

    edges_created = 0
    for edge_data in edges:
        src_title = edge_data.get("source_title", "")
        tgt_title = edge_data.get("target_title", "")
        edge_type = edge_data.get("edge_type", "relates_to")
        src_id = title_to_id.get(src_title, "")
        tgt_id = title_to_id.get(tgt_title, "")
        if src_id and tgt_id:
            try:
                KnowledgeGraphDB.create_edge({
                    "source_id": src_id,
                    "target_id": tgt_id,
                    "edge_type": edge_type,
                    "label": edge_data.get("label", ""),
                })
                edges_created += 1
            except (ValueError, Exception):
                pass

    return jsonify({
        "nodes_created": created_count,
        "nodes_skipped": skipped_count,
        "edges_created": edges_created,
    })


# ---------------------------------------------------------------------------
# Info (grouped stats)
# ---------------------------------------------------------------------------

@knowledge_bp.route("/api/knowledge/info", methods=["GET"])
def kg_info():
    """Return grouped node and edge summaries for the info modal."""
    workspace_id = request.args.get("workspace_id", "").strip()
    include_staged = request.args.get("include_staged", "").lower() in ("true", "1", "yes")
    graph = KnowledgeGraphDB.get_full_graph(limit=1000, include_staged=include_staged)
    nodes = graph["nodes"]
    edges = graph["edges"]

    if workspace_id:
        nodes = [n for n in nodes
                 if workspace_id in (n.get("metadata") or {}).get("workspaces", [])]
        ws_ids = {n["node_id"] for n in nodes}
        edges = [e for e in edges
                 if e["source_id"] in ws_ids and e["target_id"] in ws_ids]

    nodes_by_type = {}
    for n in nodes:
        t = n.get("node_type", "unknown")
        if t not in nodes_by_type:
            nodes_by_type[t] = []
        nodes_by_type[t].append({"node_id": n["node_id"], "title": n["title"]})

    edges_by_type = {}
    id_to_title = {n["node_id"]: n["title"] for n in nodes}
    for e in edges:
        t = e.get("edge_type", "relates_to")
        if t not in edges_by_type:
            edges_by_type[t] = []
        edges_by_type[t].append({
            "edge_id": e["edge_id"],
            "source": id_to_title.get(e["source_id"], e["source_id"]),
            "target": id_to_title.get(e["target_id"], e["target_id"]),
        })

    return jsonify({
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "nodes_by_type": sorted([
            {"type": t, "count": len(items), "items": sorted(items, key=lambda x: x["title"])}
            for t, items in nodes_by_type.items()
        ], key=lambda x: x["type"]),
        "edges_by_type": sorted([
            {"type": t, "count": len(items), "items": items}
            for t, items in edges_by_type.items()
        ], key=lambda x: x["type"]),
    })


# ---------------------------------------------------------------------------
# Purge workspace
# ---------------------------------------------------------------------------

def _classify_workspace_nodes(workspace_id: str):
    """Return (exclusive, shared) node lists for a workspace."""
    all_nodes = KnowledgeGraphDB.list_nodes(include_staged=True, limit=10_000)
    ws_nodes = [n for n in all_nodes
                if workspace_id in (n.get("metadata") or {}).get("workspaces", [])]
    exclusive = []
    shared = []
    for n in ws_nodes:
        ws_list = (n.get("metadata") or {}).get("workspaces", []) or []
        if len(ws_list) <= 1:
            exclusive.append(n)
        else:
            shared.append(n)
    return exclusive, shared


@knowledge_bp.route("/api/knowledge/purge-workspace-preview", methods=["POST"])
def purge_workspace_preview():
    """Dry-run: show what a workspace purge would do without modifying data."""
    data = request.get_json(force=True)
    workspace_id = (data.get("workspace_id") or "").strip()
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    exclusive, shared = _classify_workspace_nodes(workspace_id)
    return jsonify({
        "workspace_id": workspace_id,
        "to_delete": len(exclusive),
        "to_unlink": len(shared),
        "delete_node_ids": [n["node_id"] for n in exclusive],
        "unlink_node_ids": [n["node_id"] for n in shared],
    })


@knowledge_bp.route("/api/knowledge/purge-workspace", methods=["POST"])
@admin_required
@require_savant_app
def purge_workspace():
    """Delete exclusive nodes and unlink shared nodes for a workspace."""
    data = request.get_json(force=True)
    workspace_id = (data.get("workspace_id") or "").strip()
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    exclusive, shared = _classify_workspace_nodes(workspace_id)

    for n in exclusive:
        KnowledgeGraphDB.delete_node(n["node_id"])

    for n in shared:
        meta = n.get("metadata", {}) or {}
        ws_list = [w for w in (meta.get("workspaces", []) or []) if w != workspace_id]
        meta["workspaces"] = ws_list
        KnowledgeGraphDB.update_node(n["node_id"], {"metadata": meta})

    return jsonify({
        "purged": True,
        "workspace_id": workspace_id,
        "deleted_count": len(exclusive),
        "unlinked_count": len(shared),
    })
