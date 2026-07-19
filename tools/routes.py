from __future__ import annotations

import io
import json
import logging
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, g, jsonify, request, send_file

from db.knowledge_graph import KnowledgeGraphDB
from db.tools import ToolPackageDB
from server_paths import get_server_data_dir
from utils.auth import admin_required


logger = logging.getLogger(__name__)
tools_bp = Blueprint("tools", __name__, url_prefix="/api/tools")

TOOLS_DIR = Path(
    Path(os.environ["SAVANT_TOOLS_DIR"]).expanduser()
    if os.environ.get("SAVANT_TOOLS_DIR", "").strip()
    else get_server_data_dir() / "tools"
)
TOOLS_DIR.mkdir(parents=True, exist_ok=True)

MAX_TOOL_BYTES = 1_048_576
TOOL_DOMAIN_TITLE = "Savant Tools"
TOOL_OWNERSHIP_TITLE = "Tool Package Data Ownership"
TOOL_NAME_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_ .")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_rel_path(rel_path: str) -> bool:
    return not (rel_path.startswith("/") or ".." in Path(rel_path).parts)


def _tool_dir(tool_name: str) -> Path:
    return (TOOLS_DIR / tool_name).resolve()


def _tool_archive_path(tool_name: str) -> Path:
    return _tool_dir(tool_name) / f"{tool_name}.zip"


def _tool_meta_path(tool_name: str) -> Path:
    return _tool_dir(tool_name) / "metadata.json"


def _tool_manifest_path(tool_name: str) -> Path:
    base = _tool_dir(tool_name)
    target = f"{tool_name}-kg.json"
    # Try exact path first
    exact = base / target
    if exact.exists():
        return exact
    # Search for it in the directory (handles nested zips)
    for path in base.rglob(target):
        return path
    return exact


def _read_meta(tool_name: str) -> dict:
    meta_path = _tool_meta_path(tool_name)
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(tool_name: str, meta: dict) -> None:
    _tool_dir(tool_name).mkdir(parents=True, exist_ok=True)
    _tool_meta_path(tool_name).write_text(json.dumps(meta, ensure_ascii=True), encoding="utf-8")


def _read_manifest(tool_name: str) -> dict:
    manifest_path = _tool_manifest_path(tool_name)
    if not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _list_tool_names() -> list[str]:
    names = []
    for path in TOOLS_DIR.iterdir():
        if path.is_dir():
            names.append(path.name)
    return sorted(names)


def _tool_base_name(filename: str) -> str:
    name = Path(str(filename or "").strip()).name
    if not name.lower().endswith(".zip"):
        return ""
    return Path(name).stem.strip()


def _valid_tool_name(tool_name: str) -> bool:
    return bool(tool_name) and all(ch in TOOL_NAME_ALLOWED for ch in tool_name)


