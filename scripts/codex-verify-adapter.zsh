#!/usr/bin/env zsh
set -euo pipefail

codex_bin="${CODEX_BIN:-/home/ys/.local/bin/codex}"
args=("$@")

if [[ " ${args[*]} " != *" exec "* || " ${args[*]} " != *" --json "* ]]; then
  exec "${codex_bin}" "${args[@]}"
fi

contract="$(<&0)"
test_log="$(mktemp "${TMPDIR:-/tmp}/triagent-codex-tests.XXXXXX")"
compile_log="$(mktemp "${TMPDIR:-/tmp}/triagent-codex-compile.XXXXXX")"
diff_log="$(mktemp "${TMPDIR:-/tmp}/triagent-codex-diff.XXXXXX")"
artifact_log="$(mktemp "${TMPDIR:-/tmp}/triagent-codex-artifact.XXXXXX")"
cleanup() {
  rm -f -- "${test_log}" "${compile_log}" "${diff_log}" "${artifact_log}"
  [[ -n "${m36_eval_dir:-}" ]] && rm -rf -- "${m36_eval_dir}"
  [[ -n "${m39_eval_dir:-}" ]] && rm -rf -- "${m39_eval_dir}"
  if [[ "${m32_materialized:-false}" == true || "${m33_materialized:-false}" == true ]]; then
    rm -rf -- ".physical_g1_runs"
  fi
}
trap cleanup EXIT HUP INT TERM

export PYTHONDONTWRITEBYTECODE=1
export PYTEST_ADDOPTS="-p no:cacheprovider"
python_bin="/home/ys/miniforge3/envs/triagent/bin/python"
[[ -x "${python_bin}" ]] || python_bin="/usr/bin/python3"

set +e
policy_json_status="not-requested"
artifact_status="not-requested"
cpp_syntax_status="not-requested"
if [[ "${contract}" == *"tests/test_g1_sonic_isaac_semantics.py"* \
   && "${contract}" == *"services/g1_telemetry/sonic_isaac_semantics.py"* ]]; then
  test_scope="m36-semantics-focused"
  "${python_bin}" -m pytest -q \
    tests/test_g1_sonic_isaac_semantics.py > "${test_log}" 2>&1
  test_status=$?
  "${python_bin}" -m py_compile \
    services/g1_telemetry/sonic_isaac_semantics.py \
    tests/test_g1_sonic_isaac_semantics.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests/test_g1_sonic_onnx_bridge.py"* \
   && "${contract}" == *"services/g1_telemetry/sonic_onnx_bridge.py"* \
   && "${contract}" == *"tools/sonic_onnx_stream.cpp"* ]]; then
  test_scope="m36-core-focused"
  "${python_bin}" -m pytest -q \
    tests/test_g1_sonic_onnx_bridge.py > "${test_log}" 2>&1
  test_status=$?
  "${python_bin}" -m py_compile \
    services/g1_telemetry/sonic_onnx_bridge.py \
    tests/test_g1_sonic_onnx_bridge.py > "${compile_log}" 2>&1
  compile_status=$?
  if [[ ${compile_status} -eq 0 ]]; then
    g++ -std=c++17 -Wall -Wextra -Werror -fsyntax-only \
      -I /home/ys/projects/sonic-g1-official/GR00T-WholeBodyControl/deps/onnxruntime/include \
      tools/sonic_onnx_stream.cpp >> "${compile_log}" 2>&1
    cpp_syntax_status=$?
    [[ ${cpp_syntax_status} -eq 0 ]] || compile_status=${cpp_syntax_status}
  fi

