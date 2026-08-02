"""Offline Cursor stream-v2 migration contracts.

No test in this module starts Cursor, runs a capability probe, or contacts a
provider.  The doubles model only the local adapter's final envelope.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from triagent.adapters.base import AgentRequest, AgentRole, AgentStatus
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.process import ProcessResult, StreamingProcessResult


def _request(root: Path) -> AgentRequest:
    task = root / "task.yaml"
    task.write_text("goal: cursor stream fixture\n", encoding="utf-8")
    return AgentRequest(
        role=AgentRole.IMPLEMENTER,
        task_file=task,
        handoff_file=None,
        workdir=root,
        output_schema="implementation-result-v1",
        timeout_seconds=19,
    )


def _envelope(result: str = "vendor output must not persist") -> str:
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": result, "total_cost_usd": 0.25,
    })


class _LegacyRunner:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def run(self, argv, cwd, timeout, env, stdin=None):
        self.calls.append((list(argv), Path(cwd), timeout, dict(env), stdin))
        return ProcessResult(0, _envelope(), "", False)


class _OfflineStream:
    """A delayed final envelope, without a subprocess or capability probe."""

    def __init__(self, output: str, *, timed_out: bool = False) -> None:
        self.output, self.timed_out = output, timed_out
        self.calls: list[tuple] = []
        self.progress: list[bool] = []
        self.terminal: list[bool] = []

    def run(self, argv, cwd, policy, env, stdin=None, *, is_progress=None, is_terminal_result=None, on_event=None):
        self.calls.append((list(argv), Path(cwd), policy, dict(env), stdin))
        # The first two chunks simulate a slow local wrapper: neither may be
        # mistaken for arbitrary provider progress.  Only its final envelope
        # renews liveness and begins the bounded finalization interval.
        cuts = (self.output[:5], self.output[5:23], self.output[23:])
        for chunk in cuts:
            self.progress.append(is_progress("stdout", chunk) if is_progress else False)
            self.terminal.append(is_terminal_result("stdout", chunk) if is_terminal_result else False)
        return StreamingProcessResult(
            None if self.timed_out else 0, self.output, "", self.timed_out,
            (), "hard-timeout" if self.timed_out else None,
            any(self.terminal), False,
        )


def test_cursor_stream_v2_is_explicit_default_stays_legacy(tmp_path: Path) -> None:
    legacy, stream = _LegacyRunner(), _OfflineStream(_envelope())

    result = CursorAdapter(runner=legacy, stream_runner=stream, command=("cursor-offline",)).run(_request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert len(legacy.calls) == 1 and stream.calls == []


def test_cursor_stream_v2_delayed_final_envelope_never_probes_or_leaks_vendor_text(tmp_path: Path) -> None:
    stream = _OfflineStream(_envelope("CURSOR_VENDOR_PRIVATE_TEXT"))
    # No legacy runner is passed: if stream-v2 tried an ambient probe, the
    # fake stream call accounting below would expose it.  Its sole call is run.
    result = CursorAdapter(stream_v2=True, stream_runner=stream, command=("cursor-offline",)).run(_request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert result.data == {"status": "passed", "summary_code": "completed", "evidence": [], "artifacts": []}
    assert "CURSOR_VENDOR_PRIVATE_TEXT" not in result.model_dump_json()
    assert len(stream.calls) == 1
    assert stream.progress[:2] == [False, False]
    assert stream.progress[-1] is True and stream.terminal[-1] is True


def test_cursor_stream_v2_preserves_sandboxed_argv_and_finalization_policy(tmp_path: Path) -> None:
    stream = _OfflineStream(_envelope())
    command = ("cursor-offline", "--auto-review", "--sandbox", "enabled")

    assert CursorAdapter(stream_v2=True, stream_runner=stream, command=command).run(_request(tmp_path)).status is AgentStatus.SUCCEEDED

    argv, cwd, policy, env, stdin = stream.calls[0]
    assert argv == [*command, "--trust", "--print", "--output-format", "json"]
    assert cwd == tmp_path and env == {} and stdin is not None
    assert policy.hard_timeout == 19 and policy.idle_timeout == 19
    assert policy.finalize_grace == 19


def test_cursor_stream_v2_timeout_does_not_fall_back_to_legacy(tmp_path: Path) -> None:
    stream = _OfflineStream("", timed_out=True)
    result = CursorAdapter(stream_v2=True, stream_runner=stream, command=("cursor-offline",)).run(_request(tmp_path))
    assert result.status is AgentStatus.TIMED_OUT and len(stream.calls) == 1


def test_cursor_stream_v2_is_git_quiet_and_cannot_change_a_stable_candidate(tmp_path: Path) -> None:
    """Transport supervision has no git side effect before orchestration commits."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "offline@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "offline"], cwd=tmp_path, check=True)
    (tmp_path / "candidate.txt").write_text("stable\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=tmp_path, check=True)
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, text=True, capture_output=True).stdout

    result = CursorAdapter(stream_v2=True, stream_runner=_OfflineStream(_envelope()), command=("cursor-offline",)).run(_request(tmp_path))

    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, text=True, capture_output=True).stdout
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, check=True, text=True, capture_output=True).stdout
    assert result.status is AgentStatus.SUCCEEDED and after == before
    # task.yaml is controller input, not a candidate mutation.
    assert status == "?? task.yaml\n"
