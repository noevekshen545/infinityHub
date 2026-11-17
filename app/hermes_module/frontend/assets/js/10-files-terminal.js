/* 10-files-terminal.js — Файлы агента, diff, терминал, горячие клавиши */
/* ============================================================
   ФАЙЛЫ АГЕНТА: чипы, просмотр, правка (Monaco) и diff
   ============================================================
   Агент создаёт файлы и сообщает путь. Здесь этот путь
   превращается в кликабельный чип, а по клику открывается
   панель, подходящая по типу файла:
     html  → рендер в iframe
     текст/код → редактор Monaco
     картинка → просмотр изображения
     прочее → сведения + кнопка «Скачать»
   ============================================================ */

/* ---- Ленивая загрузка локальных библиотек из /vendor ---- */
const Vendor={monaco:null,term:null,fit:null};
function loadScript(src){
  return new Promise((res,rej)=>{
    const s=document.createElement('script');
    s.src=src; s.async=true;
    s.onload=()=>res();
    s.onerror=()=>rej(new Error('Не загрузилась библиотека: '+src));
    document.head.appendChild(s);
  });
}
function loadStyle(href){
  return new Promise((res,rej)=>{
    const l=document.createElement('link');
    l.rel='stylesheet'; l.href=href;
    l.onload=()=>res(); l.onerror=()=>rej(new Error('Не загрузился стиль: '+href));
    document.head.appendChild(l);
  });
}
/* Monaco грузится через AMD-загрузчик: worker и языки тянутся с того же /vendor */
function vendorMonaco(){
  if(Vendor.monaco)return Promise.resolve(Vendor.monaco);
  if(window.__infMonaco)return Promise.resolve(window.__infMonaco);
  if(IS_FILE_PROTOCOL)return Promise.reject(new Error('Monaco доступен только при запуске через веб-сервер'));
  return new Promise((res,rej)=>{
    const s=document.createElement('script');
    s.src='/vendor/monaco/vs/loader.js';
    s.onload=()=>{
      if(!window.require)return rej(new Error('AMD-загрузчик Monaco не инициализирован'));
      /* Локальный воркер: без него ломается подсветка JSON/TS/HTML.
         ВАЖНО: внутри blob-воркера относительные URL недопустимы —
         importScripts считает их от blob:, поэтому путь должен быть
         абсолютным (нов URL(..., location.href)). */
      const absRoot=new URL('/vendor/monaco/',location.href).href;
      const absWorker=new URL('/vendor/monaco/vs/base/worker/workerMain.js',location.href).href;
      window.MonacoEnvironment={
        baseUrl:absRoot,
        getWorkerUrl:function(){ return absWorker },
        getWorker:function(moduleId,label){
          return new Worker(absWorker,{name:label||'monaco-worker'});
        }
      };
      window.require.config({paths:{vs:'/vendor/monaco/vs'}});
      window.require(['vs/editor/editor.main'],function(){
        window.__infMonaco=window.monaco;
        Vendor.monaco=window.monaco;
        res(window.monaco);
      },function(e){ rej(e instanceof Error?e:new Error(String(e&&e.messageText||e))) });
    };
    s.onerror=()=>rej(new Error('Не загрузился Monaco (/vendor/monaco/vs/loader.js)'));
    document.head.appendChild(s);
  });
}
function vendorXterm(){
  if(Vendor.term)return Promise.resolve(Vendor);
  if(IS_FILE_PROTOCOL)return Promise.reject(new Error('Терминал доступен только при запуске через веб-сервер'));
  return Promise.all([loadStyle('/vendor/xterm/xterm.css'),loadScript('/vendor/xterm/xterm.js'),loadScript('/vendor/xterm/addon-fit.js')])
    .then(()=>{
      /* UMD аддона кладёт в global не сам класс, а модуль:
         window.FitAddon = {__esModule:true, FitAddon: class}. У xterm.js
         наоборот — класс разложен по глобалу. Учитываем оба случая. */
      Vendor.term=window.Terminal;
      const f=window.FitAddon;
      Vendor.fit=(f&&typeof f==='function')?f:(f&&typeof f.FitAddon==='function'?f.FitAddon:null);
      if(!Vendor.term)throw new Error('xterm.js не отдал конструктор Terminal');
      if(!Vendor.fit)throw new Error('xterm-addon-fit не отдал конструктор FitAddon');
      return Vendor;
    });
}

/* ---- Типы файлов: иконка, цвет, язык Monaco, режим открытия ---- */
const FILE_KINDS=[
  {re:/\.html?$|\.shtml$/i,        kind:'html',ico:'fa-brands fa-html5',      cls:'html',mode:'preview',lang:'html'},
  {re:/\.(png|jpe?g|gif|webp|bmp|svg|ico|avif)$/i,kind:'img', ico:'fa-regular fa-image', cls:'img', mode:'image', lang:''},
  {re:/\.css$|\.scss$|\.sass$|\.less$/i,             kind:'code',ico:'fa-solid fa-palette',  cls:'code',mode:'edit',lang:'css'},
  {re:/\.(js|mjs|cjs|jsx|ts|tsx|json|jsonc|map)$/i,  kind:'code',ico:'fa-solid fa-code',     cls:'code',mode:'edit',lang:'javascript'},
  {re:/\.(py|pyw|rb|php|pl|lua|sh|bash|zsh|ps1|bat|cmd)$/i,kind:'code',ico:'fa-solid fa-terminal',cls:'code',mode:'edit',lang:'python'},
  {re:/\.(md|markdown|rst|txt|log|text|ini|cfg|conf|toml|env|gitignore)$/i,kind:'doc',ico:'fa-solid fa-file-lines',cls:'doc',mode:'edit',lang:'markdown'},
  {re:/\.(csv|tsv)$/i,                              kind:'data',ico:'fa-solid fa-table-cells',cls:'data',mode:'edit',lang:'csv'},
  {re:/\.(xml|yml|yaml)$/i,                         kind:'data',ico:'fa-solid fa-code-branch',cls:'data',mode:'edit',lang:'xml'},
  {re:/\.(sql|db|sqlite3?)$/i,                      kind:'data',ico:'fa-solid fa-database',cls:'data',mode:'bin'},
  {re:/\.pdf$/i,                                    kind:'bin', ico:'fa-regular fa-file-pdf',cls:'bin', mode:'bin'},
  {re:/\.(zip|7z|rar|tar|gz|exe|dll|bin|iso|mp3|mp4|woff2?|ttf)$/i,kind:'bin',ico:'fa-solid fa-file-zipper',cls:'bin',mode:'bin'}
];
function fileKind(path){
  const p=String(path||'');
  for(const k of FILE_KINDS) if(k.re.test(p)) return k;
  /* без расширения — пробуем как обычный текст */
  if(!/\.[a-z0-9]{1,6}$/i.test(p)) return {kind:'doc',ico:'fa-solid fa-file-lines',cls:'doc',mode:'edit',lang:'plaintext'};
  return {kind:'bin',ico:'fa-solid fa-file',cls:'bin',mode:'bin',lang:''};
}
const fileName=p=>String(p||'').split(/[\\/]/).pop()||String(p||'');
const fileExt =p=>{const n=fileName(p);const i=n.lastIndexOf('.');return i>0?n.slice(i+1).toLowerCase():''};

/* ---- Izvlechenie putej k failam iz teksta agenta ----
   Ranshe put otrezalsya lyubym probelom, poetomu Windows-puti vida
   "C:\Users\Ivan Petrov\a.html" ne raspoznavalis voobsche. Teper razbor
   idet ot putevogo prefiksa do KONCA RASSHIRENIYA, a ne do konta stroki:
   probely vnutri putya razresheny, a vsyakoe slovo posle rasshireniya
   ("/a/b.html chtoby posmotret") otbivaetsya. */
