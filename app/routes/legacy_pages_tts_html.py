"""legacy_pages_tts_html — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import JSONResponse
from fastapi.responses import Response
from urllib.parse import quote
from urllib.parse import urlparse
import asyncio
import json
import os
import re
import server
from modules import llm_service
import threading
import time
import worker_pool
from fastapi import APIRouter, Request
from routes.legacy_common import _aio_read, _run_sync, _sanitize_tts_text, _start_low_priority_daemon, logger

# секция читает файлы из каталога приложения (app/), а не из routes/
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

router = APIRouter()


def _opt_float(value):
    """Число из query-параметра или None, если параметр не задан."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@router.get("/api/tts")
async def api_tts(request: Request):
    """Direct FastAPI TTS endpoint — returns WAV audio.

    Движок: параметр engine (silero|piper|qwen3|webspeech|custom:<id>|auto)
    или, если не указан/равен auto — централизованный движок из настроек.
    Оптимизировано: async + кэш.
    """
    import speech_engine
    text = request.query_params.get("text", "")
    engine = request.query_params.get("engine", "")
    # Голос: читатели (книги, ZIM) передают Piper-голос в параметре voice, а
    # Silero/qwen3 — в speaker. Без этого параметра голос берётся из настроек.
    speaker = request.query_params.get("speaker", "") or request.query_params.get("voice", "")
    lang = request.query_params.get("lang", "ru")
    # None (а не 1.0/0.0) означает «клиент не указал» — тогда speech_engine
    # возьмёт скорость и тон из настроек речи.
    rate = _opt_float(request.query_params.get("rate"))
    pitch = _opt_float(request.query_params.get("pitch"))

    if not text:
        return JSONResponse({"error": "Нет текста"}, status_code=400)

    text = _sanitize_tts_text(text)
    if len(text.strip()) < 2:
        return Response(status_code=204)

    if not engine or engine in ("auto", "default"):
        engine = ""

    # Кэш живёт в speech_engine.synthesize_tts: он нужен всем вызывающим,
    # а не только этому обработчику, и ключ строится по разрешённому движку.
    try:
        wav_data = await worker_pool.run_async(
            speech_engine.synthesize_tts, text,
            engine=engine or None, speaker=speaker or None,
            rate=rate, pitch=pitch,
        )
        if not wav_data:
            # webspeech — синтез в браузере
            return Response(status_code=204)
        return Response(content=wav_data, media_type="audio/wav",
                        headers={"Cache-Control": "no-cache"})
    except Exception as e:
        logger.error(f"TTS error (engine={engine or 'auto'}): {e}", exc_info=True)
        return JSONResponse({"error": str(e), "text_preview": text[:80]}, status_code=500)

@router.get("/api/tts/voices")
async def api_tts_voices():
    return await _run_sync(tts_plugins.handle_tts_voices)

@router.post("/api/tts/install-piper")
async def api_tts_install_piper(request: Request):
    body = await request.json()
    return await _run_sync(tts_installers.handle_install_piper, body.get("voice", ""))

@router.post("/api/tts/install-silero")
async def api_tts_install_silero():
    return await _run_sync(tts_installers.handle_install_silero)

# --- Index / Models ---
from routes.legacy_system import router as _legacy_system_router
from modules import assets_index
from modules import tts_installers
from modules import tts_plugins
router.include_router(_legacy_system_router)

# --- HTML ---
_HTML_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_HTML_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _legacy_shared_html_dir():
    """Старый общий каталог data/html (до разделения библиотек по профилям)."""
    try:
        from config_module import _root
        return os.path.abspath(os.path.join(_root(), "data", "html"))
    except Exception:
        return ""


def _request_profile_id(request) -> str:
    """Профиль запроса по cookie. Важно для API: активный профиль в процессе
    один, а у каждого окна/устройства свой cookie — вьюер обязан показывать
    библиотеку именно владельца запроса."""
    if request is None:
        return ""
    try:
        import profiles_module as _pm
        pid = request.cookies.get(_pm.COOKIE_NAME) or ""
        return _pm.resolve_profile_id(pid) or ""
    except Exception:
        return ""


def _profile_html_library_dirs(profile_id: str = ""):
    """Персональный каталог HTML-страниц профиля (по умолчанию активного).

    У каждого профиля в config.json уже есть html_dir
    (data/profiles/<id>/data/html), но вьюер его игнорировал и писал всё
    в общий data/html — страницы были видны всем профилям. Возвращаем
    профильный каталог первым: относительные пути при создании файлов
    разрешаются от dirs[0], поэтому новые страницы остаются у владельца.
    """
    try:
        import profiles_module as _pm
        cfg = (_pm.get_effective_config(profile_id) if profile_id
               else _pm.get_active_effective_config()) or {}
        out = []
        hd = cfg.get("html_dir") or ""
        if hd:
            out.append(_pm._expand_token(hd))
        else:
            if not profile_id:
                profile_id = (_pm.get_active_profile() or {}).get("id") or ""
            if profile_id:
                out.append(os.path.join(_pm._profile_data_path(profile_id), "html"))
        legacy = _legacy_shared_html_dir()
        for d in (cfg.get("html_viewer_dirs") or []):
            p = _pm._expand_token(d)
            # Общий legacy-каталог уже разложен по профилям отдельным
            # переносом; держать его в списке — значит показывать каждую
            # страницу дважды. Внешние каталоги пользователя не трогаем.
            if legacy and os.path.abspath(p) == legacy:
                continue
            out.append(p)
        return [p for p in out if p]
    except Exception:
        return []


