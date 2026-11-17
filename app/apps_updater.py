"""Обновление приложений из defaultapps по официальным GitHub-релизам.

Приложение в defaultapps — это папка с index.html и manifest.json. Почти все
такие папки собраны из GitHub-релизов (web-сборки Rust-приложений), поэтому
обновление сводится к четырём шагам: узнать последний тег, скачать web-архив,
заменить содержимое папки и сохранить пользовательский manifest.json, иконку
и обложку. Архивы и распаковка живут только во временном каталоге внутри
defaultapps и удаляются в finally, независимо от исхода.

Состояние задания отдаётся через state(): прогресс по каждому приложению и
общая строка «что сейчас делается» для админ-панели.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

import apps_flags
import swallow

logger = logging.getLogger("infinity")

GITHUB_API = "https://api.github.com"
USER_AGENT = "InfinitySearch-AppUpdater/1.0"
RELEASE_TTL = 15 * 60
STALE_AFTER = 24 * 60 * 60

# Папка в defaultapps → официальный репозиторий. Порядок важен: более
# длинные префиксы раньше коротких (cadcraft-browser-edition и cadcraft-web —
# разные приложения от разных команд, у обоих в названии «cadcraft»).
REPO_HINTS = (
    ("bimcraft", "getcadcraft/bimcraft"),
    ("cadcraft-browser", "getcadcraft/cadcraft"),
    ("cadcraft-web", "storytold/cadcraft"),
    ("civilcraft", "getcadcraft/civilcraft"),
    ("craft-anything", "storytold/craft-anything"),
    ("detailcraft", "getcadcraft/detailcraft"),
    ("markupcraft", "getcadcraft/markupcraft"),
    ("modelcraft", "getcadcraft/modelcraft"),
    ("partcraft", "getcadcraft/partcraft"),
    ("takeoffcraft", "getcadcraft/takeoffcraft"),
    ("deckcraft", "storytold/deckcraft"),
    ("designcraft", "storytold/designcraft"),
    ("effectcraft", "storytold/effectcraft"),
    ("figcraft", "storytold/figcraft"),
    ("filmcraft", "storytold/filmcraft"),
    ("gridcraft", "storytold/gridcraft"),
    ("lightcraft", "storytold/lightcraft"),
    ("materialcraft", "storytold/materialcraft"),
    ("pdfcraft", "storytold/pdfcraft"),
    ("photocraft", "storytold/photocraft"),
    ("soundcraft", "storytold/soundcraft"),
    ("texturecraft", "storytold/texturecraft"),
    ("vectorcraft", "storytold/vectorcraft"),
    ("wordcraft", "storytold/wordcraft"),
    ("solidcraft", "tangency-labs/solidcraft"),
    ("crraftstudio", "HakanSeven12/OpenCADStudio"),
    ("jumpstart-3d", "MaxSikorski/jumpstart-3d"),
    ("dos", "dividebysandwich/rust-dos"),
    ("jsdos", "caiiiycuk/js-dos"),
    ("x86", "copy/v86"),
)

_PLATFORM_ASSET = re.compile(
    r"(windows|macos|darwin|linux|freebsd|android|ios|portable|appimage|"
    r"\.deb$|\.rpm$|\.dmg$|\.snap$|\.exe$|\.msi$|\.tar\.gz$|\.zsync$|"
    r"source|sha256|\.sig$)",
    re.I,
)
_WEB_ASSET = re.compile(r"(web|browser)[-_.]?", re.I)
_GITHUB_URL = re.compile(r"github\.com[/:]([^/\s]+)/([^/\s#?]+)", re.I)

_lock = threading.RLock()
_job = {
    "running": False,
    "kind": "",
    "started": 0,
    "finished": 0,
    "message": "",
    "total": 0,
    "done": 0,
    "items": {},
}
_releases = {}        # repo -> (ts, release | {"error": ...})

# Настройки автообновления. Значения по умолчанию — выключено: проверка
# ходит в GitHub, а установка пишет в defaultapps, поэтому ни то, ни другое
# не должно случаться без явного согласия администратора.
APPS_SETTINGS_DEFAULTS = {
    "auto_check": False,
    "auto_update": False,
    "notify_updates": False,
    "check_interval_hours": 24,
}

_scheduler_started = False
_scheduler_lock = threading.Lock()


# ---------------------------------------------------------------- каталоги

def apps_root():
    """Каталог defaultapps с учётом конфига (может быть абсолютным путём)."""
    try:
        from config_module import get_config
        path = (get_config() or {}).get("default_apps_dir", "") or ""
    except Exception:
        swallow.note('apps_updater.apps_root:L111')
        path = ""
    if not path:
        try:
            import server
            path = getattr(server, "DEFAULT_APPS_DIR", "") or ""
        except Exception:
            swallow.note('apps_updater.apps_root:L118')
            path = ""
    if not path:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaultapps")
    elif not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    return os.path.normpath(path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        swallow.note('apps_updater._read_json:L133')
        return {}


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _app_manifest(app_dir):
    for name in ("manifest.json", "app.json"):
        p = os.path.join(app_dir, name)
        if os.path.isfile(p):
            return _read_json(p)
    return {}


def _app_name(app_dir, manifest):
    name = str(manifest.get("name") or "").strip()
    if name:
        return name
    index = os.path.join(app_dir, "index.html")
    if os.path.isfile(index):
        try:
            with open(index, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(50000)
            m = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
            if m and m.group(1).strip():
                return re.sub(r"\s+", " ", m.group(1)).strip()
        except Exception:
            swallow.note('apps_updater._app_name:L165')
            pass
    return os.path.basename(app_dir)


# ---------------------------------------------------------------- GitHub

def parse_repo(value):
    """'https://github.com/owner/repo/releases/...' → 'owner/repo'."""
    m = _GITHUB_URL.search(str(value or ""))
    if not m:
        return ""
    repo = m.group(2)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return f"{m.group(1)}/{repo}"


def _repo_hint(app_id):
    low = str(app_id or "").lower()
    for prefix, repo in REPO_HINTS:
        if low == prefix or low.startswith(prefix + "-"):
            return repo
    return ""


def _repo_from_files(app_dir, app_id):
    """Последний шанс: ищем ссылку на свой репозиторий в README/PROVENANCE."""
    token = re.split(r"[-_.]", str(app_id or "").lower())[0]
    if len(token) < 3:
        return ""
    for fname in ("PROVENANCE.md", "README.md", "INSTALL.md", "NOTICE", "manifest.json"):
        path = os.path.join(app_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(200000)
        except Exception:
            swallow.note('apps_updater._repo_from_files:L204')
            continue
        for m in _GITHUB_URL.finditer(text):
            owner, repo = m.group(1), m.group(2)
            if repo.lower() in (token, token + ".git") or repo.lower().startswith(token):
                if repo.lower().endswith(".git"):
                    repo = repo[:-4]
                return f"{owner}/{repo}"
    return ""


def app_repo(app_id, app_dir, manifest=None):
    manifest = manifest if manifest is not None else _app_manifest(app_dir)
    repo = parse_repo(manifest.get("github_repo")) or parse_repo(manifest.get("url"))
    if repo:
        return repo
    repo = _repo_hint(app_id)
    if repo:
        return repo
    return _repo_from_files(app_dir, app_id)


def _http_open(url, timeout=30):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    # Без токена GitHub даёт 60 запросов в час — на 25 приложений хватает
    # ненадолго. Токен читаем из окружения, чтобы не хранить его в репозитории.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _http_json(url, timeout=30):
    with _http_open(url, timeout) as r:
        return json.load(r)


def _http_text(url, timeout=20):
    with _http_open(url, timeout) as r:
        return r.read().decode("utf-8", "replace")


def latest_release(repo, force=False):
    """Последний релиз репозитория. Кэш 15 минут — GitHub даёт 60 запросов/час."""
    now = time.time()
    hit = _releases.get(repo)
    if hit and not force and now - hit[0] < RELEASE_TTL:
        data = hit[1]
        if data.get("error"):
            raise ValueError(data["error"])
        return data
    try:
        data = _http_json(f"{GITHUB_API}/repos/{repo}/releases/latest")
        tag = str(data.get("tag_name") or "").strip()
        assets = [
            {
                "name": a.get("name", ""),
                "size": int(a.get("size") or 0),
                "url": a.get("browser_download_url", ""),
            }
            for a in (data.get("assets") or [])
            if a.get("browser_download_url")
        ]
        info = {
            "repo": repo,
            "tag": tag,
            "version": clean_version(tag),
            "html_url": data.get("html_url") or f"https://github.com/{repo}",
            "assets": assets,
            "asset": pick_asset(assets),
            "published_at": data.get("published_at") or "",
        }
        _releases[repo] = (now, info)
        return info
    except urllib.error.HTTPError as e:
        swallow.note('apps_updater.latest_release:L283', e)
        if e.code == 404:
            msg = "репозиторий или релиз не найден"
        elif e.code in (403, 429):
            msg = "GitHub ограничил частоту запросов, повторите позже"
        else:
            msg = f"GitHub ответил {e.code}"
    except Exception as e:
        swallow.note('apps_updater.latest_release:L291', e)
        msg = f"нет связи с GitHub: {e}"
    _releases[repo] = (now, {"error": msg})
    raise ValueError(msg)


def clean_version(tag):
    """'bimcraft-v0.6.0' → '0.6.0', 'v2.5.1-community.25' → '2.5.1-community.25'."""
    t = str(tag or "").strip()
    m = re.search(r"\d+(?:\.\d+)+(?:[-.][0-9A-Za-z]+)*", t)
    return m.group(0) if m else t.lstrip("vV")


def _version_key(value):
    nums = tuple(int(n) for n in re.findall(r"\d+", str(value or "")))
    words = tuple(w.lower() for w in re.findall(r"[A-Za-z]+", str(value or "")))
    return nums, words


def is_newer(latest, current):
    la, lb = _version_key(latest), _version_key(current)
    if not lb[0]:
        return bool(la[0])
    width = max(len(la[0]), len(lb[0]))
    na = la[0] + (0,) * (width - len(la[0]))
    nb = lb[0] + (0,) * (width - len(lb[0]))
    if na != nb:
        return na > nb
    return la[1] > lb[1]


def pick_asset(assets):
    """Web-сборка релиза: *-web.zip / *-browser-edition-*.zip, иначе один html."""
    zips = []
    for a in assets or []:
        name = str(a.get("name") or "")
        if not name.lower().endswith(".zip"):
            continue
        if _PLATFORM_ASSET.search(name):
            continue
        zips.append(a)
    for a in zips:
        if _WEB_ASSET.search(a["name"]):
            return a
    if zips:
        return zips[0]
    htmls = [a for a in (assets or []) if str(a.get("name") or "").lower().endswith((".html", ".htm"))]
    if len(htmls) == 1:
        return htmls[0]
    return None


# ---------------------------------------------------------------- состояние

def list_apps():
    root = apps_root()
    result = []
    if not os.path.isdir(root):
        return result
    for entry in sorted(os.listdir(root)):
        app_dir = os.path.join(root, entry)
        if entry.startswith(".") or not os.path.isdir(app_dir):
            continue
        manifest = _app_manifest(app_dir)
        version = str(manifest.get("version") or "1.0")
        repo = app_repo(entry, app_dir, manifest)
        cached = _releases.get(repo) if repo else None
        rel = cached[1] if cached else None
        flags = apps_flags.app_flags(entry)
        row = {
            "id": entry,
            "name": _app_name(app_dir, manifest),
            "version": version,
            "repo": repo,
            "url": str(manifest.get("url") or (f"https://github.com/{repo}" if repo else "")),
            "icon": str(manifest.get("icon") or ""),
            "cover": str(manifest.get("cover") or ""),
            "latest_version": "",
            "latest_tag": "",
            "asset": "",
            "update_available": False,
            "error": "",
            "system_only": flags["system_only"],
            "no_auto_install": flags["no_auto_install"],
        }
        if rel:
            if rel.get("error"):
                row["error"] = rel["error"]
            else:
                row["latest_version"] = rel.get("version", "")
                row["latest_tag"] = rel.get("tag", "")
                row["update_available"] = is_newer(rel.get("version"), version)
                row["asset"] = (rel.get("asset") or {}).get("name", "")
        result.append(row)
    return result


def state():
    with _lock:
        job = json.loads(json.dumps(_job))
    return {
        "root": apps_root(),
        "apps": list_apps(),
        "job": job,
        "settings": settings(),
        "auto_last_check": _auto_state().get("last_auto_check", 0),
    }


# ------------------------------------------------------- настройки и уведомления

def settings():
    """Текущие флаги автообновления (config.json → apps)."""
    try:
        from config_module import get_config
        sec = (get_config() or {}).get("apps") or {}
    except Exception:
        swallow.note('apps_updater.settings:L408')
        sec = {}
    out = dict(APPS_SETTINGS_DEFAULTS)
    if isinstance(sec, dict):
        for key in ("auto_check", "auto_update", "notify_updates"):
            if key in sec:
                out[key] = bool(sec[key])
        try:
            hours = int(sec.get("check_interval_hours") or 24)
        except (TypeError, ValueError):
            swallow.note('apps_updater.settings:L418')
            hours = 24
        out["check_interval_hours"] = max(1, min(168, hours))
    # Автоустановка без периодической проверки не имеет смысла: обновлять
    # нечего, пока не узнали о новых версиях. Держим флаги согласованными
    # в коде, а не только в интерфейсе.
    if out["auto_update"]:
        out["auto_check"] = True
    return out


def save_settings(body):
    """Сохранить флаги автообновления в активный профиль."""
    from config_module import get_config, save_config, reload_config
    body = body if isinstance(body, dict) else {}
    cfg = get_config()
    sec = dict((cfg or {}).get("apps") or {})
    for key in ("auto_check", "auto_update", "notify_updates"):
        if key in body:
            sec[key] = bool(body[key])
    if "check_interval_hours" in body:
        try:
            hours = int(body["check_interval_hours"])
        except (TypeError, ValueError):
            swallow.note('apps_updater.save_settings:L442')
            hours = 24
        sec["check_interval_hours"] = max(1, min(168, hours))
    cfg["apps"] = sec
    save_config(cfg)
    reload_config()
    return {"ok": True, "settings": settings()}


def _state_file():
    return os.path.join(apps_root(), ".updates-state.json")


def _auto_state():
    try:
        with open(_state_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        swallow.note('apps_updater._auto_state:L461')
        return {}


def _save_auto_state(data):
    path = _state_file()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("apps state save: %s", e)


def notify_updates():
    """Разослать в ленту /desktop уведомления о доступных обновлениях.

    Одно уведомление на версию приложения: повторный вызов для уже
    отправленной версии ничего не добавляет, иначе кнопка «Уведомить»
    засоряла бы ленту дублями при каждом нажатии.
    """
    from modules.notification_engine import add_notification

    apps = list_apps()
    # Релизы без кэша подтягиваем здесь: на свежем сервере список ещё не
    # проверялся, и без этого кнопка отвечала бы «обновлений нет».
    for a in apps:
        if a.get("repo") and not a.get("latest_version") and not a.get("error"):
            try:
                latest_release(a["repo"])
            except Exception:
                swallow.note('apps_updater.notify_updates:L495')
                pass
    apps = list_apps()

    st = _auto_state()
    notified = st.get("notified") if isinstance(st.get("notified"), dict) else {}
    sent = []
    available = 0
    errors = sum(1 for a in apps if a.get("error"))
    for a in apps:
        if a.get("error") or not a.get("latest_version") or not a.get("update_available"):
            continue
        available += 1
        key = "%s@%s" % (a["id"], a["latest_version"])
        if key in notified:
            continue
        try:
            add_notification(
                "Доступно обновление приложения «%s»: %s → %s. "
                "Обновить можно в админке, вкладка «Приложения»." % (
                    a["name"], a["version"], a["latest_version"]),
                module="apps", type="app-update")
        except Exception as e:
            swallow.note('apps_updater.notify_updates:L518', e)
            return {"ok": False, "error": "Не удалось отправить уведомление: %s" % e}
        notified[key] = {"sent": time.time(), "name": a["name"],
                         "version": a["latest_version"]}
        sent.append({"id": a["id"], "name": a["name"],
                     "version": a["latest_version"]})

    cutoff = time.time() - 180 * 86400
    notified = {k: v for k, v in notified.items()
                if isinstance(v, dict) and (v.get("sent") or 0) > cutoff}
    st["notified"] = notified
    _save_auto_state(st)

    if sent:
        message = "Отправлено уведомлений: %d" % len(sent)
    elif available:
        message = "Уведомления об этих версиях уже отправлены"
    elif errors:
        message = "Не удалось проверить приложений: %d (GitHub недоступен или лимит запросов)" % errors
    else:
        message = "Обновлений нет — уведомлять не о чем"
    return {"ok": True, "sent": len(sent), "available": available,
            "errors": errors, "items": sent, "message": message}


def start_scheduler():
    """Фоновый цикл автообновления. Работает всегда, но без включённых
    флагов не делает ни одного сетевого запроса."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return False
        _scheduler_started = True
    threading.Thread(target=_scheduler_loop, name="apps-auto", daemon=True).start()
    return True


