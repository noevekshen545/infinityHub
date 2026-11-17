/* Обёртка Infinity Search: окно приложения, трей и управление окном.
 *
 * Веб-интерфейс не меняется — Electron грузит его с локального сервера,
 * поэтому все /api, SSE и ассеты работают как в обычном браузере.
 *
 * Окно без системной рамки; кнопки свернуть/развернуть/закрыть рисует
 * веб-страница (static/js/desktop_frame.js) в том же дизайне, что и в
 * изолированной сборке Infinity.
 *
 * Сервер (venv\Scripts\python.exe app\main.py) запускает сама обёртка
 * и гасит при выходе — отдельно запускать его не нужно.
 */
const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, shell,
        globalShortcut, screen, desktopCapturer, session, dialog,
        webContents } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn } = require('child_process');
const { pathToFileURL } = require('url');
const extensions = require('./extensions');
const plugins = require('./plugins');

/* Путь к preload гостевых страниц <webview>. Считаем его здесь (в main-процессе
   доступны path/url) и передаём окну браузера через additionalArguments: сам
   preload работает в песочнице, где __dirname/require('path') недоступны. */
const GUEST_PRELOAD_URL = pathToFileURL(path.join(__dirname, 'browser_guest_preload.js')).href;

const ROOT = path.resolve(__dirname, '..');
// Портативная сборка: сервер — единый exe со встроенными зависимостями.
// Код приложения (app\) остаётся внешним и правится без пересборки.
const PY = path.join(ROOT, 'InfinitySearch.exe');
const APP = null;
const LOG = path.join(ROOT, 'logs', 'desktop.log');

const PORT = Number(process.env.INFINITY_PORT) || 8281;
const HOST = '127.0.0.1';
const BIND_HOST = process.env.INFINITY_HOST || '127.0.0.1';
const URL_MAIN = `http://${HOST}:${PORT}/`;

/* Идентичность приложения. electron.exe в своих метаданных называется
   «Electron», и панель задач/системные диалоги берут это имя, пока мы не
   представимся сами. AUMID связывает окно с ярлыком «Пуск» с тем же id —
   без него Windows группирует окно как electron.exe. */
app.setName('Infinity Search');
if (process.platform === 'win32') {
  app.setAppUserModelId('com.infinity.search.desktop');
  /* Папку данных не переносим: app.setName меняет userData на
     %APPDATA%\Infinity Search, и вместе с ним «переехали» бы тема,
     настройки и сессия встроенного браузера. Оставляем прежнее место,
     если оно уже есть. */
  try {
    const legacyUserData = path.join(app.getPath('appData'), 'Electron');
    if (fs.existsSync(legacyUserData)) app.setPath('userData', legacyUserData);
  } catch (e) { log('userData: ' + e.message); }
}

/* На Windows панель задач и трей читают .ico (много размеров) заметно
   корректнее, чем PNG: из PNG система берёт один размер и масштабирует. */
const ICON = process.platform === 'win32'
  ? path.join(__dirname, 'icon.ico')
  : path.join(__dirname, 'icon.png');

let mainWindow = null;
let tray = null;
let server = null;
let reallyQuit = false;
// Окно палитры быстрого запроса к ИИ. Создаётся лениво и живёт, пока
// нужно: при Shift+Space из трея показываем его, при закрытии прячем.
let aiBarWindow = null;
/* Прозрачное окно 3D-аватара: живёт отдельно от палитры, чтобы модель
   оставалась на экране, пока мини-приложение закрыто. */
let avatarWindow = null;
let avatarSaveTimer = null;
// Агент сейчас отвечает — окно палитры не прячем по потере фокуса.
let aiBarBusy = false;
// Пользователь закрепил окно палитры кнопкой «закрепить»: оно живёт, пока
// приложение не вернут из трея, а не прячется при каждой потере фокуса.
let aiBarPinned = false;
// Встроенный браузер. Режимы: 'half' (пристыкован к правой половине главного
// окна, ребёнок mainWindow — двигается и меняет размер вместе с ним),
// 'full' (пристыкован на весь главный экран) и 'window' (самостоятельное
// окно поверх рабочего стола). Живёт, пока открыто: закрытие = уничтожение.
let browserWindow = null;
let browserMode = 'half';
/* Отдельные (плавающие) окна браузера. browserWindow — только доковое окно
   ('half'/'full'); плавающие живут отдельно и не должны влиять на док.
   Держать их в одной переменной нельзя: тогда закрытие одного окна задевает
   другое, а «×» в отдельном окне закрывает не то окно. */
const freeBrowserWindows = new Set();
/* Отдельные окна страниц самого приложения (/agent, memory-graph, карточки
   приложений). Раньше они создавались с parent: mainWindow — на Windows такое
   окно «ведомое»: стоило скрыть главное окно (кнопка «×» в доп. окне уходила в
   window-close и прятала в трей именно main), как доп. окно оставалось на
   экране без владельца и переставало отвечать на клики и клавиши. Теперь они
   самостоятельные, а список нужен, чтобы их видно и закрываемо по-честному:
   «×» закрывает приславшее окно, а при выходе приложение гасит их все. */
const extraWindows = new Set();
/* Док «half»: главное окно сужается на ширину браузера, а сам браузер
   стыкуется к его правому краю (окна стоят рядом, а не внахлёст). Так
   веб-страница приложения сама перетекает под оставшуюся ширину — правки
   вёрстки не нужны. preDockBounds — геометрия главного окна до входа в док;
   dockWidth — ширина браузера в доке (её меняет пользователь полосой-
   разделителем, значение переживает перезапуск). dockMaximized — «развернуть»
   в доковом режиме: вся рабочая область минус док. */
const DOCK_STATE = path.join(ROOT, 'data', 'browser_dock.json');
const DOCK_MIN_BROWSER = 360;   // уже браузера в доке быть не может
const DOCK_MIN_MAIN = 760;      // уже главной страницы — не должно
let dockWidth = 0;
let preDockBounds = null;
let dockMaximized = false;

function loadDockWidth() {
  try {
    const v = Number(JSON.parse(fs.readFileSync(DOCK_STATE, 'utf8')).width);
    if (v >= DOCK_MIN_BROWSER) return Math.round(v);
  } catch (e) { /* первый запуск — ширина по умолчанию */ }
  return 0;
}
let dockSaveTimer = null;
function saveDockWidth(w) {
  clearTimeout(dockSaveTimer);
  dockSaveTimer = setTimeout(() => {
    try {
      fs.mkdirSync(path.dirname(DOCK_STATE), { recursive: true });
      fs.writeFileSync(DOCK_STATE, JSON.stringify({ width: Math.round(w) }));
    } catch (e) { /* не критично */ }
  }, 400);
}
function clamp(v, lo, hi) { return Math.max(lo, Math.min(Math.round(v), hi)); }
// Активные загрузки встроенного браузера: id -> DownloadItem. Нужен, чтобы
// страница /browser могла ставить на паузу, возобновлять и отменять —
// в Electron это делается только из главного процесса.
const browserDownloads = new Map();
let dlSeq = 0;

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`;
  try { fs.appendFileSync(LOG, line); } catch (e) { /* лог не критичен */ }
  // В консоль пишем только по запросу. Иначе сообщение «сервер запущен: …»
  // выводилось в окно cmd, оставшееся от start-desktop.bat, и сидело там
  // всё время работы приложения. Всё важное пишется в logs\desktop.log.
  if (process.env.INFINITY_DEBUG === '1') console.log(msg);
}

/* Ошибки и предупреждения страниц — в тот же журнал. Без этого ошибка
 * внутри мини-окна или палитры не видна нигде: консоль рендерера никто не
 * читает, а страница падает молча. */
function attachPageLogs(win, tag) {
  if (!win || win.__logsAttached) return;
  win.__logsAttached = true;
  win.webContents.on('console-message', (_e, level, message, line, source) => {
    /* 0 = verbose, 1 = info, 2 = warning, 3 = error. Пишем только то,
       что может что-то сломать, — иначе журнал забивается болтовнёй. */
    if (level === 2 || level === 3) {
      const where = String(source || '').split('/').pop();
      log(`[${tag}] ${level === 3 ? 'ERROR' : 'WARN'}: ${message} (${where}:${line})`);
    }
  });
  win.webContents.on('did-fail-load', (_e, code, desc, url) => {
    log(`[${tag}] страница не загрузилась: ${code} ${desc} ${url || ''}`);
  });
}

/* ---------- GPU: флаги ----------
   EffectCraft/LightCraft построены на wgpu (WebGPU), и «The graphics device
   was lost» — это потеря GPU-устройства, а не ошибка сервера.
   В Electron WebGPU по умолчанию выключён (navigator.gpu отсутствует), и
   wgpu уходит в запасной путь WebGL2 — на этом пути у EffectCraft падал
   GPU-процесс (exit_code=2). С флагом enable-unsafe-webgpu WebGPU включается
   и EffectCraft работает напрямую (проверено пробой: device-lost не
   появляется). Дополнительные флаки можно выложить в data\gpu_flags.json:
   массив строк вида "enable-unsafe-webgpu" или "webgpu-dawn-backend=vulkan".
   Флаги применяются ДО старта GPU-процесса. */
function applyGpuFlags() {
  // WebGPU в Electron по умолчанию выключён — включаем явно (для
  // EffectCraft/LightCraft). Если в какой-то версии он станет включён по
  // умолчанию, флаг безвреден.
  app.commandLine.appendSwitch('enable-unsafe-webgpu');
  /* Диагностика окон через DevTools-протокол. По умолчанию выключено:
     порт открывается только при явно заданной переменной окружения. */
  if (process.env.INFINITY_DEBUG_PORT) {
    app.commandLine.appendSwitch('remote-debugging-port', String(process.env.INFINITY_DEBUG_PORT));
    log('DevTools-порт: ' + process.env.INFINITY_DEBUG_PORT);
  }
  try {
    const f = path.join(ROOT, 'data', 'gpu_flags.json');
    if (!fs.existsSync(f)) return;
    const flags = JSON.parse(fs.readFileSync(f, 'utf8'));
    if (!Array.isArray(flags)) return;
    for (const s of flags) {
      const str = String(s).replace(/^-+/, '');
      const i = str.indexOf('=');
      if (i < 0) app.commandLine.appendSwitch(str);
      else app.commandLine.appendSwitch(str.slice(0, i), str.slice(i + 1));
    }
    log('применены gpu flags: ' + flags.join(', '));
  } catch (e) { log('gpu_flags.json: ' + e.message); }
}
applyGpuFlags();

/* ---------- Сервер ---------- */
function serverAlive() {
  return new Promise((resolve) => {
    const req = http.get(`http://${HOST}:${PORT}/api/health`, { timeout: 1500 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

async function waitForServer(timeoutMs = 180000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await serverAlive()) return true;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

/* ---------- Перехват роли сервера (mesh.failover) ----------
   Если свой сервер не поднялся или умер, а в настройках включён перехват,
   окно читает данные у соседа. Выбор узла живёт здесь, а не на сервере:
   спросить сервер в этот момент нельзя по определению. Формат файлов тот же,
   что читает app\mesh_failover.py — анонсы и подтверждённые соседи.
   Запись у соседа запрещена дважды: оболочка отменяет изменяющие запросы
   до сети и помечает остальные заголовком, а сервер соседа (этой же сборки)
   отклоняет такие запросы сам. */
const failover = require('./failover');
const FAILOVER_HEADER = 'x-infinity-readonly';
let failoverState = null;
let failoverTries = 0;

function readFailoverCfg() { return failover.readCfg(ROOT); }

function readFailoverCandidates() { return failover.readCandidates(ROOT); }

function pickFailoverNode(cfg, items) { return failover.pick(cfg, items); }

function probePeerUrl(url, timeoutMs = 1200) { return failover.probe(url, timeoutMs); }

/* Один слушатель на событие в сессии: обработчики ставятся при первом
   перехвате и живут всё время, глядя на failoverState. */
let failoverGuardInstalled = false;
function installFailoverGuard() {
  if (failoverGuardInstalled) return;
  failoverGuardInstalled = true;
  const ses = session.defaultSession;
  const ours = (url) => !!(failoverState && String(url || '').startsWith(failoverState.url));
  ses.webRequest.onBeforeSendHeaders((details, cb) => {
    const headers = details.requestHeaders;
    if (ours(details.url)) headers[FAILOVER_HEADER] = '1';
    else delete headers[FAILOVER_HEADER];
    cb({ requestHeaders: headers });
  });
  ses.webRequest.onBeforeRequest((details, cb) => {
    const method = String(details.method || 'GET').toUpperCase();
    if (ours(details.url) && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      log(`[перехват] запись у соседа заблокирована: ${method} ${details.url}`);
      cb({ cancel: true });
      return;
    }
    cb({});
  });
}

