/* 09-init-api.js — Инициализация, API-клиент, шлюз, режим «Диалог» */
/* ============================================================
   INIT
   ============================================================ */
function renderAll(){ renderTree(); renderHead(); renderStream(); renderDetails(); setSend(); updateConnPill(); updUndoRedo(); updatePomoUI(); if(state.toolsPanelOpen)renderToolsPanel(); renderQueueChip(); }

function demoData(){ return; }

/* ============================================================
 ЖИВЫЕ ДАННЫЕ ШЛЮЗА: slash-команды (cmdPop) + модели
 ============================================================ */
let GATEWAY_CMDS=[];
async function loadCommands(){
 try{ 
 const r=await fetch('/api/commands'); 
 if(!r.ok)return;
 const d=await r.json(); 
 GATEWAY_CMDS=d.data||[];
 const pop=$('#cmdPop'); 
 if(!pop)return;
 
 // Группируем команды по категориям
 const categories={};
 GATEWAY_CMDS.forEach(c=>{
 const cat=c.category||'Прочее';
 if(!categories[cat])categories[cat]=[];
 categories[cat].push(c);
 });
 
 // Строим HTML с категориями
 let html='<b>Команды infinity CLI</b>';
 Object.keys(categories).sort().forEach(cat=>{
 html+='<div style="margin-top:8px;font-size:10px;color:var(--text-3);text-transform:uppercase;letter-spacing:.05em">'+esc(cat)+'</div>';
 categories[cat].forEach(c=>{
 html+='<button data-cmd="'+esc(c.name)+'" title="'+esc(c.description)+'">'+
 '<i class="fa-solid fa-terminal"></i> '+esc(c.name)+
 (c.args?'<span style="opacity:.55;font-size:10px">'+esc(c.args)+'</span>':'')+
 '</button>';
 });
 });
 
 pop.innerHTML=html;
 console.log('Loaded slash commands:',GATEWAY_CMDS.length);
 }catch(e){
 console.error('Failed to load commands:',e);
 }
}

async function loadGatewayModels(){
 try{ 
 const r=await fetch('/v1/models',{headers:{Authorization:'Bearer inf-2f9c4d7a8e1b3c5d6a9f0e2b4d6c8a0f'}});
 if(!r.ok)return; 
 const d=await r.json();
 const ids=(d.data||[]).map(m=>m.id).filter(Boolean);
 if(!ids.length)return;
 MODELS.length=0; 
 ids.slice(0,20).forEach(id=>MODELS.push({id,n:id,ctx:200000}));
 renderModels();
 }catch(e){}
}

// Загрузка реальных инструментов из backend
async function loadRealTools(){
 try{
 const toolsets=await API.tools.toolsets().catch(e=>{ console.warn('toolsets:',e); return null });
 // Навыки нужны для вкладки «Инструменты». Эндпоинта может не быть
 // в этой сборке шлюза — тогда навыков просто ноль, а не ошибка загрузки.
 const skills=await API.tools.skills().catch(e=>{ console.warn('skills:',e); return null });

 // Обновляем список TOOLS реальными данными
 if(toolsets&&toolsets.toolsets){
 TOOLS.length=0;
 toolsets.toolsets.forEach(ts=>{
 if(ts.tools&&Array.isArray(ts.tools)){
 ts.tools.forEach(tool=>{
 TOOLS.push({
 k:tool.name||tool,
 n:tool.display_name||tool.name||tool,
 d:tool.description||'Инструмент backend',
 i:getToolIcon(tool.name||tool),
 enabled:true
 });
 });
 }
 });
 }
 // Навыки: приводим к списку объектов с названием и описанием
 SKILLS.length=0;
 const raw=Array.isArray(skills)?skills
   :(skills&&(Array.isArray(skills.skills)?skills.skills:null));
 (raw||[]).forEach(s=>{
  if(typeof s==='string') SKILLS.push({k:s,n:s,d:''});
  else if(s&&typeof s==='object'){
   const k=String(s.name||s.id||'').trim(); if(!k) return;
   SKILLS.push({k, n:String(s.display_name||s.title||s.name||k),
                d:String(s.description||s.desc||''),
                i:skillIcon(k),
                enabled:s.enabled!==false});
  }
 });
 console.log('Loaded real tools:',TOOLS.length,'| skills:',SKILLS.length);
 // Данные пришли после первой отрисовки — обновляем открытую вкладку
  // Шлюз мог ничего не отдать — добираем список с диска, иначе вкладка
  // «Инструменты» осталась бы пустой без объяснения.
  if(!TOOLS.length) await loadToolsFallback();

 if(typeof renderToolsSettings==='function')renderToolsSettings();
 }catch(e){
 console.error('Failed to load real tools:',e);
}
}

/* Резервный список с диска, когда шлюз ничего не отдал.
   Без него вкладка «Инструменты» выглядела пустой, хотя инструменты на
   диске есть: /v1/toolsets есть не во всех сборках шлюза, и при его
   недоступности TOOLS оставался пустым без всякого объяснения.
   Возвращает true, если что-то удалось получить. */
async function loadToolsFallback(){
 let d=null;
 try{ d=await API.tools.toolsLocal() }catch(e){ console.warn('tools/local:',e) }
 if(!d)return false;
 let added=0;
 (d.tools||[]).forEach(t=>{
  const k=String(t.name||'').trim(); if(!k)return;
  if(TOOLS.some(x=>x.k===k))return;          /* шлюз уже отдал — не дублируем */
  TOOLS.push({k,n:k,d:t.desc||('Модуль '+(t.file||k)),i:getToolIcon(k),
              enabled:true,local:true});
  added++;
 });
 (d.skills||[]).forEach(s=>{
  const k=String(s.name||'').trim(); if(!k)return;
  if(SKILLS.some(x=>x.k===k))return;
  SKILLS.push({k,n:k,d:s.desc||'Навык из backend/skills',i:skillIcon(k),
               enabled:true,local:true,path:s.path});
  added++;
 });
 if(added&&typeof renderToolsSettings==='function')renderToolsSettings();
 return added>0;
}

// Получение иконки для инструмента
function getToolIcon(toolName){
 const name=(toolName||'').toLowerCase();
 if(name.includes('file')||name.includes('read')||name.includes('write'))return'fa-solid fa-file-lines';
 if(name.includes('terminal')||name.includes('command')||name.includes('shell'))return'fa-solid fa-terminal';
 if(name.includes('web')||name.includes('search')||name.includes('browser'))return'fa-solid fa-globe';
 if(name.includes('code')||name.includes('python')||name.includes('exec'))return'fa-solid fa-code';
 if(name.includes('image')||name.includes('vision'))return'fa-regular fa-image';
 if(name.includes('task')||name.includes('todo')||name.includes('plan'))return'fa-solid fa-list-check';
 if(name.includes('memory')||name.includes('recall'))return'fa-solid fa-brain';
 if(name.includes('delegate')||name.includes('agent'))return'fa-solid fa-users';
 if(name.includes('git'))return'fa-solid fa-code-branch';
 if(name.includes('cron')||name.includes('schedule'))return'fa-solid fa-clock';
 return'fa-solid fa-screwdriver-wrench';
}

// Иконка навыка по его названию: у навыков нет типа инструмента,
// поэтому ориентируемся на слова в имени.
function skillIcon(skillName){
 const n=(skillName||'').toLowerCase();
 if(n.includes('doc')||n.includes('write')||n.includes('text'))return'fa-solid fa-file-pen';
 if(n.includes('test')||n.includes('qa')||n.includes('review'))return'fa-solid fa-vial-circle-check';
 if(n.includes('deploy')||n.includes('build')||n.includes('ci'))return'fa-solid fa-rocket';
 if(n.includes('data')||n.includes('sql')||n.includes('db'))return'fa-solid fa-database';
 if(n.includes('design')||n.includes('ui')||n.includes('css'))return'fa-solid fa-palette';
 if(n.includes('security')||n.includes('audit'))return'fa-solid fa-shield-halved';
 if(n.includes('git')||n.includes('commit'))return'fa-solid fa-code-branch';
 if(n.includes('api')||n.includes('http')||n.includes('web'))return'fa-solid fa-globe';
 if(n.includes('debug')||n.includes('fix')||n.includes('error'))return'fa-solid fa-bug';
 return'fa-solid fa-wand-magic-sparkles';
}

// Загрузка рабочей директории
async function loadWorkingDirectory(){
 try{
 const cwd=await API.fs.defaultCWD();
 if(cwd&&cwd.cwd){
 // Запоминаем CWD шлюза — используется как резервная рабочая папка
 state.gatewayCwd=cwd.cwd;
 console.log('Working directory:',cwd.cwd);
 
 // Если есть активная сессия без проекта, показываем CWD
 const s=active();
 if(s&&!s.projectId){
 const fPath=$('#fPath');
 if(fPath){
 fPath.style.display='';
 fPath.lastElementChild.textContent=cwd.cwd;
 fPath.title='Рабочая директория';
 }
 }
 }
 }catch(e){
 console.warn('Working directory API недоступен (роуты /api/fs/* ещё не включены):',e&&e.message?e.message:e);
 }
}

/* ============================================================
 ПОЛНЫЙ API КЛИЕНТ (все эндпоинты Hermes)
 ============================================================ */
