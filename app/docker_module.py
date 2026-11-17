#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Docker Module — интегрированный модуль Docker Portable UI для Infinity AI.

Это не отдельно запускаемый скрипт, а FastAPI-модуль, подключаемый в main.py
через app.include_router(docker_router). Он:

  * загружает движок Docker UI (docker/Qwen_python_*.py) в память ОДИН раз
    (без субпроцесса и без внешнего запуска) и поднимает его внутренний
    HTTP-сервер на 127.0.0.1:<порт> в фоновом потоке;
  * проксирует GET/POST /docker{path} -> внутренний сервер (все HTTP-методы);
  * проксирует WebSocket (/?cid=... и /?logs=...) через raw-TCP проброс;
  * при старте main.py автоматически поднимает dockerd внутри WSL
    (start_daemon с флагом --userland-proxy-path, см. build_configs движка).

Доступ: http://localhost:<порт main.py>/docker/

Совместимо с паттерном прокси-модулей проекта (как hermes_module.integration.bridge).
"""

import os
import re as _re
import sys
import json
import time as _time
import importlib.util
import threading
import asyncio
import subprocess

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse

import logging
import swallow

# Второй контейнерный бэкенд (Podman). Импорт не должен валить модуль,
# если файла/зависимостей нет — Docker-режим остаётся рабочим.
try:
    import docker_backend
except Exception as _be:  # pragma: no cover - защитный импорт
    docker_backend = None
    logging.getLogger("docker_module").warning(f"docker_backend недоступен: {_be}")

logger = None
_logger_fallback = logging.getLogger("docker_module")


def _get_logger():
    global logger
    if logger is None:
        try:
            import server
            logger = server.logger
        except Exception:
            swallow.note('docker_module._get_logger:L47')
            logger = _logger_fallback
    return logger

# --------------------------------------------------------------------------
# Исполняемый каталог проекта (runjson/)
# --------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.abspath(__file__))
_DOCKER_DIR = os.path.join(_ROOT, "docker")
_UI_SCRIPT = None
# Каталог docker/ может отсутствовать (например, в переносимой сборке, где
# dockerd не входит в комплект). Раньше здесь был голый os.listdir без
# проверки — модуль падал на импорте и валил запуск всего приложения.
try:
    _DOCKER_ENTRIES = os.listdir(_DOCKER_DIR)
except OSError:
    swallow.note('docker_module.<module>:L63')
    _DOCKER_ENTRIES = []
for _f in _DOCKER_ENTRIES:
    if _f.startswith("Qwen_python_") and _f.endswith(".py"):
        _UI_SCRIPT = os.path.join(_DOCKER_DIR, _f)
        break

router = APIRouter()

_INNER_PORT = int(os.environ.get("DOCKER_UI_INNER_PORT", "8077"))
_INNER_HOST = "127.0.0.1"
_engine = None          # loaded UI module
_engine_lock = threading.Lock()
_inner_server = None    # http.server instance
_inner_thread = None
_active_port = _INNER_PORT  # фактический порт внутреннего сервера (может переехать)
_startup_lock = threading.Lock()
# --------------------------------------------------------------------------
# Хелперы
# --------------------------------------------------------------------------
def _log(msg, level="info"):
    try:
        lg = _get_logger()
        if level == "error":
            lg.error(f"[docker] {msg}")
        elif level == "warning":
            lg.warning(f"[docker] {msg}")
        else:
            lg.info(f"[docker] {msg}")
    except Exception:
        swallow.note('docker_module._log:L93')
        pass


def _load_engine():
    """Загружает движок Docker UI один раз в память.

    Замок нужен с тех пор, как ручки модуля ушли в пул потоков: без него два
    первых параллельных запроса исполняли модуль движка дважды.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        if not _UI_SCRIPT:
            raise RuntimeError("docker/Qwen_python_*.py не найден")
        spec = importlib.util.spec_from_file_location("docker_ui_engine", _UI_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _engine = mod
        _apply_backend(_engine)
        return _engine


def _apply_backend(eng=None):
    """Применяет выбранный в настройках бэкенд (docker/podman) к движку."""
    eng = eng or _engine
    if eng is None or docker_backend is None:
        return
    try:
        docker_backend.apply_engine(eng)
    except Exception as e:
        _log(f"не удалось применить бэкенд к движку: {e}", "warning")


def _engine_backend():
    """Фактический бэкенд движка: docker или podman."""
    if docker_backend is None:
        return "docker"
    try:
        wanted = docker_backend.get_backend()
    except Exception:
        swallow.note('docker_module._engine_backend:L143')
        return "docker"
    if wanted != "podman":
        return "docker"
    # Конфиг может остаться от прошлой установки: без CLI движок продолжает
    # работать на Docker, и UI не должен показывать несуществующий Podman.
    try:
        if not docker_backend.podman_installed():
            return "docker"
    except Exception:
        swallow.note('docker_module._engine_backend.L152')
        return "docker"
    try:
        if _engine is not None and str(getattr(_engine, "_ACTIVE_BACKEND", "")).lower() == "podman":
            return "podman"
    except Exception:
        swallow.note('docker_module._engine_backend:L158')
    return "podman"


def _run_sync(fn, *args, low=False, **kw):
    """Блокирующую работу — из event loop в пул потоков (low=True — в LOW_POOL).

    Замер (test_loop_block_heavy.py): GET /api/docker/settings звал get_status()
    прямо на цикле и держал его — /api/health в это время вырастал с 3.6 мс до
    357.8 мс, то есть за один запрос пользователя тормозили все остальные.
    try_start_daemon() на цикле был бы хуже: он ждёт демон десятками секунд.
    """
    import worker_pool
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(worker_pool.LOW_POOL if low else worker_pool.HIGH_POOL,
                                lambda: fn(*args, **kw))


def _ensure_engine():
    if _engine is None:
        return _load_engine()
    # Настройка backend может измениться на лету — применяем её к уже
    # загруженному движку (для docker это восстановление оригиналов).
    _apply_backend(_engine)
    return _engine


# --------------------------------------------------------------------------
# Внутренний сервер
# --------------------------------------------------------------------------
def _can_bind(port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        swallow.note('docker_module._can_bind:L148')
        return False
    finally:
        s.close()


def _probe_alive(port):
    """Проверяет, отвечает ли на порту живой HTTP-сервер (наш Docker UI)."""
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        s.sendall(b"GET /api/state HTTP/1.0\r\nHost: localhost\r\n\r\n")
        data = b""
        s.settimeout(2)
        while len(data) < 4096:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        s.close()
        # живой сервер отвечает HTTP + JSON со state, а не пустотой/мусором
        return b"200" in data.split(b"\r\n", 1)[0] and b"daemon" in data
    except Exception:
        swallow.note('docker_module._probe_alive:L171')
        return False


def _start_inner_server():
    """Поднимает внутренний ThreadingHTTPServer движка на 127.0.0.1:<порт>.

    Порт по умолчанию — _INNER_PORT (8077). Если он занят:
      * живым нашим сервером — считаем запущенным;
      * мёртвым слушателем (например, залипший wslrelay.exe в Windows при
        mirrored networking, убитый процесс и т.п.) — молча выбрать
        следующий свободный порт (8078..8097).
    """
    global _inner_server, _inner_thread, _active_port
    eng = _ensure_engine()
    with _startup_lock:
        if _inner_server is not None:
            return
        port = _INNER_PORT
        if not _can_bind(port):
            if _probe_alive(port):
                _log(f"внутренний Docker UI уже работает на {port}")
                _active_port = port
                return
            # порт держит мёртвый процесс — ищем свободный
            picked = None
            for cand in range(port + 1, port + 21):
                if _can_bind(cand):
                    picked = cand
                    break
            if picked is None:
                _log(f"не найден свободный порт для внутреннего Docker UI ({port}..{port+20})", "error")
                return
            _log(f"порт {port} занят мёртвым слушателем — внутренний Docker UI переехал на {picked}")
            port = picked
        try:
            Handler = eng.Handler
            srv = eng.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            srv.daemon_threads = True
            _inner_server = srv
            _active_port = port
            _inner_thread = threading.Thread(
                target=srv.serve_forever, daemon=True, name="docker-ui-inner"
            )
            _inner_thread.start()
            _log(f"внутренний Docker UI слушает 127.0.0.1:{port}")
        except Exception as e:
            _log(f"не удалось поднять внутренний сервер: {e}", "error")
            _inner_server = None
# --------------------------------------------------------------------------
# HTTP-проксирование
# --------------------------------------------------------------------------
def _strip_prefix(path: str):
    """Удаляет префикс /docker из пути, оставляя оригинальный путь UI."""
    if path == "/docker":
        return "/"
    if path.startswith("/docker/"):
        return path[len("/docker"):]
    return path


def _adapt_html(body: bytes) -> bytes:
    """Переписывает пути встроенного UI с корня на /docker/ при отдаче HTML.

    Встроенный фронтенд ходит на '/api/*', '/sw.js', '/manifest.json',
    '/icon.svg' и WebSocket на '/'. Так как модуль монтируется на /docker/,
    эти пути нужно переписать — иначе браузер на странице /docker будет
    обращаться в корень FastAPI.
    """
    b = body
    # v5.2: безусловный replace(b"/api/", …) портил любые ВНЕШНИЕ URL,
    # содержащие /api/ (например https://hub.docker.com/api/...). Заменяем
    # только корневые пути: /api/… в начале строки или после кавычки/пробела,
    # но не после буквы, цифры, точки или второго слэша.
    b = _re.sub(rb'(?<![\w.\-/])/api/', b'/docker/api/', b)
    b = b.replace(b"location.host+'/?'", b"location.host+'/docker/ws?'")
    b = b.replace(b"navigator.serviceWorker.register('/sw.js')",
                  b"navigator.serviceWorker.register('/docker/sw.js')")
    b = b.replace(b'href="/manifest.json"', b'href="/docker/manifest.json"')
    b = b.replace(b'href="/icon.svg"', b'href="/docker/icon.svg"')
    return b


async def _proxy_to_inner(request: Request, target_subpath: str):
    """Проксирует HTTP-запрос на внутренний сервер через httpx."""
    _start_inner_server()
    import httpx
    qs = request.url.query
    url = f"http://{_INNER_HOST}:{_active_port}{target_subpath}"
    if qs:
        url += "?" + qs
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host",)}
    body = await request.body()
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.request(
            method=request.method.lower(),
            url=url,
            headers=headers,
            content=body or None,
        )
    exclude = {"content-length", "content-encoding", "transfer-encoding", "connection",
               "content-security-policy", "x-frame-options", "cross-origin-opener-policy",
               "cross-origin-embedder-policy", "permissions-policy", "report-to", "nel"}
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in exclude}
    content_type = resp_headers.get("content-type", "").lower()
    if "text/html" in content_type:
        return HTMLResponse(_adapt_html(resp.content), status_code=resp.status_code, headers=resp_headers)
    return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)


