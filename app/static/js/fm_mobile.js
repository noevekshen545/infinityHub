/* =====================================================================
   Infinity Search — мобильный файловый менеджер
   ---------------------------------------------------------------------
   Наследник desktop-класса FileManagerApp (static/js/file_manager.js):
   транспорт, навигация, история, буфер, шаринг, шифрование, AI-задачи,
   модалки и просмотрщик переиспользуются БЕЗ изменений; переписан только
   слой вывода под мобильный UX:
   - сетка/список -> mh-row/mh-cell (тап = открыть, долгий тап = меню);
   - контекстное меню -> bottom-sheet из того же массива пунктов
     (buildItemMenuEntries / buildEmptyMenuEntries);
   - ribbon -> нижняя панель действий (селект-режим + фон-меню);
   - tree -> drawer; dual-pane/tabs отключены (pane всегда 1).
   Паттерны (cut/copy/delete/rename) совпадают с desktop showContextMenu.
   ===================================================================== */
(function () {
  'use strict';

  window.FM_MOBILE_MODE = true;
  var PANE = 1;

  function $(sel) { return document.querySelector(sel); }

  /* Класс и подписка создаются внутри функции: extends читается в момент
     вычисления class-выражения, а window.FileManagerApp появляется только
     когда выполнен file_manager.js ( defer-порядок). */
  function defineMobileFM() {
    if (window.MobileFMApp) return;
    window.MobileFMApp = class MobileFM extends window.FileManagerApp {

    /* ---------------- init ---------------- */
    async init() {
      this.mobile = true;
      this.state.layout = 'single';
      this.state.dualSync = false;
      this.state.viewModes = { 1: 'grid', 2: 'grid' };
      this.bindGlobalTheme();
      this.setTheme(this.state.theme, false);
      await this.loadTrash();
      this.loadDisks();
      await this.loadProfileRoot();
      this.loadPlatform();
      this.loadAssociations();
      this.loadEncryptionStatus();
      this.mountMobileUI();
      var _p = new URLSearchParams(location.search).get('path');
      var startPath = _p || this.state.profileRoot || '';
      this.navigate(PANE, startPath);
      this.checkIncomingShares(true, location.search.indexOf('incoming=1') >= 0);
    }

    mountMobileUI() {
      var self = this;
      this.mhBar = $('#mhBar');
      this.mhMain = $('.mh-main');
      $('#mhNavBtn').addEventListener('click', function () { self.openNavSheet(); });
      $('#mhSearchBtn').addEventListener('click', function () { self.openSearchSheet(); });
      $('#mhViewBtn').addEventListener('click', function () { self.toggleGridList(); });
      $('#mhMoreBtn').addEventListener('click', function () { self.openModuleSheet(); });
      $('#mhFab').addEventListener('click', function () { self.openBgSheet(); });
      document.querySelectorAll('#mhFilterChips .mh-chip').forEach(function (chip) {
        chip.addEventListener('click', function () {
          self.setFilter(chip.dataset.filter);
          document.querySelectorAll('#mhFilterChips .mh-chip').forEach(function (c) {
            c.classList.toggle('on', c === chip);
          });
        });
      });
      // Долгий тап по пустому месту = «фон»-меню desktop-версии.
      var listEl = $('#fileList1');
      if (listEl) {
        window.MShell.longPress(this.mhMain, function () { self.openBgSheet(); }, { ms: 520 });
        listEl.addEventListener('contextmenu', function (e) {
          if (!e.target.closest('.mh-row,.mh-cell')) { e.preventDefault(); self.openBgSheet(); }
        });
      }
      // Загрузка перетаскиванием OS-файлов (desktop bindEvents делает то же).
      document.addEventListener('dragover', function (e) { e.preventDefault(); });
      document.addEventListener('drop', function (e) {
        e.preventDefault();
        var fs = e.dataTransfer && e.dataTransfer.files;
        if (fs && fs.length) self.uploadFiles(fs);
      });
    }

    /* ---------------- render ---------------- */
    render(pane) {
      if (pane !== PANE) return;
      var el = document.querySelector('.fm-file-list[data-pane="' + pane + '"]');
      if (!el) return;
      this.mhListEl = el;
      var items = this.state.paneItems[pane];
      if (this.isTrashPath(this.state.panePaths[pane])) {
        this.mhRenderTrash(el, items || []);
        this.renderMobileBar();
        return;
      }
      if (!items || !items.length) {
        el.innerHTML = '<div class="mh-empty"><i class="fas fa-folder-open"></i>Папка пуста</div>';
        this.renderMobileBar();
        return;
      }
      var sorted = this.sortItems(items);
      if (this.state.viewModes[pane] === 'grid') this.mhRenderGrid(el, sorted);
      else this.mhRenderList(el, sorted);
      this.renderMobileBar();
    }

    mhThumb(item) {
      if (item.media_type === 'image') {
        return '<img loading="lazy" src="/filemanager/thumbnail?path=' +
          encodeURIComponent(item.path) + '" alt="">';
      }
      return this.getIcon(item);
    }

    mhMeta(item) {
      var bits = [];
      if (item.type === 'file') bits.push(this.formatSize(item.size));
      else bits.push('папка');
      if (item.modified) bits.push(this._fmtDate(item.modified));
      return this.esc(bits.filter(Boolean).join(' · '));
    }

    mhBindRow(node, path) {
      var self = this;
      node.dataset.path = path;
      node.addEventListener('click', function (e) {
        e.preventDefault();
        self.mhTap(path, node);
      });
      window.MShell.longPress(node, function (e) {
        e.preventDefault && e.preventDefault();
        self.mhRowMenu(path);
      });
      node.addEventListener('contextmenu', function (e) {
        e.preventDefault();
        self.mhRowMenu(path);
      });
    }

    mhRenderGrid(el, items) {
      var sel = this.state.selectedItems[PANE];
      var html = '<div class="mh-grid">';
      for (var i = 0; i < items.length; i++) {
        var it = items[i], isSel = sel.has(it.path);
        html += '<div class="mh-cell' + (isSel ? ' sel' : '') + '" data-i="' + i + '">' +
          '<div class="mh-cell-ico">' + this.mhThumb(it) + '</div>' +
          '<div class="mh-cell-name">' + this.esc(it.name) + '</div>' +
          (isSel ? '<div class="mh-row-check"><i class="fas fa-check"></i></div>' : '') +
          '</div>';
      }
      html += '</div>';
      el.innerHTML = html;
      var nodes = el.querySelectorAll('.mh-cell');
      for (var k = 0; k < nodes.length; k++) this.mhBindRow(nodes[k], items[k].path);
    }

    mhRenderList(el, items) {
      var sel = this.state.selectedItems[PANE];
      var html = '<div class="mh-list">';
      for (var i = 0; i < items.length; i++) {
        var it = items[i], isSel = sel.has(it.path);
        html += '<div class="mh-row' + (isSel ? ' sel' : '') + '" data-i="' + i + '">' +
          '<div class="mh-row-ico">' + this.mhThumb(it) + '</div>' +
          '<div class="mh-row-main">' +
          '<div class="mh-row-name">' + this.esc(it.name) + '</div>' +
          '<div class="mh-row-meta">' + this.mhMeta(it) + '</div>' +
          '</div>' +
          (isSel ? '<div class="mh-row-check"><i class="fas fa-check"></i></div>' : '') +
          '</div>';
      }
      html += '</div>';
      el.innerHTML = html;
      var nodes = el.querySelectorAll('.mh-row');
      for (var k = 0; k < nodes.length; k++) this.mhBindRow(nodes[k], items[k].path);
    }

    mhRenderTrash(el, items) {
      var self = this, sel = this.state.selectedItems[PANE];
      if (!items.length) {
        el.innerHTML = '<div class="mh-empty"><i class="fas fa-trash-can"></i>Корзина пуста</div>';
        return;
      }
      var html = '<div class="mh-list">';
      for (var i = 0; i < items.length; i++) {
        var it = items[i], isSel = sel.has(it.path || it.name);
        html += '<div class="mh-row' + (isSel ? ' sel' : '') + '" data-i="' + i + '">' +
          '<div class="mh-row-ico"><i class="fas fa-trash-can"></i></div>' +
          '<div class="mh-row-main">' +
          '<div class="mh-row-name">' + this.esc(it.name) + '</div>' +
          '<div class="mh-row-meta">было: ' + this.esc(it.original_path || '') + '</div>' +
          '</div></div>';
      }
      html += '</div>';
      el.innerHTML = html;
      el.querySelectorAll('.mh-row').forEach(function (node, idx) {
        var it = items[idx];
        node.addEventListener('click', function () {
          self.toggleSelect(PANE, it.path || it.name);
        });
        window.MShell.longPress(node, function () {
          self.mhTrashMenu(it, items);
        });
      });
    }

    mhTap(path, node) {
      var item = (this.state.paneItems[PANE] || []).find(function (i) { return i.path === path; });
      if (this.state.checkboxMode || (this.state.selectedItems[PANE].size && !item)) {
        this.toggleSelect(PANE, path); return;
      }
      if (this.state.selectedItems[PANE].size && !this.state.checkboxMode) {
        // выделение есть: тап по элементу — пересоздать выделение на него
        this.state.selectedItems[PANE].clear();
      }
      if (!item) { this.toggleSelect(PANE, path); return; }
      if (item.type === 'directory') { this.navigate(PANE, path); return; }
      if (this.state.checkboxMode) { this.toggleSelect(PANE, path); return; }
      this.openFile(PANE, item);
    }

    /* ---------------- нижняя панель ---------------- */
    renderMobileBar() {
      var self = this;
      var bar = this.mhBar;
      if (!bar) return;
      var sel = this.state.selectedItems[PANE];
      var inTrash = this.isTrashPath(this.state.panePaths[PANE]);
      bar.innerHTML = '';
      function btn(icon, label, onClick, cls, disabled) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'mh-barbtn' + (cls ? ' ' + cls : '');
        b.innerHTML = '<i class="fas fa-' + icon + '"></i><span>' + label + '</span>';
        if (disabled) b.disabled = true;
        else b.addEventListener('click', onClick);
        bar.appendChild(b);
        return b;
      }
      var crumbEl = $('#mhCrumbs');
      if (sel.size) {
        btn('xmark', 'Снять', function () { sel.clear(); self.render(PANE); });
        if (!inTrash) {
          btn('copy', 'Копир.', function () { self.mhClipboard('copy'); });
          btn('scissors', 'Вырез.', function () { self.mhClipboard('cut'); });
          btn('trash', 'Удалить', function () { self.deleteSelected(PANE); }, 'danger');
          btn('share-nodes', 'Шера', function () { self.shareSelected(PANE); });
          btn('ellipsis', 'Ещё', function () { self.mhSelectionSheet(); });
        } else {
          btn('undo', 'Восст.', function () { self.mhTrashAction('restore'); });
          btn('trash', 'Удалить', function () { self.mhTrashAction('delete'); }, 'danger');
        }
      } else {
        btn('bars', 'Меню', function () { self.openNavSheet(); });
        btn('arrow-up', 'Вверх', function () { self.goUp(PANE); });
        btn('rotate-right', 'Обнов.', function () { self.navigate(PANE, self.state.panePaths[PANE]); });
        btn('square-check', this.state.checkboxMode ? 'Выкл. выбор' : 'Выбор',
            function () { self.toggleCheckboxMode(); self.render(PANE); },
            this.state.checkboxMode ? 'on' : '');
        btn('plus', 'Создать', function () { self.openBgSheet(); });
      }
      var sub = $('#mhSubtitle');
      var items = this.state.paneItems[PANE] || [];
      if (sub) {
        sub.textContent = (inTrash ? 'Корзина · ' : '') +
          (sel.size ? 'выбрано ' + sel.size : items.length + ' эл.');
      }
      if (crumbEl) this.renderBreadcrumbs(PANE);
    }

    /* ---------------- буфер (паттерн как desktop toolbar) ------------- */
    mhClipboard(action) {
      var sel = Array.from(this.state.selectedItems[PANE]);
      if (!sel.length) return;
      this.state.clipboard = { items: sel, action: action };
      this.apiJson('POST', '/clipboard/set', { items: sel, action: action });
      this.showNotification((action === 'cut' ? 'Вырезано: ' : 'Скопировано: ') + sel.length + ' эл.',
        action === 'cut' ? 'warn' : 'info');
      this.renderClipboardIndicators(PANE);
    }

    mhSelectionSheet() {
      var self = this;
      var sel = Array.from(this.state.selectedItems[PANE]);
      if (!sel.length) return;
      var path = sel[0];
      var act = function (fn) { return function (ctx) { ctx.close(); fn(); }; };
      window.MShell.sheet({
        title: 'Выбрано: ' + sel.length,
        actionsList: [
          { icon: 'pen', label: 'Переименовать', disabled: sel.length !== 1,
            onClick: act(function () { self.renameSelected(PANE); }) },
          { icon: 'i-cursor', label: 'Массовое переименование', disabled: sel.length < 2,
            onClick: act(function () { self.openBatchRename(); }) },
          { icon: 'file-zipper', label: 'Создать архив',
            onClick: act(function () { self.createArchive(PANE); }) },
          { icon: 'download', label: 'Скачать',
            onClick: act(function () { self.downloadSelected(PANE); }) },
          { icon: 'share-alt', label: 'Поделиться…', disabled: sel.length !== 1,
            onClick: act(function () { self.shareSelected(PANE); }) },
          { icon: 'folder', label: 'Переместить в...', disabled: sel.length !== 1,
            onClick: act(function () { self.moveToDialog(path, PANE); }) },
          { icon: 'star', label: 'В избранное',
            onClick: act(function () { sel.forEach(function (p) { self.toggleFavorite(p); }); }) },
          { sep: true },
          { icon: 'lock', label: 'Зашифровать', hidden: !this.state.encryptionEnabled,
            onClick: act(function () { self.encryptSelected(PANE); }) },
          { icon: 'lock-open', label: 'Расшифровать',
            onClick: act(function () { self.decryptSelected(PANE); }) },
          { sep: true },
          { icon: 'trash', label: 'Удалить навсегда', danger: true,
            onClick: act(function () { self.deleteSelected(PANE, true); }) }
        ]
      });
    }

    mhTrashAction(kind) {
      var sel = this.state.selectedItems[PANE];
      var items = this.state.paneItems[PANE] || [];
      var ids = items.filter(function (i) { return sel.has(i.path || i.name); })
                     .map(function (i) { return i.id || i.name; });
      if (!ids.length) return;
      if (kind === 'restore') this.trashRestore(ids);
      else this.trashDeleteForever(ids);
    }

    mhTrashMenu(it, items) {
      var self = this;
      var act = function (fn) { return function (ctx) { ctx.close(); fn(); }; };
      var only = [it.path || it.name];
      window.MShell.sheet({
        title: it.name,
        actionsList: [
          { icon: 'undo', label: 'Восстановить',
            onClick: act(function () { self.trashRestore(only); }) },
          { icon: 'copy', label: 'Восстановить как…',
            onClick: act(function () { self.restoreAsDialog ? self.restoreAsDialog(PANE, it) : self.showNotification('Недоступно', 'warn'); }) },
          { icon: 'copy', label: 'Копировать путь',
            onClick: act(function () { self.copyText(it.original_path || ''); }) },
          { sep: true },
          { icon: 'trash', label: 'Удалить навсегда', danger: true,
            onClick: act(function () { self.trashDeleteForever(only); }) },
          { icon: 'broom', label: 'Очистить корзину', danger: true,
            onClick: act(function () { self.trashEmpty(); }) }
        ]
      });
    }

    /* ---------------- лист элемента (долгий тап) ---------------- */
    mhRowMenu(path) {
      var self = this;
      var inTrash = this.isTrashPath(this.state.panePaths[PANE]);
      if (inTrash) {
        var itT = (this.state.paneItems[PANE] || []).find(function (i) { return (i.path || i.name) === path; }) ||
                  (this.state.paneItems[PANE] || []).find(function (i) { return i.path === path; });
        return this.mhTrashMenu(itT || { name: path.split(/[/\\]/).pop() || path },
                                this.state.paneItems[PANE] || []);
      }
      var item = (this.state.paneItems[PANE] || []).find(function (i) { return i.path === path; });
      var entries = this.buildItemMenuEntries(PANE, item, path);
      var tbEntries = [
        { icon: 'scissors', label: 'Вырезать', act: 'cut' },
        { icon: 'copy', label: 'Скопировать', act: 'copy' }
      ];
      var tiles = document.createElement('div');
      tiles.className = 'mh-tiles';
      tbEntries.forEach(function (t) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'mh-tile';
        b.innerHTML = '<i class="fas fa-' + t.icon + '"></i><span>' + t.label + '</span>';
        b.addEventListener('click', function () {
          var selN = self.state.selectedItems[PANE];
          var arr = selN.size > 0 ? Array.from(selN) : [path];
          self.state.clipboard = { items: arr, action: t.act };
          self.apiJson('POST', '/clipboard/set', { items: arr, action: t.act });
          self.showNotification((t.act === 'cut' ? 'Вырезано: ' : 'Скопировано: ') + arr.length + ' эл.',
            t.act === 'cut' ? 'warn' : 'info');
          self.renderClipboardIndicators(PANE);
          MShell.closeSheet();
        });
        tiles.appendChild(b);
      });
      [['paste', 'Вставить', function () { self.pasteTo(PANE); }],
       ['pen', 'Переименовать', function () {
          self.state.selectedItems[PANE].clear();
          self.state.selectedItems[PANE].add(path);
          self.renameSelected(PANE); }],
       ['trash', 'Удалить', function () {
          self.state.selectedItems[PANE].clear();
          self.state.selectedItems[PANE].add(path);
          self.deleteSelected(PANE); }]].forEach(function (t) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'mh-tile';
        b.innerHTML = '<i class="fas fa-' + t[0] + '"></i><span>' + t[1] + '</span>';
        b.addEventListener('click', function () { MShell.closeSheet(); t[2](); });
        tiles.appendChild(b);
      });
      // desktop-пункт «Открыть в новой панели» на мобильном теряет смысл:
      // панель одна, переход и так в неё.
      var actionsList = entries.filter(function (e2) {
        return !(e2.label && e2.label.indexOf('в новой панели') >= 0);
      }).map(function (e2) {
        if (e2.sep) return { sep: true };
        return { icon: e2.icon, label: e2.label.replace(/…$/, ''), danger: false,
                 onClick: function (ctx) { ctx.close(); e2.action(); } };
      });
      window.MShell.sheet({
        title: item ? item.name : (path || 'Файлы'),
        body: tiles,
        actionsList: actionsList,
        onClose: null
      });
    }

    /* ---------------- фоновое меню (пустое место / FAB) ------------- */
    openBgSheet() {
      var self = this;
      var entries = this.buildEmptyMenuEntries(PANE);
      var actionsList = entries.map(function (e2) {
        if (e2.sep) return { sep: true };
        return { icon: e2.icon, label: e2.label.replace(/…$/, ''),
                 onClick: function (ctx) { ctx.close(); e2.action(); } };
      });
      actionsList.push({ sep: true });
      actionsList.push({ icon: 'upload', label: 'Загрузить файлы',
        onClick: function (ctx) { ctx.close(); self.mhUpload(); } });
      var hasClip = this.state.clipboard.items && this.state.clipboard.items.length;
      if (hasClip) actionsList.unshift({ icon: 'paste', label: 'Вставить',
        onClick: function (ctx) { ctx.close(); self.pasteTo(PANE); } });
      window.MShell.sheet({ title: 'Создать', actionsList: actionsList });
    }

    /* ---------------- drawer навигации ---------------- */
    openNavSheet() {
      var self = this;
      var body = document.createElement('div');
      var html = '';
      html += '<div class="mh-nav-section"><div class="mh-label">Назад во времени</div></div>';
      var drives = ($('#fmDrives') || {}).innerHTML || '';
      var tree = ($('#fmTreeContent') || {}).innerHTML || '';
      var wrap = document.createElement('div');
      wrap.className = 'fm-tree mh-tree-clone';
      wrap.innerHTML = '<div class="fm-drive-indicator">' + drives + '</div>' + tree;
      body.appendChild(wrap);
      var s = window.MShell.sheet({ title: 'Навигация', body: body });
      wrap.querySelectorAll('.fm-tree-item,.fm-drive-item').forEach(function (n) {
        n.addEventListener('click', function () {
          var p = n.dataset.path || '';
          s.close();
          self.navigate(PANE, p);
        });
      });
      // доступы/корзина — пункты, которых в дереве desktop может и не быть
      var acts = document.createElement('div');
      acts.style.marginTop = '8px';
      body.appendChild(acts);
      [{ icon: 'inbox', label: 'Доступы мне', fn: function () { self.openIncomingDialog(); } },
       { icon: 'trash-can', label: 'Корзина (' + (this.state.trash.total || 0) + ')',
         fn: function () { self.openTrash(); } }].forEach(function (a) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'mh-action';
        b.innerHTML = '<i class="fas fa-' + a.icon + '"></i><span>' + a.label + '</span>';
        b.addEventListener('click', function () { MShell.closeSheet(); a.fn(); });
        acts.appendChild(b);
      });
    }

    /* ---------------- лист модуля (ellipsis) ---------------- */
    openModuleSheet() {
      var self = this;
      var go = function (fn) { return function (ctx) { ctx.close(); fn(); }; };
      window.MShell.sheet({
        title: 'Файловый менеджер',
        actionsList: [
          { icon: 'inbox', label: 'Доступы мне',
            onClick: go(function () { self.openIncomingDialog(); }) },
          { icon: 'trash-can', label: 'Корзина',
            onClick: go(function () { self.openTrash(); }) },
          { icon: 'robot', label: 'AI-задания',
            onClick: go(function () { self.openAiTasks(); }) },
          { icon: 'terminal', label: 'Терминал',
            onClick: go(function () { self.openTerminal(); }) },
          { sep: true },
          { icon: 'eye-slash', label: this.state.showHidden ? 'Скрыть скрытые файлы' : 'Показать скрытые',
            onClick: go(function () { self.toggleHidden(); self.render(PANE); }) },
          { icon: this.state.encryptionEnabled ? 'lock-open' : 'lock',
            label: this.state.encryptionEnabled ? 'Шифрование: вкл' : 'Шифрование: выкл',
            onClick: go(function () { self.toggleEncryption(!self.state.encryptionEnabled); }) },
          { icon: 'rotate-left', label: 'Отменить', onClick: go(function () { self.undo(); }) },
          { icon: 'rotate-right', label: 'Повторить', onClick: go(function () { self.redo(); }) },
          { sep: true },
          { icon: 'palette', label: 'Тема: ' + this.state.theme,
            onClick: go(function () { self.toggleTheme(); }) },
          { icon: 'gear', label: 'Настройки модуля',
            onClick: go(function () { self.openSettings(); }) },
          { icon: 'desktop', label: 'Desktop-версия',
            onClick: go(function () { window.MShell.forceDesktop(); }) }
        ]
      });
    }

    /* ---------------- поиск ---------------- */
    openSearchSheet() {
      var self = this;
      var s = window.MShell.sheet({
        title: 'Поиск',
        body: '<div class="mh-field"><label class="mh-label">Режим</label>' +
              '<select class="mh-select" id="mhSearchMode">' +
              '<option value="name">По имени</option><option value="content">По содержимому</option>' +
              '</select></div>' +
              '<div class="mh-field"><input class="mh-input" id="mhSearchInput" placeholder="имя файла…" autocomplete="off"></div>',
        actions: [
          { label: 'Отмена', onClick: function (ctx) { ctx.close(); } },
          { label: 'Искать', kind: 'primary', onClick: function (ctx) {
              var q = document.getElementById('mhSearchInput').value.trim();
              var mode = document.getElementById('mhSearchMode').value;
              ctx.close();
              if (q) self.mhRunSearch(q, mode);
            } }
        ],
        onMount: function () {
          setTimeout(function () { document.getElementById('mhSearchInput').focus(); }, 150);
        }
      });
    }

    async mhRunSearch(query, mode) {
      var pane = PANE;
      try {
        if (mode === 'content') {
          var data = await this.apiJson('POST', '/search-content',
            { path: this.state.panePaths[pane] || '', query: query });
          this.state.searchQuery = query;
          this.state.paneItems[pane] = data.items || [];
          this.state.panePaths[pane] = (data.root || this.state.panePaths[pane] || '') ;
          this.render(pane);
          this.showNotification('Совпадений: ' + (this.state.paneItems[pane] || []).length, 'info');
        } else {
          var d = await this.api('/search?path=' + encodeURIComponent(this.state.panePaths[pane] || '') +
            '&query=' + encodeURIComponent(query) + '&max_results=200');
          this.state.paneItems[pane] = d.items || [];
          this.render(pane);
          this.showNotification('Найдено: ' + (this.state.paneItems[pane] || []).length, 'info');
        }
      } catch (err) { this.showNotification(err.message, 'error'); }
    }

    /* ---------------- загрузка файлов (input вместо desktop upload) --- */
    mhUpload() {
      var self = this;
      var inp = document.createElement('input');
      inp.type = 'file';
      inp.multiple = true;
      inp.style.display = 'none';
      document.body.appendChild(inp);
      inp.addEventListener('change', function () {
        if (inp.files && inp.files.length) self.uploadFiles(inp.files);
        setTimeout(function () { inp.remove(); }, 400);
      });
      inp.click();
    }

    /* ---------------- хлебные кропки ---------------- */
    renderBreadcrumbs(pane) {
      var el = $('#mhCrumbs');
      if (!el || pane !== PANE) return;
      var path = this.state.panePaths[pane] || '';
      var self = this;
      var sep = path.indexOf('\\') >= 0 ? '\\' : '/';
      var parts = path.split(sep).filter(Boolean);
      var html = '<button class="mh-crumb" type="button" data-p="__edit" title="Ввести путь">' +
                 '<i class="fas fa-pencil"></i></button>';
      var cum = '';
      var firstSep = function () {
        return cum ? '<span class="mh-sep">' + (sep === '\\' ? '\\' : '/') + '</span>' : '';
      };
      var driveCrumbCur = false;
      if (path.indexOf(':') >= 0) {
        var m = path.match(/^([A-Za-z]:)/);
        if (m) {
          cum = m[1] + sep;
          // drive уже выделен отдельной кропкой — иначе он удваивается
          // и кумулятивный путь превращается в "E:\E:\..."
          if (parts.length && parts[0].toUpperCase() === m[1].toUpperCase()) parts = parts.slice(1);
          driveCrumbCur = parts.length === 0;
          html += '<button class="mh-crumb' + (driveCrumbCur ? ' cur' : '') +
                  '" data-p="' + this.escAttr(cum) + '">' + this.esc(m[1]) + '</button>';
        }
      } else if (path.charAt(0) === '/') {
        cum = '/';
        html += '<button class="mh-crumb" data-p="/">/</button>';
      }
      for (var i = 0; i < parts.length; i++) {
        if (cum && cum.slice(-1) === sep) cum = cum + parts[i];
        else if (cum) cum = cum + sep + parts[i];
        else cum = parts[i];
        var active = i === parts.length - 1 ? ' cur' : '';
        html += firstSep() +
          '<button class="mh-crumb' + active + '" data-p="' + this.escAttr(cum) + '">' +
          this.esc(parts[i]) + '</button>';
      }
      el.innerHTML = html;
      el.querySelectorAll('.mh-crumb').forEach(function (b) {
        b.addEventListener('click', function () {
          var p = b.dataset.p;
          if (p === '__edit') { self.showPathInput(PANE); return; }
          self.navigate(PANE, p);
        });
      });
      var cur = el.querySelector('.mh-crumb.cur');
      if (cur) cur.scrollIntoView({ block: 'nearest', inline: 'end' });
    }

    showPathInput(pane) {
      var self = this;
      var current = this.state.panePaths[PANE] || '';
      window.MShell.prompt({ title: 'Путь', label: 'Перейти по пути', value: current })
        .then(function (v) { if (v != null && v !== '') self.navigate(PANE, v); });
    }

    /* ---------------- вид ---------------- */
    toggleGridList() {
      var cur = this.state.viewModes[PANE];
      this.state.viewModes[PANE] = cur === 'grid' ? 'list' : 'grid';
      this.render(PANE);
    }

    toggleViewMode(pane, mode) {
      if (!mode) mode = 'grid';
      this.state.viewModes[PANE] = mode;
      this.render(PANE);
    }

    setSort(field) {
      if (this.state.sortBy === field) this.state.sortAsc = !this.state.sortAsc;
      else { this.state.sortBy = field; this.state.sortAsc = true; }
      this.render(PANE);
    }

    setFilter(f) {
      this.state.fileFilter = f;
      this.navigate(PANE, this.state.panePaths[PANE]);
    }

    /* ---------------- отключённое desktop-наследие ---------------- */
    bindRubberBand() {}
    onItemDblClick() {}
    renderTabs() {}
    updateTabTitle() {}
    renderInfo() {}
    renderStatusBar() {}
    showContextMenu(e, pane) {
      if (e && e.currentTarget && e.currentTarget.dataset && e.currentTarget.dataset.path !== undefined) {
        var p = e.currentTarget.dataset.path;
        if (p) return this.mhRowMenu(p);
      }
      return this.openBgSheet();
    }
    showEmptyContextMenu() { this.openBgSheet(); }
    showTrashContextMenu() {}
    showPreview(pane, path) {
      // desktop-панель превью на мобильном закрыта — открываем просмотрщик
      var item = (this.state.paneItems[PANE] || []).find(function (i) { return i.path === path; });
      if (!item) return;
      var t = this.viewerTypeFor ? this.viewerTypeFor(item) : null;
      if (t) this.openFullscreen(t, path);
      else this.openFile(PANE, item);
    }

    appWindow(title, url) {
      // WinBox нет на мобильном: полноэкранный iframe-viewer
      var host = document.createElement('div');
      host.className = 'mh-viewer open';
      host.innerHTML =
        '<div class="mh-viewer-head"><div class="mh-title">' + window.MShell.esc(title) +
        '</div><button class="mh-iconbtn" type="button" id="mhViewerClose"><i class="fas fa-xmark"></i></button></div>' +
        '<div class="mh-viewer-body"><iframe src="' + url +
        '" style="width:100%;height:100%;border:none;background:#0d1117" title="' +
        window.MShell.esc(title) + '"></iframe></div>';
      host.style.height = '100%';
      document.body.appendChild(host);
      host.querySelector('#mhViewerClose').addEventListener('click', function () { host.remove(); });
    }

    showProgress(msg, pct) {
      window.MShell.busy(true);
      return 'msh_' + Date.now();
    }
    hideProgress(id) {
      var ops = this.state.operations.filter(function (o) { return o !== id; });
      this.state.operations = ops;
      if (!ops.length) window.MShell.busy(false);
    }

    showNotification(msg, type) {
      window.MShell.toast(msg, type === 'error' ? 'err' : '');
    }
  };
  } /* defineMobileFM */

  document.addEventListener('DOMContentLoaded', function () {
    if (typeof window.FileManagerApp !== 'function') {
      console.error('[fm_mobile] FileManagerApp не загружен');
      return;
    }
    defineMobileFM();
    window.FM = new window.MobileFMApp();
  });
})();
