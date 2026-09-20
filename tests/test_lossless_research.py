import pytest
from flask import Flask


pytestmark = pytest.mark.no_db


def test_code_research_combines_lossless_ast_and_codegraph_results(monkeypatch):
    import context.routes as routes

    app = Flask(__name__)
    app.register_blueprint(routes.context_bp)
    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setattr(routes, "_parse_repo_ids", lambda _repo: ["repo-a"])
    monkeypatch.setattr(routes, "_exec_code_search", lambda *_args: {"results": []})
    monkeypatch.setattr(routes, "_exec_structure_search", lambda *_args: {
        "results": [{"name": "Service", "rel_path": "service.py"}],
    })
    monkeypatch.setattr(routes, "_exec_lossless_search", lambda *_args: {
        "results": [{"rel_path": "service.py", "source": "class Service:", "nodes": []}],
    })
    monkeypatch.setattr(routes, "_exec_graph_search", lambda *_args: {
        "symbols": [], "edges": [], "warnings": [], "incomplete": False,
    })

    response = app.test_client().post(
        "/api/context/research",
        json={"q": "Service", "repo": "repo-a", "type": "code"},
        headers={"X-App-Name": "savant-olympus"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["structure_search"]["results"][0]["name"] == "Service"
    assert payload["lossless_tree_search"]["results"][0]["source"] == "class Service:"
    assert "code_graph_search" in payload
    assert "exact source-tree matches" in payload["overview"]["summary"]
