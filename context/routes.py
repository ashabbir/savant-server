"""Flask Blueprint for Context API — /api/context/*.

Provides REST endpoints for semantic code search, memory bank,
project management, and indexing.
"""

import logging
from pathlib import Path

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

context_bp = Blueprint("context", __name__)

# ---------------------------------------------------------------------------
# Lazy init — context schema is initialized on first request
# ---------------------------------------------------------------------------
_initialized = False


def _ensure_init():
    global _initialized
    if _initialized:
        return True
    try:
        from .db import init_context_schema
        ok = init_context_schema()
        if ok:
            _initialized = True
        return ok
    except Exception as e:
        logger.error(f"Context init failed: {e}")
        return False


def _validate_repo_path(repo):
    repo_path = Path(repo.get("path", ""))
    if not repo_path.exists():
        return None, f"Project path no longer exists: {repo_path}"
    if not repo_path.is_dir():
        return None, f"Project path is not a directory: {repo_path}"
    return repo_path, None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@context_bp.route("/api/context/health")
def health():
    _ensure_init()
    from .db import vec_version, ContextDB
    from .embeddings import EmbeddingModel, MODEL_NAME, EMBEDDING_DIM, resolve_model_dir

    try:
        stats = ContextDB.get_stats()
    except Exception:
        stats = {"repos": 0, "files": 0, "chunks": 0}

    vv = vec_version()
    model_dir = resolve_model_dir()
    return jsonify({
        "available": _initialized and vv is not None,
        "sqlite_vec": {"loaded": vv is not None, "version": vv},
        "model": {
            "name": MODEL_NAME,
            "dim": EMBEDDING_DIM,
            "downloaded": EmbeddingModel.is_available(),
            "loaded": EmbeddingModel.is_loaded(),
            "path": str(model_dir),
        },
        "counts": stats,
    })


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@context_bp.route("/api/context/search")
def search():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "query required", "results": []}), 400

    repo = request.args.get("repo")
    limit = min(100, max(1, int(request.args.get("limit", 10))))
    exclude_mb = request.args.get("exclude_memory_bank", "").lower() in ("1", "true")

    try:
        from .embeddings import EmbeddingModel
        embedder = EmbeddingModel.get()
        qvec = embedder.embed_one(q)

        from .db import ContextDB
        repo_filter = repo.split(",") if repo and "," in repo else repo
        results = ContextDB.vector_search(
            qvec, limit=limit, repo_filter=repo_filter,
            exclude_memory_bank=exclude_mb,
        )
        return jsonify({"query": q, "result_count": len(results), "results": results})
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return jsonify({"error": str(e), "results": []}), 500


@context_bp.route("/api/context/memory/search")
def memory_search():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "query required", "results": []}), 400

    repo = request.args.get("repo")
    limit = min(100, max(1, int(request.args.get("limit", 20))))

    try:
        from .embeddings import EmbeddingModel
        embedder = EmbeddingModel.get()
        qvec = embedder.embed_one(q)

        from .db import ContextDB
        repo_filter = repo.split(",") if repo and "," in repo else repo
        results = ContextDB.vector_search(
            qvec, limit=limit, repo_filter=repo_filter, memory_bank_only=True,
        )
        return jsonify({"query": q, "result_count": len(results), "results": results})
    except Exception as e:
        logger.error(f"Memory search failed: {e}")
        return jsonify({"error": str(e), "results": []}), 500


@context_bp.route("/api/context/ast/search")
def ast_search():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    query = request.args.get("query", "").strip()
    if not query:
        return jsonify({"error": "query required", "results": []}), 400

    repo = request.args.get("repo")
    repo_filter = repo.split(",") if repo and "," in repo else repo

    try:
        from .db import ContextDB
        results = ContextDB.search_ast_nodes(query, repo_filter=repo_filter)
        return jsonify({"query": query, "result_count": len(results), "results": results})
    except Exception as e:
        logger.error(f"AST search failed: {e}")
        return jsonify({"error": str(e), "results": []}), 500


@context_bp.route("/api/context/ast/list")
def ast_list():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    repo = request.args.get("repo")
    repo_filter = repo.split(",") if repo and "," in repo else repo
    nodes = ContextDB.list_ast_nodes(repo_filter)
    return jsonify({"ast_count": len(nodes), "nodes": nodes})


