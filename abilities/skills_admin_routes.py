import io
import zipfile
import tarfile
import uuid
import shutil
import logging
from flask import jsonify, request, g, send_file
from utils.auth import admin_required
from .skills_shared import (
    skills_bp,
    SKILLS_DIR,
    _create_skill_from_json,
    _safe_extract_zip,
    _safe_extract_tar,
    _title_from_archive_filename,
    _has_duplicate_title,
    _read_meta,
    _write_meta,
    _now_iso,
    _coalesce_skill_timestamps,
    _display_skill_name,
    _is_noise_skill_file,
)
from .default_skills import is_default_skill

logger = logging.getLogger(__name__)


@skills_bp.route("", methods=["POST"])
@admin_required
def upload_skill():
    if request.is_json:
        try:
            created, error_response = _create_skill_from_json(request.get_json(silent=True) or {})
            if error_response:
                return error_response
            return jsonify(created), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception("Failed creating generated skill")
            return jsonify({"error": f"Failed to create skill: {exc}"}), 500

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    
    skill_id = f"skill_{uuid.uuid4().hex[:12]}"
    skill_path = SKILLS_DIR / skill_id
    skill_path.mkdir(parents=True, exist_ok=True)
    
    if not file.filename:
        return jsonify({"error": "File name is required"}), 400

    # Save and extract
    archive_path = skill_path / file.filename
    file.save(archive_path)

    try:
        if archive_path.suffix.lower() == ".zip":
            _safe_extract_zip(archive_path, skill_path)
        elif archive_path.suffix.lower() in [".tar", ".gz", ".tgz", ".bz2", ".xz"]:
            _safe_extract_tar(archive_path, skill_path)
        else:
            return jsonify({"error": "Unsupported archive type. Use zip or tar.*"}), 400
        # Keep only extracted contents; the uploaded archive should not appear as a skill file.
        try:
            archive_path.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as e:
        shutil.rmtree(skill_path, ignore_errors=True)
        return jsonify({"error": f"Failed to extract archive: {e}"}), 400

    inferred_title = _title_from_archive_filename(file.filename)
    if not inferred_title:
        inferred_title = skill_id
    if _has_duplicate_title(inferred_title):
        shutil.rmtree(skill_path, ignore_errors=True)
        return jsonify({"error": f"Skill title '{inferred_title}' already exists"}), 409

    meta = {
        "id": skill_id,
        "title": inferred_title,
        "description": request.form.get("description", ""),
        "uploaded_by": g.user_id,
        "status": "active",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _write_meta(skill_path, meta)
    return jsonify(meta), 201


@skills_bp.route("", methods=["GET"])
def list_skills():
    skills = []
    for skill_dir in SKILLS_DIR.iterdir():
        if skill_dir.is_dir():
            meta = _read_meta(skill_dir)
            if not meta:
                meta = {
                    "id": skill_dir.name,
                    "title": skill_dir.name,
                    "description": "",
                    "uploaded_by": "",
                    "status": "active",
                    "created_at": "",
                    "updated_at": "",
                }
            meta = _coalesce_skill_timestamps(skill_dir, meta)
            if meta.get("status", "active") != "active":
                continue
            skills.append({
                "id": meta.get("id", skill_dir.name),
                "title": meta.get("title", skill_dir.name),
                "description": meta.get("description", ""),
                "uploaded_by": meta.get("uploaded_by", ""),
                "status": meta.get("status", "active"),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "system": is_default_skill(meta.get("id", skill_dir.name)),
                "deletable": not is_default_skill(meta.get("id", skill_dir.name)),
            })
    return jsonify({"skills": skills})


@skills_bp.route("/<skill_id>", methods=["PUT"])
@admin_required
def update_skill(skill_id):
    skill_path = (SKILLS_DIR / skill_id).resolve()
    if not skill_path.exists() or not str(skill_path).startswith(str(SKILLS_DIR.resolve())):
        return jsonify({"error": "Skill not found"}), 404

    if is_default_skill(skill_id) and (request.get_json(silent=True) or {}).get("status") == "inactive":
        return jsonify({"error": "Built-in Savant skills must remain active"}), 403
        
    data = request.get_json()
    meta = _read_meta(skill_path)
    if not meta:
        meta = {
            "id": skill_id,
            "title": skill_id,
            "description": "",
            "uploaded_by": "",
            "status": "active",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }

    if isinstance(data, dict):
        if "title" in data:
            meta["title"] = data.get("title") or meta.get("title", skill_id)
        if "description" in data:
            meta["description"] = data.get("description") or ""
        if "status" in data and data.get("status") in ("active", "inactive"):
            meta["status"] = data["status"]
    meta["updated_at"] = _now_iso()
    _write_meta(skill_path, meta)
    return jsonify(meta)


@skills_bp.route("/<skill_id>/archive", methods=["GET"])
@admin_required
def download_skill_archive(skill_id):
    skill_path = (SKILLS_DIR / skill_id).resolve()
    if not skill_path.exists() or not str(skill_path).startswith(str(SKILLS_DIR.resolve())):
        return jsonify({"error": "Skill not found"}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in skill_path.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(skill_path)
            if _is_noise_skill_file(rel, path):
                continue
            zf.write(path, arcname=str(rel))
    buf.seek(0)
    meta = _read_meta(skill_path)
    archive_name = _display_skill_name(skill_path, meta)
    safe_name = "".join(ch for ch in archive_name if ch.isalnum() or ch in ("-", "_", ".")).strip("._") or skill_id
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{safe_name}.zip",
    )


@skills_bp.route("/<skill_id>", methods=["DELETE"])
@admin_required
def delete_skill(skill_id):
    skill_path = (SKILLS_DIR / skill_id).resolve()
    if not skill_path.exists() or not str(skill_path).startswith(str(SKILLS_DIR.resolve())):
        return jsonify({"error": "Skill not found"}), 404
    if is_default_skill(skill_id):
        return jsonify({"error": "Built-in Savant skills cannot be deleted"}), 403

    try:
        shutil.rmtree(skill_path)
    except Exception as e:
        logger.exception("Failed deleting skill directory: %s", skill_path)
        return jsonify({"error": f"Failed to delete skill: {e}"}), 500

    return jsonify({"deleted": True, "id": skill_id, "status": "deleted"})
