import os
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))


def _seed_python_repo(tmp_path: Path, name: str = "ctx-ast-repo") -> Path:
    repo_dir = tmp_path / name
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "sample.py").write_text(
        """
class Service:
    def run(self):
        return 1

def helper(x):
    return x + 1
""".strip()
    )
    return repo_dir


def _seed_js_repo(tmp_path: Path, name: str = "ctx-ast-js-repo") -> Path:
    repo_dir = tmp_path / name
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "sample.js").write_text(
        """
class AuthService {
    login(user) {
        return true;
    }
}

function connect() {
    return "ok";
}
""".strip()
    )
    return repo_dir


def test_js_ast_generation(client, tmp_path, monkeypatch):
    from context.db import ContextDB, init_context_schema
    from context.indexer import Indexer

    assert init_context_schema()

    class _FakeEmbedder:
        def embed_one(self, _text):
            return [0.0] * 768

    monkeypatch.setattr(Indexer, "_get_embedder", lambda self: _FakeEmbedder())

    repo_dir = _seed_js_repo(tmp_path, "repo-js")
    ContextDB.add_repo("repo-js", str(repo_dir))

    Indexer().index_repository(repo_dir, repo_name="repo-js")
    Indexer().generate_ast_for_repository(repo_dir, repo_name="repo-js")

    resp = client.get("/api/context/ast/list?repo=repo-js")
    assert resp.status_code == 200

    data = resp.get_json()
    assert data["ast_count"] >= 3

    names = {(n["node_type"], n["name"]) for n in data["nodes"]}
    assert ("class", "AuthService") in names
    assert ("function", "login") in names
    assert ("function", "connect") in names


def test_index_generates_ast_and_repo_overview_count(client, tmp_path, monkeypatch):
    from context.db import ContextDB, init_context_schema
    from context.indexer import Indexer

    assert init_context_schema()

    class _FakeEmbedder:
        def embed_one(self, _text):
            return [0.0] * 768

    monkeypatch.setattr(Indexer, "_get_embedder", lambda self: _FakeEmbedder())

    repo_dir = _seed_python_repo(tmp_path, "repo-overview")
    ContextDB.add_repo("repo-overview", str(repo_dir))

    Indexer().index_repository(repo_dir, repo_name="repo-overview")

    repos = client.get("/api/context/repos").get_json()["repos"]
    repo = next(r for r in repos if r["name"] == "repo-overview")

    assert repo["status"] == "indexed"
    assert repo["ast_node_count"] >= 3


def test_ast_list_returns_generated_nodes(client, tmp_path, monkeypatch):
    from context.db import ContextDB, init_context_schema
    from context.indexer import Indexer

    assert init_context_schema()

    class _FakeEmbedder:
        def embed_one(self, _text):
            return [0.0] * 768

    monkeypatch.setattr(Indexer, "_get_embedder", lambda self: _FakeEmbedder())

    repo_dir = _seed_python_repo(tmp_path, "repo-ast-list")
    ContextDB.add_repo("repo-ast-list", str(repo_dir))

    Indexer().index_repository(repo_dir, repo_name="repo-ast-list")

    resp = client.get("/api/context/ast/list?repo=repo-ast-list")
    assert resp.status_code == 200

    data = resp.get_json()
    assert data["ast_count"] >= 3

    names = {(n["node_type"], n["name"]) for n in data["nodes"]}
    assert ("class", "Service") in names
    assert ("function", "run") in names
    assert ("function", "helper") in names


def test_generate_ast_marks_repo_ast_only_even_if_indexed(client, tmp_path, monkeypatch):
    from context.db import ContextDB, init_context_schema
    from context.indexer import Indexer

    assert init_context_schema()

    class _FakeEmbedder:
        def embed_one(self, _text):
            return [0.0] * 768

    monkeypatch.setattr(Indexer, "_get_embedder", lambda self: _FakeEmbedder())

    repo_dir = _seed_python_repo(tmp_path, "repo-status-ast")
    ContextDB.add_repo("repo-status-ast", str(repo_dir))

    idx = Indexer()
    idx.index_repository(repo_dir, repo_name="repo-status-ast")
    idx.generate_ast_for_repository(repo_dir, repo_name="repo-status-ast")

    repo = ContextDB.get_repo("repo-status-ast")
    assert repo["status"] == "ast_only"


