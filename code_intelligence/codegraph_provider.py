"""Canonical provider adapter for the supervised CodeGraph bridge."""

from pathlib import Path

from .contracts import CodeSymbol, ExploreResult, IndexResult, ProviderHealth, Subgraph, SymbolContext
from .provider import ProviderCapabilities


class CodeGraphProvider:
    name = "codegraph"
    capabilities = ProviderCapabilities(
        indexing=True, symbol_search=True, explore=True, symbol_lookup=True,
        callers=True, callees=True, impact=True, neighbors=True,
    )

    def __init__(self, client, *, watch_enabled: bool = True, index_timeout: float = 600.0):
        self.client = client
        self.watch_enabled = watch_enabled
        self.index_timeout = index_timeout
        self._registered: dict[str, str] = {}

    def _repo(self, repo):
        repo_id = str(repo["repo_id"] if isinstance(repo, dict) else repo.repo_id)
        root = Path(repo["root"] if isinstance(repo, dict) else repo.root).resolve(strict=True)
        if self._registered.get(repo_id) != str(root):
            self.client.call("register", repo_id=repo_id, params={"root": str(root)})
            self._registered[repo_id] = str(root)
        return repo_id

    def ensure_index(self, repo, mode="create_or_sync", request_id=None):
        repo_id = self._repo(repo)
        return IndexResult.model_validate(self.client.call(
            "ensure_index", repo_id=repo_id,
            params={"mode": mode, "watch": self.watch_enabled},
            request_id=request_id,
            timeout=self.index_timeout,
        ))

    def search_symbols(self, repo, query, filters, limit):
        repo_id = self._repo(repo)
        result = self.client.call("search_symbols", repo_id=repo_id, params={"query": query, "filters": filters, "limit": limit})
        return [CodeSymbol.model_validate(item) for item in result]

    def list_symbols(self, repo, filters, limit, cursor=None):
        repo_id = self._repo(repo)
        result = self.client.call("list_symbols", repo_id=repo_id, params={"filters": filters, "limit": limit, "cursor": cursor})
        result["items"] = [CodeSymbol.model_validate(item) for item in result.get("items", [])]
        return result

    def explore(self, repo, query, max_files, include_source=True):
        repo_id = self._repo(repo)
        return ExploreResult.model_validate(self.client.call("explore", repo_id=repo_id, params={"query": query, "max_files": max_files, "include_source": include_source}))

    def get_symbol(self, repo, symbol_ref, include_source=False):
        repo_id = self._repo(repo)
        return SymbolContext.model_validate(self.client.call("get_symbol", repo_id=repo_id, params={"symbol_ref": symbol_ref, "include_source": include_source}))

    def _subgraph(self, operation, repo, symbol_ref, **params):
        repo_id = self._repo(repo)
        params["symbol_ref"] = symbol_ref
        return Subgraph.model_validate(self.client.call(operation, repo_id=repo_id, params=params))

    def get_callers(self, repo, symbol_ref, depth=1, limit=20): return self._subgraph("get_callers", repo, symbol_ref, depth=depth, limit=limit)
    def get_callees(self, repo, symbol_ref, depth=1, limit=20): return self._subgraph("get_callees", repo, symbol_ref, depth=depth, limit=limit)
    def get_impact(self, repo, symbol_ref, depth=2, direction="both"): return self._subgraph("get_impact", repo, symbol_ref, depth=depth, direction=direction)
    def get_neighbors(self, repo, symbol_ref, depth=1, edge_kinds=None): return self._subgraph("get_neighbors", repo, symbol_ref, depth=depth, edge_kinds=edge_kinds)

    def health(self, repo):
        repo_id = self._repo(repo)
        return ProviderHealth.model_validate(self.client.call("health", repo_id=repo_id))