def _html_library_dirs(profile_id: str = ""):
    """Актуальные каталоги HTML-библиотеки (профиль → server → config → data/html).

    Профильный каталог идёт первым; общие каталоги из html_viewer_dirs
    остаются доступны (старая библиотека не пропадает), но новые страницы
    создаются в профильном.
    """
    dirs = []
    for d in _profile_html_library_dirs(profile_id):
        if d and d not in dirs:
            dirs.append(d)
    legacy = _legacy_shared_html_dir()
    # server.HTML_DIRS — снимок конфига АКТИВНОГО профиля. Для чужого
    # cookie он показал бы чужой каталог, поэтому при явном profile_id
    # общие пути берём только из его собственного конфига (ниже).
    if not profile_id:
        try:
            for d in (getattr(server, "HTML_DIRS", None) or []):
                if legacy and os.path.abspath(d) == legacy:
                    continue
                if d and d not in dirs:
                    dirs.append(d)
        except Exception:
            pass
    if not dirs:
        try:
            for d in (server.get_config().get("html_viewer_dirs") or []):
                if legacy and os.path.abspath(d) == legacy:
                    continue
                if d and d not in dirs:
                    dirs.append(d)
        except Exception:
            pass
    if not dirs:
        try:
            from config_module import _root
            dirs = [os.path.join(_root(), "data", "html")]
        except Exception:
            dirs = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "html")]
    out = []
    for d in dirs:
        try:
            p = os.path.abspath(os.path.expanduser(str(d)))
            # Профильный каталог может отсутствовать у нового профиля —
            # создаём его, чтобы «Создать файл» не падал на пустой библиотеке.
            if p not in out:
                try:
                    os.makedirs(p, exist_ok=True)
                except OSError:
                    pass
                out.append(p)
        except Exception:
            continue
    return out


def _safe_html_component(name: str) -> str:
    """Проверить один компонент пути (файл/папку) на недопустимые имена."""
    name = (name or "").strip().strip(". ")
    if not name:
        raise ValueError("Пустое имя файла или папки")
    if _HTML_BAD_CHARS.search(name):
        raise ValueError("Имя содержит недопустимые символы: < > : \" | ? *")
    base = name.split(".")[0].upper()
    if base in _HTML_RESERVED_NAMES:
        raise ValueError(f"Имя «{name}» зарезервировано Windows")
    return name


def _resolve_html_path(raw: str, is_folder: bool = False, add_ext: bool = None,
                       profile_id: str = "") -> str:
    """Привести путь к абсолютному внутри HTML_DIRS и проверить его.

    Поддерживает относительные пути (от первого каталога библиотеки), прямые
    и обратные слэши, автоматически добавляет .html для файлов. Бросает
    ValueError/PermissionError с понятным текстом.
    """
    if add_ext is None:
        add_ext = not is_folder
    dirs = _html_library_dirs(profile_id)
    if not dirs:
        raise PermissionError("Не настроен каталог HTML-библиотеки")
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Нет пути")
    raw = raw.replace("\\", "/")
    if not os.path.isabs(raw) and not re.match(r"^[A-Za-z]:/", raw):
        raw = os.path.join(dirs[0], raw)
    ap = os.path.abspath(os.path.expanduser(raw))

    matched = None
    for d in dirs:
        if ap == d or ap.startswith(d + os.sep):
            matched = d
            break
    if not matched:
        raise PermissionError("Доступ разрешён только внутри HTML-библиотеки")

    rel = os.path.relpath(ap, matched)
    if rel == ".":
        if not is_folder:
            raise ValueError("Нельзя создать файл вместо каталога библиотеки")
        return ap
    for part in rel.split(os.sep):
        _safe_html_component(part)

    if add_ext and not ap.lower().endswith((".html", ".htm")):
        ap += ".html"
    return ap


def _html_existing_path(raw: str, profile_id: str = ""):
    """Найти существующий файл/папку внутри HTML-библиотеки (None, если нет)."""
    try:
        ap = _resolve_html_path(raw, is_folder=False, add_ext=False, profile_id=profile_id)
    except ValueError:
        return None
    if os.path.isfile(ap) or os.path.isdir(ap):
        return ap
    alt = ap + ".html"
    if os.path.isfile(alt):
        return alt
    return None


@router.get("/api/html/dirs")
async def api_html_dirs(request: Request):
    pid = _request_profile_id(request)
    return {"dirs": await _run_sync(_html_library_dirs, pid)}


@router.get("/api/html")
async def api_html(request: Request):
    pid = _request_profile_id(request)
    dirs = await _run_sync(_html_library_dirs, pid)
    results = await _run_sync(server.find_html_files, dirs)
    return {"results": results}

@router.get("/api/html/tree")
async def api_html_tree(request: Request):
    from html_module import build_html_tree
    pid = _request_profile_id(request)
    dirs = await _run_sync(_html_library_dirs, pid)
    tree = await _run_sync(build_html_tree, dirs)
    return {"tree": tree}

@router.get("/api/html/folders")
async def api_html_folders(request: Request):
    from html_module import build_html_tree
    pid = _request_profile_id(request)
    dirs = await _run_sync(_html_library_dirs, pid)
    tree = await _run_sync(build_html_tree, dirs)
    return {"tree": tree}

