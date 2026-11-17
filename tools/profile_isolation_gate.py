"""Гейт изоляции профилей: данные профиля запроса, а не активного профиля.

Две части.

Живая матрица (главная). Из настоящих router-модулей собирается приложение и
каждый запрос идёт с cookie конкретного профиля. Проверяются три свойства:
  1) ответ API == прямое чтение того же хранилища в том же профиле — значит,
     зависимость profile_paths.bind и перенос контекста в поток пула работают;
  2) наборы id попарно не пересекаются между профилями — значит, данные не
     подмешиваются;
  3) чужая заметка/чат по чужому id не отдаётся, а неизвестный cookie
     разворачивается в общедоступный профиль, а не в «последний переключившийся»;
  4) коллаборация заметок: владелец комнаты, списки комнат, приглашения и вход
     по WebSocket берутся из профиля запроса (bind на HTTP-маршрутах и ручная
     постановка SCOPE на WebSocket — зависимость к нему не подключается);
  5) галка «показывать данные общедоступного профиля»: без ключа профиль видит
     ровно своё (как до появления галки), с ключом к своему набору добавляется
     общедоступный — без двойников, и запись уходит туда, где файл нашли.

Статическая часть. Router-модуль, который читает каталоги профиля
(server.NOTES_DIR и компания), обязан вешать на роутер profile_paths.bind, иначе
он обслуживается глобалами активного профиля. Исключения перечислены явно с
причиной — новый файл без bind роняет гейт, а не проходит молча. Тем же
правилом покрыты модули вне routes/ (проверка 12): то, что сознательно остаётся
процесс-глобальным, помечено KNOWN LEAK и печатается в отчёт.

Запуск: PYTHONIOENCODING=utf-8 E:/compilinfinity/venv/Scripts/python.exe tools/profile_isolation_gate.py
"""
import asyncio
import glob
import json
import os
import re
import sys
from concurrent import futures

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

from fastapi import FastAPI
from starlette.testclient import TestClient

import profile_paths
from modules import chat_store, notes_store
from routes import chat as chat_routes
from routes import legacy_notes_ws as notes_routes
from routes import legacy_memory_bookmarks as mem_routes
from routes import profiles as profiles_routes
from routes import legacy_search_config_books as cfg_routes

COOKIE = "infinity_profile"
DIR_ATTRS = ("NOTES_DIR", "AI_DIR", "CHAT_DIR", "MEMORY_DIR",
             "ATTACHMENTS_DIR", "PLUGINS_DIR", "EXTENSIONS_DIR")

# Маршруты, которые обязаны различать профили.
# Порядок важен: у legacy_collaboration есть catch-all /{path:path}, в живом
# приложении он подключается последним, поэтому профили, AI и сохранение
# конфига вешаем до него — иначе гейт мерил бы 404 вместо чужих данных.
app_ = FastAPI()
app_.include_router(notes_routes.router)
app_.include_router(chat_routes.router)
app_.include_router(profiles_routes.router)
app_.include_router(mem_routes.router)
app_.include_router(cfg_routes.router)
client = TestClient(app_)

FAILS = []


def check(label, ok, detail=""):
    print("  [%s] %s%s" % ("OK" if ok else "FAIL", label, (" — " + detail) if detail else ""))
    if not ok:
        FAILS.append("%s%s" % (label, (" (" + detail + ")") if detail else ""))


def api_notes(pid):
    r = client.get("/api/notes/list", cookies={COOKIE: pid} if pid else {})
    if r.status_code != 200:
        return "HTTP %s" % r.status_code
    return [a.get("id") for a in (r.json().get("articles") or [])]


def api_chat(pid):
    r = client.get("/api/chat/sessions", cookies={COOKIE: pid} if pid else {})
    if r.status_code != 200:
        return "HTTP %s" % r.status_code
    return [s.get("id") for s in (r.json().get("sessions") or [])]


def api_ai(pid):
    r = client.get("/api/ai/list", cookies={COOKIE: pid} if pid else {})
    if r.status_code != 200:
        return "HTTP %s" % r.status_code
    return [a.get("id") for a in (r.json().get("answers") or [])]


def share_index_of(pid):
    """Индекс расшарок профиля: сюда падает приглашение в комнату."""
    try:
        import share_module
        return share_module._load_share_index(pid) or {}
    except Exception:
        return {}


def api_json(path, pid=None):
    r = client.get(path, cookies={COOKIE: pid} if pid else {})
    if r.status_code != 200:
        return {"_http": r.status_code, "_body": r.text[:200]}
    return r.json()


