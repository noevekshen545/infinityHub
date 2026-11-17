"""«Скачать» и «Загрузить» в медиа: стриминговый ZIP, приём чанков, лимиты.

Почему отдельный модуль: обе кнопки опираются на одни и те же ограничения —
видимость по профилю, зашифрованные контейнеры, имена и размеры. Расхождение
здесь дало бы дыру того же класса, что «в UI скрыто, по прямому пути отдаётся».

ZIP пишется прямо в ответ: заголовок записи, её байты, дескриптор с CRC и
размерами, и лишь в конце центральный каталог. Архив не собирается ни в памяти,
ни во временном файле — альбом или сезон это гигабайты, а двойная запись между
дисками бьёт по свободному месту. Метод — хранение без сжатия: mp3/mkv/jpg уже
сжаты, повторное сжатие стоит CPU и ничего не даёт. ZIP64 включается только для
записей, где размер или смещение не влезают в 32 бита, поэтому маленькие архивы
остаются обычными для проводника Windows.

Загрузка идёт чанками (UPLOAD_CHUNK): security_guard режет тело запроса по
`security.max_body_mb` (16 МБ по умолчанию) и по Content-Length, и по chunked,
исключений для загрузок у него нет. Один PUT на фильм в 5 ГБ отсёк бы сам
гвард, а поднимать глобальный лимит ради одной кнопки нельзя.

Состояние сессий живёт в памяти процесса: уронили сервер посреди загрузки —
клиент получит 410 и начнёт заново, а песочница в data/media_upload_tmp доживёт
до sweep по TTL. Хранить её на диске между перезапусками незачем: файл всё
равно не был дописан.
"""

import os
import re
import struct
import swallow
import time
import uuid
import zlib

MAX_SET_ITEMS = 300
MAX_NAME_LEN = 140
UPLOAD_CHUNK = 8 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024
SESSION_TTL = 6 * 3600.0
MAX_ZIP_ENTRIES = 5000
MAX_ZIP_BYTES = 16 * 1024 * 1024 * 1024

_READ = 512 * 1024

# Что пустим в библиотеку. Индексатор своими расширениями не ограничен, но
# приём exe/js превратил бы медиатеку в свалку и открыл способ положить
# исполняемое имя на диск пользователя.
AUDIO_EXTS = {'.mp3', '.flac', '.ogg', '.oga', '.wav', '.aac', '.m4a', '.wma',
              '.opus', '.ape', '.aiff', '.dsf', '.dff', '.m4b', '.mka'}
VIDEO_EXTS = {'.mp4', '.m4v', '.mkv', '.webm', '.avi', '.mov', '.wmv', '.flv',
              '.ts', '.m2ts', '.ogv', '.mpg', '.mpeg', '.3gp'}
PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif',
              '.heic', '.heif'}
CATEGORY_EXTS = {"music": AUDIO_EXTS, "video": VIDEO_EXTS, "photos": PHOTO_EXTS}
CATEGORY_MEDIA_KEY = {"music": "music_dirs", "video": "video_dirs",
                      "photos": "photo_dirs"}

_SIG_LOCAL = 0x04034b50
_SIG_DESC = 0x08074b50
_SIG_CENTRAL = 0x02014b50
_SIG_EOCD64 = 0x06064b50
_SIG_EOCD64_LOC = 0x07064b50
_SIG_EOCD = 0x06054b50
_U32 = 0xFFFFFFFF
_FLAG = 0x0808          # бит 3 — размеры в дескрипторе, бит 11 — UTF-8 имена
_1980 = 315532800


class TransferError(Exception):
    """Отказ с HTTP-кодом: маршрут отдаёт str(e) как error-поле."""

    def __init__(self, message, status=400):
        Exception.__init__(self, message)
        self.status = status


