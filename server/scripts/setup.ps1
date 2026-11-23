<#
.SYNOPSIS
  One-time setup of the Handy Remote recognition server on this Windows machine.
.DESCRIPTION
  1. creates a Python virtual environment in <home>\.venv and installs requirements.txt
  2. downloads and verifies the models (scripts\download_models.ps1)
  3. checks the machine and writes <home>\config\config.toml (doctor.py --write)
  4. with -InstallTask: registers the background task (scripts\install_task.ps1)
  Re-running is safe: finished steps are skipped or verified.
.EXAMPLE
  powershell -ExecutionPolicy RemoteSigned -File server\scripts\setup.ps1 -InstallTask
#>
param(
    [string]$HomeDir = $(if ($env:HANDY_REMOTE_HOME) { $env:HANDY_REMOTE_HOME } else { Join-Path $env:LOCALAPPDATA 'handy-remote-server' }),
    [ValidateSet('hf', 'hf-mirror')][string]$Source = 'hf',
    [switch]$SkipNpu,
    [switch]$InstallTask,
    [switch]$AtStartup
)
$ErrorActionPreference = 'Stop'
$serverDir = Split-Path $PSScriptRoot -Parent
New-Item -ItemType Directory -Force $HomeDir | Out-Null
$venv = Join-Path $HomeDir '.venv'
$py = Join-Path $venv 'Scripts\python.exe'

Write-Host '== 1/4 Python environment'
if (-not (Test-Path $py)) {
    $made = $false
    foreach ($v in '3.13', '3.12') {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py "-$v" -m venv $venv 2>$null
            if ($LASTEXITCODE -eq 0) { $made = $true; break }
        }
    }
    if (-not $made) { throw 'Python 3.12 or 3.13 is required (https://www.python.org/downloads/ with the "py" launcher)' }
}
& $py -m pip install --disable-pip-version-check -q --upgrade pip
& $py -m pip install --disable-pip-version-check -q -r (Join-Path $serverDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

Write-Host '== 2/4 Models'
& (Join-Path $PSScriptRoot 'download_models.ps1') -HomeDir $HomeDir -Source $Source -SkipNpu:$SkipNpu
if ($LASTEXITCODE -ne 0) { throw 'model download failed; re-run setup to resume' }

Write-Host '== 3/4 Machine check'
& $py (Join-Path $serverDir 'doctor.py') --home $HomeDir --write

if ($InstallTask) {
    Write-Host '== 4/4 Background task'
    & (Join-Path $PSScriptRoot 'install_task.ps1') -HomeDir $HomeDir -AtStartup:$AtStartup
} else {
    Write-Host "== 4/4 skipped. Start the server with: $PSScriptRoot\install_task.ps1  (or run_server.ps1 in a console)"
}
Write-Host "`nNext: on this machine run '$py $serverDir\doctor.py pair' and paste the code into Handy."
