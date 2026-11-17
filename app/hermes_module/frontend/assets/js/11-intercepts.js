/* КОМАНДЫ-ПЕРЕХВАТЧИКИ
   Ловят фразу пользователя ДО отправки агенту и выполняют заранее написанный
   скрипт. Ключевое отличие от /команд и «!»: ни модель, ни агент, ни режим
   «Диалог» не вызываются вообще — ни одного запроса к шлюзу не уходит.
   Поэтому проверка стоит раньше ветки с очередью и перебиванием: перехватчик
   срабатывает даже когда агент занят. */

/* Нормализация фразы: регистр, лишние пробелы, «ё», знаки препинания по краям.
   Без неё «Покажи  погоду » и «покажи погоду» считались бы разными. */
function icNorm(t){
  return String(t||'').toLowerCase().replace(/ё/g,'е').replace(/\s+/g,' ').trim()
          .replace(/[.!?,;:]+$/,'').trim();
}
/* Коэффициент сходства по биграммам (Dice). 1 — совпадение, 0 — ничего общего.
   Нужен для нечёткого режима: речь и опечатки дают «покажи погоду»,
   «покaжи погоду», «покажите погоду» — точное сравнение их уже не узнает. */
function icDice(a,b){
  if(a===b)return 1;
  if(a.length<2||b.length<2)return 0;
  const bg=s=>{ const m=new Map(); for(let i=0;i<s.length-1;i++){ const g=s.slice(i,i+2); m.set(g,(m.get(g)||0)+1) } return m };
  const A=bg(a),B=bg(b); let inter=0;
  A.forEach((n,g)=>{ const m=B.get(g); if(m) inter+=Math.min(n,m) });
  return (2*inter)/(a.length-1+b.length-1);
}
const IC_FUZZY_MIN=0.72;   /* ниже — считаем не тем же самым */


/* Найти перехватчик для фразы. Возвращает {rule, score} или null.
   Точное совпадение по phrase/variants всегда важнее нечёткого, поэтому
   нечёткий режим проверяется вторым проходом: фраза «покажи погоду» не
   должна «съесть» команду, совпавшую с ней лишь приблизительно. */
function icMatch(text){
  const raw=String(text||'').trim();
  if(!raw)return null;
  const list=(S().intercepts||[]).filter(r=>r&&r.enabled!==false&&(r.phrase||'').trim());
  if(!list.length)return null;
  const n=icNorm(raw);
  /* Проход 1: точное совпадение — сама фраза, один из вариантов, либо фраза,
     которой пользователь дописал хвост («покажи погоду в Москве» → args).
     Хвост обязательно отделён пробелом, иначе «покажи погодуа» сочлось бы
     за команду «покажи погоду». */
  for(const r of list){
    const cands=[r.phrase,...(r.variants||[])];
    for(const c of cands){
      const nc=icNorm(c);
      if(nc===n||(nc&&n.startsWith(nc+' '))) return {rule:r,score:1,matched:c};
    }
  }
  /* Проход 2: нечёткое сравнение, только у правил с включённым fuzzy.
     Берём лучшее совпадение, но только если оно явно выше порога. */
  let best=null;
  for(const r of list){
    if(!r.fuzzy) continue;
    for(const c of [r.phrase,...(r.variants||[])]){
      const sc=icDice(n,icNorm(c));
      if(sc>=IC_FUZZY_MIN&&(!best||sc>best.score)) best={rule:r,score:sc,matched:c};
    }
  }
  return best;
}

/* Куда выполнять скрипт: папка активного проекта, иначе последний cwd шлюза. */
function icCwd(){
  const s=active();
  const proj=s?projectOf(s):null;
  return (proj&&(proj.absPath||proj.path))||state.gatewayCwd||'';
}


/* Сам запуск. JS исполняется прямо в браузере, Python — через сервер.
   Возвращает {ok, out, code, dur}. Исключения наружу не бросает. */
