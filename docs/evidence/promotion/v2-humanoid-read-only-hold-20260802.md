# V2 humanoid read-only promotion hold — 2026-08-02

This is a hold record, not a passing promotion-evidence record.  The promotion
evaluator deliberately accepts only passing records; producing one for this
stage would incorrectly skip a failed rollout level.

## Task boundary

- Controller: isolated `triagent-v2` only.
- Task: `1b94ffa1-a50d-477b-905c-a86cdb785e83`.
- Mode: live-provider, billing-confirmed, strictly read-only inspection.
- Source: `/home/ys/works/robots/projects/humanoid`.
- Before and after source snapshot: `aa1670548deea36721c916d0599433b2ac2fe1ad`, clean porcelain state.
- No worktree, candidate commit, tests, deployment, service, simulator, network
  control, or hardware action was created by the task.

## Persisted local artifacts

The run data is outside any provider worktree and remains in the isolated v2
data root.  SHA-256 values bind this record to its sanitized persisted output:

| Artifact | SHA-256 |
| --- | --- |
| `final-report.md` | `be0720aebb7787a04697a9cf713c792d50fb2a1fe78e2f889ebea21dc5821eaf` |
| `events.jsonl` | `4cffba8758ac5a6e78ace5d34d09a507ee50f94c9a26a35fa837e2bd2aac0c33` |
| `source-snapshot.json` | `6c3a9966e8ebdc8be40dc69be59177858a0a5beff31fa516790b75cd493e3635` |

## Independent finding hold

The Codex read-only verification completed; the independent AGY review returned
blocking residual findings.  The persisted finding codes are:

- `DDS_SHARED_OWNERSHIP_NO_EXCLUSIVITY`
- `UNVERIFIED_REMOTE_WRITER_IDENTITY`
- `SAFE_HOLD_CONTINUITY_UNPROVEN`

## Semantic validation — current isolated controller

Task `4be42b96-2afd-430d-9458-80290b10707b` repeated the same live,
billing-confirmed, strictly read-only boundary with the current v2 controller.
It completed verification and review, then recorded
`read-only-target-admission-denied` and stopped in `INSPECTION_HOLD`.

This is controller acceptance evidence: a target-risk conclusion is represented
as an intentional fail-closed hold, not as `FAILED_FINAL`. It does not admit
this target to any mutation, deployment, simulation, service, or hardware
stage. The source snapshot is identical before and after.

| Artifact | SHA-256 |
| --- | --- |
| `final-report.md` | `5cc713cfc1a8d59479bed906a72cbb594da249e2f737c3e65972cb2269be8c9a` |
| `events.jsonl` | `3ed0cf48d504385f8d0ce6c9fd3bacbfac37bc0a4c23af38be8b68a97bf898dd` |
| `source-snapshot.json` | `6c3a9966e8ebdc8be40dc69be59177858a0a5beff31fa516790b75cd493e3635` |
| `timeout-selections.json` | `fc1b0f43fda9d38a8629bf940dcd36e33c5cbc7157b4345b50c83c268c2cbcd0` |
