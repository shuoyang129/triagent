#!/usr/bin/env bash
set -euo pipefail
set -- "${1:-}"

mode="check"
case "$1" in
  "") ;;
  --check) mode="check" ;;
  --apply) mode="apply" ;;
  *) printf 'Usage: %s [--check|--apply]\n' "$0" >&2; exit 2 ;;
esac

: "$HOME"
conda_path="$HOME/miniforge3/bin/conda"
robot_root="$HOME/works/robots"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
runs_root="$robot_root/triagent-runs"
projects_root="$robot_root/projects"
env_name="triagent"
env_path="$HOME/miniforge3/envs/$env_name"

if [[ ! -x "$conda_path" ]]; then
  printf 'Conda missing: %s\n' "$conda_path" >&2
  exit 4
fi
if [[ ! -f "$repo_root/pyproject.toml" ]]; then
  printf 'Repository invalid: %s\n' "$repo_root" >&2
  exit 5
fi

printf 'repository=%s\nconda=%s\nenvironment=%s\nruns=%s\nprojects=%s\n' \
  "$repo_root" "$conda_path" "$env_path" "$runs_root" "$projects_root"

if [[ "$mode" == "check" ]]; then
  if [[ -d "$env_path" ]]; then
    "$conda_path" run -n "$env_name" python --version
  else
    printf 'environment-status=missing\n'
  fi
  printf 'Check complete. No files or environments were changed.\n'
  exit 0
fi

mkdir -p "$runs_root" "$projects_root"
if [[ -d "$env_path" ]]; then
  python_version="$("$conda_path" run -n "$env_name" python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [[ "$python_version" != "3.12" ]]; then
    printf 'Existing triagent Python is %s, expected 3.12\n' "$python_version" >&2
    exit 6
  fi
else
  "$conda_path" create -y -n "$env_name" python=3.12 pip
fi

"$conda_path" run -n "$env_name" python -m pip install -e "${repo_root}[test]"
"$conda_path" run -n "$env_name" python -m triagent.cli --help >/dev/null
printf 'TriAgent environment installation complete.\n'
