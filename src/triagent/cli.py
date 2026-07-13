from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated

import typer

from triagent.adapters.base import AgentRequest, AgentResult, AgentStatus
from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.deepseek import DeepSeekAdapter
from triagent.adapters.fake import FakeAgent
from triagent.domain import Budget, TaskSpec, TaskState
from triagent.git_workspace import GitWorkspace
from triagent.orchestrator import Orchestrator
from triagent.report import render_persisted_report
from triagent.router import ImplementationRouter
from triagent.store import TaskStore


app = typer.Typer(no_args_is_help=True, help="Run and inspect TriAgent tasks.")
DataRoot = Annotated[Path, typer.Option(help="TriAgent state directory.")]


class _FakeImplementer(FakeAgent):
    def run(self, request: AgentRequest) -> AgentResult:
        (request.workdir / "health-endpoint.txt").write_text("ok\n", encoding="utf-8")
        return super().run(request)


def _root(value: Path | None) -> Path:
    return value or Path(os.environ.get("TRIAGENT_HOME", ".triagent"))


def _spec(repo: Path, goal: str, budget: Budget | None = None) -> TaskSpec:
    return TaskSpec(goal=goal, scope=[str(repo.resolve())], acceptance=["requested outcome implemented", "tests pass"], budget=budget or Budget())

def _priced(config: dict, name: str, field: str = "estimated_usd") -> float | None:
    value=config.get("agents",{}).get(name,{}).get(field)
    return float(value) if isinstance(value,(int,float)) and value >= 0 else None


def _profile_command(config: dict, name: str) -> list[str]:
    value = config.get("agents", {}).get(name, {}).get("command")
    if not isinstance(value, list) or not value or not all(isinstance(part, str) and part for part in value):
        raise typer.BadParameter(f"Profile agent {name!r} requires a non-empty command array")
    return value


def _status(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


@app.command()
def create(repo: Path, goal: str, data_root: DataRoot = None) -> None:
    task = TaskStore(_root(data_root)).create_task(_spec(repo, goal))
    typer.echo(f"Task: {task.id}\nState: {task.state.value}")


@app.command()
def run(repo: Path, goal: str, profile: Annotated[str, typer.Option()] = "fake", data_root: DataRoot = None,
        live_confirmed: Annotated[bool, typer.Option("--live-confirmed")] = False,
        billing_confirmed: Annotated[bool, typer.Option("--billing-confirmed")] = False) -> None:
    if profile != "fake" and not (live_confirmed and billing_confirmed):
        raise typer.BadParameter("real profiles require --live-confirmed and --billing-confirmed")
    config=None; budget=Budget()
    if profile != "fake":
        config=tomllib.loads(Path(profile).read_text(encoding="utf-8"))
        values=config.get("budget",{})
        budget=Budget(max_agent_calls=int(values.get("max_agent_calls",20)),max_minutes=int(values.get("max_minutes",60)),max_usd=float(values.get("max_usd",0)))
    GitWorkspace.validate(repo)
    store = TaskStore(_root(data_root)); task = store.create_task(_spec(repo, goal, budget))
    run_worktree = store.runs_root / task.id / "worktree"
    try:
        if profile != "fake": store.record_approval(task.id,"live"); store.record_approval(task.id,"billing")
        run_worktree.rmdir()
        workspace=GitWorkspace.create(repo, task.id, destination=run_worktree)
        store.set_workspace(task.id,str(workspace.repo),workspace.base_commit,f"triagent/{task.id}")
        if profile == "fake":
            success = AgentResult(status=AgentStatus.SUCCEEDED, data={"status":"passed","summary_code":"completed","evidence":[]})
            orchestrator = Orchestrator(store, _FakeImplementer([success]), FakeAgent([success]), FakeAgent([success]))
        else:
            assert config is not None
            cursor = CursorAdapter(command=_profile_command(config, "cursor"), deepseek_billing_confirmed=False, estimated_usd=_priced(config,"cursor"))
            codex = CodexAdapter(command=_profile_command(config, "codex"), estimated_usd=_priced(config,"codex"))
            antigravity = AntigravityAdapter(command=_profile_command(config, "antigravity"), estimated_usd=_priced(config,"antigravity"))
            cursor_caps=cursor.capabilities()  # preferred free/version/auth probe first; no model smoke
            capabilities={"cursor":cursor_caps,"deepseek":False}
            if not cursor_caps.available:
                probe_cost=_priced(config,"opencode","probe_estimated_usd")
                call=store.reserve_agent_call(task.id,probe_cost)
                deepseek=DeepSeekAdapter(command=_profile_command(config,"opencode"),enabled=True,billing_confirmed=True,live_confirmed=True,estimated_usd=_priced(config,"opencode"))
                try:
                    deepseek_caps=deepseek.capabilities()
                except BaseException as error:
                    store.interrupt_agent_call(task.id,call,type(error).__name__); raise
                else: store.complete_agent_call(task.id,call,probe_cost or 0.0)
                capabilities["deepseek"]=deepseek_caps
            choice = ImplementationRouter().choose(cursor_usage=0.0, capabilities=capabilities)
            orchestrator = Orchestrator(store, cursor if choice.name == "cursor" else deepseek, codex, antigravity)
        state = orchestrator.run_until_blocked(task.id)
    except Exception as error:
        preserved=[]
        if run_worktree.exists(): preserved.append(f"preserved worktree: {run_worktree}")
        preserved.append(f"preservation branch may exist: triagent/{task.id}")
        if store.load(task.id).state is TaskState.SPEC: store.fail_setup(task.id, f"Setup failed: {type(error).__name__}", preserved)
        raise typer.BadParameter(str(error)) from error
    (store.runs_root / task.id / "final-report.md").write_text(render_persisted_report(store, task.id), encoding="utf-8")
    typer.echo(f"Task: {task.id}\nState: {state.value}\nReport: {store.runs_root / task.id / 'final-report.md'}")


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
        typer.echo(f"Approval refused: {error}", err=True)
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
        raise typer.BadParameter(f"Cannot read profile: {profile_path}") from error

    opencode_enabled = bool(config.get("agents", {}).get("opencode", {}).get("enabled", False))
    probes = (
        ("codex", CodexAdapter(command=_profile_command(config, "codex"))),
        ("cursor", CursorAdapter(command=_profile_command(config, "cursor"), deepseek_billing_confirmed=False)),
        ("antigravity", AntigravityAdapter(command=_profile_command(config, "antigravity"))),
        ("opencode/deepseek", DeepSeekAdapter(command=_profile_command(config, "opencode"), enabled=opencode_enabled, billing_confirmed=False, probe_installed=True)),
    )
    typer.echo(f"Profile: {profile_path}")
    for name, adapter in probes:
        capability = adapter.capabilities()
        typer.echo(
            f"{name}: installed={_status(capability.installed)} "
            f"authenticated={_status(capability.authenticated)} "
            f"ready={_status(capability.ready)}"
        )


if __name__ == "__main__":
    app()