const API={
 baseURL:'',
 key:'inf-2f9c4d7a8e1b3c5d6a9f0e2b4d6c8a0f',
 
 // Helper для запросов
 async req(method,path,body=null,headers={}){
 try{
 const opts={
 method,
 headers:{
 'Content-Type':'application/json',
 'Authorization':`Bearer ${this.key}`,
 ...headers
 }
 };
 if(body)opts.body=JSON.stringify(body);
 const r=await fetch(this.baseURL+path,opts);
 if(!r.ok){
 const err=await r.json().catch(()=>({error:'HTTP '+r.status}));
 // Шлюз 0.23 отдаёт ошибку объектом {message, code}; без разбора было бы «[object Object]».
 const eo=err&&err.error;
 const em=(eo&&typeof eo==='object')?(eo.message||eo.code):(eo||err.message);
 throw new Error(em||('HTTP '+r.status));
 }
 return await r.json();
 }catch(e){
 console.error(`API ${method}${path}:`,e);
 throw e;
 }
 },
 
 // === СЕССИИ ===
 sessions:{
 list:()=>API.req('GET','/api/sessions'),
 create:(title)=>API.req('POST','/api/sessions',{title}),
 get:(id)=>API.req('GET',`/api/sessions/${id}`),
 update:(id,data)=>API.req('PATCH',`/api/sessions/${id}`,data),
 delete:(id)=>API.req('DELETE',`/api/sessions/${id}`),
 fork:(id)=>API.req('POST',`/api/sessions/${id}/fork`),
 setModel:(id,model)=>API.req('POST',`/api/sessions/${id}/model`,{model}),
 search:(query)=>API.req('POST','/api/sessions/search',{query}),
 bulkDelete:(ids)=>API.req('POST','/api/sessions/bulk-delete',{session_ids:ids}),
 prune:(filters)=>API.req('POST','/api/sessions/prune',filters),
 import:(data)=>API.req('POST','/api/sessions/import',data),
 export:(id,format='json')=>API.req('GET',`/api/sessions/${id}/export?format=${format}`),
 rename:(id,title)=>API.req('POST','/api/sessions/rename',{session_id:id,title}),
 },
 
 // === СООБЩЕНИЯ ===
 messages:{
 list:(sessionId,limit=100)=>API.req('GET',`/api/sessions/${sessionId}/messages?limit=${limit}`),
 send:(sessionId,message,stream=false)=>API.req('POST',`/api/sessions/${sessionId}/chat`,{message,stream}),
 },
 
 // === МОДЕЛИ ===
 models:{
 list:()=>API.req('GET','/v1/models'),
 options:()=>API.req('GET','/api/model/options'),
 capabilities:()=>API.req('GET','/v1/capabilities'),
 catalog:()=>API.req('GET','/api/models/catalog'),
 recommended:()=>API.req('GET','/api/model/recommended-default'),
 },
 
 // === ИНСТРУМЕНТЫ ===
 tools:{
 list:()=>API.req('GET','/api/tools/registry'),
 execute:(name,args)=>API.req('POST','/api/tools/execute',{tool:name,args}),
 toolsets:()=>API.req('GET','/v1/toolsets'),
 skills:()=>API.req('GET','/v1/skills'),
 // Резервный список с диска: шлюз может не отдавать /v1/toolsets, а
 // пользователю всё равно нужно видеть, что у агента есть. Метод обязан
 // лежать ВНУТРИ tools: loadToolsFallback зовёт API.tools.toolsLocal().
 // Раньше строка стояла на уровень выше (в самом API), и резервный список
 // падал с «API.tools.toolsLocal is not a function».
 toolsLocal:()=>API.req('GET','/api/tools/local'),
 },

  // === ФАЙЛЫ ИНСТРУКЦИЙ АГЕНТА (Настройки → Агент) ===
  // На сервере белый список имён; путь отсюда не передаётся. Эндпоинты
  // локальные: если шлюз «не разрешает», файл всё равно читается с диска —
  // это собственный сервер приложения, а не шлюз Hermes.
  agentFiles:()=>API.req('GET','/api/agent/files'),
  agentFileRead:(name)=>API.req('GET','/api/agent/file?name='+encodeURIComponent(name)),
  agentFileWrite:(name,content)=>API.req('POST','/api/agent/file',{name,content}),
  // Системные инструкции живут на сервере, а не в браузере: иначе после
  // перезапуска сервера поле выглядело пустым.
  agentPrompt:()=>API.req('GET','/api/agent/prompt'),
  agentPromptSave:(content)=>API.req('POST','/api/agent/prompt',{content}),

 // === ФАЙЛЫ ===
 files:{
 list:(path='.')=>API.req('GET',`/api/files?path=${encodeURIComponent(path)}`),
 read:(path)=>API.req('GET',`/api/files/read?path=${encodeURIComponent(path)}`),
 download:(path)=>API.req('GET',`/api/files/download?path=${encodeURIComponent(path)}`),
 upload:(path,file)=>{
 const formData=new FormData();
 formData.append('file',file);
 formData.append('path',path);
 return fetch(API.baseURL+'/api/files/upload',{
 method:'POST',
 headers:{'Authorization':`Bearer ${API.key}`},
 body:formData
 }).then(r=>r.json());
 },
 mkdir:(path)=>API.req('POST','/api/files/mkdir',{path}),
 delete:(path)=>API.req('DELETE',`/api/files?path=${encodeURIComponent(path)}`),
 },
 
 // Файловая система (альтернативные эндпоинты)
 fs:{
 list:(path)=>API.req('GET',`/api/fs/list?path=${encodeURIComponent(path)}`),
 readText:(path)=>API.req('GET',`/api/fs/read-text?path=${encodeURIComponent(path)}`),
 writeText:(path,content)=>API.req('POST','/api/fs/write-text',{path,content}),
 readDataURL:(path)=>API.req('GET',`/api/fs/read-data-url?path=${encodeURIComponent(path)}`),
 download:(path)=>API.req('GET',`/api/fs/download?path=${encodeURIComponent(path)}`),
 gitRoot:(fromPath)=>API.req('GET',`/api/fs/git-root?from_path=${encodeURIComponent(fromPath||'')}`),
 defaultCWD:()=>API.req('GET','/api/fs/default-cwd'),
 },
 
 // === ГОЛОС ===
 audio:{
 transcribe:(audioBlob)=>{
 const formData=new FormData();
 formData.append('audio',audioBlob);
 return fetch(API.baseURL+'/api/audio/transcribe',{
 method:'POST',
 headers:{'Authorization':`Bearer ${API.key}`},
 body:formData
 }).then(r=>r.json());
 },
 speak:(text,voice=null,rate=1,pitch=1)=>API.req('POST','/api/audio/speak',{text,voice,rate,pitch}),
 voiceConfig:()=>API.req('GET','/api/audio/voice-config'),
 liveStatus:()=>API.req('GET','/api/audio/voice-live/status'),
 liveSession:()=>API.req('POST','/api/audio/voice-live/session'),
 },
 
 // === GIT ===
 git:{
 status:()=>API.req('GET','/api/git/status'),
 ghAuth:()=>API.req('GET','/api/git/gh-auth'),
 worktrees:()=>API.req('GET','/api/git/worktrees'),
 branches:()=>API.req('GET','/api/git/branches'),
 baseBranches:()=>API.req('GET','/api/git/base-branches'),
 // fileDiff(root,file): root — корень git-репозитория, file — путь относительно
  // него. Бэкенд (hermes_cli/web_routers/git.py) требует оба параметра.
  fileDiff:(root,file)=>API.req('GET',`/api/git/file-diff?path=${encodeURIComponent(root||'')}&file=${encodeURIComponent(file||'')}`),
 stage:(files)=>API.req('POST','/api/git/review/stage',{files}),
 commit:(message)=>API.req('POST','/api/git/review/commit',{message}),
 push:()=>API.req('POST','/api/git/review/push'),
 createPR:(title,body)=>API.req('POST','/api/git/review/create-pr',{title,body}),
 switchBranch:(branch)=>API.req('POST','/api/git/branch/switch',{branch}),
 },
 
 // === КОНФИГУРАЦИЯ ===
 config:{
 getRaw:()=>API.req('GET','/api/config/raw'),
 setRaw:(content)=>API.req('PUT','/api/config/raw',{content}),
 set:(key,value)=>API.req('PUT','/api/config',{key,value}),
 },
 
 env:{
 list:()=>API.req('GET','/api/env'),
 set:(key,value)=>API.req('PUT','/api/env',{key,value}),
 delete:(key)=>API.req('DELETE',`/api/env?key=${encodeURIComponent(key)}`),
 reveal:(key)=>API.req('POST','/api/env/reveal',{key}),
 },
 
 // === CRON JOBS ===
 jobs:{
 list:()=>API.req('GET','/api/jobs'),
 create:(job)=>API.req('POST','/api/jobs',job),
 get:(id)=>API.req('GET',`/api/jobs/${id}`),
 update:(id,data)=>API.req('PATCH',`/api/jobs/${id}`,data),
 delete:(id)=>API.req('DELETE',`/api/jobs/${id}`),
 pause:(id)=>API.req('POST',`/api/jobs/${id}/pause`),
 resume:(id)=>API.req('POST',`/api/jobs/${id}/resume`),
 run:(id)=>API.req('POST',`/api/jobs/${id}/run`),
 },
 
 // === RUNS API (Async) ===
 runs:{
 create:(message,sessionId=null,model=null)=>API.req('POST','/v1/runs',{user_message:message,session_id:sessionId,model}),
 get:(id)=>API.req('GET',`/v1/runs/${id}`),
 events:(id)=>API.req('GET',`/v1/runs/${id}/events`),
 stop:(id)=>API.req('POST',`/v1/runs/${id}/stop`),
 steer:(id,message)=>API.req('POST',`/v1/runs/${id}/steer`,{message}),
 approval:(id,choice,requestId,generation)=>API.req('POST',`/v1/runs/${id}/approval`,
   {request_id:requestId,execution_generation:generation,choice}),
 clarify:(id,answer,requestId,generation)=>API.req('POST',`/v1/runs/${id}/clarify`,
   {request_id:requestId,execution_generation:generation,answer}),
 },
 
 // === ПРОФИЛИ ===
 profiles:{
 list:()=>API.req('GET','/api/profiles'),
 create:(name,cloneFrom=null)=>API.req('POST','/api/profiles',{name,clone_from:cloneFrom}),
 delete:(name)=>API.req('DELETE',`/api/profiles/${name}`),
 switch:(name)=>API.req('POST',`/api/profiles/${name}/switch`),
 sessions:(name)=>API.req('GET',`/api/profiles/${name}/sessions`),
 },
 
 // === СТАТУС ===
 status:{
 health:()=>API.req('GET','/health'),
 healthDetailed:()=>API.req('GET','/health/detailed'),
 system:()=>API.req('GET','/api/status'),
 checkUpdate:()=>API.req('GET','/api/hermes/update/check'),
 update:(backup=true)=>API.req('POST','/api/hermes/update',{backup}),
 },
 
 // === АНАЛИТИКА ===
 analytics:{
 usage:()=>API.req('GET','/api/analytics/usage'),
 models:()=>API.req('GET','/api/analytics/models'),
 },
 
 // === ПРОВАЙДЕРЫ ===
 providers:{
 customEndpoints:()=>API.req('GET','/api/providers/custom-endpoints'),
 createEndpoint:(data)=>API.req('POST','/api/providers/custom-endpoints',data),
 deleteEndpoint:(id)=>API.req('DELETE',`/api/providers/custom-endpoints/${id}`),
 validate:(provider,config)=>API.req('POST','/api/providers/validate',{provider,config}),
 },
};

