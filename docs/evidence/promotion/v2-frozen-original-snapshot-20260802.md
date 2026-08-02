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
