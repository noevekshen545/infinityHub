/* Собственная система плагинов встроенного браузера.
 *
 * Отличие от Chrome-расширений (desktop/extensions.js):
 *   - свой манифест plugin.json вместо manifest.json;
 *   - скрипт выполняется в обычном мире страницы после готовности DOM
 *     (userscript-подход), а не как content script в изолированном мире;
 *   - не нужен MV2/MV3, background-страницы и service worker отсутствуют:
 *     у плагина есть только js (страница) и css (страница);
 *   - реестр data/browser_plugins/registry.json, папка плагина — рядом.
 *
 * Манифест plugin.json:
 *   {
 *     "id": "my-plugin",            // необязательно, иначе из имени
 *     "name": "Мой плагин",         // обязательно
 *     "version": "1.0.0",
 *     "description": "что делает",
 *     "matches": ["https://example.com", "example.org"],  // все http(s) — по умолчанию
 *     "exclude": [],
 *     "js": "main.js",              // строка или массив
 *     "css": "style.css"            // строка или массив
 *   }
 * Шаблоны адресов: "https://example.com/*", "example.org", "*" — всё;
 * без поля matches плагин применяется ко всем http(s)-страницам.
 *
 * Внедрение: webview-гость (любая вкладка браузера, включая инкогнито)
 * на событии dom-ready получает CSS через insertCSS и скрипты через
 * executeJavaScript. Скрипт оборачивается в IIFE и получает объект
 * window.InfinityPlugins[id] с id/name/version и storage (localStorage,
 * пространство имён на плагин).
 *
 * Выключение снимает внедрённый CSS сразу; эффекты скрипта живут до
 * перезагрузки страницы — об этом честно сообщает интерфейс.
 */
const { app, ipcMain, dialog, shell, webContents } = require('electron');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

let ROOT = null;
let PLUGIN_DIR = '';
let REG_PATH = '';
let log = () => {};
let registry = [];       // [{dir, enabled, id, addedAt, source}]
let ipcBound = false;

/* Ключи insertCSS по webContents: при перезагрузке/выключении их надо снять. */
const INJECTED = new Map();   // wc.id -> [{plugin, key}]

/* ---------- реестр ---------- */
function loadRegistry() {
  try {
    const data = JSON.parse(fs.readFileSync(REG_PATH, 'utf8'));
    registry = Array.isArray(data.plugins) ? data.plugins : [];
  } catch (e) { registry = []; }
}
function saveRegistry() {
  try {
    fs.mkdirSync(path.dirname(REG_PATH), { recursive: true });
    fs.writeFileSync(REG_PATH, JSON.stringify({ plugins: registry }, null, 2));
  } catch (e) { log('plugins: не удалось сохранить реестр: ' + e.message); }
}

function readManifest(dir) {
  for (const name of ['plugin.json', 'infinity-plugin.json']) {
    try {
      const p = path.join(dir, name);
      if (fs.existsSync(p)) return JSON.parse(fs.readFileSync(p, 'utf8'));
    } catch (e) {
      log('plugins: битый ' + name + ' в ' + dir + ': ' + e.message);
      return null;
    }
  }
  return null;
}

function slug(name) {
  return String(name || 'plugin')
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^[-_]+/, '')
    .replace(/[-_]+$/, '')
    .slice(0, 40) || 'plugin';
}

/* ---------- шаблоны адресов ----------
   Chrome-подобные: "https://example.com/*", "example.com", "*" — всё.
   Плагин без matches применяется ко всем http(s)-страницам. */
function toRegExp(pattern) {
  const s = String(pattern || '').trim();
  if (!s) return null;
  let p = s;
  if (p.indexOf('://') < 0) p = '*://' + p + '/*';
  const re = p.replace(/[.+^${}()|[\]\\]/g, '\\$&')
              .replace(/\*/g, '.*')
              .replace(/\?/g, '.');
  try { return new RegExp('^' + re + '$', 'i'); } catch (e) { return null; }
}

function asList(v) {
  if (!v) return [];
  return Array.isArray(v) ? v.filter(Boolean).map(String) : [String(v)];
}

function matchersOf(man) {
  let inc = asList(man.matches).map(toRegExp).filter(Boolean);
  if (!inc.length) inc = [toRegExp('*://*/*')];
  const exc = asList(man.exclude).map(toRegExp).filter(Boolean);
  return { inc, exc };
}

