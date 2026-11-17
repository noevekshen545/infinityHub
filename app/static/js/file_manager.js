class FileManagerApp {
  constructor() {
    this.state = {
      panePaths: { 1: '', 2: '' },
      paneItems: { 1: [], 2: [] },
      selectedItems: { 1: new Set(), 2: new Set() },
      viewModes: { 1: 'grid', 2: 'grid' },
      activePane: 1,
      layout: 'single',
      history: { 1: { stack: [], index: -1 }, 2: { stack: [], index: -1 } },
      clipboard: { items: [], action: null },
      tabs: { 1: [], 2: [] },
      activeTab: { 1: 0, 2: 0 },
      iconSize: 80,
      sortBy: 'name',
      sortAsc: true,
      searchQuery: '',
      treeExpanded: new Set(),
      favorites: JSON.parse(localStorage.getItem('fm_favorites') || '[]'),
      // Источник темы теперь один на всё приложение: infinity_theme.
      // Старый 'fm_theme' читаем только как миграцию, если нового ключа нет.
      theme: localStorage.getItem('infinity_theme') || localStorage.getItem('fm_theme') || 'light',
      showHidden: false,
      // Корзина: последний ответ /trash/list. Заполняется при старте,
      // нужен пункту «Корзина» в дереве (там показывается число файлов).
      trash: { items: [], total: 0, trash_dir: '' },
      trashDirty: true,
      fileFilter: 'all',
      dualSync: false,
      checkboxMode: localStorage.getItem('fm_checkbox') === 'true',
      recentPaths: JSON.parse(localStorage.getItem('fm_recent') || '[]'),
      fullscreen: false,
      lastSelected: null,
      dragData: null,
      operations: [],
      conflictStrategy: 'ask',
      platformInfo: null,
      undoStack: [],
      redoStack: [],
      profileRoot: null,
      // «Открыть с помощью…»: расширение → {target, method, label}.
      associations: {},
      // Установленные программы Windows (null — ещё не загружались).
      winApps: null,
      // Опция шифрования файлов (по умолчанию выключена на сервере).
      encryptionEnabled: false,
      encryptionAvailable: true,
      // Доступы, выданные текущему профилю (/share/incoming).
      incomingGrants: [],
    };
    this.init();
  }

  async init() {
    this.embedded = new URLSearchParams(location.search).get('embed') === '1';
    if (this.embedded) {
      document.body.classList.add('fm-embedded');
      document.querySelectorAll('[data-embed-hide]').forEach(el => el.style.display = 'none');
      const tree = document.getElementById('fmTree');
      if (tree) tree.classList.add('collapsed');
    }
    // Тема — общая с приложением. Параметр ?theme= рабочий стол больше не
    // передаёт: тема читается из infinity_theme, и параметр только запутывал
    // (сжимал 9 тем до трёх, а где-то не передавался вовсе).
    this.bindGlobalTheme();
    this.setTheme(this.state.theme, false);
    // Счётчик на пункте «Корзина» должен быть верным сразу при открытии.
    await this.loadTrash();
    this.loadDisks();
    await this.loadProfileRoot();
    this.bindEvents();
    this.loadPlatform();
    this.loadAssociations();
    this.loadEncryptionStatus();
    const _p = new URLSearchParams(location.search).get('path');
    const _file = new URLSearchParams(location.search).get('file');
    let startPath = _p || this.state.profileRoot || '';
    let previewPath = '';
    // Переход из Search All: ?path=<папка>&file=<имя> открывает ещё и
    // предпросмотр файла, иначе результат вёл бы только в каталог. Папка
    // файла может быть вне корня профиля (пользователь сам её проиндексировал):
    // список тогда показываем в корне, а файл — в предпросмотре.
    if (_file && startPath) {
      const sep = startPath.indexOf('\\') >= 0 ? '\\' : '/';
      previewPath = startPath.replace(/[\\/]+$/, '') + sep + _file;
      if (!this.isWithinRoot(startPath)) startPath = this.state.profileRoot || '';
    }
    this.state.tabs[1].push({ path: startPath, name: 'Файлы' });
    this.state.activeTab[1] = 0;
    this.renderTabs(1);
    this.navigate(1, startPath);
    this.addRecent(this.state.panePaths[1]);
    if (previewPath) this.showPreview(1, previewPath);
    // Доступы, которыми со мной поделились: если есть непринятые —
    // показываем окно сразу при входе в модуль, как и просили.
    // ?incoming=1 — переход из уведомления на рабочем столе.
    const forceIncoming = new URLSearchParams(location.search).get('incoming') === '1';
    this.checkIncomingShares(true, forceIncoming);
  }

  // ─── PROFILE ROOT ───────────────────────────────────
  async loadProfileRoot() {
    try {
      const data = await this.api('/profile-root');
      if (data && data.root) {
        this.state.profileRoot = data.root;
      }
    } catch {}
  }

  isWithinRoot(path) {
    if (!this.state.profileRoot || !path) return true;
    if (path.startsWith('Результаты:') || path.startsWith('Поиск')) return true;
    const norm = path.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
    const root = this.state.profileRoot.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
    if (!norm || !root) return true;
    const ok = norm === root || norm.startsWith(root + '/');
    if (!ok) console.warn('[FM] isWithinRoot BLOCKED:', { path, norm, root });
    return ok;
  }

  // ─── API helpers ────────────────────────────────────
  async api(endpoint, options = {}) {
    const res = await fetch(`/filemanager${endpoint}`, options);
    if (!res.ok) {
      let msg;
      try { msg = (await res.json()).detail || res.statusText; } catch { msg = await res.text(); }
      throw new Error(msg.slice(0, 300));
    }
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('json')) return res.json();
    return res;
  }

  async apiJson(method, endpoint, body) {
    return this.api(endpoint, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  // API индекса файлов живёт вне префикса /filemanager.
  async fileIndexApi(method, endpoint, body) {
    const res = await fetch(`/api/file-index${endpoint}`, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = {};
    try { data = await res.json(); } catch {}
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  async mediaIndexApi(method, endpoint, body) {
    const res = await fetch(`/api/media-index${endpoint}`, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = {};
    try { data = await res.json(); } catch {}
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  // ─── NAVIGATION ─────────────────────────────────────
  async navigate(pane, path) {
    if (path === undefined || path === null) path = '';
    if (!this.isWithinRoot(path)) {
      this.showNotification('Нельзя выйти за пределы корневой папки профиля', 'warn');
      return;
    }
    try {
      const data = await this.api(`/list?path=${encodeURIComponent(path)}`);
      this.state.panePaths[pane] = data.path;
      let items = data.items;
      // Служебные файлы корзины (.trashinfo) пользователю не показываем,
      // даже если «Показать скрытые» включено и он зашёл в .trash вручную.
      items = items.filter(i => !i.name.endsWith('.trashinfo'));
      if (!this.state.showHidden) {
        items = items.filter(i => !i.name.startsWith('.'));
      }
      if (this.state.fileFilter !== 'all') {
        items = items.filter(i => this.matchFilter(i));
      }
      this.state.paneItems[pane] = items;
      this.state.selectedItems[pane].clear();
      this.pushHistory(pane, data.path);
      this.render(pane);
      this.renderClipboardIndicators(pane);
      this.renderBreadcrumbs(pane);
      this.renderStatusBar();
      this.updateTabTitle(pane);
      this.addRecent(data.path);
      if (this.state.dualSync && pane === 1) {
        this.navigate(2, data.path);
      }
    } catch (err) {
      this.showNotification(err.message, 'error');
    }
  }

  matchFilter(item) {
    if (item.type === 'directory') return true;
    const ext = (item.extension || '').toLowerCase();
    switch (this.state.fileFilter) {
      case 'images': return ['.jpg','.jpeg','.png','.gif','.webp','.svg','.ico','.bmp'].includes(ext);
      case 'documents': return ['.pdf','.doc','.docx','.xls','.xlsx','.ppt','.pptx','.txt','.md','.csv','.odt','.ods'].includes(ext);
      case 'audio': return ['.mp3','.wav','.ogg','.flac','.m4a','.aac','.wma'].includes(ext);
      case 'video': return ['.mp4','.webm','.avi','.mkv','.mov','.m4v','.wmv'].includes(ext);
      case 'archives': return ['.zip','.tar','.gz','.bz2','.7z','.rar'].includes(ext);
      default: return true;
    }
  }

  pushHistory(pane, path) {
    const h = this.state.history[pane];
    if (h.index < h.stack.length - 1) h.stack = h.stack.slice(0, h.index + 1);
    h.stack.push(path);
    h.index = h.stack.length - 1;
  }

  goBack(pane) {
    const h = this.state.history[pane];
    if (h.index > 0) { h.index--; this.navigate(pane, h.stack[h.index]); }
  }

  goForward(pane) {
    const h = this.state.history[pane];
    if (h.index < h.stack.length - 1) { h.index++; this.navigate(pane, h.stack[h.index]); }
  }

  goUp(pane) {
    const p = this.state.panePaths[pane];
    const parent = this.getParentPath(p);
    if (parent !== p && this.isWithinRoot(parent)) {
      this.navigate(pane, parent);
    }
  }

  getParentPath(p) {
    if (!p) return '';
    const norm = p.replace(/\\/g, '/').replace(/\/+$/, '');
    const idx = norm.lastIndexOf('/');
    if (idx <= 0) return norm.slice(0, idx + 1);
    return norm.slice(0, idx);
  }

  // ─── PLATFORM ───────────────────────────────────────
  async loadPlatform() {
    try {
      const info = await this.api('/platform');
      this.state.platformInfo = info;
      this.renderTree();
    } catch {}
  }

  // ─── RECENT ─────────────────────────────────────────
  addRecent(path) {
    if (!path) return;
    this.state.recentPaths = this.state.recentPaths.filter(p => p !== path);
    this.state.recentPaths.unshift(path);
    if (this.state.recentPaths.length > 20) this.state.recentPaths = this.state.recentPaths.slice(0, 20);
    localStorage.setItem('fm_recent', JSON.stringify(this.state.recentPaths));
  }

  // ─── RENDER ─────────────────────────────────────────
  render(pane) {
    const el = document.querySelector(`.fm-file-list[data-pane="${pane}"]`);
    if (!el) return;
    const items = this.state.paneItems[pane];
    // Корзина — не папка: у элементов есть «исходное расположение» и
    // дата удаления, а внизу нужны кнопки восстановления. Обычные
    // renderGrid/renderList про эти поля ничего не знают.
    if (this.isTrashPath(this.state.panePaths[pane])) {
      this.renderTrash(el, items || [], pane);
      this.renderInfo(pane);
      this.bindRubberBand(el, pane);
      return;
    }
    const mode = this.state.viewModes[pane];
    if (!items || items.length === 0) {
      el.innerHTML = `<div class="fm-empty"><div class="fm-empty-icon"><i class="fas fa-folder-open"></i></div><div class="fm-empty-text">Папка пуста</div></div>`;
      return;
    }
    const sorted = this.sortItems(items);
    if (mode === 'grid') this.renderGrid(el, sorted, pane);
    else if (mode === 'list') this.renderList(el, sorted, pane);
    else if (mode === 'details') this.renderDetails(el, sorted, pane);
    this.bindRubberBand(el, pane);
    this.renderInfo(pane);
  }

  renderTrash(el, items, pane) {
    const sel = this.state.selectedItems[pane];
    const fmtSize = (b) => {
      if (!b) return '0 Б';
      const u = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
      let i = 0, v = b;
      while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
      return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
    };
    const fmtDate = (s) => {
      if (!s) return '';
      const d = new Date(s);
      return isNaN(d) ? s : d.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
    };

    const toolbar = `
      <div class="fm-trash-bar">
        <button class="fm-btn fm-btn-primary" data-act="restore" ${sel.size ? '' : 'disabled'}>
          <i class="fas fa-undo"></i> Восстановить${sel.size ? ` (${sel.size})` : ''}
        </button>
        <button class="fm-btn fm-btn-danger" data-act="del" ${sel.size ? '' : 'disabled'}>
          <i class="fas fa-trash"></i> Удалить навсегда
        </button>
        <span class="fm-trash-sep"></span>
        <button class="fm-btn" data-act="empty" ${items.length ? '' : 'disabled'}>
          <i class="fas fa-broom"></i> Очистить корзину
        </button>
        <span class="fm-trash-hint"><i class="fas fa-circle-info"></i> Двойной клик — восстановить</span>
      </div>`;

    if (!items.length) {
      el.innerHTML = toolbar + `<div class="fm-empty"><div class="fm-empty-icon"><i class="fas fa-trash-restore"></i></div><div class="fm-empty-text">Корзина пуста</div></div>`;
      this.bindTrashBar(el, pane);
      return;
    }

    const rows = items.map((it) => {
      const isSel = sel.has(it.path);
      const orig = it.original_path
        ? `<span class="fm-trash-orig" title="${this.escAttr(it.original_path)}">${this.esc(it.original_path)}</span>`
        : `<span class="fm-trash-orig fm-trash-orig-unknown">исходное место неизвестно</span>`;
      return `<div class="fm-trash-row${isSel ? ' selected' : ''}" data-path="${this.escAttr(it.path)}">
        <span class="fm-trash-icon"><i class="fas fa-${it.type === 'directory' ? 'folder' : 'file'}"></i></span>
        <span class="fm-trash-name">${this.esc(it.name)}</span>
        ${orig}
        <span class="fm-trash-size">${it.type === 'directory' ? 'папка' : fmtSize(it.size)}</span>
        <span class="fm-trash-date">${this.esc(fmtDate(it.deleted_at))}</span>
        <button class="fm-trash-restore-btn" data-restore="${this.escAttr(it.id || it.name)}" title="Восстановить">
          <i class="fas fa-undo"></i>
        </button>
      </div>`;
    }).join('');

    el.innerHTML = toolbar +
      `<div class="fm-trash-head"><span></span><span>Имя</span><span>Исходное расположение</span><span>Размер</span><span>Удалён</span><span></span></div>` +
      rows;

    this.bindTrashBar(el, pane);

    el.querySelectorAll('.fm-trash-row').forEach((row) => {
      row.addEventListener('click', (e) => {
        if (e.target.closest('[data-restore]')) return;
        const p = row.dataset.path;
        if (!this.state.ctrlKey && !this.state.shiftKey) sel.clear();
        if (sel.has(p)) sel.delete(p); else sel.add(p);
        this.renderTrash(el, items, pane);
        this.renderInfo(pane);
      });
      row.addEventListener('dblclick', () => {
        const id = row.querySelector('[data-restore]')?.dataset.restore;
        if (id) this.trashRestore([id]);
      });
      row.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const it = (this.state.paneItems[pane] || []).find(i => i.path === row.dataset.path);
        this.showTrashContextMenu(e, pane, it);
      });
    });

    el.querySelectorAll('[data-restore]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.trashRestore([btn.dataset.restore]);
      });
    });
  }

  bindTrashBar(el, pane) {
    const sel = this.state.selectedItems[pane];
    const ids = () => {
      const items = this.state.paneItems[pane] || [];
      return items.filter(i => sel.has(i.path)).map(i => i.id || i.name);
    };
    el.querySelector('[data-act="restore"]')?.addEventListener('click', () => this.trashRestore(ids()));
    el.querySelector('[data-act="del"]')?.addEventListener('click', () => this.trashDeleteForever(ids()));
    el.querySelector('[data-act="empty"]')?.addEventListener('click', () => this.trashEmpty());
  }

  sortItems(items) {
    const sb = this.state.sortBy, sa = this.state.sortAsc;
    return [...items].sort((a, b) => {
      if (a.type !== b.type) return a.type === 'directory' ? -1 : 1;
      let va, vb;
      if (sb === 'name') { va = (a.name || '').toLowerCase(); vb = (b.name || '').toLowerCase(); }
      else if (sb === 'size') { va = a.size || 0; vb = b.size || 0; }
      else if (sb === 'modified') { va = a.modified || ''; vb = b.modified || ''; }
      else if (sb === 'type') { va = a.extension || ''; vb = b.extension || ''; }
      else { va = a.name || ''; vb = b.name || ''; }
      if (typeof va === 'string') return sa ? va.localeCompare(vb) : vb.localeCompare(va);
      return sa ? va - vb : vb - va;
    });
  }

  renderGrid(el, items, pane) {
    const sel = this.state.selectedItems[pane];
    let html = '<div class="fm-grid">';
    for (const item of items) {
      const isSel = sel.has(item.path) ? ' selected' : '';
      const thumb = item.media_type === 'image'
        ? `<img src="/filemanager/thumbnail?path=${encodeURIComponent(item.path)}" class="fm-grid-thumb" alt="">`
        : `<div class="fm-grid-icon">${this.getIcon(item)}</div>`;
      html += `<div class="fm-grid-item${isSel}" data-path="${this.escAttr(item.path)}" data-type="${item.type}">
        ${this.state.checkboxMode ? `<div class="fm-checkbox${isSel ? ' checked' : ''}"><i class="fas fa-check"></i></div>` : ''}
        ${thumb}
        <div class="fm-grid-name">${this.esc(item.name)}</div>
        <div class="fm-grid-details">${item.type === 'file' ? this.formatSize(item.size) : ''}</div>
      </div>`;
    }
    html += '</div>';
    el.innerHTML = html;
    el.querySelectorAll('.fm-grid-item').forEach(el2 => {
      const cb = el2.querySelector('.fm-checkbox');
      if (cb) cb.addEventListener('click', e => { e.stopPropagation(); this.toggleSelect(pane, el2.dataset.path); });
      el2.addEventListener('click', e => this.onItemClick(e, pane));
      el2.addEventListener('dblclick', e => this.onItemDblClick(e, pane));
      el2.addEventListener('contextmenu', e => { this.showContextMenu(e, pane); e.preventDefault(); });
      el2.addEventListener('dragstart', e => this.onDragStart(e, pane));
      el2.addEventListener('dragover', e => e.preventDefault());
      el2.addEventListener('drop', e => this.onDrop(e, pane));
      el2.draggable = true;
    });
  }

  renderList(el, items, pane) {
    const sel = this.state.selectedItems[pane];
    const sb = this.state.sortBy, sa = this.state.sortAsc;
    const arrow = (f) => sb === f ? `<i class="fas fa-sort-${sa ? 'up' : 'down'}"></i>` : '';
    let html = `<div class="fm-list"><div class="fm-list-header">
      <div></div>
      <div onclick="FM.setSort('name')">Имя ${arrow('name')}</div>
      <div class="fm-col-size" onclick="FM.setSort('size')">Размер ${arrow('size')}</div>
      <div class="fm-col-type" onclick="FM.setSort('type')">Тип ${arrow('type')}</div>
      <div class="fm-col-date" onclick="FM.setSort('modified')">Дата ${arrow('modified')}</div>
    </div>`;
    for (const item of items) {
      const isSel = sel.has(item.path) ? ' selected' : '';
      const thumb = item.media_type === 'image'
        ? `<img src="/filemanager/thumbnail?path=${encodeURIComponent(item.path)}" class="fm-list-thumb" alt="">`
        : `<div class="fm-list-icon">${this.getIcon(item)}</div>`;
      const cb = this.state.checkboxMode ? `<div class="fm-checkbox${isSel ? ' checked' : ''}">${isSel ? '<i class="fas fa-check"></i>' : ''}</div>` : '';
      html += `<div class="fm-list-row${isSel}${this.state.checkboxMode ? ' fm-checkbox-mode' : ''}" data-path="${this.escAttr(item.path)}" data-type="${item.type}">
        ${cb || `<div class="fm-list-icon">${thumb}</div>`}
        ${cb ? `<div class="fm-list-icon">${thumb}</div>` : ''}
        <div class="fm-list-name" title="${this.esc(item.name)}">${this.esc(item.name)}</div>
        <div class="fm-list-size">${item.type === 'file' ? this.formatSize(item.size) : ''}</div>
        <div class="fm-list-type">${item.extension || 'папка'}</div>
        <div class="fm-list-modified">${item.modified ? new Date(item.modified).toLocaleString() : ''}</div>
      </div>`;
    }
    html += '</div>';
    el.innerHTML = html;
    el.querySelectorAll('.fm-list-row').forEach(el2 => {
      const cb = el2.querySelector('.fm-checkbox');
      if (cb) cb.addEventListener('click', e => { e.stopPropagation(); this.toggleSelect(pane, el2.dataset.path); });
      el2.addEventListener('click', e => this.onItemClick(e, pane));
      el2.addEventListener('dblclick', e => this.onItemDblClick(e, pane));
      el2.addEventListener('contextmenu', e => { this.showContextMenu(e, pane); e.preventDefault(); });
      el2.addEventListener('dragstart', e => this.onDragStart(e, pane));
      el2.addEventListener('dragover', e => e.preventDefault());
      el2.addEventListener('drop', e => this.onDrop(e, pane));
      el2.draggable = true;
    });
    this.initColResize(el);
  }

  renderDetails(el, items, pane) {
    const sel = this.state.selectedItems[pane];
    let html = '<div class="fm-list"><div class="fm-list-header">';
    html += '<div></div><div>Имя</div><div class="fm-col-size">Размер</div><div class="fm-col-type">Тип</div><div class="fm-col-date">Дата</div></div>';
    for (const item of items) {
      const isSel = sel.has(item.path) ? ' selected' : '';
      const thumb = item.media_type === 'image'
        ? `<img src="/filemanager/thumbnail?path=${encodeURIComponent(item.path)}" class="fm-list-thumb" alt="">`
        : `<div class="fm-list-icon">${this.getIcon(item)}</div>`;
      const cb = this.state.checkboxMode ? `<div class="fm-checkbox${isSel ? ' checked' : ''}">${isSel ? '<i class="fas fa-check"></i>' : ''}</div>` : '';
      html += `<div class="fm-list-row${isSel}${this.state.checkboxMode ? ' fm-checkbox-mode' : ''}" data-path="${this.escAttr(item.path)}" data-type="${item.type}">
        ${cb || `<div class="fm-list-icon">${thumb}</div>`}
        ${cb ? `<div class="fm-list-icon">${thumb}</div>` : ''}
        <div><strong>${this.esc(item.name)}</strong><br><span class="fm-details-path">${this.esc(item.path)}</span></div>
        <div class="fm-list-size">${item.type === 'file' ? this.formatSize(item.size) : ''}</div>
        <div class="fm-list-type">${item.extension || 'папка'}</div>
        <div class="fm-list-modified">${item.modified ? new Date(item.modified).toLocaleString() : ''}</div>
      </div>`;
    }
    html += '</div>';
    el.innerHTML = html;
    el.querySelectorAll('.fm-list-row').forEach(el2 => {
      const cb = el2.querySelector('.fm-checkbox');
      if (cb) cb.addEventListener('click', e => { e.stopPropagation(); this.toggleSelect(pane, el2.dataset.path); });
      el2.addEventListener('click', e => this.onItemClick(e, pane));
      el2.addEventListener('dblclick', e => this.onItemDblClick(e, pane));
      el2.addEventListener('contextmenu', e => { this.showContextMenu(e, pane); e.preventDefault(); });
      el2.addEventListener('dragstart', e => this.onDragStart(e, pane));
      el2.addEventListener('dragover', e => e.preventDefault());
      el2.addEventListener('drop', e => this.onDrop(e, pane));
      el2.draggable = true;
    });
    this.initColResize(el);
  }

  initColResize(el) {
    // Use mouse drag on column borders for resize
    const header = el.querySelector('.fm-list-header');
    if (!header) return;
    let isResizing = false, currentCol = null, startX, startW;
    const onMouseMove = (e) => {
      if (!isResizing || !currentCol) return;
      const dx = e.clientX - startX;
      const newW = Math.max(40, startW + dx);
      document.querySelectorAll(`.fm-list-header .${currentCol}, .fm-list-row .${currentCol}`).forEach(c => c.style.width = newW + 'px');
    };
    const onMouseUp = () => { isResizing = false; currentCol = null; document.removeEventListener('mousemove', onMouseMove); document.removeEventListener('mouseup', onMouseUp); };
    header.querySelectorAll('.fm-col-size, .fm-col-type, .fm-col-date').forEach(col => {
      const grip = document.createElement('div');
      grip.className = 'fm-col-grip';
      grip.addEventListener('mousedown', (e) => { isResizing = true; currentCol = col.className.split(' ')[0]; startX = e.clientX; startW = col.offsetWidth; e.preventDefault(); document.addEventListener('mousemove', onMouseMove); document.addEventListener('mouseup', onMouseUp); });
      col.appendChild(grip);
    });
  }

  renderInfo(pane) {
    const el = document.querySelector(`.fm-pane-header[data-pane="${pane}"] .fm-pane-info`);
    if (el) {
      const items = this.state.paneItems[pane];
      const sel = this.state.selectedItems[pane].size;
      const total = items ? items.length : 0;
      el.textContent = sel ? `${sel} / ${total}` : `${total} эл.`;
    }
  }

  renderBreadcrumbs(pane) {
    const el = document.querySelector(`.fm-breadcrumbs[data-pane="${pane}"]`);
    if (!el) return;
    const path = this.state.panePaths[pane] || '';
    const sep = path.includes('\\') ? '\\' : '/';
    const parts = path.split(sep).filter(Boolean);
    let html = '';
    html += `<span class="fm-breadcrumb-action" onclick="FM.showPathInput(${pane})" title="Редактировать путь (Alt+D)"><i class="fas fa-pencil-alt"></i></span>`;
    if (parts.length === 0) {
      el.innerHTML = html;
      el.querySelectorAll('.fm-breadcrumb-item').forEach(el2 => {
        el2.addEventListener('click', () => this.navigate(pane, el2.dataset.path));
      });
      return;
    }
    let cum = '';
    if (path.includes(':')) {
      const m = path.match(/^([A-Za-z]:)/);
      if (m) {
        cum = m[1] + sep;
        html += `<span class="fm-breadcrumb-item" data-path="${cum}">${cum}</span>`;
      }
    } else if (path.startsWith('/')) {
      cum = '/';
      html += `<span class="fm-breadcrumb-item" data-path="/">/</span>`;
    }
    for (let i = 0; i < parts.length; i++) {
      if (cum && cum.endsWith(sep)) cum = cum + parts[i];
      else if (cum) cum = cum + sep + parts[i];
      else cum = parts[i];
      const active = i === parts.length - 1 ? ' active' : '';
      const displayPath = cum.endsWith(sep) ? cum : cum;
      html += `<span class="fm-breadcrumb-sep">${sep === '\\' ? '\\' : '/'}</span><span class="fm-breadcrumb-item${active}" data-path="${this.escAttr(displayPath)}">${this.esc(parts[i])}</span>`;
    }
    el.innerHTML = html;
    el.querySelectorAll('.fm-breadcrumb-item').forEach(el2 => {
      el2.addEventListener('click', () => this.navigate(pane, el2.dataset.path));
    });
  }

  showPathInput(pane) {
    const el = document.querySelector(`.fm-breadcrumbs[data-pane="${pane}"]`);
    if (!el) return;
    const current = this.state.panePaths[pane] || '';
    el.innerHTML = `<input class="fm-path-input" id="pathInput${pane}" value="${this.esc(current)}" autofocus>`;
    const inp = document.getElementById(`pathInput${pane}`);
    inp.select();
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { this.navigate(pane, inp.value); }
      else if (e.key === 'Escape') { this.renderBreadcrumbs(pane); }
    });
    inp.addEventListener('blur', () => this.renderBreadcrumbs(pane));
  }

  renderStatusBar() {
    const leftEl = document.querySelector('.fm-statusbar-left');
    const rightEl = document.querySelector('.fm-statusbar-right-prepend');
    if (!leftEl) return;
    const p1 = this.state.panePaths[1];
    const p2 = this.state.panePaths[2];
    const n1 = (this.state.paneItems[1] || []).length;
    const n2 = (this.state.paneItems[2] || []).length;
    const s1 = this.state.selectedItems[1].size;
    const s2 = this.state.selectedItems[2].size;
    let parts = [];
    parts.push(`${p1 || '~'} — ${n1} эл.` + (s1 ? ` (${s1} выд.)` : ''));
    if (this.state.layout === 'dual') parts.push(`${p2 || '~'} — ${n2} эл.` + (s2 ? ` (${s2} выд.)` : ''));
    leftEl.innerHTML = parts.join(' | ');
    if (rightEl) {
      const selItems = this.state.selectedItems[this.state.activePane];
      let totalSize = 0;
      selItems.forEach(sp => {
        const item = (this.state.paneItems[this.state.activePane] || []).find(i => i.path === sp);
        if (item) totalSize += item.size || 0;
      });
      rightEl.textContent = totalSize > 0 ? `Выделено: ${this.formatSize(totalSize)}` : '';
    }
  }

