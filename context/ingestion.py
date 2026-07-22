"""Project ingestion helpers for context repository sources."""

from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional
from urllib.parse import urlparse, urlunparse

import fcntl
from dulwich import porcelain
from dulwich.repo import Repo


class IngestionError(Exception):
    """Raised for user-facing ingestion failures."""


@dataclass(frozen=True)
class SourceAvailability:
    github: bool
    gitlab: bool
    directory: bool
    base_dir: Optional[str] = None
    base_host_dir: Optional[str] = None

    def as_dict(self) -> Dict[str, Dict[str, object]]:
        directory_cfg: Dict[str, object] = {"enabled": self.directory}
        if self.base_dir:
            directory_cfg["base_dir"] = self.base_dir
        if self.base_host_dir:
            directory_cfg["base_host_dir"] = self.base_host_dir
        return {
            "github": {"enabled": self.github},
            "gitlab": {"enabled": self.gitlab},
            "directory": directory_cfg,
        }


@dataclass(frozen=True)
class IngestedProject:
    name: str
    path: str
    changed: bool = False
    provider: str = "git"
    operation: str = ""
    branch: str = ""
    before_commit: str = ""
    after_commit: str = ""


def _get_git_head(repo_path: Path) -> str:
    """Get current HEAD commit hash for a git repository."""
    repo = None
    try:
        repo = Repo(str(repo_path))
        return repo.head().decode("ascii")
    except Exception:
        return ""
    finally:
        if repo is not None:
            repo.close()


def inspect_project_source(repo_path: str) -> Dict[str, str]:
    """Infer source metadata for a stored project path."""
    resolved = Path(repo_path or "").expanduser().resolve()
    source_path = str(resolved)
    if not resolved.exists():
        return {
            "source": "unknown",
            "source_label": "Unknown",
            "source_origin": "",
            "source_path": source_path,
        }

    remote_url = _git_remote_url(resolved)
    if not remote_url:
        return {
            "source": "directory",
            "source_label": "Server Directory",
            "source_origin": source_path,
            "source_path": source_path,
        }

    provider = _detect_provider_from_remote(remote_url)
    return {
        "source": provider,
        "source_label": {
            "github": "GitHub",
            "gitlab": "GitLab",
            "git": "Git Repository",
        }.get(provider, "Git Repository"),
        "source_origin": _sanitize_remote_for_display(remote_url),
        "source_path": source_path,
    }


def _detect_base_host_dir(base_dir: str) -> Optional[str]:
    """Detect the host path of the base code mount.

    Parses /proc/self/mountinfo (Linux/Docker). Handles Docker Desktop on macOS
    where the filesystem type is 'fakeowner' and the real host path is assembled
    from the 'root' subpath field + the mount source base.

    Falls back to BASE_CODE_HOST_DIR env var if detection fails.
    """
    mountinfo = Path("/proc/self/mountinfo")
    if mountinfo.exists():
        try:
            for line in mountinfo.read_text().splitlines():
                parts = line.split()
                # Format: mount_id parent_id major:minor root mountpoint mount_opts [opts] - fs_type source super_opts
                if len(parts) >= 5 and parts[4] == base_dir:
                    root_subpath = parts[3]  # subpath within the source fs
                    if " - " in line:
                        after = line.split(" - ", 1)[1]
                        fs_parts = after.split()
                        fs_type = fs_parts[0] if fs_parts else ""
                        source = fs_parts[1] if len(fs_parts) > 1 else ""
                        # Docker Desktop macOS: fakeowner fs, source=/run/host_mark/X
                        # real host path = source + root_subpath, strip /run/host_mark
                        if fs_type == "fakeowner" and "/run/host_mark" in source:
                            candidate = source.replace("/run/host_mark", "", 1) + root_subpath
                            if candidate.startswith("/"):
                                return candidate
                        # Standard Linux bind mount: source IS the host path
                        if source.startswith("/") and source != base_dir:
                            return source
        except Exception:
            pass
    return os.environ.get("BASE_CODE_HOST_DIR", "").strip() or None


