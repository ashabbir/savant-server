"""Collaborative notebook REST API."""

import base64
import binascii
import hashlib
import json
import os
import re
from urllib.parse import quote

import psycopg2
from flask import Blueprint, Response, g, jsonify, request

from db.engrams import EngramConflictError, EngramDB
from db.notebooks import NotebookDB
from db.notebooks import NotebookMutationError
from db.users import UserDB
from hardening import safe_limit


notebooks_bp = Blueprint("notebooks", __name__)

SOURCE_TYPES = {"file", "directory", "url", "savant_context_repo"}
MEMORY_TYPES = {
    "decision",
    "turning_point",
    "assumption",
    "rejected_approach",
    "open_question",
    "working_state",
}
ENGRAM_TYPES = MEMORY_TYPES | {"fact"}
ENGRAM_STATUSES = {
    "candidate",
    "accepted",
    "rejected",
    "resolved",
    "superseded",
    "retracted",
}
MEMBER_ROLES = {"editor", "viewer"}
WRITE_ROLES = {"owner", "editor"}
COVER_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_COVER_BYTES = 2 * 1024 * 1024
MAX_SOURCE_TEXT = 1024 * 1024
MAX_EVENT_CONTENT = 256 * 1024
MAX_ENGRAM_CONTENT = 128 * 1024
MAX_COMPACTION_CONTENT = 256 * 1024
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
RENDITION_STATUSES = {"pending", "rendering", "ready", "failed", "cancelled"}
RENDITION_STATUS_UPDATES = RENDITION_STATUSES - {"ready"}
MAX_RENDITION_BYTES = 100 * 1024 * 1024
RENDITION_MEDIA_TYPES = {
    "markdown": {"text/markdown"},
    "pdf": {"application/pdf"},
    "csv": {"text/csv"},
    "pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    },
    "html": {"text/html"},
    "m4a": {"audio/mp4", "audio/aac"},
    "mp4": {"video/mp4"},
}
RENDITION_EXTENSIONS = {
    "markdown": ".md",
    "pdf": ".pdf",
    "csv": ".csv",
    "pptx": ".pptx",
    "html": ".html",
    "m4a": ".m4a",
    "mp4": ".mp4",
}


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _access(notebook_id: str, write: bool = False, owner: bool = False):
    notebook = NotebookDB.get_access(notebook_id, g.user_id)
    if not notebook:
        return None, (jsonify({"error": "Notebook not found"}), 404)
    role = notebook["access_role"]
    if owner and role != "owner":
        return None, (jsonify({"error": "Owner permission required"}), 403)
    if write and role not in WRITE_ROLES:
        return None, (jsonify({"error": "Editor permission required"}), 403)
    return notebook, None


def _require_text(data: dict, field: str):
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        return None, (jsonify({"error": f"{field} is required"}), 400)
    return value.strip(), None


def _text(data: dict, field: str, maximum: int, required: bool = False):
    value = data.get(field)
    if value is None and not required:
        return "", None
    if not isinstance(value, str) or (required and not value.strip()):
        return None, (jsonify({"error": f"{field} must be a string"}), 400)
    if len(value) > maximum:
        return None, (
            jsonify({"error": f"{field} exceeds maximum length of {maximum}"}),
            400,
        )
    return value.strip() if required else value, None


def _limit(default=100, maximum=250):
    raw = request.args.get("limit")
    try:
        requested = int(raw) if raw is not None else None
    except ValueError:
        requested = None
    return safe_limit(requested, default=default, maximum=maximum)


def _paged(items: list[dict], limit: int, id_field: str):
    has_more = len(items) > limit
    visible = items[:limit]
    return {
        "items": visible,
        "next_cursor": visible[-1][id_field] if has_more and visible else None,
    }


def _valid_json_object(data: dict, field: str, maximum=16 * 1024):
    value = data.get(field, {})
    if not isinstance(value, dict):
        return None, (jsonify({"error": f"{field} must be an object"}), 400)
    if len(json.dumps(value, separators=(",", ":")).encode()) > maximum:
        return None, (jsonify({"error": f"{field} is too large"}), 400)
    return value, None


def _integrity_error(error):
    message = getattr(error, "diag", None)
    detail = getattr(message, "message_detail", "") if message else ""
    return jsonify({"error": "Referenced resource is invalid", "detail": detail}), 400


def _valid_cover(content: bytes, media_type: str) -> bool:
    if media_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if media_type == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False


def _valid_metadata(data: dict):
    _, error = _valid_json_object(data, "metadata")
    return error


def _multipart_json(field: str, maximum=32 * 1024):
    raw = request.form.get(field, "{}")
    if len(raw.encode()) > maximum:
        return None, (jsonify({"error": f"{field} is too large"}), 400)
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None, (jsonify({"error": f"{field} must be valid JSON"}), 400)
    if not isinstance(value, dict):
        return None, (jsonify({"error": f"{field} must be an object"}), 400)
    return value, None


def _valid_rendition_signature(content: bytes, rendition_format: str) -> bool:
    if rendition_format == "pdf":
        return content.startswith(b"%PDF")
    if rendition_format == "pptx":
        return content.startswith(b"PK\x03\x04")
    if rendition_format in {"m4a", "mp4"}:
        return len(content) >= 12 and content[4:8] == b"ftyp"
    if rendition_format in {"markdown", "csv", "html"}:
        try:
            content.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    return False


def _rendition_version(notebook_id: str, artifact_id: str, version_number: int):
    return NotebookDB.get_artifact_version(notebook_id, artifact_id, version_number)


