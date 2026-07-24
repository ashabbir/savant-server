"""Flask Blueprint for Context API — /api/context/*.

Provides REST endpoints for semantic code search, memory bank,
project management, and indexing.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, g, jsonify, request
from utils.auth import admin_required, ALLOWED_SAVANT_APPS

logger = logging.getLogger(__name__)

context_bp = Blueprint("context", __name__)


def _record_repo_sync_activity(**fields):
    """Write audit history without allowing telemetry failure to mask Git results."""
    try:
        from .db import ContextDB
        fields.setdefault("actor_id", getattr(g, "user_id", "") or "")
        fields.setdefault(
            "source_app",
            (request.headers.get("X-App-Name") or request.headers.get("X-Savant-App") or "").strip().lower(),
        )
        return ContextDB.record_repo_sync_log(**fields)
    except Exception:
        logger.exception(
            "Failed to persist repository sync activity for %s",
            fields.get("repo_name", "unknown"),
        )
        return {}

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
            record = ContextDB.get_repo_by_identifier(repo)
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
                        "provider": "context_ast", "freshness": "fresh", "deprecated": True})
    except Exception as e:
        logger.error(f"AST search failed: {e}")
        return jsonify({"error": str(e), "results": []}), 500


@context_bp.route("/api/context/ast/list")
def ast_list():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    repo = request.args.get("repo_id") or request.args.get("repo")
    if repo and "," not in repo:
        record = ContextDB.get_repo_by_identifier(repo)
        if not record:
            return jsonify({"error": "repository not found", "nodes": []}), 404
        from db.code_intelligence import CodeIntelligenceConfigDB
        config = CodeIntelligenceConfigDB.get(str(record["id"]))
        if config and config.get("provider") == "codegraph":
            try:
                from code_intelligence.runtime import build_service
                service = build_service()
                limit = max(1, min(request.args.get("limit", type=int) or 500, 1000))
                listed = service.list_symbols(str(record["id"]), _resolve_repo_path(record["path"]),
                                              limit=limit, cursor=request.args.get("cursor"))
                health = service.health(str(record["id"]), _resolve_repo_path(record["path"]))
                nodes = [{
                    "repo": record["name"], "path": item.location.file_path,
                    "node_type": item.kind, "name": item.name,
                    "start_line": item.location.start_line, "end_line": item.location.end_line,
                } for item in listed["items"]]
                return jsonify({"ast_count": len(nodes), "nodes": nodes, "provider": listed["provider"],
                                "freshness": health.freshness.value, "graph_version": health.graph_version,
                                "incomplete": listed["incomplete"], "cursor": listed["next_cursor"],
                                "warnings": listed["warnings"], "deprecated": True})
            except Exception:
                pass
    repo_filter = repo.split(",") if repo and "," in repo else repo
    nodes = ContextDB.list_ast_nodes(repo_filter)
    return jsonify({"ast_count": len(nodes), "nodes": nodes})


@context_bp.route("/api/context/analysis", methods=["POST"])
def analyze():
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    params = _analysis_request_params(data)
    if not params["path"] and not params["code"] and not params["diff"]:
        return jsonify({"error": "path, uri, code, or diff required"}), 400
    if params["repo"] and params["path"] and not _analysis_file_allowed(params["repo"], params["path"]):
        return jsonify({"error": "Analysis is limited to tracked, non-ignored repository source files"}), 404
    return jsonify(_execute_analysis(params))


def _request_text(value) -> str:
    return "" if value is None else str(value).strip()


def _analysis_request_params(data: dict) -> dict:
    repo = _request_text(data.get("repo"))
    path = _request_text(data.get("path"))
    uri = _request_text(data.get("uri"))
    if not path and uri:
        repo_part, separator, path_part = uri.partition(":")
        repo = repo or (repo_part if separator else "")
        path = path_part if separator else uri
    name = _request_text(data.get("name") or data.get("class_name") or data.get("symbol")) or None
    node_type = _request_text(data.get("node_type")) or None
    return {
        "repo": repo, "path": path, "uri": uri, "name": name, "node_type": node_type,
        "diff": str(data["diff"]) if data.get("diff") is not None else None,
        "code": str(data["code"]) if data.get("code") is not None else None,
    }


def _analysis_file_allowed(repo: str, path: str) -> bool:
    from .db import ContextDB
    from .walker import FileWalker

    repo_record = ContextDB.get_repo_by_identifier(repo)
    if not repo_record:
        return False
    repo_root = Path(repo_record.get("path", "")).resolve(strict=False)
    if not repo_root.exists():
        return True
    return FileWalker(repo_root, tracked_only=False).is_allowed(path)


def _execute_analysis(params: dict) -> dict:
    from .analysis import AnalysisTarget, analyze_code
    from .db import ContextDB

    before_text = ""
    if params["code"] is None and params["repo"] and params["path"]:
        current = ContextDB.read_code_file(f"{params['repo']}:{params['path']}")
        before_text = (current or {}).get("content", "")
    target = AnalysisTarget(
        path=params["path"] or params["uri"], name=params["name"], node_type=params["node_type"]
    )
    result = analyze_code(
        content_before=before_text, content_after=params["code"], target=target,
        diff=params["diff"], target_missing_is_new=bool(params["code"] is not None and not before_text),
    )
    result.update({
        "repo": params["repo"], "path": params["path"],
        "uri": params["uri"] or (
            f"{params['repo']}:{params['path']}" if params["repo"] and params["path"] else params["path"]
        ),
    })
    return result


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


@context_bp.route("/api/context/repos/periodic-sync/status")
def periodic_sync_status():
    """Get status of the 6-hour periodic repository sync runner."""
    from .periodic_runner import get_runner_status
    return jsonify(get_runner_status())


@context_bp.route("/api/context/repos/periodic-sync/logs")
def periodic_sync_logs():
    """Retrieve execution log history of periodic 6-hour sync runs."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    repo_name = request.args.get("repo_name") or request.args.get("repo")
    limit = min(500, max(1, request.args.get("limit", type=int) or 50))
    logs = ContextDB.list_periodic_sync_logs(repo_name=repo_name, limit=limit)
    return jsonify({"count": len(logs), "logs": logs})


