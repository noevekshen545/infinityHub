"""Profiles API routes — вынесены из main.py."""

import os, io, json, asyncio, logging, shutil, tempfile, zipfile
from fastapi import APIRouter, Request, Query, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

import server
import worker_pool
from modules import profile_state
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()


def _run_sync(fn, *args, **kw):
    return worker_pool.run_in_pool(worker_pool.HIGH_POOL, fn, *args, **kw)


ADMIN_ROLE = "admin"


def _caller_id(request):
    """ID профиля вызывающего из cookie запроса (БЕЗ переключения профиля).

    Cookie подделываема (в системе нет полноценной аутентификации), но guard
    соответствует архитектуре: пароли при переключении проверяются так же.
    Закрывает случайные удаления/клонирование чужих профилей через UI.
    """
    try:
        import profiles_module as PM
        cookie_val = request.cookies.get(PM.COOKIE_NAME, "") or ""
        return PM.resolve_profile_id(cookie_val)
    except Exception:
        swallow.note('profiles._caller_id:L35')
        return None


def _profile_role(profile_id):
    """Роль профиля из индекса (или пустая строка)."""
    if not profile_id:
        return ""
    try:
        import profiles_module as PM
        for p in PM._index_snapshot().get("profiles", []):
            if p.get("id") == profile_id:
                return (p.get("role") or "").strip().lower()
    except Exception:
        swallow.note('profiles._profile_role:L49')
        pass
    return ""


def _is_admin_profile(profile_id):
    return _profile_role(profile_id) == ADMIN_ROLE


def _has_password(profile_id):
    try:
        import profiles_module as PM
        for p in PM._index_snapshot().get("profiles", []):
            if p.get("id") == profile_id:
                return bool(p.get("password_hash", ""))
    except Exception:
        swallow.note('profiles._has_password:L65')
        pass
    return False


def _deny(msg="Недостаточно прав для действия с чужим профилем"):
    return JSONResponse({"error": msg, "forbidden": True}, status_code=403)


def _require_admin_or_self(request, profile_id, allow_no_password=False):
    """Guard: действие над profile_id разрешено админу или самому себе.

    allow_no_password — если профиль вызывающего не защищён паролем и целевой
    профиль не админский, действие разрешается (ситуация «локальный хост,
    пользователь без пароля», как было до guard'а).
    """
    caller = _caller_id(request)
    if not caller:
        return _deny("Профиль вызывающего не определён")
    if caller == profile_id:
        return None
    if _is_admin_profile(caller):
        return None
    if allow_no_password and not _has_password(caller) and not _is_admin_profile(profile_id):
        return None
    return _deny()


def _require_admin(request):
    caller = _caller_id(request)
    if not caller:
        return _deny("Профиль вызывающего не определён")
    if _is_admin_profile(caller):
        return None
    return _deny()


def _require_caller(request):
    """Guard для операций, создающих новый профиль.

    Раньше /create, /switch и /import не проверяли вызывающего вовсе:
    запрос без cookie создавал профили и переключал активный профиль
    безусловно. Требуем, чтобы профиль вызывающего определялся.
    """
    if not _caller_id(request):
        return _deny("Профиль вызывающего не определён")
    return None


def _update_globals_from_cfg(_cfg):
    """Применить конфиг к глобалам сервера.

    Раньше здесь лежала копия разбора config.json, и она разошлась с
    server.apply_profile_globals: не добавляла профильный apps_dir и
    default_apps_dir, поэтому find_apps сканировал пустой набор каталогов.
    Держим один источник истины.
    """
    try:
        profile_state.apply_profile_globals(_cfg=_cfg)
    except Exception as e:
        logger.error(f"apply_profile_globals: {e}")


def _apply_paths_live(profile_id):
    """Перечитать конфиг профиля и применить пути к рантайму (без перезапуска)."""
    try:
        from modules.profile_state import apply_paths_live
        apply_paths_live(profile_id=profile_id)
        return True
    except Exception as e:
        logger.error(f"apply_paths_live({profile_id}): {e}")
        return False