@notebooks_bp.route("/api/notebooks", methods=["GET"])
def list_notebooks():
    return jsonify(
        NotebookDB.list_for_user(
            g.user_id,
            _limit(default=50, maximum=200),
            request.args.get("cursor"),
        )
    )


@notebooks_bp.route("/api/notebooks", methods=["POST"])
def create_notebook():
    data = _body()
    title, error = _text(data, "title", 200, required=True)
    if error:
        return error
    for field, maximum in (("description", 5000), ("objective", 5000), ("tagline", 300)):
        _, field_error = _text(data, field, maximum)
        if field_error:
            return field_error
    visibility = data.get("visibility", "private")
    if visibility not in {"private", "shared"}:
        return jsonify({"error": "visibility must be private or shared"}), 400
    cover_style, style_error = _valid_json_object(data, "cover_style", 4096)
    if style_error:
        return style_error
    notebook = NotebookDB.create(
        {
            "title": title,
            "description": data.get("description", ""),
            "objective": data.get("objective", ""),
            "tagline": data.get("tagline", ""),
            "visibility": visibility,
            "cover_style": cover_style,
        },
        g.user_id,
    )
    return jsonify(notebook), 201


@notebooks_bp.route("/api/notebooks/<notebook_id>", methods=["GET"])
def get_notebook(notebook_id):
    notebook, error = _access(notebook_id)
    return error or jsonify(notebook)


@notebooks_bp.route("/api/notebooks/<notebook_id>", methods=["PATCH"])
def update_notebook(notebook_id):
    _, error = _access(notebook_id, owner=True)
    if error:
        return error
    data = _body()
    for field, maximum, required in (
        ("title", 200, True),
        ("description", 5000, False),
        ("objective", 5000, False),
        ("tagline", 300, False),
    ):
        if field in data:
            _, field_error = _text(data, field, maximum, required=required)
            if field_error:
                return field_error
    if "visibility" in data and data["visibility"] not in {"private", "shared"}:
        return jsonify({"error": "visibility must be private or shared"}), 400
    if "cover_style" in data:
        _, style_error = _valid_json_object(data, "cover_style", 4096)
        if style_error:
            return style_error
    if "runtime_settings" in data:
        settings, settings_error = _valid_json_object(data, "runtime_settings", 4096)
        if settings_error:
            return settings_error
        for key in ("provider", "model"):
            value = settings.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 200):
                return jsonify({"error": f"runtime_settings.{key} must be a string of at most 200 characters"}), 400
    return jsonify(NotebookDB.update(notebook_id, data, g.user_id))


@notebooks_bp.route("/api/notebooks/<notebook_id>/cover", methods=["POST"])
def upload_cover(notebook_id):
    _, error = _access(notebook_id, owner=True)
    if error:
        return error
    style = {}
    if request.files:
        uploaded = request.files.get("file")
        if not uploaded:
            return jsonify({"error": "file is required"}), 400
        media_type = (uploaded.mimetype or "").lower()
        content = uploaded.stream.read(MAX_COVER_BYTES + 1)
        style_raw = request.form.get("style", "{}")
        try:
            style = json.loads(style_raw)
        except json.JSONDecodeError:
            return jsonify({"error": "style must be valid JSON"}), 400
    else:
        data = _body()
        media_type = str(data.get("media_type") or "").lower()
        encoded = data.get("content_base64")
        if not isinstance(encoded, str):
            return jsonify({"error": "content_base64 is required"}), 400
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return jsonify({"error": "content_base64 is invalid"}), 400
        style = data.get("style") or {}
    if media_type not in COVER_TYPES:
        return jsonify({"error": "Unsupported cover media_type"}), 400
    if not content or len(content) > MAX_COVER_BYTES:
        return jsonify({"error": f"Cover must be 1-{MAX_COVER_BYTES} bytes"}), 400
    if not _valid_cover(content, media_type):
        return jsonify({"error": "Cover bytes do not match media_type"}), 400
    if not isinstance(style, dict) or len(json.dumps(style).encode()) > 4096:
        return jsonify({"error": "style must be a small object"}), 400
    notebook = NotebookDB.set_cover(notebook_id, content, media_type, style, g.user_id)
    return jsonify(notebook["cover"]), 201


