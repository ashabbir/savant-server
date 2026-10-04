"""Focused no-database tests for the scheduled differential sync pipeline."""

from pathlib import Path

import pytest
from flask import Flask, g


@pytest.mark.no_db
def test_unchanged_hash_skips_all_processing(monkeypatch, tmp_path):
    from context import job_worker
    from context.ingestion import IngestedProject
    from context.db import ContextDB

    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(job_worker, "_resolve_repo", lambda _target: (repo_path, "repo"))
    monkeypatch.setattr(ContextDB, "get_repo", staticmethod(lambda _name: {"id": 7, "status": "indexed"}))
    monkeypatch.setattr(
        "context.ingestion.refresh_repo",
        lambda _path: IngestedProject(name="repo", path=str(repo_path), changed=False,
                                      before_commit="abc123", after_commit="abc123"),
    )
    monkeypatch.setattr("context.ingestion._get_git_head", lambda _path: "abc123")
    result = job_worker._run_differential_sync("job-1", "repo", lambda *args: calls.append(args))

    assert result["status"] == "skipped"
    assert result["hash_changed"] is False
    assert result["queued_jobs"] == []
    assert calls[-1][0] == 100


@pytest.mark.no_db
def test_changed_hash_enqueues_scoped_follow_up_jobs(monkeypatch, tmp_path):
    from context import job_worker
    from context.ingestion import IngestedProject
    from context.db import ContextDB
    from db.jobs import JobDB

    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    enqueued = []

    monkeypatch.setattr(job_worker, "_resolve_repo", lambda _target: (repo_path, "repo"))
    monkeypatch.setattr(ContextDB, "get_repo", staticmethod(lambda _name: {"id": 7, "status": "indexed"}))
    monkeypatch.setattr(ContextDB, "add_repo", staticmethod(lambda *_args: None))
    monkeypatch.setattr(ContextDB, "mark_repo_fetched", staticmethod(lambda *_args: None))
    monkeypatch.setattr(
        "context.ingestion.refresh_repo",
        lambda _path: IngestedProject(name="repo", path=str(repo_path), changed=True,
                                      before_commit="abc123", after_commit="def456"),
    )
    monkeypatch.setattr(
        "context.indexer.get_git_diff_files",
        lambda *_args: (["new.py"], ["changed.py"], ["deleted.py"]),
    )
    monkeypatch.setattr(JobDB, "create_job", staticmethod(
        lambda job_type, target, payload: enqueued.append((job_type, target, payload)) or {
            "id": f"job-{len(enqueued)}", "job_type": job_type, "target": target,
        }
    ))

    result = job_worker._run_differential_sync("job-1", "repo", lambda *_args: None)

    assert [job_type for job_type, _target, _payload in enqueued] == ["index", "ast", "lst", "codegraph_sync"]
    assert enqueued[-1][1] == "repo"
    for _job_type, _target, payload in enqueued:
        assert payload["parent_job_id"] == "job-1"
        assert payload["provider_repo_id"] == "7"
        assert payload["differential"] is True
        assert payload["files_changed"] == {
            "added": ["new.py"], "modified": ["changed.py"], "deleted": ["deleted.py"],
        }
    assert result["changed"] is True
    assert [job["job_type"] for job in result["queued_jobs"]] == ["index", "ast", "lst", "codegraph_sync"]


@pytest.mark.no_db
def test_differential_children_use_only_the_parent_diff(monkeypatch, tmp_path):
    from context import job_worker

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    calls = []
    files_changed = {"added": ["new.py"], "modified": ["changed.py"], "deleted": ["deleted.py"]}

    class FakeIndexer:
        def index_repository(self, _path, **kwargs):
            calls.append(("index", kwargs))
            return {}

        def generate_ast_for_repository(self, _path, **kwargs):
            calls.append(("ast", kwargs))
            return {}

        def sync_lossless_trees_for_repository(self, _path, **kwargs):
            calls.append(("lst", kwargs))
            return {}

    monkeypatch.setattr(job_worker, "_resolve_repo", lambda _target: (repo_path, "repo"))
    monkeypatch.setattr("context.indexer.Indexer", FakeIndexer)
    payload = {"differential": True, "files_changed": files_changed, "before_commit": "before", "after_commit": "after"}

    job_worker._run_index("repo", lambda *_args: None, payload=payload)
    job_worker._run_ast("repo", lambda *_args: None, payload=payload)
    job_worker._run_lst("repo", lambda *_args: None, payload=payload)

    assert [name for name, _kwargs in calls] == ["index", "ast", "lst"]
    for _name, kwargs in calls:
        assert kwargs["differential"] is True
        assert kwargs["clear"] is False
        assert kwargs["changed_files"] == files_changed


