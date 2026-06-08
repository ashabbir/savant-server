"""Tests for Gemini session ingestion and provider endpoints."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def gemini_dir(tmp_path, monkeypatch):
    gdir = tmp_path / "gemini"
    chats = gdir / "tmp" / "savant-app" / "chats"
    chats.mkdir(parents=True)
    meta_dir = gdir / ".savant-meta"
    meta_dir.mkdir(parents=True)

    session_id = "ccf7999b-2e9a-4bc7-bc83-7e34b5492e18"
    session_file = chats / "session-2026-04-12T00-12-ccf7999b.json"
    artifact_dir = chats / session_id
    artifact_dir.mkdir()
    (artifact_dir / "notes.md").write_text("# artifact\n")
    (artifact_dir / "trace.json").write_text("{}\n")

    payload = {
        "sessionId": session_id,
        "projectPath": "/tmp/project-gemini",
        "startTime": "2026-04-12T00:12:06.723Z",
        "lastUpdated": "2026-04-12T00:15:49.974Z",
        "summary": "Gemini summary",
        "messages": [
            {"id": "m1", "timestamp": "2026-04-12T00:12:13.808Z", "type": "user", "content": [{"text": "build gemini support"}]},
            {
                "id": "m2",
                "timestamp": "2026-04-12T00:12:16.701Z",
                "type": "gemini",
                "content": "Working on it",
                "thoughts": [{"subject": "Plan", "description": "Investigating"}],
                "toolCalls": [
                    {
                        "id": "tool_1",
                        "name": "run_shell_command",
                        "displayName": "Shell",
                        "args": {"command": "git status"},
                        "resultDisplay": "On branch main",
                        "status": "success",
                    },
                    {
                        "id": "tool_2",
                        "name": "write_file",
                        "displayName": "WriteFile",
                        "args": {"path": "/tmp/project-gemini/README.md"},
                        "status": "success",
                    },
                ],
            },
        ],
    }
    session_file.write_text(json.dumps(payload))

    nested_payload = {
        "sessionId": "nested-short-id",
        "projectPath": "/tmp/project-gemini",
        "startTime": "2026-04-12T00:14:34.649Z",
        "lastUpdated": "2026-04-12T00:15:11.212Z",
        "messages": [
            {"id": "m3", "timestamp": "2026-04-12T00:14:35.000Z", "type": "user", "content": [{"text": "nested"}]},
        ],
    }
    (artifact_dir / "nested.json").write_text(json.dumps(nested_payload))
    (meta_dir / f"{session_id}.json").write_text(json.dumps({"workspace": "ws-1", "starred": True, "nickname": "Gem Session"}))

    monkeypatch.setenv("GEMINI_DIR", str(gdir))
    import app as app_mod
    monkeypatch.setattr(app_mod, "GEMINI_DIR", str(gdir))
    monkeypatch.setattr(app_mod, "GEMINI_CHATS_DIR", str(chats))
    app_mod._bg_cache["gemini_sessions"] = None
    app_mod._bg_cache["gemini_usage"] = None
    return {"session_id": session_id, "artifact_dir": str(artifact_dir)}


@pytest.fixture
def gemini_client(_isolated_db, gemini_dir):
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_gemini_sessions_list(gemini_client, gemini_dir):
    resp = gemini_client.get("/api/gemini/sessions")
    assert resp.status_code == 200
    data = resp.get_json()
    sessions = data["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == gemini_dir["session_id"]
    assert sessions[0]["provider"] == "gemini"
    assert sessions[0]["summary"] == "Gemini summary"
    assert sessions[0]["nickname"] == "Gem Session"
    assert sessions[0]["file_count"] == 3


def test_gemini_session_detail_and_conversation(gemini_client, gemini_dir):
    sid = gemini_dir["session_id"]
    resp = gemini_client.get(f"/api/gemini/session/{sid}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["id"] == sid
    assert data["artifact_dir"].endswith(sid)
    assert data["file_count"] == 3

    convo = gemini_client.get(f"/api/gemini/session/{sid}/conversation")
    assert convo.status_code == 200
    body = convo.get_json()
    assert body["stats"]["user_messages"] == 1
    assert body["stats"]["assistant_messages"] == 1
    assert body["stats"]["tool_calls"] == 2
    assert body["conversation"][1]["type"] == "assistant"


def test_gemini_search_and_project_files(gemini_client, gemini_dir):
    sid = gemini_dir["session_id"]
    search = gemini_client.get("/api/gemini/search?q=build")
    assert search.status_code == 200
    results = search.get_json()["results"]
    assert results[0]["session_id"] == sid

    files = gemini_client.get(f"/api/gemini/session/{sid}/project-files")
    assert files.status_code == 200
    data = files.get_json()
    assert data["cwd"] == "/tmp/project-gemini"
    assert data["files"][0]["path"] == "/tmp/project-gemini/README.md"


def test_gemini_rename_and_delete(gemini_client, gemini_dir):
    sid = gemini_dir["session_id"]
    rename = gemini_client.post(f"/api/gemini/session/{sid}/rename", json={"nickname": "Renamed Gemini"})
    assert rename.status_code == 200
    assert rename.get_json()["nickname"] == "Renamed Gemini"

    delete = gemini_client.delete(f"/api/gemini/session/{sid}")
    assert delete.status_code == 200
    assert delete.get_json()["deleted"] == sid

    follow_up = gemini_client.get("/api/gemini/sessions")
    assert follow_up.status_code == 200
    assert follow_up.get_json()["sessions"] == []
