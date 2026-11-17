/* MIDI-клавиатура GenLab.
 *
 * Играбельная клавиатура с превью-синтезом (Web Audio), записью исполнения,
 * воспроизведением и экспортом: MIDI-файл (.mid) скачиванием и WAV в галерею.
 * Полностью автономна, модели не нужны.
 */

const bridge = () => window.GenLab || {};

let dom = null;
let ac = null;
let master = null;
let session = null;   // { octave, instrument, volume, recording, playing }

const VOICES = {
  piano: { wave: 'triangle', attack: 0.005, decay: 0.28, sustain: 0.22, release: 0.35, gain: 0.5, detune: 1 },
  organ: { wave: 'sine', attack: 0.02, decay: 0.05, sustain: 0.9, release: 0.12, gain: 0.42, detune: 0 },
  strings: { wave: 'sawtooth', attack: 0.14, decay: 0.1, sustain: 0.8, release: 0.42, gain: 0.26, detune: 3 },
  square: { wave: 'square', attack: 0.008, decay: 0.1, sustain: 0.55, release: 0.16, gain: 0.26, detune: 0 },
  pluck: { wave: 'sawtooth', attack: 0.003, decay: 0.18, sustain: 0.05, release: 0.16, gain: 0.42, detune: 0 },
};

const NOTES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

/* Раскладка компьютерной клавиатуры: относительные полутоны от базовой ноты. */
const KEYMAP = {
  z: 0, s: 1, x: 2, d: 3, c: 4, v: 5, g: 6, b: 7, h: 8, n: 9, j: 10, m: 11, ',': 12,
  q: 12, '2': 13, w: 14, '3': 15, e: 16, r: 17, '5': 18, t: 19, '6': 20, y: 21, '7': 22, u: 23,
  i: 24, '9': 25, o: 26, '0': 27, p: 28,
};

function actx() {
  if (!ac) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    ac = new Ctx();
    master = ac.createGain();
    master.gain.value = 0.6;
    master.connect(ac.destination);
  }
  return ac;
}

function midiFreq(m) { return 440 * Math.pow(2, (m - 69) / 12); }

function midiName(m) { return NOTES[m % 12] + (Math.floor(m / 12) - 1); }

/* ------------------------------------------------------------- разметка */

const TEMPLATE = `
<div class="gl-midi" id="glMidi" hidden>
  <div class="gl-editor-top">
    <span class="gl-editor-title"><i class="fas fa-music"></i> MIDI-клавиатура</span>
    <span class="gl-spacer"></span>
    <label class="gl-midi-inline">Тембр
      <select id="mdInst">
        <option value="piano">Пиано</option>
        <option value="organ">Орган</option>
        <option value="strings">Струнные</option>
        <option value="square">8-bit</option>
        <option value="pluck">Щипок</option>
      </select>
    </label>
    <label class="gl-midi-inline">Громкость
      <input type="range" id="mdVol" min="0" max="1" step="0.01" value="0.6">
    </label>
    <span class="gl-edit-status" id="mdStatus"></span>
    <button class="gl-icon-btn" type="button" id="mdClose" title="Закрыть (Esc)"><i class="fas fa-xmark"></i></button>
  </div>

  <div class="gl-midi-main">
    <div class="gl-midi-toolbar">
      <button class="gl-btn gl-btn-ghost" type="button" id="mdOctDown" title="Октава ниже"><i class="fas fa-angle-down"></i> Октава</button>
      <span class="gl-midi-oct" id="mdOctLabel">C4</span>
      <button class="gl-btn gl-btn-ghost" type="button" id="mdOctUp" title="Октава выше"><i class="fas fa-angle-up"></i> Октава</button>
      <span class="gl-spacer"></span>
      <button class="gl-btn gl-btn-primary" type="button" id="mdRec"><i class="fas fa-circle"></i> Запись</button>
      <button class="gl-btn gl-btn-ghost" type="button" id="mdPlay" disabled><i class="fas fa-play"></i> Проиграть</button>
      <button class="gl-btn gl-btn-ghost" type="button" id="mdStop" disabled><i class="fas fa-stop"></i></button>
      <button class="gl-btn gl-btn-ghost" type="button" id="mdClear" disabled>Очистить</button>
      <span class="gl-midi-count" id="mdCount">0 нот</span>
      <button class="gl-btn gl-btn-ghost" type="button" id="mdMidi" disabled><i class="fas fa-download"></i> MIDI</button>
      <button class="gl-btn gl-btn-ghost" type="button" id="mdWav" disabled><i class="fas fa-floppy-disk"></i> WAV в галерею</button>
    </div>

    <div class="gl-midi-keys" id="mdKeys"></div>

    <div class="gl-midi-row">
      <label class="gl-midi-inline">Темп
        <input type="number" id="mdTempo" min="30" max="300" step="1" value="120" style="width:64px"> BPM
      </label>
      <button class="gl-btn gl-btn-ghost" type="button" id="mdMetro"><i class="fas fa-stopwatch"></i> Метроном: выкл</button>
      <label class="gl-midi-inline">Сетка
        <select id="mdGrid">
          <option value="4">1/4</option>
          <option value="8" selected>1/8</option>
          <option value="16">1/16</option>
          <option value="8T">1/8 триоль</option>
        </select>
      </label>
      <button class="gl-btn gl-btn-ghost" type="button" id="mdQuant">Квантовать</button>
      <label class="gl-btn gl-btn-ghost gl-midi-import">
        <i class="fas fa-file-import"></i> Импорт .mid
        <input type="file" id="mdImport" accept=".mid,.midi,audio/midi" hidden>
      </label>
    </div>

    <div class="gl-midi-roll"><canvas id="mdRoll"></canvas></div>

    <p class="gl-hint gl-midi-hint">
      Играйте мышью или с клавиатуры: <b>Z X C V B N M</b> — нижний ряд,
      <b>Q W E R T Y U</b> — верхний, чёрные — <b>S D G H J</b> и <b>2 3 5 6 7</b>.
      «Запись» фиксирует исполнение. Ноты видны на пиано-ролле — щёлкните ноту, чтобы удалить.
    </p>
  </div>
</div>`;

