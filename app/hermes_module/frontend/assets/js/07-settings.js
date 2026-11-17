/* 07-settings.js — Предпросмотр, привязка настроек, крон, колокольчик */
/* ============================================================
   FILE PREVIEW
   ============================================================ */
let pvCurrent=null;
function openPreview(o){
  pvCurrent=o;
  const ov=$('#previewOv'), body=$('#pvBody'), ttl=$('#pvTitle');
  if(!ov||!body)return;
  if(ttl)ttl.textContent=o.filename||o.title||'preview';
  if(o.lang==='html'){ body.innerHTML='<iframe sandbox="allow-scripts allow-modals allow-forms" srcdoc="'+esc(o.content)+'"></iframe>' }
  else { body.innerHTML='<pre>'+esc(o.content)+'</pre>' }
  ov.classList.add('open');
}
function closePreview(){ const ov=$('#previewOv'); if(ov)ov.classList.remove('open'); const b=$('#pvBody'); if(b)b.innerHTML=''; pvCurrent=null }
$('#pvClose').onclick=closePreview;
$('#previewOv').addEventListener('click',e=>{ if(e.target.id==='previewOv')closePreview() });
$('#pvDownload').onclick=()=>{ if(!pvCurrent)return;
  const isHtml=pvCurrent.lang==='html';
  const fname=(pvCurrent.filename||'document').replace(/\.[^.]+$/,'')+(isHtml?'.html':'.txt');
  dl(new Blob([pvCurrent.content],{type:isHtml?'text/html':'text/plain'}),fname);
  toast('Файл сохранён','ok');
};
$('#pvCopy').onclick=()=>{ if(!pvCurrent)return; navigator.clipboard.writeText(pvCurrent.content).catch(()=>{}); toast('Скопировано','ok') };
function extractCode(content){
  const m=String(content||'').match(/```(\w+)?\n?([\s\S]*?)```/);
  if(!m)return null;
  return {lang:(m[1]||'').toLowerCase()||'text',code:m[2].replace(/^\n/,'').replace(/\n$/,'')};
}
function demoPage(topic){
  const t=String(topic||'Демо-страница').replace(/</g,'&lt;').slice(0,80);
  return '<!DOCTYPE html>\n<html lang="ru">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>'+t+'</title>\n'
  +'<style>\n*{margin:0;padding:0;box-sizing:border-box}\nbody{font-family:system-ui,sans-serif;background:#f6f7f9;color:#161a22;line-height:1.6}\n'
  +'header{background:#4f46e5;color:#fff;padding:56px 24px;text-align:center}\nheader h1{font-size:32px;margin-bottom:8px}\n'
  +'main{max-width:820px;margin:0 auto;padding:40px 24px}\n.card{background:#fff;border:1px solid #e8eaef;border-radius:14px;padding:24px;margin-bottom:16px;box-shadow:0 1px 2px rgba(16,24,40,.04)}\n'
  +'.card h2{font-size:19px;margin-bottom:8px}\n.btn{display:inline-block;background:#4f46e5;color:#fff;padding:10px 20px;border-radius:9px;text-decoration:none;margin-top:8px}\n'
  +'footer{text-align:center;color:#99a1af;padding:32px;font-size:12px}\n</style>\n</head>\n<body>\n'
  +'<header><h1>'+t+'</h1><p>Создано голосовым агентом</p></header>\n<main>\n'
  +'<div class="card"><h2>Раздел 1</h2><p>Это демонстрационная страница. Замените этот текст на свой контент — структура и стили уже готовы.</p><a class="btn" href="#">Кнопка</a></div>\n'
  +'<div class="card"><h2>Раздел 2</h2><p>Страница адаптивна: проверьте предпросмотр при разной ширине окна.</p></div>\n'
  +'</main>\n<footer>© '+new Date().getFullYear()+'</footer>\n</body>\n</html>';
}
function demoDoc(topic){
  const t=String(topic||'Документ').replace(/</g,'<').slice(0,80);
  return '# '+t+'\n\nСоздано голосовым агентом, '+new Date().toLocaleDateString('ru-RU')+'\n\n## Введение\n\nЭтот документ сгенерирован автоматически. Замените текст на свой.\n\n'
  +'## Основная часть\n\n- Первый пункт\n- Второй пункт\n- Третий пункт\n\n## Заключение\n\nГотов к редактированию и экспорту.\n';
}