async function init(){
  try{ await load(); }catch(e){ console.warn('Load failed:',e); }
  // инициализация pomodoro
  if(!S().pomodoro.todayDate){
    S().pomodoro.todayDate=new Date().toDateString();
    S().pomodoro.todaySessions=0;
  }
  ui.pomo.seconds=S().pomodoro.focus*60;
  renderModels(); bindSettings(); bindCronBell(); fillVoices(); loadDevices(); apply(); applyTheme(); demoData();
  /* Делегированные обработчики списка движков: биндим один раз за сессию,
     элементы внутри #engSttBox/#engTtsBox перерисовываются при каждом рендере.
     Функция объявлена в 12-voice-engines.js, который подключается ПОЗЖЕ
     этого файла, поэтому прямой вызов падал с ReferenceError и обрывал init.
     Если её ещё нет — ждём DOMContentLoaded, когда все скрипты уже загружены. */
  if (typeof bindVoiceEngines === 'function') bindVoiceEngines();
  else document.addEventListener('DOMContentLoaded', function () {
    if (typeof bindVoiceEngines === 'function') bindVoiceEngines();
  });
  if(!active()&&state.sessions[0])state.activeSessionId=state.sessions[0].id;
  if(!active())newSession();
 renderAll();
 
 // Восстанавливаем состояние панелей
 applySidePanel();
 
 applyDetailsPanel();
 
 if(state.compactMode){
 const chatEl=$('#chat');
 if(chatEl)chatEl.classList.add('compact');
 const hdComp=$('#hdCompact');
 if(hdComp)hdComp.classList.add('on');
 }
 
 if(state.fullscreenMode){
 const overlay=$('#chatOverlay');
 if(overlay)overlay.classList.add('fullscreen');
 const hdFull=$('#hdFullscreen');
 if(hdFull){
 hdFull.classList.add('on');
 const icon=hdFull.querySelector('i');
 if(icon)icon.className='fa-solid fa-compress';
 }
 }
 
 if(state.toolsPanelOpen){
 const panel=$('#toolsPanel');
 if(panel)panel.classList.add('open');
 const hdToolsBtn=$('#hdTools');
 if(hdToolsBtn)hdToolsBtn.classList.add('on');
 renderToolsPanel(); // панель была открыта — сразу показываем вызовы текущей сессии
 }

/* Терминал был открыт при прошлом запуске (или включён в настройках) —
   восстанавливаем панель. xterm подключается лениво, поэтому откладываем
   до конца анимации открытия. */
if(state.termDrawerOpen||(S().tools&&S().tools.liveTerminal)){
  setTimeout(()=>{ try{ toggleTerm(true) }catch(e){ console.warn('Term restore:',e) } },700);
}

 
 /* Плагины: сначала встроенные (мигрированные из старого формата), затем
    подтягиваем файлы из папки и включаем наблюдение за ней. PluginBus.init()
    сам эмитит 'ready'; PluginFS.start() догрузит файловые плагины после. */
  PluginBus.init();
  // Python-плагины живут на сервере: сообщаем им 'ready' после старта.
  if(typeof pyNotifyReady==='function')setTimeout(()=>pyNotifyReady(),50);
  // Наблюдение за папкой плагинов: подхват новых/изменённых .js на лету.
  // Через file:// серверные эндпоинты недоступны — start() просто выйдет.
  setTimeout(()=>{ PluginFS.start() },0);
  renderPluginsSettings();
  if(S().connection.enabled)Gateway.connect().then(updateConnPill);
  if(S().general.autostartChat)openChat();
  setTimeout(()=>{ if(!active()||ui.listening)return; setStatus('Готов к работе') },300);
  // Восстановленная очередь: если агент свободен — продолжаем автоотправку
  setTimeout(()=>{ if(!ui.generating&&ui.msgQueue.length){ flushQueueSoon(500); toast('Очередь восстановлена: '+ui.msgQueue.length+' сообщ.','info') } },1500);
  if(!localStorage.getItem('agent.tourSeen.v2')){
    setTimeout(()=>{
      if(confirm('Показать тур по новым возможностям?')){
        localStorage.setItem('agent.tourSeen.v2','1');
        openChat();
        showTour();
      } else {
        localStorage.setItem('agent.tourSeen.v2','1');
      }
    },800);
  }
}

/* ============================================================
 BACKEND GATEWAY (Hermes Integration)
 ============================================================ */
const Gateway={
 connected:false,
 async connect(){ 
 try{ 
 const r=await fetch('/api/health'); 
 this.connected=r.ok;
 if(this.connected){ 
 // Загружаем все данные из backend
 await Promise.all([
 loadCommands(),
 loadGatewayModels(),
 loadRealTools(),
 loadWorkingDirectory()
 ]);
 }
 return this.connected 
 }catch(e){ 
 this.connected=false; 
 return false 
 } 
 },
 ping(){ return this.connect() },
 async ensureBackend(s){ 
 if(s.backendId)return s.backendId;
 const create=(title)=>fetch('/api/sessions',{method:'POST',
 headers:{'Content-Type':'application/json','X-Hermes-Session-Key':'inf-'+s.id},
 body:JSON.stringify({title:title})});
 const errMsg=async(r)=>{ 
 let m='HTTP '+r.status;
 try{ const e=await r.json(); const t=e&&e.error; m=(t&&(t.message||t))||m }catch(_){ }
 return m };
 try{
 let r=await create(s.title);
 if(!r.ok){ const m=await errMsg(r);
 if(r.status===400 && /already in use/i.test(m)){
 r=await create(s.title+' ('+Date.now().toString(36)+')'); }
 if(!r.ok){ toast('Шлюз: сессия не создана — '+(await errMsg(r)),'err'); return null } }
 const d=await r.json(); s.backendId=(d.session&&d.session.id)||null; save();
 return s.backendId 
 }catch(e){ toast('Шлюз недоступен: '+e,'err'); return null } 
 }
};

function updateConnPill(){ 
 const p=$('#connPill');
 if(!p)return;
 if(S().connection.enabled&&Gateway.connected){
 p.className='pill live';
 p.innerHTML='<i class="fa-solid fa-circle"></i><span>Backend подключён</span>';
 p.title='infinity Gateway работает';
 }
 else if(S().connection.enabled){
 p.className='pill';
 p.innerHTML='<i class="fa-solid fa-plug-circle-xmark"></i><span>Нет соединения</span>';
 p.title='Не удалось подключиться к infinity Gateway';
 }
 else {
 p.className='pill';
 p.innerHTML='<i class="fa-solid fa-circle"></i><span>Локальный режим</span>';
 p.title='Backend отключен в настройках';
 } 
}

/* ============================================================
   РЕЖИМ «ДИАЛОГ» — прямой чат с моделью в обход агента Hermes.
   Ни инструментов, ни памяти, ни роутинга: обычный стриминг
   /v1/chat/completions у той же модели, что настроена в шлюзе.
   Работает и из чата, и голосом с главного экрана.
   ============================================================ */
