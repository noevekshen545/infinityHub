/* 05-chat.js — Рендер чата, композер, отправка, консоль */
/* ============================================================
   RENDER CHAT
   ============================================================ */
function renderHead(){
  const s=active(); if(!s)return;
  const st=$('#sessTitle'); if(st) st.textContent=s.title;
  const p=projectOf(s); const chip=$('#sessProject');
  if(chip){
    if(p){chip.style.display='';chip.lastElementChild.textContent=p.name;chip.title=p.path}
    else chip.style.display='none';
  }
  const fp=$('#fPath'); if(fp){ if(p){fp.style.display='';fp.lastElementChild.textContent=p.path;} else fp.style.display='none'; }
  const sc=$('#sessCount'); if(sc) sc.innerHTML='<i class="fa-regular fa-message"></i> '+s.messages.filter(m=>m.kind!=='system').length;
  const sa=$('#sessArchive'); if(sa) sa.style.display=s.archived?'':'none';
  const fm=$('#fModel'); if(fm) fm.value=s.model||S().agent.model;
  const fmo=$('#fMode'); if(fmo) fmo.value=s.mode||S().agent.mode;
  const hs=$('#hdSpeak'); if(hs) hs.classList.toggle('on',S().voice.autoSpeak);
  const cs=$('#cSpeak'); if(cs) cs.classList.toggle('on',S().voice.autoSpeak);
  const hd=$('#hdDetails'); if(hd) hd.classList.toggle('on',!!state.detailsOpen);
  const hp=$('#hdPin'); if(hp){ hp.innerHTML='<i class="fa-solid fa-thumbtack"></i>'; hp.classList.toggle('on',!!s.pinned); }
  const hl=$('#hdLock'); if(hl){ hl.innerHTML='<i class="fa-solid fa-lock'+(s.locked?'':'-open')+'"></i>'; hl.classList.toggle('on',!!s.locked); }
  updCost();
}
function updCost(){
 const s=active(); if(!s)return;
 
 // Обновляем скорость генерации, потраченные токены и заполнение контекста
 const fSpeed=$('#fSpeed');
 const fContext=$('#fContext');
 if(!fSpeed||!fContext)return;
 
 /* Скорость считаем по скользящему окну в 5 секунд, а не от начала хода.
    Раньше elapsed считался от ui.lastTokenTime, который выставлялся один раз
    при старте, поэтому токены делились на всё прошедшее время — значение
    постоянно занижалось и выглядело как «токены не считаются». */
 const win=ui.tokWin||(ui.tokWin=[]);
 const now=Date.now();
 while(win.length&&now-win[0][0]>5000)win.shift();
 if(ui.generating&&win.length){
  const tps=win.reduce((a,b)=>a+b[1],0)/5;
  fSpeed.textContent=Math.max(1,Math.round(tps))+' tok/s';
 }else{
  fSpeed.textContent='— tok/s';
 }
 
 /* Всего токенов за сессию (входящие + исходящие). Это число считалось, но
    нигде не показывалось — добавляем его в подвал композера. */
 const fTok=$('#fTokens');
 if(fTok){
  const spent=(s.costIn||0)+(s.costOut||0);
  fTok.textContent=spent.toLocaleString('ru')+' ток.';
 }
 
 // Заполнение контекстного окна
 const model=MODELS.find(m=>m.id===(s.model||S().agent.model));
 if(model){
 const toks=Math.round(JSON.stringify(s.messages).length/3.4);
 const pct=Math.min(100,Math.round(toks/model.ctx*100));
 fContext.textContent=pct+'%';
 
 // Цветовая индикация
 if(pct>90)fContext.style.color='var(--err)';
 else if(pct>70)fContext.style.color='var(--warn)';
 else fContext.style.color='';
 }else{
 fContext.textContent='—%';
 }
}
/* --- Прокрутка ленты сообщений -------------------------------------------
   Раньше чат принудительно уходил вниз при КАЖДОМ обновлении: каждый
   чанк текста, каждый вызов инструмента, каждое рассуждение. Во время
   работы агента прочитать уже написанное было невозможно — лента
   дёргалась вниз. Теперь автопрокрутка работает только когда
   пользователь сам держит чат внизу (ui.stickBottom): прокрутил вверх —
   и чат больше не дёргается, а в углу появляется кнопка «вниз». */
const STICK_PX=60;
function isAtBottom(el,px){ return !el || (el.scrollHeight-el.scrollTop-el.clientHeight)<(px||STICK_PX) }
/* Прокрутка вниз. force=true — принудительно (отправка сообщения,
   открытие сессии, клик по кнопке «вниз»). */
function autoScrollBottom(el,force){
  el=el||$('#stream'); if(!el)return;
  if(force){ ui.stickBottom=true; el.scrollTop=el.scrollHeight; updScrollBtn(); return }
  if(!ui.stickBottom)return;
  el.scrollTop=el.scrollHeight;
}
/* Показать/спрятать плавающую кнопку «вниз». */
function updScrollBtn(){
  const b=$('#scrollBottom'); if(!b)return;
  b.classList.toggle('show',!ui.stickBottom);
}
/* Следим за положением пользователя: он ушёл вверх — автопрокрутка
   выключается, вернулся вниз — включается обратно. */
function initStreamScroll(){
  const el=$('#stream'); if(!el||el._stickBound)return;
  el._stickBound=true;
  el.addEventListener('scroll',function(){
    ui.stickBottom=isAtBottom(el);
    updScrollBtn();
  },{passive:true});
}
document.addEventListener('DOMContentLoaded',initStreamScroll);
if(document.readyState!=='loading')initStreamScroll();
document.addEventListener('click',function(e){
  const b=e.target.closest('#scrollBottom'); if(!b)return;
  const el=$('#stream'); if(!el)return;
  ui.stickBottom=true; el.scrollTop=el.scrollHeight; updScrollBtn();
});

/* Полная перерисовка ленты. Именно она вызывала мерцание: innerHTML
   уничтожал все узлы и создавал их заново, поэтому при каждом вызове
   инструмента (а он идёт посреди ответа агента) весь видимый текст
   на секунду исчезал и появлялся снова.

   Теперь рисуем так же, но переиспользуем DOM: сообщение, которого ещё
   нет, дописываем в конец; у которого изменилось состояние — обновляем
   точечно через patch(). Полную замену оставляем только когда список
   изменился структурно: смена сессии, поисковый запрос, скрытые сообщения. */
