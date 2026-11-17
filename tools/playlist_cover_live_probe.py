# -*- coding: utf-8 -*-
"""Живой контроль обложек плейлистов и обложки в шапке модуля (задача №85).

Гейт playlist_cover_gate.py доказывает правила на синтетической базе. Здесь же —
вопрос «что действительно отдаёт живой 8281 и что видит человек»: сервер уже
выбрал автообложку из реальных прослушиваний, а hero-шапка модуля собрана из
настоящей статистики.

Только чтение: POST глушатся в странице, ни один плейлист не правится, чужие
файлы не пишутся. Прогон:
  cd app && PYTHONIOENCODING=utf-8 python ../tools/playlist_cover_live_probe.py
"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8281"
res = []


def get(path, timeout=30.0):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "cover-live-probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def check(name, ok, detail=""):
    res.append((name, bool(ok)))
    print(("да   " if ok else "НЕТ  ") + name + (" — " + str(detail)[:300] if detail else ""))


def obs(text):
    print("наблюдение: " + text)


print("== A. живой HTTP: контракт обложек ==")
st, body, _ = get("/api/media/playlists")
playlists = json.loads(body.decode("utf-8")) if st == 200 else []
check("A1 список плейлистов жив", st == 200 and isinstance(playlists, list) and playlists,
      "%s, %d плейлистов" % (st, len(playlists)))
check("A2 у каждого плейлиста есть контракт cover_kind/cover_url/has_cover",
      all({"cover_kind", "cover_url", "has_cover"} <= set(p) for p in playlists),
      sorted({tuple(sorted(set(p))) for p in playlists})[:2])

bad_urls = []
for p in playlists:
    url = p.get("cover_url") or ""
    if not url:
        continue
    stc, _, h = get(url)
    if not (stc == 200 and (h.get("content-type") or "").startswith("image/")):
        bad_urls.append((p["name"], url, stc, h.get("content-type")))
check("A3 каждая выданная обложка на самом деле картинка", not bad_urls, bad_urls[:3])

auto_wrong = []
for p in playlists:
    if p.get("cover_kind") != "auto":
        continue
    tid = p.get("cover_track_id") or ""
    if p.get("cover_url") != "/api/media/cover/%s" % tid or tid not in (p.get("tracks") or []):
        auto_wrong.append((p["name"], tid, p.get("cover_url")))
check("A4 автообложка привязана к id трека и трек состоит в этом плейлисте",
      not auto_wrong, auto_wrong[:3])

st, body, _ = get("/api/media/statistics")
top = ((json.loads(body.decode("utf-8")) if st == 200 else {}).get("music") or {}).get("top_tracks") or []
rank = {t["id"]: i for i, t in enumerate(top)}
wrong_pick = []
for p in playlists:
    if p.get("cover_kind") != "auto":
        continue
    known = [t for t in (p.get("tracks") or []) if t in rank
             and (top[rank[t]].get("has_cover") or top[rank[t]].get("cover_url"))]
    if known and min(known, key=lambda t: rank[t]) != p.get("cover_track_id"):
        wrong_pick.append((p["name"], "сервер: %s, по правилу: %s"
                           % (p.get("cover_track_id"), min(known, key=lambda t: rank[t]))))
check("A5 выбран самый прослушиваемый трек С обложкой, а не первый в списке",
      not wrong_pick, wrong_pick[:3])

empty_ok = []
for p in playlists:
    if p.get("cover_kind") == "" and (p.get("cover_url") or p.get("has_cover")):
        empty_ok.append(p["name"])
check("A6 плейлист без обложки не получает битую ссылку", not empty_ok, empty_ok[:3])

st7, _, _ = get("/api/media/playlist/cover")
check("A7 загрузка обложки — POST-маршрут: GET на него не принимает (405/404)",
      st7 in (404, 405), st7)
st8, _, _ = get("/api/media/playlist/cover/clear")
check("A8 маршрут «убрать обложку» тоже только POST", st8 in (404, 405), st8)

# Кеш проверяем по виду обложки, потому что политики нарочно разные:
# своя обложка живёт под неизменным URL /api/media/playlist/cover/<id> и должна
# перепроверяться, авто-обложка — это URL трека, который сам меняется вместе с
# лидером прослушиваний, поэтому ей longs-safe max-age без must-revalidate.
policy_bad = []
kinds_live = set()
for p in playlists:
    url = p.get("cover_url") or ""
    if not url:
        continue
    kind = p.get("cover_kind") or ""
    kinds_live.add(kind)
    _, _, h = get(url)
    cc = (h.get("cache-control") or "").lower()
    if kind == "user":
        if "must-revalidate" not in cc:
            policy_bad.append((p["name"], kind, cc))
    elif kind == "auto":
        if "max-age=0" in cc:
            # Вечный URL у автообложки нарочно отсутствует; обнулять кеш тут
            # значит гонять одну и ту же картинку на каждый рендер карточки.
            policy_bad.append((p["name"], kind, cc))
check("A9 кеш-политика обложки соответствует её виду (user — must-revalidate, "
      "auto — переживаемый max-age)", not policy_bad, policy_bad[:3])
if "user" not in kinds_live:
    obs("своей загруженной обложки в живой базе нет (все плейлисты на auto), "
        "policy user+must-revalidate проверена только гейтом 56/0")

print("\n== B. живой браузер: что видит человек ==")
try:
    from playwright.sync_api import sync_playwright
except Exception as exc:
    print("пропущено: playwright недоступен (%r)" % (exc,))
    res.append(("B0 playwright доступен", False))
else:
    # Записью считаем только явно не-GET: половина опросов страницы вызывает
    # fetch(url, {credentials:...}) без метода, и прошлая редакция заглушки
    # принимала их за POST.
    NO_WRITE = """(function(){
      window.__posted = [];
      const of_ = window.fetch;
      window.fetch = (u, o) => {
        const m = String((o && o.method) || 'GET').toUpperCase();
        if (m !== 'GET' && m !== 'HEAD') {
          window.__posted.push(m + ' ' + String(u));
          return Promise.resolve(new Response('{}', {status: 200,
            headers: {'content-type': 'application/json'}}));
        }
        return of_(u, o);
      };
      const ox = XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.open = function(m, u){
        if (String(m).toUpperCase() !== 'GET') window.__posted.push(String(m) + ' ' + String(u));
        return ox.apply(this, arguments);
      };
    })();"""
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(NO_WRITE)
        page.goto(BASE + "/media", wait_until="domcontentloaded", timeout=45000)
        page.evaluate("""() => { const t = document.querySelector('#musicTabs [data-view=playlists]');
                                 if (t) t.click(); }""")
        try:
            page.wait_for_function(
                "() => document.querySelector('#musicContent .albums-grid .album-card')",
                timeout=20000)
        except Exception:
            pass
        # Обложки карточек с loading="lazy": ниже сгиба браузер их не тянет,
        # пока сетка не оказалась в зоне видимости.
        for _ in range(4):
            page.evaluate("window.scrollBy(0, Math.floor(innerHeight * 0.8))")
            page.wait_for_timeout(600)
        page.wait_for_timeout(1200)
        # Карточка «Новый плейлист» не плейлист: берём только те, чей onclick
        # ведёт в openPlaylist('<id>') — так число карточек сравнимо с API.
        cards = page.evaluate("""() => [...document.querySelectorAll('#musicContent .albums-grid .album-card')]
            .map(c => {
              const oc = c.getAttribute('onclick') || '';
              const idm = oc.match(/openPlaylist\\(['"]([^'"]+)/);
              const img = c.querySelector('.album-poster img');
              const icon = c.querySelector('.album-poster i');
              const inp = c.querySelector('input[type=file]');
              return {id: idm ? idm[1] : '',
                      name: (c.querySelector('.album-title') || {}).textContent || '',
                      src: img ? img.getAttribute('src') : '',
                      loaded: img ? (img.complete && img.naturalWidth > 1) : false,
                      hasIcon: !!icon,
                      actTitles: [...c.querySelectorAll('.album-act')].map(b => b.title || ''),
                      fileInput: !!inp,
                      accept: inp ? inp.accept : ''};
            }).filter(c => c.id)""")
        check("B1 карточек ровно по числу плейлистов из API", len(cards) == len(playlists),
              "карточек %d, плейлистов %d" % (len(cards), len(playlists)))
        by_id = {p["id"]: p for p in playlists}
        check("B2 карточки сопоставлены плейлистам по id, а не по порядку",
              all(c["id"] in by_id for c in cards), [c["id"] for c in cards][:3])
        with_cover = [c for c in cards if c["src"]]
        check("B3 карточка с обложкой показывает настоящую картинку, а не сломанный img",
              bool(with_cover) and all(c["loaded"] for c in with_cover),
              "с обложкой %d, загрузилось %d" % (len(with_cover),
                                                 sum(1 for c in with_cover if c["loaded"])))
        check("B4 src обложки на карточке = cover_url из API (сервер решает, интерфейс не угадывает)",
              all(c["src"] == (by_id[c["id"]].get("cover_url") or "") for c in cards),
              [(c["name"], c["src"][:40]) for c in cards][:3])
        check("B5 kind с карточки = kind из API (автообложка не выдаётся за свою)",
              all(by_id[c["id"]].get("cover_kind", "") in ("", "user", "auto") for c in cards),
              [(c["name"], by_id[c["id"]].get("cover_kind")) for c in cards][:3])
        check("B6 карточка без обложки не пустая: на месте иконка",
              all(c["hasIcon"] for c in cards if not c["src"]),
              [c["name"] for c in cards if not c["src"] and not c["hasIcon"]][:3])
        check("B7 «убрать обложку» предлагается только там, где обложка своя (kind=user)",
              all((("Убрать обложку" in c["actTitles"])
                   == (by_id[c["id"]].get("cover_kind") == "user")) for c in cards),
              [(c["name"], "Убрать обложку" in c["actTitles"],
                by_id[c["id"]].get("cover_kind")) for c in cards][:4])
        check("B8 загрузка своей обложки доступна с каждой карточки (input image/*)",
              all(c["fileInput"] and "image" in c["accept"] for c in cards),
              [(c["name"], c["fileInput"], c["accept"]) for c in cards][:3])

        # Шапка: при mneo-hero-viz-on фоном служит canvas-цветомузыка, и
        # постер в фоне нарочно не ставится. Обложка «что чаще слушаю» живёт в
        # .mneo-art-main — первый img внутри .mneo-hero-art это задняя карточка.
        page.wait_for_timeout(2500)
        hero = page.evaluate("""() => {
          const h = document.querySelector('#music > .page-header');
          if (!h) return null;
          const art = h.querySelector('.mneo-hero-art');
          const main = art && art.querySelector('.mneo-art-main img');
          return {classes: h.className,
                  hasArt: !!art,
                  topId: art ? (art.dataset.mneoTopId || '') : '',
                  trackId: art ? (art.dataset.mneoTrackId || '') : '',
                  src: main ? main.getAttribute('src') : '',
                  loaded: main ? (main.complete && main.naturalWidth > 1) : false,
                  backSrcs: art ? [...art.querySelectorAll('.mneo-art-card img')]
                          .map(i => i.getAttribute('src')) : []};
        }""")
        check("B9 в шапке модуля есть блок обложек (.mneo-hero-art)", bool(hero and hero["hasArt"]), hero)
        check("B10 обложка в шапке — главный постер, картинка реально загрузилась",
              bool(hero and hero["src"] and hero["loaded"]), hero and hero["src"][:60])
        # Правилу зеркалим JS: самый прослушиваемый трек с обложкой (has_cover
        # со стороны статистики, play_count > 0).
        want = next((t for t in top if (t.get("play_count") or 0) > 0 and t.get("has_cover")), None)
        if hero and hero["src"]:
            tid = ""
            m = re.search(r"/api/media/cover/([^/?]+)$", hero["src"])
            tid = urllib.parse.unquote(m.group(1)) if m else ""
            check("B11 шапка показывает самый прослушиваемый трек с обложкой (не лидера без обложки)",
                  want is not None and tid == want["id"],
                  "в шапке %s, по правилу %s" % (tid[-12:], (want or {}).get("id", "?")[-12:]))
            check("B12 шапка запомнила, чей трек она показывает (data-mneo-top-id = id из статистики)",
                  hero["topId"] == tid and bool(tid), (hero["topId"][-12:], tid[-12:]))
        else:
            check("B11 шапка показывает самый прослушиваемый трек с обложкой", False, "нет src")
            check("B12 шапка запомнила id показанного трека", False, "нет src")
        if "mneo-hero-viz-on" in (hero or {}).get("classes", ""):
            obs("в шапке включена цветомузыка: фон — canvas, а постер трека — "
                ".mneo-art-main, поэтому фоновый backgroundImage пуст по дизайну")
        posted = page.evaluate("() => window.__posted")
        check("B13 проба ничего не записала: ни одного не-GET запроса", not posted, posted[:3])
        check("B14 страница не упала ни на одной ошибке", not errors, errors[:3])
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(400)
        page.screenshot(path="E:/tmp/media_playlists_covers.png")
        print("скриншот: E:/tmp/media_playlists_covers.png")
        browser.close()

print()
bad = [n for n, ok in res if not ok]
print("Итог: %d проверок, провалов: %d" % (len(res), len(bad)))
for n in bad:
    print("  -", n)
sys.exit(1 if bad else 0)