@router.get("/api/profiles")
async def api_profiles_get(request: Request):
    try:
        import profiles_module
        cookie_val = request.cookies.get(profiles_module.COOKIE_NAME, "") or ""
        req_profile = await _run_sync(profiles_module.resolve_profile_id, cookie_val)
        caller = _caller_id(request)
        caller_admin = _is_admin_profile(caller)
        def _list():
            result = []
            for p in profiles_module.list_profiles():
                p = dict(p)
                p["is_active"] = p["id"] == req_profile
                # Управлять (удалить/переименовать/клонировать/выгрузить)
                # можно своим профилем; чужими — только профилю с ролью admin
                p["is_caller"] = p["id"] == caller
                p["can_manage"] = bool(caller) and (caller_admin or p["id"] == caller)
                result.append(p)
            return result
        profiles = await _run_sync(_list)
        return {"profiles": profiles, "active_profile": req_profile,
                "caller_id": caller, "caller_is_admin": caller_admin}
    except Exception as e:
        logger.error(f"/api/profiles error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/profiles/create")
async def api_profiles_create(request: Request):
    try:
        import profiles_module
        guard = _require_caller(request)
        if guard:
            return guard
        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            return JSONResponse({"error": "Имя профиля не может быть пустым"}, status_code=400)
        def _create():
            from config_module import get_config
            from modules import public_view
            result = profiles_module.create_profile(name, from_config=get_config())
            # Новый профиль по умолчанию не видит данные общедоступного —
            # галка включается сама в его настройках.
            public_view.seed_profile((result or {}).get("id"))
            return result
        result = await _run_sync(_create)
        return {"ok": True, "profile": result}
    except Exception as e:
        logger.error(f"/api/profiles/create error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/profiles/public-data")
async def api_profiles_public_data(request: Request):
    """Галки «показывать данные общедоступного профиля» у вызывающего профиля.

    Отдельный маршрут, а не поле /api/config: тот собирает белый список ключей,
    и добавлять в него приходится правкой, которой здесь нет.
    """
    try:
        import profiles_module as PM
        from modules import public_view
        pid = _caller_id(request)
        if not pid:
            return JSONResponse({"error": "Профиль вызывающего не определён"}, status_code=403)
        return {"profile_id": pid,
                "is_public": bool(PM.is_public_profile(pid)),
                "categories": list(public_view.CATEGORIES),
                "flags": public_view.flags_for(pid)}
    except Exception as e:
        logger.error(f"/api/profiles/public-data error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/profiles/switch")
async def api_profiles_switch(request: Request):
    try:
        import profiles_module
        guard = _require_caller(request)
        if guard:
            return guard
        body = await request.json()
        profile_id = body.get("id", "")
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        # Обход пароля при переключении закрыт: на защищённый профиль
        # переключиться без пароля может только админ (иначе —
        # switch-with-state, где пароль проверяется).
        if _has_password(profile_id):
            caller = _caller_id(request)
            if caller != profile_id and not _is_admin_profile(caller):
                return _deny("Профиль защищён паролем — используйте вход по паролю")
        def _switch():
            if not profiles_module.switch_profile(profile_id):
                return False
            profile_state.apply_profile_globals(profile_id=profile_id)
            return True
        ok = await _run_sync(_switch)
        if not ok:
            return JSONResponse({"error": "Профиль не найден"}, status_code=404)
        resp = JSONResponse({"ok": True, "profile_id": profile_id})
        resp.set_cookie(profiles_module.COOKIE_NAME, profile_id, path="/", max_age=31536000,
                        httponly=True, samesite="lax")
        return resp
    except Exception as e:
        logger.error(f"/api/profiles/switch error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/profiles/rename")
async def api_profiles_rename(request: Request):
    try:
        import profiles_module
        body = await request.json()
        profile_id = body.get("id", "")
        name = body.get("name", "").strip()
        if not profile_id or not name:
            return JSONResponse({"error": "Не указан ID или имя"}, status_code=400)
        guard = _require_admin_or_self(request, profile_id)
        if guard:
            return guard
        ok = await _run_sync(profiles_module.rename_profile, profile_id, name)
        if ok:
            return {"ok": True}
        return JSONResponse({"error": "Профиль не найден"}, status_code=404)
    except Exception as e:
        logger.error(f"/api/profiles/rename error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/profiles/delete")
