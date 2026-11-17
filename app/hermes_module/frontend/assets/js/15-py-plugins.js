/* PY-ПЛАГИНЫ (Настройки → Плагины → Python-плагины)
   Файлы .py лежат в той же папке, что и .js, но исполняются на сервере:
   поэтому список и правка идут через /api/pyplugins/*, а не через PluginBus.
   Состояние (включён/выключен) сервер хранит сам — в .pyplugins-disabled.json,
   поэтому переживает перезапуск и не зависит от localStorage браузера. */

const PYP={list:[],commands:[],events:[],errors:{},loaded:false,busy:false};

const PY_SAMPLE=[
'# @name: Счётчик сообщений',
'# @desc: Считает сообщения и показывает тост при старте',
'# @version: 1.0',
'def register(api):',
'    n = api.store_get("count", 0)',
'    api.log("счётчик начинал с %d" % n)',
'',
'    def on_message(payload):',
'        # payload — словарь с текстом сообщения; вернённое значение',
'        # подставляется дальше, поэтому None отменяет обработку.',
'        api.store_set("count", api.store_get("count", 0) + 1)',
'        return payload',
'',
'    api.on("message", on_message)',
'    api.register_command("count", lambda arg: str(api.store_get("count", 0)),',
'                         "Показать счётчик сообщений")',
'    api.register_route("count", lambda q: {"count": api.store_get("count", 0)})',
'    api.toast("Счётчик сообщений подключён")',
''].join('\n');

function renderPyPlugins(){
  const list=$('#pyPluginList'); if(!list)return;
  if(!PYP.loaded){
    list.innerHTML='<div class="empty" style="padding:18px"><i class="fa-solid fa-spinner fa-spin"></i><p>Загрузка…</p></div>';
    return;
  }
  if(!PYP.list.length){
    list.innerHTML='<div class="empty" style="padding:26px"><i class="fa-brands fa-python"></i>'+
      '<p>Python-плагинов нет. Создайте .py в папке плагинов — сервер подхватит его сам.</p></div>';
    return;
  }
  /* Поиск общий для обоих видов: строка в поле фильтрует и .py.
     Без этого переключатель «Python» показывал бы все подряд. */
  const q=String((S().plugins&&S().plugins.query)||'').trim().toLowerCase();
  const hit=p=>!q||[p.name,(p.meta||{}).name,(p.meta||{}).desc,p.name+'.py']
    .some(x=>String(x||'').toLowerCase().indexOf(q)>=0);
  const list2=PYP.list.filter(hit);
  const errNames=Object.keys(PYP.errors||{});
  list.innerHTML=list2.length?list2.map(p=>{
    const file=p.name+'.py';
    const on=p.enabled!==false;
    const err=p.error||PYP.errors[p.name]||'';
    const meta=p.meta||{};
    const status=on?'<span class="badge done"><i class="fa-solid fa-bolt"></i> активен</span>'
      :'<span class="badge wait"><i class="fa-solid fa-pause"></i> выключен</span>';
    const bits=[];
    if(meta.version)bits.push('<span class="ver">v'+esc(meta.version)+'</span>');
    bits.push('<span class="chip"><i class="fa-brands fa-python"></i>'+esc(file)+'</span>');
    if(p.commands&&p.commands.length)bits.push('<span class="chip"><i class="fa-solid fa-terminal"></i>'+
      p.commands.length+' '+esc(p.commands.length===1?'команда':'команды')+'</span>');
    if(p.events&&p.events.length)bits.push('<span class="chip"><i class="fa-solid fa-bolt"></i>'+
      p.events.length+' '+esc(p.events.length===1?'событие':'событий')+'</span>');
    if(p.routes&&p.routes.length)bits.push('<span class="chip"><i class="fa-solid fa-route"></i>'+
      p.routes.length+' маршрут'+esc(p.routes.length===1?'':'а')+'</span>');
    return '<div class="plugcard'+(on?'':' off')+(err?' err':'')+'" data-pyp="'+esc(p.name)+'">'+
      '<div class="pc-top">'+
        '<span class="pi"><i class="fa-brands fa-python"></i></span>'+
        '<div class="pt"><b>'+esc(meta.name||p.name)+'</b>'+
          '<small>'+esc(meta.desc||'Без описания')+'</small></div>'+
        status+
      '</div>'+
      '<div class="pc-meta">'+bits.join('')+'</div>'+
      (err?'<div class="pc-err">'+esc(err)+'</div>':'')+
      '<div class="pc-act">'+
        '<div class="toggle'+(on?' on':'')+'" data-pyp-toggle="'+esc(p.name)+'" title="'+(on?'Отключить':'Включить')+'"></div>'+
        '<button class="icon-btn sm" data-pyp-edit="'+esc(p.name)+'" title="Редактировать код"><i class="fa-solid fa-pen"></i></button>'+
        '<button class="icon-btn sm" data-pyp-rel="'+esc(p.name)+'" title="Перечитать с диска"><i class="fa-solid fa-rotate"></i></button>'+
        '<button class="icon-btn sm" data-pyp-del="'+esc(p.name)+'" title="Удалить файл"><i class="fa-regular fa-trash-can"></i></button>'+
      '</div></div>';
  }).join('')
   :(PYP.list.length?'<div class="empty" style="padding:26px"><i class="fa-solid fa-magnifying-glass"></i>'+
     '<p>Python-плагины не найдены по запросу «'+esc(q)+'»</p></div>'
    :'<div class="empty" style="padding:26px"><i class="fa-brands fa-python"></i>'+
     '<p>Python-плагинов нет. Создайте .py в папке плагинов — сервер подхватит его сам.</p></div>')
  +(errNames.length&&!list2.some(p=>PYP.errors[p.name])
    ?'<div class="pc-err" style="margin-top:8px">Не загружены: '+esc(errNames.join(', '))+'</div>':'');
}

