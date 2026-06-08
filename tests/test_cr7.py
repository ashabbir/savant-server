"""Tests for CR-7: issue node type, prune_graph, and workspace metadata preservation."""

import sys
import os
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.knowledge_graph import KnowledgeGraphDB, VALID_NODE_TYPES


# ══════════════════════════════════════════════════════════════════════════════
# T4 — issue node type
# ══════════════════════════════════════════════════════════════════════════════

class TestIssueNodeType:

    def test_issue_in_valid_node_types(self):
        assert "issue" in VALID_NODE_TYPES

    def test_create_issue_node(self, _isolated_db):
        node = KnowledgeGraphDB.create_node({
            "title": "Login bug",
            "node_type": "issue",
            "content": "Users cannot log in with SSO",
        })
        assert node["node_type"] == "issue"
        assert node["title"] == "Login bug"

    def test_create_node_route_accepts_issue(self, client):
        resp = client.post("/api/knowledge/nodes", json={
            "title": "Auth failure",
            "node_type": "issue",
        })
        assert resp.status_code == 200
        assert resp.get_json()["node_type"] == "issue"

    def test_update_node_type_to_issue(self, client, _isolated_db):
        node = KnowledgeGraphDB.create_node({"title": "Bug", "node_type": "insight"})
        resp = client.put(f"/api/knowledge/nodes/{node['node_id']}", json={"node_type": "issue"})
        assert resp.status_code == 200
        assert resp.get_json()["node_type"] == "issue"

    def test_search_route_accepts_issue_filter(self, client, _isolated_db):
        KnowledgeGraphDB.create_node({"title": "Auth bug", "node_type": "issue"})
        resp = client.post("/api/knowledge/search", json={"query": "auth", "node_type": "issue"})
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# T1 — workspace metadata preserved on update
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkspacePreservationOnUpdate:

    def test_update_node_preserves_workspaces(self, _isolated_db):
        """Updating title/content/repo must not drop the workspaces array."""
        node = KnowledgeGraphDB.create_node({
            "node_id": "ws-test-1",
            "title": "Original",
            "node_type": "insight",
            "metadata": {"workspaces": ["ws-abc", "ws-def"], "repo": "icn"},
        })
        assert node["metadata"]["workspaces"] == ["ws-abc", "ws-def"]

        # Update only title and repo — no workspaces key in payload
        updated = KnowledgeGraphDB.update_node("ws-test-1", {
            "title": "Updated Title",
            "metadata": {"repo": "new-repo", "files": ["x.py"]},
        })
        assert updated["title"] == "Updated Title"
        assert updated["metadata"]["repo"] == "new-repo"
        # workspaces must be preserved
        assert updated["metadata"]["workspaces"] == ["ws-abc", "ws-def"]

    def test_update_node_with_explicit_workspaces_overrides(self, _isolated_db):
        """Explicitly providing workspaces in update payload should update them."""
        KnowledgeGraphDB.create_node({
            "node_id": "ws-test-2",
            "title": "Node",
            "node_type": "insight",
            "metadata": {"workspaces": ["ws-old"]},
        })
        updated = KnowledgeGraphDB.update_node("ws-test-2", {
            "metadata": {"workspaces": ["ws-new"]},
        })
        assert updated["metadata"]["workspaces"] == ["ws-new"]

    def test_route_update_preserves_workspaces(self, client, _isolated_db):
        """PUT /api/knowledge/nodes/<id> preserves workspaces even without them in payload."""
        node = KnowledgeGraphDB.create_node({
            "title": "Linked Node",
            "node_type": "service",
            "metadata": {"workspaces": ["ws-xyz"], "repo": "old"},
        })
        nid = node["node_id"]
        resp = client.put(f"/api/knowledge/nodes/{nid}", json={
            "title": "Updated Service",
            "metadata": {"repo": "new-repo"},
        })
        assert resp.status_code == 200
        result = resp.get_json()
        assert result["metadata"]["workspaces"] == ["ws-xyz"]
        assert result["metadata"]["repo"] == "new-repo"


# ══════════════════════════════════════════════════════════════════════════════
# T5 — prune_graph
# ══════════════════════════════════════════════════════════════════════════════