let _directCfg=null;
/* URL/Kлюч из настроек важнее серверных: то, что вставлено в Настройки → Агент,
   работает всегда, даже если сервер не смог прочитать config.yaml */
async function directConfig(){
  const local=(S().agent.directUrl||'').trim();
  const localKey=(S().agent.directKey||'').trim();
  if(!_directCfg){
    try{
      const r=await fetch('/api/direct/config');
      const j=await r.json();
      if(j&&typeof j==='object'&&j.base_url)_directCfg=j;   /* неудачу не кэшируем */
    }catch(e){ /* старая сборка сервера — не страшно, есть поле в настройках */ }
  }
  const srv=_directCfg||{base_url:'',model:'',key:''};
  return {
    base_url:(local||srv.base_url||'').replace(/\/+$/,''),
    model:srv.model||'',
    key:localKey||srv.key||'',
  };
}
/* Заглушки-названия из UI: это не настоящие id моделей, для прямого режима не годятся */
const PLACEHOLDER_MODEL=/^agent-(pro|fast|code|lite)$/;
/* Имя модели: сессия → серверный конфиг → первый id из GET /models */
async function directModelId(base,cfg){
  const s=active(); const sess=s&&s.model;
  if(sess&&!PLACEHOLDER_MODEL.test(sess))return sess;
  if(cfg.model)return cfg.model;
  try{
    const r=await fetch(base+'/models');
    const j=await r.json();
    const d=(j&&j.data&&j.data[0])||{};
    return d.id||d.model||'';
  }catch(e){ return '' }
}
/* Кнопка «Проверить» в настройках: опрашивает /models выбранного URL */
const _directTestBtn=$('#directTest');if(_directTestBtn)_directTestBtn.onclick=async()=>{
  const cfg=await directConfig();
  if(!cfg.base_url){ toast('Сначала укажите URL модели','err'); return }
  try{
    const r=await fetch(cfg.base_url+'/models');
    if(!r.ok)throw new Error('HTTP '+r.status);
    const j=await r.json();
    const n=(j&&j.data&&j.data.length)||0;
    toast('Модель отвечает, доступно: '+n,'ok');
  }catch(e){ toast('Нет связи: '+((e&&e.message)||e),'err') }
};


async function directTurn(text,atts){
  const s=active(); if(!s)return;
  ui.turnSeq=(ui.turnSeq||0)+1;
  const cfg=await directConfig();
  const base=String(cfg.base_url||'').replace(/\/+$/,'');
  if(!base){
    toast('Диалог: укажите URL модели в Настройки → Агент','err');
    ui._errorStartNoRetry=true;
    if(stageTurn){ stageTurn=false; stageReply('','',false); setStatus('Укажите URL модели в настройках') }
    return;
  }
  /* Текст с вложениями: содержимое файлов подмешиваем в запрос (без инструментов).
     Картинки уходят настоящими частями image_url (vision-модель их увидит) и
     дублируются текстом с абсолютным путём — как при прикреплении файла. */
  let prompt=String(text||'');
  const imgParts=[];
  if(Array.isArray(atts)&&atts.length){
    atts.forEach(a=>{ if(!a)return;
      if(a.dataUrl&&String(a.type||'').startsWith('image/')){
        imgParts.push({type:'image_url',image_url:{url:a.dataUrl}});
        let ip='\n\n[Изображение: '+a.name+']';
        if(a.path)ip+='\n[Путь к файлу: '+a.path+']';
        prompt+=ip;
      }
      else if(a.content)prompt+='\n\n['+a.name+']\n'+String(a.content).slice(0,20000);
      else if(a.path)prompt+='\n\n[Файл: '+a.path+']';
      /* Раньше файл без пути и без содержимого просто молча терялся —
         модель о нём не узнавала вообще. Теперь сообщаем явно. */
      else prompt+='\n\n[Файл: '+a.name+' — недоступен: путь не определён, содержимое не приложено. '+
                    'Не выдумывай его содержимое; если он нужен — попроси пользователя указать путь.]' });
  }
  const model=await directModelId(base,cfg);
  ui.generating=true; ui.abort=false; setSend(); renderHead(); setStatus('Думаю…');
  queueMarkTurnStart();
  /* Новый ход — снимаем заглушку озвучки, если её ставил «стоп» в прошлом ходе.
     Поток от сервера обрывает только stopByVoice/abort, а LiveTTS.stop() сам по
     себе глушит дельты до конца генерации, поэтому разблокировка нужна именно
     здесь, в самом начале запроса. */
  if(typeof LiveTTS!=='undefined')LiveTTS.arm();
  let cur=null,finalText='',err=null,stopped=false,ctl=null,reason=null;
  const t0=performance.now();
  try{
    const messages=[];
    const sys=(S().agent.systemPrompt||'').trim();
    if(sys) messages.push({role:'system',content:sys});
    s.messages.filter(m=>m.kind!=='system'&&m.content&&(m.role==='user'||m.role==='agent'))
      .slice(-24).forEach(m=>messages.push({role:m.role==='user'?'user':'assistant',content:String(m.content)}));
    if(prompt!==text||imgParts.length){
      let idx=-1;
      for(let i=messages.length-1;i>=0;i--){ if(messages[i].role==='user'){ idx=i; break } }
      const content=imgParts.length?[{type:'text',text:prompt},...imgParts]:prompt;
      if(idx>=0) messages[idx].content=content; else messages.push({role:'user',content});
    }
    const headers={'Content-Type':'application/json'};
    if(cfg.key) headers['Authorization']='Bearer '+cfg.key;
    ctl=(typeof AbortController!=='undefined')?new AbortController():null;
    const resp=await fetch(base+'/chat/completions',{method:'POST',headers,
      signal:ctl?ctl.signal:undefined,
      body:JSON.stringify(Object.assign(
        {messages,stream:true,
         temperature:Number(S().agent.temperature??.7),
         max_tokens:Number(S().agent.maxTokens||4096)},
        model?{model}:{}))});
    if(!resp.ok||!resp.body)throw new Error('модель ответила '+resp.status);
    const reader=resp.body.getReader(),dec=new TextDecoder(); let buf='';
    while(true){
      if(ui.abort){ stopped=true; try{reader.cancel()}catch(e){} break }
      const rd=await reader.read(); if(rd.done)break;
      buf+=dec.decode(rd.value,{stream:true});
      let nl;
      while((nl=buf.indexOf('\n'))>=0){
        const line=buf.slice(0,nl).trim(); buf=buf.slice(nl+1);
        if(!line||!line.startsWith('data:'))continue;
        const payload=line.slice(5).trim();
        if(!payload||payload==='[DONE]')continue;
        let j=null; try{ j=JSON.parse(payload) }catch(e){ continue }
        const d=(j.choices&&j.choices[0]&&j.choices[0].delta)||{};
        /* reasoning-модели (Qwen3 и др.) сначала шлют размышления — показываем их как «Думаю» */
        const think=(d&&d.reasoning_content)||'';
        if(think){
          if(!reason){
            reason=msg(s,{role:'agent',kind:'thinking',content:'',meta:{open:true},status:'running'});
            renderStream();
          }
          reason.content+=think; patch(reason);
        }
        const piece=(d&&d.content)||'';
        if(piece){
          if(reason&&reason.status==='running'){ reason.status='done' }
          if(!cur){ cur=msg(s,{role:'agent',kind:'text',content:'',status:'streaming',meta:{model:'диалог · напрямую'}}); renderStream() }
          cur.content+=piece; finalText+=piece; patch(cur);
          const st=$('#stream'); if(st)autoScrollBottom(st);
          /* Озвучка на лету — тот же путь, что и у агентного хода. */
          if(typeof LiveTTS!=='undefined')LiveTTS.feed(piece);
        }
      }
    }
  }catch(e){
    if(e&&e.name==='AbortError')stopped=true;
    else { err=errText(e.message||e); console.error('[direct]',e) }
  }finally{ try{ if(ctl)ctl.abort() }catch(e){} }   /* закрываем поток, если оборвали */
  if(cur&&cur.status==='streaming')cur.status='done';
  if(typeof LiveTTS!=='undefined')LiveTTS.end();
  ui.generating=false; queueMarkTurnEnd(); setSend(); renderAll();
  const stageWasTurn=stageTurn, stageAbort=stopped||!!ui.stageAborted;
  stageTurn=false; ui.stageAborted=false;
  if(stageWasTurn){
    stageReply(err?'':finalText,err,stageAbort);
  }else{
    if(err) msg(s,{role:'system',kind:'system',content:'⚠️ '+err});
    setStatus(err?'Ошибка':(stageAbort?'Остановлено':'Готово · '+((performance.now()-t0)/1000).toFixed(1)+' c'));
    const liveSpoke=typeof LiveTTS!=='undefined'&&LiveTTS.spokeThisTurn();
    if(finalText&&speakEnabled()&&!stageAbort&&!liveSpoke)speak(finalText);
  }
  flushQueueSoon(120);
}

// Переопределяем agentTurn для использования backend
const originalAgentTurn=agentTurn;

