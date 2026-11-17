/* ============================================================================
   РЕДАКТОР ЗАМЕТОК — обвязка Obsidian
   ----------------------------------------------------------------------------
   Отдельный файл, потому что page_notes_edit.html — 9k строк, и править его
   целиком ради панели структуры неразумно.

   Что добавляет:
     * строка свойств (frontmatter) — статус, проект, автор, дедлайн;
     * панель структуры документа по заголовкам #;
     * обратные ссылки: кто ссылается на эту заметку и с какой формулировкой;
     * неразрешённые ссылки [[...]] с кнопкой создать заметку;
     * кнопки «закрепить», «в избранное», «в архив» в шапке.

   Чего здесь НЕТ намеренно: автодополнения [[ ]]. В странице оно уже есть —
   checkWikiLink / showWikiDropdown / insertWikiLink и навигация стрелками
   (page_notes_edit.html:5841+). Первая версия модуля добавила второй,
   конкурирующий список подсказок: два видима одновременно, курсоры
   независимы, Enter вставляет то один, то другой вариант. Убрано.

   Всё общается с сервером существующими эндпоинтами:
     GET  /api/notes/properties?id=    GET /api/notes/backlinks?id=
     POST /api/notes/patch             POST /api/notes/property
     POST /api/notes/save
   ============================================================================ */
