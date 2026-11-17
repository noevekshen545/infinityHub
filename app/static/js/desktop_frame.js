/* Панель управления окном для режима Electron.

   Рисуется только когда страница открыта в обёртке (window.electronAPI есть).
   В обычном браузере скрипт ничего не делает — приложение выглядит как
   прежде. Дизайн копирует Infinity: те же иконки, размеры и цвета.
*/
(function () {
  'use strict';

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn, { once: true });
    } else {
      fn();
    }
  }

  ready(function () {
    var api = window.electronAPI;
    if (!api || api.isElectron !== true) return;      // обычный браузер
    /* Панель кнопок окна нужна только главному окну без рамки. Прозрачным
       оверлеям (аватар, палитра) она не нужна — у них своя полоса
       перетаскивания, а окнам с системной рамкой кнопки рисует Windows:
       вторая панель только дублировала бы их и сдвигала страницу на 30px. */
    if (api.chrome && api.chrome !== 'app') return;

    // Разметка может быть уже встроена в шаблон (главная) — тогда стили
    // берутся из её <style>, а скрипт только вешает обработчики.
    // На остальных страницах панели нет, рисуем её сами.
    var own = false;
    var bar = document.getElementById('winBtns');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'winBtns';
      bar.className = 'win-btns';
      bar.innerHTML =
        '<button class="wb wb-browser" data-win="browser" title="Браузер" aria-label="Браузер">' +
          '<svg viewBox="0 0 16 16" aria-hidden="true">' +
          '<circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" stroke-width="1.2"/>' +
          '<path d="M1.8 8h12.4M8 1.8c1.9 2 1.9 10.4 0 12.4M8 1.8c-1.9 2-1.9 10.4 0 12.4" ' +
          'fill="none" stroke="currentColor" stroke-width="1"/></svg>' +
        '</button>' +
        '<button class="wb" data-win="minimize" title="Свернуть" aria-label="Свернуть">' +
          '<svg viewBox="0 0 12 12" aria-hidden="true">' +
          '<path d="M2.2 6h7.6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>' +
        '</button>' +
        '<button class="wb" data-win="maximize" title="Развернуть" aria-label="Развернуть">' +
          '<svg viewBox="0 0 12 12" aria-hidden="true">' +
          '<rect x="2.6" y="2.6" width="6.8" height="6.8" rx="1" fill="none" ' +
          'stroke="currentColor" stroke-width="1.1"/></svg>' +
        '</button>' +
        '<button class="wb wb-close" data-win="close" title="Закрыть" aria-label="Закрыть">' +
          '<svg viewBox="0 0 12 12" aria-hidden="true">' +
          '<path d="M3.2 3.2l5.6 5.6M8.8 3.2l-5.6 5.6" stroke="currentColor" ' +
          'stroke-width="1.2" stroke-linecap="round"/></svg>' +
        '</button>';
      document.body.appendChild(bar);
      own = true;
    }

    if (own && !document.getElementById('infinityDesktopCss')) {
      var link = document.createElement('link');
      link.id = 'infinityDesktopCss';
      link.rel = 'stylesheet';
      link.href = '/static/css/desktop_frame.css';
      document.head.appendChild(link);
    }

    bar.hidden = false;
    // Класс вешаем и на <html>, и на <body>: стили панели и полосы
    // перетаскивания окна ориентируются на html.electron-mode.
    document.documentElement.classList.add('electron-mode');
    document.body.classList.add('electron-mode');

    /* Скрытие кнопок окна (Ctrl+Shift+H) — модуль win_nav.js: одна клавиша
       и одно состояние на всё приложение, включая окно браузера. Здесь
       только просим применить сохранённый выбор к нашей панели. */
    function applyWinNav() {
      if (window.infinityWinNav) { window.infinityWinNav.refresh(); return; }
      var s = document.querySelector('script[src*="win_nav.js"]');
      if (!s) {
        s = document.createElement('script');
        s.src = '/static/js/win_nav.js?v=3';
        s.defer = true;
        s.onload = function () {
          if (window.infinityWinNav) window.infinityWinNav.refresh();
        };
        document.head.appendChild(s);
      }
    }
    applyWinNav();

    if (bar.dataset.bound === '1') return;      // обработчики уже навешены
    bar.dataset.bound = '1';

    bar.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-win]');
      if (!btn) return;
      var action = btn.getAttribute('data-win');
      if (action === 'minimize') api.minimize();
      else if (action === 'maximize') api.maximize();
      else if (action === 'close') api.close();
      else if (action === 'browser') {
        // Глобус: открыть/показать встроенный браузер на правой половине.
        if (api.openBrowser) api.openBrowser('half');
      }
    });

    // Двойной клик по пустому месту панели — развернуть/свернуть
    bar.addEventListener('dblclick', function (e) {
      if (e.target.closest('[data-win]')) return;
      api.maximize();
    });

    // Горячие клавиши управления окном (работают на любой странице):
    //   Ctrl+Shift+F — свернуть      Ctrl+Shift+M — развернуть/свернуть
    //   Ctrl+Shift+W — закрыть в трей Alt+Enter    — свернуть/показать
    // Фаза перехвата: иначе фокус в поле ввода или в ифрейме съедал клавишу
    // и сочетание «не работало».
    document.addEventListener('keydown', function (e) {
      /* Программные события (клик по строке палитры горячих клавиш)
         адресованы самой странице: Ctrl+Shift+F там может значить
         «форматировать», а не «свернуть окно». */
      if (!e.isTrusted) return;
      if (e.altKey && e.key === 'Enter') {
        e.preventDefault();
        api.toggleWindow();
        return;
      }
      if (!(e.ctrlKey && e.shiftKey)) return;
      var k = (e.key || '').toLowerCase();
      if (k === 'f') { e.preventDefault(); api.minimize(); }
      else if (k === 'm') { e.preventDefault(); api.maximize(); }
      else if (k === 'w') { e.preventDefault(); api.close(); }
    }, true);
  });
})();
