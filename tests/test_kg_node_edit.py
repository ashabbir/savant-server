"""Tests for PUT /api/knowledge/nodes/<node_id> — node editing."""

import sys, os, json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.knowledge_graph import KnowledgeGraphDB

VALID_TYPES = ["client", "domain", "service", "library", "technology", "insight"]


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_kg_node(_isolated_db):
    return KnowledgeGraphDB.create_node({
        "node_id": "node-edit-1",
        "title": "Original Title",
        "node_type": "insight",
        "content": "Original content",
        "metadata": {"repo": "old-repo", "files": ["a.py", "b.py"]},
    })


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestKGNodeUpdate:

    def test_update_title(self, client, sample_kg_node):
        resp = client.put("/api/knowledge/nodes/node-edit-1", json={"title": "New Title"})
        assert resp.status_code == 200
        assert resp.get_json()["title"] == "New Title"

    def test_update_content(self, client, sample_kg_node):
        resp = client.put("/api/knowledge/nodes/node-edit-1", json={"content": "Updated content"})
        assert resp.status_code == 200
        assert resp.get_json()["content"] == "Updated content"

    def test_update_node_type(self, client, sample_kg_node):
        resp = client.put("/api/knowledge/nodes/node-edit-1", json={"node_type": "service"})
        assert resp.status_code == 200
        assert resp.get_json()["node_type"] == "service"

    def test_update_metadata_repo(self, client, sample_kg_node):
        resp = client.put("/api/knowledge/nodes/node-edit-1", json={
            "metadata": {"repo": "new-repo", "files": ["a.py", "b.py"]}
        })
        assert resp.status_code == 200
        assert resp.get_json()["metadata"]["repo"] == "new-repo"

    def test_update_metadata_files(self, client, sample_kg_node):
        resp = client.put("/api/knowledge/nodes/node-edit-1", json={
            "metadata": {"repo": "old-repo", "files": ["x.py", "y.py", "z.py"]}
        })
        assert resp.status_code == 200
        assert resp.get_json()["metadata"]["files"] == ["x.py", "y.py", "z.py"]

    def test_update_returns_updated_node(self, client, sample_kg_node):
        resp = client.put("/api/knowledge/nodes/node-edit-1", json={"title": "Returned Node"})
        assert resp.status_code == 200
        node = resp.get_json()
        assert node["node_id"] == "node-edit-1"
        assert node["title"] == "Returned Node"

    def test_update_nonexistent_node_returns_404(self, client):
        resp = client.put("/api/knowledge/nodes/does-not-exist", json={"title": "Whatever"})
        assert resp.status_code == 404

    def test_update_empty_title_returns_400(self, client, sample_kg_node):
        resp = client.put("/api/knowledge/nodes/node-edit-1", json={"title": ""})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_update_invalid_node_type_returns_400(self, client, sample_kg_node):
        resp = client.put("/api/knowledge/nodes/node-edit-1", json={"node_type": "not-a-type"})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_partial_update_preserves_other_fields(self, client, sample_kg_node):
        resp = client.put("/api/knowledge/nodes/node-edit-1", json={"title": "Changed Title"})
        assert resp.status_code == 200
        node = resp.get_json()
        assert node["content"] == "Original content"
        assert node["node_type"] == "insight"
