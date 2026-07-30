from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import re

import pytest

from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters._cli import invoke_json, probe
from triagent.adapters.base import AgentRequest, AgentRole, AgentStatus
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.deepseek import DeepSeekAdapter
from triagent.adapters.process import ProcessResult, ProcessRunner


class FakeRunner:
    def __init__(self, *results: ProcessResult | BaseException) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], Path, float, dict[str, str]]] = []

    def run(self, argv, cwd, timeout, env_allowlist, stdin=None):
        self.stdin = stdin
        self.calls.append((list(argv), Path(cwd), timeout, dict(env_allowlist)))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class SentinelRunner(FakeRunner):
    def run(self, argv, cwd, timeout, env_allowlist, stdin=None):
        result = super().run(argv, cwd, timeout, env_allowlist, stdin)
        prompt = argv[-1]
        match = re.search(r'TRIAGENT_SENTINEL=(\{.*\})', prompt)
        if match:
            contract = json.loads(match.group(1))
            path = contract["path"]
            wsl = re.fullmatch(r"/mnt/([a-zA-Z])(/.*)", path)
            if wsl:
                path = f"{wsl.group(1).upper()}:{wsl.group(2)}"
            Path(path).write_text(contract["nonce"], encoding="utf-8")
        return result


class DirectorySentinelRunner(FakeRunner):
    def run(self, argv, cwd, timeout, env_allowlist, stdin=None):
        result = super().run(argv, cwd, timeout, env_allowlist, stdin)
        match = re.search(r'TRIAGENT_SENTINEL=(\{.*\})', argv[-1])
        if match:
            contract = json.loads(match.group(1))
            path = contract["path"]
            wsl = re.fullmatch(r"/mnt/([a-zA-Z])(/.*)", path)
            if wsl:
                path = f"{wsl.group(1).upper()}:{wsl.group(2)}"
            Path(path).mkdir()
        return result


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> ProcessResult:
    return ProcessResult(returncode, stdout, stderr, False)


def review_payload() -> dict:
    return {
        "status": "passed",
        "evidence": [],
        "artifacts": [],
        "findings": [],
    }


@pytest.mark.parametrize(
    "rendered",
    [
        lambda value: value,
        lambda value: f"```json\n{value}\n```",
        lambda value: f"```\n{value}\n```",
        lambda value: f"```JSON\n{value}\n```",
    ],
)
def test_plain_json_transport_accepts_raw_or_one_complete_fence(tmp_path: Path, rendered) -> None:
    payload = json.dumps(review_payload())
    result = invoke_json(
        FakeRunner(completed(rendered(payload))),
        ["agy"],
        tmp_path,
        1,
        role=AgentRole.REVIEWER,
        allow_fenced_json=True,
    )
    assert result.status is AgentStatus.SUCCEEDED
    assert result.data["findings"] == []


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        'prefix\n```json\n{"status":"passed"}\n```',
        '```json\n{"status":"passed"}\n```\ntrailing',
        '```json\n{}\n```\n```json\n{}\n```',
    ],
)
def test_plain_json_transport_rejects_noncanonical_wrapping(tmp_path: Path, stdout: str) -> None:
    result = invoke_json(FakeRunner(completed(stdout)), ["agy"], tmp_path, 1, role=AgentRole.REVIEWER, allow_fenced_json=True)
    assert result.status is AgentStatus.INVALID_OUTPUT
    assert result.data == {"diagnostic_code": "json-malformed"}


def test_plain_json_transport_rejects_non_object_json(tmp_path: Path) -> None:
    result = invoke_json(FakeRunner(completed("[]")), ["agy"], tmp_path, 1, role=AgentRole.REVIEWER, allow_fenced_json=True)
    assert result.status is AgentStatus.INVALID_OUTPUT
    assert result.data == {"diagnostic_code": "json-non-object"}


def test_plain_json_transport_keeps_canonical_schema_strict(tmp_path: Path) -> None:
    result = invoke_json(FakeRunner(completed("{}")), ["agy"], tmp_path, 1, role=AgentRole.REVIEWER, allow_fenced_json=True)
    assert result.status is AgentStatus.INVALID_OUTPUT
    assert result.data == {"diagnostic_code": "canonical-output-invalid"}


