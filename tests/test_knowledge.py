"""Comprehensive tests for the Knowledge/Experience layer.

Covers:
  - ExperienceDB CRUD operations (create, get, search, list, delete)
  - Knowledge REST API endpoints (store, search, recent, project_context, list, delete)
  - Edge cases: empty content, missing fields, duplicate IDs, large payloads
  - Schema migration to v3 (experiences table exists)
"""

import sys, os, json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.experiences import ExperienceDB


# ── Helpers ──────────────────────────────────────────────────────────────────

def _create_workspace(client, name="Knowledge Test WS"):
    resp = client.post("/api/workspaces", json={"name": name})
    assert resp.status_code == 200, f"Workspace creation failed: {resp.data}"
    return resp.get_json()["workspace_id"]


def _store_experience(client, content="Test experience", **kwargs):
    payload = {"content": content, **kwargs}
    return client.post("/api/knowledge/store", json=payload)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def ws(client):
    return _create_workspace(client)


@pytest.fixture
def sample_experiences(ws):
    """Create several experiences with varying sources and content."""
    exps = []
    configs = [
        {"content": "Implemented JWT auth with refresh tokens", "source": "session", "repo": "auth-service"},
        {"content": "Fixed race condition in payment processor", "source": "session", "repo": "payment-svc"},
        {"content": "Deployed new monitoring stack with Grafana", "source": "task", "repo": "infra"},
        {"content": "Need to review API rate limiting approach", "source": "note"},
        {"content": "Refactored database connection pooling", "source": "session", "repo": "auth-service", "files": ["db_pool.py", "config.py"]},
        {"content": "Auth service uses bcrypt for password hashing", "source": "note", "repo": "auth-service"},
    ]
    for i, cfg in enumerate(configs):
        exp = ExperienceDB.create({
            "experience_id": f"exp-test-{i+1}",
            "workspace_id": ws,
            **cfg,
        })
        exps.append(exp)
    return exps


# ══════════════════════════════════════════════════════════════════════════════
# ExperienceDB — Data Layer Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestExperienceCreate:
    """Experience creation must return all fields correctly."""

    def test_create_returns_experience_id(self, sample_workspace):
        exp = ExperienceDB.create({
            "experience_id": "exp-new",
            "workspace_id": sample_workspace,
            "content": "New experience",
        })
        assert exp["experience_id"] == "exp-new"

    def test_create_defaults(self, sample_workspace):
        exp = ExperienceDB.create({
            "experience_id": "exp-def",
            "workspace_id": sample_workspace,
            "content": "Default fields test",
        })
        assert exp["source"] == "note"
        assert exp["repo"] == ""
        assert exp["files"] == []
        assert exp["created_at"] is not None
        assert exp["updated_at"] is not None

    def test_create_with_all_fields(self, sample_workspace):
        exp = ExperienceDB.create({
            "experience_id": "exp-full",
            "workspace_id": sample_workspace,
            "content": "Full fields test",
            "source": "session",
            "repo": "my-repo",
            "files": ["file1.py", "file2.js"],
        })
        assert exp["source"] == "session"
        assert exp["repo"] == "my-repo"
        assert exp["files"] == ["file1.py", "file2.js"]

    def test_create_with_empty_workspace(self):
        exp = ExperienceDB.create({
            "experience_id": "exp-no-ws",
            "content": "No workspace",
        })
        assert exp["workspace_id"] == ""

    def test_create_duplicate_id_fails(self, sample_workspace):
        ExperienceDB.create({
            "experience_id": "exp-dup",
            "workspace_id": sample_workspace,
            "content": "First",
        })
        with pytest.raises(Exception):
            ExperienceDB.create({
                "experience_id": "exp-dup",
                "workspace_id": sample_workspace,
                "content": "Duplicate",
            })


class TestExperienceRead:
    """Experiences must be retrievable by ID."""

    def test_get_by_id(self, sample_experiences):
        exp = ExperienceDB.get_by_id("exp-test-1")
        assert exp is not None
        assert exp["content"] == "Implemented JWT auth with refresh tokens"
        assert exp["source"] == "session"

    def test_get_nonexistent(self):
        assert ExperienceDB.get_by_id("nonexistent") is None

    def test_get_returns_files_as_list(self, sample_experiences):
        exp = ExperienceDB.get_by_id("exp-test-5")
        assert isinstance(exp["files"], list)
        assert "db_pool.py" in exp["files"]