def test_extract_ast_retries_transient_lock(tmp_path, monkeypatch):
    from context.db import ContextDB, init_context_schema
    from context.indexer import Indexer

    init_context_schema()

    repo = ContextDB.add_repo("repo-retry", str(tmp_path))
    file_id = ContextDB.insert_file(repo["id"], "retry.py", "Python", False, 1, "now")

    real_insert = ContextDB.insert_ast_node
    attempts = {"count": 0}

    def flaky_insert(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(ContextDB, "insert_ast_node", flaky_insert)

    Indexer()._extract_and_store_ast(
        file_id,
        "retry.py",
        "def retry_me():\n    return True\n",
    )

    nodes = ContextDB.list_ast_nodes("repo-retry")
    assert any(n["name"] == "retry_me" for n in nodes)


def test_insert_ast_node_legacy_schema_with_required_content(client):
    from context.db import ContextDB, init_context_schema
    from sqlite_client import get_connection

    assert init_context_schema()

    conn = get_connection()

    # Simulate legacy schema drift: content column exists and is required.
    conn.execute("DROP TABLE IF EXISTS ctx_ast_nodes")
    conn.execute(
        """
        CREATE TABLE ctx_ast_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            node_type TEXT NOT NULL,
            name TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()

    repo = ContextDB.add_repo("repo-legacy", "/tmp/repo-legacy")
    file_id = ContextDB.insert_file(repo["id"], "legacy.py", "Python", False, 1, "now")

    node_id = ContextDB.insert_ast_node(file_id, "function", "legacy_fn", 1, 2)
    assert node_id > 0

    rows = conn.execute("SELECT name, content FROM ctx_ast_nodes").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "legacy_fn"
    assert rows[0]["content"] == ""


def test_context_mcp_structure_search_accepts_q_alias(monkeypatch):
    import context_server

    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"ok": True}

    monkeypatch.setattr(context_server, "_get", fake_get)

    out = context_server.structure_search(q="Service", repo="repo-overview")
    assert out == {"ok": True}
    assert captured["path"] == "/api/context/ast/search"
    assert captured["params"]["query"] == "Service"
    assert captured["params"]["repo"] == "repo-overview"


def test_context_analysis_by_class_name(monkeypatch):
    from context.analysis import AnalysisTarget, analyze_code

    content = """
class Service:
    def run(self):
        if True:
            return 1
""".strip()

    analysis = analyze_code(
        content_before=content,
        target=AnalysisTarget(path="sample.py", name="Service", node_type="class"),
    )

    assert analysis["summary"]["before_complexity"] >= 1
    assert analysis["summary"]["after_complexity"] >= 1
    assert analysis["target"]["found_after"] is True


def test_context_analysis_new_code_starts_from_zero_when_missing_before():
    from context.analysis import AnalysisTarget, analyze_code

    analysis = analyze_code(
        content_before="",
        content_after="""
class NewService:
    def run(self):
        return 1
""".strip(),
        target=AnalysisTarget(path="new.py", name="NewService", node_type="class"),
        target_missing_is_new=True,
    )

    assert analysis["summary"]["before_complexity"] == 0
    assert analysis["summary"]["after_complexity"] >= 1
    assert analysis["summary"]["status"] == "new"


def test_context_analysis_applies_unified_diff():
    from context.analysis import AnalysisTarget, analyze_code

    before = """
class Service:
    def run(self):
        return 1
""".strip()
    diff = """\
@@ -1,3 +1,5 @@
 class Service:
     def run(self):
-        return 1
+        if True:
+            return 2
"""

    analysis = analyze_code(
        content_before=before,
        diff=diff,
        target=AnalysisTarget(path="sample.py", name="Service", node_type="class"),
    )

    assert analysis["summary"]["after_complexity"] >= analysis["summary"]["before_complexity"]
    assert analysis["delta"]["complexity"] >= 0


def test_context_analysis_emits_frontend_style_categories():
    from context.analysis import AnalysisTarget, analyze_code

    content = """
def sample(a, b, c, d, e, f):
    secret = "TOKEN=abcdef123456"
    df.append(1)
    if True:
        if True:
            if True:
                if True:
                    if True:
                        return 1
    print("after return")
""".strip()

    analysis = analyze_code(
        content_before=content,
        target=AnalysisTarget(path="sample.py", name="sample", node_type="function"),
    )

    categories = {f["category"] for f in analysis["after"]["findings"]}
    assert "structural" in categories
    assert "security" in categories
    assert "modernization" in categories
    assert "style" in categories
    assert "dead_code" in categories


def test_context_analysis_api_and_mcp_proxy(monkeypatch, client, tmp_path):
    from context.db import ContextDB, init_context_schema
    from context.indexer import Indexer
    import context_server

    assert init_context_schema()

    class _FakeEmbedder:
        def embed_one(self, _text):
            return [0.0] * 768

    monkeypatch.setattr(Indexer, "_get_embedder", lambda self: _FakeEmbedder())

    repo_dir = _seed_python_repo(tmp_path, "repo-analysis")
    ContextDB.add_repo("repo-analysis", str(repo_dir))
    Indexer().index_repository(repo_dir, repo_name="repo-analysis")
    Indexer().generate_ast_for_repository(repo_dir, repo_name="repo-analysis")

    resp = client.post("/api/context/analysis", json={
        "repo": "repo-analysis",
        "path": "sample.py",
        "name": "Service",
        "node_type": "class",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["summary"]["after_complexity"] >= 1
    assert data["target"]["found_after"] is True

    captured = {}

    def fake_post(path, json=None):
        captured["path"] = path
        captured["json"] = json
        return {"ok": True}

    monkeypatch.setattr(context_server, "_post", fake_post)
    out = context_server.analyze_code(repo="repo-analysis", path="sample.py", name="Service", node_type="class")
    assert out == {"ok": True}
    assert captured["path"] == "/api/context/analysis"
    assert captured["json"]["name"] == "Service"


def test_context_analysis_accepts_diff_only_payload(client):
    resp = client.post("/api/context/analysis", json={
        "diff": """\
@@ -1,3 +1,5 @@
 class Service:
     def run(self):
-        return 1
+        if True:
+            return 2
""",
    })

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["target"]["path"] == ""
    assert data["summary"]["status"] in {"new", "updated"}
