/* 18-agent-sync.js — страница агента и мини-окно (Shift+Space) работают как одно.
   ----------------------------------------------------------------------------
   Страница и палитра обращаются к одному шлюзу, но состояние держат порознь,
   поэтому получалось так:
     · сессии, начатые в палитре, не появлялись в сайдбаре страницы;
     · если уйти со страницы (или перезагрузить её) во время хода агента,
       вёрнувшись в сессию, не было видно ни ответа, ни работы инструментов —
       шлюз ход доканчивал, а интерфейс о нём больше не знал;
     · страница и палитра показывали разные диалоги.

   Здесь три независимые вещи, каждая безопасна при недоступном шлюзе:
     1) импорт сессий шлюза, которых нет в локальном состоянии (сайдбар);
     2) подхват незаконченного хода через GET /api/sessions/{id}/stream —
        шлюз отдаёт повторы событий, поэтому ответ дорисовывается как живой;
     3) общие ключи aibar.active.* / aibar.rev.* — те же, что у палитры,
        чтобы окна переключались на один и тот же разговор.

   Локальные сессии страницы остаются главными для самих себя: история с
   диска дописывается только «хвостом» и только текстовыми сообщениями,
   карточки инструментов и организация (папки, закладки) не перетираются. */
(function () {
  'use strict';
  if (typeof state === 'undefined' || typeof ui === 'undefined' ||
      typeof API === 'undefined' || typeof Gateway === 'undefined' ||
      !state.sessions) return;                      // не страница агента

  var REV = 'aibar.rev.';
  var ACTIVE = 'aibar.active.';
  var PERSONA = 'aibar.persona.';
  var GONE = 'agent.sync.gone';                     // удалённые локально id шлюза
  var POLL_MS = 20000;
  var MAX_IMPORT = 40;                              // за один проход
  var ATTACH_TRIES = 3;                             // попыток подхвата за один заход
  var ATTACH_STALL_MS = 25000;                      // 2.5 keepalive-интервала бэкенда (10 с)

  var syncBusy = false;
  var booted = false;
  var pendingSwitch = '';                           // мини-окно выбрало сессию
  var attached = {};                                // backendId -> run_id
  var attaching = {};                               // backendId -> идёт подхват
  var tried = {};                                   // backendId -> время попытки
  var gone = readGone();
  /* Место, до которого ход уже показан. Переживает переподключение: бэкенду
     уходит last_seq, и повтор уже показанных дельт не задваивает текст. */
  var attachSeq = {};                               // backendId -> последний seq
  var attachRun = {};                               // backendId -> run_id

  function readGone() {
    try {
      var a = JSON.parse(localStorage.getItem(GONE) || '[]');
      return Array.isArray(a) ? a : [];
    } catch (e) { return []; }
  }
  function markGone(sid) {
    if (!sid || gone.indexOf(sid) >= 0) return;
    gone.push(sid);
    if (gone.length > 300) gone = gone.slice(-300);
    try { localStorage.setItem(GONE, JSON.stringify(gone)); } catch (e) { /* */ }
  }

  function appProfile() {
    try {
      var m = document.cookie.match(/(?:^|;\s*)infinity_profile=([^;]*)/);
      return m && m[1] ? decodeURIComponent(m[1]) : 'default';
    } catch (e) { return 'default'; }
  }
  function personaOf(prof) {
    try { return localStorage.getItem(PERSONA + prof) || 'default'; }
    catch (e) { return 'default'; }
  }
  function activeKey() {
    var p = appProfile();
    return ACTIVE + p + '.' + personaOf(p);
  }
  function revKey() {
    var p = appProfile();
    return REV + p + '.' + personaOf(p);
  }
  function bumpRev() {
    try { localStorage.setItem(revKey(), String(Date.now())); } catch (e) { /* */ }
  }

  function byBackend(sid) {
    return state.sessions.find(function (s) { return s.backendId === sid; }) || null;
  }
  function textMessages(s) {
    return (s.messages || []).filter(function (m) {
      return m.role === 'user' || (m.role === 'agent' && m.kind === 'text');
    });
  }
  function hash(s) {
    var h = 5381, i = String(s).length;
    while (i) h = (h * 33 ^ String(s).charCodeAt(--i)) >>> 0;
    return h.toString(36);
  }
  function contentText(c) {
    if (typeof c === 'string') return c;
    if (Array.isArray(c)) {
      return c.map(function (p) {
        if (!p || typeof p !== 'object') return '';
        if (p.type === 'text') return String(p.text || '');
        if (p.image_url || String(p.type || '').indexOf('image') >= 0) {
          /* Картинка переживает перезагрузку сессии: шлюз хранит её data-URL,
             а не только ярлык. Иначе ответ агента терял скриншот после resume. */
          var iu = p.image_url && (p.image_url.url || p.image_url) || p.url || p.dataUrl || '';
          if (!iu && p.source && p.source.data)
            iu = 'data:' + (p.source.media_type || 'image/png') + ';base64,' + p.source.data;
          if (typeof iu === 'string' && iu.indexOf('data:image/') === 0)
            return '![изображение](' + iu + ')';
          return '[изображение]';
        }
        return '';
      }).filter(Boolean).join('\n');
    }
    return c == null ? '' : String(c);
  }
  /* Сообщение шлюза -> форма интерфейса. Инструменты сюда не попадают:
     шлюз отдаёт текстовые user/assistant, а карточки вызовов у страницы свои. */
  function remoteMsg(m, i, sid) {
    if (!m || (m.role !== 'user' && m.role !== 'assistant')) return null;
    var text = contentText(m.content);
    if (!text.trim()) return null;
    var ts = Number(m.timestamp || m.ts || 0);
    if (ts && ts < 1e12) ts *= 1000;                // шлюз хранит секунды
    if (!ts) ts = Date.now();
    var id = m.id ? 'm_' + m.id :
      'm_r' + hash(String(m.role) + '|' + ts + '|' + text.slice(0, 120));
    return {
      id: id, ts: ts, status: 'done',
      role: m.role === 'user' ? 'user' : 'agent',
      kind: 'text', content: text,
      /* Durable row id: по нему «откат до сообщения» адресует запись в БД
         шлюза без поиска по тексту (локальный id у новых сообщений свой). */
      backendMsgId: m.id ? Number(m.id) || 0 : 0
    };
  }
  function tsOf(r) {
    var v = Number(r && (r.last_active || r.started_at || r.created_at) || 0);
    if (v && v < 1e12) v *= 1000;
    return v;
  }

  async function history(sid) {
    try {
      var d = await API.req('GET', '/api/sessions/' + encodeURIComponent(sid) +
                            '/messages?order=oldest&limit=200');
      var rows = (d && d.data) || [];
      var out = [];
      for (var i = 0; i < rows.length; i++) {
        var n = remoteMsg(rows[i], i, sid);
        if (n) out.push(n);
      }
      return out;
    } catch (e) { return []; }
  }

  /* Дописывает к сессии только те текстовые сообщения, которых в ней нет.
     Совпадение последнего локального сообщения ищется по вхождению, потому
     что агенту уходит текст с префиксами ([SYSTEM CONTEXT], [PLUGINS]),
     а в ленте сообщение показывается без них. */
  function sameText(a, b) {
    if (a === b) return true;
    if (!a || !b) return false;
    return a.indexOf(b) >= 0 || b.indexOf(a) >= 0;
  }
  /* Тот же разговор, но записанный по-разному: у пользователя в истории
     остаётся видимый текст, а шлюзу уходит он же с префиксами
     ([SYSTEM CONTEXT], [PLUGINS]) — поэтому для роли «пользователь»
     допускаем вхождение, для ответов агента — только точное совпадение.
     Иначе короткое «да» склеивалось бы с любым другим сообщением. */
  function sameMessage(local, remote) {
    if (!local || !remote || local.role !== remote.role) return false;
    if (local.content === remote.content) return true;
    if (local.role !== 'user') return false;
    if (!local.content || !remote.content) return false;
    return remote.content.indexOf(local.content) >= 0;
  }
  function appendTail(s, remote) {
    if (!remote.length) return 0;
    var local = textMessages(s);
    /* Якорь — последнее локальное текстовое сообщение: всё, что в истории
       шлюза идёт после него, — это пропущенный хвост. Если якорь не нашёлся
       (текст усечён или сообщение сохранилось не полностью), пробуем
       последний вопрос пользователя: ответы всегда идут после него. */
    var start = 0, found = false;
    function anchorAt(m) {
      for (var i = remote.length - 1; i >= 0; i--) {
        if (sameMessage(m, remote[i])) { start = i + 1; found = true; return; }
      }
    }
    if (local.length) anchorAt(local[local.length - 1]);
    if (!found) {
      for (var k = local.length - 1; k >= 0; k--) {
        if (local[k].role === 'user') { anchorAt(local[k]); break; }
      }
    }
    var added = 0;
    for (var j = start; j < remote.length; j++) {
      var rm = remote[j];
      if (s.messages.some(function (m) { return m.id === rm.id; })) continue;
      /* Даже без якоря не показываем второй раз то, что уже есть: иначе
         после перезагрузки в чате появлялись бы копии вопроса и ответа. */
      if (local.some(function (m) { return sameMessage(m, rm); })) continue;
      s.messages.push(rm);
      added++;
    }
    if (added) s.updatedAt = Date.now();
    return added;
  }

  function makeSession(r, msgs) {
    var base = 's_gw_' + String(r.id).replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 28);
    var id = base, n = 1;
    while (state.sessions.some(function (x) { return x.id === id; })) id = base + '_' + (++n);
    return {
      id: id,
      title: String(r.title || 'Сессия агента'),
      projectId: null, folderId: null,
      pinned: false, archived: false, locked: false,
      tags: [], bookmarks: [], reactions: {},
      createdAt: tsOf(r) || Date.now(),
      updatedAt: Date.now(),
      model: r.model || S().agent.model,
      mode: 'agent',
      messages: msgs,
      costIn: 0, costOut: 0,
      backendId: r.id
    };
  }

  /* Тот же разговор, у которого потерялся backendId (например, состояние
     сохранилось до создания сессии в шлюзе). Раньше хватало совпадения ПЕРВОГО
     вопроса, а sameText допускает вхождение: короткое «да» связывалось с любым
     разговором, где оно встретилось, — и чужие вызовы инструментов доезжали в
     чужую сессию. Теперь обязательно: первый вопрос сопоставим по длине (не
     короче 12 символов), у шлюза не меньше сообщений, чем локально, и КАЖДЫЙ
     локальный вопрос стоит на своём месте. Не совпало целиком — не склеиваем:
     лучше разговор будет виден отдельной записью, чем чужой ход попадёт в
     чужой чат. */
  function findTwin(msgs) {
    var theirs = msgs.filter(function (m) { return m.role === 'user'; });
    if (!theirs.length) return null;
    var best = null;
    state.sessions.forEach(function (s) {
      if (s.backendId) return;
      var mine = textMessages(s).filter(function (m) { return m.role === 'user'; });
      if (!mine.length || mine.length > theirs.length) return;
      var a = String(mine[0].content || '').trim();
      var b = String(theirs[0].content || '').trim();
      if (a.length < 12 || b.length < 12) return;
      if (!sameText(a, b)) return;
      for (var i = 0; i < mine.length; i++) {
        if (!sameMessage(mine[i], theirs[i])) return;
      }
      if (!best || mine.length > best.score) best = { s: s, score: mine.length };
    });
    return best ? best.s : null;
  }

  async function importGateway() {
    var list;
    try { list = await API.req('GET', '/api/sessions?limit=100'); }
    catch (e) { return false; }
    var rows = (list && list.data) || [];
    var changed = false, added = 0;
    for (var i = 0; i < rows.length && added < MAX_IMPORT; i++) {
      var r = rows[i];
      if (!r || !r.id) continue;
      if (r.archived || r.hidden || r.is_internal_child) continue;
      if (gone.indexOf(r.id) >= 0) continue;
      if (byBackend(r.id)) continue;
      if (!(Number(r.message_count) > 1)) continue;
      /* Историю тянем один раз за сессию страницы: пустые/битые ответы
         иначе запрашивались бы на каждом проходе. */
      if (tried[r.id] && Date.now() - tried[r.id] < 120000) continue;
      tried[r.id] = Date.now();
      var msgs;
      try { msgs = await history(r.id); } catch (e) { continue; }
      if (!msgs.length) continue;
      var twin = findTwin(msgs);
      if (twin) {
        twin.backendId = r.id;
        twin.updatedAt = Math.max(tsOf(r), Number(twin.updatedAt) || 0);
        changed = true;
        continue;
      }
      state.sessions.unshift(makeSession(r, msgs));
      added++;
      changed = true;
    }
    return changed;
  }

  async function refresh(s) {
    if (!s || !s.backendId) return false;
    if (ui.generating && s.id === state.activeSessionId) return false;
    var remote = await history(s.backendId);
    if (!remote.length) return false;
    var added = appendTail(s, remote);
    if (added) {
      await save();
      if (s.id === state.activeSessionId) renderAll();
    }
    return added > 0;
  }

  /* Есть ли незавершённый ход: после последнего сообщения пользователя
     агент ничего не дописал, или его текст остался в статусе потокового. */
  function needsResume(s) {
    var msgs = s.messages || [];
    for (var i = msgs.length - 1; i >= 0; i--) {
      var m = msgs[i];
      if (m.role === 'agent' && (m.kind === 'text' || m.kind === 'tool' || m.kind === 'thinking')) {
        return m.status === 'streaming' || m.status === 'running';
      }
      if (m.role === 'user') return true;
    }
    return false;
  }

  async function maybeAttach(s) {
    if (!s || !s.backendId) return;
    if (s.id !== state.activeSessionId) return;
    if (ui.generating) return;
    /* Свёрнутое окно ничего не показывает: поток откроем при возвращении
       (visibilitychange), иначе впустую вычитывали бы весь бэклог. */
    if (document.hidden) return;
    if (attaching[s.backendId]) return;             // уже подключаемся — второй поток не нужен
    var info;
    try {
      info = await API.req('GET', '/api/sessions/' +
                           encodeURIComponent(s.backendId) + '/active-run');
    } catch (e) { return; }
    if (!info || !info.exists) return;
    var run = String(info.run_id || '');
    if (attached[s.backendId] === run) return;
    if (!info.active && !needsResume(s)) return;
    attached[s.backendId] = run;
    attaching[s.backendId] = true;
    try { await attach(s, info); }
    finally { attaching[s.backendId] = false; }
  }

  async function attach(s, info) {
    var sid = s.backendId;
    var shown = function () { return s.id === state.activeSessionId; };
    var done = false;
    var cur = null, think = null, finalText = '', errMsg = '', netMsg = '';
    /* Новый ход в той же сессии начинает seq заново: продолжаем только тот
       run, который уже показывали, иначе пропустили бы все его события. */
    var runKey = String((info && info.run_id) || '');
    if (attachRun[sid] !== runKey) { attachRun[sid] = runKey; attachSeq[sid] = -1; }
    var lastSeq = Number(attachSeq[sid]) || -1;

    var patchIfShown = function (m) { if (m && shown()) patch(m); };
    var ensureText = function () {
      if (!cur) {
        cur = msg(s, { role: 'agent', kind: 'text', content: '', status: 'streaming',
                       meta: { model: 'восстановлено' } });
        if (shown()) renderStream();
      }
      return cur;
    };
    var ensureThink = function () {
      if (!think || think.status !== 'running') {
        think = msg(s, { role: 'agent', kind: 'thinking', content: '',
                         meta: { open: true }, status: 'running' });
        if (shown()) renderStream();
      }
      return think;
    };
    var finish = async function (note) {
      if (done) return;
      done = true;
      if (cur && cur.status === 'streaming') cur.status = 'done';
      if (think && think.status === 'running') think.status = 'done';
      if (typeof closeStaleTools === 'function') closeStaleTools(s);
      ui.generating = false;
      if (typeof queueMarkTurnEnd === 'function') queueMarkTurnEnd();
      ui.abort = false;
      ui.toolSessionId = null;
      setSend();
      renderHead();
      if (typeof renderThinkPinSoon === 'function') renderThinkPinSoon();
      try { await save(); } catch (e) { /* */ }
      if (shown()) renderAll();
      setStatus(note || (errMsg ? 'Ошибка' : 'Готово'));
      bumpRev();
      flushQueueSoon(120);
    };

    ui.generating = true;
    ui.abort = false;
    if (typeof queueMarkTurnStart === 'function') queueMarkTurnStart();
    ui.toolSessionId = s.id;
    setSend();
    renderHead();
    setStatus(info && info.active ? 'Агент ещё работает — подключаюсь…' : 'Восстанавливаю ход…');

    var sawEnd = false, gotGone = false, attempts = 0;
    while (attempts < ATTACH_TRIES) {
      attempts++;
      var reader = null, stalled = false, stallTimer = null;
      try {
        var resp = await fetch('/api/sessions/' + encodeURIComponent(sid) +
                               '/stream?last_seq=' + lastSeq, {
          cache: 'no-store',
          headers: {
            'Accept': 'text/event-stream',
            'Authorization': 'Bearer ' + API.key,
            'X-Hermes-Session-Key': 'inf-' + s.id
          }
        });
        if (!resp.ok || !resp.body) { gotGone = true; break; }

        reader = resp.body.getReader();
        var dec = new TextDecoder();
        var buf = '';
        var arm = function () {
          clearTimeout(stallTimer);
          /* Бэкенд шлёт keepalive каждые 10 секунд. Более долгая тишина —
             поток где-то буферизуется; обрываем чтение и переподключаемся,
             иначе страница навсегда осталась бы на «подключаюсь…». */
          stallTimer = setTimeout(function () {
            stalled = true;
            try { reader.cancel(); } catch (e) { /* */ }
          }, ATTACH_STALL_MS);
        };
        arm();
        while (true) {
          if (ui.abort) { try { reader.cancel(); } catch (e) { /* */ } break; }
          var rd = await reader.read();
          if (rd.done) break;
          arm();
          buf += dec.decode(rd.value, { stream: true });
          var idx;
          while ((idx = buf.indexOf('\n\n')) >= 0) {
            var frame = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            var ev = 'message', data = null;
            var lines = frame.split('\n');
            for (var li = 0; li < lines.length; li++) {
              var ln = lines[li];
              if (ln.indexOf('event:') === 0) ev = ln.slice(6).trim();
              else if (ln.indexOf('data:') === 0) {
                try { data = JSON.parse(ln.slice(5).trim()); } catch (e) { data = null; }
              }
            }
            if (!data) continue;
            var evRun = data.run_id ? String(data.run_id) : '';
            if (evRun && runKey && evRun !== runKey) {
              runKey = evRun; lastSeq = -1; attachSeq[sid] = -1;
            }
            var seqNum = Number(data.seq);
            if (seqNum > 0) {
              if (seqNum <= lastSeq) continue;   // это уже показано до обрыва
              lastSeq = seqNum;
              attachSeq[sid] = seqNum;
            }

            if (ev === 'assistant.delta' && data.delta) {
              var mt = ensureText();
              if (think && think.status === 'running') { think.status = 'done'; patchIfShown(think); think = null; }
              mt.content += data.delta;
              mt.status = 'streaming';
              patchIfShown(mt);
              var st = $('#stream');
              if (st && shown()) autoScrollBottom(st);
            } else if (ev === 'assistant.reasoning' && data.delta) {
              var rt = ensureThink();
              rt.content += data.delta;
              patchIfShown(rt);
            } else if (ev === 'assistant.commentary' && data.text && !data.already_streamed) {
              var ct = ensureText();
              ct.content += (ct.content ? '\n\n' : '') + data.text;
              ct.status = 'streaming';
              patchIfShown(ct);
            } else if (ev === 'tool.progress' && data.tool_name === '_thinking' && data.delta) {
              var tt = ensureThink();
              tt.content += data.delta;
              patchIfShown(tt);
            } else if (ev === 'tool.started') {
              if (think) { think.status = 'done'; think = null; }
              cur = null;
              var toolIcon = 'fa-solid fa-screwdriver-wrench';
              var tn = String(data.tool_name || '').toLowerCase();
              if (tn.indexOf('file') >= 0 || tn.indexOf('read') >= 0 || tn.indexOf('write') >= 0) toolIcon = 'fa-solid fa-file-lines';
              else if (tn.indexOf('terminal') >= 0 || tn.indexOf('shell') >= 0) toolIcon = 'fa-solid fa-terminal';
              else if (tn.indexOf('web') >= 0 || tn.indexOf('search') >= 0) toolIcon = 'fa-solid fa-globe';
              msg(s, { role: 'agent', kind: 'tool', content: '', status: 'running',
                       meta: { name: data.tool_name || 'tool', icon: toolIcon,
                               preview: data.preview || '', open: true,
                               input: data.args ? (typeof data.args === 'string' ? data.args : JSON.stringify(data.args, null, 2)) : '' } });
              if (shown()) renderStream();
              renderToolsPanelSoon();
            } else if (ev === 'tool.completed' || ev === 'tool.failed') {
              var tm = null;
              for (var k = s.messages.length - 1; k >= 0; k--) {
                if (s.messages[k].kind === 'tool' && s.messages[k].status === 'running') { tm = s.messages[k]; break; }
              }
              if (tm) {
                tm.status = ev === 'tool.completed' ? 'done' : 'error';
                tm.meta = tm.meta || {};
                if (data.preview) tm.meta.preview = data.preview;
                if (data.args) tm.meta.args = data.args;
                if (data.result) tm.meta.output = typeof data.result === 'string' ? data.result : JSON.stringify(data.result, null, 2);
                else if (data.output) tm.meta.output = typeof data.output === 'string' ? data.output : JSON.stringify(data.output, null, 2);
                if (data.duration) tm.meta.dur = (data.duration / 1000).toFixed(2) + 's';
                patchIfShown(tm);
              } else if (shown()) {
                renderStream();
              }
              renderToolsPanelSoon();
            } else if (ev === 'delegation.started') {
              msg(s, { role: 'agent', kind: 'tool', content: '', status: 'running',
                       meta: { name: 'Делегирование: ' + (data.subagent || 'субагент'),
                               icon: 'fa-solid fa-users',
                               preview: data.task || 'Выполнение подзадачи...', open: true } });
              if (shown()) renderStream();
              renderToolsPanelSoon();
            } else if (ev === 'delegation.completed') {
              var dm = null;
              for (var di = s.messages.length - 1; di >= 0; di--) {
                var dmi = s.messages[di];
                if (dmi.kind === 'tool' && dmi.status === 'running' &&
                    dmi.meta && dmi.meta.icon === 'fa-solid fa-users') { dm = dmi; break; }
              }
              if (dm) { dm.status = 'done'; dm.meta.output = data.result || 'Подзадача выполнена'; }
              if (shown()) renderStream();
              renderToolsPanelSoon();
            } else if (ev === 'assistant.completed') {
              if (!finalText) finalText = data.content || '';
            } else if (ev === 'run.completed' || ev === 'run.failed' || ev === 'run.cancelled') {
              sawEnd = true;
              if (data.final_response || data.content) finalText = data.final_response || data.content;
              if (finalText && !cur) {
                cur = msg(s, { role: 'agent', kind: 'text', content: finalText, status: 'done', meta: {} });
                if (shown()) renderStream();
              }
            } else if (ev === 'done') {
              sawEnd = true;
            } else if (ev === 'approval.request' || ev === 'approval' || ev === 'tool.approval' || ev === 'approval_required') {
              msg(s, { role: 'agent', kind: 'approval', status: 'running',
                       content: String(data.description || data.command || data.text || data.message || data.reason || 'Агент запрашивает разрешение.'),
                       meta: { cmd: data.command || data.cmd || data.tool || '',
                               tool: data.tool || '',
                               runId: data.run_id || data.runId || (info && info.run_id) || '',
                               requestId: data.request_id || data.prompt_id || '',
                               gen: data.execution_generation,
                               choices: Array.isArray(data.choices) ? data.choices : [] } });
              if (shown()) { renderStream(); toast('Агент ждёт разрешения', 'warn'); }
            } else if (ev === 'approval.settled') {
              var arid = data.request_id || data.prompt_id;
              var am2 = null;
              for (var ai = s.messages.length - 1; ai >= 0; ai--) {
                var ami = s.messages[ai];
                if (ami.kind === 'approval' && ami.status === 'running' &&
                    (!arid || (ami.meta && ami.meta.requestId === arid))) { am2 = ami; break; }
              }
              if (am2) { am2.status = 'done'; am2.meta = am2.meta || {};
                         if (!am2.meta.decision) am2.meta.decision = 'решено'; patchIfShown(am2); }
            } else if (ev === 'clarify.request') {
              msg(s, { role: 'agent', kind: 'clarify', status: 'running',
                       content: String(data.question || data.text || 'Агент задаёт вопрос.'),
                       meta: { requestId: data.request_id || data.prompt_id || '',
                               gen: data.execution_generation,
                               runId: data.run_id || (info && info.run_id) || '',
                               choices: Array.isArray(data.choices) ? data.choices : [],
                               multi: !!data.multi_select, timeoutS: data.timeout_s } });
              if (shown()) { renderStream(); toast('Агент ждёт ответа на вопрос', 'warn'); }
            } else if (ev === 'clarify.settled') {
              var crid = data.request_id || data.prompt_id;
              var cm2 = null;
              for (var ci = s.messages.length - 1; ci >= 0; ci--) {
                var cmi = s.messages[ci];
                if (cmi.kind === 'clarify' && cmi.status === 'running' &&
                    (!crid || (cmi.meta && cmi.meta.requestId === crid))) { cm2 = cmi; break; }
              }
              if (cm2) { cm2.status = 'done'; cm2.meta = cm2.meta || {};
                         if (!cm2.meta.answer) cm2.meta.answer = 'закрыт'; patchIfShown(cm2); }
            } else if (ev === 'error') {
              errMsg = errText(data.message || data.error || data, 'Ошибка шлюза');
              msg(s, { role: 'system', kind: 'system', content: '⚠️ ' + errMsg });
              if (shown()) renderStream();
            } else if (ev && ev.indexOf('subagent') === 0) {
              if (typeof onSubagentEvent === 'function' && onSubagentEvent(s, ev, data)) renderToolsPanelSoon();
            }
          }
        }
      } catch (e) {
        if (!(e && e.name === 'AbortError')) netMsg = errText(e.message || e, 'Подхват хода не удался');
      } finally {
        clearTimeout(stallTimer);
      }
      if (ui.abort || sawEnd || gotGone) break;
      if (attempts < ATTACH_TRIES) {
        /* Тишина или обрыв при живом ходе: догоняем поток с последнего seq,
           вместо того чтобы замереть на «подключаюсь» навсегда. */
        setStatus(stalled ? 'Поток молчит — переподключаюсь…'
                          : 'Связь с агентом прервалась — переподключаюсь…');
        await new Promise(function (r) { setTimeout(r, 1200 * attempts); });
      }
    }

    if (!sawEnd && !ui.abort) {
      /* Потока больше нет (ход завершился и буфер вытеснен) или подхват так и
         не поднялся: добираем хвост из истории — он переживает перезапуск. */
      try {
        var rem = await history(sid);
        if (rem.length && appendTail(s, rem) && shown()) renderAll();
      } catch (e) { /* */ }
      if (!gotGone) attached[sid] = '';   // следующий опрос попробует снова
    }
    await finish(ui.abort ? 'Отсоединено'
                 : (errMsg ? 'Ошибка'
                    : (sawEnd ? 'Готово'
                       : (gotGone ? 'Ход завершён'
                          : (netMsg ? 'Ошибка подключения' : 'Нет потока — обновлю позже')))));
  }

  /* Сообщить палитре, какой разговор открыт. Ключ — тот же, что пишет
     ai_bar.js; личность берётся из её же настройки, поэтому окна сходятся. */
  function announce(s) {
    if (!s || !s.backendId) return;
    try {
      if (localStorage.getItem(activeKey()) !== s.backendId) {
        localStorage.setItem(activeKey(), s.backendId);
      }
      localStorage.setItem(revKey(), String(Date.now()));
    } catch (e) { /* */ }
  }

  async function syncOnce() {
    if (syncBusy) return;
    /* init() в 09 сначала читает состояние с сервера и только потом выбирает
       сессию. Пока activeSessionId пуст, сохранение затёрло бы прочитанное
       пустым состоянием — поэтому до готовности не трогаем ни импорт, ни save. */
    if (!state.activeSessionId) return;
    syncBusy = true;
    lastSync = Date.now();
    try {
      var changed = await importGateway();
      if (changed) {
        try { await save(); } catch (e) { /* */ }
        renderAll();
      }
      if (pendingSwitch) {
        var target = byBackend(pendingSwitch);
        if (target) {
          pendingSwitch = '';
          if (document.hidden && target.id !== state.activeSessionId) openSession(target.id);
        }
      }
      var s = active();
      if (s) {
        await refresh(s);
        maybeAttach(s);
      }
    } catch (e) {
      /* шлюз недоступен — попробуем на следующем проходе */
    } finally {
      syncBusy = false;
    }
  }

  function scheduleSync(delay) {
    setTimeout(syncOnce, delay || 300);
  }

  /* ---------- Подключение к существующему интерфейсу ---------- */
  var _openSession = openSession;
  openSession = function (id, mid) {
    var r = _openSession.apply(this, arguments);
    var s = state.sessions.find(function (x) { return x.id === id; });
    if (s) {
      announce(s);
      /* Открытая сессия могла уйти вперёд, пока её не было на экране
         (ход шёл в палитре): подтягиваем хвост и, если агент ещё работает,
         подключаемся к его потоку. */
      setTimeout(function () {
        refresh(s).then(function () { maybeAttach(s); },
                        function () { maybeAttach(s); });
      }, 400);
    }
    return r;
  };

  var _killSession = killSession;
  killSession = function (id, silent) {
    var s = state.sessions.find(function (x) { return x.id === id; });
    if (s && s.backendId) markGone(s.backendId);   // шлюз не удаляем: отмена в истории должна работать
    return _killSession.apply(this, arguments);
  };

  window.addEventListener('storage', function (e) {
    if (!e.key) return;
    if (e.key === activeKey()) {
      var sid = e.newValue || '';
      if (!sid) return;
      var target = byBackend(sid);
      if (target) {
        if (document.hidden && target.id !== state.activeSessionId) openSession(target.id);
        else refresh(target);
      } else {
        pendingSwitch = sid;
        scheduleSync(200);
      }
      return;
    }
    if (e.key === revKey()) scheduleSync(400);
  });

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) syncOnce();
  });
  /* Окно Electron может быть видимым, но неактивным (фокус в палитре):
     при возврате фокуса догоняем изменения сразу, не ожидая опроса. */
  var lastSync = 0;
  window.addEventListener('focus', function () {
    if (Date.now() - lastSync < 8000) return;
    syncOnce();
  });

  function boot() {
    if (booted) return;
    /* Ждём окончания init(): до этого state ещё не прочитан с сервера. */
    if (!state.activeSessionId) { setTimeout(boot, 800); return; }
    booted = true;
    syncOnce();
    setInterval(function () { if (!document.hidden) syncOnce(); }, POLL_MS);
  }
  /* init() в 09 грузит состояние асинхронно; ждём его окончания по факту
     появления сессий в дереве, а не по таймеру «на глаз» — плюс страховки. */
  setTimeout(boot, 1500);
  setTimeout(syncOnce, 5000);
  setTimeout(syncOnce, 12000);
})();