function renderStream(scroll){
  if(ui._inRenderStream)return;          /* защита от рекурсии renderStream->patch->renderStream */
  ui._inRenderStream=true;
  try{ renderStreamInner(scroll); }
  finally{ ui._inRenderStream=false }
}
function renderStreamInner(scroll){
  const s=active(),el=$('#stream'); if(!s||!el){if(el)el.innerHTML='';ui.renderedIds=null;ui.renderedSig=null;ui.renderedDay='';return}
  const st=$('#sessState');
  if(st){
    if(ui.generating){st.className='pill busy';st.innerHTML='<i class="fa-solid fa-circle-notch"></i> Выполняется'}
    else{st.className='pill live';st.innerHTML='<i class="fa-solid fa-circle-check"></i> Ожидание'}
  }
  // Фильтруем скрытые сообщения (meta.hidden=true)
  const visibleMessages=s.messages.filter(m=>!m.meta?.hidden);
  if(!visibleMessages.length){
    el.innerHTML='<div class="empty"><i class="fa-solid fa-infinity"></i>'+
      '<p>Опишите задачу — агент составит план и применит инструменты.</p>'+
      '<div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center">'+
      ['Проанализируй папку проекта','Составь план','Найди ошибки','Подготовь отчёт']
        .map(x=>'<button class="btn sm preset">'+x+'</button>').join('')+'</div></div>';
    ui.renderedIds=[]; ui.renderedSig=null; ui.renderedDay='';
    return;
  }
  /* Что уже нарисовано. Сессия и поисковый запрос меняют всю разметку,
     поэтому при их смене перерисовываем целиком — это редкий случай. */
  /* Ключ подписи включает и настройки отображения инструментов/рассуждений:
     переключатель обязан приводить к полной перерисовке. Иначе карточки
     вызовов оставались в чате после выключения и не появлялись после
     включения (список id от настройки не меняется). */
  const sig=s.id+'|'+(ui.sessSearch.query||'')+'|'
    +(S().agent.showToolCalls?'1':'0')+'|'+(S().agent.showReasoning?'1':'0');
  const ids=visibleMessages.map(m=>m.id);
  if(!(ui.renderedIds&&ui.renderedSig===sig&&el.childElementCount)){
    let h='',lastDay='';
    visibleMessages.forEach((m,idx)=>{
      const body=msgHTML(m,idx);
      /* Скрытые настройкой сообщения (вызовы инструментов, рассуждения)
         не рисуются вовсе — и разделитель даты для них не нужен.
         Раньше он печатался всё равно, и в чате появлялись «голые» даты
         перед каждым вызовом инструмента и каждым рассуждением. */
      if(!body)return;
      const d=tDate(m.ts); if(d!==lastDay){h+='<div class="daysep">'+d+'</div>';lastDay=d}
      h+=body;
    });
    const prevTop=el.scrollTop;
    el.innerHTML=h;
    ui.renderedIds=ids; ui.renderedSig=sig; ui.renderedDay=lastDay;
    if(scroll===false){ if(ui.stickBottom)el.scrollTop=el.scrollHeight; else el.scrollTop=prevTop }
    else autoScrollBottom(el);
    updScrollBtn();
    return;
  }
  const prev=ui.renderedIds;
  if(prev.length===ids.length&&prev.every((x,i)=>x===ids[i])){
    /* Набор сообщений не изменился — трогаем только то, что ещё «живёт»:
       стриминг текста, рассуждения и вызовы инструментов. */
    for(let i=0;i<visibleMessages.length;i++){
      const m=visibleMessages[i];
      if(m.status==='streaming'||m.kind==='thinking'||m.kind==='tool'){
        /* Патчить можно только то, что действительно нарисовано в ленте.
           Блоки рассуждений (agent.showReasoning) и вызовы инструментов
           (agent.showToolCalls) по умолчанию СКРЫТЫ: msgHTML() для них
           возвращает пустую строку, то есть узла [data-id] в DOM нет.
           Раньше patch() на таком сообщении не находил элемент и звал
           renderStream(), а тот патчил то же сообщение снова — бесконечная
           рекурсия и «Maximum call stack size exceeded». */
        if(streamShows(m))patch(m);
        else if(m.kind==='thinking')renderThinkPinSoon();   /* лента скрыта — пишем в панель */
      }
    }
    autoScrollBottom(el);
    return;
  }
  /* Сообщения добавились или исчезли. Новые дописываем в конец — старые
     остаются нетронутыми, поэтому текст, который читают, не перерисовывается. */
  const prevSet=new Set(prev);
  const keep=new Set(ids);
  /* Дата, на которой закончился уже нарисованный кусок ленты. Берём её
     из ui.renderedDay, а НЕ начинаем с пустой строки: раньше lastDay
     обнулялся на каждой дорисовке, поэтому перед каждым новым сообщением
     печаталась дата — даже когда день не менялся. */
  let lastDay=ui.renderedDay||'';
  let h='';
  for(let i=0;i<visibleMessages.length;i++){
    const m=visibleMessages[i];
    if(prevSet.has(m.id))continue;          /* уже нарисован — не трогаем */
    const body=msgHTML(m,i);
    if(!body)continue;                     /* скрыто настройкой — не рисуем */
    const d=tDate(m.ts);
    if(d!==lastDay){h+='<div class="daysep">'+d+'</div>';lastDay=d}
    h+=body;
  }
  if(h)el.insertAdjacentHTML('beforeend',h);
  ui.renderedDay=lastDay;
  /* Удалённые сообщения убираем, иначе они остались бы висеть в чате.
     У блоков (рассуждения, консоль, план) data-id стоит на внутреннем
     .blk, а обёртка .msg — без него: снимаем весь контейнер, чтобы не
     оставлять пустых «простыней» в ленте. */
  $$('#stream [data-id]').forEach(node=>{
    const id=node.getAttribute('data-id');
    if(id&&!keep.has(id)){
      const wrap=node.closest('.msg')||node;
      wrap.remove();
    }
  });
  ui.renderedIds=ids;
  autoScrollBottom(el);
}
function highlightText(text,q){
  if(!q) return esc(text);
  const escaped=esc(text);
  const re=new RegExp('('+q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','ig');
  return escaped.replace(re,'<mark>$1</mark>');
}
/* Кнопки подтверждения строятся из choices движка: 0.23 принимает только
   once/session/always/deny, самодельное «allow» отклоняется как invalid_params. */
const APPR_CHOICE_UI={
  once:['Разрешить','fa-check','primary sm'],
  session:['На сеанс','fa-clock','sm'],
  always:['Всегда','fa-check-double','sm'],
  deny:['Отклонить','fa-ban','danger sm'],
  allow:['Разрешить','fa-check','primary sm']
};
function apprChoicesHtml(m){
  const raw=(Array.isArray(m.meta?.choices)&&m.meta.choices.length)?m.meta.choices:['once','deny'];
  const seen={};
  return raw.filter(c=>APPR_CHOICE_UI[c]&&!seen[c]&&(seen[c]=1)).map(c=>{
    const u=APPR_CHOICE_UI[c];
    return '<button class="btn '+u[2]+'" data-appr="'+esc(c)+'" data-id="'+m.id+'">'+
      '<i class="fa-solid '+u[1]+'"></i> '+u[0]+'</button>';
  }).join('');
}
/* Вопрос агента: варианты кликом (одиночный — сразу ответ), либо текст.
   При multi_select выбор копится и уходит одной строкой через «Ответить». */
function clarifyBodyHtml(m){
  const md=m.meta||{}, ch=Array.isArray(md.choices)?md.choices:[];
  if(m.status==='done')return '';
  if(!ch.length)
    return '<div class="appr-acts clr-text"><input class="clr-input" id="clrInput_'+m.id+'" placeholder="Ваш ответ…">'+
      '<button class="btn primary sm" data-clr-send="'+m.id+'"><i class="fa-solid fa-paper-plane"></i> Ответить</button></div>';
  return '<div class="appr-acts clr-acts">'+ch.map((c,i)=>
      '<button class="btn sm" data-clr-pick="'+i+'" data-id="'+m.id+'">'+esc(c)+'</button>').join('')+'</div>'+
    (md.multi?'<div class="appr-acts"><button class="btn primary sm" data-clr-send="'+m.id+'">'+
      '<i class="fa-solid fa-check"></i> Ответить</button></div>':'');
}
function msgHTML(m,idx){
  const s=active();
  const q=ui.sessSearch.query;
  const isMatch=ui.sessSearch.matches.some(mm=>mm.msgIdx===idx);
  const isCur=ui.sessSearch.idx>=0&&ui.sessSearch.matches[ui.sessSearch.idx]?.msgIdx===idx;
  if(m.kind==='system')return '<div class="sysmsg" data-id="'+m.id+'"><i class="fa-solid fa-circle-info"></i>'+esc(m.content)+'</div>';
  if(m.kind==='thinking'){
    /* Блок рассуждений в чате скрыт по умолчанию. Сообщения kind:'thinking'
       продолжают создаваться и копиться в панели справа — скрыт только вид в чате. */
    if(!S().agent.showReasoning) return '';
    return blk(m,'think','fa-solid fa-brain','Рассуждение',m.meta?.open,
  '<div class="txt">'+(q?highlightText(m.content,q):fmt(m.content))+'</div>', m.status==='running'?'<span class="badge running"><i class="fa-solid fa-circle-notch fa-spin"></i> думаю</span>':'<span class="badge done">готово</span>');
  }
  /* Вызовы инструментов в чате скрыты по умолчанию: их видно в панели
     «Инструменты». Переключатель agent.showToolCalls показывает те же
     карточки в ленте — рендерер общий с панелью. */
  if(m.kind==='tool') return S().agent.showToolCalls ? toolCallHTML(m,toolOpen(m),true) : '';
  /* Результат перехватчика: тот же вид, что у консольной команды «!». */
  if(m.kind==='intercept'){
    const md=m.meta||{};
    const ist=m.status==='running';
    const bad=!ist&&md.ok===false;
    return blk(m,'shell','fa-solid fa-bolt','Команда: '+(md.name||'без фразы'),
      ist?true:(md.open||false),
      '<div class="lbl">'+esc(md.lang==='js'?'JavaScript':'Python')+'</div>'+shBody(m),
      ist?'<span class="badge running"><i class="fa-solid fa-circle-notch fa-spin"></i> выполняется</span>'
         :(bad?'<span class="badge error">ошибка</span>':'<span class="badge done">готово</span>'));
  }
 if(m.kind==='plan')return blk(m,'plan','fa-solid fa-list-check','План',true,
    '<div class="steps">'+(m.meta?.steps||[]).map(st=>'<div class="step '+st.s+'"><i class="fa-solid '+
      (st.s==='done'?'fa-check':st.s==='run'?'fa-circle-notch':st.s==='err'?'fa-xmark':'fa-clock')+'"></i><span>'+esc(st.t)+'</span></div>').join('')+
    '</div>','<span class="badge '+(m.status==='running'?'running':'done')+'">'+(m.meta?.steps||[]).filter(x=>x.s==='done').length+'/'+(m.meta?.steps||[]).length+'</span>');
  if(m.kind==='approval')return '<div class="msg"><div class="col"><div class="blk appr open" data-id="'+m.id+'">'+
    '<div class="blk-head"><span class="bi"><i class="fa-solid fa-shield-halved"></i></span><b>Подтверждение</b>'+
    '<span class="badge '+(m.status==='done'?'wait':'warn')+'">'+(m.status==='done'?(m.meta?.decision||'—'):'ожидает')+'</span></div>'+
    '<div class="appr-body" style="display:block"><p>'+esc(m.content)+'</p>'+
    (m.meta?.cmd?'<pre>'+esc(m.meta.cmd)+'</pre>':'')+
    (m.status!=='done'?'<div class="appr-acts">'+apprChoicesHtml(m)+'</div>':'')+'</div></div></div></div>';
  if(m.kind==='clarify')return '<div class="msg"><div class="col"><div class="blk appr open" data-id="'+m.id+'">'+
    '<div class="blk-head"><span class="bi"><i class="fa-solid fa-circle-question"></i></span><b>Вопрос агента</b>'+
    '<span class="badge '+(m.status==='done'?'wait':'warn')+'">'+(m.status==='done'?(m.meta?.answer||'отвечено'):'ждёт ответа')+'</span></div>'+
    '<div class="appr-body" style="display:block"><p>'+esc(m.content)+'</p>'+clarifyBodyHtml(m)+'</div></div></div></div>';

  // изображения
  if(m.kind==='shell')return blk(m,'shell','fa-solid fa-terminal','Консоль',true,shBody(m),
    '<span class="badge '+(m.status==='running'?'running':(Number(m.meta?.code)===0?'done':'error'))+'">'+
    (m.status==='running'?'выполняется':(Number(m.meta?.code)===0?'ok':('код '+m.meta?.code)))+'</span>');

  const imgs=(m.meta?.atts||[]).filter(a=>a.type&&a.type.startsWith('image')&&a.dataUrl).map(a=>
    '<img src="'+a.dataUrl+'" data-imgprev alt="'+esc(a.name)+'">').join('');
  const imgsBlock=imgs?'<div class="msg-imgs">'+imgs+'</div>':'';

  // reply
  const replyBlock=m.meta?.replyTo?'<div class="chip" style="margin-bottom:6px;opacity:.85"><i class="fa-solid fa-reply"></i> <span>'+esc(m.meta.replyTo.slice(0,80))+(m.meta.replyTo.length>80?'…':'')+'</span></div>':'';

  // реакции
  const rx=m.reactions||{};
  const rxBlock='<div class="rx">'+
    REACTIONS.filter(r=>rx[r.k]).map(r=>'<button class="on" data-rx="'+r.k+'" data-msg="'+m.id+'" title="'+r.label+'"><i class="'+r.icon+'"></i> '+rx[r.k]+'</button>').join('')+
    '<button class="rx-add" data-rx-add="'+m.id+'" title="Добавить реакцию"><i class="fa-solid fa-plus"></i></button></div>';

  const contentHtml = q ? highlightText(plain(m.content),q) : fmt(m.content);

  // редактирование
  if(ui.editMsg===m.id&&m.role==='user'){
    return '<div class="msg user" data-id="'+m.id+'"><span class="avatar"><i class="fa-solid fa-user"></i></span>'+
      '<div class="col"><div class="edit-wrap">'+
      '<textarea id="editText">'+esc(m.content)+'</textarea>'+
      '<div class="edit-acts">'+
      '<button class="btn sm ghost" id="editCancel">Отмена</button>'+
      '<button class="btn sm primary" id="editSave"><i class="fa-solid fa-check"></i> Сохранить и перегенерировать</button>'+
      '</div></div></div></div>';
  }

  // actions - разные для user/agent
  const userActs=m.role==='user'?'<div class="acts">'+
    '<button class="act" data-act="edit" data-id="'+m.id+'" title="Редактировать"><i class="fa-solid fa-pen"></i></button>'+
    '<button class="act" data-act="copy" data-id="'+m.id+'" title="Копировать"><i class="fa-regular fa-copy"></i></button>'+
    '<button class="act" data-act="reply" data-id="'+m.id+'" title="Ответить"><i class="fa-solid fa-reply"></i></button>'+
    '<button class="act" data-act="rewind" data-id="'+m.id+'" title="Откатить переписку до этого сообщения"><i class="fa-solid fa-clock-rotate-left"></i></button>'+
    '<button class="act" data-act="bookmark" data-id="'+m.id+'" title="В закладки"><i class="fa-solid fa-bookmark"></i></button>'+
    '</div>':
    '<div class="acts">'+
    '<button class="act" data-act="copy" data-id="'+m.id+'" title="Копировать"><i class="fa-regular fa-copy"></i></button>'+
    '<button class="act" data-act="preview" data-id="'+m.id+'" title="Предпросмотр файла из ответа"><i class="fa-regular fa-eye"></i></button>'+
    '<button class="act" data-act="speak" data-id="'+m.id+'" title="Озвучить"><i class="fa-solid fa-volume-high"></i></button>'+
    '<button class="act" data-act="apply" data-id="'+m.id+'" title="В поле"><i class="fa-solid fa-arrow-right-to-bracket"></i></button>'+
    '<button class="act" data-act="regen" data-id="'+m.id+'" title="Повторить"><i class="fa-solid fa-rotate-right"></i></button>'+
    '<button class="act" data-act="reply" data-id="'+m.id+'" title="Ответить"><i class="fa-solid fa-reply"></i></button>'+
    '<button class="act" data-act="rewind" data-id="'+m.id+'" title="Откатить переписку до этого сообщения"><i class="fa-solid fa-clock-rotate-left"></i></button>'+
    '<button class="act" data-act="bookmark" data-id="'+m.id+'" title="В закладки"><i class="fa-solid fa-bookmark"></i></button>'+
    '</div>';

  if(m.role==='user')return '<div class="msg user'+(isCur?' flash':'')+'" data-id="'+m.id+'"><span class="avatar"><i class="fa-solid fa-user"></i></span>'+
    '<div class="col">'+replyBlock+attsHTML(m.meta?.atts)+imgsBlock+'<div class="bubble"><p>'+contentHtml+'</p>'+(m.status==='streaming'?'<span class="caret"></span>':'')+'</div>'+rxBlock+
    '<div class="meta"><span class="time">'+tShort(m.ts)+'</span>'+userActs+'</div></div></div>';
  // Файлы, названные агентом: кликабельные чипы под ответом.
  // Пока сообщение стримится — не показываем, иначе чипы мельтешат на каждом чанке.
  const fileChips=(m.status!=='streaming')?(()=>{
    const found=collectFilePaths(m);
    /* Fakly iz instrumentov, kotorye agent ne upomyanal v tekste */
    const fromTools=createdFilesFromTools(active(),found);
    if(!fromTools.length)return fileChipsHtml(found);
    return '<div class="fchips-label">Создано агентом</div>'+
           fileChipsHtml(found)+fileChipsHtml(fromTools);
  })():'';
  return '<div class="msg agent'+(isCur?' flash':'')+'" data-id="'+m.id+'"><span class="avatar"><i class="fa-solid fa-infinity"></i></span>'+
    '<div class="col">'+replyBlock+imgsBlock+'<div class="bubble"><p>'+contentHtml+'</p>'+(m.status==='streaming'?'<span class="caret"></span>':'')+'</div>'+fileChips+rxBlock+
    '<div class="meta"><span class="time">'+tShort(m.ts)+(m.meta?.model?' · '+esc(m.meta.model):'')+'</span>'+userActs+'</div></div></div>';
}
function attsHTML(a){ if(!a||!a.length)return '';
  return '<div class="att">'+a.filter(x=>!x.type||!x.type.startsWith('image')).map(x=>'<span class="chip"><i class="fa-solid '+(x.type==='folder'?'fa-folder':'fa-file')+'"></i>'+
    '<span>'+esc(x.name)+'</span><span style="opacity:.6">'+fmtBytes(x.size)+'</span></span>').join('')+'</div>' }
function blk(m,cls,icon,title,open,body,badge){
  return '<div class="msg"><div class="col"><div class="blk '+cls+(open?' open':'')+'" data-id="'+m.id+'">'+
    '<div class="blk-head" data-tgl="'+m.id+'"><span class="bi"><i class="'+icon+'"></i></span><b>'+esc(title)+'</b>'+
    (badge||'')+'<span class="hint-t"></span><i class="fa-solid fa-chevron-down chev"></i></div>'+
    '<div class="blk-body">'+body+'</div></div></div></div>';
}
/* Тело блока «Консоль»: вывод команды, код возврата, кнопки «в поле»/«копировать» */
function shBody(m){
  const meta=m.meta||{};
  const head=meta.cwd?'<div style="opacity:.6;font-size:11px;margin-bottom:6px">'+esc(meta.cwd)+'</div>':'';
  const codeTxt=m.status==='running'?'':('код '+Number(meta.code||0)+' · '+(meta.dur||0)+' c'+(meta.truncated?' · вывод усечён':''));
  return head+'<div class="txt"><pre style="margin:0;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Consolas,\'Courier New\',monospace;font-size:12.5px;line-height:1.5;overflow:auto;max-height:420px">'+esc(m.content||'')+'</pre>'+
    '<div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'+
    (codeTxt?'<span style="font-size:11px;opacity:.65">'+codeTxt+'</span>':'')+
    '<button class="btn sm" data-shsend="'+m.id+'"><i class="fa-solid fa-arrow-right-to-bracket"></i> В поле</button>'+
    '<button class="btn sm ghost" data-shcopy="'+m.id+'"><i class="fa-regular fa-copy"></i> Копировать</button>'+
    '</div></div>';
}
/* Рисуется ли сообщение в ленте чата. Рассуждения и вызовы инструментов
   по умолчанию скрыты (agent.showReasoning / agent.showToolCalls), и для них
   msgHTML() отдаёт пустую строку — узла [data-id] в DOM не появляется.
   Функция используется перед patch(), чтобы не искать отсутствующий узел. */
function streamShows(m){
  if(!m)return false;
  if(m.kind==='thinking')return !!S().agent.showReasoning;
  if(m.kind==='tool')return !!S().agent.showToolCalls;
  return true;
}
function patch(m){ 
 /* Узла в ленте нет — значит его там и не должно быть: блок рассуждений или
    вызов инструмента скрыт настройкой (agent.showReasoning / showToolCalls).
    Звать renderStream() в этом случае нельзя: тот снова дойдёт до patch()
    этого же сообщения — получится бесконечная рекурсия.

    Но панель «Рассуждения» справа показывает поток независимо от ленты, и
    раньше обновлялась именно здесь. Поэтому для скрытого рассуждения
    обновляем панель и выходим; для скрытого вызова инструмента делать
    нечего — его поток рисует renderToolsPanel(). */
 if(!streamShows(m)){
  if(m.kind==='thinking')renderThinkPinSoon();
  return;
 }
  const el=$('.blk[data-id="'+m.id+'"]')||$('.msg[data-id="'+m.id+'"]')||$('.tool-call-item[data-id="'+m.id+'"]');
  if(!el){
   if(ui._inRenderStream)return;
   renderStream();
   /* pervyyiy blok rassuzhdeniy eshche ne otrysovan - obnovlyaem i panel vyzovov,
      inache tekst poyavitsya tam tolko sleduyushhey porcii soobshheniy */
   if(m.kind==='thinking')renderThinkPinSoon();
   return;}
 
  /* Карточка вызова инструмента в ленте чата (.tool-call-item). Раньше
     узел искался только по .blk/.msg, а карточка инструмента не имела
     ни data-id, ни .blk-body — из-за чего patch() промахивался и
     перерисовывал весь чат целиком, а карточка оставалась «выполняется». */
  if(m.kind==='tool'&&el.classList.contains('tool-call-item')){
   const meta=m.meta||{};
   const body=el.querySelector('.tool-call-body');
   /* Тело перерисовываем только когда оно реально изменилось — иначе
      карточка «моргала» при каждом обновлении ленты. */
   const key=JSON.stringify([meta.preview||'',meta.input||'',meta.output||'',meta.subs||'']);
   if(body&&el._bodyKey!==key){ body.innerHTML=toolCardBody(m); el._bodyKey=key }
   const st=m.status==='running'?'running':m.status==='error'?'error':'done';
   el.classList.remove('running','error','done'); el.classList.add(st);
   const badge=el.querySelector('.badge');
   if(badge){
    badge.className='badge '+st;
    badge.textContent=st==='running'?'выполняется':st==='error'?'ошибка':'готово';
   }
   const dur=el.querySelector('.tool-dur');
   const durTxt=meta.dur?String(meta.dur):'';
   if(dur){ dur.textContent=durTxt; dur.style.display=durTxt?'':'none' }
   else if(durTxt){ const sp=document.createElement('span'); sp.className='tool-dur';
    sp.style.fontSize='9px'; sp.style.color='var(--text-3)'; sp.textContent=durTxt;
    const ch=el.querySelector('.tool-call-head'); if(ch)ch.insertBefore(sp,ch.querySelector('.chev')); }
   return;
  }
 
 // Для текстовых сообщений обновляем только содержимое bubble
 const bubble=el.querySelector('.bubble');
 if(bubble&&(m.kind==='text'||m.role==='agent'&&!m.kind)){
 const content=bubble.querySelector('p')||bubble;
 content.innerHTML=fmt(m.content).replace(/^<p>|<\/p>$/g,'')+(m.status==='streaming'?'<span class="caret"></span>':'');
 return;
 }
 
 // Для thinking блоков
 const tb=el.querySelector('.blk-body'); 
 if(tb&&m.kind==='thinking'){
 tb.innerHTML='<div class="txt">'+fmt(m.content)+'</div>';
 /* Панель вызовов инструментов показывает тот же поток рассуждений —
    обновляем её с троттлингом, иначе текст там отставал от чата. */
 renderThinkPinSoon();

 return;
 }
 
 // Для tool блоков
 // Для блоков консоли («!команда»)
 if(tb&&m.kind==='shell'){
 tb.innerHTML=shBody(m);
 const sb=el.querySelector('.badge');
 if(sb){ sb.className='badge '+(m.status==='running'?'running':(Number(m.meta?.code)===0?'done':'error'));
 sb.textContent=m.status==='running'?'выполняется':(Number(m.meta?.code)===0?'ok':('код '+m.meta?.code)) }
 return;
 }

 if(m.kind==='tool'&&tb){
 tb.innerHTML=(m.meta?.input?'<div class="lbl">Ввод</div><pre>'+esc(m.meta.input)+'</pre>':'')+
 (m.meta?.output?'<div class="lbl">Результат</div><pre>'+esc(m.meta.output)+'</pre>':'')+
 (m.meta?.progress!==undefined?'<div class="progress"><i style="width:'+m.meta.progress+'%"></i></div>':'')+
 /* Дерево субагентов — та же вставка, что и в панели вызовов: карточка
    делегации показывает работу ребёнка и в ленте чата. */
 (typeof subTreeFor==='function'?subTreeFor(m):'');
 
 // Обновляем badge статуса
 const badge=el.querySelector('.badge');
 if(badge){
 badge.className='badge '+(m.status==='running'?'running':m.status==='error'?'error':'done');
 badge.textContent=m.status==='running'?'выполняется':m.status==='error'?'ошибка':'готово';
 }
 return;
 }
 
 // Для plan блоков
 if(m.kind==='plan'&&tb){
 tb.innerHTML='<div class="steps">'+(m.meta?.steps||[]).map(st=>'<div class="step '+st.s+'"><i class="fa-solid '+
 (st.s==='done'?'fa-check':st.s==='run'?'fa-circle-notch':st.s==='err'?'fa-xmark':'fa-clock')+'"></i><span>'+esc(st.t)+'</span></div>').join('')+'</div>';
 return;
 }
}
async function stream(m,text,speed=9){
  m.status='streaming'; const chunks=text.match(/[\s\S]{1,3}/g)||[];
  for(let i=0;i<chunks.length;i++){ if(ui.abort)break; m.content+=chunks[i]; patch(m);
    const s=active(); if(s)s.updatedAt=Date.now();
    const st=$('#stream'); if(st) autoScrollBottom(st); await sleep(speed); }
  m.status='done'; renderStream();
}
function openSession(id,mid){
  const s=state.sessions.find(x=>x.id===id);
  if(s&&s.locked){ askPin('verify','PIN для этой сессии').then(ok=>{ if(ok)_openSession(id,mid) }); return }
  _openSession(id,mid);
}
function _openSession(id,mid){
  saveDraft();
  state.activeSessionId=id; ui.query=''; const si=$('#searchInput'); if(si){si.value=''} const sw=$('#searchWrap'); if(sw)sw.classList.remove('has');
  /* Новая сессия открывается снизу: «липкий низ» сбрасываем, иначе чат
     останется на месте, где был прокручен в прошлой сессии. */
  ui.stickBottom=true; updScrollBtn();
  save(); renderAll(); checkDraft();
  if(mid){ setTimeout(()=>{ const el=$('.msg[data-id="'+mid+'"]');
    if(el){el.scrollIntoView({block:'center',behavior:'smooth'});el.classList.add('flash');setTimeout(()=>el.classList.remove('flash'),1600)} },260); }
  if(innerWidth<=860)closeSide();
}
// --- Боковая панель: на десктопе сворачивается в рельс (класс collapsed), на узких экранах это «шторка» (класс open) ---
const SIDE_BP=860;
const sideIsDrawer=()=>innerWidth<=SIDE_BP;
function applySidePanel(){
 const side=$('#side'); if(!side) return;
 if(sideIsDrawer()) side.classList.remove('collapsed'); // на узких экранах класс collapsed только ломает «шторку»
 else side.classList.toggle('collapsed',!state.sideOpen);
 const burger=$('#burger');
 if(burger) burger.classList.toggle('on',sideIsDrawer()?side.classList.contains('open'):!!state.sideOpen);
 const btn=$('#sideCollapseBtn');
 if(btn){
  const open=!!state.sideOpen, t=open?'Свернуть панель':'Развернуть панель';
  btn.title=t; btn.setAttribute('aria-label',t); btn.setAttribute('aria-expanded',String(open));
 }
}
function setSideOpen(open){ state.sideOpen=!!open; applySidePanel(); save(); } // состояние панели (сохраняется)
function setSideDrawer(open){ // «шторка» на узких экранах: состояние панели не меняем
 const side=$('#side'); if(!side) return;
 side.classList.toggle('open',!!open);
 const burger=$('#burger');
 if(burger) burger.classList.toggle('on',!!open);
}
function closeSide(){ setSideDrawer(false) }
$('#burger').onclick=()=>{ sideIsDrawer()?setSideDrawer(!$('#side').classList.contains('open')):setSideOpen(!state.sideOpen) };

// Кнопка сворачивания боковой панели
const sideCollapseBtn=$('#sideCollapseBtn');
if(sideCollapseBtn){
 sideCollapseBtn.onclick=()=>{ sideIsDrawer()?setSideDrawer(false):setSideOpen(!state.sideOpen) };
 sideCollapseBtn.onkeydown=e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); sideCollapseBtn.click() } };
}
addEventListener('resize',()=>{ if(!sideIsDrawer()) setSideDrawer(false); applySidePanel() });

