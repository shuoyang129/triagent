---
name: triagent
description: Use when operating approval-gated TriAgent tasks across Cursor, Codex, and Antigravity, including provider diagnosis, fake or paid task execution, reports, approvals, DGX Cursor sandbox configuration, and strictly isolated synthetic-force runs.
---

# Operate TriAgent

Use only the `triagent` operator CLI. Never construct or invoke vendor-agent commands directly. Do not install software, change networks, expose credentials, or claim onsite verification from simulated results.

## Workflow

1. Diagnose the selected profile before running work. On DGX use `/home/ys/works/robots/triagent/profiles/dgx.spark.toml` for normal work.
2. Create a task for inspection with an explicit `--risk`, one or more `--acceptance` criteria, and a declared `--visual-check`. Add repeatable `--forbidden` path exclusions when needed. Prefer the quota-free `fake` profile. A configured real profile requires both `--live-confirmed` and `--billing-confirmed`; never infer either confirmation.
3. Inspect status and the rendered report.
4. Resume only with an explicit `--profile` that matches the task's immutable execution provenance. Never downgrade a live task to `fake`, substitute a different profile, or replace its selected implementer.
5. In outcome approval mode, present the user outcome, tests, independent review, visual artifacts, residual risk, rollback, and pending approval.
6. Run approval only after the operator explicitly chooses the named approval action.

The normal DGX non-fake route uses Cursor Composer 2.5 Fast with Auto-review and sandboxing enabled, then Codex verification and independent Antigravity review. DeepSeek/OpenCode remains disabled in the checked-in DGX profile. Treat provider failures as recoverable only when TriAgent says so; inspect the saved diagnostic and resume using the same profile digest and execution provenance. DGX/LAN/Isaac remains Phase B.

Before normal Cursor work on DGX, verify the narrowly scoped AppArmor support from `/home/ys/works/robots/triagent`: run `bash scripts/install-cursor-sandbox-apparmor.sh --check`, refresh authorization with `sudo -v`, then run `bash scripts/install-cursor-sandbox-apparmor.sh --apply`.

This grants `userns` only to `~/.local/share/cursor-agent/versions/*/cursorsandbox` and retains the global restriction. Do not broaden the profile.

Use `/home/ys/works/robots/triagent/profiles/dgx.spark.synthetic-force.toml` only for deliberately disposable synthetic tests. It passes `--force`, so require both enforced boundaries: source scopes below `/home/ys/works/robots/synthetic-projects` and task worktrees below `/home/ys/works/robots/triagent-synthetic-runs/runs`. Never use it for robot, production, or general repositories, and never make it the default.

Provider calls default to 900 seconds. Set `TRIAGENT_AGENT_TIMEOUT_SECONDS` only within 60 through 3600 seconds when a task needs a different bound. Cursor's filesystem capability probe is fixed at 30 seconds.

```shell
triagent doctor --profile profiles/windows.example.toml
triagent create --risk low --acceptance "tests pass" --visual-check none PATH "GOAL"
triagent run --profile fake --risk low --acceptance "tests pass" --visual-check none PATH "GOAL"
triagent run --profile profiles/windows.example.toml --live-confirmed --billing-confirmed --risk low --acceptance "tests pass" --forbidden secrets/ --visual-check none PATH "GOAL"
triagent doctor --profile /home/ys/works/robots/triagent/profiles/dgx.spark.toml
triagent run --profile /home/ys/works/robots/triagent/profiles/dgx.spark.toml --live-confirmed --billing-confirmed --risk low --acceptance "tests pass" --visual-check none PATH "GOAL"
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
- Do not use the synthetic `--force` profile outside its two adapter-enforced path roots.
- Do not infer authentication, device, GPU, display, Isaac, WebRTC, service, or remote-control readiness.
- Do not approve an outcome, visual result, merge, paid use, installation, or destructive action without explicit operator approval.
- Recording `merge`, `deploy`, or `destructive` approval never performs that action; execution is a separate operator-controlled operation.
- Do not include secrets or private reasoning in commands or reports.
