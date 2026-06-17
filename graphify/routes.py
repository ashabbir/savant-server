"""Flask Blueprint for Graphify REST API. All routes under /api/graphify/*."""

import logging
from flask import Blueprint, jsonify, request
from db.graphify import GraphifyDB

logger = logging.getLogger(__name__)

graphify_bp = Blueprint("graphify", __name__)

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

@graphify_bp.route("/api/graphify/stats", methods=["GET"])
def get_stats():
    try:
        workspace_id = request.args.get("workspace_id")
        if not workspace_id:
            return jsonify({"error": "workspace_id is required"}), 400

        stats = GraphifyDB.get_stats(workspace_id)
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
        workspace_id = data.get("workspace_id")
        query = data.get("query", "")
        limit = int(data.get("limit", 20))

        results = GraphifyDB.search(query, workspace_id, limit=limit)
        return jsonify(results), 200
    except Exception as e:
        logger.error(f"Failed to search graphify: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@graphify_bp.route("/api/graphify/main-entities", methods=["GET"])
def get_main_entities():
    try:
        workspace_id = request.args.get("workspace_id")
        limit = int(request.args.get("limit", 30))
        if not workspace_id:
            return jsonify({"error": "workspace_id is required"}), 400

        result = GraphifyDB.get_main_entities(workspace_id, limit=limit)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Failed to get main entities: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@graphify_bp.route("/api/graphify/neighbors", methods=["GET"])
def get_neighbors():
    try:
        workspace_id = request.args.get("workspace_id")
        node_id = request.args.get("node_id")
        if not workspace_id:
            return jsonify({"error": "workspace_id is required"}), 400
        if not node_id:
            return jsonify({"error": "node_id is required"}), 400

        result = GraphifyDB.get_neighbors(workspace_id, node_id)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Failed to get neighbors: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
