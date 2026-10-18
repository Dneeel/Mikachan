#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(dirname "$script_dir")"
install_root="$(dirname "$project_dir")"
panel_venv="$install_root/panel-venv"
requirements="$script_dir/manager-requirements.txt"

if (( EUID != 0 )); then
  echo "Run the panel with sudo: sudo bash $script_dir/manage.sh" >&2
  exit 1
fi

# Read-only commands work even when Textual, uv, or the bot environment is broken.
case "${1:-}" in
  status|preflight|diagnostics|logs|config|backups|queue|storage)
    exec python3 "$script_dir/manager.py" "$@"
    ;;
esac

if ! command -v python3 >/dev/null; then
  echo 'Python 3 is required. Install python3 and python3-venv first.' >&2
  exit 1
fi
umask 077
mkdir -p "$install_root/tmp/panel" "$install_root/cache/pip"
export TMPDIR="$install_root/tmp/panel"
export PIP_CACHE_DIR="$install_root/cache/pip"
export XDG_CACHE_HOME="$install_root/cache"
if [[ -f "$install_root/config/ssh/config" ]]; then
  export GIT_SSH_COMMAND="ssh -F $install_root/config/ssh/config"
fi
if [[ ! -x "$panel_venv/bin/python" ]]; then
  if ! python3 -m venv "$panel_venv"; then
    echo 'Could not create manager venv. Install python3-venv and retry.' >&2
    exit 1
  fi
fi
if [[ ! -x "$panel_venv/bin/uv" ]] || ! cmp -s "$requirements" "$panel_venv/.requirements"; then
  "$panel_venv/bin/python" -m pip install --disable-pip-version-check -r "$requirements"
  cp "$requirements" "$panel_venv/.requirements"
fi
export PATH="$panel_venv/bin:$PATH"
if [[ $# -eq 0 ]]; then
  if [[ ! -t 0 || ! -t 1 ]]; then
    echo 'Interactive panel needs a terminal; use a CLI subcommand instead.' >&2
    exit 1
  fi
  cd "$project_dir"
  exec "$panel_venv/bin/python" -m deploy.manager_ui
fi
exec "$panel_venv/bin/python" "$script_dir/manager.py" "$@"
