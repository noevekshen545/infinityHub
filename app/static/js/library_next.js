/* Library Next — переключатель оформления для /books и /bookgen/next.

   Скрипт подключается ТОЛЬКО к новым шаблонам (атрибуты data-books-skin /
   data-bookgen-skin на <html>). Классические страницы (?skin=classic и
   /bookgen) его не загружают.

   Задачи:
     1) плавающая панель «Оформление»: ссылки на классический шаблон и на
        соседний модуль, чтобы сравнивать без правки адреса;
     2) выбор одной из девяти схем и акцентного цвета — через общий
        InfinityTheme, то есть те же настройки, что и во всём приложении;
     3) панель ничего не переопределяет в логике страницы: только читает
        текущее состояние и вызывает публичное API theme.js.

   Если theme.js ещё не выполнился (он подключён с defer), ждём его —
   страница к этому моменту уже отрисована. */
(function () {
  'use strict';

  var root = document.documentElement;
  var isBooks = !!root.getAttribute('data-books-skin');
  var isBookgen = !!root.getAttribute('data-bookgen-skin');
  if (!isBooks && !isBookgen) return;

  var LS_OPEN = 'library_skin_panel_open';

  /* id — значения, которые понимает theme.js (InfinityTheme.themes). */
  var THEMES = [
    { id: 'light',    label: 'Светлая',  icon: 'fa-sun' },
    { id: 'dark',     label: 'Тёмная',   icon: 'fa-moon' },
    { id: 'sepia',    label: 'Сепия',    icon: 'fa-scroll' },
    { id: 'warm',     label: 'Тёплая',   icon: 'fa-fire' },
    { id: 'paper',    label: 'Бумага',   icon: 'fa-file-lines' },
    { id: 'ocean',    label: 'Океан',    icon: 'fa-water' },
    { id: 'forest',   label: 'Лес',      icon: 'fa-tree' },
    { id: 'midnight', label: 'Полночь',  icon: 'fa-star' },
    { id: 'rose',     label: 'Роза',     icon: 'fa-heart' }
  ];

  var LINKS = isBooks ? [
    { href: '/books?skin=classic', label: 'Классическая библиотека', icon: 'fa-book' },
    { href: '/books',              label: 'Библиотека',              icon: 'fa-layer-group', current: true },
    { href: '/bookgen/next',       label: 'Генератор книг 2.0',      icon: 'fa-wand-magic-sparkles' }
  ] : [
    { href: '/bookgen',      label: 'Классический генератор',  icon: 'fa-book' },
    { href: '/bookgen/next', label: 'Генератор книг 2.0',      icon: 'fa-layer-group', current: true },
    { href: '/books',        label: 'Библиотека',              icon: 'fa-book-open' }
  ];

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function readOpen() {
    try { return localStorage.getItem(LS_OPEN) === '1'; } catch (e) { return false; }
  }

  function storeOpen(value) {
    try { localStorage.setItem(LS_OPEN, value ? '1' : '0'); } catch (e) { /* приватный режим */ }
  }

  function theme(id) {
    for (var i = 0; i < THEMES.length; i++) {
      if (THEMES[i].id === id) return THEMES[i];
    }
    return THEMES[0];
  }

  function themeVars(id) {
    var T = window.InfinityTheme;
    var vars = (T && T.vars && T.vars[id]) || {};
    return {
      bg: vars['--bg'] || '',
      accent: vars['--accent'] || ''
    };
  }

  function start() {
    var T = window.InfinityTheme;
    if (!T) return;

    /* В библиотеке этой кнопки нет по просьбе пользователя. Схема и акцент
       и так меняются в тулбаре страницы (#themeBtn, #themePicker) и в общих
       настройках; откат на исходный шаблон остаётся доступным по адресу
       /books?skin=classic. Панель сохранилась только в генераторе книг 2.0. */
    if (isBooks) return;

    var panel = el('div', 'library-skin');
    var toggle = el('button', 'library-skin__toggle');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', 'Оформление и шаблоны');
    var dot = el('span', 'dot');
    var caption = el('span', null, 'Оформление');
    var chevron = document.createElement('i');
    chevron.className = 'fa-solid fa-chevron-up';
    toggle.appendChild(dot);
    toggle.appendChild(caption);
    toggle.appendChild(chevron);

    var body = el('div', 'library-skin__panel');
    body.setAttribute('role', 'dialog');
    body.setAttribute('aria-label', 'Оформление страницы');

    /* ── Шаблоны ─────────────────────────────────────────────────────── */
    body.appendChild(el('div', 'library-skin__cap', 'Шаблоны'));
    var links = el('div', 'library-skin__links');
    var linkNodes = [];
    LINKS.forEach(function (item) {
      var a = el('a');
      a.href = item.href;
      if (item.current) a.className = 'on';
      var icon = document.createElement('i');
      icon.className = 'fa-solid ' + item.icon;
      a.appendChild(icon);
      a.appendChild(el('span', null, item.label));
      if (item.current) {
        var mark = document.createElement('i');
        mark.className = 'fa-solid fa-check';
        mark.style.marginLeft = 'auto';
        mark.style.fontSize = '11px';
        a.appendChild(mark);
        a.setAttribute('aria-current', 'page');
      }
      links.appendChild(a);
      linkNodes.push(a);
    });
    body.appendChild(links);

    /* ── Схемы ───────────────────────────────────────────────────────── */
    body.appendChild(el('div', 'library-skin__cap', 'Схема'));
    var grid = el('div', 'library-skin__grid');
    var themeButtons = [];
    THEMES.forEach(function (t) {
      var b = el('button', 'library-skin__theme');
      b.type = 'button';
      b.dataset.theme = t.id;
      b.title = t.label;
      var swatch = el('span', 'swatch');
      var s = themeVars(t.id);
      /* Образец строится из самой палитры темы, а не из отдельного списка
         цветов: если палитра изменится, образец изменится вместе с ней. */
      swatch.style.background = s.accent && s.bg
        ? 'linear-gradient(135deg, ' + s.bg + ' 0 50%, ' + s.accent + ' 50% 100%)'
        : 'var(--bg3)';
      var icon = document.createElement('i');
      icon.className = 'fa-solid ' + t.icon;
      b.appendChild(swatch);
      b.appendChild(icon);
      b.appendChild(el('span', null, t.label));
      b.addEventListener('click', function () {
        try { T.set(t.id); } catch (e) { /* тема — украшение */ }
        sync();
      });
      grid.appendChild(b);
      themeButtons.push(b);
    });
    body.appendChild(grid);

    /* ── Акцент ──────────────────────────────────────────────────────── */
    body.appendChild(el('div', 'library-skin__cap', 'Акцент'));
    var accents = el('div', 'library-skin__accents');
    var accentButtons = [];
    (T.accent && T.accent.presets ? T.accent.presets : []).forEach(function (p) {
      var b = el('button', 'library-skin__accent');
      b.type = 'button';
      b.style.background = p.hex;
      b.title = p.name;
      b.setAttribute('aria-label', 'Акцент: ' + p.name);
      b.addEventListener('click', function () {
        try { T.accent.set(p.hex); } catch (e) { /* украшение */ }
        sync();
      });
      accents.appendChild(b);
      accentButtons.push({ node: b, hex: p.hex.toLowerCase() });
    });
    var reset = el('button', 'library-skin__accent reset', 'Авто');
    reset.type = 'button';
    reset.title = 'Вернуть акцент темы';
    reset.addEventListener('click', function () {
      try { T.accent.reset(); } catch (e) { /* украшение */ }
      sync();
    });
    accents.appendChild(reset);
    body.appendChild(accents);

    var hint = el('div', 'library-skin__cap', '');
    hint.style.textTransform = 'none';
    hint.style.letterSpacing = '0';
    hint.style.fontWeight = '500';
    hint.style.color = 'var(--text3)';
    hint.textContent = 'Настройки общие для всего приложения.';
    body.appendChild(hint);

    panel.appendChild(toggle);
    panel.appendChild(body);
    document.body.appendChild(panel);

    var open = readOpen();
    function setOpen(value) {
      open = !!value;
      panel.classList.toggle('open', open);
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      storeOpen(open);
    }
    setOpen(open);

    toggle.addEventListener('click', function () { setOpen(!open); });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && open) {
        setOpen(false);
        toggle.focus();
      }
    });

    /* Клик вне панели закрывает её — как у остальных поповеров страницы. */
    document.addEventListener('mousedown', function (e) {
      if (!open) return;
      if (panel.contains(e.target)) return;
      setOpen(false);
    });

    function sync() {
      var current = '';
      try { current = T.get(); } catch (e) { current = 'light'; }
      themeButtons.forEach(function (b) {
        b.classList.toggle('on', b.dataset.theme === current);
      });
      var accent = '';
      try { accent = (T.accent.get() || '').toLowerCase(); } catch (e) { accent = ''; }
      accentButtons.forEach(function (item) {
        item.node.classList.toggle('on', !!accent && item.hex === accent);
      });
      reset.classList.toggle('on', !accent);
      /* Заголовок панели подсказывает текущую схему. */
      var t = theme(current);
      toggle.title = 'Оформление · ' + t.label + (accent ? ' · свой акцент' : '');
    }

    sync();

    /* Тема может измениться из другого места страницы (кнопка в тулбаре,
       горячая клавиша) или из другой вкладки — подписываемся на оба канала. */
    if (typeof T.onChange === 'function') {
      try { T.onChange(sync); } catch (e) { /* */ }
    }
    window.addEventListener('storage', function (e) {
      if (!e.key || e.key === 'infinity_theme' || e.key === 'infinity_accent') sync();
    });
    /* Акцент theme.js пишет инлайном в style на <html>: наблюдаем атрибут. */
    if (typeof MutationObserver === 'function') {
      try {
        new MutationObserver(sync).observe(root, {
          attributes: true,
          attributeFilter: ['style', 'data-theme']
        });
      } catch (e) { /* */ }
    }

    /* Ссылки на копии того же шаблона: ?open=<путь> и прочие параметры
       страницы сохраняем, чтобы не потерять открытую книгу/проект. */
    linkNodes.forEach(function (a) {
      a.addEventListener('click', function () {
        if (!location.search || a.classList.contains('on')) return;
        var keep = new URLSearchParams(location.search);
        /* Параметры самой ссылки (например, ?skin=classic) главнее. */
        new URLSearchParams(a.search || '').forEach(function (value, key) {
          keep.set(key, value);
        });
        var qs = keep.toString();
        a.href = a.pathname + (qs ? '?' + qs : '');
      });
    });
  }

  function boot() {
    if (window.InfinityTheme) { start(); return; }
    /* theme.js подключён с defer и обычно уже выполнен. Если нет (медленная
       сеть, порядок вставки) — ждём до 5 секунд. */
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      if (window.InfinityTheme) {
        clearInterval(timer);
        start();
      } else if (tries > 100) {
        clearInterval(timer);
      }
    }, 50);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();

