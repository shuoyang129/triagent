#!/usr/bin/env bash
set -uo pipefail
set -- "${1:-}"

if [[ -z "${HOME:-}" ]]; then
  HOME="${USERPROFILE:-/tmp}"
fi

install=false
case "$1" in
  "") ;;
  --install) install=true ;;
  *) printf 'Usage: %s [--install]\n' "$0" >&2; exit 2 ;;
esac

: "$HOME"

check_command() {
  local label="$1"
  local executable="$2"
  if [[ "$executable" == /* ]]; then
    if [[ -x "$executable" ]]; then
      printf 'available: %s=%s\n' "$label" "$executable"
    else
      printf 'missing: %s=%s\n' "$label" "$executable"
    fi
  elif command -v "$executable" >/dev/null 2>&1; then
    printf 'available: %s=%s\n' "$label" "$(command -v "$executable")"
  else
    printf 'missing: %s=%s\n' "$label" "$executable"
  fi
}

printf 'DGX diagnostic mode: read-only capability checks\n'
check_command python3 python3
check_command git git
check_command codex "$HOME/.local/bin/codex"
check_command cursor-agent "$HOME/.local/bin/cursor-agent"
check_command antigravity "$HOME/.local/bin/agy"
check_command opencode "$HOME/.opencode/bin/opencode"
check_command conda "$HOME/miniforge3/bin/conda"
check_command nvidia-smi nvidia-smi
check_command docker docker
check_command systemctl systemctl
check_command tmux tmux
check_command rsync rsync

if [[ "$install" != true ]]; then
  printf 'Diagnostics complete. No system components were changed.\n'
  exit 0
fi
if [[ ! -t 0 ]]; then
  printf 'Installation cancelled: interactive confirmation is required.\n' >&2
  exit 3
fi
printf 'Type INSTALL to permit apt package installation: '
IFS= read -r confirm
if [[ "$confirm" != "INSTALL" ]]; then
  printf 'Installation cancelled.\n' >&2
  exit 3
fi
sudo apt-get update
sudo apt-get install -y python3 git tmux
