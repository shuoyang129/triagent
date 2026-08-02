from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from triagent.adapters.process import StreamEventKind, StreamPolicy, StreamingProcessRunner, _StreamingRedactor, safe_progress_event_sink


def policy(**overrides: object) -> StreamPolicy:
    values: dict[str, object] = {
        "startup_timeout": 0.25, "idle_timeout": 0.20, "hard_timeout": 1.0,
        "finalize_grace": 0.15, "terminate_grace": 0.20, "max_output_bytes": 64,
    }
    values.update(overrides)
    return StreamPolicy(**values)  # type: ignore[arg-type]


def invoke(tmp_path: Path, script: str, **kwargs: object):
    return StreamingProcessRunner(redactions=["top-secret"]).run(
        [sys.executable, "-u", "-c", script], tmp_path, policy(**kwargs.pop("policy", {})), {}, **kwargs,
    )


def kinds(result) -> list[StreamEventKind]:
    return [event.kind for event in result.events]


def test_streaming_runner_reports_progress_and_terminal_result(tmp_path: Path) -> None:
    result = invoke(
        tmp_path, "import time; print('progress: one', flush=True); time.sleep(.03); print('RESULT: ok', flush=True)",
        is_progress=lambda _stream, text: "progress:" in text,
        is_terminal_result=lambda _stream, text: "RESULT:" in text,
    )
    assert result.returncode == 0 and not result.timed_out and result.terminal_result_seen
    assert kinds(result)[0] is StreamEventKind.STARTED
    assert kinds(result)[-1] is StreamEventKind.COMPLETED
    assert StreamEventKind.LIVENESS in kinds(result)
    assert kinds(result).index(StreamEventKind.PROGRESS) < kinds(result).index(StreamEventKind.TERMINAL_RESULT_SEEN)


def test_streaming_runner_startup_timeout(tmp_path: Path) -> None:
    result = invoke(tmp_path, "import time; time.sleep(2)", policy={"startup_timeout": .05})
    assert result.timed_out and result.timeout_reason == "startup-timeout"
    assert StreamEventKind.COMPLETED in kinds(result)


def test_streaming_runner_idle_timeout_ignores_noisy_liveness(tmp_path: Path) -> None:
    result = invoke(tmp_path, "import time\nfor _ in range(20):\n print('noise', flush=True); time.sleep(.03)")
    assert result.timed_out and result.timeout_reason == "idle-timeout"
    assert StreamEventKind.LIVENESS in kinds(result)
    assert StreamEventKind.PROGRESS not in kinds(result)

def test_streaming_runner_controller_probe_refreshes_on_state_change(tmp_path: Path) -> None:
    marker = tmp_path / "candidate-state"
    previous: str | None = None

    def changed() -> bool:
        nonlocal previous
        current = marker.read_text(encoding="utf-8") if marker.exists() else ""
        if previous is None:
            previous = current
            return False
        if current == previous:
            return False
        previous = current
        return True

    result = invoke(
        tmp_path,
        "import pathlib,time; p=pathlib.Path('candidate-state'); time.sleep(.08); p.write_text('one'); time.sleep(.12); p.write_text('two'); time.sleep(.08)",
        policy={"idle_timeout": .14, "hard_timeout": 1.0},
        progress_probe=changed,
    )

    assert result.returncode == 0 and not result.timed_out
    assert StreamEventKind.PROGRESS in kinds(result)


def test_semantic_event_sink_persists_no_liveness_content(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    result = invoke(
        tmp_path, "import time; print('progress', flush=True); time.sleep(.03)",
        is_progress=lambda _stream, text: "progress" in text,
        on_event=safe_progress_event_sink(events_file),
    )

    assert result.returncode == 0
    persisted = events_file.read_text(encoding="utf-8")
    assert "stream-progress" in persisted and "stream-completed" in persisted
    assert "liveness" not in persisted and "progress\\n" not in persisted


def test_streaming_runner_hard_timeout_wins_over_progress(tmp_path: Path) -> None:
    result = invoke(
        tmp_path, "import time\nwhile True:\n print('progress', flush=True); time.sleep(.02)",
        policy={"idle_timeout": .3, "hard_timeout": .12}, is_progress=lambda _stream, text: "progress" in text,
    )
    assert result.timed_out and result.timeout_reason == "hard-timeout"
    assert StreamEventKind.PROGRESS in kinds(result)


def test_streaming_runner_finalization_is_bounded(tmp_path: Path) -> None:
    result = invoke(
        tmp_path, "import time; print('RESULT', flush=True); time.sleep(2)",
        is_terminal_result=lambda _stream, text: "RESULT" in text,
    )
    assert result.timed_out and result.timeout_reason == "finalize-timeout"
    assert StreamEventKind.FINALIZING in kinds(result)


def test_streaming_runner_redacts_split_secrets_and_bounds_evidence(tmp_path: Path) -> None:
    result = invoke(tmp_path, "import sys; sys.stdout.write('top-'); sys.stdout.flush(); sys.stdout.write('secret' + 'x'*200); sys.stdout.flush()")
    evidence = result.stdout + result.stderr + "".join(event.text for event in result.events)
    assert "top-secret" not in evidence
    assert "[REDACTED]" in evidence
    assert result.output_truncated and len(result.stdout.encode()) <= 64


def test_streaming_runner_terminates_descendant_processes(tmp_path: Path) -> None:
    marker = tmp_path / "descendant.txt"
    child = f"import time; time.sleep(.4); open({str(marker)!r}, 'w').write('orphan')"
    script = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-u', '-c', {child!r}]); time.sleep(2)"
    result = invoke(tmp_path, script, policy={"startup_timeout": .05, "terminate_grace": .1})
    assert result.timed_out and result.timeout_reason == "startup-timeout"
    time.sleep(.55)
    assert not marker.exists()


def test_stream_policy_rejects_invalid_values() -> None:
    try:
        StreamPolicy(0, 1, 1, 1)
    except ValueError as error:
        assert "positive" in str(error)
    else:
        raise AssertionError("invalid policy accepted")


def test_streaming_redactor_without_secrets_flushes_each_chunk() -> None:
    redactor = _StreamingRedactor(())

    assert redactor.feed("first") == "first"
    assert redactor.feed("second") == "second"