$('#sideScrim').onclick=closeSide;
// --- Панель деталей: на десктопе сворачивается в рельс (класс collapsed) ---
function applyDetailsPanel(){
 const det=$('#details'), chat=$('#chat');
 if(det) det.classList.toggle('collapsed',!state.detailsOpen);
 if(chat) chat.classList.toggle('show-details',!!state.detailsOpen);
 const hd=$('#hdDetails'); if(hd) hd.classList.toggle('on',!!state.detailsOpen);
 const btn=$('#detailsCollapseBtn');
 if(btn){
  const open=!!state.detailsOpen, t=open?'Свернуть панель':'Развернуть панель';
  btn.title=t; btn.setAttribute('aria-label',t); btn.setAttribute('aria-expanded',String(open));
 }
}
function setDetailsOpen(open){ state.detailsOpen=!!open; applyDetailsPanel(); renderDetails(); save(); }
$('#hdDetails').onclick=()=>setDetailsOpen(!state.detailsOpen);

// Кнопка сворачивания панели деталей
const detailsCollapseBtn=$('#detailsCollapseBtn');
if(detailsCollapseBtn){
 detailsCollapseBtn.onclick=()=>setDetailsOpen(!state.detailsOpen);
 detailsCollapseBtn.onkeydown=e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); detailsCollapseBtn.click() } };
}

// Компактный режим
const hdCompact=$('#hdCompact');
if(hdCompact)hdCompact.onclick=()=>{
 const chat=$('#chat');
 if(!chat)return;
 state.compactMode=!state.compactMode;
 chat.classList.toggle('compact',state.compactMode);
 hdCompact.classList.toggle('on',state.compactMode);
 toast(state.compactMode?'Компактный режим':'Обычный режим','ok');
 save(); // Сохраняем состояние
};

// Панель вызовов инструментов
const hdTools=$('#hdTools');
if(hdTools)hdTools.onclick=()=>{
 state.toolsPanelOpen=!state.toolsPanelOpen;
 const panel=$('#toolsPanel');
 if(panel)panel.classList.toggle('open',state.toolsPanelOpen);
 hdTools.classList.toggle('on',state.toolsPanelOpen);
 if(state.toolsPanelOpen)renderToolsPanel();
 save(); // Сохраняем состояние
};

// Закрытие панели инструментов
const toolsPanelClose=$('#toolsPanelClose');
if(toolsPanelClose)toolsPanelClose.onclick=()=>{
 state.toolsPanelOpen=false;
 const panel=$('#toolsPanel');
 if(panel)panel.classList.remove('open');
 const hd=$('#hdTools');
 if(hd)hd.classList.remove('on');
 save(); // Сохраняем состояние
};

// Рендер панели инструментов
/* Сессия, чьи вызовы показываем: ВСЕГДА открытая. Раньше панель следовала за
   сессией, в которой работал агент (ui.toolSessionId), и уводила вызовы
   активного чата в чужую сессию — пользователь просит, чтобы у каждой сессии
   были только свои вызовы: и в ленте, и в панели, и в рассуждениях, и в
   субагентах (все они идут через toolsOwner). */
function toolsOwner(){ return active() }
/* Ход идёт в другой сессии: её вызовы мы не показываем, но молчать нельзя —
   иначе кажется, что агент бросил работу. Возвращаем подпись той сессии. */
function toolsOtherRun(){
 if(!ui.toolSessionId||ui.toolSessionId===state.activeSessionId)return null;
 const other=state.sessions.find(x=>x.id===ui.toolSessionId);
 return other?other:null;
}
function toolsSetCount(total,running){
 const cnt=$('#toolsCount'); if(!cnt)return;
 cnt.textContent=String(total);
 cnt.classList.toggle('busy',running>0);
 const ic=cnt.previousElementSibling;
 if(ic&&ic.tagName==='I') ic.className='fa-solid '+(running>0?'fa-circle-notch fa-spin':'fa-screwdriver-wrench');
 cnt.title='Вызовов в сессии: '+total+(running?(' · выполняется: '+running):'');
}
function toolsCut(v){
 const t=String(v==null?'':v);
 if(t.length<=6000)return t;
 return t.slice(0,6000)+' … (обрезано — полный результат в логе сессии)';
}
/* Закреплённый блок «Рассуждения» в панели инструментов.
   Показывает reasoning текущего/последнего хода агента. Развёрнутый — до 80%
   высоты панели, свёрнутый — полоса 34px. Состояние в state.thinkPinOpen. */
function renderThinkPin(){
  const pin=$('#thinkPin'); if(!pin)return;
  const s=toolsOwner();
  const thinks=s?s.messages.filter(m=>m.kind==='thinking'):[];
  const text=thinks.map(m=>m.content||'').filter(Boolean).join('\n');
  const running=thinks.some(m=>m.status==='running');
  const badge=$('#thinkPinBadge');
  if(badge)badge.style.display=running?'':'none';
  if(!text){ pin.style.display='none'; return }
  pin.style.display='';
   const body=$('#thinkPinBody');
   if(body&&body.textContent!==text){
     const atBottom=body.scrollHeight-body.scrollTop-body.clientHeight<40;
     body.textContent=text;
     /* Автопрокрутка только если пользователь сам держит панель внизу.
        Раньше здесь стояло «||running» — во время работы агента панель
        рассуждений невозможно было прокрутить вверх (тот же баг, что
        и в ленте сообщений). */
     if(atBottom)body.scrollTop=body.scrollHeight;
   }
  pin.classList.toggle('open',state.thinkPinOpen);
  pin.classList.toggle('collapsed',!state.thinkPinOpen);
  /* Свернутый блок тоже должен показывать, что агент думает прямо сейчас */
  pin.classList.toggle('live',running&&!state.thinkPinOpen);
}
/* Поток рассуждений приходит по событиям SSE кусками, поэтому панель
   вызовов инструментов перерисовываем с троттлингом. Раньше patch()
   обновлял только блок в чате — здесь текст появлялся рывками. */
