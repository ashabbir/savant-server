import pytest
from app import app
import sqlite_client
from flask import g

@pytest.fixture
def client():
    app.config["TESTING"] = True
    # Force use of in-memory DB
    sqlite_client.DB_PATH = ":memory:"
    
    with app.test_client() as client:
        with app.app_context():
            # Initialize schema
            conn = sqlite_client.get_connection()
            conn.execute("DROP TABLE IF EXISTS users")
            conn.execute("""
                CREATE TABLE users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT,
                    email TEXT,
                    api_key TEXT,
                    api_key_hash TEXT,
                    role TEXT,
                    is_active INTEGER,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            conn.commit()
            
            # Seed admin for tests
            from db.users import UserDB
            UserDB.create({
                "user_id": "admin",
                "name": "Admin",
                "role": "admin",
                "api_key": "admin-key"
            })
            
            # Helper to set up admin context
            def set_admin():
                g.user_id = "admin"
                
            yield client, set_admin

def test_api_users_list(client):
    c, set_admin = client
    set_admin()
    
    response = c.get("/api/users", headers={"X-API-Key": "admin-key"})
    assert response.status_code == 200
    assert len(response.json) >= 1
    assert response.json[0]["user_id"] == "admin"

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
