/* ============ СУБАГЕНТЫ ============
   Субагент — это делегация, которую агент передал отдельному агенту (delegate_task).
   Изначально мы показывали их отдельным блоком и без внутренностей: агент виден
   как обычный вызов инструмента, а что делал ребёнок — не видно.

   Теперь работу ребёнка показываем ВНУТРИ карточки delegate_task. Источник —
   события `subagent.*`, которые шлюз кладёт в тот же SSE, что и остальные события
   хода (gateway/platforms/api_server.py::_tool_progress). Движок шлёт их с полным
   набором идентификаторов (subagent_id, parent_id, depth, delegation_id), поэтому
   дерево собирается однозначно, в том числе для вложенных делегаций и пакетов.

   Блок «Субагенты» справа не удалён, но теперь он показывает только то, чего нет
   в карточках: делегации, начавшиеся до загрузки страницы (гидратация через
   /api/subagents) и фоновые, у которых карточки delegate_task уже нет. */

function subStatus(st){
  const s=String(st||'').toLowerCase();
  if(s==='running'||s==='dispatched'||s==='in_progress')return 'run';
  if(s==='stalling'||s==='stalled'||s==='rejected'||s==='error'||s==='failed')return 'err';
  if(s==='done'||s==='completed'||s==='finished'||s==='success')return 'ok';
  return 'wait';
}
const SUB_ST_TXT={run:['выполняется','fa-circle-notch fa-spin'],
                  ok:['готов','fa-check'],
                  err:['ошибка','fa-triangle-exclamation'],
                  wait:['ожидает','fa-clock']};

/* Человеческая длительность: «12 с» / «4 мин» / «1 ч 5 мин». */
function subDur(sec){
  const n=Number(sec);
  if(!isFinite(n)||n<0)return '';
  if(n<60)return Math.round(n)+' с';
  if(n<3600)return Math.round(n/60)+' мин';
  return Math.floor(n/3600)+' ч '+Math.round((n%3600)/60)+' мин';
}
function subCounters(d){
  const out=[];
  if(d.toolCount)out.push('вызовов: '+d.toolCount);
  if(d.apiCalls)out.push('вызовов API: '+d.apiCalls);
  if(d.model)out.push('модель: '+d.model);
  if(d.duration)out.push('время: '+subDur(d.duration));
  if(d.tokens)out.push('токенов: '+d.tokens);
  return out;
}

/* ===================== СБОР ДЕРЕВА ИЗ СОБЫТИЙ ===================== */

/* Идентификатор ветки. Движок присылает subagent_id; если его нет (старая ветка
   движка или пакет без id), составной ключ из delegation_id и номера задачи —
   он стабилен в пределах одной делегации и различает ветки пакета. */
function subKey(d){
  const id=d&&d.subagent_id;
  if(id)return String(id);
  const del=(d&&d.delegation_id)||'';
  return del?del+':'+((d&&d.task_index)||0):'';
}

/* Карточка-владелец веток: выполняющийся вызов инструмента. Пока родитель ждёт
   ребёнка, он не может вызвать другие инструменты, поэтому «последний
   выполняющийся вызов» — это ровно та делегация, к которой относится событие.
   Если вызова уже нет (фоновая делегация, перезагрузка страницы) — берём
   последний вызов вообще, а если и его нет, создаём карточку: событие не
   должно теряться только из-за того, что не нашлось родителя. */
function subsOwner(s,create){
  if(!s)return null;
  const tools=s.messages.filter(m=>m.kind==='tool');
  for(let i=tools.length-1;i>=0;i--){
    if(tools[i].status==='running')return tools[i];
  }
  if(create){
    if(tools.length)return tools[tools.length-1];
    return msg(s,{role:'agent',kind:'tool',content:'',status:'running',
      meta:{name:'delegate_task',icon:'fa-solid fa-users',preview:'Делегирование…',open:true,subs:[]}});
  }
  return null;
}
function subsList(m){
  if(!m)return null;
  if(!Array.isArray(m.meta.subs))m.meta.subs=[];
  return m.meta.subs;
}
/* Поиск ветки по ключу в дереве — нужен для вложенных делегаций (depth ≥ 1):
   родитель присылает свой subagent_id, и событие ребёнка должно попасть внутрь
   родительской ветки, а не в корень карточки. */
