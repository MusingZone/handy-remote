<#
.SYNOPSIS
Collects the release assets of a Handy Remote build: the renamed NSIS installer and its updater
signature, latest.json for the in-app updater, the recognition server zip, SHA256SUMS.txt and the
release notes. Used by .github/workflows/remote-release.yml; runs locally for a dry run.

.EXAMPLE
tools\ci\New-ReleaseAssets.ps1 -Version 0.10.1-remote.1 -BundleDir src-tauri\target\release\bundle\nsis -OutDir dist-release
#>
param(
    [Parameter(Mandatory)][string]$Version,
    [Parameter(Mandatory)][string]$BundleDir,
    [Parameter(Mandatory)][string]$OutDir,
    [string]$Repo = 'MusingZone/handy-remote',
    [string]$UpstreamRepo = 'cjpais/Handy',
    # Commit range for the notes; defaults to everything since the previous fork release.
    [string]$Since
)
$ErrorActionPreference = 'Stop'
if ($Version -notmatch '^\d+\.\d+\.\d+-remote\.\d+$') { throw "version must look like 0.10.1-remote.1, got '$Version'" }
$root = (& git rev-parse --show-toplevel).Trim()
$utf8 = New-Object Text.UTF8Encoding($false)
function Write-Utf8([string]$path, [string]$text) { [IO.File]::WriteAllText($path, $text.Replace("`r`n", "`n"), $utf8) }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$tag = "v$Version"
$base = "https://github.com/$Repo/releases/download/$tag"

# Installer and signature. Tauri names it after productName, which has a space; GitHub would turn
# that into a dot, so the assets get a stable name of our own.
$setup = Get-ChildItem -LiteralPath $BundleDir -Filter "*_${Version}_x64-setup.exe" | Select-Object -First 1
if (-not $setup) { throw "no *_${Version}_x64-setup.exe in $BundleDir" }
$sig = Get-Item -LiteralPath "$($setup.FullName).sig" -ErrorAction SilentlyContinue
if (-not $sig) { throw "no updater signature next to $($setup.Name); is TAURI_SIGNING_PRIVATE_KEY set?" }
$setupName = "Handy-Remote_${Version}_x64-setup.exe"
Copy-Item -LiteralPath $setup.FullName -Destination (Join-Path $OutDir $setupName) -Force
Copy-Item -LiteralPath $sig.FullName -Destination (Join-Path $OutDir "$setupName.sig") -Force
$signature = (Get-Content -LiteralPath $sig.FullName -Raw).Trim()

# Release notes: upstream base, fork changes since the previous fork release, install hints.
$upstream = (Get-Content -LiteralPath (Join-Path $root '.upstream-version') -Raw).Trim()
if (-not $Since) {
    $prev = @(& git tag --list 'v*-remote.*' --merged HEAD --sort=-v:refname) | Where-Object { $_ -ne $tag } | Select-Object -First 1
    if ($prev) { $Since = "$prev..HEAD" } else { $Since = "$upstream..HEAD" }
}
$changes = @(& git log --first-parent --no-merges --format='- %s' $Since) | Where-Object { $_ }
if (-not $changes) { $changes = @('- Rebuilt on the current upstream release.') }
$notes = @"
Handy Remote $Version, based on upstream Handy [$upstream](https://github.com/$UpstreamRepo/releases/tag/$upstream).
Upstream's own changes are in its release notes; this fork's additions are listed in
[CHANGES-FROM-UPSTREAM.md](https://github.com/$Repo/blob/$tag/CHANGES-FROM-UPSTREAM.md).

### Changes in this fork since the previous release
$($changes -join "`n")

### Install
- **Windows client:** run ``$setupName``. It is not code-signed: if SmartScreen says "Windows
  protected your PC", choose **More info -> Run anyway**. Installed copies update themselves from
  the stable release feed only; this trial build is opt-in. Every update is verified against the
  minisign key in the app. Stable promotion requires trial feedback and explicit approval.
- **Recognition server:** ``handy-remote-server-$Version.zip``; see
  [server setup](https://github.com/$Repo/blob/$tag/docs/remote/SERVER-SETUP.md).
- Check downloads against ``SHA256SUMS.txt``.
"@
Write-Utf8 (Join-Path $OutDir 'release-notes.md') $notes

# latest.json for the Tauri updater (static JSON format).
$platform = [ordered]@{ signature = $signature; url = "$base/$setupName" }
$latest = [ordered]@{
    version   = $Version
    notes     = "Handy Remote $Version (upstream $upstream). See https://github.com/$Repo/releases/tag/$tag"
    pub_date  = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    platforms = [ordered]@{ 'windows-x86_64' = $platform; 'windows-x86_64-nsis' = $platform }
}
Write-Utf8 (Join-Path $OutDir 'latest.json') ($latest | ConvertTo-Json -Depth 5)

# Server zip: the tracked server/ files plus VERSION, under a top-level server/ folder.
$stage = Join-Path ([IO.Path]::GetTempPath()) ("hr-server-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    $tar = Join-Path $stage 'server.tar'
    & git -C $root archive --format=tar -o $tar HEAD server
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
    & tar -xf $tar -C $stage
    if ($LASTEXITCODE -ne 0) { throw 'tar extract failed' }
    Remove-Item -LiteralPath $tar
    Write-Utf8 (Join-Path $stage 'server\VERSION') "$Version`n"
    $zip = Join-Path $OutDir "handy-remote-server-$Version.zip"
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip }
    Compress-Archive -Path (Join-Path $stage 'server') -DestinationPath $zip
} finally { Remove-Item -LiteralPath $stage -Recurse -Force }

# Checksums of everything a user or the updater downloads.
$sums = Get-ChildItem -LiteralPath $OutDir -File | Where-Object { $_.Name -notin @('SHA256SUMS.txt', 'release-notes.md') } |
    Sort-Object Name | ForEach-Object { "{0}  {1}" -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $_.Name }
Write-Utf8 (Join-Path $OutDir 'SHA256SUMS.txt') (($sums -join "`n") + "`n")
Get-ChildItem -LiteralPath $OutDir -File | ForEach-Object { "{0,12}  {1}" -f $_.Length, $_.Name }
