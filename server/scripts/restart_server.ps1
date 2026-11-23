<#
.SYNOPSIS
  Restarts the recognition server (the supervisor relaunches it within seconds) and waits until the new
  engine reports ready. Use after changing config.toml; lexicon.json changes are hot-reloaded.
#>
param(
    [string]$HomeDir = $(if ($env:HANDY_REMOTE_HOME) { $env:HANDY_REMOTE_HOME } else { Join-Path $env:LOCALAPPDATA 'handy-remote-server' }),
    [int]$TimeoutSec = 120
)
$serverDir = Split-Path $PSScriptRoot -Parent
$script = Join-Path $serverDir 'asr_server.py'
$since = Get-Date
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*$script*" })
if (-not $procs) { Write-Host "no running server found for $script" }
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force; Write-Host "stopped $($p.ProcessId)" }
$log = Join-Path $HomeDir 'logs\server.log'
$deadline = $since.AddSeconds($TimeoutSec)
do {
    Start-Sleep 2
    $ready = Select-String -Path $log -Pattern 'engine ready' -ErrorAction SilentlyContinue | Select-Object -Last 1
    $fresh = $ready -and [datetime]::ParseExact($ready.Line.Substring(0, 19), 'yyyy-MM-dd HH:mm:ss', $null) -ge $since.AddSeconds(-1)
} until ($fresh -or (Get-Date) -gt $deadline)
if ($fresh) { $ready.Line } else { Write-Host "NOT READY after $TimeoutSec s; check $log"; exit 1 }
