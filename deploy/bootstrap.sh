#!/usr/bin/env bash
# Interactive, version-pinned installer for a fresh Debian/Ubuntu systemd host.
# Never overwrites an existing Mikachan installation.
set -euo pipefail

repo_url='https://github.com/Dneeel/Mikachan.git'
source_dir=''
selected_branch=''
selected_commit=''
config_file=''
list_only=false
resume_partial=false
resume_bootstrap=false

usage() {
  cat <<'HELP'
Usage: bootstrap.sh [--branch EXISTING_BRANCH] [--commit FULL_40_CHAR_SHA]
                    [--config /private/bot.env] [--repo-url GIT_URL]
                    [--source CHECKOUT_DIR] [--list-versions]
Without --branch, the installer asks you to choose a remote version.
--config installs and starts the service; otherwise guided Setup starts it later.
Existing /opt/Mikachan installations are NEVER overwritten.
HELP
}

while (($#)); do
  case "$1" in
    --repo-url|--source|--branch|--commit|--config)
      (($# >= 2)) || { echo "Missing value for $1" >&2; exit 2; }
      case "$1" in
        --repo-url) repo_url="$2" ;;
        --source) source_dir="$2" ;;
        --branch) selected_branch="$2" ;;
        --commit) selected_commit="$2" ;;
        --config) config_file="$2" ;;
      esac
      shift 2 ;;
    --list-versions) list_only=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown installer argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

valid_branch() {
  [[ -n "$1" && "$1" != -* ]] && git check-ref-format --branch "$1" >/dev/null 2>&1
}

if [[ -n "$selected_branch" ]] && ! valid_branch "$selected_branch"; then
  echo 'Invalid Git branch name.' >&2
  exit 2
