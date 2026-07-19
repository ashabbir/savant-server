"""Persisted repository-to-provider selection."""

from collections.abc import Callable, Mapping
from typing import Any


class CodeIntelligenceProviderRegistry:
    def __init__(
        self,
        providers: Mapping[str, Any],
        selection_loader: Callable[[str], str | None] | None = None,
    ):
        self.providers = dict(providers)
        self.selection_loader = selection_loader or (lambda _repo_id: None)
        if "legacy" not in self.providers:
            raise ValueError("legacy provider is required")

    def get_provider(self, repo_id: str):
        selection = self.selection_loader(repo_id) or "legacy"
        try:
            return self.providers[selection]
        except KeyError as exc:
            raise LookupError(f"configured code intelligence provider is unavailable: {selection}") from exc

    def get_named_provider(self, name: str):
        return self.providers[name]
