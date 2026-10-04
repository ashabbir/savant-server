"""Focused no-database tests for the scheduled differential sync pipeline."""

from pathlib import Path
from types import SimpleNamespace

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
    assert result["index_result"]["files_indexed"] == 0
    assert result["ast_result"]["files_processed"] == 0
    assert result["lst_result"]["files_processed"] == 0
    assert result["graph_result"]["accepted"] is False
    assert calls[-1][0] == 100


@pytest.mark.no_db
def test_changed_hash_runs_only_changed_files_and_records_full_summary(monkeypatch, tmp_path):
    from context import job_worker
    from context.ingestion import IngestedProject
    from context.db import ContextDB
    from db.code_intelligence import CodeIntelligenceConfigDB

    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    invocations = []

    class FakeIndexer:
        def index_repository(self, _path, **kwargs):
            invocations.append(("index", kwargs))
            return {"files_indexed": 2, "files_removed": 1, "chunks_indexed": 5}

        def generate_ast_for_repository(self, _path, **kwargs):
            invocations.append(("ast", kwargs))
            return {"files_processed": 2, "files_removed": 1}

        def sync_lossless_trees_for_repository(self, _path, **kwargs):
            invocations.append(("lst", kwargs))
            return {"files_processed": 2, "files_removed": 1}

    class FakeGraphResult:
        accepted = True

        def model_dump(self, **_kwargs):
            return {"accepted": True, "result": {"files_updated": 2}}

    health = SimpleNamespace(provider="codegraph", graph_version="1", indexed_at="now",
                             freshness=SimpleNamespace(value="fresh"))
    service = SimpleNamespace(
        ensure_index=lambda *args, **kwargs: FakeGraphResult(),
        health=lambda *args, **kwargs: health,
    )

    monkeypatch.setattr(job_worker, "_resolve_repo", lambda _target: (repo_path, "repo"))
    monkeypatch.setattr(ContextDB, "get_repo", staticmethod(lambda _name: {"id": 7, "status": "indexed"}))
    monkeypatch.setattr(ContextDB, "add_repo", staticmethod(lambda *_args: None))
    monkeypatch.setattr(ContextDB, "mark_repo_fetched", staticmethod(lambda *_args: None))
    monkeypatch.setattr(
        "context.ingestion.refresh_repo",
        lambda _path: IngestedProject(name="repo", path=str(repo_path), changed=True,
                                      before_commit="abc123", after_commit="def456"),
    )
    monkeypatch.setattr("context.indexer.Indexer", FakeIndexer)
    monkeypatch.setattr(
        "context.indexer.get_git_diff_files",
        lambda *_args: (["new.py"], ["changed.py"], ["deleted.py"]),
    )
    monkeypatch.setattr("code_intelligence.runtime.build_service", lambda: service)
    monkeypatch.setattr(CodeIntelligenceConfigDB, "upsert", staticmethod(lambda *_args, **_kwargs: None))

    result = job_worker._run_differential_sync("job-1", "repo", lambda *_args: None)

    assert [name for name, _kwargs in invocations] == ["index", "ast", "lst"]
    for _name, kwargs in invocations:
        assert kwargs["differential"] is True
        assert kwargs["changed_files"] == {
            "added": ["new.py"], "modified": ["changed.py"], "deleted": ["deleted.py"],
        }
    assert result["graph_result"]["accepted"] is True
    assert "Indexed: 2 files" in result["summary"]
    assert "AST: 2 files updated" in result["summary"]
    assert "LST: 2 files updated" in result["summary"]
    assert "Code Graph: accepted" in result["summary"]


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
