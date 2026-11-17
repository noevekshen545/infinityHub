/* Режим ботов в интерфейсе агента (/agentai).

Комната — это обсуждение, где несколько личностей Hermes говорят по кругу.
Панель только показывает ленту и запускает ходы: сама переписка живёт на
сервере (файл комнаты профиля), поэтому закрытие окна не прерывает авто-режим.

Запросы идут по относительным путям /api/...: панель внедряется в HTML на
origin приложения, и там эти маршруты существуют. На прямой адрес веб-сервера
агента (:9120) панель не инжектится — там наших маршрутов нет. */
(function () {
  'use strict';
  if (window.__infinityBots) return;
  window.__infinityBots = true;

  var API = '/api/agentai/bots';
  var VCS = '/api/vcs';

  var state = {
    enabled: false,
    profile: 'default',
    personas: [],
    rooms: [],
    room: null,
    roomId: '',
    creating: false,
    busy: false,
    stream: null,
    poll: null,
    watch: null,
    git: { open: false, items: [], active: -1, commits: [], loading: false }
  };

  var root, fab, modal, statusEl, topicEl, feedEl, rosterEl, roomsEl,
      createEl, composeEl, sayEl, roundsEl, delayEl, gitEl, gitListEl, gitDiffEl,
      btnRound, btnAuto, btnStop, btnNew, btnGit;

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

  /* Иконка личности приходит с сервера классом Font Awesome (fa-*). В тексте
     ленты и ростера она должна краситься темой, как и везде в панели, поэтому
     рендерим <i>, а не эмодзи. */
  function iconHtml(icon, fallback) {
    var v = String(icon || '');
    if (/^fa-[a-z0-9-]+$/.test(v)) {
      return '<i class="fa-solid ' + v + '" aria-hidden="true"></i>';
    }
    return esc(v || fallback || '');
  }

  function api(url, opts) {
    return fetch(url, opts || {}).then(function (r) {
      return r.text().then(function (txt) {
        var data = {};
        try { data = txt ? JSON.parse(txt) : {}; } catch (e) { data = { error: txt }; }
        if (!r.ok) {
          var err = new Error(data.error || ('ошибка ' + r.status));
          err.status = r.status;
          err.code = data.code || '';
          throw err;
        }
        return data;
      });
    });
  }

  function json(method, url, body) {
    return api(url, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  }

  function toast(msg, kind) {
    try {
      if (window.toast) { window.toast(msg, kind || 'info'); return; }
    } catch (e) { /* у агента свой тост — воспользуемся своим */ }
    var t = el('div', 'ab-toast ' + (kind || 'info'), msg);
    document.body.appendChild(t);
    setTimeout(function () {
      t.style.opacity = '0';
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
    }, 2800);
  }

  function fmtTime(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function roomResult(d) {
    return (d && (d.result || d.room)) || null;
  }

  // ------------------------------------------------------------- разметка
  function build() {
    root = el('div', 'ab-root');
    root.setAttribute('aria-hidden', 'true');

    fab = el('button', 'ab-fab');
    fab.type = 'button';
    fab.title = 'Режим ботов: личности обсуждают проект кругами';
    fab.setAttribute('aria-label', 'Режим ботов');
    fab.innerHTML = '<i class="fa-solid fa-people-group"></i><span class="ab-dot"></span>';
    fab.addEventListener('click', open);

    modal = el('div', 'ab-modal');
    modal.innerHTML =
      '<div class="ab-panel">' +
        '<header class="ab-head">' +
          '<span class="ab-title"><i class="fa-solid fa-people-group"></i> Режим ботов</span>' +
          '<span class="ab-chip" id="abStatus">…</span>' +
          '<span class="ab-spacer"></span>' +
          '<button class="ab-btn" id="abGitBtn" type="button" title="Коммиты и изменения этой сессии">' +
            '<i class="fa-solid fa-code-branch"></i> Git</button>' +
          '<button class="ab-x" id="abClose" type="button" title="Закрыть"><i class="fa-solid fa-xmark"></i></button>' +
        '</header>' +
        '<div class="ab-body">' +
          '<section class="ab-main">' +
            '<div class="ab-topic"><span class="ab-topic-text" id="abTopic">Комната не выбрана</span></div>' +
            '<div class="ab-create" id="abCreate" style="display:none"></div>' +
            '<div class="ab-feed" id="abFeed"></div>' +
            '<div class="ab-compose" id="abCompose">' +
              '<div class="ab-controls">' +
                '<button class="ab-btn primary" id="abRound" type="button">' +
                  '<i class="fa-solid fa-rotate-right"></i> Круг</button>' +
                '<label>Авто до <input id="abRounds" type="number" min="1" max="20" value="3"> круг.</label>' +
                '<label>Пауза <input id="abDelay" type="number" min="0" max="60" step="1" value="3"> с.</label>' +
                '<button class="ab-btn" id="abAuto" type="button">' +
                  '<i class="fa-solid fa-forward"></i> Поехали</button>' +
                '<button class="ab-btn danger" id="abStop" type="button">' +
                  '<i class="fa-solid fa-stop"></i> Стоп</button>' +
                '<span class="ab-spacer"></span>' +
                '<button class="ab-btn" id="abDel" type="button" title="Удалить комнату">' +
                  '<i class="fa-solid fa-trash"></i></button>' +
              '</div>' +
              '<div class="ab-say">' +
                '<textarea id="abSay" rows="2" placeholder="Сказать ботам… (Enter — отправить, Shift+Enter — новая строка)"></textarea>' +
                '<button class="ab-btn primary" id="abSaySend" type="button">' +
                  '<i class="fa-solid fa-paper-plane"></i></button>' +
              '</div>' +
            '</div>' +
          '</section>' +
          '<aside class="ab-side">' +
            '<div class="ab-side-head">Комнаты<span class="ab-spacer"></span>' +
              '<button class="ab-btn" id="abNew" type="button"><i class="fa-solid fa-plus"></i></button></div>' +
            '<div class="ab-rooms" id="abRooms"></div>' +
            '<div class="ab-side-head">Состав</div>' +
            '<div class="ab-roster" id="abRoster"></div>' +
          '</aside>' +
        '</div>' +
      '</div>' +
      '<div class="ab-git" id="abGit">' +
        '<div class="ab-git-panel">' +
          '<header class="ab-head">' +
            '<span class="ab-title"><i class="fa-solid fa-code-branch"></i> Git этой сессии</span>' +
            '<span class="ab-chip" id="abGitChip">…</span>' +
            '<span class="ab-spacer"></span>' +
            '<button class="ab-btn primary" id="abGitCommit" type="button" title="Зафиксировать состояние профиля с привязкой к этой комнате">' +
              '<i class="fa-solid fa-floppy-disk"></i> Зафиксировать</button>' +
            '<button class="ab-btn" id="abGitReload" type="button"><i class="fa-solid fa-rotate"></i></button>' +
            '<button class="ab-x" id="abGitClose" type="button"><i class="fa-solid fa-xmark"></i></button>' +
          '</header>' +
          '<div class="ab-git-body">' +
            '<div class="ab-git-list" id="abGitList"></div>' +
            '<div class="ab-git-diff" id="abGitDiff"></div>' +
          '</div>' +
        '</div>' +
      '</div>';

    root.appendChild(fab);
    root.appendChild(modal);
    document.body.appendChild(root);

    var q = function (id) { return root.querySelector('#' + id); };
    statusEl = q('abStatus');
    topicEl = q('abTopic');
    feedEl = q('abFeed');
    rosterEl = q('abRoster');
    roomsEl = q('abRooms');
    createEl = q('abCreate');
    composeEl = q('abCompose');
    sayEl = q('abSay');
    roundsEl = q('abRounds');
    delayEl = q('abDelay');
    gitEl = q('abGit');
    gitListEl = q('abGitList');
    gitDiffEl = q('abGitDiff');
    btnRound = q('abRound');
    btnAuto = q('abAuto');
    btnStop = q('abStop');
    btnNew = q('abNew');
    btnGit = q('abGitBtn');

    modal.addEventListener('mousedown', function (e) {
      if (e.target === modal) close();
    });
    q('abClose').addEventListener('click', close);
    btnNew.addEventListener('click', showCreate);
    btnRound.addEventListener('click', function () { runRound(1, 0); });
    btnAuto.addEventListener('click', function () {
      runRound(parseInt(roundsEl.value, 10) || 3, parseFloat(delayEl.value) || 0);
    });
    btnStop.addEventListener('click', stopRound);
    q('abDel').addEventListener('click', deleteRoom);
    q('abSaySend').addEventListener('click', sendSay);
    sayEl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendSay(); }
    });
    btnGit.addEventListener('click', openGit);
    q('abGitClose').addEventListener('click', closeGit);
    q('abGitReload').addEventListener('click', loadGit);
    q('abGitCommit').addEventListener('click', commitSession);
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      if (gitEl.classList.contains('open')) closeGit();
      else if (root.classList.contains('open')) close();
    });
  }

  // ------------------------------------------------------------- открытие
  function open() {
    root.classList.add('open');
    root.setAttribute('aria-hidden', 'false');
    loadStatus().then(function () {
      if (!state.enabled) { renderDisabled(); return; }
      return Promise.all([loadRooms(), loadPersonas()]).then(function () {
        renderRooms();
        if (!state.room && state.rooms.length) {
          var want = remembered();
          var pick = null;
          for (var i = 0; i < state.rooms.length; i++) {
            if (state.rooms[i].id === want) { pick = state.rooms[i]; break; }
          }
          openRoom((pick || state.rooms[0]).id);
        } else if (!state.rooms.length) {
          renderFeed();
        }
      });
    }).catch(function (e) {
      toast('Режим ботов недоступен: ' + e.message, 'error');
    });
    setTimeout(function () { try { sayEl.focus(); } catch (e) { /* нет поля */ } }, 80);
  }

  function close() {
    root.classList.remove('open');
    root.setAttribute('aria-hidden', 'true');
    stopStream();
    hideCreate();
    if (gitEl.classList.contains('open')) closeGit();
  }

  function rememberRoom(id) {
    try { localStorage.setItem('ab.room.' + (state.profile || 'default'), id); } catch (e) { /* */ }
  }
  function remembered() {
    try { return localStorage.getItem('ab.room.' + (state.profile || 'default')) || ''; } catch (e) { return ''; }
  }

  // ------------------------------------------------------------- загрузка
  function loadStatus() {
    return api(API + '/status').then(function (d) {
      state.enabled = !!d.enabled;
      state.profile = d.profile || 'default';
      renderStatus();
    });
  }

  function loadRooms() {
    return api(API + '/rooms').then(function (d) {
      state.rooms = (d.result || d.rooms) || [];
      if (!state.room && !state.rooms.length) renderRooms();
    });
  }

  function loadPersonas() {
    return api(API + '/personas').then(function (d) {
      state.personas = d.personas || [];
    }).catch(function () { state.personas = []; });
  }

  function openRoom(id) {
    stopStream();
    state.roomId = id;
    rememberRoom(id);
    state.room = null;
    renderRooms();
    renderAll();
    return api(API + '/rooms/' + encodeURIComponent(id))
      .then(function (d) {
        state.room = roomResult(d);
        renderRooms();
        renderAll();
        startStream(id);
      })
      .catch(function (e) {
        toast('Комната не открылась: ' + e.message, 'error');
        state.roomId = '';
        renderAll();
      });
  }

  function refreshRoom() {
    if (!state.roomId) return Promise.resolve();
    return api(API + '/rooms/' + encodeURIComponent(state.roomId))
      .then(function (d) {
        var room = roomResult(d);
        if (room) { state.room = room; renderAll(); }
      })
      .catch(function () { /* мигновенный сбой: следующий снимок всё равно придёт */ });
  }

  // ------------------------------------------------------------- лента
  function startStream(id) {
    stopStream();
    if (!window.EventSource) { startPolling(); return; }
    try {
      state.stream = new EventSource(API + '/rooms/' + encodeURIComponent(id) + '/events');
    } catch (e) {
      startPolling();
      return;
    }
    state.stream.addEventListener('room', function (ev) {
      stopPolling();
      var room;
      try { room = JSON.parse(ev.data); } catch (e) { return; }
      if (!room || room.id !== state.roomId) return;
      state.room = room;
      renderAll();
    });
    state.stream.addEventListener('gone', function () {
      toast('Комната удалена', 'warn');
      state.room = null;
      state.roomId = '';
      stopStream();
      loadRooms().then(function () { renderRooms(); renderAll(); });
    });
    state.stream.onopen = function () { stopPolling(); };
    state.stream.onerror = function () { startPolling(); };
  }

  function startPolling() {
    if (state.poll) return;
    state.poll = setInterval(function () {
      if (!root.classList.contains('open')) { stopPolling(); return; }
      refreshRoom().then(function () {
        if (state.room && state.room.status !== 'running') stopPolling();
      });
    }, 2500);
  }

  function stopPolling() {
    if (state.poll) { clearInterval(state.poll); state.poll = null; }
  }

  function stopStream() {
    if (state.stream) {
      try { state.stream.close(); } catch (e) { /* */ }
      state.stream = null;
    }
    stopPolling();
  }

  // Комнату видно и с закрытой панелью: пока идёт авто-режим, редкий опрос
  // держит точку на кнопке актуальной.
  function ensureWatch() {
    var running = !!(state.room && state.room.status === 'running');
    if (running && !state.watch) {
      state.watch = setInterval(function () {
        if (!state.roomId) { clearInterval(state.watch); state.watch = null; return; }
        refreshRoom();
        loadRooms().then(function () {
          renderRooms();
          if (state.room && state.room.status !== 'running') {
            clearInterval(state.watch); state.watch = null;
          }
        }).catch(function () { /* */ });
      }, 8000);
    } else if (!running && state.watch) {
      clearInterval(state.watch); state.watch = null;
    }
  }

  // ------------------------------------------------------------- рендер
  function renderAll() {
    renderStatus();
    renderTopic();
    renderFeed();
    renderRoster();
    renderControls();
    ensureWatch();
  }

  function renderStatus() {
    if (!statusEl) return;
    var room = state.room;
    var txt = '—', on = false;
    if (!state.enabled) txt = 'выключен';
    else if (!room) txt = state.rooms.length ? 'нет комнаты' : 'нет комнат';
    else if (room.status === 'running') {
      on = true;
      var rounds = room.rounds || [];
      var idx = rounds.length ? rounds[rounds.length - 1].index : 0;
      if (room.auto) {
        txt = 'авто: круг ' + idx + ' из ' + room.auto.max_rounds;
      } else {
        txt = 'круг ' + idx + ' идёт';
      }
    } else txt = 'ожидание';
    statusEl.textContent = txt;
    statusEl.className = 'ab-chip' + (on ? ' on' : '');
    if (root) root.classList.toggle('running', !!(room && room.status === 'running'));
  }

  function renderTopic() {
    if (!topicEl) return;
    var room = state.room;
    if (!room) { topicEl.textContent = 'Комната не выбрана'; return; }
    topicEl.textContent = room.title + ' — ' + (room.topic || 'без темы');
    topicEl.title = room.topic || '';
  }

  function renderRooms() {
    if (!roomsEl) return;
    roomsEl.innerHTML = '';
    if (!state.rooms.length) {
      roomsEl.appendChild(el('div', 'ab-note', 'Комнат пока нет — создайте.'));
      return;
    }
    state.rooms.forEach(function (r) {
      var row = el('div', 'ab-room' + (r.id === state.roomId ? ' active' : ''));
      var head = el('div', 'ab-room-title', r.title || r.id);
      var sub = el('div', 'ab-room-sub',
        (r.bots || []).length + ' бота · кругов ' + (r.rounds || 0) +
        ' · ' + (r.status === 'running' ? 'идёт' : 'ожидание'));
      var del = el('button', 'ab-room-del');
      del.type = 'button';
      del.title = 'Удалить комнату';
      del.innerHTML = '<i class="fa-solid fa-trash"></i>';
      del.addEventListener('click', function (e) {
        e.stopPropagation();
        deleteRoom(r.id);
      });
      row.appendChild(head);
      row.appendChild(sub);
      row.appendChild(del);
      row.addEventListener('click', function () {
        if (r.id !== state.roomId) openRoom(r.id);
      });
      roomsEl.appendChild(row);
    });
  }

  function renderRoster() {
    if (!rosterEl) return;
    rosterEl.innerHTML = '';
    var room = state.room;
    if (!room) { rosterEl.appendChild(el('div', 'ab-note', 'Состав появится вместе с комнатой.')); return; }
    (room.bots || []).forEach(function (b) {
      var row = el('div', 'ab-bot');
      row.innerHTML =
        '<span class="ab-av">' + iconHtml(b.icon, 'fa-robot') + '</span>' +
        '<span><span class="ab-name">' + esc(b.display_name || b.persona) + '</span>' +
        '<span class="ab-role">' + esc(b.role || 'без роли') + '</span></span>';
      rosterEl.appendChild(row);
    });
  }

  function renderFeed() {
    if (!feedEl) return;
    var room = state.room;
    var near = feedEl.scrollTop + feedEl.clientHeight >= feedEl.scrollHeight - 90;
    feedEl.innerHTML = '';
    if (state.creating) return;

    if (!state.enabled) { renderDisabled(); return; }
    if (!room) {
      var hint = el('div', 'ab-empty');
      hint.innerHTML = state.rooms.length
        ? 'Выберите комнату слева.'
        : 'Комнат ещё нет.<br><button class="ab-btn primary" id="abEmptyNew" type="button">' +
          '<i class="fa-solid fa-plus"></i> Создать первую комнату</button>';
      feedEl.appendChild(hint);
      var b = feedEl.querySelector('#abEmptyNew');
      if (b) b.addEventListener('click', showCreate);
      return;
    }
    var msgs = room.messages || [];
    if (!msgs.length) {
      feedEl.appendChild(el('div', 'ab-empty',
        'Пусто. Нажмите «Круг», чтобы боты сказали по реплике, или «Поехали» — авто-режим до лимита.'));
      return;
    }
    var rounds = {};
    (room.rounds || []).forEach(function (r) { rounds[r.index] = r; });
    var last = -1;
    msgs.forEach(function (m) {
      var idx = parseInt(m.round || 0, 10) || 0;
      if (idx !== last) {
        last = idx;
        var info = rounds[idx] || {};
        var label = 'Круг ' + idx;
        if (info.interrupted) label += ' · прерван';
        else if (info.finished && info.failed) label += ' · ошибок: ' + info.failed;
        feedEl.appendChild(el('div', 'ab-round', label));
      }
      feedEl.appendChild(msgNode(m));
    });
    if (near) feedEl.scrollTop = feedEl.scrollHeight;
  }

  function msgNode(m) {
    var cls = 'ab-msg ' + (m.kind === 'user' ? 'user' : 'bot') + (m.error ? ' err' : '');
    var node = el('div', cls);
    var av = el('span', 'ab-av');
    av.innerHTML = m.kind === 'user'
      ? '<i class="fa-solid fa-user" aria-hidden="true"></i>'
      : iconHtml(m.icon, 'fa-robot');
    var body = el('div');
    var who = el('div', 'ab-who');
    who.innerHTML = '<b>' + esc(m.kind === 'user' ? 'Вы' : (m.display_name || m.bot || 'бот')) + '</b>' +
      (m.kind === 'bot' && m.role ? ' · ' + esc(m.role) : '') +
      ' <span>' + esc(fmtTime(m.ts)) + '</span>';
    var bub = el('div', 'ab-bubble', m.text || '');
    body.appendChild(who);
    body.appendChild(bub);
    node.appendChild(av);
    node.appendChild(body);
    return node;
  }

  function renderDisabled() {
    feedEl.innerHTML = '';
    composeEl.style.display = 'none';
    var box = el('div', 'ab-empty');
    box.innerHTML = 'Режим ботов выключен.<br><span class="ab-note">Включите в админке: ' +
      'модули → «Режим ботов» (agentai_bots.enabled).</span>';
    feedEl.appendChild(box);
  }

  function renderControls() {
    var room = state.room;
    var running = !!(room && room.status === 'running');
    var canRun = state.enabled && !!room && !state.busy;
    composeEl.style.display = state.enabled ? '' : 'none';
    btnRound.disabled = !canRun || running;
    btnAuto.disabled = !canRun || running;
    btnStop.disabled = !(state.enabled && running);
    sayEl.disabled = !state.enabled || !room;
    btnGit.disabled = !(state.enabled && room);
    root.querySelector('#abDel').disabled = !state.enabled || !room;
    if (running) {
      btnStop.innerHTML = '<i class="fa-solid fa-stop"></i> Стоп';
    }
  }

  // ------------------------------------------------------------- действия
  function runRound(rounds, delay) {
    if (!state.roomId) { toast('Сначала выберите комнату', 'warn'); return; }
    state.busy = true;
    renderControls();
    var url = rounds > 1
      ? API + '/rooms/' + encodeURIComponent(state.roomId) + '/auto'
      : API + '/rooms/' + encodeURIComponent(state.roomId) + '/round';
    var body = rounds > 1 ? { max_rounds: rounds, delay_sec: delay } : {};
    json('POST', url, body).then(function (d) {
      if (d.ok === false) throw new Error(d.error || 'не запустилось');
      toast(rounds > 1 ? ('Авто-режим: до ' + rounds + ' кругов') : 'Круг пошёл', 'ok');
      return refreshRoom();
    }).catch(function (e) {
      toast(e.message, 'error');
    }).then(function () {
      state.busy = false;
      renderControls();
    });
  }

  function stopRound() {
    if (!state.roomId) return;
    json('POST', API + '/rooms/' + encodeURIComponent(state.roomId) + '/stop', {})
      .then(function (d) {
        var r = d.result || {};
        toast(r.stopping ? 'Останавливаю после текущей реплики' : 'Комната и так тиха', 'info');
        return refreshRoom();
      })
      .catch(function (e) { toast(e.message, 'error'); });
  }

  function sendSay() {
    var text = (sayEl.value || '').trim();
    if (!text || !state.roomId) return;
    sayEl.value = '';
    json('POST', API + '/rooms/' + encodeURIComponent(state.roomId) + '/say', { text: text })
      .then(function (d) {
        var room = roomResult(d);
        if (room) { state.room = room; renderAll(); }
        else refreshRoom();
      })
      .catch(function (e) {
        toast(e.message, 'error');
        sayEl.value = text;
      });
  }

  function deleteRoom(id) {
    var rid = id || state.roomId;
    if (!rid) return;
    var room = state.room;
    var title = (id && id !== state.roomId) ? rid : ((room && room.title) || rid);
    if (!confirm('Удалить комнату «' + title + '» с её лентой?')) return;
    json('DELETE', API + '/rooms/' + encodeURIComponent(rid))
      .then(function () {
        toast('Комната удалена', 'ok');
        if (rid === state.roomId) {
          state.roomId = '';
          state.room = null;
          stopStream();
        }
        return loadRooms();
      })
      .then(function () { renderRooms(); renderAll(); })
      .catch(function (e) { toast(e.message, 'error'); });
  }

  // ------------------------------------------------------------- создание
  function showCreate() {
    if (!state.enabled) { toast('Сначала включите режим ботов в админке', 'warn'); return; }
    state.creating = true;
    feedEl.innerHTML = '';
    createEl.style.display = '';
    createEl.innerHTML =
      '<h4>Новая комната</h4>' +
      '<label class="ab-note">Тема обсуждения' +
        '<textarea name="topic" rows="3" placeholder="Что обсуждают боты: например «как поделить задачи, чтобы не конфликтовать правками»"></textarea></label>' +
      '<label class="ab-note">Название (необязательно)' +
        '<input name="title" type="text" maxlength="120" placeholder="по первому абзацу темы"></label>' +
      '<div class="ab-note">Боты (минимум два, порядок — очередь реплик в круге):</div>' +
      '<div class="ab-pick" id="abPick"></div>' +
      '<div class="ab-note" id="abPickHint"></div>' +
      '<div class="ab-controls">' +
        '<button class="ab-btn primary" id="abCreateGo" type="button">Создать</button>' +
        '<button class="ab-btn" id="abCreateCancel" type="button">Отмена</button>' +
      '</div>';

    var pick = createEl.querySelector('#abPick');
    if (!state.personas.length) {
      pick.appendChild(el('div', 'ab-note', 'Личности не загружаются: шлюз агента недоступен.'));
    }
    state.personas.forEach(function (p, i) {
      var row = el('label', 'ab-pick-row');
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = p.name;
      cb.id = 'abp-' + i;
      var info = el('div');
      info.innerHTML = '<div class="ab-pname"><span class="ab-picon">' +
        iconHtml(p.icon, 'fa-robot') + '</span> ' +
        esc(p.display_name || p.name) + '</div>' +
        '<div class="ab-pdesc">' + esc(p.description || p.name) + '</div>';
      var role = document.createElement('input');
      role.type = 'text';
      role.maxLength = 200;
      role.placeholder = 'роль в обсуждении';
      row.appendChild(cb);
      row.appendChild(info);
      row.appendChild(role);
      pick.appendChild(row);
      cb.addEventListener('change', updatePickHint);
      role.addEventListener('input', function () { cb.checked = true; updatePickHint(); });
    });
    function updatePickHint() {
      var n = pick.querySelectorAll('input[type=checkbox]:checked').length;
      createEl.querySelector('#abPickHint').textContent = n >= 2
        ? ('Ботов выбрано: ' + n)
        : 'Нужно минимум 2 бота (выбрано ' + n + ')';
    }
    updatePickHint();

    createEl.querySelector('#abCreateGo').addEventListener('click', submitCreate);
    createEl.querySelector('#abCreateCancel').addEventListener('click', hideCreate);
  }

  function submitCreate() {
    var topic = (createEl.querySelector('[name=topic]').value || '').trim();
    var title = (createEl.querySelector('[name=title]').value || '').trim();
    var bots = [];
    var rows = createEl.querySelectorAll('.ab-pick-row');
    for (var i = 0; i < rows.length; i++) {
      var cb = rows[i].querySelector('input[type=checkbox]');
      if (cb && cb.checked) {
        bots.push({ persona: cb.value,
                    role: (rows[i].querySelector('input[type=text]').value || '').trim() });
      }
    }
    if (bots.length < 2) { toast('Выберите минимум двух ботов', 'warn'); return; }
    var go = createEl.querySelector('#abCreateGo');
    go.disabled = true;
    json('POST', API + '/rooms', {
      title: title, topic: topic, bots: bots,
      max_rounds: parseInt(roundsEl.value, 10) || 3,
      delay_sec: parseFloat(delayEl.value) || 3
    }).then(function (d) {
      var room = roomResult(d);
      hideCreate();
      toast('Комната создана', 'ok');
      return loadRooms().then(function () { renderRooms(); if (room) openRoom(room.id); });
    }).catch(function (e) {
      toast(e.message, 'error');
      go.disabled = false;
    });
  }

  function hideCreate() {
    state.creating = false;
    createEl.style.display = 'none';
    createEl.innerHTML = '';
    renderFeed();
  }

  // ------------------------------------------------------------- Git
  function openGit() {
    if (!state.room) { toast('Комната не выбрана', 'warn'); return; }
    gitEl.classList.add('open');
    state.git.open = true;
    loadGit();
  }

  function closeGit() {
    gitEl.classList.remove('open');
    state.git.open = false;
  }

  /* Коммит привязывается к id комнаты: так кнопка Git показывает историю
     именно этого обсуждения, а не всей переписки профиля. */
  function commitSession() {
    if (!state.room) return;
    var message = (prompt('Сообщение коммита',
                           'боты · ' + (state.room.title || state.room.id)) || '').trim();
    if (!message) return;
    var btn = root.querySelector('#abGitCommit');
    btn.disabled = true;
    json('POST', VCS + '/commit', { message: message, session_id: state.room.id })
      .then(function (row) {
        toast('Коммит ' + (row.short || row.id || '') + ' записан', 'ok');
        loadGit();
      })
      .catch(function (e) {
        toast(e.status === 403 ? 'Свой VCS выключен — коммит недоступен' : e.message, 'error');
      })
      .then(function () { btn.disabled = false; });
  }

  function setGitChip(txt, on) {
    var c = root.querySelector('#abGitChip');
    c.textContent = txt;
    c.className = 'ab-chip' + (on ? ' on' : '');
  }

  function loadGit() {
    if (!state.room) return;
    state.git.loading = true;
    setGitChip('загрузка…');
    gitListEl.innerHTML = '';
    gitDiffEl.innerHTML = '';
    var sessionId = state.room.id;
    Promise.all([
      api(VCS + '/log?limit=50').catch(function (e) { return { __err: e }; }),
      api(VCS + '/status').catch(function () { return null; })
    ]).then(function (res) {
      state.git.loading = false;
      var logRow = res[0], statusRow = res[1];
      if (logRow && logRow.__err) {
        var err = logRow.__err;
        setGitChip(err.status === 403 ? 'VCS выключен' : 'ошибка');
        gitListEl.appendChild(el('div', 'ab-note',
          err.status === 403
            ? 'Свой VCS выключен: включите модуль «Свой VCS» в админке.'
            : 'Не удалось прочитать историю: ' + err.message));
        return;
      }
      var all = (logRow && logRow.commits) || [];
      var mine = all.filter(function (c) { return c.session_id === sessionId; });
      state.git.commits = mine;
      var items = [];
      if (statusRow && !statusRow.clean) items.push({ kind: 'work', label: 'Рабочее дерево', sub: statusRow });
      mine.forEach(function (c) {
        items.push({ kind: 'commit', id: c.id, label: c.message || '(без сообщения)',
                     sub: c.short + ' · ' + fmtTime(c.created_at) +
                          (c.author ? ' · ' + c.author : ''), parents: c.parents || [] });
      });
      state.git.items = items;
      setGitChip(items.length ? (mine.length + ' комм. · ' + all.length + ' в профиле') : 'пусто', items.length > 0);
      renderGitList();
      if (items.length) selectGit(0);
      else renderGitNote('Изменений нет: у этой сессии ещё не было коммитов, ' +
                         'а рабочее дерево совпадает с HEAD.');
    });
  }

  function renderGitList() {
    gitListEl.innerHTML = '';
    state.git.items.forEach(function (it, i) {
      var row = el('div', 'ab-commit' + (i === state.git.active ? ' active' : ''));
      row.innerHTML = '<div class="ab-cm">' + esc(it.kind === 'work' ? '● ' + it.label : it.label) + '</div>' +
        '<div class="ab-cs">' + esc(it.kind === 'work'
          ? 'незакоммиченных файлов: ' + (it.sub.changes || []).length
          : it.sub) + '</div>';
      row.addEventListener('click', function () { selectGit(i); });
      gitListEl.appendChild(row);
    });
  }

  function renderGitNote(text) {
    gitDiffEl.innerHTML = '';
    gitDiffEl.appendChild(el('div', 'ab-empty', text));
  }

  function selectGit(i) {
    state.git.active = i;
    renderGitList();
    var it = state.git.items[i];
    if (!it) return;
    gitDiffEl.innerHTML = '';
    if (it.kind === 'work') {
      api(VCS + '/diff').then(showDiff).catch(function (e) { renderGitNote('Дифф не получен: ' + e.message); });
      return;
    }
    if (!it.parents.length) {
      api(VCS + '/show?commit=' + encodeURIComponent(it.id)).then(function (row) {
        gitDiffEl.innerHTML = '';
        gitDiffEl.appendChild(el('div', 'ab-files',
          'Первый коммит — диффа нет. Файлов: ' + (row.file_count || 0) +
          ', размер: ' + Math.round((row.total_bytes || 0) / 1024) + ' КБ'));
        var list = el('pre');
        (row.files || []).forEach(function (f) {
          list.appendChild(lineSpan(' ', f.path + '  ' + f.size + ' б\n'));
        });
        gitDiffEl.appendChild(list);
      }).catch(function (e) { renderGitNote('Коммит не прочитан: ' + e.message); });
      return;
    }
    var url = VCS + '/diff?from=' + encodeURIComponent(it.parents[0]) +
              '&to=' + encodeURIComponent(it.id);
    api(url).then(showDiff).catch(function (e) { renderGitNote('Дифф не получен: ' + e.message); });
  }

  function lineSpan(cls, text) {
    var s = document.createElement('span');
    if (cls) s.className = cls;
    s.textContent = text;
    return s;
  }

  function diffClass(line) {
    if (line.indexOf('@@') === 0) return 'hunk';
    if (line[0] === '+') return 'add';
    if (line[0] === '-') return 'del';
    return '';
  }

  function showDiff(row) {
    gitDiffEl.innerHTML = '';
    var changes = (row && row.changes) || [];
    if (!changes.length) {
      renderGitNote('Изменений нет — дерево совпадает с этим коммитом.');
      return;
    }
    var head = el('div', 'ab-files');
    head.textContent = (row.from_short || '…') + ' → ' +
      (row.to === 'worktree' ? 'рабочее дерево' : (row.to_short || row.to)) +
      ' · файлов: ' + changes.length;
    gitDiffEl.appendChild(head);
    changes.forEach(function (ch) {
      var bar = el('div', 'ab-files');
      bar.textContent = ch.path + ' · ' + ch.status +
        (ch.note ? ' · ' + ch.note : '') + ' · ' + ch.old_size + '→' + ch.size + ' б';
      gitDiffEl.appendChild(bar);
      if (ch.binary || !ch.diff) return;
      var pre = el('pre');
      ch.diff.split('\n').forEach(function (line) {
        pre.appendChild(lineSpan(diffClass(line), line + '\n'));
      });
      gitDiffEl.appendChild(pre);
    });
  }

  // ------------------------------------------------------------- запуск
  function start() {
    build();
    loadStatus().catch(function () { /* панель покажет ошибку при открытии */ });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  window.InfinityBots = {
    open: open,
    close: close,
    state: state,
    refresh: refreshRoom,
    reload: loadRooms
  };
})();
