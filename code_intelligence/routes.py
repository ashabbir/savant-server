"""Versioned native REST API for structural code intelligence."""

from pathlib import Path, PurePosixPath

from flask import Blueprint, jsonify, request

from context.db import ContextDB
from db.jobs import JobDB

from .provider import CodeIntelligenceError, ErrorCategory
from .contracts import SubgraphRequest
from .runtime import build_service

code_intelligence_bp = Blueprint("code_intelligence", __name__)
BASE = "/api/context/code-intelligence/repos/<repo_id>"


def _repo(repo_id):
    record = ContextDB.get_repo_by_identifier(repo_id)
    if not record:
        return None, (jsonify({"error": "repository not found"}), 404)
    return record, None


def _error(exc):
    if isinstance(exc, PermissionError):
        return jsonify({"error": str(exc), "code": "unauthorized"}), 403
    if isinstance(exc, CodeIntelligenceError):
        status = 503 if exc.category in {ErrorCategory.ENGINE_UNAVAILABLE, ErrorCategory.BUSY, ErrorCategory.TIMEOUT} else 400
        if exc.category is ErrorCategory.NOT_INDEXED:
            return jsonify({"provider": "codegraph", "freshness": "unavailable", "incomplete": True,
                            "warnings": [str(exc)], "next_action": "index"}), 200
        return jsonify({"error": str(exc), "code": exc.category.value}), status
    return jsonify({"error": str(exc)}), 400


@code_intelligence_bp.get(f"{BASE}/health")
def provider_health(repo_id):
    record, error = _repo(repo_id)
    if error:
        return error
    try:
        health = build_service().health(repo_id, Path(record["path"]))
        active = JobDB.find_active("codegraph_sync", repo_id) or JobDB.find_active("codegraph_index", repo_id)
        payload = health.model_dump(mode="json")
        payload.update({"capabilities": build_service().registry.get_provider(repo_id).capabilities.model_dump(), "current_job": active})
        return jsonify(payload)
    except Exception as exc:
        return _error(exc)


@code_intelligence_bp.post(f"{BASE}/sync")
def sync(repo_id):
    record, error = _repo(repo_id)
    if error:
        return error
    active = JobDB.find_active("codegraph_sync", repo_id) or JobDB.find_active("codegraph_index", repo_id)
    if active:
        return jsonify({"started": True, "reused": True, "repo_id": repo_id, "provider": "codegraph", "job_id": active["id"]})
    try:
        job = JobDB.create_job("codegraph_sync", repo_id)
    except Exception:
        active = JobDB.find_active("codegraph_sync", repo_id) or JobDB.find_active("codegraph_index", repo_id)
        if not active:
            raise
        job = active
    return jsonify({"started": True, "repo_id": repo_id, "provider": "codegraph", "job_id": job["id"]}), 202


@code_intelligence_bp.get(f"{BASE}/symbols")
def symbols(repo_id):
    record, error = _repo(repo_id)
    if error:
        return error
    query = (request.args.get("q") or "").strip()
    try:
        service = build_service()
        filters = {key: request.args.get(key) for key in ("kind", "language", "path") if request.args.get(key)}
        limit = request.args.get("limit", type=int) or 100
        if query:
            result = service.search_symbols(repo_id, Path(record["path"]), query, filters=filters, limit=limit)
            return jsonify(result.model_dump(mode="json"))
        result = service.list_symbols(repo_id, Path(record["path"]), filters=filters, limit=limit,
                                      cursor=request.args.get("cursor"))
        return jsonify({**result, "items": [item.model_dump(mode="json") for item in result["items"]]})
    except Exception as exc:
        return _error(exc)


