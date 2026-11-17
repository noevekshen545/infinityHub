"""Съёмка экрана и её хранение: то, что делает глобальная клавиша скриншота.

Модуль существует ровно потому, что снимок нужен по сочетанию клавиш, когда
главное окно спрятано в трей: перехват живёт в фоновом потоке сервера (hotkey_hub),
а окно редактора рисует Electron. Связывает их desktop_bridge — отсюда только
команда «открой эту страницу».

Файлы лежат в data/profiles/<id>/data/screenshots: кто экран снимал, тому и
история. В редактор снимок попадает по имени, а не по пути: имя превращается в
путь через resolve(), который не выпускает за пределы каталога — страница не
может попросить чужой файл, подставив ../.
"""

import ctypes
import datetime as _dt
import io
import json
import logging
import os
import re
import shutil
import threading
import swallow

logger = logging.getLogger("infinity")

_LOCK = threading.Lock()

# Хранилище по профилям: data/profiles/<id>/data/screenshots — та же схема, что
# у заметок и чата (PROFILE_DATA_DIRS в profiles_module). Снимки общего доступа
# между профилями тут нет: кто экран снимал, тому история и принадлежит.
KINDS = ("full", "window", "region")
LEGACY_DIR = os.path.join(os.path.expanduser("~/.infinity"), "screenshots")
_SUBDIR = "screenshots"
_FOLDERS = (".trash",)
_MIGRATION_MARKER = ".migrated_from_legacy"

# Профильный id приходит из CookieString, но в путь он превращается только в
# таком виде: посторонние символы не должны дать "../" или ":".
_SAFE_ID = re.compile(r"[^A-Za-z0-9_\-]")


def _profiles_root() -> str:
    try:
        import profiles_module as PM
        return PM.PROFILES_DIR
    except Exception as e:
        logger.error("[screenshot] каталог профилей недоступен: %s", e)
        return os.path.join(os.path.expanduser("~/.infinity"), "profiles")


def resolve_profile(profile_id: str = "") -> str:
    """Чей это снимок: явный id -> активный профиль -> default.

    Поток глобальной клавиши запроса не видит, поэтому для него источник один —
    активный профиль сервера (его ставит middleware при переходе по странице).
    """
    pid = str(profile_id or "").strip()
    if not pid:
        try:
            import profiles_module as PM
            pid = PM.get_active_id() or ""
        except Exception:
            swallow.note('screenshots_module.resolve_profile:L63')
            pid = ""
    return pid or "default"


def store_dir(profile_id: str = "") -> str:
    safe = _SAFE_ID.sub("_", resolve_profile(profile_id))[:64]
    path = os.path.join(_profiles_root(), safe, "data", _SUBDIR)
    os.makedirs(path, exist_ok=True)
    _adopt_legacy(path)
    return path


def _adopt_legacy(path: str) -> None:
    """Перенести старые снимки (общий ~/.infinity/screenshots) в первый профиль.

    До профильного хранилища модуль писал в одну папку на всех. Удалять эти
    кадры нельзя, поэтому они переезжают в профиль, который открыл снимки первым,
    и больше никому не перекладываются: маркер ставится один на весь каталог.
    """
    if not os.path.isdir(LEGACY_DIR) or not os.listdir(LEGACY_DIR):
        return
    profiles = _profiles_root()
    marker = os.path.join(profiles, _MIGRATION_MARKER)
    if os.path.exists(marker):
        return
    try:
        os.makedirs(profiles, exist_ok=True)
        moved = 0
        for name in sorted(os.listdir(LEGACY_DIR)):
            src = os.path.join(LEGACY_DIR, name)
            if not os.path.isfile(src) or not name.lower().endswith(".png"):
                continue
            shutil.move(src, os.path.join(path, name))
            moved += 1
        with open(marker, "w", encoding="utf-8") as f:
            f.write("перенесено %d снимков в %s\n" % (moved, path))
        logger.info("[screenshot] старых снимков перенесено: %d -> %s", moved, path)
    except Exception as e:
        logger.warning("[screenshot] перенос старых снимков не удался: %s", e)


def _ensure_dir(profile_id: str = "") -> str:
    return store_dir(profile_id)


