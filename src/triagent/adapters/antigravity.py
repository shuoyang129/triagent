from __future__ import annotations

from collections.abc import Sequence

from triagent.adapters._cli import TransportSecurityError,external_restricted_input,invoke_json, probe, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult, AgentRole, AgentStatus, CostEstimate
from triagent.adapters.process import ProcessResult, ProcessRunner, StreamPolicy, StreamingProcessResult, StreamingProcessRunner


class AntigravityAdapter(AgentAdapter):
    identity = "antigravity"
    allowed_roles = frozenset({AgentRole.REVIEWER})
    def __init__(
        self,
        runner: ProcessRunner | None = None,
        secret_values: Sequence[str] = (),
        command: Sequence[str] = ("agy.exe",),
        estimated_usd: float | None = None,
        acl_verifier=None,
        *,
        stream_v2: bool = False,
        stream_runner: StreamingProcessRunner | None = None,
        stream_policy: StreamPolicy | None = None,
    ) -> None:
        """Create an Antigravity reviewer.

        The v2 stream route is opt-in.  Leaving ``stream_v2`` unset preserves
        the frozen wrapper argv and blocking transport exactly; the flag is
        never inferred from environment or provider state.
        """
        if not isinstance(stream_v2, bool):
            raise TypeError("stream_v2 must be a bool")
        default_runner, self._env, self._secrets = runtime(
            ("AGY_API_KEY", "GOOGLE_API_KEY", "SSH_CONNECTION"),
            secret_values,
        )
        self._runner = runner or default_runner
        self._command = list(command)
        self._estimated_usd=estimated_usd
        self._acl_verifier=acl_verifier
        self._stream_v2 = stream_v2
        self._stream_runner = stream_runner or StreamingProcessRunner(redactions=self._secrets)
        self._stream_policy = stream_policy
    def estimate_cost(self, request): return CostEstimate(self._estimated_usd)

    def capabilities(self) -> AgentCapabilities:
        installed, version = probe(self._runner, [*self._command, "--version"], self._env)
        return AgentCapabilities(available=False, installed=installed, version=version or None, authenticated=None, headless=installed, ready=None)

    def run(self, request: AgentRequest) -> AgentResult:
        try:
            with external_restricted_input(request,self._acl_verifier) as (path,error):
                if error:return error
                instruction=f"Read and follow the complete instructions in this local file: {path}"
                argv = [*self._command, "-p", instruction]
                if not self._stream_v2:
                    return invoke_json(
                        self._runner, argv, request.workdir, request.timeout_seconds,
                        self._env, self._secrets, request.role,
                        allow_fenced_json=True, empty_output_diagnostic="agy-empty-output",
                    )
                return _invoke_agy_stream(
                    self._stream_runner, argv, request.workdir,
                    self._stream_policy or _compat_stream_policy(request.timeout_seconds),
                    self._env, self._secrets, request.role,
                )
        except TransportSecurityError as error:
            return AgentResult(status=AgentStatus.FAILED,summary=error.code,data={"diagnostic_code":error.code})


def _compat_stream_policy(timeout_seconds: float) -> StreamPolicy:
    hard = float(timeout_seconds)
    return StreamPolicy(
        startup_timeout=min(30.0, hard), idle_timeout=hard, hard_timeout=hard,
        finalize_grace=min(60.0, hard), terminate_grace=min(2.0, hard),
    )


def _invoke_agy_stream(
    runner: StreamingProcessRunner,
    argv: Sequence[str],
    cwd,
    policy: StreamPolicy,
    env,
    secrets: Sequence[str],
    role: AgentRole,
) -> AgentResult:
    """Stream the wrapper while retaining its one-object/fenced JSON contract."""
    chunks: list[str] = []
    terminal = False
    provider_output_marker = "TRIAGENT_AGY_PROVIDER_OUTPUT_V1"
    liveness_marker = "TRIAGENT_AGY_LIVENESS_V1"

    def recognizes(stream: str, text: str) -> bool:
        nonlocal terminal
        if stream == "stderr":
            return "TRIAGENT_AGY_PROVIDER_OUTPUT_V1" in text
        if stream != "stdout" or terminal:
            return False
        # The v2-owned wrapper emits this marker only after it has received a
        # provider output record. It contains no provider text, and no wrapper
        # timer can create it. It is meaningful provider progress; ordinary
        # wrapper liveness remains non-progress.
        if sum(map(len, chunks)) + len(text) > 256 * 1024:
            chunks.clear()
            return False
        chunks.append(text)
        candidate = "".join(chunks)
        provider_output_seen = provider_output_marker in candidate
        internal_marker_seen = provider_output_seen or liveness_marker in candidate
        if internal_marker_seen:
            candidate = candidate.replace(provider_output_marker, "")
            candidate = candidate.replace(liveness_marker, "")
            chunks[:] = [candidate]
        candidate = candidate.strip()
        if candidate.startswith("```json") and candidate.endswith("```"):
            candidate = candidate[len("```json"): -3].strip()
        elif candidate.startswith("```") and candidate.endswith("```"):
            candidate = candidate[3: -3].strip()
        try:
            import json
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return provider_output_seen
        if not isinstance(payload, dict):
            return provider_output_seen
        required = {"status", "evidence", "artifacts", "findings"}
        if not required.issubset(payload):
            return provider_output_seen
        terminal = True
        return True

    try:
        result: StreamingProcessResult = runner.run(
            argv, cwd, policy, env, is_progress=recognizes,
            is_terminal_result=lambda _stream, _text: terminal,
        )
    except (FileNotFoundError, OSError):
        return AgentResult(status=AgentStatus.UNAVAILABLE, summary="CLI executable is unavailable")
    if result.timed_out:
        return AgentResult(status=AgentStatus.TIMED_OUT, summary="CLI execution timed out")
    # Reuse the legacy parser with an adapter over the already-completed,
    # in-memory result.  It cannot invoke a provider or perform a fallback.
    class _Completed:
        def run(self, *_args, **_kwargs):
            # Marker records are internal controller progress evidence, not
            # part of AGY's canonical JSON wire contract.
            return ProcessResult(
                result.returncode, result.stdout.replace(provider_output_marker, "").replace(liveness_marker, ""),
                result.stderr, result.timed_out,
            )
    return invoke_json(
        _Completed(), argv, cwd, policy.hard_timeout, env, secrets, role,
        allow_fenced_json=True, empty_output_diagnostic="agy-empty-output",
    )
