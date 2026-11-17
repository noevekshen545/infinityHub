/* ============ НАСТРОЙКИ: ПОЛЬЗОВАТЕЛЬСКИЕ ДВИЖКИ ГОЛОСА ============
   Список движков лежит в S().voice.engines. Активный выбирается отдельными
   полями voice.sttEngineId / voice.ttsEngineId: так один и тот же движок
   можно назначить и на распознавание, и на синтез, и не заводить дубликаты. */
function engList(){ const v=S().voice||(S().voice={});
  if(!Array.isArray(v.engines))v.engines=[]; return v.engines }
function engActive(kind){
  const v=S().voice||{};
  const id=v[kind==='stt'?'sttEngineId':'ttsEngineId'];
  return engList().find(e=>e.id===id)||null;
}
/* Активный движок в виде того объекта, который ждёт voiceEngineCfg. */
function engActiveCfg(kind){
  const e=engActive(kind);
  if(!e)return null;
  const local=kind==='stt'?'whisper':'silero';
  if(e.id==='local')return {mode:'local',engine:local};
  return {mode:e.mode||'openai',url:e.url,apiKey:e.apiKey||'',model:e.model||'',
          voice:e.voice||'',fmt:e.fmt||'',lang:e.lang||''};
}
function renderVoiceEngines(){
  const sb=$('#engSttBox'), tb=$('#engTtsBox');
  if(!sb||!tb)return;
  const list=engList(), v=S().voice||{};
  const opt=cur=>'<option value="">— локальный —</option>'+
    list.map(e=>'<option value="'+esc(e.id)+'"'+(cur===e.id?' selected':'')+'>'+esc(e.name||e.url)+'</option>').join('');
  const localName=kind=>kind==='stt'?'Whisper (локально)':'Silero (локально)';
  const active=kind=>{ const a=engActive(kind);
    return a? (a.id==='local'?localName(kind):(a.name||a.url)) : localName(kind) };
  sb.innerHTML='<div class="srow"><div class="txt"><b>Движок распознавания</b>'+
    '<small>Сейчас: '+esc(active('stt'))+'</small></div><div class="ctl">'+
    '<select class="field" data-engsel="stt">'+opt(v.sttEngineId)+'</select>'+
    '<button class="btn sm" data-engtest="stt" title="Проверить движок"><i class="fa-solid fa-stethoscope"></i></button></div></div>';
  tb.innerHTML='<div class="srow"><div class="txt"><b>Движок синтеза</b>'+
    '<small>Сейчас: '+esc(active('tts'))+'</small></div><div class="ctl">'+
    '<select class="field" data-engsel="tts">'+opt(v.ttsEngineId)+'</select>'+
    '<button class="btn sm" data-engtest="tts" title="Проверить движок"><i class="fa-solid fa-stethoscope"></i></button></div></div>'+
    (list.length?list.map((e,i)=>
      '<div class="cmdrow"><span class="cmd-name">'+esc(e.name||e.url)+'</span>'+
      '<span class="cmd-desc">'+esc(e.mode==='custom'?'свой формат':'OpenAI-совместимый')+'</span>'+
      '<button class="icon-btn sm" data-engeedit="'+i+'" title="Изменить"><i class="fa-solid fa-pen"></i></button>'+
      '<button class="icon-btn sm" data-engdel="'+i+'" title="Удалить"><i class="fa-regular fa-trash-can"></i></button></div>').join(''):'');
}
async function editEngine(idx){
  const e=idx!=null?engList()[idx]:null;
  const v=await askForm({title:idx!=null?'Изменить движок':'Новый движок',icon:'fa-plug-circle-bolt',
    ok:'Сохранить',fields:[
    {name:'name',label:'Название',value:e?.name||'',ph:'Мой Whisper на сервере'},
    {name:'mode',label:'Формат: openai или custom',value:e?.mode||'openai',ph:'openai'},
    {name:'url',label:'Адрес (endpoint)',value:e?.url||'',ph:'https://api.openai.com/v1/audio/speech'},
    {name:'apiKey',label:'Ключ API',value:e?.apiKey||'',ph:'sk-...'},
    {name:'model',label:'Модель',value:e?.model||'',ph:'whisper-1 / gpt-4o-mini-tts'},
    {name:'voice',label:'Голос (только синтез)',value:e?.voice||'',ph:'alloy / xenia / o'},
    {name:'fmt',label:'Формат аудио (только синтез)',value:e?.fmt||'',ph:'mp3 / wav / ogg-opus'},
    {name:'lang',label:'Язык (только custom-распознавание)',value:e?.lang||'',ph:'ru'}]});
  if(!v||!v.url)return;
  const rec={id:e?.id||uid('eng'),name:v.name||v.url,mode:/^custom/i.test(v.mode)?'custom':'openai',
    url:v.url.trim(),apiKey:v.apiKey||'',model:v.model||'',voice:v.voice||'',fmt:v.fmt||'',lang:v.lang||''};
  if(e)engList()[idx]=rec; else engList().push(rec);
  save(); renderVoiceEngines(); toast('Сохранено','ok');
}
function bindVoiceEngines(){
  const box=$('#engSttBox')&&$('#engSttBox').closest('.group');
  if(!box)return;
  const root=box.parentElement;
  root.addEventListener('change',e=>{
    const sel=e.target.closest('[data-engsel]');
    if(!sel)return;
    const kind=sel.dataset.engsel;
    /* Пустое значение = локальный движок. Отдельного id не заводим:
       voiceEngineCfg трактует отсутствие url как «локальный». */
    S().voice[kind==='stt'?'sttEngineId':'ttsEngineId']=sel.value||'';
    save(); renderVoiceEngines();
    toast(sel.value?'Движок выбран':'Используется локальный движок','info');
  });
  root.addEventListener('click',e=>{
    const t=e.target.closest('[data-engtest]');
    if(t){ voiceEngineCheck(t.dataset.engtest,t); return }
    const ed=e.target.closest('[data-engeedit]');
    if(ed){ editEngine(+ed.dataset.engeedit); return }
    const dl=e.target.closest('[data-engdel]');
    if(dl){
      const i=+dl.dataset.engdel, id=engList()[i].id;
      engList().splice(i,1);
      for(const k of ['sttEngineId','ttsEngineId']) if(S().voice[k]===id)S().voice[k]='';
      save(); renderVoiceEngines(); toast('Движок удалён','ok'); return;
    }
    if(e.target.closest('#engAdd'))editEngine(null);
  });
}
