# V2 staged gate execution evidence — 2026-08-02

This document records completed isolated task gates. It is deliberately not a
passing `v2-promotion-evidence` record: the frozen-original compatibility gate
and the ordinary-humanoid admission gate remain unproven. Nothing here grants
outcome approval, merge approval, cutover, or a public `triagent` change.

## Fake three-stage workflow

- Task: `c0849048-a9ae-4a25-90fd-b0f22b886540`
- Controller/profile: `triagent-v2`, `fake`; provider calls: none.
- Source: `/home/ys/works/robots/projects/triagent-dgx-smoke`, clean before
  and after.
- Terminal state: `APPROVAL`; pending approvals: `outcome`, `merge`.
- Report SHA-256: `5c65fd05036d0c1c9e0ca4f30e118b2972391d1386bad44a0bfd9bf2e7e0b2e0`.
- Event stream SHA-256: `9288d8dbfac551250c49a26836ddd02e83b562472ec832f9137032f04c601b25`.

## Isolated no-provider synthetic task

- Task: `ecc3cdb0-6ec2-4ed9-9150-c0fda461c8ca`
- Controller/profile: `triagent-v2`, `fake`; provider calls: none.
- Source: `/home/ys/works/robots/synthetic-projects/isaac-manual-validation-doc`,
  clean `main` before and after.
- Terminal state: `APPROVAL`; pending approvals: `outcome`, `merge`.
- Report SHA-256: `5c65fd05036d0c1c9e0ca4f30e118b2972391d1386bad44a0bfd9bf2e7e0b2e0`.
- Event stream SHA-256: `6c515ff4516b762e0700821783dd4d2d6a8ade0b0e46b90650ffa14144c8c931`.

## Isolated real-provider synthetic task

- Task: `b105ba67-3c25-4ae7-bbb0-d575c058c777`.
- Controller/profile: `triagent-v2`, `dgx.spark.v2.toml`, with explicit live
  and billing confirmation.
- Implementer: DeepSeek/OpenCode; verifier: Codex; independent reviewer: the
  v2-owned `agy-review-adapter.zsh` binding. Provider calls occurred only via
  the TriAgent controller.
- Source: `/home/ys/works/robots/synthetic-projects/isaac-manual-validation-doc`,
  clean `main` before and after; the candidate only changes `README.md` in the
  isolated v2 task worktree.
- Terminal state: `APPROVAL`; persisted verification and independent review
  are clean; pending approvals: `outcome`, `merge`.
- Report SHA-256: `4b4e00dd381b173233f6fc8fba5ef175a41781d00f4911b1c73bd634de5b2e7f`.
- Event stream SHA-256: `7e9ecf7fe896e864adc7343e015d5c3b6fe84dbb9d44da718f4df5c69afe3847`.

## Explicit holds

- The frozen original baseline records two pre-existing full-test failures and
  does not yet prove that the original CLI can inspect a real task root without
  writing to it. This gate remains open.
- The isolated humanoid read-only task correctly reached `INSPECTION_HOLD`.
  It is fail-closed controller evidence, not admission to an ordinary humanoid
  mutation task. The rollout must not skip that hold.