function subsFind(nodes,key){
  if(!Array.isArray(nodes))return null;
  for(const n of nodes){
    if(n.key===key)return n;
    const hit=subsFind(n.kids,key);
    if(hit)return hit;
  }
  return null;
}
function subsFindDeep(s,key){
  if(!s)return null;
  const tools=s.messages.filter(m=>m.kind==='tool');
  for(const m of tools){
    const hit=subsFind(subsList(m),key);
    if(hit)return hit;
  }
  return null;
}

function subNewNode(key,d){
  return {
    key:key, goal:String(d.goal||'').trim(), status:'run',
    model:d.model||'', depth:Number(d.depth||0),
    toolCount:Number(d.tool_count||0), apiCalls:0, tokens:0,
    duration:0, summary:'', error:'',
    tools:[], text:'', thinking:'', kids:[],
  };
}

/* Обработчик одного кадра subagent.*. Вызывается из 09-init-api.js потоком SSE.
   node — найденная или созданная ветка; false — событие к нам не относится. */
function onSubagentEvent(s,ev,d){
  if(!s||!d)return false;
  const key=subKey(d);
  if(!key)return false;

  /* Родитель: если движок назвал своего родителя и мы его уже знаем, ветка
     вкладывается в него (вложенная делегация). Иначе — в корень карточки. */
  let node=subsFindDeep(s,key);
  if(!node){
    const parentKey=(d.parent_id!=null&&d.parent_id!=='')?String(d.parent_id):'';
    const parent=parentKey?subsFindDeep(s,parentKey):null;
    const host=(parent&&parent.kids)||subsList(subsOwner(s,true));
    if(!host)return false;
    node=subNewNode(key,d);
    host.push(node);
  }

  if(ev==='subagent.start'||ev==='subagent.spawn_requested'){
    if(d.goal)node.goal=String(d.goal).trim();
    if(d.model)node.model=d.model;
    node.depth=Number(d.depth||node.depth||0);
  }
  else if(ev==='subagent.thinking'){
    /* Рассуждения ребёнка приходят потоком. Копим в буфер и показываем
       одним свёрнутым блоком — иначе карточка растёт на сотни строк. */
    const t=String(d.preview||'');
    if(t&&t!==node.thinking)node.thinking=(node.thinking?node.thinking+' ':'')+t;
  }
  else if(ev==='subagent.tool'){
    /* Собственный вызов инструмента ребёнка. Имя приходит в tool_name. */
    const name=String(d.tool_name||'').trim();
    if(name){
      node.tools.push({name:name,preview:String(d.preview||''),status:'run'});
      node.toolCount=Math.max(node.toolCount,node.tools.length);
    }
  }
  else if(ev==='subagent.progress'||ev==='subagent_progress'){
    /* Сводка пачки имён инструментов: движок копит их по 5 и присылает списком.
       Отдельной строкой не показываем — те же имена уже есть в .sub-tools. */
  }
  else if(ev==='subagent.complete'){
    const st=subStatus(d.status||'done');
    node.status=(st==='run')?'ok':st;
    if(d.summary)node.summary=String(d.summary);
    else if(d.preview&&!node.text)node.text=String(d.preview);
    if(d.duration_seconds!=null)node.duration=Number(d.duration_seconds);
    if(d.api_calls!=null)node.apiCalls=Number(d.api_calls);
    const tok=(Number(d.input_tokens||0)+Number(d.output_tokens||0));
    if(tok)node.tokens=tok;
    if(st==='err')node.error=String(d.summary||d.preview||'делегация не завершилась');
    /* Открытый вызов инструмента ребёнка, который не прислал завершения,
       помечаем выполненным: иначе он навсегда остался бы «выполняется». */
    for(const t of node.tools)if(t.status==='run')t.status='done';
  }
  return true;
}

/* Счётчики, которые движок шлёт вместе с complete, — в отдельных полях. */
function subApplyCounters(node,d){
  if(!node)return;
  if(d.api_calls!=null)node.apiCalls=Number(d.api_calls);
  const tok=Number(d.input_tokens||0)+Number(d.output_tokens||0);
  if(tok)node.tokens=tok;
}

/* ===================== ОТРИСОВКА ===================== */

/* Значок вызова инструмента — тот же набор, что у карточек наверху, чтобы
   чтение списка не требовало запоминания второго алфавита. */
