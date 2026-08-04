# Frozen original compatibility snapshot — 2026-08-02

This is a read-only compatibility observation. The real original data root was
not passed to the original CLI because its `TaskStore` initialization executes
DDL and can write SQLite metadata. Instead, a byte-identical database and one
real recoverable run directory were copied to `/tmp`; the frozen non-editable
original runtime was used only against that snapshot.

## Boundary and result

- Frozen controller environment:
  `/home/ys/miniforge3/envs/triagent-original-frozen`.
- Real task selected through SQLite `mode=ro` access:
  `22b1ffb4-eb34-4e54-b541-dbabacbec431`.
- Persisted real state: `FAILED_RECOVERABLE`.
- Commands executed against `/tmp/triagent-original-readonly-snapshot`:
  `triagent status` and `triagent report`; both succeeded and reported the same
  task state.
- Real and snapshot database SHA-256 after the commands:
  `a6fc71da323806e3cc819b11435d59447330a5a6c7e58a321d6702f5d5e6880c`.

This establishes that the frozen compatibility runtime can deserialize and
report a real historical recoverable task without changing the real source
SQLite or run tree. It does **not** claim that original `status/report` are
safe to invoke directly on the live root, and it does not authorize resume,
approval, migration, or cutover.

## Remaining original-baseline hold

The frozen baseline still records two original-suite failures caused by the
frozen profile/model expectation drift. The original controller is deliberately
not modified to make those tests pass; that condition remains a promotion gate
hold until the acceptance policy explicitly defines the frozen baseline
exception or an independent immutable baseline with passing contracts exists.


## Accepted lifetime-budget exception — 2026-08-04

An isolated clone of original task `672cc083-b6ce-4933-9e00-a4a5a37e90cc`
was resumed with the same frozen original CLI, matching normalized profile
digest, and explicit live/billing authorization. Only the clone source and
worktree paths were relocated; its goal, acceptance criteria, budget,
checkpoint, candidate reference, and execution provenance were retained.

The frozen controller rejected the resume before a provider request because the
immutable 60-minute lifetime budget from `task_runtime.started_at` had elapsed.
The clone retained exactly three pre-existing completed calls and no new charge.
Read-only inventory of the original database found 219 `FAILED_RECOVERABLE`
tasks, with zero still in their lifetime budget window. The real database and
selected task artifacts remained byte-identical: database
`fbb6ed82693622b93cd58179d78f4e1adbc7a53d8219cdf66ea272551de546d4`, task
`a4498c987fb1b661d21531f6ccd78657f6250cbeefd72313f64bacf088b38e75`, handoff
`ab16d09d55a76eca0f3826bafe3d03f8b8ec0c612139f9349e7c592c1d2f2284`, and
events `0f433df28be4e5a5825e25eccfdee04a679cd789292e572f67e6271588d7428d`.

On `2026-08-04T02:38:12Z`, the operator accepted this exact lifetime-budget
condition as the single `original-tasks-readable-and-resumable` frozen-baseline
exception. It does not allow modification of original runtime budget, task
timestamps, provenance, profile, code, or tests, and it is not final cutover
acceptance.
