/* Версия bumped: старая кэш-хранилище содержало код приложения, отданный
   cache-first без проверки с сервера (см. fetch ниже). Пока не вычистить
   старое хранилище, браузеры, открывавшие медиа или сообщения, продолжали
   бы брать старый file_manager.js. */
const CACHE_NAME = 'infinity-social-v4';
const STATIC_ASSETS = [
  '/static/css/font-awesome.min.css',
  '/static/fonts/google-fonts.css',
  '/static/css/social-theme.css',
  '/static/libs/simplepeer.min.js',
  '/static/js/qrcode.min.js'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = e.request.url;
  if (url.includes('/api/media/') || url.includes('/api/social/file/')) {
    return;
  }
  /* API — ВСЕГДА из сети. Кэш тут означал бы, что страница видит устаревшие
     данные: история агента, список его сессий, список личностей, профили.
     Так и случилось: первый ответ (пустой, потому что агент ещё не
     поднялся) закешировался, и палитра Shift+Space до сих пор показывала
     пустую переписку вместо прежней. */
  if (url.includes('/api/')) {
    return;
  }
  /* Код приложения — вне кэша офлайн-стратегии.
     Ниже стоит cache-first («отдать из кэша, если есть»), и для кода это
     означало бы, что file_manager.js или file_manager.css никогда не
     обновляются: правка файла на сервере не доходила до клиента, пока он не
     почистит кэш вручную. Именно так в Electron пропадали корзина и выделение
     рамкой. Для статических js/css сервер отдаёт Cache-Control: no-cache,
     поэтому пусть их обслуживает обычный HTTP-кэш с проверкой на сервере. */
  if (url.includes('/static/js/') || url.includes('/static/css/')) {
    return;
  }
  e.respondWith(
    caches.match(e.request).then(cached => {
      const network = fetch(e.request).then(resp => {
        if (resp && resp.status === 200 && e.request.method === 'GET') {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));
        }
        return resp;
      }).catch(() => cached);
      return cached || network;
    })
  );
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  let url = e.notification.data?.url || '/';
  if (e.action === 'view') url = e.notification.data?.url || '/social/feed';
  if (e.action === 'reply') url = '/social/messages';
  e.waitUntil(clients.openWindow(url));
});

self.addEventListener('push', (e) => {
  if (!e.data) return;
  try {
    const data = e.data.json();
    const title = data.title || 'Infinity Social';
    const options = {
      body: data.body || '',
      icon: data.icon || '/favicon.jpg',
      badge: '/favicon.jpg',
      tag: data.tag || 'social',
      data: { url: data.url || '/social/feed' },
      actions: [
        { action: 'view', title: 'Открыть' },
        { action: 'reply', title: 'Ответить' }
      ],
      vibrate: [200, 100, 200]
    };
    e.waitUntil(self.registration.showNotification(title, options));
  } catch (err) {
    console.warn('Push error:', err);
  }
});
