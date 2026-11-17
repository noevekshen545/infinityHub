/* 04-render.js — Дерево проекта, помодоро, фокус, поиск */
/* ============================================================
   RENDER TREE
   ============================================================ */
function filterSessions(list){
  const q=ui.query.trim().toLowerCase();
  let out=list;
  if(ui.filter==='pinned') out=out.filter(s=>s.pinned);
  if(ui.filter==='archived') out=out.filter(s=>s.archived);
  if(ui.filter==='bookmarked') out=out.filter(s=>s.bookmarks&&s.bookmarks.length);
  if(ui.filter==='today'){ const t=new Date(); t.setHours(0,0,0,0); out=out.filter(s=>s.updatedAt>=t.getTime()) }
  if(!q) return out;
  return out.filter(s=>s.title.toLowerCase().includes(q)||s.messages.some(m=>plain(m.content).toLowerCase().includes(q)));
}
function renderTree(){
  const t=$('#tree'); if(!t)return;
  const q=ui.query.trim().toLowerCase();
  if(q){ t.innerHTML=renderSearch(q); return }
  const pinned=filterSessions(state.sessions).filter(s=>s.pinned&&!s.archived);
  const archived=state.sessions.filter(s=>s.archived);
  const bookmarked=filterSessions(state.sessions).filter(s=>s.bookmarks&&s.bookmarks.length&&!s.archived);
  const inProj=id=>filterSessions(state.sessions).filter(s=>s.projectId===id);
  const inFol=id=>filterSessions(state.sessions).filter(s=>s.folderId===id&&!s.projectId);
  const loose=filterSessions(state.sessions).filter(s=>!s.projectId&&!s.folderId&&!s.archived);
  /* Лимит «последних сессий» применяем только к группе «Сессии».
     Избранное, закладки, проекты, папки и архив не режем — иначе
     закреплённая сессия исчезала бы из списка молча. */
  const limit=Number(S().general.recentSessions)||0;
  let shown=loose, hiddenCount=0;
  if(limit>0&&loose.length>limit){
    /* Сортируем по активности: state.sessions может лежать в произвольном
       порядке, а «последние» — это недавние по времени, не первые в массиве. */
    shown=[...loose].sort((a,b)=>(b.updatedAt||0)-(a.updatedAt||0)).slice(0,limit);
    hiddenCount=loose.length-shown.length;
  }
  let h='';
  if(pinned.length) h+=group('pinned','Избранное','fa-thumbtack',pinned.length,pinned.map(sessionRow).join(''));
  if(bookmarked.length&&ui.filter==='all') h+=group('bookmarked','С закладками','fa-bookmark',bookmarked.length,bookmarked.map(sessionRow).join(''));
  h+=group('projects','Проекты','fa-diagram-project',state.projects.length,
    state.projects.length?state.projects.map(p=>projRow(p,inProj(p.id))).join('')
    :'<div class="row" style="cursor:default"><div class="rt"><small>Нет проектов</small></div></div>');
  h+=group('folders','Папки','fa-folder',state.folders.length,
    state.folders.length?state.folders.map(f=>folderRow(f,inFol(f.id))).join('')
    :'<div class="row" style="cursor:default"><div class="rt"><small>Нет папок</small></div></div>');
  h+=group('sessions','Сессии','fa-clock-rotate-left',loose.length,shown.length?shown.map(sessionRow).join(''):
    '<div class="row" style="cursor:default"><div class="rt"><small>Нет сессий</small></div></div>');
  /* Подсказка со счётчиком: показываем, что список урезан, и даём вернуть
     все сессии, не заставляя искать настройки. */
  if(hiddenCount>0){
    h+='<div class="row" style="cursor:pointer" id="showAllSessions"><span class="ric"><i class="fa-solid fa-ellipsis"></i></span>'+
       '<span class="rt"><small>Ещё '+hiddenCount+' — показать все</small></span></div>';
  }
  if(archived.length) h+=group('archived','Архив','fa-box-archive',archived.length,archived.map(sessionRow).join(''));
  t.innerHTML=h;
  t.classList.toggle('bulk-mode',ui.bulkMode);
}
function group(k,title,icon,n,inner){
  const col=ui.expanded[k]===false;
  return '<div class="grp'+(col?' collapsed':'')+'" data-g="'+k+'">'+
    '<button class="grp-head" data-gt="'+k+'"><i class="fa-solid fa-chevron-down chev"></i><i class="fa-solid '+icon+'" style="font-size:10px"></i>'+title+'<span class="n">'+n+'</span></button>'+
    '<div class="grp-items">'+inner+'</div></div>';
}
function sessionRow(s){
  const on=s.id===state.activeSessionId, p=projectOf(s);
  const checked=ui.bulkSel.has(s.id);
  const tags=(s.tags||[]).slice(0,2).map(t=>'<span class="tag-mini">'+esc(t)+'</span>').join('');
  const hasBookmarks=s.bookmarks&&s.bookmarks.length;
  return '<div class="row'+(on?' on':'')+(s.archived?' archived':'')+'" data-sess="'+s.id+'" draggable="true">'+
    '<span class="check'+(checked?' on':'')+'" data-chk="'+s.id+'"><i class="fa-solid fa-check"></i></span>'+
    '<span class="ric"><i class="fa-regular fa-comment'+(on?'s':'')+'"></i></span>'+
    '<span class="rt"><b>'+esc(s.title)+'</b><small>'+
      (p?'<i class="fa-solid fa-folder-open"></i>'+esc(p.name)+' · ':'')+
      (s.messages.length?s.messages.length+' сообщ. · ':'')+ago(s.updatedAt)+
      (hasBookmarks?' <i class="fa-solid fa-bookmark bookmark-mini"></i>':'')+tags+'</small></span>'+
    (s.pinned?'<i class="fa-solid fa-thumbtack pin"></i>':'')+
    (s.locked?'<i class="fa-solid fa-lock lock"></i>':'')+
    '<button class="more" data-smore="'+s.id+'"><i class="fa-solid fa-ellipsis"></i></button></div>';
}
function projRow(p,list){
  const on=p.id===state.activeProjectId;
  return '<div class="row proj'+(on?' on':'')+'" data-proj="'+p.id+'">'+
    '<span class="ric"><i class="fa-solid fa-diagram-project"></i></span>'+
    '<span class="rt"><b>'+esc(p.name)+'</b><small><i class="fa-solid fa-location-dot"></i>'+esc(p.path)+'</small></span>'+
    '<button class="more" data-pmore="'+p.id+'"><i class="fa-solid fa-ellipsis"></i></button></div>'+
    (list.length?'<div class="nested">'+list.map(sessionRow).join('')+'</div>':'');
}
function folderRow(f,list){
  return '<div class="row folder" data-folder="'+f.id+'">'+
    '<span class="ric"><i class="fa-solid fa-folder"></i></span>'+
    '<span class="rt"><b>'+esc(f.name)+'</b><small>'+list.length+' сес.</small></span>'+
    '<button class="more" data-fmore="'+f.id+'"><i class="fa-solid fa-ellipsis"></i></button></div>'+
    (list.length?'<div class="nested">'+list.map(sessionRow).join('')+'</div>':'');
}
$('#tree').addEventListener('click',e=>{
  const chk=e.target.closest('[data-chk]');
  if(chk&&ui.bulkMode){ e.stopPropagation(); const id=chk.dataset.chk;
    if(ui.bulkSel.has(id))ui.bulkSel.delete(id); else ui.bulkSel.add(id); updBulk(); renderTree(); return }
  const gt=e.target.closest('[data-gt]'); if(gt){ ui.expanded[gt.dataset.gt]=ui.expanded[gt.dataset.gt]===false; renderTree(); return }
  const sm=e.target.closest('[data-smore]'); if(sm){ e.stopPropagation(); sessionCtx(sm.dataset.smore,sm); return }
  const pm=e.target.closest('[data-pmore]'); if(pm){ e.stopPropagation(); projectCtx(pm.dataset.pmore,pm); return }
  const fm=e.target.closest('[data-fmore]'); if(fm){ e.stopPropagation(); folderCtx(fm.dataset.fmore,fm); return }
  const rs=e.target.closest('[data-res]'); if(rs){ const [sid,mid]=rs.dataset.res.split('|'); openSession(sid,mid); return }
  /* «Показать все» снимает лимит на время сессии и сохраняет выбор,
     чтобы он не вернулся после перезагрузки страницы. */
  const sa=e.target.closest('#showAllSessions');
  if(sa){ S().general.recentSessions=0; save(); bindSettings(); renderAll(); toast('Показаны все сессии','ok'); return }
  const s=e.target.closest('[data-sess]'); if(s){ openSession(s.dataset.sess); return }
  const p=e.target.closest('[data-proj]'); if(p){ state.activeProjectId=p.dataset.proj; save(); renderAll(); }
});

