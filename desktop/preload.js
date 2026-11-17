/* Мост между main.js и веб-интерфейсом.
   Через contextBridge в страницу попадает только явно перечисленный API —
   произвольный доступ к Node из рендера закрыт. */
const { contextBridge, ipcRenderer } = require('electron');

/* Preload гостевых страниц <webview> приходит из main-процесса аргументом:
   в песочнице нет __dirname и require('path'), поэтому путь считает main.js. */
let GUEST_PRELOAD = '';
try {
  const args = (typeof process !== 'undefined' && process.argv) || [];
  for (let i = 0; i < args.length; i++) {
    const p = '--infinity-guest-preload=';
    if (args[i] && args[i].indexOf(p) === 0) { GUEST_PRELOAD = args[i].slice(p.length); break; }
  }
} catch (e) { GUEST_PRELOAD = ''; }

/* Какое у окна «хром». main.js передаёт это аргументом, потому что страница
   сама решает, рисовать ли ей панель кнопок окна (desktop_frame.js):
   'app' — главное окно без рамки, панель нужна; 'native' — окно с системной
   рамкой, панель была бы дублем; 'overlay' — прозрачный оверлей с собственной
   полосой перетаскивания. */
let CHROME = 'app';
try {
  const args = (typeof process !== 'undefined' && process.argv) || [];
  for (let i = 0; i < args.length; i++) {
    const p = '--infinity-chrome=';
    if (args[i] && args[i].indexOf(p) === 0) { CHROME = args[i].slice(p.length); break; }
  }
} catch (e) { CHROME = 'app'; }

/* События, на которые страница может подписаться. Список закрыт, чтобы HTML
   не мог подписаться на внутренние сообщения оболочки. */
const CHANNELS = ['window-shown', 'window-hidden', 'server-status',
                   'ai-bar-hotkey', 'ai-bar-submit', 'ai-bar-busy', 'ai-bar-pin',
                   'ai-bar-shown', 'ai-bar-hidden',
                   'browser-download', 'browser-open-tab', 'browser-mode',
                   'theme-sync'];

