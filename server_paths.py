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


# --- Host path mapping (container <-> host) ---
_HOST_PATH_MAP = []
for _env_key in ("_VOL_MAP_0", "_VOL_MAP_1", "_VOL_MAP_2", "_VOL_MAP_3", "_VOL_MAP_4", "_VOL_MAP_5"):
    _val = os.environ.get(_env_key, "")
    if ":" in _val:
        _parts = _val.split(":", 1)
        # Store as (container_prefix, host_prefix)
        _HOST_PATH_MAP.append((_parts[1], _parts[0]))
# Sort longest prefixes first for most-specific match
_HOST_PATH_MAP.sort(key=lambda x: -len(x[0]))
_HOST_TO_CONTAINER_MAP = sorted(_HOST_PATH_MAP, key=lambda x: -len(x[1]))


def container_to_host_path(container_path: str) -> str:
    """Map a container absolute path back to the host filesystem path."""
    if not container_path:
        return container_path
    for container_prefix, host_prefix in _HOST_PATH_MAP:
        if container_path.startswith(container_prefix):
            return host_prefix + container_path[len(container_prefix):]
    return container_path


def host_to_container_path(host_path: str) -> str:
    """Map a host absolute path to the container filesystem path."""
    if not host_path:
        return host_path
    for container_prefix, host_prefix in _HOST_TO_CONTAINER_MAP:
        if host_path.startswith(host_prefix):
            return container_prefix + host_path[len(host_prefix):]
    return host_path
