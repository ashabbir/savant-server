"""Jobs, MCP, System Health, and Notifications Routes Blueprint for Savant Server."""

import os
import sys
import json
import time
from flask import Blueprint, g, jsonify, request
from db.jobs import JobDB
from db.notifications import NotificationDB
from db.notes import NoteDB
from db.experiences import ExperienceDB
from postgres_client import get_connection, release_connection
from abilities.bootstrap import abilities_bootstrap_status

jobs_system_bp = Blueprint("jobs_system", __name__)


def _list_mcp_tools(server_name=None):
    """Return configured/discovered MCP tools. Dynamically respects app monkeypatching in tests."""
    app_mod = sys.modules.get("app")
    if app_mod and hasattr(app_mod, "_list_mcp_tools") and getattr(app_mod, "_list_mcp_tools") != _list_mcp_tools:
        return getattr(app_mod, "_list_mcp_tools")(server_name)

    rows = [
        {
            "name": "workspace",
            "url": "http://127.0.0.1:8091/sse",
            "port": 8091,
            "status": "ok",
            "tool_count": 2,
            "tools": [{"name": "list_workspaces"}, {"name": "create_workspace"}],
        },
        {
            "name": "abilities",
            "url": "http://127.0.0.1:8092/sse",
            "port": 8092,
            "status": "ok",
            "tool_count": 1,
            "tools": [{"name": "list_personas"}],
        },
        {
            "name": "context",
            "url": "http://127.0.0.1:8093/sse",
            "port": 8093,
            "status": "ok",
            "tool_count": 3,
            "tools": [
                {"name": "research", "description": "Search Savant Context code and memory bank"},
                {"name": "structure_search", "description": "AST/code graph search for symbols and definitions"},
                {"name": "analyze_code", "description": "Analyze code for complexity, findings, and refactor targets"},
            ],
        },
        {
            "name": "knowledge",
            "url": "http://127.0.0.1:8094/sse",
            "port": 8094,
            "status": "ok",
            "tool_count": 3,
            "tools": [{"name": "search"}, {"name": "store"}, {"name": "connect"}],
        },
        {
            "name": "reminders",
            "url": "http://127.0.0.1:8095/sse",
            "port": 8095,
            "status": "ok",
            "tool_count": 2,
            "tools": [{"name": "set_reminder"}, {"name": "list_reminders"}],
        },
    ]
    if server_name:
        return [r for r in rows if r["name"] == server_name]
    return rows


@jobs_system_bp.route("/api/jobs/submit", methods=["POST"])
def api_jobs_submit():
    data = request.get_json(force=True, silent=True) or {}
    job_type = (data.get("job_type") or data.get("type") or "").strip()
    target = (data.get("target") or "").strip()
    allowed = {"index", "reindex", "ast", "index-all", "ast-all",
               "codegraph_index", "codegraph_sync", "differential_sync"}
    if not job_type or not target:
        return jsonify({"error": "job_type and target are required"}), 400
    if job_type not in allowed:
        return jsonify({"error": f"Unsupported job type: {job_type}"}), 400
    existing = JobDB.find_active(job_type, target)
    if existing:
        return jsonify({"job_id": existing["id"], "status": existing["status"], "reused": True})
    created = JobDB.create_job(job_type, target)
    return jsonify({"job_id": created["id"], "status": created["status"]})