@router.api_route("/docker", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def docker_root(request: Request):
    if request.method == "GET":
        # По умолчанию — объединённый UI: дизайн и возможности движка
        # (docker/Qwen_python_*.py) + наши «Топ-100» и «На Desktop».
        # ?ui=classic — прежняя page_docker.html как безопасный fallback.
        ui = (request.query_params.get("ui") or "").lower()
        if ui == "classic":
            return _render_docker_page()
        return _render_engine_page()
    return await _proxy_to_inner(request, "/")


# --------------------------------------------------------------------------
# Отдача страницы модуля: templates/page_docker.html (новый UI v6).
# Страница проходит webui_assets.inject() — получает единую тему приложения
# (theme.js + infinity_theme), design.css и loader расширений.
# --------------------------------------------------------------------------
_DOCKER_PAGE_PATH = os.path.join(_ROOT, "templates", "page_docker.html")
_docker_page_html = None
_docker_page_lock = threading.Lock()


def _render_docker_page():
    """Читает шаблон, инжектит общие стили/тему и кэширует результат."""
    global _docker_page_html
    with _docker_page_lock:
        if _docker_page_html is None:
            try:
                with open(_DOCKER_PAGE_PATH, encoding="utf-8") as f:
                    html = f.read()
            except OSError as e:
                _log(f"шаблон страницы не найден ({_DOCKER_PAGE_PATH}): {e}", "error")
                return HTMLResponse(
                    "<h1>Docker</h1><p>page_docker.html не найден — см. логи.</p>",
                    status_code=500,
                )
            try:
                from webui_assets import inject as _inject_ui, add_page_class as _add_pg
                html = _add_pg(html, "page_docker.html")
                html = _inject_ui(html)
            except Exception as e:
                _log(f"webui_assets не применился к /docker: {e}", "warning")
            _docker_page_html = html
    return HTMLResponse(_docker_page_html)


# --------------------------------------------------------------------------
# Слияние UI движка с фишками Infinity Search.
#
# Движок (Qwen_python_*.py) даёт современный дизайн и полный набор действий
# с Docker, но в нём нет наших «Топ-100» и «На Desktop». Добавляем их в
# отдаваемый HTML: пункты меню, секции-вью и JS, который живёт внутри IIFE
# движка (поэтому видит App, api, Forms, Toast, Views, esc, ico, attr).
# Патч применяется ДО _adapt_html, чтобы пути /api/ переписались на /docker/.
# --------------------------------------------------------------------------
# Тема движка (dark/light/sepia) берётся из основной темы Infinity.
# Скрипт стоит в <head> и применяет её до отрисовки, чтобы не было вспышки
# тёмной темы. Основной ключ — infinity_theme (html_theme — legacy).
_ENGINE_THEME_BOOT = """<script>(function(){try{
var k1='infinity_theme',k2='html_theme';
var v=String(localStorage.getItem(k1)||localStorage.getItem(k2)||'light').toLowerCase();
var th=(v==='dark'||v==='midnight')?'dark':(v==='sepia'?'sepia':'light');
document.documentElement.setAttribute('data-theme',th);
localStorage.setItem('dpu6.theme',JSON.stringify(th));
}catch(e){}})();</script>
"""

_ENGINE_EXTRA_CSS = """
<style>
#view-top100 .grid.g4{grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}
#view-top100 .card{transition:border-color .15s,transform .15s}
#view-top100 .card:hover{border-color:var(--acc,#6366f1)}
#view-desktop .card .bdg.ok{color:var(--ok)}
/* Иконка #i-box в ряде тем рисуется чёрным артефактом — не выводим её нигде.
   Сам <svg> оставляем, чтобы не сбить выравнивание пунктов меню. */
use[href="#i-box"]{display:none}
/* Карточка контейнера: убираем иконку-«коробку» и дубли терминала/логов —
   они есть в «Деталях». */
#ctBox .rc .rc-ico{display:none}
#ctBox .rc-a button[onclick*="Ct.term("],
#ctBox .rc-a button[onclick*="Ct.liveLogs("]{display:none}

/* ── Переключатель движка Docker / Podman ───────────────────────────── */
.bkswitch{
  display:inline-flex;align-items:center;position:relative;gap:0;
  padding:3px;border-radius:999px;background:var(--bg3,#1b1e27);
  border:1px solid var(--bd,#2a2f3a);cursor:pointer;user-select:none;
  font-size:12px;font-weight:600;line-height:1;white-space:nowrap;
}
.bkswitch .opt{
  position:relative;z-index:2;padding:6px 10px;border-radius:999px;
  color:var(--dim,#8b93a7);transition:color .15s;
}
.bkswitch .opt.on{color:#fff}
.bkswitch .knob{
  position:absolute;z-index:1;top:3px;bottom:3px;left:3px;width:calc(50% - 3px);
  border-radius:999px;background:linear-gradient(135deg,#4f8cff,#6d5efc);
  transition:transform .18s ease;box-shadow:0 2px 8px rgba(79,140,255,.35);
}
.bkswitch[data-backend="podman"] .knob{transform:translateX(100%)}
.bkswitch[data-backend="podman"] .knob{background:linear-gradient(135deg,#892ca0,#4f46e5)}
.bkcard .bk-row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.bkcard .bk-status{font-size:12.5px;line-height:1.55;color:var(--dim,#8b93a7)}
.bkcard .bk-status b{color:var(--txt,#e6eaf5)}
.bkcard .bk-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.bkcard .bk-progress{height:7px;border-radius:99px;background:var(--bg3,#1b1e27);overflow:hidden;margin-top:10px;display:none}
.bkcard .bk-progress.show{display:block}
.bkcard .bk-progress i{display:block;height:100%;width:0;background:linear-gradient(90deg,#4f8cff,#892ca0);transition:width .2s}
</style>
"""

_ENGINE_EXTRA_SECTIONS = """
      <!-- ───── TOP-100 (Infinity) ───── -->
      <section class="view" id="view-top100">
        <div class="page-h">
          <div><h1><svg><use href="#i-rocket"/></svg> Топ-100 контейнеров</h1>
          <p id="t100Sub">Популярные образы Docker Hub — установка в один клик</p></div>
          <div class="acts">
            <select id="t100Sort" onchange="__infTop.setSort()" style="width:auto">
              <option value="pulls">По скачиваниям</option>
              <option value="stars">По звёздам</option>
              <option value="name">По имени</option>
              <option value="cat">По категории</option>
            </select>
            <button class="btn" onclick="__infTop.refreshTop()"><svg><use href="#i-refresh"/></svg><span class="lbl">Обновить</span></button>
          </div>
        </div>
        <div class="row" style="gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
          <div class="input-ico" style="flex:1;min-width:180px;max-width:340px">
            <svg><use href="#i-search"/></svg>
            <input id="t100Search" placeholder="Поиск: nginx, база, мониторинг…" oninput="__infTop.renderTop()">
          </div>
          <div class="row" style="gap:6px;flex-wrap:wrap" id="t100Cats"></div>
        </div>
        <div class="grid g4" id="t100Grid"></div>
      </section>

      <!-- ───── DESKTOP (Infinity) ───── -->
      <section class="view" id="view-desktop">
        <div class="page-h">
          <div><h1><svg><use href="#i-monitor"/></svg> Приложения на Desktop</h1>
          <p>Ярлыки на рабочем столе для веб-интерфейсов запущенных контейнеров</p></div>
          <div class="acts">
            <button class="btn" onclick="__infTop.loadDesk(true)"><svg><use href="#i-refresh"/></svg><span class="lbl">Обновить</span></button>
          </div>
        </div>
        <div class="card" style="padding:12px;margin-bottom:12px">
          <div class="dim" style="font-size:12.5px;line-height:1.5">
            Для каждого опубликованного порта можно создать ярлык: он откроет
            <span class="mono">/docker-port/&lt;порт&gt;/</span> как отдельное приложение Infinity Search.
          </div>
        </div>
        <div class="stack" id="deskList"></div>
      </section>
"""

_ENGINE_BACKEND_SWITCH = """
      <div class="bkswitch" id="infBackendSwitch" data-backend="docker"
           onclick="__infBackend.toggle()" title="Контейнерный движок: Docker или Podman">
        <span class="opt on" data-b="docker">Docker</span>
        <span class="opt" data-b="podman">Podman</span>
        <i class="knob"></i>
      </div>
"""

_ENGINE_BACKEND_CARD = """
          <div class="card bkcard" id="infBackendCard">
            <div class="card-h"><h3><svg><use href="#i-layers"/></svg> Движок: Docker / Podman</h3></div>
            <div class="bk-row">
              <div class="bkswitch" id="infBackendSwitch2" data-backend="docker"
                   onclick="__infBackend.toggle()">
                <span class="opt on" data-b="docker">Docker</span>
                <span class="opt" data-b="podman">Podman</span>
                <i class="knob"></i>
              </div>
              <div class="bk-status" id="infBackendStatus">Проверка…</div>
            </div>
            <div class="bk-progress" id="infPodmanProgress"><i></i></div>
            <div class="bk-actions">
              <button class="btn sm" id="infPodmanInstall" onclick="__infBackend.install()">
                <svg><use href="#i-download"/></svg> Скачать Podman
              </button>
              <button class="btn sm" onclick="__infBackend.refresh()">
                <svg><use href="#i-refresh"/></svg> Обновить
              </button>
              <button class="btn sm ghost" onclick="__infBackend.openLogs()">
                <svg><use href="#i-file"/></svg> Логи
              </button>
            </div>
            <div class="note" style="margin-top:12px"><svg><use href="#i-info"/></svg>
              <span>Podman работает через собственный CLI и машину (Windows/WSL2).
              Docker-команды UI транслируются автоматически; переключение
              обратимо без перезапуска.</span>
            </div>
          </div>
"""

_ENGINE_EXTRA_JS = r"""
/* ═══ Infinity extras: Топ-100 + На Desktop ═══════════════════════════════ */
const InfinityExtras = {
  topItems: [], topCats: [], topLive: false, topLoaded: false, topLoading: false,
  topSort: 'pulls', topCat: '',
  fmt(n){
    n = Number(n || 0);
    if (n >= 1e9) return (n / 1e9).toFixed(1).replace('.0', '') + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace('.0', '') + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace('.0', '') + 'k';
    return String(n);
  },
  setSort(){
    const el = document.getElementById('t100Sort');
    this.topSort = (el && el.value) || 'pulls';
    this.loadTop(true);
  },
  refreshTop(){ this.loadTop(true, true); },
  async loadTop(force, refreshHub){
    if (this.topLoading) return;
    if (this.topLoaded && !force) { this.renderCats(); this.renderTop(); return; }
    this.topLoading = true;
    const grid = document.getElementById('t100Grid');
    if (grid) grid.innerHTML = '<div class="dim" style="grid-column:1/-1;padding:26px;text-align:center">Загрузка каталога…</div>';
    const sortEl = document.getElementById('t100Sort');
    const sort = (sortEl && sortEl.value) || this.topSort || 'pulls';
    this.topSort = sort;
    const r = await api('/api/hub/top?sort=' + encodeURIComponent(sort) + (refreshHub ? '&refresh=1' : ''), {silent: true});
    this.topLoading = false;
    if (!r || r.error) {
      if (grid) grid.innerHTML = '<div class="dim" style="grid-column:1/-1;padding:26px;text-align:center">Не удалось загрузить каталог: ' + esc((r && r.error) || 'нет ответа') + '</div>';
      return;
    }
    this.topItems = Array.isArray(r.items) ? r.items : [];
    this.topCats = Array.isArray(r.cats) ? r.cats : [];
    this.topLive = !!r.live;
    this.topLoaded = true;
    const sub = document.getElementById('t100Sub');
    if (sub) sub.textContent = 'Популярные образы Docker Hub — установка в один клик · ' + (this.topLive ? 'данные онлайн' : 'офлайн-каталог');
    this.renderCats();
    this.renderTop();
  },
  renderCats(){
    const box = document.getElementById('t100Cats');
    if (!box) return;
    const chips = this.topCats.map(c =>
      '<button class="btn xs ' + (this.topCat === c.name ? 'primary' : 'ghost') + '" data-cat="' + attr(c.name) + '">' + esc(c.name) + ' · ' + c.count + '</button>'
    ).join('');
    box.innerHTML = '<button class="btn xs ' + (this.topCat === '' ? 'primary' : 'ghost') + '" data-cat="">Все · ' + this.topItems.length + '</button>' + chips;
    box.querySelectorAll('[data-cat]').forEach(b => b.onclick = () => { this.topCat = b.dataset.cat; this.renderCats(); this.renderTop(); });
  },
  renderTop(){
    const grid = document.getElementById('t100Grid');
    if (!grid) return;
    const q = ((document.getElementById('t100Search') || {}).value || '').toLowerCase().trim();
    const L = this.topItems.filter(e => (!this.topCat || e.category === this.topCat) &&
      (!q || ((e.id || '') + ' ' + (e.name || '') + ' ' + (e.category || '') + ' ' + (e.desc || '')).toLowerCase().includes(q)));
    if (!L.length) {
      grid.innerHTML = '<div class="dim" style="grid-column:1/-1;padding:26px;text-align:center">Ничего не найдено</div>';
      return;
    }
    grid.innerHTML = L.map(e => {
      const ref = e.image || (e.repo ? e.repo + ':latest' : '');
      const letter = esc(((e.name || e.id || '?')[0] || '?').toUpperCase());
      const icoHtml = '<span style="font-weight:700">' + letter + '</span>'
        + (e.icon ? '<img src="https://cdn.simpleicons.org/' + attr(e.icon) + '" alt="" loading="lazy" style="position:absolute;inset:0;margin:auto;width:24px;height:24px;object-fit:contain" onerror="this.remove()">' : '');
      return '<div class="card" style="padding:14px;display:flex;flex-direction:column;gap:9px;min-height:190px">'
        + '<div class="row" style="gap:10px;align-items:flex-start">'
        + '<div style="position:relative;width:38px;height:38px;border-radius:10px;display:flex;align-items:center;justify-content:center;background:var(--bg3,rgba(127,127,127,.14));flex:0 0 auto">' + icoHtml + '</div>'
        + '<div style="flex:1;min-width:0"><div style="font-weight:700;display:flex;gap:6px;align-items:center;flex-wrap:wrap">' + esc(e.name || e.id)
        + (e.official ? '<span class="bdg acc">Official</span>' : '') + '</div>'
        + '<div class="dim" style="font-size:11.5px">' + esc(e.category || '') + ' · ★ ' + this.fmt(e.stars) + ' · ⬇ ' + this.fmt(e.pulls) + '</div></div></div>'
        + '<div class="dim" style="font-size:12px;line-height:1.45;flex:1">' + esc(e.desc_live || e.desc || '') + '</div>'
        + '<div class="mono dim" style="font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="' + attr(ref) + '">' + esc(ref) + '</div>'
        + '<div class="row" style="gap:6px;margin-top:auto"><button class="btn sm primary" data-install="' + attr(ref) + '"' + (ref ? '' : ' disabled') + '>' + ico('rocket') + 'Развернуть</button>'
        + (e.homepage ? '<a class="btn sm ghost" href="' + attr(e.homepage) + '" target="_blank" rel="noopener">Сайт</a>' : '') + '</div></div>';
    }).join('');
    grid.querySelectorAll('[data-install]').forEach(b => b.onclick = () => Forms.runModal(b.dataset.install));
  },
  async toDesktop(name, port){
    const label = name + ' :' + port;
    const res = await api('/api/desktop-app', {method: 'POST', body: {name: name, port: Number(port)}});
    if (res && (res.ok || res.desktop)) {
      const d = res.desktop || {};
      if (d.added) Toast.ok('«' + label + '» добавлено на Desktop' + (d.profile ? ' (' + d.profile + ')' : ''));
      else Toast.warn('Приложение создано, но иконку на Desktop добавить не удалось');
      return true;
    }
    Toast.err((res && res.error) || 'Не удалось создать приложение');
    return false;
  },
  pickDesktop(c){
    const ports = Views.portsOf(c);
    if (!ports.length) return Toast.warn('У контейнера нет опубликованных портов');
    if (ports.length === 1) return this.toDesktop(c.name, ports[0].host);
    Modal.open({title: 'На Desktop', icon: 'monitor',
      body: '<div class="stack" style="gap:6px">' + ports.map(p =>
        '<button class="btn" style="justify-content:flex-start;width:100%" data-desk="' + p.host + '">' + ico('monitor') + 'Порт :' + p.host + '</button>'
      ).join('') + '</div>',
      footer: '<button class="btn" onclick="Modal.close()">Закрыть</button>',
      onMount: (m) => { m.querySelectorAll('[data-desk]').forEach(el => el.onclick = () => { Modal.close(); this.toDesktop(c.name, el.dataset.desk); }); }});
  },
  decorateContainers(){
    const box = document.getElementById('ctBox');
    if (!box) return;
    const byId = {};
    (App.CT || []).forEach(c => byId[c.id] = c);
    const addBtn = (host, c, cls, label) => {
      const b = document.createElement('button');
      b.className = cls; b.dataset.desk = '1'; b.title = 'На Desktop';
      b.innerHTML = ico('monitor') + (label ? 'На Desktop' : '');
      b.onclick = (ev) => { ev.stopPropagation(); InfinityExtras.pickDesktop(c); };
      return b;
    };
    box.querySelectorAll('.rc[data-id]').forEach(card => {
      const c = byId[card.dataset.id]; if (!c || !Views.portsOf(c).length) return;
      const acts = card.querySelector('.rc-a');
      if (acts && !acts.querySelector('[data-desk]')) {
        const b = addBtn(0, c, 'btn sm', true);
        acts.insertBefore(b, acts.querySelector('.more') || null);
      }
    });
    box.querySelectorAll('table.tbl tbody tr[data-id]').forEach(tr => {
      const c = byId[tr.dataset.id]; if (!c || !Views.portsOf(c).length) return;
      const acts = tr.querySelector('td.acts .row');
      if (acts && !acts.querySelector('[data-desk]')) {
        const b = addBtn(0, c, 'btn xs icon', false);
        acts.insertBefore(b, acts.querySelector('.danger') || null);
      }
    });
  },
  async loadDesk(){
    const box = document.getElementById('deskList');
    if (!box) return;
    box.innerHTML = '<div class="dim" style="padding:20px;text-align:center">Загрузка контейнеров…</div>';
    const r = await api('/api/containers', {silent: true});
    let list = null;
    if (Array.isArray(r)) list = r;
    else if (r && Array.isArray(r.containers)) list = r.containers;
    else if (App.CT && App.CT.length) list = App.CT;
    if (!list) {
      box.innerHTML = '<div class="card dim" style="padding:22px;text-align:center">Не удалось получить список контейнеров</div>';
      return;
    }
    const rows = [];
    list.forEach(c => {
      const ports = (Views && Views.portsOf) ? Views.portsOf(c) : [];
      if (!ports.length) return;
      rows.push('<div class="card" style="padding:14px">'
        + '<div class="row" style="gap:10px;align-items:center;margin-bottom:9px;flex-wrap:wrap"><span class="bdg' + (c.state === 'running' ? ' ok' : '') + '">' + esc(c.state || '') + '</span>'
        + '<b>' + esc(c.name || '') + '</b><span class="dim mono" style="font-size:11.5px">' + esc(c.image || '') + '</span></div>'
        + '<div class="row" style="gap:6px;flex-wrap:wrap">'
        + ports.map(p => '<button class="btn sm" data-name="' + attr(c.name || 'container') + '" data-port="' + p.host + '">' + ico('monitor') + 'На Desktop · :' + p.host + '</button>'
            + '<button class="btn sm ghost" data-open="' + p.host + '">' + ico('ext') + 'Открыть</button>').join('')
        + '</div></div>');
    });
    if (!rows.length) {
      box.innerHTML = '<div class="card dim" style="padding:22px;text-align:center">Запустите контейнер с опубликованным портом, и здесь появятся кнопки для ярлыков на рабочем столе.</div>';
      return;
    }
    box.innerHTML = rows.join('');
    box.querySelectorAll('[data-open]').forEach(b => b.onclick = () => window.open('/docker-port/' + b.dataset.open + '/', '_blank'));
    box.querySelectorAll('[data-port]').forEach(b => b.onclick = async () => {
      b.disabled = true;
      await InfinityExtras.toDesktop(b.dataset.name, b.dataset.port);
      b.disabled = false;
    });
  }
};
window.__infTop = InfinityExtras;

/* Тема: следуем за основной темой приложения (infinity_theme).
   Движок знает только dark/light/sepia — остальные темы сводим к ним. */
function __infMainTheme(){
  try {
    const v = String(localStorage.getItem('infinity_theme') || localStorage.getItem('html_theme') || 'light').toLowerCase();
    if (v === 'dark' || v === 'midnight') return 'dark';
    if (v === 'sepia') return 'sepia';
    return 'light';
  } catch (e) { return 'light'; }
}
function __infApplyTheme(){
  const th = __infMainTheme();
  try { document.documentElement.setAttribute('data-theme', th); } catch (e) {}
  try { localStorage.setItem('dpu6.theme', JSON.stringify(th)); } catch (e) {}
  try { if (typeof UI !== 'undefined' && UI.setTheme) UI.setTheme(th, true); } catch (e) {}
}
try { __infApplyTheme(); } catch (e) {}
try {
  window.addEventListener('storage', (e) => {
    if (!e.key || String(e.key).indexOf('theme') >= 0) __infApplyTheme();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) __infApplyTheme(); });
} catch (e) {}

/* В движке portsOf() путает host/container: 0.0.0.0:8080->80/tcp
   отдавал host=80. Из-за этого чипы и «Открыть» вели на порт контейнера. */
Views.portsOf = function(c){
  const out = [], seen = new Set();
  const m = String(c.ports || '').matchAll(/(?:(\d+\.\d+\.\d+\.\d+):)?(\d+)->(\d+)\/(tcp|udp)/g);
  for (const x of m) {
    const p = {ip: x[1] || '0.0.0.0', host: x[2], container: x[3], proto: x[4]};
    /* Docker Desktop отдаёт IPv4 и IPv6-биндинги одного порта — не дублируем. */
    const key = p.host + ':' + p.container + '/' + p.proto;
    if (seen.has(key)) continue;
    seen.add(key); out.push(p);
  }
  return out;
};

/* «На Desktop» прямо в карточках/таблице контейнеров, как в старом UI. */
const __infOrigRenderContainers = Views.renderContainers;
Views.renderContainers = function(){
  const r = __infOrigRenderContainers.apply(this, arguments);
  try { InfinityExtras.decorateContainers(); } catch (e) {}
  return r;
};
const __infOrigPaintQuick = Ct.paintQuick;
Ct.paintQuick = function(){
  __infOrigPaintQuick.apply(this, arguments);
  try {
    const q = document.getElementById('ctQuick');
    if (!q || !this.cur || q.querySelector('[data-desk]')) return;
    const c = (App.CT || []).find(x => x.id === this.cur.id) || {name: this.cur.name};
    if (!Views.portsOf(c).length) return;
    const b = document.createElement('button');
    b.className = 'btn sm'; b.dataset.desk = '1'; b.title = 'На Desktop';
    b.innerHTML = ico('monitor') + 'На Desktop';
    b.onclick = () => InfinityExtras.pickDesktop(c);
    q.appendChild(b);
  } catch (e) {}
};
const __infCtBox = document.getElementById('ctBox');
if (__infCtBox && window.MutationObserver) {
  try {
    new MutationObserver(() => { try { InfinityExtras.decorateContainers(); } catch (e) {} })
      .observe(__infCtBox, {childList: true, subtree: true});
  } catch (e) {}
}
try { InfinityExtras.decorateContainers(); } catch (e) {}
/* Старый SW движка под /docker/ кэшировал live-API — снимаем регистрацию. */
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.getRegistrations().then(rs => rs.forEach(r => {
    if (String(r.scope || '').indexOf('/docker') >= 0) r.unregister();
  })).catch(() => {});
  if (window.caches && caches.keys) {
    caches.keys().then(ks => ks.forEach(k => { if (k === 'dpu-v6') caches.delete(k); })).catch(() => {});
  }
}
const __infOrigGo = App.go;
App.go = function(tab, skipPush){
  try { __infOrigGo.call(App, tab, skipPush); } catch (e) { console.error(e); }
  if (tab === 'top100') InfinityExtras.loadTop();
  if (tab === 'desktop') InfinityExtras.loadDesk();
};
if (App.tab === 'top100') InfinityExtras.loadTop();
if (App.tab === 'desktop') InfinityExtras.loadDesk();

/* ═══ Infinity extras: движок Docker ↔ Podman ═════════════════════════════ */
const __infBackend = {
  backend: 'docker', podman: {}, job: {}, busy: false, poll: null, wantPodman: false,

  /* Эндпоинты модуля живут в основном приложении, а не в движке: HTML
     страницы /docker переписывает '/api/' в '/docker/api/', поэтому путь
     собираем по частям — так запрос идёт в корень и минует rewrite. */
  host(){ return '/ap' + 'i/docker'; },

  /* Полная перерисовка переключателя, статуса и кнопки установки. */
  paint(){
    const p = this.podman || {};
    const switches = [
      document.getElementById('infBackendSwitch'),
      document.getElementById('infBackendSwitch2')
    ].filter(Boolean);
    switches.forEach(el => {
      el.dataset.backend = this.backend;
      el.querySelectorAll('.opt').forEach(o => {
        o.classList.toggle('on', o.dataset.b === this.backend);
      });
    });

    const st = document.getElementById('infBackendStatus');
    if (st){
      const lines = [];
      if (this.backend === 'podman'){
        if (p.installed){
          lines.push('<b>Podman ' + esc(p.version || '') + '</b> · ' +
            (p.running ? 'демон работает' : 'демон остановлен'));
          if (p.machine) lines.push('machine: <b>' + esc(p.machine) + '</b>');
          if (p.path) lines.push('<span class="mono">' + esc(p.path) + '</span>');
        } else {
          lines.push('<b>Podman не установлен.</b> Нажмите «Скачать Podman» — ' +
            'будет загружен podman-remote, после установки движок переключится автоматически.');
        }
      } else {
        lines.push('Активен <b>Docker</b>');
        if (p.installed) lines.push('Podman ' + esc(p.version || '') + ' доступен как альтернатива');
        else lines.push('Podman не установлен — можно скачать кнопкой ниже, не покидая страницу.');
      }
      st.innerHTML = lines.join('<br>');
    }

    const inst = document.getElementById('infPodmanInstall');
    if (inst) inst.style.display = p.installed ? 'none' : '';
    this.paintJob();
  },

  paintJob(){
    const box = document.getElementById('infPodmanProgress');
    if (!box) return;
    const j = this.job || {};
    const active = (j.state === 'downloading' || j.state === 'extracting');
    const show = active || j.state === 'error';
    box.classList.toggle('show', show);
    const bar = box.querySelector('i');
    if (bar) bar.style.width = (active ? Math.max(2, Number(j.percent || 0)) : 100) + '%';
    box.title = j.error || j.message || '';
  },

  async load(silent){
    const r = await api(this.host() + '/backend', {silent: true});
    if (!r || r.error){
      if (silent === false) Toast.err((r && r.error) || 'нет ответа от модуля Docker', 'Движок');
      return;
    }
    this.backend = r.backend || 'docker';
    this.podman = r.podman || {};
    this.job = r.job || {};
    this.paint();
    if (this.job && (this.job.state === 'downloading' || this.job.state === 'extracting')) this.startPoll();
  },

  async toggle(){
    if (this.busy) return;
    const target = this.backend === 'podman' ? 'docker' : 'podman';
    if (target === 'podman' && !((this.podman || {}).installed)){
      this.wantPodman = true;
      Toast.info('Podman не установлен — начинаю скачивание', 'Движок');
      this.install();
      return;
    }
    this.busy = true;
    try {
      const r = await api(this.host() + '/backend', {method: 'POST', body: {backend: target}, silent: true});
      if (!r || r.error){
        Toast.err((r && r.error) || 'не удалось переключить движок', 'Движок');
        return;
      }
      this.backend = r.backend || target;
      this.podman = r.podman || this.podman;
      this.paint();
      Toast.ok(this.backend === 'podman'
        ? 'Движок переключён на Podman'
        : 'Движок переключён на Docker', 'Движок');
      try { App.refresh(true); Dash.load(); } catch (e) {}
    } finally {
      this.busy = false;
    }
  },

  async install(){
    if (this.busy) return;
    this.busy = true;
    try {
      const r = await api(this.host() + '/podman/download', {method: 'POST', body: {}, silent: true});
      if (!r || r.error){
        Toast.err((r && r.error) || 'не удалось начать скачивание', 'Podman');
        this.wantPodman = false;
        return;
      }
      this.job = r.job || {state: 'downloading', percent: 0, message: 'Скачивание…'};
      if (this.job.state === 'download' || this.job.state === 'downloading')
        Toast.info('Скачивание Podman запущено', 'Podman');
      this.paint();
      this.startPoll();
    } finally {
      this.busy = false;
    }
  },

  startPoll(){
    this.stopPoll();
    this.poll = setInterval(() => { this.pollJob(); }, 1200);
  },

  stopPoll(){
    if (this.poll){ clearInterval(this.poll); this.poll = null; }
  },

  async pollJob(){
    const r = await api(this.host() + '/podman/status', {silent: true});
    if (!r || r.error) return;
    this.podman = r.podman || this.podman;
    this.job = r.job || {};
    this.paint();
    const j = this.job || {};
    if (j.state === 'done'){
      this.stopPoll();
      Toast.ok(j.message || 'Podman установлен', 'Podman');
      if (this.wantPodman){
        this.wantPodman = false;
        this.toggle();
      }
    } else if (j.state === 'error'){
      this.stopPoll();
      this.wantPodman = false;
      Toast.err(j.error || j.message || 'не удалось установить Podman', 'Podman');
    }
  },

  refresh(){ this.load(false); },

  openLogs(){
    const p = this.podman || {};
    const j = this.job || {};
    const rows = [
      ['Движок', this.backend === 'podman' ? 'Podman' : 'Docker'],
      ['Podman установлен', p.installed ? 'да' : 'нет'],
      ['Версия', p.version || '—'],
      ['CLI', p.path || '—'],
      ['Каталог', p.dir || '—'],
      ['Демон', p.running ? 'работает' : 'остановлен'],
      ['machine', p.machine || '—'],
      ['Задача', j.state || 'idle'],
      ['Сообщение', j.message || '—'],
      ['Ошибка', j.error || '—'],
    ];
    Modal.open({
      title: 'Движок: Podman / Docker',
      icon: 'info',
      body: '<pre class="term" style="max-height:52vh">' +
        esc(rows.map(x => x[0] + ': ' + x[1]).join('\n')) + '</pre>'
    });
  },
};
window.__infBackend = __infBackend;
try { __infBackend.load(true); } catch (e) {}
"""


def _merge_engine_extras(html: str) -> str:
    """Добавляет в HTML движка пункты «Топ-100» и «На Desktop»."""
    if not html or 'id="view-top100"' in html:
        return html
    nav_anchor = "    {id:'templates',label:'Каталог приложений',icon:'puzzle'},"
    if nav_anchor not in html:
        _log("не найден пункт NAV 'templates' — extras не добавлены", "warning")
        return html
    html = html.replace(
        nav_anchor,
        nav_anchor
        + "\n    {id:'top100',label:'Топ-100',icon:'rocket'},"
        + "\n    {id:'desktop',label:'На Desktop',icon:'monitor'},",
        1,
    )
    content_anchor = '<div class="content" id="content">'
    html = html.replace(content_anchor, content_anchor + _ENGINE_EXTRA_SECTIONS, 1)
    # Переключатель движка в топбаре (рядом с индикатором демона) и карточка
    # в Настройках. Если разметка движка изменится — просто не вставится.
    topbar_anchor = '<button class="dpill" id="daemonPill"'
    if topbar_anchor in html:
        html = html.replace(topbar_anchor, _ENGINE_BACKEND_SWITCH + topbar_anchor, 1)
    settings_anchor = ('<div class="card">\n'
                       '            <div class="card-h"><h3><svg><use href="#i-cpu"/>'
                       '</svg> Демон dockerd</h3></div>')
    if settings_anchor in html:
        html = html.replace(settings_anchor, _ENGINE_BACKEND_CARD + settings_anchor, 1)
    html = html.replace("</head>", _ENGINE_THEME_BOOT + _ENGINE_EXTRA_CSS + "</head>", 1)
    # Тема из конфига движка (engine-only) больше не перебивает основную тему
    # приложения; см. _ENGINE_THEME_BOOT и __infApplyTheme ниже.
    html = html.replace(
        "if(c.theme) UI.setTheme(c.theme,true);",
        "/* Infinity: тема синхронизируется с основной темой приложения */",
    )
    # Под /docker/ service worker движка кэширует live-API (его проверка
    # пути '/api/' не видит '/docker/api/') и ломает обновления. Не регистрируем.
    html = html.replace("navigator.serviceWorker.register('/sw.js')", "Promise.resolve()")
    script_pos = html.rfind("})();")
    if script_pos < 0:
        _log("не найден конец скрипта движка — JS extras не добавлены", "warning")
        return html
    # Extras выносим в ОТДЕЛЬНЫЙ <script> после скрипта движка. Вставка перед
    # этим '})();' попадала внутрь async function boot(), и переключатель
    # движка не появлялся, пока boot не завершит свои сетевые запросы (а при
    # медленном/зависшем API движка — не появлялся вовсе). Топ-уровневые
    # const/let классического скрипта видны следующему скрипту, поэтому
    # App/api/Toast/esc остаются доступны.
    close_pos = html.find("</script>", script_pos)
    if close_pos < 0:
        _log("не найден закрывающий </script> — JS extras не добавлены", "warning")
        return html
    close_pos += len("</script>")
    html = html[:close_pos] + "<script>\n" + _ENGINE_EXTRA_JS + "\n</script>" + html[close_pos:]
    return html


# UI из docker/Qwen_python_*.py (движок). Кэшируем как и classic: движок
# всё равно импортируется один раз за процесс, правки требуют перезапуска.
_engine_page_html = None
_engine_page_lock = threading.Lock()


def _render_engine_page():
    """Отдаёт собственный UI движка, адаптируя пути под /docker/."""
    global _engine_page_html
    with _engine_page_lock:
        if _engine_page_html is None:
            try:
                eng = _ensure_engine()
            except Exception as e:
                _log(f"движок Docker UI не загрузился: {e}", "error")
                return HTMLResponse(
                    "<h1>Docker</h1><p>UI движка не найден — см. логи.</p>",
                    status_code=500,
                )
            html = getattr(eng, "HTML", "") or ""
            if not html:
                return HTMLResponse(
                    "<h1>Docker</h1><p>В движке нет константы HTML.</p>",
                    status_code=500,
                )
            try:
                html = _merge_engine_extras(html)
            except Exception as e:
                _log(f"не удалось добавить extras в HTML движка: {e}", "warning")
            try:
                html = _adapt_html(html.encode("utf-8")).decode("utf-8")
            except Exception as e:
                _log(f"не удалось адаптировать HTML движка: {e}", "warning")
            _engine_page_html = html
    return HTMLResponse(
        _engine_page_html,
        headers={"Cache-Control": "no-store, max-age=0"},
    )



# --------------------------------------------------------------------------
# Docker Hub API — поиск, Топ-100, карточка репозитория.
# Живут ЗДЕСЬ, а не в движке: страница работает даже когда демон не поднят,
# плюс это чинит мёртвый endpoint старого search_hub движка.
# --------------------------------------------------------------------------
_HUB_UA = {"User-Agent": "InfinityDocker/2.0", "Accept": "application/json"}
_HUB_SEARCH_TTL = 300        # кэш поиска, с
_HUB_TOP_TTL = 6 * 3600      # кэш обогащения Топ-100, с (RAM + диск)
_hub_search_cache = {}       # key -> (ts, payload)
_hub_top_cache = None        # (ts, items)
_hub_lock = threading.Lock()


def _hub_data_dir():
    d = os.environ.get("INFINITY_DATA_DIR") or os.path.join(_ROOT, "..", "data")
    d = os.path.abspath(d)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        swallow.note('docker_module._hub_data_dir:L767')
        d = _ROOT
    return d


def _top_cache_path():
    return os.path.join(_hub_data_dir(), "docker_top_cache.json")


async def _hub_get_json(url, timeout=12.0):
    import httpx
    async with httpx.AsyncClient(headers=_HUB_UA, timeout=timeout,
                                 follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


@router.get("/docker/api/hub/search")
async def api_hub_search(q: str = "", page: int = 1, limit: int = 24):
    """Живой поиск образов на Docker Hub (v2 search API), кэш 5 минут.

    Формат совместим со старым /api/search (name/desc/stars/official)
    + новые поля (pulls, page, count).
    """
    import urllib.parse as _up
    q = (q or "").strip()
    if not q:
        return {"results": [], "q": "", "page": 1}
    try:
        page = max(1, int(page))
        limit = min(max(1, int(limit)), 50)
    except (TypeError, ValueError):
        swallow.note('docker_module.api_hub_search:L800')
        page, limit = 1, 24
    key = f"{q.lower()}|{page}|{limit}"
    now = _time.time()
    with _hub_lock:
        hit = _hub_search_cache.get(key)
    if hit and now - hit[0] < _HUB_SEARCH_TTL:
        return hit[1]
    url = ("https://hub.docker.com/v2/search/repositories/?query="
           + _up.quote(q) + f"&page={page}&page_size={limit}")
    try:
        data = await _hub_get_json(url, timeout=15.0)
    except Exception as e:
        _log(f"hub search error: {e}", "warning")
        return Response(
            content=json.dumps({"error": f"Docker Hub недоступен: {e}"},
                               ensure_ascii=False),
            media_type="application/json",
        )
    results = []
    for i in data.get("results") or []:
        name = i.get("repo_name") or ""
        if not name:
            continue
        results.append({
            "name": name,
            "desc": (i.get("short_description") or "")[:200],
            "stars": i.get("star_count") or 0,
            "pulls": i.get("pull_count") or 0,
            "official": bool(i.get("is_official")),
        })
    payload = {"results": results, "q": q, "page": page,
               "count": data.get("count", len(results))}
    with _hub_lock:
        _hub_search_cache[key] = (now, payload)
    return payload


async def _enrich_top(items, concurrency=10):
    """Обогащает записи каталога живыми stars/pulls с Docker Hub.

    Возвращает обогащённый список или None, если НИ ОДИН запрос не прошёл
    (полный офлайн — вызывающий использует статику/диск-кэш).
    """
    import httpx
    sem = asyncio.Semaphore(concurrency)
    ok_any = False

    async def one(entry):
        nonlocal ok_any
        repo = entry.get("repo")
        if not repo:
            return entry
        async with sem:
            try:
                data = await _hub_get_json(
                    f"https://hub.docker.com/v2/repositories/{repo}/", timeout=8.0)
            except Exception:
                swallow.note('docker_module.one:L858')
                return entry
        if not isinstance(data, dict):
            return entry
        out = dict(entry)
        if isinstance(data.get("star_count"), int):
            out["stars"] = data["star_count"]
        if isinstance(data.get("pull_count"), int):
            out["pulls"] = data["pull_count"]
        desc = (data.get("description") or "").strip()
        if desc:
            out["desc_live"] = desc[:220]
        cats = data.get("categories")
        if isinstance(cats, list) and cats and isinstance(cats[0], dict):
            out["hub_category"] = str(cats[0].get("name") or "")[:60]
        out["live"] = True
        ok_any = True
        return out

    results = await asyncio.gather(*(one(e) for e in items))
    return list(results) if ok_any else None


def _sort_top(items, sort):
    if sort == "stars":
        return sorted(items, key=lambda e: e.get("stars", 0), reverse=True)
    if sort == "name":
        return sorted(items, key=lambda e: (e.get("name") or "").lower())
    if sort == "cat":
        return sorted(items, key=lambda e: (e.get("category") or "",
                                            (e.get("name") or "").lower()))
    return sorted(items, key=lambda e: e.get("pulls", 0), reverse=True)


@router.get("/docker/api/hub/top")
async def api_hub_top(sort: str = "pulls", refresh: int = 0):
    """Топ-100 контейнеров: каталог + живые звёзды/качки с Docker Hub.

    Кэш: RAM 6 ч + data/docker_top_cache.json. При полном отсутствии сети
    отдаёт статику каталога (offline-fallback). refresh=1 принудительно
    перезапрашивает Hub.
    """
    import docker_catalog
    global _hub_top_cache
    now = _time.time()
    with _hub_lock:
        cached = _hub_top_cache
    if cached and not refresh and now - cached[0] < _HUB_TOP_TTL:
        items, cached_at, live = cached[1], cached[0], True
    else:
        # Диск-кэш — страховка при отказе сети.
        disk_items, disk_ts = None, 0.0
        try:
            with open(_top_cache_path(), encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict) and isinstance(disk.get("items"), list):
                disk_items = disk["items"]
                disk_ts = float(disk.get("ts") or 0)
        except Exception:
            swallow.note('docker_module.api_hub_top:L917')
            pass
        base = docker_catalog.get_top100(limit=0)
        try:
            items = await _enrich_top(base)
        except Exception as e:
            _log(f"top enrichment error: {e}", "warning")
            items = None
        if items is None:  # офлайн
            items = disk_items or base
            cached_at = disk_ts or 0
            live = False
        else:
            cached_at = now
            live = True
            with _hub_lock:
                _hub_top_cache = (now, items)
            try:  # атомарная запись диск-кэша
                tmp = _top_cache_path() + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"ts": now, "items": items}, f, ensure_ascii=False)
                os.replace(tmp, _top_cache_path())
            except Exception as e:
                _log(f"не удалось записать disk-кэш топа: {e}", "warning")
    sort = str(sort or "pulls").lower()
    return {
        "items": _sort_top(items, sort),
        "cats": docker_catalog.categories(),
        "live": live,
        "cached_at": cached_at,
    }