let thinkPinTimer=null;
function renderThinkPinSoon(){
  if(thinkPinTimer)return;
  thinkPinTimer=setTimeout(()=>{
    thinkPinTimer=null;
    try{ renderThinkPin() }catch(_){}
  },80);
}
const thinkPinHead=$('#thinkPinHead');
if(thinkPinHead)thinkPinHead.onclick=function(){
  state.thinkPinOpen=!state.thinkPinOpen; save();
  renderThinkPin();
  const body=$('#thinkPinBody'); if(body&&state.thinkPinOpen)body.scrollTop=body.scrollHeight;
};

/* Тело карточки вызова инструмента. Вынесено отдельно, чтобы карточка в
   ленте чата и в панели «Инструменты» собирались из одного кода: раньше
   patch() перерисовывал тело по-другому, и карточка в чате не обновлялась
   (оставалась в состоянии «выполняется»). */
function toolCardBody(m){
  const meta=m.meta||{};
  return (meta.preview?'<div class="preview">'+esc(meta.preview)+'</div>':'')+
  (meta.input?'<div class="section"><div class="lbl">Ввод</div><pre>'+esc(toolsCut(meta.input))+'</pre></div>':'')+
  (meta.output?'<div class="section"><div class="lbl">Результат</div><pre>'+esc(toolsCut(meta.output))+'</pre></div>':'')+
  (typeof subTreeFor==='function'?subTreeFor(m):'')+
  ((!meta.preview&&!meta.input&&!meta.output&&!Array.isArray(meta.subs))?'<div class="preview" style="opacity:.7">Детали вызова не переданы</div>':'');
}
/* Карточка вызова инструмента. Используется в двух местах: панель
   «Инструменты» справа и — если включён переключатель
   agent.showToolCalls — прямо в чате. Стили .tool-call-item общие,
   поэтому разметка одна и та же. open — начальное состояние раскрытия.
   forChat=true — рисуем в ленте: добавляем data-id (чтобы patch() нашёл
   узел) и обёртку .msg (чтобы блок встал в общий поток сообщений). */
function toolCallHTML(m,open,forChat){
  const meta=m.meta||{};
  const st=m.status==='running'?'running':m.status==='error'?'error':'done';
  const stText=st==='running'?'<i class="fa-solid fa-circle-notch fa-spin"></i>выполняется':st==='error'?'ошибка':'готово';
  const card='<div class="tool-call-item '+st+(open?' open':'')+'" data-tool-id="'+m.id+'"'+(forChat?' data-id="'+m.id+'"':'')+'>'+
  '<div class="tool-call-head" data-toggle-tool="'+m.id+'">'+
  '<span class="icon" style="background:var(--accent-soft);color:var(--accent)"><i class="'+esc(meta.icon||'fa-solid fa-screwdriver-wrench')+'"></i></span>'+
  '<span class="name">'+esc(meta.name||'Инструмент')+'</span>'+
  '<span class="badge '+st+'">'+stText+'</span>'+
  (meta.dur?'<span class="tool-dur" style="font-size:9px;color:var(--text-3)">'+esc(meta.dur)+'</span>':'')+
  '<i class="fa-solid fa-chevron-down chev"></i>'+
  '</div>'+
  '<div class="tool-call-body">'+toolCardBody(m)+'</div>'+
  '</div>';
  return forChat?('<div class="msg"><div class="col">'+card+'</div></div>'):card;
}
/* Состояние раскрытия карточки: запоминаем в ui.toolsOpen, чтобы
   перерисовка по SSE его не сбрасывала. */
function toolOpen(m){
  return Object.prototype.hasOwnProperty.call(ui.toolsOpen,m.id)?ui.toolsOpen[m.id]:!!(m.meta||{}).open;
}

function renderToolsPanel(){
 const body=$('#toolsPanelBody');
 if(!body)return;
 renderThinkPin();
  /* Субагенты живут своим циклом: обновляем их, пока панель открыта,
     иначе статус застынет на момент первого запроса. */
  if(state.toolsPanelOpen){ renderSubagents(); subRefreshSoon() }
 const s=toolsOwner();
 if(!s){
 toolsSetCount(0,0);
 body.innerHTML='<div class="empty"><i class="fa-solid fa-screwdriver-wrench"></i><p>Нет активной сессии</p></div>';
 return;
 }
  const toolCalls=s.messages.filter(m=>m.kind==='tool');
  const running=toolCalls.filter(m=>m.status==='running').length;
  toolsSetCount(toolCalls.length,running);
  /* Открытая сессия не обязана быть той, где работает агент: показываем её
     собственные вызовы, а про чужой ход только говорим ссылкой. */
  const other=toolsOtherRun();
  const owner=other?('<div class="tools-owner"><i class="fa-solid fa-circle-notch fa-spin"></i><span>Агент работает в сессии «'+esc(other.title||'Без названия')+'» — её вызовы видны там</span></div>'):'';
  if(!toolCalls.length){
  body.innerHTML=owner+'<div class="empty"><i class="fa-solid fa-screwdriver-wrench"></i><p>Нет вызовов инструментов в этой сессии</p></div>';
  return;
  }
  /* Панель пересобирается целиком, поэтому позицию прокрутки надо
     сохранить: иначе при каждом обновлении (а оно идёт каждые 120 мс
     во время работы агента) список вызовов прыгал в начало и читать
     его было невозможно. */
  const prevTop=body.scrollTop;
  body.innerHTML=owner+toolCalls.map(function(m){ return toolCallHTML(m,toolOpen(m)) }).join('');
  body.scrollTop=prevTop;
 }
// Перерисовка по SSE — с задержкой, чтобы не пересобирать список на каждый чанк
let toolsRenderTimer=null;
function renderToolsPanelSoon(){
 if(!state.toolsPanelOpen){ renderThinkPin(); return }
 clearTimeout(toolsRenderTimer);
 toolsRenderTimer=setTimeout(function(){ if(state.toolsPanelOpen)renderToolsPanel(); else renderThinkPin() },120);
}

// Раскрытие карточки вызова: состояние запоминаем, чтобы SSE-перерисовка его не сбрасывала
document.addEventListener('click',function(e){
 const head=e.target.closest('[data-toggle-tool]');
 if(!head)return;
 const item=head.closest('.tool-call-item');
 if(!item)return;
 item.classList.toggle('open');
 ui.toolsOpen[item.dataset.toolId]=item.classList.contains('open');
});

// Развернуть / Свернуть все карточки
const toolsExpandAll=$('#toolsExpandAll');
if(toolsExpandAll)toolsExpandAll.onclick=function(){
 const items=[].slice.call(document.querySelectorAll('#toolsPanelBody .tool-call-item'));
 if(!items.length)return;
 const allOpen=items.every(function(it){ return it.classList.contains('open') });
 items.forEach(function(it){ it.classList.toggle('open',!allOpen); ui.toolsOpen[it.dataset.toolId]=!allOpen });
 const ic=toolsExpandAll.querySelector('i');
 if(ic)ic.className='fa-solid '+(allOpen?'fa-expand':'fa-compress');
 toolsExpandAll.title=allOpen?'Развернуть все':'Свернуть все';
 toolsExpandAll.classList.toggle('on',!allOpen);
};

// Очистить список вызовов (подтверждение + отмена по Ctrl+Z)
const toolsClear=$('#toolsClear');
if(toolsClear)toolsClear.onclick=async function(){
 const s=toolsOwner(); if(!s)return;
 const n=s.messages.filter(function(m){ return m.kind==='tool' }).length;
 if(!n){ toast('Список вызовов пуст','info'); return }
 const okv=await askForm({title:'Очистить список вызовов?',icon:'fa-broom',ok:'Очистить',
 fields:[{name:'c',label:'Вызовы этой сессии будут удалены (Ctrl+Z вернёт)',value:'очистить'}]});
 if(!okv)return;
 const old=s.messages.slice();
 s.messages=s.messages.filter(function(m){ return m.kind!=='tool' });
 ui.toolsOpen={};
 pushHistory(function(){ s.messages=s.messages.filter(function(m){ return m.kind!=='tool' }); save(); renderAll() },
 function(){ s.messages=old; save(); renderAll() });
 save(); renderAll(); renderToolsPanel(); toast('Список вызовов очищен','ok');
};

// Полноэкранный режим
const hdFullscreen=$('#hdFullscreen');
if(hdFullscreen)hdFullscreen.onclick=()=>{
 const overlay=$('#chatOverlay');
 if(!overlay)return;
 ui.fullscreenMode=!ui.fullscreenMode;
 overlay.classList.toggle('fullscreen',ui.fullscreenMode);
 const icon=hdFullscreen.querySelector('i');
 if(icon)icon.className=ui.fullscreenMode?'fa-solid fa-compress':'fa-solid fa-expand';
 toast(ui.fullscreenMode?'Полноэкранный режим':'Оконный режим','ok');
};
$('#hdSpeak').onclick=$('#cSpeak').onclick=()=>{ S().voice.autoSpeak=!S().voice.autoSpeak; save(); renderHead();
  toast('Автоозвучка '+(S().voice.autoSpeak?'включена':'выключена'),'ok'); };
const hdPin=$('#hdPin');
if(hdPin)hdPin.onclick=()=>{const s=active();if(!s)return;s.pinned=!s.pinned;save();renderAll();toast(s.pinned?'Закреплено':'Откреплено','ok')};

const hdLock=$('#hdLock');
if(hdLock)hdLock.onclick=async()=>{
 const s=active();if(!s)return;
 if(s.locked){ const ok=await askPin('verify','PIN'); if(!ok){toast('Неверный','err');return} s.locked=false; }
 else { if(!state.pin){ const ok=await askPin('set','Задайте PIN'); if(!ok)return } s.locked=true; }
 save();renderAll();
};
$('#sessTitle').ondblclick=()=>{const s=active();s&&renameSession(s)};
$('#exportSession').onclick=()=>{const s=active();if(!s)return;exportSession(s,'txt')};
$('#clearSession').onclick=async()=>{const s=active();if(!s)return;
  if(await askForm({title:'Очистить?',icon:'fa-broom',ok:'Очистить',fields:[{name:'c',label:'Подтвердите',value:s.title}]})){
    const old=s.messages.slice(); s.messages=[];
    pushHistory(async()=>{s.messages=[];save();renderAll()},async()=>{s.messages=old;save();renderAll()});
    save();renderAll();toast('Очищено','ok')}};
$('#hdSessMore').onclick=e=>{const s=active();if(!s)return;const r=e.currentTarget.getBoundingClientRect();
  sessionCtx(s.id,{getBoundingClientRect:()=>({left:r.left,bottom:r.bottom+6,top:r.top,right:r.right})})};

// ОБРАБОТКА ДЕЙСТВИЙ В СООБЩЕНИЯХ
$('#stream').addEventListener('click',e=>{
  const tg=e.target.closest('[data-tgl]'); if(tg){ tg.parentElement.classList.toggle('open'); return }
  const cp=e.target.closest('[data-copy]'); if(cp){ const txt=cp.closest('pre').querySelector('code').textContent;
    navigator.clipboard.writeText(txt).catch(()=>{}); cp.classList.add('copied');
    cp.innerHTML='<i class="fa-solid fa-check"></i> Скопировано';
    setTimeout(()=>{cp.classList.remove('copied');cp.innerHTML='<i class="fa-regular fa-copy"></i> Copy'},1500); return }
  const ip=e.target.closest('[data-imgprev]'); if(ip){ openImgPreview(ip.src); return }
  const ap=e.target.closest('[data-appr]'); if(ap){ resolveApproval(ap.dataset.id,ap.dataset.appr); return }
  const cpk=e.target.closest('[data-clr-pick]'); if(cpk){ pickClarify(cpk.dataset.id,Number(cpk.dataset.clrPick),cpk); return }
  const csd=e.target.closest('[data-clr-send]'); if(csd){ sendClarifyAnswer(csd.dataset.id); return }
  const pr=e.target.closest('.preset'); if(pr){ const inp=$('#input');if(inp){inp.value=pr.textContent;autoGrow(inp)} submit(); return }

  // РЕАКЦИИ
  const rxa=e.target.closest('[data-rx]');
  if(rxa){ toggleReaction(rxa.dataset.msg,rxa.dataset.rx); return }
  const rxb=e.target.closest('[data-rx-add]');
  if(rxb){ openRxPicker(rxb,rxb.dataset.rxAdd); return }

  // BOOKMARK
  const bk=e.target.closest('[data-act="bookmark"]');
  if(bk){ addBookmark(bk.dataset.id); return }

  // REPLY
  const rp=e.target.closest('[data-act="reply"]');
  if(rp){ setReply(rp.dataset.id); return }

  // EDIT
  const ed=e.target.closest('[data-act="edit"]');
  if(ed){ startEdit(ed.dataset.id); return }

  const pvt=e.target.closest('[data-pv-tool]');
  if(pvt){
    const s3=active(), m3=s3&&s3.messages.find(x=>x.id===pvt.dataset.pvTool);
    if(m3&&m3.meta?.fileContent){ const isHtml=/\.html$/i.test(m3.meta?.label||''); openPreview({title:m3.meta?.label,content:m3.meta.fileContent,lang:isHtml?'html':'text',filename:m3.meta?.label||'document.txt'}) }
    return;
  }
  const pva=e.target.closest('[data-act="preview"]');
  if(pva){
    const s4=active(), m4=s4&&s4.messages.find(x=>x.id===pva.dataset.id);
    if(m4){ const c=extractCode(m4.content); if(c) openPreview({title:'Из ответа агента',content:c.code,lang:c.lang,filename:'agent-export.'+(c.lang==='html'?'html':'txt')}); else toast('В сообщении нет кода','info') }
    return;
  }
  const a=e.target.closest('[data-act]'); if(!a)return;
  const s=active(),m=s&&s.messages.find(x=>x.id===a.dataset.id); if(!m)return;
  const act=a.dataset.act;
  if(act==='rewind'){ rewindToMessage(m); return }
  if(act==='copy'){ navigator.clipboard.writeText(plain(m.content)).catch(()=>{}); toast('Скопировано','ok') }
  if(act==='speak') speak(m.content);
  if(act==='apply'){ const i=$('#input'); if(i){i.value=(i.value?i.value+'\n':'')+plain(m.content); autoGrow(i); i.focus(); updTok()} }
  if(act==='regen'){
    /* Второй ход поверх работающего ломает очередь и общий ui.generating:
       агент отвечает дважды, а «стоп» останавливает только последний. */
    if(ui.generating){ toast('Агент ещё работает — дождитесь ответа или нажмите «Стоп»','info'); return }
    s.messages=s.messages.filter(x=>x!==m); renderStream(); agentTurn(lastUserText(s)||'повтори задачу') }
});

