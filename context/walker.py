"""File system walker with .gitignore support."""

import logging
import os
import subprocess
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import pathspec

logger = logging.getLogger(__name__)


class FileWalker:
    """Walk files respecting .gitignore and skipping non-human-written files."""

    DEFAULT_SKIP_PATTERNS = {
        "node_modules", ".git", ".venv", "venv", "env", "__pypackages__", "site-packages",
        "vendor", "vendor_modules", "bower_components", "jspm_packages", ".pnpm-store",
        ".yarn", ".yarn-cache", "Pods", "Carthage", ".gradle", "__pycache__",
        ".pytest_cache", ".tox", "dist", "build", "*.egg-info", ".mypy_cache",
        ".vscode", ".idea", ".DS_Store", "target", "out", ".next", ".nuxt",
        ".cache", "coverage", "tmp_wheels", "tmp_wheels_urls", "tmp",
    }

    SKIP_EXTENSIONS = {
        ".pyc", ".pyo", ".pyd", ".so", ".o", ".a", ".exe", ".dll", ".dylib",
        ".lib", ".class", ".jar", ".jpg", ".jpeg", ".png", ".gif", ".bmp",
        ".svg", ".ico", ".zip", ".whl", ".tar", ".gz", ".rar", ".7z", ".bin",
        ".wasm", ".swf", ".lock", ".min.js", ".min.css", ".min.html", ".map",
        ".d.ts", ".pb.go", ".pb.py", ".pb.js", ".out",
    }

    SKIP_FILENAME_PATTERNS = {
        ".gitignore", ".eslintcache", ".stylelintcache", "tsconfig.tsbuildinfo", ".coverage",
        ".pytest_cache", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "composer.lock", "Gemfile.lock", "Pipfile.lock", "poetry.lock",
        ".env.local", ".env.*.local", "dist", "build", "out",
    }

    def __init__(self, repo_path: Path, *, tracked_only: bool = False):
        self.repo_path = Path(repo_path).resolve()
        self.tracked_only = tracked_only
        self._tracked_paths: Optional[set[str]] = None
        self.gitignore_spec: Optional[pathspec.PathSpec] = None
        self._gitignore_specs: List[Tuple[Path, pathspec.PathSpec]] = []
        self._load_gitignore()

    def _load_gitignore(self) -> None:
        for root, dirs, files in os.walk(str(self.repo_path)):
            dirs[:] = [d for d in dirs if d not in self.DEFAULT_SKIP_PATTERNS]
            if ".gitignore" not in files:
                continue
            gitignore_path = Path(root) / ".gitignore"
            try:
                with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
                    patterns = f.read().splitlines()
                spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
                base = gitignore_path.parent.resolve()
                self._gitignore_specs.append((base, spec))
                if base == self.repo_path:
                    self.gitignore_spec = spec
            except Exception as e:
                logger.warning(f"Failed to load .gitignore {gitignore_path}: {e}")

    def _walk_filesystem(self) -> Iterator[Path]:
        """Yield files by recursively scanning the filesystem, skipping directories early."""
        import os
        for root, dirs, files in os.walk(str(self.repo_path)):
            # Prune directories in-place to avoid descending into them
            dirs[:] = [
                d for d in dirs
                if d not in self.DEFAULT_SKIP_PATTERNS
                and not self._should_skip(Path(root) / d)
            ]
            for file in files:
                abs_path = Path(root) / file
                if not self._should_skip(abs_path):
                    try:
                        yield abs_path.relative_to(self.repo_path)
                    except ValueError:
                        pass

    def _walk_git_repo(self) -> Iterator[Path]:
        """Yield files from git's own ignore-aware file listing."""
        try:
            git_args = [
                "git", "-C", str(self.repo_path), "ls-files", "-z", "--cached",
            ]
            if not self.tracked_only:
                git_args.extend(["--others", "--exclude-standard"])
            proc = subprocess.run(
                git_args,
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "git ls-files failed")

            raw_paths = [raw_path for raw_path in proc.stdout.split("\0") if raw_path]
            ignored_paths = set()
            if raw_paths:
                ignored = subprocess.run(
                    ["git", "-C", str(self.repo_path), "check-ignore", "--no-index", "--stdin", "-z"],
                    input="\0".join(raw_paths),
                    check=False,
                    capture_output=True,
                    text=True,
                )
                ignored_paths = {path for path in ignored.stdout.split("\0") if path}

            for raw_path in raw_paths:
                if not raw_path:
                    continue
                if raw_path in ignored_paths:
                    continue
                rel_path = Path(raw_path)
                abs_path = self.repo_path / rel_path
                if abs_path.is_file() and not self._should_skip(abs_path):
                    yield rel_path
        except Exception as e:
            logger.warning(f"Git-aware walk failed, falling back to filesystem scan: {e}")
            yield from self._walk_filesystem()

    def _should_skip(self, path: Path) -> bool:
        filename = path.name.lower()
        path_parts = {part.lower() for part in path.parts}

        if path.suffix.lower() in self.SKIP_EXTENSIONS:
            return True

        if len(path.suffixes) > 1:
            compound_ext = "".join(path.suffixes).lower()
            if compound_ext in self.SKIP_EXTENSIONS:
                return True

        if filename in self.SKIP_FILENAME_PATTERNS:
            return True

        for pattern in self.DEFAULT_SKIP_PATTERNS:
            clean_pattern = pattern.replace("*.", "").replace("*", "")
            if clean_pattern.lower() in path_parts:
                return True

        try:
            abs_path = path if path.is_absolute() else self.repo_path / path
            for base, spec in self._gitignore_specs:
                try:
                    rel_path = abs_path.relative_to(base)
                except ValueError:
                    continue
                if spec.match_file(str(rel_path)):
                    return True
        except Exception:
            pass

        return False

    def _get_tracked_paths(self) -> set[str]:
        if self._tracked_paths is not None:
            return self._tracked_paths
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.repo_path), "ls-files", "-z", "--cached"],
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                return set()
            self._tracked_paths = {path for path in proc.stdout.split("\0") if path}
        except Exception:
            self._tracked_paths = set()
        return self._tracked_paths

    def is_allowed(self, path: Path | str) -> bool:
        """Return whether a relative path is eligible for source analysis."""
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                candidate = candidate.resolve(strict=False).relative_to(self.repo_path.resolve(strict=False))
            except ValueError:
                return False
        candidate_text = candidate.as_posix()
        if not candidate_text or not (self.repo_path / candidate).is_file():
            return False
        if self._should_skip(candidate):
            return False
        if not self.tracked_only:
            return True
        if not (self.repo_path / ".git").exists():
            return True
        if candidate_text not in self._get_tracked_paths():
            return False
        ignored = subprocess.run(
            ["git", "-C", str(self.repo_path), "check-ignore", "--no-index", "-q", "--", candidate_text],
            check=False,
            capture_output=True,
        )
        return ignored.returncode != 0

    def walk(self) -> Iterator[Path]:
        """Yield all non-skipped file paths relative to repo root."""
        try:
            if (self.repo_path / ".git").exists():
                yield from self._walk_git_repo()
                return

            if self.tracked_only:
                return

            yield from self._walk_filesystem()
        except Exception as e:
            logger.error(f"Error walking repository: {e}")

    def get_file_count(self) -> int:
        return sum(1 for _ in self.walk())
