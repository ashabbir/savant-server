import pytest
from db.users import UserDB
import sqlite_client

# Setup in-memory DB for tests
@pytest.fixture(autouse=True)
def setup_db():
    # Force use of in-memory DB for testing
    sqlite_client.DB_PATH = ":memory:"
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
    yield
    conn.close()

def test_user_lifecycle():
    # Create
    user = UserDB.create({"user_id": "test_user", "name": "Test User", "email": "test@test.com"})
    assert user["user_id"] == "test_user"
    assert "api_key" in user
    assert user["role"] == "user"
    
    # Get by ID
    fetched = UserDB.get_by_id("test_user")
    assert fetched["name"] == "Test User"
    
    # Get by API Key
    by_key = UserDB.get_by_api_key(user["api_key"])
    assert by_key["user_id"] == "test_user"
    
    # Update
    updated = UserDB.update("test_user", {"name": "Updated User"})
    assert updated["name"] == "Updated User"
    
    # Deactivate
    deactivated = UserDB.deactivate("test_user")
    assert deactivated["is_active"] == 0
    
    # Rotate Key
    old_key = user["api_key"]
    rotated = UserDB.rotate_api_key("test_user")
    assert rotated["api_key"] != old_key
    assert UserDB.get_by_api_key(rotated["api_key"])["user_id"] == "test_user"
    
    # Delete
    deleted = UserDB.delete("test_user")
    assert deleted is True
    assert UserDB.get_by_id("test_user") is None

def test_seed_defaults():
    users = UserDB.seed_defaults()
    assert len(users) == 2
    assert users[0]["user_id"] == "ahmed"
    assert users[1]["user_id"] == "lex"
    
    # Should not re-seed if users exist
    users_again = UserDB.seed_defaults()
    assert len(users_again) == 2
