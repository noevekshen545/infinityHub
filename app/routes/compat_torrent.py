"""Торренты и медиастор, перенесённые из legacy-лестницы.

Раньше страницы `/torrents`, `/mediastore`, `/downloads` и 22 API-ветки
`/api/torrent/*`, `/api/repos*` обслуживал патченный `H` из `server/torrent.py`
(через реестры `GET_ROUTES`/`POST_ROUTES`, которые уже удалены). Здесь те же
ветки живут обычными FastAPI-маршрутами, иначе менеджер загрузок и каталог
репозиториев перестанут работать.

Логика не переписана: обработчики вызывают те же функции `server.torrent`,
что и раньше, поэтому тела ответов и коды совпадают. Блокирующие операции
(запуск helper'а, HTTP к helper'у, обход файлов, explorer.exe) вынесены в
threadpool — event loop не должен ждать torrent-хелпер.
"""

import base64
import json
import os
import subprocess
import sys

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from .compat_helpers import auth_denied, j, q1
import swallow

router = APIRouter()

_HTML_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


async def _json_body(request):
    """Мягкий разбор тела, как у legacy-веток: битый JSON -> {}."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        swallow.note('compat_torrent._json_body:L40')
        return {}
    return data if isinstance(data, dict) else {}


def _T():
    import server.torrent as T
    return T


def _helper_error(T):
    return getattr(T, "_helper_error", "Torrent helper is not available")


def _err(T, where, e):
    T.logger.error(f"{where} error: {e}", exc_info=True)
    return j({"error": str(e)}, 500)


async def _ensure_helper(T, with_list=False):
    """Проверка хелпера в threadpool; ответ — как у legacy-веток."""
    try:
        ok = await run_in_threadpool(T.ensure_torrent_helper)
    except Exception as e:
        T.logger.error(f"ensure_torrent_helper failed: {e}", exc_info=True)
        ok = False
    if ok:
        return None
    payload = {"error": _helper_error(T)}
    if with_list:
        payload["torrents"] = []
        return j(payload)
    return j(payload, 503)


# ---------------------------------------------------------------- страницы

def _page(template_attr, fallback):
    """Отдать PAGE_* с инъекцией темы — как legacy `_html`.

    Загрузчик расширений сюда не добавляется: его вставит middleware на
    выходе, как и для остальных FastAPI-страниц.
    """
    try:
        import server_backup as SB
        html = getattr(SB, template_attr, "") or ""
    except Exception:
        swallow.note('compat_torrent._page:L87')
        html = ""
    if not html:
        return HTMLResponse(fallback, headers=_HTML_CACHE)
    try:
        from webui_assets import inject as _inject_ui
        html = _inject_ui(html)
    except Exception:
        swallow.note('compat_torrent._page:L95')
        pass
    return HTMLResponse(html, headers=_HTML_CACHE)


@router.get("/torrents")
def torrents_page(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    return _page("PAGE_MEDIA", "<h1>Торренты</h1><p>Медиа модуль недоступен</p>")


@router.get("/mediastore")
def mediastore_page(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    return _page("PAGE_STORE", "<h1>Медиастор</h1><p>Шаблон не найден</p>")


@router.get("/downloads")
def downloads_page(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    return _page("PAGE_DOWNLOADS", "<h1>Загрузки</h1><p>Шаблон не найден</p>")


# ---------------------------------------------------------------- GET API

@router.get("/api/torrent/list")
async def torrent_list(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    early = await _ensure_helper(T, with_list=True)
    if early is not None:
        return early
    try:
        return j(await run_in_threadpool(lambda: T._fix_torrent_names(T._torrent_api("/list"))))
    except Exception as e:
        swallow.note('compat_torrent.torrent_list:L138', e)
        return _err(T, "/api/torrent/list", e)


@router.get("/api/torrent/status")
async def torrent_status(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    early = await _ensure_helper(T)
    if early is not None:
        return early
    info_hash = q1(request, "hash", "")
    if not info_hash:
        return j({"error": "Missing infoHash"}, 400)
    try:
        return j(await run_in_threadpool(
            lambda: T._fix_torrent_names(T._torrent_api(f"/status/{info_hash}"))))
    except Exception as e:
        swallow.note('compat_torrent.torrent_status:L158', e)
        return _err(T, "/api/torrent/status", e)


@router.get("/api/torrent/settings")
async def torrent_settings(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    try:
        return j(await run_in_threadpool(_settings_sync, T))
    except Exception as e:
        swallow.note('compat_torrent.torrent_settings:L171', e)
        return _err(T, "/api/torrent/settings", e)


def _settings_sync(T):
    T._update_torrent_cfg()
    media = T._original.CFG.get("media", {}) or {}
    return {
        "download_dir": T.TORRENT_DOWNLOAD_DIR,
        "auto_sort": bool(T.TORRENT_AUTO_SORT),
        "max_down_kbs": int(media.get("torrent_max_down_kbs", 0) or 0),
        "max_up_kbs": int(media.get("torrent_max_up_kbs", 0) or 0),
    }


@router.get("/api/torrent/files")
async def torrent_files(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    cat = (q1(request, "category", "") or "all").strip()
    scope = (q1(request, "scope", "") or "downloaded").strip()
    try:
        files = await run_in_threadpool(_files_sync, T, cat, scope)
        return j({"files": files})
    except Exception as e:
        swallow.note('compat_torrent.torrent_files:L198', e)
        return _err(T, "/api/torrent/files", e)


def _files_sync(T, cat, scope):
    files = T._iter_downloaded_files(include_library=scope in ("all", "library"))
    if cat and cat != "all":
        files = [f for f in files if f.get("kind") == cat]
    return T._fix_torrent_names(files)


@router.get("/api/torrent/create/status")
async def torrent_create_status(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    early = await _ensure_helper(T)
    if early is not None:
        return early
    task_id = q1(request, "taskId", "") or q1(request, "task_id", "")
    if not task_id:
        return j({"error": "Missing taskId"}, 400)
    try:
        return j(await run_in_threadpool(
            lambda: T._torrent_api(f"/create-torrent/{task_id}/status")))
    except Exception as e:
        swallow.note('compat_torrent.torrent_create_status:L225', e)
        return _err(T, "/api/torrent/create/status", e)


@router.get("/api/repos")
async def repos_list(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    try:
        return j(await run_in_threadpool(_repos_list_sync, T))
    except Exception as e:
        swallow.note('compat_torrent.repos_list:L238', e)
        return _err(T, "/api/repos", e)


def _repos_list_sync(T):
    T._seed_builtin_repos()
    return {"repos": T._load_repos_index(), "dir": T.REPOS_DIR}


@router.get("/api/repos/items")
async def repos_items(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    repo_id = q1(request, "id", "")
    if not repo_id:
        return j({"error": "Missing repo id"}, 400)
    try:
        data = await run_in_threadpool(T._load_repo, repo_id)
    except Exception as e:
        swallow.note('compat_torrent.repos_items:L259', e)
        return _err(T, "/api/repos/items", e)
    if not data:
        return j({"error": "Repository not found"}, 404)
    return j({
        "id": repo_id,
        "name": data.get("name", repo_id),
        "description": data.get("description", ""),
        "items": data.get("items", []),
    })


# ---------------------------------------------------------------- POST API

@router.post("/api/torrent/add")
async def torrent_add(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    early = await _ensure_helper(T)
    if early is not None:
        return early
    data = await _json_body(request)
    magnet = data.get("magnet", "")
    content_type = data.get("type", "video") or "video"
    file_base64 = data.get("file_base64", "")
    if not magnet and not file_base64:
        return j({"error": "No magnet link or torrent file provided"}, 400)
    add_data = {"type": content_type, "output_dir": T.TORRENT_DOWNLOAD_DIR}
    if magnet:
        add_data["magnet"] = magnet
    if file_base64:
        add_data["file_base64"] = file_base64
    try:
        return j(await run_in_threadpool(
            lambda: T._torrent_api("/add", method="POST", data=add_data, timeout=300)))
    except Exception as e:
        swallow.note('compat_torrent.torrent_add:L297', e)
        return _err(T, "/api/torrent/add", e)


@router.post("/api/torrent/remove")
async def torrent_remove(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    data = await _json_body(request)
    info_hash = data.get("infoHash", "")
    if not info_hash:
        return j({"error": "Missing infoHash"}, 400)
    # Хелпер здесь намеренно не проверяется: legacy-ветка тоже вызывала API
    # напрямую и отдавала 200 с {"error": ...} при недоступном хелпере.
    try:
        return j(await run_in_threadpool(
            lambda: T._torrent_api(f"/remove/{info_hash}", method="POST")))
    except Exception as e:
        swallow.note('compat_torrent.torrent_remove:L317', e)
        return _err(T, "/api/torrent/remove", e)


@router.post("/api/torrent/callback")
async def torrent_callback(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    data = await _json_body(request)
    action = data.get("action", "")
    info_hash = data.get("infoHash", "")
    if action == "done" and info_hash:
        try:
            await run_in_threadpool(_callback_sync, T, info_hash)
        except Exception as e:
            swallow.note('compat_torrent.torrent_callback:L334', e)
            return _err(T, "/api/torrent/callback", e)
    return j({"status": "ok"})


def _callback_sync(T, info_hash):
    T._update_torrent_cfg()
    T.logger.info(f"Torrent callback: done {info_hash}")
    tdata = T._torrent_api(f"/status/{info_hash}")
    T._move_completed_files(tdata)


@router.post("/api/torrent/settings/save")
async def torrent_settings_save(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    data = await _json_body(request)
    try:
        ok, result = await run_in_threadpool(_settings_save_sync, T, data)
        return j(result, 200 if ok else 503)
    except Exception as e:
        swallow.note('compat_torrent.torrent_settings_save:L357', e)
        return _err(T, "/api/torrent/settings/save", e)


def _settings_save_sync(T, data):
    media = T._original.CFG.get("media", {}) or {}
    if "download_dir" in data and isinstance(data["download_dir"], str) and data["download_dir"].strip():
        media["torrent_download_dir"] = os.path.expanduser(data["download_dir"].strip())
    if "auto_sort" in data:
        media["torrent_auto_sort"] = bool(data["auto_sort"])
    if "max_down_kbs" in data:
        try:
            media["torrent_max_down_kbs"] = max(0, int(data["max_down_kbs"]))
        except (TypeError, ValueError):
            swallow.note('compat_torrent._settings_save_sync:L371')
            pass
    if "max_up_kbs" in data:
        try:
            media["torrent_max_up_kbs"] = max(0, int(data["max_up_kbs"]))
        except (TypeError, ValueError):
            swallow.note('compat_torrent._settings_save_sync:L377')
            pass
    T._original.CFG["media"] = media
    T._update_torrent_cfg()
    try:
        from config_module import save_config
        save_config(T._original.CFG)
    except Exception as e:
        T.logger.warning(f"Не удалось сохранить конфиг: {e}")
    if not T.ensure_torrent_helper():
        return False, {"error": _helper_error(T)}
    try:
        T._torrent_api("/limits", method="POST", data={
            "download": int(media.get("torrent_max_down_kbs", 0) or 0) * 1024,
            "upload": int(media.get("torrent_max_up_kbs", 0) or 0) * 1024,
        })
    except Exception as e:
        T.logger.warning(f"Не удалось применить лимиты скорости: {e}")
    return True, {"ok": True}


@router.post("/api/torrent/create")
async def torrent_create(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    early = await _ensure_helper(T)
    if early is not None:
        return early
    data = await _json_body(request)
    files = data.get("files", [])
    if not isinstance(files, list) or not files:
        return j({"error": "No files provided"}, 400)
    files = [os.path.abspath(os.path.expanduser(f)) for f in files if isinstance(f, str)]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return j({"error": "No existing files found"}, 400)
    name = data.get("name", "") or None
    try:
        return j(await run_in_threadpool(
            lambda: T._torrent_api("/create-torrent", method="POST",
                                   data={"files": files, "name": name}, timeout=30)))
    except Exception as e:
        swallow.note('compat_torrent.torrent_create:L421', e)
        return _err(T, "/api/torrent/create", e)


@router.post("/api/torrent/pause")
async def torrent_pause(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    early = await _ensure_helper(T)
    if early is not None:
        return early
    data = await _json_body(request)
    info_hash = data.get("infoHash", "")
    if not info_hash:
        return j({"error": "Missing infoHash"}, 400)
    try:
        return j(await run_in_threadpool(
            lambda: T._torrent_api(f"/pause/{info_hash}", method="POST")))
    except Exception as e:
        swallow.note('compat_torrent.torrent_pause:L442', e)
        return _err(T, "/api/torrent/pause", e)


@router.post("/api/torrent/resume")
async def torrent_resume(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    early = await _ensure_helper(T)
    if early is not None:
        return early
    data = await _json_body(request)
    info_hash = data.get("infoHash", "")
    if not info_hash:
        return j({"error": "Missing infoHash"}, 400)
    try:
        return j(await run_in_threadpool(
            lambda: T._torrent_api(f"/resume/{info_hash}", method="POST")))
    except Exception as e:
        swallow.note('compat_torrent.torrent_resume:L463', e)
        return _err(T, "/api/torrent/resume", e)


@router.post("/api/torrent/open-folder")
async def torrent_open_folder(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    data = await _json_body(request)
    fp = (data.get("path") or "").strip()
    if not fp:
        return j({"error": "Missing path"}, 400)
    fp = os.path.expanduser(fp)
    if not os.path.exists(fp):
        return j({"error": "Файл не найден"}, 404)
    try:
        return j(await run_in_threadpool(_open_folder_sync, fp))
    except Exception as e:
        T.logger.error(f"open-folder error: {e}", exc_info=True)
        return j({"error": f"Не удалось открыть проводник: {e}"}, 500)


def _open_folder_sync(fp):
    folder = fp if os.path.isdir(fp) else os.path.dirname(fp)
    if sys.platform == "win32":
        if os.path.isfile(fp):
            subprocess.Popen(["explorer.exe", "/select," + fp])
        else:
            subprocess.Popen(["explorer.exe", folder])
        return {"ok": True}
    try:
        wsl = subprocess.run(["wslpath", "-w", folder], capture_output=True,
                             text=True, timeout=15)
        winpath = wsl.stdout.strip()
        if not winpath:
            raise OSError("wslpath вернул пустой путь")
        if os.path.isfile(fp):
            wsl_fp = subprocess.run(["wslpath", "-w", fp], capture_output=True,
                                    text=True, timeout=15).stdout.strip()
            subprocess.Popen(["explorer.exe", "/select," + wsl_fp],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["explorer.exe", winpath],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
    except OSError:
        import shutil
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", folder])
            return {"ok": True}
        raise


@router.post("/api/torrent/fix-names")
async def torrent_fix_names(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    data = await _json_body(request)
    dirs = data.get("dirs") or []
    if not dirs:
        music_dirs, video_dirs = T._get_media_dirs()
        dirs = list(music_dirs) + list(video_dirs) + [T.TORRENT_DOWNLOAD_DIR]
    try:
        renamed = await run_in_threadpool(T._rename_mojibake_in_dirs, dirs)
    except Exception as e:
        swallow.note('compat_torrent.torrent_fix_names:L532', e)
        return _err(T, "/api/torrent/fix-names", e)
    if renamed:
        try:
            T._trigger_reindex(["music", "video"])
        except Exception as e:
            T.logger.warning(f"fix-names reindex: {e}")
    return j({"ok": True, "renamed": renamed})


@router.post("/api/repos/add")
async def repos_add(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    data = await _json_body(request)
    try:
        status, result = await run_in_threadpool(_repos_add_sync, T, data)
        return j(result, status)
    except Exception as e:
        T.logger.error(f"Repos add error: {e}", exc_info=True)
        return j({"error": f"Ошибка добавления: {e}"}, 500)


def _repos_add_sync(T, data):
    payload = data.get("repo") if isinstance(data.get("repo"), dict) else None
    if payload is not None:
        try:
            repo = T._validate_repo(payload)
        except ValueError as ve:
            swallow.note('compat_torrent._repos_add_sync:L563', ve)
            return 500, {"error": str(ve)}
        if not isinstance(repo, dict):
            return 400, {"error": "Невалидный репозиторий"}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        repo_id = T.hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        entry = T._save_repo_entry(repo, repo_id, "local:json")
        return 200, {"ok": True, "repo": entry}
    source = (data.get("source") or "").strip()
    if not source:
        return 400, {"error": "Missing source (URL или путь к JSON-файлу)"}
    if source in T.BUILTIN_REPO_SOURCES or source.startswith("builtin:"):
        T._seed_builtin_repos()
        entry = next((e for e in T._load_repos_index()
                      if e.get("id") == T._builtin_repo_id(source)), None)
        if entry:
            return 200, {"ok": True, "repo": entry}
        return 503, {"error": "Встроенный каталог недоступен"}
    if T.re.match(r'^https?://', source, T.re.I):
        req = T.urllib.request.Request(source, headers={"User-Agent": "Infinity-Mediastor/1.0"})
        with T.urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    else:
        src_path = os.path.abspath(os.path.expanduser(source))
        if not os.path.isfile(src_path):
            return 404, {"error": f"Файл не найден: {src_path}"}
        with open(src_path, "r", encoding="utf-8") as f:
            raw = f.read().encode("utf-8")
    try:
        repo = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as je:
        swallow.note('compat_torrent._repos_add_sync:L594', je)
        return 500, {"error": f"Невалидный JSON: {je}"}
    try:
        repo = T._validate_repo(repo)
    except ValueError as ve:
        swallow.note('compat_torrent._repos_add_sync:L599', ve)
        return 500, {"error": str(ve)}
    if repo is False or repo is None:
        return 400, {"error": "Невалидный репозиторий"}
    repo_id = T.hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
    entry = T._save_repo_entry(repo, repo_id, source)
    return 200, {"ok": True, "repo": entry}


@router.post("/api/repos/remove")
async def repos_remove(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    data = await _json_body(request)
    repo_id = data.get("id", "")
    if not repo_id:
        return j({"error": "Missing repo id"}, 400)
    try:
        await run_in_threadpool(_repos_remove_sync, T, repo_id)
    except Exception as e:
        swallow.note('compat_torrent.repos_remove:L621', e)
        return _err(T, "/api/repos/remove", e)
    return j({"ok": True})


def _repos_remove_sync(T, repo_id):
    index = [e for e in T._load_repos_index() if e.get("id") != repo_id]
    T._save_repos_index(index)
    try:
        os.remove(T._repo_file(repo_id))
    except OSError:
        swallow.note('compat_torrent._repos_remove_sync:L632')
        pass


@router.post("/api/repos/download")
async def repos_download(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    T = _T()
    early = await _ensure_helper(T)
    if early is not None:
        return early
    data = await _json_body(request)
    try:
        status, result = await run_in_threadpool(_repos_download_sync, T, data)
        return j(result, status)
    except Exception as e:
        swallow.note('compat_torrent.repos_download:L650', e)
        return _err(T, "/api/repos/download", e)


def _repos_download_sync(T, data):
    item = data.get("item") if isinstance(data.get("item"), dict) else None
    if not item:
        repo_id = data.get("repoId", "") or data.get("repo_id", "")
        item_index = data.get("itemIndex", -1)
        repo = T._load_repo(repo_id) if repo_id else None
        if not repo:
            return 404, {"error": "Repository not found"}
        items = repo.get("items", [])
        if not isinstance(item_index, int) or item_index < 0 or item_index >= len(items):
            return 404, {"error": "Item not found"}
        item = items[item_index]
    magnet = (item.get("magnet") or "").strip()
    torrent_url = (item.get("torrent_url") or "").strip()
    content_type = item.get("type") or "video"
    if not magnet and not torrent_url:
        return 400, {"error": "Item has no magnet or torrent_url"}
    if magnet:
        result = T._torrent_api("/add", method="POST", data={
            "magnet": magnet, "type": content_type,
            "output_dir": T.TORRENT_DOWNLOAD_DIR,
        }, timeout=300)
    else:
        try:
            req = T.urllib.request.Request(torrent_url,
                                           headers={"User-Agent": "infinity-torrent/1.0"})
            with T.urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            file_base64 = base64.b64encode(raw).decode("ascii")
        except Exception as e:
            swallow.note('compat_torrent._repos_download_sync:L684', e)
            return 500, {"error": f"Не удалось скачать .torrent: {e}"}
        result = T._torrent_api("/add", method="POST", data={
            "file_base64": file_base64, "type": content_type,
            "output_dir": T.TORRENT_DOWNLOAD_DIR,
        }, timeout=300)
    result["item"] = item.get("title", "")
    return 200, result
