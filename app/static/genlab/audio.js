/* Звуковой редактор GenLab.
 *
 * Отдельный модуль, как и редактор области: тянет из window.GenLab только
 * api/toast/галерею. Обработка «без ИИ» идёт прямо в браузере через Web Audio
 * (OfflineAudioContext для эквалайзера, ручной DSP для громкости/затуханий/
 * обрезки), поэтому результат мгновенный и не зависит от моделей. Экспорт —
 * WAV, затем обычная выгрузка в галерею.
 *
 * Разметка строится один раз (build) и переиспользуется. Состояние — session.
 */

const bridge = () => window.GenLab || {};

let dom = null;
let session = null;
let audioCtx = null;

const HISTORY_LIMIT = 12;

function actx() {
  if (!audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    audioCtx = new Ctx();
  }
  return audioCtx;
}

/* ------------------------------------------------------------- разметка */

const TEMPLATE = `
<div class="gl-audio" id="glAudio" hidden>
  <div class="gl-editor-top">
    <span class="gl-editor-title"><i class="fas fa-wave-square"></i> <span id="adName">Звук</span></span>
    <span class="gl-spacer"></span>
    <button class="gl-btn gl-btn-ghost" type="button" id="adUndo" title="Отменить (Ctrl+Z)" disabled><i class="fas fa-rotate-left"></i></button>
    <button class="gl-btn gl-btn-ghost" type="button" id="adRedo" title="Повторить (Ctrl+Y)" disabled><i class="fas fa-rotate-right"></i></button>
    <span class="gl-edit-status" id="adStatus"></span>
    <button class="gl-icon-btn" type="button" id="adClose" title="Закрыть (Esc)"><i class="fas fa-xmark"></i></button>
  </div>

  <div class="gl-editor-main">
    <div class="gl-audio-stage">
      <div class="gl-audio-toolbar">
        <button class="gl-btn gl-btn-primary" type="button" id="adPlay"><i class="fas fa-play"></i> Играть</button>
        <button class="gl-btn gl-btn-ghost" type="button" id="adPlaySel" title="Проиграть выделенное"><i class="fas fa-play"></i> Выделение</button>
        <button class="gl-btn gl-btn-ghost" type="button" id="adStop"><i class="fas fa-stop"></i></button>
        <span class="gl-audio-time" id="adTime">0:00.0</span>
        <span class="gl-spacer"></span>
        <span class="gl-audio-selinfo" id="adSelInfo">Выделите участок на волне</span>
        <button class="gl-btn gl-btn-ghost" type="button" id="adSelAll">Выделить всё</button>
        <button class="gl-btn gl-btn-ghost" type="button" id="adSelNone">Снять</button>
      </div>
      <div class="gl-audio-wave" id="adWave">
        <canvas id="adCanvas"></canvas>
      </div>
    </div>

    <aside class="gl-editor-side">
      <div class="gl-scroll">
        <section>
          <h4>Выделенный участок</h4>
          <label class="gl-field-label">Громкость <span class="gl-field-num" id="adGainVal">0 dB</span></label>
          <input type="range" id="adGain" min="-40" max="40" step="0.5" value="0" style="width:100%">
          <div class="gl-editor-actions">
            <button class="gl-btn gl-btn-ghost" type="button" id="adGainApply">Применить громкость</button>
            <button class="gl-btn gl-btn-ghost" type="button" id="adFadeIn">Плавно вверх</button>
            <button class="gl-btn gl-btn-ghost" type="button" id="adFadeOut">Плавно вниз</button>
            <button class="gl-btn gl-btn-ghost" type="button" id="adSilence">В тишину</button>
            <button class="gl-btn gl-btn-ghost" type="button" id="adReverse">Отразить</button>
            <button class="gl-btn gl-btn-ghost gl-btn-danger" type="button" id="adDelete">Удалить участок</button>
          </div>
          <label class="gl-field-label">Переход (кроссфейд), мс <span class="gl-field-num" id="adXfVal">30</span></label>
          <input type="range" id="adXf" min="5" max="500" step="5" value="30" style="width:100%">
          <div class="gl-editor-actions">
            <button class="gl-btn gl-btn-ghost" type="button" id="adXfEdges">Сгладить края выделения</button>
            <button class="gl-btn gl-btn-ghost gl-btn-danger" type="button" id="adDeleteXf">Удалить с переходом</button>
          </div>
          <p class="gl-hint">Тяните края выделения прямо на волне, чтобы точно задать границы.</p>
        </section>

        <section>
          <h4>Весь трек</h4>
          <div class="gl-editor-actions">
            <button class="gl-btn gl-btn-ghost" type="button" id="adTrim">Оставить выделенное</button>
            <button class="gl-btn gl-btn-ghost" type="button" id="adNormalize">Выровнять громкость</button>
          </div>
          <label class="gl-field-label">Порог лимитера <span class="gl-field-num" id="adLimVal">-1 dB</span></label>
          <input type="range" id="adLim" min="-24" max="0" step="0.5" value="-1" style="width:100%">
          <div class="gl-editor-actions">
            <button class="gl-btn gl-btn-ghost" type="button" id="adLimiter">Применить лимитер</button>
          </div>
          <p class="gl-hint">Лимитер мягко приглушает пики выше порога — участок или весь трек без щелчков.</p>
        </section>

        <section>
          <h4>Эквалайзер</h4>
          <label class="gl-field-label">Низкие <span class="gl-field-num" id="adLowVal">0 dB</span></label>
          <input type="range" id="adLow" min="-18" max="18" step="0.5" value="0" style="width:100%">
          <label class="gl-field-label">Средние <span class="gl-field-num" id="adMidVal">0 dB</span></label>
          <input type="range" id="adMid" min="-18" max="18" step="0.5" value="0" style="width:100%">
          <label class="gl-field-label">Верхние <span class="gl-field-num" id="adHighVal">0 dB</span></label>
          <input type="range" id="adHigh" min="-18" max="18" step="0.5" value="0" style="width:100%">
          <div class="gl-editor-actions">
            <button class="gl-btn gl-btn-ghost" type="button" id="adEqWhole">Применить ко всему</button>
            <button class="gl-btn gl-btn-ghost" type="button" id="adEqZero">Сбросить ручки</button>
          </div>
        </section>

        <section>
          <h4>ИИ: перерисовать участок</h4>
          <label class="gl-field-label">Теги и описание</label>
          <textarea id="adRpPrompt" rows="3" placeholder="pop, warm piano, female vocal, 110 bpm"></textarea>
          <label class="gl-field-label">Текст (пусто — сохранить прежний вокал)</label>
          <textarea id="adRpLyrics" rows="2" placeholder="[verse] ..."></textarea>
          <label class="gl-field-label">Сила изменений <span class="gl-field-num" id="adRpVarVal">0.50</span></label>
          <input type="range" id="adRpVar" min="0.01" max="1" step="0.01" value="0.5" style="width:100%">
          <p class="gl-hint">Перегенерируется только выделенный участок. Нужен пак
            ACE-Step и его веса в models/TTS (модели не скачиваются сами).</p>
          <div class="gl-editor-actions">
            <button class="gl-btn gl-btn-primary" type="button" id="adRpRun"><i class="fas fa-wand-magic-sparkles"></i> Перерисовать выделение</button>
          </div>
        </section>

        <section>
          <h4>Сохранить</h4>
          <label class="gl-field-label">Имя файла</label>
          <input type="text" id="adFileName" placeholder="например: трек-правка.wav">
          <p class="gl-hint">Результат попадёт в галерею как новый файл (WAV).
            Не забудьте: правки этого окна не трогают исходник.</p>
          <div class="gl-editor-actions">
            <button class="gl-btn gl-btn-primary" type="button" id="adSave"><i class="fas fa-floppy-disk"></i> Сохранить в галерею</button>
            <button class="gl-btn gl-btn-ghost gl-btn-danger" type="button" id="adReset">Сбросить правки</button>
          </div>
        </section>
      </div>
    </aside>
  </div>
</div>`;