def _stamp() -> str:
    now = _dt.datetime.now()
    return now.strftime("shot_%Y-%m-%d_%H-%M-%S_") + "%03d" % (now.microsecond // 1000)


def _new_path(ext="png", profile_id: str = "") -> str:
    return os.path.join(store_dir(profile_id), "%s.%s" % (_stamp(), ext))


def _store(image, ext="png", profile_id: str = "") -> dict:
    path = _new_path(ext, profile_id)
    image.save(path, format="PNG" if ext == "png" else ext.upper())
    return {
        "name": os.path.basename(path),
        "path": path,
        "width": image.width,
        "height": image.height,
        "bytes": os.path.getsize(path),
        "created": os.path.getmtime(path),
    }


def _desktop():
    from PIL import ImageGrab
    # all_screens=True: на нескольких мониторах обычный grab() даёт только
    # монитор с началом координат, а пользователю нужен весь рабочий стол.
    return ImageGrab.grab(all_screens=True)


def _window_rect():
    """Прямоугольник активного окна в координатах съёмки, либо None.

    GetWindowRect и ImageGrab работают в одной системе координат только когда
    процесс знает о реальном масштабе DPI, иначе окно срежется сдвинутой рамкой.
    SetProcessDPIAware() вызывается один раз на процесс сервера: окон у сервера
    нет, рисовать ему нечего, а снимок становится точным при масштабе 125-150%.
    """
    try:
        u = ctypes.windll.user32
        u.SetProcessDPIAware()
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        if rect.right - rect.left < 2 or rect.bottom - rect.top < 2:
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception as e:
        logger.warning("[screenshot] активное окно не измерено: %s", e)
        return None


def _virtual_origin():
    """Начало координат рабочего стола: на втором мониторе слева оно отрицательное.

    Изображение от grab(all_screens=True) покрывает весь виртуальный экран, но
    его левый верхний пиксель — это (SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN), а не
    (0, 0); без этого сдвига кроп уезжает на чужой монитор.
    """
    try:
        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(76)), int(u.GetSystemMetrics(77))
    except Exception:
        swallow.note('screenshots_module._virtual_origin:L179')
        return 0, 0


def capture(kind: str = "full", box=None, profile_id: str = "") -> dict:
    """Сделать снимок; вернуть метаданные сохранённого файла.

    kind=full   — весь рабочий стол (все мониторы);
    kind=window — рамка активного окна;
    kind=region — прямоугольник box=(l,t,r,b) в экранных координатах.
    """
    kind = str(kind or "full").strip().lower()
    if kind not in KINDS:
        raise ValueError("Неизвестный вид снимка: %s" % kind)
    desktop = _desktop()
    if kind == "full":
        image = desktop
    else:
        rect = box if kind == "region" else _window_rect()
        if not rect:
            raise ValueError("Не определена область съёмки")
        ox, oy = _virtual_origin()
        left, top, right, bottom = [int(v) for v in rect]
        x0 = max(0, left - ox)
        y0 = max(0, top - oy)
        x1 = min(desktop.width, right - ox)
        y1 = min(desktop.height, bottom - oy)
        if x1 - x0 < 2 or y1 - y0 < 2:
            raise ValueError("Область съёмки вне экрана")
        image = desktop.crop((x0, y0, x1, y1))
    with _LOCK:
        return _store(image, "png", profile_id)


def _clean_name(name: str) -> str:
    """Имя файла из запроса: только basename, только PNG, без выхода за каталог."""
    base = os.path.basename(str(name or "").strip())
    if base.startswith("."):
        raise ValueError("Некорректное имя снимка")
    if base and not base.lower().endswith(".png"):
        raise ValueError("Редактор сохраняет только PNG")
    return base


def resolve(name: str, profile_id: str = "") -> str:
    """Путь снимка по имени; за пределы каталога профиля не выпускает."""
    base = os.path.basename(str(name or "").strip())
    if not base or not base.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        raise ValueError("Некорректное имя снимка")
    path = os.path.join(store_dir(profile_id), base)
    if not os.path.isfile(path):
        raise FileNotFoundError(base)
    return path


