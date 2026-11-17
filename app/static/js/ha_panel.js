'use strict';
/* ═══════════════════════ БАЗОВЫЙ ПУТЬ ПАНЕЛИ ═══════════════════════════
   Панель живёт под /ha, поэтому все её запросы (REST, WS, прокси к HA)
   собираются от этого префикса. Значение приходит из <body data-ha-base>. */
const HA_BASE=(function(){try{
 var v=(document.body&&document.body.dataset.haBase)||'';
 if(!v||!/^[A-Za-z0-9_\-./]*$/.test(v))return '/ha';
 return v.replace(/\/+$/,'');}catch(_){return '/ha';}})();
const HA_API=HA_BASE+'/api';
/* ═══════════════════════ ГЛОБАЛЬНОЕ СОСТОЯНИЕ ═══════════════════════ */
let WS=null,WSQ=[],PID=0,PROM={},STATES={},SERVICES=null,SYSCFG=null;
let REG={entities:[],devices:[],areas:[],map:{}};
let ROUTE='dashboard',CLEAN=[],WSOK=false,DASH_DOMAIN='',MAP_EXTRA=new Set(LSget('hap_map_extra')||[]);

/* ═══════════════════════ УТИЛИТЫ ═══════════════════════ */
const $=s=>document.querySelector(s);
const $$=s=>Array.from(document.querySelectorAll(s));
const dom=e=>String(e).split('.')[0];
const S=e=>STATES[e];
const A=(e,k,d)=>{const s=STATES[e];return s&&s.attributes&&s.attributes[k]!==undefined?s.attributes[k]:d;};
const FN=e=>A(e,'friendly_name',e);
function LSget(k){try{return JSON.parse(localStorage.getItem(k))}catch(_){return null}}
function LSset(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(_){}return v}
const LS=(k,v)=>v===undefined?LSget(k):LSset(k,v);
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function sleep(ms){return new Promise(r=>setTimeout(r,ms));}
function debounce(fn,ms){let t;return function(...a){clearTimeout(t);t=setTimeout(()=>fn.apply(this,a),ms);};}
function ftime(iso){if(!iso)return'';try{return new Date(iso).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});}catch(_){return iso;}}
function fdate(iso){try{return new Date(iso).toLocaleDateString('ru-RU',{day:'numeric',month:'short'});}catch(_){return iso;}}
function fmtNum(v){if(v==null||!isFinite(v))return'—';const a=Math.abs(v);
 if(a>=1e6)return (v/1e6).toFixed(1)+'M';if(a>=1e4)return (v/1e3).toFixed(1)+'k';
 if(a>=100)return v.toFixed(0);if(a>=1)return v.toFixed(a>=10?1:2).replace(/\.?0+$/,'');
 return v.toFixed(3).replace(/\.?0+$/,'');}
function fmtAttr(v){if(v==null)return'—';if(typeof v==='boolean')return v?'да':'нет';
 if(Array.isArray(v))return v.map(x=>typeof x==='object'?JSON.stringify(x):x).join(', ');
 if(typeof v==='object')return JSON.stringify(v);return String(v);}
function slug(s){return String(s).toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'').slice(0,60)||'new';}
function isoAgo(h){return new Date(Date.now()-h*3600*1000).toISOString();}
function toast(msg,kind,ms){const w=$('#toasts');const t=document.createElement('div');
 t.className='toast '+(kind||'');t.innerHTML=msg;t.onclick=()=>t.remove();w.appendChild(t);
 setTimeout(()=>{t.classList.add('out');setTimeout(()=>t.remove(),400);},ms||3600);}
function confirmDo(text){return window.confirm(text);}

/* ── диалоги ── */
function openDialog(html,wide){const wrap=$('#dialogWrap'),box=$('#dialog');
 box.className=wide?'wide':'';box.innerHTML=html;wrap.classList.remove('hidden');
 document.body.classList.add('no-scroll');}
function closeDialog(){$('#dialogWrap').classList.add('hidden');$('#dialog').innerHTML='';
 document.body.classList.remove('no-scroll');}

/* ── REST API (через прокси сервера) ── */
async function api(path,opts){opts=opts||{};
 const o={method:opts.method||'GET'};
 if(opts.body!==undefined){o.headers={'Content-Type':'application/json'};
  o.body=typeof opts.body==='string'?opts.body:JSON.stringify(opts.body);}
 let r;try{r=await fetch(path,o);}catch(e){toast('Сервер панели недоступен','err');throw e;}
 if(r.status===401){location.href=HA_BASE+'/login';throw new Error('unauthorized');}
 const ct=r.headers.get('content-type')||'';
 let data=ct.includes('json')?await r.json().catch(()=>null):await r.text();
 if(!r.ok){const m=(data&&(data.message||data.error))||(typeof data==='string'&&data?data.slice(0,300):'')||('HTTP '+r.status);
  const e=new Error(m);e.data=data;throw e;}
 return data;}
async function apiSafe(p,o){try{return await api(p,o);}catch(_){return null;}}

/* ═══════════════════════ WEBSOCKET ═══════════════════════ */
function wsConnect(){
 const proto=location.protocol==='https:'?'wss':'ws';
 try{WS=new WebSocket(proto+'://'+location.host+HA_BASE+'/ws');}catch(_){setTimeout(wsConnect,3000);return;}
 WS.onopen=()=>{const q=WSQ;WSQ=[];q.forEach(m=>{try{WS.send(m)}catch(_){}});};
 WS.onmessage=e=>{let d;try{d=JSON.parse(e.data);}catch(_){return;}handleWS(d);};
 WS.onclose=()=>{setChip(false);WS=null;setTimeout(wsConnect,3000);};
 WS.onerror=()=>{try{WS.close()}catch(_){}};
}
function wsSend(obj){const s=JSON.stringify(obj);
 if(WS&&WS.readyState===1)WS.send(s);else if(WSQ.length<200)WSQ.push(s);}
function setChip(ok){WSOK=!!ok;const c=$('#wsChip');if(!c)return;
 c.classList.toggle('on',ok);c.classList.toggle('off',!ok);
 $('#wsTxt').textContent=ok?'HA · онлайн':'HA · переподключение';
 const f=$('#footStatus');if(f)f.textContent=ok?'связь с HA: WebSocket':'связь с HA: опрос REST';}
function handleWS(d){
 if(d.type==='hello'){
  setChip(d.ws);
  if(d.states&&Object.keys(d.states).length){STATES=d.states;}
  if(WSOK&&(!REG.entities.length))loadRegistries();
  refreshRoute();
 }
 else if(d.type==='status'){setChip(d.ws);if(d.ws&&!REG.entities.length)loadRegistries();}
 else if(d.type==='state'){
  if(d.state)STATES[d.entity_id]=d.state;else delete STATES[d.entity_id];
  onStateChange(d.entity_id);
 }
 else if(d.type==='proxy_result'){
  const p=PROM[d.pid];if(!p)return;const pl=d.payload||{};
  if(pl.type==='result'){
   if(pl.success){p.done=true;try{p.res(pl.result);}catch(_){} if(!p.sub)delete PROM[d.pid];}
   else if(!(p.sub&&p.gotEvent)){delete PROM[d.pid];
    try{p.rej(new Error((pl.error&&pl.error.message)||'Ошибка запроса к HA'));}catch(_){}}
  }else if(pl.type==='event'){p.gotEvent=true;if(p.onEvent)try{p.onEvent(pl.event,pl);}catch(_){}}
 }
}
/* одноразовая команда к HA WS API (реестры, call_service с ответом, statistics…) */
function haproxy(payload){
 return new Promise((res,rej)=>{
  const pid=++PID;const t=String(payload.type||'');
  const p={res,rej,sub:t.startsWith('subscribe_')||t==='render_template',done:false};
  PROM[pid]=p;wsSend({type:'proxy',pid,payload});
  setTimeout(()=>{if(PROM[pid]&&!PROM[pid].done){delete PROM[pid];rej(new Error('Таймаут ответа Home Assistant'));}},20000);
 });}
/* подписка (события / живые шаблоны) — возвращает pid для отписки */
function haSub(payload,onEvent){
 const pid=++PID;PROM[pid]={res:()=>{},rej:()=>{},onEvent,sub:true,done:false};
 wsSend({type:'proxy',pid,payload});return pid;}
function unproxy(pid){wsSend({type:'unproxy',pid});delete PROM[pid];}

/* ═══════════════════════ ВЫЗОВ СЛУЖБ HA ═══════════════════════ */
async function callSvc(domain,service,data,silent){
 try{const r=await api(HA_API+'/ha/services/'+domain+'/'+service,{method:'POST',body:data||{}});
  if(!silent)toast('✅ '+domain+'.'+service,'ok',1800);return r;}
 catch(e){toast('❌ '+domain+'.'+service+': '+esc(e.message),'err',5000);throw e;}}
/* вызов через WS — умеет возвращать response службы */
async function callSvcWS(domain,service,data,target){
 return await haproxy({type:'call_service',domain,service,service_data:data||{},
  target:target||{},return_response:true});}

/* ═══════════════════════ ТЕКСТЫ СОСТОЯНИЙ / ИКОНКИ ═══════════════════════ */
const RU_STATE={on:'Вкл',off:'Выкл',unavailable:'Недоступно',unknown:'Неизвестно',idle:'Ожидание',
 playing:'Воспроизведение',paused:'Пауза',standby:'Режим ожидания',home:'Дома',away:'Не дома',
 not_home:'Не дома',open:'Открыто',closed:'Закрыто',opening:'Открывается',closing:'Закрывается',
 locked:'Заперто',unlocked:'Отперто',jammed:'Заклинило',locking:'Закрывается…',unlocking:'Открывается…',
 cleaning:'Уборка',docked:'На базе',returning:'Возвращается',error:'Ошибка',
 heat:'Нагрев',cool:'Охлаждение',heat_cool:'Авто климат',auto:'Авто',dry:'Осушение',fan_only:'Вентиляция',off_:'Выкл',
 armed_home:'Охрана · дома',armed_away:'Охрана · вне дома',armed_night:'Охрана · ночь',
 armed_custom_bypass:'Охрана · спец',armed_vacation:'Охрана · отпуск',disarmed:'Снято с охраны',
 triggered:'🚨 ТРЕВОГА',pending:'Ожидание',arming:'Постановка…',
 recording:'Запись',streaming:'Поток',sunny:'Ясно','clear-night':'Ясная ночь',cloudy:'Облачно',
 partlycloudy:'Переменная облачность',rainy:'Дождь',pouring:'Ливень',snowy:'Снег','snowy-rainy':'Мокрый снег',
 fog:'Туман',haze:'Дымка',windy:'Ветрено',hail:'Град',lightning:'Гроза','lightning-rainy':'Гроза с дождём',
 exceptional:'Опасные явления',buffering:'Буферизация',active:'Активен',paused_:'Пауза',
 unavailable_:'Недоступно',finished:'Завершён',running:'Выполняется',queued:'В очереди',
 updating:'Обновляется…',installing:'Установка…'};
function stateText(st){if(!st)return'—';const s=st.state,d=dom(st.entity_id),a=st.attributes||{};
 if(d==='light'&&s==='on'){return a.brightness?'Вкл · '+Math.round(a.brightness/2.55)+'%':'Вкл';}
 if(d==='cover'&&a.current_position!=null)return 'Позиция '+a.current_position+'%';
 if(d==='climate'&&a.current_temperature!=null)return (RU_STATE[s]||s)+' · '+a.current_temperature+'°';
 if(d==='media_player'&&a.media_title)return a.media_title;
 if(d==='fan'&&s==='on'&&a.percentage!=null)return 'Вкл · '+a.percentage+'%';
 if(d==='sensor'||d==='number'||d==='input_number'){return s+(a.unit_of_measurement?' '+a.unit_of_measurement:'');}
 if((d==='person'||d==='device_tracker')&&s!=='home'&&s!=='not_home'&&s!=='unavailable')return s;
 return RU_STATE[s]||s;}
const ICON={light:'💡',switch:'🔌',sensor:'📟',binary_sensor:'🔔',climate:'🌡️',weather:'🌤️',
 cover:'🪟',media_player:'🎵',lock:'🔒',camera:'📷',vacuum:'🧹',fan:'🌀',humidifier:'💧',
 water_heater:'🔥',person:'👤',device_tracker:'📍',zone:'📌',automation:'🤖',scene:'🎬',
 script:'📜',input_boolean:'🎚️',input_number:'🔢',input_select:'📋',input_text:'✏️',
 input_datetime:'🕑',input_button:'🔘',timer:'⏲️',select:'📋',number:'🔢',button:'🔘',
 siren:'🚨',alarm_control_panel:'🛡️',sun:'☀️',update:'⬆️',notify:'📣',remote:'📡',
 lawn_mower:'🚜',valve:'🚰',event:'📅',image:'🖼️',stt:'🎙️',tts:'🗣️',conversation:'💬'};
const BS_ICON={motion:'🏃',occupancy:'👁️',presence:'👁️',door:'🚪',window:'🪟',garage_door:'🚪',
 moisture:'💧',leak:'💧',smoke:'🌫️',gas:'☣️',co:'☣️',heat:'🔥',cold:'❄️',battery:'🔋',
 plug:'🔌',connectivity:'📶',opening:'🚪',lock:'🔒',safety:'⚠️',sound:'🔊',vibration:'📳',
 problem:'⚠️',running:'🏃',tamper:'⚠️',power:'⚡',light:'💡'};
const SENSOR_ICON={temperature:'🌡️',humidity:'💧',battery:'🔋',power:'⚡',energy:'⚡',
 illuminance:'☀️',pressure:'🧭',pm25:'🌫️',pm10:'🌫️',carbon_dioxide:'☣️',co2:'☣️',
 gas:'☣️',monetary:'💰',water:'💧',gas_:'🔥',signal_strength:'📶',timestamp:'🕑',
 apparent_power:'⚡',current:'⚡',voltage:'⚡',frequency:'〰️',duration:'⏱️',distance:'📏',
 weight:'⚖️',volume:'🧊',atmospheric_pressure:'🧭',wind_speed:'🌬️'};
function iconOf(st){const eid=st.entity_id||st,d=dom(eid),dc=A(eid,'device_class'),s=st.state;
 if(d==='binary_sensor'&&BS_ICON[dc])return BS_ICON[dc];
 if(d==='sensor'&&dc){if(SENSOR_ICON[dc])return SENSOR_ICON[dc];if(dc==='battery'){const v=parseFloat(s);
   if(isFinite(v))return v<=15?'🪫':v<=40?'🔋':'🔋';}}
 if(d==='weather'){if(s==='sunny'||s==='clear-night')return'☀️';
  if(String(s).includes('rain')||s==='pouring')return'🌧️';if(String(s).includes('snow'))return'🌨️';
  if(s==='lightning'||s==='lightning-rainy')return'⛈️';if(s==='fog'||s==='haze')return'🌫️';
  if(s==='cloudy'||s==='partlycloudy')return'⛅';if(s==='windy')return'🌬️';}
 if(d==='cover'&&['garage','garage_door'].includes(dc))return'🚪';
 if(d==='media_player')return dc==='tv'?'📺':dc==='speaker'?'🔊':dc==='receiver'?'📻':'🎵';
 if(d==='switch'&&dc==='outlet')return'🔌';
 if(d==='update')return s==='on'?'⬆️':'✅';
 return ICON[d]||'⚙️';}
function iconOfDomain(d){return ICON[d]||'⚙️';}
const TOGGLEABLE=new Set(['light','switch','input_boolean','fan','automation','scene','script',
 'humidifier','siren','cover','lock','vacuum','media_player','button','input_button','remote','valve','lawn_mower']);
function isOn(st){const s=st.state,d=dom(st.entity_id);
 if(d==='cover')return['open','opening'].includes(s);
 if(d==='lock')return s==='locked';
 if(d==='vacuum')return s==='cleaning';
 if(d==='media_player')return['playing','paused','on'].includes(s);
 if(d==='binary_sensor')return s==='on';
 if(d==='person'||d==='device_tracker')return s==='home';
 if(d==='update')return s==='on';
 if(d==='timer')return s==='active';
 if(d==='alarm_control_panel')return String(s).startsWith('armed')||s==='triggered';
 if(d==='valve')return s==='open';
 if(d==='lawn_mower')return s==='mowing';
 return s==='on';}
async function toggleEntity(eid){
 const d=dom(eid),st=S(eid);if(!st)return;const s=st.state;
 try{
  if(d==='script')return await callSvc('script','turn_on',{entity_id:eid});
  if(d==='scene')return await callSvc('scene','turn_on',{entity_id:eid});
  if(d==='lock')return await callSvc('lock',s==='locked'?'unlock':'lock',{entity_id:eid});
  if(d==='button'||d==='input_button')return await callSvc(d,'press',{entity_id:eid});
  if(d==='vacuum')return await callSvc('vacuum',s==='cleaning'?'pause':'start',{entity_id:eid});
  if(d==='valve')return await callSvc('valve',s==='open'?'close':'open',{entity_id:eid});
  if(d==='lawn_mower')return await callSvc('lawn_mower',s==='mowing'?'pause':'start_mowing',{entity_id:eid});
  if(['light','switch','input_boolean','fan','automation','humidifier','siren','media_player','cover','remote'].includes(d))
   return await callSvc(d,'toggle',{entity_id:eid});
  moreInfo(eid);
 }catch(_){}
}

/* ═══════════════════════ ГРАФИКИ (canvas, без библиотек) ═══════════════════════ */
const PALETTE=['#5eb1ff','#3ddc84','#ffb454','#ff6b6b','#c792ea','#4dd0e1','#f78fb3','#a3e635','#fbbf24','#60a5fa'];
function drawChart(cv,series,opts){
 opts=opts||{};if(!cv)return;
 const dpr=window.devicePixelRatio||1;
 const W=cv.clientWidth||600,H=cv.clientHeight||220;
 cv.width=W*dpr;cv.height=H*dpr;
 const g=cv.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);
 const cs=getComputedStyle(document.body);
 const txtC=cs.getPropertyValue('--mut').trim()||'#8b96a8';
 const lineC=cs.getPropertyValue('--line').trim()||'#2a3342';
 const pad={l:52,r:14,t:14,b:26};
 const all=[];series.forEach(s=>s.pts.forEach(p=>{if(p.v!=null&&isFinite(p.v))all.push(p.v);}));
 g.clearRect(0,0,W,H);
 if(!all.length){g.fillStyle=txtC;g.font='13px sans-serif';g.fillText('Нет числовых данных',pad.l,H/2);cv.onmousemove=null;return;}
 let min=Math.min(...all),max=Math.max(...all);
 if(opts.min!=null)min=Math.min(min,opts.min);
 if(opts.max!=null)max=Math.max(max,opts.max);
 if(max===min){max+=1;min-=1;}
 const py=(max-min)*0.12;min-=py;max+=py;
 let tmin=Infinity,tmax=-Infinity;
 series.forEach(s=>s.pts.forEach(p=>{if(p.t<tmin)tmin=p.t;if(p.t>tmax)tmax=p.t;}));
 if(!isFinite(tmin)){tmin=Date.now()-3600e3;tmax=Date.now();}
 if(tmax<=tmin)tmax=tmin+1000;
 const X=t=>pad.l+(t-tmin)/(tmax-tmin)*(W-pad.l-pad.r);
 const Y=v=>H-pad.b-(v-min)/(max-min)*(H-pad.t-pad.b);
 g.strokeStyle=lineC;g.fillStyle=txtC;g.font='10px sans-serif';g.lineWidth=1;
 for(let i=0;i<=4;i++){const v=min+(max-min)*i/4,y=Y(v);
  g.globalAlpha=.45;g.beginPath();g.moveTo(pad.l,y);g.lineTo(W-pad.r,y);g.stroke();g.globalAlpha=1;
  g.fillText(fmtNum(v),6,y+3);}
 for(let i=0;i<=4;i++){const t=tmin+(tmax-tmin)*i/4,x=X(t);
  g.fillText(new Date(t).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}),Math.min(Math.max(x-16,pad.l),W-50),H-8);}
 series.forEach((s,si)=>{
  const col=s.color||PALETTE[si%PALETTE.length];s._col=col;
  g.strokeStyle=col;g.lineWidth=1.8;g.beginPath();let started=false,last=null;
  s.pts.forEach(p=>{if(p.v==null||!isFinite(p.v)){started=false;return;}
   const x=X(p.t),y=Y(p.v);if(!started){g.moveTo(x,y);started=true;}else g.lineTo(x,y);last={x,y};});
  g.stroke();
  if(opts.fill!==false&&started&&last){g.globalAlpha=.09;g.lineTo(last.x,H-pad.b);
   let firstX=null;s.pts.forEach(p=>{if(firstX==null&&p.v!=null&&isFinite(p.v))firstX=X(p.t);});
   g.lineTo(firstX!=null?firstX:pad.l,H-pad.b);g.closePath();g.fillStyle=col;g.fill();g.globalAlpha=1;}});
 /* легенда */
 let lg=cv.parentElement&&cv.parentElement.querySelector('.legend');
 if(series.length>1){
  if(!lg&&cv.parentElement){lg=document.createElement('div');lg.className='legend';cv.parentElement.appendChild(lg);}
  if(lg)lg.innerHTML=series.map((s,i)=>`<span><i style="background:${s.color||PALETTE[i%PALETTE.length]}"></i>${esc(s.name)}</span>`).join('');
 }else if(lg)lg.remove();
 /* курсор+подсказка */
 const tip=$('#chartTip');
 cv.onmousemove=e=>{
  const r=cv.getBoundingClientRect();const mx=e.clientX-r.left;
  if(mx<pad.l-10||mx>W-pad.r+10){tip.classList.add('hidden');return;}
  const t=tmin+(mx-pad.l)/(W-pad.l-pad.r)*(tmax-tmin);
  let html='<b>'+new Date(t).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+'</b>';
  series.forEach(s=>{let best=null,bd=Infinity;
   s.pts.forEach(p=>{const dd=Math.abs(p.t-t);if(dd<bd){bd=dd;best=p;}});
   if(best&&best.v!=null)html+=`<div><span style="color:${s._col||''}">●</span> ${esc(s.name)}: <b>${fmtNum(best.v)}</b></div>`;});
  tip.innerHTML=html;tip.classList.remove('hidden');
  tip.style.left=Math.min(e.clientX+14,window.innerWidth-250)+'px';
  tip.style.top=(e.clientY-10)+'px';
 };
 cv.onmouseleave=()=>tip.classList.add('hidden');
}
function toSegments(list){
 const segs=[];let cur=null;
 (list||[]).forEach(p=>{
  const t=new Date(p.last_changed).getTime();
  if(cur)cur.t1=t;
  cur={t0:t,t1:null,state:p.state};segs.push(cur);});
 if(cur)cur.t1=Date.now();
 return segs;}