function build() {
  if (dom) return dom;
  const wrap = document.createElement('div');
  wrap.innerHTML = TEMPLATE;
  const node = wrap.firstElementChild;
  document.body.appendChild(node);
  const q = (id) => node.querySelector('#' + id);
  dom = {
    root: node,
    inst: q('mdInst'), vol: q('mdVol'),
    status: q('mdStatus'),
    octLabel: q('mdOctLabel'),
    rec: q('mdRec'), play: q('mdPlay'), stop: q('mdStop'),
    clear: q('mdClear'), count: q('mdCount'),
    midi: q('mdMidi'), wav: q('mdWav'),
    keys: q('mdKeys'),
    tempo: q('mdTempo'), metro: q('mdMetro'), grid: q('mdGrid'),
    quant: q('mdQuant'), importInput: q('mdImport'), roll: q('mdRoll'),
  };
  return dom;
}

function status(t, kind = '') {
  if (!dom) return;
  dom.status.textContent = t || '';
  dom.status.className = 'gl-edit-status' + (kind ? ' is-' + kind : '');
}

/* ------------------------------------------------------------- клавиши */

function keyLayout() {
  // Две октавы, начиная с базовой. Считаем белые клавиши и позиции чёрных.
  const base = session.baseMidi;
  const whites = [];
  const blacks = [];
  for (let oct = 0; oct < 2; oct++) {
    for (let i = 0; i < 12; i++) {
      const m = base + oct * 12 + i;
      if (NOTES[i].includes('#')) blacks.push(m);
      else whites.push(m);
    }
  }
  whites.push(base + 24); // замыкающий C
  return { whites, blacks };
}

function renderKeys() {
  const { whites, blacks } = keyLayout();
  const nw = whites.length;
  dom.keys.textContent = '';
  const whiteEls = {};
  whites.forEach((m, idx) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'gl-midi-key gl-midi-white';
    b.dataset.midi = m;
    b.title = midiName(m);
    if (session.keysDown.has(m)) b.classList.add('is-on');
    b.addEventListener('pointerdown', (ev) => { ev.preventDefault(); pressKey(m, 0.8); });
    b.addEventListener('pointerup', () => releaseKey(m));
    b.addEventListener('pointerleave', () => releaseKey(m));
    dom.keys.appendChild(b);
    whiteEls[m] = { idx, el: b };
  });
  // Чёрные клавиши позиционируем между соответствующими белыми.
  const whiteW = 100 / nw;
  blacks.forEach((m) => {
    const below = m - 1;                 // белая клавиша слева
    const w = whiteEls[below];
    if (!w) return;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'gl-midi-key gl-midi-black';
    b.dataset.midi = m;
    b.title = midiName(m);
    if (session.keysDown.has(m)) b.classList.add('is-on');
    b.style.left = ((w.idx + 1) * whiteW - whiteW * 0.3) + '%';
    b.style.width = (whiteW * 0.6) + '%';
    b.addEventListener('pointerdown', (ev) => { ev.preventDefault(); pressKey(m, 0.8); });
    b.addEventListener('pointerup', () => releaseKey(m));
    b.addEventListener('pointerleave', () => releaseKey(m));
    dom.keys.appendChild(b);
  });
  highlightKeys();
}

