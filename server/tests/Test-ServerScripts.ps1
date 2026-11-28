$ErrorActionPreference = 'Stop'

$serverDir = Split-Path $PSScriptRoot -Parent
$scriptsDir = Join-Path $serverDir 'scripts'
$runScript = Join-Path $scriptsDir 'run_server.ps1'
$restartScript = Join-Path $scriptsDir 'restart_server.ps1'

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Parse-ScriptAst([string]$Path) {
    $tokens = $null
    $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    if ($errors) { throw ("Parse errors in ${Path}:`n" + ($errors | Out-String)) }
    return $ast
}

# 1. PowerShell AST parse check on both public server scripts.
$runAst = Parse-ScriptAst $runScript
$restartAst = Parse-ScriptAst $restartScript

# 2. Static checks on run_server.ps1 (PS 5.1 ExitCode + AbandonedMutexException + cleanup + safe launch).
$runText = $runAst.Extent.Text
Assert-True ($runText -match 'AbandonedMutexException') 'run_server.ps1 must handle AbandonedMutexException'
Assert-True ($runText -match 'ReleaseMutex\(\)') 'run_server.ps1 must release the supervisor mutex in finally'
Assert-True ($runText -match 'Dispose\(\)') 'run_server.ps1 must dispose the supervisor mutex'
Assert-True ($runText -match '\.Handle') 'run_server.ps1 must touch Process.Handle before WaitForExit for PS 5.1 ExitCode'
$handleIdx = $runText.IndexOf('.Handle')
$waitIdx = $runText.IndexOf('.WaitForExit()')
Assert-True ($handleIdx -ge 0 -and $waitIdx -gt $handleIdx) 'run_server.ps1 must touch .Handle before .WaitForExit()'
Assert-True ($runText -match 'try\s*\{[\s\S]*Start-Process[\s\S]*\}\s*catch\s*\{') 'run_server.ps1 must wrap Start-Process in try/catch'

# 3. Dot-source restart_server.ps1 safely (must not execute main body when dot-sourced).
Assert-True ($restartAst.Extent.Text -match "MyInvocation\.InvocationName\s+-ne\s+'\.'") 'restart_server.ps1 must guard main body for dot-sourcing'
. $restartScript

$fakeServerDir = 'C:\handy-remote\server'
$fakeHomeDir = 'C:\handy-remote\home'
$fakeServerPy = Join-Path $fakeServerDir 'asr_server.py'
$fakeSupervisorPs1 = Join-Path $fakeServerDir 'scripts\run_server.ps1'
$fakeVenvPy = Join-Path $fakeHomeDir '.venv\Scripts\python.exe'
$fakeBasePy = 'C:\Python312\python.exe'
$fakePsExe = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