@code_intelligence_bp.post(f"{BASE}/explore")
def explore(repo_id):
    record, error = _repo(repo_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    query = str(data.get("query") or data.get("q") or "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    try:
        result = build_service().explore(repo_id, Path(record["path"]), query,
            max_files=data.get("max_files", 5), include_source=data.get("include_source", True))
        return jsonify(result.model_dump(mode="json"))
    except Exception as exc:
        return _error(exc)


@code_intelligence_bp.post(f"{BASE}/subgraph")
def subgraph(repo_id):
    record, error = _repo(repo_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        graph_request = SubgraphRequest.model_validate(data)
        result = build_service().subgraph(repo_id, Path(record["path"]), graph_request)
        return jsonify(result.model_dump(mode="json"))
    except Exception as exc:
        return _error(exc)


@code_intelligence_bp.get(f"{BASE}/source")
def source(repo_id):
    record, error = _repo(repo_id)
    if error:
        return error
    rel = (request.args.get("path") or "").replace("\\", "/")
    parsed = PurePosixPath(rel)
    if not rel or parsed.is_absolute() or ".." in parsed.parts:
        return jsonify({"error": "unsafe repository-relative path", "code": "path_refused"}), 400
    root = Path(record["path"]).resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return jsonify({"error": "path escaped repository root", "code": "path_refused"}), 400
    if not candidate.is_file():
        return jsonify({"error": "source not found"}), 404
    return jsonify({"repo_id": repo_id, "path": rel, "uri": f"{repo_id}:{rel}", "content": candidate.read_text(encoding="utf-8", errors="replace")})


@code_intelligence_bp.post(f"{BASE}/analysis")
def analysis(repo_id):
    """Deterministic findings plus explicitly separate, versioned metric families."""
    record, error = _repo(repo_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    rel = str(data.get("path") or "").replace("\\", "/")
    code = data.get("code")
    diff = data.get("diff")
    if not rel and code is None and diff is None:
        return jsonify({"error": "path, code, or diff required"}), 400

    root = Path(record["path"]).resolve()
    before = ""
    if rel:
        parsed = PurePosixPath(rel)
        if parsed.is_absolute() or ".." in parsed.parts:
            return jsonify({"error": "unsafe repository-relative path", "code": "path_refused"}), 400
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return jsonify({"error": "path escaped repository root", "code": "path_refused"}), 400
        if candidate.is_file():
            before = candidate.read_text(encoding="utf-8", errors="replace")
        elif code is None and diff is None:
            return jsonify({"error": "source not found"}), 404

    from context.analysis import AnalysisTarget, analyze_code
    target = AnalysisTarget(
        path=rel,
        name=(str(data.get("name") or data.get("symbol") or "").strip() or None),
        node_type=(str(data.get("node_type") or "").strip() or None),
    )
    deterministic = analyze_code(
        content_before=before,
        content_after=code,
        target=target,
        diff=diff,
        target_missing_is_new=bool(code is not None and not before),
    )
    graph_metrics = {
        "algorithm": "provider_topology_counts",
        "version": "1",
        "nodes": None,
        "edges": None,
        "warnings": [],
    }
    symbol_ref = data.get("symbol_ref")
    service = build_service()
    try:
        if not symbol_ref and target.name:
            found = service.search_symbols(repo_id, root, target.name, filters={"path": rel} if rel else {}, limit=5)
            exact = [item for item in found.items if item.name == target.name and (not rel or item.location.file_path == rel)]
            if exact:
                symbol_ref = {"id": exact[0].id}
        if symbol_ref:
            topology = service.subgraph(repo_id, root, SubgraphRequest(roots=[symbol_ref]))
            graph_metrics.update({"nodes": len(topology.symbols), "edges": len(topology.edges), "incomplete": topology.incomplete})
        else:
            graph_metrics["warnings"].append("symbol could not be resolved for graph metrics")
    except Exception as exc:
        graph_metrics["warnings"].append(str(exc))

    return jsonify({
        **deterministic,
        "repo_id": repo_id,
        "complexity_metrics": {
            "algorithm": "deterministic_ast",
            "version": "1",
            "before": deterministic["before"]["complexity"],
            "after": deterministic["after"]["complexity"],
            "delta": deterministic["delta"]["complexity"],
        },
        "graph_metrics": graph_metrics,
    })
