# DGX Spark TriAgent Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install TriAgent on SSH host `spark` under `/home/ys/works/robots`, with a dedicated Python 3.12 Conda environment and `/home/ys/.local/bin/cursor-agent` as the Cursor CLI.

**Architecture:** Keep `D:\workspace` as the source of truth, send only committed Git history through a bundle, and clone it to `/home/ys/works/robots/triagent`. Keep runs and robot repositories in sibling directories, use fixed provider paths, and execute Python through `/home/ys/miniforge3/bin/conda run -n triagent` so zsh, SSH, tmux, and future systemd calls behave consistently.

**Tech Stack:** PowerShell, OpenSSH/SCP, Git bundle, Ubuntu 24.04.3 aarch64, zsh, Miniforge/Conda, Python 3.12, Bash, pytest, TOML, Codex CLI, Cursor Agent CLI, Antigravity CLI.

## Global Constraints

- Source repository: `D:\workspace`.
- SSH target: `spark`; verified host `spark-5643`, user `ys`.
- Remote repository: `/home/ys/works/robots/triagent`.
- Runs: `/home/ys/works/robots/triagent-runs`.
- Robot projects: `/home/ys/works/robots/projects`.
- Conda: `/home/ys/miniforge3/bin/conda`.
- New environment: `/home/ys/miniforge3/envs/triagent`, Python 3.12.
- Do not modify `base`, `isaaclab`, `sonic-g1`, or system Python.
- Provider commands are `/home/ys/.local/bin/codex`, `/home/ys/.local/bin/cursor-agent`, and `/home/ys/.local/bin/agy`.
- Do not select `/home/ys/.local/bin/agent`.
- DeepSeek/OpenCode remains disabled.
- Budget is at most 20 calls, 60 minutes, and USD estimate 20.0; paid overage is disabled.
- Do not expose a public port or configure Tencent relay, systemd, GUI, Isaac Lab, WebRTC, robot hardware, merge, or deployment.
- No real provider task runs before local tests, remote tests, doctor, and fake verification pass.
- A live smoke requires a separate confirmation at its checkpoint and stops at `APPROVAL`.

---

### Task 1: Commit the operator guide and this plan

**Files:**
- Add: `docs/triagent-user-guide-zh.md`
- Add: `docs/superpowers/plans/2026-07-14-dgx-spark-deployment-plan.md`

**Interfaces:**
- Consumes: the approved design and verified Chinese guide.
- Produces: committed documentation included in the bundle.

- [ ] **Step 1: Confirm the expected untracked files**

Run:

```powershell
Set-Location D:\workspace
git status --short
```

Expected: only the plan and `docs/triagent-user-guide-zh.md` are untracked.

- [ ] **Step 2: Validate encoding and Markdown fences**

Run:

```powershell
$files = @(
  'docs\triagent-user-guide-zh.md',
  'docs\superpowers\plans\2026-07-14-dgx-spark-deployment-plan.md'
)
foreach ($file in $files) {
  $text = Get-Content -LiteralPath $file -Raw -Encoding UTF8
  if ($text.Contains([char]0xFFFD) -or $text.Contains([char]0x951F)) { throw "Encoding artifact: $file" }
  $fences = ([regex]::Matches($text, '(?m)^\x60\x60\x60')).Count
  if ($fences % 2 -ne 0) { throw "Unbalanced fences: $file" }
}
```

Expected: exit code 0.

- [ ] **Step 3: Commit only these files**

Run:

```powershell
git add -- docs/triagent-user-guide-zh.md docs/superpowers/plans/2026-07-14-dgx-spark-deployment-plan.md
git diff --cached --check
git commit -m "docs: add operator and DGX deployment guides"
```

Expected: one documentation commit.

---

### Task 2: Add a concrete DGX profile and Linux test compatibility

**Files:**
- Create: `profiles/dgx.spark.toml`
- Create: `tests/test_dgx_deployment_contract.py`
- Modify: `tests/test_bootstrap_contract.py`

