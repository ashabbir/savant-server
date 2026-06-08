"""TDD Red-phase tests for kg_stats in GET /api/workspaces response.

Each workspace returned by the API should include a ``kg_stats`` object:

    {
        "kg_stats": {
            "total_nodes": <int>,
            "total_edges": <int>,
            "nodes_by_type": { "<type>": <count>, ... }
        }
    }

Only *committed* KG nodes (not staged) that reference the workspace in
``metadata.workspaces`` should be counted.
"""

import pytest
from db.knowledge_graph import KnowledgeGraphDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_workspace(client, name="Test WS"):
    """Create a workspace via the API and return its workspace_id."""
    resp = client.post("/api/workspaces", json={"name": name})
    assert resp.status_code == 200
    return resp.get_json()["workspace_id"]


def _create_kg_node(node_id, node_type, title, workspace_id, status="committed"):
    """Insert a KG node linked to *workspace_id*."""
    return KnowledgeGraphDB.create_node({
        "node_id": node_id,
        "node_type": node_type,
        "title": title,
        "content": f"Test content for {title}",
        "metadata": {"workspaces": [workspace_id]},
        "status": status,
    })


def _create_kg_edge(edge_id, source_id, target_id, edge_type="relates_to"):
    """Insert a KG edge between two existing nodes."""
    return KnowledgeGraphDB.create_edge({
        "edge_id": edge_id,
        "source_id": source_id,
        "target_id": target_id,
        "edge_type": edge_type,
    })


def _get_workspace_stats(client, workspace_id):
    """Fetch workspace list and return the kg_stats for *workspace_id*."""
    resp = client.get("/api/workspaces?include_kg=1")
    assert resp.status_code == 200
    workspaces = resp.get_json()
    match = [w for w in workspaces if w["workspace_id"] == workspace_id]
    assert len(match) == 1, f"Expected 1 workspace with id {workspace_id}, got {len(match)}"
    return match[0]["kg_stats"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestKgWorkspaceStats:
    """GET /api/workspaces must include kg_stats for every workspace."""

    def test_workspace_has_kg_stats_key(self, client):
        """Each workspace object must contain a 'kg_stats' key."""
        ws_id = _create_workspace(client)
        resp = client.get("/api/workspaces?include_kg=1")
        assert resp.status_code == 200
        workspaces = resp.get_json()
        ws = next(w for w in workspaces if w["workspace_id"] == ws_id)
        assert "kg_stats" in ws, "Workspace response is missing 'kg_stats' key"

    def test_kg_stats_empty_workspace(self, client):
        """Workspace with no KG nodes → zeroed stats."""
        ws_id = _create_workspace(client)
        stats = _get_workspace_stats(client, ws_id)
        assert stats == {
            "total_nodes": 0,
            "total_edges": 0,
            "nodes_by_type": {},
            "staged_count": 0,
        }

    def test_kg_stats_counts_committed_nodes(self, client):
        """Committed nodes linked to a workspace should be counted."""
        ws_id = _create_workspace(client)
        _create_kg_node("n1", "insight", "Insight A", ws_id)
        _create_kg_node("n2", "insight", "Insight B", ws_id)
        _create_kg_node("n3", "service", "Service X", ws_id)

        stats = _get_workspace_stats(client, ws_id)
        assert stats["total_nodes"] == 3

    def test_kg_stats_ignores_staged_nodes(self, client):
        """Staged (non-committed) nodes must NOT appear in kg_stats."""
        ws_id = _create_workspace(client)
        _create_kg_node("n1", "insight", "Committed", ws_id, status="committed")
        _create_kg_node("n2", "insight", "Staged", ws_id, status="staged")

        stats = _get_workspace_stats(client, ws_id)
        assert stats["total_nodes"] == 1, "Staged nodes should not be counted"

    def test_kg_stats_nodes_by_type_breakdown(self, client):
        """nodes_by_type must map each node_type to its count."""
        ws_id = _create_workspace(client)
        _create_kg_node("n1", "insight", "Insight 1", ws_id)
        _create_kg_node("n2", "insight", "Insight 2", ws_id)
        _create_kg_node("n3", "service", "Service 1", ws_id)
        _create_kg_node("n4", "technology", "Tech 1", ws_id)

        stats = _get_workspace_stats(client, ws_id)
        assert stats["nodes_by_type"] == {
            "insight": 2,
            "service": 1,
            "technology": 1,
        }

    def test_kg_stats_counts_edges(self, client):
        """Edges between committed workspace nodes should be counted."""
        ws_id = _create_workspace(client)
        _create_kg_node("n1", "insight", "Node A", ws_id)
        _create_kg_node("n2", "service", "Node B", ws_id)
        _create_kg_node("n3", "technology", "Node C", ws_id)
        _create_kg_edge("e1", "n1", "n2", "relates_to")
        _create_kg_edge("e2", "n2", "n3", "uses")

        stats = _get_workspace_stats(client, ws_id)
        assert stats["total_edges"] == 2

    def test_kg_stats_only_counts_workspace_nodes(self, client):
        """Nodes belonging to a different workspace must not leak into stats."""
        ws_a = _create_workspace(client, "Workspace A")
        ws_b = _create_workspace(client, "Workspace B")

        # 2 nodes for ws_a, 1 node for ws_b
        _create_kg_node("n1", "insight", "A Insight", ws_a)
        _create_kg_node("n2", "service", "A Service", ws_a)
        _create_kg_node("n3", "insight", "B Insight", ws_b)
        # Edge inside ws_b only
        _create_kg_node("n4", "service", "B Service", ws_b)
        _create_kg_edge("e1", "n3", "n4", "relates_to")

        stats_a = _get_workspace_stats(client, ws_a)
        stats_b = _get_workspace_stats(client, ws_b)

        assert stats_a["total_nodes"] == 2
        assert stats_a["total_edges"] == 0
        assert stats_b["total_nodes"] == 2
        assert stats_b["total_edges"] == 1
