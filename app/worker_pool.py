"""
Центральный менеджер фоновых задач и потоков.
Два пула: HIGH (быстрые операции) и LOW (фоновые задачи).
Асинхронное чтение файлов без блокировки event loop.
"""
import os, sys, json, time, uuid, logging, threading, asyncio, queue, sqlite3, http.client, urllib.parse, contextvars
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import httpx
from typing import Callable, Optional, Dict, Any, Union, List, AsyncIterator
import swallow

logger = logging.getLogger("infinity")

# Количество воркеров
_CPU_COUNT = os.cpu_count() or 4

# HIGH — для операций, которые пользователь ждёт (TTS, STT, AI, поиск)
HIGH_POOL = ThreadPoolExecutor(
    max_workers=min(32, _CPU_COUNT * 4),
    thread_name_prefix="high-worker"
)

# LOW — для фоновых задач (индексация, перестроение, загрузка)
LOW_POOL = ThreadPoolExecutor(
    max_workers=max(4, _CPU_COUNT),
    thread_name_prefix="low-worker"
)

# SEARCH — изолированный пул для search_all. Без него поиск делил HIGH_POOL со
# всеми остальными эндпоинтами: 32 воркера забивались долгими поисками, и в
# очереди за ними вставали health/notes/books — весь UI тормозил из-за одного
# поиска. Отдельный пул держит этот хвост только на поиске.
SEARCH_POOL = ThreadPoolExecutor(
    max_workers=max(4, min(8, _CPU_COUNT)),
    thread_name_prefix="search-worker"
)

# Хранилище фоновых задач: task_id -> info
_tasks: Dict[str, Dict[str, Any]] = {}
_tasks_lock = threading.Lock()

# WebSocket уведомления
_ws_queues: list = []
_ws_lock = threading.Lock()


def register_ws(queue, loop):
    """Зарегистрировать очередь для WebSocket-уведомлений."""
    with _ws_lock:
        _ws_queues.append((queue, loop))


def unregister_ws(queue):
    """Отменить регистрацию WebSocket-очереди."""
    with _ws_lock:
        _ws_queues[:] = [(q, l) for q, l in _ws_queues if q != queue]


def _notify_ws(event_type: str, task_id: str, **extra):
    """Отправить уведомление всем WebSocket клиентам (можно вызывать из потоков).

    Список читается под локом, а рассылка — уже вне его: `call_soon_threadsafe`
    пишет в self-pipe цикла и при переполненном буфере сокета может ждать. Держать
    на это `_ws_lock` значило, что один зависший клиент останавливает уведомления
    всех остальных и блокирует register/unregister.
    """
    msg = {"type": event_type, "task_id": task_id, **extra}
    with _ws_lock:
        targets = list(_ws_queues)
    for q, loop in targets:
        try:
            loop.call_soon_threadsafe(q.put_nowait, dict(msg))
        except Exception:
            swallow.note('worker_pool._notify_ws:L73')
            pass


def background_task(func: Callable, *args, **kwargs) -> str:
    """Запустить функцию в фоновом LOW-пуле. Возвращает task_id."""
    task_id = uuid.uuid4().hex[:12]
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id,
            "status": "running",
            "progress": 0,
            "message": "Запуск...",
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
            "result": None,
        }

    def _wrapper():
        try:
            logger.info(f"[worker_pool] Task {task_id} started: {func.__name__}")
            _notify_ws("task_started", task_id, status="running", progress=0)
            result = func(*args, **kwargs)
            with _tasks_lock:
                _tasks[task_id]["status"] = "done"
                _tasks[task_id]["progress"] = 100
                _tasks[task_id]["message"] = "Завершено"
                _tasks[task_id]["finished_at"] = time.time()
                _tasks[task_id]["result"] = result
            _notify_ws("task_completed", task_id, status="done", progress=100)
            logger.info(f"[worker_pool] Task {task_id} completed: {func.__name__}")
        except Exception as e:
            logger.error(f"[worker_pool] Task {task_id} error: {e}", exc_info=True)
            with _tasks_lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["error"] = str(e)
                _tasks[task_id]["finished_at"] = time.time()
            _notify_ws("task_error", task_id, status="error", error=str(e))

    LOW_POOL.submit(_wrapper)
    return task_id