class TestExperienceSearch:
    """Text search must match content and respect workspace scoping."""

    def test_search_finds_matching(self, sample_experiences, ws):
        results = ExperienceDB.search("JWT auth")
        assert len(results) >= 1
        assert any("JWT" in r["content"] for r in results)

    def test_search_case_insensitive_like(self, sample_experiences, ws):
        # SQLite LIKE is case-insensitive for ASCII
        results = ExperienceDB.search("jwt auth")
        assert len(results) >= 1

    def test_search_scoped_to_workspace(self, sample_experiences, ws):
        results = ExperienceDB.search("auth", workspace_id=ws)
        assert len(results) >= 1
        assert all(r["workspace_id"] == ws for r in results)

    def test_search_no_match(self, sample_experiences):
        results = ExperienceDB.search("xyznonexistentxyz")
        assert len(results) == 0

    def test_search_respects_limit(self, sample_experiences):
        results = ExperienceDB.search("e", limit=2)
        assert len(results) <= 2

    def test_search_wrong_workspace_returns_empty(self, sample_experiences):
        results = ExperienceDB.search("JWT", workspace_id="ws-nonexistent")
        assert len(results) == 0


class TestExperienceList:
    """List operations must return correct subsets."""

    def test_list_recent_returns_all(self, sample_experiences):
        results = ExperienceDB.list_recent()
        assert len(results) == 6

    def test_list_recent_scoped(self, sample_experiences, ws):
        results = ExperienceDB.list_recent(workspace_id=ws)
        assert len(results) == 6

    def test_list_recent_respects_limit(self, sample_experiences):
        results = ExperienceDB.list_recent(limit=3)
        assert len(results) == 3

    def test_list_by_workspace(self, sample_experiences, ws):
        results = ExperienceDB.list_by_workspace(ws)
        assert len(results) == 6

    def test_list_by_workspace_empty(self, sample_experiences):
        results = ExperienceDB.list_by_workspace("ws-nonexistent")
        assert len(results) == 0

    def test_list_all(self, sample_experiences):
        results = ExperienceDB.list_all()
        assert len(results) == 6

    def test_list_ordered_by_created_desc(self, sample_experiences):
        results = ExperienceDB.list_all()
        for i in range(len(results) - 1):
            assert results[i]["created_at"] >= results[i+1]["created_at"]

    def test_count_by_workspace(self, sample_experiences, ws):
        count = ExperienceDB.count_by_workspace(ws)
        assert count == 6

    def test_count_empty_workspace(self):
        count = ExperienceDB.count_by_workspace("ws-nonexistent")
        assert count == 0


class TestExperienceDelete:
    """Delete must remove the experience and return success/failure."""

    def test_delete_existing(self, sample_experiences):
        assert ExperienceDB.delete("exp-test-1") is True
        assert ExperienceDB.get_by_id("exp-test-1") is None

    def test_delete_nonexistent(self):
        assert ExperienceDB.delete("nonexistent") is False

    def test_delete_reduces_count(self, sample_experiences, ws):
        before = ExperienceDB.count_by_workspace(ws)
        ExperienceDB.delete("exp-test-1")
        after = ExperienceDB.count_by_workspace(ws)
        assert after == before - 1


