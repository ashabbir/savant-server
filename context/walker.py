"""File system walker with .gitignore support."""

import logging
from pathlib import Path
from typing import Iterator, Optional

import pathspec

logger = logging.getLogger(__name__)


class FileWalker:
    """Walk files respecting .gitignore and skipping non-human-written files."""

    DEFAULT_SKIP_PATTERNS = {
        "node_modules", ".git", ".venv", "venv", "env", "__pycache__",
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
        ".eslintcache", ".stylelintcache", "tsconfig.tsbuildinfo", ".coverage",
        ".pytest_cache", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "composer.lock", "Gemfile.lock", "Pipfile.lock", "poetry.lock",
        ".env.local", ".env.*.local", "dist", "build", "out",
    }

    def __init__(self, repo_path: Path):
        self.repo_path = Path(repo_path).resolve()
        self.gitignore_spec: Optional[pathspec.PathSpec] = None
        self._load_gitignore()

    def _load_gitignore(self) -> None:
        gitignore_path = self.repo_path / ".gitignore"
        if gitignore_path.exists():
            try:
                with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
                    patterns = f.read().splitlines()
                self.gitignore_spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
            except Exception as e:
                logger.warning(f"Failed to load .gitignore: {e}")

    def _should_skip(self, path: Path) -> bool:
        filename = path.name.lower()

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
            if clean_pattern in path.parts:
                return True

        if self.gitignore_spec:
            try:
                rel_path = path.relative_to(self.repo_path)
                if self.gitignore_spec.match_file(str(rel_path)):
                    return True
            except (ValueError, Exception):
                pass

        return False

    def walk(self) -> Iterator[Path]:
        """Yield all non-skipped file paths relative to repo root."""
        try:
            for item in self.repo_path.rglob("*"):
                if item.is_file() and not self._should_skip(item):
                    yield item.relative_to(self.repo_path)
        except Exception as e:
            logger.error(f"Error walking repository: {e}")

    def get_file_count(self) -> int:
        return sum(1 for _ in self.walk())