def app_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def tmp_root():
    """Песочница недописанных загрузок — на диске проекта.

    media_module.DATA_DIR смотрит на ~/.infinity/data (C:), а временный файл
    загрузки весит как фильм: складывать его на системный диск нельзя.
    """
    return os.path.join(app_root(), "data", "media_upload_tmp")


def attachment_header(filename):
    """Content-Disposition: ASCII-запас для старых клиентов + UTF-8 версия.

    Кодируем по RFC 5987, а не «не-буквы»: у str.isalnum() кириллица тоже
    буква, её заголовок улетает сырыми байтами и Starlette падает на
    latin-1-кодировании заголовка. Разрешены только ASCII-буквы, цифры и -._~
    """
    raw = str(filename or "файл")
    fallback = (re.sub(r"[^A-Za-z0-9 ._-]", "_", raw).strip() or "file").replace('"', "'")
    quoted = "".join(c if c in "-._~" or (c.isascii() and c.isalnum()) else "".join(
        "%%%02X" % b for b in c.encode("utf-8")) for c in raw)
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (fallback, quoted)


def safe_component(name):
    """Один сегмент имени: без пути, без запрещённых символов, без точки в конце."""
    s = os.path.basename(str(name or "").replace("\\", "/")).strip()
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip(" .")
    if not s:
        return ""
    if len(s) > MAX_NAME_LEN:
        stem, ext = os.path.splitext(s)
        s = stem[:max(1, MAX_NAME_LEN - len(ext))] + ext
    return s


def safe_relpath(rel, keep_last=False):
    """Путь из webkitRelativePath: только каталоги, без .. и без корня диска.

    keep_last=True оставляет и имя файла (нужно распаковке архива), иначе —
    только префикс-каталоги, имя передаётся отдельно.
    """
    raw = str(rel or "").replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return ""
    parts = [p for p in raw.split("/") if p not in ("", ".", "..")]
    if not keep_last:
        parts = parts[:-1]
    out = []
    for seg in parts:
        seg = safe_component(seg)
        if seg:
            out.append(seg)
    return "/".join(out)


def unique_arcname(taken, name):
    """Неповторимое имя внутри архива: « (2)», « (3)» перед расширением."""
    base = name
    n = 2
    while base in taken:
        stem, ext = os.path.splitext(name)
        base = "%s (%d)%s" % (stem, n, ext)
        n += 1
    taken.add(base)
    return base


def read_chunks(path, start=0, length=None):
    """Байты файла частями; length ограничивает чтение точным размером."""
    with open(path, "rb") as f:
        if start:
            f.seek(start)
        remaining = length
        while remaining is None or remaining > 0:
            size = _READ if remaining is None else min(_READ, remaining)
            block = f.read(size)
            if not block:
                break
            if remaining is not None:
                remaining -= len(block)
            yield block


def decrypted_chunks(path):
    """Расшифрованный поток контейнера <path>.infenc — как при стриме."""
    import media_crypto
    enc = path + media_crypto.SUFFIX
    return media_crypto.iter_decrypted(enc, 0, media_crypto.plain_size(enc))


def _dos_stamp(mtime):
    """DOS time/date: в архиве не бывает дат раньше 1980."""
    try:
        t = time.localtime(mtime)
        if t.tm_year < 1980:
            t = time.localtime(_1980)
    except (OverflowError, OSError, ValueError):
        # Год дальше 2107 или mtime=0: архив не должен из-за этого
        # разваливаться, ставим датой 1980.
        swallow.note('media_transfer._dos_stamp:L171')
        t = time.localtime(_1980)
    return ((t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2),
            ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday)


