"""Tests for Knowledge Graph staging, purge, and multi-workspace features."""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.knowledge_graph import KnowledgeGraphDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_node(client, title="test-node", node_type="insight", content="test content",
                 metadata=None):
    """Create a node via API. Returns response JSON."""
    payload = {"title": title, "node_type": node_type, "content": content}
    if metadata:
        payload["metadata"] = metadata
    r = client.post("/api/knowledge/nodes", json=payload)
    assert r.status_code == 200, f"create_node failed: {r.data}"
    return r.get_json()


def _create_edge(client, source_id, target_id, edge_type="relates_to"):
    """Create an edge via API. Returns response JSON."""
    r = client.post("/api/knowledge/edges", json={
        "source_id": source_id,
        "target_id": target_id,
        "edge_type": edge_type,
    })
    assert r.status_code == 200, f"create_edge failed: {r.data}"
    return r.get_json()


# ===========================================================================
# Staging Lifecycle
# ===========================================================================

class TestStagingLifecycle:

    def test_new_node_defaults_to_staged(self, client):
        """1. New node defaults to status='staged'."""
        node = _create_node(client, title="staging-default")
        assert node["status"] == "staged"

    def test_staged_node_hidden_from_default_list(self, client):
        """2. Staged node is NOT returned by default list_nodes()."""
        node = _create_node(client, title="hidden-staged")
        nodes = KnowledgeGraphDB.list_nodes()
        ids = [n["node_id"] for n in nodes]
        assert node["node_id"] not in ids

    def test_staged_node_hidden_from_default_graph(self, client):
        """2b. Staged node is NOT returned by default get_full_graph()."""
        node = _create_node(client, title="hidden-graph")
        r = client.get("/api/knowledge/graph")
        assert r.status_code == 200
        graph = r.get_json()
        ids = [n["node_id"] for n in graph["nodes"]]
        assert node["node_id"] not in ids

    def test_staged_node_visible_with_include_staged(self, client):
        """3. Staged node visible when include_staged=True."""
        node = _create_node(client, title="visible-staged")
        nodes = KnowledgeGraphDB.list_nodes(include_staged=True)
        ids = [n["node_id"] for n in nodes]
        assert node["node_id"] in ids

    def test_staged_node_visible_in_graph_with_include_staged(self, client):
        """3b. Staged node visible in graph when include_staged=true."""
        node = _create_node(client, title="visible-graph-staged")
        r = client.get("/api/knowledge/graph?include_staged=true")
        assert r.status_code == 200
        graph = r.get_json()
        ids = [n["node_id"] for n in graph["nodes"]]
        assert node["node_id"] in ids

    def test_commit_nodes_by_node_ids(self, client):
        """4. commit via node_ids (backward compat) changes status to 'committed'."""
        node = _create_node(client, title="to-commit")
        r = client.post("/api/knowledge/nodes/commit", json={
            "node_ids": [node["node_id"]],
        })
        assert r.status_code == 200
        body = r.get_json()
        assert body["committed"] is True
        assert body["count"] == 1

        refreshed = KnowledgeGraphDB.get_node(node["node_id"])
        assert refreshed["status"] == "committed"

    def test_commit_by_workspace_id(self, client):
        """4b. commit via workspace_id commits all staged nodes in that workspace."""
        ws = "ws-commit-all"
        n1 = _create_node(client, title="ws-commit-1", metadata={"workspaces": [ws]})
        n2 = _create_node(client, title="ws-commit-2", metadata={"workspaces": [ws]})
        n3 = _create_node(client, title="ws-commit-3", metadata={"workspaces": [ws]})

        # All three should be staged
        for n in (n1, n2, n3):
            assert KnowledgeGraphDB.get_node(n["node_id"])["status"] == "staged"

        r = client.post("/api/knowledge/nodes/commit", json={"workspace_id": ws})
        assert r.status_code == 200
        body = r.get_json()
        assert body["committed"] is True
        assert body["count"] == 3
        assert body["workspace_id"] == ws
        assert set(body["node_ids"]) == {n1["node_id"], n2["node_id"], n3["node_id"]}

        # All three should now be committed
        for n in (n1, n2, n3):
            assert KnowledgeGraphDB.get_node(n["node_id"])["status"] == "committed"

    def test_commit_by_workspace_id_ignores_other_workspaces(self, client):
        """4c. Workspace commit only affects nodes in that workspace."""
        ws_target = "ws-target"
        ws_other = "ws-other"
        n_target = _create_node(client, title="target-node", metadata={"workspaces": [ws_target]})
        n_other = _create_node(client, title="other-node", metadata={"workspaces": [ws_other]})

        r = client.post("/api/knowledge/nodes/commit", json={"workspace_id": ws_target})
        assert r.status_code == 200
        assert r.get_json()["count"] == 1

        assert KnowledgeGraphDB.get_node(n_target["node_id"])["status"] == "committed"
        assert KnowledgeGraphDB.get_node(n_other["node_id"])["status"] == "staged"

    def test_commit_by_workspace_empty_returns_zero(self, client):
        """4d. Workspace commit with no staged nodes returns count 0."""
        r = client.post("/api/knowledge/nodes/commit", json={"workspace_id": "ws-empty-nothing"})
        assert r.status_code == 200
        body = r.get_json()
        assert body["committed"] is True
        assert body["count"] == 0

    def test_committed_node_visible_in_default_graph(self, client):
        """5. Committed node appears in default (no include_staged) graph."""
        node = _create_node(client, title="committed-visible")
        KnowledgeGraphDB.commit_nodes([node["node_id"]])

        nodes = KnowledgeGraphDB.list_nodes()
        ids = [n["node_id"] for n in nodes]
        assert node["node_id"] in ids

        r = client.get("/api/knowledge/graph")
        graph = r.get_json()
        graph_ids = [n["node_id"] for n in graph["nodes"]]
        assert node["node_id"] in graph_ids

    def test_uncommit_nodes_moves_committed_back_to_staged(self, client):
        """Uncommit should move committed nodes back out of the main graph."""
        node = _create_node(client, title="uncommit-me")
        KnowledgeGraphDB.commit_nodes([node["node_id"]])

        r = client.post("/api/knowledge/nodes/uncommit", json={"node_ids": [node["node_id"]]})
        assert r.status_code == 200
        data = r.get_json()
        assert data["uncommitted"] is True
        assert data["count"] == 1
        assert node["node_id"] in data["node_ids"]

        graph = client.get("/api/knowledge/graph").get_json()
        graph_ids = [n["node_id"] for n in graph["nodes"]]
        assert node["node_id"] not in graph_ids

        staged_graph = client.get("/api/knowledge/graph?include_staged=true").get_json()
        staged_ids = [n["node_id"] for n in staged_graph["nodes"]]
        assert node["node_id"] in staged_ids

    def test_commit_empty_payload_returns_400(self, client):
        """7. Commit with neither workspace_id nor node_ids returns 400."""
        r = client.post("/api/knowledge/nodes/commit", json={})
        assert r.status_code == 400

    def test_commit_nonexistent_ids_returns_zero_count(self, client):
        """8. Commit with non-existent IDs succeeds but count is 0."""
        r = client.post("/api/knowledge/nodes/commit", json={
            "node_ids": ["nonexistent-id-abc"],
        })
        assert r.status_code == 200
        assert r.get_json()["count"] == 0

    def test_search_excludes_staged_by_default(self, client):
        """9. Search excludes staged nodes by default."""
        node = _create_node(client, title="searchable-staged-unique", content="xyzzy42 keyword")
        results = KnowledgeGraphDB.search_nodes("xyzzy42")
        ids = [n["node_id"] for n in results]
        assert node["node_id"] not in ids

    def test_search_includes_staged_when_requested(self, client):
        """9b. Search includes staged nodes with include_staged=True."""
        node = _create_node(client, title="searchable-staged-inc", content="plugh99 keyword")
        results = KnowledgeGraphDB.search_nodes("plugh99", include_staged=True)
        ids = [n["node_id"] for n in results]
        assert node["node_id"] in ids

    def test_search_api_excludes_staged_by_default(self, client):
        """9c. Search API excludes staged by default."""
        node = _create_node(client, title="api-search-staged", content="foobar77 unique")
        r = client.post("/api/knowledge/search", json={"query": "foobar77"})
        assert r.status_code == 200
        ids = [n["node_id"] for n in r.get_json()["result"]]
        assert node["node_id"] not in ids

    def test_search_api_includes_staged_when_requested(self, client):
        """9d. Search API includes staged with include_staged=true."""
        node = _create_node(client, title="api-search-inc", content="bazqux88 unique")
        r = client.post("/api/knowledge/search", json={
            "query": "bazqux88",
            "include_staged": "true",
        })
        assert r.status_code == 200
        ids = [n["node_id"] for n in r.get_json()["result"]]
        assert node["node_id"] in ids