def test_fenced_json_transport_requires_explicit_adapter_opt_in(tmp_path: Path) -> None:
    payload = json.dumps(review_payload())
    result = invoke_json(
        FakeRunner(completed(f"```json\n{payload}\n```")),
        ["not-antigravity"],
        tmp_path,
        1,
        role=AgentRole.REVIEWER,
    )
    assert result.status is AgentStatus.INVALID_OUTPUT
    assert result.data == {}


@pytest.fixture
def agent_request(tmp_path: Path) -> AgentRequest:
    task = tmp_path / "task.txt"
    task.write_text("fix the quoted value: '$(touch nope)'", encoding="utf-8")
    return AgentRequest(
        role=AgentRole.IMPLEMENTER,
        task_file=task,
        workdir=tmp_path,
        output_schema="result-v1",
        timeout_seconds=10,
    )


def test_antigravity_adapter_enables_strict_fenced_json_transport(agent_request: AgentRequest) -> None:
    handoff = agent_request.workdir / "handoff.json"
    handoff.write_text("{}", encoding="utf-8")
    review_request = agent_request.model_copy(update={"role": AgentRole.REVIEWER, "handoff_file": handoff})
    payload = json.dumps({
        "status": "passed",
        "evidence": [],
        "artifacts": [],
        "findings": [],
    })
    result = AntigravityAdapter(
        runner=FakeRunner(completed(f"```json\n{payload}\n```")),
        acl_verifier=lambda directory, file: True,
    ).run(review_request)
    assert result.status is AgentStatus.SUCCEEDED


def test_antigravity_adapter_forwards_ssh_session_for_oauth_selection(
    agent_request: AgentRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "client 12345 server 22")
    handoff = agent_request.workdir / "handoff.json"
    handoff.write_text("{}", encoding="utf-8")
    runner = FakeRunner(completed(json.dumps(review_payload())))

    result = AntigravityAdapter(
        runner=runner,
        acl_verifier=lambda directory, file: True,
    ).run(agent_request.model_copy(update={
        "role": AgentRole.REVIEWER,
        "handoff_file": handoff,
    }))

    assert result.status is AgentStatus.SUCCEEDED
    assert runner.calls[0][3]["SSH_CONNECTION"] == "client 12345 server 22"


def test_codex_capabilities_use_read_only_probes(tmp_path: Path) -> None:
    runner = FakeRunner(completed("codex-cli 1.2\n"), completed("Logged in\n"))
    capabilities = CodexAdapter(runner=runner).capabilities()
    assert capabilities.available and capabilities.authenticated and capabilities.headless
    assert capabilities.version == "codex-cli 1.2"
    assert [call[0] for call in runner.calls] == [
        ["codex.exe", "--version"],
        ["codex.exe", "login", "status"],
    ]


def test_capability_probe_uses_30_second_timeout() -> None:
    runner = FakeRunner(completed("ready"))

    available, output = probe(runner, ["provider", "status"])

    assert available is True
    assert output == "ready"
    assert runner.calls[0][2] == 30


def test_missing_binary_is_unavailable(agent_request: AgentRequest) -> None:
    runner = FakeRunner(FileNotFoundError("not installed"))
    result = CodexAdapter(runner=runner).run(agent_request)
    assert result.status is AgentStatus.UNAVAILABLE


@pytest.mark.parametrize(
    ("process", "status"),
    [
        (ProcessResult(None, "", "", True), AgentStatus.TIMED_OUT),
        (completed(returncode=2, stderr="bad invocation"), AgentStatus.FAILED),
        (completed(returncode=1, stderr="authentication required"), AgentStatus.UNAVAILABLE),
        (completed("not json"), AgentStatus.INVALID_OUTPUT),
    ],
)
def test_codex_maps_process_failures(agent_request: AgentRequest, process: ProcessResult, status: AgentStatus) -> None:
    result = CodexAdapter(runner=FakeRunner(process)).run(agent_request)
    assert result.status is status


@pytest.mark.parametrize("message", ["HTTP 401", "403 Forbidden", "unauthorized", "invalid token", "expired token", "missing API key", "login required", "sign-in required"])
def test_auth_and_configuration_failures_are_unavailable(agent_request: AgentRequest, message: str) -> None:
    result = CodexAdapter(runner=FakeRunner(completed(returncode=1, stderr=message))).run(agent_request)
    assert result.status is AgentStatus.UNAVAILABLE