const SEG_COLORS={on:'#3ddc84',off:'#39424f',home:'#3ddc84',not_home:'#39424f',unavailable:'#262b33',
 unknown:'#262b33',open:'#3ddc84',closed:'#39424f',locked:'#ff6b6b',unlocked:'#3ddc84',
 playing:'#5eb1ff',paused:'#ffb454',idle:'#39424f',standby:'#39424f',cleaning:'#5eb1ff',docked:'#39424f'};
function drawTimeline(cv,rows){
 /* rows: [{name,segs:[{t0,t1,state}]}] */
 if(!cv)return;
 const dpr=window.devicePixelRatio||1;
 const W=cv.clientWidth||600,rh=34,pad={l:110,r:14,t:8,b:24};
 const H=Math.max(80,pad.t+pad.b+rows.length*rh);
 cv.width=W*dpr;cv.height=H*dpr;cv.style.height=H+'px';
 const g=cv.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);
 const cs=getComputedStyle(document.body);
 const txtC=cs.getPropertyValue('--mut').trim(),lineC=cs.getPropertyValue('--line').trim();
 let tmin=Infinity,tmax=-Infinity;
 rows.forEach(r=>r.segs.forEach(s=>{if(s.t0<tmin)tmin=s.t0;if(s.t1>tmax)tmax=s.t1;}));
 if(!isFinite(tmin)){g.fillStyle=txtC;g.font='13px sans-serif';g.fillText('Нет данных',pad.l,30);return;}
 if(tmax<=tmin)tmax=tmin+1000;
 const X=t=>pad.l+(t-tmin)/(tmax-tmin)*(W-pad.l-pad.r);
 g.clearRect(0,0,W,H);
 rows.forEach((r,i)=>{
  const y=pad.t+i*rh;
  g.fillStyle=txtC;g.font='11.5px sans-serif';
  g.fillText(r.name.length>16?r.name.slice(0,15)+'…':r.name,6,y+rh/2+4);
  r.segs.forEach(s=>{
   const x0=X(s.t0),x1=X(s.t1==null?tmax:s.t1);
   g.fillStyle=SEG_COLORS[s.state]||PALETTE[Math.abs(hashStr(s.state))%PALETTE.length];
   g.globalAlpha=(s.state==='unavailable'||s.state==='unknown')?.5:1;
   const w=Math.max(x1-x0,1.2);
   g.beginPath();g.roundRect?g.roundRect(x0,y+5,w,rh-12,3):g.rect(x0,y+5,w,rh-12);g.fill();
   g.globalAlpha=1;
   if(w>34){g.fillStyle=(s.state==='on'||s.state==='home'||s.state==='unlocked')?'#06121a':'#dfe6f0';
    g.font='9.5px sans-serif';g.fillText(s.state,x0+4,y+rh/2+3);}});
  g.strokeStyle=lineC;g.globalAlpha=.4;g.beginPath();g.moveTo(pad.l,y+rh);g.lineTo(W-pad.r,y+rh);g.stroke();g.globalAlpha=1;});
 for(let i=0;i<=4;i++){const t=tmin+(tmax-tmin)*i/4;
  g.fillStyle=txtC;g.font='10px sans-serif';
  g.fillText(new Date(t).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}),Math.min(Math.max(X(t)-16,pad.l),W-52),H-8);}
 const tip=$('#chartTip');
 cv.onmousemove=e=>{
  const r=cv.getBoundingClientRect();const mx=e.clientX-r.left,my=e.clientY-r.top;
  const ri=Math.floor((my-pad.t)/rh);
  if(ri<0||ri>=rows.length||mx<pad.l){tip.classList.add('hidden');return;}
  const t=tmin+(mx-pad.l)/(W-pad.l-pad.r)*(tmax-tmin);
  const seg=rows[ri].segs.find(s=>t>=s.t0&&t<=(s.t1==null?Infinity:s.t1));
  if(!seg){tip.classList.add('hidden');return;}
  tip.innerHTML=`<b>${esc(rows[ri].name)}</b><div>${esc(RU_STATE[seg.state]||seg.state)}</div>
   <div style="color:var(--mut)">${new Date(seg.t0).toLocaleString('ru-RU')}</div>`;
  tip.classList.remove('hidden');
  tip.style.left=Math.min(e.clientX+14,window.innerWidth-250)+'px';tip.style.top=(e.clientY-10)+'px';};
 cv.onmouseleave=()=>tip.classList.add('hidden');
}
function hashStr(s){let h=0;for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))|0;return h;}

/* ═══════════════════════ ТЕМА ═══════════════════════ */
/* Тема — общая с системой (static/js/theme.js, ключ localStorage
   'infinity_theme'). Панель не переключает тему сама: она читает текущую
   и меняет её через InfinityTheme, поэтому выбор на любой странице
   InfinitySearch сразу виден здесь и наоборот. */
const HA_THEME_LIST=['light','dark','sepia','warm','paper','ocean','forest','midnight','rose'];
const HA_DARK_THEMES=['dark','midnight','forest'];
const HA_THEME_TITLE={light:'☀️ Светлая',dark:'🌙 Тёмная',sepia:'📜 Сепия',warm:'🔥 Тёплая',
 paper:'📄 Бумага',ocean:'🌊 Океан',forest:'🌲 Лес',midnight:'🌌 Полночь',rose:'🌹 Роза'};
function haTheme(){try{
 if(window.InfinityTheme&&InfinityTheme.get)return InfinityTheme.get();
 return localStorage.getItem('infinity_theme')||'light';}catch(_){return 'light';}}
function applyTheme(){
 const t=haTheme();
 const b=$('#themeBtn');if(b)b.textContent=HA_DARK_THEMES.indexOf(t)>=0?'☀️':'🌙';
 const m=document.querySelector('meta[name=theme-color]');
 if(m){try{const c=getComputedStyle(document.body).getPropertyValue('--bg');
  if(c&&c.trim())m.content=c.trim();}catch(_){}}}
function toggleTheme(){
  try{if(window.InfinityTheme&&InfinityTheme.toggle)InfinityTheme.toggle();
  else{localStorage.setItem('infinity_theme',haTheme()==='dark'?'light':'dark');applyTheme();}
  }catch(_){}
  haThemeChanged();}
/* Синхронизируем <select> темы в настройках, не перерисовывая страницу:
   полная перерисовка сбросила бы введённые URL/токен. */
function haSyncThemeSelect(){const s=document.getElementById('haThemeSel');
 if(!s)return;const t=haTheme();if(s.value!==t)s.value=t;}
function haThemeChanged(){applyTheme();haSyncThemeSelect();refreshRoute();}
function haOnThemeChange(){try{
 if(window.InfinityTheme&&InfinityTheme.onChange)
  InfinityTheme.onChange(haThemeChanged);}catch(_){}}


/* ═══════════════════════ НАВИГАЦИЯ / РОУТЕР ═══════════════════════ */
const ROUTES=[
 {r:'dashboard',ic:'🏠',t:'Обзор',fn:pageDashboard},
 {r:'map',ic:'🗺️',t:'Карта',fn:pageMap},
 {r:'history',ic:'📈',t:'История',fn:pageHistory},
 {r:'logbook',ic:'📃',t:'Журнал',fn:pageLogbook},
 {sep:1},
 {r:'automations',ic:'🤖',t:'Автоматизации',fn:pageAutomations},
 {r:'scenes',ic:'🎬',t:'Сцены',fn:pageScenes},
 {r:'scripts',ic:'📜',t:'Скрипты',fn:pageScripts},
 {sep:1},
 {r:'cameras',ic:'📷',t:'Камеры',fn:pageCameras},
 {r:'media',ic:'🎵',t:'Медиа',fn:pageMedia},
 {r:'climate',ic:'🌡️',t:'Климат',fn:pageClimate},
 {sep:1},
 {r:'devices',ic:'🧩',t:'Устройства',fn:pageDevices},
 {r:'entities',ic:'📇',t:'Сущности',fn:pageEntities},
 {r:'areas',ic:'🗂️',t:'Области',fn:pageAreas},
 {r:'integrations',ic:'🔌',t:'Интеграции',fn:pageIntegrations},
 {sep:1},
 {r:'dev',ic:'🛠️',t:'Разработчику',fn:pageDev},
 {r:'system',ic:'🖥️',t:'Система',fn:pageSystem},
 {r:'settings',ic:'⚙️',t:'Настройки',fn:pageSettings}];
const DOMAIN_ORDER=['light','climate','cover','media_player','fan','vacuum','lock','switch','camera',
 'sensor','binary_sensor','weather','person','device_tracker','alarm_control_panel','update','button',
 'automation','scene','script','input_boolean','input_number','input_select','input_text','select',
 'number','timer','humidifier','water_heater','siren','valve','lawn_mower','remote','sun','zone','image','event'];
const DOMAIN_RU={light:'Свет',climate:'Климат',cover:'Шторы и ворота',media_player:'Медиаплееры',
 fan:'Вентиляция',vacuum:'Пылесосы',lock:'Замки',switch:'Выключатели',camera:'Камеры',sensor:'Датчики',
 binary_sensor:'Бинарные датчики',weather:'Погода',person:'Люди',device_tracker:'Трекинг',
 automation:'Автоматизации',scene:'Сцены',script:'Скрипты',update:'Обновления',button:'Кнопки',
 input_boolean:'Переключатели (input)',input_number:'Числа (input)',input_select:'Списки (input)',
 input_text:'Текст (input)',input_datetime:'Дата/время (input)',input_button:'Кнопки (input)',
 timer:'Таймеры',select:'Списки',number:'Числа',humidifier:'Увлажнители',water_heater:'Водонагреватели',
 alarm_control_panel:'Охранная сигнализация',siren:'Сирены',sun:'Солнце',zone:'Зоны',remote:'Пульты',
 valve:'Краны',lawn_mower:'Газонокосилки',image:'Изображения',notify:'Уведомления',stt:'Распознавание речи',
 tts:'Синтез речи',conversation:'Ассистент',event:'События',persistent_notification:'Уведомления HA'};
function buildNav(){
 $('#nav').innerHTML=ROUTES.map(x=>x.sep?'<div class="nav-sep"></div>':
  `<a class="nav-i" data-r="${x.r}" onclick="go('${x.r}')"><span class="nav-ic">${x.ic}</span>${x.t}</a>`).join('');}
function go(r){
 if(!ROUTES.find(x=>x.r===r))r='dashboard';
 if(location.hash!=='#'+r){location.hash=r;return;} /* hashchange вызовет go повторно */
 setRoute(r);}
function setRoute(r){
 ROUTE=r;
 CLEAN.forEach(f=>{try{f()}catch(_){}});CLEAN=[];
 closeDialog();
 $$('.nav-i').forEach(a=>a.classList.toggle('active',a.dataset.r===r));
 const meta=ROUTES.find(x=>x.r===r);
 $('#pageTitle').textContent=meta?meta.t:'';
 $('#view').innerHTML='<div class="loading">Загрузка…</div>';
 window.scrollTo(0,0);
 try{meta.fn();}catch(e){$('#view').innerHTML='<div class="empty">Ошибка страницы: '+esc(e.message)+'</div>';console.error(e);}
 if(window.innerWidth<900)$('#sidebar').classList.remove('open');}
function refreshRoute(){
 if(ROUTE==='dashboard'&&$('#cards'))renderCards();
 else if(ROUTE==='dashboard')return;
 /* лёгкое обновление для страниц со значениями */
 const cs=$('#cards');if(cs)renderCards();}

/* ═══════════════════════ РЕЕСТРЫ (WS API) ═══════════════════════ */
async function loadRegistries(){
 try{
  const [ents,areas,devs]=await Promise.all([
   haproxy({type:'config/entity_registry/list'}),
   haproxy({type:'config/area_registry/list'}),
   haproxy({type:'config/device_registry/list'})]);
  REG.entities=ents||[];REG.areas=areas||[];REG.devices=devs||[];
  REG.map={};REG.entities.forEach(r=>REG.map[r.entity_id]=r);
 }catch(e){console.warn('Реестры недоступны:',e.message);}
}
function areaOf(eid){
 const reg=REG.map[eid];if(!reg)return null;
 if(reg.area_id)return reg.area_id;
 if(reg.device_id){const dv=REG.devices.find(d=>d.id===reg.device_id);if(dv&&dv.area_id)return dv.area_id;}
 return null;}
function areaName(id){if(!id)return null;const a=REG.areas.find(x=>x.area_id===id);return a?a.name:id;}
function objectIdOf(eid){const r=REG.map[eid];return (r&&r.unique_id)?r.unique_id:eid.split('.').slice(1).join('.');}

/* ═══════════════════════ ДАШБОРД ═══════════════════════ */
function pageDashboard(){
 const prefs=LS('hap_prefs')||{};const grouping=prefs.grouping||'area';
 $('#view').innerHTML=`
 <div class="page-head">
  <input id="dashSearch" class="input search" placeholder="🔍 Поиск сущностей…">
  <div class="seg">
   <button class="seg-btn ${grouping==='area'?'active':''}" onclick="setGrouping('area')">По областям</button>
   <button class="seg-btn ${grouping==='domain'?'active':''}" onclick="setGrouping('domain')">По типам</button>
  </div>
 </div>
 <div id="domainChips" class="chips"></div>
 <div id="cards"></div>`;
 $('#dashSearch').oninput=debounce(()=>renderCards(),140);
 renderDomainChips();renderCards();}
function setGrouping(g){const p=LS('hap_prefs')||{};p.grouping=g;LS('hap_prefs',p);pageDashboard();}
function setDashDomain(d){DASH_DOMAIN=d;renderDomainChips();renderCards();}
function renderDomainChips(){
 const box=$('#domainChips');if(!box)return;
 const counts={};Object.keys(STATES).forEach(e=>{const d=dom(e);counts[d]=(counts[d]||0)+1;});
 const order=DOMAIN_ORDER.filter(d=>counts[d]);
 const extra=Object.keys(counts).filter(d=>!DOMAIN_ORDER.includes(d)).sort();
 const list=[''].concat(order,extra);
 box.innerHTML=list.map(d=>`<button class="chip ${DASH_DOMAIN===d?'active':''}" onclick="setDashDomain('${d}')">
  ${d===''?'✦ Все':iconOfDomain(d)+' '+(DOMAIN_RU[d]||d)} <span class="cnt">${d===''?Object.keys(STATES).length:counts[d]}</span></button>`).join('');}
function sortEids(list){return list.sort((a,b)=>{
 const da=DOMAIN_ORDER.indexOf(dom(a)),db=DOMAIN_ORDER.indexOf(dom(b));
 if(da!==db)return (da<0?99:da)-(db<0?99:db);
 return FN(a).localeCompare(FN(b),'ru');});}
function renderCards(){
 const box=$('#cards');if(!box)return;
 const prefs=LS('hap_prefs')||{};const grouping=prefs.grouping||'area';
 const se=$('#dashSearch');const q=se?se.value.trim().toLowerCase():'';
 let eids=Object.keys(STATES);
 if(DASH_DOMAIN)eids=eids.filter(e=>dom(e)===DASH_DOMAIN);
 if(q)eids=eids.filter(e=>e.includes(q)||FN(e).toLowerCase().includes(q));
 const hidden=new Set(REG.entities.filter(r=>r.hidden_by||r.disabled_by).map(r=>r.entity_id));
 if(!q&&!DASH_DOMAIN)eids=eids.filter(e=>!hidden.has(e)&&dom(e)!=='persistent_notification');
 const groups={};
 const put=(g,e)=>{(groups[g]=groups[g]||[]).push(e);};
 if(grouping==='area')eids.forEach(e=>{const ar=areaOf(e);put(ar?areaName(ar):'Без области',e);});
 else eids.forEach(e=>put(DOMAIN_RU[dom(e)]||dom(e),e));
 const keys=Object.keys(groups).sort((a,b)=>a==='Без области'?1:b==='Без области'?-1:a.localeCompare(b,'ru'));
 let html='';
 keys.forEach(k=>{const list=sortEids(groups[k]);
  html+=`<div class="group"><div class="group-title">${esc(k)} <span class="cnt">${list.length}</span></div>
  <div class="cards">${list.map(cardHtml).join('')}</div></div>`;});
 if(!html)html=`<div class="empty">${Object.keys(STATES).length?'Ничего не найдено':'Нет данных от Home Assistant. Проверьте подключение во вкладке «Настройки».'}</div>`;
 box.innerHTML=html;
 $$('#cards .card').forEach(c=>{c.onclick=e=>{if(e.target.closest('.card-tgl'))return;moreInfo(c.dataset.eid);};});
 $$('#cards .card-tgl').forEach(b=>{b.onclick=e=>{e.stopPropagation();toggleEntity(b.dataset.eid);};});}
function cardHtml(eid){
 const st=STATES[eid],d=dom(eid);
 return `<div class="card ${isOn(st)?'on':''}" data-eid="${eid}">
  <div class="card-ic">${iconOf(st)}</div>
  <div class="card-b"><div class="card-n" title="${esc(FN(eid))}">${esc(FN(eid))}</div>
  <div class="card-s">${esc(stateText(st))}</div></div>
  ${TOGGLEABLE.has(d)?`<button class="card-tgl" data-eid="${eid}" title="Переключить">⏻</button>`:''}
 </div>`;}
function onStateChange(eid){
 const card=document.querySelector(`.card[data-eid="${eid}"]`);
 if(card){const st=STATES[eid];
  if(st){card.classList.toggle('on',isOn(st));
   const s=card.querySelector('.card-s');if(s)s.textContent=stateText(st);}
  else card.remove();}
 const de=$('#dlgEid');
 if(de&&de.value===eid)refreshDialogValues(eid);
 if(ROUTE==='climate'&&$('#climateCards'))pageClimate();
 if(ROUTE==='media'&&$('#mediaCards'))renderMediaCards();}