def _wait_job(timeout=1800):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            if not _job["running"]:
                return True
        time.sleep(0.5)
    return False


def _scheduler_loop():
    # Даём серверу подняться, чтобы первый цикл не конкурировал с загрузкой.
    time.sleep(60)
    while True:
        try:
            st = settings()
            if st.get("auto_check"):
                interval = int(st.get("check_interval_hours") or 24) * 3600
                last = _auto_state().get("last_auto_check", 0) or 0
                if time.time() - last >= interval:
                    if start_check()[0]:
                        data = _auto_state()
                        data["last_auto_check"] = time.time()
                        _save_auto_state(data)
                        if _wait_job():
                            if st.get("auto_update"):
                                ids = [a["id"] for a in list_apps()
                                       if a.get("update_available")]
                                if ids:
                                    logger.info("apps auto-update: %s", ", ".join(ids))
                                    if start_update(ids)[0]:
                                        _wait_job()
                            if st.get("notify_updates"):
                                try:
                                    res = notify_updates()
                                    logger.info("apps auto-notify: %s",
                                                res.get("message", ""))
                                except Exception as e:
                                    logger.warning("apps auto-notify failed: %s", e)
        except Exception as e:
            logger.warning("apps auto scheduler: %s", e)
        time.sleep(120)


def _job_set(**kw):
    with _lock:
        _job.update(kw)


