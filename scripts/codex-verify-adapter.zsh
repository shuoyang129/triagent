#!/usr/bin/env zsh
set -euo pipefail

codex_bin="${CODEX_BIN:-/home/ys/.local/bin/codex}"
args=("$@")

if [[ " ${args[*]} " != *" exec "* || " ${args[*]} " != *" --json "* ]]; then
  exec "${codex_bin}" "${args[@]}"
fi

contract="$(<&0)"
test_log="$(mktemp "${TMPDIR:-/tmp}/triagent-codex-tests.XXXXXX")"
diff_log="$(mktemp "${TMPDIR:-/tmp}/triagent-codex-diff.XXXXXX")"
cleanup() { rm -f -- "${test_log}" "${diff_log}"; }
trap cleanup EXIT HUP INT TERM

export PYTHONDONTWRITEBYTECODE=1
export PYTEST_ADDOPTS="-p no:cacheprovider"
python_bin="/home/ys/miniforge3/envs/triagent/bin/python"
[[ -x "${python_bin}" ]] || python_bin="/usr/bin/python3"

set +e
"${python_bin}" -m unittest discover -s tests -v > "${test_log}" 2>&1
test_status=$?
git -c core.whitespace=cr-at-eol diff --check HEAD^ HEAD > "${diff_log}" 2>&1
diff_status=$?
set -e

head_commit="$(git rev-parse HEAD 2>/dev/null || true)"
parent_commit="$(git rev-parse HEAD^ 2>/dev/null || true)"
candidate_nonempty=false
if [[ -n "${head_commit}" && -n "${parent_commit}" ]]; then
  set +e
  git diff --quiet "${parent_commit}" "${head_commit}"
  quiet_status=$?
  set -e
  [[ ${quiet_status} -eq 1 ]] && candidate_nonempty=true
fi

evidence=$'\n\nTRIAGENT_EMBEDDED_VERIFICATION_EVIDENCE_V1\n'
evidence+=$'VERIFICATION_EXECUTION_RULE: Do not invoke tools or request permissions. Verify only the independently collected evidence below and return exactly the required JSON object.\n'
evidence+="UNITTEST_EXIT_STATUS=${test_status}"
evidence+=$'\nGIT_DIFF_CHECK_EXIT_STATUS='
evidence+="${diff_status}"
evidence+=$'\nCANDIDATE_COMMIT_NONEMPTY='
evidence+="${candidate_nonempty}"
evidence+=$'\nHEAD_COMMIT='
evidence+="${head_commit}"
evidence+=$'\nPARENT_COMMIT='
evidence+="${parent_commit}"
evidence+=$'\nGIT_STATUS\n'
evidence+="$(git -c core.quotepath=false status --short 2>/dev/null || true)"
evidence+=$'\nUNITTEST_OUTPUT\n'
evidence+="$(head -c 60000 "${test_log}")"
evidence+=$'\nDIFF_CHECK_OUTPUT\n'
evidence+="$(head -c 10000 "${diff_log}")"
evidence+=$'\nCANDIDATE_DIFF\n'
evidence+="$(git -c core.quotepath=false diff --no-ext-diff --text HEAD^ HEAD -- 2>/dev/null || true)"
evidence="${evidence[1,90000]}"

print -rn -- "${contract}${evidence}" | "${codex_bin}" "${args[@]}"
