/* Редактор фото — открывается поверх просмотрщика.
 *
 * Предпросмотр делается на canvas прямо в браузере: операции те же, что
 * потом применит PIL на сервере, поэтому предпросмотр не расходится с
 * результатом. Итоговый файл собирает сервер — canvas отдаёт только
 * координаты обрезки, а не картинку, и качество оригинала не теряется.
 *
 * Серверный формат операций совпадает с social_photo_edit.py:
 *   {type:'crop', x,y,w,h} {type:'rotate',degrees} {type:'flip_h'} {type:'flip_v'}
 *   {type:'brightness'|'contrast'|'saturation', factor} {type:'grayscale'} {type:'reset'}
 *
 * Использование:
 *   SocialPhotoEditor.open(photoId, onSaved)
 * onSaved(photoId) вызывается после сохранения, чтобы вызывающая
 * поверхность перечитала альбом и показала новую версию.
 */
window.SocialPhotoEditor = (function () {
  'use strict';

  const API = '/api/social';
  /* Превью рисуется с ограничением по длинной стороне: фото на 8000px
     занимал бы в canvas сотни мегабайт и подвешивал вкладку. */
  const PREVIEW_MAX = 1600;

  const PRESETS = [
    { id: 'original', name: 'Оригинал', css: 'none', ops: [] },
    { id: 'vivid', name: 'Яркие', css: 'saturate(1.5) contrast(1.12)', ops: [{ type: 'saturation', factor: 1.5 }, { type: 'contrast', factor: 1.12 }] },
    { id: 'soft', name: 'Мягкие', css: 'brightness(1.06) contrast(.9) saturate(.92)', ops: [{ type: 'brightness', factor: 1.06 }, { type: 'contrast', factor: 0.9 }, { type: 'saturation', factor: 0.92 }] },
    { id: 'punch', name: 'Контраст', css: 'contrast(1.35) saturate(1.12)', ops: [{ type: 'contrast', factor: 1.35 }, { type: 'saturation', factor: 1.12 }] },
    { id: 'mono', name: 'Ч/Б', css: 'grayscale(1) contrast(1.05)', ops: [{ type: 'grayscale' }] },
    { id: 'noir', name: 'Нуар', css: 'grayscale(1) contrast(1.4) brightness(.94)', ops: [{ type: 'grayscale' }, { type: 'contrast', factor: 1.4 }, { type: 'brightness', factor: 0.94 }] },
    { id: 'sepia', name: 'Сепия', css: 'sepia(.75) contrast(1.08) saturate(1.1)', ops: [{ type: 'brightness', factor: 1.04 }, { type: 'saturation', factor: 0.75 }] },
    { id: 'warm', name: 'Тёплые', css: 'sepia(.32) saturate(1.35) brightness(1.05)', ops: [{ type: 'saturation', factor: 1.35 }, { type: 'brightness', factor: 1.05 }] },
    { id: 'cool', name: 'Холодные', css: 'saturate(.85) hue-rotate(-12deg) brightness(1.04)', ops: [{ type: 'saturation', factor: 0.85 }, { type: 'brightness', factor: 1.04 }] },
    { id: 'vintage', name: 'Винтаж', css: 'sepia(.4) contrast(.92) brightness(1.08) saturate(.8)', ops: [{ type: 'contrast', factor: 0.92 }, { type: 'brightness', factor: 1.08 }, { type: 'saturation', factor: 0.8 }] },
    { id: 'fade', name: 'Выцветшие', css: 'contrast(.82) brightness(1.1) saturate(.7)', ops: [{ type: 'contrast', factor: 0.82 }, { type: 'brightness', factor: 1.1 }, { type: 'saturation', factor: 0.7 }] },
    { id: 'ink', name: 'Чернила', css: 'grayscale(1) contrast(2.1)', ops: [{ type: 'grayscale' }, { type: 'contrast', factor: 2.1 }] },
  ];

  const ST = {
    el: null, built: false,
    photoId: '', img: null,        // исходное изображение
    naturalW: 0, naturalH: 0,
    scale: 1,                       // во сколько раз превью меньше оригинала
    rotate: 0, flipH: false, flipV: false,
    brightness: 1, contrast: 1, saturation: 1, grayscale: false,
    preset: 'original',
    crop: null,                     // {x,y,w,h} в координатах ОРИГИНАЛА
    busy: false,
    onSaved: null,
  };

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function toast(msg, type) {
    if (typeof window.toast === 'function') window.toast(msg, type);
  }

  /* --------------------------------------------------------- геометрия */

  /* Превью может быть повёрнуто на 90°, поэтому прямоугольник обрезки
     живёт в системе координат ОРИГИНАЛА и пересчитывается при отрисовке
     рамки. Без этого поворот ломал бы рамку: она уезжала бы за края. */
  function previewSize() {
    const swap = ST.rotate % 180 !== 0;
    return {
      w: swap ? ST.previewH : ST.previewW,
      h: swap ? ST.previewW : ST.previewH,
    };
  }

  function cropToPreview() {
    if (!ST.crop) return null;
    const pw = previewSize().w;
    const ph = previewSize().h;
    const swap = ST.rotate % 180 !== 0;
    /* Оригинальные координаты после поворота становятся транспонированными. */
    const sx = swap ? ST.crop.y : ST.crop.x;
    const sy = swap ? ST.crop.x : ST.crop.y;
    const sw = swap ? ST.crop.h : ST.crop.w;
    const sh = swap ? ST.crop.w : ST.crop.h;
    return {
      x: (sx / ST.naturalW) * pw,
      y: (sy / ST.naturalH) * ph,
      w: (sw / ST.naturalW) * pw,
      h: (sh / ST.naturalH) * ph,
    };
  }

  function previewToCrop(r) {
    const pw = previewSize().w;
    const ph = previewSize().h;
    const x = (r.x / pw) * ST.naturalW;
    const y = (r.y / ph) * ST.naturalH;
    const w = (r.w / pw) * ST.naturalW;
    const h = (r.h / ph) * ST.naturalH;
    const swap = ST.rotate % 180 !== 0;
    return swap
      ? { x: y, y: x, w: h, h: w }
      : { x, y, w, h };
  }

  function filterCss() {
    const parts = [];
    if (ST.brightness !== 1) parts.push('brightness(' + ST.brightness + ')');
    if (ST.contrast !== 1) parts.push('contrast(' + ST.contrast + ')');
    if (ST.saturation !== 1) parts.push('saturate(' + ST.saturation + ')');
    if (ST.grayscale) parts.push('grayscale(1)');
    /* Поворот и отражения задаются трансформацией холста, а не фильтром:
       CSS-фильтр их не поддерживает. */
    return parts.length ? parts.join(' ') : 'none';
  }

  /* ------------------------------------------------------------- разметка */

  function ensure() {
    if (ST.built) return ST.el;
    const el = document.createElement('div');
    el.className = 'pe-overlay';
    el.hidden = true;
    el.innerHTML =
      '<div class="pe-modal">'
      + '<div class="pe-head">'
        + '<button class="pe-close" title="Закрыть"><i class="fas fa-times"></i></button>'
        + '<div class="pe-title">Редактирование фото</div>'
        + '<div class="pe-head-actions">'
          + '<button class="btn btn-secondary btn-sm" data-pe="history">История версий</button>'
        + '</div>'
      + '</div>'
      + '<div class="pe-body">'
        + '<div class="pe-canvas-wrap" data-pe-canvas>'
          + '<canvas class="pe-canvas"></canvas>'
          + '<div class="pe-crop-box" hidden>'
            + '<div class="pe-handle" data-h="nw"></div><div class="pe-handle" data-h="ne"></div>'
            + '<div class="pe-handle" data-h="sw"></div><div class="pe-handle" data-h="se"></div>'
            + '<div class="pe-handle" data-h="n"></div><div class="pe-handle" data-h="s"></div>'
            + '<div class="pe-handle" data-h="w"></div><div class="pe-handle" data-h="e"></div>'
          + '</div>'
          + '<div class="pe-spinner"><i class="fas fa-spinner fa-spin"></i></div>'
        + '</div>'
        + '<div class="pe-panel">'
          + '<div class="pe-section">'
            + '<div class="pe-label">Фильтр</div>'
            + '<div class="pe-presets" data-pe-presets></div>'
          + '</div>'
          + '<div class="pe-section">'
            + '<div class="pe-label">Поворот и отражение</div>'
            + '<div class="pe-row">'
              + '<button class="btn btn-secondary btn-xs" data-pe="rot-left" title="Повернуть влево"><i class="fas fa-undo"></i> ←</button>'
              + '<button class="btn btn-secondary btn-xs" data-pe="rot-right" title="Повернуть вправо"><i class="fas fa-redo"></i> →</button>'
              + '<button class="btn btn-secondary btn-xs" data-pe="flip-h" title="Отразить по горизонтали"><i class="fas fa-arrows-left-right"></i></button>'
              + '<button class="btn btn-secondary btn-xs" data-pe="flip-v" title="Отразить по вертикали"><i class="fas fa-arrows-up-down"></i></button>'
            + '</div>'
          + '</div>'
          + '<div class="pe-section">'
            + '<div class="pe-label">Обрезка</div>'
            + '<div class="pe-row">'
              + '<select class="pe-ratio" data-pe-ratio>'
                + '<option value="">Свободно</option>'
                + '<option value="1">1 : 1</option>'
                + '<option value="1.7778">16 : 9</option>'
                + '<option value="1.3333">4 : 3</option>'
                + '<option value="0.75">3 : 4</option>'
              + '</select>'
              + '<button class="btn btn-secondary btn-xs" data-pe="crop-all">Всё фото</button>'
            + '</div>'
          + '</div>'
          + '<div class="pe-section">'
            + '<div class="pe-label">Настройки</div>'
            + '<div class="pe-sliders" data-pe-sliders></div>'
          + '</div>'
        + '</div>'
      + '</div>'
      + '<div class="pe-foot">'
        + '<button class="btn btn-secondary" data-pe="cancel">Отмена</button>'
        + '<button class="btn btn-secondary" data-pe="reset">Вернуть оригинал</button>'
        + '<span class="grow"></span>'
        + '<button class="btn btn-primary" data-pe="save">Сохранить</button>'
      + '</div>'
      + '</div>';
    document.body.appendChild(el);
    ST.el = el;
    ST.built = true;

    el.querySelector('.pe-presets').innerHTML = PRESETS.map(p =>
      '<button class="pe-preset" data-preset="' + p.id + '" title="' + esc(p.name) + '">'
      + '<span class="pe-preset-thumb" data-css="' + esc(p.css) + '"></span>'
      + '<span class="pe-preset-name">' + esc(p.name) + '</span></button>').join('');

    el.querySelector('[data-pe-sliders]').innerHTML = [
      ['brightness', 'Яркость', 0.4, 2, 0.02],
      ['contrast', 'Контраст', 0.4, 2, 0.02],
      ['saturation', 'Насыщенность', 0, 2, 0.02],
    ].map(s => sliderHtml(s)).join('');

    bind();
    return el;
  }

  function sliderHtml(s) {
    return '<label class="pe-slider">'
      + '<span class="pe-slider-name">' + s[1] + '</span>'
      + '<input type="range" min="' + s[2] + '" max="' + s[3] + '" step="' + s[4]
      + '" value="1" data-slider="' + s[0] + '">'
      + '<span class="pe-slider-val" data-slider-val="' + s[0] + '">1.00</span>'
      + '</label>';
  }

  /* -------------------------------------------------------------- отрисовка */

  function render() {
    const cv = ST.el.querySelector('.pe-canvas');
    if (!cv || !ST.img) return;
    const { w, h } = previewSize();
    cv.width = w;
    cv.height = h;
    const ctx = cv.getContext('2d');
    ctx.save();
    ctx.filter = filterCss();
    ctx.clearRect(0, 0, w, h);
    ctx.translate(w / 2, h / 2);
    ctx.rotate(ST.rotate * Math.PI / 180);
    ctx.scale(ST.flipH ? -1 : 1, ST.flipV ? -1 : 1);
    /* Исходник рисуется с поворотом на -90°, потому что ctx.rotate идёт
       по часовой стрелке, а степени поворота фото считаются так же. */
    const rot = ST.rotate;
    let dw = w, dh = h;
    if (rot % 180 !== 0) { dw = h; dh = w; }
    ctx.drawImage(ST.img, -dw / 2, -dh / 2, dw, dh);
    ctx.restore();
    ctx.filter = 'none';
    drawCropBox();
  }

  function drawCropBox() {
    const box = ST.el.querySelector('.pe-crop-box');
    const wrap = ST.el.querySelector('[data-pe-canvas]');
    if (!ST.crop) {
      box.hidden = true;
      wrap.classList.remove('is-cropping');
      return;
    }
    const r = cropToPreview();
    const cv = ST.el.querySelector('.pe-canvas');
    const cr = cv.getBoundingClientRect();
    const wr = wrap.getBoundingClientRect();
    /* Превью показывается с object-fit: contain, поэтому масштаб
       кадра относительно контейнера нужно вычислить, а не взять 1:1. */
    const fit = Math.min(wr.width / cv.width, wr.height / cv.height);
    const offX = (wr.width - cv.width * fit) / 2;
    const offY = (wr.height - cv.height * fit) / 2;
    const left = cr.left - wr.left + offX + r.x * fit;
    const top = cr.top - wr.top + offY + r.y * fit;
    box.hidden = false;
    box.style.left = left + 'px';
    box.style.top = top + 'px';
    box.style.width = Math.max(8, r.w * fit) + 'px';
    box.style.height = Math.max(8, r.h * fit) + 'px';
    wrap.classList.add('is-cropping');
  }

  /* ------------------------------------------------------------- операции */

  function collectOps() {
    const ops = [];
    if (ST.rotate) ops.push({ type: 'rotate', degrees: ST.rotate });
    if (ST.flipH) ops.push({ type: 'flip_h' });
    if (ST.flipV) ops.push({ type: 'flip_v' });
    if (ST.crop) ops.push({ type: 'crop', x: Math.round(ST.crop.x), y: Math.round(ST.crop.y), w: Math.round(ST.crop.w), h: Math.round(ST.crop.h) });
    if (ST.grayscale) ops.push({ type: 'grayscale' });
    if (ST.brightness !== 1) ops.push({ type: 'brightness', factor: +ST.brightness.toFixed(2) });
    if (ST.contrast !== 1) ops.push({ type: 'contrast', factor: +ST.contrast.toFixed(2) });
    if (ST.saturation !== 1) ops.push({ type: 'saturation', factor: +ST.saturation.toFixed(2) });
    return ops;
  }

  function resetState() {
    ST.rotate = 0; ST.flipH = false; ST.flipV = false;
    ST.brightness = 1; ST.contrast = 1; ST.saturation = 1; ST.grayscale = false;
    ST.preset = 'original'; ST.crop = null;
  }

  function applyPreset(id) {
    const p = PRESETS.find(x => x.id === id);
    if (!p) return;
    ST.preset = id;
    ST.brightness = 1; ST.contrast = 1; ST.saturation = 1; ST.grayscale = false;
    ST.el.querySelectorAll('[data-slider]').forEach(s => { s.value = 1; });
    ST.el.querySelectorAll('[data-slider-val]').forEach(s => { s.textContent = '1.00'; });
    if (id === 'mono') ST.grayscale = true;
    ST.el.querySelectorAll('.pe-preset').forEach(b => {
      b.classList.toggle('is-active', b.getAttribute('data-preset') === id);
    });
    render();
  }

  /* --------------------------------------------------------------- загрузка */

  async function loadImage(url) {
    const wrap = ST.el.querySelector('[data-pe-canvas]');
    wrap.classList.add('is-loading');
    try {
      /* Файлы соцсети отдаются с того же origin, поэтому canvas не
         помечается как «испорченный» и его можно читать обратно. */
      ST.img = await new Promise((res, rej) => {
        const im = new Image();
        im.onload = () => res(im);
        im.onerror = () => rej(new Error('Не удалось загрузить фото'));
        im.src = url;
      });
      ST.naturalW = ST.img.naturalWidth;
      ST.naturalH = ST.img.naturalHeight;
      const k = Math.min(1, PREVIEW_MAX / Math.max(ST.naturalW, ST.naturalH));
      ST.previewW = Math.round(ST.naturalW * k);
      ST.previewH = Math.round(ST.naturalH * k);
      render();
    } catch (e) {
      toast(e.message || 'Не удалось загрузить фото', 'error');
    } finally {
      wrap.classList.remove('is-loading');
    }
  }

  /* ------------------------------------------------------------------ API */

  async function api(url, opts) {
    opts = opts || {};
    const headers = Object.assign({}, opts.headers || {});
    if (opts.body !== undefined && !('Content-Type' in headers)) {
      headers['Content-Type'] = 'application/json';
    }
    const res = await fetch(url, Object.assign({}, opts, { headers, credentials: 'same-origin' }));
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok) throw new Error(data.detail || data.error || ('Ошибка ' + res.status));
    return data;
  }

  async function save(resetToOriginal) {
    if (ST.busy) return;
    const ops = resetToOriginal ? [{ type: 'reset' }] : collectOps();
    if (!resetToOriginal && !ops.length) { toast('Ничего не изменено'); return; }
    ST.busy = true;
    const btn = ST.el.querySelector('[data-pe="save"]');
    btn.disabled = true;
    btn.textContent = resetToOriginal ? 'Возврат…' : 'Сохранение…';
    try {
      const r = await api(`${API}/photos/${encodeURIComponent(ST.photoId)}/edit`, {
        method: 'POST', body: JSON.stringify({ operations: ops }),
      });
      await loadImage(r.url);
      resetState();
      applyPreset('original');
      toast('Изменения сохранены');
      if (ST.onSaved) ST.onSaved(ST.photoId, r.url);
    } catch (e) {
      toast('Не удалось сохранить: ' + e.message, 'error');
    } finally {
      ST.busy = false;
      btn.disabled = false;
      btn.textContent = 'Сохранить';
    }
  }

  async function showHistory() {
    try {
      const d = await api(`${API}/photos/${encodeURIComponent(ST.photoId)}/versions`);
      if (!d.versions || !d.versions.length) { toast('Правок пока не было'); return; }
      const lines = d.versions.map(v => {
        const when = v.created_at ? new Date(v.created_at * 1000).toLocaleString('ru-RU') : '';
        const cur = v.is_current ? ' · текущая' : '';
        return v.id + '\t' + (v.author_name || v.author_id) + '\t' + when + cur;
      });
      const pick = window.UI && UI.choose
        ? await UI.choose('История версий — куда вернуться?',
            lines.map(l => ({ value: l.split('\t')[0], title: l.split('\t').slice(1).join(' · '), desc: '' })),
            (d.versions.find(v => v.is_current) || d.versions[0]).id)
        : null;
      if (!pick || pick === 'null') return;
      const r = await api(`${API}/photos/${encodeURIComponent(ST.photoId)}/revert`, {
        method: 'POST', body: JSON.stringify({ version_id: pick }),
      });
      await loadImage(r.url);
      resetState();
      applyPreset('original');
      toast('Восстановлена выбранная версия');
      if (ST.onSaved) ST.onSaved(ST.photoId, r.url);
    } catch (e) {
      toast('Не удалось открыть историю: ' + e.message, 'error');
    }
  }

  /* ------------------------------------------------------------ обработчики */

  function bind() {
    const el = ST.el;

    el.querySelector('.pe-close').addEventListener('click', close);
    el.addEventListener('mousedown', e => { if (e.target === el) close(); });
    document.addEventListener('keydown', e => {
      if (el.hidden) return;
      if (e.key === 'Escape') { e.preventDefault(); close(); }
    });

    el.querySelector('[data-pe="cancel"]').addEventListener('click', close);
    el.querySelector('[data-pe="save"]').addEventListener('click', () => save(false));
    el.querySelector('[data-pe="reset"]').addEventListener('click', () => save(true));
    el.querySelector('[data-pe="history"]').addEventListener('click', showHistory);

    el.querySelector('[data-pe="rot-left"]').addEventListener('click', () => {
      ST.rotate = (ST.rotate + 270) % 360; render();
    });
    el.querySelector('[data-pe="rot-right"]').addEventListener('click', () => {
      ST.rotate = (ST.rotate + 90) % 360; render();
    });
    el.querySelector('[data-pe="flip-h"]').addEventListener('click', () => {
      ST.flipH = !ST.flipH; render();
    });
    el.querySelector('[data-pe="flip-v"]').addEventListener('click', () => {
      ST.flipV = !ST.flipV; render();
    });
    el.querySelector('[data-pe="crop-all"]').addEventListener('click', () => {
      ST.crop = { x: 0, y: 0, w: ST.naturalW, h: ST.naturalH };
      render();
    });

    el.querySelector('[data-pe-ratio]').addEventListener('change', e => {
      const r = parseFloat(e.target.value);
      if (!r) { ST.crop = null; render(); return; }
      const { w, h } = previewSize();
      /* Рамка вписывается по большей стороне, чтобы не выйти за края. */
      let cw = w, ch = w / r;
      if (ch > h) { ch = h; cw = h * r; }
      ST.crop = previewToCrop({ x: (w - cw) / 2, y: (h - ch) / 2, w: cw, h: ch });
      render();
    });

    el.querySelectorAll('.pe-preset').forEach(b => {
      b.addEventListener('click', () => applyPreset(b.getAttribute('data-preset')));
    });

    el.querySelectorAll('[data-slider]').forEach(s => {
      s.addEventListener('input', () => {
        const key = s.getAttribute('data-slider');
        const v = parseFloat(s.value);
        ST[key] = v;
        /* Ползунок и пресет несовместимы: как только пользователь тронул
           ползунок, активный пресет снимается, иначе подпись «Яркие»
           продолжала бы гореть при уже других значениях. */
        ST.preset = 'custom';
        el.querySelectorAll('.pe-preset').forEach(b => b.classList.remove('is-active'));
        el.querySelector('[data-slider-val="' + key + '"]').textContent = v.toFixed(2);
        if (key === 'saturation' && v === 0) ST.grayscale = true;
        render();
      });
    });

    bindCropDrag();
  }

  /* Рамка обрезки перетаскивается за углы и стороны. Размер рамки
     ограничивается пределами холста, иначе она уезжала за картинку
     и сервер получал координаты больше исходника. */
  function bindCropDrag() {
    const el = ST.el;
    const box = el.querySelector('.pe-crop-box');
    const wrap = el.querySelector('[data-pe-canvas]');
    let drag = null;

    const hit = (e) => {
      const h = e.target.getAttribute && e.target.getAttribute('data-h');
      return h || null;
    };

    wrap.addEventListener('mousedown', e => {
      if (!ST.crop) {
        /* Клик по фото без активной рамки начинает её с этого места. */
        const r = pointInCanvas(wrap, e);
        if (!r) return;
        ST.crop = previewToCrop({ x: r.x - 20, y: r.y - 20, w: 40, h: 40 });
        render();
        return;
      }
      const h = hit(e);
      if (!h) return;
      e.preventDefault();
      drag = { h, start: pointInCanvas(wrap, e), orig: Object.assign({}, ST.crop) };
    });

    document.addEventListener('mousemove', e => {
      if (!drag) return;
      const r = pointInCanvas(wrap, e);
      if (!r) return;
      const dx = r.x - drag.start.x;
      const dy = r.y - drag.start.y;
      const { w, h } = previewSize();
      const MIN = 12;
      let nx = drag.orig.x, ny = drag.orig.y, nw = drag.orig.w, nh = drag.orig.h;
      /* Рамка хранится в координатах ОРИГИНАЛА, курсор ходит по превью,
         поэтому смещение переводится тем же соотношением сторон. */
      const px = dx * (ST.naturalW / w);
      const py = dy * (ST.naturalH / h);
      if (drag.h === 'nw') { nx += px; ny += py; nw -= px; nh -= py; }
      else if (drag.h === 'ne') { ny += py; nw += px; nh -= py; }
      else if (drag.h === 'sw') { nx += px; nw -= px; nh += py; }
      else if (drag.h === 'se') { nw += px; nh += py; }
      else if (drag.h === 'n') { ny += py; nh -= py; }
      else if (drag.h === 's') { nh += py; }
      else if (drag.h === 'w') { nx += px; nw -= px; }
      else if (drag.h === 'e') { nw += px; }
      if (nw < MIN) { nw = MIN; nx = Math.min(nx, ST.naturalW - MIN); }
      if (nh < MIN) { nh = MIN; ny = Math.min(ny, ST.naturalH - MIN); }
      nx = Math.max(0, Math.min(nx, ST.naturalW - nw));
      ny = Math.max(0, Math.min(ny, ST.naturalH - nh));
      ST.crop = { x: nx, y: ny, w: nw, h: nh };
      render();
    });

    document.addEventListener('mouseup', () => { drag = null; });

    // Сенсор: рамка тянется одним пальцем так же, как мышью.
    wrap.addEventListener('touchstart', e => {
      if (!ST.crop) return;
      const t = e.changedTouches[0];
      const target = document.elementFromPoint(t.clientX, t.clientY);
      const h = target && target.getAttribute ? target.getAttribute('data-h') : null;
      if (!h) return;
      drag = { h, start: pointInCanvas(wrap, t), orig: Object.assign({}, ST.crop) };
      e.preventDefault();
    }, { passive: false });
    wrap.addEventListener('touchmove', e => {
      if (!drag) return;
      const t = e.changedTouches[0];
      const r = pointInCanvas(wrap, t);
      if (!r) return;
      const { w, h } = previewSize();
      const dx = r.x - drag.start.x;
      const dy = r.y - drag.start.y;
      const px = dx * (ST.naturalW / w);
      const py = dy * (ST.naturalH / h);
      let nx = drag.orig.x, ny = drag.orig.y, nw = drag.orig.w, nh = drag.orig.h;
      const MIN = 12;
      if (drag.h === 'nw') { nx += px; ny += py; nw -= px; nh -= py; }
      else if (drag.h === 'ne') { ny += py; nw += px; nh -= py; }
      else if (drag.h === 'sw') { nx += px; nw -= px; nh += py; }
      else if (drag.h === 'se') { nw += px; nh += py; }
      if (nw < MIN) nw = MIN;
      if (nh < MIN) nh = MIN;
      ST.crop = {
        x: Math.max(0, Math.min(nx, ST.naturalW - nw)),
        y: Math.max(0, Math.min(ny, ST.naturalH - nh)),
        w: nw, h: nh,
      };
      render();
      e.preventDefault();
    }, { passive: false });
    wrap.addEventListener('touchend', () => { drag = null; });
  }

  /* Координаты точки в системе превью с учётом object-fit: contain. */
  function pointInCanvas(wrap, e) {
    const cv = ST.el.querySelector('.pe-canvas');
    const cr = cv.getBoundingClientRect();
    const wr = wrap.getBoundingClientRect();
    const fit = Math.min(wr.width / cv.width, wr.height / cv.height);
    const offX = (wr.width - cv.width * fit) / 2;
    const offY = (wr.height - cv.height * fit) / 2;
    const x = (e.clientX - cr.left - offX) / fit;
    const y = (e.clientY - cr.top - offY) / fit;
    return { x, y };
  }

  /* ------------------------------------------------------------------ публ. */

  function open(photoId, onSaved) {
    if (!photoId) return;
    ensure();
    ST.photoId = photoId;
    ST.onSaved = onSaved || null;
    resetState();
    applyPreset('original');
    ST.el.hidden = false;
    document.body.style.overflow = 'hidden';
    /* Регистрируем слой в стеке просмотрщика: пока открыт редактор, Esc
       не должен закрывать сам просмотрщик вместе с ним. */
    if (window.SocialViewer && window.SocialViewer.pushOverlay) {
      window.SocialViewer.pushOverlay(overlayToken);
    }
    api(`${API}/photos/${encodeURIComponent(photoId)}`)
      .then(d => loadImage(d.url))
      .catch(e => { toast('Не удалось загрузить фото: ' + e.message, 'error'); close(); });
  }

  const overlayToken = {};

  function close() {
    if (!ST.el) return;
    ST.el.hidden = true;
    document.body.style.overflow = '';
    if (window.SocialViewer && window.SocialViewer.popOverlay) {
      window.SocialViewer.popOverlay(overlayToken);
    }
  }

  function isOpen() { return !!ST.el && !ST.el.hidden; }

  return { open, close, isOpen };
})();
