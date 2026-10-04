"""
Job Worker — Single-threaded FIFO job processor.

Polls the jobs table every 2 seconds for the oldest queued job and processes it.
Only one job runs at a time. Progress is written to the DB so it survives restarts.
"""

import logging
import json
import threading
import time
import traceback
from pathlib import Path
from time import perf_counter

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

    consecutive_errors = 0
    while True:
        try:
            _process_next_job()
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            # Exponential backoff (2s → 4s → 8s … capped at 30s) to avoid
            # flooding logs during DB recovery or transient connection failures.
            backoff = min(2 ** consecutive_errors, 30)
            if consecutive_errors == 1 or consecutive_errors % 5 == 0:
                logger.error(f"Job worker error (attempt {consecutive_errors}): {e}")
            time.sleep(backoff)
            continue
        time.sleep(2)


def run_forever():
    """Run the persistent queue worker in a dedicated process."""
    logger.info("Dedicated job worker starting")
    from db.jobs import JobDB
    recovered = JobDB.recover_interrupted()
    if recovered:
        logger.warning("Marked %s interrupted job(s) as cancelled after worker restart", recovered)
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

    started_at = perf_counter()
    payload = job.get("result") or {}
    if not isinstance(payload, dict):
        payload = {}
    user_id = ""
    user_id = str(payload.get("user_id") or payload.get("actor_id") or "")
    try:
        result = _execute_job(job_id, job_type, target, payload)
        JobDB.set_done(job_id, result)
        job_status = result.get("status", "success") if isinstance(result, dict) else "success"
        _record_job_activity(job_type, target, job_status, result, started_at, payload=payload)
        logger.info(f"Job {job_id} completed: {job_type} → {target} ({job_status})")
        try:
            from db.notifications import NotificationDB
            NotificationDB.notify_job_success(job_id, job_type, target, result=result, user_id=user_id)
        except Exception as notif_err:
            logger.warning("Failed to emit job success notification for %s: %s", job_id, notif_err)
    except _CancelledError:
        from db.jobs import JobDB as JDB
        JDB.set_cancelled(job_id)
        _record_job_activity(job_type, target, "cancelled", {}, started_at, payload=payload)
        logger.info(f"Job {job_id} cancelled: {job_type} → {target}")
    except Exception as e:
        if JobDB.is_cancel_requested(job_id):
            JobDB.set_cancelled(job_id)
            logger.info(f"Job {job_id} cancelled during {job_type}: {target}")
            return
        logger.error(f"Job {job_id} failed: {e}")
        JobDB.set_failed(job_id, str(e)[:2000])
        _record_job_activity(job_type, target, "failed", {}, started_at, str(e), payload=payload)
        try:
            from db.notifications import NotificationDB
            NotificationDB.notify_job_failure(
                job_id=job_id,
                job_type=job_type,
                target=target,
                error=str(e),
                user_id=user_id,
                payload=payload if isinstance(payload, dict) else {},
            )
        except Exception as notif_err:
            logger.warning("Failed to emit job failure notification for %s: %s", job_id, notif_err)


