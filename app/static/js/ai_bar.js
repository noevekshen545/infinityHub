/* ============================================================================
   Чат с Infinity Agent: палитра быстрого ввода (Shift+Space) и встроенный
   чат разбора ошибок в админ-панели.
   ----------------------------------------------------------------------------
   Подключается в трёх местах:
     • app/templates/desktop.html  — оверлей поверх рабочего стола;
     • app/templates/ai_bar.html   — отдельное прозрачное окно обёртки
       Electron (Shift+Space при свернутом приложении);
     • app/templates/page_admin.html — чат внутри карточки (data-aibar-mount).
   Логика одна; различается только то, куда монтируется тред и кто его
   открывает.

   Переписка берётся у агента (GET /api/agentai/session/<key>), а не из
   localStorage: окно рабочего стола и окно палитры — один origin, и,
   храня историю в браузере, они затрали бы друг друга. Ключ сессии
   фиксированный, поэтому оба окна говорят в одной беседе.

   Палитра умеет приложить к запросу экран: целиком, выделенную область или
   кадр «живого экрана», а также надиктовать запрос голосом (whisper).
   ========================================================================== */
(function () {
  'use strict';

  var API = '/api/agentai/ask';
  var HISTORY = '/api/agentai/session/';
  var STT_RUN = '/api/stt/transcribe';
  var STT_STATUS = '/api/stt/status';
  var CONFIG = '/api/config';
  var PERSONAS = '/api/agentai/personas';
  var SESSIONS = '/api/agentai/sessions';
  // Пинг об изменении беседы: событие storage доходит до других окон и
  // вкладок этого origin, и они перезагружают тред.
  var REV = 'aibar.rev.';
  // Режим «говорить по хоткею» включается сам в палитре и переживает
  // перезагрузку окна — иначе каждый вызов пришлось бы тыкать мышью.
  var VOICE_ON = 'aibar.voice.';
  // Выбранная личность агента (профиль Hermes). У каждой личности своя
  // переписка, поэтому ключ сессии храним вместе с именем профиля.
  var PERSONA_KEY = 'aibar.persona.';
  // Активная сессия агента. Хранится в localStorage: окно Electron и
  // палитра на рабочем столе живут на одном origin и видят одно и то же.
  var ACTIVE_KEY = 'aibar.active.';

  /* Профиль приложения (из cookie). От него зависят и личность агента, и
     диалоги: сменили профиль в системе — получили другую личность и
     другую переписку. */
  function appProfile() {
    try {
      var m = document.cookie.match(/(?:^|;\s*)infinity_profile=([^;]*)/);
      return m && m[1] ? decodeURIComponent(m[1]) : 'default';
    } catch (e) { return 'default'; }
  }
  var PROFILE_ID = appProfile();
  function personaKey() { return PERSONA_KEY + PROFILE_ID; }
  function activeKey() { return ACTIVE_KEY + PROFILE_ID + '.' + persona; }
  function revKey() { return REV + PROFILE_ID + '.' + persona; }

  var DEFAULT_KEY = 'quickbar';
  var DEFAULT_TITLE = 'Быстрый запрос';

  // Пауза молчания, после которой диктовка считается законченной.
  var SILANCE_MS = 3500;
  // Столько же ждём ответа агента без единого события, прежде чем
  // предупредить: локальная модель может быть не запущена.
  var STALL_MS = 90000;

  var overlay, panel, thread, logEl, threadInput, trayBtn, emptyEl;
  var input = null;
  var sessionKey = DEFAULT_KEY;
  var sessionTitle = DEFAULT_TITLE;
  var inline = false;                  // чат встроен в страницу, не оверлей
  var busy = false;                    // агент сейчас отвечает
  var pending = null;                  // элемент сообщения агента в логе
  var currentAbort = null;             // активный запрос: кнопка «Остановить»
  var solo = false;                    // режим отдельного окна

  var attachments = [];                // скриншоты, приложенные к следующему запросу
  var liveScreen = false;              // прикладывать кадр экрана к каждому запросу
  var voiceMode = false;               // Shift+Space сразу начинает диктовку
  var rec = null;                      // активная запись Microphone
  var pinned = false;                  // окно палитры не прячется по потере фокуса
  var view = 'chat';                   // что показано в окне: чат, плеер или мессенджер
  var sttReady = false;
  var sttLang = 'ru';
  var persona = 'default';             // личность агента (профиль Hermes)
  var personas = [];                   // список личностей для выбора
  var sessions = [];                   // сессии агента (переписки)
  var activeSession = '';              // активная сессия агента (её id)
  var sessionsExpanded = false;        // «…» раскрыл весь список сессий

  // ---------------------------------------------------------------- utils
  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /** Минимальная разметка ответа: блоки кода, `код`, **жирный**, переносы. */
  function fmt(s) {
    var parts = esc(s).split(/```([\s\S]*?)```/g);
    var out = '';
    for (var i = 0; i < parts.length; i++) {
      if (i % 2 === 1) {
        out += '<pre style="margin:6px 0;padding:8px 10px;background:rgba(0,0,0,.07);'
             + 'border-radius:6px;overflow-x:auto"><code>' + parts[i].replace(/^\w*\n/, '') + '</code></pre>';
      } else {
        out += parts[i]
          .replace(/`([^`\n]+)`/g, '<code>$1</code>')
          .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
      }
    }
    return out;
  }

  /** Источник истины — агент. Локальную копию держать нельзя: два окна
   *  одного origin перезаписывали бы друг друга. */
  function loadTranscript() {
    if (!logEl) return Promise.resolve();
    var url = HISTORY + encodeURIComponent(sessionKey) +
              '?title=' + encodeURIComponent(sessionTitle) +
              '&profile=' + encodeURIComponent(persona);
    if (activeSession) url += '&session_id=' + encodeURIComponent(activeSession);
    return fetch(url, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : { messages: [] }; })
      .then(function (d) {
        var msgs = (d && d.messages) || [];
        logEl.innerHTML = '';
        msgs.forEach(function (m) {
          addMsg(m.role === 'user' ? 'user' : 'agent', m.content, true);
        });
        if (msgs.length) setTray('done');
        scrollLog();
        /* Агент не отвечает: он ещё поднимается или выключен. Говорим
           об этом прямо — иначе переписка выглядит просто пустой. */
        if (d && d.agent_ready === false) {
          addError('Агент сейчас не отвечает. Если он только запускается — подождите минуту и повторите.');
        }
        /* Переписка часто приходит уже ПОСЛЕ того, как окно показали:
           в Electron первое нажатие Shift+Space открывает окно раньше,
           чем отвечает сервер, и на экране остаётся только строка ввода.
           Теперь, когда переписка на месте, показываем тред. */
        var wasOpen = !!(overlay && overlay.classList.contains('open'));
        if (window.console && console.log) {
          console.log('[aibar] история пришла: ' + msgs.length +
                      ', окно открыто: ' + wasOpen +
                      ', сессия: ' + (activeSession || 'по ключу'));
        }
        if (msgs.length && wasOpen) {
          showThread();
        }
      })
      .catch(function () { /* сервер не ответил — оставляем то, что есть */ });
  }

  /** Другое окно (или вкладка) изменило беседу — перечитать. */
  function bumpRevision() {
    try { localStorage.setItem(revKey(), String(Date.now())); } catch (e) { /* */ }
  }

  function watchRevision() {
    window.addEventListener('storage', function (e) {
      if (!e.key) return;
      /* Активную сессию выбирают в одном окне — другое окно обязано
         переключиться на неё же, иначе переписки разъедутся. */
      if (e.key === activeKey()) {
        var id = e.newValue || '';
        if (id && id !== activeSession) {
          setActiveSession(id);
          loadTranscript();
          loadSessions();
        }
        return;
      }
      if (e.key !== revKey() || busy) return;
      loadTranscript();
      loadSessions();
    });
  }

  // ------------------------------------------------------------- разметка
  function addMsg(role, text, quiet) {
    var el = document.createElement('div');
    el.className = 'aibar-msg ' + (role === 'user' ? 'user' : 'agent');
    el.setAttribute('data-raw', text);
    el.innerHTML = role === 'user' ? esc(text) : fmt(text);
    logEl.appendChild(el);
    if (emptyEl) emptyEl.style.display = 'none';
    if (!quiet) bumpRevision();
    scrollLog();
    return el;
  }

  function addError(text) {
    var el = document.createElement('div');
    el.className = 'aibar-msg error';
    el.textContent = text;
    logEl.appendChild(el);
    scrollLog();
  }

  function addTool(text) {
    if (!pending) return;
    var line = document.createElement('span');
    line.className = 'tool-line';
    line.textContent = text;
    pending.appendChild(line);
    scrollLog();
  }

  function scrollLog() { if (logEl) logEl.scrollTop = logEl.scrollHeight; }

  // ------------------------------------------------------------ трей
  function setTray(state) {
    if (!trayBtn) return;
    if (state === 'thinking') {
      trayBtn.classList.remove('done');
      trayBtn.classList.add('visible', 'thinking');
      trayBtn.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i>';
      trayBtn.title = 'Агент думает…';
      return;
    }
    // Готов ответ: гасим «думает» и один раз проигрываем всплытие.
    var wasThinking = trayBtn.classList.contains('thinking');
    trayBtn.classList.remove('thinking', 'bump');
    trayBtn.classList.add('visible', 'done');
    trayBtn.innerHTML = '<i class="fas fa-check"></i>';
    trayBtn.title = 'Агент ответил — открыть';
    if (wasThinking) {
      void trayBtn.offsetWidth;         // перезапуск анимации
      trayBtn.classList.add('bump');
    }
  }

  function hideTray() {
    if (!trayBtn) return;
    trayBtn.classList.remove('visible', 'thinking', 'done', 'bump');
    trayBtn.innerHTML = '';
  }

  /* Всплывающее сообщение об ответе.
     Нужно именно тогда, когда палитра закрыта: пользователь мог уйти с
     /desktop, а агент отвечает долго. Клик по нему открывает мини-панель. */
  function showDonePopup() {
    // Встроенный чат отвечает на глазах — всплывать не о чём.
    if (inline) return;
    if (overlay && overlay.classList.contains('open')) return;
    var t = $('aibarToast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'aibarToast';
      t.className = 'aibar-toast';
      t.innerHTML = '<i class="fas fa-check-circle"></i><div class="aibar-toast-text"></div>';
      document.body.appendChild(t);
      t.addEventListener('click', function () {
        hideDonePopup();
        openBar();
      });
    }
    // Последний ответ агента: :last-of-type по классу не работает, берём
    // последний подходящий элемент вручную.
    var agents = logEl ? logEl.querySelectorAll('.aibar-msg.agent') : [];
    var last = agents.length ? agents[agents.length - 1] : null;
    var text = last ? (last.getAttribute('data-raw') || '') : '';
    text = text.replace(/\s+/g, ' ').trim();
    t.querySelector('.aibar-toast-text').textContent =
      text ? (text.length > 160 ? text.slice(0, 160) + '…' : text) : 'Ответ готов';
    t.classList.add('show');
    clearTimeout(t.__hideTimer);
    t.__hideTimer = setTimeout(hideDonePopup, 9000);
  }

  function hideDonePopup() {
    var t = $('aibarToast');
    if (t) t.classList.remove('show');
  }

  /** Начать заново: чистим лог и просим мост забыть соответствие ключа.
      Если открыта конкретная сессия агента — удаляем её: продолжать
      переписку с нуля в той же сессии нельзя. */
  function clearChat() {
    if (logEl) logEl.innerHTML = '';
    if (emptyEl) emptyEl.style.display = '';
    hideTray();
    bumpRevision();
    if (activeSession) {
      var sid = activeSession;
      setActiveSession('');
      fetch('/api/agentai/session/' + encodeURIComponent(sid) +
            '?profile=' + encodeURIComponent(persona), { method: 'DELETE' })
        .catch(function () { /* сессия могла исчезнуть сама */ });
      loadSessions();
    }
    fetch('/api/agentai/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_key: sessionKey, profile: persona })
    }).catch(function () { /* локально уже чисто */ });
  }

  // --------------------------------------------------- вложения (экран)
  function renderAttachments() {
    var boxes = document.querySelectorAll('.aibar-attach');
    if (!boxes.length) return;
    boxes.forEach(function (box) {
      box.innerHTML = '';
      box.classList.remove('has');
      attachments.forEach(function (a) {
        var t = document.createElement('div');
        t.className = 'aibar-chip';
        t.innerHTML = '<img src="' + a.data_url + '" alt="">' +
                      '<span class="aibar-chip-name">' + esc(a.name) + '</span>' +
                      '<button class="aibar-chip-x" title="Убрать">×</button>';
        t.querySelector('.aibar-chip-x').addEventListener('click', function () {
          attachments = attachments.filter(function (x) { return x !== a; });
          renderAttachments();
        });
        box.appendChild(t);
      });
      if (attachments.length) box.classList.add('has');
    });
  }

  function addAttachment(f, name) {
    if (!f || !f.data_url) return;
    attachments.push({ name: name || 'экран', data_url: f.data_url, w: f.w, h: f.h });
    renderAttachments();
  }

  function stopStream(stream) {
    if (!stream) return;
    try { stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) { /* */ }
  }

  /** Снять один кадр с экрана.
   *
   * getDisplayMedia — единственный способ увидеть экран из окна: в браузере
   * он показывает системный выбор дисплея, в обёртке Electron главный
   * процесс отвечает на запрос сам (без окна выбора).
   */
  function grabScreen() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
      return Promise.reject(new Error('экран недоступен в этом браузере'));
    }
    return navigator.mediaDevices.getDisplayMedia({ video: true, audio: false })
      .then(function (stream) {
        return new Promise(function (resolve, reject) {
          var v = document.createElement('video');
          v.srcObject = stream;
          v.muted = true;
          v.playsInline = true;
          v.onloadedmetadata = function () {
            var started = false;
            var start = function () {
              if (started) return;
              started = true;
              // Кадру нужно отрисоваться: без паузы canvas выходит чёрным.
              setTimeout(function () {
                var c = document.createElement('canvas');
                c.width = v.videoWidth || 1280;
                c.height = v.videoHeight || 720;
                c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
                stopStream(stream);
                resolve({ data_url: c.toDataURL('image/jpeg', 0.85),
                          w: c.width, h: c.height });
              }, 150);
            };
            // Кадры идут и без «разрешения» на воспроизведение, но без play()
            // видео остаётся readyState=0 и canvas выходит чёрным. Автозапуск
            // может быть отклонён политикой браузера — тогда снимаем кадр всё
            // равно: поток уже подключён.
            var p = v.play();
            if (p && p.then) { p.then(start, start); } else { start(); }
            setTimeout(start, 1200);   // страховка: не ждать play() вечно
          };
          v.onerror = function () { stopStream(stream); reject(new Error('кадр не получен')); };
        });
      });
  }

  /* На время съёмки панель прячем: иначе скриншот экрана был бы
     скриншотом собственной панели, а выделенная область — её куском. */
  function hideForCapture() {
    document.documentElement.classList.add('aibar-capturing');
    return new Promise(function (r) { setTimeout(r, 250); });
  }

  function showAfterCapture() {
    document.documentElement.classList.remove('aibar-capturing');
  }

  function captureScreen() {
    return hideForCapture()
      .then(grabScreen)
      .then(function (f) { showAfterCapture(); addAttachment(f, 'экран'); return f; })
      .catch(function (e) { showAfterCapture(); addError('Экран не снят: ' + e.message); return null; });
  }

  /** Выделение области: сначала кадр, потом рамка прямо по нему.
   *
   * Нативного «выдели прямоугольник на рабочем столе» в вебе нет, поэтому
   * рамка рисуется поверх уже снятого кадра — результат тот же, зато
   * работает и в браузере, и в обёртке.
   */
  function captureRegion() {
    return hideForCapture()
      .then(grabScreen)
      .then(function (f) { showAfterCapture(); return cropRegion(f); })
      .then(function (crop) {
        if (crop) addAttachment(crop, 'область');
        return crop;
      })
      .catch(function (e) { showAfterCapture(); addError('Область не выделена: ' + e.message); return null; });
  }

  function cropRegion(frame) {
    return new Promise(function (resolve) {
      var box = document.createElement('div');
      box.className = 'aibar-crop';
      box.innerHTML =
        '<img src="' + frame.data_url + '" alt="">' +
        '<div class="aibar-crop-hint">Протяните рамку нужной области · Esc — отмена</div>' +
        '<div class="aibar-crop-rect" style="display:none"></div>';
      document.body.appendChild(box);

      var img = box.querySelector('img');
      var rect = box.querySelector('.aibar-crop-rect');
      var x0 = 0, y0 = 0, x1 = 0, y1 = 0, dragging = false;

      function done(crop) {
        document.removeEventListener('keydown', onKey, true);
        box.remove();
        resolve(crop || null);
      }
      function onKey(e) { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(null); } }
      document.addEventListener('keydown', onKey, true);

      function pos(e) {
        return { x: e.clientX, y: e.clientY };
      }
      box.addEventListener('mousedown', function (e) {
        dragging = true; x0 = x1 = e.clientX; y0 = y1 = e.clientY;
        rect.style.display = 'block';
        paint();
        e.preventDefault();
      });
      box.addEventListener('mousemove', function (e) {
        if (!dragging) return;
        x1 = e.clientX; y1 = e.clientY;
        paint();
      });
      box.addEventListener('mouseup', function () {
        if (!dragging) return;
        dragging = false;
        var w = Math.abs(x1 - x0), h = Math.abs(y1 - y0);
        if (w < 8 || h < 8) { done(null); return; }
        cropToImage(Math.min(x0, x1), Math.min(y0, y1), w, h).then(done);
      });

      function paint() {
        var l = Math.min(x0, x1), t = Math.min(y0, y1);
        rect.style.left = l + 'px'; rect.style.top = t + 'px';
        rect.style.width = Math.abs(x1 - x0) + 'px';
        rect.style.height = Math.abs(y1 - y0) + 'px';
      }

      /** Пересчитать рамку из экранных пикселей в пиксели кадра.
       *  Картинка показана с object-fit:contain — отсюда поля по краям. */
      function cropToImage(sx, sy, sw, sh) {
        return new Promise(function (res) {
          var src = new Image();
          src.onload = function () {
            var vw = window.innerWidth, vh = window.innerHeight;
            var s = Math.min(vw / src.naturalWidth, vh / src.naturalHeight);
            var ox = (vw - src.naturalWidth * s) / 2;
            var oy = (vh - src.naturalHeight * s) / 2;
            var c = document.createElement('canvas');
            c.width = Math.max(1, Math.round(sw / s));
            c.height = Math.max(1, Math.round(sh / s));
            c.getContext('2d').drawImage(
              src,
              Math.max(0, Math.round((sx - ox) / s)), Math.max(0, Math.round((sy - oy) / s)),
              c.width, c.height,
              0, 0, c.width, c.height);
            res({ data_url: c.toDataURL('image/jpeg', 0.9), w: c.width, h: c.height });
          };
          src.onerror = function () { res(null); };
          src.src = frame.data_url;
        });
      }
    });
  }

  // ------------------------------------------------------- диктовка
  function loadVoiceSettings() {
    fetch(CONFIG, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (c) {
        var sp = (c && c.speech) || {};
        sttLang = ((sp.stt || {}).local_language) || 'ru';
        voiceCfg = sp.voice_input_enabled !== false;
      })
      .catch(function () { /* настройки недоступны — голос просто не включим */ });
    fetch(STT_STATUS, { cache: 'no-store' })
      .then(function (r) { sttReady = r.ok; paintVoiceBtn(); })
      .catch(function () { sttReady = false; paintVoiceBtn(); });
    try { voiceMode = localStorage.getItem(VOICE_ON + sessionKey) === '1'; } catch (e) { /* */ }
    paintVoiceBtn();
  }

  function paintVoiceBtn() {
    /* Кнопок голоса две — в панели запроса и в треде. Красим обе: иначе
       одна из них осталась бы со старой подписью. */
    var btns = document.querySelectorAll('.aibar-voice');
    btns.forEach(function (b) {
      b.classList.toggle('on', !!voiceMode);
      b.classList.toggle('off', !sttReady);
      b.title = sttReady
        ? (voiceMode ? 'Голосовой режим включён — говорите или нажмите Shift+Space'
                     : 'Голосовой режим')
        : 'Whisper недоступен';
    });
  }

  /* Голосовой режим: поднимаем модуль, восстанавливаем включённое состояние
     и подписываемся на распознанные фразы. Фраза со словом-активацией
     уходит агенту сразу, фраза без него — в поле ввода (диктовка). */
  function bootVoice() {
    if (!window.AiBarVoice) return;
    window.AiBarVoice.boot();
    window.AiBarVoice.onText(function (text, go) {
      if (go) { send(text); return; }
      var el = activeInput();
      if (el) {
        el.value = (el.value ? el.value + ' ' : '') + text;
        try { el.focus(); } catch (e) { /* */ }
      }
    });
    if (voiceMode) window.AiBarVoice.enable();
  }

  function toggleVoiceMode() {
    if (!sttReady) { addError('Локальный Whisper не готов — откройте раздел «Голос» в настройках.'); return; }
    voiceMode = !voiceMode;
    try { localStorage.setItem(VOICE_ON + sessionKey, voiceMode ? '1' : '0'); } catch (e) { /* */ }
    paintVoiceBtn();
    /* Голосовой режим живёт в отдельном модуле: он умеет слово-активации,
       непрерывный микрофон и озвучку ответов. Включаем и выключаем его
       здесь, чтобы состояние переживало перезагрузку окна. */
    if (window.AiBarVoice) {
      if (voiceMode) window.AiBarVoice.enable();
      else window.AiBarVoice.disable();
    }
  }

  // ------------------------------------------------------- личности
  /* Личность агента = профиль Hermes: своя папка, свой SOUL.md (текст
     личности) и своя переписка. Выбрана один раз — и палитра говорит
     именно с ней, включая историю. */
  function loadPersonas() {
    return fetch(PERSONAS, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : { personas: [] }; })
      .then(function (d) {
        personas = d.personas || [];
        /* Личность, выбранная для этого профиля приложения, — из сервера:
           так смена профиля в системе меняет и личность агента. Локальный
           выбор окна важнее только если его явно делали здесь. */
        var saved = '';
        try { saved = localStorage.getItem(personaKey()) || ''; } catch (e) { saved = ''; }
        if (!saved && d.current) {
          persona = d.current;
          try { localStorage.setItem(personaKey(), persona); } catch (e) { /* */ }
        } else if (saved && saved !== persona) {
          persona = saved;
        }
        if (!personas.some(function (p) { return p.name === persona; })) {
          persona = 'default';
        }
        paintPersonaBtn();
      })
      .catch(function () { personas = []; paintPersonaBtn(); });
  }

  function personaByName(name) {
    for (var i = 0; i < personas.length; i++) {
      if (personas[i].name === name) return personas[i];
    }
    return null;
  }

  function paintPersonaBtn() {
    var p = personaByName(persona);
    var label = p ? (p.display_name || p.name) : 'Hermes';
    /* Кнопок личности две — в панели запроса и в треде. Красим обе: иначе
       одна из них осталась бы со старой подписью. Надписи на кнопке нет:
       в маленьком окне место нужно кнопкам, а имя личности — в подсказке
       и в меню выбора. */
    document.querySelectorAll('.aibar-persona').forEach(function (b) {
      b.innerHTML = '<i class="fas fa-masks-theater"></i>';
      b.classList.toggle('on', persona !== 'default');
      b.title = 'Личность агента: ' + label;
    });
  }

  /* Меню личностей. Позиционируем по КНОПКЕ, которую нажали: их две, и
     одна из них (в панели запроса) может быть скрыта — по ней меню
     уезжало за край экрана и не появлялось. */
  function togglePersonaMenu(anchor) {
    var old = document.querySelector('.aibar-persona-menu');
    if (old) { old.parentNode.removeChild(old); return; }
    var menu = document.createElement('div');
    menu.className = 'aibar-persona-menu';
    if (!personas.length) {
      var empty = document.createElement('div');
      empty.className = 'aibar-persona-empty';
      empty.textContent = 'Личности не загрузились';
      menu.appendChild(empty);
    }
    personas.forEach(function (p) {
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'aibar-persona-item' + (p.name === persona ? ' on' : '');
      row.innerHTML = '<span class="aibar-persona-ava">' + esc(p.icon || '🙂') + '</span>' +
                      '<span class="aibar-persona-t">' + esc(p.display_name || p.name) + '</span>' +
                      '<span class="aibar-persona-d">' + esc(p.description || '') + '</span>';
      row.addEventListener('click', function (e) {
        e.stopPropagation();
        setPersona(p.name);
        if (menu.parentNode) menu.parentNode.removeChild(menu);
      });
      menu.appendChild(row);
    });
    document.body.appendChild(menu);
    var btn = anchor || document.querySelector('.aibar-persona');
    if (btn) {
      var r = btn.getBoundingClientRect();
      if (!r.width && !r.height) {
        /* Кнопка скрыта (например, панель запроса свёрнута) — показываем
           меню у края окна, чтобы оно точно было видно. */
        menu.style.right = '12px';
        menu.style.bottom = '12px';
      } else {
        menu.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
        menu.style.bottom = (window.innerHeight - r.top + 8) + 'px';
      }
    }
    setTimeout(function () {
      document.addEventListener('click', function close(e) {
        if (menu.contains(e.target)) return;
        if (menu.parentNode) menu.parentNode.removeChild(menu);
        document.removeEventListener('click', close);
      });
    }, 30);
  }

  function setPersona(name) {
    persona = name || 'default';
    try { localStorage.setItem(personaKey(), persona); } catch (e) { /* */ }
    paintPersonaBtn();
    /* Выбор личности хранится в профиле приложения: сменили профиль в
       системе — вернулись к своей личности и своим диалогам. */
    fetch(PERSONAS + '/current', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: persona })
    }).catch(function () { /* сервер недоступен — останется выбор этого окна */ });
    // У личности своя переписка — переключаемся на её сессию.
    setActiveSession(readActiveSession());
    loadSessions();
    loadTranscript();
  }

  // --------------------------------------------------- сессии агента
  /* Переписки агента: показываем последние три, крестик удаляет, «+»
     создаёт новую, «…» раскрывает весь список. Активная сессия живёт в
     localStorage — окно Electron и палитра рабочего стола видят одну и ту
     же выбранную переписку. */
  function readActiveSession() {
    try { return localStorage.getItem(activeKey()) || ''; } catch (e) { return ''; }
  }

  function setActiveSession(id) {
    activeSession = id || '';
    try {
      if (activeSession) localStorage.setItem(activeKey(), activeSession);
      else localStorage.removeItem(activeKey());
    } catch (e) { /* */ }
    renderSessions();
  }

  function loadSessions() {
    return fetch(SESSIONS + '?profile=' + encodeURIComponent(persona) +
                 '&session_key=' + encodeURIComponent(sessionKey), { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : { sessions: [] }; })
      .then(function (d) {
        sessions = d.sessions || [];
        if (!activeSession) {
          /* Ничего не выбрано — открываем ту переписку, которая уже была
             привязана к нашему ключу («Быстрый запрос»), иначе самую
             свежую. */
          var cur = d.current && sessions.some(function (s) { return s.id === d.current; })
            ? d.current : (sessions[0] ? sessions[0].id : '');
          if (cur) {
            setActiveSession(cur);
            loadTranscript();
          }
        } else if (!sessions.some(function (s) { return s.id === activeSession; })) {
          /* Активная сессия исчезла (удалена в другом окне или у личности
             сменили профиль) — берём самую свежую. */
          setActiveSession(sessions.length ? sessions[0].id : '');
          loadTranscript();
        }
        renderSessions();
      })
      .catch(function () { sessions = []; renderSessions(); });
  }

  function renderSessions() {
    var host = thread && thread.querySelector('.aibar-sessions');
    if (!host) return;
    host.innerHTML = '';
    var list = sessionsExpanded ? sessions : sessions.slice(0, 3);
    if (!list.length) {
      var none = document.createElement('span');
      none.className = 'aibar-sessions-none';
      none.textContent = 'диалогов нет';
      host.appendChild(none);
    }
    list.forEach(function (s) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'aibar-session-chip' + (s.id === activeSession ? ' on' : '');
      chip.title = s.title || 'Диалог';
      chip.innerHTML = '<span class="aibar-session-t">' + esc(s.title || 'Диалог') + '</span>' +
                       '<span class="aibar-session-x" title="Удалить диалог">×</span>';
      chip.querySelector('.aibar-session-t').addEventListener('click', function (e) {
        e.stopPropagation();
        switchSession(s.id);
      });
      chip.querySelector('.aibar-session-x').addEventListener('click', function (e) {
        e.stopPropagation();
        removeSession(s);
      });
      host.appendChild(chip);
    });

    var add = document.createElement('button');
    add.type = 'button';
    add.className = 'aibar-session-chip add';
    add.title = 'Новый диалог';
    add.innerHTML = '<i class="fas fa-plus"></i>';
    add.addEventListener('click', function (e) { e.stopPropagation(); newSession(); });
    host.appendChild(add);

    var more = document.createElement('button');
    more.type = 'button';
    more.className = 'aibar-session-chip more';
    more.title = sessionsExpanded ? 'Показать только последние' : 'Показать все диалоги';
    more.innerHTML = '<i class="fas fa-ellipsis-h"></i>';
    more.addEventListener('click', function (e) {
      e.stopPropagation();
      sessionsExpanded = !sessionsExpanded;
      renderSessions();
    });
    host.appendChild(more);
  }

  function switchSession(id) {
    if (!id || id === activeSession) return;
    setActiveSession(id);
    loadTranscript();
  }

  function newSession() {
    fetch('/api/agentai/session/new?profile=' + encodeURIComponent(persona), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'Новый диалог' })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.id) throw new Error(d && d.error ? d.error : 'сессия не создана');
      setActiveSession(d.id);
      loadSessions();
      loadTranscript();
      bumpRevision();
    }).catch(function (e) { addError('Не удалось создать диалог: ' + e.message); });
  }

  function removeSession(s) {
    if (!confirm('Удалить диалог «' + (s.title || 'без названия') + '»?')) return;
    fetch('/api/agentai/session/' + encodeURIComponent(s.id) +
          '?profile=' + encodeURIComponent(persona), { method: 'DELETE' })
      .then(function () {
        if (activeSession === s.id) { setActiveSession(''); loadTranscript(); }
        loadSessions();
        bumpRevision();
      })
      .catch(function (e) { addError('Не удалось удалить диалог: ' + e.message); });
  }

  function toBase64(buf) {
    var bytes = new Uint8Array(buf), chunk = '', step = 0x8000;
    for (var i = 0; i < bytes.length; i += step) {
      chunk += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    }
    return btoa(chunk);
  }

  /** Запись до паузы: молчание дольше SILANCE_MS означает «я закончил».
   *
   * Порог по RMS, а не по «тишине» из MediaRecorder: он единственный
   * доступен синхронно и не требует размечивать аудио на клиенте.
   */
  function startVoice(autoSend) {
    if (rec) { stopVoice(); return; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      addError('Микрофон недоступен в этом браузере.');
      return;
    }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var chunks = [];
      var mr = new MediaRecorder(stream);
      mr.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };

      var ac = new (window.AudioContext || window.webkitAudioContext)();
      var an = ac.createAnalyser();
      an.fftSize = 512;
      ac.createMediaStreamSource(stream).connect(an);
      var bufArr = new Uint8Array(an.fftSize);

      var started = Date.now();
      var lastLoud = 0;
      var sawSpeech = false;
      var timer = null;

      var done = false;

      function transcribe() {
        if (!chunks.length) return;
        var blob = new Blob(chunks, { type: mr.mimeType || 'audio/webm' });
        blob.arrayBuffer().then(function (ab) {
          setBusy(true);
          return fetch(STT_RUN, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ audio: toBase64(ab), language: sttLang })
          }).then(function (r) { return r.json(); });
        }).then(function (d) {
          setBusy(false);
          var text = (d && (d.text || (d.result && d.result.text))) || '';
          text = String(text).trim();
          if (!text) { addError('Whisper не расслышал текст' + (d && d.error ? ': ' + d.error : '')); return; }
          var el = activeInput();
          if (el) { el.value = (el.value ? el.value + ' ' : '') + text; }
          if (autoSend) send(text);
        }).catch(function (e) {
          setBusy(false);
          addError('Распознавание не удалось: ' + e.message);
        });
      }

      function finish(mode) {
        if (done) return;
        done = true;
        clearInterval(timer);
        stopStream(stream);
        try { ac.close(); } catch (e) { /* */ }
        rec = null;
        paintRec(false);
        if (mode === 'cancel') { addError('Речь не распознана — тишина.'); return; }
        // Последний кусок MediaRecorder отдаёт только в onstop: без stop()
        // расшифровывать было бы нечего.
        mr.onstop = transcribe;
        try { mr.stop(); } catch (e) { transcribe(); }
      }

      mr.start(2000);   // куски каждые 2 с: короткая фраза не потеряется
      rec = { stop: function () { finish('send'); } };
      paintRec(true);

      timer = setInterval(function () {
        an.getByteTimeDomainData(bufArr);
        var sum = 0;
        for (var i = 0; i < bufArr.length; i++) {
          var v = (bufArr[i] - 128) / 128;
          sum += v * v;
        }
        var rms = Math.sqrt(sum / bufArr.length);
        var now = Date.now();
        if (rms > 0.015) { lastLoud = now; sawSpeech = true; }
        if (!sawSpeech && now - started > 8000) finish('cancel');
        else if (sawSpeech && now - lastLoud >= SILANCE_MS) finish('send');
      }, 100);
    }).catch(function (e) {
      addError('Микрофон не открыт: ' + e.message);
    });
  }

  function stopVoice() {
    if (!rec) return;
    rec.stop();
  }

  function paintRec(on) {
    var b = document.querySelector('.aibar-voice');
    if (b) b.classList.toggle('rec', !!on);
    document.body.classList.toggle('aibar-rec', !!on);
  }

  function activeInput() {
    if (inline) return threadInput;
    if (panel && panel.style.display !== 'none' && input) return input;
    return threadInput;
  }

  // ------------------------------------------------------------- панели
  function showThread() {
    if (inline) return;
    if (panel) panel.style.display = 'none';
    thread.style.display = 'flex';
    if (threadInput) setTimeout(function () { threadInput.focus(); }, 20);
  }

  function showPrompt() {
    if (inline) return;
    thread.style.display = 'none';
    panel.style.display = 'block';
    if (input) setTimeout(function () { input.focus(); }, 20);
  }

  function openBar() {
    if (inline) return;
    hideDonePopup();
    overlay.classList.add('open');
    /* Окно могли скрыть на вкладке плеера/заметок/диалогов: возвращаем
       её, иначе чат показался бы поверх открытой панели. */
    if (view && view !== 'chat') { setView(view); return; }
    // Панель ответа показываем только если в этой переписке что-то есть.
    if (logEl && logEl.querySelector('.aibar-msg')) showThread();
    else showPrompt();
  }

  function closeBar() {
    if (inline) return;
    overlay.classList.remove('open');
    if (solo && window.electronAPI && window.electronAPI.closeAiBar) {
      window.electronAPI.closeAiBar();
    }
  }

  function toggleBar() {
    if (inline) return;
    if (overlay.classList.contains('open')) closeBar();
    else openBar();
  }

  // ------------------------------------------------------ виды окна
  /* Шаблоны (/ai-bar, /desktop) несут шапку сами, поэтому приводим её к
     одному виду здесь: без надписи с названием сессии и без иконки робота —
     вместо неё кнопка музыки. Держать два варианта разметки нельзя, иначе
     окно и оверлей разъедутся при первом же изменении. */
  function ensureHeader() {
    if (!thread) return;
    var head = thread.querySelector('.aibar-thread-head');
    if (!head) return;
    Array.prototype.forEach.call(head.querySelectorAll('i.fa-robot, .aibar-title'), function (n) {
      n.parentNode.removeChild(n);
    });
    if (!head.querySelector('.aibar-music')) {
      head.insertAdjacentHTML('afterbegin',
        '<button class="aibar-iconbtn aibar-music" title="Музыка">' +
        '<i class="fas fa-music"></i></button>');
    }
    if (!head.querySelector('.aibar-avatar-btn')) {
      head.insertAdjacentHTML('afterbegin',
        '<button class="aibar-iconbtn aibar-avatar-btn" title="3D-аватар (отдельное окно)">' +
        '<i class="fas fa-user-astronaut"></i></button>');
    }
    if (!head.querySelector('.aibar-notes-btn')) {
      head.insertAdjacentHTML('afterbegin',
        '<button class="aibar-iconbtn aibar-notes-btn" title="Быстрые заметки">' +
        '<i class="fas fa-pencil"></i></button>');
    }
    if (!head.querySelector('.aibar-msgs-btn')) {
      head.insertAdjacentHTML('afterbegin',
        '<button class="aibar-iconbtn aibar-msgs-btn" title="Сообщения">' +
        '<i class="fas fa-comment-dots"></i></button>');
    }
    if (!head.querySelector('.aibar-back')) {
      head.insertAdjacentHTML('afterbegin',
        '<button class="aibar-iconbtn aibar-back" title="К запросу" style="display:none">' +
        '<i class="fas fa-arrow-left"></i></button>');
    }
    /* Полоса диалогов — отдельной строкой ПОД шапкой. В шапке ей места
       не хватало: чипы вытесняли кнопки плеера и сообщений, а длинные
       названия обрезались. */
    if (!thread.querySelector('.aibar-sessions')) {
      var row = document.createElement('div');
      row.className = 'aibar-sessions';
      row.title = 'Диалоги с агентом';
      head.parentNode.insertBefore(row, head.nextSibling);
    }
    /* Вкладки окна — единственная навигация между чатом, заметками,
       плеером и диалогами. Шапка чата прячется под панелями, поэтому
       вкладки живут в overlay, а не в треде. Встроенный чат админки
       (inline) обходится без них. */
    if (solo && overlay && !overlay.querySelector('.aibar-nav')) {
      overlay.insertAdjacentHTML('afterbegin',
        '<div class="aibar-nav" role="tablist">' +
          '<button class="aibar-nav-tab on" type="button" role="tab" aria-selected="true"' +
            ' data-view="chat"><i class="fas fa-comment"></i><span>Чат</span></button>' +
          '<button class="aibar-nav-tab" type="button" role="tab" aria-selected="false"' +
            ' data-view="notes"><i class="fas fa-note-sticky"></i><span>Заметки</span></button>' +
          '<button class="aibar-nav-tab" type="button" role="tab" aria-selected="false"' +
            ' data-view="player"><i class="fas fa-music"></i><span>Плеер</span></button>' +
          '<button class="aibar-nav-tab" type="button" role="tab" aria-selected="false"' +
            ' data-view="messenger"><i class="fas fa-comment-dots"></i><span>Диалоги</span></button>' +
        '</div>');
      overlay.querySelector('.aibar-nav').addEventListener('click', function (e) {
        var t = e.target.closest('.aibar-nav-tab');
        if (!t) return;
        var name = t.getAttribute('data-view') || 'chat';
        if (name === view) {
          if (name === 'chat' && input) input.focus();
          return;
        }
        setView(name);
      });
    }
  }

  /* Подсветка активной вкладки; играющий плеер и непрочитанные диалоги
     помечаются отдельно (paintMusic, aibar_messenger.js). */
  function paintNav(name) {
    if (!overlay) return;
    var tabs = overlay.querySelectorAll('.aibar-nav-tab');
    Array.prototype.forEach.call(tabs, function (t) {
      var on = t.getAttribute('data-view') === (name || 'chat');
      t.classList.toggle('on', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }

  function playerBox() {
    if (!overlay) return null;
    var box = overlay.querySelector('.aibar-player');
    if (!box) {
      box = document.createElement('div');
      box.className = 'aibar-player';
      box.style.display = 'none';
      overlay.appendChild(box);
    }
    return box;
  }

  /* Панель быстрых заметок — тем же способом, что и мессенджер. */
  function notesBox() {
    if (!overlay) return null;
    var box = overlay.querySelector('.aibar-notes');
    if (!box) {
      box = document.createElement('div');
      box.className = 'aibar-notes';
      box.style.display = 'none';
      overlay.appendChild(box);
    }
    return box;
  }

  /* Панель мессенджера — рядом с плеером, тем же способом. */
  function msgsBox() {
    if (!overlay) return null;
    var box = overlay.querySelector('.aibar-msgs');
    if (!box) {
      box = document.createElement('div');
      box.className = 'aibar-msgs';
      box.style.display = 'none';
      overlay.appendChild(box);
    }
    return box;
  }

  function setView(name) {
    if (inline) return;
    view = name;
    paintNav(name);
    var box = playerBox();
    var mbox = msgsBox();
    var nbox = notesBox();
    if (!box) return;
    var back = thread.querySelector('.aibar-back');
    var music = thread.querySelector('.aibar-music');
    var msgsBtn = thread.querySelector('.aibar-msgs-btn');

    if (name === 'messenger') {
      if (!window.AiBarMessenger) { setTray('done'); return; }
      window.AiBarMessenger.mount(mbox);
      mbox.style.display = '';
      if (nbox) nbox.style.display = 'none';
      box.style.display = 'none';
      if (panel) panel.style.display = 'none';
      /* Чат скрывается целиком: панель перекрывает окно, и шапка чата
         осталась бы под ней — «назад» и «закрепить» мессенджер носит свои. */
      thread.style.display = 'none';
      document.documentElement.classList.add('aibar-player-mode');
      var mback = mbox.querySelector('.aibar-msgs-back');
      if (mback) mback.onclick = function () { setView('chat'); };
      var mpin = mbox.querySelector('.aibar-msgs-pin');
      if (mpin) {
        mpin.classList.toggle('on', pinned);
        mpin.onclick = function () { setPinned(!pinned); };
      }
      window.AiBarMessenger.open();
      return;
    }

    if (name === 'notes') {
      if (!window.AiBarNotes) { setTray('done'); return; }
      window.AiBarNotes.mount(nbox);
      nbox.style.display = '';
      box.style.display = 'none';
      if (mbox) mbox.style.display = 'none';
      if (panel) panel.style.display = 'none';
      thread.style.display = 'none';
      document.documentElement.classList.add('aibar-player-mode');
      var nback = nbox.querySelector('.aibar-notes-back');
      if (nback) nback.onclick = function () { setView('chat'); };
      var npin = nbox.querySelector('.aibar-notes-pin');
      if (npin) {
        npin.classList.toggle('on', pinned);
        npin.onclick = function () { setPinned(!pinned); };
      }
      window.AiBarNotes.open();
      return;
    }

    if (name === 'player') {
      if (!window.AiBarPlayer) { setTray('done'); return; }
      window.AiBarPlayer.mount(box);
      box.style.display = '';
      if (mbox) mbox.style.display = 'none';
      if (nbox) nbox.style.display = 'none';
      if (panel) panel.style.display = 'none';
      // Чат скрывается целиком: панель плеера перекрывает окно, и шапка
      // чата осталась бы под ней — «назад» и «закрепить» плеер носит свои.
      thread.style.display = 'none';
      document.documentElement.classList.add('aibar-player-mode');
      var pback = box.querySelector('.p-back');
      if (pback) pback.onclick = function () { setView('chat'); };
      var ppin = box.querySelector('.p-pin');
      if (ppin) {
        ppin.classList.toggle('on', pinned);
        ppin.onclick = function () { setPinned(!pinned); };
      }
      window.AiBarPlayer.open();
      return;
    }

    box.style.display = 'none';
    if (mbox) mbox.style.display = 'none';
    if (nbox) nbox.style.display = 'none';
    document.documentElement.classList.remove('aibar-player-mode');
    if (back) back.style.display = 'none';
    if (music) music.style.display = '';
    if (msgsBtn) msgsBtn.style.display = '';
    if (window.AiBarMessenger) window.AiBarMessenger.close();
    if (logEl && logEl.querySelector('.aibar-msg')) showThread();
    else showPrompt();
  }

  function togglePlayer() { setView(view === 'player' ? 'chat' : 'player'); }

  // -------------------------------------------------------------- поток
  function setBusy(v) {
    busy = v;
    document.body.classList.toggle('aibar-busy', v);
    if (input) input.disabled = v;
    if (threadInput) threadInput.disabled = v;
    // В отдельном окне обёртки этим же флагом пользуется главный процесс:
    // пока агент думает, окно не прячется по потере фокуса.
    if (window.electronAPI && window.electronAPI.sendBusy) window.electronAPI.sendBusy(v);
  }

  function send(text) {
    text = (text || '').trim();
    if (busy) return;
    if (!text && !attachments.length) return;

    // «Живой экран» = к каждому запросу прикладывается текущий кадр.
    // Отдельной трансляции агент не держит: он принимает изображения на ход.
    if (liveScreen) {
      grabScreen().then(function (f) {
        if (f) attachments.push({ name: 'экран', data_url: f.data_url, w: f.w, h: f.h });
        doSend(text);
      }).catch(function () { doSend(text); });
      return;
    }
    doSend(text);
  }

  function doSend(text) {
    text = (text || '').trim();
    if (busy) return;
    if (!text && !attachments.length) return;

    var files = attachments.slice();
    attachments = [];
    renderAttachments();

    addMsg('user', text || '(приложен экран)');
    if (!inline) showThread();

    pending = addMsg('agent', '');
    pending.innerHTML = '<span class="aibar-caret"></span>';
    setBusy(true);
    setTray('thinking');

    var body = { message: text, session_key: sessionKey, title: sessionTitle,
                 profile: persona };
    if (activeSession) body.session_id = activeSession;
    if (files.length) body.attachments = files;

    var ctrl = new AbortController();
    currentAbort = ctrl;
    var stalled = false;
    var stopped = false;
    // Локальная модель может быть не запущена: агент тогда держит соединение
    // открытым и молчит. Без предупреждения чат выглядит зависшим навсегда.
    var watchdog = setTimeout(function () {
      stalled = true;
      if (pending && !pending.getAttribute('data-raw')) {
        pending.innerHTML = '<span class="aibar-stall">Агент молчит ' +
          Math.round(STALL_MS / 1000) + ' с. Проверьте, запущена ли модель.</span>';
      }
    }, STALL_MS);

    fetch(API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: ctrl.signal
    }).then(function (r) {
      if (!r.ok || !r.body) throw new Error('сервер ответил ' + r.status);
      return readSSE(r.body);
    }).catch(function (e) {
      if (e && e.name === 'AbortError') {
        /* Остановили сами: частичный текст остаётся в пузыре, ниже к нему
           добавим пометку. Пузырь не удаляем. */
        stopped = true;
      } else if (pending) {
        pending.remove();
        addError('Не удалось получить ответ: ' + e.message);
        pending = null;
      }
      setTray('done');
    }).then(function () {
      clearTimeout(watchdog);
      currentAbort = null;
      var reply = '';
      if (pending) {
        if (stalled && !pending.getAttribute('data-raw')) {
          pending.setAttribute('data-raw', 'Агент не ответил.');
        }
        reply = pending.getAttribute('data-raw') || '';
        pending.innerHTML = fmt(reply) +
          (stopped ? '<span class="aibar-stopped">Остановлено</span>' : '');
      }
      pending = null;
      setBusy(false);
      /* Голосовой режим: ответ агента озвучивается, как на странице агента.
         Недосказанный (остановленный) ответ не озвучиваем. */
      if (reply && !stopped && window.AiBarVoice && window.AiBarVoice.enabled()) {
        window.AiBarVoice.onAgentReply(reply);
      }
      // Индикатор в трее переводим в «готово» и при успехе тоже: иначе
      // он остался бы крутиться после удачного ответа.
      setTray('done');
      showDonePopup();
      bumpRevision();
      scrollLog();
    });
  }

  /** Разбор SSE: накапливаем assistant.delta в «живое» сообщение агента. */
  function readSSE(body) {
    var reader = body.getReader();
    var dec = new TextDecoder();
    var buf = '';
    var acc = '';

    function flush() {
      if (!pending) return;
      if (acc) {
        pending.setAttribute('data-raw', acc);
        pending.innerHTML = fmt(acc) + '<span class="aibar-caret"></span>';
      }
      scrollLog();
    }

    function handle(ev, data) {
      if (ev === 'assistant.delta' || ev === 'assistant.reasoning') {
        acc += (data && data.delta) || '';
        flush();
      } else if (ev === 'tool.started') {
        addTool('⚙ ' + ((data && data.tool_name) || 'инструмент') + '…');
      } else if (ev === 'assistant.completed' || ev === 'run.completed') {
        var final = (data && (data.content || data.final_response || data.output)) || '';
        if (final) { acc = final; flush(); }
      } else if (ev === 'run.failed' || ev === 'run.cancelled') {
        // Ход мог оборваться на недоступной модели: текст агент уже отдал
        // (или не отдал), но причину показываем явно.
        var why = data && data.turn_exit_reason;
        if (why && !acc) acc = 'Агент не закончил ответ: ' + why;
        else if (why) addTool('⚠ ' + why);
      } else if (ev === 'error') {
        throw new Error((data && data.message) || 'агент вернул ошибку');
      }
    }

    function pump() {
      return reader.read().then(function (r) {
        if (r.done) return;
        buf += dec.decode(r.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          var frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          var ev = 'message', data = null;
          frame.split('\n').forEach(function (ln) {
            if (ln.startsWith('event:')) ev = ln.slice(6).trim();
            else if (ln.startsWith('data:')) {
              try { data = JSON.parse(ln.slice(5).trim()); } catch (e) { /* keepalive */ }
            }
          });
          if (data !== null) handle(ev, data);
        }
        return pump();
      });
    }

    return pump();
  }

  // -------------------------------------------------------------- ввод
  function onKey(e, el) {
    if (e.key === 'Escape') { e.preventDefault(); closeBar(); return; }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      var v = el.value;
      el.value = '';
      send(v);
    }
  }

  /** Esc должен закрывать палитру, даже когда фокус ушёл из поля
   *  (клик по ответу, переключение окна). Иначе панель повисает.
   *  В виде плеера Esc сначала возвращает к запросу — закрывать окно
   *  сразу было бы неожиданно. */
  function onEscapeAnywhere(e) {
    if (e.key !== 'Escape') return;
    if (!overlay || !overlay.classList.contains('open')) return;
    e.preventDefault();
    /* Из плеера и мессенджера Esc сначала возвращает к чату: закрывать
       окно сразу было бы неожиданно. */
    if (view === 'player' || view === 'messenger' || view === 'notes') { setView('chat'); return; }
    closeBar();
  }

  // ------------------------------------------------------------ хоткей
  function isTyping(el) {
    if (!el) return false;
    var t = (el.tagName || '').toLowerCase();
    return t === 'input' || t === 'textarea' || el.isContentEditable;
  }

  /** Наше ли поле ввода? Shift+Space должен работать и внутри него. */
  function isOwnInput(el) {
    return !!(el && el.classList && el.classList.contains('aibar-input'));
  }

  /** Выделенный текст на странице (основной документ и свои iframe). */
  function pageSelection() {
    var docs = allFrameDocs();
    for (var i = 0; i < docs.length; i++) {
      var d = docs[i];
      var t = '';
      try { t = String(d.getSelection ? d.getSelection() : '').trim(); } catch (e) { t = ''; }
      if (t) return t;
    }
    return '';
  }

  /** Одно и то же действие для браузерного keydown и для хоткея обёртки. */
  function hotkeyAction(payload) {
    payload = payload || {};
    /* Голосовой режим: хоткей означает «говорю» — следующая фраза уходит
       агенту сразу, без слова-активации. */
    if (voiceMode && sttReady) {
      if (window.AiBarVoice && window.AiBarVoice.enabled()) {
        openBar();
        window.AiBarVoice.pushToTalk();
        return;
      }
      if (rec) { stopVoice(); return; }
      openBar();
      startVoice(true);
      return;
    }
    var wasOpen = !inline && overlay && overlay.classList.contains('open');
    if (wasOpen && !pinned && !payload.pinned) { closeBar(); return; }
    openBar();
    // Выделенное в браузере отправляем в поле, но не в чат: пользователь
    // просил, чтобы текст попадал агенту сразу, но без отправки.
    var sel = payload.selection || pageSelection();
    if (sel) {
      var el = activeInput();
      if (el) { el.value = sel; el.focus(); }
    }
  }

  function onHotkey(e) {
    // Shift+Space. Пробел по e.code: с русской раскладки e.key даёт « »,
    // по которому хоткей не опознаётся.
    if (!e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.code !== 'Space') return;
    // Чужим полям ввода не мешаем, своим — ещё как: фокус после
    // открытия остаётся в строке запроса, иначе хоткей не сработал бы
    // второй раз подряд.
    if (isTyping(e.target) && !isOwnInput(e.target)) return;
    e.preventDefault();
    hotkeyAction({});
  }

  /** Разметка палитры и мини-панели.
   *
   * В /desktop и в /ai-bar она уже есть в шаблоне, а вот
   * page_desktop.html её не содержит. Чтобы не дублировать её в каждом
   * шаблоне и не тянуть зависимость от её наличия, собираем сами, если
   * не нашли. Один источник правды — здесь.
   */
  /** Документы фреймов страницы: основной плюс вложенные (один уровень). */
  function allFrameDocs() {
    var out = [document];
    var frames = document.querySelectorAll('iframe');
    for (var i = 0; i < frames.length; i++) {
      try {
        var d = frames[i].contentDocument;
        if (!d) continue;
        out.push(d);
        var inner = d.querySelectorAll('iframe');
        for (var j = 0; j < inner.length; j++) {
          if (inner[j].contentDocument) out.push(inner[j].contentDocument);
        }
      } catch (e) { /* чужой origin — доступа нет */ }
    }
    return out;
  }

  /** Повесить хоткей в документы фреймов.
   *
   * /desktop — оболочка вокруг iframe с приложением, поэтому фокус почти
   * всегда внутри него, и keydown до нашего документа не доходит: без этого
   * хоткей молчал бы на всей странице, кроме панели задач.
   */
  function attachHotkeyToFrames() {
    allFrameDocs().forEach(function (d) {
      if (d === document || d.__aibarHotkeyBound) return;
      d.__aibarHotkeyBound = true;
      d.addEventListener('keydown', onHotkey);
      d.addEventListener('keydown', onEscapeAnywhere);
    });
    /* Наблюдатель вешается ровно один раз на документ. Раньше он создавался
       внутри этой же функции, а её вызывает собственный таймер каждые 2 с —
       с каждого тика добавлялся ещё один интервал и ещё один load-слушатель.
       Число таймеров удваивалось каждые 2 с (4 → 508 за 14 с), главный поток
       захлёбывался, страница замирала. В Electron эта ветка не исполняется
       вовсе, поэтому там зависаний не было. */
    if (document.__aibarWatchBound) return;
    document.__aibarWatchBound = true;
    // Фрейм перезагрузился или появился — документ сменился, слушатель
    // на старом document уже бесполезен.
    document.addEventListener('load', function (e) {
      if (e.target && e.target.tagName === 'IFRAME') attachHotkeyToFrames();
    }, true);
    setInterval(function () {
      if (document.querySelector('iframe')) attachHotkeyToFrames();
    }, 2000);
  }

  function toolbarHtml() {
    return '<div class="aibar-tools">' +
      '<button class="aibar-tool aibar-region" title="Выделить область экрана и показать ИИ">' +
        '<i class="fas fa-vector-square"></i></button>' +
      '<button class="aibar-tool aibar-shot" title="Скриншот экрана">' +
        '<i class="fas fa-desktop"></i></button>' +
      '<button class="aibar-tool aibar-live" title="Прикладывать экран к каждому запросу">' +
        '<i class="fas fa-tv"></i></button>' +
      '<button class="aibar-tool aibar-persona" title="Личность агента">' +
        '<i class="fas fa-masks-theater"></i></button>' +
      '<button class="aibar-tool aibar-voice" title="Голосовой режим">' +
        '<i class="fas fa-microphone"></i></button>' +
      '</div>';
  }

  function hintHtml(withCall) {
    var hints = '';
    if (withCall) hints += '<span><kbd>Shift</kbd>+<kbd>Space</kbd> вызвать</span>';
    hints += '<span><kbd>Enter</kbd> отправить</span>' +
             '<span><kbd>Esc</kbd> закрыть</span>';
    // Кнопка «закрепить» нужна только отдельному окну обёртки: без неё оно
    // прячется при потере фокуса, а пользователю нужно держать его открытым,
    // пока развёрнуто приложение из трея. Скрывается стилями вне solo-режима.
    return '<div class="aibar-hint">' + hints +
           '<span class="spacer"></span>' +
           '<button class="aibar-tool aibar-pin" title="Закрепить окно (не прятать при потере фокуса)">' +
             '<i class="fas fa-thumbtack"></i></button>' +
           toolbarHtml() + '</div>';
  }

  /* Шаблон может содержать разметку палитры сам (/ai-bar, /desktop) — тогда
     buildMarkup() её не создаёт, и кнопок экрана/микрофона в ней нет.
     Держать разметку в двух местах нельзя, поэтому недостающее достраиваем
     здесь: один источник правды остаётся в этом файле. */
  function ensureTools() {
    document.querySelectorAll('.aibar-input-row').forEach(function (row) {
      if (!row.previousElementSibling || !row.previousElementSibling.classList.contains('aibar-attach')) {
        row.insertAdjacentHTML('beforebegin', '<div class="aibar-attach"></div>');
      }
      // Кнопки — в строку с подсказками (Enter/Esc), а не в строку ввода:
      // так они не съедают ширину поля и стоят там, где на них смотрят.
      var scope = row.parentNode;
      var hint = scope.querySelector('.aibar-hint');
      if (!hint) {
        row.insertAdjacentHTML('afterend', hintHtml(false));
        hint = row.nextElementSibling;
      }
      var tools = hint.querySelector('.aibar-tools');
      if (!tools) {
        hint.insertAdjacentHTML('beforeend', '<span class="spacer"></span>' + toolbarHtml());
        tools = hint.querySelector('.aibar-tools');
      }
      /* Кнопка «закрепить» должна быть в ЛЮБОЙ строке подсказок, а не
         только в той, что мы сами собрали. В шаблоне /ai-bar строка
         подсказок уже есть — и в неё кнопка не попадала, так что в
         отдельном окне её было видно только вместе с тредом. */
      if (tools && !hint.querySelector('.aibar-pin')) {
        tools.insertAdjacentHTML('beforebegin',
          '<button class="aibar-tool aibar-pin" title="Закрепить окно (не прятать при потере фокуса)">' +
          '<i class="fas fa-thumbtack"></i></button>');
      }
    });
  }

  function buildMarkup() {
    if ($('aibarOverlay')) return;
    var d = document.createElement('div');
    d.className = 'aibar-overlay';
    d.id = 'aibarOverlay';
    d.innerHTML =
      '<div class="aibar-panel" id="aibarPanel">' +
        '<div class="aibar-attach" id="aibarAttach"></div>' +
        '<div class="aibar-input-row">' +
          '<i class="fas fa-robot"></i>' +
          '<input class="aibar-input" id="aibarInput" type="text" autocomplete="off" ' +
                 'spellcheck="false" placeholder="Спросить ИИ-агента…">' +
        '</div>' +
        hintHtml(true) +
      '</div>' +
      '<div class="aibar-thread" id="aibarThread">' +
        '<div class="aibar-thread-head">' +
          '<button class="aibar-iconbtn aibar-back" title="К запросу" style="display:none">' +
            '<i class="fas fa-arrow-left"></i></button>' +
          '<button class="aibar-iconbtn aibar-music" title="Музыка">' +
            '<i class="fas fa-music"></i></button>' +
          '<span class="spacer"></span>' +
          '<button class="aibar-iconbtn" id="aibarOpenAgent" title="Открыть страницу агента">' +
            '<i class="fas fa-up-right-from-square"></i></button>' +
          '<button class="aibar-iconbtn" id="aibarClear" title="Очистить переписку">' +
            '<i class="fas fa-eraser"></i></button>' +
          '<button class="aibar-iconbtn" id="aibarClose" title="Закрыть (Esc)">' +
            '<i class="fas fa-xmark"></i></button>' +
        '</div>' +
        '<div class="aibar-log" id="aibarLog">' +
          '<div class="aibar-empty">Задайте вопрос агенту — ответ появится здесь</div>' +
        '</div>' +
        '<div class="aibar-thread-foot">' +
          '<div class="aibar-attach" id="aibarAttach2"></div>' +
          '<div class="aibar-input-row">' +
            '<i class="fas fa-arrow-turn-up-left"></i>' +
            '<input class="aibar-input" id="aibarInput2" type="text" autocomplete="off" ' +
                   'spellcheck="false" placeholder="Ответить агенту…">' +
          '</div>' +
          hintHtml(false) +
        '</div>' +
      '</div>';
    document.body.appendChild(d);
  }

  // -------------------------------------------------------------- init
  /** Встроенный чат: карточка на странице, а не оверлей на весь экран.
      Ключ и заголовок беседы задаёт страница через data-атрибуты. */
  function mountInline() {
    var mount = document.querySelector('[data-aibar-mount]');
    if (!mount) return false;
    inline = true;
    thread = mount;
    logEl = mount.querySelector('.aibar-log');
    threadInput = mount.querySelector('.aibar-input');
    emptyEl = mount.querySelector('.aibar-empty');
    sessionKey = mount.getAttribute('data-aibar-session') || DEFAULT_KEY;
    sessionTitle = mount.getAttribute('data-aibar-title') || DEFAULT_TITLE;
    if (!logEl || !threadInput) { inline = false; return false; }
    return true;
  }

  /* Закрепить окно. Отдельное окно палитры прячется при потере фокуса —
     иначе оно мешало бы жить. Пользователю нужен и режим «висит, пока
     приложение свёрнуто»: с закреплённым окном его гасит только возврат
     приложения из трея (это решает main-процесс). */
  function setPinned(v) {
    pinned = !!v;
    document.querySelectorAll('.aibar-pin').forEach(function (b) {
      b.classList.toggle('on', pinned);
      b.title = pinned
        ? 'Открепить: окно снова спрячется при потере фокуса'
        : 'Закрепить окно (не прятать при потере фокуса)';
    });
    if (window.electronAPI && window.electronAPI.sendPin) window.electronAPI.sendPin(pinned);
  }

  /* Перемещение окна «за любую часть» при Alt — как в основном окне.
     screenX/screenY, а не clientX: под курсором двигается само окно. */
  function enableAltDrag() {
    var api = window.electronAPI;
    if (!api || !api.moveBy) return;
    var dragging = false, lastX = 0, lastY = 0;
    document.addEventListener('mousedown', function (e) {
      if (e.button !== 0 || !e.altKey) return;
      var t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      dragging = true; lastX = e.screenX; lastY = e.screenY;
      document.body.style.userSelect = 'none';
      e.preventDefault();
    }, true);
    document.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      var dx = e.screenX - lastX, dy = e.screenY - lastY;
      lastX = e.screenX; lastY = e.screenY;
      if (dx || dy) api.moveBy(dx, dy);
    }, true);
    ['mouseup', 'blur'].forEach(function (ev) {
      document.addEventListener(ev, function () {
        if (!dragging) return;
        dragging = false;
        document.body.style.userSelect = '';
      }, true);
    });
  }

  function wireTools() {
    ensureTools();
    if (window.AiBarEmoji) window.AiBarEmoji.boot();
    document.querySelectorAll('.aibar-pin').forEach(function (b) {
      b.addEventListener('click', function () { setPinned(!pinned); });
    });
    document.querySelectorAll('.aibar-region').forEach(function (b) {
      b.addEventListener('click', function () { captureRegion(); });
    });
    document.querySelectorAll('.aibar-shot').forEach(function (b) {
      b.addEventListener('click', function () { captureScreen(); });
    });
    document.querySelectorAll('.aibar-live').forEach(function (b) {
      b.addEventListener('click', function () {
        liveScreen = !liveScreen;
        document.querySelectorAll('.aibar-live').forEach(function (x) {
          x.classList.toggle('on', liveScreen);
        });
      });
    });
    document.querySelectorAll('.aibar-voice').forEach(function (b) {
      b.addEventListener('click', function () {
        /* Кнопка включает голосовой режим целиком: микрофон, слово-активация
           и озвучка ответов. Если движок речи недоступен — откатываемся на
           прежнюю диктовку (запись фразы в поле). */
        if (window.AiBarVoice) {
          if (window.AiBarVoice.enabled()) {
            window.AiBarVoice.disable();
            voiceMode = false;
            try { localStorage.setItem(VOICE_ON + sessionKey, '0'); } catch (e) { /* */ }
            paintVoiceBtn();
            return;
          }
          if (window.AiBarVoice.ready()) {
            voiceMode = true;
            try { localStorage.setItem(VOICE_ON + sessionKey, '1'); } catch (e) { /* */ }
            window.AiBarVoice.enable();
            paintVoiceBtn();
            return;
          }
        }
        if (rec) { stopVoice(); return; }
        startVoice(false);
      });
      b.addEventListener('dblclick', function (e) { e.preventDefault(); toggleVoiceMode(); });
    });
    // Правый клик по микрофону — прежняя диктовка одним нажатием.
    document.querySelectorAll('.aibar-voice').forEach(function (b) {
      b.addEventListener('contextmenu', function (e) {
        e.preventDefault();
        if (rec) { stopVoice(); return; }
        startVoice(false);
      });
    });
    document.querySelectorAll('.aibar-persona').forEach(function (b) {
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        togglePersonaMenu(b);
      });
    });
  }

  /* Ручной размер окна. Рамки нет, поэтому угол-«хваталка»: считаем, насколько
     растянули, и просим main-процесс изменить окно. innerWidth берём один раз
     на старте: во время перетаскивания окно уже едет вместе с курсором. */
  function ensureGrip() {
    if (!overlay) return;
    if (!window.electronAPI || !window.electronAPI.resizeAiBar) return;
    if (overlay.querySelector('.aibar-resize-grip')) return;
    var g = document.createElement('div');
    g.className = 'aibar-resize-grip';
    g.title = 'Изменить размер';
    overlay.appendChild(g);
    var dragging = false, sx = 0, sy = 0, sw = 0, sh = 0;
    g.addEventListener('mousedown', function (e) {
      dragging = true; sx = e.clientX; sy = e.clientY;
      sw = window.innerWidth; sh = window.innerHeight;
      e.preventDefault(); e.stopPropagation();
    });
    document.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      window.electronAPI.resizeAiBar(Math.round(sw + e.clientX - sx),
                                    Math.round(sh + e.clientY - sy));
    });
    document.addEventListener('mouseup', function () { dragging = false; });
  }

  /* Кнопка остановки живёт справа в строке ответа и появляется только на
     время запроса (body.aibar-busy). Поле ввода на это время блокируется,
     поэтому без кнопки прервать затянувшийся ответ было нечем. */
  function ensureStopButton() {
    if (!thread) return;
    var row = thread.querySelector('.aibar-thread-foot .aibar-input-row');
    if (!row || row.querySelector('.aibar-stop')) return;
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'aibar-stop';
    b.title = 'Остановить агента';
    b.setAttribute('aria-label', 'Остановить агента');
    b.innerHTML = '<i class="fas fa-stop"></i>';
    b.addEventListener('click', stopAgent);
    row.appendChild(b);
  }

  function stopAgent() {
    if (!currentAbort) return;
    try { currentAbort.abort(); } catch (e) { /* запрос уже завершён */ }
  }

  function wireHeader() {
    ensureHeader();
    ensureGrip();
    ensureStopButton();
    var music = thread.querySelector('.aibar-music');
    if (music) music.addEventListener('click', function () { setView('player'); });
    var avatarBtn = thread.querySelector('.aibar-avatar-btn');
    if (avatarBtn) {
      /* Кнопка есть только в оболочке Electron: в браузере окна аватара нет,
         и молчаливая кнопка-пустышка только путает. */
      if (window.electronAPI && window.electronAPI.toggleAvatar) {
        avatarBtn.addEventListener('click', function () { window.electronAPI.toggleAvatar(); });
      } else {
        avatarBtn.style.display = 'none';
      }
    }
    var notes = thread.querySelector('.aibar-notes-btn');
    if (notes) notes.addEventListener('click', function () { setView('notes'); });
    var msgs = thread.querySelector('.aibar-msgs-btn');
    if (msgs) msgs.addEventListener('click', function () { setView('messenger'); });
    var back = thread.querySelector('.aibar-back');
    if (back) back.addEventListener('click', function () { setView('chat'); });
    paintMusic();
  }

  /* Кнопка музыки показывает, что где-то играет трек: сам плеер может быть
     закрыт, а музыка — идти. Плеер сообщает об этом событием
     aibar:nowplaying (app/static/js/aibar_player.js). */
  function paintMusic(detail) {
    if (!thread) return;
    var b = thread.querySelector('.aibar-music');
    if (!b) return;
    if (!detail && window.AiBarPlayer) {
      var t = window.AiBarPlayer.nowPlaying();
      detail = t ? { title: t.title, artist: t.artist, playing: window.AiBarPlayer.isPlaying() } : null;
    }
    var on = !!(detail && detail.playing);
    b.classList.toggle('playing', on);
    var tab = overlay ? overlay.querySelector('.aibar-nav-tab[data-view="player"]') : null;
    if (tab) tab.classList.toggle('playing', on);
    b.title = (detail && detail.title)
      ? (on ? 'Играет: ' : 'Пауза: ') + [detail.artist, detail.title].filter(Boolean).join(' — ')
      : 'Музыка';
  }

  function init() {
    if (!mountInline()) {
      buildMarkup();
      overlay = $('aibarOverlay');
      panel = $('aibarPanel');
      thread = $('aibarThread');
      logEl = $('aibarLog');
      input = $('aibarInput');
      threadInput = $('aibarInput2');
      emptyEl = thread.querySelector('.aibar-empty');
      trayBtn = $('aibarTray');
      if (!overlay || !panel || !thread || !logEl) return;

      // Отдельное окно обёртки: страница сама и есть окно. Класс стоит на
      // <body> шаблона, но часть кода (CSS, тема) переносит его на <html> —
      // проверяем оба, иначе под Alt-перетаскивание окно не подпадёт.
      solo = document.documentElement.classList.contains('aibar-solo') ||
             document.body.classList.contains('aibar-solo');
    }

    if (overlay) {
      overlay.addEventListener('mousedown', function (e) {
        if (e.target === overlay) closeBar();
      });
    }
    if (input) input.addEventListener('keydown', function (e) { onKey(e, input); });
    threadInput.addEventListener('keydown', function (e) { onKey(e, threadInput); });

    var clear = thread.querySelector('[data-aibar-clear]') || $('aibarClear');
    if (clear) clear.addEventListener('click', clearChat);
    var openAgent = thread.querySelector('[data-aibar-open-agent]') || $('aibarOpenAgent');
    if (openAgent) openAgent.addEventListener('click', function () {
      window.open('/agentai', '_blank');
    });
    var closeBtn = $('aibarClose');
    if (closeBtn) closeBtn.addEventListener('click', closeBar);
    // Клик по индикатору: переписка уже есть — открываем её, пусто — строку запроса.
    if (trayBtn) trayBtn.addEventListener('click', function () { openBar(); });

    if (!inline) {
      // Esc обрабатываем сами всегда: в Electron глобальный хоткей относится
      // только к Shift+Space, а закрытие панели — дело страницы.
      document.addEventListener('keydown', onEscapeAnywhere);
      // В обёртке Electron хоткей глобальный (desktop/main.js) и приходит
      // сообщением: свой слушатель вешать нельзя — нажатие посчитается
      // дважды и переключение схлопнется в ноль. В браузере же это
      // единственный способ поймать Shift+Space.
      if (!(window.electronAPI && window.electronAPI.isElectron)) {
        document.addEventListener('keydown', onHotkey);
        // /desktop — оболочка вокруг iframe, поэтому фокус почти всегда внутри
        // него и keydown до этого документа не доходит. Вешаем слушатель туда же.
        attachHotkeyToFrames();
      }
      // Обёртка Electron: глобальный хоткей приходит сюда, когда основное
      // окно свернуто. В этом режиме само окно и есть палитра.
      if (window.electronAPI && window.electronAPI.onAiBarHotkey) {
        window.electronAPI.onAiBarHotkey(function (payload) { hotkeyAction(payload); });
      }
      if (window.electronAPI && window.electronAPI.onAiBarSubmit) {
        window.electronAPI.onAiBarSubmit(function (text) { send(text); });
      }
      // Пока агент отвечает, окно палитры не должно прятаться по потере фокуса.
      if (window.electronAPI && window.electronAPI.on) {
        window.electronAPI.on('ai-bar-busy', function (v) { busy = !!v; });
        // Оболочка снимает закрепление сама (возврат приложения из трея) —
        // кнопка должна отражать настоящее состояние окна.
        window.electronAPI.on('ai-bar-pin', function (v) {
          pinned = !!v;
          document.querySelectorAll('.aibar-pin').forEach(function (b) {
            b.classList.toggle('on', pinned);
          });
        });
        /* Показ и скрытие окна решают, где живёт звук: играет то окно,
           которое видно. Плеер сам отдаёт или забирает трек. */
        window.electronAPI.on('ai-bar-shown', function () {
          if (window.AiBarPlayer) window.AiBarPlayer.onWindowShown();
        });
        window.electronAPI.on('ai-bar-hidden', function () {
          if (window.AiBarPlayer) window.AiBarPlayer.onWindowHidden();
        });
      }
    }

    document.addEventListener('aibar:nowplaying', function (e) { paintMusic(e.detail); });
    watchRevision();
    if (!inline) {
      wireTools();
      wireHeader();
      loadVoiceSettings();
      bootVoice();
      if (solo) enableAltDrag();
    }
    try { persona = localStorage.getItem(personaKey()) || 'default'; } catch (e) { persona = 'default'; }
    activeSession = readActiveSession();
    loadPersonas().then(function () {
      // Личность могла прийти с сервера (по профилю) — перечитываем ключ.
      try { persona = localStorage.getItem(personaKey()) || persona; } catch (e) { /* */ }
      activeSession = readActiveSession();
      renderSessions();
      loadSessions();
      loadTranscript().then(function () {
        // В отдельном окне окно и есть палитра — показываем её сразу.
        if (solo) openBar();
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.AIBar = {
    open: openBar,
    close: closeBar,
    toggle: toggleBar,
    send: send,
    clear: clearChat,
    reload: loadTranscript,
    isBusy: function () { return busy; },
    isInline: function () { return inline; },
    setTray: setTray,
    shot: captureScreen,
    region: captureRegion,
    voice: startVoice,
    hotkey: hotkeyAction,
    setView: setView,
    setPinned: setPinned,
    view: function () { return view; },
    isSolo: function () { return solo; },
    isPinned: function () { return pinned; },
    attachments: function () { return attachments.length; },
    setLive: function (v) { liveScreen = !!v; },
    setVoiceMode: function (v) {
      voiceMode = !!v;
      try { localStorage.setItem(VOICE_ON + sessionKey, voiceMode ? '1' : '0'); } catch (e) { /* */ }
      paintVoiceBtn();
    },
    voiceReady: function () { return sttReady; }
  };
})();