def list_shots(limit: int = 60, profile_id: str = "", show_hidden: bool = False) -> list:
    directory = store_dir(profile_id)
    if not os.path.isdir(directory):
        return []
    hidden = set() if show_hidden else set(hidden_list(profile_id))
    entries = []
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if name in hidden:
            continue
        if os.path.isfile(path) and name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            entries.append({"name": name, "width": None, "height": None,
                            "bytes": os.path.getsize(path),
                            "created": os.path.getmtime(path)})
    entries.sort(key=lambda e: e["created"], reverse=True)
    return entries[:max(1, int(limit))]


def save_bytes(data: bytes, name: str = "", profile_id: str = "") -> dict:
    """Сохранить результат редактирования как новый файл.

    Перезапись исходника не делается: снимок — то, что пользователь мог
    использовать как доказательство, и испорченный редактором кадр должен
    оставаться доступным.
    """
    from PIL import Image
    image = Image.open(io.BytesIO(data))
    image.load()
    clean = _clean_name(name)
    path = os.path.join(store_dir(profile_id), clean) if clean else _new_path("png", profile_id)
    image.save(path, format="PNG")
    return {"name": os.path.basename(path), "path": path,
            "width": image.width, "height": image.height,
            "bytes": os.path.getsize(path)}


def delete(name: str, profile_id: str = "") -> dict:
    path = resolve(name, profile_id)
    trash = os.path.join(store_dir(profile_id), ".trash")
    os.makedirs(trash, exist_ok=True)
    # Снимок удаляется не в никуда: политика проекта — выкинутое переживать,
    # а не стирать, поэтому файл переезжает в подкаталог .trash.
    os.replace(path, os.path.join(trash, os.path.basename(path)))
    return {"ok": True, "name": os.path.basename(path)}


# ---------- закрыть снимок в истории ----------

_HIDDEN_FILE = "hidden.json"


def _hidden_path(profile_id: str = "") -> str:
    return os.path.join(store_dir(profile_id), _HIDDEN_FILE)


def hidden_list(profile_id: str = "") -> list:
    """Имена закрытых (но целых) снимков этого профиля."""
    try:
        with open(_hidden_path(profile_id), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        swallow.note('screenshots_module.hidden_list:L295')
        return []
    return [str(n) for n in (data.get("hidden") or [])]


def set_hidden(name: str, hidden: bool = True, profile_id: str = "") -> dict:
    """Закрыть снимок в списке или открыть снова.

    Закрыть — не удалить: файл остаётся в каталоге профиля, просто перестаёт
    мозолить глаза в недавних. Удаление — это отдельная кнопка и .trash.
    """
    base = os.path.basename(str(name or "").strip())
    if not base.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        raise ValueError("Некорректное имя снимка")
    items = [n for n in hidden_list(profile_id) if n != base]
    if hidden:
        resolve(base, profile_id)      # закрывать можно только существующий кадр
        items.append(base)
    tmp = _hidden_path(profile_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"hidden": sorted(items)}, f, ensure_ascii=False)
    os.replace(tmp, _hidden_path(profile_id))
    return {"ok": True, "name": base, "hidden": bool(hidden), "closed": len(items)}


# ---------- крепление к заметке ----------

def notes_for_pick(query: str = "", limit: int = 300) -> list:
    """Заметки профиля для выбора: id, заголовок, папка, предпросмотр.

    Архивные не предлагаем: прикрепить к архиву можно через саму заметку,
    в списке выбора они только шум.
    """
    import server as S
    cards = (S.notes_list_articles("", "date") or {}).get("articles") or []
    q = str(query or "").strip().lower()
    out = []
    for c in cards:
        folder = str(c.get("folder") or "inbox")
        # Корзина и служебные папки (_ai_answers, _chat_sessions) в списке выбора
        # не нужны: снимок крепится к заметке, с которой пользователь работает.
        if c.get("archived") or c.get("trashed") or folder.startswith("_"):
            continue
        if q and q not in ("%s %s" % (c.get("title", ""), c.get("preview", ""))).lower():
            continue
        out.append({"id": c.get("id"), "title": c.get("title") or "Без названия",
                    "folder": c.get("folder") or "inbox",
                    "updated": c.get("updated") or 0,
                    "preview": (c.get("preview") or "")[:120]})
        if len(out) >= limit:
            break
    return out