const api = {
  isElectron: true,
  platform: process.platform,
  /* 'app' | 'native' | 'overlay' — см. разбор аргумента выше. */
  chrome: CHROME,
  /* Preload гостевых страниц <webview> (мост выделения/двойного клика).
     Пустую строку страница воспринимает как «нет preload» и просто его не ставит. */
  guestPreload: GUEST_PRELOAD,

  /* --- управление окном --- */
  minimize: () => ipcRenderer.send('window-minimize'),
  maximize: () => ipcRenderer.send('window-maximize'),
  close: () => ipcRenderer.send('window-close'),
  toggleWindow: () => ipcRenderer.send('window-toggle'),
  /* Перемещение «за любую часть» при Alt+drag: страница шлёт дельты курсора. */
  moveBy: (dx, dy) => ipcRenderer.send('window-move-by', { dx, dy }),

  /* --- встроенный браузер ---
     Кнопка-глобус открывает браузер (half/full/window). Страница /browser
     переключает режим и создаёт отдельные окна теми же вызовами. */
  openBrowser: (mode) => ipcRenderer.send('browser-open', mode),
  setBrowserMode: (mode) => ipcRenderer.send('browser-set-mode', mode),
  newBrowserWindow: () => ipcRenderer.send('browser-new-window'),
  closeBrowser: () => ipcRenderer.send('browser-close'),
  /* Режим дока: 'half' (главное окно сужено, браузер рядом) | 'full' | 'window'.
      Страница браузера показывает полосу-разделитель только в 'half'. */
  onBrowserMode: (cb) => {
    const listener = (_e, mode) => { try { cb(String(mode || '')); } catch (err) { console.error('[electronAPI] browser-mode', err); } };
    ipcRenderer.on('browser-mode', listener);
    return () => ipcRenderer.removeListener('browser-mode', listener);
  },
  /* Перетаскивание раздела дока: желаемая ширина окна браузера. */
  dockResize: (width) => ipcRenderer.send('browser-dock-resize', { width }),
  /* Открыть загруженный файл в системной программе (кнопка «Открыть»).
     Вызывается в контексте preload (Node доступен), shell — из electron. */
  openPath: (p) => { try { require('electron').shell.openPath(String(p)); } catch (e) {} },
  /* Прогресс загрузок и запрос «открыть ссылку в новой вкладке» прилетают
     из main.js (session.will-download / setWindowOpenHandler). */
  onBrowserDownload: (cb) => {
    const listener = (_e, item) => { try { cb(item); } catch (err) { console.error('[electronAPI] browser-download', err); } };
    ipcRenderer.on('browser-download', listener);
    return () => ipcRenderer.removeListener('browser-download', listener);
  },
  onBrowserOpenTab: (cb) => {
    const listener = (_e, url) => { try { cb(url); } catch (err) { console.error('[electronAPI] browser-open-tab', err); } };
    ipcRenderer.on('browser-open-tab', listener);
    return () => ipcRenderer.removeListener('browser-open-tab', listener);
  },
  /* Управление загрузкой (пауза/продолжение/отмена) и открытие её папки.
     DownloadItem живёт в главном процессе — сюда уходит только команда. */
  downloadControl: (id, action) => ipcRenderer.send('browser-download-ctl', { id, action }),
  downloadReveal: (p) => ipcRenderer.send('browser-download-reveal', p),
  /* Печать активной вкладки в PDF: webContentsId вкладки превращается в
      webContents уже в главном процессе (из страницы это сделать нечем). */
  printToPDF: (payload) => ipcRenderer.invoke('browser-print-pdf', payload),
  /* Тема и акцент между сессиями. Обычно страницы синхронизируются через
      storage-событие localStorage, но окно браузера живёт в своей сессии
      (persist:browser) и чужого localStorage не видит. Поэтому theme.js
      присылает изменение сюда, а главный процесс рассылает его остальным
      окнам; текущее значение — theme-get (его держит главное окно). */
  sendTheme: (data) => ipcRenderer.send('theme-sync', data),
  themeGet: () => ipcRenderer.invoke('theme-get'),
  /* Сколько трекеров и рекламы заблокировано на этой сессии. */
  trackerCount: () => ipcRenderer.invoke('browser-tracker-count'),
  /* --- Chrome-расширения встроенного браузера ---
       Реестр и загрузка живут в главном процессе (desktop/extensions.js);
       страница /browser только показывает список и шлёт команды. */
  extList: () => ipcRenderer.invoke('ext-list'),
  extAddFolder: () => ipcRenderer.invoke('ext-add-folder'),
  extAddCrx: () => ipcRenderer.invoke('ext-add-crx'),
  extToggle: (key) => ipcRenderer.invoke('ext-toggle', String(key || '')),
  extRemove: (key) => ipcRenderer.invoke('ext-remove', String(key || '')),

  /* --- свои плагины браузера (не Chrome-расширения) ---
       Манифест plugin.json, скрипт и CSS внедряются в страницу вкладки.
       Реестр — data\browser_plugins, движок — desktop/plugins.js. */
  pluginsList: () => ipcRenderer.invoke('plugins-list'),
  pluginsAddFolder: () => ipcRenderer.invoke('plugins-add-folder'),
  pluginsAddArchive: () => ipcRenderer.invoke('plugins-add-archive'),
  pluginsToggle: (key) => ipcRenderer.invoke('plugins-toggle', String(key || '')),
  pluginsRemove: (key) => ipcRenderer.invoke('plugins-remove', String(key || '')),
  pluginsExample: () => ipcRenderer.invoke('plugins-create-example'),
  pluginsOpenDir: () => ipcRenderer.invoke('plugins-open-dir'),
  /* Отправить текст/ссылку из браузера в палитру ИИ (откроется Shift+Space-окно
     с уже подставленным запросом). */
  askAiBar: (text) => ipcRenderer.send('ai-bar-submit-external', { text: String(text || '') }),

  /* --- палитра быстрого запроса к ИИ (Shift+Space) ---
     Основное окно вызывает onAiBarHotkey, когда горячая клавиша нажата
     при развёрнутом приложении: оверлей тогда рисует сама страница,
     отдельное окно не нужно. closeAiBar — наоборот, из /ai-bar. */
  onAiBarHotkey: (cb) => {
    const listener = (_e, payload) => { try { cb(payload || {}); } catch (err) { console.error('[electronAPI] ai-bar-hotkey', err); } };
    ipcRenderer.on('ai-bar-hotkey', listener);
    return () => ipcRenderer.removeListener('ai-bar-hotkey', listener);
  },
  onAiBarSubmit: (cb) => {
    const listener = (_e, text) => { try { cb(text); } catch (err) { console.error('[electronAPI] ai-bar-submit', err); } };
    ipcRenderer.on('ai-bar-submit', listener);
    return () => ipcRenderer.removeListener('ai-bar-submit', listener);
  },
  closeAiBar: () => ipcRenderer.send('ai-bar-close'),
  /* Сообщаем оболочке, что агент думает: пока идёт ответ, окно палитры
     не должно прятаться по потере фокуса. */
  sendBusy: (v) => ipcRenderer.send('ai-bar-busy', !!v),
  /* Кнопка «закрепить»: то же право не прятаться по потере фокуса. */
  sendPin: (v) => ipcRenderer.send('ai-bar-pin', !!v),
  /* Ручной размер окна палитры: рам-ки нет, размер тянут за уголок в странице. */
  resizeAiBar: (w, h) => ipcRenderer.send('ai-bar-resize', { w, h }),

  /* --- 3D-аватар отдельным прозрачным окном ---
     Окно живёт независимо от палитры: кнопка в шапке мини-приложения только
     просит показать/спрятать, а настройки (размер, прозрачность) живут в
     самом окне аватара — они доступны, даже когда палитра закрыта. */
  toggleAvatar: () => ipcRenderer.send('avatar-toggle'),
  showAvatar: () => ipcRenderer.send('avatar-show'),
  hideAvatar: () => ipcRenderer.send('avatar-hide'),
  resizeAvatar: (w, h) => ipcRenderer.send('avatar-resize', { w, h }),
  setAvatarOpacity: (v) => ipcRenderer.send('avatar-opacity', v),
  isAvatarVisible: () => ipcRenderer.invoke('avatar-visible'),

  /* --- рабочий стол Windows ---
     Ярлык (.lnk) создаётся в главном процессе: там известен настоящий
     исполняемый файл (в сборке — InfinitySearch.exe, при разработке —
     electron.exe) и путь к рабочему столу. Страница только просит. */
  createDesktopShortcut: (payload) => ipcRenderer.invoke('desktop-create-shortcut', payload || {}),

  /* Отдельное окно Electron с приложением (меню «Открыть отдельно»).
     Главный процесс принимает только http(s) на локальный сервер — внешние
     адреса страница и так может открыть через window.open. */
  openAppWindow: (payload) => ipcRenderer.invoke('desktop-open-window', payload || {}),

  /* Снимок области рабочего стола для превью окон на панели задач.
     Прямоугольник — в CSS-пикселях; возвращается data:image/jpeg;base64. */
  captureDesktopRect: (rect) => ipcRenderer.invoke('desktop-capture-rect', rect || {}),

  /* Возвращаем функцию отписки, чтобы вызывающий код освободил обработчик —
     иначе при перерисовке накапливаются копии. */
  on: (channel, cb) => {
    if (!CHANNELS.includes(channel)) {
      console.warn('[electronAPI] неизвестный канал:', channel);
      return () => {};
    }
    const listener = (_e, payload) => { try { cb(payload); } catch (err) {
      console.error('[electronAPI]', channel, err);
    } };
    ipcRenderer.on(channel, listener);
    return () => ipcRenderer.removeListener(channel, listener);
  },
};

contextBridge.exposeInMainWorld('electronAPI', api);