@context_bp.route("/api/context/repos/sync-logs")
def repo_sync_logs():
    """Retrieve activity history for all repositories, optionally filtered by name."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    repo_name = request.args.get("repo_name") or request.args.get("repo")
    limit = min(500, max(1, request.args.get("limit", type=int) or 50))
    logs = ContextDB.list_repo_sync_logs(repo_name=repo_name, limit=limit)
    return jsonify({"count": len(logs), "logs": logs})


@context_bp.route("/api/context/repos/<name>/sync-logs")
def repository_sync_logs(name):
    """Retrieve activity history for one repository."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .db import ContextDB
    limit = min(500, max(1, request.args.get("limit", type=int) or 50))
    logs = ContextDB.list_repo_sync_logs(repo_name=name, limit=limit)
    return jsonify({"count": len(logs), "logs": logs})


@context_bp.route("/api/context/repos/periodic-sync/run", methods=["POST"])
@admin_required
def trigger_periodic_sync_all():
    """Manually trigger an immediate periodic sync pass for all registered projects."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503
    from .periodic_runner import run_periodic_sync_now
    res = run_periodic_sync_now()
    return jsonify(res)


@context_bp.route("/api/context/repos", methods=["POST"])
@admin_required
def add_repo():
    """Add a project from a configured source (does NOT index it)."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    data = request.get_json(force=True) or {}
    source = (data.get("source") or "").strip().lower()
    branch = (data.get("branch") or "").strip() or None
    started_at = perf_counter()
    activity_repo_name = ""

    from .db import ContextDB

    if source not in {"github", "gitlab", "directory"}:
        return jsonify({"error": "source must be one of: github, gitlab, directory"}), 400

    try:
        if source in {"github", "gitlab"}:
            from .ingestion import IngestionError, detect_repo_provider, ingest_repo

            url = (data.get("url") or "").strip()
            activity_repo_name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") or "unknown"
            provider = detect_repo_provider(url)
            if provider != source:
                return jsonify({"error": f"URL does not match source '{source}'"}), 400
            ingested = ingest_repo(url=url, branch=branch)
        else:
            from .ingestion import IngestionError, ingest_directory

            directory = (data.get("directory") or "").strip()
            ingested = ingest_directory(directory=directory)
    except IngestionError as e:
        if source in {"github", "gitlab"}:
            _record_repo_sync_activity(
                repo_name=activity_repo_name or "unknown", operation="clone",
                trigger="project_add", provider=source, branch=branch or "",
                status="failed", duration_ms=int((perf_counter() - started_at) * 1000),
                error=str(e), details="Repository clone failed",
            )
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
    if source in {"github", "gitlab"}:
        ContextDB.mark_repo_fetched(ingested.name)
        _record_repo_sync_activity(
            repo_name=ingested.name, operation=ingested.operation or "clone", trigger="project_add",
            provider=ingested.provider, branch=ingested.branch,
            status="success", after_commit=ingested.after_commit,
            fetched=True, code_changed=ingested.changed,
            duration_ms=int((perf_counter() - started_at) * 1000),
            details=(
                "Repository refreshed during project add"
                if ingested.operation == "refresh" else "Repository cloned"
            ),
        )
        repo = ContextDB.get_repo(ingested.name)
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
    started_at = perf_counter()

    try:
        refreshed = update_repo(str(repo_path))
    except IngestionError as e:
        _record_repo_sync_activity(
            repo_name=name, operation="refresh", trigger="manual", status="failed",
            duration_ms=int((perf_counter() - started_at) * 1000), error=str(e),
            details="Repository refresh failed",
        )
        return jsonify({"error": str(e)}), 400

    ContextDB.add_repo(refreshed.name, refreshed.path)
    ContextDB.mark_repo_fetched(refreshed.name)
    _record_repo_sync_activity(
        repo_name=refreshed.name, operation="refresh", trigger="manual",
        provider=refreshed.provider, branch=refreshed.branch, status="success",
        before_commit=refreshed.before_commit, after_commit=refreshed.after_commit,
        fetched=True, code_changed=refreshed.changed,
        duration_ms=int((perf_counter() - started_at) * 1000),
        details="Repository refreshed",
    )

    # Check for differential index & graph generation requirements:
    # 1. Provider is GitHub or GitLab
    # 2. Local code changed after fetch/pull
    # 3. Project was previously indexed and graphed
    differential_job_id = None
    if getattr(refreshed, "provider", "git") in {"github", "gitlab"} and getattr(refreshed, "changed", False):
        is_indexed = (repo.get("status") in {"indexed", "ast_only"}) or bool(repo.get("indexed_at"))
        from db.code_intelligence import CodeIntelligenceConfigDB
        config = CodeIntelligenceConfigDB.get(name) or CodeIntelligenceConfigDB.get(str(repo.get("id")))
        is_graphed = bool(config and config.get("provider") == "codegraph")

        if is_indexed and is_graphed:
            from db.jobs import JobDB
            existing = JobDB.find_active("differential_sync", name)
            if not existing:
                job = JobDB.create_job("differential_sync", name)
                differential_job_id = job["id"]
            else:
                differential_job_id = existing["id"]

    res = ContextDB.get_repo(refreshed.name) or {}
    if differential_job_id:
        res["differential_sync_job_id"] = differential_job_id
        res["differential_sync_triggered"] = True
    return jsonify(res)


