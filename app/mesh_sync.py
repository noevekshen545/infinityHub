"""mesh_sync — multi-master репликация заметок и новостей (mesh.write_mode=lww).

Флаг `decentral.mesh.write_mode` до 2026-11-01 читался только санитайзером:
панель его сохраняла, а в коде не было ни одного пути, который бы его видел.
Здесь у него появляется потребитель — режим `lww` (last-write-wins).

Что синхронизируется и почему именно так.
  * Заметки и статьи новостей лежат обычными файлами на диске, поэтому
    синхронизация работает на уровне файлов, а не базы: ключ записи — это
    `notes/<путь>` или `news/<файл>`, версия — mtime в наносекундах.
  * Разрешение конфликтов — последняя запись побеждает. Это честный и
    единственный осмысленный выбор для файловой модели без общего часового
    пояса: обе копии хранят mtime, и большая версия выигрывает. CRDT здесь не
    при чём: слить две правки одной заметки на уровне файла нельзя, и
    обещать это было бы враньём.
  * Модуль ТОЛЬКО тянет данные. Ни одна строчка не отправляет файл соседу по
    своей инициативе: соседи читают наше по их же запросу, а мы читаем их.
    Поэтому «включил синхронизацию» не означает «мои заметки уехали в сеть».

Кто имеет право читать:
  * адрес запроса обязан быть в `decentral.grid.peers` (та же проверка адреса,
    что и при ручной добавке соседа), И
  * при включённом `mesh.identity` запрос обязан нести корректную подпись узла
    (`mesh_identity.verify_peer_request`) — иначе 403.

Что происходит при отказе и при злой воле:
  * перезаписываемый файл сначала копируется в `data/mesh_sync_backups/<ts>/`,
    хранится 7 дней;
  * удаление соседом не «воскрешается» обратно: на ключ ставится надгробие,
    и пока оно живёт, более старая версия этого файла не принимается;
  * путь, выходящий за пределы разрешённых каталогов, отклоняется до any
    записи на диск;
  * реплицируются только json-файлы заметок и статей: служебные каталоги
    ( `_attachments`, `_plugins`, `_memory`, всё с точкой или подчёркиванием
    в начале имени) из выборки исключены, и сосед не может в них написать —
    канал передачи файлов не должен становиться способом положить код в
    чужое приложение;
  * размер одной записи ограничен, за проход применяется ограниченное число
    записей, интервал не может быть меньше минуты.

Уровни рубильника:
  * `write_mode=off` (по умолчанию) — поток не поднимается, файлы состояния,
    надгробий и бэкапов не создаются, исходящих запросов нет ни одного;
  * `write_mode=lww` — периодический обход перечисленных соседей.
"""

import base64
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.parse
import swallow

logger = logging.getLogger("infinity")

MODE_OFF = "off"
MODE_LWW = "lww"

DEFAULT_INTERVAL_S = 300
MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 86400
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_APPLY_PER_PASS = 200
BACKUP_KEEP_DAYS = 7
KEY_MAX_LEN = 400
PURGE_WAIT_S = 10.0

# Ключ записи: модуль/путь. Слэш только внутренний, «..» и ведущий слэш
# запрещены — иначе имя файла стало бы способом записать куда угодно.
# Расширение фиксировано: и заметки, и статьи лежат json-файлами, поэтому
# канал репликации не умеет приносить соседу файлы другого рода.
KEY_RE = re.compile(r"^(notes|news)/[^\0]{1,%d}\.json$" % (KEY_MAX_LEN - 12))

# Служебные каталоги внутри корня модуля: _attachments, _plugins, _memory,
# _chat_sessions, точки. Сосед по сетке не должен уметь писать в то, чем
# приложение пользуется как кодом и своими индексами, — имя сегмента с
# подчёркивания или точки отклоняется, даже если оно заканчивается на .json.
RESERVED_SEGMENT = re.compile(r"^[._]")

