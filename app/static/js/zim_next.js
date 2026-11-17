/* zim_next.js — слой рейла библиотеки ZIM.

   Подключается только к шаблону со скином (data-zim-skin="next" на <html>);
   классическая выдача (?skin=classic) ни этого файла, ни module_rail.* не
   загружает. Логика страницы не трогается: данные берутся у уже отрисованных
   элементов, своих запросов здесь нет.

   Задачи:
     1) toggleZimRail() — FAB в разметке ссылается на неё: на узких экранах
        рейл должен выдвигаться, как в новостях, обучении и App Store;
     2) подложка, Escape и закрытие при выборе пункта — иначе панель осталась
        бы открытой поверх списка;
     3) расширение окна до десктопной ширины снимает выдвижной режим: ключ
        состояния рейла относится к сворачиванию, а не к выдвижению. */
(function () {
  'use strict';

  var root = document.documentElement;
  if (!root.getAttribute('data-zim-skin')) return;

  var scrim = null;

  function isOpen() {
    return document.body.classList.contains('rail-open');
  }

  function setOpen(value) {
    var open = !!value;
    document.body.classList.toggle('rail-open', open);
    var toggle = document.getElementById('zimRailToggle');
    if (toggle) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-label', open ? 'Закрыть меню' : 'Открыть меню');
      var ic = toggle.querySelector('i');
      if (ic) ic.className = 'fa-solid fa-' + (open ? 'xmark' : 'bars');
    }
    if (scrim) scrim.classList.toggle('on', open);
  }

  window.toggleZimRail = function () {
    setOpen(!isOpen());
  };

  function start() {
    var rail = document.getElementById('zimRail');
    if (!rail) return;

    scrim = document.createElement('div');
    scrim.className = 'rail-scrim';
    scrim.addEventListener('click', function () { setOpen(false); });
    document.body.appendChild(scrim);

    rail.addEventListener('click', function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      if (target.closest('a.btn, button.btn, .btn-icon')) setOpen(false);
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && isOpen()) setOpen(false);
    });

    var mq = window.matchMedia('(min-width: 981px)');
    var onChange = function (e) { if (e.matches) setOpen(false); };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
