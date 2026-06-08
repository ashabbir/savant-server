"""Regression tests for Jira ticket REST API endpoints.

Covers: create, get, list, update, delete, notes, assign/unassign to session.
These validate the fixes for:
  - JiraTicketDB.get() → get_by_id() rename
  - FK constraint removal on workspace_id
  - _get_jira_registry_by_id() no longer depends on jira_tickets.json
"""

import sys, os, json, tempfile, shutil
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _create_workspace(client, name="Jira Test WS"):
    resp = client.post("/api/workspaces", json={"name": name})
    assert resp.status_code == 200, f"Workspace creation failed: {resp.data}"
    return resp.get_json()["workspace_id"]


def _create_ticket(client, key="TEST-100", title="Test ticket", **kwargs):
    payload = {"ticket_key": key, "title": title, **kwargs}
    return client.post("/api/jira-tickets", json=payload)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def ws(client):
    return _create_workspace(client)


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    """Create a temporary session directory with a fake session."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    sess = sdir / "test-session-1"
    sess.mkdir()
    # Write minimal session meta
    meta_file = sess / "session.json"
    meta_file.write_text(json.dumps({"id": "test-session-1"}))
    monkeypatch.setenv("SESSION_DIR", str(sdir))
    # Patch the module-level SESSION_DIR in app
    import app as app_mod
    monkeypatch.setattr(app_mod, "SESSION_DIR", str(sdir))
    return str(sdir)


# ── Create ───────────────────────────────────────────────────────────────────

class TestJiraCreate:

    def test_create_with_workspace(self, client, ws):
        resp = _create_ticket(client, key="PROJ-1", workspace_id=ws)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ticket_key"] == "PROJ-1"
        assert "id" in data or "ticket_id" in data

    def test_create_without_workspace(self, client):
        """Empty workspace_id must NOT cause FK constraint error."""
        resp = _create_ticket(client, key="PROJ-2")
        assert resp.status_code == 200, f"FK error? {resp.data}"
        data = resp.get_json()
        assert data["ticket_key"] == "PROJ-2"

    def test_create_returns_url(self, client):
        resp = _create_ticket(client, key="PROJ-3")
        data = resp.get_json()
        assert "url" in data
        assert "PROJ-3" in data["url"]

    def test_create_duplicate_rejected(self, client):
        _create_ticket(client, key="DUP-1")
        resp = _create_ticket(client, key="DUP-1")
        assert resp.status_code == 409

    def test_create_missing_key(self, client):
        resp = client.post("/api/jira-tickets", json={"title": "No key"})
        assert resp.status_code == 400

    def test_create_uppercases_key(self, client):
        resp = _create_ticket(client, key="lower-99")
        data = resp.get_json()
        assert data["ticket_key"] == "LOWER-99"

    def test_create_default_status(self, client):
        resp = _create_ticket(client, key="DEF-1")
        data = resp.get_json()
        assert data.get("status") == "todo"

    def test_create_custom_status(self, client):
        resp = _create_ticket(client, key="STAT-1", status="in-progress")
        data = resp.get_json()
        assert data.get("status") == "in-progress"


# ── Read / List ──────────────────────────────────────────────────────────────

class TestJiraRead:

    def test_get_by_id(self, client):
        cr = _create_ticket(client, key="GET-1")
        tid = cr.get_json().get("id") or cr.get_json().get("ticket_id")
        resp = client.get(f"/api/jira-tickets/{tid}")
        assert resp.status_code == 200
        assert resp.get_json()["ticket_key"] == "GET-1"

    def test_get_not_found(self, client):
        resp = client.get("/api/jira-tickets/nonexistent")
        assert resp.status_code == 404

    def test_get_by_ticket_key(self, client):
        """GET /api/jira-tickets/<key> should resolve by ticket_key."""
        _create_ticket(client, key="KEY-1")
        resp = client.get("/api/jira-tickets/KEY-1")
        assert resp.status_code == 200
        assert resp.get_json()["ticket_key"] == "KEY-1"

    def test_get_by_ticket_key_case_insensitive(self, client):
        _create_ticket(client, key="CASE-1")
        resp = client.get("/api/jira-tickets/case-1")
        assert resp.status_code == 200
        assert resp.get_json()["ticket_key"] == "CASE-1"

    def test_list_by_workspace(self, client, ws):
        _create_ticket(client, key="LST-1", workspace_id=ws)
        _create_ticket(client, key="LST-2", workspace_id=ws)
        _create_ticket(client, key="LST-3")  # no workspace
        resp = client.get("/api/jira-tickets", query_string={"workspace_id": ws})
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 2

    def test_list_all(self, client, ws):
        _create_ticket(client, key="ALL-1", workspace_id=ws)
        _create_ticket(client, key="ALL-2")
        resp = client.get("/api/jira-tickets", query_string={"workspace_id": ""})
        assert resp.status_code == 200

    def test_list_persists_after_create(self, client):
        """Tickets must survive a re-read (verifies SQLite persistence, not just in-memory)."""
        _create_ticket(client, key="PER-1")
        _create_ticket(client, key="PER-2")
        resp = client.get("/api/jira-tickets", query_string={"workspace_id": ""})
        keys = [t["ticket_key"] for t in resp.get_json()]
        assert "PER-1" in keys
        assert "PER-2" in keys


# ── Update ───────────────────────────────────────────────────────────────────

class TestJiraUpdate:

    def test_update_title(self, client):
        cr = _create_ticket(client, key="UPD-1", title="Old")
        tid = cr.get_json().get("id") or cr.get_json().get("ticket_id")
        resp = client.put(f"/api/jira-tickets/{tid}", json={"title": "New"})
        assert resp.status_code == 200
        assert resp.get_json()["title"] == "New"

    def test_update_status(self, client):
        cr = _create_ticket(client, key="UPD-2")
        tid = cr.get_json().get("id") or cr.get_json().get("ticket_id")
        resp = client.put(f"/api/jira-tickets/{tid}", json={"status": "done"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "done"

    def test_update_not_found(self, client):
        resp = client.put("/api/jira-tickets/fake", json={"title": "X"})
        assert resp.status_code == 404

    def test_update_by_ticket_key(self, client):
        """PUT /api/jira-tickets/<key> should resolve by ticket_key."""
        _create_ticket(client, key="UPK-1", title="Old")
        resp = client.put("/api/jira-tickets/UPK-1", json={"title": "New via key"})
        assert resp.status_code == 200
        assert resp.get_json()["title"] == "New via key"


# ── Delete ───────────────────────────────────────────────────────────────────

class TestJiraDelete:

    def test_delete(self, client):
        cr = _create_ticket(client, key="DEL-1")
        tid = cr.get_json().get("id") or cr.get_json().get("ticket_id")
        resp = client.delete(f"/api/jira-tickets/{tid}")
        assert resp.status_code == 200
        # Verify gone
        resp2 = client.get(f"/api/jira-tickets/{tid}")
        assert resp2.status_code == 404


# ── Notes ────────────────────────────────────────────────────────────────────

class TestJiraNotes:

    def test_add_note(self, client):
        cr = _create_ticket(client, key="NOTE-1")
        tid = cr.get_json().get("id") or cr.get_json().get("ticket_id")
        resp = client.post(f"/api/jira-tickets/{tid}/notes", json={"text": "Hello"})
        assert resp.status_code == 200

    def test_list_notes(self, client):
        cr = _create_ticket(client, key="NOTE-2")
        tid = cr.get_json().get("id") or cr.get_json().get("ticket_id")
        client.post(f"/api/jira-tickets/{tid}/notes", json={"text": "Note A"})
        client.post(f"/api/jira-tickets/{tid}/notes", json={"text": "Note B"})
        resp = client.get(f"/api/jira-tickets/{tid}/notes")
        assert resp.status_code == 200
        notes = resp.get_json()
        assert len(notes) >= 2


# ── Assign / Unassign to Session ─────────────────────────────────────────────

class TestJiraSessionAssign:

    def test_assign_to_session(self, client, session_dir):
        cr = _create_ticket(client, key="ASN-1")
        tid = cr.get_json().get("id") or cr.get_json().get("ticket_id")
        resp = client.post(
            "/api/session/test-session-1/assign-jira",
            json={"ticket_id": tid, "role": "assignee"},
        )
        assert resp.status_code == 200, f"Assign failed: {resp.data}"
        data = resp.get_json()
        assert data["session_id"] == "test-session-1"
        assert any(l["ticket_id"] == tid for l in data["jira_tickets"])

    def test_assign_duplicate_rejected(self, client, session_dir):
        cr = _create_ticket(client, key="ASN-2")
        tid = cr.get_json().get("id") or cr.get_json().get("ticket_id")
        client.post("/api/session/test-session-1/assign-jira", json={"ticket_id": tid})
        resp = client.post("/api/session/test-session-1/assign-jira", json={"ticket_id": tid})
        assert resp.status_code == 409

    def test_assign_bad_session(self, client, session_dir):
        cr = _create_ticket(client, key="ASN-3")
        tid = cr.get_json().get("id") or cr.get_json().get("ticket_id")
        resp = client.post(
            "/api/session/nonexistent-session/assign-jira",
            json={"ticket_id": tid},
        )
        assert resp.status_code == 404

    def test_assign_bad_ticket(self, client, session_dir):
        resp = client.post(
            "/api/session/test-session-1/assign-jira",
            json={"ticket_id": "no-such-ticket"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.get_json()["error"].lower()

    def test_unassign(self, client, session_dir):
        cr = _create_ticket(client, key="UNA-1")
        tid = cr.get_json().get("id") or cr.get_json().get("ticket_id")
        client.post("/api/session/test-session-1/assign-jira", json={"ticket_id": tid})
        resp = client.post(
            "/api/session/test-session-1/unassign-jira",
            json={"ticket_id": tid},
        )
        assert resp.status_code == 200
        assert resp.get_json()["removed"] == tid

    def test_unassign_not_assigned(self, client, session_dir):
        cr = _create_ticket(client, key="UNA-2")
        tid = cr.get_json().get("id") or cr.get_json().get("ticket_id")
        resp = client.post(
            "/api/session/test-session-1/unassign-jira",
            json={"ticket_id": tid},
        )
        assert resp.status_code == 404

    def test_assign_missing_ticket_id(self, client, session_dir):
        resp = client.post(
            "/api/session/test-session-1/assign-jira",
            json={},
        )
        assert resp.status_code == 400


# ── DB Layer Direct Tests ────────────────────────────────────────────────────

class TestJiraTicketDB:
    """Test the JiraTicketDB class directly (not through Flask routes)."""

    def test_create_and_get(self, _isolated_db):
        from db.jira_tickets import JiraTicketDB
        t = JiraTicketDB.create({
            "ticket_id": "jt-1",
            "workspace_id": "",
            "ticket_key": "DB-1",
            "title": "DB test",
        })
        assert t["ticket_id"] == "jt-1"
        got = JiraTicketDB.get_by_id("jt-1")
        assert got is not None
        assert got["ticket_key"] == "DB-1"

    def test_get_by_key(self, _isolated_db):
        from db.jira_tickets import JiraTicketDB
        JiraTicketDB.create({
            "ticket_id": "jt-2",
            "workspace_id": "",
            "ticket_key": "DB-2",
        })
        got = JiraTicketDB.get_by_key("DB-2")
        assert got is not None
        assert got["ticket_id"] == "jt-2"

    def test_list_all(self, _isolated_db):
        from db.jira_tickets import JiraTicketDB
        JiraTicketDB.create({"ticket_id": "jt-a", "workspace_id": "", "ticket_key": "LA-1"})
        JiraTicketDB.create({"ticket_id": "jt-b", "workspace_id": "", "ticket_key": "LA-2"})
        all_t = JiraTicketDB.list_all()
        assert len(all_t) >= 2

    def test_update(self, _isolated_db):
        from db.jira_tickets import JiraTicketDB
        JiraTicketDB.create({"ticket_id": "jt-u", "workspace_id": "", "ticket_key": "UP-1", "title": "Old"})
        updated = JiraTicketDB.update("jt-u", {"title": "New", "status": "done"})
        assert updated["title"] == "New"
        assert updated["status"] == "done"

    def test_delete(self, _isolated_db):
        from db.jira_tickets import JiraTicketDB
        JiraTicketDB.create({"ticket_id": "jt-d", "workspace_id": "", "ticket_key": "DL-1"})
        assert JiraTicketDB.delete("jt-d") is True
        assert JiraTicketDB.get_by_id("jt-d") is None

    def test_add_note(self, _isolated_db):
        from db.jira_tickets import JiraTicketDB
        JiraTicketDB.create({"ticket_id": "jt-n", "workspace_id": "", "ticket_key": "NT-1"})
        result = JiraTicketDB.add_note("jt-n", "A note", session_id="s1")
        assert len(result["notes"]) == 1
        assert result["notes"][0]["text"] == "A note"

    def test_empty_workspace_id_allowed(self, _isolated_db):
        """Empty workspace_id must not raise FK constraint error."""
        from db.jira_tickets import JiraTicketDB
        t = JiraTicketDB.create({
            "ticket_id": "jt-fk",
            "workspace_id": "",
            "ticket_key": "FK-1",
        })
        assert t["ticket_id"] == "jt-fk"

    def test_list_by_workspace(self, _isolated_db):
        from db.jira_tickets import JiraTicketDB
        JiraTicketDB.create({"ticket_id": "jt-w1", "workspace_id": "ws-a", "ticket_key": "WS-1"})
        JiraTicketDB.create({"ticket_id": "jt-w2", "workspace_id": "ws-a", "ticket_key": "WS-2"})
        JiraTicketDB.create({"ticket_id": "jt-w3", "workspace_id": "ws-b", "ticket_key": "WS-3"})
        result = JiraTicketDB.list_by_workspace("ws-a")
        assert len(result) == 2


# ── Schema Migration Tests ───────────────────────────────────────────────────

class TestSchemaMigration:

    def test_schema_version_is_6(self, _isolated_db):
        from sqlite_client import get_connection
        conn = get_connection()
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        assert row is not None
        assert int(row[0]) == 6

    def test_jira_tickets_no_fk_constraint(self, _isolated_db):
        """jira_tickets.workspace_id should NOT have FK to workspaces."""
        from sqlite_client import get_connection
        conn = get_connection()
        # Insert with non-existent workspace — should NOT fail
        conn.execute(
            "INSERT INTO jira_tickets (ticket_id, workspace_id, ticket_key, created_at, updated_at) "
            "VALUES ('fk-test', 'nonexistent-ws', 'FK-TEST', '2026-01-01', '2026-01-01')"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM jira_tickets WHERE ticket_id = 'fk-test'").fetchone()
        assert row is not None
