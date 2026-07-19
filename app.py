import os
import json
import re
import time
import asyncio
import uuid
import logging
import hashlib
import sys
from collections import Counter
from threading import Lock
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, g, jsonify, request, abort
from flask_cors import CORS
from postgres_client import get_connection, release_connection, init_schema, close_pool
from db.workspaces import WorkspaceDB
from db.workspace_session_links import WorkspaceSessionLinkDB
from db.tasks import TaskDB
from db.notes import NoteDB
from db.merge_requests import MergeRequestDB
from db.jira_tickets import JiraTicketDB
from db.notifications import NotificationDB
from db.users import UserDB
from hardening import rate_limit, validate_request, safe_limit, check_rate_limit, sanitize_text
from utils.auth import ALLOWED_SAVANT_APPS
from abilities.routes import abilities_bp
from abilities.bootstrap import abilities_bootstrap_status
from context.routes import context_bp
from knowledge.routes import knowledge_bp
from tools.routes import tools_bp
from reminders.routes import reminders_bp
from graphify.routes import graphify_bp
from code_intelligence.routes import code_intelligence_bp
from server_paths import (
    get_server_data_dir, 
    get_server_db_path, 
    get_server_abilities_base_dir,
    container_to_host_path,
    host_to_container_path
)

app = Flask(__name__)
# Enable CORS for all routes, allowing Savant authentication headers for preflight requests.
CORS(
    app,
    resources={r"/*": {"origins": "*"}},
    allow_headers=["Content-Type", "X-API-Key", "X-App-Name", "X-Savant-App", "Authorization"],
)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB request body limit
_API_ONLY_MODE = os.environ.get("SAVANT_API_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize database on startup
with app.app_context():
    try:
        init_schema()
    except Exception as e:
        logger.critical("Database initialization failed; refusing to start: %s", e)
        raise

# ── Global Error Handlers ─────────────────────────────────────────────────────
@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found", "path": request.path}), 404
    return e

@app.errorhandler(413)
def handle_413(e):
    return jsonify({"error": "Request body too large (max 100 MB)"}), 413

@app.errorhandler(429)
def handle_429(e):
    return jsonify({"error": "Rate limit exceeded"}), 429

@app.errorhandler(500)
def handle_500(e):
    logger.error(f"Internal server error: {e}")
    return jsonify({"error": "Internal server error"}), 500


# ── Hardening helpers ─────────────────────────────────────────────────────────
def _safe_json():
    """Parse request JSON safely, returning ({}, error_response) on failure."""
    try:
        data = request.get_json(force=True, silent=True)
        if data is None:
            return {}, None
        if not isinstance(data, dict):
            return None, (jsonify({"error": "Request body must be a JSON object"}), 400)
        return data, None
    except Exception:
        return None, (jsonify({"error": "Invalid JSON in request body"}), 400)

def _str_field(data, key, max_len=2000, default=""):
    """Safely extract a string field with length cap."""
    val = data.get(key, default)
    if val is None:
        return default
    if not isinstance(val, str):
        val = str(val)
    return val.strip()[:max_len]


# Register abilities API blueprint
app.register_blueprint(abilities_bp)

# Register context API blueprint
app.register_blueprint(context_bp)

# Register knowledge API blueprint
app.register_blueprint(knowledge_bp)

# Register tools API blueprint
app.register_blueprint(tools_bp)

# Register reminders API blueprint
app.register_blueprint(reminders_bp)

# Register graphify API blueprint
app.register_blueprint(graphify_bp)
app.register_blueprint(code_intelligence_bp)

# Register skills API blueprint
from abilities.skills_routes import skills_bp
app.register_blueprint(skills_bp)

# Seed default users on startup (idempotent — safe for gunicorn + dev)
try:
    UserDB.seed_defaults()
except Exception as _seed_err:
    logger.warning(f"Could not seed default users: {_seed_err}")

# ── Auth middleware — resolve API key → g.user_id ────────────────────────────
# Endpoints that skip authentication (health, system, static, users management)
_AUTH_SKIP_PREFIXES = (
    "/api/db/health", "/api/system/", "/api/mcp/",
    "/health/", "/static/",
)

@app.before_request
def _authenticate():
    """Resolve API key (header/query) → g.user_id. Skip for health/system endpoints."""
    # Skip auth for CORS preflight
    if request.method == "OPTIONS":
        return None

    p = request.path or "/"
    # Skip auth for non-API or whitelisted paths
    if not p.startswith("/api/"):
        g.user_id = ""
        return None
    for prefix in _AUTH_SKIP_PREFIXES:
        if p.startswith(prefix):
            g.user_id = ""
            return None
    # Accept header first, then query fallback for MCP SSE URL flows.
    # Header remains the canonical path for regular API callers.
    api_key = (
        request.headers.get("X-API-Key", "").strip()
        or request.args.get("api_key", "").strip()
    )
    if not api_key:
        return jsonify({"error": "API key required. Set X-API-Key header or api_key query param."}), 401
    user = UserDB.get_by_api_key(api_key)
    if not user:
        return jsonify({"error": "Invalid API key."}), 401
    if int(user.get("is_active", 1)) != 1:
        return jsonify({"error": "User account is inactive."}), 401
    g.user_id = user["user_id"]
    return None


@app.before_request
def _require_allowed_savant_app():
    """Require every API caller to identify an allowed Savant application."""
    if request.method == "OPTIONS" or not (request.path or "/").startswith("/api/"):
        return None

    app_name = (
        request.headers.get("X-App-Name")
        or request.headers.get("X-Savant-App")
        or ""
    ).strip().lower()
    if not app_name or app_name not in ALLOWED_SAVANT_APPS:
        return jsonify({"error": "Access denied."}), 403
    return None


# ── Auth validation endpoint ──────────────────────────────────────────────────
@app.route("/api/auth/validate", methods=["GET"])
def auth_validate():
    """Validate an API key. Returns user info if valid, 401 if not.
    This endpoint is NOT in _AUTH_SKIP_PREFIXES so it goes through auth middleware."""
    from db.users import UserDB
    user = UserDB.get_by_id(g.user_id)
    return jsonify({
        "valid": True,
        "user_id": g.user_id,
        "role": user.get("role", "user") if user else "user",
        "name": user.get("name", "") if user else "",
    })


def _require_admin():
    """Return a Flask error response when current user is not an active admin."""
    user = UserDB.get_by_id(g.user_id)
    if not user:
        return jsonify({"error": "User not found."}), 401
    if int(user.get("is_active", 1)) != 1:
        return jsonify({"error": "User account is inactive."}), 401
    if user.get("role") != "admin":
        return jsonify({"error": "Admin role required."}), 403
    return None


@app.route("/api/users", methods=["GET", "POST"])
def api_users():
    admin_err = _require_admin()
    if admin_err:
        return admin_err

    if request.method == "GET":
        include_inactive = (request.args.get("include_inactive", "true").strip().lower() != "false")
        return jsonify(UserDB.list_all(include_inactive=include_inactive))

    data, err = _safe_json()
    if err:
        return err
    user_id = _str_field(data, "user_id", max_len=200)
    name = _str_field(data, "name", max_len=200)
    if not user_id or not name:
        return jsonify({"error": "user_id and name required"}), 400
    payload = {
        "user_id": user_id,
        "name": name,
        "email": _str_field(data, "email", max_len=300),
        "role": _str_field(data, "role", max_len=50) or "user",
        "is_active": 1,
    }
    if _str_field(data, "api_key", max_len=500):
        payload["api_key"] = _str_field(data, "api_key", max_len=500)
    try:
        created = UserDB.create(payload)
    except Exception as e:
        return jsonify({"error": f"Failed to create user: {e}"}), 400
    return jsonify(created), 201


# ── User Domain Assignments ───────────────────────────────────────────────
@app.route("/api/users/<user_id>/domains", methods=["GET", "POST"])
def api_user_domains(user_id):
    existing = UserDB.get_by_id(user_id)
    if not existing:
        return jsonify({"error": "User not found"}), 404

    if request.method == "GET":
        # Allow admins or the user themselves to view assigned domains
        current_user = UserDB.get_by_id(g.user_id)
        if current_user and (current_user.get("role") == "admin" or current_user.get("user_id") == user_id):
            assigned = UserDB.get_assigned_domains(user_id)
            return jsonify({"user_id": user_id, "domains": assigned})
        return jsonify({"error": "Access denied"}), 403

    # POST requires admin role
    admin_err = _require_admin()
    if admin_err:
        return admin_err

    data, err = _safe_json()
    if err:
        return err

    domain_node_id = (data.get("domain_node_id") or "").strip()
    can_write = bool(data.get("can_write", True))
    if not domain_node_id:
        return jsonify({"error": "domain_node_id required"}), 400

    assigned = UserDB.assign_domain(user_id, domain_node_id, can_write=can_write)
    return jsonify(assigned), 200


@app.route("/api/users/<user_id>/domains/<domain_node_id>", methods=["DELETE"])
def api_user_domain_delete(user_id, domain_node_id):
    admin_err = _require_admin()
    if admin_err:
        return admin_err

    existing = UserDB.get_by_id(user_id)
    if not existing:
        return jsonify({"error": "User not found"}), 404

    removed = UserDB.remove_domain(user_id, domain_node_id)
    return jsonify({"user_id": user_id, "domain_node_id": domain_node_id, "removed": removed})


@app.route("/api/users/<user_id>", methods=["GET", "PUT", "DELETE"])
def api_user_by_id(user_id):
    admin_err = _require_admin()
    if admin_err:
        return admin_err

    existing = UserDB.get_by_id(user_id)
    if not existing:
        return jsonify({"error": "User not found"}), 404

    if request.method == "GET":
        return jsonify(existing)

    if request.method == "PUT":
        data, err = _safe_json()
        if err:
            return err
        updates = {}
        if "name" in data:
            updates["name"] = _str_field(data, "name", max_len=200)
        if "email" in data:
            updates["email"] = _str_field(data, "email", max_len=300)
        if "role" in data:
            updates["role"] = _str_field(data, "role", max_len=50)
        if "is_active" in data:
            updates["is_active"] = 1 if bool(data.get("is_active")) else 0
        updated = UserDB.update(user_id, updates)
        return jsonify(updated)

    deactivated = UserDB.deactivate(user_id)
    return jsonify({"deactivated": True, "user": deactivated})


@app.route("/api/users/<user_id>/workspaces", methods=["GET"])
def api_user_workspaces(user_id):
    admin_err = _require_admin()
    if admin_err:
        return admin_err
    return jsonify({
        "user_id": user_id,
        "workspaces": WorkspaceDB.list_all(limit=1000, user_id=user_id),
    })


@app.route("/api/users/<user_id>/api-key", methods=["POST"])
def api_user_rotate_api_key(user_id):
    admin_err = _require_admin()
    if admin_err:
        return admin_err
    updated = UserDB.rotate_api_key(user_id)
    if not updated:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "user_id": updated["user_id"],
        "api_key": updated["api_key"],
    })


# ── Job queue routes ──────────────────────────────────────────────────────────
VALID_JOB_TYPES = {"index", "reindex", "ast", "index-all", "ast-all"}


@app.route("/api/jobs/submit", methods=["POST"])
def job_submit():
    """Submit a job to the queue. Deduplicates active jobs."""
    data, err = _safe_json()
    if err:
        return err
    job_type = _str_field(data, "job_type", 50)
    target = _str_field(data, "target", 200)
    if not job_type or not target:
        return jsonify({"error": "job_type and target required"}), 400
    if job_type not in VALID_JOB_TYPES:
        return jsonify({"error": f"Invalid job_type. Must be one of: {', '.join(sorted(VALID_JOB_TYPES))}"}), 400

    from db.jobs import JobDB
    existing = JobDB.find_active(job_type, target)
    if existing:
        return jsonify({"job_id": existing["id"], "status": existing["status"],
                        "reused": True})

    job = JobDB.create_job(job_type, target)
    return jsonify({"job_id": job["id"], "status": "queued"})


@app.route("/api/jobs/status")
def job_status():
    """Get status of a single job by id."""
    job_id = request.args.get("id", "").strip()
    if not job_id:
        return jsonify({"error": "id parameter required"}), 400
    from db.jobs import JobDB
    job = JobDB.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/jobs/list")
def job_list():
    """List jobs with optional filters."""
    status = request.args.get("status", "").strip() or None
    target = request.args.get("target", "").strip() or None
    limit = min(int(request.args.get("limit", "20")), 100)
    from db.jobs import JobDB
    jobs = JobDB.list_jobs(status=status, target=target, limit=limit)
    return jsonify({"jobs": jobs, "count": len(jobs)})


@app.route("/api/jobs/cancel", methods=["POST"])
def job_cancel():
    """Request cancellation of a job."""
    data, err = _safe_json()
    if err:
        return err
    job_id = _str_field(data, "job_id", 100)
    if not job_id:
        return jsonify({"error": "job_id required"}), 400
    from db.jobs import JobDB
    ok = JobDB.request_cancel(job_id)
    return jsonify({"cancelled": ok, "job_id": job_id})


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def job_delete(job_id):
    """Delete a finished/failed/cancelled job."""
    from db.jobs import JobDB
    ok = JobDB.delete_job(job_id)
    return jsonify({"deleted": ok, "job_id": job_id})


# Start job worker on first request (lazy — avoids import issues during testing)
_job_worker_initialized = False


@app.before_request
def _ensure_job_worker():
    global _job_worker_initialized
    if not _job_worker_initialized:
        _job_worker_initialized = True
        try:
            from context.job_worker import start_worker
            start_worker()
        except Exception as e:
            logger.warning(f"Job worker start failed (non-fatal): {e}")

# ── Global rate-limit on all mutating API endpoints ───────────────────────────
@app.before_request
def _global_rate_limit():
    if request.path.startswith("/api/") and request.method in ("POST", "PUT", "DELETE"):
        ip = request.remote_addr or "unknown"
        allowed, error_msg = check_rate_limit(ip, max_requests=60, window_seconds=60)
        if not allowed:
            return jsonify({"error": error_msg}), 429

# SQLite connection is managed by singleton — no per-request teardown needed


def _read_preferences():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM preferences WHERE key = %s", ("__all__",))
            row = cur.fetchone()
        if not row or not row["value"]:
            return {}
        return json.loads(row["value"])
    except Exception:
        return {}
    finally:
        release_connection(conn)


def _write_preferences(prefs):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO preferences (key, value) VALUES (%s, %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                ("__all__", json.dumps(prefs)),
            )
        conn.commit()
    finally:
        release_connection(conn)


@app.after_request
def add_no_cache(response):
    ct = response.content_type or ''
    if 'text/html' in ct or 'javascript' in ct or 'text/css' in ct:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.before_request
def enforce_api_only_mode():
    if not _API_ONLY_MODE:
        return None
    p = request.path or "/"
    if p.startswith("/api/") or p in {"/version", "/health/live", "/health/ready"}:
        return None
    abort(404)


@app.route("/api/db/health")
def api_db_health():
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return jsonify({"status": "healthy", "connected": True})
    except Exception as e:
        return jsonify({"status": "unhealthy", "connected": False, "error": str(e)}), 503
    finally:
        if conn:
            release_connection(conn)


