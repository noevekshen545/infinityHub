/**
 * ExtensionAPI — Frontend API for JavaScript extensions
 * Доступен как window.ExtensionAPI
 */
(function() {
    'use strict';

    function getExtName() {
        return document.body.getAttribute('data-extension-name') || 'unknown';
    }
    function storageKey() { return 'ext:state:' + getExtName(); }

    const ExtensionAPI = {
        injectCSS: function(code) {
            const style = document.createElement('style');
            style.textContent = code;
            style.setAttribute('data-extension', getExtName());
            document.head.appendChild(style);
            return style;
        },

        addButton: function(module, config) {
            config = config || {};
            const extName = getExtName();
            const toolbar = document.querySelector('.toolbar') || document.querySelector('nav');
            if (!toolbar) return null;
            const btn = document.createElement('button');
            btn.className = 'btn-icon ext-btn-' + extName;
            btn.innerHTML = '<i class="fas ' + (config.icon || 'fa-puzzle-piece') + '"></i>';
            btn.title = config.label || '';
            btn.style.cssText = 'margin-left:4px;font-size:11px;padding:4px 8px;border-radius:8px;background:var(--bg3);border:1px solid var(--border);color:var(--text);cursor:pointer;transition:all 0.2s';
            if (config.onClick) btn.onclick = config.onClick;
            if (config.position === 'left') {
                toolbar.insertBefore(btn, toolbar.firstChild);
            } else {
                toolbar.appendChild(btn);
            }
            return btn;
        },

        onRoute: function(module, callback) {
            const handler = function() {
                const path = window.location.pathname;
                if (path.startsWith('/' + module) || path.startsWith('/api/' + module)) {
                    callback({ path: path, module: module });
                }
            };
            window.addEventListener('popstate', handler);
            window.addEventListener('hashchange', handler);
            handler();
            return handler;
        },

        showToast: function(msg, type) {
            type = type || 'info';
            const extName = getExtName();
            const existing = document.getElementById('ext-toast-' + extName);
            if (existing) existing.remove();
            const toast = document.createElement('div');
            toast.id = 'ext-toast-' + extName;
            const icons = { success: 'fa-check-circle', error: 'fa-exclamation-circle', warning: 'fa-exclamation-triangle', info: 'fa-info-circle' };
            toast.innerHTML = '<i class="fas ' + (icons[type] || icons.info) + '"></i><span>' + msg + '</span>';
            toast.style.cssText = 'position:fixed;bottom:20px;right:20px;padding:10px 16px;border-radius:10px;background:var(--bg2);border:1px solid var(--border);color:var(--text);font-size:12px;z-index:99999;display:flex;align-items:center;gap:8px;box-shadow:0 4px 20px rgba(0,0,0,0.3);animation:fadeIn 0.2s ease;max-width:400px';
            document.body.appendChild(toast);
            setTimeout(function() { toast.style.opacity = '0'; toast.style.transition = 'opacity 0.3s'; setTimeout(function() { toast.remove(); }, 300); }, 3000);
        },

        fetch: async function(path, options) {
            options = options || {};
            try {
                const res = await fetch(path, options);
                const ct = res.headers.get('content-type') || '';
                if (ct.includes('application/json')) return await res.json();
                return await res.text();
            } catch (e) {
                console.error('[ExtensionAPI] fetch error:', e);
                return null;
            }
        },

        getConfig: function() {
            try { return JSON.parse(localStorage.getItem(storageKey() + ':config') || '{}'); } catch(e) { return {}; }
        },

        setState: function(data) {
            try { localStorage.setItem(storageKey(), JSON.stringify(data)); } catch(e) {}
        },

        getState: function() {
            try { return JSON.parse(localStorage.getItem(storageKey()) || '{}'); } catch(e) { return {}; }
        },

        onModuleReady: function(module, callback) {
            if (document.readyState === 'complete') {
                callback();
            } else {
                document.addEventListener('readystatechange', function() {
                    if (document.readyState === 'complete') callback();
                });
            }
        },

        registerHook: function(hookName, callback) {
            const extName = getExtName();
            window.__extensionHooks = window.__extensionHooks || {};
            window.__extensionHooks[extName] = window.__extensionHooks[extName] || {};
            window.__extensionHooks[extName][hookName] = callback;
        },

        log: function(msg, level) {
            level = level || 'info';
            const extName = getExtName();
            console.log('[' + extName + '] ' + msg);
            fetch('/api/extensions/' + extName + '/logs?max_lines=100&message=' + encodeURIComponent(msg) + '&level=' + encodeURIComponent(level), {
                method: 'GET'
            }).catch(function() {});
        }
    };

    window.ExtensionAPI = ExtensionAPI;
    document.dispatchEvent(new CustomEvent('extension-api-ready', { detail: { api: ExtensionAPI } }));
    console.log('[ExtensionAPI] Ready');
})();
