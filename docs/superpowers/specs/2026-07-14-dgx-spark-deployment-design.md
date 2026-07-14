# DGX Spark TriAgent Deployment Design

## 1. Purpose

Deploy the existing TriAgent controller from the Windows source repository at
`D:\workspace` to the reachable NVIDIA DGX Spark host `spark`, without
polluting the DGX system Python or its existing Isaac Lab environments.

This design covers source synchronization, a dedicated Python environment,
DGX-specific configuration, diagnostics, and staged verification. It does not
claim that ChatGPT App remote control, Isaac Lab GUI, WebRTC, or a persistent
systemd service is complete.

## 2. Confirmed environment

The following facts were verified over read-only SSH on 2026-07-14:

- SSH alias: `spark`
- Hostname: `spark-5643`
- User: `ys`
- Home: `/home/ys`
- Operating system: Ubuntu 24.04.3 LTS
- Architecture: `aarch64`
- Default shell: `/usr/bin/zsh`
- GPU: NVIDIA GB10
- Driver: 580.95.05
- Target parent directory: `/home/ys/works/robots`
- Target parent directory currently exists and is empty
- Miniforge root: `/home/ys/miniforge3`
- Existing Conda environments: `base`, `isaaclab`, and `sonic-g1`
- Codex CLI: `/home/ys/.local/bin/codex`
- Cursor CLI selected by the operator: `/home/ys/.local/bin/cursor-agent`
- Antigravity CLI: `/home/ys/.local/bin/agy`
- `/home/ys/.local/bin/agent` also exists but must not be selected by the
  TriAgent DGX profile
- Available infrastructure tools include Git, Python 3.12 system Python,
  NVIDIA SMI, Docker, systemd, tmux, and rsync

## 3. Directory layout

Use three sibling directories:

```text
/home/ys/works/robots/
├── triagent/          # Git working tree for the controller
├── triagent-runs/     # Task database, reports, handoffs, and task worktrees
└── projects/          # Robot project repositories operated on by TriAgent
```

The controller repository must not store task runs, robot projects, model
weights, datasets, credentials, or Isaac Lab caches.

## 4. Source synchronization

### 4.1 Selected approach

Use a Git bundle created from the clean, committed Windows repository and copy
that bundle to DGX over SSH. Clone the bundle into
`/home/ys/works/robots/triagent`.

This preserves Git history, does not require a public hosting service, and
copies only committed repository content. The temporary bundle is transport
material and may be removed only after the remote clone and commit identity
have been verified.

### 4.2 Rejected alternatives

- A source-only rsync snapshot is not selected because it can copy untracked
  files, caches, Windows-specific artifacts, or incomplete local changes and
  does not provide a clean history boundary.
- A GitHub or GitLab remote is not selected because it adds external
  credentials and hosting that are unnecessary for the initial LAN
  deployment.

### 4.3 Preservation rules

- Do not overwrite a non-empty remote `triagent` directory.
- Re-check the target immediately before cloning.
- Do not copy `runs/`, `.tokensave/`, `.pytest_cache/`, local worktrees,
  credentials, or vendor authentication material.
- Do not delete a partial deployment automatically. Preserve it for diagnosis
  and request operator approval before cleanup.
- Commit the previously authored Chinese user guide and the DGX deployment
  changes before producing the final bundle so the remote clone is complete
  and auditable.

## 5. Python isolation

Create a dedicated Conda environment named `triagent` with Python 3.12:

```text
/home/ys/miniforge3/envs/triagent
```

Use the fixed Conda executable
`/home/ys/miniforge3/bin/conda`. Automation must prefer:

```bash
/home/ys/miniforge3/bin/conda run -n triagent <command>
```

This avoids dependence on zsh initialization or `conda activate` and works
consistently over non-interactive SSH, tmux, and future systemd units.

The deployment must not install TriAgent packages into:

- the Miniforge `base` environment;
- the existing `isaaclab` environment;
- the existing `sonic-g1` environment;
- Ubuntu system Python.

Install TriAgent into the dedicated environment in editable mode from
`/home/ys/works/robots/triagent`. Dependency installation may use Conda and
pip only inside that environment.

## 6. DGX profile

Create a real host profile distinct from the existing placeholder example. The
profile must use:

- host name: `spark-5643`
- platform: `ubuntu-24.04`
- hardware: `nvidia-dgx-spark`
- address: `spark`
- runs: `/home/ys/works/robots/triagent-runs`
- workspace: `/home/ys/works/robots/projects`
- Codex command: `/home/ys/.local/bin/codex`
- Cursor command: `/home/ys/.local/bin/cursor-agent`
- Antigravity command: `/home/ys/.local/bin/agy`
- DeepSeek/OpenCode: disabled

