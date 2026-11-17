/* 06-plugins.js — Внешний вид и плагины (JS) */
/* ============================================================
   APPEARANCE ENGINE
   ============================================================ */
function applyAppearance(){
  const a=S().appearance||{}; const r=document.documentElement.style;
  const accent=a.accent||'#4f46e5';
  r.setProperty('--accent',accent);
  r.setProperty('--accent-2',accent);
  r.setProperty('--accent-soft',accent+'14');
  document.body.dataset.density=a.density||'cozy';
  document.body.dataset.font=a.font==='system'?'':(a.font||'');
  document.body.dataset.backdrop=a.backdrop||'grid';
  document.body.style.fontSize={sm:'13px',md:'14px',lg:'15.5px'}[a.fontSize||'md'];
  const rad={sharp:['6px','8px','10px','12px','14px'],md:['7px','10px','13px','16px','20px'],round:['9px','12px','16px','20px','26px']}[a.radius||'md'];
  r.setProperty('--r-xs',rad[0]);r.setProperty('--r-sm',rad[1]);r.setProperty('--r',rad[2]);r.setProperty('--r-lg',rad[3]);r.setProperty('--r-xl',rad[4]);
  r.setProperty('--msgw',{sm:'640px',md:'860px',lg:'1100px'}[a.msgWidth||'md']);
  $$('#accentSwatches .swatch').forEach(s=>s.classList.toggle('on',s.dataset.accent===accent));
  const ac=$('#accentCustom'); if(ac&&ac.value!==accent) ac.value=accent;
}
$('#accentSwatches').addEventListener('click',e=>{
  const s=e.target.closest('.swatch'); if(!s)return;
  S().appearance.accent=s.dataset.accent; save(); applyAppearance(); flashSaved();
});
$('#accentCustom').addEventListener('input',e=>{
  S().appearance.accent=e.target.value; save(); applyAppearance();
});

/* ============================================================
   PLUGIN SYSTEM
   ============================================================ */
