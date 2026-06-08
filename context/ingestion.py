"""Project ingestion helpers for context repository sources."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse, urlunparse


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


def get_source_availability() -> SourceAvailability:
    base_dir = os.environ.get("BASE_CODE_DIR", "").strip() or None
    base_host_dir = os.environ.get("BASE_CODE_HOST_DIR", "").strip() or None
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
    auth_url = _build_auth_url(parsed, provider, token)

    if target_path.exists():
        if not (target_path / ".git").is_dir():
            raise IngestionError(
                f"Target path already exists and is not a git repository: {target_path}"
            )
        _update_checkout(target_path, auth_url, safe_url, branch)
    else:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _clone_checkout(target_path, auth_url, safe_url, branch)

    return IngestedProject(name=slug, path=str(target_path))


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
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise IngestionError("Repository URL must start with http:// or https://")
    if not parsed.hostname:
        raise IngestionError("Repository URL host is invalid")
    return parsed


def _normalize_remote_url(parsed) -> str:
    host = parsed.hostname or ""
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


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


def _build_auth_url(parsed, provider: str, token: str) -> str:
    # Keep credentials out of logs/errors; this URL is only passed directly to git.
    if provider == "github":
        netloc = f"x-access-token:{token}@{parsed.hostname}"
    else:
        netloc = f"oauth2:{token}@{parsed.hostname}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _clone_checkout(target_path: Path, auth_url: str, safe_url: str, branch: Optional[str]) -> None:
    cmd = ["git", "clone"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([auth_url, str(target_path)])
    _run_git(cmd)

    # Reset origin URL without credentials after clone.
    _run_git(["git", "-C", str(target_path), "remote", "set-url", "origin", safe_url])

    if branch:
        _ensure_local_branch(target_path, branch)


def _update_checkout(target_path: Path, auth_url: str, safe_url: str, branch: Optional[str]) -> None:
    _run_git(["git", "-C", str(target_path), "remote", "set-url", "origin", auth_url])
    try:
        _run_git(["git", "-C", str(target_path), "fetch", "origin", "--prune"])
        if branch:
            _ensure_branch_exists(target_path, branch)
            _run_git(["git", "-C", str(target_path), "checkout", branch])
            _run_git(["git", "-C", str(target_path), "pull", "origin", branch])
        else:
            default_branch = _default_remote_branch(target_path)
            _run_git(["git", "-C", str(target_path), "checkout", default_branch])
            _run_git(["git", "-C", str(target_path), "pull", "origin", default_branch])
    finally:
        _run_git(["git", "-C", str(target_path), "remote", "set-url", "origin", safe_url])


def _ensure_branch_exists(target_path: Path, branch: str) -> None:
    proc = _run_git(
        ["git", "-C", str(target_path), "show-ref", "--verify", f"refs/remotes/origin/{branch}"],
        raise_on_error=False,
    )
    if proc.returncode != 0:
        raise IngestionError(f"Branch not found: {branch}")


def _ensure_local_branch(target_path: Path, branch: str) -> None:
    proc = _run_git(
        ["git", "-C", str(target_path), "rev-parse", "--verify", branch],
        raise_on_error=False,
    )
    if proc.returncode != 0:
        _run_git(["git", "-C", str(target_path), "checkout", "-b", branch])


def _default_remote_branch(target_path: Path) -> str:
    proc = _run_git(
        ["git", "-C", str(target_path), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        raise_on_error=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        value = proc.stdout.strip()
        if value.startswith("origin/"):
            return value[len("origin/"):]
    cur = _run_git(["git", "-C", str(target_path), "rev-parse", "--abbrev-ref", "HEAD"])
    branch = cur.stdout.strip()
    if branch and branch != "HEAD":
        return branch
    raise IngestionError("Unable to determine repository default branch")


def _run_git(cmd, raise_on_error: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0 and raise_on_error:
        message = _sanitize_git_error(proc.stderr or proc.stdout or "git command failed")
        message = message.strip() or "Failed to prepare repository"
        raise IngestionError(message)
    return proc


def _sanitize_git_error(message: str) -> str:
    out = message
    for key in ("GITHUB_TOKEN", "GITLAB_TOKEN"):
        token = os.environ.get(key, "").strip()
        if token:
            out = out.replace(token, "[REDACTED]")
    return out


def _git_remote_url(repo_path: Path) -> str:
    proc = _run_git(
        ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
        raise_on_error=False,
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


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
