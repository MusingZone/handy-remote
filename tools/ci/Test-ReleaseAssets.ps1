<#
.SYNOPSIS
Verifies every downloadable release asset, updater metadata, installer signature and tamper rejection.
#>
param(
    [Parameter(Mandatory)][string]$Version,
    [Parameter(Mandatory)][string]$AssetsDir,
    [string]$Repo = 'MusingZone/handy-remote'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$AssetsDir = (Resolve-Path -LiteralPath $AssetsDir).Path
$setupName = "Handy-Remote_${Version}_x64-setup.exe"
$expected = @($setupName, "$setupName.sig", "handy-remote-server-$Version.zip", 'latest.json')
$seen = @{}
foreach ($line in Get-Content -LiteralPath (Join-Path $AssetsDir 'SHA256SUMS.txt')) {
    if ($line -notmatch '^([0-9a-fA-F]{64})  ([^\\/]+)$') { throw 'Malformed checksum entry' }
    $hash, $name = $Matches[1], $Matches[2]
    if ($name -notin $expected -or $seen.ContainsKey($name)) { throw "Unexpected or duplicate asset: $name" }
    $actual = (Get-FileHash -LiteralPath (Join-Path $AssetsDir $name) -Algorithm SHA256).Hash
    if ($actual -ne $hash) { throw "SHA-256 mismatch: $name" }
    $seen[$name] = $true
}
foreach ($name in $expected) {
    if (-not $seen.ContainsKey($name)) { throw "Checksum manifest is missing $name" }
}

$config = Get-Content -LiteralPath (Join-Path $root 'src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json
$signature = [IO.File]::ReadAllText((Join-Path $AssetsDir "$setupName.sig")).Trim()
$bytes = [IO.File]::ReadAllBytes((Join-Path $AssetsDir $setupName))
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    Add-Type -Path (Join-Path $root 'tools\updater\Minisign.cs') -ReferencedAssemblies System.Numerics
} else {
    Add-Type -Path (Join-Path $root 'tools\updater\Minisign.cs')
}
[void][HandyRemote.Minisign]::Verify($config.plugins.updater.pubkey, $signature, $bytes)
$bytes[0] = $bytes[0] -bxor 1
$rejected = $false
try {
    [void][HandyRemote.Minisign]::Verify($config.plugins.updater.pubkey, $signature, $bytes)
} catch [System.Management.Automation.MethodInvocationException] {
    if ($_.Exception.InnerException -isnot [System.Security.Cryptography.CryptographicException]) { throw }
    $rejected = $true
}
if (-not $rejected) { throw 'Signature verification accepted tampered data' }

$latest = Get-Content -LiteralPath (Join-Path $AssetsDir 'latest.json') -Raw | ConvertFrom-Json
if ($latest.version -ne $Version) { throw 'Updater version does not match the release' }
$url = "https://github.com/$Repo/releases/download/v$Version/$setupName"
foreach ($platform in @('windows-x86_64', 'windows-x86_64-nsis')) {
    $entry = $latest.platforms.$platform
    if (-not $entry -or $entry.signature -ne $signature -or $entry.url -ne $url) {
        throw "Updater metadata mismatch: $platform"
    }
}
Write-Output "Verified $($seen.Count) asset checksums, updater metadata and installer signature; tampered data rejected."