def _item(app_id, **kw):
    with _lock:
        cur = _job["items"].setdefault(app_id, {
            "id": app_id,
            "name": app_id,
            "stage": "queued",
            "percent": 0,
            "message": "",
            "version": "",
            "latest_version": "",
            "error": "",
        })
        cur.update(kw)


def _begin(kind, items):
    with _lock:
        if _job["running"]:
            return False
        _job.update({
            "running": True,
            "kind": kind,
            "started": time.time(),
            "finished": 0,
            "message": "Подготовка…",
            "total": len(items),
            "done": 0,
            "items": items,
        })
    return True


def _end(message=""):
    with _lock:
        _job["running"] = False
        _job["finished"] = time.time()
        _job["message"] = message or _job["message"]


def _cleanup_stale(root):
    now = time.time()
    try:
        names = os.listdir(root)
    except OSError:
        swallow.note('apps_updater._cleanup_stale:L648')
        return
    for name in names:
        if not (name.startswith(".update-staging-") or name.startswith(".update-backup-")):
            continue
        path = os.path.join(root, name)
        try:
            if now - os.path.getmtime(path) > STALE_AFTER:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            swallow.note('apps_updater._cleanup_stale:L658')
            pass


# ---------------------------------------------------------------- задания

def start_check():
    apps = list_apps()
    items = {a["id"]: {
        "id": a["id"], "name": a["name"], "stage": "queued", "percent": 0,
        "message": "В очереди", "version": a["version"],
        "latest_version": "", "error": "",
    } for a in apps}
    if not _begin("check", items):
        return False, "Уже выполняется другая операция"
    threading.Thread(target=_run_check, daemon=True).start()
    return True, ""


