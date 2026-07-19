"""Authorization, policy bounds, provider dispatch, and visible fallback."""

from pathlib import Path

from .contracts import ExploreResult, SearchResult
from .provider import CodeIntelligenceError, ErrorCategory


class CodeIntelligenceService:
    def __init__(self, registry, *, authorize_repo, max_search_limit: int = 50,
                 rollout_state_loader=None, shadow_executor=None, comparison_recorder=None):
        self.registry = registry
        self.authorize_repo = authorize_repo
        self.max_search_limit = max_search_limit
        self.rollout_state_loader = rollout_state_loader or (lambda _repo_id: "legacy")
        self.shadow_executor = shadow_executor
        self.comparison_recorder = comparison_recorder

    def _schedule_shadow(self, repo_id, repo, operation, query, primary_provider, primary_items, invoke):
        if self.rollout_state_loader(repo_id) != "shadow" or not self.shadow_executor or not self.comparison_recorder:
            return
        try:
            shadow = self.registry.get_named_provider("codegraph")
        except KeyError:
            return
        if shadow.name == primary_provider:
            return

        def run():
            try:
                shadow_result = invoke(shadow)
                shadow_items = getattr(shadow_result, "symbols", shadow_result)
                from .comparison import compare_symbols
                self.comparison_recorder.record(
                    repo_id=repo_id, operation=operation, query=query,
                    primary_provider=primary_provider, shadow_provider=shadow.name,
                    metrics=compare_symbols(primary_items, shadow_items),
                )
            except Exception:
                # Shadow work is diagnostic and must never alter the selected response.
                return
        self.shadow_executor.submit(run)

    def search_symbols(self, repo_id, root, query, *, filters=None, limit=20):
        root = Path(root)
        self.authorize_repo(repo_id, root)
        provider = self.registry.get_provider(repo_id)
        bounded_limit = max(1, min(limit, self.max_search_limit))
        repo = {"repo_id": repo_id, "name": repo_id, "root": root}
        try:
            items = provider.search_symbols(repo, query, filters or {}, bounded_limit)
        except CodeIntelligenceError as error:
            if error.category is not ErrorCategory.ENGINE_UNAVAILABLE or provider.name == "legacy":
                raise
            legacy = self.registry.get_named_provider("legacy")
            items = legacy.search_symbols(repo, query, filters or {}, bounded_limit)
            return SearchResult(
                items=items,
                provider="legacy_fallback",
                incomplete=True,
                warnings=[f"{provider.name} unavailable; served by legacy fallback"],
            )
        self._schedule_shadow(
            repo_id, repo, "search_symbols", query, provider.name, items,
            lambda shadow: shadow.search_symbols(repo, query, filters or {}, bounded_limit),
        )
        return SearchResult(
            items=items,
            provider=provider.name,
            incomplete=limit > bounded_limit,
        )

    def _dispatch(self, repo_id, root, operation, *args, **kwargs):
        root = Path(root)
        self.authorize_repo(repo_id, root)
        provider = self.registry.get_provider(repo_id)
        repo = {"repo_id": repo_id, "name": repo_id, "root": root}
        try:
            return provider, getattr(provider, operation)(repo, *args, **kwargs)
        except CodeIntelligenceError as error:
            if error.category is not ErrorCategory.ENGINE_UNAVAILABLE or provider.name == "legacy":
                raise
            legacy = self.registry.get_named_provider("legacy")
            return legacy, getattr(legacy, operation)(repo, *args, **kwargs)

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
        if provider.name == "legacy" and self.registry.get_provider(repo_id).name != "legacy":
            result.provider = "legacy_fallback"
            result.incomplete = True
            result.warnings.append("primary engine unavailable; served by legacy fallback")
        self._schedule_shadow(
            repo_id, {"repo_id": repo_id, "name": repo_id, "root": Path(root)},
            "explore", query, result.provider, result.symbols,
            lambda shadow: shadow.explore({"repo_id": repo_id, "name": repo_id, "root": Path(root)}, query,
                                          max_files=bounded, include_source=bool(include_source)),
        )
        return result

    def subgraph(self, repo_id, root, *, roots, mode="neighbors", depth=1, limit=100, edge_kinds=None, direction="both"):
        if not roots:
            raise ValueError("roots required")
        depth = max(1, min(int(depth), 3))
        limit = max(1, min(int(limit), 500))
        symbol_ref = roots[0]
        operations = {
            "neighbors": ("get_neighbors", {"depth": depth, "edge_kinds": edge_kinds}),
            "callers": ("get_callers", {"depth": depth, "limit": limit}),
            "callees": ("get_callees", {"depth": depth, "limit": limit}),
            "impact": ("get_impact", {"depth": depth, "direction": direction}),
        }
        if mode not in operations:
            raise ValueError("mode must be neighbors, callers, callees, or impact")
        operation, kwargs = operations[mode]
        _provider, result = self._dispatch(repo_id, root, operation, symbol_ref, **kwargs)
        result.symbols = result.symbols[:limit]
        result.edges = result.edges[: min(limit * 2, 1000)]
        return result