@notebooks_bp.route("/api/notebooks/<notebook_id>/cover", methods=["GET"])
def get_cover(notebook_id):
    _, error = _access(notebook_id)
    if error:
        return error
    cover = NotebookDB.get_cover(notebook_id)
    if not cover or cover.get("cover_content") is None:
        return jsonify({"error": "Cover not found"}), 404
    return Response(
        bytes(cover["cover_content"]),
        mimetype=cover["cover_media_type"],
        headers={
            "Content-Length": str(cover["cover_size_bytes"]),
            "ETag": f'"{cover["cover_hash"]}"',
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


@notebooks_bp.route("/api/notebooks/<notebook_id>/cover", methods=["DELETE"])
def delete_cover(notebook_id):
    _, error = _access(notebook_id, owner=True)
    if error:
        return error
    return jsonify(NotebookDB.clear_cover(notebook_id, g.user_id))


@notebooks_bp.route("/api/notebooks/<notebook_id>", methods=["DELETE"])
def delete_notebook(notebook_id):
    _, error = _access(notebook_id, owner=True)
    if error:
        return error
    NotebookDB.delete(notebook_id)
    return jsonify({"deleted": notebook_id})


@notebooks_bp.route("/api/notebooks/collaboration/users", methods=["GET"])
def collaboration_users():
    limit = _limit(default=50, maximum=200)
    query = (request.args.get("query") or "").strip()
    if len(query) > 200:
        return jsonify({"error": "query is too long"}), 400
    users = NotebookDB.list_collaboration_users(
        g.user_id,
        limit=limit,
        cursor=request.args.get("cursor"),
        query=query,
    )
    return jsonify(_paged(users, limit, "user_id"))


@notebooks_bp.route("/api/notebooks/collaboration/assignments", methods=["POST"])
def collaboration_assignments():
    data = _body()
    operations = data.get("operations")
    if operations is None:
        notebook_ids = data.get("notebook_ids")
        user_ids = data.get("user_ids")
        action = data.get("action", "upsert")
        role = data.get("role")
        if not isinstance(notebook_ids, list) or not isinstance(user_ids, list):
            return jsonify(
                {"error": "operations or notebook_ids and user_ids are required"}
            ), 400
        operations = [
            {
                "notebook_id": notebook_id,
                "user_id": user_id,
                "action": action,
                "role": role,
            }
            for notebook_id in notebook_ids
            for user_id in user_ids
        ]
    if not isinstance(operations, list) or not operations or len(operations) > 500:
        return jsonify({"error": "operations must contain 1-500 assignments"}), 400
    normalized = []
    seen = set()
    for operation in operations:
        if not isinstance(operation, dict):
            return jsonify({"error": "Each operation must be an object"}), 400
        notebook_id = operation.get("notebook_id")
        user_id = operation.get("user_id")
        action = operation.get("action", "upsert")
        role = operation.get("role")
        if not isinstance(notebook_id, str) or not notebook_id or len(notebook_id) > 100:
            return jsonify({"error": "Invalid notebook_id"}), 400
        if not isinstance(user_id, str) or not user_id or len(user_id) > 100:
            return jsonify({"error": "Invalid user_id"}), 400
        if action not in {"upsert", "remove"}:
            return jsonify({"error": "action must be upsert or remove"}), 400
        if action == "upsert" and role not in {"owner", "editor", "viewer"}:
            return jsonify({"error": "role must be owner, editor, or viewer"}), 400
        key = (notebook_id, user_id)
        if key in seen:
            return jsonify({"error": "Duplicate notebook/user assignment"}), 400
        seen.add(key)
        normalized.append(
            {
                "notebook_id": notebook_id,
                "user_id": user_id,
                "action": action,
                "role": role,
            }
        )
    try:
        results = NotebookDB.batch_assignments(g.user_id, normalized)
    except NotebookMutationError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"assignments": results, "count": len(results)})


@notebooks_bp.route("/api/notebooks/<notebook_id>/members", methods=["GET"])
def list_members(notebook_id):
    _, error = _access(notebook_id)
    if error:
        return error
    return jsonify(NotebookDB.list_members(notebook_id))


@notebooks_bp.route("/api/notebooks/<notebook_id>/members", methods=["POST"])
def add_member(notebook_id):
    notebook, error = _access(notebook_id, owner=True)
    if error:
        return error
    data = _body()
    user_id, validation_error = _require_text(data, "user_id")
    if validation_error:
        return validation_error
    role = data.get("role")
    if role not in MEMBER_ROLES:
        return jsonify({"error": "role must be editor or viewer"}), 400
    if user_id == notebook["owner_user_id"]:
        return jsonify({"error": "Owner membership cannot be changed"}), 400
    user = UserDB.get_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    if int(user.get("is_active", 1)) != 1:
        return jsonify({"error": "Inactive users cannot be assigned"}), 400
    return jsonify(NotebookDB.grant_member(notebook_id, user_id, role, g.user_id)), 201


@notebooks_bp.route("/api/notebooks/<notebook_id>/members/<user_id>", methods=["PATCH"])
def update_member(notebook_id, user_id):
    _, error = _access(notebook_id, owner=True)
    if error:
        return error
    member = NotebookDB.get_member(notebook_id, user_id)
    if not member:
        return jsonify({"error": "Member not found"}), 404
    if member["role"] == "owner":
        return jsonify({"error": "Owner membership cannot be changed"}), 400
    role = _body().get("role")
    if role not in MEMBER_ROLES:
        return jsonify({"error": "role must be editor or viewer"}), 400
    updated = NotebookDB.update_member(notebook_id, user_id, role, g.user_id)
    if not updated:
        return jsonify({"error": "Active member not found"}), 404
    return jsonify(updated)


@notebooks_bp.route("/api/notebooks/<notebook_id>/members/<user_id>", methods=["DELETE"])
def remove_member(notebook_id, user_id):
    _, error = _access(notebook_id, owner=True)
    if error:
        return error
    member = NotebookDB.get_member(notebook_id, user_id)
    if not member:
        return jsonify({"error": "Member not found"}), 404
    if member["role"] == "owner":
        return jsonify({"error": "Owner membership cannot be revoked"}), 400
    revoked = NotebookDB.revoke_member(notebook_id, user_id)
    if not revoked:
        return jsonify({"error": "Active member not found"}), 404
    return jsonify(revoked)


@notebooks_bp.route("/api/notebooks/<notebook_id>/sources", methods=["GET"])
def list_sources(notebook_id):
    _, error = _access(notebook_id)
    if error:
        return error
    return jsonify(NotebookDB.list_sources(notebook_id))


