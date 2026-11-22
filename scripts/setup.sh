#!/usr/bin/env bash
# One-command local environment bootstrap for Yami Learning Companion.
# Usage:
#   ./scripts/setup.sh            # SQLite local mode (fastest)
#   ./scripts/setup.sh --docker   # Also start PostgreSQL 16 + Neo4j 5 via Docker Compose

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> [1/4] Checking Python..."
PY_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Please install Python 3.9+ first."
  exit 1
fi
"$PY_BIN" --version

echo "==> [2/4] Creating virtual environment (.venv) & installing dependencies..."
if [[ ! -d ".venv" ]]; then
  "$PY_BIN" -m venv .venv
fi
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -e ".[dev]"

echo "==> [3/4] Preparing .env configuration..."
if [[ ! -f ".env" ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — please fill in your API keys (Doubao Seed / DeepSeek)."
else
  echo ".env already exists (kept untouched)."
fi

if [[ "${1:-}" == "--docker" ]]; then
  echo "==> [3b] Starting Docker PostgreSQL 16 + Neo4j 5..."
  docker compose up -d
fi

echo "==> [4/4] Running smoke tests..."
.venv/bin/pytest -q

echo ""
echo "✅ Setup complete!"
echo "Next steps:"
echo "  1. Edit .env with your Doubao Seed / DeepSeek keys"
echo "  2. Start all services:  ./scripts/yami_up.sh start"
echo "  3. Open Guardian Web:   http://127.0.0.1:8000/guardian"
echo "  4. Flash ESP-Mosaico:   ./scripts/flash_mosaico.sh"