# Символы, которым не место в имени файла: обратный слэш (ключ разбивается
# только прямым), управляющие и запрещённые в Windows. Сосед не обязан знать
# нашу файловую систему, но принимать имена, которые на другой системе
# превращаются в «C:\windows\...» или в мусор, незачем.
ILLEGAL_IN_NAME = re.compile(r'[\\:*?"<>|\x00-\x1f]')


def _data_dir():
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data")
    except Exception:
        swallow.note('mesh_sync._data_dir:L98')
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


STATE_PATH = os.path.join(_data_dir(), "mesh_sync_state.json")
TOMB_PATH = os.path.join(_data_dir(), "mesh_sync_tombstones.json")
BACKUP_DIR = os.path.join(_data_dir(), "mesh_sync_backups")

_lock = threading.RLock()
_state = {"loaded": False, "files": {}, "tomb": {}, "stats": {}}
_run = {"thread": None, "stop": False, "running": False, "error": None,
        "last_pass_at": 0.0, "pass_running": False, "cancel": False}


# ── Конфиг ────────────────────────────────────────────────────────────────

def _mesh():
    try:
        import client_offload
        return client_offload.decentral().get("mesh") or {}
    except Exception:
        swallow.note('mesh_sync._mesh:L119')
        return {}


def peers():
    """Соседи, с которыми эта машина готова обмениваться записями. Публичный
    вопрос, потому что панели нужно показывать «не с кем синхронизироваться»
    до нажатия кнопки, а не после."""
    return _peers()


def mode():
    """Режим записи, который этот модуль умеет исполнять.

    Всё, кроме lww, здесь — выключено: `crdt` в конфиге исторически разрешён
    санитайзером, но потребителя у него нет, и притворяться реализацией модуль
    не будет."""
    m = str(_mesh().get("write_mode") or MODE_OFF)
    return m if m == MODE_LWW else MODE_OFF


def enabled():
    return mode() == MODE_LWW


def cfg():
    m = _mesh()
    try:
        iv = int(m.get("sync_interval_s"))
    except Exception:
        swallow.note('mesh_sync.cfg:L149')
        iv = DEFAULT_INTERVAL_S
    if not isinstance(iv, int):
        iv = DEFAULT_INTERVAL_S
    mods = [str(x) for x in (m.get("sync_modules") or []) if str(x) in ("notes", "news")]
    return {"enabled": enabled(), "mode": mode(), "modules": sorted(set(mods)),
            "interval_s": max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, iv))}


def _peers():
    try:
        import client_offload
        grid = client_offload.decentral().get("grid") or {}
        return [str(p) for p in (grid.get("peers") or [])]
    except Exception:
        swallow.note('mesh_sync._peers:L164')
        return []


def _own_node():
    """Свой идентификатор узла для чужих панелей: сосед по нему и по адресу
    закрепит ключ, а без него не отличит отзыв от нового знакомства."""
    try:
        import mesh_identity
        if mesh_identity.enabled():
            return str(mesh_identity.node_id() or "")
    except Exception:
        swallow.note('mesh_sync._own_node:L176')
        pass
    return ""


def _note_peer_node(mani, base):
    """Запомнить адрес, по которому живёт этот узел.

    Закрепление ключа рождается из входящего запроса, где адреса клиента нет,
    а здесь он известен: без подписки колонка адреса пуста, а отзыв по адресу
    не находит чужой ключ и оставляет его висеть."""
    node = str((mani or {}).get("node") or "")
    if not node:
        return False
    try:
        import mesh_identity
        return bool(mesh_identity.note_peer_url(node, base))
    except Exception:
        swallow.note('mesh_sync._note_peer_node:L194')
        return False


# ── Разрешённые корни и ключи ─────────────────────────────────────────────

_roots_cache = {}


