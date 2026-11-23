<#
.SYNOPSIS
  Downloads the recognition models into <home>\models and verifies them.
.DESCRIPTION
  Hugging Face files are checked against the SHA-256 that the Hub publishes for large (LFS) files and
  against the listed size for the rest; interrupted downloads resume. sherpa-onnx models come from the
  k2-fsa/sherpa-onnx GitHub releases. curl.exe honours the standard HTTPS_PROXY / NO_PROXY variables.
.PARAMETER HomeDir
  Server home. Default: $env:HANDY_REMOTE_HOME, else %LOCALAPPDATA%\handy-remote-server.
.PARAMETER Source
  'hf' (huggingface.co) or 'hf-mirror' (hf-mirror.com, for networks where the Hub is unreachable).
.PARAMETER SkipNpu
  Do not download the NPU draft model (machines without an Intel NPU).
#>
param(
    [string]$HomeDir = $(if ($env:HANDY_REMOTE_HOME) { $env:HANDY_REMOTE_HOME } else { Join-Path $env:LOCALAPPDATA 'handy-remote-server' }),
    [ValidateSet('hf', 'hf-mirror')][string]$Source = 'hf',
    [switch]$SkipNpu
)
$ErrorActionPreference = 'Stop'
$models = Join-Path $HomeDir 'models'
New-Item -ItemType Directory -Force $models | Out-Null
$hub = if ($Source -eq 'hf') { 'https://huggingface.co' } else { 'https://hf-mirror.com' }
$sherpa = 'https://github.com/k2-fsa/sherpa-onnx/releases/download'
$failed = New-Object System.Collections.Generic.List[string]

function Get-HfRepo([string]$Repo, [string]$Dest) {
    Write-Host "== $Repo -> $Dest"
    $list = curl.exe -sSL --fail -m 120 "$hub/api/models/$Repo/tree/main?recursive=true" | ConvertFrom-Json
    if (-not $list) { throw "could not list $Repo on $hub" }
    foreach ($f in ($list | Where-Object type -eq 'file' | Sort-Object size)) {
        $out = Join-Path $Dest ($f.path -replace '/', '\')
        New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
        $sha = if ($f.lfs) { $f.lfs.oid } else { $null }
        $size = if ($f.lfs) { $f.lfs.size } else { $f.size }
        $ok = $false
        for ($i = 1; $i -le 10 -and -not $ok; $i++) {
            if ((Test-Path $out) -and (Get-Item $out).Length -eq $size) {
                $ok = -not $sha -or (Get-FileHash $out -Algorithm SHA256).Hash.ToLower() -eq $sha
                if ($ok) { break }
                Remove-Item $out
            }
            $part = "$out.part"
            curl.exe -sSL --fail --retry 5 -C - -m 7200 -o $part "$hub/$Repo/resolve/main/$($f.path)"
            if ($LASTEXITCODE -eq 0 -and (Get-Item $part).Length -eq $size) { Move-Item -Force $part $out }
            elseif ($LASTEXITCODE -ne 0) { Start-Sleep 10 }
        }
        Write-Host ("  {0,-4} {1} ({2:N1} MB)" -f $(if ($ok) { 'ok' } else { 'FAIL' }), $f.path, ($size / 1MB))
        if (-not $ok) { $failed.Add("$Repo/$($f.path)") }
    }
}

function Get-SherpaModel([string]$Name, [string]$Check) {
    if (Test-Path (Join-Path $models $Check)) { Write-Host "  ok   $Name (present)"; return }
    $file = Join-Path $models $Name
    curl.exe -sSL --fail --retry 5 -o $file "$sherpa/$(if ($Name -like '*punct*') { 'punctuation-models' } else { 'asr-models' })/$Name"
    if ($LASTEXITCODE -ne 0) { $failed.Add($Name); Write-Host "  FAIL $Name"; return }
    if ($Name -like '*.tar.bz2') { tar -xjf $file -C $models; Remove-Item $file }
    if (Test-Path (Join-Path $models $Check)) { Write-Host "  ok   $Name" } else { $failed.Add($Name); Write-Host "  FAIL $Name" }
}

Get-HfRepo 'dseditor/Qwen3-ASR-1.7B-INT8_OpenVINO' (Join-Path $models 'qwen3-asr-1.7b-ov-int8')
if (-not $SkipNpu) {
    Get-HfRepo 'iamwaqargulzar/handy-qwen3-asr-0.6b-openvino-npu-int8' (Join-Path $models 'qwen3-asr-0.6b-ov-npu-int8')
}
Write-Host '== sherpa-onnx'
Get-SherpaModel 'silero_vad.onnx' 'silero_vad.onnx'
Get-SherpaModel 'sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2' 'sherpa-onnx-streaming-paraformer-bilingual-zh-en\tokens.txt'
Get-SherpaModel 'sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8.tar.bz2' 'sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8\model.int8.onnx'

if ($failed.Count) { Write-Host "FAILED: $($failed -join ', ')"; exit 1 }
Write-Host "All models are in $models"
