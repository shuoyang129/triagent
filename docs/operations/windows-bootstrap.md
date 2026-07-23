# Windows bootstrap

## Installed tool layout

- Python 3.12: `%LOCALAPPDATA%/Programs/Python/Python312/python.exe`
- Codex CLI: `codex`
- Cursor CLI: Ubuntu 24.04 WSL, `~/.local/bin/cursor-agent`
- Antigravity CLI: `%LOCALAPPDATA%/agy/bin/agy.exe`
- DeepSeek Python SDK fallback: optional and disabled by default; OpenCode is not used

## Authentication

Run authentication interactively. Never paste credentials into a task log.

```powershell
codex
wsl -d Ubuntu-24.04 -- ~/.local/bin/cursor-agent login
agy
```

Exit the interactive tools after login. Cursor authentication can be checked with:

```powershell
wsl -d Ubuntu-24.04 -- ~/.local/bin/cursor-agent status
```

## Generate the capability record

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap-windows.ps1 -Output work/capabilities/windows.json
```

The output contains booleans and version strings only. It must never contain access tokens, cookies, API keys, or private-key material.

## Create, run, and resume tasks

`create` and `run` require an explicit risk declaration and at least one repeatable acceptance criterion. `--forbidden` is repeatable for excluded paths or constraints. Declare the visual-verification mode with `--visual-check`; robot-safety risk always forces it to `required`.

```powershell
triagent create --risk low --acceptance "unit tests pass" --visual-check none C:\src\project "Add a health endpoint"
triagent run --profile fake --risk low --acceptance "unit tests pass" --forbidden secrets\ --visual-check none C:\src\project "Add a health endpoint"
```

Resume only an existing `FAILED_RECOVERABLE` task and always pass `--profile` explicitly. The task ID is preserved, the persisted failed stage is retried, earlier passed stages are not repeated, and remaining repair/call/time/USD limits still apply. Every run persists immutable simulation/live provenance, the selected implementer, verifier, reviewer, and normalized profile digest before its first provider stage. Resume refuses missing or mismatched provenance, including live-to-fake downgrade and profile substitution. A DeepSeek-origin task resumes with DeepSeek rather than silently switching to Cursor.

```powershell
triagent resume --profile fake TASK_ID
triagent resume --profile profiles\windows.example.toml --live-confirmed --billing-confirmed TASK_ID
```

Non-fake `run` and `resume` commands require both live and billing confirmation. Missing flags, profile commands, failed-stage evidence, compatible provenance, or remaining budget fail closed before a provider call. Recoverable adapter failures—including unavailable executables, nonzero exits, timeouts, invalid output, and raised transport exceptions—persist their exact stage using sanitized controller diagnostics.

## Known first-phase boundary

Passing this bootstrap proves only that the travel Windows host can run the local tools. It does not prove DGX, Isaac, LAN SSH, ChatGPT Remote, or Tencent Cloud relay integration.