function highlightKeys() {
  dom.keys.querySelectorAll('.gl-midi-key').forEach((el) => {
    el.classList.toggle('is-on', session.keysDown.has(Number(el.dataset.midi)));
  });
}

/* ------------------------------------------------------------- синтез */

function scheduleVoice(ctx, dest, midi, t0, dur, velocity, inst) {
  const v = VOICES[inst] || VOICES.piano;
  const freq = midiFreq(midi);
  const peak = Math.max(0.001, v.gain * velocity);
  const mk = (detunes) => {
    const osc = ctx.createOscillator();
    osc.type = v.wave;
    osc.frequency.value = freq;
    if (detunes) osc.detune.value = detunes;
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.linearRampToValueAtTime(peak, t0 + v.attack);
    g.gain.linearRampToValueAtTime(peak * v.sustain, t0 + v.attack + v.decay);
    g.gain.setValueAtTime(peak * v.sustain, t0 + Math.max(dur, v.attack + v.decay + 0.01));
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur + v.release);
    osc.connect(g);
    g.connect(dest);
    osc.start(t0);
    osc.stop(t0 + dur + v.release + 0.03);
    return osc;
  };
  const osc = mk(0);
  if (v.detune) {
    const o2 = mk(v.detune);
    const o3 = mk(-v.detune);
    return { osc, extra: [o2, o3] };
  }
  return { osc, extra: [] };
}

function liveVoiceOn(midi, velocity) {
  const ctx = actx();
  ctx.resume();
  if (session.voices.has(midi)) liveVoiceOff(midi);
  const node = scheduleVoice(ctx, master, midi, ctx.currentTime, 3600, velocity, session.instrument);
  session.voices.set(midi, node);
}

function liveVoiceOff(midi) {
  const node = session.voices.get(midi);
  if (!node) return;
  session.voices.delete(midi);
  const ctx = actx();
  const t = ctx.currentTime;
  // Быстрое затухание без щелчка.
  [node.osc].concat(node.extra).forEach((osc) => {
    try { osc.stop(t + 0.08); } catch (e) { /* уже стоп */ }
  });
}

function pressKey(midi, velocity) {
  if (!session) return;
  if (!session.keysDown.has(midi)) {
    session.keysDown.add(midi);
    liveVoiceOn(midi, velocity);
    if (session.recording) session.pending.set(midi, nowSec());
    highlightKeys();
  }
}

function releaseKey(midi) {
  if (!session) return;
  if (session.keysDown.has(midi)) {
    session.keysDown.delete(midi);
    liveVoiceOff(midi);
    if (session.recording && session.pending.has(midi)) {
      const t0 = session.pending.get(midi);
      session.pending.delete(midi);
      session.notes.push({ midi, start: t0, end: nowSec(), velocity: 0.8 });
      updateCount();
    }
    highlightKeys();
  }
}

/* ------------------------------------------------------------- запись */

function nowSec() { return performance.now() / 1000; }

function toggleRecord() {
  if (session.recording) {
    session.recording = false;
    session.pending.forEach((t0, midi) => {
      session.notes.push({ midi, start: t0, end: nowSec(), velocity: 0.8 });
    });
    session.pending.clear();
    dom.rec.classList.remove('is-recording');
    dom.rec.innerHTML = '<i class="fas fa-circle"></i> Запись';
    status('Запись остановлена');
    updateCount();
  } else {
    if (session.recStart != null) { /* продолжаем добавлять */ }
    session.recording = true;
    session.recBase = nowSec();
    session.recOffset = session.notes.length ? session.notes[session.notes.length - 1].end : 0;
    // Продолжаем с конца: сдвигаем базу так, чтобы новые ноты шли после прежних.
    session.recBase = nowSec() - session.recOffset;
    dom.rec.classList.add('is-recording');
    dom.rec.innerHTML = '<i class="fas fa-square"></i> Стоп';
    status('Идёт запись — играйте', 'ok');
  }
  updateButtons();
}

