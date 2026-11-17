/* 02-voice.js — Голос: экраны, локальный слух, желе-кнопки */
/* ============================================================
   VOICE — главный экран: «Финик» → фраза → ответ агента
   ============================================================ */
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
let recog=null,recActive=false,target=null;
let recMode='wake';        /* 'wake' (ждём слово) | 'dialog' (слушаем фразу) | 'interrupt' (только «стоп») */
let recWant=false;         /* распознаватель должен работать */
let recPassive=false;      /* работает по инициативе системы, а не пользователя */
let recRunning=false,recRestartT=null;
let SRNetWarned=false;                    /* тост о network-ошибке — один раз */
let recBase='';            /* накопленный финальный текст фразы главного экрана */
let saySilT=null,sayIdleT=null;
let voiceStage='off';      /* off|sleep|listen|think|reply|wait */
let stageTurn=false;       /* текущий ход агента инициирован с главного экрана */
let curTts=null;           /* активная озвучка (аудио или utterance) — для голосового «стоп» */

function normRu(t){ return String(t||'').toLowerCase().replace(/ё/g,'е')
  .replace(/ф[еиё]н[иые]к/g,'финик').replace(/[^\p{L}\p{N}\s]+/gu,' ')
  .replace(/\s+/g,' ').trim() }
function wakePhrase(){ return normRu(S().voice.wakePhrase||'финик') }
/* «Финик» в начале фразы → остаток текста ('' — сказали только слово), иначе null */
function matchWake(text){
  const n=normRu(text),w=wakePhrase(); if(!n||!w)return null;
  const i=n.indexOf(w); if(i<0)return null;
  if(n.slice(0,i).trim().length>24)return null;   /* слово в середине длинной речи не триггер */
  return n.slice(i+w.length).trim();
}
function stripWake(text){ const n=normRu(text),w=wakePhrase(); const i=n.indexOf(w);
  return i<0?n:(n.slice(0,i)+' '+n.slice(i+w.length)).replace(/\s+/g,' ').trim() }
function matchStop(text){ return /(^|\s)(stop|стоп|стой|стойте|остановись|останови|остановить|отмена|отмени|отставить|хватит|замолчи|замолчите|прекрати)(?=\s|$)/.test(normRu(text)) }
/* Из фразы убраны все стоп-слова; null — фраза состоит только из стоп-слов (команда, а не сообщение) */
function stripStop(text){
  const n=normRu(text);
  const s=n.replace(/(^|\s)(stop|стоп|стой|стойте|остановись|останови|остановить|отмена|отмени|отставить|хватит|замолчи|замолчите|прекрати)(?=\s|$)/g,' ').replace(/\s+/g,' ').trim();
  return s?s:null;
}

function initRecog(){
  if(!SR)return null;
  const r=new SR(); r.lang=S().voice.recLang; r.continuous=true; r.interimResults=true; r.maxAlternatives=1;
  r.onresult=onRecResult;
  r.onerror=e=>{ console.warn('[voice] SR error:',e.error); if(e.error==='network'&&!SRNetWarned){SRNetWarned=true;toast('Распознаванию речи нужен интернет (сервис Google)','err')} if(e.error==='audio-capture'){toast('Микрофон не найден — проверьте устройство записи','err');recWant=false;recPassive=false;recActive=false;target=null;voiceSleep(true)} if(e.error==='not-allowed'||e.error==='service-not-allowed'){
      toast('Доступ к микрофону запрещён','err'); recWant=false; recPassive=false; recActive=false; target=null; voiceSleep(true); } };
  r.onend=()=>{ recRunning=false;
    if(recWant){ clearTimeout(recRestartT); recRestartT=setTimeout(recKick,320) }
    else if(recActive){ stopRec(true) } };
  return r;
}
/* Браузерный движок (Chrome/Google) оставлен как резерв: он требует доступа к
   серверам Google, поэтому по умолчанию слушаем локальным Whisper. */
function sttEngine(){ return (S().voice.sttEngine==='browser'&&SR)?'browser':'whisper' }
/* Слово-будильник можно слушать и без браузерного SpeechRecognition: локальный
   Whisper/VAD (MIC) ловит «Финик» через routeSpeechText→matchWake. В Electron
   window.SpeechRecognition отсутствует, поэтому дежурство не должно зависеть от SR. */
function wakeCapable(){ return !!(SR||sttEngine()==='whisper') }
/* Держим распознаватель запущенным: Chrome останавливает поток каждую минуту */
function recKickSR(){
  if(!SR)return;
  if(!recog)recog=initRecog();
  if(!recog||recRunning)return;
  try{ recog.lang=S().voice.recLang; recog.continuous=true; recog.interimResults=true;
       recog.start(); recRunning=true }catch(e){ recRunning=false }
}
function recHaltSR(){ recWant=false; clearTimeout(recRestartT); try{ recog&&recog.abort() }catch(e){} recRunning=false }

/* Единая точка включения/выключения слуха: локальный Whisper или движок браузера */
function recKick(){ if(sttEngine()==='browser')return recKickSR(); MIC.want() }
function recHalt(){ if(sttEngine()==='browser')return recHaltSR(); MIC.stop() }

/* ============================================================
   MIC — локальный слух: микрофон → VAD → Whisper на сервере.
   Google SpeechRecognition не нужен: речь расшифровывает CPU
   через POST /api/audio/transcribe (faster-whisper, int8).
   ============================================================ */