def get_source_availability() -> SourceAvailability:
    base_dir = os.environ.get("BASE_CODE_DIR", "").strip() or None
    base_host_dir = _detect_base_host_dir(base_dir) if base_dir else None
    return SourceAvailability(
        github=bool(os.environ.get("GITHUB_TOKEN", "").strip()),
        gitlab=bool(os.environ.get("GITLAB_TOKEN", "").strip()),
        directory=bool(base_dir),
        base_dir=base_dir,
        base_host_dir=base_host_dir,
    )


def detect_repo_provider(url: str) -> str:
    parsed = _parse_repo_url(url)
    host = (parsed.hostname or "").lower()
    if host == "github.com":
        return "github"
    if host == "gitlab.com" or host.endswith(".gitlab.com"):
        return "gitlab"
    # Treat non-GitHub hosts with owner/repo structure as self-hosted GitLab-style.
    if host and _repo_slug_from_url(parsed.path):
        return "gitlab"
    raise IngestionError("Unsupported repository URL host")


def ingest_repo(url: str, branch: Optional[str] = None) -> IngestedProject:
    parsed = _parse_repo_url(url)
    provider = detect_repo_provider(url)
    token = _token_for_provider(provider)
    if not token:
        raise IngestionError(f"{provider.title()} source is not configured")

    base_dir = _base_code_dir()
    slug = _repo_slug_from_url(parsed.path)
    if not slug:
        raise IngestionError("Repository URL must include owner/repository")

    target_path = (base_dir / slug).resolve()
    _assert_under_base(target_path, base_dir)

    safe_url = _normalize_remote_url(parsed)

    checkout_exists = target_path.exists()
    if checkout_exists:
        if not (target_path / ".git").is_dir():
            raise IngestionError(
                f"Target path already exists and is not a git repository: {target_path}"
            )
        _repository_sync_service.update(target_path, safe_url, provider, token, branch)
    else:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _repository_sync_service.clone(target_path, safe_url, provider, token, branch)

    after_commit = _get_git_head(target_path)
    return IngestedProject(
        name=slug,
        path=str(target_path),
        changed=bool(after_commit),
        provider=provider,
        operation="refresh" if checkout_exists else "clone",
        branch=branch or _get_git_branch(target_path),
        after_commit=after_commit,
    )


def refresh_repo(repo_path: str, branch: Optional[str] = None) -> IngestedProject:
    """Pull the configured provider's latest code into an existing checkout."""
    target_path = Path(repo_path).expanduser().resolve()
    if not target_path.is_dir() or not (target_path / ".git").is_dir():
        raise IngestionError("Project is not an existing Git repository")

    remote_url = _git_remote_url(target_path)
    if not remote_url:
        raise IngestionError("Project has no origin remote to refresh")

    parsed = _parse_repo_url(remote_url)
    provider = detect_repo_provider(remote_url)
    token = _token_for_provider(provider)
    if not token:
        raise IngestionError(f"{provider.title()} source is not configured")

    head_before = _get_git_head(target_path)
    _repository_sync_service.update(
        target_path, _normalize_remote_url(parsed), provider, token, branch
    )
    head_after = _get_git_head(target_path)

    changed = bool(head_before and head_after and head_before != head_after)

    return IngestedProject(
        name=target_path.name,
        path=str(target_path),
        changed=changed,
        provider=provider,
        operation="refresh",
        branch=branch or _get_git_branch(target_path),
        before_commit=head_before,
        after_commit=head_after,
    )


def ingest_directory(directory: str) -> IngestedProject:
    if not directory or not directory.strip():
        raise IngestionError("directory required")

    base_dir = _base_code_dir()
    rel_path = Path(directory.strip())
    if rel_path.is_absolute():
        raise IngestionError("Directory must be relative to BASE_CODE_DIR")

    resolved = (base_dir / rel_path).resolve()
    _assert_under_base(resolved, base_dir)

    if not resolved.exists():
        raise IngestionError(f"Directory not found: {rel_path}")
    if not resolved.is_dir():
        raise IngestionError("Path is not a directory")
    if not os.access(resolved, os.R_OK | os.X_OK):
        raise IngestionError("Directory is not accessible")

    return IngestedProject(name=resolved.name, path=str(resolved))


