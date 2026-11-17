"""
GenLab — клиент ComfyUI поверх его HTTP + WebSocket API (версия 0.39.x).

Два независимых слоя:
  * ComfyClient  — синхронные HTTP-вызовы (httpx). Вызывается из рабочих потоков.
  * ComfyWatcher — асинхронный WebSocket-слушатель (библиотека `websockets`).
    Её может не быть в замороженной сборке, поэтому сервис умеет работать
    без неё — на опросе /queue + /history.

Особенности API ComfyUI, которые здесь учтены:
  * Клиент ВСЕГДА отправляет `client_id` — иначе события выполнения уходят
    всем подключённым сокетам или теряются вовсе (comfy_execution/execution.py).
  * Все маршруты сервера зарегистрированы дважды: `/prompt` и `/api/prompt`
    эквивалентны. Используем `/api/...` там, где он есть.
  * Отмена: `/api/jobs/{id}/cancel` (надёжно) с откатом на `/interrupt`.
  * Результаты забираются из `/history/{prompt_id}`, файлы — через `/view`.
"""

import asyncio
import json
import os
import threading
import time
import uuid
import swallow

logger = None


def _logger():
    global logger
    if logger is None:
        import logging
        logger = logging.getLogger("infinity.genlab")
    return logger


class ComfyError(RuntimeError):
    """Ошибка ComfyUI. details — исходный JSON ответа (для UI)."""

    def __init__(self, message, details=None, status=0):
        super().__init__(message)
        self.details = details
        self.status = status


