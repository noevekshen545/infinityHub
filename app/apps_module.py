#!/usr/bin/env python3
"""
Модуль для работы с HTML приложениями.
Каждое приложение — папка с index.html и ресурсами.

Поддержка профилей:
- Каждый профиль имеет свою директорию apps (data/profiles/<id>/data/apps/)
- Стандартные приложения (default_apps_dir) видны всем
- Пользовательские приложения привязаны к профилю-владельцу
- shared_with: [] = приватно, ["*"] = всем, ["id1","id2"] = конкретным профилям
"""

import os
import re
import json
import sys
import time
import shutil
import zipfile
import tempfile
import subprocess
import threading
from html.parser import HTMLParser

import apps_flags
import swallow


class AppMetaParser(HTMLParser):
    """Парсер для извлечения метаданных приложения из index.html."""
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            attrs_dict = dict(attrs)
            name = attrs_dict.get("name", attrs_dict.get("property", "")).lower()
            if name in ("description", "og:description"):
                self.description = attrs_dict.get("content", "")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title = data.strip()


def parse_app_meta(app_dir):
    """Извлечь метаданные приложения."""
    index_path = os.path.join(app_dir, "index.html")
    title, description = "", ""
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(50000)
            parser = AppMetaParser()
            parser.feed(content)
            title = parser.title
            description = parser.description
        except Exception:
            swallow.note('apps_module.parse_app_meta:L67')
            pass
    return title, description


def load_app_json(app_dir):
    """Загрузить app.json или manifest.json если есть."""
    for fname in ['app.json', 'manifest.json']:
        json_path = os.path.join(app_dir, fname)
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                swallow.note('apps_module.load_app_json:L81')
                pass
    return {}


def _is_app_visible_to_profile(app_json, profile_id, is_default):
    """Проверить, видно ли приложение данному профилю.

    Правила видимости:
    - is_default=True -> видно всем
    - shared_with не задан или [] -> видно только владельцу (created_by)
    - shared_with=["*"] -> видно всем
    - shared_with=["id1","id2"] -> видно указанным профилям
    - Если created_by не задан -> приложение старое, видно всем (обратная совместимость)
    """
    if is_default:
        return True

    created_by = app_json.get("created_by", "")
    shared_with = app_json.get("shared_with", None)

    # Старые приложения без created_by — видны всем (обратная совместимость)
    if not created_by:
        return True

    # Нет профиля — показываем все (для не-профильных контекстов)
    if not profile_id:
        return True

    # Владелец видит всегда
    if created_by == profile_id:
        return True

    # shared_with не задан или пуст — приватно
    if not shared_with:
        return False

    # shared_with=["*"] — всем
    if "*" in shared_with:
        return True

    # shared_with содержит id профиля
    return profile_id in shared_with


