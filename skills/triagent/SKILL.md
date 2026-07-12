---
name: triagent
description: Use when an operator needs to diagnose, create, run, inspect, approve, or report an approval-gated TriAgent task from a Windows work host or DGX target.
---

# Operate TriAgent

Use only the `triagent` operator CLI. Never construct or invoke vendor-agent commands directly. Do not install software, change networks, expose credentials, or claim onsite verification from simulated results.

## Workflow

1. Diagnose the selected profile before running work.
2. Create a task for inspection. Run only with the currently executable `fake` profile.
3. Inspect status and the rendered report.
4. In outcome approval mode, present the user outcome, tests, independent review, visual artifacts, residual risk, rollback, and pending approval.
5. Run approval only after the operator explicitly chooses the named approval action.

Non-fake profiles are diagnostic-only until Phase B. Use `triagent doctor` plus the onsite checklist; do not claim a Windows or DGX profile can run tasks yet.

```shell
triagent doctor --profile profiles/windows.example.toml
triagent create PATH "GOAL"
triagent run --profile fake PATH "GOAL"
triagent status TASK_ID
triagent report TASK_ID
triagent approve TASK_ID visual
```

## Forbidden operations

- Do not bypass TriAgent to call Codex, Cursor, Antigravity, Gemini, DeepSeek, or OpenCode.
- Do not infer authentication, device, GPU, display, Isaac, WebRTC, service, or remote-control readiness.
- Do not approve an outcome, visual result, merge, paid use, installation, or destructive action without explicit operator approval.
- Do not include secrets or private reasoning in commands or reports.