def update_progress(task_id: str, progress: int, message: str = ""):
    """Обновить прогресс фоновой задачи."""
    with _tasks_lock:
        if task_id in _tasks:
            _tasks[task_id]["progress"] = progress
            if message:
                _tasks[task_id]["message"] = message
    _notify_ws("task_progress", task_id, progress=progress, message=message)


def task_status(task_id: str) -> Optional[Dict[str, Any]]:
    """Получить статус задачи."""
    with _tasks_lock:
        return _tasks.get(task_id)


def get_all_tasks() -> Dict[str, Dict[str, Any]]:
    """Получить все активные и недавние задачи."""
    with _tasks_lock:
        # Чистим старые задачи (>10 минут)
        now = time.time()
        stale = [tid for tid, t in _tasks.items()
                 if t.get("finished_at") and (now - t["finished_at"]) > 600]
        for tid in stale:
            del _tasks[tid]
        return dict(_tasks)


def run_in_thread(func: Callable, *args, **kwargs):
    """Запустить функцию в HIGH-пуле (для asyncio). Возвращает Future."""
    return HIGH_POOL.submit(func, *args, **kwargs)


def run_in_pool(pool, func: Callable, *args, **kwargs):
    """await run_in_pool(HIGH_POOL, fn, ...) — с переносом контекста задачи.

    copy_context обязателен: run_in_executor контекст вызывающего не переносит,
    а в нём лежит профиль запроса (profile_paths.SCOPE). Без переноса потоки
    пула читали бы глобалы активного профиля, и изоляция профилей молча не
    работала бы. Пул в аргументе, а не в замыкании: у каждого router-модуля
    реализация _run_sync своя и выбор пула должен оставаться на вызывающем.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return loop.run_in_executor(pool, lambda: ctx.run(func, *args, **kwargs))


async def run_async(func: Callable, *args, **kwargs):
    """Обёртка для await loop.run_in_executor(HIGH_POOL, func, ...)."""
    return await run_in_pool(HIGH_POOL, func, *args, **kwargs)


async def run_low_async(func: Callable, *args, **kwargs):
    """Обёртка для await loop.run_in_executor(LOW_POOL, func, ...)."""
    return await run_in_pool(LOW_POOL, func, *args, **kwargs)


def cancel_task(task_id: str) -> bool:
    """Отметить задачу как отменённую (флаг для _wrapper)."""
    with _tasks_lock:
        if task_id not in _tasks:
            return False
        t = _tasks[task_id]
        if t["status"] not in ("running", "pending"):
            return False
        t["status"] = "cancelled"
        t["message"] = "Отменено пользователем"
        t["finished_at"] = time.time()
    _notify_ws("task_cancelled", task_id, status="cancelled")
    logger.info(f"[worker_pool] Task {task_id} cancelled by user")
    return True


_SQLITE_POOLS = []
_SQLITE_POOLS_LOCK = threading.Lock()


class SQLitePool:
    """Пул соединений SQLite с автоматической проверкой живучести.

    Позволяет переиспользовать соединения вместо создания нового на каждый
    запрос (см. collection_module.get_default_db). Потокобезопасен.
    """
    def __init__(self, db_path: str, max_connections: int = 5, timeout: int = 5):
        self._db_path = db_path
        self._timeout = timeout
        self._pool: queue.Queue = queue.Queue(maxsize=max_connections)
        self._max = max_connections
        self._lock = threading.Lock()
        self._shutdown = False
        with _SQLITE_POOLS_LOCK:
            _SQLITE_POOLS.append(self)

    def _create_conn(self) -> sqlite3.Connection:
        # check_same_thread=False обязателен: соединение, созданное в одном
        # воркере HIGH_POOL, возвращается в пул и через acquire() попадает в
        # другой поток. Без этого флага первый же запрос чужого потока даёт
        # sqlite3.ProgrammingError.
        conn = sqlite3.connect(self._db_path, timeout=self._timeout,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except (sqlite3.OperationalError, Exception):
            swallow.note('worker_pool._create_conn:L209')
            pass
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout = %d" % (self._timeout * 1000))
        return conn

    def acquire(self) -> sqlite3.Connection:
        if self._shutdown:
            raise RuntimeError("SQLitePool is shut down")
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            # Пустой пул — штатный первый запрос, соединение создаётся ниже.
            conn = self._create_conn()
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            swallow.note('worker_pool.acquire:L227')
            try: conn.close()
            except Exception:
                swallow.note('worker_pool.acquire:L230')
                pass
            return self._create_conn()

    def release(self, conn: sqlite3.Connection):
        if self._shutdown or conn is None:
            try: conn.close()
            except Exception:
                swallow.note('worker_pool.release:L238')
                pass
            return
        if self._pool.qsize() < self._max:
            self._pool.put_nowait(conn)
        else:
            conn.close()

    def close_all(self):
        self._shutdown = True
        while True:
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except queue.Empty:
                swallow.note('worker_pool.close_all:L253')
                break

    @property
    def size(self) -> int:
        return self._pool.qsize()


# ─── Асинхронное чтение файлов ──────────────────────────────────────────────

_JSON_WRITE_LOCKS = {}
_JSON_WRITE_LOCKS_GUARD = threading.Lock()


async def read_file(path: str, mode: str = "rb", encoding: str = None) -> Any:
    """Асинхронно прочитать весь файл через HIGH_POOL (не блокирует event loop)."""
    loop = asyncio.get_running_loop()

    def _read():
        if "b" in mode:
            with open(path, mode) as f:
                return f.read()
        else:
            with open(path, mode, encoding=encoding or "utf-8") as f:
                return f.read()

    return await loop.run_in_executor(HIGH_POOL, _read)


async def read_file_lines(path: str, encoding: str = "utf-8") -> List[str]:
    """Асинхронно прочитать строки файла."""
    loop = asyncio.get_running_loop()

    def _read():
        with open(path, "r", encoding=encoding) as f:
            return f.readlines()

    return await loop.run_in_executor(HIGH_POOL, _read)


async def read_file_chunks(path: str, chunk_size: int = 65536) -> AsyncIterator[bytes]:
    """Асинхронный генератор чанков файла (для стриминга)."""
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(HIGH_POOL, lambda: open(path, "rb"))

    class _SyncReader:
        def __init__(self, fd):
            self._fd = fd
        def read_chunk(self):
            return self._fd.read(chunk_size)
        def close(self):
            self._fd.close()

    fd = await fut
    reader = _SyncReader(fd)
    try:
        while True:
            chunk = await loop.run_in_executor(HIGH_POOL, reader.read_chunk)
            if not chunk:
                break
            yield chunk
    finally:
        await loop.run_in_executor(HIGH_POOL, reader.close)


async def write_file(path: str, content: Any, mode: str = "w", encoding: str = "utf-8") -> int:
    """Асинхронно записать данные в файл через HIGH_POOL."""
    loop = asyncio.get_running_loop()

    def _write():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if "b" in mode:
            with open(path, mode) as f:
                return f.write(content)
        else:
            with open(path, mode, encoding=encoding) as f:
                return f.write(content)

    return await loop.run_in_executor(HIGH_POOL, _write)


async def read_json(path: str) -> Any:
    """Асинхронно прочитать и распарсить JSON-файл."""
    import json
    loop = asyncio.get_running_loop()

    def _read():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    return await loop.run_in_executor(HIGH_POOL, _read)


async def write_json(path: str, data: Any, indent: int = 2) -> None:
    """Асинхронно записать JSON-файл атомарно.

    Запись шла напрямую в целевой файл: процесс, убитый посреди json.dump,
    оставлял обрезанный JSON, который потом падал в read_json у всех, кто его
    читает. os.replace ставит готовый файл одной операцией, а блокировка по
    пути не даёт двум потокам писать один файл одновременно.
    """
    import json
    loop = asyncio.get_running_loop()

    def _write():
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with _JSON_WRITE_LOCKS_GUARD:
            lock = _JSON_WRITE_LOCKS.get(path)
            if lock is None:
                lock = threading.Lock()
                _JSON_WRITE_LOCKS[path] = lock
        with lock:
            tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=indent)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    swallow.note('worker_pool._write:L378')
                    pass
                raise

    return await loop.run_in_executor(HIGH_POOL, _write)


async def file_exists(path: str) -> bool:
    """Асинхронная проверка существования файла."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(HIGH_POOL, lambda: os.path.exists(path))