@app.route("/api/system/info")
def api_system_info():
    from postgres_client import get_connection, release_connection

    def _mcp_entry(name, default_port):
        port_env = os.environ.get(f"SAVANT_MCP_{name.upper()}_PORT")
        try:
            port = int(port_env) if port_env else default_port
        except Exception:
            port = default_port
        url = os.environ.get(f"SAVANT_MCP_{name.upper()}_URL", f"http://127.0.0.1:{port}/sse")
        # Probe the MCP server to check if it's actually running
        status = "offline"
        try:
            import requests as _req
            r = _req.get(url, timeout=2, stream=True)
            if r.status_code == 200:
                status = "ok"
            r.close()
        except Exception:
            pass
        return {
            "url": url,
            "port": port,
            "status": status,
        }

    db_path = "PostgreSQL"
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        db_ok = True
        db_error = ""
    except Exception as e:
        db_ok = False
        db_error = str(e)
    finally:
        if conn:
            release_connection(conn)

    build_info_path = Path(__file__).resolve().parent / "build-info.json"
    build_info = {}
    if build_info_path.exists():
        try:
            build_info = json.loads(build_info_path.read_text(encoding="utf-8"))
        except Exception:
            build_info = {}

    return jsonify({
        "version": build_info.get("version") or "unknown",
        "flask": {
            "status": "ok",
            "port": int(os.environ.get("FLASK_PORT", "8090")),
        },
        "build": {
            "version": build_info.get("version") or "unknown",
            "branch": build_info.get("branch") or "unknown",
            "commit": build_info.get("commit") or "",
            "worktree": build_info.get("worktree"),
            "built_at": build_info.get("built_at"),
        },
        "mcp_servers": {
            "workspace": _mcp_entry("workspace", int(os.environ.get("SAVANT_MCP_WORKSPACE_PORT", "8091"))),
            "abilities": _mcp_entry("abilities", int(os.environ.get("SAVANT_MCP_ABILITIES_PORT", "8092"))),
            "context": _mcp_entry("context", int(os.environ.get("SAVANT_MCP_CONTEXT_PORT", "8093"))),
            "knowledge": _mcp_entry("knowledge", int(os.environ.get("SAVANT_MCP_KNOWLEDGE_PORT", "8094"))),
            "reminders": _mcp_entry("reminders", int(os.environ.get("SAVANT_MCP_REMINDERS_PORT", "8095"))),
        },
        "blueprints": [
            "abilities",
            "context",
            "knowledge",
            "workspaces",
            "tasks",
        ],
        "context_sources": {
            "enabled": {
                "GITHUB_TOKEN": bool(os.environ.get("GITHUB_TOKEN")),
                "GITLAB_TOKEN": bool(os.environ.get("GITLAB_TOKEN")),
                "BASE_CODE_DIR": bool(os.environ.get("BASE_CODE_DIR")),
            },
            "any_enabled": bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GITLAB_TOKEN") or os.environ.get("BASE_CODE_DIR")),
            "missing": [k for k in ("GITHUB_TOKEN", "GITLAB_TOKEN", "BASE_CODE_DIR") if not os.environ.get(k)],
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
        },
        "directories": {
            "savant_server": str(Path(__file__).resolve().parent),
            "data_dir": str(get_server_data_dir()),
            "abilities_dir": str(get_server_abilities_base_dir()),
        },
        "database": {
            "status": "healthy" if db_ok else "unhealthy",
            "size_bytes": Path(db_path).stat().st_size if Path(db_path).exists() else 0,
            "path": db_path,
            "error": db_error,
        },
        "abilities": abilities_bootstrap_status(),
    })


def _read_task_ended_days() -> list[str]:
    prefs = _read_preferences()
    days = prefs.get("task_ended_days", [])
    return days if isinstance(days, list) else []


def _write_task_ended_days(days: list[str]) -> None:
    prefs = _read_preferences()
    prefs["task_ended_days"] = sorted({d for d in days if isinstance(d, str) and d})
    _write_preferences(prefs)


@app.route("/api/tasks", methods=["GET", "POST"])
def api_tasks():
    if request.method == "GET":
        workspace_id = request.args.get("workspace_id") or None
        status = request.args.get("status") or None
        date = request.args.get("date") or None

        if workspace_id:
            tasks = TaskDB.list_by_workspace(workspace_id, status=status, limit=1000, user_id=g.user_id)
        elif status and status != "all":
            tasks = TaskDB.list_by_status(status, limit=1000, user_id=g.user_id)
        elif date:
            tasks = TaskDB.list_by_date(date, user_id=g.user_id)
        else:
            tasks = TaskDB.list_all(user_id=g.user_id)
        return jsonify(tasks)

    data, err = _safe_json()
    if err:
        return err
    title = _str_field(data, "title", max_len=500)
    workspace_id = _str_field(data, "workspace_id", max_len=100)
    if not title or not workspace_id:
        return jsonify({"error": "title and workspace_id required"}), 400

    _valid_statuses = {"todo", "in-progress", "done", "blocked"}
    _valid_priorities = {"critical", "high", "medium", "low"}
    status = _str_field(data, "status", max_len=20) or "todo"
    priority = _str_field(data, "priority", max_len=20) or "medium"
    if status not in _valid_statuses:
        status = "todo"
    if priority not in _valid_priorities:
        priority = "medium"

    payload = {
        "task_id": _str_field(data, "task_id", max_len=100) or f"task_{uuid.uuid4().hex[:12]}",
        "workspace_id": workspace_id,
        "title": title,
        "description": _str_field(data, "description", max_len=5000),
        "status": status,
        "priority": priority,
        "priority": data.get("priority", "medium"),
        "date": data.get("date") or request.args.get("date"),
        "order": data.get("order", 0),
        "created_session_id": data.get("created_session_id"),
        "depends_on": data.get("depends_on", []) or [],
        "user_id": g.user_id,
    }
    task = TaskDB.create(payload)
    return jsonify(task)