def _record_job_activity(
    job_type: str, target: str, status: str, result: dict,
    started_at: float, error: str = "", payload: dict | None = None,
) -> None:
    """Persist indexing outcomes, preserving the scheduled job's provenance."""
    if job_type not in {
        "index", "reindex", "ast", "lst", "index-all", "ast-all",
        "codegraph_index", "codegraph_sync", "differential_sync",
        "initial_repo_sync", "initial_repo_processing",
    }:
        return
    try:
        from context.db import ContextDB
        repo_name = target
        if target != "__all__":
            repo = ContextDB.get_repo_by_identifier(target)
            repo_name = (repo or {}).get("name") or target
        change_stats = {"job_type": job_type, **_extract_index_metrics(result)}
        files_changed = result.get("files_changed") if isinstance(result, dict) else None
        graph_result = result.get("graph_result") if isinstance(result, dict) else None
        if isinstance(graph_result, dict):
            change_stats["codegraph_accepted"] = bool(graph_result.get("accepted", False))
            change_stats["codegraph_result"] = graph_result.get("result", {})
        if isinstance(files_changed, dict):
            change_stats["codegraph_changed_files"] = [
                *files_changed.get("added", []),
                *files_changed.get("modified", []),
                *files_changed.get("deleted", []),
            ]
        if isinstance(result, dict):
            if "index_result" in result:
                change_stats["index_summary"] = result["index_result"]
            if "ast_result" in result:
                change_stats["ast_summary"] = result["ast_result"]
            if "lst_result" in result:
                change_stats["lst_summary"] = result["lst_result"]
            if "summary" in result:
                change_stats["summary"] = result["summary"]
            if "hash_changed" in result:
                change_stats["hash_changed"] = result["hash_changed"]

        indexed = (
            job_type in {"index", "reindex", "index-all"}
            or (job_type == "differential_sync" and bool(result.get("index_result", {}).get("files_indexed", 0)))
        ) and status == "success"
        graphed = (
            job_type in {
                "ast", "ast-all", "lst", "codegraph_index", "codegraph_sync"
            }
            or (job_type == "differential_sync" and bool(
                result.get("graph_result", {}).get("accepted")
                or result.get("ast_result", {}).get("files_processed")
                or result.get("lst_result", {}).get("files_processed")
            ))
        ) and status == "success"
        code_changed = bool(result.get("hash_changed", result.get("changed", False))) if isinstance(result, dict) else False
        details_text = (result.get("summary") if isinstance(result, dict) else "") or json.dumps(result, default=str)[:10000]

        payload = payload or {}
        ContextDB.record_repo_sync_log(
            repo_name=repo_name, operation=job_type,
            trigger=str(payload.get("trigger") or "user"),
            actor_id=str(payload.get("actor_id") or payload.get("user_id") or "user"),
            source_app=str(payload.get("source_app") or "savant-olympus"), status=status,
            before_commit=result.get("before_commit") if isinstance(result, dict) else None,
            after_commit=result.get("after_commit") if isinstance(result, dict) else None,
            files_changed=files_changed,
            fetched=True if job_type == "differential_sync" else False,
            code_changed=code_changed,
            indexed=indexed,
            graphed=graphed,
            duration_ms=int((perf_counter() - started_at) * 1000),
            error=error, details=details_text,
            change_stats=change_stats,
        )
    except Exception:
        logger.exception("Failed to persist job activity for %s → %s", job_type, target)


def _extract_index_metrics(result: object) -> dict:
    """Extract and aggregate indexing counters from direct, differential, or batch results."""
    metric_names = {
        "files_indexed", "files_skipped", "files_removed", "chunks_indexed",
        "errors", "files_processed",
    }
    if not isinstance(result, dict):
        return {}
    direct = {
        name: int(result.get(name, 0))
        for name in metric_names
        if isinstance(result.get(name), (int, float))
    }
    if direct:
        return {
            **direct,
            "files_removed_from_index": direct.get("files_removed", 0),
            "index_errors": direct.get("errors", 0),
        }
    totals = {}
    for value in result.values():
        children = value if isinstance(value, list) else [value]
        for child in children:
            for name, count in _extract_index_metrics(child).items():
                if name not in {"files_removed", "errors"}:
                    totals[name] = totals.get(name, 0) + count
    return totals


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


def _execute_job(job_id: str, job_type: str, target: str, payload: dict | None = None) -> dict:
    """Dispatch job to the appropriate handler."""
    from db.jobs import JobDB

    progress_cb = _make_progress_callback(job_id)
    payload = payload or {}

    if job_type == "index":
        return _run_index(target, progress_cb, clear=True, payload=payload)
    elif job_type == "reindex":
        return _run_index(target, progress_cb, clear=True)
    elif job_type == "ast":
        return _run_ast(target, progress_cb, clear=True, payload=payload)
    elif job_type == "lst":
        return _run_lst(target, progress_cb, payload=payload)
    elif job_type == "index-all":
        return _run_batch_index(progress_cb)
    elif job_type == "ast-all":
        return _run_batch_ast(progress_cb)
    elif job_type in ("codegraph_index", "codegraph_sync"):
        return _run_code_intelligence_sync(job_id, target, progress_cb, payload=payload)
    elif job_type == "differential_sync":
        return _run_differential_sync(job_id, target, progress_cb, payload)
    elif job_type == "initial_repo_sync":
        return _run_initial_repo_sync(target, payload, progress_cb)
    elif job_type == "initial_repo_processing":
        return _run_initial_repo_processing(target, progress_cb)
    else:
        raise ValueError(f"Unknown job type: {job_type}")


