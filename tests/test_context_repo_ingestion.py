from pathlib import Path
import subprocess

import pytest

from context.ingestion import (
    IngestedProject,
    IngestionError,
    detect_repo_provider,
    ingest_directory,
    ingest_repo,
)
from context.walker import FileWalker


def _cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_sources_endpoint_reflects_env(client, monkeypatch):
    from context import routes
    import context.ingestion as ingestion

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setenv("GITHUB_TOKEN", "gh-test")
    monkeypatch.setenv("GITLAB_TOKEN", "")
    monkeypatch.setenv("BASE_CODE_DIR", "/tmp/repos")
    monkeypatch.setenv("BASE_CODE_HOST_DIR", "/Users/me/code/archived")
    # Patch _detect_base_host_dir to simulate no mountinfo match → falls back to env var
    monkeypatch.setattr(ingestion, "_detect_base_host_dir", lambda base_dir: "/Users/me/code/archived")

    resp = client.get("/api/context/repos/sources")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sources"]["github"]["enabled"] is True
    assert data["sources"]["gitlab"]["enabled"] is False
    assert data["sources"]["directory"]["enabled"] is True
    assert data["sources"]["directory"]["base_dir"] == "/tmp/repos"
    assert data["sources"]["directory"]["base_host_dir"] == "/Users/me/code/archived"
    assert data["any_enabled"] is True


def test_sources_endpoint_no_sources(client, monkeypatch):
    from context import routes

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("BASE_CODE_DIR", raising=False)

    resp = client.get("/api/context/repos/sources")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["any_enabled"] is False
    assert all(not item["enabled"] for item in data["sources"].values())


def test_ingest_directory_valid(tmp_path, monkeypatch):
    base = tmp_path / "repos"
    target = base / "apps" / "api"
    target.mkdir(parents=True)

    monkeypatch.setenv("BASE_CODE_DIR", str(base))

    out = ingest_directory("apps/api")
    assert out.name == "api"
    assert out.path == str(target.resolve())


@pytest.mark.parametrize("rel", ["../other", "../../etc"])
def test_ingest_directory_rejects_traversal(tmp_path, monkeypatch, rel):
    base = tmp_path / "repos"
    base.mkdir(parents=True)
    monkeypatch.setenv("BASE_CODE_DIR", str(base))

    with pytest.raises(IngestionError, match="Path must stay within BASE_CODE_DIR"):
        ingest_directory(rel)


def test_ingest_directory_rejects_missing_path(tmp_path, monkeypatch):
    base = tmp_path / "repos"
    base.mkdir(parents=True)
    monkeypatch.setenv("BASE_CODE_DIR", str(base))

    with pytest.raises(IngestionError, match="Directory not found"):
        ingest_directory("does-not-exist")


def test_detect_repo_provider_variants():
    assert detect_repo_provider("https://github.com/org/repo.git") == "github"
    assert detect_repo_provider("https://gitlab.com/org/repo") == "gitlab"
    assert detect_repo_provider("https://gitlab.internal.local/group/repo.git") == "gitlab"
    assert detect_repo_provider("git@github.com:org/repo.git") == "github"
    assert detect_repo_provider("ssh://git@gitlab.com/org/repo.git") == "gitlab"
    assert detect_repo_provider("git@gitlab.internal.local:group/repo.git") == "gitlab"


def test_ssh_repo_url_is_normalized_to_credential_free_https():
    from context.ingestion import _normalize_remote_url, _parse_repo_url

    parsed = _parse_repo_url("git@github.com:org/repo.git")

    assert _normalize_remote_url(parsed) == "https://github.com/org/repo.git"


def test_ssh_repo_clone_uses_askpass_environment_not_token_in_command(tmp_path, monkeypatch):
    base = tmp_path / "repos"
    base.mkdir()
    monkeypatch.setenv("BASE_CODE_DIR", str(base))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")

    commands = []
    environments = []

    def fake_run_git(cmd, raise_on_error=True, env=None):
        commands.append(cmd)
        environments.append(env)
        return _cp(cmd, returncode=0)

    monkeypatch.setattr("context.ingestion._run_git", fake_run_git)

    out = ingest_repo("git@github.com:acme/repo.git")

    assert out.name == "repo"
    assert any(cmd[:2] == ["git", "clone"] for cmd in commands)
    assert all("ghp_test_token" not in " ".join(cmd) for cmd in commands)
    auth_env = next(env for env in environments if env)
    assert auth_env["GIT_ASKPASS"]
    assert auth_env["SAVANT_GIT_ASKPASS_TOKEN"] == "ghp_test_token"
    assert any("remote" in cmd and "set-url" in cmd and "origin" in cmd and "https://github.com/acme/repo.git" in cmd for cmd in commands)


def test_ingest_repo_rejects_missing_token(tmp_path, monkeypatch):
    base = tmp_path / "repos"
    base.mkdir(parents=True)

    monkeypatch.setenv("BASE_CODE_DIR", str(base))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(IngestionError, match="Github source is not configured"):
        ingest_repo("https://github.com/org/repo.git")