async function loadPyPlugins(){
  if(PYP.busy)return;
  PYP.busy=true;
  try{
    const r=await API.req('GET','/api/pyplugins/list');
    PYP.list=r.plugins||[]; PYP.commands=r.commands||[]; PYP.events=r.events||[];
    PYP.errors=r.errors||{}; PYP.loaded=true;
    renderPyPlugins();
  }catch(e){
    PYP.loaded=true;
    const box=$('#pyPluginList');
    if(box)box.innerHTML='<div class="empty" style="padding:18px">'+
      '<i class="fa-solid fa-triangle-exclamation"></i><p>Python-плагины недоступны</p>'+
      '<small>'+esc(errText(e&&e.message||e))+'</small></div>';
  }finally{ PYP.busy=false }
}

async function pyWrite(name,code){
  const r=await API.req('POST','/api/pyplugins/write',{name:name+'.py',code});
  PYP.errors=(r.reload&&r.reload.errors)||{};
  await loadPyPlugins();
  return r;
}

async function editPyPlugin(name){
  const ex=PYP.list.find(p=>p.name===name);
  let code='';
  try{ const r=await API.req('GET','/api/pyplugins/read?name='+encodeURIComponent(name+'.py')); code=r.code||'' }
  catch(e){ if(name) toast('Не удалось прочитать: '+errText(e&&e.message||e),'err'); }
  if(ex&&!code) code='';
  const v=await askForm({title:(ex?'Изменить':'Новый')+' Python-плагин',icon:'fa-brands fa-python',ok:'Сохранить',fields:[
    {name:'name',label:'Имя файла (без .py)',value:name||'moy_plagin',ph:'moy_plagin'},
    {name:'code',label:'Код',type:'area',value:code,ph:'# @name: …\ndef register(api):\n    ...'}]});
  if(!v)return;
  const fname=String(v.name||'').trim().replace(/\.py$/i,'').replace(/[^\w\- ]/g,'');
  if(!fname){ toast('Имя файла не задано','err'); return }
  try{
    await pyWrite(fname,String(v.code||''));
    toast('Python-плагин сохранён','ok');
  }catch(e){ toast('Не сохранён: '+errText(e&&e.message||e),'err') }
}

async function showPyLogs(){
  let d={};
  try{ d=await API.req('GET','/api/pyplugins/logs') }
  catch(e){ toast('Журнал недоступен: '+errText(e&&e.message||e),'err'); return }
  const logs=(d.logs||[]).slice(-40).map(l=>'['+(l.plugin||'')+'] '+l.text).join('\n');
  const errs=Object.keys(d.errors||{}).length
    ?'\n\nОшибки:\n'+Object.keys(d.errors).map(k=>k+': '+d.errors[k]).join('\n'):'';
  const box=$('#pvBody'), ov=$('#previewOv');
  if(ov&&box){ openPreview({filename:'Журнал Python-плагинов',content:logs||'Журнал пуст'}); ov.classList.add('open') }
  if(errs) toast('Есть ошибки — откройте консоль сервера','err');
}

