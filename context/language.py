"""Memory bank and language detection for files."""

from pathlib import Path
from typing import Tuple


class MemoryBankDetector:
    """Detects if a file is part of a memory bank collection or repository documentation."""

    MEMORY_DIR_NAMES = {"memory", "memorybank", "bank", "docs", "documentation", "spec", "specs", "doc"}
    MARKDOWN_EXTS = {".md", ".mdx", ".markdown"}

    @staticmethod
    def normalize_path_segment(segment: str) -> str:
        return segment.lower().replace("-", "").replace("_", "")

    @classmethod
    def is_in_memory_dir(cls, rel_path: str) -> bool:
        segments = rel_path.lower().split("/")
        normalized = [cls.normalize_path_segment(seg) for seg in segments]
        return any(seg in cls.MEMORY_DIR_NAMES for seg in normalized)

    @classmethod
    def is_markdown(cls, rel_path: str) -> bool:
        return Path(rel_path).suffix.lower() in cls.MARKDOWN_EXTS

    @classmethod
    def is_memory_bank_file(cls, rel_path: str) -> bool:
        # Markdown is repository memory/documentation regardless of where it
        # lives. Keeping nested Markdown in the language bucket made the
        # project summary contradict the memory-bank browser.
        return cls.is_markdown(rel_path)

    @classmethod
    def should_skip_in_memory_dir(cls, rel_path: str) -> bool:
        return cls.is_in_memory_dir(rel_path) and not cls.is_markdown(rel_path)

    @classmethod
    def detect_language(cls, rel_path: str) -> Tuple[str, bool]:
        """Return (language_tag, is_memory_bank)."""
        if cls.is_memory_bank_file(rel_path):
            return ("memory_bank", True)
        ext = Path(rel_path).suffix.lower().lstrip(".")
        return (ext if ext else "txt", False)
