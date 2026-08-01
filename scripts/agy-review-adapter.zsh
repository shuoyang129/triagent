#!/usr/bin/env zsh
set -euo pipefail

agy_bin="${AGY_BIN:-/home/ys/.local/bin/agy}"
instruction_prefix="Read and follow the complete instructions in this local file: "
args=("$@")
source_path=""
prompt_index=0

for (( index = 1; index < ${#args}; index++ )); do
  if [[ "${args[index]}" == "-p" && "${args[index + 1]}" == ${instruction_prefix}* ]]; then
    prompt_index=$(( index + 1 ))
    source_path="${args[prompt_index]#${instruction_prefix}}"
    break
  fi
done

if (( prompt_index == 0 )); then
  exec "${agy_bin}" "${args[@]}"
fi

if [[ "${source_path}" != /* || ! -f "${source_path}" || ! -r "${source_path}" ]]; then
  print -u2 -- "TriAgent agy input is not a readable absolute regular file"
  exit 64
fi

review_evidence=$'\n\nTRIAGENT_EMBEDDED_REVIEW_EVIDENCE_V1\nREVIEW_EXECUTION_RULE: Do not invoke tools or request permissions. Review only the embedded repository evidence below and return exactly the required JSON object.\nGIT_STATUS\n'
review_evidence+="$(git -c core.quotepath=false status --short 2>/dev/null || true)"
review_evidence+=$'\nCOMMITTED_HEAD\n'
review_evidence+="$(git -c core.quotepath=false rev-parse HEAD 2>/dev/null || true)"
review_evidence+=$'\nCOMMITTED_HEAD_PARENT\n'
review_evidence+="$(git -c core.quotepath=false rev-parse HEAD^ 2>/dev/null || true)"
review_evidence+=$'\nCOMMITTED_HEAD_PATCH\n'
review_evidence+="$(git -c core.quotepath=false show --format=fuller --stat --patch --no-ext-diff --text HEAD -- 2>/dev/null || true)"
review_evidence+=$'\nTRACKED_DIFF\n'
review_evidence+="$(git -c core.quotepath=false diff --no-ext-diff --text HEAD -- 2>/dev/null || true)"

while IFS= read -r relative_path; do
  [[ -n "${relative_path}" && -f "${PWD}/${relative_path}" && ! -L "${PWD}/${relative_path}" ]] || continue
  case "${relative_path}" in
    (*.md|*.txt|*.py|*.cpp|*.hpp|*.h|*.c|*.json|*.yaml|*.yml|*.toml|*.zsh|*.sh|*.xml|*.msg|*.srv|*.action|*/CMakeLists.txt|*/package.xml)
      review_evidence+=$'\nUNTRACKED_FILE: '"${relative_path}"$'\n'
      review_evidence+="$(<"${PWD}/${relative_path}")"
      ;;
  esac
done < <(git -c core.quotepath=false ls-files --others --exclude-standard 2>/dev/null)

review_evidence="${review_evidence[1,40000]}"
instruction_text="$(<"${source_path}")"
generic_workdir_rule='WORKDIR_RULE=Run every repository inspection, test, and tool call in AUTHORITATIVE_WORKDIR_JSON; TASK scope paths describe the source repository and must not replace this workdir.'
review_workdir_rule='WORKDIR_RULE=Do not run repository inspections, tests, or tools. Independently review only the controller-embedded task, handoff, committed patch, and Codex evidence.'
review_output_rule=$'\nTRIAGENT_FINAL_OUTPUT_CONTRACT_V1\nReturn exactly one JSON object and no prose or markdown. The object must have exactly these keys: status, evidence, artifacts, findings. status is passed or failed. evidence and artifacts are arrays of strings. findings is an array of objects with exactly severity (BLOCKER, MAJOR, MINOR, or NOTE), code, and message. Use [] for empty arrays.\n'
instruction_text="${instruction_text//$generic_workdir_rule/$review_workdir_rule}${review_evidence}${review_output_rule}"
if [[ "${instruction_text}" != *"${review_workdir_rule}"* ]]; then
  instruction_text+=$'\n'"${review_workdir_rule}${review_output_rule}"
fi
args[prompt_index]="${instruction_text}"
args=("--new-project" "${args[@]}")

umask 077
private_dir="$(mktemp -d "${TMPDIR:-/tmp}/triagent-agy-review.XXXXXX")"
chmod 700 "${private_dir}"
stdout_file="${private_dir}/stdout"
stderr_file="${private_dir}/stderr"
stream_fifo="${private_dir}/stdout.fifo"
: > "${stdout_file}"
mkfifo -m 600 "${stream_fifo}"
cleanup() { rm -rf -- "${private_dir}"; }
trap cleanup EXIT HUP INT TERM

save_diagnostic() {
  local exit_code="$1"
  local diagnostic_file
  diagnostic_file="$(mktemp "${TMPDIR:-/tmp}/triagent-agy-review.${$}.XXXXXX")"
  {
    print -r -- "TRIAGENT_AGY_DIAGNOSTIC_V1"
    print -r -- "exit_code=${exit_code}"
    print -r -- "STDOUT"
    cat -- "${stdout_file}"
    print -r -- "STDERR"
    cat -- "${stderr_file}"
  } >| "${diagnostic_file}"
  chmod 600 "${diagnostic_file}"
  print -u2 -- "TriAgent AGY diagnostic saved for local diagnosis: ${diagnostic_file}"
}

set +e
# The provider's raw output remains private. Each complete output record is
# converted to a content-free controller marker immediately, so the v2 runner
# can distinguish real provider activity from a wrapper liveness heartbeat.
# Liveness itself is separate and never classifies as progress.
last_provider_signal=-1
{
  while IFS= read -r record || [[ -n "${record}" ]]; do
    print -r -- "${record}" >> "${stdout_file}"
    # Coalesce genuine provider records without a timer-driven signal.
    if (( SECONDS != last_provider_signal )); then
      print -u2 -r -- "TRIAGENT_AGY_PROVIDER_OUTPUT_V1"
      last_provider_signal=$SECONDS
    fi
  done < "${stream_fifo}"
} &
stream_reader_pid=$!
"${agy_bin}" "${args[@]}" > "${stream_fifo}" 2> "${stderr_file}" &
agy_pid=$!
{
  while kill -0 "${agy_pid}" 2>/dev/null; do
    print -u2 -r -- "TRIAGENT_AGY_LIVENESS_V1"
    sleep 5
  done
} &
liveness_pid=$!
wait "${agy_pid}"
agy_exit_code=$?
kill "${liveness_pid}" 2>/dev/null || true
wait "${liveness_pid}" || true
wait "${stream_reader_pid}" || true
set -e

if (( agy_exit_code != 0 )); then
  save_diagnostic "${agy_exit_code}"
  if /usr/bin/grep -Eiq 'authentication required|authentication failed|login required|not logged in' "${stderr_file}"; then
    print -u2 -- "Authentication required"
  fi
  exit "${agy_exit_code}"
fi

raw_output="$(<"${stdout_file}")"
if ! print -rn -- "${raw_output}" | /usr/bin/python3 -c '
import json
import re
import sys

text = sys.stdin.read().strip()
required = {"status", "evidence", "artifacts", "findings"}

def accepted(value):
    return isinstance(value, dict) and required.issubset(value)

try:
    whole = json.loads(text)
except (json.JSONDecodeError, TypeError):
    whole = None
if accepted(whole):
    print(json.dumps(whole, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(0)

fenced = []
for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL):
    try:
        value = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        continue
    if accepted(value):
        fenced.append(value)
if fenced:
    print(json.dumps(fenced[-1], ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(0)

decoder = json.JSONDecoder()
objects = []
for start, character in enumerate(text):
    if character != "{":
        continue
    try:
        value, end = decoder.raw_decode(text, start)
    except json.JSONDecodeError:
        continue
    if accepted(value):
        objects.append((end - start, value))
if not objects:
    print("TriAgent AGY output contained no canonical review JSON object", file=sys.stderr)
    raise SystemExit(65)
value = max(objects, key=lambda item: item[0])[1]
print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
'; then
  save_diagnostic 0
  exit 65
fi