async def file_size(path: str) -> int:
    """Асинхронное получение размера файла."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(HIGH_POOL, lambda: os.path.getsize(path))


# ============================================================
# Lazy module importer — deferred import site-модулей
# ============================================================

_LAZY_MODULES = {}
_LAZY_MODULES_LOCK = threading.Lock()


def lazy_module(mod_name: str):
    """Ленивый импорт модуля с кэшированием.

    Пытается импортировать модуль один раз, кэширует результат.
    При повторном вызове возвращает кэшированный модуль или None.
    """
    if mod_name in _LAZY_MODULES:
        return _LAZY_MODULES[mod_name]
    with _LAZY_MODULES_LOCK:
        if mod_name in _LAZY_MODULES:
            return _LAZY_MODULES[mod_name]
        try:
            mod = __import__(mod_name)
            _LAZY_MODULES[mod_name] = mod
            return mod
        except ImportError:
            swallow.note('worker_pool.lazy_module:L421')
            _LAZY_MODULES[mod_name] = None
            return None
        except Exception:
            swallow.note('worker_pool.lazy_module:L425')
            _LAZY_MODULES[mod_name] = None
            return None


def lazy_module_attr(mod_name: str, attr: str = None, default=None):
    """Ленивый импорт модуля и получение атрибута.

    Пример: lazy_module_attr('profiles_module', 'list_profiles')
    """
    mod = lazy_module(mod_name)
    if mod is None:
        return default
    if attr is None:
        return mod
    return getattr(mod, attr, default)


# ============================================================
# HTTP connection pool (stdlib only, thread-safe)
# ============================================================

class HTTPPool:
    """HTTP/HTTPS connection pool с keep-alive.

    Каждый поток получает своё соединение (thread-local).
    При повторных запросах к тому же хосту переиспользует открытое соединение.
    """

    def __init__(self, timeout: int = 30, max_keepalive: int = 10):
        self.timeout = timeout
        self.max_keepalive = max_keepalive
        self._local = threading.local()

    def _get_conn(self, host: str, port: int, scheme: str):
        """Получить или создать соединение для текущего потока."""
        if not hasattr(self._local, 'conns'):
            self._local.conns = {}
        key = (host, port, scheme)
        conn = self._local.conns.get(key)
        if conn is None:
            if scheme == 'https':
                conn = http.client.HTTPSConnection(host, port, timeout=self.timeout)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
            self._local.conns[key] = conn
        return conn

    def request(self, method: str, url: str, headers: dict = None,
                body: bytes = None, timeout: int = None):
        """Выполнить HTTP запрос с переиспользованием соединения.

        Возвращает (status_code, headers_dict, body_bytes).
        При ошибке соединения создаёт новое соединение и пробует ещё раз.
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or 'localhost'
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query

        timeout = timeout or self.timeout
        path_bytes = urllib.parse.quote(path.encode('utf-8'), safe='/?&=%@+,;$')
        if isinstance(path_bytes, bytes):
            path = path_bytes.decode('ascii')

        for attempt in range(2):
            try:
                conn = self._get_conn(host, port, parsed.scheme)
                conn.timeout = timeout
                conn.request(method, path, body=body, headers=headers or {})
                resp = conn.getresponse()
                resp_body = resp.read()
                resp_headers = dict(resp.getheaders())
                return resp.status, resp_headers, resp_body
            except Exception as e:
                if attempt == 0:
                    key = (host, port, parsed.scheme)
                    if hasattr(self._local, 'conns'):
                        self._local.conns.pop(key, None)
                    continue
                raise

    def request_json(self, method: str, url: str, headers: dict = None,
                     json_data: dict = None, timeout: int = None):
        """JSON-запрос. Возвращает (status_code, parsed_json_dict)."""
        hdrs = dict(headers or {})
        hdrs.setdefault('Content-Type', 'application/json')
        body = json.dumps(json_data).encode('utf-8') if json_data else None
        status, resp_headers, resp_body = self.request(
            method, url, headers=hdrs, body=body, timeout=timeout
        )
        try:
            data = json.loads(resp_body.decode('utf-8'))
        except Exception:
            swallow.note('worker_pool.request_json:L521')
            data = {}
        return status, data

    def clear(self):
        """Закрыть все соединения текущего потока."""
        if hasattr(self._local, 'conns'):
            for key, conn in list(self._local.conns.items()):
                try:
                    conn.close()
                except Exception:
                    swallow.note('worker_pool.clear:L532')
                    pass
            self._local.conns.clear()

    def close_all(self):
        """Закрыть все соединения во всех потоках (вызов из главного потока)."""
        pass  # per-thread connections are GC'd when threads die


