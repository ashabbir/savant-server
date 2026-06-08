from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from server_paths import get_server_abilities_base_dir

logger = logging.getLogger(__name__)


_EMBEDDED_SEED_FILES = {
    "personas/engineer.md": """---
id: persona.engineer
type: persona
tags: [engineering]
priority: 100
---
You are Savant's default engineering persona.
Focus on correctness, practical implementation, and clear explanations.
""",
    "personas/architect.md": """---
id: persona.architect
type: persona
tags: [architecture, systems]
priority: 100
---
You are Savant's architecture persona.
Focus on explicit boundaries, predictable operations, and safe evolution.
""",
    "personas/product.md": """---
id: persona.product
type: persona
tags: [product]
priority: 80
---
You are Savant's product persona.
Focus on user value, workflows, and simple defaults.
""",
    "rules/boundaries.md": """---
id: rule.boundaries
type: rule
tags: [architecture, boundaries]
priority: 90
---
Keep ownership boundaries explicit between client and server modules.
""",
    "rules/backend_api.md": """---
id: rule.backend.api
type: rule
tags: [backend, api]
priority: 90
---
Keep server APIs explicit, validated, and secure.
Prefer small endpoints and clear error messages.
""",
    "rules/delivery.md": """---
id: rule.delivery
type: rule
tags: [delivery, engineering]
priority: 80
---
Ship in small, testable increments with clear acceptance criteria.
""",
    "rules/frontend_ui.md": """---
id: rule.frontend.ui
type: rule
tags: [frontend, ui]
priority: 90
---
Keep the client UI simple, responsive, and stateful only where needed.
Preserve clear loading and error states.
""",
    "policies/style/concise.md": """---
id: policy.style.concise
type: style
tags: [style, communication]
priority: 60
---
Use concise, direct communication with explicit assumptions and actionable steps.
""",
    "policies/savant_standard.md": """---
id: policy.savant.standard
type: policy
tags: [savant, standard]
priority: 100
---
Follow Savant architecture boundaries.
Client owns UI/runtime; server owns API, MCP, and shared data.
""",
    "repos/default.md": """---
id: repo.default
type: repo
tags: [default]
priority: 10
---
Default repo overlay for Savant.
Use the active repo context and keep implementation practical.
""",
}

_EMBEDDED_SEED_CACHE: Path | None = None


def _resolve_seed_base() -> Path:
    explicit = os.environ.get("SAVANT_ABILITIES_SEED_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    # Server-local seed location (important for server-only runtime/container builds).
    server_data_seed = Path(__file__).resolve().parents[1] / "data" / "abilities"
    if server_data_seed.exists() and any(server_data_seed.rglob("*.md")):
        return server_data_seed

    return _materialize_embedded_seed_base()


def _seed_root(base: Path) -> Path:
    # Seed bundle may be provided either as <seed>/abilities/... or directly as <seed>/...
    return base / "abilities" if (base / "abilities").exists() else base


def _materialize_embedded_seed_base() -> Path:
    global _EMBEDDED_SEED_CACHE
    if _EMBEDDED_SEED_CACHE and _EMBEDDED_SEED_CACHE.exists():
        return _EMBEDDED_SEED_CACHE

    seed_base = Path(tempfile.gettempdir()) / "savant-abilities-seed"
    seed_root = seed_base / "abilities"
    for rel_path, content in _EMBEDDED_SEED_FILES.items():
        dst = seed_root / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            dst.write_text(content, encoding="utf-8")

    _EMBEDDED_SEED_CACHE = seed_base
    logger.info("Materialized embedded abilities seed bundle at %s", seed_base)
    return seed_base


def _target_root() -> Path:
    return get_server_abilities_base_dir() / "abilities"


def _asset_dirs() -> tuple[str, ...]:
    # Canonical ability folders. Seeding decisions are based only on these assets.
    return ("personas", "rules", "policies", "repos", "styles")


def _iter_asset_files(target_root: Path):
    for dirname in _asset_dirs():
        base = target_root / dirname
        if not base.exists():
            continue
        yield from base.rglob("*.md")


def abilities_asset_count() -> int:
    target_root = _target_root()
    if not target_root.exists():
        return 0
    return sum(1 for _ in _iter_asset_files(target_root))


def _target_has_any_files(target_root: Path) -> bool:
    if not target_root.exists():
        return False
    return any(p.is_file() for p in target_root.rglob("*"))


def abilities_bootstrap_status() -> dict:
    seed_base = _resolve_seed_base()
    seed_root = _seed_root(seed_base)
    count = abilities_asset_count()
    seed_exists = seed_root.exists()
    has_files = _target_has_any_files(_target_root())
    return {
        "asset_count": count,
        "bootstrap_available": count == 0,
        "seed_path": str(seed_root),
        "seed_exists": seed_exists,
        "store_has_files": has_files,
    }


def seed_abilities_if_missing() -> dict:
    target_root = _target_root()
    target_root.mkdir(parents=True, exist_ok=True)

    seed_base = _resolve_seed_base()
    seed_root = _seed_root(seed_base)
    if not seed_root.exists():
        logger.warning("Abilities seed source missing: %s", seed_root)
        return {"seeded": False, "reason": "seed-missing", "seed_path": str(seed_root)}

    if _target_has_any_files(target_root):
        return {
            "seeded": False,
            "reason": "already-populated",
            "seed_path": str(seed_root),
            "target_path": str(target_root),
            "count": abilities_asset_count(),
        }

    copied = 0
    for src in seed_root.rglob("*.md"):
        rel = src.relative_to(seed_root)
        dst = target_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    logger.info("Seeded abilities from %s to %s (%d files)", seed_root, target_root, copied)
    return {
        "seeded": True,
        "reason": "reset-from-seed",
        "seed_path": str(seed_root),
        "target_path": str(target_root),
        "count": copied,
    }