/* ============================================================
   SETTINGS BINDING
   ============================================================ */
function bindSettings(){
  $$('[data-set]').forEach(el=>{
    const p=el.dataset.set, v=getPath(S(),p);
    if(el.classList.contains('toggle')) el.classList.toggle('on',!!v);
    else if(el.type==='range'||el.type==='number'||el.tagName==='TEXTAREA'||el.tagName==='INPUT') el.value=v??'';
    else if(el.tagName==='SELECT'&&el.options.length&&[...el.options].some(o=>o.value===String(v))) el.value=v;
  });
  $$('[data-out]').forEach(o=>paint(o.dataset.out));
  $$('[data-seg]').forEach(g=>{ const cur=getPath(S(),g.dataset.seg);
    $$('button',g).forEach(b=>b.classList.toggle('on',b.dataset.val===String(cur))) });
}
function paint(p){ const o=$('[data-out="'+p+'"]'); if(!o)return; const v=getPath(S(),p);
  o.textContent=/volume|sensitivity/.test(p)?Math.round(v*100)+'%':(typeof v==='number'?v.toFixed(v<10?2:0):v) }
/* Смена настроек крон-а: таймер и колокольчик должны перезапуститься сразу, иначе будет старый интервал. */
function cronOptChanged(p){
  if(!/^cron\./.test(p))return;
  if(typeof cronAutoRefresh==='function')cronAutoRefresh(ui.panel==='cron'&&S().cron.autoRefresh!==false);
  if(typeof renderCronBell==='function')renderCronBell();
}
/* Переключатели «Отображение в чате» (Агент). Они решают, рисуются ли в ленте
   блоки рассуждений и вызовы инструментов, поэтому включение/выключение должно
   сразу перерисовать чат — иначе переключатель выглядит как не сработавший. */
function agentOptChanged(p){
  if(p!=='agent.showToolCalls'&&p!=='agent.showReasoning')return;
  if(typeof renderStream==='function')renderStream(false);
  if(typeof renderToolsPanel==='function')renderToolsPanel();
}
/* Выключение озвучки на лету должно замолчать немедленно: иначе голос
   продолжает говорить уже после того, как пользователь его выключил. */
function voiceOptChanged(p){
  if(p!=='voice.liveSpeak'&&p!=='voice.autoSpeak'&&p!=='voice.outputEnabled')return;
  if(p==='voice.liveSpeak'&&!getPath(S(),p)&&typeof LiveTTS!=='undefined'&&LiveTTS)LiveTTS.stop();
}
document.addEventListener('click',e=>{
  const tg=e.target.closest('.toggle[data-set]');
  if(tg){ const p=tg.dataset.set; const v=!tg.classList.contains('on'); tg.classList.toggle('on',v);
    setPath(S(),p,v); save(); apply(); flashSaved(); cronOptChanged(p); agentOptChanged(p); voiceOptChanged(p); return }
  const sg=e.target.closest('[data-seg] button');
  if(sg){ const g=sg.parentElement,p=g.dataset.seg; $$('button',g).forEach(b=>b.classList.toggle('on',b===sg));
    setPath(S(),p,sg.dataset.val); save(); apply(); flashSaved();
    if(p==='agent.mode'){const s=active();if(s){s.mode=sg.dataset.val;renderHead()}}
    if(p==='general.theme'){applyTheme()}
    /* Лимит сессий меняет сам список — без перерисовки дерева переключатель
       выглядит как будто не сработал. */
    if(p==='general.recentSessions'){ setPath(S(),p,Number(sg.dataset.val)||0); save(); renderTree() }
    if(p==='cron.interval') setPath(S(),p,Number(sg.dataset.val)||30);
    cronOptChanged(p); return }
  /* Режим активации плагинов: свой сегмент (id, а не data-seg), потому что
     переключение меняет поведение наблюдателя за папкой. */
  const pm=e.target.closest('#plugModeSeg button');
  if(pm){
    S().plugins.mode=pm.dataset.val;
    $$('button',pm.parentElement).forEach(b=>b.classList.toggle('on',b===pm));
    save(); flashSaved();
    toast(pm.dataset.val==='auto'?'Новые плагины будут подключаться автоматически':
                              'Новые плагины будут спрашивать подтверждение','info');
    return;
  }
});
document.addEventListener('input',e=>{ const el=e.target.closest('[data-set]'); if(!el)return;
  const p=el.dataset.set; let v=el.type==='range'||el.type==='number'?parseFloat(el.value):el.value;
  setPath(S(),p,v); paint(p); if(el.type!=='range')save(); apply(); });
