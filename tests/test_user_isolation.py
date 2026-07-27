"""TDD tests for UserDB — RED phase first, then verify GREEN."""

import pytest
import sys
import os
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestUserDB:
    """Tests for server/db/users.py UserDB class."""

    def test_create_user(self, _isolated_db):
        from db.users import UserDB

        user = UserDB.create({
            "user_id": "test-user-1",
            "name": "Test User",
            "email": "test@example.com",
            "api_key": "sk-test-key-001",
            "role": "user",
        })
        assert user is not None
        assert user["user_id"] == "test-user-1"
        assert user["name"] == "Test User"
        assert user["email"] == "test@example.com"
        assert user["role"] == "user"
        assert user["api_key"] == "sk-test-key-001"

    def test_get_by_id(self, _isolated_db):
        from db.users import UserDB

        UserDB.create({
            "user_id": "u1",
            "name": "Alice",
            "api_key": "sk-alice-001",
        })
        user = UserDB.get_by_id("u1")
        assert user is not None
        assert user["name"] == "Alice"

    def test_get_by_id_not_found(self, _isolated_db):
        from db.users import UserDB

        assert UserDB.get_by_id("nonexistent") is None

    def test_get_by_api_key(self, _isolated_db):
        from db.users import UserDB

        UserDB.create({
            "user_id": "u2",
            "name": "Bob",
            "api_key": "sk-bob-secret",
        })
        user = UserDB.get_by_api_key("sk-bob-secret")
        assert user is not None
        assert user["user_id"] == "u2"
        assert user["name"] == "Bob"

    def test_get_by_api_key_wrong_key(self, _isolated_db):
        from db.users import UserDB

        UserDB.create({
            "user_id": "u3",
            "name": "Charlie",
            "api_key": "sk-charlie-key",
        })
        assert UserDB.get_by_api_key("wrong-key") is None

    def test_list_all(self, _isolated_db):
        from db.users import UserDB

        UserDB.create({"user_id": "a", "name": "A", "api_key": "sk-a"})
        UserDB.create({"user_id": "b", "name": "B", "api_key": "sk-b"})
        users = UserDB.list_all()
        # 2 seeded defaults (ahmed, lex) + 2 created above
        assert len(users) == 4
        ids = {u["user_id"] for u in users}
        assert {"a", "b"}.issubset(ids)

    def test_update(self, _isolated_db):
        from db.users import UserDB

        UserDB.create({"user_id": "u4", "name": "Old", "api_key": "sk-u4"})
        updated = UserDB.update("u4", {"name": "New", "email": "new@test.com"})
        assert updated["name"] == "New"
        assert updated["email"] == "new@test.com"

    def test_delete(self, _isolated_db):
        from db.users import UserDB

        UserDB.create({"user_id": "u5", "name": "Del", "api_key": "sk-u5"})
        assert UserDB.delete("u5") is True
        assert UserDB.get_by_id("u5") is None
        assert UserDB.delete("u5") is False

    def test_seed_defaults(self, _isolated_db):
        from db.users import UserDB

        # conftest already seeds defaults, so calling again should be idempotent
        users = UserDB.seed_defaults()
        assert len(users) == 2
        ids = {u["user_id"] for u in users}
        assert ids == {"ahmed", "lex"}
        # Third call should also return same users (no duplicates)
        users2 = UserDB.seed_defaults()
        assert len(users2) == 2

    def test_seed_ahmed_has_admin_role(self, _isolated_db):
        from db.users import UserDB

        UserDB.seed_defaults()
        ahmed = UserDB.get_by_id("ahmed")
        assert ahmed["role"] == "admin"

    def test_seed_lex_has_admin_role(self, _isolated_db):
        from db.users import UserDB

        UserDB.seed_defaults()
        lex = UserDB.get_by_id("lex")
        assert lex["role"] == "admin"

    def test_api_key_hash_stored(self, _isolated_db):
        """Verify api_key_hash is stored for security."""
        from db.users import UserDB, _hash_key

        UserDB.create({"user_id": "u6", "name": "Hash", "api_key": "sk-hash-test"})
        user = UserDB.get_by_id("u6")
        assert user["api_key_hash"] == _hash_key("sk-hash-test")

    def test_duplicate_api_key_rejected(self, _isolated_db):
        from db.users import UserDB

        UserDB.create({"user_id": "u7", "name": "First", "api_key": "sk-dup"})
        with pytest.raises(Exception):
            UserDB.create({"user_id": "u8", "name": "Second", "api_key": "sk-dup"})