@app.route("/api/tasks/<task_id>", methods=["GET", "PUT", "DELETE"])
def api_task_by_id(task_id):
    task = TaskDB.get_by_id(task_id, user_id=g.user_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    if request.method == "GET":
        return jsonify(task)

    if request.method == "PUT":
        data = request.get_json(force=True, silent=True) or {}
        updated = TaskDB.update(task_id, data, user_id=g.user_id)
        if not updated:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(updated)

    deleted = TaskDB.delete(task_id, user_id=g.user_id)
    if not deleted:
        return jsonify({"error": "Task not found"}), 404
    return jsonify({"ok": True, "deleted": task_id})


@app.route("/api/tasks/<task_id>/deps", methods=["POST"])
def api_task_add_dependency(task_id):
    task = TaskDB.get_by_id(task_id, user_id=g.user_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    data = request.get_json(force=True, silent=True) or {}
    depends_on = (data.get("depends_on") or "").strip()
    if not depends_on:
        return jsonify({"error": "depends_on required"}), 400

    dep_task = TaskDB.get_by_id(depends_on, user_id=g.user_id)
    if not dep_task:
        return jsonify({"error": "Dependency task not found"}), 404

    ok = TaskDB.add_dependency(task_id, depends_on)
    if not ok:
        return jsonify({"error": "Could not add dependency"}), 500
    return jsonify(TaskDB.get_by_id(task_id, user_id=g.user_id))


@app.route("/api/tasks/<task_id>/deps/<depends_on>", methods=["DELETE"])
def api_task_remove_dependency(task_id, depends_on):
    task = TaskDB.get_by_id(task_id, user_id=g.user_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    dep_task = TaskDB.get_by_id(depends_on, user_id=g.user_id)
    if not dep_task:
        return jsonify({"error": "Dependency task not found"}), 404

    ok = TaskDB.remove_dependency(task_id, depends_on)
    if not ok:
        return jsonify({"error": "Dependency link not found"}), 404
    return jsonify(TaskDB.get_by_id(task_id, user_id=g.user_id))


@app.route("/api/tasks/ended-days", methods=["GET"])
def api_tasks_ended_days():
    return jsonify(_read_task_ended_days())


@app.route("/api/tasks/graph", methods=["GET"])
def api_tasks_graph():
    workspace_id = (request.args.get("workspace_id") or "").strip()
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400

    tasks = TaskDB.list_by_workspace(workspace_id, limit=1000, user_id=g.user_id)
    nodes = []
    edges = []
    for task in tasks:
        task_id = task.get("task_id") or task.get("id")
        if not task_id:
          continue
        nodes.append({
            "id": task_id,
            "title": task.get("title") or task_id,
            "description": task.get("description") or "",
            "status": task.get("status") or "todo",
            "priority": task.get("priority") or "medium",
            "date": task.get("date"),
            "created_at": task.get("created_at"),
            "depends_on": list(task.get("depends_on") or []),
        })
        for dep_id in task.get("depends_on") or []:
            edges.append({"from": task_id, "to": dep_id})

    return jsonify({"workspace_id": workspace_id, "nodes": nodes, "edges": edges})


@app.route("/api/tasks/jira", methods=["GET"])
def api_tasks_jira():
    workspace_id = (request.args.get("workspace_id") or "").strip()
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    tickets = JiraTicketDB.list_by_workspace(workspace_id, limit=1000, user_id=g.user_id)
    return jsonify(tickets)


@app.route("/api/merge-requests", methods=["GET", "POST"])
def api_merge_requests():
    if request.method == "POST":
        data = request.get_json(force=True)
        if not data.get("url"):
            return jsonify({"error": "url required"}), 400
        data.setdefault("workspace_id", "")
        mr_id = data.get("mr_id") or f"mr_{uuid.uuid4().hex[:16]}"
        data["mr_id"] = mr_id
        data["user_id"] = g.user_id
        # Auto-parse project_id/mr_iid from URL if not provided
        if not data.get("project_id"):
            pid, iid = _parse_mr_url(data["url"])
            if pid:
                data["project_id"] = pid
                data["mr_iid"] = int(iid) if iid else 0
        try:
            mr = MergeRequestDB.create(data)
            return jsonify(mr), 201
        except Exception as e:
            if "UNIQUE" in str(e):
                return jsonify({"status": "duplicate", "mr_id": mr_id}), 200
            logger.error(f"Error creating MR: {e}")
            return jsonify({"error": str(e)}), 500
    session_id = (request.args.get("session_id") or "").strip()
    if session_id:
        merge_requests = MergeRequestDB.list_full_by_session(session_id, user_id=g.user_id)
        return jsonify(merge_requests)
    workspace_id = (request.args.get("workspace_id") or "").strip()
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    merge_requests = MergeRequestDB.list_by_workspace(workspace_id, limit=1000, user_id=g.user_id)
    return jsonify(merge_requests)


def _next_available_workday(start_date_str: str, ended_set: set, work_week: list[int]) -> str:
    """Find the next day strictly after *start_date_str* that is BOTH
    a configured working day AND not in the ended_days set.

    work_week is the user-pref weekday integer list (Python convention:
    Mon=0..Sun=6; we also accept the JS convention Sun=0..Sat=6 and map it).
    Falls back to Mon-Fri when work_week is empty/unset. Bounded scan of
    400 days so a malformed config can't loop forever.
    """
    # Normalize work-week to Python's Monday=0..Sunday=6 convention.
    # JS sends Sunday=0..Saturday=6 → shift by -1 and modulo 7. We accept both:
    # if every entry is in [1..7] we assume Mon=1..Sun=7 (alternate scheme).
    if not work_week:
        py_work = {0, 1, 2, 3, 4}  # default Mon–Fri
    else:
        ints = [int(d) for d in work_week if isinstance(d, (int, float)) or (isinstance(d, str) and d.isdigit())]
        if ints and max(ints) <= 6 and 0 in ints:
            # JS-style Sun=0..Sat=6 → map to Python Mon=0..Sun=6.
            # JS 0=Sun→Py 6, JS 1=Mon→Py 0, JS 2=Tue→Py 1, etc.
            py_work = {(d - 1) % 7 for d in ints}
        elif ints and min(ints) >= 1 and max(ints) <= 7:
            # 1=Mon..7=Sun → Py 0..6.
            py_work = {(d - 1) for d in ints}
        else:
            py_work = {(d - 1) % 7 for d in ints}
        if not py_work:
            py_work = {0, 1, 2, 3, 4}
    try:
        d = datetime.fromisoformat(start_date_str + "T00:00:00").date()
    except Exception:
        return start_date_str
    for _ in range(400):
        d = d + timedelta(days=1)
        iso = d.isoformat()
        if d.weekday() in py_work and iso not in ended_set:
            return iso
    # Last resort: just return start + 1 so the caller still has a date.
    return (datetime.fromisoformat(start_date_str + "T00:00:00").date() + timedelta(days=1)).isoformat()


@app.route("/api/tasks/end-day", methods=["POST"])
def api_tasks_end_day():
    """Close *date_str* AND every prior day that has any task on it.

    "Next available day" is the next future date that is BOTH:
      - in the user's work-week preference (defaults to Mon–Fri), AND
      - not already in the ended_days set.
    All un-done tasks on or before *date_str* roll forward to that day.
    Every distinct date with any task on/before *date_str* is marked
    ended — even days that only have done tasks left over from a prior
    cascade, so the calendar stops nagging the user with stale "open"
    badges.
    """
    data = request.get_json(force=True, silent=True) or {}
    date_str = (data.get("date") or "").strip()
    if not date_str:
        return jsonify({"error": "date required"}), 400
    try:
        datetime.fromisoformat(date_str + "T00:00:00")
    except Exception:
        return jsonify({"error": "invalid date"}), 400

    # Compute the target "next available day" relative to date_str. We must
    # exclude date_str itself from ended_set when computing this because
    # we're about to close it — but we DO want subsequent already-closed
    # days to be skipped.
    ended_days = _read_task_ended_days()
    ended_set = set(ended_days)
    prefs = _read_preferences()
    work_week = prefs.get("work_week", [1, 2, 3, 4, 5])
    to_date = _next_available_workday(date_str, ended_set, work_week)

    # Snapshot every date that has ANY task on/before date_str BEFORE the
    # move clears those dates. This is what the user means by "close all
    # previous days": a day that's been touched at all should show as
    # closed once they wrap up — including days that only have done
    # tasks left over from a prior cascade.
    touched_dates = TaskDB.distinct_task_dates_on_or_before(
        date_str, user_id=g.user_id
    )

    moved, source_dates = TaskDB.move_incomplete_tasks_on_or_before(
        date_str, to_date, user_id=g.user_id
    )
    closed_dates: list[str] = []
    for d in (*touched_dates, date_str):
        if d and d not in ended_set:
            ended_set.add(d)
            closed_dates.append(d)
    _write_task_ended_days(list(ended_set))

    return jsonify({
        "ok": True,
        "from": date_str,
        "to": to_date,
        "moved": moved,
        "closed_dates": sorted(set(closed_dates) | {date_str}),
        "source_dates": source_dates,
        "touched_dates": touched_dates,
    })


@app.route("/api/tasks/unend-day", methods=["POST"])
def api_tasks_unend_day():
    data = request.get_json(force=True, silent=True) or {}
    date_str = (data.get("date") or "").strip()
    if not date_str:
        return jsonify({"error": "date required"}), 400
    ended_days = [d for d in _read_task_ended_days() if d != date_str]
    _write_task_ended_days(ended_days)
    return jsonify({"ok": True, "date": date_str})


_MCP_DEFAULT_PORTS = {
    "workspace": 8091,
    "abilities": 8092,
    "context": 8093,
    "knowledge": 8094,
    "reminders": 8095,
}


@app.route("/api/mcp/health/<name>")
def api_mcp_health(name):
    """Probe the actual MCP SSE port to verify the process is alive."""
    port_env = os.environ.get(f"SAVANT_MCP_{name.upper()}_PORT")
    default_port = _MCP_DEFAULT_PORTS.get(name, 0)
    try:
        port = int(port_env) if port_env else default_port
    except Exception:
        port = default_port
    if not port:
        return jsonify({"status": "error", "name": name, "error": "unknown MCP server"}), 404
    url = f"http://127.0.0.1:{port}/sse"
    try:
        import requests as _req
        r = _req.get(url, timeout=2, stream=True)
        alive = r.status_code == 200
        r.close()
    except Exception:
        alive = False
    if alive:
        return jsonify({"status": "ok", "name": name, "port": port})
    return jsonify({"status": "error", "name": name, "port": port, "error": "not reachable"}), 503


@app.route("/api/mcp/health")
def api_mcp_health_all():
    """Probe all known MCP servers and return aggregated status."""
    results = {}
    import requests as _req
    for name, default_port in _MCP_DEFAULT_PORTS.items():
        port_env = os.environ.get(f"SAVANT_MCP_{name.upper()}_PORT")
        try:
            port = int(port_env) if port_env else default_port
        except Exception:
            port = default_port
        url = f"http://127.0.0.1:{port}/sse"
        try:
            r = _req.get(url, timeout=2, stream=True)
            alive = r.status_code == 200
            r.close()
        except Exception:
            alive = False
        results[name] = {"status": "ok" if alive else "error", "port": port}
    return jsonify(results)


@app.route("/api/mcp")
def api_mcp():
    info = api_system_info().get_json() if hasattr(api_system_info(), "get_json") else {}
    return jsonify({
        "servers": [
            {
                "name": name,
                "type": "sse",
                "command": "savant-server",
                "args": [],
                "tools": [],
                "port": details.get("port"),
                "url": details.get("url"),
            }
            for name, details in (info.get("mcp_servers") or {}).items()
        ],
    })


async def _fetch_mcp_tools(url: str) -> list[dict]:
    """Connect to a live MCP SSE server and return its tool inventory."""
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [tool.model_dump(mode="json", exclude_none=True) for tool in (result.tools or [])]


def _list_mcp_tools(server_name: str | None = None) -> list[dict]:
    info = api_system_info().get_json() if hasattr(api_system_info(), "get_json") else {}
    servers = (info.get("mcp_servers") or {}).items()
    if server_name:
        server_name = server_name.strip().lower()
        servers = [(name, details) for name, details in servers if name == server_name]

    async def _gather():
        async def _one(name: str, details: dict) -> dict:
            url = details.get("url") or ""
            status = details.get("status") or "offline"
            port = details.get("port")
            try:
                tools = await _fetch_mcp_tools(url)
                status = "ok"
                return {
                    "name": name,
                    "url": url,
                    "port": port,
                    "status": status,
                    "tool_count": len(tools),
                    "tools": tools,
                }
            except Exception as exc:
                return {
                    "name": name,
                    "url": url,
                    "port": port,
                    "status": "error",
                    "error": str(exc),
                    "tools": [],
                }

        return await asyncio.gather(*(_one(name, details) for name, details in servers))

    return asyncio.run(_gather())


@app.route("/api/mcp/tools")
def api_mcp_tools():
    server_name = request.args.get("server", "").strip() or None
    return jsonify({
        "servers": _list_mcp_tools(server_name),
    })


@app.route("/api/mcp/tools/<server_name>")
def api_mcp_tool(server_name: str):
    server_name = str(server_name or "").strip()
    if not server_name:
        return jsonify({"error": "server_name is required"}), 400
    servers = _list_mcp_tools(server_name)
    if not servers:
        return jsonify({"error": "MCP server not found", "server": server_name}), 404
    return jsonify({"server": servers[0]})


@app.route("/api/check-mcp")
def api_check_mcp():
    providers = ["copilot", "claude", "gemini", "codex", "savant"]
    prefs = _read_preferences()
    enabled = set(prefs.get("enabled_providers") or providers)
    return jsonify({
        provider: {
            "label": provider,
            "config_exists": True,
            "savant_configured": provider in enabled,
        }
        for provider in providers
    })


@app.route("/api/setup-mcp", methods=["POST"])
def api_setup_mcp():
    data = request.get_json(force=True, silent=True) or {}
    providers = data.get("providers") or [data.get("provider")]
    providers = [str(p).strip().lower() for p in providers if str(p).strip()]
    if not providers:
        return jsonify({"results": [], "summary": {"configured": 0, "skipped": 0, "errors": 0}})
    return jsonify({
        "results": [
            {
                "provider": p,
                "label": p,
                "status": "skipped",
                "reason": "Desktop config editing is only available in the Electron client",
            }
            for p in providers
        ],
        "summary": {"configured": 0, "skipped": len(providers), "errors": 0},
    }), 501


def _empty_usage_payload(provider: str):
    return {
        "provider": provider,
        "loading": False,
        "models": [],
        "tools": [],
        "daily": [],
        "totals": {
            "sessions": 0,
            "messages": 0,
            "turns": 0,
            "tool_calls": 0,
            "total_hours": 0,
            "avg_session_minutes": 0,
            "avg_tools_per_turn": 0,
            "avg_turns_per_message": 0,
            "events": 0,
        },
    }


@app.route("/api/usage")
def api_usage():
    return jsonify(_empty_usage_payload("copilot"))


@app.route("/api/claude/usage")
def api_claude_usage():
    return jsonify(_empty_usage_payload("claude"))


@app.route("/api/codex/usage")
def api_codex_usage():
    return jsonify(_empty_usage_payload("codex"))


@app.route("/api/gemini/usage")
def api_gemini_usage():
    return jsonify(_empty_usage_payload("gemini"))


@app.route("/api/savant/usage")
def api_savant_usage():
    with _bg_lock:
        cached = _bg_cache.get("savant_usage")
    if cached is not None:
        return jsonify(cached)
    return jsonify({
        "loading": True,
        "models": [],
        "tools": [],
        "daily": [],
        "totals": {
            "sessions": 0,
            "messages": 0,
            "turns": 0,
            "tool_calls": 0,
            "total_hours": 0,
            "avg_session_minutes": 0,
            "avg_tools_per_turn": 0,
            "avg_turns_per_message": 0,
            "events": 0,
        },
    })


# --- Detect native vs Docker mode ---
_IN_DOCKER = os.path.isfile("/.dockerenv") or bool(os.environ.get("RUNNING_IN_DOCKER"))

# --- Claude Code data directories ---
META_DIR = os.environ.get("META_DIR",
    "/data/meta" if _IN_DOCKER else os.path.expanduser("~/.savant/meta"))
SAVANT_DIR = os.environ.get("SAVANT_DIR",
    os.path.expanduser("~/.savant"))
SAVANT_SESSIONS_DIR = os.path.join(SAVANT_DIR, "sessions")
SAVANT_META_DIR = os.path.join(SAVANT_DIR, ".savant-meta")
SAVANT_STATE_DB = os.path.join(SAVANT_DIR, "state.db")

import time as _time
import random as _random
import threading
from concurrent.futures import ThreadPoolExecutor


def _unique_ts_id():
    """Generate a unique timestamp-based ID (ns + random suffix)."""
    import time
    return str(time.time_ns()) + str(_random.randint(1000, 9999))

# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND CACHE — all list/usage data served from memory
# ═══════════════════════════════════════════════════════════════════════════════
_bg_cache = {
    'copilot_usage': None, 'claude_usage': None, 'codex_usage': None, 'gemini_usage': None, 'savant_usage': None,
}
_bg_lock = threading.Lock()

# Event queue for real-time toast notifications (MCP actions, etc.)
_events = []  # list of {id, type, message, timestamp} - LEGACY, kept for backward compat
_events_lock = threading.Lock()
_event_counter = 0

def _emit_event(event_type: str, message: str, detail: dict = None):
    """Push a UI notification event to SQLite. Frontend polls /api/events to pick these up."""
    global _event_counter
    
    # Generate unique notification ID
    import uuid
    notification_id = f"notif_{uuid.uuid4().hex[:12]}"
    
    # Create notification in SQLite
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        NotificationDB.create({
            "notification_id": notification_id,
            "event_type": event_type,
            "message": message,
            "detail": detail or {},
            "workspace_id": detail.get("workspace_id") if detail else None,
            "session_id": detail.get("session_id") if detail else None,
            "read": False,
            "created_at": now_iso,
        })
    except Exception as e:
        logger.error(f"Error creating notification in SQLite: {e}")
    
    # Also keep in-memory for backward compatibility (legacy code)
    with _events_lock:
        _event_counter += 1
        evt = {
            "id": _event_counter,
            "notification_id": notification_id,
            "type": event_type,
            "message": message,
            "detail": detail or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _events.append(evt)
        # Keep only last 50 events in memory
        if len(_events) > 50:
            _events[:] = _events[-50:]


def parse_timestamp(ts):
    if not ts:
        return None
    try:
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except Exception:
        return None


@app.route("/")
def index():
    return jsonify({
        "name": "savant-server",
        "mode": "api-only",
        "status": "ok",
    })


@app.route("/api/events")
def api_events():
    """Poll for UI notification events from SQLite. Only returns unread notifications to prevent duplicates."""
    since = request.args.get("since", None, type=str)
    limit = request.args.get("limit", 50, type=int)
    
    try:
        # Only get UNREAD notifications to prevent re-showing
        notifications = NotificationDB.list_unread(limit=limit, user_id=g.user_id)
        
        # If 'since' is provided, filter to only newer ones
        if since:
            notifications = [n for n in notifications if n.get("notification_id") != since and 
                           n.get("created_at") > _get_notification_timestamp(since)]
        
        # Transform to match legacy format for backward compatibility
        events = []
        notification_ids_to_mark = []
        
        for notif in notifications:
            notif_id = notif.get("notification_id")
            # Use stable hash-based ID instead of enumeration
            stable_id = abs(hash(notif_id)) % 1000000
            
            events.append({
                "id": stable_id,  # Stable ID based on notification_id
                "notification_id": notif_id,
                "type": notif.get("event_type"),
                "message": notif.get("message"),
                "detail": notif.get("detail", {}),
                "timestamp": notif.get("created_at").isoformat() if isinstance(notif.get("created_at"), datetime) else notif.get("created_at"),
                "read": False,  # Always false since we only fetch unread
            })
            
            notification_ids_to_mark.append(notif_id)
        
        # Batch mark as read after fetching (notifications have been shown)
        for notif_id in notification_ids_to_mark:
            NotificationDB.mark_as_read(notif_id)
        
        # Add cache headers to reduce unnecessary re-renders
        response = jsonify(events)
        if not events:
            # If no events, cache for longer (5 seconds)
            response.headers["Cache-Control"] = "max-age=5, must-revalidate"
        else:
            # If events exist, don't cache (need fresh data)
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response
    except Exception as e:
        logger.error(f"Error fetching notifications: {e}")
        # Fallback to in-memory events
        since_id = request.args.get("since", 0, type=int)
        with _events_lock:
            new_events = [e for e in _events if e["id"] > since_id]
        return jsonify(new_events)


def _get_notification_timestamp(notification_id: str):
    """Helper to get timestamp of a notification by ID (returns ISO string)."""
    try:
        notif = NotificationDB.get_by_id(notification_id)
        return notif.get("created_at", "") if notif else ""
    except Exception:
        return ""


# ── Workspaces ──────────────────────────────────────────────────────────────

def _workspaces_path():
    return os.path.join(META_DIR, "workspaces.json")


def _read_workspaces(user_id=""):
    """Read workspaces from SQLite."""
    try:
        workspaces = WorkspaceDB.list_all(limit=1000, user_id=user_id)
        
        normalized = []
        for ws in workspaces:
            normalized_ws = {
                "id": ws.get("workspace_id"),
                "workspace_id": ws.get("workspace_id"),
                "name": ws.get("name"),
                "description": ws.get("description", ""),
                "priority": ws.get("priority", "medium"),
                "status": ws.get("status", "open"),
                "color": ws.get("color") or None,
                "task_stats": WorkspaceDB.get_task_stats(ws.get("workspace_id", "")),
                "created_at": ws.get("created_at"),
                "updated_at": ws.get("updated_at"),
            }
            normalized.append(normalized_ws)
        
        return normalized
    except Exception as e:
        logger.error(f"Error reading workspaces: {e}")
        return []

def _write_workspaces(workspaces, user_id=""):
    """Write workspaces to SQLite."""
    try:
        for ws in workspaces:
            workspace_id = ws.get("id") or ws.get("workspace_id")
            if not workspace_id:
                continue
            
            existing = WorkspaceDB.get_by_id(workspace_id, user_id=user_id)
            if existing:
                WorkspaceDB.update(workspace_id, {
                    "name": ws.get("name", "Untitled"),
                    "description": ws.get("description", ""),
                    "priority": ws.get("priority", "medium"),
                    "status": ws.get("status", "open"),
                    "color": ws.get("color", ""),
                }, user_id=user_id)
            else:
                WorkspaceDB.create({
                    "workspace_id": workspace_id,
                    "name": ws.get("name", "Untitled"),
                    "description": ws.get("description", ""),
                    "priority": ws.get("priority", "medium"),
                    "status": ws.get("status", "open"),
                    "user_id": user_id,
                })
    except Exception as e:
        logger.error(f"Error writing workspaces: {e}")

@app.route("/api/sessions/ingest", methods=["POST"])
def api_sessions_ingest():
    """Ingest session metadata pushed by the client."""
    data, err = _safe_json()
    if err:
        return err
    
    session_id = _str_field(data, "session_id", max_len=100)
    provider = _str_field(data, "provider", max_len=50)
    workspace_id = _str_field(data, "workspace_id", max_len=100)
    
    if not session_id or not provider:
        return jsonify({"error": "session_id and provider required"}), 400
    
    # Persist or update the link
    try:
        link = WorkspaceSessionLinkDB.upsert(workspace_id or "", provider, session_id)
        return jsonify(link), 200
    except Exception as e:
        logger.error(f"Error ingesting session {session_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/workspaces", methods=["GET"])
def api_workspaces_list():
    workspaces = _read_workspaces(user_id=g.user_id)
    all_tasks = TaskDB.list_all(user_id=g.user_id)
    all_registry_mrs = _read_merge_requests()

    include_kg = request.args.get("include_kg", "").lower() in ("1", "true", "yes")

    # Pre-index tasks and MRs by workspace_id (one pass, O(n) instead of O(n*ws))
    tasks_by_ws: dict[str, list] = {}
    for t in all_tasks:
        tasks_by_ws.setdefault(t.get("workspace_id"), []).append(t)
    mrs_by_ws: dict[str, list] = {}
    for m in all_registry_mrs:
        mrs_by_ws.setdefault(m.get("workspace_id"), []).append(m)

    # Batch-load session links for all workspaces in one SQL query
    ws_ids = [ws["id"] for ws in workspaces]
    links_by_ws = WorkspaceSessionLinkDB.list_by_workspaces(ws_ids)

    # KG stats are expensive: only compute when explicitly requested
    kg_by_ws: dict[str, dict] = {}
    if include_kg:
        from db.knowledge_graph import KnowledgeGraphDB
        all_kg_nodes = KnowledgeGraphDB.list_nodes(limit=500, include_staged=False)
        all_kg_edges = KnowledgeGraphDB.list_edges(limit=2000)
        # Pre-index nodes by workspace to avoid O(ws * nodes) scans
        nodes_by_ws: dict[str, list] = {}
        for n in all_kg_nodes:
            for wid in (n.get("metadata") or {}).get("workspaces", []) or []:
                nodes_by_ws.setdefault(wid, []).append(n)
        for ws_id, ws_kg_nodes in nodes_by_ws.items():
            ws_kg_node_ids = {n["node_id"] for n in ws_kg_nodes}
            ws_kg_edges = [e for e in all_kg_edges
                           if e["source_id"] in ws_kg_node_ids and e["target_id"] in ws_kg_node_ids]
            nodes_by_type: dict[str, int] = {}
            for n in ws_kg_nodes:
                t = n.get("node_type", "unknown")
                nodes_by_type[t] = nodes_by_type.get(t, 0) + 1
            staged_count = sum(1 for n in ws_kg_nodes if n.get("status") == "staged")
            kg_by_ws[ws_id] = {
                "total_nodes": len(ws_kg_nodes),
                "total_edges": len(ws_kg_edges),
                "nodes_by_type": nodes_by_type,
                "staged_count": staged_count,
            }

    # Enrich each workspace using the pre-built indexes
    for ws in workspaces:
        ws_id = ws["id"]
        ws.setdefault("status", "open")
        ws.setdefault("priority", "medium")
        ws.setdefault("start_date", None)
        # Session link counts
        counts = {"copilot": 0, "claude": 0, "codex": 0, "gemini": 0, "savant": 0, "total": 0}
        for link in links_by_ws.get(ws_id, []):
            provider = link.get("provider", "copilot")
            if provider in counts:
                counts[provider] += 1
            counts["total"] += 1
        ws["counts"] = counts
        ws["session_status_counts"] = {}
        ws["projects"] = []
        # MR counts from registry (workspace-scoped)
        mr_by_url: dict[str, str] = {}
        for m in mrs_by_ws.get(ws_id, []):
            url = (m.get("url") or "").strip().lower().rstrip("/")
            if url:
                mr_by_url[url] = m.get("status") or "open"
        ws["mr_count"] = len(mr_by_url)
        mr_status_counts: dict[str, int] = {}
        for status in mr_by_url.values():
            mr_status_counts[status] = mr_status_counts.get(status, 0) + 1
        ws["mr_status_counts"] = mr_status_counts
        ws["note_count"] = 0
        ws["file_count"] = 0
        ws["git_commit_count"] = 0
        ws["archived_count"] = 0
        ws["session_file_count"] = 0
        # Task stats
        ws_tasks = tasks_by_ws.get(ws_id, [])
        ws["task_stats"] = {
            "total": len(ws_tasks),
            "todo": sum(1 for t in ws_tasks if t.get("status") == "todo"),
            "in_progress": sum(1 for t in ws_tasks if t.get("status") == "in-progress"),
            "done": sum(1 for t in ws_tasks if t.get("status") == "done"),
            "blocked": sum(1 for t in ws_tasks if t.get("status") == "blocked"),
        }
        # KG stats: present when ?include_kg=1, else omitted (client falls back to 0s)
        if include_kg:
            ws["kg_stats"] = kg_by_ws.get(ws_id, {
                "total_nodes": 0, "total_edges": 0, "nodes_by_type": {}, "staged_count": 0,
            })
    # Order is now manual (drag-and-drop). Open workspaces appear before closed.
    workspaces.sort(key=lambda w: 0 if w.get("status", "open") == "open" else 1)
    
    # Add ETag based on data hash to prevent unnecessary re-renders
    import hashlib
    data_str = json.dumps(workspaces, sort_keys=True, default=str)
    etag = hashlib.md5(data_str.encode()).hexdigest()
    
    # Check if client has cached version
    if request.headers.get("If-None-Match") == etag:
        return "", 304  # Not Modified
    
    response = jsonify(workspaces)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "max-age=2, must-revalidate"
    return response

@app.route("/api/workspaces", methods=["POST"])
def api_workspaces_create():
    admin_err = _require_admin()
    if admin_err:
        return admin_err
    data, err = _safe_json()
    if err:
        return err
    name = _str_field(data, "name", max_len=200)
    if not name:
        return jsonify({"error": "Name required"}), 400
    ws_id = _unique_ts_id()
    ws = {
        "id": ws_id,
        "workspace_id": ws_id,
        "name": name,
        "description": _str_field(data, "description", max_len=2000),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "start_date": _str_field(data, "start_date", max_len=20) or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "priority": _str_field(data, "priority", max_len=20) or "medium",
        "status": "open",
        "color": _str_field(data, "color", max_len=20) or None,
    }
    WorkspaceDB.create({
        "workspace_id": ws_id,
        "name": ws["name"],
        "description": ws["description"],
        "priority": ws["priority"],
        "status": ws["status"],
        "user_id": g.user_id,
    })
    _emit_event("workspace_created", f"Workspace created: {name}", {"workspace_id": ws_id, "name": name})
    return jsonify(ws)

@app.route("/api/workspaces/reorder", methods=["POST"])
def api_workspaces_reorder():
    admin_err = _require_admin()
    if admin_err:
        return admin_err
    data = request.get_json(force=True)
    order = data.get("order", [])
    if not order:
        return jsonify({"error": "order required"}), 400
    workspaces = _read_workspaces(user_id=g.user_id)
    ws_map = {ws["id"]: ws for ws in workspaces}
    reordered = [ws_map[wid] for wid in order if wid in ws_map]
    # Append any workspaces not in the order list (safety net)
    seen = set(order)
    for ws in workspaces:
        if ws["id"] not in seen:
            reordered.append(ws)
    _write_workspaces(reordered, user_id=g.user_id)
    return jsonify({"ok": True})

@app.route("/api/workspaces/<ws_id>", methods=["PUT"])
def api_workspaces_update(ws_id):
    admin_err = _require_admin()
    if admin_err:
        return admin_err
    data = request.get_json(force=True)
    workspaces = _read_workspaces(user_id=g.user_id)
    for ws in workspaces:
        if ws["id"] == ws_id:
            if "name" in data:
                ws["name"] = data["name"].strip()
            if "description" in data:
                ws["description"] = data["description"].strip()
            if "start_date" in data:
                ws["start_date"] = (data["start_date"] or "").strip() or None
            if "priority" in data:
                ws["priority"] = (data["priority"] or "").strip() or "medium"
            if "color" in data:
                ws["color"] = (data["color"] or "").strip() or None
            if "status" in data:
                ws["status"] = data["status"].strip()
                if ws["status"] == "closed" and not ws.get("closed_at"):
                    ws["closed_at"] = datetime.now(timezone.utc).isoformat()
                elif ws["status"] == "open":
                    ws.pop("closed_at", None)
            _write_workspaces(workspaces, user_id=g.user_id)
            if "status" in data and data["status"] == "closed":
                _emit_event("workspace_closed", f"Workspace closed: {ws.get('name', ws_id)}", {"workspace_id": ws_id})
            elif "status" in data and data["status"] == "open":
                _emit_event("workspace_reopened", f"Workspace reopened: {ws.get('name', ws_id)}", {"workspace_id": ws_id})
            return jsonify(ws)
    return jsonify({"error": "Workspace not found"}), 404

@app.route("/api/workspaces/<ws_id>", methods=["DELETE"])
def api_workspaces_delete(ws_id):
    admin_err = _require_admin()
    if admin_err:
        return admin_err
    existing_links = WorkspaceSessionLinkDB.list_by_workspace(ws_id)

    # Remove session links first so FK on workspace_session_links never blocks delete.
    WorkspaceSessionLinkDB.delete_by_workspace(ws_id)
    # Delete from SQLite
    success = WorkspaceDB.delete(ws_id, user_id=g.user_id)
    if not success:
        return jsonify({"error": "Workspace not found"}), 404
    
    _emit_event("workspace_deleted", f"Workspace {ws_id} deleted", {"workspace_id": ws_id})
    return jsonify({"deleted": ws_id})


_VALID_PROVIDERS = {"copilot", "claude", "codex", "gemini", "savant"}


def _normalize_provider_name(provider: str) -> str:
    p = str(provider or "").strip().lower()
    if p == "cline":
        return "copilot"
    if p in _VALID_PROVIDERS:
        return p
    raise ValueError("Invalid provider")

@app.route("/api/workspaces/<ws_id>/session-links", methods=["GET"])
def api_workspace_session_links_list(ws_id):
    if not WorkspaceDB.get_by_id(ws_id, user_id=g.user_id):
        return jsonify({"error": "Workspace not found"}), 404
    links = WorkspaceSessionLinkDB.list_by_workspace(ws_id)
    return jsonify({"workspace_id": ws_id, "links": links})


@app.route("/api/workspaces/<ws_id>/session-links", methods=["POST"])
def api_workspace_session_links_upsert(ws_id):
    if not WorkspaceDB.get_by_id(ws_id, user_id=g.user_id):
        return jsonify({"error": "Workspace not found"}), 404
    data = request.get_json(force=True) or {}
    session_id = str(data.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    try:
        link = WorkspaceSessionLinkDB.upsert(ws_id, "session", session_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _emit_event("session_assigned", "Session assigned to workspace", {"session_id": session_id, "workspace_id": ws_id})
    return jsonify(link)


@app.route("/api/workspaces/<ws_id>/session-links/<provider>/<session_id>", methods=["DELETE"])
def api_workspace_session_links_delete(ws_id, provider, session_id):
    try:
        deleted = WorkspaceSessionLinkDB.delete_from_workspace(ws_id, provider, session_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not deleted:
        return jsonify({"error": "Session link not found"}), 404
    return jsonify({"deleted": True, "workspace_id": ws_id, "provider": _normalize_provider_name(provider), "session_id": session_id})


@app.route("/api/session-links/resolve", methods=["GET"])
def api_session_links_resolve():
    provider = request.args.get("provider", "")
    session_id = request.args.get("session_id", "")
    if not provider or not session_id:
        return jsonify({"error": "provider and session_id are required"}), 400
    try:
        provider = _normalize_provider_name(provider)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    row = WorkspaceSessionLinkDB.resolve(provider, session_id)
    return jsonify({
        "provider": provider,
        "session_id": session_id,
        "workspace_id": row.get("workspace_id") if row else None,
    })


@app.route("/api/workspaces/<ws_id>/context", methods=["GET"])
def api_workspace_context(ws_id):
    """Generate a bridge prompt for an AI agent to continue work on this workspace.

    Returns a structured markdown prompt that covers:
    - Workspace name/description/status
    - All linked sessions with summaries and checkpoint history
    - Active tasks (todo/in-progress/blocked)
    - Recent notes
    - MR and Jira references
    """
    ws = WorkspaceDB.get_by_id(ws_id, user_id=g.user_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404

    ws_name = ws.get("name", ws_id)
    ws_desc = ws.get("description", "")
    ws_status = ws.get("status", "open")

    # --- Sessions ---
    links = WorkspaceSessionLinkDB.list_by_workspace(ws_id)
    session_blocks = []

    env_map = {
        "copilot": "SESSION_DIR",
        "claude": "CLAUDE_DIR",
        "codex": "CODEX_DIR",
        "gemini": "GEMINI_DIR",
        "savant": "SAVANT_DIR",
    }
    provider_display = {
        "copilot": "Copilot CLI",
        "claude": "Claude Code",
        "codex": "Codex CLI",
        "gemini": "Gemini CLI",
        "savant": "Savant",
    }

    for link in links:
        sid = link.get("session_id", "")
        provider = link.get("provider", "copilot")
        display_provider = provider_display.get(provider, provider.title())

        sdir = _resolve_session_dir(sid, provider)
        env_key = env_map.get(provider, "SESSION_DIR")
        base_dir = os.environ.get(env_key, "~/.copilot/session-state")
        fallback_path = os.path.join(base_dir, sid)

        if not sdir:
            # Session dir unavailable — still mention it with its expected path
            session_blocks.append(
                f"### Session: {sid} ({display_provider})\n"
                f"> ⚠️ Session files not accessible. Expected path: `{fallback_path}`\n"
                f"> This session exists in the workspace but its files could not be loaded.\n"
            )
            continue

        # Read workspace.yaml (copilot) or settings.json for metadata
        summary = ""
        branch = ""
        cwd = ""
        created_at = ""
        for meta_file, loader in [("workspace.yaml", "yaml"), ("settings.json", "json"), ("config.json", "json")]:
            meta_path = os.path.join(sdir, meta_file)
            if os.path.isfile(meta_path):
                try:
                    if loader == "yaml":
                        import yaml
                        with open(meta_path) as fh:
                            meta = yaml.safe_load(fh) or {}
                    else:
                        with open(meta_path) as fh:
                            meta = json.load(fh)
                    summary = meta.get("summary") or meta.get("name") or ""
                    branch = meta.get("branch") or ""
                    cwd = meta.get("cwd") or meta.get("git_root") or meta.get("projectPath") or ""
                    created_at = meta.get("created_at") or meta.get("startTime") or ""
                    break
                except Exception:
                    pass

        # Read checkpoint history from checkpoints/index.md
        checkpoint_lines = []
        cp_index = os.path.join(sdir, "checkpoints", "index.md")
        if os.path.isfile(cp_index):
            try:
                with open(cp_index) as fh:
                    for line in fh:
                        line = line.strip()
                        # Table rows look like: | N | Title | file.md |
                        if line.startswith("|") and "|" in line[1:]:
                            parts = [p.strip() for p in line.split("|") if p.strip()]
                            if len(parts) >= 2 and parts[0].isdigit():
                                checkpoint_lines.append(f"  {parts[0]}. {parts[1]}")
            except Exception:
                pass

        # If no index.md, list checkpoint files by name
        if not checkpoint_lines:
            cp_dir = os.path.join(sdir, "checkpoints")
            if os.path.isdir(cp_dir):
                for fname in sorted(os.listdir(cp_dir)):
                    if fname.endswith(".md") and fname != "index.md":
                        checkpoint_lines.append(f"  - {fname}")

        # Read latest checkpoint overview (first 800 chars of last checkpoint)
        latest_cp_text = ""
        cp_dir = os.path.join(sdir, "checkpoints")
        if os.path.isdir(cp_dir):
            cp_files = sorted(
                [f for f in os.listdir(cp_dir) if f.endswith(".md") and f != "index.md"]
            )
            if cp_files:
                latest_cp_path = os.path.join(cp_dir, cp_files[-1])
                try:
                    with open(latest_cp_path) as fh:
                        raw = fh.read(1200)
                    # Extract <overview> block if present
                    if "<overview>" in raw:
                        start = raw.index("<overview>") + len("<overview>")
                        end = raw.index("</overview>") if "</overview>" in raw else start + 600
                        latest_cp_text = raw[start:end].strip()[:600]
                    else:
                        latest_cp_text = raw[:600].strip()
                except Exception:
                    pass

        block = f"### Session: {sid} ({display_provider})\n"
        if summary:
            block += f"- **Summary**: {summary}\n"
        if cwd:
            block += f"- **Project**: `{cwd}`\n"
        if branch:
            block += f"- **Branch**: `{branch}`\n"
        if created_at:
            block += f"- **Started**: {created_at}\n"
        block += f"- **Session path**: `{sdir}`\n"

        if checkpoint_lines:
            block += f"\n**Checkpoints ({len(checkpoint_lines)} total)**:\n"
            block += "\n".join(checkpoint_lines[:20])
            if len(checkpoint_lines) > 20:
                block += f"\n  ... and {len(checkpoint_lines) - 20} more"
            block += "\n"

        if latest_cp_text:
            block += f"\n**Latest checkpoint overview**:\n> {latest_cp_text[:400]}\n"

        session_blocks.append(block)

    # --- Tasks ---
    tasks = TaskDB.list_by_workspace(ws_id, limit=200, user_id=g.user_id)
    active_tasks = [t for t in tasks if t.get("status") in ("todo", "in-progress", "blocked")]
    done_tasks = [t for t in tasks if t.get("status") == "done"]

    task_lines = []
    for t in active_tasks:
        status_icon = {"todo": "⏳", "in-progress": "🔄", "blocked": "🚫"}.get(t.get("status", ""), "•")
        title = t.get("title", "")
        desc = t.get("description", "")
        task_lines.append(f"- {status_icon} **{title}** [{t.get('status')}]" + (f"\n  > {desc}" if desc else ""))

    # --- Notes ---
    notes = NoteDB.list_by_workspace(ws_id, limit=20, user_id=g.user_id)
    note_lines = []
    for n in (notes or [])[:10]:
        text = (n.get("text") or "").strip()[:200]
        created = n.get("created_at", "")[:10]
        if text:
            note_lines.append(f"- [{created}] {text}")

    # --- Build prompt ---
    lines = [
        f"# Workspace Bridge Prompt: {ws_name}",
        "",
        f"**Status**: {ws_status}  |  **Workspace ID**: `{ws_id}`",
    ]
    if ws_desc:
        lines += ["", ws_desc]

    lines += [
        "",
        "---",
        "",
        f"## Sessions ({len(links)} linked)",
        "",
    ]
    if session_blocks:
        lines += session_blocks
    else:
        lines.append("_No sessions linked to this workspace._\n")

    lines += [
        "",
        "---",
        "",
        f"## Active Tasks ({len(active_tasks)} / {len(tasks)} total)",
        "",
    ]
    if task_lines:
        lines += task_lines
    else:
        lines.append("_No active tasks._")

    if done_tasks:
        lines += ["", f"✅ {len(done_tasks)} task(s) completed."]

    if note_lines:
        lines += [
            "",
            "---",
            "",
            "## Recent Notes",
            "",
        ]
        lines += note_lines

    lines += [
        "",
        "---",
        "",
        "## Instructions for Continuing Agent",
        "",
        "You are picking up work on this workspace. Review the session summaries and checkpoint history above.",
        "Focus on the active tasks. If a session has unresolved work, check the latest checkpoint overview for context.",
        "All session files are accessible via the paths listed above.",
    ]

    prompt = "\n".join(lines)
    return jsonify({
        "prompt": prompt,
        "workspace_id": ws_id,
        "workspace_name": ws_name,
        "session_count": len(links),
        "active_task_count": len(active_tasks),
    })


@app.route("/api/jira-tickets", methods=["GET", "POST"])
def api_jira_tickets():
    """CRUD for Jira tickets. GET: list by workspace_id or session_id. POST: create."""
    if request.method == "POST":
        data = request.get_json(force=True)
        if not data.get("ticket_key"):
            return jsonify({"error": "ticket_key required"}), 400
        ticket_id = data.get("ticket_id") or f"jira_{uuid.uuid4().hex[:16]}"
        data["ticket_id"] = ticket_id
        data["user_id"] = g.user_id
        if not data.get("workspace_id"):
            data["workspace_id"] = ""
        try:
            ticket = JiraTicketDB.create(data)
            return jsonify(ticket), 201
        except Exception as e:
            if "UNIQUE" in str(e):
                return jsonify({"status": "duplicate", "ticket_id": ticket_id}), 200
            logger.error(f"Error creating Jira ticket: {e}")
            return jsonify({"error": str(e)}), 500
    session_id = (request.args.get("session_id") or "").strip()
    if session_id:
        tickets = JiraTicketDB.list_full_by_session(session_id, user_id=g.user_id)
        return jsonify(tickets)
    workspace_id = (request.args.get("workspace_id") or "").strip()
    if workspace_id:
        tickets = JiraTicketDB.list_by_workspace(workspace_id, limit=1000, user_id=g.user_id)
    else:
        tickets = JiraTicketDB.list_all(limit=1000, user_id=g.user_id)
    return jsonify(tickets)


@app.route("/api/jira-tickets/<ticket_id>", methods=["GET", "PUT", "DELETE"])
def api_jira_ticket_detail(ticket_id):
    """Get, update, or delete a single Jira ticket."""
    if request.method == "GET":
        ticket = JiraTicketDB.get_by_id(ticket_id, user_id=g.user_id) or JiraTicketDB.get_by_key(ticket_id, user_id=g.user_id)
        if not ticket:
            return jsonify({"error": "not found"}), 404
        return jsonify(ticket)
    elif request.method == "PUT":
        data = request.get_json(force=True)
        ticket = JiraTicketDB.update(ticket_id, data, user_id=g.user_id)
        if not ticket:
            # Try by key
            existing = JiraTicketDB.get_by_key(ticket_id, user_id=g.user_id)
            if existing:
                ticket = JiraTicketDB.update(existing["ticket_id"], data, user_id=g.user_id)
        if not ticket:
            return jsonify({"error": "not found"}), 404
        return jsonify(ticket)
    else:
        ok = JiraTicketDB.delete(ticket_id, user_id=g.user_id)
        if not ok:
            existing = JiraTicketDB.get_by_key(ticket_id, user_id=g.user_id)
            if existing:
                ok = JiraTicketDB.delete(existing["ticket_id"], user_id=g.user_id)
        return jsonify({"deleted": ok})


@app.route("/api/jira-tickets/<ticket_id>/notes", methods=["POST"])
def api_jira_ticket_add_note(ticket_id):
    """Add a note to a Jira ticket."""
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    ticket = JiraTicketDB.add_note(ticket_id, text, data.get("session_id", ""), user_id=g.user_id)
    if not ticket:
        return jsonify({"error": "ticket not found"}), 404
    return jsonify(ticket)


@app.route("/api/merge-requests/<mr_id>", methods=["GET", "PUT", "DELETE"])
def api_merge_request_detail(mr_id):
    """Get, update, or delete a single MR."""
    if request.method == "GET":
        mr = MergeRequestDB.get_by_id(mr_id, user_id=g.user_id)
        if not mr:
            return jsonify({"error": "not found"}), 404
        return jsonify(mr)
    elif request.method == "PUT":
        data = request.get_json(force=True)
        mr = MergeRequestDB.update(mr_id, data, user_id=g.user_id)
        if not mr:
            return jsonify({"error": "not found"}), 404
        return jsonify(mr)
    else:
        ok = MergeRequestDB.delete(mr_id, user_id=g.user_id)
        return jsonify({"deleted": ok})


@app.route("/api/merge-requests/<mr_id>/notes", methods=["POST"])
def api_merge_request_add_note(mr_id):
    """Add a note to a MR."""
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    mr = MergeRequestDB.add_note(mr_id, text, data.get("session_id", ""), user_id=g.user_id)
    if not mr:
        return jsonify({"error": "MR not found"}), 404
    return jsonify(mr)


@app.route("/api/experiences", methods=["GET", "POST"])
def api_experiences():
    """Experiences CRUD. GET: list. POST: create."""
    from db.experiences import ExperienceDB
    if request.method == "POST":
        data = request.get_json(force=True)
        if not data.get("experience_id") or not data.get("content"):
            return jsonify({"error": "experience_id and content required"}), 400
        try:
            exp = ExperienceDB.create(data)
            return jsonify(exp), 201
        except Exception as e:
            if "UNIQUE" in str(e):
                return jsonify({"status": "duplicate", "experience_id": data["experience_id"]}), 200
            logger.error(f"Error creating experience: {e}")
            return jsonify({"error": str(e)}), 500
    ws = (request.args.get("workspace_id") or "").strip()
    if ws:
        exps = ExperienceDB.list_by_workspace(ws, limit=1000)
    else:
        exps = ExperienceDB.list_all(limit=1000)
    return jsonify(exps)


@app.route("/api/notifications", methods=["GET", "POST"])
def api_notifications():
    """Notifications CRUD. GET: list recent. POST: create."""
    if request.method == "POST":
        data = request.get_json(force=True)
        notif_id = data.get("notification_id") or f"notif_{uuid.uuid4().hex[:16]}"
        data["notification_id"] = notif_id
        data["user_id"] = g.user_id
        if not data.get("event_type") or not data.get("message"):
            return jsonify({"error": "event_type and message required"}), 400
        try:
            notif = NotificationDB.create(data)
            return jsonify(notif), 201
        except Exception as e:
            if "UNIQUE" in str(e):
                return jsonify({"status": "duplicate"}), 200
            logger.error(f"Error creating notification: {e}")
            return jsonify({"error": str(e)}), 500
    notifs = NotificationDB.list_recent(limit=100, user_id=g.user_id)
    return jsonify(notifs)


@app.route("/api/notes", methods=["POST"])
def api_notes_create():
    """Create a note. Requires note_id, session_id, text. Optional: workspace_id, created_at, updated_at."""
    data = request.get_json(force=True)
    if not data.get("note_id") or not data.get("session_id") or not data.get("text"):
        return jsonify({"error": "note_id, session_id, and text are required"}), 400
    data["user_id"] = g.user_id
    try:
        note = NoteDB.create(data)
        return jsonify(note), 201
    except Exception as e:
        # Duplicate — skip gracefully
        if "UNIQUE" in str(e):
            return jsonify({"status": "duplicate", "note_id": data["note_id"]}), 200
        logger.error(f"Error creating note: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/notes/backfill-workspaces", methods=["POST"])
def api_notes_backfill_workspaces():
    """Backfill workspace_id on notes using workspace_session_links."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE notes SET workspace_id = (
                    SELECT wsl.workspace_id FROM workspace_session_links wsl
                    WHERE wsl.session_id = notes.session_id
                    LIMIT 1
                )
                WHERE (workspace_id IS NULL OR workspace_id = '')
                AND session_id IN (SELECT session_id FROM workspace_session_links)
            """)
            updated = cur.rowcount
        conn.commit()
        return jsonify({"backfilled": updated})
    finally:
        release_connection(conn)


@app.route("/api/workspaces/<ws_id>/notes", methods=["GET"])
def api_workspaces_notes(ws_id):
    """Aggregate notes for a workspace from SQLite, grouped by session, ordered by created_at."""
    groups = []

    try:
        db_notes = NoteDB.list_by_workspace(ws_id, limit=500, user_id=g.user_id)
        by_session = {}
        for n in db_notes:
            sid = n.get("session_id", "")
            by_session.setdefault(sid, []).append({
                "text": n.get("text", ""),
                "timestamp": n.get("created_at", ""),
            })
        for sid, notes in by_session.items():
            sorted_notes = sorted(notes, key=lambda n: n.get("timestamp") or "", reverse=True)
            groups.append({
                "session_id": sid,
                "provider": "copilot",
                "summary": sid[:12] + "\u2026" if len(sid) > 12 else sid,
                "note_count": len(sorted_notes),
                "notes": sorted_notes,
            })
    except Exception as e:
        logger.error(f"Error loading SQLite notes for workspace {ws_id}: {e}")

    groups.sort(key=lambda g: g["notes"][0].get("timestamp", "") if g["notes"] else "", reverse=True)
    return jsonify({"groups": groups})


# --- Session ↔ MR / Jira assignment (used by MCP tools) --------------------

def _assign_mr_handler(session_id):
    """Assign a merge request to a session."""
    data = request.get_json(force=True) or {}
    mr_id = data.get("mr_id")
    if not mr_id:
        return jsonify({"error": "mr_id required"}), 400
    mr = MergeRequestDB.get_by_id(mr_id, user_id=g.user_id)
    if not mr:
        return jsonify({"error": f"MR {mr_id} not found"}), 404
    role = data.get("role") or "author"
    try:
        MergeRequestDB.assign_session(mr_id, session_id, role)
    except Exception as e:
        if "UNIQUE" in str(e):
            return jsonify({"error": "Already assigned", "mr_id": mr_id, "session_id": session_id}), 409
        raise
    mrs = MergeRequestDB.list_by_session(session_id, user_id=g.user_id)
    return jsonify({"session_id": session_id, "merge_requests": mrs})


def _unassign_mr_handler(session_id):
    """Remove a merge request assignment from a session."""
    data = request.get_json(force=True) or {}
    mr_id = data.get("mr_id")
    if not mr_id:
        return jsonify({"error": "mr_id required"}), 400
    removed = MergeRequestDB.unassign_session(mr_id, session_id)
    if not removed:
        return jsonify({"error": "Not assigned", "mr_id": mr_id, "session_id": session_id}), 404
    return jsonify({"removed": mr_id, "session_id": session_id})


def _assign_jira_handler(session_id):
    """Assign a Jira ticket to a session."""
    data = request.get_json(force=True) or {}
    ticket_id = data.get("ticket_id")
    if not ticket_id:
        return jsonify({"error": "ticket_id required"}), 400
    ticket = JiraTicketDB.get_by_id(ticket_id, user_id=g.user_id) or JiraTicketDB.get_by_key(ticket_id, user_id=g.user_id)
    if not ticket:
        return jsonify({"error": f"Ticket {ticket_id} not found"}), 404
    resolved_id = ticket["ticket_id"]
    role = data.get("role") or "assignee"
    try:
        JiraTicketDB.assign_session(resolved_id, session_id, role)
    except Exception as e:
        if "UNIQUE" in str(e):
            return jsonify({"error": "Already assigned", "ticket_id": resolved_id, "session_id": session_id}), 409
        raise
    tickets = JiraTicketDB.list_by_session(session_id, user_id=g.user_id)
    return jsonify({"session_id": session_id, "jira_tickets": tickets})


def _unassign_jira_handler(session_id):
    """Remove a Jira ticket assignment from a session."""
    data = request.get_json(force=True) or {}
    ticket_id = data.get("ticket_id")
    if not ticket_id:
        return jsonify({"error": "ticket_id required"}), 400
    ticket = JiraTicketDB.get_by_id(ticket_id, user_id=g.user_id) or JiraTicketDB.get_by_key(ticket_id, user_id=g.user_id)
    resolved_id = ticket["ticket_id"] if ticket else ticket_id
    removed = JiraTicketDB.unassign_session(resolved_id, session_id)
    if not removed:
        return jsonify({"error": "Not assigned", "ticket_id": resolved_id, "session_id": session_id}), 404
    return jsonify({"removed": resolved_id, "session_id": session_id})


# Copilot (default) provider
@app.route("/api/session/<session_id>/assign-mr", methods=["POST"])
def api_session_assign_mr(session_id):
    return _assign_mr_handler(session_id)

@app.route("/api/session/<session_id>/unassign-mr", methods=["POST"])
def api_session_unassign_mr(session_id):
    return _unassign_mr_handler(session_id)

@app.route("/api/session/<session_id>/assign-jira", methods=["POST"])
def api_session_assign_jira(session_id):
    return _assign_jira_handler(session_id)

@app.route("/api/session/<session_id>/unassign-jira", methods=["POST"])
def api_session_unassign_jira(session_id):
    return _unassign_jira_handler(session_id)

# Claude provider
@app.route("/api/claude/session/<session_id>/assign-mr", methods=["POST"])
def api_claude_session_assign_mr(session_id):
    return _assign_mr_handler(session_id)

@app.route("/api/claude/session/<session_id>/unassign-mr", methods=["POST"])
def api_claude_session_unassign_mr(session_id):
    return _unassign_mr_handler(session_id)

@app.route("/api/claude/session/<session_id>/assign-jira", methods=["POST"])
def api_claude_session_assign_jira(session_id):
    return _assign_jira_handler(session_id)

@app.route("/api/claude/session/<session_id>/unassign-jira", methods=["POST"])
def api_claude_session_unassign_jira(session_id):
    return _unassign_jira_handler(session_id)

# Codex provider
@app.route("/api/codex/session/<session_id>/assign-mr", methods=["POST"])
def api_codex_session_assign_mr(session_id):
    return _assign_mr_handler(session_id)

@app.route("/api/codex/session/<session_id>/unassign-mr", methods=["POST"])
def api_codex_session_unassign_mr(session_id):
    return _unassign_mr_handler(session_id)

@app.route("/api/codex/session/<session_id>/assign-jira", methods=["POST"])
def api_codex_session_assign_jira(session_id):
    return _assign_jira_handler(session_id)

@app.route("/api/codex/session/<session_id>/unassign-jira", methods=["POST"])
def api_codex_session_unassign_jira(session_id):
    return _unassign_jira_handler(session_id)

# Gemini provider
@app.route("/api/gemini/session/<session_id>/assign-mr", methods=["POST"])
def api_gemini_session_assign_mr(session_id):
    return _assign_mr_handler(session_id)

@app.route("/api/gemini/session/<session_id>/unassign-mr", methods=["POST"])
def api_gemini_session_unassign_mr(session_id):
    return _unassign_mr_handler(session_id)

@app.route("/api/gemini/session/<session_id>/assign-jira", methods=["POST"])
def api_gemini_session_assign_jira(session_id):
    return _assign_jira_handler(session_id)

@app.route("/api/gemini/session/<session_id>/unassign-jira", methods=["POST"])
def api_gemini_session_unassign_jira(session_id):
    return _unassign_jira_handler(session_id)

# Savant provider
@app.route("/api/savant/session/<session_id>/assign-mr", methods=["POST"])
def api_savant_session_assign_mr(session_id):
    return _assign_mr_handler(session_id)

@app.route("/api/savant/session/<session_id>/unassign-mr", methods=["POST"])
def api_savant_session_unassign_mr(session_id):
    return _unassign_mr_handler(session_id)

@app.route("/api/savant/session/<session_id>/assign-jira", methods=["POST"])
def api_savant_session_assign_jira(session_id):
    return _assign_jira_handler(session_id)

@app.route("/api/savant/session/<session_id>/unassign-jira", methods=["POST"])
def api_savant_session_unassign_jira(session_id):
    return _unassign_jira_handler(session_id)


# --- Session-level notes (used by MCP: GET /api/session/<id>/notes) --------

def _session_notes_handler(session_id):
    """Return notes for a session, with POST/DELETE support."""
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        data["session_id"] = session_id
        if not data.get("text"):
            return jsonify({"error": "text required"}), 400
        if not data.get("note_id"):
            import uuid as _uuid
            data["note_id"] = f"note_{_uuid.uuid4().hex[:16]}"
        data["user_id"] = g.user_id
        try:
            note = NoteDB.create(data)
            return jsonify(note), 201
        except Exception as e:
            if "UNIQUE" in str(e):
                return jsonify({"status": "duplicate", "note_id": data["note_id"]}), 200
            return jsonify({"error": str(e)}), 500

    if request.method == "DELETE":
        data = request.get_json(force=True) or {}
        idx = data.get("index")
        if idx is None:
            return jsonify({"error": "index required"}), 400
        notes = NoteDB.list_by_session(session_id, limit=500, user_id=g.user_id)
        notes.sort(key=lambda n: n.get("created_at") or "")
        if idx < 0 or idx >= len(notes):
            return jsonify({"error": f"index {idx} out of range (0-{len(notes)-1})"}), 400
        note_id = notes[idx].get("note_id", "")
        if not note_id:
            return jsonify({"error": "note has no note_id"}), 400
        NoteDB.delete(note_id, user_id=g.user_id)
        return jsonify({"deleted": note_id, "index": idx, "session_id": session_id})

    # GET
    notes = NoteDB.list_by_session(session_id, limit=500, user_id=g.user_id)
    formatted = [{"text": n.get("text", ""), "timestamp": n.get("created_at", ""), "note_id": n.get("note_id", "")} for n in notes]
    formatted.sort(key=lambda n: n.get("timestamp") or "", reverse=True)
    return jsonify({"session_id": session_id, "notes": formatted, "count": len(formatted)})


@app.route("/api/session/<session_id>/notes", methods=["GET", "POST", "DELETE"])
def api_session_notes(session_id):
    return _session_notes_handler(session_id)


@app.route("/api/claude/session/<session_id>/notes", methods=["GET", "POST", "DELETE"])
def api_claude_session_notes(session_id):
    return _session_notes_handler(session_id)


@app.route("/api/codex/session/<session_id>/notes", methods=["GET", "POST", "DELETE"])
def api_codex_session_notes(session_id):
    return _session_notes_handler(session_id)


@app.route("/api/gemini/session/<session_id>/notes", methods=["GET", "POST", "DELETE"])
def api_gemini_session_notes(session_id):
    return _session_notes_handler(session_id)


@app.route("/api/savant/session/<session_id>/notes", methods=["GET", "POST", "DELETE"])
def api_savant_session_notes(session_id):
    return _session_notes_handler(session_id)

@app.route("/api/workspaces/search", methods=["GET"])
def api_workspaces_search():
    """Deep search across all workspaces — names, descriptions, session summaries, notes, tasks."""
    query = (request.args.get("q") or "").strip().lower()
    if not query or len(query) < 2:
        return jsonify({"workspaces": [], "sessions": [], "notes": [], "tasks": []})

    workspaces = _read_workspaces(user_id=g.user_id)
    all_tasks = TaskDB.list_all(user_id=g.user_id)
    ws_matches = []
    session_matches = []
    note_matches = []
    task_matches = []

    # Build lookup map for workspace names
    ws_by_id = {ws.get("id"): ws for ws in workspaces if ws.get("id")}

    # Search workspace name/description
    for ws in workspaces:
        ws.setdefault("status", "open")
        ws.setdefault("priority", "medium")
        haystack = ((ws.get("name") or "") + " " + (ws.get("description") or "")).lower()
        if query in haystack:
            ws_matches.append(ws)

    # Search tasks
    for t in all_tasks:
        ws_id = t.get("workspace_id")
        if not ws_id or ws_id not in ws_by_id:
            continue
        haystack = ((t.get("title") or "") + " " + (t.get("description") or "")).lower()
        if query in haystack:
            task_matches.append({
                "id": t.get("task_id") or t.get("id"), "seq": t.get("seq"), "title": t.get("title", ""), "status": t.get("status", ""),
                "workspace_id": ws_id, "workspace_name": ws_by_id[ws_id].get("name", ""),
            })

    # Search notes in DB
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT note_id, session_id, workspace_id, text, created_at FROM notes WHERE text ILIKE %s ORDER BY created_at DESC LIMIT 30",
                (f"%{query}%",),
            )
            rows = cur.fetchall()
        for r in rows:
            ws_id = r.get("workspace_id") or ""
            note_matches.append({
                "session_id": r.get("session_id", ""),
                "provider": "copilot",
                "summary": (r.get("session_id") or "")[:12],
                "workspace_name": ws_by_id.get(ws_id, {}).get("name", "") if ws_id else "",
                "text": (r.get("text") or "")[:300],
                "timestamp": r.get("created_at", ""),
            })
    except Exception as e:
        logger.error(f"Note search error: {e}")
    finally:
        if conn:
            release_connection(conn)

    note_matches.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
    return jsonify({
        "workspaces": ws_matches[:20],
        "sessions": session_matches[:20],
        "notes": note_matches[:30],
        "tasks": task_matches[:20],
        "query": query,
    })

@app.route("/api/all-mrs", methods=["GET"])
def api_all_mrs():
    """Return all MRs aggregated across all sessions and providers.
    Reads from the central merge_requests.json registry first, then
    falls back to session-embedded data for any MRs not in the registry.
    ?filter=open (default) returns non-merged/closed; ?filter=closed returns merged/closed.
    """
    filter_mode = request.args.get("filter", "open")  # 'open' or 'closed'
    closed_statuses = {"merged", "closed"}

    ws_map = {}
    for w in _read_workspaces():
        ws_map[w["id"]] = w.get("name", "")

    # Build mr_id → registry entry lookup
    registry = _read_merge_requests()
    registry_by_id = {m["id"]: m for m in registry}
    
    all_mrs = []
    processed_mr_ids = set()
    
    # 1. Add MRs from the central registry
    for mr in registry:
        if filter_mode == "closed" and mr.get("status") not in closed_statuses:
            continue
        if filter_mode == "open" and mr.get("status") in closed_statuses:
            continue
        mr["source"] = "registry"
        all_mrs.append(mr)
        processed_mr_ids.add(mr["id"])

    # Sort by update time, then ID
    all_mrs.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or x.get("assigned_at") or "", reverse=True)
    
    return jsonify(all_mrs)


@app.route("/api/preferences", methods=["GET"])
def api_preferences_get():
    return jsonify(_read_preferences())


@app.route("/api/preferences", methods=["POST"])
def api_preferences_update():
    data = request.get_json(force=True) or {}
    _write_preferences(data)
    return jsonify(data)


def _get_build_info() -> dict:
    build_info_path = Path(__file__).resolve().parent / "build-info.json"
    if not build_info_path.exists():
        return {}
    try:
        return json.loads(build_info_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


@app.route("/version", methods=["GET"])
@app.route("/api/version", methods=["GET"])
def api_version():
    build_info = _get_build_info()
    return jsonify({
        "version": build_info.get("version") or "unknown",
        "branch": build_info.get("branch") or "unknown",
        "commit": build_info.get("commit") or "",
        "built_at": build_info.get("built_at"),
    })


# ── Health Checks ─────────────────────────────────────────────────────────────

@app.route("/health/live", methods=["GET"])
def health_live():
    build_info = _get_build_info()
    return jsonify({"status": "ok", "version": build_info.get("version") or "unknown"})


@app.route("/health/ready", methods=["GET"])
def health_ready():
    # Check if database is accessible
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        build_info = _get_build_info()
        return jsonify({"status": "ok", "version": build_info.get("version") or "unknown"})
    except Exception as e:
        build_info = _get_build_info()
        return jsonify({"status": "error", "message": str(e), "version": build_info.get("version") or "unknown"}), 503
    finally:
        if conn:
            release_connection(conn)


# ── Utility Routes ────────────────────────────────────────────────────────────

@app.route("/api/utils/markdown", methods=["POST"])
def api_utils_markdown():
    data, err = _safe_json()
    if err:
        return err
    text = data.get("text")
    if not text or not isinstance(text, str):
        return jsonify({"error": "Text is required (string)"}), 400
    # Cap input to 500KB to prevent abuse
    text = text[:512_000]
    from commonmark import Parser
    parser = Parser()
    ast = parser.parse(text)
    from commonmark.render.html import HtmlRenderer
    renderer = HtmlRenderer()
    html_output = renderer.render(ast)
    return jsonify({"html": html_output})


# ── Environment Information ───────────────────────────────────────────────

@app.route("/api/environment", methods=["GET"])
def api_environment_info():
    """Return environment information (OS, Python version, etc.)."""
    return jsonify({
        "os": os.name,
        "platform": sys.platform,
        "python_version": sys.version,
        "project_dir": os.path.abspath(os.getcwd()),
        "meta_dir": META_DIR,
        "in_docker": _IN_DOCKER,
    })

# ── LLM Provider Endpoints ───────────────────────────────────────────────
# These endpoints are for managing LLM providers and their data.

def _read_llm_providers():
    """Read LLM providers from SQLite."""
    try:
        providers = LLMProviderDB.list_all()
        # Ensure consistent structure and defaults
        normalized = []
        for p in providers:
            normalized_p = {
                "id": p.get("provider_id"),
                "provider_id": p.get("provider_id"),
                "name": p.get("name"),
                "description": p.get("description", ""),
                "status": p.get("status", "enabled"),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at"),
            }
            normalized.append(normalized_p)
        return normalized
    except Exception as e:
        logger.error(f"Error reading LLM providers: {e}")
        return []

def _write_llm_providers(providers):
    """Write LLM providers to SQLite."""
    try:
        for p in providers:
            provider_id = p.get("id") or p.get("provider_id")
            if not provider_id:
                continue
            
            existing = LLMProviderDB.get_by_id(provider_id)
            if existing:
                LLMProviderDB.update(provider_id, {
                    "name": p.get("name", "Untitled"),
                    "description": p.get("description", ""),
                    "status": p.get("status", "enabled"),
                })
            else:
                LLMProviderDB.create({
                    "provider_id": provider_id,
                    "name": p.get("name", "Untitled"),
                    "description": p.get("description", ""),
                    "status": p.get("status", "enabled"),
                })
    except Exception as e:
        logger.error(f"Error writing LLM providers: {e}")


@app.route("/api/llm-providers", methods=["GET"])
def api_llm_providers_list():
    providers = _read_llm_providers()
    return jsonify(providers)


@app.route("/api/llm-providers", methods=["POST"])
def api_llm_providers_create():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    provider_id = _unique_ts_id()
    p = {
        "id": provider_id,
        "name": name,
        "description": (data.get("description") or "").strip(),
        "status": "enabled",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    LLMProviderDB.create({
        "provider_id": provider_id,
        "name": p["name"],
        "description": p["description"],
        "status": p["status"],
    })
    return jsonify(p)


@app.route("/api/llm-providers/<provider_id>", methods=["PUT"])
def api_llm_providers_update(provider_id):
    data = request.get_json(force=True) or {}
    providers = _read_llm_providers()
    for p in providers:
        if p["id"] == provider_id:
            if "name" in data:
                p["name"] = data["name"].strip()
            if "description" in data:
                p["description"] = data["description"].strip()
            if "status" in data:
                p["status"] = data["status"].strip()
            _write_llm_providers(providers)
            return jsonify(p)
    return jsonify({"error": "Provider not found"}), 404

@app.route("/api/llm-providers/<provider_id>", methods=["DELETE"])
def api_llm_providers_delete(provider_id):
    success = LLMProviderDB.delete(provider_id)
    if not success:
        return jsonify({"error": "Provider not found"}), 404
    return jsonify({"deleted": provider_id})


# ── Model Registration Endpoints ──────────────────────────────────────────────

def _read_models():
    """Read models from SQLite."""
    try:
        models = ModelDB.list_all()
        # Ensure consistent structure and defaults
        normalized = []
        for m in models:
            normalized_m = {
                "id": m.get("model_id"),
                "model_id": m.get("model_id"),
                "provider_id": m.get("provider_id"),
                "name": m.get("name"),
                "description": m.get("description", ""),
                "status": m.get("status", "enabled"),
                "created_at": m.get("created_at"),
                "updated_at": m.get("updated_at"),
            }
            normalized.append(normalized_m)
        return normalized
    except Exception as e:
        logger.error(f"Error reading models: {e}")
        return []

def _write_models(models):
    """Write models to SQLite."""
    try:
        for m in models:
            model_id = m.get("id") or m.get("model_id")
            if not model_id:
                continue
            
            existing = ModelDB.get_by_id(model_id)
            if existing:
                ModelDB.update(model_id, {
                    "name": m.get("name", "Untitled"),
                    "description": m.get("description", ""),
                    "status": m.get("status", "enabled"),
                })
            else:
                ModelDB.create({
                    "model_id": model_id,
                    "provider_id": m.get("provider_id"),
                    "name": m.get("name", "Untitled"),
                    "description": m.get("description", ""),
                    "status": m.get("status", "enabled"),
                })
    except Exception as e:
        logger.error(f"Error writing models: {e}")


@app.route("/api/models", methods=["GET"])
def api_models_list():
    """List all registered models."""
    models = _read_models()
    providers = _read_llm_providers()
    provider_map = {p["id"]: p["name"] for p in providers}
    for m in models:
        m["provider_name"] = provider_map.get(m.get("provider_id"), "Unknown")
    return jsonify(models)


@app.route("/api/models", methods=["POST"])
def api_models_create():
    """Register a new model."""
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    provider_id = data.get("provider_id")
    if not name or not provider_id:
        return jsonify({"error": "name and provider_id are required"}), 400
    model_id = _unique_ts_id()
    m = {
        "id": model_id,
        "model_id": model_id,
        "provider_id": provider_id,
        "name": name,
        "description": (data.get("description") or "").strip(),
        "status": "enabled",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ModelDB.create({
        "model_id": model_id,
        "provider_id": provider_id,
        "name": m["name"],
        "description": m["description"],
        "status": m["status"],
    })
    return jsonify(m)


@app.route("/api/models/<model_id>", methods=["PUT"])
def api_models_update(model_id):
    """Update an existing model."""
    data = request.get_json(force=True) or {}
    models = _read_models()
    for m in models:
        if m["id"] == model_id:
            if "name" in data:
                m["name"] = data["name"].strip()
            if "description" in data:
                m["description"] = data["description"].strip()
            if "status" in data:
                m["status"] = data["status"].strip()
            _write_models(models)
            return jsonify(m)
    return jsonify({"error": "Model not found"}), 404

@app.route("/api/models/<model_id>", methods=["DELETE"])
def api_models_delete(model_id):
    """Delete a model."""
    success = ModelDB.delete(model_id)
    if not success:
        return jsonify({"error": "Model not found"}), 404
    return jsonify({"deleted": model_id})


# ── LLM Provider & Model Configuration Endpoints ──────────────────────────────
# These are for managing API keys, model endpoints, etc.

# Placeholder for actual config loading/saving logic
def _read_llm_config():
    """Read LLM configuration from a file or DB."""
    # In a real app, this would load from a file (e.g., config.yaml) or DB.
    # For now, return dummy data.
    return {
        "copilot": {"api_key": "dummy_copilot_key", "endpoint": "https://api.copilot.com/v1"},
        "claude": {"api_key": "dummy_claude_key", "endpoint": "https://api.claude.com/v1"},
        "gemini": {"api_key": "dummy_gemini_key", "endpoint": "https://generativelanguage.googleapis.com/v1beta/models"},
        "codex": {"api_key": "dummy_codex_key", "endpoint": "https://api.codex.ai/v1"},
        "savant": {"api_key": "dummy_savant_key", "endpoint": "https://api.savant.ai/v1"},
    }

def _write_llm_config(config):
    """Write LLM configuration."""
    # In a real app, this would save to a file or DB.
    pass


@app.route("/api/llm-config", methods=["GET"])
def api_llm_config_get():
    """Get LLM configuration."""
    config = _read_llm_config()
    # In a real app, sensitive info like API keys would be masked or omitted.
    return jsonify(config)


@app.route("/api/llm-config", methods=["POST"])
def api_llm_config_update():
    """Update LLM configuration."""
    data = request.get_json(force=True) or {}
    config = _read_llm_config()
    for key, value in data.items():
        if key in config:
            config[key] = value
    _write_llm_config(config)
    return jsonify({"ok": True, "config": config})


# ── Helper Functions for MR & Jira Integration ───────────────────────────────

# Cache for merge requests registry to avoid frequent file reads
_mr_registry_cache = {"data": None, "timestamp": 0}
_mr_registry_lock = Lock()
_MR_REGISTRY_FILE = os.path.join(META_DIR, "merge_requests.json")
_MAX_AGE_SECONDS = 5 * 60  # Cache for 5 minutes

def _read_merge_requests():
    """Read MRs from the central registry file (thread-safe)."""
    with _mr_registry_lock:
        now = time.time()
        if _mr_registry_cache["data"] is not None and (now - _mr_registry_cache["timestamp"]) < _MAX_AGE_SECONDS:
            return _mr_registry_cache["data"]
        
        if not os.path.exists(_MR_REGISTRY_FILE):
            _mr_registry_cache["data"] = []
            _mr_registry_cache["timestamp"] = now
            return []
        
        try:
            with open(_MR_REGISTRY_FILE, "r") as f:
                data = json.load(f)
            _mr_registry_cache["data"] = data
            _mr_registry_cache["timestamp"] = now
            return data
        except Exception as e:
            logger.error(f"Error reading MR registry {_MR_REGISTRY_FILE}: {e}")
            return []

def _write_merge_requests(mrs):
    """Write MRs to the central registry file (thread-safe)."""
    with _mr_registry_lock:
        try:
            with open(_MR_REGISTRY_FILE, "w") as f:
                json.dump(mrs, f, indent=2)
            # Invalidate cache
            _mr_registry_cache["data"] = None
        except Exception as e:
            logger.error(f"Error writing MR registry {_MR_REGISTRY_FILE}: {e}")


def _parse_mr_url(url):
    """Parse GitLab/GitHub MR URL to extract project_id and mr_iid."""
    # Regex to capture project ID and MR IID from common GitLab/GitHub URLs
    # Example GitLab: https://gitlab.com/mygroup/myproject/-/merge_requests/123
    # Example GitHub: https://github.com/myorg/myrepo/pull/456
    match = re.search(r'(?:gitlab\.com|github\.com)/([^/]+)/([^/]+?)(?:/-/merge_requests/|/pull/)([\d]+)', url)
    if match:
        project_id = f"{match.group(1)}/{match.group(2)}"
        mr_iid = match.group(3)
        return project_id, mr_iid
    return None, None

def _auto_detect_mr_role(mr_entry):
    """Auto-detect role based on MR author and user preferences."""
    try:
        prefs = _read_preferences()
        my_name = (prefs.get("name") or "").strip()
        mr_author = (mr_entry.get("author") or "").strip()
        if my_name and mr_author and my_name.lower() == mr_author.lower():
            return "author"
    except Exception:
        pass
    return "reviewer"  # Default role


# --- Session project files (filesystem-based, stub for Docker) --------------

def _project_files_handler(session_id, provider=None):
    """Return project files for a session.

    Uses provider-aware session directory resolution.
    """
    target = _resolve_session_dir(session_id, provider)
    if not target:
        return jsonify({"files": [], "session_id": session_id})

    files = []
    cat_map = {
        "plan.md": "plan",
        "PLAN.md": "plan",
        "RESEARCH.md": "research",
        "DISCOVERY.md": "research",
    }
    for root, _dirs, fnames in os.walk(target):
        for fn in fnames:
            fpath = os.path.join(root, fn)
            rel = os.path.relpath(fpath, target)
            try:
                sz = os.path.getsize(fpath)
            except OSError:
                sz = 0
            cat = cat_map.get(fn, "checkpoint" if "checkpoint" in rel.lower() else "file")
            files.append({"path": rel, "name": fn, "category": cat, "size": sz})

    return jsonify({"files": files, "session_id": session_id})


@app.route("/api/session/<session_id>/project-files", methods=["GET"])
def api_session_project_files(session_id):
    return _project_files_handler(session_id, "copilot")

@app.route("/api/claude/session/<session_id>/project-files", methods=["GET"])
def api_claude_session_project_files(session_id):
    return _project_files_handler(session_id, "claude")

@app.route("/api/codex/session/<session_id>/project-files", methods=["GET"])
def api_codex_session_project_files(session_id):
    return _project_files_handler(session_id, "codex")

@app.route("/api/gemini/session/<session_id>/project-files", methods=["GET"])
def api_gemini_session_project_files(session_id):
    return _project_files_handler(session_id, "gemini")

@app.route("/api/savant/session/<session_id>/project-files", methods=["GET"])
def api_savant_session_project_files(session_id):
    return _project_files_handler(session_id, "savant")


# --- Session file read (read individual file from session dir) --------------

def _resolve_session_dir(session_id, provider=None):
    """Find the session directory based on provider."""
    env_map = {
        "copilot": "SESSION_DIR",
        "claude": "CLAUDE_DIR",
        "codex": "CODEX_DIR",
        "gemini": "GEMINI_DIR",
        "savant": "SAVANT_DIR",
    }
    env_key = env_map.get(provider, "SESSION_DIR")
    base = os.environ.get(env_key, "")
    if not base or base == "/nonexistent" or not os.path.isdir(base):
        return None
    target = os.path.join(base, session_id)
    return target if os.path.isdir(target) else None


def _session_file_handler(session_id, provider=None):
    """Read a file from a session directory."""
    file_path = request.args.get("path", "")
    if not file_path:
        return jsonify({"error": "Missing path parameter"}), 400

    # Prevent path traversal — reject suspicious patterns
    if ".." in file_path or file_path.startswith("/") or "\x00" in file_path:
        return jsonify({"error": "Invalid path"}), 400

    sdir = _resolve_session_dir(session_id, provider)
    if not sdir:
        return jsonify({"error": "Session directory not available", "content": ""})

    # Resolve both sides to real paths BEFORE joining to block symlink escapes
    real_sdir = os.path.realpath(sdir)
    full = os.path.realpath(os.path.join(real_sdir, file_path))
    if not full.startswith(real_sdir + os.sep) and full != real_sdir:
        return jsonify({"error": "Invalid path"}), 400

    if not os.path.isfile(full):
        return jsonify({"error": "File not found", "content": ""})

    try:
        sz = os.path.getsize(full)
        truncated = sz > 512_000  # 500KB limit
        with open(full, "r", errors="replace") as f:
            content = f.read(512_000)
        return jsonify({
            "content": content,
            "path": file_path,
            "size": sz,
            "truncated": truncated,
            "host_path": full,
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "content": ""})


def _session_file_raw_handler(session_id, provider=None):
    """Return raw file content with appropriate content-type."""
    file_path = request.args.get("path", "")
    if not file_path or ".." in file_path or file_path.startswith("/") or "\x00" in file_path:
        return "Invalid path", 400

    sdir = _resolve_session_dir(session_id, provider)
    if not sdir:
        return "Session directory not available", 404

    real_sdir = os.path.realpath(sdir)
    full = os.path.realpath(os.path.join(real_sdir, file_path))
    if (not full.startswith(real_sdir + os.sep) and full != real_sdir) or not os.path.isfile(full):
        return "File not found", 404

    from flask import send_file
    return send_file(full)


def _session_file_write_handler(session_id, provider=None):
    """Write content to a file in a session directory."""
    data = request.get_json(force=True, silent=True) or {}
    file_path = data.get("path", "")
    content = data.get("content")
    if not file_path or content is None:
        return jsonify({"ok": False, "error": "Missing path or content"}), 400
    if ".." in file_path or file_path.startswith("/") or "\x00" in file_path:
        return jsonify({"ok": False, "error": "Invalid path"}), 400

    sdir = _resolve_session_dir(session_id, provider)
    if not sdir:
        return jsonify({"ok": False, "error": "Session directory not available"})

    real_sdir = os.path.realpath(sdir)
    full = os.path.realpath(os.path.join(real_sdir, file_path))
    if not full.startswith(real_sdir + os.sep) and full != real_sdir:
        return jsonify({"ok": False, "error": "Invalid path"}), 400

    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "File not found"})

    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"ok": True, "path": file_path, "host_path": full})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})
@app.route("/api/session/<session_id>/file", methods=["GET", "PUT"])
def api_session_file(session_id):
    if request.method == "PUT":
        return _session_file_write_handler(session_id, "copilot")
    return _session_file_handler(session_id, "copilot")

@app.route("/api/session/<session_id>/file/raw", methods=["GET"])
def api_session_file_raw(session_id):
    return _session_file_raw_handler(session_id, "copilot")

@app.route("/api/claude/session/<session_id>/file", methods=["GET", "PUT"])
def api_claude_session_file(session_id):
    if request.method == "PUT":
        return _session_file_write_handler(session_id, "claude")
    return _session_file_handler(session_id, "claude")

@app.route("/api/claude/session/<session_id>/file/raw", methods=["GET"])
def api_claude_session_file_raw(session_id):
    return _session_file_raw_handler(session_id, "claude")

@app.route("/api/codex/session/<session_id>/file", methods=["GET", "PUT"])
def api_codex_session_file(session_id):
    if request.method == "PUT":
        return _session_file_write_handler(session_id, "codex")
    return _session_file_handler(session_id, "codex")

@app.route("/api/codex/session/<session_id>/file/raw", methods=["GET"])
def api_codex_session_file_raw(session_id):
    return _session_file_raw_handler(session_id, "codex")

@app.route("/api/gemini/session/<session_id>/file", methods=["GET", "PUT"])
def api_gemini_session_file(session_id):
    if request.method == "PUT":
        return _session_file_write_handler(session_id, "gemini")
    return _session_file_handler(session_id, "gemini")

@app.route("/api/gemini/session/<session_id>/file/raw", methods=["GET"])
def api_gemini_session_file_raw(session_id):
    return _session_file_raw_handler(session_id, "gemini")

@app.route("/api/savant/session/<session_id>/file", methods=["GET", "PUT"])
def api_savant_session_file(session_id):
    if request.method == "PUT":
        return _session_file_write_handler(session_id, "savant")
    return _session_file_handler(session_id, "savant")

@app.route("/api/savant/session/<session_id>/file/raw", methods=["GET"])
def api_savant_session_file_raw(session_id):
    return _session_file_raw_handler(session_id, "savant")


# --- Session git changes -------------------------------------------------------

def _get_session_git_context(session_id, provider=None):
    """Return (git_root, session_start, session_end) for a session.

    Tries workspace.yaml (copilot), provider-specific config files,
    then falls back to the in-memory background cache.
    """
    import subprocess

    git_root = None
    session_start = None
    session_end = None

    sdir = _resolve_session_dir(session_id, provider)
    if sdir and os.path.isdir(sdir):
        # Copilot: workspace.yaml has cwd/git_root/timestamps
        ws_yaml = os.path.join(sdir, "workspace.yaml")
        if os.path.isfile(ws_yaml):
            try:
                import yaml
                with open(ws_yaml) as fh:
                    ws = yaml.safe_load(fh) or {}
                git_root = ws.get("git_root") or ws.get("cwd")
                session_start = ws.get("created_at")
                session_end = ws.get("updated_at")
            except Exception:
                pass

        # Claude/Codex/Gemini: look for a settings or config JSON
        if not git_root:
            for cfg_name in ("settings.json", "config.json", "session.json"):
                cfg_path = os.path.join(sdir, cfg_name)
                if os.path.isfile(cfg_path):
                    try:
                        with open(cfg_path) as fh:
                            cfg = json.load(fh)
                        git_root = cfg.get("git_root") or cfg.get("cwd") or cfg.get("projectPath")
                        session_start = session_start or cfg.get("created_at") or cfg.get("startTime")
                        session_end = session_end or cfg.get("updated_at") or cfg.get("endTime")
                    except Exception:
                        pass
                    if git_root:
                        break

    # Fall back to background cache which is populated by session parsers
    if not git_root:
        with _bg_lock:
            for sessions in _bg_cache.values():
                if not isinstance(sessions, list):
                    continue
                for s in sessions:
                    sid = s.get("id") or s.get("session_id")
                    if sid == session_id:
                        git_root = s.get("git_root") or s.get("cwd")
                        session_start = session_start or s.get("created_at")
                        session_end = session_end or s.get("updated_at")
                        break
                if git_root:
                    break

    if not git_root:
        return None, None, None

    # Expand ~ and resolve to real git root if needed
    git_root = os.path.expanduser(git_root)
    if not os.path.isdir(git_root):
        return None, None, None

    # Walk up to find actual .git dir if path is a subdirectory
    if not os.path.isdir(os.path.join(git_root, ".git")):
        try:
            import subprocess
            res = subprocess.run(
                ["git", "-C", git_root, "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0:
                git_root = res.stdout.strip()
        except Exception:
            return None, None, None

    return git_root, session_start, session_end


def _split_diff_by_file(unified_diff):
    """Split a unified diff string into a dict of {file_path: diff_chunk}."""
    result = {}
    current_file = None
    current_lines = []

    for line in unified_diff.split("\n"):
        if line.startswith("diff --git "):
            if current_file is not None:
                result[current_file] = "\n".join(current_lines)
            current_file = None
            current_lines = [line]
        elif line.startswith("+++ b/"):
            current_file = line[6:]
            current_lines.append(line)
        elif line.startswith("+++ /dev/null"):
            # Deleted file — key by old path captured from "--- a/<path>"
            current_lines.append(line)
        elif current_file is not None:
            current_lines.append(line)
        else:
            current_lines.append(line)

    if current_file is not None:
        result[current_file] = "\n".join(current_lines)
    return result


def _enrich_commit(git_root, commit):
    """Add insertion/deletion stats and per-file diffs to a commit object."""
    import subprocess, re as _re

    sha = commit.get("full_sha", "")
    if not sha:
        return

    parent_ref = f"{sha}^"

    # Stat: insertions + deletions
    try:
        stat = subprocess.run(
            ["git", "-C", git_root, "diff", parent_ref, sha, "--stat", "--no-color"],
            capture_output=True, text=True, timeout=10
        )
        if stat.returncode == 0:
            for line in stat.stdout.splitlines():
                ins = _re.search(r"(\d+) insertion", line)
                dels = _re.search(r"(\d+) deletion", line)
                if ins:
                    commit["insertions"] = int(ins.group(1))
                if dels:
                    commit["deletions"] = int(dels.group(1))
    except Exception:
        pass

    # Per-file unified diffs (capped at 100 KB total)
    MAX_DIFF = 100_000
    try:
        diff = subprocess.run(
            ["git", "-C", git_root, "diff", parent_ref, sha, "--no-color", "-U3"],
            capture_output=True, text=True, timeout=15
        )
        if diff.returncode == 0:
            raw = diff.stdout
            truncated = len(raw) > MAX_DIFF
            if truncated:
                raw = raw[:MAX_DIFF] + "\n... (diff truncated)\n"
            file_diffs = _split_diff_by_file(raw)
            for f in commit.get("files", []):
                f["diff"] = file_diffs.get(f["path"], "")
    except Exception:
        pass


def _get_git_commits(git_root, session_start=None, session_end=None):
    """Return list of commit objects with files and diffs."""
    import subprocess

    log_cmd = [
        "git", "-C", git_root, "log",
        "--format=COMMIT_SEP%H|%s|%aI|%aN|%D",
        "--name-status",
        "-n", "100",
    ]

    def _iso_to_git(ts):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None

    if session_start:
        g_ts = _iso_to_git(session_start)
        if g_ts:
            log_cmd.extend(["--since", g_ts])
    if session_end:
        g_ts = _iso_to_git(session_end)
        if g_ts:
            # Add a 5-min buffer so commits right at session end are included
            try:
                dt = datetime.fromisoformat(session_end.replace("Z", "+00:00"))
                dt = dt + timedelta(minutes=5)
                log_cmd.extend(["--until", dt.strftime("%Y-%m-%dT%H:%M:%S")])
            except Exception:
                log_cmd.extend(["--until", g_ts])

    try:
        res = subprocess.run(log_cmd, capture_output=True, text=True, timeout=20)
    except Exception:
        return []

    if res.returncode != 0:
        return []

    commits = []
    current = None
    status_map = {
        "M": "modified", "A": "added", "D": "deleted",
        "R": "renamed", "C": "copied", "T": "typechange",
    }

    for line in res.stdout.splitlines():
        if line.startswith("COMMIT_SEP"):
            if current:
                commits.append(current)
            parts = line[10:].split("|", 4)
            sha = parts[0] if len(parts) > 0 else ""
            msg = parts[1] if len(parts) > 1 else ""
            ts  = parts[2] if len(parts) > 2 else ""
            author = parts[3] if len(parts) > 3 else ""
            refs = parts[4] if len(parts) > 4 else ""
            branch = ""
            for ref in refs.split(","):
                ref = ref.strip()
                if ref.startswith("HEAD -> "):
                    branch = ref[8:]
                    break
                if ref.startswith("origin/") and not branch:
                    branch = ref[7:]
            current = {
                "sha": sha[:8],
                "full_sha": sha,
                "message": msg,
                "timestamp": ts,
                "author": author,
                "branch": branch,
                "files": [],
                "insertions": 0,
                "deletions": 0,
                "files_changed": 0,
            }
        elif current and "\t" in line:
            parts = line.split("\t", 2)
            st_char = parts[0][0] if parts[0] else "M"
            fpath = parts[1] if len(parts) > 1 else ""
            current["files"].append({
                "path": fpath,
                "status": status_map.get(st_char, "modified"),
                "diff": "",
            })
            current["files_changed"] += 1

    if current:
        commits.append(current)

    # Enrich each commit with stats + diffs
    for c in commits:
        _enrich_commit(git_root, c)

    return commits


def _build_file_summary(commits):
    """Aggregate file change counts across all commits."""
    counts = {}
    for c in commits:
        for f in c.get("files", []):
            p = f["path"]
            if p not in counts:
                counts[p] = {"path": p, "creates": 0, "edits": 0}
            if f["status"] == "added":
                counts[p]["creates"] += 1
            else:
                counts[p]["edits"] += 1
    return sorted(counts.values(), key=lambda x: x["creates"] + x["edits"], reverse=True)


def _git_changes_handler(session_id, provider=None):
    """Return real git changes for a session using git CLI."""
    git_root, session_start, session_end = _get_session_git_context(session_id, provider)

    if not git_root:
        return jsonify({
            "commits": [],
            "git_commands": [],
            "file_summary": [],
            "session_id": session_id,
            "error": "Could not resolve git root for this session",
        })

    commits = _get_git_commits(git_root, session_start, session_end)
    file_summary = _build_file_summary(commits)

    # Strip full diffs from file_summary (diffs live on commit.files[].diff)
    return jsonify({
        "commits": commits,
        "git_commands": [],
        "file_summary": file_summary,
        "session_id": session_id,
        "git_root": git_root,
    })

@app.route("/api/session/<session_id>/git-changes", methods=["GET"])
def api_session_git_changes(session_id):
    return _git_changes_handler(session_id, "copilot")

@app.route("/api/claude/session/<session_id>/git-changes", methods=["GET"])
def api_claude_session_git_changes(session_id):
    return _git_changes_handler(session_id, "claude")

@app.route("/api/codex/session/<session_id>/git-changes", methods=["GET"])
def api_codex_session_git_changes(session_id):
    return _git_changes_handler(session_id, "codex")

@app.route("/api/gemini/session/<session_id>/git-changes", methods=["GET"])
def api_gemini_session_git_changes(session_id):
    return _git_changes_handler(session_id, "gemini")

@app.route("/api/savant/session/<session_id>/git-changes", methods=["GET"])
def api_savant_session_git_changes(session_id):
    return _git_changes_handler(session_id, "savant")


# --- File diff (original vs modified content for Monaco diff editor) ---------

def _file_diff_handler(session_id, provider=None):
    """Return original and modified content for a file for the Monaco diff editor."""
    import subprocess
    file_path = request.args.get("path", "").strip()
    if not file_path:
        return jsonify({"error": "path is required"}), 400

    git_root, session_start, session_end = _get_session_git_context(session_id, provider)
    if not git_root:
        return jsonify({"error": "Could not resolve git root for this session"}), 404

    # Resolve full path on disk
    if os.path.isabs(file_path):
        full_path = file_path
    else:
        full_path = os.path.join(git_root, file_path)

    # Get current (modified) content
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            modified = f.read()
    except FileNotFoundError:
        modified = ""  # file was deleted during session
    except Exception as e:
        return jsonify({"error": f"Cannot read file: {e}"}), 500

    # Get original content from git: content before the first session commit that touched this file
    commits = _get_git_commits(git_root, session_start, session_end)

    original = ""
    found = False
    for commit in reversed(commits):  # oldest first
        for f in commit.get("files", []):
            if f.get("path") == file_path:
                parent_ref = f"{commit['full_sha']}^"
                try:
                    res = subprocess.run(
                        ["git", "-C", git_root, "show", f"{parent_ref}:{file_path}"],
                        capture_output=True, text=True, timeout=10, errors="replace",
                    )
                    if res.returncode == 0:
                        original = res.stdout
                    # returncode != 0 means file didn't exist before → original stays ""
                except Exception:
                    pass
                found = True
                break
        if found:
            break

    if not found:
        # File wasn't touched by any session commit — show current = original (no changes)
        original = modified

    return jsonify({
        "path": file_path,
        "original": original,
        "modified": modified,
        "git_root": git_root,
    })


@app.route("/api/session/<session_id>/file-diff", methods=["GET"])
def api_session_file_diff(session_id):
    return _file_diff_handler(session_id, "copilot")

@app.route("/api/claude/session/<session_id>/file-diff", methods=["GET"])
def api_claude_session_file_diff(session_id):
    return _file_diff_handler(session_id, "claude")

@app.route("/api/codex/session/<session_id>/file-diff", methods=["GET"])
def api_codex_session_file_diff(session_id):
    return _file_diff_handler(session_id, "codex")

@app.route("/api/gemini/session/<session_id>/file-diff", methods=["GET"])
def api_gemini_session_file_diff(session_id):
    return _file_diff_handler(session_id, "gemini")

@app.route("/api/savant/session/<session_id>/file-diff", methods=["GET"])
def api_savant_session_file_diff(session_id):
    return _file_diff_handler(session_id, "savant")


# --- Workspace session files (aggregate across linked sessions) -------------

@app.route("/api/workspaces/<workspace_id>/session-files", methods=["GET"])
def api_workspace_session_files(workspace_id):
    """Return session files grouped by session for a workspace.

    Scans session directories for linked sessions using per-provider env vars.
    """
    # Get linked sessions for this workspace
    from db.workspace_session_links import WorkspaceSessionLinkDB
    try:
        links = WorkspaceSessionLinkDB.list_by_workspace(workspace_id)
    except Exception:
        links = []

    groups = []
    cat_map = {
        "plan.md": "plan",
        "PLAN.md": "plan",
        "RESEARCH.md": "research",
        "DISCOVERY.md": "research",
    }
    for link in links:
        sid = link.get("session_id", "")
        provider = link.get("provider", "copilot")
        # Resolve session dir using provider-specific env var
        target = _resolve_session_dir(sid, provider)
        if not target:
            continue

        files = []
        for root, _dirs, fnames in os.walk(target):
            for fn in fnames:
                fpath = os.path.join(root, fn)
                rel = os.path.relpath(fpath, target)
                try:
                    sz = os.path.getsize(fpath)
                except OSError:
                    sz = 0
                cat = cat_map.get(fn, "checkpoint" if "checkpoint" in rel.lower() else "file")
                files.append({"path": rel, "name": fn, "category": cat, "size": sz})

        if files:
            groups.append({
                "session_id": sid,
                "provider": provider,
                "summary": f"{sid[:12]}… ({len(files)} files)",
                "file_count": len(files),
                "files": files,
            })

    return jsonify({"groups": groups, "workspace_id": workspace_id})


if __name__ == "__main__":
    # Seed default users on startup (idempotent)
    try:
        UserDB.seed_defaults()
    except Exception as e:
        logger.warning(f"Could not seed default users: {e}")

    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8090"))
    app.run(host=host, port=port, debug=False)
    
