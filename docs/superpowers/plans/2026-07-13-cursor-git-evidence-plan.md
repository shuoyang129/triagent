# Cursor Git Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a successful Cursor transport advance on deterministic Git worktree evidence even when Cursor's final `result` is free text.

**Architecture:** `CursorAdapter` validates only the outer Cursor envelope and returns a controller-owned minimal implementation result. `TaskStore` optionally requires a non-empty Git-derived change set, and `Orchestrator` enables that requirement only for the concrete Cursor implementer before creating the reviewed commit and handoff.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite, Git plumbing, pytest, Typer.

## Global Constraints

- Cursor free-text `result` must never be parsed as implementation evidence or copied into `AgentResult`, SQLite, events, handoff, or reports.
- Cursor transport success requires process exit code `0` and a valid outer envelope with `type=result`, `subtype=success`, and `is_error=false`.
- Actual changed paths come only from the isolated Git worktree relative to the recorded base commit.
- Existing candidate controls for secrets, protected control files, special files, path/size limits, stable rereads, reviewed refs, and approvals remain unchanged.
- A Cursor implementation with no actual changes fails recoverably as `cursor-no-worktree-change` before Codex is called.
- DeepSeek behavior, routing, billing gates, Codex verification, and Antigravity review schemas remain unchanged.
- Do not add a Cursor formatting-repair model call.

---

### Task 10: Convert Cursor output handling to transport-only success

**Files:**
- Modify: `src/triagent/adapters/_cli.py:295-312`
- Test: `tests/test_cli_capabilities.py:169-210`

**Interfaces:**
- Consumes: `CursorEnvelope.model_validate(payload)` and `invoke_json(..., cursor_envelope=True)`.
- Produces: controller-owned `AgentResult(status=SUCCEEDED, data={status, summary_code, evidence, artifacts}, actual_usd=...)` without vendor `result` content.

- [ ] **Step 1: Replace the current non-JSON nested-result expectation with a failing transport-success test**

Update the existing Cursor test to this exact behavior:

```python
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
```

Add an outer-envelope regression test:

```python
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
```

- [ ] **Step 2: Run the two tests and verify the free-text case fails for the expected reason**

Run:

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest tests\test_cli_capabilities.py::test_cursor_accepts_free_text_result_as_transport_success_without_persisting_vendor_text tests\test_cli_capabilities.py::test_cursor_still_rejects_invalid_outer_envelope -q
```

Expected: the free-text test fails because the current result is `INVALID_OUTPUT`; the invalid outer-envelope test passes or fails only if its diagnostic is not preserved.

- [ ] **Step 3: Return immediately after a valid Cursor envelope with controller-owned data**

Replace the Cursor-envelope branch in `invoke_json` with:

```python
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
```

Leave the canonical `_canonical(role, payload)` path unchanged for Codex, Antigravity, and DeepSeek.

- [ ] **Step 4: Run the Cursor adapter test file**

Run:

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest tests\test_cli_capabilities.py -q
```

Expected: all non-live tests pass; only the existing explicitly selected live tests are skipped.

- [ ] **Step 5: Commit Task 10**

```powershell
git add -- src/triagent/adapters/_cli.py tests/test_cli_capabilities.py
git commit -m "fix: use transport success for Cursor results"
```

---

### Task 11: Add an opt-in non-empty candidate requirement

**Files:**
- Modify: `src/triagent/store.py:282-429`
- Test: `tests/test_tenth_wave_contract.py:40-58`

**Interfaces:**
- Consumes: `TaskStore._candidate_manifest(..., changed_paths, require_changes=False)`.
- Produces: `TaskStore.materialize_reviewed_commit(task_id, changed_paths=None, require_changes=False) -> str` and the fixed exception text `candidate manifest rejected: no changes` when the opt-in requirement is active.

- [ ] **Step 1: Write the failing opt-in no-change test**

Add:

```python
def test_required_change_rejects_noop_without_breaking_general_noop_candidates(tmp_path):
    store, task, _work = setup(tmp_path)

    with pytest.raises(ValueError, match="candidate manifest rejected: no changes"):
        store.materialize_reviewed_commit(task.id, require_changes=True)

    candidate = store.materialize_reviewed_commit(task.id, [])
    assert candidate
```

- [ ] **Step 2: Run the test and verify the new keyword is unsupported**

