import pytest
import os
import io
from app import app
from db.users import UserDB
from abilities.skills_routes import SKILLS_DIR

@pytest.fixture
def client():
    app.config["TESTING"] = True
    # Cleanup SKILLS_DIR before each test
    import shutil
    if SKILLS_DIR.exists():
        shutil.rmtree(SKILLS_DIR)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    with app.test_client() as client:
        yield client

def test_skills_management_rbac(client):
    # Test as regular user (lex is admin now, need a user)
    # Actually both are admins now, let's create a user
    UserDB.create({
        "user_id": "test_user",
        "name": "Test User",
        "email": "test@savant.dev",
        "api_key": "sk-test-user",
        "role": "user"
    })

    # Try accessing admin endpoint
    resp = client.post("/api/skills", headers={"X-API-Key": "sk-test-user"})
    assert resp.status_code == 403
    
    # Try accessing as admin
    resp = client.post("/api/skills", headers={"X-API-Key": "sk-ahmed-savant-001"})
    # Should be 400 because no file attached, not 403
    assert resp.status_code == 400


def test_create_generated_skill_writes_arbitrary_files_atomically(client):
    payload = {
        "name": "summarize-releases",
        "description": "Summarize release history",
        "files": [
            {
                "path": "SKILL.md",
                "content": "---\nname: summarize-releases\ndescription: Summarize releases\n---\n\n# Workflow\n",
            },
            {"path": "scripts/summarize.py", "content": "print('ok')\n"},
            {"path": "references/format.md", "content": "# Output format\n"},
        ],
    }

    resp = client.post(
        "/api/skills",
        json=payload,
        headers={"X-API-Key": "sk-ahmed-savant-001"},
    )

    assert resp.status_code == 201
    assert resp.get_json()["id"] == "summarize-releases"
    skill_dir = SKILLS_DIR / "summarize-releases"
    assert (skill_dir / "SKILL.md").read_text() == payload["files"][0]["content"]
    assert (skill_dir / "scripts" / "summarize.py").read_text() == "print('ok')\n"
    assert (skill_dir / "metadata.json").exists()


def test_create_generated_skill_validates_paths_and_required_skill_file(client):
    headers = {"X-API-Key": "sk-ahmed-savant-001"}
    unsafe = client.post(
        "/api/skills",
        json={"name": "unsafe-skill", "files": [{"path": "../escape", "content": "no"}]},
        headers=headers,
    )
    assert unsafe.status_code == 400
    assert not (SKILLS_DIR / "unsafe-skill").exists()

    missing_manifest = client.post(
        "/api/skills",
        json={"name": "missing-manifest", "files": [{"path": "notes.md", "content": "no"}]},
        headers=headers,
    )
    assert missing_manifest.status_code == 400
    assert "SKILL.md is required" in missing_manifest.get_json()["error"]


def test_update_generated_skill_file(client):
    skill_dir = SKILLS_DIR / "editable-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("before")
    (skill_dir / "metadata.json").write_text('{"id":"editable-skill","title":"editable-skill"}')

    resp = client.put(
        "/api/skills/editable-skill/file?path=SKILL.md",
        json={"content": "after"},
        headers={"X-API-Key": "sk-ahmed-savant-001"},
    )

    assert resp.status_code == 200
    assert (skill_dir / "SKILL.md").read_text() == "after"