function matchUrl(man, url) {
  if (!/^https?:/i.test(url)) return false;
  const m = matchersOf(man);
  return m.inc.some((r) => r.test(url)) && !m.exc.some((r) => r.test(url));
}

/* ---------- чтение файлов плагина ---------- */
function readText(dir, name) {
  const base = path.resolve(dir);
  const p = path.resolve(dir, name);
  if (p !== base && !p.startsWith(base + path.sep)) return '';
  try { return fs.readFileSync(p, 'utf8'); } catch (e) { return ''; }
}

function pluginCode(entry, man) {
  const js = asList(man.js).map((f) => readText(entry.dir, f)).filter(Boolean);
  const css = asList(man.css).map((f) => readText(entry.dir, f)).filter(Boolean);
  return { js: js.join('\n;\n'), css: css.join('\n') };
}

function wrapJs(entry, man, code) {
  return '(function(){\'use strict\';\n' +
    'try {\n' +
    'var __inf = { id: ' + JSON.stringify(entry.id) +
    ', name: ' + JSON.stringify(String(man.name || entry.id)) +
    ', version: ' + JSON.stringify(String(man.version || '')) + ' };\n' +
    'var __all = (window.InfinityPlugins = window.InfinityPlugins || {});\n' +
    '__all[__inf.id] = {\n' +
    '  id: __inf.id, name: __inf.name, version: __inf.version,\n' +
    '  storage: {\n' +
    '    get: function(k, d){ try { var v = localStorage.getItem(\'__inf_plugin_\' + __inf.id + \'__\' + k);' +
        ' return v === null ? (d === undefined ? null : d) : JSON.parse(v); } catch(e){ return d; } },\n' +
    '    set: function(k, v){ try { localStorage.setItem(\'__inf_plugin_\' + __inf.id + \'__\' + k,' +
        ' JSON.stringify(v)); } catch(e){} },\n' +
    '    remove: function(k){ try { localStorage.removeItem(\'__inf_plugin_\' + __inf.id + \'__\' + k); } catch(e){} }\n' +
    '  }\n' +
    '};\n' +
    code + '\n' +
    '} catch (e) { try { console.error(\'[плагин ' + String(man.name || entry.id)
      .replace(/\\/g, '\\\\').replace(/'/g, "\\'") + ']\', e); } catch (_) {} }\n' +
    '})();';
}

/* ---------- внедрение ---------- */
async function injectInto(wc, url) {
  if (!wc || wc.isDestroyed()) return;
  const id = wc.id;
  const prev = INJECTED.get(id);
  // Документ новый — прежние ключи недействительны, чистим только запись.
  if (prev) prev.length = 0;
  else INJECTED.set(id, []);

  for (const entry of registry) {
    if (!entry.enabled) continue;
    const man = readManifest(entry.dir);
    if (!man || !matchUrl(man, url)) continue;
    const code = pluginCode(entry, man);
    try {
      if (code.css) {
        const key = await wc.insertCSS(code.css);
        INJECTED.get(id).push({ plugin: entry.id, key });
      }
    } catch (e) {
      entry.error = 'CSS: ' + e.message;
      log('plugins: CSS ' + entry.id + ' на ' + url + ': ' + e.message);
    }
    if (code.js) {
      try {
        await wc.executeJavaScript(wrapJs(entry, man, code.js), true);
        entry.error = '';
      } catch (e) {
        entry.error = 'JS: ' + e.message;
        log('plugins: JS ' + entry.id + ' на ' + url + ': ' + e.message);
      }
    }
  }
}

function attach(wc) {
  if (!wc || (wc.getType && wc.getType() !== 'webview')) return;
  wc.on('dom-ready', () => {
    const url = (wc.isDestroyed && wc.isDestroyed()) ? '' : wc.getURL();
    injectInto(wc, url).catch((e) => log('plugins: внедрение: ' + e.message));
  });
  wc.on('destroyed', () => INJECTED.delete(wc.id));
}

/* Снять CSS плагина со всех открытых вкладок (JS остаётся до перезагрузки). */
async function dropCss(pluginId) {
  for (const wc of webContents.getAllWebContents()) {
    if (wc.isDestroyed()) continue;
    const rec = INJECTED.get(wc.id);
    if (!rec || !rec.length) continue;
    const keep = [];
    for (const item of rec) {
      if (item.plugin !== pluginId) { keep.push(item); continue; }
      try { await wc.removeInsertedCSS(item.key); } catch (e) { /* документ уже ушёл */ }
    }
    INJECTED.set(wc.id, keep);
  }
}

/* ---------- список / добавление ---------- */
function listAll() {
  return registry.map((r) => {
    const man = readManifest(r.dir) || {};
    const js = asList(man.js), css = asList(man.css);
    const missing = [];
    js.concat(css).forEach((f) => {
      if (!fs.existsSync(path.join(r.dir, f))) missing.push(f);
    });
    return {
      key: r.id,
      id: r.id,
      name: man.name || r.id,
      version: String(man.version || ''),
      description: String(man.description || ''),
      dir: r.dir,
      enabled: !!r.enabled,
      matches: asList(man.matches),
      js: js,
      css: css,
      missing: missing,
      error: r.error || (missing.length ? 'нет файлов: ' + missing.join(', ') : ''),
    };
  });
}

function entryFromDir(dir, source) {
  const man = readManifest(dir);
  if (!man || !man.name) {
    return { ok: false, error: 'В папке нет plugin.json с полем name' };
  }
  const key = slug(man.id || man.name);
  if (registry.some((r) => r.id === key)) {
    return { ok: false, error: 'Плагин с id «' + key + '» уже добавлен' };
  }
  const entry = { dir, enabled: true, id: key, addedAt: Date.now(), source: source || 'folder' };
  registry.push(entry);
  saveRegistry();
  log('plugins: добавлен ' + key + ' из ' + dir);
  return { ok: true, key };
}

async function addFolder() {
  const res = await dialog.showOpenDialog({
    title: 'Папка плагина (plugin.json)',
    properties: ['openDirectory'],
  });
  if (res.canceled || !res.filePaths[0]) return { ok: false, canceled: true };
  return entryFromDir(res.filePaths[0], 'folder');
}

/* Минимальный рекурсивный поиск plugin.json в распакованном архиве:
   архивы часто содержат одну верхнюю папку. */
function findPluginRoot(dir, depth) {
  if (readManifest(dir)) return dir;
  if ((depth || 0) >= 2) return '';
  let items = [];
  try { items = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { return ''; }
  for (const it of items) {
    if (!it.isDirectory()) continue;
    if (it.name === '__MACOSX' || it.name.charAt(0) === '.') continue;
    const found = findPluginRoot(path.join(dir, it.name), (depth || 0) + 1);
    if (found) return found;
  }
  return '';
}

async function addArchive() {
  const res = await dialog.showOpenDialog({
    title: 'Архив плагина (.zip)',
    properties: ['openFile'],
    filters: [{ name: 'Архив', extensions: ['zip'] }, { name: 'Все файлы', extensions: ['*'] }],
  });
  if (res.canceled || !res.filePaths[0]) return { ok: false, canceled: true };
  const file = res.filePaths[0];
  let buf;
  try { buf = fs.readFileSync(file); } catch (e) { return { ok: false, error: 'Не удалось прочитать файл' }; }
  if (buf.toString('latin1', 0, 2) !== 'PK') {
    return { ok: false, error: 'Это не zip-архив' };
  }
  let unzipTo;
  try { unzipTo = require('./extensions').unzipTo; } catch (e) { unzipTo = null; }
  if (!unzipTo) return { ok: false, error: 'Распаковщик недоступен' };

  const tmp = path.join(PLUGIN_DIR, '.tmp-' + Date.now());
  fs.mkdirSync(tmp, { recursive: true });
  try { await unzipTo(tmp, buf); }
  catch (e) {
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) {}
    return { ok: false, error: 'Не удалось распаковать: ' + e.message };
  }
  const root = findPluginRoot(tmp);
  if (!root) {
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) {}
    return { ok: false, error: 'В архиве нет plugin.json' };
  }
  const man = readManifest(root) || {};
  const hash = crypto.createHash('sha1').update(path.resolve(file)).digest('hex').slice(0, 8);
  const finalDir = path.join(PLUGIN_DIR, slug(man.name || man.id || path.basename(file, '.zip')) + '-' + hash);
  try {
    fs.rmSync(finalDir, { recursive: true, force: true });
    if (path.resolve(root) === path.resolve(tmp)) fs.renameSync(tmp, finalDir);
    else { fs.cpSync(root, finalDir, { recursive: true }); fs.rmSync(tmp, { recursive: true, force: true }); }
  } catch (e) {
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) {}
    return { ok: false, error: 'Не удалось сохранить плагин: ' + e.message };
  }
  const out = entryFromDir(finalDir, 'zip');
  if (!out.ok) {
    try { fs.rmSync(finalDir, { recursive: true, force: true }); } catch (_) {}
  }
  return out;
}