class TestAuthMiddleware:
    """Tests for Flask auth middleware (header/query API key)."""

    def test_health_endpoints_require_allowed_app_but_not_api_key(self, client):
        """Health endpoints remain API-key-free but must identify a Savant app."""
        resp = client.get("/api/db/health", headers={"X-API-Key": ""})
        assert resp.status_code == 200

    def test_system_info_requires_allowed_app_but_not_api_key(self, client):
        """System info remains API-key-free but must identify a Savant app."""
        resp = client.get("/api/system/info", headers={"X-API-Key": ""})
        assert resp.status_code == 200

    def test_all_api_routes_reject_missing_app_name(self, client):
        resp = client.get("/api/users", headers={"X-App-Name": ""})
        assert resp.status_code == 403
        assert resp.get_json() == {"error": "Access denied."}

    def test_all_api_routes_reject_unlisted_app_name(self, client):
        resp = client.get("/api/users", headers={"X-App-Name": "untrusted-client"})
        assert resp.status_code == 403
        assert resp.get_json() == {"error": "Access denied."}

    def test_api_routes_accept_legacy_savant_app_header(self, client):
        resp = client.get(
            "/api/users",
            headers={"X-App-Name": "", "X-Savant-App": "savant-olympus"},
        )
        assert resp.status_code == 200

    def test_api_requires_auth(self, client):
        """API endpoints require an auth key."""
        resp = client.get("/api/workspaces", headers={"X-API-Key": ""})
        assert resp.status_code == 401

    def test_invalid_api_key_rejected(self, client):
        """Invalid (non-empty) API key should be rejected."""
        resp = client.get("/api/workspaces", headers={"X-API-Key": "sk-invalid-key-999"})
        assert resp.status_code == 401
        data = resp.get_json()
        assert "error" in data

    def test_valid_api_key(self, client, _isolated_db):
        """Valid API key should allow access."""
        resp = client.get("/api/workspaces", headers={"X-API-Key": "sk-ahmed-savant-001"})
        assert resp.status_code == 200

    def test_valid_api_key_via_query_param(self, client, _isolated_db):
        """Valid api_key query param should allow access (MCP URL compatibility)."""
        resp = client.get("/api/workspaces?api_key=sk-ahmed-savant-001")
        assert resp.status_code == 200

    def test_invalid_api_key(self, client):
        """Invalid API key should return 401."""
        resp = client.get("/api/workspaces", headers={"X-API-Key": "invalid-key"})
        assert resp.status_code == 401

    def test_user_id_in_context(self, client, _isolated_db):
        """After auth, g.user_id should be set and used for filtering."""

        # Ahmed creates a workspace
        resp = client.post("/api/workspaces",
            json={"name": "Ahmed WS"},
            headers={"X-API-Key": "sk-ahmed-savant-001"})
        assert resp.status_code in (200, 201)
        ws_id = resp.get_json()["workspace_id"]

        # Ahmed can see it
        resp = client.get("/api/workspaces",
            headers={"X-API-Key": "sk-ahmed-savant-001"})
        data = resp.get_json()
        assert any(w["workspace_id"] == ws_id for w in data)

        # Lex cannot see it
        resp = client.get("/api/workspaces",
            headers={"X-API-Key": "sk-lex-savant-001"})
        data = resp.get_json()
        assert not any(w["workspace_id"] == ws_id for w in data)


