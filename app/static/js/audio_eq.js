/* ============================================================================
   Десятиполосный эквалайзер и визуализатор.
   ----------------------------------------------------------------------------
   Та же цепочка, что уже работает на странице /media (page_media.html,
   «EQUALIZER & VISUALIZER»): пресет-гейн -> 10 полосовых BiquadFilter ->
   AnalyserNode -> колонки. Настройки те же ключи localStorage (eqCustom,
   eqPreampVal), поэтому настроенный один раз звук не разъезжается между
   страницей медиа и плеером палитры.

   create(audioElement) возвращает объект управления; вызывать можно один раз
   на элемент: Web Audio привязывает MediaElementSource к конкретному <audio>.
   ========================================================================== */
(function () {
  'use strict';

  var BANDS = [60, 170, 310, 600, 1000, 3000, 6000, 12000, 14000, 16000];

  var PRESETS = {
    flat:      [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    bass:      [7, 6, 4, 1, 0, 0, 0, 0, 0, 0],
    vocal:     [-2, -1, 0, 3, 5, 4, 2, 0, -1, -2],
    acoustic:  [4, 3, 1, 0, -1, -1, 0, 2, 3, 3],
    electronic:[6, 5, 1, 0, -2, 2, 4, 5, 6, 6],
    rock:      [5, 4, 2, -1, -2, 1, 3, 4, 5, 5]
  };

  function label(hz) { return hz >= 1000 ? (hz / 1000) + 'k' : String(hz); }

  function create(audioEl) {
    var ctx = null, preamp = null, filters = [], analyser = null, source = null;
    var bypassed = false, anim = null, gains = BANDS.map(function () { return 0; });
    var preampDb = 0;
    /* Граф Web Audio нужен только когда он что-то меняет. После
       createMediaElementSource звук идёт ТОЛЬКО через граф: если контекст
       уснул или упал (в логе Electron — «AudioContext encountered an error»),
       элемент продолжает «играть», но в колонках тишина. Поэтому при плоской
       АЧХ (по умолчанию) граф не создаём и звук идёт напрямую. */
    var graphNeeded = false;

    function isFlat() {
      if (bypassed) return true;
      if (preampDb) return false;
      for (var i = 0; i < gains.length; i++) {
        if (gains[i]) return false;
      }
      return true;
    }

    function restore() {
      try {
        var saved = JSON.parse(localStorage.getItem('eqCustom') || 'null');
        if (Array.isArray(saved)) gains = saved.slice(0, BANDS.length);
        var p = parseFloat(localStorage.getItem('eqPreampVal') || '');
        if (!isNaN(p)) preampDb = p;
      } catch (e) { /* нет настроек — плоский АЧХ */ }
    }

    function save() {
      try {
        localStorage.setItem('eqCustom', JSON.stringify(gains));
        localStorage.setItem('eqPreampVal', String(preampDb));
      } catch (e) { /* */ }
    }

    function build(force) {
      if (ctx) return true;
      if (!force && !graphNeeded && isFlat()) return false;
      try {
        ctx = new (window.AudioContext || window.webkitAudioContext)();
        source = ctx.createMediaElementSource(audioEl);
        preamp = ctx.createGain();
        analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        filters = BANDS.map(function (hz) {
          var f = ctx.createBiquadFilter();
          f.type = 'peaking';
          f.frequency.value = hz;
          f.Q.value = 1;
          return f;
        });
        source.connect(preamp);
        var prev = preamp;
        filters.forEach(function (f) { prev.connect(f); prev = f; });
        prev.connect(analyser);
        analyser.connect(ctx.destination);
        try {
          ctx.addEventListener('statechange', function () {
            /* Браузер усыпляет контекст до первого жеста. Пока он спит,
               звук через граф не идёт — будим при каждом изменении. */
            if (ctx && ctx.state === 'suspended') resume();
            if (ctx && ctx.state === 'closed') { ctx = null; stopVisualizer(); }
          });
        } catch (e) { /* старые движки без addEventListener */ }
        apply();
        return true;
      } catch (e) {
        ctx = null;
        return false;   // браузер не дал AudioContext — звук пойдёт мимо EQ
      }
    }

    function apply() {
      if (!ctx) return;
      preamp.gain.value = bypassed ? 1 : Math.pow(10, preampDb / 20);
      filters.forEach(function (f, i) { f.gain.value = bypassed ? 0 : gains[i]; });
    }

    function resume() {
      if (ctx && ctx.state === 'suspended') { try { ctx.resume(); } catch (e) { /* */ } }
    }

    /** Полосовой график: столбцы частот + линия текущего усиления. */
    function startVisualizer(canvas) {
      if (!canvas) return;
      graphNeeded = true;                 // визуализатору нужен analyser
      if (!build(true) || !analyser || anim) return;
      var c = canvas.getContext('2d');
      var data = new Uint8Array(analyser.frequencyBinCount);
      var draw = function () {
        anim = requestAnimationFrame(draw);
        analyser.getByteFrequencyData(data);
        var W = canvas.width, H = canvas.height;
        c.clearRect(0, 0, W, H);
        var n = 48, bw = W / n;
        for (var i = 0; i < n; i++) {
          var v = data[i * 2] / 255;
          var h = v * H * 0.92;
          c.fillStyle = 'hsl(' + (200 + v * 160) + ',80%,' + (50 + v * 30) + '%)';
          c.fillRect(i * bw + bw * 0.1, H - h, bw * 0.8, h);
        }
      };
      draw();
    }

    function stopVisualizer() {
      if (anim) { cancelAnimationFrame(anim); anim = null; }
    }

    restore();

    return {
      bands: BANDS.map(label),
      presets: PRESETS,
      init: function () { return build(false); },
      resume: resume,
      ready: function () { return !!ctx; },
      gains: function () { return gains.slice(); },
      setGain: function (i, db) {
        gains[i] = db;
        if (build(true)) apply();
        save();
      },
      setPreamp: function (db) {
        preampDb = db;
        if (build(db !== 0)) apply();
        save();
      },
      getPreamp: function () { return preampDb; },
      applyPreset: function (name) {
        var p = PRESETS[name];
        if (!p) return;
        gains = p.slice();
        var flat = true;
        for (var i = 0; i < gains.length; i++) { if (gains[i]) { flat = false; break; } }
        if (build(!flat)) apply();
        save();
      },
      setBypass: function (v) {
        bypassed = !!v;
        if (ctx) { apply(); return; }
        if (!bypassed && !isFlat()) { if (build(true)) apply(); }
      },
      startVisualizer: startVisualizer,
      stopVisualizer: stopVisualizer
    };
  }

  window.AudioEQ = { create: create, BANDS: BANDS, PRESETS: PRESETS };
})();