function subToolIcon(n){
  const t=String(n||'').toLowerCase();
  if(t.includes('file')||t.includes('read')||t.includes('write'))return 'fa-solid fa-file-lines';
  if(t.includes('terminal')||t.includes('command')||t.includes('shell'))return 'fa-solid fa-terminal';
  if(t.includes('web')||t.includes('search')||t.includes('browser'))return 'fa-solid fa-globe';
  if(t.includes('code')||t.includes('python')||t.includes('exec'))return 'fa-solid fa-code';
  if(t.includes('image')||t.includes('vision'))return 'fa-regular fa-image';
  if(t.includes('memory')||t.includes('recall'))return 'fa-solid fa-brain';
  if(t.includes('delegate')||t.includes('agent'))return 'fa-solid fa-users';
  return 'fa-solid fa-screwdriver-wrench';
}
function subCut(s,n){ s=String(s||''); return s.length>n?s.slice(0,n)+'…':s }

function subTreeHTML(nodes,depth){
  if(!Array.isArray(nodes)||!nodes.length)return '';
  return '<div class="sub-tree">'+
    nodes.map(function(n){
      /* Ветки переживают перечитывание сессии из хранилища, поэтому записи,
         сделанные прошлой версией, могут не иметь части полей. Рендер не должен
         из-за этого падать — иначе пропадёт вся панель вызовов. */
      const tools=Array.isArray(n.tools)?n.tools:[];
      const kids=Array.isArray(n.kids)?n.kids:[];
      const st=subStatus(n.status);
      const txt=SUB_ST_TXT[st]||SUB_ST_TXT.wait;
      const open=subNodeOpen(n);
      const lines=subCounters(n);
      const goal=subCut(n.goal||'субагент',110);
      const title=n.goal?goal:(n.model||'субагент');
      let body='';
      if(n.thinking)body+='<div class="sub-think"><i class="fa-solid fa-brain"></i>'+
        '<div><div class="lbl">Рассуждения</div><pre>'+esc(subCut(n.thinking,1500))+'</pre></div></div>';
      if(tools.length){
        body+='<div class="sub-tools">'+tools.map(function(t){
          return '<div class="sub-tool '+(t.status==='run'?'run':'done')+'">'+
            '<i class="'+subToolIcon(t.name)+'"></i><span class="n">'+esc(t.name)+'</span>'+
            (t.preview?'<span class="pv">'+esc(subCut(t.preview,90))+'</span>':'')+'</div>';
        }).join('')+'</div>';
      }
      if(n.text)body+='<div class="sub-text"><div class="lbl">Ответ субагента</div><div>'+fmt(n.text)+'</div></div>';
      if(n.summary&&n.summary!==n.text)body+='<div class="sub-out"><div class="lbl">Итог</div><div>'+fmt(n.summary)+'</div></div>';
      if(n.error)body+='<div class="sub-out err"><div class="lbl">Ошибка</div><div>'+esc(n.error)+'</div></div>';
      if(kids.length)body+=subTreeHTML(kids,depth+1);
      return '<div class="sub-node '+st+' d'+depth+'" data-subkey="'+esc(n.key)+'">'+
        '<div class="sub-hd" data-sub-toggle="'+esc(n.key)+'">'+
          '<span class="si"><i class="fa-solid fa-user-astronaut"></i></span>'+
          '<div class="st"><b>'+esc(title)+'</b>'+
            (lines.length?'<small>'+esc(lines.join(' · '))+'</small>':'')+'</div>'+
          '<span class="badge '+st+'"><i class="fa-solid '+txt[1]+'"></i>'+txt[0]+'</span>'+
          '<i class="fa-solid fa-chevron-down sub-chev'+(open?' on':'')+'"></i>'+
        '</div>'+
        (open?'<div class="sub-body">'+body+'</div>':'')+
      '</div>';
    }).join('')+
  '</div>';
}

/* Раскрытие ветки храним рядом с карточками инструментов (ui.subOpen), а не в
   самой ветке: узлы пересоздаются при перечитывании сессии, и состояние не
   должно уезжать в сохранённый JSON. */
function subNodeOpen(n){
  return Object.prototype.hasOwnProperty.call(ui.subOpen||{},n.key)
    ? !!ui.subOpen[n.key] : true;
}

/* Вставка дерева в тело карточки инструмента — используется и панелью
   вызовов, и блоком в ленте чата, поэтому одна функция на оба места. */
function subTreeFor(m){
  const meta=(m&&m.meta)||{};
  if(!Array.isArray(meta.subs)||!meta.subs.length)return '';
  return subTreeHTML(meta.subs,0);
}

/* ===================== ОТДЕЛЬНЫЙ БЛОК (только «осиротевшие» ветки) ============ */

state.subPinOpen=false;
state.subagents=null;

