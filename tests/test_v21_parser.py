"""Native Markdown failures must remain outside the queue worker process."""
import json
import subprocess
from types import SimpleNamespace

import pytest
from context.lossless_tree import build_lossless_tree
from context.indexer import Indexer

pytestmark = pytest.mark.no_db


@pytest.mark.parametrize('failure', [-11, 'timeout', 'invalid_json'])
def test_markdown_subprocess_failure_retains_exact_source(monkeypatch, failure):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        return SimpleNamespace(returncode=-11 if failure == -11 else 0, stdout='bad', stderr='crashed')
    monkeypatch.setattr(subprocess, 'run', run)
    source = '# Héllo\n\nBroken scanner input\n'
    artifact = build_lossless_tree('README.md', source)
    assert calls, 'Markdown must be parsed in a subprocess'
    assert calls[0][1]['timeout'] == 30
    assert calls[0][1]['input'] == source
    assert artifact['source'] == source
    assert artifact['tree']['parser'] == 'source-only'
    assert artifact['tree']['warning']
    assert len(artifact['tree']['nodes']) == 1
    assert artifact['tree']['nodes'][0]['end_byte'] == len(source.encode())


def test_markdown_success_returns_child_artifact(monkeypatch):
    tree = {'parser': 'tree-sitter', 'nodes': [{'type': 'document'}]}
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(tree)))
    assert build_lossless_tree('README.mdx', '# Hello')['tree'] == tree


def test_markdown_declaration_extraction_never_loads_native_parser(monkeypatch):
    import sys
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError('Native Markdown parser loaded in worker')
    monkeypatch.setitem(sys.modules, 'tree_sitter_languages', Forbidden())
    Indexer()._extract_and_store_ast(1, 'README.md', '# hello')


def test_markdown_child_process_returns_a_real_tree():
    source = '# Heading\n\nSome text.\n'
    artifact = build_lossless_tree('README.md', source)
    assert artifact['source'] == source
    assert artifact['tree']['parser'] == 'tree-sitter'
    assert artifact['tree']['nodes'][0]['type'] == 'document'
