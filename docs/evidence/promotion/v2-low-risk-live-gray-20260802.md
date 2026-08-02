# V2 low-risk live gray evidence — 2026-08-02

This is factual evidence for the isolated low-risk live gray task. It is not a
promotion-evidence JSON record and does not advance the promotion ladder: the
required earlier structured stage records have not been established. It never
authorizes outcome approval, merge approval, public entry-point cutover, or any
change to the frozen original controller.

## Task boundary

- Controller: isolated `triagent-v2`, commit `f88b3161b485ace58d2a21dcb27f1a2e96e342de`.
- Task: `b86e9af9-251f-48b5-b42a-89edcb942ad9`.
- Mode: live provider, billing-confirmed, low-risk, non-robot documentation-only task.
- Source: `/home/ys/works/robots/projects/triagent-dgx-smoke`.
- Source checkout before and after: clean `main`; the task candidate remains only in
  the isolated v2 run worktree.
- Implementer: DeepSeek/OpenCode; verifier: Codex; reviewer: the v2-owned
  `agy-review-adapter.zsh` binding.

## Persisted outcome

The task is in `APPROVAL`. Its persisted report records that the candidate
changes only `README.md`; verification found the acceptance criterion met and
the independent review is clean. `pytest` collected no tests for the
documentation-only sample, which was recorded as non-required rather than as a
passing test claim. Pending approvals remain `outcome` and `merge`.

| Artifact | SHA-256 |
| --- | --- |
| Task final report | `2f28edb0cabf0ff9d6b62153dc18437ac8464c36f044492d2c8fed51d3c408a2` |
| Task event stream | `daf11dd51366185a275a17ca01a86aec556fb506bbee96dd0a881028eddae6f3` |
| Durable/recovery related offline tests (103 passed) | `b9fa5423d36926b0597bad7f3cb095577b0709f228a3f6158036f2bea321ecd0` |
| Full isolated v2 tests (428 passed, 9 skipped) | `ea7dbce1c04e4cbfc9bd5f14d5c926f2a56fcbcd3493278ad74391fdd587530d` |

## Recovery strengthening after the gray run

Commit `f88b316` extends the trusted durable-completion protocol to live
implementers. For a v2 task with a runtime manifest, the controller now:

1. persists the pre-call input binding outside the provider worktree;
2. validates and materializes the candidate after a successful implementation;
3. binds the durable result to that stable candidate before settling the ledger; and
4. restores and consumes that result after a controller crash without a second
   provider invocation.

The crash-injection test covers the DeepSeek implementation case, in addition
to the existing Codex verifier and AGY reviewer cases. Legacy live tasks that
have no v2 runtime manifest retain their prior candidate-materialization path.