class TestUserIsolation:
    """Cross-user isolation tests — user A cannot see user B's data."""

    def _seed(self, _isolated_db):
        from db.users import UserDB
        UserDB.seed_defaults()

    def test_workspace_isolation(self, client, _isolated_db):
        self._seed(_isolated_db)
        # Ahmed creates
        resp_a = client.post("/api/workspaces",
            json={"name": "A's workspace"},
            headers={"X-API-Key": "sk-ahmed-savant-001"})
        ws_a_id = resp_a.get_json()["workspace_id"]
        # Lex creates
        resp_l = client.post("/api/workspaces",
            json={"name": "L's workspace"},
            headers={"X-API-Key": "sk-lex-savant-001"})
        ws_l_id = resp_l.get_json()["workspace_id"]
        # Ahmed sees only theirs
        resp = client.get("/api/workspaces", headers={"X-API-Key": "sk-ahmed-savant-001"})
        ws_ids = [w["workspace_id"] for w in resp.get_json()]
        assert ws_a_id in ws_ids
        assert ws_l_id not in ws_ids
        # Lex sees only theirs
        resp = client.get("/api/workspaces", headers={"X-API-Key": "sk-lex-savant-001"})
        ws_ids = [w["workspace_id"] for w in resp.get_json()]
        assert ws_l_id in ws_ids
        assert ws_a_id not in ws_ids

    def test_task_isolation(self, client, _isolated_db):
        self._seed(_isolated_db)
        # Ahmed creates workspace + task
        resp = client.post("/api/workspaces",
            json={"name": "A WS"},
            headers={"X-API-Key": "sk-ahmed-savant-001"})
        ws_id = resp.get_json()["workspace_id"]
        client.post("/api/tasks",
            json={"task_id": "t-a", "workspace_id": ws_id, "title": "Ahmed task"},
            headers={"X-API-Key": "sk-ahmed-savant-001"})
        # Lex can't see Ahmed's task
        resp = client.get("/api/tasks", headers={"X-API-Key": "sk-lex-savant-001"})
        task_ids = [t["task_id"] for t in resp.get_json()]
        assert "t-a" not in task_ids

        # Colosseum discovery and claim are bound to the same API-key owner.
        ready = client.post("/api/tasks/t-a/colosseum-ready", json={
            "config": {"repository": "/tmp/repo", "provider": "codex"},
        }, headers={"X-API-Key": "sk-ahmed-savant-001"})
        assert ready.status_code == 200
        next_for_lex = client.get(f"/api/tasks/colosseum/next?workspace_id={ws_id}", headers={"X-API-Key": "sk-lex-savant-001"})
        assert next_for_lex.get_json()["message"] == "No ready Colosseum task"
        assert client.post("/api/tasks/t-a/claim", headers={"X-API-Key": "sk-lex-savant-001"}).status_code == 409

    def test_reminder_isolation(self, client, _isolated_db):
        self._seed(_isolated_db)
        # Ahmed creates reminder
        client.post("/api/reminders",
            json={"title": "Ahmed rem", "due_date": "2026-12-31T12:00:00Z"},
            headers={"X-API-Key": "sk-ahmed-savant-001"})
        # Lex can't see it
        resp = client.get("/api/reminders", headers={"X-API-Key": "sk-lex-savant-001"})
        assert len(resp.get_json()) == 0

    def test_notification_isolation(self, client, _isolated_db):
        self._seed(_isolated_db)
        # Create notification for ahmed via API
        resp = client.post("/api/notifications",
            json={
                "notification_id": f"notif_test_{uuid.uuid4().hex[:8]}",
                "event_type": "info",
                "message": "Hello",
            },
            headers={"X-API-Key": "sk-ahmed-savant-001"})
        assert resp.status_code in (200, 201)
        # Ahmed sees it
        resp = client.get("/api/notifications", headers={"X-API-Key": "sk-ahmed-savant-001"})
        assert resp.status_code == 200
        assert len(resp.get_json()) > 0
        # Lex doesn't
        resp = client.get("/api/notifications", headers={"X-API-Key": "sk-lex-savant-001"})
        assert resp.status_code == 200
        assert len(resp.get_json()) == 0

    def test_get_by_id_cross_user_blocked(self, client, _isolated_db):
        """User A cannot access user B's specific resource."""
        self._seed(_isolated_db)
        resp = client.post("/api/workspaces",
            json={"name": "Secret"},
            headers={"X-API-Key": "sk-ahmed-savant-001"})
        ws_id = resp.get_json()["workspace_id"]
        # Lex tries to update Ahmed's workspace by ID (no GET /<id>, use PUT)
        resp = client.put(f"/api/workspaces/{ws_id}",
            json={"name": "Hacked"},
            headers={"X-API-Key": "sk-lex-savant-001"})
        assert resp.status_code == 404

    def test_delete_cross_user_blocked(self, client, _isolated_db):
        """User A cannot DELETE user B's resource."""
        self._seed(_isolated_db)
        resp = client.post("/api/workspaces",
            json={"name": "No Delete"},
            headers={"X-API-Key": "sk-ahmed-savant-001"})
        ws_id = resp.get_json()["workspace_id"]
        # Lex tries to delete Ahmed's workspace
        resp = client.delete(f"/api/workspaces/{ws_id}",
            headers={"X-API-Key": "sk-lex-savant-001"})
        assert resp.status_code == 404

    def test_update_cross_user_blocked(self, client, _isolated_db):
        """User A cannot PUT user B's resource."""
        self._seed(_isolated_db)
        resp = client.post("/api/workspaces",
            json={"name": "No Update"},
            headers={"X-API-Key": "sk-ahmed-savant-001"})
        ws_id = resp.get_json()["workspace_id"]
        resp = client.put(f"/api/workspaces/{ws_id}",
            json={"name": "Hacked"},
            headers={"X-API-Key": "sk-lex-savant-001"})
        assert resp.status_code == 404
