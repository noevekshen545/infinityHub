"""Notes API, перенесённое из legacy-лестницы.

Здесь только те маршруты, которых не было в FastAPI: дерево, версии,
комментарии, вложения, плагины заметок — 26 веток /api/notes/*. Логика
не переписана: вызываются те же функции server_backup, что и из моста,
поэтому формат ответов не меняется.
"""

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from .compat_helpers import auth_denied, body_or_error, j, q1
import profile_paths
import swallow

# Версии и комментарии теперь лежат в каталоге профиля; без bind() каталог
# берётся из «активного профиля сервера», и запрос с чужим cookie читал бы чужое.
router = APIRouter(dependencies=[Depends(profile_paths.bind)])


def _err(S, where, e):
    S.logger.error(f"{where} error: {e}", exc_info=True)
    return j({"error": str(e)}, 500)


@router.get("/api/notes/attachments-all")
async def notes_attachments_all(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    try:
        return j(await run_in_threadpool(S.notes_list_all_attachments))
    except Exception as e:
        swallow.note('compat_notes.notes_attachments_all:L34', e)
        return _err(S, "/api/notes/attachments-all", e)


@router.get("/api/notes/attachment-text")
async def notes_attachment_text(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    nid = q1(request, "note", "")
    fname = q1(request, "file", "")
    try:
        return j(await run_in_threadpool(S.notes_get_attachment_text, nid, fname))
    except Exception as e:
        swallow.note('compat_notes.notes_attachment_text:L49', e)
        return _err(S, "/api/notes/attachment-text", e)


@router.get("/api/notes/plugins")
async def notes_plugins(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    try:
        return j(await run_in_threadpool(S.plugins_list))
    except Exception as e:
        swallow.note('compat_notes.notes_plugins:L62', e)
        return _err(S, "/api/notes/plugins", e)


@router.get("/api/notes/tree")
async def notes_tree(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    try:
        return j(await run_in_threadpool(S.notes_tree_get))
    except Exception as e:
        swallow.note('compat_notes.notes_tree:L75', e)
        return _err(S, "/api/notes/tree", e)


@router.get("/api/notes/version/list")
async def notes_version_list(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    nid = q1(request, "id", "")
    try:
        return j(await run_in_threadpool(S.notes_version_list, nid))
    except Exception as e:
        swallow.note('compat_notes.notes_version_list:L89', e)
        return _err(S, "/api/notes/version/list", e)


@router.get("/api/notes/version/get")
async def notes_version_get(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    nid = q1(request, "id", "")
    try:
        vn = int(q1(request, "v", "0"))
        return j(await run_in_threadpool(S.notes_version_get, nid, vn))
    except Exception as e:
        swallow.note('compat_notes.notes_version_get:L104', e)
        return _err(S, "/api/notes/version/get", e)


@router.get("/api/notes/comment/list")
async def notes_comment_list(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    nid = q1(request, "note", "")
    try:
        return j(await run_in_threadpool(S.notes_comment_list, nid))
    except Exception as e:
        swallow.note('compat_notes.notes_comment_list:L118', e)
        return _err(S, "/api/notes/comment/list", e)


@router.post("/api/notes/tree/create")
async def notes_tree_create(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_tree_create, body.get("name", ""),
            body.get("parent_id", None), body.get("icon", "📁")))
    except Exception as e:
        swallow.note('compat_notes.notes_tree_create:L136', e)
        return _err(S, "/api/notes/tree/create", e)


@router.post("/api/notes/tree/rename")
async def notes_tree_rename(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_tree_rename, body.get("id", ""), body.get("name", "")))
    except Exception as e:
        swallow.note('compat_notes.notes_tree_rename:L153', e)
        return _err(S, "/api/notes/tree/rename", e)


@router.post("/api/notes/tree/move")
async def notes_tree_move(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_tree_move, body.get("id", ""),
            body.get("new_parent_id", None)))
    except Exception as e:
        swallow.note('compat_notes.notes_tree_move:L171', e)
        return _err(S, "/api/notes/tree/move", e)


@router.post("/api/notes/tree/delete")
async def notes_tree_delete(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(S.notes_tree_delete, body.get("id", "")))
    except Exception as e:
        swallow.note('compat_notes.notes_tree_delete:L187', e)
        return _err(S, "/api/notes/tree/delete", e)


@router.post("/api/notes/comment/save")
async def notes_comment_save(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_comment_save, body.get("note_id", ""),
            body.get("selected_text", ""), body.get("comment", ""),
            body.get("anchor_id", "")))
    except Exception as e:
        swallow.note('compat_notes.notes_comment_save:L206', e)
        return _err(S, "/api/notes/comment/save", e)


@router.post("/api/notes/comment/update")
async def notes_comment_update(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_comment_update, body.get("note_id", ""),
            body.get("comment_id", ""), body.get("comment", "")))
    except Exception as e:
        swallow.note('compat_notes.notes_comment_update:L224', e)
        return _err(S, "/api/notes/comment/update", e)


@router.post("/api/notes/comment/delete")
async def notes_comment_delete(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_comment_delete, body.get("note_id", ""),
            body.get("comment_id", "")))
    except Exception as e:
        swallow.note('compat_notes.notes_comment_delete:L242', e)
        return _err(S, "/api/notes/comment/delete", e)


@router.post("/api/notes/rename-folder")
async def notes_rename_folder(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_rename_folder, body.get("old_name", ""),
            body.get("new_name", "")))
    except Exception as e:
        swallow.note('compat_notes.notes_rename_folder:L260', e)
        return _err(S, "/api/notes/rename-folder", e)