/* ═══════════════════════ ДИАЛОГ СУЩНОСТИ (more-info) ═══════════════════════ */
function moreInfo(eid){
 const st=S(eid);if(!st){toast('Состояние недоступно','err');return;}
 const d=dom(eid);
 let ctl='';
 switch(d){
  case 'light':ctl=ctlLight(eid);break;
  case 'climate':ctl=ctlClimate(eid);break;
  case 'cover':ctl=ctlCover(eid);break;
  case 'media_player':ctl=ctlMedia(eid);break;
  case 'fan':ctl=ctlFan(eid);break;
  case 'vacuum':ctl=ctlVacuum(eid);break;
  case 'lock':ctl=ctlLock(eid);break;
  case 'camera':ctl=ctlCamera(eid);break;
  case 'alarm_control_panel':ctl=ctlAlarm(eid);break;
  case 'humidifier':ctl=ctlHumidifier(eid);break;
  case 'water_heater':ctl=ctlWaterHeater(eid);break;
  case 'siren':ctl=ctlSiren(eid);break;
  case 'input_number':case 'number':ctl=ctlNumber(eid,d);break;
  case 'input_select':case 'select':ctl=ctlSelect(eid,d);break;
  case 'input_text':ctl=ctlText(eid);break;
  case 'timer':ctl=ctlTimer(eid);break;
  case 'weather':ctl=ctlWeather(eid);break;
  case 'button':case 'input_button':
   ctl=`<button class="btn accent wide" onclick="callSvc('${d}','press',{entity_id:'${eid}'})">👆 Нажать</button>`;break;
  case 'scene':
   ctl=`<button class="btn accent wide" onclick="callSvc('scene','turn_on',{entity_id:'${eid}'})">🎬 Применить сцену</button>`;break;
  case 'script':ctl=ctlScript(eid);break;
  case 'automation':
   ctl=`<div class="row"><button class="btn ${st.state==='on'?'ok':''}" onclick="callSvc('automation','toggle',{entity_id:'${eid}'})">⏻ Вкл/выкл</button>
   <button class="btn accent" onclick="callSvc('automation','trigger',{entity_id:'${eid}'})">▶ Запустить</button>
   <button class="btn" onclick="closeDialog();editAutomation('${objectIdOf(eid)}')">✎ Редактировать</button></div>
   <div class="hint">Последний запуск: ${esc(ftime(st.attributes.last_triggered))}</div>`;break;
  case 'update':ctl=ctlUpdate(eid);break;
  case 'person':case 'device_tracker':
   ctl=`<div class="kv-big"><div><div class="v">${esc(st.state)}</div><div class="k">статус</div></div>
   ${st.attributes.latitude?`<div><div class="v">${(+st.attributes.latitude).toFixed(4)}, ${(+st.attributes.longitude).toFixed(4)}</div><div class="k">координаты</div></div>`:''}</div>
   <div class="row"><button class="btn" onclick="go('map')">🗺️ Показать на карте</button></div>`;break;
  case 'sun':
   ctl=`<div class="kv-big"><div><div class="v">${iconOf(st)} ${esc(RU_STATE[st.state]||st.state)}</div><div class="k">состояние</div></div>
   <div><div class="v">${esc(st.attributes.elevation!=null?(+st.attributes.elevation).toFixed(1)+'°':'—')}</div><div class="k">высота</div></div>
   <div><div class="v">${esc(st.attributes.azimuth!=null?(+st.attributes.azimuth).toFixed(1)+'°':'—')}</div><div class="k">азимут</div></div></div>`;break;
  case 'timer':break;
 }
 if(!ctl&&TOGGLEABLE.has(d))ctl=`<button class="btn accent wide" onclick="toggleEntity('${eid}')">Переключить</button>`;
 const attrs=st.attributes||{};
 const attrsHtml=Object.keys(attrs).filter(k=>k!=='friendly_name'&&k!=='supported_features').map(k=>
  `<tr><td>${esc(k)}</td><td class="mono">${esc(fmtAttr(attrs[k]))}</td></tr>`).join('');
 openDialog(`
 <input type="hidden" id="dlgEid" value="${eid}">
 <div class="dlg-head">
  <div class="dlg-ic ${isOn(st)?'on':''}" id="dlgIc">${iconOf(st)}</div>
  <div style="min-width:0"><div class="dlg-title">${esc(FN(eid))}</div>
  <div class="dlg-sub mono">${eid} · <span id="dlgState">${esc(stateText(st))}</span> · ${esc(ftime(st.last_changed))}</div></div>
  <div class="spacer"></div>
  ${TOGGLEABLE.has(d)?`<button class="pwr ${isOn(st)?'on':''}" id="dlgPwr" onclick="toggleEntity('${eid}')">⏻</button>`:''}
  <button class="icon-btn" onclick="closeDialog()">✕</button>
 </div>
 <div class="dlg-body">
  ${ctl}
  <div class="hist-box"><div class="sec-title">История за 24 часа</div><canvas id="sparkCv" class="spark"></canvas></div>
  <details class="attrs"><summary>Атрибуты (${Object.keys(attrs).length})</summary><table class="kv">${attrsHtml||'<tr><td>нет</td></tr>'}</table></details>
  <details class="attrs"><summary>🛠 Вызвать службу для этой сущности</summary>
   <div class="form">
    <label>Служба</label><input id="svcName" class="input mono" value="${d}.${TOGGLEABLE.has(d)?'toggle':'turn_on'}">
    <label>Данные (JSON)</label>
    <textarea id="svcData" class="input mono" rows="4">{\n "entity_id": "${eid}"\n}</textarea>
    <button class="btn accent" style="margin-top:10px" onclick="callSvcFromDialog()">Вызвать службу</button>
   </div>
  </details>
 </div>`);
 loadSpark(eid);
 if(d==='weather')loadForecast(eid);
}
async function callSvcFromDialog(){
 const [d,s]=$('#svcName').value.split('.');
 let data;try{data=JSON.parse($('#svcData').value||'{}');}catch(e){toast('JSON-данные некорректны','err');return;}
 try{const r=await callSvc(d,s,data);closeDialog();}catch(_){}}
function refreshDialogValues(eid){
 const st=S(eid);if(!st)return;
 const d=dom(eid);
 const sub=$('#dlgState');if(sub)sub.textContent=stateText(st);
 const pwr=$('#dlgPwr');if(pwr)pwr.classList.toggle('on',isOn(st));
 const ic=$('#dlgIc');if(ic)ic.classList.toggle('on',isOn(st));
 const bri=$('#lv-bri-'+eid);
 if(bri&&document.activeElement!==document.getElementById('sl-bri-'+eid))
  bri.textContent=st.attributes.brightness!=null?Math.round(st.attributes.brightness/2.55)+'%':'—';
 const cur=$('#cur-temp-'+eid);
 if(cur&&st.attributes.current_temperature!=null)cur.textContent=st.attributes.current_temperature+'°';
 const mt=$('#media-title-'+eid);
 if(mt)mt.textContent=st.attributes.media_title||stateText(st);
 const pos=$('#lv-pos-'+eid);
 if(pos&&st.attributes.current_position!=null)pos.textContent=st.attributes.current_position+'%';
}

/* ── контролы доменов ── */
function rgbFromState(a){
 if(Array.isArray(a.rgb_color))return '#'+a.rgb_color.map(v=>('0'+v.toString(16)).slice(-2)).join('');
 if(Array.isArray(a.hs_color))return '#'+hs2hex(a.hs_color[0],a.hs_color[1]);
 if(Array.isArray(a.xy_color)){const c=xy2rgb(a.xy_color[0],a.xy_color[1]);return '#'+c.map(v=>('0'+v.toString(16)).slice(-2)).join('');}
 return '#ffffff';}
function hs2hex(h,s){s/=100;const v=1;h/=60;
 const i=Math.floor(h),f=h-i,p=v*(1-s),q=v*(1-s*f),t=v*(1-s*(1-f));
 let r,g,b;
 switch(i%6){case 0:r=v;g=t;b=p;break;case 1:r=q;g=v;b=p;break;case 2:r=p;g=v;b=t;break;
  case 3:r=p;g=q;b=v;break;case 4:r=t;g=p;b=v;break;default:r=v;g=p;b=q;}
 const to=x=>('0'+Math.round(x*255).toString(16)).slice(-2);
 return to(r)+to(g)+to(b);}
function xy2rgb(x,y){const z=1-x-y;
 let r=x*1.656492-y*0.354851-z*0.255038,g=-x*0.707196+y*1.655397+z*0.036152,b=x*0.051713-y*0.121364+z*1.01153;
 const m=Math.max(r,g,b);if(m>1){r/=m;g/=m;b/=m;}
 const gam=v=>v<=0.0031308?12.92*v:1.055*Math.pow(v,1/2.4)-0.055;
 return [Math.round(gam(Math.max(r,0))*255),Math.round(gam(Math.max(g,0))*255),Math.round(gam(Math.max(b,0))*255)];}
function hex2rgb(h){h=h.replace('#','');return [parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)];}
function ctlLight(eid){
 const st=S(eid),a=st.attributes||{};
 const modes=a.supported_color_modes||[];
 const bri=a.brightness!=null?Math.round(a.brightness/2.55):0;
 let html=`<div class="row" style="margin-bottom:14px">
  <button class="btn ${st.state==='on'?'':'accent'}" style="flex:1" onclick="callSvc('light','turn_on',{entity_id:'${eid}'})">💡 Включить</button>
  <button class="btn ${st.state==='off'?'':'accent'}" style="flex:1" onclick="callSvc('light','turn_off',{entity_id:'${eid}'})">⏻ Выключить</button></div>`;
 if(st.state==='on'){
  if(modes.length){
   html+=`<div class="ctl"><label>Яркость · <span id="lv-bri-${eid}">${bri}%</span></label>
   <input id="sl-bri-${eid}" type="range" min="1" max="100" value="${bri||1}"
    oninput="document.getElementById('lv-bri-${eid}').textContent=this.value+'%'"
    onchange="callSvc('light','turn_on',{entity_id:'${eid}',brightness_pct:+this.value},true)"></div>`;}
  if(modes.includes('color_temp')){
   const minK=a.min_color_temp_kelvin||(a.min_mireds?Math.round(1e6/a.min_mireds):2000);
   const maxK=a.max_color_temp_kelvin||(a.max_mireds?Math.round(1e6/a.max_mireds):6500);
   const curK=a.color_temp_kelvin||(a.color_temp?Math.round(1e6/a.color_temp):Math.round((minK+maxK)/2));
   html+=`<div class="ctl"><label>Температура · <span id="lv-ct-${eid}">${curK}K</span></label>
   <input type="range" class="ct-slider" min="${minK}" max="${maxK}" step="50" value="${curK}"
    oninput="document.getElementById('lv-ct-${eid}').textContent=this.value+'K'"
    onchange="callSvc('light','turn_on',{entity_id:'${eid}',color_temp_kelvin:+this.value},true)"></div>`;}
  if(modes.some(m=>['hs','rgb','rgbw','rgbw','xy'].includes(m))){
   html+=`<div class="ctl row"><label style="margin:0">Цвет</label>
   <input type="color" value="${rgbFromState(a)}"
    onchange="callSvc('light','turn_on',{entity_id:'${eid}',rgb_color:hex2rgb(this.value)},true)">
   <span class="hint" style="margin:0">выбранный цвет применяется сразу</span></div>`;}
  if(a.effect_list&&a.effect_list.length){
   html+=`<div class="ctl"><label>Эффект</label><select class="input"
    onchange="callSvc('light','turn_on',{entity_id:'${eid}',effect:this.value||undefined},true)">
    <option value="">— нет —</option>
    ${a.effect_list.map(x=>`<option ${a.effect===x?'selected':''}>${esc(x)}</option>`).join('')}</select></div>`;}
 }else html+=`<div class="hint">Включите свет, чтобы настраивать яркость и цвет.</div>`;
 return html;}
function ctlClimate(eid){
 const st=S(eid),a=st.attributes||{};
 const modes=a.hvac_modes||[];
 const step=a.target_temp_step||0.5;
 const tm=a.hvac_mode||st.state;
 let html=`<div class="kv-big">
  <div><div class="v" id="cur-temp-${eid}">${a.current_temperature!=null?a.current_temperature+'°':'—'}</div><div class="k">сейчас</div></div>
  <div><div class="v">${a.current_humidity!=null?a.current_humidity+'%':'—'}</div><div class="k">влажность</div></div></div>`;
 if(tm==='heat_cool'&&a.target_temp_high!=null){
  html+=`<div class="ctl"><label>Диапазон: ${a.target_temp_low}° … ${a.target_temp_high}°</label>
  <div class="row">
   <button class="btn" onclick="adjTemp('${eid}','low',-${step})">− нижняя</button>
   <button class="btn" onclick="adjTemp('${eid}','low',${step})">+ нижняя</button>
   <button class="btn" onclick="adjTemp('${eid}','high',-${step})">− верхняя</button>
   <button class="btn" onclick="adjTemp('${eid}','high',${step})">+ верхняя</button></div></div>`;
 }else if(a.temperature!=null){
  html+=`<div class="ctl"><label>Целевая температура</label>
  <div class="row" style="gap:16px">
   <button class="btn" style="font-size:20px;padding:10px 18px" onclick="adjTemp('${eid}','t',-${step})">−</button>
   <span class="big-val" id="tgt-${eid}">${a.temperature}°</span>
   <button class="btn" style="font-size:20px;padding:10px 18px" onclick="adjTemp('${eid}','t',${step})">+</button></div></div>`;}
 if(modes.length)html+=`<div class="ctl"><label>Режим</label><div class="mode-chips">
  ${modes.map(m=>`<button class="chip ${tm===m?'active':''}" onclick="callSvc('climate','set_hvac_mode',{entity_id:'${eid}',hvac_mode:'${m}'})">${RU_STATE[m]||m}</button>`).join('')}</div></div>`;
 if(a.fan_modes&&a.fan_modes.length)html+=selCtl('Вентилятор',a.fan_modes,a.fan_mode,
  `v=>callSvc('climate','set_fan_mode',{entity_id:'${eid}',fan_mode:v})`,'fan-'+eid);
 if(a.swing_modes&&a.swing_modes.length)html+=selCtl('Качание',a.swing_modes,a.swing_mode,
  `v=>callSvc('climate','set_swing_mode',{entity_id:'${eid}',swing_mode:v})`,'swing-'+eid);
 if(a.preset_modes&&a.preset_modes.length)html+=selCtl('Пресет',a.preset_modes,a.preset_mode,
  `v=>callSvc('climate','set_preset_mode',{entity_id:'${eid}',preset_mode:v})`,'preset-'+eid);
 if(a.humidity!=null&&a.min_humidity!=null)html+=`<div class="ctl"><label>Целевая влажность · ${a.humidity}%</label>
  <input type="range" min="${a.min_humidity}" max="${a.max_humidity}" value="${a.humidity}"
   onchange="callSvc('climate','set_humidity',{entity_id:'${eid}',humidity:+this.value},true)"></div>`;
 return html;}
async function adjTemp(eid,which,delta){
 const a=S(eid).attributes||{};
 const data={entity_id:eid};
 if(which==='t'){const v=Math.round(((a.temperature||20)+delta)*10)/10;data.temperature=v;
  const t=$('#tgt-'+eid);if(t)t.textContent=v+'°';}
 else if(which==='low'){const v=Math.round(((a.target_temp_low||18)+delta)*10)/10;
  data.target_temp_low=v;data.target_temp_high=a.target_temp_high;}
 else{const v=Math.round(((a.target_temp_high||26)+delta)*10)/10;
  data.target_temp_high=v;data.target_temp_low=a.target_temp_low;}
 callSvc('climate','set_temperature',data,true);}
function selCtl(label,options,current,fnExpr,id){
 return `<div class="ctl"><label>${label}</label><select class="input" id="${id}"
  onchange="(${fnExpr})(this.value)">
  ${options.map(o=>`<option ${o===current?'selected':''}>${esc(o)}</option>`).join('')}</select></div>`;}
function ctlCover(eid){
 const st=S(eid),a=st.attributes||{};
 let html=`<div class="row" style="margin-bottom:12px">
  <button class="btn" style="flex:1" onclick="callSvc('cover','open_cover',{entity_id:'${eid}'})">⬆ Открыть</button>
  <button class="btn" style="flex:1" onclick="callSvc('cover','stop_cover',{entity_id:'${eid}'})">✋ Стоп</button>
  <button class="btn" style="flex:1" onclick="callSvc('cover','close_cover',{entity_id:'${eid}'})">⬇ Закрыть</button></div>`;
 if(a.current_position!=null)html+=`<div class="ctl"><label>Позиция · <span id="lv-pos-${eid}">${a.current_position}%</span></label>
  <input type="range" min="0" max="100" value="${a.current_position}"
   oninput="document.getElementById('lv-pos-${eid}').textContent=this.value+'%'"
   onchange="callSvc('cover','set_cover_position',{entity_id:'${eid}',position:+this.value},true)"></div>`;
 if(a.current_tilt_position!=null)html+=`<div class="ctl"><label>Наклон ламелей · ${a.current_tilt_position}%</label>
  <input type="range" min="0" max="100" value="${a.current_tilt_position}"
   onchange="callSvc('cover','set_cover_tilt_position',{entity_id:'${eid}',tilt_position:+this.value},true)"></div>`;
 return html;}
function ctlMedia(eid){
 const st=S(eid),a=st.attributes||{};
 const s=st.state;
 let html=`<div class="panel" style="margin-bottom:12px">
  <div style="font-weight:800;font-size:15px" id="media-title-${eid}">${esc(a.media_title||RU_STATE[s]||s)}</div>
  <div class="hint">${esc([a.media_artist,a.media_album_name,a.media_content_type].filter(Boolean).join(' — ')||'нет метаданных')}</div></div>`;
 html+=`<div class="media-row" style="justify-content:center">
  <button class="btn" onclick="callSvc('media_player','media_previous_track',{entity_id:'${eid}'})">⏮</button>
  <button class="btn accent" style="font-size:19px" onclick="callSvc('media_player','media_play_pause',{entity_id:'${eid}'})">${s==='playing'?'⏸':'▶'}</button>
  <button class="btn" onclick="callSvc('media_player','media_next_track',{entity_id:'${eid}'})">⏭</button>
  <button class="btn" onclick="callSvc('media_player','media_stop',{entity_id:'${eid}'})">⏹</button></div>`;
 if(a.volume_level!=null)html+=`<div class="ctl" style="margin-top:14px"><label>Громкость · <span id="lv-vol-${eid}">${Math.round(a.volume_level*100)}%</span></label>
  <div class="row"><button class="btn sm" onclick="callSvc('media_player','volume_mute',{entity_id:'${eid}',is_volume_muted:${!a.is_volume_muted}},true)">${a.is_volume_muted?'🔇':'🔊'}</button>
  <input type="range" style="flex:1" min="0" max="100" value="${Math.round(a.volume_level*100)}"
   oninput="document.getElementById('lv-vol-${eid}').textContent=this.value+'%'"
   onchange="callSvc('media_player','volume_set',{entity_id:'${eid}',volume_level:this.value/100},true)"></div></div>`;
 if(a.source_list&&a.source_list.length)html+=selCtl('Источник',a.source_list,a.source,
  `v=>callSvc('media_player','select_source',{entity_id:'${eid}',source:v})`,'src-'+eid);
 if(a.sound_mode_list&&a.sound_mode_list.length)html+=selCtl('Режим звука',a.sound_mode_list,a.sound_mode,
  `v=>callSvc('media_player','select_sound_mode',{entity_id:'${eid}',sound_mode:v})`,'sm-'+eid);
 return html;}
function ctlFan(eid){
 const st=S(eid),a=st.attributes||{};
 let html=`<div class="row" style="margin-bottom:12px">
  <button class="btn ${st.state==='on'?'ok':'accent'}" style="flex:1" onclick="callSvc('fan','turn_on',{entity_id:'${eid}'})">Включить</button>
  <button class="btn ${st.state==='off'?'ok':'accent'}" style="flex:1" onclick="callSvc('fan','turn_off',{entity_id:'${eid}'})">Выключить</button></div>`;
 if(st.state==='on'&&a.percentage!=null)html+=`<div class="ctl"><label>Скорость · ${a.percentage}%</label>
  <input type="range" min="0" max="100" step="${a.percentage_step||1}" value="${a.percentage}"
   onchange="callSvc('fan','set_percentage',{entity_id:'${eid}',percentage:+this.value},true)"></div>`;
 if(a.preset_modes&&a.preset_modes.length)html+=selCtl('Пресет',a.preset_modes,a.preset_mode,
  `v=>callSvc('fan','set_preset_mode',{entity_id:'${eid}',preset_mode:v})`,'fp-'+eid);
 if(a.oscillating!=null)html+=`<div class="ctl row"><label style="margin:0">Качание</label>
  <button class="btn sm ${a.oscillating?'ok':''}" onclick="callSvc('fan','oscillate',{entity_id:'${eid}',oscillating:${!a.oscillating}},true)">${a.oscillating?'вкл':'выкл'}</button></div>`;
 return html;}
function ctlVacuum(eid){
 const st=S(eid),a=st.attributes||{};
 let html=`<div class="kv-big"><div><div class="v">${esc(RU_STATE[st.state]||st.state)}</div><div class="k">статус</div></div>
  ${a.battery_level!=null?`<div><div class="v">${a.battery_level}%</div><div class="k">батарея</div></div>`:''}</div>
 <div class="row" style="margin-bottom:12px">
  <button class="btn accent" onclick="callSvc('vacuum','start',{entity_id:'${eid}'})">▶ Старт</button>
  <button class="btn" onclick="callSvc('vacuum','pause',{entity_id:'${eid}'})">⏸ Пауза</button>
  <button class="btn" onclick="callSvc('vacuum','stop',{entity_id:'${eid}'})">⏹ Стоп</button>
  <button class="btn" onclick="callSvc('vacuum','return_to_base',{entity_id:'${eid}'})">🏠 На базу</button>
  <button class="btn" onclick="callSvc('vacuum','locate',{entity_id:'${eid}'})">📍 Найти</button></div>`;
 if(a.fan_speed_list&&a.fan_speed_list.length)html+=selCtl('Мощность всасывания',a.fan_speed_list,a.fan_speed,
  `v=>callSvc('vacuum','set_fan_speed',{entity_id:'${eid}',fan_speed:v})`,'vs-'+eid);
 return html;}
