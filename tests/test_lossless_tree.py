import importlib.util
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_db


def test_lossless_tree_preserves_exact_source_and_concrete_ranges():
    from context.lossless_tree import bounded_tree, build_lossless_tree, source_hash

    source = "# keep this comment\nclass Service:\n    def run(self):\n        return 1  # exact trivia\n"
    artifact = build_lossless_tree("service.py", source)

    assert artifact["source"] == source
    assert artifact["source_hash"] == source_hash(source)
    assert artifact["tree"]["nodes"][0]["start_byte"] == 0
    assert artifact["tree"]["nodes"][0]["end_byte"] == len(source.encode("utf-8"))

    sliced = bounded_tree(artifact, start_line=2, end_line=3, max_nodes=20)
    assert sliced["source"] == "class Service:\n    def run(self):\n"
    assert sliced["source_range"]["start_line"] == 2
    assert sliced["source_range"]["end_line"] == 3
    assert len(sliced["nodes"]) <= 20


def test_lossless_tree_falls_back_to_source_only_for_unknown_extension():
    from context.lossless_tree import build_lossless_tree

    artifact = build_lossless_tree("README.unknown", "keep every byte: é")

    assert artifact["tree"]["parser"] == "source-only"
    assert artifact["source"] == "keep every byte: é"
    assert artifact["tree"]["nodes"][0]["end_byte"] == len(artifact["source"].encode("utf-8"))
    assert artifact["tree"]["nodes"][0]["end_point"] == [1, len(artifact["source"].encode("utf-8"))]


def test_context_mcp_lossless_tools_proxy_expected_requests(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "savant_mcp_context_lossless_server",
        Path(__file__).parents[1] / "mcp" / "context_server.py",
    )
    context_server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(context_server)
    calls = []
    monkeypatch.setattr(context_server, "_get", lambda path, params=None: calls.append((path, params)) or {"ok": True})

    assert context_server.get_lossless_tree(["a", "b"], "src/a.py", 2, 4, 99) == {"ok": True}
    assert context_server.search_lossless_tree("needle", ["a", "b"], 7) == {"ok": True}
    assert calls == [
        ("/api/context/lossless-tree", {"repo": "a,b", "path": "src/a.py", "max_nodes": 99,
                                         "start_line": 2, "end_line": 4}),
        ("/api/context/lossless-tree/search", {"q": "needle", "limit": 7, "repo": "a,b"}),
    ]
