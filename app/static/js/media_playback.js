/* ============================================================================
   Общий проигрываемый трек.
   ----------------------------------------------------------------------------
   Музыку в приложении умеют играть три места: страница /media, мини-плеер
   рабочего стола (desktop.html) и плеер окна палитры. Если каждое играет
   само по себе, из колонок лезут два трека одновременно.

   Протокол живёт в localStorage (окна одного origin, событие storage приходит
   в соседнее окно) и состоит из трёх ключей:

     media.playback — кто играет и что именно: владелец обновляет состояние
       на каждое своё действие и раз в HEARTBEAT_MS. Свежим оно считается
       FRESH_MS, дальше считается, что владелец исчез.
     media.command  — просьба к другому плееру (пауза, следующий, перемотка).
       Без этого кнопки мини-плеера управляли бы только своим <audio>, а не
       тем, что реально играет.
     media.resume   — «чем продолжить»: трек и позиция. Пишется владельцем
       периодически и обязательно при передаче/уходе страницы, чтобы трек
       пережил закрытие окна.

   Правило одного звука: играет владелец, второй молчит и показывает тот же
   трек. Передать трек = handoff: прежний владелец сохраняет resume с флагом
   handoff, гасит себя и убирает media.playback; тот, кто увидит resume с этим
   флагом, подхватывает трек с той же позиции.
   ========================================================================== */