@pytest.mark.no_db
def test_differential_codegraph_child_receives_the_parent_diff(monkeypatch, tmp_path):
    from context import job_worker
    from db.code_intelligence import CodeIntelligenceConfigDB

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    files_changed = {"added": ["new.py"], "modified": ["changed.py"], "deleted": ["deleted.py"]}
    calls = {}

    class FakeIndexer:
        def sync_lossless_trees_for_repository(self, _path, **kwargs):
            calls["lst"] = kwargs
            return {"files_processed": 2}

    class FakeHealth:
        provider = "codegraph"
        graph_version = "1"
        indexed_at = "now"

        class freshness:
            value = "fresh"

    class FakeService:
        def ensure_index(self, *_args, **kwargs):
            calls["codegraph"] = kwargs
            return {"accepted": True}

        def health(self, *_args, **_kwargs):
            return FakeHealth()

    monkeypatch.setattr(job_worker, "_resolve_repo", lambda _target: (repo_path, "repo"))
    monkeypatch.setattr("context.indexer.Indexer", FakeIndexer)
    monkeypatch.setattr("code_intelligence.runtime.build_service", lambda: FakeService())
    monkeypatch.setattr(CodeIntelligenceConfigDB, "upsert", staticmethod(lambda *_args, **_kwargs: None))

    result = job_worker._run_code_intelligence_sync(
        "job-2", "repo", lambda *_args: None,
        payload={"differential": True, "files_changed": files_changed},
    )

    assert calls["codegraph"]["changed_files"] == files_changed
    assert calls["lst"]["differential"] is True
    assert calls["lst"]["clear"] is False
    assert calls["lst"]["changed_files"] == files_changed
    assert result["files_changed"] == files_changed


@pytest.mark.no_db
def test_scheduler_enqueues_one_differential_job_and_records_enqueue_summary(monkeypatch, tmp_path):
    from context import periodic_runner
    from context.db import ContextDB
    from db.jobs import JobDB

    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    enqueued, logs = [], []
    monkeypatch.setattr(ContextDB, "list_repos", staticmethod(lambda: [{
        "name": "repo", "path": str(repo_path), "status": "indexed",
    }]))
    monkeypatch.setattr(ContextDB, "record_repo_sync_log", staticmethod(lambda **fields: logs.append(fields)))
    monkeypatch.setattr(JobDB, "find_active_types", staticmethod(lambda *_args: None))
    monkeypatch.setattr(JobDB, "create_job", staticmethod(
        lambda job_type, target, payload: enqueued.append((job_type, target, payload)) or {"id": "job-1"}
    ))

    result = periodic_runner._execute_sync_pass_for_all_repos()

    assert enqueued == [("differential_sync", "repo", {
        "trigger": "scheduled", "actor_id": "system", "source_app": "savant-server",
    })]
    assert result["results"][0]["status"] == "success"
    assert "Enqueued differential sync pipeline: job-1" in logs[0]["details"]


@pytest.mark.no_db
def test_refresh_route_queues_the_same_single_pipeline(monkeypatch, tmp_path):
    from context import routes
    from context.db import ContextDB
    from db.jobs import JobDB

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    queued = []
    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setattr(ContextDB, "get_repo", staticmethod(lambda _name: {
        "name": "repo", "path": str(repo_path),
    }))
    monkeypatch.setattr(routes, "_validate_repo_path", lambda _repo: (repo_path, None))
    monkeypatch.setattr(JobDB, "find_active", staticmethod(lambda *_args: None))
    monkeypatch.setattr(JobDB, "create_job", staticmethod(
        lambda job_type, target, payload: queued.append((job_type, target, payload)) or {"id": "job-1"}
    ))

    app = Flask(__name__)
    with app.test_request_context("/api/context/repos/repo/refresh", method="POST"):
        g.user_id = "tester"
        response, status = routes.refresh_repo.__wrapped__("repo")

    assert status == 202
    assert response.get_json()["differential_sync_job_id"] == "job-1"
    assert queued == [("differential_sync", "repo", {
        "user_id": "tester", "actor_id": "tester", "trigger": "manual", "source_app": "savant-olympus",
    })]


@pytest.mark.no_db
def test_job_activity_keeps_scheduled_provenance_and_all_pipeline_summaries(monkeypatch):
    from context.db import ContextDB
    from context.job_worker import _record_job_activity

    captured = {}
    monkeypatch.setattr(ContextDB, "get_repo_by_identifier", staticmethod(lambda _target: {"name": "repo"}))
    monkeypatch.setattr(ContextDB, "record_repo_sync_log", staticmethod(lambda **fields: captured.update(fields)))

    _record_job_activity("differential_sync", "repo", "success", {
        "hash_changed": True,
        "files_changed": {"added": ["new.py"], "modified": ["changed.py"], "deleted": ["old.py"]},
        "index_result": {"files_indexed": 2, "files_removed": 1, "chunks_indexed": 5},
        "ast_result": {"files_processed": 2, "files_removed": 1},
        "lst_result": {"files_processed": 2, "files_removed": 1},
        "graph_result": {"accepted": True, "result": {"files_updated": 2}},
        "summary": "Full differential summary",
    }, 0, payload={"trigger": "scheduled", "actor_id": "system", "source_app": "savant-server"})

    assert captured["trigger"] == "scheduled"
    assert captured["actor_id"] == "system"
    assert captured["source_app"] == "savant-server"
    assert captured["change_stats"]["index_summary"]["files_indexed"] == 2
    assert captured["change_stats"]["ast_summary"]["files_processed"] == 2
    assert captured["change_stats"]["lst_summary"]["files_processed"] == 2
    assert captured["change_stats"]["codegraph_accepted"] is True
