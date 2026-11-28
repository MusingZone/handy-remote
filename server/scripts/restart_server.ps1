<#
.SYNOPSIS
  Restarts the recognition server (the supervisor relaunches it within seconds) and waits until the new
  server reports ready (or enters a configured offline window). Use after changing config.toml;
  lexicon.json changes are hot-reloaded.
#>
param(
    [string]$HomeDir = $(if ($env:HANDY_REMOTE_HOME) { $env:HANDY_REMOTE_HOME } else { Join-Path $env:LOCALAPPDATA 'handy-remote-server' }),
    [int]$TimeoutSec = 120,
    [string]$TaskName = 'HandyRemote-Server'
)

function Resolve-NormalizedPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    $trimmed = $Path.Trim()
    if (-not [System.IO.Path]::IsPathRooted($trimmed)) { return $null }
    try {
        return [System.IO.Path]::GetFullPath($trimmed).TrimEnd('\')
    } catch {
        return $null
    }
}

function Test-PathInList {
    param(
        [string]$Path,
        [string[]]$List
    )
    if (-not $Path -or -not $List) { return $false }
    foreach ($item in $List) {
        if ($item -and [string]::Equals($Path, $item, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Split-CommandLineArgs {
    param([string]$CommandLine)
    $tokens = New-Object System.Collections.Generic.List[string]
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return @() }
    $sb = New-Object System.Text.StringBuilder
    $inQuotes = $false
    $hasToken = $false
    for ($i = 0; $i -lt $CommandLine.Length; $i++) {
        $ch = $CommandLine[$i]
        if ($ch -eq '"') {
            $inQuotes = -not $inQuotes
            $hasToken = $true
        } elseif (-not $inQuotes -and [char]::IsWhiteSpace($ch)) {
            if ($hasToken) {
                $tokens.Add($sb.ToString())
                $null = $sb.Clear()
                $hasToken = $false
            }
        } else {
            $null = $sb.Append($ch)
            $hasToken = $true
        }
    }
    if ($hasToken) { $tokens.Add($sb.ToString()) }
    return $tokens.ToArray()
}

function Get-ServerPythonPaths {
    param([string]$HomeDir)
    $venvPaths = New-Object System.Collections.Generic.List[string]
    $basePaths = New-Object System.Collections.Generic.List[string]
    $venvDir = Join-Path $HomeDir '.venv'
    $venvPy = Resolve-NormalizedPath (Join-Path $venvDir 'Scripts\python.exe')
    if ($venvPy) { $venvPaths.Add($venvPy) }
    $cfg = Join-Path $venvDir 'pyvenv.cfg'
    if (Test-Path -LiteralPath $cfg) {
        foreach ($line in (Get-Content -LiteralPath $cfg -ErrorAction SilentlyContinue)) {
            if ($line -match '^\s*home\s*=\s*(.+?)\s*$') {
                $basePy = Resolve-NormalizedPath (Join-Path $Matches[1] 'python.exe')
                if ($basePy -and -not (Test-PathInList -Path $basePy -List $basePaths.ToArray())) {
                    $basePaths.Add($basePy)
                }
            }
        }
    }
    [pscustomobject]@{
        Venv = @($venvPaths.ToArray())
        Base = @($basePaths.ToArray())
    }
}

function Test-ServerSupervisorProcess {
    param(
        $Process,
        [string]$ExpectedPowerShell,
        [string]$ExpectedScript,
        [string]$ExpectedHome
    )
    $exe = Resolve-NormalizedPath $Process.ExecutablePath
    if (-not $exe -or -not [string]::Equals($exe, $ExpectedPowerShell, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    $tokens = @(Split-CommandLineArgs $Process.CommandLine)
    if ($tokens.Count -lt 3) { return $false }
    $noArgFlags = @('-noprofile', '-nop', '-noninteractive', '-noni', '-nologo', '-nol', '-noexit', '-noe', '-sta', '-mta')
    $oneArgFlags = @('-windowstyle', '-w', '-executionpolicy', '-ep')
    $i = 1
    while ($i -lt $tokens.Count) {
        $tok = $tokens[$i].ToLowerInvariant()
        if ($tok -eq '-file' -or $tok -eq '-f') {
            if ($i + 1 -ge $tokens.Count) { return $false }
            $scriptPath = Resolve-NormalizedPath $tokens[$i + 1]
            if (-not $scriptPath -or -not [string]::Equals($scriptPath, $ExpectedScript, [System.StringComparison]::OrdinalIgnoreCase)) {
                return $false
            }
            $j = $i + 2
            while ($j -lt $tokens.Count) {
                $arg = $tokens[$j].ToLowerInvariant()
                if ($arg -eq '-homedir') {
                    if ($j + 1 -ge $tokens.Count) { return $false }
                    $argHome = Resolve-NormalizedPath $tokens[$j + 1]
                    if (-not $argHome -or -not [string]::Equals($argHome, $ExpectedHome, [System.StringComparison]::OrdinalIgnoreCase)) {
                        return $false
                    }
                    $j += 2
                } elseif ($arg -eq '-python') {
                    if ($j + 1 -ge $tokens.Count) { return $false }
                    $j += 2
                } else {
                    return $false
                }
            }
            return $true
        } elseif ($noArgFlags -contains $tok) {
            $i++
        } elseif ($oneArgFlags -contains $tok) {
            $i += 2
        } else {
            return $false
        }
    }
    return $false
}

function Test-ServerPythonProcess {
    param(
        $Process,
        [string]$ExpectedScript,
        [string]$ExpectedHome,
        [string[]]$AllowedExesWithoutHome = @()
    )
    $exe = Resolve-NormalizedPath $Process.ExecutablePath
    if (-not $exe -or -not [string]::Equals([System.IO.Path]::GetFileName($exe), 'python.exe', [System.StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    $tokens = @(Split-CommandLineArgs $Process.CommandLine)
    if ($tokens.Count -ne 2 -and $tokens.Count -ne 4) { return $false }
    $scriptPath = Resolve-NormalizedPath $tokens[1]
    if (-not $scriptPath -or -not [string]::Equals($scriptPath, $ExpectedScript, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    if ($tokens.Count -eq 4) {
        if (-not [string]::Equals($tokens[2], '--home', [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
        $argHome = Resolve-NormalizedPath $tokens[3]
        return [bool]($argHome -and [string]::Equals($argHome, $ExpectedHome, [System.StringComparison]::OrdinalIgnoreCase))
    }
    return (Test-PathInList -Path $exe -List $AllowedExesWithoutHome)
}

function Find-ServerProcesses {
    param(
        [object[]]$Processes,
        [int]$SelfPid = $PID,
        [string]$ServerDir,
        [string]$HomeDir,
        [string]$ExpectedPowerShell = (Resolve-NormalizedPath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')),
        [string[]]$VenvPythons = @(),
        [string[]]$BasePythons = @()
    )
    $expectedScript = Resolve-NormalizedPath (Join-Path $ServerDir 'asr_server.py')
    $expectedSupervisor = Resolve-NormalizedPath (Join-Path $ServerDir 'scripts\run_server.ps1')
    $expectedHome = Resolve-NormalizedPath $HomeDir
    $allowedNoHome = @($VenvPythons + $BasePythons)

    $byPid = @{}
    foreach ($p in $Processes) {
        if ($null -ne $p -and $null -ne $p.ProcessId) {
            $byPid[[int]$p.ProcessId] = $p
        }
    }

    # Exclude $SelfPid and its entire caller ancestor chain.
    $excluded = New-Object 'System.Collections.Generic.HashSet[int]'
    $cur = $SelfPid
    while ($cur -gt 0 -and $excluded.Add($cur)) {
        if ($byPid.ContainsKey($cur) -and $null -ne $byPid[$cur].ParentProcessId) {
            $cur = [int]$byPid[$cur].ParentProcessId
        } else {
            break
        }
    }

    $supervisors = New-Object System.Collections.Generic.List[object]
    foreach ($p in $Processes) {
        if ($null -eq $p -or $excluded.Contains([int]$p.ProcessId)) { continue }
        if (Test-ServerSupervisorProcess -Process $p -ExpectedPowerShell $ExpectedPowerShell `
                -ExpectedScript $expectedSupervisor -ExpectedHome $expectedHome) {
            $supervisors.Add($p)
        }
    }

    $matched = New-Object System.Collections.Generic.List[object]
    $matchedPidSet = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($p in $Processes) {
        if ($null -eq $p -or $excluded.Contains([int]$p.ProcessId)) { continue }
        if (Test-ServerPythonProcess -Process $p -ExpectedScript $expectedScript `
                -ExpectedHome $expectedHome -AllowedExesWithoutHome $allowedNoHome) {
            $matched.Add($p)
            $null = $matchedPidSet.Add([int]$p.ProcessId)
        }
    }

    # Stop child workers before their parent venv launchers.
    $children = New-Object System.Collections.Generic.List[object]
    $parents = New-Object System.Collections.Generic.List[object]
    foreach ($p in $matched) {
        if ($null -ne $p.ParentProcessId -and $matchedPidSet.Contains([int]$p.ParentProcessId)) {
            $children.Add($p)
        } else {
            $parents.Add($p)
        }
    }
    $targets = New-Object System.Collections.Generic.List[object]
    foreach ($p in $children) { $targets.Add($p) }
    foreach ($p in $parents) { $targets.Add($p) }

    [pscustomobject]@{
        Supervisors   = @($supervisors.ToArray())
        ServerTargets = @($targets.ToArray())
        ExcludedPids  = @($excluded)
    }
}

function Get-ReadyServerLogLine {
    param(
        [string]$LogPath,
        [datetime]$Since
    )
    if (-not (Test-Path -LiteralPath $LogPath)) { return $null }
    $ready = Select-String -LiteralPath $LogPath -Pattern 'engine ready|offline window: models load' -ErrorAction SilentlyContinue |
        Select-Object -Last 1
    if (-not $ready -or $ready.Line.Length -lt 19) { return $null }
    $ts = [datetime]::MinValue
    if ([datetime]::TryParseExact(
            $ready.Line.Substring(0, 19),
            'yyyy-MM-dd HH:mm:ss',
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::None,
            [ref]$ts) -and $ts -ge $Since.AddSeconds(-1)) {
        return $ready.Line
    }
    return $null
}

if ($MyInvocation.InvocationName -ne '.') {
    $serverDir = Split-Path $PSScriptRoot -Parent
    $script = Join-Path $serverDir 'asr_server.py'
    $since = Get-Date
    $pyPaths = Get-ServerPythonPaths -HomeDir $HomeDir
    $allProcs = @(Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine)
    $found = Find-ServerProcesses -Processes $allProcs -SelfPid $PID `
        -ServerDir $serverDir -HomeDir $HomeDir `
        -VenvPythons $pyPaths.Venv -BasePythons $pyPaths.Base
    if (-not $found.ServerTargets) { Write-Host "no running server found for $script" }
    foreach ($p in $found.ServerTargets) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "stopped $($p.ProcessId)"
    }
    if (-not $found.Supervisors -and $TaskName -and (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Host "started $TaskName task"
    }
    $log = Join-Path $HomeDir 'logs\server.log'
    $deadline = $since.AddSeconds($TimeoutSec)
    $readyLine = $null
    do {
        Start-Sleep 2
        $readyLine = Get-ReadyServerLogLine -LogPath $log -Since $since
    } until ($readyLine -or (Get-Date) -gt $deadline)
    if ($readyLine) { $readyLine } else { Write-Host "NOT READY after $TimeoutSec s; check $log"; exit 1 }
}