def zip_stream(entries):
    """Генератор байтов store-only ZIP.

    entries: [(arcname, ожидаемый_размер, mtime, итератор_блоков)]. Размеры и CRC
    в локальном заголовке нулевые — настоящие значения приезжают в дескрипторе
    после данных, иначе файл пришлось бы читать дважды (первый проход — CRC).
    """
    central = []
    off = 0
    for name, size, mtime, chunks in entries:
        nb = name.encode("utf-8")
        z64 = int(size or 0) > _U32 - 1 or off > _U32 - 1
        tm, dt = _dos_stamp(mtime)
        extra = struct.pack("<HHQQ", 1, 16, 0, 0) if z64 else b""
        head = struct.pack("<IHHHHHIIIHH", _SIG_LOCAL, 45 if z64 else 20, _FLAG, 0,
                           tm, dt, 0, 0, 0, len(nb), len(extra))
        local_off = off
        yield head + nb + extra
        off += len(head) + len(nb) + len(extra)
        crc = 0
        written = 0
        for block in chunks:
            if not block:
                continue
            crc = zlib.crc32(block, crc) & 0xFFFFFFFF
            written += len(block)
            yield block
            off += len(block)
        z64 = z64 or written > _U32 - 1 or local_off > _U32 - 1
        desc = struct.pack("<IIQQ" if z64 else "<IIII", _SIG_DESC, crc, written, written)
        yield desc
        off += len(desc)
        central.append((nb, crc, written, local_off, z64, tm, dt))

    cd_start = off
    cd = bytearray()
    any64 = False
    for nb, crc, usize, local_off, z64, tm, dt in central:
        any64 = any64 or z64
        extra = b""
        u_f = c_f = o_f = usize if z64 else usize
        o_f = local_off
        if z64:
            extra = struct.pack("<HHQQQ", 1, 24, usize, usize, local_off)
            u_f = usize if usize <= _U32 - 1 else _U32
            c_f = u_f
            o_f = local_off if local_off <= _U32 - 1 else _U32
        cd += struct.pack("<IHHHHHHIIIHHHHHII", _SIG_CENTRAL, 0x031E if z64 else 0x0314,
                          45 if z64 else 20, _FLAG & ~0x0008, 0, tm, dt, crc,
                          c_f, u_f, len(nb), len(extra), 0, 0, 0, 0, o_f)
        cd += nb + extra
    yield bytes(cd)
    off += len(cd)

    n = len(central)
    if n == 0:
        yield struct.pack("<IHHHHIIH", _SIG_EOCD, 0, 0, 0, 0, 0, 0, 0)
        return
    if any64 or n > 0xFFFF or len(cd) > _U32 - 1 or cd_start > _U32 - 1:
        yield struct.pack("<IQHHIIQQQQ", _SIG_EOCD64, 44, 0, 45, 0, 0, n, n,
                          len(cd), cd_start)
        yield struct.pack("<IIQI", _SIG_EOCD64_LOC, 0, off, 1)
        cnt = n if n <= 0xFFFF else 0xFFFF
        sz = len(cd) if len(cd) <= _U32 - 1 else _U32
        st = cd_start if cd_start <= _U32 - 1 else _U32
    else:
        cnt, sz, st = n, len(cd), cd_start
    yield struct.pack("<IHHHHIIH", _SIG_EOCD, 0, 0, cnt, cnt, sz, st, 0)