const FAILOVER_BANNER = `(() => {
  try {
    let b = document.getElementById('__fo_banner');
    if (!b) {
      b = document.createElement('div');
      b.id = '__fo_banner';
      Object.assign(b.style, {
        position: 'fixed', left: '0', right: '0', bottom: '0', zIndex: '2147483647',
        background: '#7a4b00', color: '#ffe9c2', font: '13px system-ui, sans-serif',
        padding: '6px 12px', textAlign: 'center', boxShadow: '0 -2px 8px rgba(0,0,0,.4)'
      });
      (document.body || document.documentElement).appendChild(b);
    }
    b.textContent = window.__foText || b.textContent;
  } catch (e) { /* страница могла не отрисоваться */ }
})();`;

async function showFailoverBanner(win, st) {
  const text = 'Данные отдаёт ' + (st.node || st.url) + ' (' + st.url + ') · режим только чтения';
  try {
    await win.webContents.executeJavaScript(
      'window.__foText = ' + JSON.stringify(text) + ';' + FAILOVER_BANNER);
  } catch (e) { /* баннер — удобство, не условие работы */ }
}

/* Свой сервер мог умереть на ходу: пробуем поднять его заново и, когда
   ответит, возвращаем окно на свою страницу. */
function watchOwnServer(win) {
  if (watchOwnServer.timer) return;
  const alive = server && server.exitCode === null && !server.killed;
  if (!alive) {
    try { startServer(); } catch (e) { log('перехват: сервер не перезапустился — ' + e.message); }
  }
  watchOwnServer.timer = setInterval(async () => {
    if (reallyQuit || !win || win.isDestroyed()) {
      clearInterval(watchOwnServer.timer);
      watchOwnServer.timer = null;
      return;
    }
    if (!(await serverAlive())) return;
    clearInterval(watchOwnServer.timer);
    watchOwnServer.timer = null;
    failoverState = null;
    log('перехват: свой сервер снова отвечает — возвращаемся');
    try { await win.loadURL(URL_MAIN); } catch (e) { /* окно уже закрыто */ }
  }, 10000);
}

async function openFailover(win) {
  const cfg = readFailoverCfg();
  if (!cfg || !win || win.isDestroyed()) return false;
  const items = readFailoverCandidates();
  const cand = pickFailoverNode(cfg, items);
  if (!cand) {
    log(`перехват включён (${cfg.trust}), но подходящего узла нет — показываем ошибку`);
    return false;
  }
  if (!(await probePeerUrl(cand.url))) {
    log('перехват: ' + cand.url + ' не отвечает на /api/health');
    return false;
  }
  installFailoverGuard();
  failoverState = { url: cand.url, node: cand.node };
  log(`перехват: читаем у ${cand.url} (${cand.node || 'узел без имени'})`);
  try { await win.loadURL(cand.url + '/'); } catch (e) {
    log('перехват: страница соседа не загрузилась — ' + e.message);
    failoverState = null;
    return false;
  }
  showFailoverBanner(win, failoverState);
  if (!win.__foHook) {
    win.__foHook = true;
    win.webContents.on('did-finish-load', () => {
      if (failoverState) showFailoverBanner(win, failoverState);
    });
  }
  watchOwnServer(win);
  return true;
}

/* ---------- Запуск и остановка сервера ---------- */
/* Ротация журнала при старте.
   desktop.log пишут и Electron (appendFileSync), и stdout сервера. Без
   ротации файл за месяцы вырос до 116 МБ: его открытие/дописывание и чтение
   в редакторе — лишняя нагрузка на диск. Держим 3 бэкапа по 20 МБ. */
const LOG_MAX_BYTES = 20 * 1024 * 1024;
const LOG_BACKUPS = 3;
function rotateLog() {
  try {
    try { fs.mkdirSync(path.dirname(LOG), { recursive: true }); } catch (e) {}
    if (!fs.existsSync(LOG)) return;
    if (fs.statSync(LOG).size < LOG_MAX_BYTES) return;
    for (let i = LOG_BACKUPS; i >= 1; i--) {
      const src = i === 1 ? LOG : `${LOG}.${i - 1}`;
      const dst = `${LOG}.${i}`;
      if (!fs.existsSync(src)) continue;
      try { fs.rmSync(dst, { force: true }); } catch (e) {}
      try { fs.renameSync(src, dst); } catch (e) {}
    }
  } catch (e) { /* журнал не критичен для работы */ }
}

function startServer() {
  rotateLog();

  try { fs.mkdirSync(path.dirname(LOG), { recursive: true }); } catch (e) { /* есть */ }

  const env = { ...process.env };
  // Переносимые пути сборки — те же, что задаёт start.bat
  env.INFINITY_ROOT = ROOT;
  env.INFINITY_DATA_DIR = path.join(ROOT, 'data');
  env.INFINITY_ENGINE_DIR = path.join(ROOT, 'engine');
  env.INFINITY_MODELS_DIR = path.join(ROOT, 'models');
  env.INFINITY_CONFIG = path.join(ROOT, 'app', 'config.json');
  env.PYTHONPATH = path.join(ROOT, 'app');
  env.PYTHONIOENCODING = 'utf-8';
  env.PYTHONUNBUFFERED = '1';
  env.INFINITY_PORT = String(PORT);

  // Портативный Hermes: тот же runtime, что использует start.bat.
  // Если снаружи уже задан HERMES_INT_PYTHON (start.bat / отладка) — не перетираем.
  const hermesExe = path.join(ROOT, 'hermes', 'HermesAgent.exe');
  if (!env.HERMES_INT_PYTHON && fs.existsSync(hermesExe)) {
    env.HERMES_INT_PYTHON = hermesExe;
  }
  // Портативные браузеры Playwright: не зависим от %LOCALAPPDATA%\ms-playwright.
  const pwBrowsers = path.join(ROOT, 'ms-playwright');
  if (!env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync(pwBrowsers)) {
    env.PLAYWRIGHT_BROWSERS_PATH = pwBrowsers;
  }

  const out = fs.openSync(LOG, 'a');
  // python.exe — консольное приложение, поэтому оно всегда выделяет себе
  // консоль. Одного windowsHide:true мало: он лишь выставляет SW_HIDE,
  // окно всё равно создаётся и успевает мигнуть. CREATE_NO_WINDOW (0x08000000)
  // запрещает выделять консоль вовсе — окно не появляется вообще.
  const CREATE_NO_WINDOW = 0x08000000;
  const args = APP ? [APP, '--port', String(PORT), '--host', BIND_HOST]
                   : ['--port', String(PORT), '--host', BIND_HOST];
  server = spawn(PY, args, {
    cwd: ROOT,
    env,
    stdio: ['ignore', out, out],
    windowsHide: true,
    ...(process.platform === 'win32' ? { creationflags: CREATE_NO_WINDOW } : {}),
  });
  server.on('exit', (code) => {
    log(`сервер завершился, код ${code}`);
    /* Умер на ходу — пробуем перехват. Повторы ограничены: если свой сервер
       падает снова и снова, страница ошибки честнее бесконечной петли. */
    if (reallyQuit || failoverState || failoverTries > 2) return;
    failoverTries += 1;
    setTimeout(() => {
      if (!reallyQuit && !failoverState) openFailover(mainWindow).catch(() => {});
    }, 2500);
  });
  server.on('error', (e) => log(`не удалось запустить сервер: ${e.message}`));
  log(`сервер запущен: ${PY} --port ${PORT}`);
}

/* Убрать всё, что осталось от ПРОШЛОГО запуска.

   Зачем: сервер мог быть поднят из start.bat, оборваться вместе с окном
   консоли или пережить аварийное закрытие. Тогда новый запуск Electron
   подхватывал чужой процесс на 8281 — и тот держал в памяти код, какой был
   на диске минуту назад. Из-за этого правки в app\*.py были не видны, пока
   пользователь не перезапустит сервер вручную, а порты 8642/9120/8080
   оставались заняты.

   Убиваем дерево целиком у каждого найденного экземпляра: дочерние
   процессы (HermesAgent, локальный LLM, помощники Playwright) переживают
   смерть родителя. Вызывается ТОЛЬКО после получения single-instance lock,
   иначе второй экземпляр приложения убил бы сервер первого. */
function killOrphanProcesses() {
  const cp = require('child_process');
  const images = ['InfinitySearch.exe', 'HermesAgent.exe', 'llama-server.exe',
                  'llama.exe', 'vlc.exe'];
  let killed = 0;
  for (const image of images) {
    try {
      if (process.platform !== 'win32') {
        // На других системах exe-именами не поймать; там ориентируемся
        // на порт, который держит сервер.
        break;
      }
      const out = cp.execFileSync(
        'tasklist', ['/FI', `IMAGENAME eq ${image}`, '/NH', '/FO', 'CSV'],
        { stdio: ['ignore', 'pipe', 'ignore'], encoding: 'utf8' } || '');
      for (const line of String(out).split(/\r?\n/)) {
        const m = line.match(/^"([^"]+)","(\d+)"/);
        if (!m) continue;
        const pid = m[2];
        // Не трогаем собственный сервер, если он уже запущен этой обёрткой.
        if (server && server.pid && Number(pid) === server.pid) continue;
        killServerTree(Number(pid));
        killed++;
      }
    } catch (e) { /* tasklist не нашёл такого имени — это нормально */ }
  }
  if (killed) log(`убрано осиротевших процессов: ${killed}`);
  else log('осиротевших процессов не найдено');
  return killed;
}

function stopServer() {
  if (!server || server.killed) return;
  try { server.kill(); log('сервер остановлен'); } catch (e) { /* уже мёртв */ }
  server = null;
}

/* Закрытие всего дерева процессов сервера.

   Python-сервер поднимает дочерние процессы (Hermes gateway/web UI,
   silero_pool, локальный LLM-движок). Убийство только родителя оставляло
   их работать: порты 8642/9120/8080 оставались заняты, и следующий
   запуск падал с «порт уже занят». Поэтому гасим дерево целиком. */
function killServerTree(pid) {
  if (!pid) return;
  try {
    if (process.platform === 'win32') {
      require('child_process').execFileSync(
        'taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' });
    } else {
      try { process.kill(-pid, 'SIGKILL'); } catch (e) { process.kill(pid, 'SIGKILL'); }
    }
  } catch (e) { /* процесс уже завершён */ }
}

function shutdownEverything() {
  const pid = server && server.pid;
  if (pid) {
    killServerTree(pid);
    log('дерево процессов сервера остановлено (pid ' + pid + ')');
  } else {
    stopServer();
  }
  server = null;
}


/* beforeunload в Electron не показывает диалог, а просто отменяет уход:
   окно со страницей настроек не закрывалось, переход не выполнялся —
   приложение выглядело зависшим. Спрашиваем сами модальным окном. */
let _unloadDialogOpen = false;
function guardPreventedUnload(contents, win) {
  contents.on('will-prevent-unload', (event) => {
    if (_unloadDialogOpen) { event.preventDefault(); return; }
    _unloadDialogOpen = true;
    let choice = 0;
    try {
      choice = dialog.showMessageBoxSync(win && !win.isDestroyed() ? win : undefined, {
        type: 'question',
        buttons: ['Остаться', 'Уйти без сохранения'],
        defaultId: 0,
        cancelId: 0,
        title: 'Несохранённые изменения',
        message: 'Есть несохранённые изменения.',
        detail: 'Уйти без сохранения?',
        noLink: true,
      });
    } finally {
      _unloadDialogOpen = false;
    }
    if (choice === 1) event.preventDefault();
  });
}

/* ---------- Окно ---------- */
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 640,
    show: false,
    frame: false,              // рамку рисует страница сама
    backgroundColor: '#0d1117',
    title: 'Infinity Search',
    icon: ICON,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
      /* Скрытое окно не должно замедлять таймеры: и у мини-плеера, и у
         палитры по скрытости работает обмен состоянием (media.playback),
         а фоновая троттлинг-пауза ломает передачу трека. */
      backgroundThrottling: false,
      /* Главное окно — единственное, где страница рисует панель кнопок
         окна (desktop_frame.js): рамки нет, кнопки нужны. */
      additionalArguments: ['--infinity-chrome=app'],
    },
  });

  mainWindow.on('closed', () => { mainWindow = null; });
  attachPageLogs(mainWindow, 'главное');
  guardPreventedUnload(mainWindow.webContents, mainWindow);
  /* Страницы самого приложения — здесь же, наружу только внешние сайты.
     Раньше было одно shell.openExternal на все случаи. */
  mainWindow.webContents.setWindowOpenHandler((details) => handleWindowOpen(details.url));
  // Пристыкованный браузер (half/full) должен ехать и менять размер вместе с
  // главным окном: он ребёнок mainWindow, поэтому move/resize родителя нужно
  // транслировать в его границы. Слушаем и обычные события, и максимизацию.
  const followMain = () => applyBrowserGeometry();
  ['move', 'resize', 'maximize', 'unmaximize', 'restore'].forEach((ev) =>
    mainWindow.on(ev, followMain));
}