class ComfyClient:
    """Синхронный HTTP-клиент ComfyUI."""

    def __init__(self, base_url, timeout=30.0):
        self.base_url = str(base_url).rstrip("/")
        self.timeout = timeout
        self._client = None
        self._lock = threading.Lock()
        self._object_info = {"value": None, "ts": 0.0}

    # ---------------------------------------------------------------- HTTP

    def _http(self):
        with self._lock:
            if self._client is None:
                import httpx
                self._client = httpx.Client(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout, connect=5.0),
                    headers={"User-Agent": "InfinityGenLab/1.0"},
                    follow_redirects=True,
                )
            return self._client

    def close(self):
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    swallow.note('comfy_client.close:L76')
                    pass
                self._client = None

    def _request(self, method, path, **kw):
        try:
            resp = self._http().request(method, path, **kw)
        except Exception as e:
            raise ComfyError(f"ComfyUI недоступен ({self.base_url}): {e}") from e
        return resp

    def _json(self, method, path, **kw):
        resp = self._request(method, path, **kw)
        if resp.status_code >= 400:
            raise ComfyError(
                f"ComfyUI вернул {resp.status_code} на {path}",
                _safe_json(resp),
                resp.status_code,
            )
        return _safe_json(resp)

    # ------------------------------------------------------------ публичное

    def system_stats(self):
        """Инфо о ComfyUI и GPU: версия, VRAM, устройства."""
        return self._json("GET", "/system_stats")

    def is_alive(self, timeout=3.0):
        """Быстрая проверка живости (короткий таймаут, без исключений)."""
        prev = self.timeout
        try:
            self.timeout = timeout
            self.system_stats()
            return True
        except Exception:
            swallow.note('comfy_client.is_alive:L111')
            return False
        finally:
            self.timeout = prev

    def features(self):
        try:
            return self._json("GET", "/features")
        except Exception:
            swallow.note('comfy_client.features:L120')
            return {}

    def queue_info(self):
        """{exec_info: {queue_remaining: N}} — сколько ждать в очереди."""
        return self._json("GET", "/prompt")

    def queue(self):
        """{queue_running: [...], queue_pending: [...]}."""
        return self._json("GET", "/queue")

    def object_info(self, node_class=None, max_age=60.0):
        """Схема нод (кэш на max_age секунд). node_class — одна конкретная нода."""
        now = time.time()
        if node_class is None:
            cached = self._object_info
            if cached["value"] is not None and now - cached["ts"] < max_age:
                return cached["value"]
        path = "/object_info" if node_class is None else f"/object_info/{node_class}"
        data = self._json("GET", path)
        if node_class is None:
            self._object_info = {"value": data, "ts": now}
        return data

    def models(self, folder=None):
        """Список папок моделей или файлов внутри папки."""
        if not folder:
            return self._json("GET", "/models")
        return self._json("GET", f"/models/{folder}")

    def history(self, prompt_id):
        """История выполнения по prompt_id: {prompt_id: {outputs, status, ...}}."""
        data = self._json("GET", f"/history/{prompt_id}")
        return data or {}

    def clear_history(self, prompt_ids=None):
        body = {"delete": list(prompt_ids)} if prompt_ids else {"clear": True}
        return self._json("POST", "/history", json=body)

    def submit(self, graph, client_id, ui_workflow=None):
        """Поставить граф в очередь. Возвращает {prompt_id, number, node_errors}."""
        payload = {"prompt": graph, "client_id": client_id}
        if ui_workflow:
            payload["extra_data"] = {"extra_pnginfo": {"workflow": ui_workflow}}
        return self._json("POST", "/prompt", json=payload)

    def interrupt(self, prompt_id=None):
        """Прервать выполнение. Без prompt_id — глобальный interrupt."""
        body = {"prompt_id": prompt_id} if prompt_id else {}
        return self._json("POST", "/interrupt", json=body)

    def cancel_job(self, job_id):
        """POST /api/jobs/{id}/cancel — снимает и running, и pending."""
        try:
            return self._json("POST", f"/api/jobs/{job_id}/cancel")
        except ComfyError:
            swallow.note('comfy_client.cancel_job:L176')
            return self.interrupt(job_id)

    def clear_queue(self, prompt_ids=None):
        body = {"delete": list(prompt_ids)} if prompt_ids else {"clear": True}
        return self._json("POST", "/queue", json=body)

    def free_memory(self, unload_models=True, free_memory=True):
        return self._json(
            "POST", "/free",
            json={"unload_models": unload_models, "free_memory": free_memory},
        )

    # ------------------------------------------------------------- загрузка

    def upload(self, file_path, filename=None, subfolder="", overwrite=True):
        """Загрузить файл в input ComfyUI → {name, subfolder, type}."""
        name = filename or os.path.basename(file_path)
        if not name:
            raise ComfyError("Файл без имени нельзя загрузить в ComfyUI")
        with open(file_path, "rb") as fh:
            data = fh.read()
        files = {"image": (name, data)}
        data_fields = {
            "overwrite": "true" if overwrite else "false",
            "subfolder": subfolder or "",
            "type": "input",
        }
        return self._json("POST", "/upload/image", files=files, data=data_fields)

    def upload_bytes(self, data, filename, subfolder="", overwrite=True):
        """То же, но из памяти (например, отредактированная картинка)."""
        files = {"image": (filename, data)}
        data_fields = {
            "overwrite": "true" if overwrite else "false",
            "subfolder": subfolder or "",
            "type": "input",
        }
        return self._json("POST", "/upload/image", files=files, data=data_fields)

    def view(self, filename, subfolder="", kind="output", preview=None):
        """Скачать файл-результат из ComfyUI (картинка, звук, видео, glb)."""
        params = {"filename": filename, "subfolder": subfolder or "", "type": kind}
        if preview:
            params["preview"] = preview
        resp = self._request("GET", "/view", params=params)
        if resp.status_code >= 400:
            raise ComfyError(
                f"ComfyUI не отдал файл {filename} ({resp.status_code})",
                _safe_json(resp), resp.status_code,
            )
        return resp.content

    def view_url(self, filename, subfolder="", kind="output"):
        """URL для прямой отдачи в <img>/<video> — без проксирования байтов."""
        from urllib.parse import quote
        parts = f"filename={quote(filename, safe='')}"
        if subfolder:
            parts += f"&subfolder={quote(subfolder, safe='')}"
        parts += f"&type={kind}"
        return f"{self.base_url}/view?{parts}"


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:
        swallow.note('comfy_client._safe_json:L243')
        try:
            return {"raw": resp.text[:2000]}
        except Exception:
            swallow.note('comfy_client._safe_json:L247')
            return None


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------