async def stream_async(sync_iter):
    """Синхронный итератор байтов в StreamingResponse: чтения уходят в пул.

    StreamingResponse умеет только асинхронные генераторы, а next() над
    файловым чтением в потоке событий froze-бы весь сервер на время large-read.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    it = iter(sync_iter)
    while True:
        block = await loop.run_in_executor(None, next, it, None)
        if block is None:
            break
        yield block


async def zip_stream_async(entries):
    """Тот же поток, но чтения идут в пуле: блоки по 512 КБ в потоке событий."""
    async for block in stream_async(zip_stream(entries)):
        yield block


def zip_entries(pairs):
    """[(имя_в_архиве, путь_на_диске, зашифрован_ли)] -> entries для zip_stream.

    Шифрованный контейнер отдаётся расшифрованным и с размером plaintext: иначе
    скачанный .infenc не сыграет ни в одном плеере, кроме нашего.
    """
    out = []
    for arcname, path, encrypted in pairs:
        real = path + ".infenc" if encrypted else path
        size = _plain_size(real) if encrypted else os.path.getsize(real)
        out.append((arcname, size, os.path.getmtime(real),
                    decrypted_chunks(path) if encrypted else read_chunks(real)))
    return out


def _plain_size(enc_path):
    import media_crypto
    return media_crypto.plain_size(enc_path)


# ---------- приём загрузки ----------

_SESSIONS = {}


def upload_enabled():
    """Тумблер «Загрузить» в настройках медиа, по умолчанию ВЫКЛ.

    Ключ живёт в таблице settings медиамодуля — там же, где
    media_encryption_enabled, и пишется тем же /api/media/settings/save (у него
    нет белого списка ключей). Отсутствие ключа читается как «выключено», поэтому
    миграция существующим конфигам не нужна.
    """
    try:
        import media_module
        val = media_module.get_setting_value("media_upload_enabled", "0")
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        swallow.note('media_transfer.upload_enabled:L309')
        return False


def _drop(sid):
    s = _SESSIONS.pop(sid, None)
    if not s or not s.get("dir"):
        return
    import shutil
    shutil.rmtree(s["dir"], ignore_errors=True)


def _sweep():
    """Забытые сессии: браузер закрыли посреди загрузки — tmp остался бы навсегда."""
    now = time.time()
    for sid in list(_SESSIONS):
        s = _SESSIONS.get(sid) or {}
        if now - float(s.get("created", now)) > SESSION_TTL:
            _drop(sid)
    root = tmp_root()
    if not os.path.isdir(root):
        return
    import shutil
    for name in os.listdir(root):
        p = os.path.join(root, name)
        try:
            if now - os.path.getmtime(p) > SESSION_TTL:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            swallow.note('media_transfer._sweep:L338')
            continue


def resolve_target(category, profile_id):
    """Каталог, куда ляжет файл: сначала личный и не обещанный никому.

    visible_dirs() начинается с базы общедоступного профиля — файл, положенный в
    неё, стал бы виден всем, а при снятии галки наследования исчез бы из
    собственного списка. Поэтому адресат ищем среди своих каталогов, а из них
    сначала берём те, которые не числятся своими ни у одного другого профиля:
    замер 04.11 дал три профиля с одним и тем же `D:/Выкачка с ноута/музыка`, и
    загрузка одного уезжала в библиотеку соседа. Если своих каталогов вообще нет
    — заводим data/profiles/<pid>/media/<category> и дописываем его в профиль,
    иначе загрузка уехала бы в никуда.

    Когда остаются только общие, файл кладём туда же (это его личная папка в
    настройках — решение владельца) и полагаемся на второй слой: finish()
    запоминает владельца пути, и чужой профиль его не увидит.
    """
    import media_visibility as MV
    from modules import profile_state
    if category not in CATEGORY_EXTS:
        raise TransferError("Неизвестная категория: %s" % category, 400)
    own = MV.own_dirs(category, profile_id)
    if own:
        if MV.filter_enabled(profile_id):
            exclusive = MV.exclusive_own_dirs(category, profile_id)
            if exclusive:
                return exclusive[0]
        return own[0]
    if not profile_id:
        raise TransferError("Профиль вызывающего не определён", 403)
    new = os.path.join(app_root(), "data", "profiles", str(profile_id), "media", category)
    os.makedirs(new, exist_ok=True)
    key = CATEGORY_MEDIA_KEY[category]
    eff = profile_state.persist_media_dirs(key,
                                           MV.visible_dirs(category, profile_id) + [new],
                                           profile_id=profile_id) or []
    if not any(MV.norm(d) == MV.norm(new) for d in eff):
        raise TransferError("Каталог %s не записан в профиль" % new, 500)
    return new


def chunk_size():
    """Размер чанка, который точно пропустит security_guard.

    Гвард режет тело по `security.max_body_mb` (16 МБ по умолчанию) и не делает
    исключений для загрузок: админ с лимитом 4 МБ получил бы 413 на каждом чанке
    и кнопка не работала бы вовсе. Клиент режет файл по этому числу, поэтому
    честный ответ — минимум из нашего предела и предела гварда, с запасом на
    служебные байты.
    """
    limit = UPLOAD_CHUNK
    try:
        import security_guard as SG
        limit = min(limit, int(SG.body_limit_bytes() * 0.9))
    except Exception:
        swallow.note('media_transfer.chunk_size:L390')
    return max(256 * 1024, limit)


def begin(category, profile_id, filename, relpath, size):
    """Открыть сессию: песочница + .part, в библиотеку не пишем до finish."""
    _sweep()
    if category not in CATEGORY_EXTS:
        raise TransferError("Неизвестная категория: %s" % category, 400)
    name = safe_component(filename)
    if not name:
        raise TransferError("Недопустимое имя файла", 400)
    ext = os.path.splitext(name)[1].lower()
    if ext != ".zip" and ext not in CATEGORY_EXTS[category]:
        raise TransferError("Формат %s не входит в библиотеку %s"
                            % (ext or "без расширения", category), 415)
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        raise TransferError("Не понятен размер файла", 400)
    if size < 0 or size > MAX_FILE_BYTES:
        raise TransferError("Файл больше %.0f ГБ — так загрузку не принять"
                            % (MAX_FILE_BYTES / 1024 ** 3), 413)
    target = resolve_target(category, profile_id)
    sub = safe_relpath(relpath)
    sid = uuid.uuid4().hex[:16]
    d = os.path.join(tmp_root(), sid)
    os.makedirs(d, exist_ok=True)
    _SESSIONS[sid] = {"id": sid, "category": category, "profile": profile_id,
                      "name": name, "sub": sub, "target": target, "size": size,
                      "received": 0, "created": time.time(), "dir": d,
                      "part": os.path.join(d, "data.part")}
    return {"session_id": sid, "chunk": chunk_size(), "target": target,
            "path": "/".join([p for p in (sub, name) if p])}


def append(sid, data):
    """Один чанк сырого тела в .part. Последовательность обязательна: пропущенный
    чанк дал бы дыру в середине файла, а не в конце."""
    s = _SESSIONS.get(sid)
    if not s:
        raise TransferError("Сессия загрузки не найдена — начни заново", 410)
    if time.time() - s["created"] > SESSION_TTL:
        _drop(sid)
        raise TransferError("Сессия загрузки протухла", 410)
    if len(data) > UPLOAD_CHUNK:
        raise TransferError("Чанк больше %d МБ" % (UPLOAD_CHUNK // 1024 ** 2), 413)
    got = s["received"] + len(data)
    if got > MAX_FILE_BYTES or (s["size"] and got > s["size"]):
        _drop(sid)
        raise TransferError("Файл больше обещанного размера", 413)
    with open(s["part"], "ab") as f:
        f.write(data)
    s["received"] = got
    return {"received": got, "total": s["size"], "percent": (100.0 * got / s["size"])
            if s["size"] else 0.0}


def unpack_zip(zpath, work_root, limit_total):
    """Распаковка архива в песочницу, потом в библиотеку перенесёт finish.

    Гейт пути — как profiles._safe_extract_zip: абсолют, «C:», пустые и «..»
    отсекаются, realpath обязан остаться внутри. Сверху лимиты числа записей,
    объёма и степени сжатия: без них архив-бомба раздулся бы на весь диск.
    """
    import zipfile
    work = os.path.join(work_root, "unz")
    work_real = os.path.realpath(work)
    os.makedirs(work_real, exist_ok=True)
    out = []
    total = 0
    with zipfile.ZipFile(zpath) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise TransferError("В архиве слишком много файлов (%d)" % len(infos), 413)
        for zi in infos:
            if zi.is_dir():
                continue
            rel = safe_relpath(zi.filename, keep_last=True)
            if not rel:
                continue
            total += zi.file_size
            if total > limit_total or zi.file_size > MAX_FILE_BYTES:
                raise TransferError("Распакованный архив слишком большой", 413)
            if zi.compress_size and zi.file_size > max(1024 * 1024,
                                                       zi.compress_size * 100):
                raise TransferError("Архив подозрительно сжат — распаковка отклонена", 413)
            dest = os.path.realpath(os.path.join(work_real, *rel.split("/")))
            if not dest.startswith(work_real + os.sep):
                raise TransferError("Архив содержит путь вне каталога", 415)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".part"
            with zf.open(zi) as src, open(tmp, "wb") as fo:
                while True:
                    block = src.read(_READ)
                    if not block:
                        break
                    fo.write(block)
            os.replace(tmp, dest)
            out.append((dest, rel))
    return out


def _avoid_overwrite(dest):
    """Не перезаписывать существующее: « (2)» перед расширением."""
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(dest)
    n = 2
    while os.path.exists("%s (%d)%s" % (stem, n, ext)):
        n += 1
    return "%s (%d)%s" % (stem, n, ext)


def _place(dest, src):
    """Перенос готового файла в библиотеку; между дисками — копирование."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.replace(src, dest)
    except OSError:
        swallow.note('media_transfer._place:L486')
        import shutil
        shutil.copyfile(src, dest)
        os.remove(src)