function showMain() {
  if (!mainWindow) return;
  // Возврат приложения из трея гасит окно палитры: оно нужно было только
  // пока приложение свёрнуто (в том числе закреплённое).
  hideAiBar();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function toggleWindow() {
  if (!mainWindow) return;
  if (mainWindow.isVisible() && !mainWindow.isMinimized()) mainWindow.hide();
  else showMain();
}

/* ---------- Встроенный браузер (webview, вкладки, загрузки) ----------
 * Кнопка-глобус рядом с кнопками окна открывает полноценный браузер.
 * Три режима показа (переключаются кнопками в самом браузере):
 *   half  — пристыкован к правой половине главного окна, ребёнок mainWindow:
 *           двигается и меняет размер вместе с ним, не уходит за границы;
 *   full  — пристыкован на всю площадь главного окна;
 *   window — самостоятельное окно поверх рабочего стола (можно несколько).
 * Содержимое грузит со своего роута /browser; страницы сайтов рендерит
 * <webview> с webviewTag. Окно браузера имеет webviewTag=true, главное — нет.
 */
function browserBounds(mode) {
  if (!mainWindow || mainWindow.isDestroyed()) return null;
  const b = mainWindow.getBounds();
  if (mode === 'full') {
    return { x: b.x, y: b.y, width: b.width, height: b.height };
  }
  if (mode === 'half') {
    /* Главный окно в доке уже сужено (enterDock), поэтому браузер встаёт
       вплотную к его правому краю, а не поверх него: суммарная ширина
       «приложение + браузер» равна ширине окна до входа в док. */
    return { x: b.x + b.width, y: b.y, width: dockWidth, height: b.height };
  }
  return null;   // режим window — размер/позицию задаёт createBrowserWindow
}

/* Вход в док «half»: запоминаем геометрию главного окна, снижаем его
   minWidth (иначе сужение упрётся в 1024) и сужаем окно на dockWidth.
   Повторный вызов в уже доковом режиме — ничего не запоминаем заново. */
function enterDock() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  // Развернутое окно нельзя «подвинуть» — сначала вернуть обычный размер.
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  if (preDockBounds) {
    // Уже в доке: пересчитать только допустимость текущей ширины дока.
    const b = mainWindow.getBounds();
    dockWidth = clamp(dockWidth, DOCK_MIN_BROWSER, b.width + dockWidth - DOCK_MIN_MAIN);
    return;
  }
  preDockBounds = mainWindow.getBounds();
  dockMaximized = false;
  mainWindow.setMinimumSize(DOCK_MIN_MAIN, 640);
  if (!dockWidth) dockWidth = loadDockWidth();
  if (!dockWidth) dockWidth = Math.round(preDockBounds.width / 2);
  dockWidth = clamp(dockWidth, DOCK_MIN_BROWSER, preDockBounds.width - DOCK_MIN_MAIN);
  const b = mainWindow.getBounds();
  mainWindow.setBounds({ x: b.x, y: b.y, width: b.width - dockWidth, height: b.height });
}

/* Выход из дока: вернуть главному окну полную ширину и обычный minWidth. */
function exitDock() {
  if (!preDockBounds || !mainWindow || mainWindow.isDestroyed()) {
    preDockBounds = null; dockMaximized = false;
    return;
  }
  try { mainWindow.setMinimumSize(1024, 640); } catch (e) { /* окно могло закрываться */ }
  if (dockMaximized) {
    // Был «максимизированный док» — возвращаем точную геометрию до дока.
    mainWindow.setBounds(preDockBounds);
  } else {
    const b = mainWindow.getBounds();
    mainWindow.setBounds({ x: b.x, y: b.y, width: b.width + dockWidth, height: b.height });
  }
  preDockBounds = null;
  dockMaximized = false;
}

/* Пересчитать геометрию пристыкованного основного окна браузера. Режим
   'window' здесь не трогаем — у него своя геометрия и нет родителя. */
function applyBrowserGeometry() {
  if (!browserWindow || browserWindow.isDestroyed()) return;
  if (browserWindow.__mode === 'window') return;
  const b = browserBounds(browserWindow.__mode);
  if (b) browserWindow.setBounds(b);
}

function createBrowserWindow(mode) {
  const m = (mode === 'full' || mode === 'half') ? mode : 'window';
  const isDocked = m !== 'window';
  const common = {
    frame: false,
    backgroundColor: '#0f1419',
    title: 'Infinity Браузер',
    icon: ICON,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: true,          // страницы сайтов рендерит <webview>
      spellcheck: false,
      additionalArguments: ['--infinity-guest-preload=' + GUEST_PRELOAD_URL],
      /* Отдельная persistent-сессия: в неё загружаются Chrome-расширения
         (Electron грузит расширения только в persistent-сессии), и они не
         лезут в страницы самого приложения (это defaultSession). Вкладки
         инкогнито получают свою ephemeral-сессию без расширений. */
      partition: extensions.PARTITION,
    },
  };
  let win;
  if (isDocked) {
    const b = browserBounds(m);
    win = new BrowserWindow({
      ...common,
      parent: mainWindow,          // ребёнок главного окна: живёт с ним
      x: b.x, y: b.y, width: b.width, height: b.height,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      hasShadow: false,
    });
  } else {
    const cursor = screen.getCursorScreenPoint();
    const area = (screen.getDisplayNearestPoint(cursor) || screen.getPrimaryDisplay()).workArea;
    const width = Math.min(1200, Math.round(area.width * 0.8));
    const height = Math.min(860, Math.round(area.height * 0.85));
    win = new BrowserWindow({
      ...common,
      x: Math.round(area.x + (area.width - width) / 2),
      y: Math.round(area.y + (area.height - height) / 2),
      width, height,
      minWidth: 560, minHeight: 420,
      resizable: true,
      minimizable: true,
      maximizable: true,
      fullscreenable: true,
      skipTaskbar: false,
    });
  }

  win.__mode = m;
  win.__isBrowser = true;
  win.__ready = false;
  if (!isDocked) freeBrowserWindows.add(win);
  /* Сообщаем странице её режим: полоса-разделитель дока нужна только в 'half',
     и страница должна знать это сразу после загрузки. */
  win.webContents.on('did-finish-load', () => {
    win.__ready = true;
    try { win.webContents.send('browser-mode', win.__mode); } catch (e) { /* окно закрывается */ }
  });
  /* Страница браузера сама по себе — тоже окно приложения: её ссылки на
     внутренние адреса должны открываться здесь, а не в системном браузере. */
  win.webContents.setWindowOpenHandler((details) => handleWindowOpen(details.url));
  // Ссылки target=_blank и window.open внутри webview ловит setWindowOpenHandler
  // (см. web-contents-created). Здесь — только загрузка самой страницы браузера.
  win.loadURL(`${URL_MAIN}browser`);

  win.on('closed', () => {
    freeBrowserWindows.delete(win);
    const wasDockOwner = browserWindow === win;
    if (wasDockOwner) browserWindow = null;
    /* Главному окно возвращаем полную ширину, только если закрылось ИМЕННО
       доковое окно и других доковых окон не осталось. Раньше проверка шла по
       режиму закрытого окна: закрой браузер в режиме 'full' — главное окно
       оставалось сужённым в доке навсегда, и обратно его было не раскрыть.
       Плавающие окна на док не влияют вообще. */
    if (wasDockOwner && !hasDockedBrowser()) exitDock();
    log(`браузер: окно закрыто (режим ${m}${wasDockOwner ? ', док' : ', отдельное'}), ` +
        `отдельных осталось ${freeBrowserWindows.size}`);
  });
  // Перетаскивание/ресайз главного окна тащит и пристыкованный браузер.
  if (isDocked) {
    win.on('move', applyBrowserGeometry);
    win.on('resize', applyBrowserGeometry);
  }
  return win;
}

/* Есть ли ещё доковое окно браузера ('half'/'full')? Плавающие окна в счёт
   не идут: они к доку отношения не имеют. */
function hasDockedBrowser() {
  return BrowserWindow.getAllWindows().some((w) =>
    w.__isBrowser && w.__mode !== 'window' && !w.isDestroyed());
}

/* Открыть/показать основной (пристыкованный) браузер.
   mode: 'half' | 'full' | 'window'. Смена docked↔window требует пересоздать
   окно; повторный вызов с тем же режимом просто поднимает существующее.
   from — окно, из которого пришла команда (кнопки в самой странице браузера). */
function showBrowser(mode, from) {
  const m = (mode === 'full' || mode === 'half') ? mode : 'half';
  if (!mainWindow || mainWindow.isDestroyed()) return;
  /* «В правую половину» / «на весь экран», нажатые в ОТДЕЛЬНОМ окне, — это
     «перевести браузер в док», а не «открыть рядом ещё одно окно».
     Поэтому плавающее окно, из которого пришла команда, закрываем: иначе
     пользователь жмёт «перенести», а его окно остаётся на месте, и рядом
     появляется другое — выглядит как «сломались окна». */
  if (from && from.__isBrowser && from.__mode === 'window' && m !== 'window') {
    log('браузер: перенос отдельного окна в док (' + m + ')');
    from.close();
  }
  /* 'half' сужает главное окно (браузер встаёт рядом), остальные режимы
     возвращают ему полную ширину. */
  if (m === 'half') enterDock();
  else exitDock();
  const needRecreate =
    !browserWindow || browserWindow.isDestroyed() ||
    browserWindow.__mode !== m;
  if (needRecreate) {
    /* half ↔ full — оба доковые режима. Пересоздавать окно незачем: это
       перезагружало страницу и теряло все вкладки. Меняем режим и геометрию
       у существующего окна. */
    const bothDocked = browserWindow && !browserWindow.isDestroyed() &&
      browserWindow.__mode !== 'window' && m !== 'window';
    if (bothDocked) {
      browserMode = m;
      browserWindow.__mode = m;
      try { browserWindow.webContents.send('browser-mode', m); } catch (e) { /* закрывается */ }
    } else {
      if (browserWindow && !browserWindow.isDestroyed()) browserWindow.close();
      browserMode = m;
      browserWindow = createBrowserWindow(m);
    }
  }
  applyBrowserGeometry();
  browserWindow.show();
  browserWindow.focus();
}

/* Закрыть доковое окно браузера. Запасной путь для команд без окна-источника:
   плавающие окна он не трогает — иначе «×» в одном из них гасил бы другое. */
function closeBrowser() {
  if (browserWindow && !browserWindow.isDestroyed()) browserWindow.close();
  browserWindow = null;
}

/* ---------- Что открывать внутри приложения, а что — в системе ----------
   Раньше любой window.open / target=_blank шёл в shell.openExternal, то есть
   в системный браузер — и страницы самого Infinity (задачи агента,
   /api/agent/agent-designer, /api/agent/memory-graph, карточки приложений)
   открывались в Chrome вместо приложения. Пользователь это и наблюдал:
   «в Electron задачи не открываются, в браузере открываются».

   Правила:
     * адрес нашего же сервера — открыть окном внутри приложения;
     * http/https/mailto наружи — в системный браузер, как и было;
     * всё остальное (file:, javascript:, data:, blob:) — не открывать вовсе:
       раньше file:// тоже уходил в shell.openExternal. */
function isInternalUrl(raw) {
  let u;
  try { u = new URL(raw); } catch (e) { return false; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
  const ourHosts = new Set([`${HOST}:${PORT}`, 'localhost:' + PORT]);
  return ourHosts.has(u.host);
}

function isExternalScheme(raw) {
  return /^(https?:|mailto:|tel:)/i.test(String(raw || ''));
}

/* Окно для страницы самого приложения. Самостоятельное (без parent): оно
   живёт независимо от главного окна — скрытие или показ main не делает его
   «ведомым» и не вешает. Тот же preload, что и остальным окнам:
   desktop_frame.js рисует по window.electronAPI кнопки окна, а главный процесс
   по e.sender понимает, какое именно окно команду прислало. */
function openInternalWindow(rawUrl, opts) {
  /* opts.nativeFrame — окно с системной рамкой. Нужно для приложений,
     запущенных по ярлыку с рабочего стола: это чужие страницы
     (/app-files/<id>/), они не рисуют наши кнопки окна, и без рамки такое
     окно нечем закрыть.
     Раньше здесь стояло `frame: !o.nativeFrame` — ровно наоборот: окно с
     nativeFrame:true открывалось без рамки (её нечем закрыть), а окна по
     ссылкам получали системную рамку поверх собственных кнопок страницы. */
  const o = opts || {};
  const win = new BrowserWindow({
    width: 1100,
    height: 780,
    minWidth: 560,
    minHeight: 420,
    show: false,
    frame: !!o.nativeFrame,
    backgroundColor: '#0d1117',
    title: o.title || 'Infinity Search',
    icon: ICON,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
      backgroundThrottling: false,
      /* Окно с системной рамкой уже имеет кнопки «свернуть/развернуть/
         закрыть»: странице свою панель рисовать нельзя (она дублировала бы
         их и сдвигала содержимое вниз на высоту полосы перетаскивания). */
      additionalArguments: [`--infinity-chrome=${o.nativeFrame ? 'native' : 'app'}`],
    },
  });
  extraWindows.add(win);
  win.on('closed', () => extraWindows.delete(win));
  guardPreventedUnload(win.webContents, win);
  win.webContents.setWindowOpenHandler((details) => handleWindowOpen(details.url));
  win.once('ready-to-show', () => win.show());
  /* Страница может отрисоваться раньше, чем применится общая тема: файловое
     окно уже показывает часть тёмных панелей, а фон и списки остаются
     светлыми. После загрузки повторно применяем тему из localStorage —
     это гарантирует, что окно откроется в выбранной теме. */
  win.webContents.on('did-finish-load', () => {
    try {
      win.webContents.executeJavaScript(
        `(function(){
           try {
             var t = localStorage.getItem('infinity_theme');
             if (t && window.InfinityTheme && InfinityTheme.set) InfinityTheme.set(t);
           } catch (e) {}
         })();`
      );
    } catch (e) { /* страница могла закрыться */ }
  });
  win.loadURL(rawUrl);
  return win;
}

