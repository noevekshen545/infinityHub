"""Стресс-тест изоляции профилей: 8 профилей, утечки по всем доменам.

Полигон живёт ВО ВРЕМЕННОМ каталоге: INFINITY_DATA_DIR, INFINITY_CONFIG и
USERPROFILE/HOME уводятся до импорта app-модулей, поэтому настоящие профили,
`app/config.json` и `media.db` пользователя не читаются и не пишутся. Медиа
каждого профиля — свой каталог в песочнице, файлы генерируются синтетические.

Проверяются два свойства, и каждое отрицание идёт с позитивным контролем:
  1) свой: профиль видит и может менять ровно то, что положил сам;
  2) чужой: объект другого профиля недоступен ни на чтение, ни на запись, ни по
     прямому id, ни по абсолютному пути, ни без cookie (это общедоступный).

Контроль обязателен: маршрут, который ломается и отдаёт 404 всем подряд,
выглядел бы «изолированным» без проверки, что владелец данные реально получает.

Запуск: PYTHONIOENCODING=utf-8 E:/compilinfinity/venv/Scripts/python.exe tools/profile_leak_stress.py
"""
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import zipfile

logging.basicConfig(level=logging.ERROR)
for noisy in ("httpx", "httpx2", "httpcore", "uvicorn", "watchfiles", "multipart"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "app")
N_PROFILES = 8
COOKIE = "infinity_profile"

# ---------- полигон: всё во временный каталог, до импорта app ----------
POLY = os.path.join(tempfile.gettempdir(), "profile_stress")
if os.path.isdir(POLY):
    shutil.rmtree(POLY, ignore_errors=True)
DATA = os.path.join(POLY, "data")
HOME = os.path.join(POLY, "home")
MEDIA = os.path.join(POLY, "media")
for d in (DATA, HOME, MEDIA):
    os.makedirs(d, exist_ok=True)

_seed_cfg = os.path.join(POLY, "config.json")
try:
    shutil.copy2(os.path.join(APP, "config.json"), _seed_cfg)
except Exception:
    open(_seed_cfg, "w").write("{}")

os.environ["INFINITY_DATA_DIR"] = DATA
os.environ["INFINITY_CONFIG"] = _seed_cfg
os.environ["USERPROFILE"] = HOME
os.environ["HOME"] = HOME
os.environ["APPDATA"] = os.path.join(HOME, "AppData", "Roaming")
os.environ["LOCALAPPDATA"] = os.path.join(HOME, "AppData", "Local")

sys.path.insert(0, APP)

from fastapi import FastAPI
from starlette.testclient import TestClient

import profile_paths  # noqa: F401
import profiles_module as PM
import server  # noqa: F401  (роутеры читают его глобалы)
from routes import chat as r_chat
from routes import compat_notes as r_compat_notes
from routes import extension as r_ext
from routes import legacy_media_player as r_media
from routes import legacy_memory_bookmarks as r_mem
from routes import legacy_notes_ws as r_notes
from routes import legacy_photos as r_photos
from routes import legacy_search_config_books as r_search
from routes import profiles as r_profiles
from routes import share as r_share
from routes import stats as r_stats
import file_manager as r_fm
from routes import userwiki as r_wiki

app_ = FastAPI()
# Catch-all-модули (userwiki, legacy_media_player c /api/media/stream/{path}) —
# в конце, иначе перекроют конкретные префиксы и гейт мерил бы 404 вместо данных.
for mod in (r_profiles, r_fm, r_notes, r_compat_notes, r_chat, r_mem, r_photos,
            r_ext, r_share, r_stats, r_search, r_media, r_wiki):
    app_.include_router(mod.router)

client = TestClient(app_)

LEAKS = []
CONTROLS = []
TOTAL = 0


def check(label, ok, detail=""):
    """Утверждение об изоляции: ok=True значит утечки нет."""
    global TOTAL
    TOTAL += 1
    if not ok:
        LEAKS.append("%s%s" % (label, (" — " + detail) if detail else ""))
    print("  [%s] %s%s" % ("OK" if ok else "УТЕЧКА", label,
                           (" — " + detail) if detail and not ok else ""))
    return ok


def control(label, ok, detail=""):
    """Позитивный контроль: без него отрицательная проверка ничего не стоит."""
    global TOTAL
    TOTAL += 1
    if not ok:
        CONTROLS.append("%s%s" % (label, (" — " + detail) if detail else ""))
    print("  [%s] контроль: %s%s" % ("OK" if ok else "СЛОМАН", label,
                                     (" — " + detail) if detail and not ok else ""))
    return ok


def J(method, path, pid=None, **kw):
    cookies = {COOKIE: pid} if pid else {}
    try:
        r = getattr(client, method.lower())(path, cookies=cookies, **kw)
    except Exception as e:
        return 0, {"_exc": str(e)[:160]}
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_text": r.text[:200]}


def RAW(method, path, pid=None, **kw):
    """Как J, но возвращает текст ответа (для не-JSON ручек: стрим, файл)."""
    cookies = {COOKIE: pid} if pid else {}
    try:
        r = getattr(client, method.lower())(path, cookies=cookies, **kw)
    except Exception as e:
        return 0, "EXC %s" % str(e)[:160]
    return r.status_code, (r.text if len(r.content) < 4000 else "<%d байт>" % len(r.content))


def blob(o):
    return json.dumps(o, ensure_ascii=False, default=str)


MARK = "STRST%d" % int(time.time())
PUB = PM.get_default_profile_id()
PIDS = [PUB]


# ---------- 0. профили ----------
print("\n[0] Полигон")
print("  data=%s" % DATA)
print("  home=%s" % HOME)
print("  media.db=%s" % os.path.join(os.path.expanduser("~"), ".infinity", "data", "media.db"))
for i in range(N_PROFILES):
    sc, body = J("post", "/api/profiles/create", pid=PUB, json={"name": "СТРЕСС-%02d" % i})
    pid = body.get("id")
    if sc == 200 and pid:
        PIDS.append(pid)
    else:
        created = PM.create_profile("СТРЕСС-%02d" % i)
        if created:
            PIDS.append(created["id"])
PRIVATE = [p for p in PIDS if p != PUB]
check("создано %d профилей через API" % len(PRIVATE), len(PRIVATE) >= N_PROFILES,
      "приватных %d, HTTP-ответы см. выше" % len(PRIVATE))
print("  общедоступный=%s, приватных=%d" % (PUB, len(PRIVATE)))


def marker(pid):
    return "%s.%s" % (MARK, pid[:8])


def other_than(pid, n=2):
    out = [p for p in PRIVATE if p != pid]
    return out[:n]


# ---------- 1. заметки: создание, чтение, списки, поиск, версии, комментарии ----------
print("\n[1] Заметки и канбан-доски (custom modes)")
note_ids = {}
save_bad = []
for pid in PIDS:
    nid = "stress-%s" % pid[:8]
    sc, body = J("post", "/api/notes/save", pid=pid,
                 json={"id": nid, "title": "Заметка %s" % marker(pid),
                       "content": "тело заметки %s" % marker(pid), "tags": []})
    note_ids[pid] = nid
    if sc != 200:
        save_bad.append("%s:%s" % (pid[:8], sc))
control("заметка сохранена каждым профилем", not save_bad, ", ".join(save_bad))

for pid in PIDS[:4]:
    sc, lst = J("get", "/api/notes/list", pid=pid)
    ids = [a.get("id") for a in (lst.get("articles") or [])]
    control("владелец видит свою заметку в списке (%s)" % pid[:8], note_ids[pid] in ids,
            "id в списке: %d" % len(ids))
for victim in PRIVATE[:3]:
    for attacker in other_than(victim, 3):
        sc, body = J("get", "/api/notes/get?id=%s" % note_ids[victim], pid=attacker)
        check("заметку %s не читает %s" % (victim[:8], attacker[:8]),
              marker(victim) not in blob(body), "HTTP %s: маркер жертвы в ответе" % sc)

