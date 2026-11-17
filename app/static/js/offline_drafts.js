/* ОФЛАЙН-ЧЕРНОВИКИ ЗАМЕТОК. Подключается снизу offline_store.js и только
   когда в админке включён отдельный рубильник decentral.offline.drafts
   (по умолчанию выключен). Сама страница заметок этот файл не знает: если
   рубильник выключен, сюда даже не придёт запрос на загрузку.

   Почему это безопасно по построению:
   - id заметки придумывает телефон, а не сервер (notes_model_module.save_note
     принимает data["id"] и делает upsert). Значит повторная отправка того же
     черновика обновляет ту же заметку, а не заводит вторую — обрыв связи в
     момент ответа больше не порождает дубли.
   - локальная запись удаляется только после ответа сервера с ok:true.
   - Никаких серверных маршрутов здесь нет: используется ровно тот
     /api/notes/save, которым редактор пишет заметки сейчас.
   - Черновики НЕ удаляются при выключении рубильника: это написанный
     пользователем текст, а не копия чужих данных. Стереть можно только
     кнопкой в самом списке с подтверждением.
   - Вложения (фото, аудио, файлы) офлайн не поддерживаются: их нельзя
     загрузить без сети, а притворяться загруженными — значит потерять. */
(function () {
  const DB = 'infinity-drafts';
  const STORE = 'drafts';
  const SAVE_URL = '/api/notes/save';
  const PROBE_URL = '/api/offload/config';   // дешёвый и без прав админa
  const ANCHOR = 'offlineDownloadBtn';
  const MAX_DRAFTS = 200;

  // ── хранилище ────────────────────────────────────────────────────────────
  function openAt(version) {
    return new Promise((res, rej) => {
      const r = indexedDB.open(DB, version);
      r.onupgradeneeded = () => {
        const db = r.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: 'id' });
        }
      };
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error || new Error('indexedDB недоступен'));
      r.onblocked = () => rej(new Error('indexedDB заблокирован'));
    });
  }

  let dbPromise = null;
  function openDb() {
    // Версию нельзя задать фиксированную: запрос к базе с более высокой
    // версией падает с VersionError. Открываем «какая есть», и только если
    // хранилища нет — ровно +1 (тот же приём, что в offline_store.js).
    if (!dbPromise) {
      dbPromise = new Promise((res, rej) => {
        if (!('indexedDB' in window)) return rej(new Error('нет indexedDB'));
        const r = indexedDB.open(DB);
        r.onsuccess = () => {
          const db = r.result;
          if (db.objectStoreNames.contains(STORE)) return res(db);
          const next = db.version + 1;
          db.close();
          openAt(next).then(res, rej);
        };
        r.onerror = () => rej(r.error || new Error('indexedDB недоступен'));
        r.onupgradeneeded = () => {
          const db = r.result;
          if (!db.objectStoreNames.contains(STORE)) {
            db.createObjectStore(STORE, { keyPath: 'id' });
          }
        };
      }).catch(e => { dbPromise = null; throw e; });
    }
    return dbPromise;
  }

  function one(mode, fn) {
    return openDb().then(db => new Promise((res, rej) => {
      try {
        const t = db.transaction(STORE, mode);
        const r = fn(t.objectStore(STORE));
        t.onerror = t.onabort = () => rej(t.error || new Error('транзакция не удалась'));
        r.onsuccess = () => res(r.result);
        r.onerror = () => rej(r.error || new Error('запрос не удался'));
      } catch (e) { rej(e); }
    }));
  }

  const list = () => one('readonly', s => s.getAll()).then(a =>
    (a || []).slice().sort((x, y) => (y.updated || 0) - (x.updated || 0)));
  const put = (d) => one('readwrite', s => s.put(d));
  const del = (id) => one('readwrite', s => s.delete(id));
  const clearAll = () => one('readwrite', s => s.clear());

  // ── сеть ─────────────────────────────────────────────────────────────────
  const reachable = () => fetch(PROBE_URL, { cache: 'no-store' })
    .then(r => !!r.ok).catch(() => false);

  function newId() {
    // Только [a-z0-9]: id заметки становится именем файла на сервере.
    return 'od' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }

  async function pushDraft(d) {
    const r = await fetch(SAVE_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: d.id, title: d.title || 'Без названия',
        content: d.content || '', folder: d.folder || 'inbox',
        tags: d.tags || [],
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.error) throw new Error(j.error || ('сервер ответил ' + r.status));
    return j;
  }

  let flushing = false;
  async function flush(manual) {
    if (flushing) return { ok: true, skipped: true };
    flushing = true;
    let sent = 0, failed = null;
    try {
      if (!await reachable()) return { ok: false, reason: 'ПК недоступен' };
      for (const d of (await list()).slice().reverse()) {   // старые — первыми
        try {
          await pushDraft(d);
          await del(d.id);
          sent++;
        } catch (e) {
          // Пробой сети или отказ валидации: очередь останавливается здесь,
          // а не идёт дальше — иначе при битом запросе мы бы перебрали все
          // черновики и каждый раз словили таймаут.
          failed = String(e && e.message || e);
          try { d.lastError = failed; await put(d); } catch (e2) {}
          break;
        }
      }
      render();
      if (manual) note(failed ? 'Не отправлено: ' + failed
                              : (sent ? 'Отправлено: ' + sent : 'черновиков нет'));
      return { ok: !failed, sent, failed };
    } finally {
      flushing = false;
    }
  }

  // ── интерфейс ────────────────────────────────────────────────────────────
  // Все пользовательские строки идут через textContent, никогда через
  // innerHTML: заметка, в которой набран <img onerror=...>, не должна
  // исполнять ничего.
  function el(tag, css, text) {
    const e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (text != null) e.textContent = text;
    return e;
  }

  function note(msg) {
    let n = document.getElementById('draftsNote');
    if (!n) {
      n = el('div', 'position:fixed;left:50%;transform:translateX(-50%);bottom:'
        + '18px;z-index:99998;max-width:88vw;padding:10px 14px;border-radius:10px;'
        + 'background:#222;color:#eee;font-size:13px;box-shadow:0 6px 20px rgba(0,0,0,.4);'
        + 'display:none');
      n.id = 'draftsNote';
      document.body.appendChild(n);
    }
    n.textContent = msg;
    n.style.display = 'block';
    clearTimeout(n._t);
    n._t = setTimeout(() => { n.style.display = 'none'; }, 4000);
  }

  function overlay(title, bodyNode, actions) {
    let ov = document.getElementById('draftsOv');
    if (!ov) {
      ov = el('div', 'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.55);'
        + 'display:flex;align-items:center;justify-content:center;padding:14px');
      ov.id = 'draftsOv';
      ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
      document.body.appendChild(ov);
    }
    ov.innerHTML = '';
    const card = el('div', 'background:#1b1b1e;color:#e8e8ea;border-radius:14px;'
      + 'width:100%;max-width:560px;max-height:86vh;overflow:auto;padding:16px;'
      + 'font:14px/1.45 system-ui,sans-serif');
    card.appendChild(el('h3', 'margin:0 0 10px;font-size:16px', title));
    card.appendChild(bodyNode);
    const row = el('div', 'display:flex;gap:8px;flex-wrap:wrap;margin-top:12px');
    (actions || []).forEach(b => row.appendChild(b));
    card.appendChild(row);
    ov.appendChild(card);
    ov.style.display = 'flex';
    return ov;
  }

  function close() {
    const ov = document.getElementById('draftsOv');
    if (ov) ov.style.display = 'none';
  }

  function btn(label, fn, primary) {
    const b = el('button', 'padding:8px 14px;border-radius:9px;border:1px solid '
      + (primary ? '#5b8cff' : '#3a3a40') + ';background:'
      + (primary ? '#2b5bd6' : 'transparent')
      + ';color:#fff;font-size:13px;cursor:pointer', label);
    b.type = 'button';
    b.addEventListener('click', fn);
    return b;
  }

  function field(label, node) {
    const w = el('label', 'display:block;margin-bottom:10px;font-size:12px;opacity:.8', label);
    const wrap = el('div');
    node.style.cssText += ';width:100%;box-sizing:border-box;padding:8px;border-radius:8px;'
      + 'border:1px solid #3a3a40;background:#111;color:#e8e8ea;font-size:14px';
    wrap.appendChild(w);
    wrap.appendChild(node);
    return wrap;
  }

  function composer(draft) {
    const d = draft || { id: newId(), title: '', content: '', folder: 'inbox', tags: [] };
    const t = el('input'); t.type = 'text'; t.value = d.title || ''; t.placeholder = 'Заголовок';
    const c = el('textarea'); c.value = d.content || ''; c.placeholder = 'Текст заметки';
    c.rows = 10;
    const f = el('input'); f.type = 'text'; f.value = d.folder || 'inbox';
    f.placeholder = 'Папка (например inbox)';
    const body = el('div');
    body.appendChild(field('Заголовок', t));
    body.appendChild(field('Текст', c));
    body.appendChild(field('Папка', f));
    body.appendChild(el('div', 'font-size:12px;opacity:.65',
      'Заметка сохранится в память телефона и уйдёт на ПК сама, как только он '
      + 'снова будет в сети. Вложения офлайн не добавляются.'));
    const save = btn('Сохранить черновик', async () => {
      if (!t.value.trim() && !c.value.trim()) { note('Пусто — сохранять нечего'); return; }
      const n = Object.assign({}, d, {
        title: t.value.trim(), content: c.value, folder: (f.value.trim() || 'inbox'),
        updated: Date.now(), created: d.created || Date.now(), lastError: '',
      });
      try {
        const all = await list();
        if (!draft && all.length >= MAX_DRAFTS) {
          note('Слишком много черновиков (' + all.length + ') — сначала отправь часть');
          return;
        }
        await put(n);
        close();
        render();
        note('Черновик сохранён на устройстве');
        flush();   // если ПК вдруг доступен, уйдёт сразу
      } catch (e) { note('Не сохранено: ' + (e && e.message || e)); }
    }, true);
    overlay(draft ? 'Черновик' : 'Новая заметка (офлайн)', body,
      [save, btn('Отмена', close)]);
    setTimeout(() => t.focus(), 50);
  }

  function draftRow(d) {
    const line = el('div', 'border-top:1px solid #2c2c31;padding:8px 0');
    const head = el('div', 'font-size:13px', (d.title || 'Без названия')
      + ' · ' + new Date(d.updated || d.created || 0).toLocaleString()
      + (d.folder && d.folder !== 'inbox' ? ' · ' + d.folder : ''));
    line.appendChild(head);
    if (d.lastError) {
      line.appendChild(el('div', 'font-size:12px;color:#ff8a8a', 'Ошибка: ' + d.lastError));
    }
    const prev = (d.content || '').slice(0, 90).replace(/\s+/g, ' ');
    if (prev) line.appendChild(el('div', 'font-size:12px;opacity:.6', prev + '…'));
    const row = el('div', 'display:flex;gap:8px;margin-top:6px');
    row.appendChild(btn('Изменить', () => composer(d)));
    const rm = btn('Удалить', async () => {
      if (!confirm('Удалить этот черновик с устройства?')) return;
      await del(d.id); render();
    });
    row.appendChild(rm);
    line.appendChild(row);
    return line;
  }

  function draftsPanel() {
    const body = el('div');
    list().then(all => {
      body.innerHTML = '';
      if (!all.length) body.appendChild(el('div', 'font-size:13px;opacity:.7',
        'Черновиков нет. Всё, что ты напишешь офлайн, появится здесь.'));
      all.forEach(d => body.appendChild(draftRow(d)));
    });
    overlay('Черновики на устройстве', body, [
      btn('Отправить на ПК', () => flush(true), true),
      btn('Новая', () => composer(null)),
      btn('Стереть все', async () => {
        if (!confirm('Стереть ВСЕ черновики с этого устройства? Текст не вернётся.')) return;
        await clearAll(); close(); render();
      }),
      btn('Закрыть', close),
    ]);
  }

  function render() {
    let bar = document.getElementById('draftsBar');
    list().then(all => {
      if (!bar) {
        const anchor = document.getElementById(ANCHOR);
        bar = el('div', 'margin:8px 0;padding:9px 12px;border:1px solid #3a3a40;'
          + 'border-radius:10px;font-size:13px;display:flex;gap:8px;align-items:center;'
          + 'flex-wrap:wrap;background:rgba(91,140,255,.10)');
        bar.id = 'draftsBar';
        const host = anchor && anchor.parentElement ? anchor.parentElement : document.body;
        host.appendChild(bar);
      }
      bar.style.display = all.length ? 'flex' : 'none';
      if (!all.length) return;
      bar.innerHTML = '';
      bar.appendChild(el('span', null, 'Черновиков: ' + all.length
        + (all.some(d => d.lastError) ? ' (есть с ошибкой)' : '')));
      bar.appendChild(btn('Открыть', draftsPanel));
      bar.appendChild(btn('Отправить на ПК', () => flush(true), true));
    }).catch(() => {});
  }

  function init() {
    // Черновики имеют смысл там, где страница открывается без сети, то есть
    // только при service worker (https-вход). На http офлайн не откроется ни
    // список, ни черновик.
    if (!window.isSecureContext || !navigator.serviceWorker) return;
    if (location.pathname !== '/notes') return;

    const anchor = document.getElementById(ANCHOR) || document.body;
    const fab = btn('+ Заметка офлайн', () => composer(null));
    fab.style.cssText += ';margin-left:6px';
    fab.setAttribute('data-drafts-fab', '1');
    if (anchor.parentElement && !document.querySelector('[data-drafts-fab]')) {
      anchor.insertAdjacentElement('afterend', fab);
    } else if (anchor === document.body) {
      document.body.appendChild(fab);
    }
    // Наследуем видимость якоря: страница прячет #offlineDownloadBtn, когда
    // модуль заметок выключен в админке, — наша кнопка не должна пережить её.
    try {
      const src = document.getElementById(ANCHOR);
      if (src) new MutationObserver(() => {
        const hid = getComputedStyle(src).display === 'none';
        fab.style.display = hid ? 'none' : '';
      }).observe(src, { attributes: true, attributeFilter: ['style', 'class'] });
    } catch (e) {}

    render();
    window.addEventListener('online', () => flush());
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') { render(); flush(); }
    });
    // Первая попытка при загрузке: если мы открылись онлайн, очередь
    // разгружается сразу, без кнопок.
    flush();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else { init(); }
  window.InfinityOfflineDrafts = { list, flush, clearAll, composer };
})();