/* Единая точка решения для всех окон приложения. */
function handleWindowOpen(url) {
  if (isInternalUrl(url)) {
    /* Системная рамка: страница, открытая по ссылке, не обязана рисовать
       наши кнопки окна (некоторые маршруты отдают шаблоны без
       desktop_frame.js). Без рамки такое окно нечем закрыть. */
    openInternalWindow(url, { nativeFrame: true });
    return { action: 'deny' };      // окно создали сами — дублировать не нужно
  }
  if (isExternalScheme(url)) {
    shell.openExternal(url);
    return { action: 'deny' };
  }
  log(`ссылка не открыта: непривычная схема ${String(url).slice(0, 80)}`);
  return { action: 'deny' };
}

/* ---------- Команды сервера: окна, рождённые вне интерфейса ----------
 * Глобальная клавиша перехватывается сервером (hotkey_hub) и не проходит через
 * страницу: главное окно может быть спрятано в трей, и спрашивать разрешение на
 * окно у него не у кого. Поэтому сервер держит очередь команд, а оболочка их
 * забирает долгим опросом — окно появляется за доли секунды после нажатия.
 *
 * Опрос один на все команды: нового соединения каждые пару секунд нет, сервер
 * просто держит ответ, пока очередь пуста. */
const shellWindows = new Map();