Run:

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest tests\test_tenth_wave_contract.py::test_required_change_rejects_noop_without_breaking_general_noop_candidates -q
```

Expected: FAIL with `unexpected keyword argument 'require_changes'`.

- [ ] **Step 3: Add the opt-in flag without changing default behavior**

Change the signatures and guard to:

```python
    def _candidate_manifest(
        self,
        task_id: str,
        work: Path,
        base: str,
        changed_paths: list[str] | None = None,
        require_changes: bool = False,
    ) -> dict[str, tuple[str, str, bytes]]:
```

Immediately after `actual_changes=tracked_changes|untracked`, add:

```python
            if require_changes and not actual_changes:
                raise ValueError("candidate manifest rejected: no changes")
```

Change the public method and both stable-manifest calls to:

```python
    def materialize_reviewed_commit(
        self,
        task_id: str,
        changed_paths: list[str] | None = None,
        require_changes: bool = False,
    ) -> str:
```

```python
            manifest = self._candidate_manifest(
                task_id, work, meta["base_commit"], changed_paths, require_changes
            )
```

```python
            stable = self._candidate_manifest(
                task_id, work, meta["base_commit"], changed_paths, require_changes
            )
```

The default remains `False`, preserving existing no-op candidate tests and non-Cursor behavior.

- [ ] **Step 4: Run candidate safety tests**

Run:

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest tests\test_tenth_wave_contract.py tests\test_ninth_wave_contract.py tests\test_eleventh_wave_contract.py -q
```

Expected: all tests pass, with only platform-specific symlink tests skipped on Windows.

- [ ] **Step 5: Commit Task 11**

```powershell
git add -- src/triagent/store.py tests/test_tenth_wave_contract.py
git commit -m "fix: support required Git candidate changes"
```

---

### Task 12: Route concrete Cursor implementations through Git-derived evidence

**Files:**
- Modify: `src/triagent/orchestrator.py:138-151`
- Create: `tests/test_cursor_git_evidence.py`

**Interfaces:**
- Consumes: `TaskStore.materialize_reviewed_commit(..., require_changes=True)` from Task 2 and the controller-owned Cursor result from Task 1.
- Produces: reviewed Cursor candidate and handoff on actual changes; failed `implement` outcome with diagnostic `cursor-no-worktree-change` when the worktree is unchanged.

- [ ] **Step 1: Create an integration test fixture using the real Cursor adapter with a fake process runner**

Create `tests/test_cursor_git_evidence.py` with these helpers:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from triagent.adapters.base import AgentResult, AgentStatus
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.fake import FakeAgent
from triagent.adapters.process import ProcessResult
from triagent.domain import Budget, TaskSpec, TaskState
from triagent.git_workspace import GitWorkspace
from triagent.orchestrator import Orchestrator
from triagent.report import render_persisted_report
from triagent.store import TaskStore


VENDOR_MARKER = "VENDOR_FREE_TEXT_MUST_NOT_PERSIST"


class CursorRunner:
    def __init__(self, edit: bool) -> None:
        self.edit = edit

    def run(self, argv, cwd: Path, timeout, env_allowlist, stdin=None) -> ProcessResult:
        if self.edit:
            (cwd / "actual.txt").write_text("actual\n", encoding="utf-8")
        vendor_result = json.dumps({
            "changed_paths": ["claimed.txt"],
            "note": VENDOR_MARKER,
        })
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": vendor_result,
        }
        return ProcessResult(0, json.dumps(envelope), "", False)


def setup_task(tmp_path: Path, *, edit: bool):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(
        goal="add actual.txt",
        scope=[str(repo)],
        acceptance=["verified"],
        budget=Budget(max_agent_calls=5, max_minutes=5, max_usd=5),
    ))
    work = store.runs_root / task.id / "worktree"
    work.rmdir()
    workspace = GitWorkspace.create(repo, task.id, destination=work)
    store.set_workspace(task.id, str(repo), workspace.base_commit, f"triagent/{task.id}")

    verifier = FakeAgent([AgentResult(
        status=AgentStatus.SUCCEEDED,
        data={"status": "passed", "summary_code": "verified", "evidence": ["tests pass"], "artifacts": []},
    )])
    reviewer = FakeAgent([AgentResult(
        status=AgentStatus.SUCCEEDED,
        data={"status": "passed", "summary_code": "clean", "evidence": [], "artifacts": [], "findings": []},
    )])
    cursor = CursorAdapter(
        runner=CursorRunner(edit),
        command=["cursor-agent"],
        estimated_usd=0.5,
    )
    orchestrator = Orchestrator(store, cursor, verifier, reviewer)
    return orchestrator, store, task, work, verifier, reviewer
