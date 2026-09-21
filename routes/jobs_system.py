"""Jobs, MCP, System Health, and Notifications Routes Blueprint for Savant Server."""

import os
import sys
import json
import time
import requests
from flask import Blueprint, g, jsonify, request
from db.jobs import JobDB
from db.notifications import NotificationDB
from db.notes import NoteDB
from db.experiences import ExperienceDB
from postgres_client import get_connection, release_connection
from abilities.bootstrap import abilities_bootstrap_status

jobs_system_bp = Blueprint("jobs_system", __name__)


def _port_from_environment(name, default):
    """Read an optional port override without allowing malformed diagnostics."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _list_mcp_tools(server_name=None):
    """Return configured/discovered MCP tools. Dynamically respects app monkeypatching in tests."""
    app_mod = sys.modules.get("app")
    if app_mod and hasattr(app_mod, "_list_mcp_tools") and getattr(app_mod, "_list_mcp_tools") != _list_mcp_tools:
        return getattr(app_mod, "_list_mcp_tools")(server_name)

    specs = [
        ("workspace", "SAVANT_MCP_WORKSPACE_PORT", 8091, "SAVANT_MCP_STREAMABLE_WORKSPACE_PORT", 8191, 2,
         [{"name": "list_workspaces"}, {"name": "create_workspace"}]),
        ("abilities", "SAVANT_MCP_ABILITIES_PORT", 8092, "SAVANT_MCP_STREAMABLE_ABILITIES_PORT", 8192, 1,
         [{"name": "list_personas"}]),
        ("context", "SAVANT_MCP_CONTEXT_PORT", 8093, "SAVANT_MCP_STREAMABLE_CONTEXT_PORT", 8193, 3,
         [
             {"name": "research", "description": "Search Savant Context code and memory bank"},
             {"name": "structure_search", "description": "AST/code graph search for symbols and definitions"},
             {"name": "analyze_code", "description": "Analyze code for complexity, findings, and refactor targets"},
         ]),
        ("knowledge", "SAVANT_MCP_KNOWLEDGE_PORT", 8094, "SAVANT_MCP_STREAMABLE_KNOWLEDGE_PORT", 8194, 3,
         [{"name": "search"}, {"name": "store"}, {"name": "connect"}]),
        ("reminders", "SAVANT_MCP_REMINDERS_PORT", 8095, "SAVANT_MCP_STREAMABLE_REMINDERS_PORT", 8195, 2,
         [{"name": "set_reminder"}, {"name": "list_reminders"}]),
    ]
    rows = []
    for name, sse_env, sse_default, http_env, http_default, tool_count, tools in specs:
        sse_port = _port_from_environment(sse_env, sse_default)
        http_port = _port_from_environment(http_env, http_default)
        streamable_http = {
            "transport": "streamable-http",
            "url": f"http://127.0.0.1:{http_port}/mcp",
            "health_url": f"http://127.0.0.1:{http_port}/health",
            "port": http_port,
        }
        rows.append({
            "name": name,
            # Existing clients and callers continue to use these SSE fields.
            "url": f"http://127.0.0.1:{sse_port}/sse",
            "port": sse_port,
            "transport": "sse",
            "streamable_http": streamable_http,
            "status": "ok",
            "tool_count": tool_count,
            "tools": tools,
        })
    if server_name:
        return [r for r in rows if r["name"] == server_name]
    return rows


def _probe_mcp_server(server):
    """Return a copy of an MCP server descriptor with its live health state."""
    result = dict(server)
    endpoints = {
        "sse": {"url": server["url"], "port": server["port"]},
        "streamable-http": {
            "url": server["streamable_http"]["health_url"],
            "port": server["streamable_http"]["port"],
        },
    }
    transport_status = {
        transport: _probe_mcp_endpoint(endpoint)
        for transport, endpoint in endpoints.items()
    }

    result["transport_status"] = transport_status
    unavailable = next((item for item in transport_status.values() if item["status"] != "ok"), None)
    if unavailable:
        result.update(status="unavailable", diagnostic=unavailable["diagnostic"])
    else:
        result.update(status="ok", diagnostic="reachable")
    return result


def _probe_mcp_endpoint(endpoint):
    """Probe one listener while keeping network diagnostics safe for API output."""
    try:
        response = requests.get(endpoint["url"], timeout=1, stream=True)
    except (requests.RequestException, OSError):
        return {"status": "unavailable", "diagnostic": "connection failed", **endpoint}

    try:
        if 200 <= response.status_code < 400:
            return {"status": "ok", "diagnostic": "reachable", **endpoint}
        return {
            "status": "unavailable",
            "diagnostic": f"HTTP {response.status_code}",
            **endpoint,
        }
    finally:
        response.close()


def _postgres_readiness():
    """Check the database without exposing connection strings or credentials."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return {"status": "ok"}
        finally:
            release_connection(conn)
    except Exception:
        return {"status": "unavailable", "diagnostic": "connection check failed"}


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
        "flask": {
            "host": os.environ.get("FLASK_HOST", "127.0.0.1"),
            "port": _port_from_environment("FLASK_PORT", 8090),
        },
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
    server = _probe_mcp_server(tools[0])
    return jsonify(server), 200 if server["status"] == "ok" else 503