@notebooks_bp.route("/api/notebooks/<notebook_id>/sources/<source_id>", methods=["DELETE"])
def delete_source(notebook_id, source_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    deleted = NotebookDB.delete_source(notebook_id, source_id)
    if not deleted:
        return jsonify({"error": "Source not found"}), 404
    return jsonify(deleted)


@notebooks_bp.route("/api/notebooks/<notebook_id>/sources", methods=["POST"])
def create_source(notebook_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    data = _body()
    if data.get("source_type") not in SOURCE_TYPES:
        return jsonify({"error": "Invalid source_type"}), 400
    for field, maximum in (("name", 500), ("reference", 4096), ("status", 100)):
        _, field_error = _text(data, field, maximum)
        if field_error:
            return field_error
    extracted_text = data.get("extracted_text", "")
    content_snapshot = data.get("content_snapshot", data.get("content", ""))
    if not isinstance(extracted_text, str) or len(extracted_text.encode()) > MAX_SOURCE_TEXT:
        return jsonify({"error": "extracted_text is too large or invalid"}), 400
    if not isinstance(content_snapshot, str) or len(content_snapshot.encode()) > MAX_SOURCE_TEXT:
        return jsonify({"error": "content_snapshot is too large or invalid"}), 400
    media_type = data.get("media_type", "text/plain")
    if not isinstance(media_type, str) or not media_type or len(media_type) > 255:
        return jsonify({"error": "Invalid media_type"}), 400
    content_bytes = (content_snapshot or extracted_text).encode()
    supplied_size = data.get("size_bytes", len(content_bytes))
    if (
        not isinstance(supplied_size, int)
        or isinstance(supplied_size, bool)
        or supplied_size < 0
        or supplied_size > 5 * 1024 * 1024
    ):
        return jsonify({"error": "Invalid size_bytes"}), 400
    supplied_hash = data.get("content_hash")
    computed_hash = hashlib.sha256(content_bytes).hexdigest() if content_bytes else ""
    if supplied_hash is not None and (
        not isinstance(supplied_hash, str)
        or not HASH_RE.fullmatch(supplied_hash)
        or (content_bytes and supplied_hash != computed_hash)
    ):
        return jsonify({"error": "Invalid content_hash"}), 400
    content_version = data.get("content_version", 1)
    if not isinstance(content_version, int) or isinstance(content_version, bool) or content_version < 1:
        return jsonify({"error": "content_version must be a positive integer"}), 400
    provenance, provenance_error = _valid_json_object(data, "provenance")
    if provenance_error:
        return provenance_error
    metadata_error = _valid_metadata(data)
    if metadata_error:
        return metadata_error
    data.update(
        {
            "content_snapshot": content_snapshot,
            "media_type": media_type,
            "size_bytes": supplied_size,
            "content_hash": supplied_hash or computed_hash,
            "content_version": content_version,
            "provenance": provenance,
        }
    )
    source = NotebookDB.create_source(notebook_id, data, g.user_id)
    return jsonify(source), 201


@notebooks_bp.route("/api/notebooks/<notebook_id>/conversations", methods=["GET"])
def list_conversations(notebook_id):
    _, error = _access(notebook_id)
    if error:
        return error
    return jsonify(NotebookDB.list_conversations(notebook_id))


@notebooks_bp.route("/api/notebooks/<notebook_id>/conversations", methods=["POST"])
def create_conversation(notebook_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    conversation = NotebookDB.create_conversation(notebook_id, _body(), g.user_id)
    return jsonify(conversation), 201


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/conversations/<conversation_id>/events",
    methods=["GET"],
)
def list_conversation_events(notebook_id, conversation_id):
    notebook, error = _access(notebook_id)
    if error:
        return error
    if not NotebookDB.get_conversation(notebook_id, conversation_id):
        return jsonify({"error": "Conversation not found"}), 404
    include_deleted = request.args.get("include_deleted", "").lower() in {"1", "true", "yes"}
    if include_deleted and notebook["access_role"] not in WRITE_ROLES:
        return jsonify({"error": "Editor permission required"}), 403
    limit = _limit(default=100, maximum=250)
    if request.args.get("view") == "compacted":
        compaction = NotebookDB.latest_compaction(notebook_id, conversation_id)
        events = NotebookDB.list_events_page(
            notebook_id,
            conversation_id,
            limit=limit,
            include_deleted=False,
            after_event_id=compaction["cutoff_event_id"] if compaction else None,
        )
        return jsonify({"compaction": compaction, "events": events})
    return jsonify(
        NotebookDB.list_events_page(
            notebook_id,
            conversation_id,
            limit=limit,
            cursor=request.args.get("cursor"),
            include_deleted=include_deleted,
        )
    )


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/conversations/<conversation_id>/events",
    methods=["POST"],
)
def create_conversation_event(notebook_id, conversation_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    if not NotebookDB.get_conversation(notebook_id, conversation_id):
        return jsonify({"error": "Conversation not found"}), 404
    data = _body()
    _, validation_error = _text(data, "content", MAX_EVENT_CONTENT, required=True)
    if validation_error:
        return validation_error
    if data.get("event_type", "message") not in {
        "message",
        "tool",
        "operator",
        "athena",
        "system",
    }:
        return jsonify({"error": "Invalid event_type"}), 400
    if data.get("message_role", "user") not in {
        "user",
        "operator",
        "assistant",
        "athena",
        "system",
        "tool",
    }:
        return jsonify({"error": "Invalid message_role"}), 400
    metadata_error = _valid_metadata(data)
    if metadata_error:
        return metadata_error
    return jsonify(NotebookDB.create_event(notebook_id, conversation_id, data, g.user_id)), 201


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/conversations/<conversation_id>/events/<event_id>",
    methods=["DELETE"],
)
def delete_conversation_event(notebook_id, conversation_id, event_id):
    notebook, error = _access(notebook_id)
    if error:
        return error
    event = NotebookDB.get_event(notebook_id, conversation_id, event_id)
    if not event:
        return jsonify({"error": "Event not found"}), 404
    can_manage = notebook["access_role"] in WRITE_ROLES
    can_delete_own = (
        event["author_user_id"] == g.user_id
        and event["event_type"] in {"message", "operator"}
        and event["message_role"] in {"user", "operator"}
    )
    if not can_manage and not can_delete_own:
        return jsonify({"error": "Event deletion is not permitted"}), 403
    data = _body()
    reason, reason_error = _text(data, "reason", 1000)
    if reason_error:
        return reason_error
    deleted = NotebookDB.tombstone_event(
        notebook_id, conversation_id, event_id, g.user_id, reason
    )
    if not deleted:
        return jsonify({"error": "Event already deleted"}), 409
    return jsonify(deleted)


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/conversations/<conversation_id>/compactions",
    methods=["GET", "POST"],
)
def conversation_compactions(notebook_id, conversation_id):
    _, error = _access(notebook_id, write=request.method == "POST")
    if error:
        return error
    if not NotebookDB.get_conversation(notebook_id, conversation_id):
        return jsonify({"error": "Conversation not found"}), 404
    if request.method == "GET":
        return jsonify(
            NotebookDB.list_compactions(
                notebook_id, conversation_id, _limit(default=25, maximum=100)
            )
        )
    data = _body()
    _, summary_error = _text(
        data, "summary_content", MAX_COMPACTION_CONTENT, required=True
    )
    if summary_error:
        return summary_error
    cutoff, cutoff_error = _text(data, "cutoff_event_id", 100, required=True)
    if cutoff_error:
        return cutoff_error
    metadata, metadata_error = _valid_json_object(data, "athena_run_metadata")
    if metadata_error:
        return metadata_error
    data["cutoff_event_id"] = cutoff
    data["athena_run_metadata"] = metadata
    try:
        return jsonify(
            NotebookDB.create_compaction(
                notebook_id, conversation_id, data, g.user_id
            )
        ), 201
    except NotebookMutationError as exc:
        return jsonify({"error": str(exc)}), 400


