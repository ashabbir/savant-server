"""Contract tests for the /api/workspaces include_kg query parameter.

The default list response must NOT carry kg_stats (we lifted that off the hot
path to fix freeze-on-startup). Only when the client opts in with
``?include_kg=1`` should the heavier KG enrichment run.
"""

from db.workspace_session_links import WorkspaceSessionLinkDB


def _mk_ws(client, name):
    resp = client.post("/api/workspaces", json={"name": name})
    assert resp.status_code == 200
    return resp.get_json()["workspace_id"]


def test_workspaces_list_omits_kg_stats_by_default(client):
    ws = _mk_ws(client, "Plain")
    WorkspaceSessionLinkDB.upsert(ws, "claude", "s-1")

    resp = client.get("/api/workspaces")
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body, list) and body, "expected non-empty workspace list"
    target = next((w for w in body if w["id"] == ws), None)
    assert target is not None
    assert "kg_stats" not in target, "default list must NOT carry kg_stats"
    # Session-link enrichment should still be present (cheap, useful).
    assert target.get("counts", {}).get("claude") == 1
    assert target["counts"]["total"] == 1


def test_workspaces_list_includes_kg_stats_when_requested(client):
    _mk_ws(client, "WithKG")
    # Every accepted truthy form maps to the same behaviour.
    for value in ("1", "true", "yes", "TRUE"):
        resp = client.get(f"/api/workspaces?include_kg={value}")
        assert resp.status_code == 200
        body = resp.get_json()
        # Every workspace should now expose a kg_stats block with the documented
        # shape, even if the user has no nodes.
        for ws in body:
            assert "kg_stats" in ws
            assert set(ws["kg_stats"].keys()) >= {
                "total_nodes",
                "total_edges",
                "nodes_by_type",
                "staged_count",
            }


def test_workspaces_list_rejects_unknown_truthy_strings(client):
    _mk_ws(client, "Strict")
    # Anything outside the documented allowlist must keep KG off — defends
    # against accidental "include_kg=maybe" enabling the slow path.
    for value in ("", "0", "false", "no", "include", "kg", "anything-else"):
        resp = client.get(f"/api/workspaces?include_kg={value}")
        assert resp.status_code == 200
        for ws in resp.get_json():
            assert "kg_stats" not in ws, f"kg_stats leaked for include_kg={value!r}"