elif [[ "${contract}" == *"tests/test_g1_sonic_isaac_runtime.py"* \
   && "${contract}" == *"services/g1_telemetry/sonic_isaac_runtime.py"* \
   && "${contract}" == *"scripts/run_m36_isaac_sonic_runtime.py"* \
   && "${contract}" == *"tools/sonic_onnx_stream.cpp"* ]]; then
  test_scope="m36-focused"
  "${python_bin}" -m pytest -q \
    tests/test_g1_sonic_isaac_runtime.py > "${test_log}" 2>&1
  test_status=$?
  "${python_bin}" -m py_compile \
    services/g1_telemetry/sonic_isaac_runtime.py \
    scripts/run_m36_isaac_sonic_runtime.py \
    scripts/evaluate_g1_sonic_isaac_runtime.py \
    tests/test_g1_sonic_isaac_runtime.py > "${compile_log}" 2>&1
  compile_status=$?
  expected_expiry_sha="cd5c0c0f6018cf873afd282ab3d28e756a476cf975604b02708196ddfc9af686"
  expected_revoke_sha="435399928850af1e6c3fd1be104b1916595c3478be67bb03b6073cb6b57ff19c"
  expected_aggregate_sha="cc6ba6ad3239eb1d12d652de5d098422145608755ef5915a0053033a7f47291f"
  expected_decision_sha="16fd71c7ee062d4a7abd944304b13cc650dd5fdef86c14e4754e2a4344be5e76"
  expected_png_sha="55d97fd7af3c004e7051d0e02333ee8bca7a601f9311db94a30d94f64a04e5f5"
  m36_dir="docs/evidence/runtime/m36_sonic_isaac_actual_20260730_v4"
  actual_expiry_sha="$(sha256sum "${m36_dir}/expiry.json" 2>/dev/null | cut -d " " -f 1)"
  actual_revoke_sha="$(sha256sum "${m36_dir}/revoke.json" 2>/dev/null | cut -d " " -f 1)"
  actual_aggregate_sha="$(sha256sum "${m36_dir}/aggregate.json" 2>/dev/null | cut -d " " -f 1)"
  actual_decision_sha="$(sha256sum "${m36_dir}/decision.json" 2>/dev/null | cut -d " " -f 1)"
  actual_png_sha="$(sha256sum docs/evidence/m36_sonic_isaac_runtime.png 2>/dev/null | cut -d " " -f 1)"
  png_description="$(file docs/evidence/m36_sonic_isaac_runtime.png 2>/dev/null)"
  m36_repository_root="$("${python_bin}" -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["provenance"]["repository_root"])' \
    "${m36_dir}/expiry.json" 2>/dev/null)"
  m36_source_dir="${m36_repository_root}/docs/evidence/runtime/m36_sonic_isaac_actual_20260730_v4"
  m36_eval_dir="$(mktemp -d "${TMPDIR:-/tmp}/triagent-m36-eval.XXXXXX")"
  "${python_bin}" "${m36_repository_root}/scripts/evaluate_g1_sonic_isaac_runtime.py" \
    --expiry "${m36_source_dir}/expiry.json" \
    --revoke "${m36_source_dir}/revoke.json" \
    --aggregate "${m36_source_dir}/aggregate.json" \
    --output "${m36_eval_dir}/decision.json" > "${artifact_log}" 2>&1
  evaluator_status=$?
  replay_decision_sha="$(sha256sum "${m36_eval_dir}/decision.json" 2>/dev/null | cut -d " " -f 1)"
  if [[ ${evaluator_status} -eq 0 \
     && "${actual_expiry_sha}" == "${expected_expiry_sha}" \
     && "${actual_revoke_sha}" == "${expected_revoke_sha}" \
     && "${actual_aggregate_sha}" == "${expected_aggregate_sha}" \
     && "${actual_decision_sha}" == "${expected_decision_sha}" \
     && "${replay_decision_sha}" == "${expected_decision_sha}" \
     && "${actual_png_sha}" == "${expected_png_sha}" \
     && "${png_description}" == *"PNG image data, 1200 x 800, 8-bit/color RGB, non-interlaced"* ]]; then
    artifact_status=0
  else
    artifact_status=1
  fi
  {
    print -r -- "M36_EVALUATOR_EXIT_STATUS=${evaluator_status}"
    print -r -- "M36_EVALUATOR_REPOSITORY_ROOT=${m36_repository_root}"
    print -r -- "M36_EXPIRY_EXPECTED_SHA256=${expected_expiry_sha}"
    print -r -- "M36_EXPIRY_ACTUAL_SHA256=${actual_expiry_sha}"
    print -r -- "M36_REVOKE_EXPECTED_SHA256=${expected_revoke_sha}"
    print -r -- "M36_REVOKE_ACTUAL_SHA256=${actual_revoke_sha}"
    print -r -- "M36_AGGREGATE_EXPECTED_SHA256=${expected_aggregate_sha}"
    print -r -- "M36_AGGREGATE_ACTUAL_SHA256=${actual_aggregate_sha}"
    print -r -- "M36_DECISION_EXPECTED_SHA256=${expected_decision_sha}"
    print -r -- "M36_DECISION_ACTUAL_SHA256=${actual_decision_sha}"
    print -r -- "M36_REPLAY_DECISION_SHA256=${replay_decision_sha}"
    print -r -- "M36_PNG_EXPECTED_SHA256=${expected_png_sha}"
    print -r -- "M36_PNG_ACTUAL_SHA256=${actual_png_sha}"
    print -r -- "M36_PNG_FILE=${png_description}"
    print -r -- "VISUAL_SEMANTIC_REVIEW_DEFERRED_TO_REQUIRED_ANTIGRAVITY_STAGE=true"
  } >> "${artifact_log}"

