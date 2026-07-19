from context import db as context_db
from context import indexer
from db.jobs import JobDB


def test_indexing_status_marks_naive_stale_timestamp_as_stalled(monkeypatch, client):
    monkeypatch.setattr(
        indexer,
        "get_indexing_status",
        lambda: {
            "repo": {
                "status": "indexing",
                "updated_at": "2000-01-01T00:00:00",
            }
        },
    )
    monkeypatch.setattr(context_db.ContextDB, "list_repos", lambda: [])
    monkeypatch.setattr(JobDB, "list_jobs", lambda **kwargs: [])

    response = client.get("/api/context/repos/indexing-status")

    assert response.status_code == 200
    assert response.get_json()["repo"]["status"] == "stalled"


def test_indexing_status_maps_numeric_graph_job_to_repository_name(monkeypatch, client):
    monkeypatch.setattr(indexer, "get_indexing_status", lambda: {})
    monkeypatch.setattr(
        context_db.ContextDB,
        "list_repos",
        lambda: [{"id": 5, "name": "savant-server", "status": "indexed"}],
    )
    monkeypatch.setattr(
        context_db.ContextDB,
        "get_repo_by_identifier",
        lambda value: {"id": 5, "name": "savant-server"} if str(value) == "5" else None,
    )
    monkeypatch.setattr(
        JobDB,
        "list_jobs",
        lambda status=None, limit=20: [{
            "id": "graph-job", "job_type": "codegraph_sync", "target": "5",
            "status": "running", "progress": 37, "phase": "Building graph",
            "message": "Resolving relationships",
        }],
    )

    payload = client.get("/api/context/repos/indexing-status").get_json()

    assert payload["savant-server"]["status"] == "added"
    assert payload["savant-server"]["structural_job"]["status"] == "running"
    assert payload["savant-server"]["structural_job"]["progress"] == 37
