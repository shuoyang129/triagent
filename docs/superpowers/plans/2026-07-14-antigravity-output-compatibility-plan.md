# Antigravity Output Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept a single complete JSON Markdown fence from Antigravity while preserving strict canonical validation and privacy-safe failure diagnostics.

**Architecture:** Add one transport decoder used only by the plain JSON response path in `triagent.adapters._cli`. It accepts raw JSON or one complete fence, returns only a JSON object, and emits categorical failure codes; the existing role-specific Pydantic models remain the canonical boundary. Allowlist the two new codes in the orchestrator so the persisted task audit distinguishes malformed JSON from a non-object response without storing vendor text.

**Tech Stack:** Python 3.12, Pydantic 2, pytest 9, Git, PowerShell locally, Ubuntu 24.04/aarch64 on DGX.

## Global Constraints

- Accept only raw JSON or one complete fence with an opening line of exactly three backticks, optionally followed immediately by `json` case-insensitively.
- Reject prose around a fence, multiple fences, arbitrary brace extraction, and non-object top-level JSON.
- Keep `CanonicalPayload`, `ImplementerPayload`, and `ReviewPayload` unchanged.
- Never persist raw stdout, response excerpts, prompts, task contents, or validation values.
- Do not change Codex JSONL parsing, Cursor envelopes, routing, budgeting, approvals, or DeepSeek state.
- Do not make any live provider call during implementation or deployment verification.
- Preserve DGX task `c16c155e-bfe8-44a0-a82b-28fb6f4a777d`, its branch, and its worktree.

---

### Task 1: Strict fenced-JSON transport decoder

**Files:**
- Modify: `tests/test_cli_capabilities.py`
- Modify: `src/triagent/adapters/_cli.py`

**Interfaces:**
- Consumes: `ProcessResult.stdout: str` from the existing `invoke_json` path.
- Produces: `_decode_json_object(value: str) -> tuple[dict | None, str | None]`, where the second item is `None`, `json-malformed`, or `json-non-object`.

- [ ] **Step 1: Add failing transport-shape tests**

Add the import and tests below to `tests/test_cli_capabilities.py`:

```python
from triagent.adapters._cli import invoke_json


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
    result = invoke_json(FakeRunner(completed(stdout)), ["agy"], tmp_path, 1, role=AgentRole.REVIEWER)
    assert result.status is AgentStatus.INVALID_OUTPUT
    assert result.data == {"diagnostic_code": "json-malformed"}


def test_plain_json_transport_rejects_non_object_json(tmp_path: Path) -> None:
    result = invoke_json(FakeRunner(completed("[]")), ["agy"], tmp_path, 1, role=AgentRole.REVIEWER)
    assert result.status is AgentStatus.INVALID_OUTPUT
    assert result.data == {"diagnostic_code": "json-non-object"}


def test_plain_json_transport_keeps_canonical_schema_strict(tmp_path: Path) -> None:
    result = invoke_json(FakeRunner(completed("{}")), ["agy"], tmp_path, 1, role=AgentRole.REVIEWER)
    assert result.status is AgentStatus.INVALID_OUTPUT
    assert result.data == {"diagnostic_code": "canonical-output-invalid"}
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest -q `
  tests/test_cli_capabilities.py::test_plain_json_transport_accepts_raw_or_one_complete_fence `
  tests/test_cli_capabilities.py::test_plain_json_transport_rejects_noncanonical_wrapping `
  tests/test_cli_capabilities.py::test_plain_json_transport_rejects_non_object_json `
  tests/test_cli_capabilities.py::test_plain_json_transport_keeps_canonical_schema_strict
```

Expected: fenced cases fail because `invoke_json` passes stdout directly to `json.loads`; malformed and non-object cases lack the new diagnostic codes.

- [ ] **Step 3: Implement the minimal decoder**

Add this helper near the payload models in `src/triagent/adapters/_cli.py`:

```python
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
```

Replace the initial parsing block in `invoke_json` with:

```python
    payload, diagnostic = _decode_json_object(process.stdout)
    if diagnostic is not None:
        return AgentResult(
            status=AgentStatus.INVALID_OUTPUT,
            summary="CLI returned invalid structured output",
            data={"diagnostic_code": diagnostic},
        )
    assert payload is not None
```

Leave sanitization, Cursor envelope handling, `_canonical`, and actual-cost handling unchanged.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command again.

Expected: all parameterized cases pass; raw response text is absent from every `AgentResult`.

- [ ] **Step 5: Run adjacent adapter tests**

Run:

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest -q `
  tests/test_cli_capabilities.py `
  tests/test_sixth_wave_contract.py `
  tests/test_seventh_wave_contract.py `
  tests/test_eighth_wave_contract.py `
  tests/test_tenth_wave_contract.py
```

Expected: zero failures; live tests remain skipped unless explicitly selected.

- [ ] **Step 6: Commit the decoder**

```powershell
git add src/triagent/adapters/_cli.py tests/test_cli_capabilities.py
git commit -m "fix: accept strict fenced Antigravity JSON"
```

---

### Task 2: Persist categorical transport diagnostics

**Files:**
- Modify: `tests/test_orchestrator.py`
- Modify: `src/triagent/orchestrator.py`

**Interfaces:**
- Consumes: `AgentResult.data["diagnostic_code"]` values `json-malformed` and `json-non-object`.
- Produces: allowlisted `agent_calls.diagnostic` and recoverable transition events containing only the categorical code.

- [ ] **Step 1: Parameterize the existing persistence test**

Replace `test_failed_agent_call_persists_only_allowlisted_diagnostic_code` in `tests/test_orchestrator.py` with:

```python
@pytest.mark.parametrize(
    "diagnostic_code",
    ["cursor-result-non-json", "json-malformed", "json-non-object"],
)
def test_failed_agent_call_persists_only_allowlisted_diagnostic_code(
    tmp_path: Path, diagnostic_code: str
) -> None:
    failed = AgentResult(
        status=AgentStatus.INVALID_OUTPUT,
        summary="vendor text must not be persisted",
        data={"diagnostic_code": diagnostic_code},
    )
    orchestrator, store = make_orchestrator(
        tmp_path,
        FakeAgent.succeeding("clean"),
        implementer=FakeAgent([failed]),
    )
    task = store.create_task(make_spec())

    orchestrator.run_until_blocked(task.id)

    with store._connect() as connection:
        row = connection.execute(
            "SELECT diagnostic FROM agent_calls WHERE task_id = ?",
            (task.id,),
        ).fetchone()
    assert row["diagnostic"] == diagnostic_code
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest -q `
  tests/test_orchestrator.py::test_failed_agent_call_persists_only_allowlisted_diagnostic_code
```

Expected: the two new parameter cases persist generic `invalid_output` because they are not allowlisted.

- [ ] **Step 3: Extend the safe diagnostic allowlist**

Add these values to `_SAFE_DIAGNOSTICS` in `src/triagent/orchestrator.py`:

```python
    "json-malformed",
    "json-non-object",
```

Do not add raw error messages or dynamic validation text.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest -q `
  tests/test_orchestrator.py::test_failed_agent_call_persists_only_allowlisted_diagnostic_code `
  tests/test_cli_capabilities.py::test_plain_json_transport_rejects_noncanonical_wrapping `
  tests/test_cli_capabilities.py::test_plain_json_transport_rejects_non_object_json
```

Expected: all cases pass and no vendor stdout is persisted.

- [ ] **Step 5: Commit diagnostic persistence**

```powershell
git add src/triagent/orchestrator.py tests/test_orchestrator.py
git commit -m "fix: classify Antigravity transport failures"
```

---

### Task 3: Full verification and DGX deployment

**Files:**
- Verify: entire repository.
- Update remotely: `/home/ys/works/robots/triagent` through an incremental Git bundle.
- Preserve: `/home/ys/works/robots/triagent-runs/runs/c16c155e-bfe8-44a0-a82b-28fb6f4a777d`.

**Interfaces:**
- Consumes: the two local fix commits.
- Produces: matching clean local/remote commits and passing local/DGX non-live tests.

- [ ] **Step 1: Run the full local suite**

```powershell
& 'C:\Users\yangs\AppData\Local\Programs\Python\Python312\python.exe' -m pytest -q
```

Expected: zero failures; live and platform-specific tests may skip.

- [ ] **Step 2: Check scope and whitespace**

```powershell
git status --short
git diff --check e03820d8ec2e47de0f1015803690bab58e442dd0..HEAD
git log -5 --oneline
```

Expected: clean worktree, no whitespace errors, and only the design, plan, and two focused fix commits after `e03820d`.

- [ ] **Step 3: Create and verify an incremental bundle**

```powershell
git bundle create triagent-antigravity-fix.bundle dgx-spark-deploy ^e03820d8ec2e47de0f1015803690bab58e442dd0
git bundle verify triagent-antigravity-fix.bundle
```

Expected: the bundle advertises `refs/heads/dgx-spark-deploy` and requires `e03820d`.

- [ ] **Step 4: Upload and fast-forward under guards**

```powershell
scp triagent-antigravity-fix.bundle spark:/home/ys/works/robots/triagent-antigravity-fix.bundle
ssh spark 'set -eu; cd /home/ys/works/robots/triagent; test "$(git rev-parse HEAD)" = "e03820d8ec2e47de0f1015803690bab58e442dd0"; test -z "$(git status --porcelain)"; git fetch /home/ys/works/robots/triagent-antigravity-fix.bundle refs/heads/dgx-spark-deploy; git merge --ff-only FETCH_HEAD; test -z "$(git status --porcelain)"; rm -f /home/ys/works/robots/triagent-antigravity-fix.bundle'
```

Expected: guarded fast-forward succeeds. Stop if the old commit or clean-worktree guard fails.

- [ ] **Step 5: Remove the local bundle**

```powershell
Remove-Item -LiteralPath 'triagent-antigravity-fix.bundle' -Force
git status --short
```

Expected: bundle absent and local worktree clean.

- [ ] **Step 6: Run remote focused and full tests**

```powershell
ssh spark 'cd /home/ys/works/robots/triagent && /home/ys/miniforge3/bin/conda run -n triagent python -m pytest -q tests/test_cli_capabilities.py tests/test_orchestrator.py'
ssh spark 'cd /home/ys/works/robots/triagent && /home/ys/miniforge3/bin/conda run -n triagent python -m pytest -q'
```

Expected: zero failures; Windows/live/onsite tests may skip.

- [ ] **Step 7: Verify preservation and stop before live validation**

```powershell
ssh spark 'set -eu; task=c16c155e-bfe8-44a0-a82b-28fb6f4a777d; test -d /home/ys/works/robots/triagent-runs/runs/$task/worktree; /home/ys/miniforge3/bin/conda run -n triagent triagent status $task --data-root /home/ys/works/robots/triagent-runs; git -C /home/ys/works/robots/projects/triagent-dgx-smoke status --short; git -C /home/ys/works/robots/triagent status --short'
```

Expected: failed task remains `FAILED_RECOVERABLE`, candidate worktree remains present, source and controller repositories remain clean. Report results and request explicit approval for exactly one direct Antigravity reviewer-adapter validation call.