document.addEventListener('change',e=>{ const el=e.target.closest('[data-set]'); if(!el)return;
  save(); apply(); flashSaved(); });
function flashSaved(){ const p=$('#savePill');if(!p)return; p.style.opacity='1'; clearTimeout(p._t); p._t=setTimeout(()=>p.style.opacity='0',1200) }
function apply(){
  applyAppearance();
  document.body.classList.toggle('no-anim',!S().general.animations);
  if(recog){recog.lang=S().voice.recLang;recog.continuous=S().voice.continuous}
  updateConnPill(); applyTheme();
}
function renderToolsSettings(){
  const tl=$('#toolsList');if(!tl)return;
  tl.innerHTML=TOOLS.map(t=>'<div class="toolrow'+(S().tools[t.k]?'':' off')+'" data-tool="'+t.k+'">'+
    '<span class="ti"><i class="'+t.i+'"></i></span><div class="txt" style="flex:1"><b>'+esc(t.n)+'</b>'+
    '<small style="color:var(--text-3)">'+esc(t.d)+'</small></div>'+
    '<div class="toggle'+(S().tools[t.k]?' on':'')+'" data-set="tools.'+t.k+'"></div></div>').join('');
  /* Навыки агента. Раньше они грузились из /v1/skills и терялись: вкладка
     показывала только инструменты, поэтому навыков не было видно вообще.
     Это список для чтения — включать их тумблерами бессмысленно, шлюз сам
     решает, какие навыки доступны агенту. */
  const skillHtml=SKILLS.length?SKILLS.map(s=>'<div class="toolrow'+(s.enabled?'':' off')+'">'+
    '<span class="ti"><i class="'+(s.i||'fa-solid fa-wand-magic-sparkles')+'"></i></span>'+
    '<div class="txt" style="flex:1"><b>'+esc(s.n)+'</b>'+
    (s.d?'<small style="color:var(--text-3)">'+esc(s.d)+'</small>':'')+'</div>'+
    '<span class="badge ok">навык</span></div>').join('')
    :'<div class="empty" style="padding:14px"><i class="fa-solid fa-wand-magic-sparkles"></i>'+
     '<p>Навыки не найдены — шлюз не отдал список (/v1/skills)</p></div>';
  tl.innerHTML+='<div class="sub" style="margin:16px 0 8px;font-size:11.5px;color:var(--text-3)">'+
    'Навыки агента · '+SKILLS.length+'</div><div id="skillsBox">'+skillHtml+'</div>';

  /* Пользовательские записи. Держатся в настройках, поэтому переживают
     перезагрузку, и шлюзу о них неизвестно — это личный справочник. */
  const T=S().tools;
  const cust=Array.isArray(T.custom)?T.custom:[];
  tl.innerHTML+='<div class="sub" style="margin:16px 0 8px;font-size:11.5px;color:var(--text-3)">'+
    'Свои инструменты · '+cust.length+'</div>'+
    (cust.length?cust.map((c,i)=>'<div class="toolrow">'+
        '<span class="ti"><i class="'+(c.i||'fa-solid fa-screwdriver-wrench')+'"></i></span>'+
        '<div class="txt" style="flex:1"><b>'+esc(c.n||c.k)+'</b>'+
        (c.d?'<small style="color:var(--text-3)">'+esc(c.d)+'</small>':'')+'</div>'+
        '<span class="badge wait">вручную</span>'+
        '<button class="icon-btn sm" data-tcedit="'+i+'" title="Изменить"><i class="fa-solid fa-pen"></i></button>'+
        '<button class="icon-btn sm" data-tcdel="'+i+'" title="Удалить"><i class="fa-regular fa-trash-can"></i></button>'+
      '</div>').join('')
     :'<div class="empty" style="padding:12px"><p>Своих инструментов нет — нажмите «Добавить»</p></div>')+
    '<div class="sub" style="margin:16px 0 8px;font-size:11.5px;color:var(--text-3)">'+
    'Свои навыки · '+((Array.isArray(T.customSkills)?T.customSkills:[]).length)+'</div>'+
    (Array.isArray(T.customSkills)&&T.customSkills.length
      ?T.customSkills.map((c,i)=>'<div class="toolrow">'+
        '<span class="ti"><i class="fa-solid fa-wand-magic-sparkles"></i></span>'+
        '<div class="txt" style="flex:1"><b>'+esc(c.n||c.k)+'</b>'+
        (c.d?'<small style="color:var(--text-3)">'+esc(c.d)+'</small>':'')+'</div>'+
        '<span class="badge wait">вручную</span>'+
        '<button class="icon-btn sm" data-skedit="'+i+'" title="Изменить"><i class="fa-solid fa-pen"></i></button>'+
        '<button class="icon-btn sm" data-skdel="'+i+'" title="Удалить"><i class="fa-regular fa-trash-can"></i></button>'+
      '</div>').join('')
     :'<div class="empty" style="padding:12px"><p>Своих навыков нет</p></div>');

  tl.onclick=async e=>{
    const ed=e.target.closest('[data-tcedit]');
    if(ed){ editCustomTool(+ed.dataset.tcedit); return }
    const dl=e.target.closest('[data-tcdel]');
    if(dl){ S().tools.custom.splice(+dl.dataset.tcdel,1); save(); renderToolsSettings(); return }
    const se=e.target.closest('[data-skedit]');
    if(se){ editCustomSkill(+se.dataset.skedit); return }
    const sd=e.target.closest('[data-skdel]');
    if(sd){ S().tools.customSkills.splice(+sd.dataset.skdel,1); save(); renderToolsSettings(); return }
    if(e.target.closest('#toolAddCustom'))editCustomTool(null);
    if(e.target.closest('#skillAddCustom'))editCustomSkill(null);
  };
}