@router.post("/api/notes/move-folder")
async def notes_move_folder(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_move_folder, body.get("folder", ""), body.get("target", "")))
    except Exception as e:
        swallow.note('compat_notes.notes_move_folder:L277', e)
        return _err(S, "/api/notes/move-folder", e)


@router.post("/api/notes/move")
async def notes_move(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_move_article, body.get("id", ""), body.get("folder", "")))
    except Exception as e:
        swallow.note('compat_notes.notes_move:L294', e)
        return _err(S, "/api/notes/move", e)


@router.post("/api/notes/upload")
async def notes_upload(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    note_id = body.get("note_id", "")
    if "file" not in files:
        return j({"error": "Файл не передан"}, 400)
    fdata = files["file"]
    try:
        return j(await run_in_threadpool(
            S.notes_upload_attachment, note_id, fdata["filename"],
            fdata["content"]))
    except Exception as e:
        swallow.note('compat_notes.notes_upload:L316', e)
        return _err(S, "/api/notes/upload", e)


@router.post("/api/notes/delete-attachment")
async def notes_delete_attachment(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_delete_attachment, body.get("note_id", ""),
            body.get("filename", "")))
    except Exception as e:
        swallow.note('compat_notes.notes_delete_attachment:L334', e)
        return _err(S, "/api/notes/delete-attachment", e)


@router.post("/api/notes/auto-links")
async def notes_auto_links(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_auto_links, body.get("use_ai", True)))
    except Exception as e:
        swallow.note('compat_notes.notes_auto_links:L351', e)
        return _err(S, "/api/notes/auto-links", e)


@router.post("/api/notes/plugin/execute")
async def notes_plugin_execute(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    plugin_name = body.get("plugin", "")
    hook = body.get("hook", "process")
    content = body.get("content", "")
    note_data = body.get("note_data", {})
    spec = S._plugins_cache.get(plugin_name)
    if not spec or hook not in spec:
        return j({"error": f"Плагин '{plugin_name}' не найден или хук "
                           f"'{hook}' отсутствует"}, 404)
    fn = spec[hook]

    def _run():
        return fn(content, note_data) if note_data else fn(content)

    try:
        return j({"ok": True, "result": await run_in_threadpool(_run)})
    except Exception as e:
        swallow.note('compat_notes.notes_plugin_execute:L380', e)
        return _err(S, "/api/notes/plugin/execute", e)


@router.post("/api/notes/version/save")
async def notes_version_save(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_version_save, body.get("id", ""), body.get("title", ""),
            body.get("content", ""), body.get("folder", "inbox")))
    except Exception as e:
        swallow.note('compat_notes.notes_version_save:L398', e)
        return _err(S, "/api/notes/version/save", e)


