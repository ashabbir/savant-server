"""Lossless, versioned change reports for Contemplate (no restoration API)."""

import hashlib
import json
from datetime import date, datetime
from typing import Any


RESTORE_INSTRUCTIONS = [
    "Use privileged PostgreSQL access. Normal create-node APIs cannot restore original IDs or timestamps. Never include credentials in this report.",
    "Verify schema_version=1, target database identity, and integrity_sha256: SHA-256 of the UTF-8 JSON report without integrity_sha256, sorted keys, compact separators, ensure_ascii=True.",
    "Begin one transaction; acquire pg_advisory_xact_lock(4933453); LOCK TABLE kg_nodes, kg_edges IN SHARE ROW EXCLUSIVE MODE. Lock any dependent tables listed in changes too.",
    (
        "Compare all changes and incident_edges with their after states, including null (absence). "
        "Also check edge uniqueness keys. If all before states already match, do no writes: already reverted. "
        "Otherwise abort on any later edit, new incident edge, missing external endpoint, or uniqueness conflict; "
        "do not overwrite subsequent work."
    ),
    (
        "Remove changed edges that differ from their before state. Restore deleted nodes with their complete "
        "original rows, and update surviving nodes to their complete before rows. Remove run-created nodes "
        "only after checking for external dependents. Restore original edge rows and other dependent rows "
        "in foreign-key order. Preserve raw metadata, IDs, creator, labels, weights and timestamps exactly."
    ),
    (
        "Verify every changed row and incident-edge inventory equals before state, then commit. "
        "Keep the original run/report as immutable audit evidence; return run ID, restored counts and any conflicts. "
        "Roll back every write if verification fails."
    ),
]


def json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def snapshot(cur) -> dict:
    result = {}
    for table, key in (("kg_nodes", "node_id"), ("kg_edges", "edge_id")):
        cur.execute(f"SELECT * FROM {table} ORDER BY {key}")
        result[table] = {row[key]: json_value(dict(row)) for row in cur.fetchall()}
    return result


def change_set(before: dict, after: dict, affected_ids: set[str]) -> tuple[dict, dict]:
    changes = {}
    for table in ("kg_nodes", "kg_edges"):
        old, new = before[table], after[table]
        changes[table] = [
            {"id": key, "before": old.get(key), "after": new.get(key)}
            for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)
        ]
    incident = {}
    for node_id in sorted(affected_ids):
        incident[node_id] = {
            state: [edge for edge in graph["kg_edges"].values()
                    if node_id in (edge["source_id"], edge["target_id"])]
            for state, graph in (("before", before), ("after", after))
        }
    return changes, incident


def seal_report(report: dict) -> dict:
    report = json_value(report)
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    report["integrity_sha256"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return report


def persist_report(cur, run_id: int, report: dict) -> None:
    cur.execute(
        "INSERT INTO kg_maintenance_reports (run_id, report) VALUES (%s, %s)",
        (run_id, json.dumps(seal_report(report))),
    )
