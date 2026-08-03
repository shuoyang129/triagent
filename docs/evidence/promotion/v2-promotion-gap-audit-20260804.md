# V2 promotion evidence gap audit — 2026-08-04

This is an audit inventory, not a promotion-evidence record. It cannot authorize
cutover or replace any missing gate artifact.

## Verified retained stage artifacts

| Stage | Retained evidence | Status |
| --- | --- | --- |
| unit-tests | `docs/evidence/promotion/artifacts/descriptors/unit-tests/full-tests.json` SHA-256 `f294c68fb3490406079f26750debe1bc1a065466a4d4b612897173b657643e75` (binds JUnit SHA-256 `c83be4770ab506322eb67dd2f31e4082ee1e7a80a1014a642e099c8db17ee87d`) | Fixed commit `b1a32cc96158dcd0c029940a789addac6136637f`: 447 passed, 9 skipped; only the full-tests gate is captured |
| adapter-contracts | `docs/evidence/promotion/artifacts/adapter-contracts-20260804.log` SHA-256 `b2e47f893ea31111460c0112ceeb9669e4f4e2ac9e4af751a4184b390a0e53c4` | Fresh contract regression retained; per-gate descriptors still required |
| fake-three-stage-workflow | task `c0849048-a9ae-4a25-90fd-b0f22b886540` report/event hashes recorded in `v2-staged-gates-20260802.md` | Retained |
| historical-replay | `docs/evidence/runtime/v2-historical-replay.json` | 12 replay cases retained |
| isolated-no-provider-synthetic | task `ecc3cdb0-6ec2-4ed9-9150-c0fda461c8ca` | Retained |
| isolated-real-provider-synthetic | task `b105ba67-3c25-4ae7-bbb0-d575c058c777` | Retained |
| low-risk-non-robot-project | task `ab116f36-6e98-4ad7-956f-af224953883c`, report SHA-256 `b0ea3b1a7b761c3e97360e7e838b9cccfa3e942b44756ed4b85967c309287620` | Retained |
| humanoid-offline-read-only | task `4be42b96-2afd-430d-9458-80290b10707b`, report SHA-256 `5cc713cfc1a8d59479bed906a72cbb594da249e2f737c3e65972cb2269be8c9a` | Controller fail-closed; no target mutation authority |
| ordinary-new-humanoid-task | task `691c7dbd-14ea-4849-a63a-146273d64690`, report SHA-256 `79b423921ebd2c94852b2863f3c6313b631dbf0ef32ab1fc2dc9615a5b0a4b71` | DeepSeek/OpenCode + immutable unittest + AGY clean |

## Current per-gate captures

The `unit-tests` stage now has independently retained, fixed-contract evidence
for five gates.  All safety-contract JUnit captures below bind source commit
`b1a32cc96158dcd0c029940a789addac6136637f`; their descriptors bind the
corresponding JUnit XML.

| Gate | Descriptor SHA-256 | Result |
| --- | --- | --- |
| full-tests | `f294c68fb3490406079f26750debe1bc1a065466a4d4b612897173b657643e75` | fixed commit `b1a32cc96158dcd0c029940a789addac6136637f`, 447 passed / 9 skipped |
| no-secret-leak | `942084a4d8dcc22057303878bd7b9708f39b17653cf6815b0e4c0ee97f146560` | 5 fixed redaction/distribution contracts passed |
| no-residual-provider-process | `0f30fce8b3b62795aaf116c40e80fe51093fb627728ab5eda44249c8df8ca02b` | 2 fixed cleanup contracts passed |
| no-duplicate-provider-call-after-recovery | `bff731e691b0dd4ab3312d0dca29985a5f4cfd323564d4fcb04f43f71c61891c` | 4 fixed durable-recovery contracts passed |
| no-candidate-misattribution | `8bcba5685055985e320f37235bfb475493eb31962ab852bfda9182a924f22935` | 3 fixed candidate-binding contracts passed |
| legacy-fallback | `aeab121341d73b143be2496806a09264015122ada546ee20a14b5818b099a8d0` | fixed commit `b1a32cc96158dcd0c029940a789addac6136637f`, 7 legacy-boundary contracts passed |

`original-tasks-readable-and-resumable` remains absent. The legacy-fallback capture proves only the v2 isolation and compatibility contract; it does not prove an original historical task can resume without an isolated runtime exercise.
Therefore this is not a complete stage record and cannot advance promotion.


## Unmet cutover evidence

The evaluator requires every stage to independently bind all seven safety gates
to reviewed artifacts. The retained Markdown and reports do not yet provide that
per-gate binding for all stages. No JSON promotion chain was generated.

The next safe action is a fresh, per-stage capture process that emits one
artifact for each gate, then creates content-linked records and evaluates the
chain offline. Until then, public `triagent` remains frozen.