class ComfyWatcher:
    """Слушает ws://host:port/ws?clientId=<id> и раздаёт события подписчикам.

    Требует библиотеку `websockets`. Если её нет — объект остаётся в состоянии
    unavailable, и сервис переходит на опрос.
    """

    def __init__(self, base_url, client_id=None, on_event=None):
        self.base_url = str(base_url).rstrip("/")
        self.client_id = client_id or uuid.uuid4().hex
        self.on_event = on_event
        self.connected = False
        self.available = _has_websockets()
        self._task = None
        self._stop = False

    @property
    def ws_url(self):
        host = self.base_url.split("://", 1)[-1]
        return f"ws://{host}/ws?clientId={self.client_id}"

    async def start(self):
        if not self.available or self._task is not None:
            return
        self._stop = False
        self._task = asyncio.create_task(self._run(), name="genlab-comfy-ws")

    async def stop(self):
        self._stop = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            swallow.note('comfy_client.stop:L292')
            pass
        self.connected = False

    async def _run(self):
        import websockets  # noqa: F401  (проверка импорта)
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(
                    self.ws_url,
                    max_size=64 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                ) as ws:
                    self.connected = True
                    backoff = 1.0
                    _logger().info(f"[genlab] ComfyUI WS подключён: {self.ws_url}")
                    await self._send_flags(ws)
                    await self._emit("ws_status", {"connected": True})
                    async for raw in ws:
                        if self._stop:
                            break
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.connected:
                    _logger().warning(f"[genlab] ComfyUI WS обрыв: {e}")
            finally:
                if self.connected:
                    self.connected = False
                    await self._emit("ws_status", {"connected": False})
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 15.0)

    async def _send_flags(self, ws):
        """Просим превью с метаданными — тип события 4 вместо 1."""
        try:
            await ws.send(json.dumps({
                "type": "feature_flags",
                "data": {"supports_preview_metadata": True},
            }))
        except Exception:
            swallow.note('comfy_client._send_flags:L339')
            pass

    async def _handle(self, raw):
        if isinstance(raw, (bytes, bytearray)):
            await self._handle_binary(bytes(raw))
            return
        try:
            msg = json.loads(raw)
        except Exception:
            swallow.note('comfy_client._handle:L349')
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        data = msg.get("data")
        if mtype == "progress" and isinstance(data, dict):
            data = dict(data)
            data["value"] = data.get("value")
            data["max"] = data.get("max")
        await self._emit(mtype, data)

    async def _handle_binary(self, buf):
        """Бинарный кадр: 4 байта big-endian = тип события (comfy protocol.py)."""
        if len(buf) < 4:
            return
        import struct
        (etype,) = struct.unpack(">I", buf[:4])
        body = buf[4:]
        if etype == 1 and len(body) >= 4:
            # PREVIEW_IMAGE: uint32 image_type + байты картинки
            await self._emit("preview", {"image": body[4:], "has_meta": False})
        elif etype == 2:
            await self._emit("preview_raw", {"size": len(body)})
        elif etype == 3 and len(body) >= 4:
            # TEXT: uint32 len(node_id) + node_id + текст
            (nlen,) = struct.unpack(">I", body[:4])
            node_id = body[4:4 + nlen].decode("utf-8", "replace")
            await self._emit("text", {"node_id": node_id,
                                      "text": body[4 + nlen:].decode("utf-8", "replace")})
        elif etype == 4:
            await self._emit("preview_meta", {"size": len(body)})

    async def _emit(self, mtype, data):
        if not self.on_event:
            return
        try:
            res = self.on_event(mtype, data or {})
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            _logger().warning(f"[genlab] обработчик WS-события {mtype} упал: {e}")


def _has_websockets():
    try:
        import websockets  # noqa: F401
        return True
    except Exception:
        swallow.note('comfy_client._has_websockets:L398')
        return False