def test_skills_file_exploration(client):
    # Setup: Create a fake skill
    import os
    skill_id = "test_skill_123"
    skill_dir = os.path.join(str(SKILLS_DIR), skill_id)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "test.txt"), "w") as f:
        f.write("hello world")

    # Test file list
    resp = client.get(f"/api/skills/{skill_id}/files", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 200
    assert "test.txt" in resp.get_json()["files"]

    # Test file content
    resp = client.get(f"/api/skills/{skill_id}/file?path=test.txt", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 200
    assert resp.get_json()["content"] == "hello world"


def test_skills_binary_files_are_filtered_and_rejected(client):
    skill_id = "test_skill_binary"
    skill_dir = os.path.join(str(SKILLS_DIR), skill_id)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "notes.md"), "w") as f:
        f.write("plain text")
    with open(os.path.join(skill_dir, "blob.bin"), "wb") as f:
        f.write(b"\x1f\x8b\x08\x00\x10\x00\x00\x00\x00\x00\xff")

    list_resp = client.get(f"/api/skills/{skill_id}/files", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert list_resp.status_code == 200
    files = list_resp.get_json()["files"]
    assert "notes.md" in files
    assert "blob.bin" not in files

    file_resp = client.get(f"/api/skills/{skill_id}/file?path=blob.bin", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert file_resp.status_code == 400
    assert "Binary files are not supported" in file_resp.get_json()["error"]

def test_skills_list_and_manage(client):
    # Setup: Create a skill
    skill_id = "test_skill_list"
    skill_dir = os.path.join(str(SKILLS_DIR), skill_id)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "metadata.json"), "w") as f:
        f.write('{"title": "Test Skill"}')

    # Test List
    resp = client.get("/api/skills", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 200
    assert len(resp.get_json()["skills"]) > 0

    # Test Update
    resp = client.put(f"/api/skills/{skill_id}", 
                     json={"title": "Updated Title"},
                     headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 200
    assert resp.get_json()["title"] == "Updated Title"

    # Test Delete
    resp = client.delete(f"/api/skills/{skill_id}", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] is True
    assert resp.get_json()["status"] == "deleted"
    assert not os.path.exists(skill_dir)


def test_skill_archive_download(client):
    skill_id = "test_skill_archive"
    skill_dir = os.path.join(str(SKILLS_DIR), skill_id)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "README.md"), "w") as f:
        f.write("archive ok")

    resp = client.get(f"/api/skills/{skill_id}/archive", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 200
    assert resp.headers.get("Content-Type", "").startswith("application/zip")
    assert len(resp.data) > 20

def test_skill_upload_invalid_type(client):
    data = {"file": (io.BytesIO(b"content"), "test.txt")}
    resp = client.post("/api/skills", data=data, headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 400
    assert "Unsupported archive type" in resp.get_json()["error"]

def test_skills_file_exploration_not_found(client):
    resp = client.get("/api/skills/nonexistent/files", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 404

def test_skills_file_content_missing(client):
    # Setup skill
    skill_id = "test_skill_missing"
    os.makedirs(SKILLS_DIR / skill_id, exist_ok=True)
    
    # Test file content
    resp = client.get(f"/api/skills/{skill_id}/file", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 400

    resp = client.get(f"/api/skills/{skill_id}/file?path=notfound.txt", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 404

def test_skills_update_invalid(client):
    resp = client.put("/api/skills/nonexistent", json={"title": "New"}, headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 404

def test_skills_delete_invalid(client):
    resp = client.delete("/api/skills/nonexistent", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 404

def test_skills_duplicate_title(client):
    # Upload first skill
    # Need to simulate a file upload with a zip
    import zipfile
    
    buf1 = io.BytesIO()
    with zipfile.ZipFile(buf1, "w") as z:
        z.writestr("test.txt", "content")
    buf1.seek(0)
    
    resp1 = client.post("/api/skills", data={"file": (buf1, "duplicate.zip")}, headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp1.status_code == 201

    # Upload second skill with same name
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as z:
        z.writestr("test.txt", "content")
    buf2.seek(0)
    
    resp2 = client.post("/api/skills", data={"file": (buf2, "duplicate.zip")}, headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp2.status_code == 409
    assert "already exists" in resp2.get_json()["error"]

def test_skill_title_from_subfolder(client):
    # Setup: Create a skill with a subfolder structure
    skill_id = "test_skill_subfolder"
    skill_dir = SKILLS_DIR / skill_id
    os.makedirs(skill_dir / "my-content-folder", exist_ok=True)
    with open(skill_dir / "my-content-folder" / "README.md", "w") as f:
        f.write("hello")

    # Test Download to trigger _display_skill_name
    resp = client.get(f"/api/skills/{skill_id}/archive", headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("Content-Disposition")
    assert "my-content-folder.zip" in resp.headers.get("Content-Disposition")

def test_skill_zip_traversal(client):
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "evil")
    buf.seek(0)

    resp = client.post("/api/skills", data={"file": (buf, "evil.zip")}, headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 400
    assert "Unsafe zip member path" in resp.get_json()["error"]

def test_skill_tar_traversal(client):
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        # Create a member that escapes
        import tarfile
        info = tarfile.TarInfo(name="../evil.txt")
        info.size = len(b"evil")
        t.addfile(info, io.BytesIO(b"evil"))
    buf.seek(0)

    resp = client.post("/api/skills", data={"file": (buf, "evil.tar")}, headers={"X-API-Key": "sk-ahmed-savant-001"})
    assert resp.status_code == 400
    assert "Unsafe tar member path" in resp.get_json()["error"]
