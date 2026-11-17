"""Редактирование фото соцсети — неразрушающее.

Файлы соцсети пишутся байт-в-байт (_save_upload), трансформации в
social_module не было вовсе: PIL там не импортируется. Правки хранятся
отдельно, оригинал остаётся на диске и всегда доступен для отката.

Семантика операций совпадает с photo_module.save_photo_edit (там она
написана, но соответствующий Flask-роут не смонтирован, а живой
main.py принимает только {photo_id, image_data}). Формат один на оба
модуля, чтобы при необходимости можно было переиспользовать код.

Операции применяются по порядку:
    {"type": "crop", "x", "y", "w", "h"}      — координаты в пикселях ОРИГИНАЛА
    {"type": "rotate", "degrees"}             — шаг 90/180/270, expand=True
    {"type": "flip_h"} / {"type": "flip_v"}
    {"type": "brightness"|"contrast"|"saturation"|"sharpen", "factor": 0.5..2.0}
    {"type": "blur", "radius": 0..20}
    {"type": "grayscale"}
    {"type": "reset"}                         — сброс всех правок этого фото

Важно: координаты обрезки всегда задаются относительно оригинала, а не
предыдущей операции. Иначе клиент пришлось бы пересчитывать рамку после
поворота, а это самая частая ошибка в таких редакторах.
"""
import io
import json
import os
import uuid

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

import social_module as sm
import swallow

# Префикс НЕ указываем: он уже задан у social_module.router, и include_router
# добавил бы его второй раз, дав путь /api/social/api/social/...
router = APIRouter(tags=["social-photo-edit"])

MAX_RESULT_BYTES = 25 * 1024 * 1024      # 25 МБ на результат правки
MAX_VERSIONS = 20                        # сколько версий держим на фото
MAX_SOURCE_PIXELS = 40_000_000           # ~8000x5000, защита от распаковки бомб
_EDITABLE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def _photo_row(conn, photo_id):
    row = conn.execute(
        "SELECT * FROM social_profile_photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Фото не найдено")
    return row


def _url_to_path(url):
    """Путь на диске по ссылке вида /api/social/file/root/<имя>.

    Всё, что не наш собственный префикс, отвергается: иначе через
    подделанный url можно было бы заставить сервер открыть произвольный
    файл и перезаписать результат правки.
    """
    if not url or not isinstance(url, str):
        return None
    marker = "/api/social/file/root/"
    if marker not in url:
        return None
    name = url.split(marker, 1)[1].split("?", 1)[0]
    # Имя файла не должно содержать путей: иначе через ../ можно было бы
    # выйти за пределы каталога загрузок.
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    path = os.path.join(sm.UPLOADS_DIR, name)
    if not os.path.isfile(path):
        return None
    return path


def _can_edit(conn, photo, profile_id):
    """Кто вправе править фото.

    Согласовано с правилами: автор фото, владелец альбома, а также
    админ или модератор группы, в альбом которой фото лежит. Для фото
    профиля — только сам владелец.
    """
    if photo["profile_id"] == profile_id:
        return True
    albums = conn.execute(
        "SELECT a.* FROM social_albums a "
        "JOIN social_album_photos ap ON ap.album_id = a.id "
        "WHERE ap.photo_id = ?", (photo["id"],)).fetchall()
    for a in albums:
        if a["profile_id"] == profile_id:
            return True
        if a["group_id"]:
            role = conn.execute(
                "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?",
                (a["group_id"], profile_id)).fetchone()
            if role and role["role"] in ("owner", "admin", "moderator"):
                return True
        if a["channel_id"]:
            role = conn.execute(
                "SELECT role FROM social_server_members WHERE server_id=? AND profile_id=?",
                (_server_of_channel(conn, a["channel_id"]), profile_id)).fetchone()
            if role and role["role"] in ("owner", "admin", "mod"):
                return True
    return False


def _server_of_channel(conn, channel_id):
    row = conn.execute(
        "SELECT server_id FROM social_server_channels WHERE id=?", (channel_id,)).fetchone()
    return row["server_id"] if row else ""