def ws_join(nid, token, uid, pid):
    """Первый кадр коллаб-WebSocket от имени профиля pid."""
    got = {}
    try:
        with client.websocket_connect("/api/collab/ws?note=%s&token=%s&uid=%s" %
                                      (nid, token, uid), cookies={COOKIE: pid}) as ws:
            got = ws.receive_json()
    except (asyncio.CancelledError, futures.CancelledError):
        # Отмена поднимается уже на выходе из контекста: обработчик в своём
        # finally ждёт отменённый task писателя, а await отменённого таска —
        # BaseException, который не ловится его except Exception. Кадры к
        # этому моменту получены, поэтому для гейта это штатный шум.
        pass
    return got


profiles = [p.get("id") for p in (profile_paths._pm().list_profiles() or []) if p.get("id")]
print("Зарегистрированные профили: %s" % ", ".join(profiles))

print("\n1. /api/notes/list: ответ == прямое чтение хранилища этого профиля")
notes_by_pid = {}
for pid in profiles:
    got = api_notes(pid)
    with profile_paths.scope(pid):
        direct = [a.get("id") for a in (notes_store.notes_list_articles("", "date").get("articles") or [])]
    notes_by_pid[pid] = got
    check("notes %s" % pid, got == direct, "%d через API, %d напрямую" % (len(got), len(direct)))

print("\n2. Ответ каждого профиля — только файлы из каталога этого профиля")


def disk_ids(pid, dir_name):
    root = profile_paths.dir_for(pid, dir_name)
    got = set()
    for _, _, files in os.walk(root):
        got.update(os.path.splitext(f)[0] for f in files if f.endswith(".json"))
    return got


for pid in profiles:
    ids = notes_by_pid.get(pid)
    if not isinstance(ids, list):
        check("notes %s отвечал списком id" % pid, False, str(ids))
        continue
    outside = [i for i in ids if i not in disk_ids(pid, "NOTES_DIR")]
    check("notes %s: %d id все лежат в его каталоге" % (pid, len(ids)), not outside,
          "чужих: %s" % outside[:3])

overlap = {}
for pid in profiles:
    for other in profiles:
        if other <= pid:
            continue
        shared = set(notes_by_pid.get(pid) or []) & set(notes_by_pid.get(other) or [])
        if shared:
            overlap[(pid, other)] = sorted(shared)
if overlap:
    print("   совпадающие id между профилями (файлы-дубликаты, заведённые до фикса):")
    for (a, b), ids in overlap.items():
        print("     %s/%s: %d — %s" % (a, b, len(ids), ", ".join(ids[:3])))
    print("   Это не утечка: каждый профиль отдаёт свой файл с таким id; "
          "проверка 2 подтверждает, что id берутся из своего каталога.")

print("\n3. /api/chat/sessions по профилям")
chat_by_pid = {}
for pid in profiles:
    got = api_chat(pid)
    with profile_paths.scope(pid):
        direct = [s.get("id") for s in (chat_store.chat_list_sessions(None) or [])]
    chat_by_pid[pid] = got
    check("chat %s" % pid, got == direct, "%d через API, %d напрямую" % (len(got), len(direct)))
seen_c = {}
clash = []
for pid in profiles:
    ids = chat_by_pid.get(pid)
    if not isinstance(ids, list):
        continue
    own = disk_ids(pid, "CHAT_DIR")
    outside = [i for i in ids if i not in own]
    check("chat %s: %d сессий все лежат в его каталоге" % (pid, len(ids)), not outside,
          "чужих: %s" % outside[:3])
    for other in profiles:
        if other <= pid:
            continue
        shared = sorted(set(ids) & set(chat_by_pid.get(other) or []))
        if shared:
            clash.append("%s/%s: %d совпадающих id (файлы-дубликаты: %s)" % (
                pid, other, len(shared), ", ".join(shared[:2])))
if clash:
    print("   " + "\n   ".join(clash))
    print("   Совпадение id — следствие записей прошлого (пишущий профиль был активный), "
          "а не утечка: содержимое каждый раз читается из своего каталога.")

print("\n4. Неизвестный cookie не попадает в чужой профиль")
guest = api_notes("net-takogo-profilya")
check("неизвестный cookie == общедоступный профиль", guest == notes_by_pid.get(
    profile_paths._pm().get_default_profile_id()), "%d заметок" % (len(guest) if isinstance(guest, list) else guest))

print("\n5. Чужая заметка по чужому id не читается")
# Берём id, которого нет больше ни у одного профиля: если файл с таким id лежит
# в двух каталогах (дубликаты прошлого), «найденная» заметка — своя копия, и
# проверка давала бы ложный провал.
foreign = {}
for p in profiles:
    mine = set(notes_by_pid.get(p) or [])
    others = set()
    for q in profiles:
        if q != p:
            others |= set(notes_by_pid.get(q) or [])
    foreign[p] = sorted(mine - others)
