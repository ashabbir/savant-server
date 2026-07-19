"""Authorization, policy bounds, and CodeGraph provider dispatch."""

from pathlib import Path

from .contracts import ExploreResult, SearchResult, SubgraphRequest


class CodeIntelligenceService:
    def __init__(self, registry, *, authorize_repo, max_search_limit: int = 50, **_compat):
        self.registry = registry
        self.authorize_repo = authorize_repo
        self.max_search_limit = max_search_limit

    def search_symbols(self, repo_id, root, query, *, filters=None, limit=20):
        root = Path(root)
        self.authorize_repo(repo_id, root)
        provider = self.registry.get_provider(repo_id)
        bounded_limit = max(1, min(limit, self.max_search_limit))
        repo = {"repo_id": repo_id, "name": repo_id, "root": root}
        items = provider.search_symbols(repo, query, filters or {}, bounded_limit)
        return SearchResult(
            items=items,
            provider=provider.name,
            incomplete=limit > bounded_limit,
        )

    def list_symbols(self, repo_id, root, *, filters=None, limit=100, cursor=None):
        root = Path(root)
        self.authorize_repo(repo_id, root)
        provider = self.registry.get_provider(repo_id)
        bounded = max(1, min(int(limit), 1000))
        repo = {"repo_id": repo_id, "name": repo_id, "root": root}
        result = provider.list_symbols(repo, filters or {}, bounded, cursor)
        return {
            "items": result.get("items", []),
            "provider": provider.name,
            "next_cursor": result.get("next_cursor"),
            "incomplete": bool(result.get("incomplete") or int(limit) > bounded),
            "warnings": result.get("warnings", []),
        }

    def _dispatch(self, repo_id, root, operation, *args, **kwargs):
        root = Path(root)
        self.authorize_repo(repo_id, root)
        provider = self.registry.get_provider(repo_id)
        repo = {"repo_id": repo_id, "name": repo_id, "root": root}
        return provider, getattr(provider, operation)(repo, *args, **kwargs)

    def ensure_index(self, repo_id, root, *, mode="create_or_sync"):
        _provider, result = self._dispatch(repo_id, root, "ensure_index", mode=mode)
        return result

    def health(self, repo_id, root):
        provider, result = self._dispatch(repo_id, root, "health")
        if provider.name != result.provider:
            result.provider = provider.name
        return result

    def explore(self, repo_id, root, query, *, max_files=5, include_source=True):
        bounded = max(1, min(int(max_files), 50))
        provider, result = self._dispatch(
            repo_id, root, "explore", query, max_files=bounded, include_source=bool(include_source)
        )
        if not isinstance(result, ExploreResult):
            result = ExploreResult.model_validate(result)
        return result

    def subgraph(self, repo_id, root, request: SubgraphRequest):
        depth = max(1, min(int(request.depth), 3))
        limit = max(1, min(int(request.limit), 500))
        symbol_ref = request.roots[0]
        operations = {
            "neighbors": ("get_neighbors", {"depth": depth, "edge_kinds": request.edge_kinds}),
            "callers": ("get_callers", {"depth": depth, "limit": limit}),
            "callees": ("get_callees", {"depth": depth, "limit": limit}),
            "impact": ("get_impact", {"depth": depth, "direction": request.direction}),
        }
        operation, kwargs = operations[request.mode]
        _provider, result = self._dispatch(repo_id, root, operation, symbol_ref, **kwargs)
        result.symbols = result.symbols[:limit]
        result.edges = result.edges[: min(limit * 2, 1000)]
        return result
