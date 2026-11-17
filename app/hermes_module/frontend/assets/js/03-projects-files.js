/* 03-projects-files.js — Проекты, папки и файловый браузер */
/* ============================================================
   PROJECTS / FOLDERS
   ============================================================ */
/* Best-effort абсолютный путь для FileSystemDirectoryHandle.
   Chromium по соображениям приватности не отдаёт полный путь из
   showDirectoryPicker — сначала пробуем автопоиск на бэкенде (resolveDirAbs),
   если не нашлось — диалог с предзаполненным значением (ручной резерв). */
function resolveDirHandlePath(handle){
  return handle&&handle.name?handle.name:'';
}
/* Гарантируем абсолютный путь: автопоиск → нативный диалог → ручной ввод.
   Отмена всех шагов возвращает null. */
async function ensureAbsPath(name){
  let abs=await resolveDirAbs(name);
  if(abs)return abs;
  const v=await askForm({title:'Папка на диске',icon:'fa-folder-tree',ok:'Сохранить',
    fields:[{name:'path',label:'Полный путь к папке (агент работает на сервере)',type:'path',value:name}]});
  if(v&&v.path)return v.path;
  return null;
}
/* Последний выбранный через showDirectoryPicker каталог: {name, abs} — нужен,
   т.к. FileSystemDirectoryHandle не отдаёт абсолютный путь напрямую. */
let lastPickedDir=null;

/* Абсолютный путь к выбранной папке без ручного ввода. Браузер отдаёт только
   имя, поэтому спрашиваем у бэкенда (/api/fs/resolve, kind=dir): он ищет папку
   в рабочей директории проекта и типичных местах (Desktop, Downloads, …).
   Если не нашёл — один нативный диалог выбора папки (/api/fs/pick-dir).
   null → пользователь отменил/нет связи с бэкендом. */
async function resolveDirAbs(name){
  if(lastPickedDir&&lastPickedDir.name===name&&lastPickedDir.abs&&lastPickedDir.abs!==name)
    return lastPickedDir.abs;
  if(IS_FILE_PROTOCOL)return null;
  try{
    let proj=null;
    if(typeof active==='function'&&typeof projectOf==='function'){
      const s=active(); proj=projectOf(s);
    }
    const root=proj&&proj.absPath?proj.absPath:null;
    const r=await fetch('/api/fs/resolve',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({names:[name],root,kind:'dir'})});
    if(r.ok){
      const data=await r.json().catch(()=>null);
      const p=data&&data.paths&&data.paths[name];
      if(p){ lastPickedDir={name,abs:p}; return p; }
    }
    // фолбэк: нативный диалог выбора папки (один на операцию)
    const pr=await fetch('/api/fs/pick-dir',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({title:'Укажите папку: '+name})});
    const pd=await pr.json().catch(()=>null);
    if(pd&&pd.path){ lastPickedDir={name,abs:pd.path}; return pd.path; }
  }catch(e){ /* офлайн — вернуть null */ }
  return null;
}

/* Полные пути для прикреплённых файлов. Браузер не отдаёт абсолютный путь из
   <input type=file> и drag&drop, поэтому спрашиваем у бэкенда (/api/fs/resolve):
   он ищет файл по имени в рабочей директории проекта и типичных папках
   (Desktop, Downloads, Documents, Pictures). Если не нашёл — один нативный
   диалог (/api/fs/pick). Путь нужен только агенту: в чат он не попадает —
   в историю уходит текст сообщения, а путь вкладывается лишь в backend-запрос. */