async def api_profiles_delete(request: Request):
    try:
        import profiles_module
        body = await request.json()
        profile_id = body.get("id", "")
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        # allow_no_password: локальный пользователь без пароля вправе
        # удалить профиль, который он сам создал. Флаг уже был заведён в
        # _require_admin_or_self именно для этой ситуации, но в delete
        # не использовался — из-за чего очистить свой профиль было
        # невозможно (см. комментарий про тупик ниже).
        guard = _require_admin_or_self(request, profile_id,
                                       allow_no_password=True)
        if guard:
            return guard
        # Раньше удаление профиля было невозможно из интерфейса:
        #   • delete_profile() отказывает, если профиль активен;
        #   • guard не даёт удалить чужой (а после возврата на исходный
        #     профиль удаляемый становится «чужим»).
        # Получался тупик: свой профиль удалить нельзя никак.
        # Если удаляем СВОЙ и он активен — сначала уходим на другой.
        if _caller_id(request) == profile_id:
            index = await _run_sync(profiles_module._load_index)
            if index.get("active") == profile_id:
                others = [p["id"] for p in index.get("profiles", [])
                          if p["id"] != profile_id and
                          not p.get("blocked", False)]
                if not others:
                    return JSONResponse({"error": "Это единственный профиль"},
                                        status_code=400)
                if not await _run_sync(profiles_module.switch_profile, others[0]):
                    return JSONResponse({"error": "Не удалось переключиться"},
                                        status_code=500)
                profile_state.apply_profile_globals(profile_id=others[0])
        ok, msg = await _run_sync(profiles_module.delete_profile, profile_id)
        if ok:
            return {"ok": True}
        return JSONResponse({"error": msg}, status_code=400)
    except Exception as e:
        logger.error(f"/api/profiles/delete error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/profiles/load-state")
async def api_profiles_load_state(request: Request):
    try:
        import profiles_module
        cookie_profile = request.cookies.get(profiles_module.COOKIE_NAME, "") or ""
        req_profile = await _run_sync(profiles_module.resolve_profile_id, cookie_profile)
        state = await _run_sync(profiles_module.load_frontend_state, req_profile)

        def _name():
            for p in profiles_module.list_profiles():
                if p.get("id") == req_profile:
                    return p.get("name", "")
            return ""

        name = await _run_sync(_name)
        return {"frontend_state": state, "profile_id": req_profile, "profile_name": name}
    except Exception as e:
        logger.error(f"/api/profiles/load-state error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/profiles/save-state")
async def api_profiles_save_state(request: Request):
    try:
        import profiles_module
        body = await request.json()
        frontend_state = body.get("frontend_state", {})
        # Страница присылает профиль, под которым была загружена. После
        # переключения cookie уже указывает на новый профиль, а localStorage
        # вкладки — ещё на старый; без этой проверки beforeunload-отправка
        # записывала имя и папки приложений старого профиля в новый.
        claimed = str(body.get("profile_id") or "").strip()
        cookie_profile = request.cookies.get(profiles_module.COOKIE_NAME, "") or ""
        req_profile = await _run_sync(profiles_module.resolve_profile_id, cookie_profile)
        if claimed and req_profile and claimed != req_profile:
            return {"ok": True, "saved": False, "profile_id": req_profile}
        if req_profile:
            await _run_sync(profiles_module.save_frontend_state, req_profile, frontend_state)
        else:
            await _run_sync(profiles_module.save_active_frontend_state, frontend_state)
        return {"ok": True, "saved": True, "profile_id": req_profile}
    except Exception as e:
        logger.error(f"/api/profiles/save-state error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/profiles/set-password")
async def api_profiles_set_password(request: Request):
    try:
        import profiles_module
        body = await request.json()
        profile_id = body.get("id", "")
        password = body.get("password", "")
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        password = str(password or "")
        if password and len(password) < 4:
            return JSONResponse({"error": "Пароль должен содержать минимум 4 символа"},
                                status_code=400)
        guard = _require_admin_or_self(request, profile_id)
        if guard:
            return guard
        ok = await _run_sync(profiles_module.set_profile_password, profile_id, password)
        if ok:
            return {"ok": True}
        return JSONResponse({"error": "Профиль не найден"}, status_code=404)
    except Exception as e:
        logger.error(f"/api/profiles/set-password error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/profiles/verify-password")
async def api_profiles_verify_password(request: Request):
    try:
        import profiles_module
        body = await request.json()
        profile_id = body.get("id", "")
        password = body.get("password", "")
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        ok = await _run_sync(profiles_module.verify_profile_password, profile_id, password)
        return {"ok": ok}
    except Exception as e:
        logger.error(f"/api/profiles/verify-password error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/profiles/private-paths")
