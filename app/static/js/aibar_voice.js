/* Голосовой режим окна палитры (Shift+Space).
 *
 * На странице агента голос устроен так: микрофон слышит всё время, фразы
 * режутся по паузам, распознаются и ждут слова-активации («финик»); после
 * активации следующие фразы уходят агенту, а его ответы озвучиваются.
 * Здесь — тот же ход вещей, но в маленьком окне:
 *
 *   off    — микрофон выключен;
 *   wake   — микрофон слушает, ждём слово-активации;
 *   listen — идёт фраза (пишем её в поле ввода);
 *   think  — агент думает;
 *   speak  — озвучиваем ответ агента.
 *
 * Движки те же, что у агента: /agentai/api/voice/stt и /agentai/api/voice/tts
 * (это переходники на движок приложения). Если они недоступны — работает
 * запасной путь через /api/stt/transcribe.
 */
(function () {
  'use strict';

  var STT_URL = '/agentai/api/voice/stt';
  var STT_FALLBACK = '/api/stt/transcribe';
  var TTS_URL = '/agentai/api/voice/tts';
  var WAKE = 'финик';
  var STOP_WORDS = ['стоп', 'стой', 'отмена', 'хватит', 'замолчи', 'выключись'];

  var BLK = 2048;                 /* ~128 мс при 16 кГц */
  var SILENCE_MS = 800;           /* пауза, отделяющая фразу */
  var MAX_UTT_MS = 14000;

  var state = {
    mode: 'off',          /* off | wake | dialog */
    stage: 'off',         /* off | wake | listen | think | speak */
    level: 0,
    ready: false,
    lang: 'ru',
    dialog: false,        /* ждём ли продолжения диалога */
    direct: false,        /* фраза уходит агенту сразу (хоткей «говорить») */
    pending: '',
    onText: null,         /* подписчик: готовый текст запроса */
    onState: null
  };

  var stream = null, ctx = null, node = null, sink = null, srcNode = null;
  var running = false, busy = false;
  var preroll = [], utt = [], uttSamples = 0, queue = [];
  var inSpeech = false, silMs = 0, noise = 0.005, lastStage = '';
  var audio = null;       /* текущая озвучка ответа */
  var listeners = [];

  // ------------------------------------------------------------- утилиты
  function setStage(s) {
    if (s === lastStage) return;
    lastStage = s;
    state.stage = s;
    paint();
    listeners.forEach(function (cb) { try { cb(s); } catch (e) { /* */ } });
  }

  function paint() {
    var label = !state.ready ? ''
      : (state.stage === 'listen' ? 'Слушаю…'
        : (state.stage === 'think' ? 'Думаю…'
          : (state.stage === 'speak' ? 'Говорю…'
            : (state.mode === 'off' ? '' : 'Жду «' + WAKE + '»'))));
    /* Кнопок голоса две — в панели запроса и в треде. Красим обе: иначе
       одна из них осталась бы без подписи состояния. */
    document.querySelectorAll('.aibar-voice').forEach(function (btn) {
      btn.classList.toggle('on', state.mode !== 'off');
      btn.classList.toggle('rec', state.stage === 'listen');
      btn.classList.toggle('think', state.stage === 'think');
      btn.classList.toggle('talk', state.stage === 'speak');
      btn.classList.toggle('off', !state.ready);
      btn.style.setProperty('--lvl', state.level.toFixed(3));
      btn.innerHTML = '<i class="fas fa-microphone"></i>' +
        (label ? '<span class="aibar-voice-state">' + label + '</span>' : '');
    });
  }

  function rms(b) {
    var s = 0;
    for (var i = 0; i < b.length; i++) s += b[i] * b[i];
    return Math.sqrt(s / b.length);
  }

  /* Порог: тише — значит ближе к шуму. Шум оцениваем только в тишине. */
  function threshold() { return 0.010; }

  function hasWake(text) {
    var t = String(text || '').toLowerCase();
    return t.indexOf(WAKE) >= 0;
  }

  function hasStop(text) {
    var t = String(text || '').toLowerCase();
    for (var i = 0; i < STOP_WORDS.length; i++) {
      if (t.indexOf(STOP_WORDS[i]) >= 0) return true;
    }
    return false;
  }

  /* Текст после слова-активации — это и есть запрос. */
  function afterWake(text) {
    var t = String(text || '');
    var i = t.toLowerCase().indexOf(WAKE);
    if (i < 0) return t.trim();
    return t.slice(i + WAKE.length).replace(/^[\s,.:;—-]+/, '').trim();
  }

  // ------------------------------------------------------------- запись
  function process(ch) {
    if (!running) return;
    var block = new Float32Array(ch);
    var level = rms(block);
    state.level = level;
    var voicy = level > Math.max(threshold(), noise * 3);
    if (!voicy) noise = noise * 0.98 + level * 0.02;
    preroll.push(block);
    if (preroll.length > 4) preroll.shift();
    if (!inSpeech) {
      if (voicy) {
        inSpeech = true;
        utt = preroll.slice();
        uttSamples = utt.reduce(function (a, b) { return a + b.length; }, 0);
        silMs = 0;
        setStage('listen');
      }
      return;
    }
    utt.push(block);
    uttSamples += block.length;
    silMs = voicy ? 0 : silMs + (block.length / (ctx ? ctx.sampleRate : 16000)) * 1000;
    if (silMs >= SILENCE_MS || uttSamples > (ctx ? ctx.sampleRate : 16000) * MAX_UTT_MS / 1000) {
      finalize();
    }
  }

  function finalize() {
    var parts = utt;
    utt = [];
    preroll = [];
    var wasSpeech = inSpeech;
    inSpeech = false;
    silMs = 0;
    if (state.stage === 'listen') setStage(state.mode === 'off' ? 'off' : 'wake');
    if (!wasSpeech) return;
    var rate = ctx ? ctx.sampleRate : 16000;
    var total = parts.reduce(function (a, b) { return a + b.length; }, 0);
    if (total / rate < 0.3) return;              /* слишком коротко — не фраза */
    queue.push({ parts: parts, total: total, rate: rate });
    drain();
  }

  function drain() {
    if (busy) return;
    busy = true;
    var jobs = queue.splice(0, queue.length);
    jobs.reduce(function (p, job) {
      return p.then(function () { return transcribe(job); });
    }, Promise.resolve()).then(function () {
      busy = false;
      if (queue.length) drain();
    }).catch(function () {
      busy = false;
    });
  }

  // ------------------------------------------------------------- STT
  function encodeWav(pcm, rate) {
    var n = pcm.length;
    var ab = new ArrayBuffer(44 + n * 2);
    var v = new DataView(ab);
    function ws(o, s) { for (var i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); }
    ws(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); ws(8, 'WAVE'); ws(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    ws(36, 'data'); v.setUint32(40, n * 2, true);
    var o = 44;
    for (var i = 0; i < n; i++) {
      var s = Math.max(-1, Math.min(1, pcm[i]));
      v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
      o += 2;
    }
    return new Blob([ab], { type: 'audio/wav' });
  }

  function transcribe(job) {
    var pcm = new Float32Array(job.total);
    var off = 0;
    for (var i = 0; i < job.parts.length; i++) {
      pcm.set(job.parts[i], off);
      off += job.parts[i].length;
    }
    var blob = encodeWav(pcm, job.rate);
    return stt(blob).then(function (text) {
      if (!text) return;
      handleText(text);
    }).catch(function (e) {
      console.warn('[aibar-voice] STT не удался:', e);
    });
  }

  function stt(blob) {
    var fd = new FormData();
    fd.append('file', blob, 'voice.wav');
    fd.append('language', state.lang);
    return fetch(STT_URL, { method: 'POST', body: fd })
      .then(function (r) {
        if (!r.ok) throw new Error('stt ' + r.status);
        return r.json();
      })
      .then(function (d) {
        return String((d && (d.text || (d.result && d.result.text))) || '').trim();
      })
      .catch(function () {
        /* Запасной путь: движок приложения (JSON с аудио в base64). */
        return blob.arrayBuffer().then(function (ab) {
          var bin = '';
          var bytes = new Uint8Array(ab);
          for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
          return fetch(STT_FALLBACK, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ audio: btoa(bin), language: state.lang })
          }).then(function (r) {
            if (!r.ok) throw new Error('stt-fallback ' + r.status);
            return r.json();
          }).then(function (d) {
            return String((d && (d.text || (d.result && d.result.text))) || '').trim();
          });
        });
      });
  }

  /* Что делать с распознанной фразой. */
  function handleText(text) {
    var low = text.toLowerCase();
    /* Нажали хоткей «говорить» — фраза уходит агенту сразу, без слова-активации. */
    if (state.direct) {
      state.direct = false;
      state.dialog = true;
      emit(text);
      return;
    }
    if (state.dialog && hasStop(text) && !hasWake(text)) {
      state.dialog = false;
      setStage('wake');
      return;
    }
    if (hasWake(text)) {
      var rest = afterWake(text);
      state.dialog = true;
      if (rest.length > 2) {
        emit(rest);
        return;
      }
      setStage('wake');
      return;
    }
    if (state.dialog) {
      emit(text);
      return;
    }
    /* Вне диалога слова-активации нет — фраза идёт в поле ввода, чтобы
       человек мог её прочитать и поправить (как при обычной диктовке). */
    if (state.onText) state.onText(text, false);
  }

  function emit(text) {
    if (state.onText) state.onText(text, true);
  }

  // ------------------------------------------------------------- микрофон
  function startMic() {
    if (running) return Promise.resolve(true);
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.resolve(false);
    }
    return navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    }).then(function (st) {
      stream = st;
      try {
        ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      } catch (e) {
        ctx = new (window.AudioContext || window.webkitAudioContext)();
      }
      try { ctx.resume(); } catch (e) { /* */ }
      srcNode = ctx.createMediaStreamSource(stream);
      node = ctx.createScriptProcessor(BLK, 1, 1);
      node.onaudioprocess = function (e) { process(e.inputBuffer.getChannelData(0)); };
      sink = ctx.createGain();
      sink.gain.value = 0;                       /* без эха в колонки */
      srcNode.connect(node);
      node.connect(sink);
      sink.connect(ctx.destination);
      running = true;
      inSpeech = false;
      utt = [];
      preroll = [];
      silMs = 0;
      lastStage = '';
      return true;
    }).catch(function (e) {
      console.warn('[aibar-voice] микрофон недоступен:', e);
      return false;
    });
  }

  function stopMic() {
    if (!running) return;
    running = false;
    inSpeech = false;
    utt = [];
    preroll = [];
    silMs = 0;
    try { node && node.disconnect(); } catch (e) { /* */ }
    try { srcNode && srcNode.disconnect(); } catch (e) { /* */ }
    try { sink && sink.disconnect(); } catch (e) { /* */ }
    try { stream && stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) { /* */ }
    try { ctx && ctx.close(); } catch (e) { /* */ }
    stream = ctx = node = sink = srcNode = null;
    state.level = 0;
  }

  // ------------------------------------------------------------- TTS
  function speak(text) {
    var t = String(text || '').trim();
    if (!t) return Promise.resolve();
    stopSpeak();
    setStage('speak');
    var fd = new FormData();
    fd.append('input', t);
    fd.append('language', state.lang);
    return fetch(TTS_URL, { method: 'POST', body: fd })
      .then(function (r) {
        if (!r.ok || r.status === 204) throw new Error('tts ' + r.status);
        return r.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        audio = new Audio(url);
        audio.onended = function () {
          URL.revokeObjectURL(url);
          audio = null;
          setStage(state.mode === 'off' ? 'off' : (state.dialog ? 'wake' : 'wake'));
        };
        audio.onerror = function () {
          URL.revokeObjectURL(url);
          audio = null;
          setStage(state.mode === 'off' ? 'off' : 'wake');
        };
        return audio.play();
      })
      .catch(function (e) {
        console.warn('[aibar-voice] озвучка не удалась:', e);
        setStage(state.mode === 'off' ? 'off' : 'wake');
      });
  }

  function stopSpeak() {
    if (audio) {
      try { audio.pause(); } catch (e) { /* */ }
      try { audio.currentTime = 0; } catch (e) { /* */ }
      audio = null;
    }
  }

  // ------------------------------------------------------------- режимы
  function enable() {
    if (!state.ready) return Promise.resolve(false);
    state.mode = 'wake';
    state.dialog = false;
    setStage('wake');
    return startMic();
  }

  function disable() {
    state.mode = 'off';
    state.dialog = false;
    stopSpeak();
    stopMic();
    setStage('off');
  }

  function toggle() {
    if (state.mode === 'off') {
      return enable().then(function (ok) {
        if (!ok) {
          state.mode = 'off';
          setStage('off');
        }
        return state.mode !== 'off';
      });
    }
    disable();
    return false;
  }

  /* Агент закончил думать — озвучиваем ответ. */
  function onAgentReply(text) {
    if (state.mode === 'off') return;
    speak(text);
  }

  /* Хоткей «говорить»: следующая фраза уходит агенту сразу. */
  function pushToTalk() {
    if (state.mode === 'off') return Promise.resolve(false);
    state.direct = true;
    state.dialog = true;
    return startMic();
  }

  function onAgentBusy(busy) {
    if (state.mode === 'off') return;
    if (busy) setStage('think');
    else if (state.stage === 'think') setStage(state.dialog ? 'wake' : 'wake');
  }

  // ------------------------------------------------------------- настройки
  function loadSettings() {
    return Promise.all([
      fetch('/api/config', { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; }),
      fetch('/api/stt/status', { cache: 'no-store' }).then(function (r) { return r.ok; }).catch(function () { return false; })
    ]).then(function (res) {
      var cfg = res[0] || {};
      var sp = cfg.speech || {};
      state.lang = ((sp.stt || {}).local_language) || 'ru';
      state.ready = !!res[1];
      paint();
    }).catch(function () {
      paint();
    });
  }

  window.AiBarVoice = {
    boot: loadSettings,
    toggle: toggle,
    enable: enable,
    disable: disable,
    pushToTalk: pushToTalk,
    speak: speak,
    stopSpeak: stopSpeak,
    onAgentReply: onAgentReply,
    onAgentBusy: onAgentBusy,
    ready: function () { return state.ready; },
    enabled: function () { return state.mode !== 'off'; },
    stage: function () { return state.stage; },
    level: function () { return state.level; },
    isSpeaking: function () { return !!audio; },
    onState: function (cb) { listeners.push(cb); },
    onText: function (cb) { state.onText = cb; }
  };
})();
