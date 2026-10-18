#!/usr/bin/env bash
set -euo pipefail
case "${1:---dry-run}" in
  --dry-run)
    echo 'Would create isolated .venv with uv sync --extra dev, run doctor and unit tests.'
    echo 'Will not modify system packages, services, users, ports or other projects.'
    ;;
  --install)
    command -v uv >/dev/null
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/mikachan-uv-cache}" uv sync --extra dev
    uv run mikachan doctor
    uv run python -m unittest discover -s tests -v
    ;;
  *) echo 'Usage: setup-ubuntu.sh [--dry-run|--install]' >&2; exit 2;;
esac
