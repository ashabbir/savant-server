"""Tests for knowledge graph route hardening."""

import sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.knowledge_graph import KnowledgeGraphDB


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_kg_node(_isolated_db):
    return KnowledgeGraphDB.create_node({
        "node_id": "node-hard-1",
        "title": "Hardening Node",
        "node_type": "insight",
        "content": "Some content",
    })


@pytest.fixture
def two_nodes(_isolated_db):
    n1 = KnowledgeGraphDB.create_node({"node_id": "node-h-a", "title": "Node A", "node_type": "service"})
    n2 = KnowledgeGraphDB.create_node({"node_id": "node-h-b", "title": "Node B", "node_type": "domain"})
    return n1, n2


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestKGHardening:

    def test_get_node_invalid_id_returns_404(self, client):
        resp = client.get("/api/knowledge/nodes/../../etc/passwd")
        assert resp.status_code == 404

    def test_create_node_invalid_type_returns_400(self, client):
        resp = client.post("/api/knowledge/nodes", json={"title": "Bad Type", "node_type": "hacker"})
        assert resp.status_code == 400

    def test_create_node_empty_title_returns_400(self, client):
        resp = client.post("/api/knowledge/nodes", json={"title": "", "node_type": "insight"})
        assert resp.status_code == 400

    def test_create_edge_invalid_edge_type_coerced_to_relates_to(self, client, two_nodes):
        resp = client.post("/api/knowledge/edges", json={
            "source_id": "node-h-a",
            "target_id": "node-h-b",
            "edge_type": "totally_invalid_type",
        })
        assert resp.status_code == 200
        assert resp.get_json()["edge_type"] == "relates_to"

    def test_recent_bad_limit_uses_default(self, client):
        resp = client.get("/api/knowledge/recent?limit=not_a_number")
        assert resp.status_code == 200

    def test_graph_bad_limit_uses_default(self, client):
        resp = client.get("/api/knowledge/graph?limit=bad")
        assert resp.status_code == 200

    def test_neighbors_bad_depth_uses_default(self, client, sample_kg_node):
        resp = client.get("/api/knowledge/neighbors/node-hard-1?depth=notanint")
        assert resp.status_code == 200

    def test_store_content_too_long_truncated(self, client):
        long_content = "x" * 25000
        resp = client.post("/api/knowledge/store", json={"content": long_content})
        assert resp.status_code == 200
        node = resp.get_json()
        assert len(node.get("content", "")) <= 20001  # truncated to MAX_CONTENT_LEN

    def test_search_empty_query_returns_400(self, client):
        resp = client.post("/api/knowledge/search", json={"query": ""})
        assert resp.status_code == 400

    def test_delete_nonexistent_node_returns_404(self, client):
        resp = client.delete("/api/knowledge/nodes/totally-nonexistent-xyz")
        assert resp.status_code == 404
