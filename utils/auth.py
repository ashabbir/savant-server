from functools import wraps
from flask import jsonify, g, request
from db.users import UserDB

ALLOWED_SAVANT_APPS = {
    "savant-olympus",
    "savant-quorum",
    "savant-sanctum",
    "savant-forge",
    "savant-server",
    "savant-dashboard",
    "savant-client",
    "savant-mcp",
}


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = UserDB.get_by_id(g.user_id)
        if not user or user.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated_function


def require_savant_app(f):
    """Enforce that request comes from an authorized Savant application."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        app_name = (request.headers.get("X-App-Name") or request.headers.get("X-Savant-App") or "").strip().lower()
        if not app_name or app_name not in ALLOWED_SAVANT_APPS:
            return jsonify({
                "error": "Access denied."
            }), 403
        return f(*args, **kwargs)
    return decorated_function


def check_domain_write_access(user_id: str, node_id: str | None = None, is_domain_creation: bool = False) -> tuple[bool, str | None]:
    """Verify if user has write access to a domain hierarchy or is allowed to create domain nodes."""
    user = UserDB.get_by_id(user_id) if user_id else None
    if not user:
        return False, "Authentication required."

    # Rule 1: ONLY admins can create node_type == 'domain'
    if is_domain_creation:
        if user.get("role") == "admin":
            return True, None
        return False, "Access denied. Only admin users can create domain nodes."

    # Rule 2: Admins have unrestricted write access across all domains
    if user.get("role") == "admin":
        return True, None

    # Rule 3: Non-admins editing/deleting an existing node
    if not node_id:
        return True, None

    from db.knowledge_graph import KnowledgeGraphDB
    connected_domains = KnowledgeGraphDB.find_root_domains(node_id)
    if not connected_domains:
        # Unassigned general knowledge node — allow write access
        return True, None

    # User must have write permission (can_write=1) for at least one connected domain
    user_write_map = UserDB.get_assigned_domain_write_map(user_id)
    for domain_id in connected_domains:
        if user_write_map.get(domain_id, False):
            return True, None

    return False, f"Access denied. User '{user_id}' has read-only access for domain hierarchy {list(connected_domains)}."

