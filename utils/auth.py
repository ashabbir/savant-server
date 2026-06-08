from functools import wraps
from flask import jsonify, g
from db.users import UserDB

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = UserDB.get_by_id(g.user_id)
        if not user or user.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated_function