async function resolveFilePaths(atts){
  const need=atts.filter(a=>a&&!a.path);
  if(!need.length)return;
  if(IS_FILE_PROTOCOL){
    /* Без сервера путь недоступен — честно помечаем, псевдопуть не подставляем */
    need.forEach(a=>{a.unresolved=true});
    return;
  }
  if(ui.resolving){ // параллельный резолв уже идёт — ждём его завершения
    while(ui.resolving){ await new Promise(r=>setTimeout(r,100)); }
    if(!need.some(a=>!a.path))return; // текущий резолв уже обработал эти файлы
  }
  ui.resolving=true;
  try{
    let proj=null;
    if(typeof active==='function'&&typeof projectOf==='function'){
      const s=active(); proj=projectOf(s);
    }
    const root=proj&&proj.absPath?proj.absPath:null;
    const types=[...new Set(need.filter(a=>!a.isDir).map(a=>String(a.name).split('.').pop().toLowerCase()))];
    const r=await fetch('/api/fs/resolve',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({names:need.map(a=>a.name),types,root,
        kind:need.some(a=>a.isDir)?'any':'file'})});
    if(r.ok){
      const data=await r.json().catch(()=>null);
      const map=(data&&data.paths)||{};
      const missed=[];
      need.forEach(a=>{
        const p=map[a.name];
        if(p){a.path=p; toast('Файл найден: '+p,'ok');}
        else missed.push(a);
      });
      if(missed.length){
        const pr=await fetch('/api/fs/pick',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({title:'Выберите файлы для агента',types})});
        const pd=await pr.json().catch(()=>null);
        const paths=(pd&&pd.paths)||[];
        missed.forEach(a=>{
          const byName=paths.find(p=>p.toLowerCase().endsWith(a.name.toLowerCase()));
          const p=byName||(paths.length===missed.length?paths[missed.indexOf(a)]:null);
          if(p){a.path=p; toast('Файл выбран: '+p,'ok');}
        });
      }
    }
    /* Путь не нашли: НЕ подставляем псевдопуть. Раньше здесь писалось
       «[Пользователь прикрепил файл: name]» и это уходило агенту как
       настоящий путь — он пытался открыть несуществующий файл.
       Теперь оставляем path пустым и помечаем: UI покажет предупреждение,
       а в запрос к агенту уйдёт честное сообщение о недоступности. */
    need.forEach(a=>{ if(!a.path)a.unresolved=true });
  }catch(e){ /* офлайн-фолбэк */
    need.forEach(a=>{ a.unresolved=true });
  }finally{ ui.resolving=false; await fetchFileContents(atts); }
}

/* Содержимое текстовых файлов и изображений забираем с диска через backend
   (/api/fs/read-text) — тогда агент видит не только путь, но и данные,
   даже если файл лежит вне его рабочей директории. Вызывается после резолва. */
async function fetchFileContents(atts){
  const need=atts.filter(a=>a&&a.path&&!a.isDir&&!a.dataUrl&&!a.content&&
    /\.(txt|md|py|js|ts|jsx|tsx|json|html?|css|scss|xml|ya?ml|csv|ini|cfg|conf|log|sh|bat|ps1|sql|rs|go|java|c|cpp|h|hpp|cs|rb|php|swift|kt|toml|env|vue)$/i.test(a.name));
  if(!need.length||IS_FILE_PROTOCOL)return;
  for(const a of need){
    try{
      const r=await fetch('/api/fs/read-text?path='+encodeURIComponent(a.path));
      if(r.ok){
        const d=await r.json();
        if(d&&!d.binary&&typeof d.text==='string'){a.content=d.text;a.truncated=!!d.truncated}
      }
    }catch(e){ /* без содержимого — агент откроет файл по пути */ }
  }
}

/* Выбор папки. Раньше использовался только window.showDirectoryPicker —
   он требует, чтобы приложение было в защищённом контексте (localhost),
   иначе диалог молча не появляется, а ошибка глоталась catch'ем.
   Поэтому сначала пробуем нативный диалог на СЕРВЕРЕ (/api/fs/pick-dir):
   он работает всегда и возвращает абсолютный путь. Браузерный — запасной. */
