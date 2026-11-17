/* settings_next.js — слой «next» страницы настроек.

   Подключается только на skin-выдаче (гейт data-settings-skin="next" на <html>):
   на ?skin=classic этого скрипта нет, и страница остаётся со своей прежней
   боковой колонкой.

   Логика настроек не трогается: switchTab, сохранение, тема и загрузка
   разделов живут в инлайновом скрипте шаблона. Здесь то, чего шаблон сам по
   себе не знает:

     1) выдвижной режим рейла на узких экранах — та же грамматика, что в
        apps_next.js (FAB, затемнение, Escape, закрытие после перехода);
     2) поиск по настройкам как индекс: раньше поле фильтровало только
       .setting-item видимого раздела, теперь оно знает все разделы,
       считает совпадения на пунктах меню и переезжает к найденному. */
(function () {
  'use strict';

  var root = document.documentElement;
  if (!root.getAttribute('data-settings-skin')) return;

  var MAX_RESULTS = 14;
  var panel = null;
  var input = null;
  var index = null;
  var results = [];
  var current = -1;
  var lastQuery = '';

  /* ── Индекс ─────────────────────────────────────────────────────────── */

  function textOf(el) {
    var label = el.querySelector('label');
    var parts = [];
    if (label) parts.push(label.textContent);
    var hint = el.querySelector('.hint');
    if (hint) parts.push(hint.textContent);
    var value = el.querySelector('input, select, textarea');
    if (value && value.id) parts.push(value.id.replace(/([a-z])([A-Z])/g, '$1 $2'));
    return parts.join(' · ').replace(/\s+/g, ' ').trim();
  }

  function unitText(el) {
    var h = el.querySelector('h4');
    if (h) {
      var p = el.querySelector('p');
      return (h.textContent + (p ? ' — ' + p.textContent : '')).replace(/\s+/g, ' ').trim();
    }
    return textOf(el);
  }

  function titleOf(tab) {
    try {
      if (typeof TITLES !== 'undefined' && TITLES[tab] && TITLES[tab].title) return TITLES[tab].title;
    } catch (e) { /* инлайновый скрипт не доехал — берём подпись пункта */ }
    var item = document.querySelector('.nav-item[data-tab="' + tab + '"]');
    if (item) {
      var span = item.querySelector('span');
      if (span) return span.textContent.trim();
    }
    return tab;
  }

  function buildIndex() {
    var list = [];
    /* Индекс идём от строк, а не от разделов: initArchiveTabs() переносит
       section-general / section-ai / section-search внутрь section-archive, и
       обход по разделам собрал бы их строки дважды — под «Архив» и под своим
       названием. closest() берёт ближайший раздел, то есть настоящий. */
    var units = document.querySelectorAll('.section[id^="section-"] .setting-item, ' +
                                           '.section[id^="section-"] .row, ' +
                                           '.section[id^="section-"] h2, ' +
                                           '.section[id^="section-"] h3');
    Array.prototype.forEach.call(units, function (el) {
      var section = el.closest('.section[id^="section-"]');
      if (!section) return;
      var tab = section.id.replace('section-', '');
      var title = titleOf(tab);
      var tag = el.tagName;
      var text = (tag === 'H2' || tag === 'H3') ? el.textContent : unitText(el);
      text = (text || '').replace(/\s+/g, ' ').trim();
      if (!text) return;
      list.push({ tab: tab, section: title, text: text, el: el, lower: text.toLowerCase() });
    });
    return list;
  }

  function score(unit, q) {
    if (unit.lower.indexOf(q) === 0) return 3;
    if (unit.lower.indexOf(q) > 0) return 2;
    if (unit.section.toLowerCase().indexOf(q) >= 0) return 1;
    return 0;
  }

  function search(q) {
    var seen = {};
    var out = [];
    if (!index) index = buildIndex();
    for (var i = 0; i < index.length; i++) {
      var unit = index[i];
      var s = score(unit, q);
      if (!s) continue;
      /* одна и та же строка может встретиться и в заголовке, и в пункте —
         дубликаты в списке только мешают читать */
      var key = unit.tab + '|' + unit.text;
      if (seen[key] !== undefined) {
        if (out[seen[key]] && s > out[seen[key]].score) out[seen[key]].score = s;
        continue;
      }
      seen[key] = out.length;
      out.push({ tab: unit.tab, section: unit.section, text: unit.text, el: unit.el, score: s });
    }
    out.sort(function (a, b) {
      if (b.score !== a.score) return b.score - a.score;
      /* при равном весе соседствуют строки одного раздела: иначе список
         перемешивает разделы в порядке разметки и под каждой строкой висит
         своя шапка — «AI», «AI», «AI» подряд */
      if (a.section !== b.section) {
        return a.section < b.section ? -1 : 1;
      }
      return 0;
    });
    return out.slice(0, MAX_RESULTS);
  }

  /* ── Панель результатов ─────────────────────────────────────────────── */

  function mount(rail) {
    panel = document.createElement('div');
    panel.className = 'rail-results';
    panel.setAttribute('role', 'listbox');
    rail.appendChild(panel);
    panel.addEventListener('mousedown', function (event) {
      /* mousedown раньше blur: без этого клик по строке успевал закрыть
         панель, и обработчик клика не получал свою строку */
      event.preventDefault();
    });
    panel.addEventListener('click', function (event) {
      var row = event.target.closest ? event.target.closest('.rail-result') : null;
      if (row) go(parseInt(row.dataset.i, 10));
    });
  }

  function mark(text, q) {
    var at = text.toLowerCase().indexOf(q);
    if (at < 0) return escapeHtml(text);
    return escapeHtml(text.slice(0, at)) + '<b>' + escapeHtml(text.slice(at, at + q.length)) +
           '</b>' + escapeHtml(text.slice(at + q.length));
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function render(q) {
    if (!panel) return;
    if (!q) {
      close();
      return;
    }
    results = search(q);
    current = -1;
    var html = '';
    var prevSec = null;
    for (var i = 0; i < results.length; i++) {
      var r = results[i];
      /* шапка раздела — опознаватель перехода, но у подряд идущих строк одного
         же раздела она ничего не добавляет, только съедает место. Раздел при
         этом остаётся в aria-label: без него свёрнутая шапка лишила бы
         строку контекста и у читалки, и у пробы. */
      var sec = r.section === prevSec ? '' :
        '<span class="r-sec">' + escapeHtml(r.section) + '</span>';
      prevSec = r.section;
      html += '<button type="button" class="rail-result" data-i="' + i + '" role="option"' +
        ' aria-label="' + escapeHtml(r.section + ' — ' + r.text) + '">' +
        sec +
        '<span class="r-title">' + mark(r.text, q) + '</span>' +
        '</button>';
    }
    if (!results.length) html = '<div class="rail-empty">Ничего не найдено</div>';
    panel.innerHTML = html;
    panel.classList.add('on');
  }

  function close() {
    if (panel) panel.classList.remove('on');
    results = [];
    current = -1;
  }

  function move(delta) {
    if (!results.length) return;
    current = (current + delta + results.length) % results.length;
    var rows = panel.querySelectorAll('.rail-result');
    Array.prototype.forEach.call(rows, function (row, i) {
      row.classList.toggle('current', i === current);
    });
    if (rows[current] && rows[current].scrollIntoView) {
      rows[current].scrollIntoView({ block: 'nearest' });
    }
  }

  /* ── Переход к найденному ───────────────────────────────────────────── */

  function go(i) {
    var hit = results[i];
    if (!hit) return;
    if (typeof window.switchTab === 'function') window.switchTab(hit.tab);
    /* switchTab прокручивает окно наверх плавно; свою прокрутку ставим после,
       иначе обе схватки за скролл дают скачок вместо нужного места */
    window.setTimeout(function () {
      if (!hit.el || !hit.el.scrollIntoView) return;
      hit.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      hit.el.classList.add('search-hit');
      window.setTimeout(function () { hit.el.classList.remove('search-hit'); }, 1900);
    }, 320);
    close();
    setOpen(false);
  }

  /* ── Счётчики совпадений на пунктах меню ────────────────────────────── */

  function paintCounts(q) {
    var counts = {};
    if (q && index) {
      for (var i = 0; i < index.length; i++) {
        if (score(index[i], q)) counts[index[i].tab] = (counts[index[i].tab] || 0) + 1;
      }
    }
    Array.prototype.forEach.call(document.querySelectorAll('.nav-item[data-tab]'), function (item) {
      var n = counts[item.dataset.tab] || 0;
      var badge = item.querySelector('.count');
      if (!n) {
        if (badge) badge.remove();
        return;
      }
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'count';
        item.appendChild(badge);
      }
      badge.textContent = String(n);
    });
  }

  /* ── Выдвижной режим (узкие экраны) ────────────────────────────────── */

  var scrim = null;
  var toggle = null;

  function isOpen() {
    return document.body.classList.contains('rail-open');
  }

  function setOpen(value) {
    var open = !!value;
    document.body.classList.toggle('rail-open', open);
    if (toggle) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-label', open ? 'Закрыть меню' : 'Открыть меню');
      var ic = toggle.querySelector('i');
      if (ic) ic.className = 'fas fa-' + (open ? 'xmark' : 'bars');
    }
    if (scrim) scrim.classList.toggle('on', open);
    if (!open) close();
  }

  window.toggleSettingsRail = function () {
    setOpen(!isOpen());
  };

  /* ── Старт ──────────────────────────────────────────────────────────── */

  function start() {
    var rail = document.getElementById('settingsRail') || document.querySelector('.toolbar');
    input = document.getElementById('settingsSearch');
    if (!rail || !input) return;

    mount(rail);

    input.addEventListener('focus', function () {
      if (input.value.trim()) render(input.value.trim().toLowerCase());
    });

    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase();
      if (q === lastQuery) return;
      lastQuery = q;
      /* Пустой запрос обнуляет индекс: разделы страницы наполняются асинхронно
         (расширения, статус, профили), и следующий поиск обязан увидеть уже
         живые строки, а не снимок первого нажатия. */
      if (!q) index = null;
      if (!index) index = buildIndex();
      paintCounts(q);
      render(q);
    });

    input.addEventListener('keydown', function (event) {
      if (event.key === 'ArrowDown') { event.preventDefault(); move(1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); move(-1); }
      else if (event.key === 'Enter') {
        if (current >= 0) { event.preventDefault(); go(current); }
      } else if (event.key === 'Escape') {
        /* панель важнее модального окна: если список открыт, Escape закрывает
           его и не даёт скрипту страницы закрыть им тему */
        if (panel.classList.contains('on')) {
          event.stopPropagation();
          close();
        }
      }
    });

    document.addEventListener('click', function (event) {
      if (!panel.classList.contains('on')) return;
      if (panel.contains(event.target) || input.contains(event.target) || event.target === input) return;
      close();
    });

    toggle = document.getElementById('settingsRailToggle') || document.querySelector('.rail-fab');
    if (toggle) toggle.addEventListener('click', function () { setOpen(!isOpen()); });

    scrim = document.createElement('div');
    scrim.className = 'rail-scrim';
    scrim.addEventListener('click', function () { setOpen(false); });
    document.body.appendChild(scrim);

    rail.addEventListener('click', function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      /* Пункты меню — это li.nav-item.btn, а не button: селектор по тегу
         их бы не нашёл, и меню на телефоне не закрывалось после перехода. */
      if (target.closest('.btn, .btn-icon')) setOpen(false);
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && isOpen()) setOpen(false);
    });

    var mq = window.matchMedia('(min-width: 981px)');
    var onChange = function (e) { if (e.matches) setOpen(false); };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);

    setOpen(false);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
