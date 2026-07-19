import io
import json
import shutil
import zipfile
from pathlib import Path

from app import app
from server_paths import get_server_data_dir
from sqlite_client import get_connection


TOOLS_DIR = Path(get_server_data_dir()) / "tools"


def _make_tool_zip(*, tool_name: str, include_readme: bool = True, include_script: bool = True, manifest_nodes=None):
    buf = io.BytesIO()
    manifest_nodes = manifest_nodes if manifest_nodes is not None else [
        {
            "node_id": f"{tool_name}-node-1",
            "node_type": "insight",
            "title": f"{tool_name} insight",
            "content": "tool insight",
        }
    ]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if include_readme:
            zf.writestr("README.md", f"# {tool_name}\n")
        if include_script:
            zf.writestr(f"{tool_name}.py", "print('hello')\n")
        zf.writestr(f"{tool_name}-kg.json", json.dumps({"nodes": manifest_nodes}))
    buf.seek(0)
    return buf


def _seed_tool_package(tool_name: str, *, description: str = "seeded tool"):
    tool_dir = TOOLS_DIR / tool_name
    tool_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tool_dir / f"{tool_name}.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", "# seeded\n")
        zf.writestr(f"{tool_name}.py", "print('seeded')\n")
        zf.writestr(f"{tool_name}-kg.json", json.dumps({"nodes": []}))
    (tool_dir / "metadata.json").write_text(
        json.dumps({
            "name": tool_name,
            "description": description,
            "author": "tester",
            "archive_name": f"{tool_name}.zip",
            "created_at": "2026-05-27T00:00:00+00:00",
            "updated_at": "2026-05-27T00:00:00+00:00",
        }),
        encoding="utf-8",
    )
    (tool_dir / "README.md").write_text("# seeded\n", encoding="utf-8")
    (tool_dir / f"{tool_name}.py").write_text("print('seeded')\n", encoding="utf-8")
    (tool_dir / f"{tool_name}-kg.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
    return tool_dir


def _kg_node_ids():
    conn = get_connection()
    rows = conn.execute("SELECT node_id FROM kg_nodes ORDER BY node_id").fetchall()
    return [row["node_id"] for row in rows]


def _tool_list_ids(resp):
    payload = resp.get_json()
    return [item["name"] for item in payload["tools"]]


def test_upload_tool_requires_readme_script_and_kg_manifest(client):
    shutil.rmtree(TOOLS_DIR, ignore_errors=True)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    resp = client.post(
        "/api/tools",
        data={"file": (_make_tool_zip(tool_name="alpha", include_readme=False), "alpha.zip")},
    )

    assert resp.status_code == 400
    assert "README.md" in resp.get_json()["error"]

    resp = client.post(
        "/api/tools",
        data={"file": (_make_tool_zip(tool_name="alpha", include_script=False), "alpha.zip")},
    )

    assert resp.status_code == 400
    assert "script" in resp.get_json()["error"].lower()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", "# alpha\n")
        zf.writestr("alpha.py", "print('hello')\n")
    buf.seek(0)

    resp = client.post("/api/tools", data={"file": (buf, "alpha.zip")})

    assert resp.status_code == 201
    assert "alpha" in resp.get_json()["tool"]["name"]


def test_upload_tool_rejects_archive_with_oversized_expanded_content(client):
    shutil.rmtree(TOOLS_DIR, ignore_errors=True)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", "# oversized\n")
        zf.writestr("oversized.py", "print('ok')\n")
        zf.writestr("payload.bin", b"0" * (1_048_576 + 1))
    archive.seek(0)

    response = client.post(
        "/api/tools",
        data={"file": (archive, "oversized.zip")},
    )

    assert response.status_code == 400
    assert "expanded" in response.get_json()["error"].lower()
    assert not (TOOLS_DIR / "oversized").exists()


def test_upload_tool_rejects_duplicate_tool_name(client):
    shutil.rmtree(TOOLS_DIR, ignore_errors=True)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    first = client.post(
        "/api/tools",
        data={"file": (_make_tool_zip(tool_name="duplicate"), "duplicate.zip")},
    )
    assert first.status_code == 201

    second = client.post(
        "/api/tools",
        data={"file": (_make_tool_zip(tool_name="duplicate"), "duplicate.zip")},
    )

    assert second.status_code == 409
    assert "already exists" in second.get_json()["error"].lower()


def test_upload_tool_rejects_duplicate_knowledge_node_ids(client):
    shutil.rmtree(TOOLS_DIR, ignore_errors=True)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    client.post(
        "/api/tools",
        data={"file": (_make_tool_zip(tool_name="seeded"), "seeded.zip")},
    )
    assert "seeded-node-1" in _kg_node_ids()

    resp = client.post(
        "/api/tools",
        data={"file": (_make_tool_zip(tool_name="conflict", manifest_nodes=[{
            "node_id": "seeded-node-1",
            "node_type": "insight",
            "title": "conflict insight",
            "content": "duplicate node id",
        }]), "conflict.zip")},
    )

    assert resp.status_code == 409
    assert "node" in resp.get_json()["error"].lower()


def test_list_tools_and_download_archive(client):
    shutil.rmtree(TOOLS_DIR, ignore_errors=True)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    _seed_tool_package("downloadable")

    resp = client.get("/api/tools")
    assert resp.status_code == 200
    assert _tool_list_ids(resp) == ["downloadable"]

    archive = client.get("/api/tools/downloadable/archive")
    assert archive.status_code == 200
    assert archive.headers.get("Content-Type", "").startswith("application/zip")
    assert "downloadable.zip" in archive.headers.get("Content-Disposition", "")


def test_delete_tool_removes_package_and_kg_nodes(client):
    shutil.rmtree(TOOLS_DIR, ignore_errors=True)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    upload = client.post(
        "/api/tools",
        data={"file": (_make_tool_zip(tool_name="deletable"), "deletable.zip")},
    )
    assert upload.status_code == 201
    assert "deletable-node-1" in _kg_node_ids()

    resp = client.delete("/api/tools/deletable")
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] is True
    assert not (TOOLS_DIR / "deletable").exists()
    assert "deletable-node-1" not in _kg_node_ids()