foreign_search = []
for pid in PRIVATE[:3]:
    sc, srch = J("get", "/api/notes/search?q=%s" % MARK, pid=pid)
    f = [marker(o) for o in PIDS if o != pid and marker(o) in blob(srch)]
    if f:
        foreign_search.append("%s: +%d" % (pid[:8], len(f)))
    sc, sa = J("get", "/api/notes/search-all?q=%s&limit=50" % MARK, pid=pid)
    f2 = [marker(o) for o in PIDS if o != pid and marker(o) in blob(sa)]
    if f2:
        foreign_search.append("%s(search-all): +%d" % (pid[:8], len(f2)))
check("поиск заметок не находит чужие маркеры", not foreign_search, "; ".join(foreign_search))

sc, tree = J("get", "/api/notes/tree", pid=PRIVATE[0])
f = [marker(o) for o in PIDS if o != PRIVATE[0] and marker(o) in blob(tree)]
check("дерево заметок не показывает чужие", not f, str(f)[:120])
sc, gr = J("get", "/api/notes/graph", pid=PRIVATE[0])
f = [marker(o) for o in PIDS if o != PRIVATE[0] and marker(o) in blob(gr)]
check("граф заметок не показывает чужие", not f, str(f)[:120])
sc, exp = J("post", "/api/notes/export-all", pid=PRIVATE[0])
f = [marker(o) for o in PIDS if o != PRIVATE[0] and marker(o) in blob(exp)]
check("export-all не выгружает чужие заметки", not f, str(f)[:120])

for victim in PRIVATE[:2]:
    sc, v = J("get", "/api/notes/version/list?id=%s" % note_ids[victim], pid=PRIVATE[-1])
    check("версии чужой заметки не отдаются (%s)" % victim[:8],
          marker(victim) not in blob(v) and sc in (200, 404), "HTTP %s" % sc)
    sc2, own = J("get", "/api/notes/version/list?id=%s" % note_ids[victim], pid=victim)
    control("владелец может спросить версии своей заметки", sc2 == 200, "HTTP %s" % sc2)
    sc, cm = J("post", "/api/notes/comment/list", pid=PRIVATE[-1], json={"note_id": note_ids[victim]})
    check("комментарии чужой заметки не отдаются (%s)" % victim[:8],
          marker(victim) not in blob(cm), "HTTP %s" % sc)
    sc, w = J("post", "/api/notes/save", pid=PRIVATE[-1],
              json={"id": note_ids[victim], "title": "ПЕРЕХВАТ %s" % MARK,
                    "content": "ПЕРЕХВАТ %s" % MARK, "tags": []})
    sc2, back = J("get", "/api/notes/get?id=%s" % note_ids[victim], pid=victim)
    check("чужой профиль не перезаписывает заметку (%s)" % victim[:8],
          "ПЕРЕХВАТ %s" % MARK not in blob(back), "HTTP %s: содержимое подменено" % sc)

# ---------- 1б. канбан / custom modes ----------
# Микросервис заметки (канбан-доска, таблица) лежит в _artifacts_ рядом с
# заметкой, а сохранённый ШАБЛОН режима — в каталоге приложения, общем на все
# профили. Проверяем оба: и артефакт по чужому id, и перетирание шаблона.
print("\n[1б] Канбан и custom modes: артефакты заметки и общие шаблоны")
import custom_mode_module as CM
kanban_leaks = []
kanban_slugs = {}
for victim in PRIVATE[:2]:
    nb = note_ids.get(victim)
    if not nb:
        continue
    kb_html = "<div class=\"kanban\">КАНБЕН %s</div>" % marker(victim)
    sc, body = J("post", "/api/custom-mode/save", pid=victim,
                 json={"id": nb, "slug": "kanban", "title": "доска %s" % marker(victim),
                       "html": kb_html})
    kanban_slugs[victim] = body.get("slug") if isinstance(body, dict) else None
    sc, lst = J("get", "/api/custom-mode/list?id=%s" % nb, pid=victim)
    control("владелец видит свою канбан-доску (%s)" % victim[:8],
             marker(victim) in blob(lst), "HTTP %s save=%s" % (sc, blob(body)[:70]))
for victim in PRIVATE[:2]:
    nb = note_ids.get(victim)
    for attacker in other_than(victim, 2):
        sc, lst = J("get", "/api/custom-mode/list?id=%s" % nb, pid=attacker)
        if marker(victim) in blob(lst):
            kanban_leaks.append("custom-mode/list чужой доски %s -> %s" % (victim[:8], attacker[:8]))
        sc, art = RAW("get", "/api/custom-mode/artifact?id=%s&slug=%s" % (nb, "kanban"), pid=attacker)
        if marker(victim) in art:
            kanban_leaks.append("custom-mode/artifact чужой доски %s -> %s (HTTP %s)"
                                % (victim[:8], attacker[:8], sc))
        # записывает ли чужой id артефакт в каталог жертвы
        sc, body = J("post", "/api/custom-mode/save", pid=attacker,
                     json={"id": nb, "slug": "poddavleno", "title": MARK,
                           "html": "<div>ПОДАВА %s</div>" % marker(attacker)})
        adir = CM.artifact_dir(profile_paths.dir_for(attacker, "NOTES_DIR"), nb)
        vdir = CM.artifact_dir(profile_paths.dir_for(victim, "NOTES_DIR"), nb)
        if os.path.isfile(os.path.join(vdir, "poddavleno.html")):
            kanban_leaks.append("custom-mode/save по чужому id записал в каталог жертвы (%s)" % victim[:8])
        if os.path.isfile(os.path.join(adir, "poddavleno.html")):
            print("   артефакт по чужому id лёг в каталог злоумышленника: %s (заметки там нет)"
                  % os.path.relpath(adir, POLY))
check("чужие канбан-доски не читаются и не затираются", not kanban_leaks, "; ".join(kanban_leaks[:6]))

TMPL_NAME = "strst-%s" % MARK.lower()
TMPL_HTML = "<div>ШАБЛОН РЕЖИМА %s</div>" % marker(PRIVATE[0])
sc, body = J("post", "/api/custom-mode/template/save", pid=PRIVATE[0],
             json={"key": TMPL_NAME, "html": TMPL_HTML})
control("профиль сохранил свой шаблон режима", sc == 200, "HTTP %s %s" % (sc, blob(body)[:90]))
tmpl_foreign = []
for attacker in other_than(PRIVATE[0], 2):
    sc, got = J("get", "/api/custom-mode/template?key=%s" % TMPL_NAME, pid=attacker)
    if marker(PRIVATE[0]) in blob(got):
        tmpl_foreign.append(attacker[:8])
    sc, over = J("post", "/api/custom-mode/template/save", pid=attacker,
                 json={"key": TMPL_NAME, "html": "<div>ПЕРЕЗАПИСЬ %s</div>" % marker(attacker)})
    sc, back = J("get", "/api/custom-mode/template?key=%s" % TMPL_NAME, pid=PRIVATE[0])
    if marker(attacker) in blob(back):
        tmpl_foreign.append("%s перезаписал шаблон владельца" % attacker[:8])
_own_tpl = os.path.join(profile_paths.dir_for(PRIVATE[0], "CUSTOM_MODE_TEMPLATES_DIR"),
                        TMPL_NAME + ".html")
control("шаблон физически лёг в каталог профиля-владельца (доказательство)",
        os.path.isfile(_own_tpl) and not os.path.isfile(
            os.path.join(r_notes.CUSTOM_MODE_TEMPLATES_DIR, TMPL_NAME + ".html")),
        "ожидался %s, а в общем каталоге %s файл есть: %s"
        % (_own_tpl, r_notes.CUSTOM_MODE_TEMPLATES_DIR,
           os.path.isfile(os.path.join(r_notes.CUSTOM_MODE_TEMPLATES_DIR,
                                       TMPL_NAME + ".html"))))
check("чужие шаблоны custom-mode не видны и не затираются", not tmpl_foreign,
      "; ".join(tmpl_foreign[:4]))

# ---------- 2. файловый менеджер ----------
print("\n[2] Файловый менеджер: загрузка файлов, чтение, запись, удаление")
roots = {}
for pid in PIDS:
    sc, body = J("get", "/filemanager/profile-root", pid=pid)
    r = body.get("root") or body.get("path") or body.get("profile_root") or ""
    if r:
        roots[pid] = r
