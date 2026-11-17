"""Admin Panel API routes — панель администратора (/admin)."""

import os
import json
import asyncio
import logging
import shutil
import time

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse

import server
from modules import llm_text
import worker_pool
from cache_manager import CacheManager
from modules import assets_index
from modules import profile_state
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()

ADMIN_COOKIE = "infinity_admin"

# Кэш снимка /api/admin/load (см. обработчик): админка опрашивает раз в 2 с.
_LOAD_CACHE = {"t": 0.0, "data": None}

# Снимки /api/admin/models и /api/admin/modules собираются из ВНЕШНИХ проб:
# замер 02.11 19:5x на отдельном процессе — health LLM-бэкенда 3.6 с,
# lm_studio_get_models 1.1-1.4 с (на КАЖДЫЙ вызов), hermes.status 2.3 с,
# docker.get_status 0.4 с. Панель не должна платить за них в запросе: свежий
# снимок строится в фоне, устаревший отдаётся сразу — тот же приём, что у
# /api/models (routes/legacy_system.py).
_ADMIN_MODELS_CACHE = CacheManager(default_ttl=30.0, max_size=4)
_ADMIN_MODULES_CACHE = CacheManager(default_ttl=5.0, max_size=4)


def _modules_sig():
    """Подпись флагов модулей: включили/выключили — снимок пересоберётся сразу."""
    try:
        import module_flags
        return tuple(sorted((m, k, bool(v))
                            for m, d in module_flags.all_flags().items()
                            for k, v in d.items()))
    except Exception:
        swallow.note('admin._modules_sig:L46')
        return ()

# Кэш размеров/счётчиков профилей для /api/admin/stats: обход дерева профиля
# (медиа, кэши) стоит секунды, а дашборд опрашивается часто.
_STATS_SIZE_TTL = 30.0
_STATS_SIZE_CACHE = {}   # profile_id -> (ts, dict)


def _run_sync(fn, *args, **kw):
    return worker_pool.run_in_pool(worker_pool.HIGH_POOL, fn, *args, **kw)


def _admin_cookie(request: Request):
    return request.cookies.get(ADMIN_COOKIE, "") or ""


def _admin_ok(request: Request):
    """Доступ к панели: действующий токен сессии И профиль с ролью admin.

    Проверка собрана в admin_module.admin_request_ok — её же используют
    routes/network.py и routes/coordination.py, чтобы «админ» значил одно и
    то же во всех админ-роутах.
    """
    import admin_module
    return admin_module.admin_request_ok(request)


def _caller_profile_id(request: Request):
    import admin_module
    return admin_module.request_profile_id(request)


def _auth_error(msg="Доступ запрещён"):
    return JSONResponse({"error": msg}, status_code=403)


def _set_admin_cookie(response, token):
    response.set_cookie(ADMIN_COOKIE, token, path="/", max_age=86400,
                        httponly=True, samesite="strict")


def _profile_extensions_dir(profile_id):
    """Каталог расширений профиля из его конфига (с фоллбэком на data/extensions)."""
    import profiles_module
    cfg = profiles_module.get_profile_config(profile_id) or {}
    ext = cfg.get("extensions_dir") or ""
    if ext and os.path.isdir(ext):
        return ext
    return os.path.join(profiles_module._profile_data_path(profile_id), "extensions")


def _profile_plugins_dir(profile_id):
    """Каталог плагинов профиля из его конфига (с фоллбэком на data/plugins)."""
    import profiles_module
    cfg = profiles_module.get_profile_config(profile_id) or {}
    plugins = cfg.get("plugins_dir") or ""
    if plugins and os.path.isdir(plugins):
        return plugins
    return os.path.join(profiles_module._profile_data_path(profile_id), "plugins")


def _dir_size(path):
    """Размер каталога в байтах (с защитой от ошибок)."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    swallow.note('admin._dir_size:L118')
                    pass
    except OSError:
        swallow.note('admin._dir_size:L121')
        pass
    return total


def _count_files(path, extensions=None):
    """Число файлов в каталоге (опционально по расширениям)."""
    count = 0
    try:
        for _root, _dirs, files in os.walk(path):
            for fn in files:
                if extensions and not fn.lower().endswith(extensions):
                    continue
                count += 1
    except OSError:
        swallow.note('admin._count_files:L136')
        pass
    return count


# ---- Аутентификация и сессия ----


@router.post("/api/admin/setup")
async def admin_setup(request: Request):
    """Первый вход: задать пароль, создать профиль администратора."""
    try:
        import admin_module
        if admin_module.is_initialized():
            return JSONResponse({"error": "Админ уже сконфигурирован"}, status_code=400)
        body = await request.json()
        password = body.get("password", "")
        if not password or len(password) < 4:
            return JSONResponse({"error": "Пароль должен содержать минимум 4 символа"}, status_code=400)
        result = await _run_sync(admin_module.setup, password)
        if "error" in result:
            return JSONResponse({"error": result["error"]}, status_code=400)
        resp = JSONResponse({"ok": True, "initialized": True})
        _set_admin_cookie(resp, result["token"])
        # Первая настройка создаёт профиль admin — сразу переключаемся на него:
        # доступ к панели есть только у профиля с ролью admin, и панель под
        # тем профилем, с которого её настраивали, тут же попросила бы вход.
        try:
            import profiles_module as PM

            def _switch_admin():
                if not PM.switch_profile(admin_module.ADMIN_PROFILE_ID):
                    return False
                profile_state.apply_profile_globals(profile_id=admin_module.ADMIN_PROFILE_ID)
                return True

            await _run_sync(_switch_admin)
            resp.set_cookie(PM.COOKIE_NAME, admin_module.ADMIN_PROFILE_ID, path="/",
                            max_age=31536000, httponly=True, samesite="lax")
        except Exception as e:
            logger.error(f"/api/admin/setup profile switch: {e}")
        return resp
    except Exception as e:
        logger.error(f"/api/admin/setup error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/login")
async def admin_login(request: Request):
    """Вход в панель администратора."""
    try:
        import admin_module
        import profiles_module as PM
        if not admin_module.is_initialized():
            return JSONResponse({"error": "Панель не настроена"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            swallow.note('admin.admin_login:L194')
            body = {}
        token, err = await _run_sync(admin_module.login, body.get("password", ""))
        if err:
            return JSONResponse({"error": err}, status_code=403)
        # Пароль админ-панели не превращает любой профиль в администратора:
        # панель описывает весь экземпляр, поэтому доступ к ней разрешён только
        # профилю с ролью admin (проверяется и дальше — admin_request_ok).
        # Но проверять роль до пароля нельзя: из-за этого ошибка «не тот
        # профиль» выглядела как «неверный пароль», и войти было невозможно,
        # пока активен другой профиль. Теперь верный пароль сам переключает
        # запрос на профиль admin — аутентификацию проходит именно пароль.
        switched = False
        if not admin_module.request_is_admin_profile(request):
            def _activate():
                if not PM.switch_profile(admin_module.ADMIN_PROFILE_ID):
                    return False
                profile_state.apply_profile_globals(profile_id=admin_module.ADMIN_PROFILE_ID)
                return True

            if not await _run_sync(_activate):
                return JSONResponse({
                    "ok": True, "authenticated": True,
                    "need_admin_profile": True,
                    "error": "Пароль верный, но переключить профиль «Администратор» "
                             "не удалось — сделайте это вручную и войдите снова",
                }, status_code=403)
            switched = True
        resp = JSONResponse({"ok": True, "authenticated": True,
                             "profile_switched": switched})
        _set_admin_cookie(resp, token)
        if switched:
            resp.set_cookie(PM.COOKIE_NAME, admin_module.ADMIN_PROFILE_ID, path="/",
                            max_age=31536000, httponly=True, samesite="lax")
        return resp
    except Exception as e:
        logger.error(f"/api/admin/login error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/change_password")
async def admin_change_password(request: Request):
    """Смена пароля администратора (требует авторизацию в панели)."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        import admin_module
        body = await request.json()
        ok, err = await _run_sync(
            admin_module.change_password,
            body.get("old_password", ""), body.get("new_password", "")
        )
        if not ok:
            return JSONResponse({"error": err}, status_code=400)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"/api/admin/change_password error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/logout")