def _roots(refresh=False):
    """Каталоги, которым принадлежит ключ. Вне их ничего не читается и не
    пишется — это единственная граница между «ключ записи» и «путь на диске»."""
    if _roots_cache and not refresh:
        return _roots_cache
    out = {}
    try:
        import config_module
        nd = (config_module.load_config() or {}).get("notes_dir")
        if nd:
            out["notes"] = os.path.abspath(nd)
    except Exception as e:
        logger.debug("[sync] notes_dir не прочитан: %s", e)
    try:
        import news_module
        out["news"] = os.path.abspath(getattr(news_module, "ARTICLES_DIR", ""))
    except Exception as e:
        logger.debug("[sync] ARTICLES_DIR не прочитан: %s", e)
    out = {k: v for k, v in out.items() if v and os.path.isdir(v)}
    _roots_cache.clear()
    _roots_cache.update(out)
    return out


def key_to_path(key):
    """Ключ → абсолютный путь, если он внутри разрешённого корня. Иначе None."""
    if not KEY_RE.match(str(key or "")):
        return None
    mod, rel = str(key).split("/", 1)
    root = _roots().get(mod)
    if not root:
        return None
    if rel.startswith("/") or "\0" in rel or ILLEGAL_IN_NAME.search(rel):
        return None
    parts = rel.split("/")
    if ".." in parts or any(RESERVED_SEGMENT.match(p) for p in parts):
        return None
    full = os.path.abspath(os.path.join(root, *parts))
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


# ── Состояние, надгробия, бэкапы ──────────────────────────────────────────