/* Добавление/правка своей записи. Ключ приводим к безопасному виду —
   он же используется в настройке tools.<k> для тумблеров. */
function customKey(name){
  return String(name||'').trim().toLowerCase()
    .replace(/[^a-zа-яё0-9]+/gi,'_').replace(/^_+|_+$/g,'').slice(0,40);
}
async function editCustomTool(i){
  const cur=Array.isArray(S().tools.custom)?S().tools.custom:[];
  const ex=i!=null?cur[i]:null;
  const v=await askForm({title:(ex?'Изменить':'Новый')+' инструмент',icon:'fa-screwdriver-wrench',ok:'Сохранить',fields:[
    {name:'n',label:'Название',value:ex?ex.n:'',ph:'Мой инструмент'},
    {name:'d',label:'Описание',value:ex?ex.d:'',ph:'Что делает'}]});
  if(!v||!String(v.n||'').trim())return;
  const k=customKey(v.n);
  if(!k){ toast('Название не даёт допустимого ключа','err'); return }
  if(i==null&&cur.some(x=>x.k===k)){ toast('Такой инструмент уже есть','err'); return }
  const rec={k,n:String(v.n).trim(),d:String(v.d||''),i:skillIcon(k)};
  if(ex) cur[i]=rec; else cur.push(rec);
  S().tools.custom=cur; S().tools['x_'+k]=true;   /* тумблер по умолчанию вкл */
  save(); renderToolsSettings(); toast('Сохранено','ok');
}
async function editCustomSkill(i){
  const cur=Array.isArray(S().tools.customSkills)?S().tools.customSkills:[];
  const ex=i!=null?cur[i]:null;
  const v=await askForm({title:(ex?'Изменить':'Новый')+' навык',icon:'fa-wand-magic-sparkles',ok:'Сохранить',fields:[
    {name:'n',label:'Название',value:ex?ex.n:'',ph:'Мой навык'},
    {name:'d',label:'Описание',value:ex?ex.d:'',ph:'Что умеет'}]});
  if(!v||!String(v.n||'').trim())return;
  const k=customKey(v.n);
  if(!k){ toast('Название не даёт допустимого ключа','err'); return }
  if(i==null&&cur.some(x=>x.k===k)){ toast('Такой навык уже есть','err'); return }
  const rec={k,n:String(v.n).trim(),d:String(v.d||'')};
  if(ex) cur[i]=rec; else cur.push(rec);
  S().tools.customSkills=cur; save(); renderToolsSettings(); toast('Сохранено','ok');
}