# 4. Exact process matching and lineage ordering (no broad wildcard matching).
$procs = @(
    # Caller ancestor chain (must be excluded even if a python process is in it).
    [pscustomobject]@{ ProcessId = 100; ParentProcessId = 1; Name = 'python.exe'; ExecutablePath = $fakeVenvPy; CommandLine = "`"$fakeVenvPy`" `"$fakeServerPy`" --home `"$fakeHomeDir`"" }
    [pscustomobject]@{ ProcessId = 101; ParentProcessId = 100; Name = 'powershell.exe'; ExecutablePath = $fakePsExe; CommandLine = "`"$fakePsExe`" -File restart_server.ps1" }
    # Real supervisor + venv launcher + base worker for this server & home.
    [pscustomobject]@{ ProcessId = 200; ParentProcessId = 1; Name = 'powershell.exe'; ExecutablePath = $fakePsExe; CommandLine = "`"$fakePsExe`" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File `"$fakeSupervisorPs1`" -HomeDir `"$fakeHomeDir`"" }
    [pscustomobject]@{ ProcessId = 201; ParentProcessId = 200; Name = 'python.exe'; ExecutablePath = $fakeVenvPy; CommandLine = "`"$fakeVenvPy`" `"$fakeServerPy`" --home `"$fakeHomeDir`"" }
    [pscustomobject]@{ ProcessId = 202; ParentProcessId = 201; Name = 'python.exe'; ExecutablePath = $fakeBasePy; CommandLine = "`"$fakeBasePy`" `"$fakeServerPy`" --home `"$fakeHomeDir`"" }
    # Decoys that broad `-like "*$script*"` would wrongly kill:
    [pscustomobject]@{ ProcessId = 301; ParentProcessId = 1; Name = 'python.exe'; ExecutablePath = $fakeBasePy; CommandLine = "`"$fakeBasePy`" -c `"print('$fakeServerPy')`"" }
    [pscustomobject]@{ ProcessId = 302; ParentProcessId = 1; Name = 'python.exe'; ExecutablePath = $fakeBasePy; CommandLine = "`"$fakeBasePy`" -m unittest `"$fakeServerPy`"" }
    [pscustomobject]@{ ProcessId = 303; ParentProcessId = 1; Name = 'python.exe'; ExecutablePath = $fakeBasePy; CommandLine = "`"$fakeBasePy`" `"$fakeServerPy`" --home `"C:\Temp\isolated-test-home`"" }
    [pscustomobject]@{ ProcessId = 304; ParentProcessId = 1; Name = 'python.exe'; ExecutablePath = 'C:\other\.venv\Scripts\python.exe'; CommandLine = "`"C:\other\.venv\Scripts\python.exe`" `"C:\other\asr_server.py`"" }
    [pscustomobject]@{ ProcessId = 305; ParentProcessId = 1; Name = 'notepad.exe'; ExecutablePath = 'C:\Windows\System32\notepad.exe'; CommandLine = "notepad.exe `"$fakeServerPy`"" }
    [pscustomobject]@{ ProcessId = 306; ParentProcessId = 1; Name = 'powershell.exe'; ExecutablePath = $fakePsExe; CommandLine = "`"$fakePsExe`" -Command `"& '$fakeSupervisorPs1'`"" }
    [pscustomobject]@{ ProcessId = 307; ParentProcessId = 1; Name = 'powershell.exe'; ExecutablePath = $fakePsExe; CommandLine = "`"$fakePsExe`" -File `"$fakeSupervisorPs1`" -HomeDir `"C:\Temp\other-home`"" }
    [pscustomobject]@{ ProcessId = 308; ParentProcessId = 1; Name = 'python.exe'; ExecutablePath = 'C:\unrelated\python.exe'; CommandLine = "`"C:\unrelated\python.exe`" `"$fakeServerPy`"" }
)

$found = Find-ServerProcesses -Processes $procs -SelfPid 101 `
    -ServerDir $fakeServerDir -HomeDir $fakeHomeDir `
    -ExpectedPowerShell $fakePsExe -VenvPythons @($fakeVenvPy) -BasePythons @($fakeBasePy)

Assert-True ($found.Supervisors.Count -eq 1 -and $found.Supervisors[0].ProcessId -eq 200) "Expected only supervisor PID 200, got: $(($found.Supervisors | ForEach-Object ProcessId) -join ',')"
$targetPids = @($found.ServerTargets | ForEach-Object { $_.ProcessId })
Assert-True ($targetPids.Count -eq 2) "Expected exactly 2 server targets (worker + launcher), got: $($targetPids -join ',')"
Assert-True ($targetPids[0] -eq 202 -and $targetPids[1] -eq 201) "Expected worker (202) stopped before launcher (201), got: $($targetPids -join ',')"

# Standalone venv python started with HANDY_REMOTE_HOME (no --home flag) is matched only when exe belongs to HomeDir's venv.
$envRunProcs = @(
    [pscustomobject]@{ ProcessId = 401; ParentProcessId = 1; Name = 'python.exe'; ExecutablePath = $fakeVenvPy; CommandLine = "`"$fakeVenvPy`" `"$fakeServerPy`"" }
    [pscustomobject]@{ ProcessId = 402; ParentProcessId = 401; Name = 'python.exe'; ExecutablePath = $fakeBasePy; CommandLine = "`"$fakeBasePy`" `"$fakeServerPy`"" }
)
$foundEnv = Find-ServerProcesses -Processes $envRunProcs -SelfPid 999 `
    -ServerDir $fakeServerDir -HomeDir $fakeHomeDir `
    -ExpectedPowerShell $fakePsExe -VenvPythons @($fakeVenvPy) -BasePythons @($fakeBasePy)
$envPids = @($foundEnv.ServerTargets | ForEach-Object { $_.ProcessId })
Assert-True ($envPids.Count -eq 2 -and $envPids[0] -eq 402 -and $envPids[1] -eq 401) "Expected env-home targets [402, 401], got: $($envPids -join ',')"

# Paths containing spaces are parsed cleanly by Split-CommandLineArgs.
$spacedServerDir = 'C:\Program Data\Handy Remote\server'
$spacedHomeDir = 'C:\Program Data\Handy Remote\home'
$spacedPy = Join-Path $spacedHomeDir '.venv\Scripts\python.exe'
$spacedScript = Join-Path $spacedServerDir 'asr_server.py'
$spacedProcs = @(
    [pscustomobject]@{ ProcessId = 501; ParentProcessId = 1; Name = 'python.exe'; ExecutablePath = $spacedPy; CommandLine = "`"$spacedPy`" `"$spacedScript`" --home `"$spacedHomeDir`"" }
)
$foundSpaced = Find-ServerProcesses -Processes $spacedProcs -SelfPid 999 `
    -ServerDir $spacedServerDir -HomeDir $spacedHomeDir `
    -ExpectedPowerShell $fakePsExe -VenvPythons @($spacedPy) -BasePythons @()
Assert-True ($foundSpaced.ServerTargets.Count -eq 1 -and $foundSpaced.ServerTargets[0].ProcessId -eq 501) 'Expected spaced path process 501 to match'

# 5. Log readiness matching: missing, malformed, stale, fresh engine ready, and fresh offline window.
$tmpDir = Join-Path ([IO.Path]::GetTempPath()) ("hr-server-script-test-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
try {
    $logPath = Join-Path $tmpDir 'server.log'
    $since = [datetime]::new(2026, 11, 29, 2, 0, 0)

    # Missing log file must return $null without throwing.
    Assert-True ($null -eq (Get-ReadyServerLogLine -LogPath $logPath -Since $since)) 'Missing log should return $null'

    # Short / malformed lines must return $null without throwing.
    Set-Content -LiteralPath $logPath -Encoding UTF8 -Value @(
        'short'
        'not-a-timestamp INFO engine ready in 12.0s'
        '2026-11-29 01:59:50 INFO engine ready in 12.0s'
        '2026-11-29 01:59:55 INFO offline window: models load at 09:00 or on the first request'
    )
    Assert-True ($null -eq (Get-ReadyServerLogLine -LogPath $logPath -Since $since)) 'Stale or malformed log lines must not match'

    # Fresh offline window startup line must match (prevents updater timeout/rollback during offline window).
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value '2026-11-29 02:00:02 INFO offline window: models load at 09:00 or on the first request'
    $offlineLine = Get-ReadyServerLogLine -LogPath $logPath -Since $since
    Assert-True ($offlineLine -like '*offline window: models load*') "Expected fresh offline-window line, got: $offlineLine"

    # Fresh engine ready line after a newer restart must also match.
    $since2 = [datetime]::new(2026, 11, 29, 10, 0, 0)
    Assert-True ($null -eq (Get-ReadyServerLogLine -LogPath $logPath -Since $since2)) 'Earlier offline-window line must be stale for later restart'
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value '2026-11-29 10:00:15 INFO engine ready in 14.2s (load 10.1s; qwen device=CPU, npu previews=False, punct=True)'
    $readyLine = Get-ReadyServerLogLine -LogPath $logPath -Since $since2
    Assert-True ($readyLine -like '*engine ready*') "Expected fresh engine-ready line, got: $readyLine"
} finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output 'PASS: public server script AST, process-matching, and offline-window readiness tests.'