async def api_profiles_private_paths(profile_id: str = "", request: Request = None):
    try:
        import profiles_module
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        guard = _require_admin_or_self(request, profile_id)
        if guard:
            return guard
        paths = await _run_sync(profiles_module.get_profile_private_paths, profile_id)
        return {
            "private_paths": paths,
            "public_paths": await _run_sync(profiles_module.get_public_paths),
            "public_paths_enabled": profiles_module.get_public_flags(profile_id),
            "is_public_profile": profiles_module.is_public_profile(profile_id),
        }
    except Exception as e:
        logger.error(f"/api/profiles/private-paths error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/profiles/save-private-paths")
async def api_profiles_save_private_paths(request: Request):
    try:
        import profiles_module
        body = await request.json()
        profile_id = body.get("profile_id", "") or body.get("id", "")
        private_paths = body.get("private_paths", {})
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)
        guard = _require_admin_or_self(request, profile_id)
        if guard:
            return guard
        ok = await _run_sync(profiles_module.set_profile_private_paths, profile_id, private_paths)
        if not ok:
            return JSONResponse({"error": "Профиль не найден"}, status_code=404)
        if "public_paths_enabled" in body:
            # Форма может прислать и общий bool, и набор {категория: bool}.
            await _run_sync(profiles_module.set_public_paths_enabled, profile_id,
                            body.get("public_paths_enabled"))
        # Файл профиля сохранён — рантайм перечитываем, только если это и есть
        # активный профиль: иначе пути второго профиля легли бы в текущий.
        applied = False
        if profile_id == profiles_module.get_active_id():
            applied = await _run_sync(_apply_paths_live, profile_id)
        return {"ok": True, "applied_live": applied}
    except Exception as e:
        logger.error(f"/api/profiles/save-private-paths error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/profiles/switch-with-state")
