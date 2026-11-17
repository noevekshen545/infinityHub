/* Infinity Social — плавающие виджеты: мини-чат (как в VK) и аудиоплеер.
   Чаты и трек запоминаются в localStorage и восстанавливаются при переходе между разделами.

   Использование:
     SocialDock.openChat({id, title, avatar, href, preview});
     SocialDock.closeChat(id);
     SocialDock.setAudio({title, artist, src, cover});
     SocialDock.toggleAudio();
*/
(function(){
  'use strict';

  const LS_CHATS='sn_dock_chats';
  const LS_AUDIO='sn_dock_audio';
  const W=340, H=420;

  function esc(s){
    return String(s===null||s===undefined?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function el(tag,cls,html){
    const e=document.createElement(tag);
    if(cls)e.className=cls;
    if(html!==undefined)e.innerHTML=html;
    return e;
  }

  const state={chats:{},order:[],audio:null,z:{},pos:{}};

  function load(){
    try{
      const raw=JSON.parse(localStorage.getItem(LS_CHATS)||'[]');
      if(Array.isArray(raw)) raw.forEach(c=>{ if(c&&c.id) openChat(c,{silent:true}); });
    }catch(e){}
    try{
      const a=JSON.parse(localStorage.getItem(LS_AUDIO)||'null');
      if(a&&a.src) setAudio(a,{autoplay:false});
    }catch(e){}
  }
  function saveChats(){
    try{
      localStorage.setItem(LS_CHATS,JSON.stringify(state.order.map(id=>{
        const c=state.chats[id];
        return {id:c.id,title:c.title,avatar:c.avatar,href:c.href,unread:c.unread||0,preview:c.preview||'',min:!!c.min};
      })));
    }catch(e){}
  }
  function saveAudio(){
    try{
      localStorage.setItem(LS_AUDIO,JSON.stringify(state.audio?{
        title:state.audio.title,artist:state.audio.artist,src:state.audio.src,cover:state.audio.cover
      }:null));
    }catch(e){}
  }

  function ensureDock(){
    let dock=document.getElementById('snDock');
    if(!dock){
      dock=el('div'); dock.id='snDock'; dock.className='sn-dock';
      document.body.appendChild(dock);
      document.addEventListener('keydown',e=>{
        if(e.key==='Escape'){ const last=state.order[state.order.length-1]; if(last) closeChat(last); }
      });
    }
    return dock;
  }

  function openChat(data,opt){
    opt=opt||{};
    ensureDock();
    const id=data.id;
    if(!state.chats[id]){
      state.chats[id]={
        id:id,title:data.title||'Чат',avatar:data.avatar||'',href:data.href||'',
        unread:data.unread||0,preview:data.preview||'',min:false
      };
      state.order.push(id);
    }else{
      const c=state.chats[id];
      if(data.title)c.title=data.title;
      if(data.avatar)c.avatar=data.avatar;
      if(data.href)c.href=data.href;
      if(data.unread!==undefined)c.unread=data.unread;
      if(data.min!==undefined)c.min=!!data.min;
    }
    if(!opt.silent&&data.unread===undefined)state.chats[id].unread=0;
    renderDock();
    saveChats();
    return state.chats[id];
  }

  function closeChat(id){
    delete state.chats[id];
    state.order=state.order.filter(x=>x!==id);
    delete state.z[id];
    delete state.pos[id];
    renderDock();
    saveChats();
  }
  function toggleChat(data){ state.chats[data.id]?closeChat(data.id):openChat(data); }

  function markUnread(id,n){
    if(!state.chats[id])return;
    state.chats[id].unread=n||0;
    renderDock(); saveChats();
  }
/* Перетаскивание окна мини-чата */
  function makeDraggable(win,id){
    const head=win.querySelector('.sn-chat-head');
    if(!head)return;
    let sx=0,sy=0,ox=0,oy=0,drag=false;
    head.addEventListener('mousedown',e=>{
      if(e.target.closest('.sn-chat-btn'))return;
      const r=win.getBoundingClientRect();
      state.z[id]=(state.z[id]||100)+1; win.style.zIndex=state.z[id];
      drag=true; sx=e.clientX; sy=e.clientY; ox=r.left; oy=r.top;
      e.preventDefault();
    });
    document.addEventListener('mousemove',e=>{
      if(!drag)return;
      const w=win.offsetWidth,h=win.offsetHeight;
      let x=e.clientX-sx+ox, y=e.clientY-sy+oy;
      x=Math.max(8,Math.min(window.innerWidth-w-8,x));
      y=Math.max(8,Math.min(window.innerHeight-h-8,y));
      win.style.left=x+'px'; win.style.top=y+'px'; win.style.right='auto'; win.style.bottom='auto';
      state.pos[id]={left:x,top:y};
    });
    document.addEventListener('mouseup',()=>{drag=false});
  }

  function renderDock(){
    const dock=ensureDock();
    dock.querySelectorAll('.sn-chat').forEach(n=>n.remove());
    state.order.forEach(id=>{
      const c=state.chats[id];
      if(!c)return;
      const min=!!c.min;
      const win=el('div','sn-chat'+(min?' sn-chat--min':''));
      win.id='sn-chat-'+id;
      win.style.width=W+'px';
      if(!state.z[id]){
        const n=state.order.length;
        const top=Math.max(70, window.innerHeight-H-20-(n-1)*46);
        state.z[id]=900+n;
        win.style.right='16px';
        win.style.top=top+'px';
      }
      // Восстанавливаем позицию после перерисовки: иначе окно «прыгало»
      // бы в угол при каждом обновлении дока (свернуть/развернуть, новые сообщения).
      if(state.pos[id]){
        win.style.left=state.pos[id].left+'px';
        win.style.top=state.pos[id].top+'px';
        win.style.right='auto';
        win.style.bottom='auto';
      }
      win.style.zIndex=state.z[id];
      const av=c.avatar
        ? '<img class="sn-chat-av" src="'+esc(c.avatar)+'" alt="">'
        : '<span class="sn-chat-av sn-chat-av--ph"><i class="fas fa-user"></i></span>';
      const badge=c.unread?'<span class="sn-chat-badge">'+(c.unread>99?'99+':c.unread)+'</span>':'';
      win.innerHTML=
        '<div class="sn-chat-head">'+
          '<div class="sn-chat-id">'+av+
            '<span class="sn-chat-title">'+esc(c.title)+'</span>'+badge+
          '</div>'+
          '<span class="sn-chat-btns">'+
            '<button class="sn-chat-btn" data-act="open" title="Открыть в мессенджере"><i class="fas fa-expand"></i></button>'+
            '<button class="sn-chat-btn" data-act="min" title="'+(min?'Развернуть':'Свернуть')+'"><i class="fas '+(min?'fa-plus':'fa-minus')+'"></i></button>'+
            '<button class="sn-chat-btn" data-act="kill" title="Закрыть"><i class="fas fa-times"></i></button>'+
          '</span>'+
        '</div>'+
        '<div class="sn-chat-body">'+
          '<div class="sn-chat-empty"><i class="fas fa-comments"></i><p>'+esc(c.preview||'Перейдите в переписку, чтобы продолжить общение')+'</p></div>'+
        '</div>'+
        '<div class="sn-chat-foot">'+
          '<button class="sn-send" data-act="go"><i class="fas fa-paper-plane"></i> Открыть чат</button>'+
        '</div>';
      win.querySelectorAll('[data-act]').forEach(b=>{
        b.addEventListener('click',e=>{
          e.stopPropagation();
          const a=b.getAttribute('data-act');
          if(a==='kill') closeChat(id);
          else if(a==='min'){
            c.min=!c.min;
            win.classList.toggle('sn-chat--min',c.min);
            const ic=b.querySelector('i'); if(ic) ic.className='fas '+(c.min?'fa-plus':'fa-minus');
            b.title=c.min?'Развернуть':'Свернуть';
            saveChats();
          }
          else location.href=c.href||('/social/messages?conv='+encodeURIComponent(id));
        });
      });
      dock.appendChild(win);
      makeDraggable(win,id);
    });
  }
/* ---------------- Аудиоплеер ---------------- */
  let audioEl=null;
  function ensureAudio(){
    if(!audioEl){
      audioEl=document.createElement('audio');
      audioEl.id='snAudio';
      audioEl.preload='none';
      document.body.appendChild(audioEl);
      audioEl.addEventListener('timeupdate',updateAudioProgress);
      audioEl.addEventListener('ended',()=>setPlayIcon(false));
    }
    return audioEl;
  }
  function setAudio(data,opt){
    opt=opt||{};
    ensureDock();
    state.audio={title:data.title||'Без названия',artist:data.artist||'',src:data.src,cover:data.cover||''};
    const a=ensureAudio();
    if(a.getAttribute('src')!==data.src) a.src=data.src;
    saveAudio();
    renderAudio();
    if(opt.autoplay!==false) a.play().then(()=>setPlayIcon(true)).catch(()=>{});
  }
  function setPlayIcon(playing){
    const b=document.getElementById('snAudioPlay');
    if(b) b.innerHTML='<i class="fas '+(playing?'fa-pause':'fa-play')+'"></i>';
  }
  function fmtTime(s){
    if(!isFinite(s)||s<0)s=0;
    const m=Math.floor(s/60), sec=Math.floor(s%60);
    return m+':'+String(sec).padStart(2,'0');
  }
  function updateAudioProgress(){
    const a=ensureAudio();
    const bar=document.getElementById('snAudioBar');
    const cur=document.getElementById('snAudioCur');
    const tim=document.getElementById('snAudioTime');
    if(!bar)return;
    bar.style.width=(a.duration?(a.currentTime/a.duration)*100:0)+'%';
    if(cur)cur.textContent=fmtTime(a.currentTime);
    if(tim)tim.textContent=fmtTime(a.duration||0);
  }
  function renderAudio(){
    ensureDock();
    const old=document.getElementById('snAudioBox');
    if(old)old.remove();
    const a=state.audio;
    if(!a)return;
    const box=el('div','sn-audio'); box.id='snAudioBox';
    const cover=a.cover?'background-image:url('+esc(a.cover)+')':'';
    box.innerHTML=
      '<div class="sn-audio-cover" style="'+cover+'"><i class="fas fa-music"></i></div>'+
      '<div class="sn-audio-meta">'+
        '<div class="sn-audio-title" title="'+esc(a.title)+'">'+esc(a.title)+'</div>'+
        '<div class="sn-audio-artist">'+esc(a.artist||'')+'</div>'+
        '<div class="sn-audio-bar" id="snAudioSeek"><div class="sn-audio-fill" id="snAudioBar" style="width:0%"></div></div>'+
        '<div class="sn-audio-time"><span id="snAudioCur">0:00</span><span id="snAudioTime">0:00</span></div>'+
      '</div>'+
      '<div class="sn-audio-ctrl">'+
        '<button id="snAudioPlay" title="Играть / Пауза"><i class="fas fa-play"></i></button>'+
        '<button id="snAudioPrev" title="Предыдущий трек"><i class="fas fa-backward"></i></button>'+
        '<button id="snAudioNext" title="Следующий трек"><i class="fas fa-forward"></i></button>'+
        '<button id="snAudioClose" title="Закрыть плеер"><i class="fas fa-times"></i></button>'+
      '</div>';
    document.getElementById('snDock').appendChild(box);
    box.querySelector('#snAudioPlay').addEventListener('click',toggleAudio);
    box.querySelector('#snAudioClose').addEventListener('click',()=>{
      ensureAudio().pause(); state.audio=null; saveAudio(); renderAudio();
    });
    box.querySelector('#snAudioSeek').addEventListener('click',seek);
    box.querySelector('#snAudioPrev').addEventListener('click',()=>{ if(typeof window.SN_AUDIO_PREV==='function') window.SN_AUDIO_PREV(); });
    box.querySelector('#snAudioNext').addEventListener('click',()=>{ if(typeof window.SN_AUDIO_NEXT==='function') window.SN_AUDIO_NEXT(); });
    if(audioEl&&audioEl.src&&!audioEl.paused) setPlayIcon(true);
    updateAudioProgress();
  }
  function seek(e){
    const a=ensureAudio();
    const bar=document.getElementById('snAudioBar');
    if(!bar||!a.duration)return;
    const r=bar.getBoundingClientRect();
    const p=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
    a.currentTime=p*a.duration;
    updateAudioProgress();
  }
  function toggleAudio(){
    const a=ensureAudio();
    if(!state.audio)return;
    if(a.paused){a.play().catch(()=>{});setPlayIcon(true)}else{a.pause();setPlayIcon(false)}
  }

  window.SocialDock={
    openChat:openChat, closeChat:closeChat, toggleChat:toggleChat, markUnread:markUnread,
    setAudio:setAudio, seek:seek, toggleAudio:toggleAudio, load:load
  };

  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',load);
  else load();

  /* Кнопки «предыдущий/следующий» плеера делегируются странице, если она их определила */
  window.SN_AUDIO_PREV=function(){ if(typeof playPrev==='function'){try{playPrev()}catch(e){}} };
  window.SN_AUDIO_NEXT=function(){ if(typeof playNext==='function'){try{playNext()}catch(e){}} };
})();