function build() {
  if (dom) return dom;
  const root = document.createElement('div');
  root.innerHTML = TEMPLATE;
  const node = root.firstElementChild;
  document.body.appendChild(node);

  const q = (id) => node.querySelector('#' + id);
  dom = {
    root: node,
    name: q('adName'),
    status: q('adStatus'),
    undo: q('adUndo'),
    redo: q('adRedo'),
    wave: q('adWave'),
    canvas: q('adCanvas'),
    time: q('adTime'),
    selInfo: q('adSelInfo'),
    gain: q('adGain'), gainVal: q('adGainVal'),
    lim: q('adLim'), limVal: q('adLimVal'),
    xf: q('adXf'), xfVal: q('adXfVal'),
    low: q('adLow'), lowVal: q('adLowVal'),
    mid: q('adMid'), midVal: q('adMidVal'),
    high: q('adHigh'), highVal: q('adHighVal'),
    fileName: q('adFileName'),
    rpPrompt: q('adRpPrompt'),
    rpLyrics: q('adRpLyrics'),
    rpVar: q('adRpVar'), rpVarVal: q('adRpVarVal'),
  };
  return dom;
}

function status(text, kind = '') {
  if (!dom) return;
  dom.status.textContent = text || '';
  dom.status.className = 'gl-edit-status' + (kind ? ' is-' + kind : '');
}

/* ------------------------------------------------------------- утилиты */

function fmt(t) {
  if (!isFinite(t) || t < 0) t = 0;
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  const d = Math.floor((t * 10) % 10);
  return m + ':' + String(s).padStart(2, '0') + '.' + d;
}

