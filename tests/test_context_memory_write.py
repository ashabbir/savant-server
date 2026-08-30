import pytest
from flask import Flask

pytestmark = pytest.mark.no_db


@pytest.fixture
def client(monkeypatch):
    import context.routes as routes
    from context.db import ContextDB

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setattr(
        ContextDB, "get_repo",
        lambda name, conn=None: {"id": 7, "name": "savant-server"} if name == "savant-server" else None,
    )
    app = Flask(__name__)
    app.register_blueprint(routes.context_bp)
    return app.test_client()


@pytest.fixture
def recorder(monkeypatch):
    from context.db import ContextDB
    from context.embeddings import EmbeddingModel

    written = {"files": [], "chunks": []}
    monkeypatch.setattr(
        ContextDB, "insert_file",
        lambda *args, **kwargs: written["files"].append(args) or 42,
    )
    monkeypatch.setattr(ContextDB, "clear_file_generated_data", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ContextDB, "insert_chunk",
        lambda file_id, index, content, embedding, conn=None: written["chunks"].append((file_id, index, content)) or index,
    )
    monkeypatch.setattr(EmbeddingModel, "get", classmethod(lambda cls: type("E", (), {"embed_one": lambda self, text: [0.0]})()))
    return written


HEADERS = {"X-App-Name": "savant-notebook"}


def test_memory_write_indexes_a_code_wiki_as_a_memory_bank_resource(client, recorder):
    response = client.post(
        "/api/context/memory/write",
        json={"repo": "savant-server", "path": "memory-bank/olympus-code-wiki.md", "content": "# Code Wiki\n\nBody."},
        headers=HEADERS,
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["uri"] == "savant-server:memory-bank/olympus-code-wiki.md"
    assert body["chunk_count"] >= 1
    repo_id, rel_path, language, is_memory_bank = recorder["files"][0][:4]
    assert (repo_id, rel_path, language, is_memory_bank) == (7, "memory-bank/olympus-code-wiki.md", "markdown", True)
    assert recorder["chunks"]


def test_memory_write_rejects_unknown_repo(client, recorder):
    response = client.post(
        "/api/context/memory/write",
        json={"repo": "ghost", "path": "memory-bank/wiki.md", "content": "x"},
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert recorder["files"] == []


@pytest.mark.parametrize("payload", [
    {"path": "memory-bank/wiki.md", "content": "x"},
    {"repo": "savant-server", "content": "x"},
    {"repo": "savant-server", "path": "memory-bank/wiki.md", "content": "   "},
    {"repo": "savant-server", "path": "../escape.md", "content": "x"},
])
def test_memory_write_rejects_invalid_payloads(client, recorder, payload):
    response = client.post("/api/context/memory/write", json=payload, headers=HEADERS)
    assert response.status_code == 400
    assert recorder["files"] == []