const FILE_WRITER_RE=/^(write_file|patch|edit_file|create_file|write_text|append_file|apply_patch|str_replace|save_file)$/i;
const FILE_ARG_KEYS=['file_path','filePath','path','filename','fileName','file','target','target_file','notebook_path'];
const KNOWN_EXTS='html|htm|shtml|php|xml|svg|json|jsonc|map|yaml|yml|toml|ini|cfg|conf|env|txt|md|markdown|rst|text|log|csv|tsv|js|mjs|cjs|jsx|ts|tsx|py|pyw|rb|pl|lua|sh|bash|zsh|ps1|bat|cmd|sql|rs|go|java|c|cpp|h|hpp|cs|swift|kt|vue|css|scss|sass|less|png|jpg|jpeg|gif|webp|bmp|ico|avif|pdf|zip|7z|rar|tar|gz|exe|dll|bin|iso|mp3|mp4|woff|woff2|ttf|db|sqlite|sqlite3|lock|gitignore';
const KNOWN_EXTS_RE=new RegExp('^(?:'+KNOWN_EXTS+')$');
const EXT_RE=/\.([a-z0-9]{2,10})(?![A-Za-z0-9])/gi;
/* Otschetlivaya (bez 'g') kopiya: String.match s flagom 'g' ne daet gruppy m[1] */
const EXT_ONE_RE=/\.([a-z0-9]{2,10})(?![A-Za-z0-9])/i;

function looksLikePath(t){
  const s=String(t||'').trim().replace(/[.,;:!?)\]}>]+$/,'');
  if(!s||s.length>400)return false;
  if(/^(https?|file|data|mailto|tel):/i.test(s))return false;
  const m=s.match(EXT_ONE_RE);
  if(!m)return false;
  if(!KNOWN_EXTS_RE.test(m[1].toLowerCase()))return false;
  return /(?:[A-Za-z]:[\\/]|~[\\/]|\.{1,2}[\\/]|[\\/])/.test(s);
}

/* Nachalo puti v fragmente teksta: berem SAMYY LEVYYI putevoy prefiks.
   Dlya "C:\Users\Ivan Petrov\site.html" eto "C:\", a ne "\site.html":
   kazhdyy separator hotya i podhodil pod pattern, vziat by nuzhno pervyy.
   Slash schitaetsya nachalom puti tolko esli pered nim nichego net
   (nachalo stroki, probel ili punktuaciya) ? inache eto vnutrenniy
   separator "C:\Users\me\file.html". */