@context_bp.route("/api/context/analysis", methods=["POST"])
def analyze():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    data = request.get_json(force=True) or {}
    repo = (data.get("repo") or "").strip()
    path = (data.get("path") or "").strip()
    uri = (data.get("uri") or "").strip()
    name = (data.get("name") or data.get("class_name") or data.get("symbol") or "").strip() or None
    node_type = (data.get("node_type") or "").strip() or None
    diff_text = data.get("diff") or None
    code_text = data.get("code") or None

    if not path and uri:
        if ":" in uri:
            repo_part, path_part = uri.split(":", 1)
            repo = repo or repo_part
            path = path or path_part
        else:
            path = uri

    if not path and not code_text and not diff_text:
        return jsonify({"error": "path, uri, code, or diff required"}), 400

    from .analysis import AnalysisTarget, analyze_code
    from .db import ContextDB

    before_text = ""
    if code_text is None and repo and path:
        current = ContextDB.read_code_file(f"{repo}:{path}")
        before_text = (current or {}).get("content", "")
    elif code_text is not None:
        before_text = ""

    target = AnalysisTarget(path=path or uri or "", name=name, node_type=node_type)
    result = analyze_code(
        content_before=before_text,
        content_after=code_text,
        target=target,
        diff=diff_text,
        target_missing_is_new=bool(code_text is not None and not before_text),
    )
    result["repo"] = repo
    result["path"] = path
    result["uri"] = uri or (f"{repo}:{path}" if repo and path else path)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Memory bank resources
# ---------------------------------------------------------------------------

@context_bp.route("/api/context/memory/list")
def memory_list():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    repo = request.args.get("repo")
    repo_filter = repo.split(",") if repo and "," in repo else repo
    resources = ContextDB.list_memory_resources(repo_filter)
    return jsonify({"resource_count": len(resources), "resources": resources})


@context_bp.route("/api/context/memory/read")
def memory_read():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    uri = request.args.get("uri", "").strip()
    if not uri:
        return jsonify({"error": "uri required"}), 400

    from .db import ContextDB
    doc = ContextDB.read_memory_resource(uri)
    if not doc:
        return jsonify({"error": f"Resource not found: {uri}"}), 404
    return jsonify(doc)


# ---------------------------------------------------------------------------
# Code files
# ---------------------------------------------------------------------------

@context_bp.route("/api/context/code/list")
def code_list():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    repo = request.args.get("repo")
    repo_filter = repo.split(",") if repo and "," in repo else repo
    files = ContextDB.list_code_files(repo_filter)
    return jsonify({"file_count": len(files), "files": files})


@context_bp.route("/api/context/code/read")
def code_read():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    uri = request.args.get("uri", "").strip()
    if not uri:
        return jsonify({"error": "uri required"}), 400

    from .db import ContextDB
    doc = ContextDB.read_code_file(uri)
    if not doc:
        return jsonify({"error": f"File not found: {uri}"}), 404
    return jsonify(doc)


# ---------------------------------------------------------------------------
# Project / Repo management
# ---------------------------------------------------------------------------

@context_bp.route("/api/context/repos")
def list_repos():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    repos = ContextDB.list_repos()
    return jsonify({"repos": repos, "count": len(repos)})


@context_bp.route("/api/context/repos/status")
def repos_status():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    stats = ContextDB.get_repo_stats()
    return jsonify({"repo_count": len(stats), "status": stats})


@context_bp.route("/api/context/repos/sources")
def repo_sources():
    from .ingestion import get_source_availability

    availability = get_source_availability()
    sources = availability.as_dict()
    return jsonify({
        "sources": sources,
        "any_enabled": any(s["enabled"] for s in sources.values()),
    })


@context_bp.route("/api/context/repos", methods=["POST"])
def add_repo():
    """Add a project from a configured source (does NOT index it)."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    data = request.get_json(force=True) or {}
    source = (data.get("source") or "").strip().lower()
    branch = (data.get("branch") or "").strip() or None

    from .db import ContextDB

    if source not in {"github", "gitlab", "directory"}:
        return jsonify({"error": "source must be one of: github, gitlab, directory"}), 400

    try:
        if source in {"github", "gitlab"}:
            from .ingestion import IngestionError, detect_repo_provider, ingest_repo

            url = (data.get("url") or "").strip()
            provider = detect_repo_provider(url)
            if provider != source:
                return jsonify({"error": f"URL does not match source '{source}'"}), 400
            ingested = ingest_repo(url=url, branch=branch)
        else:
            from .ingestion import IngestionError, ingest_directory

            directory = (data.get("directory") or "").strip()
            ingested = ingest_directory(directory=directory)
    except IngestionError as e:
        return jsonify({"error": str(e)}), 400

    existing = ContextDB.get_repo(ingested.name)
    if existing and existing.get("path") != ingested.path:
        return jsonify({
            "error": (
                f"Project '{ingested.name}' already exists at {existing.get('path')}. "
                "Use a different source path or remove the existing project first."
            )
        }), 409

    repo = ContextDB.add_repo(ingested.name, ingested.path)
    return jsonify(repo), 201


@context_bp.route("/api/context/repos/<name>", methods=["DELETE"])
def delete_repo(name):
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    ok = ContextDB.delete_repo(name)
    if not ok:
        return jsonify({"error": f"Project not found: {name}"}), 404
    return jsonify({"deleted": True, "name": name})


@context_bp.route("/api/context/repos/stop", methods=["POST"])
def stop_indexing():
    """Request cancellation of an in-progress indexing job, or reset a stuck state."""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    from .indexer import request_cancel, get_indexing_status, _clear_status
    from .db import ContextDB
    from db.jobs import JobDB

    # Cancel via job queue (new path)
    job_cancelled = False
    for jt in ("index", "reindex", "ast", "index-all"):
        active = JobDB.find_active(jt, name)
        if active:
            JobDB.request_cancel(active["id"])
            job_cancelled = True

    # Also cancel via legacy in-memory flags (backward compat)
    status = get_indexing_status()
    was_active = name in status and status[name].get("status") == "indexing"

    if was_active:
        request_cancel(name)
    elif not job_cancelled:
        _clear_status(name)

    # Always reset DB status to "added" so the project becomes indexable again
    repo = ContextDB.get_repo(name) if _ensure_init() else None
    if repo:
        ContextDB.update_repo_status(name, "added")

    return jsonify({"stopping": was_active or job_cancelled,
                    "reset": not (was_active or job_cancelled), "name": name})


@context_bp.route("/api/context/repos/purge", methods=["POST"])
def purge_repo():
    """Clear all indexed data (index + AST) for a project."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    from .db import ContextDB
    repo = ContextDB.get_repo(name)
    if not repo:
        return jsonify({"error": f"Project not found: {name}"}), 404

    ContextDB.clear_repo_data(repo["id"])
    ContextDB.update_repo_status(name, "added")
    return jsonify({"purged": True, "name": name})