```

- [ ] **Step 2: Add success and no-change tests**

Append:

```python
def test_cursor_free_text_advances_on_git_derived_change(tmp_path: Path) -> None:
    orchestrator, store, task, work, verifier, reviewer = setup_task(tmp_path, edit=True)

    state = orchestrator.run_until_blocked(task.id)

    assert state is TaskState.APPROVAL
    meta = store.workspace(task.id)
    assert subprocess.run(
        ["git", "show", f"{meta['reviewed_commit']}:actual.txt"],
        cwd=work,
        check=True,
        capture_output=True,
    ).stdout == b"actual\n"
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{meta['reviewed_commit']}:claimed.txt"],
        cwd=work,
        capture_output=True,
    ).returncode != 0
    run_dir = store.runs_root / task.id
    with store._connect() as connection:
        sqlite_text = repr(connection.execute(
            "SELECT status, diagnostic FROM agent_calls WHERE task_id = ?",
            (task.id,),
        ).fetchall()) + repr(connection.execute(
            "SELECT outcome_json FROM stage_outcomes WHERE task_id = ?",
            (task.id,),
        ).fetchall())
    persisted_text = "\n".join([
        sqlite_text,
        (run_dir / "events.jsonl").read_text(encoding="utf-8"),
        (run_dir / "handoff.json").read_text(encoding="utf-8"),
        render_persisted_report(store, task.id),
    ])
    assert VENDOR_MARKER not in persisted_text
    assert len(verifier.requests) == 1
    assert len(reviewer.requests) == 1


def test_cursor_no_change_stops_before_verification(tmp_path: Path) -> None:
    orchestrator, store, task, _work, verifier, reviewer = setup_task(tmp_path, edit=False)

    state = orchestrator.run_until_blocked(task.id)

    assert state is TaskState.FAILED_RECOVERABLE
    outcome = store.outcomes(task.id)["implement"]
    assert outcome.status == "failed"
    assert outcome.diagnostic == "cursor-no-worktree-change"
    assert verifier.requests == []
    assert reviewer.requests == []
```

- [ ] **Step 3: Run the new integration tests and verify the missing no-change orchestration behavior fails**

Run:

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest tests\test_cursor_git_evidence.py -q
```

Expected: after Tasks 10 and 11, the changed-worktree test may already pass because the existing path fallback derives changes from Git; the no-change test fails because `cursor-no-worktree-change` is not recorded.

- [ ] **Step 4: Materialize Cursor candidates before recording implementation success**

Replace the single-line materialization sequence in `Orchestrator.advance` with:

```python
            cursor_implementation = type(self.implementer) is CursorAdapter
            try:
                self.store.materialize_reviewed_commit(
                    task_id,
                    None if cursor_implementation else result.data.get("changed_paths"),
                    require_changes=cursor_implementation,
                )
            except ValueError as error:
                if cursor_implementation and str(error) == "candidate manifest rejected: no changes":
                    diagnostic = "cursor-no-worktree-change"
                    self.store.record_outcome(
                        task_id,
                        StageOutcome(
                            stage="implement",
                            status="failed",
                            summary="requires-repair",
                            diagnostic=diagnostic,
                        ),
                    )
                    return self.store.transition(
                        task_id,
                        state,
                        TaskState.FAILED_RECOVERABLE,
                        diagnostic,
                    ).state
                raise
            self.store.record_outcome(task_id, self._outcome("implement", result))
            self._write_handoff(task_id)
```

This ordering prevents a passed implementation outcome from being recorded before the Git candidate is accepted.

- [ ] **Step 5: Run integration and orchestration tests**

Run:

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest tests\test_cursor_git_evidence.py tests\test_orchestrator.py tests\test_second_wave_contract.py tests\test_third_wave_contract.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 12**

```powershell
git add -- src/triagent/orchestrator.py tests/test_cursor_git_evidence.py
git commit -m "fix: derive Cursor implementation evidence from Git"
```

---

### Task 13: Verify the complete system and run one bounded live smoke

**Files:**
- Verify only: all repository files
- Runtime evidence only: the task directory selected from the CLI's emitted UUID under `D:\workspace\runs\live-smoke\runs\`

