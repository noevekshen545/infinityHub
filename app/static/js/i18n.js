(function () {
  'use strict';

  var currentLang = localStorage.getItem('__i18n_lang__') || 'ru';
  var dict = {};
  var TRANSLATABLE_ATTRS = ['placeholder', 'title', 'alt', 'aria-label'];
  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, INPUT: 1, SELECT: 1, OPTION: 1, CODE: 1, PRE: 1 };
  var done = false;

  function $(id) { return document.getElementById(id); }

  function fetchLang(cb) {
    if (typeof window.__LANG__ !== 'undefined' && window.__LANG__ !== 'ru') {
      currentLang = window.__LANG__;
    }
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/config/language', true);
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          currentLang = JSON.parse(xhr.responseText).language || 'ru';
          localStorage.setItem('__i18n_lang__', currentLang);
        } catch (e) {}
      }
      if (currentLang === 'ru') { if (cb) cb(); return; }
      var dictXhr = new XMLHttpRequest();
      dictXhr.open('GET', '/static/lang/' + currentLang + '.json', true);
      dictXhr.onload = function () {
        if (dictXhr.status === 200) {
          try { dict = JSON.parse(dictXhr.responseText); } catch (e) { dict = {}; }
        }
        if (cb) cb();
      };
      dictXhr.onerror = function () { if (cb) cb(); };
      dictXhr.send();
    };
    xhr.onerror = function () { if (cb) cb(); };
    xhr.send();
  }

  function translateText(text) {
    var trimmed = text.trim();
    if (!trimmed) return null;
    var translation = dict[trimmed];
    if (translation) return translation;
    var lower = trimmed.toLowerCase();
    translation = dict[lower];
    if (translation) return translation;
    return null;
  }

  function walkDOM(root) {
    if (!root || !dict || Object.keys(dict).length === 0) return;
    var treeWalker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        var parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (SKIP_TAGS[parent.tagName]) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var textNodes = [];
    while (treeWalker.nextNode()) textNodes.push(treeWalker.currentNode);
    for (var i = 0; i < textNodes.length; i++) {
      var node = textNodes[i];
      var translation = translateText(node.textContent);
      if (translation !== null) {
        node.textContent = translation;
      }
    }
  }

  function translateAttributes(root) {
    if (!root || !dict || Object.keys(dict).length === 0) return;
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (SKIP_TAGS[el.tagName]) continue;
      for (var a = 0; a < TRANSLATABLE_ATTRS.length; a++) {
        var attr = TRANSLATABLE_ATTRS[a];
        var val = el.getAttribute(attr);
        if (val) {
          var translation = translateText(val);
          if (translation !== null && translation !== val) {
            el.setAttribute(attr, translation);
          }
        }
      }
    }
  }

  function translateSelectOptions(root) {
    if (!root || !dict || Object.keys(dict).length === 0) return;
    var selects = root.querySelectorAll('select');
    for (var i = 0; i < selects.length; i++) {
      var opts = selects[i].querySelectorAll('option');
      for (var j = 0; j < opts.length; j++) {
        var opt = opts[j];
        var translation = translateText(opt.textContent);
        if (translation !== null) {
          opt.textContent = translation;
        }
      }
    }
  }

  function setDocumentLang() {
    if (currentLang === 'en') {
      document.documentElement.lang = 'en';
    } else {
      document.documentElement.lang = 'ru';
    }
  }

  // --- ключевой перевод: __t('ключ', 'Оригинал') ---------------------------
  // Сервер подкладывает в страницу <script id="i18n-boot"> с картой тех ключей,
  // которые встречаются в её HTML. Дорисованный JS контент может позвать ключ,
  // которого в разметке не было, — тогда карта добирается с /api/i18n/catalog
  // один раз на страницу. Свой `__t` уже стоит в bootstrap: не затираем,
  // дополняем его данные; ставим свою версию только там, где bootstrap не
  // прибыл (страница без ключей в HTML).
  var keysLoading = false;

  function box() {
    if (!window.__I18N || typeof window.__I18N !== 'object') {
      window.__I18N = {lang: currentLang, d: {}};
    }
    if (!window.__I18N.d) window.__I18N.d = {};
    return window.__I18N;
  }

  function loadKeys() {
    var b = box();
    if (b.full || keysLoading || currentLang === 'ru') return;
    keysLoading = true;
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/i18n/catalog', true);
    xhr.onload = function () {
      keysLoading = false;
      if (xhr.status !== 200) return;
      try {
        var data = JSON.parse(xhr.responseText);
        if (!data || !data.d) return;
        for (var k in data.d) {
          if (Object.prototype.hasOwnProperty.call(data.d, k)) b.d[k] = data.d[k];
        }
        b.full = true;
        // Страница, которая уже успела нарисовать ключ до прихода карты, получает
        // шанс дорисовать себя: событие слушают те модули, что перерисовывают
        // список по своему состоянию.
        var ev = document.createEvent('Event');
        ev.initEvent('i18n-keys', true, true);
        document.dispatchEvent(ev);
      } catch (e) {}
    };
    xhr.onerror = function () { keysLoading = false; };
    xhr.send();
  }

  function tKey(key, fallback) {
    var b = box();
    if (Object.prototype.hasOwnProperty.call(b.d, key)) return b.d[key];
    loadKeys();
    return fallback;
  }

  if (typeof window.__t !== 'function') {
    window.__t = function (key, fallback) { return tKey(key, fallback); };
  }

  function applyTo(root) {
    if (!root || !dict || Object.keys(dict).length === 0) return;
    walkDOM(root);
    translateAttributes(root);
    translateSelectOptions(root);
  }

  // Дорисовка: список собralся после перевода страницы, и без наблюдателя его
  // строки оставались бы по-русски. Русский путь наблюдателя не включает вовсе.
  var moTimer = 0;
  var observer = null;

  function startObserver() {
    if (observer || !window.MutationObserver || currentLang === 'ru') return;
    observer = new MutationObserver(function (records) {
      var roots = [];
      for (var i = 0; i < records.length; i++) {
        var r = records[i];
        if (r.type === 'childList') {
          for (var j = 0; j < r.addedNodes.length; j++) {
            if (r.addedNodes[j].nodeType === 1) roots.push(r.addedNodes[j]);
          }
        } else if (r.type === 'characterData' && r.target.parentElement) {
          roots.push(r.target.parentElement);
        }
      }
      if (!roots.length) return;
      if (moTimer) clearTimeout(moTimer);
      // Пакет правок перерисовываем одним проходом: список на 2000 строк
      // иначе снял бы по проходу на каждую строку.
      moTimer = setTimeout(function () {
        moTimer = 0;
        for (var k = 0; k < roots.length; k++) {
          if (roots[k].isConnected) applyTo(roots[k]);
        }
      }, 120);
    });
    observer.observe(document.body, {childList: true, subtree: true, characterData: true});
  }

  function translate() {
    if (currentLang === 'ru') {
      setDocumentLang();
      done = true;
      return;
    }
    setDocumentLang();
    if (document.title) {
      var titleTrans = translateText(document.title);
      if (titleTrans !== null) document.title = titleTrans;
    }
    applyTo(document.body);
    startObserver();
    done = true;
    var evt = document.createEvent('Event');
    evt.initEvent('i18n-ready', true, true);
    document.dispatchEvent(evt);
  }

  function init() {
    if (done) return;
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { fetchLang(translate); });
    } else {
      fetchLang(translate);
    }
  }

  window.__i18n = {
    getLang: function () { return currentLang; },
    setLang: function (lang) {
      currentLang = lang || 'ru';
      localStorage.setItem('__i18n_lang__', currentLang);
      dict = {};
      done = false;
      // Карта ключей относится к языку, а наблюдатель — к переводу: сменить
      // язык со старым наблюдателем означало бы перерисовывать русский
      // интерфейс английским словарём.
      window.__I18N = {lang: currentLang, d: {}};
      keysLoading = false;
      if (observer) {
        observer.disconnect();
        observer = null;
      }
      init();
    },
    translateElement: function (el) {
      if (currentLang === 'ru' || !dict || Object.keys(dict).length === 0) return;
      applyTo(el);
    },
    translateKey: tKey,
    loadKeys: loadKeys,
    translateText: function (text) {
      if (currentLang === 'ru') return text;
      var t = translateText(text);
      return t !== null ? t : text;
    }
  };

  init();
})();
