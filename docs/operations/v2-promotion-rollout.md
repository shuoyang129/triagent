# TriAgent v2 promotion and rollout runbook

This runbook records the v2 promotion process.  On 2026-08-04 the operator accepted the verified nine-stage chain and the public `triagent` name was switched directly to v2.  The original controller and `/home/ys/works/robots/triagent-runs` remain frozen archival material and are not a public route.

## Evidence and gate rules

Retain immutable test/replay output outside an implementer worktree, hash each
artifact, and record it in the schema at
`docs/evidence/promotion/v2-promotion-evidence.schema.json`.  Evaluate a record offline with `triagent.promotion.evaluate`. A record binds every
gate to a retained descriptor path and its SHA-256; the descriptor binds a
sanitized raw-log path and SHA-256. Evaluate a complete chain with
`triagent.promotion.evaluate_chain(records, artifact_root=...)`; omitting
`artifact_root` is intentionally never cutover eligible. This parses local data only.
It never constructs a provider adapter, launches a provider, or changes an
entry point.

Every record must show passing evidence for all of these gates:

- full tests;
- no secret leak;
- no residual provider process;
- no duplicate provider call after durable-result recovery;
- no candidate misattribution;
- legacy fallback; and
- original tasks remain readable/resumable through the frozen original runtime.

The promotion order is fixed.  Each record binds the content digests of every
earlier stage, so a later stage cannot be evaluated without an evidence chain:

1. unit tests;
2. adapter contracts;
3. fake three-stage workflow;
4. historical replay (all twelve listed replay classes);
5. isolated no-provider synthetic task;
6. isolated real-provider synthetic task;
7. low-risk non-robot project task;
8. humanoid offline/read-only task; and
9. ordinary new humanoid task.

Do not skip a failed level.  A passing record is only a structured review aid;
the referenced artifacts must still be independently reviewed.

For stage 8, distinguish a controller/transport failure, a completed
`INSPECTION_HOLD`, and a cleared read-only admission. A hold proves the read-only
v2 control correctly denied admission after independent BLOCKER or MAJOR findings,
while preserving the source snapshot and creating no candidate or approval. It is
valid fail-closed controller evidence, but never authorizes stage 9 for that
target. Only a separately reviewed `read_only_admission.outcome=cleared` record
may satisfy stage 9 admission.

## Gray rollout boundaries

| Stage | Frozen original `triagent` | `triagent-v2` |
|---|---|---|
| A | All humanoid tasks | Synthetic Fake only |
| B | Existing and robot-safety tasks | New low-risk tasks |
| C | Existing/recoverable tasks | Ordinary new humanoid tasks |
| D | Old tasks only | All new tasks |

Use an explicit per-provider legacy fallback if a v2 feature fails.  Rollback
never migrates a database: original tasks stay with the frozen original runtime
and v2 tasks stay with v2 for audit and recovery.

## Cutover hold point

The evaluator itself never repoints the public command.  The accepted final record contains the separately recorded `operator_acceptance` object:

```json
{"action":"cutover","outcome":"accepted","operator":"named-operator","accepted_at":"2026-08-01T00:00:00Z"}
```

The named operator accepted the reviewed outcome after all gates passed.  The public name is an atomic `~/.local/bin/triagent` link to v2; removing that added link restores the prior no-public-command state.  The frozen original runtime and its data root were neither repointed nor modified.