control("у каждого профиля свой корень ФМ", len(roots) == len(PIDS) and len(set(roots.values())) == len(roots),
        "корней %d из %d" % (len(roots), len(PIDS)))

FM_BODY = ("Привет, это файл профиля %s %s" % (MARK, "%s")).encode()
fm_leaks, fm_writes, fm_deletes = [], [], []
for pid in PIDS:
    root = roots.get(pid)
    if not root:
        continue
    d = J("post", "/filemanager/mkdir", pid=pid, json={"path": root, "name": "СТРЕССПАПКА"})
    if d[0] != 200:
        print("   mkdir HTTP %s %s" % (d[0], blob(d[1])[:100]))
    target = os.path.join(root, "СТРЕССПАПКА")
    os.makedirs(target, exist_ok=True)
    r = client.post("/filemanager/upload", cookies={COOKIE: pid},
                    data={"destination": target},
                    files={"file": ("%s.txt" % marker(pid), FM_BODY % marker(pid).encode(), "text/plain")})
    if r.status_code != 200:
        print("   upload HTTP %s %s" % (r.status_code, r.text[:120]))

victim = PRIVATE[1]
attacker = PRIVATE[2]
vroot, aroot = roots.get(victim), roots.get(attacker)
vfile = os.path.join(vroot, "СТРЕССПАПКА", "%s.txt" % marker(victim)) if vroot else ""
if vfile:
    # Листим именно подпапку: в корне она одна запись-каталог, и проверка
    # «имени файла в ответе» на корне означала бы «контроль всегда сломан».
    sc, lst = J("get", "/filemanager/list?path=%s" % os.path.join(vroot, "СТРЕССПАПКА"), pid=victim)
    control("владелец листит свою папку", marker(victim) in blob(lst), "HTTP %s" % sc)
    sc, lst2 = J("get", "/filemanager/list?path=%s" % os.path.join(vroot, "СТРЕССПАПКА"), pid=attacker)
    names = blob(lst2)
    if marker(victim) in names:
        fm_leaks.append("list чужого корня (%s видит %s): HTTP %s" % (attacker[:8], victim[:8], sc))
    sc, rd = J("get", "/filemanager/read?path=%s" % vfile, pid=attacker)
    can_read = marker(victim) in blob(rd)
    if can_read:
        fm_leaks.append("read чужого файла по абсолютному пути: HTTP %s" % sc)
    sc, dl = RAW("get", "/filemanager/download?path=%s" % vfile, pid=attacker)
    if dl.startswith("<") or marker(victim) in dl:
        fm_leaks.append("download чужого файла: HTTP %s" % sc)
    sc, sr = J("get", "/filemanager/search?path=%s&query=%s" % (vroot, MARK), pid=attacker)
    if marker(victim) in blob(sr):
        fm_leaks.append("search по чужому корню: HTTP %s" % sc)
    sc, wr = J("post", "/filemanager/write", pid=attacker,
               json={"path": vfile, "content": "ПЕРЕХВАТ %s" % MARK, "encoding": "utf-8"})
    sc2, rd2 = J("get", "/filemanager/read?path=%s" % vfile, pid=victim)
    if "ПЕРЕХВАТ %s" % MARK in blob(rd2):
        fm_writes.append("write в чужой файл: HTTP %s" % sc)
    sc, mk = J("post", "/filemanager/mkdir", pid=attacker,
               json={"path": vroot, "name": "ЧУЖАЯ%s" % MARK})
    if os.path.isdir(os.path.join(vroot, "ЧУЖАЯ%s" % MARK)):
        fm_writes.append("mkdir в чужой корень: HTTP %s" % sc)
    sc, dl2 = J("post", "/filemanager/delete", pid=attacker,
                json={"paths": [vfile], "permanent": True})
    if not os.path.exists(vfile):
        fm_deletes.append("delete чужого файла permanent=true: HTTP %s" % sc)

check("чужие файлы недоступны на чтение", not fm_leaks, "; ".join(fm_leaks))
check("чужие файлы недоступны на запись", not fm_writes, "; ".join(fm_writes))
check("чужие файлы нельзя удалить", not fm_deletes, "; ".join(fm_deletes))
sc, disks = J("get", "/filemanager/disks", pid=PRIVATE[0])
print("   disks: HTTP %s, %s" % (sc, blob(disks)[:120]))
sc, lstC = J("get", "/filemanager/list?path=C:/Windows", pid=PRIVATE[0])
print("   листинг произвольного пути вне профилей: HTTP %s, записей %d" % (sc, len((lstC or {}).get("items") or [])))

# ---------- 3. чат с агентом ----------
print("\n[3] Чат с агентом: сессии, сообщения, история, папки")
chat_ids = {}
for pid in PIDS:
    sc, body = J("post", "/api/chat/sessions", pid=pid, json={"source": "stress", "folder": "stress"})
    sid = body.get("session_id") or body.get("id")
    chat_ids[pid] = sid
    if not sid:
        print("   sessions HTTP %s %s" % (sc, blob(body)[:120]))
control("сессия создана каждым профилем", all(chat_ids.get(p) for p in PIDS), "id: %d" % len([v for v in chat_ids.values() if v]))
msg_bad = []
for pid in PIDS:
    if not chat_ids.get(pid):
        continue
    sc, body = J("post", "/api/chat/attach-search-result", pid=pid,
                 json={"session_id": chat_ids[pid], "type": "text",
                       "title": "вопрос %s" % marker(pid),
                       "content": "ответ %s" % marker(pid),
                       "url": "https://example.com/%s" % marker(pid)})
    if sc != 200:
        msg_bad.append("%s:%s" % (pid[:8], sc))
control("сообщение добавлено в сессию владельца", not msg_bad, ", ".join(msg_bad[:4]))
chat_leaks = []
# Дисковое доказательство канала утечки снимаем ДО основного цикла: delete в
# цикле выбрасывает сессию из каталога и из глобала, и после него было бы не
# видно, где именно живёт чужая правка.
chat_disk = []
_p_v, _p_a = PRIVATE[0], PRIVATE[1]
_p_sid = chat_ids.get(_p_v)
_p_vf = os.path.join(profile_paths.dir_for(_p_v, "CHAT_DIR"), "%s.json" % _p_sid)
_p_af = os.path.join(profile_paths.dir_for(_p_a, "CHAT_DIR"), "%s.json" % _p_sid)


def _disk_name(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh).get("name")
    except Exception:
        return None


chat_disk.append("до правки: файл жертвы name=%r, у злоумышленника файла чужого id нет=%s"
                 % (_disk_name(_p_vf), not os.path.exists(_p_af)))
_name_before = _disk_name(_p_vf)
J("post", "/api/chat/rename", pid=_p_a,
  json={"session_id": _p_sid, "name": "ПРАВКА ОТ %s" % _p_a[:8]})
_name_after = _disk_name(_p_vf)
_api_name = (J("get", "/api/chat/session?id=%s" % _p_sid, pid=_p_v)[1] or {}).get("name")
chat_disk.append("после правки: файл жертвы name=%r (был %r), API жертве отдаёт name=%r"
                 % (_name_after, _name_before, _api_name))
_ghost = os.path.exists(_p_af)
chat_disk.append("копия чужой сессии в каталоге злоумышленника: %s exists=%s name=%r"
                 % (os.path.basename(_p_af), _ghost, _disk_name(_p_af)))
_cache = getattr(server, "CHAT_SESSIONS", {}) or {}
_globoled = [k for k in _cache if isinstance(k, str)]
_keyed = [k for k in _cache if not isinstance(k, str)]
chat_disk.append("канал: процесс-глобал server.CHAT_SESSIONS — записей %d, из них "
                 "под старым ключом «голый id» %d, под профильным ключом (профиль, id) %d; "
                 "голый id жертвы читается профилем A=%s"
                 % (len(_cache), len(_globoled), len(_keyed),
                    any(isinstance(k, str) and k == _p_sid for k in _keyed)))
for line in chat_disk:
    print("   диск/канал: " + line)