elif [[ ( "${contract}" == *"tests/test_g1_sonic_minimal_motion.py"* \
   && "${contract}" == *"tests/test_m39_minimal_motion_adapter.py"* \
   && "${contract}" == *"services/g1_telemetry/sonic_minimal_motion.py"* \
   && "${contract}" == *"scripts/materialize_m39_sonic_artifact.py"* \
   && "${contract}" == *"scripts/m39_minimal_motion_adapter.py"* \
   && "${contract}" == *"scripts/m39_restore_mode_recovery.py"* \
   && "${contract}" == *"scripts/collect_g1_sonic_minimal_motion.py"* ) \
   || ( "${contract}" == *"M39 physical G1 repair review"* \
   && "${contract}" == *"tests/test_g1_sonic_minimal_motion.py"* \
   && "${contract}" == *"tests/test_m39_minimal_motion_adapter.py"* ) \
   || ( "${contract}" == *"M39 physical restore repair"* \
   && "${contract}" == *"scripts/m39_minimal_motion_adapter.py"* \
   && "${contract}" == *"scripts/m39_restore_mode_recovery.py"* \
   && "${contract}" == *"tests/test_m39_minimal_motion_adapter.py"* ) \
   || "${contract}" == *"M39 physical restore repair final pre-active review"* ]]; then
  test_scope="m39-exact-127-321-15"
  test_status=0
  "${python_bin}" -m pytest -q \
    tests/test_g1_sonic_minimal_motion.py \
    tests/test_m39_minimal_motion_adapter.py > "${test_log}" 2>&1
  [[ $? -eq 0 ]] || test_status=1
  if ! grep -Fq "127 passed" "${test_log}"; then
    test_status=1
  fi
  "${python_bin}" -m pytest -q \
    tests/test_g1_physical_hold_rehearsal.py \
    tests/test_g1_sonic_zero_command_takeover.py \
    tests/test_m38_zero_command_takeover_adapter.py \
    tests/test_g1_sonic_minimal_motion.py \
    tests/test_m39_minimal_motion_adapter.py >> "${test_log}" 2>&1
  [[ $? -eq 0 ]] || test_status=1
  if ! grep -Fq "321 passed, 18 subtests passed" "${test_log}"; then
    test_status=1
  fi
  "${python_bin}" -m pytest -q \
    tests/test_repository_policy.py >> "${test_log}" 2>&1
  [[ $? -eq 0 ]] || test_status=1
  if ! grep -Fq "15 passed" "${test_log}"; then
    test_status=1
  fi
  "${python_bin}" -m py_compile \
    services/g1_telemetry/sonic_minimal_motion.py \
    scripts/materialize_m39_sonic_artifact.py \
    scripts/evaluate_g1_sonic_minimal_motion.py \
    scripts/m39_minimal_motion_adapter.py \
    scripts/m39_restore_mode_recovery.py \
    scripts/collect_g1_sonic_minimal_motion.py \
    tests/test_g1_sonic_minimal_motion.py \
    tests/test_m39_minimal_motion_adapter.py > "${compile_log}" 2>&1
  compile_status=$?
  if [[ ${compile_status} -eq 0 ]]; then
    "${python_bin}" -m json.tool \
      configs/g1_sonic_minimal_motion_policy.json \
      > "${artifact_log}" 2>&1
    policy_json_status=$?
    [[ ${policy_json_status} -eq 0 ]] || compile_status=${policy_json_status}
  fi
  m39_eval_dir="$(mktemp -d "${TMPDIR:-/tmp}/triagent-m39-eval.XXXXXX")"
  "${python_bin}" scripts/materialize_m39_sonic_artifact.py \
    --output "${m39_eval_dir}/sonic-artifact.json" >> "${artifact_log}" 2>&1
  materializer_status=$?
  if [[ ${materializer_status} -eq 0 ]]; then
    "${python_bin}" -c \
      "import json,sys; x=json.load(open(sys.argv[1], encoding=\"utf-8\")); assert x[\"source_evidence_canonical_sha256\"] == \"dd888cfb4216067a7b24bff1f9ba01909b7335c821b94112d384fb77ba897d69\"; assert x[\"action_vector_float32_sha256\"] == \"4083b391964332e77b63306d4f2672bbba23436f3defb681cb22e75246564213\"" \
      "${m39_eval_dir}/sonic-artifact.json" >> "${artifact_log}" 2>&1
    artifact_status=$?
  else
    artifact_status=${materializer_status}
  fi
  m39_png_path="docs/evidence/m39_sonic_minimal_motion_contract.png"
  m39_png_expected_sha="62b3682fa70689695dc83d18eccbcf358aa6f447d92391988f06916706966632"
  m39_png_actual_sha="$(sha256sum "${m39_png_path}" 2>/dev/null | cut -d " " -f 1)"
  m39_png_description="$(file "${m39_png_path}" 2>/dev/null)"
  if [[ ${artifact_status} -ne 0 \
     || "${m39_png_actual_sha}" != "${m39_png_expected_sha}" \
     || "${m39_png_description}" != *"PNG image data, 1200 x 800, 8-bit/color RGB, non-interlaced"* ]]; then
    artifact_status=1
  fi
  {
    print -r -- "M39_MATERIALIZER_EXIT_STATUS=${materializer_status}"
    print -r -- "M39_VISUAL_ARTIFACT_PATH=${m39_png_path}"
    print -r -- "M39_PNG_EXPECTED_SHA256=${m39_png_expected_sha}"
    print -r -- "M39_PNG_ACTUAL_SHA256=${m39_png_actual_sha}"
    print -r -- "M39_PNG_FILE=${m39_png_description}"
    print -r -- "M39_EXACT_FOCUSED_EXPECTED=127 passed"
    print -r -- "M39_EXACT_PROTECTION_EXPECTED=321 passed, 18 subtests passed"
    print -r -- "M39_EXACT_POLICY_EXPECTED=15 passed"
  } >> "${artifact_log}"

elif [[ "${contract}" == *"tests/test_g1_sonic_zero_command_takeover.py"* \
   && "${contract}" == *"tests/test_m38_zero_command_takeover_adapter.py"* \
   && "${contract}" == *"services/g1_telemetry/sonic_zero_command_takeover.py"* \
   && "${contract}" == *"scripts/m38_zero_command_takeover_adapter.py"* \
   && "${contract}" == *"scripts/collect_g1_sonic_zero_command_takeover.py"* ]]; then
  test_scope="m38-zero-command-takeover-focused"
  "${python_bin}" -m pytest -q \
    tests/test_g1_sonic_zero_command_takeover.py \
    tests/test_m38_zero_command_takeover_adapter.py \
    tests/test_g1_sonic_physical_readiness.py \
    tests/test_g1_physical_hold_rehearsal.py > "${test_log}" 2>&1
  test_status=$?
  "${python_bin}" -m py_compile \
    services/g1_telemetry/sonic_zero_command_takeover.py \
    scripts/m38_zero_command_takeover_adapter.py \
    scripts/collect_g1_sonic_zero_command_takeover.py \
    scripts/evaluate_g1_sonic_zero_command_takeover.py \
    tests/test_g1_sonic_zero_command_takeover.py \
    tests/test_m38_zero_command_takeover_adapter.py > "${compile_log}" 2>&1
  compile_status=$?
  if [[ ${compile_status} -eq 0 ]]; then
    "${python_bin}" -m json.tool \
      configs/g1_sonic_zero_command_takeover_policy.json \
      > "${artifact_log}" 2>&1
    policy_json_status=$?
    [[ ${policy_json_status} -eq 0 ]] || compile_status=${policy_json_status}
  fi