victim = next(((p, ids[0]) for p, ids in foreign.items() if ids), None)
if victim:
    victim_pid, vid = victim
    intruder = next((p for p in profiles if p != victim_pid), "default")
    r = api_json("/api/notes/get?id=%s" % vid, intruder)
    check("заметка %s профиля %s не отдаётся профилю %s" % (vid, victim_pid, intruder),
          isinstance(r, dict) and r.get("id") != vid and bool(r.get("error")),
          json.dumps(r, ensure_ascii=False)[:90])
    r = api_json("/api/custom-mode/list?id=%s" % vid, intruder)
    check("микросервисы (канбан) чужой заметки не перечисляются",
          isinstance(r, dict) and not r.get("artifacts"), json.dumps(r, ensure_ascii=False)[:90])
else:
    FAILS.append("нет заметки с уникальным id для проверки 5")

print("\n6. Обратный обход профилей: соединение не наследует предыдущий профиль")
for pid in reversed(profiles):
    if isinstance(notes_by_pid.get(pid), list):
        check("повторный запрос %s даёт те же данные" % pid, api_notes(pid) == notes_by_pid[pid])

print("\n7. Статика: кто читает каталоги профиля, тот обязан вешать bind")
EXEMPT = {
    "routes/admin.py": "админка показывает пути активного профиля как диагностику, данные профиля не отдаёт",
    "routes/legacy_system.py": "системная сводка путей активного профиля",
}
reader = re.compile(r"\bserver\.(" + "|".join(DIR_ATTRS) + r")\b"
                    r"|getattr\(server, *[\"'](" + "|".join(DIR_ATTRS) + r")[\"']"
                    r"|\b_pp\.(" + "|".join(DIR_ATTRS) + r")\b")
bound = re.compile(r"profile_paths\.bind")
for path in sorted(glob.glob(os.path.join(APP, "routes", "*.py"))):
    src = open(path, encoding="utf-8").read()
    name = "routes/" + os.path.basename(path)
    if not reader.search(src):
        continue
    if bound.search(src) or name in EXEMPT:
        check("bind в %s" % name, True, EXEMPT.get(name, ""))
        continue
    n = len(reader.findall(src))
    check("%s читает каталоги профиля (%d мест) и не вешает bind" % (name, n), False)

print("\n8. Каждый router-модуль импортируется")
# Потеря маршрута — молчаливый класс регрессий: main.py подключает роутеры в
# try/except, поэтому NameError в модуле (например, _pp = proxy(server) раньше,
# чем импортирован server) не роняет сервер, а просто убирает маршруты.
import importlib
bad = []
total = 0
for path in sorted(glob.glob(os.path.join(APP, "routes", "*.py"))):
    name = os.path.basename(path)[:-3]
    if name.startswith("_") or name == "main":
        continue
    total += 1
    try:
        importlib.import_module("routes." + name)
    except Exception as e:
        bad.append("%s: %s: %s" % (name, type(e).__name__, e))
check("все %d router-модулей импортируются" % total, not bad, " | ".join(bad[:3]))

print("\n9. «Поделиться»: источник — профиль запроса, чужой профиль не подставится")
from routes import share as share_routes
app_.include_router(share_routes.router)

nonadmin = next((p for p in profiles if p not in ("default", "admin")), None)
if nonadmin:
    other = "admin" if nonadmin != "admin" else "default"
    r = client.post("/api/share/publish", cookies={COOKIE: nonadmin},
                    json={"profile_id": other, "type": "note", "item_id": "x"})
    check("публикация от чужого профиля отклонена (403)", r.status_code == 403,
          "%d: %s" % (r.status_code, r.text[:80]))
    # Свой профиль доходит до модуля: заведомо неизвестный тип возвращает ошибку
    # разбора, не трогая диск — значит, 403 тут не при чём.
    r = client.post("/api/share/publish", cookies={COOKIE: nonadmin},
                    json={"type": "нет-такого-типа", "item_id": "x"})
    check("публикация в свой профиль не блокируется", r.status_code == 200,
          "%d: %s" % (r.status_code, r.text[:80]))
    own = client.get("/api/share/my-links", cookies={COOKIE: nonadmin}).text
    foreign = client.get("/api/share/my-links?profile_id=%s" % other,
                         cookies={COOKIE: nonadmin}).text
    check("чужой profile_id в списке ссылок игнорируется", own == foreign,
          "свой=%d чужой=%d байт" % (len(own), len(foreign)))
else:
    print("   нет не-административного профиля для проверки")

print("\n10. Сессии агента: бесхозные видны только общедоступному и админу")
from routes import agentai_bridge as bridge
ghost = "_sid-нет-в-реестре"
check("бесхозная сессия скрыта от чистого профиля",
      bridge._visible_session(nonadmin or "4bef8875", ghost, {}) is False)
