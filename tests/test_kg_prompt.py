"""Tests for POST /api/knowledge/prompt — generate AI prompt from KG nodes."""

import sys, os, json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.knowledge_graph import KnowledgeGraphDB


# ── Helpers ──────────────────────────────────────────────────────────────────

def _create_node(title, node_type="insight", content="", node_id=None):
    data = {"title": title, "node_type": node_type, "content": content}
    if node_id:
        data["node_id"] = node_id
    return KnowledgeGraphDB.create_node(data)


def _create_edge(source_id, target_id, edge_type="relates_to"):
    return KnowledgeGraphDB.create_edge({
        "source_id": source_id,
        "target_id": target_id,
        "edge_type": edge_type,
    })


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_kg_nodes(_isolated_db):
    n1 = _create_node("Auth Service", "service", "Handles JWT authentication", "node-auth")
    n2 = _create_node("Payment Domain", "domain", "Core payment logic", "node-pay")
    n3 = _create_node("React Library", "library", "Frontend UI framework", "node-react")
    _create_edge("node-auth", "node-pay", "integrates_with")
    _create_edge("node-react", "node-auth", "uses")
    return [n1, n2, n3]


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestKGPromptEndpoint:

    def test_returns_prompt_string(self, client, sample_kg_nodes):
        resp = client.post("/api/knowledge/prompt", json={"node_ids": ["node-auth"]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "prompt" in data
        assert isinstance(data["prompt"], str)
        assert len(data["prompt"]) > 0

    def test_prompt_contains_node_titles(self, client, sample_kg_nodes):
        resp = client.post("/api/knowledge/prompt", json={"node_ids": ["node-auth", "node-pay"]})
        assert resp.status_code == 200
        prompt = resp.get_json()["prompt"]
        assert "Auth Service" in prompt
        assert "Payment Domain" in prompt

    def test_prompt_contains_question_when_provided(self, client, sample_kg_nodes):
        question = "How does authentication work?"
        resp = client.post("/api/knowledge/prompt", json={
            "node_ids": ["node-auth"],
            "question": question
        })
        assert resp.status_code == 200
        prompt = resp.get_json()["prompt"]
        assert question in prompt

    def test_prompt_contains_connections(self, client, sample_kg_nodes):
        resp = client.post("/api/knowledge/prompt", json={"node_ids": ["node-auth"]})
        assert resp.status_code == 200
        prompt = resp.get_json()["prompt"]
        # node-auth connects to node-pay (integrates_with) and node-react (uses)
        assert "integrates_with" in prompt or "Payment Domain" in prompt

    def test_rejects_empty_node_ids(self, client):
        resp = client.post("/api/knowledge/prompt", json={"node_ids": []})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_rejects_more_than_30_nodes(self, client, _isolated_db):
        ids = []
        for i in range(31):
            n = _create_node(f"Node {i}", node_id=f"node-extra-{i}")
            ids.append(n["node_id"])
        resp = client.post("/api/knowledge/prompt", json={"node_ids": ids})
        assert resp.status_code == 400
        data = resp.get_json()
        assert "30" in str(data.get("error", ""))

    def test_returns_node_count(self, client, sample_kg_nodes):
        resp = client.post("/api/knowledge/prompt", json={"node_ids": ["node-auth", "node-pay"]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["node_count"] == 2

    def test_node_ids_not_found_skipped_gracefully(self, client, sample_kg_nodes):
        resp = client.post("/api/knowledge/prompt", json={
            "node_ids": ["node-auth", "nonexistent-node-xyz"]
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["node_count"] == 1  # only the found one counts

    def test_prompt_has_correct_structure(self, client, sample_kg_nodes):
        resp = client.post("/api/knowledge/prompt", json={"node_ids": ["node-auth"]})
        assert resp.status_code == 200
        prompt = resp.get_json()["prompt"]
        assert "KNOWLEDGE CONTEXT" in prompt
        assert "Total:" in prompt
        assert "expert software engineer" in prompt.lower() or "expert" in prompt.lower()

    def test_question_is_optional(self, client, sample_kg_nodes):
        resp = client.post("/api/knowledge/prompt", json={"node_ids": ["node-auth"]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "prompt" in data
        assert data["node_count"] >= 1