async function icRun(rule,argsText){
  let code=String(rule.code||'');
  const path=String(rule.path||'').trim();
  /* Если указан путь к файлу — читаем скрипт с диска: он считается главным
     источником, а текст из формы служит запасным вариантом. Так скрипт можно
     править и запускать, не заходя каждый раз в настройки. */
  if(path){
    try{
      const r=await fetch('/api/intercept/read',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({path:path})});
      const j=await r.json();
      if(j&&j.code) code=String(j.code);
      else if(j&&j.error) return {ok:false,code:1,out:String(j.error),dur:0};
    }catch(e){
      return {ok:false,code:1,out:'Не удалось прочитать файл скрипта: '+((e&&e.message)||e),dur:0};
    }
  }
  if(!code.trim()) return {ok:false,code:1,out:path?('Файл скрипта пуст: '+path):'Скрипт не заполнен',dur:0};
  const args=argsText!==undefined?String(argsText||''):String(rule.args||'');
  if(rule.lang==='js'){
    const t0=performance.now();
    const el=()=>Math.round(performance.now()-t0);
    try{
      /* Скрипт возвращает значение — показываем его. Если ничего не вернул,
         выводим то, что он напечатал в консоль через console.log. */
      const collect=[]; const nativeLog=console.log;
      console.log=(...a)=>{ collect.push(a.map(v=>typeof v==='string'?v:JSON.stringify(v)).join(' ')) };
      let out;
      try{ out=new Function('args','ctx',code)(args,{session:active()}) }
      finally{ console.log=nativeLog }
      const logged=collect.join('\n');
      const text=(out===undefined||out===null)?logged
        :(typeof out==='string'?out:(logged?logged+'\n':'')+JSON.stringify(out,null,2));
      return {ok:true,code:0,out:String(text||'(скрипт ничего не вернул)'),dur:el()};
    }catch(e){
      return {ok:false,code:1,out:'[JS] '+((e&&e.message)||e),dur:el()};
    }
  }
  /* Python — на сервере, отдельным процессом. */
  try{
    const r=await fetch('/api/intercept/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:code,cwd:icCwd(),timeout:Number(rule.timeout)||60,args:args})});
    let j=null; try{ j=await r.json() }catch(_){}
    if(!j) return {ok:false,code:1,out:'Сервер не ответил (HTTP '+r.status+')',dur:0};
    if(j.error) return {ok:false,code:1,out:String(j.error),dur:0};
    return {ok:j.ok!==false,code:Number(j.code||0),out:String(j.out||''),dur:j.dur||0};
  }catch(e){
    return {ok:false,code:1,out:'Не удалось выполнить скрипт: '+((e&&e.message)||e),dur:0};
  }
}

/* Главная точка входа: вызывается из submit() перед отправкой агенту.
   Возвращает true, если сообщение перехвачено — тогда в чат попадает только
   результат скрипта, а ход агента не начинается вовсе. */
async function icTryIntercept(text){
  const hit=icMatch(text);
  if(!hit)return false;
  const rule=hit.rule;
  const raw=String(text||'').trim();
  /* Аргумент скрипту: то, что пользователь дописал после сработавшей фразы.
     «покажи погоду в Москве» при фразе «покажи погоду» даст args='в Москве'.
     Считаем хвост именно от той фразы, которая сработала: если совпал вариант
     «какая погода», а основная фраза другая, резать надо по варианту.
     Если дописывать нечего — уходит args, заданный в настройках. */
  let args=String(rule.args||'');
  const matched=String(hit.matched||rule.phrase||'').trim();
  const nm=icNorm(matched);
  if(nm&&icNorm(raw).startsWith(nm)){
    /* Режем хвост по длине совпавшей фразы, но хвост сам очищаем от знаков
       препинания: иначе «Привет!» передаст скрипту args='!' вместо пустой
       строки, и скрипт решит, что пользователь что-то дописал. */
    const tail=raw.slice(matched.length).trim()
      .replace(/^[.,;:!?\-\u2013\u2014]+/,'').replace(/[.,;:!?\u2013\u2014]+$/,'').trim();
    if(tail) args=tail;
  }else if(hit.score<1){
    /* Нечёткое совпадение: слов не совпало дословно, но фраза явно команда —
       отдаём скрипту текст целиком, пусть сам разбирается. */
    args=raw;
  }
  const s=active()||newSession();
  if(input){input.value='';delete input.dataset.base;autoGrow(input)}
  /* Реплика пользователя видна в чате, но это обычное сообщение: агент её
     не читает и ответа на неё не ждёт. */
  msg(s,{role:'user',kind:'text',content:raw,meta:{intercept:rule.id}});
  const out=msg(s,{role:'agent',kind:'intercept',content:'',status:'running',
    meta:{ruleId:rule.id,name:rule.phrase,lang:rule.lang,score:hit.score}});
  save(); renderAll();
  if(setStatus) setStatus('Команда «'+rule.phrase+'»…');
  const res=await icRun(rule,args);
  out.status='done';
  out.content=res.out||'(пустой вывод)';
  out.meta.code=res.code; out.meta.dur=res.dur; out.meta.ok=res.ok;
  if(setStatus) setStatus('');
  toast(res.ok?('Выполнено: '+rule.phrase):('Ошибка команды: '+rule.phrase),res.ok?'ok':'err');
  return true;
}

/* ============ НАСТРОЙКИ: СПИСОК И РЕДАКТОР ============ */
const IC_SAMPLE_PY=`import json, sys
# args — то, что пользователь дописал после фразы-команды (строка JSON)
try:
    data = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
except Exception:
    data = {"raw": sys.argv[1] if len(sys.argv) > 1 else ""}

city = data.get("raw") or "Москва"
print(f"Погода в городе {city}: +7 °C, переменная облачность.")
print("Ветрено, зонт лучше взять.")`;

const IC_SAMPLE_JS=`// args — строка, дописанная после фразы-команды. ctx.session — сессия.
// console.log тоже попадёт в результат, если скрипт ничего не вернул.
console.log("Привет! args =", args);
return "Готово: " + new Date().toLocaleTimeString("ru-RU");`;

