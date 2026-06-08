"""Workspace-session link table and API contract tests."""

from sqlite_client import get_connection


def _mk_ws(client, name):
    resp = client.post("/api/workspaces", json={"name": name})
    assert resp.status_code == 200
    return resp.get_json()["workspace_id"]


def test_workspace_session_links_table_constraints(_isolated_db):
    conn = get_connection()

    conn.execute(
        "INSERT INTO workspaces (workspace_id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
        ("ws-a", "WS A"),
    )
    conn.commit()

    # Table exists and accepts a valid row.
    conn.execute(
        "INSERT INTO workspace_session_links (workspace_id, provider, session_id, attached_at) VALUES (?, ?, ?, datetime('now'))",
        ("ws-a", "codex", "sess-1"),
    )
    conn.commit()

    # PK(provider, session_id) enforces uniqueness.
    dup_ok = False
    try:
        conn.execute(
            "INSERT INTO workspace_session_links (workspace_id, provider, session_id, attached_at) VALUES (?, ?, ?, datetime('now'))",
            ("ws-a", "codex", "sess-1"),
        )
        conn.commit()
        dup_ok = True
    except Exception:
        pass
    assert dup_ok is False

    # FK(workspace_id) should reject unknown workspace IDs.
    fk_ok = False
    try:
        conn.execute(
            "INSERT INTO workspace_session_links (workspace_id, provider, session_id, attached_at) VALUES (?, ?, ?, datetime('now'))",
            ("ws-missing", "codex", "sess-2"),
        )
        conn.commit()
        fk_ok = True
    except Exception:
        pass
    assert fk_ok is False


def test_workspace_session_links_api_assign_reassign_unassign_and_resolve(client):
    ws1 = _mk_ws(client, "Workspace One")
    ws2 = _mk_ws(client, "Workspace Two")

    # Assign
    assign = client.post(
        f"/api/workspaces/{ws1}/session-links",
        json={"provider": "codex", "session_id": "sess-abc"},
    )
    assert assign.status_code == 200
    body = assign.get_json()
    assert body["workspace_id"] == ws1
    assert body["provider"] == "codex"
    assert body["session_id"] == "sess-abc"

    # List on ws1
    listed = client.get(f"/api/workspaces/{ws1}/session-links")
    assert listed.status_code == 200
    links = listed.get_json()["links"]
    assert len(links) == 1
    assert links[0]["provider"] == "codex"
    assert links[0]["session_id"] == "sess-abc"

    # Resolve
    resolved = client.get("/api/session-links/resolve", query_string={"provider": "codex", "session_id": "sess-abc"})
    assert resolved.status_code == 200
    assert resolved.get_json()["workspace_id"] == ws1

    # Reassign same provider+session to ws2 (upsert)
    reassigned = client.post(
        f"/api/workspaces/{ws2}/session-links",
        json={"provider": "codex", "session_id": "sess-abc"},
    )
    assert reassigned.status_code == 200

    ws1_after = client.get(f"/api/workspaces/{ws1}/session-links").get_json()["links"]
    assert ws1_after == []
    ws2_after = client.get(f"/api/workspaces/{ws2}/session-links").get_json()["links"]
    assert len(ws2_after) == 1

    resolved2 = client.get("/api/session-links/resolve", query_string={"provider": "codex", "session_id": "sess-abc"})
    assert resolved2.status_code == 200
    assert resolved2.get_json()["workspace_id"] == ws2

    # Unassign from ws2
    deleted = client.delete(f"/api/workspaces/{ws2}/session-links/codex/sess-abc")
    assert deleted.status_code == 200
    assert deleted.get_json()["deleted"] is True

    resolved3 = client.get("/api/session-links/resolve", query_string={"provider": "codex", "session_id": "sess-abc"})
    assert resolved3.status_code == 200
    assert resolved3.get_json()["workspace_id"] is None
