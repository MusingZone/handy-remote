# Regenerates the README animations in docs/assets from the app's real React UI.
# Starts a private Vite dev server, records with Playwright (Microsoft Edge, headless) against a
# mocked Tauri IPC, builds the GIFs with Pillow, then stops the server. Needs: npm install done,
# Microsoft Edge, Python with Pillow.
param(
    [string[]]$Scenes = @(),
    [int]$Port = 5179,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$frames = Join-Path $env:TEMP ("handy-readme-gifs-" + [guid]::NewGuid())
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = (Get-Command node).Source
$start.Arguments = "node_modules\vite\bin\vite.js --port $Port --strictPort --host 127.0.0.1"
$start.WorkingDirectory = $repo.Path
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$vite = [System.Diagnostics.Process]::Start($start)
try {
    $deadline = (Get-Date).AddSeconds(60)
    while (-not (Test-NetConnection 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue)) {
        if ((Get-Date) -gt $deadline -or $vite.HasExited) { throw "Vite did not start on port $Port" }
        Start-Sleep -Milliseconds 500
    }
    & node (Join-Path $PSScriptRoot "record.mjs") "http://127.0.0.1:$Port" $frames @Scenes
    if ($LASTEXITCODE) { throw "recording failed" }
    & $Python (Join-Path $PSScriptRoot "make_gif.py") $frames (Join-Path $repo "docs\assets") @Scenes
    if ($LASTEXITCODE) { throw "GIF build failed" }
}
finally {
    if (-not $vite.HasExited) { Stop-Process -Id $vite.Id }
    Remove-Item -Recurse -Force $frames -ErrorAction SilentlyContinue
}