@context_bp.route("/api/context/repos/<name>/differential-sync", methods=["POST"])
@admin_required
def trigger_differential_sync(name):
    """Manually trigger a differential index and graph sync via the job queue."""
    if not _ensure_init():
        return jsonify({"error": "Context not initialized"}), 503

    from .db import ContextDB
    repo = ContextDB.get_repo(name)
    if not repo:
        return jsonify({"error": f"Project not found: {name}"}), 404
    repo_path, path_err = _validate_repo_path(repo)
    if path_err:
        return jsonify({"error": path_err}), 400

    from .ingestion import inspect_project_source
    source_info = inspect_project_source(str(repo_path))
    provider = source_info.get("source")
    if provider not in {"github", "gitlab"}:
        return jsonify({"error": "Differential sync is only supported for GitHub or GitLab repositories"}), 400

    is_indexed = (repo.get("status") in {"indexed", "ast_only"}) or bool(repo.get("indexed_at"))
    from db.code_intelligence import CodeIntelligenceConfigDB
    config = CodeIntelligenceConfigDB.get(name) or CodeIntelligenceConfigDB.get(str(repo.get("id")))
    is_graphed = bool(config and config.get("provider") == "codegraph")

    if not (is_indexed and is_graphed):
        return jsonify({"error": "Repository must be already indexed and graphed before differential sync can run"}), 400

    from db.jobs import JobDB
    existing = JobDB.find_active("differential_sync", name)
    if existing:
        return jsonify({"started": True, "name": name, "job_id": existing["id"], "reused": True})

    job = JobDB.create_job("differential_sync", name)
    return jsonify({"started": True, "name": name, "job_id": job["id"]})


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
    live = get_indexing_status()
    live = live if isinstance(live, dict) else {}
    _merge_active_index_jobs(live)
    _merge_persisted_repo_status(live, ContextDB)
    _mark_stalled_indexing(live)
    return jsonify(live)


def _index_status_base(**overrides) -> dict:
    status = {
        "status": "added", "phase": "", "progress": 0, "total": 0,
        "files_done": 0, "chunks_done": 0, "current_file": "", "errors": 0,
    }
    status.update(overrides)
    return status


