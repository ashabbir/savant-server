"""Canonical provider adapter for the supervised CodeGraph bridge."""

from pathlib import Path
from typing import Any

from .contracts import CodeSymbol, ExploreResult, IndexResult, ProviderHealth, Subgraph, SymbolContext
from .provider import ProviderCapabilities


class CodeGraphProvider:
    name = "codegraph"
    capabilities = ProviderCapabilities(
        indexing=True, symbol_search=True, explore=True, symbol_lookup=True,
        callers=True, callees=True, impact=True, neighbors=True,
    )

    DEFAULT_DEPTH_1 = 1
    DEFAULT_DEPTH_2 = 2
    DEFAULT_LIMIT = 20

    def __init__(self, client: Any, *, watch_enabled: bool = True, index_timeout: float = 600.0) -> None:
        self.client = client
        self.watch_enabled = watch_enabled
        self.index_timeout = index_timeout
        self._registered: dict[str, str] = {}

    def _resolve_repo_details(self, repo: dict | Any) -> tuple[str, Path]:
        if isinstance(repo, dict):
            repo_id = str(repo["repo_id"])
            root = Path(repo["root"])
        else:
            repo_id = str(repo.repo_id)
            root = Path(repo.root)
        return repo_id, root.resolve(strict=False)

    def _repo(self, repo: dict | Any) -> str:
        repo_id, root = self._resolve_repo_details(repo)
        root_str = str(root)
        if self._registered.get(repo_id) != root_str:
            self.client.call("register", repo_id=repo_id, params={"root": root_str})
            self._registered[repo_id] = root_str
        return repo_id

    def ensure_index(self, repo: dict | Any, mode: str = "create_or_sync", request_id: str | None = None) -> IndexResult:
        repo_id = self._repo(repo)
        return IndexResult.model_validate(self.client.call(
            "ensure_index", repo_id=repo_id,
            params={"mode": mode, "watch": self.watch_enabled},
            request_id=request_id,
            timeout=self.index_timeout,
        ))

    def search_symbols(self, repo: dict | Any, query: str, filters: dict | None, limit: int) -> list[CodeSymbol]:
        repo_id = self._repo(repo)
        result = self.client.call("search_symbols", repo_id=repo_id, params={"query": query, "filters": filters, "limit": limit})
        return [CodeSymbol.model_validate(item) for item in result]

    def list_symbols(self, repo: dict | Any, filters: dict | None, limit: int, cursor: str | None = None) -> dict:
        repo_id = self._repo(repo)
        result = self.client.call("list_symbols", repo_id=repo_id, params={"filters": filters, "limit": limit, "cursor": cursor})
        result["items"] = [CodeSymbol.model_validate(item) for item in result.get("items", [])]
        return result

    def explore(self, repo: dict | Any, query: str, max_files: int, include_source: bool = True) -> ExploreResult:
        repo_id = self._repo(repo)
        return ExploreResult.model_validate(self.client.call("explore", repo_id=repo_id, params={"query": query, "max_files": max_files, "include_source": include_source}))

    def get_symbol(self, repo: dict | Any, symbol_ref: dict | Any, include_source: bool = False) -> SymbolContext:
        repo_id = self._repo(repo)
        return SymbolContext.model_validate(self.client.call("get_symbol", repo_id=repo_id, params={"symbol_ref": symbol_ref, "include_source": include_source}))

    def _subgraph(self, operation: str, repo: dict | Any, symbol_ref: dict | Any, **params: Any) -> Subgraph:
        repo_id = self._repo(repo)
        params["symbol_ref"] = symbol_ref
        return Subgraph.model_validate(self.client.call(operation, repo_id=repo_id, params=params))

    def get_callers(self, repo: dict | Any, symbol_ref: dict | Any, depth: int = DEFAULT_DEPTH_1, limit: int = DEFAULT_LIMIT) -> Subgraph:
        return self._subgraph("get_callers", repo, symbol_ref, depth=depth, limit=limit)

    def get_callees(self, repo: dict | Any, symbol_ref: dict | Any, depth: int = DEFAULT_DEPTH_1, limit: int = DEFAULT_LIMIT) -> Subgraph:
        return self._subgraph("get_callees", repo, symbol_ref, depth=depth, limit=limit)

    def get_impact(self, repo: dict | Any, symbol_ref: dict | Any, depth: int = DEFAULT_DEPTH_2, direction: str = "both") -> Subgraph:
        return self._subgraph("get_impact", repo, symbol_ref, depth=depth, direction=direction)

    def get_neighbors(self, repo: dict | Any, symbol_ref: dict | Any, depth: int = DEFAULT_DEPTH_1, edge_kinds: list[str] | None = None) -> Subgraph:
        return self._subgraph("get_neighbors", repo, symbol_ref, depth=depth, edge_kinds=edge_kinds)

    def health(self, repo: dict | Any) -> ProviderHealth:
        repo_id = self._repo(repo)
        return ProviderHealth.model_validate(self.client.call("health", repo_id=repo_id))
