/* Оболочка окна 3D-аватара (Electron, /avatar?embed=1).
 *
 * Задача: оставить на экране только модель, дать всегда доступную
 * шестерёнку настроек и голосовой режим. Голос — это визуализация
 * мини-чата: распознанная фраза уходит агенту (/api/agentai/ask), ответ
 * озвучивает сам аватар через window.talkingheadSpeak (он уже умеет
 * lipsync через настроенный голос), с запасным вариантом AiBarVoice.speak.
 *
 * Всё строится только при ?embed=1, чтобы страница /avatar не изменилась.
 */
(function () {
  'use strict';

  if (!/[?&]embed=1/.test(location.search)) return;
  if (window.__avatarOverlayBooted) return;
  window.__avatarOverlayBooted = true;

  var LS = {
    size: 'avatar_overlay_size',      /* 's' | 'm' | 'l' */
    opacity: 'avatar_overlay_opacity',
    voice: 'avatar_overlay_voice'
  };
  var SIZES = { s: [260, 360], m: [340, 480], l: [460, 640] };
  var OPS = [1, 0.85, 0.7];
  var ASK = '/api/agentai/ask';
  var SESSION = 'avatar-overlay';

  var busy = false;
  var menuOpen = false;

  function ls(key, fallback) {
    try { var v = localStorage.getItem(key); return v == null ? fallback : v; }
    catch (e) { return fallback; }
  }
  function lsSet(key, v) { try { localStorage.setItem(key, v); } catch (e) { /* */ } }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // ------------------------------------------------------------ подсказка
  var statusEl = null, statusTimer = null;
  function status(html, isErr, sticky) {
    if (!statusEl) return;
    statusEl.innerHTML = html;
    statusEl.classList.toggle('err', !!isErr);
    statusEl.classList.add('show');
    clearTimeout(statusTimer);
    if (!sticky) {
      statusTimer = setTimeout(function () {
        statusEl.classList.remove('show');
      }, isErr ? 9000 : 6000);
    }
  }

  // ------------------------------------------------------------ агент
  function askAgent(text) {
    if (busy) return Promise.resolve('');
    busy = true;
    setVoiceStage('think');
    status('<b>Вы:</b> ' + escapeHtml(text) + '<br><b>Агент:</b> думает…', false, true);

    var body = { message: text, session_key: SESSION, title: 'Голосовой аватар' };
    var acc = '';

    return fetch(ASK, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok || !r.body) throw new Error('сервер ответил ' + r.status);
      return readSSE(r.body, function (chunk, finalText) {
        acc = finalText || (acc + chunk);
        status('<b>Вы:</b> ' + escapeHtml(text) + '<br><b>Агент:</b> ' +
               escapeHtml(acc.slice(-1200)), false, true);
      });
    }).then(function (finalText) {
      var reply = (finalText || acc || '').trim();
      busy = false;
      setVoiceStage('idle');
      if (!reply) {
        status('Агент вернул пустой ответ.', true);
        return '';
      }
      status('<b>Вы:</b> ' + escapeHtml(text) + '<br><b>Агент:</b> ' + escapeHtml(reply));
      speak(reply);
      return reply;
    }).catch(function (e) {
      busy = false;
      setVoiceStage('idle');
      status('Не удалось получить ответ: ' + escapeHtml(e && e.message ? e.message : e), true);
      return '';
    });
  }

  /* Ответ агента: сначала просим аватара говорить (он делает lipsync),
     если страница не умеет — обычный голос мини-приложения. */
  function speak(text) {
    if (typeof window.talkingheadSpeak === 'function') {
      try {
        var p = window.talkingheadSpeak(text);
        if (p && p.catch) {
          p.catch(function () { fallbackSpeak(text); });
        }
        return;
      } catch (e) { /* ниже запасной вариант */ }
    }
    fallbackSpeak(text);
  }
  function fallbackSpeak(text) {
    if (window.AiBarVoice && window.AiBarVoice.speak) {
      window.AiBarVoice.speak(text);
    } else if (window.speechSynthesis) {
      try {
        var u = new SpeechSynthesisUtterance(text);
        u.lang = 'ru-RU';
        speechSynthesis.speak(u);
      } catch (e) { /* нет озвучки — не критично */ }
    }
  }

  function readSSE(body, onChunk) {
    var reader = body.getReader();
    var dec = new TextDecoder();
    var buf = '';
    var acc = '';
    var finalText = '';

    function handle(ev, data) {
      if (ev === 'assistant.delta' || ev === 'assistant.reasoning') {
        acc += (data && data.delta) || '';
        onChunk('', acc);
      } else if (ev === 'assistant.completed' || ev === 'run.completed') {
        var f = (data && (data.content || data.final_response || data.output)) || '';
        if (f) { finalText = f; acc = f; onChunk('', acc); }
      } else if (ev === 'run.failed' || ev === 'run.cancelled') {
        if (!acc) acc = 'Агент не закончил ответ.';
      } else if (ev === 'error') {
        throw new Error((data && data.message) || 'агент вернул ошибку');
      }
    }

    function pump() {
      return reader.read().then(function (r) {
        if (r.done) return finalText || acc;
        buf += dec.decode(r.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          var frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          var ev = 'message', data = null;
          frame.split('\n').forEach(function (ln) {
            if (ln.indexOf('event:') === 0) ev = ln.slice(6).trim();
            else if (ln.indexOf('data:') === 0) {
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

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ------------------------------------------------------------ голос
  var voiceOn = false;
  var voiceBtn = null;

  function setVoiceStage(stage) {
    if (!voiceBtn) return;
    voiceBtn.classList.toggle('rec', stage === 'listen');
    voiceBtn.classList.toggle('think', stage === 'think');
    voiceBtn.classList.toggle('talk', stage === 'speak');
  }

  function initVoice() {
    if (!window.AiBarVoice) return;
    window.AiBarVoice.onText(function (text) {
      if (!text || !text.trim()) return;
      askAgent(text.trim());
    });
    window.AiBarVoice.onState(function (stage) {
      setVoiceStage(stage === 'speak' ? 'talk' : stage);
    });
    window.AiBarVoice.boot().then(function () {
      if (!window.AiBarVoice.ready()) {
        if (voiceBtn) voiceBtn.title = 'Голосовой ввод недоступен (STT не настроен)';
        return;
      }
      if (ls(LS.voice, '0') === '1') toggleVoice(true);
    });
  }

  function toggleVoice(force) {
    if (!window.AiBarVoice || !window.AiBarVoice.ready()) {
      status('Голосовой ввод недоступен: не настроено распознавание речи ' +
             '(STT) в настройках приложения.', true);
      return;
    }
    var want = typeof force === 'boolean' ? force : !voiceOn;
    if (want) {
      window.AiBarVoice.enable().then(function (ok) {
        voiceOn = !!ok;
        lsSet(LS.voice, voiceOn ? '1' : '0');
        if (voiceBtn) voiceBtn.classList.toggle('on', voiceOn);
        if (voiceOn) {
          status('Голосовой режим включён. Скажите фразу — она уйдёт агенту, ' +
                 'ответ аватар озвучит.', false);
        } else {
          status('Не удалось включить микрофон.', true);
        }
      });
    } else {
      window.AiBarVoice.disable();
      voiceOn = false;
      lsSet(LS.voice, '0');
      if (voiceBtn) voiceBtn.classList.remove('on');
      status('Голосовой режим выключен.', false);
    }
  }

  // ------------------------------------------------------------ модель
  /* Модель по умолчанию не грузится (портальный движок ждёт клика по
     пресету в панели). В окне-оверлее панели нет — выбираем пресет сами,
     но сначала сохранённый в настройках: раньше здесь жёстко кликался
     Brunette и смена модели «не применялась» до перезапуска. */
  var modelTimer = null, modelTries = 0;

  function savedAvatarName() {
    try {
      var raw = localStorage.getItem('talkinghead');
      if (!raw) return '';
      var c = JSON.parse(raw);
      /* Модель живёт в конфиге сессии: sessions[CFG.session].avatar.name,
         а не в корне файла. */
      var list = Array.isArray(c && c.sessions) ? c.sessions : [];
      var s = list[typeof c.session === 'number' ? c.session : 0] || list[0];
      var n = s && s.avatar && s.avatar.name;
      return typeof n === 'string' ? n : '';
    } catch (e) { return ''; }
  }

  function findPreset(name) {
    if (!name) return null;
    try {
      return document.querySelector('[data-avatar-name="' +
        String(name).replace(/["\\]/g, '\\$&') + '"]');
    } catch (e) { return null; }
  }

  function autoLoadModel() {
    if (window.head && window.head.avatar) {
      modelTries = 0;
      if (window.__infinityReloadAvatar) {
        try { window.__infinityReloadAvatar(); } catch (e) { /* */ }
      }
      return;
    }
    var want = savedAvatarName();
    /* Сохранённую модель portable.js поднимает сам (блок «Show last avatar»).
       Клик по пресету здесь запускал вторую загрузку параллельно — движок
       валился с «Avatar object Hips not found», и в окне оставалась первая
       модель из siteconfig. Поэтому при сохранённой модели просто ждём. */
    if (want && want !== 'FILE' && modelTries < 8) {
      modelTries++;
      clearTimeout(modelTimer);
      modelTimer = setTimeout(autoLoadModel, 1500);
      return;
    }
    var preset = (want && want !== 'FILE' ? findPreset(want) : null) ||
                 findPreset('Brunette');
    if (preset && modelTries < 14) {
      modelTries++;
      try {
        preset.dispatchEvent(new MouseEvent('click', {
          bubbles: true, cancelable: true, view: window
        }));
      } catch (e) { /* попробуем ещё раз */ }
    } else if (modelTries >= 14) {
      status('Не удалось загрузить модель аватара. Проверьте, что файлы ' +
             'моделей (avatars/*.glb) на месте.', true);
      return;
    }
    clearTimeout(modelTimer);
    modelTimer = setTimeout(autoLoadModel, 2500);
  }

  /* Окно живёт всё время работы мини-приложения: если настройки поменяли
     в другом окне, а сообщение через localStorage/BroadcastChannel не
     дошло, перечитываем их при возврате фокуса. */
  var lastConfigRaw = null;
  function reloadIfChanged() {
    var raw = '';
    try { raw = localStorage.getItem('talkinghead') || ''; } catch (e) { return; }
    if (!raw || raw === lastConfigRaw) return;
    /* Модель ещё грузится — второй apply запустит параллельную загрузку. */
    if (!window.head || !window.head.avatar) return;
    lastConfigRaw = raw;
    if (window.__infinityReloadAvatar) {
      try { window.__infinityReloadAvatar(); } catch (e) { /* */ }
    }
  }

  // ------------------------------------------------------------ панель
  var bar, menu;

  function buildBar() {
    bar = el('div', 'av-bar');
    bar.innerHTML =
      '<span class="av-grip">Аватар — тяните за эту полосу</span>' +
      '<button type="button" class="av-voice" title="Голосовой режим">' +
        '<i class="fas fa-microphone"></i></button>' +
      '<button type="button" class="av-settings" title="Настройки аватара">' +
        '<i class="fas fa-gear"></i></button>' +
      '<button type="button" class="av-hide" title="Скрыть аватар">' +
        '<i class="fas fa-eye-slash"></i></button>';
    document.body.appendChild(bar);
    voiceBtn = bar.querySelector('.av-voice');

    voiceBtn.addEventListener('click', function () { toggleVoice(); });
    bar.querySelector('.av-hide').addEventListener('click', function () {
      if (window.electronAPI && window.electronAPI.hideAvatar) window.electronAPI.hideAvatar();
      else bar.style.display = 'none';
    });
    bar.querySelector('.av-settings').addEventListener('click', function (e) {
      e.stopPropagation();
      toggleMenu();
    });

    statusEl = el('div', 'av-status');
    document.body.appendChild(statusEl);

    /* Ручка размера: у прозрачных окон Windows системный край не всегда
       ловится, поэтому тянем сами и просим оболочку поменять размер. */
    if (window.electronAPI && window.electronAPI.resizeAvatar) {
      var grip = el('div', 'av-resize-corner');
      grip.title = 'Изменить размер окна';
      document.body.appendChild(grip);
      grip.addEventListener('pointerdown', function (e) {
        e.preventDefault();
        e.stopPropagation();
        var sx = e.clientX, sy = e.clientY;
        var w0 = window.innerWidth, h0 = window.innerHeight;
        function move(ev) {
          window.electronAPI.resizeAvatar(
            Math.max(220, Math.round(w0 + ev.clientX - sx)),
            Math.max(260, Math.round(h0 + ev.clientY - sy))
          );
        }
        function up() {
          document.removeEventListener('pointermove', move);
          document.removeEventListener('pointerup', up);
          lsSet(LS.size, 'custom');
          paintSettings();
        }
        document.addEventListener('pointermove', move);
        document.addEventListener('pointerup', up);
      });
    }
  }

  function buildMenu() {
    menu = el('div', 'av-menu');
    menu.innerHTML =
      '<h4>Размер</h4>' +
      '<div class="av-row" data-group="size">' +
        '<button type="button" data-size="s">Маленький</button>' +
        '<button type="button" data-size="m">Обычный</button>' +
        '<button type="button" data-size="l">Большой</button>' +
      '</div>' +
      '<h4>Прозрачность</h4>' +
      '<div class="av-row" data-group="opacity">' +
        '<button type="button" data-op="1">100%</button>' +
        '<button type="button" data-op="0.85">85%</button>' +
        '<button type="button" data-op="0.7">70%</button>' +
      '</div>' +
      '<div class="av-sep"></div>' +
      '<button type="button" class="av-wide av-voice-toggle">' +
        '<i class="fas fa-microphone"></i>&nbsp; Голосовой режим</button>' +
      '<button type="button" class="av-wide av-reload-model">' +
        '<i class="fas fa-arrows-rotate"></i>&nbsp; Показать модель заново</button>' +
      '<button type="button" class="av-wide av-open-avatar">' +
        '<i class="fas fa-sliders"></i>&nbsp; Полные настройки аватара</button>' +
      '<div class="av-hint">Окно можно тянуть за полосу сверху и менять размер ' +
        'за нижний правый угол.</div>';
    document.body.appendChild(menu);

    menu.querySelectorAll('[data-size]').forEach(function (b) {
      b.addEventListener('click', function () { applySize(b.getAttribute('data-size')); });
    });
    menu.querySelectorAll('[data-op]').forEach(function (b) {
      b.addEventListener('click', function () { applyOpacity(parseFloat(b.getAttribute('data-op'))); });
    });
    menu.querySelector('.av-voice-toggle').addEventListener('click', function () { toggleVoice(); });
    menu.querySelector('.av-reload-model').addEventListener('click', function () {
      modelTries = 0;
      autoLoadModel();
      closeMenu();
    });
    menu.querySelector('.av-open-avatar').addEventListener('click', function () {
      if (window.electronAPI && window.electronAPI.openAppWindow) {
        window.electronAPI.openAppWindow({ url: location.origin + '/avatar', title: 'Настройки аватара' });
      } else {
        window.open('/avatar', '_blank');
      }
      closeMenu();
    });

    paintSettings();
    document.addEventListener('click', function (e) {
      if (!menuOpen) return;
      if (menu.contains(e.target) || (bar && bar.contains(e.target))) return;
      closeMenu();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeMenu();
    });
  }

  function paintSettings() {
    if (!menu) return;
    var size = ls(LS.size, 'm');
    menu.querySelectorAll('[data-size]').forEach(function (b) {
      b.classList.toggle('on', b.getAttribute('data-size') === size);
    });
    var op = String(parseFloat(ls(LS.opacity, '1')));
    menu.querySelectorAll('[data-op]').forEach(function (b) {
      b.classList.toggle('on', b.getAttribute('data-op') === op);
    });
    menu.querySelector('.av-voice-toggle').classList.toggle('on', voiceOn);
  }

  function applySize(key) {
    var s = SIZES[key] || SIZES.m;
    lsSet(LS.size, key);
    if (window.electronAPI && window.electronAPI.resizeAvatar) {
      window.electronAPI.resizeAvatar(s[0], s[1]);
    }
    paintSettings();
  }

  function applyOpacity(v) {
    lsSet(LS.opacity, String(v));
    if (window.electronAPI && window.electronAPI.setAvatarOpacity) {
      window.electronAPI.setAvatarOpacity(v);
    }
    paintSettings();
  }

  function toggleMenu() { if (menuOpen) closeMenu(); else openMenu(); }
  function openMenu() { menu.classList.add('open'); menuOpen = true; }
  function closeMenu() { if (!menu) return; menu.classList.remove('open'); menuOpen = false; }

  // ------------------------------------------------------------ старт
  function init() {
    buildBar();
    buildMenu();
    /* На старте только запоминаем конфиг: модель уже грузит portable.js,
       а лишний apply в этот момент валит движок второй загрузкой. */
    try { lastConfigRaw = localStorage.getItem('talkinghead') || ''; }
    catch (e) { lastConfigRaw = null; }
    window.addEventListener('focus', reloadIfChanged);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) reloadIfChanged();
    });
    // Сохранённые настройки применяем к окну (оболочка Electron).
    if (window.electronAPI) {
      var op = parseFloat(ls(LS.opacity, '1'));
      if (isFinite(op) && op < 1 && window.electronAPI.setAvatarOpacity) {
        window.electronAPI.setAvatarOpacity(op);
      }
    }
    var voiceCss = document.createElement('link');
    voiceCss.rel = 'stylesheet';
    voiceCss.href = '/static/css/aibar_voice.css';
    document.head.appendChild(voiceCss);
    var vs = document.createElement('script');
    vs.src = '/static/js/aibar_voice.js';
    vs.onload = initVoice;
    document.head.appendChild(vs);

    /* Даём portable.js собрать сцену и только потом просим модель. */
    setTimeout(autoLoadModel, 2500);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
