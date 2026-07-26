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


def run_periodic_sync_now(actor_id: str = "user", source_app: str = "savant-olympus") -> dict:
    """Manually trigger a sync pass for all projects immediately."""
    logger.info("Manual trigger of periodic 2-hour sync runner for all projects")
    return _execute_sync_pass_for_all_repos(
        trigger="manual", actor_id=actor_id, source_app=source_app
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
    source_app: str = "savant-server",
) -> dict:
    """Iterate over all registered projects and perform sync (fetch + index + graph)."""
    from context.db import ContextDB
    from context.ingestion import IngestionError, refresh_repo, inspect_project_source
    from context.indexer import Indexer
    from db.code_intelligence import CodeIntelligenceConfigDB

    try:
        repos = ContextDB.list_repos()
    except Exception as exc:
        logger.error(f"Failed to list repos for periodic sync: {exc}")
        return {"error": str(exc), "count": 0}

    logger.info(f"Starting 2-hour periodic sync pass for {len(repos)} registered projects")
    results = []

    for repo in repos:
        sync_started_at = perf_counter()
        repo_name = repo.get("name")
        repo_id = repo.get("id")
        repo_path_str = repo.get("path", "")
        repo_path = Path(repo_path_str)

        if not repo_name or not repo_path.exists():
            logger.warning(f"Skipping periodic sync for invalid/missing project path: {repo_name} ({repo_path_str})")
            continue

        fetched = False
        code_changed = False
        indexed = False
        graphed = False
        details = []
        activity_errors = []
        refreshed = None

        try:
            # 1. Fetch latest code if Git repo
            if (repo_path / ".git").is_dir():
                try:
                    refreshed = refresh_repo(repo_path_str)
                    fetched = True
                    code_changed = getattr(refreshed, "changed", False)
                    details.append(f"Fetched origin (code_changed={code_changed})")
                    ContextDB.mark_repo_fetched(repo_name)
                except IngestionError as exc:
                    details.append(f"Fetch skipped/failed: {exc}")
                    activity_errors.append(f"fetch: {exc}")

            # 2. Index if needed (code changed OR un-indexed)
            is_unindexed = (repo.get("status") in {"added", "error", None}) or (repo.get("file_count", 0) == 0)
            should_index = code_changed or is_unindexed

            if should_index:
                indexer = Indexer()
                clear_flag = is_unindexed
                idx_res = indexer.index_repository(repo_path, repo_name=repo_name, clear=clear_flag, differential=not clear_flag)
                indexed = True
                details.append(f"Indexed (clear={clear_flag}, indexed={idx_res.get('files_indexed',0)}, skipped={idx_res.get('files_skipped',0)}, removed={idx_res.get('files_removed',0)})")

            # 3. CodeGraph generation if needed (code changed OR graph stale/uninitialized)
            config = CodeIntelligenceConfigDB.get(repo_name) or CodeIntelligenceConfigDB.get(str(repo_id))
            graph_freshness = config.get("freshness") if config else None
            is_graph_stale = graph_freshness in {"stale", "pending_sync", None} or not config
            should_graph = code_changed or is_graph_stale

            if should_graph:
                try:
                    from code_intelligence.runtime import build_service
                    ci_res = build_service().ensure_index(str(repo_id), repo_path, mode="create_or_sync")
                    graphed = True
                    details.append(f"CodeGraph synced (freshness={getattr(ci_res, 'freshness', 'ok')})")
                    health = build_service().health(str(repo_id), repo_path)
                    CodeIntelligenceConfigDB.upsert(
                        str(repo_id),
                        provider=health.provider,
                        graph_version=health.graph_version,
                        last_indexed_at=health.indexed_at,
                        last_synced_at=health.indexed_at,
                        freshness=health.freshness.value,
                        last_error_code=None,
                    )
                except Exception as exc:
                    details.append(f"CodeGraph sync failed: {exc}")
                    activity_errors.append(f"codegraph: {exc}")

            made_progress = fetched or indexed or graphed
            if activity_errors:
                summary_status = "partial" if made_progress else "failed"
            else:
                summary_status = "success" if made_progress else "skipped"
            log_detail_str = "; ".join(details) if details else "No updates needed"
            logger.info(f"Periodic sync [{repo_name}]: {summary_status} — {log_detail_str}")
            from context.activity import collect_git_change_details
            git_details = collect_git_change_details(
                repo_path,
                getattr(refreshed, "before_commit", "") if refreshed else "",
                getattr(refreshed, "after_commit", "") if refreshed else "",
            )

            _record_sync_activity(ContextDB,
                repo_name=repo_name,
                operation="periodic_refresh",
                trigger=trigger,
                actor_id=actor_id,
                source_app=source_app,
                provider=getattr(refreshed, "provider", "") if refreshed else "",
                branch=getattr(refreshed, "branch", "") if refreshed else "",
                status=summary_status,
                before_commit=getattr(refreshed, "before_commit", "") if refreshed else "",
                after_commit=getattr(refreshed, "after_commit", "") if refreshed else "",
                fetched=fetched,
                code_changed=code_changed,
                indexed=indexed,
                graphed=graphed,
                duration_ms=int((perf_counter() - sync_started_at) * 1000),
                error="; ".join(activity_errors),
                details=log_detail_str,
                **git_details,
            )

            results.append({
                "repo_name": repo_name,
                "status": summary_status,
                "fetched": fetched,
                "code_changed": code_changed,
                "indexed": indexed,
                "graphed": graphed,
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
