/* ============ ФАЙЛЫ ИНСТРУКЦИЙ АГЕНТА (Настройки → Агент) ============
   Правка soul.md, user.md и подобных прямо на диске через /api/agent/*.
   На сервере стоит белый список имён, поэтому здесь мы передаём только
   имя файла — никаких путей. Содержимое не хранится в состоянии: при
   открытии вкладки берём свежее с диска, иначе показали бы устаревший
   текст после правки из другого окна. */

const AGENTF={list:null, loaded:false, loading:false, open:{}, drafts:{}};

/* Активная вкладка настроек: блок имеет смысл рисовать только когда
   открыта именно «Агент» — иначе запрос уходит при каждой перерисовке. */
function agentFilesVisible(){
  const p=document.querySelector('#agentFilesBox');
  if(!p)return false;
  const panel=p.closest('.panel');
  return !!panel && panel.classList.contains('on');
}

async function loadAgentFiles(){
  const box=$('#agentFilesBox'); if(!box)return;
  if(AGENTF.loading)return;
  AGENTF.loading=true;
  try{
    const r=await API.agentFiles();
    AGENTF.list=(r&&r.files)||[];
    AGENTF.loaded=true;
    renderAgentFiles();
  }catch(e){
    box.innerHTML='<div class="empty" style="padding:18px"><i class="fa-solid fa-triangle-exclamation"></i>'+
      '<p>Не удалось загрузить файлы инструкций</p><small>'+esc(errText(e&&e.message||e))+'</small></div>';
  }finally{ AGENTF.loading=false }
}

function agentFileBody(f){
  const open=AGENTF.open[f.name]!==false;
  const loaded=f._loaded===true;
  /* Черновик приоритетнее: пользователь правит файл, а перерисовка панели
     (например, переключение тумблера) не должна затирать его серверным. */
  const val=AGENTF.drafts[f.name]!=null?AGENTF.drafts[f.name]:(loaded?(f.content||''):'');
  return '<div class="cmdrow" style="display:block;padding:0">'+
    '<div style="display:flex;align-items:center;gap:9px;padding:8px 0">'+
      '<span class="cmd-name" style="flex:1">'+esc(f.name)+'</span>'+
      (f.exists?'':'<span class="badge wait">нет файла</span>')+
      '<span style="font-size:10.5px;color:var(--text-3)">'+esc(f.desc||'')+'</span>'+
      '<button class="icon-btn sm" data-af-open="'+esc(f.name)+'" title="'+(open?'Свернуть':'Править')+'">'+
        '<i class="fa-solid fa-pen"></i></button>'+
    '</div>'+
    (open?'<div style="padding:4px 0 12px">'+
      (loaded?'':'<div style="font-size:11.5px;color:var(--text-3);padding:4px 0">Загрузка…</div>')+
      '<textarea class="field" data-af-body="'+esc(f.name)+'" rows="12" spellcheck="false" '+
        'style="width:100%;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;line-height:1.55" '+
        'placeholder="текст файла">'+esc(val)+'</textarea>'+
      '<div style="display:flex;gap:8px;align-items:center;margin-top:9px">'+
        '<button class="btn sm primary" data-af-save="'+esc(f.name)+'"><i class="fa-solid fa-floppy-disk"></i> Сохранить</button>'+
        '<button class="btn sm ghost" data-af-reload="'+esc(f.name)+'"><i class="fa-solid fa-rotate"></i> Перечитать</button>'+
        '<span style="font-size:10.5px;color:var(--text-3);margin-left:auto">'+
          (f.exists?'<code>'+esc(f.path||'')+'</code>':'файл будет создан')+'</span>'+
      '</div>'+
    '</div>':'')+
  '</div>';
}

function renderAgentFiles(){
  const box=$('#agentFilesBox'); if(!box)return;
  if(!AGENTF.loaded){
    box.innerHTML='<div class="empty" style="padding:18px"><i class="fa-solid fa-spinner fa-spin"></i><p>Загрузка…</p></div>';
    return;
  }
  if(!AGENTF.list.length){
    box.innerHTML='<div class="empty" style="padding:18px"><i class="fa-solid fa-file-lines"></i>'+
      '<p>Сервер не вернул список файлов инструкций</p></div>';
    return;
  }
  /* Первый показ сразу раскрыт: пользователь ждёт увидеть soul.md и user.md,
     а раньше они были свёрнуты за кнопкой и выглядели как отсутствующие. */
  if(!AGENTF.shownOnce){
    AGENTF.shownOnce=true;
    AGENTF.list.forEach(f=>{ if(AGENTF.open[f.name]===undefined)AGENTF.open[f.name]=true });
  }
  box.innerHTML=AGENTF.list.map(agentFileBody).join('');
  /* Дотягиваем содержимое открытых файлов, если оно ещё не загружено. */
  if(!AGENTF.loading){
    AGENTF.list.forEach(f=>{
      if(AGENTF.open[f.name]!==false&&f._loaded!==true)openAgentFile(f.name);
    });
  }
}