/* Перетаскивание сессии в папку. В bulk-режиме тянем сразу всё выделенное,
   в обычном — одну сессию под курсором. */
(()=>{
  const tree=$('#tree'); if(!tree)return;
  tree.addEventListener('dragstart',e=>{
    const row=e.target.closest('[data-sess]'); if(!row)return;
    const one=row.dataset.sess;
    const ids=(ui.bulkMode&&ui.bulkSel.size)?Array.from(ui.bulkSel):[one];
    e.dataTransfer.setData('text/plain',JSON.stringify(ids));
    e.dataTransfer.effectAllowed='move';
    row.classList.add('dragging');
  });
  tree.addEventListener('dragend',e=>{
    const row=e.target.closest('[data-sess]'); if(row)row.classList.remove('dragging');
    $$('.row.drop-ok',tree).forEach(r=>r.classList.remove('drop-ok'));
  });
  tree.addEventListener('dragover',e=>{
    const f=e.target.closest('[data-folder]');
    if(!f||!e.dataTransfer.types.includes('text/plain'))return;
    e.preventDefault(); e.dataTransfer.dropEffect='move';
    if(!f.classList.contains('drop-ok'))f.classList.add('drop-ok');
  });
  tree.addEventListener('dragleave',e=>{
    const f=e.target.closest('[data-folder]');
    if(f)f.classList.remove('drop-ok');
  });
  tree.addEventListener('drop',e=>{
    const f=e.target.closest('[data-folder]'); if(!f)return;
    e.preventDefault(); e.stopPropagation();
    f.classList.remove('drop-ok');
    let ids=[];
    try{ ids=JSON.parse(e.dataTransfer.getData('text/plain')||'[]') }catch(_){ return }
    if(Array.isArray(ids)&&ids.length) moveSessionsToFolder(ids,f.dataset.folder);
  });
})();

