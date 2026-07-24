import logging
from flask import jsonify, request
from .shared import abilities_bp, _get_store, _get_resolver

logger = logging.getLogger(__name__)


# ── POST /api/abilities/learn — append to ## Learned section ─────────────────

@abilities_bp.route("/api/abilities/learn", methods=["POST"])
def learn():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        asset_id = data.get("asset_id")
        content = data.get("content")
        if not asset_id or not content:
            return jsonify({"error": "asset_id and content required"}), 400

        store = _get_store()
        result = store.append_learned(asset_id, content)
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"learn failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── POST /api/abilities/resolve — resolve prompt from config ─────────────────

@abilities_bp.route("/api/abilities/resolve", methods=["POST"])
def resolve():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        persona = data.get("persona")
        if not persona:
            return jsonify({"error": "persona required"}), 400

        resolver = _get_resolver()
        result = resolver.resolve(
            persona=persona,
            tags=data.get("tags", []),
            repo_id=data.get("repo_id"),
            include_trace=bool(data.get("trace", False)),
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"resolve failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── GET /api/abilities/validate — validate store integrity ───────────────────

@abilities_bp.route("/api/abilities/validate", methods=["GET"])
def validate():
    try:
        store = _get_store()
        store.validate_all()
        return jsonify({"ok": True, "stats": store.stats()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── GET /api/abilities/stats — asset counts by type ──────────────────────────

@abilities_bp.route("/api/abilities/stats", methods=["GET"])
def stats():
    try:
        store = _get_store()
        return jsonify(store.stats())
    except Exception as e:
        logger.error(f"stats failed: {e}")
        return jsonify({"error": str(e)}), 500
