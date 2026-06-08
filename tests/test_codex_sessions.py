"""Tests for Codex session loading endpoints."""

import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def codex_dir(tmp_path, monkeypatch):
    """Set up a fake CODEX_DIR with a sample session."""
    cdir = tmp_path / "codex"
    sessions_dir = cdir / "sessions" / "2025" / "11" / "20"
    sessions_dir.mkdir(parents=True)

    session_id = "0204cc81-99b8-4a9c-bc27-30c868cb48ea"
    session_path = sessions_dir / f"rollout-2025-11-20T22-42-15-{session_id}.jsonl"
    session_lines = [
        json.dumps({
            "id": session_id,
            "timestamp": "2025-11-20T22:42:15.327Z",
            "instructions": "Test instructions",
            "git": {"branch": "main", "repository_url": "https://example.com/repo.git"},
        }),
        json.dumps({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "<environment_context>\n  <cwd>/tmp/project</cwd>\n</environment_context>"}],
        }),
        json.dumps({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Build the feature"}],
        }),
        json.dumps({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Acknowledged"}],
        }),
        json.dumps({
            "type": "function_call",
            "name": "shell",
            "arguments": json.dumps({"command": ["ls"]}),
            "call_id": "call_123",
        }),
        json.dumps({
            "type": "function_call_output",
            "call_id": "call_123",
            "output": json.dumps({"output": "ok", "metadata": {"exit_code": 0}}),
        }),
    ]
    session_path.write_text("\n".join(session_lines) + "\n")

    monkeypatch.setenv("CODEX_DIR", str(cdir))
    import app as app_mod
    monkeypatch.setattr(app_mod, "CODEX_DIR", str(cdir))
    monkeypatch.setattr(app_mod, "CODEX_SESSIONS_DIR", str(cdir / "sessions"))
    app_mod._bg_cache["codex_sessions"] = None
    app_mod._bg_cache["codex_usage"] = None

    return {"dir": str(cdir), "session_id": session_id}


def test_codex_sessions_list(client, codex_dir):
    resp = client.get("/api/codex/sessions")
    assert resp.status_code == 200
    data = resp.get_json()
    sessions = data.get("sessions") or []
    assert sessions, "Expected codex sessions to be listed"
    assert sessions[0]["id"] == codex_dir["session_id"]
    assert sessions[0]["provider"] == "codex"


def test_codex_session_detail(client, codex_dir):
    sid = codex_dir["session_id"]
    resp = client.get(f"/api/codex/session/{sid}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["id"] == sid
    assert data["provider"] == "codex"


def test_codex_session_project_files(client, codex_dir):
    sid = codex_dir["session_id"]
    resp = client.get(f"/api/codex/session/{sid}/project-files")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["cwd"] == "/tmp/project"
    assert data["files"] == []


def test_codex_session_git_changes(client, codex_dir):
    sid = codex_dir["session_id"]
    resp = client.get(f"/api/codex/session/{sid}/git-changes")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["commits"] == []
    assert data["git_commands"] == []
