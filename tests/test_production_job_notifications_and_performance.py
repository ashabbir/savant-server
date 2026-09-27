"""Tests for production job notifications, error reporting, and performance improvements."""

import json
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from db.notifications import NotificationDB
from db.jobs import JobDB
from context.job_worker import _process_next_job
from context.periodic_runner import _execute_sync_pass_for_all_repos
from context.db import ContextDB
from context.indexer import Indexer


class TestNotificationDBEnhancements:
    """Test NotificationDB helpers and auto-id handling."""

    def test_create_auto_generates_id_and_handles_level(self, _isolated_db):
        notif = NotificationDB.create({
            "message": "Test failure occurred",
            "level": "error",
            "user_id": "ahmed",
        })
        assert notif is not None
        assert notif["notification_id"]
        assert notif["message"] == "Test failure occurred"
        assert notif["user_id"] == "ahmed"
        assert notif["detail"].get("level") == "error"

    def test_notify_job_failure_creates_notification(self, _isolated_db):
        notif = NotificationDB.notify_job_failure(
            job_id="job-123",
            job_type="initial_repo_sync",
            target="my-org/my-repo",
            error="Connection to git remote timed out",
            user_id="ahmed",
        )
        assert notif is not None
        assert "Repository download & index" in notif["message"]
        assert "Connection to git remote timed out" in notif["message"]
        assert notif["event_type"] == "job_failed"
        assert notif["detail"]["job_id"] == "job-123"
        assert notif["detail"]["job_type"] == "initial_repo_sync"
        assert notif["detail"]["target"] == "my-org/my-repo"

    def test_notify_job_success_creates_notification(self, _isolated_db):
        notif = NotificationDB.notify_job_success(
            job_id="job-456",
            job_type="reindex",
            target="my-repo",
            result={"files_indexed": 42},
            user_id="ahmed",
        )
        assert notif is not None
        assert notif["event_type"] == "job_completed"
        assert "Repository re-indexing completed" in notif["message"]
        assert notif["detail"]["job_id"] == "job-456"

    def test_notify_sync_failure_creates_notification(self, _isolated_db):
        notif = NotificationDB.notify_sync_failure(
            repo_name="core-service",
            error="Git authentication failed",
            errors=["fetch: unauthorized"],
            user_id="ahmed",
        )
        assert notif is not None
        assert notif["event_type"] == "sync_failed"
        assert "Sync failed for repository 'core-service'" in notif["message"]
        assert notif["detail"]["repo_name"] == "core-service"

    def test_list_recent_includes_broadcast_notifications_for_user(self, _isolated_db):
        # Create broadcast notification (user_id="")
        NotificationDB.notify(
            message="System wide alert",
            event_type="system",
            user_id="",
        )
        # Create user specific notification
        NotificationDB.notify(
            message="Ahmed specific alert",
            event_type="user",
            user_id="ahmed",
        )

        recent_for_ahmed = NotificationDB.list_recent(user_id="ahmed")
        messages = [n["message"] for n in recent_for_ahmed]
        assert "System wide alert" in messages
        assert "Ahmed specific alert" in messages

        unread_count = NotificationDB.count_unread(user_id="ahmed")
        assert unread_count >= 2


class TestJobWorkerNotificationIntegration:
    """Test that job worker notifies user on execution failure and success."""

    def test_job_worker_emits_notification_on_job_failure(self, _isolated_db, monkeypatch):
        job = JobDB.create_job("index", "broken-repo", payload={"user_id": "ahmed", "actor_id": "ahmed"})
        job_id = job["id"]

        def mock_execute(*args, **kwargs):
            raise RuntimeError("Out of memory while embedding chunks")

        monkeypatch.setattr("context.job_worker._execute_job", mock_execute)

        _process_next_job()

        updated_job = JobDB.get_job(job_id)
        assert updated_job["status"] == "failed"

        notifs = NotificationDB.list_recent(user_id="ahmed")
        failure_notifs = [n for n in notifs if n["event_type"] == "job_failed"]
        assert len(failure_notifs) == 1
        assert failure_notifs[0]["detail"]["job_id"] == job_id
        assert "Out of memory" in failure_notifs[0]["message"]

    def test_job_worker_emits_notification_on_job_success(self, _isolated_db, monkeypatch):
        job = JobDB.create_job("index", "valid-repo", payload={"user_id": "ahmed", "actor_id": "ahmed"})
        job_id = job["id"]

        monkeypatch.setattr("context.job_worker._execute_job", lambda *args, **kwargs: {"files_indexed": 10})

        _process_next_job()

        updated_job = JobDB.get_job(job_id)
        assert updated_job["status"] == "done"

        notifs = NotificationDB.list_recent(user_id="ahmed")
        success_notifs = [n for n in notifs if n["event_type"] == "job_completed"]
        assert len(success_notifs) == 1
        assert success_notifs[0]["detail"]["job_id"] == job_id


class TestPeriodicRunnerNotificationIntegration:
    """Test that periodic runner notifies user on sync error."""

    def test_periodic_runner_notifies_on_sync_failure(self, _isolated_db, tmp_path, monkeypatch):
        repo_dir = tmp_path / "sync-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        ContextDB.add_repo("sync-repo", str(repo_dir))

        def failing_refresh(repo_path):
            from context.ingestion import IngestionError
            raise IngestionError("Remote origin unreachable")

        monkeypatch.setattr("context.ingestion.refresh_repo", failing_refresh)
        monkeypatch.setattr("context.indexer.Indexer.index_repository", lambda *args, **kwargs: {})

        summary = _execute_sync_pass_for_all_repos(trigger="scheduled", actor_id="ahmed")

        notifs = NotificationDB.list_recent(user_id="ahmed")
        sync_notifs = [n for n in notifs if n["event_type"] == "sync_failed"]
        assert len(sync_notifs) >= 1
        assert "sync-repo" in sync_notifs[0]["message"]


class TestContextDBBatchingAndPerformance:
    """Test batch insertion and embedding acceleration in ContextDB."""

    def test_insert_chunks_batch(self, _isolated_db, tmp_path):
        repo = ContextDB.add_repo("batch-test-repo", str(tmp_path))
        file_id = ContextDB.insert_file(
            repo["id"], "main.py", "python", False, 12345, "2026-08-31T00:00:00Z"
        )

        dummy_embedding = [0.1] * 768
        chunks = [
            (0, "def foo(): pass", dummy_embedding),
            (1, "def bar(): pass", dummy_embedding),
            (2, "def baz(): pass", dummy_embedding),
        ]

        ids = ContextDB.insert_chunks_batch(file_id, chunks)
        assert len(ids) == 3

        search_res = ContextDB.vector_search(dummy_embedding, limit=5, repo_filter="batch-test-repo")
        assert len(search_res) == 3

    def test_insert_ast_nodes_batch(self, _isolated_db, tmp_path):
        repo = ContextDB.add_repo("ast-batch-repo", str(tmp_path))
        file_id = ContextDB.insert_file(
            repo["id"], "app.py", "python", False, 12345, "2026-08-31T00:00:00Z"
        )

        nodes = [
            (file_id, "function", "foo", 1, 5),
            (file_id, "function", "bar", 6, 10),
            (file_id, "class", "MyClass", 11, 25),
        ]

        count = ContextDB.insert_ast_nodes_batch(nodes)
        assert count == 3

        ast_results = ContextDB.search_ast_nodes("foo", repo_filter="ast-batch-repo")
        assert len(ast_results) == 1
        assert ast_results[0]["name"] == "foo"
