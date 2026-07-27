"""
savant-workspace MCP Server

Thin MCP bridge to the Savant Dashboard Flask API (localhost:8090).
All session-aware tools require `session_id` to be passed explicitly by the
AI client.  The session ID is never auto-detected — the AI client must read
it from its own session state (e.g. the folder name under
~/.copilot/session-state/ for Copilot CLI, or the session file in ~/.claude/
for Claude Code).

Tool groups:
  Workspaces    — get_current_workspace, list_workspaces, create_workspace,
                  get_workspace, close_workspace, assign_session_to_workspace
  Tasks         — list_tasks, create_task, update_task, complete_task,
                  get_next_task, ready_for_colosseum, get_next_colosseum_task,
                  claim_colosseum_task, add_task_dependency, remove_task_dependency
  Session Notes — list_session_notes, create_session_note, delete_session_note
  Merge Reqs    — create_merge_request, update_merge_request, list_merge_requests,
                  get_merge_request, assign_mr_to_session, unassign_mr_from_session,
                  add_mr_note, list_mr_notes
  Jira Tickets  — create_jira_ticket, update_jira_ticket, list_jira_tickets,
                  get_jira_ticket, assign_jira_to_session, unassign_jira_from_session,
                  add_jira_note, list_jira_notes
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Ensure the MCP package directory is on the path for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = os.environ.get("SAVANT_API_BASE", "http://localhost:8090")
REQUEST_TIMEOUT = 60  # seconds

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("savant-workspace")

from auth import auth_headers, install_header_capture

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Entry point args (parsed early so host/port can be passed to FastMCP)
# ---------------------------------------------------------------------------
import argparse as _argparse
_parser = _argparse.ArgumentParser(description="savant-workspace MCP server")
_parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
_parser.add_argument("--port", type=int, default=8091)
_parser.add_argument("--host", default="127.0.0.1")
_args, _ = _parser.parse_known_args()

mcp = FastMCP(
    "savant-workspace",
    instructions=(
        "Workspace and task management for AI sessions — backed by the Savant Dashboard API. "
        "SESSION ID: Many tools accept a session_id parameter. To get your session ID, "
        "read the session context from your environment — for Copilot CLI look in "
        "~/.copilot/session-state/ (the folder name is the session ID); for Claude Code "
        "check ~/.claude/ sessions. Pass the session_id explicitly to any tool that needs it. "
        "Workspace tools: list_workspaces, create_workspace, get_workspace, close_workspace, assign_session_to_workspace. "
        "Task tools: list_tasks, create_task, update_task, complete_task, get_next_task, add_task_dependency, remove_task_dependency. "
        "Session note tools: list_session_notes, create_session_note, delete_session_note. "
        "Merge request tools: create_merge_request, update_merge_request, list_merge_requests, get_merge_request, "
        "assign_mr_to_session, unassign_mr_from_session, add_mr_note, list_mr_notes. "
        "Jira ticket tools: create_jira_ticket, update_jira_ticket, list_jira_tickets, get_jira_ticket, "
        "assign_jira_to_session, unassign_jira_from_session, add_jira_note, list_jira_notes."
    ),
    host=_args.host,
    port=_args.port,
)

install_header_capture(mcp)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api(method: str, path: str, **kwargs) -> dict | list:
    """Call the Flask API. Raises on connection error with a helpful message."""
    url = f"{API_BASE}{path}"
    hdrs = kwargs.pop("headers", {})
    hdrs.update(auth_headers())
    try:
        resp = requests.request(method, url, timeout=REQUEST_TIMEOUT, headers=hdrs, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        raise RuntimeError(
            f"Dashboard app not running at {API_BASE}. "
            "Start it with: docker compose up -d"
        )
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        raise RuntimeError(f"API error {e.response.status_code}: {body}")


SESSION_ID_HELP = (
    "session_id is required. To find your session ID: "
    "Copilot CLI → the folder name under ~/.copilot/session-state/; "
    "Claude Code → check session context in ~/.claude/; "
    "Codex → check session context in your environment."
)


def _detect_session_provider(session_id: str) -> str | None:
    """Best-effort provider detection for an explicitly supplied session ID."""
    sid = (session_id or "").strip()
    env_provider_pairs = (
        ("codex", ("CODEX_SESSION_ID", "SAVANT_SESSION_ID")),
        ("claude", ("CLAUDE_SESSION_ID",)),
        ("gemini", ("GEMINI_SESSION_ID",)),
        ("copilot", ("SESSION_ID", "COPILOT_SESSION_ID")),
    )
    for provider, env_names in env_provider_pairs:
        for env_name in env_names:
            env_value = (os.environ.get(env_name) or "").strip()
            if env_value and env_value == sid:
                return provider
    return None


def _require_session_id(session_id: str) -> str:
    """Validate that session_id was explicitly provided."""
    sid = (session_id or "").strip()
    if not sid:
        raise RuntimeError(SESSION_ID_HELP)
    return sid


def _resolve_workspace_id(workspace_id: str | None = None) -> str:
    """Use explicit workspace_id or raise with guidance."""
    if workspace_id:
        return workspace_id
    raise RuntimeError(
        "workspace_id is required. Use list_workspaces() to find workspace IDs, "
        "or get_current_workspace(session_id) to find which workspace a session belongs to."
    )


PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_current_workspace(session_id: str = "") -> dict[str, Any]:
    """
    Auto-detect which workspace this AI session belongs to.
    Returns workspace details with task summary counts.
    If session_id is provided, uses it directly instead of process-tree detection.
    If no workspace is assigned, returns an error with the session_id so you can assign one.

    session_id: For Copilot CLI, use the folder name under ~/.copilot/session-state/.
    For Claude Code, check ~/.claude/ session context. For Codex, check your environment.
    """

    sid = _require_session_id(session_id)
    ws_id = None

    # First check workspace_session_links table. If we can infer a provider,
    # try it first, but never require provider detection for the lookup.
    provider_order = [p for p in (_detect_session_provider(sid), "copilot", "claude", "codex", "gemini", "savant") if p]
    for provider in dict.fromkeys(provider_order):
        try:
            resp = requests.get(
                f"{API_BASE}/api/session-links/resolve",
                params={"provider": provider, "session_id": sid},
                timeout=REQUEST_TIMEOUT,
                headers=auth_headers(),
            )
            if resp.status_code == 200:
                data = resp.json()
                ws_id = data.get("workspace_id")
                if ws_id:
                    break
        except Exception:
            continue

    # Fallback: try provider-specific session endpoints
    if not ws_id:
        for prefix in (
            f"/api/session/{sid}",
            f"/api/claude/session/{sid}",
            f"/api/codex/session/{sid}",
            f"/api/gemini/session/{sid}",
            f"/api/savant/session/{sid}",
        ):
            try:
                resp = requests.get(f"{API_BASE}{prefix}", timeout=REQUEST_TIMEOUT, headers=auth_headers())
                if resp.status_code == 200:
                    data = resp.json()
                    ws_id = data.get("workspace_id")
                    if ws_id:
                        break
            except Exception:
                continue

    if not ws_id:
        return {
            "error": "No workspace assigned to this session.",
            "session_id": sid,
            "help": "Use assign_session_to_workspace(workspace_id, session_id) to assign one, or do it in the dashboard at http://localhost:8090",
        }

    workspaces = _api("GET", "/api/workspaces")
    ws = next((w for w in workspaces if (w.get("id") or w.get("workspace_id")) == ws_id), None)
    if not ws:
        return {"error": f"Workspace {ws_id} not found in dashboard.", "session_id": sid}

    return {
        "workspace": {
            "id": ws.get("id") or ws.get("workspace_id"),
            "name": ws.get("name", ""),
            "description": ws.get("description", ""),
            "priority": ws.get("priority", "medium"),
            "status": ws.get("status", "open"),
        },
        "tasks": ws.get("task_stats", {}),
        "session_id": sid,
    }


@mcp.tool()
def list_workspaces(status: str = "open") -> list[dict]:
    """
    List all workspaces. Filter by status: 'open', 'closed', or 'all'.
    Returns id, name, description, priority, status, and task_stats for each workspace.
    Use the workspace id from the results to pass to other tools like create_task, assign_session_to_workspace, etc.
    """
    workspaces = _api("GET", "/api/workspaces")
    if status != "all":
        workspaces = [w for w in workspaces if w.get("status", "open") == status]
    return [
        {
            "id": w.get("id") or w.get("workspace_id"),
            "name": w.get("name", ""),
            "description": w.get("description", ""),
            "priority": w.get("priority", "medium"),
            "status": w.get("status", "open"),
            "task_stats": w.get("task_stats", {}),
        }
        for w in workspaces
    ]


@mcp.tool()
def create_workspace(
    name: str,
    description: str = "",
    priority: str = "medium",
) -> dict:
    """
    Create a new workspace and return the created workspace object (including its id).
    Priority: critical, high, medium, low.
    After creating, use assign_session_to_workspace(workspace_id) to assign this session to it.
    """
    payload = {"name": name, "description": description, "priority": priority}
    return _api("POST", "/api/workspaces", json=payload)


@mcp.tool()
def assign_session_to_workspace(workspace_id: str, session_id: str = "") -> dict:
    """
    Assign the current session (or a specific session) to a workspace.
    Defaults to the current AI coding session if session_id is omitted.
    Pass workspace_id from list_workspaces or create_workspace results.
    To unassign, pass workspace_id as an empty string or null.

    session_id: For Copilot CLI, use the folder name under ~/.copilot/session-state/.
    For Claude Code, check ~/.claude/ session context. For Codex, check your environment.
    """
    sid = _require_session_id(session_id)
    return _api("POST", f"/api/workspaces/{workspace_id}/session-links",
                json={"session_id": sid})


@mcp.tool()
def close_workspace(workspace_id: str = "") -> dict:
    """
    Close a workspace (set status to 'closed'). Defaults to the current session's workspace.
    Closed workspaces are hidden from default workspace lists and all dropdown menus.
    To reopen, use the dashboard UI.
    """
    ws_id = _resolve_workspace_id(workspace_id or None)
    return _api("PUT", f"/api/workspaces/{ws_id}", json={"status": "closed"})


@mcp.tool()
def get_workspace(workspace_id: str = "", name: str = "") -> dict:
    """
    Get a specific workspace by ID or name (fuzzy match).
    Provide either workspace_id or name. Name matching is case-insensitive substring.
    Returns full workspace details including task_stats, projects, and counts.
    """
    workspaces = _api("GET", "/api/workspaces")

    if workspace_id:
        ws = next((w for w in workspaces if (w.get("id") or w.get("workspace_id")) == workspace_id), None)
    elif name:
        name_lower = name.lower()
        ws = next((w for w in workspaces if name_lower in w.get("name", "").lower()), None)
    else:
        return {"error": "Provide workspace_id or name"}

    if not ws:
        return {"error": f"Workspace not found (id={workspace_id}, name={name})"}

    return {
        "id": ws.get("id") or ws.get("workspace_id"),
        "name": ws.get("name", ""),
        "description": ws.get("description", ""),
        "priority": ws.get("priority", "medium"),
        "status": ws.get("status", "open"),
        "task_stats": ws.get("task_stats", {}),
        "projects": ws.get("projects", []),
        "counts": ws.get("counts", {}),
    }


@mcp.tool()
def list_tasks(
    workspace_id: str = "",
    status: str = "all",
    date: str = "",
) -> list[dict]:
    """
    Get tasks for a workspace. Defaults to current workspace and today's date.
    Status filter: 'todo', 'in-progress', 'done', 'blocked', or 'all'.
    Date format: YYYY-MM-DD. Omit to get all dates.
    Returns id, title, description, status, priority, date, workspace_id for each task.
    """
    ws_id = _resolve_workspace_id(workspace_id or None)

    params: dict[str, str] = {"workspace_id": ws_id}
    if date:
        params["date"] = date

    tasks = _api("GET", "/api/tasks", params=params)

    if status != "all":
        tasks = [t for t in tasks if t.get("status") == status]

    return [
        {
            "id": t.get("id") or t.get("task_id", ""),
            "title": t.get("title", ""),
            "description": t.get("description", ""),
            "status": t.get("status", "todo"),
            "priority": t.get("priority", "medium"),
            "date": t.get("date", ""),
            "workspace_id": t.get("workspace_id", ""),
        }
        for t in tasks
    ]


@mcp.tool()
def create_task(
    title: str,
    description: str = "",
    workspace_id: str = "",
    priority: str = "medium",
    status: str = "todo",
    session_id: str = "",
) -> dict:
    """
    Create a new task and assign it to a workspace.
    Defaults to the current workspace if workspace_id is omitted.
    Priority: critical, high, medium, low. Status: todo, in-progress, done, blocked.
    The task is automatically dated to today and linked to the current session.
    """
    ws_id = _resolve_workspace_id(workspace_id or None)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    payload = {
        "title": title,
        "description": description,
        "workspace_id": ws_id,
        "priority": priority,
        "status": status,
        "date": today,
        "session_id": session_id or "",
    }
    return _api("POST", "/api/tasks", json=payload)


@mcp.tool()
def update_task(
    task_id: str,
    title: str = "",
    description: str = "",
    status: str = "",
    priority: str = "",
) -> dict:
    """
    Update fields on an existing task. Only provided fields are changed.
    Status values: todo, in-progress, done, blocked. Priority: critical, high, medium, low.
    Get task_id from list_tasks or get_next_task results.
    """
    payload: dict[str, str] = {}
    if title:
        payload["title"] = title
    if description:
        payload["description"] = description
    if status:
        payload["status"] = status
    if priority:
        payload["priority"] = priority

    if not payload:
        return {"error": "No fields to update. Provide at least one of: title, description, status, priority."}

    return _api("PUT", f"/api/tasks/{task_id}", json=payload)


@mcp.tool()
def complete_task(task_id: str) -> dict:
    """
    Mark a task as done. Shortcut for update_task(task_id, status='done').
    """
    return _api("PUT", f"/api/tasks/{task_id}", json={"status": "done"})


@mcp.tool()
def get_next_task(workspace_id: str = "") -> dict:
    """
    Get the highest-priority actionable task (todo or in-progress) for a workspace.
    Defaults to current workspace. Priority order: critical > high > medium > low.
    """
    ws_id = _resolve_workspace_id(workspace_id or None)
    tasks = _api("GET", "/api/tasks", params={"workspace_id": ws_id})

    actionable = [t for t in tasks if t.get("status") in ("todo", "in-progress")]
    if not actionable:
        return {"message": "No actionable tasks remaining for this workspace.", "workspace_id": ws_id}

    actionable.sort(key=lambda t: PRIORITY_ORDER.get(t.get("priority", "medium"), 2))
    task = actionable[0]
    return {
        "id": task.get("id") or task.get("task_id", ""),
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "status": task.get("status", "todo"),
        "priority": task.get("priority", "medium"),
        "date": task.get("date", ""),
        "workspace_id": task.get("workspace_id", ""),
    }


@mcp.tool()
def ready_for_colosseum(task_id: str, config: dict) -> dict:
    """Validate and mark a Workspace task ready for the headless Colosseum worker.

    Required config: repository (absolute path) and agent.program. Optional:
    agent.args, revision, setup, validate, timeout_seconds. A ready task is
    returned by get_next_colosseum_task and can be claimed by Colosseum.
    """
    return _api("POST", f"/api/tasks/{task_id}/colosseum-ready", json={"config": config})


@mcp.tool()
def get_next_colosseum_task(workspace_id: str = "") -> dict:
    """Get the highest-priority ready Colosseum task from a workspace."""
    ws_id = _resolve_workspace_id(workspace_id or None)
    return _api("GET", "/api/tasks/colosseum/next", params={"workspace_id": ws_id})


@mcp.tool()
def claim_colosseum_task(task_id: str) -> dict:
    """Atomically claim a ready Colosseum task, moving it to in-progress."""
    return _api("POST", f"/api/tasks/{task_id}/claim")


# ---------------------------------------------------------------------------
# Task Dependencies
# ---------------------------------------------------------------------------

@mcp.tool()
def add_task_dependency(task_id: str, depends_on: str) -> dict:
    """
    Add a dependency link: task_id depends on depends_on.
    Both tasks must exist. Circular dependencies are rejected.
    Dependencies are scoped within a workspace.
    """
    return _api("POST", f"/api/tasks/{task_id}/deps", json={"depends_on": depends_on})


@mcp.tool()
def remove_task_dependency(task_id: str, depends_on: str) -> dict:
    """
    Remove a dependency link between two tasks.
    """
    return _api("DELETE", f"/api/tasks/{task_id}/deps/{depends_on}")


# ---------------------------------------------------------------------------
# Session Notes
# ---------------------------------------------------------------------------

@mcp.tool()
def list_session_notes(session_id: str = "") -> dict:
    """
    Get all notes for a session. Defaults to the current session.
    Returns notes sorted by timestamp (newest first).
    """
    sid = _require_session_id(session_id)
    data = _api("GET", f"/api/session/{sid}/notes")
    notes = data.get("notes", [])
    notes.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
    return {"session_id": sid, "notes": notes, "count": len(notes)}


@mcp.tool()
def create_session_note(text: str, session_id: str = "") -> dict:
    """
    Create a note on a session. Defaults to the current session.
    Notes are timestamped and visible in the SAVANT dashboard under the session's Notes tab.

    session_id: For Copilot CLI, use the folder name under ~/.copilot/session-state/.
    For Claude Code, check ~/.claude/ session context. For Codex, check your environment.
    """
    sid = _require_session_id(session_id)
    return _api("POST", f"/api/session/{sid}/notes", json={"text": text})


@mcp.tool()
def delete_session_note(index: int, session_id: str = "") -> dict:
    """
    Delete a note from a session by its index (0-based, oldest first).
    Defaults to the current session.
    """
    sid = _require_session_id(session_id)
    return _api("DELETE", f"/api/session/{sid}/notes", json={"index": index})


# ---------------------------------------------------------------------------
# Merge Requests
# ---------------------------------------------------------------------------

@mcp.tool()
def create_merge_request(
    url: str,
    jira: str = "",
    status: str = "open",
    author: str = "",
    priority: str = "medium",
    title: str = "",
    workspace_id: str = "",
) -> dict:
    """
    Register a merge request in the central registry.
    URL is the GitLab MR URL (required, must be unique).
    project_id and mr_iid are auto-parsed from the URL.
    Defaults to the current workspace if workspace_id is omitted.
    If author is omitted, auto-populates from user preferences.
    Status: draft, open, review, reviewing, approved, merged, closed, on-hold.
    """
    ws_id = _resolve_workspace_id(workspace_id or None)
    payload = {
        "url": url,
        "jira": jira,
        "status": status,
        "author": author,
        "priority": priority,
        "title": title,
        "workspace_id": ws_id,
    }
    return _api("POST", "/api/merge-requests", json=payload)


@mcp.tool()
def update_merge_request(
    mr_id: str,
    title: str = "",
    status: str = "",
    jira: str = "",
    author: str = "",
    priority: str = "",
    workspace_id: str = "",
) -> dict:
    """
    Update fields on an existing merge request. Only provided fields are changed.
    Get mr_id from list_merge_requests or get_merge_request results.
    Status: draft, open, review, reviewing, approved, merged, closed, on-hold.
    """
    payload: dict[str, str] = {}
    if title:
        payload["title"] = title
    if status:
        payload["status"] = status
    if jira:
        payload["jira"] = jira
    if author:
        payload["author"] = author
    if priority:
        payload["priority"] = priority
    if workspace_id:
        payload["workspace_id"] = workspace_id

    if not payload:
        return {"error": "No fields to update."}

    return _api("PUT", f"/api/merge-requests/{mr_id}", json=payload)


@mcp.tool()
def list_merge_requests(
    workspace_id: str = "",
    status: str = "",
) -> list[dict]:
    """
    List merge requests. Defaults to current workspace.
    Optional status filter: draft, open, review, reviewing, approved, merged, closed, on-hold.
    """
    ws_id = _resolve_workspace_id(workspace_id or None)
    params: dict[str, str] = {"workspace_id": ws_id}
    if status:
        params["status"] = status
    return _api("GET", "/api/merge-requests", params=params)


@mcp.tool()
def get_merge_request(mr_id: str) -> dict:
    """
    Get a merge request by ID, including notes and linked sessions.
    """
    return _api("GET", f"/api/merge-requests/{mr_id}")


@mcp.tool()
def assign_mr_to_session(
    mr_id: str,
    role: str = "",
    session_id: str = "",
) -> dict:
    """
    Assign a merge request to a session with a role.
    Role: author or reviewer (default: auto-detected).
    If role is omitted, auto-detects based on MR author vs current user.
    Defaults to the current session if session_id is omitted.

    session_id: For Copilot CLI, use the folder name under ~/.copilot/session-state/.
    For Claude Code, check ~/.claude/ session context. For Codex, check your environment.
    """
    sid = _require_session_id(session_id)
    return _api("POST", f"/api/session/{sid}/assign-mr", json={"mr_id": mr_id, "role": role})


@mcp.tool()
def unassign_mr_from_session(
    mr_id: str,
    session_id: str = "",
) -> dict:
    """
    Remove a merge request assignment from a session.
    Defaults to the current session if session_id is omitted.
    """
    sid = _require_session_id(session_id)
    return _api("POST", f"/api/session/{sid}/unassign-mr", json={"mr_id": mr_id})


@mcp.tool()
def add_mr_note(mr_id: str, text: str, session_id: str = "") -> dict:
    """
    Add a comment/note to a merge request.
    The note is attributed to the current session.
    """
    return _api("POST", f"/api/merge-requests/{mr_id}/notes", json={"text": text, "session_id": session_id or ""})


@mcp.tool()
def list_mr_notes(mr_id: str) -> dict:
    """
    Get all notes/comments for a merge request.
    """
    return _api("GET", f"/api/merge-requests/{mr_id}/notes")


# ---------------------------------------------------------------------------
# Jira Tickets
# ---------------------------------------------------------------------------

@mcp.tool()
def create_jira_ticket(
    ticket_key: str,
    title: str = "",
    status: str = "todo",
    priority: str = "medium",
    assignee: str = "",
    reporter: str = "",
    workspace_id: str = "",
) -> dict:
    """
    Register a Jira ticket in the central registry.
    ticket_key is the JIRA issue key (e.g. PROJ-1234, required, must be unique).
    URL is auto-generated from the key.
    Defaults to the current workspace if workspace_id is omitted.
    If assignee is omitted, auto-populates from user preferences.
    Status: todo, in-progress, in-review, done, blocked.
    """
    ws_id = _resolve_workspace_id(workspace_id or None)
    payload = {
        "ticket_key": ticket_key,
        "title": title,
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "reporter": reporter,
        "workspace_id": ws_id,
    }
    return _api("POST", "/api/jira-tickets", json=payload)


@mcp.tool()
def update_jira_ticket(
    ticket_id: str,
    title: str = "",
    status: str = "",
    priority: str = "",
    assignee: str = "",
    reporter: str = "",
    ticket_key: str = "",
    workspace_id: str = "",
) -> dict:
    """
    Update fields on an existing Jira ticket. Only provided fields are changed.
    ticket_id accepts either the internal ID or the JIRA issue key (e.g. PROJ-1234).
    Status: todo, in-progress, in-review, done, blocked.
    """
    payload: dict[str, str] = {}
    if title:
        payload["title"] = title
    if status:
        payload["status"] = status
    if priority:
        payload["priority"] = priority
    if assignee:
        payload["assignee"] = assignee
    if reporter:
        payload["reporter"] = reporter
    if ticket_key:
        payload["ticket_key"] = ticket_key
    if workspace_id:
        payload["workspace_id"] = workspace_id

    if not payload:
        return {"error": "No fields to update."}

    return _api("PUT", f"/api/jira-tickets/{ticket_id}", json=payload)


@mcp.tool()
def list_jira_tickets(
    workspace_id: str = "",
    status: str = "",
    assignee: str = "",
) -> list[dict]:
    """
    List Jira tickets. Defaults to current workspace.
    Optional filters: status (todo, in-progress, in-review, done, blocked), assignee name.
    """
    ws_id = _resolve_workspace_id(workspace_id or None)
    params: dict[str, str] = {"workspace_id": ws_id}
    if status:
        params["status"] = status
    if assignee:
        params["assignee"] = assignee
    return _api("GET", "/api/jira-tickets", params=params)


@mcp.tool()
def get_jira_ticket(ticket_id: str) -> dict:
    """
    Get a Jira ticket by ID, including notes and linked sessions.
    Accepts either the internal ticket_id or the JIRA issue key (e.g. PROJ-1234).
    """
    return _api("GET", f"/api/jira-tickets/{ticket_id}")


@mcp.tool()
def assign_jira_to_session(
    ticket_id: str,
    role: str = "assignee",
    session_id: str = "",
) -> dict:
    """
    Assign a Jira ticket to a session with a role.
    Role: assignee, reviewer, or watcher (default: assignee).
    Defaults to the current session if session_id is omitted.

    session_id: For Copilot CLI, use the folder name under ~/.copilot/session-state/.
    For Claude Code, check ~/.claude/ session context. For Codex, check your environment.
    """
    sid = _require_session_id(session_id)
    return _api("POST", f"/api/session/{sid}/assign-jira", json={"ticket_id": ticket_id, "role": role})


@mcp.tool()
def unassign_jira_from_session(
    ticket_id: str,
    session_id: str = "",
) -> dict:
    """
    Remove a Jira ticket assignment from a session.
    Defaults to the current session if session_id is omitted.
    """
    sid = _require_session_id(session_id)
    return _api("POST", f"/api/session/{sid}/unassign-jira", json={"ticket_id": ticket_id})


@mcp.tool()
def add_jira_note(ticket_id: str, text: str, session_id: str = "") -> dict:
    """
    Add a comment/note to a Jira ticket.
    The note is attributed to the current session.
    """
    return _api("POST", f"/api/jira-tickets/{ticket_id}/notes", json={"text": text, "session_id": session_id or ""})


@mcp.tool()
def list_jira_notes(ticket_id: str) -> dict:
    """
    Get all notes/comments for a Jira ticket.
    """
    return _api("GET", f"/api/jira-tickets/{ticket_id}/notes")


# ---------------------------------------------------------------------------
# Patch: catch ClosedResourceError in SSE transport
# ---------------------------------------------------------------------------
def _patch_sse_transport():
    try:
        from mcp.server.sse import SseServerTransport
        from anyio import ClosedResourceError
        original_handle = SseServerTransport.handle_post_message

        async def _safe_handle(self, scope, receive, send):
            try:
                await original_handle(self, scope, receive, send)
            except Exception as e:
                if "ClosedResourceError" in type(e).__name__ or "ClosedResource" in str(e):
                    log.debug("SSE client disconnected before response sent (harmless)")
                else:
                    raise

        SseServerTransport.handle_post_message = _safe_handle
    except Exception as e:
        log.warning(f"Could not apply SSE patch: {e}")

_patch_sse_transport()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if _args.transport == "sse":
        log.info(f"Starting MCP SSE server on {_args.host}:{_args.port}")
    mcp.run(transport=_args.transport)
