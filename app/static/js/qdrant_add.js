(function(){
  try{
    var st=document.createElement('style');
    st.textContent='.qdrant-add-btn{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:6px;font-size:11px;cursor:pointer;opacity:.85;vertical-align:middle}.qdrant-add-btn:hover{opacity:1;border-color:var(--accent);color:var(--accent)}.qdrant-add-btn i{font-size:11px}.qdrant-add-btn.qdrant-added{border-color:#10b981;color:#10b981;opacity:.6;cursor:default}.qdrant-add-btn.qdrant-added:hover{opacity:.6;border-color:#10b981;color:#10b981}.qdrant-add-overlay{position:absolute;bottom:6px;left:6px;z-index:3;background:rgba(0,0,0,.55);color:#fff;border:1px solid rgba(255,255,255,.25);backdrop-filter:blur(2px)}.qdrant-add-overlay:hover{color:#fff;border-color:var(--accent)}.qdrant-add-overlay.qdrant-added{color:#34d399;border-color:rgba(52,211,153,.6)}';
    st.textContent+='.qdrant-scope-menu{position:fixed;z-index:100000;min-width:236px;padding:6px;border-radius:14px;background:var(--bg2,#fff);border:1px solid var(--border,#e5e9f2);box-shadow:0 18px 44px rgba(0,0,0,.22);font:inherit;font-size:13px;color:var(--text,#111);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}.qdrant-scope-menu .qdrant-scope-h{padding:7px 10px 5px;font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text3,#888)}.qdrant-scope-menu button{display:flex;align-items:center;gap:10px;width:100%;padding:9px 10px;border:0;border-radius:9px;background:transparent;color:inherit;font:inherit;font-size:13px;cursor:pointer;text-align:left}.qdrant-scope-menu button:hover{background:var(--hover,rgba(0,0,0,.05));color:var(--accent,#6366f1)}.qdrant-scope-menu button i{width:18px;text-align:center;font-size:13px;color:var(--accent,#6366f1)}.qdrant-scope-menu button span{flex:1}.qdrant-scope-menu button em{margin-left:auto;font-style:normal;font-size:10.5px;color:var(--text3,#888);background:color-mix(in srgb,var(--text) 8%,transparent);border-radius:999px;padding:1px 8px}';
    document.head.appendChild(st);
  }catch(e){}
})();

// Выбор «общая / личная» у кнопки добавления. Настройка профиля решает,
// какая база помечена «по умолчанию», но явный выбор важнее.
var _qdrantScopeInfo=null;
function qdrantScopeInfo(){
  if(_qdrantScopeInfo) return Promise.resolve(_qdrantScopeInfo);
  return fetch('/api/qdrant/scope',{cache:'no-store'})
    .then(function(r){return r.json();})
    .then(function(d){_qdrantScopeInfo=d||{};return _qdrantScopeInfo;})
    .catch(function(){return {scope:'shared'};});
}

function qdrantChooseScope(btn, cb){
  var old=document.getElementById('qdrantScopeMenu'); if(old) old.remove();
  /* Позицию запоминаем сразу: меню заметки закрывается на следующем тике,
     и к моменту ответа /api/qdrant/scope кнопки уже нет в DOM. */
  var anchor=null;
  try{
    if(btn&&btn.getBoundingClientRect){
      var r=btn.getBoundingClientRect();
      if(r.width||r.height){ anchor={left:r.left,right:r.right,top:r.top,bottom:r.bottom}; }
    }
  }catch(e){}
  if(!anchor && typeof event!=='undefined' && event && event.clientX!==undefined){
    anchor={left:event.clientX,right:event.clientX,top:event.clientY,bottom:event.clientY};
  }
  qdrantScopeInfo().then(function(info){
    var m=document.createElement('div');
    m.id='qdrantScopeMenu'; m.className='qdrant-scope-menu';
    m.innerHTML='<div class="qdrant-scope-h">Куда добавить</div>'
      +'<button type="button" data-scope="shared"><i class="fa-solid fa-globe"></i><span>Общая база</span><em>'+(info.scope==='shared'?'по умолчанию':'')+'</em></button>'
      +'<button type="button" data-scope="personal"><i class="fa-solid fa-user"></i><span>Личная база</span><em>'+(info.scope==='personal'?'по умолчанию':'')+'</em></button>';
    document.body.appendChild(m);
    var w=m.offsetWidth,h=m.offsetHeight;
    var left,top;
    if(anchor){
      /* Правый край меню совпадает с правым краем кнопки — так оно не
         уезжает от «⋯» и не вылезает за экран. */
      left=Math.min(window.innerWidth-w-10,Math.max(10,anchor.right-w));
      top=anchor.bottom+6;
      if(top+h>window.innerHeight-10) top=Math.max(10,anchor.top-h-6);
    } else {
      left=Math.max(10,(window.innerWidth-w)/2);
      top=Math.max(10,(window.innerHeight-h)/2);
    }
    m.style.left=left+'px'; m.style.top=top+'px';
    function close(){
      document.removeEventListener('mousedown', outside, true);
      document.removeEventListener('keydown', esc, true);
      if(m.parentNode) m.parentNode.removeChild(m);
    }
    function outside(e){ if(!m.contains(e.target)) close(); }
    function esc(e){ if(e.key==='Escape') close(); }
    setTimeout(function(){
      document.addEventListener('mousedown', outside, true);
      document.addEventListener('keydown', esc, true);
    },0);
    Array.prototype.forEach.call(m.querySelectorAll('button[data-scope]'),function(b){
      b.addEventListener('click',function(ev){
        ev.stopPropagation();
        var sc=b.getAttribute('data-scope');
        close();
        cb(sc);
      });
    });
  });
}

