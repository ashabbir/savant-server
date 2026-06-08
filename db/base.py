"""Shared database utilities — DRY base for all DB layers."""

import json
from datetime import datetime, timezone


def _now() -> str:
    """UTC timestamp as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row, json_fields: dict | None = None) -> dict | None:
    """Convert sqlite3.Row → dict with optional JSON deserialization.

    Args:
        row: sqlite3.Row or None.
        json_fields: mapping of column name → default value for JSON columns.
            e.g. {"metadata": {}, "files": [], "detail": {}}
    """
    if row is None:
        return None
    d = dict(row)
    if json_fields:
        for col, default in json_fields.items():
            val = d.get(col)
            if isinstance(val, str):
                try:
                    d[col] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    d[col] = default
    return d


def _rows_to_dicts(rows, json_fields: dict | None = None) -> list[dict]:
    """Convert list of sqlite3.Row → list of dicts."""
    return [_row_to_dict(r, json_fields) for r in rows]