function makeBuffer(channels, sampleRate) {
  const buf = actx().createBuffer(channels.length, channels[0].length, sampleRate);
  for (let i = 0; i < channels.length; i++) buf.copyToChannel(channels[i], i);
  return buf;
}

function cloneChannels(buffer) {
  const out = [];
  for (let i = 0; i < buffer.numberOfChannels; i++) {
    out.push(new Float32Array(buffer.getChannelData(i)));
  }
  return out;
}

/* Запомнить состояние для отмены. Копируем каналы целиком — клипа хватает. */
function pushUndo() {
  const s = session;
  s.redo.length = 0;
  s.undo.push(cloneChannels(s.buffer));
  while (s.undo.length > HISTORY_LIMIT) s.undo.shift();
  paintHistory();
}

function paintHistory() {
  if (!dom || !session) return;
  dom.undo.disabled = !session.undo.length;
  dom.redo.disabled = !session.redo.length;
}

function setChannels(channels) {
  session.buffer = makeBuffer(channels, session.sampleRate);
  if (session.sel && session.sel.end > session.buffer.duration) {
    session.sel.end = session.buffer.duration;
  }
  session.cursor = Math.min(session.cursor, session.buffer.duration);
  session.peaks = null;
  draw();
}

function undo() {
  const s = session;
  if (!s || !s.undo.length) return;
  s.redo.push(cloneChannels(s.buffer));
  setChannels(s.undo.pop());
  paintHistory();
}

function redo() {
  const s = session;
  if (!s || !s.redo.length) return;
  s.undo.push(cloneChannels(s.buffer));
  setChannels(s.redo.pop());
  paintHistory();
}

/* Выделение в секундах; если пусто — берём весь трек. */
function rangeOrAll() {
  const s = session;
  if (s.sel && s.sel.end - s.sel.start > 0.005) {
    return { a: s.sel.start, b: s.sel.end };
  }
  return { a: 0, b: s.buffer.duration };
}

/* ------------------------------------------------------------- DSP */

function eachChannel(fn) {
  // fn обязана вернуть канал (тот же или новый) — операции, меняющие длину
  // (обрезка, удаление), возвращают slice, и его нужно сохранить.
  const s = session;
  const chans = [];
  for (let c = 0; c < s.buffer.numberOfChannels; c++) {
    chans.push(new Float32Array(s.buffer.getChannelData(c)));
  }
  return chans.map(fn);
}

function secToIndex(t) {
  return Math.max(0, Math.min(session.buffer.length, Math.round(t * session.sampleRate)));
}

function opGain(a, b, db) {
  if (Math.abs(db) < 0.01) return;
  const g = Math.pow(10, db / 20);
  const i0 = secToIndex(a), i1 = secToIndex(b);
  pushUndo();
  setChannels(eachChannel((data) => {
    for (let i = i0; i < i1; i++) data[i] *= g;
    return data;
  }));
}

function opFade(a, b, dirIn) {
  const i0 = secToIndex(a), i1 = secToIndex(b);
  const n = i1 - i0;
  if (n < 2) return;
  pushUndo();
  setChannels(eachChannel((data) => {
    for (let i = 0; i < n; i++) {
      const x = i / (n - 1);
      data[i0 + i] *= dirIn ? x : (1 - x);
    }
    return data;
  }));
}

function opSilence(a, b) {
  const i0 = secToIndex(a), i1 = secToIndex(b);
  pushUndo();
  setChannels(eachChannel((data) => {
    for (let i = i0; i < i1; i++) data[i] = 0;
    return data;
  }));
}

function opReverse(a, b) {
  const i0 = secToIndex(a), i1 = secToIndex(b);
  pushUndo();
  setChannels(eachChannel((data) => {
    for (let i = i0, j = i1 - 1; i < j; i++, j--) {
      const t = data[i]; data[i] = data[j]; data[j] = t;
    }
    return data;
  }));
}

function opDelete(a, b) {
  const i0 = secToIndex(a), i1 = secToIndex(b);
  if (i1 - i0 < 1) return;
  pushUndo();
  const keep = session.buffer.length - (i1 - i0);
  if (keep < session.sampleRate * 0.05) {
    status('Нельзя удалить почти весь трек', 'bad');
    return;
  }
  const chans = eachChannel((data) => {
    data.copyWithin(i0, i1);
    return data.slice(0, keep);
  });
  session.sel = null;
  session.cursor = 0;
  setChannels(chans);
}

function opTrim(a, b) {
  const i0 = secToIndex(a), i1 = secToIndex(b);
  if (i1 - i0 < 1) return;
  pushUndo();
  const chans = eachChannel((data) => data.slice(i0, i1));
  session.sel = null;
  session.cursor = 0;
  setChannels(chans);
}