@router.get("/api/html/search")
async def api_html_search(request: Request, q: str = ""):
    pid = _request_profile_id(request)
    dirs = await _run_sync(_html_library_dirs, pid)
    results = await _run_sync(server.search_html_files, q, dirs)
    return {"results": results}

@router.get("/api/html/view")
async def api_html_view(request: Request, f: str = ""):
    pid = _request_profile_id(request)
    if f:
        try:
            f = await _run_sync(_resolve_html_path, f, False, None, pid)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    exists = await _run_sync(os.path.isfile, f) if f else False
    if f and exists:
        try:
            content = await _aio_read(f, encoding="utf-8", errors="replace")
            title, desc = await _run_sync(server.extract_html_meta, f)
            return {"ok": True, "content": content, "title": title or f, "description": desc}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"error": "File not found"}, status_code=404)

@router.post("/api/html/file/rename")
async def api_html_file_rename(request: Request):
    body = await request.json()
    old_path = body.get("old_path", "")
    new_name = body.get("new_name", "")
    if not old_path or not new_name:
        return JSONResponse({"error": "Нет пути или имени"}, status_code=400)
    pid = _request_profile_id(request)
    return await _run_sync(_html_file_rename_sync, old_path, new_name, pid)

def _html_file_rename_sync(old_path: str, new_name: str, profile_id: str = ""):
    try:
        old_abs = _html_existing_path(old_path, profile_id)
        if not old_abs:
            return JSONResponse({"error": "Файл не найден"}, status_code=404)
        is_dir = os.path.isdir(old_abs)
        new_name = (new_name or "").strip()
        if not new_name:
            return JSONResponse({"error": "Нет имени"}, status_code=400)
        if "/" in new_name or "\\" in new_name:
            return JSONResponse({"error": "Переименование не должно менять папку"}, status_code=400)
        try:
            new_abs = _resolve_html_path(
                os.path.join(os.path.dirname(old_abs), new_name),
                is_folder=is_dir, add_ext=not is_dir, profile_id=profile_id)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if os.path.exists(new_abs):
            return JSONResponse({"error": "Файл уже существует"}, status_code=400)
        import shutil
        shutil.move(old_abs, new_abs)
        return {"ok": True, "new_path": new_abs}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/html/file/delete")
async def api_html_file_delete(request: Request):
    body = await request.json()
    fpath = body.get("path", "")
    if not fpath:
        return JSONResponse({"error": "Нет пути"}, status_code=400)
    pid = _request_profile_id(request)
    return await _run_sync(_html_file_delete_sync, fpath, pid)

def _html_file_delete_sync(fpath: str, profile_id: str = ""):
    try:
        target = _html_existing_path(fpath, profile_id)
        if not target:
            return JSONResponse({"error": "Файл не найден"}, status_code=404)
        if os.path.isfile(target):
            os.remove(target)
            return {"ok": True}
        if os.path.isdir(target):
            dirs = _html_library_dirs(profile_id)
            if any(os.path.abspath(target) == d for d in dirs):
                return JSONResponse({"error": "Нельзя удалить корневой каталог библиотеки"}, status_code=400)
            import shutil
            shutil.rmtree(target)
            return {"ok": True}
        return JSONResponse({"error": "Файл не найден"}, status_code=404)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/html/file/create")
async def api_html_file_create(request: Request):
    body = await request.json()
    fpath = body.get("path", "")
    content = body.get("content", "")
    if not fpath:
        return JSONResponse({"error": "Нет пути"}, status_code=400)
    pid = _request_profile_id(request)
    try:
        fpath = await _run_sync(_resolve_html_path, fpath, False, None, pid)
        await _run_sync(lambda p: os.makedirs(p, exist_ok=True), os.path.dirname(fpath))
        await worker_pool.write_file(fpath, content)
        return {"ok": True, "path": fpath}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/html/folder/create")
async def api_html_folder_create(request: Request):
    body = await request.json()
    fpath = body.get("path", "")
    if not fpath:
        return JSONResponse({"error": "Нет пути"}, status_code=400)
    pid = _request_profile_id(request)
    try:
        fpath = await _run_sync(_resolve_html_path, fpath, True, None, pid)
        await _run_sync(lambda p: os.makedirs(p, exist_ok=True), fpath)
        return {"ok": True, "path": fpath}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# Сохранение страницы из встроенного браузера в модуль HTML Viewer.
# Страница /browser шлёт сюда DOM активной вкладки (outerHTML): скачивать
# через HTTP нельзя — там отдаётся сырой ответ сервера, а не то, что видит
# пользователь после JS. Файл кладём в первый каталог HTML Viewer, чтобы
# дерево /html/viewer подхватило его без дополнительной настройки.
MAX_SAVE_PAGE_BYTES = 20 * 1024 * 1024   # 20 МБ — дальше HTML раздувается бессмысленно


def _html_saved_dir(profile_id: str = "") -> str:
    """Каталог для сохранённых страниц: первый каталог библиотеки профиля + /saved."""
    dirs = _html_library_dirs(profile_id)
    base = next((d for d in dirs if d and os.path.isdir(str(d))), None)
    if not base and dirs and dirs[0]:
        base = dirs[0]
    if not base:
        try:
            from config_module import _root
            base = os.path.join(_root(), "data", "html")
        except Exception:
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "html")
    return os.path.join(str(base), "saved")


