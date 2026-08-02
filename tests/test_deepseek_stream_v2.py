from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from triagent.adapters.base import AgentRequest, AgentRole, AgentStatus
from triagent.adapters.deepseek import DeepSeekAdapter, _OpenCodeStreamClassifier
from triagent.adapters.process import ProcessResult, StreamingProcessResult


MODEL = "deepseek/deepseek-v4-pro"


def _result(stdout: str = "", stderr: str = "", code: int = 0) -> ProcessResult:
    return ProcessResult(code, stdout, stderr, False)


class _ReadinessRunner:
    """Pure local fake: models/probe responses never contact OpenCode."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def run(self, argv, cwd, timeout, env, stdin=None):
        self.calls.append((list(argv), Path(cwd), timeout, dict(env), stdin))
        if argv[-1] == "--version":
            return _result("1.18.4\n")
        if "models" in argv:
            return _result(f"{MODEL}\n")
        if any(str(item).startswith("--file=") for item in argv):
            payload = {"status": "passed", "evidence": [], "artifacts": [], "changed_paths": []}
            return _result(_events(payload))
        match = re.search(r"PATH_JSON=(\".*?\") NONCE=([^ ]+)", argv[-1])
        assert match is not None
        Path(json.loads(match.group(1))).write_text(match.group(2), encoding="utf-8")
        return _result('{"type":"text","part":{"type":"text","text":"ok"}}\n')


class _StreamRunner:
    """Offline streaming double; feeds output in deliberately arbitrary chunks."""

    def __init__(self, output: str, *, code: int = 0, edit: bool = False, timeout_reason: str | None = None) -> None:
        self.output = output
        self.code = code
        self.edit = edit
        self.timeout_reason = timeout_reason
        self.calls: list[tuple] = []
        self.progress: list[bool] = []
        self.terminal: list[bool] = []

    def run(self, argv, cwd, policy, env, stdin=None, *, is_progress=None, is_terminal_result=None):
        self.calls.append((list(argv), Path(cwd), policy, dict(env), stdin))
        if self.edit:
            (Path(cwd) / "base.txt").write_text("edited\n", encoding="utf-8")
        cuts = (self.output[:7], self.output[7:29], self.output[29:])
        for chunk in cuts:
            if is_progress is not None:
                self.progress.append(is_progress("stdout", chunk))
            if is_terminal_result is not None:
                self.terminal.append(is_terminal_result("stdout", chunk))
        return StreamingProcessResult(self.code, self.output, "", self.timeout_reason is not None, (), self.timeout_reason, any(self.terminal), False)


def _request(tmp_path: Path) -> AgentRequest:
    task = tmp_path / "task.json"
    task.write_text('{"goal":"offline test"}', encoding="utf-8")
    return AgentRequest(
        role=AgentRole.IMPLEMENTER,
        agent_identity="deepseek",
        task_file=task,
        workdir=tmp_path,
        output_schema="implementation-result-v1",
        timeout_seconds=19,
    )


def _ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: _StreamRunner, *, stream_v2: bool) -> tuple[DeepSeekAdapter, _ReadinessRunner]:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-secret")
    legacy = _ReadinessRunner()
    adapter = DeepSeekAdapter(
        enabled=True,
        billing_confirmed=True,
        live_confirmed=True,
        command=("opencode-offline",),
        runner=legacy,
        stream_v2=stream_v2,
        stream_runner=stream,
        probe_dir=tmp_path / "probe",
    )
    assert adapter.capabilities().available
    return adapter, legacy


def _events(payload: dict[str, object], *, split: bool = False) -> str:
    text = json.dumps(payload)
    fragments = (text[:13], text[13:]) if split else (text,)
    return "".join(json.dumps({"type": "text", "part": {"type": "text", "text": item}}) + "\n" for item in fragments)


def test_stream_v2_is_explicit_default_uses_legacy_after_offline_readiness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"status": "passed", "evidence": [], "artifacts": [], "changed_paths": []}
    stream = _StreamRunner(_events(payload))
    adapter, legacy = _ready(tmp_path, monkeypatch, stream, stream_v2=False)

    result = adapter.run(_request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert stream.calls == []
    assert len(legacy.calls) == 4  # version, model listing, smoke, implementation


def test_stream_v2_handles_fragmented_structured_output_without_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"status": "passed", "evidence": ["offline"], "artifacts": [], "changed_paths": ["base.txt"]}
    stream = _StreamRunner(_events(payload, split=True))
    adapter, legacy = _ready(tmp_path, monkeypatch, stream, stream_v2=True)

    result = adapter.run(_request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert result.data["changed_paths"] == ["base.txt"]
    assert len(legacy.calls) == 3 and len(stream.calls) == 1
    assert any(stream.progress) and stream.terminal[-1] is True
    argv, cwd, policy, _env, stdin = stream.calls[0]
    assert argv[:2] == ["opencode-offline", "run"] and cwd == tmp_path
    assert policy.hard_timeout == 19 and stdin is None


def test_stream_v2_status_records_do_not_refresh_meaningful_progress() -> None:
    classifier = _OpenCodeStreamClassifier(AgentRole.IMPLEMENTER)

    assert classifier.progress("stdout", "{\"type\":\"status\",\"state\":\"running\"}\n") is False
    assert classifier.progress("stdout", "{\"type\":\"text\",\"part\":{\"type\":\"text\",\"text\":\"   \"}}\n") is False
    assert classifier.progress("stderr", "{\"type\":\"text\",\"part\":{\"type\":\"text\",\"text\":\"provider output\"}}\n") is False
    assert classifier.progress("stdout", "{\"type\":\"text\",\"part\":{\"type\":\"text\",\"text\":\"provider prose\"}}\n") is False
    assert classifier.progress("stdout", "{\"type\":\"text\",\"part\":{\"type\":\"text\",\"text\":\"provider output\"}}\n") is False


def test_stream_v2_invalid_output_recovers_tracked_edit_and_cleans_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    stream = _StreamRunner('{"type":"text","part":{"type":"text","text":"not canonical"}}\n', edit=True)
    adapter, _legacy = _ready(tmp_path, monkeypatch, stream, stream_v2=True)

    result = adapter.run(_request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert result.data["changed_paths"] == ["base.txt"]
    assert not list(tmp_path.glob(".triagent-opencode-input-*.txt"))
    assert not list(tmp_path.glob(".triagent-opencode-output-*.json"))


def test_stream_v2_cleanup_on_transport_failure_and_readiness_never_uses_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _StreamRunner("", code=1)
    adapter, legacy = _ready(tmp_path, monkeypatch, stream, stream_v2=True)
    assert stream.calls == []  # all three readiness steps remain legacy/local

    result = adapter.run(_request(tmp_path))

    assert result.status is AgentStatus.FAILED
    assert result.data == {"diagnostic_code": "deepseek-api-failed"}
    assert len(legacy.calls) == 3 and len(stream.calls) == 1
    assert not list(tmp_path.glob(".triagent-opencode-input-*.txt"))
    assert not list(tmp_path.glob(".triagent-opencode-output-*.json"))


def test_stream_v2_persists_specific_timeout_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _StreamRunner("", timeout_reason="idle-timeout")
    adapter, _legacy = _ready(tmp_path, monkeypatch, stream, stream_v2=True)

    result = adapter.run(_request(tmp_path))

    assert result.status is AgentStatus.TIMED_OUT
    assert result.data == {"diagnostic_code": "deepseek-idle-timeout"}


def test_stream_v2_flag_rejects_non_boolean() -> None:
    with pytest.raises(TypeError, match="stream_v2"):
        DeepSeekAdapter(stream_v2="yes")  # type: ignore[arg-type]
