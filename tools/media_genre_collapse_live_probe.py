# -*- coding: utf-8 -*-
"""Живой контроль: сворачивание полосы жанров в медиа + удаление кнопки
«Оформление» в книгах (задачи №86, №87).

Что здесь доказывает факт, а что — нет, честно:

  A. выдача /media и /static/js/library_next.js: правка действительно лежит на
     живом пути (шаблон и статика читаются с диска на каждый запрос);
  B. поведение полосы жанров. В живой библиотеке по умолчанию 50 треков и ОДИН
     ненулевой жанр («Pop»), поэтому полоса скрыта по ПРЕЖНЕМУ правилу
     entries.length < 2 и сворачивать нечего. Чтобы прогнать настоящие функции
     на настоящей странице, набор жанров подкладывается в память страницы
     (musicFullIndex / videoIndex) и вызывается buildMusicGenreFilter() /
     buildMovieGenreFilter() — сам код, CSS и обработчики при этом боевые.
     Правило «<2 жанров → полоса скрыта» проверяется отдельно, на живых данных.
  C. /books: плавающей кнопки «Оформление» больше нет, тулбар с темой и
     рейл-меню живые, а в генераторе книг 2.0 панель осталась (правка точечная).

Ни одна запись не уходит на сервер: не-GET глушатся в странице, данные
синтетические и живут до закрытия headless-вкладки.

Прогон:
  cd app && PYTHONIOENCODING=utf-8 python ../tools/media_genre_collapse_live_probe.py
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8281"
res = []


def get(path, timeout=60.0):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "genre-collapse-probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check(name, ok, detail=""):
    res.append((name, bool(ok)))
    print(("да   " if ok else "НЕТ  ") + name + (" — " + str(detail)[:320] if detail else ""))


def obs(text):
    print("наблюдение: " + text)


print("== A. живой путь отдаёт правку ==")
st, raw = get("/media")
media_html = raw.decode("utf-8", "replace") if st == 200 else ""
check("A1 /media отдаётся", st == 200 and len(media_html) > 10000, st)
check("A2 CSS свёрнутого состояния отдан живой страницей",
      ".genre-filter.collapsed .genre-chip { display: none }" in media_html
      and ".genre-filter.collapsed .genre-chip.active" in media_html)
check("A3 хелперы определены один раз (не размазаны по двум сборщикам)",
      media_html.count("function genreHeadHtml(") == 1
      and media_html.count("function bindGenreHead(") == 1
      and media_html.count("function genreIsCollapsed(") == 1)
for bar in ("musicGenreFilter", "movieGenreFilter"):
    check("A4 полоса «%s» получает заголовок-переключатель и привязку" % bar,
          "genreHeadHtml('%s'" % bar in media_html and "bindGenreHead('%s')" % bar in media_html)
check("A5 переключатель прописан ровно в двух полосах",
      media_html.count("genreHeadHtml(") == 3 and media_html.count("bindGenreHead(") == 3,
      media_html.count("genreHeadHtml("))
st, raw = get("/static/js/library_next.js")
lnjs = raw.decode("utf-8", "replace") if st == 200 else ""
check("A6 library_next.js отдаётся с точечным выходом для /books",
      st == 200 and "if (isBooks) return;" in lnjs
      and lnjs.index("if (isBooks) return;") < lnjs.index("var panel = el('div', 'library-skin')"), st)
check("A7 в панели больше нет мёртвого кода, нужного только ей: скрипт живёт дальше "
      "(рейл-меню отдельным блоком)",
      lnjs.count("function start()") == 2 and "setupRailCollapse" in lnjs)

print("\n== B. живая страница: полоса жанров ==")
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

STATE = """(barId) => {
  const gf = document.getElementById(barId);
  if (!gf) return null;
  const chips = [...gf.querySelectorAll('.genre-chip')];
  const vis = chips.filter(c => getComputedStyle(c).display !== 'none');
  const head = gf.querySelector('[data-genre-head]');
  return {
    barDisplay: getComputedStyle(gf).display,
    total: chips.length,
    visible: vis.length,
    visibleText: vis.map(c => c.textContent.trim().replace(/\\s+/g,' ').slice(0, 22)),
    collapsed: gf.classList.contains('collapsed'),
    head: !!head,
    headText: head ? head.textContent.trim().replace(/\\s+/g,' ') : '',
    aria: head ? head.getAttribute('aria-expanded') : null,
    chevron: head ? (head.querySelector('i:last-child') || {}).className : '',
    height: Math.round(gf.getBoundingClientRect().height),
  };
}"""

# Синтетический набор кладётся в переменные живой страницы; боевые функции
# (buildMusicGenreFilter / buildMovieGenreFilter) читают их как обычный индекс.
SYN_MUSIC = """([n, k]) => {
  activeMusicMood = null; activeMusicGenre = null;
  musicFullIndex = { tracks: Array.from({length: n}, (_, i) => ({
    id: 'synth-' + i, title: 'Синтез ' + i, artist: 'Артист', album: 'Альбом',
    genre: 'Жанр-' + (i % k), duration: 200 + i })) };
  return (musicFullIndex.tracks || []).length;
}"""

SYN_MOVIES = """([tpl, n]) => {
  activeMovieGenre = null;
  videoIndex = { videos: Array.from({length: n}, (_, i) => Object.assign({}, tpl, {
    id: 'synth-v' + i, title: 'Фильм ' + i,
    genres: ['Вид-' + (i % 6), 'Драма'], year: 2000 + i })) };
  return (videoIndex.videos || []).length;
}"""

# Шаблон берём у живого API: карточка фильма обязана сохраниться в renderMovies,
# иначе проверка B16 падёт не на правке, а на самоделной форме объекта.
try:
    _st, _raw = get("/api/media/video-index")
    _vids = (json.loads(_raw.decode("utf-8")).get("videos") or []) if _st == 200 else []
except Exception as exc:
    _vids = []
    obs("живой video-index не прочитан (%r) — для фильмов берём минимальную форму" % (exc,))
VIDEO_TPL = _vids[0] if _vids else {"id": 0, "title": "Т", "year": 2000, "genres": [],
                                    "overview": "", "director": ""}

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:
    print("пропущено: playwright недоступен (%r)" % (exc,))
    res.append(("B0 playwright доступен", False))
else:
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(NO_WRITE)
        page.goto(BASE + "/media", wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_function("() => document.querySelectorAll('#musicContent .track-item').length > 0",
                                   timeout=30000)
        except Exception:
            pass
        check("B1 живая страница после правки рендерит список треков",
              page.evaluate("() => document.querySelectorAll('#musicContent .track-item').length") > 0,
              page.evaluate("() => document.querySelectorAll('#musicContent .track-item').length"))
        check("B2 хелперы сворачивания определились в боевом скрипте страницы",
              page.evaluate("() => ['genreHeadHtml','bindGenreHead','genreIsCollapsed']"
                            ".every(f => typeof window[f] === 'function')"))

        # Прежнее правило должно остаться: на живых данных (<2 жанров) полосы нет.
        live = page.evaluate(STATE, "musicGenreFilter")
        live_genres = page.evaluate(
            "() => new Set((musicIndex.tracks||[]).map(t=>t.genre).filter(Boolean)).size")
        check("B3 живое правило сохранено: жанров меньше двух — полоса скрыта целиком "
              "и никакого полу-состояния нет",
              live["barDisplay"] == "none" and not live["head"], {"жанров": live_genres, **live})
        obs("в живой библиотеке по умолчанию %d треков и %d ненулевой жанр, поэтому "
            "дальше полоса собирается из синтетического набора в памяти страницы"
            % (page.evaluate("() => (musicIndex.tracks||[]).length"), live_genres))

        # ── Музыка: настоящие функции, подставленные данные ─────────────────
        page.evaluate(SYN_MUSIC, [64, 8])
        page.evaluate("() => buildMusicGenreFilter()")
        page.wait_for_timeout(400)
        exp = page.evaluate(STATE, "musicGenreFilter")
        check("B4 развёрнутая полоса: 8 жанров + «Все» видны все, заголовок с числом",
              exp["barDisplay"] == "flex" and exp["total"] == 9 and exp["visible"] == 9
              and exp["head"] and exp["aria"] == "true" and "8" in exp["headText"], exp)
        check("B5 заголовок-переключатель доступен с клавиатуры (button + aria)",
              page.evaluate("() => { const h=document.querySelector('#musicGenreFilter [data-genre-head]');"
                            "return h.tagName === 'BUTTON' && h.type === 'button' }"))

        page.click("#musicGenreFilter [data-genre-head]")
        page.wait_for_timeout(300)
        col = page.evaluate(STATE, "musicGenreFilter")
        stored = page.evaluate("() => localStorage.getItem('mediaGenreCollapsed')")
        check("B6 клик сворачивает: жанровые чипы скрыты, виден только «Все», "
              "класс/aria/стрелка обновились",
              col["collapsed"] and col["visible"] == 1 and col["total"] == 9
              and col["visibleText"][0].startswith("Все")
              and col["aria"] == "false" and "fa-chevron-down" in col["chevron"], col)
        check("B7 свёрнутая полоса — одна строка (не выше 46px)",
              col["height"] <= 46, col["height"])
        check("B8 свёрнутое состояние сохранено под id полосы",
              stored and "musicGenreFilter" in stored, stored)

        # Применённый фильтр обязан быть виден в свёрнутой полосе.
        page.evaluate("() => { activeMusicGenre = 'Жанр-3'; buildMusicGenreFilter(); }")
        page.wait_for_timeout(300)
        fcol = page.evaluate(STATE, "musicGenreFilter")
        check("B9 в свёрнутом виде применённый жанр виден, остальные скрыты",
              fcol["collapsed"] and fcol["visible"] == 1
              and fcol["visibleText"][0].startswith("Жанр-3"), fcol)

        # Перерисовка полосы (смена вкладки/поиск) не должна сбрасывать выбор.
        page.evaluate("() => buildMusicGenreFilter()")
        page.wait_for_timeout(300)
        again = page.evaluate(STATE, "musicGenreFilter")
        check("B10 после перерисовки полоса остаётся свёрнутой и применённый жанр виден",
              again["collapsed"] and again["visible"] == 1 and again["aria"] == "false", again)

        page.evaluate("() => { activeMusicGenre = null; }")
        page.click("#musicGenreFilter [data-genre-head]")
        page.wait_for_timeout(300)
        back = page.evaluate(STATE, "musicGenreFilter")
        stored2 = page.evaluate("() => localStorage.getItem('mediaGenreCollapsed')")
        check("B11 второй клик разворачивает: видно все чипы, ключ снят",
              not back["collapsed"] and back["visible"] == back["total"]
              and "musicGenreFilter" not in (stored2 or ""), back)
        # Сворачивание должно переживать перезагрузку страницы, иначе кнопка
        # бесполезна: пользователь сворачивает, а при следующем открытии всё снова развёрнуто.
        page.click("#musicGenreFilter [data-genre-head]")
        page.wait_for_timeout(300)
        page.reload(wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_function("() => document.querySelectorAll('#musicContent .track-item').length > 0",
                                   timeout=30000)
        except Exception:
            pass
        page.evaluate(SYN_MUSIC, [64, 8])
        page.evaluate("() => buildMusicGenreFilter()")
        page.wait_for_timeout(500)
        reloaded = page.evaluate(STATE, "musicGenreFilter")
        check("B12 после перезагрузки страницы полоса по-прежнему свёрнута",
              reloaded["collapsed"] and reloaded["visible"] == 1, reloaded)
        page.click("#musicGenreFilter [data-genre-head]")
        page.wait_for_timeout(300)

        # ── Фильмы: своя полоса, своё состояние ────────────────────────────
        page.evaluate("() => showSection('movies')")
        page.wait_for_timeout(600)
        page.evaluate(SYN_MOVIES, [VIDEO_TPL, 48])
        page.evaluate("() => buildMovieGenreFilter()")
        page.wait_for_timeout(400)
        mexp = page.evaluate(STATE, "movieGenreFilter")
        check("B13 полоса фильмов собирается с тем же переключателем",
              mexp["barDisplay"] == "flex" and mexp["head"]
              and mexp["total"] >= 7 and mexp["visible"] == mexp["total"], mexp)
        page.click("#movieGenreFilter [data-genre-head]")
        page.wait_for_timeout(300)
        mcol = page.evaluate(STATE, "movieGenreFilter")
        independence = page.evaluate(
            """() => ({musicCollapsed: document.getElementById('musicGenreFilter')
                         .classList.contains('collapsed'),
                       movieCollapsed: document.getElementById('movieGenreFilter')
                         .classList.contains('collapsed')})""")
        check("B14 фильмы сворачиваются сами, музыка при этом остаётся развёрнутой",
              mcol["collapsed"] and independence["movieCollapsed"]
              and not independence["musicCollapsed"], independence)
        stored3 = page.evaluate("() => localStorage.getItem('mediaGenreCollapsed')")
        check("B15 состояние полос хранится раздельно: фильмы свернулись, музыка "
              "в хранилище не помечена",
              "movieGenreFilter" in (stored3 or "") and "musicGenreFilter" not in (stored3 or ""),
              stored3)

        # Свёрнутая полоса не ломает фильтрацию: чипы остаются в DOM и живые.
        page.evaluate("() => buildMovieGenreFilter()")
        page.wait_for_timeout(300)
        handler = page.evaluate(
            """() => { const c = [...document.querySelectorAll('#movieGenreFilter .genre-chip')]
                         .find(x => x.dataset.genre === 'Драма');
                       if (!c) return 'нет чипа';
                       c.click();
                       return {collapsed: document.getElementById('movieGenreFilter')
                                 .classList.contains('collapsed'),
                               active: activeMovieGenre}; }""")
        check("B16 клик по чипу из свёрнутой DOM-полосы фильтрует как прежде",
              isinstance(handler, dict) and handler["active"] == "Драма", handler)

        # Ради этого сворачивание и делается: с десятками жанров полоса переносится
        # на несколько строк и съедает место над списком треков.
        page.evaluate("() => showSection('music')")
        page.wait_for_timeout(400)
        page.evaluate(SYN_MUSIC, [192, 24])
        page.evaluate("() => buildMusicGenreFilter()")
        page.wait_for_timeout(400)
        wide = page.evaluate(STATE, "musicGenreFilter")
        page.click("#musicGenreFilter [data-genre-head]")
        page.wait_for_timeout(300)
        wide_col = page.evaluate(STATE, "musicGenreFilter")
        check("B17 с 24 жанрами развёрнутая полоса переносится на несколько строк",
              wide["height"] > 60 and wide["visible"] == wide["total"],
              {"высота": wide["height"], "чипов": wide["total"]})
        check("B18 свёрнутая полоса остаётся одной строкой: %d px → %d px, "
              "%d чипов → %d видимых" % (wide["height"], wide_col["height"],
                                         wide["total"], wide_col["visible"]),
              wide_col["height"] <= 46 and wide_col["height"] < wide["height"],
              wide_col)

        check("B19 ни одной записи на сервер (только чтение)",
              not page.evaluate("() => window.__posted"), page.evaluate("() => window.__posted"))
        check("B20 ноль ошибок консоли и страницы", not errors, errors[:4])
        browser.close()

print("\n== C. книги: кнопка «Оформление» убрана точечно ==")
st, raw = get("/books")
books_html = raw.decode("utf-8", "replace") if st == 200 else ""
check("C1 /books отдаётся", st == 200 and len(books_html) > 10000, st)
check("C2 схема и акцент остаются доступными в тулбаре страницы",
      'id="themeBtn"' in books_html and 'id="themePicker"' in books_html)
try:
    from playwright.sync_api import sync_playwright as _sp
except Exception:
    obs("playwright недоступен — C3…C6 не проверены живьём")
else:
    with _sp() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        cerr = []
        pg.on("pageerror", lambda e: cerr.append(str(e)[:200]))
        pg.goto(BASE + "/books", wait_until="domcontentloaded", timeout=45000)
        try:
            pg.wait_for_function("() => document.querySelector('.rail-collapse') "
                                 "|| document.querySelector('.library-skin__toggle')", timeout=20000)
        except Exception:
            pass
        pg.wait_for_timeout(1500)
        state = pg.evaluate("""() => ({
          skin: !!document.querySelector('.library-skin'),
          skinToggle: !!document.querySelector('.library-skin__toggle'),
          railCollapse: !!document.querySelector('.rail-collapse'),
          railToggle: !!document.querySelector('.library-rail-toggle'),
          themeBtn: !!document.querySelector('#themeBtn'),
          themePicker: !!document.querySelector('#themePicker'),
        })""")
        check("C3 плавающей кнопки «Оформление» на /books нет",
              not state["skin"] and not state["skinToggle"], state)
        check("C4 остальное поведение library_next.js живое: рейл сворачивается, "
              "кнопка меню на месте",
              state["railCollapse"] and state["railToggle"], state)
        check("C5 кнопка темы в тулбаре на месте (выбор схемы не потерян)",
              state["themeBtn"] and state["themePicker"], state)
        check("C6 ноль ошибок страницы после правки", not cerr, cerr[:3])
        pg.goto(BASE + "/bookgen/next", wait_until="domcontentloaded", timeout=45000)
        try:
            pg.wait_for_function("() => document.querySelector('.library-skin__toggle')",
                                 timeout=20000)
        except Exception:
            pass
        check("C7 в генераторе книг 2.0 панель «Оформление» осталась (правка точечная)",
              pg.evaluate("() => !!document.querySelector('.library-skin__toggle')"))
        b.close()

print("\nИтог: %d проверок, провалов: %d" % (len(res), sum(1 for _, ok in res if not ok)))
sys.exit(1 if any(not ok for _, ok in res) else 0)