# ══════════════════════════════════════════════════════════════════════════════
# Knowledge REST API Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestKnowledgeHealth:
    """Health endpoint must return ok."""

    def test_health(self, client):
        resp = client.get("/api/knowledge/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"


class TestKnowledgeStore:
    """POST /api/knowledge/store must create insight nodes."""

    def test_store_basic(self, client, ws):
        resp = _store_experience(client, content="Test store", workspace_id=ws, source="session")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["content"] == "Test store"
        assert data["node_type"] == "insight"
        assert data["metadata"]["source"] == "session"
        assert "node_id" in data

    def test_store_generates_id(self, client):
        resp = _store_experience(client, content="Auto ID")
        data = resp.get_json()
        assert "node_id" in data
        assert data["node_id"].startswith("kgn_")

    def test_store_with_files(self, client, ws):
        resp = client.post("/api/knowledge/store", json={
            "content": "Has files",
            "workspace_id": ws,
            "files": ["a.py", "b.js"],
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["metadata"]["files"] == ["a.py", "b.js"]

    def test_store_with_repo(self, client, ws):
        resp = _store_experience(client, content="Has repo", workspace_id=ws, repo="my-repo")
        data = resp.get_json()
        assert data["metadata"]["repo"] == "my-repo"

    def test_store_empty_content_rejected(self, client):
        resp = _store_experience(client, content="")
        assert resp.status_code == 400

    def test_store_whitespace_only_rejected(self, client):
        resp = _store_experience(client, content="   ")
        assert resp.status_code == 400

    def test_store_defaults_source_to_note(self, client):
        resp = _store_experience(client, content="Default source")
        data = resp.get_json()
        assert data["metadata"]["source"] == "note"


class TestKnowledgeSearch:
    """POST /api/knowledge/search must return matching nodes."""

    def test_search_finds_results(self, client, ws):
        _store_experience(client, content="Implemented OAuth2 flow", workspace_id=ws)
        resp = client.post("/api/knowledge/search", json={"query": "OAuth2", "include_staged": True})
        assert resp.status_code == 200
        data = resp.get_json()
        results = data.get("result", data) if isinstance(data, dict) else data
        assert len(results) >= 1
        assert "OAuth2" in results[0]["content"]

    def test_search_scoped_to_workspace(self, client, ws):
        _store_experience(client, content="Scoped search test", workspace_id=ws)
        resp = client.post("/api/knowledge/search", json={
            "query": "Scoped search",
            "include_staged": True,
        })
        data = resp.get_json()
        results = data.get("result", data) if isinstance(data, dict) else data
        assert len(results) >= 1

    def test_search_empty_query_rejected(self, client):
        resp = client.post("/api/knowledge/search", json={"query": ""})
        assert resp.status_code == 400

    def test_search_respects_limit(self, client, ws):
        for i in range(5):
            _store_experience(client, content=f"Bulk item {i}", workspace_id=ws)
        resp = client.post("/api/knowledge/search", json={"query": "Bulk", "limit": 2})
        data = resp.get_json()
        results = data.get("result", data) if isinstance(data, dict) else data
        assert len(results) <= 2


class TestKnowledgeRecent:
    """GET /api/knowledge/recent must return recent nodes."""

    def test_recent_returns_data(self, client, ws):
        _store_experience(client, content="Recent test 1", workspace_id=ws)
        _store_experience(client, content="Recent test 2", workspace_id=ws)
        resp = client.get("/api/knowledge/recent?include_staged=true")
        assert resp.status_code == 200
        data = resp.get_json()
        results = data.get("result", data) if isinstance(data, dict) else data
        assert len(results) >= 2

    def test_recent_respects_limit(self, client, ws):
        for i in range(5):
            _store_experience(client, content=f"Recent limit {i}", workspace_id=ws)
        resp = client.get("/api/knowledge/recent?limit=2")
        data = resp.get_json()
        results = data.get("result", data) if isinstance(data, dict) else data
        assert len(results) <= 2

    def test_recent_without_workspace(self, client):
        _store_experience(client, content="Global recent")
        resp = client.get("/api/knowledge/recent")
        assert resp.status_code == 200
        data = resp.get_json()
        results = data.get("result", data) if isinstance(data, dict) else data
        assert len(results) >= 1


class TestKnowledgeProjectContext:
    """GET /api/knowledge/project_context must aggregate workspace context."""

    def test_project_context_returns_data(self, client, ws):
        _store_experience(client, content="Built payment API", workspace_id=ws, source="session")
        resp = client.get(f"/api/knowledge/project_context?workspace_id={ws}")
        assert resp.status_code == 200
        data = resp.get_json()
        # Graph-based context has project, insights, stats OR fallback with summary
        assert "project" in data or "summary" in data

    def test_project_context_requires_workspace(self, client):
        resp = client.get("/api/knowledge/project_context")
        assert resp.status_code == 400

    def test_project_context_empty_workspace(self, client):
        resp = client.get("/api/knowledge/project_context?workspace_id=ws-empty-999")
        assert resp.status_code == 200

    def test_project_context_includes_counts(self, client, ws):
        _store_experience(client, content="Context count test", workspace_id=ws)
        resp = client.get(f"/api/knowledge/project_context?workspace_id={ws}")
        data = resp.get_json()
        # Either graph-based stats or legacy counts
        assert "stats" in data or "experience_count" in data


class TestKnowledgeList:
    """GET /api/knowledge/list must return all experiences."""

    def test_list_all(self, client, ws):
        _store_experience(client, content="List test 1", workspace_id=ws)
        _store_experience(client, content="List test 2", workspace_id=ws)
        resp = client.get("/api/knowledge/list?include_staged=true")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 2

    def test_list_scoped_to_workspace(self, client, ws):
        _store_experience(client, content="Scoped list test", workspace_id=ws)
        resp = client.get(f"/api/knowledge/list?workspace_id={ws}")
        data = resp.get_json()
        assert all(ws in (d.get("metadata") or {}).get("workspaces", []) for d in data)

    def test_list_respects_limit(self, client, ws):
        for i in range(5):
            _store_experience(client, content=f"Limit list {i}", workspace_id=ws)
        resp = client.get(f"/api/knowledge/list?limit=3")
        data = resp.get_json()
        assert len(data) <= 3


class TestKnowledgeDelete:
    """DELETE /api/knowledge/<id> must remove nodes."""

    def test_delete_existing(self, client, ws):
        resp = _store_experience(client, content="To delete", workspace_id=ws)
        node_id = resp.get_json()["node_id"]
        del_resp = client.delete(f"/api/knowledge/{node_id}")
        assert del_resp.status_code == 200
        assert del_resp.get_json()["deleted"] is True

    def test_delete_nonexistent(self, client):
        resp = client.delete("/api/knowledge/nonexistent-id")
        assert resp.status_code == 404

    def test_delete_removes_from_list(self, client, ws):
        resp = _store_experience(client, content="Delete from list", workspace_id=ws)
        node_id = resp.get_json()["node_id"]
        client.delete(f"/api/knowledge/{node_id}")
        list_resp = client.get("/api/knowledge/list")
        ids = [e.get("node_id", e.get("experience_id")) for e in list_resp.get_json()]
        assert node_id not in ids


# ══════════════════════════════════════════════════════════════════════════════
# Schema Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestKnowledgeSchema:
    """Schema must include kg_nodes, kg_edges, and experiences tables."""

    def test_experiences_table_exists(self, _isolated_db):
        from sqlite_client import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='experiences'"
        ).fetchone()
        assert row is not None

    def test_experiences_has_correct_columns(self, _isolated_db):
        from sqlite_client import get_connection
        conn = get_connection()
        rows = conn.execute("PRAGMA table_info(experiences)").fetchall()
        columns = {r["name"] for r in rows}
        expected = {"experience_id", "content", "source", "workspace_id", "repo", "files", "created_at", "updated_at"}
        assert expected.issubset(columns)

    def test_kg_nodes_table_exists(self, _isolated_db):
        from sqlite_client import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kg_nodes'"
        ).fetchone()
        assert row is not None

    def test_kg_edges_table_exists(self, _isolated_db):
        from sqlite_client import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kg_edges'"
        ).fetchone()
        assert row is not None

    def test_kg_nodes_has_correct_columns(self, _isolated_db):
        from sqlite_client import get_connection
        conn = get_connection()
        rows = conn.execute("PRAGMA table_info(kg_nodes)").fetchall()
        columns = {r["name"] for r in rows}
        expected = {"node_id", "node_type", "title", "content", "metadata", "created_at", "updated_at"}
        assert expected.issubset(columns)

    def test_kg_edges_has_correct_columns(self, _isolated_db):
        from sqlite_client import get_connection
        conn = get_connection()
        rows = conn.execute("PRAGMA table_info(kg_edges)").fetchall()
        columns = {r["name"] for r in rows}
        expected = {"edge_id", "source_id", "target_id", "edge_type", "weight", "label", "created_at"}
        assert expected.issubset(columns)


