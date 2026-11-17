"""Text-хелперы LLM и TTS, базовые URL, список моделей и проверка здоровья.

Перенесено из server_backup.py без изменения тел. LLM_BACKEND, LM_STUDIO_BASE_URL,
CUSTOM_BASE_URL, LLAMA_CPP_BASE_URL, LM_STUDIO_MODEL читаются через _sb в момент
вызова: их переписывает админ-слой и apply_profile_globals, поэтому by-value копия
заморозила бы первый бэкенд. _lm_health_cache и _LM_HEALTH_TTL тоже берутся из _sb:
кэш пишется в тот же словарь монолита, что читает непересаженный код, и две копии
дали бы расхождение состояния эндпоинта.
lm_studio_request и get_llm_api_key уже пересажены в modules.llm_service, но вызов
идёт через _sb - поведение то же (AST-паритет доказан), единый адрес состояния
разбирается на последнем шаге, когда монолит уезжает в trash.
"""

import json
import re
import time
import urllib.error
import urllib.request

import server_backup as _sb
import swallow

def _clean_llm_output(text, extract_answer=False):
    """Удаляет цепочки рассуждений из вывода LLM.
    extract_answer=True — извлечь только итоговый ответ (последнее слово/фразу).
    """
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'(?s)```.*?```', '', text)
    text = re.sub(r'(?s)\*\*.*?\*\*', '', text)
    text = re.sub(r'(?im)^\s*\d+\.\s+.*?(?=\n|$)', '', text)
    text = re.sub(r'(?im)^\s*[-*]\s+\*\*.*?(?=\n|$)', '', text)
    text = re.sub(r'(?im)^\s*\*.*?(?=\n|$)', '', text)
    text = re.sub(r'(?im)(original|исходный|source)\s*:.*?(?:translation|перевод|target)\s*:', '', text)
    text = re.sub(r'(?i)language\s*:\s*\w+', '', text)
    text = re.sub(r'\b(analysis|input|output|task|constraint|note|step|thinking|reasoning|determine|identify|check|proceed|ready|draft|refinement|self[._-]?correction|final|ensure|verify|consider|approach|strategy|paragraph|section|overview)\b', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^[-:\s,;]+', '', text)
    text = re.sub(r'[-:\s,;]+$', '', text)
    if extract_answer:
        parts = [p.strip() for p in re.split(r'[.!?\n]', text) if p.strip()]
        parts = [p for p in parts if len(p) > 1 and not re.match(r'(?i)^\s*(and|or|the|a|an|in|on|at|to|for|of|is|was|are|were|it|i|we|you|he|she|they|this|that|these|those)\s*$', p)]
        if parts:
            text = parts[-1]
        else:
            words = [w for w in text.split() if len(w) > 1]
            text = words[-1] if words else text
        text = re.sub(r'(?i)^[-:\s]*(double.?check|i.?ll|note|output|so\s+the|answer|translation|перевод)[-:\s]*', '', text).strip()
    return text.strip()

def _clean_translation_output(text):
    """Мягкая очистка вывода LLM для перевода/саммари.

    Убирает только HTML-теги, лишние пробелы и служебные префиксы модели.
    Не трогает списки, жирный текст и обычные слова.
    """
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(
        r'^(here\s+is\s+(the\s+)?(translation|summary)|translation|перевод|'
        r'original\s*(text)?|исходный\s*(текст)?|source\s*text|результат)\s*[:\-–]\s*',
        '', text, flags=re.IGNORECASE
    )
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text.strip()

def _sanitize_tts_text(text: str) -> str:
    """Очистка текста для TTS от Markdown-разметки и мусора.

    Silero v5 падает с KeyError на символах вне токенсета (цифры, '-', '()',
    кавычки и т.п.) — путь с акцентами в apply_tts обходит фильтр модели,
    поэтому чистим текст до вызова. Делегируем общему санитайзеру.
    """
    if not text:
        return ""
    from silero_pool import silero_safe_text
    return silero_safe_text(text)

def get_llm_base_url():
    if _sb.LLM_BACKEND == "llamacpp":
        return _sb.LLAMA_CPP_BASE_URL
    if _sb.LLM_BACKEND == "custom":
        return _sb.CUSTOM_BASE_URL
    return _sb.LM_STUDIO_BASE_URL

_llm_unreachable_cache = {"base": None, "ts": 0.0, "value": False}
_LLM_UNREACHABLE_TTL = 5.0
_LLM_PROBE_TIMEOUT = 0.15


def _llm_port_unreachable(base_url):
    """True — LLM-бэкенд на localhost заведомо не слушает порт.

    Отказ соединения через urlopen на Windows стоит ~2 с: стек ждёт ответа
    на SYN, а не получает немедленный RST. TCP-проба с коротким таймаутом
    отвечает за доли секунды. Только loopback: удалённому хосту короткий
    таймаут дал бы ложное «недоступен».

    Проба кэшируется на 5 с: сам отказ на Windows тоже стоит ~250 мс
    (SYN ждёт, а не получает RST), а /api/models ходит сюда на каждом
    промахе своего 3-секундного кэша.
    """
    import socket
    host, port = _parse_url_host_port(base_url)
    if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return False
    now = time.time()
    cached = _llm_unreachable_cache
    if cached["base"] == base_url and now - cached["ts"] < _LLM_UNREACHABLE_TTL:
        return cached["value"]
    try:
        with socket.create_connection((host, port), timeout=_LLM_PROBE_TIMEOUT):
            value = False
    except OSError:
        swallow.note('llm_text._llm_port_unreachable:L117')
        value = True
    cached["base"] = base_url
    cached["ts"] = now
    cached["value"] = value
    return value


def lm_studio_get_models():
    """Получить список загруженных моделей из LLM бэкенда."""
    try:
        url = get_llm_base_url() + "/v1/models"
        if _llm_port_unreachable(get_llm_base_url()):
            return [_sb.LM_STUDIO_MODEL]
        headers = {}
        _key = _sb.get_llm_api_key()
        if _key:
            headers["Authorization"] = f"Bearer {_key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [m["id"] for m in data.get("data", [])]
    except Exception as e:
        swallow.note('llm_text.lm_studio_get_models:L140', e)
        return [_sb.LM_STUDIO_MODEL]

def _parse_url_host_port(url):
    """Извлечь host и port из URL."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port

def _check_lm_studio_health_uncached():
    """Реальная сетевая проверка LLM бэкенда (короткие таймауты)."""
    last_error = ""
    base_url = get_llm_base_url().rstrip("/")
    if not base_url:
        return {"available": False, "models": [], "total_models": 0, "error": "URL бэкенда не задан"}
    if _llm_port_unreachable(base_url):
        return {"available": False, "models": [], "total_models": 0,
                "error": "бэкенд не слушает порт"}
    # Пробуем /v1/models (OpenAI-совместимый)
    try:
        resp = urllib.request.urlopen(f"{base_url}/v1/models", timeout=1)
        if resp.status == 200:
            data = json.loads(resp.read().decode())
            models = data.get("data", [])
            return {
                "available": True,
                "models": [m.get("id", "") for m in models[:5]],
                "total_models": len(models),
            }
    except Exception as e:
        swallow.note('llm_text._check_lm_studio_health_uncached:L172', e)
        last_error = f"/v1/models: {str(e)[:80]}"
    # Если /v1/models не сработал, пробуем просто достучаться до сервера
    try:
        resp = urllib.request.urlopen(f"{base_url}/", timeout=1)
        if resp.status < 500:
            return {"available": True, "models": [], "total_models": 0}
    except Exception as e:
        swallow.note('llm_text._check_lm_studio_health_uncached:L180', e)
        last_error = f"GET /: {str(e)[:80]}"
    # Последняя попытка - проверить что порт открыт (простой TCP)
    try:
        host, port = _parse_url_host_port(base_url)
        import socket
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        return {"available": True, "models": [], "total_models": 0}
    except Exception as e:
        swallow.note('llm_text._check_lm_studio_health_uncached:L190', e)
        last_error = f"TCP {host}:{port}: {str(e)[:80]}"
    return {"available": False, "models": [], "total_models": 0, "error": last_error}

def _check_lm_studio_health():
    """Проверка доступности LLM бэкенда.

    Кэшируется на _LM_HEALTH_TTL секунд — health-эндпоинт опрашивается часто,
    а блокирующая проверка по сети (до 13с при недоступном бэкенде) забивает
    пул воркеров и вешает event loop сервера (порт слушается, но соединения
    не принимаются).
    """
    import os as _os
    if _os.environ.get("INFINITY_SKIP_LLM_HEALTH") == "1":
        return {"available": False, "models": [], "total_models": 0, "error": "skipped"}
    now = time.time()
    if now - _sb._lm_health_cache["t"] < _sb._LM_HEALTH_TTL and _sb._lm_health_cache["data"] is not None:
        return _sb._lm_health_cache["data"]
    result = _check_lm_studio_health_uncached()
    _sb._lm_health_cache["t"] = time.time()
    _sb._lm_health_cache["data"] = result
    return result

def translate_lm_studio(text, from_lang="en", to_lang="ru", model=None):
    """Перевод через LM Studio."""
    clean_text = re.sub(r'<[^>]+>', ' ', text)
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    if not clean_text:
        return {"translated": "", "method": "lm_studio"}
    lang_names = {"en": "English", "ru": "Russian", "de": "German", "fr": "French", "es": "Spanish"}
    dst = lang_names.get(to_lang, to_lang)
    if from_lang == "auto":
        prompt = f"Translate the following text to {dst}. Output ONLY the translated text, no explanations, no quotes, no HTML. Keep paragraphs.\n\n{clean_text[:8000]}"
    else:
        src = lang_names.get(from_lang, from_lang)
        prompt = f"Translate the following text from {src} to {dst}. Output ONLY the translated text, no explanations, no quotes, no HTML. Keep paragraphs.\n\n{clean_text[:8000]}"
    result = _sb.lm_studio_request([{"role": "user", "content": prompt}], max_tokens=4096, model=model)
    translated = result.get("content", "") or result.get("reasoning", "")
    translated = _clean_translation_output(translated)
    return {"translated": translated.strip(), "method": "lm_studio"}