def _is_markdown(content: str) -> bool:
    head = (content or "").lstrip()[:60]
    return not (head.startswith("{") and ('"excalidraw"' in content or '"luckysheet"' in content))


def attach_to_note(name: str, note_id: str, profile_id: str = "") -> dict:
    """Прикрепить снимок к заметке: вложение плюс строка markdown в тексте.

    Редактор заметок показывает вложение только когда оно упомянуто в тексте:
    файл, которого нет в markdown, пользователь в заметке не увидит. Заметкам
    сцены (Excalidraw) и таблицы текст не дописываем — там это ломает содержимое,
    поэтому для них остаётся только вложение.
    """
    import server as S
    note_id = str(note_id or "").strip()
    if not note_id:
        raise ValueError("Заметка не выбрана")
    note = S.notes_get_article(note_id)
    if not note or note.get("error"):
        raise ValueError("Заметка не найдена")
    path = resolve(name, profile_id)
    with open(path, "rb") as f:
        data = f.read()
    shot_name = os.path.basename(path)
    up = S.notes_upload_attachment(note_id, shot_name, data)
    if not up or up.get("error"):
        raise ValueError((up or {}).get("error") or "Вложение не принято")
    md = "![%s](%s)" % (shot_name, up["url"])
    content = str(note.get("content") or "")
    embedded = False
    if up.get("is_image") and _is_markdown(content) and up["url"] not in content:
        art = dict(note)
        art["content"] = (content.rstrip() + "\n\n" + md + "\n") if content.strip() else md + "\n"
        saved = S.notes_save_article(art)
        if saved and saved.get("error"):
            raise ValueError(saved["error"])
        embedded = True
    logger.info("[screenshot] %s -> заметка %s (в тексте: %s)", shot_name, note_id, embedded)
    return {"ok": True, "name": shot_name, "note_id": note_id,
            "note_title": note.get("title") or "Без названия",
            "attachment": up.get("filename"), "url": up.get("url"),
            "embedded": embedded}


# ---------- отправка в файловый менеджер ----------

FM_FOLDER = "скриншоты"


def filemanager_dir(profile_id: str = "") -> str:
    """Папка «скриншоты» в корне профиля: там снимок видит файловый менеджер."""
    import profiles_module as PM
    pid = resolve_profile(profile_id)
    root = PM.get_profile_root(pid)
    if not root:
        raise RuntimeError("Корень профиля не определён")
    path = os.path.join(root, FM_FOLDER)
    os.makedirs(path, exist_ok=True)
    return path


def send_to_filemanager(name: str, profile_id: str = "") -> dict:
    """Скопировать снимок в папку профиля и вернуть адрес для перехода.

    Копия, а не перенос: история модуля остаётся полной, даже если пользователь
    потом переименует или удалит файл в файловом менеджере.
    """
    src = resolve(name, profile_id)
    target_dir = filemanager_dir(profile_id)
    base, ext = os.path.splitext(os.path.basename(src))
    target = os.path.join(target_dir, base + ext)
    counter = 1
    while os.path.exists(target):
        # Один и тот же кадр отправлен дважды — второй кладём с суффиксом,
        # иначе первый затрётся без следа.
        target = os.path.join(target_dir, "%s_%d%s" % (base, counter, ext))
        counter += 1
    shutil.copy2(src, target)
    return {"ok": True, "name": os.path.basename(target), "path": target,
            "dir": target_dir, "source": os.path.basename(src)}


