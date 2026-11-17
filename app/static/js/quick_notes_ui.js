/* Быстрые заметки — общий UI.
 *
 * Одна панель на два места: окно мини-приложения (Shift+Space) и модуль
 * заметок. Хранилище и API — app/quick_notes_module.py + /api/quick-notes*.
 */
(function () {
  'use strict';

  var API = '/api/quick-notes';

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function esc(s) {
    return String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function api(url, opts) {
    return fetch(url, opts || {}).then(function (r) {
      var ct = r.headers.get('content-type') || '';
      var p = ct.indexOf('json') >= 0 ? r.json() : Promise.resolve({});
      return p.then(function (d) {
        if (!r.ok) throw new Error((d && d.error) || ('ошибка ' + r.status));
        return d;
      });
    });
  }

  function post(url, body) {
    return api(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }

  function fmtTime(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '';
    var now = new Date();
    var same = d.toDateString() === now.toDateString();
    var hh = String(d.getHours()).padStart(2, '0');
    var mm = String(d.getMinutes()).padStart(2, '0');
    if (same) return hh + ':' + mm;
    return String(d.getDate()).padStart(2, '0') + '.' +
      String(d.getMonth() + 1).padStart(2, '0');
  }

  function createPanel(opts) {
    opts = opts || {};
    var root = el('div', 'qn-panel' + (opts.compact ? ' compact' : ''));
    root.innerHTML =
      '<div class="qn-composer">' +
        '<textarea class="qn-input" rows="2" placeholder="Быстрая заметка… (Ctrl+Enter — сохранить)"></textarea>' +
        '<button class="qn-add" type="button" title="Сохранить"><i class="fas fa-plus"></i></button>' +
      '</div>' +
      '<div class="qn-status"></div>' +
      '<div class="qn-list"><div class="qn-empty">Пока нет быстрых заметок</div></div>';

    var input = root.querySelector('.qn-input');
    var list = root.querySelector('.qn-list');
    var status = root.querySelector('.qn-status');
    var statusTimer = null;

    function flash(msg, isErr) {
      status.textContent = msg || '';
      status.classList.toggle('err', !!isErr);
      clearTimeout(statusTimer);
      if (msg) {
        statusTimer = setTimeout(function () {
          status.textContent = '';
          status.classList.remove('err');
        }, 2600);
      }
    }

    function load() {
      return api(API).then(function (d) {
        render(d && d.notes ? d.notes : []);
      }).catch(function (e) {
        render([]);
        flash('Не удалось загрузить: ' + e.message, true);
      });
    }

    function render(notes) {
      list.innerHTML = '';
      if (!notes.length) {
        list.appendChild(el('div', 'qn-empty', 'Пока нет быстрых заметок'));
        return;
      }
      notes.forEach(function (n) { list.appendChild(card(n)); });
    }

    function card(n) {
      var c = el('div', 'qn-card' + (n.pinned ? ' pinned' : ''));
      var txt = el('div', 'qn-card-text', n.text || '');
      txt.title = 'Двойной клик — редактировать';
      var foot = el('div', 'qn-card-foot');
      foot.innerHTML =
        '<span class="qn-time">' + esc(fmtTime(n.updated)) + '</span>' +
        '<span class="spacer"></span>' +
        '<button type="button" data-act="pin" title="' + (n.pinned ? 'Открепить' : 'Закрепить') + '">' +
          '<i class="fas fa-thumbtack"></i></button>' +
        '<button type="button" data-act="copy" title="Копировать"><i class="fas fa-copy"></i></button>' +
        '<button type="button" data-act="to-notes" title="Отправить в заметки">' +
          '<i class="fas fa-note-sticky"></i></button>' +
        '<button type="button" data-act="del" title="Удалить"><i class="fas fa-trash"></i></button>';

      foot.addEventListener('click', function (e) {
        var b = e.target.closest('button');
        if (!b) return;
        e.stopPropagation();
        var act = b.getAttribute('data-act');
        if (act === 'pin') {
          post(API + '/pin', { id: n.id, pinned: !n.pinned })
            .then(load)
            .catch(function (err) { flash(err.message, true); });
        } else if (act === 'copy') {
          var text = n.text || '';
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function () {
              flash('Скопировано');
            }).catch(function () { flash('Не удалось скопировать', true); });
          } else {
            flash('Копирование недоступно', true);
          }
        } else if (act === 'to-notes') {
          post(API + '/to-note', { id: n.id })
            .then(function () {
              flash('Отправлено в заметки');
              load();
            })
            .catch(function (err) { flash(err.message, true); });
        } else if (act === 'del') {
          if (!window.confirm('Удалить быструю заметку?')) return;
          post(API + '/delete', { id: n.id })
            .then(load)
            .catch(function (err) { flash(err.message, true); });
        }
      });

      txt.addEventListener('dblclick', function () { startEdit(c, n, txt); });
      c.appendChild(txt);
      c.appendChild(foot);
      return c;
    }

    function startEdit(cardEl, n, txt) {
      if (cardEl.querySelector('.qn-edit')) return;
      txt.style.display = 'none';
      var ta = document.createElement('textarea');
      ta.className = 'qn-edit';
      ta.value = n.text || '';
      cardEl.insertBefore(ta, txt.nextSibling);
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
      var closed = false;

      function done(saveIt) {
        if (closed) return;
        closed = true;
        var value = ta.value.trim();
        if (!saveIt || !value || value === n.text) {
          ta.remove();
          txt.style.display = '';
          return;
        }
        post(API + '/save', { id: n.id, text: value })
          .then(load)
          .catch(function (err) {
            ta.remove();
            txt.style.display = '';
            flash(err.message, true);
          });
      }

      ta.addEventListener('blur', function () {
        setTimeout(function () {
          if (document.body.contains(ta)) done(true);
        }, 140);
      });
      ta.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') { e.preventDefault(); done(false); }
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); done(true); }
      });
    }

    function add() {
      var value = input.value.trim();
      if (!value) { input.focus(); return; }
      input.value = '';
      post(API + '/save', { text: value })
        .then(function () { flash('Сохранено'); return load(); })
        .catch(function (err) {
          input.value = value;
          flash(err.message, true);
        });
    }

    root.querySelector('.qn-add').addEventListener('click', add);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        add();
      }
    });

    root.reload = load;
    root.focusInput = function () { input.focus(); };
    load();
    return root;
  }

  window.QuickNotesUI = {
    createPanel: createPanel,
    api: api,
    esc: esc
  };
})();
