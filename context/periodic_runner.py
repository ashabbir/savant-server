"""
Periodic Repository Sync Runner — Runs every 2 hours in the background.

For all registered projects:
1. Fetches latest code from GitHub/GitLab (or origin remote).
2. Runs differential semantic indexing if code changed or if project is un-indexed.
3. Runs structural CodeGraph generation/sync if code changed or if graph is stale.
4. Logs actions to logger and persists sync execution history in ctx_repo_sync_logs.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

logger = logging.getLogger(__name__)


def _record_sync_activity(context_db, **fields):
    """Keep scheduler progress independent from audit persistence availability."""
    try:
        return context_db.record_repo_sync_log(**fields)
    except Exception:
        logger.exception(
            "Failed to persist scheduled repository sync activity for %s",
            fields.get("repo_name", "unknown"),
        )
        return {}

# Default 2-hour interval in seconds
DEFAULT_SYNC_INTERVAL_SECONDS = 2 * 3600

_runner_thread: threading.Thread | None = None
_runner_lock = threading.Lock()
_stop_event = threading.Event()
_runner_status = {
    "running": False,
    "last_run_at": None,
    "next_run_at": None,
    "last_run_summary": {},
}


def get_runner_status() -> dict:
    with _runner_lock:
        return dict(_runner_status)


def get_sync_interval_seconds() -> float:
    interval_hours = float(
        os.environ.get(
            "PERIODIC_SYNC_INTERVAL_HOURS",
            str(DEFAULT_SYNC_INTERVAL_SECONDS / 3600),
        )
    )
    return max(60.0, interval_hours * 3600.0)


def start_periodic_runner():
    """Start the 2-hour periodic sync runner thread."""
    global _runner_thread
    with _runner_lock:
        if _runner_status["running"]:
            return
        _stop_event.clear()
        _runner_status["running"] = True
        _runner_thread = threading.Thread(
            target=_periodic_sync_loop, daemon=True, name="periodic-sync-runner"
        )
        _runner_thread.start()
        logger.info("Periodic 2-hour repo sync runner started")


def stop_periodic_runner():
    """Stop the periodic sync runner thread."""
    global _runner_thread
    with _runner_lock:
        _stop_event.set()
        _runner_status["running"] = False
        logger.info("Stopping periodic sync runner")


def run_periodic_sync_now(actor_id: str = "user", source_app: str = "savant-olympus", target_repo: str | None = None) -> dict:
    """Manually trigger a sync pass for all projects (or a specific project) immediately."""
    logger.info("Manual trigger of periodic sync runner (run all)")
    return _execute_sync_pass_for_all_repos(
        trigger="manual", actor_id=actor_id, source_app=source_app, target_repo=target_repo
    )


def _periodic_sync_loop():
    """Background loop running every 2 hours (configurable via PERIODIC_SYNC_INTERVAL_HOURS)."""
    # Warm-up delay on startup so Flask DB init finishes
    time.sleep(15)

    interval_seconds = get_sync_interval_seconds()

    while not _stop_event.is_set():
        now = datetime.now(timezone.utc)
        next_time = datetime.fromtimestamp(now.timestamp() + interval_seconds, tz=timezone.utc)
        with _runner_lock:
            _runner_status["last_run_at"] = now.isoformat()
            _runner_status["next_run_at"] = next_time.isoformat()

        try:
            summary = _execute_sync_pass_for_all_repos()
            with _runner_lock:
                _runner_status["last_run_summary"] = summary
        except Exception as exc:
            logger.error(f"Error during periodic 2-hour repo sync pass: {exc}")

        # Sleep in 5-second intervals to allow responsive shutdown
        elapsed = 0.0
        while elapsed < interval_seconds and not _stop_event.is_set():
            time.sleep(5)
            elapsed += 5.0


def _execute_sync_pass_for_all_repos(
    trigger: str = "scheduled", actor_id: str = "system",
    source_app: str = "savant-server", target_repo: str | None = None,
) -> dict:
    """Enqueue one self-contained differential pipeline for each eligible project."""
    from context.db import ContextDB
    from db.jobs import JobDB

    try:
        repos = ContextDB.list_repos()
        if target_repo:
            repos = [r for r in repos if r.get("name") == target_repo]
    except Exception as exc:
        logger.error(f"Failed to list repos for periodic sync: {exc}")
        return {"error": str(exc), "count": 0}

    logger.info(f"Starting periodic sync pass (Run All) for {len(repos)} registered projects")
    results = []

    for repo in repos:
        sync_started_at = perf_counter()
        repo_name = repo.get("name")
        repo_path_str = repo.get("path", "")
        repo_path = Path(repo_path_str)

        if not repo_name or not repo_path.exists():
            logger.warning(f"Skipping periodic sync for invalid/missing project path: {repo_name} ({repo_path_str})")
            continue

        details = []
        try:
            active_job = JobDB.find_active_types(
                ["differential_sync", "ast", "lst", "codegraph_sync", "codegraph_index", "index", "reindex", "initial_repo_sync"],
                repo_name,
            )
            if active_job:
                details.append(f"Job already in progress ({active_job['job_type']}: {active_job['id']})")
                summary_status = "skipped"
            else:
                is_git = (repo_path / ".git").is_dir()
                if is_git:
                    j_diff = JobDB.create_job("differential_sync", repo_name, payload={
                        "trigger": trigger,
                        "actor_id": actor_id,
                        "source_app": source_app,
                    })
                    details.append(f"Enqueued differential sync pipeline: {j_diff['id']}")
                    summary_status = "success"
                else:
                    is_indexed = (repo.get("status") in {"indexed", "ast_only"}) or bool(repo.get("indexed_at"))
                    if not is_indexed:
                        j_idx = JobDB.create_job("index", repo_name, payload={
                            "trigger": trigger,
                            "actor_id": actor_id,
                            "source_app": source_app,
                        })
                        details.append(f"Enqueued initial index: {j_idx['id']}")
                        summary_status = "success"
                    else:
                        details.append("Non-git project already indexed; skipped")
                        summary_status = "skipped"

            log_detail_str = "; ".join(details)
            logger.info(f"Periodic sync [{repo_name}]: {summary_status} — {log_detail_str}")

            _record_sync_activity(ContextDB,
                repo_name=repo_name,
                operation="periodic_refresh",
                trigger=trigger,
                actor_id=actor_id,
                source_app=source_app,
                status=summary_status,
                duration_ms=int((perf_counter() - sync_started_at) * 1000),
                details=log_detail_str,
            )

            results.append({
                "repo_name": repo_name,
                "status": summary_status,
                "details": log_detail_str,
            })

        except Exception as exc:
            err_msg = f"Periodic sync error for {repo_name}: {exc}"
            logger.error(err_msg)
            _record_sync_activity(ContextDB,
                repo_name=repo_name,
                operation="periodic_refresh",
                trigger=trigger,
                actor_id=actor_id,
                source_app=source_app,
                status="failed",
                duration_ms=int((perf_counter() - sync_started_at) * 1000),
                error=str(exc),
                details=str(exc),
            )
            results.append({"repo_name": repo_name, "status": "failed", "error": str(exc)})

    return {"count": len(results), "timestamp": datetime.now(timezone.utc).isoformat(), "results": results}


def run_forever():
    """Run dedicated periodic sync process."""
    logger.info("Dedicated periodic sync runner process starting")
    start_periodic_runner()
    while True:
        time.sleep(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
