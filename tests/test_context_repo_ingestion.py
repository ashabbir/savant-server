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


def _cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_sources_endpoint_reflects_env(client, monkeypatch):
    from context import routes

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setenv("GITHUB_TOKEN", "gh-test")
    monkeypatch.setenv("GITLAB_TOKEN", "")
    monkeypatch.setenv("BASE_CODE_DIR", "/tmp/repos")
    monkeypatch.setenv("BASE_CODE_HOST_DIR", "/Users/me/code/archived")

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

    def fake_run_git(cmd, raise_on_error=True):
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

    def fake_run_git(cmd, raise_on_error=True):
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
