"""Collaborative notebook REST API behavior."""

import base64
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor

from db.users import UserDB
from utils.auth import ALLOWED_SAVANT_APPS


APP_HEADER = {"X-App-Name": "savant-notebook"}
OWNER_HEADERS = {"X-API-Key": "sk-ahmed-savant-001", **APP_HEADER}
EDITOR_HEADERS = {"X-API-Key": "sk-notebook-editor", **APP_HEADER}
VIEWER_HEADERS = {"X-API-Key": "sk-notebook-viewer", **APP_HEADER}
OUTSIDER_HEADERS = {"X-API-Key": "sk-notebook-outsider", **APP_HEADER}


def test_notebook_client_is_an_allowed_savant_app():
    assert "savant-notebook" in ALLOWED_SAVANT_APPS


def test_sqlite_schema_initializes_notebook_tables(_isolated_db):
    from sqlite_client import get_connection

    rows = get_connection().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'notebook%'"
    ).fetchall()
    names = {row["name"] for row in rows}
    assert {
        "notebooks",
        "notebook_memberships",
        "notebook_sources",
        "notebook_conversations",
        "notebook_events",
        "notebook_memories",
        "notebook_artifacts",
        "notebook_artifact_versions",
        "notebook_artifact_renditions",
    } <= names


def test_production_middleware_allows_non_admin_notebook_crud_and_rejects_bad_auth(
    _isolated_db,
    monkeypatch,
):
    from app import app

    UserDB.create({
        "user_id": "notebook-owner",
        "name": "Notebook owner",
        "api_key": "sk-notebook-owner",
        "role": "user",
    })
    monkeypatch.delenv("SAVANT_DISABLE_APP_CHECK", raising=False)
    previous_testing = app.config.get("TESTING")
    app.config["TESTING"] = False
    try:
        with app.test_client() as production_client:
            headers = {"X-API-Key": "sk-notebook-owner", **APP_HEADER}
            empty = production_client.get("/api/notebooks", headers=headers)
            assert empty.status_code == 200
            assert empty.get_json() == []

            created = production_client.post(
                "/api/notebooks",
                json={"title": "Production middleware notebook"},
                headers=headers,
            )
            assert created.status_code == 201
            notebook_id = created.get_json()["notebook_id"]
            assert created.get_json()["access_role"] == "owner"
            assert production_client.get(
                f"/api/notebooks/{notebook_id}",
                headers=headers,
            ).status_code == 200

            assert production_client.get(
                "/api/notebooks",
                headers={
                    "X-API-Key": "sk-notebook-owner",
                    "X-App-Name": "untrusted-client",
                },
            ).status_code == 403
            assert production_client.get(
                "/api/notebooks",
                headers={"X-API-Key": "invalid-key", **APP_HEADER},
            ).status_code == 401
    finally:
        app.config["TESTING"] = previous_testing


def _seed_users():
    for user_id, api_key in (
        ("notebook-editor", "sk-notebook-editor"),
        ("notebook-viewer", "sk-notebook-viewer"),
        ("notebook-outsider", "sk-notebook-outsider"),
    ):
        UserDB.create({
            "user_id": user_id,
            "name": user_id,
            "api_key": api_key,
            "role": "user",
        })