function opNormalize() {
  let peak = 0;
  for (let c = 0; c < session.buffer.numberOfChannels; c++) {
    const d = session.buffer.getChannelData(c);
    for (let i = 0; i < d.length; i++) {
      const v = Math.abs(d[i]);
      if (v > peak) peak = v;
    }
  }
  if (peak < 1e-6) return;
  const g = 0.99 / peak;
  pushUndo();
  setChannels(eachChannel((data) => {
    for (let i = 0; i < data.length; i++) data[i] *= g;
    return data;
  }));
}

function msToSamples(ms) {
  return Math.max(1, Math.round((ms / 1000) * session.sampleRate));
}

/* Кроссфейд краёв выделения: плавный вход и выход фиксированной длительности. */
function opCrossfadeEdges(a, b, ms) {
  const i0 = secToIndex(a), i1 = secToIndex(b);
  const n = i1 - i0;
  const x = Math.min(msToSamples(ms), Math.floor(n / 2));
  if (n < 2 || x < 1) return;
  pushUndo();
  setChannels(eachChannel((data) => {
    for (let i = 0; i < x; i++) {
      const g = i / (x - 1 || 1);
      data[i0 + i] *= g;
      data[i1 - 1 - i] *= g;
    }
    return data;
  }));
}

/* Удаление выделения со сглаживанием стыка: без щелчка на склейке. */
function opDeleteCrossfade(a, b, ms) {
  const i0 = secToIndex(a), i1 = secToIndex(b);
  if (i1 - i0 < 1) return;
  const keep = session.buffer.length - (i1 - i0);
  if (keep < session.sampleRate * 0.05) {
    status('Нельзя удалить почти весь трек', 'bad');
    return;
  }
  pushUndo();
  const x = msToSamples(ms);
  const chans = eachChannel((data) => {
    data.copyWithin(i0, i1);
    const out = data.slice(0, keep);
    const lo = Math.max(0, i0 - x);
    const hi = Math.min(keep, i0 + x);
    for (let i = lo; i < i0; i++) out[i] *= (i0 - i) / (i0 - lo || 1);
    for (let i = i0; i < hi; i++) out[i] *= (i - i0) / (hi - i0 || 1);
    return out;
  });
  session.sel = null;
  session.cursor = 0;
  setChannels(chans);
}

/* Мягкий лимитер: пики выше порога плавно «поджимаются» к потолку. */
function opLimiter(thresholdDb, a, b) {
  const T = Math.pow(10, thresholdDb / 20);
  if (T >= 0.999) return;
  const span = 1 - T;
  const i0 = secToIndex(a != null ? a : 0);
  const i1 = secToIndex(b != null ? b : session.buffer.duration);
  pushUndo();
  setChannels(eachChannel((data) => {
    for (let i = i0; i < i1; i++) {
      const v = data[i];
      const av = Math.abs(v);
      if (av > T) {
        const comp = T + span * (1 - Math.exp(-(av - T) / span));
        data[i] = v < 0 ? -comp : comp;
      }
    }
    return data;
  }));
}

async function opEq(lowDb, midDb, highDb) {
  const s = session;
  const offline = new OfflineAudioContext(
    s.buffer.numberOfChannels, s.buffer.length, s.sampleRate);
  const src = offline.createBufferSource();
  src.buffer = s.buffer;

  let node = src;
  const bands = [
    { type: 'lowshelf', freq: 250, gain: lowDb },
    { type: 'peaking', freq: 1000, gain: midDb, q: 1 },
    { type: 'highshelf', freq: 4000, gain: highDb },
  ];
  bands.forEach((band) => {
    if (Math.abs(band.gain) < 0.01) return;
    const f = offline.createBiquadFilter();
    f.type = band.type;
    f.frequency.value = band.freq;
    f.gain.value = band.gain;
    if (band.q) f.Q.value = band.q;
    node.connect(f);
    node = f;
  });
  node.connect(offline.destination);
  src.start();
  const rendered = await offline.startRendering();
  pushUndo();
  session.buffer = rendered;
  session.peaks = null;
  draw();
}

/* ------------------------------------------------------------- рисование */

