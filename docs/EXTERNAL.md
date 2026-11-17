# Внешние компоненты

Исходники Infinity Search лежат в репозитории. Тяжёлые бинарники, модели и сторонние
движки — отдельно: GitHub не принимает файлы больше 100 МБ, а полный набор занимает
десятки гигабайт.

Приложение **запускается и без них**. Модули, которым не хватает компонента, показывают
понятное сообщение, а остальное продолжает работать.

## Что уже есть в репозитории

| Компонент | Путь | Статус |
|---|---|---|
| Код приложения | `app/` | в репозитории |
| Hermes Agent | `app/hermes_module/backend`, `frontend` | в репозитории, без встроенных skill-паков |
| Интеграция Hermes | `app/hermes_module/integration` | в репозитории |
| 3D-аватары | `app/avatars` | в репозитории |
| Avi | `app/bin/avi.exe`, `modules/avi` | в репозитории |
| Electron-оболочка | `desktop/` | в репозитории, зависимости через npm |
| Rust-крейт Avi | `modules/avi/` | в репозитории |

## Что скачивается или добавляется отдельно

| Компонент | Путь | ~Размер | Нужен для | Как получить |
|---|---|---:|---|---|
| llama.cpp engine | `engine/` | 675 МБ | локальные модели | release-ассет `engine.zip` |
| Модели | `models/` | 9.8 ГБ | чат, распознавание, синтез, эмбеддинги | скачать вручную |
| ffmpeg / ffprobe | `app/binaries/ffmpeg/` | 314 МБ | превью и транскод медиа | `--with-ffmpeg` |
| Playwright | `ms-playwright/` | 1.4 ГБ | встроенный браузер | `--with-playwright` |
| VLC | `app/vlc/portable/` | 181 МБ | воспроизведение видео | установить VLC |
| Podman | `app/binaries/podman/` | 132 МБ | Docker-модуль | установить Podman |
| Tor | `app/binaries/tor/` | 23 МБ | Tor-модуль | положить `tor.exe` |
| Calibre | `calibry/Calibre/` | 633 МБ | конвертация книг | Calibre Portable |
| ComfyUI | `ComfyUI_windows_portable/` | 6 ГБ | генерация изображений | portable-сборка |
| Каталог приложений | `defaultapps/` | 2.2 ГБ | встроенный каталог | ассеты приложений |
| Skill-паки | `agent/` | 902 МБ | расширенные навыки помощника | сторонние репозитории |
| Портативный Python | `python/` | 328 МБ | только frozen-сборка | не нужен для исходников |
| PyInstaller-сборка | `_internal/`, `InfinitySearch.exe` | 1.8 ГБ | готовый portable-релиз | собирается отдельно |

---

## Движок llama.cpp

`app/aiengine/api.py` — код управления движком, он есть в репозитории. Отсутствует
только бинарник `engine/llama-server.exe` и DLL.

Проект использует **кастомную сборку** llama.cpp: в командной строке есть флаги,
которых нет в апстриме (`--fit`, `--spec-type draft-mtp`, `--reasoning`, `--cache-ram`,
`--ctx-checkpoints`, `--sampler-seq` и другие). Обычный релиз `ggml-org/llama.cpp`
может запуститься, но часть режимов работать не будет.

### Рекомендуемый способ

1. Соберите или возьмите рабочую сборку движка.
2. Упакуйте её в `engine.zip`.
3. Прикрепите архив к GitHub Release.
4. Пользователи скачивают его через bootstrap:

```bash
./scripts/bootstrap.sh --engine-url <url-до-engine.zip>
```

Или из локальной сборки:

```bash
./scripts/bootstrap.sh --engine-from /path/to/InfinitySearch/engine
```

Проверка: `engine/llama-server.exe --version`.

### Почему не в Git

Файл `cublasLt64_13.dll` занимает около 439 МБ — это больше лимита GitHub в 100 МБ на
файл. Git LFS не подходит: бесплатного места хватит ненадолго, а пользователям
придётся ставить LFS.

---

## Модели

Модели не распространяются с репозиторием. Ожидаемая структура:

```
models/
├─ gguf/       # *.gguf для llama.cpp
├─ whisper/    # faster-whisper
├─ piper/      # голоса Piper
└─ silero/     # Silero TTS
```

Подойдёт любой gguf-файл: Qwen, Gemma, Llama, Mistral и другие. Выбор модели — в
настройках «Центр моделей». Без модели можно подключить внешний OpenAI-совместимый
провайдер в `app/config.json` или в интерфейсе.

---

## Hermes Agent

Hermes уже лежит в репозитории: `app/hermes_module/backend` и `frontend`. Для запуска
нужно отдельное окружение Python 3.11–3.13:

```bash
./scripts/bootstrap.sh --with-hermes
```

Скрипт создаст `app/hermes_module/.venv` и установит
`app/hermes_module/requirements-module.txt`.

Если Python 3.13 нет:

```bat
py -3.13 -m venv app\hermes_module\.venv
app\hermes_module\.venv\Scripts\python.exe -m pip install -r app\hermes_module\requirements-module.txt
```

Без окружения раздел `/hermes` покажет диагностику, а остальное приложение продолжит
работать.

Лицензия Hermes — сторонняя (см. `app/hermes_module/backend/LICENSE`), она не
покрывается MIT-лицензией этого репозитория.

---

## ffmpeg

```bash
./scripts/bootstrap.sh --with-ffmpeg
```

Скрипт скачивает `ffmpeg-release-essentials.zip` с gyan.dev и распаковывает в
`app/binaries/ffmpeg/`. `app/binaries_env.py` сам добавляет найденный `bin/` в `PATH`.

Можно положить вручную:

```
app/binaries/ffmpeg/<папка>/bin/ffmpeg.exe
app/binaries/ffmpeg/<папка>/bin/ffprobe.exe
```

---

## Playwright

```bash
./scripts/bootstrap.sh --with-playwright
```

Скачивает Chromium для встроенного браузера. Без него браузерные функции покажут
сообщение об отсутствии браузера.

---

## VLC, Calibre, ComfyUI, defaultapps

Эти компоненты не обязательны для запуска и не скачиваются автоматически:

- **VLC** — установите обычный VLC или положите portable в `app/vlc/portable/`;
- **Calibre** — Calibre Portable в `calibry/Calibre/`;
- **ComfyUI** — portable-сборка в `ComfyUI_windows_portable/`;
- **defaultapps** — каталог приложений в `defaultapps/` (в публикации исключён).

---

## Сборка release-ассетов

Скрипт `scripts/pack-assets.ps1` собирает архивы из локальной портативной сборки:

```powershell
.\scripts\pack-assets.ps1 -Source "D:\InfinitySearch"
```

Результат:

```
release-assets/
├─ engine.zip
├─ hermes.zip
└─ README.txt
```

Прикрепите архивы к GitHub Release. Ссылки на них передавайте в bootstrap:

```bash
./scripts/bootstrap.sh \
  --engine-url https://github.com/noevekshen545/infinityHub/releases/download/v1/engine.zip
```

---

## Лицензии сторонних компонентов

| Компонент | Лицензия |
|---|---|
| Hermes Agent | см. `app/hermes_module/backend/LICENSE` |
| llama.cpp | MIT |
| ffmpeg | LGPL/GPL (зависит от сборки) |
| VLC | GPL |
| Calibre | GPL |
| ComfyUI | GPL |
| Playwright | Apache-2.0 |
| Electron | MIT |

Исходный код Infinity Search распространяется по MIT (см. `LICENSE`).