@router.post("/api/notes/version/list")
async def notes_version_list_post(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    nid = q1(request, "id", "")
    try:
        return j(await run_in_threadpool(S.notes_version_list, nid))
    except Exception as e:
        swallow.note('compat_notes.notes_version_list_post:L412', e)
        return _err(S, "/api/notes/version/list", e)


@router.post("/api/notes/version/get")
async def notes_version_get_post(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import server as S
    nid = q1(request, "id", "")
    try:
        vn = int(q1(request, "v", "0"))
        return j(await run_in_threadpool(S.notes_version_get, nid, vn))
    except Exception as e:
        swallow.note('compat_notes.notes_version_get_post:L427', e)
        return _err(S, "/api/notes/version/get", e)


@router.post("/api/notes/version/restore")
async def notes_version_restore(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_version_restore, body.get("id", ""), body.get("version", 0)))
    except Exception as e:
        swallow.note('compat_notes.notes_version_restore:L444', e)
        return _err(S, "/api/notes/version/restore", e)


@router.post("/api/notes/version/delete")
async def notes_version_delete(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    try:
        return j(await run_in_threadpool(
            S.notes_version_delete, body.get("id", ""), body.get("version", 0)))
    except Exception as e:
        swallow.note('compat_notes.notes_version_delete:L461', e)
        return _err(S, "/api/notes/version/delete", e)


# ========== Быстрые заметки (мини-приложение Shift+Space) ==========
# Хранилище отдельное (quick_notes_module), чтобы короткая запись не
# открывала полный редактор. Кнопка «В заметки» переносит её в обычный
# модуль через тот же server.notes_save_article, что и /api/notes/save.

def _qn_profile(request):
    try:
        import profiles_module as PM
        pid = PM.resolve_profile_id(request.cookies.get(PM.COOKIE_NAME))
        return pid or "default"
    except Exception:
        swallow.note('compat_notes._qn_profile:L476')
        return "default"


@router.get("/api/quick-notes")
async def quick_notes_list(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    import quick_notes_module as QN
    try:
        rows = await run_in_threadpool(QN.list_notes, _qn_profile(request))
        return j({"ok": True, "notes": rows})
    except Exception as e:
        return j({"ok": False, "error": str(e), "notes": []}, 500)


@router.post("/api/quick-notes/save")
async def quick_notes_save(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import quick_notes_module as QN
    try:
        note = await run_in_threadpool(QN.save_note, _qn_profile(request), body)
        return j({"ok": True, "note": note})
    except ValueError as e:
        return j({"ok": False, "error": str(e)}, 400)
    except Exception as e:
        return j({"ok": False, "error": str(e)}, 500)


@router.post("/api/quick-notes/delete")
async def quick_notes_delete(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import quick_notes_module as QN
    note_id = str(body.get("id") or "")
    ok = await run_in_threadpool(QN.delete_note, _qn_profile(request), note_id)
    return j({"ok": bool(ok)})


@router.post("/api/quick-notes/pin")
async def quick_notes_pin(request: Request):
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import quick_notes_module as QN
    try:
        note = await run_in_threadpool(
            QN.save_note, _qn_profile(request),
            {"id": str(body.get("id") or ""), "pinned": bool(body.get("pinned"))})
        return j({"ok": True, "note": note})
    except ValueError as e:
        return j({"ok": False, "error": str(e)}, 404)


@router.post("/api/quick-notes/to-note")
async def quick_notes_to_note(request: Request):
    """Перенести быструю заметку в обычный модуль заметок."""
    deny = auth_denied(request)
    if deny is not None:
        return deny
    body, _files, err = await body_or_error(request)
    if err is not None:
        return err
    import server as S
    import quick_notes_module as QN
    pid = _qn_profile(request)
    note_id = str(body.get("id") or "")
    note = await run_in_threadpool(QN.get_note, pid, note_id)
    if not note:
        return j({"ok": False, "error": "Заметка не найдена"}, 404)
    text = note.get("text") or ""
    first = ""
    for line in text.splitlines():
        if line.strip():
            first = line.strip()
            break
    title = (first or "Быстрая заметка")[:80]
    try:
        result = await run_in_threadpool(S.notes_save_article, {
            "title": title,
            "content": text,
            "folder": "inbox",
            "tags": ["quick"],
        })
        if not bool(body.get("keep")):
            await run_in_threadpool(QN.delete_note, pid, note_id)
        return j({"ok": True, "result": result})
    except Exception as e:
        swallow.note('compat_notes.quick_notes_to_note:L577', e)
        return _err(S, "/api/quick-notes/to-note", e)