@jobs_system_bp.route("/api/mcp/health", methods=["GET"])
def api_mcp_health_all():
    servers = [_probe_mcp_server(server) for server in _list_mcp_tools()]
    healthy = all(server["status"] == "ok" for server in servers)
    return jsonify({"status": "ok" if healthy else "degraded", "servers": servers}), 200 if healthy else 503


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


_AGENT_SETUP_PROFILES = {
    "copilot": {
        "label": "GitHub Copilot",
        "description": "GitHub Copilot Chat, CLI, and Agent Mode integration",
        "presence_path": os.path.expanduser("~/.copilot"),
        "mcp_path": os.path.expanduser("~/.copilot/mcp.json"),
        "instructions_path": os.path.expanduser("~/.copilot/copilot-instructions.md"),
        "skills_dir": os.path.expanduser("~/.copilot/skills"),
        "hook_path": os.path.expanduser("~/.copilot/record-learning.sh"),
    },
    "claude": {
        "label": "Claude Code / Desktop",
        "description": "Anthropic Claude Code CLI & Desktop Agent integration",
        "presence_path": os.path.expanduser("~/.claude"),
        "mcp_path": os.path.expanduser("~/.claude/claude_desktop_config.json"),
        "instructions_path": os.path.expanduser("~/.claude/CLAUDE.md"),
        "skills_dir": os.path.expanduser("~/.claude/skills"),
        "hook_path": os.path.expanduser("~/.claude/record-learning.sh"),
    },
    "hermes": {
        "label": "Hermes Agent",
        "description": "Hermes autonomous agent execution runtime",
        "presence_path": os.path.expanduser("~/.hermes"),
        "mcp_path": os.path.expanduser("~/.hermes/mcp.json"),
        "instructions_path": os.path.expanduser("~/.hermes/instructions.md"),
        "skills_dir": os.path.expanduser("~/.hermes/skills/custom"),
        "hook_path": os.path.expanduser("~/.hermes/record-learning.sh"),
    },
    "codex": {
        "label": "Codex Agent",
        "description": "Codex CLI & OpenAI developer environment",
        "presence_path": os.path.expanduser("~/.codex"),
        "mcp_path": os.path.expanduser("~/.codex/mcp.json"),
        "instructions_path": os.path.expanduser("~/.codex/instructions.md"),
        "skills_dir": os.path.expanduser("~/.codex/skills"),
        "hook_path": os.path.expanduser("~/.codex/record-learning.sh"),
    },
}

_LEARNING_PROTOCOL_TEXT = """
<!-- SAVANT KNOWLEDGE PROTOCOL START -->
## Savant Knowledge & Memory Persistence Protocol
You are integrated with Savant Knowledge Graph and Code Intelligence.
Whenever you:
1. Fix a bug, identify a non-trivial root cause, or resolve an edge case
2. Establish an architectural pattern, convention, or design decision
3. Complete an assigned task or major feature

**You MUST record the durable outcome into Savant Knowledge:**
- **Via MCP**: Call `savant-knowledge.store` with:
  - `node_type`: `"insight"` (for design/patterns) or `"issue"` (for bug root causes)
  - `name`: Clear, concise title
  - `content`: Problem statement, root cause or architectural decision, and rationale
  - `workspace_id`: Active Savant workspace ID
  - `repo` / `files`: Touched files
  Follow with `savant-knowledge.commit_workspace(workspace_id)` when finalizing.
- **Via Shell Fallback**: If MCP is unavailable in the current context, execute:
  `./record-learning.sh --type insight --title "..." --content "..."`
<!-- SAVANT KNOWLEDGE PROTOCOL END -->
"""

