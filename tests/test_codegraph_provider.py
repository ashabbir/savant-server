import json
import socket
import threading
import uuid

import pytest

from code_intelligence.bridge_client import CodeGraphBridgeClient
from code_intelligence.codegraph_provider import CodeGraphProvider
from code_intelligence.provider import CodeIntelligenceError, ErrorCategory

pytestmark = pytest.mark.no_db


def serve_once(path, response):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path)); server.listen(1)
    def run():
        connection, _ = server.accept()
        request = json.loads(connection.makefile().readline())
        body = response(request)
        connection.sendall(json.dumps(body).encode() + b"\n")
        connection.close(); server.close()
    threading.Thread(target=run, daemon=True).start()


def test_client_correlates_request_and_maps_typed_error(tmp_path):
    path = f"/tmp/cg-{uuid.uuid4().hex}.sock"
    serve_once(path, lambda req: {"id": req["id"], "ok": False, "error": {"code": "NOT_INDEXED", "message": "index first"}})
    with pytest.raises(CodeIntelligenceError) as caught:
        CodeGraphBridgeClient(path).call("health", repo_id="repo")
    assert caught.value.category is ErrorCategory.NOT_INDEXED


def test_client_rejects_oversized_response(tmp_path):
    path = f"/tmp/cg-{uuid.uuid4().hex}.sock"
    serve_once(path, lambda req: {"id": req["id"], "ok": True, "result": "x" * 1000})
    with pytest.raises(CodeIntelligenceError, match="size cap"):
        CodeGraphBridgeClient(path, max_response_bytes=100).call("health", repo_id="repo")


class FakeClient:
    def __init__(self): self.calls = []
    def call(self, op, **kwargs):
        self.calls.append((op, kwargs))
        if op == "register": return {"repo_id": kwargs["repo_id"]}
        return [{"id":"codegraph:1","name":"run","qualified_name":"a.run","kind":"function","language":"python","location":{"repo_id":"repo","file_path":"a.py","start_line":1,"end_line":2},"flags":{},"metadata":{}}]


def test_provider_registers_root_once_and_validates_symbols(tmp_path):
    client = FakeClient(); provider = CodeGraphProvider(client)
    repo = {"repo_id": "repo", "root": tmp_path}
    symbols = provider.search_symbols(repo, "run", {}, 10)
    provider.search_symbols(repo, "run", {}, 10)
    assert symbols[0].id == "codegraph:1"
    assert [op for op, _ in client.calls].count("register") == 1