**Interfaces:**
- Consumes: Tasks 10-12 and `profiles/windows.example.toml`.
- Produces: automated-test evidence and one persisted live task showing the reached provider stages.

- [ ] **Step 1: Run the full automated suite**

Run:

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest -q
```

Expected: at least 204 tests pass, only explicitly platform/live-selected tests skip, and exit code is `0`.

- [ ] **Step 2: Check the feature worktree is clean**

Run:

```powershell
git status --short
git log -4 --oneline
```

Expected: no status entries and the three task commits plus the plan/design commits are visible.

- [ ] **Step 3: Run exactly one real smoke with DeepSeek disabled**

Run:

```powershell
$python = 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe'
$runOutput = @(& $python -m triagent.cli run --profile profiles\windows.example.toml --data-root D:\workspace\runs\live-smoke --live-confirmed --billing-confirmed --risk low --acceptance "health_status() returns ok" --acceptance "focused pytest passes" --forbidden secrets\ --visual-check none D:\workspace\work\triagent-live-smoke "Add health_status() returning the string 'ok' in app.py and add a focused pytest test for it. Modify only app.py and tests/test_app.py.")
$runOutput
$taskLine = $runOutput | Where-Object { $_ -like 'Task:*' } | Select-Object -Last 1
$taskId = (($taskLine -split ':', 2)[1]).Trim()
if ($taskId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') { throw 'TriAgent did not emit a valid task UUID' }
```

Expected: Cursor runs once, then Codex runs once, then Antigravity runs once. DeepSeek runs zero times. Do not automatically retry if any stage fails.

- [ ] **Step 4: Inspect persisted evidence and independently test the candidate**

Run the existing read-only database helper with the emitted task id:

```powershell
& $python '.superpowers\sdd\inspect_live_db.py' 'D:\workspace\runs\live-smoke\triagent.sqlite3' $taskId
$candidateWorktree = Join-Path 'D:\workspace\runs\live-smoke\runs' (Join-Path $taskId 'worktree')
Push-Location $candidateWorktree
try { & $python -m pytest -q } finally { Pop-Location }
```

Expected on full success:

- task state is `APPROVAL` or the configured post-review approval state;
- exactly three `agent_calls` rows exist;
- `stage_outcomes` contains `implement`, `verify`, and `review`;
- the candidate tests pass;
- `final-report.md` contains a user outcome, test evidence, independent review, rollback, and residual-risk status;
- no vendor free text or credentials appear in persisted artifacts.

If the smoke stops earlier, report the exact persisted stage and diagnostic without another paid retry.

---

### Task 14: Allow Codex verification tests to run in the Windows worktree

**Discovered by:** bounded live task `5061a415-c51d-4f9f-9105-b1f62ecb50ad`, where Cursor completed and Codex reported `sandbox helper launch failed with Access denied (os error 5)` under `read-only`.

**Files:**
- Modify: `src/triagent/adapters/codex.py`
- Test: `tests/test_cli_capabilities.py`

**Constraints:**
- Use the documented `workspace-write` sandbox, not `danger-full-access` or a bypass flag.
- Keep the verifier role, structured JSONL parsing, stdin prompt transport, reviewed candidate ref, and all other adapter behavior unchanged.
- Do not run another paid live smoke in this task.

- [ ] **Step 1: Change the existing Codex invocation assertion to require `workspace-write`**

Rename the invocation test to describe workspace-write verification and change its argv assertion to:

```python
assert argv[:4] == ["codex.exe", "exec", "--sandbox", "workspace-write"]
assert "--dangerously-bypass-approvals-and-sandbox" not in argv
```

- [ ] **Step 2: Run the focused test and verify RED**

Expected: failure shows actual `read-only` differs from required `workspace-write`.

- [ ] **Step 3: Change only the Codex sandbox argument**

In `CodexAdapter.run`, replace `read-only` with `workspace-write`.

- [ ] **Step 4: Run focused adapter and immutable-candidate tests**

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest tests\test_cli_capabilities.py tests\test_ninth_wave_contract.py -q
```

Expected: all selected tests pass; verifier mutations remain outside the reviewed candidate.

- [ ] **Step 5: Commit Task 14**

```powershell
git add -- src/triagent/adapters/codex.py tests/test_cli_capabilities.py
git commit -m "fix: allow Codex verification in Windows worktrees"
```