function updBulk(){
  const bb=$('#bulkBar'), bc=$('#bulkCount');
  if(bb) bb.classList.toggle('on',ui.bulkMode);
  if(bc) bc.textContent=ui.bulkSel.size;
}
$('#bulkBtn').onclick=()=>{ ui.bulkMode=!ui.bulkMode; ui.bulkSel.clear(); updBulk(); renderTree() };
$('#bulkCancel').onclick=()=>{ ui.bulkMode=false; ui.bulkSel.clear(); updBulk(); renderTree() };
$('#bulkDel').onclick=async()=>{
  if(!ui.bulkSel.size)return;
  if(!await askForm({title:'Удалить '+ui.bulkSel.size+' сессий?',icon:'fa-trash',ok:'Удалить',fields:[{name:'c',label:'Подтвердите',value:'удалить'}]}))return;
  const ids=[...ui.bulkSel];
  ids.forEach(id=>{state.sessions=state.sessions.filter(s=>s.id!==id)});
  if(ids.includes(state.activeSessionId)) state.activeSessionId=state.sessions[0]?.id||null;
  ui.bulkMode=false; ui.bulkSel.clear(); updBulk(); save(); renderAll();
  toast('Удалено: '+ids.length,'ok');
};
$('#bulkArchive').onclick=()=>{
  ui.bulkSel.forEach(id=>{const s=state.sessions.find(x=>x.id===id);if(s)s.archived=!s.archived});
  ui.bulkMode=false; ui.bulkSel.clear(); updBulk(); save(); renderAll(); toast('Архив обновлён','ok');
};
/* Пакетное перемещение выбранных сессий в папку */
$('#bulkFolder').onclick=e=>{
  if(!ui.bulkSel.size){ toast('Сначала выделите сессии','info'); return }
  const ids=Array.from(ui.bulkSel);
  ui.bulkMode=false; ui.bulkSel.clear(); updBulk(); renderTree();
  folderPickerMenu(e.currentTarget,ids);
};