@router.get("/docker/api/hub/repo")
async def api_hub_repo(ref: str = ""):
    """Карточка репозитория Hub: описание, звёзды, теги (для модалки установки)."""
    ref = (ref or "").strip().strip("/")
    if not ref or ".." in ref or " " in ref or ref.count("/") > 1:
        return Response(content='{"error": "bad ref"}', status_code=400,
                        media_type="application/json")
    hub_ref = ref if "/" in ref else "library/" + ref
    try:
        info = await _hub_get_json(f"https://hub.docker.com/v2/repositories/{hub_ref}/")
        tags = []
        try:
            tdata = await _hub_get_json(
                f"https://hub.docker.com/v2/repositories/{hub_ref}/tags?page_size=30")
            tags = [t.get("name") for t in (tdata.get("results") or [])
                    if isinstance(t, dict) and t.get("name")]
        except Exception:
            swallow.note('docker_module.api_hub_repo:L967')
            pass
    except Exception as e:
        return Response(
            content=json.dumps({"error": f"Docker Hub недоступен: {e}"},
                               ensure_ascii=False),
            media_type="application/json",
        )
    return Response(content=json.dumps({
        "name": info.get("name") or ref.split("/")[-1],
        "namespace": info.get("namespace") or "",
        "description": (info.get("description") or "")[:500],
        "stars": info.get("star_count") or 0,
        "pulls": info.get("pull_count") or 0,
        "last_updated": info.get("last_updated") or "",
        "tags": tags,
    }, ensure_ascii=False), media_type="application/json")