/* ---------- переключение / удаление ---------- */
async function toggle(key) {
  const entry = registry.find((r) => r.id === key);
  if (!entry) return { ok: false, error: 'Плагин не найден' };
  entry.enabled = !entry.enabled;
  entry.error = '';
  saveRegistry();
  if (!entry.enabled) await dropCss(entry.id);
  return { ok: true, enabled: entry.enabled };
}

async function remove(key) {
  const idx = registry.findIndex((r) => r.id === key);
  if (idx < 0) return { ok: false, error: 'Плагин не найден' };
  const entry = registry[idx];
  await dropCss(entry.id);
  // Папки, которые мы распаковали из архива, удаляем; чужие — только
  // выписываем из реестра (пользователь мог держать там свои файлы).
  if (entry.source === 'zip' && entry.dir.startsWith(PLUGIN_DIR)) {
    try { fs.rmSync(entry.dir, { recursive: true, force: true }); } catch (e) {}
  }
  registry.splice(idx, 1);
  saveRegistry();
  return { ok: true };
}

/* Пример-заготовка: выключен, ничего не меняет, но показывает формат. */
function createExample() {
  const dir = path.join(PLUGIN_DIR, 'example');
  if (fs.existsSync(dir)) return { ok: false, error: 'Пример уже существует: ' + dir };
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'plugin.json'), JSON.stringify({
      id: 'example',
      name: 'Пример плагина',
      version: '1.0.0',
      description: 'Заготовка: раскомментируйте строки в style.css и main.js',
      matches: ['*://*/*'],
      exclude: [],
      css: 'style.css',
      js: 'main.js',
    }, null, 2));
    fs.writeFileSync(path.join(dir, 'style.css'),
      '/* CSS применяется на подходящих страницах после загрузки DOM.\n' +
      '   Пример: слегка увеличить межстрочный интервал у статей.\n\n' +
      'article p, .post-content p { line-height: 1.75; }\n' +
      '*/\n');
    fs.writeFileSync(path.join(dir, 'main.js'),
      '/* JS выполняется после DOM. window.InfinityPlugins[id].storage —\n' +
      '   хранилище плагина (JSON в localStorage, отдельное на сайт).\n\n' +
      'console.log("[плагин:пример] страница:", location.href);\n' +
      '*/\n');
  } catch (e) {
    return { ok: false, error: e.message };
  }
  return entryFromDir(dir, 'example');
}

function openDir() {
  try { fs.mkdirSync(PLUGIN_DIR, { recursive: true }); } catch (e) {}
  return shell.openPath(PLUGIN_DIR);
}

/* ---------- IPC ---------- */
function registerIpc() {
  if (ipcBound) return;
  ipcBound = true;
  ipcMain.handle('plugins-list', () => listAll());
  ipcMain.handle('plugins-add-folder', () => addFolder());
  ipcMain.handle('plugins-add-archive', () => addArchive());
  ipcMain.handle('plugins-toggle', (_e, key) => toggle(String(key || '')));
  ipcMain.handle('plugins-remove', (_e, key) => remove(String(key || '')));
  ipcMain.handle('plugins-create-example', () => createExample());
  ipcMain.handle('plugins-open-dir', () => openDir());
}

function init(opts) {
  ROOT = opts.root;
  log = opts.log || (() => {});
  PLUGIN_DIR = path.join(ROOT, 'data', 'browser_plugins');
  REG_PATH = path.join(PLUGIN_DIR, 'registry.json');
  try { fs.mkdirSync(PLUGIN_DIR, { recursive: true }); } catch (e) {}
  loadRegistry();
}

module.exports = { init, registerIpc, attach, listAll, toggle, remove, createExample };