function updateCount() {
  const n = session.notes.length;
  const m10 = n % 10, m100 = n % 100;
  let word = 'нот';
  if (m10 === 1 && m100 !== 11) word = 'нота';
  else if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) word = 'ноты';
  dom.count.textContent = n + ' ' + word;
  if (dom.roll) drawRoll();
}

function updateButtons() {
  const n = session.notes.length;
  dom.play.disabled = !n || session.playing;
  dom.clear.disabled = !n || session.recording;
  dom.midi.disabled = !n;
  dom.wav.disabled = !n;
  dom.stop.disabled = !session.playing;
}

function playback() {
  if (!session.notes.length) return;
  const ctx = actx();
  ctx.resume();
  if (session.playing) stopAll();
  session.playing = true;
  session.playNodes = [];
  const first = session.notes[0].start;
  const start = ctx.currentTime + 0.05;
  session.playBaseCtx = start;
  session.playSpan = Math.max(0.1, session.notes[session.notes.length - 1].end - first);
  session.notes.forEach((nt) => {
    const when = start + (nt.start - first);
    const dur = Math.max(0.08, nt.end - nt.start);
    const node = scheduleVoice(ctx, master, nt.midi, when, dur, nt.velocity, session.instrument);
    session.playNodes.push(node);
  });
  const total = session.playSpan;
  session.playTimer = setTimeout(() => {
    if (!session) return;
    session.playing = false;
    status('');
    updateButtons();
    drawRoll();
  }, (total + 0.5) * 1000);
  updateButtons();
  if (!session.rafId) session.rafId = requestAnimationFrame(rollTick);
}

function stopAll() {
  clearTimeout(session.playTimer);
  (session.playNodes || []).forEach((node) => {
    [node.osc].concat(node.extra).forEach((osc) => { try { osc.stop(); } catch (e) {} });
  });
  session.playNodes = [];
  session.playing = false;
  updateButtons();
}

/* ------------------------------------------------------------- метроном */

function metroClick(accent) {
  const ctx = actx();
  const t = ctx.currentTime;
  const osc = ctx.createOscillator();
  const g = ctx.createGain();
  osc.frequency.value = accent ? 1500 : 1000;
  g.gain.setValueAtTime(0.0001, t);
  g.gain.linearRampToValueAtTime(accent ? 0.5 : 0.32, t + 0.002);
  g.gain.exponentialRampToValueAtTime(0.0001, t + 0.05);
  osc.connect(g); g.connect(master);
  osc.start(t); osc.stop(t + 0.07);
}

function startMetronome() {
  if (session.metroTimer) return;
  session.metronome = true;
  session.metroBeat = 0;
  const step = () => {
    if (!session || !session.metronome) return;
    metroClick(session.metroBeat % 4 === 0);
    session.metroBeat++;
    session.metroTimer = setTimeout(step, (60 / session.tempo) * 1000);
  };
  step();
  dom.metro.classList.add('is-on');
  dom.metro.innerHTML = '<i class="fas fa-stopwatch"></i> Метроном: вкл';
}

function stopMetronome() {
  if (!session) return;
  session.metronome = false;
  clearTimeout(session.metroTimer);
  session.metroTimer = null;
  if (dom) {
    dom.metro.classList.remove('is-on');
    dom.metro.innerHTML = '<i class="fas fa-stopwatch"></i> Метроном: выкл';
  }
}

function toggleMetronome() {
  if (session.metronome) stopMetronome(); else startMetronome();
}

/* ---------------------------------------------------------- квантизация */

function gridSeconds() {
  const beat = 60 / session.tempo;      // четвертная доля
  switch (dom.grid.value) {
    case '4': return beat;
    case '16': return beat / 4;
    case '8T': return beat / 3;         // восьмая триоль
    default: return beat / 2;           // 1/8
  }
}

function quantizeNotes() {
  if (!session.notes.length) { status('Нет нот для квантизации', 'warn'); return; }
  const g = gridSeconds();
  if (g <= 0) return;
  session.notes.forEach((nt) => {
    let s = Math.round(nt.start / g) * g;
    let e = Math.round(nt.end / g) * g;
    if (e <= s) e = s + g;
    nt.start = Math.max(0, s);
    nt.end = e;
  });
  session.notes.sort((a, b) => (a.start - b.start) || (a.midi - b.midi));
  drawRoll();
  status('Квантовано по сетке ' + dom.grid.options[dom.grid.selectedIndex].text);
}

/* --------------------------------------------------------- пиано-ролл */

