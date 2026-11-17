/* ============ ПАЛИТРА КОМАНД (Ctrl+K / Cmd+K) ============
   Быстрый доступ к разделам, настройкам, сессиям и командам плагинов.
   Список пересобирается при каждом открытии — тогда подхватываются новые
   сессии и команды, зарегистрированные плагинами. */
const PALETTE_SECTIONS=[
  ['Общие','fa-solid fa-gear'],['Внешний вид','fa-solid fa-palette'],
  ['Голос','fa-solid fa-microphone-lines'],['Агент','fa-solid fa-infinity'],
  ['Инструменты','fa-solid fa-screwdriver-wrench'],['Плагины','fa-solid fa-plug'],
  ['Проекты','fa-solid fa-diagram-project'],['Крон-задачи','fa-solid fa-clock-rotate-left'],
  ['Команды','fa-solid fa-terminal'],['Шаблоны','fa-solid fa-bookmark'],
  ['Данные','fa-solid fa-database'],['Горячие клавиши','fa-regular fa-keyboard']
];
function paletteCollect(){
  const out=[];
  const sec=(label,icon,run)=>out.push({group:'Разделы',label,icon,run});
  sec('Чат','fa-solid fa-comments',()=>{ closePalette(); openChat() });
  for(const [name,icon] of PALETTE_SECTIONS)
    sec('Настройки → '+name,icon,()=>{ closePalette(); openSet(name.toLowerCase().indexOf('горячие')===0?'hotkeys':name.toLowerCase()) });
  const act=(label,icon,run)=>out.push({group:'Действия',label,icon,run});
  act('Новая сессия','fa-solid fa-plus',()=>{ closePalette(); newSession() });
  act('Новый проект','fa-solid fa-folder-plus',()=>{ closePalette(); createProject() });
  act('Очистить чат','fa-regular fa-trash-can',()=>{ closePalette();
    const s=active(); if(s){ s.messages=[]; save(); renderAll(); toast('Чат очищен','ok') } });
  act('Сохранить всё','fa-solid fa-floppy-disk',()=>{ closePalette(); save(); toast('Сохранено','ok') });
  act('Перезагрузить плагины','fa-solid fa-plug-circle-bolt',()=>{ closePalette(); PluginFS.sync() });
  act('Журнал Python-плагинов','fa-solid fa-scroll',()=>{
    closePalette(); if(typeof showPyLogs==='function')showPyLogs() });

  (state.sessions||[]).slice(0,40).forEach(s=>out.push({
    group:'Сессии',label:s.title||'Без названия',icon:'fa-regular fa-message',
    hint:tShort(s.updatedAt||s.createdAt||Date.now()),
    run:()=>{ closePalette(); state.activeSessionId=s.id; save(); renderAll() }}));

  /* Команды JS-плагинов: берём из смонтированных экземпляров (PluginBus
     держит их в instances, а не в отдельном списке id). */
  if(typeof PluginBus!=='undefined')for(const inst of (PluginBus.instances||[])){
    for(const c of ((inst.api&&inst.api._cmds)||[]))
      out.push({group:'JS-плагины',label:'/'+c.name,hint:c.desc||'',
        icon:'fa-solid fa-terminal',
        run:()=>{ closePalette(); const i=$('#input');
          if(i){ i.value='/'+c.name+' '; autoGrow(i); i.focus() } }});
  }
  /* Python-плагины: команды приходят с сервера вместе со списком */
  if(typeof PYP!=='undefined')for(const c of (PYP.commands||[]))
    out.push({group:'Python-плагины',label:'!'+c.name,hint:c.desc||'',
      icon:'fa-brands fa-python',
      run:()=>{ closePalette(); const i=$('#input');
        if(i){ i.value='!'+c.name+' '; autoGrow(i); i.focus() } }});
  return out;
}