@notebooks_bp.route("/api/notebooks/<notebook_id>/memories", methods=["GET"])
def list_memories(notebook_id):
    _, error = _access(notebook_id)
    if error:
        return error
    return jsonify(
        NotebookDB.list_memories(
            notebook_id,
            _limit(default=100, maximum=250),
            request.args.get("cursor"),
        )
    )


@notebooks_bp.route("/api/notebooks/<notebook_id>/memories", methods=["POST"])
def create_memory(notebook_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    data = _body()
    if data.get("memory_type") not in MEMORY_TYPES:
        return jsonify({"error": "Invalid memory_type"}), 400
    _, validation_error = _text(data, "content", MAX_ENGRAM_CONTENT, required=True)
    if validation_error:
        return validation_error
    metadata_error = _valid_metadata(data)
    if metadata_error:
        return metadata_error
    if not NotebookDB.provenance_is_valid(
        notebook_id, data.get("conversation_id"), data.get("event_id")
    ):
        return jsonify({"error": "Provenance does not belong to this notebook"}), 400
    return jsonify(NotebookDB.create_memory(notebook_id, data, g.user_id)), 201


@notebooks_bp.route("/api/notebooks/<notebook_id>/artifacts", methods=["GET"])
def list_artifacts(notebook_id):
    _, error = _access(notebook_id)
    if error:
        return error
    return jsonify(NotebookDB.list_artifacts(notebook_id))


@notebooks_bp.route("/api/notebooks/<notebook_id>/artifacts", methods=["POST"])
def create_artifact(notebook_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    data = _body()
    _, name_error = _require_text(data, "name")
    if name_error:
        return name_error
    if "content" not in data or not isinstance(data["content"], str):
        return jsonify({"error": "content is required"}), 400
    metadata_error = _valid_metadata(data)
    if metadata_error:
        return metadata_error
    if "version_metadata" in data and not isinstance(data["version_metadata"], dict):
        return jsonify({"error": "version_metadata must be an object"}), 400
    return jsonify(NotebookDB.create_artifact(notebook_id, data, g.user_id)), 201


@notebooks_bp.route("/api/notebooks/<notebook_id>/artifacts/<artifact_id>", methods=["GET"])
def get_artifact(notebook_id, artifact_id):
    _, error = _access(notebook_id)
    if error:
        return error
    artifact = NotebookDB.get_artifact(notebook_id, artifact_id)
    if not artifact:
        return jsonify({"error": "Artifact not found"}), 404
    return jsonify(artifact)


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/artifacts/<artifact_id>/versions",
    methods=["GET"],
)
def list_artifact_versions(notebook_id, artifact_id):
    _, error = _access(notebook_id)
    if error:
        return error
    if not NotebookDB.get_artifact(notebook_id, artifact_id):
        return jsonify({"error": "Artifact not found"}), 404
    return jsonify(NotebookDB.list_artifact_versions(notebook_id, artifact_id))


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/artifacts/<artifact_id>/versions",
    methods=["POST"],
)
def create_artifact_version(notebook_id, artifact_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    if not NotebookDB.get_artifact(notebook_id, artifact_id):
        return jsonify({"error": "Artifact not found"}), 404
    data = _body()
    if "content" not in data or not isinstance(data["content"], str):
        return jsonify({"error": "content is required"}), 400
    metadata_error = _valid_metadata(data)
    if metadata_error:
        return metadata_error
    version = NotebookDB.create_artifact_version(notebook_id, artifact_id, data, g.user_id)
    return jsonify(version), 201


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/artifacts/<artifact_id>/versions/<int:version_number>",
    methods=["GET"],
)
def get_artifact_version(notebook_id, artifact_id, version_number):
    _, error = _access(notebook_id)
    if error:
        return error
    version = NotebookDB.get_artifact_version(notebook_id, artifact_id, version_number)
    if not version:
        return jsonify({"error": "Artifact version not found"}), 404
    return jsonify(version)


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/artifacts/<artifact_id>/versions/"
    "<int:version_number>/renditions",
    methods=["GET"],
)
def list_artifact_renditions(notebook_id, artifact_id, version_number):
    _, error = _access(notebook_id)
    if error:
        return error
    if not _rendition_version(notebook_id, artifact_id, version_number):
        return jsonify({"error": "Artifact version not found"}), 404
    limit = _limit(default=50, maximum=100)
    items = NotebookDB.list_artifact_renditions(
        notebook_id,
        artifact_id,
        version_number,
        limit + 1,
        request.args.get("cursor"),
    )
    return jsonify(_paged(items, limit, "rendition_id"))


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/artifacts/<artifact_id>/versions/"
    "<int:version_number>/renditions",
    methods=["POST"],
)
def upload_artifact_rendition(notebook_id, artifact_id, version_number):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    if not _rendition_version(notebook_id, artifact_id, version_number):
        return jsonify({"error": "Artifact version not found"}), 404

    if request.mimetype == "multipart/form-data":
        upload = request.files.get("file")
        if not upload:
            return jsonify({"error": "file is required"}), 400
        content = upload.read(MAX_RENDITION_BYTES + 1)
        if len(content) > MAX_RENDITION_BYTES:
            return jsonify({"error": "Rendition exceeds 100 MB maximum"}), 413
        rendition_format = request.form.get("format", "").strip().lower()
        media_type = request.form.get("media_type", "").strip().lower()
        filename = os.path.basename(request.form.get("filename", upload.filename or ""))
        checksum = request.form.get("checksum", "").strip().lower()
        renderer = request.form.get("renderer", "").strip()
        renderer_version = request.form.get("renderer_version", "").strip()
        renderer_config, config_error = _multipart_json("renderer_config")
        if config_error:
            return config_error
        metadata, metadata_error = _multipart_json("metadata", maximum=64 * 1024)
        if metadata_error:
            return metadata_error
        status = "ready"
        rendition_error = ""
    else:
        data = _body()
        content = None
        rendition_format = str(data.get("format", "")).strip().lower()
        media_type = str(data.get("media_type", "")).strip().lower()
        filename = os.path.basename(str(data.get("filename", "")).strip())
        checksum = str(data.get("checksum", hashlib.sha256(b"").hexdigest())).strip().lower()
        renderer = str(data.get("renderer", "")).strip()
        renderer_version = str(data.get("renderer_version", "")).strip()
        renderer_config, config_error = _valid_json_object(data, "renderer_config")
        if config_error:
            return config_error
        metadata, metadata_error = _valid_json_object(data, "metadata", maximum=64 * 1024)
        if metadata_error:
            return metadata_error
        status = str(data.get("status", "pending")).strip().lower()
        rendition_error = str(data.get("error", "")).strip()
        if status not in RENDITION_STATUS_UPDATES:
            return jsonify({"error": "JSON rendition status must not be ready"}), 400

    if rendition_format not in RENDITION_MEDIA_TYPES:
        return jsonify({"error": "Unsupported rendition format"}), 400
    if media_type not in RENDITION_MEDIA_TYPES[rendition_format]:
        return jsonify({"error": "Media type does not match rendition format"}), 400
    if (
        not filename
        or len(filename) > 200
        or filename != os.path.basename(filename)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ ()-]{0,199}", filename)
        or not filename.lower().endswith(RENDITION_EXTENSIONS[rendition_format])
    ):
        return jsonify({"error": "Invalid rendition filename"}), 400
    if not HASH_RE.fullmatch(checksum):
        return jsonify({"error": "checksum must be a lowercase SHA-256 hex digest"}), 400
    if not renderer or len(renderer) > 100 or not renderer_version or len(renderer_version) > 100:
        return jsonify({"error": "renderer and renderer_version are required"}), 400
    if len(rendition_error) > 4000:
        return jsonify({"error": "error exceeds maximum length of 4000"}), 400

    if content is not None:
        actual_checksum = hashlib.sha256(content).hexdigest()
        if actual_checksum != checksum:
            return jsonify({"error": "Rendition checksum mismatch"}), 400
        if not _valid_rendition_signature(content, rendition_format):
            return jsonify({"error": "Rendition content signature is invalid"}), 400

    try:
        created = NotebookDB.create_artifact_rendition(
            notebook_id,
            artifact_id,
            version_number,
            {
                "format": rendition_format,
                "media_type": media_type,
                "filename": filename,
                "byte_size": len(content or b""),
                "checksum": checksum,
                "renderer": renderer,
                "renderer_version": renderer_version,
                "renderer_config": renderer_config,
                "status": status,
                "error": rendition_error,
                "metadata": metadata,
                "content": content,
            },
            g.user_id,
        )
        return jsonify(created), 201
    except psycopg2.IntegrityError as exc:
        return _integrity_error(exc)


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/artifacts/<artifact_id>/versions/"
    "<int:version_number>/renditions/<rendition_id>",
    methods=["PATCH"],
)
def update_artifact_rendition_status(
    notebook_id, artifact_id, version_number, rendition_id
):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    data = _body()
    status = str(data.get("status", "")).strip().lower()
    if status not in RENDITION_STATUS_UPDATES:
        return jsonify({"error": "Invalid rendition status update"}), 400
    rendition_error = str(data.get("error", "")).strip()
    if len(rendition_error) > 4000:
        return jsonify({"error": "error exceeds maximum length of 4000"}), 400
    metadata, metadata_error = _valid_json_object(data, "metadata", maximum=64 * 1024)
    if metadata_error:
        return metadata_error
    updated = NotebookDB.update_artifact_rendition_status(
        notebook_id,
        artifact_id,
        version_number,
        rendition_id,
        status,
        rendition_error,
        metadata,
    )
    if not updated:
        return jsonify({"error": "Mutable rendition not found"}), 404
    return jsonify(updated)


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/artifacts/<artifact_id>/versions/"
    "<int:version_number>/renditions/<rendition_id>/download",
    methods=["GET"],
)
def download_artifact_rendition(
    notebook_id, artifact_id, version_number, rendition_id
):
    _, error = _access(notebook_id)
    if error:
        return error
    rendition = NotebookDB.get_artifact_rendition(
        notebook_id,
        artifact_id,
        version_number,
        rendition_id,
        include_content=True,
    )
    if not rendition or rendition.get("status") != "ready":
        return jsonify({"error": "Ready rendition not found"}), 404
    content = rendition.pop("content")
    response = Response(content, status=200, mimetype=rendition["media_type"])
    response.headers["Content-Length"] = str(rendition["byte_size"])
    response.headers["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''{quote(rendition['filename'], safe='')}"
    )
    response.headers["ETag"] = f'"sha256:{rendition["checksum"]}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/artifacts/<artifact_id>/versions/"
    "<int:version_number>/renditions/<rendition_id>",
    methods=["DELETE"],
)
def delete_artifact_rendition(
    notebook_id, artifact_id, version_number, rendition_id
):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    deleted = NotebookDB.delete_artifact_rendition(
        notebook_id, artifact_id, version_number, rendition_id
    )
    if not deleted:
        return jsonify({"error": "Rendition not found"}), 404
    return jsonify(deleted)


