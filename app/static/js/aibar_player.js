/* ============================================================================
   Плеер окна палитры (Shift+Space).
   ----------------------------------------------------------------------------
   Берёт музыку из медиа-модуля: треки, плейлисты, избранное и рекомендации
   берутся из тех же эндпоинтов, что и страница /media, поэтому список один
   на всех. Звук идёт через MediaPlayback: играет всегда кто-то один, а
   мини-плеер рабочего стола показывает тот же трек.

   Аудио живёт отдельно от интерфейса: окно палитры может быть открыто в вид
   чата, а трек — играть. Поэтому audio и мост к протоколу создаются сразу при
   загрузке страницы (boot), а список и панели — только при открытии вида
   «музыка» (mount). Иначе «подхватить трек за мини-плеером» было бы нечем.

   Правило переноса звука: играет то окно, которое видно. Оболочка сообщает
   о показе и скрытии окна (ai-bar-shown / ai-bar-hidden), и плеер либо
   отдаёт трек мини-плееру рабочего стола, либо забирает его обратно.

   Эквалайзер — общий модуль AudioEQ (app/static/js/audio_eq.js).
   ========================================================================== */
(function () {
  'use strict';

  var API = {
    index: '/api/media/music-index',
    search: '/api/media/search',
    favorites: '/api/media/favorites',
    playlists: '/api/media/playlists',
    playlist: '/api/media/playlist',
    recommendations: '/api/media/recommendations',
    cover: '/api/media/cover/',
    favAdd: '/api/media/favorites/add',
    favRemove: '/api/media/favorites/remove',
    playLog: '/api/media/play-log'
  };

  var PAGE = 500;          // трек за одну загрузку индекса
  var MAX_TRACKS = 5000;   // потолок каталога в памяти окна

  var root = null, audio = null, bridge = null, eq = null;
  var catalog = { tracks: [], total: 0, loaded: false };
  var favorites = { tracks: [] };
  var view = 'tracks';
  var openPlaylist = null;
  var queue = [], qIndex = -1;
  var shuffle = false, repeat = false, seeking = false;
  /* Чей трек мы подхватили. При скрытии окна возвращаем его туда же: трек,
     взятый у /media, нельзя отдавать мини-плееру — того может не быть вовсе,
     и музыка осталась бы без владельца. */
  var adoptSource = null;
  var mounted = false, booted = false;
  /* Другой плеер держит трек: мы не играем, а показываем и управляем
     командами. remoteState — последнее его состояние для прогресса. */
  var remoteLive = false, remoteState = null;

  // ------------------------------------------------------------------ utils
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function fmtTime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function get(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('сервер ответил ' + r.status);
      return r.json();
    });
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json().catch(function () { return {}; }); });
  }

  function byId(id) {
    for (var i = 0; i < catalog.tracks.length; i++) {
      if (String(catalog.tracks[i].id) === String(id)) return catalog.tracks[i];
    }
    return null;
  }

  // ------------------------------------------------------------- каталог
  function loadCatalog(upto) {
    if (catalog.loaded) return Promise.resolve(catalog);
    var limit = Math.min(upto || PAGE, MAX_TRACKS);
    return get(API.index + '?limit=' + limit + '&offset=0').then(function (d) {
      catalog.tracks = d.tracks || [];
      catalog.total = d.total || catalog.tracks.length;
      catalog.loaded = true;
      return catalog;
    });
  }

  function loadFavorites() {
    return get(API.favorites).then(function (d) {
      favorites.tracks = (d && d.tracks) || [];
    }).catch(function () { favorites.tracks = []; });
  }

  // -------------------------------------------------------------- разметка
  function markup() {
    return '' +
      /* Шапка чата в режиме плеера скрыта — панель закрывает окно целиком,
         поэтому «назад» и «закрепить» живут здесь, иначе до них нельзя
         дотянуться. */
      '<div class="aibar-phead">' +
        '<button class="aibar-iconbtn p-back" title="К запросу"><i class="fas fa-arrow-left"></i></button>' +
        '<span class="aibar-phead-title">Медиа</span>' +
        '<span class="spacer"></span>' +
        '<button class="aibar-iconbtn p-pin" title="Закрепить окно"><i class="fas fa-thumbtack"></i></button>' +
      '</div>' +
      '<div class="aibar-ptabs">' +
        '<button data-ptab="tracks" class="on">Треки</button>' +
        '<button data-ptab="playlists">Плейлисты</button>' +
        '<button data-ptab="favorites">Избранное</button>' +
        '<button data-ptab="recommendations">Рекомендации</button>' +
        '<span class="spacer"></span>' +
        '<button class="aibar-tool aibar-eqbtn" title="Эквалайзер"><i class="fas fa-sliders"></i></button>' +
      '</div>' +
      '<div class="aibar-psearch">' +
        '<i class="fas fa-magnifying-glass"></i>' +
        '<input class="aibar-psearch-in" type="text" placeholder="Поиск по трекам…" autocomplete="off">' +
      '</div>' +
      '<div class="aibar-pcrumb" style="display:none"></div>' +
      '<div class="aibar-eq" style="display:none">' +
        '<div class="aibar-eq-head">' +
          '<span class="aibar-eq-title">Эквалайзер</span>' +
          '<select class="aibar-eq-preset">' +
            '<option value="">пресет…</option>' +
            '<option value="flat">ровно</option>' +
            '<option value="bass">бас</option>' +
            '<option value="vocal">вокал</option>' +
            '<option value="acoustic">акустика</option>' +
            '<option value="electronic">электроника</option>' +
            '<option value="rock">рок</option>' +
          '</select>' +
          '<label class="aibar-eq-bypass"><input type="checkbox"> мимо</label>' +
          '<span class="spacer"></span>' +
          '<span class="aibar-eq-preamp-lbl">предусил.</span>' +
          '<input class="aibar-eq-preamp" type="range" min="-12" max="12" step="1" value="0">' +
          '<span class="aibar-eq-preamp-val">0 dB</span>' +
        '</div>' +
        '<div class="aibar-eq-bands"></div>' +
        '<canvas class="aibar-eq-viz" width="900" height="90"></canvas>' +
      '</div>' +
      '<div class="aibar-plist"></div>' +
      '<div class="aibar-pnow">' +
        '<div class="aibar-pcover"><i class="fas fa-music"></i></div>' +
        '<div class="aibar-pmeta">' +
          '<div class="aibar-ptitle">Ничего не играет</div>' +
          '<div class="aibar-partist">выберите трек</div>' +
        '</div>' +
        '<div class="aibar-pctrl">' +
          '<button class="aibar-tool p-shuffle" title="Перемешать"><i class="fas fa-shuffle"></i></button>' +
          '<button class="aibar-tool p-prev" title="Предыдущий"><i class="fas fa-backward-step"></i></button>' +
          '<button class="aibar-tool p-play" title="Играть"><i class="fas fa-play"></i></button>' +
          '<button class="aibar-tool p-next" title="Следующий"><i class="fas fa-forward-step"></i></button>' +
          '<button class="aibar-tool p-repeat" title="Повтор"><i class="fas fa-repeat"></i></button>' +
        '</div>' +
        '<div class="aibar-pbar">' +
          '<span class="p-cur">0:00</span>' +
          '<div class="aibar-pseek"><div class="aibar-pfill"></div></div>' +
          '<span class="p-dur">0:00</span>' +
          '<i class="fas fa-volume-high"></i>' +
          '<input class="aibar-pvol" type="range" min="0" max="100" value="80">' +
        '</div>' +
      '</div>';
  }

  /* Интерфейс монтируется позже звука (mount), поэтому до открытия вида
     «музыка» root ещё null. Обращения к q() в этот момент — обычное дело:
     окно палитры в виде чата уже играет принятый трек. */
  function q(sel) { return root ? root.querySelector(sel) : null; }

  // --------------------------------------------------------------- список
  function rowHtml(t, i) {
    var fav = favorites.tracks.indexOf(t.id) >= 0;
    var cover = t.has_cover ? '<img src="' + API.cover + encodeURIComponent(t.id) + '" alt="">' : '';
    return '<div class="aibar-prow" data-i="' + i + '">' +
      '<div class="aibar-prow-cover">' + cover + '<i class="fas fa-music"></i></div>' +
      '<div class="aibar-prow-main">' +
        '<div class="aibar-prow-title">' + esc(t.title || 'без названия') + '</div>' +
        '<div class="aibar-prow-sub">' + esc(t.artist || '') + (t.album ? ' · ' + esc(t.album) : '') + '</div>' +
      '</div>' +
      '<button class="aibar-tool p-fav' + (fav ? ' on' : '') + '" title="В избранное"><i class="fas fa-heart"></i></button>' +
      '<span class="aibar-prow-dur">' + fmtTime(t.duration) + '</span>' +
      '</div>';
  }

  function renderList(list) {
    var box = q('.aibar-plist');
    if (!list.length) {
      box.innerHTML = '<div class="aibar-pempty">Пусто</div>';
      return;
    }
    box.innerHTML = list.map(rowHtml).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.aibar-prow'), function (row) {
      row.addEventListener('click', function (e) {
        if (e.target.closest('.p-fav')) return;
        playFrom(list, parseInt(row.getAttribute('data-i'), 10));
      });
    });
    Array.prototype.forEach.call(box.querySelectorAll('.p-fav'), function (b) {
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        var i = parseInt(b.closest('.aibar-prow').getAttribute('data-i'), 10);
        toggleFavorite(list[i], b);
      });
    });
  }

  function toggleFavorite(track, btn) {
    var isFav = favorites.tracks.indexOf(track.id) >= 0;
    var url = isFav ? API.favRemove : API.favAdd;
    post(url, { id: track.id, type: 'track' }).then(function () {
      if (isFav) favorites.tracks = favorites.tracks.filter(function (x) { return x !== track.id; });
      else favorites.tracks.push(track.id);
      btn.classList.toggle('on', !isFav);
    });
  }

  // ---------------------------------------------------------------- вкладки
  function showCrumb(text) {
    var c = q('.aibar-pcrumb');
    if (!text) { c.style.display = 'none'; c.innerHTML = ''; return; }
    c.style.display = 'flex';
    c.innerHTML = '<button class="aibar-tool p-crumb-back" title="К списку плейлистов">' +
                  '<i class="fas fa-chevron-left"></i></button><span>' + esc(text) + '</span>';
    c.querySelector('.p-crumb-back').addEventListener('click', function () {
      openPlaylist = null;
      setView('playlists');
    });
  }

  function setView(name) {
    view = name;
    Array.prototype.forEach.call(root.querySelectorAll('[data-ptab]'), function (b) {
      b.classList.toggle('on', b.getAttribute('data-ptab') === name);
    });
    q('.aibar-psearch').style.display = (name === 'tracks') ? '' : 'none';
    if (name !== 'playlists') showCrumb(null);

    if (name === 'tracks') {
      loadCatalog().then(function () { renderList(catalog.tracks); });
    } else if (name === 'favorites') {
      loadCatalog().then(loadFavorites).then(function () {
        renderList(catalog.tracks.filter(function (t) {
          return favorites.tracks.indexOf(t.id) >= 0;
        }));
      });
    } else if (name === 'recommendations') {
      q('.aibar-plist').innerHTML = '<div class="aibar-pempty">Подбираем…</div>';
      get(API.recommendations).then(function (d) { renderList(d.recommendations || []); })
        .catch(function (e) { q('.aibar-plist').innerHTML = '<div class="aibar-pempty">' + esc(e.message) + '</div>'; });
    } else if (name === 'playlists') {
      renderPlaylists();
    }
  }

  function renderPlaylists() {
    var box = q('.aibar-plist');
    box.innerHTML = '<div class="aibar-pempty">Загрузка…</div>';
    get(API.playlists).then(function (list) {
      if (!list || !list.length) {
        box.innerHTML = '<div class="aibar-pempty">Плейлистов нет</div>';
        return;
      }
      box.innerHTML = list.map(function (p, i) {
        return '<div class="aibar-prow aibar-prow-pl" data-i="' + i + '">' +
          '<div class="aibar-prow-cover"><i class="fas fa-list"></i></div>' +
          '<div class="aibar-prow-main"><div class="aibar-prow-title">' + esc(p.name) + '</div>' +
          '<div class="aibar-prow-sub">' + (p.track_count || (p.tracks || []).length) + ' треков</div></div>' +
          '</div>';
      }).join('');
      Array.prototype.forEach.call(box.querySelectorAll('.aibar-prow-pl'), function (row) {
        row.addEventListener('click', function () {
          openPlaylist = list[parseInt(row.getAttribute('data-i'), 10)];
          showCrumb(openPlaylist.name);
          loadCatalog().then(function () {
            renderList((openPlaylist.tracks || []).map(byId).filter(Boolean));
          });
        });
      });
    }).catch(function (e) {
      box.innerHTML = '<div class="aibar-pempty">' + esc(e.message) + '</div>';
    });
  }

  function runSearch(term) {
    if (!term) { renderList(catalog.tracks); return; }
    get(API.search + '?q=' + encodeURIComponent(term)).then(function (d) {
      renderList(d.results || []);
    }).catch(function () { renderList(catalog.tracks.filter(function (t) {
      return ((t.title || '') + ' ' + (t.artist || '')).toLowerCase().indexOf(term.toLowerCase()) >= 0;
    })); });
  }

  // ------------------------------------------------------------ воспроизведение
  function setNowPlaying(t) {
    q('.aibar-ptitle').textContent = t.title || 'без названия';
    q('.aibar-partist').textContent = [t.artist, t.album].filter(Boolean).join(' · ');
    var cover = q('.aibar-pcover');
    cover.innerHTML = t.has_cover
      ? '<img src="' + API.cover + encodeURIComponent(t.id) + '" alt="">'
      : '<i class="fas fa-music"></i>';
  }

  function markPlayingRow(scroll) {
    var cur = queue[qIndex], hit = null;
    Array.prototype.forEach.call(root.querySelectorAll('.aibar-prow'), function (row) {
      var i = parseInt(row.getAttribute('data-i'), 10);
      var t = row.querySelector('.aibar-prow-title');
      var on = !!(cur && t && t.textContent === (cur.title || 'без названия'));
      row.classList.toggle('playing', on);
      if (on) hit = row;
      void i;
    });
    /* Играющий трек должен быть виден: список на тысячи строк, искать его
       глазами незачем. 'nearest' — прокрутка дёргается только если строки
       действительно нет на экране. */
    if (scroll && hit && hit.scrollIntoView) {
      try { hit.scrollIntoView({ block: 'nearest' }); } catch (e) { /* */ }
    }
  }

  function playFrom(list, index) {
    queue = list.slice();
    qIndex = Math.max(0, Math.min(index, queue.length - 1));
    playCurrent();
  }

  function playCurrent() {
    var t = queue[qIndex];
    if (!t) return;
    /* У торрента и скачанного файла пути в каталоге нет — только прямой URL
       потока, поэтому источник берём из src, когда он указан. */
    audio.src = t.src || window.MediaPlayback.url(t.filepath || t.track_id);
    if (window.AudioEQ && !eq) eq = window.AudioEQ.create(audio);
    if (eq) { eq.init(); eq.resume(); }
    if (bridge) bridge.setTrack({
      id: t.id, filepath: t.filepath, title: t.title, artist: t.artist,
      album: t.album, duration: t.duration, has_cover: t.has_cover, src: t.src || ''
    }, queue.map(function (x) { return { id: x.id, title: x.title, artist: x.artist, filepath: x.filepath, src: x.src || '' }; }), qIndex);
    remoteLive = false;
    setNowPlaying(t);
    markPlayingRow(true);
    adoptSource = null;      // свою дорожку отдаём мини-плееру, как раньше
    audio.play().catch(function () { /* браузер ждёт жеста пользователя */ });
    post(API.playLog, { id: t.id, duration: t.duration || 0 });
    announce();
  }

  function step(delta) {
    if (!queue.length) return;
    if (shuffle) {
      qIndex = Math.floor(Math.random() * queue.length);
    } else {
      qIndex += delta;
      if (qIndex >= queue.length) qIndex = repeat ? 0 : queue.length - 1;
      if (qIndex < 0) qIndex = queue.length - 1;
    }
    playCurrent();
  }

  // ------------------------------------------------- чужое воспроизведение
  /* Сообщаем палитре, что играет: кнопка музыки в шапке чата должна
     показывать это, даже когда самого плеера на экране нет. */
  function announce() {
    var t = remoteLive && remoteState ? remoteState.track : queue[qIndex];
    var playing = remoteLive ? !!(remoteState && remoteState.playing)
                             : !!(audio && !audio.paused);
    var ev;
    try {
      ev = new CustomEvent('aibar:nowplaying', { detail: {
        title: (t && t.title) || '', artist: (t && t.artist) || '', playing: playing
      } });
    } catch (e) { return; }
    document.dispatchEvent(ev);
  }

  function applyRemote(st) {
    remoteLive = true;
    remoteState = st;
    if (!mounted) { announce(); return; }
    if (st.track) setNowPlaying(st.track);
    paintRemote(st);
    if (typeof st.volume === 'number') setVol(st.volume);
    q('.p-play').innerHTML = '<i class="fas fa-' + (st.playing ? 'pause' : 'play') + '"></i>';
    announce();
  }

  /* Прогресс чужого плеера приходит раз в секунду — этого хватает,
     и рывков от него меньше, чем от подтасовки под локальные кадры. */
  function paintRemote(st) {
    var d = st.duration || (st.track && st.track.duration) || 0;
    var pos = st.position || 0;
    q('.aibar-pfill').style.width = (d ? (pos / d) * 100 : 0) + '%';
    q('.p-cur').textContent = fmtTime(pos);
    q('.p-dur').textContent = fmtTime(d);
  }

  function onRemoteGone() {
    if (!remoteLive) return;
    remoteLive = false;
    remoteState = null;
    if (mounted) {
      var cur = queue[qIndex];
      if (cur) setNowPlaying(cur);
      else {
        q('.aibar-ptitle').textContent = 'Ничего не играет';
        q('.aibar-partist').textContent = 'выберите трек';
      }
    }
    announce();
  }

  /* Трек остался без хозяина (окно закрыли) или его передали нам —
     продолжаем с той же позиции, а не с начала. */
  function adoptFrom(r) {
    if (!r || !r.track) return;
    var t = r.track;
    /* src у трека бывает у торрента и скачанного файла: для них пути в
       каталоге нет, и строить URL по filepath бессмысленно. */
    var src = t.src || window.MediaPlayback.url(t.filepath || t.id);
    /* Сюда ведут две дороги сразу: событие storage по media.resume и команда
       handoff. Второй заход не должен перезагружать трек — иначе play()
       первого обрывается (AbortError) и позиция откатывается к нулю. */
    if (audio.getAttribute('src') === src && !audio.paused) {
      remoteLive = false;
      adoptSource = r.from || adoptSource;
      if (typeof r.volume === 'number') setVol(r.volume);
      return;
    }
    var list = (r.queue && r.queue.length) ? r.queue.slice() : [];
    var idx = r.queueIndex || 0;
    if (!list[idx] || String(list[idx].id) !== String(t.id)) {
      list = [{ id: t.id, title: t.title, artist: t.artist, filepath: t.filepath, src: t.src || '' }];
      idx = 0;
    }
    queue = list;
    qIndex = idx;
    audio.src = src;
    if (typeof r.volume === 'number') setVol(r.volume);
    if (window.AudioEQ && !eq) eq = window.AudioEQ.create(audio);
    if (eq) { eq.init(); eq.resume(); }
    if (bridge) bridge.setTrack(t, queue, qIndex);
    remoteLive = false;
    adoptSource = r.from || null;
    if (mounted) { setNowPlaying(t); markPlayingRow(true); }
    var start = Number(r.position) || 0;
    if (start) {
      audio.addEventListener('loadedmetadata', function once() {
        audio.removeEventListener('loadedmetadata', once);
        try { audio.currentTime = start; } catch (e) { /* */ }
      });
    }
    audio.play().catch(function () { /* */ });
    announce();
  }

  /* Просьбы другого плеера. Исполняем только то, что умеем сами, и только
     пока являемся владельцем — это проверяет протокол. */
  function onCommand(action, payload) {
    if (action === 'handoff') { adoptFrom(payload || window.MediaPlayback.resume()); return; }
    if (action === 'toggle') {
      if (audio.paused) { if (eq) { eq.init(); eq.resume(); } audio.play(); } else audio.pause();
      return;
    }
    if (action === 'play') { if (eq) { eq.init(); eq.resume(); } audio.play(); return; }
    if (action === 'pause') { audio.pause(); return; }
    if (action === 'next') { step(1); return; }
    if (action === 'prev') { step(-1); return; }
    if (action === 'seek') {
      var d = audio.duration;
      if (d && payload && payload.at != null) {
        audio.currentTime = Math.max(0, Math.min(1, Number(payload.at) || 0)) * d;
      }
      return;
    }
    if (action === 'volume' && payload) {
      setVol(Number(payload.value) || 0);
      return;
    }
    if (action === 'queue' && payload && payload.index != null) {
      const i = Number(payload.index);
      if (i >= 0 && i < queue.length) { qIndex = i; playCurrent(); }
    }
  }

  /* Кнопки: если трек держит другой плеер — не начинаем вторую дорожку,
     а просим его. Так пауза в палитре реально ставит на паузу музыку
     рабочего стола, и наоборот. */
  /* Адресат — фактический владелец, а не «сосед» по топологии: когда трек
     держит /media, команда, посланная мини-плееру, уходила в пустоту. */
  function remoteOwner() {
    var st = window.MediaPlayback.current();
    return st && st.owner !== 'aibar' ? st.owner : null;
  }

  function wantToggle() {
    if (remoteLive) { bridge.send('toggle', null, remoteOwner()); return; }
    if (!audio.src) {
      if (view === 'tracks') { loadCatalog().then(function () { playFrom(catalog.tracks, 0); }); }
      return;
    }
    if (audio.paused) { if (eq) { eq.init(); eq.resume(); } audio.play(); } else audio.pause();
  }

  function wantStep(delta) {
    if (remoteLive) { bridge.send(delta > 0 ? 'next' : 'prev', null, remoteOwner()); return; }
    step(delta);
  }

  function wantSeek(f) {
    if (remoteLive) { bridge.send('seek', { at: f }, remoteOwner()); return; }
    if (audio.duration) audio.currentTime = f * audio.duration;
  }

  function wantVolume(v) {
    if (remoteLive) { bridge.send('volume', { value: v }, remoteOwner()); return; }
    audio.volume = v;
    if (window.MediaPlayback) window.MediaPlayback.volume.write(v);
  }

  /* Применить громкость извне (handoff/чужое состояние): ставим и свою
     дорожку, и общий ключ, и ползунок — чтобы уровень не «прыгал». */
  function setVol(v) {
    v = Math.max(0, Math.min(1, Number(v)));
    if (!isFinite(v)) return;
    audio.volume = v;
    if (window.MediaPlayback) window.MediaPlayback.volume.write(v);
    var volEl = q('.aibar-pvol');
    if (volEl) volEl.value = Math.round(v * 100);
  }

  // ------------------------------------------------------------- эквалайзер
  function buildEq() {
    var bands = q('.aibar-eq-bands');
    if (bands.childElementCount) return;
    if (!window.AudioEQ) { bands.innerHTML = '<div class="aibar-pempty">Модуль эквалайзера не загружен</div>'; return; }
    if (!eq) eq = window.AudioEQ.create(audio);
    bands.innerHTML = window.AudioEQ.BANDS.map(function (hz, i) {
      return '<div class="aibar-eq-band">' +
             '<input type="range" min="-12" max="12" step="1" value="0" data-band="' + i + '">' +
             '<label>' + (hz >= 1000 ? (hz / 1000) + 'k' : hz) + '</label></div>';
    }).join('');
    var g = eq.gains();
    Array.prototype.forEach.call(bands.querySelectorAll('input[data-band]'), function (s) {
      s.value = g[parseInt(s.getAttribute('data-band'), 10)] || 0;
      s.addEventListener('input', function () {
        eq.setGain(parseInt(s.getAttribute('data-band'), 10), parseFloat(s.value));
      });
    });
    var pre = q('.aibar-eq-preamp');
    pre.value = eq.getPreamp();
    q('.aibar-eq-preamp-val').textContent = eq.getPreamp() + ' dB';
    pre.addEventListener('input', function () {
      eq.setPreamp(parseFloat(pre.value));
      q('.aibar-eq-preamp-val').textContent = pre.value + ' dB';
    });
    q('.aibar-eq-preset').addEventListener('change', function (e) {
      if (!e.target.value) return;
      eq.applyPreset(e.target.value);
      var g2 = eq.gains();
      Array.prototype.forEach.call(bands.querySelectorAll('input[data-band]'), function (s) {
        s.value = g2[parseInt(s.getAttribute('data-band'), 10)] || 0;
      });
    });
    q('.aibar-eq-bypass input').addEventListener('change', function (e) {
      eq.setBypass(e.target.checked);
    });
  }

  function toggleEq() {
    var panel = q('.aibar-eq');
    var show = panel.style.display === 'none';
    panel.style.display = show ? '' : 'none';
    q('.aibar-eqbtn').classList.toggle('on', show);
    if (show) {
      buildEq();
      if (eq) { eq.init(); eq.resume(); eq.startVisualizer(q('.aibar-eq-viz')); }
    } else if (eq) {
      eq.stopVisualizer();
    }
  }

  // ----------------------------------------------------------------- прогресс
  function bindProgress() {
    var seek = q('.aibar-pseek');
    var set = function (e) {
      var r = seek.getBoundingClientRect();
      var f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
      wantSeek(f);
      q('.aibar-pfill').style.width = (f * 100) + '%';
    };
    seek.addEventListener('mousedown', function (e) { seeking = true; set(e); });
    document.addEventListener('mousemove', function (e) { if (seeking) set(e); });
    document.addEventListener('mouseup', function () { seeking = false; });
  }

  // ------------------------------------------------------------------- монтаж
  /* Звук и мост к протоколу создаются сразу, без интерфейса: окно палитры
     может стоять в виде чата, а музыка — играть. Без этого «подхватить трек
     за мини-плеером» было бы нечем, пока пользователь не откроет плеер. */
  function boot() {
    if (booted || !window.MediaPlayback) return;
    booted = true;

    audio = new Audio();
    audio.preload = 'metadata';
    try {
      var legacy = parseFloat(localStorage.getItem('aibar.volume') || '0.8');
      audio.volume = window.MediaPlayback ? window.MediaPlayback.volume.read(legacy) : legacy;
    } catch (e) { /* */ }

    bridge = window.MediaPlayback.attach(audio, 'aibar', {
      render: function (track) { if (mounted && track) setNowPlaying(track); },
      state: function (st) { applyRemote(st); },
      command: onCommand,
      adopt: function (r) { adoptFrom(r); },
      remoteGone: onRemoteGone
    });

    var st = window.MediaPlayback.current();
    if (st && st.owner !== 'aibar' && window.MediaPlayback.isFresh(st)) applyRemote(st);
  }

  function mount(container) {
    if (mounted && root === container) return;
    boot();
    root = container;
    root.className = 'aibar-player';
    root.innerHTML = markup();
    mounted = true;

    Array.prototype.forEach.call(root.querySelectorAll('[data-ptab]'), function (b) {
      b.addEventListener('click', function () { setView(b.getAttribute('data-ptab')); });
    });
    q('.aibar-eqbtn').addEventListener('click', toggleEq);

    var search = q('.aibar-psearch-in'), timer = null;
    search.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () { runSearch(search.value.trim()); }, 300);
    });

    q('.p-play').addEventListener('click', wantToggle);
    q('.p-prev').addEventListener('click', function () { wantStep(-1); });
    q('.p-next').addEventListener('click', function () { wantStep(1); });
    q('.p-shuffle').addEventListener('click', function () {
      shuffle = !shuffle; q('.p-shuffle').classList.toggle('on', shuffle);
    });
    q('.p-repeat').addEventListener('click', function () {
      repeat = !repeat; q('.p-repeat').classList.toggle('on', repeat);
    });

    var vol = q('.aibar-pvol');
    vol.value = Math.round((audio.volume || 0.8) * 100);
    vol.addEventListener('input', function () {
      var v = Math.max(0, Math.min(1, vol.value / 100));
      wantVolume(v);
      try { localStorage.setItem('aibar.volume', String(v)); } catch (e) { /* */ }
    });

    /* Кнопка отражает только нашу дорожку: пока играет другой плеер,
       состояние рисует applyRemote. */
    audio.addEventListener('play', function () {
      if (remoteLive) return;
      q('.p-play').innerHTML = '<i class="fas fa-pause"></i>';
      announce();
    });
    audio.addEventListener('pause', function () {
      if (remoteLive) return;
      q('.p-play').innerHTML = '<i class="fas fa-play"></i>';
      announce();
    });
    audio.addEventListener('ended', function () { if (repeat || qIndex < queue.length - 1) step(1); });
    audio.addEventListener('timeupdate', function () {
      if (seeking || remoteLive) return;
      var d = audio.duration || 0;
      q('.aibar-pfill').style.width = (d ? (audio.currentTime / d) * 100 : 0) + '%';
      q('.p-cur').textContent = fmtTime(audio.currentTime);
      q('.p-dur').textContent = fmtTime(d);
    });

    bindProgress();

    // Если музыка уже играет в другом месте — показываем её, не начиная заново.
    var remote = window.MediaPlayback && window.MediaPlayback.current();
    if (remote && remote.owner !== 'aibar' && window.MediaPlayback.isFresh(remote)) applyRemote(remote);

    loadFavorites();
  }

  function open() {
    if (!root) return;
    setView(view);
    var st = window.MediaPlayback && window.MediaPlayback.current();
    if (st && st.owner !== 'aibar' && window.MediaPlayback.isFresh(st)) applyRemote(st);
  }

  /* Перенос звука за видимым окном. Оболочка сообщает о показе и скрытии
     окна палитры: скрыли — возвращаем трек туда, откуда пришли (мини-плеер
     рабочего стола или /media), показали — просим владельца отдать его нам. */
  function onWindowHidden() {
    if (bridge && audio && !audio.paused && bridge.isOwner()) {
      bridge.handoff(adoptSource || 'miniplayer');
    }
  }

  function onWindowShown() {
    if (!bridge) return;
    var st = window.MediaPlayback.current();
    if (st && st.owner !== 'aibar' && window.MediaPlayback.isFresh(st) && st.playing) {
      bridge.send('handoff', null, st.owner);
    }
  }

  function pause() { if (audio && !audio.paused) audio.pause(); }
  function isPlaying() {
    if (remoteLive && remoteState) return !!remoteState.playing;
    return !!audio && !audio.paused;
  }
  /* Своя дорожка, без учёта чужой: для диагностики и тестов. */
  function selfPlaying() { return !!audio && !audio.paused; }
  function nowPlaying() {
    var t = remoteLive && remoteState ? remoteState.track : queue[qIndex];
    return t || null;
  }

  if (document.body && document.body.classList.contains('aibar-solo')) boot();

  window.AiBarPlayer = {
    boot: boot,
    mount: mount,
    open: open,
    pause: pause,
    isPlaying: isPlaying,
    selfPlaying: selfPlaying,
    nowPlaying: nowPlaying,
    onWindowHidden: onWindowHidden,
    onWindowShown: onWindowShown
  };
})();
