/* 01-util-state.js — Утилиты, хранилище и состояние */
/* ============================================================
   UTIL
   ============================================================ */
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>Array.from(r.querySelectorAll(s));
/* Приводит любую ошибку к читаемому тексту.
   Раньше код писал «(e&&e.message)||e» — если e.message это объект
   (шлюз отдаёт {error:{message,type}}), в чат уходило «[object Object]».
   Те же шлюзы иногда присылают {error:{message:{...}}} — разворачиваем
   вложенность до строки. */
function errText(e,fallback){
  const seen=new Set();
  const dig=(v,depth)=>{
    if(v==null)return '';
    if(typeof v==='string')return v;
    if(typeof v==='number'||typeof v==='boolean')return String(v);
    if(typeof v!=='object')return String(v);
    if(depth>3||seen.has(v))return '';
    seen.add(v);
    /* Приоритет полей: самое человеческое — сверху вниз */
    for(const k of ['message','error','detail','description','reason','text','content']){
      if(v[k]!==undefined&&v[k]!==null){
        const r=dig(v[k],depth+1);
        if(r)return r;
      }
    }
    if(Array.isArray(v)){ const parts=v.map(x=>dig(x,depth+1)).filter(Boolean); if(parts.length)return parts.join('; ') }
    for(const k in v){
      if(k==='type'||k==='code'||k==='status'||k==='status_code')continue;
      const r=dig(v[k],depth+1);
      if(r)return r;
    }
    return '';
  };
  const out=dig(e,0);
  return out||(fallback||'Неизвестная ошибка');
}
const uid=p=>(p||'id')+'_'+Math.random().toString(36).slice(2,9)+Date.now().toString(36).slice(-3);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const getPath=(o,p)=>p.split('.').reduce((a,k)=>(a==null?a:a[k]),o);
const setPath=(o,p,v)=>{const k=p.split('.');let c=o;for(let i=0;i<k.length-1;i++)c=c[k[i]]??={};c[k[k.length-1]]=v;};
const fmtBytes=b=>b<1024?b+' Б':b<1048576?(b/1024).toFixed(1)+' КБ':(b/1048576).toFixed(2)+' МБ';
const tShort=ts=>new Date(ts).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
const tDate=ts=>new Date(ts).toLocaleDateString('ru-RU',{day:'2-digit',month:'short'});
function ago(ts){const d=(Date.now()-ts)/1000;if(d<60)return'только что';if(d<3600)return Math.floor(d/60)+' мин';
  if(d<86400)return Math.floor(d/3600)+' ч';if(d<604800)return Math.floor(d/86400)+' дн';return tDate(ts);}

const IS_FILE_PROTOCOL = window.location.protocol === 'file:';
if(IS_FILE_PROTOCOL) {
  const w = $('#fileWarn'); if(w) w.classList.add('show');
  setTimeout(()=>{ if(w) w.classList.remove('show') }, 8000);
}