def _validate_provenance(data: dict):
    provenance = data.get("provenance", [])
    if not isinstance(provenance, list) or len(provenance) > 100:
        return None, (jsonify({"error": "provenance must be an array of at most 100 items"}), 400)
    allowed = {
        "conversation_id",
        "originating_operator_event_id",
        "event_id",
        "athena_event_id",
        "author_user_id",
        "source_id",
        "citation_locator",
        "citation_hash",
        "confidence",
        "extraction_run_id",
    }
    for entry in provenance:
        if not isinstance(entry, dict) or set(entry) - allowed:
            return None, (jsonify({"error": "Invalid provenance entry"}), 400)
        for field in allowed - {"confidence"}:
            value = entry.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 2000):
                return None, (jsonify({"error": f"Invalid provenance {field}"}), 400)
        confidence = entry.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or confidence < 0
            or confidence > 1
        ):
            return None, (jsonify({"error": "confidence must be between 0 and 1"}), 400)
    return provenance, None


def _validate_engram_item(data: dict, require_type=True, require_content=True):
    if require_type and data.get("item_type") not in ENGRAM_TYPES:
        return jsonify({"error": "Invalid item_type"}), 400
    if "status" in data and data["status"] not in ENGRAM_STATUSES:
        return jsonify({"error": "Invalid status"}), 400
    if require_content or "content" in data:
        _, content_error = _text(
            data, "content", MAX_ENGRAM_CONTENT, required=require_content
        )
        if content_error:
            return content_error
    if "title" in data:
        _, title_error = _text(data, "title", 500)
        if title_error:
            return title_error
    if "metadata" in data:
        _, metadata_error = _valid_json_object(data, "metadata")
        if metadata_error:
            return metadata_error
    _, provenance_error = _validate_provenance(data)
    return provenance_error


