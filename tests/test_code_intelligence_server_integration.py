"""Focused server migration tests that do not require PostgreSQL."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.no_db


def test_job_claim_is_atomic_and_uses_skip_locked(monkeypatch):
    from db.jobs import JobDB
    import db.jobs as jobs

    class Cursor:
        def __init__(self):
            self.sql = ""
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def execute(self, sql, params):
            self.sql = sql
            assert len(params) == 1
        def fetchone(self):
            return {"id": "job-1", "status": "running", "result": "{}"}

    class Conn:
        def __init__(self): self.cur, self.committed = Cursor(), False
        def cursor(self): return self.cur
        def commit(self): self.committed = True

    conn = Conn()
    monkeypatch.setattr(jobs, "get_connection", lambda: conn)
    monkeypatch.setattr(jobs, "release_connection", lambda _conn: None)

    claimed = JobDB.next_queued()

    assert claimed["id"] == "job-1"
    assert "FOR UPDATE SKIP LOCKED" in conn.cur.sql
    assert "UPDATE jobs" in conn.cur.sql
    assert "RETURNING j.*" in conn.cur.sql
    assert conn.committed is True


def test_service_bounds_subgraph_depth_and_size(tmp_path):
    from code_intelligence.contracts import Subgraph
    from code_intelligence.service import CodeIntelligenceService

    class Provider:
        name = "codegraph"
        def get_callers(self, repo, ref, depth=1, limit=20):
            assert repo["root"] == tmp_path
            assert ref == {"id": "symbol-1"}
            assert depth == 3
            assert limit == 500
            return Subgraph()

    class Registry:
        def get_provider(self, _repo_id): return Provider()

    service = CodeIntelligenceService(Registry(), authorize_repo=lambda *_args: None)
    result = service.subgraph("repo-1", tmp_path, roots=[{"id": "symbol-1"}], mode="callers", depth=99, limit=9999)
    assert result.symbols == []


def test_source_route_rejects_parent_traversal(monkeypatch, tmp_path):
    from code_intelligence import routes
    from flask import Flask

    monkeypatch.setattr(routes.ContextDB, "get_repo_by_identifier", lambda _repo_id: {"path": str(tmp_path)})
    app = Flask(__name__)
    app.register_blueprint(routes.code_intelligence_bp)

    response = app.test_client().get(
        "/api/context/code-intelligence/repos/repo-1/source?path=../secret.txt"
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "path_refused"