control("чат: чужая правка не дошла до жертвы (файл и выдача name не изменились)",
        _name_after == _name_before and _api_name == _name_before,
        "; ".join(chat_disk))
control("чат: профильный ключ кэша работает (в глобале есть записи (профиль, id))",
        len(_keyed) >= 1, "%d профильных против %d голых" % (len(_keyed), len(_globoled)))
sc_lg, lst_ghost = J("get", "/api/chat/sessions", pid=_p_a)
_gb = blob(lst_ghost)
check("правка чужой сессии не оставляет её копию в моём наборе",
      sc_lg != 200 or (_p_sid not in _gb and marker(_p_v) not in _gb),
      "HTTP %s: после rename по чужому id в моём каталоге лежит файл %s.json, он в списке: %s"
      % (sc_lg, str(_p_sid)[:14], _gb[:120]))
for victim in PRIVATE[:3]:
    sc, lst = J("get", "/api/chat/sessions", pid=victim)
    control("владелец видит свою сессию в списке (%s)" % victim[:8],
            str(chat_ids.get(victim)) in blob(lst), "HTTP %s" % sc)
    for attacker in other_than(victim, 3):
        sc, lst2 = J("get", "/api/chat/sessions", pid=attacker)
        if str(chat_ids.get(victim)) in blob(lst2):
            chat_leaks.append("sessions: %s видит сессию %s" % (attacker[:8], victim[:8]))
        sc, one = J("get", "/api/chat/session?id=%s" % chat_ids.get(victim), pid=attacker)
        if marker(victim) in blob(one):
            chat_leaks.append("session?id: %s читает %s" % (attacker[:8], victim[:8]))
        sc, hist = J("get", "/api/chat/history?id=%s" % chat_ids.get(victim), pid=attacker)
        if marker(victim) in blob(hist):
            chat_leaks.append("history: %s читает %s" % (attacker[:8], victim[:8]))
        sc, ren = J("post", "/api/chat/rename", pid=attacker,
                    json={"session_id": chat_ids.get(victim), "name": "ПЕРЕХВАТ %s" % MARK})
        sc2, own = J("get", "/api/chat/session?id=%s" % chat_ids.get(victim), pid=victim)
        if "ПЕРЕХВАТ %s" % MARK in blob(own):
            chat_leaks.append("rename: %s переименовал сессию %s" % (attacker[:8], victim[:8]))
        sc, dele = J("post", "/api/chat/delete", pid=attacker, json={"session_id": chat_ids.get(victim)})
        sc2, own2 = J("get", "/api/chat/session?id=%s" % chat_ids.get(victim), pid=victim)
        if marker(victim) not in blob(own2) and dele == 200:
            chat_leaks.append("delete: %s удалил сессию %s" % (attacker[:8], victim[:8]))
check("чужие чаты не читаются, не правятся и не удаляются", not chat_leaks, "; ".join(chat_leaks[:8]))
sc, fo = J("get", "/api/chat/folders", pid=PRIVATE[0])
print("   папки чата: HTTP %s %s" % (sc, blob(fo)[:100]))

# ---------- 4. ai-ответы и память ----------
print("\n[4] AI-ответы, память, закладки")
ai_leaks = []
ai_ids = {}
for pid in PIDS:
    J("post", "/api/ai/add", pid=pid,
      json={"question": "В: %s" % marker(pid), "answer": "О: %s" % marker(pid)})
    # id берём из списка владельца, а не из ответа /add: ai_save_answer
    # возвращает не карточку, и молча получить None означало бы «проверка
    # /api/ai/get не выполнялась ни для одного профиля».
    sc, lst = J("get", "/api/ai/list", pid=pid)
    for a in (lst.get("answers") or [] if isinstance(lst, dict) else []):
        if marker(pid) in blob(a):
            ai_ids[pid] = a.get("id")
            break
control("ai-ответ каждого профиля имеет id", len(ai_ids) >= len(PIDS) - 1,
        "id получено для %d из %d" % (len(ai_ids), len(PIDS)))
for victim in PRIVATE[:2]:
    sc, lst = J("get", "/api/ai/list", pid=victim)
    control("владелец видит свой ai-ответ (%s)" % victim[:8], marker(victim) in blob(lst), "HTTP %s" % sc)
    for attacker in other_than(victim, 2):
        sc, lst2 = J("get", "/api/ai/list", pid=attacker)
        if marker(victim) in blob(lst2):
            ai_leaks.append("ai/list %s -> %s" % (attacker[:8], victim[:8]))
        sc, sr = J("get", "/api/ai/search?q=%s" % MARK, pid=attacker)
        if marker(victim) in blob(sr):
            ai_leaks.append("ai/search %s -> %s" % (attacker[:8], victim[:8]))
        if ai_ids.get(victim):
            sc, g = J("get", "/api/ai/get?id=%s" % ai_ids[victim], pid=attacker)
            if marker(victim) in blob(g):
                ai_leaks.append("ai/get по чужому id %s -> %s" % (attacker[:8], victim[:8]))
check("ai-ответы изолированы", not ai_leaks, "; ".join(ai_leaks[:6]))

mem_leaks = []
import memory_module as MM
# Категорию берём настоящую: save_memory приводит неизвестную к "general", и
# контроль «память сохранена» с категорией-выдумкой мерил бы пустой список.
MEMCAT = "facts"
for victim in PRIVATE[:2]:
    sc, body = J("post", "/api/memory/save", pid=victim,
                  json={"category": MEMCAT, "key": "k-%s" % marker(victim),
                        "value": "V %s" % marker(victim), "metadata": {}})
    sc, lst = J("post", "/api/memory/list", pid=victim, json={"category": MEMCAT})
    control("память владельца сохранена (%s)" % victim[:8], marker(victim) in blob(lst), "HTTP %s" % sc)
    for attacker in other_than(victim, 2):
        sc, lst2 = J("post", "/api/memory/list", pid=attacker, json={"category": MEMCAT})
        if marker(victim) in blob(lst2):
            mem_leaks.append("memory/list %s -> %s" % (attacker[:8], victim[:8]))
        sc, sr = J("post", "/api/memory/search", pid=attacker, json={"query": MARK, "category": ""})
        if marker(victim) in blob(sr):
            mem_leaks.append("memory/search %s -> %s" % (attacker[:8], victim[:8]))
# Дисковое доказательство: каталог памяти брался из процесс-глобала один раз при
# импорте, поэтому записи разных профилей лежали вперемешку. После правки —
# каждый в своём, а прежний общий каталог остаётся пустым.
def _dir_blob(d):
    try:
        return " ".join(open(os.path.join(d, f), encoding="utf-8").read()
                        for f in os.listdir(d) if f.endswith(".json"))
    except OSError as e:
        print("   каталог памяти не читается: %s" % e)
        return ""


_own_dirs = {pid: profile_paths.dir_for(pid, "MEMORY_DIR") for pid in PRIVATE[:2]}
_mem_lines = []
_mem_scoped = True
for pid in PRIVATE[:2]:
    b = _dir_blob(_own_dirs[pid])
    others = [other for other in PRIVATE[:2] if other != pid]
    foreign = [other[:8] for other in others if marker(other) in b]
    if marker(pid) not in b or foreign:
        _mem_scoped = False
    _mem_lines.append("%s: свой маркер=%s, чужие в его каталоге=%s (%s)"
                      % (pid[:8], marker(pid) in b, foreign or "нет",
                         os.path.basename(_own_dirs[pid])))
_legacy = _dir_blob(MM.MEMORY_DIR)
_legacy_markers = [pid[:8] for pid in PRIVATE[:2] if marker(pid) in _legacy]
_mem_lines.append("прежний общий MEMORY_DIR=%s: маркеров профилей %s"
                  % (MM.MEMORY_DIR, _legacy_markers or "нет"))
for line in _mem_lines:
    print("   диск: " + line)
control("память лежит в каталоге каждого профиля и не смешивается", _mem_scoped,
        "; ".join(_mem_lines))
check("память изолирована", not mem_leaks, "; ".join(mem_leaks[:6]))

