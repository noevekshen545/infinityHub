/* Проигрывание медиа в альбомах групп и каналов.

   Общий компонент для page_social_friends.html (группы) и
   social-channel-content.js (каналы): строит плитки фото/видео/аудио с
   нормальными обложками и ведёт плейлист аудио — после окончания одной
   дорожки автоматически играет следующая.

   Использование:
     SocialAlbumPlayer.stop();                       // при закрытии/смене альбома
     const html = SocialAlbumPlayer.buildTiles(photos, {canEdit, remove:'removeAlbumPhoto'});
     grid.innerHTML = html;
     SocialAlbumPlayer.mount(grid, photos);          // навешивает плеер

   Видео и фото открываются через глобальный lbOpen (social-lightbox.js), если
   он подключён на странице. Аудио играется inline-плеером.
*/
window.SocialAlbumPlayer = (function () {
  'use strict';

  let audio = null;
  let tracks = [];   // [{id,url,name}]
  let idx = -1;

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function fmt(s) {
    if (!isFinite(s) || s < 0) s = 0;
    const m = Math.floor(s / 60);
    return m + ':' + String(Math.floor(s % 60)).padStart(2, '0');
  }
  function rowEl(id) { return document.querySelector('[data-ap-id="' + id + '"]'); }

  function ensureAudio() {
    if (!audio) {
      audio = new Audio();
      audio.preload = 'metadata';
      audio.addEventListener('ended', () => playAt(idx + 1));
      audio.addEventListener('timeupdate', paint);
      audio.addEventListener('loadedmetadata', paint);
      audio.addEventListener('play', () => setBtn(true));
      audio.addEventListener('pause', () => setBtn(false));
    }
    return audio;
  }

  function setBtn(playing) {
    if (idx < 0) return;
    const r = rowEl(tracks[idx].id); if (!r) return;
    const i = r.querySelector('[data-ap-btn] i'); if (i) i.className = 'fas ' + (playing ? 'fa-pause' : 'fa-play');
    r.classList.toggle('ap-playing', playing);
  }

  function paint() {
    if (idx < 0 || !audio) return;
    const r = rowEl(tracks[idx].id); if (!r) return;
    const dur = audio.duration || 0, cur = audio.currentTime || 0;
    const f = r.querySelector('[data-ap-fill]'); if (f) f.style.width = (dur ? (cur / dur * 100) : 0) + '%';
    const c = r.querySelector('[data-ap-cur]'); if (c) c.textContent = fmt(cur);
    const d = r.querySelector('[data-ap-dur]'); if (d) d.textContent = fmt(dur);
  }

  function playAt(i) {
    if (!tracks.length) return;
    if (i >= tracks.length) { stop(); return; }
    if (i < 0) i = 0;
    idx = i;
    const t = tracks[idx];
    ensureAudio();
    document.querySelectorAll('.ap-track.ap-active').forEach(r => r.classList.remove('ap-active'));
    const r = rowEl(t.id); if (r) r.classList.add('ap-active');
    audio.src = t.url;
    audio.play().catch(() => {});
  }

  function toggle(id) {
    const i = tracks.findIndex(t => t.id === id);
    if (i < 0) return;
    if (idx === i && audio) {
      if (!audio.paused) { audio.pause(); return; }
      if (audio.ended || audio.currentTime > 0) { audio.play().catch(() => {}); return; }
    }
    playAt(i);
  }

  function seek(id, clientX) {
    const i = tracks.findIndex(t => t.id === id);
    if (i < 0 || !audio || !audio.duration) return;
    const r = rowEl(id); const bar = r && r.querySelector('[data-ap-bar]'); if (!bar) return;
    const rect = bar.getBoundingClientRect();
    const p = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    audio.currentTime = p * audio.duration;
    paint();
  }

  function stop() {
    if (audio) { audio.pause(); audio.removeAttribute('src'); try { audio.load(); } catch (e) {} }
    idx = -1; tracks = [];
    document.querySelectorAll('.ap-track.ap-active,.ap-track.ap-playing').forEach(r => {
      r.classList.remove('ap-active', 'ap-playing');
      const i = r.querySelector('[data-ap-btn] i'); if (i) i.className = 'fas fa-play';
      const f = r.querySelector('[data-ap-fill]'); if (f) f.style.width = '0%';
    });
  }

  function openComments(photoId, containerId) {
    const box = document.getElementById(containerId);
    if (!box || !photoId) return;
    box.style.display = 'block';
    if (!window.SocialComments) {
      box.innerHTML = '<div class="cmt-error">Компонент комментариев не загружен</div>';
      return;
    }
    /* Раньше комментарии к фото рисовались здесь же, инлайновыми стилями,
       и выглядели иначе, чем комментарии к постам. Теперь используется тот
       же компонент social-comments.js. */
    SocialComments.mount(box, {
      me: (window.activeProfile || ''),
      canLike: true,
      listUrl: '/api/social/photos/' + encodeURIComponent(photoId) + '/comments',
      postUrl: '/api/social/photos/' + encodeURIComponent(photoId) + '/comments',
      delUrl: id => '/api/social/photos/' + encodeURIComponent(photoId) + '/comments/' + encodeURIComponent(id),
      likeUrl: id => '/api/social/photos/comments/' + encodeURIComponent(id) + '/like',
    });
  }

  function buildTiles(photos, opts) {
    opts = opts || {};
    const canEdit = !!opts.canEdit;
    const removeFn = opts.remove || 'removeAlbumPhoto';
    /* Как открывается кадр. Приоритет у opts.onOpen: страница передаёт
       имя функции, которая открывает просмотрщик с панелью комментариев
       для конкретного альбома. Если её нет — откат на общий data-lb.
       Раньше здесь был только lbOpen, из-за чего в альбоме группы
       открывался голый просмотрщик без комментариев. */
    const openFn = opts.onOpen;
    const hasLB = !openFn && (typeof lbOpen === 'function');
    const lb = photos.filter(x => (x.media_type || 'photo') !== 'audio')
      .map(x => ({
        type: x.media_type === 'video' ? 'video' : 'image',
        src: x.url,
        caption: x.caption || '',
      }));
    const lbJson = JSON.stringify(lb).replace(/'/g, '&#39;');
    const del = (pid) => canEdit
      ? `<button class="ap-del" title="Убрать" onclick="event.stopPropagation();${removeFn}('${esc(pid)}')">&times;</button>` : '';
    const comments = opts.comments
      ? (pid) => `<button class="ap-comment" title="Комментарии" onclick="event.stopPropagation();SocialAlbumPlayer.openComments('${esc(pid)}','${esc(opts.comments)}')"><i class="fas fa-comment"></i></button>`
      : () => '';

    return photos.map((p, i) => {
      const url = esc(p.url);
      const kind = p.media_type || 'photo';
      if (kind === 'audio') {
        const name = esc(p.caption || ('Дорожка ' + (i + 1)));
        return `<div class="ap-track" data-ap-id="${esc(p.photo_id)}">
          <div class="ap-cover"><i class="fas fa-music"></i></div>
          <div class="ap-body">
            <div class="ap-name">${name}</div>
            <div class="ap-bar" data-ap-bar><div class="ap-fill" data-ap-fill></div></div>
            <div class="ap-time"><span data-ap-cur>0:00</span><span data-ap-dur>0:00</span></div>
          </div>
          <button class="ap-play" data-ap-btn type="button" title="Играть"><i class="fas fa-play"></i></button>
          ${comments(p.photo_id)}
          ${canEdit ? `<button class="ap-x" title="Убрать" onclick="${removeFn}('${esc(p.photo_id)}')">&times;</button>` : ''}
        </div>`;
      }
      /* Индекс в альбоме, а не в отфильтрованном списке: аудио-дорожки
         в просмотрщик не попадают, и смещение сдвигало бы стартовый кадр. */
      let click = '';
      if (openFn) {
        click = `onclick="${openFn}('${esc(p.photo_id)}',${i})"`;
      } else if (hasLB) {
        const li = Math.max(0, lb.findIndex(x => x.src === p.url));
        click = `data-lb data-lb-list='${lbJson}' data-lb-idx="${li}"`;
      }
      if (kind === 'video') {
        return `<div class="av-tile" ${click}>
          <div class="av-cover"><i class="fas fa-film"></i></div>
          <div class="av-play"><i class="fas fa-play"></i></div>
          ${comments(p.photo_id)}
          ${del(p.photo_id)}
        </div>`;
      }
      return `<div class="ph-tile" ${click}>
        <img src="${url}" alt="" loading="lazy">
        ${comments(p.photo_id)}
        ${del(p.photo_id)}
      </div>`;
    }).join('');
  }

  function mount(container, photos) {
    tracks = (photos || []).filter(p => (p.media_type || 'photo') === 'audio')
      .map(p => ({ id: p.photo_id, url: p.url, name: p.caption || '' }));
    idx = -1;
    container.querySelectorAll('[data-ap-btn]').forEach(b => {
      b.addEventListener('click', e => {
        e.stopPropagation();
        toggle(b.closest('[data-ap-id]').getAttribute('data-ap-id'));
      });
    });
    container.querySelectorAll('[data-ap-bar]').forEach(bar => {
      bar.addEventListener('click', e => {
        seek(bar.closest('[data-ap-id]').getAttribute('data-ap-id'), e.clientX);
      });
    });
  }

  return { buildTiles, mount, stop, openComments };
})();
