#!/usr/bin/env zsh
# v2-only passthrough: the Codex adapter itself requires --sandbox read-only.
# Do not add project-local tests or setup here: this wrapper may run against a
# production checkout that the operator authorized for inspection only.
set -euo pipefail

codex_bin="${CODEX_BIN:-/home/ys/.local/bin/codex}"
args=("$@")
if [[ " ${args[*]} " != *" exec "* ]]; then
  exec "${codex_bin}" "${args[@]}"
fi
if [[ " ${args[*]} " != *" --sandbox read-only "* || " ${args[*]} " != *" --output-schema "* || " ${args[*]} " != *" --output-last-message "* || " ${args[*]} " != *" --json "* ]]; then
  print -u2 -- "TriAgent read-only Codex invocation contract rejected"
  exit 64
fi
exec "${codex_bin}" "${args[@]}"