def test_result_recursively_redacts_secret_values_and_keys(agent_request: AgentRequest) -> None:
    secret = "sk-review-secret"
    payload = {"summary": f"used {secret}", "nested": {"api_key": secret, "note": secret}, "items": [{"accessToken": secret}]}
    result = AntigravityAdapter(runner=FakeRunner(completed(json.dumps(payload))), secret_values=[secret]).run(agent_request)
    serialized = result.model_dump_json()
    assert secret not in serialized
    assert "nested" not in result.data
    assert result.stdout == result.stderr == ""


def test_unknown_secret_in_secret_key_and_raw_vendor_error_never_escape(agent_request: AgentRequest) -> None:
    unknown = "vendor-unknown-secret"
    success = AntigravityAdapter(runner=FakeRunner(completed(json.dumps({"summary": "ok", "nested": {"credential": unknown}})))).run(agent_request)
    failure = CodexAdapter(runner=FakeRunner(completed(returncode=2, stderr=unknown))).run(agent_request)
    assert unknown not in success.model_dump_json()
    assert unknown not in failure.model_dump_json()
    assert success.stdout == success.stderr == failure.stdout == failure.stderr == ""


def test_default_adapter_allowlists_and_redacts_known_environment_secret(agent_request: AgentRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "environment-openai-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": secret}}
    runner = FakeRunner(completed(json.dumps(event)))
    result = CodexAdapter(runner=runner).run(agent_request)
    assert runner.calls[0][3] == {"OPENAI_API_KEY": secret}
    assert secret not in result.model_dump_json()


def test_codex_verification_uses_workspace_write_and_parses_jsonl_event_stream(agent_request: AgentRequest) -> None:
    stream = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "private"}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done", "metadata": {"api_key": "unknown-secret"}}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
    ])
    runner = FakeRunner(completed(stream))
    result = CodexAdapter(runner=runner).run(agent_request)
    argv = runner.calls[0][0]
    assert argv[:4] == ["codex.exe", "exec", "--sandbox", "workspace-write"]
    assert argv[4:6] == ["-C", str(agent_request.workdir)]
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "--json" in argv
    assert result.status is AgentStatus.INVALID_OUTPUT
    assert result.summary == "CLI returned non-JSON canonical output"
    assert result.data == {}
    assert "unknown-secret" not in result.model_dump_json()


@pytest.mark.parametrize("stream", ['{"type":"thread.started"}\nnot-json', '{"type":"thread.started"}\n{"type":"turn.completed"}'])
def test_codex_jsonl_rejects_malformed_or_no_message_stream(agent_request: AgentRequest, stream: str) -> None:
    result = CodexAdapter(runner=FakeRunner(completed(stream))).run(agent_request)
    assert result.status is AgentStatus.INVALID_OUTPUT


def test_cursor_uses_wsl_argv_and_does_not_interpolate_prompt(agent_request: AgentRequest) -> None:
    nested=json.dumps({"status":"passed","evidence":[],"artifacts":[],"changed_paths":[]}); runner = FakeRunner(completed(json.dumps({"type":"result","subtype":"success","is_error":False,"result":nested})))
    result = CursorAdapter(runner=runner).run(agent_request)
    argv = runner.calls[0][0]
    assert argv[:5] == ["wsl.exe", "--distribution", "Ubuntu-24.04", "--exec", "bash"]
    assert "--input-file" not in argv and "--output-format" in argv and "json" in argv
    assert "$(touch nope)" not in " ".join(argv)
    assert "$(touch nope)" not in argv[6]
    assert result.status is AgentStatus.SUCCEEDED


def test_cursor_headless_run_trusts_controller_created_worktree(agent_request: AgentRequest) -> None:
    nested = json.dumps({"status": "passed", "evidence": [], "artifacts": [], "changed_paths": []})
    runner = FakeRunner(completed(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": nested})))

    CursorAdapter(runner=runner).run(agent_request)

    assert "--trust" in runner.calls[0][0]


def test_cursor_accepts_free_text_result_as_transport_success_without_persisting_vendor_text(agent_request: AgentRequest) -> None:
    vendor_text = "completed but not canonical"
    runner = FakeRunner(completed(json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": vendor_text,
    })))

    result = CursorAdapter(runner=runner).run(agent_request)

    assert result.status is AgentStatus.SUCCEEDED
    assert result.data == {
        "status": "passed",
        "summary_code": "completed",
        "evidence": [],
        "artifacts": [],
    }
    assert vendor_text not in result.model_dump_json()