function ctlLock(eid){
 const st=S(eid);
 return `<div class="row">
  <button class="btn ${st.state==='locked'?'ok':'accent'}" style="flex:1" onclick="callSvc('lock','lock',{entity_id:'${eid}'})">🔒 Запереть</button>
  <button class="btn ${st.state==='unlocked'?'ok':'accent'}" style="flex:1" onclick="callSvc('lock','unlock',{entity_id:'${eid}'})">🔓 Отопереть</button>
  <button class="btn" onclick="if(confirmDo('Открыть замок?'))callSvc('lock','open',{entity_id:'${eid}'})">🚪 Открыть</button></div>`;}
function ctlCamera(eid){
 return `<div class="cam-box">
  <img id="camImg" style="width:100%;border-radius:12px;background:#000;min-height:120px"
   src=HA_API+"/ha/camera_proxy/${eid}?t=${Date.now()}" onerror="this.alt='Снимок недоступен'">
  <div class="row" style="margin-top:10px">
   <button class="btn" onclick="document.getElementById('camImg').src=HA_API+'/ha/camera_proxy/${eid}?t='+Date.now()">🔄 Обновить снимок</button>
   <button class="btn accent" onclick="openStream('${eid}')">▶ Живой поток</button></div></div>`;}
function openStream(eid){
 openDialog(`<div class="dlg-head"><div class="dlg-ic">📷</div>
  <div><div class="dlg-title">${esc(FN(eid))}</div><div class="dlg-sub mono">MJPEG-поток</div></div>
  <div class="spacer"></div><button class="icon-btn" onclick="closeDialog()">✕</button></div>
  <div class="dlg-body cam-big"><img src=HA_API+"/cam/stream/${eid}" onerror="this.alt='Поток недоступен'"></div>`,true);}
function ctlAlarm(eid){
 const st=S(eid);
 return `<div class="hint">Текущий статус: <b>${esc(RU_STATE[st.state]||st.state)}</b></div>
 <div class="form"><label>Код (если требуется)</label><input id="alarmCode-${eid}" class="input" type="password" placeholder="PIN-код"></div>
 <div class="row" style="margin-top:12px">
  <button class="btn ok" onclick="alarmCmd('${eid}','alarm_disarm')">🔓 Снять</button>
  <button class="btn" onclick="alarmCmd('${eid}','alarm_arm_home')">🏠 Дома</button>
  <button class="btn" onclick="alarmCmd('${eid}','alarm_arm_away')">🚗 Вне дома</button>
  <button class="btn" onclick="alarmCmd('${eid}','alarm_arm_night')">🌙 Ночь</button>
  <button class="btn danger" onclick="alarmCmd('${eid}','alarm_trigger')">🚨 ТРЕВОГА</button></div>`;}
function alarmCmd(eid,svc){
 const c=$('#alarmCode-'+eid);const data={entity_id:eid};
 if(c&&c.value)data.code=c.value;
 callSvc('alarm_control_panel',svc,data);}
function ctlHumidifier(eid){
 const st=S(eid),a=st.attributes||{};
 let html=`<div class="row" style="margin-bottom:12px">
  <button class="btn ${st.state==='on'?'ok':'accent'}" style="flex:1" onclick="callSvc('humidifier','turn_on',{entity_id:'${eid}'})">Включить</button>
  <button class="btn ${st.state==='off'?'ok':'accent'}" style="flex:1" onclick="callSvc('humidifier','turn_off',{entity_id:'${eid}'})">Выключить</button></div>`;
 if(a.humidity!=null)html+=`<div class="ctl"><label>Целевая влажность · ${a.humidity}%</label>
  <input type="range" min="${a.min_humidity||10}" max="${a.max_humidity||90}" value="${a.humidity}"
   onchange="callSvc('humidifier','set_humidity',{entity_id:'${eid}',humidity:+this.value},true)"></div>`;
 if(a.current_humidity!=null)html+=`<div class="hint">Текущая влажность: ${a.current_humidity}%</div>`;
 if(a.available_modes&&a.available_modes.length)html+=selCtl('Режим',a.available_modes,a.mode,
  `v=>callSvc('humidifier','set_mode',{entity_id:'${eid}',mode:v})`,'hm-'+eid);
 return html;}
function ctlWaterHeater(eid){
 const st=S(eid),a=st.attributes||{};
 let html=`<div class="kv-big"><div><div class="v">${a.current_temperature!=null?a.current_temperature+'°':'—'}</div><div class="k">сейчас</div></div>
  <div><div class="v">${a.temperature!=null?a.temperature+'°':'—'}</div><div class="k">цель</div></div></div>`;
 if(a.temperature!=null)html+=`<div class="ctl"><label>Целевая температура</label>
  <input type="range" min="${a.min_temp||30}" max="${a.max_temp||80}" step="0.5" value="${a.temperature}"
   onchange="callSvc('water_heater','set_temperature',{entity_id:'${eid}',temperature:+this.value},true)"></div>`;
 if(a.operation_list&&a.operation_list.length)html+=selCtl('Режим работы',a.operation_list,a.operation_mode,
  `v=>callSvc('water_heater','set_operation_mode',{entity_id:'${eid}',operation_mode:v})`,'wh-'+eid);
 if(a.away_mode)html+=`<div class="ctl row"><label style="margin:0">Режим «нет дома»</label>
  <button class="btn sm ${a.away_mode==='on'?'ok':''}" onclick="callSvc('water_heater','set_away_mode',{entity_id:'${eid}',away_mode:'${a.away_mode==='on'?'off':'on'}'},true)">${a.away_mode}</button></div>`;
 return html;}
function ctlSiren(eid){
 const st=S(eid),a=st.attributes||{};
 let html=`<div class="row" style="margin-bottom:12px">
  <button class="btn danger" style="flex:1" onclick="callSvc('siren','turn_on',{entity_id:'${eid}'})">🚨 Включить</button>
  <button class="btn ok" style="flex:1" onclick="callSvc('siren','turn_off',{entity_id:'${eid}'})">Выключить</button></div>`;
 if(a.volume!=null)html+=`<div class="ctl"><label>Громкость · ${Math.round(a.volume*100)}%</label>
  <input type="range" min="0" max="100" value="${Math.round(a.volume*100)}"
   onchange="callSvc('siren','turn_on',{entity_id:'${eid}',volume_level:this.value/100},true)"></div>`;
 if(a.available_tones&&a.available_tones.length)html+=selCtl('Мелодия',a.available_tones,a.tone,
  `v=>callSvc('siren','turn_on',{entity_id:'${eid}',tone:v})`,'sr-'+eid);
 return html;}
function ctlNumber(eid,d){
 const st=S(eid),a=st.attributes||{};
 const min=a.min!=null?a.min:0,max=a.max!=null?a.max:100,step=a.step||1;
 const mode=a.mode||'slider';
 if(mode==='box')return `<div class="ctl row"><input id="numv-${eid}" class="input mono" style="max-width:160px" value="${st.state}" type="number" min="${min}" max="${max}" step="${step}">
  <button class="btn accent" onclick="callSvc('${d}','set_value',{entity_id:'${eid}',value:+document.getElementById('numv-${eid}').value})">Установить</button></div>`;
 return `<div class="ctl"><label>Значение · <span id="lv-num-${eid}">${st.state}${a.unit_of_measurement?' '+a.unit_of_measurement:''}</span></label>
  <input type="range" min="${min}" max="${max}" step="${step}" value="${parseFloat(st.state)||min}"
   oninput="document.getElementById('lv-num-${eid}').textContent=this.value"
   onchange="callSvc('${d}','set_value',{entity_id:'${eid}',value:+this.value},true)"></div>`;}
function ctlSelect(eid,d){
 const st=S(eid),a=st.attributes||{};
 const opts=a.options||[];
 return `<div class="ctl"><label>Значение</label>
  ${opts.length<=4?`<div class="mode-chips">${opts.map(o=>
   `<button class="chip ${o===st.state?'active':''}" onclick="callSvc('${d}','select_option',{entity_id:'${eid}',option:'${esc(o)}'})">${esc(o)}</button>`).join('')}</div>`
  :`<select class="input" onchange="callSvc('${d}','select_option',{entity_id:'${eid}',option:this.value})">
   ${opts.map(o=>`<option ${o===st.state?'selected':''}>${esc(o)}</option>`).join('')}</select>`}</div>`;}
function ctlText(eid){
 const st=S(eid),a=st.attributes||{};
 return `<div class="form"><label>Значение ${a.mode==='password'?'(секрет)':''}</label>
  <input id="txtv-${eid}" class="input mono" value="${esc(st.state)}" ${a.mode==='password'?'type="password"':''}
   ${a.max!=null?`maxlength="${a.max}"`:''}>
  <button class="btn accent" style="margin-top:10px" onclick="callSvc('input_text','set_value',{entity_id:'${eid}',value:document.getElementById('txtv-${eid}').value})">Сохранить</button></div>`;}
function ctlTimer(eid){
 const st=S(eid),a=st.attributes||{};
 return `<div class="kv-big">
  <div><div class="v">${esc(st.state)}</div><div class="k">статус</div></div>
  <div><div class="v mono" style="font-size:18px">${esc(a.remaining||'—')}</div><div class="k">осталось</div></div>
  <div><div class="v mono" style="font-size:18px">${esc(a.duration||'—')}</div><div class="k">длительность</div></div></div>
 <div class="row">
  <button class="btn accent" onclick="callSvc('timer','start',{entity_id:'${eid}'})">▶ Старт</button>
  <button class="btn" onclick="callSvc('timer','pause',{entity_id:'${eid}'})">⏸ Пауза</button>
  <button class="btn" onclick="callSvc('timer','cancel',{entity_id:'${eid}'})">⏹ Сброс</button>
  <button class="btn" onclick="callSvc('timer','finish',{entity_id:'${eid}'})">✅ Завершить</button></div>`;}
function ctlWeather(eid){
 const st=S(eid),a=st.attributes||{};
 return `<div class="kv-big">
  <div><div class="v">${iconOf(st)} ${a.temperature!=null?a.temperature+'°':''}</div><div class="k">${esc(RU_STATE[st.state]||st.state)}</div></div>
  <div><div class="v">${a.humidity!=null?a.humidity+'%':'—'}</div><div class="k">влажность</div></div>
  <div><div class="v">${a.wind_speed!=null?a.wind_speed+(a.wind_speed_unit?' '+a.wind_speed_unit:''):'—'}</div><div class="k">ветер</div></div>
  <div><div class="v">${a.pressure!=null?a.pressure+'':'—'}</div><div class="k">давление</div></div></div>
 <div class="sec-title">Прогноз</div><div class="fc-row" id="fcst"><div class="hint">загрузка…</div></div>`;}
const WICON={sunny:'☀️','clear-night':'🌙',cloudy:'☁️',partlycloudy:'⛅',rainy:'🌧️',pouring:'🌧️',
 snowy:'🌨️','snowy-rainy':'🌨️',fog:'🌫️',haze:'🌫️',windy:'🌬️',hail:'🌨️',lightning:'⛈️',
 'lightning-rainy':'⛈️',exceptional:'⚠️'};
async function loadForecast(eid){
 let fc=A(eid,'forecast');
 if(!fc){try{
   const r=await callSvcWS('weather','get_forecasts',{type:'forecast'},{entity_id:eid});
   if(r&&r.response&&r.response[eid])fc=r.response[eid].forecast;}catch(_){}}
 const box=$('#fcst');if(!box)return;
 if(!fc||!fc.length){box.innerHTML='<div class="hint">Прогноз недоступен</div>';return;}
 box.innerHTML=fc.slice(0,7).map(f=>`<div class="fc">
  <div>${esc(fdate(f.datetime))}</div>
  <div style="font-size:22px">${WICON[f.condition]||'🌡️'}</div>
  <div class="fc-t">${f.temperature!=null?Math.round(f.temperature)+'°':''}</div>
  <div style="color:var(--mut)">${f.templow!=null?Math.round(f.templow)+'°':''} ${f.precipitation!=null?'💧'+f.precipitation:''}</div></div>`).join('');}
function ctlScript(eid){
 const st=S(eid),a=st.attributes||{};
 const fields=a.fields||{};const keys=Object.keys(fields);
 let html=`<button class="btn accent wide" onclick="runScriptDlg('${eid}')">▶ Запустить скрипт</button>`;
 html+=`<div class="hint">Последний запуск: ${esc(ftime(a.last_triggered))} · режим: ${esc(a.mode||'single')}</div>`;
 if(keys.length)html+=`<div class="sec-title">Параметры скрипта: ${keys.map(k=>`<span class="badge acc mono">${esc(k)}</span>`).join(' ')}</div>`;
 return html;}
function runScriptDlg(eid){
 const fields=(S(eid).attributes||{}).fields||{};
 const keys=Object.keys(fields);
 if(!keys.length)return callSvc('script','turn_on',{entity_id:eid}).then(()=>closeDialog()).catch(()=>{});
 const vars={};
 openDialog(`<div class="dlg-head"><div class="dlg-ic">📜</div><div><div class="dlg-title">${esc(FN(eid))}</div>
  <div class="dlg-sub">Запуск с параметрами</div></div><div class="spacer"></div>
  <button class="icon-btn" onclick="closeDialog()">✕</button></div>
  <div class="dlg-body"><div class="form">
  ${keys.map(k=>`<label>${esc(fields[k].name||k)} ${fields[k].description?'<span class="hint">— '+esc(fields[k].description)+'</span>':''}</label>
   <input class="input var-${k}" value="${esc(fields[k].default!=null?fields[k].default:'')}" placeholder="${esc(k)}">`).join('')}
  <button class="btn accent wide" style="margin-top:14px" onclick='runScriptVars("${eid}",${JSON.stringify(keys)})'>▶ Запустить</button>
  </div></div>`);}
function runScriptVars(eid,keys){
 const vars={};
 keys.forEach(k=>{const inp=document.querySelector('.var-'+CSS.escape(k));
  if(inp&&inp.value!=='')vars[k]=isNaN(+inp.value)?inp.value:+inp.value;});
 callSvc('script','turn_on',{entity_id:eid,variables:vars}).then(()=>closeDialog()).catch(()=>{});}
function ctlUpdate(eid){
 const st=S(eid),a=st.attributes||{};
 return `<div class="kv-big">
  <div><div class="v mono" style="font-size:16px">${esc(a.installed_version||'—')}</div><div class="k">установлено</div></div>
  <div><div class="v mono" style="font-size:16px">${esc(a.latest_version||'—')}</div><div class="k">доступно</div></div></div>
  ${a.release_url?`<div class="hint"><a href="${esc(a.release_url)}" target="_blank" rel="noopener">Что нового →</a></div>`:''}
  ${st.state==='on'?`<div class="row" style="margin-top:10px">
   <button class="btn accent" onclick="if(confirmDo('Установить обновление?'))callSvc('update','install',{entity_id:'${eid}'})">⬆️ Установить</button>
   <button class="btn" onclick="callSvc('update','skip',{entity_id:'${eid}'})">Пропустить</button></div>`
  :`<div class="hint" style="margin-top:8px">✅ Актуальная версия</div>`}`;}
async function loadSpark(eid){
 const cv=$('#sparkCv');if(!cv)return;
 try{
  const h=await api(HA_API+'/ha/history/period/'+isoAgo(24)+'?filter_entity_ids='+encodeURIComponent(eid)+'&minimal_response&no_attributes');
  const list=(h&&h[0])||[];
  const pts=list.map(p=>({t:new Date(p.last_changed).getTime(),v:parseFloat(p.state)})).filter(p=>isFinite(p.v));
  if(pts.length>1){drawChart(cv,[{name:FN(eid),pts}]);}
  else drawTimeline(cv,[{name:FN(eid),segs:toSegments(list)}]);
 }catch(e){const b=cv.parentElement;if(b)b.innerHTML+='<div class="hint">История недоступна (включён ли recorder?)</div>';}}

/* ═══════════════════════ КАРТА ═══════════════════════ */
function mapEntities(){
 return Object.keys(STATES).filter(e=>{
  const a=S(e).attributes||{};
  return a.latitude!=null&&a.longitude!=null&&
   (['person','device_tracker','zone'].includes(dom(e))||MAP_EXTRA.has(e));});}
function pageMap(){
 const extras=Object.keys(STATES).filter(e=>{const a=S(e).attributes||{};
  return a.latitude!=null&&!['person','device_tracker','zone'].includes(dom(e));});
 $('#view').innerHTML=`
 <div class="page-head">
  <button class="btn" onclick="drawMapNow()">🔄 Обновить</button>
  <span class="hint" id="mapHint"></span>
  <div class="spacer"></div>
  ${extras.length?`<select class="input" style="max-width:260px" onchange="toggleMapExtra(this.value);this.value=''">
   <option value="">+ добавить метку на карту…</option>
   ${extras.map(e=>`<option value="${e}" ${MAP_EXTRA.has(e)?'disabled':''}>${esc(FN(e))}</option>`).join('')}</select>`:''}
 </div>
 <div class="map-wrap"><canvas id="mapCv"></canvas><div id="mapTip" class="map-tip hidden"></div></div>
 <div class="hint">Метки — сущности с координатами (люди, трекеры, зоны). Фоновое изображение (план дома или карта) задаётся в «Настройках». Клик по метке — управление.</div>`;
 drawMapNow();
 const iv=setInterval(()=>{if(ROUTE==='map'&&$('#mapCv'))drawMapNow();},10000);
 CLEAN.push(()=>clearInterval(iv));}
function toggleMapExtra(e){if(!e)return;
 if(MAP_EXTRA.has(e))MAP_EXTRA.delete(e);else MAP_EXTRA.add(e);
 LS('hap_map_extra',[...MAP_EXTRA]);drawMapNow();}
let MAP_MARKS=[];
function drawMapNow(){
 const cv=$('#mapCv');if(!cv)return;
 const eids=mapEntities();
 $('#mapHint').textContent=eids.length+' объектов';
 const dpr=window.devicePixelRatio||1;
 const W=cv.clientWidth||800,H=cv.clientHeight||500;
 cv.width=W*dpr;cv.height=H*dpr;
 const g=cv.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);
 g.clearRect(0,0,W,H);
 /* фон */
 const prefs=LS('hap_prefs')||{};
 const bgUrl=prefs.mapbg||'';
 const pts=eids.map(e=>{const a=S(e).attributes||{};return{eid:e,lat:+a.latitude,lon:+a.longitude,r:+a.radius||0};});
 let lat0,lon0,lat1,lon1;
 if(pts.length){
  lat0=Math.min(...pts.map(p=>p.lat));lat1=Math.max(...pts.map(p=>p.lat));
  lon0=Math.min(...pts.map(p=>p.lon));lon1=Math.max(...pts.map(p=>p.lon));}
 else{lat0=(SYSCFG&&SYSCFG.latitude)||55.75;lat1=lat0+0.05;lon0=(SYSCFG&&SYSCFG.longitude)||37.6;lon1=lon0+0.1;}
 const padM=0.25;
 const dlat=(lat1-lat0)||0.01,dlon=(lon1-lon0)||0.01;
 lat0-=dlat*padM;lat1+=dlat*padM;lon0-=dlon*padM;lon1+=dlon*padM;
 const X=lon=>(lon-lon0)/(lon1-lon0)*W;
 const Y=lat=>H-(lat-lat0)/(lat1-lat0)*H;
 const mpp=(lon1-lon0)*111320*Math.cos(lat0*Math.PI/180)/W; /* метров на пиксель */
 const drawMarkers=()=>{
  MAP_MARKS=[];
  /* зоны — круги */
  pts.filter(p=>dom(p.eid)==='zone').forEach(p=>{
   const x=X(p.lon),y=Y(p.lat),rr=p.r?Math.max(p.r/mpp,8):30;
   g.beginPath();g.arc(x,y,rr,0,7);g.fillStyle='rgba(94,177,255,.12)';g.fill();
   g.strokeStyle='rgba(94,177,255,.55)';g.lineWidth=1.5;g.stroke();
   g.fillStyle='var(--mut)';g.fillStyle='#8b96a8';g.font='11px sans-serif';g.textAlign='center';
   g.fillText(FN(p.eid),x,y+rr+14);g.textAlign='left';});
  /* объекты */
  pts.filter(p=>dom(p.eid)!=='zone').forEach(p=>{
   const x=X(p.lon),y=Y(p.lat);
   const st=S(p.eid);const on=isOn(st);
   MAP_MARKS.push({x,y,eid:p.eid});
   g.beginPath();g.arc(x,y,15,0,7);
   g.fillStyle=on?'rgba(94,177,255,.95)':'rgba(120,130,150,.85)';g.fill();
   g.strokeStyle='rgba(255,255,255,.75)';g.lineWidth=2;g.stroke();
   g.font='15px sans-serif';g.textAlign='center';g.textBaseline='middle';
   g.fillText(dom(p.eid)==='person'?'👤':'📍',x,y+1);
   g.textBaseline='alphabetic';g.textAlign='center';
   g.fillStyle='#e8edf4';g.font='600 11.5px sans-serif';
   g.strokeStyle='rgba(0,0,0,.6)';g.lineWidth=3;
   g.strokeText(FN(p.eid),x,y+30);g.fillText(FN(p.eid),x,y+30);
   g.textAlign='left';});
 };
 if(bgUrl){
  const img=new Image();img.crossOrigin='anonymous';
  img.onload=()=>{
   const s=Math.max(W/img.width,H/img.height);
   const iw=img.width*s,ih=img.height*s;
   g.drawImage(img,(W-iw)/2,(H-ih)/2,iw,ih);drawMarkers();};
  img.onerror=()=>{drawGrid();drawMarkers();
   $('#mapHint').textContent='не удалось загрузить фоновое изображение';};
  img.src=bgUrl;
 }else{drawGrid();drawMarkers();}
 function drawGrid(){
  const grd=g.createLinearGradient(0,0,0,H);
  grd.addColorStop(0,'#16202e');grd.addColorStop(1,'#101823');
  g.fillStyle=grd;g.fillRect(0,0,W,H);
  g.strokeStyle='rgba(255,255,255,.05)';g.lineWidth=1;
  for(let x=0;x<W;x+=48){g.beginPath();g.moveTo(x,0);g.lineTo(x,H);g.stroke();}
  for(let y=0;y<H;y+=48){g.beginPath();g.moveTo(0,y);g.lineTo(W,y);g.stroke();}
 }
 cv.onclick=e=>{
  const r=cv.getBoundingClientRect();const mx=e.clientX-r.left,my=e.clientY-r.top;
  let best=null,bd=1e9;
  MAP_MARKS.forEach(m=>{const d=Math.hypot(m.x-mx,m.y-my);if(d<bd){bd=d;best=m;}});
  if(best&&bd<26)moreInfo(best.eid);};
 const tip=$('#mapTip');
 cv.onmousemove=e=>{
  const r=cv.getBoundingClientRect();const mx=e.clientX-r.left,my=e.clientY-r.top;
  let best=null,bd=1e9;
  MAP_MARKS.forEach(m=>{const d=Math.hypot(m.x-mx,m.y-my);if(d<bd){bd=d;best=m;}});
  if(best&&bd<26){const st=S(best.eid);
   tip.innerHTML=`<b>${esc(FN(best.eid))}</b><br>${esc(stateText(st))}`;
   tip.classList.remove('hidden');
   tip.style.left=(mx+16)+'px';tip.style.top=(my-10)+'px';
   cv.style.cursor='pointer';}
  else{tip.classList.add('hidden');cv.style.cursor='crosshair';}};
 cv.onmouseleave=()=>tip.classList.add('hidden');}

