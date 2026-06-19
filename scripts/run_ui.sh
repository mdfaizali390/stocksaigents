#!/usr/bin/env bash
# Launch the Streamlit UI with the venv's interpreter.
#
# Usage (from repo root):
#   ./scripts/run_ui.sh
#
# Opens http://localhost:8501. Set STREAMLIT_PORT to override.

set -euo pipefail

# Resolve repo root from this script's location so the command works from
# any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VENV_STREAMLIT="$REPO_ROOT/.venv/bin/streamlit"
APP_PATH="$REPO_ROOT/src/ui/streamlit_app.py"
PORT="${STREAMLIT_PORT:-8501}"

if [[ ! -x "$VENV_STREAMLIT" ]]; then
  echo "Error: $VENV_STREAMLIT not found. Did you run:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [[ ! -f "$APP_PATH" ]]; then
  echo "Error: $APP_PATH not found." >&2
  exit 1
fi

cd "$REPO_ROOT"

# Streamlit doesn't put the launch dir on sys.path — it adds the script's
# parent dir (src/ui) instead. Export PYTHONPATH so `from src.…` resolves.
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

exec "$VENV_STREAMLIT" run "$APP_PATH" --server.port "$PORT"
