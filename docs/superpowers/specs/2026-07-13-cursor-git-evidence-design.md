# Cursor Git Evidence Design

Date: 2026-07-13
Status: approved in conversation

## Problem

The Windows live smoke reached Cursor successfully and Cursor made the requested changes, but the controller stopped before Codex verification. The persisted diagnostic was `cursor-result-non-json`.

Cursor's `--output-format json` guarantees a JSON transport envelope. Its `result` field is still free assistant text, so requiring that text to match TriAgent's canonical implementation schema makes a valid implementation depend on model formatting. This contradicts the existing rule that Codex must not accept an implementation agent's textual self-assessment as proof of completion.

## Decision

For the Cursor implementer only, the controller will treat the isolated Git worktree as the authoritative implementation evidence.

A Cursor call is transport-successful only when the process exits successfully and the outer Cursor envelope validates as `type=result`, `subtype=success`, and `is_error=false`. The controller will not parse, persist, or trust the envelope's free-text `result` as an implementation outcome.

On transport success, the Cursor adapter will return a controller-owned minimal `AgentResult`: `status=succeeded`, canonical stage status `passed`, fixed summary code `completed`, and empty evidence/artifact lists. It will not copy any vendor prose into that result. An actual-cost value is accepted only from a validated outer transport field when Cursor supplies one; otherwise the existing conservative reservation remains authoritative.

After transport success, the controller will derive the candidate from the worktree relative to the recorded base commit. The existing candidate-manifest path remains authoritative for tracked changes, untracked files, deletions, file modes, control-file protection, path and size limits, secret checks, stable rereads, and reviewed-commit creation. Cursor-supplied `changed_paths` will not be used.

Codex remains responsible for determining whether the implementation satisfies the task and whether tests pass. Antigravity remains responsible for independent review of the task, reviewed diff, and verification evidence.

## Data Flow

1. TriAgent creates a clean task branch and isolated worktree at the recorded base commit.
2. Cursor runs headlessly in that worktree.
3. The Cursor adapter validates only the process result and outer transport envelope. Vendor free text is discarded.
4. The orchestrator asks the store to materialize the candidate without a model-supplied path list.
5. The store derives the actual change set from Git and filesystem state, applies all existing candidate safety checks, rereads for stability, and creates the reviewed commit and private reviewed ref.
6. If the derived change set is empty, implementation stops as `FAILED_RECOVERABLE` with the fixed diagnostic `cursor-no-worktree-change`. The first version does not support no-change implementation tasks.
7. The controller generates the canonical handoff from the reviewed diff.
8. Codex verifies the candidate. Only a passing verification advances to Antigravity review.

## Failure Handling

- Missing executable, authentication failure, timeout, non-zero exit, or malformed outer envelope: fail at the Cursor stage.
- Valid Cursor envelope but arbitrary free text: do not fail and do not persist the text; continue to deterministic Git inspection.
- Empty change set: fail recoverably with `cursor-no-worktree-change`.
- Invalid, unstable, oversized, out-of-scope, control-file, special-file, or secret-bearing candidate: fail closed through the existing manifest checks.
- Verification or review failure: use the existing bounded repair state machine.

No additional Cursor formatting-repair call is introduced. DeepSeek routing and all billing gates remain unchanged.

## Component Boundaries

- `CursorAdapter`: validates Cursor transport; does not interpret implementation claims.
- `Orchestrator`: sequences transport success, candidate materialization, handoff, verification, and review.
- `TaskStore`: owns deterministic change discovery, candidate validation, and reviewed commit creation.
- `CodexAdapter`: owns verification output.
- `AntigravityAdapter`: owns independent review output.

## Tests

The implementation must add regression coverage for:

1. A valid Cursor envelope with non-JSON `result` advances when the worktree contains a valid change.
2. Cursor free text is absent from `AgentResult`, SQLite, events, handoff, and reports.
3. A valid envelope with no worktree changes fails as `cursor-no-worktree-change`.
4. An invalid outer envelope still fails as `cursor-envelope-invalid`.
5. Git-derived changes, including untracked files and deletions, remain subject to existing candidate safety checks.
6. Model-supplied path claims cannot override the Git-derived manifest.
7. The complete automated test suite passes.
8. One bounded live smoke demonstrates Cursor implementation, Codex verification, and Antigravity review; DeepSeek remains disabled.

## Out of Scope

- Accepting arbitrary embedded or fenced JSON from Cursor free text.
- A second model call whose only purpose is output-format repair.
- No-change implementation tasks.
- Changes to Codex, Antigravity, DeepSeek, remote access, DGX, or GUI workflows.
