"""Гейт передачи медиа: «Скачать» (одиночные файлы и наборы) и «Загрузить».

Роутеры поднимаются настоящие (legacy_media_player + legacy_photos), запросы
идут с cookie конкретного профиля, данные берутся из живой базы. Проверки
разбиты по секциям; каждая печатает OK/FAIL с числом, а не «похоже работает».

Зачем отдельный гейт: у «Скачать» три ловушки, которые невозможно увидеть в
логах сервера. Зашифрованная библиотека хранит в БД исходный путь, а на диске
лежит <путь>.infenc — проверка exists() давала 404 на ровно тех файлах, которые
пользователь шифровал. Набор без фильтра по профилю = обход приватности через
прямой POST. Архив, собранный «на глаз», читается zipfile, но не распаковщиком
Windows, если перепутаны локальные заголовки.

Запуск: PYTHONIOENCODING=utf-8 E:/compilinfinity/venv/Scripts/python.exe tools/media_transfer_gate.py
"""
import io
import json
import os
import sys
import time
import zipfile

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

from fastapi import FastAPI, Request
from starlette.testclient import TestClient

import server
from routes import legacy_media_player as mp_routes
from routes import legacy_photos as photo_routes

COOKIE = "infinity_profile"

app_ = FastAPI()
app_.include_router(mp_routes.router)
app_.include_router(photo_routes.router)

# Живой стек: в main.py security_guard идёт первой строкой обработки запроса
# (§6.1 _dispatch_guarded) и режет тело по security.max_body_mb. Без него гейт
# сверял бы числа (чанк против лимита), но не саму очередь ASGI — а кнопка
# умирает именно здесь, если гвард отсекает PUT-чанк раньше маршрута.
GUARD = {"checked": 0, "blocked": []}


class _GuardStack:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and str(scope.get("path", "")).startswith("/api/media/upload"):
            # Как на живом сервере: запрос с localhost. Телефон из LAN — другой
            # путь, и он проверяется не здесь (иначе троттлинг гварда делал бы
            # прогон нестабильным).
            scope = dict(scope)
            scope["client"] = ("127.0.0.1", 54321)
            from fastapi import Request as _Req
            try:
                import security_guard as _sg
                verdict = _sg.check(_Req(scope, receive))
            except Exception:
                verdict = None
            GUARD["checked"] += 1
            if verdict is not None:
                GUARD["blocked"].append("%s %s" % (scope.get("method"), scope.get("path")))
                await verdict(scope, receive, send)
                return
        await self.app(scope, receive, send)


app_.add_middleware(_GuardStack)


@app_.get("/__gate/file")
async def _gate_file(request: Request, p: str, name: str = ""):
    """Тот же хелпер, что у маршрутов «Скачать», но для произвольного пути.

    Нужен потому, что в базе этого пользователя может не быть ни одного фильма
    (видео пустеет при смене каталогов), а починка как раз про путь: исходный
    путь в БД + контейнер <путь>.infenc на диске.
    """
    return await mp_routes._media_file_response(p, request, name or os.path.basename(p))


client = TestClient(app_)

FAILS = []


def check(label, ok, detail=""):
    print("  [%s] %s%s" % ("OK" if ok else "FAIL", label,
                           (" — " + detail) if detail else ""))
    if not ok:
        FAILS.append("%s%s" % (label, (" — " + detail) if detail else ""))


def media_module():
    import media_module
    return media_module


def rows(table):
    mm = media_module()
    conn = mm.get_db()
    try:
        cur = conn.execute("SELECT id, filepath FROM %s LIMIT 100000" % table)
        return [(r["id"], r["filepath"]) for r in cur.fetchall()]
    finally:
        conn.close()


def profile_ids():
    import profiles_module as PM
    out = [PM.get_default_profile_id()]
    for p in (PM.list_profiles() or []):
        pid = p.get("id")
        if pid and pid not in out:
            out.append(pid)
    return out


def visible_tracks(pid):
    mm = media_module()
    idx = mm.get_music_index(profile_id=pid)
    return {str(t["id"]): t for t in (idx.get("tracks") or [])}


def get(path, pid=None, **kw):
    return client.get(path, cookies={COOKIE: pid} if pid else {}, **kw)


def hdr(r):
    return r.headers.get("content-disposition") or ""