def _run_check():
    root = apps_root()
    _cleanup_stale(root)
    ids = list(_job["items"].keys())
    for index, app_id in enumerate(ids):
        with _lock:
            if not _job["running"]:
                return
        app_dir = os.path.join(root, app_id)
        _item(app_id, stage="checking", percent=10, message="Запрос последнего релиза…")
        _job_set(message=f"Проверка {_job['items'][app_id]['name']} ({index + 1}/{len(ids)})")
        manifest = _app_manifest(app_dir)
        version = str(manifest.get("version") or "1.0")
        repo = app_repo(app_id, app_dir, manifest)
        if not repo:
            _item(app_id, stage="skip", percent=100, error="",
                  message="Источник не указан")
        else:
            try:
                rel = latest_release(repo)
                latest = rel.get("version", "")
                _item(app_id, stage="done", percent=100, latest_version=latest,
                      message=("Есть обновление " + latest) if is_newer(latest, version)
                              else "Уже последняя версия")
            except Exception as e:
                _item(app_id, stage="error", percent=100, error=str(e), message=str(e))
        with _lock:
            _job["done"] += 1
    _end("Проверка завершена")


def start_update(app_ids, force=False):
    ids = [str(i) for i in (app_ids or []) if str(i).strip()]
    if not ids:
        return False, "Не выбрано ни одного приложения"
    root = apps_root()
    items = {}
    for app_id in ids:
        app_dir = os.path.join(root, app_id)
        if app_id.startswith(".") or not os.path.isdir(app_dir):
            continue
        manifest = _app_manifest(app_dir)
        items[app_id] = {
            "id": app_id, "name": _app_name(app_dir, manifest),
            "stage": "queued", "percent": 0, "message": "В очереди",
            "version": str(manifest.get("version") or "1.0"),
            "latest_version": "", "error": "",
        }
    if not items:
        return False, "Выбранные приложения не найдены"
    if not _begin("update", items):
        return False, "Уже выполняется другая операция"
    threading.Thread(target=_run_update, args=(list(items.keys()), bool(force)), daemon=True).start()
    return True, ""


