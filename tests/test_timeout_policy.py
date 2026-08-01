from __future__ import annotations

import pytest

from triagent.timeout_policy import (
    LegacyTimeout,
    Provider,
    Stage,
    StreamTimeouts,
    TaskSize,
    TimeoutMatrix,
    TimeoutPolicyError,
    default_v2_matrix,
    legacy_timeout,
)


def test_default_matrix_has_every_provider_stage_size_cell_and_is_deterministic() -> None:
    first = default_v2_matrix()
    second = default_v2_matrix()

    assert first.persistable() == second.persistable()
    assert len(first.persistable()["cells"]) == len(Provider) * len(Stage) * len(TaskSize)
    selection = first.select("antigravity", "review", "large")
    assert selection.policy.hard_timeout == 2700
    assert len(selection.persistable()["digest"]) == 64


def test_selection_is_strict_and_persistable_digest_changes_with_selector() -> None:
    matrix = default_v2_matrix()

    tiny = matrix.select(Provider.CURSOR, Stage.IMPLEMENT, TaskSize.TINY).persistable()
    small = matrix.select(Provider.CURSOR, Stage.IMPLEMENT, TaskSize.SMALL).persistable()
    assert tiny["digest"] != small["digest"]
    with pytest.raises(TimeoutPolicyError, match="provider must be one of"):
        matrix.select("Cursor", "implement", "tiny")
    with pytest.raises(TimeoutPolicyError, match="stage must be one of"):
        matrix.select("cursor", "deploy", "tiny")


def test_matrix_rejects_missing_or_invalid_cells_fail_closed() -> None:
    one_policy = {"startup_timeout": 60, "idle_timeout": 120, "hard_timeout": 600, "finalize_grace": 60, "terminate_grace": 15}
    with pytest.raises(TimeoutPolicyError, match="missing="):
        TimeoutMatrix.from_mapping({"cursor.implement.tiny": one_policy})
    with pytest.raises(TimeoutPolicyError, match="exactly the five"):
        StreamTimeouts.from_mapping({"startup_timeout": 1})
    with pytest.raises(TimeoutPolicyError, match="cannot exceed hard"):
        StreamTimeouts(61, 30, 60, 30, 15)
    with pytest.raises(TimeoutPolicyError, match="finite positive"):
        StreamTimeouts(float("nan"), 30, 60, 30, 15)


@pytest.mark.parametrize(("environ", "expected"), [({}, 900), ({"TRIAGENT_AGENT_TIMEOUT_SECONDS": "60"}, 60), ({"TRIAGENT_AGENT_TIMEOUT_SECONDS": "3600"}, 3600)])
def test_legacy_timeout_preserves_900_second_default(environ: dict[str, str], expected: int) -> None:
    policy = legacy_timeout(environ)
    assert policy.seconds == expected
    assert policy.persistable()["kind"] == "legacy-global-timeout"
    assert len(policy.persistable()["digest"]) == 64


@pytest.mark.parametrize("raw", ["59", "3601", "1.5", "not-a-number"])
def test_legacy_timeout_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(TimeoutPolicyError):
        legacy_timeout({"TRIAGENT_AGENT_TIMEOUT_SECONDS": raw})
    with pytest.raises(TimeoutPolicyError):
        LegacyTimeout(900.0)  # type: ignore[arg-type]
