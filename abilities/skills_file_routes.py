import os
import uuid
from flask import jsonify, request
from utils.auth import admin_required
from .skills_shared import (
    skills_bp,
    SKILLS_DIR,
    MAX_SKILL_FILE_BYTES,
    _is_noise_skill_file,
    _looks_like_text_file,
    _read_meta,
    _write_meta,
    _now_iso,
)


@skills_bp.route("/<skill_id>/files", methods=["GET"])
@admin_required
def list_skill_files(skill_id):
    skill_path = (SKILLS_DIR / skill_id).resolve()
    if not skill_path.exists() or not str(skill_path).startswith(str(SKILLS_DIR.resolve())):
        return jsonify({"error": "Skill not found"}), 404
        
    files = []
    for path in skill_path.rglob("*"):
        if path.is_file():
            rel = path.relative_to(skill_path)
            if _is_noise_skill_file(rel, path):
                continue
            if not _looks_like_text_file(path):
                continue
            files.append(str(rel))
    files.sort()
    return jsonify({"files": files})


@skills_bp.route("/<skill_id>/file", methods=["GET", "PUT"])
@admin_required
def get_skill_file(skill_id):
    file_path = request.args.get("path")
    if not file_path:
        return jsonify({"error": "path parameter required"}), 400
        
    skill_path = (SKILLS_DIR / skill_id).resolve()
    target_path = (skill_path / file_path).resolve()

    if not skill_path.exists() or not str(skill_path).startswith(str(SKILLS_DIR.resolve()) + os.sep):
        return jsonify({"error": "Skill not found"}), 404
    if not str(target_path).startswith(str(skill_path) + os.sep):
        return jsonify({"error": "File not found or access denied"}), 404

    if request.method == "PUT":
        if file_path == "metadata.json":
            return jsonify({"error": "metadata.json is managed by the server"}), 400
        data = request.get_json(silent=True) or {}
        content = data.get("content")
        if not isinstance(content, str):
            return jsonify({"error": "content must be text"}), 400
        if len(content.encode("utf-8")) > MAX_SKILL_FILE_BYTES:
            return jsonify({"error": "Skill file is too large"}), 400
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, target_path)
        finally:
            temp_path.unlink(missing_ok=True)
        meta = _read_meta(skill_path)
        meta["updated_at"] = _now_iso()
        _write_meta(skill_path, meta)
        return jsonify({"path": file_path, "content": content})

    if not target_path.exists():
        return jsonify({"error": "File not found or access denied"}), 404
    if not _looks_like_text_file(target_path):
        return jsonify({"error": "Binary files are not supported for skill content API"}), 400
        
    try:
        content = target_path.read_text(encoding="utf-8")
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
