import importlib.util
from pathlib import Path

import requests


spec = importlib.util.spec_from_file_location(
    "savant_mcp_context_contract_server",
    Path(__file__).parents[1] / "mcp" / "context_server.py",
)
context_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context_server)


class _BadResponse:
    def raise_for_status(self):
        raise requests.HTTPError("400 Client Error")


def test_context_mcp_http_errors_are_raised(monkeypatch):
    monkeypatch.setattr(context_server.requests, "get", lambda *args, **kwargs: _BadResponse())

    try:
        context_server._get("/api/context/ast/search", {"query": ""})
    except requests.HTTPError as exc:
        assert "400 Client Error" in str(exc)
    else:
        raise AssertionError("HTTP failures must propagate so FastMCP marks the tool call as an error")
