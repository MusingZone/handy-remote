<#
.SYNOPSIS
  Keeps the recognition server running: restarts it after a crash (with back-off) and exits at once
  if another supervisor for the same port is already running. Started by the scheduled task.
#>
param(
    [string]$HomeDir = $(if ($env:HANDY_REMOTE_HOME) { $env:HANDY_REMOTE_HOME } else { Join-Path $env:LOCALAPPDATA 'handy-remote-server' }),
    [string]$Python = ''
)
$ErrorActionPreference = 'Continue'
$serverDir = Split-Path $PSScriptRoot -Parent
if (-not $Python) { $Python = Join-Path $HomeDir '.venv\Scripts\python.exe' }
$logs = Join-Path $HomeDir 'logs'
New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs 'supervisor.log'
function Say($m) { Add-Content -Path $log -Value ("{0:s} {1}" -f (Get-Date), $m) -Encoding utf8 }

$mutex = New-Object System.Threading.Mutex($false, 'Global\HandyRemote-Server-Supervisor')
if (-not $mutex.WaitOne(0)) { exit 0 }
Say "supervisor start pid=$PID home=$HomeDir"
$fails = 0
while ($true) {
    $t = Get-Date
    $p = Start-Process -FilePath $Python -ArgumentList @("`"$serverDir\asr_server.py`"", '--home', "`"$HomeDir`"") `
        -WorkingDirectory $serverDir -WindowStyle Hidden -PassThru `
        -RedirectStandardError (Join-Path $logs 'server.stderr.log') -RedirectStandardOutput (Join-Path $logs 'server.stdout.log')
    Say "server started pid=$($p.Id)"
    $p.WaitForExit()
    $up = ((Get-Date) - $t).TotalSeconds
    Say ("server exited code={0} after {1:N0}s" -f $p.ExitCode, $up)
    if ($up -lt 60) { $fails++ } else { $fails = 0 }
    Start-Sleep -Seconds ([Math]::Min(300, 5 * [Math]::Pow(2, [Math]::Min($fails, 6))))
}
