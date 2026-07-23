from __future__ import annotations

import os
import math
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Annotated, Literal

import typer

from triagent.adapters.base import AgentRequest, AgentResult, AgentStatus
from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.deepseek import DeepSeekAdapter
from triagent.adapters.fake import FakeAgent
from triagent.domain import Budget, RiskLevel, TaskSpec, TaskState
from triagent.git_workspace import GitWorkspace
from triagent.orchestrator import Orchestrator
from triagent.report import render_persisted_report
from triagent.router import ImplementationRouter
from triagent.store import TaskStore


app = typer.Typer(no_args_is_help=True, help="Run and inspect TriAgent tasks.")
DataRoot = Annotated[Path, typer.Option(help="TriAgent state directory.")]
RiskOption = Annotated[RiskLevel, typer.Option("--risk", help="Declared task risk level.")]
AcceptanceOptions = Annotated[list[str], typer.Option("--acceptance", help="Repeatable acceptance criterion.")]
ForbiddenOptions = Annotated[list[str], typer.Option("--forbidden", help="Repeatable forbidden path or constraint.")]
VisualCheckOption = Annotated[
    Literal["required", "optional", "none"],
    typer.Option("--visual-check", help="Required, optional, or no visual verification."),
]


def _root(value: Path | None) -> Path:
    return value or Path(os.environ.get("TRIAGENT_HOME", ".triagent"))


def _spec(
    repo: Path,
    goal: str,
    risk: RiskLevel,
    acceptance: list[str],
    forbidden: list[str] | None = None,
    visual_check: Literal["required", "optional", "none"] = "none",
    budget: Budget | None = None,
) -> TaskSpec:
    if not acceptance or not all(item.strip() for item in acceptance):
        raise ValueError("at least one non-empty acceptance criterion is required")
    if forbidden and not all(item.strip() for item in forbidden):
        raise ValueError("forbidden constraints must be non-empty")
    return TaskSpec(
        goal=goal,
        scope=[str(repo.resolve())],
        acceptance=acceptance,
        risk=risk,
        forbidden=forbidden or [],
        visual_check=visual_check,
        budget=budget or Budget(),
    )

def _priced(config: dict, name: str, field: str = "estimated_usd") -> float | None:
    value=config.get("agents",{}).get(name,{}).get(field)
    return float(value) if not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value) and value >= 0 else None


def _profile_command(config: dict, name: str) -> list[str]:
    value = config.get("agents", {}).get(name, {}).get("command")
    if not isinstance(value, list) or not value or not all(isinstance(part, str) and part for part in value):
        raise typer.BadParameter(f"Profile agent {name!r} requires a non-empty command array")
    return value


def _fallback_profile(config:dict)->tuple[str,bool]:
    agents=config.get("agents",{})
    return "deepseek",agents.get("deepseek",{}).get("enabled") is True


def _deepseek_options(config: dict) -> dict[str, str]:
    section = config.get("agents", {}).get("deepseek", {})
    model = section.get("model", "deepseek-v4-flash")
    base_url = section.get("base_url", "https://api.deepseek.com")
    if not isinstance(model, str) or not model:
        raise ValueError("invalid DeepSeek model")
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("invalid DeepSeek base URL")
    return {"model": model, "base_url": base_url}


def _profile_digest(config: dict) -> str:
    normalized = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _require_provenance(store: TaskStore, task_id: str, expected: dict[str, str]) -> dict[str, str]:
    persisted = store.execution_provenance(task_id)
    if persisted is None or persisted != expected:
        raise ValueError("execution provenance unavailable or incompatible")
    return persisted


def _recovery_checkpoint(store: TaskStore, task_id: str) -> tuple[str, int]:
    if store.load(task_id).state is not TaskState.FAILED_RECOVERABLE:
        raise ValueError("task is not recoverable")
    checkpoint = store.recovery_checkpoint(task_id)
    if (
        checkpoint is None
        or checkpoint.get("stage") not in {"implement", "verify", "review"}
        or not isinstance(checkpoint.get("sequence"), int)
        or checkpoint["sequence"] < 1
    ):
        raise ValueError("recovery checkpoint unavailable")
    return str(checkpoint["stage"]), checkpoint["sequence"]


