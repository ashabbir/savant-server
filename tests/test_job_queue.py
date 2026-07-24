"""TDD tests for the persistent job queue (v8.6.0).

Tests cover:
  1. JobDB — CRUD, dedup, lifecycle, cleanup
  2. Job API routes — submit, status, list, cancel, delete
  3. Job worker — execution, progress, cancellation
  4. Backward-compat — indexing-status merges job data
"""

import json
import time
import threading
import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. JobDB unit tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestJobDB:
    """Direct DB layer tests."""

    def test_create_job(self, _isolated_db):
        from db.jobs import JobDB
        job = JobDB.create_job("index", "my-repo")
        assert job["id"]
        assert job["job_type"] == "index"
        assert job["target"] == "my-repo"
        assert job["status"] == "queued"
        assert job["progress"] == 0
        assert job["phase"] == "Queued"
        assert job["created_at"]

    def test_get_job(self, _isolated_db):
        from db.jobs import JobDB
        job = JobDB.create_job("ast", "repo-x")
        fetched = JobDB.get_job(job["id"])
        assert fetched is not None
        assert fetched["id"] == job["id"]
        assert fetched["job_type"] == "ast"
        assert fetched["target"] == "repo-x"

    def test_get_job_not_found(self, _isolated_db):
        from db.jobs import JobDB
        assert JobDB.get_job("nonexistent-id") is None

    def test_find_active_dedup(self, _isolated_db):
        """Submitting the same (type, target) twice returns existing job."""
        from db.jobs import JobDB
        j1 = JobDB.create_job("index", "repo-a")
        active = JobDB.find_active("index", "repo-a")
        assert active is not None
        assert active["id"] == j1["id"]

    def test_find_active_none_when_done(self, _isolated_db):
        from db.jobs import JobDB
        j = JobDB.create_job("index", "repo-a")
        JobDB.set_done(j["id"], {"files": 10})
        assert JobDB.find_active("index", "repo-a") is None

    def test_find_active_different_type(self, _isolated_db):
        from db.jobs import JobDB
        JobDB.create_job("index", "repo-a")
        assert JobDB.find_active("ast", "repo-a") is None

    def test_next_queued_fifo(self, _isolated_db):
        from db.jobs import JobDB
        j1 = JobDB.create_job("index", "repo-1")
        j2 = JobDB.create_job("index", "repo-2")
        nxt = JobDB.next_queued()
        assert nxt["id"] == j1["id"]  # FIFO — oldest first

    def test_next_queued_skips_running(self, _isolated_db):
        from db.jobs import JobDB
        j1 = JobDB.create_job("index", "repo-1")
        JobDB.set_running(j1["id"])
        j2 = JobDB.create_job("index", "repo-2")
        nxt = JobDB.next_queued()
        assert nxt["id"] == j2["id"]

    def test_next_queued_empty(self, _isolated_db):
        from db.jobs import JobDB
        assert JobDB.next_queued() is None

    def test_lifecycle_queued_running_done(self, _isolated_db):
        from db.jobs import JobDB
        j = JobDB.create_job("index", "repo-a")
        assert j["status"] == "queued"

        JobDB.set_running(j["id"])
        j = JobDB.get_job(j["id"])
        assert j["status"] == "running"
        assert j["started_at"] is not None

        JobDB.update_progress(j["id"], 50, "Embedding", "file.py")
        j = JobDB.get_job(j["id"])
        assert j["progress"] == 50
        assert j["phase"] == "Embedding"
        assert j["message"] == "file.py"

        JobDB.set_done(j["id"], {"files_indexed": 100})
        j = JobDB.get_job(j["id"])
        assert j["status"] == "done"
        assert j["progress"] == 100
        assert j["finished_at"] is not None
        assert j["result"]["files_indexed"] == 100

    def test_lifecycle_failed(self, _isolated_db):
        from db.jobs import JobDB
        j = JobDB.create_job("ast", "repo-b")
        JobDB.set_running(j["id"])
        JobDB.set_failed(j["id"], "Disk full")
        j = JobDB.get_job(j["id"])
        assert j["status"] == "failed"
        assert j["message"] == "Disk full"
        assert j["finished_at"] is not None

    def test_cancel_request(self, _isolated_db):
        from db.jobs import JobDB
        j = JobDB.create_job("index", "repo-a")
        JobDB.set_running(j["id"])
        assert not JobDB.is_cancel_requested(j["id"])

        ok = JobDB.request_cancel(j["id"])
        assert ok is True
        assert JobDB.is_cancel_requested(j["id"])

    def test_cancel_queued_job_finishes_immediately(self, _isolated_db):
        from db.jobs import JobDB
        j = JobDB.create_job("codegraph_sync", "repo-a")

        ok = JobDB.request_cancel(j["id"])

        assert ok is True
        cancelled = JobDB.get_job(j["id"])
        assert cancelled["status"] == "cancelled"
        assert cancelled["phase"] == "Cancelled"
        assert cancelled["finished_at"] is not None
        assert JobDB.find_active("codegraph_sync", "repo-a") is None

    def test_cancel_done_job_no_op(self, _isolated_db):
        from db.jobs import JobDB
        j = JobDB.create_job("index", "repo-a")
        JobDB.set_done(j["id"])
        ok = JobDB.request_cancel(j["id"])
        assert ok is False

    def test_set_cancelled(self, _isolated_db):
        from db.jobs import JobDB
        j = JobDB.create_job("index", "repo-a")
        JobDB.set_cancelled(j["id"])
        j = JobDB.get_job(j["id"])
        assert j["status"] == "cancelled"

    def test_recover_interrupted_jobs(self, _isolated_db):
        from db.jobs import JobDB
        running = JobDB.create_job("codegraph_sync", "repo-running")
        JobDB.set_running(running["id"])
        cancelling = JobDB.create_job("index", "repo-cancelling")
        JobDB.set_running(cancelling["id"])
        JobDB.request_cancel(cancelling["id"])

        assert JobDB.recover_interrupted() >= 2
        assert JobDB.get_job(running["id"])["status"] == "cancelled"
        assert JobDB.get_job(cancelling["id"])["status"] == "cancelled"

    def test_list_jobs_no_filter(self, _isolated_db):
        from db.jobs import JobDB
        JobDB.create_job("index", "r1")
        JobDB.create_job("ast", "r2")
        jobs = JobDB.list_jobs()
        assert len(jobs) == 2

    def test_list_jobs_status_filter(self, _isolated_db):
        from db.jobs import JobDB
        j1 = JobDB.create_job("index", "r1")
        j2 = JobDB.create_job("ast", "r2")
        JobDB.set_running(j1["id"])
        queued = JobDB.list_jobs(status="queued")
        assert len(queued) == 1
        assert queued[0]["id"] == j2["id"]

    def test_list_jobs_target_filter(self, _isolated_db):
        from db.jobs import JobDB
        JobDB.create_job("index", "r1")
        JobDB.create_job("ast", "r2")
        jobs = JobDB.list_jobs(target="r1")
        assert len(jobs) == 1
        assert jobs[0]["target"] == "r1"

    def test_list_jobs_limit(self, _isolated_db):
        from db.jobs import JobDB
        for i in range(5):
            JobDB.create_job("index", f"r{i}")
        jobs = JobDB.list_jobs(limit=3)
        assert len(jobs) == 3

    def test_delete_job_finished(self, _isolated_db):
        from db.jobs import JobDB
        j = JobDB.create_job("index", "r1")
        JobDB.set_done(j["id"])
        ok = JobDB.delete_job(j["id"])
        assert ok is True
        assert JobDB.get_job(j["id"]) is None

    def test_delete_job_running_blocked(self, _isolated_db):
        """Cannot delete a running job."""
        from db.jobs import JobDB
        j = JobDB.create_job("index", "r1")
        JobDB.set_running(j["id"])
        ok = JobDB.delete_job(j["id"])
        assert ok is False
        assert JobDB.get_job(j["id"]) is not None

    def test_get_running_job(self, _isolated_db):
        from db.jobs import JobDB
        assert JobDB.get_running_job() is None
        j = JobDB.create_job("index", "r1")
        JobDB.set_running(j["id"])
        running = JobDB.get_running_job()
        assert running["id"] == j["id"]

    def test_progress_clamped(self, _isolated_db):
        from db.jobs import JobDB
        j = JobDB.create_job("index", "r1")
        JobDB.update_progress(j["id"], 150, "Over")
        j = JobDB.get_job(j["id"])
        assert j["progress"] == 100

        JobDB.update_progress(j["id"], -10, "Under")
        j = JobDB.get_job(j["id"])
        assert j["progress"] == 0

    def test_cleanup_old(self, _isolated_db):
        from db.jobs import JobDB
        j = JobDB.create_job("index", "r1")
        JobDB.set_done(j["id"])
        # Manually backdate finished_at
        from sqlite_client import get_connection
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET finished_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (j["id"],),
        )
        conn.commit()
        JobDB.cleanup_old(max_age_hours=1)
        assert JobDB.get_job(j["id"]) is None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Job API route tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestJobRoutes:
    """Flask route tests via test client."""

    def test_submit_job(self, client):
        resp = client.post("/api/jobs/submit",
                           json={"job_type": "index", "target": "repo-x"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["job_id"]
        assert data["status"] == "queued"

    def test_submit_job_missing_fields(self, client):
        resp = client.post("/api/jobs/submit", json={"job_type": "index"})
        assert resp.status_code == 400

    def test_submit_job_invalid_type(self, client):
        resp = client.post("/api/jobs/submit",
                           json={"job_type": "invalid", "target": "repo-x"})
        assert resp.status_code == 400

    def test_submit_dedup(self, client):
        """Submitting same job twice returns existing job."""
        r1 = client.post("/api/jobs/submit",
                         json={"job_type": "index", "target": "repo-x"})
        r2 = client.post("/api/jobs/submit",
                         json={"job_type": "index", "target": "repo-x"})
        d1 = r1.get_json()
        d2 = r2.get_json()
        assert d1["job_id"] == d2["job_id"]
        assert d2.get("reused") is True

    def test_get_status(self, client):
        r = client.post("/api/jobs/submit",
                        json={"job_type": "ast", "target": "repo-y"})
        job_id = r.get_json()["job_id"]
        resp = client.get(f"/api/jobs/status?id={job_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "queued"
        assert data["id"] == job_id

    def test_get_status_not_found(self, client):
        resp = client.get("/api/jobs/status?id=nonexistent")
        assert resp.status_code == 404

    def test_list_jobs(self, client):
        client.post("/api/jobs/submit",
                    json={"job_type": "index", "target": "r1"})
        client.post("/api/jobs/submit",
                    json={"job_type": "ast", "target": "r2"})
        resp = client.get("/api/jobs/list")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["jobs"]) == 2

    def test_list_jobs_status_filter(self, client):
        r = client.post("/api/jobs/submit",
                        json={"job_type": "index", "target": "r1"})
        from db.jobs import JobDB
        JobDB.set_running(r.get_json()["job_id"])
        client.post("/api/jobs/submit",
                    json={"job_type": "ast", "target": "r2"})
        resp = client.get("/api/jobs/list?status=queued")
        data = resp.get_json()
        assert len(data["jobs"]) == 1

    def test_cancel_job(self, client):
        r = client.post("/api/jobs/submit",
                        json={"job_type": "index", "target": "r1"})
        job_id = r.get_json()["job_id"]
        from db.jobs import JobDB
        JobDB.set_running(job_id)

        resp = client.post("/api/jobs/cancel", json={"job_id": job_id})
        assert resp.status_code == 200
        assert resp.get_json()["cancelled"] is True

        # Verify it's marked cancelling
        job = JobDB.get_job(job_id)
        assert job["status"] == "cancelling"

    def test_cancel_queued_graph_job(self, client):
        from db.jobs import JobDB
        r = client.post("/api/jobs/submit",
                        json={"job_type": "codegraph_sync", "target": "repo-graph"})
        job_id = r.get_json()["job_id"]

        resp = client.post("/api/jobs/cancel", json={"job_id": job_id})

        assert resp.status_code == 200
        assert resp.get_json()["cancelled"] is True
        assert JobDB.get_job(job_id)["status"] == "cancelled"

    def test_cancel_running_graph_job_stops_bridge_request(self, client, monkeypatch):
        from db.jobs import JobDB
        from types import SimpleNamespace
        from unittest.mock import Mock

        job = JobDB.create_job("codegraph_sync", "repo-graph-running")
        JobDB.set_running(job["id"])
        bridge = SimpleNamespace(cancel=Mock(return_value={"cancelled": True}))
        provider = SimpleNamespace(client=bridge)
        service = SimpleNamespace(
            registry=SimpleNamespace(get_provider=lambda _repo_id: provider)
        )
        monkeypatch.setattr("code_intelligence.runtime.build_service", lambda: service)

        resp = client.post("/api/jobs/cancel", json={"job_id": job["id"]})

        assert resp.status_code == 200
        assert resp.get_json()["bridge_cancelled"] is True
        bridge.cancel.assert_called_once_with(job["id"])
        assert JobDB.get_job(job["id"])["status"] == "cancelling"

    def test_cancel_nonexistent(self, client):
        resp = client.post("/api/jobs/cancel", json={"job_id": "fake"})
        assert resp.status_code == 200
        assert resp.get_json()["cancelled"] is False

    def test_delete_finished_job(self, client):
        r = client.post("/api/jobs/submit",
                        json={"job_type": "index", "target": "r1"})
        job_id = r.get_json()["job_id"]
        from db.jobs import JobDB
        JobDB.set_done(job_id, {"ok": True})

        resp = client.delete(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] is True

    def test_delete_running_blocked(self, client):
        r = client.post("/api/jobs/submit",
                        json={"job_type": "index", "target": "r1"})
        job_id = r.get_json()["job_id"]
        from db.jobs import JobDB
        JobDB.set_running(job_id)

        resp = client.delete(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Job worker tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestJobWorker:
    """Worker thread logic tests (unit, not integration)."""

    def test_progress_callback_updates_db(self, _isolated_db):
        from db.jobs import JobDB
        from context.job_worker import _make_progress_callback

        j = JobDB.create_job("index", "r1")
        JobDB.set_running(j["id"])
        cb = _make_progress_callback(j["id"])

        cb(25, "Scanning", "found files")
        job = JobDB.get_job(j["id"])
        assert job["progress"] == 25
        assert job["phase"] == "Scanning"
        assert job["message"] == "found files"

    def test_progress_callback_raises_on_cancel(self, _isolated_db):
        from db.jobs import JobDB
        from context.job_worker import _make_progress_callback, _CancelledError

        j = JobDB.create_job("index", "r1")
        JobDB.set_running(j["id"])
        JobDB.request_cancel(j["id"])

        cb = _make_progress_callback(j["id"])
        with pytest.raises(_CancelledError):
            cb(50, "Embedding")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Backward compatibility — indexing-status merges job data
# ═══════════════════════════════════════════════════════════════════════════════

class TestIndexingStatusBackwardCompat:
    """The /api/context/repos/indexing-status endpoint should include job queue data."""

    def test_indexing_status_includes_running_job(self, client):
        from db.jobs import JobDB
        j = JobDB.create_job("index", "my-repo")
        JobDB.set_running(j["id"])
        JobDB.update_progress(j["id"], 42, "Embedding", "src/main.py")

        resp = client.get("/api/context/repos/indexing-status")
        assert resp.status_code == 200
        data = resp.get_json()
        # The running job should appear under target name
        assert "my-repo" in data
        entry = data["my-repo"]
        assert entry["status"] in ("indexing",)
        assert entry["job_id"] == j["id"]

    def test_indexing_status_includes_queued_job(self, client):
        from db.jobs import JobDB
        j = JobDB.create_job("ast", "my-repo")

        resp = client.get("/api/context/repos/indexing-status")
        data = resp.get_json()
        assert "my-repo" in data
        assert data["my-repo"]["status"] == "queued"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Integration — context routes use job queue
# ═══════════════════════════════════════════════════════════════════════════════

class TestContextRoutesJobIntegration:
    """Verify the existing index/ast/stop routes now submit jobs."""

    def test_index_route_returns_job_id(self, client, _isolated_db):
        """POST /api/context/repos/index should return a job_id."""
        # First add a project to context DB
        from context.db import ContextDB, init_context_schema
        init_context_schema()
        # Create a temp dir to simulate a repo
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ContextDB.add_repo("test-repo", td)
            resp = client.post("/api/context/repos/index",
                               json={"name": "test-repo"})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["started"] is True
            assert "job_id" in data

            # Verify job exists in DB
            from db.jobs import JobDB
            job = JobDB.get_job(data["job_id"])
            assert job is not None
            assert job["job_type"] == "index"
            assert job["target"] == "test-repo"

    def test_ast_route_returns_job_id(self, client, _isolated_db):
        from context.db import ContextDB, init_context_schema
        init_context_schema()
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ContextDB.add_repo("test-repo", td)
            resp = client.post("/api/context/repos/ast/generate",
                               json={"name": "test-repo"})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["started"] is True
            assert "job_id" in data
            assert data["type"] == "ast"

    def test_index_dedup(self, client, _isolated_db):
        """Double-submit returns same job_id."""
        from context.db import ContextDB, init_context_schema
        init_context_schema()
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ContextDB.add_repo("test-repo", td)
            r1 = client.post("/api/context/repos/index",
                             json={"name": "test-repo"})
            r2 = client.post("/api/context/repos/index",
                             json={"name": "test-repo"})
            assert r1.get_json()["job_id"] == r2.get_json()["job_id"]
            assert r2.get_json().get("reused") is True

    def test_stop_cancels_job(self, client, _isolated_db):
        from context.db import ContextDB, init_context_schema
        init_context_schema()
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ContextDB.add_repo("test-repo", td)
            r = client.post("/api/context/repos/index",
                            json={"name": "test-repo"})
            job_id = r.get_json()["job_id"]

            from db.jobs import JobDB
            JobDB.set_running(job_id)

            resp = client.post("/api/context/repos/stop",
                               json={"name": "test-repo"})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["stopping"] is True

            job = JobDB.get_job(job_id)
            assert job["status"] == "cancelling"
