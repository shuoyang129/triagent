#!/usr/bin/env zsh
set -euo pipefail

export PYTEST_ADDOPTS="-p no:cacheprovider"
export PYTHONDONTWRITEBYTECODE=1

cursor_agent_bin="${CURSOR_AGENT_BIN:-/home/ys/.local/bin/cursor-agent}"
args=("$@")
rewrite_output=false

for (( index = 1; index < ${#args}; index++ )); do
  if [[ "${args[index]}" == "--output-format" && "${args[index + 1]}" == "json" ]]; then
    args[$(( index + 1 ))]="text"
    rewrite_output=true
    break
  fi
done

if [[ "${rewrite_output}" != true ]]; then
  exec "${cursor_agent_bin}" "${args[@]}"
fi

stdout_file="$(mktemp "${TMPDIR:-/tmp}/triagent-cursor-output.XXXXXX")"
stderr_file="$(mktemp "${TMPDIR:-/tmp}/triagent-cursor-stderr.XXXXXX")"
cleanup() { rm -f -- "${stdout_file}" "${stderr_file}"; }
trap cleanup EXIT

save_diagnostic() {
  local exit_code="$1"
  local diagnostic_file
  diagnostic_file="$(mktemp "${TMPDIR:-/tmp}/triagent-cursor-diagnostic.${$}.XXXXXX")"
  {
    print -r -- "TRIAGENT_CURSOR_DIAGNOSTIC_V1"
    print -r -- "exit_code=${exit_code}"
    print -r -- "STDOUT"
    cat -- "${stdout_file}"
    print -r -- "STDERR"
    cat -- "${stderr_file}"
  } >| "${diagnostic_file}"
  chmod 600 "${diagnostic_file}"
  print -u2 -- "TriAgent Cursor diagnostic saved for local diagnosis: ${diagnostic_file}"
}

trap 'save_diagnostic 124; exit 124' HUP INT TERM

set +e
"${cursor_agent_bin}" "${args[@]}" > "${stdout_file}" 2> "${stderr_file}"
exit_code=$?
set -e

if (( exit_code != 0 )); then
  save_diagnostic "${exit_code}"
  exit "${exit_code}"
fi

set +e
/usr/bin/python3 - "${stdout_file}" 2>> "${stderr_file}" <<'PY'
import json
from pathlib import Path
import sys

result = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
try:
    payload = json.loads(result)
except json.JSONDecodeError:
    raise SystemExit("Cursor output is not canonical JSON")
required = {"status", "evidence", "artifacts", "changed_paths"}
if not isinstance(payload, dict) or not required.issubset(payload):
    raise SystemExit("Cursor output is missing implementer fields")
print(
    json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result,
            "total_cost_usd": None,
        },
        separators=(",", ":"),
    )
)
PY
envelope_status=$?
set -e
if (( envelope_status != 0 )); then
  save_diagnostic 65
  exit 65
fi
