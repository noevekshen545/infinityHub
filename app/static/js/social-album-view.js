/* Открытие альбома в просмотрщике — общий помощник для групп, каналов и профиля.
 *
 * Раньше каждая поверхность делала это по-своему: страница групп и каналов
 * звала lbOpen по плитке, профиль держал собственный #photo-view-modal с
 * классами .pv-*, а лента вклеивала lbOpen прямо в onclick. Здесь собрано
 * одно место, которое строит список кадров для SocialViewer и подключает
 * панель с комментариями, лайками и действиями.
 *
 * Использование:
 *   SocialAlbumView.open(album, {
 *     contextLabel: 'Альбом группы «Название»',
 *     onEditPhoto(photoId), onDeletePhoto(photoId), onShare(item),
 *   }, startIndex)
 */
window.SocialAlbumView = (function () {
  'use strict';

  const API = '/api/social';

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function toast(msg, type) {
    if (typeof window.toast === 'function') window.toast(msg, type);
  }

  function confirmBox(opts) {
    if (window.UI && UI.confirm) return UI.confirm(opts);
    return Promise.resolve(window.confirm(opts.text || opts.title || 'Продолжить?'));
  }

  /* Аудио не показываем в просмотрщике: его слушает плеер под плитками. */
  function toItem(p) {
    const kind = p.media_type || 'photo';
    return {
      id: p.photo_id || p.id,
      type: kind === 'video' ? 'video' : 'image',
      src: p.url,
      caption: p.caption || '',
      likes: p.likes !== undefined ? p.likes : (p.likes_count || 0),
      liked: !!(p.liked !== undefined ? p.liked : p.liked_by_me),
      can_edit: !!p.can_edit,
      can_delete: !!p.can_delete,
      date: p.created_at ? ago(p.created_at) : '',
      author: {
        name: p.author_name || '',
        avatar: p.author_avatar || '',
      },
      raw: p,
    };
  }

  function ago(ts) {
    const d = Date.now() / 1000 - (Number(ts) || 0);
    if (d < 60) return 'только что';
    if (d < 3600) return Math.floor(d / 60) + ' мин назад';
    if (d < 86400) return Math.floor(d / 3600) + ' ч назад';
    if (d < 604800) return Math.floor(d / 86400) + ' дн назад';
    return new Date((Number(ts) || 0) * 1000).toLocaleDateString('ru-RU');
  }

  function open(album, opts, startIndex) {
    if (!window.SocialViewer) {
      toast('Просмотрщик не загружен', 'error');
      return;
    }
    opts = opts || {};
    const all = (album.photos || []).filter(p => (p.media_type || 'photo') !== 'audio');
    if (!all.length) {
      toast('В альбоме нет изображений', 'error');
      return;
    }
    /* Клик по конкретной плитке должен открывать именно её кадр, даже если
       в альбоме вперемешку фото и видео: индекс ищем по id, а не по
       позиции — иначе после фильтрации аудио кадр смещался. */
    let index = 0;
    if (startIndex !== undefined && startIndex !== null) {
      const raw = (album.photos || [])[startIndex];
      const wanted = (raw && (raw.photo_id || raw.id)) || startIndex;
      const found = all.findIndex(p => (p.photo_id || p.id) === wanted);
      index = found >= 0 ? found : Math.min(startIndex, all.length - 1);
    }

    SocialViewer.open(all.map(toItem), index, {
      panel: true,
      contextLabel: opts.contextLabel || '',
      likeUrl: id => `${API}/photos/${encodeURIComponent(id)}/like`,
      commentsUrl: item => `${API}/photos/${encodeURIComponent(item.id)}/comments`,
      delUrl: (item, cid) => `${API}/photos/${encodeURIComponent(item.id)}/comments/${encodeURIComponent(cid)}`,
      likeCommentUrl: (item, cid) => `${API}/photos/comments/${encodeURIComponent(cid)}/like`,
      share: opts.onShare ? item => opts.onShare(item) : null,
      onEdit: opts.onEditPhoto ? item => opts.onEditPhoto(item.id) : null,
      onDelete: opts.onDeletePhoto ? item => confirmDelete(item, opts) : null,
      onChange: opts.onChange,
      /* После удаления кадра список в просмотрщике должен сократиться,
         иначе стрелки листают по уже несуществующим фото. */
      onClose: opts.onClose,
    });
  }

  async function confirmDelete(item, opts) {
    const ok = await confirmBox({
      title: 'Удалить фото?',
      text: 'Фото будет убрано из альбома.',
      okText: 'Удалить',
      danger: true,
    });
    if (!ok) return;
    try {
      await opts.onDeletePhoto(item.id);
      toast('Фото удалено');
    } catch (e) {
      toast('Не удалось удалить: ' + (e.message || e), 'error');
    }
  }

  /* Превращает фото в набор для SocialViewer без панели — используется,
     когда открывают кадр из поста, а не из альбома. */
  function openSingle(p, contextLabel) {
    if (!window.SocialViewer) { lbOpen([{ type: 'image', src: p.url }], 0); return; }
    SocialViewer.open([toItem(p)], 0, {
      panel: true,
      contextLabel: contextLabel || '',
      likeUrl: id => `${API}/photos/${encodeURIComponent(id)}/like`,
      commentsUrl: item => `${API}/photos/${encodeURIComponent(item.id)}/comments`,
      delUrl: (item, cid) => `${API}/photos/${encodeURIComponent(item.id)}/comments/${encodeURIComponent(cid)}`,
      likeCommentUrl: (item, cid) => `${API}/photos/comments/${encodeURIComponent(cid)}/like`,
    });
  }

  return { open, openSingle, toItem };
})();
