"""Read the server build metadata used by version and health endpoints."""

import json
from pathlib import Path


BUILD_INFO_PATH = Path(__file__).resolve().parent / "build-info.json"
DEFAULT_BUILD_INFO = {
    "version": "unknown",
    "branch": "unknown",
    "commit": "unknown",
    "built_at": "unknown",
}


def get_build_info() -> dict:
    try:
        payload = json.loads(BUILD_INFO_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_BUILD_INFO)
    return {**DEFAULT_BUILD_INFO, **payload}
