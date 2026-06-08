"""Regression tests for Codex session detection."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))

import session_detect


def test_find_codex_session_by_env_resolves_workspace_via_api(monkeypatch):
    session_id = "0204cc81-99b8-4a9c-bc27-30c868cb48ea"
    monkeypatch.setenv("CODEX_SESSION_ID", session_id)
    monkeypatch.setattr(session_detect, "_resolve_workspace_via_api", lambda p, sid: "ws-test-1")

    result = session_detect._find_codex_session_by_env()

    assert result == {
        "session_id": session_id,
        "workspace_id": "ws-test-1",
        "provider": "codex",
    }
