from datetime import datetime, timezone

import pytest
from flask import Flask

pytestmark = pytest.mark.no_db


def _symbol(name="run", symbol_id="codegraph:1"):
    from code_intelligence.contracts import CodeSymbol, SymbolLocation
    return CodeSymbol(id=symbol_id, name=name, kind="function", language="python",
        location=SymbolLocation(repo_id="3", file_path="a.py", start_line=1, end_line=2), metadata={})


@pytest.fixture
def codegraph_client(monkeypatch, tmp_path):
    import graphify.routes as routes
    from code_intelligence.contracts import ExploreResult, Freshness, ProviderHealth, Subgraph
    from db.code_intelligence import CodeIntelligenceConfigDB

    monkeypatch.setattr(routes.ContextDB, "get_repo_by_identifier",
                        lambda value: {"id": 3, "name": "repo", "path": str(tmp_path)} if str(value) in {"3", "repo"} else None)
    monkeypatch.setattr(CodeIntelligenceConfigDB, "get", lambda _repo: {"provider": "codegraph"})

    class Service:
        def health(self, repo_id, root):
            assert repo_id == "3"
            return ProviderHealth(provider="codegraph", indexed=True, freshness=Freshness.FRESH,
                indexed_at=datetime.now(timezone.utc), graph_version="g1", files=2, nodes=4, edges=3)
        def explore(self, repo_id, root, query, max_files, include_source=False):
            return ExploreResult(symbols=[_symbol("z", "codegraph:2"), _symbol("a", "codegraph:1")],
                edges=[], provider="codegraph")
        def subgraph(self, repo_id, root, **kwargs):
            assert kwargs["roots"] == [{"id": "codegraph:1"}]
            return Subgraph(symbols=[_symbol()], edges=[])
    monkeypatch.setattr("code_intelligence.runtime.build_service", lambda: Service())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy GraphifyDB must not be called")
    monkeypatch.setattr(routes.GraphifyDB, "get_stats", forbidden)
    monkeypatch.setattr(routes.GraphifyDB, "get_main_entities", forbidden)
    monkeypatch.setattr(routes.GraphifyDB, "get_neighbors", forbidden)
    monkeypatch.setattr(routes.GraphifyDB, "search", forbidden)
    app = Flask(__name__)
    app.register_blueprint(routes.graphify_bp)
    return app.test_client()


def test_codegraph_stats_uses_provider_health(codegraph_client):
    payload = codegraph_client.get("/api/graphify/stats?repo_id=3&workspace_id=wrong").get_json()
    assert payload["provider"] == "codegraph"
    assert payload["freshness"] == "fresh"
    assert payload["graph_version"] == "g1"
    assert payload["total"] == 4


def test_codegraph_main_entities_is_deterministic_and_does_not_use_graphify(codegraph_client):
    response = codegraph_client.get("/api/graphify/main-entities?repo_id=3&limit=10")
    assert response.status_code == 200
    assert [node["title"] for node in response.get_json()["nodes"]] == ["a", "z"]


def test_codegraph_neighbors_enforces_namespace_and_projects_graph(codegraph_client):
    refused = codegraph_client.get("/api/graphify/neighbors?repo_id=3&node_id=legacy:1")
    assert refused.status_code == 404
    response = codegraph_client.get("/api/graphify/neighbors?repo_id=3&node_id=codegraph:1")
    assert response.status_code == 200
    assert response.get_json()["nodes"][0]["node_id"] == "codegraph:1"


def test_codegraph_search_resolves_numeric_repo_id(codegraph_client):
    response = codegraph_client.post("/api/graphify/search", json={
        "repo_id": "3", "query": "run", "limit": 10,
    })
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["provider"] == "codegraph"
    assert [node["title"] for node in payload["nodes"]] == ["z", "a"]


def test_structural_worker_resolves_numeric_repo_id_to_canonical_name(monkeypatch, tmp_path):
    from context import job_worker
    from context.db import ContextDB
    monkeypatch.setattr(ContextDB, "get_repo_by_identifier",
                        lambda value: {"id": 3, "name": "canonical", "path": str(tmp_path)} if str(value) == "3" else None)
    root, name = job_worker._resolve_repo("3")
    assert root == tmp_path
    assert name == "canonical"


def test_native_sync_queues_numeric_repo_id(monkeypatch, tmp_path):
    from code_intelligence import routes
    monkeypatch.setattr(routes.ContextDB, "get_repo_by_identifier",
                        lambda value: {"id": 3, "name": "canonical", "path": str(tmp_path)} if str(value) == "3" else None)
    monkeypatch.setattr(routes.JobDB, "find_active", lambda *_args: None)
    seen = []
    monkeypatch.setattr(routes.JobDB, "create_job",
                        lambda kind, target: seen.append((kind, target)) or {"id": "job-3"})
    app = Flask(__name__)
    app.register_blueprint(routes.code_intelligence_bp)
    response = app.test_client().post("/api/context/code-intelligence/repos/3/sync")
    assert response.status_code == 202
    assert seen == [("codegraph_sync", "3")]
