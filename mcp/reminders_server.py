"""
savant-reminders MCP Server

Thin MCP bridge to the Savant Dashboard Flask API (/api/reminders/*).
Runs as SSE on port 8095.
"""

import argparse
import logging
import os
from typing import Any, Optional

import requests
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = os.environ.get("SAVANT_API_BASE", "http://localhost:8090")
REQUEST_TIMEOUT = 10

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("savant-reminders")

from auth import auth_headers, install_header_capture

# ---------------------------------------------------------------------------
# Entry point args
# ---------------------------------------------------------------------------

_parser = argparse.ArgumentParser(description="savant-reminders MCP server")
_parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
_parser.add_argument("--port", type=int, default=8095)
_parser.add_argument("--host", default="127.0.0.1")
_args, _ = _parser.parse_known_args()

mcp = FastMCP(
    "savant-reminders",
    instructions=(
        "User reminder management for Savant. Create and manage personal reminders with due dates, "
        "priorities, and notification windows. Reminders are standalone (not tied to workspaces). "
        "Tools: create_reminder (title, due_date required; priority, description, remind_before_hrs optional), "
        "list_reminders (optional status filter), get_reminder, complete_reminder, dismiss_reminder, "
        "update_reminder, list_due_today, list_due_soon. "
        "Priority levels: low, medium, high, critical. "
        "due_date format: ISO 8601 (e.g. '2024-12-25T09:00:00Z' or '2024-12-25'). "
        "remind_before_hrs: hours before due_date to trigger notification (default 1)."
    ),
    host=_args.host,
    port=_args.port,
)

install_header_capture(mcp)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api(method: str, path: str, **kwargs) -> dict | list:
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
            "Start it with: npm run dev (or docker compose up -d)"
        )
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        raise RuntimeError(f"API error {e.response.status_code}: {body}")

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def create_reminder(
    title: str,
    due_date: str,
    description: str = "",
    priority: str = "medium",
    remind_before_hrs: int = 1,
) -> dict[str, Any]:
    """Create a personal reminder with a due date.
    
    title: Short description of what to remember
    due_date: When this is due (ISO 8601, e.g. '2024-12-25T09:00:00Z' or '2024-12-25')
    description: Optional longer description
    priority: low | medium | high | critical (default: medium)
    remind_before_hrs: Hours before due_date to show notification popup (default: 1)
    """
    return _api("POST", "/api/reminders", json={
        "title": title,
        "due_date": due_date,
        "description": description,
        "priority": priority,
        "remind_before_hrs": remind_before_hrs,
    })


@mcp.tool()
def list_reminders(status: str = "") -> list[dict[str, Any]]:
    """List all reminders, optionally filtered by status.
    
    status: pending | done | dismissed | '' (all, default)
    """
    params = {}
    if status:
        params["status"] = status
    return _api("GET", "/api/reminders", params=params)


@mcp.tool()
def get_reminder(reminder_id: str) -> dict[str, Any]:
    """Get a single reminder by its ID."""
    return _api("GET", f"/api/reminders/{reminder_id}")


@mcp.tool()
def update_reminder(
    reminder_id: str,
    title: str = "",
    description: str = "",
    priority: str = "",
    due_date: str = "",
    remind_before_hrs: int = 0,
) -> dict[str, Any]:
    """Update fields on an existing reminder. Only non-empty values are applied."""
    updates: dict = {}
    if title:
        updates["title"] = title
    if description:
        updates["description"] = description
    if priority:
        updates["priority"] = priority
    if due_date:
        updates["due_date"] = due_date
    if remind_before_hrs:
        updates["remind_before_hrs"] = remind_before_hrs
    return _api("PUT", f"/api/reminders/{reminder_id}", json=updates)


@mcp.tool()
def complete_reminder(reminder_id: str) -> dict[str, Any]:
    """Mark a reminder as done/completed."""
    return _api("POST", f"/api/reminders/{reminder_id}/complete")


@mcp.tool()
def dismiss_reminder(reminder_id: str) -> dict[str, Any]:
    """Dismiss a reminder (snooze forever / not relevant)."""
    return _api("POST", f"/api/reminders/{reminder_id}/dismiss")


@mcp.tool()
def delete_reminder(reminder_id: str) -> dict[str, Any]:
    """Permanently delete a reminder. Cannot be undone."""
    return _api("DELETE", f"/api/reminders/{reminder_id}")


@mcp.tool()
def list_due_today() -> list[dict[str, Any]]:
    """List all pending reminders that are due today."""
    return _api("GET", "/api/reminders/due-today")


@mcp.tool()
def list_due_soon(within_hrs: int = 1) -> list[dict[str, Any]]:
    """List pending reminders due within the next N hours.
    
    within_hrs: Look-ahead window in hours (default: 1)
    """
    return _api("GET", "/api/reminders/due-soon", params={"within_hrs": within_hrs})

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if _args.transport == "sse":
        log.info(f"Starting savant-reminders MCP (SSE) on {_args.host}:{_args.port}")
    mcp.run(transport=_args.transport)