/* «Обновить» в «Инструментах»: перечитываем шлюз, а если он молчит — диск. */
$('#toolsReload')&&($('#toolsReload').onclick=async()=>{
  TOOLS.length=0; SKILLS.length=0;
  try{
    await loadRealTools();
    if(!TOOLS.length) await loadToolsFallback();
    renderToolsSettings();
    toast('Обновлено: инструментов '+TOOLS.length+', навыков '+SKILLS.length,'ok');
  }catch(e){ toast('Не обновилось: '+errText(e&&e.message||e),'err') }
});

/* ============================================================
   КРОН-ЗАДАЧИ АГЕНТА (Настройки → Крон-задачи)
   Роуты /api/cron/* монтируются локальным web_server поверх
   cronjob-инструмента агента — шлюз этой сборки их не отдаёт.
   ============================================================ */
const CRON={
  jobs:[], runs:[], incidents:[], gw:null, loaded:false,
  /* Поддерживаемые сокращения расписания — подсказка в форме создания */
  hints:['30m','every 2h','every day at 9am','every monday 9am','0 9 * * *','in 30m']
};
const CRON_API={
  list:()=>API.req('GET','/api/cron/jobs'),
  runs:(jobId,limit)=>API.req('GET','/api/cron/runs'+(jobId?'?job_id='+encodeURIComponent(jobId):'')+'&limit='+(limit||30)),
  act:(action,body)=>API.req('POST','/api/cron/'+action,body||{})
};
function cronStateCls(j){
  if(j.state==='failed'||j.failure_streak>0)return 'err';
  if(!j.enabled||j.state==='paused')return 'off';
  return '';
}
function cronStateText(j){
  if(j.state==='failed')return 'ошибка';
  if(j.state==='paused'||!j.enabled)return j.paused_reason?('пауза: '+j.paused_reason):'на паузе';
  if(j.state==='completed')return 'завершена';
  if(j.state==='running')return 'выполняется';
  return j.state||'по расписанию';
}
function renderCronSettings(){
  const box=$('#cronJobs'); if(!box)return;
  const pill=$('#cronGwPill');
  if(pill){
    const on=CRON.gw===true;
    pill.className='pill'+(on?'':' busy');
    pill.innerHTML='<i class="fa-solid fa-circle"></i><span>'+(CRON.gw===null?'проверка…':on?'шлюз запущен':'шлюз не отвечает')+'</span>';
  }
  if(!CRON.loaded){
    box.innerHTML='<div class="empty" style="padding:22px"><i class="fa-solid fa-spinner fa-spin"></i><p>Загрузка…</p></div>';
    return;
  }
  if(!CRON.jobs.length){
    box.innerHTML='<div class="empty" style="padding:22px"><i class="fa-solid fa-clock-rotate-left"></i><p>Задач нет. Создайте первую.</p></div>';
  }else{
    box.innerHTML=CRON.jobs.map(j=>{
      const cls=cronStateCls(j);
      const next=j.next_run_at?('след. запуск '+ago(j.next_run_at)):(j.state==='completed'?'больше не повторяется':'без следующего запуска');
      const last=j.last_run_at?('последний '+ago(j.last_run_at)+(j.last_status?' · '+j.last_status:'')):'ещё не запускалась';
      return '<div class="crow '+cls+'">'+
        '<span class="ci"><i class="fa-solid '+(cls==='err'?'fa-triangle-exclamation':(j.enabled?'fa-bolt':'fa-pause'))+'"></i></span>'+
        '<div class="cb"><b>'+esc(j.name||j.id)+'</b>'+
          '<small>'+esc(j.schedule_display||j.schedule?.display||'без расписания')+' · '+esc(cronStateText(j))+'</small>'+
          '<small>'+esc(next)+' · '+esc(last)+'</small>'+
          (j.prompt?'<small title="'+esc(j.prompt)+'">'+esc(String(j.prompt).slice(0,110))+(String(j.prompt).length>110?'…':'')+'</small>':'')+
        '</div>'+
        '<div class="cacts">'+
          '<button data-cact="run" data-id="'+esc(j.id)+'" title="Выполнить сейчас"><i class="fa-solid fa-play"></i></button>'+
          (j.state==='running'?'<button data-cact="cancel" data-id="'+esc(j.id)+'" title="Остановить выполнение (расписание сохранится)"><i class="fa-solid fa-stop"></i></button>':'')+
          '<button data-cact="toggle" data-id="'+esc(j.id)+'" title="'+(j.enabled?'Поставить на паузу':'Возобновить')+'"><i class="fa-solid '+(j.enabled?'fa-pause':'fa-play')+'"></i></button>'+
          '<button class="del" data-cact="del" data-id="'+esc(j.id)+'" title="Удалить"><i class="fa-regular fa-trash-can"></i></button>'+
        '</div></div>';
    }).join('');
  }
  const rb=$('#cronRuns');
  if(rb){
    if(!CRON.runs.length){ rb.innerHTML='<div class="empty" style="padding:22px"><i class="fa-solid fa-clock-rotate-left"></i><p>История появится после первого запуска</p></div>' }
    else{
      rb.innerHTML=CRON.runs.map(r=>'<div class="crun">'+
        '<span class="st '+(r.status||'')+'"></span>'+
        '<span class="rn" title="'+esc(r.job_name)+'">'+esc(r.job_name)+
          (r.error?' <span style="color:var(--err)">— '+esc(String(r.error).slice(0,70))+'</span>':'')+'</span>'+
        '<span class="rt">'+(r.started_at?esc(String(r.started_at).replace('T',' ').slice(0,16)):'')+'</span>'+
        '</div>').join('');
    }
  }
  const ig=$('#cronIncGroup');
  if(ig){
    ig.style.display=CRON.incidents.length?'':'none';
    if(CRON.incidents.length){
      $('#cronIncidents').innerHTML=CRON.incidents.map(i=>'<div class="cinc"><b>'+esc(i.job_name)+'</b> · '+
        esc(i.failure_type||i.state||'сбой')+'<br>'+esc(String(i.error||'').slice(0,220))+
        '<br><span style="color:var(--text-3)">с '+esc(String(i.first_seen_at||'').replace('T',' ').slice(0,16))+'</span></div>').join('');
    }
  }
}
async function loadCronSettings(silent){
  try{
    const d=await CRON_API.list();
    CRON.jobs=d.jobs||[]; CRON.gw=(d.gateway_running===true); CRON.loaded=true;
  }catch(e){
    CRON.loaded=true; CRON.gw=false; CRON.jobs=[];
    if(!silent)toast('Не удалось загрузить крон-задачи','err');
  }
  try{ const h=await CRON_API.runs(null,30); CRON.runs=h.runs||[]; CRON.incidents=h.incidents||[] }catch(e){}
  renderCronSettings();
  if(typeof renderCronBell==='function')renderCronBell();
}
async function cronAction(action,body){
  try{
    await CRON_API.act(action,body);
    await loadCronSettings(true);
    const msg={create:'Задача создана',run:'Запущено',cancel:'Выполнение остановлено',pause:'На паузе',resume:'Возобновлено',remove:'Удалено',update:'Обновлено'}[action];
    if(msg)toast(msg,'ok');
  }catch(e){
    toast('Ошибка: '+((e&&e.message)||e),'err');
  }
}
async function cronCreateForm(){
  const v=await askForm({title:'Новая крон-задача',icon:'fa-clock-rotate-left',ok:'Создать',fields:[
    {name:'name',label:'Название',ph:'Проверка бэкапа'},
    {name:'schedule',label:'Расписание',ph:'30m / every 2h / every day at 9am / 0 9 * * *',value:'every day at 9am'},
    {name:'prompt',label:'Задание для агента',type:'area',ph:'Проверь, что папка backup существует, и напиши результат'}
  ]});
  if(!v||!v.schedule||!v.prompt)return;
  await cronAction('create',{name:v.name||'',schedule:v.schedule.trim(),prompt:v.prompt.trim()});
}
function bindCronSettings(){
  const add=$('#cronAdd'); if(add)add.onclick=cronCreateForm;
  const rl=$('#cronReload'); if(rl)rl.onclick=()=>loadCronSettings();
  const box=$('#cronJobs');
  if(box)box.onclick=async e=>{
    const b=e.target.closest('[data-cact]'); if(!b)return;
    const id=b.dataset.id, act=b.dataset.cact;
    const j=CRON.jobs.find(x=>x.id===id); if(!j)return;
    if(act==='run'){ await cronAction('run',{job_id:id}); return }
    if(act==='cancel'){ await cronAction('cancel',{job_id:id,reason:'остановлено из интерфейса'}); return }
    if(act==='toggle'){ await cronAction(j.enabled?'pause':'resume',{job_id:id}); return }
    if(act==='del'){
      const ok=await askForm({title:'Удалить задачу?',icon:'fa-triangle-exclamation',ok:'Удалить',
        fields:[{name:'x',label:'Задача «'+(j.name||id)+'» будет удалена безвозвратно.'}]});
      if(ok)await cronAction('remove',{job_id:id});
    }
  };
}
/* ============================================================
   КОЛОКОЛЬЧИК ЗАДАНИЙ + АВТООБНОВЛЕНИЕ
   Задания выполняются в фоне, поэтому их состояние опрашивается
   независимо от открытой вкладки настроек.
   ============================================================ */
