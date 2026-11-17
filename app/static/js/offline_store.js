/* Локальные копии данных для чтения без центрального ПК.
   Включается только рубильниками в админке (децентрализация → офлайн),
   по умолчанию выключен: при выключенном рубильнике этот файл не делает
   ничего — ни запросов, ни записей. */
(function () {
  const DB_NAME = 'infinity-offline';
  const STORE = 'snapshots';
  const SW_PATH = '/sw_offline.js';
  const CACHE = 'infinity-offline-v2';   // тот же ключ, что в sw_offline.js
  const CFG_KEY = 'infinityOfflineCfg';  // последний полученный конфиг админки
  const LAST_KEY = 'infinityOfflineLastSync:';
  const SAVED_KEY = 'infinityOfflineSavedAt:';
  const MIN_SYNC_MS = 5 * 60 * 1000;
  const DEFAULT_INTERVAL_MIN = 15;

  let cfgPromise = null;

  // Конфиг админки запоминаем на устройстве. Без этого копия бесполезна
  // ровно в том случае, ради которого её делают: при выключенном ПК запрос
  // /api/offload/config не проходит, offlineCfg() даёт «выключено», и load()
  // отказывается читать локальную запись — страница вместо статей показывает
  // ошибку загрузки. Последний полученный конфиг переживает обрыв, а
  // «выключить совсем» по-прежнему работает: при первом же живом визите
  // рубильник окажется выключенным и purge() сотрёт копию и эту отметку.
  function config() {
    if (!cfgPromise) {
      cfgPromise = fetch('/api/offload/config', { cache: 'no-store' })
        .then(r => r.ok ? r.json() : null)
        .then(c => {
          if (c && typeof c === "object") {
            try { localStorage.setItem(CFG_KEY, JSON.stringify(c)); } catch (e) {}
            return c;
          }
          return readStoredCfg();
        })
        .catch(() => readStoredCfg());
    }
    return cfgPromise;
  }

  function readStoredCfg() {
    try {
      const raw = localStorage.getItem(CFG_KEY);
      const c = raw ? JSON.parse(raw) : null;
      return c && typeof c === "object" ? c : {};
    } catch (e) { return {}; }
  }

  function offlineCfg() {
    return config().then(c => {
      const d = (c.decentral || {}).offline || {};
      return {
        enabled: !!d.enabled,
        modules: d.modules || {},
        storageMb: Math.max(1, Math.min(2000, Number(d.storage_mb) || 20)),
        // Неизвестное значение — это всегда on_open: конфиг мог прийти из
        // памяти устройства, записанной старой версией страницы.
        fetch: (d.fetch === 'interval' || d.fetch === 'manual') ? d.fetch : 'on_open',
        intervalMin: Math.max(1, Math.min(1440, Number(d.interval_min) || DEFAULT_INTERVAL_MIN)),
        // Офлайн-черновики заметок — отдельный рубильник: это запись на
        // сервер, а не чтение копии, и включаться она должна независимо.
        drafts: !!d.drafts,
      };
    });
  }

  function openDb() {
    return new Promise((resolve, reject) => {
      if (!('indexedDB' in window)) return reject(new Error('нет indexedDB'));
      openBare().then(resolve, reject);
    });
  }

  function addStore(db) {
    if (!db.objectStoreNames.contains(STORE)) {
      db.createObjectStore(STORE, { keyPath: 'module' });
    }
  }

  // onupgradeneeded срабатывает только когда версия запроса ВЫШЕ уже
  // записанной. Если база существует с этой версией, но без хранилища (её
  // создал код, открывший её без onupgradeneeded), обработчик не вызывается
  // вовсе, каждая транзакция падает с NotFoundError, а страницы глотают её в
  // своих catch: копии перестают записываться навсегда и молча. Лечится
  // повышением версии — это единственный способ долить хранилище в живую
  // базу, не теряя записи остальных модулей.
  //
  // Версию при этом нельзя подставлять фиксированную: запрос версии 1 к
  // базе, уже поднятой до 2, отваливается с VersionError. Поэтому сначала
  // открытие без версии («какая есть»), и только если хранилища нет —
  // ровно +1 к той, что вернула база.
  function openBare() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME);
      req.onupgradeneeded = () => addStore(req.result);
      req.onsuccess = () => {
        const db = req.result;
        if (db.objectStoreNames.contains(STORE)) return resolve(db);
        const next = db.version + 1;
        db.close();
        openAt(next).then(resolve, reject);
      };
      req.onerror = () => reject(req.error || new Error('indexedDB недоступен'));
      req.onblocked = () => reject(new Error('indexedDB заблокирован'));
    });
  }

  function openAt(version) {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, version);
      req.onupgradeneeded = () => addStore(req.result);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error || new Error('indexedDB недоступен'));
      req.onblocked = () => reject(new Error('indexedDB заблокирован'));
    });
  }

  // Один запрос в одной транзакции. Внутри транзакции не должно быть await
  // чужих промисов: IndexedDB коммитит её, как только очередь запросов
  // пуста, и продолжение после await падает с TransactionInactiveError.
  function one(db, mode, fn) {
    return new Promise((resolve, reject) => {
      try {
        const t = db.transaction(STORE, mode);
        const r = fn(t.objectStore(STORE));
        t.onerror = t.onabort = () => reject(t.error || new Error('транзакция не удалась'));
        r.onsuccess = () => resolve(r.result);
        r.onerror = () => reject(r.error || new Error('запрос не удался'));
      } catch (e) { reject(e); }
    });
  }

  function runBatch(db, mode, fn) {
    return new Promise((resolve, reject) => {
      try {
        const t = db.transaction(STORE, mode);
        t.oncomplete = () => resolve(true);
        t.onerror = t.onabort = () => reject(t.error || new Error('транзакция не удалась'));
        fn(t.objectStore(STORE));
      } catch (e) { reject(e); }
    });
  }

  function sizeOf(payload) {
    try { return new TextEncoder().encode(JSON.stringify(payload)).length; }
    catch (e) { return 0; }
  }

  async function allRecords() {
    const db = await openDb();
    try { return await one(db, 'readonly', s => s.getAll()); }
    finally { db.close(); }
  }

  // LRU по savedAt: старые слепки выкидываем, пока не влезем в бюджет.
  async function prune() {
    const cfg = await offlineCfg();
    const budget = cfg.storageMb * 1024 * 1024;
    const all = (await allRecords()).slice().sort((a, b) => (a.savedAt || 0) - (b.savedAt || 0));
    let total = all.reduce((s, e) => s + (e.bytes || 0), 0);
    const dropped = [];
    for (const e of all) {
      if (total <= budget) break;
      total -= (e.bytes || 0);
      dropped.push(e.module);
    }
    if (dropped.length) {
      const db = await openDb();
      try { await runBatch(db, 'readwrite', s => dropped.forEach(m => s.delete(m))); }
      finally { db.close(); }
    }
    return { bytes: total, dropped };
  }

  async function save(module, payload) {
    const cfg = await offlineCfg();
    if (!cfg.enabled || !cfg.modules[module]) return { ok: false, reason: 'выключено' };
    const rec = { module, savedAt: Date.now(), bytes: sizeOf(payload), payload };
    const db = await openDb();
    try { await one(db, 'readwrite', s => s.put(rec)); }
    finally { db.close(); }
    const p = await prune();
    // Отметку «копия свежая» ставим только если запись после обрезки бюджета
    // осталась в базе: иначе кнопка и подпись под ней показывали бы время
    // слепка, которого на устройстве уже нет.
    if (!p.dropped.includes(module)) {
      try { localStorage.setItem(SAVED_KEY + module, String(rec.savedAt)); } catch (e) {}
    }
    return { ok: true, bytes: rec.bytes, dropped: p.dropped, savedAt: rec.savedAt };
  }

  async function load(module) {
    const cfg = await offlineCfg();
    if (!cfg.enabled || !cfg.modules[module]) return null;
    try {
      const db = await openDb();
      let rec;
      try { rec = await one(db, 'readonly', s => s.get(module)); }
      finally { db.close(); }
      return rec && rec.payload ? { savedAt: rec.savedAt, payload: rec.payload } : null;
    } catch (e) { return null; }
  }

  async function purge() {
    try {
      const db = await openDb();
      try { await runBatch(db, 'readwrite', s => s.clear()); }
      finally { db.close(); }
    } catch (e) { /* хранилища может и не быть */ }
    // Оболочки из Cache API удаляем и со страницы: сообщение воркеру может
    // не дойти, если он уже снят с регистрации, а выключенный рубильник не
    // должен оставлять на устройстве копии чужих данных.
    try { if ('caches' in window) await caches.delete(CACHE); } catch (e) {}
    // Запомненный конфиг тоже выбрасываем: иначе устройство и после «выключить
    // совсем» читало бы копии по старой отметке, когда сервер снова ляжет.
    try { localStorage.removeItem(CFG_KEY); } catch (e) {}
    // Отметки синхронизации — вместе с копиями: база очищена, а подпись
    // «копия от …» под кнопкой осталась бы и враньём проживала бы до первой
    // новой записи.
    try {
      ['news', 'notes', 'books', 'feed'].forEach(m => {
        localStorage.removeItem(LAST_KEY + m);
        localStorage.removeItem(SAVED_KEY + m);
      });
    } catch (e) {}
    if (!navigator.serviceWorker) return;
    try {
      const reg = await navigator.serviceWorker.getRegistration(SW_PATH);
      if (reg) {
        if (reg.active) reg.active.postMessage({ action: 'offline-purge' });
        await reg.unregister();
      }
    } catch (e) { /* тихо: рубильник выключен, офлайном больше не пользуемся */ }
  }

  // Локальный сервер на этой же машине: офлайн-копия не имеет смысла (сервер
  // либо работает, либо приложение закрыто), а service worker только добавляет
  // слой между страницей и сервером. Именно он в v1 ломал /notes и /news:
  // кэшировал gzip-ответ вместе с Content-Encoding. Поэтому на localhost
  // регистрацию не делаем и снимаем всё, что осталось от прошлых версий.
  function swPointlessHere() {
    try {
      const h = (location.hostname || '').toLowerCase();
      if (location.protocol === 'file:') return true;
      if (h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]') return true;
      return false;
    } catch (e) { return true; }
  }

  async function dropStaleSW() {
    if (!navigator.serviceWorker) return;
    try {
      const reg = await navigator.serviceWorker.getRegistration(SW_PATH);
      if (reg) await reg.unregister();
    } catch (e) { /* регистрации нет — и хорошо */ }
    try {
      if ('caches' in window) {
        const keys = await caches.keys();
        await Promise.all(keys.filter(k => k.indexOf('infinity-offline') === 0)
          .map(k => caches.delete(k)));
      }
    } catch (e) { /* хранилище недоступно — не критично */ }
  }

  async function register() {
    if (swPointlessHere()) {
      dropStaleSW();
      return false;
    }
    const cfg = await offlineCfg();
    if (!cfg.enabled || !navigator.serviceWorker) return false;
    try {
      await navigator.serviceWorker.register(SW_PATH, { scope: '/' });
      await primeShell();
      return true;
    } catch (e) { return false; }
  }

  // Воркер перехватывает запросы только с навигации, которая началась ПОСЛЕ
  // его активации, а страницу, на которой включили рубильник, он не видел:
  // без этой клади первое офлайн-открытие даёт пустой экран — в Cache API
  // ещё ничего нет, и воркер отвечает 503. Кладём оболочку и её /static
  // ресурсы отсюда, из уже загруженной страницы: список подключей известен
  // только ей.
  async function primeShell() {
    if (!('caches' in window)) return false;
    try {
      const cache = await caches.open(CACHE);
      const urls = new Set([location.pathname + location.search]);
      document.querySelectorAll('script[src], link[href]').forEach(el => {
        const u = el.getAttribute('src') || el.getAttribute('href') || '';
        if (u.indexOf('/static/') === 0) urls.add(u);
      });
      /* cache.add() кладёт ответ сети как есть — вместе с Content-Encoding:
         gzip. Повторная выдача такого ответа ломала страницу (см. шапку
         sw_offline.js). Поэтому качаем сами и чистим транспортные заголовки. */
      await Promise.all(Array.from(urls).map(async (u) => {
        try {
          const res = await fetch(u, { cache: 'reload' });
          if (!res || !res.ok) return;
          const headers = new Headers(res.headers);
          ['content-encoding', 'content-length', 'transfer-encoding', 'vary']
            .forEach(h => headers.delete(h));
          const clean = new Response(await res.blob(), {
            status: res.status, statusText: res.statusText, headers
          });
          await cache.put(u, clean);
        } catch (e) { /* один ресурс не мешает сохранить остальные */ }
      }));
      return true;
    } catch (e) { return false; }
  }

  // Копия в IndexedDB полезна только вместе с service worker: без него
  // страница при выключенном сервере вообще не откроется, и запись просто
  // занимает бюджет. На http-адресе (телефон в локальной сети) воркер
  // недоступен, поэтому там работает скачиваемый файл, а не эта копия.
  function shellAvailable() {
    if (swPointlessHere()) return false;
    return !!navigator.serviceWorker && !!window.isSecureContext;
  }

  // Ключ на модуль, а не один на все: страницы открываются одна за другой, и
  // с общим ключом копия успела бы записаться только у первой из них —
  // остальные три рубильника в админке молча не работали бы пять минут.
  //
  // bypass — одинокий пропуск для нажатой кнопки. Без него ручное обновление
  // не работало бы вовсе: сейвер страницы спрашивает разрешение здесь же, и
  // второй отказ по тому же модулю убивал бы смысл кнопки.
  const bypass = {};

  async function syncAllowed(module) {
    if (bypass[module]) {
      delete bypass[module];
      try { localStorage.setItem(LAST_KEY + module, String(Date.now())); } catch (e) {}
      return true;
    }
    const cfg = await offlineCfg();
    // manual — страница не пишет ничего сама: только по кнопке (refresh).
    if (cfg.fetch === 'manual') return false;
    const key = LAST_KEY + module;
    const last = Number(localStorage.getItem(key) || 0);
    const wait = cfg.fetch === 'interval' ? cfg.intervalMin * 60000 : MIN_SYNC_MS;
    if (Date.now() - last < wait) return false;
    localStorage.setItem(key, String(Date.now()));
    return true;
  }

  async function usage() {
    try {
      const all = await allRecords();
      return {
        bytes: all.reduce((s, e) => s + (e.bytes || 0), 0),
        modules: all.map(e => ({ module: e.module, savedAt: e.savedAt, bytes: e.bytes })),
      };
    } catch (e) { return { bytes: 0, modules: [] }; }
  }

  // ── Три политики обновления из админки ───────────────────────────────────
  // on_open — как было: страница сама снимает копию при загрузке;
  // interval — плюс таймер, который переснимает копию открытого модуля по
  //            расписанию и догоняет пропущенное, когда на страницу вернулись;
  // manual   — страница не пишет ничего, пока не нажали «Обновить копию».
  //
  // Фоновой работы при закрытой странице здесь нет и появиться не может:
  // периодические фоновые синхронизации Chromium разрешает только на десктопе,
  // а на телефоне service worker просыпается ровно тогда, когда его зовёт
  // навигация. Поэтому «фоном по расписанию» честно означает «пока модуль
  // открыт», и админка обязана это говорить, а не обещать синхронизацию из
  // кармана.
  const syncHooks = {};
  let driverOn = false;

  // Страница говорит, КАК переснять её копию: у каждого модуля свои данные
  // (у книг это ещё и прочитанное/избранное из локального хранилища), и
  // выдумывать их на стороне хранилища означало бы вторую реализацию
  // страницы, которая разъехалась бы с первой за неделю.
  function onSync(module, fn, anchorId) {
    syncHooks[module] = fn;
    offlineCfg().then(cfg => {
      if (!cfg.enabled) return;
      if (cfg.fetch === 'interval') startDriver();
      if (cfg.fetch === 'manual' && cfg.modules[module]) addButton(module, anchorId);
    });
  }

  async function runDue() {
    // Флаг visible, а не «документ загружен»: в фоновой вкладке браузер и так
    // режет таймеры, а на телефоне страница может лежать в фоне часами —
    // снимать там копию означает жрать батарею молча.
    if (document.visibilityState !== 'visible') return;
    const cfg = await offlineCfg();
    if (!cfg.enabled || cfg.fetch !== 'interval') return;
    for (const m of Object.keys(syncHooks)) {
      if (!cfg.modules[m]) continue;
      // Разрешение спрашивает сам сейвер страницы (он и вызывает
      // syncAllowed). Если сделать это здесь, троттл сжёг бы отметку до
      // вопроса и страница честно отказалась бы писать копию.
      try { await syncHooks[m](); } catch (e) { /* один модуль не роняет драйвер */ }
    }
  }

  function startDriver() {
    if (driverOn) return;
    driverOn = true;
    // Проверка раз в минуту вместо одного setInterval на весь интервал:
    // рубильник могли переключить, пока страница открыта, а таймер на 12 часов
    // пережил бы это ровно так же плохо, как и не успел бы догнать после
    // возвращения на страницу.
    setInterval(runDue, 60000);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') runDue();
    });
  }

  function stampOf(module) {
    return Number(localStorage.getItem(SAVED_KEY + module) || 0);
  }

  async function refresh(module) {
    const fn = syncHooks[module];
    if (!fn) return { ok: false, reason: 'эта страница не умеет переснимать свою копию' };
    // Обход троттла: нажатая кнопка — явное желание пользователя, а не
    // автосинк, которому положено отдыхать интервал. Пропуск одинокий: его
    // съедает ближайший syncAllowed, и автосинк снова начинает считаться.
    bypass[module] = true;
    const before = stampOf(module);
    try { await fn(); } catch (e) { return { ok: false, reason: String(e) }; }
    const after = stampOf(module);
    if (after > before) return { ok: true, savedAt: after };
    // Сейвер страницы молча выходит из себя при выключенном модуле и при
    // http-контексте — пользователю надо назвать причину, а не просто «нет».
    return { ok: false, savedAt: after,
             reason: 'копия не записана: модуль выключен в админке или страница открыта по http' };
  }

  function fmtWhen(ts) {
    return ts ? new Date(ts).toLocaleString() : 'копии ещё нет';
  }

  // Кнопка живёт рядом с уже существующей кнопкой скачивания копии: тот же
  // ряд, та же логика видимости, и лишнего DOM-узла на странице не появляется
  // на пустом месте.
  function addButton(module, anchorId) {
    const anchor = anchorId && document.getElementById(anchorId);
    if (!anchor || !anchor.parentElement) return;
    if (anchor.parentElement.querySelector('[data-offline-refresh]')) return;
    if (!shellAvailable()) return;   // на http копию читать некому, кнопка врала бы
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.setAttribute('data-offline-refresh', module);
    btn.style.marginLeft = '6px';
    btn.innerHTML = '<i class="fa-solid fa-rotate"></i> Обновить копию';
    const when = document.createElement('span');
    when.setAttribute('data-offline-stamp', module);
    when.style.cssText = 'margin-left:6px;font-size:12px;opacity:.7';
    // Кнопку вставляем в дерево ПЕРВОЙ: позиции 'afterend'/'beforebegin'
    // работают только у узла, у которого уже есть родитель, иначе вставка
    // молча не происходит (возвращается null). В обратном порядке подпись со
    // временем последней записи просто не появлялась бы.
    anchor.insertAdjacentElement('afterend', btn);
    btn.insertAdjacentElement('afterend', when);
    when.textContent = fmtWhen(stampOf(module));
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      const r = await refresh(module);
      btn.disabled = false;
      when.textContent = r.ok ? fmtWhen(r.savedAt) : r.reason;
    });
    // Видимость кнопки модуля принадлежит странице (она прячет её при
    // выключенном рубильнике), поэтому и наша наследует её, а не заводит
    // своё отдельное правило, которое разъехалось бы со страницей.
    const mirror = () => {
      const hidden = getComputedStyle(anchor).display === 'none';
      btn.style.display = hidden ? 'none' : '';
      when.style.display = hidden ? 'none' : '';
    };
    try {
      new MutationObserver(mirror).observe(anchor,
        { attributes: true, attributeFilter: ['style', 'class'] });
    } catch (e) { /* старый браузер без MutationObserver: кнопка просто видима */ }
    mirror();
  }

  // Черновиками заведует отдельный файл, который подгружается только при
  // включённом рубильнике. Шаблон страницы заметок при этом не трогается
  // вовсе: он заморожен в рантайме при старте, а этот файл отдаётся живьём на
  // каждый запрос — значит новая функциональность появляется без перезапуска
  // и не может сломать то, что уже работает.
  function loadDrafts() {
    if (location.pathname !== '/notes') return;
    if (document.querySelector('script[data-offline-drafts]')) return;
    offlineCfg().then(cfg => {
      if (!cfg.enabled || !cfg.drafts || !cfg.modules.notes) return;
      const s = document.createElement('script');
      s.src = '/static/js/offline_drafts.js';
      s.setAttribute('data-offline-drafts', '1');
      document.body.appendChild(s);
    }).catch(() => { /* нет конфига — нет и черновиков, страница не виновата */ });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadDrafts);
  } else { loadDrafts(); }

  window.InfinityOffline = {
    isEnabled: () => offlineCfg().then(c => c.enabled),
    moduleEnabled: (m) => offlineCfg().then(c => c.enabled && !!c.modules[m]),
    policy: () => offlineCfg().then(c => ({ fetch: c.fetch, intervalMin: c.intervalMin })),
    draftsEnabled: () => offlineCfg().then(c => c.enabled && !!c.drafts && !!c.modules.notes),
    savedAt: (m) => stampOf(m),
    save, load, purge, register, syncAllowed, usage, shellAvailable,
    onSync, refresh,
  };
})();