bm_leaks = []
for victim in PRIVATE[:2]:
    J("post", "/api/bookmarks/add", pid=victim,
      json={"url": "https://example.com/%s" % marker(victim), "title": "З %s" % marker(victim),
            "type": "web", "source": "stress", "doc_id": "d-%s" % marker(victim), "preview": ""})
    sc, lst = J("get", "/api/bookmarks", pid=victim)
    control("закладка владельца в списке (%s)" % victim[:8], marker(victim) in blob(lst), "HTTP %s" % sc)
    for attacker in other_than(victim, 2):
        sc, lst2 = J("get", "/api/bookmarks", pid=attacker)
        if marker(victim) in blob(lst2):
            bm_leaks.append("bookmarks %s -> %s" % (attacker[:8], victim[:8]))
        sc, rm = J("post", "/api/bookmarks/remove", pid=attacker,
                   json={"doc_id": "d-%s" % marker(victim), "type": "web"})
        sc2, own = J("get", "/api/bookmarks", pid=victim)
        if marker(victim) not in blob(own) and rm == 200:
            bm_leaks.append("bookmarks/remove %s снёс закладку %s" % (attacker[:8], victim[:8]))
check("закладки изолированы", not bm_leaks, "; ".join(bm_leaks[:6]))

# ---------- 5. шаринг ----------
print("\n[5] «Поделиться» и расшаренные файлы")
share_leaks = []
sc, inbox = J("get", "/api/share/inbox", pid=PRIVATE[0])
sc, mine = J("get", "/api/share/my-links", pid=PRIVATE[0])
print("   inbox HTTP %s %s | my-links HTTP %s %s" % (sc, blob(inbox)[:80], sc, blob(mine)[:80]))
sc, body = J("post", "/api/share/link", pid=PRIVATE[0],
             json={"item_id": note_ids[PRIVATE[0]], "type": "note",
                   "title": "расшарка %s" % MARK, "target_profile_id": PRIVATE[1],
                   "permissions": ["view"]})
share_id = body.get("share_id") or body.get("id") or ""
sc, inc = J("get", "/api/share/inbox", pid=PRIVATE[1])
if share_id and marker(PRIVATE[0]) in blob(inc):
    sc_acc, acc = J("get", "/api/notes/get?id=%s" % note_ids[PRIVATE[0]], pid=PRIVATE[1])
    print("   адресат прочитал расшаренную заметку: HTTP %s (разрешено явным шарингом)" % sc_acc)
sc, inc3 = J("get", "/api/share/inbox", pid=PRIVATE[2])
if share_id and marker(PRIVATE[0]) in blob(inc3):
    share_leaks.append("инвиден чужой расшарки виден третьему профилю")
sc_fsh, fsh = J("post", "/filemanager/share", pid=PRIVATE[0],
            json={"path": roots.get(PRIVATE[0], ""), "profile": PRIVATE[1], "kind": "read", "days": 1})
sc3, in3 = J("get", "/filemanager/share/incoming", pid=PRIVATE[2])
if blob(in3).find(marker(PRIVATE[0])) >= 0 or (sc_fsh == 200 and marker(PRIVATE[1]) in blob(in3)):
    share_leaks.append("filemanager/share/incoming показывает чужие приглашения")
check("расшарки видны только адресату", not share_leaks, "; ".join(share_leaks))

# ---------- 6. статистика ----------
print("\n[6] Статистика профиля")
sc, s1 = J("post", "/api/stats/track", pid=PRIVATE[0],
           json={"module": "notes", "action": "open", "item": marker(PRIVATE[0])})
control("stats/track принимает запись", sc in (200, 201) or "ok" in blob(s1).lower(),
        "HTTP %s %s" % (sc, blob(s1)[:80]))
for victim in [PRIVATE[0]]:
    for attacker in other_than(victim, 2):
        sc, sum2 = J("get", "/api/stats/summary", pid=attacker)
        sc2, d2 = J("get", "/api/stats/daily", pid=attacker)
        if marker(victim) in blob(sum2) or marker(victim) in blob(d2):
            check("статистика %s не показывает события %s" % (attacker[:8], victim[:8]),
                  False, "маркер жертвы в чужом отчёте")
sc, allst = J("get", "/api/stats", pid=PRIVATE[0])
print("   /api/stats: HTTP %s %s" % (sc, blob(allst)[:140]))

# ---------- 7. медиа ----------
print("\n[7] Медиа: свои каталоги, индекс, скачивание, стрим, загрузка, избранное")
if not getattr(server, "MEDIA_AVAILABLE", False):
    print("   MEDIA_AVAILABLE=False — секция не меряется (не «изолировано», а недоступно)")