// ─── ITEM EVENTS ────────────────────────────────────
  // Выделение рамкой мышью. Раньше файловое окно умело выделять только
  // кликами и Shift+кликом: зажать мышь на пустом месте и обвести
  // несколько файлов было нечем.
  bindRubberBand(el, pane) {
    // Флаг на самом контейнере: render() перерисовывает содержимое, но не
    // сам элемент, поэтому слушатель должен навешиваться ровно один раз.
    if (el.dataset.rbBound === '1') return;
    el.dataset.rbBound = '1';
    el.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      // Зажимать рамку можно только на пустом месте файлового окна.
      if (e.target.closest('.fm-grid-item, .fm-list-row, .fm-trash-row, .fm-list-header, button, input, select, textarea, label, .fm-checkbox')) return;
      this.startRubberBand(e, el, pane);
    });
  }

  startRubberBand(e, el, pane) {
    const startX = e.clientX, startY = e.clientY;
    const sel = this.state.selectedItems[pane];
    // Ctrl/Shift добавляют к текущему выделению, иначе рамка его заменяет.
    const base = new Set((e.ctrlKey || e.metaKey || e.shiftKey) ? sel : []);
    let moved = false;

    const band = document.createElement('div');
    band.className = 'fm-rubber-band';
    document.body.appendChild(band);
    document.body.classList.add('fm-rubber-active');

    const nodes = () => el.querySelectorAll('.fm-grid-item, .fm-list-row, .fm-trash-row');

    const onMove = (ev) => {
      const dx = ev.clientX - startX, dy = ev.clientY - startY;
      // Порог в несколько пикселей: иначе обычный щелчок по пустому месту
      // мигал бы рамкой.
      if (!moved) {
        if (Math.abs(dx) < 4 && Math.abs(dy) < 4) return;
        moved = true;
        band.style.display = 'block';
      }
      const x = Math.min(startX, ev.clientX);
      const y = Math.min(startY, ev.clientY);
      const w = Math.abs(dx), h = Math.abs(dy);
      band.style.left = x + 'px';
      band.style.top = y + 'px';
      band.style.width = w + 'px';
      band.style.height = h + 'px';

      const r = { l: x, t: y, r: x + w, b: y + h };
      sel.clear();
      base.forEach((p) => sel.add(p));
      nodes().forEach((node) => {
        const b = node.getBoundingClientRect();
        // Выделяем всё, что хоть как-то пересеклось с рамкой, — так же,
        // как в проводнике.
        const hit = b.right >= r.l && b.left <= r.r && b.bottom >= r.t && b.top <= r.b;
        if (hit) sel.add(node.dataset.path);
        node.classList.toggle('selected', hit || base.has(node.dataset.path));
      });
      this.renderStatusBar();
      this.renderInfo(pane);
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      band.remove();
      document.body.classList.remove('fm-rubber-active');
      if (!moved) {
        // Это был щелчок по пустому месту — снимаем выделение.
        sel.clear();
        this.state.lastSelected = null;
      } else {
        const last = Array.from(sel)[sel.size - 1];
        if (last) this.state.lastSelected = last;
      }
      this.render(pane);
      this.renderStatusBar();
      this.renderInfo(pane);
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    e.preventDefault();
  }

onItemClick(e, pane) {
    const item = this.getItemFromEvent(e, pane);
    if (!item) return;
    if (e.ctrlKey || e.metaKey) {
      this.toggleSelect(pane, item.path);
    } else if (e.shiftKey && this.state.lastSelected) {
      this.rangeSelect(pane, this.state.lastSelected, item.path);
    } else {
      this.state.selectedItems[pane].clear();
      this.state.selectedItems[pane].add(item.path);
      this.state.lastSelected = item.path;
      this.render(pane);
      this.renderStatusBar();
      if (item.type === 'file') this.showPreview(pane, item.path);
    }
  }

  rangeSelect(pane, fromPath, toPath) {
    const items = this.state.paneItems[pane] || [];
    // Диапазон считаем по тому же порядку, в котором элементы стоят на
    // экране. Раньше здесь использовался исходный порядок из /list, а
    // на экране его раскладывает sortItems: после смены сортировки
    // Shift+клик выделял не тот участок, и выделение выглядело случайным.
    const order = this.sortItems(items);
    const fromIdx = order.findIndex(i => i.path === fromPath);
    const toIdx = order.findIndex(i => i.path === toPath);
    if (fromIdx === -1 || toIdx === -1) return;
    const sel = this.state.selectedItems[pane];
    const start = Math.min(fromIdx, toIdx);
    const end = Math.max(fromIdx, toIdx);
    for (let i = start; i <= end; i++) {
      sel.add(order[i].path);
    }
    this.state.lastSelected = toPath;
    this.render(pane);
    this.renderStatusBar();
  }

  onItemDblClick(e, pane) {
    const item = this.getItemFromEvent(e, pane);
    if (!item) return;
    if (item.type === 'directory') {
      this.navigate(pane, item.path);
    } else {
      this.openFile(pane, item);
    }
  }

  getItemFromEvent(e, pane) {
    const el = e.currentTarget;
    const path = el.dataset.path;
    return (this.state.paneItems[pane] || []).find(i => i.path === path);
  }

  toggleSelect(pane, path) {
    const s = this.state.selectedItems[pane];
    if (s.has(path)) s.delete(path); else s.add(path);
    this.state.lastSelected = path;
    this.render(pane);
    this.renderStatusBar();
  }

  selectAll(pane) {
    const s = this.state.selectedItems[pane];
    const items = this.state.paneItems[pane] || [];
    if (s.size === items.length) s.clear();
    else items.forEach(i => s.add(i.path));
    this.render(pane);
  }

  invertSelection(pane) {
    const s = this.state.selectedItems[pane];
    const items = this.state.paneItems[pane] || [];
    items.forEach(i => { if (s.has(i.path)) s.delete(i.path); else s.add(i.path); });
    this.render(pane);
  }

  // ─── DRAG & DROP ────────────────────────────────────
  onDragStart(e, pane) {
    const path = e.currentTarget.dataset.path;
    this.state.dragData = { pane, paths: [path], action: 'move' };
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', path);
  }

  onDrop(e, pane) {
    e.preventDefault();
    const drag = this.state.dragData;
    const targetPath = e.currentTarget.dataset.path;
    const targetItem = (this.state.paneItems[pane] || []).find(i => i.path === targetPath);
    let destDir;
    if (targetItem && targetItem.type === 'directory') {
      destDir = targetPath;
    } else {
      destDir = this.state.panePaths[pane];
    }
    if (!drag) {
      const files = e.dataTransfer.files;
      if (files.length > 0) {
        const archExts = ['.zip', '.tar', '.gz', '.bz2', '.7z', '.rar'];
        const firstFile = files[0];
        const fExt = (firstFile.name || '').substring((firstFile.name || '').lastIndexOf('.')).toLowerCase();
        if (files.length === 1 && archExts.includes(fExt)) {
          this.uploadFiles(files, destDir, pane).then(() => {
            const uploadedPath = destDir.replace(/[/\\]$/, '') + (destDir.includes('\\') ? '\\' : '/') + firstFile.name;
            if (this.confirm('Архив загружен. Извлечь содержимое?')) {
              this.extractArchive(uploadedPath, destDir);
            }
          });
        } else {
          this.uploadFiles(files, destDir, pane);
        }
      }
      return;
    }
    if (drag.paths && drag.paths.length > 0) {
      for (const src of drag.paths) {
        const name = src.split(/[/\\]/).pop();
        const dest = destDir.replace(/[/\\]$/, '') + '/' + name;
        if (src === dest) continue;
        this.pushUndo({ type: 'move', path: src, destPath: dest, name: name });
        this.apiJson('POST', '/move', { source: src, destination: dest })
          .then(() => this.navigate(pane, destDir))
          .catch(err => this.showNotification(err.message, 'error'));
      }
    }
    this.state.dragData = null;
  }

  // ─── FILE ICON ──────────────────────────────────────
  getIcon(item) {
    if (item.type === 'directory') return '<i class="fas fa-folder"></i>';
    const ext = (item.extension || '').toLowerCase();
    const map = {
      '.jpg':'fas fa-file-image','.jpeg':'fas fa-file-image','.png':'fas fa-file-image','.gif':'fas fa-file-image',
      '.webp':'fas fa-file-image','.svg':'fas fa-file-image','.ico':'fas fa-file-image','.bmp':'fas fa-file-image',
      '.mp4':'fas fa-file-video','.webm':'fas fa-file-video','.avi':'fas fa-file-video','.mkv':'fas fa-file-video',
      '.mov':'fas fa-file-video','.m4v':'fas fa-file-video',
      '.mp3':'fas fa-file-audio','.wav':'fas fa-file-audio','.ogg':'fas fa-file-audio',
      '.flac':'fas fa-file-audio','.m4a':'fas fa-file-audio','.aac':'fas fa-file-audio',
      '.pdf':'fas fa-file-pdf','.epub':'fas fa-book','.fb2':'fas fa-book','.djvu':'fas fa-book',
      '.zip':'fas fa-file-archive','.tar':'fas fa-file-archive','.gz':'fas fa-file-archive',
      '.bz2':'fas fa-file-archive','.7z':'fas fa-file-archive','.rar':'fas fa-file-archive',
      '.py':'fas fa-code','.js':'fas fa-code','.html':'fas fa-code','.css':'fas fa-code',
      '.json':'fas fa-code','.xml':'fas fa-code','.ts':'fas fa-code','.tsx':'fas fa-code',
      '.jsx':'fas fa-code','.go':'fas fa-code','.rs':'fas fa-code','.cpp':'fas fa-code',
      '.c':'fas fa-code','.h':'fas fa-code','.java':'fas fa-code','.php':'fas fa-code',
      '.rb':'fas fa-code','.swift':'fas fa-code','.kt':'fas fa-code',
      '.md':'fas fa-file-alt','.txt':'fas fa-file-alt','.csv':'fas fa-file-csv',
      '.xlsx':'fas fa-file-excel','.xls':'fas fa-file-excel','.docx':'fas fa-file-word',
      '.doc':'fas fa-file-word','.pptx':'fas fa-file-powerpoint','.ppt':'fas fa-file-powerpoint',
      '.exe':'fas fa-cog','.msi':'fas fa-cog','.dll':'fas fa-cog',
      '.ps1':'fas fa-terminal','.bat':'fas fa-terminal','.sh':'fas fa-terminal',
      '.cfg':'fas fa-cog','.conf':'fas fa-cog','.ini':'fas fa-cog','.toml':'fas fa-cog',
      '.yml':'fas fa-cog','.yaml':'fas fa-cog','.env':'fas fa-cog','.gitignore':'fas fa-cog',
      '.log':'fas fa-file-alt','.lock':'fas fa-lock',
      '.iso':'fas fa-compact-disc','.img':'fas fa-compact-disc',
      // .inf — исполняемый файл Infinity. Маппинг обязателен: дальше по коду
      // идёт проверка is_text, а .inf им помечается как текст (это JSON), и без
      // строки иконка была бы «файл-текст».
      '.inf':'fas fa-box',
      // Зашифрованная копия: замок, а не «просто файл».
      '.infenc':'fas fa-lock',
    };
    // Сначала расширение, и только потом is_text. Раньше было наоборот:
    // маленький .rar/.zip сервер помечал как is_text, и проверка
    // возвращала иконку текстового файла, не доходя до маппинга —
    // поэтому у архивов «не отображалась» иконка архива.
    if (map[ext]) return '<i class="' + map[ext] + '"></i>';
    if (item.is_text) return '<i class="fas fa-file-alt"></i>';
    return '<i class="fas fa-file"></i>';
  }

  // ─── FILE OPERATIONS ────────────────────────────────
  async newFolder(pane) {
    const name = await this.prompt('Имя новой папки:');
    if (!name) return;
    const path = this.state.panePaths[pane];
    await this.apiJson('POST', '/mkdir', { path, name });
    this.navigate(pane, path);
  }

  async newFile(pane, template = '') {
    const name = await this.prompt('Имя нового файла:', template);
    if (!name) return;
    const path = this.state.panePaths[pane];
    const fullPath = path.replace(/\\/g, '/').replace(/\/$/, '') + '/' + name;
    let content = '';
    if (name.endsWith('.html')) content = '<!DOCTYPE html><html><head><title>New</title></head><body>\n\n</body></html>';
    else if (name.endsWith('.py')) content = '#!/usr/bin/env python3\n\ndef main():\n    pass\n\nif __name__ == "__main__":\n    main()\n';
    else if (name.endsWith('.md')) content = `# ${name.replace('.md', '')}\n\n`;
    await this.apiJson('POST', '/write', { path: fullPath, content });
    this.navigate(pane, path);
  }

  async createArchive(pane) {
    const sel = Array.from(this.state.selectedItems[pane]);
    const sources = sel.length > 0 ? sel : [this.state.panePaths[pane]];
    const archiveName = await this.prompt('Имя архива:', 'archive.zip');
    if (!archiveName) return;
    // Формат — по расширению: .tar.gz сжимается gzip, .gz/.bz2 — один файл.
    const low = archiveName.toLowerCase();
    let fmt = 'zip';
    if (low.endsWith('.tar.gz') || low.endsWith('.tgz')) fmt = 'tar.gz';
    else if (low.endsWith('.tar.bz2')) fmt = 'tar.bz2';
    else if (low.endsWith('.tar')) fmt = 'tar';
    else if (low.endsWith('.gz')) fmt = 'gz';
    else if (low.endsWith('.bz2')) fmt = 'bz2';
    const dest = this.state.panePaths[pane];
    const pBar = this.showProgress('Создание архива...', 0);
    try {
      await this.apiJson('POST', '/compress', { sources, archive_name: archiveName, format: fmt, destination_dir: dest });
      this.showNotification('Архив создан', 'success');
      this.navigate(pane, dest);
    } catch (err) {
      this.showNotification('Ошибка создания архива: ' + err.message, 'error');
    } finally {
      this.hideProgress(pBar);
    }
  }

  // permanent=true — безвозвратно (Shift+Del), иначе в корзину.
  async deleteSelected(pane, permanent = false) {
    const paths = Array.from(this.state.selectedItems[pane]);
    if (paths.length === 0) return;
    // Из корзины удалять можно только по-настоящему: «в корзину» из
    // корзины некуда, поэтому restore/delete там вызываются напрямую.
    const inTrash = this.isTrashPath(this.state.panePaths[pane]);
    if (inTrash) permanent = true;

    if (permanent) {
      if (!await this.confirm(
        `<i class="fas fa-exclamation-triangle"></i> Удалить <b>${paths.length}</b> элемент(ов) <b>навсегда</b>?<br>` +
        `<span style="color:var(--red)">Восстановить их будет невозможно.</span><br><br>` +
        `<small>Обычное удаление (Delete) отправляет файлы в корзину.</small>`)) return;
    } else {
      if (!await this.confirm(
        `<i class="fas fa-trash"></i> Переместить ${paths.length} элемент(ов) в <b>Корзину</b>?<br>` +
        `<small>Их можно будет восстановить.</small>`)) return;
    }

    const pBar = this.showProgress(permanent ? 'Удаление…' : 'Перемещение в корзину…', 0);
    try {
      // Прежнее «откатить удаление» держало в памяти текст каждого файла.
      // Для папок и больших файлов это молча не работало, а теперь и не
      // нужно: удаление обратимо через корзину.
      await this.apiJson('POST', '/delete', { paths, recursive: true, permanent });
      if (!permanent) this.state.trashDirty = true;
    } catch (err) {
      this.showNotification(err.message, 'error');
    } finally {
      this.hideProgress(pBar);
    }
    this.state.selectedItems[pane].clear();
    if (inTrash) this.openTrash(); else this.navigate(pane, this.state.panePaths[pane]);
  }

  isTrashPath(p) {
    const t = this.state.trash || {};
    if (!t.trash_dir || !p) return false;
    // Разделители приводим к одному виду: иначе 'C:/root/.trash' и
    // 'C:\root\.trash' считались разными путями, и корзина переставала
    // быть корзиной при первом же переходе из панели в дерево.
    const n = (x) => String(x || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
    const tp = n(t.trash_dir);
    const np = n(p);
    return np === tp || np.startsWith(tp + '/');
  }

  async loadTrash() {
    try {
      this.state.trash = await this.apiJson('GET', '/trash/list');
    } catch (e) {
      this.state.trash = { items: [], total: 0, trash_dir: '' };
    }
    return this.state.trash;
  }

  // Экран корзины: это не папка, поэтому панель собирается вручную —
  // показываем исходное расположение, дату удаления и кнопки.
  async openTrash() {
    const pane = this.state.activePane;
    const t = await this.loadTrash();
    this.state.panePaths[pane] = t.trash_dir || '';
    this.state.paneItems[pane] = t.items || [];
    this.state.selectedItems[pane].clear();
    this.render(pane);
    this.renderBreadcrumbs(pane);
    this.renderStatusBar();
    this.updateTabTitle(pane);
    this.renderTree();
  }

  async trashRestore(names) {
    if (!names || !names.length) return;
    const pBar = this.showProgress('Восстановление…', 0);
    try {
      const res = await this.apiJson('POST', '/trash/restore', { names });
      const errs = res.errors || [];
      if (errs.length) this.showNotification(`Часть файлов не восстановлена: ${errs[0].error}`, 'warn');
      else this.showNotification(`Восстановлено: ${res.restored.length}`, 'success');
    } catch (e) {
      this.showNotification(e.message, 'error');
    } finally { this.hideProgress(pBar); }
    this.state.trashDirty = true;
    this.openTrash();
  }

  async trashDeleteForever(names) {
    if (!names || !names.length) return;
    if (!await this.confirm(`<i class="fas fa-exclamation-triangle"></i> Удалить <b>${names.length}</b> элемент(ов) из корзины <b>навсегда</b>?`)) return;
    const pBar = this.showProgress('Удаление…', 0);
    try {
      const res = await this.apiJson('POST', '/trash/delete', { names });
      const errs = res.errors || [];
      if (errs.length) this.showNotification(errs[0].error, 'error');
      else this.showNotification('Удалено из корзины', 'success');
    } catch (e) {
      this.showNotification(e.message, 'error');
    } finally { this.hideProgress(pBar); }
    this.state.trashDirty = true;
    this.openTrash();
  }

  async trashEmpty() {
    const t = this.state.trash || {};
    if (!t.total) { this.showNotification('Корзина пуста', 'info'); return; }
    if (!await this.confirm(`<i class="fas fa-trash"></i> Очистить корзину? <b>${t.total}</b> элемент(ов) будет удалено навсегда.`)) return;
    const pBar = this.showProgress('Очистка корзины…', 0);
    try {
      await this.apiJson('POST', '/trash/empty', {});
      this.showNotification('Корзина очищена', 'success');
    } catch (e) {
      this.showNotification(e.message, 'error');
    } finally { this.hideProgress(pBar); }
    this.state.trashDirty = true;
    this.openTrash();
  }

  async renameSelected(pane) {
    const sel = Array.from(this.state.selectedItems[pane]);
    if (sel.length !== 1) { this.showNotification('Выберите один файл', 'warn'); return; }
    const oldPath = sel[0];
    const oldName = oldPath.split(/[/\\]/).pop();
    const newName = await this.prompt('Новое имя:', oldName);
    if (!newName || newName === oldName) return;
    const oldExt = oldName.includes('.') ? oldName.split('.').pop() : '';
    const newExt = newName.includes('.') ? newName.split('.').pop() : '';
    if (oldExt && newExt && oldExt !== newExt) {
      if (!await this.confirm(`<i class="fas fa-exclamation-triangle"></i> Вы уверены, что хотите изменить расширение файла?<br><code>${this.esc(oldName)}</code> → <code>${this.esc(newName)}</code>`)) return;
    }
    const parentDir = this.state.panePaths[pane];
    const newPath = parentDir.replace(/[/\\]$/, '') + (parentDir.includes('\\') ? '\\' : '/') + newName;
    this.pushUndo({ type: 'rename', path: oldPath, oldName, newName, newPath });
    await this.apiJson('POST', '/rename', { source: oldPath, new_name: newName });
    this.navigate(pane, this.state.panePaths[pane]);
  }

  async copySelected(pane) {
    const paths = Array.from(this.state.selectedItems[pane]);
    if (paths.length === 0) return;
    await this.apiJson('POST', '/clipboard/set', { items: paths, action: 'copy' });
    this.state.clipboard = { items: paths, action: 'copy' };
    this.showNotification(`Скопировано: ${paths.length} эл.`, 'info');
    this.renderClipboardIndicators(pane);
  }

  async cutSelected(pane) {
    const paths = Array.from(this.state.selectedItems[pane]);
    if (paths.length === 0) return;
    await this.apiJson('POST', '/clipboard/set', { items: paths, action: 'cut' });
    this.state.clipboard = { items: paths, action: 'cut' };
    this.showNotification(`Вырезано: ${paths.length} эл.`, 'warn');
    this.renderClipboardIndicators(pane);
  }

  renderClipboardIndicators(pane) {
    const el = document.querySelector(`.fm-file-list[data-pane="${pane}"]`);
    if (!el) return;
    el.querySelectorAll('.in-clipboard, .clipboard-cut').forEach(e => {
      e.classList.remove('in-clipboard', 'clipboard-cut');
    });
    if (!this.state.clipboard.items) return;
    this.state.clipboard.items.forEach(ip => {
      const name = ip.split(/[/\\]/).pop();
      el.querySelectorAll(`[data-path$="${name}"]`).forEach(e => {
        e.classList.add('in-clipboard');
        if (this.state.clipboard.action === 'cut') e.classList.add('clipboard-cut');
      });
    });
  }

  async pasteTo(pane) {
    if (!this.state.clipboard.items || this.state.clipboard.items.length === 0) {
      this.showNotification('Буфер пуст', 'warn'); return;
    }
    const dest = this.state.panePaths[pane];
    const action = this.state.clipboard.action || 'copy';
    const files = [...this.state.clipboard.items];
    await this.showCopyModal(action, files, dest);
    this.navigate(pane, dest);
  }

  showCopyModal(action, files, dest) {
    return new Promise((resolve) => {
      const title = action === 'cut' ? 'Перемещение' : 'Копирование';
      const isMove = action === 'cut';
      let completed = 0, errors = 0, cancelled = false;

      const overlay = document.createElement('div');
      overlay.className = 'fm-copy-modal';
      overlay.innerHTML = `<div class="fm-copy-dialog">
        <div class="fm-copy-header">
          <h3><i class="fas fa-${isMove ? 'cut' : 'copy'}"></i> ${title}...</h3>
          <button class="close" data-action="close"><i class="fas fa-times"></i></button>
        </div>
        <div class="fm-copy-body" id="copyBody"></div>
        <div class="fm-copy-progress-wrap"><div class="fm-copy-progress"><div class="fm-copy-progress-fill" id="copyFill" style="width:0%"></div></div></div>
        <div class="fm-copy-footer">
          <span id="copyStatus">0 / ${files.length} файлов</span>
          <button class="cancel" data-action="cancel">Отмена</button>
        </div>
      </div>`;
      document.body.appendChild(overlay);

      const body = overlay.querySelector('#copyBody');
      const fill = overlay.querySelector('#copyFill');
      const status = overlay.querySelector('#copyStatus');

      files.forEach((f, i) => {
        const name = f.split(/[/\\]/).pop();
        const row = document.createElement('div');
        row.className = 'fm-copy-file pending';
        row.dataset.idx = i;
        row.innerHTML = `<span class="icon"><i class="fas fa-file"></i></span><span class="name">${this.esc(name)}</span><span class="status">⏳</span>`;
        body.appendChild(row);
      });

      overlay.querySelector('[data-action="close"]').onclick = () => { cancelled = true; overlay.remove(); resolve(); };
      overlay.querySelector('[data-action="cancel"]').onclick = () => { cancelled = true; overlay.remove(); resolve(); };

      const processNext = async (idx) => {
        if (cancelled || idx >= files.length) {
          if (!cancelled) {
            fill.style.width = '100%';
            status.textContent = errors > 0
              ? `Готово: ${completed} OK, ${errors} ошибок`
              : `Готово: ${completed} файл(ов)`;
            setTimeout(() => { overlay.remove(); resolve(); }, 800);
          }
          return;
        }
        const src = files[idx];
        const name = src.split(/[/\\]/).pop();
        const row = body.querySelector(`[data-idx="${idx}"]`);
        row.className = 'fm-copy-file active';
        row.querySelector('.status').innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

        body.scrollTop = row.offsetTop - body.offsetTop;

        try {
          await this.apiJson('POST', '/clipboard/paste-item', {
            src, dest_dir: dest, action, force: false
          });
          row.className = 'fm-copy-file ok';
          row.querySelector('.status').innerHTML = '<i class="fas fa-check"></i>';
          completed++;
        } catch (err) {
          if (err.message.includes('exists')) {
            const doReplace = await new Promise(r => {
              const d = document.createElement('div');
              d.className = 'fm-copy-error';
              d.innerHTML = `<i class="fas fa-exclamation-triangle"></i> ${this.esc(name)} уже существует. Заменить? <button class="cancel" style="margin-left:8px;padding:2px 8px" data-r="yes">Да</button> <button class="cancel" style="padding:2px 8px" data-r="no">Нет</button>`;
              row.after(d);
              d.querySelector('[data-r="yes"]').onclick = () => { d.remove(); r(true); };
              d.querySelector('[data-r="no"]').onclick = () => { d.remove(); r(false); };
            });
            if (doReplace && !cancelled) {
              try {
                await this.apiJson('POST', '/clipboard/paste-item', {
                  src, dest_dir: dest, action, force: true
                });
                row.className = 'fm-copy-file ok';
                row.querySelector('.status').innerHTML = '<i class="fas fa-check"></i>';
                completed++;
              } catch (e2) {
                row.className = 'fm-copy-file error';
                row.querySelector('.status').innerHTML = '<i class="fas fa-times"></i>';
                errors++;
              }
            } else {
              row.className = 'fm-copy-file error';
              row.querySelector('.status').innerHTML = '<i class="fas fa-times"></i>';
              errors++;
            }
          } else {
            row.className = 'fm-copy-file error';
            row.querySelector('.status').innerHTML = '<i class="fas fa-times"></i>';
            errors++;
          }
        }
        const pct = Math.round(((idx + 1) / files.length) * 100);
        fill.style.width = pct + '%';
        status.textContent = `${idx + 1} / ${files.length} файлов` + (errors > 0 ? ` • ${errors} ошибок` : '');
        processNext(idx + 1);
      };

      processNext(0);
    });
  }

  async uploadFiles(files, dest, pane) {
    const total = files.length;
    const pBar = this.showProgress(`Загрузка 0/${total}...`, 0);
    let uploaded = 0;
    let errors = 0;
    for (const f of files) {
      try {
        const fd = new FormData();
        fd.append('destination', dest);
        fd.append('file', f);
        await this.api('/upload', { method: 'POST', body: fd });
        uploaded++;
      } catch (err) {
        errors++;
        this.showNotification(`Ошибка загрузки ${f.name}: ${err.message}`, 'error');
      }
      const pct = Math.round((uploaded / total) * 100);
      const progressBar = pBar?.querySelector?.('.fm-progress-bar') || document.querySelector('#progressBar .fm-progress-bar');
      if (progressBar) progressBar.style.width = pct + '%';
      const progressEl = document.getElementById(pBar);
      if (progressEl) progressEl.textContent = `Загрузка ${uploaded}/${total}...`;
    }
    this.hideProgress(pBar);
    this.navigate(pane, dest);
    if (errors > 0) this.showNotification(`Загружено ${uploaded}/${total} (${errors} ошибок)`, errors === total ? 'error' : 'warn');
    else if (total > 1) this.showNotification(`Загружено ${total} файлов`, 'success');
  }

  upload(pane) {
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.multiple = true;
    inp.onchange = async () => {
      const dest = this.state.panePaths[pane];
      await this.uploadFiles(inp.files, dest, pane);
    };
    inp.click();
  }

  downloadSelected(pane) {
    const sel = Array.from(this.state.selectedItems[pane]);
    if (sel.length === 0) return;
    if (sel.length === 1) {
      window.open(`/filemanager/download?path=${encodeURIComponent(sel[0])}`, '_blank');
    } else {
      this.downloadAsZip(sel);
    }
  }

  async downloadAsZip(paths) {
    const pBar = this.showProgress('Создание архива...', 0);
    try {
      const res = await fetch('/filemanager/download-zip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths, archive_name: 'files.zip' })
      });
      if (!res.ok) throw new Error('Ошибка создания архива');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'files.zip';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      this.showNotification(`Скачано ${paths.length} файлов как zip`, 'success');
    } catch (err) {
      this.showNotification('Ошибка: ' + err.message, 'error');
    } finally {
      this.hideProgress(pBar);
    }
  }

  // ─── CONTEXT MENU ───────────────────────────────────
  showTrashContextMenu(e, pane, item) {
    // Мобильный слой (fm_mobile.js) показывает те же пункты bottom-sheet'ом.
    if (this.mobile) return;
    const menu = document.createElement('div');
    menu.className = 'fm-context-menu';
    document.body.appendChild(menu);

    const selIds = () => {
      const sel = this.state.selectedItems[pane];
      const items = this.state.paneItems[pane] || [];
      const chosen = item && !sel.has(item.path) ? [item] : items.filter(i => sel.has(i.path));
      return chosen.map(i => i.id || i.name);
    };

    const entries = [];
    if (item) {
      entries.push({ icon: 'undo', label: 'Восстановить', action: () => this.trashRestore(selIds()) });
      entries.push({ icon: 'copy', label: 'Восстановить как…', action: () => this.restoreAsDialog(pane, item) });
    } else if (this.state.selectedItems[pane].size) {
      entries.push({ icon: 'undo', label: `Восстановить выбранные (${this.state.selectedItems[pane].size})`, action: () => this.trashRestore(selIds()) });
    }
    entries.push({ sep: true });
    entries.push({ icon: 'copy', label: 'Копировать путь', action: () => this.copyText(item?.original_path || '') });
    entries.push({ sep: true });
    entries.push({ icon: 'trash', label: 'Удалить навсегда', danger: true, action: () => this.trashDeleteForever(selIds()) });
    entries.push({ icon: 'broom', label: 'Очистить корзину', danger: true, action: () => this.trashEmpty() });

    this.buildContextMenu(menu, entries, e);
  }

  // «Восстановить как…» — вернуть файл в выбранную папку, а не на прежнее
  // место: после переустановки системы исходный путь часто не подходит.
  async restoreAsDialog(pane, item) {
    if (!item) return;
    const def = item.original_parent || this.state.panePaths[pane];
    const target = await this.prompt('Восстановить в папку:', def);
    if (!target) return;
    if (!this.isWithinRoot(target)) {
      this.showNotification('Нельзя выйти за пределы корневой папки профиля', 'warn');
      return;
    }
    try {
      await this.apiJson('POST', '/trash/restore', { names: [item.id || item.name], target_dir: target });
      this.showNotification('Восстановлено', 'success');
    } catch (err) {
      this.showNotification(err.message, 'error');
    }
    this.state.trashDirty = true;
    this.openTrash();
  }

  copyText(text) {
    if (!text) { this.showNotification('Путь неизвестен', 'info'); return; }
    navigator.clipboard?.writeText(text)
      .then(() => this.showNotification('Путь скопирован', 'success'))
      .catch(() => this.showNotification('Не удалось скопировать', 'error'));
  }

  // Построение пунктов контекстного меню (папка/файл/пустое место).
  // Вынесено из showContextMenu без изменения состава пунктов: mobile-слой
  // (fm_mobile.js) строит из этого же массива bottom-sheet, чтобы меню
  // не расходилось между desktop и мобильной версиями.
  buildItemMenuEntries(pane, item, path) {
    const inTrash = this.isTrashPath(this.state.panePaths[pane]);
    const isDir = item && item.type === 'directory';
    const ext = item ? (item.extension || '').toLowerCase() : '';
    const isArchive = ['.zip','.tar','.tgz','.gz','.bz2','.7z','.rar'].includes(ext);
    const items = [];

      if (path) {
        // Item-specific menu
        if (isDir) {
          items.push({ icon: 'folder-open', label: 'Открыть', action: () => this.navigate(pane, path) });
          items.push({ icon: 'arrow-right', label: 'Открыть в новой панели', action: () => this.navigate(2, path) });
          items.push({ icon: 'robot', label: 'Задать задание AI для этой папки', action: () => this.openAiTasks(this.aiFolderPrompt(path), path) });
        } else {
          items.push({ icon: 'file', label: 'Открыть', action: () => { if (item) this.openFile(pane, item); }, shortcut: 'Enter' });
          items.push({ icon: 'eye', label: 'Предпросмотр', action: () => { if (item) this.showPreview(pane, path); } });
        }
        if (!isDir) {
          items.push({ icon: 'rocket', label: 'Открыть с помощью...', action: () => this.openWithDialog(path) });
          items.push({ icon: 'external-link-alt', label: 'Открыть стандартным приложением', action: () => this.openSystemApp(path) });
          const ext2 = item ? (item.extension || '').toLowerCase() : '';
          if (ext2 === '.inf') {
            // Исполняемый файл Infinity: app — открывается окном, extension —
            // ставится через мини-окно установщика. Два явных пункта понятнее,
            // чем один, который угадывает по содержимому.
            items.push({ icon: 'box', label: 'Открыть как приложение', action: async () => {
              const info = await this.infInspect(path);
              if (info) this.runInfApp(path, info);
            }});
            items.push({ icon: 'download', label: 'Установить как расширение', action: async () => {
              const info = await this.infInspect(path);
              if (info) this.showInfInstaller(path, info);
            }});
          }
          const mt2 = item ? item.media_type : '';
          // Пункты «Открыть в …» строим из каталога приложений, а не из
          // захардкоженных id и entry — иначе новое приложение из defaultapps
          // в меню не появилось бы, а опечатка в entry дала бы 404.
          const pushAppItem = (kind, icon) => {
            const app = this.appForKind(kind);
            if (!app) return;
            items.push({
              icon, label: 'Открыть в ' + app.name,
              action: () => this.openApp(app.id, path, app.main),
            });
          };
          if (mt2 === 'image') {
            items.push({ icon: 'image', label: 'Открыть в просмотрщике', action: () => this.openFullscreen('image', path) });
          } else if (mt2 === 'video') {
            pushAppItem('video', 'play-circle');
          } else if (mt2 === 'audio') {
            pushAppItem('audio', 'headphones');
          } else {
            const officeExts = ['.docx','.doc','.xlsx','.xls','.csv','.pptx','.ppt','.pdf','.odt','.ods','.odp','.rtf'];
            if (FileManagerApp.CODE_EXTENSIONS.has(ext2) || (item && item.is_text)) {
              pushAppItem('code', 'laptop-code');
            }
            if (officeExts.includes(ext2)) {
              pushAppItem('office', 'file-word');
            }
          }
        }
        items.push({ sep: true });
        items.push({ icon: 'download', label: 'Скачать', action: () => { this.state.selectedItems[pane].clear(); this.state.selectedItems[pane].add(path); this.downloadSelected(pane); } });
        items.push({ icon: 'share-alt', label: 'Поделиться…', action: () => this.openShareDialog(path, isDir) });
        if (isArchive) {
          items.push({ sep: true });
          items.push({ icon: 'file-archive', label: 'Извлечь сюда', action: () => this.extractArchive(path, this.getParentPath(path)) });
          items.push({ icon: 'folder-open', label: 'Извлечь в...', action: () => this.extractArchiveDialog(path) });
        }
        items.push({ sep: true });
        // Шифрование: пункт «Расшифровать» показываем для .infenc всегда,
        // «Зашифровать» — только когда опция включена в настройках.
        if (!isDir) {
          if (ext === '.infenc') {
            items.push({ icon: 'lock-open', label: 'Расшифровать', action: () => this.decryptSelected(pane, path) });
          } else if (this.state.encryptionEnabled) {
            items.push({ icon: 'lock', label: 'Зашифровать', action: () => this.encryptSelected(pane, path) });
          }
        }
        items.push({ icon: 'folder-open', label: 'Открыть в проводнике', action: () => this.showInFolder(path) });
        items.push({ icon: 'clipboard-list', label: 'Свойства', action: () => this.showProperties(path) });
        items.push({ icon: 'star', label: this.state.favorites.includes(path) ? 'Убрать из избранного' : 'В избранное', action: () => this.toggleFavorite(path) });
        items.push({ sep: true });
        items.push({ icon: 'exchange-alt', label: 'Переместить в...', action: () => this.moveToDialog(path, pane) });
      } else {
        // Empty space menu — toolbar has paste/delete
        items.push({ icon: 'folder-plus', label: 'Новая папка', action: () => this.newFolder(pane), shortcut: 'Ctrl+N' });
        items.push({ icon: 'file', label: 'Новый файл', action: () => this.newFile(pane) });
        items.push({ icon: 'file-alt', label: 'Текстовый документ', action: () => this.newFile(pane, 'Новый текстовый документ.txt') });
        items.push({ icon: 'code', label: 'Python файл', action: () => this.newFile(pane, 'main.py') });
        items.push({ icon: 'code', label: 'HTML файл', action: () => this.newFile(pane, 'index.html') });
        items.push({ sep: true });
        const curPath = this.state.panePaths[pane];
        if (curPath) {
          items.push({ icon: 'clipboard-list', label: 'Свойства папки', action: () => this.showProperties(curPath) });
        }
      }

    return items;
  }

  showContextMenu(e, pane) {
    this.closeContextMenu();
    const path = e.currentTarget.dataset.path;
    const inTrash = this.isTrashPath(this.state.panePaths[pane]);
    const item = (this.state.paneItems[pane] || []).find(i => i.path === path);

    // В корзине обычное меню бессмысленно: папку там не откроешь, файл не
    // переименуешь. Оставляем только то, что имеет смысл.
    if (inTrash) return this.showTrashContextMenu(e, pane, item);

    const isDir = item && item.type === 'directory';
    const ext = item ? (item.extension || '').toLowerCase() : '';
    const isArchive = ['.zip','.tar','.tgz','.gz','.bz2','.7z','.rar'].includes(ext);

    const menu = document.createElement('div');
    menu.className = 'fm-context-menu';
    document.body.appendChild(menu);

    const items = this.buildItemMenuEntries(pane, item, path);

    let d = 0;

    // Toolbar row (like Windows 11)
    const toolbar = document.createElement('div');
    toolbar.className = 'fm-ctx-toolbar';
    const hasClipboard = this.state.clipboard.items && this.state.clipboard.items.length > 0;
    const toolbarBtns = path ? [
      { icon: 'scissors', title: 'Вырезать', action: () => {
        const sel = this.state.selectedItems[pane].size > 0 ? Array.from(this.state.selectedItems[pane]) : [path];
        this.state.clipboard = { items: sel, action: 'cut' };
        this.apiJson('POST', '/clipboard/set', { items: sel, action: 'cut' });
        this.showNotification(`Вырезано: ${sel.length} эл.`, 'warn');
        this.renderClipboardIndicators(pane);
      }},
      { icon: 'copy', title: 'Копировать', action: () => {
        const sel = this.state.selectedItems[pane].size > 0 ? Array.from(this.state.selectedItems[pane]) : [path];
        this.state.clipboard = { items: sel, action: 'copy' };
        this.apiJson('POST', '/clipboard/set', { items: sel, action: 'copy' });
        this.showNotification(`Скопировано: ${sel.length} эл.`, 'info');
        this.renderClipboardIndicators(pane);
      }},
      { icon: 'paste', title: 'Вставить', action: () => this.pasteTo(pane) },
      { icon: 'i-cursor', title: 'Переименовать', action: () => { this.state.selectedItems[pane].clear(); this.state.selectedItems[pane].add(path); this.renameSelected(pane); } },
      { icon: 'trash', title: 'Удалить', danger: true, action: () => { this.state.selectedItems[pane].clear(); this.state.selectedItems[pane].add(path); this.deleteSelected(pane); } },
    ] : [
      ...(hasClipboard ? [{ icon: 'paste', title: 'Вставить', action: () => this.pasteTo(pane) }] : []),
      { icon: 'folder-plus', title: 'Новая папка', action: () => this.newFolder(pane) },
      { icon: 'file', title: 'Новый файл', action: () => this.newFile(pane) },
    ];
    toolbarBtns.forEach(b => {
      const btn = document.createElement('button');
      btn.className = 'fm-ctx-toolbar-btn' + (b.danger ? ' danger' : '');
      btn.title = b.title;
      btn.innerHTML = `<i class="fas fa-${b.icon}"></i>`;
      btn.addEventListener('click', (e) => { e.stopPropagation(); b.action(); this.closeContextMenu(); });
      toolbar.appendChild(btn);
    });
    menu.appendChild(toolbar);

    for (const item2 of items) {
      if (item2.sep) { const sep = document.createElement('div'); sep.className = 'fm-context-sep'; menu.appendChild(sep); continue; }
      const el = document.createElement('div');
      el.className = 'fm-context-item' + (item2.danger ? ' danger' : '');
      el.innerHTML = `<i class="fas fa-${item2.icon} ctx-icon"></i><span>${item2.label}</span>${item2.shortcut ? `<span class="ctx-shortcut">${item2.shortcut}</span>` : ''}`;
      el.addEventListener('click', () => { item2.action(); this.closeContextMenu(); });
      menu.appendChild(el);
      d++;
    }

    const x = Math.min(e.clientX, window.innerWidth - 220);
    const y = Math.min(e.clientY, window.innerHeight - d * 36);
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
    setTimeout(() => document.addEventListener('click', this.closeContextMenu, { once: true }), 10);
  }

  // Общий построитель пунктов контекстного меню. Вынесен из showContextMenu,
  // потому что меню корзины строится точно так же — просто другим набором
  // пунктов, и копировать разметку в третий раз незачем.
  buildContextMenu(menu, entries, e) {
    let d = 0;
    for (const item of entries) {
      if (item.sep) {
        const sep = document.createElement('div');
        sep.className = 'fm-context-sep';
        menu.appendChild(sep);
        continue;
      }
      const el = document.createElement('div');
      el.className = 'fm-context-item' + (item.danger ? ' danger' : '');
      el.innerHTML = `<i class="fas fa-${item.icon} ctx-icon"></i><span>${item.label}</span>`;
      el.addEventListener('click', () => { item.action(); this.closeContextMenu(); });
      menu.appendChild(el);
      d++;
    }
    const x = Math.min(e.clientX, window.innerWidth - 220);
    const y = Math.min(e.clientY, Math.max(10, window.innerHeight - d * 36 - 20));
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
    setTimeout(() => document.addEventListener('click', this.closeContextMenu, { once: true }), 10);
  }

  closeContextMenu() {
    document.querySelectorAll('.fm-context-menu').forEach(el => el.remove());
  }

  async extractArchive(path, dest) {
    try {
      await this.apiJson('POST', '/extract', { archive_path: path, destination_dir: dest });
      this.showNotification('Архив извлечён', 'success');
      this.navigate(this.state.activePane, dest);
    } catch (err) { this.showNotification(err.message, 'error'); }
  }

  async extractArchiveDialog(path) {
    const name = path.split(/[/\\]/).pop().replace(/\.(zip|tar|gz|bz2|7z|rar)$/i, '');
    const dest = await this.prompt('Извлечь в папку:', name);
    if (!dest) return;
    const fullDest = this.getParentPath(path) + '/' + dest;
    await this.extractArchive(path, fullDest);
  }

  // ─── PREVIEW ────────────────────────────────────────
  async showPreview(pane, path) {
    const panel = document.getElementById('previewPanel');
    const content = document.getElementById('previewContent');
    if (!panel || !content) return;
    panel.classList.remove('collapsed');
    try {
      const info = await this.api(`/info?path=${encodeURIComponent(path)}`);
      if (info.type === 'directory') {
        content.innerHTML = '<p style="color:var(--text3)"><i class="fas fa-folder-open"></i> Предпросмотр папки недоступен</p>';
        return;
      }
      const mt = info.media_type;
      const ext = (info.extension || '').toLowerCase();
      if (mt === 'image') {
        const encPath = encodeURIComponent(path);
        content.innerHTML = `<img src="/filemanager/preview/image/${encPath}" style="max-width:100%;border-radius:8px;cursor:pointer" id="previewImg">
          <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
            <button class="fm-btn primary" onclick="FM.openFullscreen('image','${encPath}')"><i class="fas fa-expand"></i> Полный экран</button>
            <button class="fm-btn" onclick="FM.downloadSelected(${pane})"><i class="fas fa-download"></i> Скачать</button>
          </div>
          <div style="margin-top:8px;padding:8px;background:var(--bg);border-radius:8px">
            <div style="font-size:11px;color:var(--text3);margin-bottom:6px;font-weight:600"><i class="fas fa-magic"></i> Редактирование</div>
            <div style="display:flex;gap:4px;flex-wrap:wrap">
              <button class="fm-btn" onclick="FM.imageRotate('${encPath}',90)" title="Повернуть вправо"><i class="fas fa-redo"></i></button>
              <button class="fm-btn" onclick="FM.imageRotate('${encPath}',-90)" title="Повернуть влево"><i class="fas fa-undo"></i></button>
              <button class="fm-btn" onclick="FM.imageResizeDialog('${encPath}')" title="Изменить размер"><i class="fas fa-arrows-alt"></i></button>
              <button class="fm-btn" onclick="FM.imageCropDialog('${encPath}')" title="Обрезать"><i class="fas fa-crop-alt"></i></button>
              <button class="fm-btn" onclick="FM.imageFilter('${encPath}','grayscale')" title="Ч/Б"><i class="fas fa-adjust"></i></button>
              <button class="fm-btn" onclick="FM.imageFilter('${encPath}','sepia')" title="Сепия"><i class="fas fa-palette"></i></button>
              <button class="fm-btn" onclick="FM.imageFilter('${encPath}','blur')" title="Размытие"><i class="fas fa-tint"></i></button>
              <button class="fm-btn" onclick="FM.imageFilter('${encPath}','brightness')" title="Яркость"><i class="fas fa-sun"></i></button>
              <button class="fm-btn" onclick="FM.imageFilter('${encPath}','contrast')" title="Контраст"><i class="fas fa-half-circle"></i></button>
            </div>
          </div>`;
      } else if (mt === 'video') {
        content.innerHTML = `<video controls style="max-width:100%;border-radius:8px" src="/filemanager/preview/video/${encodeURIComponent(path)}"></video>
          <div style="margin-top:8px"><button class="fm-btn primary" onclick="FM.openFullscreen('video','${encodeURIComponent(path)}')"><i class="fas fa-expand"></i> Полный экран</button></div>`;
      } else if (mt === 'audio') {
        content.innerHTML = `<audio controls style="width:100%" src="/filemanager/preview/audio/${encodeURIComponent(path)}"></audio>
          <p style="margin-top:8px;color:var(--text2)">${this.esc(info.name)}</p>`;
      } else if (mt === 'pdf') {
        content.innerHTML = `<iframe src="/filemanager/download?path=${encodeURIComponent(path)}" style="width:100%;height:400px;border:none;border-radius:8px"></iframe>`;
      } else if (ext === '.inf') {
        // .inf — это JSON, но показывать его сырым бесполезно: карточка с
        // типом, именем и кнопкой запуска полезнее.
        const inf = await this.infInspect(path);
        if (inf) {
          const isApp = inf.kind === 'app';
          const details = isApp
            ? (inf.has_python ? 'Приложение с Python-частью (песочница)' : 'Приложение (HTML/CSS/JS)')
            : `Расширение · модуль ${inf.target_module || 'all'} · файлов: ${(inf.files || []).length}`;
          content.innerHTML = `
            <div style="display:flex;gap:12px;align-items:flex-start">
              <div style="width:40px;height:40px;flex-shrink:0;border-radius:10px;background:var(--bg);display:flex;align-items:center;justify-content:center">
                <i class="fas ${isApp ? 'fa-box' : 'fa-puzzle-piece'}" style="font-size:18px;color:var(--accent)"></i>
              </div>
              <div style="flex:1;min-width:0">
                <div style="font-weight:600;font-size:13px">${this.esc(inf.name || info.name)}</div>
                <div style="font-size:11px;color:var(--text3);margin-top:3px">${this.esc(details)}</div>
                ${inf.description ? `<div style="font-size:11px;color:var(--text2);margin-top:6px;line-height:1.5">${this.esc(inf.description)}</div>` : ''}
                ${(inf.permissions || []).length ? `<div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:3px">${inf.permissions.map(p => `<span style="font-size:10px;padding:2px 6px;border-radius:5px;background:var(--bg);border:1px solid var(--border)">${this.esc(p)}</span>`).join('')}</div>` : ''}
              </div>
            </div>
            <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
              <button class="fm-btn primary" id="infPvRun"><i class="fas fa-play"></i> ${isApp ? 'Открыть' : 'Установить'}</button>
              <button class="fm-btn" id="infPvEdit"><i class="fas fa-code"></i> Открыть в редакторе</button>
            </div>`;
          const enc = encodeURIComponent(path);
          document.getElementById('infPvRun')?.addEventListener('click', () => {
            if (isApp) this.runInfApp(path, inf);
            else this.showInfInstaller(path, inf);
          });
          document.getElementById('infPvEdit')?.addEventListener('click', () => this.openEditor(enc));
        } else {
          content.innerHTML = '<p style="color:var(--text3)"><i class="fas fa-box"></i> Файл .inf повреждён или имеет неизвестный формат</p>';
        }
      } else if (info.is_text) {
        const txt = await this.api(`/read?path=${encodeURIComponent(path)}`);
        content.innerHTML = `<pre style="max-height:300px;overflow:auto;background:var(--bg);padding:12px;border-radius:var(--radius-sm);font-size:12px">${this.esc(txt.content)}</pre>
          <div style="margin-top:8px"><button class="fm-btn primary" onclick="FM.openEditor('${encodeURIComponent(path)}')"><i class="fas fa-edit"></i> Редактировать</button></div>`;
      } else if (ext === '.zip' || ext === '.tar' || ext === '.gz' || ext === '.bz2' ||
                 ext === '.7z' || ext === '.rar') {
        this.previewArchive(path, content);
      } else {
        content.innerHTML = `<p style="color:var(--text3);margin-bottom:8px"><i class="fas fa-file"></i> Предпросмотр недоступен</p>
          <button class="fm-btn primary" onclick="FM.downloadSelected(${pane})"><i class="fas fa-download"></i> Скачать</button>`;
      }
      document.getElementById('previewTitle').textContent = info.name;
    } catch (err) {
      content.innerHTML = `<p style="color:var(--red)">Ошибка: ${this.esc(err.message)}</p>`;
    }
  }

  async previewArchive(path, content) {
    content.innerHTML = '<p style="color:var(--text3)">Загрузка содержимого архива...</p>';
    try {
      const data = await this.apiJson('POST', '/archive/preview', { archive_path: path });
      let html = `<div style="margin-bottom:8px;font-weight:600"><i class="fas fa-file-archive"></i> ${this.esc(data.archive)} — ${data.total} файлов</div><div class="fm-list">`;
      for (const item of data.items) {
        const icon = item.is_dir ? '<i class="fas fa-folder"></i>' : '<i class="fas fa-file"></i>';
        html += `<div class="fm-list-row" style="grid-template-columns:28px 1fr 80px">`;
        html += `<div>${icon}</div><div style="overflow:hidden;text-overflow:ellipsis">${this.esc(item.name)}</div>`;
        html += `<div style="text-align:right">${item.size ? this.formatSize(item.size) : ''}</div></div>`;
      }
      html += '</div>';
      content.innerHTML = html;
    } catch (err) {
      content.innerHTML = `<p style="color:var(--red)">Ошибка: ${this.esc(err.message)}</p>`;
    }
  }

  // ─── OPEN IN APP ────────────────────────────────────
  // entry приходит из каталога /api/apps (у musicmachine это music.html,
  // у video — videoeddit.html). Раньше путь был жёстко
  // '/app-files/{id}/index.html', и приложения без index.html открывались 404.
  //
  // Внутри рабочего стола ФМ живёт во фрейме, и окно приложения должно
  // открыться ОТ РОДИТЕЛЯ (/desktop умеет — там WinBox и обработчик
  // openAppAck). Но page_desktop.html такого обработчика не имеет, и
  // раньше это выяснялось слепым таймером в 400 мс: сообщение уходило,
  // ответа не было, приложение открывалось новой вкладкой.
  //
  // Теперь один раз спрашиваем родителя прямо — «умеешь открывать
  // приложения?» — и по ответу решаем. Отвечать умеют оба рабочих стола,
  // поэтому слепой задержки больше нет.

  _askParentSupport() {
    if (this._parentSupport !== undefined) return Promise.resolve(this._parentSupport);
    if (window.parent === window) { this._parentSupport = false; return Promise.resolve(false); }
    return new Promise((resolve) => {
      const probeId = 'fm-probe-' + Date.now() + '-' + Math.random().toString(36).slice(2);
      let done = false;
      const onMsg = (ev) => {
        if (ev.data && ev.data.action === 'openAppSupported' && ev.data.reqId === probeId) {
          done = true;
          window.removeEventListener('message', onMsg);
          this._parentSupport = !!ev.data.supported;
          resolve(this._parentSupport);
        }
      };
      window.addEventListener('message', onMsg);
      window.parent.postMessage({ action: 'openAppProbe', reqId: probeId }, '*');
      // Родитель мог не услышать (или его нет вовсе) — считаем, что не умеет.
      setTimeout(() => {
        if (done) return;
        window.removeEventListener('message', onMsg);
        this._parentSupport = false;
        resolve(false);
      }, 350);
    });
  }

  // Окно приложения средствами самого ФМ (WinBox подключён в шаблоне).
  appWindow(title, url) {
    const offset = (this._appWinCount = (this._appWinCount || 0) + 1) * 28;
    const html = `<iframe src="${url}" style="width:100%;height:100%;border:none;background:#0f1419"
                    sandbox="allow-same-origin allow-scripts allow-forms allow-modals allow-popups"></iframe>`;
    if (typeof WinBox === 'undefined') {
      window.open(url, '_blank');
      return;
    }
    new WinBox(this.esc(title), {
      html,
      x: Math.max(0, 90 + offset), y: Math.max(0, 60 + offset),
      width: Math.min(1000, window.innerWidth - 40),
      height: Math.min(680, window.innerHeight - 100),
      minwidth: 360, minheight: 240, max: false,
    });
  }

  async openApp(appId, filePath, entry, mode) {
    const page = entry || 'index.html';
    // Способ передачи файла выбирает диалог «Открыть с помощью…»:
    //   param — приложение само читает ?file=;
    //   drop  — эмулировать перетаскивание;
    //   both  — параметр + drop; auto/пусто — прежнее поведение.
    const m = mode && mode !== 'auto' && mode !== 'none'
      ? `&mode=${encodeURIComponent(mode)}` : '';
    // Для чистого drop имя параметра другое (dropfile): приложение, которое
    // само читает ?file=, не откроет файл дважды — параметром и перетаскиванием.
    const fileKey = mode === 'drop' ? 'dropfile' : 'file';
    // _v — обход кеша Electron: раньше при повторном открытии того же файла
    // он отдавал закешированный HTML БЕЗ автозагрузчика, и файл не появлялся.
    const url = `/app-files/${appId}/${page}` +
      (filePath ? `?${fileKey}=${encodeURIComponent(filePath)}&_v=${Date.now()}${m}` : '');
    const title = (this.appById(appId) || {}).name || appId;

    if (window.parent === window) {
      // Отдельная вкладка файлового менеджера — открываем окном у себя.
      this.appWindow(title, url);
      return;
    }

    if (await this._askParentSupport()) {
      // Родитель умеет (это /desktop с WinBox) — просим открыть у него.
      const reqId = 'fm-open-' + Date.now() + '-' + Math.random().toString(36).slice(2);
      window.parent.postMessage(
        { action: 'openApp', reqId, url, id: appId, entry: page, file: filePath }, '*');
      return;
    }

    // Родитель не умеет (например page_desktop.html) — открываем сами,
    // чтобы пользователь не терял приложение в новой вкладке.
    this.appWindow(title, url);
  }

  // ─── АССОЦИАЦИИ «РАСШИРЕНИЕ → ПРИЛОЖЕНИЕ» ──────────
  // Хранятся на сервере (data/file_associations.json), чтобы «открывать по
  // умолчанию» работало и в другой вкладке/после перезапуска.
  async loadAssociations(force = false) {
    if (this._assocLoaded && !force) return this.state.associations || {};
    try {
      const d = await this.api('/associations');
      this.state.associations = (d && d.associations) || {};
    } catch {
      this.state.associations = this.state.associations || {};
    }
    this._assocLoaded = true;
    return this.state.associations;
  }

  assocFor(ext) {
    return (this.state.associations || {})[(ext || '').toLowerCase()] || null;
  }

  async saveAssociation(ext, target, method, label, del = false) {
    try {
      const r = await this.apiJson('POST', '/associations',
        { ext, target, method, label, delete: !!del });
      this.state.associations = (r && r.associations) || this.state.associations || {};
      this._assocLoaded = true;
      if (!del) this.showNotification(`По умолчанию для ${ext}: ${label || target}`, 'success');
      else this.showNotification(`Ассоциация для ${ext} сброшена`, 'info');
    } catch (e) {
      this.showNotification('Не удалось сохранить ассоциацию: ' + e.message, 'error');
    }
  }

  // Список установленных программ Windows (кэш сервера + фоновое обновление).
  loadWindowsApps(force = false) {
    if (this._winAppsPromise && !force) return this._winAppsPromise;
    this._winAppsPromise = (async () => {
      try {
        const r = await fetch('/filemanager/windows-apps' + (force ? '?refresh=1' : ''));
        const d = await r.json();
        this.state.winApps = Array.isArray(d.apps) ? d.apps : [];
      } catch {
        this.state.winApps = [];
      }
      return this.state.winApps;
    })();
    return this._winAppsPromise;
  }

  // Запуск установленной программы с файлом (аргументом командной строки).
  async openWithExe(exe, path, withFile = true) {
    try {
      const r = await this.apiJson('POST', '/open-with', { exe, path, with_file: !!withFile });
      if (r && r.ok === false) this.showNotification(r.error || 'Не удалось открыть', 'error');
    } catch (e) { this.showNotification(e.message, 'error'); }
  }

  // Применить сохранённую ассоциацию (вызывается из openFile до обычных веток).
  // Возвращает true, если файл открыт; false — ассоциация устарела (приложение
  // удалили), и openFile продолжает обычное открытие вместо запроса в 404.
  async applyAssociation(assoc, path) {
    const t = assoc.target || '';
    if (t === 'system') { this.openSystemApp(path); return true; }
    if (t === 'builtin_image') { this.openFullscreen('image', path); return true; }
    if (t === 'infinity:editor' || t === 'infinity:viewer') {
      return this.applyInfinityTarget(t, path);
    }
    if (t.startsWith('app:')) {
      const id = t.slice(4);
      await this.loadApps();
      const app = this.appById(id);
      if (!app) {
        this.showNotification(
          `Приложение «${id}» больше не установлено — открываю обычным способом`, 'warn');
        return false;
      }
      const mode = assoc.method || 'auto';
      this.openApp(id, mode === 'none' ? '' : path, app.main || 'index.html', mode);
      return true;
    }
    if (t.startsWith('exe:')) {
      this.openWithExe(t.slice(4), path, assoc.method !== 'none');
      return true;
    }
    return false;
  }

  // ─── ОТКРЫТИЕ В INFINITY ────────────────────────────
  // Цель ассоциации «Открыть в Infinity»: редактор для текста/кода,
  // встроенный просмотр для медиа, PDF, HTML и архивов.
  async applyInfinityTarget(target, path) {
    if (target === 'infinity:editor') {
      this.openEditor(path);
      return true;
    }
    const type = FileManagerApp.viewerTypeFor(path);
    if (type) {
      this.openFullscreen(type, path);
      return true;
    }
    // Текст без «медийного» расширения всё равно можно править в редакторе.
    if (FileManagerApp.CODE_EXTENSIONS.has(FileManagerApp.extOf(path))) {
      this.openEditor(path);
      return true;
    }
    this.showNotification(
      'Для этого типа файла нет встроенного просмотра — выберите другое приложение', 'warn');
    return false;
  }

  // ─── OPEN FILE ──────────────────────────────────────
  // Открыть файл/папку приложением, назначенным в системе (assoc по расширению).
  // На Windows это os.startfile, на Linux/macOS — xdg-open/open.
  async openSystemApp(path) {
    try {
      const res = await this.apiJson('POST', '/open-external', { path });
      if (res && res.ok === false) this.showNotification(res.error || 'Не удалось открыть', 'error');
      else this.showNotification('Открыто стандартным приложением', 'success');
    } catch (err) { this.showNotification(err.message, 'error'); }
  }

  // Показать в Проводнике (Windows) / открыть родительскую папку (POSIX).
  async showInFolder(path) {
    try {
      const res = await this.apiJson('POST', '/show-in-folder', { path });
      if (res && res.ok === false) this.showNotification(res.error || 'Не удалось', 'error');
    } catch (err) { this.showNotification(err.message, 'error'); }
  }

  // ─── ПРИЛОЖЕНИЯ ───────────────────────────────────────
  // Раньше список приложений был захардкожен (APP_VIDEO='video',
  // APP_AUDIO='musicmachine', APP_EDITOR='code-studio-3',
  // APP_OFFICE='verba-office'), а точки входа передавались руками
  // ('videoeddit.html', 'music.html'). Из-за этого новое приложение из
  // defaultapps было недоступно, а опечатка в entry давала 404. Теперь
  // список и точки входа приходят из /api/apps — их определил сканер
  // каталогов, который умеет находить папку и без index.html.

  // ─── ШИФРОВАНИЕ ФАЙЛОВ ──────────────────────────────
  async loadEncryptionStatus() {
    try {
      const d = await this.api('/encryption/status');
      this.state.encryptionEnabled = !!d.enabled;
      this.state.encryptionAvailable = d.available !== false;
    } catch {
      this.state.encryptionAvailable = false;
    }
    this._syncEncryptionButton();
  }

  _syncEncryptionButton() {
    const btn = document.getElementById('encryptionBtn');
    if (!btn) return;
    btn.classList.toggle('active', !!this.state.encryptionEnabled);
    btn.title = this.state.encryptionEnabled
      ? 'Шифрование файлов включено: пункт «Зашифровать» есть в меню файла. Нажмите, чтобы выключить.'
      : 'Шифрование файлов выключено. Необязательная опция: включите, чтобы в меню файла появился пункт «Зашифровать».';
    const icon = btn.querySelector('i');
    if (icon) icon.className = this.state.encryptionEnabled ? 'fas fa-lock' : 'fas fa-lock-open';
    const label = btn.querySelector('span');
    if (label) label.textContent = this.state.encryptionEnabled ? 'Шифр. вкл' : 'Шифр. выкл';
  }

  async toggleEncryption(enabled) {
    try {
      const d = await this.apiJson('POST', '/encryption/toggle', { enabled: !!enabled });
      this.state.encryptionEnabled = !!d.enabled;
      this._syncEncryptionButton();
      this.showNotification(
        this.state.encryptionEnabled
          ? 'Шифрование включено. Теперь в меню файла есть «Зашифровать» — файл заменится копией .infenc.'
          : 'Шифрование выключено. Уже зашифрованные .infenc можно расшифровать в любой момент.',
        'success'
      );
    } catch (err) {
      this.showNotification(err.message, 'error');
    }
  }

  // Что шифровать/расшифровывать: выделение панели, а если кликнули по
  // невыделенному файлу — только он.
  _encryptionTargets(pane, clickedPath) {
    const sel = this.state.selectedItems[pane];
    const paths = (clickedPath && !sel.has(clickedPath))
      ? [clickedPath]
      : (sel.size ? Array.from(sel) : (clickedPath ? [clickedPath] : []));
    const lower = p => String(p).toLowerCase();
    return {
      toEncrypt: paths.filter(p => !lower(p).endsWith('.infenc')),
      toDecrypt: paths.filter(p => lower(p).endsWith('.infenc')),
    };
  }

  async encryptSelected(pane, clickedPath) {
    if (!this.state.encryptionEnabled) {
      this.showNotification('Сначала включите шифрование в настройках', 'warn');
      return;
    }
    const { toEncrypt } = this._encryptionTargets(pane, clickedPath);
    if (!toEncrypt.length) { this.showNotification('Нет файлов для шифрования', 'warn'); return; }
    const ok = await this.confirm(
      `Зашифровать файлов: <b>${toEncrypt.length}</b>?<br><br>` +
      `<span style="font-size:12px;color:var(--text3)">Файлы будут заменены копиями с расширением <b>.infenc</b>. ` +
      `Ключ хранится в профиле пользователя (~/.infinity/asio_agent/encryption.key); без него восстановить данные нельзя.</span>`
    );
    if (!ok) return;
    try {
      const d = await this.apiJson('POST', '/encrypt', { paths: toEncrypt });
      this._reportEncryption(d, 'Зашифровано');
      this.navigate(pane, this.state.panePaths[pane]);
    } catch (err) {
      this.showNotification(err.message, 'error');
    }
  }

  async decryptSelected(pane, clickedPath) {
    const { toDecrypt } = this._encryptionTargets(pane, clickedPath);
    if (!toDecrypt.length) { this.showNotification('Нет зашифрованных файлов', 'warn'); return; }
    try {
      const d = await this.apiJson('POST', '/decrypt', { paths: toDecrypt });
      this._reportEncryption(d, 'Расшифровано');
      this.navigate(pane, this.state.panePaths[pane]);
    } catch (err) {
      this.showNotification(err.message, 'error');
    }
  }

  _reportEncryption(d, verb) {
    const failed = (d.results || []).filter(r => !r.ok);
    if (d.count) this.showNotification(`${verb}: ${d.count}`, 'success');
    if (failed.length) {
      this.showNotification(`${failed.length} не удалось: ${failed[0].error || 'ошибка'}`, 'error');
    }
  }

  // ─── ПОДЕЛИТЬСЯ ──────────────────────────────────────
  // Ссылка на файл и/или копия в другой профиль. Бэкенд — file_share_module.py.
  async _shareApi(method, endpoint, body) {
    const opts = { method, cache: 'no-store' };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const res = await fetch('/filemanager' + endpoint, opts);
    let data = null;
    try { data = await res.json(); } catch { /* не JSON */ }
    if (!res.ok || (data && data.ok === false)) {
      const msg = (data && (data.error || data.detail)) || ('HTTP ' + res.status);
      throw new Error(msg);
    }
    return data;
  }

  _fmtDate(sec) {
    if (!sec) return 'бессрочно';
    const d = new Date(sec * 1000);
    return d.toLocaleDateString('ru-RU') + ' ' +
           d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  async openShareDialog(path, isDir = false) {
    isDir = !!isDir;
    const name = String(path).split(/[\\/]/).filter(Boolean).pop() || path;
    const body = `
      <div class="fm-share">
        <div class="fm-share-file"><i class="fas fa-${isDir ? 'folder' : 'file'}"></i>
          <b>${this.esc(name)}</b>
          <div class="fm-share-sub">${this.esc(path)}</div></div>

        <div class="fm-share-tabs" id="shareTabs">
          <button type="button" class="active" data-mode="access"><i class="fas fa-user-check"></i> Дать доступ</button>
          <button type="button" data-mode="copy"><i class="fas fa-copy"></i> Поделиться копией</button>
          ${isDir ? '' : '<button type="button" data-mode="link"><i class="fas fa-link"></i> Ссылка</button>'}
        </div>

        <div class="fm-share-pane" id="sharePaneAccess">
          <div class="fm-share-row"><label>Профиль</label>
            <select id="shareAccessProfile"><option value="">Загрузка…</option></select></div>
          <label class="fm-share-opt">
            <input type="checkbox" id="shareCanEdit">
            <span><b>Разрешить редактирование</b>
              <small>Получатель откроет тот же ${isDir ? 'папку' : 'файл'} в «Файлах» и сможет вносить изменения. Без галочки — только просмотр.</small>
            </span>
          </label>
          <div class="fm-share-actions">
            <button class="fm-btn primary" id="shareAccessGo"><i class="fas fa-user-plus"></i> Дать доступ</button>
            <span class="fm-share-hint" id="shareAccessHint"></span>
          </div>
        </div>

        <div class="fm-share-pane" id="sharePaneCopy" hidden>
          <div class="fm-share-row"><label>Профиль</label>
            <select id="shareCopyProfile"><option value="">Загрузка…</option></select></div>
          <div class="fm-share-hint-small">Получатель получит копию в свой раздел «Shared» — оригинал не изменится.</div>
          <div class="fm-share-actions">
            <button class="fm-btn primary" id="shareCopyGo"><i class="fas fa-paper-plane"></i> Отправить копию</button>
            <span class="fm-share-hint" id="shareCopyHint"></span>
          </div>
        </div>

        ${isDir ? '' : `
        <div class="fm-share-pane" id="sharePaneLink" hidden>
          <div class="fm-share-row"><label>Срок действия</label>
            <select id="shareDays">
              <option value="1">1 день</option>
              <option value="7" selected>7 дней</option>
              <option value="30">30 дней</option>
              <option value="0">Бессрочно</option>
            </select></div>
          <div class="fm-share-hint-small">Ссылка открывается в браузере у любого, кому её отправили: только скачивание, без редактирования.</div>
          <div class="fm-share-actions">
            <button class="fm-btn primary" id="shareLinkGo"><i class="fas fa-link"></i> Создать ссылку</button>
            <span class="fm-share-hint" id="shareLinkHint"></span>
          </div>
        </div>`}

        <div class="fm-share-result" id="shareResult" hidden></div>

        <div class="fm-share-existing">
          <div class="fm-share-existing-head">
            <span>Мои доступы и ссылки</span>
            <button class="fm-btn" id="shareReload" title="Обновить"><i class="fas fa-sync"></i></button>
          </div>
          <div id="shareList" class="fm-share-list">Загрузка…</div>
        </div>
      </div>`;

    const overlay = this.showModal('Поделиться', body, 'md', 'shareModal');
    const $ = id => overlay.querySelector('#' + id);

    // Переключение режимов: доступ / копия / ссылка.
    const panes = { access: $('sharePaneAccess'), copy: $('sharePaneCopy'), link: $('sharePaneLink') };
    overlay.querySelectorAll('#shareTabs button').forEach(btn => {
      btn.addEventListener('click', () => {
        overlay.querySelectorAll('#shareTabs button').forEach(b => b.classList.toggle('active', b === btn));
        Object.entries(panes).forEach(([mode, el]) => { if (el) el.hidden = mode !== btn.dataset.mode; });
      });
    });

    // Список профилей грузим сразу для обоих режимов.
    this._loadShareProfiles($('shareAccessProfile'));
    this._loadShareProfiles($('shareCopyProfile'));

    // Имя профиля запоминаем ДО запроса: список может перерисоваться.
    const profileName = (sel) => {
      const o = sel.options[sel.selectedIndex];
      return o ? o.textContent.replace(' (текущий)', '') : sel.value;
    };
    const showResult = (html) => {
      const out = $('shareResult');
      out.hidden = false;
      out.innerHTML = html;
    };

    const renderList = (links, grants) => {
      const box = $('shareList');
      box.innerHTML = '';
      if (!links.length && !grants.length) {
        box.innerHTML = '<div class="fm-share-empty">Пока ничего не отправлено.</div>';
        return;
      }
      // Сначала доступы: это живые права на файл, их важнее видеть.
      grants.forEach(g => {
        const row = document.createElement('div');
        row.className = 'fm-share-item';
        row.innerHTML =
          `<div class="fm-share-item-main">
             <i class="fas fa-${g.is_dir ? 'folder' : 'file'}"></i>
             <div><b>${this.esc(g.name)}</b>
               <div class="fm-share-sub">${this.esc(g.recipient_name || g.recipient)} ·
                 ${g.can_edit ? 'редактирование' : 'просмотр'} ·
                 ${g.status === 'pending' ? 'ожидает принятия' : 'принят'}${g.missing ? ' · файл недоступен' : ''}</div>
             </div>
           </div>
           <div class="fm-share-item-btns">
             <button title="Отозвать доступ" class="danger"><i class="fas fa-user-slash"></i></button>
           </div>`;
        row.querySelector('button').addEventListener('click', async () => {
          try {
            await this._shareApi('POST', '/share/revoke', { id: g.id });
            this.showNotification('Доступ отозван', 'success');
            loadList();
          } catch (e) { this.showNotification(e.message, 'error'); }
        });
        box.appendChild(row);
      });
      links.forEach(r => {
        const row = document.createElement('div');
        row.className = 'fm-share-item';
        row.innerHTML =
          `<div class="fm-share-item-main">
             <i class="fas fa-link"></i>
             <div><b>${this.esc(r.name)}</b>
               <div class="fm-share-sub">${r.expires ? 'до ' + this._fmtDate(r.expires) : 'бессрочная'}${r.missing ? ' · файл недоступен' : ''}</div>
             </div>
           </div>
           <div class="fm-share-item-btns">
             <button title="Копировать ссылку"><i class="fas fa-copy"></i></button>
             <button title="Открыть"><i class="fas fa-external-link-alt"></i></button>
             <button title="Отозвать" class="danger"><i class="fas fa-trash"></i></button>
           </div>`;
        const url = location.origin + r.url;
        row.querySelectorAll('button')[0].addEventListener('click', () => {
          navigator.clipboard?.writeText(url)
            .then(() => this.showNotification('Ссылка скопирована', 'success'))
            .catch(() => this.showNotification('Не удалось скопировать', 'error'));
        });
        row.querySelectorAll('button')[1].addEventListener('click', () => window.open(url, '_blank'));
        row.querySelectorAll('button')[2].addEventListener('click', async () => {
          try {
            await this._shareApi('POST', '/share/revoke', { token: r.token });
            this.showNotification('Ссылка отозвана', 'success');
            loadList();
          } catch (e) { this.showNotification(e.message, 'error'); }
        });
        box.appendChild(row);
      });
    };

    const loadList = async () => {
      try {
        const d = await this._shareApi('GET', '/shares');
        renderList(d.shares || [], d.grants || []);
      } catch (e) {
        $('shareList').innerHTML = `<div class="fm-share-empty">Не удалось загрузить: ${this.esc(e.message)}</div>`;
      }
    };
    $('shareReload').addEventListener('click', loadList);
    loadList();

    $('shareAccessGo').addEventListener('click', async () => {
      const sel = $('shareAccessProfile');
      const profile = sel.value;
      if (!profile) { $('shareAccessHint').textContent = 'Выберите профиль.'; return; }
      const go = $('shareAccessGo');
      go.disabled = true;
      $('shareAccessHint').textContent = 'Отправляю…';
      try {
        const can_edit = $('shareCanEdit').checked;
        await this._shareApi('POST', '/share/access', { path, profile, can_edit });
        $('shareAccessHint').textContent = '';
        showResult(`<div class="fm-share-sub"><i class="fas fa-check"></i>
          Доступ ${can_edit ? 'с редактированием' : '(только просмотр)'} выдан профилю
          <b>${this.esc(profileName(sel))}</b>. Уведомление уже в его панели —
          принять или отклонить он сможет в «Файлы → Доступы».</div>`);
        this.showNotification('Доступ предоставлен', 'success');
        loadList();
      } catch (e) {
        $('shareAccessHint').textContent = e.message;
      } finally { go.disabled = false; }
    });

    $('shareCopyGo').addEventListener('click', async () => {
      const sel = $('shareCopyProfile');
      const profile = sel.value;
      if (!profile) { $('shareCopyHint').textContent = 'Выберите профиль.'; return; }
      const go = $('shareCopyGo');
      go.disabled = true;
      $('shareCopyHint').textContent = 'Отправляю…';
      try {
        const d = await this._shareApi('POST', '/share', { path, kind: 'copy', profile });
        $('shareCopyHint').textContent = '';
        showResult(`<div class="fm-share-sub"><i class="fas fa-check"></i> Копия отправлена в профиль
          <b>${this.esc(profileName(sel))}</b>: ${this.esc(d.copy ? d.copy.name : '')}</div>`);
        this.showNotification('Копия отправлена', 'success');
        loadList();
      } catch (e) {
        $('shareCopyHint').textContent = e.message;
      } finally { go.disabled = false; }
    });

    const linkGo = $('shareLinkGo');
    if (linkGo) linkGo.addEventListener('click', async () => {
      const go = linkGo;
      go.disabled = true;
      $('shareLinkHint').textContent = 'Создаю…';
      try {
        const d = await this._shareApi('POST', '/share', {
          path, kind: 'link', days: parseInt($('shareDays').value, 10),
        });
        $('shareLinkHint').textContent = '';
        const url = location.origin + d.link.url;
        const box = document.createElement('div');
        box.className = 'fm-share-linkbox';
        box.innerHTML = `<i class="fas fa-link"></i>
          <input readonly value="${this.esc(url)}">
          <button class="fm-btn primary" title="Копировать"><i class="fas fa-copy"></i></button>
          <button class="fm-btn" title="Открыть"><i class="fas fa-external-link-alt"></i></button>`;
        const inp = box.querySelector('input');
        box.querySelectorAll('button')[0].addEventListener('click', () => {
          inp.select();
          navigator.clipboard?.writeText(url)
            .then(() => this.showNotification('Ссылка скопирована', 'success'))
            .catch(() => this.showNotification('Не удалось скопировать', 'error'));
        });
        box.querySelectorAll('button')[1].addEventListener('click', () => window.open(url, '_blank'));
        const note = document.createElement('div');
        note.className = 'fm-share-sub';
        note.textContent = d.link.expires
          ? 'Действует до ' + this._fmtDate(d.link.expires)
          : 'Бессрочная ссылка — отозвать можно в списке ниже.';
        const out = $('shareResult');
        out.hidden = false;
        out.innerHTML = '';
        out.appendChild(box);
        out.appendChild(note);
        this.showNotification('Ссылка создана', 'success');
        loadList();
      } catch (e) {
        $('shareLinkHint').textContent = e.message;
      } finally { go.disabled = false; }
    });
  }

  async _loadShareProfiles(sel, force) {
    if (!sel) return;
    if (sel.dataset.loaded && !force) return;
    if (sel.dataset.loading) return;
    sel.dataset.loading = '1';
    try {
      const d = await this._shareApi('GET', '/share-profiles');
      const rows = d.profiles || [];
      // Читаем выбор ПОСЛЕ ответа: пока шёл запрос, пользователь мог выбрать
      // профиль в старом списке, и этот выбор терять нельзя.
      const prev = sel.value;
      sel.innerHTML = rows.map(p =>
        `<option value="${this.esc(p.id)}">${this.esc(p.name)}${p.is_active ? ' (текущий)' : ''}</option>`
      ).join('') || '<option value="">Нет профилей</option>';
      // Возврат к выбору пользователя: перерисовка не должна его сбрасывать.
      if (prev && rows.some(p => p.id === prev)) {
        sel.value = prev;
      } else {
        // Текущий профиль по умолчанию не выбираем: отправлять файл самому
        // себе — заведомо ошибочный сценарий.
        const target = rows.find(p => !p.is_active) || rows[0];
        if (target) sel.value = target.id;
      }
      sel.dataset.loaded = '1';
    } catch (e) {
      sel.innerHTML = `<option value="">Ошибка: ${this.esc(e.message)}</option>`;
      delete sel.dataset.loaded;
    } finally {
      delete sel.dataset.loading;
    }
  }

  // ─── ДОСТУПЫ КО МНЕ ─────────────────────────────────
  // Получатель видит чужие файлы в отдельном окне: принять/отклонить,
  // посмотреть содержимое, а при can_edit — изменить и сохранить оригинал.
  async checkIncomingShares(autoOpen = false, force = false) {
    let rows = [];
    try {
      const d = await this._shareApi('GET', '/share/incoming');
      rows = d.grants || [];
    } catch (e) {
      return [];
    }
    this.state.incomingGrants = rows;
    const pending = rows.filter(g => g.status === 'pending');
    ['incomingBadge', 'incomingBadgeTop'].forEach(id => {
      const badge = document.getElementById(id);
      if (!badge) return;
      badge.textContent = String(pending.length);
      badge.hidden = !pending.length;
    });
    // force — пришли из уведомления: открываем даже если всё уже принято,
    // чтобы человек увидел список доступов, а не пустой экран.
    const shouldOpen = force || (autoOpen && pending.length);
    if (shouldOpen && !this._incomingAutoShown) {
      this._incomingAutoShown = true;
      this.openIncomingDialog();
    }
    return rows;
  }

  // Если в какой-то панели открыт Shared — перечитать её: принятый файл
  // должен появиться сразу, без ручного обновления.
  refreshSharedPanes() {
    for (const pane of [1, 2]) {
      const cur = this.state.panePaths[pane] || '';
      if (!cur) continue;
      if (/[\\/]Shared[\\/]?$/.test(cur)) {
        this.navigate(pane, cur).catch(() => {});
      }
    }
    this.renderTree();
  }

  async openIncomingDialog() {
    const modal = this.showModal('<i class="fas fa-inbox"></i> Доступы ко мне',
      `<div class="fm-share">
         <div class="fm-share-hint-small">Файлы и папки, которыми с вами поделились другие профили. Пока доступ не принят, он остаётся заявкой.</div>
         <div class="fm-share-existing" style="border-top:0;padding-top:0">
           <div class="fm-share-existing-head">
             <span>Список</span>
             <button class="fm-btn" id="incReload" title="Обновить"><i class="fas fa-sync"></i></button>
           </div>
           <div id="incList" class="fm-share-list">Загрузка…</div>
         </div>
       </div>`, 'md', 'incomingModal');

    const box = modal.querySelector('#incList');
    const render = (rows) => {
      box.innerHTML = '';
      if (!rows.length) {
        box.innerHTML = '<div class="fm-share-empty">Вам пока ничего не отправляли.</div>';
        return;
      }
      rows.forEach(g => {
        const row = document.createElement('div');
        row.className = 'fm-share-item';
        const pending = g.status === 'pending';
        row.innerHTML =
          `<div class="fm-share-item-main">
             <i class="fas fa-${g.is_dir ? 'folder' : 'file'}"></i>
             <div><b>${this.esc(g.name)}</b>
               <div class="fm-share-sub">от ${this.esc(g.owner_name || g.owner)} ·
                 ${g.can_edit ? 'можно редактировать' : 'только просмотр'} ·
                 ${pending ? 'ожидает ответа' : 'доступ принят'}${g.missing ? ' · файл недоступен' : ''}</div>
             </div>
           </div>
           <div class="fm-share-item-btns"></div>`;
        const btns = row.querySelector('.fm-share-item-btns');
        const mk = (icon, title, cls, fn) => {
          const b = document.createElement('button');
          b.title = title;
          if (cls) b.className = cls;
          b.innerHTML = `<i class="fas fa-${icon}"></i>`;
          b.addEventListener('click', fn);
          btns.appendChild(b);
        };
        // Посмотреть, что за файл, можно ещё до принятия.
        if (!g.missing) {
          mk(pending ? 'eye' : 'folder-open', 'Посмотреть', '',
            () => this.openGrantViewer(g.id));
        }
        if (pending) {
          mk('check', 'Принять', '', async () => {
            try {
              const d = await this._shareApi('POST', '/share/respond', { id: g.id, accept: true });
              this.showNotification(d && d.shared_path
                ? 'Доступ принят — файл добавлен в папку Shared'
                : 'Доступ принят', 'success');
              // Файл уже в Shared: если он открыт в панели — обновляем
              // список, иначе пользователь не увидит новинку до F5.
              this.refreshSharedPanes();
              refresh();
            } catch (e) { this.showNotification(e.message, 'error'); }
          });
          mk('times', 'Отклонить', 'danger', async () => {
            try {
              await this._shareApi('POST', '/share/respond', { id: g.id, accept: false });
              this.showNotification('Доступ отклонён', 'success');
              refresh();
            } catch (e) { this.showNotification(e.message, 'error'); }
          });
        }
        if (!pending && g.shared_path) {
          mk('folder-open', 'Показать в папке Shared', '',
            () => this.navigate(this.state.activePane, g.shared_path.replace(/[\\/][^\\/]+$/, '')));
        }
        box.appendChild(row);
      });
    };

    const refresh = async () => {
      render(await this.checkIncomingShares(false));
    };
    modal.querySelector('#incReload').addEventListener('click', refresh);
    refresh();
  }

  async openGrantViewer(gid) {
    let meta;
    try {
      meta = (await this._shareApi('GET', '/grant/' + encodeURIComponent(gid) + '/meta')).grant;
    } catch (e) {
      this.showNotification(e.message, 'error');
      return;
    }
    const modal = this.showModal(
      `<i class="fas fa-${meta.is_dir ? 'folder' : 'file'}"></i> ${this.esc(meta.name)}`,
      `<div class="fm-gv">
         <div class="fm-share-file"><i class="fas fa-${meta.is_dir ? 'folder' : 'file'}"></i>
           <b>${this.esc(meta.name)}</b>
           <div class="fm-share-sub">${this.esc(meta.owner_name)} ·
             ${meta.can_edit ? 'редактирование' : 'только просмотр'} ·
             ${meta.is_dir ? 'папка' : this.formatSize(meta.size)}</div></div>
         <div class="fm-gv-path" id="gvPath"></div>
         <div class="fm-gv-body" id="gvBody">Загрузка…</div>
       </div>`, 'lg', 'grantViewer');
    const bodyEl = modal.querySelector('#gvBody');
    const pathEl = modal.querySelector('#gvPath');
    const nav = (sub) => meta.is_dir
      ? this._renderGrantDir(gid, sub, meta, bodyEl, pathEl, nav)
      : this._renderGrantFile(gid, '', meta, bodyEl, pathEl);
    await nav('');
  }

  async _renderGrantDir(gid, sub, meta, bodyEl, pathEl, nav) {
    const parts = String(sub || '').split('/').filter(Boolean);
    pathEl.innerHTML = '';
    const crumb = (label, target) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'fm-gv-crumb';
      b.textContent = label;
      b.addEventListener('click', () => nav(target));
      pathEl.appendChild(b);
    };
    crumb(meta.name, '');
    parts.forEach((part, i) => {
      const sep = document.createElement('span');
      sep.className = 'fm-gv-sep';
      sep.textContent = '/';
      pathEl.appendChild(sep);
      crumb(part, parts.slice(0, i + 1).join('/'));
    });

    bodyEl.innerHTML = '<div class="fm-share-empty">Загрузка…</div>';
    let d;
    try {
      d = await this._shareApi('GET', '/grant/' + encodeURIComponent(gid) +
        '/list?sub=' + encodeURIComponent(sub || ''));
    } catch (e) {
      bodyEl.innerHTML = `<div class="fm-share-empty">${this.esc(e.message)}</div>`;
      return;
    }
    const list = document.createElement('div');
    list.className = 'fm-gv-list';
    const entries = d.entries || [];
    if (!entries.length) {
      list.innerHTML = '<div class="fm-share-empty">Папка пуста.</div>';
    }
    entries.forEach(e => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'fm-gv-row';
      row.innerHTML = `<i class="fas fa-${e.is_dir ? 'folder' : 'file'}"></i>
        <span class="fm-gv-name">${this.esc(e.name)}</span>
        <span class="fm-gv-size">${e.is_dir ? '' : this.formatSize(e.size)}</span>`;
      const next = sub ? sub + '/' + e.name : e.name;
      row.addEventListener('click', () => e.is_dir
        ? nav(next)
        : this._renderGrantFile(gid, next, meta, bodyEl, pathEl));
      list.appendChild(row);
    });
    bodyEl.innerHTML = '';
    bodyEl.appendChild(list);
  }

  async _renderGrantFile(gid, sub, meta, bodyEl, pathEl) {
    const fname = sub ? sub.split('/').pop() : meta.name;
    pathEl.textContent = meta.name + (sub ? ' / ' + sub.split('/').join(' / ') : '');
    bodyEl.innerHTML = '<div class="fm-share-empty">Загрузка…</div>';

    const qs = new URLSearchParams();
    if (sub) qs.set('sub', sub);
    const base = '/filemanager/grant/' + encodeURIComponent(gid) + '/file';
    const url = base + (qs.toString() ? '?' + qs.toString() : '');
    const downloadUrl = url + (qs.toString() ? '&' : '?') + 'download=1';

    let res;
    try { res = await fetch(url, { cache: 'no-store' }); } catch (e) {
      bodyEl.innerHTML = '<div class="fm-share-empty">Не удалось открыть файл.</div>';
      return;
    }
    if (!res.ok) {
      bodyEl.innerHTML = '<div class="fm-share-empty">Файл недоступен — возможно, доступ отозван.</div>';
      return;
    }
    const ct = (res.headers.get('content-type') || '').toLowerCase();
    if (/^image\//.test(ct)) {
      bodyEl.innerHTML = `<div class="fm-gv-media"><img alt="" src="${this.esc(url)}"></div>`;
      return;
    }
    if (/^video\//.test(ct)) {
      bodyEl.innerHTML = `<div class="fm-gv-media"><video controls src="${this.esc(url)}"></video></div>`;
      return;
    }
    if (/^audio\//.test(ct)) {
      bodyEl.innerHTML = `<div class="fm-gv-media"><audio controls src="${this.esc(url)}"></audio></div>`;
      return;
    }
    if (ct === 'application/pdf') {
      bodyEl.innerHTML = `<iframe class="fm-gv-pdf" title="" src="${this.esc(url)}"></iframe>`;
      return;
    }
    const textLike = /^text\//.test(ct) || /(json|xml|javascript|csv|markdown)/.test(ct) ||
      /\.(txt|md|markdown|json|js|mjs|ts|py|html?|css|scss|csv|log|xml|ya?ml|ini|cfg|conf|sh|bat|ps1)$/i.test(fname);
    if (textLike) {
      let text = '';
      try { text = await res.text(); } catch (e) { text = ''; }
      const canEdit = !!meta.can_edit;
      bodyEl.innerHTML =
        `<textarea class="fm-gv-text" id="gvText" spellcheck="false" ${canEdit ? '' : 'readonly'}></textarea>
         <div class="fm-share-actions">
           ${canEdit
             ? '<button class="fm-btn primary" id="gvSave"><i class="fas fa-save"></i> Сохранить</button>'
             : '<span class="fm-share-hint">Только просмотр</span>'}
           <span class="fm-share-hint" id="gvHint"></span>
         </div>`;
      // Значение ставим через .value: текст файла не должен парситься как HTML.
      bodyEl.querySelector('#gvText').value = text;
      const save = bodyEl.querySelector('#gvSave');
      if (save) save.addEventListener('click', async () => {
        save.disabled = true;
        bodyEl.querySelector('#gvHint').textContent = 'Сохраняю…';
        try {
          await this._shareApi('POST', '/grant/' + encodeURIComponent(gid) + '/save',
            { sub, content: bodyEl.querySelector('#gvText').value });
          bodyEl.querySelector('#gvHint').textContent = 'Сохранено';
          this.showNotification('Изменения записаны в общий файл', 'success');
        } catch (e) {
          bodyEl.querySelector('#gvHint').textContent = e.message;
        } finally { save.disabled = false; }
      });
      return;
    }
    bodyEl.innerHTML =
      `<div class="fm-gv-download"><i class="fas fa-file"></i>
         <div>Предпросмотр для этого типа файла недоступен.</div>
         <a class="fm-btn primary" href="${this.esc(downloadUrl)}" target="_blank" rel="noopener">
           <i class="fas fa-download"></i> Скачать</a>
       </div>`;
  }

  // Кнопка на панели: поделиться выделенным файлом или папкой.
  // Ссылку на папку дать нельзя, но копию и доступ — можно.
  shareSelected(pane) {
    const sel = Array.from(this.state.selectedItems[pane] || []);
    if (!sel.length) { this.showNotification('Выберите файл или папку', 'warn'); return; }
    if (sel.length > 1) { this.showNotification('Выберите один файл или одну папку', 'warn'); return; }
    const item = (this.state.paneItems[pane] || []).find(i => i.path === sel[0]);
    this.openShareDialog(sel[0], !!(item && item.type === 'directory'));
  }

  // Загрузить каталог приложений (один раз за сессию).
  async loadApps() {
    if (this._appsLoaded) return this.state.apps || [];
    try {
      const r = await fetch('/api/apps');
      const list = await r.json();
      this.state.apps = Array.isArray(list) ? list : [];
    } catch (e) {
      this.state.apps = [];
    }
    this._appsLoaded = true;
    return this.state.apps;
  }

  // Найти приложение по его id (musicmachine, code-studio-3, ...).
  appById(id) {
    return (this.state.apps || []).find(a => a && a.id === id) || null;
  }

  // Приложение для категории файлов: video | audio | office | code.
  // id — реальные имена папок из defaultapps (их отдаёт /api/apps).
  appForKind(kind) {
    const map = { video: 'filmcraft-web-0.6.0', audio: 'soundcraft-web-0.5.0',
                  office: 'wordcraft-web-0.5.0', code: 'code-studio-3' };
    const id = map[kind];
    if (!id) return null;
    const app = this.appById(id);
    if (!app) return null;
    return { id: app.id, main: app.main || 'index.html', name: app.name || id };
  }

  /* Соответствие «расширение → приложение из defaultapps». id — имена папок,
     именно их возвращает сканер /api/apps. Порядок ids = приоритет: первый
     установленный и открывается по умолчанию. */
  static EXT_GROUPS = [
    // Документы. WordCraft — аналог Word; .txt/.log открываются и в нём,
    // и в редакторе кода.
    { exts: ['.docx', '.doc', '.odt'], ids: ['wordcraft-web-0.5.0'] },
    { exts: ['.rtf', '.txt', '.nfo', '.log'], ids: ['wordcraft-web-0.5.0', 'code-studio-3'] },
    { exts: ['.md', '.markdown'], ids: ['code-studio-3', 'wordcraft-web-0.5.0'] },
    { exts: ['.xlsx', '.xls', '.ods'], ids: ['gridcraft-web-0.5.0'] },
    { exts: ['.csv', '.tsv'], ids: ['gridcraft-web-0.5.0', 'code-studio-3'] },
    { exts: ['.pptx', '.ppt', '.odp'], ids: ['deckcraft-web-0.5.0'] },
    // PDF: PdfCraft — чтение/правка, MarkupCraft — разметка (Bluebeam),
    // TakeoffCraft — замеры и сметы.
    { exts: ['.pdf'], ids: ['pdfcraft-web-0.6.0',
      'markupcraft-browser-edition-v0.6.0', 'takeoffcraft-browser-edition-v0.6.0'] },
    // CAD/строительство. Новый CadCraft (v0.6, WASM) первым, старый — запасным.
    { exts: ['.dxf'], ids: ['cadcraft-browser-edition-v0.6.0', 'cadcraft-web-0.5.0',
      'crraftstudio', 'jumpstart-3d'] },
    { exts: ['.dwg'], ids: ['cadcraft-browser-edition-v0.6.0', 'cadcraft-web-0.5.0',
      'crraftstudio', 'bimcraft-browser-edition-v0.6.0',
      'civilcraft-browser-edition-v0.6.0',
      'detailcraft-browser-edition-v0.6.0', 'takeoffcraft-browser-edition-v0.6.0'] },
    { exts: ['.ifc', '.rvt'], ids: ['bimcraft-browser-edition-v0.6.0',
      'detailcraft-browser-edition-v0.6.0'] },
    { exts: ['.skp'], ids: ['modelcraft-browser-edition-v0.6.0'] },
    { exts: ['.step', '.stp', '.iges', '.igs', '.f3d'], ids: ['solidcraft-web-0.15.0',
      'partcraft-browser-edition-v0.6.0', 'modelcraft-browser-edition-v0.6.0'] },
    { exts: ['.sldprt', '.sldasm'], ids: ['partcraft-browser-edition-v0.6.0',
      'solidcraft-web-0.15.0'] },
    { exts: ['.stl', '.obj', '.3mf', '.ply'], ids: ['modelcraft-browser-edition-v0.6.0',
      'solidcraft-web-0.15.0', 'partcraft-browser-edition-v0.6.0'] },
    { exts: ['.svg', '.ai', '.eps'], ids: ['vectorcraft-web-0.10.0',
      'designcraft-web-0.6.0', 'code-studio-3'] },
    { exts: ['.fig', '.sketch', '.xd'], ids: ['figcraft-web-0.5.1',
      'designcraft-web-0.6.0'] },
    { exts: ['.psd', '.xcf'], ids: ['photocraft-web-0.7.0', 'lightcraft-web-0.6.0'] },
    { exts: ['.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf', '.orf', '.rw2', '.raw'],
      ids: ['lightcraft-web-0.6.0', 'photocraft-web-0.7.0'] },
    { exts: ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico', '.tiff', '.tif',
             '.heic', '.heif'], ids: ['photocraft-web-0.7.0', 'lightcraft-web-0.6.0'] },
    { exts: ['.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.wmv', '.flv'],
      ids: ['filmcraft-web-0.6.0'] },
    { exts: ['.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.wma', '.opus'],
      ids: ['soundcraft-web-0.5.0'] },
  ];

  // Группа приложений по расширению файла (или null).
  static groupForExt(ext) {
    ext = (ext || '').toLowerCase();
    return FileManagerApp.EXT_GROUPS.find(g => g.exts.includes(ext)) || null;
  }

  // Расширение пути в нижнем регистре ('.md'); '' — если расширения нет.
  static extOf(path) {
    const name = String(path || '').split(/[/\\]/).pop().toLowerCase();
    return name.includes('.') ? '.' + name.split('.').pop() : '';
  }

  // Тип встроенного просмотра для файла (null — своего просмотра нет).
  // Используется целью ассоциации «Открыть в Infinity».
  static viewerTypeFor(path) {
    const ext = FileManagerApp.extOf(path);
    if (['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico', '.tiff', '.tif', '.avif'].includes(ext)) return 'image';
    if (['.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.wmv', '.flv', '.ogv'].includes(ext)) return 'video';
    if (['.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.wma', '.opus'].includes(ext)) return 'audio';
    if (ext === '.pdf') return 'pdf';
    if (['.html', '.htm'].includes(ext)) return 'html';
    if (['.zip', '.tar', '.tgz', '.gz', '.bz2', '.7z', '.rar'].includes(ext)) return 'archive';
    return null;
  }

  // Приложение по расширению: сначала группа, затем редактор текста/кода.
  appForExt(ext, isText) {
    const g = FileManagerApp.groupForExt(ext);
    if (g) {
      for (const id of g.ids) {
        const a = this.appById(id);
        if (a) return { id: a.id, main: a.main || 'index.html', name: a.name || id };
      }
    }
    if (FileManagerApp.CODE_EXTENSIONS.has((ext || '').toLowerCase()) || isText) {
      const ed = this.appById('code-studio-3');
      if (ed) return { id: ed.id, main: ed.main || 'index.html', name: ed.name || ed.id };
    }
    return null;
  }

  static CODE_EXTENSIONS = new Set([
    '.html', '.htm', '.css', '.js', '.mjs', '.cjs', '.json', '.py', '.md',
    '.xml', '.svg', '.sh', '.bash', '.yml', '.yaml', '.toml', '.ini', '.cfg',
    '.ts', '.jsx', '.tsx', '.vue', '.svelte', '.php', '.rb', '.go', '.rs',
    '.java', '.kt', '.c', '.h', '.cpp', '.hpp', '.cs', '.swift', '.sql',
    '.env', '.gitignore', '.dockerfile', '.log', '.csv', '.bat', '.ps1',
    '.lua', '.pl', '.r', '.dart', '.scss', '.less', '.graphql',
  ]);

  async openFile(pane, item) {
    const path = item.path;
    const mt = item.media_type;
    const ext = (item.extension || '').toLowerCase();
    const isText = item.is_text;

    // Зашифрованный файл нельзя открыть напрямую — сначала расшифровка.
    if (ext === '.infenc') {
      const ok = await this.confirm('Файл зашифрован. Расшифровать его?');
      if (ok) await this.decryptSelected(pane, path);
      return;
    }

    // Сохранённая ассоциация главнее встроенных правил: пользователь явно
    // выбрал «открывать файлы этого типа всегда этим приложением».
    await this.loadAssociations();
    const assoc = this.assocFor(ext);
    if (assoc && await this.applyAssociation(assoc, path)) return;

    // .inf проверяем ПЕРЕД isText: сервер помечает .inf как текст (внутри JSON),
    // и без этой ветки он уезжал бы в редактор кода вместо запуска.
    if (ext === '.inf') {
      this.openInfFile(path);
      return;
    }

    const isArchive = ['.zip', '.tar', '.tgz', '.gz', '.bz2',
      '.7z', '.rar'].includes(ext);
    if (isArchive) {
      this.openFullscreen('archive', path);
      return;
    }

    // Дизайнерские изображения (PSD/XCF/SVG/AI/EPS/FIG/RAW) открываем профильным
    // приложением; обычные растровые картинки — встроенным просмотрщиком.
    const DESIGN_IMG = ['.psd', '.xcf', '.svg', '.ai', '.eps', '.fig', '.sketch',
      '.xd', '.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf', '.orf', '.rw2', '.raw'];
    if (mt === 'image' && !DESIGN_IMG.includes(ext)) {
      this.openFullscreen('image', path);
      return;
    }

    // Каталог приложений нужен один раз за сессию; пока грузится — не блокируем.
    this.loadApps().then(() => {
      // Конкретное приложение по расширению; иначе — по категории медиа.
      let app = this.appForExt(ext, isText);
      if (!app && (mt === 'video' || mt === 'audio')) app = this.appForKind(mt);
      if (!app) {
        // Приложения нет: известные форматы пробуем открыть системным способом,
        // всё остальное отдаём на скачивание, а не 404-ом.
        if (DESIGN_IMG.includes(ext) || FileManagerApp.CODE_EXTENSIONS.has(ext) || isText) {
          this.openSystemApp(path);
        } else {
          window.open(`/filemanager/download?path=${encodeURIComponent(path)}`, '_blank');
        }
        return;
      }
      this.openApp(app.id, path, app.main);
    });
  }

  // Кнопка «Открыть в приложении» — приложение и его точка входа берутся
  // из каталога /api/apps, а не из захардкоженной пары (id, entry).
  appToolbarButton(kind, icon, filePath) {
    const app = this.appForKind(kind);
    if (!app) return '';
    return `<button class="fm-btn" data-open-app="${this.esc(app.id)}" data-open-main="${this.esc(app.main)}" data-open-file="${this.esc(filePath)}">
      <i class="fas fa-${icon}"></i> Открыть в ${this.esc(app.name)}</button>`;
  }

  // Навесить обработчики на кнопки «Открыть в приложении» внутри окна.
  bindAppToolbarButtons(root) {
    root.querySelectorAll('button[data-open-app]').forEach(btn => {
      btn.addEventListener('click', () => {
        this.openApp(btn.dataset.openApp, btn.dataset.openFile, btn.dataset.openMain);
      });
    });
  }

  openFullscreen(type, path) {
    const modal = document.getElementById('fullscreenModal');
    const body = document.getElementById('fullscreenBody');
    const title = document.getElementById('fullscreenTitle');
    if (!modal || !body) return;

    body.innerHTML = '';
    const p = decodeURIComponent(path);
    const name = p.split(/[/\\]/).pop();

    const iconMap = { image:'file-image', video:'file-video', audio:'file-audio', pdf:'file-pdf', archive:'file-archive', html:'file-code' };
    title.innerHTML = `<i class="fas fa-${iconMap[type] || 'file'}"></i> ${this.esc(name)}`;

    if (type === 'image') {
      body.innerHTML = `<div style="display:flex;flex-direction:column;height:100%">
        <div style="display:flex;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border);flex-shrink:0" id="fmAppToolbar">
          ${this.appToolbarButton('code', 'laptop-code', p)}
        </div>
        <div style="display:flex;align-items:center;justify-content:center;flex:1;background:#000;border-radius:0 0 8px 8px;min-height:0">
          <img src="/filemanager/preview/image/${encodeURIComponent(p)}" style="max-width:100%;max-height:100%;object-fit:contain">
        </div>
      </div>`;
      this.bindAppToolbarButtons(body);
    } else if (type === 'video') {
      body.innerHTML = `<div style="display:flex;flex-direction:column;height:100%">
        <div style="display:flex;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border);flex-shrink:0" id="fmAppToolbar">
          ${this.appToolbarButton('video', 'play-circle', p)}
        </div>
        <div style="flex:1;min-height:0;display:flex;align-items:center;justify-content:center;background:#000;border-radius:0 0 8px 8px">
          <video controls autoplay style="width:100%;max-height:100%" src="/filemanager/preview/video/${encodeURIComponent(p)}"></video>
        </div>
      </div>`;
    } else if (type === 'audio') {
      body.innerHTML = `<div style="display:flex;flex-direction:column;height:100%">
        <div style="display:flex;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border);flex-shrink:0" id="fmAppToolbar">
          ${this.appToolbarButton('audio', 'headphones', p)}
        </div>
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;gap:20px;min-height:0">
          <div style="font-size:64px"><i class="fas fa-music"></i></div>
          <div style="font-size:18px;font-weight:600">${this.esc(name)}</div>
          <audio controls autoplay style="width:80%;max-width:500px" src="/filemanager/preview/audio/${encodeURIComponent(p)}"></audio>
        </div>
      </div>`;
      this.bindAppToolbarButtons(body);
    } else if (type === 'pdf') {
      body.innerHTML = `<iframe src="/filemanager/download?path=${encodeURIComponent(p)}" style="width:100%;height:90vh;border:none;border-radius:8px"></iframe>`;
    } else if (type === 'html') {
      body.innerHTML = `<div style="display:flex;flex-direction:column;height:100%">
        <div style="display:flex;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border);flex-shrink:0">
          <button class="fm-btn" onclick="FM.openApp('code-studio-3','${encodeURIComponent(p)}')"><i class="fas fa-code"></i> Редактировать в Code Studio 3</button>
          <button class="fm-btn" onclick="window.open('/filemanager/download?path=${encodeURIComponent(p)}','_blank')" title="Открыть в новой вкладке"><i class="fas fa-external-link-alt"></i> В браузере</button>
        </div>
        <iframe src="/filemanager/download?path=${encodeURIComponent(p)}" style="flex:1;border:none"></iframe>
      </div>`;
      document.getElementById('fullscreenSaveBtn').style.display = 'none';
    } else if (type === 'archive') {
      this.previewArchive(p, body);
    }

    modal.style.display = 'flex';
    const saveBtn = document.getElementById('fullscreenSaveBtn');
    if (type !== 'html') saveBtn.style.display = 'none';
    document.getElementById('closeFullscreenBtn').onclick = () => { modal.style.display = 'none'; };
  }

  // ─── TEXT EDITOR ────────────────────────────────────
  async openEditor(path) {
    const modal = document.getElementById('fullscreenModal');
    const body = document.getElementById('fullscreenBody');
    const title = document.getElementById('fullscreenTitle');
    const btn = document.getElementById('fullscreenSaveBtn');
    if (!modal || !body) return;

    const p = decodeURIComponent(path);
    const name = p.split(/[/\\]/).pop();
    title.innerHTML = `<i class="fas fa-edit"></i> ${this.esc(name)}`;

    try {
      const data = await this.api(`/read?path=${encodeURIComponent(p)}`);
      body.innerHTML = `<div id="editorContainer" style="height:calc(90vh - 60px);border-radius:8px;overflow:hidden"></div>`;

      if (typeof require !== 'undefined' && require.paths) {
        require.config({ paths: { vs: '/static/monaco/min/vs' } });
        require(['vs/editor/editor.main'], (monaco) => {
          const editor = monaco.editor.create(document.getElementById('editorContainer'), {
            value: data.content,
            language: this.getMonacoLanguage(p),
            theme: this.state.theme === 'dark' ? 'vs-dark' : 'vs',
            automaticLayout: true,
            minimap: { enabled: false },
            fontSize: 13,
            lineNumbers: 'on',
            scrollBeyondLastLine: false,
            wordWrap: 'on',
          });
          btn.style.display = 'inline-flex';
          btn.onclick = async () => {
            await this.apiJson('POST', '/write', { path: p, content: editor.getValue() });
            this.showNotification('Сохранено', 'success');
            modal.style.display = 'none';
          };
        });
      } else {
        body.innerHTML = `<textarea id="simpleEditor" class="fm-input" style="height:calc(90vh - 60px);font-family:var(--font-mono);font-size:13px;resize:none">${this.esc(data.content)}</textarea>`;
        btn.style.display = 'inline-flex';
        btn.onclick = async () => {
          const val = document.getElementById('simpleEditor')?.value || '';
          await this.apiJson('POST', '/write', { path: p, content: val });
          this.showNotification('Сохранено', 'success');
          modal.style.display = 'none';
        };
      }
    } catch (err) {
      body.innerHTML = `<p style="color:var(--red)">Ошибка: ${this.esc(err.message)}</p>`;
      btn.style.display = 'none';
    }

    modal.style.display = 'flex';
    document.getElementById('closeFullscreenBtn').onclick = () => { modal.style.display = 'none'; btn.style.display = 'none'; };
  }

  getMonacoLanguage(path) {
    const ext = path.split('.').pop().toLowerCase();
    const map = {
      py:'python', js:'javascript', ts:'typescript', html:'html', css:'css',
      json:'json', xml:'xml', md:'markdown', sql:'sql', sh:'shell', bat:'powershell',
      ps1:'powershell', cpp:'cpp', c:'c', java:'java', go:'go', rs:'rust',
      rb:'ruby', php:'php', yaml:'yaml', yml:'yaml', txt:'plaintext',
      cfg:'ini', conf:'ini', ini:'ini', toml:'ini', env:'ini',
    };
    return map[ext] || 'plaintext';
  }

  // ─── IMAGE EDITING ──────────────────────────────────
  async imageRotate(encPath, angle) {
    try {
      const path = decodeURIComponent(encPath);
      await this.apiJson('POST', '/image/rotate', { path, angle });
      this.showNotification(`Поворот на ${angle}° выполнен`, 'success');
      this.navigate(this.state.activePane, this.state.panePaths[this.state.activePane]);
    } catch (err) {
      this.showNotification('Ошибка: ' + err.message, 'error');
    }
  }

  async imageFilter(encPath, filterType) {
    try {
      const path = decodeURIComponent(encPath);
      await this.apiJson('POST', '/image/filter', { path, filter: filterType });
      this.showNotification(`Фильтр "${filterType}" применён`, 'success');
      this.navigate(this.state.activePane, this.state.panePaths[this.state.activePane]);
    } catch (err) {
      this.showNotification('Ошибка: ' + err.message, 'error');
    }
  }

  imageResizeDialog(encPath) {
    const html = `
      <div style="display:flex;flex-direction:column;gap:10px">
        <label>Ширина (px):</label>
        <input type="number" id="resizeW" class="fm-input" value="800" min="1">
        <label>Высота (px):</label>
        <input type="number" id="resizeH" class="fm-input" value="600" min="1">
      </div>`;
    const modal = this.showModal('<i class="fas fa-arrows-alt"></i> Изменить размер', html, 'sm');
    const btn = modal.querySelector('.fm-modal-footer .fm-btn.primary');
    if (btn) {
      btn.textContent = 'Применить';
      btn.onclick = async () => {
        const w = parseInt(document.getElementById('resizeW')?.value);
        const h = parseInt(document.getElementById('resizeH')?.value);
        if (!w || !h || w < 1 || h < 1) { this.showNotification('Введите размер', 'warn'); return; }
        try {
          const path = decodeURIComponent(encPath);
          await this.apiJson('POST', '/image/resize', { path, width: w, height: h });
          this.showNotification('Размер изменён', 'success');
          modal.style.display = 'none';
          this.navigate(this.state.activePane, this.state.panePaths[this.state.activePane]);
        } catch (err) {
          this.showNotification('Ошибка: ' + err.message, 'error');
        }
      };
    }
  }

  imageCropDialog(encPath) {
    const html = `
      <div style="display:flex;flex-direction:column;gap:10px">
        <label>Лево (px):</label>
        <input type="number" id="cropL" class="fm-input" value="0" min="0">
        <label>Верх (px):</label>
        <input type="number" id="cropT" class="fm-input" value="0" min="0">
        <label>Право (px):</label>
        <input type="number" id="cropR" class="fm-input" value="400" min="1">
        <label>Низ (px):</label>
        <input type="number" id="cropB" class="fm-input" value="300" min="1">
      </div>`;
    const modal = this.showModal('<i class="fas fa-crop-alt"></i> Обрезать', html, 'sm');
    const btn = modal.querySelector('.fm-modal-footer .fm-btn.primary');
    if (btn) {
      btn.textContent = 'Обрезать';
      btn.onclick = async () => {
        const l = parseInt(document.getElementById('cropL')?.value);
        const t = parseInt(document.getElementById('cropT')?.value);
        const r = parseInt(document.getElementById('cropR')?.value);
        const b = parseInt(document.getElementById('cropB')?.value);
        if (isNaN(l) || isNaN(t) || isNaN(r) || isNaN(b)) { this.showNotification('Введите координаты', 'warn'); return; }
        try {
          const path = decodeURIComponent(encPath);
          await this.apiJson('POST', '/image/crop', { path, left: l, top: t, right: r, bottom: b });
          this.showNotification('Изображение обрезано', 'success');
          modal.style.display = 'none';
          this.navigate(this.state.activePane, this.state.panePaths[this.state.activePane]);
        } catch (err) {
          this.showNotification('Ошибка: ' + err.message, 'error');
        }
      };
    }
  }

  // ─── MOVE TO DIALOG ─────────────────────────────────
  async moveToDialog(sourcePath, pane) {
    const sourceName = sourcePath.split(/[/\\]/).pop();
    const currentDir = this.state.panePaths[pane] || '';
    const html = `
      <div style="display:flex;flex-direction:column;gap:10px">
        <label>Переместить <b>${this.esc(sourceName)}</b> в:</label>
        <input type="text" id="moveTargetDir" class="fm-input" value="${this.esc(currentDir)}" placeholder="Путь к папке назначения">
        <div style="font-size:11px;color:var(--text3)">Введите путь к папке назначения</div>
      </div>`;
    const modal = this.showModal('<i class="fas fa-exchange-alt"></i> Переместить в...', html, 'sm');
    const btn = modal.querySelector('.fm-modal-footer .fm-btn.primary');
    if (btn) {
      btn.textContent = 'Переместить';
      btn.onclick = async () => {
        const targetDir = document.getElementById('moveTargetDir')?.value?.trim();
        if (!targetDir) { this.showNotification('Укажите папку назначения', 'warn'); return; }
        const destPath = targetDir.replace(/[/\\]$/, '') + (targetDir.includes('\\') ? '\\' : '/') + sourceName;
        try {
          this.pushUndo({ type: 'move', path: sourcePath, destPath: destPath, name: sourceName });
          await this.apiJson('POST', '/move', { source: sourcePath, destination: destPath });
          this.showNotification(`"${sourceName}" перемещён`, 'success');
          modal.style.display = 'none';
          this.navigate(pane, this.state.panePaths[pane]);
        } catch (err) {
          this.showNotification('Ошибка: ' + err.message, 'error');
        }
      };
    }
  }

  // ─── PROPERTIES ─────────────────────────────────────
  async showProperties(path) {
    try {
      const info = await this.api(`/properties?path=${encodeURIComponent(path)}`);
      const name = info.name;
      const size = info.type === 'directory' ? this.formatSize(info.total_size || 0) : this.formatSize(info.size);
      const files = info.total_files !== undefined ? `Файлов: ${info.total_files}, Папок: ${info.total_dirs}` : '';
      const disk = info.disk_usage ? `Свободно: ${this.formatSize(info.disk_usage.free)} из ${this.formatSize(info.disk_usage.total)} (${info.disk_usage.percent}%)` : '';
      const perms = info.mode ? info.mode : info.permissions ? `0${info.permissions}` : '';
      const html = `
        <div style="display:grid;grid-template-columns:auto 1fr;gap:10px 20px;font-size:13px">
          <div style="color:var(--text3)">Имя:</div><div>${this.esc(name)}</div>
          <div style="color:var(--text3)">Путь:</div><div style="word-break:break-all">${this.esc(info.path)}</div>
          <div style="color:var(--text3)">Размер:</div><div>${size}</div>
          <div style="color:var(--text3)">Тип:</div><div>${info.type === 'directory' ? 'Папка' : (info.mime_type || 'Файл')}</div>
          <div style="color:var(--text3)">Изменён:</div><div>${new Date(info.modified).toLocaleString()}</div>
          <div style="color:var(--text3)">Создан:</div><div>${new Date(info.created).toLocaleString()}</div>
          ${perms ? `<div style="color:var(--text3)">Права:</div><div>${perms}</div>` : ''}
          ${info.is_symlink ? `<div style="color:var(--text3)">Ссылка:</div><div>${this.esc(info.symlink_target)}</div>` : ''}
          ${files ? `<div style="color:var(--text3)">Содержит:</div><div>${files}</div>` : ''}
          ${disk ? `<div style="color:var(--text3)">Диск:</div><div>${disk}</div>` : ''}
        </div>
        <div style="margin-top:16px;font-size:11px;color:var(--text3);text-align:center"><i class="fas fa-microchip"></i> Infinity File Manager</div>`;
      this.showModal(`<i class="fas fa-clipboard-list"></i> Свойства`, html);
    } catch (err) {
      this.showNotification(err.message, 'error');
    }
  }

  // ─── SEARCH ─────────────────────────────────────────
  async doSearch(query, mode = 'name') {
    if (!query || query.length < 1) { this.showNotification('Введите запрос', 'warn'); return; }
    const activePane = this.state.activePane;
    const rootPath = this.state.panePaths[activePane] || '';
    try {
      let data;
      if (mode === 'content') {
        data = await this.apiJson('POST', '/search-content', { path: rootPath, query, max_results: 100 });
      } else {
        data = await this.api(`/search?path=${encodeURIComponent(rootPath)}&query=${encodeURIComponent(query)}&max_results=200`);
      }
      const pane = this.state.activePane;
      if (mode === 'content' && data.items) {
        this.state.paneItems[pane] = data.items.map(item => ({
          name: item.name,
          path: item.path,
          type: item.type || 'file',
          size: 0,
          modified: null,
          extension: item.name.includes('.') ? '.' + item.name.split('.').pop() : '',
          is_text: true,
          _searchLine: item.line,
          _searchContent: item.content,
        }));
      } else {
        this.state.paneItems[pane] = data.items || [];
      }
      this.state.selectedItems[pane].clear();
      const modeLabel = mode === 'content' ? 'в содержимом' : 'по имени';
      this.state.panePaths[pane] = `Поиск ${modeLabel}: "${query}" (${data.total})`;
      this.render(pane);
      this.renderStatusBar();
    } catch (err) {
      this.showNotification(err.message, 'error');
    }
  }

  // ─── UNDO / REDO ────────────────────────────────────
  pushUndo(action) {
    this.state.undoStack.push(action);
    if (this.state.undoStack.length > 20) this.state.undoStack.shift();
    this.state.redoStack = [];
  }

  async undo() {
    if (this.state.undoStack.length === 0) {
      this.showNotification('Нечего отменять', 'info');
      return;
    }
    const action = this.state.undoStack.pop();
    try {
      if (action.type === 'rename') {
        await this.apiJson('POST', '/rename', { source: action.newPath, new_name: action.oldName });
        this.state.redoStack.push(action);
      } else if (action.type === 'delete') {
        if (action.isDir) {
          await this.apiJson('POST', '/mkdir', { path: action.path.replace(/[/\\][^/\\]+$/, ''), name: action.path.split(/[/\\]/).pop() });
        } else {
          await this.apiJson('POST', '/write', { path: action.path, content: action.content || '' });
        }
        this.state.redoStack.push(action);
      } else if (action.type === 'move') {
        await this.apiJson('POST', '/move', { source: action.destPath, destination: action.path });
        this.state.redoStack.push(action);
      } else if (action.type === 'copy') {
        await this.apiJson('POST', '/delete', { paths: [action.destPath], recursive: true });
        this.state.redoStack.push(action);
      }
      this.navigate(this.state.activePane, this.state.panePaths[this.state.activePane]);
      this.showNotification('Действие отменено', 'info');
    } catch (err) {
      this.showNotification('Ошибка отмены: ' + err.message, 'error');
    }
  }

  async redo() {
    if (this.state.redoStack.length === 0) {
      this.showNotification('Нечего повторить', 'info');
      return;
    }
    const action = this.state.redoStack.pop();
    try {
      if (action.type === 'rename') {
        await this.apiJson('POST', '/rename', { source: action.path, new_name: action.newName });
        this.state.undoStack.push(action);
      } else if (action.type === 'move') {
        await this.apiJson('POST', '/move', { source: action.path, destination: action.destPath });
        this.state.undoStack.push(action);
      } else if (action.type === 'copy') {
        await this.apiJson('POST', '/copy', { source: action.path, destination: action.destPath });
        this.state.undoStack.push(action);
      } else if (action.type === 'delete') {
        await this.apiJson('POST', '/delete', { paths: [action.path], recursive: true });
        this.state.undoStack.push(action);
      }
      this.navigate(this.state.activePane, this.state.panePaths[this.state.activePane]);
      this.showNotification('Действие повторено', 'info');
    } catch (err) {
      this.showNotification('Ошибка повтора: ' + err.message, 'error');
    }
  }

  // ─── LAYOUT & VIEW ──────────────────────────────────
  toggleRibbon() {
    const ribbon = document.querySelector('.fm-ribbon');
    if (ribbon) ribbon.classList.toggle('collapsed');
    const active = ribbon && ribbon.classList.contains('collapsed');
    document.getElementById('ribbonToggle')?.classList.toggle('active', active);
  }

  toggleLayout() {
    const newLayout = this.state.layout === 'single' ? 'dual' : 'single';
    this.state.layout = newLayout;
    const pane2 = document.querySelector('.fm-pane[data-pane="2"]');
    if (pane2) pane2.style.display = newLayout === 'dual' ? 'flex' : 'none';
    if (newLayout === 'dual' && !this.state.panePaths[2]) this.navigate(2, this.state.panePaths[1]);
    this.renderStatusBar();
    localStorage.setItem('fm_layout', newLayout);
  }

  toggleViewMode(pane, mode) {
    this.state.viewModes[pane] = mode;
    this.render(pane);
  }

  setSort(field) {
    if (this.state.sortBy === field) this.state.sortAsc = !this.state.sortAsc;
    else { this.state.sortBy = field; this.state.sortAsc = true; }
    this.render(this.state.activePane);
  }

  toggleHidden() {
    this.state.showHidden = !this.state.showHidden;
    this.navigate(this.state.activePane, this.state.panePaths[this.state.activePane]);
  }

  toggleDualSync() {
    this.state.dualSync = !this.state.dualSync;
    this.showNotification(this.state.dualSync ? 'Синхронизация панелей включена' : 'Синхронизация выключена', 'info');
  }

  toggleCheckboxMode() {
    this.state.checkboxMode = !this.state.checkboxMode;
    localStorage.setItem('fm_checkbox', this.state.checkboxMode);
    this.render(this.state.activePane);
  }

  setFilter(filter) {
    this.state.fileFilter = filter;
    this.navigate(this.state.activePane, this.state.panePaths[this.state.activePane]);
  }

  toggleFullscreen() {
    this.state.fullscreen = !this.state.fullscreen;
    const toolbar = document.querySelector('.fm-ribbon');
    const statusbar = document.querySelector('.fm-statusbar');
    const tree = document.getElementById('fmTree');
    const preview = document.getElementById('previewPanel');
    if (this.state.fullscreen) {
      if (toolbar) toolbar.style.display = 'none';
      if (statusbar) statusbar.style.display = 'none';
      if (tree) tree.classList.add('collapsed');
      if (preview) preview.classList.add('collapsed');
    } else {
      if (toolbar) toolbar.style.display = '';
      if (statusbar) statusbar.style.display = '';
    }
  }

  // ─── FAVORITES ──────────────────────────────────────
  toggleFavorite(path) {
    const idx = this.state.favorites.indexOf(path);
    if (idx >= 0) this.state.favorites.splice(idx, 1);
    else this.state.favorites.push(path);
    localStorage.setItem('fm_favorites', JSON.stringify(this.state.favorites));
    this.renderTree();
  }

  // ─── TABS ───────────────────────────────────────────
  addTab(pane) {
    this.state.tabs[pane].push({ path: '', name: 'Новая вкладка' });
    this.state.activeTab[pane] = this.state.tabs[pane].length - 1;
    this.renderTabs(pane);
    this.navigate(pane, this.state.panePaths[pane]);
  }

  closeTab(pane, index) {
    if (this.state.tabs[pane].length <= 1) return;
    this.state.tabs[pane].splice(index, 1);
    if (this.state.activeTab[pane] >= this.state.tabs[pane].length)
      this.state.activeTab[pane] = this.state.tabs[pane].length - 1;
    this.renderTabs(pane);
    if (this.state.tabs[pane][this.state.activeTab[pane]])
      this.navigate(pane, this.state.tabs[pane][this.state.activeTab[pane]].path);
  }

  switchTab(pane, index) {
    this.state.activeTab[pane] = index;
    this.renderTabs(pane);
    this.navigate(pane, this.state.tabs[pane][index].path);
  }

  updateTabTitle(pane) {
    const tabs = this.state.tabs[pane];
    const idx = this.state.activeTab[pane];
    if (tabs && tabs[idx]) {
      const path = this.state.panePaths[pane];
      tabs[idx].path = path;
      tabs[idx].name = path.split(/[/\\]/).filter(Boolean).pop() || 'Корень';
      this.renderTabs(pane);
    }
  }

  renderTabs(pane) {
    const el = document.getElementById('titlebarTabs');
    if (!el) return;
    el.dataset.pane = pane;
    const tabs = this.state.tabs[pane] || [];
    const active = this.state.activeTab[pane];
    if (tabs.length === 0) {
      tabs.push({ path: '', name: 'Главная' });
      this.state.activeTab[pane] = 0;
    }
    el.innerHTML = tabs.map((t, i) =>
      `<div class="fm-titlebar-tab${i === active ? ' active' : ''}" data-idx="${i}">
        <i class="fas fa-folder" style="color:var(--folder-grad-2);font-size:13px"></i>
        <span>${this.esc(t.name || 'Новая')}</span>
        ${tabs.length > 1 ? `<span class="fm-pane-tab-close" data-idx="${i}"><i class="fas fa-times"></i></span>` : ''}
      </div>`
    ).join('') + `<div class="fm-titlebar-newtab" title="Новая вкладка"><i class="fas fa-plus"></i></div>`;

    el.querySelectorAll('.fm-titlebar-tab').forEach(tab => {
      tab.addEventListener('click', (e) => {
        if (e.target.closest('.fm-pane-tab-close')) {
          this.closeTab(pane, parseInt(e.target.closest('.fm-pane-tab-close').dataset.idx));
        } else {
          this.switchTab(pane, parseInt(tab.dataset.idx));
        }
      });
    });

    const newTabBtn = el.querySelector('.fm-titlebar-newtab');
    if (newTabBtn) {
      newTabBtn.addEventListener('click', () => this.addTab(pane));
    }
  }

  // ─── TREE SIDEBAR ───────────────────────────────────
  async loadDisks() {
    try {
      const data = await this.api('/disks');
      const el = document.getElementById('fmDrives');
      if (!el) return;
      el.innerHTML = data.disks.map(d => {
        const pct = d.percent || 0;
        const color = pct > 90 ? 'var(--red)' : pct > 70 ? 'var(--yellow)' : 'var(--green)';
        return `<div class="fm-drive-item" data-path="${d.name}">
          <i class="fas fa-hdd"></i>
          <span style="flex:1">${d.label || d.name}</span>
          <span style="font-size:10px;color:var(--text3)">${pct}%</span>
          <div class="fm-drive-bar"><div class="fm-drive-bar-fill" style="width:${pct}%;background:${color}"></div></div>
        </div>`;
      }).join('');
      el.querySelectorAll('.fm-drive-item').forEach(el2 => {
        el2.addEventListener('click', () => this.navigate(1, el2.dataset.path));
      });
    } catch {}
  }

  async renderTree() {
    const el = document.getElementById('fmTreeContent');
    if (!el) return;
    const home = this.state.panePaths[this.state.activePane] || '';
    const parts = home.replace(/\\/g, '/').split('/').filter(Boolean);
    let html = '';

    // Recent locations
    if (this.state.recentPaths.length > 0) {
      html += `<div class="fm-tree-label-header"><i class="fas fa-clock"></i> НЕДАВНИЕ</div>`;
      const shown = this.state.recentPaths.slice(0, 5);
      for (const rp of shown) {
        const name = rp.split(/[/\\]/).filter(Boolean).pop() || rp;
        const active = rp === home ? ' active' : '';
        html += `<div class="fm-tree-item${active}" data-path="${this.escAttr(rp)}">
          <span class="fm-tree-icon"><i class="fas fa-history"></i></span>
          <span class="fm-tree-label">${this.esc(name)}</span>
        </div>`;
      }
    }

    // Favorites
    if (this.state.favorites.length > 0) {
      html += `<div class="fm-tree-label-header"><i class="fas fa-star"></i> ИЗБРАННОЕ</div>`;
      for (const fav of this.state.favorites) {
        const name = fav.split(/[/\\]/).pop() || fav;
        const active = fav === home ? ' active' : '';
        html += `<div class="fm-tree-item${active}" data-path="${this.escAttr(fav)}">
          <span class="fm-tree-icon"><i class="fas fa-star"></i></span>
          <span class="fm-tree-label">${this.esc(name)}</span>
        </div>`;
      }
    }

    // Quick access
    html += `<div class="fm-tree-label-header"><i class="fas fa-bolt"></i> БЫСТРЫЙ ДОСТУП</div>`;
    const qa = this.state.platformInfo?.quick_access || {};
    // Показываем только те папки, которые сервер подтвердил как
    // существующие. Раньше список был фиксированным, и на переносимой
    // сборке пять пунктов «Быстрый доступ» вели в несуществующие
    // каталоги — клик давал «Файл ... недоступен».
    const qaMeta = [
      { label: 'Рабочий стол', key: 'desktop', icon: 'desktop' },
      { label: 'Документы', key: 'documents', icon: 'file-alt' },
      { label: 'Загрузки', key: 'downloads', icon: 'download' },
      { label: 'Изображения', key: 'pictures', icon: 'image' },
      { label: 'Музыка', key: 'music', icon: 'music' },
      { label: 'Видео', key: 'videos', icon: 'video' },
    ];
    const quickItems = [{ label: 'Главная', path: '', icon: 'home' }]
      .concat(qaMeta.filter(q => qa[q.key]).map(q => ({ label: q.label, path: qa[q.key], icon: q.icon })));
    // Shared — папка, куда попадают принятые файлы и копии от других
    // профилей. Пункт постоянный: она создаётся на сервере при открытии
    // менеджера, даже если получателю ещё ничего не присылали.
    if (this.state.profileRoot) {
      const sep = this.state.profileRoot.includes('\\') ? '\\' : '/';
      quickItems.push({
        label: 'Shared',
        path: this.state.profileRoot.replace(/[\\/]+$/, '') + sep + 'Shared',
        icon: 'share-alt'
      });
    }
    for (const q of quickItems) {
      const active = home.startsWith(q.path) ? ' active' : '';
      html += `<div class="fm-tree-item${active}" data-path="${q.path}">
        <span class="fm-tree-icon"><i class="fas fa-${q.icon}"></i></span>
        <span class="fm-tree-label">${q.label}</span>
      </div>`;
    }

    // Корзина. Сколько в ней файлов — считаем при отрисовке дерева,
    // иначе в боковой панели всегда стоял бы пустой пункт «Корзина»,
    // и непонятно, работает ли удаление вообще.
    {
      const t = this.state.trash || {};
      const n = t.total || 0;
      const active = this.isTrashPath(home) ? ' active' : '';
      html += `<div class="fm-tree-item fm-tree-trash${active}" data-path="${this.escAttr(t.trash_dir || '')}" data-trash="1" title="${t.trash_dir ? this.escAttr(t.trash_dir) : 'Корзина'}">
        <span class="fm-tree-icon"><i class="fas fa-trash-restore"></i></span>
        <span class="fm-tree-label">Корзина</span>
        ${n ? `<span class="fm-tree-badge">${n}</span>` : ''}
      </div>`;
    }

    // Current path with expandable tree
    if (home) {
      html += `<div class="fm-tree-label-header" style="margin-top:4px"><i class="fas fa-location-arrow"></i> ТЕКУЩАЯ</div>`;
      html += this.buildTreeItems(home, 0);
    }

    el.innerHTML = html;
    el.querySelectorAll('.fm-tree-item').forEach(el2 => {
      el2.addEventListener('click', () => {
        if (el2.dataset.trash) return this.openTrash();
        this.navigate(this.state.activePane, el2.dataset.path);
      });
    });
    el.querySelectorAll('.fm-tree-toggle').forEach(el2 => {
      el2.addEventListener('click', async (e) => {
        e.stopPropagation();
        const path = el2.dataset.path;
        const children = el2.parentElement.nextElementSibling;
        if (children && children.classList.contains('fm-tree-children')) {
          children.style.display = children.style.display === 'none' ? '' : 'none';
          el2.innerHTML = children.style.display === 'none' ? '<i class="fas fa-chevron-right"></i>' : '<i class="fas fa-chevron-down"></i>';
        } else {
          await this.expandTreeFolder(path, el2.parentElement);
        }
      });
    });
  }

  async expandTreeFolder(path, parentEl) {
    try {
      // Prevent double expansion
      if (parentEl.querySelector('.fm-tree-children')) return;
      const data = await this.api(`/list?path=${encodeURIComponent(path)}`);
      if (!data.items || data.items.length === 0) return;
      const children = document.createElement('div');
      children.className = 'fm-tree-children';
      for (const item of data.items) {
        if (item.type !== 'directory') continue;
        if (!this.state.showHidden && item.name.startsWith('.')) continue;
        const sub = document.createElement('div');
        sub.className = 'fm-tree-item';
        sub.dataset.path = item.path;
        sub.innerHTML = `<span class="fm-tree-toggle" data-path="${this.escAttr(item.path)}"><i class="fas fa-chevron-right"></i></span>
          <span class="fm-tree-icon"><i class="fas fa-folder"></i></span>
          <span class="fm-tree-label">${this.esc(item.name)}</span>`;
        sub.addEventListener('click', () => this.navigate(this.state.activePane, sub.dataset.path));
        sub.querySelector('.fm-tree-toggle').addEventListener('click', async (e) => {
          e.stopPropagation();
          const ch = sub.querySelector('.fm-tree-children');
          if (ch) {
            ch.style.display = ch.style.display === 'none' ? '' : 'none';
          } else {
            await this.expandTreeFolder(item.path, sub);
          }
        });
        children.appendChild(sub);
      }
      parentEl.appendChild(children);
      const toggle = parentEl.querySelector('.fm-tree-toggle');
      if (toggle) toggle.innerHTML = '<i class="fas fa-chevron-down"></i>';
    } catch {}
  }

  buildTreeItems(path, depth) {
    if (depth > 2) return ''; // limit depth
    const name = path.split(/[/\\]/).filter(Boolean).pop() || path;
    return `<div class="fm-tree-item active" data-path="${this.escAttr(path)}">
      <span class="fm-tree-toggle" data-path="${this.escAttr(path)}"><i class="fas fa-chevron-right"></i></span>
      <span class="fm-tree-icon"><i class="fas fa-folder-open"></i></span>
      <span class="fm-tree-label">${this.esc(name)}</span>
    </div>`;
  }

  // ─── TERMINAL ───────────────────────────────────────
  async openTerminal() {
    const cwd = this.state.panePaths[this.state.activePane] || '';
    const html = `<div class="fm-terminal" id="fmTerminal">
      <div class="fm-terminal-output">Infinity Terminal — ${this.esc(cwd)}<br>Введите команду и нажмите Enter</div>
      <div class="fm-terminal-input-line">
        <span class="fm-terminal-prompt">$</span>
        <input class="fm-terminal-input" id="fmTerminalInput" autofocus>
      </div>
    </div>`;
    this.showModal('<i class="fas fa-terminal"></i> Терминал', html, 'lg');
    const input = document.getElementById('fmTerminalInput');
    if (input) {
      input.addEventListener('keydown', async (e) => {
        if (e.key === 'Enter') {
          const cmd = input.value;
          if (!cmd.trim()) return;
          const term = document.getElementById('fmTerminal');
          if (!term) return;
          term.innerHTML += `<div><span style="color:var(--green)">$</span> ${this.esc(cmd)}</div>`;
          input.value = '';
          try {
            const res = await this.apiJson('POST', '/terminal', { command: cmd, cwd });
            const out = (res.stdout || '') + (res.stderr ? `<span style="color:var(--red)">${this.esc(res.stderr)}</span>` : '');
            term.innerHTML += `<div class="fm-terminal-output">${out || '(пусто)'}</div>`;
          } catch (err) {
            term.innerHTML += `<div class="fm-terminal-error">${this.esc(err.message)}</div>`;
          }
          term.innerHTML += `<div class="fm-terminal-input-line">
            <span class="fm-terminal-prompt">$</span>
            <input class="fm-terminal-input" id="fmTerminalInput" autofocus>
          </div>`;
          term.scrollTop = term.scrollHeight;
          document.getElementById('fmTerminalInput')?.focus();
        }
      });
    }
  }

  // ─── BATCH RENAME ───────────────────────────────────
  async openBatchRename() {
    const pane = this.state.activePane;
    const path = this.state.panePaths[pane];
    const html = `
      <div style="display:flex;flex-direction:column;gap:12px">
        <label><i class="fas fa-folder"></i> Папка: <strong>${this.esc(path)}</strong></label>
        <label>Что заменить:</label>
        <input class="fm-input" id="batchPattern" placeholder="например, old_text">
        <label>На что заменить:</label>
        <input class="fm-input" id="batchReplace" placeholder="например, new_text">
        <label><input type="checkbox" id="batchRegex"> Регулярное выражение</label>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="fm-btn" onclick="document.getElementById('batchModal').closest('.fm-modal-overlay').style.display='none'">Отмена</button>
          <button class="fm-btn primary" onclick="FM.doBatchRename()">Переименовать</button>
        </div>
      </div>`;
    this.showModal('<i class="fas fa-i-cursor"></i> Пакетное переименование', html, 'sm', 'batchModal');
  }

  async doBatchRename() {
    const pane = this.state.activePane;
    const path = this.state.panePaths[pane];
    const pattern = document.getElementById('batchPattern')?.value;
    const replace = document.getElementById('batchReplace')?.value || '';
    const regex = document.getElementById('batchRegex')?.checked || false;
    if (!pattern) { this.showNotification('Введите шаблон', 'warn'); return; }
    try {
      const data = await this.apiJson('POST', '/batch/rename', { path, pattern, replace, regex });
      this.showNotification(`Переименовано ${data.changed} из ${data.total}`, 'success');
      document.getElementById('batchModal')?.closest('.fm-modal-overlay')?.remove();
      this.navigate(pane, path);
    } catch (err) {
      this.showNotification(err.message, 'error');
    }
  }

  // ─── THEME ──────────────────────────────────────────
  // ─── ТЕМА ────────────────────────────────────────────
  // Тема теперь ОБЩАЯ с остальным приложением (static/js/theme.js).
  // Раньше файловый менеджер жил по своим правилам и отличался от всего
  // остального сразу по трём причинам:
  //   * держал отдельный ключ localStorage 'fm_theme' — смена темы на
  //     главной странице сюда не доходила;
  //   * ставил data-theme на <body>, тогда как theme.js ставит его на
  //     <html>: правило на <body> всегда выигрывает у правила на <html>
  //     для потомков, то есть ФМ перекрывал общую тему;
  //   * делал `document.body.className = theme`, затирая классы fm-embedded
  //     и pg-filemanager, из-за чего в файловом менеджере на рабочем столе
  //     не применялись компактные стили.
  setTheme(theme, persist = true) {
    if (!theme) return;
    this.state.theme = theme;
    if (persist) {
      // Пишем через общий модуль — иначе тема разъедется с остальным
      // приложением снова.
      if (window.InfinityTheme && typeof window.InfinityTheme.set === 'function') {
        window.InfinityTheme.set(theme);
      } else {
        localStorage.setItem('infinity_theme', theme);
        document.documentElement.setAttribute('data-theme', theme);
      }
    }
    // Атрибут — на documentElement, как это делает theme.js.
    document.documentElement.setAttribute('data-theme', theme);
    // Отдельный атрибут для собственных палитр файлового менеджера.
    // Сервер дописывает в страницу design.css ПОСЛЕ file_manager.css, и он
    // объявляет те же --bg/--text/--accent; без своего признака файловый
    // менеджер получал бы смесь двух палитр. См. file_manager.css.
    document.documentElement.setAttribute('data-fm-theme', theme);
    this._syncThemeSelectors(theme);
  }

  _syncThemeSelectors(theme) {
    document.querySelectorAll('[data-fm-theme-select]').forEach(sel => {
      sel.value = theme;
    });
  }

  // Подстроить файловый менеджер под общую тему: подписаться на её смену
  // (в этой вкладке и в других) и взять текущую.
  bindGlobalTheme() {
    if (window.InfinityTheme) {
      try {
        window.InfinityTheme.onChange((t) => this.setTheme(t, false));
        this.setTheme(window.InfinityTheme.get(), false);
      } catch (e) { /* тема — украшение */ }
    }
    // Другая вкладка сменила тему — подхватываем.
    window.addEventListener('storage', (e) => {
      if (e.key === 'infinity_theme' || e.key === null) {
        this.setTheme(localStorage.getItem('infinity_theme') || 'light', false);
      }
    });
    this.fillThemeSelectors();
  }

  // Наполнить списки выбора темы всеми темами приложения.
  fillThemeSelectors() {
    const themes = (window.InfinityTheme && window.InfinityTheme.themes) ||
      ['light', 'dark', 'sepia', 'warm', 'paper', 'ocean', 'forest', 'midnight', 'rose'];
    const labels = {
      light: 'Светлая', dark: 'Тёмная', sepia: 'Сепия', warm: 'Тёплая',
      paper: 'Бумага', ocean: 'Океан', forest: 'Лес', midnight: 'Полночь', rose: 'Роза',
    };
    document.querySelectorAll('[data-fm-theme-select]').forEach(sel => {
      if (sel.dataset.filled === '1') return;
      sel.innerHTML = themes.map(t =>
        `<option value="${t}">${labels[t] || t}</option>`).join('');
      sel.dataset.filled = '1';
    });
    this._syncThemeSelectors(this.state.theme);
  }

  toggleTheme() {
    const themes = (window.InfinityTheme && window.InfinityTheme.themes) ||
      ['light', 'dark', 'sepia'];
    const idx = themes.indexOf(this.state.theme);
    this.setTheme(themes[(idx + 1) % themes.length]);
  }

  // ─── PROGRESS ───────────────────────────────────────
  showProgress(msg, pct = 0) {
    const bar = document.getElementById('progressBar');
    if (bar) {
      bar.style.display = 'block';
      bar.querySelector('.fm-progress-bar').style.width = pct + '%';
    }
    const id = 'op_' + Date.now();
    this.state.operations.push(id);
    return id;
  }

  hideProgress(id) {
    this.state.operations = this.state.operations.filter(o => o !== id);
    if (this.state.operations.length === 0) {
      const bar = document.getElementById('progressBar');
      if (bar) bar.style.display = 'none';
    }
  }

  // ─── MODALS ─────────────────────────────────────────
  showModal(title, bodyHtml, size = '', id = '') {
    const overlay = document.createElement('div');
    overlay.className = 'fm-modal-overlay';
    overlay.id = id || 'modal_' + Date.now();
    const sizeClass = size ? `fm-modal-${size}` : '';
    overlay.style.display = 'flex';
    overlay.innerHTML = `<div class="fm-modal ${sizeClass}">
      <div class="fm-modal-header">
        <h3>${title}</h3>
        <button class="fm-close-btn" onclick="this.closest('.fm-modal-overlay').remove()"><i class="fas fa-times"></i></button>
      </div>
      <div class="fm-modal-body">${bodyHtml}</div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
    return overlay;
  }

  showNotification(msg, type = 'info') {
    const existing = document.querySelector('.fm-notification');
    if (existing) existing.remove();
    const el = document.createElement('div');
    el.className = 'fm-notification';
    const iconMap = { info:'info-circle', success:'check-circle', error:'exclamation-circle', warn:'exclamation-triangle' };
    const colors = { info: 'var(--accent)', success: 'var(--green)', error: 'var(--red)', warn: 'var(--yellow)' };
    el.style.cssText = `position:fixed;bottom:40px;right:20px;background:var(--bg2);border:1px solid var(--border);border-left:4px solid ${colors[type] || colors.info};border-radius:var(--radius);padding:12px 20px;z-index:9999;box-shadow:var(--shadow-lg);animation:fmSlideUp 0.2s ease;font-size:13px;max-width:400px;display:flex;align-items:center;gap:8px;`;
    el.innerHTML = `<i class="fas fa-${iconMap[type] || iconMap.info}"></i> ${msg}`;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  prompt(msg, defaultValue = '') {
    return new Promise(resolve => {
      const html = `
        <div style="display:flex;flex-direction:column;gap:12px">
          <label>${msg}</label>
          <input class="fm-input" id="promptInput" value="${this.esc(defaultValue)}" autofocus>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="fm-btn" id="promptCancel">Отмена</button>
            <button class="fm-btn primary" id="promptOk">OK</button>
          </div>
        </div>`;
      const modal = this.showModal('', html, 'sm');
      const input = document.getElementById('promptInput');
      input?.focus();
      input?.select();
      const close = (val) => { modal.remove(); resolve(val); };
      document.getElementById('promptOk')?.addEventListener('click', () => close(input?.value || ''));
      document.getElementById('promptCancel')?.addEventListener('click', () => close(''));
      input?.addEventListener('keydown', (e) => { if (e.key === 'Enter') close(input.value); if (e.key === 'Escape') close(''); });
    });
  }

  confirm(msg) {
    return new Promise(resolve => {
      const html = `
        <div style="display:flex;flex-direction:column;gap:16px">
          <div style="font-size:15px">${msg}</div>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="fm-btn" id="confirmCancel">Отмена</button>
            <button class="fm-btn primary" id="confirmOk" style="background:var(--red)">OK</button>
          </div>
        </div>`;
      const modal = this.showModal('', html, 'sm');
      const close = (val) => { modal.remove(); resolve(val); };
      document.getElementById('confirmOk')?.addEventListener('click', () => close(true));
      document.getElementById('confirmCancel')?.addEventListener('click', () => close(false));
    });
  }

  // ─── UTILS ──────────────────────────────────────────
  formatSize(b) {
    if (b === 0 || b == null) return '0 B';
    const k = 1024, s = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(Math.floor(Math.log(Math.abs(b)) / Math.log(k)), s.length - 1);
    return parseFloat((b / Math.pow(k, i)).toFixed(1)) + ' ' + s[i];
  }

  esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[m]);
  }

  escAttr(s) {
    return this.esc(s).replace(/"/g, '&quot;');
  }

  // ─── KEYBOARD ───────────────────────────────────────
  handleKeyboard(e) {
    const pane = this.state.activePane;
    const ctrl = e.ctrlKey || e.metaKey;
    const key = e.key;
    const tag = e.target?.tagName;
    const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target?.isContentEditable;

    if (isInput) {
      if (key === 'Escape') {
        e.target.blur();
        e.preventDefault();
        return;
      }
      // Allow Ctrl+C/X/V for file operations when no text is selected in input
      if (ctrl && ['c','x','v'].includes(key.toLowerCase())) {
        const input = e.target;
        const hasSelection = input.selectionStart !== input.selectionEnd;
        if (hasSelection) return; // let browser handle text copy/cut/paste
        // No text selected — blur and handle as file operation
        e.target.blur();
      } else {
        return;
      }
    }

    if (key === 'F5' || (ctrl && key === 'r')) {
      e.preventDefault();
      this.navigate(pane, this.state.panePaths[pane]);
    } else if (key === 'F2') {
      e.preventDefault();
      this.renameSelected(pane);
    } else if (key === 'Delete' || key === 'Del') {
      e.preventDefault();
      // Delete — в корзину, Shift+Delete — насовсем (как в проводнике).
      this.deleteSelected(pane, e.shiftKey);
    } else if (key === 'F11') {
      e.preventDefault();
      this.toggleFullscreen();
    } else if (ctrl && key === 'f') {
      e.preventDefault();
      const si = document.getElementById('searchInput');
      if (si) { si.focus(); si.select(); }
    } else if (ctrl && key === 'c') {
      e.preventDefault();
      this.copySelected(pane);
    } else if (ctrl && key === 'x') {
      e.preventDefault();
      this.cutSelected(pane);
    } else if (ctrl && key === 'v') {
      e.preventDefault();
      this.pasteTo(pane);
    } else if (ctrl && key === 'a') {
      e.preventDefault();
      this.selectAll(pane);
    } else if (ctrl && key === 'z') {
      e.preventDefault();
      this.undo();
    } else if (ctrl && key === 'y') {
      e.preventDefault();
      this.redo();
    } else if (key === 'Backspace') {
      e.preventDefault();
      this.goUp(pane);
    } else if (key === 'ArrowLeft' && e.altKey) {
      e.preventDefault();
      this.goBack(pane);
    } else if (key === 'ArrowRight' && e.altKey) {
      e.preventDefault();
      this.goForward(pane);
    } else if (key === 'n' && ctrl) {
      e.preventDefault();
      this.newFolder(pane);
    } else if (key === 'd' && e.altKey) {
      e.preventDefault();
      this.showPathInput(pane);
    }
  }

  // ─── EVENTS ─────────────────────────────────────────
  bindEvents() {
    document.addEventListener('keydown', (e) => this.handleKeyboard(e));

    // Search
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
      searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          const mode = document.getElementById('searchMode')?.value || 'name';
          this.doSearch(searchInput.value, mode);
        }
      });
    }

    // Theme select
    const themeSelect = document.getElementById('themeSelect');
    if (themeSelect) themeSelect.addEventListener('change', (e) => this.setTheme(e.target.value));

    // Icon size slider
    const iconSlider = document.getElementById('iconSizeSlider');
    if (iconSlider) {
      iconSlider.addEventListener('input', (e) => {
        this.state.iconSize = parseInt(e.target.value);
        document.getElementById('iconSizeSliderVal').textContent = e.target.value + 'px';
        document.documentElement.style.setProperty('--icon-size', this.state.iconSize + 'px');
        this.render(this.state.activePane);
      });
    }

    // Toolbar buttons
    const bind = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener('click', () => fn()); };
    bind('backBtn', () => this.goBack(this.state.activePane));
    bind('forwardBtn', () => this.goForward(this.state.activePane));
    bind('upBtn', () => this.goUp(this.state.activePane));
    bind('refreshBtn', () => this.navigate(this.state.activePane, this.state.panePaths[this.state.activePane]));
    bind('layoutToggle', () => this.toggleLayout());
    bind('terminalBtn', () => this.openTerminal());
    bind('aiTasksBtn', () => this.openAiTasks('', this.currentFolderPath()));
    bind('aiTasksBtnTop', () => this.openAiTasks('', this.currentFolderPath()));
    bind('themeSwitcher', () => this.toggleTheme());
    bind('settingsBtn', () => this.openSettings());
    bind('encryptionBtn', () => this.toggleEncryption(!this.state.encryptionEnabled));
    bind('batchRenameBtn', () => this.openBatchRename());
    bind('searchBtn', () => { const inp = document.getElementById('searchInput'); const mode = document.getElementById('searchMode')?.value || 'name'; if (inp) this.doSearch(inp.value, mode); });
    bind('newFolderBtn', () => this.newFolder(this.state.activePane));
    bind('newFileBtn', () => this.newFile(this.state.activePane));
    bind('copyBtn', () => this.copySelected(this.state.activePane));
    bind('cutBtn', () => this.cutSelected(this.state.activePane));
    bind('pasteBtn', () => this.pasteTo(this.state.activePane));
    bind('deleteBtn', () => this.deleteSelected(this.state.activePane));
    bind('renameBtn', () => this.renameSelected(this.state.activePane));
    bind('uploadBtn', () => this.upload(this.state.activePane));
    bind('downloadBtn', () => this.downloadSelected(this.state.activePane));
    bind('shareBtn', () => this.shareSelected(this.state.activePane));
    bind('incomingBtn', () => this.openIncomingDialog());
    bind('incomingBtnTop', () => this.openIncomingDialog());
    bind('closePreviewBtn', () => document.getElementById('previewPanel').classList.add('collapsed'));
    bind('hiddenToggle', () => this.toggleHidden());
    bind('checkboxToggle', () => this.toggleCheckboxMode());
    bind('dualSyncToggle', () => this.toggleDualSync());
    bind('ribbonToggle', () => this.toggleRibbon());
    bind('invertBtn', () => this.invertSelection(this.state.activePane));
    bind('filterAll', () => this.setFilter('all'));
    bind('filterImages', () => this.setFilter('images'));
    bind('filterDocuments', () => this.setFilter('documents'));
    bind('filterAudio', () => this.setFilter('audio'));
    bind('filterVideo', () => this.setFilter('video'));
    bind('filterArchives', () => this.setFilter('archives'));

    // Tree toggle
    const treeToggle = document.getElementById('treeToggle');
    if (treeToggle) {
      treeToggle.addEventListener('click', () => {
        document.getElementById('fmTree').classList.toggle('collapsed');
      });
    }
    bind('treeCloseBtn', () => document.getElementById('fmTree').classList.add('collapsed'));

    // Preview toggle
    const previewToggle = document.getElementById('previewToggle');
    if (previewToggle) {
      previewToggle.addEventListener('click', () => {
        document.getElementById('previewPanel').classList.toggle('collapsed');
      });
    }

    // Right-click on empty file list area
    document.querySelectorAll('.fm-file-list').forEach(el => {
      el.addEventListener('contextmenu', (e) => {
        // Only if clicking directly on the container (not on an item)
        if (e.target === el || e.target.classList.contains('fm-empty')) {
          e.preventDefault();
          this.showEmptyContextMenu(e, parseInt(el.dataset.pane));
        }
      });
    });

    // Restore saved state
    const savedLayout = localStorage.getItem('fm_layout');
    if (savedLayout === 'dual') {
      this.state.layout = 'dual';
      const pane2 = document.querySelector('.fm-pane[data-pane="2"]');
      if (pane2) pane2.style.display = 'flex';
    }
    const savedSize = localStorage.getItem('fm_icon_size');
    if (savedSize) {
      this.state.iconSize = parseInt(savedSize);
      document.documentElement.style.setProperty('--icon-size', this.state.iconSize + 'px');
    }

    // Drag & drop upload from browser
    document.addEventListener('dragover', (e) => e.preventDefault());
    document.addEventListener('drop', async (e) => {
      e.preventDefault();
      const files = e.dataTransfer.files;
      if (files.length === 0) return;
      const dest = this.state.panePaths[this.state.activePane];
      await this.uploadFiles(files, dest, this.state.activePane);
    });

    // Scroll sync for dual pane
    document.querySelectorAll('.fm-file-list').forEach(el => {
      el.addEventListener('scroll', () => { this.renderTree(); });
    });

    // Click outside modals to close
    document.addEventListener('click', (e) => {
      if (e.target.classList.contains('fm-modal-overlay')) e.target.remove();
    });

    this.renderTree();
  }

  buildEmptyMenuEntries(pane) {
    return [
      { icon: 'folder-plus', label: 'Новая папка', action: () => this.newFolder(pane), shortcut: 'Ctrl+N' },
      { icon: 'file', label: 'Новый файл', action: () => this.newFile(pane) },
      { icon: 'file-archive', label: 'Создать архив', action: () => this.createArchive(pane) },
      { sep: true },
      { icon: 'file-alt', label: 'Текстовый документ', action: () => this.newFile(pane, 'Новый текстовый документ.txt') },
      { icon: 'code', label: 'Python файл', action: () => this.newFile(pane, 'main.py') },
      { icon: 'code', label: 'HTML файл', action: () => this.newFile(pane, 'index.html') },
      { icon: 'markdown', label: 'Markdown файл', action: () => this.newFile(pane, 'README.md') },
      { sep: true },
      { icon: 'clipboard-list', label: 'Свойства папки', action: () => this.showProperties(this.state.panePaths[pane]) },
    ];
  }

  buildEmptyToolbarBtns(pane) {
    const hasClipboard = this.state.clipboard.items && this.state.clipboard.items.length > 0;
    return [
      ...(hasClipboard ? [{ icon: 'paste', title: 'Вставить', action: () => this.pasteTo(pane) }] : []),
      { icon: 'folder-plus', title: 'Новая папка', action: () => this.newFolder(pane) },
      { icon: 'file', title: 'Новый файл', action: () => this.newFile(pane) },
    ];
  }

  showEmptyContextMenu(e, pane) {
    this.closeContextMenu();
    const menu = document.createElement('div');
    menu.className = 'fm-context-menu';
    document.body.appendChild(menu);

    // Toolbar row
    const toolbar = document.createElement('div');
    toolbar.className = 'fm-ctx-toolbar';
    const toolbarBtns = this.buildEmptyToolbarBtns(pane);
    toolbarBtns.forEach(b => {
      const btn = document.createElement('button');
      btn.className = 'fm-ctx-toolbar-btn';
      btn.title = b.title;
      btn.innerHTML = `<i class="fas fa-${b.icon}"></i>`;
      btn.addEventListener('click', (ev) => { ev.stopPropagation(); b.action(); this.closeContextMenu(); });
      toolbar.appendChild(btn);
    });
    menu.appendChild(toolbar);

    const items = this.buildEmptyMenuEntries(pane);

    for (const item of items) {
      if (item.sep) { const sep = document.createElement('div'); sep.className = 'fm-context-sep'; menu.appendChild(sep); continue; }
      const el = document.createElement('div');
      el.className = 'fm-context-item';
      el.innerHTML = `<i class="fas fa-${item.icon} ctx-icon"></i><span>${item.label}</span>${item.shortcut ? `<span class="ctx-shortcut">${item.shortcut}</span>` : ''}`;
      el.addEventListener('click', () => { item.action(); this.closeContextMenu(); });
      menu.appendChild(el);
    }

    const x = Math.min(e.clientX, window.innerWidth - 240);
    const y = Math.min(e.clientY, window.innerHeight - items.length * 36);
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
    setTimeout(() => document.addEventListener('click', this.closeContextMenu, { once: true }), 10);
  }

  async openWithDialog(path) {
    const name = path.split(/[/\\]/).pop() || path;
    const ext = (name.includes('.') ? '.' + name.split('.').pop().toLowerCase() : '');
    const imgExts = ['.jpg','.jpeg','.png','.gif','.webp','.bmp','.ico','.tiff','.tif'];
    const CODE = FileManagerApp.CODE_EXTENSIONS;

    await this.loadApps();
    await this.loadAssociations();

    const catalog = (this.state.apps || []).filter(a => a && a.id);
    const descByGroup = {
      'wordcraft-web-0.5.0': 'Документы Word', 'verba-office': 'Офисный пакет',
      'gridcraft-web-0.5.0': 'Таблицы', 'deckcraft-web-0.5.0': 'Презентации',
      'pdfcraft-web-0.6.0': 'PDF', 'cadcraft-web-0.5.0': 'CAD-чертежи',
      'solidcraft-web-0.15.0': '3D/CAD-модели', 'vectorcraft-web-0.10.0': 'Векторная графика',
      'designcraft-web-0.6.0': 'Дизайн', 'figcraft-web-0.5.1': 'Интерфейсы',
      'photocraft-web-0.7.0': 'Растровая графика', 'lightcraft-web-0.6.0': 'RAW-фото',
      'filmcraft-web-0.6.0': 'Видеомонтаж', 'video': 'Видеоплеер',
      'soundcraft-web-0.5.0': 'Аудиомонтаж', 'musicmachine': 'Аудиоплеер',
      'code-studio-3': 'Текст и код', 'code-studio-2': 'Текст и код',
      'effectcraft-web-0.8.0': 'Эффекты', 'game collection': 'Игры',
      'cadcraft-browser-edition-v0.6.0': '2D/3D CAD (WASM)',
      'bimcraft-browser-edition-v0.6.0': 'BIM-модели (IFC/DWG)',
      'civilcraft-browser-edition-v0.6.0': 'Генплан и поверхности',
      'detailcraft-browser-edition-v0.6.0': 'Деталировка конструкций',
      'modelcraft-browser-edition-v0.6.0': '3D-модели (SketchUp)',
      'partcraft-browser-edition-v0.6.0': 'Детали и сборки (SolidWorks)',
      'markupcraft-browser-edition-v0.6.0': 'Разметка PDF и чертежей',
      'takeoffcraft-browser-edition-v0.6.0': 'Замеры и сметы',
      'crraftstudio': 'CAD-студия', 'jumpstart-3d': '3D-редактор (DXF)',
      'dos': 'Rust-DOS — веб-среда', 'jsdos': 'DOS-эмулятор',
      'minecraft': 'Игра (Eaglercraft)', 'x86': 'Эмулятор ПК (v86)',
      'docker_layerling_3000': 'Docker-контейнер', 'widget': 'Виджет',
    };
    const iconByGroup = {
      'wordcraft-web-0.5.0': 'file-word', 'verba-office': 'file-word',
      'gridcraft-web-0.5.0': 'file-excel', 'deckcraft-web-0.5.0': 'file-powerpoint',
      'pdfcraft-web-0.6.0': 'file-pdf', 'cadcraft-web-0.5.0': 'drafting-compass',
      'solidcraft-web-0.15.0': 'cube', 'vectorcraft-web-0.10.0': 'bezier-curve',
      'designcraft-web-0.6.0': 'palette', 'figcraft-web-0.5.1': 'object-group',
      'photocraft-web-0.7.0': 'image', 'lightcraft-web-0.6.0': 'camera',
      'filmcraft-web-0.6.0': 'film', 'video': 'play-circle',
      'soundcraft-web-0.5.0': 'waveform', 'musicmachine': 'music',
      'code-studio-3': 'laptop-code', 'code-studio-2': 'laptop-code',
      'effectcraft-web-0.8.0': 'wand-magic-sparkles',
      'cadcraft-browser-edition-v0.6.0': 'drafting-compass',
      'bimcraft-browser-edition-v0.6.0': 'building',
      'civilcraft-browser-edition-v0.6.0': 'road',
      'detailcraft-browser-edition-v0.6.0': 'screwdriver-wrench',
      'modelcraft-browser-edition-v0.6.0': 'cubes',
      'partcraft-browser-edition-v0.6.0': 'cogs',
      'markupcraft-browser-edition-v0.6.0': 'highlighter',
      'takeoffcraft-browser-edition-v0.6.0': 'calculator',
      'crraftstudio': 'ruler-combined', 'jumpstart-3d': 'cube',
      'dos': 'desktop', 'jsdos': 'gamepad', 'minecraft': 'gamepad',
      'x86': 'server', 'docker_layerling_3000': 'layer-group',
      'widget': 'th-large',
    };
    const appName = (id) => {
      const a = this.appById(id);
      return (a && (a.name || id)) || id;
    };
    const assocLabel = (a) => {
      if (!a) return '';
      if (a.label) return a.label;
      const t = a.target || '';
      if (t === 'system') return 'Стандартное приложение Windows';
      if (t === 'builtin_image') return 'Просмотрщик файлового менеджера';
      if (t === 'infinity:editor') return 'Редактор Infinity';
      if (t === 'infinity:viewer') return 'Встроенный просмотр Infinity';
      if (t.startsWith('app:')) return appName(t.slice(4));
      if (t.startsWith('exe:')) return t.slice(4).split(/[/\\]/).pop();
      return t;
    };

    const modal = this.showModal(
      `<i class="fas fa-rocket"></i> Открыть с помощью — ${this.esc(name)}`, '', 'lg');
    const body = modal.querySelector('.fm-modal-body');
    let search = '';

    // Рекомендуемые: приложения, которым расширение соответствует по каталогу.
    const recommended = [];
    const seenIds = new Set();
    const pushApp = (id, desc) => {
      const a = this.appById(id);
      if (!a || seenIds.has(a.id)) return;
      seenIds.add(a.id);
      recommended.push({
        kind: 'app', id: a.id, entry: a.main || 'index.html',
        name: a.name || a.id, icon: iconByGroup[a.id] || 'th',
        desc: desc || descByGroup[a.id] || a.category || 'Встроенное приложение',
        acceptsFile: !!a.acceptsFile,
      });
    };
    const group = FileManagerApp.groupForExt(ext);
    if (group) for (const id of group.ids) pushApp(id);
    if (!recommended.length && (CODE.has(ext) || ext === '.txt' || ext === '.md' || ext === '.inf')) {
      pushApp('code-studio-3');
    }
    if (!recommended.length) {
      for (const a of catalog.slice(0, 6)) pushApp(a.id, 'Встроенное приложение');
    }
    if (imgExts.includes(ext)) {
      recommended.unshift({
        kind: 'builtin_image', name: 'Просмотр в файловом менеджере',
        desc: 'Встроенный просмотр изображения', icon: 'image',
        acceptsFile: false,
      });
    }

    const internal = catalog.map(a => ({
      kind: 'app', id: a.id, entry: a.main || 'index.html',
      name: a.name || a.id, icon: iconByGroup[a.id] || 'th',
      desc: descByGroup[a.id] || a.category || 'Встроенное приложение',
      acceptsFile: !!a.acceptsFile,
    }));

    const rowHtml = (c) => {
      const icon = c.kind === 'exe'
        ? `<span class="fm-ow-exeico"><img loading="lazy" src="${this.esc(c.icon || '')}" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display=''"><i class="fas fa-window-maximize" style="display:none"></i></span>`
        : `<i class="fas fa-${this.esc(c.icon || 'th')}"></i>`;
      return `<div class="fm-ow-row" role="button" tabindex="0"
        data-kind="${c.kind}" data-id="${this.esc(c.id || '')}" data-entry="${this.esc(c.entry || '')}"
        data-exe="${this.esc(c.exe || '')}" data-name="${this.esc(c.name || '')}"
        data-accepts="${c.acceptsFile ? 1 : 0}"
        data-search="${this.esc(((c.name || '') + ' ' + (c.exe || '') + ' ' + (c.desc || '')).toLowerCase())}">
        <div class="fm-ow-ico">${icon}</div>
        <div class="fm-ow-txt">
          <div class="fm-ow-name">${this.esc(c.name || '')}</div>
          <div class="fm-ow-desc">${this.esc(c.desc || '')}</div>
        </div>
        <i class="fas fa-chevron-right fm-ow-arrow"></i>
      </div>`;
    };

    const sysRow = (action, icon, title, desc, searchText) => `
      <div class="fm-ow-row" role="button" tabindex="0" data-ow-action="${action}"
           data-search="${this.esc(searchText.toLowerCase())}">
        <div class="fm-ow-ico"><i class="fas fa-${icon}"></i></div>
        <div class="fm-ow-txt">
          <div class="fm-ow-name">${title}</div>
          <div class="fm-ow-desc">${desc}</div>
        </div>
        <i class="fas fa-chevron-right fm-ow-arrow"></i>
      </div>`;

    const renderPicker = () => {
      const assoc = this.assocFor(ext);
      const banner = assoc ? `
        <div class="fm-ow-assoc">
          <i class="fas fa-star"></i>
          <span>По умолчанию: <b>${this.esc(assocLabel(assoc))}</b></span>
          <button class="fm-btn" data-ow-reset title="Удалить ассоциацию">
            <i class="fas fa-times"></i> Сбросить
          </button>
        </div>` : '';

      let winHtml;
      if (this.state.winApps == null) {
        winHtml = `<div class="fm-ow-loading"><i class="fas fa-spinner fa-spin"></i> Ищу установленные программы…</div>`;
      } else if (!this.state.winApps.length) {
        winHtml = `<div class="fm-ow-loading">Программы не найдены или платформа без Windows.</div>`;
      } else {
        winHtml = `<div class="fm-ow-scroll">${this.state.winApps.map(a => rowHtml({
          kind: 'exe', exe: a.exe, name: a.name, icon: a.icon || '',
          desc: a.exe, acceptsFile: true,
        })).join('')}</div>`;
      }

      body.innerHTML = `
        ${banner}
        <div class="fm-ow-search">
          <i class="fas fa-search"></i>
          <input class="fm-input" id="owSearch" placeholder="Найти приложение…" value="${this.esc(search)}">
        </div>
        <div class="fm-ow-section" data-ow-section>
          <div class="fm-ow-head">Рекомендуемые для ${this.esc(ext || 'этого файла')}</div>
          ${recommended.map(rowHtml).join('')}
        </div>
        <div class="fm-ow-section" data-ow-section>
          <div class="fm-ow-head">Встроенные приложения Infinity</div>
          <div class="fm-ow-scroll">${internal.map(rowHtml).join('')}</div>
        </div>
        <div class="fm-ow-section" data-ow-section>
          <div class="fm-ow-head">
            Все программы Windows
            <button class="fm-ow-refresh" data-ow-refresh title="Обновить список">
              <i class="fas fa-sync-alt"></i>
            </button>
          </div>
          ${winHtml}
        </div>
        <div class="fm-ow-section" data-ow-section>
          <div class="fm-ow-head">Другое</div>
          ${sysRow('infinity_editor', 'pen-to-square', 'Редактор Infinity',
                   'Открыть текст или код во встроенном редакторе',
                   'открыть в infinity редактор текст код editor')}
          ${sysRow('infinity_viewer', 'eye', 'Встроенный просмотр Infinity',
                   'Фото, видео, аудио, PDF, HTML и архивы — без внешних программ',
                   'открыть в infinity просмотр фото видео аудио pdf html архив viewer')}
          ${sysRow('system', 'desktop', 'Стандартное приложение Windows',
                   'Открыть той программой, которая назначена в системе',
                   'стандартное приложение windows system')}
          ${sysRow('external', 'terminal', 'Произвольная команда…',
                   'Запустить утилиту с этим файлом',
                   'произвольная команда terminal консоль')}
        </div>`;

      const input = body.querySelector('#owSearch');
      if (input) {
        input.addEventListener('input', () => {
          search = input.value.trim().toLowerCase();
          body.querySelectorAll('.fm-ow-row').forEach(r => {
            const hay = r.dataset.search || '';
            r.style.display = !search || hay.includes(search) ? '' : 'none';
          });
          body.querySelectorAll('[data-ow-section]').forEach(sec => {
            const any = Array.from(sec.querySelectorAll('.fm-ow-row'))
              .some(r => r.style.display !== 'none');
            sec.style.display = any ? '' : 'none';
          });
        });
      }

      if (this.state.winApps == null) {
        this.loadWindowsApps().then(() => {
          if (document.body.contains(modal)) renderPicker();
        });
      }
    };

    const renderMethods = (choice) => {
      const isApp = choice.kind === 'app';
      const isExe = choice.kind === 'exe';
      // У встроенных приложений способ «параметром» доступен всегда, но
      // отмечаем его заранее только если приложение само читает ?file=.
      const canParam = isApp || isExe;
      const autoParam = isApp ? !!choice.acceptsFile : isExe;
      const paramLabel = isApp
        ? 'Приложение принимает файл на вход'
        : 'Передать файл аргументом командной строки';

      body.innerHTML = `
        <div class="fm-ow-methods">
          <div class="fm-ow-mfile"><i class="fas fa-file"></i> ${this.esc(name)}</div>
          <div class="fm-ow-mapp"><i class="fas fa-window-restore"></i> ${this.esc(choice.name)}</div>
          ${(choice.kind === 'builtin_image' || choice.kind === 'infinity') ? '' : `
            <label class="fm-ow-check${canParam ? '' : ' disabled'}">
              <input type="checkbox" id="owParam" ${autoParam ? 'checked' : ''} ${canParam ? '' : 'disabled'}>
              <span>${paramLabel}</span>
            </label>
            ${isApp ? `
              <label class="fm-ow-check">
                <input type="checkbox" id="owDrop" ${autoParam ? '' : 'checked'}>
                <span>Эмулировать перетаскивание
                  <small>файл скачивается и передаётся приложению как drop (так работают PhotoCraft и др.)</small>
                </span>
              </label>` : `
              <div class="fm-ow-hint"><i class="fas fa-circle-info"></i>
                Перетаскивание доступно только встроенным приложениям: установленная программа получает путь к файлу аргументом.
              </div>`}
            ${isApp && !choice.acceptsFile ? `
              <div class="fm-ow-hint"><i class="fas fa-triangle-exclamation"></i>
                Приложение не сообщает, что читает путь из параметра. Если файл не откроется — включите эмуляцию перетаскивания.
              </div>` : ''}
          `}
          <label class="fm-ow-check${ext ? '' : ' disabled'}">
            <input type="checkbox" id="owDefault" ${ext ? '' : 'disabled'}>
            <span>Открывать по умолчанию файлы <code>${this.esc(ext || 'без расширения')}</code>
              ${ext ? '' : '<small>У файла нет расширения — запомнить ассоциацию нельзя.</small>'}
            </span>
          </label>
          <div class="fm-ow-actions">
            <button class="fm-btn" id="owBack"><i class="fas fa-arrow-left"></i> Назад</button>
            <button class="fm-btn primary" id="owGo"><i class="fas fa-external-link-alt"></i> Открыть</button>
          </div>
        </div>`;

      body.querySelector('#owBack')?.addEventListener('click', renderPicker);
      body.querySelector('#owGo')?.addEventListener('click', async () => {
        const param = !!body.querySelector('#owParam')?.checked;
        const drop = !!body.querySelector('#owDrop')?.checked;
        const makeDefault = !!body.querySelector('#owDefault')?.checked;
        modal.remove();

        if (choice.kind === 'app') {
          let mode = 'none';
          if (param && drop) mode = 'both';
          else if (param) mode = 'param';
          else if (drop) mode = 'drop';
          if (makeDefault) await this.saveAssociation(ext, 'app:' + choice.id, mode, choice.name);
          this.openApp(choice.id, mode === 'none' ? '' : path, choice.entry, mode);
        } else if (choice.kind === 'exe') {
          const method = param ? 'arg' : 'none';
          if (makeDefault) await this.saveAssociation(ext, 'exe:' + choice.exe, method, choice.name);
          this.openWithExe(choice.exe, path, param);
        } else if (choice.kind === 'builtin_image') {
          if (makeDefault) await this.saveAssociation(ext, 'builtin_image', 'auto', choice.name);
          this.openFullscreen('image', path);
        } else if (choice.kind === 'infinity') {
          if (makeDefault) await this.saveAssociation(ext, choice.target, 'none', choice.name);
          this.applyInfinityTarget(choice.target, path);
        }
      });
    };

    body.addEventListener('click', async (e) => {
      const reset = e.target.closest('[data-ow-reset]');
      if (reset) {
        await this.saveAssociation(ext, 'system', 'auto', '', true);
        renderPicker();
        return;
      }
      const refresh = e.target.closest('[data-ow-refresh]');
      if (refresh) {
        this.state.winApps = null;
        renderPicker();
        return;
      }
      const row = e.target.closest('.fm-ow-row');
      if (!row) return;

      if (row.dataset.owAction === 'system') {
        modal.remove();
        this.openSystemApp(path);
        return;
      }
      const infinityTargets = {
        infinity_editor: 'infinity:editor',
        infinity_viewer: 'infinity:viewer',
      };
      if (infinityTargets[row.dataset.owAction]) {
        renderMethods({
          kind: 'infinity',
          target: infinityTargets[row.dataset.owAction],
          name: row.dataset.owAction === 'infinity_editor'
            ? 'Редактор Infinity' : 'Встроенный просмотр Infinity',
          acceptsFile: false,
        });
        return;
      }
      if (row.dataset.owAction === 'external') {
        modal.remove();
        const isWin = /Win/i.test(navigator.platform || navigator.userAgent);
        const suggested = isWin ? 'notepad' : 'xdg-open';
        const cmd = await this.prompt(
          'Введите команду:<br><small style="color:var(--text3)">например: <code>notepad</code>, <code>code</code>, <code>explorer</code></small>',
          suggested);
        if (!cmd) return;
        try {
          const res = await this.apiJson('POST', '/terminal', { command: `${cmd} "${path}"` });
          if (res.returncode !== 0 && res.returncode !== -1) this.showNotification(`Ошибка: ${res.stderr || 'команда не найдена'}`, 'error');
          else this.showNotification(`Открыто через ${cmd}`, 'success');
        } catch (err) { this.showNotification(err.message, 'error'); }
        return;
      }

      renderMethods({
        kind: row.dataset.kind,
        id: row.dataset.id || '',
        entry: row.dataset.entry || 'index.html',
        exe: row.dataset.exe || '',
        name: row.dataset.name || '',
        acceptsFile: row.dataset.accepts === '1',
      });
    });

    body.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const row = e.target.closest('.fm-ow-row');
      if (!row) return;
      e.preventDefault();
      row.click();
    });

    renderPicker();
  }
  openSettings() {
    const html = `
      <div style="display:flex;flex-direction:column;gap:12px">
        <label><i class="fas fa-palette"></i> Тема:</label>
        <select id="themeSelectModal" class="fm-select" data-fm-theme-select>
          <option value="dark">Тёмная</option>
          <option value="light">Светлая</option>
        </select>
        <label><i class="fas fa-image"></i> Размер иконок: <span id="sizeLabel">${this.state.iconSize}px</span></label>
        <input type="range" id="iconSizeModal" min="40" max="160" value="${this.state.iconSize}" style="width:100%">
        <label><i class="fas fa-columns"></i> <input type="checkbox" id="layoutDual" ${this.state.layout === 'dual' ? 'checked' : ''}> Двухпанельный режим</label>
        <label><i class="fas fa-sync-alt"></i> <input type="checkbox" id="syncDual" ${this.state.dualSync ? 'checked' : ''}> Синхронизация панелей</label>
        <label><i class="fas fa-eye-slash"></i> <input type="checkbox" id="hiddenFiles" ${this.state.showHidden ? 'checked' : ''}> Показывать скрытые файлы</label>
        <label><i class="fas fa-check-square"></i> <input type="checkbox" id="checkboxMode" ${this.state.checkboxMode ? 'checked' : ''}> Режим чекбоксов</label>
        <label title="Необязательная функция: файлы шифруются на диске"><i class="fas fa-lock"></i> <input type="checkbox" id="encryptFiles" ${this.state.encryptionEnabled ? 'checked' : ''} ${this.state.encryptionAvailable ? '' : 'disabled'}> Шифрование файлов</label>
        <div style="margin-left:24px;font-size:11px;color:var(--text3)">${this.state.encryptionAvailable ? 'В контекстном меню появятся «Зашифровать»/«Расшифровать». Ключ: ~/.infinity/asio_agent/encryption.key' : 'Модуль шифрования недоступен в этой сборке'}</div>
        <div id="fileIndexRow" style="font-size:13px"></div>
        <div id="mobileTplRow" style="font-size:13px"></div>
        <div style="margin-top:8px;font-size:12px;color:var(--text3)">
          <div><i class="fas fa-star"></i> Избранное: ${this.state.favorites.length} папок</div>
          <div><i class="fas fa-history"></i> Недавние: ${this.state.recentPaths.length} путей</div>
          <div><i class="fas fa-keyboard"></i> F2-переименовать, Del-удалить, Ctrl+C/X/V, F11-полный экран</div>
        </div>
      </div>`;
    const modal = this.showModal('<i class="fas fa-cog"></i> Настройки', html, 'sm');
    setTimeout(() => {
      document.getElementById('themeSelectModal')?.addEventListener('change', (e) => this.setTheme(e.target.value));
      this.fillThemeSelectors();
      document.getElementById('iconSizeModal')?.addEventListener('input', (e) => {
        const v = parseInt(e.target.value);
        this.state.iconSize = v;
        document.documentElement.style.setProperty('--icon-size', v + 'px');
        document.getElementById('sizeLabel').textContent = v + 'px';
        this.render(this.state.activePane);
      });
      document.getElementById('layoutDual')?.addEventListener('change', (e) => { if (e.target.checked && this.state.layout === 'single') this.toggleLayout(); else if (!e.target.checked && this.state.layout === 'dual') this.toggleLayout(); });
      document.getElementById('syncDual')?.addEventListener('change', (e) => { this.state.dualSync = e.target.checked; });
      document.getElementById('hiddenFiles')?.addEventListener('change', (e) => { this.state.showHidden = e.target.checked; this.navigate(this.state.activePane, this.state.panePaths[this.state.activePane]); });
      document.getElementById('checkboxMode')?.addEventListener('change', (e) => { this.state.checkboxMode = e.target.checked; localStorage.setItem('fm_checkbox', this.state.checkboxMode); this.render(this.state.activePane); });
      document.getElementById('encryptFiles')?.addEventListener('change', (e) => { this.toggleEncryption(e.target.checked).then(() => this.render(this.state.activePane)); });
      this.renderMobileTemplatesRow();
      this.renderFileIndexRow();
    }, 50);
  }

  // Строка «Мобильные шаблоны» в настройках. Пустой div, если API недоступен
  // (мобильные шаблоны — опциональная возможность, desktop не должен из-за
  // неё терять ничего).
  async renderMobileTemplatesRow() {
    const host = document.getElementById('mobileTplRow');
    if (!host) return;
    let st;
    try {
      st = await this.api('/mobile-templates');
    } catch (e) {
      return;
    }
    const draw = (s2) => {
      host.innerHTML = `
        <label title="Отдельные мобильные шаблоны модулей; включается для входа с телефона">
          <i class="fas fa-mobile-screen"></i> <input type="checkbox" id="mobileTplChk" ${s2.enabled ? 'checked' : ''}> Мобильные шаблоны</label>
        <div style="margin-left:24px;font-size:11px;color:var(--text3)">
          Приоритет: config.json → кука mobile_mode → User-Agent.
          ${s2.cookie ? 'Кука этого браузера: <b>' + s2.cookie + '</b>. ' : ''}
          Этот запрос: ${s2.mobile_now ? 'мобильный' : 'не мобильный'}.
        </div>`;
      host.querySelector('#mobileTplChk')?.addEventListener('change', async (e) => {
        try {
          const r = await this.apiJson('POST', '/mobile-templates/toggle', { enabled: e.target.checked });
          this.showNotification('Мобильные шаблоны: ' + (r.enabled ? 'включены' : 'выключены'), 'success');
          draw(Object.assign({}, s2, { enabled: r.enabled }));
        } catch (err) {
          this.showNotification('Не удалось сохранить: ' + err.message, 'error');
        }
      });
    };
    draw(st);
  }

  // ─── ЛИЧНЫЙ ИНДЕКС ФАЙЛОВ ДЛЯ SEARCH ALL ─────────────
  async renderFileIndexRow() {
    const host = document.getElementById('fileIndexRow');
    if (!host) return;
    let st;
    try {
      st = await this.fileIndexApi('GET', '/status');
    } catch (e) {
      host.innerHTML = `<div style="color:var(--text3);font-size:11px">
        <i class="fas fa-file-lines"></i> Индекс файлов недоступен: ${this.esc(e.message)}</div>`;
      return;
    }
    if (!st.available) {
      host.innerHTML = `<div style="color:var(--text3);font-size:11px;border-top:1px solid var(--border);padding-top:10px">
        <i class="fas fa-file-lines"></i> Индексация файлов выключена администратором.</div>`;
      return;
    }
    const busy = st.running || st.vector_running;
    let mst = null;
    try { mst = await this.mediaIndexApi('GET', '/status'); } catch (e) { mst = null; }
    const folders = (st.folders || []).map((p, i) => `
      <div style="display:flex;align-items:center;gap:6px">
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${this.esc(p)}">${this.esc(p)}</span>
        <button class="fm-btn" data-fi-remove="${i}" title="Убрать папку" ${busy ? 'disabled' : ''}><i class="fas fa-times"></i></button>
      </div>`).join('');
    host.innerHTML = `
      <div style="border-top:1px solid var(--border);padding-top:10px;display:flex;flex-direction:column;gap:8px">
        <label title="Приватная функция: файлы ищутся только в вашем профиле">
          <i class="fas fa-file-lines"></i>
          <input type="checkbox" id="fileIndexChk" ${st.enabled ? 'checked' : ''}> Индексировать файлы для Search All
        </label>
        <div style="margin-left:24px;font-size:11px;color:var(--text3)">
          Только текстовые файлы до 2 МБ. Индекс и векторы хранятся в вашем профиле;
          другие пользователи их не видят.
        </div>
        <div style="margin-left:24px;display:flex;flex-direction:column;gap:6px">
          <div style="font-size:12px;color:var(--text3)">Папки (${(st.folders || []).length}):</div>
          ${folders || '<div style="font-size:11px;color:var(--text3)">Пока не выбрано ни одной папки</div>'}
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <button class="fm-btn" id="fileIndexAddCurrent" ${busy ? 'disabled' : ''}><i class="fas fa-folder-plus"></i> Текущая папка</button>
            <button class="fm-btn" id="fileIndexAddRoot" ${busy ? 'disabled' : ''}><i class="fas fa-house"></i> Корень профиля</button>
            <button class="fm-btn" id="fileIndexReindex" ${(busy || !st.enabled) ? 'disabled' : ''}><i class="fas fa-rotate"></i> Переиндексировать</button>
            <button class="fm-btn" id="fileIndexPurge" ${busy ? 'disabled' : ''}><i class="fas fa-trash"></i> Очистить</button>
          </div>
          <div style="display:flex;gap:6px">
            <input class="fm-input" id="fileIndexPath" placeholder="Абсолютный путь к любой доступной папке" style="flex:1;min-width:0" ${busy ? 'disabled' : ''}>
            <button class="fm-btn" id="fileIndexAddPath" ${busy ? 'disabled' : ''}><i class="fas fa-plus"></i> Добавить</button>
          </div>
          <label title="Векторный поиск по личной коллекции Qdrant">
            <i class="fas fa-database"></i>
            <input type="checkbox" id="fileIndexVector" ${st.vector ? 'checked' : ''} ${st.enabled ? '' : 'disabled'}> Векторный индекс (личный)
          </label>
          <div id="fileIndexStatus" style="font-size:11px;color:var(--text3)">${this.fileIndexStatusText(st)}</div>
          ${this.mediaIndexHtml(mst, st)}
        </div>
      </div>`;

    const save = async (patch, okMsg) => {
      try {
        await this.fileIndexApi('POST', '/settings', patch);
        this.showNotification(okMsg || 'Настройка индекса сохранена', 'success');
        await this.renderFileIndexRow();
      } catch (e) {
        this.showNotification(e.message, 'error');
        await this.renderFileIndexRow();
      }
    };

    document.getElementById('fileIndexChk')?.addEventListener('change', (e) => {
      save({ enabled: e.target.checked }, e.target.checked ? 'Индексация включена' : 'Индексация выключена');
    });
    document.getElementById('fileIndexVector')?.addEventListener('change', (e) => {
      save({ vector: e.target.checked }, e.target.checked ? 'Векторный индекс включён' : 'Векторный индекс выключен');
    });
    document.getElementById('fileIndexAddCurrent')?.addEventListener('click', () => {
      const pane = this.state.activePane || 1;
      const path = this.state.panePaths[pane] || this.state.profileRoot;
      if (!path) return;
      save({ folders: [...(st.folders || []), path] }, 'Папка добавлена');
    });
    document.getElementById('fileIndexAddRoot')?.addEventListener('click', () => {
      if (!this.state.profileRoot) {
        this.showNotification('Корень профиля не определён', 'warn');
        return;
      }
      save({ folders: [...(st.folders || []), this.state.profileRoot] }, 'Корень профиля добавлен');
    });
    document.getElementById('fileIndexAddPath')?.addEventListener('click', () => {
      const input = document.getElementById('fileIndexPath');
      const path = (input?.value || '').trim();
      if (!path) return;
      if (!/^([a-zA-Z]:[\\/]|\\\\|\/)/.test(path)) {
        this.showNotification('Нужен абсолютный путь', 'warn');
        return;
      }
      save({ folders: [...(st.folders || []), path] }, 'Папка добавлена');
    });
    host.querySelectorAll('[data-fi-remove]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.getAttribute('data-fi-remove'), 10);
        const next = (st.folders || []).filter((_, i) => i !== idx);
        save({ folders: next }, 'Папка убрана');
      });
    });
    document.getElementById('fileIndexReindex')?.addEventListener('click', async () => {
      try {
        await this.fileIndexApi('POST', '/reindex');
        this.showNotification('Переиндексация запущена', 'success');
      } catch (e) {
        this.showNotification(e.message, 'error');
      }
      this.fileIndexStatusPoll();
    });
    document.getElementById('fileIndexPurge')?.addEventListener('click', async () => {
      if (!(await this.confirm('Очистить индекс и личные векторы файлов? Настройки папок сохранятся.'))) return;
      try {
        await this.fileIndexApi('POST', '/purge');
        this.showNotification('Индекс очищен', 'success');
      } catch (e) {
        this.showNotification(e.message, 'error');
      }
      await this.renderFileIndexRow();
    });

    const saveMedia = async (patch, okMsg) => {
      try {
        await this.mediaIndexApi('POST', '/settings', patch);
        this.showNotification(okMsg || 'Медиа-настройка сохранена', 'success');
        await this.renderFileIndexRow();
      } catch (e) {
        this.showNotification(e.message, 'error');
        await this.renderFileIndexRow();
      }
    };
    const mediaBind = (id, key, onMsg, offMsg) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.addEventListener('change', (e) => saveMedia({ [key]: e.target.checked },
        e.target.checked ? onMsg : offMsg));
    };
    mediaBind('mediaDocChk', 'documents', 'Документы включены', 'Документы выключены');
    mediaBind('mediaImgChk', 'images', 'Картинки включены', 'Картинки выключены');
    mediaBind('mediaAudioChk', 'audio', 'Аудио включено', 'Аудио выключено');
    mediaBind('mediaVideoChk', 'video', 'Видео включено', 'Видео выключено');
    mediaBind('mediaOcrChk', 'ocr', 'OCR включён', 'OCR выключен');
    mediaBind('mediaSyncChk', 'sync', 'Автообновление включено', 'Автообновление выключено');

    document.getElementById('mediaReindex')?.addEventListener('click', async () => {
      try {
        await this.mediaIndexApi('POST', '/reindex');
        this.showNotification('Медиа-переиндексация запущена', 'success');
      } catch (e) {
        this.showNotification(e.message, 'error');
      }
      this.mediaIndexStatusPoll();
    });
    document.getElementById('mediaWarmup')?.addEventListener('click', async () => {
      try {
        await this.mediaIndexApi('POST', '/warmup');
        this.showNotification('Модель прогревается в фоне — прогресс в статусе ниже', 'info');
      } catch (e) {
        this.showNotification(e.message, 'error');
      }
      this.mediaIndexStatusPoll();
    });
    document.getElementById('mediaPurge')?.addEventListener('click', async () => {
      if (!(await this.confirm('Очистить медиа-индекс и его векторы? Настройки папок и типов сохранятся.'))) return;
      try {
        await this.mediaIndexApi('POST', '/purge');
        this.showNotification('Медиа-индекс очищен', 'success');
      } catch (e) {
        this.showNotification(e.message, 'error');
      }
      await this.renderFileIndexRow();
    });

    if (busy) this.fileIndexStatusPoll();
    if (mst && (mst.running || mst.vector_running)) this.mediaIndexStatusPoll();
  }

  mediaIndexHtml(mst, fst) {
    if (!mst) {
      return `<div style="margin-left:24px;font-size:11px;color:var(--text3);border-top:1px dashed var(--border);padding-top:8px">
        <i class="fas fa-photo-film"></i> Медиа-индекс: статус недоступен.</div>`;
    }
    if (!mst.available) {
      return `<div style="margin-left:24px;font-size:11px;color:var(--text3);border-top:1px dashed var(--border);padding-top:8px">
        <i class="fas fa-photo-film"></i> Медиа-индекс (документы, картинки, аудио, видео)
        выключен администратором.</div>`;
    }
    const busy = !!(mst.running || mst.vector_running);
    const off = (!fst.enabled || busy) ? 'disabled' : '';
    const gem = mst.gemma || {};
    const tools = mst.tools || {};
    const gemText = gem.loaded
      ? `модель загружена (${this.esc(gem.device || 'cpu')})`
      : (gem.running ? 'воркер запущен, модель ещё грузится'
                     : (gem.available ? 'модель не загружена — поднимется при первом поиске'
                                      : 'модель не установлена'));
    const toolBits = [
      `Tesseract: ${tools.tesseract ? 'да' : 'нет'}`,
      `DjVu: ${tools.djvu ? 'да' : 'нет'}`,
    ].join(' · ');
    return `
      <div style="border-top:1px dashed var(--border);padding-top:8px;margin-top:4px;display:flex;flex-direction:column;gap:6px">
        <div style="font-size:12px;color:var(--text2)"><i class="fas fa-photo-film"></i> Медиа-индекс
          <span style="font-size:11px;color:var(--text3)">— документы с OCR, картинки, аудио, видео</span></div>
        <div style="margin-left:18px;display:flex;flex-direction:column;gap:4px">
          <label title="PDF, DOCX, EPUB, FB2, RTF, DjVu — текст извлекается и ищется"><input type="checkbox" id="mediaDocChk" ${mst.documents ? 'checked' : ''} ${off}> Документы</label>
          <label title="Поиск по картинкам через EmbeddingGemma 2"><input type="checkbox" id="mediaImgChk" ${mst.images ? 'checked' : ''} ${off}> Картинки</label>
          <label title="Теги, исполнитель, альбом; смысловой поиск по звуку"><input type="checkbox" id="mediaAudioChk" ${mst.audio ? 'checked' : ''} ${off}> Аудио</label>
          <label title="Имя, длительность и кадры-превью"><input type="checkbox" id="mediaVideoChk" ${mst.video ? 'checked' : ''} ${off}> Видео</label>
          <label title="Распознавание текста на страницах PDF/DjVu и картинках"><input type="checkbox" id="mediaOcrChk" ${mst.ocr ? 'checked' : ''} ${off}> OCR (rus+eng)</label>
          <label title="Пересобирать индекс раз в 15 минут, когда папки меняются"><input type="checkbox" id="mediaSyncChk" ${mst.sync ? 'checked' : ''} ${off}> Автообновление</label>
        </div>
        <div style="margin-left:18px;display:flex;gap:6px;flex-wrap:wrap">
          <button class="fm-btn" id="mediaReindex" ${off}><i class="fas fa-rotate"></i> Переиндексировать</button>
          <button class="fm-btn" id="mediaWarmup" ${busy ? 'disabled' : ''}><i class="fas fa-microchip"></i> Прогреть модель</button>
          <button class="fm-btn" id="mediaPurge" ${off}><i class="fas fa-trash"></i> Очистить медиа</button>
        </div>
        <div id="mediaIndexStatus" style="margin-left:18px;font-size:11px;color:var(--text3)">${this.mediaIndexStatusText(mst)}</div>
        <div style="margin-left:18px;font-size:11px;color:var(--text3)">${gemText} · ${toolBits}</div>
        ${mst.error ? `<div style="margin-left:18px;font-size:11px;color:var(--red)">${this.esc(mst.error)}</div>` : ''}
      </div>`;
  }

  mediaIndexStatusText(mst) {
    const c = mst.counts || {};
    const parts = [];
    if (mst.running) parts.push(`разбор: ${mst.done || 0}/${mst.total || 0}`);
    else parts.push(`файлов: ${mst.files || 0} (док ${c.document || 0}, изобр ${c.image || 0}, аудио ${c.audio || 0}, видео ${c.video || 0})`);
    if (mst.vector_running) parts.push(`векторы: ${mst.vector_done || 0}/${mst.vector_total || 0}`);
    if (mst.vector_error) parts.push(`ошибка векторов: ${this.esc(mst.vector_error)}`);
    if (mst.built) {
      try { parts.push('обновлён: ' + new Date(mst.built * 1000).toLocaleString()); } catch {}
    }
    return parts.join(' · ');
  }

  mediaIndexStatusPoll() {
    clearTimeout(this._miPollTimer);
    this._miPollTimer = setTimeout(async () => {
      if (!document.getElementById('mediaIndexStatus')) return;
      try {
        const mst = await this.mediaIndexApi('GET', '/status');
        const el = document.getElementById('mediaIndexStatus');
        if (el) el.innerHTML = this.mediaIndexStatusText(mst);
        if (mst.running || mst.vector_running) this.mediaIndexStatusPoll();
        else await this.renderFileIndexRow();
      } catch {}
    }, 2500);
  }

  fileIndexStatusText(st) {
    if (st.error) return `<span style="color:var(--red)">Ошибка: ${this.esc(st.error)}</span>`;
    const parts = [];
    if (st.running) parts.push(`индексация: ${st.done || 0}/${st.total || 0}`);
    else parts.push(`файлов: ${st.files || 0}`);
    if (st.skipped) parts.push(`пропущено: ${st.skipped}`);
    if (st.built) {
      try { parts.push('обновлён: ' + new Date(st.built * 1000).toLocaleString()); } catch {}
    }
    if (st.vector_running) parts.push(`векторы: ${st.vector_done || 0}/${st.vector_total || 0}`);
    return parts.join(' · ');
  }

  fileIndexStatusPoll() {
    clearTimeout(this._fiPollTimer);
    this._fiPollTimer = setTimeout(async () => {
      if (!document.getElementById('fileIndexRow')) return;
      try {
        const st = await this.fileIndexApi('GET', '/status');
        const el = document.getElementById('fileIndexStatus');
        if (el) el.innerHTML = this.fileIndexStatusText(st);
        if (st.running || st.vector_running) this.fileIndexStatusPoll();
        else await this.renderFileIndexRow();
      } catch {}
    }, 2000);
  }

  // ══════════════════════════════════════════════════════════════
  // .inf — исполняемые файлы Infinity
  // ══════════════════════════════════════════════════════════════

  // Прочитать .inf и показать, что внутри. Возвращает разобранное описание
  // или null, если файл битый/не .inf.
  // Заготовка задания для папки: путь уже подставлен, остаётся дописать
  // расписание и уточнить, что именно делать.
  aiFolderPrompt(folderPath) {
    return `Каждый день в 14:00 работай с папкой ${folderPath}: `;
  }

  async infInspect(path) {
    try {
      const res = await fetch('/api/extensions/inf-inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const d = await res.json();
      if (!d.ok) {
        this.showNotification(d.error || 'Не удалось прочитать .inf', 'error');
        return null;
      }
      return d.info;
    } catch (e) {
      this.showNotification('Ошибка: ' + e.message, 'error');
      return null;
    }
  }

  // Точка входа: двойной клик по .inf
  async openInfFile(path) {
    const info = await this.infInspect(path);
    if (!info) return;
    if (info.kind === 'app') this.runInfApp(path, info);
    else this.showInfInstaller(path, info);
  }

  // ─── .inf типа app: окно WinBox ─────────────────────────
  async runInfApp(path, info) {
    let opened;
    try {
      const res = await fetch('/api/extensions/inf-open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      opened = await res.json();
    } catch (e) {
      this.showNotification('Ошибка: ' + e.message, 'error');
      return;
    }
    if (!opened.ok) {
      this.showNotification(opened.error || 'Не удалось открыть', 'error');
      return;
    }

    const pending = opened.pending_permissions || [];
    if (pending.length) {
      const granted = await this.infConsentDialog(info, pending);
      if (!granted) return;   // пользователь отказался — не открываем
      // Прагромаем выбор: иначе приложение открылось бы уже без этих прав,
      // и Python-часть падала бы на первой же попытке ими воспользоваться.
      try {
        await fetch('/api/extensions/inf-consent', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: opened.token, permissions: granted }),
        });
      } catch (e) {
        this.showNotification('Не удалось сохранить решение по правам', 'warn');
        return;
      }
    }
    this.infWindow(info.name || 'INF-приложение', opened.url, opened.info);
  }

  // Окно приложения. iframe с песочницей: приложение не получает доступ к
  // странице-родителю, всё общение с сервером идёт через /inf-app/.
  infWindow(title, url, info) {
    const offset = (this._infWinCount = (this._infWinCount || 0) + 1) * 28;
    const html = `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg2)">
        ${info && info.has_python ? `<div style="flex-shrink:0;padding:5px 10px;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:6px">
          <i class="fas fa-microchip" style="color:var(--green)"></i> Python-часть работает в песочнице
        </div>` : ''}
        <iframe src="${url}" style="flex:1;width:100%;height:100%;border:none;background:#0f1419"
                sandbox="allow-scripts allow-forms allow-modals allow-popups"></iframe>
      </div>`;
    if (typeof WinBox === 'undefined') {
      // Страницу открыли без winbox (файл правили вручную) — падаем на вкладку.
      window.open(url, '_blank');
      return;
    }
    new WinBox(this.esc(title), {
      html,
      x: Math.max(0, 90 + offset), y: Math.max(0, 60 + offset),
      width: Math.min(760, window.innerWidth - 40),
      height: Math.min(560, window.innerHeight - 100),
      minwidth: 320, minheight: 220, max: false,
    });
  }

  // Диалог согласия по расширенным правам .inf-приложения.
  // null — пользователь отказался.
  infConsentDialog(info, pending) {
    return new Promise((resolve) => {
      const rows = pending.map(p => `
        <label style="display:flex;gap:10px;align-items:flex-start;padding:9px 11px;background:var(--bg);border:1px solid var(--border);border-radius:8px;cursor:pointer">
          <input type="checkbox" class="inf-perm" value="${this.esc(p)}" checked style="margin-top:2px">
          <div style="flex:1;min-width:0">
            <div style="font-size:12px;font-weight:600;color:var(--warning)"><i class="fas fa-shield-halved"></i> ${this.esc(p)}</div>
            <div class="inf-perm-desc" data-perm="${this.esc(p)}" style="font-size:11px;color:var(--text3);margin-top:2px"></div>
          </div>
        </label>`).join('');

      const html = `
        <div style="display:flex;flex-direction:column;gap:12px">
          <div style="background:var(--bg);border:1px solid var(--orange);border-radius:8px;padding:11px 13px;font-size:12px;line-height:1.6">
            <b style="color:var(--orange)"><i class="fas fa-triangle-exclamation"></i> Приложение «${this.esc(info.name || '')}» просит дополнительные права</b>
            <div style="color:var(--text2);margin-top:5px">Базовые права (хранилище и интерфейс) выданы автоматически. Остальные выдаются только с вашего согласия — снимите галочку, если оно вам не нужно.</div>
          </div>
          <div style="display:flex;flex-direction:column;gap:6px">${rows}</div>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="fm-btn" id="infPermCancel">Не открывать</button>
            <button class="fm-btn primary" id="infPermOk"><i class="fas fa-play"></i> Открыть</button>
          </div>
        </div>`;

      const modal = this.showModal('<i class="fas fa-shield-halved"></i> Права приложения', html, 'sm');
      modal._permDesc = modal._permDesc || {};
      this._fillPermDescriptions(modal, pending);
      const close = (val) => { modal.remove(); resolve(val); };
      document.getElementById('infPermCancel')?.addEventListener('click', () => close(null));
      document.getElementById('infPermOk')?.addEventListener('click', () => {
        const granted = Array.from(modal.querySelectorAll('.inf-perm:checked')).map(c => c.value);
        close(granted);
      });
      modal.addEventListener('click', (e) => { if (e.target === modal) close(null); });
    });
  }

  // Подставить человеческие описания прав (список общий с расширениями).
  async _fillPermDescriptions(modal, perms) {
    modal._permDesc = modal._permDesc || {};
    if (!perms.length) return;
    try {
      const r = await fetch('/api/extensions/permissions/list-all');
      const d = await r.json();
      const arr = d.permissions || [];
      for (const p of arr) if (p && p.id) modal._permDesc[p.id] = p.description || '';
    } catch (e) { /* остаются без описаний — не критично */ }
    modal.querySelectorAll('.inf-perm-desc').forEach(el => {
      const d = modal._permDesc[el.dataset.perm];
      if (d) el.textContent = d;
    });
  }

  // ─── .inf типа extension: мини-окно установщика ─────────
  showInfInstaller(path, info) {
    const files = info.files || [];
    const perms = info.permissions || [];
    const hasPerms = !!perms.length;

    const html = `
      <div style="display:flex;flex-direction:column;gap:12px">
        <div style="display:flex;gap:12px;align-items:flex-start;background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:13px">
          <div style="width:42px;height:42px;flex-shrink:0;border-radius:10px;background:var(--bg3);display:flex;align-items:center;justify-content:center">
            <i class="fas ${this.esc(info.icon || 'fa-puzzle-piece')}" style="font-size:20px;color:var(--accent)"></i>
          </div>
          <div style="flex:1;min-width:0">
            <div style="font-size:15px;font-weight:700">${this.esc(info.name || 'Расширение')}</div>
            <div style="font-size:11px;color:var(--text3);margin-top:2px">
              версия ${this.esc(info.version || '—')}${info.author ? ' · ' + this.esc(info.author) : ''}
            </div>
            ${info.description ? `<div style="font-size:12px;color:var(--text2);margin-top:6px;line-height:1.5">${this.esc(info.description)}</div>` : ''}
          </div>
        </div>

        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <span class="fm-ai-chip"><i class="fas fa-cubes"></i> Модуль: ${this.esc(info.target_module || 'all')}</span>
          <span class="fm-ai-chip"><i class="fas fa-plug"></i> Хуков: ${(info.hooks || []).length}</span>
          <span class="fm-ai-chip"><i class="fas fa-file-code"></i> Файлов: ${files.length}</span>
          ${info.has_python ? '<span class="fm-ai-chip" style="color:var(--orange)"><i class="fas fa-microchip"></i> Есть Python</span>' : ''}
        </div>

        ${hasPerms ? `
        <div>
          <div style="font-size:12px;font-weight:600;margin-bottom:6px"><i class="fas fa-shield-halved"></i> Запрошенные права</div>
          <div style="display:flex;flex-wrap:wrap;gap:5px">
            ${perms.map(p => `<label class="inf-perm-label" data-perm="${this.esc(p)}" style="display:inline-flex;align-items:center;gap:5px;padding:4px 9px;background:var(--bg);border:1px solid var(--border);border-radius:7px;cursor:pointer;font-size:11px">
              <input type="checkbox" class="inf-ext-perm" value="${this.esc(p)}" checked> <b>${this.esc(p)}</b>
            </label>`).join('')}
          </div>
          <div id="infExtPermDesc" style="font-size:11px;color:var(--text3);margin-top:6px;min-height:15px"></div>
        </div>` : ''}

        <div>
          <div style="font-size:12px;font-weight:600;margin-bottom:6px"><i class="fas fa-cogs"></i> Режим Python</div>
          <div style="display:flex;flex-direction:column;gap:6px">
            <label style="display:flex;gap:10px;padding:9px 11px;background:var(--bg);border:1px solid var(--accent);border-radius:8px;cursor:pointer">
              <input type="radio" name="infSandbox" value="1" checked style="margin-top:2px">
              <div style="font-size:12px;line-height:1.5">
                <b><i class="fas fa-shield-halved" style="color:var(--green)"></i> В песочнице</b>
                <div style="color:var(--text3)">Код выполняется в отдельном процессе: сбой не уронит сервер, доступ к файлам ограничен выданными правами.</div>
              </div>
            </label>
            <label style="display:flex;gap:10px;padding:9px 11px;background:var(--bg);border:1px solid var(--border);border-radius:8px;cursor:pointer">
              <input type="radio" name="infSandbox" value="0" style="margin-top:2px">
              <div style="font-size:12px;line-height:1.5">
                <b><i class="fas fa-unlock" style="color:var(--orange)"></i> Напрямую</b>
                <div style="color:var(--text3)">Полный доступ к серверу. Выбирайте только для своего кода.</div>
              </div>
            </label>
          </div>
        </div>

        <label style="display:flex;gap:10px;align-items:center;padding:9px 11px;background:var(--bg);border:1px solid var(--border);border-radius:8px;cursor:pointer">
          <input type="checkbox" id="infEnableNow">
          <span style="font-size:12px"><b>Включить сразу после установки</b>
            <span style="color:var(--text3)"> — иначе расширение появится в списке выключенным, включите тумблером позже</span></span>
        </label>

        <div style="font-size:11px;color:var(--text3);max-height:80px;overflow:auto;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 10px">
          ${files.length ? files.map(f => this.esc(f)).join('<br>') : '—'}
        </div>

        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="fm-btn" id="infInstCancel">Отмена</button>
          <button class="fm-btn primary" id="infInstOk"><i class="fas fa-download"></i> Установить</button>
        </div>
      </div>`;

    const modal = this.showModal('<i class="fas fa-box-open"></i> Установка расширения', html, 'sm');
    this._fillExtPermDescriptions(modal, perms);

    document.getElementById('infInstCancel')?.addEventListener('click', () => modal.remove());
    modal.querySelectorAll('.inf-perm-label').forEach(el => {
      const show = () => this._showExtPermDesc(modal, el.dataset.perm);
      el.addEventListener('mouseenter', show);
      el.addEventListener('focusin', show);
    });
    document.getElementById('infInstOk')?.addEventListener('click', async () => {
      const btn = document.getElementById('infInstOk');
      btn.disabled = true;
      btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Установка...';
      const granted = Array.from(modal.querySelectorAll('.inf-ext-perm:checked')).map(c => c.value);
      const sandbox = modal.querySelector('input[name="infSandbox"]:checked')?.value === '1';
      const enableNow = !!document.getElementById('infEnableNow')?.checked;
      const ok = await this.infInstall(path, granted, sandbox, enableNow);
      modal.remove();
      if (ok) this.showNotification(`Расширение «${info.name || ''}» установлено`, 'success');
    });
  }

  async _fillExtPermDescriptions(modal, perms) {
    // Описания кладём на сам modal: он переживает перерисовку списка заданий,
    // а локальная переменная из async-метода там не выжила бы.
    modal._permDesc = modal._permDesc || {};
    if (!perms.length) return;
    try {
      const r = await fetch('/api/extensions/permissions/list-all');
      const d = await r.json();
      const arr = d.permissions || [];
      for (const p of arr) if (p && p.id) modal._permDesc[p.id] = p.description || '';
    } catch (e) { /* не критично */ }
    modal.querySelectorAll('.inf-perm-label').forEach(el => {
      el.title = modal._permDesc[el.dataset.perm] || el.dataset.perm;
    });
  }

  _showExtPermDesc(modal, perm) {
    const box = document.getElementById('infExtPermDesc');
    if (!box) return;
    const d = modal._permDesc && modal._permDesc[perm];
    box.innerHTML = `<b>${this.esc(perm)}</b>${d ? ' — ' + this.esc(d) : ''}`;
  }

  // Установить расширение из .inf. Возвращает true/false.
  async infInstall(path, permissions, sandbox, enableNow) {
    try {
      const res = await fetch('/api/extensions/install-inf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, permissions, sandbox }),
      });
      const d = await res.json();
      if (!d.ok) {
        this.showNotification(d.error || 'Не удалось установить', 'error');
        return false;
      }
      if (enableNow) {
        const name = d.name || (d.extension && d.extension.name);
        try {
          const en = await fetch('/api/extensions/' + encodeURIComponent(name) + '/enable', { method: 'POST' });
          const ed = await en.json();
          if (ed.ok === false && ed.need_sandbox_choice) {
            await fetch('/api/extensions/' + encodeURIComponent(name) + '/sandbox-choice', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ sandbox: sandbox !== false }),
            });
            await fetch('/api/extensions/' + encodeURIComponent(name) + '/enable', { method: 'POST' });
          }
        } catch (e) { /* включение можно повторить тумблером */ }
      }
      return true;
    } catch (e) {
      this.showNotification('Ошибка: ' + e.message, 'error');
      return false;
    }
  }

  // ══════════════════════════════════════════════════════════════
  // Задания AI-агента (cron)
  // ══════════════════════════════════════════════════════════════

  async aiCall(method, endpoint, body) {
    const opt = method === 'GET'
      ? {}
      : { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) };
    const r = await fetch('/api/fm-ai-tasks' + endpoint, opt);
    return r.json();
  }

  // Текущая папка активной панели — она же становится контекстом (workdir)
  // нового задания: агент получает AGENTS.md/контекст папки и стартует файлами в ней.
  currentFolderPath() {
    const p = this.state.panePaths[this.state.activePane] || this.state.panePaths[1] || '';
    return this.isTrashPath(p) ? '' : p;
  }

  async openAiTasks(presetPrompt, presetWorkdir) {
    this._aiPendingPrompt = presetPrompt || '';
    this._aiPendingWorkdir = presetWorkdir || '';
    this._aiLoad();
  }

  async _aiLoad() {
    let jobs = [], hints = [];
    try {
      const d = await this.aiCall('GET', '?include_disabled=true');
      if (!d.ok) this.showNotification(d.error || 'Агент недоступен', 'warn');
      jobs = d.jobs || [];
      hints = d.schedule_hints || [];
    } catch (e) {
      this.showNotification('Ошибка: ' + e.message, 'error');
      hints = ['every day at 2pm', 'every monday 9am', '30m', '0 9 * * *'];
    }
    this.aiTasksModal(jobs, hints, this._aiPendingPrompt || '', this._aiPendingWorkdir || '');
    this._aiPendingPrompt = '';
    this._aiPendingWorkdir = '';
  }

  aiTasksModal(jobs, hints, presetPrompt, presetWorkdir) {
    const rows = jobs.length ? jobs.map(j => this._aiRowHtml(j)).join('') : `<div style="text-align:center;padding:30px 20px;color:var(--text3)">
        <div style="font-size:32px;margin-bottom:10px;opacity:.4"><i class="fas fa-robot"></i></div>
        <div style="font-size:13px">Заданий пока нет</div>
        <div style="font-size:11px;margin-top:6px">Опишите задачу словами — агент выполнит её по расписанию</div>
      </div>`;

    const hintHtml = (hints.length ? hints : ['every day at 2pm']).slice(0, 6)
      .map(h => `<span class="fm-ai-hint">${this.esc(h)}</span>`).join('');

    const html = `
      <div style="display:flex;flex-direction:column;gap:12px">
        <div style="display:flex;gap:8px;align-items:center">
          <div style="flex:1;font-size:13px;font-weight:600"><i class="fas fa-robot" style="color:var(--accent)"></i> Задания AI-агента</div>
          <button class="fm-btn primary" id="aiNewBtn"><i class="fas fa-plus"></i> Новое задание</button>
        </div>
        <div style="font-size:11px;color:var(--text3);line-height:1.5">
          Агент работает с файлами и папками сам. Напишите, что и когда делать, — например:
          <i>«Каждый день в 14:00 собери все текстовые файлы из D:\Docs в D:\Archive, отсортируй картинки по названию»</i>
        </div>
        <div id="aiFormBox" style="display:none"></div>
        <div id="aiList" style="display:flex;flex-direction:column;gap:6px;max-height:46vh;overflow-y:auto">${rows}</div>
        <div>
          <div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:5px">Форматы расписания</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px">${hintHtml}</div>
        </div>
      </div>`;

    const modal = this.showModal('', html, 'lg');
    this._aiJobs = jobs;
    this._aiModal = modal;
    this._aiBind(modal);

    document.getElementById('aiNewBtn')?.addEventListener('click', () => {
      this._aiForm(modal, null, '', this.currentFolderPath());
    });
    if (presetPrompt || presetWorkdir) this._aiForm(modal, null, presetPrompt || '', presetWorkdir || '');
  }

  // Плашка задания в списке. Кнопка «output» открывает вывод агента: каждое
  // выполнение — отдельная сессия, в неё можно посмотреть и ответить.
  _aiRowHtml(j) {
    const state = j.enabled ? '<span style="color:var(--green)">активно</span>'
      : '<span style="color:var(--yellow)">на паузе</span>';
    const next = j.next_run_at ? `след. запуск: ${this.esc(j.next_run_at)}` : 'нет следующего запуска';
    return `<div class="fm-ai-row" data-id="${this.esc(j.id)}">
      <div style="flex:1;min-width:0">
        <div style="font-weight:600;font-size:13px">${this.esc(j.name || j.id)}</div>
        <div style="font-size:11px;color:var(--text3);margin-top:2px">${this.esc(j.schedule || '—')} · ${state} · ${next}</div>
        ${j.workdir ? `<div style="font-size:11px;color:var(--text3);margin-top:3px"><i class="fas fa-folder" style="color:var(--accent)"></i> контекст: ${this.esc(j.workdir)}</div>` : ''}
        ${j.prompt_preview ? `<div style="font-size:11px;color:var(--text2);margin-top:4px;max-height:34px;overflow:hidden">${this.esc(j.prompt_preview)}</div>` : ''}
        ${j.last_error ? `<div style="font-size:11px;color:var(--red);margin-top:4px"><i class="fas fa-triangle-exclamation"></i> ${this.esc(String(j.last_error).slice(0, 160))}</div>` : ''}
      </div>
      <div style="display:flex;gap:4px;flex-shrink:0">
        <button class="fm-btn" data-act="run" title="Выполнить сейчас"><i class="fas fa-play"></i></button>
        <button class="fm-btn" data-act="${j.enabled ? 'pause' : 'resume'}" title="${j.enabled ? 'На паузу' : 'Возобновить'}"><i class="fas fa-${j.enabled ? 'pause' : 'play'}"></i></button>
        <button class="fm-btn" data-act="output" title="Вывод агента"><i class="fas fa-comments"></i></button>
        <button class="fm-btn" data-act="edit" title="Изменить"><i class="fas fa-pen"></i></button>
        <button class="fm-btn" data-act="del" title="Удалить"><i class="fas fa-trash"></i></button>
      </div>
    </div>`;
  }

  // Форма создания/редактирования задания
  _aiForm(modal, job, presetPrompt, presetWorkdir) {
    const box = document.getElementById('aiFormBox');
    if (!box) return;
    const isEdit = !!job;
    const initWorkdir = isEdit ? (job.workdir || '') : (presetWorkdir || '');
    const close = () => { box.style.display = 'none'; box.innerHTML = ''; };
    box.style.display = 'block';
    box.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:10px;padding:13px;background:var(--bg);border:1px solid var(--accent);border-radius:10px">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <b style="font-size:13px">${isEdit ? 'Изменить задание' : 'Новое задание'}</b>
          <button class="fm-btn" id="aiFormClose"><i class="fas fa-times"></i></button>
        </div>
        <div>
          <label style="font-size:11px;color:var(--text3)">Название</label>
          <input class="fm-input" id="aiName" placeholder="Например: Сортировка архива" value="${this.esc(job ? job.name : '')}">
        </div>
        <div>
          <label style="font-size:11px;color:var(--text3)">Расписание</label>
          <input class="fm-input" id="aiSchedule" placeholder="every day at 2pm" value="${this.esc(job ? job.schedule : '')}">
        </div>
        <div>
          <label style="font-size:11px;color:var(--text3)">Что делать агенту</label>
          <textarea class="fm-input" id="aiPrompt" rows="4" placeholder="Каждый день в 14:00 найди все текстовые файлы в папке …">${this.esc(job ? (job.prompt || job.prompt_preview || '') : (presetPrompt || ''))}</textarea>
          <div style="font-size:10px;color:var(--text3);margin-top:4px">Задание выполняется без вас и не может задавать вопросы — опишите всё сразу.</div>
        </div>
        <div>
          <label style="font-size:11px;color:var(--text3)">Контекст: папка для агента</label>
          <div style="display:flex;gap:6px;align-items:center;margin-top:3px">
            <input class="fm-input" id="aiWorkdir" placeholder="D:\\Docs (необязательно)" value="${this.esc(initWorkdir)}" style="flex:1">
            <button class="fm-btn" id="aiWorkdirPick" title="Взять текущую папку панели"><i class="fas fa-crosshairs"></i></button>
            <button class="fm-btn" id="aiWorkdirClear" title="Убрать контекст"><i class="fas fa-xmark"></i></button>
          </div>
          <div style="font-size:10px;color:var(--text3);margin-top:4px"><i class="fas fa-link"></i> Агент получит контекст-файлы этой папки (AGENTS.md) и будет работать с файлами в ней.</div>
        </div>
        ${isEdit ? '' : `<label style="display:flex;gap:8px;align-items:center;font-size:12px;cursor:pointer">
          <input type="checkbox" id="aiRunNow"> Выполнить сразу после создания
        </label>`}
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="fm-btn" id="aiFormCancel">Отмена</button>
          <button class="fm-btn primary" id="aiFormSave"><i class="fas fa-check"></i> ${isEdit ? 'Сохранить' : 'Создать'}</button>
        </div>
      </div>`;

    document.getElementById('aiFormClose')?.addEventListener('click', close);
    document.getElementById('aiFormCancel')?.addEventListener('click', close);
    document.getElementById('aiWorkdirPick')?.addEventListener('click', () => {
      const inp = document.getElementById('aiWorkdir');
      if (inp) inp.value = this.currentFolderPath() || '';
    });
    document.getElementById('aiWorkdirClear')?.addEventListener('click', () => {
      const inp = document.getElementById('aiWorkdir');
      if (inp) inp.value = '';
    });
    document.getElementById('aiFormSave')?.addEventListener('click', async () => {
      const btn = document.getElementById('aiFormSave');
      btn.disabled = true;
      btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Сохраняю...';
      const payload = {
        name: document.getElementById('aiName')?.value.trim(),
        schedule: document.getElementById('aiSchedule')?.value.trim(),
        prompt: document.getElementById('aiPrompt')?.value.trim(),
      };
      // workdir отправляем и пустым: на update это штатное «снять привязку».
      payload.workdir = document.getElementById('aiWorkdir')?.value.trim() || '';
      if (!isEdit) payload.run_now = !!document.getElementById('aiRunNow')?.checked;
      let d;
      try {
        d = await this.aiCall(isEdit ? 'PUT' : 'POST', isEdit ? '/' + encodeURIComponent(job.id) : '', payload);
      } catch (e) {
        d = { ok: false, error: e.message };
      }
      if (!d.ok) {
        this.showNotification(d.error || 'Не удалось сохранить', 'error');
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-check"></i> ' + (isEdit ? 'Сохранить' : 'Создать');
        return;
      }
      this.showNotification(isEdit ? 'Задание обновлено' : 'Задание создано', 'success');
      close();
      this._aiRefresh();
    });
  }

  _aiBind(modal) {
    const jobs = this._aiJobs || [];
    modal.querySelectorAll('.fm-ai-row').forEach(row => {
      const id = row.dataset.id;
      row.querySelectorAll('button[data-act]').forEach(b => {
        b.addEventListener('click', async () => {
          const act = b.dataset.act;
          const job = jobs.find(j => j.id === id);
          if (act === 'edit') { this._aiForm(modal, job, ''); return; }
          if (act === 'output') { this._aiOutput(job); return; }
          if (act === 'del') {
            const yes = await this.confirm(`Удалить задание «${job ? job.name : id}»?`);
            if (!yes) return;
            b.disabled = true;
            const d = await this.aiCall('DELETE', '/' + encodeURIComponent(id));
            if (!d.ok) { this.showNotification(d.error || 'Не удалось удалить', 'error'); b.disabled = false; return; }
            this.showNotification('Задание удалено', 'success');
            this._aiRefresh();
            return;
          }
          b.disabled = true;
          const d = await this.aiCall('POST', '/' + encodeURIComponent(id) + '/' + act);
          b.disabled = false;
          if (!d.ok) { this.showNotification(d.error || 'Не удалось выполнить действие', 'error'); return; }
          const msgs = { run: 'Запущено в фоне', pause: 'Поставлено на паузу', resume: 'Возобновлено' };
          this.showNotification(msgs[act] || 'Готово', 'success');
          this._aiRefresh();
        });
      });
    });
  }

  // Перерисовать список внутри уже открытой модалки — не переоткрываем окно,
  // чтобы пользователь не терял открытую форму и позицию прокрутки.
  async _aiRefresh() {
    let jobs = [], hints = [];
    try {
      const d = await this.aiCall('GET', '?include_disabled=true');
      jobs = d.jobs || [];
      hints = d.schedule_hints || [];
    } catch (e) {
      return;
    }
    this._aiJobs = jobs;
    const old = this._aiModal;
    if (!old || !old.isConnected) return;
    // Пересобираем только список: форма поверх него остаётся нетронутой.
    const keepForm = document.getElementById('aiFormBox')?.innerHTML || '';
    const wasFormOpen = document.getElementById('aiFormBox')?.style.display === 'block';
    const rows = jobs.length ? jobs.map(j => this._aiRowHtml(j)).join('')
      : `<div style="text-align:center;padding:24px;color:var(--text3);font-size:12px">Заданий пока нет</div>`;

    const list = document.getElementById('aiList');
    if (list) list.innerHTML = rows;
    this._aiBind(old);
    // Форму после перерисовки списка не трогаем: она живёт в отдельном блоке.
    if (wasFormOpen && keepForm) {
      const box = document.getElementById('aiFormBox');
      if (box) { box.innerHTML = keepForm; box.style.display = 'block'; }
    }
  }

  // ══════════════════════════════════════════════════════════════
  // Вывод агента: transcript последнего (или выбранного) выполнения
  // задания и ответ агенту в ту же сессию.
  // ══════════════════════════════════════════════════════════════

  _fmtRunTs(ts) {
    const n = Number(ts);
    if (!n) return '—';
    try { return new Date(n > 1e11 ? n : n * 1000).toLocaleString(); } catch (e) { return String(ts); }
  }

  async _aiOutput(job) {
    if (!job) return;
    let runs = [];
    try {
      const d = await this.aiCall('GET', '/' + encodeURIComponent(job.id) + '/runs');
      if (!d.ok && d.error) { this.showNotification(d.error, 'error'); return; }
      runs = d.runs || [];
    } catch (e) {
      this.showNotification('Ошибка: ' + e.message, 'error');
      return;
    }
    if (!runs.length) {
      this.showNotification('Задание ещё ни разу не выполнялось', 'warn');
      return;
    }
    this._aiOutState = { job, runs, sid: runs[0].session_id };
    const runOpts = runs.map((r, i) =>
      `<option value="${this.esc(r.session_id)}" ${i === 0 ? 'selected' : ''}>${this.esc(this._fmtRunTs(r.started_at))} · ${r.message_count || 0} сообщ.</option>`).join('');
    const html = `
      <div style="display:flex;flex-direction:column;gap:10px">
        <div style="display:flex;gap:8px;align-items:center">
          <div style="flex:1;font-size:13px;font-weight:600"><i class="fas fa-comments" style="color:var(--accent)"></i> Вывод агента: ${this.esc(job.name || job.id)}</div>
          <select class="fm-select" id="aiOutRun" style="font-size:11px;padding:3px 6px;width:auto" title="Выбрать запуск">${runOpts}</select>
        </div>
        <div id="aiOutMsgs" style="display:flex;flex-direction:column;gap:8px;max-height:52vh;overflow-y:auto;padding:4px 2px"></div>
        <div style="display:flex;gap:8px;align-items:flex-end">
          <textarea class="fm-input" id="aiOutInput" rows="2" style="flex:1" placeholder="Ответить агенту в эту сессию…"></textarea>
          <button class="fm-btn primary" id="aiOutSend"><i class="fas fa-paper-plane"></i> Отправить</button>
        </div>
        <div style="font-size:10px;color:var(--text3)">Каждое выполнение задания — отдельная сессия агента. Ответ уходит в сессию выбранного запуска, контекст задания при этом сохраняется.</div>
      </div>`;
    const modal = this.showModal('', html, 'lg');
    this._aiOutModal = modal;

    const load = async (sid) => {
      const box = document.getElementById('aiOutMsgs');
      if (!box) return;
      box.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:10px"><i class="fas fa-spinner fa-spin"></i> Загружаю вывод…</div>';
      let d;
      try {
        d = await this.aiCall('GET', '/' + encodeURIComponent(job.id) + '/output?session_id=' + encodeURIComponent(sid));
      } catch (e) { d = { ok: false, error: e.message }; }
      if (!modal.isConnected) return;
      if (!d.ok) { box.innerHTML = `<div style="color:var(--red);font-size:12px;padding:10px">${this.esc(d.error || 'Не удалось загрузить вывод')}</div>`; return; }
      this._aiOutRender(box, d.messages || []);
    };

    document.getElementById('aiOutRun')?.addEventListener('change', (e) => {
      this._aiOutState.sid = e.target.value;
      load(e.target.value);
    });
    const send = async () => {
      const inp = document.getElementById('aiOutInput');
      const text = (inp?.value || '').trim();
      if (!text) return;
      const btn = document.getElementById('aiOutSend');
      btn.disabled = true;
      btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Думаю…';
      const box = document.getElementById('aiOutMsgs');
      this._aiOutAppend(box, { role: 'user', text });
      inp.value = '';
      let d;
      try {
        d = await this.aiCall('POST', '/' + encodeURIComponent(job.id) + '/chat',
          { session_id: this._aiOutState.sid, message: text });
      } catch (e) { d = { ok: false, error: e.message }; }
      btn.disabled = false;
      btn.innerHTML = '<i class="fas fa-paper-plane"></i> Отправить';
      if (!modal.isConnected) return;
      if (!d.ok) { this._aiOutAppend(box, { role: 'assistant', text: '⚠ ' + (d.error || 'Агент не ответил'), err: true }); return; }
      if (d.session_id) this._aiOutState.sid = d.session_id;
      this._aiOutAppend(box, { role: 'assistant', text: d.reply || '' });
    };
    document.getElementById('aiOutSend')?.addEventListener('click', send);
    document.getElementById('aiOutInput')?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });

    load(this._aiOutState.sid);
  }

  _aiOutRender(box, messages) {
    box.innerHTML = '';
    if (!messages.length) {
      box.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:10px">В этой сессии пока нет текста — возможно, агент только выполнял действия.</div>';
      return;
    }
    messages.forEach(m => this._aiOutAppend(box, m));
  }

  _aiOutAppend(box, m) {
    if (!box) return;
    const isUser = m.role === 'user';
    const div = document.createElement('div');
    div.style.cssText = `max-width:85%;align-self:${isUser ? 'flex-end' : 'flex-start'};padding:8px 11px;border-radius:10px;font-size:12px;line-height:1.5;white-space:pre-wrap;word-break:break-word;background:${isUser ? 'var(--bg-active)' : 'var(--bg)'};border:1px solid ${m.err ? 'var(--red)' : 'var(--border)'}`;
    div.textContent = m.text || '';
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }
}


// ─── INIT ─────────────────────────────────────────────
let FM;
// Класс наружу: мобильный слой (fm_mobile.js) наследуется от FileManagerApp,
// не копируя его. В мобильном шаблоне инициализацию desktop-экземпляра
// пропускаем — там window.FM ставит MobileFM (иначе два инстанса на одной
// странице тянут загрузку и обработчики дважды).
window.FileManagerApp = FileManagerApp;
document.addEventListener('DOMContentLoaded', () => {
  if (window.FM_MOBILE_MODE) return;
  FM = new FileManagerApp();
  window.FM = FM;
});
