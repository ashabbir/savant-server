import pytest

pytestmark = pytest.mark.no_db


def _symbol(name="run", line=1):
    from code_intelligence.contracts import CodeSymbol, SymbolLocation

    return CodeSymbol(
        id=f"codegraph:{line}", name=name, qualified_name=f"Service.{name}", kind="function",
        location=SymbolLocation(
            repo_id="repo-1", file_path="src/a.py", start_line=line, end_line=line,
        ),
        language="python", flags={}, metadata={},
    )


def test_native_analysis_separates_metric_families_and_rejects_escape(monkeypatch, tmp_path):
    from code_intelligence import routes
    from flask import Flask

    source = tmp_path / "src" / "a.py"
    source.parent.mkdir()
    source.write_text("def run():\n    return 1\n")
    monkeypatch.setattr(routes.ContextDB, "get_repo_by_identifier", lambda _repo: {"path": str(tmp_path)})

    class Service:
        def search_symbols(self, *_args, **_kwargs):
            from code_intelligence.contracts import SearchResult
            return SearchResult(items=[_symbol()], provider="codegraph")

        def subgraph(self, *_args, **_kwargs):
            from code_intelligence.contracts import Subgraph
            return Subgraph(symbols=[_symbol()], edges=[])

    monkeypatch.setattr(routes, "build_service", lambda: Service())
    app = Flask(__name__)
    app.register_blueprint(routes.code_intelligence_bp)
    client = app.test_client()

    response = client.post(
        "/api/context/code-intelligence/repos/repo-1/analysis",
        json={"path": "src/a.py", "name": "run"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["complexity_metrics"]["algorithm"] == "deterministic_ast"
    assert payload["graph_metrics"]["algorithm"] == "provider_topology_counts"
    assert payload["graph_metrics"]["nodes"] == 1
    assert "findings" in payload["after"]

    refused = client.post(
        "/api/context/code-intelligence/repos/repo-1/analysis",
        json={"path": "../secret.py"},
    )
    assert refused.status_code == 400
    assert refused.get_json()["code"] == "path_refused"
