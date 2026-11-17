import express from 'express';
import multer from 'multer';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { fileURLToPath } from 'url';
import http from 'http';
import createTorrent from 'create-torrent';
import parseTorrent, { toMagnetURI } from 'parse-torrent';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PORT = parseInt(process.env.TORRENT_HELPER_PORT || '5050');
const DEFAULT_OUTPUT = process.env.TORRENT_OUTPUT_DIR || process.cwd();

const app = express();
const upload = multer({ dest: os.tmpdir ? os.tmpdir() : '/tmp' });

let torrentsMap = {};

// ─── Lazy WebTorrent client ───────────────────────────────────────────────
let _wtClient = null;
let _wtLoading = null;

async function getClient() {
  if (_wtClient) return _wtClient;
  if (_wtLoading) return _wtLoading;
  _wtLoading = (async () => {
    console.log('Loading WebTorrent...');
    const wt = await import('webtorrent');
    const WebTorrent = wt.default || wt;
    console.log('WebTorrent loaded, creating client...');
    const client = new WebTorrent();

    client.on('error', (err) => console.error('WebTorrent error:', err.message));

    client.on('torrent', (torrent) => {
      const t = torrentsMap[torrent.infoHash];
      if (t) {
        t.meta = getTorrentMeta(torrent);
        t.name = torrent.name;
      }

      torrent.on('done', () => {
        console.log(`Torrent done: ${torrent.name}`);
        if (torrentsMap[torrent.infoHash]) {
          torrentsMap[torrent.infoHash].complete = true;
        }
        notifyPython('done', torrent.infoHash, torrent.name);
      });

      torrent.on('error', (err) => {
        console.error(`Torrent ${torrent.infoHash} error:`, err.message);
      });
    });

    _wtClient = client;
    console.log('WebTorrent client ready');
    return client;
  })();
  return _wtLoading;
}

// ─── Middleware ────────────────────────────────────────────────────────────
app.use(express.json({ limit: '50mb' }));
app.use(express.urlencoded({ extended: true, limit: '50mb' }));

app.use((req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
  res.header('Access-Control-Allow-Headers', 'Content-Type,Authorization');
  if (req.method === 'OPTIONS') return res.sendStatus(200);
  next();
});

// ─── Routes ────────────────────────────────────────────────────────────────

app.post('/add', upload.single('torrent_file'), async (req, res) => {
  try {
    const { magnet, type, output_dir, name, file_base64 } = req.body;
    const outputPath = output_dir || DEFAULT_OUTPUT;
    const contentType = type || 'video';
    const torrentName = name || '';

    if (!magnet && !req.file && !file_base64) {
      return res.status(400).json({ error: 'No magnet link or torrent file provided' });
    }

    fs.mkdirSync(outputPath, { recursive: true });

    let torrentId = magnet;
    if (!torrentId && file_base64) {
      const tmpPath = path.join(os.tmpdir(), `torrent_${Date.now()}.torrent`);
      fs.writeFileSync(tmpPath, Buffer.from(file_base64, 'base64'));
      torrentId = tmpPath;
    }
    if (!torrentId && req.file) {
      torrentId = req.file.path;
    }

    const client = await getClient();
    const existing = client.torrents.find(t => t.infoHash && torrentId.includes(t.infoHash));
    if (existing) {
      return res.json({
        infoHash: existing.infoHash,
        name: existing.name,
        message: 'Torrent already active'
      });
    }

    const m = (magnet || '').match(/[?&]xt=urn:btih:([0-9a-fA-F]{40})/);
    const pendingKey = (m ? m[1].toLowerCase() : '') || 'pending';

    const torrent = client.add(torrentId, { path: outputPath }, (t) => {
      const infoHash = t.infoHash;
      if (infoHash && infoHash !== pendingKey) {
        delete torrentsMap[pendingKey];
      }
      torrentsMap[infoHash] = {
        infoHash,
        name: t.name,
        type: contentType,
        outputPath,
        addedAt: Date.now(),
        torrent: t,
        meta: getTorrentMeta(t),
        complete: false,
        moved: false
      };
      console.log(`Torrent added: ${t.name} (${infoHash}) -> ${outputPath}`);
      notifyPython('add', infoHash, t.name);
    });

    torrent.on('error', (err) => {
      console.error('Torrent error:', err.message);
    });

    const infoHash = (m ? m[1].toLowerCase() : '') || torrent.infoHash || pendingKey;
    torrentsMap[infoHash] = {
      infoHash,
      name: torrentName || torrent.name || 'Loading...',
      type: contentType,
      outputPath,
      addedAt: Date.now(),
      torrent,
      meta: null,
      complete: false,
      moved: false
    };

    res.json({
      infoHash,
      name: torrentName || torrent.name || 'Loading...',
      type: contentType,
      message: 'Torrent added'
    });
  } catch (err) {
    console.error('Add error:', err);
    res.status(500).json({ error: err.message });
  }
});