_HOOK_SCRIPT_CONTENT = """#!/usr/bin/env bash
# Savant Knowledge Auto-Recorder Hook
set -e

SAVANT_SERVER_URL="${SAVANT_SERVER_URL:-http://127.0.0.1:8090}"
API_KEY="${SAVANT_API_KEY:-sk-ahmed-savant-001}"
APP_NAME="savant-client"

TYPE="insight"
TITLE=""
CONTENT=""
WORKSPACE_ID=""

while [[ "$#" -gt 0 ]]; do
  case $1 in
    --type) TYPE="$2"; shift ;;
    --title) TITLE="$2"; shift ;;
    --content) CONTENT="$2"; shift ;;
    --workspace) WORKSPACE_ID="$2"; shift ;;
    *) echo "Unknown parameter: $1"; exit 1 ;;
  esac
  shift
done

if [[ -z "$TITLE" ]]; then
  echo "Error: --title is required"
  exit 1
fi

PAYLOAD=$(cat <<EOF
{
  "title": "$TITLE",
  "content": "$CONTENT",
  "node_type": "$TYPE",
  "workspace_id": "$WORKSPACE_ID"
}
EOF
)

RESPONSE=$(curl -s -w "\\n%{http_code}" -X POST "$SAVANT_SERVER_URL/api/knowledge/nodes" \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: $API_KEY" \\
  -H "X-App-Name: $APP_NAME" \\
  -d "$PAYLOAD")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
if [[ "$HTTP_CODE" -ge 400 ]]; then
  curl -s -X POST "$SAVANT_SERVER_URL/api/experiences" \\
    -H "Content-Type: application/json" \\
    -H "X-API-Key: $API_KEY" \\
    -H "X-App-Name: $APP_NAME" \\
    -d "$PAYLOAD"
fi

echo "Posted learning to Savant Knowledge: $TITLE"
"""


def _check_agent_setup_status_dict():
    results = {}
    for key, profile in _AGENT_SETUP_PROFILES.items():
        present = os.path.exists(profile["presence_path"])

        # 1. MCP
        mcp_configured = False
        if os.path.exists(profile["mcp_path"]):
            try:
                with open(profile["mcp_path"], "r", encoding="utf-8") as f:
                    content = f.read()
                    if "8094" in content or "savant-knowledge" in content:
                        mcp_configured = True
            except Exception:
                pass

        # 2. Instructions
        instructions_configured = False
        if os.path.exists(profile["instructions_path"]):
            try:
                with open(profile["instructions_path"], "r", encoding="utf-8") as f:
                    content = f.read()
                    if "savant-knowledge" in content or "Savant Knowledge" in content or "SAVANT KNOWLEDGE PROTOCOL" in content:
                        instructions_configured = True
            except Exception:
                pass

        # 3. Skills
        skills_configured = False
        target_skill_dir = os.path.join(profile["skills_dir"], "savant-knowledge-commit")
        if os.path.exists(target_skill_dir):
            skills_configured = True

        # 4. Hook Script
        hook_configured = os.path.exists(profile["hook_path"])

        parts = [
            {
                "id": "mcp",
                "label": "MCP Knowledge Bridge",
                "configured": mcp_configured,
                "path": profile["mcp_path"],
                "details": "SSE connection to Savant Knowledge (port 8094) & Context (port 8093)",
            },
            {
                "id": "instructions",
                "label": "Learning Protocol Instructions",
                "configured": instructions_configured,
                "path": profile["instructions_path"],
                "details": "Mandates posting durable insights & bug root causes to Savant Knowledge",
            },
            {
                "id": "skills",
                "label": "Savant Default Skills",
                "configured": skills_configured,
                "path": profile["skills_dir"],
                "details": "savant-knowledge-commit, savant-code-analysis, savant-session-workspace",
            },
            {
                "id": "hook",
                "label": "Fallback Learning Hook",
                "configured": hook_configured,
                "path": profile["hook_path"],
                "details": "CLI curl wrapper for posting learnings directly to Savant Knowledge API",
            },
        ]

        all_conf = all(p["configured"] for p in parts)
        none_conf = all(not p["configured"] for p in parts)
        status = "configured" if all_conf else ("not_configured" if none_conf else "partial")

        results[key] = {
            "provider": key,
            "label": profile["label"],
            "description": profile["description"],
            "presencePath": profile["presence_path"],
            "present": present,
            "status": status,
            "parts": parts,
            "lastChecked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    agents_list = list(results.values())
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "serverUrl": request.host_url.rstrip("/") if request else "http://127.0.0.1:8090",
        "agents": results,
        "summary": {
            "total": len(agents_list),
            "configured": sum(1 for a in agents_list if a["status"] == "configured"),
            "partial": sum(1 for a in agents_list if a["status"] == "partial"),
            "notConfigured": sum(1 for a in agents_list if a["status"] == "not_configured"),
        },
    }


@jobs_system_bp.route("/api/agents/setup/status", methods=["GET"])
def api_agent_setup_status():
    return jsonify(_check_agent_setup_status_dict())