@notebooks_bp.route("/api/notebooks/<notebook_id>/engrams/current", methods=["GET"])
def engram_current(notebook_id):
    _, error = _access(notebook_id)
    if error:
        return error
    return jsonify(EngramDB.get_current(notebook_id, _limit(default=100, maximum=250)))


@notebooks_bp.route("/api/notebooks/<notebook_id>/engrams/candidates", methods=["GET"])
def engram_candidates(notebook_id):
    _, error = _access(notebook_id)
    if error:
        return error
    limit = _limit(default=100, maximum=250)
    items = EngramDB.list_items(
        notebook_id, ("candidate",), limit + 1, request.args.get("cursor")
    )
    return jsonify(_paged(items, limit, "item_id"))


@notebooks_bp.route("/api/notebooks/<notebook_id>/engrams/items", methods=["GET"])
def list_engram_items(notebook_id):
    _, error = _access(notebook_id)
    if error:
        return error
    limit = _limit(default=100, maximum=250)
    items = EngramDB.list_items(
        notebook_id,
        tuple(sorted(ENGRAM_STATUSES)),
        limit + 1,
        request.args.get("cursor"),
    )
    return jsonify(_paged(items, limit, "item_id"))


@notebooks_bp.route("/api/notebooks/<notebook_id>/engrams/items", methods=["POST"])
def create_engram_item(notebook_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    data = _body()
    validation_error = _validate_engram_item(data)
    if validation_error:
        return validation_error
    if data.get("status", "candidate") not in {"candidate", "accepted"}:
        return jsonify({"error": "New items must be candidate or accepted"}), 400
    try:
        return jsonify(EngramDB.create_item(notebook_id, data, g.user_id)), 201
    except EngramConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    except psycopg2.IntegrityError as exc:
        return _integrity_error(exc)


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/engrams/items/<item_id>", methods=["GET"]
)
def get_engram_item(notebook_id, item_id):
    _, error = _access(notebook_id)
    if error:
        return error
    item = EngramDB.get_item(notebook_id, item_id)
    if not item:
        return jsonify({"error": "Engram item not found"}), 404
    return jsonify(item)


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/engrams/items/batch", methods=["POST"]
)
@notebooks_bp.route("/api/notebooks/<notebook_id>/engrams/batch", methods=["POST"])
def batch_engram_items(notebook_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    data = _body()
    item_ids = data.get("item_ids")
    action = data.get("action")
    if (
        not isinstance(item_ids, list)
        or not item_ids
        or len(item_ids) > 250
        or len(set(item_ids)) != len(item_ids)
        or any(not isinstance(item_id, str) or len(item_id) > 100 for item_id in item_ids)
    ):
        return jsonify({"error": "item_ids must contain 1-250 unique IDs"}), 400
    if action not in {"accept", "reject"}:
        return jsonify({"error": "action must be accept or reject"}), 400
    expected = data.get("expected_revision")
    if expected is not None and (
        not isinstance(expected, int) or isinstance(expected, bool) or expected < 0
    ):
        return jsonify({"error": "expected_revision must be a non-negative integer"}), 400
    try:
        items = EngramDB.batch_decide(
            notebook_id, item_ids, action, g.user_id, expected
        )
        return jsonify({"items": items, "count": len(items)})
    except EngramConflictError as exc:
        return jsonify({"error": str(exc)}), 409


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/engrams/items/<item_id>/revise",
    methods=["POST"],
)
def revise_engram_item(notebook_id, item_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    data = _body()
    if "content" not in data and "title" not in data and "metadata" not in data:
        return jsonify({"error": "A revision field is required"}), 400
    validation_error = _validate_engram_item(
        data, require_type=False, require_content=False
    )
    if validation_error:
        return validation_error
    try:
        return jsonify(EngramDB.revise_item(notebook_id, item_id, data, g.user_id))
    except EngramConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    except psycopg2.IntegrityError as exc:
        return _integrity_error(exc)


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/engrams/items/<item_id>/supersede",
    methods=["POST"],
)
def supersede_engram_item(notebook_id, item_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    data = _body()
    validation_error = _validate_engram_item(data)
    if validation_error:
        return validation_error
    try:
        return jsonify(EngramDB.supersede_item(notebook_id, item_id, data, g.user_id))
    except EngramConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    except psycopg2.IntegrityError as exc:
        return _integrity_error(exc)


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/engrams/items/<item_id>/resolve",
    methods=["POST"],
)
def resolve_engram_item(notebook_id, item_id):
    _, error = _access(notebook_id, write=True)
    if error:
        return error
    data = _body()
    if "reason" in data:
        _, reason_error = _text(data, "reason", 2000)
        if reason_error:
            return reason_error
    try:
        return jsonify(
            EngramDB.transition_item(
                notebook_id, item_id, "resolved", "resolved", data, g.user_id
            )
        )
    except EngramConflictError as exc:
        return jsonify({"error": str(exc)}), 409