function safeGet(fn, fallback) {
  try { return fn(); } catch (e) { return fallback; }
}

/* Названия в торрентах иногда хранятся в UTF-8, но читаются как Windows-1252
   («ÐœÑƒÐ·Ñ‹ÐºÐ°» вместо «Музыка»). Восстанавливаем байты через таблицу CP1252
   и декодируем как UTF-8; применяем только если кириллицы стало больше. */
const CP1252_SPECIAL = {
  '\u20AC': 0x80, '\u201A': 0x82, '\u0192': 0x83, '\u201E': 0x84, '\u2026': 0x85,
  '\u2020': 0x86, '\u2021': 0x87, '\u02C6': 0x88, '\u2030': 0x89, '\u0160': 0x8A,
  '\u2039': 0x8B, '\u0152': 0x8C, '\u017D': 0x8E, '\u2018': 0x91, '\u2019': 0x92,
  '\u201C': 0x93, '\u201D': 0x94, '\u2022': 0x95, '\u2013': 0x96, '\u2014': 0x97,
  '\u02DC': 0x98, '\u2122': 0x99, '\u0161': 0x9A, '\u203A': 0x9B, '\u0153': 0x9C,
  '\u017E': 0x9E, '\u0178': 0x9F
};
function toCp1252Bytes(s) {
  const out = Buffer.alloc(s.length);
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (CP1252_SPECIAL[ch] !== undefined) { out[i] = CP1252_SPECIAL[ch]; continue; }
    const code = ch.charCodeAt(0);
    if (code <= 0xFF) { out[i] = code; continue; }
    return null;
  }
  return out;
}
function fixMojibakeName(s) {
  if (typeof s !== 'string' || !s || /^[\x00-\x7F]*$/.test(s)) return s;
  const raw = toCp1252Bytes(s);
  if (!raw) return s;
  const fixed = raw.toString('utf8');
  const cyr = (x) => (x.match(/[\u0400-\u04FF]/g) || []).length;
  return fixed && cyr(fixed) > cyr(s) ? fixed : s;
}

app.get('/list', async (req, res) => {
  const list = Object.values(torrentsMap).filter(t => t.torrent && !t.torrent.destroyed).map(t => {
    const status = {
      progress: safeGet(() => t.torrent.progress, 0),
      downloadSpeed: safeGet(() => t.torrent.downloadSpeed, 0),
      uploadSpeed: safeGet(() => t.torrent.uploadSpeed, 0),
      numPeers: safeGet(() => t.torrent.numPeers, 0),
      downloaded: safeGet(() => t.torrent.downloaded, 0),
      totalLength: safeGet(() => t.torrent.length, 0),
      timeRemaining: safeGet(() => t.torrent.timeRemaining, null),
      files: safeGet(() => t.torrent.files ? t.torrent.files.map((f, i) => ({
        name: fixMojibakeName(f.name),
        length: f.length,
        path: fixMojibakeName(f.path),
        index: i,
        downloaded: safeGet(() => f.downloaded, 0)
      })) : [], [])
    };
    return {
      infoHash: t.infoHash,
      name: fixMojibakeName(t.name),
      type: t.type,
      outputPath: t.outputPath,
      addedAt: t.addedAt,
      complete: t.complete,
      moved: t.moved,
      ...status
    };
  });
  res.json({ torrents: list });
});

