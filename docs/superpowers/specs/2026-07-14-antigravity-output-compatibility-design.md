# Antigravity output compatibility design

## Context

The bounded DGX live smoke task
`c16c155e-bfe8-44a0-a82b-28fb6f4a777d` completed Cursor implementation and
Codex verification, then stopped recoverably during Antigravity review. The
persisted diagnostic was `invalid_output`, not the already allowlisted
`canonical-output-invalid`. This proves that the response failed before
canonical schema validation: it was malformed JSON or its top level was not
an object.

Antigravity CLI 1.1.2 provides print mode but no JSON or JSONL output option.
The adapter therefore has to handle the model's printed response while
preserving the controller's strict canonical schema boundary.

## Decision

Accept exactly two transport shapes:

1. A JSON object, allowing only surrounding whitespace.
2. One complete Markdown code fence whose body is a JSON object. The opening
   line must be exactly three backticks, optionally followed immediately by
   `json` (case-insensitive), and only whitespace may appear outside the
   fence.

Do not scan prose for braces, select one object from multiple objects, accept
multiple fences, or ignore text before or after a fence. After transport
decoding, the existing role-specific Pydantic models remain authoritative and
unchanged.

## Diagnostics and privacy

Return and persist only categorical diagnostics:

- `json-malformed` when neither accepted transport shape decodes as JSON;
- `json-non-object` when decoded JSON is not an object;
- `canonical-output-invalid` when the object fails the existing role schema.

Add the first two codes to the orchestrator's diagnostic allowlist. Never
persist raw stdout, response excerpts, prompts, task contents, or validation
values. Existing redaction and canonical sanitization continue to run after
transport decoding.

## Implementation boundary

Add one small transport-decoding helper in `triagent.adapters._cli`, expose it
through an `invoke_json` opt-in that defaults to false, and enable the opt-in
only from `AntigravityAdapter`. Do not change the Codex JSONL parser, Cursor
envelope contract, DeepSeek parser behavior, role schemas, prompt schema,
budgeting, approvals, or agent routing.

## Tests

Use test-driven development to cover:

- a valid raw JSON object remains accepted;
- a single complete `json` fence is accepted;
- an unlabelled complete fence is accepted;
- prose around a fence is rejected as `json-malformed`;
- multiple fences are rejected as `json-malformed`;
- malformed JSON is rejected as `json-malformed`;
- a JSON array is rejected as `json-non-object`;
- both new diagnostic codes are persisted by the orchestrator allowlist;
- canonical schema violations remain `canonical-output-invalid`.

Run focused tests, the full local suite, remote focused tests, and the full DGX
suite without live providers. Keep the previous failed task, branch, and
worktree intact.

## Live validation boundary

After all non-live verification passes, stop for a new explicit confirmation.
The preferred live validation is one direct Antigravity reviewer-adapter call
against the preserved task and handoff, rather than repeating Cursor and Codex.
It must not approve, merge, deploy, edit the source repository, or call
DeepSeek. A full three-agent rerun is a separate later decision.
