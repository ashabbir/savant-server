"""Tests for differential graph and index generation after GitHub/GitLab refresh."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from context.db import ContextDB
from context.indexer import Indexer
from context.ingestion import IngestedProject
from db.code_intelligence import CodeIntelligenceConfigDB
from db.jobs import JobDB


def test_differential_indexing_removes_deleted_and_skips_unchanged(tmp_path, _isolated_db, monkeypatch):
    repo_dir = tmp_path / "diff-repo"
    repo_dir.mkdir()
    subprocess.run(["git", "-C", str(repo_dir), "init"], check=True, capture_output=True)

    file_a = repo_dir / "file_a.py"
    file_b = repo_dir / "file_b.py"

    file_a.write_text("def fn_a(): pass\n")
    file_b.write_text("def fn_b(): pass\n")

    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)

    # Mock embedder
    mock_embedder = MagicMock()
    mock_embedder.embed_one.return_value = [0.1] * 768
    monkeypatch.setattr("context.embeddings.EmbeddingModel.get", lambda: mock_embedder)

    # Initial full index (clear=True)
    indexer = Indexer()
    res1 = indexer.index_repository(repo_dir, repo_name="diff-repo", clear=True)
    assert res1["files_indexed"] == 2

    repo = ContextDB.get_repo("diff-repo")
    assert repo is not None
    repo_id = repo["id"]

    # Verify initial stored files
    stored_before = ContextDB.get_repo_files_mtime(repo_id)
    assert "file_a.py" in stored_before
    assert "file_b.py" in stored_before

    # Now simulate git pull changes:
    # 1. Delete file_a.py
    file_a.unlink()
    # 2. Modify file_b.py
    file_b.write_text("def fn_b(): return 'updated'\n")
    # 3. Add file_c.py
    file_c = repo_dir / "file_c.py"
    file_c.write_text("def fn_c(): pass\n")

    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True, capture_output=True)

    # Run differential index (clear=False, differential=True)
    res2 = indexer.index_repository(repo_dir, repo_name="diff-repo", clear=False, differential=True)

    assert res2["files_removed"] == 1  # file_a.py removed
    assert res2["files_indexed"] == 2  # file_b.py updated, file_c.py added
    assert res2["files_skipped"] == 0

    stored_after = ContextDB.get_repo_files_mtime(repo_id)
    assert "file_a.py" not in stored_after
    assert "file_b.py" in stored_after
    assert "file_c.py" in stored_after

    # Run differential index again without any file changes
    res3 = indexer.index_repository(repo_dir, repo_name="diff-repo", clear=False, differential=True)
    assert res3["files_removed"] == 0
    assert res3["files_indexed"] == 0
    assert res3["files_skipped"] == 2  # file_b.py and file_c.py skipped because unchanged


def test_refresh_route_triggers_differential_sync_when_conditions_met(client, _isolated_db, monkeypatch):
    from context import routes

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setattr(routes, "_validate_repo_path", lambda _repo: (Path("/tmp/repos/gh-repo"), None))

    repo_record = ContextDB.add_repo("gh-repo", "/tmp/repos/gh-repo")
    ContextDB.update_repo_status("gh-repo", "indexed", indexed_at="2026-07-19T10:00:00Z")

    # Repo is graphed
    CodeIntelligenceConfigDB.upsert("gh-repo", provider="codegraph", freshness="fresh")

    # Mock update_repo returning changed=True and provider="github"
    refreshed_obj = IngestedProject(
        name="gh-repo", path="/tmp/repos/gh-repo", changed=True, provider="github"
    )
    monkeypatch.setattr("context.ingestion.refresh_repo", lambda path, branch=None: refreshed_obj)

    resp = client.post("/api/context/repos/gh-repo/refresh")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("differential_sync_triggered") is True
    assert "differential_sync_job_id" in data

    # Verify a job was enqueued in JobDB
    active_job = JobDB.find_active("differential_sync", "gh-repo")
    assert active_job is not None


def test_refresh_route_skips_differential_sync_if_no_code_changes(client, _isolated_db, monkeypatch):
    from context import routes

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setattr(routes, "_validate_repo_path", lambda _repo: (Path("/tmp/repos/no-change-repo"), None))

    ContextDB.add_repo("no-change-repo", "/tmp/repos/no-change-repo")
    ContextDB.update_repo_status("no-change-repo", "indexed", indexed_at="2026-07-19T10:00:00Z")
    CodeIntelligenceConfigDB.upsert("no-change-repo", provider="codegraph", freshness="fresh")

    # Mock update_repo returning changed=False
    refreshed_obj = IngestedProject(
        name="no-change-repo", path="/tmp/repos/no-change-repo", changed=False, provider="github"
    )
    monkeypatch.setattr("context.ingestion.refresh_repo", lambda path, branch=None: refreshed_obj)

    resp = client.post("/api/context/repos/no-change-repo/refresh")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "differential_sync_triggered" not in data


def test_trigger_differential_sync_endpoint(client, _isolated_db, monkeypatch):
    from context import routes

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setattr(routes, "_validate_repo_path", lambda _repo: (Path("/tmp/repos/my-repo"), None))

    ContextDB.add_repo("my-repo", "/tmp/repos/my-repo")
    ContextDB.update_repo_status("my-repo", "indexed", indexed_at="2026-07-19T10:00:00Z")
    CodeIntelligenceConfigDB.upsert("my-repo", provider="codegraph", freshness="fresh")
    monkeypatch.setattr("context.ingestion.inspect_project_source", lambda path: {"source": "gitlab"})

    resp = client.post("/api/context/repos/my-repo/differential-sync")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["started"] is True
    assert data["name"] == "my-repo"
    assert "job_id" in data
