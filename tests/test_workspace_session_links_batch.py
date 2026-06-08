"""Batch-load contract for WorkspaceSessionLinkDB.list_by_workspaces.

These tests are the safety net for the /api/workspaces N+1 fix — the endpoint
now relies on this batch method, so any regression here turns a 1-query path
back into a 50-query path.
"""

from db.workspace_session_links import WorkspaceSessionLinkDB
from sqlite_client import get_connection


def _mk_ws(client, name):
    resp = client.post("/api/workspaces", json={"name": name})
    assert resp.status_code == 200
    return resp.get_json()["workspace_id"]


def test_list_by_workspaces_returns_empty_dict_for_empty_input():
    # No SQL should be issued and result must be an empty mapping (not None).
    assert WorkspaceSessionLinkDB.list_by_workspaces([]) == {}
    assert WorkspaceSessionLinkDB.list_by_workspaces(None) == {}


def test_list_by_workspaces_groups_links_by_workspace(client):
    ws1 = _mk_ws(client, "WS One")
    ws2 = _mk_ws(client, "WS Two")
    ws3 = _mk_ws(client, "WS Three")  # no links — must still appear with empty list

    WorkspaceSessionLinkDB.upsert(ws1, "claude", "s-1")
    WorkspaceSessionLinkDB.upsert(ws1, "copilot", "s-2")
    WorkspaceSessionLinkDB.upsert(ws2, "claude", "s-3")

    out = WorkspaceSessionLinkDB.list_by_workspaces([ws1, ws2, ws3])

    assert set(out.keys()) == {ws1, ws2, ws3}
    assert len(out[ws1]) == 2
    assert len(out[ws2]) == 1
    assert out[ws3] == []
    # Each link carries the full row shape, not just an id.
    sample = out[ws1][0]
    assert set(sample.keys()) >= {"workspace_id", "provider", "session_id", "attached_at"}


def test_list_by_workspaces_dedupes_and_drops_empty_inputs(client):
    ws = _mk_ws(client, "WS X")
    WorkspaceSessionLinkDB.upsert(ws, "claude", "s-x")

    # Mix of duplicates, empty strings, and None — only one effective lookup.
    out = WorkspaceSessionLinkDB.list_by_workspaces([ws, ws, "", None, ws])
    assert list(out.keys()) == [ws]
    assert len(out[ws]) == 1


def test_list_by_workspaces_caps_oversized_input(monkeypatch, client):
    # Set a tiny cap so we can assert the truncation behaviour without
    # actually creating hundreds of workspaces.
    monkeypatch.setattr(WorkspaceSessionLinkDB, "_LIST_BY_WORKSPACES_MAX", 3)
    ids = [f"nonexistent-{i}" for i in range(50)]
    out = WorkspaceSessionLinkDB.list_by_workspaces(ids)
    assert len(out) == 3
    # First 3 ids by insertion order survive.
    assert list(out.keys()) == ids[:3]


def test_list_by_workspaces_issues_one_sql_query(client):
    """Regression guard: the whole point of this method is one query, not N."""
    ws1 = _mk_ws(client, "Q1")
    ws2 = _mk_ws(client, "Q2")
    WorkspaceSessionLinkDB.upsert(ws1, "claude", "a")
    WorkspaceSessionLinkDB.upsert(ws2, "claude", "b")

    conn = get_connection()
    seen: list[str] = []
    # sqlite3 won't let us replace conn.execute directly (read-only), but
    # set_trace_callback gives us every statement the connection runs.
    conn.set_trace_callback(seen.append)
    try:
        WorkspaceSessionLinkDB.list_by_workspaces([ws1, ws2])
    finally:
        conn.set_trace_callback(None)

    select_sqls = [s for s in seen if "FROM workspace_session_links" in s and s.lstrip().upper().startswith("SELECT")]
    assert len(select_sqls) == 1, f"expected 1 SELECT, got {len(select_sqls)}: {select_sqls}"
