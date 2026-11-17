/* module_rail.js — сворачивание левого рейла в полосу иконок.

   Работает в паре с module_rail.css и подключается на тех же страницах, что
   и он (новости, обучение, App Store, настройки, ZIM). Гейт по скину своего
   модуля: на классической выдаче (?skin=classic) ни css, ни этого скрипта
   нет, а страницы книг и поиска этот файл не загружают — у них своя
   реализация в library_next.js.

   Поведение повторяет книги: кнопка-стрелка на правой кромке рейла, ширина
   меняется классом rail-collapsed на <html> (от неё зависит и отступ
   контента), подписи улетают в title. Состояние хранится раздельно по
   модулям, чтобы свёрнутые новости не сворачивали обучение. */
(function () {
  'use strict';

  var root = document.documentElement;
  var skinModule = root.getAttribute('data-news-skin') ? 'news'
    : root.getAttribute('data-education-skin') ? 'education'
    : root.getAttribute('data-apps-skin') ? 'apps'
    : root.getAttribute('data-settings-skin') ? 'settings'
    : root.getAttribute('data-zim-skin') ? 'zim' : null;
  if (!skinModule) return;

  var KEY = skinModule + '_rail_collapsed';

  function start() {
    var rail = document.querySelector('.toolbar');
    if (!rail || rail.querySelector('.rail-collapse')) return;

    /* Подсказка для пунктов без своего title: берём первую подпись. В
       свёрнутом виде это единственный способ понять, что за пункт. */
    Array.prototype.forEach.call(rail.querySelectorAll('.btn, .btn-icon'), function (b) {
      if (b.title) return;
      var spans = b.querySelectorAll('span');
      for (var i = 0; i < spans.length; i++) {
        if (spans[i].classList.contains('rail-name')) continue;
        var label = (spans[i].textContent || '').trim();
        if (label) {
          b.title = label;
          break;
        }
      }
    });

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'rail-collapse';
    btn.innerHTML = '<i class="fa-solid fa-chevron-left"></i>';
    rail.appendChild(btn);

    function isCollapsed() {
      return root.classList.contains('rail-collapsed');
    }

    function setCollapsed(on) {
      root.classList.toggle('rail-collapsed', !!on);
      try { localStorage.setItem(KEY, on ? '1' : '0'); } catch (e) { /* приватный режим */ }
      var label = on ? 'Развернуть меню' : 'Свернуть меню';
      btn.title = label;
      btn.setAttribute('aria-label', label);
      btn.setAttribute('aria-expanded', on ? 'false' : 'true');
    }

    var stored = '0';
    try { stored = localStorage.getItem(KEY) || '0'; } catch (e) { /* приватный режим */ }
    setCollapsed(stored === '1');

    btn.addEventListener('click', function (ev) {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      setCollapsed(!isCollapsed());
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
