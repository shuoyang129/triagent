from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
AGY_ADAPTER = ROOT / "scripts" / "agy-review-adapter.zsh"
CODEX_ADAPTER = ROOT / "scripts" / "codex-verify-adapter.zsh"
CURSOR_ADAPTER = ROOT / "scripts" / "cursor-agent-adapter.zsh"
FORCE_ADAPTER = ROOT / "scripts" / "cursor-synthetic-force-adapter.zsh"


def _fake_cursor(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "fake-cursor"
    argv_log = tmp_path / "argv.json"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["FAKE_CURSOR_ARGV_LOG"], "w", encoding="utf-8") as stream:
    json.dump(sys.argv[1:], stream)
print(json.dumps({
    "status": "passed",
    "evidence": ["fake evidence"],
    "artifacts": [],
    "changed_paths": ["app.py"],
}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, argv_log


def test_cursor_adapter_rewrites_text_and_emits_cursor_envelope(tmp_path: Path) -> None:
    fake_cursor, argv_log = _fake_cursor(tmp_path)
    environment = {
        **os.environ,
        "CURSOR_AGENT_BIN": str(fake_cursor),
        "FAKE_CURSOR_ARGV_LOG": str(argv_log),
    }

    result = subprocess.run(
        [str(CURSOR_ADAPTER), "--print", "--output-format", "json"],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(argv_log.read_text(encoding="utf-8")) == [
        "--print",
        "--output-format",
        "text",
    ]
    envelope = json.loads(result.stdout)
    assert envelope["type"] == "result"
    assert envelope["subtype"] == "success"
    assert envelope["is_error"] is False
    assert json.loads(envelope["result"])["changed_paths"] == ["app.py"]


def test_synthetic_force_adapter_rejects_normal_repository() -> None:
    result = subprocess.run(
        [str(FORCE_ADAPTER), "--force", "--sandbox", "enabled"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert "refusing non-synthetic worktree" in result.stderr


def test_codex_adapter_embeds_controller_verification_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    (repo / "health.py").write_bytes(b'def health_status():\r\n    return "ok"\r\n')
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_health.py").write_bytes(
        b"import unittest\r\nfrom health import health_status\r\n"
        b"class TestHealth(unittest.TestCase):\r\n"
        b"    def test_ok(self): self.assertEqual(health_status(), 'ok')\r\n"
    )
    subprocess.run(["git", "add", "health.py", "tests/test_health.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=repo, check=True)

    fake_codex = tmp_path / "fake-codex"
    prompt_log = tmp_path / "prompt.txt"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
prompt = sys.stdin.read()
open(os.environ["FAKE_CODEX_PROMPT_LOG"], "w", encoding="utf-8").write(prompt)
payload = {"status":"passed","evidence":["embedded"],"artifacts":[]}
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":json.dumps(payload)}}))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    result = subprocess.run(
        [str(CODEX_ADAPTER), "exec", "--json", "-"],
        cwd=repo,
        input="TRIAGENT_CONTROLLER_PROMPT_V2\n",
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "CODEX_BIN": str(fake_codex),
            "FAKE_CODEX_PROMPT_LOG": str(prompt_log),
        },
        check=False,
    )

    assert result.returncode == 0, result.stderr
    prompt = prompt_log.read_text(encoding="utf-8")
    assert "UNITTEST_EXIT_STATUS=0" in prompt
    assert "GIT_DIFF_CHECK_EXIT_STATUS=0" in prompt
    assert "CANDIDATE_COMMIT_NONEMPTY=true" in prompt
    event = json.loads(result.stdout)
    assert json.loads(event["item"]["text"])["status"] == "passed"


def test_codex_adapter_has_exact_m16_verification_scope() -> None:
    text = CODEX_ADAPTER.read_text(encoding="utf-8")
    assert "m34-focused-22" in text
    assert "tests/test_g1_pc1_blackbox_boundary.py" in text
    assert "22 passed" in text
    assert "M34_PNG_ACTUAL_SHA256" in text
    assert "VISUAL_SEMANTIC_REVIEW_DEFERRED_TO_REQUIRED_ANTIGRAVITY_STAGE=true" in text
    assert "semantic image inspection belongs exclusively to the later independent reviewer stage" in text
    assert "m33-exact-36-265-149" in text
    assert "tests.test_g1_physical_single_writer_remediation" in text
    assert "expected 265/265" in text
    assert "Ran 36 tests" in text
    assert "Ran 265 tests" in text
    assert "services/g1_telemetry/physical_single_writer_remediation.py" in text
    assert "scripts/evaluate_g1_physical_single_writer_remediation.py" in text
    assert "scripts/collect_g1_remote_owner_attestation.py" in text
    assert "tests/test_g1_physical_single_writer_remediation.py" in text
    assert "m32-exact-34-229-149" in text
    assert "tests.test_g1_physical_motion_admission_reeval" in text
    assert "expected 229/229" in text
    assert "expected 149/149" in text
    assert "Ran 34 tests" in text
    assert "Ran 229 tests" in text
    assert "Ran 149 tests" in text
    assert "Ran 15 tests" in text
    assert "services/g1_telemetry/physical_motion_admission_reeval.py" in text
    assert "scripts/evaluate_g1_physical_motion_admission_reeval.py" in text
    assert "m28-exact-173" in text
    assert "tests/test_g1_physical_hold_rehearsal.py" in text
    assert "tests/test_m28_live_observers.py" in text
    assert "EVIDENCE_VALID_FAIL_CLOSED" in text
    assert "m25-g1-readonly-20260728-115500-v1" in text
    assert "m26-g1-odom-20260728-132400-v2" in text
    assert "173 passed, 116 subtests passed" in text
    assert "m16-exact-36" in text
    assert "m17-focused" in text
    assert "m18-exact-38" in text
    assert "m19-exact-46" in text
    assert "m20-focused" in text
    assert "m21-focused" in text
    assert "m22-focused" in text
    assert "m23-focused" in text
    assert "m24-exact-364" in text
    assert "m25-exact-83" in text
    assert "m26-exact-62" in text
    assert "m27-exact-141" in text
    assert (
        "tests.test_g1_physical_motion_admission "
        "tests.test_g1_physical_odom tests.test_g1_physical_readonly"
    ) in text
    assert "services/g1_telemetry/physical_motion_admission.py" in text
    assert "scripts/evaluate_g1_physical_motion_admission.py" in text
    assert "tests/test_g1_physical_motion_admission.py" in text
    assert "tests.test_g1_physical_odom tests.test_g1_physical_readonly" in text
    assert "services/g1_telemetry/physical_odom.py" in text
    assert "scripts/collect_g1_physical_odom.py" in text
    assert "tests/test_g1_physical_odom.py" in text
    assert 'm25_python_bin="/usr/bin/python3"' in text
    assert (
        "tests.test_g1_physical_readonly tests.test_g1_physical_readiness "
        "tests.test_g1_telemetry_contract tests.test_repository_policy"
    ) in text
    assert "services/g1_telemetry/physical_readonly.py" in text
    assert "scripts/collect_g1_physical_readonly.py" in text
    assert "tests/test_g1_physical_readonly.py" in text
    assert 'm24_python_bin="/usr/bin/python3"' in text
    assert "tests.test_g1_isaac_retry_safety_candidate_admission" in text
    assert "services/full_e2e/retry_safety_candidate_admission.py" in text
    assert "scripts/evaluate_g1_isaac_retry_safety_candidate_admission.py" in text
    assert "tests/test_g1_isaac_retry_safety_candidate_admission.py" in text
    assert 'm23_python_bin="/usr/bin/python3"' in text
    assert "tests.test_g1_isaac_retry_safety_candidate" in text
    assert "services/full_e2e/retry_safety_candidate.py" in text
    assert "scripts/capture_isaac_g1_retry_safety_candidate.py" in text
    assert "tests/test_g1_isaac_retry_safety_candidate.py" in text
    assert 'm22_python_bin="/usr/bin/python3"' in text
    assert "tests.test_g1_isaac_retry_safety_admission" in text
    assert "services/full_e2e/retry_safety_admission.py" in text
    assert "scripts/evaluate_g1_isaac_retry_safety_admission.py" in text
    assert "tests/test_g1_isaac_retry_safety_admission.py" in text
    assert 'm21_python_bin="/usr/bin/python3"' in text
    assert "tests.test_g1_isaac_denial_retry" in text
    assert "services/full_e2e/denial_retry_gate.py" in text
    assert "scripts/evaluate_g1_isaac_denial_retry.py" in text
    assert "tests/test_g1_isaac_denial_retry.py" in text
    assert 'm20_python_bin="/usr/bin/python3"' in text
    assert "tests.test_g1_isaac_denial_feedback" in text
    assert "services/full_e2e/denial_feedback.py" in text
    assert "scripts/finalize_g1_isaac_denial_feedback.py" in text
    assert "tests/test_g1_isaac_denial_feedback.py" in text
    assert 'm19_python_bin="/usr/bin/python3"' in text
    assert "tests.test_g1_isaac_premotion_denial" in text
    assert "services/full_e2e/premotion_denial.py" in text
    assert "scripts/evaluate_g1_isaac_premotion_denial.py" in text
    assert "tests/test_g1_isaac_premotion_denial.py" in text
    assert "tests.test_g1_isaac_premotion_safety" in text
    assert "services/g1_telemetry/premotion.py" in text
    assert "scripts/evaluate_g1_isaac_premotion.py" in text
    assert "tests/test_g1_isaac_premotion_safety.py" in text
    assert "tests.test_g1_isaac_support_safety" in text
    assert "services/g1_telemetry/support.py" in text
    assert "scripts/capture_isaac_g1_support_artifact.py" in text
    assert "scripts/evaluate_g1_isaac_support.py" in text
    assert "tests/test_g1_isaac_support_safety.py" in text
    assert "tests.test_g1_isaac_posture_safety" in text
    assert "services/g1_telemetry/__init__.py" in text
    assert "services/g1_telemetry/posture.py" in text
    assert "scripts/evaluate_g1_isaac_posture.py" in text
    assert "tests/test_g1_isaac_posture_safety.py" in text


def test_agy_adapter_propagates_safe_auth_marker(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    source = tmp_path / "input.txt"
    source.write_text("TRIAGENT_CONTROLLER_PROMPT_V2\n", encoding="utf-8")
    fake_agy = tmp_path / "fake-agy"
    fake_agy.write_text(
        "#!/bin/sh\nprintf 'Authentication required. Please log in.\\n' >&2\nexit 1\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)

    result = subprocess.run(
        [str(AGY_ADAPTER), "-p", f"Read and follow the complete instructions in this local file: {source}"],
        cwd=repo,
        text=True,
        capture_output=True,
        env={**os.environ, "AGY_BIN": str(fake_agy)},
        check=False,
    )

    assert result.returncode == 1
    assert "Authentication required" in result.stderr
    marker = "TriAgent AGY diagnostic saved for local diagnosis: "
    diagnostic = Path(result.stderr.split(marker, 1)[1].splitlines()[0])
    diagnostic.unlink()
