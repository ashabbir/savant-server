"""User Management Routes Blueprint for Savant Server."""

from flask import Blueprint, g, jsonify, request
from db.users import UserDB
from db.workspaces import WorkspaceDB
from utils.auth import check_domain_write_access, admin_required

users_bp = Blueprint("users", __name__)


def _require_admin():
    user = UserDB.get_by_id(getattr(g, "user_id", ""))
    if not user or user.get("role") != "admin":
        return jsonify({"error": "Admin access required."}), 403
    return None


@users_bp.route("/api/auth/validate", methods=["GET"])
def auth_validate():
    """Validate an API key. Returns user info if valid."""
    user = UserDB.get_by_id(getattr(g, "user_id", ""))
    if not user:
        return jsonify({"error": "User not found."}), 404
    return jsonify({
        "user_id": user["user_id"],
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "role": user.get("role", "user"),
        "is_active": user.get("is_active", 1),
    })


api_auth_validate = auth_validate


@users_bp.route("/api/users", methods=["GET", "POST"])
def api_users():
    err = _require_admin()
    if err:
        return err

    if request.method == "GET":
        include_inactive = request.args.get("include_inactive", "true").lower() in ("true", "1", "yes")
        users = UserDB.list_all(include_inactive=include_inactive)
        for u in users:
            u.pop("api_key_hash", None)
            u["api_key_masked"] = (u.get("api_key", "")[:7] + "...") if u.get("api_key") else ""
        return jsonify(users)

    data = request.get_json(force=True, silent=True) or {}
    user_id = (data.get("user_id") or "").strip().lower()
    name = (data.get("name") or "").strip()
    if not user_id or not name:
        return jsonify({"error": "user_id and name are required"}), 400

    if UserDB.get_by_id(user_id):
        return jsonify({"error": f"User '{user_id}' already exists"}), 409

    role = data.get("role", "user")
    if role not in ("admin", "operator", "user", "guest"):
        return jsonify({"error": "Invalid role. Must be admin, operator, user, or guest"}), 400

    created = UserDB.create({
        "user_id": user_id,
        "name": name,
        "email": (data.get("email") or "").strip(),
        "role": role,
    })
    return jsonify(created), 201


@users_bp.route("/api/users/<user_id>/domains", methods=["GET", "POST"])
def api_user_domains(user_id):
    user = UserDB.get_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    if request.method == "GET":
        return jsonify(UserDB.get_assigned_domains(user_id))

    err = _require_admin()
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    domain_node_id = (data.get("domain_node_id") or "").strip()
    if not domain_node_id:
        return jsonify({"error": "domain_node_id is required"}), 400

    can_write = bool(data.get("can_write", True))
    res = UserDB.assign_domain(user_id, domain_node_id, can_write=can_write)
    return jsonify(res), 200


@users_bp.route("/api/users/<user_id>/domains/<domain_node_id>", methods=["DELETE"])
def api_user_domain_delete(user_id, domain_node_id):
    err = _require_admin()
    if err:
        return err

    if not UserDB.get_by_id(user_id):
        return jsonify({"error": "User not found"}), 404

    removed = UserDB.remove_domain(user_id, domain_node_id)
    if not removed:
        return jsonify({"error": "Domain assignment not found"}), 404
    return jsonify({"status": "unassigned"}), 200


@users_bp.route("/api/users/<user_id>", methods=["GET", "PUT", "DELETE"])
def api_user_detail(user_id):
    user = UserDB.get_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    current_user_id = getattr(g, "user_id", "")
    if current_user_id != user_id:
        err = _require_admin()
        if err:
            return err

    if request.method == "GET":
        out = dict(user)
        out.pop("api_key_hash", None)
        out["api_key_masked"] = (out.get("api_key", "")[:7] + "...") if out.get("api_key") else ""
        return jsonify(out)

    if request.method == "DELETE":
        if user_id == "ahmed":
            return jsonify({"error": "Cannot delete primary admin account"}), 400
        UserDB.delete(user_id)
        return jsonify({"status": "deleted"}), 200

    data = request.get_json(force=True, silent=True) or {}
    updated = UserDB.update(user_id, data)
    return jsonify(updated)


@users_bp.route("/api/users/<user_id>/workspaces", methods=["GET"])
def api_user_workspaces(user_id):
    if not UserDB.get_by_id(user_id):
        return jsonify({"error": "User not found"}), 404
    all_ws = WorkspaceDB.list_all(user_id=user_id)
    return jsonify(all_ws)


@users_bp.route("/api/users/<user_id>/api-key", methods=["POST"])
def api_user_rotate_api_key(user_id):
    current_user_id = getattr(g, "user_id", "")
    if current_user_id != user_id:
        err = _require_admin()
        if err:
            return err

    updated = UserDB.rotate_api_key(user_id)
    if not updated:
        return jsonify({"error": "User not found"}), 404

    return jsonify({
        "user_id": updated["user_id"],
        "api_key": updated["api_key"],
        "updated_at": updated["updated_at"],
    })