def _run_update(ids, force):
    root = apps_root()
    _cleanup_stale(root)
    try:
        for index, app_id in enumerate(ids):
            with _lock:
                if not _job["running"]:
                    return
            _job_set(message=f"Обновление {_job['items'][app_id]['name']} ({index + 1}/{len(ids)})")
            app_dir = os.path.join(root, app_id)
            manifest = _app_manifest(app_dir)
            if not app_repo(app_id, app_dir, manifest):
                # «Выбрать все» неизбежно задевает приложения без источника
                # (например code-studio-3). Это не ошибка обновления.
                _item(app_id, stage="skip", percent=100, message="Источник не указан")
                with _lock:
                    _job["done"] += 1
                continue
            try:
                _update_one(app_id, force)
            except Exception as e:
                logger.error("app update %s failed: %s", app_id, e, exc_info=True)
                _item(app_id, stage="error", percent=100, message=str(e), error=str(e))
            with _lock:
                _job["done"] += 1
        _end("Обновление завершено")
    finally:
        _invalidate_apps_cache()


def _update_one(app_id, force=False):
    root = apps_root()
    app_dir = os.path.join(root, app_id)
    manifest = _app_manifest(app_dir)
    version = str(manifest.get("version") or "1.0")
    repo = app_repo(app_id, app_dir, manifest)
    if not repo:
        raise ValueError("Не указан репозиторий GitHub (поле url в manifest.json)")
    _item(app_id, stage="checking", percent=3, message=f"Релиз {repo}…")
    rel = latest_release(repo)
    asset = rel.get("asset") or pick_asset(rel.get("assets"))
    if not asset:
        raise ValueError("В релизе нет web-сборки (zip с web/browser или один html)")
    latest = rel.get("version", "")
    _item(app_id, latest_version=latest)
    if not force and not is_newer(latest, version):
        _item(app_id, stage="done", percent=100,
              message=f"Уже последняя версия {version}")
        return

    staging = tempfile.mkdtemp(prefix=f".update-staging-{app_id}-", dir=root)
    try:
        download = os.path.join(staging, asset["name"])
        _download(asset, download, app_id, rel.get("assets"))
        new_root = _unpack(asset, download, staging, app_id)
        _place_snippets_for_app_js(new_root)

        _item(app_id, stage="installing", percent=88, message="Сохранение манифеста, иконки и обложки")
        new_manifest = _merge_manifest(app_dir, new_root, app_id, repo, latest)
        _copy_preserved(app_dir, new_root, manifest)
        _write_json(os.path.join(new_root, "manifest.json"), new_manifest)

        _item(app_id, stage="installing", percent=94, message="Замена старой версии")
        _replace_dir(new_root, app_dir, app_id)
        _item(app_id, stage="done", percent=100,
              message=f"Обновлено до {latest}", version=latest)
        logger.info("app updated: %s %s -> %s (%s)", app_id, version, latest, repo)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _download(asset, dest, app_id, assets=None):
    name = asset.get("name", "release.zip")
    total = int(asset.get("size") or 0)
    _item(app_id, stage="downloading", percent=5,
          message=f"Скачивание {name}" + (f" ({total // 1048576} МБ)" if total else ""))
    try:
        with _http_open(asset["url"], timeout=60) as r, open(dest, "wb") as f:
            length = int(r.headers.get("Content-Length") or total or 0)
            got = 0
            last_update = 0.0
            while True:
                chunk = r.read(1048576)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last_update > 0.25:
                    last_update = now
                    percent = 5 + int((got / length) * 78) if length else 40
                    _item(app_id, percent=min(83, percent),
                          message=f"Скачивание {name} — {got // 1048576} МБ"
                                  + (f" из {length // 1048576} МБ" if length else ""))
        if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
            raise ValueError("Скачанный файл пуст")
        _verify_checksum(asset, dest, assets)
    except Exception as e:
        raise ValueError(f"Ошибка скачивания: {e}")


