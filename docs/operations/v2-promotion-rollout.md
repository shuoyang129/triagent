# TriAgent v2 promotion and rollout runbook

This runbook is for the isolated `triagent-v2` controller.  Before accepted
cutover, the public `triagent`, its environment, profiles, wrappers, and
`/home/ys/works/robots/triagent-runs` remain untouched.

## Evidence and gate rules

Retain immutable test/replay output outside an implementer worktree, hash each
artifact, and record it in the schema at
`docs/evidence/promotion/v2-promotion-evidence.schema.json`.  Evaluate the
record offline with `triagent.promotion.evaluate`; this parses local data only.
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

For stage 8, distinguish a controller/transport failure from a completed
`INSPECTION_HOLD`. The latter proves the read-only v2 control correctly denied
admission after independent BLOCKER or MAJOR findings, while preserving the
source snapshot and creating no candidate or approval. It is valid fail-closed
evidence for the controller, but it does not authorize stage 9 for that target.

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

No code path in the evaluator or this runbook may repoint public `triagent`.
Even a final-stage, rollout-D evidence record is not cutover eligible until it
contains a separately recorded `operator_acceptance` object with exactly:

```json
{"action":"cutover","outcome":"accepted","operator":"named-operator","accepted_at":"2026-08-01T00:00:00Z"}
```

The named operator must explicitly accept the reviewed outcome after all gates
pass.  Only then may a separately reviewed, atomic cutover procedure be
considered.  That later procedure must route old task roots to the frozen
original runtime, route v2 roots to v2, preserve the v2-owned AGY wrapper, and
have a tested atomic restoration of the original public entry point.
