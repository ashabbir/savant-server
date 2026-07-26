"""Tests for the 2-hour periodic repository sync runner and logging."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from context.db import ContextDB
from context.ingestion import IngestedProject
from context.periodic_runner import (
    _execute_sync_pass_for_all_repos,
    get_sync_interval_seconds,
    get_runner_status,
    start_periodic_runner,
    stop_periodic_runner,
)
from db.code_intelligence import CodeIntelligenceConfigDB


def test_periodic_sync_defaults_to_two_hours_and_allows_override(monkeypatch):
    monkeypatch.delenv("PERIODIC_SYNC_INTERVAL_HOURS", raising=False)
    assert get_sync_interval_seconds() == 2 * 3600

    monkeypatch.setenv("PERIODIC_SYNC_INTERVAL_HOURS", "3.5")
    assert get_sync_interval_seconds() == 3.5 * 3600


def test_periodic_sync_pass_runs_for_all_projects(tmp_path, _isolated_db, monkeypatch):
    # Setup two mock project directories
    repo1_dir = tmp_path / "repo-alpha"
    repo1_dir.mkdir()
    subprocess.run(["git", "-C", str(repo1_dir), "init"], check=True, capture_output=True)
    (repo1_dir / "main.py").write_text("def alpha(): pass\n")
    subprocess.run(["git", "-C", str(repo1_dir), "add", "."], check=True, capture_output=True)

    repo2_dir = tmp_path / "repo-beta"
    repo2_dir.mkdir()
    subprocess.run(["git", "-C", str(repo2_dir), "init"], check=True, capture_output=True)
    (repo2_dir / "index.js").write_text("function beta() {}\n")
    subprocess.run(["git", "-C", str(repo2_dir), "add", "."], check=True, capture_output=True)

    ContextDB.add_repo("repo-alpha", str(repo1_dir))
    ContextDB.add_repo("repo-beta", str(repo2_dir))

    monkeypatch.setattr(
        ContextDB,
        "list_repos",
        staticmethod(lambda: [
            {"id": 1, "name": "repo-alpha", "path": str(repo1_dir), "status": "indexed"},
            {"id": 2, "name": "repo-beta", "path": str(repo2_dir), "status": "indexed"},
        ]),
    )

    # Mock embedder
    mock_embedder = MagicMock()
    mock_embedder.embed_one.return_value = [0.1] * 768
    monkeypatch.setattr("context.embeddings.EmbeddingModel.get", lambda: mock_embedder)

    # Mock git refresh to return changed=True
    monkeypatch.setattr(
        "context.ingestion.refresh_repo",
        lambda p: IngestedProject(name=Path(p).name, path=p, changed=True, provider="github"),
    )

    # Mock CodeGraph build service
    mock_ci_res = MagicMock()
    mock_ci_res.freshness = "fresh"
    mock_health = MagicMock()
    mock_health.provider = "codegraph"
    mock_health.graph_version = "1.4.1:unknown"
    mock_health.indexed_at = "2026-07-19T17:00:00Z"
    mock_health.freshness.value = "fresh"

    mock_service = MagicMock()
    mock_service.ensure_index.return_value = mock_ci_res
    mock_service.health.return_value = mock_health
    monkeypatch.setattr("code_intelligence.runtime.build_service", lambda: mock_service)

    summary = _execute_sync_pass_for_all_repos()

    assert summary["count"] == 2
    assert len(summary["results"]) == 2

    res_names = [r["repo_name"] for r in summary["results"]]
    assert "repo-alpha" in res_names
    assert "repo-beta" in res_names

    for r in summary["results"]:
        assert r["status"] == "success"
        assert r["fetched"] is True
        assert r["code_changed"] is True
        assert r["indexed"] is True
        assert r["graphed"] is True

    # Verify logs were recorded in the unified repository activity table.
    logs = ContextDB.list_periodic_sync_logs()
    assert len(logs) >= 2
    logged_repos = [l["repo_name"] for l in logs]
    assert "repo-alpha" in logged_repos
    assert "repo-beta" in logged_repos


def test_periodic_sync_runner_status_and_lifecycle():
    stop_periodic_runner()
    status1 = get_runner_status()
    assert status1["running"] is False

    start_periodic_runner()
    status2 = get_runner_status()
    assert status2["running"] is True

    stop_periodic_runner()
    status3 = get_runner_status()
    assert status3["running"] is False


def test_periodic_sync_api_endpoints(client, _isolated_db, monkeypatch):
    from context import routes

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)

    ContextDB.record_repo_sync_log(
        repo_name="demo-repo",
        operation="periodic_refresh",
        trigger="scheduled",
        provider="github",
        branch="main",
        status="success",
        before_commit="abc123",
        after_commit="def456",
        fetched=True,
        code_changed=True,
        indexed=True,
        graphed=True,
        details="Fetched origin (code_changed=True); Indexed; CodeGraph synced",
        duration_ms=125,
    )

    # Status route
    resp = client.get("/api/context/repos/periodic-sync/status")
    assert resp.status_code == 200
    assert "running" in resp.get_json()

    # Logs route
    resp = client.get("/api/context/repos/periodic-sync/logs?repo_name=demo-repo")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] >= 1
    assert data["logs"][0]["repo_name"] == "demo-repo"
    assert data["logs"][0]["operation"] == "periodic_refresh"
    assert data["logs"][0]["after_commit"] == "def456"

    resp = client.get("/api/context/repos/sync-logs?repo_name=demo-repo")
    assert resp.status_code == 200
    assert resp.get_json()["logs"][0]["duration_ms"] == 125

    resp = client.get("/api/context/repos/demo-repo/sync-logs")
    assert resp.status_code == 200
    assert resp.get_json()["logs"][0]["trigger"] == "scheduled"

    # Manual run route
    monkeypatch.setattr(
        "context.periodic_runner.run_periodic_sync_now",
        lambda **_kwargs: {"count": 1, "results": []},
    )
    resp = client.post("/api/context/repos/periodic-sync/run")
    assert resp.status_code == 200
    assert resp.get_json()["count"] == 1
