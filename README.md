# TriAgent

TriAgent is the active public controller.  Its current runtime is the accepted
v2 implementation, now maintained directly in this repository.  The public
`triagent` command uses the isolated task root
`/home/ys/works/robots/triagent-runs-v2`; no operational command depends on a
separate `triagent-v2` source checkout.

## Default provider route

The normal DGX profile is [`profiles/dgx.spark.toml`](profiles/dgx.spark.toml).
It uses DeepSeek through the restricted OpenCode implementer, then Codex
verification and independent Antigravity review.  Cursor remains a configurable
provider, but is disabled in the default profile.

OpenCode is run with the runtime boundary: no shell, network, subagents, skills,
external directories, `.env`, or `.git` access.  Provider output is parsed as
structured data and sensitive content is not retained in task reports.

## Normal operation

```sh
triagent --version
triagent doctor --profile /home/ys/works/robots/triagent/profiles/dgx.spark.toml

# No provider call; controller and workflow validation only.
triagent run REPOSITORY "Goal" \
  --profile fake --risk low --acceptance "tests pass" --visual-check none

# A paid run requires both confirmations and an explicit real profile.
triagent run REPOSITORY "Goal" \
  --profile /home/ys/works/robots/triagent/profiles/dgx.spark.toml \
  --live-confirmed --billing-confirmed \
  --risk low --acceptance "tests pass" --forbidden secrets/ --visual-check none

triagent status TASK_ID --data-root /home/ys/works/robots/triagent-runs-v2
triagent report TASK_ID --data-root /home/ys/works/robots/triagent-runs-v2
```

Do not approve an outcome, merge, deployment, paid action, or destructive
operation without an explicit operator decision.  Task edits occur only in
isolated worktrees; no task directly edits its source checkout.

## Streaming and recovery

The runtime records two different signals:

- liveness: the wrapper process is still alive;
- meaningful progress: provider output, a new event, a test-stage change, or a
  candidate-state change.

Only meaningful progress refreshes the idle timeout.  A terminal result written
by Codex's official final-message channel is recovered even if the CLI exits
after the bounded finalization interval, preventing a duplicate paid call.

## Promotion and rollback

The accepted nine-stage promotion chain is retained under
[`docs/evidence/promotion`](docs/evidence/promotion).  Public cutover is a
symlink at `/home/ys/.local/bin/triagent` to the installed runtime.  Git
history retains the pre-cutover controller; rollback is an explicit Git
decision and must not alter either task database.

For a robot-safety repository, first use the explicit `triagent inspect`
read-only route.  It requires both live/billing confirmations and must never
create a candidate, source edit, service, simulation, network-control, or
physical action.
