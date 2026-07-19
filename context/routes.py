"""Flask Blueprint for Context API — /api/context/*.

Provides REST endpoints for semantic code search, memory bank,
project management, and indexing.
"""

import logging
import os
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, jsonify, request
from utils.auth import admin_required, ALLOWED_SAVANT_APPS

logger = logging.getLogger(__name__)

context_bp = Blueprint("context", __name__)

# ---------------------------------------------------------------------------
# Global Header Guard
# ---------------------------------------------------------------------------

@context_bp.before_request
def check_savant_app_header():
    # Allow health check endpoint without header
    if request.path == "/api/context/health":
        return None
    app_name = (request.headers.get("X-App-Name") or request.headers.get("X-Savant-App") or "").strip().lower()
    if not app_name or app_name not in ALLOWED_SAVANT_APPS:
        return jsonify({
            "error": "Access denied."
        }), 403

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


def _resolve_repo_path(raw_path: str) -> Path:
    """Remap /base-code/ prefix to BASE_CODE_DIR when not running in Docker."""
    p = Path(raw_path)
    base_code_dir = os.environ.get("BASE_CODE_DIR", "").strip()
    if base_code_dir and str(p).startswith("/base-code/"):
        rel = str(p)[len("/base-code/"):]
        return Path(base_code_dir).expanduser() / rel
    return p


def _validate_repo_path(repo):
    repo_path = _resolve_repo_path(repo.get("path", ""))
    if not repo_path.exists():
        return None, f"Project path no longer exists: {repo_path}. Re-add the project from Context > Add Project or fix the server mount path."
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
    payload = {
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
    }
    repo_id = request.args.get("repo_id") or request.args.get("repo")
    if repo_id:
        try:
            record = ContextDB.get_repo_by_identifier(repo_id)
            if record:
                from code_intelligence.runtime import build_service
                structural = build_service().health(str(repo_id), _resolve_repo_path(record["path"]))
                payload["code_intelligence"] = structural.model_dump(mode="json")
        except Exception as exc:
            payload["code_intelligence"] = {
                "provider": "unknown", "indexed": False, "freshness": "unavailable",
                "warnings": [str(exc)],
            }
    return jsonify(payload)


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
    try:
        from .db import ContextDB
        if repo and "," not in repo:
            record = ContextDB.get_repo(repo)
            if not record:
                return jsonify({"error": "repository not found", "results": []}), 404
            from code_intelligence.runtime import build_service
            result = build_service().search_symbols(
                repo, _resolve_repo_path(record["path"]), query, limit=request.args.get("limit", type=int) or 20
            )
            results = [{
                "id": symbol.id, "node_type": symbol.kind, "name": symbol.name,
                "start_line": symbol.location.start_line, "end_line": symbol.location.end_line,
                "rel_path": symbol.location.file_path, "repo": repo,
                "qualified_name": symbol.qualified_name, "signature": symbol.signature,
                "provider": result.provider,
            } for symbol in result.items]
            return jsonify({"query": query, "result_count": len(results), "results": results,
                            "provider": result.provider, "incomplete": result.incomplete,
                            "warnings": result.warnings, "deprecated": True})
        repo_filter = repo.split(",") if repo and "," in repo else repo
        results = ContextDB.search_ast_nodes(query, repo_filter=repo_filter)
        return jsonify({"query": query, "result_count": len(results), "results": results,
                        "provider": "legacy", "freshness": "fresh", "deprecated": True})
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
    from .walker import FileWalker

    if repo and path:
        repo_record = ContextDB.get_repo(repo)
        repo_root = Path((repo_record or {}).get("path", "")).resolve() if repo_record else None
        if not repo_root or not repo_root.exists() or not FileWalker(repo_root, tracked_only=True).is_allowed(path):
            return jsonify({"error": "Analysis is limited to tracked, non-ignored repository source files"}), 404

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
    from .ingestion import inspect_project_source
    repos = ContextDB.list_repos()
    for repo in repos:
        repo.update(inspect_project_source(repo.get("path", "")))
    return jsonify({"repos": repos, "count": len(repos)})


@context_bp.route("/api/context/repos/status")
def repos_status():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    stats = ContextDB.get_repo_stats()
    return jsonify({"repo_count": len(stats), "status": stats})


