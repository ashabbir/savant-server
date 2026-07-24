"""
Flask Blueprint for Abilities REST API.

All routes under /api/abilities/*.
The MCP server and (future) UI both call these endpoints.
"""

import io
import logging
import os
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from flask import Blueprint, jsonify, request, send_file, Response
from utils.auth import admin_required

from .store import AbilityStore
from .resolver import Resolver
from .bootstrap import seed_abilities_if_missing
from server_paths import get_server_abilities_base_dir

# Categories on disk under <base>/abilities/
_CATEGORIES = ("personas", "rules", "policies", "styles", "repos")

logger = logging.getLogger(__name__)

abilities_bp = Blueprint("abilities", __name__)

# ── Singleton store + resolver ────────────────────────────────────────────────

_store: Optional[AbilityStore] = None
_resolver: Optional[Resolver] = None


def _get_store() -> AbilityStore:
    global _store
    base_dir = Path(str(get_server_abilities_base_dir()))
    if _store is None or _store.base_path != base_dir:
        _store = AbilityStore(base_dir)
    # Reload on every request to pick up file changes
    _store.load()
    return _store


def _get_resolver() -> Resolver:
    global _resolver
    store = _get_store()
    if _resolver is None or _resolver.store is not store:
        _resolver = Resolver(store)
    return _resolver


# ── GET /api/abilities/assets — list all assets grouped by type ───────────────