check("бесхозная сессия видна общедоступному",
      bridge._visible_session("default", ghost, {}) is True)
check("бесхозная сессия видна админу", bridge._visible_session("admin", ghost, {}) is True)
check("своя сессия видна владельцу",
      bridge._visible_session("admin", "s1", {"s1": "admin"}) is True)
check("чужая сессия скрыта",
      bridge._visible_session("default", "s1", {"s1": "admin"}) is False)

print("\n11. Коллаборация заметок: владелец комнаты, inbox и права — профиль запроса")
import collab_module
from routes import legacy_collaboration as collab_routes
app_.include_router(collab_routes.router)

# id комнаты заведомо не-число: get_my_rooms обходит мета-файлы с числовыми
# именами, поэтому тестовая комната не просочится в списки других профилей
# даже до очистки.
cnid = "gate-isolation-%s" % os.getpid()
owner_pid = nonadmin or "default"
other_pid = next((p for p in profiles if p != owner_pid), "default")
try:
    r = client.post("/api/collab/start", cookies={COOKIE: owner_pid},
                    json={"note_id": cnid, "name": "гейт"})
    check("комната создана профилем %s" % owner_pid,
          r.status_code == 200 and r.json().get("ok") is True,
          "%s: %s" % (r.status_code, r.text[:80]))
    st_owner = api_json("/api/collab/status?note=%s&token=%s" %
                        (cnid, r.json().get("token", "")), owner_pid)
    check("владелец комнаты — профиль запроса, а не активный профиль сервера",
          st_owner.get("owner_profile") == owner_pid and st_owner.get("is_owner") is True,
          json.dumps(st_owner, ensure_ascii=False)[:120])
    st_other = api_json("/api/collab/status?note=%s&token=%s" %
                        (cnid, r.json().get("token", "")), other_pid)
    check("чужой профиль %s комнаты не видит (invite-режим)" % other_pid,
          st_other.get("active") is False and st_other.get("locked") is True,
          json.dumps(st_other, ensure_ascii=False)[:120])
    mine = [x.get("nid") for x in (api_json("/api/collab/my-rooms", owner_pid).get("rooms") or [])]
    theirs = [x.get("nid") for x in (api_json("/api/collab/my-rooms", other_pid).get("rooms") or [])]
    check("my-rooms: комната у владельца есть и её нет у чужого профиля",
          cnid in mine and cnid not in theirs, "свой=%s чужой=%s" % (mine[:3], theirs[:3]))
    tok = r.json().get("token", "")
    inv = client.post("/api/collab/invite", cookies={COOKIE: other_pid},
                      json={"note_id": cnid, "token": tok,
                            "target_profile_id": owner_pid})
    check("приглашение владельца комнаты отклоняется (он и так внутри)",
          inv.status_code == 200 and (inv.json().get("ok") is False),
          json.dumps(inv.json(), ensure_ascii=False)[:120])
    # Право приглашать: раньше его давал один токен комнаты, и перехвативший
    # токен вписывал произвольные профили в allowed_profiles (и переписывал мету).
    third_pid = next((p for p in profiles if p not in (owner_pid, other_pid)), None)
    if third_pid:
        denied_inv = client.post("/api/collab/invite", cookies={COOKIE: other_pid},
                                 json={"note_id": cnid, "token": tok,
                                       "target_profile_id": third_pid})
        check("профиль без роли пригласить не может (%s)" % third_pid,
              denied_inv.json().get("ok") is False
              and denied_inv.json().get("code") == "not_owner",
              json.dumps(denied_inv.json(), ensure_ascii=False)[:120])
        idx = json.dumps(share_index_of(third_pid), ensure_ascii=False)
        check("чужое приглашение не дописано в share_index %s" % third_pid,
              cnid not in idx, idx[:120])

    # WebSocket: зависимость bind() к нему не подключается, профиль ставится
    # вручную — проверка, что участник регистрируется своим профилем и чужой
    # с тем же токеном не входит.
    snap = ws_join(cnid, tok, "uid-owner", owner_pid)
    ids = [u.get("profile_id") for u in (snap.get("users") or {}).values()]
    check("в_WS участник закреплён за профилем запроса",
          snap.get("type") != "error" and ids and set(ids) == {owner_pid},
          json.dumps(snap, ensure_ascii=False)[:120])
    denied = ws_join(cnid, tok, "uid-guest", other_pid)
    check("в_WS чужой профиль с тем же токеном не входит",
          denied.get("type") == "error", json.dumps(denied, ensure_ascii=False)[:120])

    # Жалоба пользователя: с чистого профиля делились заметкой с общедоступным
    # и получали «Нельзя расшарить самому себе» — источник брался из активного
    # профиля сервера и совпадал с целью. После правки источник = профиль
    # запроса, поэтому приглашение проходит, а не упирается в «сам себе».
    inv2 = client.post("/api/collab/invite", cookies={COOKIE: owner_pid},
                       json={"note_id": cnid, "token": tok,
                             "target_profile_id": other_pid, "title": "гейт"})
    share_id = inv2.json().get("share_id")
    check("пригласить ДРУГОЙ профиль можно (было «Нельзя расшарить самому себе»)",
          inv2.json().get("ok") is True,
          json.dumps(inv2.json(), ensure_ascii=False)[:120])
    inbox = api_json("/api/collab/inbox", other_pid)
    items = json.dumps(inbox, ensure_ascii=False)
    check("приглашение легло в inbox целевого профиля", cnid in items, items[:120])
    st_after = api_json("/api/collab/status?note=%s&token=%s" % (cnid, tok), other_pid)
    check("после приглашения целевой профиль видит комнату",
          st_after.get("active") is True and st_after.get("owner_profile") == owner_pid,
          json.dumps(st_after, ensure_ascii=False)[:120])
    if share_id:
        import share_module
        rv = share_module.revoke_share(owner_pid, share_id)
        check("тестовая расшарка отозвана (индексы профилей чистые)",
              rv.get("ok") is True, json.dumps(rv, ensure_ascii=False)[:80])
