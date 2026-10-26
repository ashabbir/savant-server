"""Transactional, scheduled maintenance for the institutional knowledge graph."""

from __future__ import annotations

import json
import logging
import os
import threading
from time import perf_counter
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from db.base import _now
from postgres_client import get_connection, release_connection

logger = logging.getLogger(__name__)

MAINTENANCE_LOCK_ID = 0x4B474D  # "KGM"; transaction-scoped and shared by every container.
_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()
CONTEMPLATE_JOB_TYPE = "contemplate"
CONTEMPLATE_TARGET = "knowledge-graph"
_status: dict[str, Any] = {"last_enqueued": None, "next_run_at": None}


def _metadata(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _merge_metadata(canonical: dict, staged: dict, *, now: str) -> dict:
    from knowledge.contemplate_matching import workspaces as metadata_workspaces
    result = _metadata(canonical.get("metadata"))
    incoming = _metadata(staged.get("metadata"))
    workspaces = metadata_workspaces(result) | metadata_workspaces(incoming)
    if workspaces:
        result["workspaces"] = sorted(workspaces)
    for key, value in incoming.items():
        if key not in {"supersedes_node_id", "contradicts_node_id"} and key not in result and value not in (None, "", [], {}):
            result[key] = value
    result["last_consolidated_at"] = now
    sources = list(result.get("consolidation_sources") or [])
    sources.append({"node_id": staged["node_id"], "title": staged["title"], "created_by": staged.get("created_by", ""),
                    "metadata": incoming})
    result["consolidation_sources"] = sources
    return result


def _rewire_edges(cur, survivor_id: str, absorbed_id: str) -> None:
    """Move edges to survivor, retaining the strongest duplicate edge."""
    cur.execute(
        "SELECT edge_id, source_id, target_id, edge_type, weight, label FROM kg_edges WHERE source_id = %s OR target_id = %s",
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
            "SELECT edge_id, weight, label FROM kg_edges WHERE source_id = %s AND target_id = %s AND edge_type = %s",
            (source_id, target_id, edge["edge_type"]),
        )
        existing = cur.fetchone()
        if existing:
            labels = "\n".join(dict.fromkeys(label for label in (existing["label"], edge["label"]) if label))
            weights = [weight for weight in (edge["weight"], existing["weight"]) if weight is not None]
            cur.execute("UPDATE kg_edges SET weight = %s, label = %s WHERE edge_id = %s",
                        (max(weights) if weights else None, labels, existing["edge_id"]))
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


def _summary(trigger: str) -> dict:
    return {**{key: 0 for key in (
        "nodes_promoted", "duplicates_merged", "contradictions_resolved", "nodes_expired",
        "edges_pruned", "nodes_pruned", "clusters_assigned",
    )}, "trigger": trigger, "affected_node_count": 0, "affected_node_ids": [],
        "affected_node_limit": 100, "limit_reached": False, "deferred_operations": 0}


def run_contemplate_now(trigger: str = "manual", job_id: str | None = None) -> dict:
    """Run bounded cosine reconciliation and atomically persist its undo report."""
    from knowledge.contemplate_engine import Contemplation
    from knowledge.contemplate_matching import SimilarityMatcher
    from knowledge.contemplate_report import (
        RESTORE_INSTRUCTIONS, change_set, json_value, persist_report, snapshot,
    )
    started = perf_counter()
    summary = _summary(trigger)
    conn = get_connection()
    run_id = None
    locked = False
    matcher = SimilarityMatcher()
    report = {"schema_version": 1, "job_id": job_id, "trigger": trigger,
              "started_at": _now(), "configuration": {"affected_node_limit": 100,
              "requested_similarity_threshold": os.getenv("KG_CONTEMPLATE_SIMILARITY_THRESHOLD", "0.70"),
              "embedding_model": matcher.model_identity},
              "restore_instructions": RESTORE_INSTRUCTIONS,
              "changes": {"kg_nodes": [], "kg_edges": []}, "incident_edges": {}, "actions": []}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database() AS database, inet_server_addr()::text AS server_address, inet_server_port() AS server_port")
            report["target_database"] = dict(cur.fetchone())
            run_id = _create_run(cur, trigger)
            cur.execute("UPDATE kg_maintenance_runs SET job_id = %s WHERE id = %s", (job_id, run_id))
        conn.commit()
        report["run_id"] = run_id
        with conn.cursor() as cur:
            # Session lock covers preparation as well as the mutation transaction.
            cur.execute("SELECT pg_try_advisory_lock(%s) AS locked", (MAINTENANCE_LOCK_ID,))
            locked = cur.fetchone()["locked"]
        conn.commit()
        if not locked:
            summary.update(status="skipped", reason="already_running")
        else:
            threshold = float(os.getenv("KG_CONTEMPLATE_SIMILARITY_THRESHOLD", "0.70"))
            if not 0 <= threshold <= 1:
                raise ValueError("KG_CONTEMPLATE_SIMILARITY_THRESHOLD must be in [0, 1]")
            report["configuration"]["similarity_threshold"] = threshold
            # Holding the maintenance lock proves an earlier abandoned graph transaction is over.
            with conn.cursor() as cur:
                cur.execute("""SELECT r.* FROM kg_maintenance_runs r LEFT JOIN jobs j ON j.id = r.job_id
                    WHERE r.status = 'running' AND r.id < %s
                    AND (r.job_id IS NULL OR j.id IS NULL OR j.status NOT IN ('queued', 'running'))""", (run_id,))
                abandoned = list(cur.fetchall())
                for old in abandoned:
                    failure = _summary(old["trigger"])
                    failure.update(status="failed", reason="interrupted", run_id=old["id"])
                    _finish_run(cur, old["id"], "failed", failure, "Interrupted before graph/report commit")
                    persist_report(cur, old["id"], {
                        **report, "run_id": old["id"], "job_id": old["job_id"], "trigger": old["trigger"],
                        "started_at": json_value(old["started_at"]), "finished_at": _now(),
                        "status": "failed", "summary": failure, "error": "Interrupted before graph/report commit",
                    })
                cur.execute("SELECT * FROM kg_nodes ORDER BY created_at, node_id")
                initial_nodes = list(cur.fetchall())
            conn.commit()
            matcher.prepare(initial_nodes)
            with conn.cursor() as cur:
                cur.execute("LOCK TABLE kg_nodes, kg_edges IN SHARE ROW EXCLUSIVE MODE")
                before = snapshot(cur)
                report["configuration"]["domain_policies"] = {
                    key: {"title": row["title"], "threshold": _metadata(row.get("metadata")).get("contemplate_similarity_threshold")}
                    for key, row in before["kg_nodes"].items() if row["node_type"] == "domain"
                }
                engine = Contemplation(cur, matcher, summary, before, _now(), threshold)
                engine.reconcile()
                after = snapshot(cur)
                report["changes"], report["incident_edges"] = change_set(before, after, engine.affected)
                report["actions"] = engine.actions
                report["configuration"].update(seed_ids=engine.seed_ids, seed_limit=200, nearest_candidates=20,
                                                domain_threshold_field="contemplate_similarity_threshold")
                summary["status"] = "success"
                report.update(status="success", finished_at=_now(), summary=dict(summary))
                _finish_run(cur, run_id, "success", summary)
                persist_report(cur, run_id, report)
            conn.commit()
        if not locked:
            with conn.cursor() as cur:
                report.update(status="skipped", finished_at=_now(), summary=dict(summary))
                _finish_run(cur, run_id, "skipped", summary)
                persist_report(cur, run_id, report)
            conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.exception("Knowledge graph contemplation failed")
        attempted = dict(summary)
        summary = {**_summary(trigger), "status": "failed", "error": str(exc)}
        if run_id is not None:
            report.update(status="failed", finished_at=_now(), summary=dict(summary),
                          attempted_summary=attempted, error=str(exc),
                          changes={"kg_nodes": [], "kg_edges": []}, incident_edges={}, actions=[])
            try:
                with conn.cursor() as cur:
                    _finish_run(cur, run_id, "failed", summary, str(exc))
                    persist_report(cur, run_id, report)
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("Could not persist failed contemplation report")
        try:
            from db.notifications import NotificationDB
            NotificationDB.notify(message=f"Knowledge graph maintenance failed: {str(exc)[:300]}",
                                  event_type="kg_maintenance_failed", level="warning",
                                  detail={"error": str(exc), "trigger": trigger, "run_id": run_id})
        except Exception:
            logger.warning("Could not notify about contemplation failure", exc_info=True)
    finally:
        if locked:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (MAINTENANCE_LOCK_ID,))
                conn.commit()
            except Exception:
                # Never return a session holding an advisory lock to the pool.
                conn.close()
                logger.exception("Could not release contemplation advisory lock")
        release_connection(conn)
    return {**summary, "run_id": run_id, "duration_ms": round((perf_counter() - started) * 1000)}