def _pretty_name(folder_id):
    """Человеческое имя из id папки, если manifest.json отсутствует.

    Приложения вроде musicmachine и video не имеют манифеста, поэтому в
    списке и на панели задач они выглядели как «musicmachine» вместо
    «Music Machine».
    """
    words = str(folder_id or "").replace("_", "-").replace(".", "-").split("-")
    words = [w for w in words if w]
    if not words:
        return str(folder_id or "")
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _build_app_record(full_path, app_json, is_default, entry):
    """Собрать запись приложения из метаданных."""
    title, description = parse_app_meta(full_path)
    manifest_path = os.path.join(full_path, "manifest.json")
    has_manifest = os.path.exists(manifest_path)
    has_index = os.path.exists(os.path.join(full_path, "index.html"))

    if app_json:
        title = app_json.get("name", title)
        description = app_json.get("description", description)
        icon = app_json.get("icon", "")
        screenshots = app_json.get("screenshots", [])
        version = app_json.get("version", "1.0")
        category = app_json.get("category", "")
    else:
        icon = ""
        screenshots = []
        version = "1.0"
        category = ""
        for fname in os.listdir(full_path):
            if fname.lower() in ("icon.png", "logo.png", "icon.svg"):
                icon = fname
            if fname.lower().startswith("screenshot") or fname.lower().startswith("screen"):
                screenshots.append(fname)

    mtime = os.path.getmtime(manifest_path if has_manifest else (os.path.join(full_path, "index.html") if has_index else full_path))
    size = sum(
        os.path.getsize(os.path.join(full_path, f))
        for f in os.listdir(full_path)
        if os.path.isfile(os.path.join(full_path, f))
    )

    screenshots_dir = os.path.join(full_path, "screenshots")
    if os.path.isdir(screenshots_dir):
        for sf in os.listdir(screenshots_dir):
            if sf.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                if sf not in screenshots:
                    screenshots.append(sf)

    return {
        "id": entry,
        "dir": full_path,
        "name": title or _pretty_name(entry),
        "description": description,
        "icon": icon,
        "screenshots": screenshots,
        "version": version,
        "category": category,
        "size": size,
        "mtime": mtime,
        "has_server": os.path.exists(os.path.join(full_path, "server.py")),
        "type": app_json.get("type", "app"),
        "background": app_json.get("background", False),
        "main": app_json.get("main", "index.html"),
        "windowWidth": app_json.get("windowWidth", 800),
        "windowHeight": app_json.get("windowHeight", 600),
        "needsServer": app_json.get("needsServer", False),
        "serverPort": app_json.get("serverPort", 8000),
        "idleTimeout": app_json.get("idleTimeout", 10),
        "serverCmd": app_json.get("serverCmd", ""),
        "isBackground": app_json.get("isBackground", False),
        "url": app_json.get("url", ""),
        "cover": app_json.get("cover", ""),
        "defaultView": app_json.get("defaultView", "iframe"),
        "is_default": bool(is_default),
        "created_by": app_json.get("created_by", ""),
        "shared_with": app_json.get("shared_with", []),
    }


def _app_accepts_file(full_path, main_file):
    """Умеет ли приложение само открыть файл из ?file= без эмуляции drop.

    Определяем по разметке точки входа: приложение, которое читает
    query-параметр, не нуждается в автозагрузчике main.py. Это подсказка
    для диалога «Открыть с помощью…», а не гарантия — поэтому в диалоге
    пользователь всё равно может включить эмуляцию перетаскивания.
    """
    if not main_file:
        return False
    try:
        path = os.path.join(full_path, str(main_file))
        if not os.path.isfile(path) or os.path.getsize(path) > 2 * 1024 * 1024:
            return False
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            html = f.read()
    except OSError:
        swallow.note('apps_module._app_accepts_file:L227')
        return False
    return any(m in html for m in (
        "openFromQuery", "get('file')", 'get("file")',
        "searchParams.get('file')", 'searchParams.get("file")',
    ))


def _flags_for(app_id):
    """Флаги приложения из config.json; битый конфиг не роняет список."""
    try:
        return apps_flags.app_flags(app_id)
    except Exception:
        swallow.note('apps_module._flags_for:L240')
        return dict(apps_flags.DEFAULTS)


