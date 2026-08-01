from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from triagent.promotion import (
    PromotionEvidenceError,
    REPLAY_CASES,
    SAFETY_GATES,
    STAGES,
    evaluate,
    evidence_digest,
)


def _hash(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _evidence(stage: str = "unit-tests", rollout: str = "A", *, accepted: bool = False) -> dict[str, object]:
    index = STAGES.index(stage)
    record: dict[str, object] = {
        "schema_version": 1,
        "stage": stage,
        "rollout_stage": rollout,
        "generated_at": "2026-08-01T00:00:00Z",
        "gates": [{"id": item, "passed": True, "artifact_sha256": _hash(item)} for item in SAFETY_GATES],
        "replays": [{"id": item, "passed": True, "artifact_sha256": _hash(item)} for item in REPLAY_CASES] if index >= 3 else [],
        "prior_stage_digests": [{"stage": item, "digest": _hash(item)} for item in STAGES[:index]],
    }
    if accepted:
        record["operator_acceptance"] = {"action": "cutover", "outcome": "accepted", "operator": "operator", "accepted_at": "2026-08-01T00:01:00Z"}
    record["digest"] = evidence_digest(record)
    return record


def test_evaluator_accepts_complete_offline_stage_and_never_implies_cutover() -> None:
    evaluation = evaluate(_evidence())

    assert evaluation.passed is True
    assert evaluation.stage == "unit-tests"
    assert evaluation.cutover_eligible is False


def test_final_stage_requires_explicit_operator_acceptance_for_cutover_eligibility() -> None:
    without_acceptance = evaluate(_evidence(STAGES[-1], "D"))
    with_acceptance = evaluate(_evidence(STAGES[-1], "D", accepted=True))

    assert without_acceptance.cutover_eligible is False
    assert with_acceptance.cutover_eligible is True


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda record: record["gates"].pop(), "gates is incomplete"),
        (lambda record: record.__setitem__("prior_stage_digests", []), "prior_stage_digests must cover"),
        (lambda record: record.__setitem__("digest", "0" * 64), "digest does not match"),
    ],
)
def test_evaluator_rejects_missing_gate_chain_or_tampered_content(mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    record = _evidence("historical-replay")
    mutate(record)

    with pytest.raises(PromotionEvidenceError, match=message):
        evaluate(record)


def test_historical_replay_requires_every_declared_replay_case() -> None:
    record = _evidence("historical-replay")
    replays = record["replays"]
    assert isinstance(replays, list)
    replays.pop()
    record["digest"] = evidence_digest(record)

    with pytest.raises(PromotionEvidenceError, match="replays is incomplete"):
        evaluate(record)


def test_schema_is_present_and_describes_offline_acceptance_boundary() -> None:
    schema = Path(__file__).resolve().parents[1] / "docs/evidence/promotion/v2-promotion-evidence.schema.json"
    parsed = json.loads(schema.read_text(encoding="utf-8"))

    assert parsed["properties"]["operator_acceptance"]["$ref"] == "#/$defs/acceptance"
    assert parsed["$defs"]["acceptance"]["properties"]["action"] == {"const": "cutover"}