def _create_notebook(client, title="Shared research"):
    response = client.post(
        "/api/notebooks",
        json={"title": title, "description": "Collaborative notes", "visibility": "shared"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 201
    return response.get_json()


def _add_member(client, notebook_id, user_id, role):
    return client.post(
        f"/api/notebooks/{notebook_id}/members",
        json={"user_id": user_id, "role": role},
        headers=OWNER_HEADERS,
    )


def test_owner_member_listing_and_outsider_isolation(client):
    _seed_users()
    notebook = _create_notebook(client)
    notebook_id = notebook["notebook_id"]
    assert notebook["access_role"] == "owner"

    added = _add_member(client, notebook_id, "notebook-editor", "editor")
    assert added.status_code == 201

    editor_list = client.get("/api/notebooks", headers=EDITOR_HEADERS)
    assert [item["notebook_id"] for item in editor_list.get_json()] == [notebook_id]
    assert editor_list.get_json()[0]["access_role"] == "editor"

    outsider_list = client.get("/api/notebooks", headers=OUTSIDER_HEADERS)
    assert outsider_list.status_code == 200
    assert outsider_list.get_json() == []
    assert client.get(
        f"/api/notebooks/{notebook_id}", headers=OUTSIDER_HEADERS
    ).status_code == 404


def test_editor_can_write_viewer_is_read_only_and_only_owner_manages_members(client):
    _seed_users()
    notebook_id = _create_notebook(client)["notebook_id"]
    assert _add_member(client, notebook_id, "notebook-editor", "editor").status_code == 201
    assert _add_member(client, notebook_id, "notebook-viewer", "viewer").status_code == 201

    editor_source = client.post(
        f"/api/notebooks/{notebook_id}/sources",
        json={
            "source_type": "url",
            "name": "Design reference",
            "reference": "https://example.com/design",
            "status": "ready",
            "metadata": {"language": "en"},
            "extracted_text": "Reference text",
        },
        headers=EDITOR_HEADERS,
    )
    assert editor_source.status_code == 201
    assert editor_source.get_json()["created_by"] == "notebook-editor"

    viewer_write = client.post(
        f"/api/notebooks/{notebook_id}/sources",
        json={"source_type": "file", "name": "blocked.txt"},
        headers=VIEWER_HEADERS,
    )
    assert viewer_write.status_code == 403

    editor_member_write = client.post(
        f"/api/notebooks/{notebook_id}/members",
        json={"user_id": "notebook-outsider", "role": "viewer"},
        headers=EDITOR_HEADERS,
    )
    assert editor_member_write.status_code == 403

    viewer_sources = client.get(
        f"/api/notebooks/{notebook_id}/sources", headers=VIEWER_HEADERS
    )
    assert viewer_sources.status_code == 200
    assert len(viewer_sources.get_json()) == 1


def test_revoked_membership_immediately_removes_access(client):
    _seed_users()
    notebook_id = _create_notebook(client)["notebook_id"]
    assert _add_member(client, notebook_id, "notebook-editor", "editor").status_code == 201
    assert client.get(
        f"/api/notebooks/{notebook_id}", headers=EDITOR_HEADERS
    ).status_code == 200

    revoked = client.delete(
        f"/api/notebooks/{notebook_id}/members/notebook-editor",
        headers=OWNER_HEADERS,
    )
    assert revoked.status_code == 200
    assert revoked.get_json()["active"] is False
    assert revoked.get_json()["revoked_at"]

    assert client.get(
        f"/api/notebooks/{notebook_id}", headers=EDITOR_HEADERS
    ).status_code == 404
    assert client.get("/api/notebooks", headers=EDITOR_HEADERS).get_json() == []


def test_nested_resources_must_belong_to_authorized_notebook(client):
    _seed_users()
    notebook_a = _create_notebook(client, "Notebook A")["notebook_id"]
    notebook_b = _create_notebook(client, "Notebook B")["notebook_id"]
    conversation = client.post(
        f"/api/notebooks/{notebook_a}/conversations",
        json={"title": "Architecture"},
        headers=OWNER_HEADERS,
    ).get_json()

    wrong_parent = client.post(
        f"/api/notebooks/{notebook_b}/conversations/{conversation['conversation_id']}/events",
        json={"event_type": "message", "message_role": "user", "content": "Wrong notebook"},
        headers=OWNER_HEADERS,
    )
    assert wrong_parent.status_code == 404

    event = client.post(
        f"/api/notebooks/{notebook_a}/conversations/{conversation['conversation_id']}/events",
        json={"event_type": "message", "message_role": "user", "content": "Use explicit ACLs"},
        headers=OWNER_HEADERS,
    )
    assert event.status_code == 201
    event_id = event.get_json()["event_id"]

    memory = client.post(
        f"/api/notebooks/{notebook_a}/memories",
        json={
            "memory_type": "decision",
            "content": "Use explicit ACLs",
            "conversation_id": conversation["conversation_id"],
            "event_id": event_id,
        },
        headers=OWNER_HEADERS,
    )
    assert memory.status_code == 201
    assert memory.get_json()["author_user_id"] == "ahmed"
    assert client.get(
        f"/api/notebooks/{notebook_a}/memories", headers=OWNER_HEADERS
    ).get_json()[0]["event_id"] == event_id

    invalid_provenance = client.post(
        f"/api/notebooks/{notebook_b}/memories",
        json={
            "memory_type": "decision",
            "content": "Invalid cross-notebook provenance",
            "conversation_id": conversation["conversation_id"],
            "event_id": event_id,
        },
        headers=OWNER_HEADERS,
    )
    assert invalid_provenance.status_code == 400


def test_artifact_versions_are_ordered_readable_and_editor_only_for_writes(client):
    _seed_users()
    notebook_id = _create_notebook(client)["notebook_id"]
    assert _add_member(client, notebook_id, "notebook-editor", "editor").status_code == 201
    assert _add_member(client, notebook_id, "notebook-viewer", "viewer").status_code == 201

    created = client.post(
        f"/api/notebooks/{notebook_id}/artifacts",
        json={
            "name": "Proposal",
            "description": "Working proposal",
            "format": "markdown",
            "content": "# Version 1",
            "metadata": {"audience": "engineering"},
        },
        headers=EDITOR_HEADERS,
    )
    assert created.status_code == 201
    artifact = created.get_json()
    artifact_id = artifact["artifact_id"]
    assert artifact["latest_version"]["version_number"] == 1

    second = client.post(
        f"/api/notebooks/{notebook_id}/artifacts/{artifact_id}/versions",
        json={"content": "# Version 2", "format": "markdown", "metadata": {"reviewed": True}},
        headers=EDITOR_HEADERS,
    )
    assert second.status_code == 201
    assert second.get_json()["version_number"] == 2

    versions = client.get(
        f"/api/notebooks/{notebook_id}/artifacts/{artifact_id}/versions",
        headers=VIEWER_HEADERS,
    )
    assert versions.status_code == 200
    assert [version["version_number"] for version in versions.get_json()] == [1, 2]

    first = client.get(
        f"/api/notebooks/{notebook_id}/artifacts/{artifact_id}/versions/1",
        headers=VIEWER_HEADERS,
    )
    assert first.status_code == 200
    assert first.get_json()["content"] == "# Version 1"

    viewer_version = client.post(
        f"/api/notebooks/{notebook_id}/artifacts/{artifact_id}/versions",
        json={"content": "# Blocked"},
        headers=VIEWER_HEADERS,
    )
    assert viewer_version.status_code == 403


def test_rendition_binary_round_trip_auth_checksum_limit_and_scope(client, monkeypatch):
    _seed_users()
    notebook_a = _create_notebook(client, "Rendition A")["notebook_id"]
    notebook_b = _create_notebook(client, "Rendition B")["notebook_id"]
    assert _add_member(client, notebook_a, "notebook-editor", "editor").status_code == 201
    assert _add_member(client, notebook_a, "notebook-viewer", "viewer").status_code == 201
    artifact = client.post(
        f"/api/notebooks/{notebook_a}/artifacts",
        json={"name": "Report", "format": "output-studio.v1", "content": "{}"},
        headers=EDITOR_HEADERS,
    ).get_json()
    artifact_id = artifact["artifact_id"]
    content = b"%PDF-1.7\nbinary\x00payload\n%%EOF"
    checksum = hashlib.sha256(content).hexdigest()
    rendition_url = (
        f"/api/notebooks/{notebook_a}/artifacts/{artifact_id}/versions/1/renditions"
    )

    uploaded = client.post(
        rendition_url,
        data={
            "file": (io.BytesIO(content), "report.pdf"),
            "format": "pdf",
            "media_type": "application/pdf",
            "filename": "report.pdf",
            "checksum": checksum,
            "renderer": "electron-print-to-pdf",
            "renderer_version": "1",
            "renderer_config": '{"page":"A4"}',
            "metadata": '{"notebook_id":"%s"}' % notebook_a,
        },
        content_type="multipart/form-data",
        headers=EDITOR_HEADERS,
    )
    assert uploaded.status_code == 201
    rendition = uploaded.get_json()
    assert rendition["byte_size"] == len(content)
    assert "content" not in rendition

    listed = client.get(rendition_url + "?limit=1", headers=VIEWER_HEADERS)
    assert listed.status_code == 200
    assert listed.get_json()["items"][0]["rendition_id"] == rendition["rendition_id"]

    downloaded = client.get(
        f"{rendition_url}/{rendition['rendition_id']}/download",
        headers=VIEWER_HEADERS,
    )
    assert downloaded.status_code == 200
    assert downloaded.data == content
    assert downloaded.content_type == "application/pdf"
    assert "report.pdf" in downloaded.headers["Content-Disposition"]

    blocked = client.post(
        rendition_url,
        json={
            "format": "pdf",
            "media_type": "application/pdf",
            "filename": "blocked.pdf",
            "renderer": "test",
            "renderer_version": "1",
        },
        headers=VIEWER_HEADERS,
    )
    assert blocked.status_code == 403

    mismatch = client.post(
        rendition_url,
        data={
            "file": (io.BytesIO(content), "bad.pdf"),
            "format": "pdf",
            "media_type": "application/pdf",
            "filename": "bad.pdf",
            "checksum": "0" * 64,
            "renderer": "test",
            "renderer_version": "1",
        },
        content_type="multipart/form-data",
        headers=EDITOR_HEADERS,
    )
    assert mismatch.status_code == 400
    assert "checksum" in mismatch.get_json()["error"].lower()

    monkeypatch.setattr("routes.notebooks.MAX_RENDITION_BYTES", 8)
    oversized = client.post(
        rendition_url,
        data={
            "file": (io.BytesIO(b"%PDF-more-than-eight"), "large.pdf"),
            "format": "pdf",
            "media_type": "application/pdf",
            "filename": "large.pdf",
            "checksum": hashlib.sha256(b"%PDF-more-than-eight").hexdigest(),
            "renderer": "test",
            "renderer_version": "1",
        },
        content_type="multipart/form-data",
        headers=EDITOR_HEADERS,
    )
    assert oversized.status_code == 413

    cross_notebook = client.get(
        f"/api/notebooks/{notebook_b}/artifacts/{artifact_id}/versions/1/renditions",
        headers=OWNER_HEADERS,
    )
    assert cross_notebook.status_code == 404


def test_artifact_version_numbers_are_concurrent_and_unique(client):
    notebook_id = _create_notebook(client, "Concurrent versions")["notebook_id"]
    artifact = client.post(
        f"/api/notebooks/{notebook_id}/artifacts",
        json={"name": "Concurrent", "content": "v1"},
        headers=OWNER_HEADERS,
    ).get_json()

    from db.notebooks import NotebookDB

    with ThreadPoolExecutor(max_workers=4) as executor:
        versions = list(executor.map(
            lambda index: NotebookDB.create_artifact_version(
                notebook_id,
                artifact["artifact_id"],
                {"content": f"v{index + 2}", "metadata": {"index": index}},
                "ahmed",
            ),
            range(6),
        ))

    assert sorted(version["version_number"] for version in versions) == [2, 3, 4, 5, 6, 7]


def test_collaboration_directory_and_batch_assignment_are_safe_and_transactional(client):
    _seed_users()
    UserDB.create({
        "user_id": "notebook-disabled",
        "name": "Disabled",
        "email": "disabled@example.com",
        "api_key": "sk-notebook-disabled",
        "role": "user",
        "is_active": 0,
    })
    first = _create_notebook(client, "One")["notebook_id"]
    second = _create_notebook(client, "Two")["notebook_id"]

    directory = client.get(
        "/api/notebooks/collaboration/users?limit=2",
        headers=OWNER_HEADERS,
    )
    assert directory.status_code == 200
    assert len(directory.get_json()["items"]) == 2
    assert all(
        set(user) <= {"user_id", "name", "email"}
        for user in directory.get_json()["items"]
    )

    failed = client.post(
        "/api/notebooks/collaboration/assignments",
        json={
            "operations": [
                {
                    "notebook_id": first,
                    "user_id": "notebook-editor",
                    "role": "editor",
                },
                {
                    "notebook_id": second,
                    "user_id": "notebook-disabled",
                    "role": "viewer",
                },
            ]
        },
        headers=OWNER_HEADERS,
    )
    assert failed.status_code == 400
    assert client.get(
        f"/api/notebooks/{first}", headers=EDITOR_HEADERS
    ).status_code == 404

    assigned = client.post(
        "/api/notebooks/collaboration/assignments",
        json={
            "notebook_ids": [first, second],
            "user_ids": ["notebook-editor"],
            "role": "editor",
        },
        headers=OWNER_HEADERS,
    )
    assert assigned.status_code == 200
    assert assigned.get_json()["count"] == 2


def test_shared_source_snapshot_tagline_and_safe_cover(client):
    notebook_id = _create_notebook(client)["notebook_id"]
    updated = client.patch(
        f"/api/notebooks/{notebook_id}",
        json={"tagline": "Ship durable context", "cover_style": {"palette": "sunset"}},
        headers=OWNER_HEADERS,
    )
    assert updated.status_code == 200
    assert updated.get_json()["tagline"] == "Ship durable context"

    content = "Shared extracted content"
    source = client.post(
        f"/api/notebooks/{notebook_id}/sources",
        json={
            "source_type": "file",
            "name": "notes.txt",
            "reference": "/Users/private/notes.txt",
            "content_snapshot": content,
            "extracted_text": content,
            "media_type": "text/plain",
            "provenance": {"extractor": "test"},
        },
        headers=OWNER_HEADERS,
    )
    assert source.status_code == 201
    assert source.get_json()["content_snapshot"] == content
    assert source.get_json()["reference"] == ""
    assert len(source.get_json()["content_hash"]) == 64

    png = b"\x89PNG\r\n\x1a\n" + b"safe-image"
    cover = client.post(
        f"/api/notebooks/{notebook_id}/cover",
        json={
            "media_type": "image/png",
            "content_base64": base64.b64encode(png).decode(),
            "style": {"palette": ["#112233", "#abcdef"]},
        },
        headers=OWNER_HEADERS,
    )
    assert cover.status_code == 201
    assert cover.get_json()["media_type"] == "image/png"
    assert client.get(
        f"/api/notebooks/{notebook_id}/cover", headers=OWNER_HEADERS
    ).data == png
    cleared = client.delete(
        f"/api/notebooks/{notebook_id}/cover",
        headers=OWNER_HEADERS,
    )
    assert cleared.status_code == 200
    assert cleared.get_json()["cover"] is None
    assert client.get(
        f"/api/notebooks/{notebook_id}/cover", headers=OWNER_HEADERS
    ).status_code == 404
    rejected_svg = client.post(
        f"/api/notebooks/{notebook_id}/cover",
        json={
            "media_type": "image/svg+xml",
            "content_base64": base64.b64encode(b"<svg><script/></svg>").decode(),
        },
        headers=OWNER_HEADERS,
    )
    assert rejected_svg.status_code == 400


def test_deleted_events_are_excluded_and_compaction_returns_post_cutoff(client):
    _seed_users()
    notebook_id = _create_notebook(client)["notebook_id"]
    assert _add_member(client, notebook_id, "notebook-editor", "editor").status_code == 201
    conversation_id = client.post(
        f"/api/notebooks/{notebook_id}/conversations",
        json={"title": "Lifecycle"},
        headers=OWNER_HEADERS,
    ).get_json()["conversation_id"]
    first = client.post(
        f"/api/notebooks/{notebook_id}/conversations/{conversation_id}/events",
        json={"content": "first"},
        headers=EDITOR_HEADERS,
    ).get_json()
    second = client.post(
        f"/api/notebooks/{notebook_id}/conversations/{conversation_id}/events",
        json={"content": "second"},
        headers=EDITOR_HEADERS,
    ).get_json()

    deleted = client.delete(
        f"/api/notebooks/{notebook_id}/conversations/{conversation_id}/events/{first['event_id']}",
        json={"reason": "duplicate"},
        headers=EDITOR_HEADERS,
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["content"] is None
    assert [event["event_id"] for event in client.get(
        f"/api/notebooks/{notebook_id}/conversations/{conversation_id}/events",
        headers=OWNER_HEADERS,
    ).get_json()] == [second["event_id"]]

    compacted = client.post(
        f"/api/notebooks/{notebook_id}/conversations/{conversation_id}/compactions",
        json={
            "summary_content": "Summary through second",
            "cutoff_event_id": second["event_id"],
            "athena_run_metadata": {"run": "athena-1"},
        },
        headers=EDITOR_HEADERS,
    )
    assert compacted.status_code == 201
    third = client.post(
        f"/api/notebooks/{notebook_id}/conversations/{conversation_id}/events",
        json={"content": "third"},
        headers=EDITOR_HEADERS,
    ).get_json()
    context = client.get(
        f"/api/notebooks/{notebook_id}/conversations/{conversation_id}/events?view=compacted",
        headers=OWNER_HEADERS,
    ).get_json()
    assert context["compaction"]["cutoff_event_id"] == second["event_id"]
    assert [event["event_id"] for event in context["events"]] == [third["event_id"]]


def test_engram_candidates_provenance_snapshot_and_concurrency(client):
    _seed_users()
    notebook_id = _create_notebook(client)["notebook_id"]
    conversation_id = client.post(
        f"/api/notebooks/{notebook_id}/conversations",
        json={"title": "Engram"},
        headers=OWNER_HEADERS,
    ).get_json()["conversation_id"]
    event_id = client.post(
        f"/api/notebooks/{notebook_id}/conversations/{conversation_id}/events",
        json={"content": "PostgreSQL is canonical"},
        headers=OWNER_HEADERS,
    ).get_json()["event_id"]
    candidate = client.post(
        f"/api/notebooks/{notebook_id}/engrams/items",
        json={
            "item_type": "fact",
            "title": "Canonical store",
            "content": "PostgreSQL is the canonical store.",
            "provenance": [{
                "conversation_id": conversation_id,
                "originating_operator_event_id": event_id,
                "confidence": 0.99,
            }],
        },
        headers=OWNER_HEADERS,
    )
    assert candidate.status_code == 201
    item = candidate.get_json()
    assert item["status"] == "candidate"
    assert item["provenance"][0]["originating_operator_event_id"] == event_id

    stale = client.post(
        f"/api/notebooks/{notebook_id}/engrams/items/batch",
        json={
            "item_ids": [item["item_id"]],
            "action": "accept",
            "expected_revision": 0,
        },
        headers=OWNER_HEADERS,
    )
    assert stale.status_code == 409
    accepted = client.post(
        f"/api/notebooks/{notebook_id}/engrams/items/batch",
        json={"item_ids": [item["item_id"]], "action": "accept"},
        headers=OWNER_HEADERS,
    )
    assert accepted.status_code == 200
    current = client.get(
        f"/api/notebooks/{notebook_id}/engrams/current",
        headers=VIEWER_HEADERS,
    )
    assert current.status_code == 404
    current = client.get(
        f"/api/notebooks/{notebook_id}/engrams/current",
        headers=OWNER_HEADERS,
    ).get_json()
    assert [entry["item_id"] for entry in current["items"]] == [item["item_id"]]
    all_items = client.get(
        f"/api/notebooks/{notebook_id}/engrams/items",
        headers=OWNER_HEADERS,
    ).get_json()
    assert [entry["item_id"] for entry in all_items["items"]] == [item["item_id"]]
    snapshot = client.post(
        f"/api/notebooks/{notebook_id}/engrams/snapshots",
        json={"expected_revision": current["revision"]},
        headers=OWNER_HEADERS,
    )
    assert snapshot.status_code == 201
    assert snapshot.get_json()["accepted_manifest"][0]["item_id"] == item["item_id"]
    assert client.delete(
        f"/api/notebooks/{notebook_id}", headers=OWNER_HEADERS
    ).status_code == 200
