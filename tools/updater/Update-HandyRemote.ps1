<#
.SYNOPSIS
  Installs the latest Handy Remote release when it is newer than the installed one, safely and unattended.
.DESCRIPTION
  1. Reads the latest GitHub release of -Repo.
  2. Downloads the Windows installer, its updater signature (.sig) and SHA256SUMS.txt.
  3. Verifies the SHA-256 and the minisign signature against -PublicKey (the app's updater key).
  4. Waits until Handy Remote is idle: no keyboard/mouse input and no log activity for -IdleMinutes.
  5. Backs up the installed files, stops Handy Remote, runs the installer silently, restarts it with
     --start-hidden, and checks it stays up. On failure the backup is restored and the old version restarted.
  With -ServerDir it also updates a recognition server installed from the release's server zip
  (only when the server is idle, with a Status health check and rollback).
  Log: %LOCALAPPDATA%\handy-remote\updater\update.log. Normally run by the task Install-AutoUpdate.ps1 registers.
#>
param(
    [string]$Repo = 'MusingZone/handy-remote',
    # Same value as plugins.updater.pubkey in src-tauri/tauri.conf.json.
    [string]$PublicKey = 'dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDM2MTZGMTlEMThBRDcwNApSV1FFMTRyUkdXOWhBOTU2K1lpZS92L0JNWUN1K1M3N2ZQMUtidUwxZEpVempBWEpMR1FZYzZOMAo=',
    [string]$ProductName = 'Handy Remote',
    [string]$Identifier = 'com.musingzone.handy-remote',
    [int]$IdleMinutes = 2,
    [int]$MaxWaitMinutes = 45,
    [int]$HealthSeconds = 30,
    # Reinstall even when the installed version is current.
    [switch]$Force,
    # Install this release (e.g. 0.10.1-remote.6) instead of the latest one: trying a pre-release, or going back.
    [string]$Version,
    # Only report what would happen.
    [switch]$CheckOnly,
    # Optional: recognition server code folder (the contents of server/), its home folder and port.
    [string]$ServerDir,
    [string]$ServerHome = $(if ($env:HANDY_REMOTE_HOME) { $env:HANDY_REMOTE_HOME } else { Join-Path $env:LOCALAPPDATA 'handy-remote-server' }),
    [int]$ServerPort = 47821,
    # Optional script called with -Message <text> after an install, rollback or failure (e.g. a phone push).
    [string]$NotifyScript
)
$ErrorActionPreference = 'Stop'
$state = Join-Path $env:LOCALAPPDATA 'handy-remote\updater'
New-Item -ItemType Directory -Force -Path $state | Out-Null
$logFile = Join-Path $state 'update.log'

function Log([string]$msg) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
}

function Notify([string]$msg) {
    Log $msg
    if ($NotifyScript -and (Test-Path -LiteralPath $NotifyScript)) {
        try { & $NotifyScript -Message "Handy Remote: $msg" } catch { Log "notify failed: $_" }
    }
}

function Compare-SemVer([string]$a, [string]$b) {
    $pa = $a.TrimStart('v') -split '[-+]', 2
    $pb = $b.TrimStart('v') -split '[-+]', 2
    $ca = @($pa[0].Split('.') | ForEach-Object { [int]$_ })
    $cb = @($pb[0].Split('.') | ForEach-Object { [int]$_ })
    for ($i = 0; $i -lt 3; $i++) { if ($ca[$i] -ne $cb[$i]) { return [Math]::Sign($ca[$i] - $cb[$i]) } }
    $ra = if ($pa.Count -gt 1) { ($pa[1] -split '\+')[0] } else { '' }
    $rb = if ($pb.Count -gt 1) { ($pb[1] -split '\+')[0] } else { '' }
    if ($ra -eq $rb) { return 0 }
    if (-not $ra) { return 1 }
    if (-not $rb) { return -1 }
    $ia = $ra.Split('.'); $ib = $rb.Split('.')
    for ($i = 0; $i -lt [Math]::Min($ia.Count, $ib.Count); $i++) {
        $na = $ia[$i] -match '^\d+$'; $nb = $ib[$i] -match '^\d+$'
        if ($na -and $nb) { $d = [Math]::Sign([long]$ia[$i] - [long]$ib[$i]) }
        elseif ($na) { $d = -1 } elseif ($nb) { $d = 1 }
        else { $d = [Math]::Sign([string]::CompareOrdinal($ia[$i], $ib[$i])) }
        if ($d -ne 0) { return $d }
    }
    return [Math]::Sign($ia.Count - $ib.Count)
}