async function agentTurn(text,attachments=[]){
 const s=active(); if(!s)return;
 ui.turnSeq=(ui.turnSeq||0)+1; // маркер попытки старта хода (для автоотправки очереди)
 ui.toolSessionId=s.id; // панель «Вызовы инструментов» следит за сессией, в которой работает агент
 
 if(!S().connection.enabled){
 toast('Backend отключен. Включите в настройках.','err');
 ui._errorStartNoRetry=true; // явный отказ — очередь не ретраит автоматически
 return;
 }
 
 const sid=await Gateway.ensureBackend(s);
 if(!sid){ toast('Шлюз недоступен','err'); ui._errorStartNoRetry=true;
   ui.toolSessionId=null;              // ход не состоялся — панель не привязываем
   if(stageTurn){ stageTurn=false; stageReply('','Шлюз недоступен',false) } return }
 
  ui.generating=true; ui.abort=false; setSend(); renderHead(); setStatus('Думаю…');
  queueMarkTurnStart();
  /* Снимаем заглушку озвучки от «стоп» прошлого хода — см. directTurn. */
  if(typeof LiveTTS!=='undefined')LiveTTS.arm();
  let cur=null,think=null,finalText='',err=null;
 const t0=performance.now();
 
 // Сброс счетчиков для отслеживания скорости
 ui.lastTokenTime=Date.now();
 ui.tokenCount=0; ui.tokWin=[];
 updCost();
 
 try{
 // Рабочая директория проекта: абсолютный путь (у старых проектов — что сохранено)
 let workingDir=null;
 const proj=projectOf(s);
 if(proj&&(proj.absPath||proj.path)){
 workingDir=proj.absPath||proj.path;
 }
 if(!workingDir&&state.gatewayCwd)workingDir=state.gatewayCwd;

 // Невидимая передача рабочей папки агенту (пункт 4 error.txt): путь кладём в тело
 // сообщения бэкенд-сессии — в чате он не отображается, агент видит его как
 // присланный пользователем путь. Один раз на сессию (до подтверждения агентом).
 if(workingDir&&!s.wdSent){
 s.wdSent=true; save();
 text='[SYSTEM CONTEXT] Рабочая директория пользователя: '+workingDir+
 '. Все файловые операции выполняй в этой директории.\n\n'+text;
 }
 // Путь к папке плагинов интерфейса — чтобы агент мог сам создавать .js.
 // Тоже один раз на сессию и невидимо: пользователь в чате его не видит.
 if(S().plugins.dir&&!s.plugSent){
  s.plugSent=true; save();
  text='[PLUGINS] Папка плагинов интерфейса: '+S().plugins.dir+'.\n'+
  'Ты можешь создавать там .js-файлы — интерфейс подхватит их сам, без перезагрузки страницы.\n'+
  'ПОЛНОЕ РУКОВОДСТВО лежит в этой же папке в файле README.txt — прочитай его ПЕРЕД\n'+
  'созданием плагина: там правила имени файла, полный справочник API, готовые шаблоны\n'+
  'и частые ошибки.\n'+
  'Кратко: один файл — один плагин, только верхний уровень папки, имя латиницей через\n'+
  'дефис. Заголовок в начале файла: // @name, // @desc, // @version, // @author.\n'+
  'API: api.on(send|reply|ready|session|dispose, fn), api.registerCommand({name,desc,run}),\n'+
  'api.toast(msg), api.storeGet(k,d), api.storeSet(k,v), api.setInterval(fn,ms), api.log(),\n'+
  'api.session(), api.reload(), api.dir().\n'+
  'Плагин выполняется в браузере: доступен DOM, fetch, localStorage. Модулей и Node.js там нет.\n'+
  'Таймеры создавай только через api.setInterval — иначе они не снимутся при выключении.\n'+
  'Перед сдачей проверь синтаксис: node --check имя-файла.js\n'+
  'Если в настройках выбран режим ручной активации, плагин останется выключенным —\n'+
  'предупреди пользователя, что его нужно включить в «Настройки → Плагины».\n\n'+text;
 }
 
 // Подготавливаем message с attachments (backend ожидает multimodal content)
 let messageContent=text;
 if(attachments.length>0){
 // Backend ожидает массив частей: [{type:'text',text:'...'},{type:'image_url',image_url:{url:'data:...'}}]
 const parts=[{type:'text',text:text}];
 attachments.forEach(a=>{
 if(a.dataUrl&&a.type.startsWith('image/')){
 // Изображение: сам файл уходит как image_url, а путь на диске — отдельной
 // текстовой частью, чтобы агент мог открыть исходник, если попросят
 parts.push({type:'image_url',image_url:{url:a.dataUrl}});
 let ip='\n\n[Изображение: '+a.name+']';
 if(a.path)ip+='\n[Путь к файлу: '+a.path+']';
 parts.push({type:'text',text:ip});
 }else if(a.isDir&&a.path){
 // Папка, перетащенная из панели «Файлы проекта» — отдаём агенту путь
 parts.push({type:'text',text:'\n\n[Пользователь перетащил папку: '+a.path+']'});
 }else if(a.isDir){
 // Папка без определённого пути (автопоиск и диалог не сработали)
 parts.push({type:'text',text:'\n\n[Пользователь прикрепил папку: '+a.name+' (путь не определён — попроси пользователя указать расположение)]'});
 }else{
 // Файл: агенту нужен ПОЛНЫЙ путь на диске, чтобы он мог открыть файл.
 // Путь уходит только в backend-запрос — в чате он не отображается.
 let tp;
 if(a.path){
  tp='\n\n[Файл: '+a.name+']\n[Путь к файлу: '+a.path+']';
  if(a.content)tp+='\n[Содержимое файла:]\n'+a.content+(a.truncated?'\n[...файл усечён...]':'');
  else tp+='\n(содержимое не приложено — открой файл по указанному пути)';
 }else{
  // Путь так и не нашли. Честно говорим об этом: агент не должен
  // пытаться открыть несуществующий файл и не должен выдумывать содержимое.
  tp='\n\n[Файл: '+a.name+']\n[Путь НЕ ОПРЕДЕЛЁН — файл недоступен для чтения. '+
     'Не открывай его по имени и не выдумывай содержимое. Если работа с ним нужна — '+
     'попроси пользователя прикрепить файл заново, указав полный путь.]';
  if(a.content)tp+='\n[Содержимое файла:]\n'+a.content;
 }
 parts.push({type:'text',text:tp});
 }
 });
 messageContent=parts;
 }
 
 const resp=await fetch('/api/sessions/'+sid+'/chat/stream',{method:'POST',
 headers:{'Content-Type':'application/json','X-Hermes-Session-Key':'inf-'+s.id},
 body:JSON.stringify({
 message:messageContent
 })});
 
 if(!resp.ok||!resp.body){
 let errMsg='Шлюз вернул ошибку '+resp.status;
 try{const errData=await resp.json();errMsg=errText(errData,errMsg);}catch(_){}
 toast(errMsg,'err');
 msg(s,{role:'system',kind:'system',content:'⚠️ '+errMsg});
 ui.generating=false; queueMarkTurnEnd(); setSend(); renderHead();
 ui.toolSessionId=null; // ход не стартовал — панель не должна остаться на этой сессии
 if(typeof LiveTTS!=='undefined')LiveTTS.stop();   // очередь озвучки незакрыта — глушим
 if(stageTurn){ stageTurn=false; stageReply('',errMsg,false); return } // ход с главного экрана — ошибка в реплику
 flushQueueSoon(5000); // ошибка старта — очередь не должна застревать
 return
 }
 
 const reader=resp.body.getReader(); const dec=new TextDecoder(); let buf='';
 let liveReasoning=false;   /* пришёл живой поток рассуждений (reasoning_content) */
 const ensureText=()=>{ 
 if(!cur){ cur=msg(s,{role:'agent',kind:'text',content:'',status:'streaming',meta:{}}); renderStream() } 
 return cur 
 };
 const ensureThink=()=>{ 
 /* Новая итерация агента (после инструментов) — новый блок рассуждений,
    а не дописывание в уже закрытый. */
 if(!think||think.status!=='running'){ think=msg(s,{role:'agent',kind:'thinking',content:'',meta:{open:true},status:'running'}); renderStream() } 
 return think 
 };
 
 while(true){
 if(ui.abort){ try{reader.cancel()}catch(e){} break }
 const rd=await reader.read(); if(rd.done)break;
 buf+=dec.decode(rd.value,{stream:true});
 let idx; 
 while((idx=buf.indexOf('\n\n'))>=0){
 const frame=buf.slice(0,idx); buf=buf.slice(idx+2);
 let ev='message',data=null;
 for(const ln of frame.split('\n')){
 if(ln.startsWith('event:'))ev=ln.slice(6).trim();
 else if(ln.startsWith('data:')){ try{data=JSON.parse(ln.slice(5).trim())}catch(e){} } 
 }
 if(!data)continue;
 
 if(ev==='assistant.delta'&&data.delta){ 
 const m=ensureText();
 /* Пошёл текст ответа — блок рассуждений закрываем, чтобы ответ и мысли не смешивались */
 if(think&&think.status==='running'){ think.status='done'; patch(think); think=null }
 m.content+=data.delta; m.status='streaming'; 
 patch(m); // Используем patch() вместо renderStream() для устранения мерцания
 
 /* Прикидка токенов по длине куска: точный счётчик приходит от шлюза,
    но для live-индикатора этой оценки достаточно. Кладём пары [мс, токены]
    в скользящее окно, по которому updCost() считает скорость. */
 const dt=Math.ceil(data.delta.length/3.4);
 ui.tokenCount=(ui.tokenCount||0)+dt;
 (ui.tokWin||(ui.tokWin=[])).push([Date.now(),dt]);
 updCost();

 /* Озвучка на лету: отдаём дельту в LiveTTS ПОСЛЕ отрисовки текста, чтобы
    голос никогда не забегал вперёд того, что человек видит на экране.
    Блоки рассуждений (assistant.reasoning, tool.progress _thinking) сюда
    намеренно не подключены — озвучивается только ответ. */
 if(typeof LiveTTS!=='undefined')LiveTTS.feed(data.delta);

 // Прокрутка вниз при стриминге
 const st=$('#stream'); if(st)autoScrollBottom(st);
 }
 else if(ev==='assistant.reasoning'&&data.delta){
 /* Живой поток рассуждений модели (reasoning_content) — в блок «Рассуждение».
    В озвучку не идёт: пользователь этого не просил, и рассуждения звучали бы
    как ответ. */
 liveReasoning=true;
 const m=ensureThink(); m.content+=data.delta;
 patch(m);
 const st=$('#stream'); if(st)autoScrollBottom(st);
 }
 else if(ev==='assistant.commentary'&&data.text&&!data.already_streamed){
 /* Промежуточный текст агента по ходу инструментов (если не был отстримлен) */
 const m=ensureText(); m.content+=(m.content?'\n\n':'')+data.text; m.status='streaming';
 patch(m);
 /* Граница сегмента: не даём склеить предложения через вставку. */
 if(typeof LiveTTS!=='undefined')LiveTTS.segment();
 }
 else if(ev==='tool.progress'&&(data.tool_name==='_thinking')&&data.delta&&!liveReasoning){
 const m=ensureThink(); m.content+=data.delta;
 patch(m); // Используем patch() вместо renderStream()
 const st=$('#stream'); if(st)autoScrollBottom(st);
 }
 else if(ev==='tool.started'){
 if(think){think.status='done'}
 cur=null;
 /* Ответ разбит вызовами инструментов на отдельные куски сообщений
    (cur обнулился выше) — для озвучки это граница фразы. */
 if(typeof LiveTTS!=='undefined')LiveTTS.segment();
 let toolIcon='fa-solid fa-screwdriver-wrench';
 const toolName=(data.tool_name||'').toLowerCase();
 if(toolName.includes('file')||toolName.includes('read')||toolName.includes('write'))toolIcon='fa-solid fa-file-lines';
 else if(toolName.includes('terminal')||toolName.includes('command')||toolName.includes('shell'))toolIcon='fa-solid fa-terminal';
 else if(toolName.includes('web')||toolName.includes('search')||toolName.includes('browser'))toolIcon='fa-solid fa-globe';
 else if(toolName.includes('code')||toolName.includes('python')||toolName.includes('exec'))toolIcon='fa-solid fa-code';
 else if(toolName.includes('image')||toolName.includes('vision'))toolIcon='fa-regular fa-image';
 else if(toolName.includes('task')||toolName.includes('todo')||toolName.includes('plan'))toolIcon='fa-solid fa-list-check';
 else if(toolName.includes('memory')||toolName.includes('recall'))toolIcon='fa-solid fa-brain';
 else if(toolName.includes('delegate')||toolName.includes('agent'))toolIcon='fa-solid fa-users';
 
 // Сохраняем input если есть
 const toolMeta={
 name:data.tool_name||'tool',
 icon:toolIcon,
 preview:data.preview||'',
 open:true
 };
 if(data.args)toolMeta.input=typeof data.args==='string'?data.args:JSON.stringify(data.args,null,2);
 
 msg(s,{role:'agent',kind:'tool',content:'',status:'running',meta:toolMeta}); 
 renderStream(); 
 // Перерисовываем панель вызовов (с задержкой, чтобы не собирать список на каждый чанк)
 renderToolsPanelSoon();
 }
 else if(ev==='tool.completed'||ev==='tool.failed'){
 const tm=[...s.messages].reverse().find(x=>x.kind==='tool'&&x.status==='running');
 if(tm){ 
 tm.status=ev==='tool.completed'?'done':'error';
 
 // Обновляем preview
 if(data.preview)tm.meta.preview=data.preview;
 
 // Сохраняем input если пришел
 if(data.args){
 tm.meta.input=typeof data.args==='string'?data.args:JSON.stringify(data.args,null,2);
 }
 // Аргументы целиком: из них вытаскиваются пути файлов для чипов и diff
 if(data.args)tm.meta.args=data.args;
 
 // Сохраняем output/result
 if(data.result){
 tm.meta.output=typeof data.result==='string'?data.result:JSON.stringify(data.result,null,2);
 }else if(data.output){
 tm.meta.output=typeof data.output==='string'?data.output:JSON.stringify(data.output,null,2);
 }
 
 // Сохраняем время выполнения
 if(data.duration){
 tm.meta.dur=(data.duration/1000).toFixed(2)+'s';
 }
 
 // Используем patch() для обновления без мерцания
 patch(tm);
// Запоминаем последний изменённый файл — для кнопки «Сравнить с Git».
// Путь принадлежит сессии, в которой шёл ход: иначе переключившись на другой
// чат, кнопка сравнивала бы файл из чужого прогона.
if(ev==='tool.completed'&&tm.meta){
const fp=pathsFromArgs(tm.meta.args||tm.meta.input)[0];
if(fp){ ui.diffPath=fp; ui.diffPathSession=s.id }
}
 // Перерисовываем панель вызовов (с задержкой, чтобы не собирать список на каждый чанк)
 renderToolsPanelSoon();
 }else{
 // Если не нашли running tool, делаем полный renderStream
 renderStream();
 // Перерисовываем панель вызовов (с задержкой, чтобы не собирать список на каждый чанк)
 renderToolsPanelSoon();
 }
 }
 else if(ev==='assistant.completed'){
 /* Финальный текст всего хода приходит здесь (поле content); run.completed его не содержит */
 if(!finalText)finalText=data.content||'';
 }
 else if(ev==='run.completed'){
 finalText=data.final_response||data.content||finalText;
 if(finalText&&!cur){ cur=msg(s,{role:'agent',kind:'text',content:finalText,status:'done',meta:{}}) }
 /* Ход закончился — дописываем хвост без знака препинания и запускаем
    доигрывание очереди. Дальше голос говорит сам, без участия потока. */
 if(typeof LiveTTS!=='undefined')LiveTTS.end();
 }
else if(ev==='subagent_progress'||(ev&&ev.indexOf('subagent.')===0)){
  /* Работа субагента. Движок шлёт эти кадры на тот же SSE, что и остальные
     события хода; дерево собирает 13-subagents.js и рисует внутри карточки
     delegate_task. События не меняют статус карточки — она и так закрывается
     по tool.completed самой делегации. */
  if(onSubagentEvent(s,ev,data)){
    renderToolsPanelSoon();
  }
  }
  else if(ev==='delegation.started'){
  msg(s,{role:'agent',kind:'tool',content:'',status:'running',
  meta:{name:'Делегирование: '+(data.subagent||'субагент'),icon:'fa-solid fa-users',
  preview:data.task||'Выполнение подзадачи...',open:true}}); renderStream(); 
  // Перерисовываем панель вызовов (с задержкой, чтобы не собирать список на каждый чанк)
  renderToolsPanelSoon();
  }
 else if(ev==='delegation.completed'){
 const tm=[...s.messages].reverse().find(x=>x.kind==='tool'&&x.status==='running'&&x.meta?.icon==='fa-solid fa-users');
 if(tm){tm.status='done';tm.meta.output=data.result||'Подзадача выполнена';}
 renderStream(); 
 // Перерисовываем панель вызовов (с задержкой, чтобы не собирать список на каждый чанк)
 renderToolsPanelSoon();
 }
   /* \u0417\u0430\u043f\u0440\u043e\u0441 \u043e\u043f\u0430\u0441\u043d\u043e\u0433\u043e \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044f \u043e\u0442 \u0448\u043b\u044e\u0437\u0430: \u0440\u0438\u0441\u0443\u0435\u043c \u0431\u043b\u043e\u043a \u0441 \u0432\u043e\u0437\u043c\u043e\u0436\u043d\u043e\u0441\u0442\u044f\u043c\u0438 \u043a\u043d\u043e\u043f\u043a\u0430\u043c\u0438. */
  /* 0.23 шлёт approval.request с request_id и execution_generation: без них ответ на
     /v1/runs/{id}/approval отклоняется как invalid_params. Старые имена оставлены для
     потоков, начатых до обновления движка. */
  else if(ev==='approval.request'||ev==='approval'||ev==='tool.approval'||ev==='approval_required'){
   const am=msg(s,{role:'agent',kind:'approval',status:'running',
     content:String(data.description||data.command||data.text||data.message||data.reason||'Агент запрашивает разрешение.'),
     meta:{cmd:data.command||data.cmd||data.tool||'',tool:data.tool||'',runId:data.run_id||data.runId||ui.runId||'',
           requestId:data.request_id||data.prompt_id||'',gen:data.execution_generation,
           choices:Array.isArray(data.choices)?data.choices:[]}});
   renderStream();
   toast('Агент ждёт разрешения','warn');
   if(typeof notifyAttention==='function')notifyAttention('Агент ждёт разрешения');
  }else if(ev==='approval.settled'){
   const rid=data.request_id||data.prompt_id;
   const am=[...s.messages].reverse().find(x=>x.kind==='approval'&&x.status==='running'&&(!rid||x.meta?.requestId===rid));
   if(am){ am.status='done'; am.meta=am.meta||{}; if(!am.meta.decision)am.meta.decision='решено'; patch(am) }
  }else if(ev==='clarify.request'){
   msg(s,{role:'agent',kind:'clarify',status:'running',
     content:String(data.question||data.text||'Агент задаёт вопрос.'),
     meta:{requestId:data.request_id||data.prompt_id||'',gen:data.execution_generation,
           runId:data.run_id||ui.runId||'',choices:Array.isArray(data.choices)?data.choices:[],
           multi:!!data.multi_select,timeoutS:data.timeout_s}});
   renderStream();
   toast('Агент ждёт ответа на вопрос','warn');
   if(typeof notifyAttention==='function')notifyAttention('Агент задаёт вопрос');
  }else if(ev==='clarify.settled'){
   const rid=data.request_id||data.prompt_id;
   const cm=[...s.messages].reverse().find(x=>x.kind==='clarify'&&x.status==='running'&&(!rid||x.meta?.requestId===rid));
   if(cm){ cm.status='done'; cm.meta=cm.meta||{}; if(!cm.meta.answer)cm.meta.answer='закрыт'; patch(cm) }
  }else if(ev==='error'){ 
 err=errText(data.message||data.error||data,'Ошибка шлюза'); 
 console.error('Gateway error:',data);
 toast('Ошибка: '+err,'err');
msg(s,{role:'system',kind:'system',content:'⚠️ '+err});
  renderStream();
  // После ошибки run.completed уже не придёт — очередь озвучки закрываем сами,
  // иначе голос зависнет на середине фразы.
  if(typeof LiveTTS!=='undefined')LiveTTS.end();
 }
 }
 }
 }catch(e){ 
 console.error('Connection error:',e);
 toast('Соединение со шлюзом потеряно: '+errText(e.message||e),'err');
msg(s,{role:'system',kind:'system',content:'⚠️ Потеряно соединение с шлюзом: '+errText(e.message||e)});
  if(typeof LiveTTS!=='undefined')LiveTTS.end();
  }
 
 if(think){ think.status='done'; renderThinkPinSoon() }   /* рассуждения не сворачиваем — их можно прочитать (клик по заголовку скрывает) */
 if(cur&&cur.status==='streaming')cur.status='done';
 closeStaleTools(s); // незакрытые вызовы не должны оставаться «выполняется»
 closeStaleControls(s); // неотвеченные подтверждения/вопросы не должны висеть «ожидает»
 ui.toolSessionId=null; // агент закончил — панель снова показывает активную сессию
 ui.generating=false; queueMarkTurnEnd(); setSend(); renderAll();
 const stageWasTurn=stageTurn, stageAbort=!!ui.stageAborted;
 if(!stageAbort){
   if(err)notifyAttention('Ошибка агента',err);
   else if(finalText)notifyAttention('Агент закончил',finalText);
 }
 stageTurn=false; ui.stageAborted=false;
 if(stageWasTurn){                 /* ход с главного экрана — ответ репликой, а не статусом */
   stageReply(finalText,stageAbort?null:err,stageAbort);
 } else {
   setStatus(err?'Ошибка':'Готово · '+((performance.now()-t0)/1000).toFixed(1)+' c');
   /* Хук 'reply' для хода в чате (ход с главного экрана отработал выше).
     Плагин может вернуть строку — заменим ею текст последнего сообщения. */
  if(finalText&&!err&&cur){
    try{ const r=PluginBus.emit('reply',{text:String(finalText),session:s});
      if(typeof r==='string'&&r.trim()&&r!==finalText){ finalText=r; cur.content=r; renderAll() } }
    catch(e){ console.warn('Плагины (reply):',e) }
  }
  /* Голос на лету уже произнёс ответ по ходу генерации — повторный speak()
     озвучил бы его второй раз. spokeThisTurn() истинно только если текст
     реально дошёл до синтеза, поэтому при выключенной озвучке на лету
     и при пустом ответе обычный путь сохраняется. */
  const liveSpoke=typeof LiveTTS!=='undefined'&&LiveTTS.spokeThisTurn();
  if(finalText&&speakEnabled()&&!liveSpoke)speak(finalText);
 }
 // === ОЧЕРЕДЬ / ПЕРЕБИВАНИЕ: отправка накопленных сообщений после хода ===
 flushQueueSoon(120);
}

