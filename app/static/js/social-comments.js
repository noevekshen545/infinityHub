/* Общий компонент комментариев соцсети.
 *
 * Один рендерер и один контроллер на три места: комментарии поста группы,
 * комментарии к фото в альбоме и ответы в форуме. Разница только в
 * переданных адресах API, поэтому подключение выглядит так:
 *
 *   SocialComments.mount(document.getElementById('comments-xyz'), {
 *     listUrl:  `${API}/groups/${gid}/posts/${pid}/comments`,
 *     postUrl:  url => `${API}/groups/${gid}/posts/${pid}/comments`,
 *     delUrl:   url => `${API}/groups/${gid}/posts/${pid}/comments/${url}`,
 *   });
 *
 * Компонент сам подгружает следующие страницы по кнопке «Показать ещё»,
 * поэтому listUrl должен возвращать {items, total, offset, has_more}.
 * Для обратной совместимости поддержан и «голый» массив: если ответ — это
 * массив, подгрузки не будет, но отрисовка останется прежней.
 */
window.SocialComments = (function () {
  'use strict';

  const PAGE = 50;

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  /* Текст с безопасной подстановкой ссылок: протокол ограничен http(s),
     иначе через javascript: можно было бы выполнить код. Упоминания
     @имя подсвечиваются отдельно — раньше они выглядели как обычный
     текст, и было не видно, к кому вообще обращение. */
  function linkify(s) {
    const withMentions = esc(s).replace(
      /(^|\s)(@[^\s@,.:;!?()[\]{}"']{1,40})/g,
      '$1<span class="cmt-mention">$2</span>');
    return withMentions.replace(/(https?:\/\/[^\s<]+[^\s<.,:;"')\]])/g,
      u => '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + u + '</a>');
  }

  function ago(ts) {
    const d = Date.now() / 1000 - (Number(ts) || 0);
    if (d < 60) return 'только что';
    if (d < 3600) return Math.floor(d / 60) + ' мин';
    if (d < 86400) return Math.floor(d / 3600) + ' ч';
    if (d < 604800) return Math.floor(d / 86400) + ' дн';
    return new Date((Number(ts) || 0) * 1000).toLocaleDateString('ru-RU');
  }

  function avatar(url, name, cls) {
    const letter = (String(name || '?').trim()[0] || '?').toUpperCase();
    return '<div class="' + (cls || 'cmt-avatar') + '">' +
      (url ? '<img src="' + esc(url) + '" alt="" loading="lazy">'
           : esc(letter)) + '</div>';
  }

  function normalize(c) {
    /* Комментарии приходят из трёх разных источников, и поля у них
       называются по-разному: content у постов и ответов форума,
       text у фото, id/author_id против id/profile_id. Приводим к одному
       виду здесь, чтобы разметка не знала о разнице. */
    return {
      id: c.id,
      parent_id: c.parent_id || '',
      author_id: c.author_id || c.profile_id || '',
      display_name: c.display_name || c.author || '',
      avatar_path: c.avatar_path || c.avatar || '',
      text: c.content !== undefined ? c.content : (c.text || ''),
      created_at: c.created_at || 0,
      edited_at: c.edited_at || 0,
      likes_count: c.likes_count || 0,
      liked_by_me: !!c.liked_by_me,
      can_delete: c.can_delete !== undefined ? !!c.can_delete : !!c.can_edit,
      can_edit: !!c.can_edit,
      is_mine: c.is_mine,
    };
  }

  function itemHtml(c, opts) {
    const mine = c.is_mine !== undefined ? c.is_mine
      : String(c.author_id) === String(opts.me);
    const name = c.display_name || c.author_id || 'Участник';
    return '<div class="cmt-item' + (c.parent_id ? ' cmt-item--reply' : '') + '" data-cid="' + esc(c.id) + '">'
      + avatar(c.avatar_path, name)
      + '<div class="cmt-main">'
        + '<div class="cmt-line1">'
          + '<span class="cmt-author">' + esc(name) + '</span>'
          + '<span class="cmt-time">' + esc(ago(c.created_at)) + '</span>'
          + (c.edited_at ? '<span class="cmt-badge cmt-badge--edited">изменено</span>' : '')
        + '</div>'
        + '<div class="cmt-text">' + linkify(c.text) + '</div>'
        + '<div class="cmt-actions">'
          + (opts.canLike
            ? '<button class="cmt-btn cmt-btn--like' + (c.liked_by_me ? ' is-active' : '') + '" data-act="like">'
              + '<i class="fas fa-thumbs-up"></i>'
              + (c.likes_count ? '<span data-like-count>' + c.likes_count + '</span>' : '')
              + '</button>' : '')
          + (opts.canReply
            ? '<button class="cmt-btn" data-act="reply"><i class="fas fa-reply"></i>Ответить</button>' : '')
          + ((mine || c.can_delete)
            ? '<button class="cmt-btn" data-act="edit"><i class="fas fa-pen"></i>Изменить</button>' : '')
          + ((mine || c.can_delete)
            ? '<button class="cmt-btn cmt-btn--del" data-act="del"><i class="fas fa-trash"></i></button>' : '')
        + '</div>'
      + '</div>'
    + '</div>';
  }

  function mount(box, opts) {
    if (!box) return null;
    opts = opts || {};
    box.classList.add('cmt-box');
    box.innerHTML =
      '<div class="cmt-head"><i class="fas fa-comment cmt-head-icon"></i>'
      + '<span>Комментарии</span><span class="cmt-head-count" data-cmt-total></span>'
      + '<button class="cmt-head-close" title="Свернуть"><i class="fas fa-chevron-up"></i></button></div>'
      + '<div class="cmt-list" data-cmt-list><div class="cmt-loading"><i class="fas fa-spinner fa-spin"></i></div></div>'
      + '<button class="cmt-more" data-cmt-more hidden>Показать ещё</button>'
      + '<div class="cmt-replyhint" data-cmt-hint hidden>'
        + '<span data-cmt-hint-text></span><button data-cmt-hint-x><i class="fas fa-times"></i></button></div>'
      + '<div class="cmt-form">'
        + '<div class="cmt-input-wrap"><textarea class="cmt-input" rows="1" '
          + 'placeholder="Написать комментарий…"></textarea></div>'
        + '<button class="cmt-send" data-cmt-send disabled title="Отправить"><i class="fas fa-paper-plane"></i></button>'
      + '</div>';

    const list = box.querySelector('[data-cmt-list]');
    const moreBtn = box.querySelector('[data-cmt-more]');
    const totalEl = box.querySelector('[data-cmt-total]');
    const hint = box.querySelector('[data-cmt-hint]');
    const hintText = box.querySelector('[data-cmt-hint-text]');
    const input = box.querySelector('.cmt-input');
    const send = box.querySelector('[data-cmt-send]');
    const closeBtn = box.querySelector('.cmt-head-close');

    let items = [];
    let total = 0;
    let offset = 0;
    let loading = false;
    let replyTo = null;
    let editingId = null;

    function setTotal() {
      totalEl.textContent = total ? '(' + total + ')' : '';
      if (opts.onChange) opts.onChange(total);
    }

    function render() {
      if (!items.length) {
        list.innerHTML = '<div class="cmt-empty">Комментариев пока нет</div>';
      } else {
        /* Ответы идут после своего родителя, а не в конец списка: иначе
           ветка разваливалась бы на несвязанные куски. */
        const byId = new Map();
        items.forEach(c => byId.set(c.id, c));
        const roots = items.filter(c => !c.parent_id || !byId.has(c.parent_id));
        const out = [];
        roots.forEach(r => {
          out.push(r);
          items.filter(c => c.parent_id === r.id).forEach(x => out.push(x));
        });
        /* Ветки, чей родитель не пришёл на этой странице, иначе потерялись. */
        items.forEach(c => { if (out.indexOf(c) === -1) out.push(c); });
        list.innerHTML = out.map(c => itemHtml(c, opts)).join('');
      }
      moreBtn.hidden = offset >= total || !items.length;
      setTotal();
    }

    async function load(reset) {
      if (loading) return;
      loading = true;
      if (reset) { offset = 0; items = []; }
      moreBtn.hidden = true;
      try {
        const url = opts.listUrl + (opts.listUrl.indexOf('?') === -1 ? '?' : '&')
          + 'offset=' + offset + '&limit=' + PAGE;
        const res = await fetch(url, { credentials: 'same-origin' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const rows = Array.isArray(data) ? data : (data.comments || data.items || []);
        const chunk = rows.map(normalize);
        items = items.concat(chunk);
        if (Array.isArray(data)) {
          total = items.length;
        } else {
          total = data.total !== undefined ? data.total : items.length;
        }
        offset = items.length;
        render();
      } catch (e) {
        list.innerHTML = '<div class="cmt-error">Не удалось загрузить: ' + esc(e.message) + '</div>';
      } finally {
        loading = false;
      }
    }

    function setReply(c) {
      replyTo = c;
      editingId = null;
      hint.hidden = false;
      hintText.textContent = 'Ответ для ' + (c.display_name || c.author_id);
      input.focus();
    }

    function clearHint() {
      replyTo = null;
      hint.hidden = true;
    }

    async function sendComment() {
      const text = input.value.trim();
      if (!text || send.disabled) return;
      send.disabled = true;
      input.value = '';
      autoGrow();
      try {
        const body = { content: text, text: text };
        if (replyTo) body.parent_id = replyTo.id;
        const res = await fetch(opts.postUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          let msg = 'HTTP ' + res.status;
          try { const j = await res.json(); if (j.detail) msg = j.detail; } catch (e) {}
          throw new Error(msg);
        }
        clearHint();
        await load(true);
      } catch (e) {
        input.value = text;          // не теряем написанное
        autoGrow();
        if (window.toast) window.toast('Не удалось отправить: ' + e.message, 'error');
      } finally {
        send.disabled = !input.value.trim();
      }
    }

    async function saveEdit(id) {
      const node = list.querySelector('[data-cid="' + CSS.escape(id) + '"]');
      if (!node) return;
      const textEl = node.querySelector('.cmt-text');
      const old = textEl.textContent;
      if (editingId === id) {
        const ta = node.querySelector('[data-cmt-edit]');
        const val = (ta.value || '').trim();
        if (!val) return;
        try {
          const res = await fetch(opts.delUrl(id), {
            method: 'PUT',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: val, text: val }),
          });
          if (!res.ok) throw new Error('HTTP ' + res.status);
          editingId = null;
          await load(true);
        } catch (e) {
          if (window.toast) window.toast('Не удалось сохранить: ' + e.message, 'error');
        }
        return;
      }
      editingId = id;
      textEl.innerHTML = '<textarea class="cmt-input" data-cmt-edit style="min-height:60px">'
        + esc(old) + '</textarea>'
        + '<div style="display:flex;gap:6px;margin-top:6px">'
        + '<button class="btn btn-primary btn-xs" data-cmt-edit-save>Сохранить</button>'
        + '<button class="btn btn-secondary btn-xs" data-cmt-edit-cancel>Отмена</button></div>';
      textEl.querySelector('[data-cmt-edit]').focus();
    }

    async function remove(id) {
      const confirmFn = (window.UI && UI.confirm) ? UI.confirm : null;
      if (confirmFn) {
        const ok = await confirmFn('Удалить комментарий?');
        if (!ok) return;
      } else if (!window.confirm('Удалить комментарий?')) return;
      try {
        const res = await fetch(opts.delUrl(id), { method: 'DELETE', credentials: 'same-origin' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        await load(true);
      } catch (e) {
        if (window.toast) window.toast('Не удалось удалить: ' + e.message, 'error');
      }
    }

    async function toggleLike(id, btn) {
      if (!opts.likeUrl) return;
      try {
        const res = await fetch(opts.likeUrl(id), { method: 'POST', credentials: 'same-origin' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        btn.classList.toggle('is-active', !!data.liked);
        const span = btn.querySelector('[data-like-count]');
        const n = data.likes_count || 0;
        if (span) span.textContent = n;
        else if (n) btn.insertAdjacentHTML('beforeend', '<span data-like-count>' + n + '</span>');
        const item = items.find(c => String(c.id) === String(id));
        if (item) { item.liked_by_me = !!data.liked; item.likes_count = n; }
      } catch (e) {
        if (window.toast) window.toast('Не удалось поставить лайк', 'error');
      }
    }

    function autoGrow() {
      input.style.height = '22px';
      input.style.height = Math.min(input.scrollHeight, 180) + 'px';
    }

    input.addEventListener('input', () => {
      send.disabled = !input.value.trim();
      autoGrow();
    });
    input.addEventListener('keydown', e => {
      /* Enter отправляет, Shift+Enter — перенос строки. */
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendComment();
      }
      if (e.key === 'Escape') clearHint();
    });
    send.addEventListener('click', sendComment);
    moreBtn.addEventListener('click', () => load(false));
    hint.querySelector('[data-cmt-hint-x]').addEventListener('click', clearHint);
    closeBtn.addEventListener('click', () => {
      const h = box.closest('.modal') || box.parentElement;
      if (h && h.style) h.style.display = 'none';
    });

    list.addEventListener('click', e => {
      const save = e.target.closest('[data-cmt-edit-save]');
      const cancel = e.target.closest('[data-cmt-edit-cancel]');
      if (cancel) { editingId = null; load(true); return; }
      const btn = e.target.closest('.cmt-btn');
      if (!btn) return;
      const row = btn.closest('.cmt-item');
      const id = row && row.getAttribute('data-cid');
      if (!id) return;
      if (save) { saveEdit(id); return; }
      const act = btn.getAttribute('data-act');
      if (act === 'like') toggleLike(id, btn);
      else if (act === 'reply') {
        const c = items.find(x => String(x.id) === String(id));
        if (c) setReply(c);
      }
      else if (act === 'edit') saveEdit(id);
      else if (act === 'del') remove(id);
    });

    load(true);
    /* Подсказки @ подключаются один раз на элемент: повторный вызов
       на ту же textarea SocialMentions игнорирует. */
    if (window.SocialMentions && opts.canMention !== false) {
      SocialMentions.attach(input, { groupId: opts.groupId });
    }
    return { reload: () => load(true), close: clearHint };
  }

  return { mount, esc, ago, avatar, linkify };
})();