const PluginBus={
  instances:[],   // смонтированные: {def,api,id}
  /* Полный пересбор: размонтируем всё и заново монтируем активные.
     Используется при загрузке, смене профиля и массовых операциях. */
  init(){
    this.instances.slice().forEach(i=>this.unmount(i.def.id));
    (S().plugins.defs||[]).forEach(def=>{ if(def.enabled!==false) this.mount(def) });
    this._ready=true;
    try{ this.emit('ready',{}) }catch(e){}
  },
  /* Монтирование одного плагина. Возвращает id или null при ошибке.
     Код выполняется в new Function — песочницы нет, это осознанный
     выбор: плагины пишет локальный пользователь и агент в своей папке. */
  mount(def){
    if(!def||!def.code)return null;
    const timers=[],dispose=[];
    const api={
      _hooks:{},_cmds:[],
      _timers:timers,_dispose:dispose,
      on(evt,cb){ if(typeof cb!=='function')return; (this._hooks[evt]=this._hooks[evt]||[]).push(cb) },
      onDispose(cb){ if(typeof cb==='function') dispose.push(cb) },
      registerCommand(c){ if(c&&c.name&&typeof c.run==='function') this._cmds.push(c) },
      toast:(m,t)=>toast(m,t),
      storeGet(k,d){ def.store=def.store||{}; return def.store[k]!==undefined?def.store[k]:d },
      storeSet(k,v){ def.store=def.store||{}; def.store[k]=v; save() },
      session:()=>active(),
      /* Таймеры плагина: при размонтировании (перезагрузка/удаление файла)
         снимаются автоматически — иначе старый плагин жил бы вечно.
         Храним функции очистки; сработавший setTimeout убирает свою запись. */
      setInterval:(fn,ms)=>{ const t=setInterval(fn,ms); timers.push(()=>clearInterval(t)); return t },
      setTimeout:(fn,ms)=>{
        const clean=()=>{ const i=timers.indexOf(clean); if(i>=0) timers.splice(i,1) };
        const t=setTimeout(()=>{ clean(); fn() },ms);
        timers.push(()=>{ clearTimeout(t); clean() });
        return t;
      },
      /* Метаданные плагина и путь к папке — полезно для логов и подсказок */
      name:def.name, version:def.version, file:def.file||null,
      dir:()=>S().plugins.dir||'',
      reload:()=>{ if(def.source==='file'&&def.file) PluginFS.reloadOne(def.file) },
      log:(...a)=>console.log('[плагин '+(def.name||def.file)+']',...a)
    };
    try{
      new Function('api','"use strict";\n'+def.code)(api);
      // Повторный mount того же id: сначала корректно размонтируем старый,
      // иначе его таймеры остались бы жить (простое filter их терял).
      this.unmount(def.id);
      this.instances.push({def,api,id:def.id});
      def.error=null;
      // Плагин, добавленный ПОСЛЕ готовности (файловый при sync или
      // горячая перезагрузка), тоже должен получить 'ready' — иначе
      // api.on('ready') в нём никогда не сработает.
      if(this._ready){
        const hooks=(api._hooks.ready||[]).slice();
        for(const h of hooks){ try{ h({}) }catch(e){ console.warn('Плагин «'+def.name+'» (ready):',e) } }
      }
      return def.id;
    }catch(e){
      console.warn('Плагин «'+(def.name||def.file)+'»:',e);
      def.error=String((e&&e.message)||e);
      toast('Ошибка в плагине «'+(def.name||def.file)+'»','err');
      return null;
    }
  },
  /* Размонтирование: снимаем таймеры, зовём dispose-хук, чистим хуки/команды.
     Без этого горячая перезагрузка копила бы старые обработчики. */
  unmount(id){
    const i=this.instances.findIndex(x=>x.def.id===id);
    if(i<0)return false;
    const inst=this.instances[i];
    this.instances.splice(i,1);
    try{ (inst.api._hooks.dispose||[]).forEach(f=>{ try{ f() }catch(e){} }) }catch(e){}
    try{ inst.api._timers.forEach(clear=>{ try{ clear() }catch(e){} }) }catch(e){}
    inst.api._hooks={}; inst.api._cmds=[];
    return true;
  },
  byId(id){ return this.instances.find(x=>x.def.id===id)||null },
  isOn(id){ return !!this.byId(id) },
  /* Команды всех активных плагинов. Дубли имён схлопываются:
     последний зарегистрировавший побеждает (порядок = порядок в defs). */
  commands(){
    const map=new Map();
    this.instances.forEach(p=>p.api._cmds.forEach(c=>{
      if(c&&c.name) map.set(c.name,{...c,plugin:p.def.name,pluginId:p.def.id});
    }));
    return [...map.values()];
  },
  emit(evt,payload){
    let out=payload;
    for(const p of this.instances.slice()){
      for(const h of (p.api._hooks[evt]||[])){
        try{ const r=h(out); if(r!==undefined&&r!==null) out=r }catch(e){ console.warn('Hook '+evt+' плагина «'+p.def.name+'»:',e) }
      }
    }
    return out;
  }
};

/* ============================================================
   ФАЙЛОВЫЕ ПЛАГИНЫ (папка plugins на диске)

   Источник правды — папка. Агент (Hermes fs tools) кладёт туда .js,
   фронтенд следит за ней через SSE и подхватывает изменения БЕЗ
   перезагрузки страницы: новый файл → монтирование (или вопрос
   пользователю в ручном режиме), изменённый → горячая перезагрузка,
   удалённый → размонтирование.

   Через file:// серверные /api/plugins/* недоступны — наблюдение
   просто не стартует, остальное приложение работает как раньше.
   ============================================================ */