function Get-Installed {
    foreach ($root in 'HKCU:', 'HKLM:') {
        $key = "$root\Software\Microsoft\Windows\CurrentVersion\Uninstall\$ProductName"
        if (Test-Path $key) {
            $p = Get-ItemProperty $key
            $dir = $p.'(default)'
            $exe = if ($p.DisplayIcon) { $p.DisplayIcon.Trim('"') -replace ',\d+$', '' } else { $null }
            if (-not $dir -and $exe) { $dir = Split-Path $exe -Parent }
            return [pscustomobject]@{ Key = $key; Dir = $dir; Exe = $exe; Version = $p.DisplayVersion }
        }
    }
    return $null
}

function Get-AppProcesses($inst) {
    if (-not $inst -or -not $inst.Exe) { return @() }
    $name = [IO.Path]::GetFileNameWithoutExtension($inst.Exe)
    # Process.Path is empty for elevated processes when this runs unelevated; this query works across levels.
    @(Get-Process -Name $name -ErrorAction SilentlyContinue | Where-Object {
        $path = [HrIdle]::ImagePath($_.Id)
        $path -and $path -ieq $inst.Exe
    })
}

function Start-App($inst) {
    if ($inst.Exe -and (Test-Path -LiteralPath $inst.Exe) -and -not (Get-AppProcesses $inst)) {
        Start-Process -FilePath $inst.Exe -ArgumentList '--start-hidden' | Out-Null
    }
}

function Stop-App($inst) {
    foreach ($p in (Get-AppProcesses $inst)) {
        Log "stopping $($p.Id)"
        try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch { Log "cannot stop $($p.Id): $($_.Exception.Message)" }
    }
    for ($i = 0; $i -lt 20 -and (Get-AppProcesses $inst); $i++) { Start-Sleep -Milliseconds 500 }
    return -not (Get-AppProcesses $inst)
}

Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public static class HrIdle {
    [StructLayout(LayoutKind.Sequential)] struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    public static double Seconds() {
        LASTINPUTINFO i = new LASTINPUTINFO(); i.cbSize = (uint)Marshal.SizeOf(i);
        if (!GetLastInputInfo(ref i)) return 0;
        return unchecked((uint)Environment.TickCount - i.dwTime) / 1000.0;
    }
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool QueryFullProcessImageName(IntPtr h, uint flags, System.Text.StringBuilder name, ref uint size);
    public static string ImagePath(int pid) {
        IntPtr h = OpenProcess(0x1000, false, pid);
        if (h == IntPtr.Zero) return null;
        try {
            var sb = new System.Text.StringBuilder(1024); uint n = (uint)sb.Capacity;
            return QueryFullProcessImageName(h, 0, sb, ref n) ? sb.ToString() : null;
        } finally { CloseHandle(h); }
    }
}
'@

function Wait-Idle {
    $logs = @(
        (Join-Path $env:LOCALAPPDATA "$Identifier\logs"),
        (Join-Path $env:APPDATA "$Identifier\logs")
    ) | Where-Object { Test-Path -LiteralPath $_ }
    $deadline = (Get-Date).AddMinutes($MaxWaitMinutes)
    while ($true) {
        $inputIdle = [HrIdle]::Seconds()
        $last = $logs | ForEach-Object { Get-ChildItem -LiteralPath $_ -File -ErrorAction SilentlyContinue } |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        $logIdle = if ($last) { ((Get-Date) - $last.LastWriteTime).TotalSeconds } else { [double]::MaxValue }
        if ($inputIdle -ge $IdleMinutes * 60 -and $logIdle -ge $IdleMinutes * 60) { return $true }
        if ((Get-Date) -gt $deadline) { return $false }
        Start-Sleep 20
    }
}

function Get-Asset($release, [string]$pattern) {
    @($release.assets | Where-Object { $_.name -like $pattern }) | Select-Object -First 1
}

