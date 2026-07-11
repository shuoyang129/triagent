# Windows bootstrap

## Installed tool layout

- Python 3.12: `%LOCALAPPDATA%/Programs/Python/Python312/python.exe`
- Codex CLI: `codex`
- Cursor CLI: Ubuntu 24.04 WSL, `~/.local/bin/cursor-agent`
- Antigravity CLI: `%LOCALAPPDATA%/agy/bin/agy.exe`
- OpenCode: optional and disabled by default

## Authentication

Run authentication interactively. Never paste credentials into a task log.

```powershell
codex
wsl -d Ubuntu-24.04 -- ~/.local/bin/cursor-agent login
agy
```

Exit the interactive tools after login. Cursor authentication can be checked with:

```powershell
wsl -d Ubuntu-24.04 -- ~/.local/bin/cursor-agent status
```

## Generate the capability record

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap-windows.ps1 -Output work/capabilities/windows.json
```

The output contains booleans and version strings only. It must never contain access tokens, cookies, API keys, or private-key material.

## Known first-phase boundary

Passing this bootstrap proves only that the travel Windows host can run the local tools. It does not prove DGX, Isaac, LAN SSH, ChatGPT Remote, or Tencent Cloud relay integration.
