<#
.SYNOPSIS
  Registers (or removes) the scheduled task that runs the recognition server in the background.
.DESCRIPTION
  Default: runs at logon of the current user (no administrator rights needed).
  -AtStartup: runs at boot without anyone logged on ("Run whether user is logged on or not", S4U);
  needs an elevated PowerShell. A 5-minute repetition restarts the supervisor if it ever stopped;
  a second copy exits immediately.
#>
param(
    [string]$HomeDir = $(if ($env:HANDY_REMOTE_HOME) { $env:HANDY_REMOTE_HOME } else { Join-Path $env:LOCALAPPDATA 'handy-remote-server' }),
    [string]$TaskName = 'HandyRemote-Server',
    [switch]$AtStartup,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed task $TaskName (a running server keeps running until restart)"
    return
}
$run = Join-Path $PSScriptRoot 'run_server.ps1'
$taskArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File `"$run`" -HomeDir `"$HomeDir`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArgs -WorkingDirectory (Split-Path $PSScriptRoot -Parent)
$user = "$env:USERDOMAIN\$env:USERNAME"
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
if ($AtStartup) {
    $triggers = @((New-ScheduledTaskTrigger -AtStartup), $watchdog)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
} else {
    $triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $user), $watchdog)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "task $TaskName registered and started; watch $HomeDir\logs\server.log for 'engine ready'"
