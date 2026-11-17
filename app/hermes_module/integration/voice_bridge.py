"""
Голос агента → голос системы.

У Hermes свой голос на torch (Silero TTS + faster-whisper), но в Infinity
Search TTS/STT уже установлены и работают легче:

    GET  /api/tts?text=...            — silero / piper / qwen3 / custom
    POST /api/stt/transcribe          — multipart file=<аудио>

Hermes умеет принимать конфигурацию движка в заголовке
`X-Voice-Engine-stt` / `X-Voice-Engine-tts` (JSON). Фронт агента эти
заголовки не шлёт — значит, подставляем их мы на стороне прокси.

Но у проекта эндпоинты другой формы (GET с query для TTS, multipart
с полем file для STT), поэтому добавляем два OpenAI-совместимых
адаптера и указываем агенту на них:

    POST /hermes/api/voice/tts   (multipart: input, voice)  -> audio
    POST /hermes/api/voice/stt   (multipart: file)           -> {"text": ...}

Адаптеры живут в основном приложении (bridge.py), здесь — только сборка
конфигурации и проверка, что голос вообще доступен.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass


def _tts_url():
    # Адаптер в основном приложении: принимает multipart, отдаёт wav.
    return f"http://127.0.0.1:{_main_port()}{_mount()}/api/voice/tts"


def _stt_url():
    return f"http://127.0.0.1:{_main_port()}{_mount()}/api/voice/stt"


def _main_port():
    from . import config
    return config.main_port()


def _mount():
    from . import config
    return config.MOUNT_PATH.rstrip("/")


def tts_config():
    """Конфигурация TTS-движка для заголовка X-Voice-Engine-tts."""
    return {
        "engine": "custom",
        "url": _tts_url(),
        "model": "tts-1",
        "voice": "default",
        "format": "wav",
        "timeout": 120,
    }


def stt_config():
    """Конфигурация STT-движка для заголовка X-Voice-Engine-stt."""
    return {
        "engine": "custom",
        "url": _stt_url(),
        "model": "whisper-1",
        "language": "ru",
        "timeout": 180,
    }


def headers_for(request):
    """Заголовки, которые надо добавить к проксируемому запросу.

    Не перетираем то, что прислал фронт агента (пользователь может
    выбрать свой движок в настройках UI агента).
    """
    out = {}
    try:
        incoming = request.headers
    except Exception:
        swallow.note('voice_bridge.headers_for:L90')
        incoming = {}
    if not incoming.get("X-Voice-Engine-tts"):
        out["X-Voice-Engine-tts"] = json.dumps(tts_config(), ensure_ascii=False)
    if not incoming.get("X-Voice-Engine-stt"):
        out["X-Voice-Engine-stt"] = json.dumps(stt_config(), ensure_ascii=False)
    return out


def is_voice_path(path):
    return "/api/voice/" in path


def system_voice_available():
    """Диагностика: отвечают ли /api/tts и /api/stt основного приложения."""
    from . import config
    base = f"http://127.0.0.1:{config.main_port()}"
    # Текст в запросе должен быть percent-encoded: кириллица в строке запроса
    # роняла urllib ('ascii' codec can't encode...), и голос помечался
    # недоступным, хотя /api/tts работает.
    probe = urllib.parse.quote("тест")
    out = {}
    for name, url in (("tts", base + "/api/tts?engine=silero&text=" + probe),
                      ("stt_status", base + "/api/stt/status")):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as r:
                out[name] = {"ok": 200 <= r.status < 400, "status": r.status}
        except urllib.error.HTTPError as e:
            swallow.note('voice_bridge.system_voice_available:L119', e)
            out[name] = {"ok": False, "status": e.code}
        except Exception as e:
            swallow.note('voice_bridge.system_voice_available:L122', e)
            out[name] = {"ok": False, "error": str(e)[:120]}
    return out
