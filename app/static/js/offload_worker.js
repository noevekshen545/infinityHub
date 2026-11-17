/* Infinity — Web Worker для задач, вынесенных на клиент.
   Никакого произвольного кода: только фиксированный набор чистых операций.
   Запрос: {id, type, payload}; ответ: {id, ok, result|error, ms}. */
(function () {
  'use strict';

  function blobToDataUrl(b) {
    return new Promise(function (res, rej) {
      var r = new FileReader();
      r.onload = function () { res(r.result); };
      r.onerror = function () { rej(r.error || new Error('read error')); };
      r.readAsDataURL(b);
    });
  }

  function bufToB64(u8) {
    var s = '', chunk = 0x8000;
    for (var i = 0; i < u8.length; i += chunk) {
      s += String.fromCharCode.apply(null, u8.subarray(i, Math.min(i + chunk, u8.length)));
    }
    return btoa(s);
  }

  function b64ToBuf(b64) {
    var s = atob(String(b64 || '').replace(/\s+/g, ''));
    var u8 = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) u8[i] = s.charCodeAt(i);
    return u8;
  }

  async function digest(payload, alg) {
    var bytes;
    if (typeof payload === 'string') bytes = new TextEncoder().encode(payload);
    else if (payload instanceof ArrayBuffer) bytes = new Uint8Array(payload);
    else bytes = payload;
    var buf = await crypto.subtle.digest(alg, bytes);
    return Array.from(new Uint8Array(buf)).map(function (b) {
      return b.toString(16).padStart(2, '0');
    }).join('');
  }

  function hashSha256(payload) { return digest(payload, 'SHA-256'); }
  function hashSha1(payload) { return digest(payload, 'SHA-1'); }
  function hashSha512(payload) { return digest(payload, 'SHA-512'); }

  var BATCH_ALG = { sha1: 'SHA-1', sha256: 'SHA-256', sha512: 'SHA-512' };

  async function hashBatch(payload) {
    var items = (payload && payload.items) || [];
    var alg = String((payload && payload.alg) || 'sha256').toLowerCase();
    if (!Array.isArray(items)) throw new Error('items must be an array');
    if (items.length > 1000) throw new Error('too many items (max 1000)');
    var out = [];
    for (var i = 0; i < items.length; i++) {
      out.push(await digest(items[i], BATCH_ALG[alg] || 'SHA-256'));
    }
    return out;
  }

  function decodeEntities(t) {
    return t
      .replace(/&nbsp;/gi, ' ')
      .replace(/&amp;/gi, '&')
      .replace(/&lt;/gi, '<')
      .replace(/&gt;/gi, '>')
      .replace(/&quot;/gi, '"')
      .replace(/&#0*39;|&apos;/gi, "'")
      .replace(/&#x([0-9a-f]+);/gi, function (_, h) {
        try { return String.fromCodePoint(parseInt(h, 16)); } catch (e) { return _; }
      })
      .replace(/&#(\d+);/g, function (_, d) {
        try { return String.fromCodePoint(parseInt(d, 10)); } catch (e) { return _; }
      });
  }

  function textExtract(payload) {
    return decodeEntities(String(payload == null ? '' : payload)
      .replace(/<script[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')
      .replace(/<[^>]+>/g, ' '))
      .replace(/\s+/g, ' ')
      .trim();
  }

  function htmlToText(payload) {
    var t = String(payload == null ? '' : payload)
      .replace(/<(script|style)[\s\S]*?<\/\1>/gi, ' ')
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<\/(p|div|li|tr|h[1-6]|section|article|blockquote|table|ul|ol)\s*>/gi, '\n')
      .replace(/<[^>]+>/g, ' ');
    t = decodeEntities(t);
    return t
      .replace(/[ \t\f\v]+/g, ' ')
      .replace(/ *\n */g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function wordStats(payload) {
    var t = String(payload == null ? '' : (typeof payload === 'object' ? (payload.text || '') : payload));
    var words = t.match(/[\p{L}\p{N}]+(?:['’\-][\p{L}\p{N}]+)*/gu) || [];
    var uniq = new Set();
    for (var i = 0; i < words.length; i++) uniq.add(words[i].toLowerCase());
    var sentences = (t.match(/[^.!?…]+[.!?…]+/g) || []).length;
    return {
      chars: t.length,
      chars_no_spaces: t.replace(/\s+/g, '').length,
      words: words.length,
      unique_words: uniq.size,
      lines: t ? t.split(/\r\n|\r|\n/).length : 0,
      sentences: sentences
    };
  }

  function textSnippet(payload) {
    var text = String((payload && payload.text) || '');
    var q = String((payload && payload.query) || '');
    var radius = Math.max(20, Math.min(200, parseInt((payload && payload.radius) || 60, 10) || 60));
    var max = Math.max(1, Math.min(20, parseInt((payload && payload.max) || 5, 10) || 5));
    if (!q) return [];
    var low = text.toLowerCase(), ql = q.toLowerCase();
    var out = [], from = 0;
    while (out.length < max) {
      var i = low.indexOf(ql, from);
      if (i < 0) break;
      var s = Math.max(0, i - radius);
      var e = Math.min(text.length, i + q.length + radius);
      out.push({
        start: i, end: i + q.length,
        text: (s > 0 ? '…' : '') + text.slice(s, e) + (e < text.length ? '…' : '')
      });
      from = i + q.length;
    }
    return out;
  }

  function searchFilter(payload) {
    var items = (payload && payload.items) || [];
    var q = String((payload && payload.query) || '').toLowerCase();
    var fields = (payload && payload.fields && payload.fields.length)
      ? payload.fields : ['name', 'title', 'description'];
    if (!q) return items;
    return items.filter(function (it) {
      for (var i = 0; i < fields.length; i++) {
        var v = it && it[fields[i]];
        if (v != null && String(v).toLowerCase().indexOf(q) !== -1) return true;
      }
      return false;
    });
  }

  function parseCsv(text, delim) {
    var rows = [], row = [], cur = '', quoted = false, i = 0;
    while (i < text.length) {
      var c = text[i];
      if (quoted) {
        if (c === '"') {
          if (text[i + 1] === '"') { cur += '"'; i += 2; continue; }
          quoted = false; i++; continue;
        }
        cur += c; i++; continue;
      }
      if (c === '"') { quoted = true; i++; continue; }
      if (c === delim) { row.push(cur); cur = ''; i++; continue; }
      if (c === '\n' || c === '\r') {
        row.push(cur); rows.push(row); row = []; cur = '';
        if (c === '\r' && text[i + 1] === '\n') i++;
        i++; continue;
      }
      cur += c; i++;
    }
    if (cur !== '' || row.length) { row.push(cur); rows.push(row); }
    while (rows.length && rows[rows.length - 1].length === 1 && rows[rows.length - 1][0] === '') {
      rows.pop();
    }
    return rows;
  }

  function csvToJson(payload) {
    var text = String((payload && payload.text) || '');
    var delim = String((payload && payload.delimiter) || ',').slice(0, 1) || ',';
    var header = !(payload && payload.header === false);
    var rows = parseCsv(text, delim);
    if (!rows.length) return { count: 0, rows: [] };
    if (!header) return { count: rows.length, rows: rows };
    var cols = rows[0].map(function (c) { return String(c).trim(); });
    var out = [];
    for (var r = 1; r < rows.length; r++) {
      var obj = {};
      for (var c = 0; c < cols.length; c++) obj[cols[c] || ('col' + c)] = rows[r][c] == null ? '' : rows[r][c];
      out.push(obj);
    }
    return { count: out.length, columns: cols, rows: out };
  }

  function jsonPretty(payload) {
    var value = payload && typeof payload === 'object' ? payload.text : payload;
    if (typeof value === 'string') value = JSON.parse(value);
    var indent = Math.max(0, Math.min(8, parseInt((payload && payload.indent) || 2, 10) || 2));
    return JSON.stringify(value, null, indent);
  }

  function jsonText(payload) {
    return payload && typeof payload === 'object' ? payload.text : payload;
  }

  function jsonMinify(payload) {
    return JSON.stringify(JSON.parse(jsonText(payload)));
  }

  // Задача «проверить JSON» отвечает объектом, а не исключением: сервер просит
  // именно узнать, годится ли текст, и «не годится» — это успех задачи.
  function jsonValidate(payload) {
    var t = jsonText(payload);
    if (typeof t !== 'string') t = String(t == null ? '' : t);
    try {
      var v = JSON.parse(t);
      var kind = Array.isArray(v) ? 'array' : (v === null ? 'null' : typeof v);
      return { ok: true, type: kind, chars: t.length,
               length: kind === 'array' ? v.length : (kind === 'object' ? Object.keys(v).length : null) };
    } catch (err) {
      return { ok: false, type: '', chars: t.length,
               error: String((err && err.message) || err).slice(0, 200) };
    }
  }

  function base64Encode(payload) {
    var text = typeof payload === 'string' ? payload : String((payload && payload.text) || '');
    return bufToB64(new TextEncoder().encode(text));
  }

  function base64Decode(payload) {
    var b64 = typeof payload === 'string' ? payload : String((payload && payload.base64) || '');
    return new TextDecoder().decode(b64ToBuf(b64));
  }

  async function gzipCompress(payload) {
    if (typeof CompressionStream === 'undefined') throw new Error('CompressionStream недоступен');
    var text = typeof payload === 'string' ? payload : String((payload && payload.text) || '');
    var stream = new Blob([new TextEncoder().encode(text)]).stream()
      .pipeThrough(new CompressionStream('gzip'));
    var buf = await new Response(stream).arrayBuffer();
    return { bytes: buf.byteLength, base64: bufToB64(new Uint8Array(buf)) };
  }

  async function gzipDecompress(payload) {
    if (typeof DecompressionStream === 'undefined') throw new Error('DecompressionStream недоступен');
    var b64 = typeof payload === 'string' ? payload : String((payload && payload.base64) || '');
    var stream = new Blob([b64ToBuf(b64)]).stream()
      .pipeThrough(new DecompressionStream('gzip'));
    var buf = await new Response(stream).arrayBuffer();
    return { bytes: buf.byteLength, text: new TextDecoder().decode(buf) };
  }

  async function loadBitmap(payload) {
    if (typeof OffscreenCanvas === 'undefined') throw new Error('OffscreenCanvas недоступен');
    var dataUrl = payload && payload.dataUrl;
    if (!dataUrl) throw new Error('dataUrl required');
    var blob = await (await fetch(dataUrl)).blob();
    return await createImageBitmap(blob);
  }

  async function encodeCanvas(cv, payload, mime, quality) {
    var type = mime || (payload && payload.mime) || 'image/jpeg';
    var q = Math.max(0.1, Math.min(1,
      parseFloat((payload && payload.quality) || quality || 0.85) || 0.85));
    var out = await cv.convertToBlob({ type: type, quality: q });
    if (out.type !== type) throw new Error(type + ' не поддерживается устройством');
    return {
      width: cv.width, height: cv.height, type: out.type, bytes: out.size,
      dataUrl: await blobToDataUrl(out)
    };
  }

  async function resizeImage(payload, thumb, mime, quality) {
    var maxSide = Math.max(16, Math.min(4096,
      parseInt((payload && payload.max) || (thumb ? 256 : 1280), 10) || (thumb ? 256 : 1280)));
    var bmp = await loadBitmap(payload);
    var scale = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
    var w = Math.max(1, Math.round(bmp.width * scale));
    var h = Math.max(1, Math.round(bmp.height * scale));
    var cv = new OffscreenCanvas(w, h);
    cv.getContext('2d').drawImage(bmp, 0, 0, w, h);
    if (bmp.close) bmp.close();
    return await encodeCanvas(cv, payload, mime, quality);
  }

  // Перекодирование без изменения размера: сервер отдаёт помощнику задачу
  // «сделай из этого WebP/ JPEG той же величины». Масштаб здесь намеренно
  // не трогается — иначе это был бы тот же image_resize под другим именем.
  async function reencodeImage(payload) {
    var bmp = await loadBitmap(payload);
    var cv = new OffscreenCanvas(bmp.width, bmp.height);
    cv.getContext('2d').drawImage(bmp, 0, 0);
    if (bmp.close) bmp.close();
    return await encodeCanvas(cv, payload, payload && payload.mime, 0.9);
  }

  async function transformImage(payload, angle, flipX, flipY) {
    var bmp = await loadBitmap(payload);
    var a = ((parseInt(angle, 10) || 0) % 360 + 360) % 360;
    if (a % 90 !== 0) throw new Error('поворот только на 90/180/270');
    var swap = (a === 90 || a === 270);
    var w = swap ? bmp.height : bmp.width;
    var h = swap ? bmp.width : bmp.height;
    var cv = new OffscreenCanvas(w, h);
    var ctx = cv.getContext('2d');
    ctx.translate(w / 2, h / 2);
    if (a) ctx.rotate(a * Math.PI / 180);
    // Отражение после поворота: «отразить по горизонтали» значит по экранной
    // горизонтали, а не по оси исходного файла.
    if (flipX || flipY) ctx.scale(flipX ? -1 : 1, flipY ? -1 : 1);
    ctx.drawImage(bmp, -bmp.width / 2, -bmp.height / 2);
    if (bmp.close) bmp.close();
    return await encodeCanvas(cv, payload, payload && payload.mime, 0.9);
  }

  // Пиксели читаются напрямую: в Worker нет ни DOM, ни CSS-фильтров, а
  // drawImage с globalCompositeOperation не даёт точных оттенков серого.
  async function grayscaleImage(payload) {
    var bmp = await loadBitmap(payload);
    var cv = new OffscreenCanvas(bmp.width, bmp.height);
    var ctx = cv.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(bmp, 0, 0);
    if (bmp.close) bmp.close();
    var img = ctx.getImageData(0, 0, cv.width, cv.height);
    var d = img.data;
    for (var i = 0; i < d.length; i += 4) {
      var y = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
      d[i] = d[i + 1] = d[i + 2] = y;
    }
    ctx.putImageData(img, 0, 0);
    return await encodeCanvas(cv, payload, payload && payload.mime, 0.9);
  }

  async function imageMeta(payload) {
    var bmp = await loadBitmap(payload);
    var meta = { width: bmp.width, height: bmp.height };
    if (bmp.close) bmp.close();
    var blob = await (await fetch(payload.dataUrl)).blob();
    meta.type = blob.type || '';
    meta.bytes = blob.size || 0;
    return meta;
  }

  self.onmessage = async function (e) {
    var msg = e.data || {};
    var id = msg.id, type = msg.type, payload = msg.payload;
    var t0 = (self.performance && performance.now) ? performance.now() : Date.now();
    try {
      var result;
      switch (type) {
        case 'hash_sha256': result = await hashSha256(payload); break;
        case 'hash_sha1': result = await hashSha1(payload); break;
        case 'hash_sha512': result = await hashSha512(payload); break;
        case 'hash_batch': result = await hashBatch(payload); break;
        case 'text_extract': result = textExtract(payload); break;
        case 'html_to_text': result = htmlToText(payload); break;
        case 'word_stats': result = wordStats(payload); break;
        case 'text_snippet': result = textSnippet(payload); break;
        case 'search_filter': result = searchFilter(payload); break;
        case 'csv_to_json': result = csvToJson(payload); break;
        case 'json_pretty': result = jsonPretty(payload); break;
        case 'json_minify': result = jsonMinify(payload); break;
        case 'json_validate': result = jsonValidate(payload); break;
        case 'base64_encode': result = base64Encode(payload); break;
        case 'base64_decode': result = base64Decode(payload); break;
        case 'gzip_compress': result = await gzipCompress(payload); break;
        case 'gzip_decompress': result = await gzipDecompress(payload); break;
        case 'image_resize': result = await resizeImage(payload, false); break;
        case 'image_thumbnail': result = await resizeImage(payload, true); break;
        case 'image_webp': result = await resizeImage(payload, false, 'image/webp', 0.85); break;
        case 'image_meta': result = await imageMeta(payload); break;
        case 'image_reencode': result = await reencodeImage(payload); break;
        case 'image_rotate': result = await transformImage(payload, (payload || {}).angle, false, false); break;
        case 'image_flip':
          var axis = String((payload && payload.axis) || 'h').toLowerCase();
          result = await transformImage(payload, 0, axis === 'h' || axis === 'both',
                                        axis === 'v' || axis === 'both');
          break;
        case 'image_grayscale': result = await grayscaleImage(payload); break;
        default: throw new Error('unknown task: ' + type);
      }
      var ms = ((self.performance && performance.now) ? performance.now() : Date.now()) - t0;
      self.postMessage({ id: id, ok: true, result: result, ms: ms });
    } catch (err) {
      var ms2 = ((self.performance && performance.now) ? performance.now() : Date.now()) - t0;
      self.postMessage({ id: id, ok: false, error: String((err && err.message) || err), ms: ms2 });
    }
  };
})();
