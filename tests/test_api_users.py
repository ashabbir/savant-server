import pytest
from app import app
from flask import g
from postgres_client import init_schema
from db.users import UserDB

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        with app.app_context():
            init_schema()
            UserDB.seed_defaults()
            UserDB.create({
                "user_id": "admin",
                "name": "Admin",
                "role": "admin",
                "api_key": "admin-key"
            })
            def set_admin():
                g.user_id = "admin"
            yield client, set_admin

def test_api_users_list(client):
    c, set_admin = client
    set_admin()
    
    response = c.get("/api/users", headers={"X-API-Key": "admin-key"})
    assert response.status_code == 200
    assert len(response.json) >= 1
    assert any(u["user_id"] == "admin" for u in response.json)

def test_api_users_create(client):
    c, set_admin = client
    set_admin()
    
    response = c.post("/api/users", 
                      json={"user_id": "newuser", "name": "New User"},
                      headers={"X-API-Key": "admin-key"})
    assert response.status_code == 201
    assert response.json["user_id"] == "newuser"

def test_api_users_admin_required(client):
    c, _ = client
    # Set as non-admin user
    from db.users import UserDB
    UserDB.create({"user_id": "user", "name": "User", "role": "user", "api_key": "user-key"})
    
    # Try to access as user
    response = c.get("/api/users", headers={"X-API-Key": "user-key"})
    assert response.status_code == 403
