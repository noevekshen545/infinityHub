<#
.SYNOPSIS
  Infinity Search — bootstrap for Windows source checkouts.

.DESCRIPTION
  Installs Python and Electron dependencies, creates runtime directories and
  can fetch optional heavy engines that are not stored in Git.

.EXAMPLE
  .\scripts\bootstrap.ps1

.EXAMPLE
  .\scripts\bootstrap.ps1 -All

.EXAMPLE
  .\scripts\bootstrap.ps1 -EngineUrl "https://github.com/<owner>/<repo>/releases/download/v1/engine.zip"

.EXAMPLE
  .\scripts\bootstrap.ps1 -HermesFrom "D:\InfinitySearch\app\hermes_module"
#>
[CmdletBinding()]
param(
    [switch]$WithPlaywright,
    [switch]$WithFfmpeg,
    [switch]$WithHermes,
    [string]$EngineUrl = $env:INFINITY_ENGINE_URL,
    [string]$EngineFrom = $env:INFINITY_ENGINE_FROM,
    [string]$HermesUrl = $env:INFINITY_HERMES_URL,
    [string]$HermesFrom = $env:INFINITY_HERMES_FROM,
    [switch]$NoDesktop,
    [switch]$All
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if ($All) {
    $WithPlaywright = $true
    $WithFfmpeg = $true
    $WithHermes = $true
}

function Say([string]$Text)  { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Warn([string]$Text) { Write-Host "[!] $Text" -ForegroundColor Yellow }
function Die([string]$Text)  { Write-Host "[x] $Text" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# Python 3.12+
# ---------------------------------------------------------------------------
Say "Looking for Python 3.12+"
$PythonExe = $null
$PythonArgs = @()

$probes = @(
    @{ Exe = "py";        Args = @("-3.13"); Label = "py -3.13" },
    @{ Exe = "py";        Args = @("-3.12"); Label = "py -3.12" },
    @{ Exe = "python";    Args = @();        Label = "python" },
    @{ Exe = "python3";   Args = @();        Label = "python3" }
)
foreach ($p in $probes) {
    if (-not (Get-Command $p.Exe -ErrorAction SilentlyContinue)) { continue }
    try {
        & $p.Exe @($p.Args) -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonExe = $p.Exe
            $PythonArgs = $p.Args
            break
        }
    } catch { }
}
if (-not $PythonExe) { Die "Python 3.12+ not found. Install it from https://python.org and re-run." }
& $PythonExe @PythonArgs --version

# ---------------------------------------------------------------------------
# Virtualenv + pip dependencies
# ---------------------------------------------------------------------------
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Say "Creating .venv"
    & $PythonExe @PythonArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Die "venv creation failed" }
}
if (-not (Test-Path $VenvPy)) { Die "venv interpreter not found: $VenvPy" }

Say "Installing Python dependencies (this can take a while: torch/transformers are large)"
& $VenvPy -m pip install --upgrade pip
& $VenvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Die "pip install failed" }

# ---------------------------------------------------------------------------
# Runtime directories
# ---------------------------------------------------------------------------
Say "Creating runtime directories"
foreach ($d in @("data", "logs", "apps", "models\gguf", "models\piper", "models\whisper", "engine")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null
}

# ---------------------------------------------------------------------------
# Electron shell
# ---------------------------------------------------------------------------
if (-not $NoDesktop) {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Say "Installing Electron dependencies (desktop\)"
        Push-Location (Join-Path $Root "desktop")
        try {
            & npm install
            if ($LASTEXITCODE -ne 0) { Warn "npm install failed — web UI still works" }
        } finally { Pop-Location }

        if (Test-Path (Join-Path $Root "app\torrent_helper\package.json")) {
            Say "Installing torrent helper dependencies (app\torrent_helper\)"
            Push-Location (Join-Path $Root "app\torrent_helper")
            try { & npm install } finally { Pop-Location }
        }
    } else {
        Warn "npm not found — desktop\ dependencies skipped (web UI still works)"
    }
}

