#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

export SAVANT_LOCAL_TMP_DIR="${SAVANT_LOCAL_TMP_DIR:-$ROOT_DIR/.tmp}"
export TMPDIR="$SAVANT_LOCAL_TMP_DIR"
export TEMP="$SAVANT_LOCAL_TMP_DIR"
export TMP="$SAVANT_LOCAL_TMP_DIR"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$SAVANT_LOCAL_TMP_DIR/pip-cache}"
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"

if [[ ! -d ".venv" ]]; then
  python3.11 -m venv .venv
fi

.venv/bin/pip install -q -r requirements-dev.txt
.venv/bin/python -m pytest -v

echo "Server test suite passed."