function peaksFor(width) {
  const s = session;
  if (s.peaks && s.peaks.width === width) return s.peaks;
  const data = s.buffer.getChannelData(0);
  const n = data.length;
  const cols = Math.max(1, Math.floor(width));
  const step = n / cols;
  const min = new Float32Array(cols);
  const max = new Float32Array(cols);
  for (let c = 0; c < cols; c++) {
    const i0 = Math.floor(c * step);
    const i1 = Math.min(n, Math.floor((c + 1) * step));
    let lo = 0, hi = 0;
    for (let i = i0; i < i1; i++) {
      const v = data[i];
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    min[c] = lo; max[c] = hi;
  }
  s.peaks = { width: cols, min, max };
  return s.peaks;
}

function timeToX(t, width) {
  return (t / session.buffer.duration) * width;
}

function xToTime(x, width) {
  return Math.max(0, Math.min(session.buffer.duration,
                             (x / width) * session.buffer.duration));
}

function draw() {
  if (!dom || !session) return;
  const canvas = dom.canvas;
  const W = dom.wave.clientWidth || 800;
  const H = dom.wave.clientHeight || 220;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  canvas.style.width = W + 'px';
  canvas.style.height = H + 'px';
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);

  const mid = H / 2;
  const amp = H * 0.46;

  const css = getComputedStyle(document.body);
  const line = css.getPropertyValue('--gl-muted') || '#8a93a2';
  const accent = css.getPropertyValue('--gl-accent') || '#3b6ef5';
  const bg2 = css.getPropertyValue('--gl-surface-2') || '#eef1f5';

  g.fillStyle = bg2;
  g.fillRect(0, 0, W, H);

  const pk = peaksFor(W);
  g.strokeStyle = line;
  g.globalAlpha = 0.85;
  g.beginPath();
  for (let c = 0; c < pk.width; c++) {
    const x = c + 0.5;
    g.moveTo(x, mid - pk.max[c] * amp);
    g.lineTo(x, mid - pk.min[c] * amp);
  }
  g.stroke();
  g.globalAlpha = 1;

  g.strokeStyle = 'rgba(0,0,0,.18)';
  g.beginPath(); g.moveTo(0, mid); g.lineTo(W, mid); g.stroke();

  if (session.sel) {
    const x0 = timeToX(session.sel.start, W);
    const x1 = timeToX(session.sel.end, W);
    g.fillStyle = 'rgba(59,110,245,.18)';
    g.fillRect(x0, 0, x1 - x0, H);
    g.strokeStyle = accent;
    g.lineWidth = 1.5;
    g.beginPath();
    g.moveTo(x0, 0); g.lineTo(x0, H);
    g.moveTo(x1, 0); g.lineTo(x1, H);
    g.stroke();
    // Ручки для перетаскивания границ.
    const hw = 4, hh = 16;
    g.fillStyle = accent;
    g.fillRect(x0 - hw, 0, hw * 2, hh);
    g.fillRect(x1 - hw, 0, hw * 2, hh);
    g.fillRect(x0 - hw, H - hh, hw * 2, hh);
    g.fillRect(x1 - hw, H - hh, hw * 2, hh);
  }

  const cx = timeToX(session.cursor, W);
  g.strokeStyle = '#e5484d';
  g.lineWidth = 1.5;
  g.beginPath(); g.moveTo(cx, 0); g.lineTo(cx, H); g.stroke();
}

/* ------------------------------------------------------------- плеер */

function stop(keepCursor) {
  const s = session;
  if (!s || !s.playing) return;
  const pos = currentPos();
  if (keepCursor) s.cursor = Math.min(pos, s.buffer.duration);
  const p = s.playing;
  s.playing = null;
  try { p.src.onended = null; p.src.stop(); } catch (e) { /* уже остановлен */ }
  draw();
  updateTime();
}

function currentPos() {
  const s = session;
  if (!s || !s.playing) return s ? s.cursor : 0;
  const t = s.playing.offset + (actx().currentTime - s.playing.startCtx);
  return Math.max(0, Math.min(t, s.buffer.duration));
}

function play(from, to) {
  const s = session;
  if (!s) return;
  stop(true);
  actx().resume();
  const off = Math.max(0, Math.min(from != null ? from : s.cursor, s.buffer.duration));
  const dur = (to != null && to > off) ? (to - off) : undefined;
  const src = actx().createBufferSource();
  src.buffer = s.buffer;
  src.connect(actx().destination);
  src.start(0, off, dur);
  s.playing = { src, startCtx: actx().currentTime, offset: off, until: dur };
  src.onended = () => {
    if (s.playing && s.playing.src === src) {
      s.playing = null;
      s.cursor = (to != null && dur != null) ? to : s.buffer.duration;
      if (s.cursor >= s.buffer.duration) s.cursor = 0;
      draw(); updateTime();
    }
  };
  rafLoop();
}

let rafId = null;
function rafLoop() {
  if (rafId) return;
  const tick = () => {
    if (!session || !session.playing) { rafId = null; return; }
    if (session.cursor !== null && session.sel) { /* выбор не мешает */ }
    draw();
    updateTime();
    rafId = requestAnimationFrame(tick);
  };
  rafId = requestAnimationFrame(tick);
}

function updateTime() {
  if (!dom || !session) return;
  const pos = session.playing ? currentPos() : session.cursor;
  dom.time.textContent = fmt(pos) + ' / ' + fmt(session.buffer.duration);
}

