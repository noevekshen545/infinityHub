/* Infinity — клиентский офлоад.
   Выполняет часть обработки в Web Worker на устройстве пользователя, чтобы
   не грузить сервер. Всё управляется настройками админки (/api/offload/config),
   по умолчанию выключено. Также умеет работать устройством-помощником.

   Уведомления (баннер согласия и плашки статуса) живут по трём правилам:
     • показывать их разрешает только рубильник decentral.notify.enabled;
     • каждое уведомление — ровно один раз на устройство, отметки хранит
       сервер по идентификатору устройства, а не по хранилищу браузера;
     • на машине, где стоит сервер, уведомлений нет вообще: спрашивать
       разрешение на помощь её же процессору не у кого. */
(function () {
  'use strict';
  if (window.__infinityOffload) return;
  window.__infinityOffload = true;

  var WORKER_URL = '/static/js/offload_worker.js';
  var CONFIG = { enabled: false, tasks: {}, helpers_enabled: false, helper_tasks: [],
                 require_approval: false, require_consent: true, notify_enabled: false,
                 is_local: false, consent: null, consentLoaded: false,
                 notice: {}, ask_banner: false };
  var worker = null, seq = 0, waiters = {};
  var stats = {};             // type -> {count, total_ms, errors}
  var flushTimer = null;
  var helperWs = null, helperTimer = null, helperPing = null, helperDelay = 2000;
  var helperStopped = false, helperState = null;
  var deviceId = null;

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) {} }

  // ── mesh.identity ───────────────────────────────────────────────────────
  // Подпись подтверждения считается на чистом JS, а не через crypto.subtle:
  // приложение routinely отдаётся по http://192.168.x.x, где subtle нет, а
  // проверять личность нужно и там. Байт-в-байт с серверным hmac.new это
  // доказано гейтом (tools/mesh_identity_parity.py), а не на слово.
  var SHA_K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ];

  function _rotr(x, n) { return ((x >>> n) | (x << (32 - n))) >>> 0; }

  function sha256Bytes(bytes) {
    var len = bytes.length;
    var bitLen = len * 8;
    var padded = Math.ceil((len + 9) / 64) * 64;
    var d = new Uint8Array(padded);
    d.set(bytes);
    d[len] = 0x80;
    var hi = Math.floor(bitLen / 4294967296);
    var lo = (bitLen - hi * 4294967296) >>> 0;
    for (var q = 0; q < 4; q++) d[padded - 8 + q] = (hi >>> (24 - q * 8)) & 255;
    for (q = 0; q < 4; q++) d[padded - 4 + q] = (lo >>> (24 - q * 8)) & 255;
    var H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
             0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    var w = new Uint32Array(64);
    for (var off = 0; off < padded; off += 64) {
      for (var i = 0; i < 16; i++) {
        var p = off + i * 4;
        w[i] = ((d[p] << 24) | (d[p + 1] << 16) | (d[p + 2] << 8) | d[p + 3]) >>> 0;
      }
      for (i = 16; i < 64; i++) {
        var s0 = (_rotr(w[i - 15], 7) ^ _rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3)) >>> 0;
        var s1 = (_rotr(w[i - 2], 17) ^ _rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10)) >>> 0;
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      var a = H[0], b = H[1], c = H[2], e = H[3], f = H[4], g = H[5], hh = H[6], k = H[7];
      for (i = 0; i < 64; i++) {
        var S1 = (_rotr(f, 6) ^ _rotr(f, 11) ^ _rotr(f, 25)) >>> 0;
        var ch = ((f & g) ^ ((~f >>> 0) & hh)) >>> 0;
        var t1 = (k + S1 + ch + SHA_K[i] + w[i]) >>> 0;
        var S0 = (_rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)) >>> 0;
        var maj = ((a & b) ^ (a & c) ^ (b & c)) >>> 0;
        var t2 = (S0 + maj) >>> 0;
        k = hh; hh = g; g = f; f = (e + t1) >>> 0; e = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0; H[2] = (H[2] + c) >>> 0;
      H[3] = (H[3] + e) >>> 0; H[4] = (H[4] + f) >>> 0; H[5] = (H[5] + g) >>> 0;
      H[6] = (H[6] + hh) >>> 0; H[7] = (H[7] + k) >>> 0;
    }
    var out = new Uint8Array(32);
    for (i = 0; i < 8; i++) {
      out[i * 4] = (H[i] >>> 24) & 255; out[i * 4 + 1] = (H[i] >>> 16) & 255;
      out[i * 4 + 2] = (H[i] >>> 8) & 255; out[i * 4 + 3] = H[i] & 255;
    }
    return out;
  }

  function _toBytes(s) {
    if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(s);
    var out = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i) & 255;
    return out;
  }

  function _toHex(bytes) {
    var s = '';
    for (var i = 0; i < bytes.length; i++) s += ('0' + bytes[i].toString(16)).slice(-2);
    return s;
  }

  function hmacSha256Hex(keyStr, msgStr) {
    var key = _toBytes(String(keyStr || ''));
    if (key.length > 64) key = sha256Bytes(key);
    var block = new Uint8Array(64);
    block.set(key);
    var ip = new Uint8Array(64), op = new Uint8Array(64);
    for (var i = 0; i < 64; i++) { ip[i] = (block[i] ^ 0x36) & 255; op[i] = (block[i] ^ 0x5c) & 255; }
    var msg = _toBytes(String(msgStr || ''));
    var inner = new Uint8Array(64 + msg.length);
    inner.set(ip); inner.set(msg, 64);
    var ih = sha256Bytes(inner);
    var outer = new Uint8Array(96);
    outer.set(op); outer.set(ih, 64);
    return _toHex(sha256Bytes(outer));
  }

  function meshSecretKey(deviceId) { return 'infinity_mesh_secret_' + deviceId; }

  function meshHandle(msg) {
    if (msg.action === 'mesh_key') {
      // Сервер выдаёт секрет этому устройству один раз; дальше он только
      // спрашивает подпись. Сам секрет наружу не уходит ни одним путём.
      if (msg.secret) lsSet(meshSecretKey(msg.device || getDeviceId()), String(msg.secret));
      return true;
    }
    if (msg.action === 'mesh_revoke') {
      lsDel(meshSecretKey(msg.device || getDeviceId()));
      return true;
    }
    if (msg.action === 'mesh_challenge') {
      var secret = lsGet(meshSecretKey(msg.device || getDeviceId()));
      if (!secret || !msg.nonce) return true;
      try {
        helperWs.send(JSON.stringify({ action: 'mesh_proof', device: msg.device || getDeviceId(),
          nonce: msg.nonce, mac: hmacSha256Hex(secret, (msg.device || getDeviceId()) + '|' + msg.nonce) }));
      } catch (e) {}
      return true;
    }
    return false;
  }

  function fnv1a128(s) {
    // 4 независимых 32-битных потока FNV-1a = 128 бит: двух устройств не
    // спутает, а Math.imul даёт точное целое (обычное * теряет биты за
    // пределами 2^53). Это не crypto.subtle: тот недоступен в небезопасном
    // контексте, а приложение отдаётся по http://192.168.x.x.
    var p = 0x01000193;
    var h = [0x811c9dc5, 0xcbf29ce4, 0x84222325, 0x01000193];
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      for (var k = 0; k < 4; k++) h[k] = Math.imul(h[k] ^ c, p) >>> 0;
    }
    for (var j = 0; j < 4; j++) h[j] = (h[j] ^ h[(j + 1) & 3]) >>> 0;
    var out = '';
    for (var m = 0; m < 4; m++) out += ('0000000' + h[m].toString(16)).slice(-8);
    return out;
  }

  // Устойчивый идентификатор устройства. Куки и localStorage под ним не:
  // их чистят, в приватном режиме их нет, а в двух браузерах на одном
  // телефоне это два разных «устройства». MAC адрес браузеру недоступен
  // (и в мобильных ОС он рандомизируется), поэтому берётся то, что у
  // устройства действительно своё: железо, экран, пояс и подпись канваса.
  // Обновление браузера меняет отпечаток — это новая сборка возможностей,
  // и запросить согласие один раз заново в таком случае честно.
  function fingerprint() {
    try {
      var w = Math.max(screen.width || 0, screen.availWidth || 0);
      var hh = Math.min(screen.width || 0, screen.availWidth || 0);
      var parts = [
        navigator.userAgent || '',
        navigator.platform || '',
        navigator.language || '',
        (navigator.languages || []).join(','),
        w + 'x' + hh + 'x' + (screen.pixelDepth || 24),
        String(window.devicePixelRatio || 1),
        String(navigator.hardwareConcurrency || 0),
        String(navigator.deviceMemory || 0),
        String(navigator.maxTouchPoints || 0),
        String(new Date().getTimezoneOffset()),
        (window.Intl && Intl.DateTimeFormat)
          ? (Intl.DateTimeFormat().resolvedOptions().timeZone || '') : ''
      ];
      var sig = parts.join('|');
      try {
        var c = document.createElement('canvas');
        c.width = 180; c.height = 32;
        var g = c.getContext && c.getContext('2d');
        if (g) {
          g.textBaseline = 'top';
          g.font = '14px sans-serif';
          g.fillStyle = '#f60';
          g.fillText('infinity-offload', 2, 2);
          g.fillStyle = 'rgba(0,90,200,.7)';
          g.fillRect(2, 20, 70, 6);
          sig += '|' + c.toDataURL();
        }
      } catch (e) { /* канвас бывает закрыт — отпечаток просто грубее */ }
      return 'dev-' + fnv1a128(sig).slice(0, 16);
    } catch (e) {
      return null;
    }
  }

  function getDeviceId() {
    if (deviceId) return deviceId;
    deviceId = fingerprint();
    if (!deviceId) {
      // Отпечаток не собрался (нет screen/canvas): прежнее поведение —
      // случайный идентификатор в хранилище, иначе помощник не появится.
      deviceId = lsGet('infinity_offload_device');
      if (!deviceId) {
        deviceId = 'dev-' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
        lsSet('infinity_offload_device', deviceId);
      }
    } else {
      lsSet('infinity_offload_device', deviceId);
    }
    return deviceId;
  }

  function ensureWorker() {
    if (worker) return worker;
    worker = new Worker(WORKER_URL);
    worker.onmessage = function (e) {
      var m = e.data || {};
      var w = waiters[m.id];
      if (!w) return;
      delete waiters[m.id];
      clearTimeout(w.timer);
      if (m.ok) w.resolve(m.result);
      else w.reject(new Error(m.error || 'task failed'));
    };
    worker.onerror = function (e) {
      Object.keys(waiters).forEach(function (id) {
        var w = waiters[id];
        delete waiters[id];
        clearTimeout(w.timer);
        w.reject(new Error('worker error: ' + (e.message || 'unknown')));
      });
    };
    return worker;
  }

  function run(type, payload, opts) {
    opts = opts || {};
    if (!opts.force) {
      if (!CONFIG.enabled) return Promise.reject(new Error('client offload disabled'));
      if (!CONFIG.tasks || !CONFIG.tasks[type]) return Promise.reject(new Error('task disabled: ' + type));
    }
    var id = ++seq;
    var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        delete waiters[id];
        reject(new Error('timeout: ' + type));
      }, opts.timeout || 15000);
      waiters[id] = { resolve: resolve, reject: reject, timer: timer };
      try {
        ensureWorker().postMessage({ id: id, type: type, payload: payload });
      } catch (e) {
        clearTimeout(timer);
        delete waiters[id];
        reject(e);
      }
    }).then(function (res) {
      note(type, true, ((window.performance && performance.now) ? performance.now() : Date.now()) - t0);
      return res;
    }, function (err) {
      note(type, false, ((window.performance && performance.now) ? performance.now() : Date.now()) - t0);
      throw err;
    });
  }

  function note(type, ok, ms) {
    var s = stats[type] || (stats[type] = { count: 0, total_ms: 0, errors: 0 });
    s.count++;
    s.total_ms += ms || 0;
    if (!ok) s.errors++;
  }

  function flushStats() {
    var jobs = Object.keys(stats).map(function (t) {
      var s = stats[t];
      return { type: t, ok: !s.errors, count: s.count,
               avg_ms: s.count ? s.total_ms / s.count : 0 };
    });
    if (!jobs.length) return;
    stats = {};
    try {
      fetch('/api/offload/stats', {
        method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jobs: jobs })
      }).catch(function () {});
    } catch (e) {}
  }

  // ── Режим помощника ──────────────────────────────────────────────────────

  // Показывать ли что-нибудь этому экрану вообще: рубильник уведомлений и
  // признак «это серверная машина».
  function silent() {
    return !CONFIG.notify_enabled || CONFIG.is_local;
  }

  function noticeSeen(kind) {
    if (CONFIG.notice && CONFIG.notice[kind]) return true;
    // Локальная отметка — только про две вкладки, открытые одновременно:
    // серверное решение по устройству первично, его она не переопределяет.
    return lsGet('infinity_offload_notice_' + kind) === '1';
  }

  function markNotice(kind) {
    lsSet('infinity_offload_notice_' + kind, '1');
    if (CONFIG.notice) CONFIG.notice[kind] = true;
    post('/api/offload/helper/notice', { device: getDeviceId(), kind: kind });
  }

  function post(url, data) {
    try {
      fetch(url, {
        method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      }).catch(function () {});
    } catch (e) {}
  }

  function sendConsent(answer) {
    CONFIG.consent = answer;
    lsSet('infinity_offload_helper', answer);
    post('/api/offload/helper/consent', {
      device: getDeviceId(), answer: answer,
      name: (navigator.platform || 'device') + ' ' + (navigator.userAgent || '').slice(0, 60)
    });
  }

  function helperNote(text, sticky) {
    var id = 'offloadHelperNote';
    var el = document.getElementById(id);
    if (!el) {
      if (!document.body) return;
      el = document.createElement('div');
      el.id = id;
      el.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483646;' +
        'max-width:340px;padding:10px 14px;border-radius:12px;background:#151b26;' +
        'color:#e6edf7;border:1px solid rgba(255,255,255,.14);' +
        'box-shadow:0 10px 30px rgba(0,0,0,.4);font:500 12.5px/1.4 system-ui,Segoe UI,Roboto,sans-serif';
      el.dataset.sticky = sticky ? '1' : '';
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.dataset.sticky = sticky ? '1' : '';
    if (!sticky) {
      clearTimeout(el._t);
      el._t = setTimeout(function () { if (el.dataset.sticky !== '1') el.remove(); }, 8000);
    }
  }

  function hideHelperNote(force) {
    var el = document.getElementById('offloadHelperNote');
    if (!el) return;
    if (force || el.dataset.sticky !== '1') el.remove();
  }

  function showNotice(kind, text, sticky) {
    if (silent() || noticeSeen(kind)) return;
    markNotice(kind);
    helperNote(text, sticky);
  }

  function applyHelperState(state) {
    if (!state) return;
    var prev = helperState;
    helperState = state;
    if (state === 'pending') {
      showNotice('pending', 'Устройство ожидает одобрения администратора. Задачи пока не приходят.', true);
    } else if (state === 'approved') {
      // «Ожидает одобрения» было липким: после одобрения оно обязано уйти,
      // иначе устройство до конца дня читает про себя вчерашний статус.
      hideHelperNote(true);
      if (prev === 'pending') {
        showNotice('approved', 'Устройство одобрено — помогает с обработкой.', false);
      }
    } else if (state === 'rejected') {
      helperStopped = true;
      showNotice('rejected', 'Администратор отклонил это устройство. Помощь отключена.', true);
      try { if (helperWs) helperWs.close(); } catch (e) {}
    }
  }

  function showConsentBanner() {
    if (document.getElementById('offloadConsent')) return;
    if (!document.body) return;
    markNotice('banner');
    var el = document.createElement('div');
    el.id = 'offloadConsent';
    el.style.cssText = 'position:fixed;left:16px;right:16px;bottom:16px;z-index:2147483647;' +
      'max-width:520px;margin:0 auto;padding:14px 16px;border-radius:14px;' +
      'background:#151b26;color:#e6edf7;border:1px solid rgba(255,255,255,.12);' +
      'box-shadow:0 12px 40px rgba(0,0,0,.45);font:500 13px/1.45 system-ui,Segoe UI,Roboto,sans-serif';
    el.innerHTML = '<div style="font-weight:700;margin-bottom:6px">Помочь обработкой?</div>' +
      '<div style="opacity:.85;margin-bottom:10px">Это устройство может выполнять часть задач ' +
      '(сжатие картинок, хеши) в фоне и отправлять только результат. Данные не сохраняются.</div>' +
      '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button id="offloadNo" style="padding:7px 12px;border-radius:9px;border:1px solid rgba(255,255,255,.18);background:transparent;color:#e6edf7;cursor:pointer">Нет</button>' +
      '<button id="offloadYes" style="padding:7px 12px;border-radius:9px;border:0;background:#4f7cff;color:#fff;cursor:pointer;font-weight:600">Разрешить</button>' +
      '</div>';
    document.body.appendChild(el);
    el.querySelector('#offloadYes').onclick = function () {
      sendConsent('yes'); el.remove(); startHelper();
    };
    el.querySelector('#offloadNo').onclick = function () {
      sendConsent('no'); el.remove();
    };
  }

  function startHelper() {
    if (helperStopped || helperWs || CONFIG.is_local) return;
    var tasks = (CONFIG.helper_tasks || []).join(',');
    if (!tasks) return;
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    var name = encodeURIComponent((navigator.platform || 'device') + ' ' + (navigator.userAgent || '').slice(0, 60));
    var url = proto + location.host + '/api/offload/helper/ws?device=' +
      encodeURIComponent(getDeviceId()) + '&consent=' + (CONFIG.consent === 'no' ? '0' : '1') +
      '&tasks=' + encodeURIComponent(tasks) + '&name=' + name;
    try { helperWs = new WebSocket(url); } catch (e) { helperWs = null; scheduleHelper(); return; }
    helperWs.onopen = function () {
      helperDelay = 2000;
      clearInterval(helperPing);
      helperPing = setInterval(function () {
        try { helperWs.send(JSON.stringify({ action: 'ping' })); } catch (e) {}
      }, 25000);
    };
    helperWs.onmessage = function (e) {
      var msg;
      try { msg = JSON.parse(e.data); } catch (err) { return; }
      if (meshHandle(msg)) return;
      if (msg.action === 'hello' || msg.action === 'state') {
        applyHelperState(msg.state);
        return;
      }
      if (msg.action === 'approved') { applyHelperState('approved'); return; }
      if (msg.action === 'rejected') { applyHelperState('rejected'); return; }
      if (msg.action === 'pong') {
        if (msg.state) applyHelperState(msg.state);
        return;
      }
      if (msg.action === 'error') {
        if (msg.state === 'rejected') applyHelperState('rejected');
        else if (msg.state === 'no_consent' || msg.state === 'local') {
          // Сервер не пустил по своим основаниям: решения и отметки живут на
          // сервере, а не в этом окне, поэтому здесь только молча встаём.
          helperStopped = true;
        } else {
          showNotice('error', 'Помощник: ' + (msg.error || 'ошибка подключения'), true);
        }
        return;
      }
      if (msg.action === 'job' && msg.job_id) {
        if ((CONFIG.helper_tasks || []).indexOf(msg.type) === -1) {
          var tBad = (performance && performance.now) ? performance.now() : Date.now();
          replyJob(msg.job_id, false, null, tBad, 'task not allowed');
          return;
        }
        var t0 = (performance && performance.now) ? performance.now() : Date.now();
        run(msg.type, msg.payload, { force: true, timeout: msg.timeout_ms || 15000 }).then(function (result) {
          replyJob(msg.job_id, true, result, t0);
        }, function (err) {
          replyJob(msg.job_id, false, null, t0, err && err.message);
        });
      }
    };
    helperWs.onerror = function () { try { helperWs.close(); } catch (e) {} };
    helperWs.onclose = function () { clearInterval(helperPing); helperWs = null; scheduleHelper(); };
  }

  function replyJob(jobId, ok, result, t0, error) {
    var ms = ((performance && performance.now) ? performance.now() : Date.now()) - t0;
    try {
      helperWs.send(JSON.stringify({ action: 'job_result', job_id: jobId, ok: ok,
        result: result, error: error || '', ms: ms }));
    } catch (e) {}
  }

  function consentGiven() {
    // Ответ устройства помнит сервер. Хранилище браузера — только быстрый
    // путь на случай, пока не пришёл ответ конфига.
    var c = CONFIG.consent;
    if (c === 'yes' || c === 'no') return c;
    if (CONFIG.consentLoaded) return null;
    // Сервер уже ответил и решения у него нет: старая отметка в этом окне —
    // чужая память (её писали до серверного хранения, её оставляет другой
    // браузер на том же телефоне). Держать из-за неё помощника выключенным
    // нельзя: при выключенных уведомлениях устройство никогда не узнает,
    // почему оно ничего не считает.
    var ls = lsGet('infinity_offload_helper');
    return ls === 'yes' || ls === 'no' ? ls : null;
  }

  function scheduleHelper() {
    if (helperStopped || CONFIG.is_local || consentGiven() !== 'yes'
        || !CONFIG.helpers_enabled) return;
    clearTimeout(helperTimer);
    helperTimer = setTimeout(startHelper, helperDelay);
    helperDelay = Math.min(helperDelay * 2, 60000);
  }

  function maybeStartHelper() {
    if (!CONFIG.helpers_enabled || CONFIG.is_local) return;
    var decision = consentGiven();
    if (!decision && !CONFIG.require_consent) {
      // Спрос снят администратором: согласие считается выданным заранее,
      // спрашивать на устройстве не о чем и незачем.
      sendConsent('yes');
      decision = 'yes';
    }
    if (decision === 'yes') { startHelper(); return; }
    if (decision === 'no') return;
    // Решения нет. Баннер — единственное, что тут вообще появляется, и
    // появляется он по разрешению сервера: он знает и про рубильник
    // уведомлений, и про то, что этому устройству уже показывали.
    if (CONFIG.notify_enabled && CONFIG.ask_banner) showConsentBanner();
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    if (CONFIG.helpers_enabled && !CONFIG.is_local && consentGiven() === 'yes' && !helperWs) {
      helperDelay = 2000;
      startHelper();
    }
  });
  window.addEventListener('pagehide', function () { flushStats(); });

  function loadConfig() {
    return fetch('/api/offload/config?device=' + encodeURIComponent(getDeviceId()),
                 { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (cfg) {
        CONFIG = Object.assign(CONFIG, cfg || {});
        // Ключ есть в ответе — значит сервер про это устройство знает и его
        // слово первично; хранилище браузера больше ничего не решает.
        if (cfg && Object.prototype.hasOwnProperty.call(cfg, 'consent')) {
          CONFIG.consentLoaded = true;
        }
        if (CONFIG.is_local) helperStopped = true;
        flushTimer = setInterval(flushStats, 30000);
        maybeStartHelper();
        return CONFIG;
      })
      .catch(function () { return CONFIG; });
  }

  function nowMs() {
    return (window.performance && performance.now) ? performance.now() : Date.now();
  }

  // Картинка для пробы возможностей: 16×10 с полосами. Прямоугольная, чтобы
  // поворот был виден как смена размеров, и цветная, чтобы оттенок серого не
  // совпадал с исходником. Свою пользовательскую картинку тест не запрашивает.
  function makeProbePng() {
    if (typeof OffscreenCanvas === 'undefined') {
      return Promise.reject(new Error('OffscreenCanvas недоступен'));
    }
    var cv = new OffscreenCanvas(16, 10);
    var ctx = cv.getContext('2d');
    ctx.fillStyle = '#c0392b'; ctx.fillRect(0, 0, 16, 10);
    ctx.fillStyle = '#2ecc71'; ctx.fillRect(0, 0, 8, 5);
    ctx.fillStyle = '#ffffff'; ctx.fillRect(12, 6, 3, 3);
    return cv.convertToBlob({ type: 'image/png' }).then(function (b) {
      return new Promise(function (res, rej) {
        var r = new FileReader();
        r.onload = function () { res(r.result); };
        r.onerror = function () { rej(r.error || new Error('read error')); };
        r.readAsDataURL(b);
      });
    });
  }

  window.InfinityOffload = {
    ready: loadConfig(),
    get config() { return CONFIG; },
    get helperState() { return helperState; },
    get deviceId() { return getDeviceId(); },
    run: run,
    // локальная проверка возможностей устройства (работает и при выключенном офлоаде)
    selfTest: function () {
      var t0 = nowMs();
      var results = {
        worker: !!window.Worker,
        image: !!window.OffscreenCanvas,
        gzip: typeof CompressionStream !== 'undefined'
      };
      var gz = null;
      return run('hash_sha256', 'infinity', { force: true })
        .then(function (h) {
          results.hash_sha256 = typeof h === 'string' && h.length === 64;
          return run('hash_sha1', 'infinity', { force: true });
        })
        .then(function (h) {
          results.hash_sha1 = typeof h === 'string' && h.length === 40;
          return run('hash_batch', { items: ['a', 'b'], alg: 'sha256' }, { force: true });
        })
        .then(function (arr) {
          results.hash_batch = Array.isArray(arr) && arr.length === 2 && arr[0].length === 64;
          return run('text_extract', '<b>Привет</b> <i>мир</i>', { force: true });
        })
        .then(function (txt) {
          results.text_extract = txt === 'Привет мир';
          return run('word_stats', 'Привет мир. Тест!', { force: true });
        })
        .then(function (st) {
          results.word_stats = !!(st && st.words === 3 && st.sentences === 2);
          return run('base64_encode', 'привет', { force: true });
        })
        .then(function (b64) {
          results.base64 = typeof b64 === 'string' && b64.length > 0;
          return run('base64_decode', b64, { force: true });
        })
        .then(function (txt) {
          results.base64 = results.base64 && txt === 'привет';
          return run('gzip_compress', 'infinity', { force: true });
        })
        .then(function (out) {
          gz = out;
          results.gzip = !!(out && typeof out.base64 === 'string' && out.base64.length);
          return run('gzip_decompress', out && out.base64, { force: true });
        })
        .then(function (out) {
          results.gzip = results.gzip && !!(out && out.text === 'infinity');
          return run('hash_sha512', 'infinity', { force: true });
        })
        .then(function (h) {
          results.hash_sha512 = typeof h === 'string' && h.length === 128;
          return run('json_minify', '{ "a" : 1,\n "b": [2] }', { force: true });
        })
        .then(function (s) {
          results.json_minify = s === '{"a":1,"b":[2]}';
          return run('json_validate', '{"a":1', { force: true });
        })
        .then(function (v) {
          // «Не годится» для этой задачи — правильный ответ: она и существует
          // чтобы отвечать на вопрос о пригодности, а не падать.
          results.json_validate = !!(v && v.ok === false && v.error);
          // Картинки проверяются на специально сгенерированном квадрате:
          // просить у пользователя файл ради теста возможностей нельзя.
          return makeProbePng();
        })
        .then(function (dataUrl) {
          return run('image_reencode', { dataUrl: dataUrl, mime: 'image/webp',
                                         quality: 0.8 }, { force: true });
        })
        .then(function (im) {
          results.image_reencode = !!(im && im.dataUrl && im.type === 'image/webp'
                                      && im.width === 16 && im.height === 10);
          return run('image_rotate', { dataUrl: im.dataUrl, angle: 90,
                                       mime: 'image/webp' }, { force: true });
        })
        .then(function (im2) {
          // Прямоугольник не случайно: квадрат после поворота на 90° выглядит
          // так же, и проверка «помощник повернул» превратилась бы в проверку
          // «помощник что-то вернул».
          results.image_rotate = !!(im2 && im2.width === 10 && im2.height === 16);
          return run('image_flip', { dataUrl: im2.dataUrl, axis: 'h',
                                     mime: 'image/webp' }, { force: true });
        })
        .then(function (im3) {
          results.image_flip = !!(im3 && im3.dataUrl);
          return run('image_grayscale', { dataUrl: im3.dataUrl,
                                          mime: 'image/webp' }, { force: true });
        })
        .then(function (im4) {
          results.image_grayscale = !!(im4 && im4.dataUrl && im4.type === 'image/webp');
          return { ok: true, results: results, ms: Math.round(nowMs() - t0) };
        })
        .catch(function (err) {
          return { ok: true, results: results, ms: Math.round(nowMs() - t0),
                   error: (err && err.message) || String(err) };
        });
    }
  };

  // ── Телеметрия «подвисаний» интерфейса ────────────────────────────────
  // Серверные метрики знают только время своего ответа. Если главный поток
  // браузера занят (тяжёлый рендер, разбор большого JSON, чужая вкладка),
  // ответ приходит за 20 мс, а интерфейс всё равно дёргается — и в админке
  // это выглядело как «сервер ни при чём». PerformanceObserver с типом
  // longtask даёт длительность блокировок main thread (браузеры на Chromium).
  //
  // Отправка: не чаще раза в минуту и только если задачи реально были.
  // Тело крошечное, ошибки игнорируются — телеметрия не должна влиять на
  // работу приложения. На file:// и при отсутствии PerformanceObserver
  // наблюдатель просто не включается.
  (function initLongTaskBeacon() {
    try {
      if (location.protocol === 'file:') return;
      if (typeof PerformanceObserver !== 'function') return;
      var supported = (PerformanceObserver.supportedEntryTypes || []);
      if (supported.indexOf('longtask') < 0) return;

      var PERF_URL = '/api/perf/client';
      var WINDOW_MS = 60000;
      var state = { longtasks: 0, blocked: 0, max: 0, worst: '' };

      function clientId() {
        var id = '';
        try { id = localStorage.getItem('infinity.client.id') || ''; } catch (e) {}
        if (!id) {
          id = 'c-' + Date.now().toString(36) + '-' +
               Math.random().toString(36).slice(2, 10);
          try { localStorage.setItem('infinity.client.id', id); } catch (e) {}
        }
        return id;
      }

      var cid = clientId();

      function describe(entry) {
        try {
          var at = entry.attribution && entry.attribution[0];
          if (!at) return 'longtask';
          return String(at.containerName || at.containerType || at.name || 'longtask');
        } catch (e) { return 'longtask'; }
      }

      try {
        var obs = new PerformanceObserver(function (list) {
          var entries = list.getEntries() || [];
          for (var i = 0; i < entries.length; i++) {
            var ms = Number(entries[i].duration) || 0;
            if (ms < 50) continue;
            state.longtasks += 1;
            state.blocked += ms;
            if (ms > state.max) {
              state.max = ms;
              state.worst = describe(entries[i]);
            }
          }
        });
        obs.observe({ entryTypes: ['longtask'] });
      } catch (e) {
        return;
      }

      function flush() {
        if (!state.longtasks) return;
        var payload = {
          client: cid,
          page: location.pathname + location.hash,
          longtasks: state.longtasks,
          blocked_ms: Math.round(state.blocked),
          max_block_ms: Math.round(state.max),
          worst: state.worst,
          ua: (navigator.userAgent || '').slice(0, 160)
        };
        // Сбрасываем сразу: если отправка не удастся, не будем копить вечно
        // и завышать цифры следующих окон.
        state = { longtasks: 0, blocked: 0, max: 0, worst: '' };
        try {
          var body = JSON.stringify(payload);
          if (navigator.sendBeacon) {
            var blob = new Blob([body], { type: 'application/json' });
            if (navigator.sendBeacon(PERF_URL, blob)) return;
          }
          fetch(PERF_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: body,
            keepalive: true,
            credentials: 'same-origin'
          }).catch(function () {});
        } catch (e) { /* телеметрия не должна ломать страницу */ }
      }

      setInterval(flush, WINDOW_MS);
      window.addEventListener('pagehide', flush);
      document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'hidden') flush();
      });
    } catch (e) { /* нет поддержки — молча выключаемся */ }
  })();
})();
