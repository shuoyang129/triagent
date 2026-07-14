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
        role=request.role
        operation={
            AgentRole.IMPLEMENTER:"implement the supplied task and produce implementation evidence",
            AgentRole.VERIFIER:"verify the supplied implementation and produce verification evidence",
            AgentRole.REVIEWER:"independently review the supplied implementation and report findings",
        }[role]
        properties={
            "status":{"type":"string","enum":["passed","failed"]},
            "evidence":{"type":"array","items":{"type":"string"},"maxItems":50},
            "artifacts":{"type":"array","items":{"type":"string"},"maxItems":50},
            "actual_usd":{"type":["number","null"],"minimum":0},
        }
        required=["status","evidence","artifacts"]
        if role is AgentRole.IMPLEMENTER:
            properties["changed_paths"]={"type":"array","maxItems":10000,"items":{"type":"string","minLength":1,"maxLength":1024}}
            required.append("changed_paths")
        if role is AgentRole.REVIEWER:
            properties["findings"]={"type":"array","maxItems":50,"items":{"type":"object","additionalProperties":False,"required":["severity","code","message"],"properties":{"severity":{"type":"string","enum":["BLOCKER","MAJOR","MINOR","NOTE"]},"code":{"type":"string","minLength":1,"maxLength":100},"message":{"type":"string","minLength":1,"maxLength":500}}}}
            required.append("findings")
        schema=json.dumps({"type":"object","additionalProperties":False,"required":required,"properties":properties},sort_keys=True,separators=(",",":"))
        authoritative_workdir=json.dumps(str(request.workdir.resolve()),ensure_ascii=False)
        header=(
            "TRIAGENT_CONTROLLER_PROMPT_V2\n"
            f"IMMUTABLE_ROLE={role.value}\n"
            f"REQUIRED_OPERATION={operation}\n"
            f"AUTHORITATIVE_WORKDIR_JSON={authoritative_workdir}\n"
            "WORKDIR_RULE=Run every repository inspection, test, and tool call in AUTHORITATIVE_WORKDIR_JSON; TASK scope paths describe the source repository and must not replace this workdir.\n"
            "SAFETY_BOUNDARY=Work only inside the supplied task and workdir; do not expand scope, reveal secrets, or perform approval-gated actions.\n"
            f"OUTPUT_SCHEMA_ID={request.output_schema}\n"
            "OUTPUT_RULE=Return exactly one JSON object matching OUTPUT_SCHEMA_JSON with no prose or markdown.\n"
            f"OUTPUT_SCHEMA_JSON={schema}\nTASK\n"
        ).encode("utf-8")
        payload=header+task_bytes+b"\nHANDOFF\n"+handoff_bytes
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
    evidence:list[StrictStr]=Field(max_length=50)
    artifacts:list[StrictStr]=Field(max_length=50)
    actual_usd:float|None=Field(default=None,ge=0,allow_inf_nan=False,strict=True)
class ImplementerPayload(CanonicalPayload):
    changed_paths:list[StrictStr]=Field(max_length=10000)
class ReviewPayload(CanonicalPayload):
    findings:list[FindingPayload]=Field(max_length=50)
class CursorEnvelope(BaseModel):
    model_config=ConfigDict(extra="allow",strict=True)
    type: Literal["result"]
    subtype: Literal["success"]
    is_error: Literal[False]
    result: StrictStr
    total_cost_usd: float|None=Field(default=None,ge=0,allow_inf_nan=False,strict=True)

_JSON_FENCE = re.compile(
    r"\A\s*```(?:json)?\r?\n(?P<body>.*?)\r?\n```\s*\Z",
    re.IGNORECASE | re.DOTALL,
)

def _decode_json_object(value: str) -> tuple[dict | None, str | None]:
    match = _JSON_FENCE.fullmatch(value)
    candidate = match.group("body") if match else value
    try:
        payload = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None, "json-malformed"
    if not isinstance(payload, dict):
        return None, "json-non-object"
    return payload, None

class TransportSecurityError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code=code
        super().__init__(code)

def _powershell_json(script:str):
    result=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",script],capture_output=True,text=True,check=False)
    if result.returncode != 0:return None
    try:return json.loads(result.stdout)
    except (json.JSONDecodeError,TypeError):return None

def _windows_current_sid()->str|None:
    value=_powershell_json("[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value | ConvertTo-Json -Compress")
    return value if isinstance(value,str) and re.fullmatch(r"S-1-[0-9-]+",value) else None