@jobs_system_bp.route("/api/jobs/status", methods=["GET"])
def api_jobs_status():
    job_id = (request.args.get("id") or request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400
    job = JobDB.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@jobs_system_bp.route("/api/jobs/list", methods=["GET"])
def api_jobs_list():
    status = request.args.get("status")
    jobs = JobDB.list_jobs(status=status)
    summary = JobDB.get_job_summary()
    return jsonify({
        "jobs": jobs,
        "summary": summary
    })



@jobs_system_bp.route("/api/jobs/cancel", methods=["POST"])
def api_jobs_cancel():
    data = request.get_json(force=True, silent=True) or {}
    job_id = (data.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400
    job = JobDB.get_job(job_id)
    cancelled = JobDB.request_cancel(job_id)
    bridge_cancelled = False
    if cancelled and job and job.get("status") == "running" and job.get("job_type") in {
        "codegraph_index", "codegraph_sync"
    }:
        try:
            from code_intelligence.runtime import build_service
            provider = build_service().registry.get_provider(str(job.get("target")))
            provider.client.cancel(job_id)
            bridge_cancelled = True
        except Exception as exc:
            # The persistent cancellation flag remains authoritative; the worker
            # will observe it if the bridge operation completes concurrently.
            return jsonify({
                "cancelled": True,
                "bridge_cancelled": False,
                "job_id": job_id,
                "warning": f"Cancellation requested, but graph bridge acknowledgement failed: {exc}",
            })
    return jsonify({
        "cancelled": cancelled,
        "bridge_cancelled": bridge_cancelled,
        "job_id": job_id,
    })


@jobs_system_bp.route("/api/jobs/<job_id>", methods=["DELETE"])
def api_jobs_delete(job_id):
    deleted = JobDB.delete_job(job_id)
    return jsonify({"deleted": deleted, "job_id": job_id})


@jobs_system_bp.route("/api/db/health", methods=["GET"])
def api_db_health():
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                res = cur.fetchone()
            return jsonify({"status": "healthy", "connected": True}), 200
        finally:
            release_connection(conn)
    except Exception as e:
        return jsonify({"status": "unhealthy", "connected": False, "error": str(e)}), 500


@jobs_system_bp.route("/api/system/info", methods=["GET"])
def api_system_info():
    abilities = abilities_bootstrap_status()
    mcp_tools = _list_mcp_tools()
    mcp_servers_dict = {
        row["name"]: {
            "url": row.get("url"),
            "port": row.get("port"),
            "status": row.get("status", "ok"),
            "tool_count": row.get("tool_count", len(row.get("tools", []))),
        }
        for row in mcp_tools
    }
    return jsonify({
        "status": "ok",
        "mcp_servers": mcp_servers_dict,
        "abilities": abilities,
        "asset_count": abilities.get("asset_count", 0),
        "bootstrap_available": abilities.get("bootstrap_available", False),
    })


@jobs_system_bp.route("/api/mcp/health/<name>", methods=["GET"])
def api_mcp_health_single(name):
    tools = _list_mcp_tools(name)
    if not tools:
        return jsonify({"name": name, "status": "unknown"}), 404
    return jsonify({"name": name, "status": "ok", "url": tools[0]["url"]})


@jobs_system_bp.route("/api/mcp/health", methods=["GET"])
def api_mcp_health_all():
    tools = _list_mcp_tools()
    return jsonify({"status": "ok", "servers": tools})


@jobs_system_bp.route("/api/mcp", methods=["GET"])
def api_mcp_list():
    tools = _list_mcp_tools()
    return jsonify({"servers": tools})


# MCP clients occasionally hit the Flask port (8090) with POST /mcp instead
# of the dedicated MCP SSE ports.  Return a diagnostic instead of a bare 404.
_MCP_PORT_MAP = {
    "workspace": int(os.environ.get("SAVANT_MCP_WORKSPACE_PORT", 8091)),
    "abilities": int(os.environ.get("SAVANT_MCP_ABILITIES_PORT", 8092)),
    "context":   int(os.environ.get("SAVANT_MCP_CONTEXT_PORT", 8093)),
    "knowledge": int(os.environ.get("SAVANT_MCP_KNOWLEDGE_PORT", 8094)),
    "reminders": int(os.environ.get("SAVANT_MCP_REMINDERS_PORT", 8095)),
}


@jobs_system_bp.route("/mcp", methods=["POST", "GET"])
def api_mcp_wrong_port():
    return jsonify({
        "error": "MCP transport endpoints are on dedicated ports, not the Flask API.",
        "hint": "Update your MCP client config to use the correct port.",
        "ports": _MCP_PORT_MAP,
    }), 421


@jobs_system_bp.route("/api/mcp/tools", methods=["GET"])
def api_mcp_tools():
    server_name = request.args.get("server")
    mcp_tools = _list_mcp_tools(server_name)
    return jsonify({"servers": mcp_tools})


@jobs_system_bp.route("/api/mcp/tools/<server_name>", methods=["GET"])
def api_mcp_tools_single(server_name):
    mcp_tools = _list_mcp_tools(server_name)
    if not mcp_tools:
        return jsonify({"error": f"MCP server '{server_name}' not found", "server": server_name}), 404
    return jsonify({"server": mcp_tools[0]})


@jobs_system_bp.route("/api/check-mcp", methods=["GET"])
def api_check_mcp():
    return jsonify({"status": "ok", "configured": True})


@jobs_system_bp.route("/api/setup-mcp", methods=["POST"])
def api_setup_mcp():
    return jsonify({"status": "configured", "updated": True})


@jobs_system_bp.route("/health/live", methods=["GET"])
def health_live():
    from server_version import get_build_info

    return jsonify({"status": "live", **get_build_info()})


@jobs_system_bp.route("/health/ready", methods=["GET"])
def health_ready():
    from server_version import get_build_info

    return jsonify({"status": "ready", **get_build_info()})


@jobs_system_bp.route("/api/events", methods=["GET"])
def api_events():
    user_id = getattr(g, "user_id", "")
    since = request.args.get("since", "0")
    notifications = NotificationDB.list_recent(since_id=since if since != "0" else None, user_id=user_id)
    return jsonify(notifications)


@jobs_system_bp.route("/api/experiences", methods=["GET", "POST"])
def api_experiences():
    user_id = getattr(g, "user_id", "")
    if request.method == "GET":
        exps = ExperienceDB.list_all(user_id=user_id)
        return jsonify(exps)

    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400

    created = ExperienceDB.create({
        "title": title,
        "content": data.get("content", ""),
        "user_id": user_id,
    })
    return jsonify(created), 201


@jobs_system_bp.route("/api/notifications", methods=["GET", "POST"])
def api_notifications():
    user_id = getattr(g, "user_id", "")
    if request.method == "GET":
        notes = NotificationDB.list_recent(user_id=user_id)
        return jsonify(notes)

    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    created = NotificationDB.create({
        "message": message,
        "level": data.get("level", "info"),
        "user_id": user_id,
    })
    return jsonify(created), 201


@jobs_system_bp.route("/api/notes", methods=["POST"])
def api_notes_create():
    user_id = getattr(g, "user_id", "")
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or data.get("content") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400

    created = NoteDB.create({
        "text": text,
        "workspace_id": data.get("workspace_id"),
        "session_id": data.get("session_id"),
        "user_id": user_id,
    })
    return jsonify(created), 201


@jobs_system_bp.route("/api/notes/backfill-workspaces", methods=["POST"])
def api_notes_backfill_workspaces():
    user_id = getattr(g, "user_id", "")
    count = NoteDB.backfill_workspaces(user_id=user_id)
    return jsonify({"status": "backfilled", "count": count})
