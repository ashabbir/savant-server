"""Shared fixtures for Savant tests."""

import os
import sys
import tempfile
import pytest

# Add savant/ to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Every test gets its own fresh SQLite database."""
    db_path = str(tmp_path / "test_savant.db")
    monkeypatch.setenv("SAVANT_DB", db_path)

    # Reset the singleton so it reconnects to the test DB
    from sqlite_client import SQLiteClient
    old = SQLiteClient._instance
    SQLiteClient._instance = None

    from sqlite_client import init_sqlite
    init_sqlite()

    # Seed default users so auth works in tests
    from db.users import UserDB
    UserDB.seed_defaults()

    yield db_path

    # Tear down
    from sqlite_client import close_sqlite
    close_sqlite()
    SQLiteClient._instance = old


@pytest.fixture
def client(_isolated_db):
    """Flask test client with default auth (ahmed's API key)."""
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        # Wrap the test client to inject X-API-Key by default
        original_open = c.open
        def _authed_open(*args, **kwargs):
            headers = kwargs.get("headers") or {}
            if isinstance(headers, dict) and "X-API-Key" not in headers:
                headers["X-API-Key"] = "sk-ahmed-savant-001"
                kwargs["headers"] = headers
            return original_open(*args, **kwargs)
        c.open = _authed_open
        yield c


@pytest.fixture
def sample_workspace(_isolated_db):
    """Create a workspace and return its id."""
    from db.workspaces import WorkspaceDB
    ws = WorkspaceDB.create({
        "workspace_id": "ws-test-1",
        "name": "Test Workspace",
        "description": "For testing",
        "priority": "high",
        "user_id": "ahmed",
    })
    return ws["workspace_id"]


@pytest.fixture
def sample_tasks(sample_workspace):
    """Create several tasks across dates and statuses. Returns list of created tasks."""
    from db.tasks import TaskDB
    tasks = []
    configs = [
        {"title": "Task A", "status": "todo",        "date": "2026-03-20", "priority": "high"},
        {"title": "Task B", "status": "in-progress",  "date": "2026-03-20", "priority": "medium"},
        {"title": "Task C", "status": "done",         "date": "2026-03-20", "priority": "low"},
        {"title": "Task D", "status": "todo",         "date": "2026-03-21", "priority": "critical"},
        {"title": "Task E", "status": "blocked",      "date": "2026-03-21", "priority": "medium"},
        {"title": "Task F", "status": "in-progress",  "date": "2026-03-22", "priority": "high"},
    ]
    for i, cfg in enumerate(configs):
        t = TaskDB.create({
            "task_id": f"tid-{i+1}",
            "workspace_id": sample_workspace,
            "user_id": "ahmed",
            **cfg,
        })
        tasks.append(t)
    return tasks