def _safe_slug(text: str, limit: int = 60) -> str:
    """Имя файла из заголовка: без разделителей пути и запретных в Windows символов."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")
    s = s[:limit].strip(" .")
    return s or "page"


def _html_save_page_sync(url: str, title: str, html: str, profile_id: str = "") -> dict:
    target_dir = _html_saved_dir(profile_id)
    os.makedirs(target_dir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M")
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    host = re.sub(r"[^A-Za-z0-9.-]", "_", host)[:60]
    name = f"{stamp}_{_safe_slug(title)}" + (f"_{host}" if host else "") + ".html"
    path = os.path.join(target_dir, name)
    # Одинаковые заголовок+хост в одну минуту — не затираем предыдущий файл.
    n = 2
    while os.path.exists(path):
        path = os.path.join(target_dir, name[:-5] + f" ({n}).html")
        n += 1
    header = (f"<!-- сохранено из встроенного браузера Infinity\n"
              f"     источник: {url}\n"
              f"     заголовок: {title}\n"
              f"     дата:     {time.strftime('%Y-%m-%d %H:%M:%S')} -->\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + html)
    # Дерево и список файлов HTML Viewer кэшируются — сбрасываем, иначе
    # свежесохранённая страница не появится до истечения TTL.
    try:
        import html_module
        html_module._html_cache.clear()
        html_module._html_cache_time = 0
    except Exception:
        pass
    # Живой /api/files читает cache_manager.search_cache (ключи files:<dirs>),
    # а server.search._files_cache принадлежит старому обработчику, которого
    # в FastAPI-маршрутах нет: чистка того кэша ничего не сбрасывала, и
    # сохранённая страница пропадала из списка до истечения TTL.
    try:
        from cache_manager import search_cache
        search_cache.invalidate("files:")
    except Exception:
        pass
    return {"ok": True, "path": path, "name": os.path.basename(path), "dir": target_dir}


@router.post("/api/html/save-page")
async def api_html_save_page(request: Request):
    body = await request.json()
    url = str(body.get("url", "") or "")
    title = str(body.get("title", "") or "")
    html = body.get("html", "")
    if not html:
        return JSONResponse({"error": "Пустая страница"}, status_code=400)
    if not isinstance(html, str):
        return JSONResponse({"error": "Некорректное содержимое"}, status_code=400)
    if len(html.encode("utf-8", errors="ignore")) > MAX_SAVE_PAGE_BYTES:
        return JSONResponse({"error": "Страница слишком большая для сохранения"}, status_code=413)
    if not title:
        try:
            title = urlparse(url).hostname or "страница"
        except Exception:
            title = "страница"
    pid = _request_profile_id(request)
    try:
        res = await _run_sync(_html_save_page_sync, url, title, html, pid)
    except Exception as e:
        logger.error(f"[html] save-page failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    # Ссылка, по которой HTML Viewer сам откроет файл (bootFromUrl читает
    # path/title из query). Путь кодируем: в нём могут быть пробелы и кириллица.
    res["viewer_url"] = (
        "/html/viewer?path=" + quote(res["path"]) +
        "&title=" + quote(res["name"])
    )
    return res


# --- Перевод страницы локальной моделью ---
# Кнопка «Перевести» во встроенном браузере присылает текст страницы кусками.
# Перевод идёт той же моделью, что и весь ИИ в приложении (llama.cpp/LM Studio),
# поэтому работает офлайн и не отдаёт содержимое страниц наружу.
TRANSLATE_MAX_CHARS = 6000        # один запрос к модели — больше рвёт контекст
TRANSLATE_MAX_TOTAL = 120000      # общий лимит на страницу
TRANSLATE_TIMEOUT = 600

_TR_SYSTEM = (
    "Ты — переводчик веб-страниц. Переведи присланный текст на {lang}.\n"
    "Правила:\n"
    "- Сохраняй разметку, абзацы, списки и переводы строк как в исходнике.\n"
    "- Не переводи код, URL, имена файлов, команды и идентификаторы.\n"
    "- Не сокращай и не пересказывай: переводи весь текст целиком.\n"
    "- Не добавляй пояснений, комментариев и кавычек вокруг результата.\n"
    "- Верни ТОЛЬКО переведённый текст."
)


def _tr_llm_request_sync(messages, max_tokens, model, temperature):
    """Синхронный HTTP к OpenAI-совместимому endpoint (вызывается из пула потоков)."""
    import urllib.request
    import urllib.error
    url = llm_service.get_llm_url()
    api_key = llm_service.get_llm_api_key()
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if server.LLM_BACKEND == "llamacpp":
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TRANSLATE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choice = (data.get("choices") or [{}])[0].get("message", {})
        return {"content": choice.get("content", "")}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        logger.error(f"[translate] HTTP {e.code}: {detail}")
        return {"content": f"Ошибка переводчика ({e.code}): {detail}"}
    except Exception as e:
        logger.error(f"[translate] ошибка: {e}")
        return {"content": f"Ошибка переводчика: {e}"}


def _translate_sync(text: str, target_lang: str) -> str:
    """Один кусок текста -> перевод. Ошибки модели отдаём как текст, чтобы
    страница показала причину, а не молча потеряла результат."""
    default_model = llm_service.resolve_llm_model(
        server.LLAMA_CPP_MODEL if server.LLM_BACKEND == "llamacpp"
        else server.LM_STUDIO_MODEL
    )
    result = _tr_llm_request_sync(
        [
            {"role": "system", "content": _TR_SYSTEM.format(lang=target_lang)},
            {"role": "user", "content": text},
        ],
        max_tokens=min(8192, max(512, len(text) * 3)),
        model=default_model,
        temperature=0.2,      # перевод, не творчество: держим близко к оригиналу
    )
    return (result or {}).get("content", "").strip()


# ─── Лёгкий переводчик страниц (без тяжёлой LLM) ─────────────────────────────
# Для перевода страницы «на месте» не нужен локальный LLM: достаточно быстрых
# веб-сервисов перевода. Это на порядки легче и не занимает модель/GPU.
# Порядок провайдеров: Google (gtx) → MyMemory. При недоступности обоих
# возвращаем пустую строку — вызывающий код оставит оригинал.
_LANG_CODES = {
    "русский": "ru", "ru": "ru", "russian": "ru",
    "английский": "en", "en": "en", "english": "en",
    "украинский": "uk", "uk": "uk", "ukrainian": "uk",
    "немецкий": "de", "de": "de", "german": "de", "deutsch": "de",
    "французский": "fr", "fr": "fr", "french": "fr",
    "испанский": "es", "es": "es", "spanish": "es",
    "итальянский": "it", "it": "it", "italian": "it",
    "португальский": "pt", "pt": "pt", "portuguese": "pt",
    "польский": "pl", "pl": "pl", "polish": "pl",
    "турецкий": "tr", "tr": "tr", "turkish": "tr",
    "китайский": "zh-CN", "zh": "zh-CN", "chinese": "zh-CN",
    "японский": "ja", "ja": "ja", "japanese": "ja",
    "корейский": "ko", "ko": "ko", "korean": "ko",
    "арабский": "ar", "ar": "ar", "arabic": "ar",
}

_TR_CACHE = {}
_TR_CACHE_LOCK = threading.Lock()
_TR_CACHE_MAX = 5000


def _target_code(target_lang: str) -> str:
    key = (target_lang or "").strip().lower()
    return _LANG_CODES.get(key, "ru")


def _gtx_translate(text: str, tgt: str, timeout: float = 8.0) -> str:
    """Google Translate (публичный gtx-эндпоинт). Без ключей и установки пакетов."""
    import requests
    r = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": tgt, "dt": "t", "q": text},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    parts = []
    for seg in (data[0] or []):
        if seg and seg[0]:
            parts.append(seg[0])
    return "".join(parts).strip()


def _mymemory_translate(text: str, tgt: str, timeout: float = 8.0) -> str:
    """MyMemory — резервный сервис (нужен явный язык источника)."""
    import requests
    src = "ru" if re.search(r"[\u0400-\u04ff]", text) else "en"
    if src == tgt:
        return text
    r = requests.get(
        "https://api.mymemory.translated.net/get",
        params={"q": text[:500], "langpair": f"{src}|{tgt}"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    out = (data.get("responseData") or {}).get("translatedText") or ""
    return str(out).strip()


def _light_translate_sync(text: str, target_lang: str) -> str:
    """Перевод одной строки лёгким сервисом. Кэш по (язык, текст)."""
    text = (text or "").strip()
    if not text or len(text) < 2:
        return ""
    tgt = _target_code(target_lang)
    key = (tgt, text)
    with _TR_CACHE_LOCK:
        cached = _TR_CACHE.get(key)
    if cached is not None:
        return cached
    out = ""
    for fn in (_gtx_translate, _mymemory_translate):
        try:
            out = fn(text, tgt)
        except Exception as e:
            logger.debug(f"light translate {fn.__name__} failed: {e}")
            out = ""
        if out:
            break
    if out:
        with _TR_CACHE_LOCK:
            if len(_TR_CACHE) > _TR_CACHE_MAX:
                _TR_CACHE.clear()
            _TR_CACHE[key] = out
    return out


def _gtx_translate_batch(texts, tgt: str, timeout: float = 12.0):
    """Несколько строк одним запросом к gtx.

    Google разбивает вход по «\\n» и возвращает по сегменту на строку, поэтому
    пакет из 30 подписей/абзацев — один HTTP round-trip вместо 30. Если число
    сегментов не совпало (сервис склеил строку), возвращаем None: вызывающий
    код переведёт эти строки по одной, чтобы не перепутать местами.
    """
    import urllib.parse
    import urllib.request
    if not texts:
        return []
    q = "\n".join(str(t) for t in texts)
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode({
        "client": "gtx", "sl": "auto", "tl": tgt, "dt": "t", "q": q,
    })
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    segs = data[0] if isinstance(data, list) and data else []
    vals = []
    for item in segs or []:
        if isinstance(item, list) and item and isinstance(item[0], str):
            vals.append(item[0].rstrip("\n"))
        else:
            vals.append("")
    if len(vals) != len(texts):
        return None
    return vals


def _light_translate_many(unique, target_lang: str):
    """Перевести набор уникальных строк с минимальным числом запросов.

    Возвращает {строка: перевод}. Сначала кэш, затем пакеты по ~1200 символов
    (до 32 строк) в 4 потока; упавший пакет добирается по одной строке.
    """
    from concurrent.futures import ThreadPoolExecutor
    tgt = _target_code(target_lang)
    result = {}
    misses = []
    for s in unique or []:
        s = (s or "").strip()
        if len(s) < 2:
            continue
        key = (tgt, s)
        with _TR_CACHE_LOCK:
            cached = _TR_CACHE.get(key)
        if cached is not None:
            result[s] = cached
        else:
            misses.append(s)
    if not misses:
        return result

    batches, cur, cur_len = [], [], 0
    for s in misses:
        add = len(s) + 1
        if cur and (cur_len + add > 1200 or len(cur) >= 32):
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(s)
        cur_len += add
    if cur:
        batches.append(cur)

    def run_batch(batch):
        try:
            vals = _gtx_translate_batch(batch, tgt)
        except Exception as e:
            logger.debug(f"gtx batch failed ({len(batch)}): {e}")
            vals = None
        if vals is None:
            vals = []
            for s in batch:
                try:
                    vals.append(_light_translate_sync(s, target_lang))
                except Exception:
                    vals.append("")
        return batch, vals

    with ThreadPoolExecutor(max_workers=4) as pool:
        for batch, vals in pool.map(run_batch, batches):
            for s, v in zip(batch, vals):
                result[s] = v or ""
                if v:
                    with _TR_CACHE_LOCK:
                        if len(_TR_CACHE) > _TR_CACHE_MAX:
                            _TR_CACHE.clear()
                        _TR_CACHE[(tgt, s)] = v
    return result


def _split_for_translation(text: str, limit: int) -> list:
    """Режем по абзацам, чтобы не рвать предложения. Абзац, который сам больше
    лимита, режем по предложениям — иначе он не влезет в контекст модели."""
    parts, cur = [], ""
    for para in text.split("\n"):
        para = para.rstrip()
        if not para:
            if cur:
                parts.append(cur)
                cur = ""
            continue
        while len(para) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            cut = para.rfind(". ", 0, limit)
            if cut < limit // 2:
                cut = para.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            parts.append(para[:cut + 1].strip())
            para = para[cut + 1:].lstrip()
        candidate = (cur + "\n" + para) if cur else para
        if len(candidate) > limit:
            parts.append(cur)
            cur = para
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return [p for p in parts if p.strip()]


@router.post("/api/browser/translate")
async def api_browser_translate(request: Request):
    body = await request.json()
    text = body.get("text", "")
    target = str(body.get("target", "") or "русский").strip() or "русский"
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"error": "Нет текста для перевода"}, status_code=400)
    if len(text) > TRANSLATE_MAX_TOTAL:
        return JSONResponse({"error": "Страница слишком велика для перевода"}, status_code=413)
    chunks = _split_for_translation(text, TRANSLATE_MAX_CHARS)
    if not chunks:
        return JSONResponse({"error": "Нечего переводить"}, status_code=400)
    out = []
    for ch in chunks:
        # Лёгкий переводчик без LLM; при сбое оставляем оригинал.
        piece = await _run_sync(_light_translate_sync, ch, target)
        out.append(piece or ch)
    return {"ok": True, "text": "\n".join(out), "chunks": len(chunks), "target": target}

BROWSER_NODES_MAX = 400

@router.post("/api/browser/translate-nodes")
async def api_browser_translate_nodes(request: Request):
    """Перевод страницы «на месте»: браузер присылает список текстовых узлов,
    сервер возвращает переводы в том же порядке. Текст подменяется прямо в
    DOM, без reader-режима и потери вёрстки. Используется лёгкий веб-перевод
    (Google/MyMemory), а не локальная LLM: так страница переводится за секунды."""
    body = await request.json()
    nodes = body.get("nodes", [])
    target = str(body.get("target", "") or "русский").strip() or "русский"
    if not isinstance(nodes, list) or not nodes:
        return JSONResponse({"error": "Нет узлов для перевода"}, status_code=400)
    if len(nodes) > BROWSER_NODES_MAX:
        return JSONResponse({"error": f"Слишком много узлов (максимум {BROWSER_NODES_MAX})"},
                            status_code=413)
    total = sum(len(n) for n in nodes if isinstance(n, str))
    if total > TRANSLATE_MAX_TOTAL:
        return JSONResponse({"error": "Страница слишком велика для перевода"}, status_code=413)

    strings = [n if isinstance(n, str) else "" for n in nodes]
    # Уникальные строки переводим один раз: одинаковые пункты меню/подписи не
    # запрашиваются повторно. Сам перевод — пакетами по несколько строк на
    # запрос (_light_translate_many), это в разы меньше round-trip'ов.
    cores = []
    for s in strings:
        core = s.strip()
        cores.append(core if len(core) >= 2 else "")
    unique = list(dict.fromkeys(c for c in cores if c))
    mapping = await _run_sync(_light_translate_many, unique, target) if unique else {}
    out = []
    for s, core in zip(strings, cores):
        tr = mapping.get(core) if core else None
        if not tr:
            out.append(s)
            continue
        lead = s[:len(s) - len(s.lstrip())]
        trail = s[len(s.rstrip()):]
        out.append(lead + tr + trail)
    return {"ok": True, "translations": out, "target": target}

# --- Avatar ---
@router.get("/api/avatar/motions")
async def api_avatar_motions():
    if not server.AMS_AVAILABLE:
        return JSONResponse({"error": "Avatar motions недоступны"}, status_code=400)
    try:
        motions = await _run_sync(server.AMS.list_motions)
        return {"motions": motions}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/avatar/gateway/poll")
async def api_avatar_gateway_poll(idle_ms: int = 0, max: int = 16):
    if not server.AG_AVAILABLE:
        return {"events": [], "queue_size": 0}
    try:
        events = await _run_sync(server.AG.pop_batch, max_n=max, timeout_idle_ms=idle_ms)
        qsize = await _run_sync(server.AG.size)
        return {"events": events, "queue_size": qsize}
    except Exception as e:
        logger.error(f"/api/avatar/gateway/poll error: {e}")
        return {"events": [], "queue_size": 0}

# --- STT ---
@router.get("/api/stt/status")
async def api_stt_status():
    """Статус локального STT-движка (faster-whisper)."""
    if not server.STT_AVAILABLE:
        return JSONResponse({"error": "STT недоступен"}, status_code=400)
    st = await _run_sync(server.STT.status)
    st["engine"] = "local"
    return st

@router.post("/api/stt/transcribe")
async def api_stt_transcribe(request: Request):
    """Распознавание речи выбранным движком (из настроек или параметром engine).

    Движки: local (faster-whisper), openai, custom:<id>. Формат — как раньше.
    """
    import speech_engine
    try:
        import base64
        content_type = request.headers.get("content-type", "")
        audio_bytes = None
        lang = None
        engine = None
        model = None
        vad_params = None
        
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            file = form.get("file")
            if file and hasattr(file, "read"):
                audio_bytes = await file.read()
            elif file and isinstance(file, str):
                try:
                    audio_bytes = base64.b64decode(file)
                except Exception:
                    audio_bytes = file.encode("utf-8")
            lang = form.get("language")
            engine = form.get("engine") or form.get("provider") or None
            model = form.get("model") or None
            vad_raw = form.get("vad")
            if form.get("vad_threshold"):
                vad_params = {
                    "threshold": float(form.get("vad_threshold", 0.5)),
                    "min_speech_duration_ms": int(form.get("vad_min_speech_ms", 250)),
                    "max_speech_duration_s": float(form.get("vad_max_speech_s", 30.0)),
                    "min_silence_duration_ms": int(form.get("vad_min_silence_ms", 1000)),
                }
        else:
            body = await request.json()
            lang = body.get("language")
            engine = body.get("engine") or body.get("provider") or None
            model = body.get("model") or None
            vad_raw = body.get("vad")
            if "file" in body and isinstance(body["file"], str):
                try:
                    audio_bytes = base64.b64decode(body["file"])
                except Exception:
                    audio_bytes = body["file"].encode("utf-8")
            elif "audio" in body and isinstance(body["audio"], str):
                try:
                    audio_bytes = base64.b64decode(body["audio"])
                except Exception:
                    audio_bytes = body["audio"].encode("utf-8")
            if body.get("vad_threshold"):
                vad_params = {
                    "threshold": float(body.get("vad_threshold", 0.5)),
                    "min_speech_duration_ms": int(body.get("vad_min_speech_ms", 250)),
                    "max_speech_duration_s": float(body.get("vad_max_speech_s", 30.0)),
                    "min_silence_duration_ms": int(body.get("vad_min_silence_ms", 1000)),
                }
        
        if not audio_bytes:
            return JSONResponse({"error": "No audio data", "text": ""}, status_code=400)

        # vad="0" — полное распознавание без VAD (музыка/длинные файлы)
        vad_filter = str(vad_raw or "1").strip().lower() not in ("0", "false", "no", "off")

        if engine not in (None, "", "local"):
            # Локальный VAD применяется только для локального движка
            pass
        elif vad_params and getattr(server, "STT", None) is not None:
            await _run_sync(server.STT.set_vad_parameters, **vad_params)

        result = await worker_pool.run_async(
            speech_engine.transcribe_stt, audio_bytes, language=lang, engine=engine,
            vad_filter=vad_filter, model=model
        )
        return result
    except Exception as e:
        logger.error(f"/api/stt/transcribe error: {e}", exc_info=True)
        return JSONResponse({"error": str(e), "text": ""}, status_code=500)

@router.post("/api/stt/load")
async def api_stt_load(request: Request):
    if not server.STT_AVAILABLE:
        return JSONResponse({"error": "STT недоступен"}, status_code=400)
    body = await request.json()
    # Пустая модель — «авто»: resolve_model выберет base на CPU и turbo на CUDA.
    # Здесь стояло `or "turbo"`, и кнопка «Загрузить модель» при выборе «Авто»
    # принудительно грузила turbo — на CPU расшифровка шла в разы медленнее.
    ok = await worker_pool.run_async(
        server.STT.load,
        model=body.get("model") or None,
        language=body.get("language") or None,
        device=body.get("device") or None,
        compute_type=body.get("compute_type") or None,
    )
    st = await _run_sync(server.STT.status)
    return {"ok": ok, "status": st}

# --- Speech: реестр движков + тест ---
@router.get("/api/speech/engines")
async def api_speech_engines():
    """Статус всех TTS/STT движков и текущий выбор из настроек."""
    import speech_engine
    return await _run_sync(speech_engine.list_engines)

@router.post("/api/speech/test")
async def api_speech_test(request: Request):
    """Тест движка. Body: {type: 'tts'|'stt', engine?: 'silero'|..., config?: {...}}"""
    import speech_engine
    body = await request.json()
    return await _run_sync(
        speech_engine.test_engine,
        engine_type=body.get("type", "tts"),
        engine_id=body.get("engine") or None,
        config=body.get("config") or None,
    )

# --- Wakeword ---
@router.get("/api/wakeword/status")
async def api_wakeword_status():
    if not server.WW_AVAILABLE:
        return JSONResponse({"error": "Wakeword недоступен"}, status_code=400)
    return await _run_sync(server.WW.status)

@router.post("/api/wakeword/register")
async def api_wakeword_register(request: Request):
    if not server.WW_AVAILABLE:
        return JSONResponse({"error": "Wakeword недоступен"}, status_code=400)
    body = await request.json()
    word = body.get("word", "").strip().lower()
    threshold = float(body.get("threshold", 0.5))
    if not word:
        return JSONResponse({"error": "Нет слова"}, status_code=400)
    ok = await _run_sync(server.WW.register_word, word, threshold)
    return {"ok": ok, "word": word, "threshold": threshold}

@router.post("/api/wakeword/detect-text")
async def api_wakeword_detect_text(request: Request):
    body = await request.json()
    text = body.get("text", "").strip().lower()
    threshold = float(body.get("threshold", 0.3))
    if not text:
        return {"detected": False, "word": ""}
    
    words = await _run_sync(server.WW.list_words) if server.WW_AVAILABLE else []
    
    for w in words:
        if w in text:
            return {"detected": True, "word": w, "confidence": 1.0}
    
    interrupt_words = ["стоп", "замолчи", "финик", "хватит"]
    for w in interrupt_words:
        if w in text and w not in words:
            return {"detected": True, "word": f"_{w}", "confidence": 1.0, "interrupt": True}
    
    return {"detected": False, "word": ""}

# Auto-register wake word on startup
@router.on_event("startup")
async def _register_wakeword():
    if not server.WW_AVAILABLE:
        return
    def _run():
        try:
            server.WW.register_word("финик", threshold=0.3)
            logger.info("[WAKEWORD] Auto-registered 'финик' on startup")
        except Exception as e:
            logger.warning(f"[WAKEWORD] Failed to register 'финик': {e}")
    # В фоновом потоке: register_word прогревает STT-модель (~20-30с),
    # блокировать старт сервера этим нельзя.
    threading.Thread(target=_run, daemon=True, name="wakeword-register").start()

# Прогрев поисковых индексов (в фоне, чтобы первый поиск был мгновенным)
@router.on_event("startup")
async def _warmup_search_indexes():
    def _run():
        try:
            # Отложенный старт: не мешаем подъёму сервера (health-check).
            time.sleep(30)
            import search_all_sources
            # Лёгкие индексы (notes/ai/html) — в фоновых потоках,
            # тяжёлый news — в ОТДЕЛЬНОМ процессе (search_index_worker.py).
            search_all_sources.schedule_rebuilds()
            # Мониторинг готовности индексов (подхват результата воркера).
            search_all_sources.start_cache_monitor()
        except Exception as e:
            logger.warning(f"[SearchAll] warmup error: {e}")
    # Пониженный приоритет: сборка индексов + открытие многогигабайтного ZIM
    # (ZIM warmup) иначе «замораживают» HTTP-обработчики после старта.
    _start_low_priority_daemon(_run, name="search-all-warmup", delay=25)

# Прогрев файловых кэшей legacy-сервера (server.py).
# ВАЖНО: server.main() — где раньше жил прогрев — НЕ выполняется под uvicorn
# (он срабатывает только при прямом запуске server.py). Без этого хука
# после каждого рестарта первый заход на / или /desktop заново сканирует
# все jsonl (подсчёт строк) и zim (article_count) и тормозит минуты.
@router.on_event("startup")
async def _warm_server_file_caches():
    def _warm():
        # 1) ZIM: открытие архивов + article_count (самое тяжёлое)
        try:
            if getattr(server, "ZIM_AVAILABLE", False):
                dirs = list(server.ZIM_DIRS or []) + list(getattr(server, "DATA_DIRS", None) or [])
                if dirs:
                    zs = assets_index.find_zim_files(dirs)
                    logger.info(f"[Warmup] ZIM файлов: {len(zs)}")
        except Exception as e:
            logger.warning(f"[Warmup] ZIM: {e}")
        # 2) JSONL: подсчёт строк (кэшируется по size/mtime)
        try:
            fs = assets_index.find_files(server.DATA_DIRS)
            logger.info(f"[Warmup] JSONL файлов: {len(fs)}")
        except Exception as e:
            logger.warning(f"[Warmup] JSONL: {e}")
        # 3) STT: НЕ грузим при старте. faster-whisper — тяжёлая модель, её
        # предзагрузка здесь конкурирует с подъёмом UI и может «замораживать»
        # интерфейс на минуты. Модель грузится лениво:
        #   - по первому POST /api/stt/transcribe (через speech_engine)
        #   - по POST /api/stt/load (ручной прогрев из настроек)
        # Конфигурация (engine/model) обрабатывается там же, набор остаётся прежним.
        try:
            if getattr(server, "STT_AVAILABLE", False) and server.STT is not None:
                # Какая модель реально будет взята — с учётом профиля и устройства.
                _stt_model = server.STT.resolve_model(None, server.STT._pick_device())
                logger.info(f"[Warmup] STT: модель '{_stt_model}' отложена — загрузится при первом запросе транскрипции.")
        except Exception as e:
            logger.warning(f"[Warmup] STT: {e}")
    _start_low_priority_daemon(_warm, name="server-cache-warmup", delay=40)
