"""
Job Worker — Single-threaded FIFO job processor.

Polls the jobs table every 2 seconds for the oldest queued job and processes it.
Only one job runs at a time. Progress is written to the DB so it survives restarts.
"""

import logging
import threading
import time
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)

_worker_thread: threading.Thread | None = None
_worker_started = False


def start_worker():
    """Start the background job worker (call once on app boot)."""
    global _worker_thread, _worker_started
    if _worker_started:
        return
    _worker_started = True
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="job-worker")
    _worker_thread.start()
    logger.info("Job worker thread started")


def _worker_loop():
    """Main worker loop — poll for queued jobs and process them."""
    # Delay initial start to let Flask finish booting
    time.sleep(3)

    while True:
        try:
            _process_next_job()
        except Exception as e:
            logger.error(f"Job worker error: {e}")
        time.sleep(2)


def run_forever():
    """Run the persistent queue worker in a dedicated process."""
    logger.info("Dedicated job worker starting")
    _worker_loop()


def _process_next_job():
    """Pick the next queued job and execute it."""
    from db.jobs import JobDB

    job = JobDB.next_queued()
    if not job:
        return

    job_id = job["id"]
    job_type = job["job_type"]
    target = job["target"]

    logger.info(f"Processing job {job_id}: {job_type} → {target}")
    # next_queued() already claimed this row atomically.

    try:
        result = _execute_job(job_id, job_type, target)
        JobDB.set_done(job_id, result)
        logger.info(f"Job {job_id} completed: {job_type} → {target}")
    except _CancelledError:
        from db.jobs import JobDB as JDB
        JDB.set_cancelled(job_id)
        logger.info(f"Job {job_id} cancelled: {job_type} → {target}")
    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}")
        JobDB.set_failed(job_id, str(e)[:2000])


class _CancelledError(Exception):
    pass


def _make_progress_callback(job_id: str):
    """Create a progress callback that writes to the DB and checks cancellation."""
    from db.jobs import JobDB

    def callback(progress: int, phase: str = "", message: str = ""):
        # Check cancellation
        if JobDB.is_cancel_requested(job_id):
            raise _CancelledError(f"Job {job_id} cancelled by user")
        JobDB.update_progress(job_id, progress, phase, message)

    return callback


def _execute_job(job_id: str, job_type: str, target: str) -> dict:
    """Dispatch job to the appropriate handler."""
    from db.jobs import JobDB

    progress_cb = _make_progress_callback(job_id)

    if job_type == "index":
        return _run_index(target, progress_cb, clear=True)
    elif job_type == "reindex":
        return _run_index(target, progress_cb, clear=True)
    elif job_type == "ast":
        return _run_ast(target, progress_cb, clear=True)
    elif job_type == "index-all":
        return _run_batch_index(progress_cb)
    elif job_type == "ast-all":
        return _run_batch_ast(progress_cb)
    elif job_type in ("codegraph_index", "codegraph_sync"):
        return _run_code_intelligence_sync(target, progress_cb)
    else:
        raise ValueError(f"Unknown job type: {job_type}")


def _run_code_intelligence_sync(target: str, progress_cb) -> dict:
    """Run structural create/sync without changing semantic repository status."""
    from code_intelligence.runtime import build_service
    from db.code_intelligence import CodeIntelligenceConfigDB

    repo_path, repo_name = _resolve_repo(target)
    # Preserve the stable repository identifier used by the caller. Converting
    # numeric IDs to a display name here creates a second bridge registration
    # and splits watcher/freshness state for the same repository.
    provider_repo_id = str(target)
    progress_cb(5, "Preparing", "Resolving structural provider")
    CodeIntelligenceConfigDB.upsert(provider_repo_id, freshness="pending_sync", last_error_code=None)
    try:
        result = build_service().ensure_index(provider_repo_id, repo_path, mode="create_or_sync")
        progress_cb(95, "Finalizing", "Recording structural graph state")
        health = build_service().health(provider_repo_id, repo_path)
        CodeIntelligenceConfigDB.upsert(
            provider_repo_id,
            provider=health.provider,
            graph_version=health.graph_version,
            last_indexed_at=health.indexed_at,
            last_synced_at=health.indexed_at,
            freshness=health.freshness.value,
            last_error_code=None,
            last_error_at=None,
        )
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
    except Exception as exc:
        CodeIntelligenceConfigDB.upsert(
            provider_repo_id, freshness="stale", last_error_code=getattr(getattr(exc, "category", None), "value", "internal"),
            last_error_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        raise


def _resolve_repo(name: str):
    """Look up repo in ContextDB and return (Path, repo_name)."""
    from context.db import ContextDB
    repo = ContextDB.get_repo_by_identifier(name)
    if not repo:
        raise FileNotFoundError(f"Project not found: {name}")
    repo_path = Path(repo.get("path", ""))
    if not repo_path.exists():
        raise FileNotFoundError(f"Path does not exist: {repo_path}")
    return repo_path, repo["name"]


def _run_index(target: str, progress_cb, clear: bool = True) -> dict:
    """Run index for a single repo."""
    from context.indexer import Indexer
    repo_path, repo_name = _resolve_repo(target)
    indexer = Indexer()
    return indexer.index_repository(repo_path, repo_name=repo_name,
                                    clear=clear, job_progress_cb=progress_cb)


def _run_ast(target: str, progress_cb, clear: bool = True) -> dict:
    """Run AST generation for a single repo."""
    from context.indexer import Indexer
    repo_path, repo_name = _resolve_repo(target)
    indexer = Indexer()
    return indexer.generate_ast_for_repository(repo_path, repo_name=repo_name,
                                               clear=clear,
                                               job_progress_cb=progress_cb)


def _run_batch_index(progress_cb) -> dict:
    """Index all un-indexed repos."""
    from context.db import ContextDB
    from context.indexer import Indexer

    repos = ContextDB.list_repos()
    to_index = [r for r in repos if r.get("status") in ("added", None, "error")]
    total = len(to_index)
    results = []
    indexer = Indexer()

    for i, repo in enumerate(to_index):
        progress_cb(int(i / total * 100) if total else 100,
                     f"Indexing {repo['name']} ({i+1}/{total})")
        try:
            r = indexer.index_repository(Path(repo["path"]), repo_name=repo["name"])
            results.append({"name": repo["name"], "status": "done"})
        except Exception as e:
            logger.error(f"Batch index failed for {repo['name']}: {e}")
            results.append({"name": repo["name"], "status": "failed", "error": str(e)[:200]})

    return {"count": total, "results": results}


def _run_batch_ast(progress_cb) -> dict:
    """Generate AST for all repos."""
    from context.db import ContextDB
    from context.indexer import Indexer

    repos = ContextDB.list_repos()
    total = len(repos)
    results = []
    indexer = Indexer()

    for i, repo in enumerate(repos):
        progress_cb(int(i / total * 100) if total else 100,
                     f"AST for {repo['name']} ({i+1}/{total})")
        try:
            r = indexer.generate_ast_for_repository(Path(repo["path"]), repo_name=repo["name"])
            results.append({"name": repo["name"], "status": "done"})
        except Exception as e:
            logger.error(f"Batch AST failed for {repo['name']}: {e}")
            results.append({"name": repo["name"], "status": "failed", "error": str(e)[:200]})

    return {"count": total, "results": results}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