@jobs_system_bp.route("/api/agents/setup/trigger", methods=["POST"])
def api_agent_setup_trigger():
    data = request.get_json(force=True, silent=True) or {}
    provider = data.get("provider", "all")
    targets = list(_AGENT_SETUP_PROFILES.keys()) if provider == "all" else [provider]

    configured_parts = []
    for p in targets:
        profile = _AGENT_SETUP_PROFILES.get(p)
        if not profile:
            continue

        os.makedirs(profile["presence_path"], exist_ok=True)

        # 1. MCP
        try:
            os.makedirs(os.path.dirname(profile["mcp_path"]), exist_ok=True)
            current_mcp = {"mcpServers": {}}
            if os.path.exists(profile["mcp_path"]):
                try:
                    with open(profile["mcp_path"], "r", encoding="utf-8") as f:
                        current_mcp = json.load(f)
                    if "mcpServers" not in current_mcp or not isinstance(current_mcp["mcpServers"], dict):
                        current_mcp["mcpServers"] = {}
                except Exception:
                    pass

            current_mcp["mcpServers"]["savant-knowledge"] = {
                "type": "sse",
                "url": "http://127.0.0.1:8094/sse?api_key=sk-ahmed-savant-001&app_name=savant-mcp",
            }
            current_mcp["mcpServers"]["savant-context"] = {
                "type": "sse",
                "url": "http://127.0.0.1:8093/sse?api_key=sk-ahmed-savant-001&app_name=savant-mcp",
            }
            current_mcp["mcpServers"]["savant-workspace"] = {
                "type": "sse",
                "url": "http://127.0.0.1:8091/sse?api_key=sk-ahmed-savant-001&app_name=savant-mcp",
            }
            with open(profile["mcp_path"], "w", encoding="utf-8") as f:
                json.dump(current_mcp, f, indent=2)
            configured_parts.append(f"{p}:mcp")
        except Exception as e:
            pass

        # 2. Instructions
        try:
            os.makedirs(os.path.dirname(profile["instructions_path"]), exist_ok=True)
            existing = ""
            if os.path.exists(profile["instructions_path"]):
                try:
                    with open(profile["instructions_path"], "r", encoding="utf-8") as f:
                        existing = f.read()
                except Exception:
                    pass

            if "SAVANT KNOWLEDGE PROTOCOL" not in existing:
                updated = (existing.strip() + "\n\n" + _LEARNING_PROTOCOL_TEXT.strip() + "\n") if existing else (_LEARNING_PROTOCOL_TEXT.strip() + "\n")
                with open(profile["instructions_path"], "w", encoding="utf-8") as f:
                    f.write(updated)
            configured_parts.append(f"{p}:instructions")
        except Exception as e:
            pass

        # 3. Skills
        try:
            os.makedirs(profile["skills_dir"], exist_ok=True)
            skill_dir = os.path.join(profile["skills_dir"], "savant-knowledge-commit")
            os.makedirs(skill_dir, exist_ok=True)
            skill_md = os.path.join(skill_dir, "SKILL.md")
            with open(skill_md, "w", encoding="utf-8") as f:
                f.write("""---
name: savant-knowledge-commit
description: Record workspace-scoped outcomes in the Savant knowledge graph through MCP.
---

# Savant Knowledge Commit
Capture durable outcomes in Savant Knowledge using only savant-knowledge MCP tools.
""")
            configured_parts.append(f"{p}:skills")
        except Exception as e:
            pass

        # 4. Hook Script
        try:
            os.makedirs(os.path.dirname(profile["hook_path"]), exist_ok=True)
            with open(profile["hook_path"], "w", encoding="utf-8") as f:
                f.write(_HOOK_SCRIPT_CONTENT)
            os.chmod(profile["hook_path"], 0o755)
            configured_parts.append(f"{p}:hook")
        except Exception as e:
            pass

    report = _check_agent_setup_status_dict()
    return jsonify({
        "success": True,
        "provider": provider,
        "configuredParts": configured_parts,
        "report": report,
    })


@jobs_system_bp.route("/health/live", methods=["GET"])
def health_live():
    from server_version import get_build_info

    return jsonify({"status": "live", **get_build_info()})


@jobs_system_bp.route("/health/ready", methods=["GET"])
def health_ready():
    from server_version import get_build_info

    postgres = _postgres_readiness()
    ready = postgres["status"] == "ok"
    return jsonify({
        "status": "ready" if ready else "not_ready",
        "dependencies": {"postgres": postgres},
        **get_build_info(),
    }), 200 if ready else 503


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