async function pickDir(){
  if(IS_FILE_PROTOCOL){ toast('Выбор папки не работает через file://','err'); return null }
  let served=false;
  try{
    const r=await fetch('/api/fs/pick-dir',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({title:'Выберите папку проекта'})});
    if(r.ok){
      const d=await r.json().catch(()=>null);
      const abs=(d&&(d.path||d.abs||d.absPath))||'';
      if(abs){ lastPickedDir={name:abs.split(/[\\/]/).filter(Boolean).pop()||abs,abs:abs}; return {name:lastPickedDir.name,abs:abs} }
    }else served=true;   /* диалог есть, но не сработал (например отмена) */
  }catch(e){ served=true }
  /* Запасной путь — диалог браузера */
  if(window.showDirectoryPicker){
    try{
      const h=await window.showDirectoryPicker({mode:'readwrite'});
      lastPickedDir={name:h.name,abs:resolveDirHandlePath(h)};
      return h;
    }catch(e){ return null }   /* пользователь отменил */
  }
  if(served)toast('Серверный диалог недоступен и браузер его не поддерживает — укажите путь вручную','err');
  return null;
}
async function listDir(h){ const out=[]; try{
  for await (const [name,handle] of h.entries()){ out.push({name,kind:handle.kind}); if(out.length>200)break; }
}catch(e){} return out.sort((a,b)=>a.kind===b.kind?0:(a.kind==='directory'?-1:1)); }
async function createProject(manual){
  if(!manual&&!IS_FILE_PROTOCOL){
    const h=await pickDir();
    if(!h) return createProject(true);   /* диалог закрыт — ручной режим */
    /* Абсолютный путь обязателен: агент работает на бэкенде и должен знать,
       где лежит проект (по имени папки он его не найдёт). */
    const abs=h.abs||await resolveDirAbs(h.name)||h.name;
    const p=addProject(h.name,abs);
    if(ui.handles)ui.handles[p.id]=h;
    p.absPath=abs;
    if(h.abs)loadProjectFiles(p); else p.files=[];
    save(); renderAll(); toast('Проект «'+h.name+'» добавлен','ok'); return p;
  }
  const v=await askForm({title:'Новый проект',icon:'fa-diagram-project',ok:'Создать',fields:[
    {name:'name',label:'Название',ph:'Мой проект'},
    {name:'path',label:'Путь к папке',type:'path',ph:'C:\\Users\\me\\project'}]});
  if(!v)return null;
  const nm=String(v.name||'').trim();
  if(!nm){ toast('Укажите название проекта','err'); return null }
  const abs=String(v.path||'').trim();
  if(!abs)toast('Путь не указан — список файлов проекта будет недоступен, но агент получит имя папки','info');
  const p=addProject(nm,abs||nm);
  if(abs)loadProjectFiles(p);
  save(); renderAll(); toast('Проект создан','ok'); return p;
}
/* Загружает список файлов проекта через файловый API. Отдельная функция,
   чтобы работала и после перезагрузки страницы, и для проекта, созданного
   вручную (раньше файлы читались только из браузерного handle). */
function loadProjectFiles(p){
  const dir=p&&(p.absPath||p.path);
  if(!dir)return;
  fsbLoad(dir).then(()=>renderFileBrowser());
}
function addProject(name,path){
  const p={id:uid('p'),name,path,absPath:path,createdAt:Date.now(),files:[]};
  state.projects.push(p); state.activeProjectId=p.id;
  if(!S().projects.defaultProjectId)S().projects.defaultProjectId=p.id;
  return p;
}
async function createFolder(){
  const v=await askForm({title:'Новая папка',icon:'fa-folder-plus',ok:'Создать',fields:[{name:'name',label:'Название',ph:'Исследования'}]});
  if(!v||!v.name)return; state.folders.push({id:uid('f'),name:v.name,createdAt:Date.now()});
  save(); renderAll(); toast('Папка создана','ok');
}
async function renameFolder(f){
  const v=await askForm({title:'Переименовать',icon:'fa-folder',ok:'Сохранить',fields:[{name:'name',label:'Название',value:f.name}]});
  if(v&&v.name){f.name=v.name;save();renderAll()}
}
function deleteFolder(f){ state.folders=state.folders.filter(x=>x.id!==f.id);
  state.sessions.forEach(s=>{if(s.folderId===f.id)s.folderId=null}); save(); renderAll(); toast('Удалена','ok') }

/* ============================================================
   ПЕРЕМЕЩЕНИЕ СЕССИЙ В ПАПКИ
   Папки можно было только создать и удалить — положить в них сессию
   было нечем. Здесь общий механизм: одна сессия, несколько или все.
   ============================================================ */
