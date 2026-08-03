#!/usr/bin/env bash
# Install all Python dependencies for Legal OCR backend (Linux server).
# Usage (from repo):  bash backend/scripts/install_server.sh
# Or from backend/:   bash scripts/install_server.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Legal OCR backend install (root: $ROOT)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.10+ first."
  exit 1
fi

PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "==> Python $PYVER"

if [ ! -d ".venv" ]; then
  echo "==> Creating virtualenv .venv"
  python3 -m venv .venv
fi

VENV_PY=".venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: $VENV_PY not found"
  exit 1
fi

echo "==> Using venv Python: $("$VENV_PY" -c 'import sys; print(sys.executable)')"
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Upgrading pip, wheel, setuptools"
"$VENV_PY" -m pip install --upgrade pip wheel "setuptools>=65.0.0,<82"

echo "==> Installing requirements.txt"
"$VENV_PY" -m pip install -r requirements.txt

echo "==> Installing memory_management (editable)"
"$VENV_PY" -m pip install -e ./memory_management

echo "==> spaCy model for Mem0 NLP (optional but recommended for /chat)"
if "$VENV_PY" -m spacy download en_core_web_sm; then
  echo "    en_core_web_sm installed"
else
  echo "    WARNING: spaCy model download failed; run: $VENV_PY -m spacy download en_core_web_sm"
fi

echo "==> Creating data folders"
mkdir -p docs output logs ocr_cache query_results

echo "==> Verifying imports"
"$VENV_PY" scripts/verify_install.py
echo ""
echo "SUCCESS. Next steps:"
echo "  1. Copy backend/.env.example to backend/.env and set API keys"
echo "  2. source .venv/bin/activate"
echo "  3. python run_api.py"
echo "  API docs: http://<server>:8000/docs"
