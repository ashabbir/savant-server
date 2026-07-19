import uuid
import subprocess

from context.db import ContextDB
from context.indexer import Indexer


class _Embedder:
    def embed_one(self, text):
        return [0.0] * 768


def test_incremental_reindex_replaces_generated_rows_instead_of_duplicating(monkeypatch, tmp_path):
    repo_name = f"index-refactor-{uuid.uuid4().hex}"
    (tmp_path / "sample.py").write_text(
        "def calculate(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "sample.py"], check=True)
    monkeypatch.setattr(Indexer, "_get_embedder", lambda self: _Embedder())
    indexer = Indexer()

    try:
        indexer.index_repository(tmp_path, repo_name=repo_name, clear=True)
        first = next(row for row in ContextDB.get_repo_stats() if row["name"] == repo_name)
        assert first["chunk_count"] > 0
        assert first["ast_node_count"] > 0

        indexer.index_repository(tmp_path, repo_name=repo_name, clear=False)
        second = next(row for row in ContextDB.get_repo_stats() if row["name"] == repo_name)

        assert second["file_count"] == first["file_count"]
        assert second["chunk_count"] == first["chunk_count"]
        assert second["ast_node_count"] == first["ast_node_count"]
    finally:
        ContextDB.delete_repo(repo_name)


def test_incremental_ast_generation_replaces_nodes_instead_of_duplicating(tmp_path):
    repo_name = f"ast-refactor-{uuid.uuid4().hex}"
    (tmp_path / "sample.py").write_text(
        "class Calculator:\n    def calculate(self, value):\n        return value + 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "sample.py"], check=True)
    indexer = Indexer()

    try:
        indexer.generate_ast_for_repository(tmp_path, repo_name=repo_name, clear=True)
        first = next(row for row in ContextDB.get_repo_stats() if row["name"] == repo_name)
        assert first["ast_node_count"] > 0

        indexer.generate_ast_for_repository(tmp_path, repo_name=repo_name, clear=False)
        second = next(row for row in ContextDB.get_repo_stats() if row["name"] == repo_name)

        assert second["ast_node_count"] == first["ast_node_count"]
    finally:
        ContextDB.delete_repo(repo_name)
