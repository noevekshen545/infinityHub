# =============================================================================
#  Infinity Search — полная интерактивная настройка (Windows PowerShell)
#  Запускается из setup.bat. Задаёт вопросы и ставит выбранные компоненты.
# =============================================================================
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("setup-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
try { Start-Transcript -Path $LogFile -Append | Out-Null } catch { }

function Say([string]$Text)  { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Warn([string]$Text) { Write-Host "[!] $Text" -ForegroundColor Yellow }
function Ok([string]$Text)   { Write-Host "[+] $Text" -ForegroundColor Green }
function Fail([string]$Text) { Write-Host "[x] $Text" -ForegroundColor Red }
function Hr { Write-Host ("-" * 68) -ForegroundColor DarkGray }

function Ask-YesNo([string]$Question, [bool]$Default = $true) {
    if ($Yes) { return $Default }
    $hint = "[Y/n]"
    if (-not $Default) { $hint = "[y/N]" }
    while ($true) {
        $a = Read-Host "$Question $hint"
        if ([string]::IsNullOrWhiteSpace($a)) { return $Default }
        switch ($a.Trim().ToLower()) {
            "y"   { return $true }
            "yes" { return $true }
            "да"  { return $true }
            "n"   { return $false }
            "no"  { return $false }
            "нет" { return $false }
        }
    }
}

function Ask-Choice([string]$Question, [string[]]$Options, [int]$Default = 1) {
    Write-Host "`n$Question" -ForegroundColor White
    for ($i = 0; $i -lt $Options.Count; $i++) {
        Write-Host ("  [{0}] {1}" -f ($i + 1), $Options[$i])
    }
    if ($Yes) { return $Default }
    while ($true) {
        $a = Read-Host ("Выбор [{0}]" -f $Default)
        if ([string]::IsNullOrWhiteSpace($a)) { return $Default }
        $n = 0
        if ([int]::TryParse($a, [ref]$n) -and $n -ge 1 -and $n -le $Options.Count) { return $n }
        Warn "Введите число от 1 до $($Options.Count)."
    }
}

function Ask-Text([string]$Question, [string]$Default = "") {
    if ($Yes -and $Default) { return $Default }
    $prompt = $Question
    if ($Default) { $prompt = "$Question [$Default]" }
    $a = Read-Host $prompt
    if ([string]::IsNullOrWhiteSpace($a)) { return $Default }
    return $a.Trim()
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-File([string]$Url, [string]$Dest) {
    Say "Скачивание: $Url"
    $dir = Split-Path -Parent $Dest
    if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    if (Test-Command "curl.exe") {
        & curl.exe -L --retry 3 --fail --progress-bar -o "$Dest" "$Url"
        if ($LASTEXITCODE -ne 0) { throw "curl завершился с кодом $LASTEXITCODE" }
    } else {
        Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
    }
}

function Expand-ZipTo([string]$Zip, [string]$Dest, [switch]$Flatten) {
    $tmp = Join-Path $env:TEMP ("infinity-setup-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        Expand-Archive -Path $Zip -DestinationPath $tmp -Force
        $entries = @(Get-ChildItem $tmp -Force)
        if ($Flatten -and $entries.Count -eq 1 -and $entries[0].PSIsContainer) {
            $src = $entries[0].FullName
        } else {
            $src = $tmp
        }
        New-Item -ItemType Directory -Force -Path $Dest | Out-Null
        Get-ChildItem $src -Force | Copy-Item -Destination $Dest -Recurse -Force
    } finally {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Link-Dir([string]$Link, [string]$Target) {
    if (-not (Test-Path $Target)) { Warn "Папка не найдена: $Target"; return $false }
    if (Test-Path $Link) { Warn "Уже существует: $Link"; return $true }
    $parent = Split-Path -Parent $Link
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null
    Ok "Ссылка: $Link  ->  $Target"
    return $true
}

function Invoke-Step([string]$Title, [scriptblock]$Action) {
    Say $Title
    try {
        & $Action
        Ok "$Title — готово"
        return $true
    } catch {
        Fail "$Title — ошибка: $($_.Exception.Message)"
        return $false
    }
}

# =============================================================================
# Приветствие
# =============================================================================
Clear-Host
Write-Host @"
  ___       __ _  __ _           ____            _     
 |_ _|_ __ / _| (_)/ _|_ __   ___/ ___|  ___  ___| |__  
  | || '_ \| |_| | | |_| '_ \ / _ \___ \ / _ \/ __| '_ \ 
  | || | | |  _| | |  _| | | |  __/___) |  __/ (__| | | |
 |___|_| |_|_| |_|_|_| |_| |_|\___|____/ \___|\___|_| |_|
"@ -ForegroundColor Magenta
Write-Host "  Полная настройка Infinity Search" -ForegroundColor White
Write-Host "  Папка: $Root" -ForegroundColor DarkGray
Hr

if (-not $Yes) {
    Write-Host "Скрипт задаст несколько вопросов и установит выбранные компоненты." -ForegroundColor Gray
    Write-Host "Базовые зависимости обязательны, остальное можно пропустить." -ForegroundColor Gray
    Write-Host ""
    if (-not (Ask-YesNo "Продолжить настройку?" $true)) { exit 0 }
}

$results = New-Object System.Collections.ArrayList

# =============================================================================
# Python
# =============================================================================
Say "Проверка Python 3.12+"
$py = $null
$candidates = @(
    @{ Exe = "py"; Args = @("-3.12") },
    @{ Exe = "py"; Args = @("-3.13") },
    @{ Exe = "python"; Args = @() },
    @{ Exe = "python3"; Args = @() }
)
foreach ($c in $candidates) {
    if (-not (Test-Command $c.Exe)) { continue }
    try {
        & $c.Exe @($c.Args) -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $py = $c
            break
        }
    } catch { }
}

if (-not $py) {
    Warn "Python 3.12+ не найден."
    if (Test-Command "winget") {
        if (Ask-YesNo "Установить Python 3.12 через winget?" $true) {
            & winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
            if (Test-Command "py") { $py = @{ Exe = "py"; Args = @("-3.12") } }
        }
    }
}
if (-not $py) {
    Fail "Установите Python 3.12 с https://www.python.org/downloads/ и запустите setup.bat снова."
    if (-not $Yes) { Read-Host "Нажмите Enter, чтобы выйти" }
    exit 1
}
Ok "Python: $($py.Exe) $($py.Args -join ' ')"

# =============================================================================
# venv + requirements
# =============================================================================
$venvOk = Invoke-Step "Виртуальное окружение и зависимости Python" {
    $venv = Join-Path $Root ".venv"
    if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
        & $py.Exe @($py.Args) -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw "не удалось создать .venv" }
    }
    $vp = Join-Path $venv "Scripts\python.exe"
    & $vp -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
    & $vp -m pip install -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed" }
}
[void]$results.Add([pscustomobject]@{ Component = "Python-зависимости"; Result = $venvOk })

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Fail "Без .venv дальше идти нельзя."
    if (-not $Yes) { Read-Host "Нажмите Enter, чтобы выйти" }
    exit 1
}

# =============================================================================
# Node.js / Electron
# =============================================================================
$doDesktop = Ask-YesNo "Установить Electron-оболочку и зависимости desktop?" $true
$doTorrent = Ask-YesNo "Установить торрент-помощник (Node.js)?" $true
if (($doDesktop -or $doTorrent) -and -not (Test-Command "npm")) {
    Warn "Node.js/npm не найден."
    if (Test-Command "winget" -and (Ask-YesNo "Установить Node.js LTS через winget?" $true)) {
        & winget install -e --id OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    }
}

if ($doDesktop -and (Test-Command "npm")) {
    $ok = Invoke-Step "Electron и зависимости desktop" {
        Push-Location (Join-Path $Root "desktop")
        try {
            & npm install
            if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
        } finally { Pop-Location }
    }
    [void]$results.Add([pscustomobject]@{ Component = "Electron"; Result = $ok })
}

if ($doTorrent -and (Test-Command "npm")) {
    $ok = Invoke-Step "Торрент-помощник" {
        Push-Location (Join-Path $Root "app\torrent_helper")
        try {
            & npm install
            if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
        } finally { Pop-Location }
    }
    [void]$results.Add([pscustomobject]@{ Component = "Торрент-помощник"; Result = $ok })
}

# =============================================================================
# ffmpeg
# =============================================================================
if (Ask-YesNo "Скачать ffmpeg для превью и транскода медиа?" $true) {
    $ok = Invoke-Step "ffmpeg" {
        $zip = Join-Path $env:TEMP "infinity-ffmpeg.zip"
        Get-File "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $zip
        Expand-ZipTo $zip (Join-Path $Root "app\binaries\ffmpeg")
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
    }
    [void]$results.Add([pscustomobject]@{ Component = "ffmpeg"; Result = $ok })
}

# =============================================================================
# Playwright
# =============================================================================
if (Ask-YesNo "Скачать Chromium для встроенного браузера (Playwright)?" $true) {
    $ok = Invoke-Step "Playwright Chromium" {
        & $VenvPy -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw "playwright install failed" }
    }
    [void]$results.Add([pscustomobject]@{ Component = "Playwright Chromium"; Result = $ok })
}