let PALETTE={items:[],sel:0,open:false};
function paletteFilter(q){
  const s=String(q||'').trim().toLowerCase();
  if(!s)return PALETTE.items;
  return PALETTE.items.filter(it=>
    (it.label+' '+(it.hint||'')+' '+it.group).toLowerCase().indexOf(s)>=0);
}
function paletteRender(){
  const box=$('#palBody'), inp=$('#palInput');
  if(!box)return;
  const list=paletteFilter(inp?inp.value:'');
  if(PALETTE.sel>=list.length)PALETTE.sel=Math.max(0,list.length-1);
  const cnt=$('#palCount');
  if(cnt)cnt.textContent=list.length?list.length+' из '+PALETTE.items.length:'';
  box.innerHTML=list.length?list.map((it,i)=>
    '<div class="pal-row'+(i===PALETTE.sel?' sel':'')+'" data-pal="'+i+'">'+
      '<i class="'+(it.icon||'fa-solid fa-circle-dot')+'"></i>'+
      '<div class="txt"><b>'+esc(it.label)+'</b>'+
        (it.hint?'<small>'+esc(it.hint)+'</small>':'')+'</div>'+
      '<span class="chip">'+esc(it.group)+'</span>'+
    '</div>').join('')
   :'<div class="empty" style="padding:22px"><i class="fa-solid fa-magnifying-glass"></i>'+
    '<p>Ничего не найдено</p></div>';
  /* Выделенную строку держим в виду при длинном списке */
  const sel=box.querySelector('.pal-row.sel');
  if(sel&&sel.scrollIntoView)try{ sel.scrollIntoView({block:'nearest'}) }catch(e){}
}
function openPalette(){
  const ov=$('#palOv'); if(!ov)return;
  PALETTE.items=paletteCollect();
  PALETTE.sel=0; PALETTE.open=true;
  const inp=$('#palInput');
  if(inp){ inp.value=''; inp.focus() }
  ov.classList.add('open');
  paletteRender();
}
function closePalette(){
  const ov=$('#palOv'); if(ov)ov.classList.remove('open');
  PALETTE.open=false;
}
function paletteRun(i){
  const inp=$('#palInput');
  const list=paletteFilter(inp?inp.value:'');
  const it=list[i]; if(!it)return;
  try{ it.run() }catch(e){ console.warn('Палитра:',e); closePalette() }
}
function paletteMove(d){
  const inp=$('#palInput');
  const list=paletteFilter(inp?inp.value:'');
  if(!list.length)return;
  PALETTE.sel=(PALETTE.sel+d+list.length)%list.length;
  paletteRender();
}
(function bindPalette(){
  const ov=$('#palOv');
  if(!ov)return;
  const btn=$('#palBtn');
  if(btn)btn.onclick=()=>openPalette();
  ov.addEventListener('click',e=>{ if(e.target===ov)closePalette() });
  const close=$('#palClose');
  if(close)close.onclick=closePalette;
  const inp=$('#palInput');
  if(inp)inp.oninput=()=>{ PALETTE.sel=0; paletteRender() };
  const body=$('#palBody');
  if(body)body.onclick=e=>{
    const r=e.target.closest('[data-pal]');
    if(r)paletteRun(+r.dataset.pal);
  };
  document.addEventListener('keydown',e=>{
    const k=(e.key||'').toLowerCase();
    if((k==='k'||k==='к')&&(e.ctrlKey||e.metaKey)){
      e.preventDefault();
      PALETTE.open?closePalette():openPalette();
      return;
    }
    if(!PALETTE.open)return;
    if(e.key==='Escape'){ e.preventDefault(); closePalette(); return }
    if(e.key==='ArrowDown'){ e.preventDefault(); paletteMove(1); return }
    if(e.key==='ArrowUp'){ e.preventDefault(); paletteMove(-1); return }
    if(e.key==='Enter'){ e.preventDefault(); paletteRun(PALETTE.sel) }
  });

/* Кнопки окна в обёртке Electron: системной рамки нет (frame:false), поэтому
   сворачивание, разворачивание и закрытие рисует сам интерфейс. В обычном
   браузере панель скрыта — там рамка системная.

   Скрипт подключён ДО разметки #winBtns, поэтому на момент запуска элемента
   ещё нет в DOM: без ожидания DOMContentLoaded панель оставалась скрытой,
   а кнопки не появлялись. Ждём готовности документа — тогда порядок
   подключения не важен. */
function bindWindowButtons(){
  const box=$('#winBtns');
  if(!box)return;
  const api=window.electronAPI;
  if(!api)return;                     /* обычный браузер — не показываем */
  box.hidden=false;
  /* Ctrl+Shift+H и состояние скрытия. Общий модуль win_nav.js на странице
     агента может быть недоступен (у неё своя раздача ассетов), поэтому здесь
     есть собственная реализация: если модуль подключён — делегируем ему,
     иначе работаем сами. Флаг события не даёт сработать дважды. */
  const HIDE_KEY='infinity_win_nav_hidden';
  const navHidden=()=>{ try{ return localStorage.getItem(HIDE_KEY)==='1' }catch(e){ return false } };
  const applyNavHidden=(hide)=>{ box.hidden=false; box.style.display=hide?'none':''; box.setAttribute('aria-hidden',hide?'true':'false') };
  if(window.infinityWinNav){
    window.infinityWinNav.refresh();
  } else {
    applyNavHidden(navHidden());
    window.addEventListener('storage',e=>{ if(e.key===HIDE_KEY) applyNavHidden(navHidden()) });
    document.addEventListener('keydown',e=>{
      if(!(e.ctrlKey&&e.shiftKey))return;
      if(String(e.key||'').toLowerCase()!=='h')return;
      if(e.__infinityWinNav)return;
      e.__infinityWinNav=true;
      e.preventDefault();
      e.stopPropagation();
      const hide=!navHidden();
      try{ localStorage.setItem(HIDE_KEY,hide?'1':'0') }catch(err){ /* */ }
      applyNavHidden(hide);
      if(typeof toast==='function')toast(hide?'Кнопки окна скрыты · Ctrl+Shift+H — показать':'Кнопки окна показаны','info');
    }, true);
  }
  box.addEventListener('click',e=>{
    const b=e.target.closest('[data-win]'); if(!b)return;
    const a=b.dataset.win;
    if(a==='browser')api.openBrowser();
    else if(a==='minimize')api.minimize();
    else if(a==='maximize')api.maximize();
    else if(a==='close')api.close();
  });
  /* Двойной клик по шапкам разворачивает окно — привычное поведение. */
  $$('.m-head').forEach(h=>h.addEventListener('dblclick',e=>{
    if(e.target.closest('#winBtns'))return;
    if(e.target.closest('button,input,select,textarea'))return;
    api.maximize();
  }));
  /* Alt+Enter — оверлей поверх окон, как и на горячей клавише. */
  document.addEventListener('keydown',e=>{
    if(e.altKey&&e.key==='Enter'){ e.preventDefault(); api.toggleWindow() }
  });
}
if(document.readyState==='loading')
  document.addEventListener('DOMContentLoaded',bindWindowButtons);
else
  bindWindowButtons();

/* ---- Контекст из встроенного браузера ----
   Кнопка «в чат» в браузере присылает адрес, заголовок и текст страницы.
   Показываем это системным сообщением и кладём текст в поле ввода,
   чтобы пользователь мог сразу отредактировать вопрос. */
(function bindBrowserContext(){
  if(!window.electronAPI)return;
  window.electronAPI.on('browser-context',ctx=>{
    if(!ctx||!ctx.url)return;
    const s=active();
    if(s)msg(s,{role:'system',kind:'system',
      content:'Страница из браузера: '+(ctx.title||ctx.url)+' — '+ctx.url});
    const inp=$('#input');
    if(inp){
      inp.value='Посмотри страницу '+ctx.url
        +(ctx.title?' («'+ctx.title+'»)':'')
        +' и расскажи, что на ней важного.\n\n'
        +String(ctx.text||'').slice(0,4000);
      if(typeof autoGrow==='function')autoGrow(inp);
      inp.focus();
    }
    save(); renderAll();
    toast('Страница добавлена в чат','ok');
  });
})();


})();