**Interfaces:**
- Consumes: profile parsing in `src/triagent/cli.py`.
- Produces: a concrete, secret-free profile and a test suite runnable on Ubuntu.

- [ ] **Step 1: Write the failing profile contract**

Create `tests/test_dgx_deployment_contract.py`:

```python
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "dgx.spark.toml"
BOOTSTRAP = ROOT / "scripts" / "bootstrap-dgx.sh"
INSTALLER = ROOT / "scripts" / "install-triagent-dgx.sh"


def test_concrete_dgx_profile_uses_verified_paths_and_budgets() -> None:
    config = tomllib.loads(PROFILE.read_text(encoding="utf-8"))
    assert config["host"] == {
        "name": "spark-5643",
        "platform": "ubuntu-24.04",
        "hardware": "nvidia-dgx-spark",
        "address": "spark",
    }
    assert config["paths"]["runs"] == "/home/ys/works/robots/triagent-runs"
    assert config["paths"]["workspace"] == "/home/ys/works/robots/projects"
    assert config["paths"]["python"] == "/home/ys/miniforge3/envs/triagent/bin/python"
    assert config["agents"]["codex"]["command"] == ["/home/ys/.local/bin/codex"]
    assert config["agents"]["cursor"]["command"] == ["/home/ys/.local/bin/cursor-agent"]
    assert config["agents"]["antigravity"]["command"] == ["/home/ys/.local/bin/agy"]
    assert config["agents"]["opencode"]["enabled"] is False
    assert config["budget"]["max_agent_calls"] == 20
    assert config["budget"]["max_minutes"] == 60
    assert config["budget"]["max_usd"] == 20.0
    assert config["budget"]["allow_paid_overage"] is False


def test_concrete_dgx_profile_never_selects_agent_alias() -> None:
    text = PROFILE.read_text(encoding="utf-8")
    assert 'command = ["/home/ys/.local/bin/agent"]' not in text
```

- [ ] **Step 2: Confirm the test fails**

Run:

```powershell
python -m pytest tests/test_dgx_deployment_contract.py -q
```

Expected: failure because `profiles/dgx.spark.toml` is absent.

- [ ] **Step 3: Create `profiles/dgx.spark.toml`**

```toml
[host]
name = "spark-5643"
platform = "ubuntu-24.04"
hardware = "nvidia-dgx-spark"
address = "spark"

[paths]
runs = "/home/ys/works/robots/triagent-runs"
workspace = "/home/ys/works/robots/projects"
python = "/home/ys/miniforge3/envs/triagent/bin/python"

[capabilities]
nvidia_gpu = true
container_runtime = "nvidia-container-toolkit"
robot_simulation = "isaac-lab"
visualization = ["local-display", "webrtc"]
background_execution = ["systemd-user", "tmux"]

[agents.codex]
command = ["/home/ys/.local/bin/codex"]
estimated_usd = 1.0

[agents.cursor]
command = ["/home/ys/.local/bin/cursor-agent"]
estimated_usd = 1.0

[agents.antigravity]
command = ["/home/ys/.local/bin/agy"]
estimated_usd = 1.0

[agents.opencode]
enabled = false
command = ["/home/ys/.local/bin/opencode"]
estimated_usd = 1.0
probe_estimated_usd = 0.25

[budget]
max_agent_calls = 20
max_minutes = 60
max_usd = 20.0
cursor_saver_threshold = 0.70
cursor_handoff_threshold = 0.90
allow_paid_overage = false
```

- [ ] **Step 4: Skip the Windows-only runtime test on Ubuntu**

Add `import shutil` to `tests/test_bootstrap_contract.py` and decorate
`test_native_probe_contract_checks_nonzero_last_exit_code`:

```python
@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell is unavailable")
def test_native_probe_contract_checks_nonzero_last_exit_code() -> None:
    script = Path("scripts/bootstrap-windows.ps1").read_text(encoding="utf-8")
    assert "$LASTEXITCODE" in script and "-eq 0" in script
    command = "$o = (& { cmd /c 'echo plausible-output & exit /b 7' } | Out-String).Trim(); if ($LASTEXITCODE -eq 0) { exit 9 } else { exit 0 }"
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command], check=False)
    assert result.returncode == 0
```

- [ ] **Step 5: Run and commit**

Run:

```powershell
python -m pytest tests/test_dgx_deployment_contract.py tests/test_bootstrap_contract.py -q
git add -- profiles/dgx.spark.toml tests/test_dgx_deployment_contract.py tests/test_bootstrap_contract.py
git diff --cached --check
git commit -m "feat: add concrete DGX Spark profile"
```

Expected: zero failures and one focused commit.

---

### Task 3: Add fixed-path diagnostics and isolated installation

**Files:**
- Modify: `scripts/bootstrap-dgx.sh`
- Create: `scripts/install-triagent-dgx.sh`
- Modify: `tests/test_dgx_deployment_contract.py`

**Interfaces:**
- Consumes: `HOME=/home/ys`, Miniforge, and the cloned repository.
- Produces: a read-only diagnostic and an explicit `--apply` installer.

- [ ] **Step 1: Append failing script tests**

```python
def test_dgx_diagnostic_uses_fixed_vendor_and_conda_paths() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "$HOME/.local/bin/codex" in text
    assert "$HOME/.local/bin/cursor-agent" in text
    assert "$HOME/.local/bin/agy" in text
    assert "$HOME/miniforge3/bin/conda" in text
    assert "$HOME/.local/bin/agent" not in text


def test_dgx_installer_is_explicit_and_avoids_system_installation() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "--apply" in text
    assert 'env_name="triagent"' in text
    assert "python=3.12" in text
    assert "triagent-runs" in text
    assert "projects" in text
    assert "sudo" not in text
    assert "apt-get" not in text
    assert "conda install" not in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is unavailable")
def test_dgx_scripts_have_valid_bash_syntax() -> None:
    for script in (BOOTSTRAP, INSTALLER):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Confirm failures**

Run:

```powershell
python -m pytest tests/test_dgx_deployment_contract.py -q
```

Expected: fixed-path and missing-installer failures.

- [ ] **Step 3: Replace `scripts/bootstrap-dgx.sh`**

```bash
#!/usr/bin/env bash
set -uo pipefail
set -- "${1:-}"

install=false
case "$1" in
  "") ;;
  --install) install=true ;;
  *) printf 'Usage: %s [--install]\n' "$0" >&2; exit 2 ;;
esac

: "$HOME"