fi
if [[ -n "$selected_commit" && ! "$selected_commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Commit must be an exact 40-character lowercase SHA.' >&2
  exit 2
fi
if [[ -n "$selected_commit" && -z "$selected_branch" ]]; then
  echo '--commit requires an explicit --branch.' >&2
  exit 2
fi
if [[ -n "$config_file" && ( ! -f "$config_file" || -L "$config_file" ) ]]; then
  echo 'Configuration must be a regular file, not a symlink.' >&2
  exit 2
fi

# Listing is read-only, usable without sudo or systemd.
if ! $list_only; then
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
  install_root='/opt/Mikachan'
  # A completed installation is always managed through its existing panel.
  if [[ -e "$install_root/manifest.json" || -e /opt/mikachan/manifest.json ||
        -e /var/lib/mikachan/mikachan.sqlite ]]; then
    echo 'Existing Mikachan installation detected. No files changed.' >&2
    echo 'Open the existing mikachan manager for updates; bootstrap will not replace it.' >&2
    exit 1
  fi
  if [[ -L "$install_root" ]]; then
    echo 'Installation root is a symlink; refusing to proceed.' >&2
    exit 1
  fi
  if [[ -e "$install_root/.managed-by-mikachan" ]]; then
    if [[ -L "$install_root/.managed-by-mikachan" ||
          -L "$install_root/panel" || -L "$install_root/panel/deploy" ||
          "$(cat "$install_root/.managed-by-mikachan")" != 'Mikachan manager schema 1' ||
          -L "$install_root/panel/deploy/manager.py" ||
          ! -f "$install_root/panel/deploy/manager.py" ||
          -L /etc/systemd/system/mikachan.service ||
          ( -e /etc/systemd/system/mikachan.service &&
            "$(head -n 1 /etc/systemd/system/mikachan.service)" != '# Managed by Mikachan panel' ) ]]; then
      echo 'Managed installation is incomplete but its ownership cannot be verified.' >&2
      exit 1
    fi
    for managed_path in "$install_root" "$install_root/.managed-by-mikachan" \
        "$install_root/panel" "$install_root/panel/deploy" \
        "$install_root/panel/deploy/manager.py"; do
      managed_mode="$(stat -c %a "$managed_path")"
      if [[ "$(stat -c %u "$managed_path")" != 0 ]] ||
          (( (8#$managed_mode & 8#22) != 0 )); then
        echo 'Interrupted manager files are not exclusively root-controlled; manual inspection required.' >&2
        exit 1
      fi
    done
    printf 'An earlier Mikachan installation was interrupted. State and backups are preserved.\n' >/dev/tty
    while :; do
      printf '  1) Resume Installation\n  2) Review Previous Steps\n  3) Cancel\nChoose: ' >/dev/tty
      IFS= read -r recovery_choice </dev/tty || exit 1
      case "$recovery_choice" in
        1) resume_partial=true; break ;;
        2) python3 "$install_root/panel/deploy/manager.py" status >/dev/tty || true ;;
        3) exit 0 ;;
        *) printf 'Choose 1, 2 or 3.\n' >/dev/tty ;;
      esac
    done
  elif [[ -e "$install_root/.bootstrap-in-progress" ]]; then
    bootstrap_root_mode="$(stat -c %a "$install_root")"
    bootstrap_marker_mode="$(stat -c %a "$install_root/.bootstrap-in-progress")"
    if [[ -L "$install_root/.bootstrap-in-progress" ||
          "$(cat "$install_root/.bootstrap-in-progress")" != 'Mikachan bootstrap schema 1' ||
          -e /etc/systemd/system/mikachan.service ||
          "$(stat -c %u "$install_root")" != 0 ||
          "$(stat -c %u "$install_root/.bootstrap-in-progress")" != 0 ]] ||
          (( (8#$bootstrap_root_mode & 8#22) != 0 ||
             (8#$bootstrap_marker_mode & 8#22) != 0 )); then
      echo 'Incomplete bootstrap cannot be safely identified; manual inspection required.' >&2
      exit 1
    fi
    for managed_path in "$install_root/panel" "$install_root/tmp" \
        "$install_root/cache" "$install_root/config" "$install_root/assets"; do
      if [[ -L "$managed_path" ]]; then
        echo 'Interrupted bootstrap contains a linked managed directory; refusing to overwrite it.' >&2
        exit 1
      fi
    done
    if [[ -d "$install_root/panel" &&
          -n "$(find "$install_root/panel" -type l -print -quit)" ]]; then
      echo 'Interrupted panel contains a linked file; manual inspection required.' >&2
      exit 1
    fi
    printf 'Earlier bootstrap staging was interrupted. No running service was changed.\n' >/dev/tty
    while :; do
      printf '  1) Resume Installation\n  2) Review Previous Steps\n  3) Cancel\nChoose: ' >/dev/tty
      IFS= read -r recovery_choice </dev/tty || exit 1
      case "$recovery_choice" in
        1) resume_bootstrap=true; break ;;
        2) find "$install_root" -mindepth 1 -maxdepth 2 -type f -printf '%P\n' >/dev/tty ;;
        3) exit 0 ;;
        *) printf 'Choose 1, 2 or 3.\n' >/dev/tty ;;
      esac
    done
  elif [[ -e /etc/systemd/system/mikachan.service ]]; then
    echo 'An unmanaged Mikachan service unit exists; refusing to replace it.' >&2
    exit 1
  elif [[ -d "$install_root" && -n "$(find "$install_root" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    # Private deploy-key staging is the only permitted preexisting root content.
    staged="$(realpath -m "$source_dir")"
    if [[ -z "$source_dir" || "$staged" != "$install_root/tmp/bootstrap/"* ]] || \
       [[ -n "$(find "$install_root" -mindepth 1 -maxdepth 1 ! -name config ! -name tmp -print -quit)" ]] || \
       [[ -n "$(find "$install_root/config" -mindepth 1 -maxdepth 1 ! -name ssh -print -quit)" ]] || \
       [[ -n "$(find "$install_root/tmp" -mindepth 1 -maxdepth 1 ! -name bootstrap -print -quit)" ]]; then
      echo 'Installation root contains unmanaged files; refusing to overwrite them.' >&2
      exit 1
    fi
  fi
fi

if ! $list_only; then
  printf '\nWelcome to Mikachan installation\n\nRead-only server preflight\n'
  printf '  CPUs: %s · available RAM: %s MiB · free /opt disk: %s GiB\n' \
    "$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '?')" \
    "$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)" \
    "$(df -Pk /opt | awk 'NR==2 {print int($4/1048576)}')"
  printf '  Mikachan: /opt/Mikachan · optional independent n8n: /opt/n8n\n'
  printf '  n8n needs at least 3 GB installed RAM, 1.2 GB available RAM, and 6 GB free disk.\n'
  printf '  Existing Docker, containers, ports, DNS and firewall are checked before n8n changes.\n'
  if command -v python3 >/dev/null 2>&1 && ! python3 -c \
      'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
    echo 'Mikachan v2 requires Python 3.12+. This host Python is older; no packages changed.' >&2
    exit 1
  fi
  if pgrep -f '(^|/)(apt-get|dpkg|unattended-upgrade)( |$)' >/dev/null 2>&1; then
    echo 'A package-manager operation is active; retry installation later.' >&2
    exit 1
  fi
fi

safe_apt_install() {
  local simulation
  simulation="$(apt-get -s install --no-install-recommends "$@")"
  if printf '%s\n' "$simulation" | grep -q '^Remv '; then
    echo 'Package installation would remove existing software; cancelled.' >&2
    exit 1
  fi
  NEEDRESTART_MODE=l apt-get install -y --no-install-recommends "$@"
}

if ! command -v git >/dev/null 2>&1; then
  if $list_only; then
    echo 'Git is required to list remote versions.' >&2
    exit 1
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  safe_apt_install git ca-certificates
fi

# Read the actual remote branches. No implicit 'latest' or default version.
if ! remote_refs="$(git ls-remote --heads "$repo_url")"; then
  echo 'Could not query GitHub branches; installation cancelled.' >&2
  exit 1
fi
versions=()
while read -r sha ref; do
  [[ "$sha" =~ ^[0-9a-f]{40}$ && "$ref" == refs/heads/* ]] || continue
  name="${ref#refs/heads/}"
  if valid_branch "$name"; then versions+=("$name"); fi
done <<< "$remote_refs"
if ((${#versions[@]} == 0)); then
  echo 'No valid branches found; installation cancelled.' >&2
  exit 1
fi
mapfile -t versions < <(printf '%s\n' "${versions[@]}" | sort -Vu)
if $list_only; then
  printf '%s\n' "${versions[@]}"
  exit 0
fi

if [[ -n "$selected_branch" ]]; then
  found=false
  for version in "${versions[@]}"; do
    if [[ "$version" == "$selected_branch" ]]; then found=true; break; fi
  done
  if ! $found; then
    echo "Requested version $selected_branch does not exist on the remote. No files changed." >&2
    exit 1
  fi
else
  printf '\nMikachan: choose a branch to install (no automatic selection)\n' >/dev/tty
  for index in "${!versions[@]}"; do
    printf '  %d) %s\n' "$((index+1))" "${versions[index]}" >/dev/tty
  done
  while :; do
    printf 'Version number (or q to cancel): ' >/dev/tty
    IFS= read -r choice </dev/tty || { echo 'Cancelled' >&2; exit 1; }
    if [[ "$choice" == 'q' || "$choice" == 'Q' ]]; then exit 0; fi
    if [[ "$choice" =~ ^[0-9]{1,3}$ ]]; then
      index=$((10#$choice))
      if (( index >= 1 && index <= ${#versions[@]} )); then
        selected_branch="${versions[index-1]}"
        break
      fi
    fi
    printf 'Invalid selection; try again.\n' >/dev/tty
  done
fi

printf '\nSelected version: %s\n' "$selected_branch"
if [[ -n "$selected_commit" ]]; then printf 'Pinned commit: %s\n' "$selected_commit"; fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
safe_apt_install git ca-certificates python3 python3-venv ffmpeg libcairo2
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  echo 'Mikachan v2 requires Python 3.12+; this distribution default is older.' >&2
  exit 1
fi

panel_root="$install_root/panel"
mkdir -p "$panel_root/deploy" "$install_root/tmp/bootstrap" "$install_root/cache/pip" \
         "$install_root/config"
if ! $resume_partial && ! $resume_bootstrap; then
  printf 'Mikachan bootstrap schema 1\n' > "$install_root/.bootstrap-in-progress"
  chmod 0600 "$install_root/.bootstrap-in-progress"
fi
if getent group mikachan >/dev/null; then
  chgrp mikachan "$install_root/tmp" "$install_root/config"
  chmod 0750 "$install_root/tmp" "$install_root/config"
else
  chmod 0700 "$install_root/tmp" "$install_root/config"
fi
if [[ -z "$source_dir" ]]; then
  temp_dir="$(mktemp -d "$install_root/tmp/bootstrap/source.XXXXXX")"
  trap 'rm -rf "$temp_dir"' EXIT
  git clone --depth 1 --branch "$selected_branch" "$repo_url" "$temp_dir/repo"
  source_dir="$temp_dir/repo"
fi
if [[ ! -f "$source_dir/deploy/manager.py" || ! -f "$source_dir/deploy/manage.sh" ||
      ! -f "$source_dir/deploy/manager_ui.py" ]]; then
  echo 'Selected source does not contain the Mikachan release manager.' >&2
  exit 1
fi
if $resume_partial; then
  printf '\nClearing incomplete managed code and service unit; preserving data and backups.\n'
  printf 'ABORT INSTALL\n' | python3 "$panel_root/deploy/manager.py" abort-install
fi
printf 'Mikachan manager schema 1\n' > "$install_root/.managed-by-mikachan"
install -m 0644 "$source_dir/deploy/__init__.py" "$panel_root/deploy/__init__.py"
install -m 0644 "$source_dir/deploy/manager.py" "$panel_root/deploy/manager.py"
install -m 0644 "$source_dir/deploy/manager_ui.py" "$panel_root/deploy/manager_ui.py"
for panel_module in terminal_assets.py terminal_intro.py; do
  if [[ -f "$source_dir/deploy/$panel_module" ]]; then
    install -m 0644 "$source_dir/deploy/$panel_module" "$panel_root/deploy/$panel_module"
  fi
done
install -m 0644 "$source_dir/deploy/manager-requirements.txt" "$panel_root/deploy/manager-requirements.txt"
install -m 0755 "$source_dir/deploy/manage.sh" "$panel_root/deploy/manage.sh"
asset_root="$install_root/assets/terminal"
mkdir -p "$asset_root/intro" "$asset_root/artwork"
for asset in intro/intro.txt README.md; do
  if [[ ! -e "$asset_root/$asset" && -f "$source_dir/assets/terminal/$asset" ]]; then
    install -m 0644 "$source_dir/assets/terminal/$asset" "$asset_root/$asset"
  fi
done
printf 'Managed by Mikachan bootstrap\n' > "$panel_root/.managed-by-mikachan"
chmod 0600 "$panel_root/.managed-by-mikachan"
python3 - "$repo_url" "$install_root/config/manager.json" <<'PYCONFIG'
import json, os, sys
path = sys.argv[2]
with open(path + '.new', 'w', encoding='utf-8') as output:
    json.dump({'repo_url': sys.argv[1]}, output)
    output.write('\n')
os.chmod(path + '.new', 0o600)
os.replace(path + '.new', path)
PYCONFIG
cat > /usr/local/bin/mikachan <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
if (( EUID != 0 )); then
  exec sudo bash /opt/Mikachan/panel/deploy/manage.sh "$@"
fi
exec bash /opt/Mikachan/panel/deploy/manage.sh "$@"
WRAPPER
chmod 0755 /usr/local/bin/mikachan

install_args=(install --branch "$selected_branch")
if [[ -n "$selected_commit" ]]; then install_args+=(--commit "$selected_commit"); fi
if [[ -n "$config_file" ]]; then
  install_args+=(--config "$config_file")
  printf '\nInstalling and activating %s with private configuration\n' "$selected_branch"
else
  install_args+=(--code-only)
  printf '\nStaging %s; Guided setup will activate the Telegram service\n' "$selected_branch"
fi
mikachan "${install_args[@]}"
rm -f "$install_root/.bootstrap-in-progress"
if [[ -f "$source_dir/deploy/n8n_manager.py" ]]; then
  printf '\nOptional independent n8n setup; Mikachan stays installed if you skip or n8n fails.\n'
  if ! python3 "$source_dir/deploy/n8n_manager.py" wizard </dev/tty >/dev/tty; then
    printf 'n8n setup stopped. Mikachan is unaffected; retry later from Integrations or n8n-manage.\n' >&2
  fi
fi
printf '\nOpening Mikachan panel; choose Guided setup if not configured yet\n'
mikachan </dev/tty >/dev/tty
