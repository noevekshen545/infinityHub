/* Форум группы: разделы, темы и ответы.
 *
 * Отдельный файл, потому что page_social_friends.html давно больше двух
 * тысяч строк, а вся логика форума в него не влезает.
 *
 * Бэкенд — social_group_forum.py (роуты под /api/social).
 * Разметка и стили — page_social_friends.html (.forum-*).
 *
 * Группу и профиль берём из window.currentGroup / window.activeProfile:
 * они уже есть на странице, заводить вторые копии незачем.
 */
(function () {
  'use strict';

  const API = '/api/social';
  const POSTS_PAGE = 20;
  const THREADS_PAGE = 50;
  const REFRESH_MS = 20000;
  const SORTS = ['active', 'new', 'posts', 'views'];
  const NO_SECTION = '__none__';   // псевдораздел «темы без раздела»

  let sections = [];
  let isManager = false;
  let sectionId = '';
  let noSectionOnly = false;   // псевдораздел «темы без раздела»
  let sort = 'active';
  let threadsOffset = 0;
  let threadsTotal = 0;

  let openThreadId = '';
  let threadOffset = 0;
  let threadTotal = 0;
  let postsCache = [];
  let replyTo = null;      // сообщение, на которое отвечают
  let editingPostId = '';  // сообщение, которое правим

  let forumTimer = null;

  /* ------------------------------------------------------------------ утилиты */

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  const groupId = () => (window.currentGroup && window.currentGroup.id) || '';
  const me = () => window.activeProfile || '';

  function toast(msg, type) {
    if (typeof window.toast === 'function') window.toast(msg, type);
  }

  function confirmBox(opts) {
    if (window.UI && UI.confirm) return UI.confirm(opts);
    return Promise.resolve(window.confirm((opts && opts.text) || (opts && opts.title) || 'Продолжить?'));
  }

  function promptBox(opts) {
    if (window.UI && UI.prompt) return UI.prompt(opts);
    return Promise.resolve(window.prompt((opts && opts.title) || '', (opts && opts.value) || ''));
  }

  function openModal(id) {
    if (typeof window.openModal === 'function') window.openModal(id);
    else {
      const m = document.getElementById(id);
      if (m) m.classList.add('active', 'show');
    }
  }

  function closeModal(id) {
    if (typeof window.closeModal === 'function') window.closeModal(id);
    else {
      const m = document.getElementById(id);
      if (m) m.classList.remove('active', 'show');
    }
  }

  /* apiOrThrow-обёртка. Раньше здесь использовался «тихий» api(), который на
     4xx/5xx не бросает исключение: пользователю показывалось «Тема создана»
     при 403. */
  async function api(url, opts) {
    opts = opts || {};
    const headers = Object.assign({}, opts.headers || {});
    if (opts.body !== undefined && !(opts.body instanceof FormData) && !('Content-Type' in headers)) {
      headers['Content-Type'] = 'application/json';
    }
    const res = await fetch(url, Object.assign({}, opts, { headers, credentials: 'same-origin' }));
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok) throw new Error(data.detail || data.error || ('Ошибка ' + res.status));
    return data;
  }

  function ago(ts) {
    const d = Date.now() / 1000 - (Number(ts) || 0);
    if (d < 60) return 'только что';
    if (d < 3600) return Math.floor(d / 60) + ' мин';
    if (d < 86400) return Math.floor(d / 3600) + ' ч';
    if (d < 604800) return Math.floor(d / 86400) + ' дн';
    return new Date((Number(ts) || 0) * 1000).toLocaleDateString('ru-RU');
  }

  function when(ts) {
    if (!ts) return '';
    return ago(ts) + ' · ' + new Date(ts * 1000).toLocaleString('ru-RU', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    });
  }

  function avatarHtml(url, name) {
    const letter = (String(name || '?').trim()[0] || '?').toUpperCase();
    return url ? `<img src="${esc(url)}" alt="" loading="lazy">` : esc(letter);
  }

  /* ------------------------------------------------------------------ разделы */

  async function loadForum() {
    if (!groupId()) return;
    await loadForumSections();
    await loadForumThreads(true);
    startForumTimer();
  }

  async function loadForumSections() {
    const gid = groupId();
    const box = document.getElementById('forum-sections');
    if (!gid || !box) return;
    const data = await api(`${API}/groups/${gid}/forum`);
    sections = data.sections || [];
    isManager = !!data.is_manager;
    renderSections();
    renderSectionSelect();
    const totalEl = document.querySelector('[data-fsec-count]');
    if (totalEl) totalEl.textContent = data.threads_total || '';
  }

  function renderSections() {
    const box = document.getElementById('forum-sections');
    if (!box) return;
    document.querySelectorAll('.forum-sec[data-fsec]').forEach(b => {
      const v = b.getAttribute('data-fsec') || '';
      b.classList.toggle('is-active', v === (sectionId || ''));
    });
    box.innerHTML = sections.map(s => `
      <button class="forum-sec${s.id === sectionId ? ' is-active' : ''}" data-fsec="${esc(s.id)}"
              onclick="selectForumSection('${esc(s.id)}')" title="${esc(s.description || s.title)}">
        <i class="fas fa-folder"></i>
        <span>${esc(s.title)}</span>
        <span class="fs-n">${s.threads_count || 0}</span>
      </button>`).join('');
  }

  function renderSectionSelect() {
    const sel = document.getElementById('forum-new-section');
    if (!sel) return;
    sel.innerHTML = '<option value="">Без раздела</option>'
      + sections.map(s => `<option value="${esc(s.id)}">${esc(s.title)}</option>`).join('');
    sel.value = sectionId || '';
  }

  function selectForumSection(id) {
    sectionId = (id === NO_SECTION) ? '' : (id || '');
    // «Без раздела» сервер не умеет: фильтруем на клиенте из полной выборки.
    noSectionOnly = (id === NO_SECTION);
    renderSections();
    loadForumThreads(true);
  }

  function setForumSort(s) {
    if (SORTS.indexOf(s) === -1) s = 'active';
    sort = s;
    document.querySelectorAll('.forum-sort').forEach(b => {
      b.classList.toggle('is-active', b.getAttribute('data-fsort') === sort);
    });
    loadForumThreads(true);
  }

  /* --------------------------------------------------------------------- темы */

  async function loadForumThreads(reset) {
    const gid = groupId();
    const box = document.getElementById('forum-threads');
    if (!gid || !box) return;
    if (reset) { threadsOffset = 0; threadsTotal = 0; box.innerHTML = '<div class="loading"><i class="fas fa-spinner"></i></div>'; }

    const q = (document.getElementById('forum-search') && document.getElementById('forum-search').value || '').trim();
    const params = new URLSearchParams();
    params.set('sort', sort);
    params.set('offset', '0');
    params.set('limit', String(THREADS_PAGE * 3));
    if (sectionId && !noSectionOnly) params.set('section_id', sectionId);
    if (q) params.set('q', q);

    try {
      const data = await api(`${API}/groups/${gid}/forum/threads?${params}`);
      let list = data.threads || [];
      threadsTotal = data.total || list.length;
      if (noSectionOnly) list = list.filter(t => !t.section_id);
      renderThreads(list, reset);
    } catch (e) {
      box.innerHTML = '<div class="empty-state" style="padding:20px;color:var(--red)"><i class="fas fa-exclamation-triangle"></i><p>'
        + esc(e.message) + '</p></div>';
    }
  }

  function threadRow(t) {
    const name = t.display_name || t.author_id || 'Участник';
    const cls = 'forum-thread' + (t.is_pinned ? ' is-pinned' : '') + (t.is_closed ? ' is-closed' : '');
    return `
      <div class="${cls}" onclick="openForumThread('${esc(t.id)}')" role="button" tabindex="0">
        <div class="forum-t-ava">${avatarHtml(t.author_avatar, name)}</div>
        <div class="forum-t-body">
          <div class="forum-t-title">
            ${t.is_pinned ? '<span class="forum-flag forum-flag--pin"><i class="fas fa-thumbtack"></i></span>' : ''}
            ${t.is_closed ? '<span class="forum-flag forum-flag--close">закрыта</span>' : ''}
            ${t.section_title ? `<span class="forum-flag forum-flag--sec">${esc(t.section_title)}</span>` : ''}
            <span>${esc(t.title)}</span>
          </div>
          <div class="forum-t-meta">
            <span>${esc(name)}</span>
            <span><i class="fas fa-clock"></i> ${when(t.last_post_at || t.updated_at || t.created_at)}</span>
            ${t.last_author ? `<span><i class="fas fa-user"></i> ${esc(t.last_author)}</span>` : ''}
          </div>
        </div>
        <div class="forum-t-stats">
          <div><b>${t.posts_count || 0}</b><span>ответов</span></div>
          <div><b>${t.views_count || 0}</b><span>просм.</span></div>
        </div>
      </div>`;
  }

  function renderThreads(list, replace) {
    const box = document.getElementById('forum-threads');
    if (!box) return;
    if (!list.length) {
      box.innerHTML = '<div class="empty-state" style="padding:28px"><i class="fas fa-comments" style="font-size:28px;opacity:.4"></i>'
        + '<p>В этом разделе тем пока нет</p>'
        + '<button class="btn btn-primary btn-sm" onclick="openForumCreate()"><i class="fas fa-plus"></i> Создать первую</button></div>';
      removeMoreButton();
      return;
    }
    if (replace) box.innerHTML = '';
    box.insertAdjacentHTML('beforeend', list.map(threadRow).join(''));
    renderMoreButton(list.length);
  }

  function renderMoreButton(loaded) {
    const box = document.getElementById('forum-threads');
    if (!box) return;
    let btn = document.getElementById('forum-more');
    if (btn) btn.remove();
    if (!loaded || threadsOffset + loaded >= threadsTotal) return;
    // Догрузка идёт отдельным запросом со смещением.
    btn = document.createElement('button');
    btn.id = 'forum-more';
    btn.className = 'cmt-more';
    btn.textContent = 'Показать ещё';
    btn.onclick = loadMoreThreads;
    box.parentElement.insertBefore(btn, box.nextSibling);
  }

  function removeMoreButton() {
    const btn = document.getElementById('forum-more');
    if (btn) btn.remove();
  }

  async function loadMoreThreads() {
    const gid = groupId();
    const box = document.getElementById('forum-threads');
    if (!gid || !box) return;
    const btn = document.getElementById('forum-more');
    if (btn) btn.disabled = true;
    threadsOffset += box.querySelectorAll('.forum-thread').length;
    const params = new URLSearchParams();
    params.set('sort', sort);
    params.set('offset', String(threadsOffset));
    params.set('limit', String(THREADS_PAGE));
    if (sectionId && !noSectionOnly) params.set('section_id', sectionId);
    const q = (document.getElementById('forum-search') && document.getElementById('forum-search').value || '').trim();
    if (q) params.set('q', q);
    try {
      const data = await api(`${API}/groups/${gid}/forum/threads?${params}`);
      let list = data.threads || [];
      if (noSectionOnly) list = list.filter(t => !t.section_id);
      renderThreads(list, false);
    } catch (e) {
      toast('Не удалось загрузить: ' + e.message, 'error');
      if (btn) btn.disabled = false;
    }
  }

  /* ------------------------------------------------------------- тема и ответы */

  async function openForumThread(id) {
    openThreadId = id;
    threadOffset = 0;
    threadTotal = 0;
    postsCache = [];
    replyTo = null;
    editingPostId = '';
    const body = document.getElementById('forum-thread-body');
    if (!body) return;
    openModal('forum-thread-modal');
    body.innerHTML = '<div class="loading"><i class="fas fa-spinner"></i></div>';
    await loadThreadPosts(true);
    loadForumThreads(true);
  }

  async function loadThreadPosts(reset) {
    const box = document.getElementById('forum-thread-body');
    if (!box || !openThreadId) return;
    if (reset) {
      threadOffset = 0;
      postsCache = [];
      box.innerHTML = '<div class="forum-thread-head" style="margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--border)"></div>'
        + '<div id="forum-post-list"></div>'
        + '<div id="forum-post-more"></div>'
        + '<div id="forum-reply"></div>';
    }
    try {
      const data = await api(`${API}/forum/threads/${openThreadId}?offset=${threadOffset}&limit=${POSTS_PAGE}`);
      const t = data.thread || {};
      const posts = data.posts || [];
      postsCache = postsCache.concat(posts);
      threadTotal = data.total || postsCache.length;
      threadOffset += posts.length;

      const list = document.getElementById('forum-post-list');
      if (list && posts.length) list.insertAdjacentHTML('beforeend', posts.map(postHtml).join(''));

      const moreBox = document.getElementById('forum-post-more');
      if (moreBox) {
        moreBox.innerHTML = threadOffset < threadTotal
          ? `<button class="cmt-more" onclick="loadMoreThreadPosts()">Показать ещё ответы (${threadTotal - threadOffset})</button>`
          : '';
      }
      renderThreadHeader(t);
      renderReplyForm(t);
    } catch (e) {
      box.innerHTML = '<div class="empty-state" style="padding:20px;color:var(--red)"><i class="fas fa-exclamation-triangle"></i><p>'
        + esc(e.message) + '</p></div>';
    }
  }

  function loadMoreThreadPosts() { return loadThreadPosts(false); }

  function renderThreadHeader(t) {
    const box = document.getElementById('forum-thread-body');
    const actions = document.getElementById('forum-thread-actions');
    if (!box) return;
    const head = box.querySelector('.forum-thread-head');
    if (!head) return;
    const name = t.display_name || t.author_id || '';
    head.innerHTML = `
      <div class="forum-t-title" style="font-size:17px">
        ${t.is_pinned ? '<span class="forum-flag forum-flag--pin"><i class="fas fa-thumbtack"></i></span>' : ''}
        ${t.is_closed ? '<span class="forum-flag forum-flag--close">закрыта</span>' : ''}
        ${t.section_title ? `<span class="forum-flag forum-flag--sec">${esc(t.section_title)}</span>` : ''}
        <span id="forum-thread-title">${esc(t.title || '')}</span>
      </div>
      <div class="forum-t-meta" style="margin-top:6px">
        <span>${esc(name)}</span>
        <span><i class="fas fa-clock"></i> ${when(t.created_at)}</span>
        <span><i class="fas fa-comments"></i> ${threadTotal}</span>
        <span><i class="fas fa-eye"></i> ${t.views_count || 0}</span>
      </div>`;
    if (!actions) return;
    const mine = t.author_id === me();
    actions.innerHTML =
      (mine ? '<button class="btn btn-secondary btn-sm" onclick="editForumThread()"><i class="fas fa-pen"></i> Переименовать</button>' : '')
      + (t.can_moderate
        ? `<button class="btn btn-secondary btn-sm" onclick="toggleForumPin()"><i class="fas fa-thumbtack"></i> ${t.is_pinned ? 'Открепить' : 'Закрепить'}</button>
           <button class="btn btn-secondary btn-sm" onclick="toggleForumClose()"><i class="fas fa-${t.is_closed ? 'unlock' : 'lock'}"></i> ${t.is_closed ? 'Открыть' : 'Закрыть'}</button>` : '')
      + ((mine || t.can_delete)
        ? '<button class="btn btn-danger btn-sm" onclick="deleteForumThread()" title="Удалить тему"><i class="fas fa-trash"></i></button>' : '');
  }

  function attachmentsHtml(list) {
    const atts = (list || []).filter(a => a && (a.url || a.src));
    if (!atts.length) return '';
    return '<div class="forum-p-atts">' + atts.map(a => {
      const u = esc(a.url || a.src);
      if (a.type === 'video') return `<video src="${u}" controls preload="metadata"></video>`;
      if (a.type === 'audio') return `<audio src="${u}" controls preload="none"></audio>`;
      if (a.type === 'file') return `<a class="forum-p-file" href="${u}" target="_blank" rel="noopener"><i class="fas fa-file"></i> ${esc(a.title || a.name || 'файл')}</a>`;
      return `<img src="${u}" alt="" loading="lazy" data-lb data-lb-list='${esc(JSON.stringify(atts.filter(x => x.type === 'image').map(x => ({ type: 'image', src: x.url || x.src, caption: x.title || '' }))))}' data-lb-idx="0">`;
    }).join('') + '</div>';
  }

  function postHtml(p) {
    const name = p.display_name || p.author_id || 'Участник';
    const mine = p.author_id === me();
    return `
      <div class="forum-post${p.is_first ? ' forum-post--first' : ''}" data-post="${esc(p.id)}">
        <div class="fm-avatar">${avatarHtml(p.author_avatar, name)}</div>
        <div class="forum-p-main">
          <div class="forum-p-head">
            <span class="post-author">${esc(name)}</span>
            <span class="post-time">${when(p.created_at)}${p.edited_at ? ' · изменено' : ''}</span>
          </div>
          <div class="forum-p-text">${esc(p.content)}</div>
          ${attachmentsHtml(p.attachments)}
          <div class="forum-p-actions">
            <button class="cmt-btn cmt-btn--like${p.liked_by_me ? ' is-active' : ''}" data-act="like">
              <i class="fas fa-thumbs-up"></i><span data-like-count>${p.likes_count || 0}</span></button>
            ${p.is_first ? '' : '<button class="cmt-btn" data-act="reply"><i class="fas fa-reply"></i>Ответить</button>'}
            ${(mine || p.can_edit) ? '<button class="cmt-btn" data-act="edit"><i class="fas fa-pen"></i>Изменить</button>' : ''}
            ${(mine || p.can_delete) ? '<button class="cmt-btn cmt-btn--del" data-act="del"><i class="fas fa-trash"></i></button>' : ''}
          </div>
        </div>
      </div>`;
  }

  /* Вложения текущего ответа: {file, url, type, title, uploading}.
     Файл грузится сразу после выбора, чтобы к моменту отправки ответа
     ссылка уже была готова и отправка не зависала на загрузке. */
  let pendingAtts = [];

  function renderAttsPreview() {
    const box = document.getElementById('forum-reply');
    if (!box) return;
    const host = box.querySelector('[data-atts-preview]');
    if (!host) return;
    if (!pendingAtts.length) { host.hidden = true; host.innerHTML = ''; return; }
    host.hidden = false;
    host.innerHTML = pendingAtts.map((a, i) => {
      const thumb = a.type === 'image' && a.preview
        ? `<img src="${esc(a.preview)}" alt="">`
        : `<i class="fas ${a.type === 'video' ? 'fa-film' : a.type === 'audio' ? 'fa-music' : 'fa-file'}"></i>`;
      return `<div class="rp-att${a.uploading ? ' is-uploading' : ''}">
        ${thumb}
        <span class="rp-att-name">${esc(a.title || '')}</span>
        <button data-att-del="${i}" title="Убрать"><i class="fas fa-times"></i></button>
      </div>`;
    }).join('');
    host.querySelectorAll('[data-att-del]').forEach(b => {
      b.addEventListener('click', () => {
        pendingAtts.splice(parseInt(b.getAttribute('data-att-del'), 10), 1);
        renderAttsPreview();
        syncSend();
      });
    });
  }

  async function uploadForumFile(file) {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch(`${API}/upload/post-media`, { method: 'POST', body: fd, credentials: 'same-origin' });
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok) throw new Error(data.detail || ('Ошибка ' + res.status));
    return data.path || data.url;
  }

  function pickForumFiles() {
    const inp = document.getElementById('forum-att-input');
    if (inp) inp.click();
  }

  async function onForumFilesPicked(input) {
    const files = Array.from(input.files || []);
    input.value = '';
    for (const f of files) {
      const type = f.type.startsWith('video/') ? 'video'
        : f.type.startsWith('audio/') ? 'audio'
        : f.type.startsWith('image/') ? 'image' : 'file';
      const item = { file: f, type, title: f.name, uploading: true,
                     preview: type === 'image' ? URL.createObjectURL(f) : '' };
      pendingAtts.push(item);
      renderAttsPreview();
      try {
        const url = await uploadForumFile(f);
        item.url = url;
        item.uploading = false;
      } catch (e) {
        pendingAtts = pendingAtts.filter(x => x !== item);
        toast('Не удалось загрузить ' + f.name + ': ' + e.message, 'error');
      }
      renderAttsPreview();
      syncSend();
    }
  }

  function syncSend() {
    const box = document.getElementById('forum-reply');
    if (!box) return;
    const send = box.querySelector('[data-reply-send]');
    const area = box.querySelector('[data-reply-area]');
    if (!send || !area) return;
    const uploading = pendingAtts.some(a => a.uploading);
    send.disabled = uploading || (!area.value.trim() && !pendingAtts.length);
  }

  function renderReplyForm(t) {
    const box = document.getElementById('forum-reply');
    if (!box) return;
    if (t && t.is_closed) {
      box.className = '';
      box.innerHTML = '<div class="cmt-empty"><i class="fas fa-lock"></i> Тема закрыта — новые ответы не принимаются</div>';
      return;
    }
    if (box.dataset.ready) return;
    box.className = 'forum-reply-form';
    box.dataset.ready = '1';
    box.innerHTML = `
      <div class="cmt-replyhint" data-reply-hint hidden>
        <span data-reply-text></span>
        <button data-reply-x title="Отменить"><i class="fas fa-times"></i></button>
      </div>
      <textarea data-reply-area placeholder="Ваш ответ…"></textarea>
      <div class="rp-atts" data-atts-preview hidden></div>
      <input type="file" id="forum-att-input" multiple accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.txt,.zip"
             style="display:none" onchange="onForumFilesPicked(this)">
      <div class="forum-reply-bar">
        <div class="frp-tools">
          <button class="frp-tool" data-frp="file" title="Прикрепить файл"><i class="fas fa-paperclip"></i></button>
          <button class="frp-tool" data-frp="emoji" title="Эмодзи и стикеры"><i class="fas fa-smile"></i></button>
        </div>
        <span class="grow"></span>
        <span class="post-time" data-reply-count>0 / 20000</span>
        <button class="btn btn-primary btn-sm" data-reply-send disabled><i class="fas fa-paper-plane"></i> Ответить</button>
      </div>
      <div class="wall-picker" id="fpPicker">
        <div class="wp-tabs">
          <button type="button" class="active" data-wtab="emoji" onclick="fpPicker.switchTab('emoji')">Эмодзи</button>
          <button type="button" data-wtab="sticker" onclick="fpPicker.switchTab('sticker')">Стикеры</button>
        </div>
        <div class="wp-body" id="fpBody"></div>
      </div>`;

    const area = box.querySelector('[data-reply-area]');
    const send = box.querySelector('[data-reply-send]');
    const counter = box.querySelector('[data-reply-count]');
    const hint = box.querySelector('[data-reply-hint]');

    function sync() {
      counter.textContent = area.value.length + ' / 20000';
      counter.style.color = area.value.length > 20000 ? 'var(--red)' : '';
      syncSend();
    }
    area.addEventListener('input', sync);
    area.addEventListener('keydown', e => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendReply(); }
    });
    send.addEventListener('click', sendReply);
    box.querySelector('[data-reply-x]').addEventListener('click', clearReplyHint);
    box.querySelector('[data-frp="file"]').addEventListener('click', pickForumFiles);

    /* Эмодзи и стикеры — общий компонент social-emoji.js. Панель стоит
       внутри формы, поэтому z-index поднят в social-forum CSS: иначе она
       рисовалась бы под модальным окном темы. */
    if (window.SOCIAL_EMOJI && window.SOCIAL_EMOJI.makeWallPicker) {
      // makeWallPicker ищет поле по id, а у textarea id не было. Имя
      // выбрано отличное от id контейнера (#forum-reply), иначе
      // getElementById('forum-reply') начал бы возвращать textarea.
      area.id = 'forum-reply-area';
      window.fpPicker = SOCIAL_EMOJI.makeWallPicker({
        panel: 'fpPicker', body: 'fpBody', target: 'forum-reply-area',
      });
      box.querySelector('[data-frp="emoji"]').addEventListener('click', () => {
        if (window.fpPicker) window.fpPicker.toggle();
      });
    } else {
      box.querySelector('[data-frp="emoji"]').style.display = 'none';
    }

    /* Подсказки @ в ответах форума. */
    if (window.SocialMentions) {
      SocialMentions.attach(area, { groupId: () => groupId() });
    }
  }

  function setReplyHint(text) {
    const box = document.getElementById('forum-reply');
    if (!box) return;
    const hint = box.querySelector('[data-reply-hint]');
    const hintText = box.querySelector('[data-reply-text]');
    if (!hint || !hintText) return;
    if (text) { hintText.textContent = text; hint.hidden = false; }
    else hint.hidden = true;
  }

  function clearReplyHint() {
    replyTo = null;
    editingPostId = '';
    setReplyHint('');
  }

  async function sendReply() {
    const box = document.getElementById('forum-reply');
    const area = box && box.querySelector('[data-reply-area]');
    const send = box && box.querySelector('[data-reply-send]');
    const text = area ? area.value.trim() : '';
    if (!openThreadId) return;
    if (!text && !pendingAtts.length) return;
    if (pendingAtts.some(a => a.uploading)) { toast('Дождитесь загрузки вложений', 'error'); return; }
    send.disabled = true;
    try {
      if (editingPostId) {
        await api(`${API}/forum/posts/${editingPostId}`, {
          method: 'PUT', body: JSON.stringify({ content: text }),
        });
      } else {
        let content = text;
        // Ответ на конкретное сообщение оформляется цитатой в тексте:
        // список ответов плоский и читается легче, чем дерево, а связь
        // «на кого отвечаем» при этом не теряется.
        if (replyTo) {
          const who = replyTo.display_name || replyTo.author_id || 'участник';
          content = `[в ответ ${who}]\n${text}`;
        }
        const atts = pendingAtts.filter(a => a.url).map(a => ({
          type: a.type, url: a.url, title: a.title || '',
        }));
        await api(`${API}/forum/threads/${openThreadId}/posts`, {
          method: 'POST', body: JSON.stringify({ content, attachments: atts }),
        });
      }
      area.value = '';
      pendingAtts = [];
      renderAttsPreview();
      clearReplyHint();
      await loadThreadPosts(true);
      await loadForumThreads(true);
    } catch (e) {
      toast('Не удалось отправить: ' + e.message, 'error');
    } finally {
      syncSend();
    }
  }

  async function onForumPostClick(e) {
    const btn = e.target.closest('.forum-p-actions .cmt-btn');
    if (!btn) return;
    const row = btn.closest('.forum-post');
    const pid = row && row.getAttribute('data-post');
    if (!pid) return;
    const act = btn.getAttribute('data-act');
    const post = postsCache.find(p => String(p.id) === String(pid));

    if (act === 'like') {
      try {
        const r = await api(`${API}/forum/posts/${pid}/like`, { method: 'POST' });
        btn.classList.toggle('is-active', !!r.liked);
        const sp = btn.querySelector('[data-like-count]');
        if (sp) sp.textContent = r.likes_count || 0;
      } catch (err) { toast(err.message || 'Не удалось поставить лайк', 'error'); }
      return;
    }
    if (act === 'reply') {
      replyTo = post || { display_name: 'участник' };
      editingPostId = '';
      setReplyHint('Ответ для ' + (replyTo.display_name || 'участника'));
      const area = document.querySelector('[data-reply-area]');
      if (area) area.focus();
      return;
    }
    if (act === 'edit') {
      if (!post) return;
      const textEl = row.querySelector('.forum-p-text');
      const area = document.querySelector('[data-reply-area]');
      if (!area) return;
      editingPostId = pid;
      replyTo = null;
      setReplyHint('Редактирование сообщения');
      area.value = textEl.textContent;
      area.dispatchEvent(new Event('input'));
      area.focus();
      return;
    }
    if (act === 'del') {
      if (!await confirmBox({ title: 'Удалить сообщение?', text: 'Сообщение будет удалено.', okText: 'Удалить', danger: true })) return;
      try {
        await api(`${API}/forum/posts/${pid}`, { method: 'DELETE' });
        await loadThreadPosts(true);
      } catch (err) { toast(err.message || 'Не удалось удалить', 'error'); }
    }
  }

  function closeForumThread() {
    openThreadId = '';
    postsCache = [];
    replyTo = null;
    editingPostId = '';
    const box = document.getElementById('forum-thread-body');
    if (box) box.innerHTML = '';
    closeModal('forum-thread-modal');
  }

  /* ------------------------------------------------------------- действия темы */

  async function editForumThread() {
    const el = document.getElementById('forum-thread-title');
    if (!el) return;
    const title = await promptBox({
      title: 'Название темы', label: 'Название', value: el.textContent, required: true,
    });
    if (!title || !String(title).trim()) return;
    try {
      await api(`${API}/forum/threads/${openThreadId}`, {
        method: 'PUT', body: JSON.stringify({ title: String(title).trim() }),
      });
      toast('Название изменено');
      await loadThreadPosts(true);
      await loadForumThreads(true);
    } catch (e) { toast('Не удалось изменить: ' + e.message, 'error'); }
  }

  async function toggleForumPin() {
    try {
      await api(`${API}/forum/threads/${openThreadId}/pin`, { method: 'POST' });
      await loadThreadPosts(true);
      await loadForumThreads(true);
    } catch (e) { toast(e.message || 'Не удалось изменить', 'error'); }
  }

  async function toggleForumClose() {
    try {
      await api(`${API}/forum/threads/${openThreadId}/close`, { method: 'POST' });
      toast('Состояние темы изменено');
      await loadThreadPosts(true);
      await loadForumThreads(true);
    } catch (e) { toast(e.message || 'Не удалось изменить', 'error'); }
  }

  async function deleteForumThread() {
    if (!await confirmBox({ title: 'Удалить тему?', text: 'Тема и все ответы будут удалены.', okText: 'Удалить', danger: true })) return;
    try {
      await api(`${API}/forum/threads/${openThreadId}`, { method: 'DELETE' });
      toast('Тема удалена');
      closeForumThread();
      await loadForumThreads(true);
    } catch (e) { toast('Не удалось удалить: ' + e.message, 'error'); }
  }

  /* ------------------------------------------------------------ создание темы */

  function openForumCreate() {
    if (!groupId()) { toast('Группа не загружена', 'error'); return; }
    const title = document.getElementById('forum-new-title');
    const body = document.getElementById('forum-new-body');
    if (title) title.value = '';
    if (body) body.value = '';
    const sel = document.getElementById('forum-new-section');
    if (sel) sel.value = sectionId || '';
    openModal('forum-create-modal');
    if (title) title.focus();
  }

  async function submitForumThread() {
    const t = document.getElementById('forum-new-title');
    const b = document.getElementById('forum-new-body');
    const sel = document.getElementById('forum-new-section');
    const title = (t && t.value || '').trim();
    const content = (b && b.value || '').trim();
    if (!title) { toast('Введите заголовок', 'error'); return; }
    if (!content) { toast('Введите текст сообщения', 'error'); return; }
    try {
      const r = await api(`${API}/groups/${groupId()}/forum/threads`, {
        method: 'POST',
        body: JSON.stringify({ title, content, section_id: (sel && sel.value) || '' }),
      });
      closeModal('forum-create-modal');
      toast('Тема создана');
      await loadForumThreads(true);
      if (r.thread_id) await openForumThread(r.thread_id);
    } catch (e) { toast('Не удалось создать тему: ' + e.message, 'error'); }
  }

  /* ------------------------------------------------------- управление разделами */

  async function manageForumSections() {
    if (!groupId()) { toast('Группа не загружена', 'error'); return; }
    if (!isManager) { toast('Управление разделами доступно администраторам', 'error'); return; }
    openModal('forum-sections-modal');
    await renderSectionsManager();
  }

  async function renderSectionsManager() {
    const box = document.getElementById('forum-sections-list');
    if (!box) return;
    box.innerHTML = '<div class="loading"><i class="fas fa-spinner"></i></div>';
    try {
      await loadForumSections();
    } catch (e) {
      box.innerHTML = '<div class="cmt-error">' + esc(e.message) + '</div>';
      return;
    }
    if (!sections.length) {
      box.innerHTML = '<div class="cmt-empty">Разделов пока нет. Создайте первый ниже.</div>';
      return;
    }
    box.innerHTML = sections.map((s, i) => `
      <div style="display:flex;gap:6px;align-items:center">
        <span class="forum-flag forum-flag--sec">${i + 1}</span>
        <span style="flex:1;min-width:0;font-size:13px">${esc(s.title)}</span>
        <span style="font-size:11px;color:var(--text3)">${s.threads_count || 0} тем</span>
        <button class="btn btn-secondary btn-xxs" onclick="moveForumSection('${esc(s.id)}',-1)" ${i === 0 ? 'disabled' : ''} title="Выше"><i class="fas fa-arrow-up"></i></button>
        <button class="btn btn-secondary btn-xxs" onclick="moveForumSection('${esc(s.id)}',1)" ${i === sections.length - 1 ? 'disabled' : ''} title="Ниже"><i class="fas fa-arrow-down"></i></button>
        <button class="btn btn-secondary btn-xxs" onclick="renameForumSection('${esc(s.id)}')" title="Переименовать"><i class="fas fa-pen"></i></button>
        <button class="btn btn-danger btn-xxs" onclick="removeForumSection('${esc(s.id)}')" title="Удалить"><i class="fas fa-trash"></i></button>
      </div>`).join('');
  }

  async function submitForumSection() {
    const input = document.getElementById('forum-new-section-title');
    const title = (input && input.value || '').trim();
    if (!title) { toast('Введите название раздела', 'error'); return; }
    try {
      await api(`${API}/groups/${groupId()}/forum/sections`, {
        method: 'POST', body: JSON.stringify({ title }),
      });
      if (input) input.value = '';
      toast('Раздел создан');
      await renderSectionsManager();
      await loadForumThreads(true);
    } catch (e) { toast('Не удалось создать: ' + e.message, 'error'); }
  }

  async function renameForumSection(id) {
    const s = sections.find(x => x.id === id);
    if (!s) return;
    const title = await promptBox({ title: 'Название раздела', label: 'Название', value: s.title });
    if (!title || !String(title).trim()) return;
    try {
      await api(`${API}/groups/${groupId()}/forum/sections/${id}`, {
        method: 'PUT', body: JSON.stringify({ title: String(title).trim() }),
      });
      await renderSectionsManager();
      await loadForumThreads(true);
    } catch (e) { toast('Не удалось изменить: ' + e.message, 'error'); }
  }

  async function moveForumSection(id, dir) {
    const idx = sections.findIndex(x => x.id === id);
    const to = idx + dir;
    if (idx < 0 || to < 0 || to >= sections.length) return;
    const a = sections[idx];
    const b = sections[to];
    // Меняем местами: сервер сортирует по position, и одинаковые значения
    // дали бы произвольный порядок.
    const pa = a.position !== undefined ? a.position : idx;
    const pb = b.position !== undefined ? b.position : to;
    try {
      if (pa !== pb) {
        await api(`${API}/groups/${groupId()}/forum/sections/${b.id}`, {
          method: 'PUT', body: JSON.stringify({ position: pa }),
        });
      }
      await api(`${API}/groups/${groupId()}/forum/sections/${a.id}`, {
        method: 'PUT', body: JSON.stringify({ position: pb }),
      });
      await renderSectionsManager();
      await loadForumThreads(true);
    } catch (e) { toast('Не удалось изменить порядок: ' + e.message, 'error'); }
  }

  async function removeForumSection(id) {
    if (!await confirmBox({
      title: 'Удалить раздел?',
      text: 'Темы не удалятся, а останутся без раздела.',
      okText: 'Удалить', danger: true,
    })) return;
    try {
      await api(`${API}/groups/${groupId()}/forum/sections/${id}`, { method: 'DELETE' });
      toast('Раздел удалён');
      await renderSectionsManager();
      await loadForumThreads(true);
    } catch (e) { toast('Не удалось удалить: ' + e.message, 'error'); }
  }

  /* ---------------------------------------------------------- автообновление */

  function startForumTimer() {
    stopForumTimer();
    forumTimer = setInterval(() => {
      const pane = document.getElementById('group-forum');
      if (!pane || pane.style.display === 'none' || !groupId()) return;
      if (document.getElementById('forum-create-modal').classList.contains('active')) return;
      if (document.getElementById('forum-sections-modal').classList.contains('active')) return;
      loadForumThreads(true);
    }, REFRESH_MS);
  }

  function stopForumTimer() {
    if (forumTimer) { clearInterval(forumTimer); forumTimer = null; }
  }

  /* -------------------------------------------------------------------- экспорт */

  /**
   * Обработка WS-события forum_post.
   *
   * Если открыта та же тема — догружаем новый ответ, не перерисовывая весь
   * список (иначе сбрасывается прокрутка читателя). Если открыта другая тема
   * или только список тем — обновляем счётчики и показываем мягкое напоминание.
   */
  async function onForumMessage(msg) {
    if (!msg || !msg.group_id || msg.group_id !== groupId()) return;
    if (openThreadId && msg.thread_id === openThreadId) {
      const box = document.getElementById('forum-thread-body');
      if (box && !document.querySelector('.forum-post[data-post="' + msg.post_id + '"]')) {
        await loadThreadPosts(false);
        return;
      }
    }
    if (document.getElementById('forum-threads')) loadForumThreads(true);
    if (!openThreadId && msg.author_name) {
      toast(msg.author_name + ': ' + (msg.content || '').slice(0, 60), 'info');
    }
  }

  const EXPORTS = {
    loadForum, loadForumThreads, loadMoreThreads,
    selectForumSection, setForumSort,
    openForumThread, closeForumThread, loadMoreThreadPosts,
    openForumCreate, submitForumThread,
    manageForumSections, submitForumSection, renameForumSection,
    moveForumSection, removeForumSection,
    editForumThread, toggleForumPin, toggleForumClose, deleteForumThread,
    stopForumTimer, onForumMessage,
  };
  Object.keys(EXPORTS).forEach(k => { window[k] = EXPORTS[k]; });
  window.SocialForum = EXPORTS;

  /* Один делегированный обработчик вместо onclick в разметке: список
     ответов перерисовывается целиком, и inline-обработчики размножались бы. */
  document.addEventListener('click', function (e) {
    if (e.target.closest('#forum-thread-body')) onForumPostClick(e);
  });

  // Функция вызывается из inline onchange= в шаблоне формы ответа.
  window.onForumFilesPicked = onForumFilesPicked;

  window.addEventListener('beforeunload', stopForumTimer);
})();