# =============================================================================
# Whisper
# =============================================================================
$whisperChoice = Ask-Choice "Скачать модель распознавания речи Whisper?" @(
    "Пропустить",
    "tiny (~75 МБ)",
    "base (~140 МБ)",
    "small (~460 МБ, рекомендуется)",
    "medium (~1.5 ГБ)",
    "large-v3 (~3 ГБ)",
    "large-v3-turbo (~1.5 ГБ)"
) 4

if ($whisperChoice -gt 1) {
    $map = @("", "tiny", "base", "small", "medium", "large", "turbo")
    $model = $map[$whisperChoice]
    $ru = Ask-YesNo "Использовать русскоязычную версию модели?" $true
    $ok = Invoke-Step "Whisper $model" {
        $env:INFINITY_STT_MODELS_DIR = Join-Path $Root "models\whisper"
        $pyArgs = @((Join-Path $Root "app\download_whisper.py"), "--model", $model)
        if ($ru -and ($model -eq "base" -or $model -eq "small")) { $pyArgs += "--ru" }
        & $VenvPy @pyArgs
        if ($LASTEXITCODE -ne 0) { throw "download_whisper failed" }
    }
    [void]$results.Add([pscustomobject]@{ Component = "Whisper $model"; Result = $ok })
}

# =============================================================================
# Silero TTS
# =============================================================================
if (Ask-YesNo "Скачать Silero TTS (русский голос)?" $true) {
    $ok = Invoke-Step "Silero TTS" {
        $tmp = Join-Path $env:TEMP ("infinity-silero-" + [guid]::NewGuid().ToString("N") + ".py")
        @'
import os, torch
root = os.environ["INFINITY_ROOT"]
hub = os.path.join(root, "models", "silero", "silero_hub")
os.makedirs(hub, exist_ok=True)
torch.hub.set_dir(hub)
torch.hub.load("snakers4/silero-models", model="silero_tts", language="ru", speaker="v5_5_ru", trust_repo=True)
print("SILERO_OK")
'@ | Set-Content -Path $tmp -Encoding UTF8
        $env:INFINITY_ROOT = $Root
        & $VenvPy $tmp
        $code = $LASTEXITCODE
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        if ($code -ne 0) { throw "torch.hub load failed" }
    }
    [void]$results.Add([pscustomobject]@{ Component = "Silero TTS"; Result = $ok })
}