def _verify_checksum(asset, path, assets=None):
    """Если в релизе есть SHA256SUMS для нашего файла — сверяем."""
    sums = next((a for a in (assets or []) if "sha256" in a["name"].lower()), None)
    if not sums:
        return
    try:
        text = _http_text(sums["url"])
    except Exception:
        swallow.note('apps_updater._verify_checksum:L841')
        return
    want = ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*").strip() == asset.get("name"):
            want = parts[0].strip().lower()
            break
    if not want:
        return
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h.update(chunk)
    if h.hexdigest() != want:
        raise ValueError("контрольная сумма SHA256 не совпала")


def _unpack(asset, download, staging, app_id):
    extract_dir = os.path.join(staging, "extract")
    os.makedirs(extract_dir, exist_ok=True)
    if asset["name"].lower().endswith((".html", ".htm")):
        shutil.move(download, os.path.join(extract_dir, asset["name"]))
    else:
        _item(app_id, stage="extracting", percent=84, message="Распаковка архива")
        try:
            with zipfile.ZipFile(download) as zf:
                for member in zf.namelist():
                    target = os.path.realpath(os.path.join(extract_dir, member))
                    base = os.path.realpath(extract_dir)
                    if target != base and not target.startswith(base + os.sep):
                        raise ValueError(f"небезопасный путь в архиве: {member}")
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            raise ValueError("скачанный архив повреждён")
        os.remove(download)

    top = [e for e in os.listdir(extract_dir) if e != "__MACOSX"]
    root = extract_dir
    if len(top) == 1 and os.path.isdir(os.path.join(extract_dir, top[0])):
        root = os.path.join(extract_dir, top[0])
    if not _looks_like_app(root):
        raise ValueError("в архиве нет приложения (index.html или manifest.json)")
    return root


_SNIPPET_IMPORT = re.compile(r"""from\s+['"]\./snippets/""")


def _place_snippets_for_app_js(root):
    """snippets/ рядом с app-модулем, если сборка импортирует их оттуда.

    Факт 04.11: web-архив photocraft 0.20.1 кладёт wasm-bindgen glue в
    ``app/photocraft-web.js``, а snippet — в корень ``snippets/``. Модуль
    импортирует ``./snippets/eframe-.../inline0.js``, браузер ищет его в
    ``app/snippets/`` и получает 404 — приложение навсегда висит на «Loading».
    Корневую раскладку не трогаем, только копируем каталог рядом с модулем.
    """
    src = os.path.join(root, "snippets")
    if not os.path.isdir(src):
        return 0
    copied = 0
    for name in os.listdir(root):
        folder = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(folder):
            continue
        try:
            files = os.listdir(folder)
        except OSError:
            swallow.note('apps_updater._place_snippets_for_app_js:L911')
            continue
        for fname in files:
            if not fname.lower().endswith((".js", ".mjs")):
                continue
            path = os.path.join(folder, fname)
            try:
                # Импорт стоит в первой строке модуля — хватает начала файла.
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(65536)
            except OSError:
                swallow.note('apps_updater._place_snippets_for_app_js:L922')
                continue
            if not _SNIPPET_IMPORT.search(head):
                continue
            dst = os.path.join(folder, "snippets")
            if os.path.isdir(dst):
                continue
            try:
                shutil.copytree(src, dst)
            except OSError as e:
                logger.warning("app: не удалось скопировать snippets для %s: %s", path, e)
                continue
            copied += 1
            logger.info("app: snippets скопированы рядом с %s", path)
    return copied