/* Рейл-меню на узких экранах.
   На десктопе меню всегда видно; на телефоне и планшете оно уезжает за
   край и открывается кнопкой-гамбургером. Кнопка и затемнение создаются
   здесь, чтобы не трогать разметку страницы: при отключённом JS меню
   просто остаётся на месте. */
(function () {
  'use strict';

  var root = document.documentElement;
  var isBooks = !!root.getAttribute('data-books-skin');
  var isBookgen = !!root.getAttribute('data-bookgen-skin');
  if (!isBooks && !isBookgen) return;

  /* Свёрнутый рейл: на кромке появляется стрелка, пункты остаются иконками.
     Подписи уходят в title, поэтому подсказка есть на любом пункте. */
  function setupRailCollapse(rail) {
    var rootEl = document.documentElement;
    var KEY = 'books_rail_collapsed';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'rail-collapse';
    btn.innerHTML = '<i class="fa-solid fa-chevron-left"></i>';
    rail.appendChild(btn);

    function isCollapsed() {
      return rootEl.classList.contains('rail-collapsed');
    }

    function setCollapsed(on) {
      rootEl.classList.toggle('rail-collapsed', !!on);
      try { localStorage.setItem(KEY, on ? '1' : '0'); } catch (e) { /* приватный режим */ }
      btn.title = on ? 'Развернуть меню' : 'Свернуть меню';
      btn.setAttribute('aria-label', btn.title);
      btn.setAttribute('aria-expanded', on ? 'false' : 'true');
    }

    Array.prototype.forEach.call(rail.querySelectorAll('.btn'), function (b) {
      if (b.title) return;
      var kids = b.children;
      for (var i = 0; i < kids.length; i++) {
        var kid = kids[i];
        if (kid.tagName === 'SPAN' && !kid.classList.contains('rail-brand-text')) {
          var label = (kid.textContent || '').trim();
          if (label) b.title = label;
          break;
        }
      }
    });

    var stored = '0';
    try { stored = localStorage.getItem(KEY) || '0'; } catch (e) { /* приватный режим */ }
    setCollapsed(stored === '1');

    btn.addEventListener('click', function (ev) {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      setCollapsed(!isCollapsed());
    });

    var search = rail.querySelector('.search-box');
    if (search) {
      search.addEventListener('click', function () {
        if (!isCollapsed()) return;
        setCollapsed(false);
        var input = rail.querySelector('#searchInput');
        if (input) setTimeout(function () { try { input.focus(); } catch (e) {} }, 150);
      });
    }
  }

  function start() {
    var rail = document.querySelector('.toolbar');
    if (!rail) return;

    /* Сворачивание рейла в полосу иконок (по образцу меню Search All).
       Ширина задаётся классом rail-collapsed на <html>: от неё зависят и
       отступ контента, и позиция панели жанров. */
    if (isBooks) setupRailCollapse(rail);

    var toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'library-rail-toggle';
    toggle.setAttribute('aria-label', 'Меню разделов');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.innerHTML = '<i class="fa-solid fa-bars"></i>';

    var scrim = document.createElement('div');
    scrim.className = 'library-rail-scrim';

    document.body.appendChild(scrim);
    document.body.appendChild(toggle);

    function isOpen() {
      return document.body.classList.contains('library-rail-open');
    }

    function setOpen(value) {
      var open = !!value;
      document.body.classList.toggle('library-rail-open', open);
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.innerHTML = '<i class="fa-solid fa-' + (open ? 'xmark' : 'bars') + '"></i>';
    }

    toggle.addEventListener('click', function () { setOpen(!isOpen()); });
    scrim.addEventListener('click', function () { setOpen(false); });

    /* Переход по пункту меню закрывает панель. Раскрытое «Ещё» не считаем
       переходом — оно само прячется по клику вне. */
    rail.addEventListener('click', function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      var item = target.closest('a.btn, button.btn');
      if (item && !item.closest('details.more')) setOpen(false);
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && isOpen()) {
        setOpen(false);
        toggle.focus();
      }
    });

    window.addEventListener('resize', function () {
      if (window.innerWidth > 980) setOpen(false);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