const PluginFS={
  es:null, pollTimer:null, onLine:false, busy:false, _askRetry:null,
  askAsk:null, askQueue:[],

  api(p){ return fetch(p,{headers:{'Content-Type':'application/json'}}) },
  post(p,body){
    return fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body||{})});
  },

  /* Старт наблюдения. SSE — основной канал; если он не поднялся,
     включаем опрос /list раз в 3 с. */
  async start(){
    if(IS_FILE_PROTOCOL) return;
    this.stop();
    try{
      const r=await this.api('/api/plugins/dir');
      if(!r.ok) throw new Error('HTTP '+r.status);
      const d=await r.json();
      S().plugins.dir=d.dir||'';
      S().plugins.watching=true;
    }catch(e){
      console.warn('Плагины с диска недоступны:',e&&e.message?e.message:e);
      S().plugins.watching=false;
      renderPluginsSettings();
      return;
    }
    await this.sync({initial:true});
    try{
      const es=new EventSource('/api/plugins/watch');
      this.es=es;
      es.onopen=()=>{ this.onLine=true; S().plugins.watching=true; renderPluginsSettings() };
      es.onmessage=ev=>{
        let m=null; try{ m=JSON.parse(ev.data) }catch(e){ return }
        if(m&&m.type==='changed') this.sync();
      };
      es.onerror=()=>{                       // SSE отвалился — уходим на poll
        this.onLine=false; S().plugins.watching=false;
        try{ es.close() }catch(e){}
        this.es=null;
        this.ensurePoll();
        renderPluginsSettings();
      };
    }catch(e){
      this.ensurePoll();
    }
    renderPluginsSettings();
  },

  ensurePoll(){
    if(this.pollTimer||IS_FILE_PROTOCOL) return;
    this.pollTimer=setInterval(()=>this.sync(),3000);
  },
  stop(){
    if(this.es){ try{ this.es.close() }catch(e){} this.es=null }
    if(this.pollTimer){ clearInterval(this.pollTimer); this.pollTimer=null }
    if(this._askRetry){ clearTimeout(this._askRetry); this._askRetry=null }
    this.onLine=false;
  },
  ensurePollIfNeeded(){
    // Если SSE жив — опрос не нужен; если упал — держим poll
    if(!this.es&&!this.pollTimer&&!IS_FILE_PROTOCOL) this.ensurePoll();
  },

  findByFile(name){ return (S().plugins.defs||[]).find(d=>d.file===name) },
  findById(id){ return (S().plugins.defs||[]).find(d=>d.id===id) },

  /* Пересоздать def для файла: метаданные из заголовка, если есть */
  makeDef(f){
    const dec={id:uid('pl'),source:'file',file:f.name,code:'',hash:'',
      enabled:false,store:{},mtime:f.mtime,size:f.size};
    Object.assign(dec,parsePluginHeader(f.code||'',f.name));
    return dec;
  },

  /* Трёхсторонняя синхронизация папки и state.defs.
     initial:true — первый прогон (загрузка страницы): накопленные
     ранее файлы подключаем без вопросов, иначе при каждом старте
     приложение спрашивало бы про одни и те же файлы. */
  async sync(opt={}){
    if(this.busy) return;
    this.busy=true;
    try{
      const r=await this.api('/api/plugins/list?with=1');
      if(!r.ok) throw new Error('HTTP '+r.status);
      const d=await r.json();
      S().plugins.dir=d.dir||S().plugins.dir;
      const files=d.files||[], defs=S().plugins.defs||(S().plugins.defs=[]);
      const byFile={}; defs.forEach(p=>{ if(p.file) byFile[p.file]=p });
      const onDisk={}; files.forEach(f=>onDisk[f.name]=f);

      // 1) Изменённые и новые файлы
      for(const f of files){
        const known=byFile[f.name];
        if(known&&known.hash===f.hash){
          /* Файл не менялся — метаданные трогать не нужно, но подключить его
             всё равно надо. Раньше здесь стоял continue, и из-за этого после
             перезагрузки страницы ВКЛЮЧЁННЫЕ плагины не стартовали: sync
             проходил мимо них, а браузер и так не помнит, что их включали. */
          if(opt.initial&&known.enabled!==false) PluginBus.mount(known);
          continue;
        }
        const wasEnabled=known?known.enabled!==false:false;
        const dec=known||this.makeDef(f);
        dec.code=f.code; dec.hash=f.hash; dec.mtime=f.mtime; dec.size=f.size;
        Object.assign(dec,parsePluginHeader(f.code,f.name));
        if(known) defs[defs.indexOf(known)]=dec; else defs.push(dec);
        if(known&&wasEnabled){
          // Файл обновили — горячая перезагрузка без перезагрузки страницы
          PluginBus.unmount(dec.id);
          PluginBus.mount(dec);
          if(!opt.initial) toast('Плагин «'+dec.name+'» перезагружен','ok');
        }else if(known){
          if(!opt.initial) toast('Плагин «'+dec.name+'» обновлён (выключен)','info');
        }else if(opt.initial){
          // Старый файл при загрузке: решение пользователя уже сохранено в defs
          if(dec.enabled!==false) PluginBus.mount(dec);
        }else if(S().plugins.mode==='auto'){
          dec.enabled=true; PluginBus.mount(dec);
          toast('Новый плагин «'+dec.name+'» подключён','ok');
        }else{
          this.askActivate(dec);      // ручной режим — спросим пользователя
        }
      }
      // 2) Удалённые файлы
      for(const d of defs.filter(x=>x.file&&!onDisk[x.file]).slice()){
        PluginBus.unmount(d.id);
        const i=S().plugins.defs.indexOf(d);
        if(i>=0) S().plugins.defs.splice(i,1);
        if(!opt.initial) toast('Плагин «'+d.name+'» удалён с диска','info');
      }
      save();
      if(ui.setOpen) renderPluginsSettings();
    }catch(e){
      console.warn('Синхронизация плагинов не удалась:',e&&e.message?e.message:e);
      S().plugins.watching=false;
    }finally{
      this.busy=false;
      this.ensurePollIfNeeded();
    }
  },

  /* Принудительное перечитывание одного файла (кнопка «Перезагрузить») */
  async reloadOne(name){
    try{
      const r=await this.api('/api/plugins/read?name='+encodeURIComponent(name));
      if(!r.ok) throw new Error('HTTP '+r.status);
      const d=await r.json();
      const dec=this.findByFile(name);
      if(!dec) return this.sync();
      const wasOn=PluginBus.isOn(dec.id);
      dec.code=d.code; dec.hash=d.hash;
      Object.assign(dec,parsePluginHeader(d.code,name));
      if(wasOn){ PluginBus.unmount(dec.id); PluginBus.mount(dec) }
      save(); renderPluginsSettings();
      toast('«'+dec.name+'» перечитан','ok');
    }catch(e){ toast('Не удалось перечитать: '+(e&&e.message||e),'err') }
  },

  /* Запись плагина на диск (создание/правка из UI) */
  async writeFile(name,code){
    try{
      const r=await this.post('/api/plugins/write',{name,code});
      const d=await r.json().catch(()=>({}));
      if(!r.ok){ toast('Запись не удалась: '+(d.error||r.status),'err'); return false }
      await this.sync();
      return true;
    }catch(e){ toast('Запись не удалась: '+(e&&e.message||e),'err'); return false }
  },
  async deleteFile(name){
    try{
      const r=await this.post('/api/plugins/delete',{name});
      const d=await r.json().catch(()=>({}));
      if(!r.ok){ toast('Удаление не удалось: '+(d.error||r.status),'err'); return false }
      const dec=this.findByFile(name);
      if(dec){ PluginBus.unmount(dec.id);
        const i=S().plugins.defs.indexOf(dec); if(i>=0) S().plugins.defs.splice(i,1);
        save(); renderPluginsSettings() }
      return true;
    }catch(e){ toast('Удаление не удалось: '+(e&&e.message||e),'err'); return false }
  },
  async openDir(){
    try{
      const r=await this.post('/api/plugins/open',{});
      if(!r.ok){ const d=await r.json().catch(()=>({})); toast(d.dir||'Не удалось открыть папку','err') }
    }catch(e){ toast('Открытие папки недоступно','err') }
  },

  /* Ручной режим: окно «активировать новый плагин?».
     Очередь — чтобы несколько новых файлов не наезжали друг на друга. */
  askActivate(def){
    this.askQueue.push(def);
    this.pumpAsk();
  },
  pumpAsk(){
    if(this.askAsk||!this.askQueue.length) return;
    // Не перебиваем другие модальные окна (форма, PIN) — ждём и повторяем
    const fo=$('#formOverlay'), po=$('#pinOverlay');
    const busy=(fo&&fo.classList.contains('open'))||(po&&po.classList.contains('open'));
    if(busy){ this._askRetry=setTimeout(()=>this.pumpAsk(),1200); return }
    const ov=$('#plugAsk');
    const def=this.askQueue[0];
    if(!ov){                     // оверлей не вставлен (правки HTML) — просто молча включим
      this.askQueue.shift();
      def.enabled=true; PluginBus.mount(def); save(); renderPluginsSettings();
      return;
    }
    this.askAsk=def;
    const n=$('#plugAskName'), pv=$('#plugAskPrev'), cnt=$('#plugAskCount');
    if(n) n.textContent=def.name;
    if(pv) pv.textContent=(def.code||'').split('\n').slice(0,12).join('\n');
    if(cnt) cnt.textContent=this.askQueue.length>1?('и ещё '+(this.askQueue.length-1)):'';
    ov.classList.add('open');
  },
  answerAsk(yes){
    const ov=$('#plugAsk'); if(ov) ov.classList.remove('open');
    const def=this.askAsk; this.askAsk=null;
    if(this._askRetry){ clearTimeout(this._askRetry); this._askRetry=null }
    if(def){
      if(yes){
        def.enabled=true; def.declined=false;
        PluginBus.mount(def);
        toast('Плагин «'+def.name+'» активирован','ok');
      }else{
        // Отказ запоминаем: повторно про этот файл не спрашиваем
        def.enabled=false; def.declined=true;
        toast('Плагин «'+def.name+'» отключён','info');
      }
      save(); renderPluginsSettings();
    }
    this.askQueue.shift();
    this.pumpAsk();
  }
};