function qdrantGetBtn(){
  // event.target mozhet byt ikonka <i> vnutri knopki — podnimemsya do knopki
  var t = (typeof event !== 'undefined' && event && event.target) ? event.target : null;
  if(!t){ return null; }
  if(t.closest){
    return t.closest('.qdrant-add-btn') ||
           t.closest('[onclick*="addToQdrant("]') ||
           t.closest('[onclick*="addSourceToQdrant("]') || t;
  }
  return t;
}

function qdrantMarkAdded(btn){
  if(!btn || btn.getAttribute('data-qdrant-added') === '1'){ return; }
  btn.setAttribute('data-qdrant-added', '1');
  // sohranyaem originalnyy onclick i pereopredelyaem: povtornoe nazhatie
  // ne dubliruet zapis, tolko pokazyvaet uvedomlenie
  btn.setAttribute('data-qdrant-onclick', btn.getAttribute('onclick') || '');
  btn.removeAttribute('onclick');
  btn.onclick = function(ev){
    if(ev){ ev.stopPropagation(); }
    if(ev && ev.preventDefault){ ev.preventDefault(); }
    showQdrantToast('Этот элемент уже добавлен в векторную БД', 'ok');
  };
  btn.title = 'Уже в векторной БД';
  // dlya ikonochnyh knopok — menyem ikonku na galochku,
  // u knopok s tekstom dobavlyaem tekst «Добавлено»
  if((btn.textContent || '').trim() === ''){
    var icon = btn.querySelector('i');
    if(icon){ icon.className = 'fa-solid fa-check'; }
    else { btn.innerHTML = '<i class="fa-solid fa-check"></i>'; }
  } else {
    var ic = btn.querySelector('i');
    var txt = document.createElement('span');
    txt.textContent = ' Добавлено';
    btn.textContent = '';
    if(ic){ btn.appendChild(ic); }
    btn.appendChild(txt);
  }
  btn.style.opacity = '.55';
  btn.classList.add('qdrant-added');
}

function qdrantSend(url, payload, btn, onOk){
  var prev = btn ? btn.innerHTML : '';
  if(btn){ btn.disabled = true; btn.innerHTML = '...'; }
  fetch(url, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)
  })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d.ok){ onOk(d); }
      else { showQdrantToast('Ошибка: '+(d.error||'неизвестно'), 'err'); }
    })
    .catch(function(e){ showQdrantToast('Ошибка сети: '+e, 'err'); })
    .finally(function(){ if(btn){ btn.disabled=false; btn.innerHTML=prev; } });
}

function addToQdrant(source, id, title){
  if(!source || !id){ return; }
  var btn = qdrantGetBtn();
  qdrantChooseScope(btn, function(scope){
    qdrantSend('/api/qdrant/add', {source:source, id:id, title:title, scope:scope}, btn, function(d){
      var res = d.result || {};
      if(res.empty){
        showQdrantToast('Источник пуст — нечего индексировать', 'err');
        return;
      }
      var where = d.scope === 'personal' ? ' (личная база)' : ' (общая база)';
      showQdrantToast('Добавлено'+where+': '+(res.indexed_items||0)+' эл., '+(res.chunks||0)+' чанков', 'ok');
      if(btn){ qdrantMarkAdded(btn); }
    });
  });
}

function addSourceToQdrant(source, id, title){
  if(!source || !id){ return; }
  var btn = qdrantGetBtn();
  qdrantChooseScope(btn, function(scope){
    qdrantSend('/api/qdrant/add-source', {source:source, id:id, title:title, scope:scope}, btn, function(d){
      var where = d.scope === 'personal' ? 'личная база' : 'общая база';
      if(d.started){
        showQdrantToast('Векторная БД ('+where+'): индексация запущена, статей '+(d.total||0)+' (прогресс на /qdrant)', 'ok');
        if(btn){ qdrantMarkAdded(btn); }
      } else if(d.empty){
        showQdrantToast('Источник пуст или недоступен', 'err');
      } else {
        showQdrantToast('Готово ('+where+')', 'ok');
        if(btn){ qdrantMarkAdded(btn); }
      }
    });
  });
}

