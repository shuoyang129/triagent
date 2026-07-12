# DGX onsite verification checklist

Record each gate independently on the real Windows work host and Ubuntu 24.04 DGX Spark. Onsite results cannot be inferred from simulated local tests. Never record credentials in evidence.

## 1. SSH reachability

- Command: `ssh -o BatchMode=yes OPERATOR@DGX_HOST true`
- Evidence: timestamp, source host, target hostname, exit code, and redacted transcript.
- Result: pending onsite verification.

## 2. Codex, Cursor, and Antigravity CLI login and capability

- Command: `codex --version`, `codex login status`, `cursor-agent --version`, `cursor-agent status`, and `agy --version` on the intended host. Do not run an Antigravity auth-status command: the installed CLI documents none.
- Evidence: version, installed status, authenticated/ready status, and timestamp; omit tokens and cookies. Record `Antigravity authentication: unknown` until an operator verifies login through a documented onsite method.
- Result: pending onsite verification.

## 3. systemd user service behavior

- Command: `systemctl --user status triagent-runner.service` followed by a restart/relogin persistence check.
- Evidence: active state, enablement, journal excerpt, login/logout timestamps, and recovery result.
- Result: pending onsite verification.

## 4. NVIDIA GPU, driver, and container access

- Command: `nvidia-smi` and a read-only NVIDIA container runtime GPU probe.
- Evidence: GPU model, driver/runtime versions, container exit code, and redacted output.
- Result: pending onsite verification.

## 5. Local desktop and display path

- Command: inspect the active display session and launch a harmless test window from the intended desktop session.
- Evidence: display/session type, monitor path, screenshot reference, and operator observation.
- Result: pending onsite verification.

## 6. Isaac Lab, Isaac Sim, and WebRTC visualization

- Command: launch the approved Isaac Lab/Isaac Sim smoke scene, then open its WebRTC stream from the approved client.
- Evidence: scene name, window screenshot, WebRTC client screenshot, frame/interaction observation, and timestamps.
- Result: pending onsite verification.

## 7. tmux, background execution, and disconnect recovery

- Command: start a timestamping job in `tmux`, disconnect SSH, reconnect, and attach to the same session.
- Evidence: session identifier, timestamps before/after disconnect, continuing output, and recovery transcript.
- Result: pending onsite verification.

## 8. ChatGPT mobile and Codex Remote through the Windows host

- Command: initiate an approved Codex Remote task from ChatGPT mobile through the always-on Windows host and inspect status without bypassing TriAgent.
- Evidence: Windows host identity, mobile request timestamp, task identifier, status/report excerpt, and operator approval record.
- Result: pending onsite verification.

## Milestone boundary

核心功能在 Windows 本地及模拟 DGX 环境中通过；等待局域网设备上的集成验收。
