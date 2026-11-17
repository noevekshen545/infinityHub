/* ============================================================================
   Единая тема для всех страниц приложения.
   ----------------------------------------------------------------------------
   Раньше тема жила в двух местах: modern-главная хранила её в
   localStorage['infinity_theme'] и умела 9 вариантов, а /desktop — в
   localStorage['html_theme'] и умела только light/dark. Из-за этого
   переключение темы на одной странице не влияло на другую.

   Теперь:
     * один ключ — 'infinity_theme' (значение старого html_theme мигрируется);
     * тема по умолчанию — светлая;
     * страница красит себя сама по классу на <body> (body.light, body.sepia…),
       недостающие CSS-переменные добираются из THEME_VARS;
     * тема применяется до первой отрисовки, без мигания;
     * изменения синхронизируются между вкладками (событие storage).
   ============================================================================ */
(function (global) {
  'use strict';

  var KEY = 'infinity_theme';
  var LEGACY_KEY = 'html_theme';
  var DEFAULT_THEME = 'light';

  var ALL = ['light', 'dark', 'sepia', 'warm', 'paper', 'ocean', 'forest', 'midnight', 'rose'];

  /* Палитра тем, которых нет в стилях конкретной страницы.
     Значения совпадают с палитрой modern-главной. */
  var THEME_VARS = {
    /* Светлая тема. Текст — #333 на всех трёх уровнях: по просьбе пользователя
       в светлой теме не должно быть ничего светлее #333, иначе подписи и
       названия на панели задач теряются на светлом фоне. */
    light: {
      '--bg': '#fafbfc', '--bg2': '#ffffff', '--bg3': '#f3f4f6', '--bg4': '#e5e7eb',
      '--border': 'rgba(0,0,0,0.06)', '--border2': 'rgba(0,0,0,0.1)',
      '--text': '#1f2937', '--text2': '#333333', '--text3': '#333333',
      '--accent': '#4f46e5', '--accent2': '#4338ca',
      '--glass': 'rgba(255,255,255,0.72)', '--glass-border': 'rgba(0,0,0,0.08)',
      /* Поверхности. Без них карточки оставались белыми на тёмных темах:
         applyVars() пишет палитру инлайном на <html> и перекрывает :root
         страниц, а --card/--surface там не было — и .card из design.css
         красился значением из чужого :root. */
      '--card': '#ffffff', '--surface': '#ffffff',
      '--shadow': '0 4px 20px rgba(0,0,0,0.06)', '--shadow-lg': '0 8px 40px rgba(0,0,0,0.08)'
    },
    /* Тёмная тема. Её не было вовсе: при выборе «dark» apply() не ставил
       класс на <body>, палитра из THEME_VARS не находилась, и страница
       оставалась на светлом :root из design.css — то есть выбор темы
       не делал ровно ничего. Значения совпадают с body.dark в design.css,
       чтобы тема выглядела одинаково на всех страницах. */
    dark: {
      '--bg': '#202020', '--bg2': '#2b2b2b', '--bg3': '#333333', '--bg4': '#3d3d3d',
      '--border': 'rgba(255,255,255,0.1)', '--border2': 'rgba(255,255,255,0.16)',
      '--text': '#ffffff', '--text2': '#c4c4c4', '--text3': '#9a9a9a',
      '--accent': '#4cc2ff', '--accent2': '#69d0ff',
      '--glass': 'rgba(43,43,43,0.75)', '--glass-border': 'rgba(255,255,255,0.12)',
      '--card': '#2b2b2b', '--surface': '#2b2b2b',
      '--shadow': '0 4px 20px rgba(0,0,0,0.4)', '--shadow-lg': '0 8px 40px rgba(0,0,0,0.5)'
    },
    sepia: {
      '--bg': '#f4ecd8', '--bg2': '#efe6d1', '--bg3': '#e8dcc7', '--bg4': '#ddd0bb',
      '--border': 'rgba(91,70,54,0.15)', '--text': '#5b4636', '--text2': '#7d6b5d', '--text3': '#736456',
      '--accent': '#8b6914', '--accent2': '#6b5010',
      '--glass': 'rgba(239,230,209,0.8)', '--glass-border': 'rgba(91,70,54,0.12)',
      '--card': '#efe6d1', '--surface': '#efe6d1',
      '--shadow': '0 8px 24px rgba(91,70,54,0.15)', '--shadow-lg': '0 8px 24px rgba(91,70,54,0.15)'
    },
    warm: {
      '--bg': '#1a1410', '--bg2': '#2a1f18', '--bg3': '#3a2f28', '--bg4': '#4a3f38',
      '--border': 'rgba(212,165,116,0.1)', '--text': '#e8d8c8', '--text2': '#a89888', '--text3': '#93887e',
      '--accent': '#d4a574', '--accent2': '#b08554',
      '--glass': 'rgba(42,31,24,0.75)', '--glass-border': 'rgba(212,165,116,0.12)',
      '--card': '#2a1f18', '--surface': '#2a1f18',
      '--shadow': '0 8px 24px rgba(0,0,0,0.5)', '--shadow-lg': '0 8px 24px rgba(0,0,0,0.5)'
    },
    paper: {
      '--bg': '#fdfcf8', '--bg2': '#faf7f0', '--bg3': '#f0ebe0', '--bg4': '#e0d9c8',
      '--border': 'rgba(80,60,40,0.1)', '--text': '#2c2418', '--text2': '#5a4a38', '--text3': '#7d6e5e',
      '--accent': '#a0522d', '--accent2': '#803818',
      '--glass': 'rgba(250,247,240,0.85)', '--glass-border': 'rgba(80,60,40,0.1)',
      '--card': '#faf7f0', '--surface': '#faf7f0',
      '--shadow': '0 8px 24px rgba(80,60,40,0.12)', '--shadow-lg': '0 8px 24px rgba(80,60,40,0.12)'
    },
    ocean: {
      '--bg': '#0a1628', '--bg2': '#102038', '--bg3': '#182840', '--bg4': '#203050',
      '--border': 'rgba(100,180,255,0.1)', '--text': '#c8dcf0', '--text2': '#8aa8c8', '--text3': '#778ca5',
      '--accent': '#4aa3df', '--accent2': '#2a83bf',
      '--glass': 'rgba(16,32,56,0.75)', '--glass-border': 'rgba(100,180,255,0.12)',
      '--card': '#102038', '--surface': '#102038',
      '--shadow': '0 8px 24px rgba(0,0,0,0.5)', '--shadow-lg': '0 8px 24px rgba(0,0,0,0.5)'
    },
    forest: {
      '--bg': '#0f1a14', '--bg2': '#16241c', '--bg3': '#1e3028', '--bg4': '#263c32',
      '--border': 'rgba(80,200,120,0.1)', '--text': '#c8e0d0', '--text2': '#88b098', '--text3': '#72907e',
      '--accent': '#4aa878', '--accent2': '#2a8858',
      '--glass': 'rgba(22,36,28,0.75)', '--glass-border': 'rgba(80,200,120,0.12)',
      '--card': '#16241c', '--surface': '#16241c',
      '--shadow': '0 8px 24px rgba(0,0,0,0.5)', '--shadow-lg': '0 8px 24px rgba(0,0,0,0.5)'
    },
    midnight: {
      '--bg': '#08090f', '--bg2': '#0e1018', '--bg3': '#141824', '--bg4': '#1c2130',
      '--border': 'rgba(140,160,255,0.1)', '--text': '#dfe3f0', '--text2': '#9aa3bf', '--text3': '#767e97',
      '--accent': '#7c8cff', '--accent2': '#5b6bdd',
      '--glass': 'rgba(14,16,24,0.78)', '--glass-border': 'rgba(140,160,255,0.12)',
      '--card': '#0e1018', '--surface': '#0e1018',
      '--shadow': '0 8px 24px rgba(0,0,0,0.6)', '--shadow-lg': '0 8px 24px rgba(0,0,0,0.6)'
    },
    rose: {
      '--bg': '#fdf2f5', '--bg2': '#fff7f9', '--bg3': '#f8e6ec', '--bg4': '#efd3dd',
      '--border': 'rgba(150,60,90,0.12)', '--text': '#3a1f2b', '--text2': '#6b4353', '--text3': '#8c6774',
      '--accent': '#d6397a', '--accent2': '#b02463',
      '--glass': 'rgba(255,247,249,0.85)', '--glass-border': 'rgba(150,60,90,0.12)',
      '--card': '#fff7f9', '--surface': '#fff7f9',
      '--shadow': '0 8px 24px rgba(150,60,90,0.14)', '--shadow-lg': '0 8px 24px rgba(150,60,90,0.14)'
    }
  };

  var listeners = [];

  function store() {
    try { return global.localStorage; } catch (e) { return null; }
  }

  function read() {
    var s = store();
    if (!s) return DEFAULT_THEME;
    var v = s.getItem(KEY);
    if (!v) v = s.getItem(LEGACY_KEY);       // миграция со старого ключа /desktop
    if (v && ALL.indexOf(v) === -1) v = DEFAULT_THEME;
    return v || DEFAULT_THEME;
  }

  /* Применяем палитру темы к <html> инлайном и ВСЕГДА.

     Раньше переменные ставились на <body> и только если там ещё ничего
     не было. Из-за второго условия вторичный текст не тематизировался:
     48 шаблонов задают --text2/--text3 в собственном :root, рассчитанном
     на тёмный фон, и он оставался #a0aec0 даже в светлой теме — контраст
     2.1:1, текст нечитаем.

     Инлайн на <html> сильнее любого :root из таблицы стилей, поэтому тема
     выигрывает у собственных палитр страниц. Файловый менеджер при этом
     не ломается: его значения объявлены на <body>, а объявление на
     внутреннем элементе наследуемое значение с <html> перебивает. */
  function applyVars(theme) {
    var vars = THEME_VARS[theme];
    var root = document.documentElement;
    if (!vars || !root || !root.style) return;
    /* Свой цвет, выбранный пользователем, главнее темы: акцент из палитры
       не записываем, иначе он затёр бы applyAccent. */
    var custom = false;
    try { custom = !!(readAccent() || '').trim(); } catch (e) { custom = false; }
    for (var k in vars) {
      if (!Object.prototype.hasOwnProperty.call(vars, k)) continue;
      if (custom && (k === '--accent' || k === '--accent2')) continue;
      root.style.setProperty(k, vars[k]);
    }
  }

  /* Старая система «яркости» (portable.js + portable.css) ставит на <body>
     класс theme-dark или theme-light и рисует им отдельную палитру. Она
     ничего не знает про выбранную тему и по умолчанию выбирает тёмную,
     из-за чего страницы на полсекунды показывали верные цвета, а потом
     снова темнели. Синхронизируем её с настоящей темой. */
  /* warm (#1a1410) и ocean (#0a1628) — тёмные палитры: в THEME_VARS у них
     тёмные --bg и светлый текст. Раньше они значились светлыми, и страницы
     с собственной «яркостной» палитрой получали на них светлые поверхности. */
  var LIGHT_BACKGROUNDS = {
    light: 1, sepia: 1, paper: 1, rose: 1
  };
  var syncing = false;
  function syncLegacyBrightness(theme) {
    var b = document.body;
    if (!b || syncing) return;
    syncing = true;
    try {
      var want = LIGHT_BACKGROUNDS[theme] ? 'theme-light' : 'theme-dark';
      if (b.classList.contains('theme-dark') !== (want === 'theme-dark') ||
          b.classList.contains('theme-light') !== (want === 'theme-light')) {
        b.classList.remove('theme-dark', 'theme-light');
        b.classList.add(want);
      }
    } catch (e) { /* нет body — бывает при загрузке в <head> */ }
    syncing = false;
  }

  /* portable.js применяет яркость позже, уже после первой отрисовки.
     Наблюдаем за классом <body>, чтобы тема оставалась главной. */
  function watchLegacyBrightness() {
    if (typeof MutationObserver !== 'function' || !document.body) return;
    var again = function () {
      if (syncing) return;
      syncLegacyBrightness(read());
    };
    try {
      new MutationObserver(again).observe(document.body,
        { attributes: true, attributeFilter: ['class'] });
    } catch (e) { /* наблюдатель недоступен — не критично */ }
  }

  function apply(theme, persist) {
    theme = (theme && ALL.indexOf(theme) !== -1) ? theme : DEFAULT_THEME;
    var b = document.body;
    if (b) {
      ALL.forEach(function (t) { b.classList.remove(t); });
      /* Класс ставим на <body> и ВСЕГДА, включая dark.
         Раньше здесь стояло «if (theme !== 'dark')»: тёмная палитра в
         design.css объявлена как body.dark, а класс для неё не ставился
         никогда — выбор «dark» оставлял страницу светлой. Плюс класс
         попадал на <html> в инлайне webui_assets, где он и не работал. */
      b.classList.add(theme);
      syncLegacyBrightness(theme);
      watchLegacyBrightness();
    }
    if (persist) {
      var s = store();
      if (s) { s.setItem(KEY, theme); s.removeItem(LEGACY_KEY); }
    }
    try { applyVars(theme); } catch (e) { /* цвет — украшение, не критично */ }
    document.documentElement.setAttribute('data-theme', theme);
    listeners.forEach(function (fn) { try { fn(theme); } catch (e) { /* */ } });
    return theme;
  }

  function set(theme) { var t = apply(theme, true); bridgeSend(); frameBroadcast(); return t; }
  function get() { return read(); }
  function init() {
    /* Страницы со своей палитрой (search_all) подключают этот файл только
       ради акцента: тему они ведут сами, и подменять их палитру нельзя. */
    if (global.__INFINITY_ACCENT_ONLY) {
      try { applyAccent(readAccent()); } catch (e) { /* украшение */ }
      return read();
    }
    var t = apply(read(), false);
    // Акцент ставим после темы: apply() мог заполнить пустые переменные.
    try { applyAccent(readAccent()); } catch (e) { /* украшение */ }
    return t;
  }
  function toggle() { return set(get() === 'light' ? 'dark' : 'light'); }

  /* ======================================================================
      Мост между сессиями Electron.
      ----------------------------------------------------------------------
      Страницы одной сессии синхронизируются storage-событием выше. Но
      встроенный браузер живёт в отдельной сессии (persist:browser — нужно
      для Chrome-расширений) и чужого localStorage не видит. Поэтому:
        * любое изменение (set / setAccent) шлём в главный процесс;
        * главный процесс рассылает его остальным окнам ('theme-sync');
        * при старте страницы просим текущее значение ('theme-get') —
          источник истины главное окно, его читает сама оболочка.
      Вне Electron (обычный браузер) мост молча выключен.
      ====================================================================== */
  function bridgeApi() {
    try {
      var api = global.electronAPI;
      return (api && api.isElectron === true) ? api : null;
    } catch (e) { return null; }
  }
  function bridgeSend() {
    var api = bridgeApi();
    if (!api || !api.sendTheme) return;
    try { api.sendTheme({ theme: read(), accent: readAccent() }); } catch (e) { /* не критично */ }
  }
  /* Применяем пришедшее из другой сессии и сохраняем у себя, но НЕ отправляем
     обратно — иначе плохой обмен превратился бы в эхо. */
  function applyRemote(d) {
    if (!d || typeof d !== 'object') return;
    if (!global.__INFINITY_ACCENT_ONLY &&
        d.theme && ALL.indexOf(d.theme) !== -1 && d.theme !== read()) apply(d.theme, true);
    if (typeof d.accent === 'string') {
      var v = normalize(d.accent);
      if (v !== readAccent()) {
        var s = store();
        if (s) { if (v) s.setItem(ACCENT_KEY, v); else s.removeItem(ACCENT_KEY); }
        applyAccent(v);
      }
    }
  }
  function bridgeInit() {
    var api = bridgeApi();
    if (!api) return;
    try { if (api.on) api.on('theme-sync', applyRemote); } catch (e) { /* */ }
    try {
      if (api.themeGet) Promise.resolve(api.themeGet()).then(applyRemote).catch(function () {});
    } catch (e) { /* */ }
  }

  /* ======================================================================
      Живая смена темы внутри ОДНОЙ вкладки (фрейм <-> оболочка).
      ----------------------------------------------------------------------
      storage-событие ниже срабатывает только в ДРУГИХ вкладках: фрейм того
      же документа — не «другая вкладка». Оболочка /desktop красит верхний
      документ, а страницы (агент, поиск, заметки) живут в iframe siteFrame и
      до перезагрузки оставались в старой палитре. Поэтому значение
      рассылаем сами: вниз по всем однопроисходным фреймам и вверх к
      оболочке. Принявшая сторона применяет его, но обратно не рассылает —
      иначе обмен превратился бы в эхо.
      ====================================================================== */
  var MSG_TYPE = 'infinity-theme-sync';

  function payload() { return { theme: read(), accent: readAccent() }; }

  function broadcastDown(node, depth) {
    if (!node || !node.getElementsByTagName || depth > 4) return;
    var list = node.getElementsByTagName('iframe');
    for (var i = 0; i < list.length; i++) {
      var cw = null, sameOrigin = false;
      try { cw = list[i].contentWindow; } catch (e) { continue; }
      if (!cw) continue;
      /* Чужой (внешний) фрейм не получает ничего: адрес читаем только у
         своего происхождения, а postMessage с чужим targetOrigin молча
         отбрасывается браузером. */
      try { sameOrigin = (cw.location.origin === global.location.origin); } catch (e) { }
      if (sameOrigin) {
        try { cw.postMessage({ type: MSG_TYPE, data: payload() }, global.location.origin); } catch (e) { }
      }
      try { broadcastDown(cw.document, depth + 1); } catch (e) { /* чужое содержимое */ }
    }
  }

  function frameBroadcast() {
    try {
      if (global.parent && global.parent !== global) {
        global.parent.postMessage({ type: MSG_TYPE, data: payload() }, '*');
      }
    } catch (e) { /* */ }
    try { broadcastDown(document, 0); } catch (e) { /* */ }
  }

  // Тема сменилась в оболочке/фрейме этой же вкладки — подхватываем.
  global.addEventListener('message', function (e) {
    var d = e && e.data;
    if (!d || d.type !== MSG_TYPE) return;
    applyRemote(d.data);
  });

  // Тема или акцент сменились в другой вкладке — подхватываем.
  global.addEventListener('storage', function (e) {
    if (!global.__INFINITY_ACCENT_ONLY && (e.key === KEY || e.key === null)) apply(read(), false);
    if (e.key === ACCENT_KEY || e.key === null) {
      try { applyAccent(readAccent()); } catch (err) { /* */ }
    }
  });

  /* ======================================================================
     Акцентный цвет пользователя.
     ----------------------------------------------------------------------
     Темы задают --accent/--accent2/--accent-glow/--accent-light каждый
     по-своему, поэтому свой цвет мы ставим инлайном на <html>: инлайн
     сильнее и :root, и классов темы на <body> — значит перекрывает их
     на всех страницах, включая /desktop с его собственными переменными.

     Производные (потемнение, свечение, подложка) считаем из одного
     исходного цвета, иначе интерфейс рассыпался бы на несогласованных
     оттенках.
     ====================================================================== */
  var ACCENT_KEY = 'infinity_accent';

  var PRESETS = [
    { name: 'Сиреневый', hex: '#6366f1' },
    { name: 'Индиго',    hex: '#4f46e5' },
    { name: 'Синий',     hex: '#2563eb' },
    { name: 'Лазурный',  hex: '#0891b2' },
    { name: 'Изумруд',   hex: '#059669' },
    { name: 'Лаймовый',  hex: '#65a30d' },
    { name: 'Янтарный',  hex: '#d97706' },
    { name: 'Оранжевый', hex: '#ea580c' },
    { name: 'Розовый',   hex: '#db2777' },
    { name: 'Красный',   hex: '#dc2626' }
  ];

  function hex2rgb(h) {
    h = String(h || '').trim().replace('#', '');
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    if (!/^[0-9a-fA-F]{6}$/.test(h)) return null;
    return {
      r: parseInt(h.slice(0, 2), 16),
      g: parseInt(h.slice(2, 4), 16),
      b: parseInt(h.slice(4, 6), 16)
    };
  }

  function rgba(c, a) { return 'rgba(' + c.r + ',' + c.g + ',' + c.b + ',' + a + ')'; }

  /* Осветление/затемнение: amount > 0 — светлее, < 0 — темнее. */
  function shift(c, amount) {
    function f(v) {
      var n = amount >= 0 ? v + (255 - v) * amount : v * (1 + amount);
      return Math.max(0, Math.min(255, Math.round(n)));
    }
    return { r: f(c.r), g: f(c.g), b: f(c.b) };
  }

  function rgb2hex(c) {
    function h(v) { var s = v.toString(16); return s.length === 1 ? '0' + s : s; }
    return '#' + h(c.r) + h(c.g) + h(c.b);
  }

  function normalize(hex) {
    var c = hex2rgb(hex);
    return c ? rgb2hex(c) : '';
  }

  function readAccent() {
    var s = store();
    if (!s) return '';
    return normalize(s.getItem(ACCENT_KEY) || '');
  }

  /* Ставит акцент на <html> И на <body>. Пустое значение — снимает
     переопределение и возвращает цвета темы.

     Только <html> недостаточно: portable.css (.theme-light/.theme-dark) и
     страницы со своей палитрой (search_all: body.dark/body.sepia) объявляют
     --accent на <body>, а объявление на элементе сильнее наследуемого
     значения с <html>. Именно из-за этого выбранный цвет не доходил до
     рабочего стола и поиска. Инлайн на <body> выигрывает у их правил. */
  function applyAccent(hex) {
    var root = document.documentElement;
    var c = hex2rgb(hex);
    var targets = [root];
    if (document.body) targets.push(document.body);
    targets.forEach(function (el) {
      if (!el || !el.style) return;
      ['--accent', '--accent2', '--accent-glow', '--accent-light'].forEach(function (k) {
        el.style.removeProperty(k);
      });
    });
    if (!c) {
      /* Свой акцент снят — возвращаем акценты темы.
         Раньше здесь был просто return, и удалённые выше --accent/--accent2
         пропадали совсем: --accent ещё находился в палитре страницы, а
         --accent2 не имел запасного значения. Из-за этого градиенты вида
         linear-gradient(var(--accent), var(--accent2)) становились
         невалидными, фон .taskbar-start становился none, а белый текст
         на нём — невидимым.
         В режиме accent-only палитру страницы не трогаем вовсе: у неё своя. */
      if (!global.__INFINITY_ACCENT_ONLY) {
        try { applyVars(read()); } catch (e) { }
      }
      return '';
    }
    hex = rgb2hex(c);
    var dark = shift(c, -0.22);          // второй тон — как в темах
    targets.forEach(function (el) {
      if (!el || !el.style) return;
      el.style.setProperty('--accent', hex);
      el.style.setProperty('--accent2', rgb2hex(dark));
      el.style.setProperty('--accent-glow', rgba(c, 0.25));
      el.style.setProperty('--accent-light', rgba(c, 0.14));
    });
    return hex;
  }

  function setAccent(hex) {
    var val = normalize(hex);
    var s = store();
    if (s) {
      if (val) { s.setItem(ACCENT_KEY, val); }
      else { s.removeItem(ACCENT_KEY); }
    }
    applyAccent(val);
    bridgeSend();
    frameBroadcast();
    return val;
  }

  function resetAccent() { return setAccent(''); }

  global.InfinityTheme = {
    get: get, set: set, init: init, toggle: toggle,
    themes: ALL, vars: THEME_VARS,
    onChange: function (fn) { listeners.push(fn); },
    accent: {
      get: readAccent,
      set: setAccent,
      reset: resetAccent,
      presets: PRESETS,
      /* Список «свой цвет» → HEX без сжатия: принимает и #abc, и #aabbcc. */
      list: function () {
        var a = [];
        for (var i = 0; i < arguments.length; i++) {
          var v = normalize(arguments[i]);
          if (v && a.indexOf(v) === -1) a.push(v);
        }
        return a;
      }
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { init(); bridgeInit(); }, { once: true });
  } else {
    init();
    bridgeInit();
  }
})(window);
