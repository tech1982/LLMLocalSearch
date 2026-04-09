#!/bin/bash
# Native runner — no Docker required.
# Requires: Ollama running natively (brew services start ollama)

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/.venv"

# Create venv on first run
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
    echo "Installing dependencies..."
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -r requirements.txt
    echo "Done."
fi

# Check Ollama is running
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "Ollama is not running. Start it with: brew services start ollama"
    exit 1
fi

mkdir -p data sessions

source "$VENV/bin/activate"
exec streamlit run src/app.py