/* Метаданные из необязательного заголовка файла:
     // @name: Счётчик слов
     // @desc: Показывает длину ответа агента
     // @version: 1.0
     // @author: local
   Без заголовка имя берётся из имени файла, автор — 'local'. */
function parsePluginHeader(code,filename){
  const out={name:String(filename||'').replace(/\.js$/i,''),desc:'',author:'local',version:'1.0'};
  // Заголовок читаем только из ведущих строк-комментариев и только до первого
  // блока кода. Иначе «// @name:» внутри логики плагина (например, в строке
  // с примером или в шаблонной литерале) подменял бы название файла.
  const head=[];
  for(const line of String(code||'').split('\n')){
    const s=line.trim();
    if(!s) continue;                       // пустые строки пропускаем
    if(s.startsWith('//')){ head.push(s); continue }
    if(s.startsWith('/*')||s.startsWith('*')){ head.push(s); continue }
    break;                                  // начался код — заголовок кончился
  }
  const text=head.join('\n');
  const grab=k=>{ const m=text.match(new RegExp('@'+k+'\\s*:\\s*(.+)','i')); return m?m[1].trim():'' };
  const nm=grab('name'); if(nm) out.name=nm;
  const de=grab('desc')||grab('description'); if(de) out.desc=de;
  const au=grab('author'); if(au) out.author=au;
  const ve=grab('version'); if(ve) out.version=ve;
  return out;
}

