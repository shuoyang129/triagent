"""Offline evidence validation for the v2 promotion ladder.

This module is intentionally a policy evaluator, not a deployment tool.  It
does not construct adapters, start processes, inspect secrets, or modify entry
points.  In particular, a passing cutover evaluation is evidence for an
operator review; it can never replace the operator's explicit acceptance or
perform a cutover.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
import os
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class PromotionEvidenceError(ValueError):
    """Promotion evidence is malformed or contains unsafe claims."""


STAGES = (
    "unit-tests",
    "adapter-contracts",
    "fake-three-stage-workflow",
    "historical-replay",
    "isolated-no-provider-synthetic",
    "isolated-real-provider-synthetic",
    "low-risk-non-robot-project",
    "humanoid-offline-read-only",
    "ordinary-new-humanoid-task",
)

ROLLOUT_STAGES = ("A", "B", "C", "D")

SAFETY_GATES = (
    "full-tests",
    "no-secret-leak",
    "no-residual-provider-process",
    "no-duplicate-provider-call-after-recovery",
    "no-candidate-misattribution",
    "legacy-fallback",
    "original-tasks-readable-and-resumable",
)

REPLAY_CASES = (
    "slow-cursor-version-status-probe",
    "cursor-filesystem-probe-bound",
    "antigravity-oauth-failure",
    "antigravity-mcp-stall-wrapper-alive",
    "antigravity-final-output-delayed-exit",
    "antigravity-one-transient-unavailable-then-success",
    "fragmented-opencode-json-output",
    "deepseek-transport-cleanup-invalid-output-after-edits",
    "codex-home-environment-contamination",
    "provider-descendant-after-timeout",
    "controller-crash-after-durable-result",
    "runtime-manifest-drift-on-resume",
)

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,99}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def evidence_digest(evidence: Mapping[str, Any]) -> str:
    """Content digest excluding the self-referential ``digest`` field."""
    payload = dict(evidence)
    payload.pop("digest", None)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PromotionEvidenceError(f"{label} must be an object")
    return value


def _required_keys(value: Mapping[str, Any], *, required: set[str], allowed: set[str], label: str) -> None:
    if set(value) - allowed or required - set(value):
        raise PromotionEvidenceError(f"{label} has unknown or missing fields")


def _text(value: object, label: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\n" in value or "\x00" in value:
        raise PromotionEvidenceError(f"{label} must be a bounded non-empty string")
    return value


def _id(value: object, label: str) -> str:
    text = _text(value, label, limit=100)
    if not _ID.fullmatch(text):
        raise PromotionEvidenceError(f"{label} has invalid characters")
    return text


def _digest(value: object, label: str) -> str:
    text = _text(value, label, limit=64)
    if not _SHA256.fullmatch(text):
        raise PromotionEvidenceError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _artifact_path(value: object, label: str) -> str:
    """Validate a repository-relative evidence descriptor path."""
    text = _text(value, label, limit=256)
    if text.startswith("/") or "\\" in text or any(part in {"", ".", ".."} for part in text.split("/")):
        raise PromotionEvidenceError(f"{label} must be a safe relative path")
    return text


def _evidence_items(value: object, allowed_ids: tuple[str, ...], label: str, *, required: bool) -> set[str]:
    if not isinstance(value, list):
        raise PromotionEvidenceError(f"{label} must be an array")
    found: set[str] = set()
    for item in value:
        record = _object(item, label)
        _required_keys(record, required={"id", "passed", "artifact_sha256", "artifact_path"}, allowed={"id", "passed", "artifact_sha256", "artifact_path", "note"}, label=label)
        identifier = _id(record["id"], f"{label}.id")
        if identifier not in allowed_ids:
            raise PromotionEvidenceError(f"{label} has unsupported id: {identifier}")
        if identifier in found:
            raise PromotionEvidenceError(f"{label} has duplicate id: {identifier}")
        if record["passed"] is not True:
            raise PromotionEvidenceError(f"{label} is not passing: {identifier}")
        _digest(record["artifact_sha256"], f"{label}.artifact_sha256")
        _artifact_path(record["artifact_path"], f"{label}.artifact_path")
        if "note" in record:
            _text(record["note"], f"{label}.note", limit=512)
        found.add(identifier)
    if required and found != set(allowed_ids):
        missing = ", ".join(sorted(set(allowed_ids) - found))
        raise PromotionEvidenceError(f"{label} is incomplete: {missing}")
    return found


@dataclass(frozen=True)
class PromotionEvaluation:
    passed: bool
    stage: str
    rollout_stage: str
    evidence_digest: str
    unmet: tuple[str, ...]
    cutover_eligible: bool


def evaluate(evidence: Mapping[str, Any]) -> PromotionEvaluation:
    """Validate a self-contained offline evidence record.

    All passed records must reference a hash of independently retained output;
    the evaluator validates structure and promotion order only.  Trusting that
    output remains an operator/reviewer responsibility.
    """
    record = _object(evidence, "promotion evidence")
    _required_keys(
        record,
        required={"schema_version", "stage", "rollout_stage", "generated_at", "gates", "replays", "prior_stage_digests", "digest"},
        allowed={"schema_version", "stage", "rollout_stage", "generated_at", "gates", "replays", "prior_stage_digests", "read_only_admission", "operator_acceptance", "digest"},
        label="promotion evidence",
    )
    if record["schema_version"] != 1 or isinstance(record["schema_version"], bool):
        raise PromotionEvidenceError("unsupported promotion evidence schema_version")
    stage = _text(record["stage"], "stage")
    rollout_stage = _text(record["rollout_stage"], "rollout_stage")
    if stage not in STAGES:
        raise PromotionEvidenceError("unknown promotion stage")
    if rollout_stage not in ROLLOUT_STAGES:
        raise PromotionEvidenceError("unknown rollout stage")
    timestamp = _text(record["generated_at"], "generated_at", limit=20)
    if not _ISO_UTC.fullmatch(timestamp):
        raise PromotionEvidenceError("generated_at must be UTC second precision")
    _evidence_items(record["gates"], SAFETY_GATES, "gates", required=True)
    _evidence_items(record["replays"], REPLAY_CASES, "replays", required=stage in STAGES[3:])
    prior = record["prior_stage_digests"]
    if not isinstance(prior, list):
        raise PromotionEvidenceError("prior_stage_digests must be an array")
    expected_prior = STAGES[:STAGES.index(stage)]
    if len(prior) != len(expected_prior):
        raise PromotionEvidenceError("prior_stage_digests must cover every earlier stage")
    seen: set[str] = set()
    for item, expected_stage in zip(prior, expected_prior, strict=True):
        prior_record = _object(item, "prior stage")
        _required_keys(prior_record, required={"stage", "digest"}, allowed={"stage", "digest"}, label="prior stage")
        if prior_record["stage"] != expected_stage:
            raise PromotionEvidenceError("prior stage order is invalid")
        digest = _digest(prior_record["digest"], "prior stage digest")
        if digest in seen:
            raise PromotionEvidenceError("prior stage digest is duplicated")
        seen.add(digest)
    supplied_digest = _digest(record["digest"], "digest")
    calculated = evidence_digest(record)
    if supplied_digest != calculated:
        raise PromotionEvidenceError("promotion evidence digest does not match content")
    admission = record.get("read_only_admission")
    if stage == "humanoid-offline-read-only":
        admission_record = _object(admission, "read_only_admission")
        _required_keys(admission_record, required={"outcome"}, allowed={"outcome", "note"}, label="read_only_admission")
        if admission_record["outcome"] not in {"fail-closed", "cleared"}:
            raise PromotionEvidenceError("read_only_admission must record fail-closed or cleared")
    elif admission is not None:
        raise PromotionEvidenceError("read_only_admission is only valid for humanoid-offline-read-only")

    accepted = False
    acceptance = record.get("operator_acceptance")
    if acceptance is not None:
        acceptance_record = _object(acceptance, "operator_acceptance")
        _required_keys(acceptance_record, required={"action", "outcome", "operator", "accepted_at"}, allowed={"action", "outcome", "operator", "accepted_at"}, label="operator_acceptance")
        if acceptance_record["action"] != "cutover" or acceptance_record["outcome"] != "accepted":
            raise PromotionEvidenceError("operator_acceptance must explicitly accept cutover")
        _text(acceptance_record["operator"], "operator_acceptance.operator", limit=100)
        accepted_at = _text(acceptance_record["accepted_at"], "operator_acceptance.accepted_at", limit=20)
        if not _ISO_UTC.fullmatch(accepted_at):
            raise PromotionEvidenceError("operator_acceptance.accepted_at must be UTC second precision")
        accepted = True
    # A single record cannot prove that its claimed prior digests resolve to
    # real earlier records. ``evaluate_chain`` is required for cutover.
    return PromotionEvaluation(True, stage, rollout_stage, supplied_digest, (), False)


def write_artifact_descriptor(
    root: Path | str, *, stage: str, gate: str, raw_log_path: str, command: str,
    captured_at: str | None = None, task_id: str | None = None, profile_digest: str | None = None,
    source_commit: str | None = None, note: str | None = None, sanitized: bool = False,
) -> tuple[str, str]:
    """Atomically bind one reviewed raw log to a promotion gate descriptor.

    This helper never runs a command or inspects a provider. The caller must
    supply a reviewed, sanitized retained output and explicitly attest it.
    """
    base = Path(root).resolve(strict=True)
    checked_stage = _text(stage, "descriptor stage")
    checked_gate = _id(gate, "descriptor gate")
    if checked_stage not in STAGES or checked_gate not in {*SAFETY_GATES, *REPLAY_CASES}:
        raise PromotionEvidenceError("descriptor has unsupported stage or gate")
    raw_relative = _artifact_path(raw_log_path, "descriptor raw_log_path")
    raw_path = (base / raw_relative).resolve()
    if not raw_path.is_relative_to(base) or raw_path.is_symlink() or not raw_path.is_file():
        raise PromotionEvidenceError("descriptor raw log path is unsafe or missing")
    if sanitized is not True:
        raise PromotionEvidenceError("descriptor requires explicit sanitized attestation")
    timestamp = captured_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not _ISO_UTC.fullmatch(_text(timestamp, "descriptor captured_at", limit=20)):
        raise PromotionEvidenceError("descriptor captured_at must be UTC second precision")
    descriptor: dict[str, object] = {
        "schema_version": 1, "stage": checked_stage, "gate": checked_gate, "passed": True,
        "exit_code": 0, "raw_log_path": raw_relative,
        "raw_log_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "sanitized": True, "captured_at": timestamp, "command": _text(command, "descriptor command", limit=512),
    }
    for name, value in {"task_id": task_id, "profile_digest": profile_digest, "source_commit": source_commit, "note": note}.items():
        if value is not None:
            descriptor[name] = _text(value, f"descriptor {name}", limit=512)
    relative = f"artifacts/descriptors/{checked_stage}/{checked_gate}.json"
    target = base / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    payload = canonical_json(descriptor).encode("utf-8") + b"\n"
    temporary.write_bytes(payload)
    os.replace(temporary, target)
    return relative, hashlib.sha256(payload).hexdigest()


def capture_junit_full_tests_descriptor(
    root: Path | str, *, junit_path: str, source_commit: str, captured_at: str | None = None,
) -> tuple[str, str]:
    """Capture a reviewed full-regression JUnit result without running tests."""
    base = Path(root).resolve(strict=True)
    raw_relative = _artifact_path(junit_path, "junit path")
    raw_path = (base / raw_relative).resolve()
    if not raw_path.is_relative_to(base) or raw_path.is_symlink() or not raw_path.is_file():
        raise PromotionEvidenceError("junit path is unsafe or missing")
    raw = raw_path.read_bytes()
    if len(raw) > 2_000_000 or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise PromotionEvidenceError("junit input is unsafe")
    try:
        document = ET.fromstring(raw)
    except ET.ParseError as error:
        raise PromotionEvidenceError("junit input is malformed") from error
    if document.tag not in {"testsuites", "testsuite"} or document.find(".//system-out") is not None or document.find(".//system-err") is not None or document.find(".//properties") is not None:
        raise PromotionEvidenceError("junit input contains unsupported output")
    suites = [node for node in document.iter("testsuite")]
    if not suites:
        raise PromotionEvidenceError("junit input has no test suites")
    def count(attribute: str) -> int:
        values = [suite.get(attribute, "0") for suite in suites]
        if not all(value.isdecimal() for value in values):
            raise PromotionEvidenceError("junit counters are invalid")
        return sum(int(value) for value in values)
    if count("tests") < 1 or count("errors") != 0 or count("failures") != 0:
        raise PromotionEvidenceError("junit full tests did not pass")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise PromotionEvidenceError("junit source_commit must be a full lowercase git SHA")
    return write_artifact_descriptor(
        base, stage="unit-tests", gate="full-tests", raw_log_path=raw_relative,
        command="pytest -q --junitxml=artifacts/unit-tests-current.junit.xml",
        captured_at=captured_at, source_commit=source_commit, sanitized=True,
    )


def verify_artifacts(evidence: Mapping[str, Any], root: Path | str) -> PromotionEvaluation:
    """Verify retained descriptor and raw-log content for every claimed check."""
    result = evaluate(evidence)
    base = Path(root).resolve(strict=True)
    items = [*evidence["gates"], *evidence["replays"]]
    for item in items:
        if not isinstance(item, dict):
            raise PromotionEvidenceError("evidence item must be an object")
        identifier = _id(item.get("id"), "evidence item id")
        relative = _artifact_path(item.get("artifact_path"), "evidence item artifact_path")
        descriptor_path = (base / relative).resolve()
        if not descriptor_path.is_relative_to(base) or descriptor_path.is_symlink() or not descriptor_path.is_file():
            raise PromotionEvidenceError("evidence descriptor path is unsafe or missing")
        if hashlib.sha256(descriptor_path.read_bytes()).hexdigest() != _digest(item.get("artifact_sha256"), "evidence item artifact_sha256"):
            raise PromotionEvidenceError("evidence descriptor digest does not match content")
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PromotionEvidenceError("evidence descriptor is unreadable") from error
        descriptor = _object(descriptor, "evidence descriptor")
        _required_keys(descriptor, required={"schema_version", "stage", "gate", "passed", "exit_code", "raw_log_path", "raw_log_sha256", "sanitized"}, allowed={"schema_version", "stage", "gate", "passed", "exit_code", "raw_log_path", "raw_log_sha256", "sanitized", "captured_at", "command", "task_id", "profile_digest", "source_commit", "note"}, label="evidence descriptor")
        if descriptor["schema_version"] != 1 or descriptor["stage"] != evidence["stage"] or descriptor["gate"] != identifier or descriptor["passed"] is not True or descriptor["exit_code"] != 0 or descriptor["sanitized"] is not True:
            raise PromotionEvidenceError("evidence descriptor does not attest the claimed passing check")
        raw_relative = _artifact_path(descriptor["raw_log_path"], "evidence descriptor raw_log_path")
        raw_path = (base / raw_relative).resolve()
        if not raw_path.is_relative_to(base) or raw_path.is_symlink() or not raw_path.is_file():
            raise PromotionEvidenceError("evidence raw log path is unsafe or missing")
        if hashlib.sha256(raw_path.read_bytes()).hexdigest() != _digest(descriptor["raw_log_sha256"], "evidence descriptor raw_log_sha256"):
            raise PromotionEvidenceError("evidence raw log digest does not match content")
    return result


def evaluate_chain(records: Sequence[Mapping[str, Any]], *, artifact_root: Path | str | None = None) -> PromotionEvaluation:
    """Validate an ordered, content-linked promotion record chain."""
    if not records:
        raise PromotionEvidenceError("promotion evidence chain is empty")
    if len(records) > len(STAGES):
        raise PromotionEvidenceError("promotion evidence chain is too long")
    if artifact_root is not None:
        for record in records:
            verify_artifacts(record, artifact_root)
    prior: list[PromotionEvaluation] = []
    for index, record in enumerate(records):
        result = evaluate(record)
        if result.stage != STAGES[index]:
            raise PromotionEvidenceError("promotion evidence chain stage order is invalid")
        expected = [
            {"stage": earlier.stage, "digest": earlier.evidence_digest}
            for earlier in prior
        ]
        if record.get("prior_stage_digests") != expected:
            raise PromotionEvidenceError("promotion evidence chain digest linkage is invalid")
        prior.append(result)
    final = prior[-1]
    accepted = isinstance(records[-1].get("operator_acceptance"), Mapping)
    read_only_admission = records[STAGES.index("humanoid-offline-read-only")].get("read_only_admission") if len(records) > STAGES.index("humanoid-offline-read-only") else None
    read_only_cleared = isinstance(read_only_admission, Mapping) and read_only_admission.get("outcome") == "cleared"
    eligible = (
        final.stage == STAGES[-1]
        and final.rollout_stage == "D"
        and len(prior) == len(STAGES)
        and accepted
        and artifact_root is not None
        and read_only_cleared
    )
    return PromotionEvaluation(True, final.stage, final.rollout_stage, final.evidence_digest, (), eligible)