/* ═══════════════════════ ИСТОРИЯ + СТАТИСТИКА ═══════════════════════ */
let HIST_PICK=[];
function pageHistory(){
 $('#view').innerHTML=`
 <div class="tabs">
  <button class="seg-btn active" id="tabHist" onclick="histTab('h')">📈 История состояний</button>
  <button class="seg-btn" id="tabStats" onclick="histTab('s')">📊 Долгосрочная статистика</button>
 </div>
 <div id="histPane">
  <div class="page-head" style="position:relative">
   <input id="hPick" class="input search" placeholder="🔍 Добавьте сущности…" autocomplete="off">
   <div id="hSugg" class="sugg hidden"></div>
   <select id="hRange" class="input" style="max-width:170px">
    <option value="1">1 час</option><option value="6">6 часов</option>
    <option value="24" selected>24 часа</option><option value="72">3 суток</option><option value="168">7 суток</option></select>
   <button class="btn accent" onclick="loadHistory()">Показать</button>
  </div>
  <div class="chips" id="hChips"></div>
  <div class="chart-box"><canvas id="histCv"></canvas></div>
  <div class="chart-box" style="margin-top:14px"><canvas id="histTl"></canvas></div>
 </div>
 <div id="statsPane" class="hidden">
  <div class="page-head">
   <select id="statId" class="input" style="max-width:420px"><option>загрузка…</option></select>
   <select id="statRange" class="input" style="max-width:170px">
    <option value="24">24 часа</option><option value="72">3 суток</option>
    <option value="168" selected>7 суток</option><option value="720">30 суток</option></select>
   <button class="btn accent" onclick="loadStat()">Показать</button>
  </div>
  <div class="chart-box"><canvas id="statCv"></canvas></div>
  <div class="hint" id="statHint"></div>
 </div>`;
 renderHistChips();
 const pk=$('#hPick');
 pk.oninput=()=>{
  const q=pk.value.trim().toLowerCase();const sg=$('#hSugg');
  if(!q){sg.classList.add('hidden');return;}
  const list=Object.keys(STATES).filter(e=>e.includes(q)||FN(e).toLowerCase().includes(q))
   .sort((a,b)=>FN(a).localeCompare(FN(b),'ru')).slice(0,14);
  sg.innerHTML=list.length?list.map(e=>`<div onclick="histAdd('${e}')">${iconOf(S(e))} ${esc(FN(e))} <span class="mono" style="color:var(--mut)">${e}</span></div>`).join(''):'<div>ничего не найдено</div>';
  sg.classList.remove('hidden');};
 pk.onblur=()=>setTimeout(()=>$('#hSugg').classList.add('hidden'),200);
 loadStatIds();
 /* старт: все сенсоры с единицами измерения, максимум 6 */
 if(!HIST_PICK.length){
  HIST_PICK=Object.keys(STATES).filter(e=>dom(e)==='sensor'&&A(e,'unit_of_measurement')&&
   isFinite(parseFloat(S(e).state))).slice(0,4);}
 renderHistChips();loadHistory();}
function histTab(t){
 $('#histPane').classList.toggle('hidden',t!=='h');
 $('#statsPane').classList.toggle('hidden',t!=='s');
 $('#tabHist').classList.toggle('active',t==='h');
 $('#tabStats').classList.toggle('active',t==='s');}
function histAdd(e){if(!HIST_PICK.includes(e))HIST_PICK.push(e);
 $('#hPick').value='';$('#hSugg').classList.add('hidden');renderHistChips();loadHistory();}
function histDel(e){HIST_PICK=HIST_PICK.filter(x=>x!==e);renderHistChips();loadHistory();}
function renderHistChips(){const b=$('#hChips');if(!b)return;
 b.innerHTML=HIST_PICK.map((e,i)=>`<span class="chip on" style="cursor:default">
  <span style="color:${PALETTE[i%PALETTE.length]}">●</span> ${esc(FN(e))}
  <b style="cursor:pointer;margin-left:4px" onclick="histDel('${e}')">✕</b></span>`).join('');}
async function loadHistory(){
 const cv=$('#histCv'),tl=$('#histTl');if(!cv)return;
 if(!HIST_PICK.length){return;}
 const hours=+($('#hRange')?$('#hRange').value:24);
 try{
  const h=await api(HA_API+'/ha/history/period/'+isoAgo(hours)+'?filter_entity_ids='+
   encodeURIComponent(HIST_PICK.join(','))+'&minimal_response');
  const numSeries=[],tlRows=[];
  (h||[]).forEach((list)=>{
   const eid=(list[0]&&list[0].entity_id)||'';if(!eid)return;
   const pts=list.map(p=>({t:new Date(p.last_changed).getTime(),v:parseFloat(p.state)})).filter(p=>isFinite(p.v));
   const distinct=new Set(list.map(p=>p.state));
   if(pts.length>1&&distinct.size>2&&list.every(p=>isFinite(parseFloat(p.state))))
    numSeries.push({name:FN(eid),pts});
   else tlRows.push({name:FN(eid),segs:toSegments(list)});});
  if(numSeries.length)drawChart(cv,numSeries);
  else{cv.parentElement.classList.add('hidden');}
  if(numSeries.length)cv.parentElement.classList.remove('hidden');
  if(tlRows.length){tl.parentElement.classList.remove('hidden');drawTimeline(tl,tlRows);}
  else tl.parentElement.classList.add('hidden');
 }catch(e){toast('История: '+esc(e.message),'err');}}
async function loadStatIds(){
 try{
  const ids=await haproxy({type:'recorder/list_statistic_ids'});
  const sel=$('#statId');if(!sel)return;
  sel.innerHTML=(ids||[]).map(x=>`<option value="${esc(x.statistic_id)}">${esc(x.statistic_id)} (${esc(x.unit_of_measurement||x.state_class||'')})</option>`).join('')
   ||'<option value="">нет данных (recorder отключён?)</option>';
 }catch(e){const sel=$('#statId');if(sel)sel.innerHTML='<option value="">недоступно: '+esc(e.message)+'</option>';}}
async function loadStat(){
 const id=$('#statId')?$('#statId').value:'';if(!id)return;
 const hours=+($('#statRange')?$('#statRange').value:168);
 try{
  const r=await haproxy({type:'recorder/statistic_during_period',statistic_id:id,
   start_time:isoAgo(hours),types:['mean','sum','min','max','state']});
  const rows=(r&&r[id])||[];
  const pts=rows.map(x=>({t:x.start,v:x.mean!=null?x.mean:(x.sum!=null?x.sum:x.state)})).filter(p=>p.v!=null);
  const cv=$('#statCv');
  if(pts.length)drawChart(cv,[{name:id,pts}]);
  const hint=$('#statHint');
  if(hint)hint.textContent=rows.length?`${rows.length} записей · последнее: ${rows.length?new Date(rows[rows.length-1].start).toLocaleString('ru-RU'):''}`:'нет данных за период';
 }catch(e){toast('Статистика: '+esc(e.message),'err');}}

/* ═══════════════════════ ЖУРНАЛ (LOGBOOK) ═══════════════════════ */
function pageLogbook(){
 $('#view').innerHTML=`
 <div class="page-head">
  <select id="lbHours" class="input" style="max-width:150px" onchange="loadLogbook()">
   <option value="1">1 час</option><option value="6">6 часов</option>
   <option value="24" selected>24 часа</option><option value="72">3 суток</option></select>
  <input id="lbFilter" class="input search" placeholder="Фильтр: entity_id или имя…">
  <button class="btn" onclick="loadLogbook()">🔄 Обновить</button>
 </div>
 <div class="tbl-wrap"><table class="tbl"><thead><tr>
  <th style="width:150px">Время</th><th style="width:230px">Сущность</th><th>Событие</th><th style="width:110px">Статус</th>
 </tr></thead><tbody id="lbBody"><tr><td colspan="4" class="hint">загрузка…</td></tr></tbody></table></div>`;
 $('#lbFilter').oninput=debounce(()=>filterLogbook(),160);
 loadLogbook();
 const iv=setInterval(()=>{if(ROUTE==='logbook')loadLogbook(true);},45000);
 CLEAN.push(()=>clearInterval(iv));}
let LB_ROWS=[];
async function loadLogbook(silent){
 const hours=+($('#lbHours')?$('#lbHours').value:24);
 try{
  const rows=await api(HA_API+'/ha/logbook/'+isoAgo(hours)+'?end_time='+encodeURIComponent(new Date().toISOString()));
  LB_ROWS=Array.isArray(rows)?rows:[];
  filterLogbook();
  if(!silent&&LB_ROWS.length===0)$('#lbBody').innerHTML='<tr><td colspan="4" class="hint">записей нет (logbook/recorder отключены?)</td></tr>';
 }catch(e){$('#lbBody').innerHTML='<tr><td colspan="4" class="hint">Ошибка: '+esc(e.message)+'</td></tr>';}}
function filterLogbook(){
 const q=($('#lbFilter')?$('#lbFilter').value:'').trim().toLowerCase();
 let rows=LB_ROWS;
 if(q)rows=rows.filter(r=>(r.entity_id||'').includes(q)||(r.name||'').toLowerCase().includes(q)||(r.domain||'').includes(q));
 rows=rows.slice(0,600);
 $('#lbBody').innerHTML=rows.map(r=>{
  const eid=r.entity_id||'';const st=eid?S(eid):null;
  return `<tr ${eid?'style="cursor:pointer" onclick="moreInfo(\''+eid+'\')"':''}>
  <td class="mono" style="color:var(--mut)">${esc(ftime(r.when))}</td>
  <td>${st?iconOf(st):'📄'} ${esc(r.name||eid)}</td>
  <td>${esc(r.message||r.state||'')}</td>
  <td>${r.state?`<span class="badge ${isOnStr(r.state)?'ok':'mut'}">${esc(RU_STATE[r.state]||r.state)}</span>`:''}</td></tr>`;}).join('')
 ||'<tr><td colspan="4" class="hint">ничего не найдено</td></tr>';}
function isOnStr(s){return['on','home','open','locked','playing','cleaning','active'].includes(s);}

/* ═══════════════════════ АВТОМАТИЗАЦИИ ═══════════════════════ */
function pageAutomations(){
 const list=Object.keys(STATES).filter(e=>dom(e)==='automation').sort((a,b)=>FN(a).localeCompare(FN(b),'ru'));
 $('#view').innerHTML=`
 <div class="page-head">
  <input id="autoSearch" class="input search" placeholder="🔍 Поиск автоматизаций…">
  <div class="spacer"></div>
  <button class="btn accent" onclick="editAutomation(null)">＋ Новая автоматизация</button>
 </div>
 <div id="autoList">${list.length?list.map(autoRow).join(''):'<div class="empty">Автоматизаций пока нет — создайте первую.</div>'}</div>`;
 $('#autoSearch').oninput=debounce(()=>{
  const q=$('#autoSearch').value.toLowerCase();
  $$('#autoList .panel').forEach(p=>{p.style.display=p.dataset.s.includes(q)?'':'none';});},140);}
function autoRow(eid){
 const st=S(eid),a=st.attributes||{};const id=objectIdOf(eid);
 return `<div class="panel" data-s="${esc((eid+' '+FN(eid)).toLowerCase())}" style="display:flex;gap:12px;align-items:center;margin-bottom:10px">
  <div class="card-ic">🤖</div>
  <div style="flex:1;min-width:0">
   <div style="font-weight:700">${esc(FN(eid))}</div>
   <div class="hint">id: <span class="mono">${esc(id)}</span> · последний запуск: ${esc(ftime(a.last_triggered))||'—'} ·
   <span class="badge ${st.state==='on'?'ok':'mut'}">${st.state==='on'?'включена':'выключена'}</span></div>
  </div>
  <div class="acts row" style="flex-shrink:0">
   <button class="btn sm ${st.state==='on'?'ok':''}" onclick="callSvc('automation','toggle',{entity_id:'${eid}'})" title="Вкл/выкл">⏻</button>
   <button class="btn sm" onclick="callSvc('automation','trigger',{entity_id:'${eid}'})" title="Запустить сейчас">▶</button>
   <button class="btn sm" onclick="moreInfo('${eid}')" title="Подробнее">ℹ</button>
   <button class="btn sm" onclick="editAutomation('${esc(id)}','${eid}')" title="Редактировать">✎</button>
   <button class="btn sm danger" onclick="delConfig('automation','${esc(id)}')" title="Удалить">🗑</button>
  </div></div>`;}
async function delConfig(kind,id){
 if(!confirmDo(`Удалить ${kind} «${id}»? Действие необратимо.`))return;
 try{await api(HA_API+'/ha/config/'+kind+'/config/'+encodeURIComponent(id),{method:'DELETE'});
  toast('Удалено','ok');refreshRoute();}
 catch(e){toast('Ошибка удаления: '+esc(e.message),'err',6000);}}

/* ── конструктор строк (триггеры/условия/действия) ── */
const TRIG_TYPES={
 state:'Состояние (state)',numeric_state:'Число (numeric_state)',time:'Время (time)',
 time_pattern:'Расписание (time_pattern)',sun:'Солнце (sun)',zone:'Зона (zone)',
 template:'Шаблон (template)',webhook:'Webhook',event:'Событие (event)',mqtt:'MQTT'};
const TRIG_EX={
 state:{entity_id:'binary_sensor.door',to:'on'},
 numeric_state:{entity_id:'sensor.temperature',above:25},
 time:{at:'07:30:00'},
 time_pattern:{minutes:'/5'},
 sun:{event:'sunset',offset:'-00:30:00'},
 zone:{entity_id:'person.ivan',zone:'zone.home',event:'enter'},
 template:{value_template:'{{ states("sensor.x") | float > 10 }}'},
 webhook:{webhook_id:'my_webhook'},
 event:{event_type:'my_event'},
 mqtt:{topic:'home/topic',payload:'on'}};
const COND_TYPES={state:'Состояние (state)',numeric_state:'Число (numeric_state)',time:'Время (time)',
 sun:'Солнце (sun)',zone:'Зона (zone)',template:'Шаблон (template)',and:'И (and)',or:'ИЛИ (or)'};
const ACT_TYPES={call_service:'Вызвать службу',delay:'Пауза (delay)',scene:'Включить сцену',
 wait_template:'Ждать шаблон',event:'Событие (event.fire)',stop:'Остановить (stop)'};
const ACT_EX={delay:{seconds:5},scene:{entity_id:'scene.movie'},wait_template:{wait_template:'{{ is_state("binary_sensor.x","on") }}'},
 event:{event:'my_event',event_data:{}},stop:{}};
function rowBuilder(kind,types,examples){
 return `<div class="rb-list" data-kind="${kind}"></div>
 <button class="btn sm" onclick="rbAdd('${kind}')">＋ добавить</button>`;}
function rbAdd(kind,type,initVal){
 const box=document.querySelector(`.rb-list[data-kind="${kind}"]`);if(!box)return;
 const types=kind==='trigger'?TRIG_TYPES:kind==='condition'?COND_TYPES:ACT_TYPES;
 const ex=kind==='trigger'?TRIG_EX:kind==='action'?ACT_EX:{};
 const idx=box.children.length;
 const div=document.createElement('div');div.className='row-builder';
 const t0=type||Object.keys(types)[0];
 div.innerHTML=`
 <div class="rb-head"><b>${kind==='trigger'?'Триггер':kind==='condition'?'Условие':'Действие'} #${idx+1}</b>
  <select class="input rb-type" style="max-width:250px">
   ${Object.keys(types).map(t=>`<option value="${t}" ${t===t0?'selected':''}>${esc(types[t])}</option>`).join('')}</select>
  <div class="spacer"></div><button class="btn sm danger" onclick="this.closest('.row-builder').remove()">✕</button></div>
 <div class="rb-fields"></div>`;
 box.appendChild(div);
 const sel=div.querySelector('.rb-type');
 const render=()=>{
  const t=sel.value;const f=div.querySelector('.rb-fields');
  if(kind==='action'&&t==='call_service'){
   f.innerHTML=`
    <label>Служба</label><input class="input mono rb-svc" list="svcListAll" placeholder="light.turn_on">
    <label>Цель (entity_id, можно через запятую)</label><input class="input mono rb-target" placeholder="light.kitchen">
    <label>Данные службы (JSON)</label><textarea class="input mono rb-data" rows="3" placeholder='{"brightness_pct": 80}'>{}</textarea>`;
  }else{
   const e=(ex[t]||{});
   f.innerHTML=`<label>Параметры (JSON)</label>
    <textarea class="input mono rb-json" rows="3">${esc(JSON.stringify(initVal&&initVal.platform===t?stripPlat(initVal):e,null,1))}</textarea>`;}
 };
 sel.onchange=()=>render();
 render();
 if(initVal){sel.value=initVal.platform||initVal.type||t0;
  if(kind==='action'&&initVal.service){sel.value='call_service';render();
   div.querySelector('.rb-svc').value=initVal.service||'';
   const _tgt=(initVal.target&&initVal.target.entity_id)||(initVal.data&&initVal.data.entity_id)||'';
   div.querySelector('.rb-target').value=Array.isArray(_tgt)?_tgt.join(', '):String(_tgt);
   const d=Object.assign({},initVal.data||{});delete d.entity_id;
   div.querySelector('.rb-data').value=JSON.stringify(d,null,1);}
  else if(kind==='action'&&initVal.scene){sel.value='scene';render();
   div.querySelector('.rb-json').value=JSON.stringify({entity_id:initVal.scene},null,1);}
  else{render();div.querySelector('.rb-json').value=JSON.stringify(stripPlat(initVal),null,1);}}
 return div;}
function stripPlat(o){const c=Object.assign({},o);delete c.platform;delete c.id;return c;}
function rbCollect(kind){
 const box=document.querySelector(`.rb-list[data-kind="${kind}"]`);if(!box)return[];
 const out=[];let i=0;
 for(const div of box.children){
  i++;const t=div.querySelector('.rb-type').value;
  if(kind==='action'&&t==='call_service'){
   const svc=div.querySelector('.rb-svc').value.trim();
   if(!svc){throw new Error(`Действие #${i}: укажите службу`);}
   const target=div.querySelector('.rb-target').value.trim();
   let data={};const dtxt=div.querySelector('.rb-data').value.trim();
   if(dtxt){try{data=JSON.parse(dtxt);}catch(e){throw new Error(`Действие #${i}: некорректный JSON данных`);}}
   const act={service:svc};
   if(target){const parts=target.split(',').map(x=>x.trim()).filter(Boolean);
    act.target={entity_id:parts.length===1?parts[0]:parts};}
   if(Object.keys(data).length)act.data=data;
   out.push(act);
  }else{
   let params={};const txt=div.querySelector('.rb-json')?div.querySelector('.rb-json').value.trim():'';
   if(txt&&txt!=='{}'){try{params=JSON.parse(txt);}catch(e){throw new Error(`${kind} #${i}: некорректный JSON`);}}
   const platformKey=(kind==='action')?'':(t==='and'||t==='or')?'condition':'platform';
   if(platformKey)params[platformKey]=t;else params=t==='and'||t==='or'?{condition:t,...params}:{...params};
   if(kind==='action'){/* действия: call_service уже обработан, остальные — как есть */}
   out.push(params);}}
 return out;}