function showQdrantToast(text, kind){
  try{
    var t = document.createElement('div');
    t.textContent = text;
    t.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:99999;padding:10px 14px;border-radius:10px;font-size:13px;max-width:340px;color:#fff;background:'+(kind==='err'?'#ef4444':'#6366f1')+';box-shadow:0 6px 24px rgba(0,0,0,.3)';
    document.body.appendChild(t);
    setTimeout(function(){ if(t.parentNode) t.parentNode.removeChild(t); }, 3400);
  }catch(e){}
}

// ====== Статус «уже добавлено» на кнопках ======
(function(){
  var SRC_MAP = [
    {sel:'[onclick*="addToQdrant(\'article\'"]', source:'article'},
    {sel:'[onclick*="addToQdrant(\'book\'"]', source:'book'},
    {sel:'[onclick*="addToQdrant(\'html\'"]', source:'html'},
    {sel:'[onclick*="addToQdrant(\'news\'"]', source:'news'},
    {sel:'[onclick*="addToQdrant(\'note\'"]', source:'note'},
    {sel:'[onclick*="addToQdrant(\'zim\'"]', source:'zim'}
  ];

  function extractArgs(src){
    // Razbiraem argumenty vyzova addToQdrant(...): balans skobok, kavychki, zapятye
    var start = src.indexOf('addToQdrant(');
    if(start < 0) return null;
    var i = start + 'addToQdrant('.length;
    var depth = 1, args = [], cur = '', inQ = null;
    for(; i < src.length; i++){
      var ch = src.charAt(i);
      if(inQ){
        if(ch === '\\'){ cur += ch + (src.charAt(i+1) || ''); i++; continue; }
        if(ch === inQ){ inQ = null; cur += ch; continue; }
        cur += ch; continue;
      }
      if(ch === "'" || ch === '"'){ inQ = ch; cur += ch; continue; }
      if(ch === '('){ depth++; cur += ch; continue; }
      if(ch === ')'){ depth--; if(depth === 0){ args.push(cur); break; } cur += ch; continue; }
      if(ch === ',' && depth === 1){ args.push(cur); cur = ''; continue; }
      cur += ch;
    }
    return args.map(function(a){ return a.trim(); });
  }

  function extractId(btn){
    var src = btn.getAttribute('onclick') || '';
    var args = extractArgs(src);
    if(!args || args.length < 2) return '';
    var expr = args[1];
    // strokovyj literal bez vychisleniya
    var m = /^(['"])([\s\S]*)\1$/.exec(expr);
    if(m) return m[2];
    // dinamicheskoe vyrazhenie (window.location.href, entryUrl(...)) — vychislyaem
    try{ return String(Function('return (' + expr + ')')() || ''); }
    catch(e){ return ''; }
  }

  function markAdded(btn){
    // delegiruem obschey realizacii (ona zhe ispolzuetsya posle uspeshnogo add)
    qdrantMarkAdded(btn);
  }

  function checkPage(){
    try{
      // собираем кнопки по источникам
      var jobs = [];
      SRC_MAP.forEach(function(s){
        var btns = document.querySelectorAll(s.sel);
        if(!btns.length) return;
        var byId = {};
        btns.forEach(function(b){
          var id = extractId(b);
          if(!id) return;
          (byId[id] = byId[id] || []).push(b);
        });
        var ids = Object.keys(byId);
        if(!ids.length) return;
        jobs.push({source: s.source, byId: byId, ids: ids});
      });
      if(!jobs.length) return;
      // один запрос на источник
      jobs.forEach(function(j){
        fetch('/api/qdrant/added', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({source: j.source, ids: j.ids})
        }).then(function(r){ return r.json(); }).then(function(d){
          (d.added||[]).forEach(function(id){
            (j.byId[id]||[]).forEach(markAdded);
          });
        }).catch(function(){});
      });
    }catch(e){}
  }

  // первое сканирование + после динамических изменений
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', function(){ setTimeout(checkPage, 400); });
  } else {
    setTimeout(checkPage, 400);
  }
  var _lastCount = -1;
  setInterval(function(){
    try{
      var n = document.querySelectorAll('.qdrant-add-btn,[onclick*="addToQdrant("]').length;
      if(n !== _lastCount){ _lastCount = n; checkPage(); }
    }catch(e){}
  }, 2500);
})();