# ---------------------------------------------------------------------------
# Playwright
# ---------------------------------------------------------------------------
if ($WithPlaywright) {
    Say "Installing Playwright Chromium"
    & $VenvPy -m playwright install chromium
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Download([string]$Url, [string]$Dest) {
    Say "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
}

function Expand-Zip([string]$Zip, [string]$Dest, [switch]$Flatten) {
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    $tmp = Join-Path $env:TEMP ("infinity-unzip-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        Expand-Archive -Path $Zip -DestinationPath $tmp -Force
        $entries = @(Get-ChildItem $tmp -Force)
        if ($Flatten -and $entries.Count -eq 1 -and $entries[0].PSIsContainer) {
            $src = $entries[0].FullName
        } else {
            $src = $tmp
        }
        Get-ChildItem $src -Force | Copy-Item -Destination $Dest -Recurse -Force
    } finally {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------
if ($WithFfmpeg) {
    Say "Installing ffmpeg into app\binaries\ffmpeg"
    $zip = Join-Path $env:TEMP "infinity-ffmpeg.zip"
    Download "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $zip
    Expand-Zip $zip (Join-Path $Root "app\binaries\ffmpeg")
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    Warn "ffmpeg extracted; restart the app so PATH picks it up."
}

# ---------------------------------------------------------------------------
# llama.cpp engine (optional, custom build required)
# ---------------------------------------------------------------------------
if ($EngineFrom) {
    if (-not (Test-Path $EngineFrom)) { Die "engine source not found: $EngineFrom" }
    Say "Copying engine from $EngineFrom"
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "engine") | Out-Null
    Get-ChildItem $EngineFrom -Force | Copy-Item -Destination (Join-Path $Root "engine") -Recurse -Force
} elseif ($EngineUrl) {
    Say "Downloading engine into engine\"
    $zip = Join-Path $env:TEMP "infinity-engine.zip"
    Download $EngineUrl $zip
    Expand-Zip $zip (Join-Path $Root "engine") -Flatten
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
} else {
    Warn "engine\ is empty. The app starts, but local AI is disabled until you add"
    Warn "llama-server.exe (see docs/EXTERNAL.md). Use -EngineUrl/-EngineFrom."
}

# ---------------------------------------------------------------------------
# Hermes Agent backend/frontend (optional)
# ---------------------------------------------------------------------------
$HermesPresent = $false
if ($HermesFrom) {
    if (-not (Test-Path $HermesFrom)) { Die "Hermes source not found: $HermesFrom" }
    Say "Copying Hermes backend/frontend from $HermesFrom"
    $dst = Join-Path $Root "app\hermes_module"
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    foreach ($name in @("backend", "frontend")) {
        $src = Join-Path $HermesFrom $name
        if (Test-Path $src) {
            Get-ChildItem $src -Force | Copy-Item -Destination (Join-Path $dst $name) -Recurse -Force
        }
    }
    $HermesPresent = $true
} elseif ($HermesUrl) {
    Say "Downloading Hermes backend/frontend"
    $zip = Join-Path $env:TEMP "infinity-hermes.zip"
    Download $HermesUrl $zip
    Expand-Zip $zip (Join-Path $Root "app\hermes_module") -Flatten
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    $HermesPresent = $true
} elseif ((Test-Path "app\hermes_module\backend") -and (Test-Path "app\hermes_module\frontend")) {
    $HermesPresent = $true
} else {
    Warn "Hermes backend/frontend are not present. The /hermes section will show"
    Warn "diagnostics instead of the agent (see docs/EXTERNAL.md)."
}

if ($HermesPresent -and $WithHermes) {
    Say "Preparing Hermes virtualenv (Python 3.11-3.13)"
    $HermesPy = $null
    $HermesArgs = @()
    foreach ($p in @(
        @{ Exe = "py"; Args = @("-3.13") },
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "py"; Args = @("-3.11") },
        @{ Exe = "python3.13"; Args = @() },
        @{ Exe = "python3.12"; Args = @() },
        @{ Exe = "python3.11"; Args = @() }
    )) {
        if (-not (Get-Command $p.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            & $p.Exe @($p.Args) -c "import sys; raise SystemExit(0)" 2>$null
            if ($LASTEXITCODE -eq 0) { $HermesPy = $p.Exe; $HermesArgs = $p.Args; break }
        } catch { }
    }
    if ($HermesPy) {
        & $HermesPy @HermesArgs -m venv (Join-Path $Root "app\hermes_module\.venv")
        $hp = Join-Path $Root "app\hermes_module\.venv\Scripts\python.exe"
        & $hp -m pip install --upgrade pip
        & $hp -m pip install -r (Join-Path $Root "app\hermes_module\requirements-module.txt")
    } else {
        Warn "Python 3.11-3.13 not found. Install it and run:"
        Warn "  py -3.13 -m venv app\hermes_module\.venv"
        Warn "  app\hermes_module\.venv\Scripts\python.exe -m pip install -r app\hermes_module\requirements-module.txt"
    }
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Say "Bootstrap complete"
Write-Host @"

Next steps:
  1. run-dev.bat
  2. Open http://127.0.0.1:8281

Optional components and exact paths: docs/EXTERNAL.md
"@