function shellRequest(method, urlPath, payload, timeoutMs) {
  return new Promise((resolve, reject) => {
    const body = payload ? Buffer.from(JSON.stringify(payload)) : null;
    const headers = {};
    if (body) {
      headers['content-type'] = 'application/json';
      headers['content-length'] = body.length;
    }
    const req = http.request({ host: HOST, port: PORT, path: urlPath, method, headers,
                               timeout: timeoutMs }, (res) => {
      let text = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { text += chunk; });
      res.on('end', () => {
        try { resolve(JSON.parse(text || '{}')); }
        catch (e) { resolve({ ok: false, raw: text.slice(0, 200) }); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('таймаут')));
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

function sleepShell(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/* Одно окно на снимок-очередь: частые нажатия клавиши не должны плодить десяток
   одинаковых окон — новое содержимое приходит в то же окно. */
function openShotWindow(fullUrl, payload) {
  const tag = String(payload.tag || 'window');
  const existing = shellWindows.get(tag);
  if (existing && !existing.isDestroyed()) {
    existing.loadURL(fullUrl);
    if (!existing.isVisible()) existing.show();
    existing.focus();
    return existing;
  }
  const win = openInternalWindow(fullUrl, { title: payload.title || 'Infinity Search',
                                            nativeFrame: false });
  attachPageLogs(win, payload.title || 'окно');
  shellWindows.set(tag, win);
  win.on('closed', () => { if (shellWindows.get(tag) === win) shellWindows.delete(tag); });
  return win;
}

function applyShellCommand(command) {
  const payload = command.payload || {};
  if (command.action !== 'open-window') {
    log(`команда сервера не понята: ${command.action}`);
    return;
  }
  const url = String(payload.url || '');
  /* Сервер присылает путь внутри приложения: чужой адрес из этой очереди
     открывать не будем — окно должно принадлежать самому Infinity. */
  if (!url.startsWith('/') || url.startsWith('//')) {
    log(`команда с непривычным адресом: ${url.slice(0, 80)}`);
    return;
  }
  try {
    openShotWindow(URL_MAIN + url.slice(1), payload);
  } catch (e) {
    log(`окно по команде не открылось: ${e.message}`);
  }
}

/* Канал команд: сначала WebSocket (не занимает слот HTTP/1.1), при любой
   ошибке — прежний долгий опрос. Что сработало, видно в desktop.log строками
   «канал команд: websocket» / «long-poll»; сервер показывает то же самое в
   /api/hotkeys/status полем desktop.ws_active. */
let shellWsFailures = 0;

async function startShellChannel() {
  if (typeof WebSocket !== 'function') {
    log('канал команд: long-poll (WebSocket недоступен в этой сборке)');
    return pollShellCommands();
  }
  const ok = await new Promise((resolve) => {
    let ws;
    try {
      ws = new WebSocket(`ws://127.0.0.1:${PORT}/api/desktop/ws`);
    } catch (e) {
      log('канал команд: WS не создан — ' + e.message);
      resolve(false);
      return;
    }
    let ready = false;
    const timer = setTimeout(() => {
      if (!ready) {
        log('канал команд: WS молчит 3 с');
        try { ws.close(); } catch (e) { /* */ }
      }
    }, 3000);
    ws.onopen = () => {
      ready = true;
      clearTimeout(timer);
      log('канал команд: websocket');
      resolve(true);
    };
    ws.onerror = () => { /* исход решает onclose */ };
    ws.onclose = () => {
      clearTimeout(timer);
      if (!ready) { resolve(false); return; }
      shellWsFailures += 1;
      log(`канал команд: WS закрыт (${shellWsFailures})`);
      if (shellWsFailures <= 3) {
        setTimeout(() => { startShellChannel().catch(() => {}); },
                   1000 * shellWsFailures);
      } else {
        log('канал команд: перехожу на long-poll');
        pollShellCommands().catch((e) => log('опрос команд: ' + e.message));
      }
    };
    ws.onmessage = async (event) => {
      let msg = null;
      try { msg = JSON.parse(String(event.data)); } catch (e) { return; }
      if (msg && msg.event === 'hello') { shellWsFailures = 0; return; }
      const commands = (msg && msg.commands) || [];
      if (!commands.length) return;
      for (const command of commands) {
        try { applyShellCommand(command); }
        catch (e) { log(`команда #${command.id}: ${e.message}`); }
      }
      const ids = commands.map((c) => c.id);
      try { ws.send(JSON.stringify({ ack: ids })); }
      catch (e) {
        shellRequest('POST', '/api/desktop/ack', { ids }, 5000).catch(() => {});
      }
    };
  });
  if (!ok) {
    log('канал команд: long-poll (WS не поднялся)');
    return pollShellCommands();
  }
}

async function pollShellCommands() {
  for (;;) {
    let answer;
    try {
      answer = await shellRequest('GET', '/api/desktop/commands?wait=25', null, 35000);
    } catch (e) {
      /* Сервер мог ещё не подняться или ушёл в перезапуск: ждём и пробуем снова,
         цикл живёт всё время жизни оболочки. */
      await sleepShell(4000);
      continue;
    }
    const commands = (answer && answer.commands) || [];
    if (!commands.length) continue;
    for (const command of commands) {
      try { applyShellCommand(command); }
      catch (e) { log(`команда #${command.id}: ${e.message}`); }
    }
    try {
      await shellRequest('POST', '/api/desktop/ack',
                         { ids: commands.map((c) => c.id) }, 5000);
    } catch (e) { /* подтверждение потеряно — команда протухнет сама */ }
  }
}

/* ---------- Ярлык приложения на рабочем столе Windows ----------
 * Пользователь просит: из меню «Приложения» (/desktop) отправить
 * приложение на рабочий стол. Ярлык — обычный .lnk, который запускает
 * Infinity Search с аргументом --open-window=<url>; главный процесс
 * разбирает этот аргумент и открывает отдельное окно с приложением.
 *
 * Создаём ярлык сами (shell.writeShortcutLink): путь к electron.exe жёстко
 * зашивать нельзя — в собранной сборке это InfinitySearch.exe, а при
 * разработке electron.exe плюс каталог приложения. */
const OPEN_WINDOW_FLAG = '--open-window=';
/* Заголовок окна (имя приложения) — отдельным аргументом: из адреса он не
   восстанавливается, а окно без понятного заголовка на рабочем столе
   выглядит заброшенным. */
const OPEN_TITLE_FLAG = '--open-title=';

function parseArgValue(argv, flag) {
  if (!Array.isArray(argv)) return '';
  for (const a of argv) {
    if (typeof a !== 'string') continue;
    const i = a.indexOf(flag);
    if (i < 0) continue;
    const raw = a.slice(i + flag.length).trim().replace(/^["']|["']$/g, '');
    if (raw) return raw;
  }
  return '';
}

/* URL из аргументов запуска. Значение может быть экранировано кавычками —
   Electron отдаёт argv как есть, поэтому снимаем их. */
function parseOpenWindowUrl(argv) {
  return parseArgValue(argv, OPEN_WINDOW_FLAG);
}

function parseOpenWindowTitle(argv) {
  return parseArgValue(argv, OPEN_TITLE_FLAG);
}

/* Куда указывать ярлык. При разработке запускает electron.exe, которому
 * нужен каталог приложения; в собранной сборке — сам InfinitySearch.exe. */
function shortcutTarget() {
  if (app.isPackaged) {
    return { target: process.execPath, args: '' };
  }
  const appDir = path.resolve(__dirname);
  return {
    target: process.execPath,
    args: `"${appDir}"`,
  };
}

/* Имя файла ярлыка: Windows не любит в имени \ / : * ? " < > |. */
function safeShortcutName(raw) {
  let name = String(raw || '').trim().replace(/[\\/:*?"<>|]/g, ' ').replace(/\s+/g, ' ').trim();
  if (!name) name = 'Infinity';
  return name.slice(0, 100);
}

function desktopIconPath() {
  const ico = path.join(ROOT, 'infinity.ico');
  if (fs.existsSync(ico)) return ico;
  return fs.existsSync(ICON) ? ICON : path.join(__dirname, 'icon.png');
}

/* Создать .lnk на рабочем столе. Возвращает {ok, path} или {ok:false,error}. */
function createDesktopShortcut(payload) {
  const p = payload || {};
  const url = String(p.url || '').trim();
  if (!url) return { ok: false, error: 'Не указан адрес приложения' };
  if (!/^(https?|file):\/\//i.test(url)) {
    return { ok: false, error: 'Адрес приложения должен быть http(s) или file' };
  }
  const name = safeShortcutName(p.name || 'Infinity');
  let linkPath;
  try {
    const dir = app.getPath('desktop');
    linkPath = path.join(dir, name + '.lnk');
    const t = shortcutTarget();
    const args = [t.args,
                  OPEN_WINDOW_FLAG + url,
                  OPEN_TITLE_FLAG + name].filter(Boolean).join(' ');
    const ok = shell.writeShortcutLink(linkPath, {
      target: t.target,
      args: args,
      cwd: ROOT,
      icon: desktopIconPath(),
      description: name + ' — Infinity Search',
    });
    if (!ok) return { ok: false, error: 'Windows не смогла создать ярлык' };
    log(`ярлык создан: ${linkPath} -> ${url}`);
    return { ok: true, path: linkPath };
  } catch (e) {
    log('ярлык не создан: ' + e.message);
    return { ok: false, error: e.message };
  }
}

ipcMain.handle('desktop-create-shortcut', (_e, payload) => createDesktopShortcut(payload));

/* Превью окон на панели задач: снимок области страницы рабочего стола.
 * Координаты — CSS-пиксели окна WinBox относительно вьюпорта, capturePage
 * принимает их напрямую. Кадр уменьшаем до 320 px и жмём в JPEG: карточка
 * превью маленькая, а PNG на весь экран весил бы мегабайты. */
ipcMain.handle('desktop-capture-rect', async (event, rect) => {
  try {
    const r = rect || {};
    const x = Math.round(Number(r.x) || 0);
    const y = Math.round(Number(r.y) || 0);
    const width = Math.round(Number(r.width) || 0);
    const height = Math.round(Number(r.height) || 0);
    if (width < 8 || height < 8) return '';
    const img = await event.sender.capturePage({
      x: Math.max(0, x), y: Math.max(0, y), width, height,
    });
    if (!img || img.isEmpty()) return '';
    const scaled = img.resize({ width: Math.min(320, width), quality: 'good' });
    return 'data:image/jpeg;base64,' + scaled.toJPEG(72).toString('base64');
  } catch (e) {
    log('превью окна не снято: ' + e.message);
    return '';
  }
});

/* «Открыть отдельно» из меню приложений: отдельное окно Electron с самим
 * приложением. Пускаем только http(s) на локальный сервер (или локальный
 * порт приложения с собственным сервером) — внешние адреса страница и так
 * открывает через window.open/браузер. Рамка системная: чужие web-сборки
 * не рисуют наши кнопки окна, и такое окно иначе нечем закрыть. */
ipcMain.handle('desktop-open-window', (_e, payload) => {
  const p = payload || {};
  const url = String(p.url || '').trim();
  if (!/^https?:\/\//i.test(url)) {
    return { ok: false, error: 'Адрес приложения должен быть http(s)' };
  }
  let host = '';
  try { host = new URL(url).hostname; } catch (e) {
    return { ok: false, error: 'Некорректный адрес приложения' };
  }
  const local = host === HOST || host === 'localhost' || host === '127.0.0.1' || host === '::1';
  if (!local) {
    return { ok: false, error: 'Отдельным окном открываются только локальные приложения' };
  }
  openInternalWindow(url, {
    nativeFrame: true,
    title: String(p.title || 'Приложение'),
  });
  return { ok: true };
});

/* Адрес из аргументов запуска: ярлык на рабочем столе запускает приложение
 * отдельным окном. Запоминаем сразу — сервер в этот момент ещё не поднят,
 * окно откроем после его старта. */
const startupOpenUrl = parseOpenWindowUrl(process.argv);
const startupOpenTitle = parseOpenWindowTitle(process.argv);

/* Окно приложения, запущенного по ярлыку: системная рамка (приложение
   чужое, наши кнопки окна оно не рисует) и понятный заголовок. */
function openShortcutWindow(url, title) {
  return openInternalWindow(url, { nativeFrame: true, title: title || 'Infinity Search' });
}

/* ---------- Палитра быстрого запроса к ИИ (Shift+Space) ----------
 * Основное окно развёрнуто — показываем оверлей прямо в нём, /desktop
 * сам открывает палитру по горячей клавише. Окно свёрнуто или скрыто в
 * трей — поднимаем отдельное прозрачное окно поверх рабочего стола,
 * не трогая mainWindow: пользователь просил строку запроса вместо
 * разворачивания всего приложения.
 */
/* Размер окна палитры хранит пользователь: в нём живёт и чат, и плеер,
   а плеер без нормального размера бесполезен. Пишем в data\aibar_window.json
   с задержкой — событий resize/move бывает очень много. */
const AI_BAR_STATE = path.join(ROOT, 'data', 'aibar_window.json');
const AI_BAR_MIN = { width: 560, height: 420 };
const AVATAR_STATE = path.join(ROOT, 'data', 'avatar_window.json');
const AVATAR_MIN = { width: 220, height: 260 };

/* Прозрачное окно поверх рабочего стола перехватывает клики по всей своей
   площади. Поэтому сохранённую геометрию оверлеев ограничиваем долей рабочего
   стола: раздутое когда-то окно после перезапуска накрывало почти весь экран,
   и ни приложение, ни другие программы не получали кликов. */
function clampOverlayBounds(b, min, area, maxShare) {
  if (!b) return null;
  const maxW = Math.max(min.width, Math.min(area.width - 20, Math.round(area.width * maxShare)));
  const maxH = Math.max(min.height, Math.min(area.height - 20, Math.round(area.height * maxShare)));
  const width = Math.max(min.width, Math.min(Math.round(Number(b.width)) || min.width, maxW));
  const height = Math.max(min.height, Math.min(Math.round(Number(b.height)) || min.height, maxH));
  const x = Math.max(area.x, Math.min(Math.round(Number(b.x)) || area.x,
                                     area.x + area.width - width));
  const y = Math.max(area.y, Math.min(Math.round(Number(b.y)) || area.y,
                                      area.y + area.height - height));
  return { x, y, width, height, opacity: b.opacity };
}

function loadAiBarBounds() {
  try {
    const b = JSON.parse(fs.readFileSync(AI_BAR_STATE, 'utf8'));
    if (b && b.width >= AI_BAR_MIN.width && b.height >= AI_BAR_MIN.height) return b;
  } catch (e) { /* первый запуск — размер дефолтный */ }
  return null;
}

let aiBarSaveTimer = null;
function writeAiBarBounds(win) {
  if (!win || win.isDestroyed()) return;
  try {
    fs.mkdirSync(path.dirname(AI_BAR_STATE), { recursive: true });
    fs.writeFileSync(AI_BAR_STATE, JSON.stringify(win.getBounds()));
  } catch (e) { /* не критично */ }
}
function saveAiBarBounds(win) {
  if (!win || win.isDestroyed()) return;
  clearTimeout(aiBarSaveTimer);
  // Событий resize/move бывает много: пишем не чаще, чем раз в полсекунды.
  aiBarSaveTimer = setTimeout(() => writeAiBarBounds(win), 500);
}

function createAiBarWindow() {
  const cursor = screen.getCursorScreenPoint();
  const area = (screen.getDisplayNearestPoint(cursor) || screen.getPrimaryDisplay()).workArea;
  const saved = clampOverlayBounds(loadAiBarBounds(), AI_BAR_MIN, area, 0.9);
  const width = saved ? saved.width : 760;
  const height = saved ? saved.height : 560;
  const x = saved ? Math.max(area.x, Math.min(saved.x, area.x + area.width - 80))
                  : Math.round(area.x + (area.width - width) / 2);
  const y = saved ? Math.max(area.y, Math.min(saved.y, area.y + area.height - 80))
                  : Math.round(area.y + (area.height - height) / 2);
  const win = new BrowserWindow({
    x, y,
    width, height,
    frame: false,
    transparent: true,
    // Размер меняет сам пользователь (угол-«хваталка» в странице): окно
    // живёт не только чатом, но и плеером медиа-модуля.
    resizable: true,
    minWidth: AI_BAR_MIN.width,
    minHeight: AI_BAR_MIN.height,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    hasShadow: false,
    alwaysOnTop: true,
    show: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
      /* Скрытое окно не должно замедлять таймеры: и у мини-плеера, и у
         палитры по скрытости работает обмен состоянием (media.playback),
         а фоновая троттлинг-пауза ломает передачу трека. */
      backgroundThrottling: false,
      /* Прозрачный оверлей: свои кнопки окна ему не нужны (панель
         перетаскивания рисует сама страница ai-bar). */
      additionalArguments: ['--infinity-chrome=overlay'],
    },
  });
  win.setAlwaysOnTop(true, 'floating');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.__ready = false;
  win.webContents.on('did-finish-load', () => { win.__ready = true; });
  attachPageLogs(win, 'палитра');
  /* Кнопка «открыть агента» в палитре делает window.open('/agentai').
     Без обработчика Electron создавал обычное дочернее окно: с системной
     рамкой, вне трея и вне управления приложением. */
  win.webContents.setWindowOpenHandler((details) => handleWindowOpen(details.url));
  win.loadURL(`${URL_MAIN}ai-bar`);
  // Закрытие прячет окно, а не уничтожает: Shift+Space из трея должен
  // поднимать его снова мгновенно, без нового BrowserWindow.
  win.on('close', (e) => {
    writeAiBarBounds(win);          // размер должен пережить закрытие окна
    if (!reallyQuit) { e.preventDefault(); hideAiBar(); }
  });
  win.on('resize', () => saveAiBarBounds(win));
  win.on('move', () => saveAiBarBounds(win));
  // Потеря фокуса прячет окно — кроме случаев, когда агент отвечает или
  // пользователь закрепил его кнопкой «закрепить».
  win.on('blur', () => { if (!aiBarBusy && !aiBarPinned) hideAiBar(); });
  return win;
}

function hideAiBar() {
  aiBarPinned = false;
  if (aiBarWindow && !aiBarWindow.isDestroyed()) {
    aiBarWindow.webContents.send('ai-bar-pin', false);
    /* Окно скрыли — значит звук должен переехать на рабочий стол:
       страница сама отдаёт трек мини-плееру. */
    aiBarWindow.webContents.send('ai-bar-hidden');
    aiBarWindow.hide();
  }
}

/* Показать окно палитры. Страница должна успеть инициализироваться:
   сообщение о хоткее, посланное в незагруженный документ, теряется, и
   первый вызов из трея прошёл бы впустую. */
function showAiBar() {
  if (!aiBarWindow || aiBarWindow.isDestroyed()) aiBarWindow = createAiBarWindow();
  // Новое showing — всегда незакреплённое: закрепление относится к одному
  // вызову окна, а не ко всем последующим.
  aiBarPinned = false;
  aiBarWindow.show();
  aiBarWindow.focus();
  if (aiBarWindow.__ready) {
    aiBarWindow.webContents.send('ai-bar-pin', false);
    // Окно показали — трек, если его держит рабочий стол, должен переехать сюда.
    aiBarWindow.webContents.send('ai-bar-shown');
    return Promise.resolve(aiBarWindow);
  }
  return new Promise((resolve) => {
    aiBarWindow.webContents.once('did-finish-load', () => {
      aiBarWindow.webContents.send('ai-bar-shown');
      resolve(aiBarWindow);
    });
  });
}

/* ---------- Окно 3D-аватара ----------
 * Прозрачное окно без рамки: на экране только модель. Позицию, размер и
 * прозрачность пользователь меняет сам (ручка-«хваталка» и меню-шестерёнка
 * нарисованы в самой странице /avatar?embed=1). Окно живёт независимо от
 * палитры Shift+Space: закрыл палитру — аватар остался.
 */
function loadAvatarBounds() {
  try {
    const b = JSON.parse(fs.readFileSync(AVATAR_STATE, 'utf8'));
    if (b && b.width >= AVATAR_MIN.width && b.height >= AVATAR_MIN.height) return b;
  } catch (e) { /* первый запуск */ }
  return null;
}

/* Состояние окна аватара целиком (позиция, прозрачность, видимость).
   Видимость — часть состояния: скрытый аватар не должен всплывать после
   перезапуска, а показанный должен вернуться на место. */
function loadAvatarState() {
  try {
    const s = JSON.parse(fs.readFileSync(AVATAR_STATE, 'utf8'));
    return (s && typeof s === 'object') ? s : {};
  } catch (e) { return {}; }
}

function writeAvatarState(patch) {
  try {
    const next = Object.assign(loadAvatarState(), patch || {});
    fs.mkdirSync(path.dirname(AVATAR_STATE), { recursive: true });
    fs.writeFileSync(AVATAR_STATE, JSON.stringify(next));
  } catch (e) { /* не критично */ }
}

function setAvatarVisible(visible) { writeAvatarState({ visible: !!visible }); }

function writeAvatarBounds(win) {
  if (!win || win.isDestroyed()) return;
  try {
    const b = win.getBounds();
    const prev = loadAvatarState();
    if (typeof prev.opacity === 'number') b.opacity = prev.opacity;
    if (typeof prev.visible === 'boolean') b.visible = prev.visible;
    fs.mkdirSync(path.dirname(AVATAR_STATE), { recursive: true });
    fs.writeFileSync(AVATAR_STATE, JSON.stringify(b));
  } catch (e) { /* не критично */ }
}
function saveAvatarBounds(win) {
  if (!win || win.isDestroyed()) return;
  clearTimeout(avatarSaveTimer);
  avatarSaveTimer = setTimeout(() => writeAvatarBounds(win), 500);
}

function createAvatarWindow() {
  const cursor = screen.getCursorScreenPoint();
  const area = (screen.getDisplayNearestPoint(cursor) || screen.getPrimaryDisplay()).workArea;
  const saved = clampOverlayBounds(loadAvatarBounds(), AVATAR_MIN, area, 0.6);
  const width = saved ? saved.width : 340;
  const height = saved ? saved.height : 480;
  const x = saved ? Math.max(area.x, Math.min(saved.x, area.x + area.width - 80))
                  : Math.round(area.x + area.width - width - 40);
  const y = saved ? Math.max(area.y, Math.min(saved.y, area.y + area.height - 80))
                  : Math.round(area.y + area.height - height - 60);
  const win = new BrowserWindow({
    x, y, width, height,
    frame: false,
    transparent: true,
    resizable: true,
    minWidth: AVATAR_MIN.width,
    minHeight: AVATAR_MIN.height,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    hasShadow: false,
    alwaysOnTop: true,
    show: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
      /* Модель и lipsync должны работать, даже когда окно не в фокусе. */
      backgroundThrottling: false,
      /* Прозрачный оверлей без рамки: панель перетаскивания и кнопки
         (голос, настройки, скрыть) рисует сама страница /avatar?embed=1. */
      additionalArguments: ['--infinity-chrome=overlay'],
    },
  });
  win.setAlwaysOnTop(true, 'floating');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  /* Прозрачность из прошлого запуска: окно могло остаться полупрозрачным,
     и без применения настройки пользователь видел бы «пропавший» аватар. */
  if (saved && typeof saved.opacity === 'number') {
    try { win.setOpacity(Math.max(0.35, Math.min(1, saved.opacity))); } catch (e) { /* */ }
  }
  win.__ready = false;
  win.webContents.on('did-finish-load', () => { win.__ready = true; writeAvatarBounds(win); });
  /* Страница не загрузилась (сервер ещё не поднялся, порт занят) — окно
     прячем: прозрачный оверлей без содержимого молча съедал бы все клики. */
  win.webContents.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
    if (!isMainFrame) return;
    log(`аватар: окно спрятано, страница не загрузилась (${code} ${desc})`);
    try { win.hide(); } catch (e2) { /* окно уже уничтожено */ }
  });
  attachPageLogs(win, 'аватар');
  win.webContents.setWindowOpenHandler((details) => handleWindowOpen(details.url));
  win.loadURL(`${URL_MAIN}avatar?embed=1`);
  /* Первое появление — по готовности страницы: иначе на секунду видна
     пустая прозрачная рамка и тёмная заливка загрузчика. */
  win.once('ready-to-show', () => {
    if (!reallyQuit) win.show();
  });
  win.on('close', (e) => {
    writeAvatarBounds(win);
    if (!reallyQuit) { e.preventDefault(); win.hide(); setAvatarVisible(false); }
  });
  win.on('resize', () => saveAvatarBounds(win));
  win.on('move', () => saveAvatarBounds(win));
  return win;
}

function showAvatar() {
  const created = !avatarWindow || avatarWindow.isDestroyed();
  if (created) avatarWindow = createAvatarWindow();
  // Новое окно покажет себя само по ready-to-show (страница ещё грузится).
  if (!created) {
    avatarWindow.show();
    /* Настройки могли поменять, пока окно было скрыто. storage-событие
       обычно доходит и так, но просим страницу перечитать конфиг явно —
       иначе модель/ракурс обновлялись только после перезапуска. */
    avatarWindow.webContents.executeJavaScript(
      'try{window.__infinityReloadAvatar&&window.__infinityReloadAvatar()}catch(e){}'
    ).catch(() => {});
  }
  avatarWindow.setAlwaysOnTop(true, 'floating');
  setAvatarVisible(true);
  return avatarWindow;
}

function hideAvatar() {
  if (avatarWindow && !avatarWindow.isDestroyed()) {
    writeAvatarBounds(avatarWindow);
    avatarWindow.hide();
  }
  setAvatarVisible(false);
}

function toggleAvatar() {
  if (avatarWindow && !avatarWindow.isDestroyed() && avatarWindow.isVisible()) {
    hideAvatar();
    return false;
  }
  showAvatar();
  return true;
}

/* Страница аватара сообщает, каким должен быть размер окна (пункты меню
   «маленький / обычный / большой»). Ограничиваем рабочим столом, чтобы окно
   не уехало за край. */
function setAvatarSize(w, h) {
  if (!avatarWindow || avatarWindow.isDestroyed()) return;
  const area = avatarWindow.getBounds();
  const disp = (screen.getDisplayNearestPoint({ x: area.x, y: area.y }) ||
                screen.getPrimaryDisplay()).workArea;
  const maxW = Math.max(AVATAR_MIN.width, Math.min(disp.width - 20, Math.round(disp.width * 0.6)));
  const maxH = Math.max(AVATAR_MIN.height, Math.min(disp.height - 20, Math.round(disp.height * 0.6)));
  const width = Math.max(AVATAR_MIN.width, Math.min(Math.round(w) || 340, maxW));
  const height = Math.max(AVATAR_MIN.height, Math.min(Math.round(h) || 480, maxH));
  avatarWindow.setSize(width, height);
  writeAvatarBounds(avatarWindow);
}

function setAvatarOpacity(v) {
  let op = Number(v);
  if (!isFinite(op)) op = 1;
  op = Math.max(0.35, Math.min(1, op));
  if (avatarWindow && !avatarWindow.isDestroyed()) avatarWindow.setOpacity(op);
  writeAvatarState({ opacity: op });
}

ipcMain.on('avatar-toggle', () => { toggleAvatar(); });
ipcMain.on('avatar-show', () => { showAvatar(); });
ipcMain.on('avatar-hide', () => { hideAvatar(); });
ipcMain.on('avatar-resize', (_e, size) => {
  if (!size) return;
  setAvatarSize(size.w, size.h);
});
ipcMain.on('avatar-opacity', (_e, v) => { setAvatarOpacity(v); });
ipcMain.handle('avatar-visible', () => !!(avatarWindow && !avatarWindow.isDestroyed() && avatarWindow.isVisible()));


/* ---------- Съёмка экрана для палитры (показать ИИ экран/область) ----------
 * Страница зовёт getDisplayMedia. Без обработчика Electron вообще не даёт
 * снимать экран, а системный выбор дисплея перекрыл бы ровно то, что
 * пользователь собирается снять. Отвечаем сами — первым экраном.
 */
function setupDisplayCapture() {
  const d = screen.getPrimaryDisplay();
  const size = {
    width: Math.min(Math.round(d.bounds.width * d.scaleFactor), 2560),
    height: Math.min(Math.round(d.bounds.height * d.scaleFactor), 1440),
  };
  try {
    const handler = (_request, callback) => {
      desktopCapturer.getSources({ types: ['screen'], thumbnailSize: size })
        .then((sources) => callback(sources && sources.length ? { video: sources[0] } : {}))
        .catch(() => callback({}));
    };
    session.defaultSession.setDisplayMediaRequestHandler(handler);
    /* Встроенный браузер живёт в своей persistent-сессии (расширения) —
       без своего обработчика getDisplayMedia там был бы запрещён. */
    try { extensions.getSession().setDisplayMediaRequestHandler(handler); } catch (e) {}
  } catch (e) {
    log('съёмка экрана недоступна: ' + e.message);
  }
}

/* ---------- Блокировка трекеров и рекламы во встроенном браузере ----------
   Список — только заведомо нефункциональные хосты (счётчики, рекламные сети,
   пиксели соцсетей). Широкий «блокируй всё похожее на рекламу» ломает страницы,
   поэтому здесь консервативный набор, а не полный фильтр вроде EasyList.

   webRequest.onBeforeRequest в сессии webview отменяет запрос ДО сети, так что
   он не тратит трафик и не дёргает страницу. Счётчик блокировок показан в
   меню браузера — иначе непонятно, работает ли блокировка вообще. */
const TRACKER_HOSTS = [
  'google-analytics.com', 'ssl.google-analytics.com', 'www.google-analytics.com',
  'googletagmanager.com', 'www.googletagmanager.com', 'googlesyndication.com',
  'pagead2.googlesyndication.com', 'googleadservices.com', 'www.googleadservices.com',
  'doubleclick.net', 'stats.g.doubleclick.net', 'adservice.google.com',
  'connect.facebook.net', 'pixel.facebook.com', 'an.facebook.com',
  'mc.yandex.ru', 'mc.yandex.com', 'an.yandex.ru', 'adfox.yandex.ru',
  'counter.yadro.ru', 'top-fwz1.mail.ru', 'ad.mail.ru', 'r.mail.ru',
  'scorecardresearch.com', 'sb.scorecardresearch.com',
  'hotjar.com', 'static.hotjar.com', 'script.hotjar.com',
  'amplitude.com', 'api.amplitude.com', 'mixpanel.com', 'api.mixpanel.com',
  'segment.io', 'api.segment.io', 'cdn.segment.com',
  'quantserve.com', 'pixel.quantserve.com', 'adnxs.com', 'adsrvr.org',
  'taboola.com', 'cdn.taboola.com', 'outbrain.com', 'widgets.outbrain.com',
  'criteo.com', 'static.criteo.net', 'bat.bing.com', 'clarity.ms',
];

function blockedHost(rawUrl) {
  let h;
  try { h = new URL(rawUrl).hostname.toLowerCase(); } catch (e) { return false; }
  // Точное совпадение или поддомен (.example.com перекроет a.b.example.com).
  return TRACKER_HOSTS.some((d) => h === d || h.endsWith('.' + d));
}

let trackerBlocked = 0;

function setupTrackerBlocking(contents) {
  try {
    contents.session.webRequest.onBeforeRequest((details, cb) => {
      if (blockedHost(details.url)) {
        trackerBlocked++;
        cb({ cancel: true });
        return;
      }
      cb({ cancel: false });
    });
  } catch (err) {
    log('блокировка трекеров недоступна: ' + err.message);
  }
}

/* ---------- Вкладки <webview> встроенного браузера ----------
 * Каждый webview — отдельный webContents. Здесь два дела:
 *   1) target=_blank / window.open открываем новой вкладкой (сообщение в
 *      страницу браузера), а не во внешнем браузере;
 *   2) загрузки файлов перехватываем и шлём прогресс в страницу браузера,
 *      чтобы та показала панель загрузок; путь сохраняем через диалог.
 * Вешаем на все webview в приложении: отличить webview браузера от прочих
 * можно по hostWebContents, но проще покрыть все — в остальных окнах webview
 * нет (webviewTag=false). */
function setupBrowserWebviews() {
  app.on('web-contents-created', (_e, contents) => {
    if (contents.getType && contents.getType() !== 'webview') return;

    setupTrackerBlocking(contents);
    /* Свои плагины браузера: внедрение на dom-ready каждой вкладки. */
    plugins.attach(contents);

    // Новое окно из webview (target=_blank) — открываем вкладкой в браузере.
    contents.setWindowOpenHandler(({ url }) => {
      try {
        contents.hostWebContents && contents.hostWebContents.send('browser-open-tab', url);
      } catch (err) { /* нет хоста — игнор */ }
      return { action: 'deny' };
    });

    // Загрузки: прогресс и завершение шлём в окно-хост (страницу браузера).
    // item держим в реестре по id, чтобы страница могла ставить на паузу,
    // возобновлять и отменять загрузку (раньше оставалось только смотреть).
    contents.session.on('will-download', (_event, item) => {
      const total = item.getTotalBytes();
    const name = item.getFilename();
    const dlId = 'dl' + (++dlSeq);
    browserDownloads.set(dlId, item);
    item.on('done', () => {
      // Чистим реестр не сразу: страница читает state по последнему
      // событию, а 'done' приходит раньше, чем она успевает нажать кнопку.
      setTimeout(() => browserDownloads.delete(dlId), 5000);
    });
    const send = (extra) => {
      try {
        if (contents.hostWebContents) {
          contents.hostWebContents.send('browser-download', Object.assign({
            id: dlId,
            url: item.getURL(),
            name,
            received: item.getReceivedBytes(),
            total,
            state: item.isPaused() ? 'paused' : 'progressing',
            canResume: item.canResume(),
          }, extra));
        }
      } catch (err) { /* хост мог закрыться */ }
    };
    item.on('updated', () => send());
    item.on('done', (_ev, state) => send({ state, path: item.getSavePath() }));

      // Спрашиваем, куда сохранить. Пользователь отменил — прерываем загрузку.
      dialog.showSaveDialog({
        title: 'Сохранить файл',
        defaultPath: path.join(app.getPath('downloads'), name),
      }).then((res) => {
        if (res.canceled || !res.filePath) item.cancel();
        else item.setSavePath(res.filePath);
      }).catch(() => item.cancel());
    });
  });
}

/* Выделенный текст на странице. Отдаём его палитре: пользователь просил,
   чтобы выдержка из браузера попадала агенту сразу, без пересылки вручную.
   /desktop — оболочка с iframe, поэтому смотрим и основной документ, и
   вложенные (доступен только свой origin). */
const SELECTION_JS = `(function(){
  function pick(d){ try { return String(d.getSelection ? d.getSelection() : '').trim(); } catch(e){ return ''; } }
  var t = pick(document); if (t) return t;
  var f = document.querySelectorAll('iframe');
  for (var i=0;i<f.length;i++){ try { var c=f[i].contentDocument; if(!c) continue; t=pick(c); if(t) return t; } catch(e){} }
  return '';
})()`;

function pageSelection(win) {
  if (!win || win.isDestroyed()) return Promise.resolve('');
  return win.webContents.executeJavaScript(SELECTION_JS, true).catch(() => '');
}

function onAiBarHotkey() {
  // Палитра на экране — решение отдаём странице: при включённой диктовке
  // это нажатие начинает/останавливает запись, иначе окно прячется.
  if (aiBarWindow && !aiBarWindow.isDestroyed() && aiBarWindow.isVisible()) {
    aiBarWindow.webContents.send('ai-bar-hotkey', { pinned: aiBarPinned });
    return;
  }
  const mainFocused = mainWindow && !mainWindow.isDestroyed()
    && mainWindow.isVisible() && !mainWindow.isMinimized() && mainWindow.isFocused();
  if (mainFocused) {
    // В фокусе своя страница — оверлей рисует она сама, и выделенный текст
    // берём у неё же.
    pageSelection(mainWindow).then((sel) => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('ai-bar-hotkey', { selection: sel });
      }
    });
    return;
  }
  pageSelection(mainWindow).then((sel) => {
    showAiBar().then((win) => {
      if (win && !win.isDestroyed()) win.webContents.send('ai-bar-hotkey', { selection: sel });
    });
  });
}

function registerAiBarHotkey() {
  try {
    globalShortcut.register('Shift+Space', onAiBarHotkey);
    log('хоткей Shift+Space зарегистрирован');
  } catch (e) {
    log('Shift+Space не зарегистрирован: ' + e.message);
  }
}

/* ---------- Трей ---------- */
function createTray() {
  let image = nativeImage.createFromPath(ICON);
  if (image.isEmpty()) image = nativeImage.createEmpty();
  tray = new Tray(image);
  tray.setToolTip('Infinity Search');
  /* Меню пересобирается перед показом: пункт аватара — переключатель,
     и его подпись должна отражать текущее состояние. */
  const buildMenu = () => {
    const shown = !!(avatarWindow && !avatarWindow.isDestroyed() && avatarWindow.isVisible());
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: 'Показать', click: showMain },
      { label: 'Свернуть', click: () => mainWindow && mainWindow.minimize() },
      { label: shown ? 'Скрыть 3D-аватар' : 'Показать 3D-аватар', click: () => toggleAvatar() },
      { type: 'separator' },
      { label: 'Выход', click: () => { reallyQuit = true; app.quit(); } },
      { label: 'Выход и гасить сервер сразу', click: () => { reallyQuit = true; shutdownEverything(); app.quit(); } },
    ]));
  };
  buildMenu();
  tray.on('right-click', buildMenu);
  tray.on('click', toggleWindow);
}