/* EDIT MESSAGE */
function startEdit(msgId){
  /* Правка обрезает переписку и запускает новый ход. Пока идёт текущий ход,
     это дало бы два параллельных ответа в одной сессии. */
  if(ui.generating){ toast('Пока агент работает, правка недоступна — остановите ход','info'); return }
  ui.editMsg=msgId;
  renderStream();
  setTimeout(()=>{
    const ta=$('#editText');
    if(ta){ ta.focus(); ta.setSelectionRange(ta.value.length,ta.value.length); }
    $('#editCancel').onclick=()=>{ ui.editMsg=null; renderStream(); };
    $('#editSave').onclick=()=>{
      const s=active(); if(!s)return;
      if(ui.generating){ toast('Агент уже начал новый ход — правка отменена','info'); return }
      const m=s.messages.find(x=>x.id===msgId); if(!m)return;
      const newTxt=ta.value.trim(); if(!newTxt)return;
      const oldTxt=m.content;
      m.content=newTxt;
      // удалить все последующие сообщения
      const idx=s.messages.indexOf(m);
      const removed=s.messages.splice(idx+1);
      ui.editMsg=null;
      save(); renderStream();
      toast('Сообщение обновлено, перегенерирую','info');
      agentTurn(newTxt);
    };
  },50);
}

/* ОТКАТ ДО СООБЩЕНИЯ
   Пользователь выбрал «оставить это сообщение, всё ниже убрать». Режем и ленту,
   и durable-транскрипт: иначе старшие сообщения остались бы в контексте модели.
   В БД строки мягко архивируются (active=0), не стираются. */
function remoteTextOf(row){
  function part(p){ if(!p||typeof p!=='object')return '';
    if(p.type==='text')return String(p.text||'');
    if(p.image_url||String(p.type||'').indexOf('image')>=0){
      let iu=(p.image_url&&(p.image_url.url||p.image_url))||p.url||p.dataUrl||'';
      if(!iu&&p.source&&p.source.data)
        iu='data:'+(p.source.media_type||'image/png')+';base64,'+p.source.data;
      return (typeof iu==='string'&&iu.indexOf('data:image/')===0)?'![изображение]('+iu+')':'[изображение]';
    }
    return '' }
  const c=row&&row.content;
  if(Array.isArray(c))return c.map(part).filter(Boolean).join('\n');
  return c==null?'':String(c);
}
function sameRemoteText(a,b){ if(a===b)return true; if(!a||!b)return false;
  return a.indexOf(b)>=0||b.indexOf(a)>=0 }
function localTextMessages(s){
  return (s.messages||[]).filter(m=>m.kind==='text'&&(m.role==='user'||m.role==='agent')&&String(m.content||'').trim());
}
async function durableMessageId(s,m){
  if(m&&m.backendMsgId)return Number(m.backendMsgId)||0;
  const x=String((m&&m.id)||'');
  if(x.indexOf('m_')===0&&/^\d+$/.test(x.slice(2)))return Number(x.slice(2));
  const d=await API.req('GET','/api/sessions/'+encodeURIComponent(s.backendId)+'/messages?order=oldest&limit=500');
  const rows=((d&&d.data)||[]).filter(r=>r&&(r.role==='user'||r.role==='assistant'));
  const wantRole=m.role==='user'?'user':'assistant', want=String(m.content||'');
  const local=localTextMessages(s), at=local.indexOf(m);
  let ordinal=0;
  for(let i=0;i<at;i++){
    const q=local[i];
    if((q.role==='user'?'user':'assistant')===wantRole&&String(q.content||'')===want)ordinal++;
  }
  let seen=0;
  for(const r of rows){
    if(r.role!==wantRole||!sameRemoteText(remoteTextOf(r),want))continue;
    if(seen===ordinal)return Number(r.id)||0;
    seen++;
  }
  for(let i=rows.length-1;i>=0;i--){
    if(rows[i].role===wantRole&&sameRemoteText(remoteTextOf(rows[i]),want))return Number(rows[i].id)||0;
  }
  return 0;
}
async function rewindToMessage(m){
  const s=active(); if(!s||!m||m.kind!=='text')return;
  if(ui.generating){ toast('Дождитесь конца хода — откат изменит историю','info'); return }
  const idx=s.messages.indexOf(m); if(idx<0)return;
  if(!s.messages.slice(idx+1).length&&!s.backendId){ toast('После этого сообщения ничего нет','info'); return }
  if(!confirm('Откатить переписку до этого сообщения?\n\nВсё, что ниже, исчезнет из чата и из контекста агента.'+
              (s.backendId?' В базе сообщения архивируются (active=0), а не стираются.':'')))return;
  if(!s.backendId){
    s.messages.splice(idx+1); s.updatedAt=Date.now(); save(); renderAll();
    toast('Откат выполнен','ok'); return;
  }
  try{
    const targetId=await durableMessageId(s,m);
    if(!targetId){ toast('Не удалось определить сообщение на сервере','err'); return }
    const r=await API.req('POST','/api/sessions/'+encodeURIComponent(s.backendId)+'/truncate',
                          {after_message_id:targetId});
    s.messages.splice(idx+1); s.updatedAt=Date.now();
    if(ui.replyTo&&!s.messages.some(x=>x.id===ui.replyTo.id))ui.replyTo=null;
    save(); renderAll();
    toast('Откат: убрано сообщений — '+(Number(r&&r.truncated_count)||0),'ok');
  }catch(e){ toast('Откат не выполнен: '+errText((e&&e.message)||e),'err') }
}
/* Правый клик по сообщению — то же действие, что и кнопка «Откатить». */
document.addEventListener('contextmenu',e=>{
  const el=e.target.closest('.msg[data-id]'); if(!el)return;
  const s=active(), m=s&&s.messages.find(x=>x.id===el.dataset.id);
  if(!m||m.kind!=='text')return;
  e.preventDefault();
  ctxMenu(e.clientX,e.clientY,[
    {icon:'fa-clock-rotate-left',text:'Откатить переписку до этого сообщения',onClick:()=>rewindToMessage(m)},
    {icon:'fa-regular fa-copy',text:'Копировать текст',onClick:()=>{navigator.clipboard.writeText(plain(m.content)).catch(()=>{});toast('Скопировано','ok')}}
  ]);
});

/* REPLY */
function setReply(msgId){
  const s=active(); if(!s)return;
  const m=s.messages.find(x=>x.id===msgId); if(!m)return;
  ui.replyTo={id:msgId,text:plain(m.content).slice(0,200)};
  const rb=$('#replyBar'),rt=$('#replyText');
  if(rb)rb.classList.add('on');
  if(rt)rt.textContent=ui.replyTo.text;
  const inp=$('#input'); if(inp) inp.focus();
}
$('#replyClose').onclick=()=>{
  ui.replyTo=null;
  const rb=$('#replyBar'); if(rb) rb.classList.remove('on');
};

/* BOOKMARKS */
function addBookmark(msgId){
  const s=active(); if(!s)return;
  const m=s.messages.find(x=>x.id===msgId); if(!m)return;
  s.bookmarks=s.bookmarks||[];
  const existing=s.bookmarks.find(b=>b.msgId===msgId);
  if(existing){
    s.bookmarks=s.bookmarks.filter(b=>b.msgId!==msgId);
    toast('Закладка удалена','info');
  } else {
    s.bookmarks.push({msgId,ts:Date.now(),label:plain(m.content).slice(0,80)});
    toast('Добавлено в закладки','ok');
  }
  save(); renderAll();
}
const hdBookmarks=$('#hdBookmarks');
if(hdBookmarks)hdBookmarks.onclick=()=>{
 ui.bookmarksOpen=!ui.bookmarksOpen;
 const bp=$('#bookmarksPanel'); if(bp) bp.classList.toggle('open',ui.bookmarksOpen);
 if(ui.bookmarksOpen) renderBookmarks();
};
$('#bmClose').onclick=()=>{ ui.bookmarksOpen=false; const bp=$('#bookmarksPanel'); if(bp) bp.classList.remove('open'); };
function renderBookmarks(){
  const body=$('#bmBody'); if(!body)return;
  const s=active();
  if(!s||!s.bookmarks||!s.bookmarks.length){ body.innerHTML='<div class="empty"><i class="fa-solid fa-bookmark"></i><p>Нет закладок. Добавьте через меню сообщения.</p></div>'; return }
  body.innerHTML=s.bookmarks.map(b=>{
    const m=s.messages.find(x=>x.id===b.msgId);
    if(!m) return '';
    return '<div class="bm-row" data-bm="'+b.msgId+'">'+
      '<div class="top"><b>'+(m.role==='user'?'Вы':'Агент')+'</b><small>'+tDate(b.ts)+'</small></div>'+
      '<p>'+esc(plain(m.content).slice(0,200))+'</p>'+
      '<div class="acts">'+
      '<button data-jump="'+b.msgId+'"><i class="fa-solid fa-arrow-right"></i> К сообщению</button>'+
      '<button class="danger" data-rmbm="'+b.msgId+'"><i class="fa-regular fa-trash-can"></i> Убрать</button>'+
      '</div></div>';
  }).join('');
  body.onclick=e=>{
    const j=e.target.closest('[data-jump]');
    if(j){
      const el=$('.msg[data-id="'+j.dataset.jump+'"]');
      if(el) el.scrollIntoView({block:'center',behavior:'smooth'});
      return;
    }
    const rm=e.target.closest('[data-rmbm]');
    if(rm){
      s.bookmarks=s.bookmarks.filter(b=>b.msgId!==rm.dataset.rmbm);
      save(); renderBookmarks(); renderAll();
    }
  };
}

/* REACTIONS */
function toggleReaction(msgId,rxKey){
  const s=active(); if(!s)return;
  const m=s.messages.find(x=>x.id===msgId); if(!m)return;
  m.reactions=m.reactions||{};
  if(m.reactions[rxKey]){ delete m.reactions[rxKey]; }
  else { m.reactions[rxKey]=1; }
  save(); renderStream();
}
function openRxPicker(el,msgId){
  closeRxPicker();
  const s=active(); if(!s)return;
  const m=s.messages.find(x=>x.id===msgId); if(!m)return;
  const rect=el.getBoundingClientRect();
  const stream=$('#stream'); if(!stream) return;
  const streamRect=stream.getBoundingClientRect();
  const pick=document.createElement('div');
  pick.className='rx-pick';
  pick.id='rxPicker';
  pick.innerHTML=REACTIONS.map(r=>'<button data-rxpick="'+r.k+'" data-msg="'+msgId+'" title="'+r.label+'"><i class="'+r.icon+'"></i></button>').join('');
  pick.style.left=(rect.left-streamRect.left-80)+'px';
  pick.style.top=(rect.top-streamRect.top-44)+'px';
  stream.appendChild(pick);
  pick.onclick=ev=>{
    const b=ev.target.closest('[data-rxpick]');
    if(b){ toggleReaction(b.dataset.msg,b.dataset.rxpick); closeRxPicker(); }
  };
  setTimeout(()=>document.addEventListener('click',closeRxPickerOutside),10);
}
function closeRxPicker(){ const p=$('#rxPicker'); if(p) p.remove(); document.removeEventListener('click',closeRxPickerOutside); }
function closeRxPickerOutside(e){
  if(!e.target.closest('#rxPicker')&&!e.target.closest('[data-rx-add]')) closeRxPicker();
}

/* IMAGE PREVIEW */
function openImgPreview(src){
  const ip=$('#imgPreview'), img=$('#imgPreviewImg');
  if(ip&&img){ img.src=src; ip.classList.add('on'); }
}
$('#imgPreview').onclick=()=>{ const ip=$('#imgPreview'); if(ip) ip.classList.remove('on'); };

/* EXTRACT TASKS
   Logika zhitela tolko v obrabotchike knopki #hdExtract, kotoroy v razmetke net:
   punkt meni ne delal nichego. Teper eto obychnaya funkciya. */
function extractSessionTasks(){
  const s=active(); if(!s)return;
  const tasks=[];
  const patterns=[
    /\b(TODO|FIXME|XXX|HACK|NOTE)\b[:\-]?\s*([^\n.]+)/gi,
    /(?<=^|\s)(сделать|нужно|надо|не забыть|обязательно|важно)(?=\s|$)[:\-]?\s*([^\n.]+)/gi,
    /\b(need to|must|should|have to)\b[:\-]?\s*([^\n.]+)/gi,
    /[-•]\s*([^.\n]+)/g
  ];
  s.messages.forEach(m=>{
    if(m.kind==='system')return;
    const txt=plain(m.content);
    patterns.forEach(pat=>{
      let match;
      while((match=pat.exec(txt))!==null){
        const task=match[2]||match[1];
        if(task&&task.trim().length>5&&task.trim().length<200){
          tasks.push(task.trim());
        }
      }
    });
  });
  const unique=[...new Set(tasks)].slice(0,20);
  if(!unique.length){ toast('Задач не найдено','info'); return }
  const outText='**Извлечённые задачи из сессии:**\n\n'+unique.map((t,i)=>(i+1)+'. '+t).join('\n');
  msg(s,{role:'agent',kind:'text',content:outText,status:'done',meta:{}});
  save(); renderAll();
  toast('Найдено задач: '+unique.length,'ok');
}
/* Esli knopka poyavitsya v shapke - ispolzuet tu zhe funkciyu */
const hdExtract=$('#hdExtract');
if(hdExtract)hdExtract.onclick=extractSessionTasks;

