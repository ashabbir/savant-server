"""Flask Blueprint for Graphify REST API. All routes under /api/graphify/*."""

import uuid
import json
import logging
from pathlib import Path
from flask import Blueprint, jsonify, request
from db.graphify import GraphifyDB
from context.db import ContextDB

logger = logging.getLogger(__name__)

graphify_bp = Blueprint("graphify", __name__)

# In-memory session tracker for chunked uploads: upload_id -> {workspace_id, node_ids}
_upload_sessions: dict = {}


def _codegraph_repo(repo_id=None, workspace_id=None):
    """Resolve explicit repo_id first, then the legacy name/id compatibility key."""
    identifier = repo_id or workspace_id
    if not identifier:
        return None, None
    repo = ContextDB.get_repo_by_identifier(str(identifier))
    if not repo:
        return None, None
    from db.code_intelligence import CodeIntelligenceConfigDB
    config = CodeIntelligenceConfigDB.get(str(repo["id"]))
    return (repo, config) if config and config.get("provider") == "codegraph" else (None, None)


def _project_symbol(symbol):
    return {
        "node_id": symbol.id,
        "node_type": symbol.kind,
        "title": symbol.name,
        "content": symbol.metadata.get("source", ""),
        "metadata": {
            **symbol.metadata,
            "path": symbol.location.file_path,
            "start_line": symbol.location.start_line,
            "end_line": symbol.location.end_line,
            "qualified_name": symbol.qualified_name,
            "signature": symbol.signature,
        },
    }


def _project_edge(edge):
    return {
        "edge_id": f"{edge.source_id}:{edge.kind}:{edge.target_id}",
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "edge_type": edge.kind,
        "weight": edge.confidence if edge.confidence is not None else 1.0,
        "label": edge.kind,
        "provenance": edge.provenance.value,
    }

@graphify_bp.route("/api/graphify/import", methods=["POST"])
def import_graphify():
    try:
        data = request.get_json(force=True) or {}
        workspace_id = data.get("workspace_id")
        graph_data = data.get("graph")
        meta_data = data.get("meta")
        
        if not workspace_id:
            return jsonify({"error": "workspace_id is required"}), 400
        if not graph_data or not isinstance(graph_data, dict):
            return jsonify({"error": "graph dictionary is required"}), 400

        result = GraphifyDB.import_graph(workspace_id, graph_data, meta_data)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Failed to import graphify: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@graphify_bp.route("/api/graphify/import/from-disk", methods=["POST"])
def import_from_disk():
    """Server reads graphify-out/graph.json directly from the repo path on disk.
    No file upload needed — zero size limit."""
    try:
        data = request.get_json(force=True) or {}
        workspace_id = data.get("workspace_id")
        if not workspace_id:
            return jsonify({"error": "workspace_id is required"}), 400

        repo = ContextDB.get_repo(workspace_id)
        if not repo:
            return jsonify({"error": f"Repo '{workspace_id}' not found"}), 404

        graph_path = Path(repo["path"]) / "graphify-out" / "graph.json"
        meta_path  = Path(repo["path"]) / "graphify-out" / "meta.json"

        if not graph_path.exists():
            return jsonify({"error": f"graphify-out/graph.json not found at {repo['path']}"}), 404

        graph_data = json.loads(graph_path.read_text(encoding="utf-8"))
        meta_data  = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None

        result = GraphifyDB.import_graph(workspace_id, graph_data, meta_data)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Failed to import graphify from disk: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@graphify_bp.route("/api/graphify/check-disk", methods=["GET"])