def finish(sid):
    """Дописать файл (или распакованный архив) в библиотеку и сказать, что легло."""
    s = _SESSIONS.get(sid)
    if not s:
        raise TransferError("Сессия загрузки не найдена — начни заново", 410)
    if not os.path.isfile(s["part"]):
        _drop(sid)
        raise TransferError("Данные загрузки не записаны", 410)
    received = os.path.getsize(s["part"])
    if s["size"] and received != s["size"]:
        _drop(sid)
        raise TransferError("Файл долетел не целиком: %d из %d байт"
                            % (received, s["size"]), 400)
    base = os.path.join(s["target"], s["sub"]) if s["sub"] else s["target"]
    placed = []
    absolute = []
    if s["name"].lower().endswith(".zip"):
        allowed = CATEGORY_EXTS[s["category"]]
        for (src, rel) in unpack_zip(s["part"], s["dir"], MAX_ZIP_BYTES):
            ext = os.path.splitext(rel)[1].lower()
            if ext not in allowed:
                os.remove(src)
                continue
            dest = _avoid_overwrite(os.path.join(base, *rel.split("/")))
            _place(dest, src)
            placed.append(os.path.relpath(dest, s["target"]).replace("\\", "/"))
            absolute.append(dest)
    else:
        dest = _avoid_overwrite(os.path.join(base, s["name"]))
        _place(dest, s["part"])
        placed.append(os.path.relpath(dest, s["target"]).replace("\\", "/"))
        absolute.append(dest)
    _drop(sid)
    import media_visibility as MV
    _mark_owner(s["category"], s["profile"], absolute)
    MV.invalidate(s["profile"])
    return {"category": s["category"], "profile": s["profile"], "target": s["target"],
            "files": placed}


def _mark_owner(category, profile_id, paths):
    """Второй слой изоляции: память о том, чья это загрузка.

    Общедоступный профиль — сам себе база, и его загрузки по прежнему решению
    владельца видны всем; для него владельца не пишем. Молчание при сбое памяти
    оправдано: файл уже в библиотеке, а отказывать из-за этого в загрузке
    значило бы терять данные.
    """
    if not paths or not profile_id:
        return
    try:
        import profiles_module as PM
        if PM.is_public_profile(profile_id):
            return
    except Exception:
        swallow.note('media_transfer._mark_owner:L562')
    try:
        import media_owner
        media_owner.remember(category, profile_id, paths)
    except Exception:
        swallow.note('media_transfer._mark_owner:L567')


def abort(sid):
    _drop(sid)
    return {"ok": True}
