/* WidgetManager — безрамочные виджеты на рабочем столе.
   Виджеты: авто-открытие при установке, перетаскивание, закрытие,
   сохранение позиции, авто-закрытие при удалении (через storage-событие). */

window.WidgetManager = (function () {
  var STORE = 'desktop_apps';
  var POS = 'widget_positions';
  var windows = {}; // id -> {el, app}

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function getInstalled() {
    try { return JSON.parse(localStorage.getItem(STORE) || '[]'); } catch (e) { return []; }
  }

  function getPos(id) {
    try { var p = JSON.parse(localStorage.getItem(POS) || '{}'); return p[id] || null; } catch (e) { return null; }
  }

  function setPos(id, x, y) {
    var p = {};
    try { p = JSON.parse(localStorage.getItem(POS) || '{}'); } catch (e) {}
    p[id] = { x: x, y: y };
    localStorage.setItem(POS, JSON.stringify(p));
  }

  function getContainer() {
    var c = document.getElementById('widgetContainer');
    if (!c) {
      c = document.createElement('div');
      c.id = 'widgetContainer';
      c.className = 'widget-overlay';
      document.body.appendChild(c);
    }
    return c;
  }

  function makeDraggable(el, handle, id) {
    var sx = 0, sy = 0, ox = 0, oy = 0, drag = false;
    handle.addEventListener('mousedown', function (e) {
      if (e.target && e.target.classList && e.target.classList.contains('wb-widget-x')) return;
      drag = true;
      sx = e.clientX; sy = e.clientY;
      var r = el.getBoundingClientRect();
      ox = r.left; oy = r.top;
      el.style.right = 'auto'; el.style.bottom = 'auto';
      e.preventDefault();
    });
    window.addEventListener('mousemove', function (e) {
      if (!drag) return;
      var nx = ox + (e.clientX - sx);
      var ny = oy + (e.clientY - sy);
      nx = Math.max(0, Math.min(nx, window.innerWidth - el.offsetWidth));
      ny = Math.max(0, Math.min(ny, window.innerHeight - el.offsetHeight));
      el.style.left = nx + 'px';
      el.style.top = ny + 'px';
    });
    window.addEventListener('mouseup', function () {
      if (!drag) return;
      drag = false;
      setPos(id, parseInt(el.style.left, 10) || 0, parseInt(el.style.top, 10) || 0);
    });
  }

  function open(app) {
    if (!app || !app.id) return null;
    if (windows[app.id]) return windows[app.id];
    var c = getContainer();
    var w = Math.max(160, parseInt(app.width, 10) || 320);
    var h = Math.max(120, parseInt(app.height, 10) || 200);
    var el = document.createElement('div');
    el.className = 'wb-widget';
    el.setAttribute('data-widget-id', app.id);
    el.style.cssText = 'position:fixed;width:' + w + 'px;height:' + h + 'px;z-index:86000;display:flex;flex-direction:column';
    var pos = getPos(app.id);
    if (pos) { el.style.left = pos.x + 'px'; el.style.top = pos.y + 'px'; }
    else { el.style.right = '20px'; el.style.bottom = '60px'; }

    var bar = document.createElement('div');
    bar.className = 'wb-widget-bar';
    bar.innerHTML = '<span class="wb-widget-title">' + escapeHtml(app.name || app.id) +
      '</span><button class="wb-widget-x" title="Закрыть" onclick="WidgetManager.close(\'' +
      escapeHtml(app.id) + '\')">✕</button>';

    var f = document.createElement('iframe');
    f.src = '/app-files/' + encodeURIComponent(app.id);
    f.className = 'wb-widget-frame';
    f.setAttribute('sandbox', 'allow-same-origin allow-scripts allow-forms allow-popups allow-modals');

    el.appendChild(bar);
    el.appendChild(f);
    makeDraggable(el, bar, app.id);
    c.appendChild(el);
    windows[app.id] = { el: el, app: app };
    return windows[app.id];
  }

  function close(id) {
    if (windows[id]) {
      windows[id].el.remove();
      delete windows[id];
    }
  }

  function toggle(app) {
    if (windows[app.id]) close(app.id);
    else open(app);
  }

  function restore() {
    var inst = getInstalled();
    inst.forEach(function (a) {
      if (a && (a.type === 'widget')) open(a);
    });
  }

  function reconcile() {
    var inst = getInstalled();
    var ids = {};
    inst.forEach(function (a) { if (a && a.type === 'widget') ids[a.id] = 1; });
    Object.keys(windows).forEach(function (id) {
      if (!ids[id]) close(id);
    });
  }

  function init() {
    restore();
    window.addEventListener('storage', function (e) {
      if (!e || e.key === STORE || e.key === null) reconcile();
    });
  }

  return { open: open, close: close, toggle: toggle, init: init, restore: restore, reconcile: reconcile };
})();
