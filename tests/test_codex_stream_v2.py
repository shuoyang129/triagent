from __future__ import annotations

import json
from pathlib import Path

from triagent.adapters.base import AgentRequest, AgentRole, AgentStatus
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.process import ProcessResult, StreamingProcessResult


def _request(tmp_path: Path) -> AgentRequest:
    task = tmp_path / "task.txt"
    handoff = tmp_path / "handoff.json"
    task.write_text("verify this offline fixture", encoding="utf-8")
    handoff.write_text(json.dumps({"final_diff": "offline"}), encoding="utf-8")
    return AgentRequest(
        role=AgentRole.VERIFIER,
        task_file=task,
        handoff_file=handoff,
        workdir=tmp_path,
        output_schema="offline-verifier-v1",
        timeout_seconds=17,
    )


def _jsonl(payload: dict[str, object]) -> str:
    return "\n".join((
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}}),
    )) + "\n"


class _LegacyRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[tuple] = []

    def run(self, argv, cwd, timeout, env, stdin=None):
        self.calls.append((list(argv), Path(cwd), timeout, dict(env), stdin))
        return self.result


class _FakeStreamingRunner:
    """Offline-only test double; no binary or provider is invoked."""

    def __init__(self, output: str, *, timed_out: bool = False) -> None:
        self.output = output
        self.timed_out = timed_out
        self.calls: list[tuple] = []
        self.progress: list[bool] = []
        self.terminal: list[bool] = []

    def run(self, argv, cwd, policy, env, stdin=None, *, is_progress=None, is_terminal_result=None):
        self.calls.append((list(argv), Path(cwd), policy, dict(env), stdin))
        # Deliberately split JSON records, proving the stream classifier never
        # accepts arbitrary bytes as progress and recognizes a final message.
        for part in (self.output[:9], self.output[9:31], self.output[31:]):
            if is_progress is not None:
                self.progress.append(is_progress("stdout", part))
            if is_terminal_result is not None:
                self.terminal.append(is_terminal_result("stdout", part))
        return StreamingProcessResult(0, self.output, "", self.timed_out, (), "hard-timeout" if self.timed_out else None, any(self.terminal), False)


def test_codex_stream_v2_is_explicit_and_default_remains_legacy(tmp_path: Path) -> None:
    payload = {"status": "passed", "evidence": [], "artifacts": []}
    legacy = _LegacyRunner(ProcessResult(0, _jsonl(payload), "", False))
    stream = _FakeStreamingRunner(_jsonl(payload))

    result = CodexAdapter(runner=legacy, stream_runner=stream, command=("codex-offline",)).run(_request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert len(legacy.calls) == 1
    assert stream.calls == []


def test_codex_stream_v2_uses_same_jsonl_wire_contract_with_offline_fake(tmp_path: Path) -> None:
    payload = {"status": "passed", "evidence": ["offline"], "artifacts": []}
    stream = _FakeStreamingRunner(_jsonl(payload))

    result = CodexAdapter(stream_v2=True, stream_runner=stream, command=("codex-offline",)).run(_request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert result.data == {"status": "passed", "evidence": ["offline"], "artifacts": [], "summary_code": "verified"}
    argv, cwd, policy, env, stdin = stream.calls[0]
    assert argv == ["codex-offline", "exec", "--sandbox", "workspace-write", "-C", str(tmp_path), "--json", "-"]
    assert cwd == tmp_path and policy.hard_timeout == 17 and policy.idle_timeout == 17
    assert env == {} and stdin.startswith("TRIAGENT_CONTROLLER_PROMPT_V2")
    assert any(stream.progress) and stream.terminal[-1] is True


def test_codex_stream_v2_rejects_malformed_jsonl_without_legacy_fallback(tmp_path: Path) -> None:
    stream = _FakeStreamingRunner('{"type":"thread.started"}\nnot-json\n')

    result = CodexAdapter(stream_v2=True, stream_runner=stream, command=("codex-offline",)).run(_request(tmp_path))

    assert result.status is AgentStatus.INVALID_OUTPUT
    assert result.summary == "CLI returned malformed structured output"


def test_codex_stream_v2_maps_stream_timeout_without_provider_call(tmp_path: Path) -> None:
    stream = _FakeStreamingRunner("", timed_out=True)

    result = CodexAdapter(stream_v2=True, stream_runner=stream, command=("codex-offline",)).run(_request(tmp_path))

    assert result.status is AgentStatus.TIMED_OUT
    assert len(stream.calls) == 1