def get_maintenance_status() -> dict:
    with _scheduler_lock:
        return dict(_status)


def queue_contemplate(trigger: str = "manual") -> dict:
    """Durably queue one contemplation job, without overlapping an active pass."""
    from db.jobs import JobDB

    existing = JobDB.find_active(CONTEMPLATE_JOB_TYPE, CONTEMPLATE_TARGET)
    if existing:
        return {"accepted": True, "reused": True, "job": existing}
    job = JobDB.create_job(
        CONTEMPLATE_JOB_TYPE,
        CONTEMPLATE_TARGET,
        payload={"trigger": trigger, "source_app": "savant-server"},
    )
    with _scheduler_lock:
        _status["last_enqueued"] = job
    return {"accepted": True, "reused": False, "job": job}


def _scheduled_run() -> None:
    queue_contemplate("scheduled")


def start_maintenance_scheduler() -> None:
    """Queue a contemplation job every four hours; execution belongs to the worker."""
    if os.getenv("SAVANT_WORKER_SCHEDULER_ENABLED", "1") == "0":
        return
    global _scheduler
    with _scheduler_lock:
        if _scheduler and _scheduler.running:
            return
        _scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
        _scheduler.add_job(_scheduled_run, CronTrigger(hour="*/4", minute=0), id="contemplate", replace_existing=True,
                           max_instances=1, coalesce=True, misfire_grace_time=3600)
        _scheduler.start()
        job = _scheduler.get_job("contemplate")
        _status["next_run_at"] = job.next_run_time.isoformat() if job and job.next_run_time else None
        logger.info("Knowledge graph contemplation scheduler started (UTC cron: 0 */4 * * *)")


def stop_maintenance_scheduler() -> None:
    global _scheduler
    with _scheduler_lock:
        if _scheduler:
            _scheduler.shutdown(wait=False)
            _scheduler = None
        _status["next_run_at"] = None


def trigger_maintenance_async() -> dict:
    """Compatibility name for manually queueing the durable contemplation job."""
    return queue_contemplate("manual")


# Kept for API callers while the observable queued job is named ``contemplate``.
def run_maintenance_now(trigger: str = "manual") -> dict:
    return run_contemplate_now(trigger)