app.get('/status/:infoHash', (req, res) => {
  const t = torrentsMap[req.params.infoHash];
  if (!t) return res.status(404).json({ error: 'Torrent not found' });

  const status = {
    progress: safeGet(() => t.torrent.progress, 0),
    downloadSpeed: safeGet(() => t.torrent.downloadSpeed, 0),
    uploadSpeed: safeGet(() => t.torrent.uploadSpeed, 0),
    numPeers: safeGet(() => t.torrent.numPeers, 0),
    downloaded: safeGet(() => t.torrent.downloaded, 0),
    totalLength: safeGet(() => t.torrent.length, 0),
    timeRemaining: safeGet(() => t.torrent.timeRemaining, null),
    files: safeGet(() => t.torrent.files.map((f, i) => ({
      name: fixMojibakeName(f.name),
      length: f.length,
      path: fixMojibakeName(f.path),
      index: i,
      downloaded: safeGet(() => f.downloaded, 0)
    })), [])
  };

  res.json({
    infoHash: t.infoHash,
    name: fixMojibakeName(t.name),
    type: t.type,
    outputPath: t.outputPath,
    complete: t.complete,
    moved: t.moved,
    ...status
  });
});

/* WebTorrent раскладывает куски по принципу «редчайший сначала», а
   FileIterator помечает критическими только ~2 куска прямо перед плеером.
   На торренте из сотен файлов кусок, нужный через секунду после начала
   чтения, стоит в общей очереди: HTTP-ответ молчит, буфер <audio> пустеет,
   и звук «перепрыгивает». Здесь окно в 16 МБ от позиции плеера помечается
   критическим — такие куски движок может забрать у медленного пира (hotswap)
   и запросить заранее. Флаги только добавляют приоритет уже существующей
   выборке: сами выборки и загрузка остальных файлов остаются как были. */
const STREAM_LOOKAHEAD = 16 * 1024 * 1024;

/* У webtorrent 3.0.16 `_critical` только включается (torrent.critical) и не
   снимается — при перемотке окно накапливается. Цена ограничена: флаг всего
   лишь разрешает hotswap (куска можно лишить медленный пир), очерёдность
   файлов и сами выборки он не меняет. Окно держим узким (16 МБ ≈ 70 с
   звука), чтобы накопление за долгий сеанс осталось мелочью. */
function markStreamWindow(file, startByte, endByte) {
  try {
    const torrent = file._torrent;
    if (!torrent || torrent.destroyed) return;
    const pieceLength = torrent.pieceLength;
    const total = torrent.numPieces || (torrent.pieces ? torrent.pieces.length : 0);
    if (!pieceLength || !total) return;
    const first = Math.floor((file.offset + startByte) / pieceLength);
    const ahead = Math.floor((file.offset + startByte + STREAM_LOOKAHEAD) / pieceLength);
    const throughEnd = Math.floor((file.offset + endByte) / pieceLength);
    const last = Math.min(ahead, throughEnd, total - 1);
    if (last >= first) torrent.critical(first, last);
  } catch (e) {
    // приоритет не важнее самого потока: срыв здесь не должен ронять ответ
  }
}

/* Измерение «перепрыгивания». Ответ хелпера идёт в stderr, который
   основной сервер пишет в свой лог (server/torrent.py), поэтому строки
   видно в логе Infinity без отдельного окна.
     [stream-req]  — сколько range-запросов делает плеер: второй запрос на
                     перекрывающуюся зону означает, что он сам ищет заново;
     [stream-stall] — пауза в поставке дольше 2 с: если скорость загрузки
                     ниже скорости чтения, значит дело в рое, а не в файле. */