function Save-Asset($asset, [string]$dir) {
    $out = Join-Path $dir $asset.name
    if (-not (Test-Path -LiteralPath $out) -or (Get-Item -LiteralPath $out).Length -ne $asset.size) {
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $out -UseBasicParsing -Headers $headers
    }
    return $out
}

function Test-Sha256([string]$file, [string]$sums) {
    $name = Split-Path $file -Leaf
    $line = Get-Content -LiteralPath $sums | Where-Object { $_ -match "^([0-9a-fA-F]{64})\s+\*?(.+)$" -and $Matches[2].Trim() -eq $name } | Select-Object -First 1
    if (-not $line) { throw "$name is not listed in SHA256SUMS" }
    $want = ($line -split '\s+')[0].ToLowerInvariant()
    $have = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($want -ne $have) { throw "SHA-256 mismatch for $name" }
}

function Send-StatusProbe {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $client.ReceiveTimeout = 10000; $client.SendTimeout = 10000
        $client.Connect('127.0.0.1', $ServerPort)
        $s = $client.GetStream()
        $j = [Text.Encoding]::UTF8.GetBytes('"Status"')
        $frame = [byte[]]([BitConverter]::GetBytes([uint32]$j.Length) + $j + [BitConverter]::GetBytes([uint32]0))
        $s.Write($frame, 0, $frame.Length)
        $r = New-Object IO.BinaryReader($s)
        $len = $r.ReadUInt32()
        $body = [Text.Encoding]::UTF8.GetString($r.ReadBytes([int]$len))
        [void]$r.ReadBytes([int]$r.ReadUInt32())
        return ($body | ConvertFrom-Json).Status
    } finally { $client.Close() }
}