def _status(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


@app.command()
def create(
    repo: Path,
    goal: str,
    risk: RiskOption,
    acceptance: AcceptanceOptions,
    forbidden: ForbiddenOptions = None,
    visual_check: VisualCheckOption = "none",
    data_root: DataRoot = None,
) -> None:
    try:
        task = TaskStore(_root(data_root)).create_task(
            _spec(repo, goal, risk, acceptance, forbidden, visual_check)
        )
    except Exception:
        raise typer.BadParameter("task creation failed") from None
    typer.echo(f"Task: {task.id}\nState: {task.state.value}")


@app.command()
def run(repo: Path, goal: str, risk: RiskOption, acceptance: AcceptanceOptions,
        forbidden: ForbiddenOptions = None, visual_check: VisualCheckOption = "none",
        profile: Annotated[str, typer.Option()] = "fake", data_root: DataRoot = None,
        live_confirmed: Annotated[bool, typer.Option("--live-confirmed")] = False,
        billing_confirmed: Annotated[bool, typer.Option("--billing-confirmed")] = False) -> None:
    if profile != "fake" and not (live_confirmed and billing_confirmed):
        raise typer.BadParameter("real profiles require --live-confirmed and --billing-confirmed")
    config=None; budget=Budget(); fallback_name="deepseek"; fallback_enabled=False; fallback_estimate=None; probe_cost=None
    setup_diagnostic: str | None = None
    try:
        if profile != "fake":
            config=tomllib.loads(Path(profile).read_text(encoding="utf-8"))
            values=config.get("budget",{})
            budget=Budget(max_agent_calls=int(values.get("max_agent_calls",20)),max_minutes=int(values.get("max_minutes",60)),max_usd=float(values.get("max_usd",0)))
            for agent in ("cursor","codex","antigravity"):_profile_command(config,agent)
            fallback_name,fallback_enabled=_fallback_profile(config)
            if fallback_enabled:
                fallback_estimate=_priced(config,fallback_name);probe_cost=_priced(config,fallback_name,"probe_estimated_usd")
                if fallback_estimate is None or fallback_estimate<=0 or probe_cost is None or probe_cost<=0:raise ValueError("invalid fallback cost estimates")
        GitWorkspace.validate(repo)
    except (OSError,tomllib.TOMLDecodeError,ValueError,TypeError,RuntimeError):
        raise typer.BadParameter("task input validation failed") from None
    try:
        store = TaskStore(_root(data_root)); task = store.create_task(
            _spec(repo, goal, risk, acceptance, forbidden, visual_check, budget)
        )
    except Exception:
        raise typer.BadParameter("task creation failed") from None
    run_worktree = store.runs_root / task.id / "worktree"
    try:
        if profile != "fake": store.record_attestation(task.id,"live-confirmed",True); store.record_attestation(task.id,"billing-confirmed",True)
        run_worktree.rmdir()
        workspace=GitWorkspace.create(repo, task.id, destination=run_worktree)
        store.set_workspace(task.id,str(workspace.repo),workspace.base_commit,f"triagent/{task.id}")
        if profile == "fake":
            success = AgentResult(status=AgentStatus.SUCCEEDED, data={"status":"passed","summary_code":"completed","evidence":[]})
            (run_worktree/"health-endpoint.txt").write_text("ok\n",encoding="utf-8")
            orchestrator = Orchestrator(store, FakeAgent([success]), FakeAgent([success]), FakeAgent([success]), profile_digest="fake-v1")
        else:
            assert config is not None
            cursor = CursorAdapter(command=_profile_command(config, "cursor"), estimated_usd=_priced(config,"cursor"))
            codex = CodexAdapter(command=_profile_command(config, "codex"), estimated_usd=_priced(config,"codex"))
            antigravity = AntigravityAdapter(command=_profile_command(config, "antigravity"), estimated_usd=_priced(config,"antigravity"))
            cursor_caps=cursor.capabilities()  # preferred free/version/auth probe first; no model smoke
            capabilities={"cursor":cursor_caps,"deepseek":False}
            if not cursor_caps.available and fallback_enabled:
                deepseek=DeepSeekAdapter(enabled=True,billing_confirmed=billing_confirmed,live_confirmed=live_confirmed,estimated_usd=fallback_estimate,**_deepseek_options(config))
                deepseek_caps=store.execute_paid_operation(task.id,probe_cost,deepseek.capabilities)
                capabilities["deepseek"]=deepseek_caps
                if not deepseek_caps.available:
                    setup_diagnostic = deepseek_caps.diagnostic_code or "deepseek-api-failed"
            choice = ImplementationRouter().choose(cursor_usage=0.0, capabilities=capabilities)
            orchestrator = Orchestrator(
                store, cursor if choice.name == "cursor" else deepseek, codex, antigravity,
                profile_digest=_profile_digest(config),
            )
        state = orchestrator.run_until_blocked(task.id)
    except Exception as error:
        preserved=[]
        if run_worktree.exists(): preserved.append(f"preserved worktree: {run_worktree}")
        preserved.append(f"preservation branch may exist: triagent/{task.id}")
        if store.load(task.id).state is TaskState.SPEC:
            store.fail_setup(task.id, setup_diagnostic or f"Setup failed: {type(error).__name__}", preserved)
        raise typer.BadParameter("task setup failed; inspect persisted task status") from error
    (store.runs_root / task.id / "final-report.md").write_text(render_persisted_report(store, task.id), encoding="utf-8")
    typer.echo(f"Task: {task.id}\nState: {state.value}\nReport: {store.runs_root / task.id / 'final-report.md'}")


@app.command()
def resume(
    task_id: str,
    profile: Annotated[str, typer.Option()],
    data_root: DataRoot = None,
    live_confirmed: Annotated[bool, typer.Option("--live-confirmed")] = False,
    billing_confirmed: Annotated[bool, typer.Option("--billing-confirmed")] = False,
) -> None:
    if profile != "fake" and not (live_confirmed and billing_confirmed):
        raise typer.BadParameter("real profiles require --live-confirmed and --billing-confirmed")
    store = TaskStore(_root(data_root))
    try:
        store.load(task_id)
        recovery_checkpoint = _recovery_checkpoint(store, task_id)
        if profile == "fake":
            expected = {
                "mode": "simulation", "implementer": "fake", "verifier": "fake",
                "reviewer": "fake", "profile_digest": "fake-v1",
            }
            _require_provenance(store, task_id, expected)
            success = AgentResult(
                status=AgentStatus.SUCCEEDED,
                data={"status": "passed", "summary_code": "completed", "evidence": []},
            )
            orchestrator = Orchestrator(
                store, FakeAgent([success]), FakeAgent([success]), FakeAgent([success]),
                profile_digest="fake-v1",
                expected_recovery_checkpoint=recovery_checkpoint,
            )
        else:
            config = tomllib.loads(Path(profile).read_text(encoding="utf-8"))
            digest = _profile_digest(config)
            persisted = store.execution_provenance(task_id)
            if persisted is None or persisted.get("mode") != "live":
                raise ValueError("live execution provenance unavailable")
            expected = {
                "mode": "live", "implementer": persisted["implementer"],
                "verifier": "codex", "reviewer": "antigravity",
                "profile_digest": digest,
            }
            _require_provenance(store, task_id, expected)
            for agent in ("codex", "antigravity"):
                _profile_command(config, agent)
            if persisted["implementer"] == "cursor":
                probe_cost = None
                implementer = CursorAdapter(
                    command=_profile_command(config, "cursor"),
                    estimated_usd=_priced(config, "cursor"),
                )
            elif persisted["implementer"] == "deepseek":
                fallback_name, fallback_enabled = _fallback_profile(config)
                if not fallback_enabled:
                    raise ValueError("persisted DeepSeek implementer is unavailable")
                fallback_estimate = _priced(config, fallback_name)
                if fallback_estimate is None or fallback_estimate <= 0:
                    raise ValueError("invalid fallback cost estimate")
                implementer = DeepSeekAdapter(
                    enabled=True,
                    billing_confirmed=billing_confirmed, live_confirmed=live_confirmed,
                    estimated_usd=fallback_estimate, **_deepseek_options(config),
                )
                probe_cost = None
                if recovery_checkpoint[0] == "implement":
                    probe_cost = _priced(config, fallback_name, "probe_estimated_usd")
                    if probe_cost is None or probe_cost <= 0:
                        raise ValueError("invalid fallback probe estimate")
            else:
                raise ValueError("persisted implementer is unavailable")
            orchestrator = Orchestrator(
                store,
                implementer,
                CodexAdapter(
                    command=_profile_command(config, "codex"),
                    estimated_usd=_priced(config, "codex"),
                ),
                AntigravityAdapter(
                    command=_profile_command(config, "antigravity"),
                    estimated_usd=_priced(config, "antigravity"),
                ),
                profile_digest=digest,
                expected_recovery_checkpoint=recovery_checkpoint,
                implementer_probe_estimated_usd=probe_cost,
            )
            store.record_attestation(task_id, "live-confirmed", True)
            store.record_attestation(task_id, "billing-confirmed", True)
        state = orchestrator.resume_until_blocked(task_id)
    except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError, RuntimeError):
        raise typer.BadParameter("task resume refused; inspect persisted task status") from None
    report_path = store.runs_root / task_id / "final-report.md"
    report_path.write_text(render_persisted_report(store, task_id), encoding="utf-8")
    typer.echo(f"Task: {task_id}\nState: {state.value}\nReport: {report_path}")