def _looks_like_app(path):
    try:
        names = os.listdir(path)
    except OSError:
        swallow.note('apps_updater._looks_like_app:L891')
        return False
    low = [n.lower() for n in names]
    if "index.html" in low or "manifest.json" in low or "app.json" in low:
        return True
    return any(n.endswith((".html", ".htm")) for n in low)


def _local_asset_missing(value, dirs):
    """Ссылка на локальный файл, которого нет ни в старой, ни в новой папке.

    Внешние URL, иконки Font Awesome и эмодзи локальными не считаются:
    их нельзя проверить и незачем переписывать.
    """
    v = str(value or "").strip()
    if not v:
        return True
    low = v.lower()
    if low.startswith(("http://", "https://")) or " fa-" in low or low.startswith("fa-"):
        return False
    if len(v) <= 4 and "." not in v:
        return False
    name = _asset_basename(v)
    if not name:
        return True
    return not any(os.path.isfile(os.path.join(d, name)) for d in dirs)


def _merge_manifest(old_dir, new_dir, app_id, repo, version):
    old = _app_manifest(old_dir)
    new = _app_manifest(new_dir)
    merged = dict(old)
    for key, value in new.items():
        merged.setdefault(key, value)
    merged["id"] = str(old.get("id") or app_id)
    merged["version"] = version
    merged["url"] = f"https://github.com/{repo}"
    merged["github_repo"] = repo
    main = str(merged.get("main") or "")
    if not main or not os.path.exists(os.path.join(new_dir, main)):
        if os.path.exists(os.path.join(new_dir, "index.html")):
            merged["main"] = "index.html"
        else:
            htmls = sorted(n for n in os.listdir(new_dir)
                           if n.lower().endswith((".html", ".htm"))
                           and os.path.isfile(os.path.join(new_dir, n)))
            if htmls:
                merged["main"] = htmls[0]
    # Если манифест ссылается на иконку/обложку, а файла нет ни в старой
    # папке, ни в релизе (у crraftstudio и jumpstart-3d так было изначально),
    # берём подходящий файл из новой сборки вместо битой ссылки.
    for key, candidates in (
        ("icon", ("icon.svg", "icon.png", "logo.svg", "logo.png")),
        ("cover", ("cover.svg", "cover.png", "cover.jpg", "cover.jpeg")),
    ):
        ref = str(merged.get(key) or "").strip()
        foreign = False
        # /app-files/<чужой id>/... — ссылка на файл другого приложения.
        # Такие обложки остались в crraftstudio и jumpstart-3d; если релиз
        # принёс свою, привязываем к своей папке.
        if ref.startswith("/app-files/"):
            owner = ref[len("/app-files/"):].split("/", 1)[0]
            foreign = bool(owner) and owner != app_id
        if not foreign and not _local_asset_missing(merged.get(key), (old_dir, new_dir)):
            continue
        found = _first_file(new_dir, candidates)
        if not found:
            continue
        merged[key] = found if key == "icon" else f"/app-files/{app_id}/{found}"
    return merged


def _asset_basename(value):
    """Путь из манифеста → имя файла внутри папки приложения."""
    v = str(value or "").strip()
    if not v:
        return ""
    v = v.split("?", 1)[0].split("#", 1)[0]
    name = v.rstrip("/").rsplit("/", 1)[-1].replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in (".", "..") or ".." in name:
        return ""
    return name


