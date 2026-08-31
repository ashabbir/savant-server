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

if [[ -z "${SAVANT_TEST_DATABASE_URL:-}" ]]; then
  echo "SAVANT_TEST_DATABASE_URL is required and must name a dedicated PostgreSQL test database." >&2
  exit 2
fi
export SAVANT_DATABASE_URL="${SAVANT_DATABASE_URL:-$SAVANT_TEST_DATABASE_URL}"
.venv/bin/pip install -q -r requirements-dev.txt
.venv/bin/python -m pytest -v

echo "Server test suite passed."