@app.command()
def status(task_id: str, data_root: DataRoot = None) -> None:
    task = TaskStore(_root(data_root)).load(task_id)
    typer.echo(f"Task: {task.id}\nState: {task.state.value}")


@app.command()
def approve(task_id: str, action: str, data_root: DataRoot = None) -> None:
    store = TaskStore(_root(data_root))
    orchestrator = Orchestrator(store, FakeAgent([]), FakeAgent([]), FakeAgent([]))
    try:
        state = orchestrator.approve(task_id, action)
    except ValueError as error:
        typer.echo("Approval refused: no matching outstanding request", err=True)
        raise typer.Exit(2) from error
    typer.echo(f"Task: {task_id}\nState: {state.value}")


@app.command(name="report")
def report_command(task_id: str, data_root: DataRoot = None) -> None:
    store = TaskStore(_root(data_root))
    task = store.load(task_id)
    typer.echo(render_persisted_report(store, task_id))


@app.command()
def doctor(profile: Annotated[str, typer.Option()] = "fake") -> None:
    if profile == "fake":
        typer.echo("Fake: ready (no vendor calls)")
        return

    profile_path = Path(profile)
    try:
        config = tomllib.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise typer.BadParameter("Cannot read selected profile") from error

    deepseek_enabled = bool(config.get("agents", {}).get("deepseek", {}).get("enabled", False))
    probes = (
        ("codex", CodexAdapter(command=_profile_command(config, "codex"))),
        ("cursor", CursorAdapter(command=_profile_command(config, "cursor"))),
        ("antigravity", AntigravityAdapter(command=_profile_command(config, "antigravity"))),
        ("deepseek", DeepSeekAdapter(enabled=deepseek_enabled, billing_confirmed=False, **_deepseek_options(config))),
    )
    typer.echo(f"Profile: {profile_path}")
    for name, adapter in probes:
        capability = adapter.capabilities()
        diagnostic = getattr(capability, "diagnostic_code", None)
        typer.echo(
            f"{name}: installed={_status(capability.installed)} "
            f"authenticated={_status(capability.authenticated)} "
            f"ready={_status(capability.ready)}"
            + (f" diagnostic={diagnostic}" if diagnostic else "")
        )


if __name__ == "__main__":
    app()