def test_cursor_still_rejects_invalid_outer_envelope(agent_request: AgentRequest) -> None:
    runner = FakeRunner(completed(json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "ignored",
    })))

    result = CursorAdapter(runner=runner).run(agent_request)

    assert result.status is AgentStatus.INVALID_OUTPUT
    assert result.data == {"diagnostic_code": "cursor-envelope-invalid"}


def test_cursor_capabilities_never_receive_deepseek_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    runner = FakeRunner(completed("cursor-agent 1"), completed("authenticated"))
    caps = CursorAdapter(runner=runner).capabilities()
    assert caps.available is True
    assert len(runner.calls) == 2
    assert runner.calls[0][3] == {"CURSOR_API_KEY": "cursor-secret", "WSLENV": "CURSOR_API_KEY/u"}
    assert "deepseek-secret" not in repr(runner.calls)


def test_antigravity_print_mode_never_skips_permissions(agent_request: AgentRequest) -> None:
    runner = FakeRunner(completed('{"status":"passed","evidence":[],"artifacts":[],"changed_paths":[]}'))
    result = AntigravityAdapter(runner=runner,acl_verifier=lambda directory,file: True).run(agent_request)
    argv = runner.calls[0][0]
    assert argv[:2] == ["agy.exe", "-p"]
    assert "--mode" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert result.status is AgentStatus.SUCCEEDED


def test_antigravity_empty_stdout_has_specific_recoverable_diagnostic(
    agent_request: AgentRequest,
) -> None:
    handoff = agent_request.workdir / "handoff.json"
    handoff.write_text("{}", encoding="utf-8")
    result = AntigravityAdapter(
        runner=FakeRunner(completed("")),
        acl_verifier=lambda directory, file: True,
    ).run(agent_request.model_copy(update={
        "role": AgentRole.REVIEWER,
        "handoff_file": handoff,
    }))

    assert result.status is AgentStatus.INVALID_OUTPUT
    assert result.data == {"diagnostic_code": "agy-empty-output"}


def test_antigravity_capabilities_use_only_bounded_version_probe_and_auth_is_unknown() -> None:
    runner = FakeRunner(completed("agy 1.2.3"))

    caps = AntigravityAdapter(runner=runner, command=["/opt/agy"]).capabilities()

    assert [call[0] for call in runner.calls] == [["/opt/agy", "--version"]]
    assert runner.calls[0][2] == 30
    assert caps.installed is True
    assert caps.authenticated is None
    assert caps.ready is None
    assert caps.available is False
    assert all("auth" not in argument.lower() for call in runner.calls for argument in call[0])


def test_antigravity_timeout_never_infers_authentication() -> None:
    runner = FakeRunner(ProcessResult(None, "", "", True))

    caps = AntigravityAdapter(runner=runner).capabilities()

    assert caps.installed is False
    assert caps.authenticated is None
    assert caps.ready is None
    assert len(runner.calls) == 1


def test_deepseek_defaults_disabled_runs_only_local_version_probe() -> None:
    runner = FakeRunner(completed("1.18.4"))
    caps = DeepSeekAdapter(runner=runner).capabilities()
    assert caps.available is False
    assert caps.enabled is False
    assert caps.diagnostic_code == "deepseek-disabled"
    assert len(runner.calls) == 1


def test_cursor_wslenv_contains_only_explicit_allowlisted_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "nope")
    runner = FakeRunner(completed("cursor-agent 1"), completed("authenticated"))
    CursorAdapter(runner=runner).capabilities()
    expected = {"CURSOR_API_KEY": "cursor-secret", "WSLENV": "CURSOR_API_KEY/u"}
    assert runner.calls[0][3] == expected
    assert runner.calls[0][0][:5] == ["wsl.exe", "--distribution", "Ubuntu-24.04", "--exec", "bash"]


