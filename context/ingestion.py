"""Project ingestion helpers for context repository sources."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional
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
    changed: bool = False
    provider: str = "git"


def _get_git_head(repo_path: Path) -> str:
    """Get current HEAD commit hash for a git repository."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


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

    if target_path.exists():
        if not (target_path / ".git").is_dir():
            raise IngestionError(
                f"Target path already exists and is not a git repository: {target_path}"
            )
        _update_checkout(target_path, safe_url, provider, token, branch)
    else:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _clone_checkout(target_path, safe_url, provider, token, branch)

    return IngestedProject(name=slug, path=str(target_path))


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
    _update_checkout(target_path, _normalize_remote_url(parsed), provider, token, branch)
    head_after = _get_git_head(target_path)

    changed = bool(head_before and head_after and head_before != head_after)

    return IngestedProject(name=target_path.name, path=str(target_path), changed=changed, provider=provider)


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


@contextmanager
def _git_auth_environment(provider: str, token: str) -> Iterator[Dict[str, str]]:
    """Provide Git credentials without putting the token in argv or git config."""
    username = "x-access-token" if provider == "github" else "oauth2"
    script_path = None
    try:
        with tempfile.NamedTemporaryFile("w", prefix="savant-askpass-", delete=False) as script:
            script.write(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *[Uu]sername*) printf '%s\\n' \"$SAVANT_GIT_ASKPASS_USERNAME\" ;;\n"
                "  *) printf '%s\\n' \"$SAVANT_GIT_ASKPASS_TOKEN\" ;;\n"
                "esac\n"
            )
            script_path = Path(script.name)
        script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
        yield {
            **os.environ,
            "GIT_ASKPASS": str(script_path),
            "GIT_TERMINAL_PROMPT": "0",
            "SAVANT_GIT_ASKPASS_USERNAME": username,
            "SAVANT_GIT_ASKPASS_TOKEN": token,
        }
    finally:
        if script_path:
            script_path.unlink(missing_ok=True)


def _clone_checkout(target_path: Path, safe_url: str, provider: str, token: str, branch: Optional[str]) -> None:
    cmd = ["git", "clone"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([safe_url, str(target_path)])
    with _git_auth_environment(provider, token) as env:
        _run_git(cmd, env=env)
        _run_git(["git", "-C", str(target_path), "remote", "set-url", "origin", safe_url], env=env)


def _update_checkout(target_path: Path, safe_url: str, provider: str, token: str, branch: Optional[str]) -> None:
    lock_file = target_path / ".git" / "index.lock"
    if lock_file.exists():
        try:
            lock_file.unlink()
        except Exception:
            pass
    with _git_auth_environment(provider, token) as env:
        _run_git(["git", "-C", str(target_path), "remote", "set-url", "origin", safe_url], env=env)
        try:
            _run_git(["git", "-C", str(target_path), "fetch", "origin", "--prune"], env=env)
            if branch:
                _ensure_branch_exists(target_path, branch, env=env)
                _run_git(["git", "-C", str(target_path), "checkout", branch], env=env)
                _run_git(["git", "-C", str(target_path), "pull", "origin", branch], env=env)
            else:
                default_branch = _default_remote_branch(target_path, env=env)
                _run_git(["git", "-C", str(target_path), "checkout", default_branch], env=env)
                _run_git(["git", "-C", str(target_path), "pull", "origin", default_branch], env=env)
        finally:
            _run_git(["git", "-C", str(target_path), "remote", "set-url", "origin", safe_url], env=env)


def _ensure_branch_exists(target_path: Path, branch: str, env=None) -> None:
    proc = _run_git(
        ["git", "-C", str(target_path), "show-ref", "--verify", f"refs/remotes/origin/{branch}"],
        raise_on_error=False, env=env,
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


def _default_remote_branch(target_path: Path, env=None) -> str:
    proc = _run_git(
        ["git", "-C", str(target_path), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        raise_on_error=False, env=env,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        value = proc.stdout.strip()
        if value.startswith("origin/"):
            return value[len("origin/"):]
    cur = _run_git(["git", "-C", str(target_path), "rev-parse", "--abbrev-ref", "HEAD"], env=env)
    branch = cur.stdout.strip()
    if branch and branch != "HEAD":
        return branch
    raise IngestionError("Unable to determine repository default branch")


def _run_git(cmd, raise_on_error: bool = True, env=None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
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
