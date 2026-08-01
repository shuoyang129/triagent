from __future__ import annotations

import json
from pathlib import Path

from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.base import AgentRequest, AgentRole, AgentStatus
from triagent.adapters.process import ProcessResult, StreamingProcessResult


def _request(root: Path) -> AgentRequest:
    task = root / "task.txt"
    handoff = root / "handoff.json"
    task.write_text("offline AGY review", encoding="utf-8")
    handoff.write_text("{}", encoding="utf-8")
    return AgentRequest(
        role=AgentRole.REVIEWER, task_file=task, handoff_file=handoff,
        workdir=root, output_schema="offline-review-v1", timeout_seconds=19,
    )


def _payload(*, evidence: list[str] | None = None) -> str:
    return json.dumps({"status": "passed", "evidence": evidence or [], "artifacts": [], "findings": []})


class _LegacyRunner:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def run(self, argv, cwd, timeout, env, stdin=None):
        self.calls.append((list(argv), Path(cwd), timeout, dict(env), stdin))
        return ProcessResult(0, _payload(), "", False)


class _OfflineStream:
    """Fake transport only: it never creates a process or contacts AGY."""

    def __init__(self, stdout: str, *, stderr: str = "", returncode: int = 0, timed_out: bool = False) -> None:
        self.stdout, self.stderr, self.returncode, self.timed_out = stdout, stderr, returncode, timed_out
        self.calls: list[tuple] = []
        self.progress: list[bool] = []
        self.terminal: list[bool] = []

    def run(self, argv, cwd, policy, env, stdin=None, *, is_progress=None, is_terminal_result=None):
        self.calls.append((list(argv), Path(cwd), policy, dict(env), stdin))
        # Fragmentation demonstrates terminal recognition before a deliberately
        # delayed fake process exit; arbitrary chunks are not progress.
        thirds = (self.stdout[:7], self.stdout[7:23], self.stdout[23:])
        for part in thirds:
            if is_progress is not None:
                self.progress.append(is_progress("stdout", part))
            if is_terminal_result is not None:
                self.terminal.append(is_terminal_result("stdout", part))
        return StreamingProcessResult(
            self.returncode, self.stdout, self.stderr, self.timed_out, (),
            "idle-timeout" if self.timed_out else None, any(self.terminal), False,
        )


def test_agy_stream_v2_is_explicit_and_default_is_legacy(tmp_path: Path) -> None:
    legacy = _LegacyRunner()
    stream = _OfflineStream(_payload())

    result = AntigravityAdapter(
        runner=legacy, stream_runner=stream, command=("agy-offline",),
        acl_verifier=lambda *_: True,
    ).run(_request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert len(legacy.calls) == 1 and stream.calls == []


def test_agy_stream_v2_preserves_ssh_context_and_detects_terminal_before_delayed_exit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "client 111 server 22")
    stream = _OfflineStream(_payload())

    result = AntigravityAdapter(
        stream_v2=True, stream_runner=stream, command=("agy-offline",),
        acl_verifier=lambda *_: True,
    ).run(_request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    argv, cwd, policy, env, _stdin = stream.calls[0]
    assert argv[:2] == ["agy-offline", "-p"] and cwd == tmp_path
    assert env == {"SSH_CONNECTION": "client 111 server 22"}
    assert policy.hard_timeout == 19
    assert any(stream.progress) and stream.terminal[-1] is True


def test_agy_stream_v2_oauth_unavailable_preserves_private_diagnostics(tmp_path: Path) -> None:
    secret = "private-oauth-detail"
    stream = _OfflineStream("", stderr=f"Authentication required: {secret}", returncode=1)

    result = AntigravityAdapter(
        stream_v2=True, stream_runner=stream, command=("agy-offline",),
        acl_verifier=lambda *_: True,
    ).run(_request(tmp_path))

    assert result.status is AgentStatus.UNAVAILABLE
    assert secret not in result.model_dump_json()


def test_agy_stream_v2_mcp_stall_and_empty_output_are_not_success(tmp_path: Path) -> None:
    stalled = AntigravityAdapter(
        stream_v2=True, stream_runner=_OfflineStream("MCP waiting", timed_out=True),
        command=("agy-offline",), acl_verifier=lambda *_: True,
    ).run(_request(tmp_path))
    empty = AntigravityAdapter(
        stream_v2=True, stream_runner=_OfflineStream(""),
        command=("agy-offline",), acl_verifier=lambda *_: True,
    ).run(_request(tmp_path))

    assert stalled.status is AgentStatus.TIMED_OUT
    assert empty.status is AgentStatus.INVALID_OUTPUT
    assert empty.data == {"diagnostic_code": "agy-empty-output"}


def test_agy_stream_v2_accepts_fenced_json_and_redacts_payload(tmp_path: Path) -> None:
    secret = "agy-stream-secret"
    stream = _OfflineStream(f"```json\n{_payload(evidence=[secret])}\n```")

    result = AntigravityAdapter(
        stream_v2=True, stream_runner=stream, command=("agy-offline",),
        secret_values=(secret,), acl_verifier=lambda *_: True,
    ).run(_request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert secret not in result.model_dump_json()
    assert result.data["evidence"] == ["[REDACTED]"]


def test_v2_agy_stream_path_never_names_original_wrapper() -> None:
    root = Path(__file__).resolve().parents[1]
    original = "/home/ys/works/robots/triagent/scripts/agy-review-adapter.zsh"
    wrapper = root / "scripts" / "agy-review-adapter.zsh"
    source = (root / "src" / "triagent" / "adapters" / "antigravity.py").read_text(encoding="utf-8")

    assert wrapper.exists()
    assert str(wrapper) != original
    assert original not in source