# ---------- 0. окружение ----------
print("\n[0] Окружение")
if not server.MEDIA_AVAILABLE:
    print("  FAIL: медиа-модуль недоступен — гейт нечего проверять")
    sys.exit(1)
MUSIC = rows("music_tracks")
VIDEO = rows("video_files")
PIDS = profile_ids()
check("профили найдены", bool(PIDS), "profile ids: %s" % len(PIDS))
check("строки в базе", bool(MUSIC or VIDEO),
      "music=%d video=%d" % (len(MUSIC), len(VIDEO)))

# Публичный профиль: его индекс = надстройка «общедоступное + своё»,
# поэтому любые id отсюда обязаны отдаваться и ему, и профилю с включённой галкой.
PUB = PIDS[0] if PIDS else None
PUB_TRACKS = visible_tracks(PUB) if PUB else {}
REAL_TRACKS = [(i, p) for i, p in MUSIC if os.path.isfile(p) or os.path.isfile(p + ".infenc")]
check("файлы на диске", len(REAL_TRACKS) > 0,
      "%d из %d треков читаются" % (len(REAL_TRACKS), len(MUSIC)))

# ---------- 1. одиночный трек ----------
print("\n[1] GET /api/media/music-download")
VIS = {pid: visible_tracks(pid) for pid in PIDS[:4]}
for pid in PIDS[:4]:
    vis = VIS.get(pid) or {}
    if not vis:
        continue
    # приватный трек интереснее общедоступного: он проверяет, что «видит» и
    # «отдаётся» совпадают для профиля-владельца; берём самый маленький, чтобы
    # не гонять по 6 МБ на каждый профиль ради сверки байтов
    own = [i for i in vis if str(i) not in {str(x) for x in PUB_TRACKS}]
    cands = own or list(vis)
    tid = min(cands, key=lambda i: os.path.getsize(vis[i]["filepath"])
              if os.path.isfile(vis[i]["filepath"]) else 0)
    r = get("/api/media/music-download?id=%s" % tid, pid)
    fp = vis[tid]["filepath"]
    enc = (not os.path.isfile(fp)) and os.path.isfile(fp + ".infenc")
    if enc:
        import media_crypto
        want = b"".join(media_crypto.iter_decrypted(
            fp + media_crypto.SUFFIX, 0, media_crypto.plain_size(fp + media_crypto.SUFFIX)))
    else:
        want = open(fp, "rb").read()
    ok = (r.status_code == 200 and r.content == want
          and "attachment" in hdr(r))
    check("скачан трек профиля %s (%s%s, %d КБ)" % (
        pid, "приватный" if own else "общий", ", шифр" if enc else "",
        len(want) // 1024), ok,
          "HTTP %s, байт %d/%d, CD=%r" % (r.status_code, len(r.content), len(want), hdr(r)[:60]))
    # чужой id: виден кому-то другому и не виден этому профилю — обязан быть 404,
    # иначе архивный/прямой путь обходит фильтр выдачи
    others = set()
    for other, ovis in VIS.items():
        if other != pid:
            others |= {str(i) for i in ovis}
    foreign = [i for i in sorted(others - {str(x) for x in vis})
               if os.path.isfile(dict((str(a), b) for a, b in REAL_TRACKS).get(i, ""))]
    if not foreign:
        foreign = [str(i) for i, _ in REAL_TRACKS if str(i) not in vis]
    if foreign:
        f = get("/api/media/music-download?id=%s" % foreign[0], pid)
        check("чужой трек не отдаётся (%s)" % pid, f.status_code == 404,
              "HTTP %s, байт %d" % (f.status_code, len(f.content)))
missing = get("/api/media/music-download?id=%s" % "не-существует", PUB)
check("несуществующий id = 404", missing.status_code == 404, "HTTP %s" % missing.status_code)

# ---------- 2. фильм, в т.ч. зашифрованный ----------
print("\n[2] GET /api/media/download")
vids = [(i, p) for i, p in VIDEO if os.path.isfile(p) or os.path.isfile(p + ".infenc")]
if not vids:
    print("  SKIP: в базе нет ни одного читаемого фильма — проверка на синтетике ниже")
else:
    enc_vids = [(i, p) for i, p in vids if not os.path.isfile(p)]
    picked = (enc_vids or vids)[0]
    r = client.get("/api/media/download", params={"id": picked[0]},
                   cookies={COOKIE: PUB} if PUB else {})
    kind = "зашифрован" if enc_vids else "открыт"
    check("фильм скачан (%s, %d КБ)" % (kind, len(r.content) // 1024),
          r.status_code == 200 and "attachment" in hdr(r),
          "HTTP %s CD=%r" % (r.status_code, hdr(r)[:70]))

# Синтетика: ровно та ловушка, из-за которой «Скачать» у фильма возвращал 404 —
# путь из БД указывает на отсутствующий plaintext, а байты лежат в контейнере.
import shutil
import tempfile
import media_crypto

tmp = os.path.join(tempfile.gettempdir(), "mtg_media")
shutil.rmtree(tmp, ignore_errors=True)
os.makedirs(tmp)
try:
    body = os.urandom(200 * 1024)
    plain = os.path.join(tmp, "Обычный — 01.mkv")
    with open(plain, "wb") as f:
        f.write(body)
    r = client.get("/__gate/file", params={"p": plain})
    check("plaintext отдан как загрузка", r.status_code == 200 and r.content == body
          and "attachment" in hdr(r),
          "HTTP %s, байт %d/%d" % (r.status_code, len(r.content), len(body)))

    secret = os.path.join(tmp, "Секретный — 02.mkv")
    with open(secret, "wb") as f:
        f.write(body)
    if not media_crypto.available():
        print("  SKIP: шифрование недоступно в этом рантайме — контейнер не создать")
    else:
        res = media_crypto.encrypt_file(secret)
        r2 = client.get("/__gate/file", params={"p": secret})
        check("зашифрованный фильм скачивается (не 404)", r2.status_code == 200,
              "HTTP %s, контейнер=%s (%d байт), оригинал удалён=%s"
              % (r2.status_code, os.path.isfile(res["encrypted_path"]), res["size"],
                 res["original_removed"]))
        check("приезжает расшифрованные байты, не .infenc", r2.content == body,
              "байт %d/%d, первые %r" % (len(r2.content), len(body), r2.content[:8]))
    r3 = client.get("/__gate/file", params={"p": os.path.join(tmp, "нет-такого.mkv")})
    check("отсутствующий файл = 404", r3.status_code == 404, "HTTP %s" % r3.status_code)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------- 3. набор одним ZIP ----------
print("\n[3] POST /api/media/download-zip")
from modules import media_transfer as MT

pid = PUB or (PIDS[0] if PIDS else None)
vis = visible_tracks(pid)
ids = [str(i) for i in list(vis)[:3]]
if len(ids) < 2:
    check("хотя бы два трека у профиля", False, "id: %s" % ids)
else:
    r = client.post("/api/media/download-zip", json={"kind": "music", "ids": ids,
                                                     "name": "Альбом — 2026"},
                    cookies={COOKIE: pid} if pid else {})
    ok_http = r.status_code == 200 and "attachment" in hdr(r) \
        and r.headers.get("content-type", "").startswith("application/zip")
    check("JSON-запрос отдан архивом", ok_http,
          "HTTP %s CD=%r ct=%r" % (r.status_code, hdr(r)[:60],
                                   r.headers.get("content-type")))
    if ok_http:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        bad = zf.testzip()
        names = zf.namelist()
        check("архив читается, CRC целы", bad is None, "testzip -> %r" % bad)
        check("записей ровно столько, сколько id", len(names) == len(ids),
              "%s vs %s" % (names, ids))
        content_ok = True
        for n, i in zip(names, ids):
            fp = vis[i]["filepath"]
            if os.path.isfile(fp):
                want = open(fp, "rb").read()
            else:
                import media_crypto
                enc = fp + media_crypto.SUFFIX
                want = b"".join(media_crypto.iter_decrypted(
                    enc, 0, media_crypto.plain_size(enc)))
            if zf.read(n) != want:
                content_ok = False
                break
        check("содержимое = оригиналы (расшифровано)", content_ok,
              "размеры: %s" % [len(zf.read(n)) for n in names])
        # форма: браузерный download умеет только urlencoded
        r2 = client.post("/api/media/download-zip",
                         data={"kind": "music", "ids": ",".join(ids)},
                         headers={"content-type": "application/x-www-form-urlencoded"},
                         cookies={COOKIE: pid} if pid else {})
        same = r2.status_code == 200 and len(r2.content) == len(r.content)
        check("та же выдача через форму", same,
              "HTTP %s, байт %d/%d" % (r2.status_code, len(r2.content), len(r.content)))
        dup = client.post("/api/media/download-zip",
                          json={"kind": "music", "ids": ids + ids[:1]},
                          cookies={COOKIE: pid} if pid else {})
        if dup.status_code == 200:
            dn = zipfile.ZipFile(io.BytesIO(dup.content)).namelist()
            check("дубль имени не теряет файл", len(dn) == len(ids) + 1, str(dn))
        else:
            check("дубль имени не теряет файл", False, "HTTP %s" % dup.status_code)
    over = client.post("/api/media/download-zip",
                       json={"kind": "music", "ids": [str(i) for i in range(MT.MAX_SET_ITEMS + 1)]},
                       cookies={COOKIE: pid} if pid else {})
    check("набор больше лимита отклонён", over.status_code == 413,
          "HTTP %s (лимит %d)" % (over.status_code, MT.MAX_SET_ITEMS))
    foreign = [str(i) for i, _ in REAL_TRACKS if str(i) not in vis]
    if foreign:
        f = client.post("/api/media/download-zip", json={"kind": "music", "ids": foreign[:2]},
                        cookies={COOKIE: pid} if pid else {})
        check("чужими id архив не собирается", f.status_code == 404,
              "HTTP %s, байт %d" % (f.status_code, len(f.content)))

# ---------- 4. фото ----------
print("\n[4] GET /api/photos/download/{id}")
if not server.PHOTO_AVAILABLE:
    print("  SKIP: фото-модуль недоступен")
else:
    import photo_module
    conn = photo_module.get_photo_db()
    try:
        prows = [(r["id"], r["filepath"]) for r in
                 conn.execute("SELECT id, filepath FROM photos LIMIT 2000")]
    finally:
        conn.close()
    readable = [(i, p) for i, p in prows if os.path.isfile(p) or os.path.isfile(p + ".infenc")]
    if not readable:
        # каталог фото может быть недоступен (съёмный диск, чужой профиль) —
        # тогда проверяем, что «Скачать» отвечает 403/404, а не падает на 500.
        # 403 здесь означает «каталог принадлежит другому профилю», 404 — «файла
        # нет на диске»; оба ответа правильные, байты не уходят ни в одном.
        codes = sorted({get("/api/photos/download/%s" % i, PUB).status_code
                        for i, _ in prows[:5]})
        leaked = [i for i, _ in prows[:5]
                  if get("/api/photos/download/%s" % i, PUB).status_code == 200]
        check("скрытое или отсутствующее фото не отдаётся", not leaked,
              "коды %s, отдали байты: %s" % (codes, leaked))
        print("  SKIP: %d строк, ни одного читаемого файла — сверку байтов негде мерить"
              % len(prows))
    else:
        fid, fp = readable[0]
        r = get("/api/photos/download/%s" % fid, PUB)
        if os.path.isfile(fp):
            want = open(fp, "rb").read()
        else:
            import media_crypto
            enc = fp + media_crypto.SUFFIX
            want = b"".join(media_crypto.iter_decrypted(
                enc, 0, media_crypto.plain_size(enc)))
        check("оригинал фото скачан (%d КБ)" % (len(want) // 1024),
              r.status_code == 200 and r.content == want and "attachment" in hdr(r),
              "HTTP %s, байт %d/%d" % (r.status_code, len(r.content), len(want)))
        nf = get("/api/photos/download/%s" % "нет-такого", PUB)
        check("несуществующее фото = 404", nf.status_code == 404, "HTTP %s" % nf.status_code)

# ---------- 5. «Загрузить» ----------
print("\n[5] /api/media/upload (begin -> chunk -> finish)")
import media_module as MM

def set_upload_flag(value):
    """Тот же ключ, что читает media_transfer.upload_enabled, и тот же способ записи."""
    conn = MM.get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) "
                     "VALUES ('media_upload_enabled', ?, CURRENT_TIMESTAMP)", (value,))
        conn.commit()
    finally:
        conn.close()


def upload_http(method, path, pid, **kw):
    return client.request(method, path, cookies={COOKIE: pid} if pid else {}, **kw)


def send_file(pid, blob, category="music", name="Трек — 01.mp3", relpath="", chunk=None):
    """begin + чанки + finish. Возвращает (ответ_finish, ответ_begin, ответы_чанков)."""
    b = upload_http("POST", "/api/media/upload/begin", pid,
                    json={"category": category, "filename": name,
                          "relpath": relpath, "size": len(blob)})
    if b.status_code != 200:
        return b, b, []
    sid = b.json()["session_id"]
    size = chunk or int(b.json().get("chunk") or MT.UPLOAD_CHUNK)
    parts = []
    for i in range(0, len(blob), size):
        parts.append(upload_http("PUT", "/api/media/upload/%s" % sid, pid,
                                 content=blob[i:i + size],
                                 headers={"content-type": "application/octet-stream"}))
        if parts[-1].status_code != 200:
            break
    fin = upload_http("POST", "/api/media/upload/%s/finish" % sid, pid)
    return fin, b, parts


try:
    prev = None
    conn = MM.get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='media_upload_enabled'").fetchone()
        prev = row["value"] if row else None
    finally:
        conn.close()

    # a) рубильник выключен: ни одна ручка не принимает, включая прямой PUT
    set_upload_flag("0")
    codes = [upload_http("POST", "/api/media/upload/begin", PUB,
                         json={"category": "music", "filename": "а.mp3", "size": 3}).status_code,
             upload_http("PUT", "/api/media/upload/нет-сессии", PUB, content=b"abc").status_code,
             upload_http("POST", "/api/media/upload/нет-сессии/finish", PUB).status_code,
             upload_http("POST", "/api/media/upload/нет-сессии/abort", PUB).status_code]
    check("выключенный рубильник = 403 на всех четырёх ручках",
          set(codes) == {403}, "коды %s" % codes)

    set_upload_flag("1")
    check("включённый рубильник читается модулем", MT.upload_enabled(),
          "upload_enabled() = %r" % MT.upload_enabled())

    # b) отказы по формату до создания сессии
    bad = upload_http("POST", "/api/media/upload/begin", PUB,
                      json={"category": "music", "filename": "вредонос.exe", "size": 10})
    check("чужой формат отклонён", bad.status_code == 415,
          "HTTP %s, %r" % (bad.status_code, bad.text[:60]))
    bcat = upload_http("POST", "/api/media/upload/begin", PUB,
                       json={"category": "не-категория", "filename": "а.mp3", "size": 10})
    check("неизвестная категория отклонена", bcat.status_code == 400,
          "HTTP %s, %r" % (bcat.status_code, bcat.text[:60]))
    big = upload_http("POST", "/api/media/upload/begin", PUB,
                      json={"category": "music", "filename": "а.mp3",
                            "size": MT.MAX_FILE_BYTES + 1})
    check("размер больше лимита отклонён", big.status_code == 413, "HTTP %s" % big.status_code)

    # j) чанк, который обещают клиенту, обязан проходить через лимит тела
    #    гварда: иначе на каждом PUT в живом сервере стоит 413 и кнопка мертва.
    import security_guard as SG

    def begin_only(**kw):
        """Открыть и тут же закрыть сессию: песочница не должна расти от гейта."""
        r = upload_http("POST", "/api/media/upload/begin", PUB,
                        json=dict({"category": "music", "filename": "а.mp3", "size": 10}, **kw))
        d = r.json() if r.status_code == 200 else {}
        if d.get("session_id"):
            upload_http("POST", "/api/media/upload/%s/abort" % d["session_id"], PUB)
        return d

    adv = begin_only().get("chunk") or 0
    sg_limit = SG.body_limit_bytes()
    check("обещанный чанк не больше лимита тела гварда",
          0 < adv <= MT.UPLOAD_CHUNK and adv <= sg_limit,
          "чанк %d, UPLOAD_CHUNK %d, лимит гварда %d" % (adv, MT.UPLOAD_CHUNK, sg_limit))
    real_limit = SG.body_limit_bytes
    try:
        SG.body_limit_bytes = lambda: 4 * 1024 * 1024
        small = begin_only().get("chunk") or 0
        check("при низком лимите тела чанк урезается", 0 < small < 4 * 1024 * 1024,
              "чанк %d при лимите 4 МБ" % small)
    finally:
        SG.body_limit_bytes = real_limit

    # Песочница вместо библиотеки: гейт не имеет права класть мусор в реальные
    # каталоги пользователя, поэтому подменяем точку назначения и отключаем
    # переиндексацию — иначе подставные байты попали бы в живую базу.
    sandbox = os.path.join(tempfile.gettempdir(), "mtg_upload")
    shutil.rmtree(sandbox, ignore_errors=True)
    os.makedirs(sandbox)
    sandbox_real = os.path.realpath(sandbox)
    real_resolve = MT.resolve_target
    real_rescan = mp_routes._rescan_after_upload
    rescans = []
    MT.resolve_target = lambda category, profile_id: sandbox
    mp_routes._rescan_after_upload = lambda category, target: rescans.append((category, target))

    try:
        blob = os.urandom(300 * 1024)
        chunk = 100 * 1024
        expect_parts = -(-len(blob) // chunk)
        fin, begin, parts = send_file(PUB, blob, chunk=chunk)
        placed = (fin.json().get("files") or []) if fin.status_code == 200 else []
        check("файл долетел и записан (%d чанка)" % len(parts),
              fin.status_code == 200 and placed == ["Трек — 01.mp3"]
              and len(parts) == expect_parts,
              "HTTP %s, чанков %d/%d, файлы %s, %r"
              % (fin.status_code, len(parts), expect_parts, placed, fin.text[:60]))
        dest = os.path.join(sandbox, "Трек — 01.mp3")
        check("чанки склеились в исходные байты",
              os.path.isfile(dest) and open(dest, "rb").read() == blob,
              "байт %d/%d" % (os.path.getsize(dest) if os.path.isfile(dest) else -1, len(blob)))
        check("переиндексация запрошена для целевого каталога",
              rescans[-1:] == [("music", sandbox)], "вызовы %s" % rescans)

        fin2, _, _ = send_file(PUB, blob)
        n2 = (fin2.json().get("files") or [""])[0]
        check("существующее не перезаписано", n2 == "Трек — 01 (2).mp3",
              "легло как %r" % n2)

        # c) недогруз: обещали N, прислали N-100
        sid = upload_http("POST", "/api/media/upload/begin", PUB,
                          json={"category": "music", "filename": "недолёт.mp3",
                                "size": len(blob)}).json()["session_id"]
        upload_http("PUT", "/api/media/upload/%s" % sid, PUB, content=blob[:-100],
                    headers={"content-type": "application/octet-stream"})
        half = upload_http("POST", "/api/media/upload/%s/finish" % sid, PUB)
        check("неполный файл не попадает в библиотеку", half.status_code == 400,
              "HTTP %s, %r" % (half.status_code, half.text[:70]))
        check("после отказа сессия закрыта",
              upload_http("POST", "/api/media/upload/%s/finish" % sid, PUB).status_code == 410,
              "")

        # d) перерасход: больше обещанного — отсекается на чанке
        sid2 = upload_http("POST", "/api/media/upload/begin", PUB,
                           json={"category": "music", "filename": "перерасход.mp3",
                                 "size": 10}).json()["session_id"]
        over = upload_http("PUT", "/api/media/upload/%s" % sid2, PUB, content=blob[:200],
                           headers={"content-type": "application/octet-stream"})
        check("чанк больше обещанного размера отклонён", over.status_code == 413,
              "HTTP %s, %r" % (over.status_code, over.text[:60]))

        # e) чанк больше UPLOAD_CHUNK не проходит (тот лимит и причина чанкования)
        sid3 = upload_http("POST", "/api/media/upload/begin", PUB,
                           json={"category": "music", "filename": "гигант.mp3",
                                 "size": MT.UPLOAD_CHUNK + 1}).json()["session_id"]
        huge = upload_http("PUT", "/api/media/upload/%s" % sid3, PUB,
                           content=b"\0" * (MT.UPLOAD_CHUNK + 1),
                           headers={"content-type": "application/octet-stream"})
        check("чанк больше UPLOAD_CHUNK отклонён", huge.status_code == 413,
              "HTTP %s, %r" % (huge.status_code, huge.text[:60]))

        # f) abort освобождает песочницу
        sid4 = upload_http("POST", "/api/media/upload/begin", PUB,
                           json={"category": "music", "filename": "отмена.mp3",
                                 "size": 10}).json()["session_id"]
        ab = upload_http("POST", "/api/media/upload/%s/abort" % sid4, PUB)
        after = upload_http("POST", "/api/media/upload/%s/finish" % sid4, PUB)
        check("abort закрывает сессию", ab.status_code == 200 and after.status_code == 410,
              "abort=%s finish=%s" % (ab.status_code, after.status_code))

        # g) ZIP: вложенность сохраняется, «..» и чужие форматы — нет
        import zipfile as Z
        zbuf = io.BytesIO()
        with Z.ZipFile(zbuf, "w") as zf:
            zf.writestr("Концерт/Первый.mp3", blob)
            zf.writestr("Концерт/Второй.flac", blob[:1000])
            zf.writestr("readme.txt", "не медиа")
            zf.writestr("../../Выход.mp3", blob)
            zf.writestr("C:/absolute.mp3", blob)
        zbytes = zbuf.getvalue()
        zfin, _, _ = send_file(PUB, zbytes, name="архив.zip", category="music")
        names = (zfin.json().get("files") or []) if zfin.status_code == 200 else []
        check("ZIP распакован в подкаталог", "Концерт/Первый.mp3" in names,
              "HTTP %s, файлы %s, %r" % (zfin.status_code, names, zfin.text[:80]))
        check("не-медиа из архива не легло", not any(n.endswith(".txt") for n in names),
              str(names))
        escaped = [n for n in names if ".." in n or n.startswith("/") or ":" in n]
        outside = [n for n in names
                   if not os.path.realpath(os.path.join(sandbox_real, *n.split("/")))
                       .startswith(sandbox_real + os.sep)]
        check("имена из архива не вышли за пределы каталога",
              names and not escaped and not outside,
              "имена %s, вне песочницы %s" % (escaped, outside))

        # h) папка: relpath из webkitRelativePath должен дать подкаталог
        pfin, pbegin, _ = send_file(PUB, blob, name="Live.mp3", relpath="Альбом 2026/Live.mp3")
        pnames = (pfin.json().get("files") or []) if pfin.status_code == 200 else []
        check("файл из папки лёг в подкаталог", pnames == ["Альбом 2026/Live.mp3"],
              "HTTP %s, файлы %s, path=%r" % (pfin.status_code, pnames,
                                              pbegin.json().get("path") if pbegin.status_code == 200 else None))
        check("имя внутри песочницы читается",
              os.path.isfile(os.path.join(sandbox, "Альбом 2026", "Live.mp3")), "")
    finally:
        MT.resolve_target = real_resolve
        mp_routes._rescan_after_upload = real_rescan
        shutil.rmtree(sandbox, ignore_errors=True)

    # i) адресат реальной загрузки — свой каталог профиля, не общедоступный
    import media_visibility as MV
    own = MV.own_dirs("music", PUB)
    if not own:
        print("  SKIP: у профиля нет своих каталогов — проверять нечего "
              "(создание каталога трогать профиль не будем)")
    else:
        got = MT.resolve_target("music", PUB)
        check("разметка пути: грузим в свой каталог",
              MV.norm(got) == MV.norm(own[0]), "%r vs %r" % (got, own[0]))
        pub = MV.visible_dirs("music", PUB)
        check("свой каталог отличается от общедоступной базы",
              MV.norm(got) != MV.norm(pub[0]) or pub[0] == own[0],
              "видимых %d, первый %r" % (len(pub), pub[0]))
finally:
    set_upload_flag(prev if prev is not None else "0")

# l) слияние переиндексаций: папка из 40 файлов = один проход, а не сорок
real_run = mp_routes._run_media_rescan
real_delay = mp_routes._RESCAN_DELAY
runs = []
mp_routes._run_media_rescan = lambda category, target: runs.append((category, target))
mp_routes._RESCAN_DELAY = 0.2
try:
    for _ in range(40):
        mp_routes._rescan_after_upload("music", sandbox)
    mp_routes._rescan_after_upload("video", sandbox)
    time.sleep(1.2)
    check("40 финишей в один каталог сливаются в один проход",
          sorted(runs) == sorted([("music", sandbox), ("video", sandbox)]),
          "проходов %d: %s" % (len(runs), runs))
    mp_routes._rescan_after_upload("", "")
    mp_routes._rescan_after_upload("music", "")
    time.sleep(0.4)
    check("пустой адресат не порождает проход", len(runs) == 2, "проходов %d" % len(runs))
finally:
    mp_routes._RESCAN_DELAY = real_delay
    mp_routes._run_media_rescan = real_run

put_blocked = [b for b in GUARD["blocked"] if b.startswith("PUT")]
check("загрузка идёт через security_guard, как в живом стеке",
      GUARD["checked"] >= 10, "гвард вызван %d раз для /api/media/upload/*" % GUARD["checked"])
check("ни один PUT-чанк не отсечён гвардом", not put_blocked, "; ".join(put_blocked[:5]))

print("\nИтого провалов: %d" % len(FAILS))
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