def _windows_acl(directory:Path,file:Path|None)->bool:
    sid=_windows_current_sid()
    if sid is None:return False
    target=file or directory
    quoted=str(target).replace("'","''")
    is_directory=file is None
    grant=f"*{sid}:(OI)(CI)F" if is_directory else f"*{sid}:F"
    system_grant="*S-1-5-18:(OI)(CI)F" if is_directory else "*S-1-5-18:F"
    try:
        acl_process=subprocess.run(
            ["icacls.exe",str(target),"/inheritance:r","/remove:g","*S-1-3-4","*S-1-5-32-544","/grant:r",grant,"/grant:r",system_grant],
            capture_output=True,text=True,check=False,timeout=10,
        )
    except (OSError,subprocess.TimeoutExpired):return False
    if acl_process.returncode!=0:return False
    apply=(
        f"$a=Get-Acl -LiteralPath '{quoted}' -ErrorAction Stop;"
        "$rules=@($a.Access|ForEach-Object{@{sid=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;type=$_.AccessControlType.ToString();rights=$_.FileSystemRights.ToString();inheritance=$_.InheritanceFlags.ToString();propagation=$_.PropagationFlags.ToString()}});"
        "@{owner=$a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;protected=$a.AreAccessRulesProtected;rules=$rules}|ConvertTo-Json -Depth 4 -Compress"
    )
    evidence=_powershell_json(apply)
    if not isinstance(evidence,dict) or evidence.get("owner")!=sid or evidence.get("protected") is not True:return False
    rules=evidence.get("rules",[])
    if isinstance(rules,dict):rules=[rules]
    allowed={sid,"S-1-5-18"}
    if not isinstance(rules,list) or {r.get("sid") for r in rules if isinstance(r,dict)} != allowed:return False
    if not all(isinstance(r,dict) and r.get("type")=="Allow" and "FullControl" in r.get("rights","") for r in rules):return False
    if is_directory:
        return all({part.strip() for part in r.get("inheritance","").split(",")}=={"ContainerInherit","ObjectInherit"} and r.get("propagation")=="None" for r in rules)
    return all(r.get("inheritance")=="None" and r.get("propagation")=="None" for r in rules)

def _posix_permissions(directory:Path,file:Path|None)->bool:
    target=file or directory
    expected=0o600 if file is not None else 0o700
    try:return (target.stat().st_mode&0o777)==expected
    except OSError:return False

@contextmanager
def external_restricted_input(request,acl_verifier=None):
    payload,error=read_prompt(request)
    if error: yield None,error; return
    directory=Path(tempfile.mkdtemp(prefix="triagent-private-")); file=directory/"input.txt"
    try:
        if os.name=="nt":
            verifier=acl_verifier or _windows_acl
        else:
            directory.chmod(0o700)
            verifier=acl_verifier or _posix_permissions
        if not verifier(directory,None):raise TransportSecurityError("transport-acl-setup-failed")
        file.touch(mode=0o600,exist_ok=False)
        if os.name!="nt":file.chmod(0o600)
        if not verifier(directory,file):raise TransportSecurityError("transport-acl-verification-failed")
        file.write_bytes(payload.encode("utf-8"))
        yield file,None
    finally:
        for attempt in range(3):
            try: shutil.rmtree(directory); break
            except OSError:
                if attempt==2: raise TransportSecurityError("transport-cleanup-failed")
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
    model=ReviewPayload if role is AgentRole.REVIEWER else ImplementerPayload if role is AgentRole.IMPLEMENTER else CanonicalPayload
    try: parsed=model.model_validate(payload)
    except ValidationError as error: raise ValueError from error
    data={"status":parsed.status,"summary_code":{AgentRole.IMPLEMENTER:"completed",AgentRole.VERIFIER:"verified",AgentRole.REVIEWER:"clean"}[role],"evidence":parsed.evidence,"artifacts":parsed.artifacts}
    if role is AgentRole.REVIEWER: data["findings"]=[x.model_dump() for x in parsed.findings]
    if role is AgentRole.IMPLEMENTER: data["changed_paths"]=parsed.changed_paths
    return data


def invoke_json(
    runner: ProcessRunner,
    argv: Sequence[str],
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
    secret_values: Sequence[str] = (), role: AgentRole = AgentRole.IMPLEMENTER, stdin: str | None = None, cursor_envelope: bool = False,
    allow_fenced_json: bool = False,
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
    if allow_fenced_json:
        payload, diagnostic = _decode_json_object(process.stdout)
        if diagnostic is not None:
            return AgentResult(
                status=AgentStatus.INVALID_OUTPUT,
                summary="CLI returned invalid structured output",
                data={"diagnostic_code": diagnostic},
            )
        assert payload is not None
    else:
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
            envelope = CursorEnvelope.model_validate(payload)
        except ValidationError:
            return AgentResult(
                status=AgentStatus.INVALID_OUTPUT,
                summary="Cursor returned invalid result envelope",
                data={"diagnostic_code": "cursor-envelope-invalid"},
            )
        return AgentResult(
            status=AgentStatus.SUCCEEDED,
            data={
                "status": "passed",
                "summary_code": "completed",
                "evidence": [],
                "artifacts": [],
            },
            actual_usd=envelope.total_cost_usd,
        )
    try: data=_canonical(role,payload)
    except ValueError: return AgentResult(status=AgentStatus.INVALID_OUTPUT,summary="CLI returned invalid canonical output",data={"diagnostic_code":"canonical-output-invalid"})
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
