from __future__ import annotations

import json
import os
import re
import uuid
import tempfile,shutil,subprocess,time,math
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping, Sequence

from triagent.adapters.base import AgentResult, AgentStatus, AgentRole
from pydantic import BaseModel,ConfigDict,Field,StrictStr,ValidationError
from typing import Literal
from triagent.adapters.process import ProcessRunner

REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization|credential)", re.IGNORECASE)
_AUTH_FAILURES = (
    "unauthorized", "forbidden", "not logged in", "unauthenticated",
    "authentication required", "login required", "sign-in required", "signin required",
    "invalid token", "expired token", "missing api key", "api key required",
)


def runtime(names: Sequence[str], secret_values: Sequence[str] = ()) -> tuple[ProcessRunner, dict[str, str], tuple[str, ...]]:
    env = {name: os.environ[name] for name in names if os.environ.get(name)}
    secrets = tuple(dict.fromkeys([*secret_values, *env.values()]))
    return ProcessRunner(redactions=secrets), env, secrets


def sanitize(value: object, secrets: Sequence[str], key: str = "") -> object:
    if key and _SECRET_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {str(k): sanitize(v, secrets, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    return value


def read_prompt(request) -> tuple[str | None, AgentResult | None]:
    try:
        task_bytes=request.task_file.read_bytes()
        task_bytes.decode("utf-8")
        handoff_bytes=b""
        if request.role in {AgentRole.VERIFIER, AgentRole.REVIEWER}:
            if request.handoff_file is None: raise ValueError("handoff required")
            handoff_bytes=request.handoff_file.read_bytes(); json.loads(handoff_bytes.decode("utf-8"))
        payload=b"TRIAGENT_INPUT_V1\nTASK\n"+task_bytes+b"\nHANDOFF\n"+handoff_bytes
        return payload.decode("utf-8"), None
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None, AgentResult(status=AgentStatus.FAILED, summary="Unable to read task file")

class FindingPayload(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    severity: Literal["BLOCKER","MAJOR","MINOR","NOTE"]
    code: StrictStr=Field(min_length=1,max_length=100)
    message: StrictStr=Field(min_length=1,max_length=500)
class CanonicalPayload(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    status: Literal["passed","failed"]
    evidence:list[StrictStr]=Field(default_factory=list,max_length=50)
    artifacts:list[StrictStr]=Field(default_factory=list,max_length=50)
    actual_usd:float|None=Field(default=None,ge=0,allow_inf_nan=False,strict=True)
class ReviewPayload(CanonicalPayload):
    findings:list[FindingPayload]=Field(default_factory=list,max_length=50)
class CursorEnvelope(BaseModel):
    model_config=ConfigDict(extra="allow",strict=True)
    type: Literal["result"]
    subtype: Literal["success"]
    is_error: Literal[False]
    result: StrictStr
    total_cost_usd: float|None=Field(default=None,ge=0,allow_inf_nan=False,strict=True)

class TransportSecurityError(RuntimeError): pass
def _windows_acl(directory:Path,file:Path)->bool:
    user=os.environ.get("USERNAME")
    if not user:return False
    for target in (directory,file):
        grant=f"{user}:(OI)(CI)F" if target==directory else f"{user}:F"
        result=subprocess.run(["icacls",str(target),"/inheritance:r","/grant:r",grant,"SYSTEM:F"],capture_output=True,text=True,check=False)
        if result.returncode!=0:return False
        check=subprocess.run(["icacls",str(target)],capture_output=True,text=True,check=False)
        lowered=check.stdout.lower()
        if check.returncode!=0 or any(x in lowered for x in ("everyone:","authenticated users:","builtin\\users:")):return False
    return True
@contextmanager
def external_restricted_input(request,acl_verifier=None):
    payload,error=read_prompt(request)
    if error: yield None,error; return
    directory=Path(tempfile.mkdtemp(prefix="triagent-private-")); file=directory/"input.txt"
    try:
        file.write_bytes(payload.encode("utf-8"))
        if os.name=="nt": secured=(acl_verifier or _windows_acl)(directory,file)
        else:
            directory.chmod(0o700); file.chmod(0o600); secured=(directory.stat().st_mode&0o777)==0o700 and (file.stat().st_mode&0o777)==0o600
        if not secured: raise TransportSecurityError("input transport ACL verification failed")
        yield file,None
    finally:
        for attempt in range(3):
            try: shutil.rmtree(directory); break
            except OSError:
                if attempt==2: raise TransportSecurityError("input transport cleanup failed")
                time.sleep(.01)


def filesystem_probe(
    runner: ProcessRunner,
    argv_prefix: Sequence[str],
    probe_dir: Path,
    env: Mapping[str, str],
    prompt_path=None,
) -> bool:
    nonce = uuid.uuid4().hex
    target = probe_dir / f"triagent-probe-{uuid.uuid4().hex}.txt"
    displayed_path = prompt_path(target) if prompt_path else str(target)
    contract = json.dumps({"path": displayed_path, "nonce": nonce}, separators=(",", ":"))
    prompt = f"Write the exact nonce to the exact file path using a file tool. TRIAGENT_SENTINEL={contract}"
    success = False
    try:
        target.unlink(missing_ok=True)
        result = runner.run([*argv_prefix, prompt], probe_dir, 30, env)
        success = not result.timed_out and result.returncode == 0 and target.read_text(encoding="utf-8") == nonce
    except (OSError, UnicodeError):
        success = False
    try:
        target.unlink(missing_ok=True)
    except OSError:
        return False
    return success


def invoke_codex_jsonl(
    runner: ProcessRunner,
    argv: Sequence[str],
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
    secret_values: Sequence[str] = (), role: AgentRole = AgentRole.VERIFIER, stdin: str | None = None,
) -> AgentResult:
    try:
        process = runner.run(argv, cwd, timeout, env or {}, stdin=stdin)
    except (FileNotFoundError, OSError):
        return AgentResult(status=AgentStatus.UNAVAILABLE, summary="CLI executable is unavailable")
    if process.timed_out:
        return AgentResult(status=AgentStatus.TIMED_OUT, summary="CLI execution timed out")
    if process.returncode != 0:
        diagnostic = f"{process.stdout}\n{process.stderr}".lower()
        auth_code = re.search(r"(?<!\d)(?:401|403)(?!\d)", diagnostic) is not None
        if auth_code or any(marker in diagnostic for marker in _AUTH_FAILURES):
            return AgentResult(status=AgentStatus.UNAVAILABLE, summary="CLI authentication or configuration is unavailable")
        return AgentResult(status=AgentStatus.FAILED, summary="CLI execution failed")
    messages: list[str] = []
    try:
        for line in process.stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError
            item = event.get("item")
            if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                messages.append(item["text"])
                continue
            if event.get("type") == "agent_message" and isinstance(event.get("message"), str):
                messages.append(event["message"])
                continue
            if event.get("type") == "message" and event.get("role") == "assistant" and isinstance(event.get("content"), list):
                text = "".join(part.get("text", "") for part in event["content"] if isinstance(part, dict) and part.get("type") in {"output_text", "text"})
                if text:
                    messages.append(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="CLI returned malformed structured output")
    if not messages:
        return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="CLI returned no final agent message")
    message = sanitize(messages[-1], secret_values)
    assert isinstance(message, str)
    try: payload=json.loads(message)
    except json.JSONDecodeError: return AgentResult(status=AgentStatus.INVALID_OUTPUT,summary="CLI returned non-JSON canonical output")
    try: data=_canonical(role,payload)
    except ValueError: return AgentResult(status=AgentStatus.INVALID_OUTPUT,summary="CLI returned invalid canonical output")
    actual=payload.get("actual_usd") if isinstance(payload.get("actual_usd"),(int,float)) else None
    return AgentResult(status=AgentStatus.SUCCEEDED, data=data,actual_usd=actual)

def _canonical(role: AgentRole, payload: dict) -> dict:
    try: parsed=(ReviewPayload if role is AgentRole.REVIEWER else CanonicalPayload).model_validate(payload)
    except ValidationError as error: raise ValueError from error
    data={"status":parsed.status,"summary_code":{AgentRole.IMPLEMENTER:"completed",AgentRole.VERIFIER:"verified",AgentRole.REVIEWER:"clean"}[role],"evidence":parsed.evidence,"artifacts":parsed.artifacts}
    if role is AgentRole.REVIEWER: data["findings"]=[x.model_dump() for x in parsed.findings]
    return data


def invoke_json(
    runner: ProcessRunner,
    argv: Sequence[str],
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
    secret_values: Sequence[str] = (), role: AgentRole = AgentRole.IMPLEMENTER, stdin: str | None = None, cursor_envelope: bool = False,
) -> AgentResult:
    try:
        process = runner.run(argv, cwd, timeout, env or {}, stdin=stdin)
    except (FileNotFoundError, OSError):
        return AgentResult(status=AgentStatus.UNAVAILABLE, summary="CLI executable is unavailable")
    if process.timed_out:
        return AgentResult(status=AgentStatus.TIMED_OUT, summary="CLI execution timed out")
    if process.returncode != 0:
        diagnostic = f"{process.stdout}\n{process.stderr}".lower()
        auth_code = re.search(r"(?<!\d)(?:401|403)(?!\d)", diagnostic) is not None
        if auth_code or any(marker in diagnostic for marker in _AUTH_FAILURES):
            return AgentResult(status=AgentStatus.UNAVAILABLE, summary="CLI authentication or configuration is unavailable")
        return AgentResult(status=AgentStatus.FAILED, summary="CLI execution failed")
    try:
        payload = json.loads(process.stdout)
    except (json.JSONDecodeError, TypeError):
        return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="CLI returned malformed structured output")
    if not isinstance(payload, dict):
        return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="CLI returned invalid structured output")
    payload = sanitize(payload, secret_values)
    assert isinstance(payload, dict)
    actual=payload.get("actual_usd") if isinstance(payload.get("actual_usd"),(int,float)) and not isinstance(payload.get("actual_usd"),bool) and math.isfinite(payload.get("actual_usd")) else None
    if cursor_envelope:
        try:
            envelope=CursorEnvelope.model_validate(payload); payload=json.loads(envelope.result)
        except (ValidationError,json.JSONDecodeError): return AgentResult(status=AgentStatus.INVALID_OUTPUT,summary="Cursor returned invalid result envelope")
        actual=envelope.total_cost_usd
    try: data=_canonical(role,payload)
    except ValueError: return AgentResult(status=AgentStatus.INVALID_OUTPUT,summary="CLI returned invalid canonical output")
    return AgentResult(
        status=AgentStatus.SUCCEEDED,
        data=data,
        stdout="",
        stderr="",
        actual_usd=actual,
    )


def probe(runner: ProcessRunner, argv: Sequence[str], env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    try:
        result = runner.run(argv, Path.cwd(), 10, env or {})
    except (FileNotFoundError, OSError):
        return False, ""
    return (not result.timed_out and result.returncode == 0), result.stdout.strip()
