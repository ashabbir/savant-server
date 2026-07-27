"""Workspace and Session Links Routes Blueprint for Savant Server."""

import os
import json
from pathlib import Path
from flask import Blueprint, g, jsonify, request
from db.workspaces import WorkspaceDB
from db.workspace_session_links import WorkspaceSessionLinkDB
from db.tasks import TaskDB
from db.notes import NoteDB
from db.merge_requests import MergeRequestDB
from db.knowledge_graph import KnowledgeGraphDB
from server_paths import get_server_data_dir

workspaces_bp = Blueprint("workspaces", __name__)


def _normalize_provider_name(provider: str) -> str:
    prov = (provider or "").strip().lower()
    if prov in ("claude", "claude_desktop", "claude-code", "claude_code"):
        return "claude"
    if prov in ("codex", "openai", "codex_cli"):
        return "codex"
    if prov in ("gemini", "gemini_cli", "google_gemini"):
        return "gemini"
    if prov in ("savant", "savant_agent", "savant_cli"):
        return "savant"
    return prov or "unknown"


@workspaces_bp.route("/api/workspaces", methods=["GET"])
def api_workspaces_list():
    user_id = getattr(g, "user_id", "")
    include_archived = request.args.get("include_archived", "false").lower() in ("true", "1", "yes")
    status_filter = request.args.get("status")

    workspaces = WorkspaceDB.list_all(
        user_id=user_id,
        status=status_filter,
        include_archived=include_archived
    )
    links = WorkspaceSessionLinkDB.list_all(user_id=user_id)
    links_by_ws = {}
    for link in links:
        ws_id = link["workspace_id"]
        if ws_id not in links_by_ws:
            links_by_ws[ws_id] = []
        links_by_ws[ws_id].append(link)

    for ws in workspaces:
        ws_id = ws["workspace_id"]
        ws["sessions"] = links_by_ws.get(ws_id, [])
        ws["session_count"] = len(ws["sessions"])

    return jsonify(workspaces)


@workspaces_bp.route("/api/workspaces", methods=["POST"])
def api_workspaces_create():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Workspace name is required"}), 400

    user_id = getattr(g, "user_id", "")
    created = WorkspaceDB.create({
        "name": name,
        "description": data.get("description", ""),
        "priority": data.get("priority", "medium"),
        "status": data.get("status", "open"),
        "user_id": user_id,
    })
    return jsonify(created), 201


@workspaces_bp.route("/api/workspaces/reorder", methods=["POST"])
def api_workspaces_reorder():
    data = request.get_json(force=True, silent=True) or {}
    ordered_ids = data.get("ordered_ids", [])
    if not isinstance(ordered_ids, list):
        return jsonify({"error": "ordered_ids must be a list"}), 400
    user_id = getattr(g, "user_id", "")
    WorkspaceDB.reorder(user_id, ordered_ids)
    return jsonify({"status": "reordered", "count": len(ordered_ids)})


