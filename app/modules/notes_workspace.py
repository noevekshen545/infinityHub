"""Состояние workspace заметок: снапшоты, список заметок папки, вложения.

Перенесено из server_backup.py. Пути профиля переписывает apply_profile_globals,
поэтому NOTES_DIR читается через _sb при каждом вызове, а не копируется по значению.

WORKSPACES_DIR в монолите — константа, вычисленная из NOTES_DIR один раз на импорте,
и в синхронизацию флапа она не входит (см. server_backup.py:5419); после переключения
профиля в том же процессе она указывала бы на холсты прошлого профиля, а файлы
именуются по папке, так что сохранение в одном профиле затирало бы чужой canvas_data.
От неё здесь отказались в пользу пересчёта из активного NOTES_DIR.
"""

import json
import os
import time

import server_backup as _sb
import profile_paths
import swallow
from modules import public_view as _pv

_pp = profile_paths.proxy(_sb)   # каталоги профиля запроса; вне запроса — глобалы _sb


def _workspaces_dir():
    """Каталог холстов активного профиля — пересчитан в момент вызова."""
    return os.path.join(_pp.NOTES_DIR, "_workspaces")

def _visible_state_file(folder):
    """Файл холста папки: свой, иначе (по галке) общедоступный, иначе пустой путь."""
    for base in _pv.read_dirs("notes"):
        fp = _sb._notes_child(os.path.join(base, "_workspaces"), f"{folder}.json")
        if fp and os.path.exists(fp):
            return fp
    return ""

def workspace_get_state(folder):
    """Загрузить состояние workspace для папки.

    Своего холста может и не быть — тогда по галке показываем общедоступный;
    первая же правка сохранится в свой набор и перекроет чужой (своё всегда
    впереди), поэтому общий холст остаётся только видом, а не общей записью.
    """
    fp = _visible_state_file(folder)
    if fp:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            swallow.note('notes_workspace.workspace_get_state:L49')
            pass
    return {"folder": folder, "updated": 0, "tldraw_snapshot": None, "note_windows": {}}

def workspace_save_state(data):
    """Сохранить состояние workspace."""
    folder = data.get("folder", "")
    if not folder:
        return {"error": "No folder specified"}
    ws = _workspaces_dir()
    os.makedirs(ws, exist_ok=True)
    fp = _sb._notes_child(ws, f"{folder}.json")
    if not fp:
        return {"error": "Недопустимое имя папки"}
    data["updated"] = time.time()
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception as e:
        swallow.note('notes_workspace.workspace_save_state:L69', e)
        return {"error": str(e)}

def workspace_get_notes(folder):
    """Получить все заметки папки с полным контентом, режимом и вложениями."""
    articles = []
    seen = set()
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    for base in _pv.read_dirs("notes"):
        search_dir = _sb._notes_child(base, folder) if folder and folder not in ("all", "inbox") else base
        if not search_dir:
            continue
        try:
            for root, dirs, files in os.walk(search_dir):
                dirs[:] = [d for d in dirs if d not in system_folders and not d.startswith('.')]
                for f in files:
                    if not f.endswith(".json"):
                        continue
                    fp = os.path.join(root, f)
                    try:
                        with open(fp, encoding="utf-8") as fh:
                            art = json.load(fh)
                        rel = os.path.relpath(fp, base)
                        parts = rel.split(os.sep)
                        art_folder = parts[0] if len(parts) > 1 else "inbox"
                        if art_folder in system_folders:
                            continue
                        if folder and folder not in ("all", "inbox") and art_folder != folder:
                            continue
                        if art.get("id", f.replace(".json", "")) in seen:
                            continue
                        seen.add(art.get("id", f.replace(".json", "")))
                        # Get attachments
                        atts = []
                        att_dir = os.path.join(base, "_attachments", art.get("id", ""))
                        if os.path.isdir(att_dir):
                            for af in os.listdir(att_dir):
                                afp = os.path.join(att_dir, af)
                                if os.path.isfile(afp):
                                    ext = os.path.splitext(af)[1].lower()
                                    is_img = ext in ('.png','.jpg','.jpeg','.gif','.svg','.webp')
                                    atts.append({
                                        "filename": af,
                                        "ext": ext,
                                        "size": os.path.getsize(afp),
                                        "url": f"/api/notes/attachment?note={art.get('id','')}&file={af}",
                                        "is_image": is_img
                                    })
                        # Determine mode from note content or metadata
                        mode = art.get("mode", "markdown")
                        if mode not in ("markdown", "excalidraw", "luckysheet"):
                            mode = "markdown"
                        articles.append({
                            "id": art.get("id", f.replace(".json", "")),
                            "title": art.get("title", "Без названия"),
                            "folder": art_folder,
                            "content": art.get("content", ""),
                            "mode": mode,
                            "created": art.get("created", 0),
                            "updated": art.get("updated", 0),
                            "tags": art.get("tags", []),
                            # Признак рисунка без его байтов: сцена весит мегабайты
                            # (вложенные base64-картинки), а карточке холста нужно
                            # только понять, какой это режим. Без него заметка с
                            # рисунком в поле scene выглядела бы markdown-ом.
                            "scene_present": isinstance(art.get("scene"), dict)
                            and isinstance(art["scene"].get("elements"), list),
                            "attachments": atts
                        })
                    except Exception:
                        swallow.note('notes_workspace.workspace_get_notes:L139')
                        continue
        except Exception:
            swallow.note('notes_workspace.workspace_get_notes:L142')
            pass
    articles.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return {"articles": articles}

def workspace_get_attachments(folder):
    """Получить все вложения заметок в папке."""
    notes = workspace_get_notes(folder).get("articles", [])
    all_atts = []
    for n in notes:
        for a in n.get("attachments", []):
            a["note_id"] = n["id"]
            a["note_title"] = n["title"]
            all_atts.append(a)
    return {"attachments": all_atts}

def workspace_get_folders_with_notes():
    """Получить список папок у которых есть заметки (для workspace кнопки)."""
    folders = set()
    system_folders = {"_ai_answers", "_chat_sessions", "_attachments", "_plugins", "_memory", "_workspaces"}
    for base in _pv.read_dirs("notes"):
        try:
            for item in os.listdir(base):
                item_path = os.path.join(base, item)
                if os.path.isdir(item_path) and item not in system_folders and not item.startswith('.'):
                    has_json = any(f.endswith('.json') for f in os.listdir(item_path))
                    if has_json:
                        folders.add(item)
        except Exception:
            swallow.note('notes_workspace.workspace_get_folders_with_notes:L171')
            pass
    return {"folders": sorted(folders)}
