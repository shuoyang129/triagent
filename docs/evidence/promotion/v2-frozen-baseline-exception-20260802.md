# Frozen original baseline exception — 2026-08-02

## Decision

The operator accepted the original `triagent` controller as frozen.  Its two
already-recorded historical test drifts are accepted as a **frozen baseline
exception** for the v2 promotion assessment.  The exception does not authorize
any change to the original controller, its profile, or its tests.

## Exact scope

The allowlist is limited to the two failures recorded in
[`original-controller-acfd40b05296.json`](../runtime/original-controller-acfd40b05296.json):

1. The Codex adapter intentionally allowlists inherited `CODEX_HOME` while the
   historical test expected rejection.
2. The frozen profile configures `deepseek-v4-flash` while the historical test
   expected `deepseek-v4-pro`.

The recorded original baseline is `2 failed, 294 passed, 9 skipped`.  No other
failure, drift, missing test, or changed byte is covered by this exception.

## Boundaries and evidence

- The deployed original commit, profile hashes, and wrapper bytes remain
  captured in the immutable-controller record above.
- Read-only compatibility was verified against a byte-identical temporary copy
  of a recoverable original task; see
  [`v2-frozen-original-snapshot-20260802.md`](v2-frozen-original-snapshot-20260802.md).
- This exception removes only the original-baseline test-drift hold.  It does
  not satisfy, weaken, or bypass any v2 staged gate, including the ordinary
  humanoid robot-safety admission gate.
- Public-name cutover remains forbidden until the linked promotion evidence
  chain is complete and the final rollout gate accepts it.
