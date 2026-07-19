"""MCP Session Detection module for Savant, Codex, Copilot, and Claude sessions."""

import os
import json
from pathlib import Path

COPILOT_SESSION_DIR = os.path.expanduser("~/.config/github-copilot/sessions")
CLAUDE_SESSIONS_DIR = os.path.expanduser("~/.config/claude/sessions")


def _resolve_workspace_via_api(provider: str, session_id: str):
    """Fallback resolver via server mapping or DB."""
    try:
        from db.workspace_session_links import WorkspaceSessionLinkDB
        link = WorkspaceSessionLinkDB.find_by_session(session_id)
        if link:
            return link.get("workspace_id")
    except Exception:
        pass
    return None


def _find_codex_session_by_env():
    """Find Codex session via CODEX_SESSION_ID env var."""
    session_id = os.environ.get("CODEX_SESSION_ID")
    if not session_id:
        return None
    ws_id = _resolve_workspace_via_api("codex", session_id)
    return {
        "session_id": session_id,
        "workspace_id": ws_id,
        "provider": "codex",
    }


def _find_savant_session_by_env():
    """Find Savant session via SAVANT_SESSION_ID env var."""
    session_id = os.environ.get("SAVANT_SESSION_ID")
    if not session_id:
        return None
    ws_id = _resolve_workspace_via_api("savant", session_id)
    return {
        "session_id": session_id,
        "workspace_id": ws_id,
        "provider": "savant",
    }


def detect_session():
    """Detect current active AI session and resolved workspace."""
    codex = _find_codex_session_by_env()
    if codex:
        return codex
    
    savant = _find_savant_session_by_env()
    if savant:
        return savant

    ws_env = os.environ.get("SAVANT_WORKSPACE_ID")
    return {
        "session_id": None,
        "workspace_id": ws_env,
        "provider": None,
    }
