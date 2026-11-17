#!/usr/bin/env python3
"""App Detail Page — красивая страница приложения как в современных App Store."""

PAGE_APP_DETAIL = r"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{app_name}} — Infinity App Store</title>
<link rel="stylesheet" href="/static/css/font-awesome.min.css">
<style>
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--border:#30363d;--text:#c9d1d9;--text2:#8b949e;--text3:#484f58;--accent:#58a6ff;--accent2:#1f6feb;--green:#238636;--red:#da3633;--orange:#d29922;--purple:#8b5cf6}
body.light{--bg:#fff;--bg2:#f6f8fa;--bg3:#eaeef2;--border:#d0d7de;--text:#1f2328;--text2:#656d76;--text3:#8b949e;--accent:#0969da;--accent2:#0550ae;--green:#1a7f37;--red:#cf222e;--orange:#9a6700;--purple:#7c3aed}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.header{background:var(--bg2);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:100;backdrop-filter:blur(10px)}
body.light .header{background:rgba(246,248,250,.9)}
.header a{color:var(--accent);text-decoration:none;font-size:14px}
.header h1{font-size:16px;flex:1}
.hero{position:relative;padding:40px 20px;text-align:center;overflow:hidden}
.hero-bg{position:absolute;top:0;left:0;right:0;bottom:0;background:linear-gradient(135deg,var(--accent),var(--purple));opacity:.3;background-size:cover;background-position:center}
.hero-content{position:relative;z-index:1;max-width:600px;margin:0 auto}
.app-icon{width:100px;height:100px;border-radius:20px;background:var(--bg3);margin:0 auto 16px;display:flex;align-items:center;justify-content:center;font-size:48px;color:var(--accent);box-shadow:0 8px 24px rgba(0,0,0,.2);overflow:hidden}
.app-icon img{width:100%;height:100%;object-fit:cover}
.app-name{font-size:28px;font-weight:700;margin-bottom:4px}
.app-dev{font-size:14px;color:var(--text2);margin-bottom:16px}
.app-stats{display:flex;justify-content:center;gap:24px;margin-bottom:20px}
.app-stat{text-align:center}
.app-stat .val{font-size:20px;font-weight:700}
.app-stat .label{font-size:11px;color:var(--text3);text-transform:uppercase}
.app-actions{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
.app-actions button{padding:10px 24px;border-radius:20px;border:1px solid var(--border);background:var(--bg3);color:var(--text);cursor:pointer;font-size:14px;font-weight:600;transition:all .2s;display:flex;align-items:center;gap:6px}
.app-actions .install{background:var(--accent);border-color:var(--accent);color:#fff}
.app-actions .install:hover{background:var(--accent2)}
.app-actions .edit-btn:hover{background:var(--green);border-color:var(--green);color:#fff}
.app-actions .del-btn:hover{background:var(--red);border-color:var(--red);color:#fff}
.app-actions .installed{background:var(--bg);color:var(--text2);cursor:default}
.screenshots{padding:20px;overflow-x:auto;display:flex;gap:12px}
.screenshots::-webkit-scrollbar{height:4px}
.screenshots::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.screenshot{width:200px;height:140px;border-radius:8px;background:var(--bg3);flex-shrink:0;overflow:hidden;border:1px solid var(--border)}
.screenshot img{width:100%;height:100%;object-fit:cover}
.section{padding:20px;border-top:1px solid var(--border)}
.section h2{font-size:20px;font-weight:700;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.section p{font-size:14px;line-height:1.7;color:var(--text2)}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.tag{padding:4px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:16px;font-size:12px;color:var(--text2)}
.info-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px;margin-top:12px}
.info-item{background:var(--bg3);border-radius:8px;padding:12px;text-align:center}
.info-item .val{font-size:16px;font-weight:600;margin-bottom:2px}
.info-item .label{font-size:11px;color:var(--text3)}
.similar-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-top:12px}
.similar-card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px;cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:12px}
.similar-card:hover{border-color:var(--accent);transform:translateY(-2px)}
.similar-card img{width:48px;height:48px;border-radius:10px;object-fit:cover}
.similar-card .sc-info{flex:1;min-width:0}
.similar-card .sc-name{font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.similar-card .sc-desc{font-size:11px;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style>
<script src="/static/js/i18n.js"></script>
</head><body>
<div class="header">
  <a href="/apps"><i class="fas fa-arrow-left"></i> Назад</a>
  <h1>App Store</h1>
  <button onclick="window.location.href='/desktop'" style="padding:6px 12px;background:var(--accent);border:none;border-radius:4px;color:#fff;cursor:pointer;font-size:12px"><i class="fas fa-desktop"></i> Desktop</button>
</div>
<div class="hero">
  <div class="hero-bg"></div>
  <div class="hero-content">
    <div class="app-icon" id="appIcon"></div>
    <div class="app-name" id="appName"></div>
    <div class="app-dev" id="appDev"></div>
    <div class="app-stats">
      <div class="app-stat"><div class="val" id="appVersion">1.0</div><div class="label">Версия</div></div>
      <div class="app-stat"><div class="val" id="appSize">-</div><div class="label">Размер</div></div>
      <div class="app-stat"><div class="val" id="appType">-</div><div class="label">Тип</div></div>
    </div>
    <div class="app-actions" id="appActions"></div>
  </div>
</div>
<div class="screenshots" id="screenshots"></div>
<div class="section">
  <h2><i class="fas fa-info-circle"></i> Описание</h2>
  <p id="appDesc"></p>
  <div class="tags" id="appTags"></div>
</div>
<div class="section">
  <h2><i class="fas fa-cog"></i> Информация</h2>
  <div class="info-grid" id="appInfo"></div>
</div>
<div class="section" id="depsSection" style="display:none">
  <h2><i class="fas fa-boxes"></i> Зависимости</h2>
  <p style="font-size:13px;color:var(--text2)">Загрузка информации...</p>
</div>
<div class="section" id="similarSection" style="display:none">
  <h2><i class="fas fa-th"></i> Похожие приложения</h2>
  <div class="similar-grid" id="similarApps"></div>
</div>

<script>
const appId='{{app_id}}';
let appData=null;
let allApps=[];
let installedApps=[];
try{installedApps=JSON.parse(localStorage.getItem('desktop_apps')||'[]')}catch(e){}

async function loadApp(){
  try{
    const r=await fetch('/api/apps/detail?id='+appId);
    appData=await r.json();
    if(appData.error){document.body.innerHTML='<div style="text-align:center;padding:60px">'+appData.error+'</div>';return}
    renderApp();
  }catch(e){console.error(e)}
}

async function loadAllApps(){
  try{
    const r=await fetch('/api/apps');
    allApps=await r.json();
    renderSimilar();
    renderApp();   // каталог нужен, чтобы понять, защищённое ли приложение
  }catch(e){}
}

function renderApp(){
  if(!appData)return;
  
  // Icon
  const icon=document.getElementById('appIcon');
  icon.innerHTML=`<img src="/api/apps/icon?id=${appId}" onerror="this.style.display='none';this.parentElement.textContent='${(appData.name||'?')[0]}'">`;

  // Hero background: cover-обложка приложения (если задана в manifest.json)
  const heroBg=document.querySelector('.hero-bg');
  const coverUrl=(appData.cover&&typeof appData.cover==='string'&&(appData.cover.startsWith('http')||appData.cover.startsWith('/')))?appData.cover:'';
  if(heroBg&&coverUrl){
    heroBg.style.backgroundImage=`linear-gradient(180deg,rgba(13,17,23,.15),rgba(13,17,23,.55)),url('${coverUrl.replace(/'/g,'%27')}')`;
    heroBg.style.backgroundSize='cover';
    heroBg.style.backgroundPosition='center';
    heroBg.style.backgroundRepeat='no-repeat';
    heroBg.style.opacity='.95';
  }

  // Name & dev
  document.getElementById('appName').textContent=appData.name||appId;
  document.getElementById('appDev').textContent='Infinity App Store';
  
  // Stats
  document.getElementById('appVersion').textContent=appData.version||'1.0';
  document.getElementById('appSize').textContent=fmtSize(appData.size||0);
  document.getElementById('appType').textContent=appData.type==='app'?'Приложение':appData.type==='bg'?'Фоновый':'Виджет';
  
  // Actions
  const actions=document.getElementById('appActions');
  const installed=installedApps.some(a=>a.id===appId);
  const hasDeps = appData.hasRequirements && appData.dependencies && appData.dependencies.length;
  const depsReady = appData.hasVenv;
  
  let installBtn;
  if(installed){
    installBtn=`<button class="installed" disabled><i class="fas fa-check"></i> Установлено</button>`;
  }else if(hasDeps && !depsReady){
    installBtn=`<button class="install" onclick="installApp()"><i class="fas fa-download"></i> Установить</button>`;
  }else{
    installBtn=`<button class="install" onclick="installApp()"><i class="fas fa-download"></i> Установить</button>`;
  }
  actions.innerHTML=`
    ${installBtn}
    <button onclick="window.location.href='/apps/edit?id=${appId}'" class="edit-btn"><i class="fas fa-pen"></i> Редактировать</button>
    <button onclick="openInDesktop()" style="background:var(--purple);border-color:var(--purple);color:#fff"><i class="fas fa-desktop"></i> Открыть</button>
    ${isProtectedApp()?'<button onclick="removeFromMenu()" class="edit-btn" title="Убрать из меню приложений"><i class="fas fa-eye-slash"></i></button>':'<button onclick="deleteApp()" class="del-btn"><i class="fas fa-trash"></i></button>'}
  `;
  
  // Screenshots
  const ss=document.getElementById('screenshots');
  if(appData.screenshots&&appData.screenshots.length){
    ss.innerHTML=appData.screenshots.map(s=>`<div class="screenshot"><img src="/apps/screenshot?id=${appId}&name=${s}" onerror="this.parentElement.style.display='none'"></div>`).join('');
  }else{
    ss.style.display='none';
  }
  
  // Description
  document.getElementById('appDesc').textContent=appData.description||'Описание отсутствует';
  
  // Tags
  const tags=appData.tags||(appData.category?[appData.category]:[]);
  const tagsEl=document.getElementById('appTags');
  if(tags.length){
    tagsEl.innerHTML=tags.map(t=>`<span class="tag">${t}</span>`).join('');
  }else{
    tagsEl.style.display='none';
  }
  
  // Info grid
  let depsStatus = '';
  if(hasDeps){
    depsStatus = depsReady
      ? `<div class="info-item"><div class="val" style="color:var(--green)">✓</div><div class="label">Зависимости</div></div>`
      : `<div class="info-item"><div class="val" style="color:var(--orange)">⚠</div><div class="label">Требуются зависимости</div></div>`;
  }
  document.getElementById('appInfo').innerHTML=`
    <div class="info-item"><div class="val">${appData.version||'1.0'}</div><div class="label">Версия</div></div>
    <div class="info-item"><div class="val">${fmtSize(appData.size||0)}</div><div class="label">Размер</div></div>
    <div class="info-item"><div class="val">${appData.type||'app'}</div><div class="label">Тип</div></div>
    <div class="info-item"><div class="val">${appData.windowWidth||800}×${appData.windowHeight||600}</div><div class="label">Окно</div></div>
    ${depsStatus}
  `;
  
  document.title=(appData.name||appId)+' — Infinity App Store';
  
  // Show deps section if python app with requirements
  if(appData.type==='python' && hasDeps){
    showDepsSection();
  }
}

function showDepsSection(){
  const depsEl=document.getElementById('depsSection');
  if(!depsEl)return;
  const deps=appData.dependencies||[];
  const installed=installedApps.some(a=>a.id===appId);
  const depsReady = appData.hasVenv;
  depsEl.style.display='block';
  depsEl.innerHTML=`
    <h2><i class="fas fa-boxes"></i> Зависимости</h2>
    <div style="margin-bottom:12px">
      ${depsReady
        ? `<span style="color:var(--green);font-size:13px"><i class="fas fa-check-circle"></i> Виртуальное окружение готово</span>`
        : `<span style="color:var(--orange);font-size:13px"><i class="fas fa-exclamation-triangle"></i> Требуется установка зависимостей</span>`
      }
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">
      ${deps.map(d=>`<span class="tag">${escHtml(d)}</span>`).join('')}
    </div>
    ${!depsReady && installed ? `<button class="install" onclick="installDeps()"><i class="fas fa-cog"></i> Установить зависимости</button>` : ''}
    ${!depsReady && !installed ? `<p style="font-size:12px;color:var(--text2)">Зависимости будут установлены при установке приложения</p>` : ''}
  `;
}

function renderSimilar(){
  if(!appData)return;
  const appTags=appData.tags||(appData.category?[appData.category]:[]);
  const similar=allApps.filter(a=>{
    if(a.id===appId)return false;
    const aTags=a.tags||(a.category?[a.category]:[]);
    return aTags.some(t=>appTags.includes(t));
  }).slice(0,6);
  
  if(!similar.length)return;
  document.getElementById('similarSection').style.display='block';
  document.getElementById('similarApps').innerHTML=similar.map(a=>`
    <div class="similar-card" onclick="window.location.href='/apps/detail?id=${a.id}'">
      <img src="/api/apps/icon?id=${a.id}" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 48 48%22><rect fill=%22%2358a6ff%22 width=%2248%22 height=%2248%22 rx=%2210%22/><text x=%2224%22 y=%2232%22 text-anchor=%22middle%22 fill=%22white%22 font-size=%2220%22>${a.name?a.name[0]:'?'}</text></svg>'">
      <div class="sc-info">
        <div class="sc-name">${a.name||a.id}</div>
        <div class="sc-desc">${a.description||''}</div>
      </div>
    </div>
  `).join('');
}

function showDepsModal(message){
  let m=document.getElementById('depsModal');
  if(!m){
    m=document.createElement('div');
    m.id='depsModal';
    m.style.cssText='position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);z-index:100000;display:flex;align-items:center;justify-content:center';
    m.innerHTML='<div style="background:var(--bg2);border:1px solid var(--border);border-radius:16px;padding:24px;width:500px;max-width:90vw;max-height:80vh;overflow-y:auto;box-shadow:0 24px 64px rgba(0,0,0,.4)"><div id="depsModalContent"></div></div>';
    document.body.appendChild(m);
  }
  document.getElementById('depsModalContent').innerHTML=message;
  m.style.display='flex';
}

function hideDepsModal(){
  const m=document.getElementById('depsModal');
  if(m)m.style.display='none';
}

async function installDeps(){
  showDepsModal(`<div style="text-align:center">
    <div style="font-size:40px;margin-bottom:16px"><i class="fas fa-cog fa-spin" style="color:var(--accent)"></i></div>
    <h3 style="margin-bottom:8px;color:var(--text)">Установка зависимостей...</h3>
    <p style="font-size:13px;color:var(--text2);margin-bottom:16px">Создание виртуального окружения и установка пакетов</p>
    <pre id="depsLog" style="background:#000;color:#c9d1d9;padding:12px;border-radius:8px;font-size:12px;text-align:left;max-height:300px;overflow-y:auto;white-space:pre-wrap;word-break:break-all"></pre>
  </div>`);
  try{
    const r=await fetch('/api/apps/install-deps',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:appId})});
    const result=await r.json();
    const logEl=document.getElementById('depsLog');
    if(logEl)logEl.textContent=result.output||'';
    if(result.ok){
      appData.hasVenv=true;
      setTimeout(()=>{
        hideDepsModal();
        renderApp();
      },1000);
    }else{
      showDepsModal(`<div style="text-align:center">
        <div style="font-size:40px;margin-bottom:16px"><i class="fas fa-exclamation-triangle" style="color:var(--red)"></i></div>
        <h3 style="margin-bottom:8px;color:var(--text)">Ошибка установки</h3>
        <pre style="background:#000;color:#c9d1d9;padding:12px;border-radius:8px;font-size:12px;text-align:left;max-height:300px;overflow-y:auto;white-space:pre-wrap">${escHtml(result.output||result.error||'')}</pre>
        <button class="install" onclick="hideDepsModal()" style="margin-top:16px"><i class="fas fa-times"></i> Закрыть</button>
      </div>`);
    }
  }catch(e){
    showDepsModal(`<div style="text-align:center">
      <div style="font-size:40px;margin-bottom:16px"><i class="fas fa-exclamation-triangle" style="color:var(--red)"></i></div>
      <h3 style="margin-bottom:8px;color:var(--text)">Ошибка: ${escHtml(e.message)}</h3>
      <button class="install" onclick="hideDepsModal()" style="margin-top:16px"><i class="fas fa-times"></i> Закрыть</button>
    </div>`);
  }
}

async function installApp(){
  if(!appData||installedApps.some(a=>a.id===appId))return;
  
  const hasDeps=appData.hasRequirements && appData.dependencies && appData.dependencies.length;
  const depsReady=appData.hasVenv;
  
  // If deps needed but not installed, offer to install them
  if(hasDeps && !depsReady){
    const depsList=appData.dependencies.map(d=>'<span class="tag">'+escHtml(d)+'</span>').join(' ');
    showDepsModal(`<div style="text-align:center">
      <div style="font-size:40px;margin-bottom:16px"><i class="fas fa-boxes" style="color:var(--orange)"></i></div>
      <h3 style="margin-bottom:8px;color:var(--text)">Требуются зависимости</h3>
      <p style="font-size:13px;color:var(--text2);margin-bottom:12px">Для работы приложения <strong>${escHtml(appData.name)}</strong> необходимы следующие пакеты:</p>
      <div style="margin-bottom:16px">${depsList}</div>
      <p style="font-size:13px;color:var(--text2);margin-bottom:16px">Установить их в изолированное виртуальное окружение?</p>
      <div style="display:flex;gap:8px;justify-content:center">
        <button class="install" onclick="installDepsAndApp()"><i class="fas fa-cog"></i> Установить и продолжить</button>
        <button onclick="installAppSkipDeps()" style="padding:10px 24px;border-radius:20px;border:1px solid var(--border);background:var(--bg3);color:var(--text);cursor:pointer;font-size:14px"><i class="fas fa-forward"></i> Пропустить</button>
        <button onclick="hideDepsModal()" style="padding:10px 24px;border-radius:20px;border:1px solid var(--border);background:var(--bg3);color:var(--text);cursor:pointer;font-size:14px"><i class="fas fa-times"></i> Отмена</button>
      </div>
    </div>`);
    return;
  }
  
  completeInstall();
}

async function installDepsAndApp(){
  await installDeps();
  if(appData.hasVenv){
    completeInstall();
  }
}

function installAppSkipDeps(){
  hideDepsModal();
  completeInstall();
}

function completeInstall(){
  try{ // установка снова возвращает приложение в меню
    const hidden=new Set(JSON.parse(localStorage.getItem('desktop_menu_hidden')||'[]'));
    hidden.delete(appId);
    localStorage.setItem('desktop_menu_hidden',JSON.stringify([...hidden]));
  }catch(e){}
  installedApps.push({id:appId,name:appData.name,type:appData.type,background:appData.background,main:appData.main||'index.html',width:appData.windowWidth||800,height:appData.windowHeight||600});
  localStorage.setItem('desktop_apps',JSON.stringify(installedApps));
  renderApp();
  // Show success toast
  showDepsModal(`<div style="text-align:center">
    <div style="font-size:40px;margin-bottom:16px"><i class="fas fa-check-circle" style="color:var(--green)"></i></div>
    <h3 style="margin-bottom:8px;color:var(--text)">✓ Приложение установлено</h3>
    <p style="font-size:13px;color:var(--text2)">${escHtml(appData.name)} успешно добавлено на рабочий стол</p>
    <button class="install" onclick="hideDepsModal()" style="margin-top:16px"><i class="fas fa-check"></i> Готово</button>
  </div>`);
}

// Стандартные приложения и приложения из модуля Docker удалить нельзя —
// их можно только убрать из меню приложений.
function isProtectedApp(){
  const catalog=allApps.find(a=>a.id===appId);
  if(catalog&&catalog.is_default)return true;
  return String(appId||'').startsWith('docker_');
}
function removeFromMenu(){
  try{
    const hidden=new Set(JSON.parse(localStorage.getItem('desktop_menu_hidden')||'[]'));
    hidden.add(appId);
    localStorage.setItem('desktop_menu_hidden',JSON.stringify([...hidden]));
    installedApps=installedApps.filter(a=>a.id!==appId);
    localStorage.setItem('desktop_apps',JSON.stringify(installedApps));
  }catch(e){}
  alert('Приложение убрано из меню — в App Store оно осталось.');
  renderApp();
}

function deleteApp(){
  if(isProtectedApp()){
    alert('Это приложение нельзя удалить. Убрать его из меню приложений?');
    removeFromMenu();
    return;
  }
  if(!confirm('Удалить приложение?'))return;
  fetch('/api/apps/delete?id='+appId,{method:'POST'}).then(r=>r.json().then(d=>({ok:r.ok,d}))).then(({ok,d})=>{
    if(!ok||(d&&d.error)){alert('Не удалось удалить: '+((d&&d.error)||'ошибка'));return}
    installedApps=installedApps.filter(a=>a.id!==appId);
    localStorage.setItem('desktop_apps',JSON.stringify(installedApps));
    window.location.href='/apps';
  }).catch(()=>alert('Не удалось удалить приложение'));
}

function openInDesktop(){
  if(!installedApps.some(a=>a.id===appId)){
    installApp();
  }
  window.location.href='/desktop';
  setTimeout(()=>{
    try{window.parent.postMessage({action:'openApp',id:appId},'*')}catch(e){}
  },500);
}

function fmtSize(b){if(!b)return'0 B';const u=['B','KB','MB','GB'];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++}return b.toFixed(1)+' '+u[i]}

function escHtml(s){
  if(!s)return'';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

loadApp();
loadAllApps();
</script>
</body></html>"""