elif [[ "${contract}" == *"tests/test_g1_sonic_physical_readiness.py"* \
   && "${contract}" == *"services/g1_telemetry/sonic_physical_readiness.py"* \
   && "${contract}" == *"scripts/collect_g1_sonic_physical_readiness.py"* \
   && "${contract}" == *"scripts/evaluate_g1_sonic_physical_readiness.py"* ]]; then
  test_scope="m37-focused"
  "${python_bin}" -m pytest -q \
    tests/test_g1_sonic_physical_readiness.py > "${test_log}" 2>&1
  test_status=$?
  "${python_bin}" -m py_compile \
    services/g1_telemetry/sonic_physical_readiness.py \
    scripts/collect_g1_sonic_physical_readiness.py \
    scripts/evaluate_g1_sonic_physical_readiness.py \
    tests/test_g1_sonic_physical_readiness.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ ( "${contract}" == *"tests/test_g1_sonic_writer_admission.py"* \
      && "${contract}" == *"services/g1_telemetry/sonic_writer_admission.py"* \
      && "${contract}" == *"scripts/simulate_g1_sonic_writer_admission.py"* \
      && "${contract}" == *"scripts/evaluate_g1_sonic_writer_admission.py"* ) \
   || ( "${contract}" == *"docs/evidence/m35_triagent_review_scope.md"* \
      && "${contract}" == *"6bf175704a096dad527522232311ad77d4c79b86"* ) ]]; then
  test_scope="m35-focused"
  "${python_bin}" -m pytest -q \
    tests/test_g1_sonic_writer_admission.py > "${test_log}" 2>&1
  test_status=$?
  "${python_bin}" -m py_compile \
    services/g1_telemetry/sonic_writer_admission.py \
    scripts/simulate_g1_sonic_writer_admission.py \
    scripts/evaluate_g1_sonic_writer_admission.py \
    tests/test_g1_sonic_writer_admission.py > "${compile_log}" 2>&1
  compile_status=$?
  expected_png_sha="76de8e88eb738a1331547e67326f17f518b11ac0f7a611d153f8687bf37aedfe"
  expected_decision_sha="ac6bea8cfd69afcc3a8791f64d6cb2a6fd6f8e13b1fae7245b7317b260e85794"
  actual_png_sha="$(sha256sum docs/evidence/m35_sonic_writer_admission.png 2>/dev/null | cut -d " " -f 1)"
  actual_decision_sha="$(sha256sum docs/evidence/m35_sonic_writer_admission.json 2>/dev/null | cut -d " " -f 1)"
  png_description="$(file docs/evidence/m35_sonic_writer_admission.png 2>/dev/null)"
  if [[ "${actual_png_sha}" == "${expected_png_sha}" \
     && "${actual_decision_sha}" == "${expected_decision_sha}" \
     && "${png_description}" == *"PNG image data, 1200 x 760, 8-bit/color RGB, non-interlaced"* ]]; then
    artifact_status=0
  else
    artifact_status=1
  fi
  {
    print -r -- "M35_PNG_EXPECTED_SHA256=${expected_png_sha}"
    print -r -- "M35_PNG_ACTUAL_SHA256=${actual_png_sha}"
    print -r -- "M35_DECISION_EXPECTED_SHA256=${expected_decision_sha}"
    print -r -- "M35_DECISION_ACTUAL_SHA256=${actual_decision_sha}"
    print -r -- "M35_PNG_FILE=${png_description}"
    print -r -- "VISUAL_SEMANTIC_REVIEW_DEFERRED_TO_REQUIRED_ANTIGRAVITY_STAGE=true"
  } > "${artifact_log}"

elif [[ "${contract}" == *"tests/test_g1_pc1_blackbox_boundary.py"* \
   && "${contract}" == *"33287da3f641c53ae2e79ff56c34d96d9b1b64f58aeb5b24a416ddfc23c1d1e0"* ]]; then
  test_scope="m34-focused-22"
  "${python_bin}" -m pytest -q \
    tests/test_g1_pc1_blackbox_boundary.py > "${test_log}" 2>&1
  test_status=$?
  if [[ ${test_status} -eq 0 ]] && ! grep -Fq "22 passed" "${test_log}"; then
    test_status=1
  fi
  compile_status="not-requested"
  expected_png_sha="33287da3f641c53ae2e79ff56c34d96d9b1b64f58aeb5b24a416ddfc23c1d1e0"
  actual_png_sha="$(sha256sum docs/evidence/m34_pc1_blackbox_boundary.png 2>/dev/null | cut -d " " -f 1)"
  png_description="$(file docs/evidence/m34_pc1_blackbox_boundary.png 2>/dev/null)"
  if [[ "${actual_png_sha}" == "${expected_png_sha}" \
     && "${png_description}" == *"PNG image data, 1200 x 760, 8-bit/color RGB, non-interlaced"* ]]; then
    artifact_status=0
  else
    artifact_status=1
  fi
  {
    print -r -- "M34_PNG_EXPECTED_SHA256=${expected_png_sha}"
    print -r -- "M34_PNG_ACTUAL_SHA256=${actual_png_sha}"
    print -r -- "M34_PNG_FILE=${png_description}"
    print -r -- "VISUAL_SEMANTIC_REVIEW_DEFERRED_TO_REQUIRED_ANTIGRAVITY_STAGE=true"
  } > "${artifact_log}"

