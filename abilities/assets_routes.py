import logging
from flask import jsonify, request
from utils.auth import admin_required
from .shared import abilities_bp, _get_store

logger = logging.getLogger(__name__)


# ── GET /api/abilities/assets — list all assets grouped by type ───────────────

@abilities_bp.route("/api/abilities/assets", methods=["GET"])
def list_assets():
    try:
        store = _get_store()
        return jsonify(store.list_assets_grouped())
    except Exception as e:
        logger.error(f"list_assets failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── GET /api/abilities/assets/<id> — get single asset ─────────────────────────

@abilities_bp.route("/api/abilities/assets/<path:asset_id>", methods=["GET"])
def get_asset(asset_id: str):
    try:
        store = _get_store()
        asset = store.get_asset_dict(asset_id)
        if not asset:
            return jsonify({"error": f"Asset '{asset_id}' not found"}), 404
        return jsonify(asset)
    except Exception as e:
        logger.error(f"get_asset failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── POST /api/abilities/assets — create new asset ────────────────────────────

@abilities_bp.route("/api/abilities/assets", methods=["POST"])
@admin_required
def create_asset():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        required = ["id", "type", "tags", "priority"]
        for field in required:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400

        store = _get_store()
        result = store.create_asset(
            asset_type=data["type"],
            asset_id=data["id"],
            tags=data["tags"],
            priority=int(data["priority"]),
            body=data.get("body", ""),
            includes=data.get("includes"),
            name=data.get("name"),
            aliases=data.get("aliases"),
        )
        return jsonify(result), 201
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        logger.error(f"create_asset failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── PUT /api/abilities/assets/<id> — update existing asset ───────────────────

@abilities_bp.route("/api/abilities/assets/<path:asset_id>", methods=["PUT"])
@admin_required
def update_asset(asset_id: str):
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        store = _get_store()
        result = store.update_asset(
            asset_id=asset_id,
            tags=data.get("tags"),
            priority=int(data["priority"]) if "priority" in data else None,
            body=data.get("body"),
            includes=data.get("includes"),
            name=data.get("name"),
            aliases=data.get("aliases"),
        )
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"update_asset failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── DELETE /api/abilities/assets/<id> — delete asset ─────────────────────────

@abilities_bp.route("/api/abilities/assets/<path:asset_id>", methods=["DELETE"])
@admin_required
def delete_asset(asset_id: str):
    try:
        store = _get_store()
        store.delete_asset(asset_id)
        return jsonify({"ok": True, "deleted": asset_id})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"delete_asset failed: {e}")
        return jsonify({"error": str(e)}), 500