function rollGeom() {
  const canvas = dom.roll;
  const W = canvas.parentElement.clientWidth || 800;
  const H = canvas.parentElement.clientHeight || 150;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  canvas.style.width = W + 'px';
  canvas.style.height = H + 'px';
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { g, W, H };
}

function rollSpan() {
  const notes = session.notes;
  if (!notes.length) return { first: 0, span: 4, lo: 55, hi: 65 };
  let last = 0, lo = 127, hi = 0;
  notes.forEach((nt) => {
    if (nt.end > last) last = nt.end;
    if (nt.midi < lo) lo = nt.midi;
    if (nt.midi > hi) hi = nt.midi;
  });
  const first = notes[0].start;
  return { first, span: Math.max(0.5, last - first), lo: lo - 1, hi: hi + 1 };
}

function drawRoll() {
  if (!dom || !session || dom.root.hidden) return;
  const { g, W, H } = rollGeom();
  g.clearRect(0, 0, W, H);
  const css = getComputedStyle(document.body);
  const accent = css.getPropertyValue('--gl-accent') || '#3b6ef5';
  const muted = css.getPropertyValue('--gl-muted') || '#8a93a2';
  const surface = css.getPropertyValue('--gl-surface-2') || '#eef1f5';

  g.fillStyle = surface;
  g.fillRect(0, 0, W, H);

  const { first, span, lo, hi } = rollSpan();
  const rows = Math.max(1, hi - lo);
  const rowH = H / rows;
  const yFor = (midi) => H - (midi - lo + 0.5) * rowH;

  // чёрные клавиши-полосы
  for (let m = lo; m <= hi; m++) {
    if (NOTES[m % 12].includes('#')) {
      g.fillStyle = 'rgba(0,0,0,.12)';
      g.fillRect(0, H - (m - lo + 1) * rowH, W, rowH);
    }
  }
  // вертикали по долям
  const beat = 60 / session.tempo;
  g.strokeStyle = 'rgba(0,0,0,.12)';
  for (let t = 0; t <= span + beat; t += beat) {
    const x = (t / span) * W;
    g.globalAlpha = (Math.round(t / beat) % 4 === 0) ? 0.5 : 0.22;
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, H); g.stroke();
  }
  g.globalAlpha = 1;

  // ноты
  session.notes.forEach((nt) => {
    const x = ((nt.start - first) / span) * W;
    const w = Math.max(2, ((nt.end - nt.start) / span) * W);
    const y = yFor(nt.midi) - rowH * 0.42;
    g.fillStyle = accent;
    g.globalAlpha = 0.55 + 0.45 * Math.min(1, nt.velocity || 0.8);
    g.fillRect(x, y, w, Math.max(2, rowH * 0.84));
  });
  g.globalAlpha = 1;

  // playhead
  if (session.playing) {
    const el = Math.max(0, actx().currentTime - session.playBaseCtx);
    const x = (el / span) * W;
    g.strokeStyle = '#e5484d';
    g.lineWidth = 1.5;
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, H); g.stroke();
  }
  if (!session.notes.length) {
    g.fillStyle = muted;
    g.font = '12px sans-serif';
    g.fillText('Запишите или импортируйте ноты — они появятся здесь', 12, H / 2);
  }
}

function rollTick() {
  if (!session || !session.playing) { if (session) session.rafId = null; return; }
  drawRoll();
  session.rafId = requestAnimationFrame(rollTick);
}

function rollPointAt(ev) {
  const r = dom.roll.getBoundingClientRect();
  const x = ev.clientX - r.left;
  const y = ev.clientY - r.top;
  const { first, span, lo, hi } = rollSpan();
  const rows = Math.max(1, hi - lo);
  const rowH = r.height / rows;
  const t = first + (x / r.width) * span;
  const midi = Math.round(lo + (r.height - y) / rowH - 0.5);
  return { x, y, t, midi, rowH };
}

function onRollClick(ev) {
  if (!session || !session.notes.length) return;
  const r = dom.roll.getBoundingClientRect();
  const { first, span, lo, hi } = rollSpan();
  const rowH = r.height / Math.max(1, hi - lo);
  const px = ev.clientX - r.left;
  const py = ev.clientY - r.top;
  const idx = session.notes.findIndex((nt) => {
    const x0 = ((nt.start - first) / span) * r.width;
    const x1 = ((nt.end - first) / span) * r.width;
    const yc = (r.height - (nt.midi - lo + 0.5) * rowH);
    return px >= x0 && px <= Math.max(x1, x0 + 2) &&
           py >= yc - rowH * 0.5 && py <= yc + rowH * 0.5;
  });
  if (idx >= 0) {
    session.notes.splice(idx, 1);
    updateCount(); updateButtons(); drawRoll();
    status('Нота удалена');
  }
}

