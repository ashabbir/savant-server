from context.job_worker import _extract_index_metrics


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
