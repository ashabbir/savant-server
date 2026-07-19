"""TDD tests for Phase 1 selection, policy, fallback, and legacy projections."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.no_db


class FakeProvider:
    def __init__(self, name, *, symbols=None, error=None):
        self.name = name
        self.symbols = list(symbols or [])
        self.error = error
        self.calls = []

    def search_symbols(self, repo, query, filters, limit):
        self.calls.append((repo, query, filters, limit))
        if self.error:
            raise self.error
        return self.symbols[:limit]


def _symbol(name="run"):
    from code_intelligence.contracts import CodeSymbol, SymbolLocation

    return CodeSymbol(
        id=f"test:{name}",
        name=name,
        qualified_name=None,
        kind="function",
        language="python",
        location=SymbolLocation(
            repo_id="repo-1",
            file_path="src/service.py",
            start_line=1,
            end_line=2,
            start_column=None,
            end_column=None,
        ),
        signature=None,
        docstring=None,
        flags={},
        metadata={},
    )


def test_registry_defaults_to_legacy_without_persisted_configuration():
    from code_intelligence.registry import CodeIntelligenceProviderRegistry

    legacy = FakeProvider("legacy")
    codegraph = FakeProvider("codegraph")
    registry = CodeIntelligenceProviderRegistry(
        providers={"legacy": legacy, "codegraph": codegraph},
        selection_loader=lambda _repo_id: None,
    )

    selected = registry.get_provider("repo-without-config")

    assert selected is legacy


def test_registry_uses_persisted_selection_and_never_request_selection():
    from code_intelligence.registry import CodeIntelligenceProviderRegistry

    legacy = FakeProvider("legacy")
    codegraph = FakeProvider("codegraph")
    seen = []
    registry = CodeIntelligenceProviderRegistry(
        providers={"legacy": legacy, "codegraph": codegraph},
        selection_loader=lambda repo_id: seen.append(repo_id) or "codegraph",
    )

    assert registry.get_provider("repo-1") is codegraph
    assert seen == ["repo-1"]
    with pytest.raises(TypeError):
        registry.get_provider("repo-1", provider="legacy")


def test_service_authorizes_repo_before_loading_or_invoking_provider(tmp_path):
    from code_intelligence.service import CodeIntelligenceService

    events = []

    class Registry:
        def get_provider(self, repo_id):
            events.append(("select", repo_id))
            return FakeProvider("legacy")

    def deny(repo_id, root):
        events.append(("authorize", repo_id, root))
        raise PermissionError("repository not authorized")

    service = CodeIntelligenceService(Registry(), authorize_repo=deny)

    with pytest.raises(PermissionError, match="not authorized"):
        service.search_symbols("repo-1", tmp_path, "run", filters={}, limit=10)
    assert events == [("authorize", "repo-1", tmp_path)]


def test_service_caps_client_limit_at_server_policy(tmp_path):
    from code_intelligence.registry import CodeIntelligenceProviderRegistry
    from code_intelligence.service import CodeIntelligenceService

    provider = FakeProvider("legacy", symbols=[_symbol(str(i)) for i in range(75)])
    registry = CodeIntelligenceProviderRegistry({"legacy": provider})
    service = CodeIntelligenceService(
        registry,
        authorize_repo=lambda _repo_id, _root: None,
        max_search_limit=50,
    )

    result = service.search_symbols("repo-1", tmp_path, "run", filters={}, limit=5000)

    assert provider.calls[0][-1] == 50
    assert len(result.items) == 50
    assert result.incomplete is True


def test_normal_empty_primary_result_does_not_fallback(tmp_path):
    from code_intelligence.registry import CodeIntelligenceProviderRegistry
    from code_intelligence.service import CodeIntelligenceService

    codegraph = FakeProvider("codegraph", symbols=[])
    legacy = FakeProvider("legacy", symbols=[_symbol()])
    registry = CodeIntelligenceProviderRegistry(
        {"legacy": legacy, "codegraph": codegraph},
        selection_loader=lambda _repo_id: "codegraph",
    )
    service = CodeIntelligenceService(
        registry,
        authorize_repo=lambda _repo_id, _root: None,
    )

    result = service.search_symbols("repo-1", tmp_path, "missing", filters={}, limit=20)

    assert result.items == []
    assert result.provider == "codegraph"
    assert legacy.calls == []


@pytest.mark.parametrize("category", ["timeout", "busy", "internal", "unsupported"])
def test_non_engine_provider_errors_do_not_fallback(tmp_path, category):
    from code_intelligence.provider import CodeIntelligenceError, ErrorCategory
    from code_intelligence.registry import CodeIntelligenceProviderRegistry
    from code_intelligence.service import CodeIntelligenceService

    error = CodeIntelligenceError(ErrorCategory(category), "primary failed")
    codegraph = FakeProvider("codegraph", error=error)
    legacy = FakeProvider("legacy", symbols=[_symbol()])
    registry = CodeIntelligenceProviderRegistry(
        {"legacy": legacy, "codegraph": codegraph},
        selection_loader=lambda _repo_id: "codegraph",
    )
    service = CodeIntelligenceService(registry, authorize_repo=lambda *_args: None)

    with pytest.raises(CodeIntelligenceError) as raised:
        service.search_symbols("repo-1", tmp_path, "run", filters={}, limit=20)
    assert raised.value.category is ErrorCategory(category)
    assert legacy.calls == []


def test_engine_unavailable_falls_back_to_legacy_with_visible_warning(tmp_path):
    from code_intelligence.provider import CodeIntelligenceError, ErrorCategory
    from code_intelligence.registry import CodeIntelligenceProviderRegistry
    from code_intelligence.service import CodeIntelligenceService

    codegraph = FakeProvider(
        "codegraph",
        error=CodeIntelligenceError(
            ErrorCategory.ENGINE_UNAVAILABLE,
            "bridge unavailable",
            retryable=True,
        ),
    )
    legacy = FakeProvider("legacy", symbols=[_symbol()])
    registry = CodeIntelligenceProviderRegistry(
        {"legacy": legacy, "codegraph": codegraph},
        selection_loader=lambda _repo_id: "codegraph",
    )
    service = CodeIntelligenceService(registry, authorize_repo=lambda *_args: None)

    result = service.search_symbols("repo-1", tmp_path, "run", filters={}, limit=20)

    assert [item.name for item in result.items] == ["run"]
    assert result.provider == "legacy_fallback"
    assert result.incomplete is True
    assert any("codegraph" in warning and "unavailable" in warning for warning in result.warnings)


def test_legacy_provider_maps_ast_rows_to_canonical_symbols(monkeypatch, tmp_path):
    from code_intelligence.legacy_provider import LegacyCodeIntelligenceProvider

    rows = [{
        "id": 42,
        "node_type": "function",
        "name": "run",
        "start_line": 10,
        "end_line": 12,
        "rel_path": "src/service.py",
        "repo": "repo-1",
    }]
    seen = []

    class Context:
        @staticmethod
        def search_ast_nodes(query, repo_filter=None):
            seen.append((query, repo_filter))
            return rows

    provider = LegacyCodeIntelligenceProvider(context_db=Context, graphify_db=object())
    repo = {"repo_id": "repo-1", "name": "repo-1", "root": Path(tmp_path)}

    symbols = provider.search_symbols(repo, "run", {}, 10)

    assert seen == [("run", "repo-1")]
    assert len(symbols) == 1
    assert symbols[0].id == "legacy:ast:42"
    assert symbols[0].location.file_path == "src/service.py"
    assert symbols[0].location.repo_id == "repo-1"
    assert symbols[0].metadata["provider_kind"] == "function"


def test_legacy_provider_maps_graphify_search_to_canonical_explore(monkeypatch, tmp_path):
    from code_intelligence.legacy_provider import LegacyCodeIntelligenceProvider

    graph_rows = [{
        "node_id": "node-1",
        "node_type": "function",
        "title": "run",
        "content": "def run(): ...",
        "metadata": {"path": "src/service.py", "start_line": 10, "end_line": 12},
        "edges": [{
            "source_id": "node-1",
            "target_id": "node-2",
            "edge_type": "calls",
            "weight": 1.0,
        }],
    }]
    seen = []

    class Graphify:
        @staticmethod
        def search(query, workspace_id=None, limit=20):
            seen.append((query, workspace_id, limit))
            return graph_rows

    provider = LegacyCodeIntelligenceProvider(context_db=object(), graphify_db=Graphify)
    repo = {"repo_id": "repo-1", "name": "repo-1", "root": Path(tmp_path)}

    result = provider.explore(repo, "run", max_files=5, include_source=True)

    assert seen == [("run", "repo-1", 5)]
    assert result.symbols[0].id == "legacy:graphify:node-1"
    assert result.edges[0].source_id == "legacy:graphify:node-1"
    assert result.edges[0].target_id == "legacy:graphify:node-2"
    assert result.edges[0].provenance.value == "legacy"
    assert result.edges[0].confidence is None


def test_legacy_provider_health_returns_stable_provider_health_shape():
    """Health is mandatory even when richer traversal capabilities are unsupported."""
    from code_intelligence.contracts import ProviderHealth
    from code_intelligence.legacy_provider import LegacyCodeIntelligenceProvider

    class Context:
        @staticmethod
        def get_repo(repo_id):
            assert repo_id == "repo-1"
            return {"name": repo_id, "status": "indexed", "indexed_at": None}

        @staticmethod
        def list_ast_nodes(repo_filter=None):
            assert repo_filter == "repo-1"
            return [{"name": "run"}]

        @staticmethod
        def list_code_files(repo_filter=None):
            assert repo_filter == "repo-1"
            return [{"path": "src/service.py"}]

    class Graphify:
        @staticmethod
        def get_stats(workspace_id):
            assert workspace_id == "repo-1"
            return {"node_count": 2, "edge_count": 1}

    provider = LegacyCodeIntelligenceProvider(context_db=Context, graphify_db=Graphify)

    health = provider.health({"repo_id": "repo-1"})

    assert isinstance(health, ProviderHealth)
    assert health.provider == "legacy"
    assert health.indexed is True
    assert health.freshness.value == "fresh"
    assert health.files == 1
    assert health.nodes == 2
    assert health.edges == 1


@pytest.mark.parametrize(
    ("repo_status", "ast_rows", "graph_nodes", "expected_indexed"),
    [
        ("indexed", [], 0, False),
        ("ast_only", [{"name": "run"}], 0, True),
    ],
)
def test_legacy_health_uses_structural_data_not_semantic_repo_status(
    repo_status, ast_rows, graph_nodes, expected_indexed
):
    from code_intelligence.legacy_provider import LegacyCodeIntelligenceProvider

    class Context:
        @staticmethod
        def get_repo(_repo_id):
            return {"status": repo_status, "indexed_at": None}

        @staticmethod
        def list_ast_nodes(repo_filter=None):
            return ast_rows

        @staticmethod
        def list_code_files(repo_filter=None):
            return []

    class Graphify:
        @staticmethod
        def get_stats(workspace_id):
            return {"node_count": graph_nodes, "edge_count": 0}

    provider = LegacyCodeIntelligenceProvider(context_db=Context, graphify_db=Graphify)

    health = provider.health({"repo_id": "repo-1"})

    assert health.indexed is expected_indexed
    assert (health.freshness.value == "fresh") is expected_indexed