def _run_initial_repo_sync(target: str, payload: dict, progress_cb) -> dict:
    """Clone a newly registered remote repository (initial git sync)."""
    from context.ingestion import ingest_repo
    from context.db import ContextDB
    started_at = perf_counter()
    url = str(payload.get("url") or "")
    if not url:
        raise ValueError("Initial repository sync is missing its remote URL")
    progress_cb(10, "Downloading repository", "Preparing first checkout")
    ingested = ingest_repo(url, branch=payload.get("branch") or None)
    if ingested.name != target:
        raise RuntimeError(f"Registered repository name changed from {target} to {ingested.name}")
    ContextDB.add_repo(ingested.name, ingested.path)
    ContextDB.mark_repo_fetched(ingested.name)
    ContextDB.update_repo_status(ingested.name, "ready")
    ContextDB.record_repo_sync_log(
        repo_name=ingested.name, operation=ingested.operation or "clone", trigger="project_add",
        provider=ingested.provider, branch=ingested.branch, status="success",
        after_commit=ingested.after_commit, fetched=True, code_changed=ingested.changed,
        duration_ms=int((perf_counter() - started_at) * 1000), details="Initial repository clone completed",
        actor_id=str(payload.get("actor_id") or "user"),
        source_app=str(payload.get("source_app") or ""),
    )
    progress_cb(100, "Complete", "Initial repository clone completed")
    return {
        "repo_name": ingested.name,
        "operation": ingested.operation,
        "after_commit": ingested.after_commit,
        "status": "success",
    }


def _run_initial_repo_processing(target: str, progress_cb, clone_result: dict | None = None) -> dict:
    """Mark initial registration ready for directory projects."""
    from context.db import ContextDB
    repo_path, repo_name = _resolve_repo(target)
    ContextDB.update_repo_status(repo_name, "ready")
    progress_cb(100, "Complete", "Repository registration completed")
    return {"repo_name": repo_name, "status": "ready"}