def _merge_active_index_jobs(live: dict) -> None:
    try:
        from db.jobs import JobDB
        jobs = JobDB.list_jobs(limit=50)
    except Exception:
        return
    for job in jobs:
        target = job.get("target")
        if not target or target == "__all__":
            continue
        job_type = str(job.get("job_type") or "")
        if job_type in {"codegraph_sync", "codegraph_index"}:
            try:
                from context.db import ContextDB
                repo = ContextDB.get_repo_by_identifier(str(target))
                repo_name = repo.get("name") if repo else str(target)
            except Exception:
                repo_name = str(target)
            current = live.setdefault(repo_name, _persisted_repo_status(repo) if repo else _index_status_base())
            if "structural_job" in current:
                continue
            job_status = str(job.get("status") or "")
            finished_at = _status_timestamp(job.get("finished_at"))
            is_recent_terminal = (
                job_status in {"done", "failed", "cancelled"}
                and finished_at is not None
                and (datetime.now(timezone.utc) - finished_at).total_seconds() <= 600
            )
            if job_status in {"queued", "running", "cancelling"} or is_recent_terminal:
                current["structural_job"] = job
            continue
        if job.get("status") not in {"queued", "running", "cancelling"}:
            continue
        if live.get(target, {}).get("status") == "indexing":
            continue
        live[target] = _index_status_base(
            status=("cancelling" if job.get("status") == "cancelling"
                    else "indexing" if job.get("status") == "running" else "queued"),
            phase=job.get("phase", ""), progress=job.get("progress", 0),
            current_file=job.get("message", ""), job_id=job.get("id"),
            job_type=job.get("job_type", ""),
        )


def _persisted_repo_status(repo: dict) -> dict:
    repo_status = repo.get("status", "added")
    if repo_status == "indexing":
        return _index_status_base(
            status="stalled", phase="No active worker",
            error=(
                "This repo is marked as indexing in the database, but no live index worker is running. "
                "The previous job likely exited early or the app was restarted. Use Stop Indexing to reset it, then retry."
            ),
        )
    complete = repo_status == "indexed"
    return _index_status_base(
        status=repo_status, phase="Complete" if complete else "",
        progress=100 if complete else 0, total=repo.get("file_count", 0),
        files_done=repo.get("file_count", 0), chunks_done=repo.get("chunk_count", 0),
    )


def _merge_persisted_repo_status(live: dict, context_db) -> None:
    try:
        repos = context_db.list_repos()
    except Exception:
        return
    for repo in repos:
        name = repo.get("name")
        if name and name not in live:
            live[name] = _persisted_repo_status(repo)


def _status_timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _mark_stalled_indexing(live: dict) -> None:
    now = datetime.now(timezone.utc)
    for item in live.values():
        updated_at = _status_timestamp(item.get("updated_at"))
        if item.get("status") != "indexing" or updated_at is None:
            continue
        age = (now - updated_at).total_seconds()
        if age < 180:
            continue
        item.update({
            "status": "stalled",
            "phase": "No recent progress",
            "error": (
                f"No index progress has been reported for {int(age)} seconds. "
                "The worker may be hung on model load, file IO, or embedding generation."
            ),
        })


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
    from .impact import build_impact_surface
    return build_impact_surface(results, top_symbols, top_files)


def _parse_repo_ids(repo) -> list[str]:
    if isinstance(repo, str):
        return [item.strip() for item in repo.split(",") if item.strip()]
    if isinstance(repo, list):
        return [str(item).strip() for item in repo if str(item).strip()]
    return []


def _exec_code_search(q: str, repo: str | None, limit: int, should_exclude_tests: bool) -> dict:
    from .db import ContextDB
    from .embeddings import EmbeddingModel
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