@context_bp.route("/api/context/repos/browse", methods=["GET"])
def browse_directory():
    """Browse host/server directory contents relative to BASE_CODE_DIR."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    
    # path_param is now expected to be relative to BASE_CODE_DIR
    rel_path = request.args.get("path", "").strip()
    
    import os
    from pathlib import Path

    base_dir = os.environ.get("BASE_CODE_DIR", "").strip()
    if not base_dir:
        return jsonify({"error": "BASE_CODE_DIR not set on server"}), 500
    
    # Securely join base_dir with rel_path
    # Prevent directory traversal by resolving and checking prefix
    try:
        base_path = Path(base_dir).resolve()
        if rel_path:
            # Remove leading slashes to ensure it's treated as relative
            safe_rel = rel_path.lstrip("/").lstrip("\\")
            target_path = (base_path / safe_rel).resolve()
        else:
            target_path = base_path

        if not str(target_path).startswith(str(base_path)):
            return jsonify({"error": "Invalid path: outside of BASE_CODE_DIR"}), 403

        if not target_path.exists():
            return jsonify({"error": f"Path not found: {rel_path}"}), 404
        if not target_path.is_dir():
            return jsonify({"error": f"Path is not a directory: {rel_path}"}), 400
        
        entries = []
        for entry in os.scandir(target_path):
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue
            
            # Calculate path relative to BASE_CODE_DIR
            entry_rel_path = str(Path(entry.path).resolve().relative_to(base_path))

            entries.append({
                "name": entry.name,
                "isDirectory": entry.is_dir(),
                "path": entry_rel_path
            })
            
        entries.sort(key=lambda x: (not x["isDirectory"], x["name"].lower()))
        return jsonify(entries)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
@admin_required
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


@context_bp.route("/api/context/repos/<name>/refresh", methods=["POST"])
@admin_required
def refresh_repo(name):
    """Update a registered Git checkout from its origin remote."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    from .db import ContextDB
    repo = ContextDB.get_repo(name)
    if not repo:
        return jsonify({"error": f"Project not found: {name}"}), 404

    repo_path, path_error = _validate_repo_path(repo)
    if path_error:
        return jsonify({"error": path_error}), 400

    from .ingestion import IngestionError, refresh_repo as update_repo

    try:
        refreshed = update_repo(str(repo_path))
    except IngestionError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(ContextDB.add_repo(refreshed.name, refreshed.path))


@context_bp.route("/api/context/repos/<name>", methods=["DELETE"])
@admin_required
def delete_repo(name):
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    ok = ContextDB.delete_repo(name)
    if not ok:
        return jsonify({"error": f"Project not found: {name}"}), 404
    return jsonify({"deleted": True, "name": name})


@context_bp.route("/api/context/repos/stop", methods=["POST"])
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
    from db.code_intelligence import CodeIntelligenceConfigDB
    config = CodeIntelligenceConfigDB.get(name)
    if config and config.get("provider") == "codegraph":
        existing = JobDB.find_active("codegraph_sync", name) or JobDB.find_active("codegraph_index", name)
        if existing:
            return jsonify({"started": True, "name": name, "type": "codegraph",
                            "provider": "codegraph", "job_id": existing["id"], "reused": True})
        job = JobDB.create_job("codegraph_sync", name)
        return jsonify({"started": True, "name": name, "type": "codegraph",
                        "provider": "codegraph", "job_id": job["id"]})
    existing = JobDB.find_active("ast", name)
    if existing:
        return jsonify({"started": True, "name": name, "type": "ast",
                        "job_id": existing["id"], "reused": True})

    job = JobDB.create_job("ast", name)
    return jsonify({"started": True, "name": name, "type": "ast",
                    "job_id": job["id"]})


@context_bp.route("/api/context/repos/ast/purge", methods=["POST"])
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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


# ---------------------------------------------------------------------------
# Research & Impact Surface API
# ---------------------------------------------------------------------------

