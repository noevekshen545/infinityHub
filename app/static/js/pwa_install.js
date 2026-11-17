/* Предложение установить приложение на главный экран телефона.

   Вставляется во все страницы через webui_assets.inject, но работает только
   в мобильном браузере: на ПК, в Electron и во встроенных iframe сразу
   выходит, ничего не рисуя.

   Путей два:
     * Chromium — ловим beforeinstallprompt и по кнопке открываем системный
       диалог установки. Событие приходит не всегда (нет HTTPS, первая
       сессия без вовлечённости), поэтому есть запасной текст с меню;
     * iOS Safari — системного события нет вовсе, ставит только «Поделиться
       → На экран „Домой“», об этом и пишем.

   Service worker регистрируется здесь же и только на телефоне: без него
   Chromium не считает сайт устанавливаемым. Тот же /sw_offline.js уже
   регистрирует offline_store.js при включённых офлайн-копиях — повторная
   регистрация того же файла безопасна и просто возвращает регистрацию. */

(function () {
  'use strict';

  var KEY = 'pwa.install.v1';
  var DISMISS_MS = 14 * 24 * 60 * 60 * 1000;
  var DELAY_MS = 5000;

  var ua = navigator.userAgent || '';
  var electron = /Electron/i.test(ua);
  var ios = /iPad|iPhone|iPod/.test(ua) ||
            (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  function isMobile() {
    if (/Android|iPhone|iPad|iPod|Mobile|Opera Mini|IEMobile|Windows Phone/i.test(ua)) {
      return true;
    }
    try {
      return window.matchMedia('(pointer: coarse)').matches &&
             Math.min(screen.width, screen.height) <= 820;
    } catch (e) { return false; }
  }

  function isStandalone() {
    try {
      if (navigator.standalone === true) return true;
      if (window.matchMedia) {
        if (matchMedia('(display-mode: standalone)').matches) return true;
        if (matchMedia('(display-mode: fullscreen)').matches) return true;
        if (matchMedia('(display-mode: minimal-ui)').matches) return true;
      }
      if (document.referrer && document.referrer.indexOf('android-app://') === 0) {
        return true;
      }
    } catch (e) { /* старые браузеры без matchMedia */ }
    return false;
  }

  function inFrame() {
    try { return window.top !== window.self; } catch (e) { return true; }
  }

  function readState() {
    try { return JSON.parse(localStorage.getItem(KEY) || 'null') || {}; }
    catch (e) { return {}; }
  }

  function saveState(patch) {
    var s = readState();
    for (var k in patch) {
      if (Object.prototype.hasOwnProperty.call(patch, k)) s[k] = patch[k];
    }
    try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) { /* приватный режим */ }
  }

  if (!isMobile() || electron || inFrame() || isStandalone()) return;

  /* Регистрация нужна и тем, кто закрыл нашу плашку: пункт «Установить
     приложение» в меню Chrome тоже требует активного воркера. */
  if (window.isSecureContext && navigator.serviceWorker) {
    try {
      navigator.serviceWorker.register('/sw_offline.js', { scope: '/' }).catch(function () {});
    } catch (e) { /* без SW установка просто не предложится */ }
  }

  var state = readState();
  if (state.installed === true) return;
  if (state.dismissedAt && Date.now() - state.dismissedAt < DISMISS_MS) return;

  /* Страницы перезагружаются целиком, поэтому без этой отметки плашка
     вылезала бы заново после каждого перехода. Один раз за вкладку: если
     её проигнорировали, пункт установки всё равно остаётся в меню браузера. */
  try {
    if (sessionStorage.getItem(KEY + '.shown') === '1') return;
  } catch (e) { /* приватный режим */ }

  var deferred = null;   // сохранённое beforeinstallprompt
  var banner = null;
  var nodes = {};
  var shown = false;
  var done = false;      // плашку убрали — второй раз за загрузку не рисуем

  function takePrompt() {
    var e = window.__pwaBip || deferred;
    if (!e) return;
    window.__pwaBip = null;
    deferred = e;
    if (banner) applyMode();
    else show();
  }

  window.addEventListener('beforeinstallprompt', function (e) {
    /* Без preventDefault браузер нарисует свою мини-плашку, и вместе с ней
       наша кнопка выглядела бы дублем. */
    e.preventDefault();
    window.__pwaBip = e;
    takePrompt();
  });

  window.addEventListener('appinstalled', function () {
    saveState({ installed: true, dismissedAt: 0 });
    hide();
  });

  function dismiss() {
    saveState({ dismissedAt: Date.now() });
    hide();
  }

  function hide() {
    done = true;
    if (!banner) return;
    var el = banner;
    banner = null;
    el.style.opacity = '0';
    el.style.transform = 'translateY(14px)';
    setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 180);
  }

  function promptInstall() {
    if (!deferred) { applyMode(); return; }
    var ev = deferred;
    deferred = null;
    try {
      ev.prompt();
      var res = ev.userChoice;
      if (res && typeof res.then === 'function') {
        res.then(function (choice) {
          /* Согласие не гарантирует установку (бывает отказ в системе),
             но appinstalled всё равно придёт и запомнит результат. */
          if (choice && choice.outcome === 'dismissed') dismiss();
          else hide();
        }, function () { hide(); });
      } else {
        hide();
      }
    } catch (e) {
      /* Событие протухло (страница долго висела) — показываем инструкцию. */
      applyMode();
    }
  }

  function applyMode() {
    if (!banner) return;
    if (deferred) {
      nodes.title.textContent = 'Установить Infinity?';
      nodes.desc.textContent = 'Значок на главном экране открывает приложение без адресной строки.';
      nodes.go.textContent = 'Установить';
      nodes.later.hidden = false;
      nodes.go.onclick = promptInstall;
    } else if (ios) {
      nodes.title.textContent = 'Добавить на экран «Домой»';
      nodes.desc.textContent = 'Нажмите «Поделиться» (квадрат со стрелкой), затем «На экран „Домой“» и «Добавить».';
      nodes.go.textContent = 'Понятно';
      nodes.later.hidden = true;
      nodes.go.onclick = dismiss;
    } else {
      nodes.title.textContent = 'Добавить на главный экран';
      nodes.desc.textContent = 'Откройте меню браузера (⋮) и выберите «Установить приложение» или «Добавить на главный экран».';
      nodes.go.textContent = 'Понятно';
      nodes.later.hidden = true;
      nodes.go.onclick = dismiss;
    }
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }

  function show() {
    if (shown || done) return;
    if (!document.body) {
      document.addEventListener('DOMContentLoaded', show, { once: true });
      return;
    }
    shown = true;
    try { sessionStorage.setItem(KEY + '.shown', '1'); } catch (e) { /* приватный режим */ }

    var style = document.createElement('style');
    style.textContent = [
      '.pwa-inst{position:fixed;left:calc(10px + env(safe-area-inset-left,0px));',
      'right:calc(10px + env(safe-area-inset-right,0px));',
      'bottom:calc(10px + env(safe-area-inset-bottom,0px));z-index:2147483000;',
      'max-width:440px;margin:0 auto;padding:14px;border-radius:16px;',
      'background:var(--bg2,#171b22);color:var(--text,#e8e8ea);',
      'border:1px solid var(--border,rgba(255,255,255,.14));',
      'box-shadow:0 18px 50px rgba(0,0,0,.38);',
      'font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;',
      'animation:pwa-inst-in .22s ease-out;transition:opacity .18s,transform .18s}',
      '@keyframes pwa-inst-in{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}',
      '@media (prefers-reduced-motion:reduce){.pwa-inst{animation:none}}',
      '.pwa-inst__row{display:flex;gap:12px;align-items:flex-start}',
      '.pwa-inst__icon{width:44px;height:44px;border-radius:11px;flex:0 0 auto}',
      '.pwa-inst__text{flex:1 1 auto;min-width:0;padding-right:20px}',
      '.pwa-inst__title{font-weight:600;font-size:15px;margin:1px 0 3px}',
      '.pwa-inst__desc{color:var(--text-2,#a9b0bf);font-size:13px}',
      '.pwa-inst__close{position:absolute;top:6px;right:6px;width:32px;height:32px;',
      'border:0;background:transparent;color:inherit;font-size:20px;line-height:1;',
      'opacity:.6;cursor:pointer;border-radius:50%}',
      '.pwa-inst__actions{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}',
      '.pwa-inst__actions button{font:inherit;font-weight:600;border-radius:10px;',
      'padding:9px 14px;cursor:pointer;border:1px solid transparent}',
      '.pwa-inst__later{background:transparent;color:var(--text-2,#a9b0bf);',
      'border-color:var(--border,rgba(255,255,255,.14))}',
      '.pwa-inst__go{background:var(--accent,#3b6ef5);color:#fff}'
    ].join('');
    document.head.appendChild(style);

    var box = el('div', 'pwa-inst');
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-label', 'Установка приложения');

    var row = el('div', 'pwa-inst__row');
    var icon = el('img', 'pwa-inst__icon');
    icon.src = '/static/icons/icon-192.png';
    icon.alt = '';
    var text = el('div', 'pwa-inst__text');
    nodes.title = el('div', 'pwa-inst__title');
    nodes.desc = el('div', 'pwa-inst__desc');
    text.appendChild(nodes.title);
    text.appendChild(nodes.desc);
    row.appendChild(icon);
    row.appendChild(text);

    var close = el('button', 'pwa-inst__close', '\u00d7');
    close.type = 'button';
    close.setAttribute('aria-label', 'Закрыть');
    close.onclick = dismiss;

    var actions = el('div', 'pwa-inst__actions');
    nodes.later = el('button', 'pwa-inst__later', 'Позже');
    nodes.later.type = 'button';
    nodes.later.onclick = dismiss;
    nodes.go = el('button', 'pwa-inst__go');
    nodes.go.type = 'button';
    actions.appendChild(nodes.later);
    actions.appendChild(nodes.go);

    box.appendChild(close);
    box.appendChild(row);
    box.appendChild(actions);
    document.body.appendChild(box);
    banner = box;

    applyMode();
  }

  /* Если событие успело прийти до этого скрипта, забираем его из инлайнового
     перехватчика в <head>; иначе плашка появится по таймеру с инструкцией. */
  if (window.__pwaBip) takePrompt();

  setTimeout(function () {
    if (!shown && !deferred && !done) show();
  }, DELAY_MS);
})();