/* ------------------------------------------------------------- импорт MIDI */

function parseMidi(buf) {
  const dv = new DataView(buf);
  if (buf.byteLength < 14 || dv.getUint32(0) !== 0x4D546864) throw new Error('не MIDI-файл');
  let p = 4;                       // после «MThd»
  const hlen = dv.getUint32(p); p += 4;
  p += 2;                          // формат (0/1/2) — читаем впустую
  const ntrk = dv.getUint16(p); p += 2;
  const div = dv.getUint16(p);
  p = 8 + hlen;
  if (div & 0x8000) throw new Error('SMPTE-тайминг не поддержан');
  const ppq = div;
  let tempo = 500000;
  const raw = [];
  for (let tr = 0; tr < ntrk && p + 8 <= buf.byteLength; tr++) {
    if (dv.getUint32(p) !== 0x4D54726B) break;
    p += 4;
    const len = dv.getUint32(p); p += 4;
    const end = p + len;
    let tick = 0, running = 0;
    const active = new Map();
    while (p < end) {
      let delta = 0, b;
      do { b = dv.getUint8(p++); delta = (delta << 7) | (b & 0x7f); } while (b & 0x80);
      tick += delta;
      let status = dv.getUint8(p);
      if (status & 0x80) { p++; running = status; } else { status = running; }
      const type = status & 0xf0;
      if (status === 0xFF) {
        const meta = dv.getUint8(p++);
        let mlen = 0;
        do { b = dv.getUint8(p++); mlen = (mlen << 7) | (b & 0x7f); } while (b & 0x80);
        if (meta === 0x51 && mlen === 3) {
          tempo = (dv.getUint8(p) << 16) | (dv.getUint8(p + 1) << 8) | dv.getUint8(p + 2);
        }
        p += mlen;
      } else if (status === 0xF0 || status === 0xF7) {
        let slen = 0;
        do { b = dv.getUint8(p++); slen = (slen << 7) | (b & 0x7f); } while (b & 0x80);
        p += slen;
      } else if (type === 0x90 || type === 0x80) {
        const note = dv.getUint8(p++);
        const vel = dv.getUint8(p++);
        if (type === 0x90 && vel > 0) {
          if (!active.has(note)) active.set(note, []);
          active.get(note).push({ tick, vel });
        } else {
          const st = active.get(note);
          if (st && st.length) {
            const on = st.shift();
            raw.push({ midi: note, t0: on.tick, t1: tick, vel: on.vel });
          }
        }
      } else if (type === 0xA0 || type === 0xB0 || type === 0xE0) {
        p += 2;
      } else if (type === 0xC0 || type === 0xD0) {
        p += 1;
      } else {
        break;
      }
    }
    p = end;
  }
  const secPerTick = (tempo / 1e6) / ppq;
  raw.sort((a, b) => a.t0 - b.t0);
  const first = raw.length ? raw[0].t0 : 0;
  const notes = raw.map((n) => ({
    midi: n.midi,
    start: (n.t0 - first) * secPerTick,
    end: Math.max((n.t1 - first) * secPerTick, (n.t0 - first) * secPerTick + 0.02),
    velocity: Math.max(0.1, Math.min(1, n.vel / 127)),
  }));
  return { notes, tempo: Math.round(60000000 / tempo), ppq };
}

async function importMidiFile(file) {
  try {
    const buf = await file.arrayBuffer();
    const parsed = parseMidi(buf);
    if (!parsed.notes.length) { status('В MIDI-файле нет нот', 'warn'); return; }
    stopAll();
    session.notes = parsed.notes;
    if (parsed.tempo) { session.tempo = parsed.tempo; dom.tempo.value = parsed.tempo; }
    updateCount(); updateButtons(); drawRoll();
    status('Импортировано нот: ' + parsed.notes.length);
  } catch (e) {
    status('Ошибка импорта: ' + e.message, 'bad');
  }
}

/* ------------------------------------------------------------- экспорт */

