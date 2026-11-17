/* social-composer.js — общий редактор публикации для стены группы, ленты и
 * профиля.
 *
 * До этого каждая из трёх стен имела свой набор обработчиков: автоувеличение
 * textarea, счётчик символов, отправка по Enter, загрузка файлов и превью
 * были продублированы в трёх шаблонах и вели себя по-разному (в ленте
 * превью не было вовсе, в профиле счётчика не было). Здесь общая фабрика:
 * страницы передают только свои id и текст плейсхолдера.
 *
 * Подключение:
 *   SocialComposer.create({
 *     area: 'group-post-content',   // id textarea (обязательно)
 *     form: 'group-post-form',      // id контейнера — для подсветки фокуса
 *     count: 'group-post-count',    // id счётчика
 *     preview: 'group-media-preview',
 *     fileInput: 'group-media-input',
 *     chip: 'gpStickerChip',        // куда кладётся выбранный стикер
 *     picker: 'gpPicker', pickerBody: 'gpBody',
 *     max: 4000,
 *     placeholder: 'Написать пост…',
 *     onSubmit: (text, attachments, sticker) => {...},  // async
 *     groupId: () => currentGroup.id,   // для подсказок @
 *   })
 *
 * Возвращает объект с методами: value(), clear(), setSticker(), focus().
 */
