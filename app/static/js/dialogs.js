/* InfinityDialogs — модальные диалоги вместо window.prompt/confirm.
 *
 * Зачем: window.prompt() в Electron-оболочке не поддерживается (возвращает
 * null), поэтому кнопки «Создать файл», «Создать папку» и любые другие
 * места с prompt() в приложении выглядели как «ничего не происходит».
 * Нативный prompt() ещё и блокирует поток, а confirm() в Electron
 * показывает системное окно поверх интерфейса. Здесь — свои диалоги
 * в стиле страницы: те же CSS-переменные (--bg, --accent, --text …),
 * поэтому светлая/тёмная тема и палитра сайта соблюдаются.
 *
 * API (все функции возвращают Promise):
 *   InfinityDialogs.prompt({title, label, value, placeholder, okText}) -> string|null
 *   InfinityDialogs.confirm({title, text, okText, cancelText, danger}) -> boolean
 *   InfinityDialogs.alert({title, text}) -> void
 *   InfinityDialogs.form({title, fields:[{name,label,type,value,options,placeholder,required}],
 *                         okText, cancelText}) -> object|null
 *
 * Короткие псевдонимы: askPrompt(text, value), askConfirm(text), askAlert(text).
 */
(function () {
  'use strict';
  if (window.InfinityDialogs && window.InfinityDialogs.__v) return;

  var CSS = [
    '.idl-overlay{position:fixed;inset:0;z-index:2147483000;display:none;align-items:center;',
    'justify-content:center;background:rgba(8,11,16,.55);backdrop-filter:blur(3px);',
    '-webkit-backdrop-filter:blur(3px);font-family:inherit}',
    '.idl-overlay.idl-open{display:flex}',
    '.idl-box{width:min(460px,calc(100vw - 32px));background:var(--bg2,#151b23);color:var(--text,#e4e8ef);',
    'border:1px solid var(--border,rgba(255,255,255,.12));border-radius:14px;box-shadow:0 18px 60px rgba(0,0,0,.45);',
    'overflow:hidden;animation:idl-in .16s ease-out}',
    '@keyframes idl-in{from{opacity:0;transform:translateY(-8px) scale(.98)}to{opacity:1;transform:none}}',
    '.idl-head{display:flex;align-items:center;gap:8px;padding:14px 16px 10px;font-size:14px;font-weight:700}',
    '.idl-head .idl-ico{color:var(--accent,#6366f1);font-size:14px}',
    '.idl-body{padding:4px 16px 14px;font-size:13px;line-height:1.5;color:var(--text2,#a0aec0)}',
    '.idl-field{margin-top:10px}',
    '.idl-field label{display:block;font-size:11px;font-weight:600;color:var(--text3,#6b7280);',
    'margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}',
    '.idl-field input[type=text],.idl-field select,.idl-field textarea{width:100%;padding:8px 10px;',
    'background:var(--bg,#0f1419);color:var(--text,#e4e8ef);border:1px solid var(--border,rgba(255,255,255,.12));',
    'border-radius:8px;font-size:13px;font-family:inherit;outline:none;transition:border-color .15s,box-shadow .15s}',
    '.idl-field input[type=text]:focus,.idl-field select:focus,.idl-field textarea:focus{border-color:var(--accent,#6366f1);',
    'box-shadow:0 0 0 3px var(--accent-glow,rgba(99,102,241,.2))}',
    '.idl-field .idl-check{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text2,#a0aec0);',
    'text-transform:none;letter-spacing:0;font-weight:500;cursor:pointer}',
    '.idl-field .idl-check input{width:auto;accent-color:var(--accent,#6366f1)}',
    '.idl-foot{display:flex;justify-content:flex-end;gap:8px;padding:12px 16px;border-top:1px solid var(--border,rgba(255,255,255,.08));',
    'background:var(--bg3,rgba(255,255,255,.02))}',
    '.idl-btn{padding:8px 16px;border-radius:9px;border:1px solid var(--border,rgba(255,255,255,.12));',
    'background:var(--bg3,#1c242f);color:var(--text,#e4e8ef);font-size:13px;font-weight:600;cursor:pointer;',
    'font-family:inherit;transition:filter .15s,transform .1s}',
    '.idl-btn:hover{filter:brightness(1.12)}',
    '.idl-btn:active{transform:translateY(1px)}',
    '.idl-btn.idl-primary{background:linear-gradient(135deg,var(--accent,#6366f1),var(--accent2,#4f46e5));',
    'border-color:transparent;color:#fff}',
    '.idl-btn.idl-danger{background:var(--red,#ef4444);border-color:transparent;color:#fff}',
    '.idl-err{color:var(--red,#ef4444);font-size:11px;margin-top:6px;min-height:14px}'
  ].join('');

  function injectStyle() {
    if (document.getElementById('idl-style')) return;
    var st = document.createElement('style');
    st.id = 'idl-style';
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);
  }

  var state = null;   // {overlay, box, resolve, prevFocus, onKey}

  function close(value) {
    if (!state) return;
    var s = state;
    state = null;
    s.overlay.classList.remove('idl-open');
    s.overlay.style.display = 'none';
    if (s.prevFocus && s.prevFocus.focus) { try { s.prevFocus.focus(); } catch (e) {} }
    if (s.resolve) s.resolve(value);
  }

  function build(title, icon, bodyHtml, footHtml) {
    injectStyle();
    var overlay = document.createElement('div');
    overlay.className = 'idl-overlay';
    overlay.innerHTML =
      '<div class="idl-box" role="dialog" aria-modal="true">' +
        '<div class="idl-head"><i class="' + (icon || 'fas fa-circle-info') + ' idl-ico"></i>' +
          '<span class="idl-title"></span></div>' +
        '<div class="idl-body">' + bodyHtml + '</div>' +
        '<div class="idl-foot">' + footHtml + '</div>' +
      '</div>';
    overlay.querySelector('.idl-title').textContent = title || '';
    document.body.appendChild(overlay);
    overlay.addEventListener('mousedown', function (e) {
      if (e.target === overlay) close(null);   // клик по фону — отмена
    });
    return overlay;
  }

  function onKey(e) {
    if (!state) return;
    if (e.key === 'Escape') { e.preventDefault(); close(null); }
    else if (e.key === 'Enter' && state.enterCloses) {
      var tag = (e.target && e.target.tagName) || '';
      if (tag === 'TEXTAREA') return;
      e.preventDefault();
      state.ok();
    }
  }

  function open(overlay, okHandler) {
    return new Promise(function (resolve) {
      var prevFocus = document.activeElement;
      state = {
        overlay: overlay,
        resolve: resolve,
        prevFocus: prevFocus,
        enterCloses: true,
        ok: function () { if (okHandler) okHandler(); else close(null); }
      };
      overlay.style.display = 'flex';
      overlay.classList.add('idl-open');
      document.addEventListener('keydown', onKey, true);
      /* Кнопки диалога. Раньше «OK» и «Отмена» не были подписаны — диалог
         закрывался только по Enter, из-за чего кнопка выглядела мёртвой. */
      var okBtn = overlay.querySelector('[data-act="ok"]');
      if (okBtn) okBtn.addEventListener('click', function (e) { e.preventDefault(); state.ok(); });
      var cancelBtn = overlay.querySelector('[data-act="cancel"]');
      if (cancelBtn) cancelBtn.addEventListener('click', function (e) { e.preventDefault(); close(null); });
      var f = overlay.querySelector('[data-focus]') || overlay.querySelector('input,select,textarea');
      if (f) { try { f.focus(); if (f.select && f.tagName === 'INPUT') f.select(); } catch (e) {} }
    });
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* ---------- prompt ---------- */
  function prompt(opts) {
    opts = opts || {};
    var id = 'idl-p-' + Date.now();
    var body =
      '<div class="idl-field">' +
        (opts.label ? '<label for="' + id + '">' + esc(opts.label) + '</label>' : '') +
        '<input type="text" id="' + id + '" data-focus value="' + esc(opts.value == null ? '' : opts.value) + '"' +
        (opts.placeholder ? ' placeholder="' + esc(opts.placeholder) + '"' : '') + '>' +
      '</div>' +
      '<div class="idl-err" data-err></div>';
    var foot =
      '<button class="idl-btn" data-act="cancel">' + esc(opts.cancelText || 'Отмена') + '</button>' +
      '<button class="idl-btn idl-primary" data-act="ok">' + esc(opts.okText || 'OK') + '</button>';
    var ov = build(opts.title || 'Введите значение', opts.icon || 'fas fa-pen-to-square', body, foot);
    var input = ov.querySelector('input');
    var err = ov.querySelector('[data-err]');

    return open(ov, function () {
      var v = input.value;
      if (opts.required && !String(v).trim()) {
        err.textContent = opts.requiredText || 'Значение не может быть пустым';
        input.focus();
        return;                                   // диалог остаётся открытым
      }
      close(v);
    }).then(function (r) {
      ov.remove();
      return r;
    });
  }

  /* ---------- confirm ---------- */
  function confirm(opts) {
    opts = opts || {};
    var body = '<div class="idl-text">' + (opts.html || esc(opts.text || '')) + '</div>';
    var foot =
      '<button class="idl-btn" data-act="cancel">' + esc(opts.cancelText || 'Отмена') + '</button>' +
      '<button class="idl-btn ' + (opts.danger ? 'idl-danger' : 'idl-primary') + '" data-act="ok">' +
        esc(opts.okText || 'ОК') + '</button>';
    var ov = build(opts.title || 'Подтвердите действие', opts.icon || 'fas fa-triangle-exclamation', body, foot);
    ov.querySelector('[data-act="cancel"]').addEventListener('click', function () { close(false); });
    ov.querySelector('[data-act="ok"]').addEventListener('click', function () { close(true); });
    return open(ov).then(function (r) {
      ov.remove();
      return r === true;
    });
  }

  /* ---------- alert ---------- */
  function alertBox(opts) {
    opts = opts || {};
    var body = '<div class="idl-text">' + (opts.html || esc(opts.text || '')) + '</div>';
    var foot = '<button class="idl-btn idl-primary" data-act="ok">' + esc(opts.okText || 'ОК') + '</button>';
    var ov = build(opts.title || 'Уведомление', opts.icon || 'fas fa-circle-info', body, foot);
    ov.querySelector('[data-act="ok"]').addEventListener('click', function () { close(true); });
    return open(ov).then(function (r) { ov.remove(); return r; });
  }

  /* ---------- form (несколько полей) ---------- */
  function form(opts) {
    opts = opts || {};
    var fields = opts.fields || [];
    var html = '';
    fields.forEach(function (f, i) {
      var fid = 'idl-f-' + i + '-' + Date.now();
      if (f.type === 'select') {
        var optsHtml = (f.options || []).map(function (o) {
          var val = (o && o.value != null) ? o.value : o;
          var lbl = (o && o.label != null) ? o.label : o;
          return '<option value="' + esc(val) + '"' + (String(val) === String(f.value) ? ' selected' : '') +
                 '>' + esc(lbl) + '</option>';
        }).join('');
        html += '<div class="idl-field"><label for="' + fid + '">' + esc(f.label || '') + '</label>' +
                '<select id="' + fid + '" data-name="' + esc(f.name) + '"' + (i === 0 ? ' data-focus' : '') + '>' +
                optsHtml + '</select></div>';
      } else if (f.type === 'checkbox') {
        html += '<div class="idl-field"><label class="idl-check">' +
                '<input type="checkbox" data-name="' + esc(f.name) + '"' + (f.value ? ' checked' : '') + '> ' +
                esc(f.label || '') + '</label></div>';
      } else if (f.type === 'textarea') {
        html += '<div class="idl-field"><label for="' + fid + '">' + esc(f.label || '') + '</label>' +
                '<textarea id="' + fid + '" rows="3" data-name="' + esc(f.name) + '"' +
                (f.placeholder ? ' placeholder="' + esc(f.placeholder) + '"' : '') + '>' +
                esc(f.value == null ? '' : f.value) + '</textarea></div>';
      } else {
        html += '<div class="idl-field"><label for="' + fid + '">' + esc(f.label || '') + '</label>' +
                '<input type="text" id="' + fid + '" data-name="' + esc(f.name) + '"' +
                (i === 0 ? ' data-focus' : '') +
                ' value="' + esc(f.value == null ? '' : f.value) + '"' +
                (f.placeholder ? ' placeholder="' + esc(f.placeholder) + '"' : '') + '></div>';
      }
    });
    html += '<div class="idl-err" data-err></div>';
    var foot =
      '<button class="idl-btn" data-act="cancel">' + esc(opts.cancelText || 'Отмена') + '</button>' +
      '<button class="idl-btn idl-primary" data-act="ok">' + esc(opts.okText || 'Создать') + '</button>';
    var ov = build(opts.title || 'Заполните поля', opts.icon || 'fas fa-folder-plus', html, foot);
    ov.querySelector('[data-act="cancel"]').addEventListener('click', function () { close(null); });

    return open(ov, function () {
      var err = ov.querySelector('[data-err]');
      var out = {};
      var bad = null;
      fields.forEach(function (f) {
        var el = ov.querySelector('[data-name="' + f.name + '"]');
        if (!el) return;
        var v = (f.type === 'checkbox') ? el.checked : el.value;
        if (f.required && !String(v).trim()) bad = bad || (f.label || f.name);
        out[f.name] = v;
      });
      if (bad) {
        err.textContent = 'Заполните поле: ' + bad;
        return;
      }
      close(out);
    }).then(function (r) {
      ov.remove();
      return r;
    });
  }

  window.InfinityDialogs = {
    __v: 1,
    prompt: prompt,
    confirm: confirm,
    alert: alertBox,
    form: form,
    /* Короткие псевдонимы для мест, где prompt() использовался как однострочник. */
    askPrompt: function (text, value) { return prompt({ label: text, value: value || '' }); },
    askConfirm: function (text, title) { return confirm({ text: text, title: title }); },
    askAlert: function (text, title) { return alertBox({ text: text, title: title }); },
    isOpen: function () { return !!state; }
  };

  /* Немедленная (не-MPromise) версия prompt для мест, где нужен sync-ответ.
     Работает только если диалог уже открыт из другого вызова — поэтому
     штатно используется асинхронный API. */
  window.__idlPrompt = prompt;
})();