/* ---------- Команды окна из страницы ----------
   Полосу с кнопками окна рисует сама страница (static/js\desktop_frame.js), и
   она есть у каждого окна приложения — не только у главного. Поэтому команда
   должна относиться к ТОМУ окну, которое её прислало: иначе «×» в
   дополнительном окне прятал в трей главное окно, а доп. окно оставалось
   висеть без владельца и не реагировало ни на клики, ни на повторное «×».
   Приём уже применяется в window-move-by и browser-close. */
function senderWin(e) {
  const win = BrowserWindow.fromWebContents(e.sender);
  return win && !win.isDestroyed() ? win : null;
}
function isMainWin(win) { return !!win && win === mainWindow; }

ipcMain.on('window-minimize', (e) => {
  const win = senderWin(e) || (mainWindow && !mainWindow.isDestroyed() ? mainWindow : null);
  if (win) win.minimize();
});
ipcMain.on('window-maximize', (e) => {
  const win = senderWin(e);
  /* Любое окно, кроме главного, — обычные «развернуть/свернуть» самого окна:
      док и его геометрия к нему отношения не имеют. */
  if (win && !isMainWin(win)) {
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
    return;
  }
  if (!mainWindow) return;
  if (preDockBounds) {
    /* В доковом режиме настоящего maximize нет: окно и так подвинуто.
       «Развернуть» = занять всю рабочую область, сохранив док справа;
       повторное нажатие — вернуть геометрию до дока. */
    const area = screen.getDisplayMatching(mainWindow.getBounds()).workArea;
    if (!dockMaximized) {
      dockMaximized = true;
      dockWidth = clamp(dockWidth, DOCK_MIN_BROWSER, area.width - DOCK_MIN_MAIN);
      mainWindow.setBounds({ x: area.x, y: area.y, width: area.width - dockWidth, height: area.height });
    } else {
      dockMaximized = false;
      mainWindow.setBounds(preDockBounds);
    }
    applyBrowserGeometry();
    return;
  }
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
});
ipcMain.on('window-close', (e) => {
  const win = senderWin(e);
  /* «×» в дополнительном окне страницы или в отдельном окне браузера —
      закрываем именно это окно, главное не трогаем. */
  if (win && !isMainWin(win)) { win.close(); return; }
  // Кнопка «×» главного окна прячет окно в трей: приложение продолжает жить
  // одним инстансом (single-instance lock), повторный запуск просто показывает
  // это же окно — так не накапливаются копии и не сиротеют дерева процессов.
  // Полный выход — только из меню трея («Выход» / «Выход и гасить сервер сразу»),
  // там корректно останавливается дерево сервера (см. shutdownEverything).
  // Свернуть окно без скрытия — кнопка «–» или Alt+Enter.
  if (mainWindow) mainWindow.hide();
});
ipcMain.on('window-toggle', (e) => {
  const win = senderWin(e);
  // Alt+Enter в дополнительном окне — свернуть/показать ЕГО, а не главное.
  if (win && !isMainWin(win)) {
    if (win.isVisible() && !win.isMinimized()) win.minimize();
    else { win.show(); win.focus(); }
    return;
  }
  toggleWindow();
});
// Открыть/показать встроенный браузер. mode: 'half' (прав. половина, по
// умолчанию), 'full' (весь экран) или 'window' (отдельное окно). Повторный
// вызов с тем же режимом просто поднимает уже открытое окно.
ipcMain.on('browser-open', (_e, mode) => showBrowser(mode || 'half'));
// Переключение режима показа из самой страницы браузера (кнопки half/full/new).
ipcMain.on('browser-set-mode', (e, mode) => {
  const from = BrowserWindow.fromWebContents(e.sender);
  showBrowser(mode || 'half', from && from.__isBrowser ? from : null);
});
// Создать ещё одно самостоятельное окно браузера (кнопка «отдельное окно»).
// Это НЕ основной browserWindow: у него своя геометрия, и оно не следует за
// главным окном. Основной browserWindow при этом не трогаем.
ipcMain.on('browser-new-window', () => {
  const win = createBrowserWindow('window');
  win.show();
  win.focus();
  log('браузер: открыто отдельное окно (отдельных всего: ' + freeBrowserWindows.size + ')');
});
// Закрыть браузер (кнопка «×» в его панели). Действует строго на приславшее
// окно: в режиме «отдельное окно» их может быть несколько, и закрывать надо
// именно то, где нажали «×». Запасной путь (closeBrowser) трогает только
// доковое окно — никогда главное окно приложения и никогда чужое окно.
ipcMain.on('browser-close', (e) => {
  const win = BrowserWindow.fromWebContents(e.sender);
  if (win && win.__isBrowser) {
    log('браузер: «×» в окне браузера, режим ' + win.__mode);
    win.close();
    return;
  }
  log('браузер: «×» пришёл не из окна браузера — закрываю доковое');
  closeBrowser();
});
/* Полоса-разделитель на левом крае докового браузера: страница присылает
   желаемую ширину браузера, мы перераспределяем её между окнами так, чтобы
   суммарная ширина не менялась (главное окно расширяется на d, браузер
   сужается на d). Работает только в доковом режиме 'half'. */