/* === ОЧЕРЕДЬ: ОЦЕНКА ВРЕМЕНИ ОЖИДАНИЯ ================================
   Средняя длительность хода копится в localStorage (сглаживание 70/30).
   ETA для сообщения на позиции N = остаток текущего хода + (N-1) средних
   хода. Пока текущий ход идёт, остаток уменьшается на прошедшее время. */
const QUEUE_AVG_KEY='infinity_agent_turn_avg_ms';
function queueAvgMs(){
  const v=Number(localStorage.getItem(QUEUE_AVG_KEY)||0);
  return (isFinite(v)&&v>=3000)?v:45000;
}
function queueMarkTurnStart(){ ui.turnStartedAt=performance.now(); }
function queueMarkTurnEnd(){
  if(!ui.turnStartedAt)return;
  const ms=performance.now()-ui.turnStartedAt;
  ui.turnStartedAt=0;
  if(ms<1000||ms>30*60*1000)return; // битые значения не портят среднее
  const next=Math.round(queueAvgMs()*0.7+ms*0.3);
  try{localStorage.setItem(QUEUE_AVG_KEY,String(next))}catch(e){}
}
function fmtEta(ms){
  const s=Math.max(1,Math.round(ms/1000));
  if(s<60)return '~'+s+' с';
  const m=Math.round(s/60);
  if(m<60)return '~'+m+' мин';
  return '~'+(m/60).toFixed(1)+' ч';
}
function queueEtaMs(pos){
  const avg=queueAvgMs();
  const elapsed=ui.turnStartedAt?performance.now()-ui.turnStartedAt:0;
  const cur=ui.generating?Math.max(4000,avg-elapsed):0;
  return cur+Math.max(0,(pos||1)-1)*avg;
}
function renderQueueChip(){
  const chip=$('#queueChip'); if(!chip)return;
  const n=(ui.msgQueue||[]).length;
  if(!n){chip.style.display='none';renderQueueStrip();return}
  const wait=queueEtaMs(n);
  chip.style.display='';
  chip.title='Сообщений в очереди: '+n+' · последнее ≈ через '+fmtEta(wait);
  const lbl=$('#queueEta'); if(lbl)lbl.textContent='Очередь: '+n+' · '+fmtEta(wait);
  renderQueueStrip();
}

