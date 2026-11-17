/* =====================================================================
   Infinity Search — мобильная поддержка (≤640px)
   Минимальный JS: только то, что нельзя сделать в CSS:
   1. Класс `mobile` на <body>/<html> (для CSS-селекторов и JS-логики)
   2. WinBox-окна на телефонах — полноэкранные, без drag/resize,
      с поддержкой обеих сигнатур конструктора ('title', opts) и (opts)
   3. Пересчёт при повороте экрана (планшет/телефон)
   4. Скрытый «маркер» мобильной версии (кнопка-телефон с подсказкой)
   ===================================================================== */
(function () {
  'use strict';

  var MOBILE_MAX = 640;
  var BADGE_ID = 'infinity-mobile-badge';
  /* Окно 3D-аватара всегда уже 640px, и бейдж-«телефон» висел поверх модели.
     На странице-оверлее он не нужен: там нет мобильной вёрстки, а сама
     страница рисует свои кнопки. */
  var IS_EMBED = /[?&]embed=1/.test(location.search);

  function isMobile() {
    return (window.innerWidth || document.documentElement.clientWidth) <= MOBILE_MAX;
  }

  function applyMobileClass() {
    var mob = isMobile();
    document.body.classList.toggle('mobile', mob);
    document.documentElement.classList.toggle('mobile', mob);
    if (mob && !IS_EMBED) ensureBadge();
    else removeBadge();
  }

  /* --- Бейдж «Мобильный интерфейс» --- */
  function badgeStyle() {
    return '#' + BADGE_ID + '{position:fixed;right:10px;bottom:10px;z-index:100002;' +
      'width:26px;height:26px;min-width:26px!important;min-height:26px!important;' +
      'border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;' +
      'background:var(--bg2,#1e1e1e);color:var(--text,#f1f5f9);border:1px solid var(--border,rgba(255,255,255,.18));' +
      'box-shadow:0 2px 8px rgba(0,0,0,.25);opacity:.72;transition:opacity .15s ease;padding:0;' +
      'box-sizing:border-box;-webkit-tap-highlight-color:transparent}' +
      '#' + BADGE_ID + ':hover,#' + BADGE_ID + ':active{opacity:1}' +
      '#' + BADGE_ID + ' .badge-tip{position:absolute;right:0;bottom:30px;white-space:nowrap;' +
      'font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-size:11px;line-height:1.3;' +
      'color:var(--text,#f1f5f9);background:var(--bg3,#0f172a);border:1px solid var(--border,rgba(255,255,255,.15));' +
      'padding:4px 8px;border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.3);opacity:1;' +
      'transition:opacity .15s ease}';
  }

  function ensureBadge() {
    if (document.getElementById(BADGE_ID)) return;
    var b = document.createElement('button');
    b.id = BADGE_ID;
    b.type = 'button';
    b.setAttribute('aria-label', 'Мобильный интерфейс');
    b.innerHTML =
      '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false">' +
      '<path fill="currentColor" d="M15.5 1h-8A2.5 2.5 0 0 0 5 3.5v17A2.5 2.5 0 0 0 7.5 23h8a2.5 2.5 0 0 0 2.5-2.5v-17A2.5 2.5 0 0 0 15.5 1zm-4 21c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5zm4.5-4H7V3.5h9V18z"/>' +
      '</svg>';
    b.addEventListener('click', function () {
      var span = b.querySelector('.badge-tip');
      if (span) {
        b.removeChild(span);
        return;
      }
      var tip = document.createElement('span');
      tip.className = 'badge-tip';
      tip.textContent = 'Мобильный интерфейс · ширина ' +
        (window.innerWidth || document.documentElement.clientWidth) + 'px';
      b.appendChild(tip);
      setTimeout(function () {
        if (tip && tip.parentNode === b) {
          tip.style.opacity = '0';
          setTimeout(function () { if (tip && tip.parentNode === b) b.removeChild(tip); }, 250);
        }
      }, 2200);
    });
    var css = document.createElement('style');
    css.id = BADGE_ID + '-css';
    css.textContent = badgeStyle();
    document.head.appendChild(css);
    document.body.appendChild(b);
  }

  function removeBadge() {
    var b = document.getElementById(BADGE_ID);
    if (b) b.remove();
    var c = document.getElementById(BADGE_ID + '-css');
    if (c) c.remove();
  }

  /* --- WinBox: на мобиле максимизируем, отключаем drag/resize --- */
  function patchWinBox() {
    if (!window.WinBox || window.__mobileWinBoxPatched) return;
    var OrigWinBox = window.WinBox;
    function MobileWinBox(a, b) {
      var isMob = isMobile();
      var title = typeof a === 'string' ? a : null;
      var opts = title !== null ? (b || {}) : (a || {});
      if (isMob) {
        opts = Object.assign({}, opts, {
          drag: false,
          resize: false,
          movable: false,
          zoom: false
        });
      }
      var w = title !== null ? new OrigWinBox(title, opts) : new OrigWinBox(opts);
      if (isMob && !opts.min) {
        setTimeout(function () {
          try {
            w.maximize();
          } catch (e) { /* окно могло быть уже закрыто */ }
        }, 30);
      }
      return w;
    }
    MobileWinBox.prototype = OrigWinBox.prototype;
    MobileWinBox.constructor = OrigWinBox.constructor;
    for (var k in OrigWinBox) {
      if (Object.prototype.hasOwnProperty.call(OrigWinBox, k)) {
        MobileWinBox[k] = OrigWinBox[k];
      }
    }
    window.WinBox = MobileWinBox;
    window.__mobileWinBoxPatched = true;
  }

  /* --- Меню переполнения (<details class="more">) ---
     Попап переносится в <body> и ставится position:fixed. Это обязательно:
     тулбары page_media / page_books имеют overflow-x:auto и
     ВАЖНО: после переноса попап перестаёт быть потомком <details>, поэтому
     искать его через d.querySelector('.more-pop') НЕЛЬЗЯ — вернёт null.
     Ссылку на попап храним в d.__pop (задаётся один раз при привязке). */
  function initMoreMenus() {
    if (window.__moreMenusBound) return;
    window.__moreMenusBound = true;

    var GAP = 8;                       // отступ от кнопки до меню
    var EDGE = 10;                     // минимальный отступ от края экрана

    function allMenus() {
      return document.querySelectorAll('details.more');
    }

    // Текущее открытое меню берём из DOM, а не из кэша: кэш рассинхронизировался
    // и подложка удалялась не вовремя.
    function currentOpen() {
      return document.querySelector('details.more[open]');
    }

    function cleanup(d) {
      if (!d) return;
      var pop = d.__pop;
      if (pop && pop.parentNode !== d) {
        d.appendChild(pop);
        pop.removeAttribute('data-portal');
        pop.style.cssText = '';
      }
      d.classList.remove('more-ready');
      var scrim = document.querySelector('.more-scrim');
      if (scrim) scrim.remove();
      document.documentElement.classList.remove('has-more-open');
    }

    function closeMenu(d) {
      if (!d) return;
      if (d.hasAttribute('open')) d.removeAttribute('open');  // -> событие toggle
      cleanup(d);
    }

    function closeAll() {
      var list = allMenus();
      for (var i = 0; i < list.length; i++) closeMenu(list[i]);
      var scrim = document.querySelector('.more-scrim');
      if (scrim) scrim.remove();
      document.documentElement.classList.remove('has-more-open');
    }

    /* Ставит попап под кнопку, не давая ему вылезти за края экрана. */
    function position(pop, summary) {
      var r = summary.getBoundingClientRect();
      var vw = document.documentElement.clientWidth;
      var vh = document.documentElement.clientHeight;

      pop.style.left = '0px';
      pop.style.top = '0px';
      pop.style.maxHeight = '';
      var w = pop.offsetWidth;
      var h = pop.offsetHeight;

      // По ширине: правый край выравниваем по правому краю кнопки,
      // но не выходим за пределы экрана.
      var left = r.right - w;
      if (left < EDGE) left = EDGE;
      if (left + w > vw - EDGE) left = Math.max(EDGE, vw - EDGE - w);

      // По высоте: предпочитаем вниз, но если не влезает — открываем вверх.
      var below = r.bottom + GAP;
      var top = below;
      if (below + h > vh - EDGE) {
        var above = r.top - GAP - h;
        if (above < EDGE) {
          // не помещается ни вниз, ни вверх — прижимаем к верху и скроллим
          top = EDGE;
          pop.style.maxHeight = Math.max(160, vh - EDGE * 2) + 'px';
        } else {
          top = above;
        }
      }
      pop.style.left = Math.round(left) + 'px';
      pop.style.top = Math.round(top) + 'px';
    }

    function openMenu(d) {
      var pop = d.__pop;
      var summary = d.querySelector('summary');
      if (!pop || !summary) return;

      if (!document.querySelector('.more-scrim')) {
        var scrim = document.createElement('div');
        scrim.className = 'more-scrim';
        document.body.appendChild(scrim);
      }

      // портал в body: снимаем обрезку тулбаром
      document.body.appendChild(pop);
      pop.setAttribute('data-portal', '1');
      pop.style.position = 'fixed';
      pop.style.zIndex = '2147483000';
      d.classList.add('more-ready');

      position(pop, summary);
      document.documentElement.classList.add('has-more-open');
    }

    /* Реагируем на событие toggle, а не на click: <details> меняет [open]
       ПОСЛЕ обработчика click, поэтому в click атрибут ещё старый —
       раньше из-за этого меню успевало закрыться, не успев открыться. */
    function bind(d) {
      if (d.__moreBound) return;
      d.__moreBound = true;
      d.__pop = d.querySelector('.more-pop');   // запоминаем ДО переноса
      if (!d.__pop) return;
      d.addEventListener('toggle', function () {
        if (d.hasAttribute('open')) {
          var was = currentOpen();
          if (was && was !== d) closeMenu(was);   // закрываем прежнее меню
          openMenu(d);
        } else {
          cleanup(d);
        }
      });
    }

    function bindAll() {
      var list = allMenus();
      for (var i = 0; i < list.length; i++) bind(list[i]);
    }

    // Тап по подложке, вне открытого меню, ИЛИ по пункту меню — закрыть.
    // Последнее важно: после переноса попапа в <body> он перекрывает страницу
    // подложкой, поэтому оставлять его открытым после действия нельзя.
    document.addEventListener('click', function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var d = currentOpen();
      if (!d) return;
      var inPop = t.closest('.more-pop');
      if (inPop && !t.closest('button, a, [role="menuitem"]')) return; // клик по фону меню
      // Клик по пункту: сначала отдаём обработчику пункта (он сработает на bubbling),
      // закрываем на следующем тике, чтобы не мешать его логике.
      if (inPop) setTimeout(function () { closeMenu(d); }, 0);
      else closeMenu(d);
    }, true);

    bindAll();
    // Страницы могут подставлять меню позже — ловим появление
    if (window.MutationObserver) {
      new MutationObserver(function () {
        bindAll();
      }).observe(document.documentElement, {childList: true, subtree: true});
    }

    // Прокрутка/ресайз: держим меню под кнопкой
    function reposition() {
      var d = currentOpen();
      if (!d) return;
      var pop = d.__pop;
      var summary = d.querySelector('summary');
      if (!pop || !summary || !document.body.contains(pop)) {
        closeMenu(d);
        return;
      }
      position(pop, summary);
    }
    window.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', function () {
      // Меню одинаково работает на ПК и на телефоне, поэтому на любой
      // ширине просто держим его под кнопкой. Раньше здесь был closeAll()
      // для десктопа — из-за этого на ПК меню закрывалось само.
      reposition();
    });

    // Escape закрывает (доступность)
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeAll();
    });
  }

  /* --- Инициализация --- */
  function init() {
    applyMobileClass();
    patchWinBox();
    initMoreMenus();
    var t = null;
    window.addEventListener('resize', function () {
      clearTimeout(t);
      t = setTimeout(function () {
        applyMobileClass();
      }, 150);
    });
    document.addEventListener('DOMContentLoaded', applyMobileClass);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();