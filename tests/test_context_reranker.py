"""Tests for dedicated Cross-Encoder reranker integration."""

import os
import pytest
from unittest.mock import MagicMock, patch

from context.reranker import RerankerModel, resolve_model_dir


@pytest.mark.no_db
def test_resolve_model_dir_finds_local_model():
    path = resolve_model_dir()
    assert path is not None
    assert (path / "config.json").exists()


@pytest.mark.no_db
def test_reranker_disabled_by_env(monkeypatch):
    monkeypatch.setenv("SAVANT_ENABLE_RERANKER", "0")
    RerankerModel.reset()
    try:
        model = RerankerModel.get()
        assert model is None
    finally:
        RerankerModel.reset()


@pytest.mark.no_db
def test_reranker_scoring_and_ordering():
    RerankerModel.reset()
    model = RerankerModel.get()
    assert model is not None

    query = "postgresql connection pool configuration"
    candidates = [
        {"id": 1, "content": "How to make a chocolate cake in 10 minutes"},
        {"id": 2, "content": "def get_connection(): return db_pool.getconn()"},
    ]

    reranked = model.rerank(query, candidates, top_k=2)
    assert len(reranked) == 2
    # The database snippet must be ranked first
    assert reranked[0]["id"] == 2
    assert "rerank_score" in reranked[0]
    assert reranked[0]["rerank_score"] > reranked[1]["rerank_score"]


def test_exec_code_search_uses_reranker(monkeypatch):
    from context.routes import _exec_code_search
    from context.db import ContextDB

    dummy_results = [
        {"id": 10, "content": "unrelated snippet", "rel_path": "other.py"},
        {"id": 20, "content": "auth middleware token validation", "rel_path": "auth.py"},
    ]
    monkeypatch.setattr(ContextDB, "vector_search", lambda *args, **kwargs: list(dummy_results))

    class _MockEmbedder:
        def embed_one(self, text):
            return [0.1] * 768

    monkeypatch.setattr("context.embeddings.EmbeddingModel.get", lambda: _MockEmbedder())

    res = _exec_code_search("jwt auth token validation", repo=None, limit=2, should_exclude_tests=False)
    assert res["result_count"] == 2
    # Reranker should prioritize the auth snippet
    assert res["results"][0]["id"] == 20
    assert "rerank_score" in res["results"][0]


@pytest.mark.no_db
def test_exec_memory_search_uses_reranker_after_vector_recall(monkeypatch):
    from context.routes import _exec_memory_search
    from context.db import ContextDB

    candidates = [
        {"id": 1, "content": "unrelated deployment notes", "rel_path": "memory/deploy.md"},
        {"id": 2, "content": "JWT token refresh and authentication requirements", "rel_path": "memory/auth.md"},
    ]
    vector_limits = []
    monkeypatch.setattr(ContextDB, "vector_search", lambda *_args, **kwargs: vector_limits.append(kwargs["limit"]) or list(candidates))

    class _MockEmbedder:
        def embed_one(self, _text):
            return [0.1] * 768

    class _Reranker:
        def rerank(self, _query, entries, **_kwargs):
            return [entries[1]]

    monkeypatch.setattr("context.embeddings.EmbeddingModel.get", lambda: _MockEmbedder())
    monkeypatch.setattr("context.reranker.RerankerModel.get", lambda: _Reranker())

    result = _exec_memory_search("jwt authentication", repo=None, limit=1)

    assert vector_limits == [20]
    assert result["result_count"] == 1
    assert result["results"][0]["id"] == 2


def test_search_api_with_rerank_toggle(client, monkeypatch):
    from context.db import ContextDB

    dummy_results = [
        {"id": 1, "content": "general logging setup", "rel_path": "log.py"},
        {"id": 2, "content": "psycopg2 execute_values batch insert", "rel_path": "db.py"},
    ]
    monkeypatch.setattr(ContextDB, "vector_search", lambda *args, **kwargs: list(dummy_results))

    class _MockEmbedder:
        def embed_one(self, text):
            return [0.1] * 768

    monkeypatch.setattr("context.embeddings.EmbeddingModel.get", lambda: _MockEmbedder())

    resp = client.get("/api/context/search?q=batch+insert+postgres&limit=2&rerank=1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["results"]) == 2
    assert data["results"][0]["id"] == 2
    assert "rerank_score" in data["results"][0]