finally:
    with collab_module.ROOMS_LOCK:
        collab_module.ROOMS.pop(cnid, None)
    try:
        os.remove(collab_module._meta_path(cnid))
    except OSError:
        pass
    check("тестовая комната убрана (мета и память)",
          cnid not in collab_module.ROOMS and not os.path.exists(collab_module._meta_path(cnid)))

print("\n12. Статика: модули вне routes/, которые читают каталоги профиля")
# Внутри routes/ правило уже проверяется проверкой 7. Здесь — хранилища и
# сервисы: они вызываются из связанных bind'ом маршрутов, поэтому обязаны
# читать profile_paths, а не глобалы сервера. Что сознательно остаётся
# процесс-глобальным — перечислено с причиной; новый файл роняет гейт.
GLOBAL_OK = {
    "modules/profile_state.py": "пишет глобалы (apply_profile_globals) — здесь им и место",
    "profile_paths.py": "реализация прокси: читает глобалы как фолбэк вне запроса",
    "collab_module.py": "_remote_room_ids/handle_remote_invite — фон координации, запроса нет",
    "modules/index_writer.py": "KNOWN LEAK: поисковый индекс процесс-глобален, собирается из каталогов активного профиля",
    "modules/search_unified.py": "KNOWN LEAK: выдача unified-поиска не фильтруется по профилю",
    "modules/tts_plugins.py": "KNOWN LEAK: PLUGINS_DIR общий на процесс (задача #35)",
}
modpat = re.compile(r"\b(?:server|_sb)\.(" + "|".join(DIR_ATTRS) + r")\b"
                    r"|getattr\((?:server|_sb), *[\"'](" + "|".join(DIR_ATTRS) + r")[\"']")
leaks, unknown = [], []
for path in sorted(glob.glob(os.path.join(APP, "**", "*.py"), recursive=True)):
    rel = os.path.relpath(path, APP).replace("\\", "/")
    if rel.startswith("routes/") or re.match(r"(hermes_module|trash|legacy)/", rel):
        continue
    src = open(path, encoding="utf-8").read()
    n = len(modpat.findall(src))
    if not n:
        continue
    if rel in GLOBAL_OK:
        leaks.append("%s (%d)" % (rel, n))
        continue
    unknown.append("%s: %d мест без описания" % (rel, n))
check("все глобальные читатели каталого профиля описаны явно", not unknown,
      " | ".join(unknown[:4]))
print("   сознательно глобальные: %s" % ", ".join(leaks))

print("\n13. Закладки и «прочитанное»: файл берётся из профиля запроса")
# _profile_bookmarks_file() брал идентичность из активного профиля сервера,
# поэтому GET/POST /api/bookmarks с чужим cookie правил чужой файл.
from modules import search_store
import shutil