# =============================================================================
# Piper voice
# =============================================================================
$piperChoice = Ask-Choice "Скачать голос Piper?" @(
    "Пропустить",
    "Руслан (мужской)",
    "Ирина (женский)",
    "Дмитрий (мужской)"
) 1

if ($piperChoice -gt 1) {
    $voices = @{
        2 = @{ Id = "ru_RU-ruslan-medium"; Url = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/ruslan/medium/ru_RU-ruslan-medium.onnx" }
        3 = @{ Id = "ru_RU-irina-medium";  Url = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx" }
        4 = @{ Id = "ru_RU-dmitri-medium"; Url = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx" }
    }
    $v = $voices[$piperChoice]
    $ok = Invoke-Step "Голос Piper $($v.Id)" {
        $dir = Join-Path $Root "models\piper"
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Get-File $v.Url (Join-Path $dir ($v.Id + ".onnx"))
        Get-File ($v.Url + ".json") (Join-Path $dir ($v.Id + ".onnx.json"))
    }
    [void]$results.Add([pscustomobject]@{ Component = "Piper $($v.Id)"; Result = $ok })
}

# =============================================================================
# Qdrant / embeddings
# =============================================================================
if (Ask-YesNo "Скачать модель эмбеддингов для Qdrant и поиска по смыслу?" $true) {
    $ok = Invoke-Step "Qdrant / embeddings" {
        & $VenvPy (Join-Path $Root "app\download_fastembed_model.py")
        if ($LASTEXITCODE -ne 0) { throw "download_fastembed_model failed" }
    }
    [void]$results.Add([pscustomobject]@{ Component = "Qdrant / embeddings"; Result = $ok })
}

# =============================================================================
# Hermes
# =============================================================================
$hasHermes = (Test-Path (Join-Path $Root "app\hermes_module\backend")) -and (Test-Path (Join-Path $Root "app\hermes_module\frontend"))
if ($hasHermes -and (Ask-YesNo "Подготовить Hermes Agent (отдельный Python 3.11-3.13)?" $true)) {
    $ok = Invoke-Step "Hermes Agent venv" {
        $hpy = $null
        foreach ($c in @(
            @{ Exe = "py"; Args = @("-3.13") },
            @{ Exe = "py"; Args = @("-3.12") },
            @{ Exe = "py"; Args = @("-3.11") },
            @{ Exe = "python3.13"; Args = @() },
            @{ Exe = "python3.12"; Args = @() },
            @{ Exe = "python3.11"; Args = @() }
        )) {
            if (-not (Test-Command $c.Exe)) { continue }
            try {
                & $c.Exe @($c.Args) -c "import sys; raise SystemExit(0)" 2>$null
                if ($LASTEXITCODE -eq 0) { $hpy = $c; break }
            } catch { }
        }
        if (-not $hpy) { throw "Python 3.11-3.13 не найден. Установите Python 3.13 и повторите." }
        $hv = Join-Path $Root "app\hermes_module\.venv"
        if (-not (Test-Path (Join-Path $hv "Scripts\python.exe"))) {
            & $hpy.Exe @($hpy.Args) -m venv $hv
            if ($LASTEXITCODE -ne 0) { throw "не удалось создать venv Hermes" }
        }
        $hp = Join-Path $hv "Scripts\python.exe"
        & $hp -m pip install --upgrade pip
        & $hp -m pip install -r (Join-Path $Root "app\hermes_module\requirements-module.txt")
        if ($LASTEXITCODE -ne 0) { throw "pip install requirements-module.txt failed" }
    }
    [void]$results.Add([pscustomobject]@{ Component = "Hermes Agent"; Result = $ok })
}

# =============================================================================
# llama.cpp engine
# =============================================================================
$engineChoice = Ask-Choice "Установить движок llama.cpp для локальных моделей?" @(
    "Пропустить",
    "Скачать по ссылке (Release/GitHub)",
    "Скопировать из локальной папки"
) 1

if ($engineChoice -eq 2) {
    $url = Ask-Text "Ссылка на engine.zip" "https://github.com/noevekshen545/infinitygihub/releases/download/v1/engine.zip"
    if ($url) {
        $ok = Invoke-Step "Движок llama.cpp" {
            $zip = Join-Path $env:TEMP "infinity-engine.zip"
            Get-File $url $zip
            Expand-ZipTo $zip (Join-Path $Root "engine") -Flatten
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
        }
        [void]$results.Add([pscustomobject]@{ Component = "llama.cpp engine"; Result = $ok })
    }
} elseif ($engineChoice -eq 3) {
    $path = Ask-Text "Путь к папке с llama-server.exe"
    if ($path -and (Test-Path $path)) {
        $ok = Invoke-Step "Движок llama.cpp" {
            New-Item -ItemType Directory -Force -Path (Join-Path $Root "engine") | Out-Null
            Get-ChildItem $path -Force | Copy-Item -Destination (Join-Path $Root "engine") -Recurse -Force
        }
        [void]$results.Add([pscustomobject]@{ Component = "llama.cpp engine"; Result = $ok })
    }
}

# =============================================================================
# GGUF model
# =============================================================================
$modelChoice = Ask-Choice "Скачать модель для локального помощника?" @(
    "Пропустить",
    "Qwen2.5-3B-Instruct Q4_K_M (~1.9 ГБ)",
    "Gemma-2-2B-it Q4_K_M (~1.7 ГБ)",
    "Llama-3.2-3B-Instruct Q4_K_M (~2.0 ГБ)",
    "Qwen2.5-7B-Instruct Q4_K_M (~4.7 ГБ)",
    "Свой прямой URL"
) 1

if ($modelChoice -eq 6) {
    $url = Ask-Text "Прямая ссылка на .gguf"
    $name = Ask-Text "Имя файла" "model.gguf"
    if ($url) {
        $ok = Invoke-Step "Модель $name" {
            $dir = Join-Path $Root "models\gguf"
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            Get-File $url (Join-Path $dir $name)
        }
        [void]$results.Add([pscustomobject]@{ Component = "Модель $name"; Result = $ok })
    }
} elseif ($modelChoice -gt 1) {
    $presets = @{
        2 = @{ Repo = "bartowski/Qwen2.5-3B-Instruct-GGUF"; File = "Qwen2.5-3B-Instruct-Q4_K_M.gguf" }
        3 = @{ Repo = "bartowski/gemma-2-2b-it-GGUF"; File = "gemma-2-2b-it-Q4_K_M.gguf" }
        4 = @{ Repo = "bartowski/Llama-3.2-3B-Instruct-GGUF"; File = "Llama-3.2-3B-Instruct-Q4_K_M.gguf" }
        5 = @{ Repo = "bartowski/Qwen2.5-7B-Instruct-GGUF"; File = "Qwen2.5-7B-Instruct-Q4_K_M.gguf" }
    }
    $m = $presets[$modelChoice]
    $ok = Invoke-Step "Модель $($m.File)" {
        $tmp = Join-Path $env:TEMP ("infinity-hf-" + [guid]::NewGuid().ToString("N") + ".py")
        @'
import os, sys
from huggingface_hub import hf_hub_download
repo, fn, dest = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(dest, exist_ok=True)
p = hf_hub_download(repo_id=repo, filename=fn, local_dir=dest)
print("MODEL_OK", p)
'@ | Set-Content -Path $tmp -Encoding UTF8
        & $VenvPy $tmp $m.Repo $m.File (Join-Path $Root "models\gguf")
        $code = $LASTEXITCODE
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        if ($code -ne 0) { throw "huggingface download failed" }
    }
    [void]$results.Add([pscustomobject]@{ Component = "Модель $($m.File)"; Result = $ok })
}

# =============================================================================
# VLC / Calibre (portable папки)
# =============================================================================
if (Ask-YesNo "Подключить VLC (portable или установленный)?" $false) {
    $vlc = Ask-Text "Путь к папке VLC (где лежит vlc.exe) или Enter — пропустить"
    if ($vlc -and (Test-Path $vlc)) {
        $ok = Link-Dir (Join-Path $Root "app\vlc\portable") $vlc
        [void]$results.Add([pscustomobject]@{ Component = "VLC"; Result = $ok })
    } elseif (Ask-YesNo "Установить VLC через winget?" $false) {
        if (Test-Command "winget") {
            & winget install -e --id VideoLAN.VLC --silent --accept-package-agreements --accept-source-agreements
            [void]$results.Add([pscustomobject]@{ Component = "VLC (winget)"; Result = ($LASTEXITCODE -eq 0) })
        }
    }
}

if (Ask-YesNo "Подключить Calibre Portable для конвертации книг?" $false) {
    $cal = Ask-Text "Путь к папке Calibre (где лежит calibre.exe) или Enter — пропустить"
    if ($cal -and (Test-Path $cal)) {
        $ok = Link-Dir (Join-Path $Root "calibry\Calibre") $cal
        [void]$results.Add([pscustomobject]@{ Component = "Calibre"; Result = $ok })
    }
}

# =============================================================================
# ComfyUI / defaultapps (ссылки на существующие папки)
# =============================================================================
if (Ask-YesNo "Подключить ComfyUI (папка ComfyUI_windows_portable)?" $false) {
    $comfy = Ask-Text "Путь к ComfyUI_windows_portable или Enter — пропустить"
    if ($comfy -and (Test-Path $comfy)) {
        $ok = Link-Dir (Join-Path $Root "ComfyUI_windows_portable") $comfy
        [void]$results.Add([pscustomobject]@{ Component = "ComfyUI"; Result = $ok })
    }
}

if (Ask-YesNo "Подключить каталог приложений defaultapps?" $false) {
    $apps = Ask-Text "Путь к папке defaultapps или Enter — пропустить"
    if ($apps -and (Test-Path $apps)) {
        $ok = Link-Dir (Join-Path $Root "defaultapps") $apps
        [void]$results.Add([pscustomobject]@{ Component = "defaultapps"; Result = $ok })
    }
}

# =============================================================================
# Рабочие папки и ярлык
# =============================================================================
foreach ($d in @("data", "logs", "apps", "models\gguf", "models\piper", "models\whisper", "models\silero", "engine")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null
}

if (Ask-YesNo "Создать ярлык Infinity Search на рабочем столе?" $true) {
    try {
        $ws = New-Object -ComObject WScript.Shell
        $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Infinity Search.lnk"
        $sc = $ws.CreateShortcut($lnk)
        $sc.TargetPath = Join-Path $Root "run-dev.bat"
        $sc.WorkingDirectory = $Root
        $sc.IconLocation = Join-Path $Root "infinity.ico"
        $sc.Description = "Infinity Search"
        $sc.Save()
        Ok "Ярлык: $lnk"
    } catch {
        Warn "Не удалось создать ярлык: $($_.Exception.Message)"
    }
}

# =============================================================================
# Итог
# =============================================================================
Hr
Say "Готово"
if ($results.Count -gt 0) {
    $results | Format-Table -AutoSize | Out-String | Write-Host
}
Write-Host "Журнал: $LogFile" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Запуск: run-dev.bat  ->  http://127.0.0.1:8281" -ForegroundColor White
Write-Host "Полная документация: docs\README.md" -ForegroundColor DarkGray

if (-not $NoLaunch) {
    if (Ask-YesNo "Запустить Infinity Search сейчас?" $true) {
        Start-Process -FilePath (Join-Path $Root "run-dev.bat") -WorkingDirectory $Root
    }
}

try { Stop-Transcript | Out-Null } catch { }
if (-not $Yes) { Read-Host "Нажмите Enter, чтобы закрыть" }