function renderPluginsSettings(){
  const list=$('#pluginList'); if(!list)return;
  const P=S().plugins||{};
  const defs=P.defs||[];

  /* Блок папки: путь, режим активации, состояние наблюдателя */
  const dirBox=$('#plugDirBox');
  if(dirBox){
    const dir=P.dir||'';
    dirBox.querySelector('#plugDirPath').textContent=dir||'папка недоступна (нужен запуск через веб-сервер)';
    dirBox.querySelector('#plugDirPath').title=dir;
    const w=$('#plugWatchPill');
    if(w){
      const on=!!P.watching;
      w.className='badge '+(on?'done':'warn');
      w.innerHTML='<i class="fa-solid '+(on?'fa-bolt':'fa-plug-circle-xmark')+'"></i> '+
                  (on?'наблюдение работает':'наблюдение недоступно');
    }
  }
  const seg=$('#plugModeSeg');
  if(seg) $$('button',seg).forEach(b=>b.classList.toggle('on',b.dataset.val===String(P.mode||'auto')));

  /* Совпадение с поиском: по названию, описанию, автору и имени файла. */
  const q=String(P.query||'').trim().toLowerCase();
  const hit=t=>!q||[t.name,t.desc,t.author,t.file].some(x=>
    String(x||'').toLowerCase().indexOf(q)>=0);
  /* Переключатель вида: 'all' показывает и .js, и .py, остальные — по одному.
     Фильтруем здесь же, чтобы и карточки, и счётчики считали одно и то же. */
  const view=String(P.view||'all');
  const jsDefs=(view==='all'||view==='js')?defs.filter(hit):[];
  const showPy=(view==='all'||view==='py');

  /* Карточки плагинов: название, описание, версия, автор, статус, действия */
  list.innerHTML=jsDefs.length?jsDefs.map(p=>{
    const on=PluginBus.isOn(p.id);
    const fromFile=p.source==='file';
    // Число команд берём из живого экземпляра: в def оно не хранится,
    // а список команд собирается при монтировании.
    const inst=on?PluginBus.byId(p.id):null;
    const ncmds=inst?inst.api._cmds.length:0;
    const status=on?'<span class="badge done"><i class="fa-solid fa-bolt"></i> активен</span>'
      :p.error?'<span class="badge error"><i class="fa-solid fa-triangle-exclamation"></i> ошибка</span>'
      :p.declined?'<span class="badge wait"><i class="fa-solid fa-hand"></i> отклонён</span>'
      :'<span class="badge wait"><i class="fa-solid fa-pause"></i> выключен</span>';
    return '<div class="plugcard'+(on?'':' off')+(p.error?' err':'')+'" data-plug="'+p.id+'">'+
      '<div class="pc-top">'+
        '<span class="pi"><i class="fa-solid '+(on?'fa-puzzle-piece':'fa-plug')+'"></i></span>'+
        '<div class="pt"><b>'+esc(p.name||'Без имени')+'</b>'+
          '<small>'+esc(p.desc||'Без описания')+'</small></div>'+
        status+
      '</div>'+
      '<div class="pc-meta">'+
        '<span class="ver">v'+esc(p.version||'1.0')+'</span>'+
        '<span class="chip"><i class="fa-solid fa-user"></i>'+esc(p.author||'local')+'</span>'+
        '<span class="chip"><i class="fa-solid '+(fromFile?'fa-file-code':'fa-puzzle-piece')+'"></i>'+
          esc(fromFile?p.file:'встроенный')+'</span>'+
        (ncmds?'<span class="chip"><i class="fa-solid fa-terminal"></i>'+
          ncmds+' '+esc(ncmds===1?'команда':'команды')+'</span>':'')+
      '</div>'+
      (p.error?'<div class="pc-err">'+esc(p.error)+'</div>':'')+
      '<div class="pc-act">'+
        '<div class="toggle'+(on?' on':'')+'" data-ptoggle="'+p.id+'" title="'+(on?'Отключить':'Включить')+'"></div>'+
        '<button class="icon-btn sm" data-pedit="'+p.id+'" title="Редактировать код"><i class="fa-solid fa-pen"></i></button>'+
        (fromFile?'<button class="icon-btn sm" data-preload="'+p.id+'" title="Перечитать файл с диска"><i class="fa-solid fa-rotate"></i></button>':'')+
        '<button class="icon-btn sm" data-pdel="'+p.id+'" title="'+(fromFile?'Удалить файл':'Удалить плагин')+'">'+
          '<i class="fa-regular fa-trash-can"></i></button>'+
      '</div></div>';
  }).join('')
   :(defs.length?
     '<div class="empty" style="padding:26px"><i class="fa-solid fa-magnifying-glass"></i>'+
      '<p>Ничего не найдено по запросу «${esc(q)}»</p></div>'
     :'<div class="empty" style="padding:26px"><i class="fa-solid fa-plug"></i>'+
      '<p>Плагинов нет. Добавьте примеры, создайте свой или попросите агента положить .js в папку плагинов.</p></div>');

  /* Python-блок живёт отдельным списком: переключатель вида должен убирать
     его целиком, а не оставлять пустую рамку. */
  const pyBox=$('#pyPluginGroup');
  if(pyBox){
    pyBox.style.display=showPy?'':'none';
    if(showPy&&typeof renderPyPlugins==='function')renderPyPlugins();
  }
  const segv=$('#plugViewSeg');
  if(segv) $$('button',segv).forEach(b=>b.classList.toggle('on',b.dataset.val===view));
  const srch=$('#plugSearch');
  if(srch&&srch.value!==(P.query||''))srch.value=P.query||'';
  const cnt=$('#plugCount');
  if(cnt)cnt.textContent=jsDefs.length+
    ((showPy&&typeof PYP!=='undefined'&&PYP.list.length)?' + '+PYP.list.length:'');

  list.onclick=async e=>{
    const tg=e.target.closest('[data-ptoggle]');
    if(tg){
      const p=PluginFS.findById(tg.dataset.ptoggle);
      if(p){
        const willOn=!PluginBus.isOn(p.id);
        p.enabled=willOn; p.declined=!willOn;
        if(willOn) PluginBus.mount(p); else PluginBus.unmount(p.id);
        save(); renderPluginsSettings();
        toast(willOn?'Плагин включён':'Плагин выключен',willOn?'ok':'info');
      }
      return;
    }
    const rl=e.target.closest('[data-preload]');
    if(rl){
      const p=PluginFS.findById(rl.dataset.preload);
      if(p&&p.file) await PluginFS.reloadOne(p.file);
      return;
    }
    const del=e.target.closest('[data-pdel]');
    if(del){
      const p=PluginFS.findById(del.dataset.pdel);
      if(!p) return;
      const fromFile=p.source==='file';
      const v=await askForm({title:'Удалить плагин «'+p.name+'»?',icon:'fa-plug',ok:'Удалить',
        fields:[{name:'c',label:fromFile?
          'Будет удалён файл '+p.file+' из папки плагинов. Введите название для подтверждения':
          'Введите название для подтверждения',value:p.name}]});
      if(!v) return;
      PluginBus.unmount(p.id);
      const i=(S().plugins.defs||[]).indexOf(p);
      if(i>=0) S().plugins.defs.splice(i,1);
      save();
      if(fromFile&&p.file) await PluginFS.deleteFile(p.file);   // сносим и файл
      renderPluginsSettings();
      toast('Плагин удалён','ok');
      return;
    }
    const ed=e.target.closest('[data-pedit]');
    if(ed){ editPlugin(ed.dataset.pedit) }
  };
}
async function editPlugin(id){
  const p=id?PluginFS.findById(id):null;
  const v=await askForm({title:p?'Редактировать плагин':'Новый плагин',icon:'fa-puzzle-piece',ok:'Сохранить',fields:[
    {name:'name',label:'Название',value:p?p.name:'',ph:'Мой плагин'},
    {name:'desc',label:'Описание',value:p?p.desc:'',ph:'Что делает'},
    {name:'author',label:'Автор',value:p?p.author:'',ph:'local'},
    {name:'version',label:'Версия',value:p?p.version:'1.0'},
    {name:'code',label:'Код (JavaScript)',type:'area',value:p?p.code:'',ph:"api.on('reply', ctx => { ... });"}
  ]});
  if(!v||!v.name)return;
  const defs=S().plugins.defs||(S().plugins.defs=[]);

  /* Правка файлового плагина: пишем .js на диск (тогда папка остаётся
     источником правды, и агент сможет править тот же файл). */
  if(p&&p.source==='file'){
    const header='// @name: '+v.name+'\n// @desc: '+(v.desc||'')+'\n'+
                 '// @version: '+(v.version||'1.0')+'\n// @author: '+(v.author||'local')+'\n\n';
    p.name=v.name; p.desc=v.desc||''; p.author=v.author||'local'; p.version=v.version||'1.0';
    p.enabled=p.enabled!==false;
    if(p.enabled) PluginBus.unmount(p.id);
    const ok=await PluginFS.writeFile(p.file,header+(v.code||''));
    if(!ok){                                    // не записался — вернуть как было
      if(p.enabled) PluginBus.mount(p);
      return;
    }
    p.code=header+(v.code||'');
    if(p.enabled) PluginBus.mount(p);
    save(); renderPluginsSettings(); flashSaved();
    toast('Файл '+p.file+' сохранён','ok');
    return;
  }

  if(p){ p.name=v.name; p.desc=v.desc||''; p.author=v.author||'local'; p.version=v.version||'1.0'; p.code=v.code||''; p.enabled=p.enabled!==false; }
  else defs.push({id:uid('pl'),source:'local',name:v.name,desc:v.desc||'',author:v.author||'local',
    version:v.version||'1.0',code:v.code||'',enabled:true,store:{}});
  save(); PluginBus.init(); renderPluginsSettings(); flashSaved(); toast('Плагин сохранён','ok');
}

