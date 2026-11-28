<#
.SYNOPSIS
  Registers (or removes) the per-user task "HandyRemote-AutoUpdate" that keeps Handy Remote up to date.
.DESCRIPTION
  Copies Update-HandyRemote.ps1 and Minisign.cs to %LOCALAPPDATA%\handy-remote\updater\ and runs them hidden
  at logon and every 6 hours. Normal tasks need no administrator rights; -RunElevated requires them.
  An existing task's run level is preserved. Extra parameters (for example -ServerDir to also
  update a recognition server on this PC, or -NotifyScript) are passed through to Update-HandyRemote.ps1.
.EXAMPLE
  powershell -ExecutionPolicy RemoteSigned -File Install-AutoUpdate.ps1 -RunNow
.EXAMPLE
  powershell -ExecutionPolicy RemoteSigned -File Install-AutoUpdate.ps1 -Uninstall
#>
param(
    [string]$TaskName = 'HandyRemote-AutoUpdate',
    [int]$EveryHours = 6,
    [string]$Repo,
    [string]$ServerDir,
    [string]$ServerHome,
    [int]$ServerPort,
    [string]$NotifyScript,
    [switch]$RunElevated,
    [switch]$RunNow,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
function Resolve-UpdateRunLevel([bool]$elevated, [string]$existingLevel) {
    if ($elevated -or $existingLevel -eq 'Highest') { return 'Highest' }
    return 'Limited'
}

$dest = Join-Path $env:LOCALAPPDATA 'handy-remote\updater'
if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Write-Host "removed task $TaskName (log, downloads and backups stay in $dest)"
    return
}
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$existingLevel = if ($existingTask) { [string]$existingTask.Principal.RunLevel } else { '' }
$runLevel = Resolve-UpdateRunLevel $RunElevated.IsPresent $existingLevel
if ($runLevel -eq 'Highest') {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try { $admin = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
    finally { $identity.Dispose() }
    if (-not $admin) { throw 'This task runs elevated. Re-run this installer as administrator; its run level will not be downgraded.' }
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null
foreach ($f in 'Update-HandyRemote.ps1', 'Minisign.cs') { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $f) -Destination $dest -Force }

$extra = " -TaskName `"$TaskName`""
if ($Repo) { $extra += " -Repo `"$Repo`"" }
if ($ServerDir) { $extra += " -ServerDir `"$ServerDir`"" }
if ($ServerHome) { $extra += " -ServerHome `"$ServerHome`"" }
if ($ServerPort) { $extra += " -ServerPort $ServerPort" }
if ($NotifyScript) { $extra += " -NotifyScript `"$NotifyScript`"" }
$script = Join-Path $dest 'Update-HandyRemote.ps1'
$taskArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File `"$script`"$extra"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArgs -WorkingDirectory $dest
$user = "$env:USERDOMAIN\$env:USERNAME"
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $user),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(1) -RepetitionInterval (New-TimeSpan -Hours $EveryHours))
)
$triggers[0].Delay = 'PT5M'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel $runLevel
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "task $TaskName registered (at logon + every $EveryHours h); log: $dest\update.log"
if ($RunNow) { Start-ScheduledTask -TaskName $TaskName; Write-Host 'started a run now' }