def _copy_preserved(old_dir, new_dir, manifest):
    """Иконка и обложка остаются прежними, даже если релиз привёз свои."""
    for key in ("icon", "cover"):
        name = _asset_basename(manifest.get(key))
        if not name:
            continue
        src = os.path.join(old_dir, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(new_dir, name)
        try:
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
        except Exception as e:
            logger.warning("app update: не удалось сохранить %s: %s", name, e)


def _replace_dir(new_root, dest, app_id):
    parent = os.path.dirname(dest)
    backup = os.path.join(parent, f".update-backup-{app_id}")
    if os.path.exists(backup) and not os.path.exists(dest):
        # Прошлая замена оборвалась между двумя rename: старая версия цела
        # в backup, возвращаем её на место, чтобы не потерять приложение.
        os.rename(backup, dest)
    elif os.path.exists(backup):
        shutil.rmtree(backup, ignore_errors=True)
    moved = False
    if os.path.exists(dest):
        os.rename(dest, backup)
        moved = True
    try:
        os.rename(new_root, dest)
    except Exception:
        if moved:
            os.rename(backup, dest)
        raise
    if moved:
        shutil.rmtree(backup, ignore_errors=True)


# ---------------------------------------------------------------- добавление

def start_add(repo_value, app_id=""):
    repo = parse_repo(repo_value)
    if not repo:
        return False, "Нужна ссылка вида https://github.com/owner/repo"
    key = "__add__:" + repo
    items = {key: {
        "id": key, "name": repo, "stage": "queued", "percent": 0,
        "message": "В очереди", "version": "", "latest_version": "", "error": "",
    }}
    if not _begin("add", items):
        return False, "Уже выполняется другая операция"
    threading.Thread(target=_run_add, args=(repo, key, str(app_id or "").strip()),
                     daemon=True).start()
    return True, ""


def _run_add(repo, key, app_id):
    root = apps_root()
    _cleanup_stale(root)
    staging = None
    try:
        _job_set(message=f"Добавление {repo}")
        _item(key, stage="checking", percent=3, message="Релиз GitHub…")
        rel = latest_release(repo)
        asset = rel.get("asset") or pick_asset(rel.get("assets"))
        if not asset:
            raise ValueError("В релизе нет web-сборки (zip с web/browser или один html)")
        version = rel.get("version", "")
        _item(key, latest_version=version)
        name_hint = app_id or repo.split("/")[-1]
        dest_id = _safe_id(name_hint)
        if not dest_id:
            raise ValueError("Некорректный id приложения")
        dest = os.path.join(root, dest_id)
        if os.path.exists(dest):
            raise ValueError(f"Папка {dest_id} уже существует — используйте обновление")

        staging = tempfile.mkdtemp(prefix=f".update-staging-{dest_id}-", dir=root)
        download = os.path.join(staging, asset["name"])
        _download(asset, download, key, rel.get("assets"))
        new_root = _unpack(asset, download, staging, key)
        _place_snippets_for_app_js(new_root)

        _item(key, stage="installing", percent=90, message="Запись манифеста")
        manifest = _app_manifest(new_root)
        manifest["id"] = str(manifest.get("id") or dest_id)
        manifest["version"] = version
        manifest["url"] = f"https://github.com/{repo}"
        manifest["github_repo"] = repo
        manifest.setdefault("name", _human_name(repo.split("/")[-1] or dest_id))
        manifest.setdefault("type", "html")
        if not manifest.get("main"):
            htmls = sorted(n for n in os.listdir(new_root)
                           if n.lower().endswith((".html", ".htm"))
                           and os.path.isfile(os.path.join(new_root, n)))
            manifest["main"] = htmls[0] if htmls else "index.html"
        if not manifest.get("icon"):
            icon = _first_file(new_root, ("icon.svg", "icon.png", "logo.svg", "logo.png"))
            if icon:
                manifest["icon"] = icon
        if not manifest.get("cover"):
            cover = _first_file(new_root, ("cover.svg", "cover.png", "cover.jpg"))
            if cover:
                # /app-files/... — так обложку ждёт страница приложения:
                # относительный путь там не разрешается.
                manifest["cover"] = f"/app-files/{dest_id}/{cover}"
        _write_json(os.path.join(new_root, "manifest.json"), manifest)

        _item(key, stage="installing", percent=95, message="Установка")
        os.rename(new_root, dest)
        _item(key, stage="done", percent=100, version=version,
              name=str(manifest.get("name") or dest_id),
              message=f"Установлено {dest_id} {version}")
        logger.info("app added: %s %s from %s", dest_id, version, repo)
    except Exception as e:
        logger.error("app add %s failed: %s", repo, e, exc_info=True)
        _item(key, stage="error", percent=100, message=str(e), error=str(e))
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        _end("Добавление завершено")
        _invalidate_apps_cache()


def _first_file(folder, names):
    for name in names:
        if os.path.isfile(os.path.join(folder, name)):
            return name
    return ""


def _human_name(value):
    words = [w for w in re.split(r"[-_.]+", str(value or "")) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _safe_id(value):
    value = re.sub(r"[\\/]+", "_", str(value or "").strip())
    value = re.sub(r"[^\w.\-]", "_", value).strip(".-_ ")
    return value[:80]


def _invalidate_apps_cache():
    try:
        # "import main" здесь сработает только если приложение уже загружено под
        # именем main. В frozen-сборке main.py исполняется не как __main__, а
        # импорт по имени наткнулся бы на заглушку из шапки main.py, поэтому
        # берём живой модуль из sys.modules под любым из его имён.
        for _name in ("main", "__main__", "app.main"):
            _m = sys.modules.get(_name)
            if _m is not None and hasattr(_m, "_apps_cache_invalidate"):
                _m._apps_cache_invalidate()
                return
    except Exception:
        swallow.note('apps_updater._invalidate_apps_cache:L1130')
        pass
