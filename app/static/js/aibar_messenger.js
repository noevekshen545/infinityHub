/* Компактный мессенджер для окна палитры.
 *
 * Это дополнение к мессенджеру соцсети, а не его замена: полный мессенджер
 * в соцсети остаётся как был. Здесь — маленькая панель для быстрого чтения
 * и отправки сообщений из отдельного окна Electron.
 *
 * Работает по тем же API, что и большая страница:
 *   GET  /api/social/conversations                 — список диалогов
 *   GET  /api/social/conversations/{id}/messages   — сообщения
 *   POST /api/social/conversations/{id}/messages   — отправка
 *   WS   /api/social/ws                            — новые сообщения и «печатает»
 */
(function () {
  'use strict';

  var API = '/api/social';
  var POLL_MS = 4000;          /* подстраховка, если сокет отвалился */
  var TABS_DEFAULT = 3;        /* сколько последних сессий показываем */

  var box = null, mounted = false;
  var convs = [];
  var openIds = [];            /* сессии, показанные чипами */
  var expanded = false;        /* «…» раскрыл весь список */
  var activeId = '';
  var messages = [];
  var ws = null, wsRetry = 0, wsTimer = null, pollTimer = null;
  var picker = false;           /* открыт выбор собеседника для нового диалога */
  var unread = {};

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

  function api(url, opts) {
    return fetch(url, opts || {}).then(function (r) {
      if (!r.ok) throw new Error('ошибка ' + r.status);
      return r.json();
    });
  }

  function initial(name) {
    var s = String(name || '?').trim();
    return s ? s.charAt(0).toUpperCase() : '?';
  }

  function convById(id) {
    for (var i = 0; i < convs.length; i++) if (convs[i].id === id) return convs[i];
    return null;
  }

  function convTitle(c) {
    if (!c) return '';
    if (c.title) return c.title;
    var others = (c.members || []).filter(function (m) { return m.profile_id !== myId(); });
    if (others.length === 1) return others[0].display_name || 'Диалог';
    if (others.length > 1) return 'Группа (' + (others.length + 1) + ')';
    return 'Диалог';
  }

  var _myId = null;
  function myId() {
    return _myId || '';
  }

  /* Свой профиль нужен, чтобы отличить свои сообщения от чужих. */
  function loadMe() {
    if (_myId) return Promise.resolve(_myId);
    return api(API + '/me').then(function (d) {
      _myId = (d && (d.profile_id || d.id)) || '';
      return _myId;
    }).catch(function () { return ''; });
  }

  function fmtTime(ts) {
    var t = new Date((Number(ts) || 0) * 1000);
    if (!t.getTime()) return '';
    var d = new Date();
    var same = t.toDateString() === d.toDateString();
    var hh = String(t.getHours()).padStart(2, '0');
    var mm = String(t.getMinutes()).padStart(2, '0');
    if (same) return hh + ':' + mm;
    return String(t.getDate()).padStart(2, '0') + '.' + String(t.getMonth() + 1).padStart(2, '0');
  }

  // ------------------------------------------------------------- разметка
  function markup() {
    return '' +
      '<div class="aibar-msgs-head">' +
        '<button class="aibar-iconbtn aibar-msgs-back" title="К запросу"><i class="fas fa-arrow-left"></i></button>' +
        '<span class="aibar-msgs-title">Сообщения</span>' +
        '<span class="spacer"></span>' +
        '<button class="aibar-iconbtn aibar-msgs-new" title="Новый диалог"><i class="fas fa-square-pen"></i></button>' +
        '<button class="aibar-iconbtn aibar-msgs-pin" title="Закрепить окно"><i class="fas fa-thumbtack"></i></button>' +
      '</div>' +
      '<div class="aibar-msgs-tabs"></div>' +
      '<div class="aibar-msgs-body"></div>';
  }

  // ------------------------------------------------------------- данные
  function loadConvs() {
    return api(API + '/conversations').then(function (list) {
      convs = Array.isArray(list) ? list : [];
      if (!openIds.length) {
        openIds = convs.slice(0, TABS_DEFAULT).map(function (c) { return c.id; });
      }
      /* Чипы, которых больше нет, убираем — диалог могли удалить. */
      if (convs.length) {
        openIds = openIds.filter(function (id) { return !!convById(id); });
      }
      /* Кнопка в шапке сообщает о непрочитанных. */
      var total = 0;
      convs.forEach(function (c) { total += (c.unread_count || 0); });
      var btn = document.querySelector('.aibar-msgs-btn');
      if (btn) btn.classList.toggle('unread', total > 0);
      var navTab = document.querySelector('.aibar-nav-tab[data-view="messenger"]');
      if (navTab) navTab.classList.toggle('unread', total > 0);
      renderTabs();
      renderBody();
    }).catch(function () { /* соцсеть недоступна — покажем пусто */ });
  }

  function openConv(id) {
    activeId = id || '';
    messages = [];
    renderBody();
    if (!activeId) return Promise.resolve();
    return loadMessages();
  }

  function loadMessages() {
    if (!activeId) return Promise.resolve();
    return api(API + '/conversations/' + encodeURIComponent(activeId) + '/messages')
      .then(function (list) {
        if (!Array.isArray(list)) return;
        messages = list.map(function (m) {
          var a = null;
          try { a = m.attachments ? JSON.parse(m.attachments) : []; } catch (e) { a = []; }
          return {
            id: m.id, role: m.sender_id === myId() ? 'me' : 'ag',
            text: m.content || '', name: m.display_name || '',
            ts: m.created_at, read: !!m.is_read, attachments: a || []
          };
        });
        renderThread();
      }).catch(function () { /* переписка не пришла */ });
  }

  function send(text) {
    var t = String(text || '').trim();
    if (!t || !activeId) return;
    api(API + '/conversations/' + encodeURIComponent(activeId) + '/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: t, attachments: '[]', ttl: 0 })
    }).then(function () {
      loadMessages();
    }).catch(function () { /* отправка не удалась — сообщение не появится */ });
  }

  // ------------------------------------------------------------- отрисовка
  function renderTabs() {
    if (!box) return;
    var host = box.querySelector('.aibar-msgs-tabs');
    if (!host) return;
    host.innerHTML = '';
    var list = expanded ? convs : convs.filter(function (c) { return openIds.indexOf(c.id) >= 0; });
    if (!list.length) {
      var none = el('div', 'aibar-msgs-none', 'Диалогов пока нет');
      host.appendChild(none);
    }
    list.slice(0, expanded ? 200 : TABS_DEFAULT + 6).forEach(function (c) {
      var chip = el('div', 'aibar-msgs-chip' + (c.id === activeId ? ' on' : ''));
      var title = convTitle(c);
      chip.innerHTML = '<span class="aibar-msgs-chip-t">' + esc(title) + '</span>' +
                       '<button class="aibar-msgs-chip-x" title="Закрыть">×</button>' +
                       (c.unread_count ? '<span class="aibar-msgs-badge">' +
                         (c.unread_count > 99 ? '99+' : c.unread_count) + '</span>' : '');
      chip.querySelector('.aibar-msgs-chip-t').addEventListener('click', function () {
        openConv(c.id);
      });
      chip.querySelector('.aibar-msgs-chip-x').addEventListener('click', function (e) {
        e.stopPropagation();
        /* Закрыть сессию: убираем чип, вкладка перестаёт быть открытой. */
        openIds = openIds.filter(function (id) { return id !== c.id; });
        if (activeId === c.id) { activeId = ''; messages = []; }
        renderTabs();
        renderBody();
      });
      host.appendChild(chip);
    });

    var add = el('button', 'aibar-msgs-chip add', '+');
    add.title = 'Новый диалог';
    add.addEventListener('click', function (e) {
      e.stopPropagation();
      picker = true;
      activeId = '';
      renderBody();
    });
    host.appendChild(add);

    var more = el('button', 'aibar-msgs-chip more', '…');
    more.title = expanded ? 'Показать только последние' : 'Показать все диалоги';
    more.addEventListener('click', function (e) {
      e.stopPropagation();
      expanded = !expanded;
      renderTabs();
    });
    host.appendChild(more);
  }

  function renderBody() {
    if (!box) return;
    var host = box.querySelector('.aibar-msgs-body');
    if (!host) return;
    host.innerHTML = '';
    if (picker) { renderPicker(host); return; }
    if (!activeId) {
      host.className = 'aibar-msgs-body list';
      convs.forEach(function (c) {
        var row = el('button', 'aibar-msgs-conv');
        row.type = 'button';
        row.innerHTML =
          '<span class="aibar-msgs-ava">' + esc(initial(convTitle(c))) + '</span>' +
          '<span class="aibar-msgs-conv-n">' +
            '<span class="aibar-msgs-conv-t">' + esc(convTitle(c)) + '</span>' +
            '<span class="aibar-msgs-conv-p">' + esc(c.last_message || '') + '</span>' +
          '</span>' +
          '<span class="aibar-msgs-conv-time">' + esc(fmtTime(c.last_message_at || c.updated_at)) + '</span>' +
          (c.unread_count ? '<span class="aibar-msgs-badge">' +
            (c.unread_count > 99 ? '99+' : c.unread_count) + '</span>' : '');
        row.addEventListener('click', function () {
          if (openIds.indexOf(c.id) < 0) openIds.unshift(c.id);
          openConv(c.id);
        });
        host.appendChild(row);
      });
      if (!convs.length) {
        host.appendChild(el('div', 'aibar-msgs-none', 'Диалогов пока нет'));
      }
      return;
    }

    host.className = 'aibar-msgs-body thread';
    var thread = el('div', 'aibar-msgs-thread');
    messages.forEach(function (m) {
      var b = el('div', 'aibar-msgs-msg ' + m.role);
      b.innerHTML = '<span class="aibar-msgs-who">' + esc(m.name) + '</span>' +
                    '<span class="aibar-msgs-text">' + esc(m.text) + '</span>' +
                    '<span class="aibar-msgs-time">' + esc(fmtTime(m.ts)) + '</span>';
      thread.appendChild(b);
    });
    host.appendChild(thread);

    var foot = el('div', 'aibar-msgs-foot');
    var inp = el('input', 'aibar-msgs-input');
    inp.type = 'text';
    inp.placeholder = 'Сообщение…';
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        var v = inp.value;
        inp.value = '';
        send(v);
      }
    });
    /* Кнопка смайликов — в поле сообщений. */
    var emo = el('button', 'aibar-msgs-emo');
    emo.type = 'button';
    emo.title = 'Эмодзи';
    emo.innerHTML = '<i class="fa-regular fa-face-smile"></i>';
    emo.addEventListener('click', function (e) {
      e.stopPropagation();
      if (!window.AiBarEmoji) return;
      window.AiBarEmoji.open(emo);
    });
    var btn = el('button', 'aibar-msgs-send');
    /* Иконку ставим через innerHTML: el() задаёт textContent, и разметка
       попала бы в кнопку как текст. */
    btn.innerHTML = '<i class="fas fa-paper-plane"></i>';
    btn.addEventListener('click', function () {
      var v = inp.value;
      inp.value = '';
      send(v);
    });
    foot.appendChild(emo);
    foot.appendChild(inp);
    foot.appendChild(btn);
    host.appendChild(foot);
    host.scrollTop = host.scrollHeight;
    var t = host.querySelector('.aibar-msgs-thread');
    if (t) t.scrollTop = t.scrollHeight;
  }

  /* Выбор собеседника для нового диалога. */
  function renderPicker(host) {
    host.className = 'aibar-msgs-body list';
    var bar = el('div', 'aibar-msgs-pick');
    var back = el('button', 'aibar-msgs-pick-back');
    /* Иконку ставим через innerHTML: el() задаёт textContent, и разметка
       попала бы в кнопку как текст. */
    back.innerHTML = '<i class="fas fa-arrow-left"></i>';
    back.title = 'Назад к диалогам';
    back.addEventListener('click', function () { picker = false; renderBody(); });
    var search = el('input', 'aibar-msgs-pick-search');
    search.type = 'text';
    search.placeholder = 'Кого позовём?';
    var list = el('div', 'aibar-msgs-pick-list');
    search.addEventListener('input', function () { pickUsers(search.value, list); });
    bar.appendChild(back);
    bar.appendChild(search);
    host.appendChild(bar);
    host.appendChild(list);
    pickUsers('', list);
    setTimeout(function () { try { search.focus(); } catch (e) { /* */ } }, 40);
  }

  function pickUsers(q, host) {
    host.innerHTML = '';
    api(API + '/search?q=' + encodeURIComponent(q || '') + '&limit=30')
      .then(function (users) {
        (users || []).forEach(function (u) {
          if (u.profile_id === myId()) return;
          var row = el('button', 'aibar-msgs-user');
          row.type = 'button';
          row.innerHTML =
            '<span class="aibar-msgs-ava">' + esc(initial(u.display_name)) + '</span>' +
            '<span class="aibar-msgs-user-t">' + esc(u.display_name || u.profile_id) + '</span>';
          row.addEventListener('click', function () { startChat(u); });
          host.appendChild(row);
        });
        if (!host.children.length) {
          host.appendChild(el('div', 'aibar-msgs-none', 'Никого не нашлось'));
        }
      })
      .catch(function () {
        host.innerHTML = '';
        host.appendChild(el('div', 'aibar-msgs-none', 'Список пользователей недоступен'));
      });
  }

  function startChat(user) {
    api(API + '/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: '', member_ids: [user.profile_id] })
    }).then(function (d) {
      picker = false;
      if (d && d.id && openIds.indexOf(d.id) < 0) openIds.unshift(d.id);
      return loadConvs().then(function () { openConv(d && d.id); });
    }).catch(function (e) {
      var note = el('div', 'aibar-msgs-none', 'Не удалось создать диалог: ' + e.message);
      var host = box && box.querySelector('.aibar-msgs-pick-list');
      if (host) { host.innerHTML = ''; host.appendChild(note); }
    });
  }

  function renderThread() {
    renderBody();
  }

  // ------------------------------------------------------------- сокет
  function connectWS() {
    if (ws || !box) return;
    try {
      var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(proto + '//' + location.host + API + '/ws');
    } catch (e) {
      ws = null;
      return;
    }
    ws.onopen = function () { wsRetry = 0; };
    ws.onmessage = function (ev) {
      var d;
      try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (!d || !d.type) return;
      if (d.type === 'new_message') {
        if (d.conversation_id === activeId) loadMessages();
        loadConvs();
      } else if (d.type === 'typing' || d.type === 'typing_stopped') {
        /* Индикатор «печатает» в компактной панели не показываем: места
           мало, а пользы от одного слова мало. */
      }
    };
    ws.onclose = function () {
      ws = null;
      wsRetry = Math.min(wsRetry + 1, 6);
      wsTimer = setTimeout(connectWS, Math.min(30000, 1000 * Math.pow(2, wsRetry)));
    };
    ws.onerror = function () { try { ws.close(); } catch (e) { /* */ } };
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(function () {
      if (activeId) loadMessages();
      loadConvs();
    }, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // ------------------------------------------------------------- публичное
  function mount(container) {
    if (mounted && box && box.parentNode === container) return box;
    box = container;
    box.className = 'aibar-msgs';
    box.innerHTML = markup();
    mounted = true;

    var back = box.querySelector('.aibar-msgs-back');
    if (back) back.onclick = function () {
      if (picker) { picker = false; renderBody(); return; }
      if (activeId) { openConv(''); return; }
      if (window.AIBar && window.AIBar.setView) window.AIBar.setView('chat');
    };
    var pin = box.querySelector('.aibar-msgs-pin');
    if (pin) pin.onclick = function () {
      if (window.AIBar && window.AIBar.setPinned) window.AIBar.setPinned(true);
    };
    var nw = box.querySelector('.aibar-msgs-new');
    if (nw) nw.onclick = function () {
      picker = true;
      activeId = '';
      renderBody();
    };

    loadMe().then(function () {
      loadConvs();
    });
    connectWS();
    startPolling();
    return box;
  }

  function open() {
    loadConvs();
    if (activeId) loadMessages();
  }

  function close() {
    stopPolling();
  }

  window.AiBarMessenger = {
    mount: mount,
    open: open,
    close: close,
    conversations: function () { return convs; }
  };
})();