def _parse_repo_url(url: str):
    if not url or not url.strip():
        raise IngestionError("url required")
    candidate = url.strip()
    scp_match = re.match(r"^(?P<user>[^@/:]+)@(?P<host>[^/:]+):(?P<path>.+)$", candidate)
    if scp_match:
        return urlparse(
            f"ssh://{scp_match.group('user')}@{scp_match.group('host')}/"
            f"{scp_match.group('path').lstrip('/')}"
        )
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https", "ssh"}:
        raise IngestionError("Repository URL must be HTTPS or SSH")
    if not parsed.hostname:
        raise IngestionError("Repository URL host is invalid")
    return parsed


def _normalize_remote_url(parsed) -> str:
    host = parsed.hostname or ""
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    scheme = "https" if parsed.scheme == "ssh" else parsed.scheme
    return urlunparse(parsed._replace(scheme=scheme, netloc=netloc))


def _repo_slug_from_url(path: str) -> Optional[str]:
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    last = parts[-1]
    if last.endswith(".git"):
        last = last[:-4]
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", last).strip(".-_")
    return slug or None


def _token_for_provider(provider: str) -> str:
    if provider == "github":
        return os.environ.get("GITHUB_TOKEN", "").strip()
    if provider == "gitlab":
        return os.environ.get("GITLAB_TOKEN", "").strip()
    return ""


def _base_code_dir() -> Path:
    base = os.environ.get("BASE_CODE_DIR", "").strip()
    if not base:
        raise IngestionError("BASE_CODE_DIR is not configured")
    base_path = Path(base).expanduser().resolve()
    if not base_path.exists() or not base_path.is_dir():
        raise IngestionError("BASE_CODE_DIR is invalid or inaccessible")
    return base_path


def _assert_under_base(target: Path, base: Path) -> None:
    try:
        target.relative_to(base)
    except Exception as exc:
        raise IngestionError("Path must stay within BASE_CODE_DIR") from exc


