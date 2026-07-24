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
import re
from server_paths import get_server_data_dir

skills_bp = Blueprint("skills", __name__, url_prefix="/api/skills")
logger = logging.getLogger(__name__)

SKILLS_DIR = Path(
    (Path(os.environ["SAVANT_SKILLS_DIR"]).expanduser()
     if os.environ.get("SAVANT_SKILLS_DIR", "").strip()
     else get_server_data_dir() / "skills")
)
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

MAX_SKILL_FILES = 128
MAX_SKILL_FILE_BYTES = 1024 * 1024
MAX_SKILL_TOTAL_BYTES = 4 * 1024 * 1024
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_rel_path(rel_path: str) -> bool:
    return not (rel_path.startswith("/") or ".." in Path(rel_path).parts)


def _resolve_extraction_target(target_dir: Path, member_name: str) -> Path:
    """Resolve an archive member and guarantee it stays inside target_dir."""
    if not member_name or not _safe_rel_path(member_name):
        raise ValueError(f"Unsafe archive member path: {member_name}")
    root = target_dir.resolve()
    candidate = (root / member_name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Archive extraction escaped target dir: {member_name}") from exc
    return candidate


def _validated_skill_files(raw_files) -> dict[str, str]:
    if isinstance(raw_files, dict):
        entries = [{"path": path, "content": content} for path, content in raw_files.items()]
    elif isinstance(raw_files, list):
        entries = raw_files
    else:
        raise ValueError("files must be an object or an array of path/content entries")

    if not entries or len(entries) > MAX_SKILL_FILES:
        raise ValueError(f"files must contain between 1 and {MAX_SKILL_FILES} entries")

    files: dict[str, str] = {}
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each file must contain path and content")
        rel_path = str(entry.get("path", "")).strip().replace("\\", "/")
        content = entry.get("content")
        path_parts = Path(rel_path).parts
        if (not rel_path or not _safe_rel_path(rel_path) or Path(rel_path).is_absolute()
                or any(part in ("", ".") for part in path_parts)):
            raise ValueError(f"unsafe skill file path: {rel_path or '<empty>'}")
        if rel_path == "metadata.json":
            raise ValueError("metadata.json is managed by the server")
        if rel_path in files:
            raise ValueError(f"duplicate skill file path: {rel_path}")
        if any(existing.startswith(f"{rel_path}/") or rel_path.startswith(f"{existing}/") for existing in files):
            raise ValueError(f"skill file conflicts with another path: {rel_path}")
        if not isinstance(content, str):
            raise ValueError(f"skill file content must be text: {rel_path}")
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > MAX_SKILL_FILE_BYTES:
            raise ValueError(f"skill file is too large: {rel_path}")
        total_bytes += content_bytes
        if total_bytes > MAX_SKILL_TOTAL_BYTES:
            raise ValueError("skill files exceed the total size limit")
        files[rel_path] = content

    if "SKILL.md" not in files:
        raise ValueError("SKILL.md is required")
    return files


def _create_skill_from_json(data: dict):
    name = str(data.get("name", "")).strip().lower()
    if not SKILL_NAME_RE.fullmatch(name) or len(name) > 64:
        raise ValueError("name must be 1-64 lowercase letters, digits, or hyphen-separated words")
    if (SKILLS_DIR / name).exists() or _has_duplicate_title(name):
        return None, (jsonify({"error": f"Skill title '{name}' already exists"}), 409)

    files = _validated_skill_files(data.get("files"))
    now = _now_iso()
    meta = {
        "id": name,
        "title": name,
        "description": str(data.get("description", "")).strip(),
        "uploaded_by": g.user_id,
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    temp_path = SKILLS_DIR / f".creating-{uuid.uuid4().hex}"
    final_path = SKILLS_DIR / name
    try:
        temp_path.mkdir(parents=True, exist_ok=False)
        for rel_path, content in files.items():
            target = temp_path / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        _write_meta(temp_path, meta)
        os.replace(temp_path, final_path)
    except Exception:
        shutil.rmtree(temp_path, ignore_errors=True)
        raise
    return {**meta, "files": files}, None


def _safe_extract_zip(archive_path: Path, target_dir: Path) -> None:
    with zipfile.ZipFile(archive_path, "r") as zip_ref:
        for member in zip_ref.infolist():
            member_name = member.filename
            if not member_name or member_name.endswith("/"):
                continue
            out_path = _resolve_extraction_target(target_dir, member_name)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zip_ref.open(member, "r") as src, out_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _safe_extract_tar(archive_path: Path, target_dir: Path) -> None:
    with tarfile.open(archive_path, "r:*") as tar_ref:
        for member in tar_ref.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(f"Links are not allowed in skill archives: {member.name}")
            if not member.isfile():
                continue
            member_name = member.name
            out_path = _resolve_extraction_target(target_dir, member_name)
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
