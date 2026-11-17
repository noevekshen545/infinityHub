/* ============================================================================
   ПАЛИТРА КОМАНД (Ctrl/Cmd+K)
   ----------------------------------------------------------------------------
   Общий файл, а не код на каждой странице: палитра должна работать отовсюду,
   иначе «найти заметку» пришлось бы начинать с захода в /notes.

   Что делает:
     * ищет заметки, теги и папки по /api/notes/search-all;
     * выполняет команды приложения (тема, переходы, создание заметки);
     * показывает неразрешённые ссылки — по ним заметка создаётся,
       как в Obsidian.

   Подключается одним <script> и не требует инициализации: если палитра уже
   открыта, повторный вызов ничего не делает.
   ============================================================================ */
(function (global) {
  'use strict';
  if (global.InfinityPalette) return;   // уже загружена (быстрые переходы между страницами)

  const LS = 'notes_palette_recent';
  let root = null, input = null, listEl = null, results = [], active = 0, token = 0;

  /* ─────────────────────────── команды ─────────────────────────── */

  const THEMES = ['light', 'dark', 'sepia', 'midnight', 'forest', 'ocean', 'rose', 'warm', 'paper'];
  const THEME_NAMES = {
    light: 'Светлая', dark: 'Тёмная', sepia: 'Сепия', midnight: 'Полночь',
    forest: 'Лес', ocean: 'Океан', rose: 'Роза', warm: 'Тёплая', paper: 'Бумага',
  };

  function commands() {
    const t = (global.InfinityTheme && global.InfinityTheme.get) ? global.InfinityTheme.get() : 'light';
    const next = THEMES[(THEMES.indexOf(t) + 1) % THEMES.length];
    const list = [
      { group: 'Команды', icon: '📁', title: 'Новая заметка',
        hint: 'inbox', run: () => go('/notes/edit?folder=inbox') },
      { group: 'Команды', icon: '📅', title: 'Ежедневная заметка',
        hint: 'за сегодня', run: dailyNote },
      { group: 'Команды', icon: '📃', title: 'Все заметки',
        run: () => go('/notes') },
      { group: 'Команды', icon: '🕸️', title: 'Граф связей',
        run: () => go('/notes/graph') },
      { group: 'Команды', icon: '🎨', title: 'Переключить тему',
        hint: THEME_NAMES[t] + ' \u2192 ' + THEME_NAMES[next],
        run: () => { if (global.InfinityTheme) global.InfinityTheme.set(next); close(); } },
      { group: 'Команды', icon: '\u2699\uFE0F', title: 'Настройки',
        run: () => go('/settings') },
      { group: 'Команды', icon: '🖥️', title: 'Рабочее пространство',
        run: () => go('/workspace?folder=inbox') },
      { group: 'Команды', icon: '🗂️', title: 'Файловый менеджер',
        run: () => go('/files') },
    ];
    return list;
  }

  function go(url) { close(); location.href = url; }

  async function dailyNote() {
    close();
    try {
      const r = await fetch('/api/notes/daily');
      const d = await r.json();
      if (d.id) location.href = '/notes/edit?id=' + encodeURIComponent(d.id);
      else location.href = '/notes/edit?folder=inbox';
    } catch (e) {
      location.href = '/notes/edit?folder=inbox';
    }
  }

  /* ─────────────────────────── разметка ─────────────────────────── */

  function build() {
    root = document.createElement('div');
    root.className = 'ip-root';
    root.style.cssText = [
      'position:fixed', 'inset:0', 'z-index:20000', 'display:none',
      'background:rgba(0,0,0,.42)', 'backdrop-filter:blur(3px)',
      'align-items:flex-start', 'justify-content:center', 'padding-top:12vh',
      'font-family:inherit',
    ].join(';');
    root.innerHTML =
      '<div class="ip-box" style="width:620px;max-width:calc(100vw - 32px);' +
      'background:var(--card,#fff);color:var(--text,#111827);' +
      'border:1px solid var(--border,rgba(0,0,0,.1));border-radius:14px;' +
      'box-shadow:0 24px 70px rgba(0,0,0,.4);overflow:hidden">' +
        '<input class="ip-input" type="text" autocomplete="off" spellcheck="false" ' +
        'placeholder="Поиск заметок, тегов, папок\u2026  Ctrl+K" ' +
        'style="width:100%;border:none;outline:none;background:transparent;' +
        'color:inherit;font-size:16px;padding:16px 18px;border-bottom:1px solid var(--border,rgba(0,0,0,.08))">' +
        '<div class="ip-list" style="max-height:52vh;overflow:auto"></div>' +
        '<div class="ip-foot" style="padding:8px 14px;font-size:11px;opacity:.6;' +
        'border-top:1px solid var(--border,rgba(0,0,0,.08));display:flex;gap:12px">' +
          '<span>\u2191\u2193 \u2014 \u043d\u0430\u0432\u0438\u0433\u0430\u0442\u044c</span>' +
          '<span>Enter \u2014 \u043e\u0442\u043a\u0440\u044b\u0442\u044c</span>' +
          '<span>Esc \u2014 \u0437\u0430\u043a\u0440\u044b\u0442\u044c</span>' +
        '</div>' +
      '</div>';
    document.body.appendChild(root);

    input = root.querySelector('.ip-input');
    listEl = root.querySelector('.ip-list');

    root.addEventListener('mousedown', e => { if (e.target === root) close(); });
    input.addEventListener('input', onInput);
    input.addEventListener('keydown', onKey);
  }

  /* Горячая клавиша вешается сразу, а не внутри build(). Иначе получается
     замкнутый круг: слушатель появлялся только после первого открытия
     палитры, а открыть её можно было только по этому слушателю — то есть
     Ctrl+K не работал никогда. */
  global.addEventListener('keydown', onGlobalKey, true);

  /* ─────────────────────────── поведение ─────────────────────────── */

  function open(prefill) {
    if (!root) build();
    root.style.display = 'flex';
    input.value = prefill || '';
    refresh();
    setTimeout(() => input.focus(), 10);
  }

  function close() {
    if (root) root.style.display = 'none';
  }

  function toggle() {
    if (root && root.style.display === 'flex') close();
    else open();
  }

  function onGlobalKey(e) {
    const k = (e.key || '').toLowerCase();
    if (k === 'k' && (e.ctrlKey || e.metaKey)) {
      /* В редакторе заметок есть своя палитра: к поиску заметок и тегов
         она добавляет действия с текстом (сохранить, шаблоны, экспорт,
         версии). Иначе на Ctrl+K открывались бы две палитры — общая
         перехватывает клавишу в capture-фазе и глушит страницу. */
      if (global.__notesEditorPalette) return;
      e.preventDefault();
      toggle();
    } else if (k === 'escape' && root && root.style.display === 'flex') {
      e.preventDefault();
      close();
    }
  }

  function onKey(e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const r = results[active];
      if (r) { close(); r.run(); }
    }
  }

  function move(d) {
    if (!results.length) return;
    active = (active + d + results.length) % results.length;
    paint();
  }

  function onInput() {
    const q = input.value.trim();
    if (!q) { paint(commands()); return; }
    const my = ++token;
    fetch('/api/notes/search-all?q=' + encodeURIComponent(q))
      .then(r => r.json())
      .then(d => {
        // Ответ мог прийти не для текущего запроса — пользователь уже
        // допечатал. Такой ответ игнорируем, иначе список прыгает.
        if (my !== token) return;
        buildResults(d, q);
      })
      .catch(() => paint([]));
  }

  function refresh() {
    const q = input.value.trim();
    if (!q) { paint(commands()); return; }
    onInput();
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function recent() {
    try { return JSON.parse(localStorage.getItem(LS) || '[]'); } catch (e) { return []; }
  }
  function pushRecent(id) {
    try {
      const r = recent().filter(x => x !== id);
      r.unshift(id);
      localStorage.setItem(LS, JSON.stringify(r.slice(0, 20)));
    } catch (e) { /* приватный режим */ }
  }

  function buildResults(d, q) {
    const out = [];
    const lq = q.toLowerCase();

    // Команды — только по совпадению названия, чтобы не мешали заметкам.
    for (const c of commands()) {
      if (c.title.toLowerCase().includes(lq)) out.push(c);
    }

    for (const n of (d.notes || [])) {
      out.push({
        group: n.pinned ? '\u0417\u0430\u043a\u0440\u0435\u043f\u043b\u0451\u043d\u043D\u044B\u0435' : '\u0417\u0430\u043c\u0435\u0442\u043A\u0438',
        icon: n.favorite ? '\u2B50' : '📝',
        title: n.title,
        hint: n.folder + (n.tags && n.tags.length ? '  #' + n.tags.slice(0, 3).join(' #') : ''),
        sub: n.snippet,
        run: () => { pushRecent(n.id); go('/notes/edit?id=' + encodeURIComponent(n.id)); },
      });
    }
    for (const t of (d.tags || [])) {
      out.push({
        group: '\u0422\u0435\u0433\u0438', icon: '#',
        title: '#' + t.tag, hint: t.count + ' ' + plural(t.count),
        run: () => { location.href = '/notes?tag=' + encodeURIComponent(t.tag); close(); },
      });
    }
    for (const f of (d.folders || [])) {
      out.push({
        group: '\u041F\u0430\u043F\u043A\u0438', icon: '📁',
        title: f, hint: '\u043F\u0430\u043F\u043A\u0430',
        run: () => { location.href = '/notes?folder=' + encodeURIComponent(f); close(); },
      });
    }
    // Последние — только когда строка пустая: иначе они вытесняли бы
    // результаты поиска на первой же букве.
    if (!q) for (const id of recent()) {
      out.push({ group: '\u041d\u0435\u0434\u0430\u0432\u043D\u043E', icon: '🔄', title: id, run: () => go('/notes/edit?id=' + encodeURIComponent(id)) });
    }
    paint(out);
  }

  function plural(n) {
    const m = n % 100;
    if (m >= 11 && m <= 19) return '\u0437\u0430\u043C\u0435\u0442\u043E\u043A';
    const n1 = n % 10;
    if (n1 === 1) return '\u0437\u0430\u043C\u0435\u0442\u043A\u0430';
    if (n1 >= 2 && n1 <= 4) return '\u0437\u0430\u043C\u0435\u0442\u043A\u0438';
    return '\u0437\u0430\u043C\u0435\u0442\u043E\u043A';
  }

  function paint(items) {
    results = items || [];
    active = 0;
    if (!results.length) {
      listEl.innerHTML =
        '<div style="padding:22px 18px;font-size:13px;opacity:.6;text-align:center">' +
        '\u041D\u0438\u0447\u0435\u0433\u043E \u043D\u0435 \u043D\u0430\u0439\u0434\u0435\u043D\u043E</div>';
      return;
    }
    let html = '';
    let group = null;
    results.forEach((r, i) => {
      if (r.group && r.group !== group) {
        group = r.group;
        html += '<div style="padding:9px 18px 5px;font-size:10px;letter-spacing:1px;' +
                'text-transform:uppercase;opacity:.55;font-weight:700">' + esc(group) + '</div>';
      }
      html += '<div class="ip-row" data-i="' + i + '" style="display:flex;gap:10px;' +
              'align-items:center;padding:9px 18px;cursor:pointer;' +
              (i === 0 ? 'background:var(--accent-soft,rgba(59,110,245,.09))' : '') + '">' +
        '<span style="width:20px;text-align:center;flex:0 0 auto">' + (r.icon || '\u2022') + '</span>' +
        '<span style="flex:1;min-width:0">' +
          '<span style="display:block;font-size:13.5px;font-weight:500;overflow:hidden;' +
            'text-overflow:ellipsis;white-space:nowrap">' + esc(r.title) + '</span>' +
          (r.sub ? '<span style="display:block;font-size:11.5px;opacity:.6;overflow:hidden;' +
            'text-overflow:ellipsis;white-space:nowrap">' + esc(r.sub) + '</span>' : '') +
        '</span>' +
        (r.hint ? '<span style="font-size:11px;opacity:.55;flex:0 0 auto">' + esc(r.hint) + '</span>' : '') +
      '</div>';
    });
    listEl.innerHTML = html;
    listEl.querySelectorAll('.ip-row').forEach(el => {
      el.addEventListener('click', () => {
        const r = results[Number(el.dataset.i)];
        if (r) { close(); r.run(); }
      });
      el.addEventListener('mouseenter', () => {
        active = Number(el.dataset.i);
        listEl.querySelectorAll('.ip-row').forEach(x => { x.style.background = ''; });
        el.style.background = 'var(--accent-soft,rgba(59,110,245,.09))';
      });
    });
  }

  global.InfinityPalette = { open, close, toggle };
})(window);