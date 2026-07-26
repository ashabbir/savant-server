"""Stable DTOs shared by code-intelligence providers and consumers."""

from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


class Freshness(str, Enum):
    FRESH = "fresh"
    PENDING_SYNC = "pending_sync"
    STALE = "stale"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class Provenance(str, Enum):
    STATIC = "static"
    HEURISTIC = "heuristic"
    IMPORTED = "imported"
    UNKNOWN = "unknown"


class SymbolLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    start_column: int | None = Field(default=None, ge=0)
    end_column: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_location(self):
        path = PurePosixPath(self.file_path.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or not self.file_path:
            raise ValueError("file_path must be a safe repository-relative path")
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        return self


class CodeSymbol(BaseModel):
    id: str
    name: str
    qualified_name: str | None = None
    kind: str
    language: str | None = None
    location: SymbolLocation
    signature: str | None = None
    docstring: str | None = None
    flags: dict[str, bool] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CodeEdge(BaseModel):
    source_id: str
    target_id: str
    kind: str
    location: SymbolLocation | None = None
    provenance: Provenance = Provenance.UNKNOWN
    confidence: float | None = Field(default=None, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderHealth(BaseModel):
    provider: str
    indexed: bool
    freshness: Freshness
    indexed_at: datetime | None = None
    graph_version: str | None = None
    files: int | None = None
    nodes: int | None = None
    edges: int | None = None
    warnings: list[str] = Field(default_factory=list)

    @field_serializer("indexed_at", when_used="json")
    def serialize_indexed_at(self, value: datetime | None):
        return value.isoformat() if value is not None else None


class IndexResult(BaseModel):
    provider: str
    accepted: bool = True
    warnings: list[str] = Field(default_factory=list)
    result: Any = Field(default_factory=dict)


class SearchResult(BaseModel):
    items: list[CodeSymbol] = Field(default_factory=list)
    provider: str
    incomplete: bool = False
    warnings: list[str] = Field(default_factory=list)


class ExploreResult(BaseModel):
    symbols: list[CodeSymbol] = Field(default_factory=list)
    edges: list[CodeEdge] = Field(default_factory=list)
    provider: str = "unknown"
    incomplete: bool = False
    warnings: list[str] = Field(default_factory=list)


class SymbolContext(BaseModel):
    symbol: CodeSymbol
    source: str | None = None


class Subgraph(BaseModel):
    symbols: list[CodeSymbol] = Field(default_factory=list)
    edges: list[CodeEdge] = Field(default_factory=list)
    incomplete: bool = False
    warnings: list[str] = Field(default_factory=list)


class SubgraphRequest(BaseModel):
    """Provider-independent subgraph query accepted by service boundaries."""

    model_config = ConfigDict(extra="forbid")

    roots: list[Any] = Field(min_length=1)
    mode: Literal["neighbors", "callers", "callees", "impact"] = "neighbors"
    depth: int = 1
    limit: int = 100
    edge_kinds: list[str] | None = None
    direction: Literal["both", "upstream", "downstream"] = "both"
