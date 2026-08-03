# V2 promotion evidence gap audit — 2026-08-04

This is an audit inventory, not a promotion-evidence record. It cannot authorize
cutover or replace any missing gate artifact.

## Verified retained stage artifacts

| Stage | Retained evidence | Status |
| --- | --- | --- |
| unit-tests | `docs/evidence/promotion/artifacts/descriptors/unit-tests/full-tests.json` SHA-256 `26eaf67a850f109bc88558952506506953aaeccbfe92d49bc6b67e907d64c26d` (binds JUnit SHA-256 `45f60ee4b8085e0ec34e1460049c8dd8f092542d71670e230c1109f08807778a`) | Fixed commit `0467e946fd045100c402d4abd1b20aa5558ed20f`: 446 passed, 9 skipped; only the full-tests gate is captured |
| adapter-contracts | `docs/evidence/promotion/artifacts/adapter-contracts-20260804.log` SHA-256 `b2e47f893ea31111460c0112ceeb9669e4f4e2ac9e4af751a4184b390a0e53c4` | Fresh contract regression retained; per-gate descriptors still required |
| fake-three-stage-workflow | task `c0849048-a9ae-4a25-90fd-b0f22b886540` report/event hashes recorded in `v2-staged-gates-20260802.md` | Retained |
| historical-replay | `docs/evidence/runtime/v2-historical-replay.json` | 12 replay cases retained |
| isolated-no-provider-synthetic | task `ecc3cdb0-6ec2-4ed9-9150-c0fda461c8ca` | Retained |
| isolated-real-provider-synthetic | task `b105ba67-3c25-4ae7-bbb0-d575c058c777` | Retained |
| low-risk-non-robot-project | task `ab116f36-6e98-4ad7-956f-af224953883c`, report SHA-256 `b0ea3b1a7b761c3e97360e7e838b9cccfa3e942b44756ed4b85967c309287620` | Retained |
| humanoid-offline-read-only | task `4be42b96-2afd-430d-9458-80290b10707b`, report SHA-256 `5cc713cfc1a8d59479bed906a72cbb594da249e2f737c3e65972cb2269be8c9a` | Controller fail-closed; no target mutation authority |
| ordinary-new-humanoid-task | task `691c7dbd-14ea-4849-a63a-146273d64690`, report SHA-256 `79b423921ebd2c94852b2863f3c6313b631dbf0ef32ab1fc2dc9615a5b0a4b71` | DeepSeek/OpenCode + immutable unittest + AGY clean |
\n## Current per-gate captures\n\nThe `unit-tests` stage now has independently retained, fixed-contract evidence\nfor five gates.  All safety-contract JUnit captures below bind source commit\n`6827cd1fb931c4c9848fbe727b2be29f72eb78c9`; their descriptors bind the\ncorresponding JUnit XML.\n\n| Gate | Descriptor SHA-256 | Result |\n| --- | --- | --- |\n| full-tests | `26eaf67a850f109bc88558952506506953aaeccbfe92d49bc6b67e907d64c26d` | fixed commit `0467e946fd045100c402d4abd1b20aa5558ed20f`, 446 passed / 9 skipped |\n| no-secret-leak | `16c1a3c719111b58b43347c8d15d513096024ef642e15b59154ee9bb1ba707f9` | 5 fixed redaction/distribution contracts passed |\n| no-residual-provider-process | `e82f01f73b456992eaf2e6d21641e0340353ea9439918ecc5e12cce60943b9a0` | 2 fixed cleanup contracts passed |\n| no-duplicate-provider-call-after-recovery | `9f12742ce8150259e878331d301f93aebe0a08e291017aec673548170da5e693` | 4 fixed durable-recovery contracts passed |\n| no-candidate-misattribution | `1f7d90ba68331acdb669addef9f2d74ce2481a3242d61a94c3ceb079ceba0daa` | 3 fixed candidate-binding contracts passed |\n\n`legacy-fallback` and `original-tasks-readable-and-resumable` remain absent.\nTherefore this is not a complete stage record and cannot advance promotion.\n\n
## Unmet cutover evidence

The evaluator requires every stage to independently bind all seven safety gates
to reviewed artifacts. The retained Markdown and reports do not yet provide that
per-gate binding for all stages. No JSON promotion chain was generated.

The next safe action is a fresh, per-stage capture process that emits one
artifact for each gate, then creates content-linked records and evaluates the
chain offline. Until then, public `triagent` remains frozen.
