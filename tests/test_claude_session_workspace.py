"""Tests for Claude session workspace assignment endpoint.

Validates that POST /api/claude/session/<id>/workspace correctly rejects
non-Claude sessions (404) so the MCP tool falls through to the Copilot route,
and accepts valid Claude sessions.
"""

import sys, os, json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def claude_dir(tmp_path, monkeypatch):
    """Set up a fake CLAUDE_DIR with a valid session."""
    cdir = tmp_path / "claude"
    cdir.mkdir()
    projects = cdir / "projects" / "test-project"
    projects.mkdir(parents=True)

    # Create a valid Claude session with a JSONL file
    session_id = "aaaa1111-bbbb-cccc-dddd-eeee2222ffff"
    jsonl = projects / f"{session_id}.jsonl"
    jsonl.write_text('{"type":"human","content":"hello"}\n')

    # Also create the session artifact directory
    sess_dir = projects / session_id
    sess_dir.mkdir()

    monkeypatch.setenv("CLAUDE_DIR", str(cdir))
    import app as app_mod
    monkeypatch.setattr(app_mod, "CLAUDE_DIR", str(cdir))

    return {"dir": str(cdir), "session_id": session_id}


@pytest.fixture
def meta_dir(tmp_path, monkeypatch):
    """Isolated META_DIR for claude-meta.json."""
    mdir = tmp_path / "meta"
    mdir.mkdir()
    monkeypatch.setenv("META_DIR", str(mdir))
    import app as app_mod
    monkeypatch.setattr(app_mod, "META_DIR", str(mdir))
    return str(mdir)


@pytest.fixture
def ws(client):
    """Create a workspace and return its ID."""
    resp = client.post("/api/workspaces", json={"name": "Test WS"})
    assert resp.status_code == 200
    return resp.get_json()["workspace_id"]


class TestClaudeSessionWorkspaceAssign:

    def test_rejects_nonexistent_session(self, client, claude_dir, meta_dir, ws):
        """A session ID that doesn't exist as a Claude session should 404."""
        fake_id = "deadbeef-0000-1111-2222-333344445555"
        resp = client.post(
            f"/api/claude/session/{fake_id}/workspace",
            json={"workspace_id": ws},
        )
        assert resp.status_code == 404
        assert "Not a Claude session" in resp.get_json()["error"]

    def test_rejects_copilot_session_id(self, client, claude_dir, meta_dir, ws):
        """A Copilot session ID passed to the Claude endpoint should 404."""
        copilot_id = "ff32997e-cb59-42cd-9f88-3bd5b45c4fc0"
        resp = client.post(
            f"/api/claude/session/{copilot_id}/workspace",
            json={"workspace_id": ws},
        )
        assert resp.status_code == 404

    def test_does_not_write_meta_for_nonexistent_session(self, client, claude_dir, meta_dir, ws):
        """Rejecting a non-Claude session must not leave stale metadata."""
        fake_id = "deadbeef-0000-1111-2222-333344445555"
        client.post(
            f"/api/claude/session/{fake_id}/workspace",
            json={"workspace_id": ws},
        )
        meta_path = os.path.join(meta_dir, "claude-meta.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                data = json.load(f)
            assert fake_id not in data, "Stale meta written for non-existent session"

    def test_accepts_valid_claude_session(self, client, claude_dir, meta_dir, ws):
        """A real Claude session should be assigned successfully."""
        sid = claude_dir["session_id"]
        resp = client.post(
            f"/api/claude/session/{sid}/workspace",
            json={"workspace_id": ws},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == sid
        assert data["workspace"] == ws

    def test_unassign_valid_claude_session(self, client, claude_dir, meta_dir, ws):
        """Unassigning (workspace_id=None) should work for valid sessions."""
        sid = claude_dir["session_id"]
        # Assign first
        client.post(
            f"/api/claude/session/{sid}/workspace",
            json={"workspace_id": ws},
        )
        # Unassign
        resp = client.post(
            f"/api/claude/session/{sid}/workspace",
            json={"workspace_id": None},
        )
        assert resp.status_code == 200
        assert resp.get_json()["workspace"] is None