def _safe_extract_zip(archive_bytes: bytes, target_dir: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as zip_ref:
        members = [m for m in zip_ref.infolist() if m.filename and not m.filename.endswith("/")]
        
        # Identify common prefix (ignoring __MACOSX)
        important_members = [m for m in members if not m.filename.startswith("__MACOSX/")]
        prefix = ""
        if important_members:
            first_parts = Path(important_members[0].filename).parts
            if len(first_parts) > 1:
                potential_prefix = first_parts[0]
                all_match = True
                for m in important_members:
                    if not m.filename.startswith(f"{potential_prefix}/"):
                        all_match = False
                        break
                if all_match:
                    prefix = f"{potential_prefix}/"

        for member in members:
            member_name = member.filename
            if member_name.startswith("__MACOSX/"):
                continue
            
            # Strip prefix if it exists
            rel_path = member_name[len(prefix):] if prefix and member_name.startswith(prefix) else member_name
            if not rel_path:
                continue

            if not _safe_rel_path(rel_path):
                raise ValueError(f"Unsafe zip member path: {member_name}")
            out_path = (target_dir / rel_path).resolve()
            if not str(out_path).startswith(str(target_dir.resolve())):
                raise ValueError(f"Zip extraction escaped target dir: {member_name}")
            
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zip_ref.open(member, "r") as src, out_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _iter_existing_nodes() -> list[dict]:
    return KnowledgeGraphDB.list_nodes(limit=10000, include_staged=True)


def _find_node_by_title(title: str, node_type: str | None = None) -> dict | None:
    target = str(title or "").strip().lower()
    if not target:
        return None
    for node in _iter_existing_nodes():
        if str(node.get("title", "")).strip().lower() != target:
            continue
        if node_type and node.get("node_type") != node_type:
            continue
        return node
    return None


def _ensure_tool_domain_node() -> dict:
    domain_node = _find_node_by_title(TOOL_DOMAIN_TITLE, "concept")
    if domain_node:
        return domain_node

    ownership_node = _find_node_by_title(TOOL_OWNERSHIP_TITLE, "concept")
    domain_node = KnowledgeGraphDB.create_node({
        "node_type": "concept",
        "title": TOOL_DOMAIN_TITLE,
        "content": "Server-owned tool catalog and package delivery boundary.",
        "metadata": {
            "graph_type": "technical",
            "source": "tool_registry",
        },
        "status": "committed",
    })
    if ownership_node:
        try:
            KnowledgeGraphDB.create_edge({
                "source_id": domain_node["node_id"],
                "target_id": ownership_node["node_id"],
                "edge_type": "part_of",
            })
        except Exception:
            pass
    return domain_node


def _kg_node_ids_for_tool(tool_name: str) -> list[str]:
    meta = _read_meta(tool_name)
    ids = set(meta.get("kg_node_ids", []) or [])
    service_id = meta.get("service_node_id")
    if service_id:
        ids.add(service_id)
    return sorted(ids)


def _tool_summary(tool_name: str) -> dict:
    meta = _read_meta(tool_name)
    
    # Preferred source: Database nodes from kg_node_ids in meta
    kg_node_ids = meta.get("kg_node_ids", []) or []
    nodes = []
    
    # Also include the service node
    service_id = meta.get("service_node_id")
    ids_to_fetch = list(kg_node_ids)
    if service_id and service_id not in ids_to_fetch:
        ids_to_fetch.insert(0, service_id)

    for nid in ids_to_fetch:
        node = KnowledgeGraphDB.get_node(nid)
        if node:
            nodes.append({
                "node_id": node["node_id"],
                "title": node["title"],
                "node_type": node["node_type"]
            })

    # Fallback to manifest if database is empty (e.g. before commit or if deleted)
    if not nodes:
        manifest = _read_manifest(tool_name)
        if isinstance(manifest, list):
            raw_nodes = manifest
        elif isinstance(manifest, dict):
            raw_nodes = manifest.get("nodes", [])
        else:
            raw_nodes = []
        
        for node in raw_nodes:
            nodes.append({
                "node_id": str(node.get("node_id", "")),
                "title": str(node.get("title", "")),
                "node_type": "insight",
            })

    return {
        "name": meta.get("name", tool_name),
        "description": meta.get("description", ""),
        "author": meta.get("author", ""),
        "uploaded_by": meta.get("uploaded_by", ""),
        "created_at": meta.get("created_at", ""),
        "updated_at": meta.get("updated_at", ""),
        "archive_name": meta.get("archive_name", f"{tool_name}.zip"),
        "service_node_id": service_id or "",
        "kg_node_count": len(nodes),
        "kg_node_ids": kg_node_ids,
        "node_titles": nodes,
    }


def _list_tools_payload() -> list[dict]:
    tools_by_name = {}
    for tool in ToolPackageDB.list_all():
        tools_by_name[tool["name"]] = _database_tool_summary(tool)
    for tool_name in _list_tool_names():
        if tool_name in tools_by_name:
            continue
        meta = _read_meta(tool_name)
        if not meta:
            continue
        tools_by_name[tool_name] = _tool_summary(tool_name)
    return sorted(tools_by_name.values(), key=lambda item: item.get("name", "").lower())


def _database_tool_summary(tool: dict) -> dict:
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "author": tool.get("author", ""),
        "uploaded_by": tool.get("uploaded_by", ""),
        "created_at": tool.get("created_at", ""),
        "updated_at": tool.get("updated_at", ""),
        "archive_name": f"{tool['name']}.zip",
        "service_node_id": tool.get("service_node_id", ""),
        "kg_node_ids": tool.get("kg_node_ids", []) or [],
        "kg_node_count": len(tool.get("kg_node_ids", []) or []),
        "node_titles": [],
        "input_schema": tool.get("input_schema", {}) or {},
        "source": "postgresql",
    }


