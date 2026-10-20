#!/usr/bin/env bash
# Install the v2-test branch on a fresh VPS without a version-selection prompt.
# A configured bot starts immediately; otherwise complete Guided setup in the panel.
set -euo pipefail
if (( EUID != 0 )); then
  echo 'Run as root (e.g. curl ... | sudo bash).' >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo 'curl is required to fetch the v2-test installer.' >&2
  exit 1
fi
installer="$(mktemp /tmp/mikachan-test-bootstrap.XXXXXXXX)"
trap 'rm -f "$installer"' EXIT
curl --fail --silent --show-error --location \
  https://raw.githubusercontent.com/Dneeel/Mikachan/v2-test/deploy/bootstrap.sh \
  --output "$installer"
# Force v2-test LAST: callers cannot accidentally override it with --branch v2.
bash "$installer" "$@" --branch v2-test
