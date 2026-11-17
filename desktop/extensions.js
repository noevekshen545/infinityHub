/* Система Chrome-расширений для встроенного браузера.

   Возможности Electron (проверено по официальным докам):
     * грузятся ТОЛЬКО распакованные расширения (session.extensions.loadExtension);
     * сессия должна быть persistent — поэтому браузер живёт в отдельной
       сессии 'persist:browser' (вкладки-инкогнито остаются без расширений);
     * поддерживается подмножество chrome.* API: content_scripts, webRequest,
       storage.local, scripting, частично tabs/runtime; MV2 background-страницы
       работают, MV3 service worker — официально нет.

   Реестр (data/chrome_extensions/registry.json) хранит список добавленных
   расширений и их включённое состояние: при каждом запуске включённые
   загружаются заново — Electron сам ничего не запоминает.

   .crx с ПК поддерживаются «как в Chrome»: файл распаковывается (CRX2/CRX3
   заголовок + zip) в data/chrome_extensions/<имя>/ и дальше живёт как
   обычное распакованное расширение. */
const { session, ipcMain, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

const PARTITION = 'persist:browser';

let ROOT = null;
let EXT_DIR = '';
let REG_PATH = '';
let log = () => {};
let registry = [];          // [{dir, enabled, id, name, version, source, addedAt}]
let ipcBound = false;

/* ---------- реестр ---------- */
function loadRegistry() {
  try {
    const data = JSON.parse(fs.readFileSync(REG_PATH, 'utf8'));
    registry = Array.isArray(data.extensions) ? data.extensions : [];
  } catch (e) { registry = []; }
}
function saveRegistry() {
  try {
    fs.mkdirSync(path.dirname(REG_PATH), { recursive: true });
    fs.writeFileSync(REG_PATH, JSON.stringify({ extensions: registry }, null, 2));
  } catch (e) { log('extensions: не удалось сохранить реестр: ' + e.message); }
}

function readManifest(dir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
  } catch (e) { return null; }
}

/* ---------- отчёты об ошибках ----------
   Раньше ни одна ошибка загрузки не попадала в logs\desktop.log: в журнале
   не было ни одной строки 'extensions:', и реальная причина отказа Chromium
   («Invalid value for …», «Cannot load extension with file or directory
   name …») терялась. Пишем всё: действие, папку, версию манифеста, тип фона
   и полный текст исключения. */
function manifestInfo(man) {
  const b = man && man.background;
  const bg = !b ? 'нет'
    : b.service_worker ? 'service_worker (MV3)'
    : b.page ? 'page' : b.scripts ? 'scripts' : 'неизвестно';
  return 'mv=' + (man && man.manifest_version !== undefined ? man.manifest_version : 'нет') +
         ' фон=' + bg;
}

function fail(action, dir, man, e) {
  const msg = (e && e.message) ? e.message : String(e);
  const stack = (e && e.stack) ? String(e.stack).split('\n').slice(0, 4).join(' <- ') : '';
  log('extensions: ОШИБКА ' + action + ' | dir=' + dir + ' | ' + manifestInfo(man) +
      ' | ' + msg + (stack ? ' | ' + stack : ''));
}

/* Технические подробности для тоста «Подробнее»: что ответил Electron и что
   делать пользователю дальше. */
function detailOf(dir, man, e) {
  const out = [];
  out.push('Отказ Electron: ' + ((e && e.message) ? e.message : String(e)));
  out.push('Папка: ' + dir);
  if (man) out.push('Манифест: ' + manifestInfo(man));
  if (man && man.background && man.background.service_worker)
    out.push('MV3 service_worker: Electron его не запускает — даже если расширение загрузится, фон не заработает.');
  out.push('Подробности записаны в logs\\desktop.log.');
  return out.join('\n');
}

/* Проверки ДО loadExtension. Chromium отказывает молча и по-английски,
   поэтому самые частые причины формулируем здесь по-русски. */
function preflight(dir, man) {
  const mv = man.manifest_version;
  if (mv === undefined || mv === null)
    throw new Error('в manifest.json нет manifest_version');
  if (typeof mv !== 'number')
    throw new Error('manifest_version должен быть числом, а не ' + JSON.stringify(mv));
  const base = path.basename(dir);
  if (base.charAt(0) === '_')
    throw new Error('папка «' + base + '» начинается с «_» — Chromium запрещает такие имена, переименуйте папку');
}

/* ---------- сессия браузера ---------- */
function getSession() { return session.fromPartition(PARTITION); }

/* API расширений переехал: в Electron 36+ это ses.extensions.loadExtension(),
   а в 32–35 — ses.loadExtension() (старый вариант ещё есть, но помечен
   deprecated). Берём то, что есть в текущей версии, — код работает на обеих
   и не падает с «Cannot read properties of undefined». */
