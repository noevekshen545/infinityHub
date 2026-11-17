# -*- coding: utf-8 -*-
"""Живой контроль рекомендаций в книгах на 8281.

Разделяет два фронта. Дизайн (шаблон, CSS, JS) обслуживается с диска на каждый
запрос — он обязан быть правильным прямо сейчас. Логика движка живёт в Python и
меняется только после рестарта, поэтому её проверки в режиме «до рестарта»
печатается наблюдением, а с --after-restart становятся требованием:

    python ../tools/books_reco_live_probe.py            # что уже живое
    python ../tools/books_reco_live_probe.py --after-restart
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from books_reco_gate import js_function  # тот же разбор шаблона, что в гейте

BASE = "http://127.0.0.1:8281"
AFTER = "--after-restart" in sys.argv
CONCRETE = "/api/stats/recommendations/concrete?module=books&limit=15"
res = []


def get(path, timeout=30.0):
    t0 = time.perf_counter()
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "books-reco-probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read(), (time.perf_counter() - t0) * 1000


def section(title):
    print("\n== %s ==" % title)


def check(name, ok, detail=""):
    res.append((name, bool(ok)))
    print(("да   " if ok else "НЕТ  ") + name + (" — " + str(detail)[:300] if detail else ""))


def note(name, ok, detail="", pending=""):
    """Половина движка: до рестарта это констатация, после — проверка."""
    if AFTER:
        check(name, ok, detail)
    else:
        print(("уже  " if ok else "ждёт " + pending + "  ") + name
              + (" — " + str(detail)[:300] if detail else ""))


def zero_hours(texts):
    """Часы равны нулю только если так написано: «3,0 ч» — это не ноль."""
    bad = []
    for t in texts:
        for m in re.findall(r"(\d+(?:[,.]\d+)?)\s*ч", t):
            if float(m.replace(",", ".")) == 0:
                bad.append(t)
                break
    return bad


# ============================================================ A. HTTP: движок
section("A. живой HTTP (только GET, ничего не пишется)")
st, body, ms1 = get("/books")
html = body.decode("utf-8", "replace")
check("A1 страница /books отвечает", st == 200, "%s за %d мс, %d КБ" % (st, ms1, len(body) // 1024))

st, body, ms = get(CONCRETE)
check("A2 API подборки жив", st == 200, "%s за %d мс" % (st, ms))
data = json.loads(body.decode("utf-8", "replace")) if st == 200 else {}
items = data.get("items") or []
reasons = [it.get("reason", "") for it in items]
ids = [it.get("id", "") for it in items]
banner = data.get("based_on") or ""

check("A3 карточки приходят (подборка не пустая)", len(items) > 0, "items=%d" % len(items))
check("A4 id карточек вида collection://<число> — их принимает /api/books/cover",
      all(re.fullmatch(r"collection://\d+", i) for i in ids), ids[:3])
note("A5 в причинах нет нулевых часов («0,0 ч» / «0 ч»)", not zero_hours(reasons),
     zero_hours(reasons) or reasons[:2], "рестарта")
note("A6 часы по-русски запятой, а не точкой",
     all("." not in m for m in re.findall(r"\d+[.,]\d+\s*ч", " ".join(reasons))),
     re.findall(r"\d+[.,]\d+\s*ч", " ".join(reasons))[:4], "рестарта")
code_genre = [r for r in reasons if re.search(r"«[A-Za-z][A-Za-z_ ]*»", r)]
note("A7 жанр в причине назван по-русски, а не кодом базы («sf», «sf_fantasy»)",
     not code_genre, code_genre[:2], "рестарта")

st2, body2, ms2 = get(CONCRETE)
ids2 = [it.get("id") for it in json.loads(body2.decode("utf-8", "replace")).get("items", [])]
overlap = len(set(ids) & set(ids2))
note("A8 две подрядки дают один и тот же набор (не перемешивается на каждый запрос)",
     ids == ids2, "пересечение %d из %d" % (overlap, len(ids)), "рестарта")

st3, body3, _ = get(CONCRETE + "&salt=PROBE-SALT-1")
salted = [it.get("id") for it in json.loads(body3.decode("utf-8", "replace")).get("items", [])] \
    if st3 == 200 else []
check("A9 запрос с salt принимается (не 4xx)", st3 == 200, st3)
if AFTER:
    st4, body4, _ = get(CONCRETE + "&salt=PROBE-SALT-1")
    again = [it.get("id") for it in json.loads(body4.decode("utf-8", "replace")).get("items", [])]
    check("A10 одна и та же соль → тот же набор", salted == again,
          "пересечение %d из %d" % (len(set(salted) & set(again)), len(salted)))

# Обложка: карточка строит URL из id; по file_path сервер отвечает 403.
if items:
    codes = []
    for it in items[:6]:
        stc, _, _ = get("/api/books/cover?f=" + urllib.parse.quote(it["id"], safe=""))
        codes.append(stc)
    ok200 = sum(1 for c in codes if c == 200)
    check("A11 обложка по id отдаётся картинкой (у части флибустовских файлов обложки нет — 404)",
          ok200 > 0 and all(c in (200, 404) for c in codes), "первые 6: %s" % codes)
    fp = urllib.parse.quote(items[0].get("file_path") or "", safe="")
    st6, _, _ = get("/api/books/cover?f=" + fp)
    check("A12 обложка по file_path остаётся закрытой (403) — потому карточка её не зовёт",
          st6 in (403, 404), "%s для %r" % (st6, (items[0].get("file_path") or "")[:40]))

st7, body7, _ = get("/api/stats/recommendations/books")
prefs = json.loads(body7.decode("utf-8", "replace")) if st7 == 200 else {}
chip_names = [g[0] for g in (prefs.get("genres") or []) if isinstance(g, list)]
note("A13 чипы предпочтений называют жанр по имени, а не кодом («sf»)",
     all(not re.fullmatch(r"[a-z_]+", c) for c in chip_names), chip_names[:6], "рестарта")
note("A14 баннер подборки без сырых кодов жанров",
     not re.search(r"\bsf\b|\bfant\w*\b|\bsf_\w+\b", banner), banner[:200], "рестарта")

# ==================================== B. живой HTML: шаблон уже отдан новый
section("B. живой HTML: дизайн-контракт шаблона")
fn = js_function(html, "renderRecommendations")
check("B1 в живом HTML нет несуществующего класса .books-grid", "books-grid" not in fn,
      "есть" if "books-grid" in html else "нет во всём шаблоне")
check("B2 карточка строит обложку из id (collection://), а не из file_path",
      "encodeURIComponent(b.id" in js_function(html, "recsCard"), "")
check("B3 сетка recommendations ссылается на .book-grid, который есть в CSS",
      "book-grid" in fn and re.search(r"\.book-grid\s*\{", html) is not None, "")
check("B4 контейнеры блока описаны в CSS шаблона, а не выдуманы в разметке",
      all(re.search(r"\.%s\s*[,{]" % c, html) for c in
          ("recs-wrap", "recs-head", "recs-note", "recs-grid", "recs-reason", "pref-chip")), "")
check("B5 кнопка «Обновить» передаёт свежую соль", "renderRecommendations(String(Date.now()))" in fn)
check("B6 ленивые обложки подключаются после вставки html", "setupLazyCovers(el)" in fn)

# ====================================== C. рендер в браузере: что видит человек
section("C.headless-рендер страницы (видеокарточка «рекомендации»)")
try:
    from playwright.sync_api import sync_playwright
except Exception as exc:
    print("пропущено: playwright недоступен (%r)" % (exc,))
    res.append(("C0 playwright доступен", False))
else:
    errors = []
    cover404 = []
    unrelated = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        # У части флибустовских файлов обложки нет — это ожидаемый 404 и
        # плейсхолдер, а не поломка интерфейса. Ошибки чужих эндпоинтов
        # (например /api/qdrant/added) печатаем наблюдением: к подборке книг
        # они отношения не имеют, и молчать о них тоже нельзя.
        def on_console(m):
            if m.type != "error":
                return
            url = (m.location or {}).get("url", "") or ""
            if "/api/books/cover" in url:
                cover404.append(url)
            elif "/api/stats/recommendations" in url:
                errors.append("%s %s" % (url, m.text))
            else:
                unrelated.append(url.split("8281")[-1])

        page.on("console", on_console)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/books", wait_until="domcontentloaded", timeout=45000)
        # Секция «Рекомендации» открывается по nav-элементу с id=recommendations.
        opened = page.evaluate("""() => {
          if (typeof showSection === 'function') { showSection('recommendations'); return 'showSection'; }
          if (typeof switchTab === 'function') { switchTab('recommendations'); return 'switchTab'; }
          const el = document.querySelector('[data-id=recommendations], #nav-recommendations');
          if (el) { el.click(); return 'click'; }
          if (typeof renderRecommendations === 'function') { renderRecommendations(); return 'direct'; }
          return 'none';
        }""")
        try:
            page.wait_for_function(
                """() => { const el = document.getElementById('recsContent');
                            return el && !/Загрузка/.test(el.textContent) && el.children.length; }""",
                timeout=30000)
        except Exception:
            pass
        print("способ открытия: %s" % opened)
        # Обложка подгружается по IntersectionObserver: карточка ниже сгиба не
        # грузится, пока её не увидели. Поэтому сначала прокручиваем блок
        # экранами вниз, и только потом ждём исхода по каждой карточке.
        page.evaluate("""() => { const el = document.getElementById('recsContent');
                                 el.scrollIntoView({block: 'start'}); }""")
        for _ in range(8):
            page.evaluate("window.scrollBy(0, Math.floor(innerHeight * 0.85))")
            page.wait_for_timeout(700)
            if page.evaluate("document.getElementById('recsContent')"
                             ".getBoundingClientRect().bottom <= innerHeight"):
                break
        # Битая обложка ретраится ~3,5 с, поэтому ждём, пока каждая карточка не
        # придёт к одному из двух исходов: реальная картинка либо плейсхолдер.
        try:
            page.wait_for_function(
                """() => [...document.querySelectorAll('#recsContent .book-card')].every(c => {
                     const i = c.querySelector('.book-cover img');
                     const ph = c.querySelector('.book-cover .placeholder');
                     if (!i) return true;
                     return (i.complete && i.naturalWidth > 1 && i.style.display !== 'none')
                            || (ph && ph.style.display === 'flex');
                   })""", timeout=40000)
        except Exception:
            pass
        # Если ожидание истекло, даём ретраям последних 404-обложек договорить:
        # иначе снимок показывает «незавершённые», которых на самом деле нет.
        page.wait_for_timeout(2000)
        snap = page.evaluate("""() => {
          const el = document.getElementById('recsContent');
          const grid = el.querySelector('.recs-grid');
          const cards = [...el.querySelectorAll('.book-card')];
          const cs = grid ? getComputedStyle(grid) : null;
          let real = 0, ph = 0, dead = 0;
          cards.forEach(c => {
            const i = c.querySelector('.book-cover img');
            const p = c.querySelector('.book-cover .placeholder');
            if (i && i.complete && i.naturalWidth > 1 && i.style.display !== 'none') real++;
            else if (p && p.style.display === 'flex') ph++;
            else dead++;
          });
          const box = cards.length ? cards[0].getBoundingClientRect() : null;
          const second = cards.length > 1 ? cards[1].getBoundingClientRect() : null;
          return {
            text: (el.textContent || '').slice(0, 400),
            cards: cards.length,
            tracks: cs ? cs.gridTemplateColumns.split(' ').length : 0,
            rows: box && second ? (Math.abs(box.top - second.top) > 2 ? 'столбиком' : 'в ряд') : '',
            real: real, ph: ph, dead: dead,
            hasHead: !!el.querySelector('.recs-head'),
            hasNote: !!el.querySelector('.recs-note'),
            hasReason: !!el.querySelector('.recs-reason'),
          };
        }""")
        check("C1 блок рекомендаций отрендерился (не «Загрузка…»)", snap["cards"] > 0,
              "карточек: %d; текст: %s" % (snap["cards"], snap["text"][:120]))
        check("C2 карточки стоят в сетку, а не столбиком по одной",
              snap["tracks"] > 1 and snap["rows"] == "в ряд",
              "колонок: %d, вторая карточка — %s" % (snap["tracks"], snap["rows"]))
        check("C3 обложки доезжают, а у книг без обложки стоит плейсхолдер (не пустое место)",
              snap["real"] > 0 and snap["dead"] == 0,
              "картинок %d, плейсхолдеров %d, незавершённых %d из %d"
              % (snap["real"], snap["ph"], snap["dead"], snap["cards"]))
        check("C4 на месте шапка блока, баннер «на основе» и чип причины",
              snap["hasHead"] and snap["hasNote"] and snap["hasReason"],
              (snap["hasHead"], snap["hasNote"], snap["hasReason"]))
        note("C5 в отрендеренном тексте нет кодов жанров («sf»)",
             not re.search(r"\bsf\b", snap["text"]), snap["text"][:160], "рестарта")
        check("C6 при открытии блока ошибок в консоли нет", not errors, errors[:3])

        # «Обновить» = новый запрос с солью; список не должен зависать в загрузке.
        seen_urls = []
        page.on("request", lambda r: seen_urls.append(r.url) if "recommendations/concrete" in r.url else None)
        page.get_by_role("button", name="Обновить").first.click()
        page.wait_for_timeout(4000)
        after = page.evaluate("""() => {
          const el = document.getElementById('recsContent');
          return {cards: el.querySelectorAll('.book-card').length,
                  loading: /Загрузка/.test(el.textContent)};
        }""")
        check("C7 «Обновить» уходит с солью в URL и возвращает готовый список",
              any("salt=" in u for u in seen_urls) and after["cards"] > 0 and not after["loading"],
              "запросов: %d, соль в URL: %s, карточек после: %d"
              % (len(seen_urls), any("salt=" in u for u in seen_urls), after["cards"]))

        # Параллель с карточкой библиотеки сравниваем по-честному: открываем
        # «Коллекцию», снимаемComputed стили первой карточки и сверяем с карточкой
        # подборки. Одинаковый класс обязан давать одинаковый стиль.
        card_style = """() => { const c = document.querySelector('%s .book-card');
          if (!c) return null; const cs = getComputedStyle(c);
          return [cs.borderRadius, cs.boxShadow, cs.overflow,
                  getComputedStyle(c.querySelector('.book-cover')).aspectRatio]; }"""
        recs_style = page.evaluate(card_style % "#recsContent")
        # Снимок подборки — ДО переключения на «Коллекцию»: иначе на файле
        # оказывается библиотека, а не то, что правили (так и было в первом прогоне).
        page.evaluate("document.getElementById('recsContent').scrollIntoView({block: 'start'})")
        page.wait_for_timeout(400)
        on_screen = page.evaluate(
            "() => !!document.getElementById('recsContent').offsetParent")
        page.screenshot(path="E:/tmp/books_recs_live.png")
        check("C9 в момент снимка на экране блок подборки, а не библиотека", on_screen,
              "скриншот: E:/tmp/books_recs_live.png")
        page.evaluate("showSection('library')")
        page.wait_for_timeout(3000)
        lib_style = page.evaluate(card_style % "#libraryView")
        page.screenshot(path="E:/tmp/books_library_live.png")
        check("C8 карточка подборки выглядит как карточка библиотеки (те же computed-стили)",
              recs_style is not None and lib_style is not None and recs_style == lib_style,
              "подборка: %s / библиотека: %s" % (recs_style, lib_style))
        badges = page.evaluate("""() => document.querySelectorAll(
            '#recsContent .book-card .format-badge').length""")
        years = [it.get("year") for it in items]
        if any(years):
            check("C10 год на карточке — бейдж, а не строка текста", badges > 0,
                  "бейджей года: %d" % badges)
        else:
            print("наблюдение: в живом каталоге у всех предложенных книг year = NULL, "
                  "поэтому бейдж года молчит (в гейте он проверен на синтетическом годе)")
        if unrelated:
            print("наблюдение (чужие эндпоинты, к подборке не относится): %s"
                  % ", ".join(sorted(set(unrelated))[:4]))
        print("скриншоты: E:/tmp/books_recs_live.png (подборка), "
              "E:/tmp/books_library_live.png (библиотека для сравнения)")
        browser.close()

print()
bad = [n for n, ok in res if not ok]
print("Итог: %d проверок, провалов: %d  (режим: %s)"
      % (len(res), len(bad), "после рестарта" if AFTER else "до рестарта"))
for n in bad:
    print("  -", n)
sys.exit(1 if bad else 0)