else:
    import base64
    import media_visibility as MV

    def mp3_bytes(frames=12):
        """Синтетический MP3: MPEG-1 Layer III, 128 кбит/с, 44,1 кГц, без CRC.

        Рамка = 144*128000/44100 = 417 байт. Разбора может не случиться (нет
        ffmpeg в PATH) — индексатор тогда берёт имя из пути, а для гейта важна
        именно строка в базе, а не её длительность.
        """
        frame = bytearray(b"\xff\xfb\x90\x00") + bytes(413)
        return bytes(frame) * frames

    media_dirs, media_files = {}, {}
    for pid in PIDS:
        d = os.path.join(MEDIA, "p" + pid[:8])
        os.makedirs(d, exist_ok=True)
        media_dirs[pid] = d
        p = os.path.join(d, "СТРЕСС-ТРЕК.%s.%s.mp3" % (MARK, pid[:8]))
        with open(p, "wb") as fh:
            fh.write(mp3_bytes())
        media_files[pid] = p

    paths_bad = []
    for pid in PIDS:
        sc, body = J("post", "/api/profiles/save-private-paths", pid=pid,
                     json={"profile_id": pid,
                           "private_paths": {"private_music_dirs": [media_dirs[pid]]}})
        if sc != 200:
            paths_bad.append("%s:HTTP %s %s" % (pid[:8], sc, blob(body)[:60]))
    control("каждый профиль задал свой медиа-каталог", not paths_bad, "; ".join(paths_bad[:3]))
    MV.invalidate()

    def mindex(pid):
        sc, body = J("get", "/api/media/music-index?limit=5000", pid=pid)
        if not isinstance(body, dict) or "tracks" not in body:
            return None, "HTTP %s %s" % (sc, blob(body)[:80])
        return body.get("tracks") or [], "HTTP %s" % sc

    rebuilt = 0
    for pid in PRIVATE:
        try:
            server.rebuild_music_index([media_dirs[pid]])
            rebuilt += 1
        except Exception as e:
            print("   rebuild %s: %s" % (pid[:8], str(e)[:120]))
    control("индекс построен для каталогов всех приватных профилей",
            rebuilt == len(PRIVATE), "%d из %d" % (rebuilt, len(PRIVATE)))
    MV.invalidate()

    track_ids, own_seen = {}, 0
    for pid in PRIVATE:
        tracks, how = mindex(pid)
        if tracks is None:
            print("   music-index %s: %s" % (pid[:8], how))
            continue
        mine = [t for t in tracks if marker(pid) in blob(t)]
        if mine:
            own_seen += 1
            track_ids[pid] = str(mine[0].get("id"))
    control("владелец видит свой трек в индексе", own_seen >= len(PRIVATE) - 1,
            "свой трек у %d из %d" % (own_seen, len(PRIVATE)))

    foreign = []
    for pid in PRIVATE:
        tracks, _ = mindex(pid)
        if tracks is None:
            continue
        b = blob(tracks)
        foreign += ["%s видит %s" % (pid[:8], o[:8]) for o in PRIVATE
                    if o != pid and marker(o) in b]
    check("индекс профиля не показывает чужие приватные треки", not foreign,
          "; ".join(foreign[:6]))

    guest, ghow = mindex(None)
    g_for = [o[:8] for o in PRIVATE if guest and marker(o) in blob(guest)]
    check("гость без cookie не видит приватные библиотеки",
          not g_for, "без cookie видно треки %d профилей (%s)" % (len(g_for), ghow))

    # Второй замер: у общедоступного профиля ПОЯВИЛСЯ свой каталог. Если
    # «профиль без каталогов = без фильтра» был единственной причиной, здесь
    # гость должен стать чистым; если нет — дыра в самом фильтре выдачи.
    try:
        PM.save_media_dirs("music_dirs", [media_dirs[PUB]], profile_id=PUB)
        server.rebuild_music_index([media_dirs[PUB]])
    except Exception as e:
        print("   общедоступный каталог: %s" % str(e)[:120])
    MV.invalidate()
    guest2, ghow2 = mindex(None)
    control("общедоступный профиль видит свой трек",
            bool(guest2) and marker(PUB) in blob(guest2), ghow2)
    g_for2 = [o[:8] for o in PRIVATE if guest2 and marker(o) in blob(guest2)]
    check("гость с собственным каталогом видит только общедоступное",
          not g_for2, "при своих каталогах у общедоступного видно чужие треки: %s"
                      % ", ".join(g_for2[:6]))

    victim, attacker = PRIVATE[1], PRIVATE[2]
    vt = track_ids.get(victim)
    if not vt:
        print("   нет id чужого трека — скачивание/стрим не меряются")
    else:
        dl = []
        sc, _ = RAW("get", "/api/media/music-download?id=%s" % vt, pid=attacker)
        if sc == 200:
            dl.append("music-download чужого трека: HTTP 200")
        sc2, _ = RAW("get", "/api/media/music-download?id=%s" % vt, pid=victim)
        control("владелец скачивает свой трек", sc2 == 200, "HTTP %s" % sc2)

        enc = base64.urlsafe_b64encode(media_files[victim].encode()).decode().rstrip("=")
        sc, _ = RAW("get", "/api/media/stream/%s" % enc, pid=attacker)
        if sc == 200:
            dl.append("stream чужого файла по абсолютному пути: HTTP 200")
        sc3, _ = RAW("get", "/api/media/stream/%s" % enc, pid=victim)
        control("владелец стримит свой файл", sc3 == 200, "HTTP %s" % sc3)
        sc4, _ = RAW("get", "/api/media/stream/%s" % enc)
        if sc4 == 200:
            dl.append("stream чужого файла без cookie: HTTP 200")

        sc5, z = RAW("post", "/api/media/download-zip", pid=attacker,
                     data=json.dumps({"kind": "music", "ids": [vt], "name": "ЧУЖОЕ"}),
                     headers={"Content-Type": "application/json"})
        if sc5 == 200:
            dl.append("download-zip по чужому id: HTTP 200")
        check("чужой трек нельзя скачать, зазиповать или стримить", not dl, "; ".join(dl))

        fav_bad = []
        J("post", "/api/media/favorites/add", pid=attacker, json={"id": vt, "type": "track"})
        sc, fav_v = J("get", "/api/media/favorites", pid=victim)
        sc2, fav_a = J("get", "/api/media/favorites", pid=attacker)
        if vt in blob(fav_v):
            fav_bad.append("избранное жертвы пополнено чужим запросом")
        # Избранное/плейлисты читаются без profile_id вообще (routes
        # legacy_media_player.py:277, 550) — поэтому один список на всех.
        if vt in blob(fav_a) and vt in blob(fav_v):
            fav_bad.append("список избранного общий: %s и %s видят одно и то же"
                           % (attacker[:8], victim[:8]))
        sc, pl = J("post", "/api/media/playlist/create", pid=attacker,
                   json={"name": "ЧУЖАЯ %s" % MARK, "description": marker(victim),
                         "tracks": [vt]})
        plist_id = ""
        if isinstance(pl, dict) and isinstance(pl.get("playlist"), dict):
            plist_id = str(pl["playlist"].get("id") or "")
        sc2, allpl = J("get", "/api/media/playlists", pid=victim)
        if plist_id and plist_id in blob(allpl):
            fav_bad.append("плейлист %s виден в списке профиля %s" % (plist_id[:8], victim[:8]))
        sc3, one = J("get", "/api/media/playlist?id=%s" % plist_id, pid=victim)
        if marker(victim) in blob(one):
            fav_bad.append("чужой плейлист с чужим треком открывается по id у жертвы")
        check("избранное и плейлисты не общие и не пополняются чужими",
              not fav_bad, "; ".join(fav_bad))

        sc, st_before = J("get", "/api/media/statistics", pid=victim)
        J("post", "/api/media/play-log", pid=attacker, json={"id": vt, "duration": 30})
        time.sleep(0.7)
        sc2, st_after = J("get", "/api/media/statistics", pid=victim)
        check("прослушивание чужого трека не меняет статистику жертвы",
              blob(st_before) == blob(st_after),
              "статистика %s изменилась после play-log от %s" % (victim[:8], attacker[:8]))

    # чужие пути профиля
    sc, body = J("post", "/api/profiles/save-private-paths", pid=attacker,
                 json={"profile_id": victim,
                       "private_paths": {"private_music_dirs": [MEDIA]}})
    MV.invalidate()
    tr, _ = mindex(victim)
    check("чужой профиль не переприсваивает медиа-каталоги жертвы",
          sc != 200 and marker(victim) in blob(tr or []),
          "HTTP %s, индекс жертвы: %d треков" % (sc, len(tr or [])))

    # ---------- загрузка файлов в библиотеку ----------
    sc, _ = J("post", "/api/media/settings/save", pid=PRIVATE[0],
              json={"media_upload_enabled": "1"})
    from modules import media_transfer as MT
    control("тумблер «Загрузка в медиатеку» включается", MT.upload_enabled(), "HTTP %s" % sc)
    up_bad, up_landing = [], []
    for pid in PRIVATE[:3]:
        data = mp3_bytes(8)
        fname = "ЗАГРУЗКА.%s.%s.mp3" % (MARK, pid[:8])
        sc, beg = J("post", "/api/media/upload/begin", pid=pid,
                    json={"category": "music", "filename": fname, "size": len(data)})
        sid = beg.get("session_id") if isinstance(beg, dict) else None
        target = beg.get("target") if isinstance(beg, dict) else ""
        if not sid:
            up_bad.append("%s: begin HTTP %s %s" % (pid[:8], sc, blob(beg)[:80]))
            continue
        if os.path.normcase(str(target)) != os.path.normcase(media_dirs[pid]):
            up_landing.append("%s: файл идёт в %s" % (pid[:8], target))
        cookies = {COOKIE: pid}
        r = client.put("/api/media/upload/%s" % sid, cookies=cookies, content=data)
        if r.status_code != 200:
            up_bad.append("%s: chunk HTTP %s %s" % (pid[:8], r.status_code, r.text[:80]))
            continue
        sc, fin = J("post", "/api/media/upload/%s/finish" % sid, pid=pid)
        if sc != 200:
            up_bad.append("%s: finish HTTP %s %s" % (pid[:8], sc, blob(fin)[:80]))
            continue
        if not os.path.isfile(os.path.join(media_dirs[pid], fname)):
            up_landing.append("%s: файла нет в его каталоге после finish" % pid[:8])
    control("загрузка проходит begin/chunk/finish", not up_bad, "; ".join(up_bad[:3]))
    check("загруженный файл попадает в каталог своего профиля", not up_landing,
          "; ".join(up_landing))
    # finish ставит отложенный переиндекс (4 с) — реальный путь, ждём его
    time.sleep(6.0)
    MV.invalidate()
    up_for = []
    for pid in PRIVATE[:3]:
        tracks, _ = mindex(pid)
        b = blob(tracks or [])
        if "ЗАГРУЗКА.%s.%s" % (MARK, pid[:8]) not in b:
            up_for.append("%s: свой загруженный трек не появился в индексе" % pid[:8])
        for o in PRIVATE[3:]:
            if "ЗАГРУЗКА.%s.%s" % (MARK, o[:8]) in b:
                up_for.append("%s видит загрузку %s" % (pid[:8], o[:8]))
    check("загруженное видно только владельцу (после отложенного переиндекса)",
          not up_for, "; ".join(up_for[:4]))

    # ---------- взрыв радиус: очистка индекса (последняя проверка) ----------
    sc, v_before = mindex(victim)
    n_before = len(v_before or [])
    sc, body = J("post", "/api/media/delete-music", pid=attacker)
    time.sleep(1.0)
    MV.invalidate()
    sc2, v_after = mindex(victim)
    n_after = len(v_after or [])
    check("delete-music не стирает индекс чужого профиля",
          not (sc == 200 and n_before > 0 and n_after == 0),
          "POST /api/media/delete-music от %s (HTTP %s) вернул %s; у %s было %d "
          "треков в выдаче, стало %d — маршрут зовёт clear_music_index() без "
          "фильтра по профилю" % (attacker[:8], sc, blob(body)[:60], victim[:8],
                                  n_before, n_after))

    # ---------- общий каталог двух профилей (живой замер 8281, 04.11) ----------
    # Три профиля объявляли своим один и тот же каталог, загрузка одного
    # попадала в библиотеку соседа и игралась у него. Здесь те же двое лезут в
    # один список private_music_dirs: каталог общий, поэтому спасает только
    # память о владельце пути (media_owner).
    sh_a, sh_b = PRIVATE[3], PRIVATE[4]
    shared = os.path.join(MEDIA, "shared")
    os.makedirs(shared, exist_ok=True)
    scan_file = os.path.join(shared, "ОБЩИЙ-СКАН.%s.mp3" % MARK)
    with open(scan_file, "wb") as fh:
        fh.write(mp3_bytes(8))
    for pid in (sh_a, sh_b):
        J("post", "/api/profiles/save-private-paths", pid=pid,
          json={"profile_id": pid, "private_paths": {"private_music_dirs": [shared]}})
    MV.invalidate()
    up_name = "ОБЩИЙ-ЗАГРУЗКА.%s.%s.mp3" % (MARK, sh_a[:8])
    data = mp3_bytes(8)
    sc, beg = J("post", "/api/media/upload/begin", pid=sh_a,
                json={"category": "music", "filename": up_name, "size": len(data)})
    sid = beg.get("session_id") if isinstance(beg, dict) else None
    target = str((beg or {}).get("target") or "")
    landed = None
    if sid:
        client.put("/api/media/upload/%s" % sid, cookies={COOKIE: sh_a}, content=data)
        scf, fin = J("post", "/api/media/upload/%s/finish" % sid, pid=sh_a)
        rel = ((fin or {}).get("files") or [""])[0]
        landed = os.path.join(target, *rel.split("/")) if rel else None
    else:
        scf, fin = sc, beg
    control("загрузка в общий каталог прошла", bool(landed) and scf == 200,
            "HTTP %s %s" % (scf, blob(fin)[:120]))
    try:
        server.rebuild_music_index([shared])
    except Exception as e:
        print("   rebuild shared: %s" % str(e)[:120])
    time.sleep(1.0)
    MV.invalidate()
    tr_a, _ = mindex(sh_a)
    tr_b, _ = mindex(sh_b)
    ba, bb = blob(tr_a or []), blob(tr_b or [])
    sh_bad = []
    if up_name not in ba:
        sh_bad.append("владелец не видит свою загрузку в общем каталоге")
    if up_name in bb:
        sh_bad.append("%s видит загрузку %s" % (sh_b[:8], sh_a[:8]))
    if os.path.basename(scan_file) not in bb:
        sh_bad.append("контроль сорвался: отсканированный файл общего каталога "
                      "невиден обоим — значит, hides не владелец, а настройка каталога")
    check("загруженное одним профилем в общий каталог скрыто от другого",
          not sh_bad, "; ".join(sh_bad))
    if landed:
        enc = base64.urlsafe_b64encode(landed.encode()).decode().rstrip("=")
        s1, _ = RAW("get", "/api/media/stream/%s" % enc, pid=sh_b)
        s2, _ = RAW("get", "/api/media/stream/%s" % enc, pid=sh_a)
        s3, _ = RAW("get", "/api/media/stream/%s"
                     % base64.urlsafe_b64encode(scan_file.encode()).decode().rstrip("="),
                    pid=sh_b)
        check("прямая ссылка на чужую загрузку не играется, свои — играются",
              s1 != 200 and s2 in (200, 206) and s3 in (200, 206),
              "чужая загрузка: HTTP %s, своя: HTTP %s, общий скан: HTTP %s" % (s1, s2, s3))

