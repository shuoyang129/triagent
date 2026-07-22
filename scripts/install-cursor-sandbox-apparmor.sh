#!/usr/bin/env bash
set -euo pipefail

mode="${1:---check}"
case "$mode" in
  --check|--apply) ;;
  *) printf 'Usage: %s [--check|--apply]\n' "$0" >&2; exit 2 ;;
esac

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
source_profile="$repo_root/deploy/apparmor/cursor-agent-sandbox"
target_profile="/etc/apparmor.d/cursor-agent-sandbox"

if [[ ! -f "$source_profile" ]]; then
  printf 'AppArmor source profile missing: %s\n' "$source_profile" >&2
  exit 3
fi

if [[ "$mode" == "--check" ]]; then
  sysctl kernel.apparmor_restrict_unprivileged_userns
  if sudo -n cmp -s "$source_profile" "$target_profile"; then
    printf 'profile-status=installed-and-current\n'
  else
    printf 'profile-status=missing-or-stale\n'
  fi
  exit 0
fi

sudo install -m 0644 "$source_profile" "$target_profile"
sudo apparmor_parser -r "$target_profile"
sysctl kernel.apparmor_restrict_unprivileged_userns
printf 'Cursor sandbox AppArmor profile installed; global userns restriction retained.\n'