def _run_differential_sync(job_id: str, target: str, progress_cb, payload: dict | None = None) -> dict:
    """Discover a Git diff and enqueue the scoped follow-up processing jobs.

    The differential job intentionally does not perform indexing itself.  Keeping
    each follow-up as a durable queue item makes progress visible in Olympus and
    prevents a long-running diff from hiding failures in AST, LST, or CodeGraph.

    1. Check if current hash is different from remote git hash.
    2. If so then Pull.
    3. If hash didn't change: skip the whole process.
    4. Figure out which code files changed.
    5. Queue differential Index, AST, LST, and CodeGraph work with that exact
       file set, including deleted paths so generated data is cleaned up.
    """
    from context.db import ContextDB
    from context.ingestion import refresh_repo, IngestionError, _get_git_head
    from context.indexer import get_git_diff_files
    from db.jobs import JobDB

    repo_path, repo_name = _resolve_repo(target)
    started_at = perf_counter()
    payload = payload or {}
    actor_id = str(payload.get("actor_id") or payload.get("user_id") or "user")
    source_app = str(payload.get("source_app") or "savant-olympus")
    trigger = str(payload.get("trigger") or "user")

    if not (repo_path / ".git").is_dir():
        progress_cb(100, "Complete", "Not a Git repository; differential sync skipped")
        return {"repo_name": repo_name, "status": "skipped", "message": "Not a Git repository"}

    progress_cb(5, "Checking Git Hash", f"Checking if current hash is different from git hash for {repo_name}")

    repo_record = ContextDB.get_repo(repo_name) or {}
    is_indexed = (repo_record.get("status") in {"indexed", "ast_only"}) or bool(repo_record.get("indexed_at"))

    try:
        refreshed = refresh_repo(str(repo_path))
    except IngestionError as exc:
        ContextDB.record_repo_sync_log(
            repo_name=repo_name, operation="differential_sync", trigger=trigger,
            status="failed", error=str(exc), duration_ms=int((perf_counter() - started_at) * 1000),
            details=f"Repository git differential pull failed: {exc}",
            actor_id=actor_id, source_app=source_app,
        )
        raise

    before_commit = refreshed.before_commit or ""
    after_commit = refreshed.after_commit or ""
    hash_changed = bool(refreshed.changed)

    # If the hash didn't change and project is already indexed: SKIP THE WHOLE PROCESS
    if not hash_changed and is_indexed:
        current_hash_display = (after_commit or before_commit or _get_git_head(repo_path))[:7] or "N/A"
        summary_msg = f"Git hash unchanged ({current_hash_display}). Skipped whole process."
        progress_cb(100, "Complete", summary_msg)

        return {
            "repo_name": repo_name,
            "status": "skipped",
            "changed": False,
            "hash_changed": False,
            "before_commit": before_commit,
            "after_commit": after_commit,
            "message": summary_msg,
            "summary": summary_msg,
            "files_changed": {"added": [], "modified": [], "deleted": []},
            "queued_jobs": [],
        }

    # Hash changed (or project was unindexed):
    ContextDB.add_repo(refreshed.name, refreshed.path)
    ContextDB.mark_repo_fetched(refreshed.name)

    # Figure out which files changed:
    progress_cb(15, "Diff Analysis", f"Analyzing changed files between commits for {repo_name}")
    added, modified, deleted = get_git_diff_files(repo_path, before_commit, after_commit)
    files_changed = {"added": added, "modified": modified, "deleted": deleted}
    num_changed = len(added) + len(modified) + len(deleted)

    if num_changed == 0:
        commit_display = (after_commit or "HEAD")[:7]
        summary_msg = f"Commit {commit_display} contains no code file changes. No follow-up jobs queued."
        progress_cb(100, "Complete", summary_msg)
        return {
            "repo_name": repo_name, "status": "skipped", "changed": False,
            "hash_changed": True, "before_commit": before_commit, "after_commit": after_commit,
            "summary": summary_msg, "files_changed": files_changed, "queued_jobs": [],
        }

    child_payload = {
        "parent_job_id": job_id,
        "differential": True,
        "files_changed": files_changed,
        "before_commit": before_commit,
        "after_commit": after_commit,
        "provider_repo_id": str(repo_record.get("id") or target),
        "user_id": actor_id,
        "actor_id": actor_id,
        "trigger": trigger,
        "source_app": source_app,
    }
    # Index must precede AST and LST: both annotate the repository file records
    # created/updated by the differential indexer.  FIFO retains this dependency.
    queued_jobs = [
        JobDB.create_job("index", target, payload=child_payload),
        JobDB.create_job("ast", target, payload=child_payload),
        JobDB.create_job("lst", target, payload=child_payload),
        JobDB.create_job("codegraph_sync", target, payload=child_payload),
    ]
    summary_text = (
        f"Differential sync found {len(added)} added, {len(modified)} modified, and {len(deleted)} deleted code files. "
        "Queued differential Index, AST, LST, and CodeGraph jobs."
    )
    progress_cb(100, "Complete", summary_text)

    return {
        "repo_name": repo_name,
        "status": "success",
        "changed": True,
        "hash_changed": True,
        "before_commit": before_commit,
        "after_commit": after_commit,
        "files_changed": files_changed,
        "summary": summary_text,
        "queued_jobs": [{"id": job["id"], "job_type": job["job_type"], "target": job["target"]} for job in queued_jobs],
    }