function renderDetails(){
  const s=active(),d=$('#details'),db=$('#detailsBody'); if(!s||!d||!db)return;
  const p=projectOf(s),f=folderOf(s);
  const notes=ui.notes[s.id]||[];
 const toks=(s.costIn||0)+(s.costOut||0);
 const model=MODELS.find(m=>m.id===(s.model||S().agent.model))||{ctx:200000}; // Fallback если MODELS пустой
 const pct=model.ctx>0?clamp(toks/model.ctx*100,0,100):0;
 const rate=S().pricing.rates[s.model]||{in:1,out:1};
  const costUSD=((s.costIn||0)*rate.in+(s.costOut||0)*rate.out)/1000000;
  const cur=S().pricing.currency;
  const rateFx=cur==='EUR'?0.92:cur==='RUB'?92:1;
  const sym=cur==='EUR'?'€':cur==='RUB'?'₽':'$';
  db.innerHTML=
  '<div class="dsec"><h4><i class="fa-solid fa-circle-info"></i> Сессия</h4><div class="dcard">'+
    '<div class="dline"><span>Название</span><b>'+esc(s.title)+'</b></div>'+
    '<div class="dline"><span>Создана</span><b>'+new Date(s.createdAt).toLocaleString('ru-RU')+'</b></div>'+
    '<div class="dline"><span>Активность</span><b>'+ago(s.updatedAt)+'</b></div>'+
    '<div class="dline"><span>Сообщений</span><b>'+s.messages.length+'</b></div>'+
    '<div class="dline"><span>Закладок</span><b>'+(s.bookmarks?.length||0)+'</b></div>'+
    '<div class="dline"><span>Токенов</span><b>'+toks.toLocaleString('ru')+'</b></div>'+
    '<div class="dline"><span>Стоимость</span><b>'+sym+(costUSD*rateFx).toFixed(4)+'</b></div>'+
    '<div class="bar" title="Контекст"><i style="width:'+pct.toFixed(1)+'%"></i></div>'+
  '</div></div>'+
  '<div class="dsec"><h4><i class="fa-solid fa-tags"></i> Теги</h4><div class="dcard"><div class="tags">'+
    ((s.tags||[]).length?(s.tags||[]).map(t=>'<span class="tag">'+esc(t)+'<i class="fa-solid fa-xmark x" data-dtag="'+t+'"></i></span>').join(''):
    '<span style="font-size:12px;color:var(--text-3)">Нет тегов</span>')+
    '</div><button class="btn sm" style="margin-top:10px;width:100%" id="dAddTag"><i class="fa-solid fa-plus"></i> Добавить тег</button></div></div>'+
  '<div class="dsec"><h4><i class="fa-solid fa-diagram-project"></i> Рабочая папка</h4><div class="dcard">'+
    (p?'<div class="dline"><span>Проект</span><b>'+esc(p.name)+'</b></div><div class="dline"><span>Путь</span><b style="font-family:var(--mono);font-size:11px" title="'+esc(p.path)+'">'+esc(p.path)+'</b></div>'+
      '<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">'+
      '<button class="btn sm" id="dChangeProject" style="flex:1"><i class="fa-solid fa-right-left"></i> Сменить</button>'+
      '<button class="btn sm ghost" id="dLeaveProject" style="flex:1" title="Отвязать сессию от проекта — агент будет работать без рабочей папки"><i class="fa-solid fa-arrow-right-from-bracket"></i> Выйти</button>'+
      '</div>'
     :'<div class="empty" style="padding:16px 6px"><i class="fa-solid fa-folder-open"></i><p>Проект не подключён</p>'+
      '<button class="btn primary sm" id="dAttach"><i class="fa-solid fa-folder-plus"></i> Выбрать</button></div>')+
 '</div></div>'+
 '<div class="dsec" id="fsbSec" style="display:'+(p||state.activeProjectId?'':'none')+'"><h4><i class="fa-solid fa-folder-open"></i> Файлы проекта</h4>'+
   '<div class="dcard fsb" id="fsbBox"></div></div>'+
 '<div class="dsec"><h4><i class="fa-solid fa-note-sticky"></i> Заметки</h4><div class="dcard">'+
    (notes.length?notes.map((n,i)=>'<div class="note"><i class="fa-solid fa-circle"></i><div style="flex:1">'+esc(n)+'</div>'+
      '<button class="act" data-delnote="'+i+'"><i class="fa-regular fa-trash-can"></i></button></div>').join(''):
      '<div style="font-size:12px;color:var(--text-3);padding:4px 0">Заметки привязаны к сессии</div>')+
    '<button class="btn sm" style="margin-top:10px;width:100%" id="dNote"><i class="fa-solid fa-plus"></i> Добавить</button></div></div>';
  const at=$('#dAttach'); if(at)at.onclick=async()=>{ const pr=await createProject(); if(pr){s.projectId=pr.id;save();renderAll()} };
  /* Отвязать сессию от проекта. Раньше это было невозможно: чтобы создать
     сессию «без проекта», приходилось удалять сам проект. */
  const lp=$('#dLeaveProject');
  if(lp)lp.onclick=async()=>{
    const ok=await askForm({title:'Выйти из проекта?',icon:'fa-arrow-right-from-bracket',ok:'Выйти',
      fields:[{name:'x',label:'Сессия «'+s.title+'» останется в списке, но потеряет рабочую папку. Проект и его файлы не удаляются.'}]});
    if(ok===undefined||ok===null)return;
    s.projectId=null;
    if(state.activeProjectId===(p&&p.id))state.activeProjectId=null;
    save(); renderAll();
    toast('Сессия вне проекта','ok');
  };
  const cp=$('#dChangeProject');
  if(cp)cp.onclick=async()=>{ const pr=await pickProjectForSession(); if(pr!==undefined){ s.projectId=pr; save(); renderAll() } };
  const nt=$('#dNote'); if(nt)nt.onclick=async()=>{ const v=await askForm({title:'Заметка',icon:'fa-note-sticky',ok:'Добавить',
    fields:[{name:'t',label:'Текст',ph:'Важный контекст'}]}); if(v&&v.t){(ui.notes[s.id]=ui.notes[s.id]||[]).push(v.t);renderDetails()} };
  const at2=$('#dAddTag'); if(at2)at2.onclick=async()=>{ const v=await askForm({title:'Тег',icon:'fa-tag',ok:'Добавить',
    fields:[{name:'t',label:'Название',ph:'важное'}]}); if(v&&v.t){s.tags=s.tags||[];if(!s.tags.includes(v.t))s.tags.push(v.t);save();renderAll()} };
  d.onclick=e=>{
    const dn=e.target.closest('[data-delnote]');if(dn){ui.notes[s.id].splice(+dn.dataset.delnote,1);renderDetails()}
    const dt=e.target.closest('[data-dtag]');if(dt){s.tags=s.tags.filter(x=>x!==dt.dataset.dtag);save();renderAll()}
  };
  // Файловый браузер: грузим содержимое рабочей папки (асинхронно, кэш — по пути).
  // Путь берём из absPath: раньше использовался только p.path, из-за чего
  // проекты, созданные с диалога (там absPath), панель не грузила вовсе.
  // Если сессия вне проекта, показываем файлы активного проекта — иначе
  // панель выглядит «пустой» и непонятно, сломано что-то или нет.
  const activeProj=(!p&&state.activeProjectId)?state.projects.find(x=>x.id===state.activeProjectId):null;
  const fsbProj=p||activeProj;
  const wantDir=(fsbProj&&(fsbProj.absPath||fsbProj.path))||'';
  /* Загружаем каталог только когда сменился корень проекта. Раньше условие
     сравнивало fsb.path с корнем, поэтому любая навигация в подпапку
     (fsb.path !== корень) вызывала перезагрузку корня — выбранная
     папка сбрасывалась при каждом renderDetails(). */
  if(wantDir&&(fsb.root!==wantDir||!fsb.entries.length&&!fsb.error)){
    fsb.root=wantDir;
    fsbLoad(wantDir).then(()=>renderFileBrowser());
  }else{
    if(!wantDir){ fsb.path=null; fsb.root=null; fsb.entries=[]; fsb.error=null }
    renderFileBrowser();
  }
  /* Подсказка: чьи файлы показаны, если сессия не привязана к проекту */
  const note=$('#fsbBox .fsb-hint');
  if(note&&activeProj&&!p&&fsb.entries.length)note.textContent=
    'Файлы проекта «'+esc(activeProj.name)+'» · перетащите в чат';
}

/* ============================================================
   COMPOSER
   ============================================================ */
const input=$('#input');
function autoGrow(t){ if(!t)return; t.style.height='auto'; t.style.height=Math.min(t.scrollHeight,180)+'px' }
if(input){
  input.addEventListener('input',()=>{autoGrow(input);updTok();
    const v=input.value.trim();
    const cp=$('#cmdPop'); if(cp) cp.classList.toggle('open',v.startsWith('/')&&v.length<20);
    updateSuggestions();
    clearTimeout(draftTimer); draftTimer=setTimeout(saveDraft,800);
  });
  input.addEventListener('keydown',e=>{
    if(e.key==='Enter'&&!e.shiftKey){
      const sg=$('#suggest');
      if(sg&&sg.classList.contains('on')){e.preventDefault();applySuggestion();return}
      e.preventDefault();submit();
    }
    if(e.key==='Escape'){const cp=$('#cmdPop');if(cp)cp.classList.remove('open');const sg=$('#suggest');if(sg)sg.classList.remove('on')}
    if(e.key==='ArrowDown'){const sg=$('#suggest');if(sg&&sg.classList.contains('on')){e.preventDefault();moveSuggestion(1)}}
    if(e.key==='ArrowUp'){const sg=$('#suggest');if(sg&&sg.classList.contains('on')){e.preventDefault();moveSuggestion(-1)}}
  });
}
function updTok(){
  const s=active();
  const ft=$('#fCost');
  if(!ft)return;
  const inputTok=input?Math.round(input.value.length/3.4):0;
  const msgTok=s?(s.costIn||0)+(s.costOut||0):0;
  const totalTok=inputTok+msgTok;
  const rate=s?S().pricing.rates[s.model]:{in:1,out:1};
  const costUSD=((s?.costIn||0)+inputTok)*rate.in/1000000 + ((s?.costOut||0))*rate.out/1000000;
  const cur=S().pricing.currency;
  const rateFx=cur==='EUR'?0.92:cur==='RUB'?92:1;
  const sym=cur==='EUR'?'€':cur==='RUB'?'₽':'$';
  ft.innerHTML='<b>'+sym+(costUSD*rateFx).toFixed(4)+'</b> · '+totalTok.toLocaleString('ru')+' ток.';
}

let suggestIdx=0;
function updateSuggestions(){
  const v=input?input.value:'';
  const box=$('#suggest');
  if(!box)return;
  if(!v.startsWith('/')){box.classList.remove('on');return}
  const term=v.toLowerCase();
  const builtin=[
    {cmd:'/plan',d:'составить план',i:'fa-list-check'},
    {cmd:'/code',d:'работа с кодом',i:'fa-code'},
    {cmd:'/files',d:'обзор папки',i:'fa-folder-tree'},
    {cmd:'/search',d:'поиск в сети',i:'fa-magnifying-glass'},
    {cmd:'/run',d:'выполнить команду',i:'fa-terminal'},
    {cmd:'/clear',d:'очистить диалог',i:'fa-broom'}
  ];
  const custom=(S().commands||[]).map(c=>({cmd:c.name,d:c.desc||'своя команда',i:'fa-terminal'}));
  const recent=ui.recentCmds.filter(c=>c.startsWith('/')&&!builtin.find(x=>x.cmd===c)&&!custom.find(x=>x.cmd===c)).slice(0,3)
    .map(c=>({cmd:c,d:'недавно',i:'fa-clock-rotate-left'}));
  const plug=PluginBus.commands().map(c=>({cmd:c.name,d:c.desc||'плагин',i:'fa-puzzle-piece'}));
  const all=[...builtin,...custom,...recent,...plug];
  const filtered=all.filter(x=>x.cmd.startsWith(term));
  if(!filtered.length){box.classList.remove('on');return}
  suggestIdx=Math.min(suggestIdx,filtered.length-1);
  box.innerHTML=filtered.map((c,i)=>'<button data-cmd="'+c.cmd+' " class="'+(i===suggestIdx?'sel':'')+'">'+
    '<i class="fa-solid '+c.i+'"></i><b>'+c.cmd+'</b><small>'+c.d+'</small></button>').join('');
  box.classList.add('on');
}
function moveSuggestion(dir){
  const box=$('#suggest');
  if(!box)return;
  const btns=$$('button',box); if(!btns.length)return;
  suggestIdx=(suggestIdx+dir+btns.length)%btns.length;
  btns.forEach((b,i)=>b.classList.toggle('sel',i===suggestIdx));
}
function applySuggestion(){
  const box=$('#suggest');
  if(!box)return;
  const sel=$('.sel',box);
  if(sel&&input){input.value=sel.dataset.cmd;autoGrow(input);box.classList.remove('on');
    input.setSelectionRange(input.value.length,input.value.length);suggestIdx=0}
}
$('#suggest').addEventListener('click',e=>{
  const b=e.target.closest('[data-cmd]');if(!b||!input)return;
  input.value=b.dataset.cmd;autoGrow(input);$('#suggest').classList.remove('on');input.focus();suggestIdx=0;
});

$('#cCmd').onclick=e=>{e.stopPropagation();const cp=$('#cmdPop');if(cp)cp.classList.toggle('open')};
$('#cmdPop').addEventListener('click',e=>{const b=e.target.closest('[data-cmd]');if(!b||!input)return;
  const c=b.dataset.cmd; if(c==='/clear'){$('#clearSession').click()} else {input.value=c;autoGrow(input);input.focus()}
  const cp=$('#cmdPop');if(cp)cp.classList.remove('open')});