function extApi(ses) { return ses.extensions || ses; }

/* ---------- включение/выключение ---------- */
async function enableEntry(entry) {
  const ses = getSession();
  const ext = extApi(ses);
  if (entry.id && ext.getExtension(entry.id)) return entry.id;
  if (!fs.existsSync(path.join(entry.dir, 'manifest.json'))) {
    throw new Error('папка ' + entry.dir + ' больше не содержит manifest.json');
  }
  const loaded = await ext.loadExtension(entry.dir);
  entry.id = loaded.id;
  entry.name = loaded.name;
  entry.version = loaded.version;
  return loaded.id;
}
function disableEntry(entry) {
  try {
    if (!entry.id) return;
    const ext = extApi(getSession());
    const live = ext.getExtension(entry.id);
    if (live) ext.removeExtension(entry.id);
  } catch (e) { /* уже выгружено */ }
}

/* Загрузить всё включённое. Вызывается один раз при старте приложения,
   до открытия любого окна браузера. */
async function loadAllEnabled() {
  for (const entry of registry) {
    if (!entry.enabled) continue;
    try {
      await enableEntry(entry);
      log('extensions: загружено ' + (entry.name || entry.dir));
    } catch (e) {
      fail('загрузка при старте', entry.dir, readManifest(entry.dir), e);
    }
  }
  saveRegistry();
}

/* ---------- .crx/.zip: заголовок CRX2/CRX3 и распаковка zip ---------- */
function crxZipOffset(buf) {
  if (buf.length < 16 || buf.toString('latin1', 0, 4) !== 'Cr24') {
    throw new Error('это не .crx (нет подписи Cr24)');
  }
  const version = buf.readUInt32LE(4);
  // CRX2: 16 байт заголовка, затем открытый ключ и подпись.
  if (version === 2) {
    const pubLen = buf.readUInt32LE(8), sigLen = buf.readUInt32LE(12);
    return 16 + pubLen + sigLen;
  }
  // CRX3: 12 байт заголовка, затем подписанный заголовок длиной pubLen.
  // Открытый ключ и подпись лежат ВНУТРИ него, поэтому zip начинается сразу
  // за ним. Раньше здесь ещё вычиталась подпись — из-за этого указатель
  // уезжал внутрь zip на 4 байта, и ни один .crx не распаковывался.
  if (version === 3) return 12 + buf.readUInt32LE(8);
  throw new Error('неизвестная версия CRX: ' + version);
}

/* Где начинается zip: обычный .zip — сразу с 'PK', .crx — после заголовка
   Cr24. С поддержкой обоих форматов кнопка «Файл…» работает и с .zip,
   который сейчас качают вместо .crx. */
function zipStartOf(buf) {
  if (buf.length >= 2 && buf.toString('latin1', 0, 2) === 'PK') return 0;
  return crxZipOffset(buf);
}

function unzipTo(dir, buffer) {
  const yauzl = require('yauzl');
  return new Promise((resolve, reject) => {
    yauzl.fromBuffer(buffer, { lazyEntries: true }, (err, zipfile) => {
      if (err) return reject(err);
      let failed = null;
      const fail = (e) => { if (!failed) { failed = e; reject(e); } };
      // lazyEmit: следующая запись приходит только после readEntry(),
      // поэтому вызываем его строго после полной обработки текущей.
      zipfile.on('entry', (entry) => {
        // path.resolve — чтобы сравнение не ломалось от смешанных «/» и «\»,
        // и защита от "../" в именах внутри архива.
        const base = path.resolve(dir);
        const target = path.resolve(path.join(base, entry.fileName));
        if (target !== base && !target.startsWith(base + path.sep)) return zipfile.readEntry();
        if (entry.isDirectory || /\/$/.test(entry.fileName)) {
          try { fs.mkdirSync(target, { recursive: true }); } catch (e) {}
          return zipfile.readEntry();
        }
        try { fs.mkdirSync(path.dirname(target), { recursive: true }); } catch (e) {}
        zipfile.openReadStream(entry, (err2, stream) => {
          if (err2) return zipfile.readEntry();
          const ws = fs.createWriteStream(target);
          stream.on('error', fail);
          ws.on('error', fail);
          ws.on('finish', () => { if (!failed) zipfile.readEntry(); });
          stream.pipe(ws);
        });
      });
      zipfile.on('end', () => { if (!failed) resolve(); });
      zipfile.on('error', fail);
      zipfile.readEntry();
    });
  });
}

function safeName(name, fallback) {
  let s = String(name || fallback || 'extension')
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    // Chromium не грузит расширения, папка которых начинается с «_»,
    // — такое имя получается, например, из «_metadata».
    .replace(/^[-_]+/, '')
    .replace(/[-_]+$/, '')
    .slice(0, 40);
  return s || 'extension';
}

