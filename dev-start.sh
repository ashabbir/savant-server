#!/bin/bash
# Savant Server Dev Start Script

# Exit on any error
set -e

# Change to the server directory
cd "$(dirname "$0")"

# Ensure pip and tempfile have writable local directories in local runs.
export SAVANT_LOCAL_TMP_DIR="${SAVANT_LOCAL_TMP_DIR:-$PWD/.tmp}"
export TMPDIR="$SAVANT_LOCAL_TMP_DIR"
export TEMP="$SAVANT_LOCAL_TMP_DIR"
export TMP="$SAVANT_LOCAL_TMP_DIR"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$SAVANT_LOCAL_TMP_DIR/pip-cache}"
export BASE_CODE_DIR="~/code"
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"

# Create .venv if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate virtual environment
source .venv/bin/activate

# Install dependencies (prefer dev requirements if available)
if [ -f "requirements-dev.txt" ]; then
    echo "Installing dev dependencies..."
    pip install -r requirements-dev.txt
else
    echo "Installing standard dependencies..."
    pip install -r requirements.txt
fi

# Start the Flask Server
echo "Starting Savant Flask Server..."
python app.py
