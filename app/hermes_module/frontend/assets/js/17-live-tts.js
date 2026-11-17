/* ============ ОЗВУЧКА НА ЛЕТУ ============
   Агент отвечает голосом не после конца генерации, а пока текст ещё пишется:
   поток assistant.delta режется на предложения, и каждое озвучивается, как
   только оно дописано. Блок рассуждений (assistant.reasoning) сюда не
   подключён намеренно — озвучивается только ответ.

   Откуда берётся текст: 09-init-api.js уже копит дельты в m.content, поэтому
   сервер трогать не нужно — LiveTTS.feed(delta) вызывается сразу после patch().

   Разбиение на предложения — перенос SentenceChunker из Hermes
   (hermes_module/backend/tools/tts_streaming.py), но ручным сканером, без
   (?<=): разбивать текст нужно прямо в браузере, а Electron/WebView не
   даёт гарантии поддержки lookbehind.

   Главное решение — очередь хранит ТЕКСТ, а не готовые WAV. Синтезируется
   только то, что вот-вот прозвучит (AHEAD предложений, одновременно INFLIGHT
   запросов). Так память ограничена, синтез не тратится попусту, и при
   прерывании несозвучанный хвост отбрасывается бесплатно.

   Издержки накопителя известны: генерация текста примерно в 5-8 раз быстрее
   речи, поэтому на длинном ответе голос отстаёт от текста. Это осознанно:
   счётчик «озвучено N из M» показывает отставание, а прервать можно всегда. */