@context_bp.route("/api/context/repos/index/purge", methods=["POST"])
def purge_index():
    """Clear ONLY chunk/vector data for a project."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    from .db import ContextDB
    repo = ContextDB.get_repo(name)
    if not repo:
        return jsonify({"error": f"Project not found: {name}"}), 404

    ContextDB.clear_index_data(repo["id"])
    return jsonify({"purged": True, "name": name, "type": "index"})


@context_bp.route("/api/context/repos/ast/generate", methods=["POST"])
def generate_ast():
    """Extract AST nodes for a single project via the job queue."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    from .db import ContextDB
    repo = ContextDB.get_repo(name)
    if not repo:
        return jsonify({"error": f"Project not found: {name}"}), 404
    repo_path, path_err = _validate_repo_path(repo)
    if path_err:
        ContextDB.update_repo_status(name, "error")
        return jsonify({
            "error": (
                f"{path_err}. Re-add the project from Context > Add Project "
                "or fix the server mount path."
            )
        }), 400

    from db.jobs import JobDB
    existing = JobDB.find_active("ast", name)
    if existing:
        return jsonify({"started": True, "name": name, "type": "ast",
                        "job_id": existing["id"], "reused": True})

    job = JobDB.create_job("ast", name)
    return jsonify({"started": True, "name": name, "type": "ast",
                    "job_id": job["id"]})


@context_bp.route("/api/context/repos/ast/purge", methods=["POST"])
def purge_ast():
    """Clear ONLY AST data for a project."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    from .db import ContextDB
    repo = ContextDB.get_repo(name)
    if not repo:
        return jsonify({"error": f"Project not found: {name}"}), 404

    ContextDB.clear_ast_data(repo["id"])
    return jsonify({"purged": True, "name": name, "type": "ast"})


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

@context_bp.route("/api/context/repos/index", methods=["POST"])
def index_repo():
    """Index (or re-index) a single project via the job queue."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    from .db import ContextDB
    repo = ContextDB.get_repo(name)
    if not repo:
        return jsonify({"error": f"Project not found: {name}"}), 404
    repo_path, path_err = _validate_repo_path(repo)
    if path_err:
        ContextDB.update_repo_status(name, "error")
        return jsonify({
            "error": (
                f"{path_err}. Re-add the project from Context > Add Project "
                "or fix the server mount path."
            )
        }), 400

    from db.jobs import JobDB
    existing = JobDB.find_active("index", name)
    if existing:
        return jsonify({"started": True, "name": name,
                        "job_id": existing["id"], "reused": True})

    job = JobDB.create_job("index", name)
    return jsonify({"started": True, "name": name, "job_id": job["id"]})