check_command() {
  local label="$1"
  local executable="$2"
  if [[ "$executable" == /* ]]; then
    [[ -x "$executable" ]] && printf 'available: %s=%s\n' "$label" "$executable" || printf 'missing: %s=%s\n' "$label" "$executable"
  elif command -v "$executable" >/dev/null 2>&1; then
    printf 'available: %s=%s\n' "$label" "$(command -v "$executable")"
  else
    printf 'missing: %s=%s\n' "$label" "$executable"
  fi
}

printf 'DGX diagnostic mode: read-only capability checks\n'
check_command python3 python3
check_command git git
check_command codex "$HOME/.local/bin/codex"
check_command cursor-agent "$HOME/.local/bin/cursor-agent"
check_command antigravity "$HOME/.local/bin/agy"
check_command conda "$HOME/miniforge3/bin/conda"
check_command nvidia-smi nvidia-smi
check_command docker docker
check_command systemctl systemctl
check_command tmux tmux
check_command rsync rsync

if [[ "$install" != true ]]; then
  printf 'Diagnostics complete. No system components were changed.\n'
  exit 0
fi
if [[ ! -t 0 ]]; then
  printf 'Installation cancelled: interactive confirmation is required.\n' >&2
  exit 3
fi
printf 'Type INSTALL to permit apt package installation: '
IFS= read -r confirm
[[ "$confirm" == "INSTALL" ]] || { printf 'Installation cancelled.\n' >&2; exit 3; }
sudo apt-get update
sudo apt-get install -y python3 git tmux
```

- [ ] **Step 4: Create `scripts/install-triagent-dgx.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
set -- "${1:-}"

mode="check"
case "$1" in
  "") ;;
  --check) mode="check" ;;
  --apply) mode="apply" ;;
  *) printf 'Usage: %s [--check|--apply]\n' "$0" >&2; exit 2 ;;
esac

: "$HOME"
conda_path="$HOME/miniforge3/bin/conda"
robot_root="$HOME/works/robots"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
runs_root="$robot_root/triagent-runs"
projects_root="$robot_root/projects"
env_name="triagent"
env_path="$HOME/miniforge3/envs/$env_name"

[[ -x "$conda_path" ]] || { printf 'Conda missing: %s\n' "$conda_path" >&2; exit 4; }
[[ -f "$repo_root/pyproject.toml" ]] || { printf 'Repository invalid: %s\n' "$repo_root" >&2; exit 5; }

printf 'repository=%s\nconda=%s\nenvironment=%s\nruns=%s\nprojects=%s\n' "$repo_root" "$conda_path" "$env_path" "$runs_root" "$projects_root"

if [[ "$mode" == "check" ]]; then
  [[ -d "$env_path" ]] && "$conda_path" run -n "$env_name" python --version || printf 'environment-status=missing\n'
  printf 'Check complete. No files or environments were changed.\n'
  exit 0
fi

mkdir -p "$runs_root" "$projects_root"
if [[ -d "$env_path" ]]; then
  python_version="$("$conda_path" run -n "$env_name" python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  [[ "$python_version" == "3.12" ]] || { printf 'Existing triagent Python is %s, expected 3.12\n' "$python_version" >&2; exit 6; }
else
  "$conda_path" create -y -n "$env_name" python=3.12 pip
fi

"$conda_path" run -n "$env_name" python -m pip install -e "$repo_root[test]"
"$conda_path" run -n "$env_name" python -m triagent.cli --help >/dev/null
printf 'TriAgent environment installation complete.\n'
```

- [ ] **Step 5: Run and commit**

Run:

```powershell
python -m pytest tests/test_dgx_deployment_contract.py tests/test_packaging.py -q
git add -- scripts/bootstrap-dgx.sh scripts/install-triagent-dgx.sh tests/test_dgx_deployment_contract.py
git diff --cached --check
git commit -m "feat: add isolated DGX installation workflow"
```

Expected: zero failures and one focused commit.

---

### Task 4: Add the concrete DGX runbook

**Files:**
- Create: `docs/operations/dgx-bootstrap.md`
- Modify: `docs/operations/dgx-onsite-checklist.md`

**Interfaces:**
- Consumes: the concrete profile and scripts.
- Produces: exact operator commands and a readable milestone boundary.

- [ ] **Step 1: Create `docs/operations/dgx-bootstrap.md`**

````markdown
# DGX Spark TriAgent bootstrap

## Verified host

- SSH alias: `spark`
- Host/user: `spark-5643` / `ys`
- OS: Ubuntu 24.04.3 LTS, aarch64
- Default shell: zsh
- Miniforge: `/home/ys/miniforge3`
- TriAgent environment: `/home/ys/miniforge3/envs/triagent`
- Controller: `/home/ys/works/robots/triagent`
- Runs: `/home/ys/works/robots/triagent-runs`
- Projects: `/home/ys/works/robots/projects`

## Diagnostics and installation

```bash
cd /home/ys/works/robots/triagent
bash scripts/bootstrap-dgx.sh
bash scripts/install-triagent-dgx.sh --check
bash scripts/install-triagent-dgx.sh --apply
```

The installer creates only the `triagent` Python 3.12 environment and the
`triagent-runs` and `projects` directories. It does not modify `base`,
`isaaclab`, `sonic-g1`, system Python, NVIDIA, Docker, or zsh.

## Controller

```bash
CONDA=/home/ys/miniforge3/bin/conda
PROFILE=/home/ys/works/robots/triagent/profiles/dgx.spark.toml
DATA_ROOT=/home/ys/works/robots/triagent-runs
$CONDA run -n triagent triagent doctor --profile "$PROFILE"
$CONDA run -n triagent triagent status TASK_ID --data-root "$DATA_ROOT"
$CONDA run -n triagent triagent report TASK_ID --data-root "$DATA_ROOT"
```

Use only a task ID printed by TriAgent. The profile selects
`/home/ys/.local/bin/cursor-agent`; do not substitute `agent`.
DeepSeek/OpenCode remains disabled.

Isaac GUI, WebRTC, systemd persistence, ChatGPT App remote control, and robot
hardware remain separate onsite gates.
````

- [ ] **Step 2: Correct the garbled checklist ending**

Replace the final milestone paragraph in
`docs/operations/dgx-onsite-checklist.md` with:

```markdown
## Milestone boundary

Windows local three-agent execution has passed. DGX controller installation,
CLI capability, NVIDIA, tmux, GUI, Isaac Lab, WebRTC, and ChatGPT App remote
control must be recorded independently on the real host; none may be inferred
from simulation or Windows-only tests.
```

- [ ] **Step 3: Test and commit**

Run:

```powershell
python -m pytest tests/test_packaging.py tests/test_dgx_deployment_contract.py -q
git diff --check
git add -- docs/operations/dgx-bootstrap.md docs/operations/dgx-onsite-checklist.md
git diff --cached --check
git commit -m "docs: add DGX Spark bootstrap runbook"
```

Expected: zero failures and one documentation commit.

---

### Task 5: Complete local verification

**Files:** Verify only.

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: a clean, tested commit eligible for bundling.

- [ ] **Step 1: Run all tests**

```powershell
python -m pytest -q
```

Expected: zero failures; explicitly selected live/onsite tests may skip.

- [ ] **Step 2: Run focused contracts**

```powershell
python -m pytest tests/test_dgx_deployment_contract.py tests/test_bootstrap_contract.py tests/test_packaging.py -q
```

Expected: zero failures.

- [ ] **Step 3: Verify Git**

```powershell
git diff --check
git status --short
git log -6 --oneline
```

Expected: clean status and all design/deployment commits present.

---

### Task 6: Bundle and clone on DGX

**Files:**
- Temporary: `D:\workspace\work\triagent-dgx.bundle`
- Temporary: `/home/ys/works/robots/triagent-dgx.bundle`
- Create: `/home/ys/works/robots/triagent/`

**Interfaces:**
- Consumes: clean local `main`.
- Produces: a remote clone at the exact local commit.

- [ ] **Step 1: Refuse overwrite**

```powershell
ssh -o BatchMode=yes spark "test ! -e /home/ys/works/robots/triagent"
```

Expected: exit code 0; otherwise stop.

- [ ] **Step 2: Create and verify bundle**

```powershell
New-Item -ItemType Directory -Force -Path D:\workspace\work | Out-Null
git bundle create D:\workspace\work\triagent-dgx.bundle main
git bundle verify D:\workspace\work\triagent-dgx.bundle
```

Expected: complete bundle containing `refs/heads/main`.

- [ ] **Step 3: Copy and clone**

```powershell
scp D:\workspace\work\triagent-dgx.bundle spark:/home/ys/works/robots/triagent-dgx.bundle
ssh spark "git clone /home/ys/works/robots/triagent-dgx.bundle /home/ys/works/robots/triagent"
```

Expected: new clone created.

- [ ] **Step 4: Verify commit and clean state**

```powershell
$localCommit = (git rev-parse HEAD).Trim()
$remoteCommit = (ssh spark "git -C /home/ys/works/robots/triagent rev-parse HEAD").Trim()
if ($localCommit -ne $remoteCommit) { throw "Commit mismatch" }
ssh spark "git -C /home/ys/works/robots/triagent status --short"
```

Expected: matching commits and empty remote status.

- [ ] **Step 5: Remove only verified bundle files**

```powershell
ssh spark "rm -f /home/ys/works/robots/triagent-dgx.bundle"
Remove-Item -LiteralPath D:\workspace\work\triagent-dgx.bundle -Force
```

Expected: only transport bundles removed.

---

### Task 7: Create the isolated environment

**Files:**
- Create: `/home/ys/miniforge3/envs/triagent/`
- Create: `/home/ys/works/robots/triagent-runs/`
- Create: `/home/ys/works/robots/projects/`

**Interfaces:**
- Consumes: clone and Miniforge.
- Produces: Python 3.12 with editable TriAgent and pytest.

- [ ] **Step 1: Read-only preflight**

```powershell
ssh spark "cd /home/ys/works/robots/triagent && bash scripts/bootstrap-dgx.sh && bash scripts/install-triagent-dgx.sh --check"
```

Expected: fixed provider/Conda paths and no mutation.

- [ ] **Step 2: Capture existing environments**

```powershell
ssh spark "/home/ys/miniforge3/bin/conda env list"
```

Expected: `base`, `isaaclab`, and `sonic-g1`.

- [ ] **Step 3: Apply installation**

```powershell
ssh spark "cd /home/ys/works/robots/triagent && bash scripts/install-triagent-dgx.sh --apply"
```

Expected: new `triagent` Python 3.12 environment and completion message.

- [ ] **Step 4: Verify isolation**

```powershell
ssh spark "/home/ys/miniforge3/bin/conda run -n triagent python -c 'import sys; print(sys.executable); print(sys.version)'"
ssh spark "/home/ys/miniforge3/bin/conda env list"
```

Expected: `/home/ys/miniforge3/envs/triagent/bin/python`, Python 3.12, and all
three original environments still present.

---

### Task 8: Run DGX tests, doctor, and fake task

**Files:**
- Create: `/home/ys/works/robots/projects/triagent-dgx-smoke/`
- Create: task records under `/home/ys/works/robots/triagent-runs/`.

**Interfaces:**
- Consumes: installed environment/profile.
- Produces: Linux tests, CLI capability evidence, and no-model workflow evidence.

- [ ] **Step 1: Run all tests on DGX**

```powershell
ssh spark "cd /home/ys/works/robots/triagent && /home/ys/miniforge3/bin/conda run -n triagent python -m pytest -q"
```

Expected: zero failures; Windows/live/onsite tests may skip.

- [ ] **Step 2: Run doctor**

```powershell
ssh spark "/home/ys/miniforge3/bin/conda run -n triagent triagent doctor --profile /home/ys/works/robots/triagent/profiles/dgx.spark.toml"
```

Expected: Codex and Cursor installed/authenticated; Antigravity installed with
authentication unknown; OpenCode disabled or missing. No model smoke.

- [ ] **Step 3: Create a disposable repository**

```powershell
ssh spark "mkdir -p /home/ys/works/robots/projects/triagent-dgx-smoke && cd /home/ys/works/robots/projects/triagent-dgx-smoke && git init -q && git config user.name 'Shuo Yang' && git config user.email 'yangshuo129@gmail.com' && printf 'DGX smoke repository\n' > README.md && git add README.md && git commit -qm 'base'"
```

Expected: clean repository with one commit.

- [ ] **Step 4: Run fake workflow**

```powershell
ssh spark "/home/ys/miniforge3/bin/conda run -n triagent triagent run --profile fake --data-root /home/ys/works/robots/triagent-runs --risk low --acceptance 'controller reaches approval' --visual-check none /home/ys/works/robots/projects/triagent-dgx-smoke 'Run a no-model DGX controller smoke test'"
```

Expected: task ID, `APPROVAL`, report under `triagent-runs`, zero provider use.

- [ ] **Step 5: Verify preservation**

```powershell
ssh spark "git -C /home/ys/works/robots/triagent status --short && /home/ys/miniforge3/bin/conda env list && find /home/ys/works/robots -mindepth 1 -maxdepth 1 -printf '%f\n' | sort"
```

Expected: clean controller; original environments plus `triagent`; top-level
`projects`, `triagent`, and `triagent-runs`.

---

### Task 9: Optional bounded real smoke

**Files:** Reuse disposable repository; create one live run.

**Interfaces:**
- Consumes: all passing no-model evidence and separate live/billing confirmation.
- Produces: one approval-gated Cursor/Codex/Antigravity provenance record.

- [ ] **Step 1: Stop for confirmation**

Report remote pytest, doctor, fake task, and the estimate of three provider
calls. Continue only after explicit confirmation of this one live smoke.

- [ ] **Step 2: Confirm disposable repo is clean**

```powershell
ssh spark "git -C /home/ys/works/robots/projects/triagent-dgx-smoke status --short"
```

Expected: empty. If dirty, inspect and stop; do not reset.

- [ ] **Step 3: Run one real task**

```powershell
$liveOutput = ssh spark "/home/ys/miniforge3/bin/conda run -n triagent triagent run --profile /home/ys/works/robots/triagent/profiles/dgx.spark.toml --live-confirmed --billing-confirmed --data-root /home/ys/works/robots/triagent-runs --risk low --acceptance 'python -m pytest -q passes' --acceptance 'health_status returns ok' --forbidden '.env' --visual-check none /home/ys/works/robots/projects/triagent-dgx-smoke 'Add a Python health_status function returning ok with a focused pytest'"
$liveOutput
$taskId = [regex]::Match(($liveOutput -join [Environment]::NewLine), 'Task:\s*([0-9a-f-]{36})').Groups[1].Value
if (-not $taskId) { throw "Live task ID was not returned" }
```

Expected: Cursor through `cursor-agent`, Codex verification, Antigravity
review, `APPROVAL`, no DeepSeek, merge, or deployment.

- [ ] **Step 4: Read report and stop**

Use the exact UUID captured in `$taskId`:

```powershell
ssh spark "/home/ys/miniforge3/bin/conda run -n triagent triagent report $taskId --data-root /home/ys/works/robots/triagent-runs"
```

Do not approve, merge, deploy, delete worktrees, or prune branches.

---

### Task 10: Final handoff

**Files:** Verify only.

**Interfaces:**
- Consumes: local and remote evidence.
- Produces: an outcome-oriented Chinese report.

- [ ] **Step 1: Verify commit identity**

```powershell
Set-Location D:\workspace
git status --short
$localCommit = (git rev-parse HEAD).Trim()
$remoteCommit = (ssh spark "git -C /home/ys/works/robots/triagent rev-parse HEAD").Trim()
if ($localCommit -ne $remoteCommit) { throw "Final commit mismatch" }
```

Expected: clean local status and matching commits.

- [ ] **Step 2: Verify installation**

```powershell
ssh spark "/home/ys/miniforge3/bin/conda run -n triagent python --version && /home/ys/miniforge3/bin/conda run -n triagent triagent --help >/dev/null && git -C /home/ys/works/robots/triagent status --short"
```

Expected: Python 3.12, TriAgent success, clean remote clone.

- [ ] **Step 3: Report the exact boundary**

Report the local/remote commit, environment path and Python version, remote
pytest and doctor results, fake task ID, optional live task ID, preservation of
the original Conda environments, DeepSeek disabled status, and confirmation
that no merge, deployment, systemd, GUI, WebRTC, or robot action occurred.
List ChatGPT App remote control and Isaac Lab visual verification as pending.