/* Безопасное имя файла из названия плагина */
function pluginFileName(name,prefix){
  let s=String(name||'').trim().toLowerCase()
    .replace(/[^a-zа-яё0-9]+/gi,'-').replace(/^-+|-+$/g,'');
  if(!s) s=(prefix||'plugin')+'-'+Math.random().toString(36).slice(2,6);
  return s+'.js';
}
$('#pluginNew').onclick=()=>editPlugin(null);
$('#pluginOpenDir').onclick=()=>PluginFS.openDir();
/* Переключатель вида и поиск. Состояние лежит в settings.plugins, поэтому
   выбор и строка запроса переживают перезагрузку страницы. */
const _viewSeg=$('#plugViewSeg');
if(_viewSeg)_viewSeg.onclick=e=>{
  const b=e.target.closest('[data-val]'); if(!b)return;
  S().plugins.view=b.dataset.val||'all'; save(); renderPluginsSettings();
};
let _searchTimer=null;
const _search=$('#plugSearch');
if(_search)_search.oninput=e=>{
  const v=e.target.value;
  clearTimeout(_searchTimer);
  _searchTimer=setTimeout(()=>{ S().plugins.query=v; save(); renderPluginsSettings() },160);
};
/* Смена папки плагинов. Системный диалог надёжнее ручного ввода:
   пользователю не нужно знать формат пути. Ручной ввод оставлен на
   случай копирования пути откуда-то ещё. */
