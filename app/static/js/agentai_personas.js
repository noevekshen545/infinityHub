/* Панель личностей агента (личности = профили Hermes).
 *
 * Внедряется в страницу агента (/agentai) через прокси интеграции —
 * исходники агента не правим. Личность здесь — это профиль агента: своя
 * папка, свой SOUL.md (текст личности), своя база переписки. У каждой
 * личности свой чат: переписка ведётся через /api/agentai/ask с
 * параметром profile, поэтому диалоги не смешиваются.
 */
(function () {
  'use strict';
  if (window.__infinityPersonas) return;
  window.__infinityPersonas = true;

  var API = '/api/agentai/personas';
  var ASK = '/api/agentai/ask';
  var HIST = '/api/agentai/session/';
  var SESSION_KEY = 'persona-chat';

  var state = {
    personas: [],
    current: 'default',
    profile: 'default',  // профиль приложения — ключ сохранённого выбора
    loaded: false,
    messages: [],
    busy: false,
    editing: null,      // имя личности, которую сейчас правим
    draft: null         // форма создания/правки
  };

  var root = null, listEl = null, detailEl = null, chatEl = null, inputEl = null;

  // ------------------------------------------------------------- утилиты
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

  /* Иконка личности — класс Font Awesome от сервера (персона сама по себе
     не «картинка», поэтому плитку красит тема). fa-* заворачиваем в <i>,
     всё остальное остаётся текстом. */
  function iconHtml(icon, fallback) {
    var v = String(icon || '');
    if (/^fa-[a-z0-9-]+$/.test(v)) {
      return '<i class="fa-solid ' + v + '" aria-hidden="true"></i>';
    }
    return esc(v || fallback || '');
  }

  function personaByName(name) {
    for (var i = 0; i < state.personas.length; i++) {
      if (state.personas[i].name === name) return state.personas[i];
    }
    return null;
  }

  function toast(msg, kind) {
    try {
      if (window.toast) { window.toast(msg, kind || 'info'); return; }
    } catch (e) { /* у агента свой тост — воспользуемся своим */ }
    var t = el('div', 'ip-toast ' + (kind || 'info'), msg);
    document.body.appendChild(t);
    setTimeout(function () {
      t.style.opacity = '0';
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
    }, 2600);
  }

  function api(url, opts) {
    return fetch(url, opts || {}).then(function (r) {
      return r.text().then(function (txt) {
        var data = {};
        try { data = txt ? JSON.parse(txt) : {}; } catch (e) { data = { error: txt }; }
        if (!r.ok) throw new Error(data.error || ('ошибка ' + r.status));
        return data;
      });
    });
  }

  // ------------------------------------------------------------- загрузка
  /* Выбор личности хранится в профиле приложения на сервере. Раньше панель
     его игнорировала и при каждом открытии показывала первую личность, а в
     localStorage писала только палитра — поэтому страница агента и мини-окно
     могли разойтись по «активной» личности. */
  function selectionKey() { return 'aibar.persona.' + (state.profile || 'default'); }
  function rememberSelection(name) {
    try { localStorage.setItem(selectionKey(), name); } catch (e) { /* нет localStorage */ }
  }
  function persistSelection(name) {
    rememberSelection(name);
    return api(API + '/current', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name })
    }).catch(function () { /* выбор остаётся хотя бы локальным */ });
  }

  function load() {
    return api(API).then(function (d) {
      state.personas = d.personas || [];
      state.profile = d.profile || 'default';
      /* Серверный выбор — источник правды; текущий выбор не перетираем при
         повторной загрузке, иначе он откатится на время работы панели. */
      var want = state.loaded ? state.current : (d.current || state.current);
      if (!personaByName(want)) want = personaByName('default') ? 'default' :
        ((state.personas[0] && state.personas[0].name) || 'default');
      state.current = want;
      state.loaded = true;
      rememberSelection(state.current);
      renderList();
      renderDetail();
    }).catch(function (e) {
      toast('Не удалось загрузить личности: ' + e.message, 'error');
    });
  }

  function loadHistory() {
    state.messages = [];
    renderChat();
    return api(HIST + encodeURIComponent(SESSION_KEY) + '?profile=' + encodeURIComponent(state.current))
      .then(function (d) {
        state.messages = d.messages || [];
        renderChat();
      }).catch(function () { /* переписки ещё нет — это не ошибка */ });
  }

  // ------------------------------------------------------------- разметка
  function build() {
    root = el('div', 'ip-root');
    root.setAttribute('aria-hidden', 'true');

    var fab = el('button', 'ip-fab');
    fab.type = 'button';
    fab.title = 'Личности агента';
    fab.setAttribute('aria-label', 'Личности агента');
    fab.innerHTML = '<i class="fa-solid fa-masks-theater"></i>';
    fab.addEventListener('click', open);

    var modal = el('div', 'ip-modal');
    modal.innerHTML =
      '<div class="ip-panel">' +
        '<header class="ip-head">' +
          '<span class="ip-title"><i class="fa-solid fa-masks-theater"></i> Личности агента</span>' +
          '<span class="ip-spacer"></span>' +
          '<button class="ip-x" type="button" title="Закрыть"><i class="fa-solid fa-xmark"></i></button>' +
        '</header>' +
        '<div class="ip-body">' +
          '<aside class="ip-side">' +
            '<div class="ip-list" id="ipList"></div>' +
            '<button class="ip-add" type="button"><i class="fa-solid fa-plus"></i> Создать личность</button>' +
          '</aside>' +
          '<section class="ip-main">' +
            '<div class="ip-detail" id="ipDetail"></div>' +
            '<div class="ip-chat">' +
              '<div class="ip-chat-log" id="ipChatLog"></div>' +
              '<div class="ip-chat-input">' +
                '<textarea id="ipInput" rows="2" placeholder="Спросить личность…"></textarea>' +
                '<button class="ip-send" type="button" title="Отправить"><i class="fa-solid fa-paper-plane"></i></button>' +
              '</div>' +
            '</div>' +
          '</section>' +
        '</div>' +
      '</div>';

    root.appendChild(fab);
    root.appendChild(modal);
    document.body.appendChild(root);

    listEl = root.querySelector('#ipList');
    detailEl = root.querySelector('#ipDetail');
    chatEl = root.querySelector('#ipChatLog');
    inputEl = root.querySelector('#ipInput');

    modal.addEventListener('mousedown', function (e) {
      if (e.target === modal) close();
    });
    root.querySelector('.ip-x').addEventListener('click', close);
    root.querySelector('.ip-add').addEventListener('click', function () { openForm(null); });
    root.querySelector('.ip-send').addEventListener('click', send);
    inputEl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && root.classList.contains('open')) close();
    });
  }

  function open() {
    root.classList.add('open');
    root.setAttribute('aria-hidden', 'false');
    /* После загрузки списка подтягиваем переписку выбранной личности: без
       этого чат в панели оставался пустым до клика по строке. */
    load().then(loadHistory);
    setTimeout(function () { try { inputEl.focus(); } catch (e) { /* нет поля */ } }, 60);
  }

  function close() {
    root.classList.remove('open');
    root.setAttribute('aria-hidden', 'true');
  }

  // ------------------------------------------------------------- список
  function renderList() {
    if (!listEl) return;
    listEl.innerHTML = '';
    state.personas.forEach(function (p) {
      var row = el('button', 'ip-row' + (p.name === state.current ? ' on' : ''));
      row.type = 'button';
      row.innerHTML =
        '<span class="ip-ava">' + iconHtml(p.icon, 'fa-masks-theater') + '</span>' +
        '<span class="ip-rown">' +
          '<span class="ip-rname">' + esc(p.display_name || p.name) + '</span>' +
          '<span class="ip-rdesc">' + esc(p.description || p.soul_preview || '') + '</span>' +
        '</span>';
      row.addEventListener('click', function () {
        state.current = p.name;
        state.draft = null;
        persistSelection(p.name);
        renderList();
        renderDetail();
        loadHistory();
      });
      listEl.appendChild(row);
    });
  }

  // ------------------------------------------------------------- карточка
  function renderDetail() {
    if (!detailEl) return;
    var p = personaByName(state.current);
    detailEl.innerHTML = '';
    if (!p) {
      detailEl.innerHTML = '<div class="ip-empty">Личность не найдена</div>';
      return;
    }
    if (state.draft) { renderForm(p); return; }

    var head = el('div', 'ip-card');
    head.innerHTML =
      '<div class="ip-card-top">' +
        '<span class="ip-ava big">' + iconHtml(p.icon, 'fa-masks-theater') + '</span>' +
        '<div class="ip-card-n">' +
          '<div class="ip-card-t">' + esc(p.display_name || p.name) + '</div>' +
          '<div class="ip-card-s">' + esc(p.description || 'Без описания') + '</div>' +
        '</div>' +
      '</div>' +
      '<div class="ip-card-soul">' + esc(p.soul_preview || '') + '</div>' +
      '<div class="ip-card-a">' +
        '<button class="ip-mini" type="button" data-a="edit"><i class="fa-solid fa-pen"></i> Изменить</button>' +
        (p.name === 'default' ? '' :
          '<button class="ip-mini danger" type="button" data-a="del"><i class="fa-solid fa-trash"></i> Удалить</button>') +
      '</div>';
    detailEl.appendChild(head);

    head.querySelector('[data-a="edit"]').addEventListener('click', function () { openForm(p); });
    var del = head.querySelector('[data-a="del"]');
    if (del) del.addEventListener('click', function () { removePersona(p); });
  }

  function openForm(p) {
    state.draft = p || { name: '', display_name: '', description: '', soul: '' };
    renderDetail();
    var f = detailEl.querySelector('.ip-form');
    if (!f) return;
    var nameInput = f.querySelector('[name="name"]');
    if (nameInput && !p) setTimeout(function () { try { nameInput.focus(); } catch (e) { /* */ } }, 40);
  }

  function renderForm(p) {
    var d = state.draft;
    detailEl.innerHTML =
      '<form class="ip-form">' +
        '<label>Имя<span class="ip-req">*</span>' +
          '<input name="name" type="text" maxlength="64" placeholder="например: editor"' +
            (p && p.name ? ' value="' + esc(d.name) + '" readonly' : ' value="' + esc(d.name || '') + '"') +
            '><small>латиница, цифры, дефис. Папка профиля будет называться так.</small></label>' +
        '<label>Как показывать' +
          '<input name="display_name" type="text" maxlength="64" placeholder="Редактор" value="' + esc(d.display_name || '') + '"></label>' +
        '<label>Кто это' +
          '<input name="description" type="text" maxlength="200" placeholder="Помогает с текстами" value="' + esc(d.description || '') + '"></label>' +
        '<label>Личность (SOUL.md)' +
          '<textarea name="soul" rows="7" placeholder="Опишите характер, манеру, правила.">' + esc(d.soul || '') + '</textarea>' +
          '<small>Этот текст агент читает как свою личность: характер, манеру, что любит и чего не делает.</small></label>' +
        '<div class="ip-form-a">' +
          '<button class="ip-mini primary" type="submit">' + (p && p.name ? 'Сохранить' : 'Создать') + '</button>' +
          '<button class="ip-mini" type="button" data-a="cancel">Отмена</button>' +
        '</div>' +
      '</form>';

    detailEl.querySelector('[data-a="cancel"]').addEventListener('click', function () {
      state.draft = null;
      renderDetail();
    });
    detailEl.querySelector('form').addEventListener('submit', function (e) {
      e.preventDefault();
      saveForm(p);
    });
  }

  function saveForm(p) {
    var f = detailEl.querySelector('form');
    var data = {
      name: (f.querySelector('[name="name"]').value || '').trim(),
      display_name: (f.querySelector('[name="display_name"]').value || '').trim(),
      description: (f.querySelector('[name="description"]').value || '').trim(),
      soul: (f.querySelector('[name="soul"]').value || '').trim()
    };
    if (!data.name) { toast('Придумайте имя личности', 'warn'); return; }
    var btn = f.querySelector('button[type="submit"]');
    btn.disabled = true;
    var req = p && p.name
      ? api(API + '/' + encodeURIComponent(p.name), {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data)
        })
      : api(API, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data)
        });
    req.then(function (res) {
      state.draft = null;
      toast(p && p.name ? 'Личность сохранена' : 'Личность создана', 'ok');
      state.current = res.name || data.name;
      persistSelection(state.current);
      return load();
    }).catch(function (e) {
      toast('Не удалось сохранить: ' + e.message, 'error');
      btn.disabled = false;
    });
  }

  function removePersona(p) {
    if (!confirm('Удалить личность «' + (p.display_name || p.name) + '»?\nЕё переписка и память тоже будут удалены.')) return;
    api(API + '/' + encodeURIComponent(p.name), { method: 'DELETE' })
      .then(function () {
        toast('Личность удалена', 'ok');
        state.current = 'default';
        persistSelection(state.current);
        return load();
      })
      .catch(function (e) { toast('Не удалось удалить: ' + e.message, 'error'); });
  }

  // ------------------------------------------------------------- чат
  function renderChat() {
    if (!chatEl) return;
    chatEl.innerHTML = '';
    if (!state.messages.length) {
      chatEl.innerHTML = '<div class="ip-empty">Переписка с этой личностью ещё не начата</div>';
      return;
    }
    state.messages.forEach(function (m) {
      var b = el('div', 'ip-msg ' + (m.role === 'user' ? 'me' : 'ag'), m.content);
      chatEl.appendChild(b);
    });
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  function send() {
    if (state.busy) return;
    var text = (inputEl.value || '').trim();
    if (!text) return;
    var who = personaByName(state.current);
    inputEl.value = '';
    state.messages.push({ role: 'user', content: text });
    state.busy = true;
    renderChat();

    var bubble = el('div', 'ip-msg ag', '…');
    chatEl.appendChild(bubble);
    chatEl.scrollTop = chatEl.scrollHeight;

    var body = {
      message: text,
      session_key: SESSION_KEY,
      title: 'Личность: ' + ((who && (who.display_name || who.name)) || state.current),
      profile: state.current
    };

    fetch(ASK, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok || !r.body) throw new Error('агент недоступен');
      var reader = r.body.getReader();
      var dec = new TextDecoder();
      var buf = '';
      var answer = '';
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) {
            if (answer) { state.messages.push({ role: 'assistant', content: answer }); }
            state.busy = false;
            renderChat();
            return;
          }
          buf += dec.decode(res.value, { stream: true });
          var parts = buf.split('\n\n');
          buf = parts.pop();
          parts.forEach(function (chunk) {
            var ev = '', data = '';
            chunk.split('\n').forEach(function (line) {
              if (line.indexOf('event:') === 0) ev = line.slice(6).trim();
              else if (line.indexOf('data:') === 0) data += line.slice(5).trim();
            });
            if (ev === 'assistant.delta' || ev === 'assistant.completed') {
              try {
                var d = JSON.parse(data || '{}');
                var piece = d.delta || d.content || d.text || '';
                if (piece) {
                  answer += piece;
                  bubble.textContent = answer;
                  chatEl.scrollTop = chatEl.scrollHeight;
                }
              } catch (e) { /* не наш формат — пропускаем */ }
            } else if (ev === 'error') {
              bubble.textContent = 'Ошибка агента';
              state.busy = false;
            }
          });
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      bubble.textContent = 'Ошибка: ' + e.message;
      state.busy = false;
    });
  }

  // ------------------------------------------------------------- запуск
  function start() {
    build();
    load();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  window.InfinityPersonas = {
    open: open,
    close: close,
    list: function () { return state.personas; },
    select: function (name) {
      state.current = name;
      persistSelection(name);
      renderList();
      renderDetail();
      loadHistory();
    }
  };
})();