def _apply_ops(img, ops):
    """Применить список операций к объекту PIL."""
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter

    for op in ops:
        if not isinstance(op, dict):
            raise HTTPException(400, "Некорректная операция")
        kind = op.get("type")
        if kind == "crop":
            x = int(op.get("x", 0)); y = int(op.get("y", 0))
            w = int(op.get("w", 0)); h = int(op.get("h", 0))
            # Обрезка за границами изображения ломала бы сохранение,
            # поэтому прямоугольник стыкуется с текущими размерами.
            x = max(0, min(x, img.width - 1))
            y = max(0, min(y, img.height - 1))
            w = max(1, min(w, img.width - x))
            h = max(1, min(h, img.height - y))
            img = img.crop((x, y, x + w, y + h))
        elif kind == "rotate":
            deg = int(op.get("degrees", 0)) % 360
            if deg:
                img = img.rotate(-deg, expand=True)
        elif kind == "flip_h":
            img = ImageOps.mirror(img)
        elif kind == "flip_v":
            img = ImageOps.flip(img)
        elif kind == "brightness":
            img = ImageEnhance.Brightness(img).enhance(_factor(op))
        elif kind == "contrast":
            img = ImageEnhance.Contrast(img).enhance(_factor(op))
        elif kind == "saturation":
            img = ImageEnhance.Color(img).enhance(_factor(op))
        elif kind == "sharpen":
            img = ImageEnhance.Sharpness(img).enhance(_factor(op))
        elif kind == "blur":
            r = max(0.0, min(20.0, float(op.get("radius", 0) or 0)))
            if r > 0:
                img = img.filter(ImageFilter.GaussianBlur(r))
        elif kind == "grayscale":
            img = ImageOps.grayscale(img)
        else:
            raise HTTPException(400, "Неизвестная операция: %s" % (kind,))
    return img


def _factor(op):
    try:
        v = float(op.get("factor", 1.0))
    except (TypeError, ValueError):
        swallow.note('social_photo_edit._factor:L162')
        v = 1.0
    return max(0.0, min(3.0, v))


def _save_image(img, ext):
    """Сохранить в BytesIO, вернуть (байты, формат)."""
    buf = io.BytesIO()
    fmt = "JPEG" if ext in (".jpg", ".jpeg") else ("WEBP" if ext == ".webp" else "PNG")
    if fmt in ("JPEG", "WEBP"):
        # JPEG и WEBP без альфы: RGBA туда не пишется, и без сведения
        # на белый фон прозрачные области стали бы чёрными.
        if img.mode in ("RGBA", "LA", "P"):
            from PIL import Image
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
    params = {"quality": 92} if fmt == "JPEG" else ({"quality": 92} if fmt == "WEBP" else {"optimize": True})
    img.save(buf, format=fmt, **params)
    return buf.getvalue(), fmt


def _render(source_path, ops, out_ext):
    from PIL import Image
    img = Image.open(source_path)
    # EXIF-поворот: без него телефонные снимки вставали боком, а
    # координаты обрезки считали бысь по неверной геометрии.
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        swallow.note('social_photo_edit._render:L196')
        pass
    if img.width * img.height > MAX_SOURCE_PIXELS:
        raise HTTPException(413, "Фото слишком большое для правки в браузере")
    img = img.convert("RGB") if img.mode not in ("RGBA", "LA", "P") else img
    img = _apply_ops(img, ops)
    return _save_image(img, out_ext)


def _ensure_original_registered(conn, photo, author_id):
    """Перед первой правкой занести текущий файл в историю как оригинал.

    Без этого «Вернуть оригинал» восстанавливал бы первую же правку:
    в истории лежали только результаты правок, и самого оригинала в ней
    не было вовсе.
    """
    has = conn.execute(
        "SELECT 1 FROM social_photo_versions WHERE photo_id=? LIMIT 1",
        (photo["id"],)).fetchone()
    if has:
        return
    conn.execute(
        "INSERT INTO social_photo_versions (id, photo_id, path, ops, author_id, created_at) "
        "VALUES (?,?,?,'[]',?,?)",
        (uuid.uuid4().hex[:12], photo["id"], photo["url"], author_id, sm._now()))
    conn.execute(
        "UPDATE social_profile_photos SET current_version=? WHERE id=?",
        (photo["url"], photo["id"]))


