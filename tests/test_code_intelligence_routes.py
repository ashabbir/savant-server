"""CodeGraph-only provider selection and service policy tests."""

import pytest

pytestmark = pytest.mark.no_db


class FakeProvider:
    name = "codegraph"

    def __init__(self, *, symbols=None, error=None):
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
        id=f"codegraph:{name}", name=name, kind="function", language="python",
        location=SymbolLocation(
            repo_id="repo-1", file_path="src/service.py", start_line=1, end_line=2,
        ),
    )


def test_registry_requires_codegraph_provider():
    from code_intelligence.registry import CodeIntelligenceProviderRegistry

    with pytest.raises(ValueError, match="codegraph provider is required"):
        CodeIntelligenceProviderRegistry({})


def test_registry_defaults_to_codegraph_and_ignores_removed_provider_selection():
    from code_intelligence.registry import CodeIntelligenceProviderRegistry

    provider = FakeProvider()
    registry = CodeIntelligenceProviderRegistry(
        {"codegraph": provider}, selection_loader=lambda _repo_id: "removed_provider"
    )

    assert registry.get_provider("repo-1") is provider


def test_service_authorizes_repo_before_loading_provider(tmp_path):
    from code_intelligence.service import CodeIntelligenceService

    events = []

    class Registry:
        def get_provider(self, repo_id):
            events.append(("select", repo_id))
            return FakeProvider()

    def deny(repo_id, root):
        events.append(("authorize", repo_id, root))
        raise PermissionError("repository not authorized")

    service = CodeIntelligenceService(Registry(), authorize_repo=deny)
    with pytest.raises(PermissionError, match="not authorized"):
        service.search_symbols("repo-1", tmp_path, "run", limit=10)
    assert events == [("authorize", "repo-1", tmp_path)]


def test_service_caps_client_limit_at_server_policy(tmp_path):
    from code_intelligence.registry import CodeIntelligenceProviderRegistry
    from code_intelligence.service import CodeIntelligenceService

    provider = FakeProvider(symbols=[_symbol(str(index)) for index in range(75)])
    service = CodeIntelligenceService(
        CodeIntelligenceProviderRegistry({"codegraph": provider}),
        authorize_repo=lambda *_args: None,
        max_search_limit=50,
    )

    result = service.search_symbols("repo-1", tmp_path, "run", limit=5000)

    assert provider.calls[0][-1] == 50
    assert len(result.items) == 50
    assert result.provider == "codegraph"
    assert result.incomplete is True


def test_engine_errors_propagate_without_fallback(tmp_path):
    from code_intelligence.provider import CodeIntelligenceError, ErrorCategory
    from code_intelligence.registry import CodeIntelligenceProviderRegistry
    from code_intelligence.service import CodeIntelligenceService

    error = CodeIntelligenceError(ErrorCategory.ENGINE_UNAVAILABLE, "bridge unavailable", retryable=True)
    provider = FakeProvider(error=error)
    service = CodeIntelligenceService(
        CodeIntelligenceProviderRegistry({"codegraph": provider}),
        authorize_repo=lambda *_args: None,
    )

    with pytest.raises(CodeIntelligenceError) as raised:
        service.search_symbols("repo-1", tmp_path, "run")
    assert raised.value.category is ErrorCategory.ENGINE_UNAVAILABLE
