from __future__ import annotations

import os
from pathlib import Path


def _default_data_dir() -> Path:
    in_docker = os.path.isfile("/.dockerenv") or bool(os.environ.get("RUNNING_IN_DOCKER"))
    if in_docker:
        return Path("/data/savant")
    return Path(__file__).resolve().parent / "data"


def get_server_data_dir() -> Path:
    configured = os.environ.get("SAVANT_SERVER_DATA_DIR", "").strip()
    base = Path(configured).expanduser() if configured else _default_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_server_db_path() -> Path:
    explicit = os.environ.get("SAVANT_DB", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
    else:
        p = get_server_data_dir() / "savant.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_server_abilities_base_dir() -> Path:
    explicit = os.environ.get("SAVANT_ABILITIES_DIR", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
    else:
        # AbilityStore expects base/abilities/<personas|rules|...>
        p = get_server_data_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p
