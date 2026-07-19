import app as server_app


def test_workspace_context_includes_documented_in_progress_status(monkeypatch, client):
    monkeypatch.setattr(
        server_app.WorkspaceDB,
        "get_by_id",
        lambda workspace_id, user_id="": {
            "workspace_id": workspace_id,
            "name": "Audit workspace",
            "description": "Complexity cleanup",
            "status": "open",
        },
    )
    monkeypatch.setattr(server_app.WorkspaceSessionLinkDB, "list_by_workspace", lambda workspace_id: [])
    monkeypatch.setattr(
        server_app.TaskDB,
        "list_by_workspace",
        lambda workspace_id, limit=200, user_id="": [{
            "task_id": "task-1",
            "title": "Refactor route",
            "description": "Split orchestration from rendering",
            "status": "in_progress",
        }],
    )
    monkeypatch.setattr(server_app.NoteDB, "list_by_workspace", lambda workspace_id, limit=20, user_id="": [])

    response = client.get("/api/workspaces/workspace-1/context")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_task_count"] == 1
    assert "Refactor route" in payload["prompt"]


def test_workspace_list_counts_documented_in_progress_status(monkeypatch, client):
    monkeypatch.setattr(server_app, "_read_workspaces", lambda user_id="": [{"id": "workspace-1", "name": "Audit"}])
    monkeypatch.setattr(
        server_app.TaskDB,
        "list_all",
        lambda user_id="": [{"workspace_id": "workspace-1", "status": "in_progress"}],
    )
    monkeypatch.setattr(server_app, "_read_merge_requests", lambda: [])
    monkeypatch.setattr(server_app.WorkspaceSessionLinkDB, "list_by_workspaces", lambda workspace_ids: {})

    response = client.get("/api/workspaces")

    assert response.status_code == 200
    assert response.get_json()[0]["task_stats"]["in_progress"] == 1


def test_workspace_search_finds_cached_savant_session(monkeypatch, client):
    monkeypatch.setattr(
        server_app,
        "_read_workspaces",
        lambda user_id="": [{"id": "workspace-1", "name": "Search workspace"}],
    )
    monkeypatch.setattr(server_app.TaskDB, "list_all", lambda user_id="": [])
    monkeypatch.setitem(server_app._bg_cache, "savant_sessions", [{
        "session_id": "session-1",
        "summary": "Kubernetes deployment cleanup",
        "project": "savant-server",
        "workspace": "workspace-1",
        "updated_at": "2026-07-19T00:00:00",
    }])

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, *args): pass
        def fetchall(self): return []

    class Connection:
        def cursor(self): return Cursor()

    monkeypatch.setattr(server_app, "get_connection", lambda: Connection())
    monkeypatch.setattr(server_app, "release_connection", lambda connection: None)

    response = client.get("/api/workspaces/search?q=kubernetes")

    assert response.status_code == 200
    sessions = response.get_json()["sessions"]
    assert sessions == [{
        "session_id": "session-1",
        "provider": "savant",
        "summary": "Kubernetes deployment cleanup",
        "project": "savant-server",
        "workspace_id": "workspace-1",
        "workspace_name": "Search workspace",
        "updated_at": "2026-07-19T00:00:00",
    }]


def test_workspace_update_coerces_scalar_fields_without_server_error(monkeypatch, client):
    workspaces = [{"id": "workspace-1", "name": "Before", "status": "open"}]
    monkeypatch.setattr(server_app, "_require_admin", lambda: None)
    monkeypatch.setattr(server_app, "_read_workspaces", lambda user_id="": workspaces)
    monkeypatch.setattr(server_app, "_write_workspaces", lambda rows, user_id="": None)
    monkeypatch.setattr(server_app, "_emit_event", lambda *args, **kwargs: None)

    response = client.put("/api/workspaces/workspace-1", json={"name": 123})

    assert response.status_code == 200
    assert response.get_json()["name"] == "123"


def test_workspace_update_normalizes_status_before_emitting_event(monkeypatch, client):
    workspaces = [{"id": "workspace-1", "name": "Audit", "status": "open"}]
    events = []
    monkeypatch.setattr(server_app, "_require_admin", lambda: None)
    monkeypatch.setattr(server_app, "_read_workspaces", lambda user_id="": workspaces)
    monkeypatch.setattr(server_app, "_write_workspaces", lambda rows, user_id="": None)
    monkeypatch.setattr(server_app, "_emit_event", lambda *args: events.append(args))

    response = client.put("/api/workspaces/workspace-1", json={"status": " closed "})

    assert response.status_code == 200
    assert response.get_json()["status"] == "closed"
    assert response.get_json()["closed_at"]
    assert events[0][0] == "workspace_closed"


def test_workspace_update_rejects_non_object_json(monkeypatch, client):
    monkeypatch.setattr(server_app, "_require_admin", lambda: None)

    response = client.put(
        "/api/workspaces/workspace-1",
        data="[]",
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Request body must be a JSON object"
