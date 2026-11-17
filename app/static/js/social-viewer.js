/* Просмотрщик медиа соцсети — единый компонент.
 *
 * Заменил три несовместимые реализации: голый лайтбокс (лента, группы,
 * каналы), отдельный #photo-view-modal с классами .pv-* (профиль) и
 * плитки SocialAlbumPlayer. Теперь поведение одинаковое везде, а сбоку
 * от фото — панель с комментариями, как в VK и Facebook.
 *
 * Механика сцены (зум, перетаскивание, свайп, щипок, полоса превью)
 * перенесена из social-lightbox.js и доработана:
 *   • клавиши гасят прокрутку страницы под оверлеем (раньше стрелки
 *     прокручивали страницу, потому что preventDefault не вызывался);
 *   • стек оверлеев: Esc закрывает только верхний, поэтому открытый
 *     поверх просмотрщика редактор не закрывает оба сразу;
 *   • панель комментариев вплотную к сцене, на мобильном — нижний лист.
 *
 * Использование:
 *   SocialViewer.open(items, index, opts)
 *     items: [{ id, type:'image'|'video', src, caption, likes, liked, can_edit,
 *               author:{name, avatar}, date, contextLabel }]
 *     opts:  { panel, commentsUrl, postUrl, delUrl, likeUrl, extraActions,
 *               onChange(item), onEdit(item), onDelete(item) }
 *
 * Совместимость: social-lightbox.js по-прежнему экспортирует lbOpen/lbClose
 * и просто вызывает этот компонент.
 */