# ---------- 8. расширения ----------
print("\n[8] Расширения: установка, список, включение, хранилище, удаление")
import extension_system as ES

ext_deps = [getattr(d, "dependency", None) for d in getattr(r_ext.router, "dependencies", [])]
check("роутер расширений привязан к профилю запроса (profile_paths.bind)",
      any(getattr(f, "__name__", "") == "bind" for f in ext_deps),
      "у routes/extension.py нет зависимости bind: менеджер берёт каталог из "
      "глобального BASE_DIR, то есть из «последнего переключившегося» профиля")

ext_host = PRIVATE[0]
ext_attacker = PRIVATE[1]
ext_outsider = PRIVATE[2]
ext_dir = {pid: profile_paths.dir_for(pid, "EXTENSIONS_DIR") for pid in PIDS}
# Полигон воспроизводит состояние живого сервера: применить globals профиля
# host — это и есть set_base_dir(host) (modules/profile_state.py:194).
ES.set_base_dir(ext_dir[ext_host])
print("   BASE_DIR=%s (профиль %s), каталог профиля %s=%s" %
      (ES.BASE_DIR, ext_host[:8], ext_attacker[:8], ext_dir[ext_attacker]))

EXT_NAME = "stress_%s" % MARK
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as zf:
    zf.writestr("extension.json", json.dumps(
        {"name": EXT_NAME, "version": "1.0.0", "description": "стресс %s" % MARK,
         "target_module": "notes", "permissions": ["storage"]}, ensure_ascii=False))
    zf.writestr("frontend.js", "// %s" % MARK)
sc, body = J("post", "/api/extensions/upload", pid=ext_attacker, content=buf.getvalue(),
             headers={"Content-Type": "application/octet-stream"})
installed_host = os.path.isdir(os.path.join(ext_dir[ext_host], EXT_NAME))
installed_own = os.path.isdir(os.path.join(ext_dir[ext_attacker], EXT_NAME))
control("установка расширения прошла (HTTP %s)" % sc,
        isinstance(body, dict) and bool(body.get("ok")), blob(body)[:120])
check("расширение установилось в каталог профиля-владельца запроса",
      installed_own and not installed_host,
      "cookie профиля %s: в его каталоге %s, в каталоге активного профиля %s — %s"
      % (ext_attacker[:8], installed_own, ext_host[:8], installed_host))

sc, lstA = J("get", "/api/extensions", pid=ext_attacker)
sc, lstB = J("get", "/api/extensions", pid=ext_outsider)
both_see = EXT_NAME in blob(lstA) and EXT_NAME in blob(lstB)
check("чужой профиль не видит это расширение в своём списке", not both_see,
      "список один на всех: %s и %s видят %s" % (ext_attacker[:8], ext_outsider[:8], EXT_NAME))

sc, st = J("post", "/api/extensions/%s/storage/k" % EXT_NAME, pid=ext_outsider,
           json={"value": "ЧУЖОЕ %s" % MARK})
sp = os.path.join(ext_dir[ext_attacker], EXT_NAME, "storage.json")
written = os.path.isfile(sp) and MARK in open(sp, encoding="utf-8").read()
check("чужой профиль не пишет в хранилище расширения", not written,
      "POST storage от %s (HTTP %s) записал %s в каталог профиля %s"
      % (ext_outsider[:8], sc, sp, ext_attacker[:8]))

sc, meta0 = J("get", "/api/extensions/%s/metadata" % EXT_NAME, pid=ext_attacker)
J("post", "/api/extensions/%s/sandbox-choice" % EXT_NAME, pid=ext_attacker,
  json={"sandbox": True})
