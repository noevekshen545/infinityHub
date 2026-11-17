/* =====================================================================
   Infinity Search — JS мобильной оболочки (mobile shell)
   ---------------------------------------------------------------------
   Общие примитивы для отдельных мобильных шаблонов:
   1. MShell.sheet()  — bottom-sheet вместо контекстного меню/модалки
   2. MShell.confirm/prompt/toast — мобильные эквиваленты desktop-диалогов
   3. MShell.longPress — долгое нажатие = правый клик desktop-версии
   4. MShell.modeSwitch — кнопка «Desktop-версия» (кука mobile_mode)
   Файл подключается ТОЛЬКО мобильными шаблонами.
   ===================================================================== */
(function (global) {
  'use strict';

  var MShell = {};

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  MShell.el = el;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  MShell.esc = esc;

  /* ---------------- bottom-sheet ---------------- */
  var _sheetStack = [];

  MShell.sheet = function (opts) {
    opts = opts || {};
    var wrap = el('div', 'mh-sheet-wrap open');
    var back = el('div', 'mh-sheet-back');
    var sheet = el('div', 'mh-sheet');
    sheet.appendChild(el('div', 'mh-sheet-grip'));

    var head = el('div', 'mh-sheet-head');
    head.appendChild(el('div', 'mh-sheet-title', esc(opts.title || '')));
    var closeBtn = el('button', 'mh-iconbtn', '<i class="fas fa-xmark"></i>');
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', 'Закрыть');
    head.appendChild(closeBtn);
    sheet.appendChild(head);

    var body = el('div', 'mh-sheet-body');
    if (typeof opts.body === 'string') body.innerHTML = opts.body;
    else if (opts.body) body.appendChild(opts.body);
    sheet.appendChild(body);

    var actionsRow = null;
    if (opts.actions && opts.actions.length) {
      actionsRow = el('div', 'mh-sheet-actions');
      opts.actions.forEach(function (a) {
        var b = el('button', 'mh-btn' + (a.kind === 'primary' ? ' primary' : '') +
                          (a.kind === 'danger' ? ' danger' : ''), esc(a.label));
        b.type = 'button';
        if (a.disabled) b.disabled = true;
        b.addEventListener('click', function () {
          if (a.onClick) a.onClick({ close: MShell.closeSheet, body: body, sheet: sheet });
          else MShell.closeSheet();
        });
        actionsRow.appendChild(b);
      });
      sheet.appendChild(actionsRow);
    }

    if (opts.actionsList && opts.actionsList.length) {
      var list = el('div', 'mh-sheet-list');
      opts.actionsList.forEach(function (a) {
        if (a.sep) { list.appendChild(el('div', 'mh-sheet-sep')); return; }
        var b = el('button', 'mh-action' + (a.danger ? ' danger' : ''),
          '<i class="fas ' + esc(a.icon || 'fa-ellipsis') + '"></i><span>' + esc(a.label) + '</span>' +
          (a.hint ? '<span class="mh-action-hint">' + esc(a.hint) + '</span>' : ''));
        b.type = 'button';
        if (a.disabled) { b.disabled = true; b.style.opacity = '.4'; }
        b.addEventListener('click', function () {
          if (a.onClick) a.onClick({ close: MShell.closeSheet, body: body });
        });
        list.appendChild(b);
      });
      body.appendChild(list);
    }

    wrap.appendChild(back);
    wrap.appendChild(sheet);
    document.body.appendChild(wrap);
    _sheetStack.push(wrap);

    function close() {
      var i = _sheetStack.indexOf(wrap);
      if (i < 0) return;
      _sheetStack.splice(i, 1);
      wrap.classList.remove('open');
      setTimeout(function () { if (wrap.parentNode) wrap.parentNode.removeChild(wrap); }, 180);
      if (opts.onClose) opts.onClose();
    }
    wrap._close = close;
    back.addEventListener('click', close);
    closeBtn.addEventListener('click', close);
    if (opts.swipeToClose !== false) {
      var y0 = null;
      sheet.addEventListener('touchstart', function (e) {
        y0 = e.touches[0].clientY;
      }, { passive: true });
      sheet.addEventListener('touchmove', function (e) {
        if (y0 == null) return;
        if (e.touches[0].clientY - y0 > 60) { y0 = null; close(); }
      }, { passive: true });
    }
    if (opts.content && opts.onMount) opts.onMount({ body: body, close: close });
    return { el: wrap, body: body, close: close };
  };

  MShell.closeSheet = function () {
    var top = _sheetStack[_sheetStack.length - 1];
    if (top && top._close) top._close();
  };
  MShell.closeAllSheets = function () {
    _sheetStack.slice().forEach(function (w) { w._close && w._close(); });
  };

  /* ---------------- toast ---------------- */
  function toastHost() {
    var h = document.querySelector('.mh-toast-host');
    if (!h) { h = el('div', 'mh-toast-host'); document.body.appendChild(h); }
    return h;
  }
  MShell.toast = function (msg, kind) {
    var t = el('div', 'mh-toast' + (kind === 'err' ? ' err' : ''), esc(msg));
    toastHost().appendChild(t);
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, kind === 'err' ? 4200 : 2600);
  };

  /* ---------------- confirm / prompt ---------------- */
  MShell.confirm = function (opts) {
    return new Promise(function (resolve) {
      var s = MShell.sheet({
        title: opts.title || 'Подтверждение',
        body: '<div style="padding:6px 2px 10px;font-size:14px;color:var(--m-text2)">' +
              esc(opts.message || '') + '</div>',
        actions: [
          { label: opts.cancelLabel || 'Отмена', onClick: function () { s.close(); resolve(false); } },
          { label: opts.okLabel || 'ОК', kind: opts.danger ? 'danger' : 'primary',
            onClick: function () { s.close(); resolve(true); } }
        ],
        onClose: function () { resolve(false); }
      });
    });
  };

  MShell.prompt = function (opts) {
    return new Promise(function (resolve) {
      var done = false;
      var s = MShell.sheet({
        title: opts.title || 'Ввод',
        body: '<div class="mh-field"><label class="mh-label">' + esc(opts.label || '') +
              '</label><input class="mh-input" id="mshPromptInput" value="' + esc(opts.value || '') +
              '" placeholder="' + esc(opts.placeholder || '') + '" autocomplete="off"></div>',
        actions: [
          { label: 'Отмена', onClick: function () { s.close(); resolve(null); } },
          { label: opts.okLabel || 'Сохранить', kind: 'primary', onClick: function () {
              done = true; var v = document.getElementById('mshPromptInput').value;
              s.close(); resolve(v); } }
        ],
        onClose: function () { if (!done) resolve(null); },
        onMount: function () {
          var i = document.getElementById('mshPromptInput');
          setTimeout(function () { i.focus(); i.select(); }, 120);
          i.addEventListener('keydown', function (e) { if (e.key === 'Enter') {
            done = true; s.close(); resolve(i.value); } });
        }
      });
    });
  };

  /* ---------------- long press = context menu ---------------- */
  MShell.longPress = function (node, handler, opts) {
    opts = opts || {};
    var ms = opts.ms || 480;
    var tol = opts.tolerance || 12;
    var timer = null, sx = 0, sy = 0, fired = false;

    function start(e) {
      if (e.button != null && e.button !== 0) return;   // правый клик обрабатывается отдельно
      var p = e.touches ? e.touches[0] : e;
      sx = p.clientX; sy = p.clientY; fired = false;
      timer = setTimeout(function () {
        fired = true;
        if (navigator.vibrate) { try { navigator.vibrate(8); } catch (err) {} }
        handler(e);
      }, ms);
    }
    function move(e) {
      if (timer == null) return;
      var p = e.touches ? e.touches[0] : e;
      if (Math.abs(p.clientX - sx) > tol || Math.abs(p.clientY - sy) > tol) {
        clearTimeout(timer); timer = null;
      }
    }
    function end() {
      if (timer != null) { clearTimeout(timer); timer = null; }
    }
    node.addEventListener('touchstart', start, { passive: true });
    node.addEventListener('touchmove', move, { passive: true });
    node.addEventListener('touchend', end);
    node.addEventListener('touchcancel', end);
    node.addEventListener('mousedown', start);
    node.addEventListener('mousemove', move);
    node.addEventListener('mouseup', end);
    node.addEventListener('mouseleave', end);
    if (opts.contextMenu !== false) {
      node.addEventListener('contextmenu', function (e) {
        e.preventDefault();
        if (!fired) handler(e);
      });
    }
    node.addEventListener('click', function (e) {
      if (fired) { e.preventDefault(); e.stopPropagation(); fired = false; }
    }, true);
  };

  /* ---------------- переключатель desktop/mobile ---------------- */
  function setModeCookie(mode, days) {
    document.cookie = 'mobile_mode=' + mode + ';path=/;max-age=' +
      Math.round((days || 365) * 86400) + ';SameSite=Lax';
  }
  MShell.forceDesktop = function () {
    setModeCookie('off');
    location.reload();
  };
  MShell.forceMobile = function () {
    setModeCookie('on');
    location.reload();
  };
  MShell.resetMode = function () {
    setModeCookie('', -1);
    location.reload();
  };

  MShell.mountModeSwitch = function (headerEl) {
    if (!headerEl || headerEl.querySelector('.mh-modeswitch')) return;
    var b = el('button', 'mh-iconbtn mh-modeswitch',
      '<i class="fas fa-desktop"></i>');
    b.type = 'button';
    b.title = 'Открыть desktop-версию этого модуля';
    b.addEventListener('click', function () {
      MShell.sheet({
        title: 'Версия интерфейса',
        actionsList: [
          { icon: 'fa-desktop', label: 'Desktop-версия (сохранить выбор)',
            onClick: function () { MShell.forceDesktop(); } },
          { icon: 'fa-mobile-screen', label: 'Мобильная версия (сохранить выбор)',
            onClick: function () { MShell.forceMobile(); } },
          { icon: 'fa-wand-magic-sparkles', label: 'Авто: определять по устройству',
            onClick: function () { MShell.resetMode(); } }
        ]
      });
    });
    headerEl.appendChild(b);
  };

  /* ---------------- прогресс ---------------- */
  MShell.busy = function (on) {
    var p = document.querySelector('.mh-progress');
    if (p) p.classList.toggle('on', !!on);
  };

  global.MShell = MShell;
})(window);
