from __future__ import annotations

from collections.abc import Sequence

import json

from triagent.adapters._cli import canonical_output_schema, invoke_codex_jsonl, invoke_codex_jsonl_stream, parse_codex_final_message, probe, read_prompt, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult, AgentRole, AgentStatus, CostEstimate
from triagent.adapters.process import ProcessRunner, StreamPolicy, StreamingProcessRunner, safe_progress_event_sink


class CodexAdapter(AgentAdapter):
    identity = "codex"
    allowed_roles = frozenset({AgentRole.VERIFIER})
    def __init__(
        self,
        runner: ProcessRunner | None = None,
        secret_values: Sequence[str] = (),
        command: Sequence[str] = ("codex.exe",),
        estimated_usd: float | None = None,
        *,
        stream_v2: bool = False,
        stream_runner: StreamingProcessRunner | None = None,
        stream_policy: StreamPolicy | None = None,
    ) -> None:
        """Create a verifier adapter.

        ``stream_v2`` is deliberately opt-in.  Its default preserves the
        frozen controller's single blocking ``ProcessRunner`` invocation,
        including the exact argv, timeout, and JSONL parsing behavior.  The
        v2 runner is injected for tests and later approval-gated migration;
        it is never selected from ambient environment state.
        """
        if not isinstance(stream_v2, bool):
            raise TypeError("stream_v2 must be a bool")
        default_runner, self._env, self._secrets = runtime(("OPENAI_API_KEY", "CODEX_HOME"), secret_values)
        self._runner = runner or default_runner
        self._command = list(command)
        self._estimated_usd=estimated_usd
        self._stream_v2 = stream_v2
        self._stream_runner = stream_runner or StreamingProcessRunner(redactions=self._secrets)
        self._stream_policy = stream_policy
    def estimate_cost(self, request): return CostEstimate(self._estimated_usd)

    def capabilities(self) -> AgentCapabilities:
        installed, version = probe(self._runner, [*self._command, "--version"], self._env)
        authenticated = False
        if installed:
            authenticated, _ = probe(self._runner, [*self._command, "login", "status"], self._env)
        ready = installed and authenticated
        return AgentCapabilities(available=ready, installed=installed, version=version or None, authenticated=authenticated, headless=installed, ready=ready)

    def run(self, request: AgentRequest) -> AgentResult:
        payload,error=read_prompt(request)
        if error:return error
        sandbox = "read-only" if request.read_only else "workspace-write"
        argv = [*self._command,"exec","--sandbox",sandbox,"-C",str(request.workdir)]
        output_path = None
        if request.read_only:
            schema_path=request.task_file.with_name("codex-output-schema.json")
            schema_path.write_text(json.dumps(canonical_output_schema(request.role),sort_keys=True,separators=(",",":")),encoding="utf-8")
            argv.extend(("--output-schema",str(schema_path)))
            output_path=request.task_file.with_name("codex-final-message.json")
            output_path.unlink(missing_ok=True)
            argv.extend(("--output-last-message",str(output_path)))
        argv.extend(("--json","-"))
        try:
            if not self._stream_v2:
                result = invoke_codex_jsonl(self._runner,argv,request.workdir,request.timeout_seconds,self._env,self._secrets,request.role,stdin=payload)
            else:
                result = invoke_codex_jsonl_stream(
                    self._stream_runner, argv, request.workdir,
                    self._stream_policy or _compat_stream_policy(request.timeout_seconds),
                    self._env, self._secrets, request.role, stdin=payload,
                    on_event=safe_progress_event_sink(request.task_file.parent / "events.jsonl"),
                )
            # Codex writes ``--output-last-message`` only after it has a final
            # answer. A bounded stream may subsequently hit its finalization
            # limit while the CLI is slow to exit. In that case the official
            # final-message artifact is stronger evidence than the transport
            # timeout, and consuming it avoids a duplicate provider call on
            # resume. Do not use this fallback for normal non-read-only
            # calls: only the read-only path asks Codex for the artifact.
            if output_path is not None and result.status in {AgentStatus.INVALID_OUTPUT, AgentStatus.TIMED_OUT}:
                return parse_codex_final_message(output_path, self._secrets, request.role) or result
            return result
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)


def _compat_stream_policy(timeout_seconds: float) -> StreamPolicy:
    """Bound an opt-in stream run by the legacy request timeout.

    This is a compatibility bridge only.  A later persisted timeout-selection
    integration may supply an explicit ``stream_policy``; no implicit matrix
    lookup or provider probe is performed here.
    """
    hard = float(timeout_seconds)
    startup = min(30.0, hard)
    finalize = min(60.0, hard)
    return StreamPolicy(
        startup_timeout=startup,
        idle_timeout=hard,
        hard_timeout=hard,
        finalize_grace=finalize,
        terminate_grace=min(2.0, finalize),
    )
