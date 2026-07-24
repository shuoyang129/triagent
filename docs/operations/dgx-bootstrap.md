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
bash scripts/install-cursor-sandbox-apparmor.sh --check
sudo -v
bash scripts/install-cursor-sandbox-apparmor.sh --apply
```

The installer creates only the `triagent` Python 3.12 environment and the
`triagent-runs` and `projects` directories. It does not modify `base`,
`isaaclab`, `sonic-g1`, system Python, NVIDIA, Docker, or zsh.
If the environment has no `node` executable, the installer links an existing
NVM or `~/.local/opt` Node runtime into `triagent/bin` so the Codex CLI can
start; it does not download Node or activate NVM globally.

The separate AppArmor installer grants `userns` only to
`~/.local/share/cursor-agent/versions/*/cursorsandbox`. It does not disable
`kernel.apparmor_restrict_unprivileged_userns`. Configure the current Cursor
account with `approvalMode=auto-review` and `sandbox.mode=enabled`; the normal
DGX profile also passes `--auto-review --sandbox enabled` explicitly.

## Controller

```bash
CONDA=/home/ys/miniforge3/bin/conda
PROFILE=/home/ys/works/robots/triagent/profiles/dgx.spark.toml
DATA_ROOT=/home/ys/works/robots/triagent-runs
$CONDA run -n triagent triagent doctor --profile "$PROFILE"
$CONDA run -n triagent triagent status TASK_ID --data-root "$DATA_ROOT"
$CONDA run -n triagent triagent report TASK_ID --data-root "$DATA_ROOT"
```

Use only a task ID printed by TriAgent. The profile selects the repository-owned
Cursor and Antigravity adapters, which delegate to the fixed vendor binaries;
do not substitute `agent`.
The OpenCode-backed DeepSeek fallback remains disabled by default. It requires `DEEPSEEK_API_KEY`, defaults to `deepseek/deepseek-v4-pro`, and uses a restricted TriAgent OpenCode agent with shell, network, subagents, skills, external directories, `.env`, and `.git` denied. Enable it only with explicit live and billing confirmation.

`profiles/dgx.spark.synthetic-force.toml` is an explicit exception for
strictly isolated synthetic repositories. It passes `--force`, which overrides
Cursor repository safety policy, and its adapter refuses execution unless the
worktree is below `/home/ys/works/robots/triagent-synthetic-runs/runs` and every
task scope path resolves below `/home/ys/works/robots/synthetic-projects`.
Never make this profile the default and never use it for a robot repository.

Isaac GUI, WebRTC, systemd persistence, ChatGPT App remote control, and robot
hardware remain separate onsite gates.