const MIC=(function(){
  const BLK=2048;                        /* ~128 мс при 16 кГц */
  let stream=null,ctx=null,node=null,sink=null,srcNode=null;
  let running=false,busy=false;
  let preroll=[],utt=[],uttSamples=0,queue=[];
  let inSpeech=false,silMs=0,noise=0.005,lastState='';
  let onText=null,onEnd=null,onState=null,onError=null;

  function sens(){ const v=Number(S().voice.sensitivity||.6); return Math.max(.05,Math.min(1,v)) }
  function thr(){ return 0.008+(1-sens())*0.030 }        /* выше чувствительность → ниже порог */
  function endMs(){ const v=Number(S().voice.silence||.8); return Math.max(350,Math.min(1600,v*1000)) }
  function state(s){ if(s!==lastState){ lastState=s; if(onState)onState(s) } }
  function rms(b){ let s=0; for(let i=0;i<b.length;i++){ s+=b[i]*b[i] } return Math.sqrt(s/b.length) }

  function process(ch){
    if(!running)return;
    const block=new Float32Array(ch);                    /* копия: буфер переиспользуется браузером */
    const level=rms(block);
    const voicy=level>Math.max(thr(),noise*3);
    if(!voicy) noise=noise*0.98+level*0.02;              /* оценка шума — только в тишине */
    preroll.push(block); if(preroll.length>4) preroll.shift();
    if(!inSpeech){
      if(voicy){ inSpeech=true; utt=preroll.slice(); uttSamples=utt.reduce((a,b)=>a+b.length,0);
        silMs=0; state('speech') }
      return;
    }
    utt.push(block); uttSamples+=block.length;
    silMs=voicy?0:silMs+(block.length/(ctx?ctx.sampleRate:16000))*1000;
    if(silMs>=endMs()||uttSamples>(ctx?ctx.sampleRate:16000)*14) finalize();
  }

  function finalize(){
    const parts=utt; utt=[]; preroll=[]; const wasSpeech=inSpeech; inSpeech=false; silMs=0;
    state('idle');
    if(!wasSpeech)return;
    const rate=ctx?ctx.sampleRate:16000;
    const total=parts.reduce((a,b)=>a+b.length,0);
    if(total/rate<0.35)return;                           /* слишком коротко — не речь */
    if(onEnd)onEnd();                                    /* отсчёт автоотправки стартует сразу */
    queue.push({parts,total,rate});
    drain();
  }

  /* Расшифровка по одному: если человек заговорил снова, пока предыдущая фраза
     ещё обрабатывается, её аудио не теряется, а встаёт в очередь. */
  async function drain(){
    if(busy)return;
    busy=true; state('stt');
    try{
      while(queue.length){
        const job=queue.shift();
        const pcm=new Float32Array(job.total); let o=0;
        for(const p of job.parts){ pcm.set(p,o); o+=p.length }
        try{
          /* При выбранном движке идём не глубим агенту, а своему:
             локальный Whisper живёт в этом же месте. */
          const useCustom=!!voiceEngineCfg('stt');
          /* Модель шлём только если её выбрали вручную. Пустое значение — это
             «Авто»: сервер берёт модель по профилю и железу, и на CPU это base
             (замер фразы 3.4 c: base 0.7 c против small 2.2 c). */
          const wantModel=String(S().voice.sttModel||'').trim();
          const sttUrl=useCustom?('/api/voice/stt?lang='+encodeURIComponent(S().voice.recLang||'ru'))
            :('/api/audio/transcribe?lang='+encodeURIComponent(S().voice.recLang||'ru')+
             (wantModel?'&model='+encodeURIComponent(wantModel):''));
          const r=await fetch(sttUrl,{method:'POST',
            headers:Object.assign({'Content-Type':'audio/wav'},voiceEngineHeaders('stt')),
            body:encodeWav(pcm,job.rate)});
          const j=await r.json().catch(()=>({}));
          const text=String((j&&j.text)||'').trim();
          if(text&&onText)onText(text);
        }catch(e){
          console.warn('[voice] STT недоступен:',e);
          if(onError)onError('stt');
        }
      }
    }finally{ busy=false; state('idle') }
  }

  /* --- WAV: Float32 [-1..1] → 16-бит PCM --- */
  function encodeWav(pcm,rate){
    const n=pcm.length, ab=new ArrayBuffer(44+n*2), v=new DataView(ab);
    const ws=(o,s)=>{ for(let i=0;i<s.length;i++) v.setUint8(o+i,s.charCodeAt(i)) };
    ws(0,'RIFF'); v.setUint32(4,36+n*2,true); ws(8,'WAVE'); ws(12,'fmt ');
    v.setUint32(16,16,true); v.setUint16(20,1,true); v.setUint16(22,1,true);
    v.setUint32(24,rate,true); v.setUint32(28,rate*2,true);
    v.setUint16(32,2,true); v.setUint16(34,16,true);
    ws(36,'data'); v.setUint32(40,n*2,true);
    let o=44;
    for(let i=0;i<n;i++){ const s=Math.max(-1,Math.min(1,pcm[i]));
      v.setInt16(o,s<0?s*0x8000:s*0x7FFF,true); o+=2 }
    return new Blob([ab],{type:'audio/wav'});
  }

  async function start(){
    if(running)return true;
    try{
      stream=await navigator.mediaDevices.getUserMedia({audio:{
        channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
    }catch(e){
      console.warn('[voice] нет доступа к микрофону:',e);
      if(onError)onError('mic');
      return false;
    }
    try{ ctx=new (window.AudioContext||window.webkitAudioContext)({sampleRate:16000}) }
    catch(e){ ctx=new (window.AudioContext||window.webkitAudioContext)() }
    try{ await ctx.resume() }catch(e){}
    srcNode=ctx.createMediaStreamSource(stream);
    node=ctx.createScriptProcessor(BLK,1,1);
    node.onaudioprocess=e=>process(e.inputBuffer.getChannelData(0));
    sink=ctx.createGain(); sink.gain.value=0;             /* нулевой выход: без эха в колонки */
    srcNode.connect(node); node.connect(sink); sink.connect(ctx.destination);
    running=true; inSpeech=false; utt=[]; preroll=[]; silMs=0; lastState='';
    return true;
  }

  function stop(){
    if(!running)return;
    running=false; inSpeech=false; utt=[]; preroll=[]; silMs=0; lastState='';
    try{ node&&node.disconnect() }catch(e){}
    try{ srcNode&&srcNode.disconnect() }catch(e){}
    try{ sink&&sink.disconnect() }catch(e){}
    try{ stream&&stream.getTracks().forEach(t=>t.stop()) }catch(e){}
    try{ ctx&&ctx.close() }catch(e){}
    stream=ctx=node=sink=srcNode=null;
  }

  return {
    want(){ if(!running)start().catch(()=>{}) },
    stop,
    busy:()=>busy,
    running:()=>running,
    handlers(h){ onText=h.text||onText; onEnd=h.end||onEnd; onState=h.state||onState; onError=h.error||onError },
  };
})();

/* Единая маршрутизация распознанной речи: чат / главный экран (wake|dialog|stop) */
function routeSpeechText(text){
  text=String(text||'').trim(); if(!text)return;
  if(text.indexOf('[Ошибка распознавания')===0){ toast('Распознавание не удалось','err'); return }
  if(target==='chat'){                                   /* голос в поле чата */
    const i=$('#input'); if(i){ i.value=(i.value?i.value+' ':'')+text; autoGrow(i); updTok() }
    return;
  }
  if(target!=='stage'){                                  /* пассивный режим главного экрана */
    if(ui.chatOpen||ui.setOpen)return;
    if(recMode==='wake'){ const rest=matchWake(text); if(rest!==null)wakeHit(rest) }
    else if(recMode==='interrupt'){ if(matchStop(text))stopByVoice() }
    return;
  }
  /* активный диалог на главном экране */
  if(matchStop(text)&&stripStop(stripWake(text))===null){ stopByVoice(); return }
  const add=stripStop(stripWake(text))||'';
  const prev=(recBase||'').trim();
  if(add&&add!==prev&&!prev.endsWith(add)) recBase=(prev?prev+' ':'')+add;
  if(recBase) stSay(stripWake(recBase),'user');
  if(JM.state!=='listen'){ JM.setState('listen'); stage&&stage.classList.add('listening') }
  if(sttEngine()==='browser') armSilence();               /* у Whisper автоотправку армёт пауза VAD */
  touchListenIdle();
}

/* Локальный Whisper: пришёл распознанный текст (границы фразы уже определил VAD) */
function onSpeechText(text){ routeSpeechText(text) }

/* Движок браузера (Chrome/Google): interim показываем сразу, final маршрутизируем */
function onRecResult(e){
  let fin='',int='';
  for(let i=e.resultIndex;i<e.results.length;i++){ const t=e.results[i][0].transcript; e.results[i].isFinal?fin+=t:int+=t; }
  if(target==='stage'&&int){
    stSay(stripWake(((recBase||'')+' '+int).trim()),'user',true);
    if(JM.state!=='listen'){ JM.setState('listen'); stage&&stage.classList.add('listening') }
    touchListenIdle();
  }
  if(fin) routeSpeechText(fin);
  else if(target!=='stage') routeSpeechText(int);         /* wake/«стоп» ловим и по interim */
}

/* Связываем локальный слух с состоянием главного экрана */
let micWarned=false;
MIC.handlers({
  text:(t)=>onSpeechText(t),
  end:()=>armSilence(),                                   /* тишина наступила — отсчёт автоотправки */
  state:(s)=>{ if(s==='stt'&&target==='stage') stSay(stripWake(recBase||''),'user',true) },
  error:(code)=>{
    if(micWarned)return;
    if(code==='mic'){ micWarned=true; toast('Доступ к микрофону запрещён','err'); voiceSleep(true) }
    else if(code==='stt'){ micWarned=true;
      toast('Распознавание недоступно — запустите download-voice-models.bat','err') }
  },
});

function startRec(t){
  if(sttEngine()==='browser'&&!SR){ toast('Распознавание речи не поддерживается','err'); return }
  target=t; recActive=true; ui.listening=true; beep(760);
  const cm=$('#cMic'); if(cm) cm.classList.toggle('mic-on',t==='chat');
  MG.pulse(); MG.refresh();                                /* желе-волна: орбы микрофона пошли */
  if(t==='stage'){ stageListen(); return }                 /* главный экран: свой цикл состояния */
  const inp=$('#input'); if(inp)inp.dataset.base=inp.value?inp.value+' ':'';
  recMode='dialog'; recPassive=false; recWant=true; recKick();
}
function stopRec(send){
  const t0=target;
  const inp=$('#input');
  const text=t0==='chat'?(inp?inp.value.trim():''):(recBase||'').trim();
  recActive=false; ui.listening=false; clearTimeout(saySilT); clearTimeout(sayIdleT);
  if(stage) stage.classList.remove('listening');
  const cm=$('#cMic'); if(cm) cm.classList.remove('mic-on');
  MG.pulse(); MG.refresh();                                /* желе-волна: запись остановлена */
  if(inp) delete inp.dataset.base;
  beep(420,.07);
  if(t0==='chat'){                                          /* голос в поле чата — как раньше */
    recMode='off'; recPassive=false; recHalt(); setStatus('Готов к работе');
    if(send&&text){ if(inp){inp.value='';autoGrow(inp)} submitText(text) }
    else if(text&&inp){ inp.value=text; autoGrow(inp) }
    return;
  }
  recBase=text;                                             /* главный экран */
  if(send&&text){ stageSaySend(text); return }
  if(text) stSay(text,'user');
  voiceWait();
}
/* ---------- состояния главного экрана ---------- */
function stageListen(){                     /* проснулись или нажали/держим пробел — слушаем фразу */
  clearTimeout(saySilT); clearTimeout(sayIdleT);
  target='stage'; recMode='dialog'; recBase=''; recActive=true; ui.listening=true;
  recPassive=true; recWant=true; recKick();
  if(stage) stage.classList.add('listening');
  voiceStage='listen'; JM.setState('listen'); stSay('','user');
  touchListenIdle();
}
function wakeHit(rest){                     /* услышали «Финик» */
  if(rest&&matchStop(rest)&&stripStop(rest)===null){ stopByVoice(); voiceWait(); return }
  beep(880,.08); JM.pulse(); stageListen();
  if(rest){ recBase=rest; stSay(rest,'user'); armSilence() }
}
function armSilence(){                      /* автоотправка фразы после паузы */
  clearTimeout(saySilT);
  if(S().voice.autoSend===false)return;
  const sec=Number(S().voice.silenceSend||2);
  saySilT=setTimeout(()=>{
    if(target!=='stage')return;
    if(MIC.busy()){ armSilence(); return }               /* ещё расшифровываю — огрызок не отправляем */
    const t=(recBase||'').trim(); if(t)stageSaySend(t);
  },Math.max(500,sec*1000));
}
function touchListenIdle(){                 /* если молчим — возвращаемся в сон */
  clearTimeout(sayIdleT);
  const sec=Number(S().voice.wakeIdle||10);
  sayIdleT=setTimeout(()=>{ if(target==='stage'&&!ui.generating&&!(recBase||'').trim())voiceSleep() },
    Math.max(4,sec)*1000);
}
function stageSaySend(text){                /* отправляем фразу агенту, чат не открываем */
  text=String(text||'').trim(); if(!text)return;
  /* Фраза из одних стоп-слов — команда остановки, а не сообщение агенту */
  if(matchStop(text)&&stripStop(text)===null){ stopByVoice(); voiceWait(); return }
  clearTimeout(saySilT); clearTimeout(sayIdleT);
  recBase=''; target=null; stageTurn=true;
  voiceThink();
  const seq0=ui.turnSeq||0;
  dispatchUserText(text,{origin:'stage'});
  setTimeout(()=>{ if(!ui.generating&&(ui.turnSeq||0)===seq0)voiceWait() },600);
}
function voiceThink(){                      /* агент работает: принимаем только «стоп» */
  clearTimeout(saySilT); clearTimeout(sayIdleT);
  target=null; recBase=''; recActive=false; ui.listening=false;
  if(stage) stage.classList.remove('listening');
  voiceStage='think';
  if(S().voice.stopWord===false){ recMode='off'; recPassive=false; recHalt() }
  else { recMode='interrupt'; recPassive=true; recWant=true; recKick() }
  JM.setState('busy');
  setStatus('Думаю…');
}
function voiceWait(){                       /* окно продолжения разговора без слова-будильника */
  voiceStage='wait';
  if(!(wakeCapable()&&S().voice.wakeWord)){ voiceSleep(); return }
  clearTimeout(saySilT); clearTimeout(sayIdleT);
  target='stage'; recMode='dialog'; recBase='';
  if(ui.chatOpen||ui.setOpen){ recPassive=false; recWant=false }   /* оверлей открыт — микрофон молчит до закрытия */
  else { recPassive=true; recWant=true; recKick() }
  JM.setState('idle');
  touchListenIdle();
}
function voiceSleep(force){                 /* ждём «Финик» (или полностью выключаем слух) */
  clearTimeout(saySilT); clearTimeout(sayIdleT);
  target=null; recBase=''; recActive=false; ui.listening=false;
  if(stage) stage.classList.remove('listening');
  voiceStage='sleep';
  if(wakeCapable()&&S().voice.wakeWord&&!force){ recMode='wake';
    if(ui.chatOpen||ui.setOpen){ recPassive=false; recWant=false }   /* оверлей открыт — микрофон молчит до закрытия */
    else { recPassive=true; recWant=true; recKick() }
    JM.setState('sleep') }
  else { recMode='off'; recPassive=false; recHalt(); JM.setState('idle') }
  if(!ui.generating) setStatus('Готов к работе');
}
function stopByVoice(){                     /* «стоп» — прерываем агента и озвучку */
  /* Проверять только ui.generating нельзя: к моменту, когда пользователь
     говорит «стоп» или жмёт кнопку, генерация давно закончилась (ui.generating
     уже false), а голос ещё доигрывает хвост ответа. Раньше здесь стоял
     выход по этому флагу, и стоп не делал ровно ничего — озвучка продолжалась.
     curTts ловит speak(), LiveTTS.busy() — живую очередь. */
  const liveBusy=(typeof LiveTTS!=='undefined')&&LiveTTS&&LiveTTS.busy&&LiveTTS.busy();
  if(!ui.generating&&!curTts&&!liveBusy)return;
  ui.abort=true; ui.stageAborted=true; ttsStop();
  toast('Остановлено голосом','info'); beep(520,.09);
  setStatus('Остановлено');
  voiceWait();
}
function stageReply(text,err,aborted){      /* ответ агента на главном экране */
  voiceStage='reply';
  if(aborted){ setStatus('Остановлено'); voiceWait(); return }
  if(err||!text){ setStatus(err?'Ошибка':'Готово'); voiceWait(); return }
  /* Хук плагинов 'reply'. Раньше он был задокументирован и использовался
     примером, но не вызывался нигде — плагины на ответе агента не срабатывали.
     Плагин может вернуть строку, чтобы подменить ответ. */
  let out=String(text);
  try{ const r=PluginBus.emit('reply',{text:out,session:active()}); if(typeof r==='string'&&r.trim()) out=r }
  catch(e){ console.warn('Плагины (reply):',e) }
  stSay(out,'agent');
 /* Ответ уже озвучен на лету, пока текст печатался (LiveTTS.feed шёл по
    assistant.delta), поэтому повторный speak() озвучил бы его второй раз.
    В voiceWait() возвращаемся НЕ сразу, а когда очередь действительно
    доиграет: иначе микрофон включился бы посреди речи (агент услышит сам
    себя), а счётчик простоя в voiceWait() истёк бы на длинном ответе и
    увёл разговор в режим слова-будильника. */
 const liveSpoke=(typeof LiveTTS!=='undefined')&&LiveTTS.spokeThisTurn();
 if(speakEnabled()&&!liveSpoke) speak(out,()=>voiceWait());
 else if(liveSpoke){
   try{ LiveTTS.whenDone(()=>voiceWait()); }
   catch(e){ voiceWait(); }
 }
 else voiceWait();
}
/* Пока открыт чат или настройки, пассивный слушатель молчит */
function passiveHold(on){
  if(on){ if(recPassive&&!recActive&&target!=='stage'){ recWant=false; clearTimeout(recRestartT);
      if(sttEngine()==='browser'){ try{ recog&&recog.abort() }catch(e){} recRunning=false }
      else MIC.stop() } }                                  /* Whisper: гасим и захват микрофона */
  else if((voiceStage==='sleep'||voiceStage==='wait')&&S().voice.wakeWord&&!recActive
      &&!ui.chatOpen&&!ui.setOpen){ recPassive=true; recWant=true; recKick() }
}
$('#btnMic').onclick=()=>{
  if(ui.generating){ stopByVoice(); return }
  if(recActive&&target==='stage'){ stopRec(true); return }
  if(sttEngine()==='browser'&&!SR){ toast('Распознавание речи не поддерживается','err'); return }
  startRec('stage');
};
$('#cMic').onclick=()=>recActive?stopRec(true):startRec('chat');
if(sayEl) sayEl.addEventListener('click',()=>{ if(sayEl.dataset.who==='agent')openChat() });
/* Слух по слову-будильнику включаем после первого действия пользователя */
let wakeArmedOnce=false;
function armWakeOnce(){ if(wakeArmedOnce)return; wakeArmedOnce=true;
  if(S().voice.wakeWord&&!ui.chatOpen&&!ui.setOpen) voiceSleep(); }
document.addEventListener('pointerdown',armWakeOnce,{once:true});
document.addEventListener('keydown',armWakeOnce,{once:true});

/* ============================================================
   ЖЕЛЕ-МИКРОФОН — точный порт кнопки из bootom.html (Jelly Mic).
   Разметка и классы как в оригинале: .micWrap/.micGlow/.ring/.goo/
   .core/.orb-s/.sat/.face/.wave.
   Состояния приложения: idle | listen (пишем, класс .recording) |
   busy (агент думает) | sleep (дежурство по слову-будильнику).
   ============================================================ */
const JM=(function(){
  const wrap=$('#jmWrap'), mic=$('#btnMic'), goo=$('#jmGoo'), face=$('#jmFace'),
        bars=$$('#jmWave i');
  let state='idle', recording=false, t0=0, level=0, smooth=0;
  let actx=null, analyser=null, buf=null, stream=null;
  let sx=0, sv=0;                       /* желе-пружина (squash & stretch) */

  /* ===== ЗАПИСЬ + РЕАКЦИЯ НА ГОЛОС ===== */
  async function levelOn(){
    if(analyser)return;
    try{
      stream=await navigator.mediaDevices.getUserMedia({audio:true});
      actx=new (window.AudioContext||window.webkitAudioContext)();
      analyser=actx.createAnalyser();
      analyser.fftSize=512; analyser.smoothingTimeConstant=.72;
      actx.createMediaStreamSource(stream).connect(analyser);
      buf=new Uint8Array(analyser.fftSize);
    }catch(e){ analyser=null }          /* фолбэк — имитация уровня */
  }
  function levelOff(){
    try{ if(stream)stream.getTracks().forEach(t=>t.stop()); if(actx)actx.close() }catch(e){}
    stream=actx=analyser=null; buf=null; level=smooth=0;
    if(wrap){ wrap.style.setProperty('--lvl',0); wrap.style.setProperty('--amp','.7') }
    bars.forEach(b=>b.style.transform='scaleY(.12)');
  }
  function setState(s){
    state=s||'idle'; recording=(state==='listen');
    if(recording)t0=performance.now();
    if(wrap){ wrap.classList.toggle('busy',state==='busy'); wrap.classList.toggle('sleep',state==='sleep') }
    if(mic){ mic.classList.toggle('recording',recording); mic.setAttribute('aria-pressed',recording?'true':'false') }
    if(recording){ sv+=0.46; levelOn() } else levelOff();
  }
  function pulse(){ if(!wrap)return; wrap.classList.remove('pulse'); void wrap.offsetWidth; wrap.classList.add('pulse') }
  let onClick=null;
  if(mic){
    mic.addEventListener('pointerdown',()=>{ sx=-0.24; sv=0 });
    mic.addEventListener('click',()=>{ if(onClick)onClick() });
  }
  window.addEventListener('pointerup',()=>{ if(sx<-0.05)sv+=0.3 });
  /* Оригинальная кнопка — желе-шар: путь ∞, капли и «мухи» из SVG-версии
     не используются (см. bootom.html: ядро + .orb-s/.sat + gooey-фильтр). */

  /* ===== УРОВЕНЬ + ЖЕЛЕ-ПРУЖИНА КАЖДЫЙ КАДР ===== */
  function updateAudio(now){
    if(analyser){
      analyser.getByteTimeDomainData(buf);
      let sum=0;
      for(let i=0;i<buf.length;i++){ const v=(buf[i]-128)/128; sum+=v*v }
      level=Math.min(1,Math.sqrt(sum/buf.length)*3.6);
    } else if(recording){
      level=0.22+Math.abs(Math.sin(now*0.0042))*0.34+Math.random()*0.2;
    }
    smooth+=(level-smooth)*0.22;
    if(wrap){ wrap.style.setProperty('--lvl',smooth.toFixed(3));
              wrap.style.setProperty('--amp',(0.7+smooth*1.1).toFixed(3)) }
    if(recording){
      bars.forEach((b,i)=>{
        const v=0.12+smooth*(0.5+Math.abs(Math.sin(now*0.011+i*1.7))*0.9)+Math.random()*0.08;
        b.style.transform='scaleY('+Math.min(1,v).toFixed(3)+')';
      });
      if(sv<0.02&&smooth>0.55)sv+=smooth*0.06;      /* желе дрожит от голоса */
    }
  }
  function updateSpring(){
    sv+=-0.17*sx;      /* упругость */
    sv*=0.855;         /* затухание */
    sx+=sv;
    if(Math.abs(sx)<0.0004&&Math.abs(sv)<0.0004){ sx=0; sv=0 }
    if(goo)goo.style.transform='scale('+(1+sx).toFixed(4)+','+(1-sx*0.94).toFixed(4)+') rotate('+(sx*9).toFixed(3)+'deg)';
    if(face)face.style.transform='scale('+(1-sx*0.5).toFixed(4)+')';
  }

  function frame(now){
    updateAudio(now);
    updateSpring();
    requestAnimationFrame(frame);
  }
  if(mic){ setState('idle'); requestAnimationFrame(frame) }
  return { setState, pulse, levelOn, levelOff, get state(){return state},
    onClick(fn){ onClick=fn } };
})();

/* ============================================================
   ЖЕЛЕ-ОРБЫ ДЛЯ КНОПОК — порт «Orbit Send»/«Jelly» из bootom.html.
   Разметка: .sendWrap/.cmicWrap > canvas (.sendOrb) + button.
   Ядро: орбитальные частицы-сферы в canvas, желе-пружина (squash & stretch
   + морфинг скругления) и всплеск искр при отправке.
   Палитра частиц читается из акцентов темы (--accent/--accent-2/--accent-3),
   а в «горячем» состоянии (агент работает / идёт запись) — из --err/--warn,
   поэтому кнопки «поддерживают» акцентный цвет в светлой и тёмной теме.
   orbEngine(o): o.wrap/o.btn/o.canvas — селекторы; o.size — размер canvas;
   o.count, o.r/o.rd, o.ps/o.pd — число, радиус и размер частиц; o.halo —
   радиус гало; o.spark — масштаб искр; o.morph — морфинг скругления;
   o.armed()/o.hot() — состояния; o.hotSpeed — скорость орбит в работе.
   ============================================================ */
function orbEngine(o){
  const wrap=$(o.wrap), btn=$(o.btn), cvs=$(o.canvas);
  if(!wrap||!btn||!cvs)return { fire(){}, pulse(){}, refresh(){} };
  /* count:0 — движок выключен: кнопка статична, но слушатели и refresh()
     работают, чтобы состояние (класс .hot) всё ещё отражалось. */
  if(o.count===0){
    function refresh0(){ wrap.classList.toggle('hot', o.hot?!!o.hot():false) }
    refresh0();
    return { fire:refresh0, pulse:refresh0, refresh:refresh0 };
  }
  const ctx=cvs.getContext('2d');
  const SIZE=o.size??104, DPR=Math.min(window.devicePixelRatio||1,2), C=SIZE/2;
  const N=o.count??30, R0=o.r||14, RD=o.rd===undefined?7:o.rd;
  const P0=o.ps||0.6, PD=o.pd===undefined?1.2:o.pd;
  const HALO=o.halo||22, SPARK=o.spark||1, MORPH=o.morph!==false;

  /* --- hue из CSS-цвета: hex (#abc/#aabbcc) или rgb(a)/hsl --- */
  function hueOf(v){
    v=String(v||'').trim(); if(!v)return null;
    let r,g,b,m;
    if((m=v.match(/^#([0-9a-f]{3,8})$/i))){
      let h=m[1];
      if(h.length===3||h.length===4)h=h.slice(0,3).split('').map(c=>c+c).join('');
      if(h.length>=6){ r=parseInt(h.slice(0,2),16); g=parseInt(h.slice(2,4),16); b=parseInt(h.slice(4,6),16) }
    }
    if(r===undefined&&(m=v.match(/rgba?\(([^)]+)\)/i))){ const p=m[1].split(/[,\s\/]+/).map(Number); r=p[0]; g=p[1]; b=p[2] }
    if(r===undefined)return null;
    r/=255; g/=255; b/=255;
    const mx=Math.max(r,g,b), mn=Math.min(r,g,b), d=mx-mn;
    let h=0;
    if(d){ if(mx===r)h=((g-b)/d)%6; else if(mx===g)h=(b-r)/d+2; else h=(r-g)/d+4; h*=60; if(h<0)h+=360 }
    return Math.round(h);
  }

  /* --- частицы на орбитах вокруг сферы --- */
  const orbs=[];
  for(let i=0;i<N;i++)orbs.push({
    a:Math.random()*Math.PI*2, r:R0+Math.random()*RD,          /* радиус орбиты */
    sp:(0.008+Math.random()*0.024)*(Math.random()<.33?-1:1),
    tilt:0.15+Math.random()*0.8, rot:Math.random()*Math.PI,    /* «сплюснутость» и наклон */
    wob:Math.random()*6.28, wobSp:0.01+Math.random()*0.03,
    size:P0+Math.random()*PD, hue:268, ho:0 });
  const sparks=[];
  let HUES=[268,322,196,42], HOT=null;
  let hover=false, armed=false, hot=false, speed=1, speedT=1, sx=0, sv=0;
  let seen=false, tick=0, visible=true, wasOff=false, wasHidden=false;

  function readHues(){
    const cs=getComputedStyle(document.body);
    const hs=(o.colors||['--accent-2','--accent','--accent-3'])
      .map(v=>hueOf(cs.getPropertyValue(v))).filter(h=>h!==null);
    if(!hs.length)return;
    HUES=hs.concat([(hs[0]+38)%360]);
    const hb=(o.hotColors||[]).map(v=>hueOf(cs.getPropertyValue(v))).filter(h=>h!==null);
    HOT=hb.length?hb[0]:null;
    for(const p of orbs){ p.hue=HUES[(Math.random()*HUES.length)|0]; p.ho=p.hue-HUES[0] }
  }
  /* цвет частицы: в «горячем» состоянии палитра сдвигается к --err/--warn */
  function hueAt(base,off){ return (hot&&HOT!==null)?(HOT+off+720)%360:base }
  readHues();

  function burst(n){
    for(let i=0;i<n;i++){
      const a=Math.random()*6.283, v=(0.5+Math.random()*2.2)*SPARK;
      sparks.push({ x:C, y:C, vx:Math.cos(a)*v, vy:Math.sin(a)*v, life:1,
        dec:0.018+Math.random()*0.022, size:(0.5+Math.random()*1.3)*Math.max(.6,SPARK),
        hue:hueAt(HUES[(Math.random()*HUES.length)|0],(Math.random()*40-10)) });
    }
  }

  /* --- состояния: курсор / «заряжена» / «горячая» (агент работает, идёт запись) --- */
  function inp(){ return $('#input') }
  function refresh(){
    armed=o.armed?!!o.armed():!!(inp()&&inp().value&&inp().value.trim());
    hot=o.hot?!!o.hot():false;
    wrap.classList.toggle('armed',armed||hot);
    wrap.classList.toggle('hot',hot);
    speedT=hover?2.3:(hot?(o.hotSpeed||2.6):(armed?1.5:1));
  }
  wrap.addEventListener('pointerenter',()=>{ hover=true; wrap.classList.add('hover'); refresh(); sv+=0.05 });
  wrap.addEventListener('pointerleave',()=>{ hover=false; wrap.classList.remove('hover'); refresh() });
  wrap.addEventListener('pointerdown',()=>{ sx=-0.26; sv=0 });
  window.addEventListener('pointerup',()=>{ if(sx<-0.06)sv+=0.32 });
  btn.addEventListener('click',()=>{ if(ui&&ui.generating)pulse() });   /* стоп — короткая волна */

  /* --- всплеск: кольцо, желе-сквош и вылет иконки (как .send.sent в оригинале) --- */
  function sentFx(){
    btn.classList.remove('sent'); void btn.offsetWidth; btn.classList.add('sent');
    btn.addEventListener('animationend',e=>{ if(e.target===btn)btn.classList.remove('sent') },{once:true});
    setTimeout(()=>btn.classList.remove('sent'),1100);
  }
  function fire(){ burst(46); speedT=3.2; sv+=0.34; sentFx(); refresh(); setTimeout(refresh,760) }
  function pulse(){ burst(18); sentFx() }

  /* --- желе-пружина: squash & stretch + морфинг скругления --- */
  function spring(t){
    sv+=-0.17*sx; sv*=0.855; sx+=sv;
    if(Math.abs(sx)<0.0006&&Math.abs(sv)<0.0006){ sx=0; sv=0 }
    const k=Math.max(-0.34,Math.min(0.34,sx));
    btn.style.transform='scale('+(1+k).toFixed(4)+','+(1-k*0.92).toFixed(4)+') rotate('+(k*7).toFixed(3)+'deg)';
    const ic=btn.firstElementChild; if(ic)ic.style.transform='scale('+(1-k*0.5).toFixed(4)+')';
    if(!MORPH)return;
    /* желе «дышит» постоянно; когда кнопка заряжена или агент работает — сильнее */
    const m=(armed||hot)?7.5:4.2, s1=Math.sin(t*0.85), s2=Math.cos(t*1.17);
    btn.style.borderRadius=(50+s1*m).toFixed(2)+'% '+(50-s1*m).toFixed(2)+'% '+(50+s2*m*0.8).toFixed(2)+'% '+(50-s2*m*0.8).toFixed(2)+'% / '
      +(50-s1*m).toFixed(2)+'% '+(50+s1*m).toFixed(2)+'% '+(50-s2*m*0.8).toFixed(2)+'% '+(50+s2*m*0.8).toFixed(2)+'%';
  }

  /* --- отрисовка: затухающий след, гало, орбитальные сферы, искры --- */
  function draw(t){
    ctx.globalCompositeOperation='destination-out';
    ctx.fillStyle='rgba(0,0,0,.24)';
    ctx.fillRect(0,0,SIZE,SIZE);
    ctx.globalCompositeOperation='lighter';
    speed+=(speedT-speed)*0.06;
    /* гало вокруг сферы — акцентный цвет темы (в работе агента — --err) */
    const R=HALO+Math.sin(t*1.5)*1.4+(armed?1.6:0)+(hot?1.4:0);
    const HA=hueAt(HUES[0],0), HB=hueAt(HUES[1%HUES.length],HUES[1%HUES.length]-HUES[0]);
    const g=ctx.createRadialGradient(C,C,0,C,C,R);
    g.addColorStop(0,'hsla('+HA+',96%,64%,'+(hot?.36:(armed?.30:.16))+')');
    g.addColorStop(.55,'hsla('+HB+',96%,60%,.10)');
    g.addColorStop(1,'hsla('+HA+',96%,60%,0)');
    ctx.fillStyle=g; ctx.beginPath(); ctx.arc(C,C,R,0,6.283); ctx.fill();
    for(const p of orbs){
      p.a+=p.sp*speed; p.wob+=p.wobSp;
      const rr=p.r+Math.sin(p.wob)*1.8+(armed?1.4:0);
      const ca=Math.cos(p.a), sa=Math.sin(p.a);
      const x0=ca*rr, y0=sa*rr*p.tilt;
      const cr=Math.cos(p.rot), sr=Math.sin(p.rot);
      const X=C+x0*cr-y0*sr, Y=C+x0*sr+y0*cr;
      const z=(sa+1)/2;                                   /* псевдо-глубина */
      const s=p.size*(0.6+z*0.9)*(armed?1.2:1);
      const al=Math.min(1,(0.22+z*0.72)*(0.6+speed*0.3));
      const H=hueAt(p.hue,p.ho);
      ctx.shadowBlur=7+z*10;
      ctx.shadowColor='hsla('+H+',100%,65%,.95)';
      ctx.fillStyle='hsla('+H+',100%,'+(60+z*16)+'%,'+al+')';
      ctx.beginPath(); ctx.arc(X,Y,s,0,6.283); ctx.fill();
    }
    for(let i=sparks.length-1;i>=0;i--){
      const s=sparks[i];
      s.x+=s.vx; s.y+=s.vy; s.vx*=0.93; s.vy*=0.93; s.life-=s.dec;
      if(s.life<=0){ sparks.splice(i,1); continue }
      ctx.shadowBlur=11; ctx.shadowColor='hsla('+s.hue+',100%,65%,.95)';
      ctx.fillStyle='hsla('+s.hue+',100%,72%,'+s.life+')';
      ctx.beginPath(); ctx.arc(s.x,s.y,s.size*s.life+.3,0,6.283); ctx.fill();
    }
    ctx.shadowBlur=0;
  }

  /* режим «без анимаций» — кнопка статичная */
  function reset(){
    ctx.globalCompositeOperation='source-over';
    ctx.clearRect(0,0,SIZE,SIZE);
    sparks.length=0;
    btn.style.transform=''; btn.style.borderRadius='';
    const ic=btn.firstElementChild; if(ic)ic.style.transform='';
  }

  function frame(now){
    if(!seen){ seen=true;
      const el=inp();
      if(el){ el.addEventListener('input',refresh); el.addEventListener('change',refresh) }
      refresh();
    }
    tick++;
    if(tick%15===0)refresh();          /* страховка: ловим ui.generating и запись */
    if(tick===1||tick%25===0)visible=!!(wrap.offsetParent||wrap.getClientRects().length);
    if(!visible){ if(!wasHidden){ wasHidden=true; ctx.clearRect(0,0,SIZE,SIZE) } }
    else wasHidden=false;
    if(document.body.classList.contains('no-anim')){
      if(!wasOff){ wasOff=true; reset() }
      requestAnimationFrame(frame); return;
    }
    if(wasOff){ wasOff=false; refresh() }
    if(!document.hidden&&visible){ const t=now/1000; spring(t); draw(t) }
    requestAnimationFrame(frame);
  }

  cvs.width=SIZE*DPR; cvs.height=SIZE*DPR;
  cvs.style.width=cvs.style.height=SIZE+'px';
  ctx.scale(DPR,DPR);
  readHues();
  /* смена темы (light/dark) или акцентов → пересчёт палитры частиц */
  new MutationObserver(readHues).observe(document.body,{attributes:true,attributeFilter:['data-theme','style']});
  new MutationObserver(readHues).observe(document.documentElement,{attributes:true,attributeFilter:['style','data-theme']});
  requestAnimationFrame(frame);
  return { fire, pulse, refresh };
}

/* --- кнопка «Отправить» в чате: анимация включается только когда агент
       работает (ui.generating → класс .stop). В простое — обычная кнопка.
       armed всегда false, чтобы наличие текста в поле не запускало орбиты. --- */
const SG=orbEngine({
  wrap:'#sendWrap', btn:'#cSend', canvas:'#cOrb',
  size:104, count:30, r:14, rd:7, ps:0.6, pd:1.2, halo:22, spark:1, morph:true, hotSpeed:2.6,
  colors:['--accent-2','--accent','--accent-3'], hotColors:['--err','--warn'],
  armed:()=>false,
  hot:()=>!!(ui&&ui.generating)
});

/* --- кнопка микрофона в композере: те же орбы + желе; «горячая» во время записи --- */
const MG=orbEngine({
  wrap:'#cmWrap', btn:'#cMic', canvas:'#cOrbMic',
  size:0, count:0, r:0, rd:0, ps:0, pd:0, halo:0, spark:0, morph:false,
  colors:['--accent','--accent-3'], hotColors:['--err','--warn'],
  armed:()=>false,
  hot:()=>recActive||!!($('#cMic')&&$('#cMic').classList.contains('mic-on'))
});

function voices(){ return speechSynthesis.getVoices()||[] }

let sileroOk=null; /* null=не проверено, true/false — доступность серверного TTS */

/* ============ ПОЛЬЗОВАТЕЛЬСКИЕ ДВИЖКИ STT/TTS ============
   Конфигурация лежит только в браузере (S().voice.sttEngine / ttsEngine) и
   уходит на сервер заголовком вместе с запросом. Ключ API на диск не
   попадает, а после перезагрузки страницы движок возвращается к локальному.
   Выбран «пустой» движок или поля не заполнены — сервер берёт локальный. */
function voiceEngineCfg(kind){
  /* Список и выбор движков живут в 12-voice-engines.js. Здесь только достаём
     конфигурацию активного; не выбран он или локальный — null, и сервер
     берёт локальные Whisper/Silero. */
  return (typeof engActiveCfg==='function')?engActiveCfg(kind):null;
}
/* Заголовок с конфигурацией. Без него сервер не станет трогать интернет. */
function voiceEngineHeaders(kind){
  const cfg=voiceEngineCfg(kind);
  return cfg?{['X-Voice-Engine-'+kind.toUpperCase()]:JSON.stringify(cfg)}:{};
}
/* Кнопка «Проверить» в настройках: спрашивает сервер, доходит ли движок. */
async function voiceEngineCheck(kind,btn){
  const cfg=voiceEngineCfg(kind);
  if(!cfg){ toast('Сначала укажите адрес и сохраните', 'info'); return }
  if(btn)btn.disabled=true;
  try{
    const r=await fetch('/api/voice/check',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({kind:kind,cfg:cfg})});
    const j=await r.json().catch(()=>null);
    if(j&&j.ok)toast('Движок отвечает: '+(j.detail||'ок'),'ok');
    else toast('Движок недоступен: '+((j&&j.error)||'нет ответа'),'err');
  }catch(e){ toast('Проверка не удалась: '+e.message,'err') }
  finally{ if(btn)btn.disabled=false }
}
async function speakSilero(text,onEnd){
  try{
    const r=await fetch(API.baseURL+'/api/voice/tts',{method:'POST',
      headers:Object.assign({'Content-Type':'application/json'},voiceEngineHeaders('tts')),
      body:JSON.stringify({text:plain(text).slice(0,1200)})});
    if(!r.ok)throw 0;
    const b=await r.blob();
    if(!b.size||b.type && !b.type.startsWith('audio'))throw 0;
    const a=new Audio(URL.createObjectURL(b)); a.volume=Math.min(1,S().voice.volume||1);
    a.playbackRate=S().voice.rate||1;
    if(onEnd){ a.onended=()=>{ if(curTts&&curTts.audio===a)curTts=null; onEnd() };
               a.onerror=()=>{ if(curTts&&curTts.audio===a)curTts=null; onEnd() } }
    curTts={audio:a};
    sileroOk=true; await a.play(); return true;
  }catch(e){ sileroOk=false; curTts=null; return false }
}
/* Озвучка включена, если не выключены оба переключателя: «Голосовой вывод» и «Озвучивать ответы» */
function speakEnabled(){ return S().voice.outputEnabled!==false&&S().voice.autoSpeak!==false }

async function speak(text,onEnd,force){
  if(!(force||speakEnabled())||!text){ if(onEnd)onEnd(); return }
  speechSynthesis.cancel(); curTts=null;
  /* Микрофон молчит, пока звучит озвучка — иначе агент услышит сам себя */
  passiveHold(true);
  const done=()=>{ passiveHold(false); if(onEnd)onEnd() };
  /* Серверный Silero 5.5 (голос xenia) — приоритет; при недоступности браузерный TTS */
  if(sileroOk!==false && !IS_FILE_PROTOCOL){ if(await speakSilero(text,done))return; }
  const u=new SpeechSynthesisUtterance(plain(text).slice(0,1200));
  const v=voices().find(x=>x.voiceURI===S().voice.voiceURI); if(v)u.voice=v;
  u.lang=v?v.lang:'ru-RU';
  u.rate=S().voice.rate; u.pitch=S().voice.pitch; u.volume=S().voice.volume;
  u.onend=()=>{ if(curTts&&curTts.utt===u)curTts=null; done() };
  u.onerror=()=>{ if(curTts&&curTts.utt===u)curTts=null; done() };
  curTts={utt:u};
  speechSynthesis.speak(u);
}
/* Мгновенная остановка озвучки — по голосовому «стоп» или отмене */
function ttsStop(){
  try{ if(curTts&&curTts.audio){curTts.audio.onended=null;curTts.audio.onerror=null;curTts.audio.pause()} }catch(e){}
  try{ speechSynthesis.cancel() }catch(e){}
  curTts=null; passiveHold(false);
  /* Живая озвучка держит свою очередь — без этого вызов она продолжала бы
     говорить после «стоп». Её же зовут Esc (08-gateway.js) и голосовое «стоп»
     (stopByVoice), поэтому один этот вызов закрывает оба сценария. */
  try{ if(typeof LiveTTS!=='undefined'&&LiveTTS)LiveTTS.stop() }catch(e){}
}
function fillVoices(){ const sel=$('#voiceSelect'); if(!sel)return; const cur=S().voice.voiceURI;
  sel.innerHTML='<option value="">По умолчанию</option>'+voices().map(v=>'<option value="'+v.voiceURI+'">'+esc(v.name+' · '+v.lang)+'</option>').join('');
  sel.value=cur; }
if('speechSynthesis' in window){ speechSynthesis.onvoiceschanged=fillVoices; }
$('#testVoice').onclick=()=>{ fillVoices(); speak('Голосовой вывод настроен.',null,true) };

async function loadDevices(){
  const sel=$('#devSelect'); if(!sel)return;
  try{ const ds=await navigator.mediaDevices.enumerateDevices();
    const auds=ds.filter(d=>d.kind==='audioinput');
    sel.innerHTML='<option value="default">По умолчанию</option>'+auds.map((d,i)=>'<option value="'+d.deviceId+'">'+esc(d.label||('Микрофон '+(i+1)))+'</option>').join('');
    sel.value=S().voice.inputDevice;
    const mh=$('#micHint'); if(mh) mh.textContent=auds.some(a=>a.label)?'Найдено: '+auds.length:'Требуется разрешение';
  }catch(e){}
}
$('#reqMic').onclick=async()=>{
  if(IS_FILE_PROTOCOL){ toast('Микрофон не работает через file://','err'); return }
  try{ const s=await navigator.mediaDevices.getUserMedia({audio:true}); s.getTracks().forEach(t=>t.stop());
    await loadDevices(); toast('Доступ получен','ok'); }catch(e){ toast('Не удалось','err') }
};

/* Проверка локального Whisper: какая модель выбрана, какая загружена и какие
   веса реально найдены в models/ (включая папки base/ и turbo/, добавленные вручную). */
$('#sttCheck').onclick=async()=>{
  const want=S().voice.sttModel||'small';
  try{
    const r=await fetch('/api/voice/stt/status');
    const j=await r.json();
    const local=(j&&j.local_models)||[], paths=(j&&j.local_paths)||{}, engine=j&&j.engine;
    const has=local.indexOf(want)>=0;
    toast('Whisper: '+want+(has?' — веса найдены локально':' — локальных весов нет'),has?'ok':'info');
    console.log('[stt]',j);
    const st=active();
    if(st){
      msg(st,{role:'system',kind:'system',content:'🎙 Whisper — движок: '+(engine||'ещё не загружен')+
        '; активная модель: '+((j&&j.model)||want)+'; в настройках: '+want+(has?' ✓':' (весов нет)')+
        '. Локально найдены: '+(local.length?local.map(n=>n+' → '+paths[n]).join('; '):'нет')});
      save(); renderAll();
    }
  }catch(e){ toast('Не удалось проверить: '+((e&&e.message)||e),'err') }
};