const CRON_TICK={timer:null,seen:new Set(),primed:false};
function cronFailCount(){ return CRON.runs.filter(r=>r.status!=='ok'&&r.status).length }
function cronBeep(){
  try{
    if(!S().cron.notifySound)return;
    const C=window.AudioContext||window.webkitAudioContext; if(!C)return;
    const ac=new C(), o=ac.createOscillator(), g=ac.createGain();
    o.type='sine'; o.frequency.setValueAtTime(880,ac.currentTime);
    o.frequency.exponentialRampToValueAtTime(440,ac.currentTime+.18);
    g.gain.setValueAtTime(.0001,ac.currentTime);
    g.gain.exponentialRampToValueAtTime(.12*Number(S().general.volume||.6),ac.currentTime+.02);
    g.gain.exponentialRampToValueAtTime(.0001,ac.currentTime+.3);
    o.connect(g); g.connect(ac.destination); o.start(); o.stop(ac.currentTime+.32);
  }catch(e){}
}
function renderCronBell(){
  const btn=$('#hdCron'); if(!btn)return;
  const on=S().cron.notify!==false;
  const list=$('#cronPopList');
  btn.classList.toggle('has',on);
  if(!on){ $('#cronPop').classList.remove('open'); return }
  const box=$('#cronBdg');
  const bad=cronFailCount();
  if(box){ box.textContent=bad>99?'99+':String(bad); btn.classList.toggle('has',bad>0) }
  if(!list)return;
  if(!CRON.loaded){ list.innerHTML='<div style="padding:10px;font-size:11.5px;color:var(--text-3)">Загрузка…</div>'; return }
  const items=CRON.runs.slice(0,8);
  if(!items.length){
    list.innerHTML='<div style="padding:10px;font-size:11.5px;color:var(--text-3)">Запусков пока не было</div>';
    return;
  }
  list.innerHTML=items.map(r=>'<div class="ci'+(r.status&&r.status!=='ok'?' failed':'')+'">'+
    '<span class="dot"></span><span class="cb"><b>'+esc(r.job_name||r.job_id)+
    '</b><small>'+(r.status==='ok'?'успешно':esc(r.status||'ошибка'))+
    (r.error?' — '+esc(String(r.error).slice(0,60)):'')+
    (r.started_at?' · '+esc(String(r.started_at).replace('T',' ').slice(5,16)):'')+'</small></span></div>').join('');
}
/* Первый опрос только запоминает текущие запуски; уведомления
   начинают приходить со следующего — иначе колокольчик «загорается»
   сразу при открытии страницы на давно завершившихся задачах. */