def _atomic_write(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.debug("[sync] запись %s: %s", path, e)
        return False


def _load_state():
    with _lock:
        if _state["loaded"]:
            return _state
        _state["loaded"] = True
        for key, path in (("files", STATE_PATH),):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    _state["files"] = dict(raw.get("files") or {})
                    _state["stats"] = dict(raw.get("stats") or {})
            except Exception:
                swallow.note('mesh_sync._load_state:L276')
                pass
        try:
            with open(TOMB_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                _state["tomb"] = dict(raw.get("tomb") or {})
        except Exception:
            swallow.note('mesh_sync._load_state:L284')
            pass
        return _state


def _save_state():
    st = _load_state()
    with _lock:
        _atomic_write(STATE_PATH, {"files": st["files"], "stats": st["stats"]})
        _atomic_write(TOMB_PATH, {"tomb": st["tomb"]})


def _stat(**kw):
    _stat_inc(**kw)
    _save_state()


def _stat_inc(**kw):
    """Только в память. Отдельная функция потому, что снимок состояния — это
    один файл со всеми известными ключами: на машине с десятками тысяч статей
    перезаписывать его ради каждого счётчика означало бы гонять мегабайты
    туда-сюда на каждую применённую запись. Флешит конец прохода."""
    st = _load_state()
    with _lock:
        for k, v in kw.items():
            if isinstance(v, int) and k != "last_error_ts":
                st["stats"][k] = int(st["stats"].get(k) or 0) + v
            else:
                st["stats"][k] = v


def _note_tombstone(key, ver):
    st = _load_state()
    with _lock:
        old = int(st["tomb"].get(key) or 0)
        if ver > old:
            st["tomb"][key] = int(ver)


def _local_ver(key):
    path = key_to_path(key)
    try:
        return int(os.stat(path).st_mtime_ns)
    except Exception:
        swallow.note('mesh_sync._local_ver:L328')
        return 0


def _scope(refresh=True):
    """Модули, реально обходимые прямо сейчас: выбраны в конфиге И их корень
    существует.

    Пустой `sync_modules` — это «ничего», а не «всё»: включить режим и не
    выбрать модуль не должно означать тайную отдачу всего архива соседу.
    Второй условие важнее первого: у выбранного модуля корень может временно
    исчезнуть (сетевой диск, перенос папки). Если бы это читалось как
    «все файлы удалены», по всему свету улетели бы надгробия, и соседи
    удалили бы свои копии живых заметок."""
    roots = _roots(refresh=refresh)
    return sorted(m for m in cfg()["modules"] if m in roots)


def _scan():
    """Обход разрешённых каталогов: ключ → (mtime_ns, размер).

    Ищутся только .json — заметки и статьи хранятся ими; вложенные каталоги
    обходятся (папки заметок живут внутри корня), а всё служебное пропускается:
    точка или подчёркивание в начале имени — это индексы, вложения и код
    приложения, а не пользовательские записи. Тот же фильтр стоит в
    `key_to_path`, поэтому манифест никогда не обещает ключ, который сам же
    не пускает."""
    found = {}
    for mod in _scope():
        root = _roots().get(mod)
        if not root:
            continue
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not RESERVED_SEGMENT.match(d)]
            for fn in fns:
                if not fn.endswith(".json") or RESERVED_SEGMENT.match(fn):
                    continue
                full = os.path.join(dp, fn)
                try:
                    st = os.stat(full)
                except Exception:
                    swallow.note('mesh_sync._scan:L369')
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                found["%s/%s" % (mod, rel)] = (int(st.st_mtime_ns), int(st.st_size))
    return found


def _detect_deletions(found):
    """Ключи, которые были в нашем снимке и пропали, — это удаления. Без
    надгробия сосед отдал бы свою старую копию, и удалённая заметка
    «воскресла» бы на этой машине.

    Считаются только ключи обходимых сейчас модулей: недоступный корень — это
    не удаление, и превращать его в надгробие нельзя."""
    st = _load_state()
    scanned = set(_scope())
    with _lock:
        gone = [k for k in st["files"]
                if k not in found and str(k).split("/", 1)[0] in scanned]
        now = time.time_ns()
        for k in gone:
            old = int(st["files"].get(k) or 0)
            ver = max(now, old + 1)
            st["tomb"][k] = max(int(st["tomb"].get(k) or 0), ver)
            st["files"].pop(k, None)
        if gone:
            _atomic_write(TOMB_PATH, {"tomb": st["tomb"]})
            _atomic_write(STATE_PATH, {"files": st["files"], "stats": st["stats"]})
    return len(gone)


def _backup(path, key):
    try:
        if not os.path.isfile(path):
            return None
        dst = os.path.join(BACKUP_DIR, time.strftime("%Y%m%d-%H%M%S"),
                           key.replace("/", "__"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst)
        return dst
    except Exception as e:
        logger.debug("[sync] бэкап %s: %s", key, e)
        return None


def _prune_backups():
    """Хранение семь дней: бэкап — страховка на случай кривой версии, а не
    вторая библиотека, которая растёт бесконечно."""
    cutoff = time.time() - BACKUP_KEEP_DAYS * 86400
    try:
        if not os.path.isdir(BACKUP_DIR):
            return 0
        removed = 0
        for name in os.listdir(BACKUP_DIR):
            full = os.path.join(BACKUP_DIR, name)
            try:
                if os.path.isdir(full) and os.path.getmtime(full) < cutoff:
                    shutil.rmtree(full, ignore_errors=True)
                    removed += 1
            except Exception:
                swallow.note('mesh_sync._prune_backups:L429')
                continue
        return removed
    except Exception as e:
        logger.debug("[sync] чистка бэкапов: %s", e)
        return 0


# ── Манифест и выдача записи ──────────────────────────────────────────────

def manifest():
    """Что эта машина может отдать соседy. Снимок обновляется на каждое
    обращение, иначе новые файлы не уехали бы до ближайшего прохода потока."""
    st = _load_state()
    found = _scan()
    with _lock:
        for k, (ver, _sz) in found.items():
            st["files"][k] = int(ver)
    _detect_deletions(found)
    _save_state()
    return {"mode": mode(), "node": _own_node(), "files": dict(st["files"]),
            "tomb": dict(st["tomb"])}


def record(key):
    """Одна запись для соседа: содержимое + версия. Удалённый ключ приходит
    надгробием без тела — иначе удаление нельзя передать вообще."""
    st = _load_state()
    with _lock:
        tomb = int(st["tomb"].get(key) or 0)
    path = key_to_path(key)
    if path is None:
        return {"ok": False, "why": "ключ вне разрешённых каталогов"}
    mod = str(key).split("/", 1)[0]
    if mod not in cfg()["modules"]:
        return {"ok": False, "why": "модуль %s не выбран" % mod}
    try:
        sz = os.path.getsize(path)
        ver = int(os.stat(path).st_mtime_ns)
    except Exception:
        swallow.note('mesh_sync.record:L469')
        if tomb:
            return {"ok": True, "key": key, "ver": tomb, "deleted": True}
        return {"ok": False, "why": "нет такого файла"}
    if tomb and tomb >= ver:
        return {"ok": True, "key": key, "ver": tomb, "deleted": True}
    if sz > MAX_RECORD_BYTES:
        return {"ok": False, "why": "запись больше %d Б" % MAX_RECORD_BYTES}
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        swallow.note('mesh_sync.record:L481', e)
        return {"ok": False, "why": "чтение: %s" % e}
    return {"ok": True, "key": key, "ver": ver, "deleted": False,
            "size": sz, "data": base64.b64encode(raw).decode()}


# ── Применение чужой записи ───────────────────────────────────────────────

def apply_remote(key, ver, deleted=False, data_b64=None):
    """LWW: чужая версия принимаетcя, только если она новее нашей.

    Возвращает действие, а не просто успех, — панели нужно различать
    «применили», «старше нашей» и «надгробие»."""
    ver = int(ver or 0)
    st = _load_state()
    with _lock:
        tomb = int(st["tomb"].get(key) or 0)
    path = key_to_path(key)
    if path is None:
        _stat(errors=1)
        return {"ok": False, "why": "ключ вне разрешённых каталогов"}
    mod = str(key).split("/", 1)[0]
    if mod not in cfg()["modules"]:
        _stat_inc(skipped=1)
        return {"ok": False, "why": "модуль %s не выбран" % mod}
    ours = _local_ver(key)
    if tomb and tomb >= ver:
        _stat_inc(skipped=1)
        return {"ok": False, "why": "старше надгробия"}
    if ver <= ours:
        _stat_inc(kept=1)
        return {"ok": True, "action": "kept", "why": "наша версия новее"}

    if deleted:
        if ours:
            _backup(path, key)
            try:
                os.remove(path)
            except Exception as e:
                _stat(errors=1)
                return {"ok": False, "why": "удаление: %s" % e}
        _note_tombstone(key, ver)
        with _lock:
            st["files"].pop(key, None)
        _stat_inc(deleted=1)
        _save_state()
        return {"ok": True, "action": "deleted"}

    try:
        raw = base64.b64decode(str(data_b64 or ""))
    except Exception:
        _stat(errors=1)
        return {"ok": False, "why": "тело не декодируется"}
    if len(raw) > MAX_RECORD_BYTES:
        _stat(errors=1)
        return {"ok": False, "why": "запись больше %d Б" % MAX_RECORD_BYTES}
    _backup(path, key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".inf-sync.tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Версия хранится в mtime: без этого следующая локальная правка
        # соседом выглядела бы старше, чем она на самом деле.
        ns = (int(ver), int(ver))
        try:
            os.utime(path, ns=ns)
        except Exception:
            swallow.note('mesh_sync.apply_remote:L552')
            pass
    except Exception as e:
        _stat(errors=1)
        return {"ok": False, "why": "запись: %s" % e}
    with _lock:
        st["files"][key] = int(ver)
    _stat_inc(applied=1)
    _save_state()
    return {"ok": True, "action": "written", "size": len(raw)}


# ── Право соседа читать ───────────────────────────────────────────────────

def _host_of(url):
    try:
        p = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
        return (p.hostname or "").lower()
    except Exception:
        swallow.note('mesh_sync._host_of:L571')
        return ""


def peer_allowed(ip):
    """Сосед — это адрес из grid.peers, ничего другого не принимается.

    Loopback разрешён отдельно: два экземпляра на одной машине — это штатный
    способ проверить механизм, а не дыра."""
    h = str(ip or "").strip().lower()
    if not h:
        return False
    if h in ("127.0.0.1", "::1", "localhost"):
        return True
    return any(_host_of(p) == h for p in _peers())


def incoming_ok(method, path, params, headers, ip):
    """Вердикт входящему соседскому запросу.

    Порядок жёсткий: сначала адрес, потом подпись. Проверка подписи раньше
    адреса означала бы, что чужой IP получает хотя бы ответ этого модуля."""
    if not enabled():
        return {"ok": False, "reason": "mesh.write_mode=off"}
    if not peer_allowed(ip):
        return {"ok": False, "reason": "адрес не в списке соседей"}
    try:
        import mesh_identity
        if mesh_identity.enabled():
            v = mesh_identity.verify_peer_request(method, path, params, headers or {})
            if not v.get("ok"):
                return {"ok": False, "reason": "подпись: %s" % v.get("reason"),
                        "node": v.get("node") or ""}
    except Exception as e:
        swallow.note('mesh_sync.incoming_ok:L605', e)
        return {"ok": False, "reason": "проверка подписи: %s" % e}
    return {"ok": True}


# ── Проход: тянем у соседей ───────────────────────────────────────────────

def _get_json(base, path, params, timeout_s):
    import httpx
    url = base.rstrip("/") + path
    try:
        import mesh_identity
        hdrs = (mesh_identity.peer_headers("GET", path, params)
                if mesh_identity.enabled() else {})
    except Exception:
        swallow.note('mesh_sync._get_json:L620')
        hdrs = {}
    with httpx.Client(timeout=timeout_s, verify=False) as c:
        r = c.get(url, params=params or {}, headers=hdrs)
        if r.status_code != 200:
            return None, {"error": "HTTP %d" % r.status_code}
        try:
            return r.json(), None
        except Exception as e:
            swallow.note('mesh_sync._get_json:L629', e)
            return None, {"error": "не json: %s" % e}


def pull_from(base, timeout_s=10.0, limit=MAX_APPLY_PER_PASS):
    """Забрать у одного соседа всё, что у него новее нашего.

    Только чтение: мы спрашиваем его манифест и просим отдельные записи. Никак
    не отправляем свои файлы — наш манифест отдаётся по его запросу."""
    if not enabled():
        # Рубильник означает «ни пакетов», а не «поток не поднят, но попросили —
        # отправим». Вызов возможен из ручного прохода, админской ручки или теста.
        return {"ok": False, "peer": base, "skipped": "mesh.write_mode=off",
                "checked": 0, "applied": 0, "deleted": 0, "kept": 0, "errors": 0}
    out = {"peer": base, "checked": 0, "applied": 0, "deleted": 0,
           "kept": 0, "errors": 0}
    mani, err = _get_json(base, "/api/mesh/sync/manifest", {}, timeout_s)
    if err:
        out["error"] = err["error"]
        return out
    _note_peer_node(mani, base)
    remote = dict((mani or {}).get("files") or {})
    tombs = dict((mani or {}).get("tomb") or {})
    st = _load_state()
    with _lock:
        local = dict(st["files"])
        ours_tomb = dict(st["tomb"])
    todo = []
    for k, ver in remote.items():
        try:
            ver = int(ver)
        except Exception:
            swallow.note('mesh_sync.pull_from:L661')
            continue
        # Чужое надгробие поверх чужого же файла означает «этого больше нет»:
        # тянуть сам файл незачем, его удалят отдельной записью ниже.
        if ver <= int(local.get(k) or 0) or ver <= int(ours_tomb.get(k) or 0):
            continue
        if ver <= int(tombs.get(k) or 0):
            continue
        todo.append((ver, k))
    for k, ver in tombs.items():
        try:
            ver = int(ver)
        except Exception:
            swallow.note('mesh_sync.pull_from:L674')
            continue
        if ver > int(local.get(k) or 0) and ver > int(ours_tomb.get(k) or 0):
            todo.append((ver, k))
    todo.sort(reverse=True)
    for _ver, key in todo[:limit]:
        rec, err = _get_json(base, "/api/mesh/sync/record", {"key": key}, timeout_s)
        out["checked"] += 1
        if err:
            out["errors"] += 1
            continue
        if not (rec or {}).get("ok"):
            out["errors"] += 1
            continue
        res = apply_remote(key, rec.get("ver"), bool(rec.get("deleted")),
                           rec.get("data"))
        act = (res or {}).get("action")
        if act == "written":
            out["applied"] += 1
        elif act == "deleted":
            out["deleted"] += 1
        elif act == "kept":
            out["kept"] += 1
        else:
            out["errors"] += 1
    # Счётчики этого соседа копятся в памяти; на диск — один раз, иначе каждый
    # применённый ключ перезаписывал бы весь снимок.
    _save_state()
    return out


def pass_once():
    """Один проход по всем соседям из сетки. Пустой список соседей — это
    осмысленное состояние: делать нечего, ошибок нет."""
    if not enabled():
        return {"ok": False, "skipped": "mesh.write_mode=off", "peers": [],
                "local_deletions": 0}
    _run["cancel"] = False
    _run["pass_running"] = True
    results = []
    try:
        for base in _peers():
            if _run.get("cancel"):
                # Снятие рубильника и «стереть данные» должны обрывать и ручной
                # проход, который живёт в своём потоке и stop() его не ждёт.
                break
            try:
                results.append(pull_from(base))
            except Exception as e:
                results.append({"peer": base, "error": type(e).__name__})
        gone = _detect_deletions(_scan())
        _prune_backups()
        _stat(passes=1, last_pass_at=time.time())
        _run["last_pass_at"] = time.time()
        _run["error"] = next((r["error"] for r in results if r.get("error")), None)
        return {"ok": True, "peers": results, "local_deletions": gone}
    finally:
        _run["pass_running"] = False


def _loop(interval_s):
    # Условие читается из _run каждый виток: сохранённый в локальную переменную
    # флаг делал остановку мнимой — поток не замечал её и после stop() брался за
    # новый проход, а с ним и за чужой диск.
    while not _run.get("stop"):
        try:
            pass_once()
        except Exception as e:
            _run["error"] = str(e)[:200]
            logger.debug("[sync] проход: %s", e)
        n = 0
        while n < interval_s and not _run.get("stop"):
            time.sleep(1)
            n += 1


# ── Поток и рубильник ─────────────────────────────────────────────────────

def start():
    with _lock:
        if _run["running"]:
            return {"ok": True, "started": False, "why": "уже запущен"}
        if not enabled():
            return {"ok": False, "started": False, "why": "mesh.write_mode=off"}
        _run.update({"stop": False, "running": True})
        iv = cfg()["interval_s"]
        t = threading.Thread(target=_loop, args=(iv,), daemon=True,
                             name="infinity-mesh-sync")
        _run["thread"] = t
        t.start()
    return {"ok": True, "started": True, "interval_s": iv, "peers": len(_peers())}


def stop():
    with _lock:
        _run["stop"] = True
        _run["cancel"] = True
        _run["running"] = False
        t = _run.get("thread")
    if t and t is not threading.current_thread():
        try:
            t.join(timeout=3)
        except Exception:
            swallow.note('mesh_sync.stop:L777')
            pass
    with _lock:
        _run["thread"] = None
    return {"ok": True}


def running():
    return bool(_run["running"])


def pass_running():
    """Идёт ли проход сейчас — публичный вопрос, потому что панели нужно
    отличать «нажали дважды» от «ничего не происходит»."""
    return bool(_run["pass_running"])


def apply_state():
    """Привести поток в соответствие конфигурации: включено ⇒ поднять,
    выключено ⇒ погасить и ничего не трогать на диске."""
    c = cfg()
    if not c["enabled"]:
        if running():
            stop()
        return {"enabled": False, "running": False, "started": False}
    if running():
        stop()
    res = start()
    res.update({"enabled": True, "interval_s": c["interval_s"],
                "modules": c["modules"], "peers": len(_peers())})
    return res


def status():
    c = cfg()
    st = _load_state()
    with _lock:
        files = len(st["files"])
        tombs = len(st["tomb"])
        stats = dict(st["stats"])
        # По модулям, а не общим числом: панель должна показывать, что
        # именно выбрано и чем именно машина делится, а не «что-то известно».
        by = {}
        for src, field in ((st["files"], "keys"), (st["tomb"], "tomb")):
            for k in src:
                mod = str(k).split("/", 1)[0]
                by.setdefault(mod, {"keys": 0, "tomb": 0})[field] += 1
    roots = _roots()
    return {
        "enabled": c["enabled"],
        "mode": c["mode"],
        "running": running(),
        "pass_running": bool(_run["pass_running"]),
        "interval_s": c["interval_s"],
        "modules": c["modules"],
        "available_modules": sorted(roots.keys()),
        "roots": roots,
        "peers": _peers(),
        "known_files": files,
        "tombstones": tombs,
        "by_module": by,
        "limits": {"record_bytes": MAX_RECORD_BYTES,
                   "apply_per_pass": MAX_APPLY_PER_PASS,
                   "backup_keep_days": BACKUP_KEEP_DAYS},
        "backup_dir": BACKUP_DIR,
        "last_pass_at": _run["last_pass_at"],
        "error": _run["error"],
        "stats": stats,
        "note": ("режим `crdt` в конфиге разрешён, но не реализован: слить две "
                 "правки одной заметки на уровне файла нельзя"),
    }


def summary():
    s = status()
    if not s["enabled"]:
        return "синхронизация выключена (write_mode=%s)" % s["mode"]
    return ("lww: поток %s, модули %s, соседей %d, известно %d ключей, "
            "надгробий %d" % ("идёт" if s["running"] else "не запущен",
                              "/".join(s["modules"]) or "—", len(s["peers"]),
                              s["known_files"], s["tombstones"]))


def purge():
    """Стереть всё состояние модуля: снимок, надгробия и бэкапы.

    Данные пользователя (сами заметки) не трогаются — удаляются только
    служебные копии этого модуля."""
    stop()
    # Ручной проход живёт в отдельном потоке, который stop() не дожидает: без
    # паузы он дописал бы снимок обратно сразу после стирания, и «стерто»
    # оказалось бы неправдой.
    waited = 0.0
    while _run.get("pass_running") and waited < PURGE_WAIT_S:
        time.sleep(0.1)
        waited += 0.1
    removed = []
    for p in (STATE_PATH, TOMB_PATH):
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed.append(os.path.basename(p))
        except Exception as e:
            logger.debug("[sync] удаление %s: %s", p, e)
    try:
        if os.path.isdir(BACKUP_DIR):
            shutil.rmtree(BACKUP_DIR, ignore_errors=True)
            removed.append(os.path.basename(BACKUP_DIR))
    except Exception as e:
        logger.debug("[sync] удаление бэкапов: %s", e)
    with _lock:
        _state.update({"loaded": False, "files": {}, "tomb": {}, "stats": {}})
    return {"ok": True, "removed": removed}


def reset_for_tests():
    """То же, что purge, но без обещаний панели: для гейтов."""
    out = purge()
    _roots_cache.clear()
    _run.update({"error": None, "last_pass_at": 0.0, "pass_running": False,
                 "stop": False, "cancel": False, "running": False})
    return out