def find_apps(dirs, default_dir=None, profile_id=None):
    """Найти все приложения в указанных директориях.

    Args:
        dirs: Список директорий для сканирования (общие)
        default_dir: Директория стандартных приложений (is_default=True, видны всем)
        profile_id: ID текущего профиля. Если задан, добавляет сканирование
                     профильной директории apps и фильтрует по видимости.
    """
    results = []
    _abs_default = os.path.abspath(default_dir) if default_dir else None

    for d in dirs:
        if not os.path.isdir(d):
            continue
        for entry in os.listdir(d):
            # Служебные каталоги обновлятора (.update-staging-*, .update-backup-*)
            # лежат внутри defaultapps и не должны попадать в список приложений.
            if entry.startswith("."):
                continue
            full_path = os.path.join(d, entry)
            if not os.path.isdir(full_path):
                if entry.lower().endswith(('.html', '.htm')):
                    title, description = "", ""
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read(50000)
                        parser = AppMetaParser()
                        parser.feed(content)
                        title = parser.title
                        description = parser.description
                    except Exception:
                        swallow.note('apps_module.find_apps:L276')
                        pass
                    size = os.path.getsize(full_path)
                    mtime = os.path.getmtime(full_path)
                    is_default = _abs_default and os.path.abspath(full_path).startswith(_abs_default)
                    app_record = {
                        "id": entry,
                        "dir": full_path,
                        "name": title or entry,
                        "description": description,
                        "icon": "",
                        "screenshots": [],
                        "version": "1.0",
                        "category": "",
                        "size": size,
                        "mtime": mtime,
                        "has_server": False,
                        "type": "app",
                        "is_file": True,
                        "is_default": bool(is_default),
                        "created_by": "",
                        "shared_with": [],
                    }
                    if _is_app_visible_to_profile({}, profile_id, is_default):
                        flags = _flags_for(entry)
                        if flags["system_only"]:
                            continue
                        app_record["system_only"] = False
                        app_record["no_auto_install"] = flags["no_auto_install"]
                        results.append(app_record)
                continue

            index_path = os.path.join(full_path, "index.html")
            manifest_path = os.path.join(full_path, "manifest.json")
            has_index = os.path.exists(index_path)
            has_manifest = os.path.exists(manifest_path)

            app_json = load_app_json(full_path)

            # Точка входа: index.html → манифест → первый *.html верхнего
            # уровня. Раньше папка без index.html/manifest.json просто
            # отбрасывалась (строки «if not has_index and not has_manifest:
            # continue»), из-за чего musicmachine и video — у них точки
            # входа music.html и videoeddit.html — не попадали в /api/apps
            # вообще и были недоступны ни файловому менеджеру, ни десктопу.
            #
            # Имя переменной НЕ entry: entry здесь — имя папки из цикла
            # os.listdir(d), оно уходит в id записи. Присваивание
            # entry = "index.html" перетирало id, и все приложения
            # приходили как «index.html».
            main_file = None
            if has_index:
                main_file = "index.html"
            elif app_json.get("main") and os.path.exists(
                    os.path.join(full_path, str(app_json["main"]))):
                main_file = str(app_json["main"])
            else:
                try:
                    for fn in sorted(os.listdir(full_path)):
                        if fn.lower().endswith((".html", ".htm")) and os.path.isfile(
                                os.path.join(full_path, fn)):
                            main_file = fn
                            break
                except OSError:
                    swallow.note('apps_module.find_apps:L340')
                    main_file = None

            if not main_file:
                # Приложения-ссылки (type=link) не имеют HTML-точки входа:
                # их открывают по url. Без этого Docker-контейнеры, созданные
                # кнопкой «на Desktop», отбрасывались и не попадали в
                # /api/apps — иконка не появлялась.
                _is_link = (str(app_json.get("type") or "").lower() == "link"
                            and bool(str(app_json.get("url") or "").strip()))
                if not _is_link:
                    # Нет ни манифеста, ни единой html-страницы — это не
                    # приложение (например пустая папка в defaultapps).
                    continue

            is_default = _abs_default and os.path.abspath(full_path).startswith(_abs_default)

            if not _is_app_visible_to_profile(app_json, profile_id, is_default):
                continue

            flags = _flags_for(entry)
            if flags["system_only"]:
                continue
            record = _build_app_record(full_path, app_json, is_default, entry)
            record["system_only"] = False
            record["no_auto_install"] = flags["no_auto_install"]
            record["main"] = main_file or ""
            # Подсказка «приложение принимает файл параметром» для диалога
            # «Открыть с помощью…» в файловом менеджере.
            record["acceptsFile"] = _app_accepts_file(full_path, main_file)
            results.append(record)

    results.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return results