def copy_to_clipboard(name: str, profile_id: str = "") -> dict:
    """Положить снимок в буфер обмена как картинку (CF_DIB).

    Через окно редактора это делать нельзя: clipboard-write в Electron отдаёт
    отказ без разрешения, а картинка нужна ровно в том виде, в каком она
    сохранена. DIB — это BMP без 14-байтного файлового заголовка.
    """
    import io as _io
    from PIL import Image
    path = resolve(name, profile_id)
    image = Image.open(path)
    image.load()
    if image.mode != "RGB":
        # CF_DIB с альфа-каналом часть программ показывает с чёрным фоном,
        # а снимок экрана прозрачных пикселей не содержит вовсе.
        image = image.convert("RGB")
    buf = _io.BytesIO()
    image.save(buf, format="BMP")
    dib = buf.getvalue()[14:]

    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    u32.EmptyClipboard.restype = ctypes.c_bool
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]

    CF_DIB = 8
    GMEM_MOVEABLE = 0x0002
    if not u32.OpenClipboard(None):
        raise OSError("Буфер обмена занят другим процессом")
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
        if not handle:
            raise MemoryError("GlobalAlloc не вернул блок")
        ptr = k32.GlobalLock(handle)
        ctypes.memmove(ptr, dib, len(dib))
        k32.GlobalUnlock(handle)
        if not u32.SetClipboardData(CF_DIB, handle):
            raise OSError("SetClipboardData отказал")
        # После успешной передачи буфер владеет блоком — освобождать его нельзя.
    finally:
        u32.CloseClipboard()
    return {"ok": True, "name": os.path.basename(path),
            "width": image.width, "height": image.height}


# ---------- связь с глобальными клавишами и оболочкой ----------

def open_in_shell(shot: dict, mode: str = "edit", profile_id: str = "") -> dict:
    import desktop_bridge
    pid = resolve_profile(profile_id)
    # Профиль в адресе, а не только в команде: окно редактора открывается без
    # нашего запроса, и страница иначе не узнала бы, чью историю показывать.
    # Право доступа всё равно проверяет маршрут по cookie.
    url = "/screenshots?shot=%s&mode=%s&profile=%s" % (shot["name"], mode, pid)
    # tag=shots: оболочка держит одно окно редактора и подменяет в нём снимок,
    # иначе десяток нажатий клавиши оставил бы десяток окон на рабочем столе.
    command = desktop_bridge.push("open-window",
                                  {"url": url, "title": "Скриншот", "tag": "shots",
                                   "profile_id": pid})
    return {"ok": True, "command_id": command["id"], "url": url, "shot": shot}


def take(kind: str = "full", box=None, reveal: bool = True, profile_id: str = "") -> dict:
    """Снять и показать: то, что делает клавиша и кнопка в интерфейсе.

    Реальной рамки выделения на экране у сервера нет (её рисует оболочка, это
    следующий этап), поэтому клавиша области снимает весь рабочий стол и открывает
    редактор сразу в режиме кропа — пользователь выделяет рамку там.
    """
    import module_flags
    kind = str(kind or "full").strip().lower()
    pid = resolve_profile(profile_id)
    # Флаг читаем у того профиля, который снимает: у клавиши это активный, у
    # запроса из редактора — указанный в адресе. Иначе выключенный у одного
    # профиль блокировал бы съёмку у другого.
    if not module_flags.enabled_for("screenshots", pid):
        return {"ok": False, "error": "Модуль скриншотов выключен"}
    mode = "edit"
    if kind == "region" and not box:
        kind, mode = "full", "crop"
    try:
        shot = capture(kind, box, pid)
    except Exception as e:
        logger.error("[screenshot] съёмка %s не удалась: %s", kind, e)
        return {"ok": False, "error": str(e)}
    logger.info("[screenshot] %s -> %s (%dx%d) профиль %s", kind, shot["name"],
                shot["width"], shot["height"], pid)
    if not reveal:
        return {"ok": True, "shot": shot, "profile": pid}
    opened = open_in_shell(shot, mode, pid)
    return {"ok": True, "shot": shot, "opened": opened, "profile": pid}


def _handler(kind: str):
    def handler(event):
        # Поток клавиатуры запроса не имеет: профиль берётся активный на момент
        # нажатия, и снимок уходит именно в его историю.
        take(kind)
    return handler


def attach() -> dict:
    """Зарегистрировать обработчики клавиш; вызывать при старте сервера.

    Обработчик можно повесить и на выключенный модуль: флаги проверяются в
    take(), а привязки клавиш для выключенного модуля снимает hotkey_shell.
    """
    import hotkey_hub
    hub = hotkey_hub.hub()
    hub.on("screenshot.full", _handler("full"))
    hub.on("screenshot.window", _handler("window"))
    hub.on("screenshot.region", _handler("region"))
    hub.on("screenshot.timer", _handler("full"))
    return {"ok": True, "handlers": hub.snapshot()["handlers"]}
