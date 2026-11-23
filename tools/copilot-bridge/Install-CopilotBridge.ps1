<#
.SYNOPSIS
  Installs (or removes) the Copilot bridge for the current user: copies it to
  %LOCALAPPDATA%\copilot-bridge\app, installs its one dependency, and starts it hidden at logon.

.EXAMPLE
  powershell -ExecutionPolicy RemoteSigned -File Install-CopilotBridge.ps1
  powershell -ExecutionPolicy RemoteSigned -File Install-CopilotBridge.ps1 -Uninstall
#>
param([switch]$Uninstall, [ValidateRange(1, 65535)][int]$Port = 47870)
$ErrorActionPreference = 'Stop'
$task = 'CopilotBridge'
$home_ = Join-Path $env:LOCALAPPDATA 'copilot-bridge'
$app = Join-Path $home_ 'app'

function Stop-Bridge {
    Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
        Where-Object { $_.CommandLine -like "*$app*bridge.mjs*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Stop-Bridge
    if (Test-Path $app) { Remove-Item $app -Recurse -Force }
    Write-Host "Removed. Logs and runtime data remain in $home_ (delete it if you like)."
    return
}

$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $node) { throw 'Node.js 22.12 or newer is required: https://nodejs.org' }
$ver = [version]((& $node --version).TrimStart('v'))
if ($ver -lt [version]'22.12') { throw "Node.js $ver is too old; 22.12 or newer is required." }

Stop-Bridge
New-Item -ItemType Directory -Force $app | Out-Null
foreach ($f in 'bridge.mjs', 'package.json', 'package-lock.json', 'launch.vbs') {
    $src = Join-Path $PSScriptRoot $f
    if (Test-Path $src) { Copy-Item $src $app -Force }
}
[IO.File]::WriteAllText((Join-Path $app 'node-path.txt'), $node)
[IO.File]::WriteAllText((Join-Path $app 'port.txt'), [string]$Port)

$npm = Join-Path (Split-Path $node) 'npm.cmd'
if (-not (Test-Path $npm)) { $npm = 'npm.cmd' }
Push-Location $app
try {
    $cmd = if (Test-Path 'package-lock.json') { 'ci' } else { 'install' }
    & $npm $cmd --omit=dev --no-audit --no-fund --loglevel=error
    if ($LASTEXITCODE) { throw "npm $cmd failed" }
} finally { Pop-Location }

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$app\launch.vbs`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings `
    -Description 'GitHub Copilot models as a local OpenAI-compatible API (127.0.0.1)' -Force | Out-Null
Start-ScheduledTask -TaskName $task

$ok = $false
for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
    Start-Sleep 1
    try {
        $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 20
        $ok = $h.ok
    } catch { }
}
if ($ok) {
    Write-Host "Copilot bridge is running: http://127.0.0.1:$Port/v1 ($($h.models) models)."
    Write-Host "In Handy Remote: Settings > Refine before paste > Detect, or add a custom provider with that URL."
} else {
    Write-Warning "Installed, but the bridge did not answer yet. Sign in once with 'copilot' (GitHub Copilot CLI) or 'gh auth login', then check $home_\bridge.log."
}
