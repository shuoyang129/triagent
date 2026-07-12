param(
    [string]$Output = "work/capabilities/windows.json"
)

$ErrorActionPreference = "Stop"

function Invoke-Probe {
    param([scriptblock]$Command)
    try {
        $text = (& $Command 2>$null | Out-String).Trim()
        $code = $LASTEXITCODE
        return @{ ok = ($null -eq $code -or $code -eq 0); text = $(if ($null -eq $code -or $code -eq 0) { $text } else { "" }) }
    } catch {
        return @{ ok = $false; text = "" }
    }
}

function New-Capability {
    param(
        [bool]$Installed,
        [AllowNull()][string]$Version,
        [bool]$Authenticated,
        [bool]$Headless
    )
    [ordered]@{
        installed = $Installed
        version = $Version
        authenticated = $Authenticated
        headless = $Headless
    }
}

$pythonPath = Join-Path $env:LOCALAPPDATA "Programs/Python/Python312/python.exe"
$python = Invoke-Probe { & $pythonPath --version }
$git = Invoke-Probe { git --version }
$codex = Invoke-Probe { codex --version }
$codexAuth = Invoke-Probe { codex login status }
$cursor = Invoke-Probe { wsl -d Ubuntu-24.04 -- bash -lc '~/.local/bin/cursor-agent --version' }
$cursorAuth = Invoke-Probe { wsl -d Ubuntu-24.04 -- bash -lc '~/.local/bin/cursor-agent status' }
$agyPath = Join-Path $env:LOCALAPPDATA "agy/bin/agy.exe"
$antigravity = Invoke-Probe { & $agyPath --version }
$antigravityConfig = Test-Path (Join-Path $env:USERPROFILE ".gemini/antigravity-cli/settings.json")
$opencodeCommand = Get-Command opencode -ErrorAction SilentlyContinue
$opencode = if ($opencodeCommand) { Invoke-Probe { opencode --version } } else { @{ ok = $false; text = "" } }

$payload = [ordered]@{
    generated_at = [DateTimeOffset]::UtcNow.ToString("o")
    host = $env:COMPUTERNAME
    tools = [ordered]@{
        python = New-Capability $python.ok $python.text $false $true
        git = New-Capability $git.ok $git.text $false $true
        codex = New-Capability $codex.ok $codex.text $codexAuth.ok $true
        cursor = New-Capability $cursor.ok $cursor.text ($cursorAuth.ok -and $cursorAuth.text -match "Logged in") $true
        antigravity = New-Capability $antigravity.ok $antigravity.text ($antigravityConfig -and $antigravity.ok) $true
        opencode = New-Capability $opencode.ok $(if ($opencode.ok) { $opencode.text } else { $null }) $false $true
    }
}

$outputPath = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Output))
$outputDir = Split-Path -Parent $outputPath
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
$json = $payload | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($outputPath, $json, [System.Text.UTF8Encoding]::new($false))
Write-Output $outputPath

if (-not ($python.ok -and $git.ok -and $codex.ok -and $cursor.ok -and $antigravity.ok)) {
    exit 2
}
