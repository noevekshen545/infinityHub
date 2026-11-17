/* News Next — слой оформления новой страницы новостей.

   Подключается только к шаблону со скином (data-news-skin="next" на <html>);
   классическая страница (?skin=classic) его не загружает.

   Логика страницы не трогается: данные берутся из уже отрисованных элементов
   (#totalCount, #allStat, карточки в #results), никаких своих запросов.

   Задачи:
     1) toggleNewsRail() — кнопка в разметке ссылалась на неё, но реализации
        не было: на узких экранах рейл должен выдвигаться;
     2) шапка и правая сводка: дата, всего/в ленте. Заполняются по факту
        отрисовки ленты, поэтому не разъезжаются с фильтрами. */
(function () {
  'use strict';

  var root = document.documentElement;
  if (!root.getAttribute('data-news-skin')) return;

  /* ── Рейл: выдвижной режим на узких экранах ─────────────────────── */
  var scrim = null;

  function isOpen() {
    return document.body.classList.contains('rail-open');
  }

  function setOpen(value) {
    var open = !!value;
    document.body.classList.toggle('rail-open', open);
    var toggle = document.getElementById('newsRailToggle');
    if (toggle) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-label', open ? 'Закрыть меню' : 'Открыть меню');
      var ic = toggle.querySelector('i');
      if (ic) ic.className = 'fa-solid fa-' + (open ? 'xmark' : 'bars');
    }
    if (scrim) scrim.classList.toggle('on', open);
  }

  window.toggleNewsRail = function () {
    setOpen(!isOpen());
  };

  function wireRail() {
    var rail = document.getElementById('newsRail');
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

    /* Расширение окна до десктопной ширины закрывает выдвижной режим,
       иначе рейл остался бы спрятанным за кулисами при видимой колонке. */
    var mq = window.matchMedia('(min-width: 981px)');
    var onChange = function (e) { if (e.matches) setOpen(false); };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }

  /* ── Шапка и сводка ─────────────────────────────────────────────── */
  function todayLine() {
    try {
      return new Intl.DateTimeFormat('ru-RU',
        { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date())
        .replace(/^./, function (c) { return c.toUpperCase(); });
    } catch (e) {
      return new Date().toLocaleDateString();
    }
  }

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el && el.textContent !== value) el.textContent = value;
  }

  function countIn(node) {
    if (!node) return 0;
    return node.querySelectorAll('.news-card').length;
  }

  function totalFromStats() {
    var el = document.getElementById('totalCount');
    var strong = el && el.querySelector('strong');
    var raw = (strong ? strong.textContent : (el ? el.textContent : ''))
      .replace(/\D/g, '');
    return raw ? parseInt(raw, 10) : 0;
  }

  function ruPlural(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  function syncSummary() {
    var visible = countIn(document.getElementById('results'));
    var total = totalFromStats();
    var date = todayLine();
    setText('asideDate', date);
    setText('asideTotal', String(total));
    setText('asideVisible', String(visible));
    setText('newsHeroDate', total
      ? date + ' · ' + total + ' ' + ruPlural(total, 'материал', 'материала', 'материалов')
        + ' в ленте ' + visible
      : 'Свежие материалы из ваших источников');
  }

  function wireSummary() {
    var results = document.getElementById('results');
    var stats = document.getElementById('totalCount');
    if (results) {
      new MutationObserver(syncSummary).observe(results, { childList: true });
    }
    if (stats) {
      new MutationObserver(syncSummary).observe(stats,
        { childList: true, characterData: true, subtree: true });
    }
    /* Первый заход: лента могла отрисоваться до подключения наблюдателя. */
    syncSummary();
    window.addEventListener('load', syncSummary);
  }

  function start() {
    wireRail();
    wireSummary();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