ipcMain.on('browser-dock-resize', (e, payload) => {
  const win = BrowserWindow.fromWebContents(e.sender);
  if (!win || win.__mode !== 'half' || !preDockBounds) return;
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const want = Number(payload && payload.width);
  if (!isFinite(want) || want <= 0) return;
  const b = mainWindow.getBounds();
  const total = b.width + dockWidth;          // текущий суммарный горизонт
  const next = clamp(want, DOCK_MIN_BROWSER, total - DOCK_MIN_MAIN);
  if (next === dockWidth) return;
  dockWidth = next;
  saveDockWidth(dockWidth);
  mainWindow.setBounds({ x: b.x, y: b.y, width: total - dockWidth, height: b.height });
  applyBrowserGeometry();
});
// Закрытие палитры из её собственного окна (Esc или клик по фону).
ipcMain.on('ai-bar-close', () => hideAiBar());

/* Управление загрузками встроенного браузера: пауза / продолжение / отмена.
   DownloadItem живёт в главном процессе, поэтому кнопки страницы /browser
   присылают сюда id загрузки. Ответ о новом состоянии придёт странице сам —
   через события item.on('updated'/'done'), которые уже шлются в хост. */
ipcMain.on('browser-download-ctl', (_e, cmd) => {
  const id = cmd && cmd.id;
  const item = id && browserDownloads.get(id);
  if (!item || item.isDestroyed && item.isDestroyed()) {
    if (id) browserDownloads.delete(id);
    return;
  }
  const act = String(cmd.action || '');
  try {
    if (act === 'pause' && !item.isPaused()) item.pause();
    else if (act === 'resume' && item.isPaused() && item.canResume()) item.resume();
    else if (act === 'cancel') { item.cancel(); browserDownloads.delete(id); }
  } catch (err) {
    log('browser-download-ctl: ' + err.message);
  }
});

/* Открыть папку загруженного файла в проводнике (кнопка «В папке»). */
ipcMain.on('browser-download-reveal', (_e, p) => {
  try {
    if (p) shell.showItemInFolder(String(p));
  } catch (err) { /* файл мог быть удалён */ }
});

/* Сколько трекеров заблокировано — показывает меню браузера. */
ipcMain.handle('browser-tracker-count', () => trackerBlocked);

/* Печать страницы в PDF. Делаем в главном процессе: webview.print() открыл бы
   системный диалог печати, а нужен файл. Печатаем webContents САМОЙ ВКЛАДКИ —
   e.sender здесь это окно /browser, а не страница сайта, поэтому id вкладки
   приходит аргументом и превращается в webContents через fromId. */