# ══════════════════════════════════════════════════════════════════════════════
# Graph API Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestKnowledgeGraphNodes:
    """POST/GET/PUT/DELETE /api/knowledge/nodes CRUD."""

    def test_create_node(self, client):
        resp = client.post("/api/knowledge/nodes", json={
            "node_type": "concept",
            "title": "MCP Protocol",
            "content": "Model Context Protocol for AI agents",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["node_type"] == "concept"
        assert data["title"] == "MCP Protocol"
        assert "node_id" in data

    def test_create_node_requires_title(self, client):
        resp = client.post("/api/knowledge/nodes", json={"node_type": "concept"})
        assert resp.status_code == 400

    def test_get_node_with_edges(self, client):
        n1 = client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "Flask"}).get_json()
        n2 = client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "SQLite"}).get_json()
        client.post("/api/knowledge/edges", json={
            "source_id": n1["node_id"], "target_id": n2["node_id"], "edge_type": "relates_to"
        })
        resp = client.get(f"/api/knowledge/nodes/{n1['node_id']}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["edges"]) >= 1

    def test_update_node(self, client):
        n = client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "Old"}).get_json()
        resp = client.put(f"/api/knowledge/nodes/{n['node_id']}", json={"title": "New Title"})
        assert resp.status_code == 200
        assert resp.get_json()["title"] == "New Title"

    def test_delete_node_cascades_edges(self, client):
        n1 = client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "A"}).get_json()
        n2 = client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "B"}).get_json()
        client.post("/api/knowledge/edges", json={
            "source_id": n1["node_id"], "target_id": n2["node_id"], "edge_type": "relates_to"
        })
        resp = client.delete(f"/api/knowledge/nodes/{n1['node_id']}")
        assert resp.status_code == 200
        # Edge should be gone too
        n2_data = client.get(f"/api/knowledge/nodes/{n2['node_id']}").get_json()
        assert len(n2_data["edges"]) == 0