async function openAgentFile(name){
  const f=AGENTF.list&&AGENTF.list.find(x=>x.name===name); if(!f)return;
  try{
    const r=await API.agentFileRead(name);
    f.content=(r&&r.content)||''; f.exists=!!(r&&r.exists); f._loaded=true;
    delete AGENTF.drafts[name];          /* свежий текст вытесняет черновик */
    renderAgentFiles();
  }catch(e){ toast('Не удалось прочитать '+name+': '+errText(e&&e.message||e),'err') }
}

async function saveAgentFile(name){
  const ta=document.querySelector('[data-af-body="'+name+'"]');
  if(!ta)return;
  const btn=document.querySelector('[data-af-save="'+name+'"]');
  if(btn)btn.disabled=true;
  try{
    await API.agentFileWrite(name,ta.value);
    const f=AGENTF.list.find(x=>x.name===name);
    if(f){ f.content=ta.value; f.exists=true; f._loaded=true; f.size=ta.value.length }
    delete AGENTF.drafts[name];
    toast('Файл '+name+' сохранён','ok');
    renderAgentFiles();
  }catch(e){ toast('Не удалось сохранить: '+errText(e&&e.message||e),'err') }
  finally{ const b=document.querySelector('[data-af-save="'+name+'"]'); if(b)b.disabled=false }
}

/* Черновик держим в памяти, чтобы перерисовка панели не стирала правку
   и не гоняла запрос на сервер. */
function bindAgentFiles(){
  const box=$('#agentFilesBox'); if(!box)return;
  box.addEventListener('click',e=>{
    const op=e.target.closest('[data-af-open]');
    if(op){ const n=op.dataset.afOpen;
      AGENTF.open[n]=AGENTF.open[n]===false;
      if(AGENTF.open[n])openAgentFile(n); else renderAgentFiles();
      return }
    const sv=e.target.closest('[data-af-save]');
    if(sv){ saveAgentFile(sv.dataset.afSave); return }
    const rl=e.target.closest('[data-af-reload]');
    if(rl){ openAgentFile(rl.dataset.afReload); return }
  });
  box.addEventListener('input',e=>{
    const ta=e.target.closest('[data-af-body]');
    if(ta)AGENTF.drafts[ta.dataset.afBody]=ta.value;
  });
  /* Открытие вкладки «Агент» подгружает список и содержимое один раз. */
  const nav=document.querySelector('.snav [data-p="agent"]');
  if(nav&&!nav._afBound){ nav._afBound=true;
    nav.addEventListener('click',()=>setTimeout(()=>{ if(!AGENTF.loaded)loadAgentFiles() },60)) }
}
bindAgentFiles();

/* ---- Системные инструкции (поле в «Инструкции и память») ----
   Раньше текст жил только в localStorage, поэтому после перезапуска
   сервера поле выглядело пустым. Теперь источник правды — файл на
   сервере, а настройки браузера лишь кэш на время правки. */
const PROMPT={loaded:false,saving:0};
function promptBox(){ return document.querySelector('[data-set="agent.systemPrompt"]') }

async function loadAgentPrompt(){
  const ta=promptBox(); if(!ta)return;
  if(PROMPT.loaded)return;
  try{
    const r=await API.agentPrompt();
    PROMPT.loaded=true;
    if(r&&typeof r.content==='string'){
      /* Не затираем то, что пользователь уже печатает. */
      if(!ta.value.trim()&&r.content)ta.value=r.content;
      S().agent.systemPrompt=ta.value;      /* синхронизируем кэш настроек */
      save();
    }
    const hint=document.querySelector('#agentPromptPath');
    if(hint)hint.textContent=(r&&r.path)||'';
  }catch(e){ /* сервер не поднят — останется локальный текст */ }
}
async function saveAgentPrompt(){
  const ta=promptBox(); if(!ta)return;
  try{
    await API.agentPromptSave(ta.value);
    PROMPT.saving=0;
    toast('Инструкции сохранены','ok');
  }catch(e){
    if(PROMPT.saving++<2) toast('Не сохранено на сервере: '+errText(e&&e.message||e),'err');
  }
}
/* Пишем на сервер не на каждый символ, а после паузы в наборе. */
let _promptTimer=null;
document.addEventListener('input',e=>{
  const ta=e.target.closest('[data-set="agent.systemPrompt"]');
  if(!ta)return;
  clearTimeout(_promptTimer);
  _promptTimer=setTimeout(saveAgentPrompt,1200);
});