/* ── редактор автоматизации ── */
async function editAutomation(id,eid){
 let cfg={alias:'',description:'',triggers:[],conditions:[],actions:[],mode:'single'};
 if(id){
  try{cfg=await api(HA_API+'/ha/config/automation/config/'+encodeURIComponent(id));}
  catch(e){toast('Не удалось прочитать конфиг: '+esc(e.message),'err',6000);return;}}
 cfg=cfg||{};
 const norm=(a,b)=>cfg[a]||cfg[b]||[];
 const triggers=asList(norm('triggers','trigger'));
 const conditions=asList(norm('conditions','condition'));
 const actions=asList(norm('actions','action'));
 const isNew=!id;
 openDialog(`
 <div class="dlg-head"><div class="dlg-ic">🤖</div>
  <div><div class="dlg-title">${isNew?'Новая автоматизация':'Автоматизация: '+esc(cfg.alias||id)}</div>
  <div class="dlg-sub">${isNew?'будет создана через REST config API':'id: '+esc(id)}</div></div>
  <div class="spacer"></div><button class="icon-btn" onclick="closeDialog()">✕</button></div>
 <div class="dlg-body">
  <div class="tabs">
   <button class="seg-btn active" id="atb1" onclick="autoTab(1)">Конструктор</button>
   <button class="seg-btn" id="atb2" onclick="autoTab(2)">JSON</button>
  </div>
  <div id="autoForm">
   <div class="form">
    <label>Название *</label><input id="aAlias" class="input" value="${esc(cfg.alias||'')}">
    <label>Описание</label><input id="aDesc" class="input" value="${esc(cfg.description||'')}">
    <label>Режим</label><select id="aMode" class="input" style="max-width:220px">
     ${['single','restart','queued','parallel'].map(m=>`<option ${cfg.mode===m?'selected':''}>${m}</option>`).join('')}</select>
   </div>
   <div class="sec-title">ТРИГГЕРЫ (когда запускать)</div>
   ${rowBuilder('trigger')}
   <div class="sec-title">УСЛОВИЯ (необязательно)</div>
   ${rowBuilder('condition')}
   <div class="sec-title">ДЕЙСТВИЯ (что сделать)</div>
   ${rowBuilder('action')}
  </div>
  <div id="autoJson" class="hidden">
   <div class="hint">Полный конфиг в JSON. Ключи trigger/condition/action — классические; triggers/conditions/actions — новые (HA 2024.8+). Сервер HA примет оба варианта.</div>
   <textarea id="aJson" class="input mono" rows="20" spellcheck="false"></textarea>
  </div>
 </div>
 <div class="dlg-foot">
  ${eid?`<button class="btn" onclick="callSvc('automation','trigger',{entity_id:'${eid}'})">▶ Запустить</button>`:''}
  <div class="spacer"></div>
  <button class="btn" onclick="closeDialog()">Отмена</button>
  <button class="btn accent" onclick="saveAutomation('${esc(id||'')}')">💾 Сохранить</button>
 </div>`,true);
 triggers.forEach(t=>rbAdd('trigger',t.platform||t.trigger,t));
 conditions.forEach(c=>rbAdd('condition',c.condition||c.platform||c.type,c));
 actions.forEach(a=>rbAdd('action',a.service?'call_service':(a.scene?'scene':(a.delay?'delay':(a.wait_template?'wait_template':(a.event?'event':(a.stop!=null?'stop':'delay'))))),a));
 syncAutoJson();}
function asList(x){return Array.isArray(x)?x:(x?[x]:[]);}
function autoTab(n){
 $('#atb1').classList.toggle('active',n===1);$('#atb2').classList.toggle('active',n===2);
 $('#autoForm').classList.toggle('hidden',n!==1);$('#autoJson').classList.toggle('hidden',n!==2);
 if(n===2)syncAutoJson();else syncAutoForm();}
function collectAutoForm(){
 return {alias:$('#aAlias').value.trim(),description:$('#aDesc').value.trim(),
  mode:$('#aMode').value,trigger:rbCollect('trigger'),condition:rbCollect('condition'),
  action:rbCollect('action')};}
function syncAutoJson(){
 try{const f=collectAutoForm();
  const j=$('#aJson');if(j&&!j.dataset.dirty)j.value=JSON.stringify(f,null,2);}catch(_){}}
function syncAutoForm(){
 try{const j=$('#aJson');if(!j)return;const c=JSON.parse(j.value);
  if(c.alias!=null)$('#aAlias').value=c.alias;
  if(c.description!=null)$('#aDesc').value=c.description;
  if(c.mode)$('#aMode').value=c.mode;
  ['trigger','condition','action'].forEach(k=>{
   const box=document.querySelector(`.rb-list[data-kind="${k==='trigger'?'trigger':k==='condition'?'condition':'action'}"]`);
   if(box)box.innerHTML='';
   asList(c[k+'s']||c[k]).forEach(item=>rbAdd(k==='trigger'?'trigger':k==='condition'?'condition':'action',
    item.platform||item.condition||(item.service?'call_service':undefined),item));});
 }catch(e){/* JSON невалиден — форму не трогаем */}}
async function saveAutomation(id){
 let cfg;
 if(!$('#autoJson').classList.contains('hidden')){
  try{cfg=JSON.parse($('#aJson').value);}catch(e){toast('JSON некорректен: '+esc(e.message),'err');return;}}
 else{try{cfg=collectAutoForm();}catch(e){toast(esc(e.message),'err');return;}}
 if(!cfg.alias){toast('Укажите название','err');return;}
 if(!id){id=slug(cfg.alias)+'_'+Math.random().toString(36).slice(2,6);
  id=await prompt('ID новой автоматизации (латиница/цифры/_):',id)||id;}
 try{
  await api(HA_API+'/ha/config/automation/config/'+encodeURIComponent(id),{method:'POST',body:cfg});
  toast('💾 Автоматизация сохранена','ok');closeDialog();
  callSvc('automation','reload',{},true).catch(()=>{});
  setTimeout(()=>{loadRegistries().then(()=>refreshRoute());},800);
 }catch(e){toast('Ошибка сохранения: '+esc(e.message)+'\n(на HAOS может требоваться доступ к config/automation — см. документацию)','err',9000);}}

/* ═══════════════════════ СЦЕНЫ ═══════════════════════ */
function pageScenes(){
 const list=Object.keys(STATES).filter(e=>dom(e)==='scene').sort((a,b)=>FN(a).localeCompare(FN(b),'ru'));
 $('#view').innerHTML=`
 <div class="page-head"><div class="spacer"></div>
  <button class="btn" onclick="sceneFromCurrent()">📸 Создать из текущих состояний</button>
  <button class="btn accent" onclick="editScene(null)">＋ Новая сцена</button></div>
 <div class="cards" style="grid-template-columns:repeat(auto-fill,minmax(260px,1fr))">
 ${list.map(eid=>{const id=objectIdOf(eid);return `<div class="card" onclick="moreInfo('${eid}')">
  <div class="card-ic">🎬</div>
  <div class="card-b"><div class="card-n">${esc(FN(eid))}</div><div class="card-s mono">${esc(id)}</div></div>
  <button class="card-tgl" title="Применить" onclick="event.stopPropagation();callSvc('scene','turn_on',{entity_id:'${eid}'})">▶</button>
  <button class="card-tgl" title="Редактировать" onclick="event.stopPropagation();editScene('${esc(id)}')">✎</button>
  <button class="card-tgl" title="Удалить" onclick="event.stopPropagation();delConfig('scene','${esc(id)}')">🗑</button>
 </div>`;}).join('')||'<div class="empty">Сцен пока нет.</div>'}</div>`;}
async function editScene(id){
 let cfg={name:'',icon:'mdi:palette',entities:{}};
 if(id){try{cfg=await api(HA_API+'/ha/config/scene/config/'+encodeURIComponent(id));}
  catch(e){toast('Не удалось прочитать: '+esc(e.message),'err');return;}}
 cfg=cfg||{entities:{}};
 openDialog(`
 <div class="dlg-head"><div class="dlg-ic">🎬</div>
  <div><div class="dlg-title">${id?'Сцена: '+esc(cfg.name||id):'Новая сцена'}</div>
  <div class="dlg-sub">${id?'id: '+esc(id):'зафиксируйте состояния сущностей'}</div></div>
  <div class="spacer"></div><button class="icon-btn" onclick="closeDialog()">✕</button></div>
 <div class="dlg-body"><div class="form">
  <label>Название *</label><input id="scName" class="input" value="${esc(cfg.name||'')}">
  <label>Иконка</label><input id="scIcon" class="input mono" value="${esc(cfg.icon||'mdi:palette')}">
  <label>Состояния сущностей (JSON: {"entity_id": {"state": "on", …}})</label>
  <textarea id="scEnts" class="input mono" rows="12" spellcheck="false">${esc(JSON.stringify(cfg.entities||{},null,2))}</textarea>
  <div class="row" style="margin-top:10px">
   <input id="scPick" class="input" style="flex:1" list="entsAll" placeholder="entity_id…">
   <button class="btn sm" onclick="sceneAddEntity()">＋ добавить текущее состояние</button></div>
 </div></div>
 <div class="dlg-foot"><button class="btn" onclick="closeDialog()">Отмена</button>
  <button class="btn accent" onclick="saveScene('${esc(id||'')}')">💾 Сохранить</button></div>`);
 ensureDatalists();}
function sceneAddEntity(){
 const eid=$('#scPick').value.trim();if(!eid||!S(eid)){toast('Сущность не найдена','err');return;}
 let ents;try{ents=JSON.parse($('#scEnts').value||'{}');}catch(_){ents={};}
 const st=S(eid),a=st.attributes||{},d=dom(eid);
 const cap={state:st.state};
 if(d==='light'){if(a.brightness!=null)cap.brightness=a.brightness;
  if(a.color_temp!=null)cap.color_temp=a.color_temp;else if(a.hs_color)cap.hs_color=a.hs_color;}
 ents[eid]=cap;
 $('#scEnts').value=JSON.stringify(ents,null,2);$('#scPick').value='';}
async function saveScene(id){
 const name=$('#scName').value.trim();if(!name){toast('Укажите название','err');return;}
 let ents;try{ents=JSON.parse($('#scEnts').value||'{}');}catch(e){toast('JSON состояний некорректен','err');return;}
 if(!id)id=await prompt('ID сцены:',slug(name))||slug(name);
 try{
  await api(HA_API+'/ha/config/scene/config/'+encodeURIComponent(id),
   {method:'POST',body:{name,icon:$('#scIcon').value.trim()||'mdi:palette',entities:ents}});
  toast('💾 Сцена сохранена','ok');closeDialog();
  callSvc('scene','reload',{},true).catch(()=>{});
  setTimeout(()=>{loadRegistries().then(()=>refreshRoute());},800);
 }catch(e){toast('Ошибка сохранения: '+esc(e.message),'err',8000);}}
function sceneFromCurrent(){
 const eids=Object.keys(STATES).filter(e=>['light','switch','cover','fan','input_boolean'].includes(dom(e))&&isOn(S(e)));
 openDialog(`
 <div class="dlg-head"><div class="dlg-ic">📸</div><div><div class="dlg-title">Сцена из текущих состояний</div>
 <div class="dlg-sub">отмеченные сущности (${eids.length} включённых) попадут в сцену</div></div>
 <div class="spacer"></div><button class="icon-btn" onclick="closeDialog()">✕</button></div>
 <div class="dlg-body"><div id="sfcList" style="max-height:44vh;overflow-y:auto">
  ${Object.keys(STATES).filter(e=>['light','switch','cover','fan','input_boolean','media_player'].includes(dom(e)))
   .sort().map(e=>`<label style="display:flex;gap:8px;align-items:center;margin:4px 0;font-size:13px;color:var(--txt)">
   <input type="checkbox" class="sfc-cb" value="${e}" ${isOn(S(e))?'checked':''} style="width:auto">
   ${iconOf(S(e))} ${esc(FN(e))} <span class="mono hint">${esc(stateText(S(e)))}</span></label>`).join('')}</div>
 </div>
 <div class="dlg-foot"><button class="btn" onclick="closeDialog()">Отмена</button>
  <button class="btn accent" onclick="sfcBuild()">Далее →</button></div>`);}
function sfcBuild(){
 const ents={};
 $$('.sfc-cb:checked').forEach(cb=>{const eid=cb.value,st=S(eid);if(!st)return;
  const cap={state:st.state};const a=st.attributes||{};
  if(dom(eid)==='light'&&a.brightness!=null)cap.brightness=a.brightness;
  ents[eid]=cap;});
 if(!Object.keys(ents).length){toast('Отметьте хотя бы одну сущность','err');return;}
 const name=prompt('Название сцены:','Вечерний свет');if(!name)return;
 const id=slug(name);
 api(HA_API+'/ha/config/scene/config/'+encodeURIComponent(id),
  {method:'POST',body:{name,icon:'mdi:palette',entities:ents}})
  .then(()=>{toast('💾 Сцена «'+esc(name)+'» создана','ok');closeDialog();
   callSvc('scene','reload',{},true).catch(()=>{});setTimeout(refreshRoute,800);})
  .catch(e=>toast('Ошибка: '+esc(e.message),'err',8000));}

/* ═══════════════════════ СКРИПТЫ ═══════════════════════ */
function pageScripts(){
 const list=Object.keys(STATES).filter(e=>dom(e)==='script').sort((a,b)=>FN(a).localeCompare(FN(b),'ru'));
 $('#view').innerHTML=`
 <div class="page-head"><div class="spacer"></div>
  <button class="btn accent" onclick="editScript(null)">＋ Новый скрипт</button></div>
 <div class="cards" style="grid-template-columns:repeat(auto-fill,minmax(260px,1fr))">
 ${list.map(eid=>{const id=objectIdOf(eid);const st=S(eid);return `
  <div class="card ${isOn(st)?'on':''}" onclick="moreInfo('${eid}')">
  <div class="card-ic">📜</div>
  <div class="card-b"><div class="card-n">${esc(FN(eid))}</div>
  <div class="card-s mono">${esc(id)} · ${esc(ftime(st.attributes.last_triggered)||'—')}</div></div>
  <button class="card-tgl" title="Запустить" onclick="event.stopPropagation();runScriptDlg('${eid}')">▶</button>
  <button class="card-tgl" title="Редактировать" onclick="event.stopPropagation();editScript('${esc(id)}')">✎</button>
  <button class="card-tgl" title="Удалить" onclick="event.stopPropagation();delConfig('script','${esc(id)}')">🗑</button>
 </div>`;}).join('')||'<div class="empty">Скриптов пока нет.</div>'}</div>`;}
async function editScript(id){
 let cfg={alias:'',sequence:[]};
 if(id){try{cfg=await api(HA_API+'/ha/config/script/config/'+encodeURIComponent(id));}
  catch(e){toast('Не удалось прочитать: '+esc(e.message),'err');return;}}
 cfg=cfg||{sequence:[]};
 const seq=asList(cfg.sequence);
 openDialog(`
 <div class="dlg-head"><div class="dlg-ic">📜</div>
  <div><div class="dlg-title">${id?'Скрипт: '+esc(cfg.alias||id):'Новый скрипт'}</div>
  <div class="dlg-sub">последовательность действий</div></div>
  <div class="spacer"></div><button class="icon-btn" onclick="closeDialog()">✕</button></div>
 <div class="dlg-body">
  <div class="form"><label>Название *</label><input id="scrAlias" class="input" value="${esc(cfg.alias||'')}">
  <label>Описание</label><input id="scrDesc" class="input" value="${esc(cfg.description||'')}"></div>
  <div class="sec-title">ПОСЛЕДОВАТЕЛЬНОСТЬ (sequence)</div>
  <div class="tabs"><button class="seg-btn active" id="stb1" onclick="scrTab(1)">Конструктор</button>
  <button class="seg-btn" id="stb2" onclick="scrTab(2)">JSON</button></div>
  <div id="scrForm">${rowBuilder('action')}</div>
  <div id="scrJson" class="hidden"><textarea id="scrSeq" class="input mono" rows="16" spellcheck="false">${esc(JSON.stringify(seq,null,2))}</textarea></div>
 </div>
 <div class="dlg-foot"><button class="btn" onclick="closeDialog()">Отмена</button>
  <button class="btn accent" onclick="saveScript('${esc(id||'')}')">💾 Сохранить</button></div>`,true);
 seq.forEach(a=>rbAdd('action',a.service?'call_service':(a.delay?'delay':(a.scene?'scene':(a.wait_template?'wait_template':'delay'))),a));}
function scrTab(n){
 $('#stb1').classList.toggle('active',n===1);$('#stb2').classList.toggle('active',n===2);
 $('#scrForm').classList.toggle('hidden',n!==1);$('#scrJson').classList.toggle('hidden',n!==2);
 if(n===2){try{$('#scrSeq').value=JSON.stringify(rbCollect('action'),null,2);}catch(e){toast(esc(e.message),'err');}}
 else{try{const s=JSON.parse($('#scrSeq').value);
   document.querySelector('.rb-list[data-kind="action"]').innerHTML='';
   asList(s).forEach(a=>rbAdd('action',a.service?'call_service':(a.delay?'delay':(a.scene?'scene':'delay')),a));}catch(_){}}}
async function saveScript(id){
 const alias=$('#scrAlias').value.trim();if(!alias){toast('Укажите название','err');return;}
 let seq;
 if(!$('#scrJson').classList.contains('hidden')){
  try{seq=JSON.parse($('#scrSeq').value);}catch(e){toast('JSON некорректен','err');return;}}
 else{try{seq=rbCollect('action');}catch(e){toast(esc(e.message),'err');return;}}
 if(!seq.length){toast('Добавьте хотя бы одно действие','err');return;}
 if(!id)id=await prompt('ID скрипта:',slug(alias))||slug(alias);
 try{
  await api(HA_API+'/ha/config/script/config/'+encodeURIComponent(id),{method:'POST',
   body:{alias,description:$('#scrDesc').value.trim(),mode:'single',sequence:seq}});
  toast('💾 Скрипт сохранён','ok');closeDialog();
  callSvc('script','reload',{},true).catch(()=>{});
  setTimeout(()=>{loadRegistries().then(()=>refreshRoute());},800);
 }catch(e){toast('Ошибка сохранения: '+esc(e.message),'err',8000);}}

/* ═══════════════════════ УСТРОЙСТВА ═══════════════════════ */
async function pageDevices(){
 if(!REG.devices.length)await loadRegistries();
 $('#view').innerHTML=`
 <div class="page-head"><input id="devSearch" class="input search" placeholder="🔍 Поиск устройств…">
  <span class="hint" id="devCount"></span></div>
 <div class="grid2" id="devList"></div>`;
 renderDevices();
 $('#devSearch').oninput=debounce(renderDevices,140);}
function renderDevices(){
 const q=($('#devSearch')?$('#devSearch').value:'').toLowerCase();
 const devs=(REG.devices||[]).filter(d=>!q||((d.name||'')+' '+(d.model||'')+' '+(d.manufacturer||'')+' '+(d.id||'')).toLowerCase().includes(q));
 $('#devCount').textContent=devs.length+' устройств';
 $('#devList').innerHTML=devs.map(d=>{
  const ents=(REG.entities||[]).filter(e=>e.device_id===d.id&&!e.disabled_by);
  const areaSel=areaSelectHtml(d.area_id,`setDevArea('${d.id}',this.value)`);
  return `<div class="panel">
  <div style="display:flex;gap:10px;align-items:flex-start">
   <div class="card-ic">${d.entry_type==='service'?'☁️':'🧩'}</div>
   <div style="flex:1;min-width:0">
    <div style="font-weight:700">${esc(d.name||d.name_by_user||'без имени')}</div>
    <div class="hint">${esc(d.manufacturer||'')}${d.manufacturer&&d.model?' · ':''}${esc(d.model||'')}</div>
    <div class="hint mono" style="font-size:11px">${esc(d.id)}</div>
   </div></div>
  <div class="form" style="margin-top:8px"><label>Область</label>${areaSel}</div>
  <details class="attrs" style="margin-top:8px"><summary>Сущности (${ents.length})</summary>
   ${ents.map(e=>`<div style="padding:4px 0;font-size:12.5px;cursor:pointer" onclick="moreInfo('${e.entity_id}')">
    ${S(e.entity_id)?iconOf(S(e.entity_id)):'📄'} ${esc(e.name||e.original_name||e.entity_id)}
    <span class="mono hint">${esc(e.entity_id)}</span></div>`).join('')||'<div class="hint">нет</div>'}
  </details></div>`;}).join('')||'<div class="empty">Устройства не найдены (нет связи с HA WS?)</div>';}