class TestKnowledgeGraphEdges:
    """POST/DELETE /api/knowledge/edges."""

    def test_create_edge(self, client):
        n1 = client.post("/api/knowledge/nodes", json={"node_type": "insight", "title": "I1"}).get_json()
        n2 = client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "C1"}).get_json()
        resp = client.post("/api/knowledge/edges", json={
            "source_id": n1["node_id"], "target_id": n2["node_id"],
            "edge_type": "relates_to", "label": "uses this tech"
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["edge_type"] == "relates_to"
        assert data["label"] == "uses this tech"

    def test_create_edge_invalid_node(self, client):
        n1 = client.post("/api/knowledge/nodes", json={"node_type": "insight", "title": "I1"}).get_json()
        resp = client.post("/api/knowledge/edges", json={
            "source_id": n1["node_id"], "target_id": "nonexistent", "edge_type": "relates_to"
        })
        assert resp.status_code == 400

    def test_disconnect(self, client):
        n1 = client.post("/api/knowledge/nodes", json={"node_type": "insight", "title": "I1"}).get_json()
        n2 = client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "C1"}).get_json()
        client.post("/api/knowledge/edges", json={
            "source_id": n1["node_id"], "target_id": n2["node_id"], "edge_type": "relates_to"
        })
        resp = client.post("/api/knowledge/edges/disconnect", json={
            "source_id": n1["node_id"], "target_id": n2["node_id"], "edge_type": "relates_to"
        })
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] is True


