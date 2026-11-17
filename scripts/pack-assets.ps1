<#
.SYNOPSIS
  Builds the heavy optional archives (engine.zip, hermes.zip) for GitHub Releases.

.DESCRIPTION
  The repository intentionally does not store llama.cpp binaries (a single
  cublas DLL is ~440 MB, above GitHub's 100 MB per-file limit) or the vendored
  Hermes source. Build the archives here, attach them to a GitHub Release, and
  let users fetch them with:

      .\scripts\bootstrap.ps1 -EngineUrl <url-to-engine.zip> -HermesUrl <url-to-hermes.zip>

.EXAMPLE
  .\scripts\pack-assets.ps1 -Source "D:\InfinitySearch"

.EXAMPLE
  .\scripts\pack-assets.ps1 -Source "D:\InfinitySearch" -WithHermesProfile
#>
[CmdletBinding()]
param(
    [string]$Source = "",
    [string]$Out = (Join-Path (Split-Path -Parent $PSScriptRoot) "release-assets"),
    [switch]$WithHermesProfile
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

if (-not $Source) {
    if (Test-Path (Join-Path $Root "engine\llama-server.exe")) {
        $Source = $Root
    } else {
        throw "Pass -Source <portable build folder> (the one containing engine\ and app\hermes_module\)."
    }
}
$Source = (Resolve-Path $Source).Path
New-Item -ItemType Directory -Force -Path $Out | Out-Null

function Say([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Warn([string]$Text) { Write-Host "[!] $Text" -ForegroundColor Yellow }

function New-Zip([string]$SrcDir, [string]$ZipPath) {
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
    $tar = Get-Command tar -ErrorAction SilentlyContinue
    if ($tar) {
        & tar -a -c -f $ZipPath -C $SrcDir .
        if ($LASTEXITCODE -eq 0) { return }
        Warn "tar failed, falling back to Compress-Archive"
    }
    Compress-Archive -Path (Join-Path $SrcDir "*") -DestinationPath $ZipPath -CompressionLevel Optimal -Force
}

# ---------------------------------------------------------------------------
# engine.zip
# ---------------------------------------------------------------------------
$engine = Join-Path $Source "engine"
if (Test-Path (Join-Path $engine "llama-server.exe")) {
    Say "Packing engine -> engine.zip"
    New-Zip $engine (Join-Path $Out "engine.zip")
    $mb = [math]::Round((Get-Item (Join-Path $Out "engine.zip")).Length / 1MB, 1)
    Write-Host "    engine.zip: $mb MB"
} else {
    Warn "engine\llama-server.exe not found in $Source — engine.zip skipped"
}

# ---------------------------------------------------------------------------
# hermes.zip
# ---------------------------------------------------------------------------
$hm = Join-Path $Source "app\hermes_module"
$stage = Join-Path $env:TEMP ("infinity-hermes-stage-" + [guid]::NewGuid().ToString("N"))
$hasHermes = (Test-Path (Join-Path $hm "backend")) -or (Test-Path (Join-Path $hm "frontend"))
if ($hasHermes) {
    Say "Packing Hermes backend/frontend -> hermes.zip"
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    try {
        $excludeDirs = @("__pycache__", "node_modules", ".venv", "logs", "data", ".hermes-portable")
        foreach ($name in @("backend", "frontend")) {
            $src = Join-Path $hm $name
            if (-not (Test-Path $src)) { continue }
            $dst = Join-Path $stage $name
            $args = @($src, $dst, "/E", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/XD") + $excludeDirs + @("/XF", "*.pyc", ".env")
            & robocopy @args | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $name ($LASTEXITCODE)" }
        }
        if ($WithHermesProfile) {
            $prof = Join-Path $hm ".hermes-portable"
            if (Test-Path $prof) {
                Say "Including .hermes-portable profile"
                $args = @($prof, (Join-Path $stage ".hermes-portable"), "/E", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/XD", "__pycache__", "logs")
                & robocopy @args | Out-Null
            }
        }
        New-Zip $stage (Join-Path $Out "hermes.zip")
        $mb = [math]::Round((Get-Item (Join-Path $Out "hermes.zip")).Length / 1MB, 1)
        Write-Host "    hermes.zip: $mb MB"
    } finally {
        Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
} else {
    Warn "Hermes backend/frontend not found in $Source — hermes.zip skipped"
}

# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
$readme = @"
Release assets for Infinity Search
==================================

Attach these files to a GitHub Release (not to the git tree):

  engine.zip  -> llama.cpp build (llama-server.exe + DLLs).
                 Users: scripts\bootstrap.ps1 -EngineUrl <url>
  hermes.zip  -> app\hermes_module\backend + frontend.
                 Users: scripts\bootstrap.ps1 -HermesUrl <url>

Why not in Git:
  * engine\cublasLt64_13.dll is ~440 MB, above GitHub's 100 MB per-file limit;
  * the engine and Hermes are third-party binaries and are distributed
    separately from the MIT-licensed source.

Full installation and optional components: docs\EXTERNAL.md
"@
Set-Content -Path (Join-Path $Out "README.txt") -Value $readme -Encoding UTF8

Say "Done"
Write-Host "Output: $Out"
Get-ChildItem $Out -File | Select-Object Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } } | Format-Table -AutoSize