def share_app(app_dir, shared_with, profile_id=None):
    """Обновить права доступа к приложению.

    Args:
        app_dir: Путь к директории приложения
        shared_with: Список профилей, [] = приватно, ["*"] = всем, ["id1"] = конкретным
        profile_id: ID профиля-владельца (для проверки)

    Returns:
        dict с результатом
    """
    manifest_path = os.path.join(app_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return {"error": "App manifest not found"}

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        swallow.note('apps_module.share_app:L395', e)
        return {"error": f"Failed to read manifest: {e}"}

    if profile_id and manifest.get("created_by") and manifest["created_by"] != profile_id:
        return {"error": "Only the app owner can change sharing settings"}

    manifest["shared_with"] = shared_with

    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        swallow.note('apps_module.share_app:L407', e)
        return {"error": f"Failed to save manifest: {e}"}

    return {"ok": True, "shared_with": shared_with}


# Хранилище запущенных серверов
_running_app_servers = {}


def start_app_server(app_dir, port=8888):
    """Запустить HTTP сервер для приложения."""
    global _running_app_servers

    for key, proc in list(_running_app_servers.items()):
        if proc.poll() is None:
            if key.startswith(f"{app_dir}:"):
                return {"ok": True, "port": int(key.split(":")[-1]), "already_running": True}

    key = f"{app_dir}:{port}"
    try:
        # Раньше здесь жёстко стоял «python3» — на Windows такой исполняемый
        # файл отсутствует, и сервер приложения не запускался вовсе.
        # sys.executable — тот же интерпретатор, на котором работает сам сервер.
        # CREATE_NO_WINDOW — иначе каждый запуск мигает чёрным окном консоли.
        popen_kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} \
            if os.name == "nt" else {}
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "http.server", str(port)],
            cwd=app_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kw
        )
        _running_app_servers[key] = proc

        def check_server():
            time.sleep(1)
            if proc.poll() is not None:
                _running_app_servers.pop(key, None)

        threading.Thread(target=check_server, daemon=True).start()

        return {"ok": True, "port": port, "pid": proc.pid}
    except Exception as e:
        swallow.note('apps_module.start_app_server:L452', e)
        return {"ok": False, "error": str(e)}


def stop_app_server(app_dir, port=None):
    """Остановить сервер приложения."""
    global _running_app_servers

    if port:
        key = f"{app_dir}:{port}"
        proc = _running_app_servers.pop(key, None)
        if proc and proc.poll() is None:
            proc.terminate()
            return {"ok": True}
    else:
        stopped = []
        for key in list(_running_app_servers.keys()):
            if key.startswith(f"{app_dir}:"):
                proc = _running_app_servers.pop(key)
                if proc.poll() is None:
                    proc.terminate()
                    stopped.append(key.split(":")[-1])
        return {"ok": True, "stopped_ports": stopped}

    return {"ok": False, "error": "Server not found"}


def get_running_servers():
    """Получить список запущенных серверов."""
    global _running_app_servers
    result = []
    for key, proc in list(_running_app_servers.items()):
        if proc.poll() is None:
            parts = key.rsplit(":", 1)
            result.append({"app_dir": parts[0], "port": int(parts[1]), "pid": proc.pid})
        else:
            _running_app_servers.pop(key, None)
    return result


# Кэш
_app_cache = {}
_app_cache_time = 0
_app_cache_ttl = 300


def get_apps_cached(dirs, force=False):
    """Получить приложения с кэшированием."""
    global _app_cache, _app_cache_time
    now = time.time()
    cache_key = "|".join(sorted(dirs))

    if not force and cache_key in _app_cache and (now - _app_cache_time) < _app_cache_ttl:
        return _app_cache[cache_key]

    _app_cache[cache_key] = find_apps(dirs)
    _app_cache_time = now
    return _app_cache[cache_key]


def _safe_app_id(name):
    """Превратить имя в безопасный id папки: без слэшей, «..», точек в начале."""
    name = str(name or "").strip().strip("/\\")
    name = re.sub(r'[\\/]+', '_', name)
    name = re.sub(r'[^\w.\-]', '_', name).strip('.-_ ')
    return name[:80]


def _looks_like_app(path):
    """Есть ли у папки точка входа (то, что принимает find_apps)."""
    try:
        entries = os.listdir(path)
    except OSError:
        swallow.note('apps_module._looks_like_app:L525')
        return False
    lowered = [e.lower() for e in entries]
    if 'manifest.json' in lowered or 'app.json' in lowered:
        return True
    return any(e.endswith(('.html', '.htm')) for e in lowered)


