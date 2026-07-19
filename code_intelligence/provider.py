"""Provider interface and classified errors."""

from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class ErrorCategory(str, Enum):
    NOT_INDEXED = "not_indexed"
    PATH_REFUSED = "path_refused"
    BUSY = "busy"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    INTERNAL = "internal"


class CodeIntelligenceError(RuntimeError):
    def __init__(self, category: ErrorCategory, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class ProviderCapabilities(BaseModel):
    indexing: bool
    symbol_search: bool
    explore: bool
    symbol_lookup: bool
    callers: bool
    callees: bool
    impact: bool
    neighbors: bool


@runtime_checkable
class CodeIntelligenceProvider(Protocol):
    name: str
    capabilities: ProviderCapabilities

    def ensure_index(self, repo: Any, mode: str = "create_or_sync"): ...
    def search_symbols(self, repo: Any, query: str, filters: dict, limit: int): ...
    def list_symbols(self, repo: Any, filters: dict, limit: int, cursor: str | None = None): ...
    def explore(self, repo: Any, query: str, max_files: int, include_source: bool = True): ...
    def get_symbol(self, repo: Any, symbol_ref: Any, include_source: bool = False): ...
    def get_callers(self, repo: Any, symbol_ref: Any, depth: int = 1, limit: int = 20): ...
    def get_callees(self, repo: Any, symbol_ref: Any, depth: int = 1, limit: int = 20): ...
    def get_impact(self, repo: Any, symbol_ref: Any, depth: int = 2, direction: str = "both"): ...
    def get_neighbors(self, repo: Any, symbol_ref: Any, depth: int = 1, edge_kinds=None): ...
    def health(self, repo: Any): ...
