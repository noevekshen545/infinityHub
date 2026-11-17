"""Extension & Marketplace API routes — вынесены из main.py."""

import os, json, asyncio, logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

import server
import worker_pool
import profile_paths
import swallow

logger = logging.getLogger("infinity")

# Каталог расширений живёт в модульном глобале extension_system.BASE_DIR, и
# менеджер читает его в момент файловой операции, а не при создании:
# get_manager_for(dir) возвращает глобал обратно ДО того, как вызывающий
# что-нибудь установит, поэтому «взять менеджер чужого профиля» — это не
# изоляция. Каталог запроса держится эксклюзивно на всю обработку: один запрос
# к расширениям за раз, зато в своём каталоге и со своим extensions_state.json.
_EXT_DIR_LOCK = asyncio.Lock()


async def ext_dir_scope(conn: HTTPConnection):
    """Каталог расширений профиля на время HTTP-обработки.

    WebSocket сюда не пускаем в критическую секцию: обработчик hot-reload
    каталог расширений не читает, а lock держится всё время соединения — с
    исправленной зависимостью bind (она раньше падала на WS) живое WS
    заблокировало бы /api/extensions/frontend-scripts навсегда.
    """
    if isinstance(conn, WebSocket):
        yield
        return
    import extension_system as ES
    target = os.path.normpath(str(profile_paths.current("EXTENSIONS_DIR", server) or ""))
    if not target:
        yield
        return
    async with _EXT_DIR_LOCK:
        prev = os.path.normpath(str(ES.BASE_DIR))
        if target != prev:
            ES.set_base_dir(target)
        try:
            yield
        finally:
            if os.path.normpath(str(ES.BASE_DIR)) != prev:
                ES.set_base_dir(prev)


# bind обязателен первым: без него ext_dir_scope не знает, чей каталог брать,
# и менеджер берёт каталог «последнего переключившегося» профиля.
router = APIRouter(dependencies=[Depends(profile_paths.bind), Depends(ext_dir_scope)])


def _run_sync(fn, *args, **kw):
    return worker_pool.run_in_pool(worker_pool.HIGH_POOL, fn, *args, **kw)


def _get_mgr():
    from extension_system import get_manager
    return get_manager()


# ---- Extension System API ----


@router.get("/api/extensions")
async def extension_list():
    try:
        mgr = _get_mgr()
        extensions, stats = await _run_sync(lambda: (mgr.get_extensions(), mgr.get_stats()))
        return {"extensions": extensions, "stats": stats}
    except Exception as e:
        logger.error(f"Extension list error: {e}")
        return {"extensions": [], "stats": {}}


def _laya_extension_warning(ext_name):
    """Оценка риска расширения по его манифесту (laya).

    Возвращает предупреждение (dict) или None: фича выключена, движок
    недоступен или манифеста нет. Установку не блокирует — предупреждение
    показывается пользователю.
    """
    try:
        import laya_client
    except Exception:
        swallow.note('extension._laya_extension_warning:L48')
        return None
    try:
        if not laya_client.feature("extensions_access_warning"):
            return None
        if not ext_name:
            return None
        import extension_system
        manifest_path = os.path.join(extension_system.BASE_DIR, ext_name, "extension.json")
        if not os.path.isfile(manifest_path):
            return None
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        perms = manifest.get("permissions") or []
        text = (
            "Расширение: %s\nОписание: %s\nРазрешения: %s" % (
                manifest.get("name") or ext_name,
                (manifest.get("description") or "")[:600],
                ", ".join(str(p) for p in perms) or "нет",
            )
        )
        res = laya_client.classify_extension(text, permissions=perms)
        if not res:
            return None
        return {
            "risk": res.get("risk"),
            "why": res.get("why"),
            "confidence": res.get("confidence"),
            "permissions": perms,
        }
    except Exception as e:
        logger.debug(f"laya extension warning failed: {e}")
        return None