class TestKnowledgeGraphQueries:
    """GET /api/knowledge/graph and /neighbors."""

    def test_get_full_graph(self, client):
        client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "GraphTest1", "status": "committed"})
        client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "GraphTest2", "status": "committed"})
        resp = client.get("/api/knowledge/graph")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "nodes" in data
        assert "edges" in data

    def test_get_neighbors(self, client):
        n1 = client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "Center", "status": "committed"}).get_json()
        n2 = client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "Neighbor1", "status": "committed"}).get_json()
        n3 = client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "Neighbor2", "status": "committed"}).get_json()
        client.post("/api/knowledge/edges", json={
            "source_id": n1["node_id"], "target_id": n2["node_id"], "edge_type": "relates_to"
        })
        client.post("/api/knowledge/edges", json={
            "source_id": n1["node_id"], "target_id": n3["node_id"], "edge_type": "uses"
        })
        resp = client.get(f"/api/knowledge/neighbors/{n1['node_id']}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["nodes"]) >= 3  # center + 2 neighbors
        assert len(data["edges"]) >= 2

    def test_list_concepts(self, client):
        client.post("/api/knowledge/nodes", json={"node_type": "concept", "title": "ConceptA", "status": "committed"})
        client.post("/api/knowledge/nodes", json={"node_type": "insight", "title": "InsightB", "status": "committed"})
        resp = client.get("/api/knowledge/concepts")
        assert resp.status_code == 200
        data = resp.get_json()
        types = {n["node_type"] for n in data}
        assert types == {"concept"} or len(data) == 0  # only concepts


# ══════════════════════════════════════════════════════════════════════════════
# graph_type Support Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGraphTypeSupport:
    """graph_type parameter in node creation and updates."""

    def test_create_node_with_graph_type(self, client):
        """POST /api/knowledge/nodes with graph_type stores it in metadata."""
        resp = client.post("/api/knowledge/nodes", json={
            "node_type": "concept",
            "title": "Architecture Decision",
            "content": "Use event sourcing for audit trail",
            "graph_type": "technical",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["metadata"]["graph_type"] == "technical"

    def test_store_with_graph_type(self, client, ws):
        """POST /api/knowledge/store with graph_type stores it in metadata."""
        resp = client.post("/api/knowledge/store", json={
            "content": "Fidelity integration uses SAML SSO",
            "workspace_id": ws,
            "graph_type": "business",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["metadata"]["graph_type"] == "business"

    def test_update_node_graph_type(self, client):
        """PUT /api/knowledge/nodes/<id> with graph_type changes metadata.graph_type."""
        n = client.post("/api/knowledge/nodes", json={
            "node_type": "insight",
            "title": "Old graph type",
            "graph_type": "technical",
        }).get_json()
        assert n["metadata"]["graph_type"] == "technical"

        resp = client.put(f"/api/knowledge/nodes/{n['node_id']}", json={
            "graph_type": "operational",
        })
        assert resp.status_code == 200
        updated = resp.get_json()
        assert updated["metadata"]["graph_type"] == "operational"

    def test_create_node_without_graph_type(self, client):
        """Creating a node without graph_type works (backward compat)."""
        resp = client.post("/api/knowledge/nodes", json={
            "node_type": "concept",
            "title": "No graph type node",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        # graph_type should not be present or should be absent from metadata
        assert "graph_type" not in (data.get("metadata") or {})

    def test_store_without_graph_type(self, client):
        """POST /api/knowledge/store without graph_type works (backward compat)."""
        resp = _store_experience(client, content="No graph type experience")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "graph_type" not in (data.get("metadata") or {})

    def test_create_node_with_empty_graph_type(self, client):
        """Empty string graph_type is treated as absent."""
        resp = client.post("/api/knowledge/nodes", json={
            "node_type": "concept",
            "title": "Empty graph type",
            "graph_type": "",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert "graph_type" not in (data.get("metadata") or {})

    def test_store_with_empty_graph_type(self, client):
        """POST /api/knowledge/store with empty graph_type is treated as absent."""
        resp = client.post("/api/knowledge/store", json={
            "content": "Empty graph type store",
            "graph_type": "",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert "graph_type" not in (data.get("metadata") or {})

    def test_graph_type_preserved_on_other_update(self, client):
        """Updating title does not lose existing graph_type."""
        n = client.post("/api/knowledge/nodes", json={
            "node_type": "insight",
            "title": "Keep my graph type",
            "graph_type": "business",
        }).get_json()
        resp = client.put(f"/api/knowledge/nodes/{n['node_id']}", json={
            "title": "Updated title",
        })
        assert resp.status_code == 200
        updated = resp.get_json()
        assert updated["metadata"]["graph_type"] == "business"


# ══════════════════════════════════════════════════════════════════════════════
# update_node Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestUpdateNode:
    """PUT /api/knowledge/nodes/<id> for various field updates."""

    def test_update_title(self, client):
        n = client.post("/api/knowledge/nodes", json={
            "node_type": "concept", "title": "Original Title",
        }).get_json()
        resp = client.put(f"/api/knowledge/nodes/{n['node_id']}", json={
            "title": "Updated Title",
        })
        assert resp.status_code == 200
        assert resp.get_json()["title"] == "Updated Title"

    def test_update_content(self, client):
        n = client.post("/api/knowledge/nodes", json={
            "node_type": "concept", "title": "Content Test",
            "content": "Old content",
        }).get_json()
        resp = client.put(f"/api/knowledge/nodes/{n['node_id']}", json={
            "content": "New content here",
        })
        assert resp.status_code == 200
        assert resp.get_json()["content"] == "New content here"

    def test_update_node_type(self, client):
        n = client.post("/api/knowledge/nodes", json={
            "node_type": "concept", "title": "Type Change",
        }).get_json()
        assert n["node_type"] == "concept"
        resp = client.put(f"/api/knowledge/nodes/{n['node_id']}", json={
            "node_type": "insight",
        })
        assert resp.status_code == 200
        assert resp.get_json()["node_type"] == "insight"

    def test_update_graph_type(self, client):
        n = client.post("/api/knowledge/nodes", json={
            "node_type": "concept", "title": "Graph Type Change",
        }).get_json()
        resp = client.put(f"/api/knowledge/nodes/{n['node_id']}", json={
            "graph_type": "onboarding",
        })
        assert resp.status_code == 200
        assert resp.get_json()["metadata"]["graph_type"] == "onboarding"

    def test_update_multiple_fields(self, client):
        n = client.post("/api/knowledge/nodes", json={
            "node_type": "concept", "title": "Multi Update",
            "content": "Old",
        }).get_json()
        resp = client.put(f"/api/knowledge/nodes/{n['node_id']}", json={
            "title": "New Multi Title",
            "content": "New content",
            "node_type": "insight",
            "graph_type": "technical",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["title"] == "New Multi Title"
        assert data["content"] == "New content"
        assert data["node_type"] == "insight"
        assert data["metadata"]["graph_type"] == "technical"

    def test_update_nonexistent_node(self, client):
        resp = client.put("/api/knowledge/nodes/kgn_nonexistent_999", json={
            "title": "Ghost",
        })
        assert resp.status_code == 404

    def test_update_with_invalid_node_type(self, client):
        n = client.post("/api/knowledge/nodes", json={
            "node_type": "concept", "title": "Invalid Type Test",
        }).get_json()
        resp = client.put(f"/api/knowledge/nodes/{n['node_id']}", json={
            "node_type": "invalid_type",
        })
        assert resp.status_code == 400

    def test_update_empty_title_rejected(self, client):
        n = client.post("/api/knowledge/nodes", json={
            "node_type": "concept", "title": "Blank Title Test",
        }).get_json()
        resp = client.put(f"/api/knowledge/nodes/{n['node_id']}", json={
            "title": "",
        })
        assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# Workspace-Required Staged Workflow Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestStagedWorkflow:
    """Workspace-gated staged creation and commit workflow."""

    def test_store_with_workspace_creates_staged(self, client, ws):
        """Nodes created via /store WITH workspace_id start as staged."""
        resp = _store_experience(client, content="Staged node test", workspace_id=ws)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "staged"

    def test_store_without_workspace_creates_committed(self, client):
        """Nodes created via /store WITHOUT workspace_id are committed (backward compat)."""
        resp = _store_experience(client, content="Committed node test")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "committed"

    def test_commit_workspace_makes_nodes_committed(self, client, ws):
        """commit_workspace transitions all staged nodes in a workspace to committed."""
        # Create several staged nodes
        ids = []
        for i in range(3):
            resp = _store_experience(client, content=f"Staged commit test {i}", workspace_id=ws)
            ids.append(resp.get_json()["node_id"])

        # All should be staged
        for nid in ids:
            node = client.get(f"/api/knowledge/nodes/{nid}").get_json()
            assert node["status"] == "staged"

        # Commit the workspace
        commit_resp = client.post("/api/knowledge/nodes/commit", json={"workspace_id": ws})
        assert commit_resp.status_code == 200
        commit_data = commit_resp.get_json()
        assert commit_data["committed"] is True
        assert commit_data["count"] == 3

        # All should now be committed
        for nid in ids:
            node = client.get(f"/api/knowledge/nodes/{nid}").get_json()
            assert node["status"] == "committed"

    def test_staged_nodes_hidden_from_default_search(self, client, ws):
        """Staged nodes do not appear in search without include_staged."""
        _store_experience(client, content="Hidden staged search test XYZ123", workspace_id=ws)
        resp = client.post("/api/knowledge/search", json={"query": "XYZ123"})
        data = resp.get_json()
        results = data.get("result", data) if isinstance(data, dict) else data
        assert len(results) == 0

    def test_staged_nodes_found_with_include_staged(self, client, ws):
        """Staged nodes appear in search when include_staged=true."""
        _store_experience(client, content="Visible staged search test ABC789", workspace_id=ws)
        resp = client.post("/api/knowledge/search", json={
            "query": "ABC789",
            "include_staged": True,
        })
        data = resp.get_json()
        results = data.get("result", data) if isinstance(data, dict) else data
        assert len(results) >= 1
        assert "ABC789" in results[0]["content"]

    def test_staged_nodes_hidden_from_default_list(self, client, ws):
        """Staged nodes do not appear in /list without include_staged."""
        resp = _store_experience(client, content="Hidden staged list test", workspace_id=ws)
        node_id = resp.get_json()["node_id"]
        list_resp = client.get("/api/knowledge/list")
        ids = [n["node_id"] for n in list_resp.get_json()]
        assert node_id not in ids

    def test_staged_nodes_visible_in_list_with_include_staged(self, client, ws):
        """Staged nodes appear in /list when include_staged=true."""
        resp = _store_experience(client, content="Visible staged list test", workspace_id=ws)
        node_id = resp.get_json()["node_id"]
        list_resp = client.get("/api/knowledge/list?include_staged=true")
        ids = [n["node_id"] for n in list_resp.get_json()]
        assert node_id in ids

    def test_staged_nodes_hidden_from_default_recent(self, client, ws):
        """Staged nodes do not appear in /recent without include_staged."""
        resp = _store_experience(client, content="Hidden staged recent test", workspace_id=ws)
        node_id = resp.get_json()["node_id"]
        recent_resp = client.get("/api/knowledge/recent")
        data = recent_resp.get_json()
        results = data.get("result", data) if isinstance(data, dict) else data
        ids = [n["node_id"] for n in results]
        assert node_id not in ids

    def test_staged_nodes_visible_in_recent_with_include_staged(self, client, ws):
        """Staged nodes appear in /recent when include_staged=true."""
        resp = _store_experience(client, content="Visible staged recent test", workspace_id=ws)
        node_id = resp.get_json()["node_id"]
        recent_resp = client.get("/api/knowledge/recent?include_staged=true")
        data = recent_resp.get_json()
        results = data.get("result", data) if isinstance(data, dict) else data
        ids = [n["node_id"] for n in results]
        assert node_id in ids

    def test_commit_workspace_with_no_staged_nodes(self, client, ws):
        """Committing a workspace with no staged nodes returns count=0, no error."""
        resp = client.post("/api/knowledge/nodes/commit", json={"workspace_id": ws})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["committed"] is True
        assert data["count"] == 0

    def test_commit_specific_node_ids(self, client, ws):
        """Committing specific node IDs via node_ids array works."""
        r1 = _store_experience(client, content="Specific commit 1", workspace_id=ws)
        r2 = _store_experience(client, content="Specific commit 2", workspace_id=ws)
        nid1 = r1.get_json()["node_id"]
        nid2 = r2.get_json()["node_id"]

        resp = client.post("/api/knowledge/nodes/commit", json={"node_ids": [nid1]})
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 1

        # nid1 committed, nid2 still staged
        assert client.get(f"/api/knowledge/nodes/{nid1}").get_json()["status"] == "committed"
        assert client.get(f"/api/knowledge/nodes/{nid2}").get_json()["status"] == "staged"

    def test_staged_node_has_workspace_in_metadata(self, client, ws):
        """Staged nodes have workspace_id in metadata.workspaces."""
        resp = _store_experience(client, content="WS metadata test", workspace_id=ws)
        data = resp.get_json()
        workspaces = (data.get("metadata") or {}).get("workspaces", [])
        assert ws in workspaces

    def test_commit_requires_workspace_or_node_ids(self, client):
        """POST /api/knowledge/nodes/commit with neither workspace_id nor node_ids returns 400."""
        resp = client.post("/api/knowledge/nodes/commit", json={})
        assert resp.status_code == 400

    def test_staged_nodes_hidden_from_default_graph(self, client, ws):
        """Staged nodes do not appear in /graph without include_staged."""
        resp = _store_experience(client, content="Hidden staged graph test", workspace_id=ws)
        node_id = resp.get_json()["node_id"]
        graph_resp = client.get("/api/knowledge/graph")
        assert graph_resp.status_code == 200
        ids = [n["node_id"] for n in graph_resp.get_json()["nodes"]]
        assert node_id not in ids

    def test_staged_nodes_visible_in_graph_with_include_staged(self, client, ws):
        """Staged nodes appear in /graph when include_staged=true."""
        resp = _store_experience(client, content="Visible staged graph test", workspace_id=ws)
        node_id = resp.get_json()["node_id"]
        graph_resp = client.get("/api/knowledge/graph?include_staged=true")
        assert graph_resp.status_code == 200
        ids = [n["node_id"] for n in graph_resp.get_json()["nodes"]]
        assert node_id in ids


# ══════════════════════════════════════════════════════════════════════════════
# Combined graph_type + Staged Workflow Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGraphTypeWithStagedWorkflow:
    """Verify graph_type works correctly within the staged workflow."""

    def test_store_with_graph_type_and_workspace(self, client, ws):
        """Store with both graph_type and workspace_id: staged + graph_type set."""
        resp = client.post("/api/knowledge/store", json={
            "content": "Combined test",
            "workspace_id": ws,
            "graph_type": "technical",
            "node_type": "insight",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "staged"
        assert data["metadata"]["graph_type"] == "technical"
        assert ws in data["metadata"].get("workspaces", [])

    def test_graph_type_survives_commit(self, client, ws):
        """graph_type is preserved after committing a staged node."""
        resp = client.post("/api/knowledge/store", json={
            "content": "Persist graph type through commit",
            "workspace_id": ws,
            "graph_type": "operational",
        })
        node_id = resp.get_json()["node_id"]

        # Commit
        client.post("/api/knowledge/nodes/commit", json={"workspace_id": ws})

        # Verify graph_type still there
        node = client.get(f"/api/knowledge/nodes/{node_id}").get_json()
        assert node["status"] == "committed"
        assert node["metadata"]["graph_type"] == "operational"

    def test_update_graph_type_on_staged_node(self, client, ws):
        """Can update graph_type on a staged node before committing."""
        resp = client.post("/api/knowledge/store", json={
            "content": "Update before commit",
            "workspace_id": ws,
            "graph_type": "business",
        })
        node_id = resp.get_json()["node_id"]

        update_resp = client.put(f"/api/knowledge/nodes/{node_id}", json={
            "graph_type": "technical",
        })
        assert update_resp.status_code == 200
        assert update_resp.get_json()["metadata"]["graph_type"] == "technical"

    def test_store_with_node_type_override(self, client, ws):
        """Store with custom node_type (not default insight)."""
        resp = client.post("/api/knowledge/store", json={
            "content": "This is a service entry",
            "workspace_id": ws,
            "node_type": "service",
            "graph_type": "technical",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["node_type"] == "service"
        assert data["metadata"]["graph_type"] == "technical"