/* === ПАНЕЛЬ ОЧЕРЕДИ =====================================================
   Раньше очередь существовала только как счётчик в чипе: сообщение исчезало
   из поля ввода, и человек не мог ни увидеть его текст, ни убрать, ни
   переставить. Полоса над вводом — тот же ui.msgQueue, ничего нового не
   хранится: порядок в массиве и есть порядок отправки (flushQueueSoon берёт
   элемент [0]). */
let qsSig='';
function queueSig(){ return (ui.msgQueue||[]).map(x=>x.text+'#'+(x.sessionId||'')).join('\n') }
function renderQueueStrip(){
  const strip=$('#queueStrip'); if(!strip)return;
  const n=(ui.msgQueue||[]).length;
  // Скрыто/показано классом .on: у полосы в CSS базовое display:none,
  // поэтому inline style.display='' вернул бы «нет» вместо показа.
  strip.classList.toggle('on',n>0);
  if(!n){ qsSig=''; const l0=$('#qsList'); if(l0)l0.innerHTML='';
          const t0=$('#qsTitle'); if(t0)t0.textContent='Очередь: 0'; return }
  const t=$('#qsTitle'); if(t)t.textContent='Очередь: '+n+' · последний ≈ через '+fmtEta(queueEtaMs(n));
  const list=$('#qsList'); if(!list)return;
  if(list.querySelector('.qs-editing'))return;   // человек печатает правку — не трогаем разметку
  if(qsSig!==queueSig()){ qsSig=queueSig(); buildQueueRows(list) }
  else updateQueueEtas(list);
}
function buildQueueRows(list){
  list.innerHTML='';
  (ui.msgQueue||[]).forEach((item,i)=>list.appendChild(queueRow(item,i)));
}
function updateQueueEtas(list){
  const rows=list.querySelectorAll('.qs-row');
  rows.forEach((r,i)=>{ const e=r.querySelector('.qs-eta'); if(e)e.textContent=fmtEta(queueEtaMs(i+1)) });
}
function queueRow(item,i){
  const row=document.createElement('div');
  row.className='qs-row'; row.draggable=true; row.dataset.i=String(i);
  const other=item.sessionId&&item.sessionId!==state.activeSessionId;
  row.innerHTML=
    '<span class="qs-grip" title="Перетащить, чтобы изменить порядок"><i class="fa-solid fa-grip-vertical"></i></span>'+
    '<span class="qs-n">'+(i+1)+'</span>'+
    '<div class="qs-txt" title="Клик — развернуть текст">'+esc(item.text)+'</div>'+
    (other?'<span class="qs-sess" title="Уйдёт не в эту переписку"><i class="fa-solid fa-comment-dots"></i>другая сессия</span>':'')+
    '<span class="qs-eta">'+fmtEta(queueEtaMs(i+1))+'</span>'+
    '<button type="button" class="qs-btn" data-act="now" title="Перебить текущий ход и отправить это первым">'+
      '<i class="fa-solid fa-circle-play"></i> сейчас</button>'+
    '<button type="button" class="qs-btn" data-act="edit" title="Редактировать"><i class="fa-solid fa-pen"></i></button>'+
    '<button type="button" class="qs-btn x" data-act="del" title="Убрать из очереди"><i class="fa-solid fa-xmark"></i></button>';
  row.querySelector('.qs-txt').addEventListener('click',()=>row.classList.toggle('exp'));
  row.addEventListener('click',ev=>{
    const b=ev.target.closest('button[data-act]'); if(!b)return;
    const i=Number(row.dataset.i);
    if(b.dataset.act==='now')queueSendNow(i);
    else if(b.dataset.act==='del')queueDelete(i);
    else if(b.dataset.act==='edit')queueEdit(row,i);
  });
  row.addEventListener('dragstart',ev=>{
    qsDrag=Number(row.dataset.i);
    try{ev.dataTransfer.setData('text/plain',String(qsDrag));ev.dataTransfer.effectAllowed='move'}catch(_){}
    row.classList.add('drag');
  });
  row.addEventListener('dragend',()=>{ qsDrag=-1; clearQueueMarks(row.parentElement) });
  row.addEventListener('dragover',ev=>{
    if(qsDrag<0||qsDrag===Number(row.dataset.i))return;
    ev.preventDefault(); try{ev.dataTransfer.dropEffect='move'}catch(_){}
    clearQueueMarks(row.parentElement);
    row.classList.add('target');
  });
  row.addEventListener('drop',ev=>{
    if(qsDrag<0)return;
    ev.preventDefault();
    queueMove(qsDrag,Number(row.dataset.i));
  });
  return row;
}
let qsDrag=-1;
function clearQueueMarks(list){
  if(!list||!list.querySelectorAll)return;
  list.querySelectorAll('.target').forEach(r=>r.classList.remove('target'));
}
function queueMove(from,to){
  const a=ui.msgQueue||[];
  if(from<0||from>=a.length||to<0||to>=a.length||from===to)return;
  const it=a.splice(from,1)[0]; a.splice(to,0,it);
  save(); qsSig=''; renderQueueChip();
}
function queueDelete(i){
  const a=ui.msgQueue||[]; if(i<0||i>=a.length)return;
  a.splice(i,1);
  save(); qsSig=''; renderQueueChip();
  if(!a.length)toast('Очередь пуста','info');
}
function queueSendNow(i){
  const a=ui.msgQueue||[]; const it=a[i]; if(!it)return;
  a.splice(i,1); a.unshift(it);
  if(ui.generating){
    /* «Сейчас» = то же перебивание, что режим занятого агента «перебить»:
       ход оборачивается, а по его концу flushQueueSoon отправит голову
       очереди в ЕЁ сессию (не в активную — иначе текст ушёл бы в чужой
       диалог). pendingInterrupt здесь не используем: он отправляет через
       поле ввода и всегда в активную сессию. */
    ui.abort=true;
    if(typeof ttsStop==='function')ttsStop();
    setStatus('Перебиваю…');
    toast('Ставлю первым — уйдёт сразу после остановки хода','info');
  } else {
    flushQueueSoon(0);
  }
  save(); qsSig=''; renderQueueChip();
}
function queueEdit(row,i){
  const item=(ui.msgQueue||[])[i]; if(!item)return;
  const txt=row.querySelector('.qs-txt'); if(!txt)return;
  row.classList.add('qs-editing'); row.classList.remove('exp'); row.draggable=false;
  const ta=document.createElement('textarea');
  ta.className='qs-ta'; ta.rows=3; ta.value=item.text;
  txt.replaceWith(ta);
  ta.focus(); ta.setSelectionRange(ta.value.length,ta.value.length);
  const done=(commit)=>{
    if(row.dataset.qsDone)return; row.dataset.qsDone='1';
    if(commit){
      const v=ta.value.trim();
      if(!v){ queueDelete(i); return }
      item.text=v; save();
    }
    row.classList.remove('qs-editing'); row.draggable=true;
    qsSig=''; renderQueueChip();
  };
  ta.addEventListener('keydown',ev=>{
    if(ev.key==='Enter'&&!ev.shiftKey){ ev.preventDefault(); done(true) }
    else if(ev.key==='Escape'){ ev.preventDefault(); done(false) }
  });
  ta.addEventListener('blur',()=>done(true));
}
const qsClearBtn=$('#qsClear');
if(qsClearBtn)qsClearBtn.onclick=()=>{
  const n=(ui.msgQueue||[]).length; if(!n)return;
  ui.msgQueue=[]; save(); qsSig=''; renderQueueChip();
  toast('Очередь очищена: '+n,'info');
};
if(typeof window!=='undefined'){
  if(window.__queueEtaTimer)clearInterval(window.__queueEtaTimer);
  window.__queueEtaTimer=setInterval(renderQueueChip,1000);
}