# ===========================================================================
# Purge Workspace
# ===========================================================================

class TestPurgeWorkspace:

    def _setup_purge_scenario(self, client):
        """Create a scenario with exclusive and shared nodes for purge testing.

        Returns dict with node references and workspace IDs.
        """
        ws_a = "ws-purge-a"
        ws_b = "ws-purge-b"

        # Exclusive to ws_a (only in ws_a → should be deleted on purge)
        excl1 = _create_node(client, title="exclusive-1",
                             metadata={"workspaces": [ws_a]})
        excl2 = _create_node(client, title="exclusive-2",
                             metadata={"workspaces": [ws_a]})
        # Commit them so they're accessible
        KnowledgeGraphDB.commit_nodes([excl1["node_id"], excl2["node_id"]])

        # Shared between ws_a and ws_b (should be unlinked, not deleted)
        shared1 = _create_node(client, title="shared-1",
                               metadata={"workspaces": [ws_a, ws_b]})
        KnowledgeGraphDB.commit_nodes([shared1["node_id"]])

        # Belongs only to ws_b (unaffected by purge of ws_a)
        other = _create_node(client, title="other-ws-b",
                             metadata={"workspaces": [ws_b]})
        KnowledgeGraphDB.commit_nodes([other["node_id"]])

        return {
            "ws_a": ws_a, "ws_b": ws_b,
            "excl1": excl1, "excl2": excl2,
            "shared1": shared1, "other": other,
        }

    def test_purge_preview_correct_counts(self, client):
        """10. Purge preview returns correct exclusive vs shared counts."""
        s = self._setup_purge_scenario(client)
        r = client.post("/api/knowledge/purge-workspace-preview", json={
            "workspace_id": s["ws_a"],
        })
        assert r.status_code == 200
        body = r.get_json()
        assert body["workspace_id"] == s["ws_a"]
        assert body["to_delete"] == 2   # excl1, excl2
        assert body["to_unlink"] == 1   # shared1
        assert set(body["delete_node_ids"]) == {
            s["excl1"]["node_id"], s["excl2"]["node_id"],
        }
        assert body["unlink_node_ids"] == [s["shared1"]["node_id"]]

    def test_purge_deletes_exclusive_nodes(self, client):
        """11. Purge deletes nodes exclusive to the workspace."""
        s = self._setup_purge_scenario(client)
        r = client.post("/api/knowledge/purge-workspace", json={
            "workspace_id": s["ws_a"],
        })
        assert r.status_code == 200
        body = r.get_json()
        assert body["purged"] is True
        assert body["deleted_count"] == 2

        # Exclusive nodes should be gone
        assert KnowledgeGraphDB.get_node(s["excl1"]["node_id"]) is None
        assert KnowledgeGraphDB.get_node(s["excl2"]["node_id"]) is None

        # Other workspace's node is untouched
        assert KnowledgeGraphDB.get_node(s["other"]["node_id"]) is not None

    def test_purge_unlinks_shared_nodes(self, client):
        """12. Purge unlinks shared nodes — node remains, workspace removed."""
        s = self._setup_purge_scenario(client)
        client.post("/api/knowledge/purge-workspace", json={
            "workspace_id": s["ws_a"],
        })

        shared = KnowledgeGraphDB.get_node(s["shared1"]["node_id"])
        assert shared is not None, "Shared node should still exist"
        ws_list = (shared.get("metadata") or {}).get("workspaces", [])
        assert s["ws_a"] not in ws_list, "Purged workspace should be removed"
        assert s["ws_b"] in ws_list, "Other workspace should remain"

    def test_purge_cascade_deletes_edges(self, client):
        """13. Purge cascade-deletes edges of deleted exclusive nodes."""
        s = self._setup_purge_scenario(client)
        # Create an edge between exclusive and shared nodes
        edge = _create_edge(client, s["excl1"]["node_id"], s["shared1"]["node_id"])

        # Verify edge exists
        edges_before = KnowledgeGraphDB.list_edges(node_id=s["excl1"]["node_id"])
        assert len(edges_before) > 0

        # Purge
        client.post("/api/knowledge/purge-workspace", json={
            "workspace_id": s["ws_a"],
        })

        # Edge should be gone (node was deleted → cascade)
        edges_after = KnowledgeGraphDB.list_edges(node_id=s["excl1"]["node_id"])
        assert len(edges_after) == 0

    def test_purge_nonexistent_workspace_returns_zero(self, client):
        """14. Purge with non-existent workspace returns 0 counts."""
        r = client.post("/api/knowledge/purge-workspace", json={
            "workspace_id": "ws-does-not-exist",
        })
        assert r.status_code == 200
        body = r.get_json()
        assert body["deleted_count"] == 0
        assert body["unlinked_count"] == 0

    def test_purge_requires_workspace_id(self, client):
        """15. Purge without workspace_id returns 400."""
        r = client.post("/api/knowledge/purge-workspace", json={})
        assert r.status_code == 400
        assert "workspace_id" in r.get_json().get("error", "").lower()

    def test_purge_preview_requires_workspace_id(self, client):
        """15b. Purge preview without workspace_id returns 400."""
        r = client.post("/api/knowledge/purge-workspace-preview", json={})
        assert r.status_code == 400