async def api_profiles_switch_with_state(request: Request):
    try:
        import profiles_module
        body = await request.json()
        profile_id = body.get("id", "")
        frontend_state = body.get("frontend_state", {})
        if not profile_id:
            return JSONResponse({"error": "Не указан ID профиля"}, status_code=400)

        def _switch_with_state():
            password = body.get("password", "")
            new_state = profiles_module.switch_profile_with_state(profile_id, frontend_state, password)
            if new_state is None:
                return {"error": "not_found"}
            if isinstance(new_state, dict) and new_state.get("error") == "wrong_password":
                return {"error": "wrong_password"}
            profile_state.apply_profile_globals(profile_id=profile_id)
            return {"ok": True, "frontend_state": new_state}

        result = await _run_sync(_switch_with_state)
        if result.get("error") == "not_found":
            return JSONResponse({"error": "Профиль не найден"}, status_code=404)
        if result.get("error") == "wrong_password":
            return JSONResponse({"error": "Неверный пароль"}, status_code=403)
        if result.get("error") == "blocked":
            return JSONResponse({"error": "Профиль заблокирован"}, status_code=403)
        resp = JSONResponse(result)
        resp.set_cookie(profiles_module.COOKIE_NAME, profile_id, path="/", max_age=31536000,
                        httponly=True, samesite="lax")
        # Дизайн — часть профиля; общую cookie сразу переводим на значение
        # нового профиля, иначе / после переключения откроется чужим шаблоном.
        _design = (result.get("frontend_state") or {}).get("page_design") or ""
        if _design:
            resp.set_cookie("page_design", _design, max_age=31536000,
                            path="/", samesite="lax")
        else:
            resp.delete_cookie("page_design", path="/")
        return resp
    except Exception as e:
        logger.error(f"/api/profiles/switch-with-state error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _safe_extract_zip(zf, dest):
    """Распаковать zip без zip-slip: только пути внутри dest.

    ZipFile.extractall в старых версиях Python пропускал `..\\` и абсолютные
    пути — архив мог записать файл куда угодно. Проверяем каждый элемент.
    """
    dest_real = os.path.realpath(dest)
    sep = dest_real + os.sep
    for member in zf.infolist():
        name = (member.filename or "").replace("\\", "/")
        if (not name or name.startswith("/")
                or (len(name) > 1 and name[1] == ":")
                or name.startswith("//")):
            raise ValueError("Недопустимый путь в архиве: %s" % member.filename)
        target = os.path.realpath(os.path.join(dest_real, name))
        if target != dest_real and not target.startswith(sep):
            raise ValueError("Недопустимый путь в архиве: %s" % member.filename)
    zf.extractall(dest_real)


@router.get("/api/profiles/export")
def api_profiles_export(request: Request, id: str = Query(...)):
    """Экспорт профиля в zip (данные без пароля)."""
    try:
        import profiles_module
        if not profiles_module.profile_exists(id):
            return JSONResponse({"error": "Профиль не найден"}, status_code=404)
        # Выгружать (со всеми данными) можно свой профиль; чужой — только админ
        guard = _require_admin_or_self(request, id)
        if guard:
            return guard
        src = profiles_module._profile_path(id)
        if not os.path.isdir(src):
            return JSONResponse({"error": "Каталог профиля не найден"}, status_code=404)
        # Пишем во временный файл, а не в память: профиль с медиа/кэшами
        # может весить гигабайты, BytesIO держал всё это в RAM.
        fd, tmp_path = tempfile.mkstemp(prefix="profile_export_", suffix=".zip")
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _dirs, files in os.walk(src):
                    for fn in files:
                        fp = os.path.join(root, fn)
                        try:
                            zf.write(fp, os.path.relpath(fp, src))
                        except Exception:
                            swallow.note('profiles.api_profiles_export:L543')
                            pass
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                swallow.note('profiles.api_profiles_export:L549')
                pass
            raise

        def _cleanup():
            try:
                os.remove(tmp_path)
            except OSError:
                swallow.note('profiles._cleanup:L557')
                pass

        return StreamingResponse(
            open(tmp_path, "rb"),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{id}.zip"'},
            background=BackgroundTask(_cleanup),
        )
    except Exception as e:
        logger.error(f"/api/profiles/export error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/profiles/import")
async def api_profiles_import(request: Request, file: UploadFile = File(...), name: str = Form(None)):
    """Импорт профиля из zip (пароль не переносится)."""
    try:
        import profiles_module
        guard = _require_caller(request)
        if guard:
            return guard
        raw = await file.read()
        if not raw:
            return JSONResponse({"error": "Пустой файл"}, status_code=400)
        display = (name or file.filename or "Импортированный").strip()[:60] or "Импортированный"

        # распаковка zip и перенос дерева профиля — ввод пользователя пришёл,
        # дальше можно блокировать только рабочий поток, не event loop
        def _do_import():
            created = profiles_module.create_profile(display)
            pid = created["id"]
            dst_dir = profiles_module._profile_path(pid)
            tmp = tempfile.mkdtemp(prefix="profile_import_")
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    _safe_extract_zip(zf, tmp)
                for entry in os.listdir(tmp):
                    s = os.path.join(tmp, entry)
                    d = os.path.join(dst_dir, entry)
                    try:
                        if os.path.isdir(s):
                            shutil.copytree(s, d, dirs_exist_ok=True)
                        else:
                            shutil.copy2(s, d)
                    except Exception:
                        swallow.note('profiles._do_import:L603')
                        pass
                profiles_module._redirect_profile_config_file(pid)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            # В архиве ключа мог не быть (профиль создан до этой настройки) —
            # импорт не должен включать показ общедоступных данных сам.
            from modules import public_view
            public_view.seed_profile(pid)
            return created

        try:
            created = await asyncio.to_thread(_do_import)
        except zipfile.BadZipFile:
            return JSONResponse({"error": "Файл не является корректным zip-архивом"},
                                status_code=400)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"ok": True, "profile": created}
    except Exception as e:
        logger.error(f"/api/profiles/import error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/profiles/clone")
async def api_profiles_clone(request: Request):
    """Клонировать профиль (данные + настройки, без пароля)."""
    try:
        import profiles_module
        body = await request.json()
        src_id = body.get("id", "")
        new_name = (body.get("name") or "").strip()
        if not src_id or not profiles_module.profile_exists(src_id):
            return JSONResponse({"error": "Профиль не найден"}, status_code=404)
        # Клонировать можно свой профиль; чужой — только админ
        guard = _require_admin_or_self(request, src_id)
        if guard:
            return guard
        # дерево профиля копируется файловой системой — в отдельный поток
        created, err = await asyncio.to_thread(
            profiles_module.clone_profile, src_id, new_name or None)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True, "profile": created}
    except Exception as e:
        logger.error(f"/api/profiles/clone error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
