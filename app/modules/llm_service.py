"""Доступ к LLM: бэкенд, URL, ключ, модель, кэш эндпоинтов и сам запрос.

Перенесено из server_backup.py без изменения тел. LLM_BACKEND / CUSTOM_* / LM_STUDIO_*
/ LLAMA_CPP_* и CFG читаются через _sb в момент вызова: их меняет админ-слой и
apply_profile_globals, поэтому by-value копия заморозила бы первый бэкенд.
_LLM_URL_CACHE в оригинале пишется через global; декларация оставлена как есть, а запись
идёт в _sb._LLM_URL_CACHE, то есть в тот же объект монолита - кэш остаётся общим.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import server_backup as _sb
import swallow

def get_llm_api_key():
    if _sb.LLM_BACKEND == "llamacpp":
        return _sb.LLAMA_CPP_API_KEY
    if _sb.LLM_BACKEND == "custom":
        return _sb.CUSTOM_API_KEY
    return _sb.LM_STUDIO_API_KEY

def get_llm_url():
    """Адрес LLM с проверкой живости и откатом на рабочий движок.

    Раньше адрес брался только из конфига, и если он указывал на несуществующий
    порт (LM Studio 1234 при работающем llama.cpp на 8080), все ИИ-функции —
    генерация курсов, словарь, помощник в книгах — падали с
    «WinError 10061: удалённый компьютер отверг запрос». Теперь нерабочий
    адрес один раз проверяется и заменяется на живой локальный движок.
    """
    global _LLM_URL_CACHE
    if _sb.LLM_BACKEND == "llamacpp":
        configured = _sb.LLAMA_CPP_URL
    elif _sb.LLM_BACKEND == "custom":
        configured = _sb.CUSTOM_URL
    else:
        configured = _sb.LM_STUDIO_URL

    now = time.time()
    if _sb._LLM_URL_CACHE and now - _sb._LLM_URL_CACHE[0] < 30:
        return _sb._LLM_URL_CACHE[1]

    url = configured
    base = configured.split("/v1/")[0] if configured else ""
    _probe_key = _sb.CUSTOM_API_KEY if _sb.LLM_BACKEND == "custom" else ""
    # 1.5 с хватает локальному порту, но не облаку: первый запрос после
    # старта тратит ~2 с на DNS+TLS и проба давала ложное «недоступен»
    # (замер 2026-10-27). Результат кэшируется на 30 с, поэтому ждём дольше.
    _probe_timeout = 5.0 if _sb.LLM_BACKEND == "custom" else 1.5
    if base and not _llm_endpoint_reachable(base, _probe_timeout, _probe_key):
        # Облачный бэкенд не подменяем локальным портом: молчаливый откат на
        # :8080 лишал пользователя самого факта, что облако не отвечает.
        if _sb.LLM_BACKEND == "custom":
            _sb.logger.warning(
                f"LLM: облачный провайдер {configured} не ответил на /v1/models, "
                f"запрос уходит по адресу из настроек")
        else:
            for fb in ("http://127.0.0.1:8080", "http://localhost:8080"):
                if _llm_endpoint_reachable(fb):
                    _sb.logger.warning(
                        f"LLM: адрес из конфига недоступен ({configured}), "
                        f"используем {fb}")
                    url = fb + "/v1/chat/completions"
                    break
    _sb._LLM_URL_CACHE = (now, url)
    return url

def get_llm_model():
    """Имя модели активного бэкенда (та же ветка, что и в URL/ключе)."""
    if _sb.LLM_BACKEND == "custom":
        return _sb.CUSTOM_MODEL
    if _sb.LLM_BACKEND == "llamacpp":
        return _sb.LLAMA_CPP_MODEL
    return _sb.LM_STUDIO_MODEL

def resolve_llm_model(model=None):
    """Привести запрошенную модель к активной.

    Для локальных бэкендов поведение совпадает с прежним: вернули запрошенную
    либо модель активного движка. Для облачного (backend=custom) локальное имя
    или пустая строка заменяются на модель из настройки «Облачный провайдер»;
    имя, которое локальным не является (пользователь сам выбрал его в списке
    моделей), остаётся как есть.
    """
    want = str(model or "").strip()
    if _sb.LLM_BACKEND != "custom":
        return want or get_llm_model()
    active = (_sb.CUSTOM_MODEL or "").strip()
    if not want:
        return active or get_llm_model()
    if active and want != active and want in _local_model_names():
        return active
    return want

def _llm_endpoint_reachable(base_url, timeout=1.5, api_key=""):
    """Быстрая проверка, что по адресу отвечает LLM-сервер.

    api_key обязателен для облачных провайдеров: clodex.xyz на GET /v1/models
    без ключа отвечает 401 (замер 2026-10-27), и без ключа любой облачный
    бэкенд выглядел «мёртвым».
    """
    if not base_url:
        return False
    import urllib.request
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/v1/models")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        swallow.note('llm_service._llm_endpoint_reachable:L117')
        return False

def _local_model_names():
    """Имена локальных моделей из настроек.

    Это и базовая модель, и per-role имена (model_ask_chat, model_translator и
    т.д.) из секции lm_studio/llama_cpp: облачный провайдер их не знает и
    отвечает 404 model_not_found (замер 2026-10-27).
    """
    names = {"local-model"}
    for section in ("lm_studio", "llama_cpp"):
        for value in (_sb.CFG.get(section) or {}).values():
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
    return names

# ============================================================
# Очередь инференса
# ============================================================
# Локальный движок (llama.cpp / LM Studio) считает один запрос за раз: при
# нескольких одновременных генерациях они мешают друг другу — контекст
# переключается, скорость падает, а часть запросов отваливается по таймауту.
# Поэтому запросы к локальному бэкенду проходят через один слот, а ожидающие
# видны в /api/ai-engine/queue и в админке. Облачный бэкенд (custom) не
# ограничиваем: он умеет параллельные запросы.
#
# Отключить очередь: INFINITY_AI_QUEUE=0. Таймаут ожидания слота:
# INFINITY_AI_QUEUE_TIMEOUT (секунды, по умолчанию 900).
_QUEUE_TIMEOUT = float(os.environ.get("INFINITY_AI_QUEUE_TIMEOUT", "900") or 900)


class _InferenceQueue:
    def __init__(self):
        self._sem = threading.Semaphore(1)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._waiting = 0
        self._active = 0
        self._served = 0
        self._label = ""
        self._model = ""
        self._since = 0.0
        self._last_wait = 0.0
        self._max_wait = 0.0

    def needed(self):
        """Нужна ли очередь для текущего бэкенда."""
        if os.environ.get("INFINITY_AI_QUEUE", "").strip().lower() in ("0", "off", "false", "no"):
            return False
        try:
            return _sb.LLM_BACKEND != "custom"
        except Exception:
            swallow.note('llm_service.needed:L170')
            return True

    @contextmanager
    def slot(self, module="", model=""):
        """Занять единственный слот инференса.

        Вложенный вызов из того же потока не ждёт сам себя: внутренние
        функции могут звать lm_studio_request повторно.
        """
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield True
            finally:
                self._local.depth -= 1
            return

        t0 = time.time()
        with self._lock:
            self._waiting += 1
            waiting = self._waiting
        if waiting > 1:
            _sb.logger.info(f"AI queue: {module} ждёт слот (в очереди {waiting})")

        acquired = False
        try:
            acquired = self._sem.acquire(timeout=_QUEUE_TIMEOUT)
            waited = time.time() - t0
            with self._lock:
                self._waiting = max(0, self._waiting - 1)
                if acquired:
                    self._active += 1
                    self._label = module or ""
                    self._model = model or ""
                    self._since = time.time()
                    self._last_wait = waited
                    self._max_wait = max(self._max_wait, waited)
            if not acquired:
                # Не блокируем навсегда: лучше выполнить запрос параллельно,
                # чем навсегда подвесить интерфейс.
                _sb.logger.warning(
                    f"AI queue: слот не освободился за {_QUEUE_TIMEOUT:.0f} с — "
                    f"{module} выполняется без очереди")
            self._local.depth = 1
            yield acquired
        finally:
            self._local.depth = 0
            if acquired:
                with self._lock:
                    self._active = max(0, self._active - 1)
                    self._served += 1
                    self._label = ""
                    self._model = ""
                    self._since = 0.0
                self._sem.release()

    def stats(self):
        with self._lock:
            current = None
            if self._active:
                current = {
                    "module": self._label,
                    "model": self._model,
                    "elapsed": round(time.time() - self._since, 1) if self._since else 0.0,
                }
            return {
                "enabled": self.needed(),
                "waiting": self._waiting,
                "active": self._active,
                "served": self._served,
                "current": current,
                "last_wait": round(self._last_wait, 3),
                "max_wait": round(self._max_wait, 3),
                "timeout_seconds": _QUEUE_TIMEOUT,
            }


_INFERENCE_QUEUE = _InferenceQueue()


def inference_queue_stats():
    """Состояние очереди инференса (для админки и агента)."""
    return _INFERENCE_QUEUE.stats()


def _queue_unavailable(messages, model, max_tokens, temperature, context_length,
                       enable_thinking, vision_image_base64):
    """Движок не ответил: сохранить запрос до его возвращения.

    Возвращает пустую строку, когда очередь выключена или отказала, — тогда
    текст ошибки остаётся ровно тем, каким был до этого модуля. Очередь
    обязана молчать: она вызывается из ветки исключения, и вторая ошибка
    поверх первой спрятала бы настоящую.
    """
    try:
        import inference_queue
        if not inference_queue.enabled():
            return ""
        label = "lm_studio_request"
        try:
            from aiengine.api import _ACTIVE_GEN
            label = str(_ACTIVE_GEN.get("module") or label)[:40]
        except Exception:
            swallow.note('llm_service._queue_unavailable:L275')
            pass
        res = inference_queue.enqueue(
            messages, model=model or "", module=label,
            opts={"max_tokens": max_tokens, "temperature": temperature,
                  "context_length": context_length,
                  "enable_thinking": enable_thinking},
            vision_image_base64=vision_image_base64)
        if not res.get("ok"):
            return ""
        return (" Запрос сохранён в очереди и выполнится сам, когда движок "
                "вернётся (id %s)." % res["id"])
    except Exception:
        swallow.note('llm_service._queue_unavailable:L288')
        return ""


def _lm_studio_request_inner(messages, max_tokens=2048, model=None, temperature=0.7, stream=False, context_length=None, vision_image_base64=None, enable_thinking=None):
    """Запрос к LM Studio API с поддержкой изображений."""
    try:
        m = resolve_llm_model(model)

        _track = None
        try:
            from aiengine.api import engine_begin, engine_update_text, engine_end
            prompt = ""
            for mm in reversed(messages):
                if isinstance(mm, dict) and mm.get("role") == "user":
                    c = mm.get("content")
                    if isinstance(c, str):
                        prompt = c
                    elif isinstance(c, list):
                        prompt = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
                    if prompt:
                        break
            engine_begin("lm_studio_request", m, prompt=prompt,
                         params={"temperature": temperature, "max_tokens": max_tokens, "stream": stream})
            _track = (engine_update_text, engine_end)
        except Exception:
            swallow.note('llm_service._lm_studio_request_inner:L314')
            _track = None

        # Если есть изображение, добавляем его к сообщениям
        if vision_image_base64 and messages:
            # LM Studio требует data URL с base64: "data:image/jpeg;base64,..."
            import base64
            try:
                # Проверяем что это валидный base64
                img_data = base64.b64decode(vision_image_base64)
                _sb.logger.info(f"Vision image size: {len(img_data)} bytes")
                
                # Определяем формат изображения
                # Первые байты определяют тип: JPEG, PNG, WebP
                mime_type = "image/jpeg"
                if img_data[:4] == b'\x89PNG':
                    mime_type = "image/png"
                elif img_data[:4] == b'RIFF' and img_data[8:12] == b'WEBP':
                    mime_type = "image/webp"
                elif img_data[:2] == b'GIF':
                    mime_type = "image/gif"
                
                data_url = f"data:{mime_type};base64,{vision_image_base64}"
                _sb.logger.info(f"Vision data URL created: {mime_type}, length: {len(data_url)}")
                
                # Находим последнее сообщение пользователя и добавляем изображение
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "user":
                        original_content = messages[i].get("content", "")
                        messages[i]["content"] = [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": data_url
                                }
                            },
                            {
                                "type": "text",
                                "text": original_content
                            }
                        ]
                        _sb.logger.info(f"Vision image added to message: {original_content[:100]}")
                        break
            except Exception as e:
                _sb.logger.error(f"Ошибка обработки изображения: {e}", exc_info=True)
        
        payload_dict = {
            "model": m,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if _sb.LLM_BACKEND == "llamacpp":
            _thinking = False if enable_thinking is None else bool(enable_thinking)
            payload_dict["chat_template_kwargs"] = {"enable_thinking": _thinking}
        elif not enable_thinking and "qwen" in m.lower():
            payload_dict["reasoning"] = {"enabled": False}
        if context_length and _sb.LLM_BACKEND != "llamacpp":
            payload_dict["context_length"] = context_length
        
        _sb.logger.info(f"LM Studio request: model={m}, messages={len(messages)}, max_tokens={max_tokens}")
        payload = json.dumps(payload_dict).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        _api_key = get_llm_api_key()
        if _api_key:
            headers["Authorization"] = f"Bearer {_api_key}"
        req = urllib.request.Request(
            get_llm_url(),
            data=payload,
            headers=headers,
            method="POST"
        )
        
        _sb.logger.info(f"LM Studio: отправка запроса...")
        with urllib.request.urlopen(req, timeout=600) as resp:
            _sb.logger.info(f"LM Studio: получен ответ, статус={resp.status}")
            raw = resp.read()
            _sb.logger.info(f"LM Studio: размер ответа={len(raw)} bytes")
            data = json.loads(raw.decode("utf-8"))
            choice = data["choices"][0]["message"]
            content = choice.get("content", "")
            reasoning = choice.get("reasoning_content", "")
            _sb.logger.info(f"LM Studio: content length={len(content)}, reasoning length={len(reasoning)}")
            if _track:
                try:
                    _track[1](content)
                except Exception:
                    swallow.note('llm_service._lm_studio_request_inner:L402')
                    pass
            return {
                "content": content,
                "reasoning": reasoning
            }
    except urllib.error.URLError as e:
        reason = str(e.reason) if hasattr(e, 'reason') else str(e)
        _sb.logger.error(f"LM Studio URL Error: {reason}")
        if _track:
            try:
                _track[1]("", error=reason)
            except Exception:
                swallow.note('llm_service._lm_studio_request_inner:L415')
                pass
        _held = _queue_unavailable(messages, model, max_tokens, temperature,
                                   context_length, enable_thinking,
                                   vision_image_base64)
        return {"content": f"Ошибка подключения к LM Studio: {reason}. Убедитесь что LM Studio запущен и модель загружена.{_held}", "reasoning": ""}
    except Exception as e:
        _sb.logger.error(f"LM Studio error: {e}", exc_info=True)
        if _track:
            try:
                _track[1]("", error=str(e))
            except Exception:
                swallow.note('llm_service._lm_studio_request_inner:L427')
                pass
        return {"content": f"Ошибка LM Studio: {e}", "reasoning": ""}


def lm_studio_request(messages, max_tokens=2048, model=None, temperature=0.7, stream=False, context_length=None, vision_image_base64=None, enable_thinking=None):
    """Публичная точка входа: локальный инференс — через общую очередь."""
    if not _INFERENCE_QUEUE.needed():
        return _lm_studio_request_inner(
            messages, max_tokens=max_tokens, model=model,
            temperature=temperature, stream=stream,
            context_length=context_length,
            vision_image_base64=vision_image_base64,
            enable_thinking=enable_thinking)
    with _INFERENCE_QUEUE.slot("lm_studio_request", resolve_llm_model(model)):
        return _lm_studio_request_inner(
            messages, max_tokens=max_tokens, model=model,
            temperature=temperature, stream=stream,
            context_length=context_length,
            vision_image_base64=vision_image_base64,
            enable_thinking=enable_thinking)
