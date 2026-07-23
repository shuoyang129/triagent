# TriAgent

TriAgent is an approval-gated local orchestrator for a three-provider coding
workflow:

1. Cursor implements the requested change.
2. Codex independently verifies the candidate and test evidence.
3. Google Antigravity performs an independent review.

The controller records immutable task provenance, provider-call budgets,
sanitized failure diagnostics, candidate commits, and explicit operator
approvals. A provider failure does not silently switch an existing task to a
different profile or implementation route.

## Requirements

- Python 3.12 or newer
- Git
- Authenticated provider CLIs for real runs
- Linux or Windows; the repository also contains a verified DGX Spark profile

## Install and test

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
.venv/bin/triagent --help
```

DGX Spark operators can use the idempotent installers and detailed checklist
in [`docs/operations/dgx-bootstrap.md`](docs/operations/dgx-bootstrap.md):

```bash
bash scripts/bootstrap-dgx.sh
bash scripts/install-triagent-dgx.sh --check
bash scripts/install-triagent-dgx.sh --apply
bash scripts/install-cursor-sandbox-apparmor.sh --check
sudo -v
bash scripts/install-cursor-sandbox-apparmor.sh --apply
```

The AppArmor installer grants `userns` only to Cursor's `cursorsandbox`
binary. It retains the global unprivileged-userns restriction.

## Run a task

Use the fake profile first when validating a new workflow:

```bash
triagent doctor --profile fake
triagent run \
  --profile fake \
  --risk low \
  --acceptance "tests pass" \
  --visual-check none \
  /absolute/path/to/repository \
  "Implement the requested change"
```

A real profile always requires explicit live and billing confirmation:

```bash
triagent doctor --profile profiles/dgx.spark.toml
triagent run \
  --profile profiles/dgx.spark.toml \
  --live-confirmed \
  --billing-confirmed \
  --risk low \
  --acceptance "python -m pytest -q passes" \
  --forbidden .env \
  --visual-check none \
  /absolute/path/to/repository \
  "Implement the requested change"
```

Inspect and continue a task using the same profile that created it:

```bash
triagent status TASK_ID
triagent report TASK_ID
triagent resume --profile profiles/dgx.spark.toml \
  --live-confirmed --billing-confirmed TASK_ID
triagent approve TASK_ID outcome
triagent approve TASK_ID merge
```

Approval records an operator decision; it does not itself merge, deploy, or
perform a destructive operation.

## DGX provider policy

The normal DGX profile is [`profiles/dgx.spark.toml`](profiles/dgx.spark.toml).
It uses Cursor Composer 2.5 Fast with Auto-review and sandboxing enabled, then
the repository-owned Codex verifier and Antigravity reviewer adapters.
Provider calls default to a 900-second timeout; operators may set
`TRIAGENT_AGENT_TIMEOUT_SECONDS` from 60 through 3600 seconds. Cursor's
filesystem capability probe remains independently bounded at 30 seconds.

The optional DeepSeek fallback is a native OpenAI-compatible Python adapter, not a Cursor custom model or OpenCode process. It reads `DEEPSEEK_API_KEY` only from the environment, connects only to the official `https://api.deepseek.com` endpoint, supplies a bounded snapshot of tracked UTF-8 files, and accepts only validated relative `write`/`delete` operations. It has no shell tool. The checked-in profiles keep it disabled; enabling it requires explicit live and billing confirmation plus positive `estimated_usd` and `probe_estimated_usd` values. Readiness failures are reduced to allowlisted diagnostic codes such as authentication, balance, model-list, rate-limit, timeout, connection, request, service, or invalid-smoke categories; provider response text is never persisted.

[`profiles/dgx.spark.synthetic-force.toml`](profiles/dgx.spark.synthetic-force.toml)
is a deliberately narrow exception. It passes Cursor `--force`, which
overrides repository safety policy, and its adapter rejects work unless both
conditions hold:

- source scopes resolve below `/home/ys/works/robots/synthetic-projects`;
- task worktrees resolve below
  `/home/ys/works/robots/triagent-synthetic-runs/runs`.

Never make the force profile the default and never use it for a robot or
production repository.

## Verification status

On 2026-07-22 the DGX route completed a real paid Cursor, Codex, and Antigravity synthetic test. On 2026-07-23 the native DeepSeek implementation, Codex verification, and Antigravity review route also passed and reached the approval gate. The complete offline suite at the latter revision reported 287 passed and 9 skipped.

Real calls consume provider quota or incur charges. TriAgent's estimated-cost
ledger is conservative accounting, not a provider invoice.

## Codex skill

The operator skill is maintained in [`skills/triagent`](skills/triagent).
Install that directory as `~/.codex/skills/triagent` and restart/reload Codex
after updates. The skill requires all vendor calls to go through the TriAgent
CLI and preserves the same paid-call, profile-provenance, approval, AppArmor,
and synthetic-force boundaries described above.
