"""Tests for knowledge node types, prune_graph, and workspace metadata preservation."""

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

    def test_person_in_valid_node_types(self):
        assert "person" in VALID_NODE_TYPES

    @pytest.mark.parametrize("node_type", ["operation", "organization"])
    def test_operational_node_types_are_valid(self, node_type):
        assert node_type in VALID_NODE_TYPES

    @pytest.mark.parametrize("node_type", ["operation", "organization"])
    def test_create_node_route_accepts_operational_node_types(self, client, node_type):
        resp = client.post("/api/knowledge/nodes", json={
            "title": f"Test {node_type}",
            "node_type": node_type,
        })
        assert resp.status_code == 200
        assert resp.get_json()["node_type"] == node_type

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
        from postgres_client import get_connection, release_connection
        from db.base import _now
        import time
        conn = get_connection()
        try:
            edge_id = f"kge_{int(time.time()*1000)}"
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kg_nodes (node_id, title, node_type, created_at, updated_at) VALUES (%s, %s, %s, %s, %s)",
                    (ghost_node_id, "Ghost Node", "concept", _now(), _now())
                )
                cur.execute(
                    "INSERT INTO kg_edges (edge_id, source_id, target_id, edge_type, created_at) VALUES (%s, %s, %s, %s, %s)",
                    (edge_id, valid_node_id, ghost_node_id, "relates_to", _now())
                )
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE kg_edges DROP CONSTRAINT IF EXISTS kg_edges_target_id_fkey;")
                cur.execute("DELETE FROM kg_nodes WHERE node_id = %s", (ghost_node_id,))
            conn.commit()
            return edge_id
        finally:
            release_connection(conn)

    def _create_edge(self, source_id, target_id, edge_type="relates_to"):
        from postgres_client import get_connection, release_connection
        from db.base import _now
        import time
        conn = get_connection()
        try:
            edge_id = f"kge_{int(time.time()*1000)}"
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kg_edges (edge_id, source_id, target_id, edge_type, created_at) VALUES (%s, %s, %s, %s, %s)",
                    (edge_id, source_id, target_id, edge_type, _now())
                )
            conn.commit()
            return edge_id
        finally:
            release_connection(conn)

    def test_prune_removes_dangling_edges(self, _isolated_db):
        """Edges referencing non-existent nodes should be removed."""
        n1 = KnowledgeGraphDB.create_node({"title": "Node1", "node_type": "insight"})
        self._create_dangling_edge(n1["node_id"])
        result = KnowledgeGraphDB.prune_graph()
        assert result["edges_removed"] == 1
        from postgres_client import get_connection, release_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE kg_edges ADD CONSTRAINT kg_edges_target_id_fkey FOREIGN KEY (target_id) REFERENCES kg_nodes(node_id) ON DELETE CASCADE;")
            conn.commit()
        finally:
            release_connection(conn)

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
        from postgres_client import get_connection, release_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM kg_nodes")
                row = cur.fetchone()
                val = list(row.values())[0] if isinstance(row, dict) else row[0]
            assert val >= 1
        finally:
            release_connection(conn)

    def test_prune_orphan_nodes_flag_true(self, _isolated_db):
        """remove_orphan_nodes=True should remove nodes with no edges."""
        n = KnowledgeGraphDB.create_node({"title": "OrphanUnique", "node_type": "insight"})
        result = KnowledgeGraphDB.prune_graph(remove_orphan_nodes=True)
        assert result["nodes_removed"] >= 1
        from postgres_client import get_connection, release_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM kg_nodes WHERE node_id = %s", (n["node_id"],))
                row = cur.fetchone()
                val = list(row.values())[0] if isinstance(row, dict) else row[0]
            assert val == 0
        finally:
            release_connection(conn)

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
