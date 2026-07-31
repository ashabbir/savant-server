import os
import logging
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from postgres_client import init_schema
from db.users import UserDB
from utils.auth import ALLOWED_SAVANT_APPS
from utils.request_logging import install_request_logging

SAVANT_DIR = os.environ.get("SAVANT_DIR", os.path.expanduser("~/.gemini/antigravity-cli"))
SAVANT_SESSIONS_DIR = os.path.join(SAVANT_DIR, "brain")
SAVANT_META_DIR = os.path.join(SAVANT_DIR, ".savant-meta")
SAVANT_STATE_DB = os.path.join(SAVANT_DIR, "state.db")

CLAUDE_DIR = os.environ.get("CLAUDE_DIR", os.path.expanduser("~/.config/claude"))
CODEX_DIR = os.environ.get("CODEX_DIR", os.path.expanduser("~/.codex"))
CODEX_SESSIONS_DIR = os.path.join(CODEX_DIR, "sessions")
GEMINI_DIR = os.environ.get("GEMINI_DIR", os.path.expanduser("~/.gemini"))
GEMINI_CHATS_DIR = os.path.join(GEMINI_DIR, "chats")
AGY_DIR = os.environ.get("AGY_DIR", os.path.expanduser("~/.agy"))
META_DIR = os.path.join(SAVANT_DIR, ".meta")
SESSION_DIR = SAVANT_SESSIONS_DIR

_bg_cache = {}


from abilities.routes import abilities_bp
from context.routes import context_bp
from knowledge.routes import knowledge_bp
from tools.routes import tools_bp
from reminders.routes import reminders_bp
from code_intelligence.routes import code_intelligence_bp
from abilities.skills_routes import skills_bp
from abilities.default_skills import ensure_default_skills

from routes import (
    users_bp,
    workspaces_bp,
    tasks_bp,
    jira_mr_bp,
    preferences_bp,
    jobs_system_bp,
    sessions_bp,
)
from routes.tasks import _next_available_workday
from routes.jobs_system import _list_mcp_tools
from routes.sessions import (
    savant_sessions_list,
    savant_session_detail,
    savant_session_conversation,
    savant_session_workspace_assign,
    savant_session_star_toggle,
    savant_session_archive_toggle,
    savant_session_rename,
    savant_session_notes_crud,
    savant_session_project_files,
    savant_session_git_changes,
    savant_search,
    savant_convert_prompt,
    savant_usage,
    savant_session_delete,
    savant_bulk_delete,
    _detect_sessions,
    _collect_workspace_sessions,
    _collect_session_artifacts,
)
from routes.jira_mr import api_all_mrs, api_all_jira, api_merge_requests, api_jira_tickets
from routes.workspaces import api_workspaces, api_workspace_detail, api_workspaces_search, api_workspace_search
from routes.users import api_users, api_auth_validate
from routes.preferences import api_preferences, api_models, api_llm_providers
from utils.session_parser import (
    savant_parse_full_conversation,
    savant_get_session_detail,
    _savant_build_session_chains,
)

app = Flask(__name__)
CORS(
    app,
    resources={r"/*": {"origins": "*"}},
    allow_headers=["Content-Type", "X-API-Key", "X-App-Name", "X-Savant-App", "Authorization"],
)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB limit

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Request lifecycle logging ─────────────────────────────────────────────────
# Emits structured per-request log lines: method, path, status, user, app, ms.
install_request_logging(app)

# Initialize database on startup
with app.app_context():
    try:
        init_schema()
        ensure_default_skills()
        if os.environ.get("SAVANT_EXTERNAL_PERIODIC_RUNNER") != "1":
            from context.periodic_runner import start_periodic_runner
            start_periodic_runner()
        if os.environ.get("SAVANT_EXTERNAL_KG_MAINTENANCE") != "1":
            from knowledge.maintenance import start_maintenance_scheduler
            start_maintenance_scheduler()
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


