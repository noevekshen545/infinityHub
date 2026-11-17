/* ============================================================================
   Infinity Media — Neo enhancement layer
   ----------------------------------------------------------------------------
   Основной дизайн страницы медиа (/media, /torrents). Не переопределяет
   бизнес-логику: все действия делегируются существующим глобальным функциям
   страницы (showSection, playerTogglePlay, playerNext, playerPrev,
   toggleShuffle, toggleFavPlayer, openGlobalSearch и т.д.). Классическая
   вёрстка лежит в templates_backup/page_media.html (/media/classic).
   ========================================================================== */
(function () {
  'use strict';

  var root = document.documentElement;
  if (!root.classList.contains('media-neo')) return;

  var isMobile = root.classList.contains('mneo-mobile');
  var $ = function (sel, ctx) { return (ctx || document).querySelector(sel); };
  var $$ = function (sel, ctx) {
    return Array.prototype.slice.call((ctx || document).querySelectorAll(sel));
  };

  function on(el, ev, fn, opts) {
    if (el) el.addEventListener(ev, fn, opts || false);
  }

  function call(name) {
    try {
      if (typeof window[name] === 'function') return window[name].apply(window, Array.prototype.slice.call(arguments, 1));
    } catch (e) { /* не критично */ }
    return undefined;
  }

  /* ==========================================================================
     1. Тема приложения
     ========================================================================== */

  /* В page_media.html есть собственный body.dark, который конфликтует с
     палитрой theme.js (у неё другой тёмный фон и акценты). Убираем его:
     цвета задаёт InfinityTheme инлайном на <html>. */
  function stripLegacyDark() {
    if (document.body) document.body.classList.remove('dark');
  }
  stripLegacyDark();

  if (window.InfinityTheme) {
    try { window.InfinityTheme.init(); } catch (e) { /* */ }
  }

  if (document.body && typeof MutationObserver === 'function') {
    try {
      new MutationObserver(function () {
        if (document.body.classList.contains('dark')) {
          document.body.classList.remove('dark');
        }
      }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    } catch (e) { /* */ }
  }

  function syncThemeIcon(theme) {
    var icon = document.getElementById('themeIcon');
    if (!icon) return;
    var dark = theme ? theme === 'dark' : (window.InfinityTheme ? window.InfinityTheme.get() === 'dark' : false);
    icon.className = dark ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
  }

  /* Кнопка «Сменить тему» на странице вызывает глобальный toggleTheme().
     Переопределяем его на единый переключатель приложения. */
  window.toggleTheme = function () {
    if (window.InfinityTheme) {
      var t = window.InfinityTheme.toggle();
      syncThemeIcon(t);
      return;
    }
    var dark = document.body.classList.toggle('dark');
    syncThemeIcon(dark ? 'dark' : 'light');
  };

  if (window.InfinityTheme) {
    try { window.InfinityTheme.onChange(syncThemeIcon); } catch (e) { /* */ }
    syncThemeIcon();
  }

  /* ==========================================================================
     2. Брендинг и переключатель дизайна
     ========================================================================== */

  var toolbar = $('.toolbar');

  function buildBrand() {
    if (!toolbar || $('.mneo-brand', toolbar)) return;
    var brand = document.createElement('div');
    brand.className = 'mneo-brand';
    brand.setAttribute('aria-hidden', 'true');
    brand.innerHTML =
      '<div class="mneo-brand-mark"><i class="fa-solid fa-wave-square"></i></div>' +
      '<div class="mneo-brand-copy"><b>Infinity</b><small>Media</small></div>';
    toolbar.insertBefore(brand, toolbar.firstChild);
  }

  /* Группируем разделы в аккуратную «капсулу» и превращаем иконку поиска
     в полноценную командную строку. Только десктоп: на телефоне навигацию
     заменяет нижняя панель. */
  function buildToolbarExtras() {
    if (!toolbar || isMobile) return;

    if (!$('.mneo-nav-group', toolbar)) {
      var group = document.createElement('div');
      group.className = 'mneo-nav-group';

      /* «Главная» — первая кнопка капсулы, а не отдельный элемент слева. */
      var home = $('.toolbar > a.btn[href="/"]', toolbar);
      var homeAnchor = home ? home.nextSibling : null;
      if (home) {
        if (!home.getAttribute('aria-label')) {
          home.setAttribute('aria-label', home.title || 'Главная');
        }
        group.appendChild(home);
      }

      ['music', 'movies', 'series', 'photos', 'continue'].forEach(function (id) {
        var btn = null;
        Array.prototype.forEach.call(toolbar.children, function (ch) {
          if (!btn && ch.getAttribute && ch.getAttribute('data-section') === id) btn = ch;
        });
        if (!btn) return;
        /* Кнопки в группе показывают только иконку — подпись уходит в
           title/aria-label, иначе разделы не различить на глаз. */
        var label = $('span:not(.badge)', btn);
        if (label) {
          var text = (label.textContent || '').trim();
          if (text) {
            btn.title = text;
            btn.setAttribute('aria-label', text);
          }
        }
        group.appendChild(btn);
      });
      if (group.children.length) {
        if (homeAnchor && homeAnchor.parentNode === toolbar) {
          toolbar.insertBefore(group, homeAnchor);
        } else {
          toolbar.appendChild(group);
        }
      }
    }

    Array.prototype.forEach.call(toolbar.children, function (ch) {
      if (ch.classList && ch.classList.contains('btn-icon') &&
          (ch.getAttribute('onclick') || '').indexOf('openGlobalSearch') !== -1) {
        ch.classList.add('mneo-search-original');
      }
    });

    if (!$('.mneo-cmd', toolbar)) {
      var cmd = document.createElement('button');
      cmd.type = 'button';
      cmd.className = 'mneo-cmd';
      cmd.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i>' +
        '<span>Поиск по медиа</span><kbd>⌘K</kbd>';
      on(cmd, 'click', function () { call('openGlobalSearch'); });
      var more = $('details.more', toolbar);
      if (more) toolbar.insertBefore(cmd, more);
      else toolbar.appendChild(cmd);
    }
  }

  buildBrand();
  buildToolbarExtras();

  /* ==========================================================================
     3. Скролл верхней панели
     ========================================================================== */

  if (toolbar) {
    var onScroll = function () {
      toolbar.classList.toggle('mneo-scrolled', (window.scrollY || window.pageYOffset || 0) > 24);
    };
    on(window, 'scroll', onScroll, { passive: true });
    onScroll();
  }

  /* ==========================================================================
     4. Hero-баннеры с реальными постерами
     ========================================================================== */

  var HERO_IDS = ['music', 'movies', 'series', 'photos', 'continue'];

  function heroEl(id) {
    return document.querySelector('#' + id + ' > .page-header');
  }

  function pickPoster(kind) {
    try {
      if (kind === 'music' && typeof musicIndex !== 'undefined' && musicIndex && musicIndex.tracks) {
        var t = musicIndex.tracks.filter(function (x) { return x && x.has_cover; })[0];
        if (t) return '/api/media/cover/' + encodeURIComponent(t.id);
      }
      if (kind === 'photos' && typeof photoIndex !== 'undefined' && photoIndex && photoIndex.photos) {
        var p = photoIndex.photos.filter(function (x) { return x && x.id; })[0];
        if (p) return (p.has_thumbnail ? '/api/photos/thumb/' : '/api/photos/file/') + encodeURIComponent(p.id);
      }
      if (typeof videoIndex !== 'undefined' && videoIndex && videoIndex.videos) {
        var videos = videoIndex.videos;
        var pool = videos.filter(function (v) {
          if (!v) return false;
          if (kind === 'series') return !!v._is_series;
          if (kind === 'photos' || kind === 'music') return false;
          return !v._is_series;
        });
        if (!pool.length) pool = videos;
        var v2 = pool.filter(function (v) { return v && (v.backdrop_url || v.poster_url); })[0];
        if (v2) return v2.backdrop_url || v2.poster_url || '';
      }
    } catch (e) { /* данные ещё не загрузились */ }
    return '';
  }

  function applyHero(id) {
    var el = heroEl(id);
    if (!el || !el.style) return;
    /* Убираем эмодзи из крупного заголовка: на hero-баннере он выглядит
       случайным символом, раздел и так понятен по навигации. */
    try {
      var h1 = el.querySelector('h1');
      if (h1 && !h1.dataset.mneoClean) {
        h1.dataset.mneoClean = '1';
        h1.textContent = (h1.textContent || '').replace(/^[^\p{L}\p{N}]+/u, '');
      }
    } catch (e) { /* старый браузер без \p{...} — оставляем как есть */ }
    if (el.dataset.mneoHero === '1') return;
    /* На hero с цветомузыкой постер не нужен: фон — canvas-визуализатор. */
    if (el.classList.contains('mneo-hero-viz-on')) {
      el.dataset.mneoHero = '1';
      el.classList.add('mneo-has-photo');
      return;
    }
    var url = pickPoster(id);
    if (!url) return;
    el.dataset.mneoHero = '1';
    el.classList.add('mneo-has-photo');
    el.style.backgroundImage =
      'linear-gradient(120deg, rgba(6,9,17,.55) 0%, rgba(6,9,17,.18) 55%, rgba(6,9,17,.42) 100%), url("' +
      String(url).replace(/"/g, '%22') + '")';
    el.style.backgroundSize = 'cover';
    el.style.backgroundPosition = 'center';
  }

  function applyAllHeroes() {
    HERO_IDS.forEach(applyHero);
  }

  applyAllHeroes();
  [700, 1800, 3500, 7000, 12000].forEach(function (ms) {
    setTimeout(applyAllHeroes, ms);
  });

  /* ==========================================================================
     4b. Богатый desktop-hero: обложки, CTA и быстрые показатели
     ========================================================================== */

  /* Счётчики карточек — числа из базы, а не из загруженного куска списка.
     Список приходит по 100 за раз, поэтому «Треков 100» при 195 в базе было бы
     враньём, а исполнители и альбомы считались бы только по первой сотне.
     /api/media/statistics прогретый стоит 9-19 мс, но первым ударом — 206 мс,
     поэтому запрос идёт после отрисовки и пересобирает даш, когда приходит. */
  var mneoDbStats = null;
  var mneoDbStatsTs = 0;

  function fetchMusicStats() {
    if (mneoDbStats && (Date.now() - mneoDbStatsTs) < 20000) return;
    fetch('/api/media/statistics').then(function (r) { return r.json(); }).then(function (d) {
      mneoDbStats = (d && d.music) || null;
      mneoDbStatsTs = Date.now();
      if (mneoDbStats) { try { buildDash('music'); } catch (e) { /* даш пересоберётся сам */ } }
    }).catch(function () { /* остаёмся на подсчёте по загруженному списку */ });
  }

  /* Ключ группы — зеркало серверного media_module._group_key. Нужен только в
     подсчёте вслепую (когда /statistics не ответил): иначе запасные числа
     считают «ЛЮБЭ» и «Любэ» двумя артистами, а сервер — одним. */
  function groupKey(value) {
    var s = String(value == null ? '' : value).replace(/\s+/g, ' ').trim();
    return s.replace(/[ ,;.&\u2013\u2014-]+$/, '').toLowerCase();
  }

  function musicStats() {
    try {
      var tracks = (musicIndex && musicIndex.tracks) || [];
      var artists = {}, albums = {};
      tracks.forEach(function (t) {
        if (!t) return;
        var ak = groupKey(t.artist);
        if (ak) artists[ak] = 1;
        var bk = groupKey(t.album);
        /* Заползший в теги «Неизвестный альбом» сервер считает отдельным
           альбомом — здесь та же граница, иначе запасные числа разойдутся
           со статистикой и с карточками в сетке. */
        if (bk) albums[bk + '|' + ak] = 1;
      });
      var fav = 0;
      try { fav = ((favorites && favorites.tracks) || []).length; } catch (e) { fav = 0; }
      if (mneoDbStats) {
        return {
          tracks: mneoDbStats.total_tracks || 0,
          artists: mneoDbStats.artists || 0,
          albums: mneoDbStats.albums || 0,
          favorites: fav
        };
      }
      return { tracks: tracks.length, artists: Object.keys(artists).length, albums: Object.keys(albums).length, favorites: fav };
    } catch (e) { return { tracks: 0, artists: 0, albums: 0, favorites: 0 }; }
  }

  function coverList(kind, limit) {
    var out = [];
    try {
      if (kind === 'music' && typeof musicIndex !== 'undefined' && musicIndex) {
        (musicIndex.tracks || []).forEach(function (t) {
          if (out.length < limit && t && t.has_cover) out.push('/api/media/cover/' + encodeURIComponent(t.id));
        });
      } else if (kind === 'photos' && typeof photoIndex !== 'undefined' && photoIndex) {
        (photoIndex.photos || []).forEach(function (p) {
          if (out.length < limit && p && p.id) out.push((p.has_thumbnail ? '/api/photos/thumb/' : '/api/photos/file/') + encodeURIComponent(p.id));
        });
      } else if (typeof videoIndex !== 'undefined' && videoIndex) {
        (videoIndex.videos || []).forEach(function (v) {
          if (out.length >= limit || !v) return;
          var match = kind === 'series' ? !!v._is_series : !v._is_series;
          if (!match) return;
          var u = v.backdrop_url || v.poster_url;
          if (u) out.push(u);
        });
      }
    } catch (e) { /* */ }
    return out;
  }

  /* Топ прослушиваний для hero-обложек: сервер уже отдаёт play_count и
     has_cover, поэтому порядок «чаще всего слушаемый с обложкой, дальше по
     убыванию» считан на выдаче. has_cover строки — главный признак; локальный
     индекс сверяем только если статистика пришла без этого поля (старый кеш). */
  var heroTopCache = null;
  var heroTopPromise = null;

  function topPlayedTracks() {
    if (heroTopCache) return Promise.resolve(heroTopCache);
    if (heroTopPromise) return heroTopPromise;
    heroTopPromise = fetch('/api/media/statistics', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) {
        var list = (((s || {}).music || {}).top_tracks) || [];
        var byId = {};
        try {
          ((typeof musicIndex !== 'undefined' && musicIndex && musicIndex.tracks) || []).forEach(function (t) {
            if (t && t.id) byId[t.id] = t;
          });
        } catch (e) { /* */ }

        var seen = {};
        var out = [];
        list.forEach(function (t) {
          if (!t || !t.id || seen[t.id] || !(t.play_count > 0)) return;
          seen[t.id] = 1;
          if (t.has_cover === undefined) {
            var known = byId[t.id];
            if (known && !known.has_cover) return;
          } else if (!t.has_cover) {
            return;
          }
          out.push(t);
        });
        heroTopCache = out;
        return out;
      })
      .catch(function () { heroTopCache = []; return []; });
    return heroTopPromise;
  }

  function upgradeMusicHeroArt(art) {
    topPlayedTracks().then(function (tops) {
      if (!art || !art.isConnected) return;

      var tracks = [];
      try {
        tracks = ((typeof musicIndex !== 'undefined' && musicIndex && musicIndex.tracks) || []);
      } catch (e) { /* */ }
      var withCover = tracks.filter(function (t) { return t && t.id && t.has_cover; });

      var queue = [];
      var seen = {};
      function push(t) {
        if (!t || !t.id || seen[t.id]) return;
        seen[t.id] = 1;
        queue.push({ id: t.id, src: '/api/media/cover/' + encodeURIComponent(t.id) });
      }

      (tops || []).forEach(push);

      if (!queue.length) {
        /* Истории прослушиваний ещё нет — ведём себя как раньше: обложки из индекса. */
        if (art.dataset.mneoFallbackDone) return;
        withCover.slice(0, 12).forEach(push);
        if (!queue.length) return;
        art.dataset.mneoFallbackDone = '1';
      } else {
        if (art.dataset.mneoTopId === queue[0].id) return;
        art.dataset.mneoTopId = queue[0].id;
        art.classList.add('mneo-hero-top');
        /* Запасной вариант, если файл обложки топового трека пропал. */
        withCover.slice(0, 6).forEach(push);
      }
      queue = queue.slice(0, 16);

      var used = {};
      function pick(from) {
        for (var i = from; i < queue.length; i++) {
          if (used[queue[i].src]) continue;
          used[queue[i].src] = 1;
          return i;
        }
        return -1;
      }
      function attach(img, idx) {
        if (!img || idx < 0) return;
        var item = queue[idx];
        img.onload = function () {
          if (img.parentNode && img.parentNode.classList.contains('mneo-art-main')) {
            art.dataset.mneoTrackId = item.id;
          }
        };
        img.onerror = function () {
          img.onerror = null;
          attach(img, pick(idx + 1));
        };
        img.src = item.src;
      }

      var mainIdx = pick(0);
      if (mainIdx < 0) return;

      /* Hero мог быть собран без локальных обложек — достраиваем задние карточки. */
      var mainCard = $('.mneo-art-main', art);
      if (mainCard && queue.length > 1 && !$('.mneo-art-back-1', art)) {
        var back2 = document.createElement('div');
        back2.className = 'mneo-art-card mneo-art-back-2';
        back2.innerHTML = '<img alt="">';
        var back1 = document.createElement('div');
        back1.className = 'mneo-art-card mneo-art-back-1';
        back1.innerHTML = '<img alt="">';
        art.insertBefore(back2, mainCard);
        art.insertBefore(back1, mainCard);
      }

      attach($('.mneo-art-main img', art), mainIdx);
      attach($('.mneo-art-back-1 img', art), pick(0));
      attach($('.mneo-art-back-2 img', art), pick(0));
    });
  }

  function buildHeroArt(kind) {
    if (isMobile) return;
    var header = heroEl(kind);
    if (!header) return;

    var existing = header.querySelector('.mneo-hero-art');
    if (existing) {
      if (kind === 'music') upgradeMusicHeroArt(existing);
      return;
    }

    var covers = coverList(kind, 3);
    /* Для музыки обложки могут прийти только из статистики — не отказываемся
       от hero-art, если локальный индекс ещё не отдал has_cover. */
    if (!covers.length && kind !== 'music') return;

    header.classList.add('mneo-hero-rich');

    var c0 = covers[0], c1 = covers[1], c2 = covers[2];
    function src(u) { return u ? ' src="' + u.replace(/"/g, '&quot;') + '"' : ''; }

    var art = document.createElement('div');
    art.className = 'mneo-hero-art';
    var html = '<div class="mneo-art-glow"></div>';
    if (c1) html += '<div class="mneo-art-card mneo-art-back-2"><img alt=""' + src(c1) + '></div>';
    if (c0) html += '<div class="mneo-art-card mneo-art-back-1"><img alt=""' + src(c2 || c0) + '></div>';
    html += '<div class="mneo-art-card mneo-art-main">' +
      '<img alt=""' + src(c0) + '>' +
      '<span class="mneo-art-shade"></span>' +
      '<span class="mneo-art-play"><i class="fa-solid fa-play"></i></span>' +
      '</div>';
    art.innerHTML = html;

    var chip = document.createElement('div');
    chip.className = 'mneo-art-chip';
    chip.innerHTML = kind === 'photos'
      ? '<i class="fa-solid fa-image"></i> Коллекция'
      : (kind === 'series' ? '<i class="fa-solid fa-tv"></i> Сериалы' : '<i class="fa-solid fa-compact-disc"></i> Коллекция');
    art.appendChild(chip);

    header.appendChild(art);

    if (kind === 'music') {
      on($('.mneo-art-main', art), 'click', function () {
        var id = art.dataset.mneoTrackId;
        var tracks = (musicIndex.tracks || []);
        if (id) {
          var idx = tracks.findIndex(function (t) { return t && t.id === id; });
          call('playTrack', id, idx >= 0 ? idx : 0);
          return;
        }
        var t = tracks[0];
        if (t) call('playTrack', t.id, 0);
      });
      upgradeMusicHeroArt(art);
    } else if (kind === 'photos') {
      on($('.mneo-art-main', art), 'click', function () {
        var p = (photoIndex.photos || [])[0];
        if (p) call('openPhotoDetail', p.id, 0);
      });
    }

    buildHeroViz(header);
  }

  /* ==========================================================================
     4c. Hero-цветомузыка: вместо фонового постера — canvas, реагирующий на
     спектр играющего трека. Отдельный AudioContext не создаём: страница уже
     подключила <audio> к эквалайзеру, второй createMediaElementSource упал бы.
     Используем её глобальный analyser, а без музыки рисуем спокойный дрейф.
     ========================================================================== */

  var vizState = {
    heroes: [],
    raf: null,
    last: 0,
    data: null,
    palette: null,
    reduced: !!(window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  };

  function vizColors() {
    if (vizState.palette) return vizState.palette;
    var cs = getComputedStyle(document.documentElement);
    vizState.palette = {
      a: (cs.getPropertyValue('--accent') || '').trim() || '#4f46e5',
      b: (cs.getPropertyValue('--accent2') || '').trim() || '#4338ca'
    };
    return vizState.palette;
  }

  if (window.InfinityTheme) {
    try {
      window.InfinityTheme.onChange(function () { vizState.palette = null; });
    } catch (e) { /* */ }
  }

  function vizResize(hero) {
    var rect = hero.el.getBoundingClientRect();
    if (rect.width < 60 || rect.height < 60) return false;
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    if (hero.w === Math.round(rect.width) && hero.h === Math.round(rect.height) &&
        hero.dpr === dpr) return true;
    hero.w = rect.width;
    hero.h = rect.height;
    hero.dpr = dpr;
    hero.cv.width = Math.round(rect.width * dpr);
    hero.cv.height = Math.round(rect.height * dpr);
    hero.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return true;
  }

  function vizRoundRect(ctx, x, y, w, h, r) {
    if (typeof ctx.roundRect === 'function') {
      ctx.beginPath();
      ctx.roundRect(x, y, w, h, r);
      ctx.fill();
    } else {
      ctx.fillRect(x, y, w, h);
    }
  }

  function drawHeroViz(hero, data, t) {
    var ctx = hero.ctx;
    if (!ctx || !hero.w || !hero.h) return;
    var w = hero.w, h = hero.h;
    var P = vizColors();
    var bass = 0, mid = 0, high = 0;
    var i;

    if (data && data.length) {
      for (i = 1; i < Math.min(9, data.length); i++) bass += data[i];
      for (i = 9; i < Math.min(41, data.length); i++) mid += data[i];
      for (i = 41; i < Math.min(111, data.length); i++) high += data[i];
      bass = bass / (Math.min(9, data.length) - 1) / 255;
      mid = mid / (Math.min(41, data.length) - 9) / 255;
      high = high / (Math.min(111, data.length) - 41) / 255;
    }

    hero.bass += (bass - hero.bass) * .25;
    hero.mid += (mid - hero.mid) * .2;
    hero.high += (high - hero.high) * .18;
    var level = Math.max(hero.bass, hero.mid * .85, hero.high * .7);

    ctx.clearRect(0, 0, w, h);
    ctx.globalCompositeOperation = 'lighter';

    /* Цветные пятна: медленно дрейфуют, на бас/середину/верх реагируют
       размером и яркостью. */
    [ { x: .18, y: .42, c: P.a, l: hero.bass, ph: 0 },
      { x: .55, y: .30, c: P.b, l: hero.mid, ph: 2.1 },
      { x: .84, y: .58, c: P.a, l: hero.high, ph: 4.2 }
    ].forEach(function (blob) {
      var cx = w * (blob.x + Math.sin(t * .33 + blob.ph) * .05);
      var cy = h * (blob.y + Math.cos(t * .27 + blob.ph) * .16);
      var rad = h * (.5 + blob.l * .8) * (1 + Math.sin(t * .5 + blob.ph) * .06);
      var grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.max(rad, 1));
      grad.addColorStop(0, blob.c);
      grad.addColorStop(.55, blob.c);
      grad.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.globalAlpha = .07 + blob.l * .22;
      ctx.fillStyle = grad;
      /* Заливаем только габарит пятна, а не весь canvas — так дешевле. */
      ctx.fillRect(cx - rad, cy - rad, rad * 2, rad * 2);
    });

    /* Эквалайзер вдоль низа баннера. */
    var bars = Math.max(26, Math.min(60, Math.floor(w / 30)));
    var pad = Math.max(14, w * .022);
    var bw = (w - pad * 2) / bars;
    var usable = data && data.length ? Math.min(data.length, 112) : 0;
    var barGrad = ctx.createLinearGradient(0, h * .48, 0, h);
    barGrad.addColorStop(0, P.a);
    barGrad.addColorStop(1, P.b);
    ctx.fillStyle = barGrad;
    for (var b = 0; b < bars; b++) {
      var v = 0;
      if (usable) {
        var bin = Math.floor(Math.pow(b / bars, 1.55) * (usable - 1));
        v = (data[bin] || 0) / 255;
      }
      var idle = .05 + .045 * Math.sin(t * 1.3 + b * .42);
      v = Math.max(idle, Math.pow(v, 1.65) * (.35 + level * 1.05));
      var bh = Math.max(2, v * h * .38);
      ctx.globalAlpha = Math.min(.7, .1 + v * .55);
      vizRoundRect(ctx, pad + b * bw + bw * .16, h - bh - 8,
                   bw * .68, bh, Math.min(bw * .34, 6));
    }

    /* Тёмная вуаль слева-снизу, чтобы заголовок и кнопки читались на цвете. */
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    var scrim = ctx.createLinearGradient(0, 0, w, 0);
    scrim.addColorStop(0, 'rgba(4,7,14,.52)');
    scrim.addColorStop(.5, 'rgba(4,7,14,.22)');
    scrim.addColorStop(1, 'rgba(4,7,14,.04)');
    ctx.fillStyle = scrim;
    ctx.fillRect(0, 0, w, h);
    var bottom = ctx.createLinearGradient(0, h * .55, 0, h);
    bottom.addColorStop(0, 'rgba(4,7,14,0)');
    bottom.addColorStop(1, 'rgba(4,7,14,.34)');
    ctx.fillStyle = bottom;
    ctx.fillRect(0, 0, w, h);
  }

  function vizFrame(ts) {
    vizState.raf = requestAnimationFrame(vizFrame);
    if (ts - vizState.last < 32) return;
    vizState.last = ts;

    var an = null;
    try {
      if (typeof analyser !== 'undefined' && analyser) an = analyser;
    } catch (e) { /* */ }
    var data = null;
    if (an) {
      if (!vizState.data || vizState.data.length !== an.frequencyBinCount) {
        vizState.data = new Uint8Array(an.frequencyBinCount);
      }
      an.getByteFrequencyData(vizState.data);
      data = vizState.data;
    }

    vizState.heroes.forEach(function (hero) {
      if (hero.el.offsetParent === null) return;
      if (!vizResize(hero)) return;
      drawHeroViz(hero, data, ts * .001);
    });
  }

  function vizStart() {
    if (vizState.raf) return;
    if (vizState.reduced) {
      vizState.heroes.forEach(function (hero) {
        if (vizResize(hero)) drawHeroViz(hero, null, 1.15);
      });
      return;
    }
    vizState.raf = requestAnimationFrame(vizFrame);
  }

  function buildHeroViz(header) {
    if (isMobile || !header || header.querySelector('.mneo-hero-viz')) return;
    var cv = document.createElement('canvas');
    cv.className = 'mneo-hero-viz';
    cv.setAttribute('aria-hidden', 'true');
    header.appendChild(cv);
    header.classList.add('mneo-hero-viz-on');
    header.style.backgroundImage = '';
    header.style.backgroundSize = '';
    header.style.backgroundPosition = '';
    vizState.heroes.push({
      el: header, cv: cv, ctx: cv.getContext('2d'),
      w: 0, h: 0, dpr: 1, bass: 0, mid: 0, high: 0
    });
    vizStart();
  }

  if (vizState.reduced) {
    on(window, 'resize', function () { vizStart(); });
  }

  function buildHeroCta(kind) {
    if (isMobile) return;
    var header = heroEl(kind);
    if (!header) return;
    var copy = header.querySelector(':scope > div:first-child');
    if (!copy) return;

    var existingCta = copy.querySelector('.mneo-hero-cta');
    if (existingCta) {
      var lateActions = copy.querySelector('.mneo-hero-actions');
      if (lateActions && copy.lastElementChild !== lateActions) copy.appendChild(lateActions);
      return;
    }

    /* Кнопки из правой части баннера переносим под описание: иначе они
       наезжают на коллаж обложек. */
    var actions = copy.querySelector('.mneo-hero-actions') || null;
    Array.prototype.forEach.call(header.querySelectorAll(':scope > div'), function (d) {
      if (d === copy) return;
      if (d.classList.contains('mneo-hero-art') || d.classList.contains('mneo-hero-cta')) return;
      if (d.querySelector('.btn')) actions = d;
    });
    if (actions && actions.classList) {
      actions.classList.add('mneo-hero-actions');
      copy.appendChild(actions);
    }

    var cta = document.createElement('div');
    cta.className = 'mneo-hero-cta';
    var primary = '', secondary = '';
    var hasData = false;

    if (kind === 'music') {
      hasData = !!(typeof musicIndex !== 'undefined' && musicIndex && (musicIndex.tracks || []).length);
      primary = '<button type="button" class="mneo-cta mneo-cta-primary" data-act="play"><i class="fa-solid fa-play"></i> Слушать</button>';
      secondary = '<button type="button" class="mneo-cta mneo-cta-ghost" data-act="shuffle"><i class="fa-solid fa-shuffle"></i> Перемешать</button>';
    } else if (kind === 'movies') {
      hasData = !!(typeof videoIndex !== 'undefined' && videoIndex && (videoIndex.videos || []).filter(function (v) { return !v._is_series; }).length);
      primary = '<button type="button" class="mneo-cta mneo-cta-primary" data-act="watch"><i class="fa-solid fa-play"></i> Смотреть</button>';
      secondary = '<button type="button" class="mneo-cta mneo-cta-ghost" data-act="random"><i class="fa-solid fa-dice"></i> Мне повезёт</button>';
    } else if (kind === 'series') {
      hasData = !!(typeof videoIndex !== 'undefined' && videoIndex && (videoIndex.videos || []).some(function (v) { return v && v._is_series; }));
      primary = '<button type="button" class="mneo-cta mneo-cta-primary" data-act="open"><i class="fa-solid fa-play"></i> Открыть сериал</button>';
    } else if (kind === 'photos') {
      hasData = !!(typeof photoIndex !== 'undefined' && photoIndex && (photoIndex.photos || []).length);
      primary = '<button type="button" class="mneo-cta mneo-cta-primary" data-act="view"><i class="fa-solid fa-expand"></i> Смотреть</button>';
    }

    if (!hasData || !primary) return;

    cta.innerHTML = primary + secondary;
    copy.appendChild(cta);
    if (actions && actions.parentNode === copy) copy.appendChild(actions);

    on(cta, 'click', function (e) {
      var b = e.target.closest ? e.target.closest('[data-act]') : null;
      if (!b) return;
      var act = b.getAttribute('data-act');
      if (kind === 'music') {
        var list = (musicIndex.tracks || []);
        if (act === 'play' && list[0]) call('playTrack', list[0].id, 0);
        if (act === 'shuffle') {
          try { if (typeof isShuffle !== 'undefined' && !isShuffle) call('toggleShuffle'); } catch (err) { /* */ }
          var rnd = Math.floor(Math.random() * list.length);
          if (list[rnd]) call('playTrack', list[rnd].id, rnd);
        }
      } else if (kind === 'movies') {
        if (act === 'random') { call('playRandomMovie'); return; }
        var m = (videoIndex.videos || []).filter(function (v) { return !v._is_series; })[0];
        if (m) call('openVideoPlayer', m.id);
      } else if (kind === 'series') {
        var s = null;
        try { s = call('detectSeries', videoIndex.videos || [])[0]; } catch (err) { s = null; }
        if (s && s.name) call('openSeriesDetail', s.name);
      } else if (kind === 'photos') {
        var p = (photoIndex.photos || [])[0];
        if (p) call('openPhotoDetail', p.id, 0);
      }
    });
  }

  function buildDash(kind) {
    if (isMobile) return;
    var header = heroEl(kind);
    if (!header || !header.parentNode) return;

    /* Пересобираем при каждом обновлении, чтобы счётчики не застревали на 0,
       если индекс ещё не был загружен в момент первой отрисовки. */
    var previous = header.parentNode.querySelector('.mneo-dash');
    if (previous) previous.parentNode.removeChild(previous);

    var cards = [];
    function card(icon, value, label, action) {
      return '<button type="button" class="mneo-dash-card"' + (action ? ' data-act="' + action + '"' : ' disabled') + '>' +
        '<span class="mneo-dash-ico"><i class="fa-solid ' + icon + '"></i></span>' +
        '<span class="mneo-dash-meta"><b>' + value + '</b><small>' + label + '</small></span>' +
        '</button>';
    }

    try {
      if (kind === 'music') {
        fetchMusicStats();
        var ms = musicStats();
        cards.push(card('fa-music', ms.tracks, 'Треков', 'view:all'));
        cards.push(card('fa-user', ms.artists, 'Исполнителей', 'view:artists'));
        cards.push(card('fa-compact-disc', ms.albums, 'Альбомов', 'view:albums'));
        cards.push(card('fa-heart', ms.favorites, 'В избранном', 'view:favorites'));
      } else if (kind === 'movies') {
        var vids = (videoIndex.videos || []);
        var movies = vids.filter(function (v) { return !v._is_series; });
        var series = vids.filter(function (v) { return v._is_series; });
        var favs = 0; try { favs = ((favorites && favorites.videos) || []).length; } catch (e) { favs = 0; }
        cards.push(card('fa-film', movies.length, 'Фильмов', 'mview:all'));
        cards.push(card('fa-tv', series.length, 'Серий/эпизодов', 'mview:all'));
        cards.push(card('fa-heart', favs, 'В избранном', 'mview:favorites'));
        cards.push(card('fa-folder', 0, 'Коллекций', 'mview:collections'));
      } else if (kind === 'series') {
        var sAll = (videoIndex.videos || []);
        var names = {}; sAll.forEach(function (v) { if (v && v._is_series && v._series_id) names[v._series_id] = 1; });
        cards.push(card('fa-tv', Object.keys(names).length, 'Сериалов', ''));
        cards.push(card('fa-clapperboard', sAll.filter(function (v) { return v && v._is_series; }).length, 'Эпизодов', ''));
      } else if (kind === 'photos') {
        var ph = (photoIndex.photos || []);
        var pf = ph.filter(function (p) { return p && p.is_favorite; }).length;
        cards.push(card('fa-image', ph.length, 'Фотографий', ''));
        cards.push(card('fa-heart', pf, 'В избранном', ''));
      }
    } catch (e) { /* данные ещё не готовы */ }

    if (!cards.length) return;

    var dash = document.createElement('div');
    dash.className = 'mneo-dash';
    dash.innerHTML = cards.join('');
    header.parentNode.insertBefore(dash, header.nextSibling);

    on(dash, 'click', function (e) {
      var b = e.target.closest ? e.target.closest('[data-act]') : null;
      if (!b) return;
      var act = b.getAttribute('data-act') || '';
      if (act.indexOf('view:') === 0) {
        var view = act.slice(5);
        var tab = document.querySelector('#musicTabs .view-tab[data-view="' + view + '"]');
        if (tab) tab.click();
      } else if (act.indexOf('mview:') === 0) {
        var mv = act.slice(6);
        var mtab = document.querySelector('#movieTabs .view-tab[data-movie-view="' + mv + '"]');
        if (mtab) mtab.click();
      }
    });
  }

  /* ---------- Режим отображения треков: списком / карточками ---------- */

  var TRACKS_VIEW_KEY = 'mneo.tracksView';

  function tracksViewMode() {
    try {
      return localStorage.getItem(TRACKS_VIEW_KEY) === 'cards' ? 'cards' : 'list';
    } catch (e) { return 'list'; }
  }

  function musicViewName() {
    try {
      if (typeof currentMusicView !== 'undefined' && currentMusicView) return currentMusicView;
    } catch (e) { /* */ }
    return 'all';
  }

  function applyTracksView(mode) {
    var music = document.getElementById('music');
    if (!music) return;
    var cards = mode === 'cards';
    music.classList.toggle('mneo-tracks-cards', cards);
    $$('.mneo-view-mode [data-mode]', music).forEach(function (b) {
      var active = (b.getAttribute('data-mode') === 'cards') === cards;
      b.classList.toggle('active', active);
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }

  function syncTrackViewSwitch() {
    var box = $('.mneo-view-mode');
    if (!box) return;
    var v = musicViewName();
    /* У этих видов в #musicContent нет списка треков — переключатель не нужен. */
    box.classList.toggle('is-hidden', v === 'artists' || v === 'albums' || v === 'playlists');
  }

  function setTracksView(mode) {
    try { localStorage.setItem(TRACKS_VIEW_KEY, mode); } catch (e) { /* */ }
    applyTracksView(mode);
    buildMusicTableHead();
  }

  function buildTrackViewSwitch() {
    if (isMobile) return;
    var tabs = document.getElementById('musicTabs');
    if (!tabs) return;

    if (!tabs.parentNode.classList.contains('mneo-tabs-row')) {
      var row = document.createElement('div');
      row.className = 'mneo-tabs-row';
      tabs.parentNode.insertBefore(row, tabs);
      row.appendChild(tabs);
    }

    if ($('.mneo-view-mode', tabs.parentNode)) {
      applyTracksView(tracksViewMode());
      syncTrackViewSwitch();
      return;
    }

    var box = document.createElement('div');
    box.className = 'mneo-view-mode';
    box.setAttribute('role', 'group');
    box.setAttribute('aria-label', 'Режим отображения треков');
    box.innerHTML =
      '<button type="button" class="mneo-view-btn" data-mode="list"' +
        ' title="Списком" aria-label="Списком"><i class="fa-solid fa-list-ul"></i></button>' +
      '<button type="button" class="mneo-view-btn" data-mode="cards"' +
        ' title="Карточками" aria-label="Карточками"><i class="fa-solid fa-table-cells-large"></i></button>';
    tabs.parentNode.appendChild(box);

    on(box, 'click', function (e) {
      var b = e.target.closest ? e.target.closest('[data-mode]') : null;
      if (b) setTracksView(b.getAttribute('data-mode'));
    });

    applyTracksView(tracksViewMode());
    syncTrackViewSwitch();
  }

  function buildMusicTableHead() {
    if (isMobile) return;
    var list = document.getElementById('tracksListContainer');
    var old = document.querySelector('.mneo-track-head');

    var music = document.getElementById('music');
    if (music && music.classList.contains('mneo-tracks-cards')) {
      if (old && old.parentNode) old.parentNode.removeChild(old);
      return;
    }

    var view = 'all';
    try {
      if (typeof currentMusicView !== 'undefined' && currentMusicView) view = currentMusicView;
    } catch (e) { /* */ }

    if (!list || !list.parentNode || view !== 'all') {
      if (old && old.parentNode) old.parentNode.removeChild(old);
      return;
    }

    if (old && old.nextElementSibling === list) return;
    if (old && old.parentNode) old.parentNode.removeChild(old);

    var head = document.createElement('div');
    head.className = 'mneo-track-head';
    head.innerHTML =
      '<span class="mneo-th-title">Трек</span>' +
      '<span class="mneo-th-time">Время</span>';
    list.parentNode.insertBefore(head, list);
  }

  function refreshDesktopStructure() {
    if (isMobile) return;
    ['music', 'movies', 'series', 'photos'].forEach(function (id) {
      buildHeroArt(id);
      buildHeroCta(id);
      buildDash(id);
    });
    buildTrackViewSwitch();
    buildMusicTableHead();
  }

  refreshDesktopStructure();
  [700, 1800, 3500, 7000].forEach(function (ms) {
    setTimeout(refreshDesktopStructure, ms);
  });

  /* Список треков перерисовывается при смене вида/жанра — заголовок таблицы
     должен следовать за ним (и исчезать в других видах). */
  var musicContentEl = document.getElementById('musicContent');
  if (musicContentEl && typeof MutationObserver === 'function') {
    var headTimer = null;
    try {
      new MutationObserver(function () {
        if (headTimer) return;
        headTimer = setTimeout(function () {
          headTimer = null;
          buildMusicTableHead();
          buildDash('music');
          syncTrackViewSwitch();
        }, 140);
      }).observe(musicContentEl, { childList: true, subtree: true });
    } catch (e) { /* */ }
  }

  /* ==========================================================================
     5. Полноэкранный аудиоплеер (клик по обложке в доке)
     ========================================================================== */

  var audioPlayer = document.getElementById('audioPlayer');
  var playerThumb = document.getElementById('playerThumb');
  var now = null;

  if (playerThumb) {
    playerThumb.title = 'Открыть полноэкранный плеер';
    playerThumb.setAttribute('role', 'button');
    playerThumb.setAttribute('tabindex', '0');
    on(playerThumb, 'keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openNow();
      }
    });
  }

  function buildNowPlaying() {
    if (now) return now;
    now = document.createElement('div');
    now.className = 'mneo-now';
    now.id = 'mneoNow';
    now.setAttribute('role', 'dialog');
    now.setAttribute('aria-modal', 'true');
    now.setAttribute('aria-label', 'Сейчас играет');
    now.innerHTML =
      '<div class="mneo-now-bg"></div>' +
      '<button class="mneo-now-close" type="button" aria-label="Закрыть"><i class="fa-solid fa-xmark"></i></button>' +
      '<div class="mneo-now-card">' +
      '  <div class="mneo-now-art"><i class="fa-solid fa-music"></i></div>' +
      '  <div class="mneo-now-copy">' +
      '    <div class="mneo-now-eyebrow">Сейчас играет</div>' +
      '    <div class="mneo-now-title">—</div>' +
      '    <div class="mneo-now-artist">—</div>' +
      '    <div class="mneo-now-progress"><div class="mneo-now-progress-fill"></div></div>' +
      '    <div class="mneo-now-times"><span class="mneo-now-cur">0:00</span><span class="mneo-now-dur">0:00</span></div>' +
      '    <div class="mneo-now-controls">' +
      '      <button class="mneo-now-btn mneo-now-shuffle" type="button" aria-label="Перемешать"><i class="fa-solid fa-shuffle"></i></button>' +
      '      <button class="mneo-now-btn mneo-now-prev" type="button" aria-label="Предыдущий"><i class="fa-solid fa-backward-step"></i></button>' +
      '      <button class="mneo-now-btn mneo-now-play" type="button" aria-label="Играть/пауза"><i class="fa-solid fa-play"></i></button>' +
      '      <button class="mneo-now-btn mneo-now-next" type="button" aria-label="Следующий"><i class="fa-solid fa-forward-step"></i></button>' +
      '      <button class="mneo-now-btn mneo-now-fav" type="button" aria-label="В избранное"><i class="fa-regular fa-heart"></i></button>' +
      '    </div>' +
      '    <div class="mneo-now-extra">' +
      '      <button class="mneo-now-chip mneo-now-speed" type="button"><i class="fa-solid fa-gauge-high"></i> <span>1x</span></button>' +
      '      <button class="mneo-now-chip mneo-now-queue" type="button"><i class="fa-solid fa-list-ol"></i> Очередь</button>' +
      '      <button class="mneo-now-chip mneo-now-dock" type="button"><i class="fa-solid fa-chevron-down"></i> Свернуть</button>' +
      '    </div>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(now);

    on($('.mneo-now-close', now), 'click', closeNow);
    on($('.mneo-now-dock', now), 'click', closeNow);
    on($('.mneo-now-play', now), 'click', function () { call('playerTogglePlay'); syncNow(); });
    on($('.mneo-now-prev', now), 'click', function () { call('playerPrev'); });
    on($('.mneo-now-next', now), 'click', function () { call('playerNext'); });
    on($('.mneo-now-shuffle', now), 'click', function () { call('toggleShuffle'); syncNow(); });
    on($('.mneo-now-fav', now), 'click', function () { call('toggleFavPlayer'); syncNow(); });
    on($('.mneo-now-queue', now), 'click', function () { call('toggleQueue'); });
    on($('.mneo-now-speed', now), 'click', function () { call('cycleSpeed'); syncNow(); });

    on($('.mneo-now-progress', now), 'click', function (e) {
      var bar = document.getElementById('progressBar');
      if (!bar) return;
      var rect = e.currentTarget.getBoundingClientRect();
      var pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      try {
        bar.dispatchEvent(new MouseEvent('click', {
          bubbles: true, cancelable: true,
          clientX: rect.left + pct * rect.width,
          clientY: rect.top + rect.height / 2
        }));
      } catch (err) { /* */ }
      syncNow();
    });

    return now;
  }

  /* ---------- Правая панель «Сейчас играет» (десктоп) ---------- */

  var desk = null;

  function buildDesk() {
    if (isMobile || desk || !audioPlayer) return;

    desk = {
      next: null,
      trackId: null,
      count: null
    };

    var top = document.createElement('div');
    top.className = 'mneo-desk-top';
    top.innerHTML =
      '<span class="mneo-desk-live"><i class="fa-solid fa-wave-square"></i> Сейчас играет</span>' +
      '<button type="button" class="mneo-desk-expand" title="Открыть плеер на весь экран">' +
      '<i class="fa-solid fa-up-right-and-down-left-from-center"></i></button>';
    audioPlayer.insertBefore(top, audioPlayer.firstChild);
    on($('.mneo-desk-expand', top), 'click', openNow);

    /* Переносим громкость и скорость в отдельную строку — на десктопе это
       аккуратный ряд инструментов под прогрессом. */
    var tools = document.createElement('div');
    tools.className = 'mneo-desk-tools';

    var speedBtn = document.getElementById('speedBtn');
    var volume = audioPlayer.querySelector('.volume-control');
    var queueBtn = null;
    Array.prototype.forEach.call(audioPlayer.querySelectorAll('.player-btn'), function (b) {
      if ((b.getAttribute('onclick') || '').indexOf('toggleQueue') !== -1) queueBtn = b;
    });

    if (speedBtn) tools.appendChild(speedBtn);
    if (volume) tools.appendChild(volume);
    if (queueBtn) tools.appendChild(queueBtn);
    if (tools.children.length) audioPlayer.appendChild(tools);

    var extras = document.createElement('div');
    extras.className = 'mneo-desk-extras';
    extras.innerHTML =
      '<div class="mneo-desk-label">' +
      '  <span><i class="fa-solid fa-list-ul"></i> Далее</span>' +
      '  <button type="button" class="mneo-desk-all">Вся очередь</button>' +
      '</div>' +
      '<div class="mneo-desk-next"></div>';
    audioPlayer.appendChild(extras);

    desk.next = $('.mneo-desk-next', extras);
    on($('.mneo-desk-all', extras), 'click', function () { call('toggleQueue'); });

    on(desk.next, 'click', function (e) {
      var b = e.target.closest ? e.target.closest('.mneo-desk-track') : null;
      if (!b) return;
      var id = b.getAttribute('data-track-id');
      var index = parseInt(b.getAttribute('data-index'), 10);
      if (id) call('playTrack', id, isNaN(index) ? 0 : index);
    });

    renderDeskNext(true);
  }

  function currentPlaylistSafe() {
    try {
      if (typeof currentPlaylist !== 'undefined' && currentPlaylist && currentPlaylist.length) return currentPlaylist;
    } catch (e) { /* */ }
    try {
      if (typeof currentContextTracks !== 'undefined' && currentContextTracks && currentContextTracks.length) return currentContextTracks;
    } catch (e) { /* */ }
    try {
      if (typeof musicIndex !== 'undefined' && musicIndex && musicIndex.tracks) return musicIndex.tracks;
    } catch (e) { /* */ }
    return [];
  }

  function renderDeskNext(force) {
    if (!desk || !desk.next) return;

    var playlist = currentPlaylistSafe();
    var currentId = null;
    var currentIndex = -1;
    try { currentId = currentTrack && currentTrack.id ? currentTrack.id : null; } catch (e) { /* */ }
    try { currentIndex = typeof currentTrackIndex === 'number' ? currentTrackIndex : -1; } catch (e) { /* */ }

    var key = String(currentId) + '|' + playlist.length + '|' + currentIndex;
    if (!force && desk.trackId === key) return;
    desk.trackId = key;

    desk.next.innerHTML = '';

    if (!playlist.length) {
      var empty = document.createElement('div');
      empty.className = 'mneo-desk-empty';
      empty.innerHTML = '<i class="fa-solid fa-list-ul"></i> Очередь пуста';
      desk.next.appendChild(empty);
      return;
    }

    var start = currentIndex >= 0 ? currentIndex + 1 : 0;
    var picked = [];
    for (var i = 0; i < playlist.length && picked.length < 3; i++) {
      var t = playlist[(start + i) % playlist.length];
      if (!t || !t.id || t.id === currentId) continue;
      picked.push({ track: t, index: (start + i) % playlist.length });
    }

    if (!picked.length) {
      var solo = document.createElement('div');
      solo.className = 'mneo-desk-empty';
      solo.innerHTML = '<i class="fa-solid fa-music"></i> В очереди только этот трек';
      desk.next.appendChild(solo);
      return;
    }

    picked.forEach(function (entry) {
      var t = entry.track;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'mneo-desk-track';
      btn.setAttribute('data-track-id', t.id);
      btn.setAttribute('data-index', String(entry.index));

      var art = document.createElement('span');
      art.className = 'mneo-desk-track-art';
      if (t.has_cover) {
        var img = document.createElement('img');
        img.alt = '';
        img.loading = 'lazy';
        img.src = '/api/media/cover/' + encodeURIComponent(t.id);
        art.appendChild(img);
      } else {
        art.innerHTML = '<i class="fa-solid fa-music"></i>';
      }

      var copy = document.createElement('span');
      copy.className = 'mneo-desk-track-copy';
      var name = document.createElement('b');
      name.textContent = t.title || 'Без названия';
      var artist = document.createElement('small');
      artist.textContent = t.artist || 'Неизвестный исполнитель';
      copy.appendChild(name);
      copy.appendChild(artist);

      var play = document.createElement('span');
      play.className = 'mneo-desk-track-play';
      play.innerHTML = '<i class="fa-solid fa-play"></i>';

      btn.appendChild(art);
      btn.appendChild(copy);
      btn.appendChild(play);
      desk.next.appendChild(btn);
    });
  }

  function syncDesk() {
    if (isMobile || !audioPlayer) return;
    if (!desk) buildDesk();
    if (!desk) return;
    renderDeskNext(false);
  }

  function syncNow() {
    syncDesk();
    if (!now) return;
    if (!audioPlayer) return;

    var title = document.getElementById('playerTitle');
    var artist = document.getElementById('playerArtist');
    var fill = document.getElementById('progressFill');
    var cur = document.getElementById('playerCurrentTime');
    var dur = document.getElementById('playerDuration');
    var playBtnIcon = document.querySelector('#playerPlayBtn i');
    var shuffleBtn = document.getElementById('shuffleBtn');
    var favBtnIcon = document.querySelector('#favPlayerBtn i');
    var speedBtn = document.getElementById('speedBtn');

    var art = $('.mneo-now-art', now);
    var bg = $('.mneo-now-bg', now);
    var img = playerThumb ? playerThumb.querySelector('img') : null;

    if (img && img.getAttribute('src')) {
      var src = img.getAttribute('src');
      if (art.dataset.mneoArt !== src) {
        art.dataset.mneoArt = src;
        art.innerHTML = '';
        var newImg = document.createElement('img');
        newImg.alt = '';
        newImg.src = src;
        art.appendChild(newImg);
      }
      bg.style.backgroundImage = 'url("' + String(src).replace(/"/g, '%22') + '")';
    } else if (art.dataset.mneoArt !== 'icon') {
      art.dataset.mneoArt = 'icon';
      art.innerHTML = '<i class="fa-solid fa-music"></i>';
      bg.style.backgroundImage = '';
    }

    $('.mneo-now-title', now).textContent = title ? title.textContent : '—';
    $('.mneo-now-artist', now).textContent = artist ? artist.textContent : '—';

    var pct = fill ? (fill.style.width || '0%') : '0%';
    $('.mneo-now-progress-fill', now).style.width = pct;

    if (cur) $('.mneo-now-cur', now).textContent = cur.textContent;
    if (dur) $('.mneo-now-dur', now).textContent = dur.textContent;

    var playIcon = $('.mneo-now-play i', now);
    if (playIcon && playBtnIcon) playIcon.className = playBtnIcon.className;

    var sh = $('.mneo-now-shuffle', now);
    if (sh) sh.classList.toggle('active', !!(shuffleBtn && shuffleBtn.classList.contains('active')));

    var favIcon = $('.mneo-now-fav i', now);
    if (favIcon && favBtnIcon) favIcon.className = favBtnIcon.className;

    var speedLabel = $('.mneo-now-speed span', now);
    if (speedLabel && speedBtn) speedLabel.textContent = speedBtn.textContent || '1x';
  }

  function openNow() {
    buildNowPlaying();
    syncNow();
    now.classList.add('open');
    document.body.classList.add('mneo-now-open');
  }

  function updateHasTrack() {
    if (!audioPlayer || !document.body) return;
    var visible = audioPlayer.style.display !== 'none' && getComputedStyle(audioPlayer).display !== 'none';
    document.body.classList.toggle('mneo-has-track', visible);
  }
  updateHasTrack();
  if (audioPlayer && typeof MutationObserver === 'function') {
    try {
      new MutationObserver(updateHasTrack).observe(audioPlayer, {
        attributes: true, attributeFilter: ['style', 'class']
      });
    } catch (e) { /* */ }
  }

  function closeNow() {
    if (now) now.classList.remove('open');
    document.body.classList.remove('mneo-now-open');
  }

  on(audioPlayer, 'click', function (e) {
    var thumb = e.target.closest ? e.target.closest('#playerThumb') : null;
    if (thumb) {
      e.preventDefault();
      openNow();
    }
  });

  if (audioPlayer && typeof MutationObserver === 'function') {
    try {
      new MutationObserver(function () {
        syncNow();
      }).observe(audioPlayer, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeFilter: ['style', 'class']
      });
    } catch (e) { /* */ }
  }

  var audioEl = document.getElementById('audioElement');
  on(audioEl, 'timeupdate', function () {
    if (now && now.classList.contains('open')) syncNow();
  });
  on(audioEl, 'play', function () {
    if (now && now.classList.contains('open')) syncNow();
  });
  on(audioEl, 'pause', function () {
    if (now && now.classList.contains('open')) syncNow();
  });

  on(document, 'keydown', function (e) {
    if (e.key === 'Escape' && now && now.classList.contains('open')) {
      e.stopPropagation();
      closeNow();
    }
  });

  /* Создаём панель «Сейчас играет» сразу, чтобы она не появлялась рывком */
  syncDesk();
  [600, 1800, 3600].forEach(function (ms) { setTimeout(syncDesk, ms); });
  on(window, 'resize', function () {
    if (!isMobile && !desk) syncDesk();
  });

  /* ==========================================================================
     6. Видео: кинематографичная обложка + фуллскрин
     ========================================================================== */

  function findCurrentVideoMeta() {
    try {
      var h1 = $('.video-player-detail h1');
      if (!h1 || typeof videoIndex === 'undefined' || !videoIndex || !videoIndex.videos) return null;
      var title = (h1.textContent || '').trim();
      for (var i = 0; i < videoIndex.videos.length; i++) {
        var v = videoIndex.videos[i];
        if (v && (v.title || '').trim() === title) return v;
      }
    } catch (e) { /* */ }
    return null;
  }

  function requestFullscreen(el, video) {
    try {
      if (document.fullscreenElement || document.webkitFullscreenElement) {
        var ex = document.exitFullscreen || document.webkitExitFullscreen;
        if (ex) { var q = ex.call(document); if (q && q.catch) q.catch(function () {}); return; }
      }
      if (el && el.requestFullscreen) {
        var p = el.requestFullscreen();
        if (p && p.catch) p.catch(function () {});
        return;
      }
      if (el && el.webkitRequestFullscreen) { el.webkitRequestFullscreen(); return; }
      if (el && el.msRequestFullscreen) { el.msRequestFullscreen(); return; }
      if (video && video.webkitEnterFullscreen) { video.webkitEnterFullscreen(); return; }
      if (video && video.webkitEnterFullScreen) { video.webkitEnterFullScreen(); }
    } catch (e) { /* */ }
  }

  function enhanceVideoPlayer() {
    var wrap = document.querySelector('.video-player-wrapper');
    if (!wrap) return;

    var video = wrap.querySelector('video');

    /* Кнопка «на весь экран» — всегда поверх плеера */
    if (!wrap.querySelector('.mneo-fullscreen-btn')) {
      var fs = document.createElement('button');
      fs.type = 'button';
      fs.className = 'mneo-fullscreen-btn';
      fs.title = 'На весь экран';
      fs.setAttribute('aria-label', 'На весь экран');
      fs.innerHTML = '<i class="fa-solid fa-expand"></i>';
      on(fs, 'click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        var v = wrap.querySelector('video');
        requestFullscreen(wrap, v);
      });
      wrap.appendChild(fs);
    }

    /* Обложка до старта */
    if (!wrap.querySelector('.mneo-video-cover')) {
      var meta = findCurrentVideoMeta();
      var poster = meta ? (meta.backdrop_url || meta.poster_url || '') : '';
      var title = '';
      var h1 = $('.video-player-detail h1');
      if (h1) title = (h1.textContent || '').trim();

      var cover = document.createElement('div');
      cover.className = 'mneo-video-cover';
      if (poster) {
        cover.style.backgroundImage = 'url("' + String(poster).replace(/"/g, '%22') + '")';
      }
      cover.innerHTML =
        '<div class="mneo-video-cover-inner">' +
        (title ? '<div class="mneo-video-cover-title"></div>' : '') +
        '<div class="mneo-video-cover-sub">Нажмите на обложку, чтобы смотреть на весь экран</div>' +
        '<button type="button" class="mneo-video-cover-play"><i class="fa-solid fa-play"></i> Смотреть</button>' +
        '</div>';
      if (title) $('.mneo-video-cover-title', cover).textContent = title;

      function start(e) {
        if (e) { e.preventDefault(); e.stopPropagation(); }
        cover.classList.add('mneo-hidden');
        var v = wrap.querySelector('video');
        if (v) {
          try {
            var p = v.play();
            if (p && p.catch) p.catch(function () {});
          } catch (err) { /* */ }
        }
        requestFullscreen(wrap, v);
      }

      on(cover, 'click', start);
      on($('.mneo-video-cover-play', cover), 'click', start);

      wrap.appendChild(cover);
    }

    if (wrap.dataset.mneoPlayBound !== '1') {
      wrap.dataset.mneoPlayBound = '1';
      wrap.addEventListener('play', function (e) {
        if (e.target && e.target.tagName === 'VIDEO') {
          var c = wrap.querySelector('.mneo-video-cover');
          if (c) c.classList.add('mneo-hidden');
        }
      }, true);
    }
  }

  var movieContent = document.getElementById('movieContent');
  if (movieContent && typeof MutationObserver === 'function') {
    try {
      new MutationObserver(function () { enhanceVideoPlayer(); })
        .observe(movieContent, { childList: true, subtree: true });
    } catch (e) { /* */ }
  }
  /* Первичная попытка (если плеер уже отрисован) */
  setTimeout(enhanceVideoPlayer, 400);

  /* ==========================================================================
     7. Мобильная нижняя навигация
     ========================================================================== */

  function buildMobileNav() {
    if (!isMobile || $('.mneo-bottom-nav')) return;

    var nav = document.createElement('nav');
    nav.className = 'mneo-bottom-nav';
    nav.setAttribute('aria-label', 'Разделы медиа');
    nav.innerHTML =
      '<button class="mneo-nav-item" type="button" data-section="music"><i class="fa-solid fa-music"></i><span>Музыка</span></button>' +
      '<button class="mneo-nav-item" type="button" data-section="movies"><i class="fa-solid fa-film"></i><span>Фильмы</span></button>' +
      '<button class="mneo-nav-item" type="button" data-section="series"><i class="fa-solid fa-tv"></i><span>Сериалы</span></button>' +
      '<button class="mneo-nav-item" type="button" data-section="photos"><i class="fa-solid fa-image"></i><span>Фото</span></button>' +
      '<button class="mneo-nav-item" type="button" data-mneo-more="1"><i class="fa-solid fa-ellipsis"></i><span>Ещё</span></button>';
    document.body.appendChild(nav);

    $$('.mneo-nav-item[data-section]', nav).forEach(function (btn) {
      on(btn, 'click', function () {
        call('showSection', btn.getAttribute('data-section'));
        updateMobileActive();
      });
    });

    on($('[data-mneo-more]', nav), 'click', openMoreSheet);

    function update() { updateMobileActive(); }

    if (typeof MutationObserver === 'function') {
      try {
        var mo = new MutationObserver(update);
        $$('.section').forEach(function (s) { mo.observe(s, { attributes: true, attributeFilter: ['class', 'style'] }); });
      } catch (e) { /* */ }
    }

    updateMobileActive();
  }

  function updateMobileActive() {
    var active = $('.section.active');
    var id = active ? active.id : '';
    if (id === 'seriesDetail' || id === 'collectionDetail') id = 'series';
    $$('.mneo-nav-item[data-section]').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-section') === id);
    });
    var more = $('.mneo-nav-item[data-mneo-more]');
    if (more) {
      var extra = ['continue', 'torrents', 'achievements', 'stats', 'settings'];
      more.classList.toggle('active', extra.indexOf(id) !== -1);
    }
    var sheetBtns = $$('.mneo-sheet-btn[data-section]');
    sheetBtns.forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-section') === id);
    });
  }

  var moreSheet = null;

  function openMoreSheet() {
    if (!isMobile) return;
    if (!moreSheet) {
      moreSheet = document.createElement('div');
      moreSheet.className = 'mneo-sheet';
      moreSheet.id = 'mneoMoreSheet';
      moreSheet.innerHTML =
        '<div class="mneo-sheet-panel" role="dialog" aria-modal="true" aria-label="Ещё разделы">' +
        '  <div class="mneo-sheet-grab"></div>' +
        '  <div class="mneo-sheet-title">Ещё</div>' +
        '  <div class="mneo-sheet-grid">' +
        '    <button class="mneo-sheet-btn" type="button" data-section="continue"><i class="fa-solid fa-play"></i><span>Продолжить</span></button>' +
        '    <button class="mneo-sheet-btn" type="button" data-section="torrents"><i class="fa-solid fa-bolt"></i><span>Торренты</span></button>' +
        '    <button class="mneo-sheet-btn" type="button" data-section="achievements"><i class="fa-solid fa-trophy"></i><span>Достижения</span></button>' +
        '    <button class="mneo-sheet-btn" type="button" data-section="stats"><i class="fa-solid fa-chart-simple"></i><span>Статистика</span></button>' +
        '    <button class="mneo-sheet-btn" type="button" data-section="settings"><i class="fa-solid fa-gear"></i><span>Настройки</span></button>' +
        '    <button class="mneo-sheet-btn" type="button" data-act="search"><i class="fa-solid fa-magnifying-glass"></i><span>Поиск</span></button>' +
        '    <button class="mneo-sheet-btn" type="button" data-act="theme"><i class="fa-solid fa-palette"></i><span>Тема</span></button>' +
        '    <a class="mneo-sheet-btn" href="/media"><i class="fa-solid fa-rotate-left"></i><span>Классика</span></a>' +
        '  </div>' +
        '</div>';
      document.body.appendChild(moreSheet);

      on(moreSheet, 'click', function (e) {
        if (e.target === moreSheet || (e.target.closest && e.target.closest('.mneo-sheet-grab'))) closeMoreSheet();
      });

      $$('.mneo-sheet-btn[data-section]', moreSheet).forEach(function (b) {
        on(b, 'click', function () {
          call('showSection', b.getAttribute('data-section'));
          closeMoreSheet();
          updateMobileActive();
        });
      });

      on($('[data-act="search"]', moreSheet), 'click', function () {
        closeMoreSheet();
        call('openGlobalSearch');
      });

      on($('[data-act="theme"]', moreSheet), 'click', function () {
        window.toggleTheme();
        updateMobileActive();
      });
    }

    moreSheet.classList.add('open');
    document.body.classList.add('mneo-now-open');
    updateMobileActive();
  }

  function closeMoreSheet() {
    if (moreSheet) moreSheet.classList.remove('open');
    if (!(now && now.classList.contains('open'))) {
      document.body.classList.remove('mneo-now-open');
    }
  }

  if (isMobile) {
    buildMobileNav();
    on(document, 'keydown', function (e) {
      if (e.key === 'Escape' && moreSheet && moreSheet.classList.contains('open')) closeMoreSheet();
    });
    /* Секции могут подгрузиться позже — обновляем активный пункт */
    [600, 2000, 5000].forEach(function (ms) { setTimeout(updateMobileActive, ms); });
  }

  /* ==========================================================================
     7b. Меню «Ещё»: богатые плитки вместо списка
     ========================================================================== */

  function upgradeMoreMenu() {
    var pop = document.querySelector('.toolbar .more-pop');
    if (!pop || pop.dataset.mneoUpgraded === '1') return;
    pop.dataset.mneoUpgraded = '1';

    var head = document.createElement('div');
    head.className = 'mneo-more-head';
    head.innerHTML =
      '<span class="mneo-more-title">Ещё</span>' +
      '<span class="mneo-more-hint">Инструменты и настройки</span>';
    pop.insertBefore(head, pop.firstChild);

    $$('button.btn[data-section], .more-pop .btn', pop).forEach(function (b) {
      b.classList.add('mneo-more-tile');
    });
  }

  /* ==========================================================================
     7c. Замена эмодзи на иконки Font Awesome
     ========================================================================== */

  var EMOJI_ICONS = [
    ['🎵', 'fa-music'], ['🎶', 'fa-music'], ['🎧', 'fa-headphones'], ['🎤', 'fa-microphone'],
    ['💿', 'fa-compact-disc'], ['📀', 'fa-compact-disc'], ['🎬', 'fa-film'], ['🎞', 'fa-film'],
    ['📺', 'fa-tv'], ['📼', 'fa-video'], ['🖼', 'fa-image'], ['📸', 'fa-camera'], ['📷', 'fa-camera'],
    ['📁', 'fa-folder'], ['📂', 'fa-folder-open'], ['📋', 'fa-clipboard-list'], ['📝', 'fa-pen-to-square'],
    ['⭐', 'fa-star'], ['🌟', 'fa-star'], ['✨', 'fa-wand-magic-sparkles'], ['🔥', 'fa-fire'],
    ['❤', 'fa-heart'], ['💜', 'fa-heart'], ['💙', 'fa-heart'], ['✅', 'fa-circle-check'],
    ['⚠', 'fa-triangle-exclamation'], ['❌', 'fa-circle-xmark'], ['⛔', 'fa-ban'],
    ['🔍', 'fa-magnifying-glass'], ['🔎', 'fa-magnifying-glass'], ['🔒', 'fa-lock'], ['🔓', 'fa-lock-open'],
    ['⚙', 'fa-gear'], ['🔧', 'fa-wrench'], ['🛠', 'fa-screwdriver-wrench'], ['⚡', 'fa-bolt'],
    ['🏆', 'fa-trophy'], ['📊', 'fa-chart-simple'], ['📈', 'fa-chart-line'], ['💡', 'fa-lightbulb'],
    ['🚀', 'fa-rocket'], ['⏱', 'fa-stopwatch'], ['⏳', 'fa-hourglass-half'], ['🕒', 'fa-clock'],
    ['▶', 'fa-play'], ['⏸', 'fa-pause'], ['⏭', 'fa-forward-step'], ['⏮', 'fa-backward-step'],
    ['🔀', 'fa-shuffle'], ['🔁', 'fa-repeat'], ['🔊', 'fa-volume-high'], ['🔈', 'fa-volume-low'],
    ['🔇', 'fa-volume-xmark'], ['➕', 'fa-plus'], ['➖', 'fa-minus'], ['🗑', 'fa-trash'],
    ['✏', 'fa-pen'], ['👤', 'fa-user'], ['👥', 'fa-users'], ['🌙', 'fa-moon'], ['☀', 'fa-sun'],
    ['🎲', 'fa-dice'], ['📥', 'fa-download'], ['📤', 'fa-upload'], ['🔗', 'fa-link'],
    ['💾', 'fa-floppy-disk'], ['🖥', 'fa-display'], ['💻', 'fa-laptop'], ['🌐', 'fa-globe'],
    ['📡', 'fa-satellite-dish'], ['ℹ', 'fa-circle-info'], ['❓', 'fa-circle-question'],
    ['🏠', 'fa-house'], ['🎯', 'fa-bullseye'], ['🎨', 'fa-palette'], ['🎭', 'fa-masks-theater'],
    ['🕹', 'fa-gamepad'], ['🍿', 'fa-champagne-glasses']
  ];

  function emojiIcon(ch) {
    for (var i = 0; i < EMOJI_ICONS.length; i++) {
      if (ch === EMOJI_ICONS[i][0]) return EMOJI_ICONS[i][1];
    }
    return '';
  }

  var EMOJI_RE = /([\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{1F000}-\u{1F2FF}]\uFE0F?)/gu;

  function iconizeTextNode(node) {
    if (!node || !node.nodeValue) return;
    var text = node.nodeValue;
    if (!EMOJI_RE.test(text)) return;
    EMOJI_RE.lastIndex = 0;

    var parent = node.parentNode;
    if (!parent) return;

    var frag = document.createDocumentFragment();
    var last = 0;
    var match;
    while ((match = EMOJI_RE.exec(text))) {
      var icon = emojiIcon(match[1].replace(/\uFE0F/g, ''));
      if (!icon) { icon = emojiIcon(match[1]); }
      if (!icon) continue;
      if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));
      var span = document.createElement('span');
      span.className = 'mneo-ico';
      span.setAttribute('aria-hidden', 'true');
      span.innerHTML = '<i class="fa-solid ' + icon + '"></i>';
      frag.appendChild(span);
      last = match.index + match[1].length;
    }

    if (!last) return;
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    parent.replaceChild(frag, node);
  }

  var ICONIZE_SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1, INPUT: 1, SELECT: 1, OPTION: 1, CODE: 1, PRE: 1 };

  function mneoIconize(scope) {
    var rootEl = scope || document.body;
    if (!rootEl || ICONIZE_SKIP[rootEl.tagName]) return;

    var walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        if (n.parentNode && n.parentNode.closest && n.parentNode.closest('.mneo-ico, .fa, .mneo-desk-track-art, .player-thumb')) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });

    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(function (n) {
      var p = n.parentNode;
      if (!p || ICONIZE_SKIP[p.tagName]) return;
      if (p.classList && p.classList.contains('mneo-ico')) return;
      iconizeTextNode(n);
    });
  }

  upgradeMoreMenu();
  mneoIconize(document.body);
  [500, 1500, 3000, 6500].forEach(function (ms) {
    setTimeout(function () {
      upgradeMoreMenu();
      mneoIconize(document.body);
    }, ms);
  });

  if (document.body && typeof MutationObserver === 'function') {
    var iconTimer = null;
    try {
      new MutationObserver(function (records) {
        var relevant = false;
        for (var i = 0; i < records.length; i++) {
          if (records[i].addedNodes && records[i].addedNodes.length) { relevant = true; break; }
        }
        if (!relevant) return;
        if (iconTimer) return;
        iconTimer = setTimeout(function () {
          iconTimer = null;
          mneoIconize(document.body);
        }, 220);
      }).observe(document.body, { childList: true, subtree: true });
    } catch (e) { /* */ }
  }

  /* ==========================================================================
     8. Мелкие улучшения
     ========================================================================== */

  /* Поддержка горизонтального свайпа по табам на телефоне не нужна —
     они и так скроллятся нативно. Здесь только аккуратный фокус поиска. */
  on(document, 'keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
      /* страница уже обрабатывает ⌘K сама — не вмешиваемся */
    }
  });

  /* Пересчитываем hero при переключении разделов */
  if (typeof MutationObserver === 'function') {
    try {
      var heroObserver = new MutationObserver(function () { applyAllHeroes(); });
      HERO_IDS.forEach(function (id) {
        var s = document.getElementById(id);
        if (s) heroObserver.observe(s, { attributes: true, attributeFilter: ['class', 'style'] });
      });
    } catch (e) { /* */ }
  }
})();