function cronScanRuns(){
  const fresh=[];
  for(const r of CRON.runs){
    const k=(r.id!=null?r.id:'')+'|'+(r.started_at||'')+'|'+(r.job_id||'');
    if(CRON_TICK.seen.has(k))continue;
    CRON_TICK.seen.add(k);
    if(CRON_TICK.primed&&(r.status!=='ok'))fresh.push(r);
  }
  if(fresh.length){
    cronBeep();
    const names=[...new Set(fresh.map(r=>r.job_name||r.job_id))].join(', ');
    toast('Задания по расписанию: ошибка в «'+(names.length>40?names.slice(0,40)+'…':names)+'»','err');
  }
  CRON_TICK.primed=true;
}
function cronTick(){
  loadCronSettings(true).then(()=>{ renderCronBell(); cronScanRuns() });
}
function cronAutoRefresh(on){
  if(CRON_TICK.timer){ clearInterval(CRON_TICK.timer); CRON_TICK.timer=null }
  if(!on)return;
  const iv=Math.max(10,Number(S().cron.interval)||30)*1000;
  CRON_TICK.timer=setInterval(cronTick,iv);
}
function bindCronBell(){
  const btn=$('#hdCron'); if(!btn||btn._bound)return;
  btn._bound=true;
  const pop=$('#cronPop');
  btn.onclick=e=>{
    e.stopPropagation();
    const open=pop.classList.toggle('open');
    if(open)cronTick();
  };
  pop.onclick=e=>e.stopPropagation();
  const openAll=$('#cronPopOpen');
  if(openAll)openAll.onclick=()=>{ pop.classList.remove('open'); openSet('cron') };
  document.addEventListener('click',()=>pop.classList.remove('open'));
  renderCronBell();
}