function sessionCtx(id,el){
 const s=state.sessions.find(x=>x.id===id); if(!s)return;
 const r=el.getBoundingClientRect();
 ctxMenu(r.left,r.bottom+6,[
 {icon:'fa-regular fa-pen-to-square',text:'Переименовать',onClick:()=>renameSession(s)},
 {icon:'fa-solid fa-folder',text:s.folderId?'Переместить в другую папку':'Переместить в папку',
  onClick:()=>folderPickerMenu(el,[s.id])},
 {icon:'fa-solid fa-bookmark',text:'Закладки',onClick:()=>{
 ui.bookmarksOpen=!ui.bookmarksOpen;
 const bp=$('#bookmarksPanel'); if(bp) bp.classList.toggle('open',ui.bookmarksOpen);
 if(ui.bookmarksOpen) renderBookmarks();
 }},
 {icon:'fa-solid fa-list-check',text:'Извлечь задачи',onClick:()=>extractSessionTasks()},
 {icon:'fa-solid fa-magnifying-glass',text:'Поиск в сессии',k:'Ctrl+F',onClick:()=>openSessSearch()},
 '-',
 {icon:s.pinned?'fa-solid fa-thumbtack':'fa-regular fa-thumbtack',text:s.pinned?'Из избранного':'В избранное',
 onClick:()=>{s.pinned=!s.pinned;save();renderAll()}},
 {icon:s.locked?'fa-solid fa-lock-open':'fa-solid fa-lock',text:s.locked?'Снять PIN':'Задать PIN',
 onClick:async()=>{
 if(s.locked){ const ok=await askPin('verify','PIN'); if(!ok){toast('Неверный','err');return} s.locked=false; }
 else { if(!state.pin){ const ok=await askPin('set','Задайте PIN'); if(!ok)return } s.locked=true; }
 save();renderAll();
 }},
 '-',
 {icon:s.archived?'fa-solid fa-box-open':'fa-solid fa-box-archive',text:s.archived?'Из архива':'В архив',
 onClick:()=>{s.archived=!s.archived;save();renderAll()}},
 {icon:'fa-solid fa-tags',text:'Теги…',onClick:async()=>{
 const v=await askForm({title:'Теги (через запятую)',icon:'fa-tags',ok:'Сохранить',fields:[{name:'t',label:'Теги',value:(s.tags||[]).join(', ')}]});
 if(v&&v.t!==undefined){s.tags=v.t.split(',').map(x=>x.trim()).filter(Boolean);save();renderAll()}}},
 {icon:'fa-regular fa-copy',text:'Дублировать',onClick:()=>{const c={...structuredClone(s),id:uid('s'),title:s.title+' (копия)',
 createdAt:Date.now(),updatedAt:Date.now()};state.sessions.unshift(c);save();renderAll();toast('Копия','ok')}},
 {icon:'fa-solid fa-file-export',text:'Экспорт',onClick:()=>exportSession(s,'txt')},
 '-',{icon:'fa-regular fa-trash-can',text:'Удалить',danger:true,onClick:async()=>{
 if(await askForm({title:'Удалить сессию?',icon:'fa-triangle-exclamation',ok:'Удалить',
 fields:[{name:'x',label:'Подтвердите',value:s.title}]})) killSession(s.id)}}
 ]);
}
function projectCtx(id,el){
  const p=state.projects.find(x=>x.id===id); if(!p)return; const r=el.getBoundingClientRect();
  ctxMenu(r.left,r.bottom+6,[
    {icon:'fa-regular fa-pen-to-square',text:'Переименовать',onClick:async()=>{
      const v=await askForm({title:'Проект',icon:'fa-diagram-project',ok:'Сохранить',fields:[
        {name:'name',label:'Название',value:p.name},{name:'path',label:'Путь',type:'path',value:p.path}]});
      if(v){p.name=v.name||p.name;p.path=v.path||p.path;p.absPath=v.path||p.absPath||p.path;save();renderAll()}}},
    {icon:'fa-solid fa-folder-tree',text:'Указать папку на диске',onClick:async()=>{
      if(IS_FILE_PROTOCOL){ toast('Выбор папки не работает через file://','err'); return }
      const h=await pickDir(); if(!h)return;
      const abs=await resolveDirAbs(h.name); if(!abs)return;
      p.path=abs; p.absPath=abs; ui.handles[p.id]=h;
      p.files=await listDir(h); fsb.path=null;
      save(); renderAll(); toast('Папка проекта: '+p.path,'ok')}},
    {icon:'fa-solid fa-rotate',text:'Пересканировать папку',onClick:async()=>{
      const h=ui.handles[p.id];
      if(h){ p.files=await listDir(h) }
      fsb.path=null; save(); renderAll(); toast('Структура обновлена','ok')}},
    {icon:'fa-solid fa-plus',text:'Новая сессия',onClick:()=>{newSession({projectId:p.id});toast('Создана','ok')}},
    '-',{icon:'fa-regular fa-trash-can',text:'Удалить проект',danger:true,onClick:()=>{
      state.projects=state.projects.filter(x=>x.id!==p.id);
      state.sessions.forEach(s=>{if(s.projectId===p.id)s.projectId=null});
      if(state.activeProjectId===p.id)state.activeProjectId=null; save(); renderAll(); toast('Удалён','ok')}}
  ]);
}
function folderCtx(id,el){
  const f=state.folders.find(x=>x.id===id); if(!f)return; const r=el.getBoundingClientRect();
  ctxMenu(r.left,r.bottom+6,[
    {icon:'fa-regular fa-pen-to-square',text:'Переименовать',onClick:()=>renameFolder(f)},
    {icon:'fa-solid fa-plus',text:'Новая сессия',onClick:()=>{newSession({folderId:f.id})}},
    '-',{icon:'fa-regular fa-trash-can',text:'Удалить',danger:true,onClick:()=>deleteFolder(f)}
  ]);
}
async function renameSession(s){
  const v=await askForm({title:'Переименовать',icon:'fa-comment',ok:'Сохранить',fields:[{name:'t',label:'Название',value:s.title}]});
  if(v&&v.t){const old=s.title;s.title=v.t;save();renderAll();
    pushHistory(async()=>{s.title=v.t;save();renderAll()},async()=>{s.title=old;save();renderAll()})}
}
function exportSession(s,format='txt'){
  const name=(s.title||'session').replace(/[^\wа-яё -]/gi,'');
  if(format==='json'){
    dl(new Blob([JSON.stringify(s,null,2)],{type:'application/json'}),name+'.json');
  } else if(format==='md'){
    const lines=['# '+s.title,'','*'+new Date(s.createdAt).toLocaleString('ru-RU')+'*','','---',''];
    s.messages.forEach(m=>{
      const who=m.role==='user'?'**Вы**':m.role==='agent'?'**Агент**':'_Система_';
      lines.push('### '+who+' · '+tShort(m.ts));
      lines.push(''); lines.push(plain(m.content)); lines.push('');
    });
    dl(new Blob([lines.join('\n')],{type:'text/markdown'}),name+'.md');
  } else {
    const lines=['# '+s.title,''];
    s.messages.forEach(m=>{ const who=m.role==='user'?'Вы':m.role==='agent'?'Агент':'Система';
      lines.push('['+tShort(m.ts)+'] '+who+': '+plain(m.content)); });
    dl(new Blob([lines.join('\n')],{type:'text/plain'}),name+'.txt');
  }
  toast('Экспорт: '+format.toUpperCase(),'ok');
}
function dl(blob,name){ const u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=name;a.click();
  setTimeout(()=>URL.revokeObjectURL(u),1500) }

