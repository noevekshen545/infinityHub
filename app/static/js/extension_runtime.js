/* ExtensionRuntime — frontend API для расширений. */
(function(){
  if(window.ExtensionRuntime) return;
  var RT = {
    _log: function(level, msg){
      console[level]('[ExtRuntime] ' + msg);
    },
    log: function(msg){ this._log('log', msg); },
    warn: function(msg){ this._log('warn', msg); },
    error: function(msg){
      this._log('error', msg);
      this._showErrorBadge(msg);
      var now = Date.now();
      if(this._lastErrMsg === msg && this._lastErrTime && (now - this._lastErrTime) < 10000) return;
      this._lastErrMsg = msg;
      this._lastErrTime = now;
      this.showNotification('[JS] ' + msg, 'error');
    },
    _showErrorBadge: function(msg){
      var self = this;
      if(!this._errBadge){
        var b = document.createElement('div');
        b.id = 'ext-js-errors';
        b.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:99999;display:none;align-items:center;gap:8px;background:#dc2626;color:#fff;border-radius:8px;padding:8px 12px;font-size:12px;font-weight:600;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,0.35);max-width:320px';
        b.innerHTML = '<i class="fas fa-exclamation-triangle"></i><span></span>';
        b.addEventListener('click', function(){
          b.style.display = 'none';
          self._errCount = 0;
          if(self._errBadgeTimer) clearTimeout(self._errBadgeTimer);
        });
        document.body.appendChild(b);
        this._errBadge = b;
      }
      this._errCount = (this._errCount || 0) + 1;
      var span = this._errBadge.querySelector('span');
      if(span) span.textContent = 'JS errors: ' + this._errCount;
      this._errBadge.style.display = 'inline-flex';
      if(this._errBadgeTimer) clearTimeout(this._errBadgeTimer);
      var self2 = this;
      this._errBadgeTimer = setTimeout(function(){
        if(self2._errBadge) self2._errBadge.style.display = 'none';
      }, 30000);
    },

    addStyles: function(css){
      var s = document.createElement('style');
      s.textContent = css;
      document.head.appendChild(s);
    },

    injectHTML: function(html, selector){
      if(selector){
        var el = document.querySelector(selector);
        if(el){ el.innerHTML += html; return true; }
      }
      var div = document.createElement('div');
      div.innerHTML = html;
      document.body.appendChild(div);
      return false;
    },

    showNotification: function(message, type){
      type = type || 'info';
      var n = document.createElement('div');
      n.style.cssText = 'position:fixed;top:20px;right:20px;z-index:99999;padding:14px 20px;border-radius:10px;background:' +
        (type==='error'?'#ef4444':type==='success'?'#22c55e':'#6366f1') +
        ';color:#fff;font-size:14px;font-weight:500;box-shadow:0 8px 30px rgba(0,0,0,0.3);' +
        'opacity:0;transform:translateY(-20px);transition:all 0.3s ease;max-width:400px';
      n.textContent = message;
      document.body.appendChild(n);
      requestAnimationFrame(function(){
        n.style.opacity = '1';
        n.style.transform = 'translateY(0)';
      });
      setTimeout(function(){
        n.style.opacity = '0';
        n.style.transform = 'translateY(-20px)';
        setTimeout(function(){ n.remove(); }, 300);
      }, 4000);
    },

    _origDisplay: {},

    modifyDom: function(selector, action, value){
      if(!selector) return;
      var els = document.querySelectorAll(selector);
      if(!els.length){ this.warn('modifyDom: no elements found for "' + selector + '"'); return; }
      var self = this;
      els.forEach(function(el){
        switch(action){
          case 'show':
            if(self._origDisplay[el.tagName+'_'+el.className+'_'+el.id]){
              el.style.display = self._origDisplay[el.tagName+'_'+el.className+'_'+el.id];
            } else {
              el.style.display = '';
            }
            break;
          case 'hide': self._origDisplay[el.tagName+'_'+el.className+'_'+el.id] = el.style.display || window.getComputedStyle(el).display; el.style.display = 'none'; break;
          case 'toggle':
            if(el.style.display && el.style.display !== 'none'){
              self._origDisplay[el.tagName+'_'+el.className+'_'+el.id] = el.style.display;
              el.style.display = 'none';
            } else if(self._origDisplay[el.tagName+'_'+el.className+'_'+el.id]){
              el.style.display = self._origDisplay[el.tagName+'_'+el.className+'_'+el.id];
              delete self._origDisplay[el.tagName+'_'+el.className+'_'+el.id];
            } else {
              el.style.display = '';
            }
            break;
          case 'addClass': if(value) el.classList.add(value); break;
          case 'removeClass': if(value) el.classList.remove(value); break;
          case 'toggleClass': if(value) el.classList.toggle(value); break;
          case 'setHTML': el.innerHTML = value || ''; break;
          case 'setText': el.textContent = value || ''; break;
          case 'setAttr': var parts = (value||'').split('='); if(parts.length>=2) el.setAttribute(parts[0], parts.slice(1).join('=')); break;
          case 'remove': el.remove(); break;
          case 'append': if(value) el.insertAdjacentHTML('beforeend', value); break;
          case 'prepend': if(value) el.insertAdjacentHTML('afterbegin', value); break;
          case 'before': if(value) el.insertAdjacentHTML('beforebegin', value); break;
          case 'after': if(value) el.insertAdjacentHTML('afterend', value); break;
          case 'replace': if(value) el.outerHTML = value; break;
          default: self.warn('modifyDom: unknown action "' + action + '"');
        }
      });
    },

    _extBadgeIds: {},

    _ensureExtMenu: function(){
      if(document.getElementById('ext-menu-btn')) return true;
      var tb = document.querySelector('.toolbar') || document.querySelector('nav');
      if(!tb) return false;
      var wrap = document.createElement('div');
      wrap.id = 'ext-menu-btn';
      wrap.className = 'ext-menu-btn';
      wrap.innerHTML = '<i class="fas fa-puzzle-piece"></i><span class="ext-count">0</span><i class="fas fa-caret-down" style="font-size:10px;margin-left:3px"></i>';
      var dd = document.createElement('div');
      dd.id = 'ext-menu-dropdown';
      dd.className = 'ext-menu-dropdown';
      dd.style.display = 'none';
      wrap.appendChild(dd);
      wrap.addEventListener('click', function(e){
        e.stopPropagation();
        dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
      });
      document.addEventListener('click', function(){ dd.style.display = 'none'; });
      tb.appendChild(wrap);
      return true;
    },

    addBadge: function(name, icon, text, onClick){
      var self = this;
      function doAdd(){
        var key = name.replace(/[^a-zA-Z0-9]/g,'');
        if(self._extBadgeIds[key]) return true;
        if(!self._ensureExtMenu()) return false;
        var dd = document.getElementById('ext-menu-dropdown');
        if(!dd) return false;
        var item = document.createElement('div');
        item.id = 'ext-menu-item-' + key;
        item.className = 'ext-menu-item';
        item.innerHTML = '<i class="fas fa-' + icon + '" style="width:16px;text-align:center"></i> ' + text;
        if(onClick) item.addEventListener('click', function(e){ e.stopPropagation(); onClick(); });
        dd.appendChild(item);
        self._extBadgeIds[key] = true;
        var countEl = document.querySelector('#ext-menu-btn .ext-count');
        if(countEl) countEl.textContent = dd.children.length;
        return true;
      }
      if(document.readyState==='loading'){
        document.addEventListener('DOMContentLoaded', doAdd);
      } else {
        if(doAdd()) return;
        var checkInterval = setInterval(function(){
          if(doAdd()) clearInterval(checkInterval);
        }, 500);
        setTimeout(function(){ clearInterval(checkInterval); }, 15000);
      }
    },

    addMenuItem: function(id, label, icon, url){
      function doAdd(){
        var tb = document.querySelector('.toolbar') || document.querySelector('nav');
        if(!tb) return false;
        if(document.getElementById(id)) return true;
        var a = document.createElement('a');
        a.className = 'btn';
        a.href = url || '#';
        a.innerHTML = '<i class="fas fa-' + icon + '"></i><span>' + label + '</span>';
        if(id) a.id = id;
        tb.appendChild(a);
        return true;
      }
      if(document.readyState==='loading'){
        document.addEventListener('DOMContentLoaded', doAdd);
      } else {
        if(doAdd()) return;
        var checkInterval = setInterval(function(){
          if(doAdd()) clearInterval(checkInterval);
        }, 500);
        setTimeout(function(){ clearInterval(checkInterval); }, 15000);
      }
    },

    registerModulePages: function(extName, pages){
      if(!pages || !pages.length) return;
      function doAdd(){
        var tb = document.querySelector('.toolbar') || document.querySelector('nav');
        if(!tb) return false;
        pages.forEach(function(p){
          if(p.in_nav === false) return;
          if(document.getElementById('ext-page-'+extName+'-'+p.path)) return;
          var a = document.createElement('a');
          a.className = 'btn';
          a.id = 'ext-page-'+extName+'-'+p.path;
          a.href = '/module/' + encodeURIComponent(extName) + '/' + encodeURIComponent(p.path);
          a.innerHTML = '<i class="fas fa-' + (p.icon||'fa-globe').replace('fa-','') + '"></i><span>' + (p.title||p.path) + '</span>';
          var sep = document.querySelector('.toolbar .sep');
          if(sep && sep.parentNode) sep.parentNode.insertBefore(a, sep.nextSibling);
          else tb.appendChild(a);
        });
        return true;
      }
      if(document.readyState==='loading'){
        document.addEventListener('DOMContentLoaded', doAdd);
      } else {
        doAdd();
      }
    },

    storage: {
      _extName: null,
      _init: function(extName){ this._extName = extName; },
      get: function(key){
        var self = this;
        return fetch('/api/extensions/' + encodeURIComponent(self._extName) + '/storage/' + encodeURIComponent(key))
          .then(function(r){ return r.json(); })
          .then(function(d){ return d.value !== undefined ? d.value : null; });
      },
      set: function(key, value){
        var self = this;
        return fetch('/api/extensions/' + encodeURIComponent(self._extName) + '/storage/' + encodeURIComponent(key), {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({value: value})
        }).then(function(r){ return r.json(); });
      },
      delete: function(key){
        var self = this;
        return fetch('/api/extensions/' + encodeURIComponent(self._extName) + '/storage/' + encodeURIComponent(key), {
          method: 'DELETE'
        }).then(function(r){ return r.json(); });
      },
      list: function(){
        var self = this;
        return fetch('/api/extensions/' + encodeURIComponent(self._extName) + '/storage')
          .then(function(r){ return r.json(); });
      }
    },

    _messageListeners: [],

    onMessage: {
      addListener: function(callback){
        RT._messageListeners.push(callback);
      },
      removeListener: function(callback){
        RT._messageListeners = RT._messageListeners.filter(function(cb){ return cb !== callback; });
      }
    },

    sendMessage: function(targetExt, type, payload){
      payload = payload || {};
      return fetch('/api/extensions/message', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          target: targetExt,
          sender: this.storage._extName || 'unknown',
          type: type || 'custom',
          payload: payload
        })
      }).then(function(r){ return r.json(); });
    },

    _receiveMessage: function(sender, type, payload){
      var listeners = RT._messageListeners;
      for(var i=0; i<listeners.length; i++){
        try { listeners[i]({sender: sender, type: type, payload: payload}); }
        catch(e){ RT.error('Message listener error: ' + e.message); }
      }
    },

    getExtName: function(){
      return this.storage._extName || null;
    },

    setExtName: function(name){
      this.storage._init(name);
    },

    sidebar: {
      _panels: {},
      create: function(id, config){
        if(this._panels[id]) return;
        config = config || {};
        var panel = document.createElement('div');
        panel.id = 'ext-sidebar-' + id;
        panel.className = 'ext-sidebar-panel';
        panel.style.cssText = 'position:fixed;top:0;right:0;width:' + (config.width || 320) + 'px;height:100vh;background:var(--bg2,#151b23);border-left:1px solid var(--border,#2a3548);z-index:99998;overflow-y:auto;transform:translateX(100%);transition:transform 0.3s ease;padding:16px';
        if(config.title){
          var hdr = document.createElement('div');
          hdr.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border)';
          hdr.innerHTML = '<span style="font-weight:700;font-size:15px">' + config.title + '</span><span style="cursor:pointer;color:var(--text2);font-size:18px" onclick="ExtensionRuntime.sidebar.close(\'' + id + '\')">&times;</span>';
          panel.appendChild(hdr);
        }
        if(config.html){
          var div = document.createElement('div');
          div.innerHTML = config.html;
          panel.appendChild(div);
        }
        document.body.appendChild(panel);
        var overlay = document.createElement('div');
        overlay.id = 'ext-sidebar-overlay-' + id;
        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.3);z-index:99997;display:none';
        overlay.addEventListener('click', function(){ RT.sidebar.close(id); });
        document.body.appendChild(overlay);
        this._panels[id] = {el: panel, overlay: overlay, visible: false};
      },
      open: function(id){
        var p = this._panels[id];
        if(!p) return;
        p.el.style.transform = 'translateX(0)';
        p.overlay.style.display = 'block';
        p.visible = true;
      },
      close: function(id){
        var p = this._panels[id];
        if(!p) return;
        p.el.style.transform = 'translateX(100%)';
        p.overlay.style.display = 'none';
        p.visible = false;
      },
      toggle: function(id){
        var p = this._panels[id];
        if(!p) return;
        if(p.visible) this.close(id);
        else this.open(id);
      },
      setContent: function(id, html){
        var p = this._panels[id];
        if(!p) return;
        var contentEl = p.el.querySelector('.ext-sidebar-content');
        if(!contentEl){
          contentEl = document.createElement('div');
          contentEl.className = 'ext-sidebar-content';
          p.el.appendChild(contentEl);
        }
        contentEl.innerHTML = html;
      }
    },

    panel: {
      _panels: {},
      create: function(id, config){
        if(this._panels[id]) return;
        config = config || {};
        var panel = document.createElement('div');
        panel.id = 'ext-panel-' + id;
        panel.className = 'ext-panel';
        panel.style.cssText = 'position:fixed;bottom:0;left:0;width:100%;height:' + (config.height || 300) + 'px;background:var(--bg2,#151b23);border-top:1px solid var(--border,#2a3548);z-index:99996;transform:translateY(100%);transition:transform 0.3s ease;padding:16px;overflow-y:auto';
        if(config.title){
          var hdr = document.createElement('div');
          hdr.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border)';
          hdr.innerHTML = '<span style="font-weight:700;font-size:14px">' + config.title + '</span><span style="cursor:pointer;color:var(--text2);font-size:16px" onclick="ExtensionRuntime.panel.close(\'' + id + '\')">&times;</span>';
          panel.appendChild(hdr);
        }
        if(config.html){
          var div = document.createElement('div');
          div.innerHTML = config.html;
          panel.appendChild(div);
        }
        document.body.appendChild(panel);
        this._panels[id] = {el: panel, visible: false};
      },
      open: function(id){
        var p = this._panels[id];
        if(!p) return;
        p.el.style.transform = 'translateY(0)';
        p.visible = true;
      },
      close: function(id){
        var p = this._panels[id];
        if(!p) return;
        p.el.style.transform = 'translateY(100%)';
        p.visible = false;
      },
      toggle: function(id){
        var p = this._panels[id];
        if(!p) return;
        if(p.visible) this.close(id);
        else this.open(id);
      },
      setContent: function(id, html){
        var p = this._panels[id];
        if(!p) return;
        var contentEl = p.el.querySelector('.ext-panel-content');
        if(!contentEl){
          contentEl = document.createElement('div');
          contentEl.className = 'ext-panel-content';
          p.el.appendChild(contentEl);
        }
        contentEl.innerHTML = html;
      }
    },

    commands: {
      _shortcuts: {},
      register: function(id, config){
        config = config || {};
        this._shortcuts[id] = config;
        if(config.key){
          var parts = config.key.split('+');
          var ctrl = parts.indexOf('Ctrl') !== -1;
          var alt = parts.indexOf('Alt') !== -1;
          var shift = parts.indexOf('Shift') !== -1;
          var key = parts[parts.length-1].toLowerCase();
          document.addEventListener('keydown', function(e){
            if(ctrl && !e.ctrlKey) return;
            if(alt && !e.altKey) return;
            if(shift && !e.shiftKey) return;
            if(e.key.toLowerCase() !== key) return;
            if(config.handler) config.handler(e);
          });
        }
      },
      unregister: function(id){
        delete this._shortcuts[id];
      }
    }
  };
  var _s = document.createElement('style');
  _s.textContent = '.ext-menu-btn{position:relative;display:inline-flex;align-items:center;gap:4px;padding:4px 10px;background:linear-gradient(135deg,#6366f1,#a855f7);color:#fff;border-radius:20px;font-size:11px;font-weight:600;cursor:pointer;transition:all 0.2s;white-space:nowrap}.ext-menu-btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(99,102,241,0.4)}.ext-menu-btn .ext-count{background:rgba(255,255,255,0.25);border-radius:10px;padding:0 6px;font-size:10px}.ext-menu-dropdown{position:absolute;top:calc(100% + 4px);right:0;min-width:180px;background:var(--bg2,#1e1e2e);border:1px solid var(--border,#2a2a3e);border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,0.4);z-index:99999;overflow:hidden}.ext-menu-item{padding:8px 14px;font-size:12px;color:var(--text1,#ddd);cursor:pointer;display:flex;align-items:center;gap:8px;transition:background 0.15s}.ext-menu-item:hover{background:var(--accent,#6366f1);color:#fff}';
  document.head.appendChild(_s);
  window.ExtensionRuntime = RT;
})();