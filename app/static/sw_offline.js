/* Офлайн-оболочка страниц модулей. Отдельный service worker, а не
   /static/sw.js: тот обслуживает push и имеет cache-first на всё подряд,
   а его scope — /static/, поэтому страницами он не управляет вовсе. Этот
   файл отдаётся с корня, поэтому может взять scope '/', но вмешивается
   ровно в несколько путей и только когда сети нет. */

/* v2: в v1 в Cache API попадали ответы вместе с заголовком Content-Encoding:
   gzip. Fetch внутри воркера отдаёт УЖЕ распакованное тело, поэтому при
   повторной выдаче из кэша браузер распаковывал его второй раз — /notes,
   /news, /books и /social/messages открывались как бинарный мусор или
   получали 400 от сервера. Новая версия чистит заголовки кодирования перед
   записью, а старые копии удаляются на activate. */
const CACHE = 'infinity-offline-v2';
/* Оболочки страниц с локальной копией: /news, /notes, /books, /social/messages.
   Держим список здесь, а не в каждой странице: воркер один на весь origin, и
   «какие страницы офлайнятся» должно читаться в одном месте. */
const SHELLS = ['/news', '/notes', '/books', '/social/messages'];
/* Страницы тянут свои скрипты/стили из /static. Если оставить их
   на произвол HTTP-кэша, офлайн они не соберутся: сервер отдаёт эти файлы с
   Cache-Control: no-cache, а «no-cache» без сети означает «перегрузи и не
   получи ничего» — код страницы не выполнится и локальную копию не прочитает.
   Поэтому и они идут через сеть, а в кэш ложатся только как запасной вариант. */
const ASSETS = ['/static/js/', '/static/css/'];
/* Заголовки, описывающие транспортное кодирование. После fetch тело уже
   распаковано, и оставлять их в кэше нельзя. */
const TRANSPORT_HEADERS = ['content-encoding', 'content-length', 'transfer-encoding'];

function isMine(pathname) {
  return SHELLS.includes(pathname) || ASSETS.some(p => pathname.startsWith(p));
}

function sanitize(res) {
  const headers = new Headers(res.headers);
  TRANSPORT_HEADERS.forEach(h => headers.delete(h));
  /* Vary ссылается на заголовки исходного запроса; для кэш-копии он только
     мешает попаданию, а содержимое уже зафиксировано. */
  headers.delete('vary');
  return headers;
}

async function putSanitized(request, response) {
  try {
    if (!response || response.status !== 200) return;
    /* Не буферизуем тело в blob: Response с тем же потоком позволяет браузеру
       отдавать страницу и параллельно писать копию в кэш. */
    const clean = new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: sanitize(response),
    });
    const cache = await caches.open(CACHE);
    await cache.put(request, clean);
  } catch (e) { /* квота/неподдерживаемый ответ — кэш просто не обновится */ }
}

async function purgeAll() {
  try { await caches.delete(CACHE); } catch (e) { /* нет caches — не критично */ }
}

/* Что показать, когда ни сети, ни кэша нет. Голый текст «offline» браузер
   рисует как пустую страницу без объяснения, и пользователь решит, что
   приложение сломалось. */
const NO_COPY_PAGE = '<!doctype html><html lang="ru"><meta charset="utf-8">'
  + '<meta name="viewport" content="width=device-width,initial-scale=1">'
  + '<title>ПК недоступен</title>'
  + '<body style="font:15px/1.5 system-ui,sans-serif;margin:24px;color:#e8e8ea;'
  + 'background:#111"><h2 style="font-size:17px">Локальной копии этой страницы нет</h2>'
  + '<p>Центральный ПК недоступен, а эта страница ещё не успела сохраниться в '
  + 'браузер. Откройте её один раз при включённом ПК — оболочка и данные лягут '
  + 'в локальный кэш, и дальше страница откроется без сети.</p>';

self.addEventListener('install', (e) => { self.skipWaiting(); });

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    /* Только свои прошлые версии: воркер теперь регистрируется ещё и на
       телефоне ради установки PWA и работает рядом с /static/sw.js, чей
       кэш соцсетей и push удалять нельзя. */
    const keys = await caches.keys();
    await Promise.all(keys
      .filter(k => k.indexOf('infinity-offline') === 0 && k !== CACHE)
      .map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('message', (e) => {
  if ((e.data || {}).action === 'offline-purge') e.waitUntil(purgeAll());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  let u;
  try { u = new URL(req.url); } catch (err) { return; }
  if (u.origin !== self.location.origin) return;
  if (!isMine(u.pathname)) return;
  if (req.headers.get('range')) return;   // диапазонные запросы не кэшируем
  /* API сюда не попадает: он под другим путём и остаётся сетевым. Кэш только
     оболочка и статика, и всегда после неудачной попытки сети — данные
     устаревшими не становятся. */
  e.respondWith(
    fetch(req).then(res => {
      /* Сначала отдаём ответ странице, кэш обновляем параллельно: clone()
         разветвляет поток, поэтому страница не ждёт записи в кэш. */
      putSanitized(req, res.clone());
      return res;
    }).catch(async () => {
      const cached = await caches.match(req, { ignoreSearch: true });
      if (cached) return cached;
      /* Подсказку только для навигации: скрипту, который ждёт JSON или JS,
         HTML-страница хуже голой ошибки — он разберёт её как данные. */
      if (req.mode === 'navigate' || req.destination === 'document') {
        return new Response(NO_COPY_PAGE, {
          status: 503,
          headers: {'Content-Type': 'text/html; charset=utf-8'}
        });
      }
      return new Response('offline', { status: 503 });
    })
  );
});