document.addEventListener('click',e=>{if(!e.target.closest('#cmdPop')&&!e.target.closest('#cCmd')){const cp=$('#cmdPop');if(cp)cp.classList.remove('open')}});
/* Модель сессии — реальная блокировка на шлюзе, а не только локальная запись:
   раньше выбор менял подпись в футере, но тело хода всегда шло без модели,
   поэтому сервер продолжал работать на модели из конфига. */
$('#fModel').onchange=async e=>{
  const s=active(); if(!s)return;
  const model=e.target.value, prev=s.model||S().agent.model;
  s.model=model; save(); updCost();
  try{
    const sid=await Gateway.ensureBackend(s);
    if(!sid)throw new Error('шлюз недоступен');
    await API.sessions.setModel(sid,model);
    toast('Модель сессии: '+model,'ok');
  }catch(err){
    s.model=prev; e.target.value=prev||S().agent.model; save(); updCost();
    toast('Модель не применена: '+((err&&err.message)||err),'err');
  }
};
$('#fMode').onchange=e=>{const s=active();if(s){s.mode=e.target.value;save();S().agent.mode=e.target.value}};

/* ============================================================
   ПРИКРЕПЛЕНИЕ ФАЙЛОВ С ТОЧНЫМ ПУТЕМ
   Кнопка скрепки открывает нативный диалог на СЕРВЕРЕ (/api/fs/pick).
   Так мы сразу получаем абсолютный путь — в браузере <input type=file>
   путь не отдаёт (f.path === undefined), и агент видел только имя файла.
   ============================================================ */
async function pickFilesViaServer(opt){
  opt=opt||{};
  /* Возврат: массив — ок; null — пользователь отменил; false — диалог
     недоступен, вызывающий код решает, открывать ли браузерный выбор. */
  if(IS_FILE_PROTOCOL){ toast('Выбор файлов доступен при запуске через веб-сервер','err'); return false }
  let resp=null;
  try{
    resp=await fetch('/api/fs/pick',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({title:opt.title||'Выберите файлы для агента',types:opt.types||[]})});
  }catch(e){ toast('Не удалось открыть диалог выбора файлов','err'); return false }
  if(!resp.ok){ toast('Диалог выбора файлов недоступен','err'); return false }
  const data=await resp.json().catch(()=>null);
  const paths=(data&&data.paths)||[];
  if(!paths.length)return null;                       /* пользователь отменил */
  const added=[];
  for(const p of paths){
    const name=p.split(/[\\/]/).pop();
    const att={name:name,size:0,type:guessMime(name),path:p,isDir:false};
    /* Картинки читаем в dataURL — так они уходят модели как vision-вход */
    if(att.type.startsWith('image/')){
      try{
        const r=await fetch('/api/fs/read-data-url?path='+encodeURIComponent(p));
        if(r.ok){ const d=await r.json(); if(d&&d.dataUrl)att.dataUrl=d.dataUrl }
      }catch(_){ /* без dataURL всё равно есть путь */ }
    }
    ui.atts.push(att); added.push(att);
  }
  renderAtts();
  /* Сразу подтягиваем содержимое текстовых файлов — агент увидит данные сразу */
  fetchFileContents(added).then(()=>renderAtts());
  toast(added.length>1?'Файлы прикреплены: '+added.length:'Файл прикреплён','ok');
  return added;
}
function guessMime(name){
  const e=String(name||'').split('.').pop().toLowerCase();
  const m={png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',gif:'image/gif',webp:'image/webp',
           bmp:'image/bmp',svg:'image/svg+xml',ico:'image/x-icon',avif:'image/avif'};
  return m[e]||'application/octet-stream';
}

/* Скрепка: по умолчанию — диалог на сервере (даёт точный путь для агента).
   Если он недоступен (file:// или сбой), откатываемся на браузерный
   выбор — тот тоже работает, но путь придётся искать автопоиском. */
$('#cFile').onclick=async()=>{
  const r=await pickFilesViaServer({title:'Выберите файлы для агента'});
  if(r===false) $('#filePick').click();   /* диалог недоступен → браузерный */
};
$('#cImage').onclick=()=>$('#imgPick').click();
$('#cFolder').onclick=async()=>{
  if(IS_FILE_PROTOCOL){ toast('Не работает через file://','err'); return }
  const h=await pickDir(); if(!h)return;
  const abs=await resolveDirAbs(h.name);
  if(abs)ui.atts.push({name:h.name+'/',size:0,type:'folder',path:abs,isDir:true});
  else ui.atts.push({name:h.name+'/',size:0,type:'folder',path:null,isDir:true});
  renderAtts();
};
$('#filePick').onchange=async e=>{
  Array.from(e.target.files).forEach(f=>{
    const att={name:f.name,size:f.size,type:f.type,file:f,
      path:f.path||f.webkitRelativePath||null,isDir:false};
    // если изображение, читаем dataURL
    if(f.type.startsWith('image/')){
      const reader=new FileReader();
      reader.onload=ev=>{ att.dataUrl=ev.target.result; renderAtts(); };
      reader.readAsDataURL(f);
    }
    ui.atts.push(att);
  });
  renderAtts(); e.target.value='';
  await resolveFilePaths(ui.atts); renderAtts();
};
$('#imgPick').onchange=async e=>{
  Array.from(e.target.files).forEach(f=>{
    const att={name:f.name,size:f.size,type:f.type,file:f};
    const reader=new FileReader();
    reader.onload=ev=>{ att.dataUrl=ev.target.result; renderAtts(); };
    reader.readAsDataURL(f);
    ui.atts.push(att);
  });
  e.target.value='';
  await resolveFilePaths(ui.atts); renderAtts();
};
/* Вложения: под именем показываем полный путь — так сразу видно, что
   агент получит. Без пути чип подсвечивается: значит файл агенту недоступен.
   Клик по чипу (без крестика) открывает файл в панели просмотра. */
function renderAtts(){ const at=$('#atts');if(!at)return;
  at.innerHTML=ui.atts.map((a,i)=>{
    const warn=a.unresolved&&!a.path&&!a.content;
    const ic=a.isDir?'fa-folder':(a.type&&a.type.startsWith('image/')?'fa-regular fa-image':'fa-file');
    const pathRow=(a.path||a.unresolved)
      ? '<span class="att-path'+(warn?' bad':'')+'" title="'+esc(a.path||'путь не найден')+'">'
        +esc(warn?'⚠ путь не найден — агент не сможет открыть':a.path)+'</span>' : '';
    if(a.dataUrl){
      return '<div class="img-att"><img src="'+a.dataUrl+'"'+(a.path?' data-attopen="'+i+'" title="'+esc(a.path)+'"':'')+'>'+
        pathRow+
        '<span class="rm" data-rm="'+i+'"><i class="fa-solid fa-xmark"></i></span></div>';
    }
    return '<span class="chip att-chip'+(warn?' warn':'')+'"'+(a.path?' data-attopen="'+i+'" title="'+esc(a.path)+'"':'')+'>'+
      '<i class="fa-solid '+(a.type&&a.type.startsWith('image/')?'fa-regular fa-image':ic)+'"></i>'+
      '<span class="att-wrap"><span class="att-name">'+esc(a.name)+'</span>'+pathRow+'</span>'+
      (a.size?'<span style="opacity:.6">'+fmtBytes(a.size)+'</span>':'')+
      '<i class="fa-solid fa-xmark x" data-rm="'+i+'"></i></span>';
  }).join('');
}
$('#atts').addEventListener('click',e=>{
  const x=e.target.closest('[data-rm]');
  if(x){ ui.atts.splice(+x.dataset.rm,1); renderAtts(); return }
  const o=e.target.closest('[data-attopen]');
  if(o){
    const a=ui.atts[+o.dataset.attopen];
    if(a&&a.path&&!a.isDir&&typeof openFilePanel==='function'){ e.preventDefault(); openFilePanel(a.path) }
  }
});

/* SNIPPETS */
$('#cSnip').onclick=e=>{
  e.stopPropagation();
  const sp=$('#snipPop'); if(sp) sp.classList.toggle('open');
  renderSnippets();
};
$('#snipAdd').onclick=async()=>{
  const v=input?input.value.trim():'';
  if(!v){ toast('Поле пустое','err'); return }
  const r=await askForm({title:'Сохранить сниппет',icon:'fa-book-open',ok:'Сохранить',fields:[
    {name:'name',label:'Название',ph:'Мой промпт'},
    {name:'desc',label:'Описание',ph:'Что делает'}]});
  if(r&&r.name){
    S().snippets=S().snippets||[];
    S().snippets.push({id:uid('snip'),name:r.name,desc:r.desc||'',text:v,used:0});
    save(); renderSnippets(); toast('Сниппет сохранён','ok');
  }
};
function renderSnippets(){
  const list=$('#snipList'); if(!list) return;
  const snips=S().snippets||[];
  if(!snips.length){ list.innerHTML='<div class="empty" style="padding:14px"><p>Нет сохранённых сниппетов. Введите текст и нажмите «Добавить текущий».</p></div>'; return }
  list.innerHTML=snips.map(s=>'<div class="snip-row" data-snip="'+s.id+'">'+
    '<i class="fa-solid fa-book-open"></i>'+
    '<div class="t"><b>'+esc(s.name)+'</b><small>'+esc(s.desc||s.text.slice(0,50))+'</small></div>'+
    '<button class="del" data-snipsel="'+s.id+'"><i class="fa-regular fa-trash-can"></i></button>'+
    '</div>').join('');
  list.onclick=e=>{
    const del=e.target.closest('[data-snipsel]');
    if(del){ S().snippets=S().snippets.filter(x=>x.id!==del.dataset.snipsel); save(); renderSnippets(); return }
    const row=e.target.closest('[data-snip]');
    if(row){
      const s=S().snippets.find(x=>x.id===row.dataset.snip);
      if(!s)return;
      if(input){ input.value=(input.value?input.value+'\n':'')+s.text; autoGrow(input); updTok(); input.focus(); }
      s.used=(s.used||0)+1; save();
      const sp=$('#snipPop'); if(sp) sp.classList.remove('open');
    }
  };
}

/* DRAG & DROP */
let dragCounter=0;
function attachDrop(target,onDrop){
  if(!target) return;
  target.addEventListener('dragenter',e=>{e.preventDefault();dragCounter++;target.classList.add('drag-over');const dov=$('#dropOverlay');if(dov)dov.classList.add('active')});
  target.addEventListener('dragleave',e=>{e.preventDefault();dragCounter--;if(dragCounter<=0){dragCounter=0;target.classList.remove('drag-over');const dov=$('#dropOverlay');if(dov)dov.classList.remove('active')}});
  target.addEventListener('dragover',e=>{e.preventDefault();e.dataTransfer.dropEffect='copy'});
  target.addEventListener('drop',e=>{e.preventDefault();dragCounter=0;target.classList.remove('drag-over');const dov=$('#dropOverlay');if(dov)dov.classList.remove('active');
    onDrop(e.dataTransfer?e.dataTransfer.files:null,e.dataTransfer)});
}
/* Перенос из панели «Файлы проекта»: путь лежит в dataTransfer, а не в
   файлах ОС. Поэтому onDrop получает сам dataTransfer и достаёт путь
   оттуда — переменная ui.fsbDrag могла уже очиститься на dragend. */
function pathFromTransfer(dt){
  if(!dt)return null;
  try{
    const raw=dt.getData('application/x-infinity-path');
    if(raw){ const d=JSON.parse(raw); if(d&&d.path)return d }
  }catch(_){}
  /* запасной вариант: обычный текст, если это похоже на путь */
  try{
    const t=String(dt.getData('text/plain')||'').trim();
    if(t&&/^[A-Za-z]:[\\/]|^\/|^\.\.?[\\/]/.test(t)) return {path:t,isDir:false};
  }catch(_){}
  return null;
}
attachDrop(document.body,files=>{
  if(!ui.chatOpen){openChat();setTimeout(()=>handleFiles(files),300)} else handleFiles(files);
});
attachDrop($('#cbox'),handleFiles);
/* Drag&drop из проводника И из панели «Файлы проекта»: если переносился путь
   с панели, вложением становится путь на диске (пункт 6 error.txt). */
function handleFiles(files,dt){
  let added=0;
  /* 1) Перенос из панели файлов проекта: путь в dataTransfer.
     ui.fsbDrag — запасной вариант, если dataTransfer недоступен. */
  const fromPanel=pathFromTransfer(dt)||ui.fsbDrag;
  if(fromPanel&&fromPanel.path){
    /* Пока файл перетаскивают, агент мог не успеть обработать drop — вот и
       причина «не сработало»: счётчик сбрасывался на dragend раньше drop.
       Теперь путь живёт в dataTransfer, но подстрахуемся и через ui.fsbDrag. */
    attachPathToChat(fromPanel.path,!!fromPanel.isDir);
    if(!files||!files.length) return;
  }
  if(ui.fsbDrag)ui.fsbDrag=null;
  /* 2) Обычные файлы из ОС */
  if(files)Array.from(files).forEach(f=>{
    const att={name:f.name,size:f.size,type:f.type,file:f,
      path:f.path||f.webkitRelativePath||null,isDir:false};
    if(f.type&&f.type.startsWith('image/')){
      const reader=new FileReader();
      reader.onload=ev=>{ att.dataUrl=ev.target.result; renderAtts(); };
      reader.readAsDataURL(f);
    }
    ui.atts.push(att); added++;
  });
  if(added){ renderAtts(); toast('Добавлено: '+added,'ok'); }
  resolveFilePaths(ui.atts).then(()=>renderAtts());
}

function setSend(){ const b=$('#cSend');
  if(!b)return;
  b.classList.toggle('stop',ui.generating);
  b.innerHTML='<i class="fa-solid '+(ui.generating?'fa-stop':'fa-paper-plane')+'"></i>';
  b.title=ui.generating?'Остановить':'Отправить';
  SG.refresh(); MG.refresh(); }   /* орбы: агент в работе → «горячий» режим */