function areaSelectHtml(current,onchange){
 return `<select class="input" onchange="${onchange}">
  <option value="">— нет —</option>
  ${(REG.areas||[]).map(a=>`<option value="${esc(a.area_id)}" ${a.area_id===current?'selected':''}>${esc(a.name)}</option>`).join('')}</select>`;}
async function setDevArea(id,area){
 try{await haproxy({type:'config/device_registry/update',device_id:id,area_id:area||null});
  toast('Область обновлена','ok');await loadRegistries();refreshRoute();}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}

/* ═══════════════════════ СУЩНОСТИ (РЕЕСТР) ═══════════════════════ */
async function pageEntities(){
 if(!REG.entities.length)await loadRegistries();
 $('#view').innerHTML=`
 <div class="page-head"><input id="entSearch" class="input search" placeholder="🔍 entity_id, имя, платформа…">
  <span class="hint" id="entCount"></span></div>
 <div class="tbl-wrap"><table class="tbl"><thead><tr>
  <th>Сущность</th><th>Имя</th><th>Область</th><th>Платформа</th><th style="width:190px">Действия</th>
 </tr></thead><tbody id="entBody"></tbody></table></div>`;
 renderEnts();
 $('#entSearch').oninput=debounce(renderEnts,140);}
function renderEnts(){
 const q=($('#entSearch')?$('#entSearch').value:'').toLowerCase();
 let ents=(REG.entities||[]);
 if(q)ents=ents.filter(e=>((e.entity_id||'')+(e.name||'')+(e.original_name||'')+(e.platform||'')).toLowerCase().includes(q));
 ents=ents.sort((a,b)=>a.entity_id.localeCompare(b.entity_id)).slice(0,800);
 $('#entCount').textContent=ents.length+' записей'+((REG.entities||[]).length>800?' (первые 800)':'');
 $('#entBody').innerHTML=ents.map(e=>{
  const st=S(e.entity_id);
  const devArea=e.device_id?((REG.devices.find(d=>d.id===e.device_id)||{}).area_id||''):'';
  const area=areaSelectHtml(e.area_id||devArea,`setEntArea('${e.entity_id}',this.value)`);
  return `<tr>
  <td class="mono" style="cursor:pointer" onclick="${st?`moreInfo('${e.entity_id}')`:''}">${st?iconOf(st):'📄'} ${esc(e.entity_id)}
   ${e.disabled_by?'<span class="badge mut">откл</span>':''}${e.hidden_by?'<span class="badge mut">скрыт</span>':''}</td>
  <td>${esc(e.name||e.original_name||'—')}</td>
  <td style="min-width:150px">${area}</td>
  <td class="hint">${esc(e.platform||'')}</td>
  <td><div class="acts">
   <button class="btn sm" onclick="renameEnt('${e.entity_id}')">✎ имя</button>
   <button class="btn sm ${e.disabled_by?'ok':'danger'}" onclick="toggleEntDisabled('${e.entity_id}',${e.disabled_by?'false':'true'})">
    ${e.disabled_by?'включить':'отключить'}</button>
  </div></td></tr>`;}).join('')||'<tr><td colspan="5" class="hint">нет данных</td></tr>';}
async function setEntArea(eid,area){
 try{await haproxy({type:'config/entity_registry/update',entity_id:eid,area_id:area||null});
  toast('Область назначена','ok');await loadRegistries();renderEnts();}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}
async function renameEnt(eid){
 const cur=(REG.map[eid]||{}).name||(REG.map[eid]||{}).original_name||'';
 const name=prompt('Новое имя для '+eid+':',cur);if(name==null)return;
 try{await haproxy({type:'config/entity_registry/update',entity_id:eid,name:name||null});
  toast('Имя обновлено','ok');await loadRegistries();renderEnts();refreshRoute();}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}
async function toggleEntDisabled(eid,disable){
 if(disable&&!confirmDo('Отключить сущность '+eid+'? Она пропадёт из HA до включения.'))return;
 try{await haproxy({type:'config/entity_registry/update',entity_id:eid,disabled_by:disable?'user':null});
  toast(disable?'Сущность отключена':'Сущность включена','ok');await loadRegistries();renderEnts();}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}

/* ═══════════════════════ ОБЛАСТИ ═══════════════════════ */
async function pageAreas(){
 if(!REG.areas.length)await loadRegistries();
 const cards=(REG.areas||[]).map(a=>{
  const cnt=(REG.entities||[]).filter(e=>e.area_id===a.area_id||
   (e.device_id&&(REG.devices.find(d=>d.id===e.device_id)||{}).area_id===a.area_id)).length;
  return `<div class="panel">
  <div style="display:flex;gap:10px;align-items:center">
   <div class="card-ic">${a.icon?iconFromMdi(a.icon):'🗂️'}</div>
   <div style="flex:1"><div style="font-weight:700">${esc(a.name)}</div>
   <div class="hint">${cnt} сущностей · <span class="mono">${esc(a.area_id)}</span></div></div>
   <button class="btn sm" onclick="renameArea('${esc(a.area_id)}','${esc(a.name)}')">✎</button>
   <button class="btn sm danger" onclick="delArea('${esc(a.area_id)}')">🗑</button></div></div>`;}).join('');
 $('#view').innerHTML=`
 <div class="page-head"><div class="spacer"></div>
  <button class="btn accent" onclick="createArea()">＋ Новая область</button></div>
 <div class="grid2">${cards||'<div class="empty">Областей нет — создайте первую (например «Кухня», «Спальня»).</div>'}</div>`;}
function iconFromMdi(m){return m&&m.includes(':')?'🏷️':'🗂️';}
async function createArea(){
 const name=prompt('Название области:');if(!name)return;
 try{await haproxy({type:'config/area_registry/create',name});
  toast('Область создана','ok');await loadRegistries();pageAreas();}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}
async function renameArea(id,cur){
 const name=prompt('Новое название:',cur);if(!name||name===cur)return;
 try{await haproxy({type:'config/area_registry/update',area_id:id,name});
  toast('Переименовано','ok');await loadRegistries();pageAreas();refreshRoute();}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}
async function delArea(id){
 if(!confirmDo('Удалить область '+id+'?'))return;
 try{await haproxy({type:'config/area_registry/delete',area_id:id});
  toast('Удалено','ok');await loadRegistries();pageAreas();}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}

/* ═══════════════════════ ИНТЕГРАЦИИ (CONFIG ENTRIES) ═══════════════════════ */
async function pageIntegrations(){
 $('#view').innerHTML='<div class="loading">Загрузка интеграций…</div>';
 let entries=[];
 try{entries=await haproxy({type:'config_entries/get'});}
 catch(e){$('#view').innerHTML='<div class="empty">Недоступно: '+esc(e.message)+'<br><span class="hint">нужно активное WebSocket-соединение с HA</span></div>';return;}
 const byDomain={};
 entries.forEach(en=>{(byDomain[en.domain]=byDomain[en.domain]||[]).push(en);});
 const domains=Object.keys(byDomain).sort();
 const stBadge=s=>s==='loaded'?'<span class="badge ok">loaded</span>':
  s==='setup_error'?'<span class="badge err">ошибка</span>':
  (s==='disabled'?'<span class="badge mut">disabled</span>':`<span class="badge warn">${esc(s)}</span>`);
 $('#view').innerHTML=`
 <div class="page-head"><span class="hint">${entries.length} записей конфигурации · ${domains.length} доменов</span>
  <div class="spacer"></div>
  <button class="btn" onclick="if(confirmDo('Перезагрузить ВСЕ интеграции YAML?'))callSvc('homeassistant','reload_all',{})">♻️ reload_all</button></div>
 <div class="grid2">${domains.map(dm=>`
  <div class="panel"><h3>${iconOfDomain(dm)} ${esc(dm)} <span class="badge acc">${byDomain[dm].length}</span></h3>
  ${byDomain[dm].map(en=>{
   const cnt=(REG.entities||[]).filter(e=>(e.config_entries||[]).includes(en.entry_id)).length;
   return `<div class="stat-row" style="align-items:center">
    <div style="min-width:0"><div style="font-weight:600">${esc(en.title)}</div>
    <div class="hint">${stBadge(en.state)} ${cnt?'· '+cnt+' сущ.':''} ${en.reason?'· <b>'+esc(en.reason)+'</b>':''}</div></div>
    <div class="acts">
     <button class="btn sm" title="Перезагрузить" onclick="reloadEntry('${en.entry_id}')">♻️</button>
     <button class="btn sm danger" title="Удалить" onclick="removeEntry('${en.entry_id}','${esc(en.title)}')">🗑</button>
    </div></div>`;}).join('')}</div>`).join('')}</div>`;}
async function reloadEntry(id){
 try{await haproxy({type:'config_entries/reload',entry_id:id});toast('Интеграция перезагружается…','ok');
  setTimeout(()=>refreshRoute(),1500);}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}
async function removeEntry(id,title){
 if(!confirmDo('Удалить интеграцию «'+title+'»? Все её сущности исчезнут.'))return;
 try{const r=await haproxy({type:'config_entries/remove',entry_id:id});
  toast(r&&r.require_restart?'Удалено (требуется перезапуск HA)':'Удалено','ok');
  await loadRegistries();pageIntegrations();}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}

/* ═══════════════════════ КАМЕРЫ ═══════════════════════ */
function pageCameras(){
 const cams=Object.keys(STATES).filter(e=>dom(e)==='camera').sort();
 const interval=Math.max(2,+(LS('hap_prefs')||{}).snap_interval||5);
 $('#view').innerHTML=`
 <div class="page-head"><span class="hint">${cams.length} камер · снимки обновляются каждые ${interval}с (настраивается в «Настройках»)</span>
  <div class="spacer"></div>
  <button class="btn" onclick="refreshSnaps()">🔄 Обновить сейчас</button></div>
 <div class="cam-grid" id="camGrid">
 ${cams.map(e=>`<div class="cam-card" onclick="openStream('${e}')">
  <img class="cam-snap" data-e="${e}" src=HA_API+"/ha/camera_proxy/${e}?t=${Date.now()}" onerror="this.classList.add('broken');this.alt='нет сигнала'">
  <div class="cap">📷 ${esc(FN(e))} <span class="badge ${S(e).state==='streaming'?'ok':'mut'}">${esc(S(e).state)}</span></div>
 </div>`).join('')||'<div class="empty">Камер нет.</div>'}</div>`;
 const iv=setInterval(()=>{if(ROUTE==='cameras')refreshSnaps();},interval*1000);
 CLEAN.push(()=>clearInterval(iv));}
function refreshSnaps(){$$('.cam-snap').forEach(img=>{img.src=HA_API+'/ha/camera_proxy/'+img.dataset.e+'?t='+Date.now();});}

/* ═══════════════════════ МЕДИА ═══════════════════════ */
function pageMedia(){
 $('#view').innerHTML='<div class="page-head"><span class="hint">все медиаплееры</span></div><div class="grid2" id="mediaCards"></div>';
 renderMediaCards();}
function renderMediaCards(){
 const box=$('#mediaCards');if(!box)return;
 const players=Object.keys(STATES).filter(e=>dom(e)==='media_player').sort();
 box.innerHTML=players.map(eid=>{
  const st=S(eid),a=st.attributes||{};
  return `<div class="panel">
  <div style="display:flex;gap:11px;align-items:center">
   <div class="card-ic ${isOn(st)?'on':''}" style="background:${isOn(st)?'rgba(94,177,255,.18)':''}">${iconOf(st)}</div>
   <div style="flex:1;min-width:0">
    <div style="font-weight:700">${esc(FN(eid))}</div>
    <div class="hint" id="media-title-${eid}">${esc(a.media_title||RU_STATE[st.state]||st.state)}</div>
    <div class="hint">${esc([a.media_artist,a.media_album_name].filter(Boolean).join(' — '))}</div>
   </div></div>
  <div class="media-row" style="justify-content:center;margin-top:12px">
   <button class="btn sm" onclick="callSvc('media_player','media_previous_track',{entity_id:'${eid}'},true)">⏮</button>
   <button class="btn sm accent" onclick="callSvc('media_player','media_play_pause',{entity_id:'${eid}'},true)">${st.state==='playing'?'⏸':'▶'}</button>
   <button class="btn sm" onclick="callSvc('media_player','media_next_track',{entity_id:'${eid}'},true)">⏭</button>
   <button class="btn sm" onclick="callSvc('media_player','volume_mute',{entity_id:'${eid}',is_volume_muted:${!a.is_volume_muted}},true)">${a.is_volume_muted?'🔇':'🔊'}</button>
   ${a.volume_level!=null?`<input type="range" style="flex:1;max-width:150px" min="0" max="100" value="${Math.round(a.volume_level*100)}"
    onchange="callSvc('media_player','volume_set',{entity_id:'${eid}',volume_level:this.value/100},true)">`:''}
   <button class="btn sm" onclick="moreInfo('${eid}')">⋯</button>
  </div></div>`;}).join('')||'<div class="empty">Медиаплееров нет.</div>';}

/* ═══════════════════════ КЛИМАТ ═══════════════════════ */
function pageClimate(){
 $('#view').innerHTML=`<div class="page-head"><span class="hint">термостаты и кондиционеры</span></div>
 <div class="grid2" id="climateCards"></div>`;
 renderClimateCards();}
function renderClimateCards(){
 const box=$('#climateCards');if(!box)return;
 const list=Object.keys(STATES).filter(e=>['climate','water_heater','fan','humidifier'].includes(dom(e))).sort();
 box.innerHTML=list.map(eid=>{
  const st=S(eid),a=st.attributes||{},d=dom(eid);
  const cur=a.current_temperature!=null?a.current_temperature:(a.current_humidity!=null?a.current_humidity+'%':'—');
  const tgt=a.temperature!=null?a.temperature+'°':(a.humidity!=null?a.humidity+'%':'—');
  const modes=(a.hvac_modes||a.operation_list||[]);
  const modeSvc=d==='climate'?'set_hvac_mode':d==='water_heater'?'set_operation_mode':'';
  const modeKey=d==='climate'?'hvac_mode':d==='water_heater'?'operation_mode':'';
  const curMode=d==='climate'?a.hvac_mode:d==='water_heater'?a.operation_mode:'';
  return `<div class="panel">
  <div style="display:flex;gap:12px;align-items:center">
   <div class="card-ic ${isOn(st)?'on':''}">${iconOf(st)}</div>
   <div style="flex:1;min-width:0">
    <div style="font-weight:700">${esc(FN(eid))}</div>
    <div class="hint">${esc(RU_STATE[st.state]||st.state)}</div></div>
   <div style="text-align:right"><div class="big-val" style="font-size:26px">${esc(String(cur))}${a.current_temperature!=null?'<small>°</small>':''}</div>
    <div class="hint">цель: ${esc(String(tgt))}</div></div>
  </div>
  ${modes.length&&modeSvc?`<div class="mode-chips" style="margin-top:10px">
   ${modes.map(m=>`<button class="chip ${curMode===m?'active':''}"
    onclick="callSvc('${d}','${modeSvc}',{entity_id:'${eid}','${modeKey}':'${m}'},true).then(()=>setTimeout(renderClimateCards,600))">${RU_STATE[m]||m}</button>`).join('')}</div>`:''}
  ${d==='climate'&&a.temperature!=null&&a.hvac_mode!=='heat_cool'?`
   <div class="row" style="margin-top:6px;justify-content:center">
    <button class="btn sm" onclick="adjTemp('${eid}','t',-${a.target_temp_step||0.5});setTimeout(renderClimateCards,600)">− цель</button>
    <button class="btn sm" onclick="adjTemp('${eid}','t',${a.target_temp_step||0.5});setTimeout(renderClimateCards,600)">+ цель</button>
    <button class="btn sm" onclick="moreInfo('${eid}')">⋯</button></div>`:''}
  <div class="row" style="margin-top:8px;justify-content:flex-end">
   <button class="btn sm" onclick="moreInfo('${eid}')">Открыть панель →</button></div>
  </div>`;}).join('')||'<div class="empty">Климатических устройств нет.</div>';}

/* ═══════════════════════ ИНСТРУМЕНТЫ РАЗРАБОТЧИКА ═══════════════════════ */
let DEV_TAB='svc',EV_PID=null,EV_PAUSED=false,TPL_PID=null;
function pageDev(){
 $('#view').innerHTML=`
 <div class="tabs">
  <button class="seg-btn ${DEV_TAB==='svc'?'active':''}" onclick="devTab('svc')">⚡ Службы</button>
  <button class="seg-btn ${DEV_TAB==='states'?'active':''}" onclick="devTab('states')">📋 Состояния</button>
  <button class="seg-btn ${DEV_TAB==='events'?'active':''}" onclick="devTab('events')">📡 События</button>
  <button class="seg-btn ${DEV_TAB==='tpl'?'active':''}" onclick="devTab('tpl')">🧪 Шаблоны</button>
 </div>
 <datalist id="svcListAll"></datalist><datalist id="entsAll"></datalist>
 <div id="devPane"></div>`;
 ensureDatalists();
 devTab(DEV_TAB,true);}
function devTab(t,keep){
 DEV_TAB=t;if(!keep){$$('#view .seg-btn').forEach((b,i)=>b.classList.toggle('active',['svc','states','events','tpl'][i]===t));}
 const p=$('#devPane');
 if(t==='svc')devSvc(p);else if(t==='states')devStates(p);else if(t==='events')devEvents(p);else devTpl(p);}
function ensureDatalists(){
 const sl=$('#svcListAll');
 if(sl&&!sl.options.length&&SERVICES){
  const opts=[];Object.keys(SERVICES).sort().forEach(d=>
   Object.keys(SERVICES[d].services||SERVICES[d]||{}).forEach(s=>opts.push(d+'.'+s)));
  sl.innerHTML=opts.map(o=>`<option value="${o}">`).join('');}
 const el=$('#entsAll');
 if(el&&!el.options.length){el.innerHTML=Object.keys(STATES).sort().map(e=>`<option value="${e}">`).join('');}}
async function devSvc(p){
 if(!SERVICES||!Object.keys(SERVICES).length)try{SERVICES=normServices(await api(HA_API+'/ha/services'));}catch(_){SERVICES={};}
 ensureDatalists();
 const domains=Object.keys(SERVICES).sort();
 p.innerHTML=`
 <div class="grid2" style="grid-template-columns:340px 1fr">
  <div class="panel" style="max-height:70vh;overflow-y:auto">
   <input id="svcFilter" class="input" placeholder="🔍 фильтр служб…">
   <div id="svcTree" style="margin-top:10px"></div></div>
  <div class="panel" id="svcDetail"><div class="hint">Выберите службу слева. Вызов идёт через официальный REST API HA.</div></div>
 </div>`;
 const renderTree=(q='')=>{
  $('#svcTree').innerHTML=domains.filter(d=>!q||d.includes(q)).map(d=>{
   const svcs=Object.keys(SERVICES[d].services||{}).filter(s=>!q||(d+'.'+s).includes(q));
   if(q&&!svcs.length&&!d.includes(q))return '';
   return `<details ${q?'open':''} style="margin-bottom:4px"><summary style="cursor:pointer;font-weight:600;font-size:13px;padding:4px">
    ${iconOfDomain(d)} ${d} <span class="badge mut">${svcs.length}</span></summary>
    ${svcs.map(s=>`<div style="padding:4px 10px;cursor:pointer;font-size:12.5px" class="mono"
     onclick="showSvc('${d}','${s}')">${d}.${s}</div>`).join('')}</details>`;}).join('');};
 renderTree();
 $('#svcFilter').oninput=debounce(()=>renderTree($('#svcFilter').value.trim().toLowerCase()),140);}
function showSvc(d,s){
 const info=(SERVICES[d].services||{})[s]||{};
 const fields=info.fields||{};
 const fkeys=Object.keys(fields);
 const example={};fkeys.forEach(k=>{const f=fields[k]||{};
  example[k]=f.example!=null?f.example:(f.default!=null?f.default:'…');});
 $('#svcDetail').innerHTML=`
 <h3 class="mono">${d}.${s}</h3>
 <div class="hint">${esc(info.description||'')}</div>
 ${fkeys.length?`<table class="kv" style="margin:10px 0">${fkeys.map(k=>{const f=fields[k]||{};
  return `<tr><td class="mono">${esc(k)}${f.required?' <span class="badge warn">*</span>':''}</td>
  <td>${esc(f.description||'')}<br><span class="hint mono">${esc(f.selector?JSON.stringify(Object.keys(f.selector)[0]||f.selector):'')}</span></td></tr>`;}).join('')}</table>`:''}
 <label>Цель — entity_id (через запятую, необязательно)</label>
 <input id="svcTarget" class="input mono" list="entsAll" placeholder="light.kitchen, switch.fan">
 <label>Данные службы (JSON)</label>
 <textarea id="svcDataDev" class="input mono" rows="7" spellcheck="false">${esc(JSON.stringify(fkeys.length?example:{},null,2))}</textarea>
 <div class="row" style="margin-top:12px">
  <button class="btn accent" onclick="devCallSvc('${d}','${s}')">⚡ Вызвать (REST)</button>
  <button class="btn" onclick="devCallSvcWS('${d}','${s}')">⚡ Вызвать (WS + response)</button></div>
 <pre class="out" id="svcOut" style="display:none"></pre>`;}