@workspaces_bp.route("/api/workspaces/<ws_id>", methods=["PUT", "DELETE"])
def api_workspace_detail(ws_id):
    user_id = getattr(g, "user_id", "")
    ws = WorkspaceDB.get_by_id(ws_id, user_id=user_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404

    if request.method == "DELETE":
        WorkspaceDB.delete(ws_id, user_id=user_id)
        WorkspaceSessionLinkDB.delete_by_workspace(ws_id, user_id=user_id)
        return jsonify({"status": "deleted"}), 200

    data = request.get_json(force=True, silent=True) or {}
    updated = WorkspaceDB.update(ws_id, data, user_id=user_id)
    return jsonify(updated)


@workspaces_bp.route("/api/workspaces/<ws_id>/session-links", methods=["GET"])
def api_workspace_session_links_list(ws_id):
    links = WorkspaceSessionLinkDB.list_by_workspace(ws_id, user_id=getattr(g, "user_id", ""))
    return jsonify({"links": links})


@workspaces_bp.route("/api/workspaces/<ws_id>/session-links", methods=["POST"])
def api_workspace_session_links_upsert(ws_id):
    user_id = getattr(g, "user_id", "")
    data = request.get_json(force=True, silent=True) or {}
    provider = _normalize_provider_name(data.get("provider"))
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    link = WorkspaceSessionLinkDB.upsert(ws_id, provider, session_id)
    return jsonify(link), 200


@workspaces_bp.route("/api/workspaces/<ws_id>/session-links/<provider>/<session_id>", methods=["DELETE"])
def api_workspace_session_links_delete(ws_id, provider, session_id):
    user_id = getattr(g, "user_id", "")
    norm_provider = _normalize_provider_name(provider)
    deleted = WorkspaceSessionLinkDB.delete_from_workspace(ws_id, norm_provider, session_id)
    if not deleted:
        return jsonify({"error": "Session link not found"}), 404
    return jsonify({"deleted": True}), 200


@workspaces_bp.route("/api/session-links/resolve", methods=["GET"])
def api_session_links_resolve():
    session_id = request.args.get("session_id", "").strip()
    provider = request.args.get("provider", "").strip()
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    user_id = getattr(g, "user_id", "")
    link = WorkspaceSessionLinkDB.resolve(_normalize_provider_name(provider), session_id)
    if not link:
        return jsonify({"workspace_id": None, "workspace": None}), 200

    ws = WorkspaceDB.get_by_id(link["workspace_id"], user_id=user_id)
    return jsonify({
        "workspace_id": link["workspace_id"],
        "workspace": ws,
        "provider": link["provider"],
        "attached_at": link.get("attached_at"),
    })


@workspaces_bp.route("/api/workspaces/<ws_id>/context", methods=["GET"])
def api_workspace_context(ws_id):
    user_id = getattr(g, "user_id", "")
    ws = WorkspaceDB.get_by_id(ws_id, user_id=user_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404

    tasks = TaskDB.list_by_workspace(ws_id, user_id=user_id)
    links = WorkspaceSessionLinkDB.list_by_workspace(ws_id, user_id=user_id)
    notes = NoteDB.list_by_workspace(ws_id, user_id=user_id)

    return jsonify({
        "workspace": ws,
        "task_count": len(tasks),
        "session_count": len(links),
        "note_count": len(notes),
        "tasks": tasks,
        "sessions": links,
        "notes": notes,
    })


@workspaces_bp.route("/api/workspaces/<ws_id>/notes", methods=["GET"])
def api_workspace_notes(ws_id):
    user_id = getattr(g, "user_id", "")
    notes = NoteDB.list_by_workspace(ws_id, user_id=user_id)
    return jsonify(notes)


@workspaces_bp.route("/api/workspaces/search", methods=["GET"])
def api_workspaces_search():
    q = (request.args.get("q") or "").strip().lower()
    user_id = getattr(g, "user_id", "")
    workspaces = WorkspaceDB.list_all(user_id=user_id)
    tasks = TaskDB.list_all(user_id=user_id)

    if not q or len(q) < 2:
        return jsonify({"results": [], "workspaces": [], "tasks": []})

    results = []
    matching_workspaces = []
    matching_tasks = []

    for ws in workspaces:
        if q in ws.get("name", "").lower() or q in ws.get("description", "").lower():
            res_item = {"type": "workspace", "id": ws["workspace_id"], "title": ws["name"], "detail": ws.get("description", "")}
            results.append(res_item)
            matching_workspaces.append(ws)

    for t in tasks:
        if q in t.get("title", "").lower() or q in t.get("description", "").lower():
            res_item = {"type": "task", "id": t["task_id"], "workspace_id": t.get("workspace_id"), "title": t["title"], "detail": t.get("description", "")}
            results.append(res_item)
            t_item = dict(t)
            t_item["id"] = t["task_id"]
            matching_tasks.append(t_item)

    return jsonify({"results": results, "workspaces": matching_workspaces, "tasks": matching_tasks})


api_workspace_search = api_workspaces_search
api_workspaces = api_workspaces_list


@workspaces_bp.route("/api/workspaces/<workspace_id>/session-files", methods=["GET"])
def api_workspace_session_files(workspace_id):
    user_id = getattr(g, "user_id", "")
    links = WorkspaceSessionLinkDB.list_by_workspace(workspace_id, user_id=user_id)
    files = []
    for link in links:
        files.append({
            "session_id": link["session_id"],
            "provider": link["provider"],
            "attached_at": link.get("attached_at"),
        })
    return jsonify({"workspace_id": workspace_id, "files": files})
