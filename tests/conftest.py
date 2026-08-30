"""Shared fixtures for Savant tests."""

import os
import sys
import tempfile
import pytest

# Add savant/ to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault(
    "SAVANT_DATABASE_URL",
    os.environ.get(
        "SAVANT_TEST_DATABASE_URL",
        "postgresql://localhost:5432/savant_test",
    ),
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch, request):
    """Every test gets isolated SQLite state and a clean PostgreSQL test database."""
    if request.node.get_closest_marker("no_db"):
        yield None
        return
    db_path = str(tmp_path / "test_savant.db")
    monkeypatch.setenv("SAVANT_DB", db_path)
    monkeypatch.setattr("routes.preferences._PREFERENCES_FILE", str(tmp_path / "preferences.json"))

    # Reset the singleton so it reconnects to the test DB
    from sqlite_client import SQLiteClient
    old = SQLiteClient._instance
    SQLiteClient._instance = None

    from sqlite_client import init_sqlite
    init_sqlite()

    monkeypatch.setenv("SAVANT_EXTERNAL_PERIODIC_RUNNER", "1")
    from postgres_client import (
        get_connection,
        init_schema,
        release_connection,
        require_test_database,
    )
    init_schema()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            require_test_database(cur)
            cur.execute("TRUNCATE experiences, kg_nodes, kg_edges, kg_maintenance_runs, notes, tasks, task_ended_days, workspaces, notebooks, jira_tickets, jira_notes, merge_requests, mr_notes, jobs, ctx_repos, ctx_files, ctx_chunks, ctx_ast_nodes, ctx_vec_chunks, ctx_repo_sync_logs, ctx_periodic_sync_logs, workspace_session_links, reminders, notifications, code_intelligence_config, users RESTART IDENTITY CASCADE;")
        conn.commit()
    finally:
        release_connection(conn)

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
            headers = kwargs.get("headers")
            if headers is None:
                headers = {}
            elif not isinstance(headers, dict):
                headers = dict(headers)
            headers.setdefault("X-API-Key", "sk-ahmed-savant-001")
            headers.setdefault("X-App-Name", "savant-olympus")
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