$('#plugDirPick')&&($('#plugDirPick').onclick=async()=>{
  const cur=S().plugins.dir||'';
  const v=await askForm({title:'Папка плагинов',icon:'fa-folder-open',ok:'Применить',fields:[
    {name:'dir',label:'Путь к папке',value:cur,ph:'C:\\\\infinity\\\\plugins'},
    {name:'c',label:'Введите путь ещё раз для подтверждения',value:cur}]});
  if(!v)return;
  const dir=String(v.dir||'').trim();
  if(!dir){ toast('Папка не указана','err'); return }
  if(String(v.c||'').trim()!==dir){ toast('Пути не совпадают — не применено','err'); return }
  await applyPluginDir(dir);
});
$('#plugDirBrowse')&&($('#plugDirBrowse').onclick=async()=>{
  if(IS_FILE_PROTOCOL){ toast('Нужен запуск через веб-сервер','err'); return }
  try{
    const r=await API.req('GET','/api/plugins/dir-pick');
    if(r&&r.pick){
      const p=String(r.pick||'').trim();
      if(p&&p!==r.dir) await applyPluginDir(p);
      else toast('Папка не выбрана','info');
    } else toast('Выбор папки недоступен на этой системе','err');
  }catch(e){ toast('Не удалось выбрать папку: '+errText(e&&e.message||e),'err') }
});
async function applyPluginDir(dir){
  try{
    const r=await API.req('POST','/api/plugins/dir',{dir});
    S().plugins.dir=r.dir||dir;
    /* Смена папки меняет весь набор плагинов: сбрасываем кэш и перечитываем. */
    S().plugins.defs=[];
    save();
    await PluginFS.sync();
    if(typeof loadPyPlugins==='function'){ PYP.loaded=false; await loadPyPlugins() }
    renderPluginsSettings();
    toast('Папка плагинов: '+r.dir,'ok');
  }catch(e){ toast('Папка не применена: '+errText(e&&e.message||e),'err') }
}
$('#pluginReadme').onclick=async()=>{
  // Показываем README прямо в превью: не заставляем пользователя искать файл.
  if(IS_FILE_PROTOCOL){ toast('Нужен запуск через веб-сервер','err'); return }
  try{
    const r=await fetch('/api/plugins/readme');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    openPreview({filename:d.name,lang:'text',content:d.code});
  }catch(e){ toast('Не удалось прочитать документацию: '+(e&&e.message||e),'err') }
};
$('#pluginRescan').onclick=()=>{ PluginFS.sync(); toast('Папка пересканирована','info') };
$('#pluginSample').onclick=async()=>{
  /* Примеры кладём настоящими .js в папку — заодно показываем агенту,
     как выглядит плагин этого приложения. */
  const samples=[
    {name:'Счётчик слов',desc:'Показывает длину ответа агента',code:
"// @name: Счётчик слов\n// @desc: Показывает длину ответа агента\n// @version: 1.0\n// @author: sample\n\n"+
"api.on('reply', function(ctx){\n"+
"  var msgs=(ctx.session&&ctx.session.messages)||[];\n"+
"  var last=msgs.filter(function(m){return m.role==='agent'&&m.kind==='text'}).pop();\n"+
"  if(last){ api.toast('Ответ: '+String(last.content).split(/\\s+/).filter(Boolean).length+' слов'); }\n"+
"});"},
    {name:'Быстрые команды',desc:'/hi и /time — примеры команд плагина',code:
"// @name: Быстрые команды\n// @desc: /hi и /time — примеры команд плагина\n// @version: 1.0\n// @author: sample\n\n"+
"api.registerCommand({name:'/hi',desc:'проверка плагинов',run:function(){\n"+
"  return {reply:'Плагины работают! Время: '+new Date().toLocaleTimeString('ru-RU')};\n"+
"}});\napi.registerCommand({name:'/time',desc:'спросить время у агента',run:function(){\n"+
"  return 'Который сейчас час? Ответь одной короткой строкой.';\n"+
"}});"}
  ];
  let done=0;
  for(const s of samples){
    if(await PluginFS.writeFile(pluginFileName(s.name,'sample'),s.code)) done++;
  }
  if(!done) return;
  // sync() в ручном режиме сам выставит окна подтверждения на каждый новый файл
  await PluginFS.sync();
  renderPluginsSettings();
  toast('Примеры добавлены в папку плагинов','ok');
};
