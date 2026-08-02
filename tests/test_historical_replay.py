from __future__ import annotations

import json
from pathlib import Path

from triagent.historical_replay import HistoricalReplayError, load_fixture, replay_fixture, write_report


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "historical-replay-v2.json"


def test_known_failure_fixture_is_complete_and_declarative() -> None:
    suite, cases, fixture_digest = load_fixture(FIXTURE)

    assert suite == "triagent-v2-known-failure-replay"
    assert len(cases) == 12
    assert len(fixture_digest) == 64
    assert {case.identifier for case in cases} >= {
        "codex-home-environment-contamination", "antigravity-one-transient-unavailable-then-success", "fragmented-opencode-json-output",
        "controller-crash-after-durable-result", "runtime-manifest-drift-on-resume",
    }


def test_historical_replay_is_provider_free_and_writes_auditable_report(tmp_path: Path) -> None:
    report = replay_fixture(FIXTURE, project_root=ROOT, timeout_seconds=30)
    destination = tmp_path / "historical-replay-report.json"
    write_report(report, destination)
    saved = json.loads(destination.read_text(encoding="utf-8"))

    assert saved == report
    assert report["status"] == "passed"
    assert report["provider_calls"] == 0
    assert report["code_home_contamination_injected"] is True
    assert len(report["cases"]) == 12
    for case in report["cases"]:
        assert case["status"] == "passed" and case["output_sha256"]
        assert "output" not in case


def test_replay_fixture_rejects_unsafe_nodeids(tmp_path: Path) -> None:
    fixture = tmp_path / "unsafe.json"
    fixture.write_text('{"schema_version":1,"suite":"x","cases":[{"id":"x","class":"x","nodeids":["/bin/sh"]}]}')

    try:
        load_fixture(fixture)
    except HistoricalReplayError as error:
        assert "invalid" in str(error)
    else:
        raise AssertionError("unsafe node id was accepted")
