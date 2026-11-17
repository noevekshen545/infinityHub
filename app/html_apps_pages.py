#!/usr/bin/env python3
"""HTML Viewer, Apps и страница выбора для Infinity Search."""

PAGE_HTML_LANDING = r"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HTML — Infinity</title>
<link rel="stylesheet" href="/static/css/font-awesome.min.css">
<style>
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--border:#30363d;--text:#c9d1d9;--text2:#8b949e;--text3:#484f58;--accent:#58a6ff;--accent2:#1f6feb;--green:#238636;--purple:#8b5cf6}
body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);margin:0;min-height:100vh}
.header{background:var(--bg2);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:12px}
.header a{color:var(--accent);text-decoration:none;font-size:14px}
.header h1{font-size:18px;flex:1}
.hero{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:calc(100vh - 60px);padding:40px 20px}
.hero h2{font-size:32px;margin-bottom:8px;background:linear-gradient(135deg,var(--accent),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero p{color:var(--text2);font-size:16px;margin-bottom:40px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px;max-width:700px;width:100%}
.card{background:linear-gradient(145deg,var(--bg2),var(--bg3));border:1px solid var(--border);border-radius:20px;padding:32px;cursor:pointer;transition:all .3s;text-decoration:none;color:inherit;text-align:center}
.card:hover{border-color:var(--accent);transform:translateY(-4px);box-shadow:0 12px 32px rgba(88,166,255,.15)}
.card .icon{font-size:48px;margin-bottom:16px;display:block}
.card .title{font-size:20px;font-weight:700;margin-bottom:8px;display:block}
.card .desc{font-size:14px;color:var(--text2);display:block}
</style><script src="/static/js/i18n.js"></script>
</head><body>
<div class="header">
  <a href="/">← Главная</a>
  <h1>🌐 HTML</h1>
</div>
<div class="hero">
  <h2>HTML раздел</h2>
  <p>Выберите, что хотите открыть</p>
  <div class="cards">
    <a class="card" href="/html/viewer">
      <span class="icon">📄</span>
      <span class="title">HTML Viewer</span>
      <span class="desc">Просмотр и поиск сохранённых HTML страниц</span>
    </a>
    <a class="card" href="/apps">
      <span class="icon">🚀</span>
      <span class="title">Apps Store</span>
      <span class="desc">HTML приложения с запуском в iframe</span>
    </a>
  </div>
</div>
</body></html>"""

PAGE_HTML_VIEWER = r"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HTML Viewer</title>
<link rel="stylesheet" href="/static/css/font-awesome.min.css">
<style>
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--border:#30363d;--text:#c9d1d9;--text2:#8b949e;--text3:#484f58;--accent:#58a6ff;--accent2:#1f6feb;--green:#238636;--red:#da3633}
body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);margin:0;min-height:100vh}
.header{background:var(--bg2);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:12px}
.header a{color:var(--accent);text-decoration:none;font-size:14px}
.header h1{font-size:18px;flex:1}
.wrap{max-width:1200px;margin:0 auto;padding:20px}
.search-bar{display:flex;gap:8px;margin-bottom:20px}
.search-bar input{flex:1;padding:10px 16px;background:var(--bg2);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:15px;outline:none}
.search-bar input:focus{border-color:var(--accent)}
.search-bar button{padding:10px 20px;background:var(--accent2);border:none;border-radius:10px;color:#fff;cursor:pointer;font-size:14px}
.file-list{display:grid;gap:8px}
.file-item{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px;cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:12px}
.file-item:hover{border-color:var(--accent);background:var(--bg3)}
.file-item .icon{font-size:24px}
.file-item .info{flex:1}
.file-item .title{font-weight:600;font-size:14px}
.file-item .desc{font-size:12px;color:var(--text2)}
.file-item .meta{font-size:11px;color:var(--text3)}
.viewer{position:fixed;top:0;left:0;right:0;bottom:0;background:var(--bg);z-index:100;display:none;flex-direction:column}
.viewer.open{display:flex}
.viewer-bar{display:flex;align-items:center;gap:12px;padding:8px 16px;background:var(--bg2);border-bottom:1px solid var(--border)}
.viewer-bar button{padding:6px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text);cursor:pointer}
.viewer-bar .title{flex:1;font-size:14px}
.viewer iframe{flex:1;border:none;width:100%}
.empty{text-align:center;padding:40px;color:var(--text3)}
</style><script src="/static/js/i18n.js"></script>
</head><body>
<div class="header">
  <a href="/html">← HTML раздел</a>
  <h1>📄 HTML Viewer</h1>
</div>
<div class="wrap">
  <div class="search-bar">
    <input type="text" id="searchInput" placeholder="Поиск по HTML страницам..." onkeydown="if(event.key==='Enter')searchHtml()">
    <button onclick="searchHtml()"><i class="fas fa-search"></i> Найти</button>
  </div>
  <div class="file-list" id="fileList"><div class="empty">Загрузка...</div></div>
</div>
<div class="viewer" id="viewer">
  <div class="viewer-bar">
    <button onclick="closeViewer()"><i class="fas fa-arrow-left"></i> Назад</button>
    <span class="title" id="viewerTitle"></span>
    <button onclick="openInNewTab()"><i class="fas fa-external-link-alt"></i></button>
  </div>
  <iframe id="viewerFrame" sandbox="allow-scripts allow-same-origin allow-forms"></iframe>
</div>
<script>
let htmlFiles=[];
async function loadHtml(){
  try{
    const r=await fetch('/api/html');
    const d=await r.json();
    htmlFiles=d.results||[];
    renderList(htmlFiles);
  }catch(e){document.getElementById('fileList').innerHTML='<div class="empty">Ошибка загрузки</div>'}
}
function renderList(files){
  if(!files.length){document.getElementById('fileList').innerHTML='<div class="empty">Нет HTML файлов. Укажите папки в настройках.</div>';return}
  let h='';
  files.forEach(f=>{
    h+=`<div class="file-item" onclick="viewFile('${f.path.replace(/'/g,"\\'")}')">
      <div class="icon">🌐</div>
      <div class="info">
        <div class="title">${esc(f.title)}</div>
        <div class="desc">${esc(f.description||f.rel_path)}</div>
      </div>
      <div class="meta">${formatSize(f.size)}</div>
    </div>`;
  });
  document.getElementById('fileList').innerHTML=h;
}
async function searchHtml(){
  const q=document.getElementById('searchInput').value.trim();
  if(!q){renderList(htmlFiles);return}
  try{
    const r=await fetch('/api/html/search?q='+encodeURIComponent(q));
    const d=await r.json();
    renderList(d.results||[]);
  }catch(e){alert('Ошибка поиска')}
}
async function viewFile(path){
  try{
    const r=await fetch('/api/html/view?f='+encodeURIComponent(path));
    const d=await r.json();
    if(d.ok){
      document.getElementById('viewerTitle').textContent=d.title;
      const frame=document.getElementById('viewerFrame');
      frame.srcdoc=d.content;
      document.getElementById('viewer').classList.add('open');
    }else{alert('Ошибка: '+d.error)}
  }catch(e){alert('Ошибка загрузки')}
}
function closeViewer(){document.getElementById('viewer').classList.remove('open');document.getElementById('viewerFrame').srcdoc=''}
function openInNewTab(){
  const frame=document.getElementById('viewerFrame');
  const blob=new Blob([frame.srcdoc],{type:'text/html'});
  window.open(URL.createObjectURL(blob));
}
function esc(t){if(!t)return'';const d=document.createElement('div');d.textContent=t;return d.innerHTML}
function formatSize(b){if(!b)return'';return b<1024?b+'B':b<1048576?(b/1024).toFixed(0)+'KB':(b/1048576).toFixed(1)+'MB'}
loadHtml();
</script></body></html>"""

PAGE_APPS = r"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Apps Store</title>
<link rel="stylesheet" href="/static/css/font-awesome.min.css">
<style>
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--border:#30363d;--text:#c9d1d9;--text2:#8b949e;--text3:#484f58;--accent:#58a6ff;--accent2:#1f6feb;--green:#238636;--red:#da3633;--purple:#8b5cf6}
body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);margin:0;min-height:100vh}
.header{background:var(--bg2);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:12px}
.header a{color:var(--accent);text-decoration:none;font-size:14px}
.header h1{font-size:18px;flex:1}
.wrap{max-width:1200px;margin:0 auto;padding:20px}
.app-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}
.app-card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px;cursor:pointer;transition:all .2s;text-align:center}
.app-card:hover{border-color:var(--accent);transform:translateY(-2px)}
.app-card .icon{width:64px;height:64px;margin:0 auto 12px;background:var(--bg3);border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:28px}
.app-card .name{font-weight:600;font-size:14px;margin-bottom:4px}
.app-card .desc{font-size:12px;color:var(--text2);margin-bottom:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.app-card .meta{font-size:11px;color:var(--text3)}
.detail{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:var(--bg);z-index:100;overflow-y:auto}
.detail.open{display:block}
.detail-bar{display:flex;align-items:center;gap:12px;padding:8px 16px;background:var(--bg2);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:10}
.detail-bar button{padding:6px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text);cursor:pointer}
.detail-bar .title{flex:1;font-size:14px}
.detail-body{max-width:800px;margin:0 auto;padding:20px}
.app-header{display:flex;gap:16px;margin-bottom:20px}
.app-header .icon{width:80px;height:80px;background:var(--bg3);border-radius:20px;display:flex;align-items:center;justify-content:center;font-size:36px;flex-shrink:0}
.app-header .info{flex:1}
.app-header .name{font-size:20px;font-weight:700}
.app-header .desc{color:var(--text2);margin:8px 0}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}
.btn{padding:8px 16px;border:1px solid var(--border);border-radius:8px;cursor:pointer;font-size:13px;background:var(--bg3);color:var(--text);transition:all .2s}
.btn:hover{border-color:var(--accent)}
.btn-primary{background:var(--accent2);border-color:var(--accent2);color:#fff}
.btn-green{background:var(--green);border-color:var(--green);color:#fff}
.btn-red{background:var(--red);border-color:var(--red);color:#fff}
.port-input{display:flex;gap:8px;align-items:center;margin:12px 0}
.port-input input{width:100px;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:14px}
.status{padding:8px 12px;border-radius:8px;margin:8px 0;font-size:13px}
.status.running{background:rgba(35,134,54,.2);border:1px solid var(--green);color:var(--green)}
.status.stopped{background:rgba(218,54,51,.2);border:1px solid var(--red);color:var(--red)}
.comments{margin-top:20px;padding-top:20px;border-top:1px solid var(--border)}
.comment{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:8px}
.comment textarea{width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;resize:vertical;min-height:60px;font-family:inherit}
.iframe-container{margin-top:20px;border:1px solid var(--border);border-radius:12px;overflow:hidden}
.iframe-container iframe{width:100%;height:500px;border:none}
.empty{text-align:center;padding:40px;color:var(--text3)}
</style><script src="/static/js/i18n.js"></script>
</head><body>
<div class="header">
  <a href="/html">← HTML раздел</a>
  <h1>🚀 Apps Store</h1>
  <a href="/api/extensions/builder-page" style="margin-left:auto;display:inline-flex;align-items:center;gap:5px;padding:6px 14px;border-radius:8px;font-size:12px;font-weight:600;text-decoration:none;background:linear-gradient(135deg,#6366f1,#a855f7);color:#fff"><i class="fas fa-magic"></i> Создать расширение</a>
</div>
<div class="wrap">
  <div class="app-grid" id="appGrid"><div class="empty">Загрузка...</div></div>
</div>
<div class="detail" id="detail">
  <div class="detail-bar">
    <button onclick="closeDetail()"><i class="fas fa-arrow-left"></i> Назад</button>
    <span class="title" id="detailTitle"></span>
  </div>
  <div class="detail-body" id="detailBody"></div>
</div>
<script>
let apps=[];
async function loadApps(){
  try{
    const r=await fetch('/api/apps');
    const d=await r.json();
    apps=d||[];
    renderGrid(apps);
  }catch(e){document.getElementById('appGrid').innerHTML='<div class="empty">Ошибка загрузки. Укажите папки в настройках.</div>'}
}
function renderGrid(apps){
  if(!apps.length){document.getElementById('appGrid').innerHTML='<div class="empty">Нет приложений. Создайте папку с index.html в директории приложений.</div>';return}
  let h='';
  apps.forEach(a=>{
    h+=`<div class="app-card" onclick="openDetail('${a.id.replace(/'/g,"\\'")}')">
      <div class="icon">${a.icon?'<img src="/api/apps/icon?f='+encodeURIComponent(a.dir)+'&i='+encodeURIComponent(a.icon)+'" style="width:100%;height:100%;object-fit:cover;border-radius:16px">':'🚀'}</div>
      <div class="name">${esc(a.name)}</div>
      <div class="desc">${esc(a.description||'')}</div>
      <div class="meta">v${a.version} · ${formatSize(a.size)}</div>
    </div>`;
  });
  document.getElementById('appGrid').innerHTML=h;
}
async function openDetail(id){
  const app=apps.find(a=>a.id===id);
  if(!app)return;
  document.getElementById('detailTitle').textContent=app.name;
  const body=document.getElementById('detailBody');
  body.innerHTML=`
    <div class="app-header">
      <div class="icon">${app.icon?'<img src="/api/apps/icon?f='+encodeURIComponent(app.dir)+'&i='+encodeURIComponent(app.icon)+'" style="width:100%;height:100%;object-fit:cover;border-radius:20px">':'🚀'}</div>
      <div class="info">
        <div class="name">${esc(app.name)}</div>
        <div class="desc">${esc(app.description||'')}</div>
        <div class="meta">Версия: ${app.version} · Размер: ${formatSize(app.size)}</div>
      </div>
    </div>
    <div class="actions">
      <button class="btn btn-primary" onclick="launchApp('${app.dir.replace(/'/g,"\\'")}')"><i class="fas fa-play"></i> Запустить</button>
      <button class="btn btn-green" id="serverBtn" onclick="toggleServer('${app.dir.replace(/'/g,"\\'")}')"><i class="fas fa-server"></i> Запустить сервер</button>
    </div>
    <div class="port-input">
      <label>Порт:</label>
      <input type="number" id="serverPort" value="8888" min="1024" max="65535">
      <span id="serverStatus" class="status stopped">Сервер не запущен</span>
    </div>
    <div id="serverStatusMsg"></div>
    <div class="iframe-container" id="appFrame" style="display:none"><iframe id="appIframe" sandbox="allow-scripts allow-same-origin allow-forms allow-modals"></iframe></div>
    <div class="comments">
      <h3><i class="fas fa-comments"></i> Комментарии</h3>
      <div id="commentsList"></div>
      <div style="margin-top:12px">
        <textarea id="commentInput" placeholder="Добавить комментарий..."></textarea>
        <button class="btn" style="margin-top:8px" onclick="addComment('${app.dir.replace(/'/g,"\\'")}')"><i class="fas fa-paper-plane"></i> Отправить</button>
      </div>
    </div>`;
  document.getElementById('detail').classList.add('open');
  loadComments(app.dir);
}
function closeDetail(){
  document.getElementById('detail').classList.remove('open');
  document.getElementById('appFrame').style.display='none';
}
function launchApp(dir){
  const frame=document.getElementById('appIframe');
  frame.src='/api/apps/serve?f='+encodeURIComponent(dir);
  document.getElementById('appFrame').style.display='block';
  document.getElementById('appFrame').scrollIntoView({behavior:'smooth'});
}
async function toggleServer(dir){
  const port=document.getElementById('serverPort').value;
  const status=document.getElementById('serverStatus');
  const btn=document.getElementById('serverBtn');
  try{
    if(btn.dataset.running==='true'){
      const r=await fetch('/api/apps/server/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir,port:parseInt(port)})});
      const d=await r.json();
      if(d.ok){status.textContent='Сервер остановлен';status.className='status stopped';btn.dataset.running='false';btn.innerHTML='<i class="fas fa-server"></i> Запустить сервер'}
    }else{
      const r=await fetch('/api/apps/server/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir,port:parseInt(port)})});
      const d=await r.json();
      if(d.ok){status.textContent='Сервер запущен на порту '+port;status.className='status running';btn.dataset.running='true';btn.innerHTML='<i class="fas fa-stop"></i> Остановить сервер'}
      else{status.textContent='Ошибка: '+(d.error||'');status.className='status stopped'}
    }
  }catch(e){status.textContent='Ошибка сети';status.className='status stopped'}
}
function loadComments(dir){
  const key='app_comments_'+dir;
  const comments=JSON.parse(localStorage.getItem(key)||'[]');
  let h='';
  comments.forEach((c,i)=>{h+=`<div class="comment"><div style="font-size:12px;color:var(--text3);margin-bottom:4px">${c.time}</div><div>${esc(c.text)}</div></div>`});
  document.getElementById('commentsList').innerHTML=h||'<div style="color:var(--text3);font-size:13px">Нет комментариев</div>';
}
function addComment(dir){
  const input=document.getElementById('commentInput');
  if(!input.value.trim())return;
  const key='app_comments_'+dir;
  const comments=JSON.parse(localStorage.getItem(key)||'[]');
  comments.unshift({text:input.value.trim(),time:new Date().toLocaleString()});
  localStorage.setItem(key,JSON.stringify(comments));
  input.value='';
  loadComments(dir);
}
function esc(t){if(!t)return'';const d=document.createElement('div');d.textContent=t;return d.innerHTML}
function formatSize(b){if(!b)return'';return b<1024?b+'B':b<1048576?(b/1024).toFixed(0)+'KB':(b/1048576).toFixed(1)+'MB'}
loadApps();
</script></body></html>"""