elif [[ "${contract}" == *"tests.test_g1_physical_single_writer_remediation"* \
   && "${contract}" == *"expected 265/265"* \
   && "${contract}" == *"expected 149/149"* ]]; then
  test_scope="m33-exact-36-265-149"
  m33_python_bin="/home/ys/miniforge3/bin/python3"
  test_status=0
  m33_materialized=false
  if [[ ! -e ".physical_g1_runs" ]]; then
    mkdir -p ".physical_g1_runs"
    for source in \
      m25-g1-readonly-20260728-115500-v1 \
      m26-g1-odom-20260728-132400-v2 \
      m27-g1-motion-admission-20260728-143310 \
      m28-g1-hold-rehearsal-20260728-170210 \
      m29-g1-owner-diagnostic-20260729-173206 \
      m30-g1-qos-diagnostic-20260729-183752; do
      cp -a "/home/ys/works/robots/projects/humanoid/.physical_g1_runs/${source}" \
        ".physical_g1_runs/${source}"
    done
    m33_materialized=true
  fi
  "${m33_python_bin}" -m unittest -v \
    tests.test_g1_physical_single_writer_remediation > "${test_log}" 2>&1
  [[ $? -eq 0 ]] || test_status=1
  "${m33_python_bin}" -m unittest \
    tests.test_g1_physical_motion_admission \
    tests.test_g1_physical_hold_rehearsal \
    tests.test_g1_physical_owner_interlock \
    tests.test_g1_safety_arbitration \
    tests.test_m29_dcps_owner_observer \
    tests.test_m29_cpp_interlock_contract \
    tests.test_m30_qos_observer \
    tests.test_m30_cpp_arbitration_contract \
    tests.test_m31_lease_lifecycle \
    tests.test_g1_physical_motion_admission_reeval \
    tests.test_g1_physical_single_writer_remediation >> "${test_log}" 2>&1
  [[ $? -eq 0 ]] || test_status=1
  "${m33_python_bin}" -m unittest \
    tests.test_protected_isaac_mainline \
    tests.test_protected_isaac_sensors \
    tests.test_protected_isaac_robustness \
    tests.test_actual_isaac_runtime_evidence >> "${test_log}" 2>&1
  [[ $? -eq 0 ]] || test_status=1
  "${m33_python_bin}" -m unittest tests.test_repository_policy >> "${test_log}" 2>&1
  [[ $? -eq 0 ]] || test_status=1
  if ! grep -Fq "Ran 36 tests" "${test_log}" \
     || ! grep -Fq "Ran 265 tests" "${test_log}" \
     || ! grep -Fq "Ran 149 tests" "${test_log}" \
     || ! grep -Fq "Ran 15 tests" "${test_log}"; then
    test_status=1
  fi
  "${m33_python_bin}" -m py_compile \
    services/g1_telemetry/physical_single_writer_remediation.py \
    scripts/evaluate_g1_physical_single_writer_remediation.py \
    scripts/collect_g1_remote_owner_attestation.py \
    tests/test_g1_physical_single_writer_remediation.py > "${compile_log}" 2>&1
  compile_status=$?
  if [[ "${m33_materialized}" == true ]]; then
    rm -rf -- ".physical_g1_runs"
  fi

elif [[ "${contract}" == *"tests.test_g1_physical_motion_admission_reeval"* \
   && "${contract}" == *"expected 229/229"* \
   && "${contract}" == *"expected 149/149"* ]]; then
  test_scope="m32-exact-34-229-149"
  m32_python_bin="/home/ys/miniforge3/bin/python3"
  test_status=0
  m32_materialized=false
  if [[ ! -e ".physical_g1_runs" ]]; then
    mkdir -p ".physical_g1_runs"
    for source in \
      m25-g1-readonly-20260728-115500-v1 \
      m26-g1-odom-20260728-132400-v2 \
      m27-g1-motion-admission-20260728-143310 \
      m28-g1-hold-rehearsal-20260728-170210 \
      m29-g1-owner-diagnostic-20260729-173206 \
      m30-g1-qos-diagnostic-20260729-183752; do
      cp -a "/home/ys/works/robots/projects/humanoid/.physical_g1_runs/${source}" \
        ".physical_g1_runs/${source}"
    done
    m32_materialized=true
  fi
  "${m32_python_bin}" -m unittest -v \
    tests.test_g1_physical_motion_admission_reeval > "${test_log}" 2>&1
  [[ $? -eq 0 ]] || test_status=1
  "${m32_python_bin}" -m unittest \
    tests.test_g1_physical_motion_admission \
    tests.test_g1_physical_hold_rehearsal \
    tests.test_g1_physical_owner_interlock \
    tests.test_g1_safety_arbitration \
    tests.test_m29_dcps_owner_observer \
    tests.test_m29_cpp_interlock_contract \
    tests.test_m30_qos_observer \
    tests.test_m30_cpp_arbitration_contract \
    tests.test_m31_lease_lifecycle \
    tests.test_g1_physical_motion_admission_reeval >> "${test_log}" 2>&1
  [[ $? -eq 0 ]] || test_status=1
  "${m32_python_bin}" -m unittest \
    tests.test_protected_isaac_mainline \
    tests.test_protected_isaac_sensors \
    tests.test_protected_isaac_robustness \
    tests.test_actual_isaac_runtime_evidence >> "${test_log}" 2>&1
  [[ $? -eq 0 ]] || test_status=1
  "${m32_python_bin}" -m unittest tests.test_repository_policy >> "${test_log}" 2>&1
  [[ $? -eq 0 ]] || test_status=1
  if ! grep -Fq "Ran 34 tests" "${test_log}" \
     || ! grep -Fq "Ran 229 tests" "${test_log}" \
     || ! grep -Fq "Ran 149 tests" "${test_log}" \
     || ! grep -Fq "Ran 15 tests" "${test_log}"; then
    test_status=1
  fi
  "${m32_python_bin}" -m py_compile \
    services/g1_telemetry/physical_motion_admission_reeval.py \
    scripts/evaluate_g1_physical_motion_admission_reeval.py \
    tests/test_g1_physical_motion_admission_reeval.py > "${compile_log}" 2>&1
  compile_status=$?
  if [[ "${m32_materialized}" == true ]]; then
    rm -rf -- ".physical_g1_runs"
  fi