# Глобальный экземпляр HTTP-пула
HTTP_POOL = HTTPPool()

# Асинхронный HTTP-клиент (thread-safe для одного event loop)
ASYNC_HTTP_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(600.0, connect=10.0),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
)

async def async_request_json(method: str, url: str, headers: dict = None,
                              json_data: dict = None, timeout: int = None):
    """Асинхронный JSON-запрос через httpx. Возвращает (status_code, parsed_json_dict)."""
    hdrs = dict(headers or {})
    hdrs.setdefault('Content-Type', 'application/json')
    try:
        resp = await ASYNC_HTTP_CLIENT.request(method, url, headers=hdrs, json=json_data, timeout=timeout)
        data = resp.json() if resp.text else {}
        return resp.status_code, data
    except Exception:
        swallow.note('worker_pool.async_request_json:L560')
        return 0, {}


def http_request(method: str, url: str, headers: dict = None,
                 body: bytes = None, timeout: int = None):
    """HTTP запрос через глобальный пул соединений.

    Возвращает (status_code, headers_dict, body_bytes).
    """
    return HTTP_POOL.request(method, url, headers=headers, body=body, timeout=timeout)


def http_request_json(method: str, url: str, headers: dict = None,
                      json_data: dict = None, timeout: int = None):
    """JSON HTTP запрос через глобальный пул соединений.

    Возвращает (status_code, parsed_json_dict).
    """
    return HTTP_POOL.request_json(method, url, headers=headers, json_data=json_data, timeout=timeout)


def shutdown():
    """Graceful shutdown всех пулов."""
    logger.info("[worker_pool] Shutting down pools...")
    HIGH_POOL.shutdown(wait=False)
    LOW_POOL.shutdown(wait=False)
    SEARCH_POOL.shutdown(wait=False)
    _pool = _SQLITE_POOLS
    for p in _pool:
        try: p.close_all()
        except Exception:
            swallow.note('worker_pool.shutdown:L592')
            pass
    try:
        HTTP_POOL.close_all()
    except Exception:
        swallow.note('worker_pool.shutdown:L597')
        pass
    logger.info("[worker_pool] Pools shut down")