def test_ingest_repo_branch_success_for_existing_checkout(tmp_path, monkeypatch):
    base = tmp_path / "repos"
    repo_dir = base / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    monkeypatch.setenv("BASE_CODE_DIR", str(base))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")

    commands = []

    def fake_run_git(cmd, raise_on_error=True, env=None):
        commands.append(cmd)
        if "show-ref" in cmd:
            return _cp(cmd, returncode=0)
        return _cp(cmd, returncode=0)

    monkeypatch.setattr("context.ingestion._run_git", fake_run_git)

    out = ingest_repo("https://github.com/acme/repo.git", branch="release")

    assert out.name == "repo"
    assert out.path == str(repo_dir.resolve())
    assert any(cmd[-2:] == ["checkout", "release"] for cmd in commands)
    assert any(cmd[-3:] == ["pull", "origin", "release"] for cmd in commands)


def test_ingest_repo_branch_failure_for_existing_checkout(tmp_path, monkeypatch):
    base = tmp_path / "repos"
    repo_dir = base / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    monkeypatch.setenv("BASE_CODE_DIR", str(base))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")

    def fake_run_git(cmd, raise_on_error=True, env=None):
        if "show-ref" in cmd:
            return _cp(cmd, returncode=1, stderr="fatal: bad ref")
        return _cp(cmd, returncode=0)

    monkeypatch.setattr("context.ingestion._run_git", fake_run_git)

    with pytest.raises(IngestionError, match="Branch not found: missing"):
        ingest_repo("https://github.com/acme/repo.git", branch="missing")


def test_add_repo_route_updates_existing_repo_without_duplicate(client, monkeypatch):
    from context import routes
    from context import db as context_db

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setattr(
        "context.ingestion.ingest_repo",
        lambda url, branch=None: IngestedProject(name="repo", path="/tmp/repos/repo"),
    )

    calls = {"add": 0}

    monkeypatch.setattr(
        context_db.ContextDB,
        "get_repo",
        staticmethod(lambda _name: {"id": 4, "name": "repo", "path": "/tmp/repos/repo"}),
    )

    def fake_add_repo(name, path):
        calls["add"] += 1
        return {"id": 4, "name": name, "path": path, "status": "added"}

    monkeypatch.setattr(context_db.ContextDB, "add_repo", staticmethod(fake_add_repo))

    resp = client.post(
        "/api/context/repos",
        json={"source": "github", "url": "https://github.com/acme/repo.git"},
    )

    assert resp.status_code == 201
    assert calls["add"] == 1
    assert resp.get_json()["name"] == "repo"


def test_add_repo_route_rejects_source_url_mismatch(client, monkeypatch):
    from context import routes

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)

    resp = client.post(
        "/api/context/repos",
        json={"source": "github", "url": "https://gitlab.com/acme/repo.git"},
    )

    assert resp.status_code == 400
    assert "does not match source" in resp.get_json()["error"]


def test_refresh_repo_updates_existing_checkout(client, monkeypatch):
    from context import routes
    from context import db as context_db

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setattr(routes, "_validate_repo_path", lambda _repo: (Path("/tmp/repos/repo"), None))
    monkeypatch.setattr(
        context_db.ContextDB,
        "get_repo",
        staticmethod(lambda _name: {"id": 4, "name": "repo", "path": "/tmp/repos/repo"}),
    )
    refreshed = []
    monkeypatch.setattr(
        "context.ingestion.refresh_repo",
        lambda path, branch=None: refreshed.append((path, branch)) or IngestedProject(
            name="repo", path="/tmp/repos/repo"
        ),
    )
    monkeypatch.setattr(
        context_db.ContextDB,
        "add_repo",
        staticmethod(lambda name, path: {"id": 4, "name": name, "path": path, "status": "added"}),
    )
    monkeypatch.setattr(context_db.ContextDB, "mark_repo_fetched", staticmethod(lambda name: None))

    resp = client.post("/api/context/repos/repo/refresh")

    assert resp.status_code == 200
    assert refreshed == [("/tmp/repos/repo", None)]
    assert resp.get_json()["name"] == "repo"


def test_file_walker_respects_gitignore_and_node_modules(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    (repo / ".gitignore").write_text("generated/\n")
    (repo / "src").mkdir()
    (repo / "src" / "main.ts").write_text("export const ok = true;\n")
    (repo / "src" / ".gitignore").write_text("private/\n")
    (repo / "src" / "private").mkdir()
    (repo / "src" / "private" / "secret.ts").write_text("export const secret = true;\n")
    (repo / "generated").mkdir()
    (repo / "generated" / "auto.ts").write_text("export const auto = true;\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "pkg.js").write_text("module.exports = {};\n")
    subprocess.run(["git", "-C", str(repo), "add", "-f", "generated/auto.ts"], check=True)

    files = sorted(str(path).replace("\\", "/") for path in FileWalker(repo).walk())

    assert files == ["src/main.ts"]