elif [[ "${contract}" == *"tests/test_g1_physical_hold_rehearsal.py"* \
   && "${contract}" == *"tests/test_m28_live_observers.py"* \
   && "${contract}" == *"173 tests"* ]]; then
  test_scope="m28-exact-173"
  m28_fixture_dir="triagent_inputs/m27/evidence"
  m28_fixture_paths=()
  mkdir -p "${m28_fixture_dir}"
  if [[ ! -f "${m28_fixture_dir}/m25_evidence.json" ]]; then
    cp /home/ys/works/robots/projects/humanoid/.physical_g1_runs/m25-g1-readonly-20260728-115500-v1/evidence.json "${m28_fixture_dir}/m25_evidence.json"
    m28_fixture_paths+=("${m28_fixture_dir}/m25_evidence.json")
  fi
  if [[ ! -f "${m28_fixture_dir}/m26_evidence.json" ]]; then
    cp /home/ys/works/robots/projects/humanoid/.physical_g1_runs/m26-g1-odom-20260728-132400-v2/evidence.json "${m28_fixture_dir}/m26_evidence.json"
    m28_fixture_paths+=("${m28_fixture_dir}/m26_evidence.json")
  fi
  "${python_bin}" -m pytest -q \
    tests/test_g1_physical_hold_rehearsal.py \
    tests/test_m28_live_observers.py \
    tests/test_g1_physical_motion_admission.py \
    tests/test_g1_physical_odom.py \
    tests/test_g1_physical_readonly.py \
    tests/test_g1_physical_readiness.py \
    tests/test_repository_policy.py > "${test_log}" 2>&1
  test_status=$?
  if [[ ${test_status} -eq 0 ]] && ! grep -Fq "173 passed, 116 subtests passed" "${test_log}"; then
    test_status=1
  fi
  "${python_bin}" -m py_compile \
    services/g1_telemetry/physical_hold_rehearsal.py \
    scripts/collect_g1_physical_hold_rehearsal.py \
    scripts/m28_observe_dcps_publications.py \
    scripts/m28_observe_ros_odom.py \
    scripts/m28_observe_unitree.py \
    tests/test_g1_physical_hold_rehearsal.py \
    tests/test_m28_live_observers.py > "${compile_log}" 2>&1
  compile_status=$?
  if [[ ${compile_status} -eq 0 ]]; then
    "${python_bin}" -c "from pathlib import Path; from services.g1_telemetry.physical_hold_rehearsal import strict_json_file, validate_evidence; evidence, _ = strict_json_file(Path(\".physical_g1_runs/m28-g1-hold-rehearsal-20260728-170210/evidence.json\")); validate_evidence(evidence); print(\"EVIDENCE_VALID_FAIL_CLOSED\")" >> "${compile_log}" 2>&1
    compile_status=$?
  fi
  for m28_fixture_path in "${m28_fixture_paths[@]}"; do
    rm -f -- "${m28_fixture_path}"
  done
  rmdir "${m28_fixture_dir}" "${m28_fixture_dir:h}" "${m28_fixture_dir:h:h}" 2>/dev/null

