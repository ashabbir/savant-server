"""Tests for Savant session detection in MCP session_detect module."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))

import session_detect


def test_find_savant_session_by_env_resolves_workspace_via_api(monkeypatch):
    """SAVANT_SESSION_ID env var should resolve via server mapping API."""
    session_id = "20260415_091817_de93bc"
    monkeypatch.setenv("SAVANT_SESSION_ID", session_id)
    monkeypatch.setattr(session_detect, "_resolve_workspace_via_api", lambda p, sid: "ws-savant-1")

    result = session_detect._find_savant_session_by_env()

    assert result == {
        "session_id": session_id,
        "workspace_id": "ws-savant-1",
        "provider": "savant",
    }


def test_find_savant_session_by_env_no_workspace(monkeypatch):
    """SAVANT_SESSION_ID without mapping should return workspace_id=None."""
    session_id = "20260415_092054_810db8"
    monkeypatch.setenv("SAVANT_SESSION_ID", session_id)
    monkeypatch.setattr(session_detect, "_resolve_workspace_via_api", lambda p, sid: None)

    result = session_detect._find_savant_session_by_env()

    assert result == {
        "session_id": session_id,
        "workspace_id": None,
        "provider": "savant",
    }


def test_find_savant_session_by_env_no_env_var(monkeypatch):
    """Without SAVANT_SESSION_ID env var, detection should return None."""
    monkeypatch.delenv("SAVANT_SESSION_ID", raising=False)
    result = session_detect._find_savant_session_by_env()
    assert result is None


def test_detect_session_finds_savant_via_env(monkeypatch):
    """detect_session() should find Savant sessions via env when no other provider matches."""
    session_id = "20260415_091350_44407f"
    monkeypatch.setattr(session_detect, "_resolve_workspace_via_api", lambda p, sid: "ws-h")

    # Clear all other provider env vars
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_SESSION", raising=False)
    monkeypatch.delenv("CODEX_SESSION_PATH", raising=False)
    monkeypatch.delenv("CODEX_SESSION_LOG", raising=False)
    monkeypatch.delenv("GEMINI_CLI", raising=False)
    monkeypatch.delenv("SAVANT_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("SAVANT_SESSION_ID", raising=False)

    monkeypatch.setenv("SAVANT_SESSION_ID", session_id)

    # Patch session dirs so Copilot/Claude don't match
    monkeypatch.setattr(session_detect, "COPILOT_SESSION_DIR", "/tmp/no-copilot")
    monkeypatch.setattr(session_detect, "CLAUDE_SESSIONS_DIR", "/tmp/no-claude")

    result = session_detect.detect_session()
    assert result["session_id"] == session_id
    assert result["provider"] == "savant"
    assert result["workspace_id"] == "ws-h"
