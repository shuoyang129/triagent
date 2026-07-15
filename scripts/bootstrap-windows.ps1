param(
    [string]$Output = "work/capabilities/windows.json"
)

$ErrorActionPreference = "Stop"

function Invoke-Probe {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Arguments = @()
    )
    if (-not (Get-Command $Executable -ErrorAction SilentlyContinue)) {
        return @{ ok = $false; text = "" }
    }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $Executable @Arguments 2>$null
        $code = $LASTEXITCODE
        $text = ($output | Out-String).Trim()
        return @{ ok = ($code -eq 0); text = $(if ($code -eq 0) { $text } else { "" }) }
    } catch {
        return @{ ok = $false; text = "" }
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
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
$python = Invoke-Probe -Executable $pythonPath -Arguments @("--version")
$git = Invoke-Probe -Executable "git" -Arguments @("--version")
$codexCommand = Get-Command codex.cmd -All -ErrorAction SilentlyContinue |
    Where-Object { $_.Source -notlike "*WindowsApps*" } |
    Select-Object -First 1
$codexExecutable = if ($codexCommand) { $codexCommand.Source } else { "codex" }
$codex = Invoke-Probe -Executable $codexExecutable -Arguments @("--version")
$codexAuth = Invoke-Probe -Executable $codexExecutable -Arguments @("login", "status")
$cursor = Invoke-Probe -Executable "wsl.exe" -Arguments @("-d", "Ubuntu-24.04", "--", "bash", "-lc", "~/.local/bin/cursor-agent --version")
$cursorAuth = Invoke-Probe -Executable "wsl.exe" -Arguments @("-d", "Ubuntu-24.04", "--", "bash", "-lc", "~/.local/bin/cursor-agent status")
$agyPath = Join-Path $env:LOCALAPPDATA "agy/bin/agy.exe"
$antigravity = Invoke-Probe -Executable $agyPath -Arguments @("--version")
$antigravityConfig = Test-Path (Join-Path $env:USERPROFILE ".gemini/antigravity-cli/settings.json")
$opencodeCommand = Get-Command opencode -ErrorAction SilentlyContinue
$opencode = if ($opencodeCommand) { Invoke-Probe -Executable $opencodeCommand.Source -Arguments @("--version") } else { @{ ok = $false; text = "" } }

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

$outputPath = [System.IO.Path]::GetFullPath($Output)
$outputDir = Split-Path -Parent $outputPath
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
$json = $payload | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($outputPath, $json, [System.Text.UTF8Encoding]::new($false))
Write-Output $outputPath

if (-not ($python.ok -and $git.ok -and $codex.ok -and $cursor.ok -and $antigravity.ok)) {
    exit 2
}
