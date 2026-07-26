from context.db import ContextDB
from context.job_worker import _extract_index_metrics, _record_job_activity


def test_extract_index_metrics_from_direct_result():
    metrics = _extract_index_metrics({
        "files_indexed": 12,
        "files_skipped": 4,
        "files_removed": 2,
        "chunks_indexed": 30,
        "errors": 1,
    })

    assert metrics["files_indexed"] == 12
    assert metrics["files_removed_from_index"] == 2
    assert metrics["index_errors"] == 1


def test_extract_index_metrics_aggregates_batch_and_differential_results():
    metrics = _extract_index_metrics({
        "results": [
            {"result": {"files_indexed": 3, "files_skipped": 2, "files_removed": 1}},
            {"result": {"files_indexed": 7, "files_skipped": 1, "files_removed": 4}},
        ]
    })

    assert metrics["files_indexed"] == 10
    assert metrics["files_skipped"] == 3
    assert metrics["files_removed_from_index"] == 5


def test_differential_activity_keeps_graph_summary_for_top_cards(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ContextDB, "get_repo_by_identifier",
        staticmethod(lambda _target: {"name": "repo"}),
    )
    monkeypatch.setattr(
        ContextDB, "record_repo_sync_log",
        staticmethod(lambda **fields: captured.update(fields)),
    )

    _record_job_activity("differential_sync", "repo", "success", {
        "files_changed": {
            "added": ["new.ts"], "modified": ["app.ts"], "deleted": ["old.ts"],
        },
        "index_result": {"files_indexed": 2, "files_removed": 1, "errors": 0},
        "graph_result": {
            "accepted": True,
            "result": {"nodesUpdated": 22, "durationMs": 1745},
        },
    }, 0)

    stats = captured["change_stats"]
    assert stats["codegraph_accepted"] is True
    assert stats["codegraph_result"]["nodesUpdated"] == 22
    assert stats["codegraph_changed_files"] == ["new.ts", "app.ts", "old.ts"]