(function () {
  'use strict';

  const API = '/api/social';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function toast(msg, type) {
    if (typeof window.toast === 'function') window.toast(msg, type);
  }

  function autoGrow(ta) {
    if (!ta) return;
    ta.style.height = 'auto';
    const max = ta.classList.contains('is-tall') ? 600 : 260;
    ta.style.height = Math.min(ta.scrollHeight + 2, max) + 'px';
  }

  function kindOf(file) {
    const t = file.type || '';
    if (t.startsWith('video/')) return 'video';
    if (t.startsWith('audio/')) return 'audio';
    if (t.startsWith('image/')) return 'image';
    return 'file';
  }

  function create(cfg) {
    const area = document.getElementById(cfg.area);
    if (!area) return null;

    const state = {
      atts: [],          // {type,url,title,uploading,preview}
      sticker: null,     // {url,title}
      max: cfg.max || 4000,
    };

    const count = cfg.count ? document.getElementById(cfg.count) : null;
    const preview = cfg.preview ? document.getElementById(cfg.preview) : null;
    const fileInput = cfg.fileInput ? document.getElementById(cfg.fileInput) : null;
    const chip = cfg.chip ? document.getElementById(cfg.chip) : null;
    const form = cfg.form ? document.getElementById(cfg.form) : null;

    /* ------------------------------------------------------------- текст */

    function syncCounter() {
      if (!count) return;
      const n = area.value.length;
      count.textContent = n > 200 ? n + ' / ' + state.max : String(n);
      count.classList.toggle('is-over', n > state.max);
    }

    function syncSubmit() {
      if (!form) return;
      const btn = form.querySelector('[data-sc-submit]');
      if (!btn) return;
      const busy = state.atts.some(a => a.uploading);
      btn.disabled = busy || (!area.value.trim() && !state.atts.length && !state.sticker);
    }

    autoGrow(area);
    syncCounter();
    syncSubmit();

    area.addEventListener('input', () => {
      autoGrow(area);
      syncCounter();
      syncSubmit();
    });

    area.addEventListener('keydown', e => {
      // Enter отправляет, Shift+Enter — перенос строки. Для длинных текстов
      // в профиле это неудобно, там отправка только по кнопке.
      if (cfg.enterSubmit === false) return;
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        submit();
      }
    });

    if (cfg.placeholder) area.setAttribute('placeholder', cfg.placeholder);

    const expand = form && form.querySelector('[data-sc-expand]');
    if (expand) {
      expand.addEventListener('click', () => {
        const tall = area.classList.toggle('is-tall');
        expand.classList.toggle('is-open', tall);
        autoGrow(area);
      });
    }

    /* --------------------------------------------------------- вложения */

    function renderPreview() {
      if (!preview) return;
      const items = state.atts.map((a, i) => {
        const thumb = a.type === 'image' && a.preview
          ? '<img src="' + esc(a.preview) + '" alt="">'
          : '<i class="fas ' + (a.type === 'video' ? 'fa-film'
            : a.type === 'audio' ? 'fa-music' : 'fa-file') + '"></i>';
        return '<div class="sc-att' + (a.uploading ? ' is-uploading' : '') + '">' +
          thumb +
          '<span class="sc-att-name">' + esc(a.title || '') + '</span>' +
          '<button type="button" data-sc-att-del="' + i + '" title="Убрать">' +
          '<i class="fas fa-times"></i></button></div>';
      }).join('');
      preview.innerHTML = items;
      preview.style.display = items ? 'flex' : 'none';
      preview.querySelectorAll('[data-sc-att-del]').forEach(b => {
        b.addEventListener('click', () => {
          state.atts.splice(parseInt(b.getAttribute('data-sc-att-del'), 10), 1);
          renderPreview();
          syncSubmit();
        });
      });
    }

    async function upload(file) {
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch(API + '/upload/post-media', {
        method: 'POST', body: fd, credentials: 'same-origin',
      });
      let data = {};
      try { data = await res.json(); } catch (e) { data = {}; }
      if (!res.ok) throw new Error(data.detail || ('Ошибка ' + res.status));
      return data.path || data.url;
    }

    async function addFiles(files) {
      for (const f of Array.from(files || [])) {
        const type = kindOf(f);
        const item = {
          type, title: f.name, uploading: true,
          preview: type === 'image' ? URL.createObjectURL(f) : '',
        };
        state.atts.push(item);
        renderPreview();
        syncSubmit();
        try {
          item.url = await upload(f);
          item.uploading = false;
        } catch (e) {
          state.atts = state.atts.filter(x => x !== item);
          toast('Не удалось загрузить ' + f.name + ': ' + e.message, 'error');
        }
        renderPreview();
        syncSubmit();
      }
    }

    if (fileInput) {
      fileInput.addEventListener('change', () => {
        const files = Array.from(fileInput.files || []);
        fileInput.value = '';
        addFiles(files);
      });
    }

    /* --------------------------------------------------------- стикер */

    function renderChip() {
      if (!chip) return;
      chip.innerHTML = state.sticker
        ? '<div class="sc-chip"><img src="' + esc(state.sticker.url) + '" alt="">' +
          'Стикер в посте' +
          '<button type="button" data-sc-chip-x title="Убрать"><i class="fas fa-times"></i></button></div>'
        : '';
      const x = chip.querySelector('[data-sc-chip-x]');
      if (x) x.addEventListener('click', () => { state.sticker = null; renderChip(); syncSubmit(); });
    }

    function setSticker(url, title) {
      state.sticker = url ? { url, title: title || 'Стикер' } : null;
      renderChip();
      syncSubmit();
    }

    if (cfg.picker && window.SOCIAL_EMOJI && window.SOCIAL_EMOJI.makeWallPicker) {
      area.id = area.id || cfg.area;
      const wp = SOCIAL_EMOJI.makeWallPicker({
        panel: cfg.picker, body: cfg.pickerBody, target: area.id,
        chips: cfg.chip ? [cfg.chip] : [],
        onSticker: url => setSticker(url),
      });
      window[cfg.picker] = wp;
      const toggleBtn = form && form.querySelector('[data-sc-emoji]');
      if (toggleBtn) toggleBtn.addEventListener('click', () => wp.toggle());
    }

    /* ------------------------------------------------------ упоминания */

    if (window.SocialMentions) {
      SocialMentions.attach(area, { groupId: cfg.groupId || null });
    }

    /* --------------------------------------------------------- отправка */

    async function submit() {
      const text = area.value.trim();
      if (!text && !state.atts.length && !state.sticker) return;
      if (state.atts.some(a => a.uploading)) {
        toast('Дождитесь загрузки вложений', 'error');
        return;
      }
      if (text.length > state.max) {
        toast('Слишком длинный текст: ' + text.length + ' из ' + state.max, 'error');
        return;
      }
      const btn = form && form.querySelector('[data-sc-submit]');
      if (btn) btn.disabled = true;
      const atts = state.atts.filter(a => a.url).map(a => ({
        type: a.type, url: a.url, title: a.title || '',
      }));
      try {
        await cfg.onSubmit(text, atts, state.sticker);
        clear();
      } catch (e) {
        toast(e.message || 'Не удалось опубликовать', 'error');
      } finally {
        syncSubmit();
      }
    }

    function clear() {
      area.value = '';
      state.atts = [];
      state.sticker = null;
      renderPreview();
      renderChip();
      autoGrow(area);
      syncCounter();
      syncSubmit();
    }

    const sendBtn = form && form.querySelector('[data-sc-submit]');
    if (sendBtn) sendBtn.addEventListener('click', submit);

    window.addEventListener('resize', () => autoGrow(area));

    return {
      value: () => area.value,
      clear,
      setSticker,
      focus: () => area.focus(),
      submit,
      attachments: () => state.atts.filter(a => a.url),

      /**
       * Добавить уже готовое вложение (ссылку из альбома группы или из
       * медиа-библиотеки). Загружать ничего не нужно — только показать
       * превью и учесть при отправке.
       *
       * Без этого окна «Прикрепить» и «Выбрать из альбома» рисовали бы свои
       * превью в тот же контейнер и затирать бы уже добавленные файлы.
       */
      addAttachment: (att) => {
        if (!att || !(att.url || att.src)) return;
        state.atts.push({
          type: att.type || 'image',
          url: att.url || att.src,
          title: att.title || '',
          uploading: false,
          preview: att.type === 'image' ? (att.url || att.src) : '',
        });
        renderPreview();
        syncSubmit();
      },
    };
  }

  window.SocialComposer = { create };
})();