function attachStreamMeter(stream, label, torrent, file) {
  let delivered = 0, lastAt = Date.now(), startedAt = Date.now(), reported = 0;
  const readyOf = () => {
    try {
      const p = typeof file.progress === 'function' ? file.progress() : file.progress;
      return (p * 100).toFixed(1) + '%';
    } catch (e) { return '?'; }
  };
  stream.on('data', chunk => {
    const now = Date.now(), gap = now - lastAt;
    delivered += (chunk && chunk.length) || 0;
    lastAt = now;
    if (gap > 2000 && delivered - reported > 0) {
      reported = delivered;
      console.error(`[stream-stall] ${label} пауза=${Math.round(gap / 1000)}с через ${Math.round((now - startedAt) / 1000)}с, ` +
        `отдано=${(delivered / 1048576).toFixed(1)} МБ, нужно к этому моменту=${(Math.round((now - startedAt) / 1000 * 40) / 1024).toFixed(1)} МБ (320 кбит/с), ` +
        `пиры=${torrent.numPeers}, загрузка=${(torrent.downloadSpeed / 1024).toFixed(0)} КБ/с, ` +
        `скачано торрент=${(torrent.downloaded / 1048576).toFixed(1)} МБ, готовность файла=${readyOf()}`);
    }
  });
  stream.on('end', () => {
    console.error(`[stream-done] ${label}: отдано=${(delivered / 1048576).toFixed(1)} МБ за ${Math.round((Date.now() - startedAt) / 1000)}с`);
  });
}

app.get('/stream/:infoHash/:fileIndex', (req, res) => {
  const t = torrentsMap[req.params.infoHash];
  if (!t || !t.torrent) return res.status(404).json({ error: 'Torrent not found' });

  const fileIndex = parseInt(req.params.fileIndex);
  const file = t.torrent.files[fileIndex];
  if (!file) return res.status(404).json({ error: 'File not found' });

  const range = req.headers.range;
  const fileSize = file.length;
  const label = `${req.params.infoHash.slice(0, 8)}/${fileIndex}`;

  let readyPct = '?';
  try {
    const p = typeof file.progress === 'function' ? file.progress() : file.progress;
    readyPct = (p * 100).toFixed(1) + '%';
  } catch (e) {}
  console.error(`[stream-req] ${label}: range=${range || 'весь файл'}, файл=${(fileSize / 1048576).toFixed(1)} МБ, ` +
    `готов=${readyPct}, пиры=${t.torrent.numPeers}, загрузка=${(t.torrent.downloadSpeed / 1024).toFixed(0)} КБ/с`);

  if (range) {
    const m = /bytes=(\d*)-(\d*)/.exec(range);
    if (!m || (m[1] === '' && m[2] === '')) {
      // Некорректный Range — отдаём файл целиком.
      markStreamWindow(file, 0, fileSize - 1);
      const stream = file.createReadStream();
      attachStreamMeter(stream, label, t.torrent, file);
      res.writeHead(200, {
        'Content-Length': fileSize,
        'Content-Type': getContentType(file.name),
        'Accept-Ranges': 'bytes'
      });
      stream.on('error', () => { if (!res.headersSent) res.status(500).end(); else res.destroy(); });
      res.on('close', () => { try { stream.destroy(); } catch (e) {} });
      return stream.pipe(res);
    }
    let start, end;
    if (m[1] === '') {
      // Суффиксный диапазон «bytes=-N»: последние N байт.
      start = Math.max(0, fileSize - parseInt(m[2], 10));
      end = fileSize - 1;
    } else {
      start = parseInt(m[1], 10);
      end = m[2] !== '' ? parseInt(m[2], 10) : fileSize - 1;
    }
    // Клампим конец: иначе FileIterator ждёт несуществующий кусок и поток
    // обрывается — плеер «перескакивает» на новую позицию.
    end = Math.min(end, fileSize - 1);
    if (!isFinite(start) || !isFinite(end) || start < 0 || start > end) {
      res.writeHead(416, { 'Content-Range': `bytes */${fileSize}`, 'Accept-Ranges': 'bytes' });
      return res.end();
    }
    const chunkSize = (end - start) + 1;
    markStreamWindow(file, start, end);
    const stream = file.createReadStream({ start, end });
    attachStreamMeter(stream, label, t.torrent, file);
    res.writeHead(206, {
      'Content-Range': `bytes ${start}-${end}/${fileSize}`,
      'Accept-Ranges': 'bytes',
      'Content-Length': chunkSize,
      'Content-Type': getContentType(file.name)
    });
    stream.on('error', () => { if (!res.headersSent) res.status(500).end(); else res.destroy(); });
    // Браузер прервал поток (seek/пауза) — освобождаем выборку кусков.
    res.on('close', () => { try { stream.destroy(); } catch (e) {} });
    stream.pipe(res);
  } else {
    res.writeHead(200, {
      'Content-Length': fileSize,
      'Content-Type': getContentType(file.name),
      'Accept-Ranges': 'bytes'
    });
    markStreamWindow(file, 0, fileSize - 1);
    const stream = file.createReadStream();
    attachStreamMeter(stream, label, t.torrent, file);
    stream.on('error', () => { if (!res.headersSent) res.status(500).end(); else res.destroy(); });
    res.on('close', () => { try { stream.destroy(); } catch (e) {} });
    stream.pipe(res);
  }
});

