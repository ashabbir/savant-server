"""Tests for MCP session-to-workspace assignment payload shape."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))

import server as mcp_server


def test_assign_session_to_workspace_sends_session_id_only(monkeypatch):
    session_id = "019ee2b4-3d57-7ac1-94b3-232ec4af933b"

    captured = {}

    def fake_api(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_api", fake_api)

    result = mcp_server.assign_session_to_workspace("ws-123", session_id=session_id)

    assert result == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/workspaces/ws-123/session-links"
    assert captured["json"] == {"session_id": session_id}


def test_get_current_workspace_does_not_require_provider_detection(monkeypatch):
    session_id = "019ee2b4-3d57-7ac1-94b3-232ec4af933b"

    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("SAVANT_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("GEMINI_SESSION_ID", raising=False)
    monkeypatch.delenv("SESSION_ID", raising=False)
    monkeypatch.delenv("COPILOT_SESSION_ID", raising=False)

    calls = []

    class FakeResp:
        def __init__(self, workspace_id=None, status_code=200):
            self._workspace_id = workspace_id
            self.status_code = status_code

        def json(self):
            return {"workspace_id": self._workspace_id}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        if url.endswith("/api/session-links/resolve"):
            return FakeResp(None)
        return FakeResp(None)

    monkeypatch.setattr(mcp_server.requests, "get", fake_get)

    result = mcp_server.get_current_workspace(session_id=session_id)

    assert result["error"] == "No workspace assigned to this session."
    assert calls[0][1] == {"provider": "copilot", "session_id": session_id}
