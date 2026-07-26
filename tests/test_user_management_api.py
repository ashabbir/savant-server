"""TDD coverage for admin user management backend APIs."""

from db.users import UserDB
from db.workspaces import WorkspaceDB


class TestUserManagementAPI:
    def test_non_admin_cannot_list_users(self, client):
        UserDB.create({
            "user_id": "member1",
            "name": "Member One",
            "email": "member1@example.com",
            "api_key": "sk-member1",
            "role": "user",
        })
        resp = client.get("/api/users", headers={"X-API-Key": "sk-member1"})
        assert resp.status_code == 403

    def test_admin_can_list_users(self, client):
        resp = client.get("/api/users")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert any(u["user_id"] == "ahmed" for u in data)

    def test_admin_can_create_user(self, client):
        resp = client.post(
            "/api/users",
            json={
                "user_id": "new-user",
                "name": "New User",
                "email": "new@example.com",
                "role": "user",
            },
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["user_id"] == "new-user"
        assert body["is_active"] == 1
        assert body["api_key"]

    def test_admin_can_get_user(self, client):
        UserDB.create({
            "user_id": "u-get",
            "name": "U Get",
            "email": "uget@example.com",
            "api_key": "sk-u-get",
            "role": "user",
        })
        resp = client.get("/api/users/u-get")
        assert resp.status_code == 200
        assert resp.get_json()["user_id"] == "u-get"

    def test_admin_can_update_user(self, client):
        UserDB.create({
            "user_id": "u-upd",
            "name": "U Old",
            "email": "old@example.com",
            "api_key": "sk-u-upd",
            "role": "user",
        })
        resp = client.put(
            "/api/users/u-upd",
            json={"name": "U New", "email": "new@example.com", "role": "admin"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["name"] == "U New"
        assert body["role"] == "admin"

    def test_admin_can_update_user_with_boolean_active_status(self, client):
        UserDB.create({
            "user_id": "u-active",
            "name": "Active User",
            "email": "active@example.com",
            "api_key": "sk-u-active",
            "role": "user",
        })
        resp = client.put(
            "/api/users/u-active",
            json={"is_active": False},
        )
        assert resp.status_code == 200
        assert resp.get_json()["is_active"] == 0

    def test_admin_can_deactivate_user(self, client):
        UserDB.create({
            "user_id": "u-deact",
            "name": "Deactivate Me",
            "email": "deact@example.com",
            "api_key": "sk-u-deact",
            "role": "user",
        })
        resp = client.delete("/api/users/u-deact")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["is_active"] == 0

    def test_inactive_user_cannot_authenticate(self, client):
        UserDB.create({
            "user_id": "u-inactive",
            "name": "Inactive",
            "email": "inactive@example.com",
            "api_key": "sk-u-inactive",
            "role": "user",
        })
        UserDB.update("u-inactive", {"is_active": 0})
        resp = client.get("/api/workspaces", headers={"X-API-Key": "sk-u-inactive"})
        assert resp.status_code == 401

    def test_user_workspaces_endpoint(self, client):
        WorkspaceDB.create({
            "workspace_id": "ws-u-1",
            "name": "User One WS",
            "description": "",
            "priority": "medium",
            "status": "open",
            "user_id": "ahmed",
        })
        WorkspaceDB.create({
            "workspace_id": "ws-u-2",
            "name": "User Two WS",
            "description": "",
            "priority": "medium",
            "status": "open",
            "user_id": "lex",
        })
        resp = client.get("/api/users/ahmed/workspaces")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["user_id"] == "ahmed"
        assert any(w["workspace_id"] == "ws-u-1" for w in body["workspaces"])
        assert not any(w["workspace_id"] == "ws-u-2" for w in body["workspaces"])

    def test_generate_api_key_endpoint_rotates_key(self, client):
        UserDB.create({
            "user_id": "u-key",
            "name": "Key User",
            "email": "key@example.com",
            "api_key": "sk-old-key",
            "role": "user",
        })
        resp = client.post("/api/users/u-key/api-key")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["user_id"] == "u-key"
        assert body["api_key"] != "sk-old-key"

    def test_create_user_missing_required_fields(self, client):
        resp = client.post("/api/users", json={"name": "Missing ID"})
        assert resp.status_code == 400