def _run_code_intelligence_sync(job_id: str, target: str, progress_cb, payload: dict | None = None) -> dict:
    """Run structural create/sync without changing semantic repository status."""
    from code_intelligence.runtime import build_service
    from db.code_intelligence import CodeIntelligenceConfigDB
    from context.indexer import Indexer

    repo_path, repo_name = _resolve_repo(target)
    payload = payload or {}
    differential = bool(payload.get("differential"))
    changed_files = payload.get("files_changed") if differential else None
    # Preserve the stable repository identifier used by the caller. Converting
    # numeric IDs to a display name here creates a second bridge registration
    # and splits watcher/freshness state for the same repository.
    provider_repo_id = str(payload.get("provider_repo_id") or target)
    progress_cb(5, "Preparing", "Resolving structural provider")
    CodeIntelligenceConfigDB.upsert(provider_repo_id, freshness="pending_sync", last_error_code=None)
    try:
        result = build_service().ensure_index(
            provider_repo_id, repo_path, mode="create_or_sync", request_id=job_id,
            changed_files=changed_files,
        )
        progress_cb(70, "Lossless source tree", "Refreshing exact syntax context")
        lossless_result = Indexer().sync_lossless_trees_for_repository(
            repo_path, repo_name=repo_name, clear=not differential, differential=differential,
            before_commit=payload.get("before_commit"), after_commit=payload.get("after_commit"),
            changed_files=changed_files,
            job_progress_cb=lambda pct, phase, message: progress_cb(
                70 + int(pct * .25), phase, message
            ),
        )
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
        result_payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
        result_payload["lossless_tree_result"] = lossless_result
        if differential:
            result_payload["differential"] = True
            result_payload["files_changed"] = changed_files
        return result_payload
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


def _run_index(target: str, progress_cb, clear: bool = True, payload: dict | None = None) -> dict:
    """Run index for a single repo."""
    from context.indexer import Indexer
    repo_path, repo_name = _resolve_repo(target)
    indexer = Indexer()
    payload = payload or {}
    differential = bool(payload.get("differential"))
    result = indexer.index_repository(
        repo_path, repo_name=repo_name, clear=False if differential else clear,
        differential=differential, before_commit=payload.get("before_commit"),
        after_commit=payload.get("after_commit"), changed_files=payload.get("files_changed"),
        job_progress_cb=progress_cb,
    )
    if differential:
        result["files_changed"] = payload.get("files_changed")
    return result


def _run_ast(target: str, progress_cb, clear: bool = True, payload: dict | None = None) -> dict:
    """Run AST generation for a single repo."""
    from context.indexer import Indexer
    repo_path, repo_name = _resolve_repo(target)
    indexer = Indexer()
    payload = payload or {}
    differential = bool(payload.get("differential"))
    result = indexer.generate_ast_for_repository(
        repo_path, repo_name=repo_name, clear=False if differential else clear,
        differential=differential, before_commit=payload.get("before_commit"),
        after_commit=payload.get("after_commit"), changed_files=payload.get("files_changed"),
        job_progress_cb=progress_cb,
    )
    if differential:
        result["files_changed"] = payload.get("files_changed")
    return result


def _run_lst(target: str, progress_cb, payload: dict | None = None) -> dict:
    """Generate Lossless Syntax Tree (LST) for a repository."""
    from context.indexer import Indexer
    repo_path, repo_name = _resolve_repo(target)
    payload = payload or {}
    differential = bool(payload.get("differential"))
    progress_cb(10, "Extracting LST", f"Parsing {'differential ' if differential else ''}Lossless Syntax Tree for {repo_name}")
    indexer = Indexer()
    lossless_result = indexer.sync_lossless_trees_for_repository(
        repo_path,
        repo_name=repo_name,
        clear=not differential,
        differential=differential,
        before_commit=payload.get("before_commit"),
        after_commit=payload.get("after_commit"),
        changed_files=payload.get("files_changed"),
        job_progress_cb=progress_cb,
    )
    progress_cb(100, "Complete", f"Lossless Syntax Tree generated for {repo_name}")
    result = {"repo_name": repo_name, "lossless_result": lossless_result}
    if differential:
        result["files_changed"] = payload.get("files_changed")
        result["differential"] = True
    return result


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
            results.append({"name": repo["name"], "status": "done", "result": r})
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
            results.append({"name": repo["name"], "status": "done", "result": r})
        except Exception as e:
            logger.error(f"Batch AST failed for {repo['name']}: {e}")
            results.append({"name": repo["name"], "status": "failed", "error": str(e)[:200]})

    return {"count": total, "results": results}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