# ===========================================================================
# Multi-Workspace
# ===========================================================================

class TestMultiWorkspace:

    def test_link_workspace_adds_to_metadata(self, client):
        """16. Link workspace adds workspace_id to metadata.workspaces[]."""
        node = _create_node(client, title="link-test")
        r = client.post("/api/knowledge/link-workspace", json={
            "node_id": node["node_id"],
            "workspace_id": "ws-link-1",
        })
        assert r.status_code == 200
        body = r.get_json()
        assert body["linked"] is True
        assert "ws-link-1" in body["workspaces"]

        # Verify via DB
        refreshed = KnowledgeGraphDB.get_node(node["node_id"])
        ws_list = (refreshed.get("metadata") or {}).get("workspaces", [])
        assert "ws-link-1" in ws_list

    def test_link_workspace_idempotent(self, client):
        """17. Linking the same workspace twice does not create duplicates."""
        node = _create_node(client, title="idempotent-link")
        for _ in range(2):
            client.post("/api/knowledge/link-workspace", json={
                "node_id": node["node_id"],
                "workspace_id": "ws-dup",
            })

        refreshed = KnowledgeGraphDB.get_node(node["node_id"])
        ws_list = (refreshed.get("metadata") or {}).get("workspaces", [])
        assert ws_list.count("ws-dup") == 1

    def test_link_multiple_workspaces(self, client):
        """16b. A node can be linked to multiple workspaces."""
        node = _create_node(client, title="multi-link")
        client.post("/api/knowledge/link-workspace", json={
            "node_id": node["node_id"], "workspace_id": "ws-m1",
        })
        r = client.post("/api/knowledge/link-workspace", json={
            "node_id": node["node_id"], "workspace_id": "ws-m2",
        })
        assert r.status_code == 200
        ws = r.get_json()["workspaces"]
        assert "ws-m1" in ws
        assert "ws-m2" in ws

    def test_unlink_workspace_removes_from_array(self, client):
        """18. Unlink workspace removes it from metadata.workspaces[]."""
        node = _create_node(client, title="unlink-test",
                            metadata={"workspaces": ["ws-u1", "ws-u2"]})
        r = client.post("/api/knowledge/unlink-workspace", json={
            "node_id": node["node_id"],
            "workspace_id": "ws-u1",
        })
        assert r.status_code == 200
        body = r.get_json()
        assert body["unlinked"] is True
        assert "ws-u1" not in body["workspaces"]
        assert "ws-u2" in body["workspaces"]

    def test_unlink_nonexistent_workspace_safe(self, client):
        """19. Unlinking a workspace not in the array does not error."""
        node = _create_node(client, title="safe-unlink",
                            metadata={"workspaces": ["ws-keep"]})
        r = client.post("/api/knowledge/unlink-workspace", json={
            "node_id": node["node_id"],
            "workspace_id": "ws-not-there",
        })
        assert r.status_code == 200
        assert "ws-keep" in r.get_json()["workspaces"]

    def test_node_with_no_workspaces_handles_gracefully(self, client):
        """20. Node with no workspaces metadata handles link/unlink gracefully."""
        node = _create_node(client, title="no-ws-node")
        # Node has no metadata.workspaces — link should still work
        r = client.post("/api/knowledge/link-workspace", json={
            "node_id": node["node_id"],
            "workspace_id": "ws-first",
        })
        assert r.status_code == 200
        assert "ws-first" in r.get_json()["workspaces"]

        # Unlink from a node that has the workspace
        r = client.post("/api/knowledge/unlink-workspace", json={
            "node_id": node["node_id"],
            "workspace_id": "ws-first",
        })
        assert r.status_code == 200
        assert r.get_json()["workspaces"] == []

    def test_link_requires_node_id(self, client):
        """20b. Link without node_id returns 400."""
        r = client.post("/api/knowledge/link-workspace", json={
            "workspace_id": "ws-x",
        })
        assert r.status_code == 400

    def test_link_nonexistent_node_returns_404(self, client):
        """20c. Link to nonexistent node returns 404."""
        r = client.post("/api/knowledge/link-workspace", json={
            "node_id": "fake-node-id",
            "workspace_id": "ws-x",
        })
        assert r.status_code == 404

    def test_unlink_nonexistent_node_returns_404(self, client):
        """20d. Unlink from nonexistent node returns 404."""
        r = client.post("/api/knowledge/unlink-workspace", json={
            "node_id": "fake-node-id",
            "workspace_id": "ws-x",
        })
        assert r.status_code == 404