function moveSessionsToFolder(ids,folderId,opts){
  opts=opts||{};
  const list=(Array.isArray(ids)?ids:[ids]).map(id=>state.sessions.find(s=>s.id===id)).filter(Boolean);
  if(!list.length)return 0;
  const before=list.map(s=>({s:s.id,folderId:s.folderId||null}));
  list.forEach(s=>{ s.folderId=folderId||null });
  save(); renderAll();
  if(!opts.silent){
    const name=folderId?(state.folders.find(f=>f.id===folderId)||{}).name:null;
    toast(name?('Перемещено в папку «'+name+'»'):'Убрано из папки','ok');
  }
  /* Отмена: возвращаем как было */
  pushHistory(
    async()=>{},
    async()=>{ before.forEach(b=>{ const s=state.sessions.find(x=>x.id===b.s); if(s)s.folderId=b.folderId }); save(); renderAll() }
  );
  return list.length;
}
/* Подменю выбора папки для контекстного меню и bulk-режима */
function folderPickerMenu(anchor,sessionIds){
  const r=anchor.getBoundingClientRect();
  const items=state.folders.map(f=>({
    icon:'fa-solid fa-folder',text:f.name,
    onClick:()=>moveSessionsToFolder(sessionIds,f.id)
  }));
  if(items.length)items.push('-');
  items.push({icon:'fa-solid fa-folder-plus',text:'Новая папка…',onClick:async()=>{
    const v=await askForm({title:'Новая папка',icon:'fa-folder-plus',ok:'Создать и переместить',
      fields:[{name:'name',label:'Название',ph:'Исследования'}]});
    if(!v||!v.name)return;
    const f={id:uid('f'),name:v.name,createdAt:Date.now()};
    state.folders.push(f);
    moveSessionsToFolder(sessionIds,f.id,{silent:true});
    toast('Создана папка «'+f.name+'»','ok');
  }});
  if(sessionIds.length===1){
    const s=state.sessions.find(x=>x.id===sessionIds[0]);
    if(s&&s.folderId)items.push('-',{icon:'fa-solid fa-folder-open',text:'Без папки',
      onClick:()=>moveSessionsToFolder(sessionIds,null)});
  }
  ctxMenu(r.left,r.bottom+6,items);
}

/* Выбор проекта для сессии: «Без проекта», затем список проектов и
   создание нового. Возвращает projectId (null = без проекта) либо
   undefined, если пользователь закрыл меню ничего не выбрав. */
function pickProjectForSession(anchorEl){
  return new Promise(res=>{
    let picked=false;
    const items=[{icon:'fa-solid fa-arrow-right-from-bracket',text:'Без проекта',onClick:()=>{ picked=true; res(null) }}];
    if(state.projects.length)items.push('-');
    state.projects.forEach(p=>items.push({
      icon:'fa-solid fa-diagram-project',text:p.name,onClick:()=>{ picked=true; res(p.id) }
    }));
    items.push('-',{icon:'fa-solid fa-folder-plus',text:'Новый проект…',onClick:async()=>{
      picked=true;
      const pr=await createProject();
      res(pr?pr.id:undefined);
    }});
    const r=anchorEl&&anchorEl.getBoundingClientRect?anchorEl.getBoundingClientRect():null;
    ctxMenu(r?r.left:innerWidth/2-140, r?r.bottom+6:innerHeight/2-120, items);
    /* Клик по пункту меню обрабатывает сам ctxMenu — там и ставится picked.
       Нас интересует только клик мимо меню: закрыл пользователь, ничего
       не выбрал. Слушатель в фазе capture, но игнорирует сам #ctx. */
    const onDoc=e=>{
      if(picked)return;
      if(e.target.closest&&e.target.closest('#ctx'))return;
      document.removeEventListener('click',onDoc,true);
      res(undefined);
    };
    setTimeout(()=>document.addEventListener('click',onDoc,true),0);
  });
}
/* ============================================================
   ФАЙЛОВЫЙ БРАУЗЕР ПРОЕКТА (панель «Файлы проекта»)
   Читает каталог через шлюз (/api/fs/list — пункт 2 бэкенда).
   Пока роутов нет — показывает подсказку вместо ошибки.
   ============================================================ */
