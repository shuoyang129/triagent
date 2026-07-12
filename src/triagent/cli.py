from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from triagent.adapters.base import AgentResult, AgentStatus
from triagent.adapters.fake import FakeAgent
from triagent.domain import TaskSpec
from triagent.orchestrator import Orchestrator
from triagent.report import render_report, write_report
from triagent.store import TaskStore


app = typer.Typer(no_args_is_help=True, help="Run and inspect TriAgent tasks.")
DataRoot = Annotated[Path, typer.Option(help="TriAgent state directory.")]


def _root(value: Path | None) -> Path:
    return value or Path(os.environ.get("TRIAGENT_HOME", ".triagent"))


def _spec(repo: Path, goal: str) -> TaskSpec:
    return TaskSpec(goal=goal, scope=[str(repo.resolve())], acceptance=["requested outcome implemented", "tests pass"])


def _values(state: str, *, complete: bool = False) -> dict[str, str]:
    return {
        "state": state,
        "user outcome": "Workflow completed and is ready for approval." if complete else "Task recorded.",
        "tests": "Fake verification passed." if complete else "Not run.",
        "independent review": "Fake independent review passed." if complete else "Pending.",
        "visual artifacts": "None.",
        "residual risk": "Human approval is still required." if complete else "Implementation has not run.",
        "rollback": "Discard the task worktree; the source checkout is unchanged.",
        "pending approval": "Approve the result." if complete else "Run the task.",
    }


@app.command()
def create(repo: Path, goal: str, data_root: DataRoot = None) -> None:
    task = TaskStore(_root(data_root)).create_task(_spec(repo, goal))
    typer.echo(f"Task: {task.id}\nState: {task.state.value}")


@app.command()
def run(repo: Path, goal: str, profile: Annotated[str, typer.Option()] = "fake", data_root: DataRoot = None) -> None:
    if profile != "fake":
        raise typer.BadParameter("Only the fake profile is enabled by this bootstrap command")
    store = TaskStore(_root(data_root))
    task = store.create_task(_spec(repo, goal))
    success = AgentResult(status=AgentStatus.SUCCEEDED, summary="fake success")
    orchestrator = Orchestrator(store, FakeAgent([success]), FakeAgent([success]), FakeAgent([success]))
    state = orchestrator.run_until_blocked(task.id)
    write_report(store.runs_root / task.id / "final-report.md", _values(state.value, complete=True))
    typer.echo(f"Task: {task.id}\nState: {state.value}\nReport: {store.runs_root / task.id / 'final-report.md'}")


@app.command()
def status(task_id: str, data_root: DataRoot = None) -> None:
    task = TaskStore(_root(data_root)).load(task_id)
    typer.echo(f"Task: {task.id}\nState: {task.state.value}")


@app.command()
def approve(task_id: str, action: str = "visual", data_root: DataRoot = None) -> None:
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
    path = store.runs_root / task_id / "final-report.md"
    typer.echo(path.read_text(encoding="utf-8") if path.exists() else render_report(_values(task.state.value)))


@app.command()
def doctor(profile: Annotated[str, typer.Option()] = "fake") -> None:
    if profile == "fake":
        typer.echo("Fake: ready (no vendor calls)")
    else:
        typer.echo(f"Profile: {profile}\nUse adapter capability checks before live execution.")


if __name__ == "__main__":
    app()