function renderSearch(q){
  const out=[];
  state.sessions.forEach(s=>{
    if(s.title.toLowerCase().includes(q)) out.push({s,m:null,sc:2});
    s.messages.forEach(m=>{ const c=plain(m.content).toLowerCase(); const i=c.indexOf(q);
      if(i>=0&&m.kind!=='system') out.push({s,m,sc:1,i}); });
  });
  out.sort((a,b)=>b.sc-a.sc||(b.s?.updatedAt||0)-(a.s?.updatedAt||0));
  const items=out.slice(0,40).map(o=>{
    if(!o.m) return '<div class="res" data-res="'+o.s.id+'|"><span class="ric"><i class="fa-solid fa-comment-dots"></i></span>'+
      '<div><b>'+hl(o.s.title,q)+'</b><small>'+ago(o.s.updatedAt)+'</small></div></div>';
    const txt=plain(o.m.content), st=Math.max(0,o.i-45);
    return '<div class="res" data-res="'+o.s.id+'|'+o.m.id+'"><span class="ric"><i class="fa-solid fa-magnifying-glass"></i></span>'+
      '<div><b>'+esc(o.s.title)+'</b><p>'+(st>0?'…':'')+hl(txt.slice(st,o.i+q.length+65),q)+'</p>'+
      '<small>'+(o.m.role==='user'?'Вы':'Агент')+' · '+tDate(o.m.ts)+'</small></div></div>';
  }).join('');
  return items||'<div class="empty"><i class="fa-solid fa-magnifying-glass"></i><p>Ничего не найдено</p></div>';
}
const hl=(s,q)=>esc(s).replace(new RegExp('('+q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','ig'),'<mark>$1</mark>');
$('#searchInput').addEventListener('input',e=>{ ui.query=e.target.value;
  const sw=$('#searchWrap'); if(sw) sw.classList.toggle('has',!!ui.query); renderTree(); });
$('#searchClear').onclick=()=>{ const si=$('#searchInput'); if(si){si.value='';ui.query=''} const sw=$('#searchWrap'); if(sw)sw.classList.remove('has'); renderTree() };

$('#filters').addEventListener('click',e=>{
  const b=e.target.closest('button[data-f]'); if(!b)return;
  ui.filter=b.dataset.f;
  $$('#filters button').forEach(x=>x.classList.toggle('on',x===b));
  renderTree();
});

/* ============================================================
   POMODORO
   ============================================================ */
function pomoTick(){
  if(!ui.pomo.running) return;
  ui.pomo.seconds--;
  if(ui.pomo.seconds<=0){
    if(ui.pomo.mode==='focus'){
      S().pomodoro.todaySessions++;
      S().pomodoro.totalSessions++;
      S().pomodoro.totalMinutes+=S().pomodoro.focus;
      save();
      beep(880,.2);
      setTimeout(()=>beep(880,.2),250);
      toast('Фокус завершён! Время перерыва','ok');
      ui.pomo.mode='break';
      ui.pomo.seconds=S().pomodoro.break*60;
    } else {
      beep(660,.2);
      toast('Перерыв окончен. К работе!','ok');
      ui.pomo.mode='focus';
      ui.pomo.seconds=S().pomodoro.focus*60;
      ui.pomo.running=false;
      $('#pomoStart').innerHTML='<i class="fa-solid fa-play"></i> Старт';
    }
    updatePomoUI();
    return;
  }
  updatePomoUI();
}
function updatePomoUI(){
  const mm=String(Math.floor(ui.pomo.seconds/60)).padStart(2,'0');
  const ss=String(ui.pomo.seconds%60).padStart(2,'0');
  const t=mm+':'+ss;
  const pt=$('#pomoTime'); if(pt) pt.innerHTML=t+'<span class="lbl">'+(ui.pomo.mode==='focus'?'Фокус':'Перерыв')+'</span>';
  const pp=$('#pomoPill'); if(pp){
    if(ui.pomo.running||ui.pomo.seconds<S().pomodoro.focus*60){ pp.style.display='';
      pp.classList.toggle('break',ui.pomo.mode==='break');
      const ppt=$('#pomoPillTime'); if(ppt) ppt.textContent=t;
    } else pp.style.display='none';
  }
  const td=$('#pomoToday'); if(td) td.textContent=S().pomodoro.todaySessions;
  const tt=$('#pomoTotal'); if(tt) tt.textContent=S().pomodoro.totalSessions;
  const hh=$('#pomoHours'); if(hh) hh.textContent=(S().pomodoro.totalMinutes/60).toFixed(1);
}
$('#hdPomo').onclick=e=>{
  e.stopPropagation();
  const pop=$('#pomoPop');
  if(pop) pop.classList.toggle('open');
};
$('#pomoStart').onclick=()=>{
  if(ui.pomo.running){
    ui.pomo.running=false;
    clearInterval(ui.pomo.intervalId);
    $('#pomoStart').innerHTML='<i class="fa-solid fa-play"></i> Старт';
  } else {
    ui.pomo.running=true;
    ui.pomo.intervalId=setInterval(pomoTick,1000);
    $('#pomoStart').innerHTML='<i class="fa-solid fa-pause"></i> Пауза';
  }
};
$('#pomoPause').onclick=()=>{
  if(ui.pomo.running){
    ui.pomo.running=false;
    clearInterval(ui.pomo.intervalId);
    $('#pomoStart').innerHTML='<i class="fa-solid fa-play"></i> Старт';
  }
};
$('#pomoReset').onclick=()=>{
  ui.pomo.running=false;
  clearInterval(ui.pomo.intervalId);
  ui.pomo.mode='focus';
  ui.pomo.seconds=S().pomodoro.focus*60;
  $('#pomoStart').innerHTML='<i class="fa-solid fa-play"></i> Старт';
  updatePomoUI();
};
// сброс дневной статистики
setInterval(()=>{
  const today=new Date().toDateString();
  if(S().pomodoro.todayDate!==today){
    S().pomodoro.todayDate=today;
    S().pomodoro.todaySessions=0;
    save();
  }
},60000);

/* ============================================================
   FOCUS MODE
   ============================================================ */
$('#hdFocus').onclick=()=>{
  ui.focusMode=!ui.focusMode;
  const chat=$('#chat'); if(chat) chat.classList.toggle('focus-mode',ui.focusMode);
  const fp=$('#focusPill'); if(fp) fp.style.display=ui.focusMode?'':'none';
  const fe=$('#focusExit'); if(fe) fe.classList.toggle('show',ui.focusMode);
  if(ui.focusMode){ toast('Фокус-режим. Esc для выхода','info') }
};
$('#focusExit').onclick=()=>{
  ui.focusMode=false;
  const chat=$('#chat'); if(chat) chat.classList.remove('focus-mode');
  const fp=$('#focusPill'); if(fp) fp.style.display='none';
  const fe=$('#focusExit'); if(fe) fe.classList.remove('show');
};

/* ============================================================
 SEARCH IN SESSION (Ctrl+F)
 ============================================================ */
function openSessSearch(){
 const ss=$('#sessSearch'); if(ss){ss.classList.add('on'); setTimeout(()=>{const inp=$('#sessSearchInput');if(inp)inp.focus()},50);}
}
$('#sessSearchClose').onclick=closeSessSearch;
function closeSessSearch(){
  const ss=$('#sessSearch'); if(ss) ss.classList.remove('on');
  const inp=$('#sessSearchInput'); if(inp) inp.value='';
  ui.sessSearch={query:'',matches:[],idx:-1};
  renderStream();
}
$('#sessSearchInput').addEventListener('input',e=>{
  ui.sessSearch.query=e.target.value.trim().toLowerCase();
  doSessSearch();
});
$('#sessSearchInput').addEventListener('keydown',e=>{
  if(e.key==='Enter'){
    e.preventDefault();
    if(e.shiftKey) sessSearchNav(-1);
    else sessSearchNav(1);
  }
  if(e.key==='Escape') closeSessSearch();
});
$('#sessSearchPrev').onclick=()=>sessSearchNav(-1);
$('#sessSearchNext').onclick=()=>sessSearchNav(1);
function doSessSearch(){
  const s=active(); const q=ui.sessSearch.query;
  if(!s||!q){ ui.sessSearch.matches=[]; ui.sessSearch.idx=-1; updateSessSearchCnt(); renderStream(); return }
  const matches=[];
  s.messages.forEach((m,i)=>{
    if(m.kind==='system') return;
    const txt=plain(m.content).toLowerCase();
    let pos=0, idx;
    while((idx=txt.indexOf(q,pos))!==-1){
      matches.push({msgIdx:i,offset:idx});
      pos=idx+1;
    }
  });
  ui.sessSearch.matches=matches;
  ui.sessSearch.idx=matches.length?0:-1;
  updateSessSearchCnt();
  renderStream();
  if(matches.length){ setTimeout(()=>scrollToCurrentMatch(),50); }
}
function updateSessSearchCnt(){
  const cnt=$('#sessSearchCnt'); if(!cnt) return;
  if(!ui.sessSearch.query){cnt.textContent='—';return}
  cnt.textContent=(ui.sessSearch.idx+1)+' / '+ui.sessSearch.matches.length;
}
function sessSearchNav(dir){
  if(!ui.sessSearch.matches.length)return;
  ui.sessSearch.idx=(ui.sessSearch.idx+dir+ui.sessSearch.matches.length)%ui.sessSearch.matches.length;
  updateSessSearchCnt();
  renderStream();
  setTimeout(()=>scrollToCurrentMatch(),50);
}
function scrollToCurrentMatch(){
  if(ui.sessSearch.idx<0)return;
  const m=ui.sessSearch.matches[ui.sessSearch.idx];
  const msg=active()?.messages[m.msgIdx];
  if(!msg)return;
  const el=$('.msg[data-id="'+msg.id+'"]');
  if(el){ el.scrollIntoView({block:'center',behavior:'smooth'});
    el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),1500); }
}