function Update-Server($release, [string]$sums, [string]$dl) {
    $zip = Get-Asset $release 'handy-remote-server-*.zip'
    if (-not $zip) { Log 'release has no server zip'; return }
    $ver = $release.tag_name.TrimStart('v')
    $verFile = Join-Path $ServerDir 'VERSION'
    $cur = if (Test-Path -LiteralPath $verFile) { (Get-Content -LiteralPath $verFile -Raw).Trim() } else { '' }
    if ($cur -eq $ver -and -not $Force) { Log "server $cur is current"; return }
    Log "server update $(if ($cur) { $cur } else { '(unknown)' }) -> $ver"
    if ($CheckOnly) { return }
    $file = Save-Asset $zip $dl
    Test-Sha256 $file $sums
    $st = Send-StatusProbe
    if ($st.loading -or ($st.idle_s -lt $IdleMinutes * 60)) { Log "server busy (idle $($st.idle_s) s); retry next run"; return }
    $tmp = Join-Path $dl 'server'
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
    Expand-Archive -LiteralPath $file -DestinationPath $tmp
    $src = if (Test-Path (Join-Path $tmp 'server\asr_server.py')) { Join-Path $tmp 'server' } else { $tmp }
    $bak = Join-Path $ServerHome ("backup\server-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Force -Path $bak | Out-Null
    Copy-Item -Path (Join-Path $ServerDir '*') -Destination $bak -Recurse -Force
    Log "server backup: $bak"
    $restart = { & powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File (Join-Path $ServerDir 'scripts\restart_server.ps1') -HomeDir $ServerHome; $LASTEXITCODE }
    Copy-Item -Path (Join-Path $src '*') -Destination $ServerDir -Recurse -Force
    $ok = (& $restart) -eq 0
    if ($ok) { try { $ok = [bool](Send-StatusProbe).label } catch { $ok = $false } }
    if ($ok) { Notify "server updated to $ver"; return }
    Copy-Item -Path (Join-Path $bak '*') -Destination $ServerDir -Recurse -Force
    [void](& $restart)
    Notify "server update to $ver failed; restored $(if ($cur) { $cur } else { 'the previous files' })"
}

$mutex = New-Object Threading.Mutex($false, 'Local\HandyRemote-AutoUpdate')
if (-not $mutex.WaitOne(0)) { Write-Host 'another update run is active'; exit 0 }
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $headers = @{ 'User-Agent' = 'handy-remote-updater'; 'Accept' = 'application/vnd.github+json' }
    if ($env:GITHUB_TOKEN) { $headers.Authorization = "Bearer $($env:GITHUB_TOKEN)" }
    $releaseUri = if ($Version) { "https://api.github.com/repos/$Repo/releases/tags/v$($Version.TrimStart('v'))" } else { "https://api.github.com/repos/$Repo/releases/latest" }
    $release = Invoke-RestMethod -Uri $releaseUri -Headers $headers -UseBasicParsing
    $latest = $release.tag_name.TrimStart('v')
    $inst = Get-Installed
    $current = if ($inst) { $inst.Version } else { $null }
    Log "$(if ($Version) { 'requested' } else { 'latest' }) $latest, installed $(if ($current) { $current } else { 'none' })"

    $dl = Join-Path $state "downloads\$latest"
    New-Item -ItemType Directory -Force -Path $dl | Out-Null
    $sumsAsset = Get-Asset $release 'SHA256SUMS*'
    if (-not $sumsAsset) { throw 'release has no SHA256SUMS' }
    $sums = Save-Asset $sumsAsset $dl

    $needed = $Force -or -not $current -or $(if ($Version) { $latest -ne $current } else { (Compare-SemVer $latest $current) -gt 0 })
    if (-not $needed) { Log 'app is current' }
    elseif ($CheckOnly) { Log "would install $latest" }
    else {
        $setupAsset = Get-Asset $release '*x64-setup.exe'
        $sigAsset = Get-Asset $release '*x64-setup.exe.sig'
        if (-not $setupAsset -or -not $sigAsset) { throw 'release has no x64 installer and signature' }
        $setup = Save-Asset $setupAsset $dl
        $sig = Save-Asset $sigAsset $dl
        Test-Sha256 $setup $sums
        Add-Type -Path (Join-Path $PSScriptRoot 'Minisign.cs') -ReferencedAssemblies System.Numerics
        $trusted = [HandyRemote.Minisign]::Verify($PublicKey, [IO.File]::ReadAllText($sig), [IO.File]::ReadAllBytes($setup))
        Log "verified $($setupAsset.name) ($trusted)"

        if (-not (Wait-Idle)) { Log "not idle within $MaxWaitMinutes min; retry next run"; exit 0 }
        $backup = $null
        if ($inst -and $inst.Dir -and (Test-Path -LiteralPath $inst.Dir)) {
            $backup = Join-Path $state "backup\$current"
            if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
            New-Item -ItemType Directory -Force -Path $backup | Out-Null
            Copy-Item -Path (Join-Path $inst.Dir '*') -Destination $backup -Recurse -Force
            Log "backup: $backup"
        }
        if (-not (Stop-App $inst)) { Log 'app could not be stopped (running elevated?); retry next run'; exit 0 }
        $p = Start-Process -FilePath $setup -ArgumentList '/S' -Wait -PassThru
        Log "installer exit code $($p.ExitCode)"
        $new = Get-Installed
        $ok = $p.ExitCode -eq 0 -and $new -and $new.Exe -and (Test-Path -LiteralPath $new.Exe)
        if ($ok) {
            $fileVersion = (Get-Item -LiteralPath $new.Exe).VersionInfo.ProductVersion
            if ($fileVersion -and $fileVersion -ne $latest) { Log "installed file reports $fileVersion, expected $latest"; $ok = $false }
        }
        if ($ok) {
            Start-App $new
            Start-Sleep -Seconds $HealthSeconds
            $ok = [bool](Get-AppProcesses $new)
        }
        if ($ok) {
            Notify "updated to $($new.Version)"
            Get-ChildItem -LiteralPath (Join-Path $state 'backup') -Directory | Sort-Object LastWriteTime -Descending |
                Select-Object -Skip 2 | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
        } elseif ($backup) {
            $null = Stop-App $new
            Copy-Item -Path (Join-Path $backup '*') -Destination $inst.Dir -Recurse -Force
            Set-ItemProperty -Path $inst.Key -Name DisplayVersion -Value $current -ErrorAction SilentlyContinue
            Start-App $inst
            Notify "update to $latest failed; restored $current"
        } else {
            Notify "install of $latest failed"
        }
    }
    Get-ChildItem -LiteralPath (Join-Path $state 'downloads') -Directory | Where-Object Name -ne $latest |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
    if ($ServerDir) { Update-Server $release $sums $dl }
} catch {
    Notify "update check failed: $($_.Exception.Message)"
    exit 1
} finally {
    $mutex.ReleaseMutex()
}
