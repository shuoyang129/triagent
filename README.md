# TriAgent v2

TriAgent v2 is the active public controller.  The public `triagent` command
resolves to the v2 runtime and uses the v2 task root
`/home/ys/works/robots/triagent-runs-v2`.

The earlier controller and its task root are frozen archival material.  They
are not a public entry point, are not migrated, and must not be modified.

## Default provider route

The normal DGX profile is [`profiles/dgx.spark.toml`](profiles/dgx.spark.toml).
It uses DeepSeek through the restricted OpenCode implementer, then Codex
verification and independent Antigravity review.  Cursor remains a configurable
provider, but is disabled in the default v2 profile.

OpenCode is run with the v2 boundary: no shell, network, subagents, skills,
external directories, `.env`, or `.git` access.  Provider output is parsed as
structured data and sensitive content is not retained in task reports.

## Normal operation

```sh
triagent --version
triagent doctor --profile /home/ys/works/robots/triagent-v2/profiles/dgx.spark.toml

# No provider call; controller and workflow validation only.
triagent run REPOSITORY "Goal" \
  --profile fake --risk low --acceptance "tests pass" --visual-check none

# A paid run requires both confirmations and an explicit v2 profile.
triagent run REPOSITORY "Goal" \
  --profile /home/ys/works/robots/triagent-v2/profiles/dgx.spark.toml \
  --live-confirmed --billing-confirmed \
  --risk low --acceptance "tests pass" --forbidden secrets/ --visual-check none

triagent status TASK_ID --data-root /home/ys/works/robots/triagent-runs-v2
triagent report TASK_ID --data-root /home/ys/works/robots/triagent-runs-v2
```

Do not approve an outcome, merge, deployment, paid action, or destructive
operation without an explicit operator decision.  Task edits occur only in
isolated worktrees; no task directly edits its source checkout.

## Streaming and recovery

v2 records two different signals:

- liveness: the wrapper process is still alive;
- meaningful progress: provider output, a new event, a test-stage change, or a
  candidate-state change.

Only meaningful progress refreshes the idle timeout.  A terminal result written
by Codex's official final-message channel is recovered even if the CLI exits
after the bounded finalization interval, preventing a duplicate paid call.

## Promotion and rollback

The accepted nine-stage promotion chain is retained under
[`docs/evidence/promotion`](docs/evidence/promotion).  Public cutover is a
symlink at `/home/ys/.local/bin/triagent` to the installed v2 command; removal
of that symlink is the rollback for the public name.  It does not modify the
frozen original runtime or either task database.

For a robot-safety repository, first use the explicit `triagent inspect`
read-only route.  It requires both live/billing confirmations and must never
create a candidate, source edit, service, simulation, network-control, or
physical action.