const LiveTTS = (function () {
  /* min_len=20 — из tts.streaming.min_len.
     SOFT_CAP — только предохранитель, а не способ резать речь на части:
     обычное предложение уходит ОДНИМ запросом. Silero сам расставляет паузы
     внутри одного apply_tts, а дробление по 140 символов давало обрывы —
     каждый кусок начинался с нейтральной интонации, и слушателю казалось,
     что фраза «не договорена» и началась следующая строка.
     MAX_WORD — отдельный лимит для «слов»-монстров (URL, base64, слипшиеся
     без пробелов блоки): их движок не произнесёт ни целиком, ни как речь. */
  const MIN_LEN = 20;
  const SOFT_CAP = 480;
  const MAX_WORD = 120;
  const AHEAD = 3;      // сколько предложений держим синтезированными впрок
  const INFLIGHT = 2;   // одновременных POST на /api/voice/tts

  let buf = '';     // нераспущенный хвост текста
  let carry = '';   // короткие куски, ждут следующего предложения
  let pending = []; // текст, ждущий синтеза
  let ready = [];   // готовое аудио, ждущее воспроизведения
  let playing = null;
  let inflight = 0;
  let gen = 0;      // поколение: ответ сервера со старым — выбрасывается

  let on = false;          // идёт ход, текст ещё принимается
  let finishedTurn = false;// генерация закончилась, очередь доигрывается
  let spoken = 0, total = 0;
  let lastCounter = '';
  let micHeld = false;   // микрофон сейчас придержан ради озвучки
  let onDone = null;     // вызывается один раз, когда очередь доиграна
  let pumpTimer = null;  // страховочный таймер насоса (см. pump)
  let muted = false;     // ход заглушён стопом — дельты текущего хода молчат (см. stop/arm)
  let aborters = [];     // живые AbortController: стоп рвёт и сами запросы

  function enabled() {
    if (typeof S !== 'function') return false;
    if (S().voice && S().voice.liveSpeak === false) return false;
    return typeof speakEnabled === 'function' ? speakEnabled() : true;
  }

  /* ---------- разбиение ---------- */

  /* Отрезает от buf всё до границы. Граница — знак конца предложения или
     перевод строки, ЗА КОТОРЫМ ИДЁТ ПРОБЕЛ (либо конец текста при flush).
     Такое условие отличает конец фразы от «3.5», «т.е.», «example.com» —
     точка внутри слова или числа границей не считается. */
  function scan(force) {
    const s = buf;
    let start = 0;
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (c !== '\n' && '.!?…'.indexOf(c) < 0) continue;
      let j = i + 1;
      let sawSpace = false;
      while (j < s.length && (s[j] === ' ' || s[j] === '\t' || s[j] === '\r' || s[j] === '\n')) {
        sawSpace = true;
        j++;
      }
      // Граница без пробела — только если дальше начинается новое предложение:
      // заглавная буква. Так «3.5», «т.е.» и «example.com» остаются целыми,
      // а «проверки.Второе» (модель иногда не ставит пробел) разрывается.
      if (!sawSpace && j < s.length) {
        const nxt = s[j];
        const isLetter = (nxt >= 'А' && nxt <= 'Я') || (nxt >= 'а' && nxt <= 'я')
          || (nxt >= 'A' && nxt <= 'Z') || (nxt >= 'a' && nxt <= 'z')
          || nxt === 'Ё' || nxt === 'ё' || nxt === '\"' || nxt === '«' || nxt === '„';
        const isUpper = (nxt >= 'А' && nxt <= 'Я') || (nxt >= 'A' && nxt <= 'Z')
          || nxt === 'Ё';
        if (!(isLetter && isUpper) && nxt !== '\n') continue;
      }
      if (!sawSpace && j >= s.length && !force) continue;
      // Короткий кусок («Да.», «Ок.») не произносим отдельно, но и НЕ
      // выбрасываем: он уходит в carry и едет вместе со следующим
      // предложением. Раньше carry обнулялся здесь же, и короткие фразы
      // просто исчезали из озвучки. Пробел при склейке — чтобы «Да.» и
      // «Нет.» не слипались в «Да.Нет.»; carry входит в head уже сам.
      const head = ((carry ? carry + ' ' : '') + s.slice(start, i + 1)).trim();
      start = j;
      i = j - 1;
      if (head.length >= MIN_LEN) { carry = ''; emit(head); }
      else carry = head;
    }
    buf = s.slice(start);
    if (force) {
      const tail = (carry ? carry + ' ' : '') + buf;
      carry = '';
      buf = '';
      const clean = tail.trim();
      if (clean.length >= 2) emit(clean);
    }
  }

  /* Слабая граница внутри уже собранной строки: точка с запятой, двоеточие,
     запятая, тире, открывающая скоба. Разрыв там — естественная пауза, а не
     обрыв посреди фразы. Возвращает позицию разрыва (разделитель остаётся
     слева) или 0, если такой границы в строке нет. */
  function weakCut(line) {
    let best = -1;
    for (const p of ['; ', ': ', ', ', ' — ', ' – ', ' - ', ' (']) {
      const i = line.lastIndexOf(p);
      if (i > best) best = i + p.length;
    }
    return best;
  }

  /* Куски для синтеза. Фраза короче SOFT_CAP возвращается ОДНОЙ строкой —
     именно этого не хватало, чтобы голос договаривал до конца. Режем только
     там, где иначе нельзя, и предпочитаем слабые границы; слово-монстр
     (URL, base64) делим принудительно, иначе движок его не произнесёт. */
  function splitLong(text) {
    const s = String(text).trim();
    if (!s) return [];
    const words = s.split(/\s+/).filter(Boolean);
    if (!words.length) return [];
    let monster = false;
    for (const w of words) if (w.length > MAX_WORD) { monster = true; break; }
    if (!monster && s.length <= SOFT_CAP) return [s];

    const out = [];
    let line = '';
    const flush = () => { const t = line.trim(); if (t) out.push(t); line = ''; };
    for (let w of words) {
      if (w.length > MAX_WORD) {
        flush();
        const parts = [];
        for (let i = 0; i < w.length; i += MAX_WORD) parts.push(w.slice(i, i + MAX_WORD));
        for (let i = 0; i + 1 < parts.length; i++) out.push(parts[i]);
        line = parts[parts.length - 1];
        continue;
      }
      if (!line) { line = w; continue; }
      if ((line + ' ' + w).length <= SOFT_CAP) { line += ' ' + w; continue; }
      const cut = weakCut(line);
      if (cut > 0) {
        const head = line.slice(0, cut).trim();
        line = line.slice(cut).trim();
        if (head) out.push(head);
        if (line && (line + ' ' + w).length <= SOFT_CAP) { line += ' ' + w; continue; }
        flush();
      } else {
        flush();
      }
      line = w;
    }
    flush();
    return out;
  }

  function emit(head) {
    // plain() убирает markdown ( fences, *, `, #) — иначе Silero произнесёт их.
    const clean = typeof plain === 'function' ? plain(head) : head;
    if (!clean || clean.length < 2) return;
    const pieces = splitLong(clean);
    if (!pieces.length) return;
    // Считаем именно куски: обычно это одно предложение целиком, но
    // патологически длинный хвост всё же режется — счётчик обязан сходиться
    // с реальным числом запросов, а не показывать «озвучено 2 из 1».
    total += pieces.length;
    for (const p of pieces) pending.push(p);
    fill();
    // Раз в ход текст приходит частями, и каждая часть — новый пинок насосу.
    pump();
  }

  /* ---------- синтез и воспроизведение ---------- */

  function holdMic(on2) {
    if (typeof passiveHold === 'function') passiveHold(on2);
  }

  /* Зависший запрос опаснее ошибки: inflight остаётся занятым навсегда, и
     очередь встаёт без движения — страховочный pump() тут не поможет, потому
     что он отличает «пустой ход» от «висящего запроса». Порог заведомо больше
     холодного старта пула Silero (~13 с), поэтому срабатывает только на
     настоящую зависшую connection. */
  const SYNTH_TIMEOUT = 30000;

  function withTimeout(p, ms) {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('tts timeout')), ms);
      p.then(v => { clearTimeout(t); resolve(v); },
             e => { clearTimeout(t); reject(e); });
    });
  }

  async function synth(piece) {
    const canServer = typeof sileroOk === 'undefined' || sileroOk !== false;
    // baseURL бывает пустым (same-origin) — проверять его на истинность нельзя,
    // иначе озвучка не работала бы вообще.
    if (canServer && !(typeof IS_FILE_PROTOCOL !== 'undefined' && IS_FILE_PROTOCOL)
        && typeof API !== 'undefined' && API) {
      try {
        const headers = Object.assign(
          { 'Content-Type': 'application/json' },
          typeof voiceEngineHeaders === 'function' ? voiceEngineHeaders('tts') : {});
        // Свой контроллер на запрос: стоп должен оборвать не только прослушивание,
        // но и синтез на сервере. Иначе «стоп» ждал бы, пока пул Silero допечёт
        // ненужные фразы, и следующий ход стартовал бы в полную очередь.
        const ctl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
        if (ctl) aborters.push(ctl);
        // Контроллер снимается с учёта и при успехе, и при ошибке/отмене —
        // иначе массив рос бы и держал ссылки на мёртвые запросы.
        const drop = () => { const k = aborters.indexOf(ctl); if (k >= 0) aborters.splice(k, 1); };
        const r = await withTimeout(fetch(API.baseURL + '/api/voice/tts', {
          method: 'POST', headers: headers, body: JSON.stringify({ text: piece }),
          signal: ctl ? ctl.signal : undefined
        }), SYNTH_TIMEOUT).then(v => { drop(); return v; },
                                    e => { drop(); throw e; });
        if (r.ok) {
          const b = await r.blob();
          if (b.size && (!b.type || b.type.indexOf('audio') === 0)) {
            if (typeof sileroOk !== 'undefined') sileroOk = true;
            const a = new Audio(URL.createObjectURL(b));
            a.volume = Math.min(1, S().voice.volume || 1);
            a.playbackRate = S().voice.rate || 1;
            return a;
          }
        }
      } catch (e) {
        // Отмена по стопу — это не поломка движка. Раньше здесь ставилось
        // sileroOk=false, и после любого «стоп» весь сеанс молча уходил на
        // браузерный голос. И падать в браузерный синтез нельзя: он бы
        // произнёс уже отменённую фразу — то самое «сказал стоп, а говорит».
        if (e && e.name === 'AbortError') return null;
        if (typeof sileroOk !== 'undefined') sileroOk = false;
      }
    }
    // Ход заглушён стопом пока шёл запрос — озвучивать нечего.
    if (muted) return null;
    // Движок недоступен — озвучиваем этот кусок браузером, как это делает speak().
    try {
      const u = new SpeechSynthesisUtterance(piece);
      const list = typeof voices === 'function' ? voices() : [];
      const v = list.find(x => x.voiceURI === S().voice.voiceURI);
      if (v) u.voice = v;
      u.lang = v ? v.lang : 'ru-RU';
      u.rate = S().voice.rate;
      u.pitch = S().voice.pitch;
      u.volume = S().voice.volume;
      return u;
    } catch (e) {
      return null;
    }
  }

  function drainable() {
    return !!(pending.length || ready.length || inflight || playing);
  }

  /* Держит синтез «на один шаг впереди» воспроизведения: память ограничена
     AHEAD предложениями, но текст в очереди не ограничен — ничего не теряется. */
  function fill() {
    if (muted) return;
    while (inflight < INFLIGHT && ready.length + inflight < AHEAD && pending.length) {
      const piece = pending.shift();
      const myGen = gen;
      inflight++;
      synth(piece).then(item => {
        inflight--;
        if (myGen !== gen) { dispose(item); return; }
        if (item) { ready.push(item); next(); }
        // fill() зовём в любом случае: иначе после первого же предложения
        // синтез больше не пойдёт, и хвост ответа останется без голоса.
        fill();
      }).catch(() => { inflight--; fill(); });
    }
  }

  /* Страховка от «тихого» простоя. Насос заводится из emit() и из разрешения
     синтеза, но если ни то, ни другое не произошло (fetch оборвался, движок не
     сообщил ни onended, ни onerror, сторож отработал вхолостую), очередь
     встала бы навсегда: pending непустой, а движения нет. Проверяем ровно это
     состояние и возобновляем синтез. */
  function pump() {
    if (muted || pumpTimer !== null) return;
    pumpTimer = setTimeout(() => {
      pumpTimer = null;
      if (muted || !pending.length) return;
      if (!inflight && !playing && !ready.length) fill();
      if (pending.length) pump();
    }, 1000);
  }

  function dispose(item) {
    try {
      if (item && typeof HTMLAudioElement !== 'undefined' && item instanceof HTMLAudioElement) {
        item.onended = null; item.onerror = null; item.pause();
        URL.revokeObjectURL(item.src);
      } else if (item && typeof SpeechSynthesisUtterance !== 'undefined'
                 && item instanceof SpeechSynthesisUtterance) {
        // Обработчики снимаем тоже: иначе speechSynthesis.cancel() вызовет
        // onend, а тот — finish() счётчика уже после остановки.
        item.onend = null; item.onerror = null;
      }
    } catch (e) { /* элемент уже освобождён */ }
  }

  function next() {
    if (muted) return;
    /* Проигрывание — тоже часть насоса: как только кусок ушёл из ready, место
       в лимите AHEAD освободилось, и синтез обязан продолжиться. Без этого
       очередь насыщалась до AHEAD, проигрывала эти куски и вставала навсегда,
       оставляя хвост ответа без голоса — ровно то и было в «озвучено 11 из 20». */
    fill();
    if (playing || !ready.length) { release(); return; }
    const item = ready.shift();
    playing = item;
    const myGen = gen;
    if (!micHeld) { micHeld = true; holdMic(true); }
    let done = false;
    const timers = [];
    const clearGuards = () => { for (const t of timers) clearTimeout(t); timers.length = 0; };
    const finish = () => {
      if (done) return;
      done = true;
      clearGuards();
      // Сторож фразы живёт до 45 с. Если после стопа он всё-таки сработает,
      // он обязан уйти молча: иначе дорисует в счётчик «озвучено 1 из 0»
      // и разбудит микрофон уже мёртвого хода.
      if (myGen !== gen) { dispose(item); if (playing === item) playing = null; return; }
      dispose(item);
      if (playing === item) playing = null;
      spoken++;
      counter();
      next();
    };
    /* Сторож. Если движок не сообщил ни об окончании, ни об ошибке, очередь
       встала бы навсегда: playing остался бы занятым, а счётчик замер бы на
       «4 из 5». Chromium так ведёт себя с speechSynthesis, когда синтез в
       системе недоступен. Считаем предложение озвучанным и идём дальше. */
    if (item && typeof HTMLAudioElement !== 'undefined' && item instanceof HTMLAudioElement) {
      item.onended = finish;
      item.onerror = finish;
      const p = item.play();
      if (p && p.catch) p.catch(finish);
      const dur = (item.duration && isFinite(item.duration)) ? item.duration * 1000 : 0;
      timers.push(setTimeout(finish, dur > 0 ? dur + 5000 : 45000));
    } else if (item && typeof SpeechSynthesisUtterance !== 'undefined'
               && item instanceof SpeechSynthesisUtterance) {
      item.onend = finish;
      item.onerror = finish;
      try { speechSynthesis.speak(item); } catch (e) { clearGuards(); finish(); return; }
      // Быстрая проверка «а началось ли вообще»: если через 700 мс движок
      // молчит, голосов в системе нет и ждать нечего — иначе очередь встала
      // бы на десятки секунд впустую.
      timers.push(setTimeout(() => {
        let alive = false;
        try { alive = !!speechSynthesis.speaking || !!speechSynthesis.pending; } catch (e) { alive = true; }
        if (!alive) { clearGuards(); finish(); }
      }, 700));
      // Если начал говорить — сторож на длительность фразы с запасом.
      timers.push(setTimeout(finish, Math.max(6000, (item.text || '').length * 100) + 3000));
    } else {
      clearGuards();
      finish();
    }
  }

  /* Микрофон возвращается в работу, только когда очередь действительно пуста,
     иначе агент услышит сам себя на середине ответа. */
  function release() {
    if (drainable()) return;
    if (micHeld) { micHeld = false; holdMic(false); }
    maybeFinish();
  }

  /* Хук «озвучка закончилась». Без него голосовой режим возвращался в
     ожидание сразу после конца генерации: микрофон включался посреди речи,
     счётчик простоя успевал истечь, и разговор слетал в режим слова-будильника. */
  function maybeFinish() {
    // Условие именно «ход завершён», а не «ход не идёт»: иначе хук,
    // зарегистрированный до старта очереди, сработал бы мгновенно.
    if (!onDone || !finishedTurn || drainable()) return;
    const fn = onDone;
    onDone = null;
    try { fn(); } catch (e) { /* вызывающий не должен ронять очередь */ }
  }

  function counter() {
    // Пока идёт новый ход, строку статуса трогать нельзя — там «Думаю…».
    // Но если очередь ещё доигрывает, счётчик обязан обновляться, иначе он
    // замирает на недоговорённом числе.
    const generating = (typeof ui !== 'undefined' && ui && ui.generating);
    if (!finishedTurn && generating) return;
    if (!total) return;
    const txt = 'озвучено ' + spoken + ' из ' + total;
    if (txt === lastCounter) return;
    lastCounter = txt;
    if (typeof setStatus === 'function') setStatus(txt);
  }

  /* ---------- публичный интерфейс ---------- */

  function beginTurn() {
    if (!drainable()) { spoken = 0; total = 0; lastCounter = ''; }
    on = true;
    finishedTurn = false;
    buf = '';
    carry = '';
  }

  return {
    /* Новый ход: снимаем заглушку, оставшуюся от стоп�� в прошлом ходе.
       Ставится в самом начале запроса (прямо рядом с ui.generating=true),
       иначе «стоп» глушил бы озвучку до конца сеанса. */
    arm: function () {
      muted = false;
      gen++;
      if (pumpTimer !== null) { clearTimeout(pumpTimer); pumpTimer = null; }
    },
    feed: function (delta) {
      if (!delta || !enabled()) return;
      // Заглушено стопом: поток от сервера ещё идёт (генерацию он не отменяет),
      // и каждая дельта оживляла бы beginTurn() — голос продолжал говорить
      // после «стоп». Молчим до конца хода.
      if (muted) return;
      if (!on) beginTurn();
      buf += delta;
      scan(false);
    },
    /* Граница сегмента: ответ разбит вызовом инструмента или вставкой
       комментария. Не прерываем озвучку — только не даём склеить предложения
       через границу, поэтому хвост дописываем принудительно. */
    segment: function () {
      if (muted || !on || !enabled()) return;
      scan(true);
      buf = '';
      carry = '';
    },
    end: function () {
      // Заглушённый ход закрываем молча: хвост выбрасываем, счётчики не трогаем.
      if (muted) { muted = false; on = false; finishedTurn = false; buf = ''; carry = ''; return; }
      if (!on) return;
      scan(true);
      on = false;
      finishedTurn = true;
      counter();
      // Если очередь уже пуста (короткий ответ, звук успел закончиться),
      // хук должен сработать прямо сейчас — иначе голосовой режим останется
      // без возврата в ожидание.
      release();
    },
    /* Озвучка закончилась — можно возвращать микрофон в режим диалога.
       Регистрируется обычно уже после end(): если очередь опустела, хук
       срабатывает сразу, иначе — когда доиграет последнее предложение. */
    whenDone: function (fn) {
      if (typeof fn !== 'function') return;
      onDone = fn;
      maybeFinish();
    },
    stop: function () {
      /* Главное отличие от «просто подождать»: стоп обязан заглушить ход, а не
         только очередь. Пока сервер дописывает ответ, его дельты продолжают
         приходить в feed(); без muted каждая из них поднимала новый ход через
         beginTurn(), и голос после «стоп» читал дальше — до самого конца
         генерации. Снимает заглушку только arm() в начале следующего хода. */
      muted = true;
      gen++;
      if (pumpTimer !== null) { clearTimeout(pumpTimer); pumpTimer = null; }
      buf = ''; carry = '';
      pending.length = 0;
      for (const a of ready) dispose(a);
      ready.length = 0;
      dispose(playing);
      playing = null;
      on = false;
      finishedTurn = false;
      /* inflight НЕ обнуляем: запросы ещё в самолёте и снимут себя сами
         обработчиками. Обнуление давало отрицательный счётчик, из-за чего
         fill() потом запускал больше INFLIGHT синтезов подряд. */
      spoken = 0; total = 0; lastCounter = '';
      // Рвём и сами запросы, а не только прослушивание — иначе пул Silero
      // допекал бы отменённые фразы и следующий ход стартовал бы вслед за ними.
      for (const ctl of aborters) { try { ctl.abort(); } catch (e) { /* уже снят */ } }
      aborters.length = 0;
      try { speechSynthesis.cancel(); } catch (e) { /* нечего отменять */ }
      if (micHeld) { holdMic(false); micHeld = false; }
      // После прерывания очередь тоже «закончилась»: без этого микрофон
      // остался бы выключенным до следующего хода.
      const fn = onDone;
      onDone = null;
      if (fn) { try { fn(); } catch (e) { /* очередь уже пуста */ } }
    },
    /* Ответ уже озвучен на лету — обычный speak() вызывать нельзя, иначе
       пользователь услышит его дважды. */
    spokeThisTurn: function () { return total > 0; },
    /* Озвучка сейчас звучит или ещё должна прозвучать. Проверяющий стоп
       (голосовое «стоп» и кнопка микрофона) смотрит только на ui.generating
       и на curTts, а к этому моменту генерация давно закончилась: голос ещё
       доигрывает хвост ответа. Без этой проверки стоп возвращался молча и
       озвучка продолжалась до конца.
       muted сюда не входит: заглушенный ход уже остановлен, повторный стоп
       нечего отменять. */
    busy: function () { return drainable() || on; },
    status: function () { return { spoken: spoken, total: total, pending: pending.length }; }
  };
})();