const fsb={path:null,root:null,entries:[],error:null,expanded:true};
function fsApiAvailable(){ try{ return API&&API.fs&&typeof API.fs.list==='function' }catch(e){ return false } }
async function fsbLoad(dir){
  if(!dir){ fsb.path=null; fsb.entries=[]; fsb.error=null; return }
  fsb.path=dir;
  if(!fsApiAvailable()){
    fsb.entries=[]; fsb.error='Файловый API шлюза недоступен (нужны роуты /api/fs/*). Папка привязана — путь уже передаётся агенту.';
    return;
  }
  try{
    const r=await API.fs.list(dir);
    fsb.entries=(r&&r.entries)||[];
    fsb.error=r&&r.error?('Каталог не читается: '+r.error):null;
  }catch(e){ fsb.entries=[]; fsb.error='Файловый API шлюза недоступен: '+(e.message||e) }
}
/* Прикрепить путь (файл или папку) к чату из панели файлов.
   Общая точка для кнопки «стрелка» и для drag&drop, чтобы поведение
   (дедупликация, подтягивание содержимого) совпадало. */
function attachPathToChat(path,isDir){
  const p=String(path||'');
  if(!p)return false;
  if(ui.atts.some(a=>a.path&&a.path===p)){
    toast('Уже прикреплено','info'); return false;
  }
  const name=fileName(p)+(isDir?'/':'');
  const att=isDir
    ? {name,size:0,type:'folder',path:p,isDir:true}
    : {name,size:0,type:guessMime(p),path:p,isDir:false};
  ui.atts.push(att);
  renderAtts();
  if(!isDir)resolveFilePaths([att]).then(()=>renderAtts());
  else resolveFilePaths(ui.atts).then(()=>renderAtts());
  if(ui.chatOpen)openChat();
  toast('Прикреплено: '+name,'ok');
  return true;
}
/* Drag строки каталога в чат: путь уходит агенту невидимым контекстом (пункт 6). */
function fsbAttachDrop(){
  const list=$('#fsbList'); if(!list)return;
  list.querySelectorAll('[data-fsbrow]').forEach(row=>{
    row.addEventListener('dragstart',e=>{
      e.stopPropagation();
      const p=row.dataset.fsbpath, isDir=row.dataset.fsbdir==='1';
      /* Кладём путь в dataTransfer: он переживёт и быстрый перенос, и смену
         вкладки. Переменная ui.fsbDrag обнуляется на dragend и могла
         исчезнуть раньше, чем сработает drop. */
      try{ e.dataTransfer.setData('application/x-infinity-path',JSON.stringify({path:p,isDir})) }catch(err){}
      e.dataTransfer.setData('text/plain',p);
      e.dataTransfer.effectAllowed='copy';
      row.classList.add('dragging'); document.body.classList.add('dragging-att');
      ui.fsbDrag={path:p,isDir};
    });
    row.addEventListener('dragend',()=>{
      row.classList.remove('dragging'); document.body.classList.remove('dragging-att');
      ui.fsbDrag=null;
    });
  });
}
function renderFileBrowser(){
  const s=active(); const box=$('#fsbBox'); if(!box)return;
  const p=projectOf(s);
  /* Сессия может быть вне проекта — тогда показываем файлы активного
     проекта (его считает renderDetails). Раньше здесь стоял жёсткий
     return, из-за чего панель исчезала и перетаскивание было невозможно. */
  const activeProj=(!p&&state.activeProjectId)?state.projects.find(x=>x.id===state.activeProjectId):null;
  const proj=p||activeProj;
  if(!proj){ box.style.display='none'; box.innerHTML=''; return }
  box.style.display='';
  const root=proj.absPath||proj.path;
  /* Сменился проект — забываем открытую подпапку, иначе покажем
     файлы чужой директории. */
  if(fsb.root!==root){ fsb.path=null; fsb.root=root; fsb.entries=[]; fsb.error=null }
  const cur=fsb.path||root;
  const head='<div class="fsb-head"><span class="fsb-path" title="'+esc(cur)+'">'+esc(cur)+'</span>'+
    '<span class="fsb-tools">'+
    (fsb.path&&fsb.path!==root?'<button id="fsbUp" title="На уровень выше"><i class="fa-solid fa-arrow-up"></i></button>':'')+
    '<button id="fsbReload" title="Обновить"><i class="fa-solid fa-rotate"></i></button></span></div>';
  let body='';
  if(fsb.error&&!fsb.entries.length){
    body='<div class="fsb-err">'+esc(fsb.error)+'</div>';
  }else if(!fsb.entries.length){
    /* Раньше здесь была пустота: непонятно, сломано что-то или каталог пуст.
       Разводим эти случаи и подсказываем, что делать. */
    const hasPath=!!cur;
    body='<div class="fsb-hint">'+
      (hasPath?'Каталог пуст или пока не прочитан.':'У проекта не задан путь к папке — список файлов недоступен. '+
        'Откройте «Проекты» в настройках и укажите путь, либо создайте проект заново через выбор папки.')+
      '</div>';
  }else{
    body='<div class="fsb-list" id="fsbList">'+fsb.entries.map(en=>{
      const isDir=!!en.isDirectory, k=fileKind(en.name);
      /* Просмотр/правка есть не у всего: картинки и бинарники открываются
         своими панелями, поэтому «карандаш» им не нужен. */
      const canOpen=!isDir&&k.mode!=='bin'&&k.mode!=='image';
      return '<div class="fsb-row" draggable="true" data-fsbrow="1" data-fsbdir="'+(isDir?1:0)+'" data-fsbpath="'+esc(en.path)+'">'+
        '<i class="fa-solid '+(isDir?'fa-folder':k.ico)+'"></i>'+
        '<span class="nm" title="'+esc(en.path)+'">'+esc(en.name)+'</span>'+
        (en.size?'<span class="sz">'+fmtBytes(en.size)+'</span>':'')+
        '<span class="fsb-acts">'+
          '<button class="att" data-act="attach" title="Прикрепить к чату — отдать агенту путь"><i class="fa-solid fa-arrow-right-to-bracket"></i></button>'+
          (canOpen?'<button data-act="view" title="Просмотр"><i class="fa-regular fa-eye"></i></button>':'')+
          (canOpen?'<button data-act="edit" title="Редактировать"><i class="fa-solid fa-pen"></i></button>':'')+
        '</span>'+
        (isDir?'<i class="fa-solid fa-chevron-right" style="width:8px"></i>':'')+'</div>';
    }).join('')+'</div>';
  }
  const hint='<div class="fsb-hint">Клик — открыть папку. Перетащите файл/папку в чат — спросить у агента.</div>';
  box.innerHTML=head+body+hint;
  const rl=$('#fsbReload'); if(rl)rl.onclick=async()=>{ await fsbLoad(cur); renderFileBrowser() };
  const up=$('#fsbUp');
  if(up)up.onclick=async()=>{
    const norm=cur.replace(/[\\/]+$/,'');
    const i=Math.max(norm.lastIndexOf('\\'),norm.lastIndexOf('/'));
    if(i>0){ await fsbLoad(norm.slice(0,i)); renderFileBrowser() }
  };
  box.querySelectorAll('[data-fsbrow]').forEach(row=>{
    row.onclick=async ev=>{
      /* Клик по кнопке действия не должен открывать папку под ней */
      const btn=ev.target.closest('.fsb-acts button');
      const path=row.dataset.fsbpath, isDir=row.dataset.fsbdir==='1';
      if(btn){
        ev.stopPropagation();
        const act=btn.dataset.act;
        if(act==='attach'){ attachPathToChat(path,isDir); return }
        /* Просмотр/правка есть только у файлов — папку так не открыть */
        if(isDir)return;
        if(act==='view') openFilePanel(path,'preview');
        else if(act==='edit') openFilePanel(path,'edit');
        return;
      }
      if(!isDir)return;
      await fsbLoad(path); renderFileBrowser();
    };
  });
  fsbAttachDrop();
}
