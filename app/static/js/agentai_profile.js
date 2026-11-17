/* Панель профиля в интерфейсе агента (/agentai).

Показывает то, что принадлежит этому профилю приложения: его чаты с выборкой
по названию, периоду и личности, комнаты режима ботов и коммиты его же VCS.
Изоляция на сервере — панель лишь спрашивает у /api/agentai/profile/panel и
рисует ответ: профиль определяется по cookie запроса, а список чатов проходит
гейт видимости, тот же, что у списка переписок в интерфейсе.

Данные не кэшируются: при смене профиля приложения та же кнопка обязана
показать сводку нового профиля, а не прошлый снимок.

Запросы идут по относительным путям /api/... — панель внедряется только в
HTML на origin приложения, где эти маршруты есть. */
(function () {
  'use strict';
  if (window.__infinityProfilePanel) return;
  window.__infinityProfilePanel = true;

  var API = '/api/agentai/profile/panel';
  var PAGE = 20;

  var state = {
    open: false,
    loading: false,
    data: null,
    error: '',
    q: '',
    persona: '',
    days: 0,
    limit: PAGE
  };

  var root, fab, modal, bodyEl, chipEl, searchEl, personaEl, daysEl, moreEl;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function ico(name, cls) {
    var i = el('i', (cls ? cls + ' ' : '') + 'fa-solid ' + name);
    i.setAttribute('aria-hidden', 'true');
    return i;
  }

  /* Строка-заголовок секции и пустое состояние: без иконки блок выглядит
     как обрезанный список, а с ней — как законченная секция панели. */
  function notice(icon, text, cls) {
    var n = el('div', cls || 'ap-empty');
    n.appendChild(ico(icon));
    n.appendChild(el('span', null, text));
    return n;
  }

  function selectBox(icon, node) {
    var wrap = el('div', 'ap-select');
    wrap.appendChild(ico(icon));
    wrap.appendChild(node);
    return wrap;
  }

  function api(url) {
    return fetch(url, { credentials: 'same-origin' }).then(function (r) {
      return r.text().then(function (txt) {
        var data = {};
        try { data = txt ? JSON.parse(txt) : {}; } catch (e) { data = { error: txt }; }
        if (!r.ok) {
          var err = new Error(data.error || ('ошибка ' + r.status));
          err.status = r.status;
          throw err;
        }
        return data;
      });
    });
  }

  function toast(msg, kind) {
    try {
      if (window.toast) { window.toast(msg, kind || 'info'); return; }
    } catch (e) { /* у агента свой тост — воспользуемся своим */ }
    var t = el('div', 'ap-toast ' + (kind || 'info'), msg);
    document.body.appendChild(t);
    setTimeout(function () {
      t.style.opacity = '0';
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
    }, 2800);
  }

  function whenReady(fn) {
    if (document.body) { fn(); return; }
    document.addEventListener('DOMContentLoaded', fn, { once: true });
  }

  function ago(ts) {
    if (!ts) return '';
    var s = Math.max(0, (Date.now() / 1000) - ts);
    if (s < 60) return 'сейчас';
    if (s < 3600) return Math.floor(s / 60) + ' мин назад';
    if (s < 86400) return Math.floor(s / 3600) + ' ч назад';
    if (s < 86400 * 30) return Math.floor(s / 86400) + ' дн назад';
    var d = new Date(ts * 1000);
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear();
  }

  // ------------------------------------------------------------------ разметка
  function build() {
    root = el('div', 'ap-root');
    root.setAttribute('data-infinity', 'profile-panel');

    fab = el('button', 'ap-fab');
    fab.type = 'button';
    fab.title = 'Панель профиля';
    fab.setAttribute('aria-label', 'Панель профиля');
    fab.innerHTML = '<i class="fa-solid fa-id-card"></i>';
    fab.addEventListener('click', open);

    modal = el('div', 'ap-modal');
    var panel = el('div', 'ap-panel');

    var head = el('div', 'ap-head');
    var title = el('div', 'ap-title');
    title.appendChild(ico('fa-id-card'));
    title.appendChild(el('span', null, 'Панель профиля'));
    head.appendChild(title);
    chipEl = el('div', 'ap-chip', '—');
    head.appendChild(chipEl);
    head.appendChild(el('div', 'ap-spacer'));

    var reload = el('button', 'ap-btn');
    reload.type = 'button';
    reload.appendChild(ico('fa-rotate'));
    reload.appendChild(el('span', null, 'Обновить'));
    reload.addEventListener('click', function () { load(); });
    head.appendChild(reload);

    var close = el('button', 'ap-btn ap-ghost');
    close.type = 'button';
    close.appendChild(ico('fa-xmark'));
    close.appendChild(el('span', null, 'Закрыть'));
    close.addEventListener('click', hide);
    head.appendChild(close);
    panel.appendChild(head);

    var filters = el('div', 'ap-filters');
    var field = el('div', 'ap-field');
    field.appendChild(ico('fa-magnifying-glass'));
    searchEl = el('input');
    searchEl.type = 'search';
    searchEl.placeholder = 'Поиск по названию чата';
    searchEl.addEventListener('input', function () {
      state.q = searchEl.value || '';
      scheduleLoad();
    });
    field.appendChild(searchEl);
    filters.appendChild(field);

    personaEl = el('select');
    personaEl.title = 'Чаты какой личности показать';
    personaEl.addEventListener('change', function () {
      state.persona = personaEl.value || '';
      state.limit = PAGE;
      load();
    });
    filters.appendChild(selectBox('fa-masks-theater', personaEl));

    daysEl = el('select');
    [['0', 'За всё время'], ['1', 'За сутки'], ['7', 'За неделю'],
     ['30', 'За месяц']].forEach(function (pair) {
      var o = el('option', null, pair[1]);
      o.value = pair[0];
      daysEl.appendChild(o);
    });
    daysEl.addEventListener('change', function () {
      state.days = parseInt(daysEl.value || '0', 10) || 0;
      state.limit = PAGE;
      load();
    });
    filters.appendChild(selectBox('fa-clock-rotate-left', daysEl));
    panel.appendChild(filters);

    bodyEl = el('div', 'ap-body');
    bodyEl.appendChild(el('div', 'ap-off', 'Загрузка…'));
    panel.appendChild(bodyEl);

    modal.appendChild(panel);
    modal.addEventListener('mousedown', function (e) {
      if (e.target === modal) hide();
    });

    root.appendChild(fab);
    root.appendChild(modal);
    document.body.appendChild(root);

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && state.open) hide();
    });
  }

  function fillPersonas() {
    var rows = (state.data && state.data.personas) || [];
    var current = state.persona;
    personaEl.innerHTML = '';
    var all = el('option', null, 'личность: общая');
    all.value = '';
    personaEl.appendChild(all);
    rows.forEach(function (p) {
      var o = el('option', null, p.display_name || p.name);
      o.value = p.name;
      personaEl.appendChild(o);
    });
    personaEl.value = current;
    if (personaEl.selectedIndex < 0) { personaEl.value = ''; state.persona = ''; }
  }

  // ---------------------------------------------------------------------- данные
  var debounce = null;
  function scheduleLoad() {
    if (debounce) clearTimeout(debounce);
    debounce = setTimeout(function () {
      debounce = null;
      state.limit = PAGE;
      load();
    }, 350);
  }

  function load() {
    if (state.loading) return;
    state.loading = true;
    render();
    var qs = '?persona=' + encodeURIComponent(state.persona) +
      '&q=' + encodeURIComponent(state.q) +
      '&days=' + encodeURIComponent(state.days) +
      '&limit=' + encodeURIComponent(state.limit) +
      '&offset=0';
    api(API + qs).then(function (d) {
      state.data = d;
      state.loading = false;
      render();
      fillPersonas();
    }).catch(function (e) {
      state.loading = false;
      state.error = e.message || 'ошибка';
      render();
      toast(state.error, 'error');
    });
  }

  function stat(num, label, icon) {
    var s = el('div', 'ap-stat');
    s.appendChild(ico(icon || 'fa-circle-dot', 'ap-stat-i'));
    var txt = el('div', 'ap-stat-t');
    txt.appendChild(el('b', null, String(num)));
    txt.appendChild(el('span', null, label));
    s.appendChild(txt);
    return s;
  }

  function section(title, rows, empty, icon) {
    var wrap = el('div', 'ap-section');
    var h = el('h3');
    h.appendChild(ico(icon || 'fa-list', 'ap-sec-i'));
    h.appendChild(el('span', null, title));
    wrap.appendChild(h);
    if (!rows.length) {
      wrap.appendChild(notice('fa-inbox', empty));
      return wrap;
    }
    rows.forEach(function (r) { wrap.appendChild(r); });
    return wrap;
  }

  function row(nameNode, metaText, extras) {
    var r = el('div', 'ap-row');
    var main = el('div', 'ap-main');
    main.appendChild(nameNode);
    main.appendChild(el('div', 'ap-meta', metaText));
    r.appendChild(main);
    (extras || []).forEach(function (x) { r.appendChild(x); });
    return r;
  }

  function chatRow(s) {
    var name = el('div', 'ap-name', s.title || 'Без названия');
    var meta = [
      (s.persona || 'default'),
      s.messages + ' сообщ.',
      ago(s.last_active)
    ].filter(Boolean).join(' · ');
    var extras = [];
    if (s.pinned) extras.push(el('span', 'ap-tag', 'закреплён'));
    (s.commits || []).slice(0, 3).forEach(function (h) {
      extras.push(el('span', 'ap-hash', h));
    });
    return row(name, meta, extras);
  }

  function roomRow(rm) {
    var name = el('div', 'ap-name', rm.title || rm.id);
    var status = rm.status === 'running' ? 'работает' : 'ожидает';
    var meta = [
      (rm.bots || []).length + ' бота',
      rm.rounds + ' круг(ов)',
      rm.messages + ' реплик',
      status,
      ago(rm.updated)
    ].filter(Boolean).join(' · ');
    var tag = el('span', 'ap-tag' + (rm.status === 'running' ? ' run' : ''), status);
    return row(name, meta, [tag]);
  }

  function commitRow(c) {
    var name = el('div', 'ap-name', c.message || 'без сообщения');
    var meta = [(c.session_id || 'без сессии'), ago(c.created_at)].join(' · ');
    return row(name, meta, [el('span', 'ap-hash', c.short || '')]);
  }

  function render() {
    if (!root) return;
    var d = state.data;
    chipEl.textContent = d ? ('профиль: ' + d.profile) : 'нет данных';
    bodyEl.innerHTML = '';

    if (state.loading && !d) {
      bodyEl.appendChild(notice('fa-spinner fa-spin', 'Загрузка…', 'ap-off'));
      return;
    }
    if (!d) {
      bodyEl.appendChild(notice(state.error ? 'fa-triangle-exclamation' : 'fa-inbox',
        state.error ? ('Панель не ответила: ' + state.error) : 'Данных нет.', 'ap-off'));
      return;
    }

    var counts = d.counts || {};
    var stats = el('div', 'ap-stats');
    stats.appendChild(stat(counts.sessions || 0, 'чатов профиля', 'fa-comments'));
    stats.appendChild(stat(counts.rooms || 0, 'комнат ботов', 'fa-people-group'));
    stats.appendChild(stat(counts.rooms_running || 0, 'из них работают', 'fa-bolt'));
    stats.appendChild(stat(counts.messages || 0, 'реплик в комнатах', 'fa-message'));
    stats.appendChild(stat(counts.commits || 0, 'коммитов в VCS', 'fa-code-branch'));
    stats.appendChild(stat(counts.personas || 0, 'личностей', 'fa-masks-theater'));
    bodyEl.appendChild(stats);

    var list = (d.sessions && d.sessions.items) || [];
    var total = (d.sessions && d.sessions.total) || 0;
    var chats = section('Чаты этого профиля', list.map(chatRow),
      'Чатов у этого профиля нет — или шлюз их не отдал.', 'fa-comments');
    if (total > list.length) {
      var more = el('button', 'ap-btn ap-wide');
      more.type = 'button';
      more.appendChild(ico('fa-arrow-down'));
      more.appendChild(el('span', null, 'Показать ещё'));
      more.addEventListener('click', function () {
        state.limit += PAGE;
        load();
      });
      chats.appendChild(more);
      chats.appendChild(el('div', 'ap-more',
        'показано ' + list.length + ' из ' + total));
    }
    bodyEl.appendChild(chats);

    if ((d.flags || {}).bots) {
      bodyEl.appendChild(section('Комнаты режима ботов',
        (d.rooms || []).map(roomRow), 'Комнат ещё нет.', 'fa-people-group'));
    }
    if ((d.flags || {}).vcs) {
      bodyEl.appendChild(section('Последние коммиты профиля',
        (d.commits || []).map(commitRow), 'Коммитов ещё нет.', 'fa-code-branch'));
    }
  }

  function open() {
    state.open = true;
    root.classList.add('open');
    load();
  }

  function hide() {
    state.open = false;
    root.classList.remove('open');
  }

  whenReady(build);
})();
