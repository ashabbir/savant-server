"""Transactional, scheduled maintenance for the institutional knowledge graph."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from db.base import _now
from postgres_client import get_connection, release_connection

logger = logging.getLogger(__name__)

MAINTENANCE_LOCK_ID = 0x4B474D  # "KGM"; transaction-scoped and shared by every container.
DEFAULT_BATCH_SIZE = 500
_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()
_manual_run_lock = threading.Lock()
_status: dict[str, Any] = {"running": False, "last_run": None, "next_run_at": None}


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


def _metadata(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _merge_metadata(canonical: dict, staged: dict, *, now: str) -> dict:
    result = _metadata(canonical.get("metadata"))
    incoming = _metadata(staged.get("metadata"))
    workspaces = set(result.get("workspaces") or []) | set(incoming.get("workspaces") or [])
    if workspaces:
        result["workspaces"] = sorted(workspaces)
    for key, value in incoming.items():
        if key not in result and value not in (None, "", [], {}):
            result[key] = value
    result["last_consolidated_at"] = now
    return result


def _rewire_edges(cur, survivor_id: str, absorbed_id: str) -> None:
    """Move edges to survivor, retaining the strongest duplicate edge."""
    cur.execute(
        "SELECT edge_id, source_id, target_id, edge_type, weight FROM kg_edges WHERE source_id = %s OR target_id = %s",
        (absorbed_id, absorbed_id),
    )
    for edge in cur.fetchall():
        external_id = edge["target_id"] if edge["source_id"] == absorbed_id else edge["source_id"]
        if external_id == survivor_id:
            cur.execute("DELETE FROM kg_edges WHERE edge_id = %s", (edge["edge_id"],))
            continue
        source_id = survivor_id if edge["source_id"] == absorbed_id else external_id
        target_id = external_id if edge["source_id"] == absorbed_id else survivor_id
        cur.execute(
            "SELECT edge_id, weight FROM kg_edges WHERE source_id = %s AND target_id = %s AND edge_type = %s",
            (source_id, target_id, edge["edge_type"]),
        )
        existing = cur.fetchone()
        if existing:
            if edge["weight"] > existing["weight"]:
                cur.execute("UPDATE kg_edges SET weight = %s WHERE edge_id = %s", (edge["weight"], existing["edge_id"]))
            cur.execute("DELETE FROM kg_edges WHERE edge_id = %s", (edge["edge_id"],))
        else:
            column = "source_id" if edge["source_id"] == absorbed_id else "target_id"
            cur.execute(f"UPDATE kg_edges SET {column} = %s WHERE edge_id = %s", (survivor_id, edge["edge_id"]))


def _absorb(cur, canonical: dict, duplicate: dict, *, now: str, contradiction: bool = False) -> None:
    metadata = _merge_metadata(canonical, duplicate, now=now)
    contents = [canonical.get("content", "").strip(), duplicate.get("content", "").strip()]
    content = "\n\n---\n\n".join(dict.fromkeys(item for item in contents if item))
    if contradiction:
        metadata["superseded_at"] = now
        metadata["superseded_by"] = duplicate["node_id"]
        # Newer staged content wins for explicit contradictions.
        content = duplicate.get("content", "") or canonical.get("content", "")
    cur.execute(
        "UPDATE kg_nodes SET content = %s, metadata = %s, status = 'committed', updated_at = %s WHERE node_id = %s",
        (content, json.dumps(metadata), now, canonical["node_id"]),
    )
    _rewire_edges(cur, canonical["node_id"], duplicate["node_id"])
    cur.execute("DELETE FROM kg_nodes WHERE node_id = %s", (duplicate["node_id"],))


def _create_run(cur, trigger: str) -> int:
    cur.execute("INSERT INTO kg_maintenance_runs (trigger, status) VALUES (%s, 'running') RETURNING id", (trigger,))
    return cur.fetchone()["id"]


def _finish_run(cur, run_id: int, status: str, summary: dict, error: str = "") -> None:
    cur.execute(
        """UPDATE kg_maintenance_runs SET status = %s, finished_at = CURRENT_TIMESTAMP,
           nodes_promoted = %s, duplicates_merged = %s, contradictions_resolved = %s,
           nodes_expired = %s, edges_pruned = %s, nodes_pruned = %s, clusters_assigned = %s,
           error = %s, details = %s WHERE id = %s""",
        (status, summary["nodes_promoted"], summary["duplicates_merged"], summary["contradictions_resolved"],
         summary["nodes_expired"], summary["edges_pruned"], summary["nodes_pruned"], summary["clusters_assigned"],
         error, json.dumps(summary), run_id),
    )


def _process_staged_node(cur, staged: dict, summary: dict, *, now: str) -> None:
    meta = _metadata(staged["metadata"])
    supersedes = str(meta.get("supersedes_node_id") or meta.get("contradicts_node_id") or "")
    canonical = None
    contradiction = False
    if supersedes:
        cur.execute("SELECT * FROM kg_nodes WHERE node_id = %s AND status = 'committed' FOR UPDATE", (supersedes,))
        canonical = cur.fetchone()
        contradiction = canonical is not None
    if canonical is None:
        cur.execute(
            """SELECT * FROM kg_nodes WHERE status = 'committed' AND node_type = %s
               AND lower(btrim(title)) = %s ORDER BY updated_at DESC LIMIT 1 FOR UPDATE""",
            (staged["node_type"], _normalise(staged["title"])),
        )
        canonical = cur.fetchone()
    if canonical:
        _absorb(cur, canonical, staged, now=now, contradiction=contradiction)
        summary["duplicates_merged"] += 1
        summary["contradictions_resolved"] += int(contradiction)
        return
    meta["cluster"] = meta.get("cluster") or f"{staged['node_type']}-knowledge"
    meta["promoted_at"] = now
    cur.execute(
        "UPDATE kg_nodes SET status = 'committed', metadata = %s, updated_at = %s WHERE node_id = %s",
        (json.dumps(meta), now, staged["node_id"]),
    )
    summary["nodes_promoted"] += 1
    summary["clusters_assigned"] += 1


def run_maintenance_now(trigger: str = "manual") -> dict:
    """Run one atomic maintenance pass. A busy peer returns a no-op, never overlaps."""
    started = perf_counter()
    summary = {key: 0 for key in (
        "nodes_promoted", "duplicates_merged", "contradictions_resolved", "nodes_expired",
        "edges_pruned", "nodes_pruned", "clusters_assigned",
    )}
    summary["trigger"] = trigger
    conn = get_connection()
    run_id: int | None = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked", (MAINTENANCE_LOCK_ID,))
            if not cur.fetchone()["locked"]:
                conn.rollback()
                return {"status": "skipped", "reason": "already_running", **summary}
            run_id = _create_run(cur, trigger)
            now = _now()
            batch_size = max(1, min(int(os.environ.get("KG_MAINTENANCE_BATCH_SIZE", DEFAULT_BATCH_SIZE)), 5000))
            while True:
                cur.execute(
                    """SELECT * FROM kg_nodes WHERE status = 'staged'
                       ORDER BY created_at ASC LIMIT %s FOR UPDATE SKIP LOCKED""",
                    (batch_size,),
                )
                staged_nodes = list(cur.fetchall())
                if not staged_nodes:
                    break
                for staged in staged_nodes:
                    _process_staged_node(cur, staged, summary, now=now)

            # Canonicalise pre-existing committed exact-name duplicates as well.
            cur.execute(
                """SELECT node_type, lower(btrim(title)) AS normalised_title, array_agg(node_id ORDER BY created_at ASC) AS ids
                   FROM kg_nodes WHERE status = 'committed' GROUP BY node_type, lower(btrim(title)) HAVING count(*) > 1"""
            )
            for group in cur.fetchall():
                ids = group["ids"]
                cur.execute("SELECT * FROM kg_nodes WHERE node_id = ANY(%s) ORDER BY created_at ASC FOR UPDATE", (ids,))
                nodes = list(cur.fetchall())
                if not nodes:
                    continue
                for duplicate in nodes[1:]:
                    _absorb(cur, nodes[0], duplicate, now=now)
                    summary["duplicates_merged"] += 1

            # Time-bound entries decay without destructive loss; a later review can restore them.
            cur.execute(
                """UPDATE kg_nodes SET metadata = jsonb_set(metadata::jsonb, '{is_active}', 'false'::jsonb)::text,
                    updated_at = %s WHERE status = 'committed' AND COALESCE((metadata::jsonb ->> 'expires_at')::timestamptz,
                    'infinity'::timestamptz) <= CURRENT_TIMESTAMP AND COALESCE(metadata::jsonb ->> 'is_active', 'true') <> 'false'""",
                (now,),
            )
            summary["nodes_expired"] = cur.rowcount
            cur.execute("DELETE FROM kg_edges WHERE source_id = target_id")
            summary["edges_pruned"] = cur.rowcount
            _finish_run(cur, run_id, "success", summary)
        conn.commit()
        summary.update({"status": "success", "run_id": run_id, "duration_ms": round((perf_counter() - started) * 1000)})
        return summary
    except Exception as exc:
        conn.rollback()
        logger.exception("Knowledge graph maintenance failed")
        failure = {**summary, "status": "failed", "duration_ms": round((perf_counter() - started) * 1000)}
        # Persist a failed audit separately because the graph transaction deliberately rolled back.
        try:
            with conn.cursor() as cur:
                if run_id is None:
                    run_id = _create_run(cur, trigger)
                _finish_run(cur, run_id, "failed", summary, str(exc))
            conn.commit()
            failure["run_id"] = run_id
        except Exception:
            conn.rollback()
        failure["error"] = str(exc)
        return failure
    finally:
        release_connection(conn)


def get_maintenance_status() -> dict:
    with _scheduler_lock:
        return dict(_status)


def _scheduled_run() -> None:
    with _scheduler_lock:
        _status["running"] = True
    try:
        result = run_maintenance_now("scheduled")
        with _scheduler_lock:
            _status["last_run"] = result
    finally:
        with _scheduler_lock:
            _status["running"] = False


def start_maintenance_scheduler() -> None:
    """Start a single UTC cron scheduler; APScheduler never blocks Flask/SSE threads."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler and _scheduler.running:
            return
        _scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
        _scheduler.add_job(_scheduled_run, CronTrigger(hour="*/4", minute=0), id="kg-maintenance", replace_existing=True,
                           max_instances=1, coalesce=True, misfire_grace_time=3600)
        _scheduler.start()
        job = _scheduler.get_job("kg-maintenance")
        _status["next_run_at"] = job.next_run_time.isoformat() if job and job.next_run_time else None
        logger.info("Knowledge graph maintenance scheduler started (UTC cron: 0 */4 * * *)")


def stop_maintenance_scheduler() -> None:
    global _scheduler
    with _scheduler_lock:
        if _scheduler:
            _scheduler.shutdown(wait=False)
            _scheduler = None
        _status["next_run_at"] = None


def trigger_maintenance_async() -> dict:
    """Queue a manual run without making the authenticated HTTP request wait."""
    if not _manual_run_lock.acquire(blocking=False):
        return {"accepted": False, "reason": "already_running"}

    def execute() -> None:
        try:
            with _scheduler_lock:
                _status["running"] = True
            result = run_maintenance_now("manual")
            with _scheduler_lock:
                _status["last_run"] = result
        finally:
            with _scheduler_lock:
                _status["running"] = False
            _manual_run_lock.release()

    threading.Thread(target=execute, daemon=True, name="kg-maintenance-manual").start()
    return {"accepted": True, "status": "queued"}
