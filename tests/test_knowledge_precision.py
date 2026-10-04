"""Knowledge graph recall-then-precision retrieval tests."""

import pytest


pytestmark = pytest.mark.no_db


def test_knowledge_precision_reranks_title_and_content(monkeypatch):
    from knowledge.routes import _precision_rerank

    candidates = [
        {"node_id": "1", "title": "Deployment", "content": "general operations notes"},
        {"node_id": "2", "title": "Authentication", "content": "JWT refresh-token architecture"},
    ]
    observed = {}

    class _Reranker:
        def rerank(self, query, entries, *, text_key, top_k):
            observed.update(query=query, text_key=text_key, top_k=top_k, text=entries[1][text_key])
            return [entries[1]]

    monkeypatch.setattr("context.reranker.RerankerModel.get", lambda: _Reranker())

    results = _precision_rerank("jwt authentication", candidates, 1)

    assert results == [candidates[1]]
    assert observed == {
        "query": "jwt authentication", "text_key": "_precision_text", "top_k": 1,
        "text": "Authentication\nJWT refresh-token architecture",
    }


def test_knowledge_search_recalls_extra_candidates_before_precision(monkeypatch):
    from flask import Flask
    from knowledge.routes import knowledge_bp
    from db.knowledge_graph import KnowledgeGraphDB

    captured = {}
    candidates = [{"node_id": "1", "title": "Result", "content": "matched"}]
    monkeypatch.setattr(
        KnowledgeGraphDB, "search_nodes", staticmethod(
            lambda query, **kwargs: captured.update(query=query, **kwargs) or candidates
        ),
    )
    monkeypatch.setattr("knowledge.routes._precision_rerank", lambda query, rows, limit: rows[:limit])

    app = Flask(__name__)
    app.register_blueprint(knowledge_bp)
    response = app.test_client().post(
        "/api/knowledge/search", json={"query": "auth", "limit": 4},
        headers={"X-App-Name": "savant-olympus"},
    )

    assert response.status_code == 200
    assert response.get_json()["result"] == candidates
    assert captured == {"query": "auth", "node_type": "", "limit": 20, "include_staged": False}