def _store_version(conn, photo_id, path, ops, author_id, old_url):
    """Записать текущую версию файла в историю и поставить её активной."""
    url = "/api/social/file/root/" + os.path.basename(path)
    conn.execute(
        "INSERT INTO social_photo_versions (id, photo_id, path, ops, author_id, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex[:12], photo_id, url, json.dumps(ops, ensure_ascii=False),
         author_id, sm._now()))
    conn.execute(
        "UPDATE social_profile_photos SET url=?, current_version=?, edited_at=? WHERE id=?",
        (url, url, sm._now(), photo_id))
    _prune_versions(conn, photo_id)
    _sync_album_covers(conn, photo_id, old_url, url)


def _prune_versions(conn, photo_id):
    """Оставить последние MAX_VERSIONS версий; более старые файлы удаляем."""
    rows = conn.execute(
        "SELECT id, path FROM social_photo_versions WHERE photo_id=? "
        "ORDER BY created_at DESC", (photo_id,)).fetchall()
    for r in rows[MAX_VERSIONS:]:
        conn.execute("DELETE FROM social_photo_versions WHERE id=?", (r["id"],))
        p = _url_to_path(r["path"])
        if p and os.path.isfile(p):
            # Оригинал не трогаем: он лежит под именем без префикса edited_
            if os.path.basename(p).startswith("edited_"):
                try:
                    os.remove(p)
                except OSError:
                    swallow.note('social_photo_edit._prune_versions:L255')
                    pass


def _sync_album_covers(conn, photo_id, old_url, new_url):
    """Обложки альбомов хранят прямую ссылку на фото, поэтому после правки
    они продолжали бы указывать на предыдущую версию.

    Сравнение идёт со старым адресом: если спросить в подзапросе текущий
    url фото, он к этому моменту уже обновлён и не совпадёт ни с чем.
    """
    if not old_url or old_url == new_url:
        return
    conn.execute(
        "UPDATE social_albums SET cover_path=? WHERE cover_path=? AND id IN "
        "(SELECT album_id FROM social_album_photos WHERE photo_id=?)",
        (new_url, old_url, photo_id))


def _version_dict(r, current):
    try:
        ops = json.loads(r["ops"] or "[]")
    except (ValueError, TypeError):
        swallow.note('social_photo_edit._version_dict:L278')
        ops = []
    if not isinstance(ops, list):
        ops = []
    return {
        "id": r["id"],
        "url": r["path"],
        "author_id": r["author_id"],
        "author_name": r["author_name"] if "author_name" in r.keys() else "",
        "created_at": r["created_at"],
        "ops": ops,
        "is_current": r["path"] == current,
    }


@router.get("/photos/{photo_id}/versions")
def list_versions(photo_id: str, request: Request):
    viewer = sm._get_active_profile(request)
    conn = sm._get_db()
    photo = _photo_row(conn, photo_id)
    # Историю правок нельзя читать перебором идентификаторов: сначала
    # та же проверка видимости, что и у самого фото.
    if not sm._photo_view_allowed(conn, photo, viewer):
        raise HTTPException(403, "Нет доступа к этому фото")
    rows = conn.execute(
        "SELECT v.*, COALESCE(u.display_name, v.author_id) AS author_name "
        "FROM social_photo_versions v "
        "LEFT JOIN social_users u ON u.profile_id = v.author_id "
        "WHERE v.photo_id=? ORDER BY v.created_at DESC", (photo_id,)).fetchall()
    current = photo["url"]
    return JSONResponse({"versions": [_version_dict(r, current) for r in rows],
                         "current": current,
                         "edited_at": photo["edited_at"] or 0})


