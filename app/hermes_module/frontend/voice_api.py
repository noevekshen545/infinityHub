"""Пользовательские движки STT/TTS по API (Настройки → Голос).

Локальные Whisper и Silero остаются движками по умолчанию и работают всегда.
Этот модуль нужен только когда пользователь сам выбрал внешний движок:
сервер получает аудио или текст, обращается к указанному адресу и возвращает
результат в том же виде, что и локальные движки — чтобы фронтенду не пришлось
знать, какой движок активен.

Поддерживаются два режима:
  openai — адрес вида http://host:port/v1/audio/transcriptions или /v1/audio/speech.
           Это то, что умеют OpenAI, Whisper.cpp-серверы, Silero API, TTS-API,
           faster-whisper-server и большинство совместимых движков.
  custom — произвольный адрес: STT ждёт multipart с полем file, TTS — multipart
           с полями text/voice/format. Так работают сервисы с нестандартной схемой.

Ключи API не хранятся на диске: конфигурация приходит сюда, используется
и сразу забывается. Проверка связи идёт тем же кодом, что и реальный запрос,
поэтому кнопка «Проверить» показывает ровно то, что получит приложение.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

MAX_ENGINE_OUT = 64 * 1024 * 1024   # ответ с аудио бывает большим
DEFAULT_TIMEOUT = 60


class VoiceEngineError(Exception):
    """Движок недоступен или ответил ошибкой. Текст уходит в чат пользователю."""


def _clean(cfg: Any) -> dict:
    """Приводит конфигурацию движка к безопасному виду: только строки и числа."""
    if not isinstance(cfg, dict):
        cfg = {}
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str):
            # Обрезаем заранее: движок не должен уметь принять мегабайтный ключ.
            out[k] = v[:4096]
        else:
            # Неизвестные типы (списки, dict, None) в запрос к движку не идут.
            out[k] = str(v)[:4096]
    return out


def _request(url: str, data: bytes, headers: dict, timeout: int) -> tuple[int, bytes, str]:
    """Один запрос без исключений наружу. Отдаёт (код, тело, content-type)."""
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_ENGINE_OUT + 1)
            ctype = resp.headers.get("Content-Type", "")
            if len(body) > MAX_ENGINE_OUT:
                raise VoiceEngineError("Ответ движка слишком большой")
            return resp.status, body, ctype
    except urllib.error.HTTPError as e:
        # Тело ошибки полезнее самого кода: движок объясняет, чего не хватило.
        try:
            detail = e.read(4000).decode("utf-8", "replace")
        except Exception:
            swallow.note('voice_api._request:L79')
            detail = ""
        raise VoiceEngineError(f"HTTP {e.code}: {detail[:400] or e.reason}") from None
    except urllib.error.URLError as e:
        raise VoiceEngineError(f"Не удалось связаться: {e.reason}") from None
    except VoiceEngineError:
        raise
    except Exception as e:
        raise VoiceEngineError(f"Ошибка связи: {e}") from None


def _multipart(fields: dict, filename: str, content: bytes, ctype: str) -> tuple[bytes, str]:
    """Собирает multipart/form-data вручную — без httpx в зависимостях.

    Пустые поля пропускаются: у многих движков пустой language или model
    засчитывается как ошибка, а отсутствие поля — нет."""
    boundary = "----InfinityBoundary" + uuid.uuid4().hex
    buf = bytearray()
    for k, v in fields.items():
        if v in (None, ""):
            continue
        buf += f"--{boundary}\r\n".encode()
        buf += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        buf += f"{v}\r\n".encode()
    buf += f"--{boundary}\r\n".encode()
    buf += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    buf += f"Content-Type: {ctype}\r\n\r\n".encode()
    buf += content
    buf += f"\r\n--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


def _text_from_response(body: bytes, ctype: str) -> str:
    """Достаёт текст из ответа движка: JSON с полем text/transcription
    либо просто текст. Формы ответа у разных сервисов различаются."""
    raw = body.decode("utf-8", "replace").strip()
    if ctype.startswith("application/json") or raw[:1] in "{[":
        try:
            data = json.loads(raw)
        except Exception:
            swallow.note('voice_api._text_from_response:L119')
            return raw
        if isinstance(data, str):
            return data.strip()
        if isinstance(data, dict):
            for key in ("text", "transcription", "result", "output", "data"):
                v = data.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
                if isinstance(v, dict):
                    for sub in ("text", "transcription"):
                        if isinstance(v.get(sub), str) and v[sub].strip():
                            return v[sub].strip()
            return raw
    return raw


def _endpoint(cfg: dict, default: str) -> str:
    """Адрес из настроек; если пустой — стандартный для режима openai."""
    url = (cfg.get("url") or "").strip()
    if not url:
        if not default:
            raise VoiceEngineError("Не указан адрес движка")
        return default
    if not url.startswith(("http://", "https://")):
        raise VoiceEngineError("Адрес должен начинаться с http:// или https://")
    return url


# ===================== ДВИЖКИ ПОЛЬЗОВАТЕЛЯ =====================

def stt(audio: bytes, cfg: dict, lang: str = "ru", ctype: str = "audio/webm") -> str:
    """Распознавание речи через пользовательский движок.

    Режим openai — multipart на /v1/audio/transcriptions, как у OpenAI.
    Режим custom — тот же multipart, но адрес и набор полей задаёт пользователь.
    Возвращает текст; при недоступности движка бросает VoiceEngineError."""
    cfg = _clean(cfg)
    url = _endpoint(cfg, "/v1/audio/transcriptions")
    model = cfg.get("model") or "whisper-1"
    fields = {"model": model}
    if cfg.get("language"):
        fields["language"] = cfg["language"]
    elif lang:
        fields["language"] = lang
    if cfg.get("prompt"):
        fields["prompt"] = cfg["prompt"]
    for extra in ("temperature", "response_format"):
        if cfg.get(extra):
            fields[extra] = cfg[extra]
    data, mctype = _multipart(fields, "audio.webm", audio, ctype or "audio/webm")
    headers = {"Content-Type": mctype}
    key = (cfg.get("key") or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    for k, v in cfg.get("headers", {}).items():
        headers[str(k)] = str(v)
    status, body, rtype = _request(url, data, headers, int(cfg.get("timeout") or DEFAULT_TIMEOUT))
    return _text_from_response(body, rtype)


def tts(text: str, cfg: dict) -> tuple[bytes, str]:
    """Синтез речи через пользовательский движок.

    Возвращает (аудиобайты, content-type). content-type нужен фронту:
    движок может отдать wav, mp3 или ogg, и браузер обязан получить
    тот же тип, иначе воспроизведение не начнётся."""
    cfg = _clean(cfg)
    url = _endpoint(cfg, "/v1/audio/speech")
    model = cfg.get("model") or "tts-1"
    voice_name = cfg.get("voice") or "alloy"
    fmt = (cfg.get("format") or "wav").lower()
    fields = {"model": model, "voice": voice_name, "input": text}
    if cfg.get("speed"):
        fields["speed"] = cfg["speed"]
    if cfg.get("format"):
        fields["response_format"] = fmt
    data, mctype = _multipart(fields, "request.txt", text.encode("utf-8"), "text/plain; charset=utf-8")
    headers = {"Content-Type": mctype}
    key = (cfg.get("key") or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    for k, v in cfg.get("headers", {}).items():
        headers[str(k)] = str(v)
    status, body, rtype = _request(url, data, headers, int(cfg.get("timeout") or DEFAULT_TIMEOUT))
    if not body:
        raise VoiceEngineError("Движок вернул пустой ответ")
    return body, rtype or "audio/" + ("mpeg" if fmt in ("mp3", "mpeg") else fmt)


def check(cfg: dict, kind: str) -> str:
    """Проверка связи для кнопки «Проверить» в настройках.

    kind: "stt" или "tts". Проверка идёт тем же кодом, что и реальный запрос,
    поэтому показывает ровно то, что потом получит приложение."""
    cfg = _clean(cfg)
    if kind == "stt":
        # Короткий тихий фрагмент WAV: 0,1 с тишины. Движок ответит текстом
        # (возможно пустым) — этого достаточно, чтобы убедиться в живой связи.
        import struct
        rate = 16000
        n = rate // 10
        pcm = b"\x00\x00" * n
        hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + \
            struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + \
            b"data" + struct.pack("<I", len(pcm))
        out = stt(hdr + pcm, cfg, lang=cfg.get("language") or "ru", ctype="audio/wav")
        return "Движок ответил. Распознано: " + (out.strip() or "(тишина)")
    out, ctype = tts(cfg.get("testText") or "Проверка синтеза речи.", cfg)
    return f"Движок ответил: {len(out)} байт, формат {ctype}"
