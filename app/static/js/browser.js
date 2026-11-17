/* ============================================================================
   Встроенный браузер Infinity (страница /browser, окно обёртки Electron).
   ----------------------------------------------------------------------------
   Полноценный мини-браузер на <webview>: вкладки, навигация, история, закладки,
   загрузки, инкогнито. Оформление и тема — как у всего приложения.
   Работает только при window.electronAPI (иначе страницу просто не покажут).
   ========================================================================== */
(function () {
  'use strict';

  var api = window.electronAPI;
  if (!api || api.isElectron !== true) return;   // вне оболочки — нечего делать

  // Стартовая страница — локальная (/browser/start): работает без сети и даёт
  // быстрые ссылки на модули. В отдельном окне без http-происхождения
  // (file://) остаётся внешний поисковик.
  var HOME = (location.protocol === 'http:' || location.protocol === 'https:')
    ? location.origin + '/browser/start'
    : 'https://duckduckgo.com/';
  var SEARCH = 'https://duckduckgo.com/?q=';

  // Хранилище: закладки и история в localStorage окна браузера. Ключи с
  // префиксом, чтобы не пересекаться с темой приложения.
  var LS_BOOK = 'infinity_browser_bookmarks';
  var LS_HIST = 'infinity_browser_history';
  var LS_SESSION = 'infinity_browser_session';
  // Сессия расширений и обычных вкладок должна совпадать: extensions.js
  // загружает расширения именно в persist:browser.
  var PARTITION = 'persist:browser';

  var $ = function (id) { return document.getElementById(id); };
  var elTabs = $('bbTabs'), elViews = $('bbViews'), elUrl = $('bbUrl');
  var elStar = $('bbStar'), elBookmarks = $('bbBookmarks');
  var elBack = $('bbBack'), elFwd = $('bbFwd'), elLock = $('bbOmniLock');
  var elDownloads = $('bbDownloads');
  var elHistoryPanel = $('bbHistoryPanel'), elHistoryList = $('bbHistoryList');
  var elDownloadsPanel = $('bbDownloadsPanel'), elDownloadsList = $('bbDownloadsList');
  var elExtPanel = $('bbExtPanel'), elExtList = $('bbExtList');
  var elPluginsPanel = $('bbPluginsPanel'), elPluginsList = $('bbPluginsList');
  var elPasswordsPanel = $('bbPasswordsPanel'), elPasswordsList = $('bbPasswordsList');
  var elExtIcons = $('bbExtIcons');
  var elHistorySearch = $('bbHistorySearch');
  var elMenu = $('bbMenu');

  var tabs = [];          // { id, webview, tabEl, favEl, titleEl, url, incognito, loading }
  var activeId = null;
  var seq = 1;
  var downloads = {};     // id -> { name, url, received, total, state, path }
  // Единое открытое всплывающее меню (контекст вкладки / закладки / папки).
  // Держим ровно одно: открытие нового закрывает предыдущее.
  var anyMenu = null;
  // Узел закладки, который сейчас тащат мышью (drag-and-drop в папку).
  var dragNode = null;

  /* ---------- Хранилище ---------- */
  function loadJSON(key, fallback) {
    try { var v = JSON.parse(localStorage.getItem(key)); return v || fallback; }
    catch (e) { return fallback; }
  }
  function saveJSON(key, val) {
    try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) { /* квота */ }
  }
  var bookmarks = normalizeBookmarks(loadJSON(LS_BOOK, []));
  var history = loadJSON(LS_HIST, []);

  /* ---------- Сессия вкладок ----------
     Вкладки живут в памяти, а окно браузера при смене вида (половина/полный
     экран/отдельное окно) пересоздаётся. Без сохранения все открытые сайты
     терялись. Инкогнито намеренно не сохраняем. */
  var restoringSession = false;
  function saveSession() {
    if (restoringSession) return;
    try {
      var list = tabs.filter(function (t) { return !t.incognito; }).map(function (t) {
        return { url: t.url || '', title: t.titleEl ? t.titleEl.textContent : '' };
      });
      var act = -1;
      var active = activeTab();
      if (active && !active.incognito) {
        act = list.findIndex(function (x) { return x.url === active.url; });
      }
      localStorage.setItem(LS_SESSION, JSON.stringify({
        tabs: list, active: act, ts: Date.now()
      }));
    } catch (e) { /* квота/приватный режим */ }
  }
  function restoreSession() {
    var s = loadJSON(LS_SESSION, null);
    if (!s || !s.tabs || !s.tabs.length) return false;
    // Старше недели не восстанавливаем: это уже не «текущая работа».
    if (s.ts && Date.now() - s.ts > 7 * 24 * 3600 * 1000) return false;
    restoringSession = true;
    try {
      s.tabs.forEach(function (item) {
        createTab({ url: item.url || HOME, activate: false });
      });
      var a = (typeof s.active === 'number' && s.active >= 0 && s.active < tabs.length)
        ? s.active : tabs.length - 1;
      if (tabs[a]) activateTab(tabs[a].id);
    } catch (e) {
      restoringSession = false;
      return false;
    }
    restoringSession = false;
    return tabs.length > 0;
  }

  /* ---------- Утилиты URL ---------- */
  function normalizeUrl(input) {
    var s = String(input || '').trim();
    if (!s) return '';
    // Уже URL со схемой.
    if (/^(https?|file|ftp):\/\//i.test(s)) return s;
    // Похоже на домен: example.com / sub.example.com/path
    if (/^[^\s.]+\.[^\s]{2,}(\/|$|\?)/.test(s) && !/\s/.test(s)) return 'https://' + s;
    // localhost с портом
    if (/^localhost(:\d+)?(\/|$)/i.test(s)) return 'http://' + s;
    // Иначе — поиск.
    return SEARCH + encodeURIComponent(s);
  }
  function prettyUrl(url) {
    try {
      var u = new URL(url);
      return u.href;
    } catch (e) { return url; }
  }
  function isSecure(url) {
    return /^https:/i.test(url || '');
  }
  function faviconUrl(url) {
    try {
      var u = new URL(url);
      return u.origin + '/favicon.ico';
    } catch (e) { return ''; }
  }
  function titleFromUrl(url) {
    try { return new URL(url).hostname; } catch (e) { return url; }
  }

  /* ---------- Вкладки ---------- */
  function activeTab() {
    for (var i = 0; i < tabs.length; i++) if (tabs[i].id === activeId) return tabs[i];
    return null;
  }

  function createTab(opts) {
    opts = opts || {};
    var id = 't' + (seq++);
    var incognito = !!opts.incognito;
    var url = opts.url || HOME;

    // Вкладка в полосе.
    var tabEl = document.createElement('div');
    tabEl.className = 'bb-tab' + (incognito ? ' incognito' : '');
    tabEl.dataset.id = id;
    tabEl.innerHTML =
      '<span class="bb-tab-fav-ph"></span>' +
      '<span class="bb-tab-title">Новая вкладка</span>' +
      '<button class="bb-tab-mute" title="Звук вкладки" hidden>' +
        '<svg viewBox="0 0 16 16"><path d="M3 6h2.5L9 3.5v9L5.5 10H3z" fill="currentColor"/><path d="M11 6c.9 1 .9 3 0 4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>' +
      '</button>' +
      '<button class="bb-tab-close" title="Закрыть вкладку">' +
        '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>' +
      '</button>';
    elTabs.appendChild(tabEl);

    // Область под страницу: контейнер со спиннером + сам webview.
    var viewWrap = document.createElement('div');
    viewWrap.className = 'bb-views-slot';
    viewWrap.dataset.id = id;
    viewWrap.style.cssText = 'position:absolute;inset:0;';

    var spinner = document.createElement('div');
    spinner.className = 'bb-tab-loading';
    spinner.innerHTML = '<i></i><span>Загрузка…</span>';
    viewWrap.appendChild(spinner);

    var webview = document.createElement('webview');
    webview.setAttribute('allowpopups', '');
    // contextIsolation для гостя + preload с мостом выделения текста.
    webview.setAttribute('webpreferences', 'contextIsolation=yes');
    // Инкогнито: непостоянный раздел (partition без persist:) — куки, кэш и
    // localStorage живут только в памяти этого webview и не попадают в историю.
    // Обычные вкладки обязаны жить в той же persist-сессии, что и оболочка:
    // иначе расширения и их content scripts сюда не внедряются, а сайты
    // видят чужой (пустой) storage и ругаются на отсутствие JavaScript.
    if (incognito) {
      webview.setAttribute('partition', 'incognito-' + id);
      webview.setAttribute('incognito', '');
    } else {
      webview.setAttribute('partition', PARTITION);
    }
    if (api.guestPreload) {
      try { webview.setAttribute('preload', api.guestPreload); } catch (e) {}
    }
    webview.src = url;
    webview.style.display = 'none';
    viewWrap.appendChild(webview);
    elViews.appendChild(viewWrap);

    var tab = {
      id: id, url: url, incognito: incognito,
      webview: webview, tabEl: tabEl, viewWrap: viewWrap,
      favEl: tabEl.querySelector('.bb-tab-fav-ph'),
      titleEl: tabEl.querySelector('.bb-tab-title'),
      loading: true, history: [], hIndex: -1
    };
    tabs.push(tab);
    wireWebview(tab);
    wireTabEl(tab);
    if (opts.activate !== false) activateTab(id);
    saveSession();
    return tab;
  }

  function closeTab(id) {
    var idx = -1;
    for (var i = 0; i < tabs.length; i++) if (tabs[i].id === id) { idx = i; break; }
    if (idx < 0) return;
    var tab = tabs[idx];
    try { tab.viewWrap.remove(); } catch (e) { /* уже удалён */ }
    try { tab.tabEl.remove(); } catch (e) {}
    tabs.splice(idx, 1);
    if (!tabs.length) {
      // Закрыли последнюю вкладку — закрываем окно браузера.
      try { localStorage.removeItem(LS_SESSION); } catch (e) {}
      if (api.closeBrowser) api.closeBrowser();
      return;
    }
    if (activeId === id) {
      var next = tabs[Math.min(idx, tabs.length - 1)];
      activateTab(next.id);
    }
    saveSession();
  }

  /* Лента вкладок прокручивается по горизонтали, когда они не помещаются.
     Выбранная вкладка может оказаться за краем (после Ctrl+T, закрытия
     соседней или восстановления сессии) — подводим её в видимую область. */
  function revealTab(tabEl) {
    if (!tabEl || !elTabs) return;
    /* Прямоугольники, а не offsetLeft: у ленты нет position:relative,
       поэтому offsetParent у вкладки — вышестоящий блок, и offsetLeft
       смещён на отступы полосы. */
    var tRect = tabEl.getBoundingClientRect();
    var cRect = elTabs.getBoundingClientRect();
    var delta = 0;
    if (tRect.left < cRect.left) delta = tRect.left - cRect.left - 4;
    else if (tRect.right > cRect.right) delta = tRect.right - cRect.right + 4;
    if (!delta) return;
    var target = Math.max(0, elTabs.scrollLeft + delta);
    if (elTabs.scrollTo) elTabs.scrollTo({ left: target, behavior: 'smooth' });
    else elTabs.scrollLeft = target;
  }

  function activateTab(id) {
    activeId = id;
    hideSelMenu();
    hideWordPop();
    /* Оверлей «режим чтения»/перевода лежит поверх elViews и не принадлежит
       ни одной вкладке: без его закрытия переключение вкладок выглядело бы
       сломанным — под оверлеем сменилась страница, а сверху остался старый
       текст. */
    closeReader();
    tabs.forEach(function (t) {
      var on = t.id === id;
      t.tabEl.classList.toggle('active', on);
      t.viewWrap.style.display = on ? '' : 'none';
      if (on) revealTab(t.tabEl);
    });
    var t = activeTab();
    if (t) syncToolbar(t);
    saveSession();
  }

  /* ---------- События webview ---------- */
  function wireWebview(tab) {
    var wv = tab.webview;

    // Показать страницу после первого кадра, убрать спиннер.
    wv.addEventListener('did-finish-load', function () {
      tab.loading = false;
      wv.style.display = '';
      var sp = tab.viewWrap.querySelector('.bb-tab-loading');
      if (sp) sp.remove();
      syncToolbar(tab);
      autoFillPasswords(tab);
    });
    wv.addEventListener('dom-ready', function () {
      tab.loading = false;
      wv.style.display = '';
      var sp = tab.viewWrap.querySelector('.bb-tab-loading');
      if (sp) sp.remove();
    });
    // Ошибки загрузки (нет сети и т.п.) — тоже убираем спиннер.
    wv.addEventListener('did-fail-load', function () {
      tab.loading = false;
      wv.style.display = '';
      var sp = tab.viewWrap.querySelector('.bb-tab-loading');
      if (sp) sp.remove();
    });

    function onNav(url) {
      tab.url = url;
      pushHistory(tab, url);
      if (tab.id === activeId) syncToolbar(tab);
      updateStar();
      saveSession();
    }
    wv.addEventListener('did-navigate', function (e) { onNav(e.url); });
    // SPA-навигация без перезагрузки (hash/router) — обновляем историю вкладки.
    wv.addEventListener('did-navigate-in-page', function (e) {
      tab.url = e.url;
      if (tab.id === activeId) syncToolbar(tab);
    });

    wv.addEventListener('page-title-updated', function (e) {
      tab.titleEl.textContent = e.title || titleFromUrl(tab.url);
      saveSession();
    });
    // Звук со страницы — показываем кнопку mute на вкладке.
    wv.addEventListener('media-started-playing', function () {
      tab.audible = true;
      updateMuteBtn(tab);
    });
    wv.addEventListener('media-paused', function () {
      tab.audible = false;
      updateMuteBtn(tab);
    });
    wv.addEventListener('page-favicon-updated', function (e) {
      var favs = e.favicons && e.favicons.length ? e.favicons : [faviconUrl(tab.url)];
      if (favs && favs[0]) {
        var img = new Image();
        img.className = 'bb-tab-fav';
        img.src = favs[0];
        img.onerror = function () { img.remove(); };
        if (tab.favEl && tab.favEl.parentNode) {
          tab.favEl.replaceWith(img);
          tab.favEl = img;
        }
      }
    });
    // Навигация внутри вкладки (клик по ссылке без target=_blank) ведёт
    // сам webview. Ссылки с target=_blank / window.open перехватывает
    // setWindowOpenHandler в main.js и присылает 'browser-open-tab' —
    // поэтому здесь отдельный обработчик new-window не нужен (иначе
    // вкладка открылась бы дважды).

    // Сообщения от guest-preload: выделение текста и слово по двойному клику.
    // ipcRenderer.sendToHost из страницы приходит сюда как 'ipc-message'.
    wv.addEventListener('ipc-message', function (e) {
      if (e.channel === 'bb-selection') {
        onGuestSelection(tab, e.args && e.args[0] || {});
      } else if (e.channel === 'bb-word') {
        onGuestWord(tab, e.args && e.args[0] || {});
      } else if (e.channel === 'bb-selection-clear') {
        hideSelMenu();
        hideWordPop();
      } else if (e.channel === 'bb-login-captured') {
        saveCapturedLogin(tab, e.args && e.args[0] || {});
      }
    });

    // Запасной путь без guest-preload: Electron сам присылает context-menu с
    // выделенным текстом и координатами. Нужен, если preload не подцепился
    // (тогда всплывашка по выделению мышью вообще не появлялась).
    wv.addEventListener('context-menu', function (e) {
      var p = e.params || {};
      var sel = String(p.selectionText || '').trim();
      if (!sel) return;
      onGuestSelection(tab, { text: sel, x: p.x || 0, y: p.y || 0 });
    });
  }

  /* ---------- События элемента вкладки ---------- */
  function wireTabEl(tab) {
    tab.tabEl.addEventListener('click', function (e) {
      if (e.target.closest('.bb-tab-close')) {
        closeTab(tab.id);
        return;
      }
      if (e.target.closest('.bb-tab-mute')) {
        e.stopPropagation();
        toggleTabMute(tab);
        return;
      }
      activateTab(tab.id);
    });
    // Средняя кнопка мыши — закрыть вкладку.
    tab.tabEl.addEventListener('auxclick', function (e) {
      if (e.button === 1) { e.preventDefault(); closeTab(tab.id); }
    });
    // Правая кнопка — своё меню (в frameless-окне системное смотрится чужим).
    tab.tabEl.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      tabContextMenu(tab, e.clientX, e.clientY);
    });
  }

  /* Звук: webview умеет setAudioMuted; иконка на вкладке видна только когда
     со страницы реально идёт звук (событие media-started-playing). */
  function toggleTabMute(tab) {
    var muted;
    try { muted = tab.webview.isAudioMuted(); } catch (e) { return; }
    try { tab.webview.setAudioMuted(!muted); } catch (e) { return; }
    updateMuteBtn(tab);
  }
  function updateMuteBtn(tab) {
    var btn = tab.tabEl.querySelector('.bb-tab-mute');
    if (!btn) return;
    var muted = false;
    try { muted = tab.webview.isAudioMuted(); } catch (e) {}
    var audible = tab.audible || muted;
    btn.hidden = !audible;
    btn.classList.toggle('muted', muted);
    btn.title = muted ? 'Включить звук' : 'Выключить звук';
  }

  function tabContextMenu(tab, x, y) {
    var muted = false, pinned = !!tab.pinned;
    try { muted = tab.webview.isAudioMuted(); } catch (e) {}
    var items = [
      { label: 'Перезагрузить', kbd: 'Ctrl+R', onClick: function () { tab.webview.reload(); } },
      { label: 'Открыть копию вкладки', onClick: function () { createTab({ url: tab.url, incognito: tab.incognito }); } },
      { sep: true },
      { label: pinned ? 'Открепить вкладку' : 'Закрепить вкладку',
        onClick: function () {
          tab.pinned = !tab.pinned;
          tab.tabEl.classList.toggle('pinned', tab.pinned);
          // Закреплённые держим слева — как в Chrome.
          if (tab.pinned) elTabs.insertBefore(tab.tabEl, elTabs.firstChild);
        } },
      { label: muted ? 'Включить звук' : 'Выключить звук', disabled: !(tab.audible || muted),
        onClick: function () { toggleTabMute(tab); } },
      { sep: true },
      { label: 'Спросить ИИ о странице', kbd: 'Ctrl+Shift+Q',
        disabled: tab.id !== activeId, onClick: askAiAboutPage },
      { label: 'Печать в PDF', kbd: 'Ctrl+P', disabled: tab.id !== activeId, onClick: printPdf },
      { sep: true },
      { label: 'Закрыть вкладку', kbd: 'Ctrl+W', danger: true, onClick: function () { closeTab(tab.id); } },
      { label: 'Закрыть остальные', danger: true, disabled: tabs.length < 2, onClick: function () {
        tabs.slice().forEach(function (t) { if (t.id !== tab.id) closeTab(t.id); });
      } },
    ];
    showContextMenu(items, x, y);
  }

  /* ---------- История вкладки (кнопки назад/вперёд) ---------- */
  function pushHistory(tab, url) {
    if (!url) return;
    // Обрезаем «вперёд»-часть и добавляем запись.
    tab.history = tab.history.slice(0, tab.hIndex + 1);
    if (tab.history[tab.history.length - 1] !== url) tab.history.push(url);
    tab.hIndex = tab.history.length - 1;
    // Общая история окна (кроме инкогнито).
    if (!tab.incognito) addGlobalHistory(url);
  }

  function addGlobalHistory(url) {
    var title = titleFromUrl(url);
    history.unshift({ url: url, title: title, ts: Date.now() });
    if (history.length > 500) history.length = 500;
    saveJSON(LS_HIST, history);
  }

  function nav(tab, delta) {
    if (!tab) return;
    var ni = tab.hIndex + delta;
    if (ni < 0 || ni >= tab.history.length) return;
    tab.hIndex = ni;
    tab.webview.loadURL(tab.history[ni]);
    if (tab.id === activeId) syncToolbar(tab);
  }

  /* ---------- Синхронизация тулбара с активной вкладкой ---------- */
  function syncToolbar(tab) {
    if (!tab) return;
    // Адресную строку не перезаписываем, пока пользователь в ней печатает.
    if (document.activeElement !== elUrl) elUrl.value = prettyUrl(tab.url);
    var secure = isSecure(tab.url);
    elLock.className = 'bb-omni-lock ' + (secure ? 'secure' : 'insecure');
    elLock.title = secure ? 'Соединение защищено' : 'Соединение не защищено';
    try {
      elBack.disabled = !tab.webview.canGoBack();
      elFwd.disabled = !tab.webview.canGoForward();
    } catch (e) { /* webview ещё не готов */ }
  }

  function updateStar() {
    var t = activeTab();
    if (!t) return;
    var on = isBookmarked(t.url);
    elStar.classList.toggle('on', on);
    elStar.title = on ? 'Убрать из закладок' : 'Добавить в закладки';
  }

  /* ---------- Закладки ----------
     Дерево: {title, url} для закладки, {title, children:[]} для папки.
     Папки раскрываются по клику; закладку можно перетащить на папку
     (drag-and-drop), переименовать или удалить через правую кнопку. */
  function normalizeBookmarks(raw) {
    // Старый формат был плоским массивом закладок — он должен продолжать
    // работать, поэтому просто переносим записи как есть.
    if (!Array.isArray(raw)) return [];
    var out = [];
    raw.forEach(function (b) {
      if (!b || typeof b !== 'object') return;
      if (Array.isArray(b.children)) {
        out.push({ title: String(b.title || 'Папка'), children: normalizeBookmarks(b.children) });
      } else if (b.url) {
        out.push({ url: String(b.url), title: String(b.title || titleFromUrl(b.url)) });
      }
    });
    return out;
  }

  function findBookmark(url) {
    var found = null;
    (function walk(list) {
      for (var i = 0; i < list.length; i++) {
        if (found) return;
        var b = list[i];
        if (b.children) walk(b.children);
        else if (b.url === url) { found = b; return; }
      }
    })(bookmarks);
    return found;
  }
  function isBookmarked(url) { return !!findBookmark(url); }

  function toggleBookmark() {
    var t = activeTab();
    if (!t) return;
    var url = t.url;
    var found = findBookmark(url);
    if (found) {
      removeBookmarkNode(found);
    } else {
      bookmarks.push({ url: url, title: t.titleEl.textContent || titleFromUrl(url) });
    }
    saveJSON(LS_BOOK, bookmarks);
    renderBookmarks();
    updateStar();
  }

  // Ссылки на родителя в узле нет, поэтому удаление/поиск родителя идут
  // рекурсивным обходом дерева.
  function removeBookmarkNode(node) {
    return (function walk(list) {
      for (var i = 0; i < list.length; i++) {
        if (list[i] === node) { list.splice(i, 1); return true; }
        if (list[i].children && walk(list[i].children)) return true;
      }
      return false;
    })(bookmarks);
  }

  function containsNode(folder, node) {
    var hit = false;
    (function walk(list) {
      (list || []).forEach(function (b) {
        if (hit) return;
        if (b === node) { hit = true; return; }
        if (b.children) walk(b.children);
      });
    })(folder.children);
    return hit;
  }

  /* Диалог ввода строки. window.prompt в рендере Electron не реализован,
     поэтому рисуем свой маленький оверлей. Возвращает Promise со строкой
     или null, если отменили. */
  function askText(title, def) {
    return new Promise(function (resolve) {
      var back = document.createElement('div');
      back.style.cssText = 'position:fixed;inset:0;z-index:95;background:rgba(0,0,0,.45);display:grid;place-items:center;';
      var box = document.createElement('div');
      box.className = 'bb-ctx';
      box.style.position = 'static';
      box.style.minWidth = '300px';
      var h = document.createElement('div');
      h.style.cssText = 'padding:6px 10px 8px;font-size:12.5px;color:var(--text,#e6eaf0);';
      h.textContent = title;
      var inp = document.createElement('input');
      inp.type = 'text';
      inp.value = def || '';
      inp.spellcheck = false;
      inp.autocomplete = 'off';
      inp.style.cssText = 'width:100%;height:30px;margin:0 0 10px;padding:0 10px;border:1px solid var(--border,#262d39);border-radius:7px;background:var(--bg,#0f1419);color:var(--text,#e6eaf0);font-size:12.5px;outline:none;box-sizing:border-box;';
      var row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:6px;justify-content:flex-end;';
      var mkBtn = function (label, primary) {
        var b = document.createElement('button');
        b.textContent = label;
        b.style.cssText = 'padding:6px 14px;border:0;border-radius:7px;font-size:12.5px;cursor:pointer;' +
          (primary ? 'background:var(--accent,#5b8cff);color:#fff;' : 'background:var(--bg3,#1d232d);color:var(--text-2,#9aa5b4);');
        return b;
      };
      var ok = mkBtn('ОК', true), cancel = mkBtn('Отмена', false);
      var done = function (val) {
        document.removeEventListener('keydown', onKey, true);
        back.remove();
        resolve(val);
      };
      ok.addEventListener('click', function () { done(inp.value.trim() || null); });
      cancel.addEventListener('click', function () { done(null); });
      back.addEventListener('mousedown', function (e) { if (e.target === back) done(null); });
      var onKey = function (e) {
        if (e.key === 'Enter') { e.preventDefault(); done(inp.value.trim() || null); }
        else if (e.key === 'Escape') { e.preventDefault(); done(null); }
      };
      document.addEventListener('keydown', onKey, true);
      row.appendChild(cancel); row.appendChild(ok);
      box.appendChild(h); box.appendChild(inp); box.appendChild(row);
      back.appendChild(box);
      document.body.appendChild(back);
      inp.focus(); inp.select();
    });
  }

  function addFolder() {
    askText('Название новой папки:', 'Новая папка').then(function (name) {
      if (!name) return;
      bookmarks.push({ title: name, children: [] });
      saveJSON(LS_BOOK, bookmarks);
      renderBookmarks();
      toast('Папка «' + name + '» создана', 'ok');
    });
  }

  function renameBookmark(b) {
    askText('Новое название:', b.title || '').then(function (name) {
      if (!name) return;
      b.title = name;
      saveJSON(LS_BOOK, bookmarks);
      renderBookmarks();
    });
  }
  function renderBookmarks() {
    elBookmarks.innerHTML = '';
    bookmarks.forEach(function (b) { elBookmarks.appendChild(bookmarkEl(b)); });
    var add = document.createElement('button');
    add.className = 'bb-bookmarks-add';
    add.textContent = '+ Папка';
    add.title = 'Создать папку закладок';
    add.addEventListener('click', function (e) { e.stopPropagation(); addFolder(); });
    elBookmarks.appendChild(add);
  }

  function bookmarkEl(b) {
    var btn = document.createElement('button');
    btn.className = 'bb-bookmark';
    btn.draggable = true;
    var isFolder = !!b.children;
    btn.innerHTML =
      '<span class="bb-tab-fav-ph" style="width:14px;height:14px"></span>' +
      '<span>' + escapeHtml(b.title || (isFolder ? 'Папка' : titleFromUrl(b.url))) + '</span>';
    if (isFolder) {
      var caret = document.createElement('span');
      caret.className = 'bb-bookmark-open';
      caret.innerHTML = ICON_CARET;
      caret.title = 'Раскрыть папку';
      caret.addEventListener('click', function (e) {
        e.stopPropagation();
        openFolderMenu(b, caret);
      });
      btn.appendChild(caret);
      btn.title = (b.title || 'Папка') + ' — раскрыть';
      btn.addEventListener('click', function (e) {
        if (e.target.closest('.bb-bookmark-open')) return;
        openFolderMenu(b, btn);
      });
    } else {
      btn.title = b.url;
      btn.addEventListener('click', function () {
        var t = activeTab();
        if (t) t.webview.loadURL(b.url);
      });
    }
    wireBookmarkDnd(btn, b);
    btn.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      e.stopPropagation();
      bookmarkContextMenu(b, e.clientX, e.clientY);
    });
    return btn;
  }

  function bookmarkContextMenu(b, x, y) {
    var items = [];
    if (!b.children) {
      items.push({ label: 'Открыть', onClick: function () {
        var t = activeTab(); if (t) t.webview.loadURL(b.url);
      } });
      items.push({ label: 'Открыть в новой вкладке', onClick: function () {
        createTab({ url: b.url });
      } });
      items.push({ sep: true });
    }
    items.push({ label: 'Переименовать', onClick: function () { renameBookmark(b); } });
    items.push({ label: 'Удалить', danger: true, onClick: function () {
      removeBookmarkNode(b);
      saveJSON(LS_BOOK, bookmarks);
      renderBookmarks();
      updateStar();
      toast('Удалено: ' + (b.title || 'без названия'), 'ok');
    } });
    showContextMenu(items, x, y);
  }

  function openFolderMenu(folder, anchorEl) {
    closeAnyMenu();
    var menu = document.createElement('div');
    menu.className = 'bb-bm-menu';
    var path = document.createElement('div');
    path.className = 'bb-bm-path';
    path.textContent = folder.title || 'Папка';
    menu.appendChild(path);
    if (!folder.children || !folder.children.length) {
      var empty = document.createElement('div');
      empty.className = 'bb-bm-path';
      empty.textContent = 'Папка пуста';
      menu.appendChild(empty);
    } else {
      folder.children.forEach(function (b) { menu.appendChild(bookmarkEl(b)); });
    }
    document.body.appendChild(menu);
    var r = anchorEl.getBoundingClientRect();
    var top = Math.min(r.bottom + 4, window.innerHeight - menu.offsetHeight - 8);
    var left = Math.min(r.left, window.innerWidth - menu.offsetWidth - 8);
    menu.style.top = Math.max(8, top) + 'px';
    menu.style.left = Math.max(8, left) + 'px';
    anyMenu = menu;
  }

  function wireBookmarkDnd(el, node) {
    el.addEventListener('dragstart', function (e) {
      dragNode = node;
      el.classList.add('dragging');
      try { e.dataTransfer.setData('text/plain', node.url || node.title || ''); } catch (err) {}
      e.dataTransfer.effectAllowed = 'move';
    });
    el.addEventListener('dragend', function () {
      el.classList.remove('dragging');
      dragNode = null;
      document.querySelectorAll('.bb-bookmark.drop-target').forEach(function (x) {
        x.classList.remove('drop-target');
      });
    });
    // Цель сброса — только папки: перетащили закладку на папку, она уехала внутрь.
    if (!node.children) return;
    el.addEventListener('dragover', function (e) {
      if (!dragNode || dragNode === node) return;
      e.preventDefault();
      el.classList.add('drop-target');
    });
    el.addEventListener('dragleave', function () { el.classList.remove('drop-target'); });
    el.addEventListener('drop', function (e) {
      e.preventDefault();
      e.stopPropagation();
      el.classList.remove('drop-target');
      moveBookmarkInto(dragNode, node);
      dragNode = null;
    });
  }

  function moveBookmarkInto(node, folder) {
    if (!node || !folder || !folder.children) return;
    if (node === folder) return;
    // Папку нельзя перенести в собственное поддерево — получился бы цикл.
    if (containsNode(folder, node)) { toast('Нельзя переместить папку внутрь себя', 'err'); return; }
    if (!removeBookmarkNode(node)) return;
    folder.children.push(node);
    saveJSON(LS_BOOK, bookmarks);
    renderBookmarks();
    toast('Перемещено в «' + (folder.title || 'папку') + '»', 'ok');
  }

  /* ---------- Загрузки ----------
     main.js шлёт объект с id (реестр DownloadItem в главном процессе):
     по нему же кладём в словарь — раньше ключом служил name|url, и одинаковые
     файлы накладывались друг на друга. */
  function onDownload(item) {
    var key = item.id || (item.name + '|' + item.url);
    downloads[key] = item;
    renderDownloads();
  }
  function renderDownloads() {
    var keys = Object.keys(downloads);
    // Нижняя панель: только активные.
    var active = keys.filter(function (k) {
      var s = downloads[k].state; return s === 'progressing' || s === 'paused';
    });
    elDownloads.hidden = active.length === 0;
    elDownloads.innerHTML = '';
    active.forEach(function (k) { elDownloads.appendChild(downloadRow(downloads[k], true)); });

    // Панель «Загрузки»: все.
    elDownloadsList.innerHTML = '';
    if (!keys.length) {
      elDownloadsList.innerHTML = '<div class="bb-panel-empty">Загрузок пока нет</div>';
      return;
    }
    keys.slice().reverse().forEach(function (k) {
      elDownloadsList.appendChild(downloadRow(downloads[k], false));
    });
  }
  function downloadRow(item, compact) {
    var row = document.createElement('div');
    row.className = 'bb-dl ' + (item.state || '');
    var pct = item.total ? Math.round(item.received / item.total * 100) : 0;
    var status = item.state === 'completed' ? 'Готово'
      : item.state === 'cancelled' ? 'Отменено'
      : item.state === 'interrupted' ? 'Ошибка'
      : fmtBytes(item.received) + (item.total ? ' / ' + fmtBytes(item.total) : '');
    row.innerHTML =
      '<div class="bb-dl-icon"><svg viewBox="0 0 16 16"><path d="M9 2H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V6L9 2z" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M9 2v4h4" fill="none" stroke="currentColor" stroke-width="1.2"/></svg></div>' +
      '<div class="bb-dl-main">' +
        '<div class="bb-dl-name">' + escapeHtml(item.name) + '</div>' +
        '<div class="bb-dl-status">' + status + (item.total && item.state !== 'completed' ? ' · ' + pct + '%' : '') + '</div>' +
        '<div class="bb-dl-bar"><i style="width:' + (item.state === 'completed' ? 100 : pct) + '%"></i></div>' +
      '</div>';
    if (item.state === 'completed' && item.path && !compact) {
      var open = document.createElement('button');
      open.className = 'bb-dl-open';
      open.textContent = 'Открыть';
      open.addEventListener('click', function () {
        if (api.openPath) api.openPath(item.path);
      });
      row.appendChild(open);
    }
    // Кнопки управления: пауза/продолжить/отмена и «в папке».
    // Команды уходят в главный процесс (DownloadItem там), новое состояние
    // вернётся само событием 'browser-download'.
    var acts = document.createElement('div');
    acts.className = 'bb-dl-acts';
    var id = item.id;
    var active = item.state === 'progressing' || item.state === 'paused';
    if (id && active) {
      var pp = document.createElement('button');
      pp.className = 'bb-dl-btn';
      pp.title = item.state === 'paused' ? 'Продолжить' : 'Пауза';
      pp.innerHTML = item.state === 'paused'
        ? '<svg viewBox="0 0 16 16"><path d="M5 3.5l7 4.5-7 4.5z" fill="currentColor"/></svg>'
        : '<svg viewBox="0 0 16 16"><path d="M6 3.5v9M10 3.5v9" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
      pp.addEventListener('click', function () {
        api.downloadControl(id, item.state === 'paused' ? 'resume' : 'pause');
      });
      acts.appendChild(pp);
      var cancel = document.createElement('button');
      cancel.className = 'bb-dl-btn warn';
      cancel.title = 'Отменить загрузку';
      cancel.innerHTML = '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8"/></svg>';
      cancel.addEventListener('click', function () { api.downloadControl(id, 'cancel'); });
      acts.appendChild(cancel);
    }
    if (item.state === 'completed' && item.path) {
      var reveal = document.createElement('button');
      reveal.className = 'bb-dl-btn';
      reveal.title = 'Показать в папке';
      reveal.innerHTML = '<svg viewBox="0 0 16 16"><path d="M2 4.5h4l1.5 2H14v6.5H2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>';
      reveal.addEventListener('click', function () { api.downloadReveal(item.path); });
      acts.appendChild(reveal);
    }
    if (acts.childNodes.length) row.appendChild(acts);
    return row;
  }
  function fmtBytes(n) {
    if (!n) return '0 Б';
    var u = ['Б', 'КБ', 'МБ', 'ГБ'], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + ' ' + u[i];
  }

  /* ---------- Панель истории ----------
     Поиск по заголовку/адресу, группировка по дням, удаление отдельной
     записи (крестиком — раньше запись можно было только снести целиком). */
  function dayLabel(ts) {
    var d = new Date(ts), now = new Date();
    var startOf = function (x) { return new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime(); };
    var diff = (startOf(now) - startOf(d)) / 86400000;
    if (diff === 0) return 'Сегодня';
    if (diff === 1) return 'Вчера';
    return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
  }

  function renderHistory() {
    var q = (elHistorySearch.value || '').trim().toLowerCase();
    elHistoryList.innerHTML = '';
    var list = history.filter(function (h) {
      if (!q) return true;
      return String(h.title || '').toLowerCase().indexOf(q) >= 0 ||
             String(h.url || '').toLowerCase().indexOf(q) >= 0;
    }).slice(0, 300);
    if (!list.length) {
      elHistoryList.innerHTML = '<div class="bb-panel-empty">' +
        (q ? 'Ничего не найдено' : 'История пуста') + '</div>';
      return;
    }
    var lastDay = null;
    list.forEach(function (h) {
      var day = dayLabel(h.ts || 0);
      if (day !== lastDay) {
        lastDay = day;
        var head = document.createElement('div');
        head.className = 'bb-hist-day';
        head.textContent = day;
        elHistoryList.appendChild(head);
      }
      var row = document.createElement('div');
      row.className = 'bb-panel-item';
      row.innerHTML =
        '<div style="flex:1;min-width:0">' +
          '<div class="t">' + escapeHtml(h.title || titleFromUrl(h.url)) + '</div>' +
          '<div class="u">' + escapeHtml(h.url) + '</div>' +
        '</div>';
      row.addEventListener('click', function () {
        var t = activeTab();
        if (t) t.webview.loadURL(h.url);
        togglePanel(elHistoryPanel, false);
      });
      var del = document.createElement('button');
      del.className = 'bb-hist-del';
      del.title = 'Удалить из истории';
      del.innerHTML = '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8"/></svg>';
      del.addEventListener('click', function (e) {
        e.stopPropagation();
        var idx = history.indexOf(h);
        if (idx >= 0) { history.splice(idx, 1); saveJSON(LS_HIST, history); }
        renderHistory();
      });
      row.appendChild(del);
      elHistoryList.appendChild(row);
    });
  }

  /* ---------- Панель Chrome-расширений ----------
     Реестр и загрузка живут в главном процессе (desktop/extensions.js,
     session.extensions). Здесь только список и команды: вкл/выкл, удалить,
     добавить папку или .crx. Инкогнито-вкладки расширений не получают. */
  /* Показать отказ главного процесса: коротко на тосте, подробности — по
     кнопке (текст ошибки Chromium длинный и в один тост не влезает). */
  function addExtError(r) {
    if (!r || !r.error) return;
    toast('Расширение: ' + r.error, 'err', r.detail ? {
      label: 'Подробнее',
      onClick: function () { toast(r.detail, 'err', null, true); }
    } : null);
  }

  /* Иконки расширений в тулбаре: Electron грузит расширение, но своего
     action-места не даёт. Кнопка открывает popup расширения (если он есть)
     или честно сообщает, что у расширения только фон. */
  function renderExtIcons() {
    if (!elExtIcons) return;
    if (!api.extList) { elExtIcons.hidden = true; return; }
    Promise.resolve(api.extList()).then(function (list) {
      var on = (list || []).filter(function (x) { return x.enabled && x.loaded; });
      elExtIcons.innerHTML = '';
      if (!on.length) { elExtIcons.hidden = true; return; }
      elExtIcons.hidden = false;
      on.forEach(function (x) {
        var b = document.createElement('button');
        b.className = 'bb-ext-btn';
        b.title = x.name + (x.warning ? '\n⚠ ' + x.warning : '');
        if (x.icon) {
          var img = document.createElement('img');
          img.src = x.icon;
          img.alt = '';
          b.appendChild(img);
        } else {
          var s = document.createElement('span');
          s.className = 'bb-ext-glyph';
          s.textContent = '🧩';
          b.appendChild(s);
        }
        b.addEventListener('click', function () {
          if (!x.id) { toast('Расширение не загружено', 'err'); return; }
          if (x.popup) {
            createTab({ url: 'chrome-extension://' + x.id + '/' + String(x.popup).replace(/^\/+/, '') });
          } else {
            toast('У расширения нет popup — оно работает в фоне' +
              (x.warning ? ': ' + x.warning : ''), x.warning ? 'err' : 'ok');
          }
        });
        elExtIcons.appendChild(b);
      });
    }).catch(function () { elExtIcons.hidden = true; });
  }

  /* ---------- Свои плагины браузера ----------
     Отдельная от Chrome-расширений система: свой формат plugin.json, JS и CSS
     внедряются в страницу после готовности DOM (desktop/plugins.js). Панель
     только показывает список и шлёт команды; включение уже открытых вкладок
     требует перезагрузки — за это отвечает кнопка «Применить». */
  function reloadAllTabs() {
    var n = 0;
    tabs.forEach(function (t) {
      try { t.webview.reload(); n++; } catch (e) {}
    });
    toast(n ? 'Вкладки перезагружены — плагины применены' : 'Нет открытых вкладок', 'ok');
  }

  function renderPlugins() {
    if (!elPluginsList) return;
    if (!api.pluginsList) {
      elPluginsList.innerHTML = '<div class="bb-panel-empty">Плагины доступны только в приложении (Electron)</div>';
      return;
    }
    elPluginsList.innerHTML = '<div class="bb-panel-empty">Загрузка…</div>';
    Promise.resolve(api.pluginsList()).then(function (list) {
      elPluginsList.innerHTML = '';

      var hint = document.createElement('div');
      hint.className = 'bb-panel-empty';
      hint.style.cssText = 'text-align:left;padding:10px 12px;line-height:1.5';
      hint.textContent = 'Свои плагины: папка с plugin.json и скриптом/CSS. ' +
        'Работают на страницах вкладок и не требуют Chrome-расширений.';
      var acts = document.createElement('div');
      acts.style.cssText = 'display:flex;gap:6px;margin-top:8px;flex-wrap:wrap';
      var mk = function (label, title, fn) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'bb-panel-clear';
        b.textContent = label;
        b.title = title;
        b.addEventListener('click', fn);
        return b;
      };
      acts.appendChild(mk('Применить', 'Перезагрузить открытые вкладки', reloadAllTabs));
      acts.appendChild(mk('Пример', 'Создать выключенный пример плагина', function () {
        Promise.resolve(api.pluginsExample()).then(function (r) {
          if (r && r.ok) { toast('Пример создан — включите его в списке', 'ok'); renderPlugins(); }
          else toast('Плагин: ' + ((r && r.error) || 'не удалось'), 'err');
        }).catch(function () {});
      }));
      acts.appendChild(mk('Папка', 'Открыть папку плагинов', function () {
        try { api.pluginsOpenDir(); } catch (e) {}
      }));
      hint.appendChild(acts);
      elPluginsList.appendChild(hint);

      if (!list || !list.length) {
        var empty = document.createElement('div');
        empty.className = 'bb-panel-empty';
        empty.textContent = 'Плагинов нет. «Папка…» или «Архив .zip…» — добавить свой.';
        elPluginsList.appendChild(empty);
        return;
      }

      list.forEach(function (x) {
        var row = document.createElement('div');
        row.className = 'bb-panel-item';
        var info = document.createElement('div');
        info.style.cssText = 'flex:1;min-width:0';
        var where = (x.matches && x.matches.length) ? x.matches.join(', ') : 'все сайты (http/https)';
        var files = [];
        if (x.css && x.css.length) files.push('CSS');
        if (x.js && x.js.length) files.push('JS');
        info.innerHTML =
          '<div class="t">' + escapeHtml(x.name) +
            (x.version ? ' <span class="bb-ext-ver">' + escapeHtml(x.version) + '</span>' : '') +
          '</div>' +
          '<div class="u">' + escapeHtml(where) + (files.length ? ' · ' + files.join('+') : '') + '</div>' +
          (x.description ? '<div class="u">' + escapeHtml(x.description) + '</div>' : '') +
          (x.error ? '<div class="u bb-ext-warn">⚠ ' + escapeHtml(x.error) + '</div>' : '');
        row.appendChild(info);

        var sw = document.createElement('button');
        sw.className = 'bb-ext-switch' + (x.enabled ? ' on' : '');
        sw.title = x.enabled ? 'Выключить' : 'Включить';
        sw.setAttribute('aria-label', sw.title);
        sw.innerHTML = '<span></span>';
        sw.addEventListener('click', function () {
          Promise.resolve(api.pluginsToggle(x.key)).then(function (r) {
            if (r && !r.ok) toast('Плагин: ' + r.error, 'err');
            renderPlugins();
          }).catch(function () {});
        });
        row.appendChild(sw);

        var del = document.createElement('button');
        del.className = 'bb-hist-del';
        del.title = 'Удалить';
        del.innerHTML = '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8"/></svg>';
        del.addEventListener('click', function () {
          Promise.resolve(api.pluginsRemove(x.key)).then(function (r) {
            if (r && !r.ok) toast('Плагин: ' + r.error, 'err');
            renderPlugins();
          }).catch(function () {});
        });
        row.appendChild(del);
        elPluginsList.appendChild(row);
      });
    }).catch(function () {
      elPluginsList.innerHTML = '<div class="bb-panel-empty">Не удалось получить список плагинов</div>';
    });
  }

  function renderExtensions() {
    renderExtIcons();
    if (!api.extList) {
      elExtList.innerHTML = '<div class="bb-panel-empty">Расширения доступны только в приложении (Electron)</div>';
      return;
    }
    elExtList.innerHTML = '<div class="bb-panel-empty">Загрузка…</div>';
    Promise.resolve(api.extList()).then(function (list) {
      elExtList.innerHTML = '';
      if (!list || !list.length) {
        elExtList.innerHTML = '<div class="bb-panel-empty">Расширений нет.<br>' +
          '«Папка…» — распакованное расширение, «Файл .crx/.zip…» — файл с ПК.</div>';
        return;
      }
      list.forEach(function (x) {
        var row = document.createElement('div');
        row.className = 'bb-panel-item';
        var info = document.createElement('div');
        info.style.cssText = 'flex:1;min-width:0';
        info.innerHTML =
          '<div class="t">' + escapeHtml(x.name) +
            (x.version ? ' <span class="bb-ext-ver">' + escapeHtml(x.version) + '</span>' : '') +
          '</div>' +
          '<div class="u">' + escapeHtml(x.dir) + (x.loaded ? '' : ' — не загружено') + '</div>' +
          (x.warning ? '<div class="u bb-ext-warn">⚠ ' + escapeHtml(x.warning) + '</div>' : '');
        row.appendChild(info);

        var sw = document.createElement('button');
        sw.className = 'bb-ext-switch' + (x.enabled ? ' on' : '');
        sw.title = x.enabled ? 'Выключить' : 'Включить';
        sw.setAttribute('aria-label', sw.title);
        sw.innerHTML = '<span></span>';
        sw.addEventListener('click', function () {
          Promise.resolve(api.extToggle(x.key)).then(function (r) {
            if (r && !r.ok) addExtError(r);
            renderExtensions();
          }).catch(function () {});
        });
        row.appendChild(sw);

        var del = document.createElement('button');
        del.className = 'bb-hist-del';
        del.title = 'Удалить';
        del.innerHTML = '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8"/></svg>';
        del.addEventListener('click', function () {
          Promise.resolve(api.extRemove(x.key)).then(function (r) {
            if (r && !r.ok) toast('Расширение: ' + r.error, 'err');
            renderExtensions();
          }).catch(function () {});
        });
        row.appendChild(del);
        elExtList.appendChild(row);
      });
    }).catch(function () {
      elExtList.innerHTML = '<div class="bb-panel-empty">Не удалось получить список</div>';
    });
  }

  /* ---------- Уведомления (тосты) ----------
     Вместо alert(): в frameless-окне системное модальное окно выглядит
     чужеродно и перехватывает фокус. Тост сам уходит через 4 с. */
  var elToasts = $('bbToasts');
  var ICON_OK = '<svg viewBox="0 0 16 16"><path d="M3 8.5l3.2 3.2L13 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var ICON_ERR = '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  var ICON_INFO = '<svg viewBox="0 0 16 16"><path d="M8 7v5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="8" cy="4.3" r="1" fill="currentColor"/></svg>';
  var ICON_CARET = '<svg viewBox="0 0 16 16"><path d="M5 6.5L8 10l3-3.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  function toast(msg, kind, action, wide) {
    if (!elToasts) return;
    var el = document.createElement('div');
    el.className = 'bb-toast' + (kind ? ' ' + kind : '') + (wide ? ' wide' : '');
    el.innerHTML =
      '<span class="bb-toast-ico">' + (kind === 'ok' ? ICON_OK : kind === 'err' ? ICON_ERR : ICON_INFO) + '</span>' +
      '<span class="bb-toast-msg">' + escapeHtml(msg) + '</span>';
    if (action && action.label) {
      var btn = document.createElement('button');
      btn.className = 'bb-toast-act';
      btn.textContent = action.label;
      btn.addEventListener('click', function () {
        hide();
        try { action.onClick(); } catch (e) { /* не критично */ }
      });
      el.appendChild(btn);
    }
    elToasts.appendChild(el);
    // Подробный тост не режется многоточием и живёт дольше обычного —
    // текст ошибки Chromium длинный, в 4 секунды его не прочитать.
    var timer = setTimeout(hide, wide ? 15000 : 4000);
    function hide() {
      clearTimeout(timer);
      if (!el.parentNode) return;
      el.classList.add('hide');
      setTimeout(function () { if (el.parentNode) el.remove(); }, 200);
    }
    return hide;
  }

  /* ---------- Сохранение страницы в модуль HTML Viewer ----------
     Забираем DOM активной вкладки через executeJavaScript (сработает и для
     кросс-доменных страниц, т.к. webview в Electron), шлём на бэкенд, тот
     кладёт файл в каталог HTML Viewer. В ответ приходит ссылка для открытия. */
  var SAVE_API = '/api/html/save-page';
  var MAX_SAVE = 20 * 1024 * 1024;   // 20 МБ — дальше HTML раздувается бессмысленно

  function savePage() {
    var t = activeTab();
    if (!t || !t.webview) { toast('Нет активной вкладки', 'err'); return; }
    if (!/^https?:/i.test(t.url || '')) {
      toast('Сохранять можно только http/https-страницы', 'err');
      return;
    }
    if (t._saving) return;
    t._saving = true;
    var hideBusy = toast('Сохраняю страницу…', null, null);

    var js = '(function(){try{return JSON.stringify({' +
      'html: document.documentElement.outerHTML,' +
      'title: document.title || location.hostname' +
      '});}catch(e){return "";}})()';

    var page;
    try {
      page = t.webview.executeJavaScript(js);
    } catch (e) {
      t._saving = false; hideBusy();
      toast('Не удалось прочитать страницу: ' + e.message, 'err');
      return;
    }
    Promise.resolve(page).then(function (raw) {
      var title = '', html = '';
      try {
        var o = JSON.parse(raw || '{}');
        html = o.html || '';
        title = o.title || '';
      } catch (e) { /* пусто — покажем общую ошибку ниже */ }
      if (!html) { t._saving = false; hideBusy(); toast('Страница пустая или ещё грузится', 'err'); return; }
      if (html.length > MAX_SAVE) {
        t._saving = false; hideBusy();
        toast('Страница слишком большая (' + fmtBytes(html.length) + ')', 'err');
        return;
      }
      return fetch(SAVE_API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: t.url, title: title, html: html })
      }).then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, d: d }; });
      }).then(function (res) {
        t._saving = false;
        hideBusy();
        if (!res.ok || !res.d || !res.d.ok) {
          toast('Не сохранено: ' + ((res.d && (res.d.error || res.d.detail)) || 'ошибка сервера'), 'err');
          return;
        }
        // Ссылка на /html/viewer открывается новой вкладкой: window.open в
        // обёртке перехватывает setWindowOpenHandler и создаёт окно
        // приложения (handleWindowOpen), а не системный браузер.
        var href = res.d.viewer_url || ('/html/viewer?path=' + encodeURIComponent(res.d.path || ''));
        toast('Сохранено в HTML Viewer: ' + (res.d.name || ''), 'ok', {
          label: 'Открыть',
          onClick: function () { window.open(href, '_blank'); }
        });
      });
    }).catch(function (e) {
      t._saving = false; hideBusy();
      toast('Ошибка сохранения: ' + (e && e.message ? e.message : e), 'err');
    });
  }
  /* ---------- Контекстные меню (общий механизм) ----------
     Правая кнопка на закладке/вкладке рисует свой .bb-ctx: системное меню
     в frameless-окне выглядит чужеродно, как и alert(). Держим одно меню
     (anyMenu) и закрываем по клику/Esc. */
  function closeAnyMenu() {
    if (anyMenu && anyMenu.parentNode) anyMenu.remove();
    anyMenu = null;
  }
  function showContextMenu(items, x, y) {
    closeAnyMenu();
    var menu = document.createElement('div');
    menu.className = 'bb-ctx';
    items.forEach(function (it) {
      if (it.sep) { var s = document.createElement('div'); s.className = 'bb-ctx-sep'; menu.appendChild(s); return; }
      var b = document.createElement('button');
      if (it.danger) b.classList.add('danger');
      if (it.disabled) b.disabled = true;
      var lbl = document.createElement('span');
      lbl.textContent = it.label;
      b.appendChild(lbl);
      if (it.kbd) { var k = document.createElement('kbd'); k.textContent = it.kbd; b.appendChild(k); }
      b.addEventListener('click', function () {
        closeAnyMenu();
        try { it.onClick(); } catch (e) { console.error('[browser] menu', e); }
      });
      menu.appendChild(b);
    });
    document.body.appendChild(menu);
    menu.style.left = Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8)) + 'px';
    menu.style.top = Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8)) + 'px';
    anyMenu = menu;
  }
  document.addEventListener('mousedown', function (e) {
    if (anyMenu && !anyMenu.contains(e.target)) closeAnyMenu();
  });

  /* ---------- Панели и меню ---------- */
  function togglePanel(panel, force) {
    var show = typeof force === 'boolean' ? force : panel.hidden;
    // Панели взаимоисключающие: открыта одна — остальные прячем.
    [elHistoryPanel, elDownloadsPanel, elExtPanel, elPluginsPanel, elPasswordsPanel].forEach(function (p) {
      if (show && p && p !== panel) p.hidden = true;
    });
    panel.hidden = !show;
  }
  /* ---------- Поиск по странице (Ctrl+F) ----------
     Своя панель: подсветку делаем в DOM страницы через executeJavaScript
     (голд поиска у <webview> не переносится на разметку панели). Состояние
     живёт в самой странице (window.__bbFind), панель только держит счётчик. */
  var elFind = $('bbFind'), elFindInput = $('bbFindInput'), elFindCount = $('bbFindCount');

  var FIND_JS = '(function(){' +
    'var q = __Q__, step = __STEP__;' +
    'var S = window.__bbFind || (window.__bbFind = {q:"", idx:0});' +
    'function clear(){' +
      'var ms = document.querySelectorAll("mark.__bbhit");' +
      'for (var i=0;i<ms.length;i++){ var m=ms[i], p=m.parentNode;' +
        'if(!p) continue; p.replaceChild(document.createTextNode(m.textContent), m); p.normalize(); }' +
    '}' +
    'if (!q) { clear(); S.q=""; S.idx=0; return JSON.stringify({total:0, idx:0}); }' +
    'if (S.q !== q) { S.q = q; S.idx = 0; }' +
    'clear();' +
    'var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {' +
      'acceptNode: function(n){' +
        'if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;' +
        'var t = n.parentNode && n.parentNode.nodeName;' +
        'if (t === "SCRIPT" || t === "STYLE" || t === "MARK" || t === "TEXTAREA" || t === "INPUT") return NodeFilter.FILTER_REJECT;' +
        'return NodeFilter.FILTER_ACCEPT;' +
      '}});' +
    'var node, targets = [], total = 0, ql = q.toLowerCase();' +
    'while ((node = walker.nextNode())) {' +
      'var text = node.nodeValue, lower = text.toLowerCase(), from = 0, at, parts = null;' +
      'while ((at = lower.indexOf(ql, from)) !== -1) {' +
        'if (!parts) parts = [document.createTextNode(text.slice(0, at))];' +
        'else parts.push(document.createTextNode(text.slice(from, at)));' +
        'var mk = document.createElement("mark"); mk.className = "__bbhit";' +
        'mk.textContent = text.slice(at, at + ql.length);' +
        'parts.push(mk); targets.push(mk); total++;' +
        'from = at + ql.length;' +
      '}' +
      'if (!parts) continue;' +
      'parts.push(document.createTextNode(text.slice(from)));' +
      'var frag = document.createDocumentFragment();' +
      'for (var j=0;j<parts.length;j++) frag.appendChild(parts[j]);' +
      'node.parentNode.replaceChild(frag, node);' +
    '}' +
    'if (!total) { S.idx = 0; return JSON.stringify({total:0, idx:0}); }' +
    'if (step === 1) S.idx = (S.idx + 1) % total;' +
    'else if (step === -1) S.idx = (S.idx - 1 + total) % total;' +
    'else S.idx = 0;' +
    'for (var k=0;k<targets.length;k++) targets[k].style.cssText = k === S.idx ? "background:#5b8cff;color:#fff" : "background:#f0b429;color:#111";' +
    'var cur = targets[S.idx];' +
    'try { cur.scrollIntoView({block:"center"}); } catch (e) {}' +
    'return JSON.stringify({total: total, idx: S.idx + 1});' +
  '})()';

  function findOnPage(step) {
    var t = activeTab();
    if (!t || !t.webview) return;
    var q = elFindInput.value || '';
    var js = FIND_JS.replace('__Q__', JSON.stringify(q)).replace('__STEP__', String(step));
    Promise.resolve(t.webview.executeJavaScript(js)).then(function (raw) {
      var r = JSON.parse(raw || '{}');
      if (!q) { elFindCount.textContent = ''; elFindCount.classList.remove('none'); }
      else if (!r.total) { elFindCount.textContent = 'Нет совпадений'; elFindCount.classList.add('none'); }
      else { elFindCount.textContent = r.idx + ' из ' + r.total; elFindCount.classList.remove('none'); }
    }).catch(function () { /* страница ещё грузится */ });
  }

  function openFind() {
    if (!elFind) return;
    elFind.hidden = false;
    elFindInput.focus();
    elFindInput.select();
  }
  /* ---------- Масштаб (Ctrl +/-/0) ----------
     zoomLevel у webview: 0 = 100%. Бейдж живёт 1.2 с. */
  var zoomBadge = null, zoomTimer = null;

  function showZoomBadge() {
    var t = activeTab();
    if (!t || !t.webview || !zoomBadge) return;
    var lvl = 0;
    try { lvl = t.webview.getZoomLevel(); } catch (e) { return; }
    zoomBadge.textContent = Math.round(100 * Math.pow(1.2, lvl)) + '%';
    zoomBadge.hidden = false;
    clearTimeout(zoomTimer);
    zoomTimer = setTimeout(function () { zoomBadge.hidden = true; }, 1200);
  }
  function zoomBy(delta) {
    var t = activeTab();
    if (!t || !t.webview) return;
    var lvl = 0;
    try { lvl = t.webview.getZoomLevel(); } catch (e) { return; }
    lvl = Math.max(-6, Math.min(8, lvl + delta));
    try { t.webview.setZoomLevel(lvl); } catch (e) { /* webview не готов */ }
    t.zoom = lvl;
    showZoomBadge();
  }
  function zoomReset() {
    var t = activeTab();
    if (!t || !t.webview) return;
    try { t.webview.setZoomLevel(0); } catch (e) {}
    t.zoom = 0;
    showZoomBadge();
  }
  /* ---------- Читаемый режим и перевод (F9/F10) ----------
     Слой .bb-reader ложится поверх webview в .bb-views: страница остаётся
     под ним (вернуться — Esc или кнопка «Закрыть»). Текст статьи вынимаем
     через executeJavaScript — та же техника, что у «Сохранить страницу». */
  var readerEl = null;

  var EXTRACT_JS = '(function(){' +
    'function txt(el){ return (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim(); }' +
    'var root = document.querySelector("article, main, [role=main]") || document.body;' +
    'var heads = [], paras = [], i, n;' +
    'var h = root.querySelectorAll("h1, h2, h3");' +
    'for (i = 0; i < h.length && heads.length < 40; i++) {' +
      'n = h[i]; var s = txt(n); if (s.length > 1 && s.length < 300) heads.push({ tag: n.tagName, text: s });' +
    '}' +
    'var p = root.querySelectorAll("p, li, blockquote, pre, td");' +
    'for (i = 0; i < p.length && paras.length < 400; i++) {' +
      'n = p[i]; var t = txt(n); if (t.length > 40) paras.push({ tag: n.tagName, text: t });' +
    '}' +
    'var bodyText = (document.body.innerText || "").replace(/\\s+/g, " ").trim();' +
    'return JSON.stringify({ title: document.title || "", url: location.href,' +
      ' heads: heads, paras: paras, full: bodyText.slice(0, 100000) });' +
  '})()';

  function extractArticle() {
    var t = activeTab();
    if (!t || !t.webview) return Promise.reject(new Error('Нет активной вкладки'));
    if (!/^https?:/i.test(t.url || '')) return Promise.reject(new Error('Не http/https-страница'));
    return Promise.resolve(t.webview.executeJavaScript(EXTRACT_JS)).then(function (raw) {
      var o = JSON.parse(raw || '{}');
      if (!o.paras || !o.paras.length) throw new Error('На странице нет текста статьи');
      return o;
    });
  }

  function openReader(badgeText) {
    closeReader();
    readerEl = document.createElement('div');
    readerEl.className = 'bb-reader';
    var inner = document.createElement('div');
    inner.className = 'bb-reader-inner';
    readerEl.appendChild(inner);
    elViews.appendChild(readerEl);
    readerEl._build = function (meta, err) {
      inner.innerHTML = '';
      var bar = document.createElement('div');
      bar.className = 'bb-reader-meta';
      if (badgeText) {
        var badge = document.createElement('span');
        badge.className = 'bb-reader-badge';
        badge.textContent = badgeText;
        bar.appendChild(badge);
      }
      var info = document.createElement('span');
      info.textContent = meta || '';
      bar.appendChild(info);
      var btn = document.createElement('button');
      btn.className = 'bb-find-btn';
      btn.title = 'Закрыть (Esc)';
      btn.innerHTML = '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6"/></svg>';
      btn.addEventListener('click', closeReader);
      bar.appendChild(btn);
      inner.appendChild(bar);
      if (err) {
        var e = document.createElement('div');
        e.className = 'bb-reader-err';
        e.textContent = err;
        inner.appendChild(e);
      }
    };
    return readerEl;
  }

  function translatePage() {
    if (readerOpen()) closeReader();
    var hideBusy = toast('Готовлю текст для перевода…', null, null);
    var el = openReader('Перевод');
    el._build('перевод локальной моделью…', null);
    var inner = el.querySelector('.bb-reader-inner');
    var progress = document.createElement('p');
    progress.textContent = 'Перевод страницы — это может занять минуту…';
    inner.appendChild(progress);
    extractArticle().then(function (o) {
      hideBusy();
      var text = o.full || o.paras.map(function (p) { return p.text; }).join('\n\n');
      return fetch('/api/browser/translate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text, target: 'русский' })
      }).then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, d: d }; });
      }).then(function (res) {
        if (!res.ok || !res.d || !res.d.ok) {
          el._build(o.url, 'Не переведено: ' + ((res.d && res.d.error) || 'ошибка сервера'));
          return;
        }
        el._build(o.title || o.url, null);
        var body = el.querySelector('.bb-reader-inner');
        res.d.text.split('\n').forEach(function (line) {
          line = line.trim();
          if (!line) return;
          var p = document.createElement('p');
          p.textContent = line;
          body.appendChild(p);
        });
        toast('Переведено (фрагментов: ' + res.d.chunks + ')', 'ok');
      });
    }).catch(function (e) {
      hideBusy();
      var msg = e && e.message ? e.message : String(e);
      if (readerEl) el._build('', msg);
      toast('Перевод: ' + msg, 'err');
    });
  }

  /* Счётчик трекеров для пункта меню (обновляем при каждом открытии). */
  function refreshTrackerCount() {
    var el = $('bbMenuTrackers');
    if (!el || !api.trackerCount) return;
    Promise.resolve(api.trackerCount()).then(function (n) {
      el.textContent = 'Заблокировано трекеров: ' + (n || 0);
    }).catch(function () { el.textContent = 'Заблокировано трекеров: —'; });
  }

  /* ---------- Печать в PDF и вопрос ИИ о странице ---------- */
  function printPdf() {
    var t = activeTab();
    if (!t || !t.webview || !api.printToPDF) { toast('Печать недоступна', 'err'); return; }
    var wcId;
    try { wcId = t.webview.getWebContentsId(); } catch (e) { toast('Вкладка ещё не готова', 'err'); return; }
    var hideBusy = toast('Сохраняю PDF…', null, null);
    Promise.resolve(api.printToPDF({ webContentsId: wcId, suggested: (t.titleEl.textContent || 'page') + '.pdf' }))
      .then(function (res) {
        hideBusy();
        if (!res || res.canceled) return;
        if (res.ok) toast('PDF сохранён', 'ok', { label: 'Открыть', onClick: function () { if (api.openPath) api.openPath(res.path); } });
        else toast('PDF: ' + (res.error || 'не удалось сохранить'), 'err');
      })
      .catch(function (e) { hideBusy(); toast('PDF: ' + e.message, 'err'); });
  }

  function askAiAboutPage() {
    var t = activeTab();
    if (!t || !t.webview || !api.askAiBar) return;
    Promise.resolve(t.webview.executeJavaScript(
      '(function(){try{var s=String(getSelection());return JSON.stringify(s?s.trim():"");}catch(e){return JSON.stringify("");}})()'
    )).then(function (raw) {
      var sel = '';
      try { sel = JSON.parse(raw || '""'); } catch (e) {}
      var text = sel
        ? sel + '\n(со страницы ' + t.url + ')'
        : 'Страница: ' + (t.titleEl.textContent || t.url) + '\n' + t.url + '\nОпиши, о чём она, коротко.';
      api.askAiBar(text);
    }).catch(function () {
      api.askAiBar('Страница: ' + t.url);
    });
  }

  /* ---------- Выделение: меню, определение слова ----------
     Guest-preload шлёт текст и координаты (относительно страницы сайта). Меню
     и карточка живут в оболочке браузера и позиционируются по экранным
     координатам: у активной вкладки добавляем смещение её viewWrap. */
  var elSelMenu = $('bbSelMenu'), elWordPop = $('bbWordPop');
  var selText = '';
  var wordSeq = 0;

  function hideSelMenu() { if (elSelMenu) elSelMenu.hidden = true; }
  function hideWordPop() { if (elWordPop) elWordPop.hidden = true; }

  function guestPoint(tab, x, y) {
    try {
      var r = tab.viewWrap.getBoundingClientRect();
      return { x: r.left + (x || 0), y: r.top + (y || 0) };
    } catch (e) { return { x: x || 0, y: y || 0 }; }
  }
  function placePopup(el, x, y) {
    if (!el) return;
    el.hidden = false;
    var w = el.offsetWidth, h = el.offsetHeight;
    var left = Math.max(6, Math.min(x, window.innerWidth - w - 8));
    var top = y - h - 10;
    if (top < 6) top = Math.min(y + 16, window.innerHeight - h - 8);
    el.style.left = left + 'px';
    el.style.top = top + 'px';
  }

  function onGuestSelection(tab, p) {
    if (tab !== activeTab()) return;
    var text = String(p.text || '').trim();
    if (!text) { hideSelMenu(); return; }
    selText = text;
    hideWordPop();
    /* context-menu из Electron уже даёт экранные координаты, а guest-preload —
       локальные в странице; если координаты пришли как «локальные», rect
       viewWrap не пуст, и мы получим отрицательное/нулевое значение x. В этом
       случае menu всё равно покажем — по курсору. */
    var pt = guestPoint(tab, p.x, p.y);
    if (pt.x <= 0 && pt.y <= 0) { pt = { x: 24, y: 60 }; }
    placePopup(elSelMenu, pt.x, pt.y);
  }

  function onGuestWord(tab, p) {
    if (tab !== activeTab()) return;
    var word = String(p.word || '').trim();
    hideSelMenu();
    if (!word) { hideWordPop(); return; }
    var pt = guestPoint(tab, p.x, p.y);
    defineWord(word, pt.x, pt.y);
  }

  /* Двойной клик по слову: короткое определение локальной моделью. */
  function defineWord(word, x, y) {
    if (!elWordPop) return;
    var seq = ++wordSeq;
    var termEl = $('bbWordTerm'), phonEl = $('bbWordPhon'), bodyEl = $('bbWordBody');
    if (termEl) termEl.textContent = word;
    if (phonEl) phonEl.textContent = '';
    if (bodyEl) bodyEl.innerHTML = '<div class="bb-word-loading">Ищу определение…</div>';
    placePopup(elWordPop, x, y);
    fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: 'Дай краткое определение слова «' + word + '»: часть речи и 1–3 основных значения. Без вступления и повторов.',
        mode: 'standard', use_cache: true, skip_memory: true
      })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (seq !== wordSeq || !bodyEl) return;
      var ans = d && (d.answer || d.error) ? String(d.answer || d.error) : '';
      if (!ans) ans = 'Определение не найдено.';
      bodyEl.innerHTML = '<div class="bb-word-def">' +
        escapeHtml(ans).replace(/\n/g, '<br>') + '</div>';
      placePopup(elWordPop, x, y);
    }).catch(function () {
      if (seq !== wordSeq || !bodyEl) return;
      bodyEl.innerHTML = '<div class="bb-word-def">Не удалось получить определение</div>';
    });
  }

  /* Прочитать текущее выделение прямо из страницы. Нужно, если сообщение
     guest-preload о выделении не дошло (preload не подцепился/потерялось
     событие): тогда кнопка «В заметки» всё равно сработает. */
  function currentSelection() {
    var t = activeTab();
    if (!t || !t.webview) return Promise.resolve('');
    return Promise.resolve(t.webview.executeJavaScript(
      '(function(){try{return String(window.getSelection?window.getSelection().toString():"")}catch(e){return ""}})()'
    )).then(function (s) { return String(s || '').trim(); })
      .catch(function () { return ''; });
  }

  function saveSelectionToNotes(text) {
    text = String(text || '').trim();
    if (!text) { toast('Нет выделенного текста', 'err'); return; }
    var t = activeTab();
    var url = t ? (t.url || '') : '';
    var title = t && t.titleEl && t.titleEl.textContent
      ? t.titleEl.textContent : titleFromUrl(url);
    fetch('/api/notes/save', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: 'Из браузера: ' + title,
        content: text + '\n\n---\n[🌐 Источник: ' + url + '](' + url + ')',
        folder: 'inbox', tags: ['browser']
      })
    }).then(function (r) {
      return r.text().then(function (raw) {
        var d = null;
        try { d = raw ? JSON.parse(raw) : null; } catch (e) { /* не JSON */ }
        if (!r.ok) throw new Error((d && d.error) || ('HTTP ' + r.status));
        if (!d) throw new Error('Пустой ответ сервера');
        return d;
      });
    }).then(function (d) {
      if (d && d.ok) toast('Сохранено в заметки', 'ok');
      else toast('Заметки: ' + ((d && d.error) || 'ошибка'), 'err');
    }).catch(function (e) {
      toast('Заметки: ' + (e && e.message ? e.message : e), 'err');
    });
  }

  /* ---------- Перевод страницы «на месте» ----------
     Собираем текстовые узлы (без скриптов, кода, полей ввода), переводим их на
     сервере и подменяем значения прямо в DOM — вёрстка и ссылки остаются. */
  var TRANSLATE_EXTRACT_JS =
    '(function(){try{' +
    'var skip={SCRIPT:1,STYLE:1,NOSCRIPT:1,TEXTAREA:1,CODE:1,PRE:1,CANVAS:1,SVG:1,MATH:1,OPTION:1};' +
    'var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,null);' +
    'var out=[],nodes=[],n;' +
    'while((n=w.nextNode())){' +
    'var p=n.parentNode;if(!p)continue;' +
    'if(skip[p.nodeName])continue;' +
    'if(p.isContentEditable)continue;' +
    'var s=n.nodeValue;if(!s||s.trim().length<2)continue;' +
    'if(/^[\\s\\d\\p{P}]+$/u.test(s))continue;' +
    'nodes.push(n);out.push(s);if(out.length>=400)break;' +
    '}' +
    'window.__bbNodes=nodes;return JSON.stringify(out);' +
    '}catch(e){return JSON.stringify([]);}})()';

  function applyTranslations(list) {
    var t = activeTab();
    if (!t || !t.webview) return Promise.resolve(-1);
    return t.webview.executeJavaScript(
      '(function(){try{var a=' + JSON.stringify(list) + ';' +
      'var n=window.__bbNodes||[];var c=0;' +
      'for(var i=0;i<a.length&&i<n.length;i++){if(a[i]){n[i].nodeValue=a[i];c++;}}' +
      'return c;}catch(e){return -1;}})()'
    );
  }

  function translatePageInline() {
    var t = activeTab();
    if (!t || !t.webview) { toast('Нет активной вкладки', 'err'); return; }
    var hideBusy = toast('Перевожу страницу на месте…', null, null);
    t.webview.executeJavaScript(TRANSLATE_EXTRACT_JS).then(function (raw) {
      var nodes = [];
      try { nodes = JSON.parse(raw || '[]'); } catch (e) {}
      if (!nodes.length) { hideBusy(); toast('Нечего переводить', 'err'); return; }
      return fetch('/api/browser/translate-nodes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nodes: nodes, target: 'русский' })
      }).then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, d: d }; });
      }).then(function (res) {
        hideBusy();
        if (!res.ok || !res.d || !res.d.ok) {
          toast('Перевод: ' + ((res.d && res.d.error) || 'ошибка сервера'), 'err');
          return null;
        }
        return applyTranslations(res.d.translations);
      }).then(function (n) {
        if (n === null) return;
        toast(typeof n === 'number' && n > 0
          ? ('Переведено фрагментов: ' + n) : 'Перевод не применён', 'ok');
      });
    }).catch(function (e) {
      hideBusy();
      toast('Перевод: ' + (e && e.message || e), 'err');
    });
  }

  /* Перевод страницы «на месте» — единственная кнопка перевода. Тяжёлая LLM
     больше не используется: сервер переводит лёгким веб-сервисом, поэтому
     страница переводится за секунды и с сохранением вёрстки. */
  function translatePageSmart() {
    translatePageInline();
  }

  function closeReader() {
    if (readerEl && readerEl.parentNode) readerEl.remove();
    readerEl = null;
  }
  function readerOpen() { return !!readerEl; }

  function toggleReader() {
    if (readerOpen()) { closeReader(); return; }
    var hideBusy = toast('Извлекаю текст статьи…', null, null);
    extractArticle().then(function (o) {
      hideBusy();
      var el = openReader('Читаемый режим');
      el._build(o.url, null);
      var inner = el.querySelector('.bb-reader-inner');
      var h = document.createElement('h1');
      h.textContent = o.title || o.url;
      inner.appendChild(h);
      o.heads.slice(1).forEach(function (hd) {
        var tag = hd.tag.toLowerCase();
        if (tag !== 'h2' && tag !== 'h3') tag = 'h2';
        var e = document.createElement(tag);
        e.textContent = hd.text;
        inner.appendChild(e);
      });
      o.paras.forEach(function (p) {
        var tag = p.tag.toLowerCase();
        var e = document.createElement((tag === 'li' || tag === 'blockquote' || tag === 'pre') ? tag : 'p');
        e.textContent = p.text;
        inner.appendChild(e);
      });
    }).catch(function (e) {
      hideBusy();
      toast('Читаемый режим: ' + (e && e.message ? e.message : e), 'err');
    });
  }

  function closeFind() {
    if (!elFind) return;
    var t = activeTab();
    if (t && t.webview) {
      Promise.resolve(t.webview.executeJavaScript(
        '(function(){var S=window.__bbFind;if(!S)return;var ms=document.querySelectorAll("mark.__bbhit");' +
        'for(var i=0;i<ms.length;i++){var m=ms[i],p=m.parentNode;if(!p)continue;' +
        'p.replaceChild(document.createTextNode(m.textContent),m);p.normalize();}' +
        'S.q="";S.idx=0;})()')).catch(function () {});
    }
    elFind.hidden = true;
    elFindCount.textContent = '';
    elFindCount.classList.remove('none');
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* ---------- Пароли профиля ----------
     Записи хранит сервер (data/passwords.json) и отдаёт по cookie профиля.
     Браузер только показывает подходящие к текущему сайту и подставляет их
     в форму через executeJavaScript; сохранение отправленных логинов делает
     guest-preload (событие bb-login-captured). */
  function hostOf(url) {
    try { return new URL(url).hostname.toLowerCase(); } catch (e) { return ''; }
  }
  function vaultFetch(path, opts) {
    return fetch(path, Object.assign({ credentials: 'same-origin' }, opts || {}))
      .then(function (r) { return r.json(); });
  }
  function loadSitePasswords(url) {
    var host = hostOf(url);
    if (!host) return Promise.resolve([]);
    return vaultFetch('/api/vault/passwords?url=' + encodeURIComponent(url))
      .then(function (d) {
        return (d.passwords || []).filter(function (p) { return hostOf(p.url) === host; });
      })
      .catch(function () { return []; });
  }
  function fillCredential(tab, cred) {
    if (!tab || !tab.webview || !cred) return;
    var u = JSON.stringify(String(cred.username || ''));
    var p = JSON.stringify(String(cred.password || ''));
    var js = '(function(u,p){' +
      'var list=Array.prototype.slice.call(document.querySelectorAll("input"));' +
      'function set(el,v){if(!el)return;var d=Object.getOwnPropertyDescriptor(el,"value");' +
      'if(d&&d.set){d.set.call(el,v)}else{el.value=v}' +
      'el.dispatchEvent(new Event("input",{bubbles:true}));' +
      'el.dispatchEvent(new Event("change",{bubbles:true}));}' +
      'var pw=list.filter(function(i){return i.type==="password"&&i.offsetParent!==null;})[0];' +
      'var user=list.filter(function(i){var t=(i.type||"").toLowerCase();' +
      'var n=(i.name||"")+" "+(i.id||"")+" "+(i.autocomplete||"");' +
      'return t!=="password"&&t!=="hidden"&&i.offsetParent!==null&&' +
      '(/user|login|email|mail|phone|account/i.test(n)||t==="email");})[0];' +
      'if(!user){user=list.filter(function(i){return (i.type==="text"||i.type==="email")&&i.offsetParent!==null;})[0];}' +
      'if(user)set(user,u);if(pw)set(pw,p);if(pw)pw.focus();' +
      'return !!(user||pw);})(' + u + ',' + p + ')';
    try {
      tab.webview.executeJavaScript(js, true).then(function (ok) {
        if (ok) toast('Логин и пароль подставлены', 'ok');
      }).catch(function () {});
    } catch (e) { /* webview ещё не готов */ }
  }
  function renderPasswords(tab) {
    if (!elPasswordsList) return;
    var url = tab ? tab.url : '';
    elPasswordsList.innerHTML = '<div class="bb-panel-empty">Загрузка…</div>';
    loadSitePasswords(url).then(function (rows) {
      if (!rows.length) {
        elPasswordsList.innerHTML = '<div class="bb-panel-empty">Для этого сайта паролей нет.<br>' +
          'Добавьте их на рабочем столе: буфер обмена → «Пароли».</div>';
        return;
      }
      elPasswordsList.innerHTML = '';
      rows.forEach(function (p) {
        var row = document.createElement('div');
        row.className = 'bb-panel-item';
        var t = document.createElement('div');
        t.className = 't';
        t.textContent = p.title || p.url || 'Без названия';
        var u = document.createElement('div');
        u.className = 'u';
        u.textContent = [p.username, p.url].filter(Boolean).join(' · ');
        row.appendChild(t); row.appendChild(u);
        row.title = 'Подставить логин и пароль';
        row.addEventListener('click', function () { fillCredential(tab, p); });
        elPasswordsList.appendChild(row);
      });
    });
  }
  function autoFillPasswords(tab) {
    if (!tab || !/^https?:/i.test(tab.url || '')) return;
    loadSitePasswords(tab.url).then(function (rows) {
      // Автоподстановка только когда запись однозначна: иначе пользователь
      // сам выбирает нужную в панели.
      if (rows.length === 1) fillCredential(tab, rows[0]);
    });
  }
  function saveCapturedLogin(tab, data) {
    if (!data || !data.password) return;
    var url = data.url || (tab && tab.url) || '';
    if (!/^https?:/i.test(url)) return;
    var host = hostOf(url);
    if (!host) return;
    loadSitePasswords(url).then(function (rows) {
      var exists = rows.some(function (r) {
        return String(r.username || '') === String(data.username || '') &&
          String(r.password || '') === String(data.password || '');
      });
      if (exists) return;
      vaultFetch('/api/vault/passwords', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: host, url: url, username: data.username || '', password: data.password })
      }).then(function (d) {
        if (!d.error) toast('Пароль сохранён в профиль', 'ok');
      }).catch(function () {});
    });
  }

  /* ---------- Навигация из адресной строки ---------- */
  function goFromOmnibox() {
    var t = activeTab();
    if (!t) return;
    var url = normalizeUrl(elUrl.value);
    if (!url) return;
    t.webview.loadURL(url);
    elUrl.blur();
  }

  /* ---------- Привязка обработчиков ---------- */
  function bind() {
    $('bbTabAdd').addEventListener('click', function () { createTab({ url: HOME }); });
    $('bbClose').addEventListener('click', function () { if (api.closeBrowser) api.closeBrowser(); });
    $('bbModeHalf').addEventListener('click', function () { if (api.setBrowserMode) api.setBrowserMode('half'); });
    $('bbModeFull').addEventListener('click', function () { if (api.setBrowserMode) api.setBrowserMode('full'); });
    $('bbNewWindow').addEventListener('click', function () { if (api.newBrowserWindow) api.newBrowserWindow(); });

    /* Колесо над лентой вкладок крутит её по горизонтали: вкладки растут
       только вправо, вертикальной прокрутки там нет. Без этого при
       переполнении лента ездила лишь перетаскиванием полосы. */
    if (elTabs) {
      elTabs.addEventListener('wheel', function (e) {
        if (elTabs.scrollWidth <= elTabs.clientWidth + 1) return;
        var d = Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
        if (!d) return;
        e.preventDefault();
        elTabs.scrollLeft += d;
      }, { passive: false });
    }

    /* Отдельное окно браузера создаётся без системной рамки (frame:false),
       поэтому свернуть/развернуть его нечем. Кнопки рисуем сами и держим
       скрытыми в доке: там окно принадлежит главному, и его размер задаёт
       док. Показ включает событие browser-mode.
       В режиме 'full' браузер занимает всё окно приложения, а своей панели
       кнопок окна у страницы /browser нет — без этих кнопок окно нельзя
       было ни свернуть, ни развернуть. */
    var minBtn = null, maxBtn = null;
    var setWinButtons = function (mode) {
      var own = mode === 'window' || mode === 'full';
      if (minBtn) minBtn.style.display = own ? '' : 'none';
      if (maxBtn) maxBtn.style.display = own ? '' : 'none';
    };
    var bbWin = document.querySelector('.bb-win');
    var bbClose = $('bbClose');
    if (bbWin && bbClose) {
      minBtn = document.createElement('button');
      minBtn.className = 'bb-win-btn';
      minBtn.id = 'bbMin';
      minBtn.title = 'Свернуть окно';
      minBtn.setAttribute('aria-label', 'Свернуть окно');
      minBtn.innerHTML = '<svg viewBox="0 0 16 16"><path d="M3.5 8h9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
      minBtn.style.display = 'none';
      maxBtn = document.createElement('button');
      maxBtn.className = 'bb-win-btn';
      maxBtn.id = 'bbMax';
      maxBtn.title = 'Развернуть / восстановить окно';
      maxBtn.setAttribute('aria-label', 'Развернуть или восстановить окно');
      maxBtn.innerHTML = '<svg viewBox="0 0 16 16"><rect x="4" y="4" width="8" height="8" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>';
      maxBtn.style.display = 'none';
      bbWin.insertBefore(minBtn, bbClose);
      bbWin.insertBefore(maxBtn, bbClose);
      minBtn.addEventListener('click', function () { if (api.minimize) api.minimize(); });
      maxBtn.addEventListener('click', function () { if (api.maximize) api.maximize(); });
      if (api.onBrowserMode) api.onBrowserMode(setWinButtons);
    }

    /* Ctrl+Shift+H — общий модуль win_nav.js: одно состояние на всё
       приложение, поэтому панель браузера скрывается вместе с кнопками
       окна главного окна, а не отдельно от них. */
    if (bbWin) {
      var applyWinNav = function () {
        if (window.infinityWinNav) { window.infinityWinNav.refresh(); return; }
        var s = document.querySelector('script[src*="win_nav.js"]');
        if (!s) {
          s = document.createElement('script');
          s.src = '/static/js/win_nav.js?v=3';
          s.defer = true;
          s.onload = function () {
            if (window.infinityWinNav) window.infinityWinNav.refresh();
          };
          document.head.appendChild(s);
        }
      };
      applyWinNav();
    }

    /* Разделитель дока. Оболочка сообщает текущий режим событием browser-mode
       (после каждой загрузки страницы и при смене режима); полоса активна
       только в 'half'. При перетаскивании отправляем в главный процесс
       желаемую ширину окна — он перераспределяет её между окнами. */
    var dockHandle = $('bbDockHandle');
    if (dockHandle) {
      if (api.onBrowserMode) {
        api.onBrowserMode(function (mode) { dockHandle.hidden = (mode !== 'half'); });
      }
      dockHandle.addEventListener('pointerdown', function (e) {
        if (dockHandle.hidden || !api.dockResize) return;
        e.preventDefault();
        var startX = e.clientX, startW = window.innerWidth;
        dockHandle.classList.add('dragging');
        try { dockHandle.setPointerCapture(e.pointerId); } catch (err) {}
        var move = function (ev) { api.dockResize(startW - (ev.clientX - startX)); };
        var up = function () {
          dockHandle.classList.remove('dragging');
          dockHandle.removeEventListener('pointermove', move);
          dockHandle.removeEventListener('pointerup', up);
          dockHandle.removeEventListener('pointercancel', up);
        };
        dockHandle.addEventListener('pointermove', move);
        dockHandle.addEventListener('pointerup', up);
        dockHandle.addEventListener('pointercancel', up);
      });
    }

    elBack.addEventListener('click', function () { nav(activeTab(), -1); });
    elFwd.addEventListener('click', function () { nav(activeTab(), 1); });
    $('bbReload').addEventListener('click', function () {
      var t = activeTab(); if (t) t.webview.reload();
    });
    $('bbHome').addEventListener('click', function () {
      var t = activeTab(); if (t) t.webview.loadURL(HOME);
    });
    $('bbIncognito').addEventListener('click', function () { createTab({ url: HOME, incognito: true }); });
    $('bbSavePage').addEventListener('click', function () { savePage(); });
    $('bbTranslate').addEventListener('click', function () { translatePageInline(); });

    // Бейдж масштаба: свой элемент (в разметке его нет — он чисто служебный).
    zoomBadge = document.createElement('div');
    zoomBadge.className = 'bb-zoom-badge';
    zoomBadge.hidden = true;
    $('bbChrome').appendChild(zoomBadge);

    // Панель поиска по странице.
    if (elFind) {
      $('bbFindNext').addEventListener('click', function () { findOnPage(1); });
      $('bbFindPrev').addEventListener('click', function () { findOnPage(-1); });
      $('bbFindClose').addEventListener('click', closeFind);
      elFindInput.addEventListener('input', function () { findOnPage(0); });
      elFindInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); findOnPage(e.shiftKey ? -1 : 1); }
        else if (e.key === 'Escape') { e.preventDefault(); closeFind(); }
      });
    }

    // Поиск по истории — перерисовываем список на каждый ввод.
    if (elHistorySearch) {
      elHistorySearch.addEventListener('input', function () { renderHistory(); });
      elHistorySearch.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') { e.preventDefault(); togglePanel(elHistoryPanel, false); }
      });
    }

    elUrl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); goFromOmnibox(); }
      else if (e.key === 'Escape') { var t = activeTab(); if (t) syncToolbar(t); elUrl.blur(); }
    });
    // При фокусе выделяем весь адрес — как в реальных браузерах.
    elUrl.addEventListener('focus', function () { elUrl.select(); });

    elStar.addEventListener('click', toggleBookmark);

    $('bbHistoryBtn').addEventListener('click', function () {
      renderHistory(); togglePanel(elHistoryPanel);
    });
    $('bbDownloadsBtn').addEventListener('click', function () {
      renderDownloads(); togglePanel(elDownloadsPanel);
    });
    var elPwBtn = $('bbPasswordsBtn');
    if (elPwBtn) elPwBtn.addEventListener('click', function () {
      renderPasswords(activeTab()); togglePanel(elPasswordsPanel);
    });
    var elPwVault = $('bbPasswordsOpenVault');
    if (elPwVault) elPwVault.addEventListener('click', function () {
      try { location.href = '/desktop'; } catch (e) {}
    });
    $('bbHistoryClear').addEventListener('click', function () {
      history = []; saveJSON(LS_HIST, history); renderHistory();
    });

    // Расширения: добавление папки или файла .crx/.zip с ПК (диалог
    // показывает main.js).
    var extAddFolder = $('bbExtAddFolder'), extAddCrx = $('bbExtAddCrx');
    var addExt = function (fn) {
      if (!fn) return;
      Promise.resolve(fn()).then(function (r) {
        if (!r || r.canceled) return;
        if (r.ok) {
          toast('Расширение добавлено', 'ok');
          if (r.warning) toast(r.warning, 'err');
          renderExtensions();
        } else {
          // Короткий тост обрезается многоточием, поэтому технические
          // детали (что именно ответил Electron) уходят в «Подробнее».
          addExtError(r);
        }
      }).catch(function () {});
    };
    if (extAddFolder) extAddFolder.addEventListener('click', function () { addExt(api.extAddFolder); });
    if (extAddCrx) extAddCrx.addEventListener('click', function () { addExt(api.extAddCrx); });

    // Свои плагины: папка или zip (диалог показывает main.js).
    var addPlugin = function (fn) {
      if (!fn) return;
      Promise.resolve(fn()).then(function (r) {
        if (!r || r.canceled) return;
        if (r.ok) { toast('Плагин добавлен — перезагрузите вкладки', 'ok'); renderPlugins(); }
        else toast('Плагин: ' + (r.error || 'не удалось'), 'err');
      }).catch(function () {});
    };
    var plAddFolder = $('bbPluginsAddFolder'), plAddArchive = $('bbPluginsAddArchive');
    if (plAddFolder) plAddFolder.addEventListener('click', function () { addPlugin(api.pluginsAddFolder); });
    if (plAddArchive) plAddArchive.addEventListener('click', function () { addPlugin(api.pluginsAddArchive); });
    $('bbDownloadsClear').addEventListener('click', function () {
      downloads = {}; renderDownloads();
    });
    document.querySelectorAll('[data-close-panel]').forEach(function (b) {
      b.addEventListener('click', function () {
        elHistoryPanel.hidden = true; elDownloadsPanel.hidden = true;
        if (elExtPanel) elExtPanel.hidden = true;
        if (elPluginsPanel) elPluginsPanel.hidden = true;
      });
    });

    // Меню.
    var menuBtn = $('bbMenuBtn');
    menuBtn.addEventListener('click', function (e) {
      e.stopPropagation(); elMenu.hidden = !elMenu.hidden;
      refreshTrackerCount();
    });
    elMenu.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-menu]');
      if (!btn) return;
      var act = btn.getAttribute('data-menu');
      elMenu.hidden = true;
      if (act === 'new-tab') createTab({ url: HOME });
      else if (act === 'incognito') createTab({ url: HOME, incognito: true });
      else if (act === 'bookmarks') elBookmarks.hidden = !elBookmarks.hidden;
      else if (act === 'bookmark-folder') addFolder();
      else if (act === 'history') { renderHistory(); togglePanel(elHistoryPanel); }
      else if (act === 'downloads') { renderDownloads(); togglePanel(elDownloadsPanel); }
      else if (act === 'save-page') { savePage(); }
      else if (act === 'find') openFind();
      else if (act === 'reader') toggleReader();
      else if (act === 'translate' || act === 'translate-inline') translatePageInline();
      else if (act === 'print-pdf') printPdf();
      else if (act === 'open-viewer') savePage();
      else if (act === 'ask-ai') askAiAboutPage();
      else if (act === 'extensions') { renderExtensions(); togglePanel(elExtPanel); }
      else if (act === 'plugins') { renderPlugins(); togglePanel(elPluginsPanel); }
      else if (act === 'zoom-reset') zoomReset();
      else if (act === 'home') { var t = activeTab(); if (t) t.webview.loadURL(HOME); }
    });
    document.addEventListener('click', function (e) {
      if (!e.target.closest('#bbMenu') && !e.target.closest('#bbMenuBtn')) elMenu.hidden = true;
    });

    // Меню выделения: показывается по сообщению от guest-preload.
    if (elSelMenu) {
      elSelMenu.addEventListener('mousedown', function (e) { e.preventDefault(); });
      elSelMenu.addEventListener('click', function (e) {
        var b = e.target.closest('[data-sel]');
        if (!b) return;
        var act = b.getAttribute('data-sel');
        var text = selText;
        hideSelMenu();
        var run = function (txt) {
          txt = String(txt || '').trim();
          if (!txt) { toast('Нет выделенного текста', 'err'); return; }
          if (act === 'notes') saveSelectionToNotes(txt);
          else if (act === 'ask') { if (api.askAiBar) api.askAiBar(txt + '\n(выделено на странице ' + (activeTab() ? activeTab().url : '') + ')'); }
          else if (act === 'copy') {
            try { navigator.clipboard.writeText(txt).then(function () { toast('Скопировано', 'ok'); }); } catch (err) {}
          }
        };
        /* Если guest-preload не прислал текст (или он пуст) — читаем выделение
           из самой страницы, чтобы «В заметки» не молчал. */
        if (text) run(text);
        else currentSelection().then(run);
      });
    }
    var wordClose = $('bbWordClose');
    if (wordClose) wordClose.addEventListener('click', hideWordPop);
    // Клик мимо всплывающих карточек закрывает их.
    document.addEventListener('mousedown', function (e) {
      if (elSelMenu && !e.target.closest('#bbSelMenu')) hideSelMenu();
      if (elWordPop && !e.target.closest('#bbWordPop')) hideWordPop();
    });

    // Горячие клавиши браузера.
    document.addEventListener('keydown', function (e) {
      var mod = e.ctrlKey || e.metaKey;
      var k = (e.key || '').toLowerCase();
      if (mod && !e.shiftKey && k === 't') { e.preventDefault(); createTab({ url: HOME }); }
      else if (mod && e.shiftKey && k === 'n') { e.preventDefault(); createTab({ url: HOME, incognito: true }); }
      else if (mod && e.shiftKey && k === 'b') { e.preventDefault(); elBookmarks.hidden = !elBookmarks.hidden; }
      else if (mod && k === 'h') { e.preventDefault(); renderHistory(); togglePanel(elHistoryPanel); }
      else if (mod && k === 'j') { e.preventDefault(); renderDownloads(); togglePanel(elDownloadsPanel); }
      else if (mod && e.shiftKey && k === 'x') { e.preventDefault(); renderExtensions(); togglePanel(elExtPanel); }
      else if (mod && e.shiftKey && k === 'p') { e.preventDefault(); renderPlugins(); togglePanel(elPluginsPanel); }
      else if (mod && k === 'l') { e.preventDefault(); elUrl.focus(); }
      else if (mod && k === 'r') { e.preventDefault(); var t = activeTab(); if (t) t.webview.reload(); }
      else if (mod && k === 'w') { e.preventDefault(); var t2 = activeTab(); if (t2) closeTab(t2.id); }
      // Ctrl+S — «Сохранить страницу» самого сайта мы перехватываем: в
      // frameless-окне диалог браузера сохранял бы только каркас страницы.
      else if (mod && !e.shiftKey && k === 's') { e.preventDefault(); savePage(); }
      // Поиск по странице.
      else if (mod && !e.shiftKey && k === 'f') { e.preventDefault(); openFind(); }
      // Печать в PDF.
      else if (mod && !e.shiftKey && k === 'p') { e.preventDefault(); printPdf(); }
      // Вопрос ИИ о странице/выделении.
      else if (mod && e.shiftKey && k === 'q') { e.preventDefault(); askAiAboutPage(); }
      // Масштаб.
      else if (mod && (e.key === '=' || e.key === '+')) { e.preventDefault(); zoomBy(1); }
      else if (mod && (e.key === '-' || e.key === '_')) { e.preventDefault(); zoomBy(-1); }
      else if (mod && k === '0') { e.preventDefault(); zoomReset(); }
      else if (!mod && k === 'f9') { e.preventDefault(); toggleReader(); }
      else if (!mod && k === 'f10') { e.preventDefault(); translatePageInline(); }
      else if (e.key === 'Escape') {
        if (readerOpen()) closeReader();
        else if (elFind && !elFind.hidden) closeFind();
        closeAnyMenu();
      }
    });

    // Загрузки и «открыть в новой вкладке» из main.js.
    if (api.onBrowserDownload) api.onBrowserDownload(onDownload);
    if (api.onBrowserOpenTab) api.onBrowserOpenTab(function (url) {
      createTab({ url: url });
    });
  }

  /* ---------- Старт ---------- */
  renderBookmarks();
  renderExtIcons();
  bind();
  window.addEventListener('pagehide', saveSession);
  if (!restoreSession()) createTab({ url: HOME });
})();