/* Автоотправка очереди: следующее сообщение уходит само, как только агент освободился.
   delayMs — пауза перед попыткой (после хода — 120мс, после ошибки — 5с retry).
   Если агент в этот момент занят — ничего не делаем: следующий flush придёт
   по завершении его хода. Ошибочный ход НЕ выкидывает сообщение из очереди
   безвозвратно: при провале соединения оно возвращается в начало очереди. */
let flushTimer=null;
function flushQueueSoon(delayMs=120){
  clearTimeout(flushTimer);
  flushTimer=setTimeout(()=>{
    flushTimer=null;
    if(ui.generating)return; // агент ещё работает — ход сам вызовет flush по завершении
    // Перебивание: сообщение, ввод которого остановил прошлый ход — приоритетнее очереди
    if(ui.pendingInterrupt){
      const t=ui.pendingInterrupt.text; ui.pendingInterrupt=null;
      if(t&&t.trim()){ if(input){input.value=t} submit(); }
      return;
    }
    if(!ui.msgQueue.length)return;
    const item=ui.msgQueue[0]; // смотрим, но не выкидываем — выкинем при успехе
    /* Сессию могли удалить, пока сообщение ждало. Подставлять активную нельзя:
       текст ушёл бы в чужой диалог. Пропускаем элемент и сообщаем об этом. */
    const ts=state.sessions.find(x=>x.id===item.sessionId);
    if(!ts){
      ui.msgQueue.shift();
      toast('Сессия из очереди удалена — сообщение пропущено','info');
      save(); renderQueueChip();
      flushQueueSoon(0); return
    }
    /* agentTurn стартует в active(), поэтому сначала переключаемся на сессию
       очереди — иначе сообщение видно в одной сессии, а ход идёт в другой.
       Поле ввода не трогаем: там может быть недописанный черновик. */
    if(ts.id!==state.activeSessionId)state.activeSessionId=ts.id;
    msg(ts,{role:'user',kind:'text',content:item.text});
    save(); renderAll();
    // Проверяем результат старта хода: при ошибке шлюза сообщение вернётся в очередь
    const seqBefore=ui.turnSeq||0;
    item._tries=(item._tries||0)+1;
    ui._errorStartNoRetry=null; // сброс: отказ должен быть подтверждён текущей попыткой
    ui._queueInFlight=item;
    ui.msgQueue.shift();
    renderQueueChip();
    agentTurn(item.text);
    // agentTurn синхронно до первого await; если ход не стартовал (generating=false
    // и turnSeq не вырос — например, шлюз недоступен), возвращаем сообщение в начало
    // очереди и пробуем позже. При _errorStartNoRetry ход явно отказал — ждём ручного retry.
    // После 3 неудачных попыток подряд авто-retry останавливается (сообщение остаётся в очереди).
    setTimeout(()=>{
      if(ui._queueInFlight!==item)return; // перезаписан более новой попыткой
      const started=ui.generating||((ui.turnSeq||0)>seqBefore);
      ui._queueInFlight=null;
      if(started){ item._tries=0; return } // ход пошёл — счётчик попыток сброшен
      if(!started&&!ui._errorStartNoRetry&&item._tries<3){
        ui.msgQueue.unshift(item);
        save(); renderQueueChip();
        setStatus('Очередь: агент недоступен — retry '+item._tries+'/3 через 5с');
        flushQueueSoon(5000);
      } else if(!started){
        ui._errorStartNoRetry=null; // отказ подтверждён (или лимит попыток) — ждём ручного retry
        /* Ход не стартовал: сообщение уже показано в чате, но из очереди оно
           выпало. Возвращаем его, иначе оно тихо исчезает из списка ожидания. */
        ui.msgQueue.unshift(item);
        save(); renderQueueChip();
        setStatus(item._tries>=3?'Очередь: 3 неудачные попытки — ждёт':'Очередь: ждёт (agent offline)');
      }
    },80);
  },delayMs);
}

// Инициализация backend при загрузке
if(S().connection.enabled){
 Gateway.connect().then(()=>{
 updateConnPill();
 if(Gateway.connected){
 toast('Backend подключён','ok');
 }
 });
 
 // Автообновление статуса каждые30 секунд
 setInterval(()=>{
 if(S().connection.enabled){
 Gateway.ping().then(updateConnPill);
 }
 },30000);
}