The profile must retain bounded execution limits equivalent to the verified
Windows profile:

- maximum 20 agent calls;
- maximum 60 minutes;
- maximum USD estimate 20.0;
- paid overage disabled.

Fixed absolute command paths are required because non-interactive SSH does not
load the same PATH as interactive zsh.

## 7. Bootstrap and diagnostics

Update the DGX bootstrap diagnostic to check `cursor-agent`, not `agent`.
The script must remain read-only unless its existing explicit `--install`
mode is selected interactively.

Diagnostics must report the selected executable paths and versions without
printing tokens, cookies, API keys, private keys, or shell configuration
contents.

The first deployment does not install or modify:

- NVIDIA drivers;
- NVIDIA Container Toolkit;
- Docker configuration;
- Isaac Sim or Isaac Lab;
- zsh configuration;
- vendor CLI authentication;
- systemd services;
- WebRTC services.

## 8. Verification stages

### Stage A: transport and repository integrity

- Confirm the remote destination is still empty before cloning.
- Verify the remote HEAD equals the bundled local commit.
- Confirm the remote working tree is clean.
- Confirm forbidden local runtime directories were not copied.

### Stage B: dedicated environment

- Confirm `conda env list` contains exactly one new `triagent` environment.
- Confirm its Python version is 3.12.
- Confirm existing `isaaclab` and `sonic-g1` environments still exist.
- Confirm `triagent --help` runs through `conda run -n triagent`.

### Stage C: controller tests

- Run the complete TriAgent test suite inside the dedicated environment.
- Run the bootstrap contract tests that cover DGX command names and paths.
- Require zero test failures before any real provider task.

### Stage D: no-model operational checks

- Run `triagent doctor` with the DGX profile.
- Run a `fake` profile task against a small clean Git repository under
  `projects/`.
- Verify state, report, task worktree, and run database are created under
  `triagent-runs`.
- Confirm no vendor model quota was used by this stage.

### Stage E: bounded live smoke

Run at most one minimal live task only after Stages A-D pass. It must:

- use Cursor via `cursor-agent` as implementer;
- use Codex as verifier;
- use Antigravity as reviewer;
- keep DeepSeek/OpenCode disabled;
- require explicit live and billing confirmation;
- target a disposable smoke-test repository, not a robot project;
- stop at the approval state without merge or deployment;
- consume no more than one normal call per provider unless a recoverable
  infrastructure error makes one bounded retry necessary.

## 9. Error handling and rollback

- If Conda environment creation fails, preserve the solver output and do not
  modify existing environments.
- If pip installation fails, keep the dedicated environment for inspection;
  do not install into system Python as a fallback.
- If a provider CLI is unavailable in non-interactive execution, correct the
  fixed path or invocation. Do not modify zsh startup files merely to make
  automation pass.
- If tests fail, stop before fake or live execution.
- If fake execution fails, preserve `triagent-runs` evidence and stop before
  live execution.
- If the live smoke fails recoverably, inspect the persisted failed stage
  before deciding whether to use the single bounded retry.
- No deployment step may remove or modify the existing `isaaclab` or
  `sonic-g1` environments.

Rollback consists of stopping TriAgent tasks and removing only the newly
created `triagent` Conda environment and the three new directories. Any
rollback removal requires a separate explicit operator approval after paths
and ownership have been re-verified.

## 10. Security and approval boundaries

- Vendor credentials remain in their existing user-scoped CLI stores and are
  never copied from Windows.
- No TriAgent port is exposed publicly.
- No Tencent Cloud relay is configured in this deployment.
- No robot project, simulator, actuator, or GUI is started by the deployment.
- No merge, deployment, destructive cleanup, or live provider call is inferred
  from successful installation.
- The live smoke stops at an approval gate; approval recording and execution
  remain separate actions.

## 11. Acceptance criteria

The deployment is accepted when all of the following are true:

1. The repository exists at `/home/ys/works/robots/triagent` at the exact
   expected commit with a clean working tree.
2. The dedicated Python 3.12 `triagent` Conda environment is installed
   without changing `base`, `isaaclab`, or `sonic-g1`.
3. The DGX profile uses `/home/ys/.local/bin/cursor-agent` and fixed paths for
   all three providers.
4. The complete local controller test suite passes on DGX.
5. Doctor and fake task checks pass with all state stored under
   `/home/ys/works/robots/triagent-runs`.
6. If the bounded live smoke is authorized, provenance proves
   Cursor/Codex/Antigravity completed their assigned stages and the task stops
   without merge or deployment.
7. Isaac Lab, GUI, WebRTC, persistent services, and ChatGPT App remote control
   remain explicitly pending rather than being inferred from this deployment.