app.post('/remove/:infoHash', (req, res) => {
  const t = torrentsMap[req.params.infoHash];
  if (!t) return res.status(404).json({ error: 'Torrent not found' });

  if (t.torrent) {
    t.torrent.destroy();
  }
  delete torrentsMap[req.params.infoHash];
  res.json({ message: 'Torrent removed' });
});

app.post('/moved/:infoHash', (req, res) => {
  const t = torrentsMap[req.params.infoHash];
  if (!t) return res.status(404).json({ error: 'Torrent not found' });
  t.moved = true;
  res.json({ message: 'Marked as moved' });
});

app.post('/pause/:infoHash', (req, res) => {
  const t = torrentsMap[req.params.infoHash];
  if (!t) return res.status(404).json({ error: 'Torrent not found' });
  if (t.torrent && !t.torrent.destroyed) {
    t.torrent.pause();
  }
  res.json({ infoHash: t.infoHash, paused: true });
});

app.post('/resume/:infoHash', (req, res) => {
  const t = torrentsMap[req.params.infoHash];
  if (!t) return res.status(404).json({ error: 'Torrent not found' });
  if (t.torrent && !t.torrent.destroyed) {
    t.torrent.resume();
  }
  res.json({ infoHash: t.infoHash, paused: false });
});

// ─── Torrent creation (hash existing files into .torrent) ───────────────────
// TODO: remove unused parseTorrent import if magnet build changes
let createTasks = {};
let createTaskSeq = 1;

app.post('/create-torrent', (req, res) => {
  const { files, name, pieceLength } = req.body || {};
  if (!Array.isArray(files) || files.length === 0) {
    return res.status(400).json({ error: 'No files provided' });
  }

  const taskId = `ct_${Date.now()}_${createTaskSeq++}`;
  const task = {
    id: taskId,
    files: files.slice(),
    name: name || '',
    percent: 0,
    done: false,
    error: null,
    infoHash: null,
    magnetUri: null,
    torrentFileBase64: null,
    resultName: null,
    startedAt: Date.now()
  };
  createTasks[taskId] = task;

  const opts = {
    name: name || null,
    onProgress: (hashed, total) => {
      const pct = total > 0 ? Math.min(99, Math.round((hashed / total) * 100)) : 0;
      task.percent = pct;
    }
  };
  if (pieceLength) opts.pieceLength = pieceLength;

  Promise.resolve().then(() => {
    createTorrent(task.files, opts, async (err, torrentBuf) => {
      if (err) {
        task.error = err.message || String(err);
        task.done = true;
        console.error('Create-torrent error:', task.error);
        return;
      }
      try {
        const parsed = await parseTorrent(torrentBuf);
        const infoHash = parsed.infoHash;
        const resultName = parsed.name || name || '';
        const magnetUri = toMagnetURI(parsed);
        task.infoHash = infoHash;
        task.magnetUri = magnetUri;
        task.torrentFileBase64 = Buffer.from(torrentBuf).toString('base64');
        task.result = resultName;
        task.percent = 100;
        task.done = true;
        console.log(`[create-torrent] done: ${task.result} (${infoHash})`);
      } catch (e) {
        task.error = e.message || String(e);
        task.done = true;
        console.error('[create-torrent] parse error:', task.error);
      }
    });
  });

  res.json({ taskId, message: 'Torrent creation started' });
});