function pathStartIndex(s){
  /* C:\, D:/ ? nachalo puti vsegda za pervym takim prefiksom */
  const drive=/[A-Za-z]:[\\/]/g;
  let m=drive.exec(s);
  if(m)return m.index;
  /* ~/ i ./, ../ ? idut posle lyubogo razdeletelya */
  const rel=/(?:^|[\s(<\[{"'`=,;:!?])(~[\\/]|\.\.?[\\/])/g;
  while((m=rel.exec(s))!==null) return m.index+1;
  /* Absolyutnyy / ? schitaetsya nachalom tolko posle razdeletelya,
     inache eto vnutrenniy separator "C:\Users\me\file.html" */
  const abs=/(?:^|[\s(<\[{"'`=,;:!?])(\/)/g;
  while((m=abs.exec(s))!==null) return m.index+1;
  return -1;
}

/* Vydelyaet puti iz svobodnogo teksta.

   Algoritm: idem po vseim rasshireniyam teksta, ot kazhdogo vozvratsya
   nazad k nachalu puti (pathStartIndex) i berem fragment do KONCA
   rasshireniya. Dalee ? reshenie, kak postepenno otkryt ili otrezat pravey
   chast: snachala probuem fragment KAK EST (tak zashchityayutsya puti s
   probelami vnutri: "C:\Users\Ivan Petrov\site.html"), i tolko esli on ne
   proshol proverku ? otrezaem hvos-tovet (" - ", " i ", dva probela, slovo
   posle), chtoby "otkryl /a/b.html chtoby posmotret" dal "/a/b.html", a ne
   vsyu frazu. */
function pathsInFreeText(text){
  const out=[];
  const s=String(text||'');
  if(!s||s.length>200000)return out;
  const trimTail=c=>c
    .replace(/\s*(?:[??-]|[,;:(<{])\s*.*$/,'')
    .replace(/\s+(?:\u0438|\u0430|\u0438\s+\u0442\u0430\u043a\u0436\u0435|and|plus|\u043f\u043b\u044e\u0441)\s+.*$/i,'')
    .replace(/\s{2,}.*$/,'')
    .replace(/\s+\p{L}{3,}.*$/u,'');
  EXT_RE.lastIndex=0;
  let m;
  /* cursor ? konec uzhe najdennogo puti. Bez nego v fraze
     "sozdal C:\\x\\1.html i C:\\x\\2.txt" vtoroy put nachinals by s
     PERVOGO "C:\" i shli vosvrat k nemu zhe. */
  let cursor=0;
  while((m=EXT_RE.exec(s))!==null){
    const extEnd=m.index+m[0].length;
    if(m.index<cursor){ continue }
    const start=cursor+pathStartIndex(s.slice(cursor,m.index));
    if(start-cursor<0)continue;
    const raw=s.slice(start,extEnd);
    /* Esli vstrechaetsya soyuz mezhdu putyami ("a.html i b.txt"),
       fragment zavedomo oborvat nuzhno: inache vsy fraza prinyala bys
       za odin put. */
    const joined=/\s+(?:\u0438|\u0430|\u0438\u043b\u0438|and|or|\u043f\u043b\u044e\u0441|plus)\s+/i.test(raw);
    let cand=joined?trimTail(raw):raw;
    /* 1) kak est ? zdes rabotaet put s probelami vnutri */
    if(!looksLikePath(cand)) cand=trimTail(raw);
    if(looksLikePath(cand)&&out.indexOf(cand)<0)out.push(cand);
    cursor=extEnd;
  }
  return out;
}

function unquotePath(s){
  let t=String(s||'').trim();
  t=t.replace(/^`+|`+$/g,'').replace(/^["'<]+|["'>]+$/g,'').trim();
  return t;
}
/* Пути из аргументов инструмента (args приходят строкой JSON или объектом) */
function pathsFromArgs(args){
  const out=[];
  let o=args;
  if(typeof o==='string'){
    try{ o=JSON.parse(o) }catch(_){ return out }
  }
  if(!o||typeof o!=='object')return out;
  for(const k of FILE_ARG_KEYS){
    const v=o[k];
    if(typeof v==='string'&&looksLikePath(v))out.push(unquotePath(v));
  }
  return out;
}
/* Пути из текста. Внимание: путь НЕ должен содержать пробелов —
   без этого «Создан файл: /a/b.html — откройте» не распалось бы.
   Реальные пути с пробелами ловятся вариантом 2 (в кавычках). */
function pathsFromText(text){
  const out=[];
  const s=String(text||'');
  if(!s||s.length>200000)return out;
  const add=v=>{
    let p=unquotePath(v);
    p=p.replace(/[.,;:!?)\]}]+$/,'');
    if(looksLikePath(p)&&out.indexOf(p)<0)out.push(p);
  };
  let m;
  /* 1) put v kavychkakh ili obratnyh kavychkax ? zdes probely razresheny */
  const reQuoted=/[`"']([^`"'\r\n]{3,400}?\.[a-z0-9]{2,10})[`"']/gim;
  while((m=reQuoted.exec(s))) add(m[1]);
  /* 2) markdown-ssylki [imya](put) */
  const reLink=/\[[^\]\r\n]{0,120}\]\(([^)\r\n]{3,400})\)/g;
  while((m=reLink.exec(s))) add(m[1]);
  /* 3) svobodnyy tekst: idem po rasshireniyam i ot kazhdogo vozvratsya nazad
        k nachalu puti. Tak nachinayutsya ?C:\a b\x.html otkryt? i ?sozdal
        /p/q.html chtoby posmotret? */
  pathsInFreeText(s).forEach(add);
  return out;
}

/* ============================================================
   ПАНЕЛЬ ФАЙЛА: просмотр / правка / diff
   ============================================================ */
let DF=null;   /* состояние открытой панели */
function cssVar(n){ return getComputedStyle(document.body).getPropertyValue(n).trim() }
function isDarkTheme(){
  const t=S().general.theme;
  return t==='dark'||(t==='auto'&&matchMedia('(prefers-color-scheme: dark)').matches);
}
function dfMsg(text,isErr){
  const el=$('#dfMsg'); if(!el)return;
  el.textContent=text||'';
  el.classList.toggle('on',!!text);
  el.classList.toggle('err',!!isErr);
}
function dfNote(html){
  const note=$('#dfNote'), host=$('#dfHost');
  if(note){ if(html){ note.style.display=''; note.innerHTML=html } else note.style.display='none' }
  if(host) host.style.display=html?'none':'';
}
function dfSetBadge(text,cls){
  const b=$('#dfBadge'); if(!b)return;
  b.textContent=text; b.className='df-badge '+(cls||'ro');
}
function dfShow(btnIds,tabOn){
  ['dfSave','dfAccept','dfReject','dfDownload','dfCopy'].forEach(id=>{
    const el=$('#'+id); if(el) el.style.display=btnIds.indexOf(id)>=0?'':'none';
  });
  const tabs=$('#dfTabs');
  if(tabs) tabs.style.display=tabOn?'':'none';
}
function dfDisposables(){
  if(DF){
    try{ DF.editor&&DF.editor.dispose() }catch(_){}
    try{ DF.origModel&&DF.origModel.dispose() }catch(_){}
    try{ DF.modModel&&DF.modModel.dispose() }catch(_){}
    DF.editor=null; DF.origModel=null; DF.modModel=null;
  }
  /* Чистим контейнер ВСЕГДА, даже если DF ещё нет: без этого повторное
     создание редактора падает с «Element already has context attribute» —
     Monaco вешает свой context-атрибут прямо на узел-контейнер. */
  const host=$('#dfHost');
  if(host){
    try{ host.innerHTML='' }catch(_){}
    host.removeAttribute('data-mouse-down-context');
  }
}
function closeFilePanel(save){
  if(!DF)return;
  if(save&&DF.dirty&&DF.mode==='edit'){ saveFile(); return }
  dfDisposables();
  const ov=$('#diffOv');
  if(ov){
    ov.classList.remove('open');
    /* Сбрасываем режим окна: при следующем открытии будет обычное модальное */
    ov.classList.remove('as-dock','as-full');
  }
  const dk=$('#dfDock'), fl=$('#dfFull');
  if(dk)dk.classList.remove('on');
  if(fl)fl.classList.remove('on');
  stopFileWatch();
  DF=null;
  dfMsg('');
}
function openFilePanel(path,mode){
  const p=unquotePath(path);
  if(!p)return;
  if(IS_FILE_PROTOCOL){ toast('Файлы доступны только при запуске через веб-сервер','err'); return }
  const k=fileKind(p);
  const want=mode||k.mode;
  dfDisposables();
  DF={path:p,kind:k,mode:want==='diff'?'diff':want,dirty:false,view:'side'};
  const ov=$('#diffOv');
  if(ov)ov.classList.add('open');
  const ttl=$('#dfTitle'); if(ttl)ttl.textContent=fileName(p);
  const pp=$('#dfPath'); if(pp){ pp.textContent=p; pp.title=p }
  /* Следим за изменениями файла на диске — правки агента подхватятся сразу */
  if(want==='edit'||want==='preview') startFileWatch(p);
  else stopFileWatch();
  if(want==='diff') openDiffPanel(p);
  else if(want==='image') openImagePanel(p);
  else if(want==='bin')  openBinPanel(p);
  else if(want==='preview') openPreviewPanel(p);
  else openEditPanel(p);
}

/* Текст ошибки чтения файла. Раньше пользователю показывалось сырое
   «ENOENT» из ответа сервера — непонятно, что именно сломалось. Теперь
   расшифровываем частые коды и показываем сам путь. */
function fileErrText(e,path){
  const raw=String((e&&e.message)||e||'');
  const m=raw.match(/\{\s*"error"\s*:\s*"([^"]+)"[\s\S]*?\}/);
  let code=m?m[1]:raw;
  let hint='';
  try{
    const j=JSON.parse(raw);
    if(j&&j.hint)hint=j.hint;
  }catch(_){}
  if(/не найден|ENOENT/i.test(code))
    return 'Файл не найден: '+String(path||'')+'.'+
      (hint?' '+hint:' Возможно, агент указал имя без папки или с другой рабочей директории.');
  if(/Нет доступа|EACCES/i.test(code))
    return 'Нет доступа к файлу: '+String(path||'')+'.';
  return code;
}

/* ---- Текстовый файл: Monaco, режим правки ---- */
async function openEditPanel(path){
  const k=DF.kind;
  dfSetBadge('Правка','edit');
  dfShow(['dfSave','dfDownload','dfCopy'],false);
  /* Возвращаем обычную подпись: после просмотра HTML здесь могла остаться
     «Правка исходника». */
  const sv=$('#dfSave');
  if(sv)sv.innerHTML='<i class="fa-solid fa-floppy-disk"></i> Сохранить';
  dfMsg('');
  dfNote('<i class="fa-solid fa-spinner fa-spin"></i><b>Чтение файла…</b>');
  let text;
  try{ const r=await API.fs.readText(path); text=r&&typeof r.text==='string'?r.text:'';
       if(r&&r.truncated)dfMsg('Файл показан частично — слишком большой.'); }
  catch(e){ dfNote('<i class="fa-solid fa-triangle-exclamation"></i><b>Не удалось прочитать</b>'+esc(fileErrText(e,path))); return }
  let monaco;
  try{ monaco=await vendorMonaco() }
  catch(e){ dfNote('<i class="fa-solid fa-triangle-exclamation"></i><b>Редактор недоступен</b>'+esc(fileErrText(e,path))+'<br>Скачайте файл кнопкой ниже или запустите приложение через веб-сервер.'); dfShow(['dfDownload'],false); return }
  try{
    monaco.editor.defineTheme('inf-'+(isDarkTheme()?'dark':'light'),monacoTheme());
    monaco.editor.setTheme('inf-'+(isDarkTheme()?'dark':'light'));
    const model=monaco.editor.createModel(text,k.lang||'plaintext');
    const ed=monaco.editor.create($('#dfHost'),{
      model:model, theme:'inf-'+(isDarkTheme()?'dark':'light'),
      automaticLayout:true, minimap:{enabled:false}, fontSize:12.5,
      fontFamily:getComputedStyle(document.body).getPropertyValue('--mono').trim()||'monospace',
      wordWrap:'on', scrollBeyondLastLine:false, smoothScrolling:true,
      renderWhitespace:'selection', padding:{top:10,bottom:10},
      tabSize:2, renderLineHighlight:'line', automaticIndent:true,
      unicodeHighlight:{ambiguousCharacters:false}, scrollbar:{verticalScrollbarSize:10,horizontalScrollbarSize:10}
    });
    ed.addCommand(monaco.KeyMod.CtrlCmd|monaco.KeyCode.KeyS,()=>saveFile());
    ed.onDidChangeModelContent(()=>{ if(DF){ DF.dirty=true; dfMsg('Есть несохранённые изменения') } });
    DF.editor=ed; DF.modModel=model; DF.baseText=text;
    dfNote(null);
    ed.focus();
  }catch(e){ dfNote('<i class="fa-solid fa-triangle-exclamation"></i><b>Ошибка редактора</b>'+esc(fileErrText(e,path))) }
}
function saveFile(){
  if(!DF||!DF.editor)return;
  const val=DF.editor.getValue();
  API.fs.writeText(DF.path,val).then(()=>{
    DF.dirty=false; DF.baseText=val;
    dfMsg('Сохранено'); toast('Файл сохранён: '+fileName(DF.path),'ok');
  }).catch(e=>{ dfMsg('Не сохранено: '+((e&&e.message)||e),true); toast('Ошибка сохранения','err') });
}

/* ---- Тема Monaco из CSS-переменных приложения ---- */
function monacoTheme(){
  const v=n=>cssVar(n)||'#000';
  const dark=isDarkTheme();
  return {
    base:dark?'vs-dark':'vs',
    inherit:true,
    rules:[
      {token:'',               foreground:v('--text').replace('#','')},
      {token:'comment',        foreground:v('--text-3').replace('#',''),fontStyle:'italic'},
      {token:'keyword',        foreground:v('--accent').replace('#','')},
      {token:'string',         foreground:v('--ok').replace('#','')},
      {token:'number',         foreground:v('--warn').replace('#','')},
      {token:'type',           foreground:v('--accent-3').replace('#','')},
      {token:'tag',            foreground:v('--accent').replace('#','')},
      {token:'attribute.name', foreground:v('--accent-2').replace('#','')},
      {token:'delimiter',      foreground:v('--text-2').replace('#','')}
    ],
    colors:{
      'editor.background':          v('--code-bg'),
      'editor.foreground':          v('--code-fg'),
      'editorLineNumber.foreground':v('--text-3'),
      'editorLineNumber.activeForeground':v('--text-2'),
      'editor.lineHighlightBackground':v('--surface-2'),
      'editorCursor.foreground':    v('--accent'),
      'editor.selectionBackground': v('--accent-soft'),
      'editorIndentGuide.background1':v('--border'),
      'editorWidget.background':    v('--surface'),
      'editorWidget.border':        v('--border'),
      'editorSuggestWidget.background':v('--surface-2'),
      'editorSuggestWidget.border': v('--border'),
      'input.background':           v('--surface-2'),
      'input.border':               v('--border'),
      'focusBorder':                v('--accent'),
      'scrollbarSlider.background':  v('--border-2')+'80',
      'scrollbarSlider.hoverBackground':v('--text-3')+'99',
      'diffEditor.insertedTextBackground':v('--ok-soft'),
      'diffEditor.removedTextBackground':v('--err-soft'),
      'diffEditor.insertedLineBackground':v('--ok-soft')+'55',
      'diffEditor.removedLineBackground':v('--err-soft')+'55',
      'diffEditor.diagonalFill':    v('--border')
    }
  };
}
/* Пересоздать тему Monaco при смене светлой/тёмной (вызов из applyTheme) */
function refreshVendorThemes(){
  if(!DF||!DF.editor||!Vendor.monaco)return;
  try{
    const name='inf-'+(isDarkTheme()?'dark':'light');
    Vendor.monaco.editor.defineTheme(name,monacoTheme());
    Vendor.monaco.editor.setTheme(name);
    if(DF.editor.updateOptions)DF.editor.updateOptions({theme:name});
  }catch(_){}
}

/* ---- HTML: рендер в iframe + возможность правки исходника ---- */
async function openPreviewPanel(path){
  const k=DF.kind;
  /* HTML/SVG смотрим как страницу, всё остальное текстовое (txt, py, js,
     json, md…) — как исходник. Раньше режим был только для HTML, поэтому
     кнопка «глаз» на .py/.js показывала пустой iframe. */
  const asPage=k.kind==='html';
  dfSetBadge(asPage?'Просмотр HTML':'Просмотр','ro');
  dfShow(['dfCopy','dfDownload'],false);
  dfMsg('');
  let text;
  try{ const r=await API.fs.readText(path); text=r&&typeof r.text==='string'?r.text:'';
       if(r&&r.truncated)dfMsg('Файл показан частично — слишком большой.'); }
  catch(e){ dfNote('<i class="fa-solid fa-triangle-exclamation"></i><b>Не удалось прочитать</b>'+esc(fileErrText(e,path))); return }
  DF.source=text;
  const body=$('#dfBody');
  dfNote(null);
  const host=$('#dfHost');
  if(host)host.style.display='';
  if(asPage){
    host.innerHTML='<iframe id="dfFrame" sandbox="allow-scripts allow-modals allow-forms allow-popups" style="width:100%;height:100%;border:none;background:#fff"></iframe>';
    const fr=$('#dfFrame');
    if(fr)fr.srcdoc=text;
  }else{
    /* Исходник моноширинным: номера строк помогают ссылаться на место
       правки в разговоре с агентом. */
    host.innerHTML='<div class="df-src"><pre>'+esc(text)+'</pre></div>';
  }
  /* Кнопка в панели действий превращается в «Правка исходника».
     ВАЖНО: обработчик onclick здесь НЕ назначаем — он уже задан в
     bindFilePanel и сам умеет переключать режим preview → edit.
     Переназначение ломало бы последующую правку того же файла. */
  const save=document.getElementById('dfSave');
  if(save) save.innerHTML='<i class="fa-solid fa-code"></i> Правка исходника';
}

/* ---- Картинка ---- */
async function openImagePanel(path){
  dfSetBadge('Изображение','ro');
  dfShow(['dfDownload'],false);
  dfMsg('');
  dfNote('<i class="fa-solid fa-spinner fa-spin"></i><b>Загрузка изображения…</b>');
  try{
    const r=await API.fs.readDataURL(path);
    const url=(r&&(r.dataUrl||r.data_url||r.url))||'';
    if(!url){ dfNote('<i class="fa-solid fa-image"></i><b>Изображение не прочитано</b>Проверьте путь к файлу.'); return }
    dfNote(null);
    const host=$('#dfHost');
    if(host){ host.style.display=''; host.innerHTML='<div style="display:grid;place-items:center;height:100%;overflow:auto;background:var(--surface-2)"><img src="'+esc(url)+'" style="max-width:100%;max-height:100%;border-radius:8px" alt=""></div>' }
  }catch(e){ dfNote('<i class="fa-solid fa-triangle-exclamation"></i><b>Не удалось открыть</b>'+esc(fileErrText(e,path))) }
}

/* ---- Прочее (pdf, архивы, бинарники): сведения и «Скачать» ---- */
async function openBinPanel(path){
  dfSetBadge('Файл','ro');
  dfShow(['dfDownload'],false);
  dfMsg('');
  const k=DF.kind;
  let size=null;
  try{ const r=await API.fs.list(String(path).replace(/[\\/][^\\/]*$/,''));
       const e=(r&&r.entries||[]).find(x=>String(x.name)===fileName(path));
       if(e)size=e.size; }catch(_){}
  dfNote('<i class="'+esc(k.ico)+'"></i><b>'+esc(fileName(path))+'</b>'+
    (size!=null?esc(fmtBytes(size))+' · ':'')+
    'Формат <code>'+esc(fileExt(path)||'без расширения')+'</code> не открывается во встроенном редакторе.<br>'+
    'Нажмите «Скачать», чтобы открыть файл внешней программой.');
}

/* ---- Diff с версией в Git ---- */
/* Бэкенд отдаёт {diff:"<unified diff>"} — восстанавливаем обе стороны,
   иначе createDiffEditor не с чем сравнивать. */
function parseUnifiedDiff(text){
  const lines=String(text||'').replace(/\r\n/g,'\n').split('\n');
  const orig=[], mod=[];
  let oStart=0,mStart=0;
  for(const ln of lines){
    if(ln.indexOf('@@')===0){
      const m=ln.match(/^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@/);
      if(m){ oStart=parseInt(m[1],10)||0; mStart=parseInt(m[3],10)||0 }
      continue;
    }
    if(ln.indexOf('diff --git')===0||ln.indexOf('index ')===0||ln.indexOf('--- ')===0||ln.indexOf('+++ ')===0
       ||ln.indexOf('new file')===0||ln.indexOf('deleted file')===0||ln.indexOf('similarity index')===0
       ||ln.indexOf('rename ')===0||ln.indexOf('old mode')===0||ln.indexOf('new mode')===0) continue;
    const c=ln.charAt(0);
    if(c==='-') orig.push(ln.slice(1));
    else if(c==='+') mod.push(ln.slice(1));
    else if(c==='\\') continue;          /* "\ No newline at end of file" */
    else { orig.push(ln.slice(1)); mod.push(ln.slice(1)) }  /* контекст */
  }
  return {original:orig.join('\n'), modified:mod.join('\n')};
}
async function openDiffPanel(path){
  dfSetBadge('Сравнение','diff');
  dfShow(['dfAccept','dfReject','dfDownload'],true);
  dfMsg('');
  dfNote('<i class="fa-solid fa-spinner fa-spin"></i><b>Запрос версии из Git…</b>');
  let root='', rel=path, raw='';
  try{
    const g=await API.fs.gitRoot(path);
    root=(g&&(g.root||g.gitRoot||g.path))||'';
    if(root){
      const norm=String(path).replace(/\\/g,'/');
      const rnorm=String(root).replace(/\\/g,'/').replace(/\/+$/,'');
      rel=norm.toLowerCase().indexOf(rnorm.toLowerCase())===0?norm.slice(rnorm.length).replace(/^\/+/,''):path;
    }
  }catch(_){}
  if(!root){
    dfNote('<i class="fa-solid fa-code-compare"></i><b>Сравнение недоступно</b>Папка с файлом не является git-репозиторием.<br>Откройте файл для просмотра и правки.');
    return;
  }
  try{ const d=await API.git.fileDiff(root,rel); raw=(d&&d.diff)||''; }
  catch(e){ dfNote('<i class="fa-solid fa-triangle-exclamation"></i><b>Не удалось получить diff</b>'+esc(fileErrText(e,path))); return }
  if(!raw||!raw.trim()){
    dfNote('<i class="fa-solid fa-check"></i><b>Изменений нет</b>Файл совпадает с версией в Git.');
    return;
  }
  const cur=parseUnifiedDiff(raw);
  let monaco;
  try{ monaco=await vendorMonaco() }
  catch(e){ dfNote('<i class="fa-solid fa-triangle-exclamation"></i><b>Редактор недоступен</b>'+esc(fileErrText(e,path))); return }
  try{
    const name='inf-'+(isDarkTheme()?'dark':'light');
    monaco.editor.defineTheme(name,monacoTheme());
    monaco.editor.setTheme(name);
    const lang=DF.kind.lang||'plaintext';
    const om=monaco.editor.createModel(cur.original,lang);
    const mm=monaco.editor.createModel(cur.modified,lang);
    const ed=monaco.editor.createDiffEditor($('#dfHost'),{
      theme:name, automaticLayout:true, readOnly:true, renderSideBySide:true,
      originalEditable:false, minimap:{enabled:false}, fontSize:12.5,
      fontFamily:getComputedStyle(document.body).getPropertyValue('--mono').trim()||'monospace',
      scrollBeyondLastLine:false, renderOverviewRuler:false, scrollbar:{verticalScrollbarSize:10}
    });
    ed.setModel({original:om,modified:mm});
    DF.editor=ed; DF.origModel=om; DF.modModel=mm;
    dfNote(null);
  }catch(e){ dfNote('<i class="fa-solid fa-triangle-exclamation"></i><b>Ошибка diff</b>'+esc(fileErrText(e,path))) }
}
function acceptDiff(){
  if(!DF||!DF.modModel)return;
  const val=DF.modModel.getValue();
  const p=DF.path;
  API.fs.writeText(p,val).then(()=>{
    toast('Изменения приняты: '+fileName(p),'ok');
    closeFilePanel(false);
  }).catch(e=>{ dfMsg('Не записано: '+((e&&e.message)||e),true); toast('Ошибка записи','err') });
}

/* ============================================================
   ЧИПЫ ФАЙЛОВ: путь из ответа агента → кликабельная иконка
   ============================================================ */
/* ============================================================
   ПОДТВЕРЖДЕНИЕ ОПАСНЫХ ДЕЙСТВИЙ
   Блок рисуется в msgHTML, а вот обработчик клика раньше отсутствовал
   вовсе: кнопки «Разрешить / Всегда / Отклонить» падали с
   ReferenceError: resolveApproval is not defined.
   Ответ уходит в runs.approval; если шлюз не знает endpoint —
   откатываемся на обычное сообщение в чат, чтобы действие не терялось.
   ============================================================ */
const APPROVAL_CHOICES={once:'Разрешено',session:'Разрешено на сеанс',always:'Разрешено всегда',deny:'Отклонено',allow:'Разрешено'};
function findApprovalMessage(id){
  const s=active(); if(!s)return null;
  return s.messages.find(m=>m.kind==='approval'&&m.id===id)||null;
}
function findClarifyMessage(id){
  const s=active(); if(!s)return null;
  return s.messages.find(m=>m.kind==='clarify'&&m.id===id)||null;
}
/* Ответ уходит точной схемой 0.23: {request_id, execution_generation, choice}.
   Раньше отправляли {choice, reason} — шлюз отвечал invalid_params, а запасной
   путь писал обычное сообщение в чат, создавая новый ход вместо ответа. */
async function resolveApproval(id,choice){
  const s=active(), m=findApprovalMessage(id);
  if(!s||!m||m.status==='done')return;
  const meta=m.meta||{}, runId=meta.runId||ui.runId||s.runId||'';
  if(!runId||!meta.requestId){ toast('Нет идентификатора запроса — ответ не отправлен','err'); return }
  const label=APPROVAL_CHOICES[choice]||choice;
  try{
    await API.runs.approval(runId,choice,meta.requestId,meta.gen);
    m.status='done'; m.meta.decision=label; patch(m); save();
    toast('Ответ отправлен: '+label,'ok');
  }catch(e){
    toast('Шлюз не принял ответ: '+((e&&e.message)||e),'err');
  }
}
function pickClarify(id,idx,btn){
  const m=findClarifyMessage(id); if(!m||m.status==='done')return;
  const ch=Array.isArray(m.meta?.choices)?m.meta.choices:[];
  if(!m.meta?.multi){ answerClarify(id,String(ch[idx]??'')); return }
  btn.classList.toggle('picked');
}
function sendClarifyAnswer(id){
  const m=findClarifyMessage(id); if(!m)return;
  let answer='';
  if(m.meta?.multi){
    const box=document.querySelector('.blk[data-id="'+id+'"]');
    answer=[...(box?box.querySelectorAll('[data-clr-pick].picked'):[])]
      .map(b=>m.meta.choices[Number(b.dataset.clrPick)]).filter(Boolean).join(', ');
  }else{
    const inp=document.getElementById('clrInput_'+id);
    answer=inp?inp.value.trim():'';
  }
  if(!answer){ toast('Выберите вариант или введите ответ','warn'); return }
  answerClarify(id,answer);
}
async function answerClarify(id,answer){
  const s=active(), m=findClarifyMessage(id);
  if(!s||!m||m.status==='done'||!answer)return;
  const meta=m.meta||{}, runId=meta.runId||ui.runId||s.runId||'';
  if(!runId||!meta.requestId){ toast('Нет идентификатора вопроса — ответ не отправлен','err'); return }
  try{
    await API.runs.clarify(runId,answer,meta.requestId,meta.gen);
    m.status='done'; m.meta.answer=answer; patch(m); save();
    toast('Ответ отправлен','ok');
  }catch(e){
    toast('Шлюз не принял ответ: '+((e&&e.message)||e),'err');
  }
}
function fileChipsHtml(paths){
  const list=[];
  for(const p of (paths||[])){
    const k=fileKind(p);
    list.push('<button class="fchip" data-fpath="'+esc(p)+'" title="'+esc(p)+'">'+
      '<span class="fico '+k.cls+'"><i class="'+esc(k.ico)+'"></i></span>'+
      '<span class="fin"><b>'+esc(fileName(p))+'</b><span>'+esc(p)+'</span></span>'+
      '<i class="fa-solid fa-chevron-right fgo"></i></button>');
  }
  return list.length?'<div class="fchips">'+list.join('')+'</div>':'';
}
/* Fakly, sozdayonnye instrumentami agenta.
   Put iz argumentov my znali vsegda, no tool-soobshheniya skryty iz chata,
   a agent ne vsegda povtoryaet put v tekste. Zdes sobiraem ix iz poslednih
   vyzovov sessii, chtoby chip appeared tam, gde polzovatel zhdet. */
function createdFilesFromTools(sess,exceptList){
  const out=[];
  if(!sess)return out;
  const calls=(sess.messages||[]).filter(m=>m.kind==='tool'&&m.status!=='running');
  for(let i=calls.length-1;i>=0&&out.length<8;i--){
    const meta=calls[i].meta||{};
    if(!FILE_WRITER_RE.test(String(meta.name||'')))continue;
    const args=meta.args||meta.input;
    const paths=pathsFromArgs(args);
    for(const pth of paths){
      if(exceptList&&exceptList.indexOf(pth)>=0)continue;
      if(out.indexOf(pth)<0)out.push(pth);
    }
  }
  return out;
}
/* Найти чипы для сообщения агента: аргументы инструментов + текст ответа */
function collectFilePaths(m){
  const out=[];
  const add=p=>{ if(p&&out.indexOf(p)<0)out.push(p) };
  if(m.meta&&m.meta.args) pathsFromArgs(m.meta.args).forEach(add);
  if(m.meta&&m.meta.input) pathsFromArgs(m.meta.input).forEach(add);
  /* Пишущие инструменты: путь берём только из аргументов, чтобы не ловить
     случайные упоминания в выводе. */
  if(m.meta&&FILE_WRITER_RE.test(String(m.meta.name||''))) return out;
  pathsFromText(m.content).forEach(add);
  return out.slice(0,24);
}
/* Клик по чипу — открыть файл (или скачать бинарный) */
document.addEventListener('click',e=>{
  const chip=e.target.closest('[data-fpath]');
  if(!chip)return;
  const p=chip.dataset.fpath;
  const k=fileKind(p);
  if(k.mode==='bin'){ downloadFile(p); return }
  openFilePanel(p);
});
function downloadFile(path){
  const url=API.baseURL+'/api/fs/download?path='+encodeURIComponent(path);
  const a=document.createElement('a');
  a.href=url; a.download=fileName(path);
  a.rel='noopener';
  document.body.appendChild(a); a.click(); a.remove();
  toast('Скачивание: '+fileName(path),'info');
}

/* ============================================================
   ЖИВОЕ ОБНОВЛЕНИЕ ОТКРЫТОГО ФАЙЛА
   Панель просмотра/правки подписывается на /api/fs/watch и подхватывает
   правки агента без перезагрузки страницы. Если SSE отвалился — тихо
   переходим на опрос по времени изменения.
   ============================================================ */
const FileWatch={es:null,path:'',timer:null,busy:false};
function stopFileWatch(){
  if(FileWatch.es){ try{ FileWatch.es.close() }catch(_){} FileWatch.es=null }
  if(FileWatch.timer){ clearTimeout(FileWatch.timer); FileWatch.timer=null }
  FileWatch.path='';
}
function startFileWatch(path){
  stopFileWatch();
  if(!path||IS_FILE_PROTOCOL)return;
  FileWatch.path=path;
  try{
    const es=new EventSource('/api/fs/watch?path='+encodeURIComponent(path));
    FileWatch.es=es;
    es.onmessage=ev=>{
      let m=null; try{ m=JSON.parse(ev.data) }catch(_){ return }
      if(!m)return;
      if(m.type==='changed') onFileChanged();
      else if(m.type==='gone'){ dfMsg('Файл удалён или переименован',true) }
    };
    /* SSE недоступен — переходим на опрос содержимого */
    es.onerror=()=>{
      try{ es.close() }catch(_){}
      if(FileWatch.es===es)FileWatch.es=null;
      schedulePoll();
    };
  }catch(_){ schedulePoll() }
}
function schedulePoll(){
  if(FileWatch.timer||!FileWatch.path)return;
  FileWatch.timer=setTimeout(async()=>{
    FileWatch.timer=null;
    if(!FileWatch.path||!DF)return;
    try{
      const r=await fetch('/api/fs/read-text?path='+encodeURIComponent(FileWatch.path));
      if(r.ok){
        const d=await r.json();
        if(d&&typeof d.text==='string'&&d.text!==DF.baseText) onFileChanged();
      }
    }catch(_){}
    schedulePoll();
  },2500);
}
/* Файл изменился на диске: обновляем панель, сохраняя курсор и скролл */
async function onFileChanged(){
  if(!DF||FileWatch.busy)return;
  if(DF.path!==FileWatch.path)return;
  if(DF.mode!=='edit'){                      /* просмотр/diff — перечитываем целиком */
    reloadFromDisk();
    return;
  }
  if(DF.dirty){
    /* Есть несохранённые правки: не затираем, а предлагаем обновить */
    dfMsg('Файл изменён на диске — ваши правки не сохранены. Обновить и потерять их?');
    const btn=document.getElementById('dfSave');
    if(btn){
      btn.dataset.prev=btn.innerHTML;
      btn.innerHTML='<i class="fa-solid fa-rotate"></i> Обновить с диска';
      btn.onclick=async()=>{
        if(DF&&DF.dirty){
          const ok=await askForm({title:'Обновить файл с диска?',icon:'fa-triangle-exclamation',
            ok:'Обновить',fields:[{name:'c',label:'Несохранённые правки будут потеряны',value:''}]});
          if(!ok)return;
        }
        reloadFromDisk();
        const b=document.getElementById('dfSave');
        if(b&&b.dataset.prev!==undefined){ b.innerHTML=b.dataset.prev; b.onclick=saveFile }
      };
    }
    return;
  }
  reloadFromDisk();
}
async function reloadFromDisk(){
  if(!DF||FileWatch.busy)return;
  FileWatch.busy=true;
  const path=DF.path;
  try{
    const r=await fetch('/api/fs/read-text?path='+encodeURIComponent(path));
    if(r.ok){
      const d=await r.json();
      if(typeof d.text==='string'&&DF&&DF.path===path){
        /* Запоминаем позицию курсора и скролл, чтобы чтение не дёргало */
        const ed=DF.editor;
        let pos=null,scroll=0;
        if(ed&&ed.getPosition&&ed.getScrollTop){ pos=ed.getPosition(); scroll=ed.getScrollTop() }
        if(DF.modModel&&DF.mode==='edit'){
          const cur=DF.modModel.getValue();
          if(cur!==d.text){
            DF.editor.setValue(d.text);
            DF.baseText=d.text; DF.dirty=false;
            if(pos){ try{ DF.editor.setPosition(pos) }catch(_){} }
            try{ DF.editor.setScrollTop(scroll) }catch(_){}
            dfMsg('Обновлено с диска · '+new Date().toLocaleTimeString('ru-RU'));
          }else dfMsg('');
        }else if(DF.mode==='preview'&&DF.editor){
          reloadPreviewFrame(d.text);
        }
      }
    }
  }catch(_){}
  finally{ FileWatch.busy=false }
}
function reloadPreviewFrame(text){
  const fr=document.getElementById('dfFrame');
  if(fr){ fr.srcdoc=text; DF.source=text; dfMsg('Обновлено с диска') }
}

/* ============================================================
   ЖИВОЙ ТЕРМИНАЛ (xterm.js)
   Псевдо-терминал: каждая команда — отдельный вызов POST /api/exec
   с тем же гейтом подтверждения, что у консоли «!команда».
   ============================================================ */
const Term={inst:null,ready:false,busy:false,runs:0,buf:'',hist:[],histPos:-1};
function termCwd(){
  const s=active();
  const proj=s&&projectOf(s);
  return (proj&&(proj.absPath||proj.path))||state.gatewayCwd||'';
}
function termCss(n,fb){ return getComputedStyle(document.body).getPropertyValue(n).trim()||fb }
function termTheme(){
  return {background:termCss('--code-bg','#0a0e16'),foreground:termCss('--code-fg','#d9e1ee'),
    cursor:termCss('--accent','#818cf8'),cursorAccent:termCss('--code-bg','#0a0e16'),
    selectionBackground:termCss('--accent-soft','#232a4a'),
    black:termCss('--text','#000'),brightBlack:termCss('--text-3','#666'),
    red:termCss('--err','#f87171'),green:termCss('--ok','#34d399'),
    yellow:termCss('--warn','#fbbf24'),blue:termCss('--accent-3','#38bdf8'),
    magenta:termCss('--accent-2','#a78bfa'),cyan:termCss('--accent-3','#38bdf8'),white:termCss('--text','#e7ebf3')};
}
function termPrompt(){
  const c=termCwd();
  return '\x1b[38;2;129;140;248m'+(c||'~')+'\x1b[0m \x1b[38;2;109;119;144m$\x1b[0m ';
}
function termWrite(txt){ try{ Term.inst&&Term.inst.write(txt) }catch(_){} }
function termSetBusy(b){
  Term.busy=!!b;
  const n=$('#termCount');
  if(n){ n.textContent=String(Term.runs); n.classList.toggle('busy',Term.busy) }
}
function toggleTerm(force){
  const d=$('#termDrawer');
  if(!d)return;
  const open=force!==undefined?force:!d.classList.contains('open');
  d.classList.toggle('open',open);
  /* Состояние переживает перезагрузку (как у панелей инструментов) */
  if(state.termDrawerOpen!==open){ state.termDrawerOpen=open; save(); }
  const btn=$('#hdTerm'); if(btn)btn.classList.toggle('on',open);
  if(open){
    setTimeout(()=>{ initTerm().then(()=>Term.inst&&Term.inst.focus())
      .catch(e=>termWrite('\x1b[31m'+((e&&e.message)||e)+'\x1b[0m\r\n')) },30);
    /* Высота drawer анимируется 250 мс. До её конца контейнер почти пуст,
       и fitAddon насчитывает rows=1 — терминал рисует одну строку. */
    setTimeout(()=>termRefit(),320);
    d.addEventListener('transitionend',function h(ev){
      if(ev.propertyName==='height'){ d.removeEventListener('transitionend',h); termRefit() }
    });
  }
}
function termRefit(){
  try{
    if(!Term.inst||!Vendor.fit)return;
    new Vendor.fit().fit();
  }catch(_){}
}
function clearTerm(){
  try{ if(Term.inst){ Term.inst.clear(); Term.inst.write('\x1b[2J\x1b[H'); termWrite(termPrompt()) } }catch(_){}
}
async function initTerm(){
  if(Term.inst&&!Term.inst._disposed)return Term.inst;
  const host=$('#termHost');
  if(!host)return null;
  const cwdEl=$('#termCwd'); if(cwdEl)cwdEl.textContent=termCwd();
  let v;
  try{ v=await vendorXterm() }catch(e){ termWrite('\x1b[31mТерминал недоступен: '+((e&&e.message)||e)+'\x1b[0m\r\n'); throw e }
  const term=new v.term({
    convertEol:true, cursorBlink:true, cursorStyle:'bar', fontSize:12.5, lineHeight:1.25,
    scrollback:5000, fontFamily:getComputedStyle(document.body).getPropertyValue('--mono').trim()||'Consolas,monospace',
    theme:termTheme(), allowProposedApi:true, macOptionIsMeta:true
  });
  const fit=new v.fit(); term.loadAddon(fit);
  term.open(host);
  /* Контейнер должен иметь реальную высоту, иначе fitAddon даёт rows=1.
     .term-host — flex:1 внутри flex-колонки drawer, этого достаточно. */
  const refit=()=>{ try{ fit.fit() }catch(_){} };
  requestAnimationFrame(()=>setTimeout(refit,0));
  setTimeout(refit,350);
  addEventListener('resize',refit);
  term.onResize(refit);
  Term.inst=term; Term.ready=true; term._disposed=false;
  termWrite('\x1b[38;2;129;140;248mInfinity\x1b[0m \x1b[38;2;109;119;144m· псевдо-терминал: каждая команда выполняется отдельным процессом\x1b[0m\r\n');
  termWrite(termPrompt());
  term.focus();
  term.onData(d=>termInput(d));
  refit();
  return term;
}
/* Ввод с клавиатуры. ВАЖНО: onData срабатывает уже ПОСЛЕ того, как xterm
   нарисовал символ у курсора. Поэтому обычные символы и Backspace
   обрабатываем только для накопления Term.buf — сам вывод на экран
   xterm делает сам (иначе символы дублируются). */
function termInput(d){
  const code=d.charCodeAt(0);
  if(code===13){ termEnter(); return }              /* Enter */
  if(code===127||code===8){                        /* Backspace */
    if(Term.buf.length)Term.buf=Term.buf.slice(0,-1);
    return;
  }
  if(code===27){                                   /* Esc */
    if(Term.busy){ termWrite('\x1b[33m^C\x1b[0m\r\n'); Term.aborted=true }
    else { Term.buf=''; termWrite('\r\x1b[K'+termPrompt()) }
    return;
  }
  if(code===3){ Term.buf=''; termWrite('\r\x1b[K'+termPrompt()); return }  /* Ctrl+C */
  if(code===12){ clearTerm(); return }              /* Ctrl+L */
  if(code===21){ return }                          /* Ctrl+U — игнор */
  if(code<32&&code!==9) return;
  if(Term.busy) return;                             /* пока идёт команда, ввод игнорируем */
  if(Term.buf.length>=400) return;
  Term.buf+=d;                                      /* экран рисует сам xterm */
}
function termEnter(){
  const cmd=Term.buf; Term.buf='';
  if(!cmd.trim()){ termWrite('\r\n'+termPrompt()); return }
  Term.hist.unshift(cmd); Term.histPos=-1;
  /* Возврат каретки: xterm сам не переносит строку, пока мы её не попросим. */
  termWrite('\r\n');
  runTermCmd(cmd);
}
async function runTermCmd(cmd){
  const cwd=termCwd();
  const to=Number((S().tools||{}).shellTimeout||60);
  termSetBusy(true);
  let res;
  try{
    res=await execPost(cmd,cwd,to,false);
    if(res.status===409){                            /* гейт: подтверждение, как в CLI */
      termWrite('\x1b[33m⚠ '+(res.j&&res.j.message||'нужно подтверждения')+'\x1b[0m\r\n');
      const ok=await askForm({title:'Выполнить команду?',icon:'fa-terminal',ok:'Выполнить',
        fields:[{name:'c',label:cmd,value:cmd}]});
      if(!ok){ termWrite('\x1b[31mотменено\x1b[0m\r\n'); termSetBusy(false); termWrite(termPrompt()); return }
      res=await execPost(cmd,cwd,to,true);
    }
    if(res.status>=400&&!res.j.out){
      termWrite('\x1b[31m'+(res.j&&(res.j.error||res.j.message)||('HTTP '+res.status))+'\x1b[0m\r\n');
    }else{
      const out=String(res.j.out||'').replace(/\s+$/,'');
      if(out)termWrite(out.replace(/\r?\n/g,'\r\n')+'\r\n');
      else termWrite('\x1b[38;2;109;119;144m(пустой вывод)\x1b[0m\r\n');
      const c=Number(res.j.code||0);
      termWrite(c===0?'\x1b[32m✓ код 0\x1b[0m · '+(res.j.dur||0)+' c\r\n'
                    :'\x1b[31m✗ код '+c+'\x1b[0m · '+(res.j.dur||0)+' c\r\n');
      if(res.j.truncated)termWrite('\x1b[33mвывод усечён\x1b[0m\r\n');
    }
  }catch(e){
    termWrite('\x1b[31mошибка запуска: '+((e&&e.message)||e)+'\x1b[0m\r\n');
  }
  Term.runs++; termSetBusy(false);
  termWrite(termPrompt());
  try{ if(Term.inst)Term.inst.focus() }catch(_){}
}

/* ============================================================
   ОБРАБОТЧИКИ: панель файла, терминал, горячие клавиши
   ============================================================ */
(function bindFilePanel(){
  const close=$('#dfClose');
  if(close)close.onclick=()=>closeFilePanel(false);
  const ov=$('#diffOv');
  if(ov)ov.addEventListener('click',e=>{
    if(e.target.id!=='diffOv')return;
    /* В режиме «панель справа» клик по свободному месту возвращает
       модальное окно, а не закрывает файл — так удобнее вернуться. */
    if(ov.classList.contains('as-dock')||ov.classList.contains('as-full')){
      ov.classList.remove('as-dock','as-full');
      const dk=$('#dfDock'), fl=$('#dfFull');
      if(dk)dk.classList.remove('on');
      if(fl)fl.classList.remove('on');
      setTimeout(()=>{ try{ DF&&DF.editor&&DF.editor.layout() }catch(_){} },60);
      return;
    }
    closeFilePanel(false);
  });

  const dl=$('#dfDownload');
  if(dl)dl.onclick=()=>{ if(DF)downloadFile(DF.path) };
  const cp=$('#dfCopy');
  if(cp)cp.onclick=()=>{
    if(!DF)return;
    const val=(DF.editor&&DF.editor.getValue&&DF.editor.getValue())||DF.source||'';
    if(!val){ toast('Нечего копировать','info'); return }
    navigator.clipboard.writeText(val).then(()=>toast('Скопировано','ok')).catch(()=>toast('Буфер обмена недоступен','err'));
  };
  const sv=$('#dfSave');
  if(sv)sv.onclick=()=>{
    if(DF&&DF.mode==='edit'){ saveFile(); return }
    /* Раньше язык принудительно ставился в html — для .py/.js/.txt это ломало
       подсветку в Monaco. Переключаемся в edit, сохраняя родной язык файла. */
    if(DF&&DF.mode==='preview'){ DF.mode='edit'; openEditPanel(DF.path) }
  };
  const ac=$('#dfAccept'); if(ac)ac.onclick=acceptDiff;
  const rj=$('#dfReject');
  if(rj)rj.onclick=()=>{ toast('Изменения отклонены','info'); closeFilePanel(false) };
  /* Режимы окна: модальное / панель справа / во весь экран.
     Панель лежит вне overlay чата и выше по z-index, поэтому чат можно
     закрыть кнопкой — панель останется на экране. */
  const dock=$('#dfDock'), full=$('#dfFull');
  const setMode=m=>{
    const ov=$('#diffOv'); if(!ov)return;
    ov.classList.toggle('as-dock',m==='dock');
    ov.classList.toggle('as-full',m==='full');
    if(dock){ dock.classList.toggle('on',m==='dock');
      const i=dock.querySelector('i'); if(i)i.className=m==='dock'?'fa-solid fa-table-columns':'fa-solid fa-table-columns' }
    if(full){ full.classList.toggle('on',m==='full');
      const i=full.querySelector('i'); if(i)i.className=m==='full'?'fa-solid fa-compress':'fa-solid fa-expand' }
    /* Monaco обязан пересчитать раскладку после смены размеров */
    setTimeout(()=>{ try{ DF&&DF.editor&&DF.editor.layout() }catch(_){} },60);
  };
  if(dock)dock.onclick=()=>{
    const ov=$('#diffOv'); if(!ov)return;
    setMode(ov.classList.contains('as-dock')?null:'dock');
  };
  if(full)full.onclick=()=>{
    const ov=$('#diffOv'); if(!ov)return;
    setMode(ov.classList.contains('as-full')?null:'full');
  };
  /* Рядом/в строке */
  const tabs=$('#dfTabs');
  if(tabs)tabs.addEventListener('click',e=>{
    const b=e.target.closest('[data-dfview]'); if(!b||!DF||!DF.editor)return;
    const v=b.dataset.dfview;
    $$('#dfTabs .df-tab').forEach(x=>x.classList.toggle('on',x===b));
    try{ DF.editor.renderSideBySide=(v==='side'); DF.editor.updateOptions&&DF.editor.updateOptions({renderSideBySide:v==='side'}) }catch(_){}
  });
  /* Ctrl+S в панели */
  document.addEventListener('keydown',e=>{
    if(!DF)return;
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){ e.preventDefault(); if(DF.mode==='edit')saveFile() }
    if(e.key==='Escape'){
      /* Esc двухуровневый: сперва снимаем «панель справа/во весь экран»,
         повторное нажатие закрывает панель. Так удобнее выйти из файла. */
      const ov=$('#diffOv');
      if(ov&&(ov.classList.contains('as-dock')||ov.classList.contains('as-full'))){
        e.stopPropagation();
        ov.classList.remove('as-dock','as-full');
        const dk=$('#dfDock'), fl=$('#dfFull');
        if(dk)dk.classList.remove('on');
        if(fl)fl.classList.remove('on');
        setTimeout(()=>{ try{ DF&&DF.editor&&DF.editor.layout() }catch(_){} },60);
        return;
      }
      e.stopPropagation(); closeFilePanel(false)
    }
  });
  /* Кнопка «Сравнить с Git» в панели инструментов */
  const td=$('#toolsDiff');
  if(td){
    td.onclick=()=>{
      /* Запомненный путь берём только если он из активной сессии — иначе
         подставляем последний изменённый файл уже её собственным вызовом. */
      const p=((ui.diffPathSession===state.activeSessionId&&ui.diffPath)||lastToolFilePath());
      if(!p){ toast('Нет файла для сравнения — сначала дождитесь записи файла агентом','info'); return }
      openFilePanel(p,'diff');
    };
    /* Тумблер «Сравнение с Git» прячет кнопку */
    const applyDiffPref=()=>{ const on=!(S().tools&&S().tools.diffEditor===false);
                             td.style.display=on?'':'none' };
    applyDiffPref();
    document.addEventListener('click',e=>{ if(e.target.closest('[data-set="tools.diffEditor"]')) setTimeout(applyDiffPref,0) });
  }
})();

function lastToolFilePath(){
  const s=toolsOwner?toolsOwner():null;
  if(!s)return '';
  const calls=s.messages.filter(m=>m.kind==='tool').reverse();
  for(const m of calls){
    const p=pathsFromArgs(m.meta&&m.meta.args).concat(pathsFromArgs(m.meta&&m.meta.input))[0];
    if(p)return p;
  }
  return '';
}

(function bindTerm(){
  const b=$('#hdTerm'); if(b)b.onclick=()=>toggleTerm();
  const c=$('#termClose'); if(c)c.onclick=()=>toggleTerm(false);
  const cl=$('#termClear'); if(cl)cl.onclick=()=>clearTerm();
  const mx=$('#termMax');
  if(mx)mx.onclick=()=>{
    const d=$('#termDrawer'); if(!d)return;
    d.classList.toggle('full');
    const ic=mx.querySelector('i');
    if(ic)ic.className=d.classList.contains('full')?'fa-solid fa-minimize':'fa-solid fa-maximize';
    setTimeout(()=>{ try{ if(Term.inst&&Vendor.fit)new Vendor.fit().fit() }catch(_){} },280);
  };
  /* Ctrl+` — открыть/закрыть терминал */
  document.addEventListener('keydown',e=>{
    if(e.ctrlKey&&e.key==='`'){ e.preventDefault(); toggleTerm() }
  });
})();

/* Публичный доступ: нужно плагинам интерфейса и удобно при отладке
   из консоли браузера (window.openFilePanel('C:/.../index.html')). */
window.InfinityFiles={
  open:openFilePanel, close:closeFilePanel, save:saveFile,
  diff:openDiffPanel, download:downloadFile,
  chips:fileChipsHtml, collect:collectFilePaths, kind:fileKind,
  openTerminal:()=>toggleTerm(true), closeTerminal:()=>toggleTerm(false),
  term:Term,
  state:()=>DF                       /* текущая открытая панель (для отладки) */
};
/* Короткие алиасы для консоли */
window.openFilePanel=openFilePanel;
window.closeFilePanel=closeFilePanel;
window.fileChipsHtml=fileChipsHtml;
window.collectFilePaths=collectFilePaths;
window.toggleTerm=toggleTerm;

/* Запускаем приложение. Без этого вызова init() не выполняется:
   плагины (PluginBus.init), голосовой ввод (fillVoices/loadDevices),
   Gateway.connect и прочая инициализация не стартуют. */
init();