@abilities_bp.route("/api/abilities/assets", methods=["GET"])
def list_assets():
    try:
        store = _get_store()
        return jsonify(store.list_assets_grouped())
    except Exception as e:
        logger.error(f"list_assets failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── GET /api/abilities/assets/<id> — get single asset ─────────────────────────

@abilities_bp.route("/api/abilities/assets/<path:asset_id>", methods=["GET"])
def get_asset(asset_id: str):
    try:
        store = _get_store()
        asset = store.get_asset_dict(asset_id)
        if not asset:
            return jsonify({"error": f"Asset '{asset_id}' not found"}), 404
        return jsonify(asset)
    except Exception as e:
        logger.error(f"get_asset failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── POST /api/abilities/assets — create new asset ────────────────────────────

@abilities_bp.route("/api/abilities/assets", methods=["POST"])
@admin_required
def create_asset():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        required = ["id", "type", "tags", "priority"]
        for field in required:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400

        store = _get_store()
        result = store.create_asset(
            asset_type=data["type"],
            asset_id=data["id"],
            tags=data["tags"],
            priority=int(data["priority"]),
            body=data.get("body", ""),
            includes=data.get("includes"),
            name=data.get("name"),
            aliases=data.get("aliases"),
        )
        return jsonify(result), 201
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        logger.error(f"create_asset failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── PUT /api/abilities/assets/<id> — update existing asset ───────────────────

@abilities_bp.route("/api/abilities/assets/<path:asset_id>", methods=["PUT"])
@admin_required
def update_asset(asset_id: str):
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        store = _get_store()
        result = store.update_asset(
            asset_id=asset_id,
            tags=data.get("tags"),
            priority=int(data["priority"]) if "priority" in data else None,
            body=data.get("body"),
            includes=data.get("includes"),
            name=data.get("name"),
            aliases=data.get("aliases"),
        )
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"update_asset failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── DELETE /api/abilities/assets/<id> — delete asset ─────────────────────────

@abilities_bp.route("/api/abilities/assets/<path:asset_id>", methods=["DELETE"])
@admin_required
def delete_asset(asset_id: str):
    try:
        store = _get_store()
        store.delete_asset(asset_id)
        return jsonify({"ok": True, "deleted": asset_id})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"delete_asset failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── POST /api/abilities/learn — append to ## Learned section ─────────────────

@abilities_bp.route("/api/abilities/learn", methods=["POST"])
def learn():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        asset_id = data.get("asset_id")
        content = data.get("content")
        if not asset_id or not content:
            return jsonify({"error": "asset_id and content required"}), 400

        store = _get_store()
        result = store.append_learned(asset_id, content)
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"learn failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── POST /api/abilities/resolve — resolve prompt from config ─────────────────

@abilities_bp.route("/api/abilities/resolve", methods=["POST"])
def resolve():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        persona = data.get("persona")
        if not persona:
            return jsonify({"error": "persona required"}), 400

        resolver = _get_resolver()
        result = resolver.resolve(
            persona=persona,
            tags=data.get("tags", []),
            repo_id=data.get("repo_id"),
            include_trace=bool(data.get("trace", False)),
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"resolve failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── GET /api/abilities/validate — validate store integrity ───────────────────

@abilities_bp.route("/api/abilities/validate", methods=["GET"])
def validate():
    try:
        store = _get_store()
        store.validate_all()
        return jsonify({"ok": True, "stats": store.stats()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── GET /api/abilities/stats — asset counts by type ──────────────────────────

@abilities_bp.route("/api/abilities/stats", methods=["GET"])
def stats():
    try:
        store = _get_store()
        return jsonify(store.stats())
    except Exception as e:
        logger.error(f"stats failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── GET /api/abilities/export — download the abilities directory tree ────────

@abilities_bp.route("/api/abilities/export", methods=["GET"])
def export_abilities():
    try:
        archive_format = request.args.get("format", "zip").strip().lower()
        if archive_format not in {"zip", "tar"}:
            return jsonify({"error": "format must be zip or tar"}), 400
        base_dir = Path(str(get_server_abilities_base_dir())) / "abilities"
        buf = io.BytesIO()
        count = 0
        paths = []
        if base_dir.exists():
            paths = sorted(
                path for path in base_dir.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
        if archive_format == "zip":
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in paths:
                    arcname = str(Path("abilities") / path.relative_to(base_dir))
                    zf.write(path, arcname)
                    count += 1
            mimetype, extension = "application/zip", "zip"
        else:
            with tarfile.open(fileobj=buf, mode="w") as tf:
                for path in paths:
                    arcname = str(Path("abilities") / path.relative_to(base_dir))
                    tf.add(path, arcname=arcname, recursive=False)
                    count += 1
            mimetype, extension = "application/x-tar", "tar"
        buf.seek(0)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"savant-abilities-{stamp}.{extension}"
        resp = send_file(buf, mimetype=mimetype, as_attachment=True, download_name=filename)
        resp.headers["X-Abilities-Count"] = str(count)
        return resp
    except Exception as e:
        logger.error(f"export_abilities failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── POST /api/abilities/import — insert missing files from ZIP or TAR ────────

class AbilityImportError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


HTTP_BAD_REQUEST = 400
HTTP_INTERNAL_SERVER_ERROR = 500


def _parse_import_request() -> bytes:
    if "file" in request.files:
        return request.files["file"].read()
    return request.get_data() or b""


def _validate_import_inputs(blob: bytes) -> bytes:
    if not blob:
        raise AbilityImportError("archive required (multipart 'file' or raw ZIP/TAR body)", HTTP_BAD_REQUEST)
    return blob


def _perform_zip_import(blob: bytes, insert_file) -> None:
    with zipfile.ZipFile(io.BytesIO(blob), "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            insert_file(info.filename, zf.read(info))


def _perform_tar_import(blob: bytes, insert_file, skipped: list[dict]) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
            for member in tf.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    skipped.append({"name": member.name, "reason": "unsupported_entry"})
                    continue
                source = tf.extractfile(member)
                if source is None:
                    skipped.append({"name": member.name, "reason": "unreadable_entry"})
                    continue
                insert_file(member.name, source.read())
    except tarfile.TarError as e:
        raise AbilityImportError("invalid ZIP or TAR archive", HTTP_BAD_REQUEST) from e


def _perform_import(blob: bytes) -> tuple[list[str], list[dict]]:
    base_dir = Path(str(get_server_abilities_base_dir())) / "abilities"
    base_dir.mkdir(parents=True, exist_ok=True)

    imported: list[str] = []
    skipped: list[dict] = []

    def insert_file(name: str, content: bytes) -> None:
        normalized = name.replace("\\", "/")
        rel = normalized[len("abilities/"):] if normalized.startswith("abilities/") else normalized
        parts = Path(rel).parts
        if not rel or not parts or any(part in {"", ".", ".."} for part in parts):
            skipped.append({"name": normalized, "reason": "invalid_path"})
            return
        dest = (base_dir / rel).resolve()
        try:
            dest.relative_to(base_dir.resolve())
        except ValueError:
            skipped.append({"name": normalized, "reason": "path_outside_base"})
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with dest.open("xb") as output:
                output.write(content)
        except FileExistsError:
            skipped.append({"name": normalized, "reason": "already_exists"})
            return
        imported.append(rel)

    if zipfile.is_zipfile(io.BytesIO(blob)):
        _perform_zip_import(blob, insert_file)
    else:
        _perform_tar_import(blob, insert_file, skipped)

    return imported, skipped


def _clear_ability_cache() -> None:
    global _store, _resolver
    _store = None
    _resolver = None


def _build_import_response(imported: list[str], skipped: list[dict]) -> Response:
    store = _get_store()
    return jsonify({
        "ok": True,
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "imported": imported,
        "skipped": skipped,
        "stats": store.stats(),
    })


def _map_import_error(exc: Exception) -> tuple[Response, int]:
    if isinstance(exc, AbilityImportError):
        return jsonify({"error": exc.message}), exc.status_code
    logger.error(f"import_abilities failed: {exc}")
    return jsonify({"error": str(exc)}), HTTP_INTERNAL_SERVER_ERROR


@abilities_bp.route("/api/abilities/import", methods=["POST"])
@admin_required
def import_abilities() -> Response | tuple[Response, int]:
    try:
        blob = _parse_import_request()
        validated_blob = _validate_import_inputs(blob)
        imported, skipped = _perform_import(validated_blob)
        _clear_ability_cache()
        return _build_import_response(imported, skipped)
    except Exception as exc:
        return _map_import_error(exc)


@abilities_bp.route("/api/abilities/bootstrap", methods=["POST"])
def bootstrap():
    try:
        result = seed_abilities_if_missing()
        if result.get("seeded"):
            global _store, _resolver
            _store = None
            _resolver = None
            return jsonify(result), 201
        if result.get("reason") == "already-populated":
            return jsonify(result), 409
        return jsonify(result), 500
    except Exception as e:
        logger.error(f"bootstrap failed: {e}")
        return jsonify({"error": str(e)}), 500