# ── Blueprint Registrations ───────────────────────────────────────────────────
app.register_blueprint(abilities_bp)
app.register_blueprint(context_bp)
app.register_blueprint(knowledge_bp)
app.register_blueprint(tools_bp)
app.register_blueprint(reminders_bp)
app.register_blueprint(code_intelligence_bp)
app.register_blueprint(skills_bp)

app.register_blueprint(users_bp)
app.register_blueprint(workspaces_bp)
app.register_blueprint(tasks_bp)
app.register_blueprint(jira_mr_bp)
app.register_blueprint(preferences_bp)
app.register_blueprint(jobs_system_bp)
app.register_blueprint(sessions_bp)

# Seed default users on startup
try:
    UserDB.seed_defaults()
except Exception as _seed_err:
    logger.warning(f"Could not seed default users: {_seed_err}")

# ── Auth middleware — resolve API key → g.user_id ────────────────────────────
_AUTH_SKIP_PREFIXES = (
    "/api/db/health", "/api/system/", "/api/mcp/",
    "/health/", "/static/",
)


def _is_auth_exempt(path: str) -> bool:
    """Return True for paths that skip API key authentication."""
    if not path.startswith("/api/"):
        return True
    return any(path.startswith(p) for p in _AUTH_SKIP_PREFIXES)


def _resolve_api_key() -> str:
    """Extract the API key from headers or query params."""
    return (
        request.headers.get("X-API-Key", "").strip()
        or request.args.get("api_key", "").strip()
    )


def _use_test_fallback() -> bool:
    """In TESTING mode, set a default user and return True."""
    if app.config.get("TESTING"):
        g.user_id = "ahmed"
        return True
    return False


def _validate_api_key(api_key: str):
    """Resolve API key to a user or return an error tuple."""
    user = UserDB.get_by_api_key(api_key)
    if not user:
        return None, (jsonify({"error": "Invalid API key."}), 401)
    if int(user.get("is_active", 1)) != 1:
        return None, (jsonify({"error": "User account is inactive."}), 401)
    return user, None


@app.before_request
def _authenticate():
    if request.method == "OPTIONS":
        return None

    path = request.path or "/"
    if _is_auth_exempt(path):
        g.user_id = ""
        return None

    api_key = _resolve_api_key()
    if not api_key:
        return None if _use_test_fallback() else (
            jsonify({"error": "API key required. Set X-API-Key header or api_key query param."}), 401
        )

    user, err = _validate_api_key(api_key)
    if err:
        return None if _use_test_fallback() else err

    g.user_id = user["user_id"]
    return None


def _resolve_app_name() -> str:
    """Extract the source application name from request headers."""
    return (
        request.headers.get("X-App-Name")
        or request.headers.get("X-Savant-App")
        or ""
    ).strip().lower()


@app.before_request
def _require_allowed_savant_app():
    if request.method == "OPTIONS" or not (request.path or "/").startswith("/api/"):
        return None
    if app.config.get("TESTING") or os.environ.get("SAVANT_DISABLE_APP_CHECK") == "1":
        return None

    app_name = _resolve_app_name()
    if not app_name or app_name not in ALLOWED_SAVANT_APPS:
        return jsonify({"error": "Access denied."}), 403
    return None


def _build_savant_usage():
    return {
        "totals": {
            "sessions": 1,
            "messages": 1,
            "turns": 3,
            "tool_calls": 2,
            "total_hours": 10.0,
            "avg_session_minutes": 15.0,
            "avg_tools_per_turn": 2.0,
            "avg_turns_per_message": 1.5,
            "events": 100,
        },
        "tools": [{"name": "read_file", "calls": 10}, {"name": "patch", "calls": 5}],
        "models": [{"name": "claude-opus-4.6", "calls": 5}],
        "daily": [{"date": "2026-04-15", "turns": 10}],
        "loading": False,
    }


@app.route("/savant/session/<session_id>", methods=["GET"])
def savant_detail_page(session_id):
    return "<html><body>Savant Session Detail</body></html>", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("FLASK_PORT", 8090)), debug=True)
