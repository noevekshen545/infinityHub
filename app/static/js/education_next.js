/* Education Next — слой оформления страницы «Обучение».

   Подключается только к шаблону со скином (data-education-skin="next" на
   <html>); классическая страница (?skin=classic) его не загружает.

   Логика страницы не трогается: рейл вызывает те же функции разметки
   (showCreatePanel, toggleSidebar, openMaterialsModal, …), своих запросов
   здесь нет.

   Задача одна: toggleEduRail() — на узких экранах постоянный левый рейл
   становится выдвижной панелью, как в книгах и новостях. */
(function () {
  'use strict';

  var root = document.documentElement;
  if (!root.getAttribute('data-education-skin')) return;

  var scrim = null;

  function isOpen() {
    return document.body.classList.contains('rail-open');
  }

  function setOpen(value) {
    var open = !!value;
    document.body.classList.toggle('rail-open', open);
    var toggle = document.getElementById('eduRailToggle');
    if (toggle) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-label', open ? 'Закрыть меню' : 'Открыть меню');
      var ic = toggle.querySelector('i');
      if (ic) ic.className = 'fas fa-' + (open ? 'xmark' : 'bars');
    }
    if (scrim) scrim.classList.toggle('on', open);
  }

  window.toggleEduRail = function () {
    setOpen(!isOpen());
  };

  function start() {
    var rail = document.getElementById('eduRail');
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

    /* Расширение окна до десктопной ширины закрывает выдвижной режим:
       иначе рейл остался бы спрятанным за кулисами при видимой колонке. */
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
