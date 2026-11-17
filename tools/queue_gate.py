#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гейт очереди воспроизведения (задача №82).

Что доказывает:
  A. Машину состояний «очередь ↔ контекст» прогоняет node на реальном коде
     шаблона: «дальше»/«назад», удаление, перетаскивание, перемешивание и
     кроссфейд держат queueIndex, а после конца очередь возвращает плеер
     в тот список, на котором она выросла.
  B. «В очередь» видит трек с любой страницы библиотеки, а не только с
     текущей сотни, и честно говорит, когда трека нет.
  C. Разметка рядов очереди кликабельна.
  D. Контракты шаблона: автопереход слушает галочку «Проигрывать дальше»,
     кроссфейд уводит указатель вместе с рядом, перенятие чужого плеера
     пересобирает очередь, безкаталожный стрим очередь отпускает, handoff
     увозит играющий порядок.

Без node часть A не «зеленеет», а падает: поведение проверяется исполнением.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
TPL = APP / "templates" / "page_media.html"

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    if not ok:
        print("  ПРОВАЛ: %s — %s" % (name, detail))


def section(title):
    print("\n== %s ==" % title)


def js_slice(text, start, name):
    """Функция целиком от `start`: счёт фигурных скобок с пропуском строк,
    комментариев и текста шаблонных литералов между `${` и `}`.

    Поиск «}` на нулевой колонке» не годится: JS этого шаблона пишется без
    отступов, и закрывающая скобка вложенного блока выглядит как конец функции.
    """
    i = text.index("{", start)
    depth = 0
    quote = None
    tmpl_at = []
    line_comment = False
    block_comment = False
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1:i + 2]
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif quote in ("'", '"'):
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif quote == "`":
            if ch == "\\":
                i += 1
            elif ch == "`":
                quote = None
            elif ch == "$" and nxt == "{":
                quote = None
                depth += 1
                tmpl_at.append(depth - 1)
                i += 1
        elif ch == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif ch in "'\"`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if tmpl_at and depth == tmpl_at[-1]:
                tmpl_at.pop()
                quote = "`"
            elif depth == 0:
                return text[start:i + 1]
        i += 1
    raise SystemExit("не найден конец функции %s" % name)


def js_function(text, name):
    # async обязательно входит в срез: без него извлечённая функция с await
    # внутри синтаксически неверна.
    m = re.search(r"(?:\basync\s+)?function\s+%s\s*\(|\b%s\s*=\s*(?:\basync\s+)?function\s*\("
                  % (name, name), text)
    if not m:
        raise SystemExit("в шаблоне нет функции %s" % name)
    return js_slice(text, m.start(), name)


NEEDED = [
    "queueTrackById", "trackById", "queueIsPlaying", "playQueueItem", "finishQueue",
    "addToQueue", "queueMultipleTracks", "removeFromQueue", "clearQueue",
    "shuffleQueue", "toggleQueue", "renderQueue", "queueDragStart",
    "queueDragOver", "queueDrop", "playerNext", "playerPrev",
    "playTrack", "startCrossfade", "pluralize",
]

STUBS = r"""
// ---- заглушки окружения --------------------------------------------------
globalThis.window = globalThis;
let queue = [];
let queueIndex = -1;
let queueContext = null;
let queueFinished = false;
let draggedQueueIdx = -1;
let currentTrack = null, currentTrackIndex = 0, currentPlaylist = [], isPlaying = false, isShuffle = false;
let currentContextTracks = null;
let musicFullIndex = null;
let musicIndex = {tracks: []};
let audio = {src: '', volume: 1, playbackRate: 1, play(){ return Promise.resolve(); }, pause(){}};
let audio2 = {src: '', volume: 0, playbackRate: 1, play(){ return Promise.resolve(); }, pause(){}};
let store = {};
let localStorage = {getItem(k){ return store[k] === undefined ? null : store[k]; },
                    setItem(k, v){ store[k] = String(v); }};
let fetch = () => Promise.resolve({json: () => Promise.resolve({})});
let notifications = [];
function showNotification(msg, type){ notifications.push(msg); }
function esc(s){ return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;'); }
function encodePath(p){ return encodeURIComponent(String(p || '')); }
let dom = {};
function el(id){
  if(!dom[id]) dom[id] = {id: id, innerHTML: '', textContent: '', style: {},
                          classList: {toggle(){}, remove(){}, add(){}}};
  return dom[id];
}
let fullIndexLoaded = 0;
async function ensureFullMusicIndex(){ fullIndexLoaded++; return musicFullIndex; }
let played = [];
function updatePlayerFavIcon(){}
function checkAchievements(){}
function extractDynamicColors(){}
function startVisualizer(){}
let recentTracks = [];
let listenStats = {};
let dynamicColorsEnabled = false;
let audioCtx = null;
function initAudioContext(){}
let playbackSpeed = 1;
let crossfadeDuration = 0;
let isCrossfading = false;
let sleepEndOfTrack = false;
let currentTorrentPlayback = null;
function cancelSleepTimer(){}
globalThis.document = {getElementById: el, querySelectorAll(){ return []; },
                       createElement(){ return {style: {}, classList: {add(){}}, querySelector(){ return {}; }}; },
                       body: {appendChild(){}}};
function reset(){ played = []; notifications = []; }
// playTrack подменяем, чтобы видеть, что реально ушло в плеер: функции шаблона
// вызывают именно эту привязку, а драйвер читает played.
const _origPlayTrack = playTrack;
playTrack = function(trackId, index){ _origPlayTrack(trackId, index); played.push(trackId); };
"""


