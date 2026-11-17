/* Превью-шаблоны редактора заметок (Aurora / Studio / Focus).

   Подключается ТОЛЬКО к копиям-превью (data-notes-skin на <html>),
   page_notes_edit.html его не загружает. Задачи:
     1) переключатель шаблонов редактора с сохранением ?id= и ?collab=;
     2) время чтения в строке метаданных;
     3) единая панель: навигация, инструменты по центру, «Ещё» при прокрутке;
     4) общая для всех шаблонов панель «Структура» (из Studio);
     5) «Машинка» (из Focus), режим фокусировки и прозрачная панель в меню.

   Существующую логику редактора не переопределяет: только добавляет свои
   элементы и слушатели; каждая инициализация изолирована try/catch. */
(function () {
  'use strict';

  var root = document.documentElement;
  var skin = root.getAttribute('data-notes-skin') || '';
  if (!skin) return;

  /* Общая палитра приложения (command_palette.js) слушает Ctrl+K в
     capture-фазе и гасит страницу. На редакторе у нас своя палитра с
     действиями с текстом, поэтому просим общую уступить клавишу. */
  window.__notesEditorPalette = true;

  var LS_TYPE = 'notes_edit_skin_typewriter';
  var LS_GLASS = 'notes_edit_rail_glass';
  var LS_PROPS = 'notes_edit_hide_props';
  var LS_OUTLINE = 'notes_edit_outline_open';
  var rail = null;
  var railExtra = null;
  var outlinePanel = null;
  var outlineTimer = 0;

  function store(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* приватный режим */ }
  }
  function load(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html) node.innerHTML = html;
    return node;
  }

  /* ── Единая закреплённая панель ───────────────────────────────────────
     Навигация, инструменты форматирования и особенности шаблона живут в
     одной строке. Инструменты стоят по центру панели, навигация — слева,
     при прокрутке панель прилипает к верху, а «Заметки» и «Папки»
     сворачиваются в меню «Ещё»: их пункты появляются в начале списка,
     сама кнопка остаётся на месте. Разметку не дублируем — оборачиваем
     существующие .toolbar и .format-bar, поэтому все id и обработчики
     редактора продолжают работать. */
  function initRail() {
    var toolbar = document.getElementById('edToolbar');
    var fmt = document.querySelector('.format-bar');
    if (!toolbar || !fmt || toolbar.parentNode !== fmt.parentNode) return;

    rail = el('div', 'ed-rail');
    rail.id = 'edRail';
    toolbar.parentNode.insertBefore(rail, toolbar);
    rail.appendChild(toolbar);
    rail.appendChild(fmt);

    railExtra = el('div', 'ed-rail-extra');
    railExtra.id = 'edRailExtra';
    rail.appendChild(railExtra);

    var navBtns = toolbar.querySelectorAll(':scope > .btn.ed-ghost');
    for (var i = 0; i < navBtns.length && i < 2; i++) {
      navBtns[i].classList.add('ed-nav-item');
    }

    var menu = document.getElementById('edMenu');
    if (menu) {
      var col = menu.querySelector('.ed-menu-col');
      var block = el('div', 'ed-menu-compact');
      block.innerHTML =
        '<div class="ed-menu-title">Навигация</div>' +
        '<a class="btn ed-compact-only" href="/notes" role="menuitem">' +
        '<i class="icon fa-solid fa-arrow-left"></i><span>К списку заметок</span></a>' +
        '<button class="btn ed-compact-only" type="button" onclick="toggleNotesExplorer()" role="menuitem">' +
        '<i class="icon fa-solid fa-folder-tree"></i><span>Папки и заметки</span></button>' +
        '<div class="ed-sep ed-compact-only"></div>';
      col.insertBefore(block, col.firstChild);
    }

    rail.appendChild(el('span', 'ed-rail-progress'));

    /* Ширина боковых зон: по ней CSS ограничивает инструменты, чтобы они
       не наезжали на навигацию и особенности шаблона. */
    function measure() {
      var side = Math.max(toolbar.offsetWidth, railExtra.offsetWidth) + 12;
      rail.style.setProperty('--ed-side', side + 'px');
    }

    var frame = 0;
    function update() {
      frame = 0;
      var y = window.pageYOffset || document.documentElement.scrollTop || 0;
      rail.classList.toggle('is-stuck', y > 24);
      var max = document.documentElement.scrollHeight - window.innerHeight;
      rail.style.setProperty('--ed-progress',
        max > 0 ? Math.min(1, y / max).toFixed(4) : 0);
      measure();
    }
    function schedule() {
      if (!frame) frame = requestAnimationFrame(update);
    }
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    try { new ResizeObserver(schedule).observe(toolbar); } catch (e) { /* старый движок */ }
    try { new ResizeObserver(schedule).observe(railExtra); } catch (e) { /* старый движок */ }
    update();

    /* Особенности шаблонов: Aurora и Studio получают счётчик слов, все
       шаблоны — кнопку фокусировки и пункты меню «Структура»/«Свойства». */
    if (load(LS_GLASS) === '1') rail.classList.add('ed-glass');
    var glassChk = document.getElementById('edGlassChk');
    if (glassChk) glassChk.checked = rail.classList.contains('ed-glass');

    if (skin === 'aurora' || skin === 'studio') {
      initStatsChip();
      initOutlineButton();
    }
    initFocusButton();
    if (skin === 'focus') initFocusRail();
  }

  /* Aurora/Studio: живой счётчик слов в панели. */
  function initStatsChip() {
    var chip = el('div', 'ed-stat');
    chip.title = 'Слов в заметке';
    chip.innerHTML = '<i class="fa-solid fa-font"></i><b id="edStatWords">0</b>';
    railExtra.appendChild(chip);

    var words = document.getElementById('wordInfo');
    var value = document.getElementById('edStatWords');
    function renderWords() {
      if (!words || !value) return;
      var m = /([\d\s\u00a0]+)/.exec(words.textContent || '');
      value.textContent = m ? m[1].replace(/[\s\u00a0]/g, '') || '0' : '0';
    }
    renderWords();
    if (words) {
      try {
        new MutationObserver(renderWords).observe(words,
          { childList: true, characterData: true, subtree: true });
      } catch (e) { /* счётчик — украшение */ }
    }
  }

  /* Кнопка «Структура» в панели (Aurora, Studio). */
  function initOutlineButton() {
    var btn = el('button', 'ed-extra-btn');
    btn.type = 'button';
    btn.id = 'edOutlineRailBtn';
    btn.title = 'Структура заметки';
    btn.setAttribute('aria-label', 'Структура заметки');
    btn.innerHTML = '<i class="fa-solid fa-list-ul"></i>';
    btn.addEventListener('click', function () { edToggleOutline(); });
    railExtra.appendChild(btn);
  }

  /* Режим фокусировки вместо «Дзена»: та же кнопка-стрелка, что была,
     но включает штатный focus-режим редактора (выход — Esc или «Выйти»). */
  function initFocusButton() {
    var btn = el('button', 'ed-extra-btn skin-focus-btn');
    btn.type = 'button';
    btn.title = 'Режим фокусировки';
    btn.setAttribute('aria-label', 'Режим фокусировки');
    btn.innerHTML = '<i class="fa-solid fa-expand"></i>';
    btn.addEventListener('click', function () {
      if (typeof window.toggleFocusMode === 'function') window.toggleFocusMode();
      sync();
    });
    railExtra.appendChild(btn);

    function sync() {
      var on = document.body.classList.contains('notes-focus');
      btn.classList.toggle('on', on);
      btn.innerHTML = '<i class="fa-solid ' + (on ? 'fa-compress' : 'fa-expand') + '"></i>';
      btn.title = on ? 'Выйти из режима фокусировки (Esc)' : 'Режим фокусировки';
    }
    sync();
    try {
      new MutationObserver(sync).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    } catch (e) { /* старый движок */ }

    /* Карандаш у текста убран, а панель в фокусе скрыта — без видимой
       кнопки выхода режим выглядел ловушкой. Возвращаем её плашкой. */
    var exit = el('button', 'ed-focus-exit');
    exit.type = 'button';
    exit.title = 'Выйти из режима фокусировки (Esc)';
    exit.innerHTML = '<i class="fa-solid fa-compress"></i><span>Выйти из фокусировки</span>';
    exit.addEventListener('click', function () {
      if (typeof window.toggleFocusMode === 'function') window.toggleFocusMode();
      sync();
    });
    document.body.appendChild(exit);
  }

  /* Прозрачная панель — опция из меню «Ещё». */
  window.edSetGlass = function (on) {
    if (!rail) return;
    rail.classList.toggle('ed-glass', !!on);
    store(LS_GLASS, on ? '1' : '0');
  };

  /* Скрыть/показать теги и строку свойств. */
  window.edToggleProps = function () {
    var hidden = document.body.classList.toggle('ed-props-hidden');
    store(LS_PROPS, hidden ? '1' : '0');
    var btn = document.getElementById('edPropsBtn');
    if (btn) btn.classList.toggle('on', hidden);
  };

  /* ── «Структура»: оглавление по заголовкам ─────────────────────────────
     Панель общая для всех шаблонов. Раньше она перерисовывалась каждые 1.2 с
     даже без изменений — мигал hover, сбрасывались прокрутка и фокус, а
     активный раздел не подсвечивался вовсе. Теперь перерисовка только при
     смене заголовков, активный пункт считается по позиции текста, уровни
     сворачиваются и помнят состояние. */
  var OUTLINE_COLLAPSED_KEY = 'notes_edit_outline_collapsed';
  var outlineState = {
    sig: '',
    items: [],
    positions: [],
    collapsed: {},
    activeKey: '',
    caretAbsY: -1,
    caretTimer: 0,
    refreshTimer: 0,
    frame: 0
  };
  var outlineScrollBound = false;

  function outlineIsOpen() {
    return !!(outlinePanel && outlinePanel.classList.contains('open'));
  }

  function outlineHeadings() {
    var ta = document.getElementById('content');
    if (!ta) return [];
    var lines = (ta.value || '').split('\n');
    var out = [], fence = false;
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (/^\s*(```|~~~)/.test(line)) { fence = !fence; continue }
      if (fence) continue;
      var m = /^(#{1,3})\s+(.+?)\s*#*\s*$/.exec(line);
      if (m) out.push({ level: m[1].length, text: m[2], line: i });
    }
    return out;
  }

  function jumpToHeading(lineNo) {
    var ta = document.getElementById('content');
    if (!ta) return;
    var lines = ta.value.split('\n');
    var pos = 0;
    for (var i = 0; i < lineNo && i < lines.length; i++) pos += lines[i].length + 1;
    ta.focus();
    try { ta.setSelectionRange(pos, pos) } catch (e) { /* не критично */ }
    var y = caretOffset(ta, pos);
    var rect = ta.getBoundingClientRect();
    var top = window.pageYOffset + rect.top + y - Math.round(window.innerHeight * 0.32);
    try { window.scrollTo({ top: Math.max(0, top), behavior: 'smooth' }) }
    catch (e) { window.scrollTo(0, Math.max(0, top)) }
    outlineState.caretAbsY = window.pageYOffset + rect.top + y;
    scheduleCaretRef();
  }

  function outlineBody() {
    return outlinePanel ? outlinePanel.querySelector('.oe-outline-body') : null;
  }

  function outlineKey(h) { return h.level + '|' + h.text }

  function outlineChildren(items, i) {
    var out = [];
    for (var j = i + 1; j < items.length; j++) {
      if (items[j].level <= items[i].level) break;
      out.push(j);
    }
    return out;
  }

  function outlineLoadCollapsed() {
    try {
      var raw = load(OUTLINE_COLLAPSED_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      if (Object.prototype.toString.call(arr) === '[object Array]') {
        arr.forEach(function (k) { outlineState.collapsed[k] = 1 });
      }
    } catch (e) { /* приватный режим */ }
  }

  function outlineSaveCollapsed() {
    try {
      store(OUTLINE_COLLAPSED_KEY, JSON.stringify(Object.keys(outlineState.collapsed)));
    } catch (e) { /* приватный режим */ }
  }

  function outlineSignature(items) {
    return items.map(function (h) { return h.level + ':' + h.line + ':' + h.text }).join('|');
  }

  function outlineVisible(items) {
    var visible = [];
    var hideBelow = 0;
    items.forEach(function (h, i) {
      if (hideBelow && h.level > hideBelow) return;
      hideBelow = 0;
      visible.push(i);
      if (outlineState.collapsed[outlineKey(h)] && outlineChildren(items, i).length) {
        hideBelow = h.level;
      }
    });
    return visible;
  }

  function outlineItemY(ta, line) {
    var lines = ta.value.split('\n');
    var pos = 0;
    for (var i = 0; i < line && i < lines.length; i++) pos += lines[i].length + 1;
    var rect = ta.getBoundingClientRect();
    return window.pageYOffset + rect.top + caretOffset(ta, pos);
  }

  function outlineMeasure(items) {
    var ta = document.getElementById('content');
    outlineState.positions = [];
    if (!ta) return;
    for (var i = 0; i < items.length; i++) {
      outlineState.positions.push(outlineItemY(ta, items[i].line));
    }
  }

  function outlineReferenceY(ta) {
    var top = window.pageYOffset || document.documentElement.scrollTop || 0;
    if (document.activeElement === ta && outlineState.caretAbsY >= 0) {
      if (outlineState.caretAbsY >= top && outlineState.caretAbsY <= top + window.innerHeight) {
        return outlineState.caretAbsY;
      }
    }
    return top + window.innerHeight * 0.36;
  }

  function updateOutlineActive() {
    if (!outlinePanel || !outlineIsOpen()) return;
    var ta = document.getElementById('content');
    if (!ta || !outlineState.items.length) return;
    var refY = outlineReferenceY(ta);
    var best = -1;
    for (var i = 0; i < outlineState.items.length; i++) {
      if ((outlineState.positions[i] || 0) <= refY + 4) best = i;
      else break;
    }
    if (best < 0) best = 0;
    var key = outlineKey(outlineState.items[best]);
    if (key === outlineState.activeKey) return;
    outlineState.activeKey = key;
    var body = outlineBody();
    if (!body) return;
    var rows = body.querySelectorAll('.oe-outline-row');
    var activeRow = null;
    for (var r = 0; r < rows.length; r++) {
      var on = rows[r].getAttribute('data-key') === key;
      rows[r].classList.toggle('active', on);
      if (on) activeRow = rows[r];
    }
    if (activeRow) {
      var top = activeRow.offsetTop;
      var bottom = top + activeRow.offsetHeight;
      if (top < body.scrollTop) body.scrollTop = Math.max(0, top - 4);
      else if (bottom > body.scrollTop + body.clientHeight) {
        body.scrollTop = bottom - body.clientHeight + 4;
      }
    }
  }

  function scheduleActiveUpdate() {
    if (outlineState.frame) return;
    outlineState.frame = requestAnimationFrame(function () {
      outlineState.frame = 0;
      updateOutlineActive();
    });
  }

  function scheduleCaretRef() {
    clearTimeout(outlineState.caretTimer);
    outlineState.caretTimer = setTimeout(function () {
      var ta = document.getElementById('content');
      if (!ta) return;
      outlineState.caretAbsY = window.pageYOffset +
        ta.getBoundingClientRect().top + caretOffset(ta, ta.selectionStart);
      updateOutlineActive();
    }, 160);
  }

  function scheduleOutlineRefresh() {
    clearTimeout(outlineState.refreshTimer);
    outlineState.refreshTimer = setTimeout(function () {
      if (outlineIsOpen()) renderOutline();
    }, 150);
  }

  function renderOutline() {
    if (!outlinePanel) return;
    var body = outlineBody();
    if (!body) return;
    var items = outlineHeadings();
    var sig = outlineSignature(items);
    var count = document.getElementById('oeOutlineCount');
    if (count) count.textContent = items.length ? String(items.length) : '';
    if (sig === outlineState.sig) {
      updateOutlineActive();
      return;
    }
    var prevScroll = body.scrollTop;
    outlineState.sig = sig;
    outlineState.items = items;
    outlineMeasure(items);
    body.innerHTML = '';
    outlineState.activeKey = '';
    if (!items.length) {
      body.innerHTML = '<div class="oe-outline-empty">Заголовков пока нет. ' +
        'Строка «<b># Название</b>» появится здесь.</div>';
      return;
    }
    outlineVisible(items).forEach(function (idx) {
      var h = items[idx];
      var key = outlineKey(h);
      var hasKids = outlineChildren(items, idx).length > 0;
      var collapsed = !!outlineState.collapsed[key];
      var row = el('div', 'oe-outline-row lvl-' + h.level);
      row.setAttribute('data-key', key);
      row.setAttribute('data-index', String(idx));

      var toggle = el('button', 'oe-outline-toggle' + (hasKids ? '' : ' empty') +
        (collapsed ? ' collapsed' : ''));
      toggle.type = 'button';
      toggle.tabIndex = -1;
      toggle.title = collapsed ? 'Развернуть раздел' : 'Свернуть раздел';
      toggle.innerHTML = '<i class="fa-solid fa-chevron-down"></i>';
      if (hasKids) {
        btnToggle(toggle, key);
      }
      row.appendChild(toggle);

      var item = el('button', 'oe-outline-item');
      item.type = 'button';
      item.textContent = h.text;
      item.title = h.text;
      item.addEventListener('click', function () { jumpToHeading(h.line) });
      row.appendChild(item);
      body.appendChild(row);
    });
    body.scrollTop = prevScroll;
    updateOutlineActive();
  }

  function btnToggle(node, key) {
    node.addEventListener('click', function (e) {
      e.stopPropagation();
      if (outlineState.collapsed[key]) delete outlineState.collapsed[key];
      else outlineState.collapsed[key] = 1;
      outlineSaveCollapsed();
      outlineState.sig = '';
      renderOutline();
    });
  }

  function outlineCollapseAll(collapse) {
    outlineState.collapsed = {};
    if (collapse) {
      outlineState.items.forEach(function (h, i) {
        if (outlineChildren(outlineState.items, i).length) {
          outlineState.collapsed[outlineKey(h)] = 1;
        }
      });
    }
    outlineSaveCollapsed();
    outlineState.sig = '';
    renderOutline();
  }

  function syncOutlineButtons() {
    var on = outlineIsOpen();
    ['edOutlineBtn', 'edOutlineRailBtn'].forEach(function (id) {
      var b = document.getElementById(id);
      if (!b) return;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  function outlinePoll(on) {
    if (outlineTimer) { clearInterval(outlineTimer); outlineTimer = 0 }
    if (on) {
      /* Резервный опрос на случай программной загрузки текста (loadNote,
         восстановление версии): сигнатура та же — DOM не трогаем. */
      outlineTimer = setInterval(function () {
        if (outlineIsOpen()) renderOutline();
      }, 700);
    }
  }

  function initOutline() {
    if (outlinePanel) return;
    outlineLoadCollapsed();
    outlinePanel = el('aside', 'oe-outline');
    outlinePanel.id = 'oeOutline';
    outlinePanel.innerHTML =
      '<div class="oe-outline-head"><i class="fa-solid fa-list-ul"></i>' +
      '<span>Структура</span><b class="oe-outline-count" id="oeOutlineCount"></b>' +
      '<button class="oe-outline-mini" type="button" data-outline-act="collapse" ' +
      'title="Свернуть все разделы" aria-label="Свернуть все разделы">' +
      '<i class="fa-solid fa-compress"></i></button>' +
      '<button class="oe-outline-mini" type="button" data-outline-act="expand" ' +
      'title="Развернуть все разделы" aria-label="Развернуть все разделы">' +
      '<i class="fa-solid fa-expand"></i></button>' +
      '<button class="oe-outline-close" type="button" title="Закрыть структуру" ' +
      'aria-label="Закрыть структуру" onclick="edToggleOutline(false)">' +
      '<i class="fa-solid fa-xmark"></i></button></div>' +
      '<div class="oe-outline-body" id="oeOutlineBody"></div>';
    document.body.appendChild(outlinePanel);

    outlinePanel.addEventListener('click', function (e) {
      var act = e.target.closest && e.target.closest('[data-outline-act]');
      if (!act) return;
      outlineCollapseAll(act.getAttribute('data-outline-act') === 'collapse');
    });

    /* ↑/↓ по пунктам без перехвата обычного Tab. */
    outlinePanel.addEventListener('keydown', function (e) {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      var body = outlineBody();
      if (!body) return;
      var focusables = Array.prototype.slice.call(body.querySelectorAll('.oe-outline-item'));
      if (!focusables.length) return;
      var idx = focusables.indexOf(document.activeElement);
      idx = idx < 0 ? 0 : Math.max(0, Math.min(focusables.length - 1,
        idx + (e.key === 'ArrowDown' ? 1 : -1)));
      e.preventDefault();
      focusables[idx].focus();
    });

    var ta = document.getElementById('content');
    if (ta) {
      ta.addEventListener('input', function () {
        scheduleOutlineRefresh();
        scheduleCaretRef();
      });
      ta.addEventListener('keyup', scheduleCaretRef);
      ta.addEventListener('click', scheduleCaretRef);
    }
    document.addEventListener('selectionchange', function () {
      if (document.activeElement === ta) scheduleCaretRef();
    });

    if (!outlineScrollBound) {
      outlineScrollBound = true;
      window.addEventListener('scroll', scheduleActiveUpdate, { passive: true });
      window.addEventListener('resize', function () {
        outlineState.sig = '';
        if (outlineIsOpen()) renderOutline();
      });
    }

    /* Заметка подгружается асинхронно: пока панель открыта, её обновляет
       опрос; после закрытия таймер не крутится. */
    if (load(LS_OUTLINE) === '1') {
      outlinePanel.classList.add('open');
      renderOutline();
      outlinePoll(true);
      syncOutlineButtons();
    }

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' || !outlineIsOpen()) return;
      if (document.body.classList.contains('notes-focus')) return;
      var modals = document.querySelectorAll('.modal-bg');
      for (var i = 0; i < modals.length; i++) {
        if (getComputedStyle(modals[i]).display !== 'none') return;
      }
      window.edToggleOutline(false);
    }, true);
  }

  window.edToggleOutline = function (force) {
    if (!outlinePanel) return;
    var open = typeof force === 'boolean' ? force : !outlineIsOpen();
    outlinePanel.classList.toggle('open', open);
    store(LS_OUTLINE, open ? '1' : '0');
    if (open) renderOutline();
    outlinePoll(open);
    syncOutlineButtons();
  };

  /* Focus: панель гаснет, пока идёт набор, и возвращается от движения мыши. */
  function initFocusRail() {
    var ta = document.getElementById('content');
    var timer = 0;
    if (ta) {
      ta.addEventListener('input', function () {
        rail.classList.add('is-dim');
        clearTimeout(timer);
        timer = setTimeout(function () { rail.classList.remove('is-dim'); }, 1600);
      });
    }
    document.addEventListener('mousemove', function () {
      rail.classList.remove('is-dim');
    }, { passive: true });
  }

  /* ── Командная палитра (Ctrl+K) и горячие клавиши ──────────────────────
     Реализация общая для всех шаблонов: палитра зовёт уже существующие
     функции страницы (save, версии, экспорт, шаблоны), поэтому поведение
     одинаковое, а код не дублируется. */
  var palette = null;
  var paletteSearchTimer = 0;
  var paletteItems = [];
  var paletteIndex = 0;
  var paletteQuery = '';

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function pageFn(name) {
    return typeof window[name] === 'function' ? window[name] : null;
  }

  function callPage(name) {
    var fn = pageFn(name);
    if (!fn) return false;
    var args = Array.prototype.slice.call(arguments, 1);
    fn.apply(window, args);
    return true;
  }

  function editorTextarea() {
    return document.getElementById('content');
  }

  function fireEditorInput(ta) {
    try { ta.dispatchEvent(new Event('input', { bubbles: true })) } catch (e) { /* не критично */ }
    if (pageFn('updateStats')) window.updateStats();
    if (pageFn('scheduleAutoSave')) window.scheduleAutoSave();
  }

  function editorFormat(before, after) {
    if (pageFn('insertFormat')) window.insertFormat(before, after);
  }

  function editorLine(prefix) {
    if (pageFn('insertLine')) window.insertLine(prefix);
  }

  function moveEditorLine(dir) {
    var ta = editorTextarea();
    if (!ta) return;
    var v = ta.value, s = ta.selectionStart, e = ta.selectionEnd;
    var lineStart = v.lastIndexOf('\n', s - 1) + 1;
    var lineEnd = v.indexOf('\n', e);
    if (lineEnd < 0) lineEnd = v.length;
    var block = v.slice(lineStart, lineEnd);
    if (dir < 0) {
      if (lineStart === 0) return;
      var prevStart = v.lastIndexOf('\n', lineStart - 2) + 1;
      var prev = v.slice(prevStart, lineStart - 1);
      ta.value = v.slice(0, prevStart) + block + '\n' + prev + v.slice(lineEnd);
      var up = prev.length + 1;
      ta.selectionStart = s - up;
      ta.selectionEnd = e - up;
    } else {
      if (lineEnd >= v.length) return;
      var nextStart = lineEnd + 1;
      var nextEnd = v.indexOf('\n', nextStart);
      if (nextEnd < 0) nextEnd = v.length;
      var next = v.slice(nextStart, nextEnd);
      ta.value = v.slice(0, lineStart) + next + '\n' + block + v.slice(nextEnd);
      var down = next.length + 1;
      ta.selectionStart = s + down;
      ta.selectionEnd = e + down;
    }
    ta.focus();
    fireEditorInput(ta);
  }

  function duplicateEditorLine() {
    var ta = editorTextarea();
    if (!ta) return;
    var v = ta.value, s = ta.selectionStart, e = ta.selectionEnd;
    var lineStart = v.lastIndexOf('\n', s - 1) + 1;
    var lineEnd = v.indexOf('\n', e);
    if (lineEnd < 0) lineEnd = v.length;
    var block = v.slice(lineStart, lineEnd);
    var insert = '\n' + block;
    ta.value = v.slice(0, lineEnd) + insert + v.slice(lineEnd);
    ta.selectionStart = s + insert.length;
    ta.selectionEnd = e + insert.length;
    ta.focus();
    fireEditorInput(ta);
  }

  /* ── AI-теги: 3–5 тегов из существующего словаря по тексту (Laya) ───── */
  window.editorAiTags = function () {
    var row = document.getElementById('tagsRow');
    if (!row) { if (pageFn('toast')) window.toast('Строка тегов недоступна', 'info'); return }
    /* Строка тегов/свойств по умолчанию скрыта. Подсказка внутри неё была
       бы невидимой, поэтому открываем строку — иначе кнопка «ИИ-теги»
       выглядела бы сломанной. */
    if (document.body.classList.contains('ed-props-hidden')) window.edToggleProps();
    var existing = row.querySelector('.ai-tag-sug');
    if (existing) { existing.remove(); return }
    var ta = editorTextarea();
    var titleEl = document.getElementById('title');
    var text = ((titleEl ? titleEl.value : '') + '\n' +
      (ta ? ta.value.slice(0, 4000) : '')).trim();
    if (!text) { if (pageFn('toast')) window.toast('Заметка пустая', 'info'); return }
    var pop = el('div', 'ai-tag-sug');
    pop.innerHTML = '<div class="ai-tag-sug-head"><i class="fa-solid fa-wand-magic-sparkles"></i> Подбираю теги…</div>';
    row.appendChild(pop);
    fetch('/api/notes/ai-tags', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text })
    }).then(function (r) { return r.json() }).then(function (d) {
      if (!d || d.error) throw new Error((d && d.error) || 'не удалось получить теги');
      var have = {};
      row.querySelectorAll('.tag-pill').forEach(function (p) {
        have[(p.textContent || '').replace(/^#/, '').trim()] = 1;
      });
      var tags = (d.tags || []).map(function (t) { return t.tag || t })
        .filter(function (t) { return t && !have[t] });
      if (!tags.length) {
        pop.innerHTML = '<div class="ai-tag-sug-head"><i class="fa-solid fa-circle-info"></i> ' +
          escapeHtml(d.hint || 'Новых тегов не нашлось') + '</div>';
        setTimeout(function () { if (pop.parentNode) pop.remove() }, 3500);
        return;
      }
      pop.innerHTML = '<div class="ai-tag-sug-head"><i class="fa-solid fa-wand-magic-sparkles"></i> ' +
        'Предложенные теги — нажмите, чтобы добавить</div>';
      tags.forEach(function (t) {
        var chip = el('button', 'ai-tag-chip');
        chip.type = 'button';
        chip.textContent = '#' + t;
        chip.title = 'Добавить тег «' + t + '»';
        chip.addEventListener('click', function () {
          var input = document.getElementById('tagInput');
          if (!input || !pageFn('commitTagInput')) return;
          input.value = t;
          window.commitTagInput(input);
          chip.classList.add('added');
          chip.disabled = true;
        });
        pop.appendChild(chip);
      });
      var close = el('button', 'ai-tag-sug-close');
      close.type = 'button';
      close.title = 'Скрыть';
      close.innerHTML = '<i class="fa-solid fa-xmark"></i>';
      close.addEventListener('click', function () { pop.remove() });
      pop.appendChild(close);
    }).catch(function (err) {
      pop.innerHTML = '<div class="ai-tag-sug-head"><i class="fa-solid fa-triangle-exclamation"></i> ' +
        escapeHtml(err.message) + '</div>';
      setTimeout(function () { if (pop.parentNode) pop.remove() }, 3500);
    });
  };

  function initAiTagsButton() {
    var row = document.getElementById('tagsRow');
    if (!row || row.querySelector('.tag-ai-btn')) return;
    var btn = el('button', 'tag-ai-btn');
    btn.type = 'button';
    btn.title = 'Подобрать теги по тексту (Laya)';
    btn.setAttribute('aria-label', 'Подобрать теги по тексту');
    btn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i>';
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      window.editorAiTags();
    });
    row.appendChild(btn);
  }

  /* ── Палитра ────────────────────────────────────────────────────────── */
  function paletteActions() {
    function act(title, hint, icon, fnName, args) {
      return {
        kind: 'action', group: 'Действия', title: title, hint: hint || '',
        icon: icon,
        run: function () { callPage.apply(null, [fnName].concat(args || [])) }
      };
    }
    return [
      act('Сохранить заметку', 'Ctrl+S', 'fa-floppy-disk', 'save'),
      act('История версий', '', 'fa-clock-rotate-left', 'openVersionsModal'),
      act('Найти и заменить', 'Ctrl+F', 'fa-magnifying-glass', 'openFindBar', [false]),
      act('Вставить шаблон', '', 'fa-paste', 'openTemplatePicker'),
      act('Сохранить как шаблон', '', 'fa-copy', 'saveAsTemplate'),
      act('Экспорт: Markdown', '', 'fa-file-lines', 'exportCurrentNote', ['markdown']),
      act('Экспорт: HTML', '', 'fa-file-code', 'exportCurrentNote', ['html']),
      act('Экспорт всех заметок в ZIP', '', 'fa-file-zipper', 'exportNotesZip'),
      act('Превью', '', 'fa-eye', 'togglePreview'),
      act('Сплит: текст и превью', '', 'fa-table-columns', 'toggleSplitMode'),
      act('Фокусировка', '', 'fa-compress', 'toggleFocusMode'),
      act('Структура заметки', 'Ctrl+Alt+O', 'fa-list-ul', 'edToggleOutline'),
      act('Свойства и теги', 'Ctrl+Alt+P', 'fa-tags', 'edToggleProps'),
      act('Вставить таблицу', '', 'fa-table-cells', 'insertTable'),
      act('AI-теги для заметки', '', 'fa-wand-magic-sparkles', 'editorAiTags'),
      act('Справка по Markdown', 'Ctrl+/', 'fa-circle-question', 'openCheatsheet')
    ];
  }

  function paletteMatches(q) {
    var out = [];
    var all = paletteActions();
    for (var i = 0; i < all.length; i++) {
      if (!q || all[i].title.toLowerCase().indexOf(q.toLowerCase()) >= 0) out.push(all[i]);
    }
    return out;
  }

  function paletteShow(items) {
    paletteItems = items;
    paletteIndex = 0;
    var list = palette.querySelector('#pkList');
    if (!list) return;
    if (!items.length) {
      list.innerHTML = '<div class="pk-empty">Ничего не найдено</div>';
      return;
    }
    var html = '', last = '';
    items.forEach(function (it, i) {
      if (it.group !== last) {
        html += '<div class="pk-cap">' + escapeHtml(it.group) + '</div>';
        last = it.group;
      }
      html += '<button type="button" class="pk-row' + (i === 0 ? ' on' : '') + '" data-i="' + i + '">' +
        '<i class="fa-solid ' + (it.icon || 'fa-arrow-right') + '"></i>' +
        '<span class="pk-title">' + escapeHtml(it.title) + '</span>' +
        (it.hint ? '<span class="pk-hint">' + escapeHtml(it.hint) + '</span>' : '') +
        '</button>';
    });
    list.innerHTML = html;
    list.querySelectorAll('.pk-row').forEach(function (row) {
      row.addEventListener('click', function () {
        var it = paletteItems[parseInt(row.getAttribute('data-i'), 10)];
        if (it) paletteRun(it);
      });
    });
  }

  function paletteHighlight() {
    var list = palette ? palette.querySelector('#pkList') : null;
    if (!list) return;
    var rows = list.querySelectorAll('.pk-row');
    var active = null;
    rows.forEach(function (row) {
      var on = parseInt(row.getAttribute('data-i'), 10) === paletteIndex;
      row.classList.toggle('on', on);
      if (on) active = row;
    });
    if (active && active.scrollIntoView) {
      active.scrollIntoView({ block: 'nearest' });
    }
  }

  function paletteRun(it) {
    closePalette();
    if (!it) return;
    if (it.kind === 'note') {
      location.href = '/notes/edit?id=' + encodeURIComponent(it.id);
      return;
    }
    if (it.kind === 'tag') {
      if (!callPage('insertTextAtCursor', '#' + it.tag + ' ')) {
        if (pageFn('toast')) window.toast('#' + it.tag, 'info');
      }
      return;
    }
    if (typeof it.run === 'function') it.run();
  }

  function paletteKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closePalette();
      return;
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!paletteItems.length) return;
      e.preventDefault();
      paletteIndex = e.key === 'ArrowDown'
        ? (paletteIndex + 1) % paletteItems.length
        : (paletteIndex - 1 + paletteItems.length) % paletteItems.length;
      paletteHighlight();
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      var it = paletteItems[paletteIndex];
      if (it) paletteRun(it);
    }
  }

  function runPaletteSearch() {
    var q = paletteQuery;
    paletteShow(paletteMatches(q));
    clearTimeout(paletteSearchTimer);
    if (!q) return;
    paletteSearchTimer = setTimeout(function () {
      if (!palette) return;
      fetch('/api/notes/search-all?q=' + encodeURIComponent(q) + '&limit=12')
        .then(function (r) { return r.json() })
        .then(function (d) {
          if (!palette || q !== paletteQuery) return;
          var out = paletteMatches(q);
          (d.notes || []).forEach(function (n) {
            out.push({
              kind: 'note', group: 'Заметки', icon: 'fa-note-sticky',
              title: n.title || 'Без названия', hint: n.folder || '', id: n.id
            });
          });
          (d.tags || []).forEach(function (t) {
            out.push({
              kind: 'tag', group: 'Теги', icon: 'fa-hashtag',
              title: '#' + t.tag, hint: 'вставить в текст', tag: t.tag
            });
          });
          paletteShow(out);
        }).catch(function () { /* поиск — подсказка, не критичен */ });
    }, 180);
  }

  function closePalette() {
    if (!palette) return;
    palette.remove();
    palette = null;
    paletteItems = [];
  }

  function openPalette() {
    if (palette) { closePalette(); return }
    paletteQuery = '';
    palette = el('div', 'pk-back');
    palette.innerHTML =
      '<div class="pk" role="dialog" aria-label="Командная палитра">' +
      '<div class="pk-head"><i class="fa-solid fa-bolt"></i>' +
      '<input id="pkInput" type="text" placeholder="Команда, заметка или тег…" ' +
      'autocomplete="off" spellcheck="false">' +
      '<kbd>Esc</kbd></div>' +
      '<div class="pk-list" id="pkList"></div>' +
      '<div class="pk-foot"><span><kbd>↑</kbd><kbd>↓</kbd> выбор</span>' +
      '<span><kbd>Enter</kbd> открыть</span><span>Ctrl+K — закрыть</span></div>' +
      '</div>';
    document.body.appendChild(palette);
    var input = palette.querySelector('#pkInput');
    input.addEventListener('input', function () {
      paletteQuery = input.value.trim();
      runPaletteSearch();
    });
    input.addEventListener('keydown', paletteKey);
    palette.addEventListener('mousedown', function (e) {
      if (e.target === palette) closePalette();
    });
    runPaletteSearch();
    setTimeout(function () { if (input) input.focus() }, 20);
  }

  function initPaletteMenu() {
    var menu = document.getElementById('edMenu');
    if (!menu || menu.querySelector('.ed-palette-btn')) return;
    var col = menu.querySelector('.ed-menu-col');
    if (!col) return;
    var btn = el('button', 'btn ed-palette-btn');
    btn.type = 'button';
    btn.setAttribute('role', 'menuitem');
    btn.innerHTML = '<i class="icon fa-solid fa-bolt"></i><span>Командная палитра</span><kbd>Ctrl K</kbd>';
    btn.addEventListener('click', function () {
      menu.classList.remove('open');
      var more = document.getElementById('edMoreBtn');
      if (more) more.setAttribute('aria-expanded', 'false');
      openPalette();
    });
    col.insertBefore(btn, col.firstChild);
  }

  function initHotkeys() {
    document.addEventListener('keydown', function (e) {
      if (e.defaultPrevented) return;
      /* Escape закрывает палитру, даже если фокус ушёл из её поля
         (например, после клика по пустому месту ввода). */
      if (e.key === 'Escape' && palette) {
        e.preventDefault();
        closePalette();
        return;
      }
      var mod = e.ctrlKey || e.metaKey;
      var alt = e.altKey;
      var target = e.target || {};
      var tagName = (target.tagName || '').toUpperCase();
      var ta = editorTextarea();
      var inEditor = target === ta;
      var key = (e.key || '').toLowerCase();
      var code = e.code || '';

      if (mod && !alt && (key === 'k' || key === 'p')) {
        e.preventDefault();
        openPalette();
        return;
      }
      if (mod && !alt && key === '/') {
        e.preventDefault();
        callPage('openCheatsheet');
        return;
      }
      /* Дальше — только набор в самом тексте заметки: в полях ввода и в
         модальных окнах сочетания остаются браузерными. */
      if (!inEditor) return;

      if (mod && !alt && !e.shiftKey && key === 'e') {
        e.preventDefault(); editorFormat('`', '`'); return;
      }
      if (mod && e.shiftKey && !alt && key === 'k') {
        e.preventDefault(); editorFormat('[', '](url)'); return;
      }
      if (mod && e.shiftKey && !alt && key === 'q') {
        e.preventDefault(); editorLine('> '); return;
      }
      if (mod && e.shiftKey && !alt &&
        (code === 'Digit7' || code === 'Digit8' || code === 'Digit9')) {
        e.preventDefault();
        editorLine(code === 'Digit7' ? '1. ' : code === 'Digit8' ? '- ' : '- [ ] ');
        return;
      }
      if (mod && e.shiftKey && !alt && key === 'd') {
        e.preventDefault(); duplicateEditorLine(); return;
      }
      if (alt && !mod && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
        e.preventDefault(); moveEditorLine(e.key === 'ArrowUp' ? -1 : 1); return;
      }
      if (mod && alt && (code === 'Digit1' || code === 'Digit2' || code === 'Digit3')) {
        e.preventDefault();
        editorLine(code === 'Digit1' ? '# ' : code === 'Digit2' ? '## ' : '### ');
        return;
      }
      if (mod && alt && key === 'o') { e.preventDefault(); callPage('edToggleOutline'); return }
      if (mod && alt && key === 'p') { e.preventDefault(); callPage('edToggleProps'); return }
      if (mod && alt && key === 'v') { e.preventDefault(); callPage('togglePreview'); return }
      if (mod && alt && key === 'b') { e.preventDefault(); callPage('toggleSplitMode'); return }
      if (mod && alt && key === 'e') { e.preventDefault(); callPage('exportCurrentNote', 'markdown'); return }
      if (mod && alt && key === 'h') { e.preventDefault(); callPage('exportCurrentNote', 'html'); return }
    });
  }

  /* ── Время чтения ───────────────────────────────────────────────────── */
  function initReadTime() {
    var info = document.getElementById('wordInfo');
    var meta = document.querySelector('.meta-line');
    if (!info || !meta) return;

    var span = el('span');
    span.id = 'readTime';
    meta.appendChild(span);

    function render() {
      var m = /([\d\s\u00a0]+)/.exec(info.textContent || '');
      var words = m ? parseInt(m[1].replace(/[\s\u00a0]/g, ''), 10) : 0;
      if (!words) {
        span.innerHTML = '<i class="fa-solid fa-book-open" style="margin-right:4px"></i>0 мин';
        return;
      }
      var mins = Math.max(1, Math.round(words / 200));
      span.innerHTML = '<i class="fa-solid fa-book-open" style="margin-right:4px"></i>' +
        mins + ' мин чтения';
    }

    render();
    try {
      new MutationObserver(render).observe(info,
        { childList: true, characterData: true, subtree: true });
    } catch (e) { /* наблюдатель недоступен */ }
  }

  /* Позиция курсора по вертикали внутри textarea. Поле растёт под текст и
     не прокручивается само, поэтому меряем по невидимой копии с теми же
     шрифтом и отступами. */
  function caretOffset(ta, pos) {
    var cs = getComputedStyle(ta);
    var probe = el('div');
    var props = ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'fontVariant',
      'lineHeight', 'letterSpacing', 'wordSpacing', 'textTransform', 'tabSize',
      'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
      'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
      'boxSizing'];
    props.forEach(function (p) { probe.style[p] = cs[p]; });
    probe.style.position = 'absolute';
    probe.style.visibility = 'hidden';
    probe.style.left = '-9999px';
    probe.style.top = '0';
    probe.style.width = ta.clientWidth + 'px';
    probe.style.whiteSpace = 'pre-wrap';
    probe.style.wordWrap = 'break-word';
    probe.style.overflowWrap = 'break-word';
    probe.textContent = ta.value.slice(0, pos);
    var mark = el('span');
    mark.textContent = '\u200b';
    probe.appendChild(mark);
    document.body.appendChild(probe);
    var y = mark.offsetTop;
    if (probe.parentNode) probe.parentNode.removeChild(probe);
    return y;
  }

  /* ── «Машинка» (из Focus): строка с курсором по центру ──────────────── */
  function addToolbarButton(title, icon, onClick) {
    var btn = el('button', 'ed-extra-btn skin-toggle-btn');
    btn.type = 'button';
    btn.title = title;
    btn.setAttribute('aria-label', title);
    btn.innerHTML = '<i class="' + icon + '"></i>';
    if (railExtra) {
      railExtra.appendChild(btn);
    } else {
      var bar = document.querySelector('.format-bar') || document.querySelector('.toolbar');
      if (bar) bar.appendChild(btn);
    }
    btn.addEventListener('click', onClick);
    return btn;
  }

  function initTypewriter() {
    var typeBtn = addToolbarButton('Режим «Машинка»: строка с курсором по центру',
      'fa-solid fa-align-center', function () {
        setTypewriter(!root.classList.contains('skin-typewriter'));
      });

    function setTypewriter(on) {
      root.classList.toggle('skin-typewriter', on);
      if (typeBtn) {
        typeBtn.classList.toggle('on', on);
        typeBtn.title = on ? 'Выключить «Машинку»'
          : 'Режим «Машинка»: строка с курсором по центру';
      }
      store(LS_TYPE, on ? '1' : '0');
      if (on) centerCaret();
    }

    function centerCaret() {
      if (!root.classList.contains('skin-typewriter')) return;
      var ta = document.getElementById('content');
      if (!ta || document.activeElement !== ta) return;
      var y = caretOffset(ta, ta.selectionStart);
      var rect = ta.getBoundingClientRect();
      var top = window.pageYOffset + rect.top + y - Math.round(window.innerHeight * 0.46);
      try { window.scrollTo({ top: Math.max(0, top), behavior: 'smooth' }); }
      catch (e) { window.scrollTo(0, Math.max(0, top)); }
    }

    var ta = document.getElementById('content');
    if (ta) {
      ta.addEventListener('keyup', centerCaret);
      ta.addEventListener('click', centerCaret);
    }

    if (load(LS_TYPE) === '1') setTypewriter(true);
  }

  function init() {
    try { initRail(); } catch (e) { /* панель не критична для правки текста */ }
    try { initReadTime(); } catch (e) { /* счётчик — украшение */ }
    try { initOutline(); } catch (e) { /* оглавление необязательно */ }
    try { initTypewriter(); } catch (e) { /* режим набора необязателен */ }
    try { initAiTagsButton(); } catch (e) { /* AI-теги необязательны */ }
    try { initPaletteMenu(); } catch (e) { /* пункт меню — украшение */ }
    try { initHotkeys(); } catch (e) { /* горячие клавиши не должны ломать страницу */ }
    /* Строка свойств и теги по умолчанию скрыты: место она занимает
       всегда, а нужна нечасто. Видимой она остаётся только у тех, кто
       явно включил её обратно ('0' в localStorage). */
    if (load(LS_PROPS) !== '0') {
      document.body.classList.add('ed-props-hidden');
      var pb = document.getElementById('edPropsBtn');
      if (pb) pb.classList.add('on');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