/* Стоп на ходе должен гасить и голос: озвучка на лету успела набрать очередь,
   и без ttsStop() агент продолжал бы говорить уже после остановки генерации.
   Проверка ui.generating одна не годится: к моменту нажатия генерация часто
   уже кончилась, а очередь озвучки ещё доигрывает — тогда кнопка вместо стопа
   отправляла бы новый запрос. LiveTTS.busy() ловит и это состояние. */
function chatStopOrSend(){
  const liveBusy=(typeof LiveTTS!=='undefined')&&LiveTTS&&LiveTTS.busy&&LiveTTS.busy();
  if(ui.generating||liveBusy){ui.abort=true;ttsStop();setStatus('Остановлено');toast('Остановлено','info')}
  else submit();
}
$('#cSend').onclick=chatStopOrSend;
function submitText(t){ if(input){input.value=t} submit() }
/* async из-за перехватчиков: нужно дождаться сопоставления и выполнения
   скрипта, чтобы понять, уходит сообщение агенту или обработано командой. */
async function submit(){
  const text=input?input.value.trim():'';
  if(!text)return;
  /* «!команда» — прямой запуск в консоли без хода модели (Настройки → Инструменты) */
  const shPref=String((S().tools||{}).shellPrefix||'!');
  if((S().tools||{}).shellCmd!==false&&shPref&&text.startsWith(shPref)){
    const rest=text.slice(shPref.length).trim();
    if(rest) runShellCmd(rest);
    else toast('Формат: '+shPref+'<команда>, например '+shPref+'dir','info');
    if(input){input.value='';delete input.dataset.base;autoGrow(input)}
    return;
  }
  const s=active()||newSession();
  /* Перехватчики проверяем ДО очереди и перебивания: команда должна сработать
     даже когда агент занят, и не должна ждать конца хода. */
  if(await icTryIntercept(text))return;
  // === РЕЖИМ ЗАНЯТОГО АГЕНТА: очередь или перебивание ===
  if(ui.generating){
    const busyMode=S().agent.busyMode||'queue';
    if(busyMode==='interrupt'){
      // Перебить: останавливаем текущий ход, сообщение отправится сразу после
      ui.abort=true;
      // Оборванный ход не пришлёт run.completed, поэтому очередь живой
      // озвучки надо погасить здесь — иначе она договорит прежний ответ.
      ttsStop();
      ui.pendingInterrupt={text};
      setStatus('Перебиваю…');
      toast('Агент будет перебит — сообщение отправляется','info');
      if(input){input.value='';delete input.dataset.base;autoGrow(input)}
      return;
    }
    // Режим «Очередь»: копим сообщения, отправим после завершения хода
    ui.msgQueue.push({text,sessionId:s.id});
    if(input){input.value='';delete input.dataset.base;autoGrow(input)}
    /* Системную строку «➕ Добавлено в очередь» в ленте убрали по слову
       пользователя: очередь теперь видна как полоса над вводом (renderQueueStrip),
       а счётчик в статусной строке остаётся, чтобы попадание было не тихим. */
    setStatus('В очереди: '+ui.msgQueue.length+' · последний ≈ через '+fmtEta(queueEtaMs(ui.msgQueue.length)));
    save(); renderAll(); renderQueueChip();
    return;
  }
  if(text.startsWith('/')&&!text.includes(' ')){
    // проверим кастомную команду
    const custom=S().commands.find(c=>c.name===text);
    if(custom){
      const finalText=custom.template.replace(/\{\{input\}\}/g,'').replace(/\{\{date\}\}/g,new Date().toLocaleDateString('ru-RU'));
      input.value=finalText;
      autoGrow(input);
      // рекурсивный submit
      ui.recentCmds=[text,...ui.recentCmds.filter(c=>c!==text)].slice(0,10);
      submit();
      return;
    }
    ui.recentCmds=[text,...ui.recentCmds.filter(c=>c!==text)].slice(0,10);
  }

  // обработка переменных в кастомных командах с {{input}}
  let finalText=text;
  const custom=S().commands.find(c=>text.startsWith(c.name+' '));
  if(custom){
    const rest=text.slice(custom.name.length).trim();
    finalText=custom.template.replace(/\{\{input\}\}/g,rest).replace(/\{\{date\}\}/g,new Date().toLocaleDateString('ru-RU'));
  }

  const pcmd=PluginBus.commands().find(c=>text===c.name||text.startsWith(c.name+' '));
  if(pcmd){
    ui.recentCmds=[text,...ui.recentCmds.filter(c=>c!==text)].slice(0,10);
    const rest=text.slice(pcmd.name.length).trim();
    Promise.resolve(pcmd.run(rest,{session:active()})).then(res=>{
      if(typeof res==='string'&&res.trim()){ if(input){input.value=res;autoGrow(input)} submit(); }
      else if(res&&typeof res==='object'&&res.reply!==undefined){ const s3=active(); if(s3){ msg(s3,{role:'agent',kind:'text',content:String(res.reply),status:'done',meta:{model:'плагин · '+pcmd.plugin}}); save(); renderAll(); } if(input){input.value='';autoGrow(input)} }
      else if(input){ input.value=''; autoGrow(input) }
    }).catch(e=>{ console.warn(e); toast('Ошибка плагина','err') });
    return;
  }

  SG.fire();                                   /* желе-волна, всплеск искр и кольцо на кнопке отправки */
  dispatchUserText(finalText,{origin:'chat'});
}

/* ============================================================
   ОБЩИЙ ПУТЬ ОТПРАВКИ СООБЩЕНИЯ АГЕНТУ
   Используется чатом (origin:'chat') и голосом с главного экрана
   (origin:'stage'). Вариант 'stage' не трогает поле ввода чата, вложения и
   «ответ на сообщение»: фраза уже показана в реплике #stageSay, а ответ
   агента возвращается туда же через stageReply() по завершении хода.
   ============================================================ */
function dispatchUserText(text,opts){
  opts=opts||{};
  const fromStage=opts.origin==='stage';
  let finalText=String(text||'').trim();
  if(!finalText)return null;

  /* Страховка: пока агент работает, ход с главного экрана не стартует параллельно —
     фраза уходит через ту же логику перебивания/очереди, что и ввод в чате. */
  if(fromStage&&ui.generating){
    const busyMode=S().agent.busyMode||'queue';
    if(busyMode==='interrupt'){ ui.abort=true; ui.pendingInterrupt={text:finalText}; setStatus('Перебиваю…'); }
    else { const bs=active()||newSession(); ui.msgQueue.push({text:finalText,sessionId:bs.id}); setStatus('Агент занят — фраза в очереди · '+fmtEta(queueEtaMs(ui.msgQueue.length))); }
    voiceWait();
    return finalText;
  }

  const hooked=PluginBus.emit('send',finalText);
  if(typeof hooked==='string'&&hooked.trim()) finalText=hooked;

  const s=active()||newSession();
  if(s.messages.filter(m=>m.role==='user').length===0&&s.title.startsWith('Новая сессия'))
    s.title=finalText.length>42?finalText.slice(0,42)+'…':finalText;

  const atts=fromStage?[]:ui.atts.map(a=>({name:a.name,size:a.size,type:a.type,dataUrl:a.dataUrl,
    path:a.path||null,isDir:!!a.isDir}));
  msg(s,{role:'user',kind:'text',content:finalText,meta:{atts,replyTo:fromStage?undefined:ui.replyTo?.text}});

  // счётчик input-токенов (примерно)
  s.costIn=(s.costIn||0)+Math.round(finalText.length/3.4);

  // добавить в историю
  S().promptHistory=S().promptHistory||[];
  S().promptHistory.unshift({text:finalText,ts:Date.now(),sessId:s.id});
  if(S().promptHistory.length>100) S().promptHistory=S().promptHistory.slice(0,100);

  if(!fromStage){
    ui.atts=[]; renderAtts(); if(input){input.value='';delete input.dataset.base;autoGrow(input)} updTok();
    localStorage.removeItem('draft.'+s.id); const dp=$('#draftPill');if(dp)dp.classList.remove('on');
    // очистить reply
    ui.replyTo=null; const rb=$('#replyBar'); if(rb) rb.classList.remove('on');
  }
  save();
  /* Отправка сообщения всегда возвращает чат вниз — к свежему сообщению
     и началу ответа агента (иначе при выключенном «липком низе» остались
     бы вверху, где ничего не происходит). */
  ui.stickBottom=true; updScrollBtn();
  renderAll();
  // Передаем attachments в agentTurn
  ui._errorStartNoRetry=null; // ручная отправка — пользователь онлайн, лимит попыток сброшен
  if(fromStage){ stageTurn=true; ui.stageAborted=false }  // ответ прилетит в реплику главного экрана
  /* Режим «Диалог» — прямой чат с моделью, минуя агента Hermes (без инструментов
     и памяти). Работает и для чата, и для голоса с главного экрана. */
  if((s.mode||S().agent.mode)==='chat') directTurn(finalText,atts);
  else agentTurn(finalText,atts);
  return finalText;
}

/* ============================================================
   КОНСОЛЬ В ЧАТЕ: «!команда» → /api/exec
   Команда выполняется локально на машине сервера Infinity, модель не
   вызывается (ни токенов, ни хода), а гейт подтверждения — тот же, что у
   терминального инструмента агента.
   ============================================================ */
function shPrefix(){ return String((S().tools||{}).shellPrefix||'!') }
async function execPost(cmd,cwd,timeout,force){
  const r=await fetch('/api/exec',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:cmd,cwd:cwd,timeout:timeout,force:!!force})});
  let j=null; try{ j=await r.json() }catch(_){}
  return {status:r.status,j:j||{}};
}
async function runShellCmd(cmd){
  const s=active()||newSession();
  const proj=projectOf(s);
  const cwd=(proj&&(proj.absPath||proj.path))||state.gatewayCwd||'';
  const to=Number((S().tools||{}).shellTimeout||60);
  msg(s,{role:'user',kind:'text',content:shPrefix()+cmd,meta:{shell:cmd}});
  const out=msg(s,{role:'agent',kind:'shell',content:'',status:'running',meta:{cmd:cmd,cwd:cwd}});
  save(); renderAll();
  try{
    let res=await execPost(cmd,cwd,to,false);
    if(res.status===409){                      /* гейт: спрашиваем подтверждение, как в CLI */
      out.status='done'; out.meta.code=-1;
      out.content=(res.j.message||'Команда требует подтверждения')+'\n\nПодтвердите запуск.';
      patch(out); renderAll();
      if(!confirm('Выполнить команду?\n\n'+cmd+'\n\n'+((res.j&&res.j.message)||''))){
        out.content+='\n[отменено пользователем]'; out.meta.code=130; patch(out); toast('Отменено','info'); return }
      out.status='running'; out.content=''; out.meta.code=null; patch(out);
      res=await execPost(cmd,cwd,to,true);
    }
    if(res.status>=400&&!res.j.out){
      out.status='done'; out.meta.code=-1;
      out.content='Ошибка: '+((res.j&&(res.j.error||res.j.message))||('HTTP '+res.status));
      patch(out); renderAll(); toast('Команда не выполнена','err'); return;
    }
    out.status='done'; out.meta.code=Number(res.j.code||0);
    out.meta.dur=res.j.dur||0; out.meta.cwd=res.j.cwd||cwd; out.meta.truncated=!!res.j.truncated;
    out.content=String(res.j.out||'').replace(/\s+$/,'')||'(пустой вывод)';
    patch(out); renderAll();
    toast('Команда завершена: код '+out.meta.code+' · '+out.meta.dur+' c',out.meta.code===0?'ok':'err');
  }catch(e){
    out.status='done'; out.meta.code=-1; out.content='Ошибка запуска: '+((e&&e.message)||e);
    patch(out); renderAll(); toast('Ошибка запуска команды','err');
  }
}
/* Кнопки «В поле» / «Копировать» внутри блока консоли */
document.addEventListener('click',e=>{
  const btn=e.target.closest('[data-shsend],[data-shcopy]'); if(!btn)return;
  const id=btn.dataset.shsend||btn.dataset.shcopy;
  const s=active(); const m=s&&s.messages.find(x=>x.id===id); if(!m)return;
  if(btn.dataset.shcopy){ try{navigator.clipboard.writeText(String(m.content||''))}catch(_){} toast('Скопировано','ok'); return }
  if(input){ input.value='Вывод команды `'+String((m.meta||{}).cmd||'')+'`:\n```\n'+String(m.content||'')+'\n```';
    delete input.dataset.base; autoGrow(input); input.focus(); toast('Вывод вставлен в поле','info') }
});

/* PROMPT HISTORY */
$('#hdSessMore').addEventListener('contextmenu',e=>{});
function openHistPop(){
  const hp=$('#histPop'); if(hp) hp.classList.add('open');
  renderHistList('');
  setTimeout(()=>{const i=$('#histSearch');if(i)i.focus()},50);
}
$('#histSearch').addEventListener('input',e=>renderHistList(e.target.value.trim().toLowerCase()));
$('#histClose').onclick=()=>{const hp=$('#histPop');if(hp)hp.classList.remove('open')};
function renderHistList(q){
  const list=$('#histList'); if(!list)return;
  let hist=S().promptHistory||[];
  if(q) hist=hist.filter(h=>h.text.toLowerCase().includes(q));
  if(!hist.length){ list.innerHTML='<div class="empty"><p>История пуста</p></div>'; return }
  list.innerHTML=hist.map((h,i)=>'<div class="hist-row" data-hidx="'+i+'">'+
    '<div class="top"><b>Промпт</b><small>'+tShort(h.ts)+'</small></div>'+
    '<p>'+esc(h.text)+'</p></div>').join('');
  list.onclick=e=>{
    const row=e.target.closest('[data-hidx]'); if(!row)return;
    const h=hist[+row.dataset.hidx]; if(!h)return;
    if(input){input.value=h.text;autoGrow(input);updTok();input.focus();}
    const hp=$('#histPop');if(hp)hp.classList.remove('open');
  };
}