elif [[ "${contract}" == *"tests.test_g1_physical_motion_admission tests.test_g1_physical_odom tests.test_g1_physical_readonly"* \
   && "${contract}" == *"services/g1_telemetry/physical_motion_admission.py scripts/evaluate_g1_physical_motion_admission.py tests/test_g1_physical_motion_admission.py"* ]]; then
  test_scope="m27-exact-141"
  m27_python_bin="/usr/bin/python3"
  "${m27_python_bin}" -m unittest -v \
    tests.test_g1_physical_motion_admission \
    tests.test_g1_physical_odom \
    tests.test_g1_physical_readonly \
    tests.test_g1_physical_readiness \
    tests.test_repository_policy > "${test_log}" 2>&1
  test_status=$?
  "${m27_python_bin}" -m py_compile \
    services/g1_telemetry/physical_motion_admission.py \
    scripts/evaluate_g1_physical_motion_admission.py \
    tests/test_g1_physical_motion_admission.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests.test_g1_physical_odom tests.test_g1_physical_readonly"* \
   && "${contract}" == *"services/g1_telemetry/physical_odom.py scripts/collect_g1_physical_odom.py tests/test_g1_physical_odom.py"* ]]; then
  test_scope="m26-exact-62"
  m26_python_bin="/usr/bin/python3"
  "${m26_python_bin}" -m unittest -v \
    tests.test_g1_physical_odom \
    tests.test_g1_physical_readonly > "${test_log}" 2>&1
  test_status=$?
  "${m26_python_bin}" -m py_compile \
    services/g1_telemetry/physical_odom.py \
    scripts/collect_g1_physical_odom.py \
    tests/test_g1_physical_odom.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests.test_g1_physical_readonly tests.test_g1_physical_readiness tests.test_g1_telemetry_contract tests.test_repository_policy"* \
   && "${contract}" == *"services/g1_telemetry/physical_readonly.py scripts/collect_g1_physical_readonly.py tests/test_g1_physical_readonly.py"* ]]; then
  test_scope="m25-exact-83"
  m25_python_bin="/usr/bin/python3"
  "${m25_python_bin}" -m unittest -v \
    tests.test_g1_physical_readonly \
    tests.test_g1_physical_readiness \
    tests.test_g1_telemetry_contract \
    tests.test_repository_policy > "${test_log}" 2>&1
  test_status=$?
  "${m25_python_bin}" -m py_compile \
    services/g1_telemetry/physical_readonly.py \
    scripts/collect_g1_physical_readonly.py \
    tests/test_g1_physical_readonly.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests.test_g1_isaac_retry_safety_candidate_admission"* ]]; then
  test_scope="m24-exact-364"
  m24_python_bin="/usr/bin/python3"
  "${m24_python_bin}" -m unittest -v \
    tests.test_g1_isaac_retry_safety_candidate_admission \
    tests.test_g1_isaac_retry_safety_admission \
    tests.test_g1_isaac_retry_safety_candidate > "${test_log}" 2>&1
  test_status=$?
  "${m24_python_bin}" -m py_compile \
    services/full_e2e/retry_safety_candidate_admission.py \
    scripts/evaluate_g1_isaac_retry_safety_candidate_admission.py \
    tests/test_g1_isaac_retry_safety_candidate_admission.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests.test_g1_isaac_retry_safety_candidate"* \
   && "${contract}" == *"services/full_e2e/retry_safety_candidate.py scripts/capture_isaac_g1_retry_safety_candidate.py tests/test_g1_isaac_retry_safety_candidate.py"* ]]; then
  test_scope="m23-focused"
  m23_python_bin="/usr/bin/python3"
  "${m23_python_bin}" -m unittest -v \
    tests.test_g1_isaac_retry_safety_candidate > "${test_log}" 2>&1
  test_status=$?
  "${m23_python_bin}" -m py_compile \
    services/full_e2e/retry_safety_candidate.py \
    scripts/capture_isaac_g1_retry_safety_candidate.py \
    tests/test_g1_isaac_retry_safety_candidate.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests.test_g1_isaac_retry_safety_admission"* \
   && "${contract}" == *"services/full_e2e/retry_safety_admission.py scripts/evaluate_g1_isaac_retry_safety_admission.py tests/test_g1_isaac_retry_safety_admission.py"* ]]; then
  test_scope="m22-focused"
  m22_python_bin="/usr/bin/python3"
  "${m22_python_bin}" -m unittest -v \
    tests.test_g1_isaac_retry_safety_admission > "${test_log}" 2>&1
  test_status=$?
  "${m22_python_bin}" -m py_compile \
    services/full_e2e/retry_safety_admission.py \
    scripts/evaluate_g1_isaac_retry_safety_admission.py \
    tests/test_g1_isaac_retry_safety_admission.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests.test_g1_isaac_denial_retry"* \
   && "${contract}" == *"services/full_e2e/denial_retry_gate.py scripts/evaluate_g1_isaac_denial_retry.py tests/test_g1_isaac_denial_retry.py"* ]]; then
  test_scope="m21-focused"
  m21_python_bin="/usr/bin/python3"
  "${m21_python_bin}" -m unittest -v \
    tests.test_g1_isaac_denial_retry > "${test_log}" 2>&1
  test_status=$?
  "${m21_python_bin}" -m py_compile \
    services/full_e2e/denial_retry_gate.py \
    scripts/evaluate_g1_isaac_denial_retry.py \
    tests/test_g1_isaac_denial_retry.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests.test_g1_isaac_denial_feedback"* \
   && "${contract}" == *"services/full_e2e/denial_feedback.py scripts/finalize_g1_isaac_denial_feedback.py tests/test_g1_isaac_denial_feedback.py"* ]]; then
  test_scope="m20-focused"
  m20_python_bin="/usr/bin/python3"
  "${m20_python_bin}" -m unittest -v \
    tests.test_g1_isaac_denial_feedback > "${test_log}" 2>&1
  test_status=$?
  "${m20_python_bin}" -m py_compile \
    services/full_e2e/denial_feedback.py \
    scripts/finalize_g1_isaac_denial_feedback.py \
    tests/test_g1_isaac_denial_feedback.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests.test_g1_isaac_premotion_denial"* \
   && "${contract}" == *"services/full_e2e/premotion_denial.py scripts/evaluate_g1_isaac_premotion_denial.py tests/test_g1_isaac_premotion_denial.py"* ]]; then
  test_scope="m19-exact-46"
  m19_python_bin="/usr/bin/python3"
  "${m19_python_bin}" -m unittest -v \
    tests.test_g1_isaac_premotion_denial > "${test_log}" 2>&1
  test_status=$?
  "${m19_python_bin}" -m py_compile \
    services/full_e2e/premotion_denial.py \
    scripts/evaluate_g1_isaac_premotion_denial.py \
    tests/test_g1_isaac_premotion_denial.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests.test_g1_isaac_premotion_safety"* \
   && "${contract}" == *"services/g1_telemetry/__init__.py services/g1_telemetry/premotion.py scripts/evaluate_g1_isaac_premotion.py tests/test_g1_isaac_premotion_safety.py"* ]]; then
  test_scope="m18-exact-38"
  "${python_bin}" -m unittest -v \
    tests.test_g1_isaac_premotion_safety > "${test_log}" 2>&1
  test_status=$?
  "${python_bin}" -m py_compile \
    services/g1_telemetry/__init__.py \
    services/g1_telemetry/premotion.py \
    scripts/evaluate_g1_isaac_premotion.py \
    tests/test_g1_isaac_premotion_safety.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests.test_g1_isaac_support_safety"* \
   && "${contract}" == *"services/g1_telemetry/support.py scripts/capture_isaac_g1_support_artifact.py scripts/evaluate_g1_isaac_support.py tests/test_g1_isaac_support_safety.py"* ]]; then
  test_scope="m17-focused"
  "${python_bin}" -m unittest -v \
    tests.test_g1_isaac_support_safety > "${test_log}" 2>&1
  test_status=$?
  "${python_bin}" -m py_compile \
    services/g1_telemetry/support.py \
    scripts/capture_isaac_g1_support_artifact.py \
    scripts/evaluate_g1_isaac_support.py \
    tests/test_g1_isaac_support_safety.py > "${compile_log}" 2>&1
  compile_status=$?

