#!/usr/bin/env bash
set -uo pipefail

install=false
case "${1:-}" in
  "") ;;
  --install) install=true ;;
  *) printf 'Usage: %s [--install]\n' "$0" >&2; exit 2 ;;
esac

printf 'DGX diagnostic mode: read-only capability checks\n'
for command_name in python3 git codex cursor-agent agy nvidia-smi docker systemctl tmux; do
  if command -v "$command_name" >/dev/null 2>&1; then
    printf 'available: %s\n' "$command_name"
  else
    printf 'missing: %s\n' "$command_name"
  fi
done

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

printf 'Installing explicitly requested baseline packages.\n'
sudo apt-get update
sudo apt-get install -y python3 git tmux