function hlCode(code){
  let s=esc(code);
  s=s.replace(/(\/\/[^\n]*)/g,'<span class="tok-c">$1</span>');
  s=s.replace(/(#[^\n]*)/g,'<span class="tok-c">$1</span>');
  s=s.replace(/(\/\*[\s\S]*?\*\/)/g,'<span class="tok-c">$1</span>');
  s=s.replace(/('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`)/g,'<span class="tok-s">$1</span>');
  s=s.replace(/\b(\d+\.?\d*)\b/g,'<span class="tok-n">$1</span>');
  s=s.replace(/\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|try|catch|throw|new|class|extends|import|export|from|default|async|await|yield|of|in|typeof|instanceof|void|null|undefined|true|false|this|super|def|as|elif|except|finally|with|lambda|pass|and|or|not|is|True|False|None|self|print)\b/g,'<span class="tok-k">$1</span>');
  s=s.replace(/\b([a-zA-Z_][a-zA-Z0-9_]*)\s*(?=\()/g,'<span class="tok-f">$1</span>');
  return s;
}

function fmt(text){
  let s=esc(text);
  s=s.replace(/```(\w+)?\n?([\s\S]*?)```/g,(m,lang,c)=>'<pre class="code" data-lang="'+(lang||'text')+'"><button class="copy" data-copy><i class="fa-regular fa-copy"></i> Copy</button><code>'+hlCode(c.replace(/^\n/,'').replace(/\n$/,''))+'</code></pre>');
  s=s.replace(/`([^`\n]+)`/g,'<code class="inline">$1</code>');
  /* Картинки агента: 0.23 отдаёт MEDIA: как markdown с data-URL (в т.ч. base64).
     Рисуем только data:image/* — удалённые src не подставляем, чтобы ответ
     агента не тянул трекинг-запросы. Текст уже экранирован, кавычка в src
     невозможна (&quot;). Клик открывает существующий просмотрщик. */
  s=s.replace(/!\[([^\]]*)\]\((data:image\/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+)\)/gi,
    (m,alt,src)=>'<img class="msg-md-img" src="'+src.replace(/\s+/g,'')+'" alt="'+alt+'" data-imgprev>');
  s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/gi,
    (m,label,url)=>'<a href="'+url+'" target="_blank" rel="noopener noreferrer">'+label+'</a>');
  s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/(^|\n)#{2,4}\s?([^\n]+)/g,'$1<h4>$2</h4>');
  s=s.replace(/(^|\n)((?:[-•]\s[^\n]+\n?)+)/g,(m,p,b)=>p+'<ul>'+b.trim().split('\n').map(l=>'<li>'+l.replace(/^[-•]\s*/,'')+'</li>').join('')+'</ul>');
  s=s.replace(/<\/pre><br>/g,'</pre>').replace(/\n{2,}/g,'</p><p>').replace(/\n/g,'<br>');
  return '<p>'+s+'</p>';
}
function plain(text){return String(text||'').replace(/```[\s\S]*?```/g,'[код]')
  .replace(/!\[([^\]]*)\]\([^)]*\)/g,'[изображение]').replace(/\[([^\]]+)\]\([^)]*\)/g,'$1').replace(/[*`#]/g,'');}

/* ============================================================
   ВНИМАНИЕ ПОЛЬЗОВАТЕЛЯ: мигание заголовка + системное уведомление
   Нужно, когда ход длинный и окно свёрнуто: агент закончил, просит
   подтверждение или задаёт вопрос. Разрешение спрашиваем лениво, по
   первому клику; в Electron оно обычно выдаётся без диалога.
   ============================================================ */
let _titleTimer=null,_titleBase=null,_notifyAsked=false;
function notifyPermission(){
  try{ return (typeof Notification!=='undefined')?Notification.permission:'denied' }
  catch(e){ return 'denied' }
}
function ensureNotifyPermission(){
  if(_notifyAsked||notifyPermission()!=='default')return;
  _notifyAsked=true;
  try{ const p=Notification.requestPermission(); if(p&&p.catch)p.catch(()=>{}) }catch(e){}
}
function flashTitle(msg){
  try{
    if(_titleBase===null)_titleBase=document.title||'Агент';
    if(_titleTimer)return;
    let on=false;
    document.title='● '+msg;
    _titleTimer=setInterval(()=>{ on=!on; document.title=on?'● '+msg:_titleBase },1200);
  }catch(e){}
}
function stopFlashTitle(){
  if(_titleTimer){ clearInterval(_titleTimer); _titleTimer=null }
  if(_titleBase!==null){ try{document.title=_titleBase}catch(e){} _titleBase=null }
}
function notifyAttention(title,body){
  try{
    if(document.hasFocus())return;
    flashTitle(title);
    if(notifyPermission()==='granted'){
      const n=new Notification(title,{body:String(body||'').slice(0,180),tag:'infinity-agent',silent:false});
      n.onclick=()=>{ try{window.focus()}catch(e){}; stopFlashTitle(); try{n.close()}catch(e){} };
    }else if(notifyPermission()==='default'){
      ensureNotifyPermission();
    }
  }catch(e){}
}
window.addEventListener('focus',stopFlashTitle);
document.addEventListener('visibilitychange',()=>{ if(!document.hidden)stopFlashTitle() });
document.addEventListener('pointerdown',ensureNotifyPermission,{once:true});

/* ============================================================
   STORAGE
   ============================================================ */
const DB_NAME='agent.workspace';
const DB_VER=1;
let _db=null;
function openDB(){
  return new Promise((res,rej)=>{
    if(_db)return res(_db);
    if(!('indexedDB' in window))return rej(new Error('no idb'));
    try{
      const r=indexedDB.open(DB_NAME,DB_VER);
      r.onupgradeneeded=e=>{
        const db=e.target.result;
        if(!db.objectStoreNames.contains('state')) db.createObjectStore('state');
      };
      r.onsuccess=e=>{_db=e.target.result;res(_db)};
      r.onerror=()=>rej(r.error);
    }catch(e){rej(e)}
  });
}
async function idbPut(store,key,val){
  const db=await openDB();
  return new Promise((res,rej)=>{
    const tx=db.transaction(store,'readwrite');
    tx.objectStore(store).put(val,key);
    tx.oncomplete=()=>res(); tx.onerror=()=>rej(tx.error);
  });
}
async function idbGet(store,key){
  const db=await openDB();
  return new Promise((res,rej)=>{
    const tx=db.transaction(store,'readonly');
    const r=tx.objectStore(store).get(key);
    r.onsuccess=()=>res(r.result); r.onerror=()=>rej(r.error);
  });
}
async function idbClear(store){
  const db=await openDB();
  return new Promise((res,rej)=>{
    const tx=db.transaction(store,'readwrite');
    tx.objectStore(store).clear();
    tx.oncomplete=()=>res(); tx.onerror=()=>rej(tx.error);
  });
}
const KEY='agent.workspace.v3';
/* Состояние дублируется на сервере (/api/workspace/state). Раньше оно
   лежало только в IndexedDB/localStorage, а оба привязаны к origin.
   Интерфейс агента открывается с двух разных адресов — напрямую
   http://127.0.0.1:9120/ и через прокси http://127.0.0.1:8281/agentai/,
   — поэтому включённые плагины и заметки выглядели пропавшими при смене
   адреса или перезапуске сервера. Теперь источник истины на диске,
   браузерное хранилище остаётся быстрым кэшем и резервной копией. */
const STATE_URL='/api/workspace/state';
const Store={
  /* Что лежит в браузере (idb или localStorage — как настроено). */
  async localSave(obj){
    const eng=state?.settings?.data?.engine||'idb';
    try{
      if(eng==='idb'&&!IS_FILE_PROTOCOL){ await idbPut('state','main',obj); }
      else { localStorage.setItem(KEY,JSON.stringify(obj)); }
    }catch(e){
      try{ localStorage.setItem(KEY,JSON.stringify(obj)); }catch(err){ toast('Хранилище переполнено','err'); }
    }
  },
  async localLoad(){
    const eng=state?.settings?.data?.engine||'idb';
    try{
      if(eng==='idb'&&!IS_FILE_PROTOCOL){
        const r=await idbGet('state','main');
        if(r)return r;
      }
      const ls=localStorage.getItem(KEY); if(ls)return JSON.parse(ls);
    }catch(e){}
    return null;
  },
  /* Сервер — общий для всех origin. */
  async remoteSave(obj){
    if(IS_FILE_PROTOCOL) return false;
    try{
      const r=await fetch(STATE_URL,{method:'POST',headers:{'Content-Type':'application/json'},
                                    body:JSON.stringify(obj)});
      return r.ok;
    }catch(e){ return false; }
  },
  async remoteLoad(){
    if(IS_FILE_PROTOCOL) return null;
    try{
      const r=await fetch(STATE_URL,{cache:'no-store'});
      if(!r.ok) return null;
      const d=await r.json();
      return (d&&typeof d==='object'&&!Array.isArray(d)&&Object.keys(d).length)?d:null;
    }catch(e){ return null; }
  }
};
const Storage={
  async save(obj){
    await Store.localSave(obj);
    Store.remoteSave(obj);   /* не ждём: сохранение не должно тормозить UI */
  },
  async load(){
    const remote=await Store.remoteLoad();
    if(remote) return remote;
    return await Store.localLoad();
  },
  async size(){
    const eng=state?.settings?.data?.engine||'idb';
    try{
      if(eng==='idb'&&!IS_FILE_PROTOCOL){ const r=await idbGet('state','main'); return new Blob([JSON.stringify(r||'')]).size; }
      const raw=localStorage.getItem(KEY)||''; return new Blob([raw]).size;
    }catch(e){return 0}
  }
};

/* ============================================================
   STATE
   ============================================================ */
const MODELS=[]; // Заполняется из backend через loadGatewayModels()
const TOOLS=[]; // Заполняется из backend через loadRealTools()
/* Навыки агента — из /v1/skills. Раньше они загружались, но нигде не
   показывались, поэтому во вкладке «Инструменты» их просто не было видно. */
const SKILLS=[];
const BUILTIN_TEMPLATES=[
  {id:'tpl-refactor',n:'Рефакторинг',d:'Проанализируй проект и предложи правки',sys:'Ты инженер-ревьюер.',msg:'Проанализируй папку проекта и предложи правки.'},
  {id:'tpl-plan',n:'План действий',d:'Пошаговый план задачи',sys:'Ты стратег-планировщик.',msg:'Составь план: '},
  {id:'tpl-debug',n:'Отладка',d:'Найди и исправь ошибки',sys:'Ты debug-инженер.',msg:'Помоги найти ошибку:'},
  {id:'tpl-docs',n:'Документация',d:'Опиши проект',sys:'Ты технический писатель.',msg:'Напиши документацию для:'},
  {id:'tpl-report',n:'Отчёт',d:'Подготовь отчёт',sys:'Ты аналитик.',msg:'Подготовь отчёт по данным:'}
];
const REACTIONS=[
  {k:'ok',icon:'fa-regular fa-thumbs-up',label:'Полезно'},
  {k:'bad',icon:'fa-regular fa-thumbs-down',label:'Неточно'},
  {k:'fire',icon:'fa-solid fa-fire',label:'Отлично'},
  {k:'idea',icon:'fa-regular fa-lightbulb',label:'Идея'},
  {k:'save',icon:'fa-regular fa-bookmark',label:'В закладки'},
  {k:'redo',icon:'fa-solid fa-rotate-right',label:'Повторить'}
];

const DEF={
  general:{lang:'ru',theme:'auto',animations:true,sounds:true,volume:.6,hints:true,autostartChat:false,autoDraft:true,autoSnipSuggest:true,
          /* Сколько последних сессий показывать в списке. 0 — все.
             Применяется к группе «Сессии»; избранное, закладки, проекты
             и архив не режутся — иначе закреплённое исчезало бы само. */
          recentSessions:0},
  appearance:{accent:'#4f46e5',font:'system',fontSize:'md',density:'cozy',radius:'md',msgWidth:'md',backdrop:'grid'},
  /* Плагины. Старый формат (plugins:[...]) мигрируется в plugins.defs в load().
     mode: 'auto' — новый .js подключается сразу, 'manual' — система спрашивает.
     defs[]: {id,name,desc,author,version,code,enabled,store,
              source:'file'|'local', file, hash, mtime, error} */
  plugins:{mode:'auto',dir:'',defs:[],pending:[],watching:false,
   /* Что показывать в списке: 'all' | 'js' | 'py' — переключатель во вкладке
      «Плагины». Раньше оба вида шли одним списком, и найти нужный плагин
      было невозможно, как только .py их становилось много. */
   view:'all', query:''},
  // recLang — короткий код ISO-639-1: он уходит и в SpeechRecognition,
  // и в /api/voice/stt, где faster-whisper региональные коды («ru-RU») не
  // принимает. Сервер тоже нормализует, но дефолт лучше сразу корректный.
  voice:{inputDevice:'default',recLang:'ru',continuous:true,autoSend:true,silence:0.8,silenceSend:2,sensitivity:.6,pushToTalk:true,
         /* sttModel='' = авто: сервер выбирает модель по профилю и устройству
            (на CPU base, на CUDA turbo). Раньше здесь стоял 'small', из-за чего
            выбор в интерфейсе не доходил до движка и фраза читалась вдвое
            дольше нужного. */
         sttEngine:'whisper',sttModel:'',
         wakeWord:true,wakePhrase:'финик',wakeIdle:10,stopWord:true,
         outputEnabled:true,voiceURI:'',rate:1,pitch:1,volume:1,autoSpeak:true,
         /* Озвучка на лету: голос идёт по ходу генерации, по предложениям,
            а не целиком после конца ответа (17-live-tts.js). Рассуждения
            не озвучиваются. */
         liveSpeak:true,
         /* Пользовательские движки по API (Настройки → Голос).
            Пока sttApi.enabled/ttsApi.enabled выключены, работают локальные
            Whisper и Silero — выбор пользователя ничего не ломает.
            mode: 'openai' — совместимый /v1/... (Whisper, Silero, OpenAI, TTS-API),
            'custom' — произвольный адрес и формат ответа. */
         sttApi:{enabled:false,mode:'openai',url:'',key:'',model:'whisper-1',lang:'ru',timeout:60},
         ttsApi:{enabled:false,mode:'openai',url:'',key:'',model:'tts-1',
                 voice:'alloy',format:'wav',speed:1,timeout:60}},
  agent:{model:'agent-pro',mode:'agent',reasoning:'medium',temperature:.7,maxTokens:4096,streaming:true,systemPrompt:'',memory:true,busyMode:'queue',
         directUrl:'',directKey:'',
         /* Что показывать в чате. Оба по умолчанию выключены:
            showToolCalls — карточки вызовов инструментов (иначе их видно
            только в боковой панели «Инструменты»);
            showReasoning — блоки «Рассуждение» рядом с ответом агента
            (сами рассуждения при этом продолжают копиться в панели справа). */
         showToolCalls:false,showReasoning:false},
  tools:{fs:true,terminal:true,web:true,browser:false,code:true,image:false,tasks:true,confirm:'dangerous',sandbox:true,timeout:60,allowList:'',
   /* Пользовательские инструменты и навыки, заведённые вручную во вкладке
      «Инструменты». Хранятся в настройках, поэтому переживают перезагрузку,
      и агент о них не знает — это справочник, а не регистрация в шлюзе. */
   custom:[], customSkills:[], showFallback:true,
         shellCmd:true,shellPrefix:'!',shellTimeout:60,
         /* Просмотр файлов: кнопка «Сравнить с Git» в панели вызовов и
            кнопка Diff у записанных агентом файлов. */
         diffEditor:true,
         /* Автоматически открывать терминал при старте приложения */
         liveTerminal:false},
  projects:{defaultProjectId:null,autoAttach:true,index:true,depth:2,ignore:'node_modules, .git, dist'},
  connection:{enabled:false,url:'ws://localhost:8080/ws',protocol:'ws',key:'',reconnect:true,interval:3,timeout:30},
  data:{engine:'idb',history:true,retention:'90',autosave:true},
  // НОВОЕ
  pricing:{
    currency:'USD',
    budget:1.0,
    rates:{'agent-pro':{in:5,out:15},'agent-fast':{in:2,out:6},'agent-code':{in:3,out:9},'agent-lite':{in:0.5,out:1.5}}
  },
  pomodoro:{focus:25,break:5,todaySessions:0,todayDate:'',totalSessions:0,totalMinutes:0},
  /* Крон-задачи. notify — колокольчик в шапке, autoRefresh/interval —
     фоновое обновление списка и истории, пока вкладка «Крон» открыта. */
  cron:{notify:true,notifySound:false,autoRefresh:true,interval:30},
  commands:[],
  /* Команды-перехватчики (Настройки → Команды). Ловят фразу ДО отправки
     агенту и выполняют скрипт сами: ни модель, ни агент, ни «Диалог» при
     этом не вызываются. Совпадение ищется по phrase и variants, fuzzy —
     нечёткое сравнение по биграммам. lang: 'js' | 'python'. */
  intercepts:[],
  snippets:[],
  promptHistory:[],
  profiles:[{id:'default',name:'По умолчанию',settings:null}],
  activeProfile:'default'
};

let state={sessions:[],projects:[],folders:[],activeSessionId:null,activeProjectId:null,
 customTemplates:[],pin:null,settings:structuredClone(DEF),
 // Сохраняем состояние панелей
 sideOpen:true,detailsOpen:true,toolsPanelOpen:false,thinkPinOpen:false,compactMode:false,fullscreenMode:false,termDrawerOpen:false};
const ui={chatOpen:false,setOpen:false,generating:false,abort:false,resolving:false,
 msgQueue:[],
 listening:false,query:'',filter:'all',bulkMode:false,bulkSel:new Set(),
 expanded:{projects:true,folders:true,sessions:true,pinned:true,archived:true,bookmarked:true},
 atts:[],handles:{},notes:{},resolvers:{},rec:null,spaceHeld:false,panel:'general',
 recentCmds:[],tourOn:false,tourStep:0,
 replyTo:null,editMsg:null,bookmarksOpen:false,focusMode:false,
 sessSearch:{query:'',matches:[],idx:-1},
 pomo:{running:false,mode:'focus',seconds:25*60,intervalId:null},
 toolsOpen:{},toolSessionId:null,diffPath:'',diffPathSession:'',
 /* Раскрытие веток субагентов внутри карточки delegate_task. Отдельное
    состояние от toolsOpen: у ветки своя идентичность (subagent_id), а не id
    сообщения, и она переживает перечитывание сессии из хранилища. */
 subOpen:{},
  /* Какие сообщения сейчас нарисованы в ленте и для какой сессии/поиска.
     Нужно renderStream(), чтобы дописывать новые сообщения, а не пересоздавать весь чат — иначе при вызовах инструментов текст мигал. */
  renderedIds:null,renderedSig:null,
  /* Флаг «мы уже внутри renderStream()»: patch() не должен возвращаться
     в неё повторно, иначе возможна бесконечная рекурсия. */
  _inRenderStream:false,
  /* «Липкий низ» ленты сообщений. Пока пользователь не прокрутил вверх,
     новые сообщения подталкивают чат вниз. Как только он ушёл читать —
     автопрокрутка выключается, иначе во время работы агента невозможно
     прокрутить чат вверх (каждый чанк возвращал его вниз). */
  stickBottom:true,
 fsbDrag:null,
  rxPicker:null};
const S=()=>state.settings;

const history={stack:[],idx:-1,MAX:40};
function pushHistory(action,inv){
  history.stack=history.stack.slice(0,history.idx+1);
  history.stack.push({action,inv,ts:Date.now()});
  if(history.stack.length>history.MAX) history.stack.shift();
  history.idx=history.stack.length-1;
  updUndoRedo();
}
function updUndoRedo(){
  const ub=$('#undoBtn'), rb=$('#redoBtn');
  if(ub) ub.disabled=history.idx<0;
  if(rb) rb.disabled=history.idx>=history.stack.length-1;
}
async function undo(){
  if(history.idx<0)return;
  const it=history.stack[history.idx]; history.idx--;
  await it.inv(); updUndoRedo(); save(); renderAll(); toast('Отменено','info');
}
async function redo(){
  if(history.idx>=history.stack.length-1)return;
  history.idx++;
  const it=history.stack[history.idx];
  await it.action(); updUndoRedo(); save(); renderAll(); toast('Повторено','info');
}

async function save(){
 /* Раньше здесь стоял выход по умолчанию:
      if(!S().data.history&&!S().data.autosave)return;
    Флаги history/autosave относятся к ИСТОРИИ ЧАТА, а не к настройкам.
    Когда оба выключали, save() выходил молча — и переключатель плагина
    (p.enabled=...), заметки и настройки НЕ сохранялись вообще: после
    перезагрузки всё выглядело «пропавшим». Теперь эти флаги не влияют
    на сохранение состояния. */
 const obj={sessions:state.sessions,projects:state.projects,folders:state.folders,
 activeSessionId:state.activeSessionId,activeProjectId:state.activeProjectId,
 customTemplates:state.customTemplates,pin:state.pin,settings:state.settings,
 // Сохраняем состояние панелей
 sideOpen:state.sideOpen,detailsOpen:state.detailsOpen,toolsPanelOpen:state.toolsPanelOpen,thinkPinOpen:state.thinkPinOpen,
 compactMode:state.compactMode,fullscreenMode:state.fullscreenMode,
 termDrawerOpen:!!state.termDrawerOpen,
 gatewayCwd:state.gatewayCwd||null,
 // Очередь сообщений переживает перезагрузку страницы
 msgQueue:ui.msgQueue.map(x=>({text:x.text,sessionId:x.sessionId}))};
 await Storage.save(obj);
 refreshStoreInfo();
}
/* Приводит settings.plugins к новой форме {mode,dir,defs,pending,watching}.
   До файловой системы плагины лежали плоским массивом settings.plugins:[{...}].
   Вызывается при загрузке, смене профиля и импорте — везде, где
   settings могли прийти из старого хранилища. Старые плагины переносим
   в defs с source:'local': код остаётся в состоянии, поведение прежнее. */
function normalizePlugins(){
  const s=S();
  if(Array.isArray(s.plugins)){
    s.plugins={mode:'auto',dir:'',pending:[],watching:false,
      defs:s.plugins.map(p=>Object.assign({source:'local',enabled:true,store:{}},p))};
  }
  const P=s.plugins;
  if(!P||typeof P!=='object'){ s.plugins=structuredClone(DEF.plugins); return }
  P.mode=(P.mode==='manual')?'manual':'auto';
  P.dir=P.dir||''; P.pending=P.pending||[]; P.watching=!!P.watching;
  if(!Array.isArray(P.defs)) P.defs=[];
  // Восстанавливаем id у записей без него (например, из импорта)
  P.defs=P.defs.filter(p=>p&&typeof p==='object').map(p=>{
    if(!p.id) p.id=uid('pl');
    if(!p.source) p.source=p.file?'file':'local';
    if(typeof p.enabled!=='boolean') p.enabled=true;
    p.store=p.store||{};
    return p;
  });
}

/* Приводит settings.intercepts к предсказуемой форме. Вызывается при загрузке,
   смене профиля и импорте — везде, где настройки могли прийти из старого
   хранилища или из чужого профиля. Заодно чинит записи, у которых часть
   полей потерялась: сопоставление не должно падать на неполных данных. */
function normalizeIntercepts(){
  const s=S();
  if(!Array.isArray(s.intercepts)) s.intercepts=[];
  s.intercepts=s.intercepts.filter(x=>x&&typeof x==='object').map(x=>{
    if(!x.id) x.id=uid('int');
    if(typeof x.phrase!=='string') x.phrase='';
    if(!Array.isArray(x.variants)) x.variants=[];
    x.variants=x.variants.filter(v=>typeof v==='string'&&v.trim());
    x.fuzzy=!!x.fuzzy;
    if(x.lang!=='js') x.lang='python';
    if(typeof x.code!=='string') x.code='';
    if(typeof x.path!=='string') x.path='';
    if(typeof x.args!=='string') x.args='';
    if(!x.desc) x.desc='';
    if(typeof x.enabled!=='boolean') x.enabled=true;
    if(!(Number(x.timeout)>0)) x.timeout=60;
    x.timeout=Math.max(1,Math.min(300,Number(x.timeout)||60));
    return x;
  });
}

async function load(){
  const r=await Storage.load(); if(!r)return;
  state={...state,...r,settings:merge(structuredClone(DEF),r.settings||{})};
  /* Разовая миграция: тумблера «Озвучивать ответы» раньше не было — включаем всем один раз */
  if(!state.settings.voice._autoSpeakMigrated){
    state.settings.voice._autoSpeakMigrated=true;
    state.settings.voice.autoSpeak=true;
  }
  /* Разовая миграция: «silence» раньше означала другое (2 с) и тормозила бы VAD — ставим 0.8 */
  if(!state.settings.voice._silenceMigrated){
    state.settings.voice._silenceMigrated=true;
    const sl=Number(state.settings.voice.silence);
    if(!(sl>0)||sl>=1.7) state.settings.voice.silence=0.8;
  }
  normalizePlugins();
  normalizeIntercepts();
  // Восстанавливаем очередь сообщений (только валидные элементы)
  if(Array.isArray(r.msgQueue)){
    ui.msgQueue=r.msgQueue.filter(x=>x&&typeof x.text==='string'&&x.text.trim());
  }
}
/* Глубокое слияние настроек: недостающие ветки берутся из значений по умолчанию,
   поэтому merge(DEF, сохранённое) даёт полный объект даже если настройки менялись
   много раз. Массивы заменяются целиком, а не сливаются по элементам. */
function merge(a,b){ for(const k in b){ if(b[k]&&typeof b[k]==='object'&&!Array.isArray(b[k])) a[k]=merge(a[k]||{},b[k]); else a[k]=b[k]; } return a; }

let draftTimer=null;
function saveDraft(){
  if(!S().general.autoDraft)return;
  const inp=$('#input');
  if(!inp)return;
  const txt=inp.value.trim();
  const s=active(); if(!s||!txt)return;
  try{localStorage.setItem('draft.'+s.id,txt)}catch(e){}
}
function checkDraft(){
  const s=active(); if(!s)return;
  const d=localStorage.getItem('draft.'+s.id);
  const dp=$('#draftPill');
  if(dp){ if(d) dp.classList.add('on'); else dp.classList.remove('on'); }
}
$('#draftRestore').onclick=()=>{ const s=active(); if(!s)return;
  const inp=$('#input');
  if(inp){ inp.value=localStorage.getItem('draft.'+s.id)||''; autoGrow(inp); updTok(); }
  const dp=$('#draftPill'); if(dp) dp.classList.remove('on');
};
$('#draftDismiss').onclick=()=>{ const s=active(); if(s)localStorage.removeItem('draft.'+s.id);
  const dp=$('#draftPill'); if(dp) dp.classList.remove('on');
};

const active=()=>state.sessions.find(s=>s.id===state.activeSessionId)||null;
const lastUserText=s=>{const m=[...(s?.messages||[])].reverse().find(x=>x.role==='user'&&x.kind==='text');return m?m.content:''};
function newSession(opt={}){
  const pid=opt.projectId!==undefined?opt.projectId:(S().projects.autoAttach?state.activeProjectId:null);
  const proj=pid?state.projects.find(p=>p.id===pid):null;
  const s={id:uid('s'),title:opt.title||(proj?'Сессия · '+proj.name:'Новая сессия'),projectId:pid||null,
    folderId:opt.folderId||null,pinned:false,archived:false,locked:false,tags:[],bookmarks:[],reactions:{},
    createdAt:Date.now(),updatedAt:Date.now(),
    model:S().agent.model,mode:S().agent.mode,messages:[],
    costIn:0,costOut:0};
  if(opt.sys) msg(s,{role:'system',kind:'system',content:opt.sys});
  state.sessions.unshift(s); state.activeSessionId=s.id;
  if(proj) msg(s,{role:'system',kind:'system',content:'Рабочая папка: '+proj.path});
  pushHistory(async()=>{},async()=>{killSession(s.id,true)});
  save(); renderAll();
  if(opt.msg){ setTimeout(()=>{ const inp=$('#input'); if(inp){inp.value=opt.msg; autoGrow(inp); inp.focus();} },260); }
  try{ PluginBus.emit('session', s) }catch(e){}
  return s;
}
function msg(s,m){ const o={id:uid('m'),ts:Date.now(),status:'done',...m}; s.messages.push(o); s.updatedAt=Date.now(); return o; }
/* Вызов инструмента, по которому событие завершения не пришло (перебитый ход,
   обрыв соединения, ошибка шлюза), оставался «выполняется» навсегда — и в ленте,
   и в панели. Гасим такие карточки честно: статус ошибки с пояснением. */
function closeStaleTools(s){
  if(!s)return 0;
  let n=0;
  for(const m of s.messages||[]){
    if(m.kind==='tool'&&m.status==='running'){
      m.status='error'; m.meta=m.meta||{};
      if(!m.meta.output)m.meta.output='Ход завершился — ответа по этому вызову не пришло';
      n++;
    }
  }
  return n;
}
/* Подтверждение или вопрос, по которому ход закончился без ответа (обрыв, стоп,
   таймаут), не должен оставаться «ожидает»: ответить на него уже некому. */
function closeStaleControls(s){
  if(!s)return 0;
  let n=0;
  for(const m of s.messages||[]){
    if((m.kind==='approval'||m.kind==='clarify')&&m.status==='running'){
      m.status='done'; m.meta=m.meta||{};
      if(m.kind==='approval'&&!m.meta.decision)m.meta.decision='ход завершён';
      if(m.kind==='clarify'&&!m.meta.answer)m.meta.answer='ход завершён';
      n++;
    }
  }
  return n;
}
function killSession(id,silent){
  const s=state.sessions.find(x=>x.id===id);
  const old=state.sessions.slice();
  state.sessions=state.sessions.filter(s=>s.id!==id);
  if(state.activeSessionId===id) state.activeSessionId=state.sessions[0]?.id||null;
  if(!silent) pushHistory(async()=>{state.sessions=old;if(!state.sessions.find(x=>x.id===id))state.sessions.push(s);
    state.activeSessionId=s.id;save();renderAll()},async()=>{killSession(id,true)});
  save(); renderAll();
}
const projectOf=s=>s&&s.projectId?state.projects.find(p=>p.id===s.projectId):null;
const folderOf=s=>s&&s.folderId?state.folders.find(f=>f.id===s.folderId):null;

function toast(text,type='info'){ const t=document.createElement('div'); t.className='toast '+type;
  const ic=type==='ok'?'fa-circle-check':type==='err'?'fa-circle-exclamation':'fa-circle-info';
  t.innerHTML='<i class="fa-solid '+ic+'"></i><span>'+esc(text)+'</span>';
  const toasts=$('#toasts');
  if(toasts) toasts.appendChild(t);
  setTimeout(()=>{t.classList.add('out');setTimeout(()=>t.remove(),260)},2600); }
function beep(freq=680,dur=.09){ if(!S().general.sounds)return; try{ const c=new (window.AudioContext||window.webkitAudioContext)();
  const o=c.createOscillator(),g=c.createGain(); o.frequency.value=freq; o.type='sine'; g.gain.value=.06*(S().general.volume||.5);
  o.connect(g); g.connect(c.destination); o.start(); g.gain.exponentialRampToValueAtTime(.0001,c.currentTime+dur);
  setTimeout(()=>{o.stop();c.close()},dur*1000+40);}catch(e){} }

let formResolve=null;
function askForm({title,icon='fa-pen',fields=[],ok='Подтвердить'}){
  return new Promise(res=>{ formResolve=res;
    const ft=$('#formTitle'), fi=$('#formIcon'), fok=$('#formOk'), fb=$('#formBody');
    if(ft) ft.textContent=title;
    if(fi) fi.className='fa-solid '+icon;
    if(fok) fok.innerHTML='<i class="fa-solid fa-check"></i> '+esc(ok);
    if(fb){
      fb.innerHTML=fields.map(f=>'<div class="fld"><label>'+esc(f.label)+'</label>'+
        (f.type==='path'
          ? '<div class="path-row"><input class="field" data-f="'+f.name+'" value="'+esc(f.value||'')+'" placeholder="'+esc(f.ph||'')+'">'+
            '<button class="btn sm" data-pick="'+f.name+'"><i class="fa-solid fa-folder-open"></i></button></div>'
          : f.type==='area'
          ? '<textarea class="field" data-f="'+f.name+'" placeholder="'+esc(f.ph||'')+'" rows="4">'+esc(f.value||'')+'</textarea>'
          : '<input class="field" type="text" data-f="'+f.name+'" value="'+esc(f.value||'')+'" placeholder="'+esc(f.ph||'')+'">')+
        '</div>').join('');
    }
    const fo=$('#formOverlay');
    if(fo) fo.classList.add('open');
    setTimeout(()=>{
      const inp=fb?fb.querySelector('input,textarea'):null;
      if(inp){inp.focus();inp.select&&inp.select()}
    },80);
  });
}
function closeForm(val){ const fo=$('#formOverlay'); if(fo) fo.classList.remove('open');
  if(formResolve){formResolve(val);formResolve=null} }
function formValues(){ const o={};
  $$('#formBody [data-f]').forEach(e=>o[e.dataset.f]=e.value.trim());
  return o; }

$('#formOk').onclick=()=>closeForm(formValues());
$('#formCancel').onclick=()=>closeForm(null);
$('#formX').onclick=()=>closeForm(null);
/* Окно подтверждения нового плагина. Отсутствие крестика — осознанно:
   файл уже на диске, вопрос нужно закрыть явно, а Escape трактуем как «не сейчас». */
$('#plugAskYes').onclick=()=>PluginFS.answerAsk(true);
$('#plugAskNo').onclick=()=>PluginFS.answerAsk(false);
document.addEventListener('keydown',e=>{
  if(e.key!=='Escape')return;
  const ov=$('#plugAsk');
  if(ov&&ov.classList.contains('open')) PluginFS.answerAsk(false);
});
$('#formBody').addEventListener('click',async e=>{
  const b=e.target.closest('[data-pick]'); if(!b)return;
  if(IS_FILE_PROTOCOL){ toast('Выбор папки не работает через file://','err'); return }
  const h=await pickDir(); if(h){
    // Подставляем абсолютный путь (agент на бэкенде работает именно с ним)
    const inp=$('#formBody [data-f="'+b.dataset.pick+'"]');
    if(inp)inp.value=(lastPickedDir&&lastPickedDir.name===h.name)?lastPickedDir.abs:h.name;
  }
});
$('#formBody').addEventListener('keydown',e=>{ if(e.key==='Enter'&&e.target.tagName!=='TEXTAREA'){e.preventDefault();closeForm(formValues())} });

let pinResolve=null,pinTarget='',pinInput='';
function askPin(mode='verify',title='Введите PIN'){
  return new Promise(res=>{
    pinResolve=res; pinTarget=mode; pinInput='';
    const pt=$('#pinTitle'), ph=$('#pinHint'), pd=$('#pinDots'), po=$('#pinOverlay');
    if(pt) pt.textContent=title;
    if(ph) ph.textContent=mode==='set'?'Задайте новый PIN (4 цифры)':'Введите PIN';
    if(pd) pd.innerHTML='<span class="pin-dot"></span><span class="pin-dot"></span><span class="pin-dot"></span><span class="pin-dot"></span>';
    if(po) po.classList.add('open');
  });
}
$('#pinX').onclick=()=>{ const po=$('#pinOverlay'); if(po) po.classList.remove('open'); if(pinResolve){pinResolve(null);pinResolve=null} };
$('#pinGrid').onclick=async e=>{
  const b=e.target.closest('[data-d]'); if(!b)return;
  const d=b.dataset.d;
  if(d==='clr'){ pinInput=pinInput.slice(0,-1); }
  else if(d==='ok'){
    if(pinTarget==='set'){
      if(pinInput.length===4){ state.pin=pinInput; await save();
        const po=$('#pinOverlay'); if(po) po.classList.remove('open');
        if(pinResolve){pinResolve(true);pinResolve=null}
        toast('PIN установлен','ok'); }
      else { $$('.pin-dot').forEach(d=>d.classList.add('err')); setTimeout(()=>$$('.pin-dot').forEach(d=>d.classList.remove('err')),400) }
    } else if(pinTarget==='verify'){
      if(pinInput===state.pin){ const po=$('#pinOverlay'); if(po) po.classList.remove('open'); if(pinResolve){pinResolve(true);pinResolve=null} }
      else { $$('.pin-dot').forEach(d=>d.classList.add('err')); setTimeout(()=>{$$('.pin-dot').forEach(d=>d.classList.remove('err'));pinInput='';updatePinDots()},400) }
    }
    return;
  }
  else if(pinInput.length<4) pinInput+=d;
  updatePinDots();
};
function updatePinDots(){ $$('#pinDots .pin-dot').forEach((d,i)=>d.classList.toggle('on',i<pinInput.length)) }

function ctxMenu(x,y,items){
  const c=$('#ctx'); if(!c)return;
  /* Подменю («Переместить в папку») открывается прямо во время обработки
     клика. Раньше closeCtx() снимал класс, новый ctxMenu ставил его снова,
     а затем тот же клик доходил до document-обработчика и закрывал меню
     уже открытое подменю. Флаг ctxKeep помечает «мы сами переоткрываем». */
  ctxKeep=true;
  c.innerHTML=items.filter(Boolean).map((it,i)=>it==='-'?'<hr>':it.label
    ? '<div class="lbl">'+esc(it.label)+'</div>'
    : '<button data-i="'+i+'" class="'+(it.danger?'danger':'')+'"><i class="fa-solid '+it.icon+'"></i>'+esc(it.text)+
      (it.k?'<span class="k">'+esc(it.k)+'</span>':'')+'</button>').join('');
  c.classList.add('open');
  const r=c.getBoundingClientRect();
  c.style.left=Math.min(x,innerWidth-r.width-10)+'px';
  c.style.top=Math.min(y,innerHeight-r.height-10)+'px';
  c.onclick=e=>{ const b=e.target.closest('button'); if(!b)return; const it=items[+b.dataset.i];
    closeCtx();
    if(it&&it.onClick)setTimeout(()=>it.onClick(),0);  /* после всплытия клика */
  };
  setTimeout(()=>{ctxKeep=false},0);
}
let ctxKeep=false;
function closeCtx(){ const c=$('#ctx'); if(c) c.classList.remove('open') }
document.addEventListener('click',e=>{ if(ctxKeep)return;
  if(!e.target.closest('#ctx')&&!e.target.closest('[data-ctx]'))closeCtx(); });
addEventListener('resize',closeCtx);

function openChat(){ ui.chatOpen=true; passiveHold(true); const co=$('#chatOverlay'); if(co) co.classList.add('open');
  if(!active()) newSession(); renderAll(); setTimeout(()=>{const inp=$('#input');if(inp)inp.focus()},260); checkDraft(); }
function closeChat(){ saveDraft(); ui.chatOpen=false; const co=$('#chatOverlay'); if(co) co.classList.remove('open'); const bc=$('#btnChat'); if(bc) bc.classList.remove('on'); passiveHold(false); }
function openSet(panel){ ui.setOpen=true; passiveHold(true); const so=$('#setOverlay'); if(so) so.classList.add('open'); if(panel)switchPanel(panel);
  renderProjectsSettings(); renderToolsSettings(); renderTemplatesSettings(); renderCommandsSettings(); renderPricingSettings(); renderProfiles(); renderPluginsSettings(); renderInterceptsSettings(); renderVoiceEngines(); refreshStoreInfo(); bindCronSettings();
  if(ui.panel==='cron')loadCronSettings();
  /* Файлы инструкций рисуем всегда: блок мог остаться пустым, если вкладку
     «Агент» открыли не кликом по боковому меню (например, через Ctrl+,),
     и тогда обработчик на кнопке меню не срабатывал — пользователь полей
     не видел вовсе. */
  if(typeof renderAgentFiles==='function')renderAgentFiles();
  if(ui.panel==='agent'){
    if(typeof loadAgentFiles==='function')loadAgentFiles();
    if(typeof loadAgentPrompt==='function')loadAgentPrompt();
  }
  if(ui.panel==='plugins'&&typeof loadPyPlugins==='function'&&!PYP.loaded)loadPyPlugins(); }
function closeSet(){ ui.setOpen=false; const so=$('#setOverlay'); if(so) so.classList.remove('open'); passiveHold(false); }
$('#chatClose').onclick=closeChat; $('#setClose').onclick=closeSet; $('#setDone').onclick=closeSet;
$('#chatOverlay').addEventListener('click',e=>{ if(e.target.id==='chatOverlay')closeChat() });
$('#setOverlay').addEventListener('click',e=>{ if(e.target.id==='setOverlay')closeSet() });
$('#btnChat').onclick=()=>{openChat();$('#btnChat').classList.add('on')};
$('#hdSettings').onclick=()=>openSet();
/* Новая сессия: даём выбор — отдельная сессия или в конкретном проекте.
   Раньше кнопка молча наследовала активный проект (autoAttach), поэтому
   создать сессию «без проекта» было невозможно без удаления проекта. */
async function newSessionWithChoice(){
  const hasProjects=state.projects.length>0;
  const suggest=S().projects.autoAttach?state.activeProjectId:null;
  if(!hasProjects&&!suggest){ newSession({projectId:null}); toast('Сессия создана','ok'); return }
  const pid=await pickProjectForSession($('#hdNewSession'));
  if(pid===undefined)return;                       /* меню закрыли без выбора */
  newSession({projectId:pid||null});
  toast('Сессия создана','ok');
}
$('#hdNewSession').onclick=$('#qSession').onclick=newSessionWithChoice;
$('#hdNewProject').onclick=$('#qProject').onclick=()=>createProject();
$('#qFolder').onclick=()=>createFolder();
$('#hdTemplates').onclick=()=>openSet('templates');

function applyTheme(){
  const t=S().general.theme;
  let real=t;
  if(t==='auto') real=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
  document.body.dataset.theme=real;
  const ht=$('#hdTheme');
  if(ht) ht.innerHTML='<i class="fa-solid '+(real==='dark'?'fa-sun':'fa-moon')+'"></i>';
  /* Monaco и xterm наследуют цвета из CSS-переменных — перекрашиваем вместе с ними */
  if(typeof refreshVendorThemes==='function')refreshVendorThemes();
  if(typeof Term!=='undefined'&&Term.inst&&Term.inst.options){
    Term.inst.options.theme=termTheme();
  }
}
$('#hdTheme').onclick=()=>{
  const cur=S().general.theme;
  const next=cur==='light'?'dark':cur==='dark'?'auto':'light';
  setPath(S(),'general.theme',next);
  applyTheme(); save(); bindSettings(); flashSaved();
  toast('Тема: '+(next==='light'?'светлая':next==='dark'?'тёмная':'авто'),'ok');
};
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{ if(S().general.theme==='auto')applyTheme() });

function switchPanel(p){ ui.panel=p; $$('#setNav button').forEach(b=>b.classList.toggle('on',b.dataset.p===p));
  $$('.panel').forEach(s=>s.classList.toggle('on',s.dataset.p===p));
  if(p==='cron'&&typeof loadCronSettings==='function')loadCronSettings();
  /* Фоновое обновление заданий нужно только пока вкладка «Крон» открыта —
     в колокольчике состояние и так подтягивается по клику. */
  if(typeof cronAutoRefresh==='function')cronAutoRefresh(p==='cron'&&S().cron.autoRefresh!==false);
  if(typeof renderAcfProfiles==='function'&&p==='agent')renderAcfProfiles();
  if(p==='agent'&&typeof loadAgentConfig==='function'){ bindAgentConfig(); loadAgentConfig(); } }
$('#setNav').addEventListener('click',e=>{const b=e.target.closest('button[data-p]'); if(b)switchPanel(b.dataset.p)});

const stage=$('#stage');
const statusEl=$('#statusText'),statusRow=$('#statusRow'),sayEl=$('#stageSay');
/* Статус и реплика главного экрана занимают одно место и взаимно исключают друг друга */
function setStatus(t,mode){
  const st=statusEl||$('#statusText'); if(st) st.textContent=t;
  if(stage) stage.classList.toggle('listening',mode==='listen');
  const row=statusRow||$('#statusRow'); if(row) row.hidden=false;
  const say=sayEl||$('#stageSay');
  if(say){ say.classList.remove('show'); say.hidden=true; say.dataset.who=''; say.innerHTML='' }
}
/* Реплика на месте статуса: моя фраза или ответ агента */
function stSay(text,who,interim){
  const say=sayEl||$('#stageSay'); if(!say)return;
  const row=statusRow||$('#statusRow'); if(row) row.hidden=true;
  say.hidden=false; say.dataset.who=who||'user';
  const body=text?esc(text):'<span class="interim">Слушаю…</span>';
  say.innerHTML=(who==='agent'?'<span class="who">Финик</span>':'')+body+(interim&&text?'<span class="interim"> …</span>':'');
  say.classList.add('show'); say.scrollTop=say.scrollHeight;
}
