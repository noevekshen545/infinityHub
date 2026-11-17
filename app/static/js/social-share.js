/* Окно «Поделиться» — выбор места публикации.
 *
 * Раньше кнопка «поделиться» была только на профиле и просто копировала
 * ссылку в буфер: repost_post читал исключительно social_posts, поэтому
 * поделиться фото альбома или постом группы было физически нечем — 404.
 * Теперь есть POST /api/social/share, который сам определяет тип объекта.
 *
 * Использование:
 *   SocialShare.openForPhoto(item, groupId)   — item из SocialViewer
 *   SocialShare.openForPost(post, groupId)
 */
window.SocialShare = (function () {
  'use strict';

  const API = '/api/social';

  let el = null, built = false;
  let state = { targets: [], filter: '', tab: 'feed', srcId: '', srcKind: '', preview: null };

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function toast(msg, type) {
    if (typeof window.toast === 'function') window.toast(msg, type);
  }

  function ensure() {
    if (built) return el;
    el = document.createElement('div');
    el.className = 'share-overlay';
    el.hidden = true;
    el.innerHTML =
      '<div class="share-modal">'
      + '<div class="share-head">'
        + '<div class="share-title"><i class="fas fa-share-alt"></i> Поделиться</div>'
        + '<button class="share-close" title="Закрыть"><i class="fas fa-times"></i></button>'
      + '</div>'
      + '<div class="share-preview" data-share-preview></div>'
      + '<div class="share-tabs">'
        + '<button class="share-tab is-active" data-tab="feed">В мою ленту</button>'
        + '<button class="share-tab" data-tab="group">В группу или канал</button>'
      + '</div>'
      + '<div class="share-body">'
        + '<div class="share-list" data-share-list></div>'
        + '<label class="share-comment-label">Ваш комментарий'
          + '<textarea class="share-comment" rows="3" maxlength="4000" placeholder="Необязательно"></textarea>'
        + '</label>'
      + '</div>'
      + '<div class="share-foot">'
        + '<button class="btn btn-secondary" data-share-copy"><i class="fas fa-link"></i> Скопировать ссылку</button>'
        + '<span class="grow"></span>'
        + '<button class="btn btn-secondary" data-share-cancel>Отмена</button>'
        + '<button class="btn btn-primary" data-share-submit>Поделиться</button>'
      + '</div>'
      + '</div>';
    document.body.appendChild(el);
    built = true;

    el.querySelector('.share-close').addEventListener('click', close);
    el.querySelector('[data-share-cancel]').addEventListener('click', close);
    el.addEventListener('mousedown', e => { if (e.target === el) close(); });
    document.addEventListener('keydown', e => {
      if (el.hidden) return;
      if (e.key === 'Escape') { e.preventDefault(); close(); }
    });

    el.querySelectorAll('.share-tab').forEach(t => {
      t.addEventListener('click', () => {
        state.tab = t.getAttribute('data-tab');
        el.querySelectorAll('.share-tab').forEach(x => x.classList.toggle('is-active', x === t));
        renderList();
      });
    });

    el.querySelector('[data-share-submit]').addEventListener('click', submit);
    el.querySelector('[data-share-copy]').addEventListener('click', copyLink);
    return el;
  }

  function renderPreview() {
    const box = el.querySelector('[data-share-preview]');
    const p = state.preview || {};
    if (p.image) {
      box.innerHTML = '<img class="share-thumb" src="' + esc(p.image) + '" alt="">'
        + '<span class="share-preview-text">' + esc(p.caption || '') + '</span>';
    } else {
      box.innerHTML = '<div class="share-preview-icon"><i class="fas fa-comment"></i></div>'
        + '<span class="share-preview-text">' + esc((p.caption || '').slice(0, 160)) + '</span>';
    }
  }

  function renderList() {
    const box = el.querySelector('[data-share-list]');
    if (state.tab === 'feed') {
      box.innerHTML = '<button class="share-target is-active" data-target="feed">'
        + '<span class="share-target-ava share-target-ava--me"><i class="fas fa-user"></i></span>'
        + '<span class="share-target-name">Моя лента</span></button>';
      return;
    }
    const list = state.targets.filter(t => !state.filter
      || t.name.toLowerCase().includes(state.filter.toLowerCase()));
    if (!list.length) {
      box.innerHTML = '<div class="share-empty">'
        + (state.targets.length ? 'Ничего не найдено' : 'Вы не состоите ни в одной группе')
        + '</div>';
      return;
    }
    box.innerHTML = list.map(t => `
      <button class="share-target${t.id === state.pickedGroup ? ' is-active' : ''}" data-target="group:${esc(t.id)}">
        <span class="share-target-ava">${t.avatar_path
          ? `<img src="${esc(t.avatar_path)}" alt="" loading="lazy">`
          : `<i class="fas ${t.is_channel ? 'fa-bullhorn' : 'fa-users'}"></i>`}</span>
        <span class="share-target-name">${esc(t.name)}</span>
        <span class="share-target-kind">${t.is_channel ? 'канал' : 'группа'}</span>
      </button>`).join('');
    box.querySelectorAll('.share-target').forEach(b => {
      b.addEventListener('click', () => {
        state.pickedGroup = b.getAttribute('data-target');
        box.querySelectorAll('.share-target').forEach(x => x.classList.toggle('is-active', x === b));
      });
    });
  }

  async function loadTargets() {
    try {
      const res = await fetch(`${API}/share/groups`, { credentials: 'same-origin' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const d = await res.json();
      state.targets = d.targets || [];
    } catch (e) {
      state.targets = [];
    }
  }

  async function submit() {
    const btn = el.querySelector('[data-share-submit]');
    const comment = el.querySelector('.share-comment').value.trim();
    const target = state.tab === 'feed' ? 'feed' : (state.pickedGroup || '');
    if (state.tab === 'group' && !target) { toast('Выберите группу'); return; }
    btn.disabled = true;
    btn.textContent = 'Публикуем…';
    try {
      const res = await fetch(`${API}/share`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: state.srcId, target, comment }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || ('Ошибка ' + res.status));
      toast('Опубликовано');
      close();
      /* Лента и стена группы обновляются своими загрузчиками; событие
         позволяет им подхватить новое без перезагрузки страницы. */
      window.dispatchEvent(new CustomEvent('social:shared', { detail: data }));
    } catch (e) {
      toast('Не удалось поделиться: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Поделиться';
    }
  }

  function copyLink() {
    const p = state.preview || {};
    if (!p.image) { toast('Ссылка доступна только для фото'); return; }
    const url = location.origin + p.image;
    if (navigator.clipboard) {
      navigator.clipboard.writeText(url)
        .then(() => toast('Ссылка скопирована'))
        .catch(() => toast('Не удалось скопировать', 'error'));
    } else {
      toast(url);
    }
  }

  function open(opts) {
    opts = opts || {};
    ensure();
    state.srcId = opts.id || '';
    state.srcKind = opts.kind || '';
    state.preview = { image: opts.image || '', caption: opts.caption || '' };
    state.pickedGroup = '';
    state.tab = opts.defaultTab === 'group' && opts.groupId ? 'group' : 'feed';
    el.querySelector('.share-comment').value = '';
    el.querySelectorAll('.share-tab').forEach(x =>
      x.classList.toggle('is-active', x.getAttribute('data-tab') === state.tab));
    renderPreview();
    renderList();
    el.hidden = false;
    loadTargets().then(() => { if (state.tab === 'group') renderList(); });
  }

  function openForPhoto(item, groupId) {
    if (!item || !item.id) return;
    open({
      id: item.id, kind: 'photo',
      image: item.src || item.url,
      caption: item.caption || '',
      groupId, defaultTab: groupId ? 'group' : 'feed',
    });
  }

  function openForPost(post, groupId) {
    if (!post || !post.id) return;
    const att = (post.attachments || []).find(a => a.type === 'image');
    open({
      id: post.id, kind: post.repost_id ? 'group_post' : 'post',
      image: att ? (att.url || att.src) : '',
      caption: post.content || '',
      groupId, defaultTab: groupId ? 'group' : 'feed',
    });
  }

  function close() {
    if (el) el.hidden = true;
  }

  return { open, openForPhoto, openForPost, close };
})();