function encodeWav(buffer) {
  const numCh = buffer.numberOfChannels, len = buffer.length, sr = buffer.sampleRate;
  const bytes = 44 + len * numCh * 2;
  const view = new DataView(new ArrayBuffer(bytes));
  const w = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
  w(0, 'RIFF'); view.setUint32(4, bytes - 8, true); w(8, 'WAVE');
  w(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, numCh, true); view.setUint32(24, sr, true);
  view.setUint32(28, sr * numCh * 2, true); view.setUint16(32, numCh * 2, true);
  view.setUint16(34, 16, true); w(36, 'data'); view.setUint32(40, len * numCh * 2, true);
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

/* Превращаем исполнение в аудиобуфер тем же синтезом — для выгрузки в галерею. */
async function renderPerformance() {
  const notes = session.notes;
  const first = notes[0].start;
  const total = (notes[notes.length - 1].end - first) + 1.0;
  const sr = 44100;
  const offline = new OfflineAudioContext(1, Math.ceil(total * sr), sr);
  notes.forEach((nt) => {
    const t0 = nt.start - first + 0.02;
    const dur = Math.max(0.08, nt.end - nt.start);
    scheduleVoice(offline, offline.destination, nt.midi, t0, dur, nt.velocity, session.instrument);
  });
  return await offline.startRendering();
}

async function exportWav() {
  const file = bridge();
  if (!session.notes.length) return;
  dom.wav.disabled = true;
  status('Собираем WAV…');
  try {
    const rendered = await renderPerformance();
    const blob = encodeWav(rendered);
    const name = 'midi-' + Date.now().toString().slice(-6) + '.wav';
    const form = new FormData();
    form.append('file', blob, name);
    form.append('kind', 'input');
    const res = await file.api('/upload', { method: 'POST', body: form });
    await file.loadGallery(false);
    if (res && res.asset && file.openAsset) file.openAsset(res.asset.id);
    file.toast('Сохранено: ' + name, 'success');
    status('Готово');
  } catch (e) {
    status('Ошибка: ' + e.message, 'bad');
    file.toast(e.message, 'error', 8000);
  } finally {
    updateButtons();
  }
}

/* Стандартный MIDI-файл (format 0, один трек). */
function exportMidi() {
  const notes = session.notes;
  if (!notes.length) return;
  const PPQ = 480;
  const first = notes[0].start;
  const events = [];
  notes.forEach((nt) => {
    events.push({ t: nt.start - first, kind: 'on', midi: nt.midi, vel: Math.round((nt.velocity || 0.8) * 127) });
    events.push({ t: nt.end - first, kind: 'off', midi: nt.midi, vel: 0 });
  });
  events.sort((a, b) => (a.t - b.t) || (a.kind === 'off' ? -1 : 1));
  const ticksPerSec = PPQ * (session.tempo || 120) / 60;
  const secToTick = (t) => Math.round(t * ticksPerSec);

  const track = [];
  const push = (arr) => track.push.apply(track, arr);
  // Tempo: микросекунд на четверть.
  const usPerBeat = Math.round(60000000 / (session.tempo || 120));
  push([0x00, 0xFF, 0x51, 0x03,
        (usPerBeat >> 16) & 0xFF, (usPerBeat >> 8) & 0xFF, usPerBeat & 0xFF]);
  let last = 0;
  events.forEach((ev) => {
    const tick = secToTick(ev.t);
    const delta = Math.max(0, tick - last);
    last = tick;
    const d = vlq(delta);
    if (ev.kind === 'on') push(d.concat([0x90, ev.midi & 0x7F, ev.vel & 0x7F]));
    else push(d.concat([0x80, ev.midi & 0x7F, 0x40]));
  });
  push(vlq(0).concat([0xFF, 0x2F, 0x00]));

  const header = [0x4D, 0x54, 0x68, 0x64, 0, 0, 0, 6, 0, 0, 0, 1, (PPQ >> 8) & 0xFF, PPQ & 0xFF];
  const trkHeader = [0x4D, 0x54, 0x72, 0x6B].concat(u32(track.length));
  const bytes = new Uint8Array(header.concat(trkHeader).concat(track));

  const blob = new Blob([bytes], { type: 'audio/midi' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'genlab-performance.mid';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  status('MIDI-файл сохранён');
}

function vlq(n) {
  const out = [n & 0x7F];
  n >>= 7;
  while (n > 0) { out.unshift((n & 0x7F) | 0x80); n >>= 7; }
  return out;
}
function u32(n) { return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255]; }

/* ------------------------------------------------------------- жизненный цикл */

function open() {
  build();
  const wasHidden = dom.root.hidden;
  dom.root.hidden = false;
  if (!session) {
    session = {
      baseMidi: 60, instrument: 'piano', volume: 0.6,
      keysDown: new Set(), voices: new Map(), pending: new Map(),
      notes: [], recording: false, playing: false,
      playNodes: [], playTimer: null, recBase: 0,
      tempo: 120, metronome: false, metroTimer: null, metroBeat: 0,
      playBaseCtx: 0, playSpan: 0, rafId: null,
    };
    dom.inst.value = 'piano';
    dom.vol.value = '0.6';
    dom.tempo.value = '120';
  }
  dom.root.querySelector('#mdKeys').style.visibility = 'visible';
  updateOctLabel();
  renderKeys();
  updateCount();
  updateButtons();
  actx().resume();
  requestAnimationFrame(drawRoll);
  if (wasHidden) status('');
}

function close() {
  if (!session) return;
  stopMetronome();
  stopAll();
  session.keysDown.forEach((m) => liveVoiceOff(m));
  session.keysDown.clear();
  if (session.recording) toggleRecord();
  dom.root.hidden = true;
}

function shiftOctave(delta) {
  session.baseMidi = Math.max(12, Math.min(96, session.baseMidi + delta * 12));
  updateOctLabel();
  renderKeys();
}

function updateOctLabel() {
  dom.octLabel.textContent = midiName(session.baseMidi);
}

/* ------------------------------------------------------------- привязки */

function bind() {
  const d = build();
  d.root.querySelector('#mdClose').addEventListener('click', close);
  d.inst.addEventListener('change', () => { session.instrument = d.inst.value; });
  d.vol.addEventListener('input', () => {
    session.volume = Number(d.vol.value);
    if (master) master.gain.value = session.volume;
  });
  d.root.querySelector('#mdOctDown').addEventListener('click', () => shiftOctave(-1));
  d.root.querySelector('#mdOctUp').addEventListener('click', () => shiftOctave(1));
  d.rec.addEventListener('click', toggleRecord);
  d.play.addEventListener('click', playback);
  d.stop.addEventListener('click', stopAll);
  d.clear.addEventListener('click', () => {
    session.notes = []; updateCount(); updateButtons(); status(''); drawRoll();
  });
  d.midi.addEventListener('click', exportMidi);
  d.wav.addEventListener('click', exportWav);
  d.tempo.addEventListener('change', () => {
    const v = Math.max(30, Math.min(300, Number(d.tempo.value) || 120));
    session.tempo = v; d.tempo.value = v; drawRoll();
  });
  d.metro.addEventListener('click', toggleMetronome);
  d.quant.addEventListener('click', quantizeNotes);
  d.importInput.addEventListener('change', () => {
    if (d.importInput.files && d.importInput.files[0]) importMidiFile(d.importInput.files[0]);
    d.importInput.value = '';
  });
  d.roll.addEventListener('click', onRollClick);

  document.addEventListener('keydown', (ev) => {
    if (!session || dom.root.hidden) return;
    if (ev.key === 'Escape') { close(); return; }
    if (ev.target && /INPUT|TEXTAREA|SELECT/.test(ev.target.tagName)) return;
    if (ev.repeat) return;
    const semi = KEYMAP[ev.key.toLowerCase()];
    if (semi == null) return;
    ev.preventDefault();
    pressKey(session.baseMidi + semi, 0.8);
  });
  document.addEventListener('keyup', (ev) => {
    if (!session || dom.root.hidden) return;
    const semi = KEYMAP[ev.key.toLowerCase()];
    if (semi == null) return;
    releaseKey(session.baseMidi + semi);
  });
}

window.GenLabMidi = {
  open, close, ready: false,
  _debug: {
    play: (m) => pressKey(m, 0.8),
    stop: (m) => releaseKey(m),
    session: () => session,
    render: renderPerformance,
    encodeWav,
    quantize: quantizeNotes,
    import: importMidiFile,
    parseMidi,
    setNotes: (notes) => { if (session) { session.notes = notes; updateCount(); updateButtons(); drawRoll(); } },
    setTempo: (bpm) => { if (session) { session.tempo = bpm; if (dom) dom.tempo.value = bpm; drawRoll(); } },
    metroOn: () => startMetronome(),
    metroOff: () => stopMetronome(),
  },
};

document.addEventListener('DOMContentLoaded', () => {
  const start = () => { bind(); window.GenLabMidi.ready = true; };
  if (window.GenLab) start(); else setTimeout(start, 0);
});
