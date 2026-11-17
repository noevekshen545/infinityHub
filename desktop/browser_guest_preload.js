/* Preload гостевой страницы <webview>.
   Работает внутри сайта (contextIsolation), поэтому наружу отдаёт только
   события выделения мышью. Никакого доступа к странице у хоста нет —
   он получает текст и координаты через ipcRenderer.sendToHost. */
const { ipcRenderer } = require('electron');

(function () {
  'use strict';

  function send(channel, payload) {
    try { ipcRenderer.sendToHost(channel, payload || {}); } catch (e) { /* окно закрыто */ }
  }

  function selectionText() {
    try {
      const sel = window.getSelection();
      return sel ? String(sel.toString() || '').trim() : '';
    } catch (e) { return ''; }
  }

  document.addEventListener('mouseup', function (e) {
    /* Даём браузеру дорисовать выделение (двойной клик ставит его уже после
       mouseup, поэтому ждём коротко). */
    setTimeout(function () {
      send('bb-selection', {
        text: selectionText(),
        x: e.clientX,
        y: e.clientY,
        dbl: false
      });
    }, 40);
  }, true);

  document.addEventListener('dblclick', function (e) {
    setTimeout(function () {
      const text = selectionText();
      if (!text || text.length > 48 || /\s/.test(text)) return;
      send('bb-word', { word: text, x: e.clientX, y: e.clientY });
    }, 30);
  }, true);

  document.addEventListener('mousedown', function () {
    send('bb-selection-clear', {});
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') send('bb-selection-clear', {});
  }, true);

  /* Логин/пароль: когда форма отправляется (submit, Enter или клик по
     кнопке входа), отдаём хосту пару логин-пароль. Хост сам решает,
     сохранять ли её в профиль. Пароль наружу уходит только по действию
     пользователя на странице, фонового сканирования нет. */
  function captureLogin(form, srcEl) {
    try {
      var scope = form || (srcEl && srcEl.closest ? srcEl.closest('form') : null) || document;
      var pw = scope.querySelector ? scope.querySelector('input[type="password"]') : null;
      if (!pw || !pw.value) return;
      var field = scope.querySelector('input[type="email"],input[autocomplete="username"],input[name*="user" i],input[name*="login" i],input[id*="user" i],input[id*="login" i]');
      if (!field) field = scope.querySelector('input[type="text"]');
      send('bb-login-captured', {
        username: field ? String(field.value || '') : '',
        password: String(pw.value || ''),
        url: location.href
      });
    } catch (e) { /* форма нестандартная */ }
  }

  document.addEventListener('submit', function (e) {
    captureLogin(e.target, e.target);
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    var el = e.target;
    if (!el || !el.matches || !el.matches('input[type="password"],input[type="text"],input[type="email"]')) return;
    setTimeout(function () { captureLogin(null, el); }, 120);
  }, true);

  document.addEventListener('click', function (e) {
    var el = e.target;
    if (!el || !el.closest) return;
    var btn = el.closest('button,input[type="submit"],input[type="button"],[role="button"]');
    if (!btn) return;
    var type = String(btn.getAttribute('type') || '').toLowerCase();
    var text = String((btn.innerText || btn.value || btn.getAttribute('aria-label') || '')).toLowerCase();
    var looksLikeSubmit = type === 'submit' ||
      /(войти|вход|логин|sign\s*in|log\s*in|login|submit|continue|продолжить|далее|next)/i.test(text);
    if (!looksLikeSubmit) return;
    setTimeout(function () { captureLogin(null, btn); }, 120);
  }, true);
})();