@router.post("/photos/{photo_id}/edit")
async def edit_photo(photo_id: str, request: Request, data: dict = Body(...)):
    """Применить правки и сохранить их как новую версию."""
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    photo = _photo_row(conn, photo_id)
    if not _can_edit(conn, photo, profile_id):
        raise HTTPException(403, "Нет прав на редактирование этого фото")

    ops = data.get("operations")
    if ops is None:
        ops = data.get("ops")
    if not isinstance(ops, list):
        raise HTTPException(400, "Нужен список операций: operations")
    if len(ops) > 30:
        raise HTTPException(400, "Слишком много операций (максимум 30)")

    source = _url_to_path(photo["url"])
    if not source:
        raise HTTPException(400, "Не удалось найти файл фото на диске")
    # Перед записью убеждаемся, что фото видно: иначе, зная photo_id чужого
    # альбома закрытой группы, можно было бы получить отредактированный файл.
    if not sm._photo_view_allowed(conn, photo, profile_id):
        raise HTTPException(403, "Нет доступа к этому фото")

    ext = os.path.splitext(source)[1].lower()
    if ext not in _EDITABLE_EXT:
        raise HTTPException(400, "Этот формат не редактируется: " + (ext or "?"))

    if any(o.get("type") == "reset" for o in ops if isinstance(o, dict)):
        _ensure_original_registered(conn, photo, profile_id)
        conn.commit()
        return await _reset_to_original(conn, photo, profile_id)

    old_url = photo["url"]
    _ensure_original_registered(conn, photo, profile_id)

    try:
        data_bytes, _fmt = _render(source, ops, ext)
    except HTTPException:
        raise
    except Exception as e:
        sm.logger.warning("Ошибка правки фото %s: %s", photo_id, e)
        raise HTTPException(500, "Не удалось применить правки")

    if len(data_bytes) > MAX_RESULT_BYTES:
        raise HTTPException(413, "Результат правки слишком большой")

    filename = "edited_%s_%s%s" % (photo_id[:12], uuid.uuid4().hex[:8], ext)
    path = os.path.join(sm.UPLOADS_DIR, filename)
    _atomic_write(path, data_bytes)

    _store_version(conn, photo_id, path, ops, profile_id, old_url)
    conn.commit()
    row = _photo_row(conn, photo_id)
    return JSONResponse({"ok": True, "url": row["url"],
                         "edited_at": row["edited_at"] or 0,
                         "size": len(data_bytes)})


async def _reset_to_original(conn, photo, profile_id):
    """Вернуть самую первую версию — то есть оригинал."""
    first = conn.execute(
        "SELECT path FROM social_photo_versions WHERE photo_id=? "
        "ORDER BY created_at ASC LIMIT 1", (photo["id"],)).fetchone()
    if not first:
        raise HTTPException(400, "У фото нет сохранённых версий")
    path = _url_to_path(first["path"])
    if not path:
        raise HTTPException(400, "Файл оригинала не найден на диске")
    conn.execute(
        "UPDATE social_profile_photos SET url=?, edited_at=? WHERE id=?",
        (first["path"], sm._now(), photo["id"]))
    _sync_album_covers(conn, photo["id"], photo["url"], first["path"])
    conn.commit()
    return JSONResponse({"ok": True, "url": first["path"]})


@router.post("/photos/{photo_id}/revert")
def revert_photo(photo_id: str, request: Request, data: dict = Body(...)):
    """Вернуть конкретную версию из истории."""
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    photo = _photo_row(conn, photo_id)
    if not _can_edit(conn, photo, profile_id):
        raise HTTPException(403, "Нет прав на редактирование этого фото")
    version_id = (data.get("version_id") or "").strip()
    row = conn.execute(
        "SELECT * FROM social_photo_versions WHERE id=? AND photo_id=?",
        (version_id, photo_id)).fetchone()
    if not row:
        raise HTTPException(404, "Версия не найдена")
    if not _url_to_path(row["path"]):
        raise HTTPException(400, "Файл версии отсутствует на диске")
    conn.execute(
        "UPDATE social_profile_photos SET url=?, edited_at=? WHERE id=?",
        (row["path"], sm._now(), photo_id))
    _sync_album_covers(conn, photo_id, photo["url"], row["path"])
    conn.commit()
    return JSONResponse({"ok": True, "url": row["path"]})


def _atomic_write(path, data):
    """Запись через временный файл.

    Прямая запись обрывала файл, если процесс успевал упасть или если
    файл кем-то читался в этот момент: получался битый JPEG, который
    уже нельзя было ни открыть, ни откатить.
    """
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, path)
    except PermissionError:
        # Файл мог быть занят (например, открыт в просмотрщике).
        try:
            os.remove(path)
            os.replace(tmp, path)
        except OSError as e:
            raise HTTPException(503, "Не удалось сохранить: файл занят")
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            swallow.note('social_photo_edit._atomic_write:L440')
            pass
        raise HTTPException(500, "Не удалось сохранить файл")
