---
name: triagent
description: Use when an operator needs to diagnose, create, run, inspect, approve, or report an approval-gated TriAgent task from a Windows work host or DGX target.
---

# Operate TriAgent

Use only the `triagent` operator CLI. Never construct or invoke vendor-agent commands directly. Do not install software, change networks, expose credentials, or claim onsite verification from simulated results.

## Workflow

1. Diagnose the selected profile before running work.
2. Create a task for inspection with an explicit `--risk`, one or more `--acceptance` criteria, and a declared `--visual-check`. Add repeatable `--forbidden` path exclusions when needed. Prefer the quota-free `fake` profile. A configured real profile requires both `--live-confirmed` and `--billing-confirmed`; never infer either confirmation.
3. Inspect status and the rendered report.
4. Resume only with an explicit `--profile` that matches the task's immutable execution provenance. Never downgrade a live task to `fake`, substitute a different profile, or replace its selected implementer.
5. In outcome approval mode, present the user outcome, tests, independent review, visual artifacts, residual risk, rollback, and pending approval.
6. Run approval only after the operator explicitly chooses the named approval action.

Non-fake real profiles cover only the local Phase A coding flow: Cursor implements (with DeepSeek/OpenCode as router fallback), Codex verifies, and Antigravity independently reviews. DGX/LAN/Isaac remains Phase B.

```shell
triagent doctor --profile profiles/windows.example.toml
triagent create --risk low --acceptance "tests pass" --visual-check none PATH "GOAL"
triagent run --profile fake --risk low --acceptance "tests pass" --visual-check none PATH "GOAL"
triagent run --profile profiles/windows.example.toml --live-confirmed --billing-confirmed --risk low --acceptance "tests pass" --forbidden secrets/ --visual-check none PATH "GOAL"
triagent resume --profile fake TASK_ID
triagent resume --profile profiles/windows.example.toml --live-confirmed --billing-confirmed TASK_ID
triagent status TASK_ID
triagent report TASK_ID
triagent approve TASK_ID visual
triagent approve TASK_ID outcome
triagent approve TASK_ID merge
```

## Forbidden operations

- Do not bypass TriAgent to call Codex, Cursor, Antigravity, Gemini, DeepSeek, or OpenCode.
- Do not infer authentication, device, GPU, display, Isaac, WebRTC, service, or remote-control readiness.
- Do not approve an outcome, visual result, merge, paid use, installation, or destructive action without explicit operator approval.
- Recording `merge`, `deploy`, or `destructive` approval never performs that action; execution is a separate operator-controlled operation.
- Do not include secrets or private reasoning in commands or reports.