_real_copy = shutil.copy2
shutil.copy2 = lambda *a, **k: None   # посев чужих закладок на disk гейту не нужен
try:
    bm_paths = {}
    for pid in [p for p in (nonadmin, "default", "admin") if p]:
        with profile_paths.scope(pid):
            bm_paths[pid] = search_store._profile_bookmarks_file()
    check("каждому профилю — свой файл закладок (%s)" % ",".join(sorted(bm_paths)),
          len(set(bm_paths.values())) == len(bm_paths),
          json.dumps(bm_paths, ensure_ascii=False)[:200])
    if nonadmin:
        check("путь профиля лежит в каталоге этого профиля",
              os.path.normpath(bm_paths[nonadmin]).startswith(
                  os.path.normpath(os.path.join(profile_paths._pm().PROFILES_DIR,
                                                str(nonadmin)))),
              bm_paths[nonadmin])
    check("общедоступный остаётся на прежнем общем файле",
          bm_paths.get("default") == search_store._sb._BOOKMARKS_FILE,
          str(bm_paths.get("default")))
    try:
        from legacy import bookmarks as legacy_bm
        with profile_paths.scope(nonadmin or "default"):
            same = legacy_bm._profile_bookmarks_file() == bm_paths.get(nonadmin or "default")
        check("копия в legacy/bookmarks.py ведёт себя так же", same)
    except Exception as e:
        print("   legacy/bookmarks.py не импортируется отдельно: %s" % e)
finally:
    shutil.copy2 = _real_copy
print("   посев: новому не-default профилю файл закладок копируется из общего"
      " ~/.infinity/bookmarks.json — копия данных, а не утечка доступа (решение за пользователем)")

print("\n14. Галка «показывать данные общедоступного профиля»")
# public_view читает public_data_enabled из конфига профиля запроса. Отсутствующий
# ключ = ВЫКЛ, поэтому первое свойство, которое здесь проверяется, — «выключено
# значит ровно то же, что до правки»: набор профиля остаётся одним каталогом.
# Второе — включённая галка добавляет общедоступный набор поверх своего, не
# удваивая записи с одинаковым id и не перекрывая свою версию чужой.
import time as _time
import tempfile
from modules import notes_workspace, public_view
import profiles_module

pub = profiles_module.get_default_profile_id()
# Наблюдателем берём чистый профиль: активный обычно admin, а POST
# /api/config/save активного профиля переписал бы ещё и глобальный config.json.
subject = nonadmin or next((p for p in profiles if p != pub), None)
if not subject:
    print("   нет профиля-наблюдателя кроме общедоступного — проверка пропущена")