def harness_src(text):
    """Мини-плеер из настоящих функций шаблона плюс заглушки DOM/audio/fetch."""
    parts = [js_function(text, name) for name in NEEDED]
    return "\n".join(parts) + "\n" + STUBS


def run_node(js, script):
    if shutil.which("node") is None:
        raise SystemExit("node не найден — часть A не может быть доказана")
    d = tempfile.mkdtemp(prefix="queue_gate_")
    # гарнес и драйвер — один файл: только тогда верхнеуровневые let видны обоим
    path = os.path.join(d, "stand.js")
    body = ("globalThis.__out = [];\n" + js + "\n(async()=>{\n" + script +
            "\n})().then(()=>process.stdout.write(JSON.stringify(__out)))"
            ".catch(e=>{console.error('ДРАЙВЕР:', e); process.exit(2);});\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    r = subprocess.run([os.environ.get("NODE", "node"), path],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        print(r.stdout[-2000:])
        print(r.stderr[-3000:])
        raise SystemExit("node-стенд упал")
    shutil.rmtree(d, ignore_errors=True)
    return json.loads(r.stdout)


DRIVER = r"""
function mk(n, off){
  const out = [];
  for(let i=0;i<n;i++) out.push({id: 't'+(i+(off||0)), title: 'Т'+(i+(off||0)), artist: 'A',
    album: 'B', duration: 100, has_cover: false, filepath: 'f'+(i+(off||0))+'.mp3', track_number: i+1});
  return out;
}
function ok(name, cond, detail){ __out.push([name, !!cond, detail === undefined ? '' : String(detail)]); }
function ids(){ return queue.map(t=>t.id).join(','); }

// --- пустая очередь: контекст листается ровно как раньше --------------------
musicIndex = {tracks: mk(5)};
currentContextTracks = null;
playTrack('t2', 2); playerNext(); playerNext();
ok('A1. пустая очередь не мешает контексту', played.join(',') === 't2,t3,t4', played.join(','));
played = []; playTrack('t4', 4); playerNext();
ok('A2. на хвосте контекста «дальше» молчит', played.join(',') === 't4', played.join(','));
played = []; playerPrev();
ok('A3. «назад» по контексту работает как раньше', played.join(',') === 't3', played.join(','));

// --- добавление --------------------------------------------------------------
reset();
await addToQueue('t7');
ok('A4. отсутствующий трек не добавляется и говорит об этом',
   queue.length === 0 && notifications.some(m=>/не найден/.test(m)), notifications.join(' | '));
ok('A5. для него догружен полный индекс', fullIndexLoaded >= 1, fullIndexLoaded);
musicFullIndex = {tracks: mk(30)};
fullIndexLoaded = 0;
reset();
await addToQueue('t20');
const loadsAfterFirst = fullIndexLoaded;
await addToQueue('t21');
ok('A6. известному треку повторная догрузка не нужна',
   fullIndexLoaded === loadsAfterFirst && ids() === 't20,t21', fullIndexLoaded + ' ' + ids());
await queueMultipleTracks(['t22','t99']);
ok('A7. трек с другой страницы добавлен', ids() === 't20,t21,t22', ids());
ok('A8. массовое добавление считает пропущенные',
   queue.length === 3 && notifications.some(m=>/не найден/.test(m)), notifications.join(' | '));
ok('A9. счётчик на панели пересобран', el('queueCount').textContent === 3, el('queueCount').textContent);

// --- очередь перехватывает плеер --------------------------------------------
reset();
playTrack('t2', 2);
playerNext();
ok('A10. «дальше» уходит в голову очереди', queueIndex === 0 && played.join(',') === 't2,t20', played.join(','));
ok('A11. порядок воспроизведения = очередь',
   currentPlaylist.map(t=>t.id).join(',') === 't20,t21,t22', currentPlaylist.map(t=>t.id).join(','));
playerNext(); playerNext();
ok('A12. очередь доиграна до конца', queueIndex === 2 && played.join(',') === 't2,t20,t21,t22', played.join(','));
playerNext();
ok('A13. после конца очередь возвращается в контекст',
   played.join(',') === 't2,t20,t21,t22,t3', played.join(','));
ok('A14. очередь больше не перехватывает «дальше»',
   queueFinished === true && queueIndex === -1, queueFinished + '/' + queueIndex);
playerNext();
ok('A15. контекст после возврата листается сам',
   played.join(',') === 't2,t20,t21,t22,t3,t4', played.join(','));

// --- «назад» с головы очереди ------------------------------------------------
reset();
queueFinished = false;
playTrack('t2', 2); playQueueItem(0);
playerPrev();
ok('A16. с головы очередь отдаёт трек, на котором выросла',
   played.join(',') === 't2,t20,t2' && queueIndex === -1, played.join(',') + ' idx=' + queueIndex);

// --- удаление ----------------------------------------------------------------
reset();
queue = mk(4).slice(); queueIndex = 1; currentTrack = queue[1];
currentPlaylist = queue.slice(); currentTrackIndex = 1;
removeFromQueue(0);
ok('A17. удаление ряда перед указателем сдвигает указатель',
   queueIndex === 0 && ids() === 't1,t2,t3', queueIndex + ' ' + ids());
removeFromQueue(0);
ok('A18. удаление играющего ряда оставляет указатель за ним',
   queueIndex === -1 && ids() === 't2,t3', queueIndex + ' ' + ids());
played = []; playerNext();
ok('A19. после удаления играющего «дальше» продолжает с подъехавшего',
   played.join(',') === 't2', played.join(',') + ' ' + ids());
reset();
queue = mk(3).slice(); queueIndex = 2; currentTrack = queue[2];
removeFromQueue(2);
ok('A20. указатель не вылезает за хвост', queueIndex === 1, queueIndex);
removeFromQueue(0); removeFromQueue(0);
ok('A21. удаление всех рядов гасит указатель',
   queue.length === 0 && queueIndex === -1, queue.length + '/' + queueIndex);
reset();
queue = mk(3).slice(); queueIndex = 1; queueFinished = true;
clearQueue();
ok('A22. clearQueue гасит указатель и «доиграно»',
   queue.length === 0 && queueIndex === -1 && queueFinished === false,
   [queue.length, queueIndex, queueFinished].join('/'));

// --- перетаскивание ----------------------------------------------------------
reset();
queue = mk(5).slice(); queueIndex = 1; currentTrack = queue[1];
draggedQueueIdx = 1; queueDrop({preventDefault(){}}, 3);
ok('A23. ряд переставлен', ids() === 't0,t2,t3,t1,t4', ids());
ok('A24. указатель поехал вместе со своим рядом',
   queueIndex === 3 && queue[3].id === 't1', queueIndex + ' ' + ids());
reset();
queue = mk(5).slice(); queueIndex = 3; currentTrack = queue[3];
draggedQueueIdx = 0; queueDrop({preventDefault(){}}, 4);
ok('A25. чужая перестановка сдвигает указатель', queueIndex === 2 && queue[2].id === 't3', queueIndex + ' ' + ids());
reset();
queue = mk(5).slice(); queueIndex = 1; currentTrack = queue[1];
draggedQueueIdx = 4; queueDrop({preventDefault(){}}, 0);
ok('A26. перенос из хвоста в голову сдвигает указатель', queueIndex === 2 && queue[2].id === 't1', queueIndex + ' ' + ids());
reset();
queue = mk(5).slice(); queueIndex = -1;
draggedQueueIdx = 2; queueDrop({preventDefault(){}}, 0);
ok('A27. перестановка без указателя его не создаёт', queueIndex === -1, queueIndex);

// --- перемешивание -----------------------------------------------------------
reset();
queue = mk(8).slice(); queueIndex = 2; currentTrack = queue[2];
for(let k=0;k<30;k++) shuffleQueue();
ok('A28. перемешивание не теряет и не плодит ряды',
   queue.length === 8 && new Set(queue.map(t=>t.id)).size === 8, queue.length);
ok('A29. текущий ряд при перемешивании остаётся на месте',
   queue[2].id === 't2' && queueIsPlaying() === true, ids());
ok('A30. хвост за указателем перемешан', ids() !== 't0,t1,t2,t3,t4,t5,t6,t7', ids());
reset();
queue = mk(9).slice(); queueIndex = -1;
shuffleQueue();
ok('A31. пустой указатель перемешивает весь список',
   ids() !== 't0,t1,t2,t3,t4,t5,t6,t7,t8' && queue.length === 9, ids());

// --- клик по ряду ------------------------------------------------------------
reset();
musicIndex = {tracks: mk(5)};
queue = mk(4).slice(); queueIndex = -1;
renderQueue();
ok('A32. ряд очереди кликабелен',
   /onclick="playQueueItem\(\d+\)"/.test(el('queueBody').innerHTML), el('queueBody').innerHTML.slice(0, 160));
playQueueItem(2);
ok('A33. клик по ряду играет его и ставит указатель',
   queueIndex === 2 && played.join(',') === 't2' && queueFinished === false,
   queueIndex + ' ' + played.join(','));

// --- режим «перемешать» в плеере --------------------------------------------
reset();
isShuffle = true;
musicFullIndex = {tracks: mk(10)};
queue = mk(6).slice(); queueIndex = 0; currentTrack = queue[0];
currentPlaylist = queue.slice(); currentTrackIndex = 0;
const seen = {};
for(let k=0;k<40;k++){ played = []; playerNext(); if(played[0]) seen[played[0]] = 1; }
ok('A34. перемешивание в очередном режиме даёт разные дорожки',
   Object.keys(seen).length > 1, Object.keys(seen).join(','));
ok('A35. и все они из очереди',
   Object.keys(seen).every(id=>queue.some(t=>t.id===id)), Object.keys(seen).join(','));
isShuffle = false;

// --- кроссфейд ---------------------------------------------------------------
reset();
crossfadeDuration = 1; isCrossfading = false;
audio = {src: '/api/media/stream/f0.mp3', volume: 0, playbackRate: 1, play(){ return Promise.resolve(); }, pause(){}};
queue = mk(4).slice(); queueIndex = 0; currentTrack = queue[0];
currentPlaylist = queue.slice(); currentTrackIndex = 0;
startCrossfade();
await new Promise(r=>setTimeout(r, 1400));
ok('A36. кроссфейд довёл следующий ряд очереди до основного элемента',
   /f1\.mp3/.test(audio.src), audio.src);
ok('A37. очередь уехала вместе с рядом',
   queueIndex === 1 && currentTrack.id === 't1' && queueIsPlaying() === true,
   queueIndex + ' ' + (currentTrack && currentTrack.id));
ok('A38. шапка плеера показывает ряд очереди',
   el('playerTitle').textContent === 'Т1', el('playerTitle').textContent);
ok('A39. перемешивание через Math.random()-sort больше не используется',
   /sort\(\(\)=>Math\.random/.test(String(shuffleQueue)) === false, '');
"""


def main():
    text = TPL.read_text(encoding="utf-8-sig")

    section("A. Машина состояний очереди (node, реальный код шаблона)")
    expected = len(re.findall(r"^ok\('", DRIVER, re.M))
    try:
        rows = run_node(harness_src(text), DRIVER)
    except SystemExit as e:
        print("стенд не запущен: %s" % e)
        rows = []
    for name, good, detail in rows:
        check(name, good, detail)
    check("A0. стенд прогнал все проверки (%d)" % expected, len(rows) >= expected,
          "вернуто %d из %d" % (len(rows), expected))

    section("B. Добавление в очередь по всему индексу")
    qt = js_function(text, "queueTrackById")
    tb = js_function(text, "trackById")
    check("B1. поиск трека идёт по полному индексу (самом или через общий лукер)",
          "musicFullIndex" in qt or ("musicFullIndex" in tb and "trackById(" in qt),
          qt[:120])
    body_add = js_function(text, "addToQueue")
    check("B2. addToQueue догружает полный индекс", "ensureFullMusicIndex()" in body_add, body_add[:200])
    check("B3. пропущенный трек не молчит", "не найден" in body_add, "")
    body_multi = js_function(text, "queueMultipleTracks")
    check("B4. массовое добавление тоже догружает", "ensureFullMusicIndex()" in body_multi, "")
    check("B5. массовое добавление считает пропущенные",
          "skipped" in body_multi and "added !== trackIds.length" in body_multi, "")
    check("B6. уведомление о добавлении сохранено",
          "добавлен в очередь" in body_add and "в очередь" in body_multi, "")

    section("C. Разметка рядов очереди")
    rq = js_function(text, "renderQueue")
    check("C1. ряд подсвечивается по указателю очереди", "queueIsPlaying()" in rq, rq[:200])
    check("C2. кнопка удаления не ловит клик ряда",
          "event.stopPropagation();removeFromQueue" in rq, "")
    check("C3. пустая очередь остаётся пустым состоянием", "Очередь пуста" in rq, "")

    section("D. Контракты шаблона")
    ended = re.search(r"addEventListener\('ended',\(\)=>\{(.*?)\n\}\);", text, re.S)
    check("D1. автопереход музыки уважает «Проигрывать дальше»",
          ended is not None and "autoplayNext" in ended.group(1),
          ended.group(0)[:200] if ended else "обработчик ended не найден")
    if ended:
        check("D2. таймер сна по-прежнему останавливает раньше",
              "sleepEndOfTrack" in ended.group(1), "")
    cf = js_function(text, "startCrossfade")
    check("D3. кроссфейд уводит указатель очереди",
          "queueWasDriving" in cf and "queueIndex = currentTrackIndex" in cf, cf[:200])
    check("D4. порядок воспроизведения для кроссфейда — очередь",
          "currentPlaylist = queue.slice()" in js_function(text, "playQueueItem"), "")
    adopt = js_slice(text[text.index("function mpAdoptRemote"):], 0, "mpAdoptRemote") \
        if "function mpAdoptRemote" in text else ""
    check("D5. перенятие чужого плеера пересобирает очередь",
          "queue = currentPlaylist.slice()" in adopt and "queueIndex = currentTrackIndex" in adopt,
          adopt[:200])
    for fn in ("playTorrentFile", "playDownloadedFile"):
        b = js_slice(text[text.index("function %s(" % fn):], 0, fn) if "function %s(" % fn in text else ""
        check("D6. %s отпускает очередь" % fn, "queueFinished = true" in b, b[:120])
    hand = js_slice(text[text.index("function continueOnDesktop"):], 0, "continueOnDesktop")
    check("D7. handoff увозит играющий порядок",
          "queueIsPlaying()" in hand and "saveQueue = qPlaying ? queue : (currentPlaylist || [])" in hand,
          hand[:300])
    check("D8. указатель handoff не уезжает в -1",
          "saveIdx = qPlaying ? queueIndex : (currentTrackIndex >= 0 ? currentTrackIndex : 0)" in hand, "")
    cmd = js_slice(text[text.index("function mpHandleCommand"):], 0, "mpHandleCommand") \
        if "function mpHandleCommand" in text else ""
    check("D9. удалённый ряд очереди играется через playQueueItem",
          "queueIsPlaying() && queue[idx]" in cmd and "playQueueItem(idx)" in cmd, cmd[:200])
    check("D10. кнопка «Очередь» осталась в шапке и в плеере",
          text.count('onclick="toggleQueue()"') >= 2, text.count('onclick="toggleQueue()"'))
    check("D11. указатель очереди объявлен ровно один раз",
          text.count("let queueIndex = -1") == 1 and text.count("let queueFinished = false") == 1, "")

    total = len(PASS) + len(FAIL)
    print("\nИтог: %d проверок, провалов: %d" % (total, len(FAIL)))
    if FAIL:
        for name, detail in FAIL:
            print("  - %s" % name)
        print("Гейт очереди воспроизведения КРАСНЫЙ")
        return 1
    print("Гейт очереди воспроизведения зелёный")
    return 0


if __name__ == "__main__":
    sys.exit(main())
