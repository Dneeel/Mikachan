#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
umask 077
mkdir -p data
chmod 700 data
export PATH="$project_dir/.venv/bin:$PATH"

# One polling consumer per bot identity on this workspace.
exec flock -n data/polling.lock "$project_dir/.venv/bin/mika-nexus" run