async def admin_logout(request: Request):
    """Выход: сбросить токен сессии."""
    try:
        import admin_module
        token = _admin_cookie(request)
        if token:
            await _run_sync(admin_module.logout, token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(ADMIN_COOKIE, path="/")
        return resp
    except Exception as e:
        logger.error(f"/api/admin/logout error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/session")
def admin_session(request: Request):
    """Состояние: сконфигурирована ли панель, авторизован ли запрос.

    admin_profile — текущий профиль запроса вообще имеет роль admin. Без него
    страница показывала бы форму пароля, которую нельзя пройти: пароль есть,
    но панель этому профилю не положена.
    """
    import admin_module
    return {
        "initialized": admin_module.is_initialized(),
        "authenticated": _admin_ok(request),
        "admin_profile": admin_module.request_is_admin_profile(request)
    }


@router.get("/api/admin/stats")
async def admin_stats(request: Request):
    """Дашборд: системные метрики, профили, аналитика агента."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        import admin_module
        import psutil

        def _collect():
            import profiles_module
            import agent_analytics

            cpu = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            sc_percent, sc_total, sc_used = None, None, None
            try:
                sc = psutil.disk_usage(os.path.abspath(os.sep) if os.name == "nt" else "/")
                sc_percent, sc_total, sc_used = sc.percent, sc.total, sc.used
            except Exception:
                swallow.note('admin._collect:L306')
                pass
            procs = 0
            try:
                procs = len(list(psutil.process_iter(attrs=None)))
            except Exception:
                swallow.note('admin._collect:L312')
                pass

            profiles = []
            for p in profiles_module.list_profiles():
                entry = dict(p)
                pid = p["id"]
                now = time.time()
                cached = _STATS_SIZE_CACHE.get(pid)
                if cached and (now - cached[0]) < _STATS_SIZE_TTL:
                    stats = cached[1]
                else:
                    pdir = profiles_module._profile_path(pid)
                    data_dir = profiles_module._profile_data_path(pid)
                    stats = {
                        "size_bytes": _dir_size(pdir),
                        "notes_count": _count_files(os.path.join(data_dir, "notes"), (".md", ".txt")),
                        # chat_sessions, не chats: PROFILE_DATA_DIRS["chat_dir"]
                        "chat_count": _count_files(os.path.join(data_dir, "chat_sessions"), (".json",)),
                        "memory_count": _count_files(os.path.join(data_dir, "memory"), (".json", ".md")),
                    }
                    _STATS_SIZE_CACHE[pid] = (now, stats)
                entry.update(stats)
                entry["extensions_dir"] = _profile_extensions_dir(pid)
                entry["plugins_dir"] = _profile_plugins_dir(pid)
                profiles.append(entry)

            analytics = {}
            try:
                analytics = agent_analytics.get_dashboard()
            except Exception:
                swallow.note('admin._collect:L343')
                analytics = {}

            blocked = admin_module.blocked_profile_ids()
            return {
                "system": {
                    "cpu_percent": cpu,
                    "memory_percent": mem.percent,
                    "memory_total": mem.total,
                    "memory_used": mem.used,
                    "disk_percent": sc_percent,
                    "disk_total": sc_total,
                    "disk_used": sc_used,
                    "processes": procs
                },
                "profiles": profiles,
                "blocked": blocked,
                "agent": analytics
            }

        return await _run_sync(_collect)
    except Exception as e:
        logger.error(f"/api/admin/stats error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Управление профилями ----


@router.post("/api/admin/profiles/block")
async def admin_profiles_block(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import admin_module
        body = await request.json()
        profile_id = body.get("id", "")
        blocked = bool(body.get("blocked"))
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        ok, err = await _run_sync(admin_module.set_profile_blocked, profile_id, blocked)
        if not ok:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True, "blocked": blocked}
    except Exception as e:
        logger.error(f"/api/admin/profiles/block error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Роли, группы, блокировка модулей, видимость (вкладка «Доступ») ----


@router.post("/api/admin/profiles/role")
async def admin_profiles_role(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import profiles_module
        body = await request.json()
        profile_id = body.get("id", "")
        role = body.get("role", "")
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        ok, err = await _run_sync(profiles_module.set_profile_role, profile_id, role)
        if not ok:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True, "role": role}
    except Exception as e:
        logger.error(f"/api/admin/profiles/role error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/profiles/groups")
async def admin_profiles_groups(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import profiles_module
        body = await request.json()
        profile_id = body.get("id", "")
        groups = body.get("groups") or []
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        ok, err = await _run_sync(profiles_module.set_profile_groups, profile_id, groups)
        if not ok:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}
    except Exception as e:
        logger.error(f"/api/admin/profiles/groups error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/profiles/modules")
async def admin_profiles_modules(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import profiles_module
        body = await request.json()
        profile_id = body.get("id", "")
        blocked_modules = body.get("blocked_modules") or []
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        ok, err = await _run_sync(profiles_module.set_profile_modules, profile_id, blocked_modules)
        if not ok:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}
    except Exception as e:
        logger.error(f"/api/admin/profiles/modules error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/groups")
async def admin_groups(request: Request):
    """CRUD групп: {action: create|rename|delete, id?, name?}."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        import profiles_module
        body = await request.json()
        action = (body.get("action") or "").strip().lower()

        def _do():
            if action == "create":
                g = profiles_module.create_group(body.get("name", ""))
                return (True, g) if g else (False, "Пустое имя группы")
            if action == "rename":
                ok = profiles_module.rename_group(body.get("id", ""), body.get("name", ""))
                return (ok, None) if ok else (False, "Группа не найдена")
            if action == "delete":
                ok = profiles_module.delete_group(body.get("id", ""))
                return (ok, None) if ok else (False, "Группа не найдена")
            return False, "Неизвестное действие"

        ok, res = await _run_sync(_do)
        if not ok:
            return JSONResponse({"error": res}, status_code=400)
        return {"ok": True, "group": res if isinstance(res, dict) else None}
    except Exception as e:
        logger.error(f"/api/admin/groups error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/access")
async def admin_access_get(request: Request):
    """Реестр модулей, группы и текущая политика видимости для вкладки «Доступ»."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _collect():
            import access_module
            import profiles_module
            return {
                "modules": access_module.list_modules(),
                "groups": profiles_module.list_groups(),
                "access": profiles_module.get_access_policy(),
            }

        return await _run_sync(_collect)
    except Exception as e:
        logger.error(f"/api/admin/access error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/access")
async def admin_access_save(request: Request):
    """Сохранить глобальную политику видимости (ai_engine_modal, notifications)."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()

        def _apply():
            import profiles_module
            ok, err = profiles_module.set_access_policy(body)
            if not ok:
                return {"error": err}
            return {"ok": True, "access": profiles_module.get_access_policy()}

        res = await _run_sync(_apply)
        if res.get("error"):
            return JSONResponse({"error": res["error"]}, status_code=400)
        return res
    except Exception as e:
        logger.error(f"/api/admin/access save error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/profiles/reset-password")
async def admin_profiles_reset_password(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import profiles_module
        body = await request.json()
        profile_id = body.get("id", "")
        password = str(body.get("password", "") or "")
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        if password and len(password) < 4:
            return JSONResponse({"error": "Пароль должен содержать минимум 4 символа"},
                                status_code=400)
        ok = await _run_sync(profiles_module.set_profile_password, profile_id, password)
        if not ok:
            return JSONResponse({"error": "Профиль не найден"}, status_code=404)
        return {"ok": True}
    except Exception as e:
        logger.error(f"/api/admin/profiles/reset-password error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/profiles/rename")
async def admin_profiles_rename(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import profiles_module
        body = await request.json()
        profile_id = body.get("id", "")
        name = body.get("name", "").strip()
        if not profile_id or not name:
            return JSONResponse({"error": "Не указан ID или имя"}, status_code=400)
        ok = await _run_sync(profiles_module.rename_profile, profile_id, name)
        if not ok:
            return JSONResponse({"error": "Профиль не найден"}, status_code=404)
        return {"ok": True}
    except Exception as e:
        logger.error(f"/api/admin/profiles/rename error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/profiles/delete")
async def admin_profiles_delete(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import profiles_module
        import admin_module
        body = await request.json()
        profile_id = body.get("id", "")
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        if profile_id == admin_module.ADMIN_PROFILE_ID:
            return JSONResponse({"error": "Нельзя удалить профиль администратора"}, status_code=400)

        def _delete():
            profiles = profiles_module.list_profiles()
            if len(profiles) <= 1:
                return False, "Нельзя удалить единственный профиль"
            active_prof = profiles_module.get_active_profile()
            active = active_prof["id"] if active_prof else None
            if active == profile_id:
                default_id = profiles_module.get_default_profile_id()
                if not profiles_module.switch_profile(default_id):
                    return False, "Не удалось переключиться на дефолтный профиль"
                profile_state.apply_profile_globals(profile_id=default_id)
            return profiles_module.delete_profile(profile_id)

        ok, msg = await _run_sync(_delete)
        if not ok:
            return JSONResponse({"error": msg}, status_code=400)
        return {"ok": True}
    except Exception as e:
        logger.error(f"/api/admin/profiles/delete error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/profiles/clone")
async def admin_profiles_clone(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import profiles_module
        body = await request.json()
        src_id = body.get("id", "")
        new_name = (body.get("name") or "").strip()
        if not src_id or not profiles_module.profile_exists(src_id):
            return JSONResponse({"error": "Профиль не найден"}, status_code=404)
        if src_id == "admin":
            return JSONResponse({"error": "Нельзя клонировать профиль администратора"}, status_code=400)

        def _clone():
            import profiles_module as PM
            created, err = PM.clone_profile(src_id, new_name or None)
            if err or not created:
                raise RuntimeError(err or "Не удалось создать профиль")
            return created["id"], created["name"]

        pid, name = await _run_sync(_clone)
        return {"ok": True, "profile": {"id": pid, "name": name}}
    except Exception as e:
        logger.error(f"/api/admin/profiles/clone error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/profiles/config")
async def admin_profile_config(request: Request, id: str = Query(...)):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import profiles_module
        cfg = await _run_sync(profiles_module.get_profile_config, id)
        private = await _run_sync(profiles_module.get_profile_private_paths, id)
        return {"config": cfg, "private_paths": private}
    except Exception as e:
        logger.error(f"/api/admin/profiles/config error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/profiles/tree")
async def admin_profile_tree(request: Request, id: str = Query(...), depth: int = Query(3, le=6)):
    """Дерево каталога профиля (пути относительно папки профиля)."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        import profiles_module

        def _tree():

            def walk(path, rel, remaining):
                if remaining <= 0:
                    return None
                items = []
                try:
                    entries = sorted(os.listdir(path))
                except OSError:
                    swallow.note('admin.walk:L670')
                    return items
                for name in entries:
                    if name.startswith("."):
                        continue
                    full = os.path.join(path, name)
                    rel_path = os.path.join(rel, name)
                    if os.path.isdir(full):
                        items.append({
                            "name": name,
                            "path": rel_path,
                            "type": "dir",
                            "children": walk(full, rel_path, remaining - 1)
                        })
                    else:
                        try:
                            size = os.path.getsize(full)
                        except OSError:
                            swallow.note('admin.walk:L688')
                            size = 0
                        items.append({"name": name, "path": rel_path, "type": "file", "size": size})
                return items

            base = profiles_module._profile_path(id)
            if not os.path.isdir(base):
                return []
            return walk(base, "", depth)

        return {"tree": await _run_sync(_tree)}
    except Exception as e:
        logger.error(f"/api/admin/profiles/tree error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Расширения профилей ----


@router.get("/api/admin/extensions")
async def admin_extensions_list(request: Request, profile_id: str = Query(...)):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import extension_system

        def _list():
            ext_dir = _profile_extensions_dir(profile_id)
            mgr = extension_system.get_manager_for(ext_dir)
            return {"extensions": mgr.get_extensions(), "stats": mgr.get_stats(), "dir": ext_dir}

        return await _run_sync(_list)
    except Exception as e:
        logger.error(f"/api/admin/extensions error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/extensions/toggle")
async def admin_extension_toggle(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import extension_system
        body = await request.json()
        profile_id = body.get("profile_id", "")
        name = body.get("name", "")
        action = body.get("action", "")  # enable | disable | delete
        if not profile_id or not name or action not in ("enable", "disable", "delete"):
            return JSONResponse({"error": "Неверные параметры"}, status_code=400)

        def _do():
            ext_dir = _profile_extensions_dir(profile_id)
            mgr = extension_system.get_manager_for(ext_dir)
            if action == "enable":
                return mgr.enable(name)
            if action == "disable":
                return mgr.disable(name)
            return mgr.delete(name)

        result = await _run_sync(_do)
        return {"ok": True, "result": result}
    except Exception as e:
        logger.error(f"/api/admin/extensions/toggle error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Плагины профилей ----


@router.get("/api/admin/plugins")
async def admin_plugins_list(request: Request, profile_id: str = Query(...)):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import plugin_system

        def _list():
            pdir = _profile_plugins_dir(profile_id)
            mgr = plugin_system.get_manager_for(pdir)
            return {"plugins": mgr.get_plugin_list(), "dir": pdir}

        return await _run_sync(_list)
    except Exception as e:
        logger.error(f"/api/admin/plugins error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/plugins/toggle")
async def admin_plugin_toggle(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import plugin_system
        body = await request.json()
        profile_id = body.get("profile_id", "")
        name = body.get("name", "")
        action = body.get("action", "")  # enable | disable | delete
        if not profile_id or not name or action not in ("enable", "disable", "delete"):
            return JSONResponse({"error": "Неверные параметры"}, status_code=400)

        def _do():
            pdir = _profile_plugins_dir(profile_id)
            mgr = plugin_system.get_manager_for(pdir)
            if action == "enable":
                return mgr.enable_plugin(name)
            if action == "disable":
                return mgr.disable_plugin(name)
            return mgr.delete_plugin(name)

        ok = await _run_sync(_do)
        return {"ok": True, "result": ok}
    except Exception as e:
        logger.error(f"/api/admin/plugins/toggle error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Мониторинг ----


@router.get("/api/admin/monitor")
async def admin_monitor(request: Request):
    """Текущая точка метрик + история для графиков."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        import admin_module
        point = await _run_sync(admin_module.snapshot_metrics)
        history = await _run_sync(admin_module.read_metrics)
        return {"current": point, "history": history}
    except Exception as e:
        logger.error(f"/api/admin/monitor error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/load")
async def admin_load(request: Request, fresh: int = 0):
    """Нагрузка на сервер: RPS, перцентили, топ эндпоинтов, клиенты, пулы, офлоад."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        # Снимок пересобирается не чаще раза в секунду: админка опрашивает
        # его каждые 2 с, а сам снимок (сортировка семплов, статус демона)
        # стоил 45–70 мс. ?fresh=1 — принудительно новый.
        _now = time.time()
        if not fresh and _LOAD_CACHE["data"] is not None and _now - _LOAD_CACHE["t"] < 1.0:
            return _LOAD_CACHE["data"]
        import perf_metrics
        snap = perf_metrics.snapshot()
        try:
            import load_daemon
            snap["load_daemon"] = await _run_sync(load_daemon.status)
        except Exception as e:
            swallow.note('admin.admin_load:L840', e)
            snap["load_daemon"] = {"error": str(e)[:200]}
        try:
            import resource_governor
            snap["governor"] = resource_governor.state()
        except Exception as e:
            swallow.note('admin.admin_load:L846', e)
            snap["governor"] = {"ok": False, "error": str(e)[:200]}
        _LOAD_CACHE["t"] = time.time()
        _LOAD_CACHE["data"] = snap
        return snap
    except Exception as e:
        logger.error(f"/api/admin/load error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/load/reset")
def admin_load_reset(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import perf_metrics
        perf_metrics.reset()
        # Сброс должен быть виден сразу, а не через секунду из кэша снимка.
        _LOAD_CACHE["t"] = 0.0
        _LOAD_CACHE["data"] = None
        return {"ok": True}
    except Exception as e:
        logger.error(f"/api/admin/load/reset error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/governor/state")
def admin_governor_state(request: Request):
    """Губернатор ресурсов: уровень, лимит, давление и последние переходы."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        import resource_governor
        return resource_governor.state()
    except Exception as e:
        logger.error(f"/api/admin/governor/state error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/admin/governor/config")
async def admin_governor_config(request: Request):
    """Включение и режим губернатора: shadow (наблюдение) | soft | full."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        try:
            body = await request.json()
        except Exception:
            swallow.note('admin.admin_governor_config:L884')
            body = {}
        if not isinstance(body, dict):
            body = {}
        import resource_governor
        enabled = body.get("enabled") if "enabled" in body else None
        mode = body.get("mode") if "mode" in body else None
        return await _run_sync(resource_governor.set_config, enabled, mode)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"/api/admin/governor/config error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/admin/processes")
async def admin_processes(request: Request, limit: int = Query(15, le=50)):
    """Топ процессов по CPU."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        import admin_module
        limit = limit or 15
        return {"processes": await _run_sync(admin_module.top_processes, limit)}
    except Exception as e:
        logger.error(f"/api/admin/processes error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/logs")
async def admin_logs(request: Request, lines: int = Query(200, le=2000)):
    """Последние строки лога сервера."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _tail():
            log_file = getattr(server, "_log_file", "")
            entries = []
            if log_file and os.path.exists(log_file):
                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        data = f.readlines()
                    for line in data[-lines:]:
                        line = line.rstrip("\n")
                        if not line:
                            continue
                        m = __import__("re").match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)$", line)
                        if m:
                            entries.append({"ts": m.group(1), "level": m.group(2), "msg": m.group(3)})
                        else:
                            entries.append({"ts": "", "level": "INFO", "msg": line[:400]})
                except Exception as e:
                    entries.append({"ts": "", "level": "ERROR", "msg": f"Ошибка чтения лога: {e}"})
            size = os.path.getsize(log_file) if log_file and os.path.exists(log_file) else 0
            return {"log_file": log_file, "size_bytes": size, "entries": entries[::-1]}

        return await _run_sync(_tail)
    except Exception as e:
        logger.error(f"/api/admin/logs error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/health")
async def admin_health(request: Request):
    """Статус сервисов: LLM, TTS, индексы, диск."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _collect():
            import psutil as _ps
            health = {}
            try:
                health["llm"] = llm_text._check_lm_studio_health()
            except Exception as e:
                swallow.note('admin._collect:L927', e)
                health["llm"] = {"available": False, "error": str(e)[:120]}
            health["llm_backend"] = server.LLM_BACKEND
            health["llm_current_model"] = _current_llm_model()
            health["tts"] = {
                "silero": getattr(server, "SILERO_TTS_AVAILABLE", False),
                "qwen3": getattr(server, "QWEN3_TTS_AVAILABLE", False),
                "piper": getattr(server, "PIPER_TTS_AVAILABLE", False),
            }
            try:
                health["index"] = assets_index.get_index_status()
            except Exception:
                swallow.note('admin._collect:L939')
                health["index"] = {}
            health["index_dir"] = getattr(server, "INDEX_DIR", None)
            try:
                p = _ps.Process()
                health["uptime_sec"] = int(time_now() - p.create_time())
            except Exception:
                swallow.note('admin._collect:L946')
                health["uptime_sec"] = 0
            try:
                health["disk"] = _ps.disk_usage(os.path.abspath(os.sep))
            except Exception:
                swallow.note('admin._collect:L951')
                health["disk"] = None
            health["python"] = __import__("sys").version.split()[0]
            health["server_pid"] = os.getpid()
            return health

        return await _run_sync(_collect)
    except Exception as e:
        logger.error(f"/api/admin/health error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def time_now():
    import time
    return time.time()


def _llama_model():
    """Модель llama.cpp (фоллбэк — LM Studio), т.к. в server.py её нет при чистом импорте."""
    return getattr(server, "LLAMA_CPP_MODEL", server.LM_STUDIO_MODEL)


def _current_llm_model():
    if server.LLM_BACKEND == "llamacpp":
        return _llama_model()
    return server.LM_STUDIO_MODEL


# ---- Управление данными ----


def _safe_profile_path(profile_id, rel_path):
    """Проверить, что путь лежит внутри каталога профиля. Вернуть полный путь или None.

    profile_id проверяется отдельно: os.path.join(base, "C:\\evil") вернёт
    абсолютный путь, и прежняя проверка «target внутри base» проходила бы,
    потому что base сам оказывался этим абсолютным путём.
    """
    import re
    import profiles_module
    pid = str(profile_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pid):
        return None
    if not profiles_module.profile_exists(pid):
        return None
    base = os.path.realpath(profiles_module._profile_path(pid))
    root = os.path.realpath(profiles_module.PROFILES_DIR)
    if base == root or not base.startswith(root + os.sep):
        return None
    target = os.path.realpath(os.path.join(base, rel_path or ""))
    if target == base or not target.startswith(base + os.sep):
        return None
    return target


@router.get("/api/admin/storage")
async def admin_storage(request: Request):
    """Каталоги данных, размеры и кэши."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _collect():
            import profiles_module
            from cache_manager import cache, tts_cache, search_cache

            def _cat(key, path, label):
                size = _dir_size(path) if path and os.path.isdir(path) else 0
                files = _count_files(path) if path and os.path.isdir(path) else 0
                return {"key": key, "label": label, "path": path, "size_bytes": size, "files": files}

            cats = []
            cats.append(_cat("profiles", profiles_module.PROFILES_DIR, "Профили (все данные)"))
            cats.append(_cat("notes", getattr(server, "NOTES_DIR", ""), "Заметки"))
            cats.append(_cat("chat", getattr(server, "CHAT_DIR", ""), "Чат-сессии"))
            cats.append(_cat("memory", getattr(server, "MEMORY_DIR", ""), "Память агента"))
            cats.append(_cat("attachments", getattr(server, "ATTACHMENTS_DIR", ""), "Вложения"))
            cats.append(_cat("extensions", getattr(server, "EXTENSIONS_DIR", ""), "Расширения"))
            cats.append(_cat("plugins", getattr(server, "PLUGINS_DIR", ""), "Плагины"))
            for i, d in enumerate(getattr(server, "DATA_DIRS", []) or []):
                cats.append(_cat(f"data_dirs_{i}", d, f"Каталог данных #{i + 1}"))
            idx = getattr(server, "INDEX_DIR", None)
            if idx:
                cats.append(_cat("search_index", idx, "Поисковый индекс"))
            caches = {
                "search": search_cache.stats() if search_cache else {},
                "tts": tts_cache.stats() if tts_cache else {},
                "general": cache.stats() if cache else {},
            }
            return {"categories": cats, "caches": caches}

        return await _run_sync(_collect)
    except Exception as e:
        logger.error(f"/api/admin/storage error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/cache/clear")
async def admin_cache_clear(request: Request):
    """Очистить кэши (search / tts / general / all)."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        from cache_manager import cache, tts_cache, search_cache
        body = await request.json()
        kind = body.get("kind", "all")
        cleared = {}
        if kind in ("all", "search") and search_cache:
            cleared["search"] = search_cache.stats().get("size", 0)
            search_cache.clear()
        if kind in ("all", "tts") and tts_cache:
            cleared["tts"] = tts_cache.stats().get("size", 0)
            tts_cache.clear()
        if kind in ("all", "general") and cache:
            cleared["general"] = cache.stats().get("size", 0)
            cache.clear()
        return {"ok": True, "cleared": cleared}
    except Exception as e:
        logger.error(f"/api/admin/cache/clear error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/storage/cleanup")
async def admin_storage_cleanup(request: Request):
    """Безопасная очистка: __pycache__, временные файлы, старые ротации логов."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _cleanup():
            import tempfile
            freed = 0
            removed = 0
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

            def _rm_file(path):
                nonlocal freed, removed
                try:
                    freed += os.path.getsize(path)
                    os.remove(path)
                    removed += 1
                except Exception:
                    swallow.note('admin._rm_file:L1093')
                    pass

            # __pycache__ в дереве проекта
            for root, dirs, _files in os.walk(base):
                for d in list(dirs):
                    if d == "__pycache__":
                        p = os.path.join(root, d)
                        try:
                            import shutil
                            shutil.rmtree(p)
                            removed += 1
                        except Exception:
                            swallow.note('admin._cleanup:L1106')
                            pass
                        dirs.remove(d)

            log_file = getattr(server, "_log_file", "")
            if log_file:
                log_dir = os.path.dirname(log_file)
                log_name = os.path.basename(log_file)
                if os.path.isdir(log_dir):
                    for fn in os.listdir(log_dir):
                        if fn.startswith(log_name + "."):
                            _rm_file(os.path.join(log_dir, fn))

            # Временный каталог: *.tmp / *.temp старше часа
            tmp_dir = tempfile.gettempdir()
            try:
                cutoff = time_now() - 3600
                for fn in os.listdir(tmp_dir):
                    if not (fn.endswith(".tmp") or fn.endswith(".temp")):
                        continue
                    p = os.path.join(tmp_dir, fn)
                    try:
                        if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                            _rm_file(p)
                    except Exception:
                        swallow.note('admin._cleanup:L1131')
                        pass
            except Exception:
                swallow.note('admin._cleanup:L1134')
                pass
            return {"ok": True, "freed_bytes": freed, "removed": removed}

        return await _run_sync(_cleanup)
    except Exception as e:
        logger.error(f"/api/admin/storage/cleanup error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/profiles/tree-delete")
async def admin_profile_tree_delete(request: Request):
    """Удалить файл/каталог внутри профиля (безопасно по пути)."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        profile_id = body.get("id", "")
        rel = body.get("path", "")
        if not profile_id or not rel:
            return JSONResponse({"error": "Не указан профиль или путь"}, status_code=400)

        def _delete():
            target = _safe_profile_path(profile_id, rel)
            if not target:
                return False, "Путь вне каталога профиля"
            if os.path.isfile(target):
                size = os.path.getsize(target)
                os.remove(target)
                return True, f"Файл удалён ({size} Б)"
            if os.path.isdir(target):
                try:
                    shutil.rmtree(target)
                    return True, "Каталог удалён"
                except Exception as e:
                    swallow.note('admin._delete:L1169', e)
                    return False, f"Ошибка: {e}"
            return False, "Путь не найден"

        ok, msg = await _run_sync(_delete)
        if not ok:
            return JSONResponse({"error": msg}, status_code=400)
        return {"ok": True, "message": msg}
    except Exception as e:
        logger.error(f"/api/admin/profiles/tree-delete error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Модели ----


@router.get("/api/admin/models")
async def admin_models(request: Request):
    """Текущий бэкенд, модель и список доступных моделей."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _collect():
            from routes.legacy_system import _models_cache, _build_models_payload
            # Список моделей — из общего кэша с /api/models (тот же источник и
            # тот же сетевой вызов), health — из своего: первый удар по
            # недоступному бэкенду стоит секунды, панель его не ждёт.
            base = _models_cache.get_or_set_swr("models", _build_models_payload,
                                                stale_ttl=300.0)
            health = _ADMIN_MODELS_CACHE.get_or_set_swr(
                "lm_health", llm_text._check_lm_studio_health, ttl=30.0, stale_ttl=300.0)
            current = _current_llm_model()
            return {
                "backend": server.LLM_BACKEND,
                "current": current,
                "lm_url": server.LM_STUDIO_BASE_URL,
                "lm_model": server.LM_STUDIO_MODEL,
                "lm_embed_model": getattr(server, "LM_STUDIO_EMBED_MODEL", ""),
                "llama_url": server.LLAMA_CPP_BASE_URL,
                "models": (base or {}).get("models") or [],
                "health": health,
            }

        return await _run_sync(_collect)
    except Exception as e:
        logger.error(f"/api/admin/models error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/models/select")
async def admin_models_select(request: Request):
    """Выбрать бэкенд/модель и сохранить в конфиг."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        backend = body.get("backend", "")
        if backend and backend not in ("lmstudio", "llamacpp"):
            return JSONResponse({"error": "Неверный бэкенд"}, status_code=400)

        def _apply():
            from config_module import get_config, save_config
            cfg = get_config()
            if backend:
                cfg["backend"] = backend
            lm = dict(cfg.get("lm_studio") or {})
            if body.get("model"):
                lm["model"] = body["model"].strip()
            if body.get("lm_url"):
                lm["url"] = body["lm_url"].strip()
            if body.get("lm_embed_model"):
                lm["embed_model"] = body["lm_embed_model"].strip()
            cfg["lm_studio"] = lm
            lc = dict(cfg.get("llama_cpp") or {})
            if body.get("llama_url"):
                lc["url"] = body["llama_url"].strip()
            cfg["llama_cpp"] = lc
            save_config(cfg)
            profile_state.apply_profile_globals()
            # Выбранный бэкенд/модель должны быть видны панели сразу, а не через TTL.
            from routes.legacy_system import _models_cache
            _models_cache.invalidate("models")
            _ADMIN_MODELS_CACHE.invalidate("lm_health")
            return {
                "ok": True,
                "backend": server.LLM_BACKEND,
                "model": _current_llm_model(),
            }

        return await _run_sync(_apply)
    except Exception as e:
        logger.error(f"/api/admin/models/select error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Движок решений laya («System 1») ----

# Описания функций для панели: ключ в config.json → (название, пояснение).
LAYA_FEATURES = [
    ("news_semantic", "Семантика в новостях",
     "Переранкер подборки: понимает тему и релевантность, а не только совпадение слов. Работает поверх обычного скоринга."),
    ("news_briefing_fast", "Быстрый брифинг без LLM",
     "Утренняя подборка новостей шаблоном — без обращения к большой модели."),
    ("social_triage", "Триаж соцсети",
     "Мягкая пометка постов и комментариев: спам, тон, срочность. Посты не блокируются."),
    ("media_autotag", "Автотеги медиа",
     "Подбор тегов для коллекции по названию и описанию."),
    ("media_recommend", "Рекомендации медиа",
     "Оценка объектов библиотеки против профиля интересов."),
    ("books_classify", "Разбор книг по категориям",
     "Книга относится к одной из ваших категорий; неуверенные уходят в обычный AI-путь."),
    ("voice_intent", "Намерение голосового ввода",
     "Команда / вопрос / болтовня в распознанной речи."),
    ("guard_injection", "Защита от инструкций в чужом тексте",
     "Проверка статей, веб-страниц и вложений: если внутри текста есть попытка изменить правила — он помечается как цитата."),
    ("bot_triage", "Триаж сообщений ботам",
     "Заведомый спам не получает ответа, обычные сообщения — как раньше."),
    ("files_category", "Категория файлов",
     "Подсказка по типу файла при загрузке."),
    ("notes_auto_tags", "Автотеги заметок",
     "Тег подбирается из ваших существующих тегов, а не выдумывается."),
    ("notes_semantic_groups", "Смысловые группы в графе",
     "laya относит заметки к смысловым темам — в графе появляется отдельный фильтр «По смыслу». Кэш пересчитывается кнопкой."),
    ("education_auto_terms", "Классификация уроков",
     "Предмет и сложность урока как метки, без генерации текста."),
    ("extensions_access_warning", "Предупреждение о расширениях",
     "Оценка риска по манифесту расширения перед установкой и запуском."),
    ("search_route", "Роутинг источников поиска",
     "Определяет, где искать ответ, и сужает поиск при высокой уверенности."),
    ("mcp", "MCP-сервер laya",
     "Регистрирует MCP-сервер laya для агента (инструменты решений)."),
]

LAYA_MODELS = ["multilingual", "english", "typed-decisions", ""]
LAYA_DEVICES = ["cpu", "cuda", "auto"]


def _laya_section(cfg):
    sec = dict((cfg or {}).get("laya") or {})
    feats = dict(sec.get("features") or {})
    return sec, feats


@router.get("/api/admin/laya")
async def admin_laya(request: Request):
    """Настройки движка laya: конфиг, статус сервиса и список функций."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _collect():
            from config_module import get_config
            import laya_client
            cfg = get_config()
            sec, feats = _laya_section(cfg)
            try:
                import laya_serve
                serve = laya_serve.status()
                runtime = laya_serve.probe()
            except Exception as e:
                swallow.note('admin._collect:L1330', e)
                serve = {}
                runtime = {"available": False, "cmd": [], "python": "", "error": str(e)}
            # Клиента спрашиваем только когда порт уже слушает: каждое
            # обращение к мёртвому порту на Windows висит до таймаута, а
            # панель опрашивает статус раз в несколько секунд.
            if serve.get("running"):
                try:
                    status = laya_client.status()
                except Exception:
                    swallow.note('admin._collect:L1340')
                    status = {"enabled": bool(sec.get("enabled")), "available": False}
            else:
                status = {"enabled": bool(sec.get("enabled")), "available": False,
                          "model": sec.get("model") or "auto"}
            return {
                "config": {
                    "enabled": bool(sec.get("enabled")),
                    "url": sec.get("url") or "http://127.0.0.1:8377",
                    "python": sec.get("python") or "",
                    "autostart": bool(sec.get("autostart")),
                    "model": sec.get("model") if sec.get("model") is not None else "multilingual",
                    "device": sec.get("device") or "cpu",
                    "max_loaded": int(sec.get("max_loaded") or 1),
                    "idle_unload_seconds": int(sec.get("idle_unload_seconds") or 900),
                    "timeout_ms": int(sec.get("timeout_ms") or 1500),
                    "max_chars": int(sec.get("max_chars") or 4000),
                    "api_key": sec.get("api_key") or "",
                    "log_level": sec.get("log_level") or "warning",
                },
                "features": [
                    {"key": k, "label": lbl, "desc": d, "enabled": bool(feats.get(k))}
                    for k, lbl, d in LAYA_FEATURES
                ],
                "status": status,
                "serve": serve,
                "runtime": runtime,
            }

        return await _run_sync(_collect)
    except Exception as e:
        logger.error(f"/api/admin/laya error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/laya")
async def admin_laya_save(request: Request):
    """Сохранить настройки laya в config.json и применить их."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "Ожидается JSON-объект"}, status_code=400)

        def _apply():
            from config_module import get_config, save_config, reload_config
            cfg = get_config()
            sec, feats = _laya_section(cfg)

            def _bool(v, default=False):
                return bool(v) if v is not None else default

            def _int(v, default):
                try:
                    return int(v)
                except Exception:
                    swallow.note('admin._int:L1397')
                    return default

            sec["enabled"] = _bool(body.get("enabled"), sec.get("enabled", False))
            sec["autostart"] = _bool(body.get("autostart"), sec.get("autostart", False))
            url = str(body.get("url") or "").strip()
            if url:
                sec["url"] = url
            py = str(body.get("python") or "").strip()
            if py:
                sec["python"] = py
            model = body.get("model")
            if model is not None:
                sec["model"] = str(model).strip()
            device = body.get("device")
            if device is not None:
                sec["device"] = str(device).strip()
            sec["max_loaded"] = _int(body.get("max_loaded"), sec.get("max_loaded", 1))
            sec["idle_unload_seconds"] = _int(body.get("idle_unload_seconds"), sec.get("idle_unload_seconds", 900))
            sec["timeout_ms"] = _int(body.get("timeout_ms"), sec.get("timeout_ms", 1500))
            sec["max_chars"] = _int(body.get("max_chars"), sec.get("max_chars", 4000))
            if "api_key" in body:
                sec["api_key"] = str(body.get("api_key") or "").strip()
            if body.get("log_level"):
                sec["log_level"] = str(body.get("log_level")).strip()

            new_feats = body.get("features")
            if isinstance(new_feats, dict):
                for k in list(feats.keys()) + [f[0] for f in LAYA_FEATURES]:
                    if k in new_feats:
                        feats[k] = bool(new_feats[k])
            sec["features"] = feats
            cfg["laya"] = sec
            save_config(cfg)
            reload_config()
            return {"ok": True, "config": sec}

        res = await _run_sync(_apply)

        # Сервис: включили — поднимаем, выключили — останавливаем.
        try:
            import laya_serve
            if res["config"].get("enabled") and res["config"].get("autostart"):
                threading_start_laya()
            elif not res["config"].get("enabled"):
                laya_serve.stop()
        except Exception as e:
            logger.warning(f"laya service after save: {e}")
        return res
    except Exception as e:
        logger.error(f"/api/admin/laya save error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def threading_start_laya():
    """Поднять сайдкар laya в фоне (не блокируя ответ админки)."""
    try:
        import laya_serve
    except Exception:
        swallow.note('admin.threading_start_laya:L1456')
        return
    laya_serve.start_async()


@router.post("/api/admin/laya/service")
async def admin_laya_service(request: Request):
    """Управление сайдкаром: start / stop / restart."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        action = str((body or {}).get("action") or "start").strip().lower()
        try:
            import laya_serve
        except Exception as e:
            return JSONResponse({"error": f"Модуль laya недоступен: {e}"}, status_code=500)

        def _act():
            act = action
            if act == "stop":
                laya_serve.stop()
                return {"ok": True, "action": act, "serve": laya_serve.status()}

            # Запуск ждёт /health до двух минут — в HTTP-запросе это висело бы
            # без ответа. Поднимаем в фоне, а панель опрашивает статус.
            probe = laya_serve.probe()
            if not probe.get("available"):
                return {"ok": False,
                        "error": probe.get("error") or "Движок laya недоступен",
                        "runtime": probe,
                        "serve": laya_serve.status()}
            if act == "restart":
                laya_serve.stop()
                time.sleep(1.0)
            laya_serve.start_async()
            return {"ok": True, "action": act,
                    "runtime": probe, "serve": laya_serve.status()}

        res = await _run_sync(_act)
        if not res.get("ok"):
            return JSONResponse(res, status_code=400)
        return res
    except Exception as e:
        logger.error(f"/api/admin/laya service error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Системные настройки ----


@router.get("/api/admin/system")
async def admin_system(request: Request):
    """Основные настройки сервера: auth, логирование, версии."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _collect():
            cfg = server.CFG or {}
            auth = cfg.get("auth") or {}
            log = cfg.get("logging") or {}
            return {
                "auth": {
                    "enabled": getattr(server, "AUTH_ENABLED", False),
                    "username": getattr(server, "AUTH_USERNAME", ""),
                    "has_password": bool(getattr(server, "AUTH_PASSWORD_HASH", "")),
                },
                "logging": {
                    "file": log.get("file", "~/.infinity/server.log"),
                    "level": log.get("level", "INFO"),
                },
                "backend": server.LLM_BACKEND,
                "model": _current_llm_model(),
                "host": getattr(server, "SERVER_HOST", "0.0.0.0"),
                "port": getattr(server, "SERVER_PORT", 8080),
            }

        return await _run_sync(_collect)
    except Exception as e:
        logger.error(f"/api/admin/system error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/auth/set")
async def admin_auth_set(request: Request):
    """Включить/выключить Basic Auth и сменить пароль сервера."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        import hashlib
        body = await request.json()

        def _apply():
            from config_module import get_config, save_config
            cfg = get_config()
            auth = dict(cfg.get("auth") or {})
            if "enabled" in body:
                auth["enabled"] = bool(body["enabled"])
            if body.get("username"):
                auth["username"] = body["username"].strip()
            if body.get("password"):
                auth["password_hash"] = hashlib.sha256(body["password"].encode("utf-8")).hexdigest()
            cfg["auth"] = auth
            save_config(cfg)
            profile_state.apply_profile_globals()
            g = server.__dict__
            g["AUTH_ENABLED"] = auth.get("enabled", False)
            g["AUTH_USERNAME"] = auth.get("username", "admin")
            g["AUTH_PASSWORD_HASH"] = auth.get("password_hash", "")
            g["_AUTH_CFG"] = auth
            return {"ok": True, "enabled": g["AUTH_ENABLED"], "username": g["AUTH_USERNAME"]}

        return await _run_sync(_apply)
    except Exception as e:
        logger.error(f"/api/admin/auth/set error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/onboarding")
async def admin_onboarding_get(request: Request):
    """Глобальные выключатели мастера настройки и тура."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _collect():
            from config_module import get_config
            sec = (get_config() or {}).get("onboarding") or {}
            return {
                "wizard_enabled": sec.get("wizard_enabled", True) is not False,
                "tour_enabled": sec.get("tour_enabled", True) is not False,
            }

        return await _run_sync(_collect)
    except Exception as e:
        logger.error(f"/api/admin/onboarding error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/onboarding/set")
async def admin_onboarding_set(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()

        def _apply():
            from config_module import get_config, save_config, reload_config
            cfg = get_config()
            sec = dict(cfg.get("onboarding") or {})
            if "wizard_enabled" in body:
                sec["wizard_enabled"] = bool(body["wizard_enabled"])
            if "tour_enabled" in body:
                sec["tour_enabled"] = bool(body["tour_enabled"])
            cfg["onboarding"] = sec
            save_config(cfg)
            reload_config()
            return {"ok": True, **{
                "wizard_enabled": sec.get("wizard_enabled", True) is not False,
                "tour_enabled": sec.get("tour_enabled", True) is not False,
            }}

        return await _run_sync(_apply)
    except Exception as e:
        logger.error(f"/api/admin/onboarding/set error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/compression")
async def admin_compression_get(request: Request):
    """Сжатие данных: состояние, настройки, статистика по модулям."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _collect():
            import compression_module as cz
            return cz.full_report()

        return await _run_sync(_collect)
    except Exception as e:
        logger.error(f"/api/admin/compression error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/compression/config")
async def admin_compression_config(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()

        def _apply():
            import compression_module as cz
            updates = {}
            if "enabled" in body:
                updates["enabled"] = bool(body["enabled"])
            if isinstance(body.get("modules"), dict):
                updates["modules"] = {
                    k: bool(v) for k, v in body["modules"].items()
                    if k in cz.DEFAULT_CONFIG["modules"]
                }
            for k, lo, hi in (("level", 1, 22), ("min_size_kb", 1, 1048576),
                              ("max_file_mb", 1, 65536), ("interval_min", 0, 10080),
                              ("reserve_mb", 0, 1048576), ("workers", 1, 16)):
                if k in body:
                    try:
                        updates[k] = max(lo, min(hi, int(body[k])))
                    except (TypeError, ValueError):
                        swallow.note('admin._apply:L1666')
                        pass
            if "exclude_globs" in body:
                # Список принимает модуль: там отсекаются пустые строки,
                # дубликаты и маска, которая означала бы «не сжимать ничего».
                updates["exclude_globs"] = body.get("exclude_globs")
            cfg = cz.save_config(updates)
            return {"ok": True, "config": cfg}

        return await _run_sync(_apply)
    except Exception as e:
        logger.error(f"/api/admin/compression/config error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/compression/run")
async def admin_compression_run(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = {}
        try:
            body = await request.json()
        except Exception:
            swallow.note('admin.admin_compression_run:L1690')
            pass
        modules = body.get("modules") if isinstance(body, dict) else None

        def _start():
            import compression_module as cz
            if not cz.available():
                return {"ok": False, "error": cz.backend_info().get("error")}
            ok, err = cz.run_now(modules)
            return {"ok": ok, "error": err or None, "state": cz.state_snapshot()}

        return await _run_sync(_start)
    except Exception as e:
        logger.error(f"/api/admin/compression/run error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/compression/restore")
async def admin_compression_restore(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = {}
        try:
            body = await request.json()
        except Exception:
            swallow.note('admin.admin_compression_restore:L1716')
            pass
        modules = body.get("modules") if isinstance(body, dict) else None

        def _start():
            import compression_module as cz
            ok, err = cz.restore_now(modules)
            return {"ok": ok, "error": err or None, "state": cz.state_snapshot()}

        return await _run_sync(_start)
    except Exception as e:
        logger.error(f"/api/admin/compression/restore error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/compression/stop")
async def admin_compression_stop(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _stop():
            import compression_module as cz
            return {"ok": cz.stop(), "state": cz.state_snapshot()}

        return await _run_sync(_stop)
    except Exception as e:
        logger.error(f"/api/admin/compression/stop error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/config/reload")
async def admin_config_reload(request: Request):
    """Перечитать конфиг и применить все глобальные настройки."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _reload():
            from config_module import reload_config
            cfg = reload_config()
            profile_state.apply_profile_globals()
            return {"ok": True, "backend": server.LLM_BACKEND,
                    "model": _current_llm_model()}

        return await _run_sync(_reload)
    except Exception as e:
        logger.error(f"/api/admin/config/reload error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Состояние модулей ----


@router.get("/api/admin/modules")
async def admin_modules(request: Request):
    """Состояние всех модулей и сервисов системы."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _collect():
            import importlib
            modules = []

            def add(cat, name, ok, detail="", version=""):
                modules.append({"category": cat, "name": name, "ok": bool(ok),
                                "detail": str(detail or "")[:160], "version": str(version or "")[:40]})

            # Поисковый движок
            add("Поиск", "Движок v3", getattr(server, "SEARCH_ENGINE_AVAILABLE", False),
                server.SEARCH_ENGINE_VERSION if hasattr(server, "SEARCH_ENGINE_VERSION") else "",
                version="")
            for flag, label in [("TANTIVY_AVAILABLE", "Tantivy BM25"), ("QDRANT_AVAILABLE", "Qdrant"),
                                ("FAISS_AVAILABLE", "Faiss"), ("WHOOSH_AVAILABLE", "Whoosh"),
                                ("CROSS_ENCODER_AVAILABLE", "Cross-encoder"), ("SYMSPELL_AVAILABLE", "SymSpell"),
                                ("PYMORPHY_AVAILABLE", "Pymorphy"), ("NLTK_AVAILABLE", "NLTK"),
                                ("_LM_EMBED_AVAILABLE", "LLM-эмбеддинги")]:
                add("Поиск", label, getattr(server, flag, False), "" if getattr(server, flag, False) else "не установлен")
            idx_dir = getattr(server, "INDEX_DIR", "")
            add("Поиск", "Индекс", bool(idx_dir), idx_dir or "нет каталога индекса")

            # AI
            try:
                health = llm_text._check_lm_studio_health()
                add("AI", "LLM бэкенд", health.get("available", False),
                    f"{health.get('total_models', 0)} моделей" if health.get("available") else health.get("error", ""))
            except Exception as e:
                swallow.note('admin._collect:L1804', e)
                add("AI", "LLM бэкенд", False, str(e)[:120])
            add("AI", "Бэкенд", True, server.LLM_BACKEND)
            add("AI", "Модель", True, _current_llm_model())

            # TTS
            add("Речь", "Silero TTS", getattr(server, "SILERO_TTS_AVAILABLE", False))
            add("Речь", "Qwen3 TTS", getattr(server, "QWEN3_TTS_AVAILABLE", False))
            add("Речь", "Piper TTS", getattr(server, "PIPER_TTS_AVAILABLE", False))

            # Агент и память
            # 30.09.2026: старый агент ASIO выведен из эксплуатации, его место
            # занял модуль Infinity Agent (/agentai). Показываем состояние
            # модуля, а не выключенного agent_module (он лежит в
            # app/_disabled/legacy_asio и в sys.path не подключается).
            try:
                from hermes_module.integration import launch as _hermes
                from hermes_module.integration import config as _hcfg
                _st = _hermes.status()
                _mount = getattr(_hcfg, "MOUNT_PATH", "/agentai")
                add("Агент", "Infinity Agent", bool(_st.get("webui_http")),
                    f"{_mount} · {_st.get('provider', {}).get('model', '')}"
                    if _st.get("webui_http") else "модуль выключен (agentai.enabled=false)")
                add("Агент", "Infinity Gateway", bool(_st.get("gateway_http")),
                    f"порт {_st.get('gateway_port')}" if _st.get("gateway_http")
                    else "не запущен")
            except Exception as e:
                swallow.note('admin._collect:L1831', e)
                add("Агент", "Infinity Agent", False, str(e)[:120])
            add("Агент", "ASIO (устаревший)",
                bool(((server.CFG.get("agent") or {}).get("legacy_asio") or {}).get("enabled")),
                "выключен, код в app/_disabled/legacy_asio")
            try:
                import memory_brain_module
                ok_bra = bool(callable(getattr(memory_brain_module, "generate_graph", None)))
                add("Агент", "memory_brain", ok_bra, "загружен" if ok_bra else "не инициализирован")
            except Exception as e:
                swallow.note('admin._collect:L1841', e)
                add("Агент", "memory_brain", False, str(e)[:120])
            try:
                import agent_analytics
                add("Агент", "Аналитика агента", callable(getattr(agent_analytics, "get_analytics", None)), "загружен")
            except Exception as e:
                swallow.note('admin._collect:L1847', e)
                add("Агент", "Аналитика агента", False, str(e)[:100])
            try:
                import agent_scheduler
                sched = agent_scheduler.get_scheduler()
                tasks = []
                try:
                    tasks = sched.list_tasks() or []
                except Exception:
                    swallow.note('admin._collect:L1856')
                    tasks = []
                add("Агент", "Планировщик", sched is not None, f"задач: {len(tasks)}")
            except Exception as e:
                swallow.note('admin._collect:L1860', e)
                add("Агент", "Планировщик", False, str(e)[:100])

            # Модули
            check_mods = [
                ("Поиск", "search_engine_v2", "загружен"),
                ("Медиа", "media_module", "загружен"),
                ("Медиа", "vlc_streamer", "загружен"),
                ("Книги", "desktop_module", "загружен"),
                ("Агент", "share_module", "загружен"),
                ("Сеть", "social_module", "загружен"),
                ("Сеть", "collab_module", "загружен"),
                ("Агент", "wget_module", "загружен"),
                ("Данные", "modules.stats_recommend", "загружен"),
                ("Данные", "modules.education_module", "загружен"),
                ("Интерфейс", "extension_system", "загружен"),
                ("Интерфейс", "plugin_system", "загружен"),
                ("Умный дом", "ha_web_panel_module", "загружен"),
                ("AI", "aiengine", "загружен"),
            ]
            for cat, mod_name, ok_detail in check_mods:
                try:
                    m = importlib.import_module(mod_name)
                    add(cat, mod_name, True, ok_detail, getattr(m, "VERSION", "") or getattr(m, "__version__", ""))
                except Exception as e:
                    swallow.note('admin._collect:L1885', e)
                    add(cat, mod_name, False, str(e)[:120])

            # Каталоги данных
            for cat, name, path in [
                ("Данные", "Заметки", getattr(server, "NOTES_DIR", "")),
                ("Данные", "Чаты", getattr(server, "CHAT_DIR", "")),
                ("Данные", "Память", getattr(server, "MEMORY_DIR", "")),
                ("Данные", "Вложения", getattr(server, "ATTACHMENTS_DIR", "")),
                ("Книги", "Каталог книг", getattr(server, "BOOKS_DIR", "")),
                ("Медиа", "Музыка", ", ".join(getattr(server, "MUSIC_DIRS", []) or [])),
                ("Медиа", "Видео", ", ".join(getattr(server, "VIDEO_DIRS", []) or [])),
            ]:
                add(cat, name, bool(path and os.path.isdir(path)),
                    path or "не настроен", version="")

            # Службы
            try:
                tasks = worker_pool.get_all_tasks()
                act = sum(1 for t in tasks.values() if t.get("status") in ("running", "pending"))
                add("Службы", "Worker pool", True, f"задач: {len(tasks)}, активных: {act}")
            except Exception as e:
                swallow.note('admin._collect:L1907', e)
                add("Службы", "Worker pool", False, str(e)[:100])
            try:
                from wget_module import list_downloads
                dl = list_downloads() or []
                add("Службы", "Загрузки (wget)", True, f"активных: {len(dl)}")
            except Exception as e:
                swallow.note('admin._collect:L1914', e)
                add("Службы", "Загрузки (wget)", False, str(e)[:100])
            try:
                import docker_module as _dm
                _dst = _dm.get_status()
                _auto = _dm.autostart_enabled()
                add("Службы", "Docker (модуль)", True,
                    f"автозапуск: {'вкл' if _auto else 'выкл'} · UI: /docker (порт {_dst.get('inner_port', '?')})")
            except Exception as e:
                swallow.note('admin._collect:L1923', e)
                add("Службы", "Docker (модуль)", False, str(e)[:100])
            try:
                dl = server.DM.scan_system_apps() or []
                add("Приложения", "Системные приложения", True, f"приложений: {len(dl) if isinstance(dl, list) else 0}")
            except Exception as e:
                swallow.note('admin._collect:L1929', e)
                add("Приложения", "Системные приложения", False, str(e)[:100])
            try:
                sessions = server.DM.get_system_app_sessions()
                add("Приложения", "Сессии приложений", True, f"активных: {len(sessions)}")
            except Exception as e:
                swallow.note('admin._collect:L1935', e)
                add("Приложения", "Сессии приложений", False, str(e)[:100])
            # Книги
            books_path = getattr(server, "BOOKS_DB_PATH", "") or ""
            books_dir = getattr(server, "BOOKS_DIR", "")
            books_ok = bool((books_path and os.path.exists(books_path)) or (books_dir and os.path.isdir(books_dir)))
            add("Книги", "Библиотека", books_ok,
                books_path if books_ok else "не настроена", version="")
            # Индексация
            try:
                idx = assets_index.get_index_status()
                status = "строится..." if idx.get("running") else ("готово" if not idx.get("error") else "ошибка")
                add("Поиск", "Индексация", not idx.get("running") and not idx.get("error"), status,
                    version="")
            except Exception as e:
                swallow.note('admin._collect:L1950', e)
                add("Поиск", "Индексация", False, str(e)[:100])

            return {"modules": modules}

        # Снимок держим 5 с и обновляем в фоне: сборка трогает LLM-бэкенд,
        # hermes, docker и wget (до ~6 с на холодную). Подпись флагов в ключе
        # означает, что после переключения модуля панель видит новое состояние
        # сразу, а не через TTL.
        key = "modules:%r" % (_modules_sig(),)
        return await _run_sync(_ADMIN_MODULES_CACHE.get_or_set_swr, key, _collect,
                               None, 300.0)
    except Exception as e:
        logger.error(f"/api/admin/modules error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Консоль ошибок ----


@router.get("/api/admin/errors")
async def admin_errors(request: Request, lines: int = Query(5000, le=20000)):
    """Только ERROR/WARNING + сводка за сутки."""
    try:
        if not _admin_ok(request):
            return _auth_error()

        def _collect():
            import re
            log_file = getattr(server, "_log_file", "")
            errors = []
            counts = {"ERROR": 0, "WARNING": 0}
            total = 0
            if log_file and os.path.exists(log_file):
                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        for line in f.readlines()[-lines:]:
                            m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)$", line.rstrip("\n"))
                            if m:
                                level = m.group(2).upper()
                                if level in ("ERROR", "WARNING"):
                                    counts[level] += 1
                                    errors.append({"ts": m.group(1), "level": level, "msg": m.group(3)[:500]})
                                total += 1
                            elif line.strip():
                                total += 1
                except Exception as e:
                    errors.append({"ts": "", "level": "ERROR", "msg": f"Ошибка чтения лога: {e}"})
            return {"errors": errors[::-1], "counts": counts, "total_lines": total}

        return await _run_sync(_collect)
    except Exception as e:
        logger.error(f"/api/admin/errors error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/log/download")
async def admin_log_download(request: Request):
    """Скачать файл лога."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        log_file = getattr(server, "_log_file", "")
        if not log_file or not os.path.exists(log_file):
            return JSONResponse({"error": "Лог не найден"}, status_code=404)
        from fastapi.responses import StreamingResponse
        import io

        def _iter():
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                yield from f

        return StreamingResponse(_iter(), media_type="text/plain",
                                 headers={"Content-Disposition": "attachment; filename=server.log"})
    except Exception as e:
        logger.error(f"/api/admin/log/download error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------- приложения
# Обновление defaultapps по официальным GitHub-релизам. Сами задания идут в
# фоновом потоке apps_updater; эти роуты только запускают их и отдают прогресс.

@router.get("/api/admin/apps/state")
async def admin_apps_state(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import apps_updater
        return await _run_sync(apps_updater.state)
    except Exception as e:
        logger.error(f"/api/admin/apps/state error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/apps/check")
def admin_apps_check(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        import apps_updater
        ok, err = apps_updater.start_check()
        return {"ok": ok, "error": err}
    except Exception as e:
        logger.error(f"/api/admin/apps/check error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/apps/update")
async def admin_apps_update(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list):
            return JSONResponse({"error": "Ожидается список ids"}, status_code=400)
        import apps_updater
        ok, err = apps_updater.start_update(ids, bool(body.get("force")))
        return {"ok": ok, "error": err}
    except Exception as e:
        logger.error(f"/api/admin/apps/update error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/apps/settings")
async def admin_apps_settings(request: Request):
    """Флаги автообновления defaultapps (все выключены по умолчанию)."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "Ожидается JSON-объект"}, status_code=400)
        import apps_updater
        return await _run_sync(apps_updater.save_settings, body)
    except Exception as e:
        logger.error(f"/api/admin/apps/settings error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/apps/flags")
async def admin_apps_flags(request: Request):
    """Флаги отдельного приложения: system_only и no_auto_install."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "Ожидается JSON-объект"}, status_code=400)
        app_id = str(body.get("id") or "").strip()
        if not app_id:
            return JSONResponse({"error": "Не указан id приложения"}, status_code=400)
        import apps_flags
        try:
            res = await _run_sync(
                apps_flags.set_flags, app_id,
                body.get("system_only"), body.get("no_auto_install"),
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        try:
            import main as _main
            _main._apps_cache_invalidate()
        except Exception:
            swallow.note('admin.admin_apps_flags:L2115')
            pass
        return res
    except Exception as e:
        logger.error(f"/api/admin/apps/flags error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/apps/notify")
async def admin_apps_notify(request: Request):
    """Отправить в ленту /desktop уведомления о доступных обновлениях."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        import apps_updater
        res = await _run_sync(apps_updater.notify_updates)
        if not res.get("ok"):
            return JSONResponse(res, status_code=500)
        return res
    except Exception as e:
        logger.error(f"/api/admin/apps/notify error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/apps/add")
async def admin_apps_add(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "Ожидается JSON-объект"}, status_code=400)
        import apps_updater
        ok, err = apps_updater.start_add(body.get("url", ""), body.get("id", ""))
        return {"ok": ok, "error": err}
    except Exception as e:
        logger.error(f"/api/admin/apps/add error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

# ── Уведомления по расписанию (админ-вкладка) ──────────────────────────────
# Хранение и планировщик — modules/notification_scheduler.py. Отправленные
# уведомления попадают в общий notification_engine и видны на /desktop.

@router.get("/api/admin/notifications")
async def admin_notifications_list(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        from modules import notification_scheduler as ns
        return {"items": ns.list_schedules()}
    except Exception as e:
        logger.error(f"/api/admin/notifications list: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/notifications")
async def admin_notifications_create(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        from modules import notification_scheduler as ns
        return {"ok": True, "item": ns.create_schedule(body)}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"/api/admin/notifications create: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/api/admin/notifications/{schedule_id}")
async def admin_notifications_update(schedule_id: str, request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        from modules import notification_scheduler as ns
        return {"ok": True, "item": ns.update_schedule(schedule_id, body)}
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"/api/admin/notifications update: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/api/admin/notifications/{schedule_id}")
async def admin_notifications_delete(schedule_id: str, request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        from modules import notification_scheduler as ns
        return {"ok": ns.delete_schedule(schedule_id)}
    except Exception as e:
        logger.error(f"/api/admin/notifications delete: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/notifications/{schedule_id}/send-now")
async def admin_notifications_send_now(schedule_id: str, request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        from modules import notification_scheduler as ns
        return {"ok": True, "item": ns.send_now(schedule_id)}
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.error(f"/api/admin/notifications send-now: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── FAQ панели уведомлений на /desktop ─────────────────────────────────────

@router.get("/api/admin/faq")
async def admin_faq_get(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        from modules import vault_store
        return {"html": vault_store.load_faq(), "default": vault_store.DEFAULT_FAQ}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/api/admin/faq")
async def admin_faq_put(request: Request):
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        from modules import vault_store
        return {"ok": True, "html": vault_store.save_faq(body.get("html", ""))}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Чистка битого медиа: «включить всем» и расписание автозапуска ──────────

@router.get("/api/admin/media/autoclean")
async def admin_autoclean_state(request: Request):
    """Сколько профилей пускают чистку, какой период, когда следующий прогон."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        import media_autoclean as AC
        out = await _run_sync(AC.auto_status)
        return {"ok": True, **out}
    except Exception as e:
        logger.error(f"/api/admin/media/autoclean error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/media/autoclean/enable_all")
async def admin_autoclean_enable_all(request: Request):
    """Флаг чистки сразу всем профилям — ради этого кнопка и заведена.

    Свой флаг пользователь включает сам (routes/legacy_media_player.py). «Всем»
    — админское решение: оно разрешает разрушающее удаление в каждой библиотеке
    и поднимает ночной автозапуск, поэтому право проверяется до первой записи.
    """
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        value = bool(body.get("enabled"))
        import media_autoclean as AC
        import module_flags

        def _apply():
            import profiles_module as PM
            done, failed = [], []
            for p in PM.list_profiles():
                pid = str(p.get("id") or "").strip()
                if not pid:
                    continue
                try:
                    module_flags.set_flag_for(AC.MODULE, "enabled", value, pid)
                    done.append(pid)
                except Exception as e:
                    logger.warning("Чистка битого: флаг не записан %s: %s", pid, e)
                    failed.append({"profile": pid, "error": str(e)[:160]})
            if not done:
                raise RuntimeError("Профили не найдены — флаг некому ставить")
            return done, failed

        done, failed = await _run_sync(_apply)
        if value:
            # Поток планировщика поднимается на старте; вызов повторяемый, и он
            # же страхует случай «флаг включили до перезапуска с планировщиком».
            AC.start_scheduler()
        return {"ok": True, "enabled": value, "profiles": done,
                "failed": failed, "state": await _run_sync(AC.auto_status)}
    except Exception as e:
        logger.error(f"/api/admin/media/autoclean/enable_all error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/media/autoclean/interval")
async def admin_autoclean_interval(request: Request):
    """Период автозапуска в часах; следующий прогон отсчитывается от сохранения."""
    try:
        if not _admin_ok(request):
            return _auth_error()
        body = await request.json()
        import media_autoclean as AC
        hours = AC.set_interval_hours(body.get("hours"))
        next_ts = AC.reschedule()
        return {"ok": True, "hours": hours, "next_run": next_ts}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"/api/admin/media/autoclean/interval error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Обновление Hermes (GitHub Releases) ----
# Апдейтер исполняется в процессе web UI агента: там живут скачивание,
# установка и резервные копии. Для пользователя его единственная поверхность —
# админка, поэтому страница агента карточку не показывает, а эти маршруты
# проксируют запросы в web UI по loopback. Проверка админ-сессии здесь своя:
# web UI принимает только запросы с этой машины и про роли профилей не знает.

_HU_TIMEOUTS = {
    "check": 120.0,
    "download": 1800.0,
    "install": 1800.0,
    "rollback": 900.0,
}


def _hermes_ui_base():
    from hermes_module.integration import launch
    return "http://127.0.0.1:%d" % launch.current_ui_port()


async def _hermes_update_proxy(method, path, body=None, timeout=20.0):
    import httpx
    url = _hermes_ui_base() + path
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.request(method, url, json=body)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[hermes-update] web UI недоступен: {e}")
        return JSONResponse(
            {"error": "Интерфейс агента не запущен — включите модуль или "
                      "перезапустите приложение"}, status_code=503)
    try:
        data = resp.json()
    except Exception:
        swallow.note('admin._hermes_update_proxy')
        data = {"error": (resp.text or "пустой ответ интерфейса агента")[:300]}
    return JSONResponse(data, status_code=resp.status_code)


async def _hermes_update_body(request):
    try:
        body = await request.json()
    except Exception:
        swallow.note('admin._hermes_update_body')
        return {}
    return body if isinstance(body, dict) else {}


@router.get("/api/admin/hermes-update")
async def admin_hermes_update_state(request: Request):
    if not _admin_ok(request):
        return _auth_error()
    return await _hermes_update_proxy("GET", "/api/hermes-update")


@router.post("/api/admin/hermes-update/config")
async def admin_hermes_update_config(request: Request):
    if not _admin_ok(request):
        return _auth_error()
    return await _hermes_update_proxy("POST", "/api/hermes-update/config",
                                      await _hermes_update_body(request))


@router.post("/api/admin/hermes-update/check")
async def admin_hermes_update_check(request: Request):
    if not _admin_ok(request):
        return _auth_error()
    return await _hermes_update_proxy(
        "POST", "/api/hermes-update/check", {},
        timeout=_HU_TIMEOUTS["check"])


@router.post("/api/admin/hermes-update/download")
async def admin_hermes_update_download(request: Request):
    if not _admin_ok(request):
        return _auth_error()
    return await _hermes_update_proxy(
        "POST", "/api/hermes-update/download",
        await _hermes_update_body(request), timeout=_HU_TIMEOUTS["download"])


@router.post("/api/admin/hermes-update/install")
async def admin_hermes_update_install(request: Request):
    if not _admin_ok(request):
        return _auth_error()
    return await _hermes_update_proxy(
        "POST", "/api/hermes-update/install",
        await _hermes_update_body(request), timeout=_HU_TIMEOUTS["install"])


@router.post("/api/admin/hermes-update/rollback")
async def admin_hermes_update_rollback(request: Request):
    if not _admin_ok(request):
        return _auth_error()
    return await _hermes_update_proxy(
        "POST", "/api/hermes-update/rollback",
        await _hermes_update_body(request), timeout=_HU_TIMEOUTS["rollback"])


@router.post("/api/admin/hermes-update/gateway/start")
async def admin_hermes_update_gateway_start(request: Request):
    """Поднять шлюз после установки/отката — апдейтер его останавливает.

    В desktop-режиме шлюзом владеет main.py, поэтому старт идёт тем же
    launch.start_gateway(), что и у движковых маршрутов, а не через web UI.
    """
    if not _admin_ok(request):
        return _auth_error()
    from hermes_module.integration import launch
    try:
        proc = await asyncio.to_thread(launch.start_gateway)
    except Exception as e:  # noqa: BLE001
        logger.error(f"/api/admin/hermes-update/gateway/start error: {e}")
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)
    st = {}
    deadline = time.time() + 25
    while True:
        st = await asyncio.to_thread(launch.health)
        if st.get("gateway_http") or time.time() >= deadline:
            break
        await asyncio.sleep(1)
    return {"ok": bool(st.get("gateway_http")),
            "pid": proc.pid if proc is not None else None,
            "running": bool(st.get("gateway_running")),
            "http": bool(st.get("gateway_http")),
            "error": None if st.get("gateway_http") else "шлюз не ответил на /health"}