@router.post("/api/extensions/upload")
async def extension_upload(request: Request):
    try:
        body = await request.body()
        mgr = _get_mgr()
        result = await _run_sync(mgr.install_from_zip, body)
        if isinstance(result, dict) and result.get("ok"):
            warn = _laya_extension_warning(result.get("name") or result.get("extension"))
            if warn:
                result["laya_warning"] = warn
        return result
    except Exception as e:
        swallow.note('extension.extension_upload:L95', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/install-git")
async def extension_install_git(request: Request):
    try:
        body = await request.json()
        git_url = body.get("url", "")
        if not git_url:
            return {"ok": False, "error": "URL is required"}
        mgr = _get_mgr()
        result = await _run_sync(mgr.install_from_git, git_url)
        return result
    except Exception as e:
        swallow.note('extension.extension_install_git:L110', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/{name}/enable")
async def extension_enable(name: str):
    try:
        mgr = _get_mgr()
        ext = mgr.extensions.get(name)
        if ext is None:
            return {"ok": False, "error": "Extension not found"}
        if ext.enabled:
            return {"ok": True}
        # Первый запуск: сначала пользователь должен выбрать режим (песочница/напрямую)
        if not ext.sandbox_asked:
            out = {
                "ok": False,
                "need_sandbox_choice": True,
                "sandbox": bool(ext.sandbox),
                "error": "Выберите режим запуска расширения"
            }
            # Оценка доступа по манифесту (laya) — подсказка перед запуском.
            warn = _laya_extension_warning(name)
            if warn:
                out["access_warning"] = warn
            return out
        ok = await _run_sync(mgr.enable, name)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_enable:L139', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/{name}/sandbox-status")
def extension_sandbox_status(name: str):
    try:
        mgr = _get_mgr()
        ext = mgr.extensions.get(name)
        if ext is None:
            return {"error": "Extension not found"}
        return {"asked": bool(ext.sandbox_asked), "sandbox": bool(ext.sandbox)}
    except Exception as e:
        swallow.note('extension.extension_sandbox_status:L152', e)
        return {"error": str(e)}


@router.post("/api/extensions/{name}/sandbox-choice")
async def extension_sandbox_choice(name: str, request: Request):
    try:
        body = await request.json()
        mgr = _get_mgr()
        ok = await _run_sync(mgr.set_sandbox_mode, name, bool(body.get("sandbox", True)))
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_sandbox_choice:L164', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/{name}/disable")
async def extension_disable(name: str):
    try:
        mgr = _get_mgr()
        ok = await _run_sync(mgr.disable, name)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_disable:L175', e)
        return {"ok": False, "error": str(e)}


@router.delete("/api/extensions/{name}")
async def extension_delete(name: str):
    try:
        mgr = _get_mgr()
        ok = await _run_sync(mgr.delete, name)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_delete:L186', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/{name}/reload")
async def extension_reload(name: str):
    try:
        mgr = _get_mgr()
        ok = await _run_sync(mgr.reload_extension, name)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_reload:L197', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/by-module/{module}")
async def extension_by_module(module: str):
    try:
        mgr = _get_mgr()
        extensions = await _run_sync(mgr.get_by_module, module)
        return {"extensions": extensions}
    except Exception as e:
        swallow.note('extension.extension_by_module:L208', e)
        return {"extensions": []}


# Псевдонимы id модулей. Канонический id файлового менеджера — "files"
# (extension_system.VALID_MODULES), но в URL живёт сегмент "filemanager",
# а пользователи называют модуль "file". Загрузчик на странице знает эти
# подстановки; серверный фильтр обязан знать их тоже, иначе ?module=file
# молча вернул бы пустой список.
_MODULE_ALIASES = {
    "file": "files",
    "filemanager": "files",
    "file-manager": "files",
    "ha": "ha_panel",
}


@router.get("/api/extensions/frontend-scripts")
async def extension_frontend_scripts(module: str = ""):
    try:
        mgr = _get_mgr()
        scripts = await _run_sync(mgr.get_frontend_scripts)
        if module:
            wanted = _MODULE_ALIASES.get(module, module)
            filtered = []
            for s in scripts:
                mod = s.get("module", "")
                target_mods = s.get("target_modules", [mod])
                if mod == wanted or mod == "all" or wanted in target_mods:
                    filtered.append(s)
            scripts = filtered
        return {"scripts": scripts}
    except Exception as e:
        swallow.note('extension.extension_frontend_scripts:L241', e)
        return {"scripts": []}


@router.post("/api/extensions/create-test")
async def extension_create_test():
    try:
        mgr = _get_mgr()
        result = await _run_sync(mgr.create_test_extension)
        return result
    except Exception as e:
        swallow.note('extension.extension_create_test:L252', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/api-docs")
async def extension_api_docs(request: Request):
    try:
        mgr = _get_mgr()
        docs = await _run_sync(lambda: mgr.get_api_docs(request.app))
        return {"routes": docs}
    except Exception as e:
        swallow.note('extension.extension_api_docs:L263', e)
        return {"routes": []}


@router.get("/api/extensions/{name}/check")
def extension_check(name: str):
    from extension_system import BASE_DIR
    exists = os.path.exists(os.path.join(BASE_DIR, name))
    return {"exists": exists, "name": name}


@router.get("/api/extensions/{name}/export")
async def extension_export(name: str):
    try:
        mgr = _get_mgr()
        data = await _run_sync(mgr.export_extension, name)
        if data:
            from starlette.responses import Response
            return Response(content=data, media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})
        return {"ok": False, "error": "Extension not found"}
    except Exception as e:
        swallow.note('extension.extension_export:L285', e)
        return {"ok": False, "error": str(e)}


# ---- .inf (исполняемые файлы Infinity) ---------------------------------

def _inf_read(path: str):
    """Прочитать .inf-файл с диска: (bytes) либо (None, сообщение об ошибке).

    Путь проверяем тем же правилом, что и файловый менеджер, — иначе через эти
    три роута можно было бы указать произвольный файл на диске.
    """
    import os as _os
    if not path:
        return None, "Файл не указан"
    try:
        from file_manager import is_path_allowed
        if not is_path_allowed(path):
            return None, "Путь запрещён политикой файлового менеджера"
    except ImportError:
        swallow.note('extension._inf_read:L305')
        pass
    if not _os.path.isfile(path):
        return None, "Файл не найден"
    try:
        with open(path, "rb") as f:
            return f.read(), None
    except Exception as e:
        swallow.note('extension._inf_read:L313', e)
        return None, f"Не удалось прочитать файл: {e}"


@router.get("/api/extensions/{name}/export-inf")
async def extension_export_inf(name: str):
    """Скачать расширение одним файлом .inf (JSON с расширением .inf)."""
    try:
        mgr = _get_mgr()
        data = await _run_sync(mgr.export_extension_inf, name, "extension")
        if data:
            from starlette.responses import Response
            return Response(content=data, media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{name}.inf"'})
        return {"ok": False, "error": "Extension not found"}
    except Exception as e:
        logger.error(f"export-inf error: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/inf-inspect")
async def extension_inf_inspect(request: Request):
    """Разобрать .inf без установки — для предпросмотра и мини-окна установщика.

    Тело: {"path": "...\\file.inf"} — читаем с диска. Либо {"raw": "<json>"} —
    если файл уже в памяти (например, из base64 при загрузке).
    """
    try:
        from extension_system import parse_inf, inf_summary
        body = await request.json()
    except Exception:
        swallow.note('extension.extension_inf_inspect:L344')
        return {"ok": False, "error": "Некорректный JSON"}

    raw = None
    path = str(body.get("path") or "").strip()
    if path:
        raw, err = _inf_read(path)
        if err:
            return {"ok": False, "error": err}
    elif isinstance(body.get("raw"), str):
        raw = body["raw"].encode("utf-8")

    if raw is None:
        return {"ok": False, "error": "Нужно поле path или raw"}

    try:
        data = parse_inf(raw)
    except ValueError as e:
        swallow.note('extension.extension_inf_inspect:L362', e)
        return {"ok": False, "error": str(e)}

    info = inf_summary(data)
    try:
        from extension_sandbox import ALL_PERMISSIONS
        info["permission_descriptions"] = {
            p: ALL_PERMISSIONS.get(p, p) for p in info.get("permissions", [])
        }
        info["all_permissions"] = ALL_PERMISSIONS
    except Exception:
        swallow.note('extension.extension_inf_inspect:L373')
        info["permission_descriptions"] = {}
        info["all_permissions"] = {}
    return {"ok": True, "info": info}


@router.post("/api/extensions/install-inf")
async def extension_install_inf(request: Request):
    """Установить расширение из .inf-файла (kind="extension").

    Тело: {"path": "...\\file.inf"} либо {"raw": "<json>"}, плюс необязательные
    "sandbox" (bool) и "permissions" (list) из мини-окна установщика.
    Расширение ставится ВЫКЛЮЧЕННЫМ — включает пользователь.
    """
    try:
        body = await request.json()
    except Exception:
        swallow.note('extension.extension_install_inf:L390')
        return {"ok": False, "error": "Некорректный JSON"}

    raw = None
    path = str(body.get("path") or "").strip()
    if path:
        raw, err = _inf_read(path)
        if err:
            return {"ok": False, "error": err}
    elif isinstance(body.get("raw"), str):
        raw = body["raw"].encode("utf-8")
    elif isinstance(body.get("data"), str):
        import base64 as _b64
        try:
            raw = _b64.b64decode(body["data"])
        except Exception:
            swallow.note('extension.extension_install_inf:L406')
            return {"ok": False, "error": "Поле data не является base64"}

    if raw is None:
        return {"ok": False, "error": "Нужно поле path, raw или data"}

    sandbox = body.get("sandbox")
    if sandbox is not None and not isinstance(sandbox, bool):
        sandbox = str(sandbox).lower() in ("1", "true", "yes", "on")
    permissions = body.get("permissions")
    if permissions is not None and not isinstance(permissions, list):
        return {"ok": False, "error": "permissions должен быть списком"}

    mgr = _get_mgr()
    result = await _run_sync(mgr.install_from_inf, raw, sandbox, permissions,
                             bool(body.get("force")))
    if result.get("ok") and result.get("name"):
        try:
            await notify_hot_reload(result["name"])
        except Exception:
            swallow.note('extension.extension_install_inf:L426')
            pass
    return result


@router.post("/api/extensions/inf-open")
async def extension_inf_open(request: Request):
    """Подготовить .inf-приложение (kind="app") к открытию в окне.

    Возвращает token, url страницы и pending_permissions — расширенные права,
    по которым пользователь ещё не давал согласия. Ничего не устанавливает.
    """
    try:
        body = await request.json()
    except Exception:
        swallow.note('extension.extension_inf_open:L441')
        return {"ok": False, "error": "Некорректный JSON"}

    path = str(body.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "Не указан путь к .inf"}
    raw, err = _inf_read(path)
    if err:
        return {"ok": False, "error": err}

    try:
        from routes.inf_apps import load_app, pending_permissions
    except ImportError:
        swallow.note('extension.extension_inf_open:L454')
        from inf_apps import load_app, pending_permissions

    try:
        app = await _run_sync(load_app, raw)
    except ValueError as e:
        swallow.note('extension.extension_inf_open:L460', e)
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error(f"inf-open error: {e}")
        return {"ok": False, "error": str(e)}

    token = app["token"]
    pending = await _run_sync(pending_permissions, app["payload"], token)
    return {"ok": True, "token": token, "url": f"/inf-app/{token}",
            "info": app["info"], "pending_permissions": pending}


@router.post("/api/extensions/inf-consent")
async def extension_inf_consent(request: Request):
    """Запомнить решение пользователя по правам .inf-приложения.

    {"token": "...", "permissions": ["files:read", ...]} — запомнить;
    {"token": "...", "revoke": true} — забыть (в следующий раз спросим снова).
    """
    try:
        body = await request.json()
    except Exception:
        swallow.note('extension.extension_inf_consent:L482')
        return {"ok": False, "error": "Некорректный JSON"}
    token = str(body.get("token") or "").strip()
    if not token:
        return {"ok": False, "error": "Не указан token"}

    try:
        import re as _re
        if not _re.match(r"^[A-Za-z0-9_.-]+$", token):
            return {"ok": False, "error": "Некорректный token"}
    except Exception:
        swallow.note('extension.extension_inf_consent:L493')
        return {"ok": False, "error": "Некорректный token"}

    try:
        if body.get("revoke"):
            from routes.inf_apps import revoke_consent
        else:
            from routes.inf_apps import remember_consent
    except ImportError:
        swallow.note('extension.extension_inf_consent:L502')
        if body.get("revoke"):
            from inf_apps import revoke_consent
        else:
            from inf_apps import remember_consent

    try:
        if body.get("revoke"):
            await _run_sync(revoke_consent, token)
            return {"ok": True, "revoked": True}
        perms = body.get("permissions")
        if not isinstance(perms, list):
            return {"ok": False, "error": "permissions должен быть списком"}
        try:
            from extension_sandbox import ALL_PERMISSIONS
            unknown = [str(p) for p in perms if str(p) not in ALL_PERMISSIONS]
            if unknown:
                return {"ok": False, "error": "Неизвестные права: " + ", ".join(unknown)}
        except Exception:
            swallow.note('extension.extension_inf_consent:L521')
            pass
        await _run_sync(remember_consent, token, [str(p) for p in perms])
        return {"ok": True, "permissions": [str(p) for p in perms]}
    except Exception as e:
        logger.error(f"inf-consent error: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/{name}/package-download")
def extension_package_download(name: str):
    try:
        from extension_system import BASE_DIR
        pkg_path = os.path.join(BASE_DIR, name, "package.zip")
        if os.path.exists(pkg_path):
            from starlette.responses import Response
            with open(pkg_path, "rb") as f:
                data = f.read()
            return Response(content=data, media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})
        return {"ok": False, "error": "Package not found"}
    except Exception as e:
        swallow.note('extension.extension_package_download:L543', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/{name}/page-element/{ei}")
async def extension_page_element_call(name: str, ei: int, request: Request):
    try:
        from extension_system import BASE_DIR
        import importlib.util, sys, asyncio
        mgr = _get_mgr()
        ext = mgr.extensions.get(name)
        if ext is None:
            return {"ok": False, "error": "Extension not found"}
        if not ext.enabled:
            return {"ok": False, "error": "Extension is disabled"}
        ext_dir = os.path.join(BASE_DIR, name)
        main_path = os.path.join(ext_dir, "main.py")
        if not os.path.exists(main_path):
            return {"ok": False, "error": "Extension main.py not found"}
        body = await request.json()
        if ext.sandbox:
            from extension_sandbox import SandboxProcess
            sb = SandboxProcess(name, ext_dir, timeout=30)
            result = await _run_sync(sb.execute_page_element, ei, body)
            return {"ok": True, "result": result}
        sys.path.insert(0, ext_dir)
        mod_name = f"ext_{name}_main"
        if mod_name in sys.modules:
            mod = sys.modules[mod_name]
        else:
            spec = importlib.util.spec_from_file_location(mod_name, main_path)
            if not spec or not spec.loader:
                return {"ok": False, "error": "Cannot load extension module"}
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sys.modules[mod_name] = mod
        handler_name = f"element_{ei}_handler"
        handler = getattr(mod, handler_name, None)
        if not handler:
            return {"ok": False, "error": f"Handler {handler_name} not found"}
        if asyncio.iscoroutinefunction(handler):
            result = await handler(data=body)
        else:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, handler, {"data": body})
        return {"ok": True, "result": result}
    except Exception as e:
        swallow.note('extension.extension_page_element_call:L590', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/{name}/config")
async def extension_get_config(name: str):
    try:
        mgr = _get_mgr()
        config = await _run_sync(mgr.get_extension_config, name)
        return {"config": config}
    except Exception as e:
        swallow.note('extension.extension_get_config:L601', e)
        return {"config": {}}


@router.post("/api/extensions/{name}/config")
async def extension_set_config(name: str, request: Request):
    try:
        body = await request.json()
        mgr = _get_mgr()
        ok = await _run_sync(mgr.set_extension_config, name, body.get("config", {}))
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_set_config:L613', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/{name}/metadata")
def extension_get_metadata(name: str):
    try:
        mgr = _get_mgr()
        if name not in mgr.extensions:
            return {"error": "Extension not found"}
        return {"metadata": mgr.extensions[name].to_dict()}
    except Exception as e:
        swallow.note('extension.extension_get_metadata:L625', e)
        return {"error": str(e)}


@router.post("/api/extensions/{name}/metadata")
async def extension_set_metadata(name: str, request: Request):
    try:
        body = await request.json()
        mgr = _get_mgr()
        ok = await _run_sync(mgr.set_extension_metadata, name, body.get("metadata", {}))
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_set_metadata:L637', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/upload-with-deps")
async def extension_upload_with_deps(request: Request):
    try:
        body = await request.body()
        mgr = _get_mgr()
        result = await _run_sync(mgr.install_from_zip_with_deps, body)
        return result
    except Exception as e:
        swallow.note('extension.extension_upload_with_deps:L649', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/updates")
async def extension_check_updates():
    try:
        mgr = _get_mgr()
        updates = await _run_sync(mgr.check_for_updates)
        return {"updates": updates}
    except Exception as e:
        swallow.note('extension.extension_check_updates:L660', e)
        return {"updates": []}


@router.post("/api/extensions/{name}/update")
async def extension_update(name: str):
    try:
        mgr = _get_mgr()
        result = await _run_sync(mgr.update_extension, name)
        return result
    except Exception as e:
        swallow.note('extension.extension_update:L671', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/{name}/backups")
def extension_list_backups(name: str):
    """Снимки расширения (для отката)."""
    try:
        mgr = _get_mgr()
        return {"backups": mgr.list_backups(name)}
    except Exception as e:
        swallow.note('extension.extension_list_backups:L682', e)
        return {"backups": [], "error": str(e)}


@router.post("/api/extensions/{name}/backup")
async def extension_create_backup(name: str, request: Request):
    """Создать снимок вручную — перед ручной правкой кода."""
    try:
        body = await request.json()
    except Exception:
        swallow.note('extension.extension_create_backup:L692')
        body = {}
    # Метка приходит от интерфейса («перед правкой», «перед обновлением»);
    # без неё все снимки неразличимы в списке.
    label = str(body.get("label") or "").strip()[:60] or "manual"
    try:
        mgr = _get_mgr()
        return await _run_sync(mgr.create_backup, name, label)
    except Exception as e:
        swallow.note('extension.extension_create_backup:L701', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/{name}/rollback")
async def extension_rollback(name: str, request: Request):
    """Откатить расширение к указанному снимку."""
    try:
        body = await request.json()
    except Exception:
        swallow.note('extension.extension_rollback:L711')
        body = {}
    backup_id = body.get("backup_id") or ""
    if not backup_id:
        return {"ok": False, "error": "Не указан backup_id"}
    try:
        mgr = _get_mgr()
        return await _run_sync(mgr.restore_backup, name, backup_id)
    except Exception as e:
        swallow.note('extension.extension_rollback:L720', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/{name}/permissions")
async def extension_get_permissions(name: str):
    try:
        mgr = _get_mgr()
        permissions = await _run_sync(mgr.get_permissions, name)
        return {"permissions": permissions}
    except Exception as e:
        swallow.note('extension.extension_get_permissions:L731', e)
        return {"permissions": []}


@router.post("/api/extensions/{name}/permissions")
async def extension_set_permissions(name: str, request: Request):
    try:
        body = await request.json()
        mgr = _get_mgr()
        await _run_sync(mgr.set_permissions, name, body.get("permissions", []))
        return {"ok": True}
    except Exception as e:
        swallow.note('extension.extension_set_permissions:L743', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/{name}/files")
async def extension_list_files(name: str):
    try:
        mgr = _get_mgr()
        files = await _run_sync(lambda: mgr.extensions[name].files if name in mgr.extensions else [])
        return {"files": files}
    except Exception as e:
        swallow.note('extension.extension_list_files:L754', e)
        return {"files": []}


@router.get("/api/extensions/{name}/files/read")
async def extension_read_file(name: str, path: str = ""):
    try:
        mgr = _get_mgr()
        content = await _run_sync(mgr.read_extension_file, name, path)
        if content is not None:
            return {"content": content}
        return {"error": "File not found"}
    except Exception as e:
        swallow.note('extension.extension_read_file:L767', e)
        return {"error": str(e)}


@router.post("/api/extensions/{name}/files/save")
async def extension_write_file(name: str, request: Request):
    try:
        body = await request.json()
        path = body.get("path", "")
        content = body.get("content", "")
        mgr = _get_mgr()
        ok = await _run_sync(mgr.write_extension_file, name, path, content)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_write_file:L781', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/{name}/files/delete")
async def extension_delete_file(name: str, request: Request):
    try:
        body = await request.json()
        path = body.get("path", "")
        mgr = _get_mgr()
        ok = await _run_sync(mgr.delete_extension_file, name, path)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_delete_file:L794', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/{name}/files/create-folder")
async def extension_create_folder(name: str, request: Request):
    try:
        body = await request.json()
        path = body.get("path", "")
        mgr = _get_mgr()
        ok = await _run_sync(mgr.create_extension_folder, name, path)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_create_folder:L807', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/{name}/logs")
async def extension_logs(name: str, max_lines: int = 100):
    try:
        mgr = _get_mgr()
        logs = await _run_sync(mgr.get_extension_logs, name, max_lines)
        return {"logs": logs}
    except Exception as e:
        swallow.note('extension.extension_logs:L818', e)
        return {"logs": []}


# ---- Marketplace API ----


def _marketplace_hidden(request: Request) -> bool:
    """Серверный каталог расширений закрыт рубильником apps_marketplace.

    Пока ~/.infinity/extensions/registry.json нет, менеджер отдаёт зашитую
    заглушку из пяти позиций с выдуманными счётчиками скачиваний, рейтингами и
    ссылками на несуществующие репозитории. Это не данные, а витрина, поэтому
    при выключенном флаге не отдаём её ни странице, ни builder-виджету.
    """
    try:
        import module_flags
        import profiles_module as PM
        return not module_flags.enabled_for("apps_marketplace",
                                            PM.request_profile(request) or "")
    except Exception as e:
        swallow.note('extension._marketplace_hidden', e)
        return True


@router.get("/api/extensions/marketplace/list")
async def marketplace_list(request: Request, query: str = "", category: str = ""):
    if _marketplace_hidden(request):
        return {"extensions": [], "hidden": True}
    try:
        mgr = _get_mgr()
        extensions = await _run_sync(mgr.get_marketplace_list, query, category)
        return {"extensions": extensions}
    except Exception as e:
        logger.error(f"Marketplace list error: {e}")
        return {"extensions": []}


@router.get("/api/extensions/marketplace/categories")
async def marketplace_categories(request: Request):
    if _marketplace_hidden(request):
        return {"categories": [], "hidden": True}
    try:
        mgr = _get_mgr()
        categories = await _run_sync(mgr.get_marketplace_categories)
        return {"categories": categories}
    except Exception as e:
        swallow.note('extension.marketplace_categories:L843', e)
        return {"categories": []}


@router.post("/api/extensions/marketplace/install")
async def marketplace_install(request: Request):
    try:
        mgr = _get_mgr()
        try:
            body = await request.json()
        except Exception:
            swallow.note('extension.marketplace_install:L854')
            body = {}
        url = (body.get("url", "") or body.get("download_url", "")
               or body.get("source_url", "") or request.query_params.get("url", ""))
        if url:
            # marketplace_install сам выбирает download_url → source_url → url;
            # передаём тело целиком, иначе поля из карточки маркетплейса терялись.
            payload = dict(body) if isinstance(body, dict) else {}
            payload["url"] = url
            result = await _run_sync(mgr.marketplace_install, payload)
            return result
        return {"ok": False, "error": "URL is required"}
    except Exception as e:
        swallow.note('extension.marketplace_install:L867', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/marketplace/publish")
async def marketplace_publish(request: Request):
    try:
        body = await request.json()
        name = body.get("name", "")
        meta = body.get("meta", {})
        mgr = _get_mgr()
        result = await _run_sync(mgr.marketplace_publish, name, meta)
        return result
    except Exception as e:
        swallow.note('extension.marketplace_publish:L881', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/marketplace/remove")
async def marketplace_remove(request: Request):
    try:
        body = await request.json()
        name = body.get("name", "")
        mgr = _get_mgr()
        result = await _run_sync(mgr.marketplace_remove, name)
        return result
    except Exception as e:
        swallow.note('extension.marketplace_remove:L894', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/marketplace/page")
def marketplace_page():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/apps#extensions")


@router.get("/api/extensions/permissions/list-all")
def extension_all_permissions():
    try:
        from extension_sandbox import ALL_PERMISSIONS
        return {"permissions": [{"id": k, "description": v} for k, v in ALL_PERMISSIONS.items()]}
    except Exception as e:
        swallow.note('extension.extension_all_permissions:L910', e)
        return {"permissions": []}


@router.post("/api/extensions/{name}/terminal/input")
async def extension_terminal_input(name: str, request: Request):
    """Команда в каталоге расширения. Только администратор.

    Ветка исполняет произвольную строку через shell, поэтому роль проверяется
    до чтения тела: без гейта любой клиент в локальной сети (при включённом
    LAN-доступе) получал RCE. Имя расширения — только существующий каталог:
    `..` увели бы рабочий каталог за пределы extensions/.
    """
    import subprocess, asyncio
    try:
        import admin_module as _admin
        if not _admin.admin_request_ok(request):
            return JSONResponse({"error": "Терминал доступен только администратору"},
                                status_code=403)
    except Exception:
        return JSONResponse({"error": "Терминал недоступен"}, status_code=403)
    try:
        from extension_system import BASE_DIR, _valid_ext_name
        if not _valid_ext_name(name):
            return JSONResponse({"error": "Некорректное имя расширения"},
                                status_code=404)
        body = await request.json()
        text = str(body.get("text", "")).strip()
        ext_dir = os.path.join(BASE_DIR, name)
        if not os.path.isdir(ext_dir):
            return JSONResponse({"error": "Extension not found"}, status_code=404)
        if not text:
            return {"output": ""}
        if len(text) > 4096:
            return JSONResponse({"error": "Команда слишком длинная"}, status_code=400)
        loop = asyncio.get_running_loop()
        def _run():
            result = subprocess.run(
                text,
                shell=True,
                capture_output=True,
                text=True,
                cwd=ext_dir,
                timeout=30
            )
            output = result.stdout or ""
            if result.stderr:
                output += "\n" + result.stderr if output else result.stderr
            if result.returncode != 0 and not output:
                output = f"Exit code: {result.returncode}"
            return {"output": output.strip() or "(команда выполнена)"}
        return await loop.run_in_executor(None, _run)
    except subprocess.TimeoutExpired:
        swallow.note('extension.extension_terminal_input:L944')
        return {"error": "Command timed out (30s)"}
    except Exception as e:
        swallow.note('extension.extension_terminal_input:L947', e)
        return {"error": str(e)}


# ---- Storage API ----

_STORAGE_LOCK = asyncio.Lock()


def _get_storage_path(name: str) -> str:
    from extension_system import BASE_DIR
    return os.path.join(BASE_DIR, name, "storage.json")


def _storage_read(name: str) -> dict:
    sp = _get_storage_path(name)
    if os.path.exists(sp):
        with open(sp, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _storage_write(name: str, data: dict):
    sp = _get_storage_path(name)
    os.makedirs(os.path.dirname(sp), exist_ok=True)
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@router.get("/api/extensions/{name}/storage")
def extension_storage_list(name: str):
    try:
        mgr = _get_mgr()
        if name not in mgr.extensions:
            return {"error": "Extension not found"}
        return _storage_read(name)
    except Exception as e:
        swallow.note('extension.extension_storage_list:L984', e)
        return {"error": str(e)}


@router.get("/api/extensions/{name}/storage/{key}")
def extension_storage_get(name: str, key: str):
    try:
        mgr = _get_mgr()
        if name not in mgr.extensions:
            return {"error": "Extension not found"}
        data = _storage_read(name)
        if key in data:
            return {"value": data[key]}
        return {"error": "Key not found"}
    except Exception as e:
        swallow.note('extension.extension_storage_get:L999', e)
        return {"error": str(e)}


@router.post("/api/extensions/{name}/storage/{key}")
async def extension_storage_set(name: str, key: str, request: Request):
    try:
        mgr = _get_mgr()
        if name not in mgr.extensions:
            return {"error": "Extension not found"}
        body = await request.json()
        async with _STORAGE_LOCK:
            data = await _run_sync(_storage_read, name)
            data[key] = body.get("value")
            await _run_sync(_storage_write, name, data)
        return {"ok": True}
    except Exception as e:
        swallow.note('extension.extension_storage_set:L1016', e)
        return {"error": str(e)}


@router.delete("/api/extensions/{name}/storage/{key}")
async def extension_storage_delete(name: str, key: str):
    try:
        mgr = _get_mgr()
        if name not in mgr.extensions:
            return {"error": "Extension not found"}
        async with _STORAGE_LOCK:
            data = await _run_sync(_storage_read, name)
            data.pop(key, None)
            await _run_sync(_storage_write, name, data)
        return {"ok": True}
    except Exception as e:
        swallow.note('extension.extension_storage_delete:L1032', e)
        return {"error": str(e)}


# ---- Cross-extension Messaging ----


@router.post("/api/extensions/message")
async def extension_send_message(request: Request):
    try:
        body = await request.json()
        target = body.get("target", "")
        sender = body.get("sender", "")
        message_type = body.get("type", "custom")
        payload = body.get("payload", {})
        mgr = _get_mgr()
        if target not in mgr.extensions:
            return {"error": f"Target extension '{target}' not found"}
        ext = mgr.extensions[target]
        if not ext.enabled:
            return {"error": f"Target extension '{target}' is disabled"}
        if not ext.module:
            return {"error": f"Target extension '{target}' has no loaded module"}
        handler = getattr(ext.module, "on_message", None)
        if not handler:
            return {"ok": True, "delivered": False, "reason": "No on_message handler"}
        import inspect
        if inspect.iscoroutinefunction(handler):
            result = await handler(sender=sender, type=message_type, payload=payload)
        else:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, handler, sender, message_type, payload)
        return {"ok": True, "delivered": True, "result": result}
    except Exception as e:
        swallow.note('extension.extension_send_message:L1066', e)
        return {"error": str(e)}


# ---- Extension Debug Info ----


@router.get("/api/extensions/debug/info")
def extension_debug_info():
    try:
        mgr = _get_mgr()
        info = []
        for name, ext in mgr.extensions.items():
            info.append({
                "name": name,
                "version": ext.version,
                "enabled": ext.enabled,
                "target_module": ext.target_module,
                "hooks": ext.hooks,
                "has_frontend": bool(ext.frontend_js),
                "frontend_size": len(ext.frontend_js or ""),
                "has_python": bool(getattr(ext, "module", None)),
                "permissions": ext.permissions,
                "files_count": len(ext.files),
                "source": getattr(ext, "source", "unknown"),
                "install_time": getattr(ext, "install_time", 0),
                "error": getattr(ext, "load_error", None),
            })
        return {"extensions": info}
    except Exception as e:
        swallow.note('extension.extension_debug_info:L1096', e)
        return {"error": str(e)}


# ---- Extensible Hooks System ----


@router.post("/api/extensions/hooks/register")
async def extension_register_hook(request: Request):
    try:
        body = await request.json()
        hook_name = body.get("hook", "").strip()
        module_name = body.get("module", "all")
        if not hook_name:
            return {"ok": False, "error": "hook name required"}
        mgr = _get_mgr()
        await _run_sync(mgr.register_custom_hook, hook_name, module_name)
        return {"ok": True, "hook": hook_name, "module": module_name}
    except Exception as e:
        swallow.note('extension.extension_register_hook:L1115', e)
        return {"error": str(e)}


@router.get("/api/extensions/hooks/list")
async def extension_list_hooks():
    try:
        mgr = _get_mgr()
        hooks = await _run_sync(mgr.get_all_hooks)
        return {"hooks": hooks}
    except Exception as e:
        swallow.note('extension.extension_list_hooks:L1126', e)
        return {"hooks": []}


# ---- Background Worker ----


@router.post("/api/extensions/{name}/background/start")
async def extension_background_start(name: str):
    try:
        mgr = _get_mgr()
        ok = await _run_sync(mgr.start_background_worker, name)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_background_start:L1140', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/{name}/background/stop")
async def extension_background_stop(name: str):
    try:
        mgr = _get_mgr()
        await _run_sync(mgr.stop_background_worker, name)
        return {"ok": True}
    except Exception as e:
        swallow.note('extension.extension_background_stop:L1151', e)
        return {"ok": False, "error": str(e)}


# ---- Permission Management ----


@router.post("/api/extensions/{name}/permissions/grant")
async def extension_grant_permission(name: str, request: Request):
    try:
        body = await request.json()
        perm = body.get("permission", "")
        mgr = _get_mgr()
        ok = await _run_sync(mgr.grant_permission, name, perm)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_grant_permission:L1167', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/{name}/permissions/revoke")
async def extension_revoke_permission(name: str, request: Request):
    try:
        body = await request.json()
        perm = body.get("permission", "")
        mgr = _get_mgr()
        ok = await _run_sync(mgr.revoke_permission, name, perm)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.extension_revoke_permission:L1180', e)
        return {"ok": False, "error": str(e)}


@router.get("/api/extensions/{name}/permissions/check")
async def extension_check_permissions(name: str, required: str = ""):
    try:
        mgr = _get_mgr()
        req_list = [p.strip() for p in required.split(",") if p.strip()]
        if not req_list:
            ext = mgr.extensions.get(name)
            return {"ok": True, "permissions": ext.permissions if ext else []}
        ok, missing = await _run_sync(mgr.check_permissions, name, req_list)
        return {"ok": ok, "missing": missing}
    except Exception as e:
        swallow.note('extension.extension_check_permissions:L1195', e)
        return {"ok": False, "error": str(e)}


# ---- Hot Reload WebSocket ----


from fastapi import WebSocket, WebSocketDisconnect

_HOT_RELOAD_CLIENTS: dict = {}   # profile_id -> [ws]


def _current_profile_id() -> str:
    """Идентификатор профиля, чьи расширения сейчас загружены."""
    import extension_system as _ext
    base = os.path.realpath(_ext.BASE_DIR)
    # .../data/profiles/<profile_id>/data/extensions -> <profile_id>
    parts = base.replace("/", "\\").split("\\")
    if "profiles" in parts:
        i = parts.index("profiles")
        if i + 1 < len(parts):
            return parts[i + 1]
    return base


@router.websocket("/api/extensions/hot-reload")
async def extension_hot_reload(websocket: WebSocket):
    """События hot-reload. Профиль — из cookie, а не из query.

    Раньше профиль брался из ?profile=<id>, т.е. любой мог подписаться на
    события чужого профиля (и получать имена его расширений). Клиент
    приложения query не передаёт, поэтому источником остаётся cookie;
    чужой Origin (CSWSH) отклоняем до accept.
    """
    from local_request import origin_allowed
    if not origin_allowed(websocket.headers.get("origin", ""),
                          websocket.headers.get("host", "")):
        await websocket.accept()
        await websocket.close(code=4403)
        return
    try:
        import profiles_module as PM
        profile_id = PM.cookie_profile(websocket) or PM.resolve_profile_id("")
    except Exception:
        swallow.note('extension.extension_hot_reload:L1239')
        profile_id = ""
    if not profile_id:
        await websocket.accept()
        await websocket.close(code=4401)
        return
    await websocket.accept()
    _HOT_RELOAD_CLIENTS.setdefault(profile_id, []).append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                # Клиент парсит каждое сообщение как JSON — голый текст «pong»
                # ловил SyntaxError каждые 25 секунд и засорял консоль.
                await websocket.send_json({"action": "pong"})
    except WebSocketDisconnect:
        swallow.note('extension.extension_hot_reload:L1255')
        pass
    except Exception:
        swallow.note('extension.extension_hot_reload:L1258')
        pass
    finally:
        ws_list = _HOT_RELOAD_CLIENTS.get(profile_id, [])
        if websocket in ws_list:
            ws_list.remove(websocket)
        if not ws_list:
            _HOT_RELOAD_CLIENTS.pop(profile_id, None)


async def notify_hot_reload(ext_name: str = "", profile_id: str = ""):
    """Уведомить клиентов hot-reload ТОЛЬКО того профиля, где живёт расширение."""
    profile_id = profile_id or _current_profile_id()
    msg = {"action": "reload", "extension": ext_name, "profile": profile_id}
    targets = list(_HOT_RELOAD_CLIENTS.get(profile_id, []))
    dead = []
    for ws in targets:
        try:
            await ws.send_json(msg)
        except Exception:
            swallow.note('extension.notify_hot_reload:L1278')
            dead.append(ws)
    for ws in dead:
        ws_list = _HOT_RELOAD_CLIENTS.get(profile_id, [])
        if ws in ws_list:
            ws_list.remove(ws)


# ---- Extension Debugger Page ----


EXT_DEBUGGER_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Extension Debugger — Infinity</title>
<style>
* { margin:0; padding:0; box-sizing:border-box }
body { font-family:'Inter',sans-serif; background:#0f1419; color:#e1e8f0; padding:24px }
h1 { font-size:22px; margin-bottom:20px; display:flex; align-items:center; gap:10px }
table { width:100%; border-collapse:collapse; font-size:13px }
th, td { padding:8px 12px; text-align:left; border-bottom:1px solid #2a3548 }
th { background:#151b23; color:#8899aa; font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:0.5px; position:sticky; top:0 }
tr:hover td { background:#1c242f }
.badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600 }
.badge-ok { background:#065f4622; color:#22c55e; border:1px solid #22c55e33 }
.badge-err { background:#7f1d1d22; color:#ef4444; border:1px solid #ef444433 }
.badge-off { background:#1c242f; color:#8899aa; border:1px solid #2a3548 }
.mono { font-family:monospace; font-size:12px }
.actions button { background:#1c242f; border:1px solid #2a3548; color:#e1e8f0; padding:4px 10px; border-radius:4px; cursor:pointer; font-size:11px; margin-right:4px }
.actions button:hover { background:#6366f1; border-color:#6366f1 }
#stats { display:flex; gap:16px; margin-bottom:20px; flex-wrap:wrap }
.stat-card { background:#151b23; border:1px solid #2a3548; border-radius:8px; padding:12px 16px; min-width:120px }
.stat-card .num { font-size:28px; font-weight:700; color:#6366f1 }
.stat-card .label { font-size:11px; color:#8899aa; margin-top:4px }
.refresh-btn { background:#6366f1; color:#fff; border:none; padding:6px 14px; border-radius:6px; cursor:pointer; font-size:12px; margin-left:auto }
.refresh-btn:hover { opacity:0.9 }
.toolbar { display:flex; align-items:center; margin-bottom:16px }
</style>
</head>
<body>
<div class="toolbar">
  <h1><span style="background:#6366f1;border-radius:6px;padding:4px 8px;font-size:14px">EXT</span> Extension Debugger</h1>
  <button class="refresh-btn" onclick="loadData()"><i class="fas fa-sync-alt"></i> Refresh</button>
</div>
<div id="stats"></div>
<div style="overflow-x:auto"><table id="ext-table">
<thead><tr>
<th>Name</th><th>Version</th><th>Status</th><th>Module</th><th>Hooks</th><th>Frontend</th><th>Python</th><th>Permissions</th><th>Files</th><th>Actions</th>
</tr></thead>
<tbody id="ext-tbody"></tbody>
</table></div>
<script>
function loadData(){
  fetch('/api/extensions/debug/info').then(function(r){return r.json()}).then(function(d){
    var exts = d.extensions || [];
    var tbody = document.getElementById('ext-tbody');
    tbody.innerHTML = '';
    var enabled = 0, total = exts.length, hasPy = 0;
    exts.forEach(function(e){
      if(e.enabled) enabled++;
      if(e.has_python) hasPy++;
      var tr = document.createElement('tr');
      var statusCls = e.enabled ? (e.error ? 'badge-err' : 'badge-ok') : 'badge-off';
      var statusTxt = e.enabled ? (e.error ? 'ERROR' : 'ON') : 'OFF';
      if(e.error) statusTxt = 'ERR';
      tr.innerHTML = '<td class="mono"><b>' + e.name + '</b></td>' +
        '<td>' + e.version + '</td>' +
        '<td><span class="badge ' + statusCls + '">' + statusTxt + '</span>' + (e.error ? '<br><small style="color:#ef4444;font-size:10px">' + e.error + '</small>' : '') + '</td>' +
        '<td>' + e.target_module + '</td>' +
        '<td>' + (e.hooks||[]).join(', ') + '</td>' +
        '<td>' + (e.has_frontend ? (e.frontend_size + 'b') : '-') + '</td>' +
        '<td>' + (e.has_python ? '<span class="badge badge-ok">OK</span>' : '-') + '</td>' +
        '<td style="font-size:11px">' + (e.permissions||[]).join(', ') + '</td>' +
        '<td>' + e.files_count + '</td>' +
        '<td class="actions">' +
        (e.enabled ? '<button onclick="toggle(\''+e.name+'\',false)">Disable</button>' : '<button onclick="toggle(\''+e.name+'\',true)">Enable</button>') +
        '<button onclick="reload(\''+e.name+'\')">Reload</button>' +
        '</td>';
      tbody.appendChild(tr);
    });
    document.getElementById('stats').innerHTML =
      '<div class="stat-card"><div class="num">'+total+'</div><div class="label">Total</div></div>' +
      '<div class="stat-card"><div class="num">'+enabled+'</div><div class="label">Enabled</div></div>' +
      '<div class="stat-card"><div class="num">'+hasPy+'</div><div class="label">With Python</div></div>' +
      '<div class="stat-card"><div class="num">'+exts.filter(function(e){return e.has_frontend}).length+'</div><div class="label">With Frontend</div></div>';
  }).catch(function(e){ document.getElementById('ext-tbody').innerHTML = '<tr><td colspan="10" style="color:#ef4444">Error: '+e.message+'</td></tr>'; });
}
function toggle(name, enable){
  fetch('/api/extensions/'+encodeURIComponent(name)+'/'+(enable?'enable':'disable'),{method:'POST'}).then(function(r){return r.json()}).then(function(){loadData();});
}
function reload(name){
  fetch('/api/extensions/'+encodeURIComponent(name)+'/reload',{method:'POST'}).then(function(r){return r.json()}).then(function(){loadData();});
}
loadData();
</script>
</body>
</html>"""


@router.get("/api/extensions/debug/page")
def extension_debug_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(EXT_DEBUGGER_PAGE)


# ---- I18n API ----


@router.get("/api/extensions/{name}/i18n")
def extension_i18n_get(name: str, lang: str = "en"):
    try:
        mgr = _get_mgr()
        if name not in mgr.extensions:
            return {"error": "Extension not found"}
        from extension_system import BASE_DIR
        i18n_path = os.path.join(BASE_DIR, name, "_locales", f"{lang}.json")
        if os.path.exists(i18n_path):
            with open(i18n_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    except Exception as e:
        swallow.note('extension.extension_i18n_get:L1401', e)
        return {"error": str(e)}


@router.post("/api/extensions/{name}/i18n")
async def extension_i18n_set(name: str, request: Request):
    try:
        body = await request.json()
        lang = body.get("lang", "en")
        messages = body.get("messages", {})
        from extension_system import BASE_DIR
        i18n_dir = os.path.join(BASE_DIR, name, "_locales")
        os.makedirs(i18n_dir, exist_ok=True)
        i18n_path = os.path.join(i18n_dir, f"{lang}.json")
        with open(i18n_path, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception as e:
        swallow.note('extension.extension_i18n_set:L1419', e)
        return {"error": str(e)}


# ---- Auto-update checker ----


@router.post("/api/extensions/check-all-updates")
async def extension_check_all_updates():
    try:
        mgr = _get_mgr()
        updates = await _run_sync(mgr.check_for_updates)
        return {"updates": updates}
    except Exception as e:
        swallow.note('extension.extension_check_all_updates:L1433', e)
        return {"error": str(e)}


@router.post("/api/extensions/{name}/auto-update")
async def extension_toggle_auto_update(name: str, request: Request):
    try:
        body = await request.json()
        enabled = body.get("enabled", False)
        mgr = _get_mgr()
        if name not in mgr.extensions:
            return {"error": "Extension not found"}
        ext = mgr.extensions[name]
        ext.auto_update = enabled
        mgr._save_state()
        return {"ok": True}
    except Exception as e:
        swallow.note('extension.extension_toggle_auto_update:L1450', e)
        return {"error": str(e)}


# ---- Marketplace UI Page ----


EXT_MARKETPLACE_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Marketplace — Infinity Extensions</title>
<style>
* { margin:0; padding:0; box-sizing:border-box }
body { font-family:'Inter',sans-serif; background:#0f1419; color:#e1e8f0; padding:24px }
h1 { font-size:22px; margin-bottom:20px; display:flex; align-items:center; gap:10px }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:16px }
.card { background:#151b23; border:1px solid #2a3548; border-radius:12px; padding:16px; transition:all 0.2s }
.card:hover { border-color:#6366f1; transform:translateY(-2px); box-shadow:0 4px 20px rgba(99,102,241,0.15) }
.card h3 { font-size:15px; margin-bottom:4px }
.card .desc { font-size:12px; color:#8899aa; margin-bottom:8px; line-height:1.4 }
.card .meta { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px }
.card .meta span { font-size:11px; padding:2px 8px; border-radius:4px; background:#1c242f; color:#8899aa }
.card .meta .ver { color:#6366f1; background:#6366f122 }
.card .actions { display:flex; gap:8px }
.card .actions button { flex:1; padding:6px 12px; border-radius:6px; border:none; cursor:pointer; font-size:12px; font-weight:600 }
.btn-install { background:#6366f1; color:#fff }
.btn-install:hover { opacity:0.9 }
.btn-installed { background:#1c242f; color:#8899aa; cursor:default }
.search-bar { display:flex; gap:8px; margin-bottom:20px }
.search-bar input { flex:1; padding:8px 14px; border-radius:8px; border:1px solid #2a3548; background:#151b23; color:#e1e8f0; font-size:13px }
.search-bar input:focus { outline:none; border-color:#6366f1 }
.search-bar select { padding:8px 14px; border-radius:8px; border:1px solid #2a3548; background:#151b23; color:#e1e8f0; font-size:13px }
#loading { text-align:center; padding:60px; color:#8899aa }
#loading .spinner { display:inline-block; width:32px; height:32px; border:3px solid #2a3548; border-top-color:#6366f1; border-radius:50%; animation:spin 0.8s linear infinite; margin-bottom:12px }
@keyframes spin { to { transform:rotate(360deg) } }
</style>
</head>
<body>
<h1><span style="background:#6366f1;border-radius:6px;padding:4px 8px;font-size:14px">MKT</span> Extension Marketplace</h1>
<div class="search-bar">
  <input type="text" id="searchInput" placeholder="Search extensions..." oninput="loadExtensions()">
  <select id="catSelect" onchange="loadExtensions()">
    <option value="">All categories</option>
  </select>
</div>
<div id="loading"><div class="spinner"></div><br>Loading...</div>
<div class="grid" id="extGrid" style="display:none"></div>
<script>
var installed = {};
function loadExtensions(){
  var q = document.getElementById('searchInput').value;
  var cat = document.getElementById('catSelect').value;
  fetch('/api/extensions/marketplace/list?query='+encodeURIComponent(q)+'&category='+encodeURIComponent(cat))
    .then(function(r){return r.json()})
    .then(function(d){
      var exts = d.extensions || [];
      document.getElementById('loading').style.display = 'none';
      var grid = document.getElementById('extGrid');
      grid.style.display = 'grid';
      grid.innerHTML = '';
      if(!exts.length){
        grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:#8899aa">No extensions found</div>';
        return;
      }
      exts.forEach(function(e){
        var isInstalled = installed[e.name];
        var card = document.createElement('div');
        card.className = 'card';
        card.innerHTML =
          '<h3><i class="fas ' + (e.icon||'fa-puzzle-piece') + '" style="color:#6366f1;margin-right:6px"></i>' + (e.title||e.name) + '</h3>' +
          '<div class="desc">' + (e.description||'') + '</div>' +
          '<div class="meta">' +
            '<span class="ver">v' + (e.version||'1.0.0') + '</span>' +
            '<span>' + (e.author||'Unknown') + '</span>' +
            (e.category ? '<span>' + e.category + '</span>' : '') +
          '</div>' +
          '<div class="actions">' +
            (isInstalled
              ? '<button class="btn-installed"><i class="fas fa-check"></i> Installed</button>'
              : '<button class="btn-install" onclick="installExt(\''+e.name+'\',\''+e.download_url+'\')"><i class="fas fa-download"></i> Install</button>') +
          '</div>';
        grid.appendChild(card);
      });
    });
}
function installExt(name, url){
  fetch('/api/extensions/marketplace/install', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url: url || name})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.ok){ installed[name] = true; loadExtensions(); }
      else alert('Install failed: ' + (d.error||'unknown'));
    });
}
fetch('/api/extensions/marketplace/categories').then(function(r){return r.json()}).then(function(d){
  var sel = document.getElementById('catSelect');
  (d.categories||[]).forEach(function(c){
    var opt = document.createElement('option');
    opt.value = c; opt.textContent = c;
    sel.appendChild(opt);
  });
});
fetch('/api/extensions').then(function(r){return r.json()}).then(function(d){
  (d.extensions||[]).forEach(function(e){ installed[e.name] = true; });
  loadExtensions();
});
</script>
</body>
</html>"""


@router.get("/api/extensions/marketplace/ui")
def marketplace_ui():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(EXT_MARKETPLACE_PAGE)


@router.get("/api/extensions/{name}/builder-config")
def extension_builder_config(name: str):
    """Конфиг расширения в формате билдера (для режима редактирования)."""
    try:
        from extension_system import BASE_DIR
        mgr = _get_mgr()
        if name not in mgr.extensions:
            return {"error": "Extension not found"}
        cfg_path = os.path.join(BASE_DIR, name, "extension.json")
        if not os.path.exists(cfg_path):
            return {"error": "extension.json not found"}
        with open(cfg_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        schema_dict = config.get("config_schema", {}) or {}
        schema_arr = []
        if isinstance(schema_dict, dict):
            for fid, fdata in schema_dict.items():
                schema_arr.append({
                    "id": fid,
                    "label": fdata.get("label", fid),
                    "type": fdata.get("type", "string"),
                    "default": fdata.get("default", ""),
                    "description": fdata.get("description", "")
                })

        fc = config.get("frontend_config", {}) or {}
        frontend_config = {
            "button_text": fc.get("button_text", ""),
            "button_icon": fc.get("button_icon", "fa-puzzle-piece"),
            "show_notification": fc.get("show_notification", False),
            "add_styles": fc.get("add_styles", False),
            "inject_html": fc.get("inject_html", ""),
            "show_badge": fc.get("show_badge", True),
        }

        result = {
            "name": config.get("name", ""),
            "version": config.get("version", "1.0.0"),
            "description": config.get("description", ""),
            "author": config.get("author", ""),
            "icon": config.get("icon", "fa-puzzle-piece"),
            "target_module": config.get("target_module", "all"),
            "target_modules": config.get("target_modules", []),
            "new_module": None,
            "hooks": config.get("hooks", []),
            "hook_actions": config.get("hook_actions", {}),
            "permissions": config.get("permissions", ["storage"]),
            "config_schema": schema_arr,
            "create_frontend": bool(config.get("frontend_entry")),
            "frontend_config": frontend_config,
            "page_elements": config.get("page_elements", []),
            "dependencies": config.get("dependencies", []),
            "sandbox": bool(config.get("sandbox", False)),
            "auto_update": bool(config.get("auto_update", False)),
            "update_url": config.get("update_url", ""),
            "module_pages": config.get("module_pages", {}),
            "edit_mode": True,
            "frontend_error": (mgr.extensions[name].frontend_error if name in mgr.extensions else None),
        }
        return {"config": result}
    except Exception as e:
        swallow.note('extension.extension_builder_config:L1629', e)
        return {"error": str(e)}


# ---- Recovery / safe-mode API ----

@router.get("/api/extensions/recovery/status")
async def recovery_status():
    try:
        mgr = _get_mgr()
        status = await _run_sync(mgr.get_recovery_status)
        return status
    except Exception as e:
        swallow.note('extension.recovery_status:L1642', e)
        return {"error": str(e)}


@router.post("/api/extensions/recovery/block")
async def recovery_block(request: Request):
    try:
        body = await request.json()
        name = body.get("name", "")
        mgr = _get_mgr()
        ok = await _run_sync(mgr.block_extension, name)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.recovery_block:L1655', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/recovery/unblock")
async def recovery_unblock(request: Request):
    try:
        body = await request.json()
        name = body.get("name", "")
        mgr = _get_mgr()
        ok = await _run_sync(mgr.unblock_extension, name)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.recovery_unblock:L1668', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/recovery/try-again")
async def recovery_try_again(request: Request):
    try:
        body = await request.json()
        name = body.get("name", "")
        mgr = _get_mgr()
        ok = await _run_sync(mgr.clear_suspect, name)
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.recovery_try_again:L1681', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/recovery/safe-mode")
async def recovery_safe_mode(request: Request):
    try:
        body = await request.json()
        mgr = _get_mgr()
        ok = await _run_sync(mgr.set_safe_mode, bool(body.get("on", False)))
        return {"ok": ok}
    except Exception as e:
        swallow.note('extension.recovery_safe_mode:L1693', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/recovery/backup")
async def recovery_backup():
    try:
        import recovery_guard
        backup_id = await _run_sync(recovery_guard.make_backup, True)
        return {"ok": bool(backup_id), "backup_id": backup_id}
    except Exception as e:
        swallow.note('extension.recovery_backup:L1704', e)
        return {"ok": False, "error": str(e)}


@router.post("/api/extensions/recovery/restore")
async def recovery_restore(request: Request):
    try:
        body = await request.json()
        backup_id = body.get("id", "")
        import recovery_guard
        result = await _run_sync(recovery_guard.restore_backup, backup_id)
        return result
    except Exception as e:
        swallow.note('extension.recovery_restore:L1717', e)
        return {"ok": False, "error": str(e)}
