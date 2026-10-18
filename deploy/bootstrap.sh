#!/usr/bin/env bash
# One-command installer for a fresh Debian/Ubuntu systemd server.
set -euo pipefail

repo_url='https://github.com/Dneeel/Mikachan.git'
source_dir=''
while (($#)); do
  case "$1" in
    --repo-url) repo_url="${2:?missing repository URL}"; shift 2 ;;
    --source) source_dir="${2:?missing source directory}"; shift 2 ;;
    *) echo "Unknown installer argument: $1" >&2; exit 2 ;;
  esac
done
if (( EUID != 0 )); then
  echo 'Run with sudo or as root.' >&2
  exit 1
fi
if [[ ! -d /run/systemd/system ]]; then
  echo 'Mikachan needs a Debian/Ubuntu server with systemd as PID 1.' >&2
  exit 1
fi
if [[ ! -t 1 || ! -r /dev/tty ]]; then
  echo 'Run from an interactive SSH terminal; setup opens a terminal panel.' >&2
  exit 1
fi
if [[ -e /etc/systemd/system/mikachan.service && ! -e /opt/mikachan/.managed-by-mikachan ]]; then
  echo 'An unmanaged Mikachan service exists. Back it up and migrate it before installation.' >&2
  exit 1
fi
printf '\n◈ Mikachan installer · preparing host tools\n'
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git ca-certificates python3 python3-venv ffmpeg tesseract-ocr tesseract-ocr-fas libcairo2

panel_root='/usr/local/lib/mikachan-panel'
mkdir -p "$panel_root/deploy"
if [[ -z "$source_dir" ]]; then
  temp_dir="$(mktemp -d /tmp/mikachan-bootstrap.XXXXXX)"
  trap 'rm -rf "$temp_dir"' EXIT
  git clone --depth 1 --branch v2 "$repo_url" "$temp_dir/repo"
  source_dir="$temp_dir/repo"
fi
if [[ ! -f "$source_dir/deploy/manager.py" || ! -f "$source_dir/deploy/manage.sh" ]]; then
  echo 'Installer source does not contain the Mikachan release manager.' >&2
  exit 1
fi
install -m 0644 "$source_dir/deploy/__init__.py" "$panel_root/deploy/__init__.py"
install -m 0644 "$source_dir/deploy/manager.py" "$panel_root/deploy/manager.py"
install -m 0644 "$source_dir/deploy/manager_ui.py" "$panel_root/deploy/manager_ui.py"
install -m 0644 "$source_dir/deploy/manager-requirements.txt" "$panel_root/deploy/manager-requirements.txt"
install -m 0755 "$source_dir/deploy/manage.sh" "$panel_root/deploy/manage.sh"
printf 'Managed by Mikachan bootstrap\n' > "$panel_root/.managed-by-mikachan"
chmod 0600 "$panel_root/.managed-by-mikachan"
python3 - "$repo_url" <<'PY'
import json, os, sys
path = '/etc/mikachan-manager.json'
with open(path + '.new', 'w', encoding='utf-8') as output:
    json.dump({'repo_url': sys.argv[1]}, output)
    output.write('\n')
os.chmod(path + '.new', 0o600)
os.replace(path + '.new', path)
PY
cat > /usr/local/bin/mikachan <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
if (( EUID != 0 )); then
  exec sudo bash /usr/local/lib/mikachan-panel/deploy/manage.sh "$@"
fi
exec bash /usr/local/lib/mikachan-panel/deploy/manage.sh "$@"
WRAPPER
chmod 0755 /usr/local/bin/mikachan
printf '\n◈ Installing verified v2 release\n'
if [[ ! -f /opt/mikachan/manifest.json ]]; then
  mikachan install --code-only
else
  echo 'Existing managed installation detected; opening panel without reinstalling.'
fi
printf '\n◈ Opening Mikachan panel · choose Guided setup to connect Telegram\n'
mikachan </dev/tty >/dev/tty