@pytest.mark.parametrize("message,status", [("error 4012", AgentStatus.FAILED), ("build 4030", AgentStatus.FAILED), ("HTTP 401", AgentStatus.UNAVAILABLE), ("status=403", AgentStatus.UNAVAILABLE)])
def test_auth_status_codes_require_numeric_boundaries(agent_request: AgentRequest, message: str, status: AgentStatus) -> None:
    result = CodexAdapter(runner=FakeRunner(completed(returncode=1, stderr=message))).run(agent_request)
    assert result.status is status


@pytest.mark.parametrize("adapter", [CodexAdapter, CursorAdapter, AntigravityAdapter])
def test_task_file_read_errors_return_structured_failure(tmp_path: Path, adapter) -> None:
    request = AgentRequest(role=AgentRole.IMPLEMENTER, task_file=tmp_path / "missing.txt", workdir=tmp_path, output_schema="result-v1", timeout_seconds=10)
    result = adapter().run(request)
    assert result.status is AgentStatus.FAILED

def test_deepseek_incomplete_live_gates_precede_task_file_access(tmp_path: Path) -> None:
    request = AgentRequest(role=AgentRole.IMPLEMENTER, task_file=tmp_path / "missing.txt", workdir=tmp_path, output_schema="result-v1", timeout_seconds=10)
    assert DeepSeekAdapter(enabled=True).run(request).status is AgentStatus.UNAVAILABLE


def test_process_runner_uses_safe_baseline_and_does_not_leak_unrelated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGENT_UNRELATED_SECRET", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "allowed-auth")
    code = "import json,os; print(json.dumps({k:os.environ.get(k) for k in ['PATH','HOME','USERPROFILE','TEMP','TMP','OPENAI_API_KEY','TRIAGENT_UNRELATED_SECRET']}))"
    result = ProcessRunner(redactions=["allowed-auth"]).run([sys.executable, "-c", code], tmp_path, 10, {"OPENAI_API_KEY": os.environ["OPENAI_API_KEY"]})
    output = json.loads(result.stdout)
    assert output["PATH"]
    assert output.get("HOME") or output.get("USERPROFILE")
    assert output.get("TEMP") or output.get("TMP")
    assert output["OPENAI_API_KEY"] == "[REDACTED]"
    assert output["TRIAGENT_UNRELATED_SECRET"] is None


def test_process_runner_supplies_temp_when_parent_has_no_temp_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    code = "import json,os; print(json.dumps({'TEMP':os.environ.get('TEMP'),'TMP':os.environ.get('TMP')}))"
    result = ProcessRunner().run([sys.executable, "-c", code], tmp_path, 10, {})
    output = json.loads(result.stdout)
    assert output.get("TEMP") or output.get("TMP")


@pytest.mark.live_cli
def test_live_codex_capabilities_only_when_selected(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("-m") or "live_cli" not in request.config.getoption("-m"):
        pytest.skip("select explicitly with -m live_cli")
    assert isinstance(CodexAdapter().capabilities().available, bool)


@pytest.mark.live_cli
@pytest.mark.parametrize("adapter", [CursorAdapter, AntigravityAdapter])
def test_live_adapter_capabilities_only_when_selected(request: pytest.FixtureRequest, adapter) -> None:
    if not request.config.getoption("-m") or "live_cli" not in request.config.getoption("-m"):
        pytest.skip("select explicitly with -m live_cli")
    assert isinstance(adapter().capabilities().available, bool)


@pytest.mark.live_cli
def test_live_deepseek_requires_explicit_enablement(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("-m") or "live_cli" not in request.config.getoption("-m"):
        pytest.skip("select explicitly with -m live_cli")
    if os.environ.get("TRIAGENT_ENABLE_DEEPSEEK_LIVE") != "1":
        pytest.skip("set TRIAGENT_ENABLE_DEEPSEEK_LIVE=1 to enable DeepSeek live probing")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY is not configured")
    if os.environ.get("TRIAGENT_DEEPSEEK_BILLING_CONFIRMED") != "1":
        pytest.skip("set TRIAGENT_DEEPSEEK_BILLING_CONFIRMED=1 only after confirming billing ownership")
    assert isinstance(DeepSeekAdapter(enabled=True, billing_confirmed=True, live_confirmed=True).capabilities().available, bool)
