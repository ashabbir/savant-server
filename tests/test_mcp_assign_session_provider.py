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
