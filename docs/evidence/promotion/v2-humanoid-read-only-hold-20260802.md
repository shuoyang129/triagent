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
