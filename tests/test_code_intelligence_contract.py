"""Contract-first tests for the provider-neutral code intelligence seam."""

from datetime import datetime, timezone
from typing import runtime_checkable

import pytest

pytestmark = pytest.mark.no_db


def test_canonical_dtos_serialize_stable_provider_neutral_shapes():
    from code_intelligence.contracts import (
        CodeEdge,
        CodeSymbol,
        Freshness,
        ProviderHealth,
        Provenance,
        SymbolLocation,
    )

    location = SymbolLocation(
        repo_id="repo-1",
        file_path="src/service.py",
        start_line=10,
        end_line=18,
        start_column=4,
        end_column=20,
    )
    symbol = CodeSymbol(
        id="codegraph:42",
        name="run",
        qualified_name="Service.run",
        kind="function",
        language="python",
        location=location,
        signature="run(self)",
        docstring=None,
        flags={"async": False},
        metadata={"provider_kind": "method"},
    )
    edge = CodeEdge(
        source_id="codegraph:42",
        target_id="codegraph:84",
        kind="calls",
        location=None,
        provenance=Provenance.STATIC,
        confidence=None,
        metadata={},
    )
    indexed_at = datetime(2026, 7, 18, tzinfo=timezone.utc)
    health = ProviderHealth(
        provider="codegraph",
        indexed=True,
        freshness=Freshness.FRESH,
        indexed_at=indexed_at,
        graph_version=None,
        files=3,
        nodes=9,
        edges=4,
        warnings=[],
    )

    assert symbol.model_dump(mode="json") == {
        "id": "codegraph:42",
        "name": "run",
        "qualified_name": "Service.run",
        "kind": "function",
        "language": "python",
        "location": {
            "repo_id": "repo-1",
            "file_path": "src/service.py",
            "start_line": 10,
            "end_line": 18,
            "start_column": 4,
            "end_column": 20,
        },
        "signature": "run(self)",
        "docstring": None,
        "flags": {"async": False},
        "metadata": {"provider_kind": "method"},
    }
    assert edge.model_dump(mode="json")["provenance"] == "static"
    assert edge.model_dump(mode="json")["confidence"] is None
    assert health.model_dump(mode="json")["indexed_at"] == indexed_at.isoformat()
    assert health.model_dump(mode="json")["freshness"] == "fresh"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start_line", 0),
        ("end_line", 0),
        ("file_path", "/absolute/path.py"),
        ("file_path", "../escape.py"),
    ],
)
def test_symbol_location_rejects_invalid_or_unsafe_source_locations(field, value):
    from code_intelligence.contracts import SymbolLocation

    values = {
        "repo_id": "repo-1",
        "file_path": "src/service.py",
        "start_line": 1,
        "end_line": 2,
        "start_column": None,
        "end_column": None,
    }
    values[field] = value

    with pytest.raises(ValueError):
        SymbolLocation(**values)


def test_provider_contract_declares_capabilities_health_and_all_required_operations():
    from code_intelligence.provider import (
        CodeIntelligenceProvider,
        ProviderCapabilities,
    )

    assert runtime_checkable(CodeIntelligenceProvider) is CodeIntelligenceProvider
    required = {
        "ensure_index",
        "search_symbols",
        "explore",
        "get_symbol",
        "get_callers",
        "get_callees",
        "get_impact",
        "get_neighbors",
        "health",
    }
    assert required <= set(CodeIntelligenceProvider.__dict__)

    capabilities = ProviderCapabilities(
        indexing=False,
        symbol_search=True,
        explore=True,
        symbol_lookup=True,
        callers=False,
        callees=False,
        impact=False,
        neighbors=True,
    )
    assert capabilities.model_dump()["symbol_search"] is True
    assert capabilities.model_dump()["impact"] is False


def test_provider_errors_are_typed_and_preserve_safe_category_details():
    from code_intelligence.provider import CodeIntelligenceError, ErrorCategory

    error = CodeIntelligenceError(
        ErrorCategory.ENGINE_UNAVAILABLE,
        "bridge socket unavailable",
        retryable=True,
    )

    assert error.category is ErrorCategory.ENGINE_UNAVAILABLE
    assert error.retryable is True
    assert str(error) == "bridge socket unavailable"
    assert {item.value for item in ErrorCategory} >= {
        "not_indexed",
        "path_refused",
        "busy",
        "timeout",
        "unsupported",
        "engine_unavailable",
        "internal",
    }