elif [[ "${contract}" == *"tests.test_g1_isaac_posture_safety"* \
   && "${contract}" == *"services/g1_telemetry/__init__.py services/g1_telemetry/posture.py scripts/evaluate_g1_isaac_posture.py tests/test_g1_isaac_posture_safety.py"* ]]; then
  test_scope="m16-exact-36"
  "${python_bin}" -m unittest -v \
    tests.test_g1_isaac_posture_safety > "${test_log}" 2>&1
  test_status=$?
  "${python_bin}" -m py_compile \
    services/g1_telemetry/__init__.py \
    services/g1_telemetry/posture.py \
    scripts/evaluate_g1_isaac_posture.py \
    tests/test_g1_isaac_posture_safety.py > "${compile_log}" 2>&1
  compile_status=$?
elif [[ "${contract}" == *"tests.test_g1_isaac_telemetry_safety tests.test_g1_offline_safety tests.test_g1_telemetry_contract"* \
   && "${contract}" == *"services/g1_telemetry/isaac_rehearsal.py scripts/capture_isaac_g1_telemetry_artifact.py scripts/run_g1_isaac_telemetry_safety_rehearsal.py"* ]]; then
  test_scope="m15-exact-95"
  "${python_bin}" -m unittest -v \
    tests.test_g1_isaac_telemetry_safety \
    tests.test_g1_offline_safety \
    tests.test_g1_telemetry_contract > "${test_log}" 2>&1
  test_status=$?
  "${python_bin}" -m py_compile \
    services/g1_telemetry/isaac_rehearsal.py \
    scripts/capture_isaac_g1_telemetry_artifact.py \
    scripts/run_g1_isaac_telemetry_safety_rehearsal.py > "${compile_log}" 2>&1
  compile_status=$?
else
  test_scope="unittest-discover"
  "${python_bin}" -m unittest discover -s tests -v > "${test_log}" 2>&1
  test_status=$?
  compile_status="not-requested"
fi
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
evidence+=$'VERIFIER_ROLE_BOUNDARY: When visual_check is required, semantic image inspection belongs exclusively to the later independent reviewer stage. The verifier must validate artifact integrity and must not fail solely because that later visual review is pending.\n'
evidence+="UNITTEST_COMMAND_SCOPE=${test_scope}"
evidence+=$'\n'
evidence+="UNITTEST_EXIT_STATUS=${test_status}"
evidence+=$'\nPY_COMPILE_EXIT_STATUS='
evidence+="${compile_status}"
evidence+=$'\nPOLICY_JSON_EXIT_STATUS='
evidence+="${policy_json_status}"
evidence+=$'\nCPP_SYNTAX_EXIT_STATUS='
evidence+="${cpp_syntax_status}"
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
evidence+=$'\nPY_COMPILE_OUTPUT\n'
evidence+="$(head -c 10000 "${compile_log}")"
evidence+=$'\nDIFF_CHECK_OUTPUT\n'
evidence+="$(head -c 10000 "${diff_log}")"
evidence+=$'\nARTIFACT_VERIFICATION_STATUS='
evidence+="${artifact_status}"
evidence+=$'\nARTIFACT_VERIFICATION_OUTPUT\n'
evidence+="$(head -c 10000 "${artifact_log}")"
evidence+=$'\nCANDIDATE_DIFF\n'
evidence+="$(git -c core.quotepath=false diff --no-ext-diff --text HEAD^ HEAD -- 2>/dev/null || true)"
evidence="${evidence[1,90000]}"

print -rn -- "${contract}${evidence}" | "${codex_bin}" "${args[@]}"
