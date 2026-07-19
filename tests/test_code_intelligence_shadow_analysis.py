from concurrent.futures import Future

import pytest

pytestmark = pytest.mark.no_db


def _symbol(provider, name="run", line=1):
    from code_intelligence.contracts import CodeSymbol, SymbolLocation
    return CodeSymbol(id=f"{provider}:{line}", name=name, qualified_name=f"Service.{name}", kind="function",
        location=SymbolLocation(repo_id="repo-1", file_path="src/a.py", start_line=line, end_line=line),
        language="python", flags={}, metadata={})


def test_shadow_comparison_uses_stable_identity_and_redacts_query(tmp_path):
    from code_intelligence.comparison import BoundedComparisonRecorder
    from code_intelligence.registry import CodeIntelligenceProviderRegistry
    from code_intelligence.service import CodeIntelligenceService

    class Provider:
        def __init__(self, name, items): self.name, self.items = name, items
        def search_symbols(self, *_args): return self.items
    class Immediate:
        def submit(self, fn):
            future = Future()
            try: future.set_result(fn())
            except Exception as exc: future.set_exception(exc)
            return future

    legacy = Provider("legacy", [_symbol("legacy")])
    codegraph = Provider("codegraph", [_symbol("codegraph")])
    recorder = BoundedComparisonRecorder(max_records=2)
    service = CodeIntelligenceService(
        CodeIntelligenceProviderRegistry({"legacy": legacy, "codegraph": codegraph}),
        authorize_repo=lambda *_args: None,
        rollout_state_loader=lambda _repo: "shadow",
        shadow_executor=Immediate(), comparison_recorder=recorder,
    )
    result = service.search_symbols("repo-1", tmp_path, "proprietary secret", limit=20)
    assert result.provider == "legacy"
    assert recorder.records[0]["metrics"]["overlap_count"] == 1
    assert "proprietary secret" not in repr(recorder.records)
    assert len(recorder.records[0]["query_hash"]) == 16


def test_shadow_failure_never_changes_primary_response(tmp_path):
    from code_intelligence.comparison import BoundedComparisonRecorder
    from code_intelligence.registry import CodeIntelligenceProviderRegistry
    from code_intelligence.service import CodeIntelligenceService

    class Legacy:
        name = "legacy"
        def search_symbols(self, *_args): return [_symbol("legacy")]
    class Broken:
        name = "codegraph"
        def search_symbols(self, *_args): raise RuntimeError("shadow failed")
    class Immediate:
        def submit(self, fn): fn()
    recorder = BoundedComparisonRecorder()
    service = CodeIntelligenceService(CodeIntelligenceProviderRegistry({"legacy": Legacy(), "codegraph": Broken()}),
        authorize_repo=lambda *_args: None, rollout_state_loader=lambda _: "shadow",
        shadow_executor=Immediate(), comparison_recorder=recorder)
    assert service.search_symbols("repo-1", tmp_path, "run").items[0].name == "run"
    assert recorder.records == []


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
            return SearchResult(items=[_symbol("legacy")], provider="legacy")
        def subgraph(self, *_args, **_kwargs):
            from code_intelligence.contracts import Subgraph
            return Subgraph(symbols=[_symbol("legacy")], edges=[])
    monkeypatch.setattr(routes, "build_service", lambda: Service())
    app = Flask(__name__)
    app.register_blueprint(routes.code_intelligence_bp)
    client = app.test_client()

    response = client.post("/api/context/code-intelligence/repos/repo-1/analysis", json={"path": "src/a.py", "name": "run"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["complexity_metrics"]["algorithm"] == "legacy_line_factor"
    assert payload["graph_metrics"]["algorithm"] == "provider_topology_counts"
    assert payload["graph_metrics"]["nodes"] == 1
    assert "findings" in payload["after"]

    refused = client.post("/api/context/code-intelligence/repos/repo-1/analysis", json={"path": "../secret.py"})
    assert refused.status_code == 400
    assert refused.get_json()["code"] == "path_refused"