class TestPruneGraph:

    def _create_dangling_edge(self, valid_node_id, ghost_node_id="ghost-node-deleted"):
        """Insert an edge referencing a non-existent node, bypassing FK enforcement."""
        from sqlite_client import get_connection
        from datetime import datetime, timezone
        import time
        conn = get_connection()
        conn.execute("PRAGMA foreign_keys=OFF")
        edge_id = f"kge_{int(time.time()*1000)}"
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO kg_edges (edge_id, source_id, target_id, edge_type, created_at) VALUES (?,?,?,?,?)",
            (edge_id, valid_node_id, ghost_node_id, "relates_to", now)
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        return edge_id

    def _create_edge(self, source_id, target_id, edge_type="relates_to"):
        from sqlite_client import get_connection
        from datetime import datetime, timezone
        import time
        conn = get_connection()
        edge_id = f"kge_{int(time.time()*1000)}"
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO kg_edges (edge_id, source_id, target_id, edge_type, created_at) VALUES (?,?,?,?,?)",
            (edge_id, source_id, target_id, edge_type, now)
        )
        conn.commit()
        return edge_id

    def test_prune_removes_dangling_edges(self, _isolated_db):
        """Edges referencing non-existent nodes should be removed."""
        n1 = KnowledgeGraphDB.create_node({"title": "Node1", "node_type": "insight"})
        # Create a dangling edge (target node doesn't exist) by bypassing FK enforcement
        self._create_dangling_edge(n1["node_id"])

        result = KnowledgeGraphDB.prune_graph()
        assert result["edges_removed"] == 1
        assert result["nodes_removed"] == 0

    def test_prune_no_dangling_edges_returns_zero(self, _isolated_db):
        n1 = KnowledgeGraphDB.create_node({"title": "A", "node_type": "insight"})
        n2 = KnowledgeGraphDB.create_node({"title": "B", "node_type": "service"})
        self._create_edge(n1["node_id"], n2["node_id"])

        result = KnowledgeGraphDB.prune_graph()
        assert result["edges_removed"] == 0
        assert result["nodes_removed"] == 0
    def test_prune_orphan_nodes_flag_false(self, _isolated_db):
        """remove_orphan_nodes=False should leave orphaned nodes."""
        KnowledgeGraphDB.create_node({"title": "Orphan", "node_type": "insight"})
        result = KnowledgeGraphDB.prune_graph(remove_orphan_nodes=False)
        assert result["nodes_removed"] == 0
        from sqlite_client import get_connection
        conn = get_connection()
        count = conn.execute("SELECT COUNT(*) FROM kg_nodes").fetchone()[0]
        assert count == 1

    def test_prune_orphan_nodes_flag_true(self, _isolated_db):
        """remove_orphan_nodes=True should remove nodes with no edges."""
        KnowledgeGraphDB.create_node({"title": "Orphan", "node_type": "insight"})
        result = KnowledgeGraphDB.prune_graph(remove_orphan_nodes=True)
        assert result["nodes_removed"] == 1
        from sqlite_client import get_connection
        conn = get_connection()
        count = conn.execute("SELECT COUNT(*) FROM kg_nodes").fetchone()[0]
        assert count == 0

    def test_prune_connected_node_not_removed(self, _isolated_db):
        """Nodes with edges should not be removed even when flag is True."""
        n1 = KnowledgeGraphDB.create_node({"title": "A", "node_type": "insight"})
        n2 = KnowledgeGraphDB.create_node({"title": "B", "node_type": "service"})
        self._create_edge(n1["node_id"], n2["node_id"])

        result = KnowledgeGraphDB.prune_graph(remove_orphan_nodes=True)
        assert result["nodes_removed"] == 0

    def test_prune_route_exists(self, client):
        resp = client.post("/api/knowledge/prune", json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "edges_removed" in data
        assert "nodes_removed" in data

    def test_prune_route_with_orphan_flag(self, client, _isolated_db):
        KnowledgeGraphDB.create_node({"title": "Orphan", "node_type": "insight"})
        resp = client.post("/api/knowledge/prune", json={"remove_orphan_nodes": True})
        assert resp.status_code == 200
        assert resp.get_json()["nodes_removed"] == 1
