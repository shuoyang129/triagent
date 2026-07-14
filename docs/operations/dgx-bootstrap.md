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