@context_bp.route("/api/context/repos/reindex", methods=["POST"])
def reindex_repo():
    """Re-index a single project (clear old data + index) via job queue."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    from .db import ContextDB
    repo = ContextDB.get_repo(name)
    if not repo:
        return jsonify({"error": f"Project not found: {name}"}), 404
    repo_path, path_err = _validate_repo_path(repo)
    if path_err:
        ContextDB.update_repo_status(name, "error")
        return jsonify({
            "error": (
                f"{path_err}. Re-add the project from Context > Add Project "
                "or fix the server mount path."
            )
        }), 400

    from db.jobs import JobDB
    existing = JobDB.find_active("reindex", name)
    if existing:
        return jsonify({"started": True, "name": name, "reindex": True,
                        "job_id": existing["id"], "reused": True})

    job = JobDB.create_job("reindex", name)
    return jsonify({"started": True, "name": name, "reindex": True,
                    "job_id": job["id"]})


@context_bp.route("/api/context/repos/index-all", methods=["POST"])
def index_all():
    """Index all un-indexed projects via a single batch job."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    from .db import ContextDB
    from db.jobs import JobDB

    repos = ContextDB.list_repos()
    to_index = [r for r in repos if r.get("status") in ("added", None, "error")]

    existing = JobDB.find_active("index-all", "__all__")
    if existing:
        return jsonify({"started": True, "count": len(to_index),
                        "job_id": existing["id"], "reused": True})

    job = JobDB.create_job("index-all", "__all__")
    return jsonify({"started": True, "count": len(to_index),
                    "projects": [r["name"] for r in to_index],
                    "job_id": job["id"]})


@context_bp.route("/api/context/repos/reindex-all", methods=["POST"])
def reindex_all():
    """Re-index all projects via a single batch job."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    from .db import ContextDB
    from db.jobs import JobDB

    repos = ContextDB.list_repos()

    existing = JobDB.find_active("index-all", "__all__")
    if existing:
        return jsonify({"started": True, "count": len(repos),
                        "job_id": existing["id"], "reused": True})

    job = JobDB.create_job("index-all", "__all__")
    return jsonify({"started": True, "count": len(repos),
                    "projects": [r["name"] for r in repos],
                    "job_id": job["id"]})


@context_bp.route("/api/context/repos/indexing-status")
def indexing_status():
    from .indexer import get_indexing_status
    from .db import ContextDB
    from datetime import datetime, timezone

    live = get_indexing_status()

    # Merge job queue status into the live dict
    try:
        from db.jobs import JobDB
        running_jobs = JobDB.list_jobs(status="running", limit=20)
        queued_jobs = JobDB.list_jobs(status="queued", limit=20)
        for job in running_jobs + queued_jobs:
            target = job["target"]
            if target == "__all__":
                continue
            if target not in live or live[target].get("status") != "indexing":
                live[target] = {
                    "status": "indexing" if job["status"] == "running" else "queued",
                    "phase": job.get("phase", ""),
                    "progress": job.get("progress", 0),
                    "total": 0,
                    "files_done": 0,
                    "chunks_done": 0,
                    "current_file": job.get("message", ""),
                    "errors": 0,
                    "job_id": job["id"],
                    "job_type": job.get("job_type", ""),
                }
    except Exception:
        pass

    # Also include DB status for any repos currently marked "indexing"
    try:
        repos = ContextDB.list_repos()
        for r in repos:
            name = r["name"]
            if name not in live and r.get("status") == "indexing":
                live[name] = {
                    "status": "stalled",
                    "phase": "No active worker",
                    "progress": 0,
                    "total": 0,
                    "files_done": 0,
                    "chunks_done": 0,
                    "current_file": "",
                    "errors": 0,
                    "error": (
                        "This repo is marked as indexing in the database, but no live index worker is running. "
                        "The previous job likely exited early or the app was restarted. Use Stop Indexing to reset it, then retry."
                    ),
                }
            elif name not in live:
                live[name] = {
                    "status": r.get("status", "added"),
                    "phase": "Complete" if r.get("status") == "indexed" else "",
                    "progress": 100 if r.get("status") == "indexed" else 0,
                    "total": r.get("file_count", 0),
                    "files_done": r.get("file_count", 0),
                    "chunks_done": r.get("chunk_count", 0),
                    "current_file": "",
                    "errors": 0,
                }
    except Exception:
        pass

    for item in live.values():
        updated_at = item.get("updated_at")
        if item.get("status") != "indexing" or not updated_at:
            continue
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(updated_at)).total_seconds()
        except Exception:
            continue
        if age >= 180:
            item["status"] = "stalled"
            item["phase"] = "No recent progress"
            item["error"] = (
                f"No index progress has been reported for {int(age)} seconds. "
                "The worker may be hung on model load, file IO, or embedding generation."
            )

    return jsonify(live)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@context_bp.route("/api/context/stats")
def stats():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    from .embeddings import MODEL_NAME, EMBEDDING_DIM, EmbeddingModel, resolve_model_dir
    return jsonify({
        "counts": ContextDB.get_stats(),
        "model": {
            "name": MODEL_NAME,
            "dim": EMBEDDING_DIM,
            "downloaded": EmbeddingModel.is_available(),
            "loaded": EmbeddingModel.is_loaded(),
            "path": str(resolve_model_dir()),
        },
    })