def _build_impact_surface_internal(results: dict, top_symbols: list, top_files: set) -> dict:
    upstream = []
    downstream = []
    seen_up = set()
    seen_down = set()

    graph_res = results.get("code_graph_search", {})
    if isinstance(graph_res, dict):
        for query_key, node_list in graph_res.items():
            if not isinstance(node_list, list):
                continue
            for node in node_list:
                if not isinstance(node, dict):
                    continue
                node_id = node.get("node_id", "")
                title = node.get("title") or node.get("norm_label") or node_id
                edges = node.get("edges", [])

                for edge in edges:
                    if not isinstance(edge, dict):
                        continue
                    src = edge.get("source_id", "")
                    tgt = edge.get("target_id", "")
                    rel = edge.get("label") or edge.get("edge_type") or "relates_to"

                    if (node_id and (tgt == node_id or node_id in tgt)) or any(sym.lower() in tgt.lower() for sym in top_symbols if sym):
                        up_key = f"{src}->{tgt}:{rel}"
                        if up_key not in seen_up:
                            seen_up.add(up_key)
                            upstream.append({
                                "upstream_caller": src,
                                "relationship": rel,
                                "target_symbol": title,
                            })

                    if (node_id and (src == node_id or node_id in src)) or any(sym.lower() in src.lower() for sym in top_symbols if sym):
                        down_key = f"{src}->{tgt}:{rel}"
                        if down_key not in seen_down:
                            seen_down.add(down_key)
                            downstream.append({
                                "source_symbol": title,
                                "relationship": rel,
                                "downstream_dependency": tgt,
                            })

    return {
        "summary": (
            f"Impact Surface Analysis: Identified {len(upstream)} upstream callers/importers (1 level up) "
            f"and {len(downstream)} downstream dependencies/callees (1 level down). "
            "AI AGENTS MUST evaluate these impact surfaces to prevent breaking changes when modifying code."
        ),
        "upstream_dependencies_1_level_up": upstream[:10],
        "downstream_impacts_1_level_down": downstream[:10],
        "affected_files": sorted(list(top_files))[:8],
    }