(function (global) {
  'use strict';

  const state = {
    noteId: '',
    properties: {},
    headings: [],
    backlinks: [],
    unresolvedOut: [],
  };

  /* Часто встречающиеся свойства. Это подсказка, а не ограничение: любой
     ключ можно ввести руками, но перечисленное используется чаще всего. */
  const COMMON_PROPS = [
    { key: 'status', label: 'Статус', type: 'select',
      values: ['draft', 'active', 'done', 'idea', 'reading', 'archived'] },
    { key: 'project', label: 'Проект', type: 'text' },
    { key: 'author', label: 'Автор', type: 'text' },
    { key: 'due', label: 'Дедлайн', type: 'date' },
    { key: 'source', label: 'Источник', type: 'text' },
    { key: 'rating', label: 'Оценка', type: 'number' },
  ];

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }
  function toastMsg(msg, kind) {
    if (typeof global.toast === 'function') global.toast(msg, kind || 'info');
  }
  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(r => r.json());
  }

  /* ═══════════════════════ МОНТАЖ ═══════════════════════ */

  function mount() {
    const ta = $('content');
    if (!ta || $('nbRoot')) return;

    /* Вставка строго перед блоком редактора, в его же родителе.
       Первый вариант писал host.insertBefore(root, ta.parentNode), где host
       вычислялся как ta.closest('.editor-wrap'). Родителем textarea и есть
       .editor-wrap, поэтому вторым аргументом в insertBefore попадал сам
       host — DOMException «is not a child of this node», и вся обвязка
       редактора не монтировалась вовсе. */
    const anchor = ta.closest('.editor-wrap') || ta;
    const host = anchor.parentNode || document.body;

    /* Раскладка в две колонки: переносим содержимое .wrap (кроме строки
       свойств) внутрь .nb-main, а панель «Структура / Обратные ссылки»
       кладём вторым элементом .nb-workbench. Сетку объявляет
       notes_obsidian.css — здесь только разметка. */
    const root = document.createElement('div');
    root.id = 'nbRoot';
    root.className = 'pg-note-box';
    host.insertBefore(root, anchor);

    const bench = document.createElement('div');
    bench.className = 'nb-workbench';
    const main = document.createElement('div');
    main.className = 'nb-main';
    host.insertBefore(bench, anchor);

    /* Всё, что лежит в .wrap после вставки, кроме самой сетки и панели,
       переносим в левую колонку: заголовок, мета-строка, теги, редактор,
       зона перетаскивания, вложения и превью. */
    [...host.children].forEach(el => {
      if (el !== bench && el !== root) main.appendChild(el);
    });
    bench.appendChild(main);

    buildPropsBar(root);
    buildSidePanel(bench);
    bindPanelHotkey();
    if (state.noteId) loadAll();
    else { renderProps(); renderOutline(); }
  }

  /* Ctrl+Shift+O — показать/скрыть панель «Структура / Обратные ссылки».
     Раньше панель была видна всегда, а включать её было нечем: кнопка в
     углу только скрывала уже показанное. */
  function bindPanelHotkey() {
    if (document.getElementById('nbPanelBound')) return;
    const flag = document.createElement('span');
    flag.id = 'nbPanelBound';
    flag.style.display = 'none';
    document.body.appendChild(flag);
    document.addEventListener('keydown', function (e) {
      if (!(e.ctrlKey && e.shiftKey && (e.key === 'O' || e.key === 'o' || e.code === 'KeyO'))) return;
      const btn = $('nbSideToggleBtn');
      if (!btn) return;
      e.preventDefault();
      toggleSidePanel();
    });
  }

  /* ── Строка свойств ── */
  function buildPropsBar(root) {
    const bar = document.createElement('div');
    bar.className = 'nb-props';
    bar.id = 'nbProps';
    bar.innerHTML =
      '<span class="nb-props-label"><i class="fa-solid fa-sliders"></i> Свойства</span>' +
      '<div class="nb-props-items" id="nbPropsItems"></div>' +
      '<button class="nb-props-add" id="nbPropAdd" title="Добавить свойство">' +
        '<i class="fa-solid fa-plus"></i></button>' +
      /* Переключатель панели живёт в строке свойств, а не плавающей кнопкой
         в углу: та была position:fixed у правого края и налезала на
         липкую панель инструментов — ровно то, что мы уже чинили. */
      '<button class="nb-props-add" id="nbSideToggleBtn" title="Структура и обратные ссылки (Ctrl+Shift+O)">' +
        '<i class="fa-solid fa-sitemap"></i></button>';
    root.appendChild(bar);
    bar.querySelector('#nbPropAdd').addEventListener('click', e => {
      e.stopPropagation();
      showAddPropMenu(e.currentTarget);
    });
    var sideBtn = bar.querySelector('#nbSideToggleBtn');
    if (sideBtn) sideBtn.addEventListener('click', e => { e.stopPropagation(); toggleSidePanel(); });
  }

  /* Показать/скрыть боковую колонку. Панель свёрнута по умолчанию. */
  function toggleSidePanel() {
    var side = $('nbSide');
    if (!side) return;
    var show = side.hasAttribute('hidden');
    if (show) side.removeAttribute('hidden'); else side.setAttribute('hidden', '');
    var bench = document.querySelector('.nb-workbench');
    if (bench) bench.classList.toggle('has-side', show);
    var b = $('nbSideToggleBtn');
    if (b) b.classList.toggle('on', show);
  }

  function renderProps() {
    const box = $('nbPropsItems');
    if (!box) return;
    const props = state.properties || {};
    const keys = Object.keys(props);
    if (!keys.length) {
      box.innerHTML = '<span class="nb-props-empty">не заданы — нажмите +</span>';
      return;
    }
    box.innerHTML = keys.map(k => {
      const meta = COMMON_PROPS.find(p => p.key === k);
      const v = props[k];
      const val = Array.isArray(v) ? v.join(', ') : (v === null || v === undefined ? '' : String(v));
      const type = meta ? meta.type : (typeof v === 'boolean' ? 'select' : 'text');
      const input = type === 'select'
        ? `<select data-k="${esc(k)}" class="nb-prop-in">${
            (meta && meta.values ? meta.values : []).map(o =>
              `<option${o === val ? ' selected' : ''}>${esc(o)}</option>`).join('')
          }${meta ? '' : `<option${val ? ' selected' : ''}>${esc(val)}</option>`}</select>`
        : type === 'date'
          ? `<input type="date" class="nb-prop-in" data-k="${esc(k)}" value="${esc(val)}">`
          : type === 'number'
            ? `<input type="number" class="nb-prop-in" data-k="${esc(k)}" value="${esc(val)}">`
            : `<input type="text" class="nb-prop-in" data-k="${esc(k)}" value="${esc(val)}" placeholder="${esc(meta ? meta.label : k)}">`;
      return `<span class="nb-prop" data-k="${esc(k)}">
          <span class="nb-prop-k" title="${esc(meta ? meta.label : k)}">${esc(meta ? meta.label : k)}</span>
          ${input}
          <button class="nb-prop-x" data-del="${esc(k)}" title="Убрать свойство">
            <i class="fa-solid fa-xmark"></i></button>
        </span>`;
    }).join('');

    box.querySelectorAll('.nb-prop-in').forEach(el => {
      const commit = () => setProp(el.dataset.k, el.value);
      el.addEventListener('change', commit);
      el.addEventListener('blur', commit);
    });
    box.querySelectorAll('[data-del]').forEach(el => {
      el.addEventListener('click', () => delProp(el.dataset.del));
    });
  }

  function showAddPropMenu(anchor) {
    document.querySelectorAll('.nb-addmenu').forEach(n => n.remove());
    const used = Object.keys(state.properties || {});
    const free = COMMON_PROPS.filter(p => used.indexOf(p.key) === -1);
    const menu = document.createElement('div');
    menu.className = 'nb-addmenu';
    const r = anchor.getBoundingClientRect();
    menu.style.cssText = 'position:fixed;z-index:12000;background:var(--card,#fff);' +
      'border:1px solid var(--border,rgba(0,0,0,.1));border-radius:10px;' +
      'box-shadow:0 12px 34px rgba(0,0,0,.22);padding:6px;min-width:180px;' +
      `top:${r.bottom + 6}px;left:${Math.min(r.left, window.innerWidth - 200)}px`;
    menu.innerHTML =
      free.map(p => `<div class="nb-addmenu-item" data-k="${esc(p.key)}">${esc(p.label)}</div>`).join('') +
      '<div class="nb-addmenu-item nb-addmenu-custom" data-custom="1">' +
        '<i class="fa-solid fa-pen"></i> Своё свойство…</div>';
    document.body.appendChild(menu);

    const close = () => menu.remove();
    setTimeout(() => document.addEventListener('click', close, { once: true }), 0);
    menu.addEventListener('click', async e => {
      const it = e.target.closest('.nb-addmenu-item');
      if (!it) return;
      if (it.dataset.custom) {
        const k = await InfinityDialogs.prompt({label:'Название свойства:'});
        if (k && k.trim()) setProp(k.trim(), '');
      } else {
        setProp(it.dataset.k, '');
      }
      close();
    });
  }

  async function setProp(key, value) {
    const d = await post('/api/notes/property', { id: state.noteId, key, value });
    if (d.error) return toastMsg(d.error, 'error');
    state.properties = d.properties || state.properties;
    state.headings = d.headings || state.headings;
    renderProps();
    renderOutline();
    /* Свойства живут в тексте заметки как YAML-шапка. Поле редактора про
       них не знает, и ближайшее автосохранение записало бы старый текст
       обратно, стерев свойство. Поэтому перечитываем содержимое. */
    await refreshContentFromServer();
    toastMsg('Свойство сохранено', 'success');
  }

  async function delProp(key) {
    /* Удаление идёт тем же эндпоинтом, что и запись, со значением null:
       раньше здесь был частичный save_note только с frontmatter, из-за чего
       YAML-шапка в тексте оставалась нетронутой и свойство возвращалось
       при следующем чтении. */
    const d = await post('/api/notes/property', { id: state.noteId, key, value: null });
    if (d.error) return toastMsg(d.error, 'error');
    state.properties = d.properties || {};
    state.headings = d.headings || state.headings;
    renderProps();
    renderOutline();
    await refreshContentFromServer();
  }

  /* ═══════════════════════ ПАНЕЛЬ СТРУКТУРЫ И ОБРАТНЫХ ССЫЛОК ═══════════════════════ */

  function buildSidePanel(host) {
    if ($('nbSide')) return;
    const side = document.createElement('div');
    side.className = 'nb-side';
    side.id = 'nbSide';
    /* Раскладка и размеры живут в notes_obsidian.css. Раньше здесь стоял
       position:fixed с top:72px/right:16px и z-index:60 — панель лежала
       поверх липкой панели инструментов (z-index:100) и поверх текста
       заметки, перекрывая кнопки. Теперь это обычный блок в потоке. */
    side.innerHTML =
      '<div class="nb-card"><div class="nb-card-h">Структура</div>' +
        '<div id="nbOutline" class="nb-outline"></div></div>' +
      '<div class="nb-card"><div class="nb-card-h">Обратные ссылки <span id="nbBackN" class="nb-n"></span></div>' +
        '<div id="nbBack" class="nb-back"></div></div>' +
      '<div class="nb-card" id="nbUnresCard" style="display:none">' +
        '<div class="nb-card-h" style="color:#ef4444">Не созданные ссылки</div>' +
        '<div id="nbUnres" class="nb-back"></div></div>';
    (host || document.body).appendChild(side);
    toggleBtn(side);
  }

  /* Кнопка переключения теперь живёт в строке свойств (buildPropsBar) и
     называется nbSideToggleBtn. Раньше здесь создавалась плавающая кнопка
     с position:fixed у правого края — она перекрывала панель
     инструментов, поэтому отдельного элемента больше не нужно. */
  function toggleBtn(side) {
    var bench = document.querySelector('.nb-workbench');
    if (bench) bench.classList.remove('has-side');
    side.setAttribute('hidden', '');
    var b = $('nbSideToggleBtn');
    if (b) b.classList.remove('on');
  }

  function renderOutline() {
    const box = $('nbOutline');
    if (!box) return;
    const hs = state.headings || [];
    if (!hs.length) {
      box.innerHTML = '<div class="nb-empty">Заголовков нет</div>';
      return;
    }
    box.innerHTML = hs.map(h =>
      `<div class="nb-ol" data-l="${h.level}" style="padding-left:${(h.level - 1) * 12}px">
         <a href="#" data-slug="${esc(h.slug)}">${esc(h.text)}</a>
       </div>`).join('');
    box.querySelectorAll('a[data-slug]').forEach(a => {
      a.addEventListener('click', e => { e.preventDefault(); scrollToHeading(a.dataset.slug); });
    });
  }

  /* В textarea нет якорей, поэтому позиция заголовка ищется по тексту. */
  function findHeadingOffset(text, slug) {
    const re = /^(#{1,6})\s+(.+?)\s*#*$/gm;
    let m;
    while ((m = re.exec(text))) {
      const s = slugOf(m[2]);
      if (s === slug) return m.index;
    }
    return -1;
  }

  function slugOf(raw) {
    return raw.trim().toLowerCase()
      .replace(/[^\w\- ]+/g, '').trim().replace(/\s+/g, '-') || 'section';
  }

  function scrollToHeading(slug) {
    const ta = $('content');
    if (!ta) return;
    const idx = findHeadingOffset(ta.value || '', slug);
    if (idx < 0) return;
    ta.focus();
    ta.setSelectionRange(idx, idx);
    const line = (ta.value.slice(0, idx).match(/\n/g) || []).length;
    const lh = parseFloat(getComputedStyle(ta).lineHeight) || 20;
    ta.scrollTop = Math.max(0, (line - 4) * lh);
  }

  function renderBacklinks() {
    const box = $('nbBack'), n = $('nbBackN');
    if (!box) return;
    const list = state.backlinks || [];
    if (n) n.textContent = list.length ? list.length : '';
    if (!list.length) {
      box.innerHTML = '<div class="nb-empty">На эту заметку никто не ссылается</div>';
      return;
    }
    box.innerHTML = list.map(b =>
      `<a class="nb-back-item" href="/notes/edit?id=${encodeURIComponent(b.from_id)}">
         <span class="nb-back-t">${esc(b.title)}</span>
         <span class="nb-back-c">${esc(b.context || '')}</span>
       </a>`).join('');
  }

  function renderUnresolved() {
    const card = $('nbUnresCard'), box = $('nbUnres');
    if (!card || !box) return;
    const list = state.unresolvedOut || [];
    card.style.display = list.length ? '' : 'none';
    if (!list.length) return;
    box.innerHTML = list.map(t =>
      `<div class="nb-back-item nb-back-item--unres">
         <span class="nb-back-t">${esc(t)}</span>
         <button class="nb-btn-mini" data-create="${esc(t)}">
           <i class="fa-solid fa-plus"></i> создать</button>
       </div>`).join('');
    box.querySelectorAll('[data-create]').forEach(b => {
      b.addEventListener('click', async e => {
        e.preventDefault();
        const r = await post('/api/notes/save', {
          title: b.dataset.create, content: '# ' + b.dataset.create + '\n\n', folder: 'inbox',
        });
        if (r.error) return toastMsg(r.error, 'error');
        toastMsg('Заметка «' + b.dataset.create + '» создана', 'success');
        loadAll();
      });
    });
  }

  /* ═══════════════════════ ЗАГРУЗКА ═══════════════════════ */

  async function loadAll() {
    if (!state.noteId) return;
    try {
      const props = await fetch('/api/notes/properties?id=' + encodeURIComponent(state.noteId))
        .then(r => r.json());
      if (props && !props.error) {
        state.properties = props.properties || {};
        state.headings = props.headings || [];
      }
    } catch (e) { /* панель не критична */ }
    try {
      const bl = await fetch('/api/notes/backlinks?id=' + encodeURIComponent(state.noteId))
        .then(r => r.json());
      if (bl && !bl.error) {
        state.backlinks = bl.incoming || [];
        state.unresolvedOut = bl.outgoing_unresolved || [];
      }
    } catch (e) { /* панель не критична */ }
    renderProps();
    renderOutline();
    renderBacklinks();
    renderUnresolved();
  }

  /* ═══════════════════════ ПАНЕЛЬ ЗАГОЛОВКА ═══════════════════════ */

  function addHeaderButtons() {
    const host = $('title');
    if (!host || !host.parentNode || $('nbHeadBtns')) return;
    const box = document.createElement('div');
    box.id = 'nbHeadBtns';
    box.className = 'nb-head-btns';
    box.innerHTML =
      '<button class="nb-hbtn" data-act="pin"   title="Закрепить"><i class="fa-solid fa-thumbtack"></i></button>' +
      '<button class="nb-hbtn" data-act="fav"   title="В избранное"><i class="fa-regular fa-star"></i></button>' +
      '<button class="nb-hbtn" data-act="arch"  title="В архив"><i class="fa-solid fa-box-archive"></i></button>';
    host.parentNode.appendChild(box);
    box.querySelectorAll('[data-act]').forEach(b => {
      b.addEventListener('click', () => headAction(b.dataset.act, b));
    });
  }

  async function headAction(act, btn) {
    if (!state.noteId) return toastMsg('Сначала сохраните заметку', 'info');
    const on = !btn.classList.contains('on');
    const patch = act === 'pin' ? { pinned: on }
                : act === 'fav' ? { favorite: on }
                : { archived: on };
    const d = await post('/api/notes/patch', Object.assign({ id: state.noteId }, patch));
    if (d.error) return toastMsg(d.error, 'error');
    btn.classList.toggle('on', on);
    if (act === 'fav') {
      const ic = btn.querySelector('i');
      if (ic) ic.className = on ? 'fa-solid fa-star' : 'fa-regular fa-star';
    }
    toastMsg(on ? 'Готово' : 'Отменено', 'success');
  }

  /* ═══════════════════════ СИНХРОНИЗАЦИЯ С ТЕКСТОМ ═══════════════════════ */

  async function refreshContentFromServer() {
    const ta = $('content');
    if (!ta || !state.noteId) return;
    const start = ta.selectionStart, end = ta.selectionEnd;
    try {
      const d = await fetch('/api/notes/get?id=' + encodeURIComponent(state.noteId))
        .then(r => r.json());
      if (!d || d.error || typeof d.content !== 'string') return;
      /* YAML-шапка свойств в редакторе скрыта: страница держит её отдельно
         (noteFrontmatterBlock) и собирает полный текст при сохранении.
         Поэтому обновляем поле через setEditorText, иначе шапка вернулась бы
         в текст заметки обычными строками. */
      const hasHelper = typeof global.setEditorText === 'function';
      const next = hasHelper ? global.splitFrontmatter(d.content).body : d.content;
      if (next === ta.value) return;
      ta.value = next;
      const at = Math.min(start, ta.value.length);
      ta.setSelectionRange(at, Math.min(Math.max(end, at), ta.value.length));
      if (hasHelper) global.setEditorText(d.content);
      if (typeof global.updateStats === 'function') global.updateStats();
    } catch (e) { /* не критично */ }
  }

  /* Заголовки и неразрешённые ссылки меняются вместе с текстом, а
     /api/notes/outline читает файл с диска и несохранённый текст не увидит,
     поэтому считаем их локально. */
  function watchContent() {
    const ta = $('content');
    if (!ta || ta._nbWatched) return;
    ta._nbWatched = true;
    let t = null;
    ta.addEventListener('input', () => {
      if (t) clearTimeout(t);
      t = setTimeout(() => {
        t = null;
        const v = ta.value || '';
        const re = /^(#{1,6})\s+(.+?)\s*#*$/gm;
        const used = {}, hs = [];
        let m;
        while ((m = re.exec(v))) {
          let s = slugOf(m[2]);
          const n = used[s] || 0;
          used[s] = n + 1;
          if (n) s += '-' + n;
          hs.push({ level: m[1].length, text: m[2].trim(), slug: s });
        }
        state.headings = hs;
        renderOutline();
        scheduleUnresolved(v);
      }, 400);
    });
  }

  /* Список известных заголовков берём у страницы: она уже загружает
     wikiNotes для своего автодополнения, второй запрос не нужен. */
  function knownTitles() {
    const list = Array.isArray(global.wikiNotes) ? global.wikiNotes : [];
    return list.map(n => ({ title: String(n.title || ''), id: String(n.id || '') }));
  }

  let _unresTimer = null;
  function scheduleUnresolved(v) {
    if (_unresTimer) clearTimeout(_unresTimer);
    _unresTimer = setTimeout(() => {
      const known = knownTitles();
      if (!known.length) return;          // список ещё не загружен
      const titles = new Set(known.map(k => k.title.toLowerCase()));
      const ids = new Set(known.map(k => k.id));
      const found = [];
      const re = /\[\[([^\[\]\n]+?)(?:\|[^\[\]\n]*)?\]\]/g;
      let m;
      while ((m = re.exec(v))) {
        const t = m[1].split('#')[0].trim();
        if (!t) continue;
        if (titles.has(t.toLowerCase()) || ids.has(t)) continue;
        if (found.indexOf(t) === -1) found.push(t);
      }
      state.unresolvedOut = found;
      renderUnresolved();
    }, 700);
  }

  /* ═══════════════════════ СТАРТ ═══════════════════════ */

  function init() {
    const id = new URLSearchParams(location.search).get('id');
    if (id) state.noteId = id;
    mount();
    watchContent();
    addHeaderButtons();
  }

  /* Страница догружает заметку через loadNote() — перечитываем и панель,
     иначе она останется с данными пустой заметки. */
  const origLoad = global.loadNote;
  if (typeof origLoad === 'function' && !origLoad._nbWrapped) {
    const wrapped = async function () {
      const r = await origLoad.apply(this, arguments);
      const id = new URLSearchParams(location.search).get('id');
      if (id) state.noteId = id;
      if (state.noteId) loadAll();
      return r;
    };
    wrapped._nbWrapped = true;
    global.loadNote = wrapped;
  }

  global.NoteBox = { init, loadAll, state, COMMON_PROPS };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(init, 150));
  } else {
    setTimeout(init, 150);
  }
})(window);