else:
    cfg_path = profiles_module._profile_config_path(subject)
    cfg_backup = None
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as fh:
            cfg_backup = fh.read()
    tag = "gate-pubview-%s" % os.getpid()
    nid, cid, aid = tag + "-note", tag + "-chat", tag + "-ai"
    pub_notes = profile_paths.dir_for(pub, "NOTES_DIR")
    planted, planted_dirs = [], []

    def plant(category, name, data, subdir=None):
        d = profile_paths.dir_for(pub, public_view.DIR_OF[category])
        if subdir:
            d = os.path.join(d, subdir)
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, name)
        with open(fp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        planted.append(fp)
        return fp

    def set_flag(val):
        cfg = profiles_module.get_profile_config(subject) or {}
        if val is None:
            cfg.pop(public_view.FLAG_KEY, None)
        else:
            cfg[public_view.FLAG_KEY] = val
        profiles_module.save_profile_config(subject, cfg)
        # Кэш paths привязан к mtime конфига; при записи в ту же секунду на
        # Windows он может не измениться, поэтому сбрасываем явно.
        profile_paths._CFG.clear()
        profile_paths._DIR.clear()

    def note_json(title):
        return {"id": nid, "title": title, "content": "гейт", "created": 1,
                "updated": 2, "tags": []}

    try:
        plant("notes", nid + ".json", note_json("общедоступная заметка"))
        plant("chat", cid + ".json", {"id": cid, "name": "гейт", "folder": "inbox",
                                      "created": 1, "updated": 2,
                                      "messages": [{"role": "user", "content": "гейт"}]})
        plant("ai_answers", aid + ".json", {"id": aid, "question": "гейт",
                                           "answer": "гейт", "created": _time.time()})

        set_flag(None)
        check("живой HTTP-путь /api/ai/list отдаёт список", isinstance(api_ai(subject), list))
        check("ключа нет: список заметок — только свой", nid not in api_notes(subject))
        check("ключа нет: сессии чата — только свои", cid not in api_chat(subject))
        with profile_paths.scope(subject):
            check("ключа нет: read_dirs берёт один каталог",
                  public_view.read_dirs("notes") == [public_view.own_dir("notes")])
        check("ключа нет: AI-ответ общедоступного не виден", aid not in api_ai(subject))
        _real_copy2 = shutil.copy2
        shutil.copy2 = lambda *a, **k: None   # посев закладок на диск гейту не нужен
        try:
            with profile_paths.scope(subject):
                bm_off = len(search_store._visible_bookmark_files())
        finally:
            shutil.copy2 = _real_copy2

        set_flag(True)
        check("мастер-галка: общедоступная заметка в списке ровно один раз",
              api_notes(subject).count(nid) == 1)
        check("мастер-галка: общедоступная сессия чата видна",
              api_chat(subject).count(cid) == 1)
        check("мастер-галка: AI-ответ общедоступного виден", aid in api_ai(subject))
        with profile_paths.scope(subject):
            bm_on = len(search_store._visible_bookmark_files())
        check("мастер-галка: файл закладок общедоступного добавился",
              bm_on == bm_off + 1, "%d -> %d" % (bm_off, bm_on))
        check("мастер-галка: /api/notes/get чужую заметку отдаёт",
              api_json("/api/notes/get?id=" + nid, subject).get("id") == nid)
        fl = api_json("/api/profiles/public-data", subject).get("flags") or {}
        check("настройки: мастер-галка включает все категории",
              len(fl) == len(public_view.CATEGORIES) and all(fl.values()),
              json.dumps(fl, ensure_ascii=False))

        # Двойник: та же заметка лежит и в своём наборе. В списке она должна
        # остаться одна, и открыться должна своя версия — правка по id пишет
        # туда, где файл нашли, а не в общедоступный каталог.
        own_dir_ = None
        with profile_paths.scope(subject):
            own_dir_ = public_view.own_dir("notes")
        os.makedirs(own_dir_, exist_ok=True)
        own_fp = os.path.join(own_dir_, nid + ".json")
        with open(own_fp, "w", encoding="utf-8") as fh:
            json.dump(note_json("своя версия"), fh, ensure_ascii=False)
        planted.append(own_fp)
        check("одинаковый id в своём и общем не удваивается",
              api_notes(subject).count(nid) == 1)
        check("своя версия заметки важнее общедоступной",
              api_json("/api/notes/get?id=" + nid, subject).get("title") == "своя версия")
        os.remove(own_fp)
        planted.remove(own_fp)

        set_flag({"notes": True, "chat": False})
        check("словарь по категориям: заметки видны", nid in api_notes(subject))
        check("словарь по категориям: чаты — нет", cid not in api_chat(subject))
        fl = api_json("/api/profiles/public-data", subject).get("flags") or {}
        check("настройки: словарь читается поштучно",
              fl.get("notes") is True and fl.get("chat") is False,
              json.dumps(fl, ensure_ascii=False))

        set_flag({"chat": True})
        gone = nid not in api_notes(subject)
        set_flag({"notes": True})
        back = nid in api_notes(subject)
        check("снятие и возврат галки видны сразу (кэш списка знает про набор)",
              gone and back, "снята=%d возвращена=%d" % (gone, back))

        # Путь сохранения формы настроек: POST /api/config/save обязан донести
        # словарь, где все категории False, до файла профиля. strip_empty выбрасывает
        # «пусто = не задано», и молча выброшенный False означал бы «галка не
        # снимается» — баг пишущего кода, а не читающего.
        if subject != profiles_module.get_active_id():
            want = {c: (c == "chat") for c in public_view.CATEGORIES}
            r = client.post("/api/config/save", cookies={COOKIE: subject},
                            json={"config": {"public_data_enabled": want}})
            fl = api_json("/api/profiles/public-data", subject).get("flags") or {}
            check("форма настроек: сохранённый словарь читается профилем обратно",
                  r.status_code == 200 and fl == {c: (c == "chat") for c in public_view.CATEGORIES},
                  "%d %s" % (r.status_code, json.dumps(fl, ensure_ascii=False)))
        else:
            print("   профиль-наблюдатель активен: POST /api/config/save трогал бы"
                  " глобалы процесса — проверка сохранения пропущена")

        # Вложения: папка заметки живёт в общедоступном наборе. child_dir обязан
        # найти её там, иначе загрузка создала бы пустую копию в своём наборе.
        att_dir = plant("attachments", ".keep", {}, subdir=nid)
        planted_dirs.append(os.path.dirname(att_dir))
        set_flag({"attachments": True})
        with profile_paths.scope(subject):
            check("галка включена: папка вложений найдена в общедоступном наборе",
                  os.path.normpath(public_view.child_dir("attachments", nid)) ==
                  os.path.normpath(os.path.dirname(att_dir)))
        set_flag({"notes": True})
        with profile_paths.scope(subject):
            check("галка выключена: вложения общедоступного не видны",
                  os.path.normpath(public_view.child_dir("attachments", nid)) !=
                  os.path.normpath(os.path.dirname(att_dir)))

        # Холст папки: по галке показывается общедоступная доска, без неё —
        # пустой холст (первая же правка сохранится в свой набор).
        ws_fp = plant("notes", "gate-ws.json", {"folder": "gate-ws", "updated": 5,
                                                "tldraw_snapshot": None,
                                                "note_windows": {}},
                     subdir="_workspaces")
        planted_dirs.append(os.path.dirname(ws_fp))
        set_flag({"attachments": True})
        with profile_paths.scope(subject):
            st_off = notes_workspace.workspace_get_state("gate-ws")
            # Канбан и артефакты custom_mode читаются из корня, где лежит
            # заметка, — заодно проверяем, что этот корень следует за галкой.
            nb_off = notes_store._nb(nid)
        set_flag({"notes": True})
        with profile_paths.scope(subject):
            st_on = notes_workspace.workspace_get_state("gate-ws")
            nb_on = notes_store._nb(nid)
        check("галка: холст папки общедоступного профиля виден",
              st_off.get("updated") == 0 and st_on.get("updated") == 5)
        check("галка: корень заметки переключается вместе с ней",
              os.path.normpath(nb_off) != os.path.normpath(pub_notes) and
              os.path.normpath(nb_on) == os.path.normpath(pub_notes),
              "%s -> %s" % (nb_off, nb_on))

        # Закладки: файл общедоступного подмешивается к списку, а снятая
        # закладка вычищается и оттуда — иначе она бы вернулась при следующем
        # открытии. Оба файла подменены на временные: трогать настоящие
        # закладки пользователя гейт не вправе.
        tmp_bm = tempfile.mkdtemp(prefix="gate-pubview-")
        own_bm, pub_bm = (os.path.join(tmp_bm, "own.json"),
                          os.path.join(tmp_bm, "pub.json"))
        with open(own_bm, "w", encoding="utf-8") as fh:
            json.dump([{"doc_id": "gate-own", "type": "bookmark", "time": 1}], fh)
        with open(pub_bm, "w", encoding="utf-8") as fh:
            json.dump([{"doc_id": "gate-pub", "type": "bookmark", "time": 2}], fh)
        _real_pub_bm = search_store._sb._BOOKMARKS_FILE
        _real_own_bm = search_store._profile_bookmarks_file
        search_store._sb._BOOKMARKS_FILE = pub_bm
        search_store._profile_bookmarks_file = lambda: own_bm
        try:
            set_flag({"notes": True})
            with profile_paths.scope(subject):
                ids_off = {b.get("doc_id") for b in
                           search_store._get_bookmarks()["bookmarks"]}
            set_flag({"bookmarks": True})
            with profile_paths.scope(subject):
                ids_on = {b.get("doc_id") for b in
                          search_store._get_bookmarks()["bookmarks"]}
                search_store._remove_bookmark("gate-pub")
            check("галка: общий файл закладок добавлен к своему",
                  "gate-own" in ids_off and "gate-pub" not in ids_off and
                  {"gate-own", "gate-pub"} <= ids_on,
                  "%s / %s" % (sorted(ids_off), sorted(ids_on)))
            check("снятая общая закладка уходит и из общего файла",
                  all(b.get("doc_id") != "gate-pub" for b in
                      json.load(open(pub_bm, encoding="utf-8"))))
        finally:
            search_store._sb._BOOKMARKS_FILE = _real_pub_bm
            search_store._profile_bookmarks_file = _real_own_bm
            shutil.rmtree(tmp_bm, ignore_errors=True)

        with profile_paths.scope(pub):
            check("общедоступный профиль не наследует ничего",
                  public_view.read_dirs("notes") == [public_view.own_dir("notes")])
        check("вне запроса флаг не применяется", public_view.enabled("notes") is False)
    finally:
        for fp in planted:
            try:
                os.remove(fp)
            except Exception:
                pass
        for d in planted_dirs:
            try:
                os.rmdir(d)
            except Exception:
                pass
        if cfg_backup is None:
            # Конфига у профиля не было: set_flag создал файл только с нашим
            # ключом — убираем его целиком, чтобы не оставить лишнее состояние.
            try:
                if os.path.exists(cfg_path):
                    os.remove(cfg_path)
            except OSError:
                pass
        else:
            with open(cfg_path, "w", encoding="utf-8") as fh:
                fh.write(cfg_backup)
        profile_paths._CFG.clear()
        profile_paths._DIR.clear()
        check("после проверки конфиг профиля в прежнем состоянии",
              (open(cfg_path, encoding="utf-8").read() if os.path.exists(cfg_path)
               else None) == cfg_backup)
        check("после проверки тестовых данных не осталось",
              nid not in api_notes(subject) and cid not in api_chat(subject))

print()
if FAILS:
    print("ПРОВАЛЕННЫЕ ПРОВЕРКИ (%d):" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("Гейт зелёный: профили изолированы на живом HTTP-пути.")