/* /api/subagents отдаёт снимок реестра делегаций в собственной форме
   (delegation_id, goal, elapsed, api_calls…). Приводим её к форме узла дерева,
   чтобы рендер был один и тот же и для кадров SSE, и для гидрации. */
function subFromSnapshot(d){
  const goals=Array.isArray(d.goals)&&d.goals.length?d.goals:null;
  const goal=d.is_batch&&goals?('Пакет из '+goals.length+' задач'):String(d.goal||'');
  const node=subNewNode(String(d.delegation_id||''),d);
  node.goal=goal;
  if(goals)node.goal=(goal+' · '+goals.join(' · '));
  node.model=d.model||'';
  node.apiCalls=Number(d.api_calls||0);
  node.duration=Number(d.elapsed!=null?d.elapsed:d.duration_seconds||0);
  node.summary=String(d.summary||'');
  node.error=String(d.error||d.exit_reason||'');
  if(node.error)node.status='err';
  if(d.role)node.role=d.role;
  return node;
}
function subSnapshotItems(){
  const raw=(state.subagents&&state.subagents.items)||[];
  return (Array.isArray(raw)?raw:[]).map(subFromSnapshot);
}

/* Ветки, которые уже показаны внутри карточки delegate_task, повторять в
   отдельном блоке не нужно — иначе одна и та же работа видна дважды. */
function subShownKeys(s){
  const out={};
  if(!s)return out;
  for(const m of s.messages){
    if(m.kind!=='tool'||!Array.isArray(m.meta?.subs))continue;
    const walk=ns=>{ for(const n of ns||[]){ out[n.key]=1; walk(n.kids) } };
    walk(m.meta.subs);
  }
  return out;
}
function subOrphans(s){
  const shown=subShownKeys(s);
  const out=[];
  const walk=ns=>{ for(const n of ns||[]){
    if(shown[n.key])continue;
    /* Показываем только завершённые ветки: пока родительский вызов в панели
       есть, мы её уже показываем. */
    if(subStatus(n.status)!=='run')out.push(n);
    walk(n.kids);
  } };
  walk(subSnapshotItems());
  return out;
}

function renderSubagents(){
  const pin=$('#subPin'); if(!pin)return;
  const list=subOrphans(toolsOwner());
  if(!list.length){ pin.style.display='none'; return }
  const running=list.filter(n=>subStatus(n.status)==='run').length;
  const badge=$('#subPinBadge');
  if(badge)badge.style.display=running?'':'none';
  pin.style.display='';
  const t=$('#subPin .t span');
  if(t)t.textContent='Субагенты · '+list.length;
  const body=$('#subPinBody');
  if(body)body.innerHTML=subTreeHTML(list,0);
  pin.classList.toggle('open',!!state.subPinOpen);
  pin.classList.toggle('collapsed',!state.subPinOpen);
  pin.classList.toggle('live',running>0&&!state.subPinOpen);
}

/* Один запрос на сервер. Пока идёт — не начинаем новый, иначе при частых
   событиях SSE получим пачку лишних обращений. */
async function loadSubagents(){
  try{
    const r=await fetch('/api/subagents');
    if(!r.ok)throw new Error('HTTP '+r.status);
    state.subagents=await r.json();
  }catch(e){
    /* Движок делегаций может быть недоступен — это не поломка интерфейса,
       поэтому молча прячем блок, а не сыплем ошибками в консоль. */
    state.subagents={items:[]};
  }
}

/* Периодическое обновление, пока панель открыта: субагенты работают сами по
   себе, и их статус должен меняться без событий SSE. */
function subRefreshSoon(delay){
  clearTimeout(state._subTimer);
  state._subTimer=setTimeout(async()=>{
    await loadSubagents();
    renderSubagents();
    if(state.toolsPanelOpen)subRefreshSoon(4000);
  },delay||1200);
}
function refreshSubagents(){
  loadSubagents().then(()=>{ renderSubagents() });
}
const subPinHead=$('#subPinHead');
if(subPinHead)subPinHead.onclick=function(){
  state.subPinOpen=!state.subPinOpen; save();
  renderSubagents();
  if(state.subPinOpen)refreshSubagents();
};

/* Раскрытие/свертывание ветки (в карточке и в отдельном блоке). */
document.addEventListener('click',function(e){
  const hd=e.target.closest('[data-sub-toggle]');
  if(!hd)return;
  e.preventDefault();
  const key=hd.dataset.subToggle;
  ui.subOpen=ui.subOpen||{};
  ui.subOpen[key]=!subNodeOpen({key:key});
  renderToolsPanelSoon();
  renderSubagents();
});