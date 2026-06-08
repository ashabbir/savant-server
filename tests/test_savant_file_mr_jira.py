"""Tests for Savant session file, MR, and Jira ticket endpoints.

These endpoints bring Savant to parity with Claude/Gemini/Codex:
- GET/PUT /api/savant/session/<id>/file (read/write session artifact files)
- GET     /api/savant/session/<id>/file/raw (raw file download)
- GET/POST/DELETE /api/savant/session/<id>/mr (merge request links)
- GET/POST/DELETE /api/savant/session/<id>/jira-ticket (Jira ticket links)
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_savant_session(session_id="20260415_091817_de93bc", model="claude-opus-4.6"):
    """Build a realistic Savant session JSON payload."""
    return {
        "session_id": session_id,
        "model": model,
        "base_url": "https://api.githubcopilot.com",
        "platform": "cli",
        "session_start": "2026-04-15T09:18:17.040230",
        "last_updated": "2026-04-15T09:18:40.067219",
        "system_prompt": "You are a helpful assistant.",
        "tools": ["terminal", "read_file", "patch"],
        "message_count": 2,
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hi there!", "finish_reason": "stop", "reasoning": "", "tool_calls": []},
        ],
    }


@pytest.fixture
def savant_env(tmp_path, monkeypatch):
    """Set up a temporary Savant directory with session + artifact files."""
    hdir = tmp_path / "savant"
    sessions = hdir / "sessions"
    sessions.mkdir(parents=True)
    meta_dir = hdir / ".savant-meta"
    meta_dir.mkdir(parents=True)

    session_id = "20260415_091817_de93bc"
    payload = _make_savant_session(session_id)
    (sessions / f"session_{session_id}.json").write_text(json.dumps(payload))

    # Create artifact directory with test files
    artifact_dir = sessions / session_id
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "output.txt").write_text("Hello from Savant artifact!")
    sub = artifact_dir / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("# Nested file")

    # Write initial meta (empty mrs/jira_tickets)
    (meta_dir / f"{session_id}.json").write_text(
        json.dumps({"workspace": None, "starred": False, "archived": False})
    )

    monkeypatch.setenv("SAVANT_DIR", str(hdir))
    import app as app_mod

    monkeypatch.setattr(app_mod, "SAVANT_DIR", str(hdir))
    monkeypatch.setattr(app_mod, "SAVANT_SESSIONS_DIR", str(sessions))
    monkeypatch.setattr(app_mod, "SAVANT_META_DIR", str(meta_dir))
    monkeypatch.setattr(app_mod, "SAVANT_STATE_DB", str(hdir / "state.db"))
    app_mod._bg_cache["savant_sessions"] = None
    app_mod._bg_cache["savant_usage"] = None
    return {
        "session_id": session_id,
        "sessions_dir": str(sessions),
        "meta_dir": str(meta_dir),
        "artifact_dir": str(artifact_dir),
    }


@pytest.fixture
def client(_isolated_db, savant_env):
    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ═══════════════════════════════════════════════════════════════════════════════
# FILE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSavantFileRead:
    """GET /api/savant/session/<id>/file — read artifact files."""

    def test_read_file_success(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.get(f"/api/savant/session/{sid}/file?path=output.txt")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["path"] == "output.txt"
        assert data["content"] == "Hello from Savant artifact!"
        assert data["size"] > 0
        assert data["truncated"] is False
        assert "host_path" in data

    def test_read_nested_file(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.get(f"/api/savant/session/{sid}/file?path=sub/nested.md")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["content"] == "# Nested file"

    def test_read_file_no_path(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.get(f"/api/savant/session/{sid}/file")
        assert resp.status_code == 400

    def test_read_file_path_traversal(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.get(f"/api/savant/session/{sid}/file?path=../../../etc/passwd")
        assert resp.status_code == 400

    def test_read_file_not_found(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.get(f"/api/savant/session/{sid}/file?path=nonexistent.txt")
        assert resp.status_code == 404

    def test_read_file_bad_session(self, client, savant_env):
        resp = client.get("/api/savant/session/NOSUCHSESSION/file?path=output.txt")
        assert resp.status_code == 404


class TestSavantFileRaw:
    """GET /api/savant/session/<id>/file/raw — serve raw file."""

    def test_raw_file_success(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.get(f"/api/savant/session/{sid}/file/raw?path=output.txt")
        assert resp.status_code == 200
        assert b"Hello from Savant artifact!" in resp.data

    def test_raw_file_no_path(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.get(f"/api/savant/session/{sid}/file/raw")
        assert resp.status_code == 400

    def test_raw_file_traversal(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.get(f"/api/savant/session/{sid}/file/raw?path=../../etc/passwd")
        assert resp.status_code == 400

    def test_raw_file_not_found(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.get(f"/api/savant/session/{sid}/file/raw?path=missing.txt")
        assert resp.status_code == 404

    def test_raw_file_bad_session(self, client, savant_env):
        resp = client.get("/api/savant/session/NOSUCHSESSION/file/raw?path=output.txt")
        assert resp.status_code == 404


class TestSavantFileWrite:
    """PUT /api/savant/session/<id>/file — write artifact files."""

    def test_write_file_success(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.put(
            f"/api/savant/session/{sid}/file",
            data=json.dumps({"path": "output.txt", "content": "Updated content!"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["size"] == len("Updated content!")

        # Verify the file was actually written
        fpath = os.path.join(savant_env["artifact_dir"], "output.txt")
        with open(fpath) as f:
            assert f.read() == "Updated content!"

    def test_write_file_no_path(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.put(
            f"/api/savant/session/{sid}/file",
            data=json.dumps({"content": "no path given"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_write_file_no_content(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.put(
            f"/api/savant/session/{sid}/file",
            data=json.dumps({"path": "output.txt"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_write_file_traversal(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.put(
            f"/api/savant/session/{sid}/file",
            data=json.dumps({"path": "../../evil.txt", "content": "hax"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_write_file_not_found(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.put(
            f"/api/savant/session/{sid}/file",
            data=json.dumps({"path": "nonexistent.txt", "content": "new"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_write_file_bad_session(self, client, savant_env):
        resp = client.put(
            "/api/savant/session/NOSUCHSESSION/file",
            data=json.dumps({"path": "output.txt", "content": "nope"}),
            content_type="application/json",
        )
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# MERGE REQUEST (MR) ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSavantMR:
    """GET/POST/DELETE /api/savant/session/<id>/mr — merge request links."""

    def test_mr_get_empty(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.get(f"/api/savant/session/{sid}/mr")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_mr_add(self, client, savant_env):
        sid = savant_env["session_id"]
        mr_payload = {
            "url": "https://gitlab.com/team/repo/-/merge_requests/42",
            "status": "open",
            "jira": "PROJ-123",
            "role": "author",
        }
        resp = client.post(
            f"/api/savant/session/{sid}/mr",
            data=json.dumps(mr_payload),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == sid
        assert len(data["mrs"]) == 1
        mr = data["mrs"][0]
        assert mr["url"] == mr_payload["url"]
        assert mr["status"] == "open"
        assert mr["jira"] == "PROJ-123"
        assert mr["role"] == "author"
        assert "id" in mr  # auto-generated ID

    def test_mr_add_with_custom_id(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.post(
            f"/api/savant/session/{sid}/mr",
            data=json.dumps({"id": "mr-custom-1", "url": "https://gitlab.com/a/b/-/merge_requests/1"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mrs"][0]["id"] == "mr-custom-1"

    def test_mr_upsert(self, client, savant_env):
        sid = savant_env["session_id"]
        # Add
        client.post(
            f"/api/savant/session/{sid}/mr",
            data=json.dumps({"id": "mr-1", "url": "https://gitlab.com/a/b/-/merge_requests/1", "status": "open"}),
            content_type="application/json",
        )
        # Update same ID
        resp = client.post(
            f"/api/savant/session/{sid}/mr",
            data=json.dumps({"id": "mr-1", "url": "https://gitlab.com/a/b/-/merge_requests/1", "status": "merged"}),
            content_type="application/json",
        )
        data = resp.get_json()
        assert len(data["mrs"]) == 1  # still one MR, not two
        assert data["mrs"][0]["status"] == "merged"

    def test_mr_get_after_add(self, client, savant_env):
        sid = savant_env["session_id"]
        client.post(
            f"/api/savant/session/{sid}/mr",
            data=json.dumps({"url": "https://gitlab.com/x/y/-/merge_requests/99", "status": "review"}),
            content_type="application/json",
        )
        resp = client.get(f"/api/savant/session/{sid}/mr")
        assert resp.status_code == 200
        mrs = resp.get_json()
        assert len(mrs) == 1
        assert mrs[0]["url"] == "https://gitlab.com/x/y/-/merge_requests/99"

    def test_mr_delete(self, client, savant_env):
        sid = savant_env["session_id"]
        # Add two MRs
        client.post(
            f"/api/savant/session/{sid}/mr",
            data=json.dumps({"id": "mr-a", "url": "url-a"}),
            content_type="application/json",
        )
        client.post(
            f"/api/savant/session/{sid}/mr",
            data=json.dumps({"id": "mr-b", "url": "url-b"}),
            content_type="application/json",
        )
        # Delete one
        resp = client.delete(f"/api/savant/session/{sid}/mr/mr-a")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["deleted"] is True
        # Verify only one remains
        resp2 = client.get(f"/api/savant/session/{sid}/mr")
        mrs = resp2.get_json()
        assert len(mrs) == 1
        assert mrs[0]["id"] == "mr-b"

    def test_mr_delete_nonexistent(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.delete(f"/api/savant/session/{sid}/mr/no-such-mr")
        assert resp.status_code == 200  # idempotent — no error

    def test_mr_persists_in_meta(self, client, savant_env):
        """MR data should be persisted in the session meta file."""
        sid = savant_env["session_id"]
        client.post(
            f"/api/savant/session/{sid}/mr",
            data=json.dumps({"id": "mr-persist", "url": "https://gitlab.com/persist/-/merge_requests/1"}),
            content_type="application/json",
        )
        # Read meta file directly
        meta_path = os.path.join(savant_env["meta_dir"], f"{sid}.json")
        with open(meta_path) as f:
            meta = json.load(f)
        assert len(meta["mrs"]) == 1
        assert meta["mrs"][0]["id"] == "mr-persist"


# ═══════════════════════════════════════════════════════════════════════════════
# JIRA TICKET ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSavantJira:
    """GET/POST/DELETE /api/savant/session/<id>/jira-ticket — Jira ticket links."""

    def test_jira_get_empty(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.get(f"/api/savant/session/{sid}/jira-ticket")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_jira_add(self, client, savant_env):
        sid = savant_env["session_id"]
        payload = {
            "ticket_key": "APPSERV-4567",
            "title": "Fix auth bug",
            "status": "in-progress",
            "assignee": "ashabbir",
            "role": "assignee",
        }
        resp = client.post(
            f"/api/savant/session/{sid}/jira-ticket",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == sid
        assert len(data["jira_tickets"]) == 1
        ticket = data["jira_tickets"][0]
        assert ticket["ticket_key"] == "APPSERV-4567"
        assert ticket["title"] == "Fix auth bug"
        assert ticket["status"] == "in-progress"
        assert ticket["assignee"] == "ashabbir"
        assert "id" in ticket

    def test_jira_add_with_custom_id(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.post(
            f"/api/savant/session/{sid}/jira-ticket",
            data=json.dumps({"id": "jira-custom-1", "ticket_key": "PIM-100"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["jira_tickets"][0]["id"] == "jira-custom-1"

    def test_jira_uppercase_key(self, client, savant_env):
        """ticket_key should be uppercased automatically."""
        sid = savant_env["session_id"]
        resp = client.post(
            f"/api/savant/session/{sid}/jira-ticket",
            data=json.dumps({"ticket_key": "appserv-100"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["jira_tickets"][0]["ticket_key"] == "APPSERV-100"

    def test_jira_upsert(self, client, savant_env):
        sid = savant_env["session_id"]
        # Add
        client.post(
            f"/api/savant/session/{sid}/jira-ticket",
            data=json.dumps({"id": "jt-1", "ticket_key": "PROJ-1", "status": "todo"}),
            content_type="application/json",
        )
        # Update same ID
        resp = client.post(
            f"/api/savant/session/{sid}/jira-ticket",
            data=json.dumps({"id": "jt-1", "ticket_key": "PROJ-1", "status": "done"}),
            content_type="application/json",
        )
        data = resp.get_json()
        assert len(data["jira_tickets"]) == 1
        assert data["jira_tickets"][0]["status"] == "done"

    def test_jira_get_after_add(self, client, savant_env):
        sid = savant_env["session_id"]
        client.post(
            f"/api/savant/session/{sid}/jira-ticket",
            data=json.dumps({"ticket_key": "XY-999", "title": "Test ticket"}),
            content_type="application/json",
        )
        resp = client.get(f"/api/savant/session/{sid}/jira-ticket")
        assert resp.status_code == 200
        tickets = resp.get_json()
        assert len(tickets) == 1
        assert tickets[0]["ticket_key"] == "XY-999"

    def test_jira_delete(self, client, savant_env):
        sid = savant_env["session_id"]
        # Add two tickets
        client.post(
            f"/api/savant/session/{sid}/jira-ticket",
            data=json.dumps({"id": "jt-a", "ticket_key": "A-1"}),
            content_type="application/json",
        )
        client.post(
            f"/api/savant/session/{sid}/jira-ticket",
            data=json.dumps({"id": "jt-b", "ticket_key": "B-2"}),
            content_type="application/json",
        )
        # Delete one
        resp = client.delete(f"/api/savant/session/{sid}/jira-ticket/jt-a")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["deleted"] is True
        # Verify only one remains
        resp2 = client.get(f"/api/savant/session/{sid}/jira-ticket")
        tickets = resp2.get_json()
        assert len(tickets) == 1
        assert tickets[0]["id"] == "jt-b"

    def test_jira_delete_nonexistent(self, client, savant_env):
        sid = savant_env["session_id"]
        resp = client.delete(f"/api/savant/session/{sid}/jira-ticket/no-such-ticket")
        assert resp.status_code == 200  # idempotent

    def test_jira_persists_in_meta(self, client, savant_env):
        """Jira data should be persisted in the session meta file."""
        sid = savant_env["session_id"]
        client.post(
            f"/api/savant/session/{sid}/jira-ticket",
            data=json.dumps({"id": "jt-persist", "ticket_key": "PERSIST-1", "title": "Persistent"}),
            content_type="application/json",
        )
        meta_path = os.path.join(savant_env["meta_dir"], f"{sid}.json")
        with open(meta_path) as f:
            meta = json.load(f)
        assert len(meta["jira_tickets"]) == 1
        assert meta["jira_tickets"][0]["ticket_key"] == "PERSIST-1"


# ═══════════════════════════════════════════════════════════════════════════════
# CACHE SYNCHRONIZATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSavantCacheSync:
    """Verify that MR/Jira changes sync to the background cache."""

    def _warm_cache(self, client, savant_env):
        """Force a cache warm by listing sessions."""
        client.get("/api/savant/sessions")

    def test_mr_add_updates_cache(self, client, savant_env):
        sid = savant_env["session_id"]
        self._warm_cache(client, savant_env)
        client.post(
            f"/api/savant/session/{sid}/mr",
            data=json.dumps({"id": "mr-cache", "url": "https://gitlab.com/cache-test"}),
            content_type="application/json",
        )
        import app as app_mod
        with app_mod._bg_lock:
            sessions = app_mod._bg_cache.get("savant_sessions") or []
            s = next((s for s in sessions if s["id"] == sid), None)
            if s:
                assert len(s.get("mrs", [])) == 1

    def test_mr_delete_updates_cache(self, client, savant_env):
        sid = savant_env["session_id"]
        self._warm_cache(client, savant_env)
        client.post(
            f"/api/savant/session/{sid}/mr",
            data=json.dumps({"id": "mr-del", "url": "https://gitlab.com/del-test"}),
            content_type="application/json",
        )
        client.delete(f"/api/savant/session/{sid}/mr/mr-del")
        import app as app_mod
        with app_mod._bg_lock:
            sessions = app_mod._bg_cache.get("savant_sessions") or []
            s = next((s for s in sessions if s["id"] == sid), None)
            if s:
                assert len(s.get("mrs", [])) == 0

    def test_jira_add_updates_cache(self, client, savant_env):
        sid = savant_env["session_id"]
        self._warm_cache(client, savant_env)
        client.post(
            f"/api/savant/session/{sid}/jira-ticket",
            data=json.dumps({"id": "jt-cache", "ticket_key": "CACHE-1"}),
            content_type="application/json",
        )
        import app as app_mod
        with app_mod._bg_lock:
            sessions = app_mod._bg_cache.get("savant_sessions") or []
            s = next((s for s in sessions if s["id"] == sid), None)
            if s:
                assert len(s.get("jira_tickets", [])) == 1

    def test_jira_delete_updates_cache(self, client, savant_env):
        sid = savant_env["session_id"]
        self._warm_cache(client, savant_env)
        client.post(
            f"/api/savant/session/{sid}/jira-ticket",
            data=json.dumps({"id": "jt-del", "ticket_key": "DEL-1"}),
            content_type="application/json",
        )
        client.delete(f"/api/savant/session/{sid}/jira-ticket/jt-del")
        import app as app_mod
        with app_mod._bg_lock:
            sessions = app_mod._bg_cache.get("savant_sessions") or []
            s = next((s for s in sessions if s["id"] == sid), None)
            if s:
                assert len(s.get("jira_tickets", [])) == 0