def install_app_from_zip(zip_bytes, apps_dir, profile_id=None):
    """Установить приложение из zip-архива в apps_dir.

    Архив может содержать приложение как в корне, так и в одной папке верхнего
    уровня. Определяет id из manifest.json (поле id) либо из имени папки/архива,
    распаковывает с защитой от «zip slip», и проставляет created_by = profile_id,
    чтобы загруженное приложение было приватно владельцу (как и созданное через
    /api/apps/create). Возвращает dict с результатом.
    """
    import io

    if not zip_bytes:
        return {"ok": False, "error": "Пустой архив"}
    try:
        bio = io.BytesIO(zip_bytes)
        if not zipfile.is_zipfile(bio):
            return {"ok": False, "error": "Это не zip-архив"}
        bio.seek(0)
        tmp = tempfile.mkdtemp(prefix="appzip_")
        tmp_real = os.path.realpath(tmp)
        with zipfile.ZipFile(bio) as zf:
            for member in zf.namelist():
                target = zf.extract(member, tmp)
                # Защита от zip slip: извлечённый путь обязан остаться внутри tmp.
                real = os.path.realpath(target)
                if real != tmp_real and not real.startswith(tmp_real + os.sep):
                    shutil.rmtree(tmp, ignore_errors=True)
                    return {"ok": False, "error": f"Небезопасный путь в архиве: {member}"}
    except zipfile.BadZipFile:
        swallow.note('apps_module.install_app_from_zip:L562')
        return {"ok": False, "error": "Повреждённый zip-архив"}
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "error": f"Ошибка распаковки: {e}"}

    try:
        # Пропустить служебные каталоги (__MACOSX) и выбрать корень приложения.
        top = [e for e in os.listdir(tmp) if e != '__MACOSX']
        app_root = tmp
        if len(top) == 1 and os.path.isdir(os.path.join(tmp, top[0])):
            app_root = os.path.join(tmp, top[0])
            base_name = top[0]
        else:
            base_name = None

        if not _looks_like_app(app_root):
            shutil.rmtree(tmp, ignore_errors=True)
            return {"ok": False, "error": "В архиве нет приложения (нет index.html или manifest.json)"}

        # id: из манифеста, иначе из имени папки/архива.
        app_id = ""
        app_name = ""
        for fname in ("manifest.json", "app.json"):
            mpath = os.path.join(app_root, fname)
            if os.path.exists(mpath):
                try:
                    with open(mpath, "r", encoding="utf-8") as f:
                        m = json.load(f)
                    app_id = str(m.get("id", "") or "")
                    app_name = str(m.get("name", "") or "")
                except Exception:
                    swallow.note('apps_module.install_app_from_zip:L594')
                    pass
                break
        if not app_id:
            app_id = base_name or ""
        app_id = _safe_app_id(app_id)
        if not app_id:
            shutil.rmtree(tmp, ignore_errors=True)
            return {"ok": False, "error": "Не удалось определить id приложения"}

        dest = os.path.join(apps_dir, app_id)
        if os.path.abspath(dest) == os.path.abspath(app_root):
            shutil.rmtree(tmp, ignore_errors=True)
            return {"ok": False, "error": "Некорректный id приложения"}
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        shutil.move(app_root, dest)

        # Проставляем владельца, если он ещё не задан (как у /api/apps/create).
        manifest_path = os.path.join(dest, "manifest.json")
        manifest = {}
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                swallow.note('apps_module.install_app_from_zip:L620')
                manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        manifest.setdefault("id", app_id)
        if not app_name:
            app_name = str(manifest.get("name", "") or _pretty_name(app_id))
        manifest["name"] = manifest.get("name") or app_name
        if not manifest.get("created_by"):
            manifest["created_by"] = profile_id or ""
        manifest.setdefault("shared_with", [])
        try:
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
        except Exception as e:
            swallow.note('apps_module.install_app_from_zip:L635', e)
            return {"ok": False, "error": f"Приложение установлено, но манифест не записан: {e}"}

        return {"ok": True, "id": app_id, "name": manifest.get("name", app_id)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
