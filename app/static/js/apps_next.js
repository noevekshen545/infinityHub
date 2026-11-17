/* App Store Next — слой оформления модуля приложений.

   Подключается к шаблонам со скином (data-apps-skin="next" на <html>): к списку
   /apps и к детальной странице приложения (?skin=classic грузит старые версии
   без этого скрипта).

   Логика страниц не трогается: список приложений, установка, расширения и
   магазин живут в инлайновых скриптах шаблонов. Здесь только то, чего шаблоны
   сами по себе не знают:

     1) выдвижной режим рейла на узких экранах (FAB, затемнение, Escape) —
        та же грамматика, что в news_next.js;
     2) подпись свёрнутого рейла: module_rail.js берёт её из первого <span>,
        а у пунктов App Store подписи в .rail-name нет.

   Рейл и кнопка-стрелка ищутся по id страницы, а не по одному имени: у
   детальной страницы они называются иначе. */
(function () {
  'use strict';

  var root = document.documentElement;
  if (!root.getAttribute('data-apps-skin')) return;

  var scrim = null;
  var toggle = null;

  function isOpen() {
    return document.body.classList.contains('rail-open');
  }

  function setOpen(value) {
    var open = !!value;
    document.body.classList.toggle('rail-open', open);
    if (toggle) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-label', open ? 'Закрыть меню' : 'Открыть меню');
      var ic = toggle.querySelector('i');
      if (ic) ic.className = 'fa-solid fa-' + (open ? 'xmark' : 'bars');
    }
    if (scrim) scrim.classList.toggle('on', open);
  }

  window.toggleAppsRail = function () {
    setOpen(!isOpen());
  };

  function start() {
    var rail = document.getElementById('appsRail') || document.querySelector('.toolbar');
    if (!rail) return;

    toggle = document.getElementById('appsRailToggle') || document.querySelector('.rail-fab');
    if (toggle) toggle.addEventListener('click', function () { setOpen(!isOpen()); });

    scrim = document.createElement('div');
    scrim.className = 'rail-scrim';
    scrim.addEventListener('click', function () { setOpen(false); });
    document.body.appendChild(scrim);

    /* Пункты рейла переводят на другую страницу или меняют вид — на узком
       экране меню после этого должно уехать за кулисы. */
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

    setOpen(false);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