app.get('/create-torrent/:taskId/status', (req, res) => {
  const task = createTasks[req.params.taskId];
  if (!task) return res.status(404).json({ error: 'Task not found' });
  res.json({
    taskId: task.id,
    percent: task.percent,
    done: task.done,
    error: task.error,
    infoHash: task.infoHash,
    magnetUri: task.magnetUri,
    torrentFileBase64: task.torrentFileBase64,
    name: task.result || task.name,
    files: task.files
  });
});

app.post('/create-torrent/:taskId/clear', (req, res) => {
  const task = createTasks[req.params.taskId];
  if (!task) return res.status(404).json({ error: 'Task not found' });
  delete createTasks[req.params.taskId];
  res.json({ message: 'Task cleared' });
});

app.get('/health', (req, res) => res.json({ status: 'ok', pid: process.pid, torrents: Object.keys(torrentsMap).length }));

app.post('/limits', async (req, res) => {
  try {
    const { download, upload } = req.body || {};
    const client = await getClient();
    if (typeof download === 'number' && download >= 0) {
      client.throttleDownload(download);
    }
    if (typeof upload === 'number' && upload >= 0) {
      client.throttleUpload(upload);
    }
    res.json({ ok: true, download: download ?? null, upload: upload ?? null });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ─── Helpers ────────────────────────────────────────────────────────────────

function getTorrentMeta(torrent) {
  if (!torrent || !torrent.files) return null;
  return {
    name: torrent.name,
    length: torrent.length,
    files: torrent.files.map((f, i) => ({
      name: f.name,
      length: f.length,
      path: f.path,
      index: i
    }))
  };
}

function getContentType(filename) {
  const ext = path.extname(filename).toLowerCase();
  const types = {
    '.mp4': 'video/mp4',
    '.mkv': 'video/x-matroska',
    '.avi': 'video/x-msvideo',
    '.mov': 'video/quicktime',
    '.webm': 'video/webm',
    '.m4v': 'video/mp4',
    '.mp3': 'audio/mpeg',
    '.flac': 'audio/flac',
    '.wav': 'audio/wav',
    '.ogg': 'audio/ogg',
    '.m4a': 'audio/mp4',
    '.aac': 'audio/aac',
    '.wma': 'audio/x-ms-wma',
    '.opus': 'audio/opus',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.srt': 'text/plain',
    '.sub': 'text/plain',
    '.idx': 'text/plain',
    '.nfo': 'text/plain'
  };
  return types[ext] || 'application/octet-stream';
}

function notifyPython(action, infoHash, name) {
  const cbUrl = process.env.PYTHON_CALLBACK_URL;
  if (!cbUrl) return;
  const data = JSON.stringify({ action, infoHash, name });
  const urlObj = new URL(cbUrl);
  const req = http.request({
    hostname: urlObj.hostname,
    port: urlObj.port || 8080,
    path: urlObj.pathname || '/api/torrent/callback',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) }
  });
  req.write(data);
  req.end();
}

process.on('unhandledRejection', (reason) => {
  console.error('Unhandled Rejection:', reason);
});

process.on('uncaughtException', (err) => {
  console.error('Uncaught Exception:', err.message);
});

// ─── Start server ───────────────────────────────────────────────────────────
const server = app.listen(PORT, '127.0.0.1', () => {
  console.log(`Torrent helper listening on 127.0.0.1:${PORT}`);
});

server.on('error', (err) => {
  console.error('Server error:', err.message);
  process.exit(1);
});
