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
        if "codegraph" not in self.providers:
            raise ValueError("codegraph provider is required")

    def get_provider(self, repo_id: str):
        selection = self.selection_loader(repo_id) or "codegraph"
        if selection != "codegraph":
            selection = "codegraph"
        return self.providers[selection]

    def get_named_provider(self, name: str):
        return self.providers[name]
