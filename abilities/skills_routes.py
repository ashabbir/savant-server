from flask import Blueprint, jsonify, request, g, send_file
from utils.auth import admin_required
import os
import zipfile
import tarfile
import uuid
import shutil
import json
from datetime import datetime, timezone
from pathlib import Path
import logging
import io
from server_paths import get_server_data_dir

skills_bp = Blueprint("skills", __name__, url_prefix="/api/skills")
logger = logging.getLogger(__name__)

SKILLS_DIR = Path(
    (Path(os.environ["SAVANT_SKILLS_DIR"]).expanduser()
     if os.environ.get("SAVANT_SKILLS_DIR", "").strip()
     else get_server_data_dir() / "skills")
)
SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_rel_path(rel_path: str) -> bool:
    return not (rel_path.startswith("/") or ".." in Path(rel_path).parts)


def _safe_extract_zip(archive_path: Path, target_dir: Path) -> None:
    with zipfile.ZipFile(archive_path, "r") as zip_ref:
        for member in zip_ref.infolist():
            member_name = member.filename
            if not member_name or member_name.endswith("/"):
                continue
            if not _safe_rel_path(member_name):
                raise ValueError(f"Unsafe zip member path: {member_name}")
            out_path = (target_dir / member_name).resolve()
            if not str(out_path).startswith(str(target_dir.resolve())):
                raise ValueError(f"Zip extraction escaped target dir: {member_name}")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zip_ref.open(member, "r") as src, out_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _safe_extract_tar(archive_path: Path, target_dir: Path) -> None:
    with tarfile.open(archive_path, "r:*") as tar_ref:
        for member in tar_ref.getmembers():
            if not member.isfile():
                continue
            member_name = member.name
            if not _safe_rel_path(member_name):
                raise ValueError(f"Unsafe tar member path: {member_name}")
            out_path = (target_dir / member_name).resolve()
            if not str(out_path).startswith(str(target_dir.resolve())):
                raise ValueError(f"Tar extraction escaped target dir: {member_name}")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar_ref.extractfile(member)
            if extracted is None:
                continue
            with extracted as src, out_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _read_meta(skill_dir: Path) -> dict:
    meta_path = skill_dir / "metadata.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(skill_dir: Path, meta: dict) -> None:
    meta_path = skill_dir / "metadata.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=True), encoding="utf-8")

def _coalesce_skill_timestamps(skill_dir: Path, meta: dict) -> dict:
    out = dict(meta or {})
    if out.get("created_at") and out.get("updated_at"):
        return out
    try:
        st = skill_dir.stat()
        # Birth time on macOS; fallback to ctime/mtime if unavailable.
        created_ts = getattr(st, "st_birthtime", None) or st.st_ctime or st.st_mtime
        updated_ts = st.st_mtime or st.st_ctime or created_ts
        if not out.get("created_at") and created_ts:
            out["created_at"] = datetime.fromtimestamp(created_ts, timezone.utc).isoformat()
        if not out.get("updated_at") and updated_ts:
            out["updated_at"] = datetime.fromtimestamp(updated_ts, timezone.utc).isoformat()
    except Exception:
        pass
    return out

def _is_noise_skill_file(rel_path: Path, file_path: Path) -> bool:
    # Ignore macOS archive metadata and hidden/dot files/dirs.
    parts = rel_path.parts
    if any(p == "__MACOSX" for p in parts):
        return True
    if any(p.startswith(".") for p in parts):
        return True
    name = rel_path.name.lower()
    if name in {"thumbs.db", "desktop.ini"}:
        return True
    if name.endswith((".zip", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz", ".gz", ".bz2", ".xz")):
        return True
    # Ignore empty files by default (usually archive noise).
    try:
        if file_path.stat().st_size == 0:
            return True
    except Exception:
        return True
    return False

def _display_skill_name(skill_dir: Path, meta: dict | None = None) -> str:
    m = meta or {}
    title = str(m.get("title", "") or "").strip()
    if title:
        return title
    # Prefer single top-level content folder when present.
    try:
        top_dirs = []
        for p in skill_dir.iterdir():
            if not p.is_dir():
                continue
            rel = p.relative_to(skill_dir)
            if _is_noise_skill_file(rel, p):
                continue
            top_dirs.append(p.name)
        if len(top_dirs) == 1:
            return top_dirs[0]
    except Exception:
        pass
    return skill_dir.name

def _title_from_archive_filename(filename: str) -> str:
    name = Path(str(filename or "").strip()).name
    if not name:
        return ""
    lower = name.lower()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem

def _has_duplicate_title(title: str) -> bool:
    target = str(title or "").strip().lower()
    if not target:
        return False
    for skill_dir in SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        meta = _read_meta(skill_dir)
        existing = str(meta.get("title", "") or "").strip().lower()
        if existing and existing == target and meta.get("status", "active") == "active":
            return True
    return False


def _looks_like_text_file(file_path: Path, sample_bytes: int = 8192) -> bool:
    """Best-effort text-file detection for skill payload files."""
    try:
        raw = file_path.read_bytes()[:sample_bytes]
    except Exception:
        return False
    if not raw:
        return False
    # Fast reject for obvious binary content.
    if b"\x00" in raw:
        return False
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


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

@skills_bp.route("/<skill_id>/file", methods=["GET"])
@admin_required
def get_skill_file(skill_id):
    file_path = request.args.get("path")
    if not file_path:
        return jsonify({"error": "path parameter required"}), 400
        
    skill_path = (SKILLS_DIR / skill_id).resolve()
    target_path = (skill_path / file_path).resolve()
    
    if not target_path.exists() or not str(target_path).startswith(str(skill_path)):
        return jsonify({"error": "File not found or access denied"}), 404
    if not _looks_like_text_file(target_path):
        return jsonify({"error": "Binary files are not supported for skill content API"}), 400
        
    try:
        content = target_path.read_text(encoding="utf-8")
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@skills_bp.route("", methods=["POST"])
@admin_required
def upload_skill():
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
            })
    return jsonify({"skills": skills})

@skills_bp.route("/<skill_id>", methods=["PUT"])
@admin_required
def update_skill(skill_id):
    skill_path = (SKILLS_DIR / skill_id).resolve()
    if not skill_path.exists() or not str(skill_path).startswith(str(SKILLS_DIR.resolve())):
        return jsonify({"error": "Skill not found"}), 404
        
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

    try:
        shutil.rmtree(skill_path)
    except Exception as e:
        logger.exception("Failed deleting skill directory: %s", skill_path)
        return jsonify({"error": f"Failed to delete skill: {e}"}), 500

    return jsonify({"deleted": True, "id": skill_id, "status": "deleted"})