window.SocialViewer = (function () {
  'use strict';

  /* Стек открытых слоёв. Esc и клик по фону гасят только верхний:
     иначе закрытие редактора поверх просмотрщика закрывало бы оба. */
  const stack = [];

  const S = {
    items: [], index: 0, opts: null,
    scale: 1, tx: 0, ty: 0, pinch: null, drag: null,
    el: null, built: false,
    sideOpen: false,
  };

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function isVideo(item) {
    const src = (item && (item.src || item.url)) || '';
    return (item && item.type === 'video') || /\.(mp4|webm|mov)(\?|$)/i.test(src);
  }

  /* ------------------------------------------------------------------ DOM */

  function ensure() {
    if (S.built) return S.el;
    const wrap = document.createElement('div');
    wrap.className = 'lb-backdrop sv-root';
    wrap.innerHTML =
      '<div class="sv-layout">' +
        '<div class="sv-main">' +
          '<div class="sv-headbar">' +
            '<button class="sv-close" title="Закрыть (Esc)"><i class="fas fa-times"></i></button>' +
            '<div class="sv-meta">' +
              '<span class="sv-author"></span>' +
              '<span class="sv-context"></span>' +
            '</div>' +
          '</div>' +
          '<div class="sv-stagewrap">' +
            '<div class="sv-stage">' +
              '<button class="sv-nav prev" title="Предыдущее (←)">&#10094;</button>' +
              '<button class="sv-nav next" title="Следующее (→)">&#10095;</button>' +
              '<img class="sv-img" alt="">' +
              '<video class="sv-video" controls playsinline style="display:none"></video>' +
              '<div class="sv-spinner"><i class="fas fa-spinner fa-spin"></i></div>' +
            '</div>' +
          '</div>' +
          '<div class="sv-zoombar">' +
            '<button class="sv-zoom-btn" data-z="-1" title="Уменьшить (−)"><i class="fas fa-minus"></i></button>' +
            '<button class="sv-zoom-btn" data-z="0" title="Сбросить (0)">1:1</button>' +
            '<button class="sv-zoom-btn" data-z="1" title="Увеличить (+)"><i class="fas fa-plus"></i></button>' +
            '<span class="sv-zoomval"></span>' +
          '</div>' +
          '<div class="sv-thumbs"></div>' +
        '</div>' +
        '<aside class="sv-side">' +
          '<div class="sv-side-grab"></div>' +
          '<div class="sv-actions"></div>' +
          '<div class="sv-caption"></div>' +
          '<div class="sv-comments"></div>' +
        '</aside>' +
      '</div>' +
      '<div class="sv-hint">← → листать · колесо — зум · 2× клик — приблизить · Esc — закрыть</div>';
    document.body.appendChild(wrap);

    S.el = wrap;
    S.built = true;

    wrap.querySelector('.sv-close').addEventListener('click', () => close());
    wrap.querySelectorAll('.sv-zoom-btn').forEach(b => {
      b.addEventListener('click', () => {
        const z = parseInt(b.getAttribute('data-z'), 10);
        if (z === 0) reset(); else zoom(z * 0.25);
      });
    });
    wrap.querySelector('.sv-nav.prev').addEventListener('click', () => go(-1));
    wrap.querySelector('.sv-nav.next').addEventListener('click', () => go(1));
    wrap.querySelector('.sv-side-grab').addEventListener('click', () => toggleSide());

    wrap.addEventListener('wheel', e => {
      e.preventDefault();
      zoom(e.deltaY > 0 ? -0.2 : 0.2);
    }, { passive: false });

    /* Клик вне сцены закрывает. Раньше проверка стояла на самом backdrop
       и ловила клики по самой сцене, из-за чего кнопки зума и навигации
       работали через stop-условие цепочкой if — легко было сломать. */
    wrap.addEventListener('click', e => {
      if (e.target.closest('.sv-layout')) return;
      close();
    });

    document.addEventListener('keydown', onKey);
    bindDrag(wrap.querySelector('.sv-stage'));
    bindTouch(wrap);
    return wrap;
  }

  function onKey(e) {
    if (!isTop()) return;
    /* Без preventDefault стрелки листали страницу под оверлеем,
       а «+» и «−» прокручивали её вверх-вниз. */
    switch (e.key) {
      case 'Escape': e.preventDefault(); close(); break;
      case 'ArrowLeft': e.preventDefault(); go(-1); break;
      case 'ArrowRight': e.preventDefault(); go(1); break;
      case '+': case '=': e.preventDefault(); zoom(0.25); break;
      case '-': e.preventDefault(); zoom(-0.25); break;
      case '0': e.preventDefault(); reset(); break;
      default: break;
    }
  }

  function isTop() {
    const i = stack.indexOf(S);
    return i === stack.length - 1;
  }

  /* ------------------------------------------------------- сцена: зум/пан */

  function bindDrag(stage) {
    stage.addEventListener('mousedown', e => {
      if (e.target.closest('.sv-nav')) return;
      if (S.scale <= 1) return;
      e.preventDefault();
      S.drag = { x: e.clientX - S.tx, y: e.clientY - S.ty };
      S.el.classList.add('is-panning');
    });
    document.addEventListener('mousemove', e => {
      if (!S.drag) return;
      S.tx = e.clientX - S.drag.x;
      S.ty = e.clientY - S.drag.y;
      apply();
    });
    document.addEventListener('mouseup', () => {
      S.drag = null;
      if (S.el) S.el.classList.remove('is-panning');
    });
    stage.addEventListener('dblclick', e => {
      if (e.target.closest('.sv-nav')) return;
      setScale(S.scale > 1 ? 1 : 2);
    });
  }

  function bindTouch(wrap) {
    let sx = 0, sy = 0, moved = false;
    wrap.addEventListener('touchstart', e => {
      if (e.target.closest('.sv-nav,.sv-close,.sv-actions')) return;
      if (e.touches.length < 1) return;
      sx = e.touches[0].clientX; sy = e.touches[0].clientY;
      moved = false;
      S.pinch = e.touches.length >= 2 ? { d: touchDist(e) } : null;
      S.drag = { x: e.touches[0].clientX - S.tx, y: e.touches[0].clientY - S.ty };
    }, { passive: true });

    wrap.addEventListener('touchmove', e => {
      if (S.pinch && e.touches.length >= 2) {
        const d = touchDist(e);
        if (S.pinch.d > 0) setScale(S.scale * (d / S.pinch.d));
        S.pinch.d = d;
        e.preventDefault();
        return;
      }
      if (e.touches.length !== 1) return;
      const dx = e.touches[0].clientX - sx;
      const dy = e.touches[0].clientY - sy;
      if (Math.abs(dx) > 8 || Math.abs(dy) > 8) moved = true;
      if (S.scale > 1) {
        S.tx = e.touches[0].clientX - S.drag.x;
        S.ty = e.touches[0].clientY - S.drag.y;
        apply();
        e.preventDefault();
        return;
      }
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) {
        go(dx < 0 ? 1 : -1);
        sx = e.touches[0].clientX;
        e.preventDefault();
      }
    }, { passive: false });

    wrap.addEventListener('touchend', e => {
      S.pinch = null;
      S.drag = null;
      /* Тап без перемещения при обычном масштабе — приблизить вдвое:
         на сенсорных экранах dblclick не срабатывает. */
      if (!moved && e.changedTouches && e.changedTouches.length === 1 && S.scale <= 1) {
        setScale(2);
      }
    });
  }

  function touchDist(e) {
    const a = e.touches[0], b = e.touches[1];
    return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  }

  function setScale(v) {
    S.scale = Math.max(0.25, Math.min(6, v));
    if (S.scale <= 1) { S.tx = 0; S.ty = 0; }
    apply();
  }

  function zoom(delta) { setScale(S.scale + delta); }

  function reset() { S.scale = 1; S.tx = 0; S.ty = 0; apply(); }

  function apply() {
    const el = S.el && S.el.querySelector('.sv-img, .sv-video');
    if (!el) return;
    el.style.transform = 'translate(' + S.tx + 'px,' + S.ty + 'px) scale(' + S.scale + ')';
    const zv = S.el.querySelector('.sv-zoomval');
    if (zv) zv.textContent = Math.round(S.scale * 100) + '%';
  }

  /* --------------------------------------------------------------- рендер */

  function current() { return S.items[S.index] || null; }

  function open(items, index, opts) {
    if (!items || !items.length) return;
    S.items = items.filter(Boolean);
    S.index = Math.max(0, Math.min(index || 0, S.items.length - 1));
    S.opts = opts || {};
    ensure();
    if (stack.indexOf(S) === -1) stack.push(S);
    S.el.classList.add('show');
    document.body.style.overflow = 'hidden';
    render();
  }

  function close() {
    if (!isTop()) return;
    const i = stack.indexOf(S);
    if (i !== -1) stack.splice(i, 1);
    if (!S.el) return;
    S.el.classList.remove('show');
    document.body.style.overflow = '';
    stopVideo();
    reset();
    /* Панель комментариев живёт в DOM и держит слушатели; чистим,
       чтобы накопленные обработчики не жили до перезагрузки страницы. */
    const c = S.el.querySelector('.sv-comments');
    if (c) c.innerHTML = '';
    if (S.opts && typeof S.opts.onClose === 'function') S.opts.onClose();
  }

  function stopVideo() {
    const v = S.el && S.el.querySelector('.sv-video');
    if (!v) return;
    v.pause();
    v.removeAttribute('src');
    try { v.load(); } catch (e) {}
  }

  function go(delta) {
    if (S.items.length < 2) return;
    S.index = (S.index + delta + S.items.length) % S.items.length;
    reset();
    render();
  }

  function render() {
    const item = current();
    if (!item) return;
    const img = S.el.querySelector('.sv-img');
    const video = S.el.querySelector('.sv-video');
    const spin = S.el.querySelector('.sv-spinner');
    const src = item.src || item.url || '';

    S.el.classList.toggle('has-side', !!S.opts.panel);
    S.el.classList.toggle('has-many', S.items.length > 1);

    if (isVideo(item)) {
      img.style.display = 'none';
      video.style.display = '';
      if (video.getAttribute('src') !== src) {
        spin.classList.add('show');
        video.setAttribute('src', src);
        video.play().catch(() => {});
      }
    } else {
      if (video.getAttribute('src')) stopVideo();
      video.style.display = 'none';
      img.style.display = '';
      if (img.getAttribute('src') !== src) {
        spin.classList.add('show');
        img.setAttribute('src', src);
      }
    }

    img.onload = () => spin.classList.remove('show');
    img.onerror = () => spin.classList.remove('show');

    /* Подпись и контекст */
    const author = item.author || {};
    S.el.querySelector('.sv-author').textContent = author.name || '';
    S.el.querySelector('.sv-context').textContent =
      [item.date || '', item.contextLabel || ''].filter(Boolean).join(' · ');
    S.el.querySelector('.sv-caption').textContent = item.caption || '';

    renderThumbs();
    renderActions();
    renderCounter();
    renderPanel();
    apply();
  }

  function renderCounter() {
    /* Счётчик живёт в подсказке снизу: на узком экране отдельная
       строка с цифрами съедала высоту и дублировала то, что видно по
       подсвеченному превью. */
    const hint = S.el.querySelector('.sv-hint');
    const many = S.items.length > 1;
    hint.innerHTML = many
      ? '<b>' + (S.index + 1) + ' / ' + S.items.length + '</b> · ← → листать · колесо — зум · 2× клик — приблизить · Esc — закрыть'
      : 'колесо — зум · 2× клик — приблизить · Esc — закрыть';
  }

  function renderThumbs() {
    const box = S.el.querySelector('.sv-thumbs');
    const many = S.items.length > 1;
    box.classList.toggle('show', many);
    if (!many) { box.innerHTML = ''; return; }
    box.innerHTML = S.items.map((it, i) => {
      if (isVideo(it)) {
        return '<button class="sv-thumb sv-thumb--video" data-i="' + i + '" title="Видео"><i class="fas fa-play"></i></button>';
      }
      const src = String(it.src || it.url || '')
        .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
      return '<button class="sv-thumb" data-i="' + i + '" title=""><img src="' + src + '" alt="" loading="lazy"></button>';
    }).join('');
    box.querySelectorAll('.sv-thumb').forEach(b => {
      b.addEventListener('click', () => {
        const i = parseInt(b.getAttribute('data-i'), 10) || 0;
        if (i === S.index) return;
        S.index = i;
        reset();
        render();
      });
    });
    markThumb();
  }

  function markThumb() {
    const box = S.el && S.el.querySelector('.sv-thumbs');
    if (!box) return;
    box.querySelectorAll('.sv-thumb').forEach(b => {
      b.classList.toggle('is-active', parseInt(b.getAttribute('data-i'), 10) === S.index);
    });
    const active = box.querySelector('.sv-thumb.is-active');
    if (active && active.scrollIntoView) {
      active.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  }

  function renderActions() {
    const box = S.el.querySelector('.sv-actions');
    const item = current() || {};
    const o = S.opts || {};
    const btn = (icon, title, act, cls) =>
      '<button class="sv-act' + (cls ? ' ' + cls : '') + '" data-act="' + act + '" title="' + esc(title) + '">'
      + '<i class="fas ' + icon + '"></i><span>' + esc(title) + '</span></button>';

    let html = '';
    if (o.likeUrl) {
      html += btn(item.liked ? 'fa-heart' : 'fa-heart', 'Нравится', 'like',
        item.liked ? 'is-active' : '');
      html += '<span class="sv-likes" data-likes>' + (item.likes || 0) + '</span>';
    }
    if (o.panel) html += btn('fa-comment', 'Комментарии', 'comments');
    if (o.share) html += btn('fa-share-alt', 'Поделиться', 'share');
    if (o.onEdit && item.can_edit) html += btn('fa-pen', 'Редактировать', 'edit');
    if (o.download !== false) html += btn('fa-download', 'Скачать', 'download');
    if (o.onDelete && item.can_delete) html += btn('fa-trash', 'Удалить', 'delete', 'sv-act--danger');
    if (o.extraActions) html += o.extraActions(item);
    box.innerHTML = html;

    box.onclick = e => {
      const b = e.target.closest('.sv-act');
      if (!b) return;
      const act = b.getAttribute('data-act');
      const it = current();
      if (act === 'like') { toggleLike(b, it); return; }
      if (act === 'comments') { toggleSide(true); return; }
      if (act === 'share' && o.share) { o.share(it); return; }
      if (act === 'edit' && o.onEdit) { o.onEdit(it); return; }
      if (act === 'delete' && o.onDelete) { o.onDelete(it); return; }
      if (act === 'download') { download(it); return; }
    };
  }

  async function toggleLike(btn, item) {
    if (!S.opts || !S.opts.likeUrl || !item || !item.id) return;
    btn.disabled = true;
    try {
      const res = await fetch(S.opts.likeUrl(item.id), { method: 'POST', credentials: 'same-origin' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json().catch(() => ({}));
      item.liked = data.liked !== undefined ? !!data.liked : !item.liked;
      if (data.likes_count !== undefined) item.likes = data.likes_count;
      else item.likes = Math.max(0, (item.likes || 0) + (item.liked ? 1 : -1));
      const cnt = S.el.querySelector('[data-likes]');
      if (cnt) cnt.textContent = item.likes;
      btn.classList.toggle('is-active', item.liked);
      if (S.opts.onChange) S.opts.onChange(item);
    } catch (e) {
      if (window.toast) window.toast('Не удалось поставить лайк', 'error');
    } finally {
      btn.disabled = false;
    }
  }

  function download(item) {
    if (!item) return;
    const a = document.createElement('a');
    a.href = item.src || item.url;
    a.download = (item.caption || 'photo').slice(0, 60) || 'photo';
    /* crossOrigin не нужен: файлы отдаются с того же origin, но атрибут
       не мешает и делает поведение явным. */
    a.setAttribute('crossorigin', 'anonymous');
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  /* ---------------------------------------------------------------- панель */

  function toggleSide(force) {
    S.sideOpen = force === undefined ? !S.sideOpen : !!force;
    S.el.classList.toggle('side-open', S.sideOpen);
    if (S.sideOpen) mountComments();
  }

  function renderPanel() {
    const side = S.el.querySelector('.sv-side');
    const want = !!(S.opts && S.opts.panel);
    side.classList.toggle('is-hidden', !want);
    if (want && S.sideOpen) mountComments();
  }

  function mountComments() {
    const o = S.opts || {};
    if (!o.panel || !o.commentsUrl) return;
    const box = S.el.querySelector('.sv-comments');
    const item = current();
    if (!box || !item || !item.id) return;
    /* Пересоздаём панель только при смене фото: иначе комментарии
       перезагружались бы при каждом листании стрелками. */
    if (box.dataset.forPhoto === String(item.id)) return;
    box.dataset.forPhoto = String(item.id);
    box.innerHTML = '';
    if (!window.SocialComments) {
      box.innerHTML = '<div class="cmt-error">Компонент комментариев не загружен</div>';
      return;
    }
    const base = o.commentsUrl(item);
    SocialComments.mount(box, {
      me: (window.activeProfile || ''),
      canLike: !!o.likeCommentUrl,
      listUrl: base,
      postUrl: base,
      delUrl: id => (o.delUrl ? o.delUrl(item, id) : base + '/' + encodeURIComponent(id)),
      likeUrl: o.likeCommentUrl ? id => o.likeCommentUrl(item, id) : null,
      placeholder: 'Написать комментарий…',
    });
  }

  /* ------------------------------------------------------------ публичное */

  return {
    open, close, go,
    /* Слой, который лежит поверх просмотрщика (редактор). Пока он открыт,
       Esc не должен закрывать сам просмотрщик. */
    pushOverlay: fn => stack.push(fn),
    popOverlay: fn => {
      const i = stack.indexOf(fn);
      if (i !== -1) stack.splice(i, 1);
    },
    isOpen: () => !!S.el && S.el.classList.contains('show'),
    refresh: render,
    current,
    openSide: () => toggleSide(true),
  };
})();

/* Совместимость со старыми вызовами lbOpen(...) со всех страниц. */
function lbOpen(items, index) {
  window.SocialViewer.open(items, index, {
    /* Боковой панели у старого вызова нет: она включается опцией panel. */
    download: true,
  });
}
function lbClose() { if (window.SocialViewer) window.SocialViewer.close(); }