def _inline_tool_archive(tool_name: str, description: str, input_schema: dict) -> bytes:
    """Build a downloadable tool package for a tool created from Olympus."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_ref:
        zip_ref.writestr("README.md", f"# {tool_name}\n\n{description}\n")
        zip_ref.writestr("tool.json", json.dumps({
            "name": tool_name,
            "description": description,
            "input_schema": input_schema,
        }, indent=2))
    return archive.getvalue()


def _validate_manifest_nodes(tool_name: str, manifest: dict | list) -> list[dict]:
    if isinstance(manifest, list):
        nodes = manifest
    else:
        nodes = manifest.get("nodes", [])
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("tool manifest must contain at least one node")

    existing_ids = {node.get("node_id", "") for node in _iter_existing_nodes()}
    validated = []
    seen_ids: set[str] = set()
    for raw in nodes:
        if not isinstance(raw, dict):
            raise ValueError("tool manifest nodes must be objects")
        node_id = str(raw.get("node_id", "")).strip()
        if not node_id:
            raise ValueError("tool manifest nodes require node_id values")
        if node_id in seen_ids:
            raise ValueError(f"duplicate node_id in manifest: {node_id}")
        if node_id in existing_ids:
            raise ValueError(f"node_id already exists in knowledge graph: {node_id}")
        seen_ids.add(node_id)
        title = str(raw.get("title", "")).strip() or node_id
        validated.append({
            "node_id": node_id,
            "title": title,
            "content": str(raw.get("content", "")),
            "metadata": raw.get("metadata", {}) if isinstance(raw.get("metadata", {}), dict) else {},
        })
    return validated


def _store_tool_nodes(tool_name: str, description: str, author: str, manifest_nodes: list[dict]) -> dict:
    domain_node = _ensure_tool_domain_node()
    service_node = KnowledgeGraphDB.create_node({
        "node_type": "service",
        "title": tool_name,
        "content": description or f"Approved tool package: {tool_name}",
        "metadata": {
            "graph_type": "technical",
            "tool_name": tool_name,
            "author": author,
            "source": "tool_registry",
        },
        "status": "committed",
    })
    try:
        KnowledgeGraphDB.create_edge({
            "source_id": service_node["node_id"],
            "target_id": domain_node["node_id"],
            "edge_type": "part_of",
        })
    except Exception:
        pass

    kg_node_ids = []
    for node in manifest_nodes:
        created = KnowledgeGraphDB.create_node({
            "node_id": node["node_id"],
            "node_type": "insight",
            "title": node["title"],
            "content": node["content"],
            "metadata": {
                **node["metadata"],
                "tool_name": tool_name,
                "source": "tool_manifest",
                "graph_type": "technical",
            },
            "status": "committed",
        })
        kg_node_ids.append(created["node_id"])
        try:
            KnowledgeGraphDB.create_edge({
                "source_id": created["node_id"],
                "target_id": service_node["node_id"],
                "edge_type": "part_of",
            })
        except Exception:
            pass
    return {"service_node_id": service_node["node_id"], "kg_node_ids": kg_node_ids}


@tools_bp.route("", methods=["GET"])
def list_tools():
    return jsonify({"tools": _list_tools_payload()})


@tools_bp.route("/<tool_name>", methods=["GET"])
def get_tool(tool_name: str):
    tool_name = str(tool_name or "").strip()
    if not tool_name:
        return jsonify({"error": "Tool not found"}), 404
    database_tool = ToolPackageDB.get(tool_name)
    if database_tool:
        return jsonify({"tool": _database_tool_summary(database_tool)})
    tool_path = _tool_dir(tool_name)
    if not tool_path.exists() or not str(tool_path).startswith(str(TOOLS_DIR.resolve())):
        return jsonify({"error": "Tool not found"}), 404
    return jsonify({"tool": _tool_summary(tool_name)})


@tools_bp.route("/<tool_name>/archive", methods=["GET"])
def download_tool_archive(tool_name: str):
    tool_name = str(tool_name or "").strip()
    database_tool = ToolPackageDB.get(tool_name, include_archive=True)
    if database_tool:
        return send_file(
            io.BytesIO(bytes(database_tool["archive_data"])),
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{tool_name}.zip",
        )
    archive_path = _tool_archive_path(tool_name)
    if not archive_path.exists():
        return jsonify({"error": "Tool not found"}), 404
    return send_file(
        archive_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{tool_name}.zip",
    )


def _parse_uploaded_archive(tool_name: str, archive_bytes: bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as zip_ref:
            members = [member for member in zip_ref.infolist() if member.filename]
            if sum(member.file_size for member in members) > MAX_TOOL_BYTES:
                raise ValueError("Tool archive expanded content must not exceed 1MB")
            names = [member.filename for member in members]
            file_names = {Path(name).name for name in names if not name.endswith("/")}
            if "README.md" not in file_names:
                raise ValueError("Tool archive must contain README.md")
            if not any(Path(name).suffix.lower() in {".py", ".js", ".rb"} for name in names if not name.endswith("/")):
                raise ValueError("Tool archive must contain at least one script file")
            manifest_name = f"{tool_name}-kg.json"
            manifest_member = next((name for name in names if Path(name).name == manifest_name), "")
            if manifest_member:
                try:
                    return json.loads(zip_ref.read(manifest_member).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"{manifest_name} must be valid JSON") from exc
            readme_member = next(name for name in names if Path(name).name == "README.md")
            readme_content = zip_ref.read(readme_member).decode("utf-8")
            return [{
                "node_id": f"kgn_{tool_name.replace('-', '_')}_readme_001",
                "node_type": "insight",
                "title": f"{tool_name} Documentation",
                "content": readme_content,
            }]
    except zipfile.BadZipFile as exc:
        raise ValueError("Unsupported or corrupt zip archive") from exc


def _upload_inline_tool(payload: dict):
    tool_name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip()
    input_schema = payload.get("input_schema", {"type": "object", "properties": {}})
    if not _valid_tool_name(tool_name):
        return jsonify({"error": "Tool name contains unsupported characters"}), 400
    if not isinstance(input_schema, dict):
        return jsonify({"error": "input_schema must be an object"}), 400
    if ToolPackageDB.get(tool_name) or _tool_dir(tool_name).exists():
        return jsonify({"error": f"Tool '{tool_name}' already exists"}), 409
    try:
        created = ToolPackageDB.create({
            "name": tool_name,
            "description": description,
            "input_schema": input_schema,
            "archive_data": _inline_tool_archive(tool_name, description, input_schema),
            "author": g.user_id,
            "uploaded_by": g.user_id,
        })
    except Exception:
        logger.exception("Failed to persist tool %s in PostgreSQL", tool_name)
        return jsonify({"error": "Failed to persist tool in PostgreSQL"}), 500
    return jsonify({"tool": _database_tool_summary(created)}), 201


@tools_bp.route("", methods=["POST"])
@admin_required
def upload_tool():
    if request.is_json:
        return _upload_inline_tool(request.get_json(silent=True) or {})

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "File name is required"}), 400

    tool_name = _tool_base_name(file.filename)
    if not _valid_tool_name(tool_name):
        return jsonify({"error": "Tool name must be derived from a .zip filename"}), 400
    tool_path = _tool_dir(tool_name)
    if tool_path.exists():
        return jsonify({"error": f"Tool '{tool_name}' already exists"}), 409

    archive_bytes = file.read()
    if len(archive_bytes) > MAX_TOOL_BYTES:
        return jsonify({"error": "Tool archive must not exceed 1MB"}), 400

    try:
        manifest = _parse_uploaded_archive(tool_name, archive_bytes)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        validated_manifest_nodes = _validate_manifest_nodes(tool_name, manifest)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409 if "already exists" in str(e).lower() else 400

    tool_path.mkdir(parents=True, exist_ok=False)
    archive_path = _tool_archive_path(tool_name)
    archive_path.write_bytes(archive_bytes)
    try:
        _safe_extract_zip(archive_bytes, tool_path)
    except Exception as e:
        shutil.rmtree(tool_path, ignore_errors=True)
        return jsonify({"error": f"Failed to extract archive: {e}"}), 400

    description = (request.form.get("description") or "").strip()
    author = (request.form.get("author") or "").strip() or g.user_id
    try:
        graph_info = _store_tool_nodes(tool_name, description, author, validated_manifest_nodes)
    except Exception as e:
        shutil.rmtree(tool_path, ignore_errors=True)
        logger.exception("Failed to store tool graph nodes for %s", tool_name)
        return jsonify({"error": f"Failed to store knowledge graph nodes: {e}"}), 500

    meta = {
        "name": tool_name,
        "description": description,
        "author": author,
        "uploaded_by": g.user_id,
        "archive_name": f"{tool_name}.zip",
        "service_node_id": graph_info["service_node_id"],
        "kg_node_ids": graph_info["kg_node_ids"],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "status": "active",
    }
    _write_meta(tool_name, meta)
    try:
        persisted = ToolPackageDB.upsert({
            "name": tool_name,
            "description": description,
            "archive_data": archive_bytes,
            "author": author,
            "uploaded_by": g.user_id,
            "service_node_id": graph_info["service_node_id"],
            "kg_node_ids": graph_info["kg_node_ids"],
        })
    except Exception:
        logger.exception("Failed to persist uploaded tool %s in PostgreSQL", tool_name)
        return jsonify({"error": "Failed to persist tool in PostgreSQL"}), 500
    return jsonify({"tool": _database_tool_summary(persisted)}), 201


@tools_bp.route("/<tool_name>", methods=["DELETE"])
@admin_required
def delete_tool(tool_name: str):
    tool_name = str(tool_name or "").strip()
    tool_path = _tool_dir(tool_name)
    database_tool = ToolPackageDB.get(tool_name)
    if not database_tool and (not tool_path.exists() or not str(tool_path).startswith(str(TOOLS_DIR.resolve()))):
        return jsonify({"error": "Tool not found"}), 404

    meta = _read_meta(tool_name) if tool_path.exists() else database_tool
    node_ids = _kg_node_ids_for_tool(tool_name) if tool_path.exists() else (database_tool.get("kg_node_ids", []) or [])
    deleted_nodes = []
    for node_id in node_ids:
        try:
            if KnowledgeGraphDB.delete_node(node_id):
                deleted_nodes.append(node_id)
        except Exception:
            pass

    if tool_path.exists():
        try:
            shutil.rmtree(tool_path)
        except Exception as e:
            logger.exception("Failed deleting tool directory: %s", tool_path)
            return jsonify({"error": f"Failed to delete tool: {e}"}), 500
    try:
        ToolPackageDB.delete(tool_name)
    except Exception:
        logger.exception("Failed deleting tool %s from PostgreSQL", tool_name)
        return jsonify({"error": "Failed to delete tool from PostgreSQL"}), 500

    return jsonify({
        "deleted": True,
        "tool_name": tool_name,
        "deleted_nodes": deleted_nodes,
        "service_node_id": meta.get("service_node_id", ""),
    })