function updateSelInfo() {
  if (!dom || !session) return;
  if (session.sel) {
    const d = session.sel.end - session.sel.start;
    dom.selInfo.textContent = 'Выделено ' + fmt(session.sel.start) + '–' +
      fmt(session.sel.end) + ' (' + d.toFixed(2) + ' с)';
  } else {
    dom.selInfo.textContent = 'Выделите участок на волне';
  }
}

/* ------------------------------------------------------- взаимодействие */

function wavePoint(ev) {
  const r = dom.canvas.getBoundingClientRect();
  const x = ev.clientX - r.left;
  return { x, t: xToTime(x, r.width) };
}

const HANDLE_PX = 7;

/* Какая граница выделения под курсором (для перетаскивания). */
function edgeAt(ev) {
  if (!session || !session.sel) return null;
  const r = dom.canvas.getBoundingClientRect();
  const x = ev.clientX - r.left;
  const x0 = timeToX(session.sel.start, r.width);
  const x1 = timeToX(session.sel.end, r.width);
  if (Math.abs(x - x0) <= HANDLE_PX) return 'start';
  if (Math.abs(x - x1) <= HANDLE_PX) return 'end';
  return null;
}

function onWaveDown(ev) {
  if (!session) return;
  dom.canvas.setPointerCapture(ev.pointerId);
  const p = wavePoint(ev);
  const edge = edgeAt(ev);
  if (edge) {
    session.drag = { edge, moved: true };
    return;
  }
  session.drag = { anchor: p.t, moved: false };
  session.sel = null;
  session.cursor = p.t;
  draw(); updateSelInfo(); updateTime();
}

function onWaveMove(ev) {
  if (!session) return;
  if (!session.drag) {
    dom.canvas.style.cursor = edgeAt(ev) ? 'col-resize' : 'crosshair';
    return;
  }
  const p = wavePoint(ev);
  const minGap = 1 / session.sampleRate;
  if (session.drag.edge) {
    if (session.drag.edge === 'start') {
      session.sel.start = Math.max(0, Math.min(p.t, session.sel.end - minGap));
    } else {
      session.sel.end = Math.min(session.buffer.duration,
                               Math.max(p.t, session.sel.start + minGap));
    }
    session.cursor = p.t;
    draw(); updateSelInfo(); updateTime();
    return;
  }
  const a = session.drag.anchor;
  if (Math.abs(p.t - a) > 0.01) session.drag.moved = true;
  session.sel = { start: Math.min(a, p.t), end: Math.max(a, p.t) };
  session.cursor = p.t;
  draw(); updateSelInfo(); updateTime();
}

function onWaveUp(ev) {
  if (!session || !session.drag) return;
  const moved = session.drag.moved;
  const edge = session.drag.edge;
  session.drag = null;
  if (!moved && !edge) session.sel = null;
  try { dom.canvas.releasePointerCapture(ev.pointerId); } catch (e) { /* ок */ }
  draw(); updateSelInfo();
}

/* ------------------------------------------------------------- экспорт */

function encodeWav(buffer) {
  const numCh = buffer.numberOfChannels;
  const len = buffer.length;
  const sampleRate = buffer.sampleRate;
  const bytes = 44 + len * numCh * 2;
  const view = new DataView(new ArrayBuffer(bytes));
  const w = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
  w(0, 'RIFF'); view.setUint32(4, bytes - 8, true); w(8, 'WAVE');
  w(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, numCh, true); view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * numCh * 2, true);
  view.setUint16(32, numCh * 2, true); view.setUint16(34, 16, true);
  w(36, 'data'); view.setUint32(40, len * numCh * 2, true);

  const chans = [];
  for (let c = 0; c < numCh; c++) chans.push(buffer.getChannelData(c));
  let off = 44;
  for (let i = 0; i < len; i++) {
    for (let c = 0; c < numCh; c++) {
      let v = Math.max(-1, Math.min(1, chans[c][i]));
      view.setInt16(off, v < 0 ? v * 0x8000 : v * 0x7fff, true);
      off += 2;
    }
  }
  return new Blob([view.buffer], { type: 'audio/wav' });
}

/* ------------------------------------------------------------- ИИ-перерисовка */

async function runRepaint() {
  if (!session) return;
  const file = bridge();
  if (!session.sel) { status('Сначала выделите участок на волне', 'warn'); return; }
  const prompt = (dom.rpPrompt.value || '').trim();
  if (!prompt) { status('Опишите, что должно звучать в этом участке', 'warn'); return; }

  const btn = dom.root.querySelector('#adRpRun');
  btn.disabled = true;
  const start = Math.max(0, Math.floor(session.sel.start));
  const end = Math.ceil(session.sel.end);
  try {
    status('Готовим трек для движка…');
    const staged = await file.api('/assets/' + encodeURIComponent(session.asset.id) + '/stage',
                                  { method: 'POST', body: {} });
    status('Ставим задание на перерисовку…');
    const res = await file.api('/generate', {
      method: 'POST',
      body: {
        preset: 'audio.repaint',
        title: 'Перерисовка участка',
        params: {
          audio: staged.comfy_name,
          start, end,
          prompt,
          lyrics: (dom.rpLyrics.value || ' ').trim() || ' ',
          variance: Number(dom.rpVar.value),
          steps: 60, guidance: 15.0,
          format: 'mp3',
        },
      },
    });
    if (file.upsertJob) file.upsertJob(res.job);
    file.toast('Задание отправлено — результат появится в галерее', 'success');
    close();
  } catch (e) {
    status('Ошибка: ' + e.message, 'bad');
    file.toast(e.message, 'error', 8000);
  } finally {
    btn.disabled = false;
  }
}

