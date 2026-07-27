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


def test_ssh_repo_clone_uses_library_credentials_and_safe_remote(tmp_path, monkeypatch):
    base = tmp_path / "repos"
    base.mkdir()
    monkeypatch.setenv("BASE_CODE_DIR", str(base))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")

    calls = []

    class FakeSyncService:
        def clone(self, target_path, safe_url, provider, token, branch):
            calls.append((target_path, safe_url, provider, token, branch))

    monkeypatch.setattr("context.ingestion._repository_sync_service", FakeSyncService())

    out = ingest_repo("git@github.com:acme/repo.git")

    assert out.name == "repo"
    assert calls == [(
        (base / "repo").resolve(),
        "https://github.com/acme/repo.git",
        "github",
        "ghp_test_token",
        None,
    )]


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

    calls = []

    class FakeSyncService:
        def update(self, target_path, safe_url, provider, token, branch):
            calls.append((target_path, safe_url, provider, token, branch))

    monkeypatch.setattr("context.ingestion._repository_sync_service", FakeSyncService())

    out = ingest_repo("https://github.com/acme/repo.git", branch="release")

    assert out.name == "repo"
    assert out.path == str(repo_dir.resolve())
    assert calls == [(
        repo_dir.resolve(),
        "https://github.com/acme/repo.git",
        "github",
        "ghp_test_token",
        "release",
    )]


def test_ingest_repo_branch_failure_for_existing_checkout(tmp_path, monkeypatch):
    base = tmp_path / "repos"
    repo_dir = base / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    monkeypatch.setenv("BASE_CODE_DIR", str(base))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")

    class FakeSyncService:
        def update(self, target_path, safe_url, provider, token, branch):
            raise IngestionError(f"Branch not found: {branch}")

    monkeypatch.setattr("context.ingestion._repository_sync_service", FakeSyncService())

    with pytest.raises(IngestionError, match="Branch not found: missing"):
        ingest_repo("https://github.com/acme/repo.git", branch="missing")


