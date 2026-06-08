"""Tests for Codex session workspace assignment endpoint."""

import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def codex_dir(tmp_path, monkeypatch):
    """Set up a fake CODEX_DIR with a valid session."""
    cdir = tmp_path / "codex"
    sessions_dir = cdir / "sessions" / "2025" / "11" / "20"
    sessions_dir.mkdir(parents=True)

    session_id = "bbbb1111-2222-3333-4444-555566667777"
    session_path = sessions_dir / f"rollout-2025-11-20T22-42-15-{session_id}.jsonl"
    session_path.write_text(json.dumps({
        "id": session_id,
        "timestamp": "2025-11-20T22:42:15.327Z",
        "instructions": "Test instructions",
    }) + "\n")

    monkeypatch.setenv("CODEX_DIR", str(cdir))
    import app as app_mod
    monkeypatch.setattr(app_mod, "CODEX_DIR", str(cdir))
    monkeypatch.setattr(app_mod, "CODEX_SESSIONS_DIR", str(cdir / "sessions"))

    return {"dir": str(cdir), "session_id": session_id}


@pytest.fixture
def meta_dir(tmp_path, monkeypatch):
    """Isolated META_DIR for codex-meta.json."""
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


class TestCodexSessionWorkspaceAssign:

    def test_rejects_nonexistent_session(self, client, codex_dir, meta_dir, ws):
        fake_id = "deadbeef-0000-1111-2222-333344445555"
        resp = client.post(
            f"/api/codex/session/{fake_id}/workspace",
            json={"workspace_id": ws},
        )
        assert resp.status_code == 404
        assert "Not a Codex session" in resp.get_json()["error"]

    def test_does_not_write_meta_for_nonexistent_session(self, client, codex_dir, meta_dir, ws):
        fake_id = "deadbeef-0000-1111-2222-333344445555"
        client.post(
            f"/api/codex/session/{fake_id}/workspace",
            json={"workspace_id": ws},
        )
        meta_path = os.path.join(meta_dir, "codex-meta.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                data = json.load(f)
            assert fake_id not in data

    def test_accepts_valid_codex_session(self, client, codex_dir, meta_dir, ws):
        sid = codex_dir["session_id"]
        resp = client.post(
            f"/api/codex/session/{sid}/workspace",
            json={"workspace_id": ws},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == sid
        assert data["workspace"] == ws

    def test_unassign_valid_codex_session(self, client, codex_dir, meta_dir, ws):
        sid = codex_dir["session_id"]
        client.post(
            f"/api/codex/session/{sid}/workspace",
            json={"workspace_id": ws},
        )
        resp = client.post(
            f"/api/codex/session/{sid}/workspace",
            json={"workspace_id": None},
        )
        assert resp.status_code == 200
        assert resp.get_json()["workspace"] is None

    def test_codex_file_endpoint_reads_session_log(self, client, codex_dir):
        sid = codex_dir["session_id"]
        path = f"rollout-2025-11-20T22-42-15-{sid}.jsonl"
        resp = client.get(f"/api/codex/session/{sid}/file", query_string={"path": path})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["path"] == path
        assert "\"id\": \"bbbb1111-2222-3333-4444-555566667777\"" in data["content"]