/* ---------- добавление ---------- */
async function addFolder() {
  const res = await dialog.showOpenDialog({
    title: 'Папка распакованного расширения',
    properties: ['openDirectory'],
  });
  if (res.canceled || !res.filePaths[0]) return { ok: false, canceled: true };
  const dir = res.filePaths[0];
  const man = readManifest(dir);
  if (!man || !man.name) {
    return { ok: false, error: 'В папке нет manifest.json — это не расширение Chrome' };
  }
  if (registry.some((r) => path.resolve(r.dir) === path.resolve(dir))) {
    return { ok: false, error: 'Эта папка уже добавлена' };
  }
  const entry = { dir, enabled: true, name: man.name, version: String(man.version || ''),
                  source: 'folder', addedAt: Date.now() };
  registry.push(entry);
  try {
    preflight(dir, man);
    await enableEntry(entry);
  } catch (e) {
    registry.pop();
    fail('добавление папки', dir, man, e);
    return { ok: false, error: 'Не удалось загрузить: ' + e.message, detail: detailOf(dir, man, e) };
  }
  saveRegistry();
  log('extensions: добавлена папка ' + dir + ' (' + manifestInfo(man) + ')');
  let warning = '';
  if (man.manifest_version === 3 && man.background && man.background.service_worker) {
    warning = 'MV3 service worker Electron не поддерживает — фоновая часть может не работать.';
  }
  return { ok: true, warning };
}

async function addCrx() {
  const res = await dialog.showOpenDialog({
    title: 'Файл расширения (.crx или .zip)',
    properties: ['openFile'],
    filters: [{ name: 'Расширение Chrome', extensions: ['crx', 'zip'] },
              { name: 'Все файлы', extensions: ['*'] }],
  });
  if (res.canceled || !res.filePaths[0]) return { ok: false, canceled: true };
  const file = res.filePaths[0];
  let buf;
  try { buf = fs.readFileSync(file); } catch (e) { return { ok: false, error: 'Не удалось прочитать файл' }; }
  let zipStart;
  try { zipStart = zipStartOf(buf); } catch (e) { return { ok: false, error: e.message }; }
  if (buf.toString('latin1', zipStart, zipStart + 2) !== 'PK') {
    return { ok: false, error: 'Внутри .crx не найден zip-архив' };
  }
  const tmp = path.join(EXT_DIR, '.tmp-' + Date.now());
  fs.mkdirSync(tmp, { recursive: true });
  try {
    await unzipTo(tmp, buf.subarray(zipStart));
  } catch (e) {
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) {}
    fail('распаковка', file, null, e);
    return { ok: false, error: 'Не удалось распаковать: ' + e.message,
             detail: 'Файл: ' + file + '\nОтказ: ' + e.message +
                     '\nПодробности записаны в logs\\desktop.log.' };
  }
  const man = readManifest(tmp);
  if (!man || !man.name) {
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) {}
    return { ok: false, error: 'В архиве нет manifest.json — это не расширение Chrome' };
  }
  const hash = crypto.createHash('sha1').update(path.resolve(file)).digest('hex').slice(0, 8);
  const finalDir = path.join(EXT_DIR, safeName(man.name, path.basename(file, /\.crx$|\.zip$/i)) + '-' + hash);
  try { fs.rmSync(finalDir, { recursive: true, force: true }); fs.renameSync(tmp, finalDir); }
  catch (e) {
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) {}
    return { ok: false, error: 'Не удалось сохранить распакованное расширение' };
  }
  const entry = { dir: finalDir, enabled: true, name: man.name, version: String(man.version || ''),
                  source: 'crx', addedAt: Date.now() };
  registry.push(entry);
  try {
    preflight(finalDir, man);
    await enableEntry(entry);
  } catch (e) {
    registry.pop();
    // Распакованную папку НЕ удаляем: если Electron её отверг, пользователь
    // сможет поправить manifest.json и добавить заново кнопкой «Папка…».
    fail('загрузка из ' + path.extname(file).toLowerCase(), finalDir, man, e);
    return { ok: false, error: 'Распаковано, но не загрузилось: ' + e.message,
             detail: detailOf(finalDir, man, e) +
                     '\nПапка с расширением сохранена: ' + finalDir };
  }
  saveRegistry();
  log('extensions: добавлен файл ' + file + ' → ' + finalDir + ' (' + manifestInfo(man) + ')');
  let warning = '';
  if (man.manifest_version === 3 && man.background && man.background.service_worker) {
    warning = 'MV3 service worker Electron не поддерживает — фоновая часть может не работать.';
  }
  return { ok: true, warning };
}

