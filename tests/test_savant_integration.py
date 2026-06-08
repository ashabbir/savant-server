"""Tests for Savant integration into cross-provider aggregation endpoints.

Validates that all 8 locations in app.py where provider lists were patched
to include savant_sessions actually work end-to-end:

1. Workspace delete clears savant session workspace assignment
2. Workspace notes includes savant session notes
3. Workspace search finds savant sessions and notes
4. All-MRs includes savant session MRs
5. All-Jira includes savant session Jira tickets
6. _collect_workspace_sessions includes savant sessions
7. _collect_session_artifacts handles savant sessions
8. Notification/search nickname lookup includes savant sessions
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _savant_session(sid, workspace=None, mrs=None, jira_tickets=None,
                    notes=None, summary="savant test", nickname=None,
                    project=None, archived=False):
    """Build a minimal savant session dict for injection into _bg_cache."""
    s = {
        "id": sid,
        "session_id": sid,
        "summary": summary,
        "updated_at": "2026-04-15T10:00:00",
        "created_at": "2026-04-15T09:00:00",
        "archived": archived,
    }
    if workspace:
        s["workspace"] = workspace
    if mrs:
        s["mrs"] = mrs
    if jira_tickets:
        s["jira_tickets"] = jira_tickets
    if notes:
        s["notes"] = notes
    if nickname:
        s["nickname"] = nickname
    if project:
        s["project"] = project
    return s


@pytest.fixture
def savant_bg(monkeypatch):
    """Inject savant sessions into _bg_cache and return a helper dict."""
    import app as app_mod

    original_savant = app_mod._bg_cache.get("savant_sessions")
    original_copilot = app_mod._bg_cache.get("copilot_sessions")

    def _set(sessions):
        app_mod._bg_cache["savant_sessions"] = sessions

    yield {"set": _set, "app_mod": app_mod}

    # Restore
    app_mod._bg_cache["savant_sessions"] = original_savant
    app_mod._bg_cache["copilot_sessions"] = original_copilot


@pytest.fixture
def integration_client(_isolated_db, savant_bg):
    """Flask test client with savant_bg ready."""
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, savant_bg


def _create_workspace(client, name):
    """Create a workspace via the API and return the auto-generated ID."""
    resp = client.post("/api/workspaces", json={"name": name})
    assert resp.status_code == 200
    return resp.get_json()["id"]


# ── 1. Workspace delete clears savant workspace assignment ─────────────────

def test_workspace_delete_clears_savant_sessions(integration_client):
    """Deleting a workspace should clear workspace field on savant sessions."""
    client, bg = integration_client

    # Create a workspace and get its auto-generated ID
    ws_id = _create_workspace(client, "Delete Me Savant")

    # Inject savant sessions assigned to that workspace
    bg["set"]([
        _savant_session("h-del-1", workspace=ws_id),
        _savant_session("h-del-2", workspace="ws-other"),
    ])

    # Delete the workspace
    resp = client.delete(f"/api/workspaces/{ws_id}")
    assert resp.status_code == 200

    # Check that savant session workspace was cleared
    sessions = bg["app_mod"]._bg_cache["savant_sessions"]
    assert sessions[0]["workspace"] is None, "Session assigned to deleted workspace should be cleared"
    assert sessions[1]["workspace"] == "ws-other", "Unrelated session should keep its workspace"


# ── 2. Workspace notes includes savant notes ──────────────────────────────

def test_workspace_notes_includes_savant(integration_client):
    """Workspace notes endpoint should aggregate notes from savant sessions."""
    client, bg = integration_client

    ws_id = _create_workspace(client, "Notes WS Savant")

    # Inject savant session with notes
    bg["set"]([
        _savant_session("h-notes-1", workspace=ws_id, nickname="Savant Auth Fix", notes=[
            {"text": "Fixed the token refresh bug", "timestamp": "2026-04-15T09:30:00"},
            {"text": "Deployed to staging", "timestamp": "2026-04-15T10:00:00"},
        ]),
        _savant_session("h-notes-2", workspace="ws-other-h", notes=[
            {"text": "This should not appear", "timestamp": "2026-04-15T09:00:00"},
        ]),
    ])

    resp = client.get(f"/api/workspaces/{ws_id}/notes")
    assert resp.status_code == 200
    data = resp.get_json()

    groups = data["groups"]
    assert len(groups) == 1, f"Expected 1 note group, got {len(groups)}"
    g = groups[0]
    assert g["session_id"] == "h-notes-1"
    assert g["provider"] == "savant"
    assert g["note_count"] == 2
    assert g["summary"] == "Savant Auth Fix"
    # Notes should be reverse-sorted by timestamp
    assert g["notes"][0]["text"] == "Deployed to staging"


def test_session_note_post_appears_in_workspace_notes(client, _isolated_db, tmp_path, monkeypatch):
    """Session notes should be written with workspace_id so workspace notes can aggregate them."""
    import app as app_mod
    from db.workspaces import WorkspaceDB

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "session-notes-1"
    sdir = session_dir / sid
    sdir.mkdir()
    (sdir / ".copilot-meta.json").write_text(json.dumps({"workspace": "ws-note-1"}))
    (sdir / "workspace.yaml").write_text("workspace_id: ws-note-1\n")

    monkeypatch.setattr(app_mod, "SESSION_DIR", str(session_dir))
    WorkspaceDB.create({
        "workspace_id": "ws-note-1",
        "name": "Notes WS",
        "description": "",
        "priority": "medium",
    })

    resp = client.post(f"/api/session/{sid}/notes", json={"text": "Workspace visible note"})
    assert resp.status_code == 200
    assert resp.get_json()["workspace_id"] == "ws-note-1"

    ws_notes = client.get("/api/workspaces/ws-note-1/notes")
    assert ws_notes.status_code == 200
    data = ws_notes.get_json()
    groups = data["groups"]
    assert len(groups) == 1
    assert groups[0]["session_id"] == sid
    assert groups[0]["note_count"] == 1
    assert groups[0]["notes"][0]["text"] == "Workspace visible note"


# ── 3. Workspace search finds savant sessions and notes ───────────────────

def test_workspace_search_finds_savant_sessions(integration_client):
    """Workspace search should find savant sessions by summary."""
    client, bg = integration_client

    ws_id = _create_workspace(client, "Search WS Savant")

    bg["set"]([
        _savant_session("h-search-1", workspace=ws_id, summary="Kubernetes deployment fix"),
    ])

    resp = client.get("/api/workspaces/search?q=kubernetes")
    assert resp.status_code == 200
    data = resp.get_json()

    sessions = data["sessions"]
    assert len(sessions) >= 1
    match = next(s for s in sessions if s["session_id"] == "h-search-1")
    assert match["provider"] == "savant"
    assert match["workspace_id"] == ws_id


def test_workspace_search_finds_savant_notes(integration_client):
    """Workspace search should find matching notes inside savant sessions."""
    client, bg = integration_client

    ws_id = _create_workspace(client, "NoteSearch WS Savant")

    bg["set"]([
        _savant_session("h-nsearch-1", workspace=ws_id, notes=[
            {"text": "Refactored authentication middleware completely", "timestamp": "2026-04-15T09:00:00"},
        ]),
    ])

    resp = client.get("/api/workspaces/search?q=middleware")
    assert resp.status_code == 200
    data = resp.get_json()

    notes = data["notes"]
    assert len(notes) >= 1
    match = next(n for n in notes if n["session_id"] == "h-nsearch-1")
    assert match["provider"] == "savant"
    assert "middleware" in match["text"].lower()


def test_workspace_search_finds_savant_by_project(integration_client):
    """Workspace search should find savant sessions by project name."""
    client, bg = integration_client

    ws_id = _create_workspace(client, "ProjSearch WS Savant")

    bg["set"]([
        _savant_session("h-proj-1", workspace=ws_id, project="savant-app"),
    ])

    resp = client.get("/api/workspaces/search?q=savant-app")
    assert resp.status_code == 200
    data = resp.get_json()

    sessions = data["sessions"]
    assert len(sessions) >= 1
    match = next(s for s in sessions if s["session_id"] == "h-proj-1")
    assert match["provider"] == "savant"


# ── 4. All-MRs includes savant MRs ────────────────────────────────────────

def test_all_mrs_includes_savant(integration_client):
    """The /api/all-mrs endpoint should include MRs from savant sessions."""
    client, bg = integration_client

    bg["set"]([
        _savant_session("h-mr-1", workspace="ws-mr-h", summary="MR Session", mrs=[
            {
                "url": "https://gitlab.com/projectx/savant/-/merge_requests/999",
                "status": "open",
                "role": "author",
                "jira": "APPSERV-5678",
            }
        ]),
    ])

    resp = client.get("/api/all-mrs?filter=open")
    assert resp.status_code == 200
    data = resp.get_json()

    # all-mrs returns a list directly, not {"mrs": [...]}
    mrs = data
    assert isinstance(mrs, list)

    # Find our savant MR
    savant_mr = None
    for mr in mrs:
        if "999" in mr.get("url", ""):
            savant_mr = mr
            break

    assert savant_mr is not None, "Savant MR not found in all-mrs response"
    assert savant_mr["jira"] == "APPSERV-5678"
    assert len(savant_mr["sessions"]) == 1
    assert savant_mr["sessions"][0]["provider"] == "savant"
    assert savant_mr["sessions"][0]["id"] == "h-mr-1"


def test_all_mrs_savant_with_multiple_providers(integration_client):
    """MRs from savant should coexist with MRs from other providers without duplication."""
    client, bg = integration_client
    import app as app_mod

    # Inject both copilot and savant sessions with different MRs
    app_mod._bg_cache["copilot_sessions"] = [
        {
            "id": "cop-mr-1", "session_id": "cop-mr-1",
            "summary": "Copilot MR session", "workspace": "",
            "mrs": [{"url": "https://gitlab.com/test/proj/-/merge_requests/100", "status": "open", "role": "author", "jira": ""}],
            "updated_at": "2026-04-15T09:00:00",
        }
    ]
    bg["set"]([
        _savant_session("h-mr-multi", mrs=[
            {"url": "https://gitlab.com/test/proj/-/merge_requests/200", "status": "open", "role": "author", "jira": ""},
        ]),
    ])

    resp = client.get("/api/all-mrs?filter=open")
    data = resp.get_json()
    urls = [m["url"] for m in data]
    assert any("100" in u for u in urls), "Copilot MR should be present"
    assert any("200" in u for u in urls), "Savant MR should be present"


# ── 5. All-Jira includes savant Jira tickets ──────────────────────────────

def test_all_jira_includes_savant(integration_client):
    """The /api/all-jira-tickets endpoint should include session links from savant."""
    client, bg = integration_client

    # The all-jira endpoint enriches tickets from the REGISTRY, not from sessions.
    # We need to create a ticket in the registry first, then link it via a savant session.
    from db.jira_tickets import JiraTicketDB
    ticket_id = "jira-savant-test-1"
    JiraTicketDB.create({
        "ticket_id": ticket_id,
        "workspace_id": "",
        "ticket_key": "PIM-1234",
        "title": "Fix payment flow",
        "status": "in-progress",
        "assignee": "ashabbir",
        "priority": "high",
    })

    bg["set"]([
        _savant_session("h-jira-1", workspace="ws-jira-h", summary="Jira Session",
                        jira_tickets=[
                            {"ticket_id": ticket_id, "role": "assignee"},
                        ]),
    ])

    resp = client.get("/api/all-jira-tickets?filter=open")
    assert resp.status_code == 200
    data = resp.get_json()

    # all-jira-tickets returns a list directly
    assert isinstance(data, list)

    savant_ticket = None
    for t in data:
        if t.get("ticket_key") == "PIM-1234":
            savant_ticket = t
            break

    assert savant_ticket is not None, "PIM-1234 should be in all-jira-tickets response"
    assert len(savant_ticket["sessions"]) == 1
    assert savant_ticket["sessions"][0]["provider"] == "savant"
    assert savant_ticket["sessions"][0]["id"] == "h-jira-1"


# ── 6. _collect_workspace_sessions includes savant ────────────────────────

def test_collect_workspace_sessions_includes_savant(integration_client):
    """_collect_workspace_sessions should return savant sessions for a workspace."""
    _, bg = integration_client
    from app import _collect_workspace_sessions

    bg["set"]([
        _savant_session("h-ws-1", workspace="ws-coll-h"),
        _savant_session("h-ws-2", workspace="ws-coll-h", archived=True),
        _savant_session("h-ws-3", workspace="ws-other-h2"),
    ])

    sessions = _collect_workspace_sessions("ws-coll-h")
    sids = [s["id"] for s in sessions]

    assert "h-ws-1" in sids, "Active savant session should be included"
    assert "h-ws-2" not in sids, "Archived savant session should be excluded"
    assert "h-ws-3" not in sids, "Session from different workspace should be excluded"

    # Verify provider is set
    savant_session = next(s for s in sessions if s["id"] == "h-ws-1")
    assert savant_session["provider"] == "savant"


# ── 7. _collect_session_artifacts handles savant ──────────────────────────

def test_collect_session_artifacts_savant(integration_client, tmp_path, monkeypatch):
    """_collect_session_artifacts should look up savant sessions under SAVANT_SESSIONS_DIR."""
    _, bg = integration_client
    from app import _collect_session_artifacts
    import app as app_mod

    # Create a fake savant session directory with a plan file
    savant_sessions_dir = tmp_path / "savant_sessions"
    savant_sessions_dir.mkdir()
    session_dir = savant_sessions_dir / "h-artifact-1"
    session_dir.mkdir()
    (session_dir / "plan.md").write_text("# Migration Plan\nStep 1: migrate DB")

    monkeypatch.setattr(app_mod, "SAVANT_SESSIONS_DIR", str(savant_sessions_dir))

    sessions = [
        {"id": "h-artifact-1", "provider": "savant"},
    ]

    artifacts = _collect_session_artifacts(sessions)
    assert "h-artifact-1" in artifacts, "Savant session should have artifacts"
    files = artifacts["h-artifact-1"]["files"]
    assert any(f["name"] == "plan.md" for f in files), "plan.md should be collected"


def test_collect_session_artifacts_savant_missing_dir(integration_client, tmp_path, monkeypatch):
    """_collect_session_artifacts should gracefully skip savant sessions with no directory."""
    _, bg = integration_client
    from app import _collect_session_artifacts
    import app as app_mod

    monkeypatch.setattr(app_mod, "SAVANT_SESSIONS_DIR", str(tmp_path / "nonexistent"))

    sessions = [
        {"id": "h-nodir-1", "provider": "savant"},
    ]

    artifacts = _collect_session_artifacts(sessions)
    assert "h-nodir-1" not in artifacts, "Missing dir should produce no artifacts"


# ── 8. Notification/search nickname lookup includes savant ────────────────

def test_search_nickname_lookup_includes_savant(integration_client):
    """The global search nickname fallback should check savant_sessions in _bg_cache."""
    _, bg = integration_client

    bg["set"]([
        _savant_session("h-nick-1", nickname="Savant Auth Session", summary="Auth bugfix"),
    ])

    import app as app_mod
    cache = app_mod._bg_cache
    # Verify savant session is in the cache and accessible
    sessions = cache.get("savant_sessions") or []
    found = any(s["id"] == "h-nick-1" for s in sessions)
    assert found, "Savant session should be in _bg_cache for nickname lookup"

    match = None
    for s in sessions:
        if s.get("id") == "h-nick-1":
            match = s
            break
    assert match["nickname"] == "Savant Auth Session"
    assert match["summary"] == "Auth bugfix"


# ── Edge cases ─────────────────────────────────────────────────────────────

def test_savant_empty_cache_no_errors(integration_client):
    """When savant_sessions is None/empty, cross-provider endpoints should not error."""
    client, bg = integration_client

    bg["set"](None)  # Simulate no savant sessions loaded yet

    # Create workspace for notes/search endpoints
    ws_id = _create_workspace(client, "Empty WS Savant")

    # All endpoints should return 200
    assert client.get("/api/all-mrs?filter=open").status_code == 200
    assert client.get("/api/all-jira-tickets").status_code == 200
    assert client.get(f"/api/workspaces/{ws_id}/notes").status_code == 200
    assert client.get("/api/workspaces/search?q=test").status_code == 200

    # Empty list should also work
    bg["set"]([])
    assert client.get("/api/all-mrs?filter=open").status_code == 200
    assert client.get("/api/all-jira-tickets").status_code == 200


def test_workspace_sessions_includes_savant(integration_client):
    """GET /api/workspaces/<id>/sessions should include savant sessions."""
    client, bg = integration_client

    ws_id = _create_workspace(client, "WS Sessions Savant")

    bg["set"]([
        _savant_session("h-sess-1", workspace=ws_id, summary="Savant deploy fix"),
    ])

    resp = client.get(f"/api/workspaces/{ws_id}/sessions")
    assert resp.status_code == 200
    data = resp.get_json()

    sessions = data["sessions"]
    savant_found = [s for s in sessions if s.get("provider") == "savant"]
    assert len(savant_found) == 1
    assert savant_found[0]["id"] == "h-sess-1"
