import pytest
from flask import Flask

pytestmark = pytest.mark.no_db


def _symbol():
    from code_intelligence.contracts import CodeSymbol, SymbolLocation
    return CodeSymbol(id="codegraph:1", name="run", kind="function", language="python",
        location=SymbolLocation(repo_id="3", file_path="src/a.py", start_line=2, end_line=4), metadata={})


@pytest.fixture
def client(monkeypatch, tmp_path):
    import context.routes as routes
    from context.db import ContextDB
    from code_intelligence.contracts import Freshness, ProviderHealth, SearchResult
    from db.code_intelligence import CodeIntelligenceConfigDB
    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setattr(ContextDB, "get_repo_by_identifier",
                        lambda value: {"id": 3, "name": "canonical", "path": str(tmp_path)} if str(value) == "3" else None)
    monkeypatch.setattr(ContextDB, "list_ast_nodes",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy AST must not be read")))
    monkeypatch.setattr(CodeIntelligenceConfigDB, "get", lambda _repo: {"provider": "codegraph"})

    class Service:
        def search_symbols(self, repo_id, root, query, **kwargs):
            assert repo_id == "3"
            return SearchResult(items=[_symbol()], provider="codegraph")
        def list_symbols(self, repo_id, root, **kwargs):
            assert repo_id == "3"
            assert kwargs["limit"] <= 1000
            return {"items": [_symbol()], "provider": "codegraph", "next_cursor": "1",
                    "incomplete": True, "warnings": []}
        def health(self, *_args):
            return ProviderHealth(provider="codegraph", indexed=True, freshness=Freshness.FRESH,
                                  graph_version="g1", files=1, nodes=1, edges=0)
    monkeypatch.setattr("code_intelligence.runtime.build_service", lambda: Service())
    app = Flask(__name__)
    app.register_blueprint(routes.context_bp)
    return app.test_client()


def test_numeric_ast_search_resolves_explicit_repo_id(client):
    response = client.get("/api/context/ast/search?repo=3&query=run", headers={"X-App-Name": "savant-olympus"})
    assert response.status_code == 200
    assert response.get_json()["results"][0]["id"] == "codegraph:1"


def test_codegraph_ast_list_is_bounded_flat_projection_and_bypasses_legacy(client):
    response = client.get("/api/context/ast/list?repo_id=3&repo=wrong&limit=99999",
                          headers={"X-App-Name": "savant-olympus"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["nodes"] == [{"repo": "canonical", "path": "src/a.py", "node_type": "function",
                                  "name": "run", "start_line": 2, "end_line": 4}]
    assert payload["provider"] == "codegraph"
    assert payload["freshness"] == "fresh"
    assert payload["incomplete"] is True
    assert payload["cursor"] == "1"
