"""Built-in Savant skill installation and protection helpers."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from .skills_shared import SKILLS_DIR

logger = logging.getLogger(__name__)

DEFAULT_SKILL_IDS = frozenset({
    "savant-session-workspace",
    "savant-knowledge-commit",
    "savant-code-analysis",
})

_DEFAULT_SKILLS_SOURCE = Path(__file__).resolve().parents[1] / "data" / "default_skills"


def is_default_skill(skill_id: str) -> bool:
    """Return whether ``skill_id`` identifies a server-managed built-in skill."""
    return skill_id in DEFAULT_SKILL_IDS


def ensure_default_skills() -> dict:
    """Install any missing built-in skills without touching user-managed files.

    The source bundle ships in the server image while ``SKILLS_DIR`` normally
    lives on a persistent volume. Reconcile each default independently so an
    existing user skill never prevents a missing Savant skill from returning.
    """
    if not _DEFAULT_SKILLS_SOURCE.exists():
        logger.error("Built-in skills source is missing: %s", _DEFAULT_SKILLS_SOURCE)
        return {"installed": [], "reason": "seed-missing"}

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for skill_id in sorted(DEFAULT_SKILL_IDS):
        source = _DEFAULT_SKILLS_SOURCE / skill_id
        target = SKILLS_DIR / skill_id
        if not source.is_dir():
            logger.error("Built-in skill source is missing: %s", source)
            continue
        if target.exists():
            continue
        shutil.copytree(source, target)
        installed.append(skill_id)

    if installed:
        logger.info("Installed built-in Savant skills: %s", ", ".join(installed))
    return {"installed": installed, "reason": "ok"}


def default_skill_metadata(skill_id: str) -> dict:
    """Read the bundled metadata used when a protected skill is listed."""
    metadata_path = _DEFAULT_SKILLS_SOURCE / skill_id / "metadata.json"
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
