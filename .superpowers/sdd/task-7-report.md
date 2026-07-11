# Task 7 report

Status: DONE

Commit: this Task 7 commit (`feat: detect and invoke coding CLIs`); exact hash is reported in the agent handoff.

Implemented native Codex and Antigravity adapters, a WSL Ubuntu 24.04 Cursor adapter with positional prompt transmission, and a disabled-by-default DeepSeek/OpenCode adapter. Capability probes keep authentication and DeepSeek model, agent-tool, and billing gates independent. Shared invocation handling maps unavailable executables/authentication, timeouts, nonzero exits, and malformed JSON to structured `AgentStatus` values.

Verification (Python 3.12.10):

- `C:/Users/yangs/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/test_cli_capabilities.py -q` — 13 passed, 1 skipped in 0.22s.
- `C:/Users/yangs/AppData/Local/Programs/Python/Python312/python.exe -m pytest -q` — 43 passed, 1 skipped in 8.86s.
- `git diff --check` — clean.

The skipped test is intentionally marked `live_cli` and requires explicit selection plus local vendor authentication. No vendor or network calls were made. No API keys or tokens are stored, logged, or returned.

Concerns: Vendor CLIs can evolve their flags and structured response formats; the adapters intentionally isolate these argv definitions and reject malformed output safely. Live CLI validation remains environment-dependent and was not selected.
