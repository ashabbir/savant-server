import pytest


pytestmark = pytest.mark.no_db


def test_codegraph_sync_refreshes_lossless_source_tree(monkeypatch, tmp_path):
    import context.job_worker as worker
    from code_intelligence.contracts import Freshness, ProviderHealth

    calls = []

    class Result:
        def model_dump(self, **_kwargs):
            return {"provider": "codegraph", "accepted": True}

    class Service:
        def ensure_index(self, repo_id, root, **kwargs):
            calls.append(("graph", repo_id, root, kwargs))
            return Result()

        def health(self, repo_id, root):
            return ProviderHealth(provider="codegraph", indexed=True, freshness=Freshness.FRESH,
                                  graph_version="g1", files=1, nodes=1, edges=0)

    class FakeIndexer:
        def sync_lossless_trees_for_repository(self, root, repo_name, job_progress_cb):
            calls.append(("lossless", root, repo_name))
            job_progress_cb(100, "Lossless source tree", "done")
            return {"files_processed": 1, "errors": 0}

    monkeypatch.setattr(worker, "_resolve_repo", lambda _target: (tmp_path, "canonical-repo"))
    monkeypatch.setattr("code_intelligence.runtime.build_service", lambda: Service())
    monkeypatch.setattr("context.indexer.Indexer", FakeIndexer)
    monkeypatch.setattr("db.code_intelligence.CodeIntelligenceConfigDB.upsert", lambda *args, **kwargs: None)

    result = worker._run_code_intelligence_sync("job-1", "repo-id", lambda *args: None)

    assert calls[0][0] == "graph"
    assert calls[1] == ("lossless", tmp_path, "canonical-repo")
    assert result["lossless_tree_result"] == {"files_processed": 1, "errors": 0}
