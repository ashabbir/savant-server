import context.routes as routes
from types import SimpleNamespace


def test_analysis_route_rejects_non_object_json(monkeypatch, client):
    monkeypatch.setattr(routes, "_ensure_init", lambda: True)

    response = client.post(
        "/api/context/analysis",
        data="[1]",
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "request body must be a JSON object"


def test_research_route_rejects_unknown_search_type(monkeypatch, client):
    monkeypatch.setattr(routes, "_ensure_init", lambda: True)

    response = client.post(
        "/api/context/research",
        json={"q": "sample", "type": "invalid"},
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "type must be one of: all, code, memory",
        "allowed_types": ["all", "code", "memory"],
    }


def test_research_route_explores_multiple_repositories(monkeypatch, client, tmp_path):
    monkeypatch.setattr(routes, "_ensure_init", lambda: True)

    from context.db import ContextDB
    from context.embeddings import EmbeddingModel

    repo_paths = {}
    for repo_id in ("repo-one", "repo-two"):
        repo_path = tmp_path / repo_id
        repo_path.mkdir()
        repo_paths[repo_id] = repo_path

    monkeypatch.setattr(EmbeddingModel, "get", lambda: SimpleNamespace(embed_one=lambda _q: [0.0]))
    monkeypatch.setattr(ContextDB, "vector_search", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        ContextDB,
        "get_repo",
        lambda repo_id: {"path": str(repo_paths[repo_id])} if repo_id in repo_paths else None,
    )

    class Service:
        def search_symbols(self, repo_id, root, query, **_kwargs):
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        id=f"symbol:{repo_id}:{index}",
                        kind="function",
                        name=f"run_{repo_id}_{index}",
                        location=SimpleNamespace(start_line=1, end_line=2, file_path="service.py"),
                        qualified_name=f"{repo_id}.run_{index}",
                        signature="()",
                    )
                    for index in range(3)
                ],
                provider="codegraph",
                incomplete=False,
                warnings=[],
            )

        def explore(self, repo_id, root, query, **_kwargs):
            symbol = SimpleNamespace(model_dump=lambda **_kwargs: {"id": f"symbol:{repo_id}"})
            edge = SimpleNamespace(model_dump=lambda **_kwargs: {"source_id": f"symbol:{repo_id}"})
            return SimpleNamespace(
                provider="codegraph",
                incomplete=False,
                warnings=[],
                symbols=[symbol],
                edges=[edge],
            )

    monkeypatch.setattr("code_intelligence.runtime.build_service", lambda: Service())

    response = client.post(
        "/api/context/research",
        json={"q": "run", "repo": ["repo-one", "repo-two"], "type": "code", "limit": 3},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert {item["repo"] for item in payload["structure_search"]["results"]} == {
        "repo-one",
        "repo-two",
    }
    graph_result = payload["code_graph_search"]["run_repo-one_0"]
    assert graph_result["provider"] == "multi_repo"
    assert set(graph_result["repositories"]) == {"repo-one", "repo-two"}
    assert {item["id"] for item in graph_result["symbols"]} == {
        "symbol:repo-one",
        "symbol:repo-two",
    }