def check_disk():
    """Returns whether graphify-out/graph.json exists on the server for a workspace."""
    try:
        workspace_id = request.args.get("workspace_id")
        if not workspace_id:
            return jsonify({"error": "workspace_id is required"}), 400

        repo = ContextDB.get_repo(workspace_id)
        if not repo:
            return jsonify({"available": False}), 200

        graph_path = Path(repo["path"]) / "graphify-out" / "graph.json"
        size_mb = round(graph_path.stat().st_size / (1024 * 1024), 1) if graph_path.exists() else 0
        return jsonify({"available": graph_path.exists(), "size_mb": size_mb}), 200
    except Exception as e:
        logger.error(f"Failed to check graphify disk: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@graphify_bp.route("/api/graphify/import/begin", methods=["POST"])
def import_begin():
    """Start a chunked upload session. Clears existing data for the workspace."""
    try:
        data = request.get_json(force=True) or {}
        workspace_id = data.get("workspace_id")
        if not workspace_id:
            return jsonify({"error": "workspace_id is required"}), 400
        upload_id = str(uuid.uuid4())
        _upload_sessions[upload_id] = {"workspace_id": workspace_id, "node_ids": set()}
        GraphifyDB.clear_workspace(workspace_id)
        return jsonify({"upload_id": upload_id}), 200
    except Exception as e:
        logger.error(f"Failed to begin chunked import: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@graphify_bp.route("/api/graphify/import/chunk", methods=["POST"])
def import_chunk():
    """Upload a batch of nodes and/or edges for an active chunked upload session."""
    try:
        data = request.get_json(force=True) or {}
        upload_id = data.get("upload_id")
        if not upload_id or upload_id not in _upload_sessions:
            return jsonify({"error": "invalid or expired upload_id"}), 400

        session = _upload_sessions[upload_id]
        workspace_id = session["workspace_id"]
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])

        result = GraphifyDB.import_chunk(workspace_id, nodes, edges, node_ids_so_far=session["node_ids"])
        session["node_ids"].update(result.get("node_ids", []))

        return jsonify({
            "nodes_inserted": result["nodes_inserted"],
            "edges_inserted": result["edges_inserted"],
            "total_node_ids": len(session["node_ids"])
        }), 200
    except Exception as e:
        logger.error(f"Failed to import chunk: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@graphify_bp.route("/api/graphify/import/commit", methods=["POST"])
def import_commit():
    """Finalize a chunked upload session and return total counts."""
    try:
        data = request.get_json(force=True) or {}
        upload_id = data.get("upload_id")
        if not upload_id or upload_id not in _upload_sessions:
            return jsonify({"error": "invalid or expired upload_id"}), 400

        session = _upload_sessions.pop(upload_id)
        workspace_id = session["workspace_id"]
        meta_data = data.get("meta")

        result = GraphifyDB.finalize_import(workspace_id, meta_data)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Failed to commit chunked import: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@graphify_bp.route("/api/graphify/stats", methods=["GET"])
def get_stats():
    try:
        repo_id = request.args.get("repo_id")
        workspace_id = request.args.get("workspace_id")
        if not repo_id and not workspace_id:
            return jsonify({"error": "repo_id or workspace_id is required"}), 400

        repo, config = _codegraph_repo(repo_id, workspace_id)
        if repo:
            from code_intelligence.runtime import build_service
            identifier = str(repo["id"])
            health = build_service().health(identifier, Path(repo["path"]))
            return jsonify({
                "total": health.nodes or 0,
                "stats": {},
                "edges": {},
                "total_edges": health.edges or 0,
                "files": health.files,
                "provider": health.provider,
                "freshness": health.freshness.value,
                "graph_version": health.graph_version,
                "warnings": health.warnings,
                "deprecated": True,
            }), 200

        stats = GraphifyDB.get_stats(workspace_id or repo_id)
        formatted_stats = {
            "total": stats.get("total_nodes", 0),
            "stats": stats.get("nodes", {}),
            "edges": stats.get("edges", {}),
            "total_edges": stats.get("total_edges", 0)
        }
        return jsonify(formatted_stats), 200
    except Exception as e:
        logger.error(f"Failed to get graphify stats: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@graphify_bp.route("/api/graphify/search", methods=["POST"])
def search_graphify():
    try:
        data = request.get_json(force=True) or {}
        workspace_id = data.get("repo_id") or data.get("workspace_id")
        query = data.get("query", "")
        limit = int(data.get("limit", 20))

        repo = ContextDB.get_repo_by_identifier(workspace_id) if workspace_id else None
        from db.code_intelligence import CodeIntelligenceConfigDB
        config = CodeIntelligenceConfigDB.get(workspace_id) if repo else None
        if repo and config and config.get("provider") == "codegraph":
            from code_intelligence.runtime import build_service
            explored = build_service().explore(workspace_id, Path(repo["path"]), query, max_files=limit)
            nodes = [{
                "node_id": s.id, "node_type": s.kind, "title": s.name,
                "content": s.metadata.get("source", ""),
                "metadata": {**s.metadata, "path": s.location.file_path,
                             "start_line": s.location.start_line, "end_line": s.location.end_line,
                             "qualified_name": s.qualified_name, "signature": s.signature},
            } for s in explored.symbols]
            edge_map = {}
            for edge in explored.edges:
                edge_map.setdefault(edge.source_id, []).append({
                    "edge_id": f"{edge.source_id}:{edge.kind}:{edge.target_id}",
                    "source_id": edge.source_id, "target_id": edge.target_id,
                    "edge_type": edge.kind, "weight": edge.confidence or 1.0,
                    "label": edge.kind, "provenance": edge.provenance.value,
                })
            for node in nodes:
                node["edges"] = edge_map.get(node["node_id"], [])
            return jsonify({"results": nodes, "nodes": nodes,
                            "edges": [item for values in edge_map.values() for item in values],
                            "provider": explored.provider, "incomplete": explored.incomplete,
                            "warnings": explored.warnings, "deprecated": True}), 200
        results = GraphifyDB.search(query, workspace_id, limit=limit)
        return jsonify(results), 200
    except Exception as e:
        logger.error(f"Failed to search graphify: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@graphify_bp.route("/api/graphify/main-entities", methods=["GET"])
def get_main_entities():
    try:
        repo_id = request.args.get("repo_id")
        workspace_id = request.args.get("workspace_id")
        limit = int(request.args.get("limit", 30))
        if not repo_id and not workspace_id:
            return jsonify({"error": "repo_id or workspace_id is required"}), 400

        repo, config = _codegraph_repo(repo_id, workspace_id)
        if repo:
            from code_intelligence.runtime import build_service
            identifier = str(repo["id"])
            bounded = max(1, min(limit, 100))
            explored = build_service().explore(identifier, Path(repo["path"]), repo["name"], max_files=bounded, include_source=False)
            nodes = sorted((_project_symbol(item) for item in explored.symbols), key=lambda item: (item["title"].lower(), item["node_id"]))[:bounded]
            node_ids = {item["node_id"] for item in nodes}
            edges = sorted(
                (_project_edge(item) for item in explored.edges if item.source_id in node_ids and item.target_id in node_ids),
                key=lambda item: (item["source_id"], item["target_id"], item["edge_type"]),
            )[: bounded * 2]
            return jsonify({"nodes": nodes, "edges": edges, "provider": explored.provider,
                            "incomplete": explored.incomplete or len(explored.symbols) > bounded,
                            "warnings": explored.warnings, "deprecated": True}), 200

        result = GraphifyDB.get_main_entities(workspace_id or repo_id, limit=limit)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Failed to get main entities: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@graphify_bp.route("/api/graphify/neighbors", methods=["GET"])
def get_neighbors():
    try:
        repo_id = request.args.get("repo_id")
        workspace_id = request.args.get("workspace_id")
        node_id = request.args.get("node_id")
        if not repo_id and not workspace_id:
            return jsonify({"error": "repo_id or workspace_id is required"}), 400
        if not node_id:
            return jsonify({"error": "node_id is required"}), 400

        repo, config = _codegraph_repo(repo_id, workspace_id)
        if repo:
            if not node_id.startswith("codegraph:"):
                return jsonify({"error": "node does not belong to selected CodeGraph repository"}), 404
            from code_intelligence.runtime import build_service
            identifier = str(repo["id"])
            graph = build_service().subgraph(identifier, Path(repo["path"]), roots=[{"id": node_id}],
                                               mode="neighbors", depth=1, limit=100)
            nodes = sorted((_project_symbol(item) for item in graph.symbols), key=lambda item: item["node_id"])
            edges = sorted((_project_edge(item) for item in graph.edges),
                           key=lambda item: (item["source_id"], item["target_id"], item["edge_type"]))
            return jsonify({"nodes": nodes, "edges": edges, "provider": "codegraph",
                            "incomplete": graph.incomplete, "warnings": graph.warnings,
                            "deprecated": True}), 200

        result = GraphifyDB.get_neighbors(workspace_id or repo_id, node_id)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Failed to get neighbors: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