/* ---------- список / переключение / удаление ---------- */
function pickIconPath(dir, man) {
  const val = (man.action && man.action.default_icon) ||
              (man.browser_action && man.browser_action.default_icon) ||
              man.icons || null;
  if (!val) return '';
  let rel = '';
  if (typeof val === 'string') {
    rel = val;
  } else if (typeof val === 'object') {
    const sizes = Object.keys(val)
      .map((k) => parseInt(k, 10))
      .filter((n) => !isNaN(n))
      .sort((a, b) => b - a);
    if (sizes.length) rel = val[String(sizes[0])];
  }
  if (!rel) return '';
  const base = path.resolve(dir);
  const p = path.resolve(dir, rel);
  if (p !== base && !p.startsWith(base + path.sep)) return '';
  return p;
}

function iconDataUrl(dir, man) {
  try {
    const p = pickIconPath(dir, man);
    if (!p || !fs.existsSync(p)) return '';
    const ext = path.extname(p).toLowerCase();
    const mime = ext === '.png' ? 'image/png'
      : ext === '.svg' ? 'image/svg+xml'
      : (ext === '.jpg' || ext === '.jpeg') ? 'image/jpeg'
      : ext === '.gif' ? 'image/gif' : '';
    if (!mime) return '';
    return 'data:' + mime + ';base64,' + fs.readFileSync(p).toString('base64');
  } catch (e) {
    return '';
  }
}

function listAll() {
  const ses = getSession();
  const ext = extApi(ses);
  return registry.map((r) => {
    const live = r.id ? ext.getExtension(r.id) : null;
    const man = (live && live.manifest) || readManifest(r.dir) || {};
    const mv = man.manifest_version !== undefined ? man.manifest_version : null;
    const hasSw = !!(man.background && man.background.service_worker);
    return {
      key: r.id || r.dir,
      id: r.id || '',
      name: (live && live.name) || r.name || path.basename(r.dir),
      version: (live && live.version) || r.version || '',
      dir: r.dir,
      enabled: !!r.enabled,
      loaded: !!live,
      manifestVersion: mv,
      // Иконка и popup нужны тулбару браузера: без них расширение
      // выглядело «добавленным в никуда».
      icon: iconDataUrl(r.dir, man),
      popup: (man.action && man.action.default_popup) ||
             (man.browser_action && man.browser_action.default_popup) || '',
      // MV3 на service worker в Electron грузится, но фон не стартует —
      // показываем это в списке, чтобы «тихо не работало» не было загадкой.
      warning: (mv === 3 && hasSw)
        ? 'MV3: service worker не запускается — фоновая часть не работает' : '',
    };
  });
}

async function toggle(key) {
  const entry = registry.find((r) => r.id === key || r.dir === key);
  if (!entry) return { ok: false, error: 'Расширение не найдено' };
  if (entry.enabled) {
    disableEntry(entry);
    entry.enabled = false;
  } else {
    try { await enableEntry(entry); entry.enabled = true; }
    catch (e) {
      fail('включение', entry.dir, readManifest(entry.dir), e);
      return { ok: false, error: e.message, detail: detailOf(entry.dir, readManifest(entry.dir), e) };
    }
  }
  saveRegistry();
  return { ok: true, enabled: entry.enabled };
}

function remove(key) {
  const idx = registry.findIndex((r) => r.id === key || r.dir === key);
  if (idx < 0) return { ok: false, error: 'Расширение не найдено' };
  const entry = registry[idx];
  disableEntry(entry);
  // Папки, которые мы сами распаковали из .crx, при удалении чистим;
  // чужие папки пользователя — только выписываем из реестра.
  if (entry.source === 'crx' && entry.dir.startsWith(EXT_DIR)) {
    try { fs.rmSync(entry.dir, { recursive: true, force: true }); } catch (e) {}
  }
  registry.splice(idx, 1);
  saveRegistry();
  return { ok: true };
}

/* ---------- IPC для страницы /browser ---------- */
function registerIpc() {
  if (ipcBound) return;
  ipcBound = true;
  ipcMain.handle('ext-list', () => listAll());
  ipcMain.handle('ext-add-folder', () => addFolder());
  ipcMain.handle('ext-add-crx', () => addCrx());
  ipcMain.handle('ext-toggle', (_e, key) => toggle(String(key || '')));
  ipcMain.handle('ext-remove', (_e, key) => remove(String(key || '')));
}

/* ---------- инициализация ---------- */
function init(opts) {
  ROOT = opts.root;
  log = opts.log || (() => {});
  EXT_DIR = path.join(ROOT, 'data', 'chrome_extensions');
  REG_PATH = path.join(EXT_DIR, 'registry.json');
  try { fs.mkdirSync(EXT_DIR, { recursive: true }); } catch (e) {}
  loadRegistry();
}

module.exports = { init, registerIpc, getSession, PARTITION,
                   loadAllEnabled, listAll, addFolder, addCrx, toggle, remove,
                   /* распаковщик переиспользует система плагинов (plugins.js) */
                   unzipTo };