def _exec_structure_search(q: str, repo: str | None, repo_ids: list[str], limit: int, should_exclude_tests: bool) -> dict:
    from .db import ContextDB
    if repo_ids:
        from code_intelligence.runtime import build_service
        service = build_service()
        results_by_repo = []
        warnings = []
        incomplete = False
        providers = set()
        for repo_id in repo_ids:
            record = ContextDB.get_repo(repo_id)
            if not record:
                warnings.append(f"{repo_id}: repository not found")
                incomplete = True
                continue
            try:
                search_result = service.search_symbols(
                    repo_id, _resolve_repo_path(record["path"]), q, limit=limit
                )
            except Exception as exc:
                warnings.append(f"{repo_id}: {exc}")
                incomplete = True
                continue
            providers.add(search_result.provider)
            incomplete = incomplete or search_result.incomplete
            warnings.extend(f"{repo_id}: {warning}" for warning in search_result.warnings)
            results_by_repo.append([
                {"id": s.id, "node_type": s.kind, "name": s.name,
                 "start_line": s.location.start_line, "end_line": s.location.end_line,
                 "rel_path": s.location.file_path, "repo": repo_id,
                 "qualified_name": s.qualified_name, "signature": s.signature}
                for s in search_result.items
            ])
        res = []
        for index in range(limit):
            for repo_results in results_by_repo:
                if index < len(repo_results):
                    res.append(repo_results[index])
                    if len(res) == limit:
                        break
            if len(res) == limit:
                break
        provider = next(iter(providers)) if len(providers) == 1 else "multi_repo"
        if len(repo_ids) > 1:
            provider = "multi_repo"
        return {"query": q, "result_count": len(res), "results": res,
                "provider": provider, "incomplete": incomplete, "warnings": warnings}

    repo_filter = repo.split(",") if repo and isinstance(repo, str) and "," in repo else repo
    res = ContextDB.search_ast_nodes(q, repo_filter=repo_filter)
    if should_exclude_tests:
        res = [r for r in res if not (r.get("rel_path", "").lower().startswith("test") or "/test" in r.get("rel_path", "").lower())]
    return {"query": q, "result_count": len(res[:limit]), "results": res[:limit]}


def _exec_memory_search(q: str, repo: str | None, limit: int) -> dict:
    from .db import ContextDB
    from .embeddings import EmbeddingModel
    embedder = EmbeddingModel.get()
    qvec = embedder.embed_one(q)
    repo_filter = repo.split(",") if repo and isinstance(repo, str) and "," in repo else repo
    res = ContextDB.vector_search(
        qvec, limit=limit, repo_filter=repo_filter, memory_bank_only=True,
    )
    return {"query": q, "result_count": len(res), "results": res}


def _exec_graph_search(g_query: str, repo_ids: list[str], limit: int) -> dict:
    from code_intelligence.runtime import build_service
    from .db import ContextDB
    if not repo_ids:
        return {"error": "explicit repo is required for structural exploration"}

    service = build_service()
    repository_results = {}
    combined_symbols = []
    combined_edges = []
    combined_warnings = []
    incomplete = False
    for repo_id in repo_ids:
        record = ContextDB.get_repo(repo_id)
        if not record:
            repository_results[repo_id] = {"error": "repository not found"}
            combined_warnings.append(f"{repo_id}: repository not found")
            incomplete = True
            continue
        try:
            explored = service.explore(
                repo_id,
                _resolve_repo_path(record["path"]),
                g_query,
                max_files=min(limit, 20),
            )
        except Exception as exc:
            repository_results[repo_id] = {"error": str(exc)}
            combined_warnings.append(f"{repo_id}: {exc}")
            incomplete = True
            continue
        repo_result = {
            "provider": explored.provider,
            "incomplete": explored.incomplete,
            "warnings": explored.warnings,
            "symbols": [s.model_dump(mode="json") for s in explored.symbols],
            "edges": [e.model_dump(mode="json") for e in explored.edges],
        }
        repository_results[repo_id] = repo_result
        incomplete = incomplete or explored.incomplete
        combined_warnings.extend(f"{repo_id}: {warning}" for warning in explored.warnings)
        combined_symbols.extend(repo_result["symbols"])
        combined_edges.extend(repo_result["edges"])

    if len(repo_ids) == 1 and repo_ids[0] in repository_results:
        return repository_results[repo_ids[0]]
    return {
        "provider": "multi_repo",
        "incomplete": incomplete,
        "warnings": combined_warnings,
        "symbols": combined_symbols,
        "edges": combined_edges,
        "repositories": repository_results,
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
    repo_ids = _parse_repo_ids(repo)
    search_type = (data.get("type") or "all").lower().strip()
    allowed_types = ("all", "code", "memory")
    if search_type not in allowed_types:
        return jsonify({
            "error": "type must be one of: all, code, memory",
            "allowed_types": list(allowed_types),
        }), 400
    limit = int(data.get("limit", 20))
    exclude_tests = bool(data.get("exclude_tests", True))
    should_exclude_tests = exclude_tests and "test" not in q.lower()

    results = {}
    top_symbols = []
    top_files = set()

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {}
        if search_type in ("all", "code"):
            futures["code_search"] = executor.submit(
                _exec_code_search, q, repo, limit, should_exclude_tests
            )
            futures["structure_search"] = executor.submit(
                _exec_structure_search, q, repo, repo_ids, limit, should_exclude_tests
            )

        if search_type in ("all", "memory"):
            futures["memory_bank_search"] = executor.submit(
                _exec_memory_search, q, repo, limit
            )

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
                g_query: executor.submit(_exec_graph_search, g_query, repo_ids, limit)
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
