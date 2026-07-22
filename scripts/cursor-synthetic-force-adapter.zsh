#!/usr/bin/env zsh
set -euo pipefail

readonly script_dir="${0:A:h}"
readonly base_adapter="${script_dir}/cursor-agent-adapter.zsh"
readonly runs_root="/home/ys/works/robots/triagent-synthetic-runs/runs"
readonly workspace_root="/home/ys/works/robots/synthetic-projects"
args=("$@")

# TriAgent appends probes after the fixed command arguments in the profile.
if (( ${#args} >= 1 )) && [[ "${args[-1]}" == "--version" || "${args[-1]}" == "status" ]]; then
  exec "${base_adapter}" "${args[@]}"
fi

has_force=false
has_sandbox_enabled=false
for (( index = 1; index <= ${#args}; index++ )); do
  case "${args[index]}" in
    --force)
      has_force=true
      ;;
    --sandbox)
      if (( index < ${#args} )) && [[ "${args[index + 1]}" == "enabled" ]]; then
        has_sandbox_enabled=true
      fi
      ;;
    --auto-review|--yolo)
      print -u2 -- "synthetic-force policy: ${args[index]} cannot be combined with --force"
      exit 64
      ;;
  esac
done

if [[ "${has_force}" != true || "${has_sandbox_enabled}" != true ]]; then
  print -u2 -- "synthetic-force policy: both --force and --sandbox enabled are required"
  exit 64
fi

worktree="$(realpath -e -- "${PWD}")" || exit 64
case "${worktree}" in
  ${runs_root}/*/worktree)
    ;;
  *)
    print -u2 -- "synthetic-force policy: refusing non-synthetic worktree: ${worktree}"
    exit 64
    ;;
esac

task_file="${worktree:h}/task.yaml"
/usr/bin/python3 -c '
import json
import sys
from pathlib import Path

task_file = Path(sys.argv[1])
workspace_root = Path(sys.argv[2]).resolve(strict=True)
try:
    task = json.loads(task_file.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"synthetic-force policy: invalid task file: {exc}")
scope = task.get("scope")
if not isinstance(scope, list) or not scope:
    raise SystemExit("synthetic-force policy: task scope must be a non-empty list")
for raw_path in scope:
    if not isinstance(raw_path, str):
        raise SystemExit("synthetic-force policy: every scope entry must be a path string")
    try:
        Path(raw_path).resolve(strict=True).relative_to(workspace_root)
    except (OSError, ValueError):
        raise SystemExit(f"synthetic-force policy: scope is outside synthetic workspace: {raw_path}")
' "${task_file}" "${workspace_root}"

exec "${base_adapter}" "${args[@]}"