ipcMain.handle('browser-print-pdf', async (e, payload) => {
  const id = payload && payload.webContentsId;
  const suggested = (payload && payload.suggested) || 'page.pdf';
  const wc = (id !== undefined && id !== null) ? webContents.fromId(id) : null;
  if (!wc || wc.isDestroyed()) return { ok: false, error: 'Вкладка уже закрыта' };
  const defName = String(suggested).replace(/[\\/:*?"<>|]/g, '_').slice(0, 80);
  const res = await dialog.showSaveDialog({
    title: 'Сохранить страницу в PDF',
    defaultPath: path.join(app.getPath('documents'), defName),
    filters: [{ name: 'PDF', extensions: ['pdf'] }],
  });
  if (res.canceled || !res.filePath) return { ok: false, canceled: true };
  try {
    const data = await wc.printToPDF({ printBackground: true, pageSize: 'A4' });
    fs.writeFileSync(res.filePath, data);
    return { ok: true, path: res.filePath };
  } catch (err) {
    log('printToPDF: ' + err.message);
    return { ok: false, error: err.message };
  }
});

/* Выделенный текст/ссылку из браузера — в палитру ИИ (Shift+Space).
   Страница /browser сама берёт выделение у webview через executeJavaScript
   и присылает сюда: палитра — отдельное окно, до чужого webview ей не достать. */
ipcMain.on('ai-bar-submit-external', (_e, payload) => {
  const text = (payload && payload.text) || '';
  showAiBar().then((win) => {
    if (win && !win.isDestroyed()) win.webContents.send('ai-bar-submit', text);
  });
});
// Кнопка «закрепить»: закреплённое окно не прячется при потере фокуса.
ipcMain.on('ai-bar-pin', (_e, v) => { aiBarPinned = !!v; });
// Угол-«хваталка» в странице: меняем размер окна, рам-ка у него нет.
ipcMain.on('ai-bar-resize', (_e, size) => {
  if (!aiBarWindow || aiBarWindow.isDestroyed() || !size) return;
  const w = Math.round(Number(size.w) || 0);
  const h = Math.round(Number(size.h) || 0);
  if (w < AI_BAR_MIN.width || h < AI_BAR_MIN.height) return;
  aiBarWindow.setSize(Math.min(w, 2600), Math.min(h, 1800));
});
// Агент ответил — окно палитры можно прятать по потере фокуса.
ipcMain.on('ai-bar-busy', (_e, v) => { aiBarBusy = !!v; });

/* Перемещение окна «за любую часть» при удержании Alt (desktop.html и окно
   палитры). Двигаем то окно, которое прислало дельту, а не всегда главное:
   иначе мини-окно палитры осталось бы неподвижным. Работает только в
   пределах экрана (не даём утащить за границы). */
ipcMain.on('window-move-by', (_e, delta) => {
  const win = BrowserWindow.fromWebContents(_e.sender);
  if (!win || !delta) return;
  const dx = Math.round(Number(delta.dx) || 0);
  const dy = Math.round(Number(delta.dy) || 0);
  if (!dx && !dy) return;
  const [x, y] = win.getPosition();
  win.setPosition(x + dx, y + dy);
});

/* Переезд браузера в отдельную сессию persist:browser (нужно для расширений).
   Закладки и история страницы /browser лежали в localStorage defaultSession —
   читаем их один раз через пустую страницу того же origin и переносим в новую
   сессию. Флаг data\browser_session_migrated.json не даёт повторяться. */
const LS_KEYS = ['infinity_browser_bookmarks', 'infinity_browser_history',
                 'infinity_theme', 'infinity_accent'];

function readBrowserLS(partition) {
  return new Promise((resolve) => {
    const w = new BrowserWindow({
      show: false,
      webPreferences: {
        contextIsolation: true,
        ...(partition ? { partition } : {}),
      },
    });
    const done = (v) => { try { w.destroy(); } catch (e) {} resolve(v); };
    w.loadURL(`${URL_MAIN}static/browser_ls.html`)
      .then(() => w.webContents.executeJavaScript(
        'JSON.stringify({' + LS_KEYS.map((k) =>
          JSON.stringify(k) + ':(localStorage.getItem(' + JSON.stringify(k) + ')||null)').join(',') + '})',
        true))
      .then((s) => { try { done(JSON.parse(s || '{}')); } catch (e) { done({}); } })
      .catch(() => done({}));
  });
}

function writeBrowserLS(partition, data) {
  return new Promise((resolve) => {
    const w = new BrowserWindow({
      show: false,
      webPreferences: { contextIsolation: true, partition },
    });
    const done = () => { try { w.destroy(); } catch (e) {} resolve(); };
    w.loadURL(`${URL_MAIN}static/browser_ls.html`)
      .then(() => w.webContents.executeJavaScript(
        '(' + function (keys, vals) {
          keys.forEach(function (k) { if (vals[k] != null) { try { localStorage.setItem(k, vals[k]); } catch (e) {} } });
        } + ')(' + JSON.stringify(LS_KEYS) + ',' + JSON.stringify(data) + ')', true))
      .then(() => done())
      .catch(() => done());
  });
}

async function migrateBrowserStorage() {
  const FLAG = path.join(ROOT, 'data', 'browser_session_migrated.json');
  if (fs.existsSync(FLAG)) return;
  try {
    const oldData = await readBrowserLS(null); // defaultSession
    const newData = await readBrowserLS(extensions.PARTITION);
    const toWrite = {};
    let any = false;
    for (const k of LS_KEYS) {
      if (!newData[k] && oldData[k]) { toWrite[k] = oldData[k]; any = true; }
    }
    if (any) await writeBrowserLS(extensions.PARTITION, toWrite);
    fs.mkdirSync(path.dirname(FLAG), { recursive: true });
    fs.writeFileSync(FLAG, JSON.stringify({ at: new Date().toISOString(), moved: any }));
    log('перенос данных браузера в persist-сессию: ' + (any ? 'выполнен' : 'не требовался'));
  } catch (e) {
    log('перенос данных браузера не удался: ' + e.message);
  }
}

/* ---------- Диагностика WebGPU ----------
   Скрытое окно спрашивает у Chromium: есть ли navigator.gpu, какой адаптер
   он отдаёт (backend/vendor) и не теряется ли устройство сразу. Результат
   одной строкой в logs\desktop.log («webgpu probe: …») — по нему видно,
   проблема в Electron, в драйвере или в самом приложении. */
async function probeWebGPU() {
  let win = null;
  try {
    win = new BrowserWindow({ show: false, webPreferences: { contextIsolation: true } });
    /* data: URL не подходит: у него непрозрачный origin и не-secure context,
       а navigator.gpu доступен только в secure context. Грузим пустую
       страницу нашего же сервера — тот же origin, что у приложений. */
    await win.loadURL(`${URL_MAIN}static/browser_ls.html`);
    const res = await win.webContents.executeJavaScript(`(async () => {
      const out = { navigatorGpu: !!navigator.gpu };
      try {
        if (navigator.gpu) {
          const adapter = await navigator.gpu.requestAdapter();
          out.adapter = adapter ? {
            vendor: (adapter.info && adapter.info.vendor) || '?',
            architecture: (adapter.info && adapter.info.architecture) || '?',
            backend: (adapter.info && adapter.info.backend) || '?',
            features: Array.from(adapter.features || []).length
          } : null;
          if (adapter) {
            const device = await adapter.requestDevice();
            out.lost = await Promise.race([
              device.lost.then((i) => ({ reason: i.reason, message: i.message })),
              new Promise((r) => setTimeout(() => r('alive-after-3s'), 3000))
            ]);
            try { device.destroy(); } catch (e) {}
          }
        }
      } catch (e) { out.error = String(e); }
      return JSON.stringify(out);
    })()`, true);
    log('webgpu probe: ' + res);
  } catch (e) {
    log('webgpu probe failed: ' + e.message);
  } finally {
    if (win && !win.isDestroyed()) win.destroy();
  }
}

/* Тема и акцент между сессиями. Страницы одного профиля синхронизируются
   через storage-событие своего localStorage, но окно браузера теперь живёт в
   отдельной сессии persist:browser (это нужно для расширений) и чужого
   localStorage не видит. Поэтому любое изменение theme.js присылает сюда,
   а мы рассылаем его всем остальным окнам. theme-get читает текущее значение
   из главного окна — оно источник истины (там страница настроек). */
let themeCache = { theme: null, accent: null };
ipcMain.on('theme-sync', (e, data) => {
  if (!data || typeof data !== 'object') return;
  themeCache = {
    theme: typeof data.theme === 'string' ? data.theme : themeCache.theme,
    accent: typeof data.accent === 'string' ? data.accent : themeCache.accent,
  };
  for (const w of BrowserWindow.getAllWindows()) {
    if (w.isDestroyed() || w.webContents === e.sender) continue;
    try { w.webContents.send('theme-sync', themeCache); } catch (err) { /* окно закрывается */ }
  }
});
ipcMain.handle('theme-get', async () => {
  try {
    if (mainWindow && !mainWindow.isDestroyed()) {
      const v = await mainWindow.webContents.executeJavaScript(
        "(function(){try{return JSON.stringify({theme:localStorage.getItem('infinity_theme')," +
        "accent:localStorage.getItem('infinity_accent')});}catch(e){return '{}';}})()", true);
      const d = JSON.parse(v || '{}');
      if (d && (d.theme || d.accent)) {
        themeCache = { theme: d.theme || themeCache.theme, accent: d.accent || themeCache.accent };
      }
    }
  } catch (e) { /* страница ещё не готова — отдаём кэш */ }
  return themeCache;
});

/* ---------- Запуск ---------- */
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();                    // второй экземпляр — просто показываем первый
} else {
  app.on('second-instance', (_e, argv) => {
    /* Приложение уже запущено, а по ярлыку на рабочем столе кликнули снова:
       второй экземпляр молча вышел, передав нам свои аргументы. Адрес из них
       открываем отдельным окном, а не показываем главное — иначе клик по
       ярлыку не сделал бы того, ради чего его создавали. */
    const url = parseOpenWindowUrl(argv);
    if (url) {
      log('second-instance с адресом: ' + url);
      openShortcutWindow(url, parseOpenWindowTitle(argv));
      return;
    }
    showMain();
  });

  app.whenReady().then(async () => {
    /* Chrome-расширения: реестр в data\chrome_extensions, загрузка включённых
       в persistent-сессию браузера. Делаем до создания любого окна браузера,
       чтобы первая же вкладка уже работала с расширениями. */
    extensions.init({ root: ROOT, log });
    extensions.registerIpc();
    extensions.loadAllEnabled().catch((e) => log('extensions: ' + e.message));

    /* Плагины браузера (свой формат, отдельно от Chrome-расширений):
       реестр в data\browser_plugins, внедрение на dom-ready вкладок. */
    plugins.init({ root: ROOT, log });
    plugins.registerIpc();

    createWindow();
    createTray();
    setupDisplayCapture();
    setupBrowserWebviews();
    registerAiBarHotkey();

    // Показываем окно СРАЗУ с заглушкой — не прячем его на время прогрева
    // сервера (InfinitySearch.exe поднимается до ~3 минут). Как только сервер
    // готов — грузим настоящую страницу.
    mainWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(
      `<meta charset="utf-8"><body style="margin:0;font-family:system-ui;background:#0d1117;` +
      `color:#e6edf3;height:100vh;display:flex;flex-direction:column;align-items:center;` +
      `justify-content:center;gap:16px;user-select:none;overflow:hidden">` +
      `<div style="width:34px;height:34px;border:3px solid rgba(230,237,243,.15);` +
      `border-top-color:#5b8cff;border-radius:50%;animation:sp .8s linear infinite"></div>` +
      `<div style="font-size:15px;letter-spacing:.3px">Infinity Search</div>` +
      `<div style="font-size:12px;color:#8b949e">Запуск сервера…</div>` +
      `<style>@keyframes sp{to{transform:rotate(360deg)}}</style></body>`));
    mainWindow.show();
    mainWindow.focus();

    /* Сначала убираем всё, что осталось от прошлого запуска, и только потом
       поднимаем свой сервер. Раньше здесь стояло «если сервера нет —
       запустим», из-за чего Electron молча брал чужой процесс на 8281: он
       держал в памяти старый код, и правки в app\*.py не были видны до
       ручного перезапуска. Теперь сервер всегда соответствует этой сборке. */
    killOrphanProcesses();
    startServer();

    const up = await waitForServer();
    if (!up) {
      /* Свой сервер не поднялся: если включён перехват, читаем у соседа —
         вместо страницы ошибки. */
      if (await openFailover(mainWindow).catch(() => false)) return;
      log(`сервер не ответил на порту ${PORT} — показываем страницу с ошибкой`);
      mainWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(
        `<meta charset="utf-8"><body style="font-family:system-ui;background:#0d1117;` +
        `color:#e6edf3;padding:40px"><h2>Сервер не запустился</h2>` +
        `<p>Порт ${PORT} не отвечает. Подробности — в <code>logs/desktop.log</code>.</p>` +
        `<p>Проверьте, что рядом с этой папкой лежит <code>InfinitySearch.exe</code>.</p></body>`));
      return;
    }

    mainWindow.loadURL(URL_MAIN);
    mainWindow.focus();

    /* Канал «сервер -> окно» поднимаем только когда сервер отвечает: до этого
       опрос крутил бы пустой цикл на таймаутах. */
    startShellChannel().catch((e) => log('канал команд сервера остановлен: ' + e.message));

    /* Окно аватара поднимаем только теперь, когда сервер отвечает. Раньше оно
       создавалось до старта сервера: страница получала ERR_CONNECTION_REFUSED,
       а прозрачное окно поверх рабочего стола оставалось висеть и перехватывало
       клики — приложение и соседние программы «замирали».
       Видимость берём из состояния: по умолчанию аватар скрыт, и если его
       скрыли, после перезапуска он сам не появится. */
    if (loadAvatarState().visible === true) showAvatar();

    /* Запуск по ярлыку с рабочего стола: адрес приложения открываем
     отдельным окном — но только когда сервер уже отвечает, иначе окно
     покажет страницу «сервер не запустился». */
    if (startupOpenUrl) {
      try {
        log('запуск по ярлыку: ' + startupOpenUrl);
        openShortcutWindow(startupOpenUrl, startupOpenTitle);
      } catch (e) { log('окно по ярлыку не открылось: ' + e.message); }
    }

    /* Перенос закладок/истории встроенного браузера в новую persist-сессию.
       Делаем сразу после подъёма сервера (читает страницу через него), до
       первого открытия окна браузера. Повторно — по флагу ничего не трогаем. */
    migrateBrowserStorage().catch(() => {});

    /* Диагностика GPU: состояние фич Chromium и проба WebGPU — именно из-за
       потери WebGPU-устройства EffectCraft пишет «The graphics device was
       lost». Никак не мешает пользователю: только запись в logs\desktop.log
       («gpu status: …», «webgpu probe: …»). */
    try { log('gpu status: ' + JSON.stringify(app.getGPUFeatureStatus())); } catch (e) { /* нет API */ }
    setTimeout(() => { probeWebGPU(); }, 4000);
  });

  // «×» главного окна прячет окно в трей (см. window-close); «×» в
  // дополнительном окне закрывает только его. В трей прячемся также
  // кнопкой «–» / Alt+Enter — приложение продолжает жить одним инстансом.
  app.on('window-all-closed', () => { /* не выходим: окно может быть скрыто в трей */ });
  app.on('activate', showMain);
  app.on('will-quit', () => {
    reallyQuit = true;
    // Размер окна палитры сохраняем и при выходе: иначе «запомненный»
    // размер всегда на полшага позади последнего действия пользователя.
    if (aiBarWindow && !aiBarWindow.isDestroyed()) {
      clearTimeout(aiBarSaveTimer);
      writeAiBarBounds(aiBarWindow);
    }
    // Глобальный хоткей держит ссылку на обработчик: без снятия он
    // остаётся зарегистрированным после выхода и следующий запуск
    // приложения не сможет его занять.
    globalShortcut.unregisterAll();
  });
  // Гасим сервер вместе со всеми дочерними процессами
  // (Hermes gateway/web UI, silero_pool, локальный LLM) — иначе порты
  // 8642/9120/8080 остаются заняты и следующий запуск падает.
  app.on('before-quit', () => {
    if (!reallyQuit) return;
    /* Отдельные окна страниц больше не дети главного окна, поэтому при выходе
       их снимаем явно: зависшая страница не должна удерживать процесс. */
    for (const w of extraWindows) {
      try { if (!w.isDestroyed()) w.destroy(); } catch (e) { /* уже закрыто */ }
    }
    extraWindows.clear();
    shutdownEverything();
  });
}