function bindPyPlugins(){
  const list=$('#pyPluginList'); if(!list)return;
  list.onclick=async e=>{
    const tg=e.target.closest('[data-pyp-toggle]');
    if(tg){ const n=tg.dataset.pypToggle;
      const p=PYP.list.find(x=>x.name===n);
      try{ await API.req('POST','/api/pyplugins/toggle',{name:n+'.py',enabled:!(p&&p.enabled!==false)});
        await loadPyPlugins();
        toast('Плагин '+(p&&p.enabled===false?'включён':'выключен'),'info');
      }catch(err){ toast('Не получилось: '+errText(err&&err.message||err),'err') }
      return;
    }
    const ed=e.target.closest('[data-pyp-edit]');
    if(ed){ editPyPlugin(ed.dataset.pypEdit); return }
    const dl=e.target.closest('[data-pyp-del]');
    if(dl){ const n=dl.dataset.pypDel;
      const v=await askForm({title:'Удалить '+n+'.py?',icon:'fa-trash-can',ok:'Удалить',
        fields:[{name:'c',label:'Введите имя для подтверждения',value:n}]});
      if(!v)return;
      try{ const r=await API.req('POST','/api/pyplugins/delete',{name:n+'.py'});
        PYP.errors=(r.reload&&r.reload.errors)||{}; await loadPyPlugins();
        toast('Удалено','ok') }
      catch(err){ toast('Не удалось удалить: '+errText(err&&err.message||err),'err') }
      return;
    }
    if(e.target.closest('[data-pyp-rel]')){
      try{ await API.req('POST','/api/pyplugins/reload'); await loadPyPlugins();
        toast('Плагины перечитаны','ok') }
      catch(err){ toast('Не получилось: '+errText(err&&err.message||err),'err') }
    }
  };
  $('#pyPluginNew')&&($('#pyPluginNew').onclick=()=>editPyPlugin(''));
  $('#pyPluginSample')&&($('#pyPluginSample').onclick=async()=>{
    try{ await pyWrite('counter',PY_SAMPLE); toast('Пример создан: counter.py','ok') }
    catch(e){ toast('Не создан: '+errText(e&&e.message||e),'err') }
  });
  $('#pyPluginReload')&&($('#pyPluginReload').onclick=async()=>{
    try{ await API.req('POST','/api/pyplugins/reload'); await loadPyPlugins(); toast('Перечитано','ok') }
    catch(e){ toast('Не получилось: '+errText(e&&e.message||e),'err') }
  });
  $('#pyPluginLogs')&&($('#pyPluginLogs').onclick=showPyLogs);
  /* Вкладка «Плагины» открыта — дочитаем список .py один раз. */
  const nav=document.querySelector('.snav [data-p="plugins"]');
  if(nav&&!nav._pypBound){ nav._pypBound=true;
    nav.addEventListener('click',()=>setTimeout(()=>{ if(!PYP.loaded)loadPyPlugins() },60)) }
}
bindPyPlugins();

/* ---- Мост событий интерфейса → python-плагины -------------------
   JS-плагины получают события через PluginBus.emit. Python-плагины живут
   в другом процессе, поэтому шлём им те же события по HTTP: сервер
   раздаёт их подписчикам через pyplugins.emit, и плагин может вернуть
   значение, которым подменяется payload (как в .js). Ошибка шлётся
   молча — обрыв моста не должен мешать работать .js-плагинам. */
function pyEmit(event,payload){
  if(IS_FILE_PROTOCOL)return Promise.resolve(null);   /* сервера нет — некуда слать */
  return API.req('POST','/api/pyplugins/emit',{event,payload})
    .then(r=>{
      /* Плагин мог попросить показать сообщение — показываем. */
      (r&&r.toasts||[]).forEach(t=>toast(String(t.text||''),t.kind||'info'));
      return r?r.payload:null;
    })
    .catch(()=>null);
}

/* Подменяет текст сообщения, если python-плагин его вернул. */
function pyRewriteText(text){
  if(text==null||text==='')return Promise.resolve(text);
  return pyEmit('message',{text:String(text)})
    .then(v=>(v&&typeof v==='object'&&typeof v.text==='string')?v.text:text)
    .catch(()=>text);
}

function pyRunCommand(name,arg){
  if(IS_FILE_PROTOCOL)return Promise.resolve(null);
  return API.req('POST','/api/pyplugins/command',{name,arg:arg||''})
    .then(r=>(r&&r.toasts||[]).forEach(t=>toast(String(t.text||''),t.kind||'info'))||r)
    .catch(e=>{ toast('Команда '+name+': '+errText(e&&e.message||e),'err'); return null });
}

/* 'ready' шлём один раз после старта интерфейса. */
let pyReadySent=false;
function pyNotifyReady(){
  if(pyReadySent||IS_FILE_PROTOCOL)return;
  pyReadySent=true;
  pyEmit('ready',{app:'infinity'});
}