@notebooks_bp.route("/api/notebooks/<notebook_id>/engrams/timeline", methods=["GET"])
def engram_timeline(notebook_id):
    _, error = _access(notebook_id)
    if error:
        return error
    limit = _limit(default=100, maximum=250)
    events = EngramDB.list_timeline(
        notebook_id, limit + 1, request.args.get("cursor")
    )
    return jsonify(_paged(events, limit, "engram_event_id"))


@notebooks_bp.route("/api/notebooks/<notebook_id>/engrams/snapshots", methods=["GET", "POST"])
def engram_snapshots(notebook_id):
    _, error = _access(notebook_id, write=request.method == "POST")
    if error:
        return error
    if request.method == "GET":
        return jsonify(
            EngramDB.list_snapshots(
                notebook_id, _limit(default=50, maximum=100)
            )
        )
    data = _body()
    expected = data.get("expected_revision")
    if expected is not None and (
        not isinstance(expected, int) or isinstance(expected, bool) or expected < 0
    ):
        return jsonify({"error": "expected_revision must be a non-negative integer"}), 400
    try:
        return jsonify(
            EngramDB.create_snapshot(notebook_id, g.user_id, expected)
        ), 201
    except EngramConflictError as exc:
        return jsonify({"error": str(exc)}), 409


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/engrams/snapshots/<snapshot_id>",
    methods=["GET"],
)
def get_engram_snapshot(notebook_id, snapshot_id):
    _, error = _access(notebook_id)
    if error:
        return error
    snapshot = EngramDB.get_snapshot(notebook_id, snapshot_id)
    if not snapshot:
        return jsonify({"error": "Engram snapshot not found"}), 404
    return jsonify(snapshot)


@notebooks_bp.route(
    "/api/notebooks/<notebook_id>/engrams/extraction-runs",
    methods=["GET", "POST"],
)
def engram_extraction_runs(notebook_id):
    _, error = _access(notebook_id, write=request.method == "POST")
    if error:
        return error
    if request.method == "GET":
        return jsonify(
            EngramDB.list_extraction_runs(
                notebook_id, _limit(default=50, maximum=100)
            )
        )
    data = _body()
    if data.get("status", "running") not in {"running", "completed", "failed"}:
        return jsonify({"error": "Invalid extraction run status"}), 400
    for field, maximum in (
        ("conversation_id", 100),
        ("extractor_version", 200),
        ("model", 200),
        ("source_fingerprint", 500),
    ):
        if field in data:
            _, field_error = _text(data, field, maximum)
            if field_error:
                return field_error
    _, metadata_error = _valid_json_object(data, "metadata")
    if metadata_error:
        return metadata_error
    try:
        return jsonify(
            EngramDB.create_extraction_run(notebook_id, data, g.user_id)
        ), 201
    except psycopg2.IntegrityError as exc:
        return _integrity_error(exc)
