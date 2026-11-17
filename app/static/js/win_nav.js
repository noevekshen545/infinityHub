/* Кнопки окна во всём приложении: одна клавиша и одно состояние.

   Раньше Ctrl+Shift+H обрабатывали три независимых куска кода —
   desktop_frame.js (обычные страницы), browser.js (окно браузера) и
   16-palette.js (страница агента). На страницах, где скрипты встречались
   вместе, клавиша срабатывала дважды и панель «не скрывалась»: первое
   нажатие прятало, второе тут же возвращало. Кроме того, обработчик висел
   на всплытии, поэтому фокус в поле ввода или в ифрейме до него не доходил.

   Здесь один модуль на всё приложение: ловит клавишу в фазе перехвата,
   помечает событие (чтобы старые копии обработчиков не сработали второй
   раз), хранит состояние в localStorage под прежним ключом и применяет его
   к любой панели кнопок окна: #winBtns, .win-btns, .bb-win.

   На телефоне клавиатуры нет — панель скрывается долгим нажатием, а чтобы
   вернуть, в правом верхнем углу остаётся почти незаметная кнопка.
*/
(function () {
  'use strict';

  var KEY = 'infinity_win_nav_hidden';
  var SEL = '#winBtns, .win-btns, .bb-win, [data-winbar]';

  function isHidden() {
    try { return localStorage.getItem(KEY) === '1'; } catch (e) { return false; }
  }

  function containers() {
    var nodes = document.querySelectorAll(SEL), out = [], i;
    for (i = 0; i < nodes.length; i++) out.push(nodes[i]);
    return out;
  }

  function restoreHotspot(hide, hasBar) {
    var btn = document.getElementById('winNavRestore');
    if (!hide || !hasBar) {
      if (btn) btn.remove();
      return;
    }
    if (btn || !document.body) return;
    btn = document.createElement('button');
    btn.id = 'winNavRestore';
    btn.type = 'button';
    btn.title = 'Показать кнопки окна (Ctrl+Shift+H)';
    btn.setAttribute('aria-label', btn.title);
    btn.textContent = '⌄';
    btn.style.cssText =
      'position:fixed;top:2px;right:2px;z-index:2147483646;width:26px;height:26px;' +
      'margin:0;padding:0;border:0;border-radius:7px;background:rgba(20,24,32,.35);' +
      'color:#fff;font:14px/1 system-ui,sans-serif;cursor:pointer;opacity:.3;' +
      'transition:opacity .15s;display:flex;align-items:center;justify-content:center';
    btn.addEventListener('mouseenter', function () { btn.style.opacity = '.9'; });
    btn.addEventListener('mouseleave', function () { btn.style.opacity = '.3'; });
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      set(false, { announce: true });
    });
    document.body.appendChild(btn);
  }

  function apply(hide) {
    var list = containers(), i;
    for (i = 0; i < list.length; i++) {
      var el = list[i];
      /* Свойство hidden не трогаем: панель по разметке скрыта до тех пор,
         пока скрипт страницы не убедится, что мы в обёртке Electron.
         Здесь управляем только видимостью поверх этого решения. */
      el.style.display = hide ? 'none' : '';
      if (hide) el.setAttribute('aria-hidden', 'true');
      else el.removeAttribute('aria-hidden');
    }
    var root = document.documentElement;
    if (root && root.classList) root.classList.toggle('win-nav-hidden', !!hide);
    /* Кнопку «вернуть» рисует только верхний документ, у которого есть своя
       панель. На /desktop приложения открываются в ифреймах, и win_nav.js
       исполняется и там: без этой проверки в каждом ифрейме появлялась
       вторая стрелка, хотя своей панели у него нет (или она управляется
       родителем, который и рисует кнопку). */
    restoreHotspot(!!hide, list.length > 0 && isTopWindow());
  }

  function isTopWindow() {
    try { return window.top === window; } catch (e) { return false; }
  }

  function announce(hide) {
    var text = hide
      ? 'Кнопки окна скрыты · Ctrl+Shift+H — показать'
      : 'Кнопки окна показаны';
    if (typeof window.toast === 'function') {
      try { window.toast(text, 'info'); return; } catch (e) { /* свой показ ниже */ }
    }
    var el = document.getElementById('winNavHint');
    if (el) el.remove();
    el = document.createElement('div');
    el.id = 'winNavHint';
    el.textContent = text;
    el.style.cssText =
      'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);' +
      'z-index:2147483647;background:rgba(20,24,32,.92);color:#e6eaf0;' +
      'font:12px/1.4 system-ui,sans-serif;padding:7px 12px;border-radius:8px;' +
      'pointer-events:none;opacity:0;transition:opacity .18s';
    document.body.appendChild(el);
    requestAnimationFrame(function () { el.style.opacity = '1'; });
    setTimeout(function () {
      el.style.opacity = '0';
      setTimeout(function () { el.remove(); }, 250);
    }, 2200);
  }

  /* Соседние окна и ифреймы той же страницы: рабочая область главного
     окна — ифрейм, и событие клавиатуры из него до родителя не доходит.
     Состояние передаём сообщением, чтобы панель скрывалась везде сразу. */
  function broadcast(hide) {
    var msg = { type: 'infinity_win_nav', hidden: !!hide };
    var i;
    try {
      for (i = 0; i < window.frames.length; i++) {
        try { window.frames[i].postMessage(msg, location.origin); } catch (e) { /* чужое окно */ }
      }
    } catch (e) { /* нет доступа к frames */ }
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage(msg, location.origin);
      }
    } catch (e) { /* родитель другого origin */ }
    try {
      window.dispatchEvent(new CustomEvent('infinity:win-nav', { detail: { hidden: !!hide } }));
    } catch (e) { /* старый движок */ }
  }

  function set(hide, opts) {
    hide = !!hide;
    try { localStorage.setItem(KEY, hide ? '1' : '0'); } catch (e) { /* приватный режим */ }
    apply(hide);
    broadcast(hide);
    if (opts && opts.announce) announce(hide);
    return hide;
  }

  function toggle() { return set(!isHidden(), { announce: true }); }

  window.addEventListener('keydown', function (e) {
    if (!(e.ctrlKey && e.shiftKey)) return;
    if (String(e.key || '').toLowerCase() !== 'h') return;
    /* Старые копии обработчиков в кеше браузера проверяют этот же флаг. */
    if (e.__infinityWinNav) return;
    e.__infinityWinNav = true;
    e.preventDefault();
    e.stopPropagation();
    toggle();
  }, true);

  window.addEventListener('message', function (e) {
    var d = e && e.data;
    if (!d || d.type !== 'infinity_win_nav' || typeof d.hidden !== 'boolean') return;
    try {
      if (e.origin && e.origin !== 'null' && e.origin !== location.origin) return;
    } catch (err) { /* origin недоступен */ }
    apply(d.hidden);
  });

  window.addEventListener('storage', function (e) {
    if (e.key === KEY) apply(isHidden());
  });

  /* Долгое нажатие на панели — тот же переключатель для тач-экрана. */
  function bindTouch() {
    var list = containers(), i;
    for (i = 0; i < list.length; i++) {
      var el = list[i];
      if (el.dataset.winNavBound === '1') continue;
      el.dataset.winNavBound = '1';
      (function (node) {
        var timer = null;
        var clear = function () {
          if (timer) { clearTimeout(timer); timer = null; }
        };
        node.addEventListener('pointerdown', function (e) {
          if (e.pointerType !== 'touch') return;
          clear();
          timer = setTimeout(function () {
            timer = null;
            set(true, { announce: true });
          }, 700);
        }, { passive: true });
        node.addEventListener('pointerup', clear, { passive: true });
        node.addEventListener('pointercancel', clear, { passive: true });
        node.addEventListener('pointermove', clear, { passive: true });
      })(el);
    }
  }

  function refresh() {
    apply(isHidden());
    bindTouch();
  }

  window.infinityWinNav = {
    isHidden: isHidden,
    set: set,
    toggle: toggle,
    apply: apply,
    refresh: refresh
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', refresh, { once: true });
  } else {
    refresh();
  }
})();
