"""Helpers for durable repository activity audit details."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict

from .indexer import get_git_diff_files


def collect_git_change_details(
    repo_path: Path, before_commit: str = "", after_commit: str = ""
) -> Dict[str, Any]:
    """Return file lists, line statistics, and commit subject for an activity."""
    if not repo_path.exists() or not (repo_path / ".git").exists():
        return {
            "commit_subject": "",
            "files_changed": {"added": [], "modified": [], "deleted": []},
            "change_stats": {
                "files_added": 0, "files_modified": 0, "files_deleted": 0,
                "files_total": 0, "insertions": 0, "deletions": 0,
            },
        }
    added, modified, deleted = get_git_diff_files(
        repo_path, before_commit or None, after_commit or None
    )
    stats: Dict[str, Any] = {
        "files_added": len(added),
        "files_modified": len(modified),
        "files_deleted": len(deleted),
        "files_total": len(added) + len(modified) + len(deleted),
        "insertions": 0,
        "deletions": 0,
    }
    subject = ""
    if before_commit and after_commit and before_commit != after_commit:
        diff = subprocess.run(
            ["git", "diff", "--shortstat", before_commit, after_commit],
            cwd=str(repo_path), capture_output=True, text=True, check=False,
        ).stdout
        for value, label in __import__("re").findall(
            r"(\d+) (file|insertion|deletion)s?", diff
        ):
            if label == "insertion":
                stats["insertions"] = int(value)
            elif label == "deletion":
                stats["deletions"] = int(value)
        subject = subprocess.run(
            ["git", "show", "-s", "--format=%s", after_commit],
            cwd=str(repo_path), capture_output=True, text=True, check=False,
        ).stdout.strip()
    return {
        "commit_subject": subject,
        "files_changed": {"added": added, "modified": modified, "deleted": deleted},
        "change_stats": stats,
    }