@context_bp.route("/api/context/research", methods=["POST"])
def context_research():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    data = request.get_json(force=True, silent=True) or {}
    q = (data.get("q") or data.get("query") or "").strip()
    if not q:
        return jsonify({"error": "q required", "overview": {}, "impact_surface": {}}), 400

    repo = data.get("repo")
    search_type = (data.get("type") or "all").lower().strip()
    limit = int(data.get("limit", 20))
    exclude_tests = bool(data.get("exclude_tests", True))
    should_exclude_tests = exclude_tests and "test" not in q.lower()

    results = {}
    top_symbols = []
    top_files = set()

    from .db import ContextDB
    from .embeddings import EmbeddingModel

    def _exec_code_search():
        embedder = EmbeddingModel.get()
        qvec = embedder.embed_one(q)
        repo_filter = repo.split(",") if repo and isinstance(repo, str) and "," in repo else repo
        res = ContextDB.vector_search(
            qvec, limit=limit * 2 if should_exclude_tests else limit, repo_filter=repo_filter,
            exclude_memory_bank=True,
        )
        if should_exclude_tests:
            res = [r for r in res if not (r.get("rel_path", "").lower().startswith("test") or "/test" in r.get("rel_path", "").lower())]
        return {"query": q, "result_count": len(res[:limit]), "results": res[:limit]}

    def _exec_structure_search():
        if repo and isinstance(repo, str) and "," not in repo:
            record = ContextDB.get_repo(repo)
            if record:
                from code_intelligence.runtime import build_service
                search_result = build_service().search_symbols(repo, _resolve_repo_path(record["path"]), q, limit=limit)
                res = [{"id": s.id, "node_type": s.kind, "name": s.name,
                        "start_line": s.location.start_line, "end_line": s.location.end_line,
                        "rel_path": s.location.file_path, "repo": repo,
                        "qualified_name": s.qualified_name, "signature": s.signature}
                       for s in search_result.items]
                return {"query": q, "result_count": len(res), "results": res,
                        "provider": search_result.provider, "incomplete": search_result.incomplete,
                        "warnings": search_result.warnings}
        repo_filter = repo.split(",") if repo and isinstance(repo, str) and "," in repo else repo
        res = ContextDB.search_ast_nodes(q, repo_filter=repo_filter)
        if should_exclude_tests:
            res = [r for r in res if not (r.get("rel_path", "").lower().startswith("test") or "/test" in r.get("rel_path", "").lower())]
        return {"query": q, "result_count": len(res[:limit]), "results": res[:limit]}

    def _exec_memory_search():
        embedder = EmbeddingModel.get()
        qvec = embedder.embed_one(q)
        repo_filter = repo.split(",") if repo and isinstance(repo, str) and "," in repo else repo
        res = ContextDB.vector_search(
            qvec, limit=limit, repo_filter=repo_filter, memory_bank_only=True,
        )
        return {"query": q, "result_count": len(res), "results": res}

    def _exec_graph_search(g_query):
        if not isinstance(repo, str) or "," in repo:
            return {"error": "explicit single repo is required for structural exploration"}
        record = ContextDB.get_repo(repo)
        if not record:
            return {"error": "repository not found"}
        from code_intelligence.runtime import build_service
        explored = build_service().explore(repo, _resolve_repo_path(record["path"]), g_query, max_files=min(limit, 20))
        return {
            "provider": explored.provider,
            "incomplete": explored.incomplete,
            "warnings": explored.warnings,
            "symbols": [s.model_dump(mode="json") for s in explored.symbols],
            "edges": [e.model_dump(mode="json") for e in explored.edges],
        }

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {}
        if search_type in ("all", "code"):
            futures["code_search"] = executor.submit(_exec_code_search)
            futures["structure_search"] = executor.submit(_exec_structure_search)

        if search_type in ("all", "memory"):
            futures["memory_bank_search"] = executor.submit(_exec_memory_search)

        for sec_key, future in futures.items():
            try:
                results[sec_key] = future.result(timeout=30)
            except Exception as e:
                results[sec_key] = {"error": str(e)}

        if "code_search" in results and isinstance(results["code_search"].get("results"), list):
            for item in results["code_search"]["results"]:
                if item.get("rel_path"):
                    top_files.add(item["rel_path"])

        if "memory_bank_search" in results and isinstance(results["memory_bank_search"].get("results"), list):
            for item in results["memory_bank_search"]["results"]:
                if item.get("rel_path"):
                    top_files.add(item["rel_path"])

        if search_type in ("all", "code"):
            graph_queries = set()
            struct_res = results.get("structure_search", {})
            if isinstance(struct_res.get("results"), list):
                for item in struct_res["results"]:
                    if isinstance(item, dict) and item.get("name"):
                        name = item["name"]
                        graph_queries.add(name)
                        if len(top_symbols) < 5:
                            top_symbols.append(name)
                        if item.get("rel_path"):
                            top_files.add(item["rel_path"])

            if not graph_queries:
                graph_queries.add(q)

            graph_futures = {
                g_query: executor.submit(_exec_graph_search, g_query)
                for g_query in sorted(graph_queries)[:5]
            }

            graph_results = {}
            for g_query, g_future in graph_futures.items():
                try:
                    graph_results[g_query] = g_future.result(timeout=30)
                except Exception as e:
                    graph_results[g_query] = {"error": str(e)}

            results["code_graph_search"] = graph_results

    code_cnt = len(results.get("code_search", {}).get("results", [])) if "code_search" in results else 0
    struct_cnt = len(results.get("structure_search", {}).get("results", [])) if "structure_search" in results else 0
    graph_cnt = len(results.get("code_graph_search", {})) if "code_graph_search" in results else 0
    mb_cnt = len(results.get("memory_bank_search", {}).get("results", [])) if "memory_bank_search" in results else 0

    overview_block = {
        "query": q,
        "search_type": search_type,
        "execution": "multithreaded_parallel",
        "summary": (
            f"Parallel research for '{q}' completed. Found {code_cnt} code snippets, {struct_cnt} AST symbol declarations, "
            f"{graph_cnt} dependency graph nodes, and {mb_cnt} memory bank/documentation excerpts."
        ),
        "top_symbols": top_symbols,
        "top_files": sorted(list(top_files))[:8],
    }

    impact_surface_block = _build_impact_surface_internal(results, top_symbols, top_files)

    final_payload = {
        "overview": overview_block,
        "impact_surface": impact_surface_block,
    }
    for key in ("code_search", "structure_search", "code_graph_search", "memory_bank_search"):
        if key in results:
            final_payload[key] = results[key]

    return jsonify(final_payload)
