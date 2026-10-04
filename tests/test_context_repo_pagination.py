"""Contract tests for the paginated Context repository list."""

import pytest
from flask import Flask


@pytest.fixture
def context_client():
    from context import routes

    app = Flask(__name__)
    app.register_blueprint(routes.context_bp)
    with app.test_client() as client:
        yield client


@pytest.mark.no_db
def test_repository_list_returns_only_the_requested_name_ordered_page(context_client, monkeypatch):
    from context import routes
    from context.db import ContextDB

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    calls = []

    def list_repos(*, page, page_size, search):
        calls.append((page, page_size, search))
        return [{"name": "bravo", "path": "/repos/bravo"}]

    monkeypatch.setattr(ContextDB, "list_repos", staticmethod(list_repos))
    monkeypatch.setattr(
        "context.ingestion.inspect_project_source",
        lambda _path: {"source": "directory"},
    )

    response = context_client.get(
        "/api/context/repos?page=2&page_size=10",
        headers={"X-App-Name": "savant-olympus"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "repos": [{"name": "bravo", "path": "/repos/bravo", "source": "directory"}],
        "page": 2,
        "page_size": 10,
        "q": "",
    }
    assert calls == [(2, 10, "")]


@pytest.mark.no_db
def test_repository_count_is_a_separate_lightweight_request(context_client, monkeypatch):
    from context import routes
    from context.db import ContextDB

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    calls = []
    monkeypatch.setattr(ContextDB, "count_repos", staticmethod(lambda *, search: calls.append(search) or 23))

    response = context_client.get("/api/context/repos/count?q=olympus", headers={"X-App-Name": "savant-olympus"})

    assert response.status_code == 200
    assert response.get_json() == {"count": 23, "q": "olympus"}
    assert calls == ["olympus"]


@pytest.mark.no_db
def test_repository_list_rejects_pages_larger_than_ten(context_client, monkeypatch):
    from context import routes

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)

    response = context_client.get(
        "/api/context/repos?page=1&page_size=11",
        headers={"X-App-Name": "savant-olympus"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "page_size must be between 1 and 10"