function devSvcData(){
 let data={};const txt=$('#svcDataDev').value.trim();
 if(txt&&txt!=='{}'){try{data=JSON.parse(txt);}catch(e){toast('JSON некорректен: '+esc(e.message),'err');return null;}}
 const tgt=$('#svcTarget').value.trim();
 if(tgt)data.entity_id=tgt.split(',').map(x=>x.trim()).length===1?tgt.split(',')[0].trim():tgt.split(',').map(x=>x.trim());
 return data;}
async function devCallSvc(d,s){
 const data=devSvcData();if(data==null)return;
 const out=$('#svcOut');out.style.display='';out.textContent='выполняется…';
 try{const r=await api(HA_API+'/ha/services/'+d+'/'+s,{method:'POST',body:data});
  out.textContent=JSON.stringify(r,null,2);toast('✅ '+d+'.'+s+' вызвана','ok');}
 catch(e){out.textContent='ОШИБКА: '+e.message;}}
async function devCallSvcWS(d,s){
 const data=devSvcData();if(data==null)return;
 const out=$('#svcOut');out.style.display='';out.textContent='выполняется (WS)…';
 try{let r;
  try{r=await callSvcWS(d,s,data);}
  catch(_){r=await haproxy({type:'call_service',domain:d,service:s,service_data:data,target:{}});}
  out.textContent=JSON.stringify(r,null,2);toast('✅ '+d+'.'+s+' вызвана (WS)','ok');}
 catch(e){out.textContent='ОШИБКА: '+e.message;}}
function devStates(p){
 p.innerHTML=`<div class="page-head"><input id="stFilter" class="input search" placeholder="🔍 фильтр…">
  <span class="hint" id="stCount"></span></div>
 <div class="tbl-wrap"><table class="tbl"><thead><tr>
  <th>entity_id</th><th style="width:200px">state</th><th style="width:170px">изменён</th><th style="width:90px"></th>
 </tr></thead><tbody id="stBody"></tbody></table></div>`;
 const render=()=>{
  const q=($('#stFilter').value||'').toLowerCase();
  let eids=Object.keys(STATES).sort();
  if(q)eids=eids.filter(e=>e.includes(q)||FN(e).toLowerCase().includes(q)||String(STATES[e].state).includes(q));
  $('#stCount').textContent=eids.length+' из '+Object.keys(STATES).length;
  $('#stBody').innerHTML=eids.slice(0,500).map(e=>{const st=STATES[e];
   return `<tr><td class="mono" style="cursor:pointer" onclick="moreInfo('${e}')">${iconOf(st)} ${e}</td>
   <td class="mono">${esc(st.state)}</td><td class="hint mono">${esc(ftime(st.last_changed))}</td>
   <td><button class="btn sm" onclick="showRaw('${e}')">{ }</button></td></tr>`;}).join('');};
 render();$('#stFilter').oninput=debounce(render,140);}
function showRaw(e){
 openDialog(`<div class="dlg-head"><div class="dlg-ic">{ }</div>
  <div><div class="dlg-title mono">${esc(e)}</div><div class="dlg-sub">сырое состояние</div></div>
  <div class="spacer"></div><button class="icon-btn" onclick="closeDialog()">✕</button></div>
  <div class="dlg-body"><pre class="out" style="max-height:60vh">${esc(JSON.stringify(S(e),null,2))}</pre></div>`);}
function devEvents(p){
 p.innerHTML=`
 <div class="grid2" style="grid-template-columns:1fr 1fr">
  <div class="panel">
   <h3>📡 Слушатель событий (WebSocket API)</h3>
   <div class="row"><input id="evType" class="input mono" placeholder="event_type (пусто = все)" style="flex:1" value="state_changed">
   <button class="btn accent" id="evBtn" onclick="evToggle()">▶ Слушать</button></div>
   <div class="row" style="margin-top:8px"><button class="btn sm" onclick="evClear()">очистить</button>
   <label class="hint" style="margin:0;display:flex;gap:6px;align-items:center"><input type="checkbox" id="evPause" style="width:auto" onchange="EV_PAUSED=this.checked"> пауза</label></div>
   <div class="ev-list" id="evList" style="margin-top:10px"></div>
  </div>
  <div class="panel">
   <h3>🔥 Вызвать событие</h3>
   <label>event_type</label><input id="fireType" class="input mono" value="my_custom_event">
   <label>данные (JSON)</label><textarea id="fireData" class="input mono" rows="5">{\n "hello": "world"\n}</textarea>
   <button class="btn accent" style="margin-top:10px" onclick="fireEvent()">Вызвать событие</button>
   <h3 style="margin-top:20px">ℹ️ Все типы событий HA</h3>
   <div class="hint">Через слушатель выше доступны все события: state_changed, call_service, automation_triggered, timer.finished, …</div>
  </div>
 </div>`;}
function evToggle(){
 if(EV_PID!=null){unproxy(EV_PID);EV_PID=null;$('#evBtn').textContent='▶ Слушать';return;}
 const t=$('#evType').value.trim();
 const payload=t?{type:'subscribe_events',event_type:t}:{type:'subscribe_events'};
 $('#evBtn').textContent='⏹ Остановить';
 EV_PID=haSub(payload,(ev)=>{
  if(EV_PAUSED)return;
  const list=$('#evList');if(!list)return;
  const div=document.createElement('div');div.className='ev-item';
  div.innerHTML=`<span class="t">${new Date().toLocaleTimeString('ru-RU')}</span>
   <pre>${esc(JSON.stringify(ev.data&&t!=='state_changed'?ev.data:(ev.data&&ev.data.entity_id?ev.data.entity_id+' → '+((ev.data.new_state||{}).state):ev.data),null,0))}</pre>`;
  list.prepend(div);
  while(list.children.length>250)list.lastChild.remove();});
 toast('Подписка активна (pid '+EV_PID+')','ok',2000);}
function evClear(){const l=$('#evList');if(l)l.innerHTML='';}
async function fireEvent(){
 const t=$('#fireType').value.trim();if(!t){toast('Укажите тип события','err');return;}
 let d={};try{d=JSON.parse($('#fireData').value||'{}');}catch(e){toast('JSON некорректен','err');return;}
 try{await haproxy({type:'fire_event',event_type:t,event_data:d});toast('🔥 Событие '+t+' отправлено','ok');}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}
function devTpl(p){
 p.innerHTML=`
 <div class="grid2" style="grid-template-columns:1fr 1fr">
  <div class="panel"><h3>🧪 Шаблон Jinja2 (живой рендеринг)</h3>
   <textarea id="tplSrc" class="input mono" rows="10" spellcheck="false">{{ states | count }} сущностей
Свет включён: {{ states.light | selectattr('state','eq','on') | list | count }}
Сейчас: {{ now().strftime('%H:%M:%S') }}</textarea>
   <div class="row" style="margin-top:10px">
    <button class="btn accent" onclick="tplRender()">▶ Рендерить (live)</button>
    <button class="btn" onclick="tplStop()">⏹ Стоп</button></div>
  </div>
  <div class="panel"><h3>Результат</h3><pre class="out" id="tplOut" style="max-height:52vh;display:block">—</pre></div>
 </div>`;}
function tplRender(){
 tplStop();
 const src=$('#tplSrc').value;
 $('#tplOut').textContent='ожидание…';
 TPL_PID=haSub({type:'render_template',template:src},(ev)=>{
  $('#tplOut').textContent=(ev&&ev.result!=null)?String(ev.result):JSON.stringify(ev,null,2);});
 $('#tplSrc').oninput=debounce(()=>{tplStop();tplRender();},700);}
function tplStop(){if(TPL_PID!=null){unproxy(TPL_PID);TPL_PID=null;}}

/* ═══════════════════════ СИСТЕМА ═══════════════════════ */
async function pageSystem(){
 $('#view').innerHTML='<div class="loading">Загрузка…</div>';
 const [cfg,ps]=await Promise.all([apiSafe(HA_API+'/ha/config'),apiSafe(HA_API+'/status')]);
 SYSCFG=cfg||SYSCFG;
 const comps=(cfg&&cfg.components)||[];
 $('#view').innerHTML=`
 <div class="grid2">
  <div class="panel"><h3>🖥 Home Assistant</h3>
   <div class="stat-row"><span class="k">Версия</span><b>${esc(cfg&&cfg.version||'?')}</b></div>
   <div class="stat-row"><span class="k">Дом</span><b>${esc(cfg&&cfg.location_name||'')}</b></div>
   <div class="stat-row"><span class="k">Часовой пояс</span><b>${esc(cfg&&cfg.time_zone||'')}</b></div>
   <div class="stat-row"><span class="k">Координаты</span><b class="mono">${cfg?`${(+cfg.latitude).toFixed(3)}, ${(+cfg.longitude).toFixed(3)} (${cfg.elevation} м)`:'?'}</b></div>
   <div class="stat-row"><span class="k">Единицы</span><b>${esc(cfg&&cfg.unit_system&&cfg.unit_system.temperature||'')} / ${esc(cfg&&cfg.unit_system&&cfg.unit_system.length||'')}</b></div>
   <div class="stat-row"><span class="k">Источник конфига</span><b>${esc(cfg&&cfg.config_source||'')}</b></div>
   <div class="stat-row"><span class="k">Компонентов</span><b>${comps.length}</b></div>
   <div class="row" style="margin-top:14px">
    <button class="btn" onclick="checkConfig()">🔍 Проверить конфигурацию</button>
    <button class="btn" onclick="if(confirmDo('Перезагрузить все YAML-конфигурации?'))callSvc('homeassistant','reload_all',{})">♻️ Reload all</button>
    <button class="btn danger" onclick="if(confirmDo('ПЕРЕЗАПУСТИТЬ Home Assistant?'))callSvc('homeassistant','restart',{})">🔄 Перезапуск HA</button>
    <button class="btn danger" onclick="if(confirmDo('ВЫКЛЮЧИТЬ Home Assistant?'))callSvc('homeassistant','shutdown',{})">⏻ Выключить</button>
   </div>
   <pre class="out" id="ccOut" style="display:none"></pre>
  </div>
  <div class="panel"><h3>🌐 Панель (этот сервер)</h3>
   <div class="stat-row"><span class="k">Версия панели</span><b>${esc(ps&&ps.version||'?')}</b></div>
   <div class="stat-row"><span class="k">Связь с HA</span><b>${ps?(ps.ws?'<span class="badge ok">WebSocket</span>':'<span class="badge warn">опрос REST</span>'):''}</b></div>
   <div class="stat-row"><span class="k">Сущностей в кэше</span><b>${ps?ps.entities:0}</b></div>
   <div class="stat-row"><span class="k">Открытых клиентов</span><b>${ps?ps.clients:0}</b></div>
   <div class="stat-row"><span class="k">Аптайм панели</span><b>${ps?Math.floor(ps.uptime/60)+' мин '+(ps.uptime%60)+' с':''}</b></div>
   <div class="stat-row"><span class="k">Адрес HA</span><b class="mono">${esc(ps&&ps.ha_url||'—')}</b></div>
  </div>
 </div>
 <div class="panel" style="margin-top:14px"><h3>🧩 Загруженные компоненты (${comps.length})</h3>
  <div class="chips">${comps.sort().slice(0,220).map(c=>`<span class="chip" style="cursor:default">${esc(c)}</span>`).join('')}</div>
  ${comps.length>220?'<div class="hint">…и ещё '+(comps.length-220)+'</div>':''}</div>`;}
async function checkConfig(){
 const out=$('#ccOut');out.style.display='';out.textContent='проверяю…';
 try{const r=await api(HA_API+'/ha/config/core/check_config',{method:'POST'});
  out.textContent=JSON.stringify(r,null,2);
  toast(r&&r.result==='valid'?'✅ Конфигурация корректна':'⚠️ Есть замечания',r&&r.result==='valid'?'ok':'warn',6000);}
 catch(e){out.textContent='ОШИБКА: '+e.message;}}

/* ═══════════════════════ НАСТРОЙКИ ПАНЕЛИ ═══════════════════════ */
async function pageSettings(){
 let ps=null;try{ps=await api(HA_API+'/status');}catch(_){}
 const prefs=LS('hap_prefs')||{};
 $('#view').innerHTML=`
 <div class="grid2">
  <div class="panel"><h3>🔗 Подключение к Home Assistant</h3>
   <div class="form">
    <label>URL Home Assistant *</label>
    <input id="setUrl" class="input mono" placeholder="http://homeassistant.local:8123" value="${esc(ps&&ps.ha_url||'')}">
    <label>Долгосрочный токен доступа *</label>
    <input id="setToken" class="input mono" type="password" placeholder="${ps&&ps.has_token?'••••• сохранён — введите для замены':'eyJhbGciOiJIUzI1NiIsInR5cCI6…'}">
    <div class="hint">HA → Профиль → Безопасность → «Долгосрочные токены доступа» → создать.</div>
    <div class="row" style="margin-top:10px">
     <button class="btn" onclick="testHA()">🔌 Проверить</button>
     <button class="btn accent" onclick="saveHA()">💾 Сохранить подключение</button></div>
    <pre class="out" id="haOut" style="display:none"></pre>
   </div></div>
  <div class="panel"><h3>🔒 Доступ к панели</h3>
   <div class="form">
    <label>Пароль панели (пусто = без пароля)</label>
    <input id="setPw" class="input" type="password" placeholder="${ps&&ps.has_password?'••••• задан — введите для замены':'не задан'}">
    <div class="hint">При установке пароля все страницы и API панели закрываются cookie-сессией.</div>
    <button class="btn accent" style="margin-top:10px" onclick="savePw()">💾 Сохранить пароль</button>
    ${ps&&ps.has_password?'<button class="btn danger" style="margin-top:10px" onclick="clearPw()">Убрать пароль</button>':''}
   </div></div>
  <div class="panel"><h3>🎨 Интерфейс (хранится в браузере)</h3>
   <div class="form">
    <label>Тема</label>
    <select id="haThemeSel" class="input" style="max-width:300px" onchange="setThemePref(this.value)">
    ${HA_THEME_LIST.map(function(t){return '<option value="'+t+'"'+(t===haTheme()?' selected':'')+'>'+
      (HA_THEME_TITLE[t]||t)+'</option>';}).join('')}
   </select>
   <div class="hint">Тема общая для всей системы Infinity — меняется и на других страницах.</div>
    <label>Группировка дашборда</label>
    <div class="seg"><button class="seg-btn ${(prefs.grouping||'area')==='area'?'active':''}" onclick="setPref('grouping','area');pageSettings()">По областям</button>
    <button class="seg-btn ${prefs.grouping==='domain'?'active':''}" onclick="setPref('grouping','domain');pageSettings()">По типам</button></div>
    <label>Фон карты (URL изображения — план дома/плитки)</label>
    <input id="setMapBg" class="input mono" placeholder="https://…/floor.png" value="${esc(prefs.mapbg||'')}">
    <label>Интервал обновления снимков камер (сек)</label>
    <input id="setSnap" class="input mono" type="number" min="2" max="120" value="${prefs.snap_interval||5}" style="max-width:120px">
    <label>Интервал REST-опроса при недоступном WS (сек, на сервере)</label>
    <input id="setPoll" class="input mono" type="number" min="2" max="300" value="${ps&&ps.poll_interval||5}" style="max-width:120px">
    <div class="row" style="margin-top:12px">
     <button class="btn accent" onclick="saveUI()">💾 Сохранить</button>
     <button class="btn danger" onclick="if(confirmDo('Очистить локальные настройки браузера?')){localStorage.clear();location.reload();}">🧹 Сброс</button></div>
   </div></div>
  <div class="panel"><h3>ℹ️ О панели</h3>
   <div class="stat-row"><span class="k">HA Web Panel</span><b>v${esc(ps&&ps.version||'1.0')}</b></div>
   <div class="stat-row"><span class="k">Сервер</span><b>Модуль InfinitySearch (FastAPI + httpx)</b></div>
   <div class="stat-row"><span class="k">Фронтенд</span><b>SPA в теме системы</b></div>
   <div class="hint" style="margin-top:10px">Все запросы к HA идут через сервер панели — токен HA не покидает сервер.
   Прозрачные прокси: <span class="mono">/api/ha/**</span> (REST) и WS-прокси (все команды HA WebSocket API).</div>
   ${ps&&ps.has_password?'<div class="row" style="margin-top:10px"><button class="btn" onclick="logout()">🚪 Выйти</button></div>':''}
  </div>
 </div>`;}
function setThemePref(t){try{
 if(window.InfinityTheme&&InfinityTheme.set)InfinityTheme.set(t);
 else{localStorage.setItem('infinity_theme',t);applyTheme();}
 }catch(_){}
 haSyncThemeSelect();}
function setPref(k,v){const p=LS('hap_prefs')||{};p[k]=v;LS('hap_prefs',p);}
async function saveUI(){
 setPref('mapbg',$('#setMapBg').value.trim());
 setPref('snap_interval',Math.max(2,+$('#setSnap').value||5));
 const poll=+$('#setPoll').value||5;
 try{await api(HA_API+'/setup',{method:'POST',body:{poll_interval:poll}});}catch(_){}
 toast('💾 Настройки сохранены','ok');}
async function testHA(){
 const out=$('#haOut');out.style.display='';out.textContent='проверяю…';
 try{
  const r=await api(HA_API+'/setup',{method:'POST',body:{test:true,
   ha_url:$('#setUrl').value.trim(),token:$('#setToken').value.trim()}});
  out.textContent='✅ Подключение успешно! '+(r.info?'HA '+(r.info.version||'')+' · '+(r.info.location||''):'');
 }catch(e){out.textContent='❌ '+e.message;}}
async function saveHA(){
 const out=$('#haOut');out.style.display='';out.textContent='сохраняю…';
 try{
  const r=await api(HA_API+'/setup',{method:'POST',body:{
   ha_url:$('#setUrl').value.trim(),token:$('#setToken').value.trim()}});
  out.textContent='✅ Сохранено.';toast('💾 Подключение сохранено','ok');
  STATES={};await sleep(1200);
  const s=await apiSafe(HA_API+'/ha/states');
  if(Array.isArray(s)){STATES={};s.forEach(x=>STATES[x.entity_id]=x);}
  await loadRegistries();await loadServices();go('dashboard');
 }catch(e){out.textContent='❌ '+e.message;toast('Ошибка: '+esc(e.message),'err');}}
async function savePw(){
 try{await api(HA_API+'/setup',{method:'POST',body:{app_password:$('#setPw').value}});
  toast('💾 Пароль сохранён. При следующем входе потребуется пароль.','ok');setTimeout(()=>location.reload(),1200);}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}
async function clearPw(){
 if(!confirmDo('Убрать пароль панели?'))return;
 try{await api(HA_API+'/setup',{method:'POST',body:{app_password:''}});toast('Пароль убран','ok');pageSettings();}
 catch(e){toast('Ошибка: '+esc(e.message),'err');}}
async function logout(){await apiSafe(HA_API+'/logout',{method:'POST'});location.href=HA_BASE+'/login';}

/* ═══════════════════════ ИНИЦИАЛИЗАЦИЯ ═══════════════════════ */
async function loadServices(){try{SERVICES=normServices(await api(HA_API+'/ha/services'));}catch(_){SERVICES=SERVICES||{};}}
function normServices(r){
 if(Array.isArray(r)){const o={};r.forEach(x=>{if(x&&x.domain)o[x.domain]={services:x.services||{}};});return o;}
 return r||{};}
async function init(){
 applyTheme();
 haOnThemeChange();
 $('#themeBtn').onclick=toggleTheme;
 $('#burger').onclick=()=>$('#sidebar').classList.toggle('open');
 $('#dialogBackdrop').onclick=closeDialog;
 document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#dialogWrap').classList.contains('hidden'))closeDialog();});
 buildNav();
 wsConnect();
 let ps=null;try{ps=await api(HA_API+'/status');}catch(e){
  $('#view').innerHTML='<div class="empty">Сервер панели не отвечает</div>';return;}
 setChip(ps.ws);
 if(!ps.configured){
  go('settings');
  toast('👋 Добро пожаловать! Укажите адрес Home Assistant и долгосрочный токен, затем «Сохранить подключение».','warn',12000);
  return;}
 go(location.hash.slice(1)||'dashboard');
 loadServices();
 for(let i=0;i<30&&!Object.keys(STATES).length;i++){await sleep(300);}
 if(!Object.keys(STATES).length){
  const s=await apiSafe(HA_API+'/ha/states');
  if(Array.isArray(s)){STATES={};s.forEach(x=>STATES[x.entity_id]=x);}}
 await loadRegistries();
 refreshRoute();
 setInterval(()=>{if(WS&&WS.readyState===1)wsSend({type:'ping'});},25000);
}
window.addEventListener('hashchange',()=>setRoute(location.hash.slice(1)||'dashboard'));
window.addEventListener('resize',debounce(()=>{if(ROUTE==='map')drawMapNow();
 else if($('#histCv')&&$('#histCv').clientWidth)loadHistory();},300));
window.addEventListener('DOMContentLoaded',init);