(function () {
  'use strict';

  var KEY = 'media.playback';
  var CMD = 'media.command';
  var RESUME = 'media.resume';
  var VOLKEY = 'mediaVolume';

  /* Ключи состояния — в пространстве профиля. У профилей один origin и один
     localStorage, поэтому трек, игравший у одного профиля, подхватывался
     другим (замер: приватная песня test1 появлялась в плеере test2). Кука
     профиля HttpOnly, из JS её не прочитать — префикс приходит от сервера
     (window.__PROFILE_STORAGE_PREFIX). Нет префикса — ведём себя как раньше. */
  var PROFILE_PREFIX = (function () {
    try {
      var p = window.__PROFILE_STORAGE_PREFIX;
      return typeof p === 'string' ? p : '';
    } catch (e) { return ''; }
  })();
  function scopeKey(name) { return PROFILE_PREFIX + name; }
  KEY = scopeKey('media.playback');
  CMD = scopeKey('media.command');
  RESUME = scopeKey('media.resume');

  var HEARTBEAT_MS = 1000;      // как часто владелец напоминает о себе
  var FRESH_MS = 3000;          // столько верим чужому состоянию без обновлений
  var CMD_TTL = 5000;           // команда старше этого не исполняется
  var RESUME_TTL = 2 * 3600000; // resume старше двух суток не подхватывается
  var HANDOFF_TTL = 120000;     // передачу принимаем, пока она свежая
  var RESUME_SAVE_MS = 5000;    // как часто владелец страхует позицию

  var PEERS = { aibar: 'miniplayer', miniplayer: 'aibar', media: 'miniplayer', srv: 'miniplayer' };

  var me = null;                // {audio, owner, hooks, mutedUntil, loop, ...}

  function encodePath(p) {
    return btoa(unescape(encodeURIComponent(p)))
      .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function read(key) {
    try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { return null; }
  }

  function write(key, v) {
    try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) { /* переполнение */ }
  }

  /* Единая громкость для всех трёх плееров: одно значение на всё приложение,
     чтобы уровень не «прыгал» при переключении между /media, мини-плеером и
     палитрой. fallback — только если общего значения ещё нет. */
  function readVolume(fallback) {
    var v = parseFloat(localStorage.getItem(VOLKEY));
    if (!isFinite(v) || v < 0 || v > 1) return (typeof fallback === 'number' ? Math.max(0, Math.min(1, fallback)) : 1);
    return v;
  }
  function writeVolume(v) {
    v = Math.max(0, Math.min(1, Number(v)));
    if (!isFinite(v)) return;
    try { localStorage.setItem(VOLKEY, String(v)); } catch (e) { /* переполнение */ }
  }

  function ageOf(st) { return Date.now() - ((st && (st.ts || st.updatedAt)) || 0); }

  /* Очередь ездит вместе с состоянием, но каталог на тысячи строк каждый
     раз писать в localStorage бессмысленно: берём начало, а если играем
     дальше — только текущий трек. */
  var QUEUE_MAX = 200;
  function trimQueue() {
    var list = me.queue || [];
    if (list.length <= QUEUE_MAX) return list;
    if (me.queueIndex >= QUEUE_MAX) return [list[me.queueIndex]].filter(Boolean);
    return list.slice(0, QUEUE_MAX);
  }

  function isFresh(st) { return !!st && ageOf(st) < FRESH_MS; }

  function peerOf(owner) { return PEERS[owner] || 'any'; }

  function call(name, a, b) {
    if (me && me.hooks && typeof me.hooks[name] === 'function') {
      try { me.hooks[name](a, b); } catch (e) { /* чужой хук не должен ронять протокол */ }
    }
  }

  // ------------------------------------------------------------- состояние
  function saveResume(playing, extra) {
    if (!me) return null;
    var a = me.audio;
    var t = me.track || (read(KEY) || {}).track || null;
    if (!t) return null;
    var r = {
      from: me.owner,
      updatedAt: Date.now(),
      playing: !!playing,
      handoff: !!(extra && extra.handoff),
      /* Кому именно отдаём: иначе передачу подхватят оба соседних плеера
         и начнут перебивать друг друга. */
      to: (extra && extra.to) || null,
      track: t,
      position: a.currentTime || 0,
      duration: a.duration || t.duration || 0,
      volume: a.volume,
      queue: trimQueue(),
      queueIndex: me.queueIndex || 0
    };
    write(RESUME, r);
    return r;
  }

  function publish(playing) {
    if (!me) return;
    var a = me.audio;
    var t = me.track || (read(KEY) || {}).track || null;
    if (!t) return;
    me.lastPub = Date.now();
    write(KEY, {
      owner: me.owner,
      ts: Date.now(),
      playing: !!playing,
      track: t,
      position: a.currentTime || 0,
      duration: a.duration || t.duration || 0,
      volume: a.volume,
      queue: trimQueue(),
      queueIndex: me.queueIndex || 0
    });
    // Позицию страхуем не на каждый кадр, но достаточно часто, чтобы
    // закрытие окна стоило максимум нескольких секунд трека.
    /* Смена трека — страховка обязательна сразу: до следующего тика запись
       «чем продолжить» держала уже брошенную песню, и плеер, который
       подхватывал трек в этом окне, запускал другую композицию. */
    var tid = t.id || t.filepath || '';
    if (me.lastResumeTrack !== tid) { me.lastResumeTrack = tid; me.lastResume = 0; }
    if (playing && (!me.lastResume || Date.now() - me.lastResume > RESUME_SAVE_MS)) {
      me.lastResume = Date.now();
      saveResume(true);
    }
  }

  /* Один цикл на всё время жизни плеера: публикует наше состояние, пока
     играем, и разбирается с чужим. Отдельные «старт/стоп» сердца ломались
     в гонке событий: трек играл, а состояние не публиковалось, и сосед
     считал его осиротевшим. */
  function startLoops() {
    if (!me || me.loop) return;
    me.loop = setInterval(function () {
      if (!me) return;
      /* Играем мы — значит мы и владелец: публикуем независимо от служебных
         флагов, иначе застрявший флаг оставляет трек без состояния. */
      if (!me.audio.paused) publish(true);
      sync();
    }, HEARTBEAT_MS);
  }

  /** Чужая игра: замолчать и показать то, что играет кто-то другой.
      Пауза программная — публиковать её нельзя, иначе владельцы начнут
      переключать друг друга по кругу. */
  function followRemote(st) {
    if (!me) return;
    call('render', st.track, st);
    call('state', st);
    /* Свою дорожку, запущенную только что, не глушим: наше состояние
       всё равно опубликует цикл, и замолчать должен тот, кто играл до
       нас. Иначе передача трека превращается в перебивание друг друга. */
    if (me.lastLocal && Date.now() - me.lastLocal < 2000) return;
    if (st.playing && !me.audio.paused) {
      me.mutedUntil = Date.now() + 150;
      try { me.audio.pause(); } catch (e) { /* */ }
    }
  }

  /* Живого владельца нет, но трек оборвался на середине — значит окно
     владельца закрыли. Подхватываем, если resume просит именно этого. */
  function maybeAdopt(st) {
    if (!me || !me.audio.paused) return;
    var r = read(RESUME);
    if (!r || r.from === me.owner || !r.playing) return;
    if (ageOf(r) > RESUME_TTL) return;
    var handedOff = !!r.handoff && ageOf(r) < HANDOFF_TTL && (!r.to || r.to === me.owner);
    /* Владелец «пропал» — это про недавний сбой (закрыли окно), а не про
       запись недельной давности: иначе плеер сам заводил старый трек при
       входе на страницу после перезагрузки/рестарта сервера. */
    var ownerVanished = !!st && st.owner !== me.owner && !isFresh(st) && st.playing
      && ageOf(r) < 30000;
    if (!handedOff && !ownerVanished) return;
    if (me.adoptGuard && Date.now() - me.adoptGuard < 2000) return;
    me.adoptGuard = Date.now();
    write(RESUME, Object.assign({}, r, { handoff: false }));
    me.following = null;
    call('adopt', r);
  }

  function handleCommand(cmd) {
    if (!me || !cmd || cmd.from === me.owner) return;
    if (Date.now() - (cmd.ts || 0) > CMD_TTL) return;
    if (cmd.to && cmd.to !== me.owner && cmd.to !== 'any') return;
    // Исполняет тот, кто сейчас играет. Передача трека — тоже команда
    // играющему: «отдай», а не «возьми» (берущий находит трек по resume).
    if (!isLiveOwner()) return;
    call('command', cmd.action, cmd.payload || null);
  }

  function isLiveOwner() {
    var st = read(KEY);
    return !!st && st.owner === me.owner && isFresh(st);
  }

  /** Один и тот же разбор состояния и для события storage, и для опроса:
      событие не приходит в то же окно, а после перезагрузки страницы его
      вообще можно пропустить. */
  function sync() {
    if (!me) return;
    var st = read(KEY);
    if (st && st.owner !== me.owner && isFresh(st)) {
      me.following = st;
      followRemote(st);
      return;
    }
    if (st && st.owner === me.owner) { me.following = null; return; }
    maybeAdopt(st);
    /* Живого чужого плеера больше нет: интерфейс должен перестать
       показывать его прогресс. */
    call('remoteGone', st);
  }

  var listening = false;
  function listenOnce() {
    if (listening) return;
    listening = true;
    window.addEventListener('storage', function (e) {
      if (!me) return;
      if (e.key === KEY) {
        var st;
        try { st = JSON.parse(e.newValue || 'null'); } catch (err) { return; }
        if (st && st.owner !== me.owner) { me.following = st; followRemote(st); }
        else if (!st) { maybeAdopt(null); call('remoteGone', null); }
        return;
      }
      if (e.key === CMD) {
        var cmd;
        try { cmd = JSON.parse(e.newValue || 'null'); } catch (err2) { return; }
        handleCommand(cmd);
        return;
      }
      /* Передачу трека принимаем сразу по записи, без ожидания секундного
         опроса: иначе после Shift+Space музыка молчит до следующего тика. */
      if (e.key === RESUME) {
        var r;
        try { r = JSON.parse(e.newValue || 'null'); } catch (err3) { return; }
        if (r && r.playing && r.handoff && r.from !== me.owner
            && ageOf(r) < HANDOFF_TTL && (!r.to || r.to === me.owner)) {
          maybeAdopt(read(KEY));
        }
      }
    });

    /* Страницу закрывают — страхуем позицию сразу, не дожидаясь очереди
      публикации: трек должен продолжиться там, где его подхватят. */
    window.addEventListener('pagehide', function () {
      if (me && !me.audio.paused) saveResume(true);
    });
  }

  /** Привязать плеер к протоколу.
      hooks.render(track, state) — показать чужой трек;
      hooks.state(state)        — зеркалить прогресс и паузу;
      hooks.command(action, payload) — другой плеер просит действие;
      hooks.adopt(resume)       — трек остался без хозяина, подхвати его. */
  function attach(audio, owner, hooks) {
    me = { audio: audio, owner: owner, hooks: hooks || {}, track: null,
           queue: [], queueIndex: 0, mutedUntil: 0, loop: null,
           lastPub: 0, lastResume: 0, lastLocal: 0, following: null, adoptGuard: 0 };
    listenOnce();

    audio.addEventListener('play', function () {
      me.lastLocal = Date.now();
      publish(true);
    });
    audio.addEventListener('playing', function () {
      me.lastLocal = Date.now();
      publish(true);
    });
    audio.addEventListener('pause', function () {
      if (Date.now() >= (me.mutedUntil || 0)) { publish(false); saveResume(false); }
    });
    audio.addEventListener('ended', function () { publish(false); });
    // timeupdate приходит несколько раз в секунду: состояние достаточно
    // обновлять раз в HEARTBEAT_MS, иначе localStorage пишется на каждый кадр.
    audio.addEventListener('timeupdate', function () {
      if (audio.paused) return;
      if (me.lastPub && Date.now() - me.lastPub < HEARTBEAT_MS) return;
      publish(true);
    });

    /* Цикл состояния запускаем сразу: даже если публикация пропущена из-за
       гонки событий, через секунду она всё равно появится, и сосед не
       решит, что трек осиротел. */
    startLoops();

    return {
      setTrack: function (track, queue, index) {
        me.track = track;
        if (queue) me.queue = queue;
        me.queueIndex = index || 0;
      },
      publish: publish,
      state: read.bind(null, KEY),
      resume: read.bind(null, RESUME),
      /* Кто-то другой держит трек — брать его себе или нет. */
      remote: function () {
        var st = read(KEY);
        if (!st || st.owner === owner || !isFresh(st)) return null;
        return st;
      },
      isOwner: isLiveOwner,
      peer: function () { return peerOf(owner); },
      /* Просьба к другому плееру: он исполнит, пока является владельцем.
         Второй аргумент — явный адресат (когда знаем фактического владельца,
         а не только «соседа» по топологии). */
      send: function (action, payload, to) {
        write(CMD, { ts: Date.now(), from: owner, to: to || peerOf(owner),
                     action: action, payload: payload || null });
      },
      /* Отдать трек другому: позиция сохранена, нас в списке играющих нет.
         Сначала гасим себя и убираем состояние, и только потом пишем resume.
         Обратный порядок давал гонку: принявший успевал опубликовать себя
         владельцем, а наш removeItem(KEY) стирал уже его запись.

         Команду handoff здесь не шлём: получатель узнаёт о передаче из
         media.resume (событие storage или секундный опрос). Команда,
         адресованная тому, кто ещё не владелец, в лучшем случае
         отбрасывалась проверкой isLiveOwner, а в худшем — приходила уже
         после подхвата, и принявший «отдавал» трек обратно: музыка
         перескакивала между окнами по кругу. */
      handoff: function (to) {
        var target = to || peerOf(owner);
        me.mutedUntil = Date.now() + 150;
        try { me.audio.pause(); } catch (e) { /* */ }
        try { localStorage.removeItem(KEY); } catch (e) { /* */ }
        return saveResume(true, { handoff: true, to: target });
      },
      stop: function () { publish(false); saveResume(false); }
    };
  }

  /* ---------------------------------------------------------------------
     Внешние команды (очередь сервера: POST /api/media/player-command).
     Агент или скрипт не видит браузерного localStorage, поэтому сервер
     хранит очередь команд, а все страницы с этим скриптом забирают их
     опросом. Исполняет ровно одна страница — та, что заявила право через
     media.srv.claim (последняя запись побеждает, проигравшие проверяют
     это через короткую выдержку). Дальше команда идёт по обычным правилам
     протокола: живому владельцу — через media.command, осиротевшему треку
     — через handoff-resume. Если на заявившей странице нет плеера, играет
     «невидимка» (обычный <audio> вне DOM), публикующий состояние как
     владелец 'srv'; любой проснувшийся настоящий плеер его глушит. */
  var SRV_CLAIM = 'media.srv.claim';
  var SRV_POLL_MS = 2000, SRV_HOLD_MS = 350, SRV_CMD_TTL_S = 300;
  var srvSince = null, srvToken = Math.random().toString(36).slice(2, 10);
  var ghost = null, ghostTimer = null;

  function srvClaim(cmdId) {
    return new Promise(function (done) {
      write(SRV_CLAIM, { id: cmdId, token: srvToken, ts: Date.now() });
      setTimeout(function () {
        var c = read(SRV_CLAIM);
        done(!!c && c.id === cmdId && c.token === srvToken);
      }, SRV_HOLD_MS);
    });
  }

  function ghostStop() {
    if (ghost) { try { ghost.audio.pause(); } catch (e) { /* */ } }
    ghost = null;
    if (ghostTimer) { clearInterval(ghostTimer); ghostTimer = null; }
  }

  function ghostPublish(playing) {
    if (!ghost) return;
    if (me && isLiveOwner()) return; // пусть говорит настоящий плеер
    write(KEY, {
      owner: 'srv', ts: Date.now(), playing: !!playing, track: ghost.track,
      position: ghost.audio.currentTime || 0,
      duration: ghost.audio.duration || ghost.track.duration || 0,
      volume: ghost.audio.volume, queue: [ghost.track], queueIndex: 0
    });
  }

  function ghostHandle(action, payload) {
    if (!ghost) return;
    var a = ghost.audio, d;
    if (action === 'pause') { a.pause(); }
    else if (action === 'play') { a.play().catch(function () { /* */ }); }
    else if (action === 'toggle') { if (a.paused) a.play().catch(function () { /* */ }); else a.pause(); }
    else if (action === 'stop') { a.pause(); try { a.currentTime = 0; } catch (e) { /* */ } }
    else if (action === 'seek' && payload && payload.at != null) {
      d = a.duration;
      if (d && isFinite(d)) a.currentTime = Math.max(0, Math.min(1, Number(payload.at) || 0)) * d;
    }
    else if (action === 'volume' && payload) {
      a.volume = Math.max(0, Math.min(1, Number(payload.value) || 0));
    }
    else if (action === 'next' || action === 'prev') { ghostStop(); }
  }

  function ghostPlay(track) {
    if (!ghost) {
      ghost = { audio: new Audio(), track: track };
      ghost.audio.preload = 'metadata';
      ghost.audio.volume = readVolume(0.8);
      ghost.audio.addEventListener('ended', function () { ghostPublish(false); });
      ghostTimer = setInterval(function () {
        if (!ghost) return;
        var st = read(KEY);
        /* Проснулся настоящий плеер — невидимка уходит первым. */
        if (st && st.owner !== 'srv' && isFresh(st)) { ghostStop(); return; }
        if (!ghost.audio.paused) ghostPublish(true);
      }, HEARTBEAT_MS);
    } else {
      ghost.track = track;
    }
    ghost.audio.src = '/api/media/stream/' + encodePath(track.filepath);
    ghost.audio.play().catch(function () { /*autoplay заблокирован — публикуемся и так*/ });
    ghostPublish(true);
  }

  function srvApply(cmd) {
    if (!cmd || !cmd.action) return;
    var action = cmd.action === 'resume' ? 'play' : cmd.action;
    if (action === 'play' && !(cmd.payload && cmd.payload.track && cmd.payload.track.filepath)) return;
    if (cmd.ts && Date.now() / 1000 - cmd.ts > SRV_CMD_TTL_S) return;
    srvClaim(cmd.id).then(function (won) {
      if (!won) return;
      var st = read(KEY);
      var owner = isFresh(st) ? st.owner : null;
      if (owner && owner !== 'srv') {
        /* Команда живому владельцу: у себя — сразу в хук, в чужое окно —
           через media.command (дальше его отработает handleCommand). */
        if (me && me.owner === owner) { call('command', action, cmd.payload || null); }
        else { write(CMD, { ts: Date.now(), from: 'srv', to: owner, action: action, payload: cmd.payload || null }); }
        return;
      }
      if (owner === 'srv') {
        if (action === 'play' && !ghost) ghostPlay(cmd.payload.track);
        else ghostHandle(action, cmd.payload || null);
        return;
      }
      if (action !== 'play') return;
      var track = cmd.payload.track;
      if (me) {
        /* Плеер на странице простой, но живой: отдаём ему трек как при
           handoff — его же цикл подхватит resume и заиграет с нуля. */
        write(RESUME, {
          from: 'srv', updatedAt: Date.now(), playing: true, handoff: true,
          to: me.owner, track: track, position: 0, duration: track.duration || 0,
          volume: readVolume(0.8), queue: [track], queueIndex: 0
        });
      } else {
        ghostPlay(track);
      }
    });
  }

  window.addEventListener('storage', function (e) {
    if (!ghost) return;
    if (e.key === KEY) {
      var st;
      try { st = JSON.parse(e.newValue || 'null'); } catch (err) { return; }
      if (st && st.owner !== 'srv' && isFresh(st)) ghostStop();
      return;
    }
    if (e.key === CMD) {
      var c;
      try { c = JSON.parse(e.newValue || 'null'); } catch (err2) { return; }
      if (c && c.action && (c.to === 'srv' || c.to === 'any')) ghostHandle(c.action, c.payload || null);
    }
  });

  setInterval(function () {
    fetch('/api/media/player-commands?since=' + (srvSince || 0), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var list = d.commands || [];
        if (srvSince === null) { srvSince = d.latest || 0; return; } // история при старте не переигрывается
        list.forEach(srvApply);
        if (d.latest != null && d.latest > srvSince) srvSince = d.latest;
      })
      .catch(function () { /* сервер недоступен — следующий тик */ });
  }, SRV_POLL_MS);

  window.MediaPlayback = {
    KEY: KEY,
    CMD: CMD,
    RESUME: RESUME,
    /* Профильное имя ключа для прочих плеерных записей (miniplayer). */
    key: scopeKey,
    attach: attach,
    url: function (filepath) { return '/api/media/stream/' + encodePath(filepath); },
    encodePath: encodePath,
    current: function () { return read(KEY); },
    resume: function () { return read(RESUME); },
    isFresh: isFresh,
    volume: { read: readVolume, write: writeVolume },
    /* Что происходит внутри собственного плеера — для диагностики. */
    debug: function () {
      return me ? { owner: me.owner, hasTrack: !!me.track, playing: !me.audio.paused,
                    loop: !!me.loop, muted: Date.now() < (me.mutedUntil || 0), following: !!me.following } : null;
    }
  };
})();