class RepositorySyncService:
    """Maintain server-owned checkouts as exact mirrors of remote branches."""

    def clone(
        self,
        target_path: Path,
        safe_url: str,
        provider: str,
        token: str,
        branch: Optional[str],
    ) -> None:
        with self._lock(target_path):
            authenticated_error = None
            for credentials in (self._credentials(provider, token), {}):
                temporary_path = Path(
                    tempfile.mkdtemp(
                        prefix=f".{target_path.name}.savant-clone-",
                        dir=str(target_path.parent),
                    )
                )
                try:
                    porcelain.clone(
                        safe_url,
                        temporary_path,
                        branch=branch,
                        errstream=io.BytesIO(),
                        **credentials,
                    ).close()
                    temporary_path.replace(target_path)
                    return
                except Exception as exc:
                    if credentials:
                        authenticated_error = exc
                        continue
                    error = authenticated_error or exc
                    raise self._ingestion_error(error, token) from error
                finally:
                    if temporary_path.exists():
                        shutil.rmtree(temporary_path, ignore_errors=True)

    def update(
        self,
        target_path: Path,
        safe_url: str,
        provider: str,
        token: str,
        branch: Optional[str],
    ) -> None:
        with self._lock(target_path):
            repo = None
            try:
                repo = Repo(str(target_path))
                self._set_origin_url(repo, safe_url)
                fetch_result = self._fetch_with_public_fallback(repo, provider, token)
                selected_branch = branch or self._default_branch(repo, fetch_result)
                remote_ref = f"refs/remotes/origin/{selected_branch}".encode()
                try:
                    remote_commit = repo.refs[remote_ref]
                except KeyError as exc:
                    raise IngestionError(f"Branch not found: {selected_branch}") from exc

                local_ref = f"refs/heads/{selected_branch}".encode()
                repo.refs.set_symbolic_ref(b"HEAD", local_ref)
                repo.refs[local_ref] = remote_commit
                porcelain.reset(repo, "hard", remote_commit)
                porcelain.clean(repo, target_dir=target_path)
            except IngestionError:
                raise
            except Exception as exc:
                raise self._ingestion_error(exc, token) from exc
            finally:
                if repo is not None:
                    repo.close()

    @staticmethod
    def _credentials(provider: str, token: str) -> Dict[str, str]:
        username = "x-access-token" if provider == "github" else "oauth2"
        return {"username": username, "password": token}

    @classmethod
    def _fetch_with_public_fallback(cls, repo: Repo, provider: str, token: str):
        authenticated_error = None
        for credentials in (cls._credentials(provider, token), {}):
            try:
                return porcelain.fetch(
                    repo,
                    "origin",
                    prune=True,
                    force=True,
                    quiet=True,
                    outstream=io.StringIO(),
                    errstream=io.BytesIO(),
                    **credentials,
                )
            except Exception as exc:
                if credentials:
                    authenticated_error = exc
                    continue
                raise authenticated_error or exc

    @staticmethod
    def _set_origin_url(repo: Repo, safe_url: str) -> None:
        config = repo.get_config()
        config.set((b"remote", b"origin"), b"url", safe_url.encode())
        config.write_to_path()

    @staticmethod
    def _default_branch(repo: Repo, fetch_result) -> str:
        remote_head = (fetch_result.symrefs or {}).get(b"HEAD", b"")
        prefix = b"refs/heads/"
        if remote_head.startswith(prefix):
            return remote_head[len(prefix):].decode()

        local_head = repo.refs.read_ref(b"HEAD") or b""
        if local_head.startswith(prefix):
            return local_head[len(prefix):].decode()
        raise IngestionError("Unable to determine repository default branch")

    @staticmethod
    @contextmanager
    def _lock(target_path: Path) -> Iterator[None]:
        lock_path = target_path.parent / f".{target_path.name}.savant-sync.lock"
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _ingestion_error(exc: Exception, token: str) -> IngestionError:
        message = _sanitize_git_error(str(exc), token).strip()
        return IngestionError(message or "Failed to synchronize repository")


_repository_sync_service = RepositorySyncService()


def _sanitize_git_error(message: str, *extra_secrets: str) -> str:
    out = message
    secrets = [os.environ.get(key, "").strip() for key in ("GITHUB_TOKEN", "GITLAB_TOKEN")]
    secrets.extend(secret.strip() for secret in extra_secrets)
    for token in secrets:
        if token:
            out = out.replace(token, "[REDACTED]")
    return out


def _git_remote_url(repo_path: Path) -> str:
    repo = None
    try:
        repo = Repo(str(repo_path))
        remote_url = repo.get_config_stack().get(
            (b"remote", b"origin"), b"url"
        )
        if isinstance(remote_url, bytes):
            return remote_url.decode()
        return str(remote_url)
    except Exception:
        return ""
    finally:
        if repo is not None:
            repo.close()


def _get_git_branch(repo_path: Path) -> str:
    repo = None
    try:
        repo = Repo(str(repo_path))
        head_ref = repo.refs.read_ref(b"HEAD") or b""
        prefix = b"refs/heads/"
        return head_ref[len(prefix):].decode() if head_ref.startswith(prefix) else ""
    except Exception:
        return ""
    finally:
        if repo is not None:
            repo.close()


def _detect_provider_from_remote(remote_url: str) -> str:
    candidate = (remote_url or "").strip().lower()
    if "github.com" in candidate:
        return "github"
    if "gitlab" in candidate:
        return "gitlab"
    return "git"


def _sanitize_remote_for_display(remote_url: str) -> str:
    remote = (remote_url or "").strip()
    if not remote:
        return ""

    if remote.startswith(("http://", "https://")):
        parsed = urlparse(remote)
        host = parsed.hostname or ""
        netloc = host
        if parsed.port:
            netloc = f"{host}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))

    if remote.startswith("git@"):
        # Keep SSH remote readable but remove any accidental password-like segment.
        return remote.split("@", 1)[0] + "@" + remote.split("@", 1)[1]

    return remote