# --------------------------------------------------------------------------
# Создание Desktop-приложений из контейнеров (type=link -> WinBox на /desktop)
# --------------------------------------------------------------------------
def _apps_dir():
    try:
        import server
        for d in getattr(server, "APPS_DIRS", []) or []:
            if os.path.isdir(d):
                return d
    except Exception:
        swallow.note('docker_module._apps_dir:L996')
        pass
    return os.path.join(_ROOT, "app")


def _docker_app_dir():
    d = _apps_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _request_profile_id(request: Request) -> str:
    """Профиль из запроса (cookie infinity_profile), иначе default."""
    try:
        import desktop_api as _da
        return _da._resolve_profile_id(request)
    except Exception:
        swallow.note('docker_module._request_profile_id:L1013')
        return "default"


async def _create_desktop_app(request: Request):
    try:
        body = await request.json()
    except Exception:
        return Response(content='{"error": "invalid json"}', status_code=400, media_type="application/json")
    name = str(body.get("name") or "Container").strip()[:60]
    try:
        port = int(body.get("port"))
        if not (1 <= port <= 65535):
            raise ValueError
    except (ValueError, TypeError):
        return Response(content='{"error": "некорректный порт"}', status_code=400, media_type="application/json")
    icon = str(body.get("icon") or "🐳")[:8]
    safe = _re.sub(r"[^a-zA-Z0-9а-яА-Я_-]+", "_", name).strip("_")[:40] or "container"
    folder = f"docker_{safe}_{port}"
    app_dir = os.path.join(_docker_app_dir(), folder)
    try:
        os.makedirs(app_dir, exist_ok=True)
        import json as _json
        with open(os.path.join(app_dir, "manifest.json"), "w", encoding="utf-8") as f:
            _json.dump({
                "name": f"{name} :{port}",
                "description": f"Контейнер Docker {name} (порт {port})",
                "type": "link",
                "url": f"/docker-port/{port}/",
                "icon": icon,
                "category": "Docker",
                "windowWidth": 1200,
                "windowHeight": 800,
            }, f, ensure_ascii=False, indent=2)
        # Совместимость: некоторые потребители ищут именно index.html.
        # type=link всё равно открывается по url.
        import html as _html
        with open(os.path.join(app_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(
                "<!doctype html><meta charset=\"utf-8\">"
                "<meta http-equiv=\"refresh\" content=\"0;url=/docker-port/%d/\">"
                "<title>%s</title><script>location.replace('/docker-port/%d/');</script>"
                % (port, _html.escape(f"{name} :{port}"), port)
            )
    except Exception as e:
        _log(f"не удалось создать приложение {folder}: {e}", "error")
        return Response(
            content=json.dumps({"ok": False, "error": f"не удалось создать приложение: {e}"},
                               ensure_ascii=False),
            status_code=500, media_type="application/json")

    pid = _request_profile_id(request)
    added = _sync_desktop_icon(folder, name, port, pid=pid)
    return Response(content=json.dumps({
        "ok": True,
        "id": folder,
        "desktop": {"added": bool(added), "profile": pid},
    }, ensure_ascii=False), media_type="application/json")


def _sync_desktop_icon(app_id: str, name: str, port: int, pid: str = "default") -> bool:
    """Добавляет иконку приложения в состояние рабочего стола профиля pid.

    Desktop-модуль (page_desktop.html) рисует иконки только из
    /api/desktop/state — без этого шага созданное из Docker-контейнера
    приложение не появилось бы на рабочем столе.
    """
    try:
        import desktop_api as _da
        state = _da.load_state(pid=pid)
        icons = state.get("icons") or []
        # дубликат — приложение уже на десктопе
        for ic in icons:
            if str(ic.get("id") or "") == app_id or str(ic.get("target") or "") == f"/docker-port/{port}/":
                return True
        n = len(icons)
        icons.append({
            "id": app_id,
            "type": "shortcut",
            "name": f"{name} :{port}",
            "icon": "fa-external-link-alt",
            "target": f"/docker-port/{port}/",
            "targetType": "shortcut",
            "x": 60 + (n % 8) * 90,
            "y": 60 + (n // 8) * 90,
            "folderId": "",
            "size": 1,
        })
        state["icons"] = icons
        _da.save_state(state, pid=pid)
        _log(f"приложение «{name} :{port}» добавлено иконкой на Desktop ({pid}, {app_id})")
        return True
    except Exception as e:
        _log(f"не удалось добавить иконку на Desktop для {app_id}: {e}", "warning")
        return False


def _unsync_desktop_icon(app_id: str, pid: str = "default"):
    """Убирает иконку Docker-приложения с рабочего стола профиля pid."""
    try:
        import desktop_api as _da
        state = _da.load_state(pid=pid)
        icons = state.get("icons") or []
        kept = [ic for ic in icons if str(ic.get("id") or "") != app_id]
        if len(kept) != len(icons):
            state["icons"] = kept
            _da.save_state(state, pid=pid)
    except Exception as e:
        _log(f"не удалось убрать иконку с Desktop для {app_id}: {e}", "warning")


async def _remove_desktop_app(request: Request):
    try:
        body = await request.json()
    except Exception:
        return Response(content='{"error": "invalid json"}', status_code=400, media_type="application/json")
    app_id = str(body.get("id") or "")
    if not app_id.startswith("docker_") or "/" in app_id or ".." in app_id:
        return Response(content='{"error": "bad id"}', status_code=400, media_type="application/json")
    import shutil
    shutil.rmtree(os.path.join(_docker_app_dir(), app_id), ignore_errors=True)
    _unsync_desktop_icon(app_id, pid=_request_profile_id(request))
    return Response(content='{"ok": true}', media_type="application/json")


def _task_load_image(tid, eng, path):
    """Загрузить образ из .tar и удалить временный файл после завершения."""
    try:
        eng.task_load(tid, path)
    finally:
        try:
            os.remove(path)
        except OSError:
            swallow.note('docker_module._task_load_image:L1146')
            pass


@router.post("/docker/api/images/upload")
async def docker_upload_image(file: UploadFile = File(...)):
    """Загрузка образа из браузерного файла (.tar) через docker load.

    Файл принимаем потоково на диск (а не base64 в JSON), затем запускаем
    штатную задачу движка; прогресс доступен через /docker/api/task/<id>.
    """
    name = os.path.basename(str(file.filename or "image.tar"))
    if not _re.search(r"\.(tar|tar\.gz|tgz)$", name, _re.I):
        return Response(content=json.dumps({"ok": False, "error": "нужен файл .tar"},
                                          ensure_ascii=False),
                        status_code=400, media_type="application/json")
    tmpdir = os.path.join(_ROOT, "data", "docker_uploads")
    os.makedirs(tmpdir, exist_ok=True)
    safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120] or "image.tar"
    path = os.path.join(tmpdir, f"{int(_time.time())}_{safe}")
    max_size = 32 * 1024 * 1024 * 1024  # 32 GiB
    size = 0
    try:
        with open(path, "wb") as out:
            while True:
                chunk = await file.read(4 * 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_size:
                    raise ValueError("файл больше 32 ГБ")
                out.write(chunk)
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            swallow.note('docker_module.docker_upload_image:L1182')
            pass
        return Response(content=json.dumps({"ok": False, "error": str(e)},
                                          ensure_ascii=False),
                        status_code=400, media_type="application/json")
    finally:
        try:
            await file.close()
        except Exception:
            swallow.note('docker_module.docker_upload_image:L1191')
            pass
    try:
        eng = _ensure_engine()
        tid = eng.run_task(_task_load_image, eng, path)
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            swallow.note('docker_module.docker_upload_image:L1200')
            pass
        return Response(content=json.dumps({"ok": False, "error": str(e)},
                                          ensure_ascii=False),
                        status_code=500, media_type="application/json")
    return Response(content=json.dumps({"ok": True, "task": tid, "size": size,
                                       "name": name}, ensure_ascii=False),
                    media_type="application/json")


@router.api_route("/docker/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def docker_proxy(request: Request, path: str):
    sub = _strip_prefix("/docker/" + path)
    # v6: /docker/ и /docker/index.html отдаём новой страницей (в старом UI
    # этот путь уходил в движок и возвращал встроенный HTML).
    if request.method == "GET" and sub in ("/", "/index.html"):
        ui = (request.query_params.get("ui") or "").lower()
        if ui == "classic":
            return _render_docker_page()
        return _render_engine_page()
    if sub.lstrip("/") == "api/desktop-app":
        if request.method == "POST":
            return await _create_desktop_app(request)
        if request.method == "DELETE":
            return await _remove_desktop_app(request)
    return await _proxy_to_inner(request, sub)
# --------------------------------------------------------------------------
# Прокси портов контейнеров: /docker-port/{port}/... -> http://127.0.0.1:{port}
#
# dockerd публикует порты контейнеров внутри WSL, поэтому Windows-браузер
# часто не может открыть http://localhost:<порт контейнера> напрямую.
# Зато http://<хост main.py>/docker/ из Windows уже работает — проксируем
# порты контейнеров через тот же канал.
# --------------------------------------------------------------------------
_FORBIDDEN_PORTS = {_INNER_PORT}

# href="/..." src="/..." action="/..." poster="/..." — но не "//host"
_ABS_ATTR_RE = _re.compile(
    r'((?:href|src|action|poster|formaction)\s*=\s*)(["\'])/(?!/)([^"\']*)\2'
)
# url(/...) в CSS — но не url(// и url(data:
_CSS_URL_RE = _re.compile(r'url\(\s*(["\']?)/(?!/)([^)\["\']*)\1?\s*\)')
# srcset="/a.png 1x, /b.png 2x"
_SRCSET_RE = _re.compile(r'(srcset\s*=\s*)(["\'])([^"\']*)\2')
# <meta http-equiv="refresh" content="0; url=/...">
_META_REFRESH_RE = _re.compile(
    r'(<meta\s[^>]*http-equiv\s*=\s*["\']refresh["\'][^>]*content\s*=\s*["\'])([^"\']*)(["\'])'
)


# @import "/x.css" / @import '/x.css' в CSS (не url()-форма)
_CSS_IMPORT_RE = _re.compile(r'(@import\s*)(["\'])(/(?!/)[^"\']+)(\2)')
# атрибуты без кавычек: src=/app.js
_UNQUOTED_ATTR_RE = _re.compile(r'((?:href|src|action|poster|formaction)\s*=\s*)(/(?!/)[^\s"\'<>]+)')


def _rewrite_abs_paths(body: bytes, port: int, _kinds):
    """Переписывает абсолютные корневые пути контейнера на префикс прокси."""
    prefix = f"/docker-port/{port}/"
    text = body.decode("utf-8", errors="replace")
    text = _ABS_ATTR_RE.sub(
        lambda m: m.group(1) + m.group(2) + prefix + m.group(3) + m.group(2), text
    )
    text = _CSS_IMPORT_RE.sub(
        lambda m: m.group(1) + m.group(2) + prefix + m.group(3).lstrip("/") + m.group(4), text
    )
    text = _UNQUOTED_ATTR_RE.sub(
        lambda m: m.group(1) + '"' + prefix + m.group(2).lstrip("/") + '"', text
    )
    text = _CSS_URL_RE.sub(lambda m: "url(" + m.group(1) + prefix + m.group(2) + m.group(1) + ")", text)
    # srcset="url 1x, url 2x" — каждый URL в списке
    text = _SRCSET_RE.sub(
        lambda m: m.group(1) + m.group(2) + ", ".join(
            (prefix + t.strip().lstrip("/")) if (t.strip().startswith("/") and not t.strip().startswith("//")) else t.strip()
            for t in m.group(3).split(",")
        ) + m.group(2),
        text,
    )
    # <meta http-equiv="refresh" content="0;url=/...">
    def _meta_refresh(m):
        c = m.group(2)
        mm = _re.match(r'([^;]*;\s*url\s*=\s*["\']?)(/[^/].*)', c, _re.IGNORECASE)
        if mm:
            c = mm.group(1) + prefix + mm.group(2).lstrip("/")
        return m.group(1) + c + m.group(3)
    text = _META_REFRESH_RE.sub(_meta_refresh, text)
    return text.encode("utf-8")


_RUNTIME_PATCH = """
<script>(function(){
if(window.__dockerPortProxy)return;window.__dockerPortProxy=1;
var P='/docker-port/PORT/';
function fix(u){try{
 if(typeof u!=='string'||!u)return u;
 if(u.charAt(0)!=='/')return u;
 if(u.indexOf(P)===0||u.indexOf('//')===0)return u;
 if(u.indexOf('data:')===0||u.indexOf('blob:')===0||u.indexOf('#')===0)return u;
 return P+u.slice(1);
}catch(e){return u}}
function fixOpts(v){return v}
var _f=window.fetch;
window.fetch=function(i,o){try{
 if(typeof i==='string')i=fix(i);
 else if(i&&i.url)i=new Request(fix(i.url),i);
}catch(e){}return _f.call(this,i,o)};
var _xo=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(m,u){try{arguments[1]=fix(u)}catch(e){}return _xo.apply(this,arguments)};
var _so=Element.prototype.setAttribute;
Element.prototype.setAttribute=function(n,v){try{
 if(typeof v==='string'){
  if(n==='src'||n==='href'||n==='action'||n==='poster')v=fix(v);
  else if(n==='srcset')v=v.split(',').map(function(p){var t=p.trim().split(/\\s+/);t[0]=fix(t[0]);return t.join(' ')}).join(', ');
 }
}catch(e){}return _so.call(this,n,v)};
['HTMLImageElement','HTMLScriptElement','HTMLLinkElement','HTMLIFrameElement','HTMLSourceElement','HTMLMediaElement','HTMLEmbedElement','HTMLTrackElement'].forEach(function(cn){
 try{var proto=window[cn]&&window[cn].prototype;if(!proto)return;
 var d=Object.getOwnPropertyDescriptor(proto,'src');if(d&&d.set)Object.defineProperty(proto,'src',{set:function(v){d.set.call(this,fix(v))},get:d.get,configurable:true});
 }catch(e){}
});
try{var ha=Object.getOwnPropertyDescriptor(HTMLAnchorElement.prototype,'href');
 if(ha&&ha.set)Object.defineProperty(HTMLAnchorElement.prototype,'href',{set:function(v){ha.set.call(this,fix(v))},get:ha.get,configurable:true});
}catch(e){}
try{var fa=Object.getOwnPropertyDescriptor(HTMLFormElement.prototype,'action');
 if(fa&&fa.set)Object.defineProperty(HTMLFormElement.prototype,'action',{set:function(v){fa.set.call(this,fix(v))},get:fa.get,configurable:true});
}catch(e){}
try{var _sr=navigator.serviceWorker&&navigator.serviceWorker.register.bind(navigator.serviceWorker);
 if(_sr)navigator.serviceWorker.register=function(u,o){try{u=fix(u);if(o&&typeof o.scope==='string')o.scope=fix(o.scope)}catch(e){}return _sr(u,o)}}catch(e){}
try{var lh=Object.getOwnPropertyDescriptor(HTMLLinkElement.prototype,'href');
 if(lh&&lh.set)Object.defineProperty(HTMLLinkElement.prototype,'href',{set:function(v){lh.set.call(this,fix(v))},get:lh.get,configurable:true})}catch(e){}
var _wo=window.open;
window.open=function(u,n,f){return _wo.call(window,fix(u),n,f)};
try{var _ps=history.pushState,_rs=history.replaceState;
 history.pushState=function(s,t,u){try{u=fix(u)}catch(e){}return _ps.call(history,s,t,u)};
 history.replaceState=function(s,t,u){try{u=fix(u)}catch(e){}return _rs.call(history,s,t,u)};
}catch(e){}
})();
</script>
"""


def _rewrite_json_paths(obj, prefix):
    """Рекурсивно префиксирует абсолютные пути "/" в JSON-структурах контейнера."""
    if isinstance(obj, dict):
        return {k: _rewrite_json_paths(v, prefix) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite_json_paths(v, prefix) for v in obj]
    if isinstance(obj, str) and obj.startswith("/") and not obj.startswith("//") \
            and not obj.startswith(prefix) and not obj.startswith("/docker-port/"):
        return prefix + obj.lstrip("/")
    return obj


def _inject_runtime_patch(body: bytes, port: int) -> bytes:
    """Вставляет runtime-патч в HTML: fetch/XHR/src/href/history внутри iframe
    контейнера получают префикс /docker-port/<port>/ для абсолютных путей."""
    text = body.decode("utf-8", errors="replace")
    if "__dockerPortProxy" in text:
        return body
    snippet = _RUNTIME_PATCH.replace("PORT", str(port))
    m = _re.search(r"<head[^>]*>", text, _re.IGNORECASE)
    if m:
        i = m.end()
        return (text[:i] + snippet + text[i:]).encode("utf-8")
    m = _re.search(r"<html[^>]*>", text, _re.IGNORECASE)
    if m:
        i = m.end()
        return (text[:i] + snippet + text[i:]).encode("utf-8")
    return (snippet + text).encode("utf-8")


@router.api_route("/docker-port/{port}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@router.api_route("/docker-port/{port}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def docker_port_proxy(request: Request, port: str, path: str = ""):
    try:
        port_i = int(port)
        if not (1 <= port_i <= 65535):
            raise ValueError
    except ValueError:
        return Response(
            content='{"error": "некорректный порт"}',
            status_code=400, media_type="application/json",
        )

    # защита от петли: нельзя проксировать на порт самого main.py или UI
    host_hdr = request.headers.get("host", "")
    try:
        incoming_port = int(host_hdr.rsplit(":", 1)[1]) if ":" in host_hdr else 80
    except ValueError:
        swallow.note('docker_module.docker_port_proxy:L1389')
        incoming_port = 80
    if port_i in _FORBIDDEN_PORTS or port_i == _active_port or port_i == incoming_port:
        return Response(
            content='{"error": "этот порт занят самим приложением — проксирование запрещено"}',
            status_code=400, media_type="application/json",
        )

    import httpx
    qs = request.url.query
    url = f"http://127.0.0.1:{port_i}/{path}"
    if qs:
        url += "?" + qs
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host",)}
    # Критично: просим у контейнера НЕСЖАТОЕ тело. Если пропустить браузерный
    # Accept-Encoding (br/zstd), контейнер ответит brotli, httpx может не
    # распаковать, заголовок content-encoding мы срезаем — и браузер получает
    # сжатый мусор вместо JS/CSS (SyntaxError, сломанные стили).
    headers["accept-encoding"] = "identity"
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            resp = await client.request(
                method=request.method.lower(),
                url=url,
                headers=headers,
                content=body or None,
            )
    except httpx.ConnectError:
        return Response(
            content=(
                '{"error": "порт ' + str(port_i) + ' недоступен — контейнер не запущен '
                'или порт не опубликован (-p хост:контейнер). WebSocket-приложения '
                'через этот прокси не работают: используйте кнопку 🌐 (WSL IP)."}'
            ),
            status_code=502, media_type="application/json",
        )
    except Exception as e:
        return Response(
            content='{"error": "' + str(e).replace('"', "'") + '"}',
            status_code=502, media_type="application/json",
        )
    exclude = {"content-length", "content-encoding", "transfer-encoding", "connection",
               "content-security-policy", "x-frame-options", "cross-origin-opener-policy",
               "cross-origin-embedder-policy", "permissions-policy", "report-to", "nel"}
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in exclude}
    content_type = resp_headers.get("content-type", "").lower()

    # Для текстового контента контейнера отключаем переиспользование кэша:
    # иначе браузер может навсегда reuse'ить устаревшую/битую копию через 304
    _no_cache_ct = ("text/html", "text/css", "javascript", "json")
    if any(x in content_type for x in _no_cache_ct):
        resp_headers.pop("etag", None)
        resp_headers.pop("last-modified", None)
        resp_headers["cache-control"] = "no-cache"

    # manifest.json контейнера: переписываем абсолютные пути на префикс прокси
    # (иконки/scope/start_url иначе уходят на корень main.py и дают 404)
    if "json" in content_type and path.rstrip("/").endswith("manifest.json"):
        try:
            import json as _json
            _data = _json.loads(resp.content)
            _data = _rewrite_json_paths(_data, f"/docker-port/{port_i}/")
            return Response(
                content=_json.dumps(_data, ensure_ascii=False),
                status_code=resp.status_code, headers=resp_headers,
            )
        except Exception:
            swallow.note('docker_module.docker_port_proxy:L1457')
            pass

    # Редиректы переписываем на наш префикс, чтобы браузер не уходил с прокси
    location = resp_headers.get("location", "")
    if location and not _re.match(r'^[a-z][a-z0-9+.-]*:', location, _re.IGNORECASE) \
            and not location.startswith("/docker-port/"):
        if location.startswith("/"):
            resp_headers["location"] = f"/docker-port/{port_i}{location}"
        else:
            resp_headers["location"] = f"/docker-port/{port_i}/{location}"
        return Response(status_code=resp.status_code, headers=resp_headers)

    # Абсолютные пути внутри HTML/CSS переписываем на префикс прокси —
    # иначе браузер при открытии через main.py запрашивает /static/..., /search и
    # т.п. у корня main.py вместо контейнера
    _looks_html = ("text/html" in content_type or
                   resp.content[:256].lstrip().lower().startswith((b"<!doctype html", b"<html")))
    if _looks_html:
        return Response(
            content=_inject_runtime_patch(
                _rewrite_abs_paths(resp.content, port_i, ("text/html",)), port_i
            ),
            status_code=resp.status_code, headers=resp_headers,
        )
    if "text/css" in content_type:
        return Response(
            content=_rewrite_abs_paths(resp.content, port_i, ("text/css",)),
            status_code=resp.status_code, headers=resp_headers,
        )
    return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)
# --------------------------------------------------------------------------
# WebSocket — нативный exec / logs (переиспользует движок docker)
# --------------------------------------------------------------------------
def _qs_params(qs: str):
    from urllib.parse import parse_qs
    d = parse_qs(qs)
    return {k: v[0] for k, v in d.items()}


def _docker_env(eng):
    """Окружение для docker/podman CLI.

    * Linux/WSL — в PATH добавляем каталог portable-бинарников;
    * Windows со встроенным docker.exe/dockerd.exe — тоже, чтобы dockerd.exe
      и сопутствующие утилиты находились;
    * Windows без встроенного движка (Docker Desktop) — PATH не трогаем;
    * Podman — PATH всегда ведёт к найденному podman.exe и DOCKER_HOST
      убираем, иначе клиент пытается идти в docker-сокет.
    """
    env = dict(os.environ)
    backend = str(getattr(eng, "_ACTIVE_BACKEND", "") or "").lower()
    if backend == "podman":
        p = getattr(eng, "DOCKER_BIN", "") or ""
        d = os.path.dirname(p)
        if d and os.path.isdir(d):
            env["PATH"] = d + os.pathsep + env.get("PATH", "")
        env.pop("DOCKER_HOST", None)
        if p:
            env["PODMAN_BIN"] = p
        return env
    d = getattr(eng, "DOCKER_DIR", "") or ""
    bundled = bool(getattr(eng, "USE_BUNDLED_WINDOWS", False))
    if bundled:
        # docker.exe/dockerd.exe лежат рядом с движком (app/docker).
        d = os.path.dirname(getattr(eng, "DOCKER_BIN", "") or "") or d
    if d and os.path.isdir(d) and (not _is_windows() or bundled):
        env["PATH"] = d + os.pathsep + env.get("PATH", "")
        env["DOCKER_DIR"] = d
    if bundled and getattr(eng, "_ACTIVE_BACKEND", "") == "bundled" and getattr(eng, "BUNDLED_WIN_HOST", ""):
        env.setdefault("DOCKER_HOST", eng.BUNDLED_WIN_HOST)
    return env


def _is_windows():
    return sys.platform == "win32"


def _hide_window_kwargs():
    """creationflags, скрывающие всплывающее окно консоли (Windows)."""
    if not _is_windows():
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _docker_cmd_args(eng, *args):
    """Полная команда docker CLI с учётом платформы.

    Linux/WSL — свой portable dockerd на unix-сокете, поэтому нужен -H.
    Windows — демон это Docker Desktop (named pipe), -H unix:// там неверен
    и docker.exe его отвергнет.
    """
    host = getattr(eng, "_docker_host_arg", None)
    host_args = list(host()) if callable(host) else (
        [] if _is_windows() else ["-H", "unix://" + getattr(eng, "SOCK", "")]
    )
    return [getattr(eng, "DOCKER_BIN", "docker")] + host_args + list(args)


async def _exec_ws(websocket: WebSocket, cid: str):
    """docker exec -i <cid> sh — живой терминал через WebSocket."""
    eng = _ensure_engine()
    shell = "cmd" if _is_windows() else "sh"
    try:
        proc = await asyncio.create_subprocess_exec(
            *_docker_cmd_args(eng, "exec", "-i", cid, shell),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_docker_env(eng),
            **_hide_window_kwargs(),
        )
    except Exception as e:
        swallow.note('docker_module._exec_ws:L1558', e)
        await websocket.send_text("err: " + str(e))
        return

    async def stdout_to_ws():
        try:
            while True:
                chunk = await proc.stdout.read(1024)
                if not chunk:
                    break
                await websocket.send_text(chunk.decode("utf-8", errors="replace"))
        except Exception:
            swallow.note('docker_module.stdout_to_ws:L1570')
            pass

    async def ws_to_stdin():
        try:
            while True:
                data = await websocket.receive_text()
                if proc.stdin and data:
                    proc.stdin.write(data.encode("utf-8", errors="replace"))
                    await proc.stdin.drain()
        except Exception:
            swallow.note('docker_module.ws_to_stdin:L1581')
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                swallow.note('docker_module.ws_to_stdin:L1586')
                pass

    t1 = asyncio.create_task(stdout_to_ws())
    t2 = asyncio.create_task(ws_to_stdin())
    await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in (t1, t2):
        if not t.done():
            t.cancel()
    try:
        proc.kill()
    except Exception:
        swallow.note('docker_module._exec_ws:L1598')
        pass


async def _logs_ws(websocket: WebSocket, cid: str):
    """docker logs -f --tail 200 <cid> — поток логов через WebSocket."""
    eng = _ensure_engine()
    try:
        proc = await asyncio.create_subprocess_exec(
            *_docker_cmd_args(eng, "logs", "-f", "--tail", "200", cid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_docker_env(eng),
            **_hide_window_kwargs(),
        )
    except Exception as e:
        swallow.note('docker_module._logs_ws:L1614', e)
        await websocket.send_text("err: " + str(e))
        return
    try:
        while True:
            chunk = await proc.stdout.read(1024)
            if not chunk:
                break
            await websocket.send_text(chunk.decode("utf-8", errors="replace"))
    except Exception:
        swallow.note('docker_module._logs_ws:L1624')
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            swallow.note('docker_module._logs_ws:L1630')
            pass


@router.websocket("/docker/ws")
async def docker_ws(websocket: WebSocket):
    await websocket.accept()
    params = _qs_params(websocket.url.query or "")
    try:
        if params.get("cid"):
            await _exec_ws(websocket, params["cid"])
        elif params.get("logs"):
            await _logs_ws(websocket, params["logs"])
        else:
            await websocket.close(code=1008)
    except WebSocketDisconnect:
        swallow.note('docker_module.docker_ws:L1646')
        pass
    except Exception as e:
        _log(f"ws error: {e}", "error")
        try:
            await websocket.close(code=1011)
        except Exception:
            swallow.note('docker_module.docker_ws:L1653')
            pass
# --------------------------------------------------------------------------
# Публичный API модуля
# --------------------------------------------------------------------------
def start():
    """Запуск движка + внутренний сервер. Вызывается при старте main.py."""
    _ensure_engine()
    _start_inner_server()


def get_status():
    """Текущее состояние — для системной панели."""
    eng = _ensure_engine()
    try:
        st = eng.get_state()
        st["inner_port"] = _active_port
        st["module"] = "integrated"
        st["backend"] = _engine_backend()
        if docker_backend is not None:
            try:
                st["podman"] = docker_backend.podman_info_dict()
            except Exception:
                swallow.note('docker_module.get_status:L1673')
        return st
    except Exception as e:
        swallow.note('docker_module.get_status:L1675', e)
        return {"module": "integrated", "error": str(e), "inner_port": _active_port}


def try_start_daemon():
    """Пытается поднять демон Docker.

    Linux/WSL: свой portable dockerd (нужен root/WSL2).
    Windows:   Docker Desktop — свой dockerd на Windows не запускается,
               поэтому поднимаем штатный Docker Desktop и ждём демон.

    Раньше здесь стоял голый `sys.platform` без `import sys` в этом файле —
    автозапуск падал с «name 'sys' is not defined» на каждом старте.
    """
    eng = _ensure_engine()
    try:
        if eng.daemon_running():
            return {"ok": True, "msg": "демон уже работает"}
        if _is_windows():
            # Внутри движка это -> start_docker_desktop().
            res = eng.start_daemon()
            if isinstance(res, dict) and not res.get("ok"):
                _log(f"демон не поднялся: {res.get('error')}", "warning")
            return res
        is_wsl = getattr(eng, "is_wsl", None)
        if callable(is_wsl) and not is_wsl():
            return {"ok": False,
                    "error": "это не WSL и не Windows — dockerd отсюда не запускается"}
        return eng.start_daemon()
    except Exception as e:
        _log(f"ошибка запуска демона: {e}", "error")
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------
# Настройки модуля: автозапуск (config.json → docker.autostart, по умолчанию
# выключен). Страница /docker поднимает внутренний UI лениво сама, поэтому
# выключенный автозапуск не ломает модуль — он просто не стартует с приложением.
# --------------------------------------------------------------------------
def autostart_enabled() -> bool:
    try:
        from config_module import get_config
        return bool(((get_config() or {}).get("docker") or {}).get("autostart", False))
    except Exception:
        swallow.note('docker_module.autostart_enabled:L1717')
        return False


def set_autostart(value: bool) -> bool:
    try:
        from config_module import get_config, save_config, reload_config
        cfg = get_config() or {}
        sec = dict(cfg.get("docker") or {})
        sec["autostart"] = bool(value)
        cfg["docker"] = sec
        save_config(cfg)
        try:
            reload_config()
        except Exception:
            swallow.note('docker_module.set_autostart:L1732')
            pass
        return True
    except Exception as e:
        _log(f"не удалось сохранить docker.autostart: {e}", "error")
        return False


def stop_daemon():
    eng = _ensure_engine()
    try:
        fn = getattr(eng, "stop_daemon", None)
        if callable(fn):
            return fn()
        return {"ok": False, "error": "движок не умеет останавливать демон"}
    except Exception as e:
        swallow.note('docker_module.stop_daemon:L1748', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/docker/settings")
async def api_docker_settings():
    def _collect():
        data = {
            "autostart": autostart_enabled(),
            "backend": _engine_backend(),
            "status": get_status(),
        }
        if docker_backend is not None:
            data["podman"] = docker_backend.podman_info_dict()
            data["podman_job"] = docker_backend.get_job()
        return json.dumps(data, ensure_ascii=False)
    payload = await _run_sync(_collect)
    return Response(payload, media_type="application/json")


@router.post("/api/docker/settings")
async def api_docker_settings_set(request: Request):
    try:
        body = await request.json()
    except Exception:
        swallow.note('docker_module.api_docker_settings_set:L1764')
        body = {}
    want = bool(body.get("autostart"))
    backend_wanted = body.get("backend")
    if (backend_wanted and docker_backend is not None
            and str(backend_wanted).lower() == "podman"
            and not docker_backend.podman_installed()):
        backend_wanted = None
    set_autostart(want)
    if backend_wanted and docker_backend is not None:
        docker_backend.set_backend(backend_wanted)
        # Применяем сразу, не дожидаясь следующего запроса к движку.
        try:
            _apply_backend()
        except Exception:
            swallow.note('docker_module.api_docker_settings_set:L1830')
    payload = await _run_sync(lambda: json.dumps({
        "ok": True,
        "autostart": autostart_enabled(),
        "backend": _engine_backend(),
        "podman": docker_backend.podman_info_dict() if docker_backend is not None else {},
    }, ensure_ascii=False))
    return Response(payload, media_type="application/json")


@router.get("/api/docker/backend")
async def api_docker_backend():
    def _collect():
        data = {"backend": _engine_backend()}
        if docker_backend is not None:
            data["podman"] = docker_backend.podman_info_dict()
            data["job"] = docker_backend.get_job()
        return json.dumps(data, ensure_ascii=False)
    payload = await _run_sync(_collect)
    return Response(payload, media_type="application/json")


@router.post("/api/docker/backend")
async def api_docker_backend_set(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    target = str(body.get("backend") or "docker").lower()
    if docker_backend is None:
        return Response(json.dumps({"ok": False, "error": "docker_backend не загружен"},
                                   ensure_ascii=False), media_type="application/json")
    if target == "podman" and not docker_backend.podman_installed():
        payload = await _run_sync(lambda: json.dumps({
            "ok": False, "backend": _engine_backend(),
            "error": "Podman не установлен — сначала скачайте его",
            "podman": docker_backend.podman_info_dict(),
        }, ensure_ascii=False))
        return Response(payload, media_type="application/json")
    backend = docker_backend.set_backend(target)
    _apply_backend()
    payload = await _run_sync(lambda: json.dumps({
        "ok": True, "backend": backend,
        "podman": docker_backend.podman_info_dict(),
    }, ensure_ascii=False))
    return Response(payload, media_type="application/json")


@router.get("/api/docker/podman/status")
async def api_docker_podman_status():
    payload = await _run_sync(lambda: json.dumps({
        "ok": True,
        "podman": docker_backend.podman_info_dict() if docker_backend is not None else {},
        "job": docker_backend.get_job() if docker_backend is not None else {},
    }, ensure_ascii=False))
    return Response(payload, media_type="application/json")


@router.post("/api/docker/podman/download")
async def api_docker_podman_download(request: Request):
    """Скачать и установить podman-remote (в фоне, прогресс — GET /api/docker/podman/status)."""
    if docker_backend is None:
        return Response(json.dumps({"ok": False, "error": "docker_backend не загружен"},
                                   ensure_ascii=False), media_type="application/json")
    try:
        body = await request.json()
    except Exception:
        body = {}
    wait = bool(body.get("wait"))
    job = await _run_sync(lambda: docker_backend.install_podman(background=not wait),
                          low=True)
    return Response(json.dumps({"ok": True, "job": job}, ensure_ascii=False),
                    media_type="application/json")


@router.post("/api/docker/daemon/start")
async def api_docker_daemon_start():
    # LOW_POOL: поднимание демона — десятки секунд, в HIGH оно выело бы воркера
    # под себя и оставил интерактивные ручки без пула.
    payload = await _run_sync(lambda: json.dumps(try_start_daemon(), ensure_ascii=False),
                              low=True)
    return Response(payload, media_type="application/json")


@router.post("/api/docker/daemon/stop")
async def api_docker_daemon_stop():
    payload = await _run_sync(lambda: json.dumps(stop_daemon(), ensure_ascii=False),
                              low=True)
    return Response(payload, media_type="application/json")