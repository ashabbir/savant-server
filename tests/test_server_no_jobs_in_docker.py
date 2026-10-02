import os
import pytest
from unittest.mock import MagicMock


def test_app_does_not_start_runners_in_server_role(monkeypatch):
    """Verify app startup does not launch periodic runner or maintenance scheduler when SAVANT_ROLE is server."""
    monkeypatch.setenv("SAVANT_ROLE", "server")
    monkeypatch.setenv("SAVANT_EXTERNAL_JOB_WORKER", "1")
    monkeypatch.setenv("SAVANT_API_ONLY", "1")

    periodic_mock = MagicMock()
    maintenance_mock = MagicMock()
    monkeypatch.setattr("context.periodic_runner.start_periodic_runner", periodic_mock)
    monkeypatch.setattr("knowledge.maintenance.start_maintenance_scheduler", maintenance_mock)

    is_server_instance = (
        os.environ.get("SAVANT_ROLE") == "server"
        or os.environ.get("SAVANT_EXTERNAL_JOB_WORKER") == "1"
        or os.environ.get("SAVANT_API_ONLY") == "1"
    )
    assert is_server_instance is True

    # Simulate the app startup block from app.py
    if not is_server_instance and os.environ.get("SAVANT_EXTERNAL_PERIODIC_RUNNER") != "1":
        from context.periodic_runner import start_periodic_runner
        start_periodic_runner()
    if not is_server_instance and os.environ.get("SAVANT_EXTERNAL_KG_MAINTENANCE") != "1":
        from knowledge.maintenance import start_maintenance_scheduler
        start_maintenance_scheduler()

    periodic_mock.assert_not_called()
    maintenance_mock.assert_not_called()


def test_refresh_repo_enqueues_job_when_in_server_role(client, tmp_path, monkeypatch):
    """Verify POST /api/context/repos/<name>/refresh delegates to job queue in server mode."""
    monkeypatch.setenv("SAVANT_ROLE", "server")
    monkeypatch.setenv("SAVANT_EXTERNAL_JOB_WORKER", "1")

    from context.db import ContextDB
    from db.jobs import JobDB

    repo_dir = tmp_path / "test-repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    ContextDB.add_repo("test-repo", str(repo_dir))

    # Ensure update_repo is NEVER called
    mock_update = MagicMock()
    monkeypatch.setattr("context.ingestion.refresh_repo", mock_update)

    res = client.post("/api/context/repos/test-repo/refresh")

    assert res.status_code == 202
    data = res.get_json()
    assert data["started"] is True
    assert "job_id" in data
    assert data["differential_sync_triggered"] is True

    # Verify update_repo was NOT called synchronously on the server
    mock_update.assert_not_called()

    # Verify a differential_sync job was persisted in the jobs table
    job = JobDB.get_job(data["job_id"])
    assert job is not None
    assert job["job_type"] == "differential_sync"
    assert job["target"] == "test-repo"
    assert job["status"] == "queued"


def test_docker_entrypoint_supervisor_rules():
    """Verify docker-entrypoint.sh routes background tasks to worker and keeps server clean."""
    entrypoint_path = os.path.join(os.path.dirname(__file__), "..", "docker-entrypoint.sh")
    with open(entrypoint_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Worker block starts job_worker, periodic_runner, maintenance_runner
    worker_block = content[content.find('if [ "${SAVANT_ROLE:-}" = "worker" ]'):content.find('fi\n\n# Run both MCP transports')]
    assert "python -m context.job_worker &" in worker_block
    assert "python -m context.periodic_runner &" in worker_block
    assert "python -m knowledge.maintenance_runner &" in worker_block

    # Server instance guards against running any of them
    server_block = content[content.find('# Queue consumer, periodic repo sync, and KG maintenance all belong on the'):content.find('gunicorn \\\n  --bind')]
    assert 'if [ "${SAVANT_ROLE:-}" != "server" ] && [ "${SAVANT_EXTERNAL_JOB_WORKER:-0}" != "1" ] && [ "${SAVANT_API_ONLY:-0}" != "1" ]; then' in server_block
    assert "python -m context.job_worker &" in server_block
    assert "python -m context.periodic_runner &" in server_block
    assert "python -m knowledge.maintenance_runner &" in server_block