sc_en, en = J("post", "/api/extensions/%s/enable" % EXT_NAME, pid=ext_attacker)
sc, meta1 = J("get", "/api/extensions/%s/metadata" % EXT_NAME, pid=ext_attacker)
on = bool((meta1.get("metadata") or {}).get("enabled")) if isinstance(meta1, dict) else False
control("владелец включает своё расширение", on, "enable HTTP %s %s" % (sc_en, blob(en)[:80]))
# Каталог, применённый к процессу вручную (строка ES.set_base_dir выше), больше
# не даёт обзору чужого профиля увидеть это расширение.
sc, meta_host = J("get", "/api/extensions/%s/metadata" % EXT_NAME, pid=ext_host)
host_sees = isinstance(meta_host, dict) and bool((meta_host.get("metadata") or {}))
check("профиль, не устанавливавший расширение, не получает его метаданные", not host_sees,
      "метаданные расширения владельца %s ушли профилю %s (HTTP %s)"
      % (ext_attacker[:8], ext_host[:8], sc))
sc_di, dis = J("post", "/api/extensions/%s/disable" % EXT_NAME, pid=ext_outsider)
sc, meta2 = J("get", "/api/extensions/%s/metadata" % EXT_NAME, pid=ext_attacker)
off = not bool((meta2.get("metadata") or {}).get("enabled")) if isinstance(meta2, dict) else True
check("чужой профиль не управляет чужим расширением", not (on and off),
      "disable от %s (HTTP %s) выключил расширение, которое включил и кому принадлежит %s"
      % (ext_outsider[:8], sc_di, ext_attacker[:8]))

sc, dele = J("delete", "/api/extensions/%s" % EXT_NAME, pid=ext_outsider)
gone = not os.path.isdir(os.path.join(ext_dir[ext_attacker], EXT_NAME))
check("чужой профиль не удаляет чужое расширение", not (dele == 200 and gone),
      "DELETE от %s (HTTP %s) снёс каталог профиля %s" % (ext_outsider[:8], sc, ext_attacker[:8]))
ES.set_base_dir(ext_dir[PUB])

# ---------- 9. галка «показывать данные общедоступного профиля» ----------
print("\n[9] Галка общедоступного набора: по умолчанию выкл, включённая даёт только общедоступное")
from modules import public_view as PV

bystander = PRIVATE[3]
own_marker_seen = [m for m in (marker(PUB), marker(PRIVATE[5])) if m in blob(J("get", "/api/notes/list", pid=bystander)[1])]
control("выключенная галка: профиль %s не видит заметку общедоступного" % bystander[:8],
        marker(PUB) not in own_marker_seen, "в списке замечены маркеры: %s" % own_marker_seen)

# 9.1 путь интерфейса: страница шлёт галку в config через POST /api/config/save
# (page_settings.html:4246, libPayload()); frontend_state для неё не используется.
pd_cfg = {k: (k == "notes") for k in PV.CATEGORIES}
sc, body = J("post", "/api/config/save", pid=bystander,
             json={"config": {"public_data_enabled": pd_cfg}})
sc, flags = J("get", "/api/profiles/public-data", pid=bystander)
got_notes = bool((flags or {}).get("flags", {}).get("notes")) if isinstance(flags, dict) else False
control("галка доезжает до сервера через /api/config/save (путь страницы настроек)",
        got_notes, "HTTP %s, /api/profiles/public-data отдал %s" % (sc, blob(flags)[:160]))
J("post", "/api/config/save", pid=bystander,
  json={"config": {"public_data_enabled": {k: False for k in PV.CATEGORIES}}})
profile_paths._CFG.pop(bystander, None)

# 9.2 механизм чтения флага — конфиг профиля; пишем его напрямую, чтобы
# проверить само поведение галки независимо от того, где она сохраняется.
cfg = PM.get_profile_config(bystander) or {}
cfg["public_data_enabled"] = {"notes": True, "chat": True}
PM.save_profile_config(bystander, cfg)
sc, lst_pub = J("get", "/api/notes/list", pid=bystander)
b = blob(lst_pub)
control("включённая галка показывает заметку общедоступного профиля",
        marker(PUB) in b, "HTTP %s, маркера общедоступного нет" % sc)
leaked_private = [o[:8] for o in PRIVATE if o != bystander and marker(o) in b]
check("галка не выпускает приватные заметки других профилей", not leaked_private,
      "при включённой галке видно заметки профилей: %s" % ", ".join(leaked_private[:6]))
sc, one_pub = J("get", "/api/notes/get?id=%s" % note_ids[PUB], pid=bystander)
control("включённая галка открывает общедоступную заметку по id",
        marker(PUB) in blob(one_pub), "HTTP %s" % sc)
sc, ch = J("get", "/api/chat/sessions", pid=bystander)
control("галка chat распространяется на общедоступные чаты",
        str(chat_ids.get(PUB)) in blob(ch), "HTTP %s, сессии общедоступного %s нет" % (sc, blob(ch)[:120]))
cfg = PM.get_profile_config(bystander) or {}
cfg["public_data_enabled"] = {k: False for k in PV.CATEGORIES}
PM.save_profile_config(bystander, cfg)
profile_paths._CFG.pop(bystander, None)
sc, lst_off = J("get", "/api/notes/list", pid=bystander)
check("снятая галка снова прячет общедоступный набор",
      marker(PUB) not in blob(lst_off), "после выключения маркер общедоступного остался в списке")

# ---------- 10. «поделиться» с общедоступным профилем ----------
print("\n[10] Расшарка в общедоступный профиль (жалоба «нельзя самому себе»)")
sc, selfshare = J("post", "/api/share/link", pid=PRIVATE[0],
                  json={"item_id": note_ids[PRIVATE[0]], "type": "note",
                        "title": "себе %s" % MARK, "target_profile_id": PRIVATE[0],
                        "permissions": ["view"]})
check("расшарка самой себе отказана", "самому себе" in blob(selfshare),
      "HTTP %s %s" % (sc, blob(selfshare)[:120]))
sc, pub_share = J("post", "/api/share/link", pid=PRIVATE[0],
                  json={"item_id": note_ids[PRIVATE[0]], "type": "note",
                        "title": "в общий %s" % MARK, "target_profile_id": PUB,
                        "permissions": ["view"]})
ok_pub = isinstance(pub_share, dict) and "самому себе" not in blob(pub_share) \
    and pub_share.get("ok", True) is not False
control("приватный профиль может поделиться с общедоступным", ok_pub,
        "HTTP %s %s" % (sc, blob(pub_share)[:140]))
sc, pub_inbox = J("get", "/api/share/inbox", pid=PUB)
pub_sid = (pub_share or {}).get("share_id") or ""
control("общедоступный профиль получил расшарку",
        bool(pub_sid) and pub_sid in blob(pub_inbox),
        "HTTP %s share_id=%s inbox=%s" % (sc, pub_sid, blob(pub_inbox)[:140]))
# Расшарка НЕ должна копировать заметку в каталог общедоступного и становиться
# видимой всем, у кого включена галка: проверим у bystander с включённой галкой.
cfg = PM.get_profile_config(bystander) or {}
cfg["public_data_enabled"] = {"notes": True}
PM.save_profile_config(bystander, cfg)
profile_paths._CFG.pop(bystander, None)
sc, lst3 = J("get", "/api/notes/list", pid=bystander)
check("расшаренная в общедоступный заметка не показана всем с включённой галкой",
      marker(PRIVATE[0]) not in blob(lst3),
      "заметка профиля %s утекла в список профиля %s через общедоступный" % (PRIVATE[0][:8], bystander[:8]))
sc, rev = J("post", "/api/share/revoke", pid=PRIVATE[0],
            json={"profile_id": PRIVATE[0], "share_id": (pub_share or {}).get("share_id", "")})
print("   revoke расшарки: HTTP %s %s" % (sc, blob(rev)[:100]))
cfg = PM.get_profile_config(bystander) or {}
cfg["public_data_enabled"] = {k: False for k in PV.CATEGORIES}
PM.save_profile_config(bystander, cfg)
profile_paths._CFG.pop(bystander, None)

print("\n[i] ИТОГО: проверок %d, утечек %d, сломанных контроли %d" % (TOTAL, len(LEAKS), len(CONTROLS)))
for l in LEAKS:
    print("   УТЕЧКА: %s" % l[:260])
for c in CONTROLS:
    print("   КОНТРОЛЬ: %s" % c[:200])
print("\n   полигон: %s" % POLY)