async function saveToGallery() {
  if (!session) return;
  const file = bridge();
  let name = (dom.fileName.value || '').trim();
  if (!name) name = session.asset.name.replace(/\.[^.]+$/, '') + '-правка';
  if (!/\.wav$/i.test(name)) name += '.wav';

  const btn = dom.root.querySelector('#adSave');
  btn.disabled = true;
  status('Сохраняем…');
  try {
    const blob = encodeWav(session.buffer);
    const form = new FormData();
    form.append('file', blob, name);
    form.append('kind', 'input');
    const res = await file.api('/upload', { method: 'POST', body: form });
    await file.loadGallery(false);
    if (res && res.asset) {
      file.state.activeAsset = res.asset;
      if (typeof file.openAsset === 'function') file.openAsset(res.asset.id);
      else if (typeof file.renderActiveAsset === 'function') file.renderActiveAsset();
    }
    file.toast('Сохранено: ' + name, 'success');
    close();
  } catch (e) {
    status('Ошибка: ' + e.message, 'bad');
    file.toast(e.message, 'error', 8000);
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------- жизненный цикл */

async function open(assetId) {
  const file = bridge();
  const asset = (file.state && file.state.assets || []).find((a) => a.id === assetId)
    || (file.state && file.state.activeAsset);
  if (!asset || asset.media_type !== 'audio') {
    if (file.toast) file.toast('Это не звуковой файл', 'warn');
    return;
  }

  build();
  dom.root.hidden = false;
  dom.name.textContent = asset.name;
  dom.fileName.value = asset.name.replace(/\.[^.]+$/, '') + '-правка.wav';
  status('Загружаем звук…');

  let buffer;
  try {
    const resp = await fetch(asset.url, { cache: 'no-store' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const arr = await resp.arrayBuffer();
    buffer = await actx().decodeAudioData(arr);
  } catch (e) {
    status('Не удалось прочитать звук: ' + e.message, 'bad');
    return;
  }

  session = {
    asset, buffer, sampleRate: buffer.sampleRate,
    sel: null, cursor: 0, drag: null, playing: null,
    peaks: null, undo: [], redo: [],
  };
  requestAnimationFrame(() => { updateSelInfo(); updateTime(); draw(); });
  paintHistory();
  status('');
}

function close() {
  if (session) stop(false);
  if (dom) dom.root.hidden = true;
  session = null;
}

/* ------------------------------------------------------------- привязки */

function bind() {
  const d = build();
  d.root.querySelector('#adClose').addEventListener('click', close);
  d.undo.addEventListener('click', undo);
  d.redo.addEventListener('click', redo);

  d.canvas.addEventListener('pointerdown', onWaveDown);
  d.canvas.addEventListener('pointermove', onWaveMove);
  d.canvas.addEventListener('pointerup', onWaveUp);
  d.canvas.addEventListener('pointercancel', onWaveUp);
  window.addEventListener('resize', () => { if (session) { session.peaks = null; draw(); } });

  d.root.querySelector('#adPlay').addEventListener('click', () => {
    const r = rangeOrAll();
    if (session.sel) play(session.sel.start, session.sel.end);
    else play(session.cursor);
  });
  d.root.querySelector('#adPlaySel').addEventListener('click', () => {
    if (!session.sel) { status('Сначала выделите участок', 'warn'); return; }
    play(session.sel.start, session.sel.end);
  });
  d.root.querySelector('#adStop').addEventListener('click', () => stop(true));
  d.root.querySelector('#adSelAll').addEventListener('click', () => {
    session.sel = { start: 0, end: session.buffer.duration };
    draw(); updateSelInfo();
  });
  d.root.querySelector('#adSelNone').addEventListener('click', () => {
    session.sel = null; draw(); updateSelInfo();
  });

  d.gain.addEventListener('input', () => { d.gainVal.textContent = d.gain.value + ' dB'; });
  d.low.addEventListener('input', () => { d.lowVal.textContent = d.low.value + ' dB'; });
  d.mid.addEventListener('input', () => { d.midVal.textContent = d.mid.value + ' dB'; });
  d.high.addEventListener('input', () => { d.highVal.textContent = d.high.value + ' dB'; });
  d.xf.addEventListener('input', () => { d.xfVal.textContent = d.xf.value; });
  d.lim.addEventListener('input', () => {
    d.limVal.textContent = Number(d.lim.value).toFixed(1) + ' dB';
  });

  d.root.querySelector('#adGainApply').addEventListener('click', () => {
    const r = rangeOrAll();
    opGain(r.a, r.b, Number(d.gain.value));
  });
  d.root.querySelector('#adFadeIn').addEventListener('click', () => {
    const r = rangeOrAll(); opFade(r.a, r.b, true);
  });
  d.root.querySelector('#adFadeOut').addEventListener('click', () => {
    const r = rangeOrAll(); opFade(r.a, r.b, false);
  });
  d.root.querySelector('#adSilence').addEventListener('click', () => {
    const r = rangeOrAll(); opSilence(r.a, r.b);
  });
  d.root.querySelector('#adReverse').addEventListener('click', () => {
    const r = rangeOrAll(); opReverse(r.a, r.b);
  });
  d.root.querySelector('#adDelete').addEventListener('click', () => {
    if (!session.sel) { status('Выделите участок для удаления', 'warn'); return; }
    opDelete(session.sel.start, session.sel.end);
    updateSelInfo();
  });
  d.root.querySelector('#adTrim').addEventListener('click', () => {
    if (!session.sel) { status('Выделите участок, который оставить', 'warn'); return; }
    opTrim(session.sel.start, session.sel.end);
    updateSelInfo();
  });
  d.root.querySelector('#adNormalize').addEventListener('click', opNormalize);
  d.root.querySelector('#adXfEdges').addEventListener('click', () => {
    const r = rangeOrAll();
    opCrossfadeEdges(r.a, r.b, Number(d.xf.value));
  });
  d.root.querySelector('#adDeleteXf').addEventListener('click', () => {
    if (!session.sel) { status('Выделите участок для удаления', 'warn'); return; }
    opDeleteCrossfade(session.sel.start, session.sel.end, Number(d.xf.value));
    updateSelInfo();
  });
  d.root.querySelector('#adLimiter').addEventListener('click', () => {
    const r = rangeOrAll();
    opLimiter(Number(d.lim.value), r.a, r.b);
  });
  d.root.querySelector('#adEqWhole').addEventListener('click', async () => {
    status('Считаем эквалайзер…');
    try {
      await opEq(Number(d.low.value), Number(d.mid.value), Number(d.high.value));
      status('');
    } catch (e) { status('Ошибка эквалайзера: ' + e.message, 'bad'); }
  });
  d.root.querySelector('#adEqZero').addEventListener('click', () => {
    ['low', 'mid', 'high'].forEach((k) => {
      d[k].value = 0; d[k + 'Val'].textContent = '0 dB';
    });
  });
  d.root.querySelector('#adReset').addEventListener('click', () => {
    if (!session.undo.length) return;
    session.undo.push(cloneChannels(session.buffer));
    setChannels(session.undo[0]);
    session.undo.length = 0;
    session.sel = null;
    paintHistory(); updateSelInfo();
  });
  d.root.querySelector('#adSave').addEventListener('click', saveToGallery);

  d.rpVar.addEventListener('input', () => { d.rpVarVal.textContent = Number(d.rpVar.value).toFixed(2); });
  d.root.querySelector('#adRpRun').addEventListener('click', runRepaint);

  document.addEventListener('keydown', (ev) => {
    if (!session || dom.root.hidden) return;
    if (ev.key === 'Escape') { close(); return; }
    if (ev.target && /INPUT|TEXTAREA/.test(ev.target.tagName)) return;
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === 'z' || ev.key === 'Z')) {
      ev.preventDefault(); if (ev.shiftKey) redo(); else undo(); return;
    }
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === 'y' || ev.key === 'Y')) {
      ev.preventDefault(); redo(); return;
    }
    if (ev.code === 'Space') {
      ev.preventDefault();
      if (session.playing) stop(true); else play(session.cursor);
    }
  });
}

window.GenLabAudio = {
  open, close, ready: false,
  /* Небольшой испытательный крючок для авто-проверок: даёт доступ к буферу
     и операциям без кликов по интерфейсу. */
  _debug: {
    session: () => session,
    bufferLength: () => (session ? session.buffer.length : 0),
    opGain, opFade, opSilence, opReverse,
    opDelete, opTrim, opNormalize, opEq,
    opCrossfadeEdges, opDeleteCrossfade, opLimiter,
    setSelection: (a, b) => { if (session) { session.sel = { start: a, end: b }; draw(); } },
  },
};

document.addEventListener('DOMContentLoaded', () => {
  if (window.GenLab) {
    bind();
    window.GenLabAudio.ready = true;
  } else {
    setTimeout(() => { bind(); window.GenLabAudio.ready = true; }, 0);
  }
});
