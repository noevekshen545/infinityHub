/* Infinity — connection watchdog
   Показывает тост «Соединение с сервером потеряно», когда сервер недоступен,
   и «Соединение восстановлено» после возврата. Самодостаточный: создаёт свой DOM.

   Логика определения намеренно «мягкая»: один случайный сбой пинга не считается
   потерей связи. Требуется FAIL_THRESHOLD неудач подряд (с быстрым повтором),
   чтобы не ловить мимолётные флэпы сети/загрузки сервера. */
(function () {
  if (window.__connWatch) return;
  window.__connWatch = true;

  var down = false;
  var failCount = 0;
  var PING_URL = '/app/jwt/get';      // лёгкий endpoint (маленький JSON)
  var INTERVAL = 10000;               // проверка каждые 10с
  var TIMEOUT = 6000;                 // таймаут пинга 6с
  var RETRY_DELAY = 1500;             // пауза перед повтором при сбое
  var FAIL_THRESHOLD = 2;             // столько неудач подряд = "потеряно"
  var TOAST_MS = 5000;                // время показа тоста

  function ensureToastEl() {
    var el = document.getElementById('connWatchToast');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'connWatchToast';
    el.setAttribute('role', 'status');
    el.style.cssText = [
      'position:fixed', 'top:18px', 'right:18px', 'left:auto',
      'z-index:2147483647', 'display:flex', 'align-items:center', 'gap:10px',
      'min-width:240px', 'max-width:320px', 'padding:14px 16px',
      'border-radius:14px', 'color:#fff', 'font:600 14px/1.35 system-ui,Segoe UI,Roboto,sans-serif',
      'box-shadow:0 10px 30px rgba(0,0,0,.35)', 'border:1px solid rgba(255,255,255,.14)',
      'backdrop-filter:blur(8px)', '-webkit-backdrop-filter:blur(8px)',
      'opacity:0', 'transform:translateX(120%)', 'pointer-events:none',
      'transition:opacity .35s ease, transform .35s cubic-bezier(.22,1,.36,1)'
    ].join(';');
    // акцентная полоска слева
    var bar = document.createElement('span');
    bar.style.cssText = 'flex:0 0 4px;align-self:stretch;border-radius:4px;background:rgba(255,255,255,.85)';
    el.appendChild(bar);
    var icon = document.createElement('span');
    icon.style.cssText = 'font-size:18px;line-height:1';
    icon.id = 'connWatchToastIcon';
    el.appendChild(icon);
    var txt = document.createElement('span');
    txt.id = 'connWatchToastText';
    el.appendChild(txt);
    el.addEventListener('click', hideToast);
    (document.body || document.documentElement).appendChild(el);
    return el;
  }

  var shown = false;
  function showToast(msg, opts) {
    try {
      var el = ensureToastEl();
      var icon = el.querySelector('#connWatchToastIcon');
      var txt = el.querySelector('#connWatchToastText');
      icon.textContent = opts.icon || '';
      txt.textContent = msg;
      el.style.background = opts.bg;
      el.style.pointerEvents = 'auto';
      // форсируем reflow, чтобы transition сработал от скрытого состояния
      void el.offsetWidth;
      el.style.opacity = '1';
      el.style.transform = 'translateX(0)';
      shown = true;
      clearTimeout(el._connToastTimer);
      el._connToastTimer = setTimeout(hideToast, TOAST_MS);
    } catch (e) { /* ignore */ }
  }

  function hideToast() {
    try {
      var el = document.getElementById('connWatchToast');
      if (!el || !shown) return;
      el.style.opacity = '0';
      el.style.transform = 'translateX(120%)';
      el.style.pointerEvents = 'none';
      shown = false;
    } catch (e) { /* ignore */ }
  }

  function markDown() {
    if (!down) {
      down = true;
      showToast('⚠ Соединение с сервером потеряно', {
        icon: '⚠️',
        bg: 'linear-gradient(135deg,#e53935,#b71c1c)'
      });
    }
  }

  function markUp() {
    if (down) {
      down = false;
      showToast('✓ Соединение с сервером восстановлено', {
        icon: '✅',
        bg: 'linear-gradient(135deg,#43a047,#1b5e20)'
      });
    }
  }

  // один пинг с таймаутом; возвращает true при успехе
  async function tryPing() {
    var ctl = new AbortController();
    var t = setTimeout(function () { ctl.abort(); }, TIMEOUT);
    try {
      // credentials: same-origin — запрос должен идти с cookie профиля.
      // С 'omit' сервер не видел infinity_profile и (до фикса) принимал пинг
      // за визит «гостя», переключая глобальный профиль на default каждые 10с.
      await fetch(PING_URL, { cache: 'no-store', signal: ctl.signal, credentials: 'same-origin' });
      clearTimeout(t);
      return true; // любой HTTP-ответ означает, что сервер жив
    } catch (e) {
      clearTimeout(t);
      return false;
    }
  }

  async function ping() {
    var ok = await tryPing();
    if (ok) {
      failCount = 0;
      markUp();
      return;
    }
    // быстрый повтор, чтобы отсечь мимолётный сбой
    await new Promise(function (r) { setTimeout(r, RETRY_DELAY); });
    ok = await tryPing();
    if (ok) {
      failCount = 0;
      markUp();
      return;
    }
    failCount++;
    if (failCount >= FAIL_THRESHOLD) markDown();
  }

  window.addEventListener('offline', markDown);
  window.addEventListener('online', function () { failCount = 0; ping(); });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { ping(); });
  } else {
    ping();
  }
  setInterval(ping, INTERVAL);
})();
