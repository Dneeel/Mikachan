#!/usr/bin/env bash
set -euo pipefail
case "${1:---dry-run}" in
  --dry-run)
    echo 'Would create isolated .venv with uv sync --extra dev, run doctor and pytest.'
    echo 'Will not modify system packages, services, users, ports or other projects.'
    ;;
  --install)
    command -v uv >/dev/null
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/mika-uv-cache}" uv sync --extra dev
    uv run mika-nexus doctor
    uv run pytest -q
    ;;
  *) echo 'Usage: setup-ubuntu.sh [--dry-run|--install]' >&2; exit 2;;
esac
