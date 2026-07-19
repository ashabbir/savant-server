"""Bounded, query-redacted shadow comparison diagnostics."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def symbol_identity(symbol) -> tuple:
    """Provider-independent identity suitable for migration comparisons."""
    location = symbol.location
    return (
        location.repo_id,
        location.file_path,
        location.start_line,
        symbol.qualified_name or symbol.name,
        symbol.kind,
    )


def compare_symbols(primary, shadow, *, max_samples: int = 20) -> dict:
    primary_ids = {symbol_identity(item) for item in primary}
    shadow_ids = {symbol_identity(item) for item in shadow}
    overlap = primary_ids & shadow_ids
    primary_only = sorted(primary_ids - shadow_ids)[:max_samples]
    shadow_only = sorted(shadow_ids - primary_ids)[:max_samples]
    return {
        "primary_count": len(primary_ids),
        "shadow_count": len(shadow_ids),
        "overlap_count": len(overlap),
        "overlap_ratio": len(overlap) / max(1, len(primary_ids | shadow_ids)),
        "primary_only": [list(item) for item in primary_only],
        "shadow_only": [list(item) for item in shadow_only],
        "samples_truncated": len(primary_ids - shadow_ids) > max_samples or len(shadow_ids - primary_ids) > max_samples,
    }


@dataclass
class BoundedComparisonRecorder:
    """In-memory bounded recorder; production telemetry adapters may replace it."""

    max_records: int = 200
    records: list[dict] = field(default_factory=list)

    def record(self, *, repo_id, operation, query, primary_provider, shadow_provider, metrics):
        record = {
            "repo_id": str(repo_id),
            "operation": operation,
            # Never retain proprietary query text. This digest is diagnostic only.
            "query_hash": hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()[:16],
            "primary_provider": primary_provider,
            "shadow_provider": shadow_provider,
            "metrics": metrics,
        }
        self.records.append(record)
        if len(self.records) > self.max_records:
            del self.records[: len(self.records) - self.max_records]