function renderInterceptsSettings(){
  const list=$('#icList');
  if(!list)return;
  const list0=S().intercepts||[];
  if(!list0.length){
    list.innerHTML='<div class="empty" style="padding:22px"><i class="fa-solid fa-bolt"></i>'+
      '<p>Перехватчиков нет</p></div>';
    return;
  }
  list.innerHTML=list0.map(r=>
    '<div class="cmdrow'+(r.enabled===false?'':'')+'">'+
    '<span class="cmd-name">'+esc(r.phrase||'(без фразы)')+'</span>'+
    '<span class="cmd-desc">'+esc(r.desc||'')+'</span>'+
    '<span class="chip tiny'+(r.lang==='js'?' js':' py')+'">'+(r.lang==='js'?'JS':'PY')+'</span>'+
    (r.fuzzy?'<span class="chip tiny">~</span>':'')+
    '<button class="icon-btn sm" data-ictedit="'+r.id+'" title="Изменить"><i class="fa-solid fa-pen"></i></button>'+
    '<button class="icon-btn sm" data-icdel="'+r.id+'" title="Удалить"><i class="fa-regular fa-trash-can"></i></button>'+
    '</div>').join('');
  list.onclick=e=>{
    const del=e.target.closest('[data-icdel]');
    if(del){
      S().intercepts=(S().intercepts||[]).filter(x=>x.id!==del.dataset.icdel);
      save(); renderInterceptsSettings(); toast('Перехватчик удалён','ok'); return;
    }
    const ed=e.target.closest('[data-ictedit]');
    if(ed) editIntercept(ed.dataset.ictedit);
  };
}

/* Редактор перехватчика.
   По требованию: язык и «нечёткое сравнение» вводятся РУКАМИ обычными
   полями, а не выбираются из выпадающего списка — списки тут мешали, когда
   язык скрипта нестандартный. Путь к скрипту тоже свой: можно писать код
   прямо здесь, а можно сослаться на файл на диске. */
async function editIntercept(id){
  const ex=id?(S().intercepts||[]).find(r=>r.id===id):null;
  const v=await askForm({title:id?'Изменить перехватчик':'Новый перехватчик',
    icon:'fa-bolt',ok:'Сохранить',fields:[
    {name:'phrase',label:'Фраза-команда',value:ex?.phrase||'',ph:'покажи погоду'},
    {name:'variants',label:'Варианты фразы (через запятую)',value:(ex?.variants||[]).join(', '),
     ph:'покажи погоду, какая погода, погода на улице'},
    {name:'desc',label:'Описание',value:ex?.desc||'',ph:'Погода из скрипта'},
    {name:'lang',label:'Язык программирования',value:ex?.lang||'python',ph:'python или js'},
    {name:'fuzzy',label:'Нечёткое сравнение',value:ex?(ex.fuzzy?'вкл':'выкл'):'вкл',
     ph:'вкл или выкл'},
    {name:'args',label:'Аргументы по умолчанию',value:ex?.args||'',ph:'Москва'},
    {name:'timeout',label:'Таймаут, с (только для python)',value:String(ex?.timeout||60),ph:'60'},
    {name:'path',label:'Путь к файлу скрипта (необязательно)',type:'path',
     value:ex?.path||'',ph:'C:\\scripts\\weather.py'},
    {name:'code',label:'Скрипт',type:'area',value:ex?ex.code:'',ph:'print("Привет")'}]});
  if(!v||!v.phrase)return;
  /* Варианты: запятая или новая строка, лишние пробелы не важны. */
  const variants=String(v.variants||'').split(/[,\n]/).map(x=>x.trim()).filter(Boolean);
  /* Язык: сравниваем вручную, без списка. Любое значение, начинающееся с js,
     считаем JavaScript, остальное — Python (единственные два движка). */
  const lang=String(v.lang||'').trim().toLowerCase().startsWith('js')?'js':'python';
  /* «Нечёткое сравнение»: принимаем вкл/выкл и их синонимы, чтобы не заставлять
     человека угадывать единственное правильное слово. */
  const fuzzy=/^(вкл|on|true|1|да|yes|y)$/i.test(String(v.fuzzy||'').trim());
  const to=Math.max(1,Math.min(300,parseInt(v.timeout,10)||60));
  if(ex){
    ex.phrase=v.phrase.trim(); ex.variants=variants; ex.desc=v.desc||'';
    ex.lang=lang; ex.fuzzy=fuzzy; ex.args=v.args||'';
    ex.timeout=to; ex.code=v.code||''; ex.path=String(v.path||'').trim();
  }else{
    S().intercepts=S().intercepts||[];
    S().intercepts.push({id:uid('int'),phrase:v.phrase.trim(),variants,desc:v.desc||'',
      lang,fuzzy,args:v.args||'',timeout:to,code:v.code||'',path:String(v.path||'').trim(),
      enabled:true});
  }
  normalizeIntercepts();
  save(); renderInterceptsSettings(); toast('Сохранено','ok');
}


/* Кнопка «Создать перехватчик» живёт в панели «Команды» — он появляется
   только вместе с ней, поэтому вешаем обработчик с проверкой на существование. */
$('#icNew')&&($('#icNew').onclick=()=>editIntercept(null));
