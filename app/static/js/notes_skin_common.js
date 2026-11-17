/* Превью-шаблоны заметок (Aurora / Studio / Focus).

   Скрипт подключается ТОЛЬКО к копиям-превью (атрибут data-notes-skin на
   <html>), основной page_notes.html его не загружает. Задачи:
     1) плавающий переключатель шаблонов, чтобы сравнивать их и вернуться
        к классической странице;
     2) Ctrl+K — фокус в поиск (дополнительный шорткат, / и n уже заняты
        самой страницей заметок);
     3) «Дзен» в Focus и «Плотность» в Studio — оформление, которое
        запоминается в localStorage.

   Ничего из существующей логики заметок не переопределяется: скрипт
   только добавляет свои элементы и слушатели. */
(function () {
  'use strict';

  var root = document.documentElement;
  var skin = root.getAttribute('data-notes-skin') || '';
  if (!skin) return;

  var LABEL = { aurora: 'Aurora', studio: 'Studio', focus: 'Focus' };
  var ICON = {
    aurora: 'fa-solid fa-cloud',
    studio: 'fa-solid fa-table-columns',
    focus: 'fa-solid fa-align-left',
    classic: 'fa-solid fa-note-sticky'
  };
  var LS_SWITCH = 'notes_skin_switch_open';
  var LS_ZEN = 'notes_skin_focus_zen';
  var LS_DENSE = 'notes_skin_studio_compact';

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

  /* ── Переключатель шаблонов ─────────────────────────────────────────── */
  function buildSwitcher() {
    var wrap = el('div', 'skin-switch');
    if (load(LS_SWITCH) === '1') wrap.classList.add('open');

    var toggle = el('button', 'skin-switch-toggle');
    toggle.type = 'button';
    toggle.title = 'Превью-шаблоны заметок';
    toggle.innerHTML =
      '<span class="dot"></span><span>Шаблон: ' + (LABEL[skin] || skin) + '</span>' +
      '<i class="fa-solid fa-chevron-up" style="font-size:10px;opacity:.55"></i>';

    var panel = el('div', 'skin-switch-panel');
    var rows = '';
    ['aurora', 'studio', 'focus'].forEach(function (key) {
      rows += '<a href="/notes/preview/' + key + '"' + (key === skin ? ' class="on"' : '') + '>' +
        '<i class="' + ICON[key] + '"></i>' + LABEL[key] + '</a>';
    });
    panel.innerHTML =
      '<div class="skin-cap">Шаблон списка</div>' + rows;

    wrap.appendChild(panel);
    wrap.appendChild(toggle);
    document.body.appendChild(wrap);

    toggle.addEventListener('click', function () {
      var open = wrap.classList.toggle('open');
      store(LS_SWITCH, open ? '1' : '0');
    });

    document.addEventListener('click', function (e) {
      if (!wrap.classList.contains('open')) return;
      if (wrap.contains(e.target)) return;
      wrap.classList.remove('open');
      store(LS_SWITCH, '0');
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && wrap.classList.contains('open')) {
        wrap.classList.remove('open');
        store(LS_SWITCH, '0');
      }
    });
  }

  /* ── Дополнительный шорткат: Ctrl/Cmd+K → поиск ─────────────────────── */
  function wireSearchShortcut() {
    document.addEventListener('keydown', function (e) {
      if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;
      if (String(e.key).toLowerCase() !== 'k') return;
      var input = document.getElementById('sq');
      if (!input) return;
      e.preventDefault();
      input.focus();
      input.select();
    });
  }

  function addTopButton(title, icon, onClick) {
    var btn = el('button', 'btn-icon skin-toggle-btn');
    btn.type = 'button';
    btn.title = title;
    btn.setAttribute('aria-label', title);
    btn.innerHTML = '<i class="' + icon + '"></i>';
    var themeBtn = document.getElementById('themeBtn');
    var bar = themeBtn ? themeBtn.parentNode : null;
    if (bar) bar.insertBefore(btn, themeBtn);
    else {
      var top = document.querySelector('.top-bar');
      if (top) top.appendChild(btn);
    }
    btn.addEventListener('click', onClick);
    return btn;
  }

  /* ── Focus: режим «Дзен» ────────────────────────────────────────────── */
  function initFocus() {
    var isZen = load(LS_ZEN) === '1';

    var btn = addTopButton('Режим «Дзен»: скрыть панели и оставить текст', 'fa-solid fa-expand', function () {
      setZen(!root.classList.contains('skin-focus-zen'));
    });

    function setZen(on) {
      root.classList.toggle('skin-focus-zen', on);
      btn.classList.toggle('on', on);
      btn.innerHTML = '<i class="fa-solid ' + (on ? 'fa-compress' : 'fa-expand') + '"></i>';
      btn.title = on ? 'Выйти из режима «Дзен» (Esc)' : 'Режим «Дзен»: скрыть панели и оставить текст';
      store(LS_ZEN, on ? '1' : '0');
    }

    if (isZen) setZen(true);

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' || !root.classList.contains('skin-focus-zen')) return;
      // Сначала Esc закрывает открытую модалку, и только потом выходит из «Дзена».
      if (document.querySelector('.nm-modal-back')) return;
      setZen(false);
    });
  }

  /* ── Studio: плотный режим ──────────────────────────────────────────── */
  function initStudio() {
    var btn = addTopButton('Плотный режим карточек', 'fa-solid fa-bars-staggered', function () {
      setDense(!root.classList.contains('skin-studio-compact'));
    });

    function setDense(on) {
      root.classList.toggle('skin-studio-compact', on);
      btn.classList.toggle('on', on);
      btn.title = on ? 'Обычный размер карточек' : 'Плотный режим карточек';
      store(LS_DENSE, on ? '1' : '0');
    }

    if (load(LS_DENSE) === '1') setDense(true);
  }

  /* ── Закрытие модалки шаблонов заметок ──────────────────────────────── */
  function wireModalClose() {
    /* В шаблоне у .nm-close нет обработчика: модалка закрывалась только
       кликом по фону. Делегированный обработчик чинит кнопку «×» и Esc
       для всех .nm-modal-back, не трогая сами модалки. */
    document.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.nm-close') : null;
      if (!btn) return;
      var back = btn.closest('.nm-modal-back');
      if (back) back.remove();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var back = document.querySelector('.nm-modal-back');
      if (back) back.remove();
    });
  }

  function init() {
    try { buildSwitcher(); } catch (e) { /* превью не должно ломать страницу */ }
    wireSearchShortcut();
    wireModalClose();
    if (skin === 'focus') initFocus();
    if (skin === 'studio') initStudio();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
