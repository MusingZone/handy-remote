<#
.SYNOPSIS
  Registers caret_helper.exe so that Handy can place its overlay under the text caret while you dictate
  inside a Remote Desktop window connected to this machine.
.DESCRIPTION
  The helper runs in the dictating user's interactive (console or Remote Desktop) session: at logon and
  whenever a session connects or unlocks. It exits at once when one is already listening, so repeated
  triggers are harmless. It runs elevated (RunLevel Highest) so it can also read the caret of elevated apps
  such as an Administrator terminal; Windows blocks accessibility queries from a lower integrity level.
  Run from an elevated PowerShell as the user who dictates. Build first with build.cmd.
#>
param(
    [int]$Port = 47823,
    [string]$TaskName = 'HandyRemote-CaretHelper',
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'caret_helper.exe'
Get-Process caret_helper -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } | ForEach-Object { Stop-Process -Id $_.Id }
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed task $TaskName"
    return
}
if (-not (Test-Path $exe)) { throw "missing $exe (run build.cmd first)" }
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $exe -Argument "--port $Port" -WorkingDirectory $PSScriptRoot
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$cls = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger
# 1 = console connect, 3 = remote connect, 8 = session unlock
$session = foreach ($change in 1, 3, 8) {
    New-CimInstance -CimClass $cls -ClientOnly -Property @{ StateChange = $change; UserId = $user; Enabled = $true }
}
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger (@($logon) + $session) `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$p = Get-Process caret_helper -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe }
if ($p) { "caret_helper running on 127.0.0.1:$Port (pid $($p.Id -join ','))" } else { 'caret_helper NOT running (no interactive session?)' }