def test_repository_sync_force_matches_latest_remote_and_removes_local_drift(tmp_path):
    from context.ingestion import RepositorySyncService

    remote = tmp_path / "remote.git"
    author = tmp_path / "author"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(author)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(author), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(author), "config", "user.name", "Test"], check=True)
    (author / "tracked.txt").write_text("version one\n")
    subprocess.run(["git", "-C", str(author), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(author), "commit", "-m", "one"], check=True, capture_output=True)
    branch = subprocess.run(
        ["git", "-C", str(author), "branch", "--show-current"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(author), "push", "origin", branch], check=True, capture_output=True)

    service = RepositorySyncService()
    service.clone(checkout, remote.as_uri(), "github", "unused", branch)
    (checkout / "tracked.txt").write_text("local modification\n")
    (checkout / "untracked.txt").write_text("remove me\n")

    (author / "tracked.txt").write_text("version two\n")
    subprocess.run(["git", "-C", str(author), "commit", "-am", "two"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(author), "push", "origin", branch], check=True, capture_output=True)

    service.update(checkout, remote.as_uri(), "github", "unused", None)

    assert (checkout / "tracked.txt").read_text() == "version two\n"
    assert not (checkout / "untracked.txt").exists()
    local_head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    remote_head = subprocess.run(
        ["git", "-C", str(author), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert local_head == remote_head


def test_repository_sync_uses_provider_credentials_and_redacts_token():
    from context.ingestion import RepositorySyncService

    service = RepositorySyncService()

    assert service._credentials("github", "secret") == {
        "username": "x-access-token",
        "password": "secret",
    }
    assert service._credentials("gitlab", "secret") == {
        "username": "oauth2",
        "password": "secret",
    }
    error = service._ingestion_error(RuntimeError("denied for secret"), "secret")
    assert str(error) == "denied for [REDACTED]"


def test_repository_sync_clone_falls_back_to_anonymous_for_public_repo(tmp_path, monkeypatch):
    from context.ingestion import RepositorySyncService

    calls = []

    class FakeRepo:
        def close(self):
            pass

    def fake_clone(_url, target, **kwargs):
        calls.append({key: kwargs[key] for key in ("username", "password") if key in kwargs})
        if "username" in kwargs:
            raise RuntimeError("No valid credentials provided")
        (Path(target) / ".git").mkdir()
        return FakeRepo()

    monkeypatch.setattr("context.ingestion.porcelain.clone", fake_clone)

    target = tmp_path / "public-repo"
    RepositorySyncService().clone(
        target,
        "https://gitlab.example.org/group/public-repo.git",
        "gitlab",
        "invalid-for-host",
        None,
    )

    assert calls == [
        {"username": "oauth2", "password": "invalid-for-host"},
        {},
    ]
    assert (target / ".git").is_dir()


def test_repository_sync_fetch_falls_back_to_anonymous_for_public_repo(monkeypatch):
    from context.ingestion import RepositorySyncService

    calls = []
    expected = object()

    def fake_fetch(_repo, _remote, **kwargs):
        calls.append({key: kwargs[key] for key in ("username", "password") if key in kwargs})
        if "username" in kwargs:
            raise RuntimeError("No valid credentials provided")
        return expected

    monkeypatch.setattr("context.ingestion.porcelain.fetch", fake_fetch)

    result = RepositorySyncService._fetch_with_public_fallback(
        object(), "gitlab", "invalid-for-host",
    )

    assert result is expected
    assert calls == [
        {"username": "oauth2", "password": "invalid-for-host"},
        {},
    ]


def test_add_repo_route_registers_immediately_and_queues_background_sync(client, monkeypatch):
    from context import routes
    from context import db as context_db
    from context.ingestion import RepositoryRegistration
    from db.jobs import JobDB

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setattr(
        "context.ingestion.prepare_repository_registration",
        lambda url, branch=None: RepositoryRegistration(
            name="repo", path="/tmp/repos/repo", provider="github",
            url="https://github.com/acme/repo.git", branch=branch or "",
        ),
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

    assert resp.status_code == 202
    assert calls["add"] == 1
    body = resp.get_json()
    assert body["name"] == "repo"
    assert body["registration_accepted"] is True
    assert body["job_type"] == "initial_repo_sync"
    job = JobDB.get_job(body["job_id"])
    assert job["status"] == "queued"
    assert job["result"] == {
        "source": "github", "url": "https://github.com/acme/repo.git",
        "branch": "", "provider": "github", "actor_id": "ahmed",
        "source_app": "savant-olympus",
    }


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
            name="repo", path="/tmp/repos/repo", changed=True, provider="gitlab",
            branch="main", before_commit="abc123", after_commit="def456",
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
    log = context_db.ContextDB.list_repo_sync_logs(repo_name="repo")[0]
    assert log["operation"] == "refresh"
    assert log["trigger"] == "manual"
    assert log["provider"] == "gitlab"
    assert log["before_commit"] == "abc123"
    assert log["after_commit"] == "def456"
    assert log["actor_id"] == "ahmed"
    assert log["source_app"] == "savant-olympus"


def test_refresh_repo_failure_is_recorded(client, monkeypatch):
    from context import routes
    from context import db as context_db

    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.setattr(routes, "_validate_repo_path", lambda _repo: (Path("/tmp/repos/repo"), None))
    monkeypatch.setattr(
        context_db.ContextDB,
        "get_repo",
        staticmethod(lambda _name: {"id": 4, "name": "repo", "path": "/tmp/repos/repo"}),
    )

    def fail_refresh(path, branch=None):
        raise IngestionError("remote unavailable")

    monkeypatch.setattr("context.ingestion.refresh_repo", fail_refresh)

    resp = client.post("/api/context/repos/repo/refresh")

    assert resp.status_code == 400
    log = context_db.ContextDB.list_repo_sync_logs(repo_name="repo")[0]
    assert log["status"] == "failed"
    assert log["operation"] == "refresh"
    assert log["error"] == "remote unavailable"


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
