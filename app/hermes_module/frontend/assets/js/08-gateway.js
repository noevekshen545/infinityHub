/* 08-gateway.js — Настройки шлюза и профили */
/* ============================================================
   НАСТРОЙКИ ШЛЮЗА в вкладке «Агент»
   Список полей приходит с бэкенда (белый список ключей config.yaml),
   поэтому браузер не может записать в конфиг никакой произвольный путь.
   ============================================================ */
const ACF={fields:[],values:{},dirty:{},loaded:false};
const ACF_API={
  get:()=>API.req('GET','/api/agent-config'),
  save:(values)=>API.req('POST','/api/agent-config',{values})
};
const ACF_GROUPS={model:'Модель',agent:'Агент',delegation:'Делегация',compression:'Сжатие контекста',
  memory:'Память',approvals:'Подтверждения',sessions:'Сессии',browser:'Браузер',computer_use:'Управление КП',display:'Отображение'};
function acfRowHtml(f){
  const v=ACF.values[f.key];
  const val=(v===null||v===undefined)?'':String(v);
  let ctl='';
  if(f.type==='choice'){
    ctl='<div class="seg" data-acf="'+esc(f.key)+'">'+
      (f.options||[]).map(o=>'<button data-val="'+esc(o)+'"'+(o===val?' class="on"':'')+'>'+esc(o)+'</button>').join('')+'</div>';
  }else if(f.type==='bool'){
    ctl='<div class="toggle'+(v===true?' on':'')+'" data-acf="'+esc(f.key)+'"></div>';
  }else if(f.type==='int'||f.type==='float'){
    ctl='<input class="field" type="number" step="'+(f.type==='int'?'1':'0.05')+'" style="width:130px" data-acf="'+esc(f.key)+'" value="'+esc(val)+'">';
  }else{
    ctl='<input class="field" type="text" style="width:250px" data-acf="'+esc(f.key)+'" value="'+esc(val)+'">';
  }
  return '<div class="srow" data-acfrow="'+esc(f.key)+'"><div class="txt"><b>'+esc(f.label)+
    '</b><small>'+esc(f.key)+'</small></div><div class="ctl">'+ctl+'</div></div>';
}
function renderAgentConfig(){
  const box=$('#acfBox'); if(!box)return;
  if(!ACF.loaded){ box.innerHTML='<div class="empty" style="padding:18px"><i class="fa-solid fa-spinner fa-spin"></i><p>Загрузка параметров шлюза…</p></div>'; return }
  if(!ACF.fields.length){
    box.innerHTML='<div class="empty" style="padding:18px"><i class="fa-solid fa-triangle-exclamation"></i><p>Настройки недоступны: не удалось прочитать config.yaml</p></div>';
    return;
  }
  let html='',group=null;
  for(const f of ACF.fields){
    const g=f.key.split('.')[0];
    if(g!==group){ group=g; html+='<div class="acf-grp">'+esc(ACF_GROUPS[g]||g)+'</div>'; }
    html+=acfRowHtml(f);
  }
  box.innerHTML=html;
}
async function loadAgentConfig(){
  try{
    const d=await ACF_API.get();
    ACF.fields=d.fields||[]; ACF.values=d.values||{}; ACF.dirty={}; ACF.loaded=true;
    ACF.orig=Object.assign({},d.values||{});
    renderAgentConfig(); acfTouch();
    if(typeof renderAcfProfiles==='function')renderAcfProfiles();
  }catch(e){
    ACF.loaded=true; ACF.fields=[];
    const box=$('#acfBox');
    if(box)box.innerHTML='<div class="empty" style="padding:18px"><i class="fa-solid fa-triangle-exclamation"></i><p>Не удалось прочитать config.yaml: '+esc(String((e&&e.message)||e))+'</p></div>';
  }
}
async function saveAgentConfig(){
  const btn=$('#acfSave');
  if(btn){ btn.disabled=true; btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Сохранение…' }
  try{
    const payload={};
    Object.keys(ACF.dirty).forEach(k=>{ if(ACF.fields.some(x=>x.key===k))payload[k]=ACF.values[k] });
    if(!Object.keys(payload).length){ toast('Нет изменений','info'); renderAgentConfig(); return }
    const r=await ACF_API.save(payload);
    ACF.dirty={};
    await loadAgentConfig();
    toast('Сохранено параметров: '+(r.updated||[]).length,'ok');
    toast('Шлюз применит их после перезапуска','info');
  }catch(e){
    toast('Не сохранилось: '+((e&&e.message)||e),'err');
  }finally{
    if(btn)btn.innerHTML='<i class="fa-solid fa-floppy-disk"></i> Сохранить в шлюз';
    renderAgentConfig();
  }
}
function acfFmt(v){
  if(v===null||v===undefined||v==='')return '—';
  if(typeof v==='boolean')return v?'вкл':'выкл';
  return String(v);
}
/* Сравнение «было → стало». Показывает ровно то, что уедет в config.yaml. */
function renderAcfDiff(){
  const box=$('#acfDiff'); if(!box)return;
  const keys=Object.keys(ACF.dirty);
  if(!keys.length){ box.innerHTML=''; return }
  const rows=keys.map(k=>{
    return '<div class="dl"><span class="dk">'+esc(k)+'</span><span class="dv">'+
      '<span class="old">'+esc(acfFmt(ACF.orig?ACF.orig[k]:undefined))+'</span>'+
      '<i class="fa-solid fa-arrow-right arw"></i>'+
      '<span class="new">'+esc(acfFmt(ACF.values[k]))+'</span></span></div>';
  }).join('');
  box.innerHTML='<div class="acf-diff"><b>Будет записано в config.yaml ('+keys.length+')</b>'+rows+'</div>';
}
function acfSame(a,b){
  if(b===null||b===undefined)return a===null||a===undefined||a==='';
  if(typeof b==='boolean')return (a===b);
  if(typeof b==='number')return Number(a)===b;
  return String(a??'')===String(b);
}
function acfTouch(){
  /* Возврат поля к исходному значению должен убирать его из diff —
     иначе в списке «будет записано» остаются бессмысленные строки. */
  for(const k of Object.keys(ACF.dirty)) if(acfSame(ACF.values[k],ACF.orig?ACF.orig[k]:undefined)) delete ACF.dirty[k];
  const b=$('#acfSave'); if(b)b.disabled=!Object.keys(ACF.dirty).length;
  renderAcfDiff();
  /* Подсветка строки берётся из списка изменённых, а не из факта клика:
     иначе снятое изменение оставалось бы выделенным. */
  $$('#acfBox [data-acfrow]').forEach(r=>r.classList.toggle('dirty',!!ACF.dirty[r.dataset.acfrow]));
}
function bindAgentConfig(){
  const box=$('#acfBox'); if(!box||box._acfBound)return;
  box._acfBound=true;
  box.addEventListener('change',e=>{
    const el=e.target.closest('[data-acf]'); if(!el)return;
    const key=el.dataset.acf, f=ACF.fields.find(x=>x.key===key); if(!f)return;
    if(f.type==='bool') ACF.values[key]=el.classList.contains('on');
    else if(f.type==='int') ACF.values[key]=el.value===''?null:parseInt(el.value,10);
    else if(f.type==='float') ACF.values[key]=el.value===''?null:parseFloat(el.value);
    else ACF.values[key]=el.value;
    ACF.dirty[key]=true;
    acfTouch();
  });
  box.addEventListener('click',e=>{
    if(e.target.closest('[data-acf].toggle')){
      const el=e.target.closest('[data-acf].toggle');
      const key=el.dataset.acf, f=ACF.fields.find(x=>x.key===key); if(!f)return;
      const on=!el.classList.contains('on');
      el.classList.toggle('on',on);
      ACF.values[key]=on; ACF.dirty[key]=true;
        acfTouch();
      return;
    }
    const seg=e.target.closest('.seg[data-acf] button'); if(!seg)return;
    const key=seg.parentElement.dataset.acf, f=ACF.fields.find(x=>x.key===key); if(!f)return;
    $$('button',seg.parentElement).forEach(b=>b.classList.toggle('on',b===seg));
    ACF.values[key]=seg.dataset.val; ACF.dirty[key]=true;
    acfTouch();
  });
  const sv=$('#acfSave'); if(sv)sv.onclick=saveAgentConfig;
  const rl=$('#acfReload'); if(rl)rl.onclick=()=>{ loadAgentConfig(); toast('Обновлено из шлюза','ok') };
  const rs=$('#acfRestart');
  if(rs)rs.onclick=async()=>{
    const label='<i class="fa-solid fa-power-off"></i> Перезапустить шлюз';
    rs.disabled=true; rs.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Перезапуск…';
    try{
      /* Запрос намеренно без собственного таймаута: шлюз поднимается
         до 90 секунд, и обрыв по таймауту выглядел бы как поломка. */
      const r=await API.req('POST','/api/gateway/restart',{});
      /* started=true — шлюз был остановлен (например, после установки
         обновления) и сейчас поднят; это не «перезапуск», а запуск. */
      toast(r.started?'Шлюз запущен (pid '+(r.pid||'?')+')':'Шлюз перезапущен (pid '+(r.pid||'?')+') — настройки вступили в силу','ok');
    }catch(e){
      toast('Не перезапустилось: '+((e&&e.message)||e),'err');
    }finally{
      rs.disabled=false; rs.innerHTML=label;
    }
  };
  bindAcfProfiles();
}
/* ============================================================
   ПРОФИЛИ ШЛЮЗА
   Хранятся локально (state.agentProfiles) и только подставляют
   значения в форму — запись в config.yaml делает «Сохранить».
   ============================================================ */
function acfProfiles(){ if(!Array.isArray(state.agentProfiles))state.agentProfiles=[]; return state.agentProfiles }
function renderAcfProfiles(){
  const list=$('#acfPfList'), grp=$('#acfPfGroup'); if(!list||!grp)return;
  const ps=acfProfiles();
  grp.style.display=ACF.fields.length?'':'none';
  if(!ps.length){
    list.innerHTML='<div class="empty" style="padding:14px"><i class="fa-solid fa-layer-group"></i><p>Профилей нет. Заполните форму и нажмите «Новый профиль».</p></div>';
    return;
  }
  list.innerHTML=ps.map(p=>{
    const n=Object.keys(p.values||{}).length;
    return '<div class="acf-pf"><i class="fa-solid fa-layer-group" style="color:var(--accent)"></i>'+
      '<span class="pn">'+esc(p.name)+'</span>'+
      '<span class="pc">'+n+' параметр'+((n%10===1&&n%100!==11)?'':(n%10>=2&&n%10<=4&&(n%100<10||n%100>=20)?'а':(n%10===0||(n%10>=5&&n%10<=9)||(n%100>=11&&n%100<=14)?'ов':'')))+'</span>'+
      '<button class="btn sm" data-pf="apply" data-id="'+esc(p.id)+'">Применить</button>'+
      '<button class="btn sm ghost" data-pf="upd" data-id="'+esc(p.id)+'">Обновить</button>'+
      '<button class="icon-btn sm" data-pf="del" data-id="'+esc(p.id)+'"><i class="fa-regular fa-trash-can"></i></button></div>';
  }).join('');
}
function acfApplyProfile(id){
  const p=acfProfiles().find(x=>x.id===id); if(!p)return;
  if(!ACF.loaded||!ACF.fields.length){ toast('Сначала загрузите параметры шлюза','err'); return }
  ACF.pfActive=id;
  for(const f of ACF.fields){
    if(!(f.key in (p.values||{})))continue;
    const v=p.values[f.key];
    if(f.type==='bool'&&ACF.values[f.key]!==v){ ACF.values[f.key]=v; ACF.dirty[f.key]=true }
    else if(f.type!=='bool'&&String(ACF.values[f.key])!==String(v)){ ACF.values[f.key]=v; ACF.dirty[f.key]=true }
  }
  renderAgentConfig(); acfTouch();
  toast('Профиль «'+p.name+'» подставлен — проверьте и сохраните','info');
}
function bindAcfProfiles(){
  const list=$('#acfPfList'); if(!list||list._pfBound)return;
  list._pfBound=true;
  list.onclick=async e=>{
    const b=e.target.closest('[data-pf]'); if(!b)return;
    const id=b.dataset.id, act=b.dataset.pf;
    if(act==='apply'){ acfApplyProfile(id); return }
    if(act==='upd'){
      const p=acfProfiles().find(x=>x.id===id); if(!p)return;
      if(!ACF.loaded){ toast('Параметры не загружены','err'); return }
      p.values=Object.assign({},ACF.values);
      save(); renderAcfProfiles(); toast('Профиль «'+p.name+'» обновлён','ok');
      return;
    }
    if(act==='del'){
      const p=acfProfiles().find(x=>x.id===id); if(!p)return;
      const ok=await askForm({title:'Удалить профиль?',icon:'fa-trash-can',ok:'Удалить',
        fields:[{name:'x',label:'Профиль «'+p.name+'» будет удалён. Параметры в config.yaml не изменятся.'}]});
      if(!ok)return;
      state.agentProfiles=acfProfiles().filter(x=>x.id!==id);
      save(); renderAcfProfiles();
    }
  };
  const nb=$('#acfPfNew');
  if(nb)nb.onclick=async()=>{
    if(!ACF.loaded||!ACF.fields.length){ toast('Сначала загрузите параметры шлюза','err'); return }
    const v=await askForm({title:'Новый профиль',icon:'fa-layer-group',ok:'Создать',fields:[
      {name:'name',label:'Название',ph:'Работа — высокий рассуждение'}]});
    if(!v||!v.name)return;
    state.agentProfiles=acfProfiles();
    state.agentProfiles.push({id:uid('gpf'),name:v.name.trim(),values:Object.assign({},ACF.values)});
    save(); renderAcfProfiles(); toast('Профиль создан','ok');
  };
  const ub=$('#acfPfUpd');
  if(ub)ub.onclick=()=>{
    const cur=acfProfiles().find(x=>x.id===acfCurPf());
    if(!cur){ toast('Сначала примените профиль, чтобы обновить его','err'); return }
    cur.values=Object.assign({},ACF.values);
    save(); renderAcfProfiles(); toast('Профиль «'+cur.name+'» обновлён из формы','ok');
  };
}
function acfCurPf(){ return ACF.pfActive||'' }
function renderProjectsSettings(){
  const list=$('#projList'); const def=$('#defProject');
  if(!list||!def)return;
  list.innerHTML=state.projects.length?state.projects.map(p=>'<div class="projrow"><span class="pi"><i class="fa-solid fa-diagram-project"></i></span>'+
    '<div class="pt"><b>'+esc(p.name)+'</b><small>'+esc(p.path)+'</small></div>'+
    '<span style="font-size:11px;color:var(--text-3)">'+state.sessions.filter(s=>s.projectId===p.id).length+'</span>'+
    '<button class="icon-btn sm" data-pact="open" data-id="'+p.id+'"><i class="fa-solid fa-arrow-right"></i></button>'+
    '<button class="icon-btn sm" data-pact="del" data-id="'+p.id+'"><i class="fa-regular fa-trash-can"></i></button></div>').join('')
    :'<div class="empty" style="padding:22px"><i class="fa-solid fa-folder-open"></i><p>Нет проектов</p></div>';
  def.innerHTML='<option value="">Не выбран</option>'+state.projects.map(p=>'<option value="'+p.id+'">'+esc(p.name)+'</option>').join('');
  def.value=S().projects.defaultProjectId||'';
  list.onclick=e=>{const b=e.target.closest('[data-pact]');if(!b)return;
    const p=state.projects.find(x=>x.id===b.dataset.id);
    if(b.dataset.pact==='open'){ state.activeProjectId=p.id; newSession({projectId:p.id}); closeSet(); openChat(); }
    if(b.dataset.pact==='del'){ state.projects=state.projects.filter(x=>x.id!==p.id); save(); renderProjectsSettings(); renderAll() }};
}
function renderTemplatesSettings(){
  const tl=$('#tmplList'), ctl=$('#customTmplList');
  if(!tl||!ctl)return;
  tl.innerHTML=BUILTIN_TEMPLATES.map(t=>'<div class="tmplrow"><span class="ti"><i class="fa-solid fa-bookmark"></i></span>'+
    '<div class="pt"><b>'+esc(t.n)+'</b><small>'+esc(t.d)+'</small></div>'+
    '<button class="btn sm" data-use="'+t.id+'"><i class="fa-solid fa-play"></i></button></div>').join('');
  ctl.innerHTML=state.customTemplates.length?state.customTemplates.map(t=>'<div class="tmplrow"><span class="ti"><i class="fa-solid fa-bookmark"></i></span>'+
    '<div class="pt"><b>'+esc(t.n)+'</b><small>'+esc(t.d||'')+'</small></div>'+
    '<button class="icon-btn sm" data-utmpl="'+t.id+'"><i class="fa-solid fa-play"></i></button>'+
    '<button class="icon-btn sm" data-dtmpl="'+t.id+'"><i class="fa-regular fa-trash-can"></i></button></div>').join('')
    :'<div class="empty" style="padding:22px"><i class="fa-solid fa-bookmark"></i><p>Нет своих шаблонов</p></div>';
  tl.onclick=e=>{const b=e.target.closest('[data-use]');if(!b)return;
    const t=BUILTIN_TEMPLATES.find(x=>x.id===b.dataset.use); if(!t)return;
    closeSet(); openChat(); newSession({title:t.n,sys:t.sys,msg:t.msg}); };
  ctl.onclick=e=>{
    const u=e.target.closest('[data-utmpl]');
    if(u){ const t=state.customTemplates.find(x=>x.id===u.dataset.utmpl); if(!t)return;
      closeSet(); openChat(); newSession({title:t.n,sys:t.sys,msg:t.msg}); return }
    const d=e.target.closest('[data-dtmpl]');
    if(d){ state.customTemplates=state.customTemplates.filter(x=>x.id!==d.dataset.dtmpl); save(); renderTemplatesSettings() }
  };
}
$('#newTmpl').onclick=async()=>{
  const v=await askForm({title:'Новый шаблон',icon:'fa-bookmark',ok:'Создать',fields:[
    {name:'n',label:'Название',ph:'Мой шаблон'},
    {name:'d',label:'Описание',ph:'Что делает'},
    {name:'sys',label:'Системная инструкция',type:'area',ph:'Ты помощник...'},
    {name:'msg',label:'Первое сообщение',type:'area',ph:'Сделай...'}]});
  if(v&&v.n){ state.customTemplates.push({id:uid('tpl'),n:v.n,d:v.d||'',sys:v.sys||'',msg:v.msg||''});
    save(); renderTemplatesSettings(); toast('Создан','ok') }
};
$('#saveCurrentTmpl').onclick=async()=>{
  const s=active(); if(!s){toast('Нет сессии','err');return}
  const sys=s.messages.find(m=>m.kind==='system')?.content||'';
  const firstUser=s.messages.find(m=>m.role==='user')?.content||'';
  const v=await askForm({title:'Сохранить как шаблон',icon:'fa-bookmark',ok:'Сохранить',fields:[
    {name:'n',label:'Название',value:s.title},
    {name:'d',label:'Описание'},
    {name:'sys',label:'Системная инструкция',type:'area',value:sys},
    {name:'msg',label:'Первое сообщение',type:'area',value:firstUser}]});
  if(v&&v.n){ state.customTemplates.push({id:uid('tpl'),n:v.n,d:v.d||'',sys:v.sys||'',msg:v.msg||''});
    save(); renderTemplatesSettings(); toast('Сохранено','ok') }
};

/* CUSTOM COMMANDS */
function renderCommandsSettings(){
  const list=$('#cmdList'); if(!list)return;
  const cmds=S().commands||[];
  if(!cmds.length){ list.innerHTML='<div class="empty" style="padding:22px"><i class="fa-solid fa-terminal"></i><p>Нет своих команд</p></div>'; return }
  list.innerHTML=cmds.map(c=>'<div class="cmdrow">'+
    '<span class="cmd-name">'+esc(c.name)+'</span>'+
    '<span class="cmd-desc">'+esc(c.desc||'')+'</span>'+
    '<button class="icon-btn sm" data-cmdedit="'+c.id+'"><i class="fa-solid fa-pen"></i></button>'+
    '<button class="icon-btn sm" data-cmddel="'+c.id+'"><i class="fa-regular fa-trash-can"></i></button>'+
    '</div>').join('');
  list.onclick=e=>{
    const del=e.target.closest('[data-cmddel]');
    if(del){ S().commands=S().commands.filter(x=>x.id!==del.dataset.cmddel); save(); renderCommandsSettings(); toast('Удалена','ok'); return }
    const edit=e.target.closest('[data-cmdedit]');
    if(edit){ editCmd(edit.dataset.cmdedit); }
  };
}
$('#cmdNew').onclick=()=>editCmd(null);
async function editCmd(id){
  const existing=id?(S().commands||[]).find(c=>c.id===id):null;
  const v=await askForm({title:id?'Редактировать команду':'Новая команда',icon:'fa-terminal',ok:'Сохранить',fields:[
    {name:'name',label:'Имя команды (например /review)',value:existing?.name||'',ph:'/review'},
    {name:'desc',label:'Описание',value:existing?.desc||'',ph:'Ревью кода'},
    {name:'template',label:'Шаблон промпта',type:'area',value:existing?.template||'',ph:'Проведи ревью {{input}} в проекте {{project}}'}]});
  if(v&&v.name){
    if(!v.name.startsWith('/')) v.name='/'+v.name;
    S().commands=S().commands||[];
    if(existing){
      existing.name=v.name;
      existing.desc=v.desc||'';
      existing.template=v.template||'';
    } else {
      S().commands.push({id:uid('cmd'),name:v.name,desc:v.desc||'',template:v.template||''});
    }
    save(); renderCommandsSettings(); toast('Сохранено','ok');
  }
}

/* PRICING */
function renderPricingSettings(){
  const list=$('#pricingList'); if(!list)return;
  const rates=S().pricing.rates||{};
  list.innerHTML=MODELS.map(m=>{
    const r=rates[m.id]||{in:0,out:0};
    return '<div class="srow">'+
      '<div class="txt"><b>'+esc(m.n)+'</b><small>Контекст: '+m.ctx.toLocaleString('ru')+' токенов</small></div>'+
      '<div class="ctl" style="gap:4px">'+
      '<input type="number" class="field" min="0" step="0.1" value="'+r.in+'" style="width:80px" data-pricein="'+m.id+'" placeholder="In">'+
      '<input type="number" class="field" min="0" step="0.1" value="'+r.out+'" style="width:80px" data-priceout="'+m.id+'" placeholder="Out">'+
      '</div></div>';
  }).join('');
  list.querySelectorAll('input').forEach(inp=>{
    inp.addEventListener('change',e=>{
      const inId=e.target.dataset.pricein;
      const outId=e.target.dataset.priceout;
      if(inId){ S().pricing.rates[inId]=S().pricing.rates[inId]||{in:0,out:0}; S().pricing.rates[inId].in=parseFloat(e.target.value)||0; }
      if(outId){ S().pricing.rates[outId]=S().pricing.rates[outId]||{in:0,out:0}; S().pricing.rates[outId].out=parseFloat(e.target.value)||0; }
      save(); flashSaved();
    });
  });
}

/* PROFILES */
function renderProfiles(){
  const sel=$('#profileSelect'); if(!sel)return;
  const profiles=S().profiles||[];
  sel.innerHTML=profiles.map(p=>'<option value="'+p.id+'">'+esc(p.name)+'</option>').join('');
  sel.value=S().activeProfile||'default';
}
$('#profileSelect').onchange=e=>{
  const id=e.target.value;
  const p=(S().profiles||[]).find(x=>x.id===id);
  if(p&&p.settings){
    state.settings=merge(structuredClone(DEF),p.settings);
    normalizePlugins();          // профиль мог быть сохранён в старом формате
    normalizeIntercepts();
    S().activeProfile=id;
    save(); bindSettings(); apply(); renderAll(); PluginBus.init(); PluginFS.sync();
    toast('Профиль: '+p.name,'ok');
  }
};
$('#profileSave').onclick=()=>{
  const id=S().activeProfile||'default';
  const p=(S().profiles||[]).find(x=>x.id===id);
  if(p){
    p.settings=structuredClone(state.settings);
    delete p.settings.activeProfile;
    save(); toast('Профиль сохранён','ok');
  }
};
$('#profileNew').onclick=async()=>{
  const v=await askForm({title:'Новый профиль',icon:'fa-user',ok:'Создать',fields:[{name:'n',label:'Название',ph:'Работа'}]});
  if(v&&v.n){
    S().profiles=S().profiles||[];
    const newP={id:uid('prof'),name:v.n,settings:structuredClone(state.settings)};
    delete newP.settings.activeProfile;
    S().profiles.push(newP);
    S().activeProfile=newP.id;
    save(); renderProfiles(); toast('Профиль создан','ok');
  }
};

document.addEventListener('change',e=>{ if(e.target&&e.target.id==='defProject'){ S().projects.defaultProjectId=e.target.value||null; save(); flashSaved(); } });
$('#stAddProject').onclick=()=>createProject(false);
$('#stManualProject').onclick=()=>createProject(true);
$('#resetSettings').onclick=async()=>{ if(await askForm({title:'Сбросить?',icon:'fa-triangle-exclamation',ok:'Сбросить',
  fields:[{name:'c',label:'Подтвердите',value:'сброс'}]})){
  const keep={sessions:state.sessions,projects:state.projects,folders:state.folders,activeSessionId:state.activeSessionId,
    activeProjectId:state.activeProjectId,customTemplates:state.customTemplates,pin:state.pin};
  state={...keep,settings:structuredClone(DEF)};
  save(); bindSettings(); renderModels(); apply();
  // Плагины тоже возвращаем к состоянию «пусто», режим — автоматический
  PluginBus.init(); PluginFS.sync(); renderPluginsSettings();
  toast('Сброшено','ok')} };
$('#expAll').onclick=()=>{ dl(new Blob([JSON.stringify({sessions:state.sessions,projects:state.projects,folders:state.folders,
  customTemplates:state.customTemplates,settings:state.settings},null,2)],{type:'application/json'}),'agent-data.json'); toast('Экспорт','ok') };
$('#expMd').onclick=()=>{
  const lines=['# Все сессии\n'];
  state.sessions.forEach(s=>{
    lines.push('## '+s.title);
    lines.push('*'+new Date(s.createdAt).toLocaleString('ru-RU')+'*\n');
    s.messages.forEach(m=>{
      const who=m.role==='user'?'**Вы**':m.role==='agent'?'**Агент**':'_Система_';
      lines.push('### '+who+' · '+tShort(m.ts));
      lines.push(''); lines.push(plain(m.content)); lines.push('');
    });
    lines.push('---\n');
  });
  dl(new Blob([lines.join('\n')],{type:'text/markdown'}),'all-sessions.md'); toast('Экспорт в Markdown','ok');
};
$('#impAll').onclick=()=>$('#jsonPick').click();
$('#jsonPick').onchange=e=>{ const f=e.target.files[0]; if(!f)return; const r=new FileReader();
  r.onload=()=>{ try{ const d=JSON.parse(r.result); state={...state,...d,settings:merge(structuredClone(DEF),d.settings||{})};
    normalizePlugins(); normalizeIntercepts(); PluginBus.init(); PluginFS.sync();
    save(); bindSettings(); renderAll(); toast('Импорт','ok') }catch(err){ toast('Файл повреждён','err') } };
  r.readAsText(f); e.target.value='' };
$('#wipe').onclick=async()=>{ if(await askForm({title:'Удалить всё?',icon:'fa-triangle-exclamation',ok:'Удалить',
  fields:[{name:'c',label:'Подтвердите',value:'удалить'}]})){
  localStorage.removeItem(KEY); if(!IS_FILE_PROTOCOL){ try{ await idbClear('state') }catch(e){} }
  state={sessions:[],projects:[],folders:[],activeSessionId:null,activeProjectId:null,customTemplates:[],settings:structuredClone(DEF)};
  newSession(); bindSettings(); renderAll(); toast('Удалено','ok') } };
$('#eyeBtn').onclick=()=>{const i=$('#eyeBtn').previousElementSibling;if(!i)return;
  i.type=i.type==='password'?'text':'password'; $('#eyeBtn').innerHTML='<i class="fa-regular '+(i.type==='text'?'fa-eye-slash':'fa-eye')+'"></i>'};
async function refreshStoreInfo(){ const sz=await Storage.size();
  const ss=$('#storeSize'),sc=$('#storeCount');
  if(ss)ss.textContent=fmtBytes(sz)+' · сессий: '+state.sessions.length;
  if(sc)sc.textContent=state.sessions.length+' сессий'; }
function renderModels(){ const h=MODELS.map(m=>'<option value="'+m.id+'">'+m.n+'</option>').join('');
  const ms=$('#modelSelect'),fm=$('#fModel');
  if(ms)ms.innerHTML=h; if(fm)fm.innerHTML=h; }

/* TOUR */
const TOUR_STEPS=[
  {target:'#btnMic',title:'Голосовой ввод',text:'Нажмите или удерживайте пробел.'},
  {target:'#btnChat',title:'Рабочая область',text:'Текстовый чат с агентом.'},
  {target:'#btnMore',title:'Меню действий',text:'Сессии, проекты, настройки.'},
  {target:'#hdPomo',title:'Pomodoro',text:'Таймер фокуса 25/5.'},
  {target:'#hdBookmarks',title:'Закладки',text:'Сохраняйте важные сообщения.'},
 {target:'#cSnip',title:'Сниппеты',text:'Сохранённые промпты в один клик.'},
 {target:'#hdSettings',title:'Настройки',text:'Полная настройка агента.'}
];
$('#tour').addEventListener('click',e=>{
  if(e.target.id==='tour'&&!e.target.closest('.tour-pop')){ stopTour() }
});
function showTour(){
  const t=$('#tour');if(!t)return;
  t.classList.add('on');
  const step=TOUR_STEPS[ui.tourStep];
  if(!step){ stopTour(); return }
  const el=$(step.target);
  if(!el){ ui.tourStep++; showTour(); return }
  const r=el.getBoundingClientRect();
  const hl=$('#tourHighlight');
  if(hl){
    hl.style.top=(r.top-6)+'px'; hl.style.left=(r.left-6)+'px';
    hl.style.width=(r.width+12)+'px'; hl.style.height=(r.height+12)+'px';
  }
  const pop=$('#tourPop');
  if(pop){
    pop.innerHTML='<h4><span class="step-n">'+(ui.tourStep+1)+'/'+TOUR_STEPS.length+'</span>'+step.title+'</h4>'+
      '<p>'+step.text+'</p><div class="tour-acts"><div class="tour-dots">'+
      TOUR_STEPS.map((_,i)=>'<span class="tour-dot'+(i===ui.tourStep?' on':'')+'"></span>').join('')+'</div>'+
      '<button class="btn sm ghost" id="tourSkip">Пропустить</button>'+
      (ui.tourStep>0?'<button class="btn sm" id="tourPrev">Назад</button>':'')+
      '<button class="btn primary sm" id="tourNext">'+(ui.tourStep===TOUR_STEPS.length-1?'Готово':'Далее')+'</button></div>';
    const left=Math.min(innerWidth-360,Math.max(10,r.left+r.width/2-170));
    pop.style.left=left+'px'; pop.style.top=(r.bottom+16)+'px';
  }
  const ts=$('#tourSkip'); if(ts)ts.onclick=stopTour;
  const tp=$('#tourPrev'); if(tp)tp.onclick=()=>{ui.tourStep--;showTour()};
  const tn=$('#tourNext'); if(tn)tn.onclick=()=>{ui.tourStep++;showTour()};
}
function stopTour(){ ui.tourOn=false; const t=$('#tour');if(t)t.classList.remove('on') }

/* HOTKEYS */
$('#btnMore').onclick=e=>{ e.stopPropagation(); const r=e.currentTarget.getBoundingClientRect();
  ctxMenu(r.left+r.width/2-112,r.top-8,[
    {icon:'fa-solid fa-plus',text:'Новая сессия',k:'Ctrl+Alt+N',onClick:()=>{newSessionWithChoice();openChat();toast('Создана','ok')}},
    {icon:'fa-solid fa-diagram-project',text:'Новый проект',k:'Ctrl+Alt+P',onClick:()=>createProject()},
    {icon:'fa-solid fa-folder-plus',text:'Новая папка',onClick:()=>createFolder()},
    '-',{label:'Сессии'},
    ...(state.sessions.slice(0,6).map(s=>({icon:'fa-regular fa-comment',text:s.title,onClick:()=>{openSession(s.id);openChat()}}))),
    '-',{icon:'fa-solid fa-clock-rotate-left',text:'История промптов',k:'Ctrl+H',onClick:()=>{openChat();openHistPop()}},
    {icon:'fa-solid fa-bookmark',text:'Шаблоны',onClick:()=>{openChat();openSet('templates')}},
    {icon:'fa-solid fa-terminal',text:'Команды',onClick:()=>openSet('commands')},
    {icon:S().general.theme==='dark'?'fa-solid fa-sun':'fa-solid fa-moon',text:'Сменить тему',k:'Ctrl+Shift+L',onClick:()=>$('#hdTheme').click()},
    {icon:'fa-solid fa-file-export',text:'Экспорт',onClick:()=>$('#expAll').click()},
    {icon:'fa-solid fa-graduation-cap',text:'Тур',onClick:()=>{openChat();$('#hdTour').click()}},
    '-',{icon:'fa-solid fa-gear',text:'Настройки',k:'Ctrl+,',onClick:()=>openSet()}
  ].filter(Boolean)) };

$('#undoBtn').onclick=undo;
$('#redoBtn').onclick=redo;

addEventListener('keydown',e=>{
  const typing=/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);

  // Esc
  if(e.key==='Escape'){
    if(ui.focusMode){ ui.focusMode=false; const chat=$('#chat');if(chat)chat.classList.remove('focus-mode');
      const fp=$('#focusPill');if(fp)fp.style.display='none';
      const fe=$('#focusExit');if(fe)fe.classList.remove('show'); return }
    const ctx=$('#ctx'); if(ctx&&ctx.classList.contains('open'))return closeCtx();
    const po=$('#pinOverlay'); if(po&&po.classList.contains('open'))return;
    const tour=$('#tour'); if(tour&&tour.classList.contains('on'))return stopTour();
    const pvo=$('#previewOv'); if(pvo&&pvo.classList.contains('open'))return closePreview();
    const fo=$('#formOverlay'); if(fo&&fo.classList.contains('open'))return closeForm(null);
    const hp=$('#histPop'); if(hp&&hp.classList.contains('open')){hp.classList.remove('open');return}
    if(ui.generating){ui.abort=true;ttsStop();return}
    if(ui.setOpen)return closeSet(); if(ui.chatOpen)return closeChat();
  }

  // Ctrl+F - поиск в сессии
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='f'&&ui.chatOpen&&!typing){
    e.preventDefault();
    openSessSearch();
    return;
  }

  // Ctrl+H - история промптов
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='h'&&!typing){
    e.preventDefault();
    if(!ui.chatOpen) openChat();
    setTimeout(openHistPop,100);
    return;
  }

  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openChat();setTimeout(()=>{const si=$('#searchInput');if(si)si.focus()},220);return}
  if((e.ctrlKey||e.metaKey)&&e.altKey&&e.key.toLowerCase()==='n'){e.preventDefault();newSession();openChat();return}
  if((e.ctrlKey||e.metaKey)&&e.altKey&&e.key.toLowerCase()==='p'){e.preventDefault();createProject();return}
  if((e.ctrlKey||e.metaKey)&&e.key===','){e.preventDefault();openSet();return}
  if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.key.toLowerCase()==='l'){e.preventDefault();$('#hdTheme').click();return}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'&&!e.shiftKey&&!typing){e.preventDefault();undo();return}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'&&e.shiftKey&&!typing){e.preventDefault();redo();return}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='y'&&!typing){e.preventDefault();redo();return}
  if(e.code==='Space'&&!typing&&!ui.spaceHeld&&S().voice.pushToTalk){
    const fo=$('#formOverlay');
    if(fo&&!fo.classList.contains('open')){
      e.preventDefault(); ui.spaceHeld=true; if(!recActive)startRec('stage')
    }
  }
});
addEventListener('keyup',e=>{ if(e.code==='Space'&&ui.spaceHeld){ ui.spaceHeld=false; if(recActive)stopRec(true) } });

// Клик вне попов
document.addEventListener('click',e=>{
  if(!e.target.closest('#snipPop')&&!e.target.closest('#cSnip')){const sp=$('#snipPop');if(sp)sp.classList.remove('open')}
  if(!e.target.closest('#pomoPop')&&!e.target.closest('#hdPomo')){const pp=$('#pomoPop');if(pp)pp.classList.remove('open')}
});
