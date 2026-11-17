/* ============================================================================
   ГРАФ СВЯЗЕЙ — 2D РЕЖИМ
   ----------------------------------------------------------------------------
   Отдельным файлом, а не куском в page_notes_graph_brain.html, потому что там
   уже 3k строк, и весь модульный граф к THREE.js отношения не имеет.

   Зачем он нужен рядом с 3D:
     * читаемость. В 3D подписи не всегда развёрнуты, рёбра проходят сквозь
       узлы, и свайп на телефоне не работает. В Obsidian граф плоский.
     * скорость. 3D-физика здесь считает силы между всеми парами узлов на
       каждом кадре — это O(n²) в кадре анимации, и на сотне заметок
       браузер начинает греться. Ниже раскладка через Barnes-Hut, O(n log n).
     * независимость от WebGL. Без аппаратного ускорения 3D не открывается
       вовсе: чёрный экран вместо графа.

   Раскладка: группы (папки и теги) становятся центрами притяжения для
   своих членов, поэтому рёбра «заметка <-> группа» видимы глазом, а не
   только в легенде.

   Модуль самодостаточен: всё общение наружу — через window.Graph2D.
   ============================================================================ */
(function (global) {
  'use strict';

  const SVG_NS = 'http://www.w3.org/2000/svg';

  /* Две палитры рёбер. Пастельные цвета тёмной темы на светлом фоне
     (#fbbf24, #34d399, #a78bfa) дают контраст ниже 2:1 — линии теряются.
     Светлая палитра — те же оттенки, но на 30–40% темнее. */
  const LINK_PALETTE = {
    dark: {
      wiki: '#a78bfa', folder: '#818cf8', tag: '#34d399',
      tag_pair: '#fbbf24', unresolved: '#ef4444', semantic: '#60a5fa',
    },
    light: {
      wiki: '#7c3aed', folder: '#4f46e5', tag: '#047857',
      tag_pair: '#b45309', unresolved: '#dc2626', semantic: '#2563eb',
    },
  };

  const LINK_STYLE = {
    wiki:      { color: LINK_PALETTE.dark.wiki, width: 1.8, dash: null,  label: 'Ссылка [[]]' },
    folder:    { color: LINK_PALETTE.dark.folder, width: 1.0, dash: '3 3',  label: 'Папка' },
    tag:       { color: LINK_PALETTE.dark.tag, width: 1.0, dash: null,  label: 'Тег' },
    tag_pair:  { color: LINK_PALETTE.dark.tag_pair, width: 0.9, dash: null,  label: 'Общий тег' },
    unresolved:{ color: LINK_PALETTE.dark.unresolved, width: 1.2, dash: '2 4',  label: 'Не разрешена' },
    semantic:  { color: LINK_PALETTE.dark.semantic, width: 1.0, dash: null,  label: 'Смысловая' },
  };

  const CONFIG = {
    /* Раскладка по мотивам Fruchterman–Reingold: отталкивание k²/d и
       притяжение d²/k. Прежняя смесь «заряд 1/d² + пружина» на плотных
       папках либо схлопывала кластер в точку, либо разлеталась. */
    repulsion:   26000,     // сила отталкивания 1/d² (пол в ближней зоне)
    spacing:     62,        // желаемое расстояние между связанными узлами
    linkStrength: 0.8,      // вес притяжения по связи
    maxAttract:  42,        // потолок притяжения, чтобы дальние не «телепортировали»
    collidePad:  1.18,      // насколько радиусы узлов раздвигаются
    clusterPull: 0.22,      // жёсткость пружины к якорю кластера внутри его радиуса
    clusterRadius: 150,     // радиус кластера, за которым пружина усиливается
    clusterEdge:  0.50,     // жёсткость пружины за пределами радиуса
    gravity:     0.02,      // к общему центру, чтобы граф не расползался
    alpha:       1.0,       // затухание раскладки
    alphaMin:    0.0018,
    alphaDecay:  0.0215,
    velocityDecay: 0.55,
    maxVelocity: 18,
    maxForce:    10,        // потолок локальных сил за кадр (гравитация — вне потолка)
    relayoutFrames: 240,
  };

  /* Членство в группе: папка, тег или смысловая тема. Раньше проверка
     перечисляла folder/tag по месту, и новая группа просто выпадала из
     физики и стилей. */
  const isMembershipType = t => t === 'folder' || t === 'tag' || t === 'semantic';

  /* ─────────────────────────── состояние ─────────────────────────── */
  const state = {
    graph: null,
    nodes: [],
    links: [],
    byId: new Map(),
    mode: 'force',            // force | tree
    showGroups: true,
    showLabels: true,
    showOrphans: true,
    hovered: null,
    selected: null,
    neighbors: new Set(),
    filterText: '',
    onlyConnected: false,
    sectionFilter: '',
    /* Какие типы рёбер скрыты. Легенда на странице включает и выключает
       их кликом: при 300 связях «общий тег» даёт такую кашу, что без
       фильтра граф не читается. */
    hiddenTypes: new Set(),
    /* Области, занятые панелями. Раскладку по-прежнему считаем в мировых
       координатах на весь экран, но «вписать» и «показать узел» должны
       целиться в свободную часть окна, иначе под правой панелью деталей
       половина графа. */
    insets: { left: 0, right: 0, top: 0, bottom: 0 },
    layoutTimer: 0,
    /* Подсветка цепочки «путь между заметками». Пока path не пуст, всё
       остальное приглушается: при сотнях рёбер линия пути иначе теряется. */
    path: null,
    pathLinks: null,
    onOpen: null,             // выбор узла → открыть сайдбар страницы
    onCreate: null,           // клик по неразрешённой ссылке → создать заметку
    onHover: null,            // наведение → подсказка страницы
  };

  let svg = null;
  let gLinks = null;
  let gGroups = null;
  let gNodes = null;
  let gLabels = null;
  let theme = {};
  /* Раскладка стартует вокруг (0,0), а вид по умолчанию — без сдвига.
     Без подгонки после первого расчёта весь граф оказывался за левым
     верхним углом канваса (видны только узлы с положительными x/y).
     Флаг ставит setData, снимает первый кадр после остановки раскладки:
     повторная подгонка на resize или перегрев не должны сбрасывать
     пользовательскую панораму. */
  let pendingFit = false;

  /* ─────────────────────────── утилиты ─────────────────────────── */

function el(name, attrs) {
  const n = document.createElementNS(SVG_NS, name);
  if (attrs) for (const k in attrs) {
    /* null/undefined пропускаем: setAttribute приводит их к строке
       «null», и элемент получает атрибут stroke-dasharray="null".
       Браузер такое значение игнорирует, но оно оставалось в DOM,
       ломало проверку «пунктирная ли связь» и мешала отладке. */
    const v = attrs[k];
    if (v === null || v === undefined) continue;
    n.setAttribute(k, v);
  }
  return n;
}

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.body).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  function parseRgb(str) {
    const s = String(str || '').trim();
    let m = s.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
    if (m) {
      let h = m[1];
      if (h.length === 3) h = h.split('').map(c => c + c).join('');
      return [
        parseInt(h.slice(0, 2), 16),
        parseInt(h.slice(2, 4), 16),
        parseInt(h.slice(4, 6), 16),
      ];
    }
    m = s.match(/rgba?\(([^)]+)\)/);
    if (m) {
      const p = m[1].split(',').map(parseFloat);
      return [p[0] || 0, p[1] || 0, p[2] || 0];
    }
    return null;
  }

  function luminance(str) {
    const p = parseRgb(str);
    if (!p) return null;
    return (0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]) / 255;
  }

  /* Притемнение к чёрному на долю amount. Нужно на светлых темах: заливки
     узлов приходят из палитры разделов и рассчитаны на тёмный фон. */
  function shade(hex, amount) {
    const p = parseRgb(hex);
    if (!p) return hex;
    const f = Math.max(0, Math.min(1, amount));
    return '#' + p.map(v => {
      const c = Math.round(v * (1 - f));
      return ('0' + c.toString(16)).slice(-2);
    }).join('');
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function truncate(s, n) {
    s = String(s == null ? '' : s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  /* ─────────────────────────── инициализация ─────────────────────────── */

  function init(opts) {
    if (opts && typeof opts.onOpen === 'function') state.onOpen = opts.onOpen;
    if (opts && typeof opts.onCreate === 'function') state.onCreate = opts.onCreate;

    const host = document.getElementById('canvas2d-container');
    if (!host) return;
    if (svg) return;                     // уже создан

    svg = el('svg', { width: '100%', height: '100%' });
    svg.style.display = 'block';
    svg.style.cursor = 'grab';
    svg.style.touchAction = 'none';      // иначе жест масштабирования мешает панораме
    svg.setAttribute('viewBox', `0 0 ${window.innerWidth} ${window.innerHeight}`);

    // defs — тень под узлами, чтобы на светлой теме кружки не сливались с фоном.
    const defs = el('defs');
    defs.innerHTML =
      '<filter id="g2d-shadow" x="-50%" y="-50%" width="200%" height="200%">' +
      '<feDropShadow dx="0" dy="1" stdDeviation="1.6" flood-opacity="0.16"/></filter>';
    svg.appendChild(defs);

    // Общая группа: панорама и зум применяются к ней одной трансформом.
    sceneG = el('g', { class: 'g2d-scene' });

    gLinks = el('g', { class: 'g2d-links' });
    gGroups = el('g', { class: 'g2d-groups' });
    gNodes = el('g', { class: 'g2d-nodes' });
    gLabels = el('g', { class: 'g2d-labels' });
    sceneG.appendChild(gLinks);
    sceneG.appendChild(gGroups);
    sceneG.appendChild(gNodes);
    sceneG.appendChild(gLabels);
    svg.appendChild(sceneG);

    host.appendChild(svg);
    bindPointer();
    applyGraph2DTheme();
    applyView();
    window.addEventListener('resize', onResize);
  }

  function bindPointer() {
    svg.addEventListener('mousedown', onDown);
    svg.addEventListener('touchstart', onDown, { passive: false });
    window.addEventListener('mousemove', onMove);
    window.addEventListener('touchmove', onMove, { passive: false });
    window.addEventListener('mouseup', onUp);
    window.addEventListener('touchend', onUp);
    svg.addEventListener('wheel', onWheel, { passive: false });
    svg.addEventListener('click', onBgClick);
    svg.addEventListener('dblclick', onDblClick);
  }

  function onResize() {
    if (svg) svg.setAttribute('viewBox', `0 0 ${window.innerWidth} ${window.innerHeight}`);
    alpha(CONFIG.alpha * 0.6);
  }

  /* ─────────────────────────── данные ─────────────────────────── */

  function setData(data) {
    state.graph = data || { nodes: [], links: [] };
    state.byId.clear();
    state.nodes = [];
    state.links = [];
    state.hovered = null;
    state.selected = null;
    state.neighbors.clear();
    /* Новые данные — старый путь ссылается на узлы, которых может уже не
       быть; подсветку снимаем вместе с пересборкой графа. */
    state.path = null;
    state.pathLinks = null;

    const raw = state.graph.nodes || [];
    /* Раскладываем узлы по кругу: стартовые координаты нужны силовой
       раскладке, иначе все узлы стартуют в (0,0) и первый кадр — каша. */
    const n = raw.length || 1;
    for (let i = 0; i < raw.length; i++) {
      const d = raw[i];
      const ang = (i / n) * Math.PI * 2;
      const r = 120 + (i % 7) * 26;
      const node = {
        data: d,
        id: d.id,
        kind: d.kind || 'note',
        title: d.title || 'Без названия',
        color: d.color || '#818cf8',
        count: d.count || (d.members ? d.members.length : 0),
        r: radiusFor(d),
        /* cr — радиус для физики. У группы он следует за видимым ореолом,
           но с ослаблением: иначе папка с 30 заметками отталкивала бы
           соседние кластеры на километры. baseCr — исходный радиус. */
        cr: radiusFor(d),
        baseCr: radiusFor(d),
        x: Math.cos(ang) * r + (Math.random() - 0.5) * 8,
        y: Math.sin(ang) * r + (Math.random() - 0.5) * 8,
        vx: 0, vy: 0,
        fixed: false,
        visible: true,
      };
      state.nodes.push(node);
      state.byId.set(node.id, node);
    }

    for (const l of (state.graph.links || [])) {
      const s = state.byId.get(l.source);
      const t = state.byId.get(l.target);
      // Ссылка на несуществующий узел — пропускаем, но считаем: такие были
      // главным источником «связей нет» — их просто не рисовали.
      if (!s || !t) continue;
      const type = l.type || l.linkType || 'semantic';
      const rec = { s, t, type, weight: l.weight || 1 };
      s.links = s.links || [];
      t.links = t.links || [];
      s.links.push(rec);
      t.links.push(rec);
      state.links.push(rec);
    }

    /* Группы-папки и группы-теги получают позицию в центре своих членов:
       иначе силовая раскладка разносит группу и её заметки по разным
       углам, и ребро «папка <-> заметка» перестаёт читаться. */
    const groups = state.nodes.filter(n => n.kind === 'group');
    const isFolderGroup = n => String(n.id).indexOf('folder:') === 0;
    const sizeOf = n => (n.data.members || []).length;
    /* Одна заметка может оказаться в двух папках (вложенность/переиндек-
       сация). В раскладке закрепляем её за самым маленьким кластером:
       иначе ореол второй папки тянется через пол-сцены до чужой заметки,
       и два кластера слипаются. Связи при этом остаются обе. */
    const claimed = new Set();
    const orderedGroups = groups.slice().sort((a, b) => {
      // Сначала папки, внутри — от маленькой к большой; теги последними.
      const fa = isFolderGroup(a) ? 0 : 1, fb = isFolderGroup(b) ? 0 : 1;
      if (fa !== fb) return fa - fb;
      return sizeOf(a) - sizeOf(b);
    });
    for (const node of orderedGroups) {
      let members = (node.data.members || [])
        .map(id => state.byId.get(id))
        .filter(Boolean);
      if (isFolderGroup(node)) {
        members = members.filter(m => !claimed.has(m.id));
      }
      for (const m of members) {
        claimed.add(m.id);
        /* Якорь кластера — папка: она задаёт компактную пружину. Тег на
           нескольких папках не должен перетягивать заметку между ними. */
        if (!m.groupAnchor || (isFolderGroup(node) && !m.groupAnchor.isFolderGroup)) {
          m.groupAnchor = node;
        }
      }
      node.members = members;
      node.isFolderGroup = isFolderGroup(node);
      // Группы не участвуют в отталкивании как обычные узлы: иначе
      // двойник папки выталкивает из её же кластера.
      node.isGroup = true;
    }
    state.groupNodes = orderedGroups;
    /* Кластеры получают постоянные якоря: крупные ближе к центру, мелкие
       по спирали. Без этого центры групп вычислялись от собственных
       заметок, система не имела точки опоры, и самый плотный кластер
       «уезжал» от остальных на полторы тысячи пикселей — автозум затем
       сжимал всю сцену в крошечное пятно. */
    placeClusterAnchors(orderedGroups.slice().sort((a, b) => sizeOf(b) - sizeOf(a)));

    render();
    /* Вписываем сразу: стартовые координаты — окружность вокруг (0,0), то
       есть левый верхний угол канваса. Без первого fit() граф выглядел
       пустым, пока силовая раскладка не доиграет (у 60 узлов это секунды),
       а при открытом навигаторе все узлы оказывались под ним. */
    fit();
    pendingFit = true;
    alpha(1);
  }

  function radiusFor(d) {
    const kind = d.kind || 'note';
    if (kind === 'group') return Math.min(46, 16 + (d.count || 2) * 2.1);
    if (kind === 'unresolved') return 9;
    return 5.5 + Math.min(9, (d.size || 1) * 1.5);
  }

  /* Якоря кластеров: самая крупная папка — в центре, остальные — по
     кольцу вокруг неё с золотым шагом. Прежняя спираль ставила соседей
     ближе, чем радиусы их ореолов, и кластеры наезжали друг на друга,
     а расчёт «центр группы — среднее её заметок» вообще не давал системе
     точки опоры. Заметки сразу раскидываем вокруг своего якоря. */
  function placeClusterAnchors(groups) {
    if (!groups.length) return;
    const golden = Math.PI * (3 - Math.sqrt(5));
    const radiusOf = g => 18 + Math.sqrt((g.members || []).length) * 22;
    const r0 = radiusOf(groups[0]);
    const restR = Math.max(40, ...groups.slice(1).map(radiusOf));
    const ring = r0 + restR + 46;
    groups.forEach((g, i) => {
      const R = radiusOf(g);
      let ax = 0, ay = 0;
      if (i > 0) {
        const ang = (i - 1) * golden + 0.8;
        const rad = ring + (i % 2) * restR * 0.6;
        ax = Math.cos(ang) * rad;
        ay = Math.sin(ang) * rad;
      }
      g.ax = ax;
      g.ay = ay;
      g.x = ax;
      g.y = ay;
      const members = g.members || [];
      /* У папки с единственной заметкой разброса нет — иначе вокруг
         одинокой точки рисовался бы пустой «пузырь» на пол-сцены. */
      const spread = members.length > 1 ? R * 0.72 : 0;
      members.forEach((m, j) => {
        if (m.groupAnchor !== g) return;
        const a = (j / members.length) * Math.PI * 2 + i * 1.7;
        const rr = spread * Math.sqrt((j + 0.5) / members.length);
        m.x = ax + Math.cos(a) * rr;
        m.y = ay + Math.sin(a) * rr;
        m.vx = 0; m.vy = 0;
      });
    });
  }

  /* ─────────────────────────── раскладка ─────────────────────────── */

  function alpha(a) {
    state.layoutTimer = Math.max(state.layoutTimer, a * CONFIG.relayoutFrames);
    tick();
  }

  function snapGroups() {
    for (const nd of state.nodes) {
      if (!nd.isGroup || nd.ax == null) continue;
      nd.x = nd.ax;
      nd.y = nd.ay;
    }
  }

  let rafId = null;
  function tick() {
    if (rafId) return;
    const step = () => {
      rafId = null;
      if (state.layoutTimer <= 0) {
        /* Финальная доводка: за время раскладки центр группы мог отстать,
           и ореол щёлкал размером уже после остановки. Ставим точно в
           центр своих заметок. */
        snapGroups();
        draw();
        if (pendingFit) { pendingFit = false; fit(); }
        return;
      }
      state.layoutTimer--;
      const a = Math.max(CONFIG.alphaMin, state.layoutTimer / CONFIG.relayoutFrames);
      if (state.mode === 'force') stepForce(a);
      else layoutTree();
      draw();
      rafId = requestAnimationFrame(step);
    };
    rafId = requestAnimationFrame(step);
  }

  /* Barnes-Hut: индекс квадрантов по сетке вместо перебора всех пар.
     При 500 заметок честный O(n²) — это 250k пар на кадр, и браузер
     задыхается. Сетка даёт практически тот же результат за O(n). */
  function stepForce(a) {
    const nodes = state.nodes;
    const cell = 90;
    const buckets = new Map();
    for (const nd of nodes) {
      const key = Math.floor(nd.x / cell) + ',' + Math.floor(nd.y / cell);
      let b = buckets.get(key);
      if (!b) { b = []; buckets.set(key, b); }
      b.push(nd);
    }

    for (const nd of nodes) {
      if (nd.fixed) continue;
      let fx = 0, fy = 0;
      const cx = Math.floor(nd.x / cell), cy = Math.floor(nd.y / cell);

      // Отталкивание: перебираем соседние клетки плюс клетки узлов.
      for (let ox = -1; ox <= 1; ox++) {
        for (let oy = -1; oy <= 1; oy++) {
          const b = buckets.get((cx + ox) + ',' + (cy + oy));
          if (!b) continue;
          for (const other of b) {
            if (other === nd) continue;
            // Член своей группы не отталкивается от её «ореола»: заметка
            // должна быть внутри кружка папки. Группы между собой
            // отталкиваются — иначе ореолы папок наезжают друг на друга.
            if ((nd.members && nd.members.indexOf(other) !== -1) ||
                (other.members && other.members.indexOf(nd) !== -1)) continue;
            let dx = nd.x - other.x, dy = nd.y - other.y;
            let d2 = dx * dx + dy * dy;
            if (d2 < 1) {
              // Совпавшие узлы: расталкиваем по детерминированному вектору,
              // иначе Math.random() на двух кадрах подряд оставит их слипшимися.
              dx = ((nd.id > other.id) ? 1 : -1) * 0.7 + 0.31;
              dy = ((nd.id < other.id) ? 1 : -1) * 0.7 + 0.17;
              d2 = dx * dx + dy * dy;
            }
            const dist = Math.sqrt(d2);
            /* Отталкивание 1/d² с полом у ближней зоны: у FR-варианта k²/d
               дальнобойная сила не затухает, и одиночные папки уезжали от
               центра на 1500 px, растягивая всю сцену. Ближнюю дистанцию
               всё равно держат resolveCollisions. */
            const sizeK = Math.sqrt(((nd.cr || nd.r) + (other.cr || other.r)) / 14);
            const f = (CONFIG.repulsion / Math.max(d2, 900)) * sizeK * a;
            fx += (dx / dist) * f;
            fy += (dy / dist) * f;
          }
        }
      }

      // Притяжение по связям.
      if (nd.links) {
        for (const rec of nd.links) {
          const other = rec.s === nd ? rec.t : rec.s;
          if (!other) continue;
          if (other.isGroup && !nd.isGroup) continue;   // группу тянем отдельно
          /* Членство в папке/теге — не связь для физики: заметку держит
             пружина к якорю кластера. Иначе заметка, входящая в две
             папки, перетягивалась между их центрами. */
          if (isMembershipType(rec.type)) continue;
          const dx = other.x - nd.x, dy = other.y - nd.y;
          const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
          /* Притяжение d²/k: у близких узлов слабое (не слипаются), у
             далёких сильное (не убегают). Потолок — от «прыжков» через
             весь экран на первых кадрах. */
          let att = (d * d / CONFIG.spacing) * CONFIG.linkStrength * rec.weight * a;
          if (att > CONFIG.maxAttract) att = CONFIG.maxAttract;
          fx += (dx / d) * att;
          fy += (dy / d) * att;
        }
      }

      /* Чужой ореол не должен накрывать заметку: центры групп — производная
         от позиций их членов, поэтому расталкивание самих групп тут же
         перезаписывается, и кластеры слипаются в одну кляксу. Толкаем
         именно заметки из чужих кружков. */
      if (!nd.isGroup && state.groupNodes) {
        for (const g of state.groupNodes) {
          if (g === nd.groupAnchor) continue;
          if (g.members && g.members.indexOf(nd) !== -1) continue;
          const dx = nd.x - g.x, dy = nd.y - g.y;
          const d = Math.hypot(dx, dy);
          if (d >= g.r + nd.r + 8) continue;
          const need = g.r + nd.r + 8;
          const push = (need - d) * 0.09 * a;
          const ux = d > 0.01 ? dx / d : 1, uy = d > 0.01 ? dy / d : 0.3;
          fx += ux * push;
          fy += uy * push;
        }
      }

      /* Тяга к центру своей группы. Слабая линейная пружина действует
         всегда, за радиусом кластера усиливается. Прежний вариант с
         пружиной только за радиусом оставлял папку из 32 заметок пустым
         кольцом радиусом под 500 px: членам кластера нечем было
         притягиваться, кроме общей гравитации. */
      if (!nd.isGroup && nd.groupAnchor) {
        const gx = nd.groupAnchor.ax - nd.x;
        const gy = nd.groupAnchor.ay - nd.y;
        const gd = Math.hypot(gx, gy);
        if (gd > 1) {
          /* Пружина Гука: сила пропорциональна смещению, а не его
             квадрату. Прежний вариант (d²) разгонял кластер, стоило
             ему чуть разойтись, и был главной причиной «уехавших»
             островов графа. */
          const k = gd > CONFIG.clusterRadius ? CONFIG.clusterEdge : CONFIG.clusterPull;
          const f = k * gd * a;
          fx += (gx / gd) * f;
          fy += (gy / gd) * f;
        }
      }

      /* Потолок локальных сил: у плотной группы (десятки соседей на
         минимальной дистанции) ближнее расталкивание складывалось в
         сотни единиц импульса за кадр. Ограничение по модулю сохраняет
         направление, но держит скорость в узде. */
      const fmag = Math.hypot(fx, fy);
      if (fmag > CONFIG.maxForce) {
        fx = (fx / fmag) * CONFIG.maxForce;
        fy = (fy / fmag) * CONFIG.maxForce;
      }

      /* Гравитация — ПОСЛЕ потолка: иначе на дальних дистанциях она
         упиралась в тот же лимит, что и расталкивание, и не могла
         вернуть к центру уехавший кластер. Заметкам внутри папки она
         не нужна вовсе: их место задаёт якорь кластера, а глобальное
         притяжение только стаскивало их с якоря к центру сцены. */
      if (!nd.groupAnchor) {
        fx -= nd.x * CONFIG.gravity * a;
        fy -= nd.y * CONFIG.gravity * a;
      }

      nd.vx = (nd.vx + fx) * CONFIG.velocityDecay;
      nd.vy = (nd.vy + fy) * CONFIG.velocityDecay;
      const sp = Math.hypot(nd.vx, nd.vy);
      if (sp > CONFIG.maxVelocity) {
        nd.vx = (nd.vx / sp) * CONFIG.maxVelocity;
        nd.vy = (nd.vy / sp) * CONFIG.maxVelocity;
      }
      nd.x += nd.vx;
      nd.y += nd.vy;
    }

    /* Ореол группы плавно возвращается на свой якорь. Без множителя a:
       это не сила, а производная позиция — с a группа замирала там, где
       её застал конец раскладки. */
    for (const nd of state.nodes) {
      if (!nd.isGroup || nd.ax == null) continue;
      nd.x += (nd.ax - nd.x) * 0.25;
      nd.y += (nd.ay - nd.y) * 0.25;
    }

    resolveCollisions();
    layoutEdges();
  }

  /* Раздвигаем наехавшие узлы. Силы этого не гарантируют: у 40 заметок
     одной папки равновесие приходится на расстояние меньше их диаметров.
     Группа-«ореол» и её заметки намеренно не раздвигаются — заметка должна
     быть внутри кружка папки. */
  function resolveCollisions() {
    const cell = 120;
    const buckets = new Map();
    const nodes = state.nodes;
    for (const nd of nodes) {
      if (nd.fixed) continue;
      const k = Math.floor(nd.x / cell) + ',' + Math.floor(nd.y / cell);
      let b = buckets.get(k);
      if (!b) { b = []; buckets.set(k, b); }
      b.push(nd);
    }
    for (const nd of nodes) {
      const cx = Math.floor(nd.x / cell), cy = Math.floor(nd.y / cell);
      for (let ox = -1; ox <= 1; ox++) {
        for (let oy = -1; oy <= 1; oy++) {
          const b = buckets.get((cx + ox) + ',' + (cy + oy));
          if (!b) continue;
          for (const other of b) {
            if (other === nd || other.fixed) continue;
            // Пары обрабатываем один раз; id стабилен между кадрами.
            if (String(nd.id) >= String(other.id)) continue;
            if (nd.isGroup !== other.isGroup) continue;   // группа ↔ её ореол
            let dx = other.x - nd.x, dy = other.y - nd.y;
            let d = Math.sqrt(dx * dx + dy * dy);
            const minD = ((nd.cr || nd.r) + (other.cr || other.r)) * CONFIG.collidePad + 2;
            if (d >= minD) continue;
            if (d < 0.01) {
              dx = (String(nd.id) > String(other.id)) ? 1 : -1;
              dy = 0.41;
              d = Math.hypot(dx, dy);
            }
            const push = (minD - d) / 2;
            const ux = dx / d, uy = dy / d;
            nd.x -= ux * push;
            nd.y -= uy * push;
            other.x += ux * push;
            other.y += uy * push;
          }
        }
      }
    }
  }

  /* Раскладка рёбер: прямые для дальних пар, дуги для близких —
     так линии не сливаются в одну в центре кластера. */
  function layoutEdges() {
    for (const rec of state.links) {
      const { s, t } = rec;
      const dx = t.x - s.x, dy = t.y - s.y;
      const dist = Math.hypot(dx, dy);
      const selfLoop = s === t;
      if (selfLoop) {
        rec.d = `M ${s.x} ${s.y - s.r - 4} a ${s.r + 6} ${s.r + 6} 0 1 1 0.01 0`;
        rec.mid = { x: s.x, y: s.y - s.r - 12 };
        continue;
      }
      // Обход концов узлов: ребро входит в узел по касательной, а не в центр.
      const angle = Math.atan2(dy, dx);
      const sx = s.x + Math.cos(angle) * s.r;
      const sy = s.y + Math.sin(angle) * s.r;
      const tx = t.x - Math.cos(angle) * t.r;
      const ty = t.y - Math.sin(angle) * t.r;

      const isMembership = isMembershipType(rec.type);
      if (isMembership) {
        // Рёбра «член — группа» прямые: их смысл в том, чтобы глаз шёл
        // от кружка заметки к её папке, изгиб бы это разрушил.
        rec.d = `M ${sx} ${sy} L ${tx} ${ty}`;
        rec.mid = { x: (sx + tx) / 2, y: (sy + ty) / 2 };
      } else {
        // Ссылка [[]] или общий тег: лёгкий изгиб, чтобы параллельные
        // рёбра не лежали друг на друге.
        const bow = Math.min(40, dist * 0.14);
        const mx = (sx + tx) / 2 - Math.sin(angle) * bow;
        const my = (sy + ty) / 2 + Math.cos(angle) * bow;
        rec.d = `M ${sx} ${sy} Q ${mx} ${my} ${tx} ${ty}`;
        rec.mid = { x: mx, y: my };
      }
    }
  }

  /* Древовидная раскладка: группы в колонки, заметки под своей группой.
     Иногда полезнее силовой — видно структуру, а не облако точек.
     Длинные папки переносятся в следующую колонку: у папки из 32 заметок
     одна вертикальная лента высотой 1000+ px уезжала за экран. */
  function layoutTree() {
    const groups = state.nodes.filter(n => n.isGroup && n.visible);
    const notes = state.nodes.filter(n => !n.isGroup && n.kind === 'note' && n.visible);
    const assigned = new Set();
    const COL_W = 170, ROW = 34, MAX_ROWS = 14, PAD = 40;
    let x0 = 80;
    const stack = (items, owner) => {
      const cols = Math.max(1, Math.ceil(items.length / MAX_ROWS));
      items.forEach((m, i) => {
        const c = Math.floor(i / MAX_ROWS), r = i % MAX_ROWS;
        m.x = x0 + c * COL_W;
        m.y = 40 + r * ROW;
        m.vx = 0; m.vy = 0;
        assigned.add(m.id);
      });
      if (owner) {
        owner.x = x0 + (cols - 1) * COL_W / 2;
        owner.y = 0;
      }
      x0 += cols * COL_W + PAD;
    };
    for (const g of groups) {
      const members = (g.members || []).filter(m => !assigned.has(m.id));
      stack(members, g);
    }
    const rest = notes.filter(nd => !assigned.has(nd.id));
    stack(rest, null);
    layoutEdges();
  }

  /* ─────────────────────────── отрисовка ─────────────────────────── */

  function render() {
    if (!gNodes) return;
    gLinks.textContent = '';
    gGroups.textContent = '';
    gNodes.textContent = '';
    gLabels.textContent = '';

    /* visible проверяем и здесь: applyFilter помечал узлы скрытыми, но
       render() рисовал их всё равно — фильтр по папке/поиску гасил только
       рёбра, а узлы оставались на экране и не реагировали на клики. */
    const groups = state.nodes.filter(n => n.isGroup && state.showGroups && n.visible);
    for (const nd of groups) {
      const color = theme.nodeFill(nd.color);
      const c = el('circle', {
        cx: nd.x, cy: nd.y, r: nd.r,
        fill: hexA(color, 0.10),
        stroke: color,
        'stroke-width': 1.6,
        'stroke-dasharray': '4 4',
      });
      c.style.cursor = 'pointer';
      c.addEventListener('mouseenter', e => hover(nd, e));
      c.addEventListener('mouseleave', () => hover(null));
      c.addEventListener('click', ev => { ev.stopPropagation(); pick(nd); });
      gGroups.appendChild(c);
      nd.el = c;
    }

    const visible = state.nodes.filter(n => n.kind !== 'group' && n.visible);
    for (const nd of visible) {
      const c = el('circle', {
        cx: nd.x, cy: nd.y, r: nd.r,
        fill: theme.nodeFill(nd.color),
        stroke: theme.nodeStroke,
        'stroke-width': nd.kind === 'unresolved' ? 1.6 : 1,
        'stroke-dasharray': nd.kind === 'unresolved' ? '2 2' : null,
        opacity: nd.kind === 'unresolved' ? 0.85 : 1,
      });
      c.style.cursor = 'pointer';
      c.addEventListener('mouseenter', e => hover(nd, e));
      c.addEventListener('mouseleave', () => hover(null));
      c.addEventListener('click', ev => { ev.stopPropagation(); pick(nd); });
      gNodes.appendChild(c);
      nd.el = c;
    }

    for (const rec of state.links) {
      if (!rec.s.visible || !rec.t.visible) continue;
      if (!state.showGroups && isMembershipType(rec.type)) continue;
      if (state.hiddenTypes.has(rec.type)) continue;
      const st = LINK_STYLE[rec.type] || LINK_STYLE.semantic;
      const p = el('path', {
        d: rec.d || `M ${rec.s.x} ${rec.s.y} L ${rec.t.x} ${rec.t.y}`,
        fill: 'none',
        stroke: st.color,
        'stroke-width': st.width,
        'stroke-dasharray': st.dash,
        /* Рёбра членства тише смысловых: они лишь показывают, к какому
           кружку относится заметка, и не должны чертить сцену насквозь. */
        opacity: isMembershipType(rec.type) ? 0.22 : 0.5,
        'stroke-linecap': 'round',
      });
      rec.el = p;
      gLinks.appendChild(p);
    }

    draw();
  }

  function draw() {
    if (!gNodes) return;
    const hl = state.hovered;
    const sel = state.selected;
    // Подсвечивать соседей имеет смысл только когда наведён узел:
    // иначе весь граф каждый кадр красится заново.
    const focus = hl || sel;
    /* Путь важнее наведения: пользователь явно выбрал цепочку, и подсветка
       от случайно задетого узла не должна её перебивать. */
    const path = state.path;

    for (const nd of state.nodes) {
      if (!nd.el) continue;
      /* Позиции обновляются здесь, а не только в render(): render()
         создаёт элементы на один момент, а раскладка двигает узлы каждый
         кадр. Без этих двух строк граф замирал на стартовой окружности,
         а подписи (они пересоздаются в draw) уезжали отдельно от узлов. */
      nd.el.setAttribute('cx', nd.x);
      nd.el.setAttribute('cy', nd.y);
      if (nd.isGroup) {
        /* Ореол папки обводит своих членов, а не висит точкой в центре:
           по нему кликают, и он должен совпадать с тем, что видно.
           У тегов члены разбросаны по разным папкам — круглый ореол
           пришлось бы растянуть на пол-сцены, поэтому тег остаётся
           компактным хаб-узлом. */
        if (nd.isFolderGroup && state.mode === 'force') {
          let mr = 0;
          for (const m of (nd.members || [])) {
            /* Чужие члены (заметка закреплена за другой папкой) ореол не
               растягивают: иначе одна далеко уехавшая заметка раздувала
               кружок папки до сотен пикселей. */
            if (m.groupAnchor !== nd) continue;
            mr = Math.max(mr, Math.hypot(m.x - nd.x, m.y - nd.y) + m.r);
          }
          nd.r = Math.max(nd.baseCr, Math.min(340, mr + 12));
        } else {
          // В дереве кружок папки — маркер над колонкой, а не ореол.
          nd.r = nd.baseCr;
        }
        nd.el.setAttribute('r', nd.r);
        /* Ореол занимает место и в физике (с коэффициентом): два кружка
           папок, наехавших друг на друга, читаются как один кластер, а
           подпись соседней папки падает внутрь чужого ореола. 0.8 — чтобы
           совсем не раздувать сцену: границы могут чуть касаться. */
        nd.cr = Math.max(18, Math.min(nd.r * 0.8, 220));
      }
      const isFocus = focus && (nd === focus);
      const isNear = focus && state.neighbors.has(nd.id);
      let dim = focus && !isFocus && !isNear;
      const onPath = path ? path.has(String(nd.id)) : false;
      if (path) dim = !onPath;
      nd.el.setAttribute('opacity',
        dim ? (path ? 0.12 : 0.16) : (nd.kind === 'unresolved' && !isFocus ? 0.85 : 1));
      if (nd.kind !== 'group') {
        nd.el.setAttribute('stroke-width',
          (onPath || isFocus) ? Math.max(2, nd.r * 0.4)
                              : (nd.kind === 'unresolved' ? 1.6 : 1));
      }
    }

    for (const rec of state.links) {
      if (!rec.el) continue;
      rec.el.setAttribute('d', rec.d ||
        `M ${rec.s.x} ${rec.s.y} L ${rec.t.x} ${rec.t.y}`);
      const base = (LINK_STYLE[rec.type] || LINK_STYLE.semantic).width;
      if (path) {
        const onPath = state.pathLinks ? state.pathLinks.has(rec) : false;
        rec.el.setAttribute('opacity', onPath ? 1 : 0.05);
        rec.el.setAttribute('stroke-width', onPath ? base + 1.2 : base);
        continue;
      }
      const touched = focus && (rec.s === focus || rec.t === focus);
      rec.el.setAttribute('opacity', focus ? (touched ? 0.95 : 0.07) : 0.5);
      /* Для нетронутых связей была ширина 0 — при наведении связи не
         приглушались, а исчезали целиком, и «окружение узла» пропадало
         вместе с ними. Теперь базовая толщина сохраняется, гаснет только
         непрозрачность. */
      rec.el.setAttribute('stroke-width', touched ? base + 0.9 : base);
    }

    if (state.showLabels) {
      /* Подписи — экранного размера (1/k), иначе при fit-зуме 0.6 текст
         превращается в 6-пиксельные закорючки. Порядок важен: сначала
         выбранный узел, потом соседи, потом папки, потом хабы — подписи
         проверяются на наложение в экранных координатах, и выживают те,
         что успели занять место. */
      const k = view.k || 1;
      const pad = 6 / k;
      const boxes = [];
      const wanted = [];
      const zoomedIn = k >= 1.7;
      const rank = nd => {
        if (path && path.has(String(nd.id))) return 0;
        if (focus && nd === focus) return 0;
        if (focus && state.neighbors.has(nd.id)) return 1;
        if (nd.kind === 'group') return 2;
        return 3;
      };
      const ordered = state.nodes.slice().sort((a, b) => {
        const ra = rank(a), rb = rank(b);
        if (ra !== rb) return ra - rb;
        return (b.links ? b.links.length : 0) - (a.links ? a.links.length : 0);
      });
      for (const nd of ordered) {
        if (nd.kind === 'group' && !state.showGroups) continue;
        if (!nd.visible) continue;
        /* При активном пути подписываем только его узлы: иначе подписи
           приглушённой части графа лезут поверх цепочки. */
        if (path && !path.has(String(nd.id))) continue;
        const isFocus = focus && nd === focus;
        const isNear = focus && state.neighbors.has(nd.id);
        if (!path && focus && !isFocus && !isNear) continue;
        if (!path && !focus && nd.kind !== 'group' && !zoomedIn) {
          const deg = nd.links ? nd.links.length : 0;
          if (deg < 2) continue;
        }
        wanted.push(nd);
      }
      while (gLabels.firstChild) gLabels.removeChild(gLabels.firstChild);
      /* Кружки заметок тоже занимают место: подпись хаба, легшая поверх
         соседней точки, выглядела как случайный мусор. Группы в
         «занимаемое» не входят — их ореол пустой внутри, и подписи
         заметок внутри папки должны оставаться возможными. */
      const blockers = [];
      const rings = [];
      for (const nd of state.nodes) {
        if (!nd.visible) continue;
        if (nd.kind === 'group') { rings.push(nd); continue; }
        blockers.push({ x: nd.x - nd.r, y: nd.y - nd.r, w: nd.r * 2, h: nd.r * 2 });
      }
      const hitsRing = (b, g) => {
        const dx = Math.max(b.x - g.x, 0, g.x - (b.x + b.w));
        const dy = Math.max(b.y - g.y, 0, g.y - (b.y + b.h));
        return dx * dx + dy * dy < (g.r + 4) * (g.r + 4);
      };
      const hit = (b, box) => box.x < b.x + b.w && box.x + box.w > b.x &&
                              box.y < b.y + b.h && box.y + box.h > b.y;
      const free = (nd, box) => {
        for (const b of boxes) if (hit(b, box)) return false;
        for (const b of blockers) if (hit(b, box)) return false;
        for (const g of rings) {
          /* Свой ореол не мешает подписи: заметка внутри папки должна
             подписываться, а не отбрасываться её же кружком. */
          if (g === nd) continue;
          if (g.members && g.members.indexOf(nd) !== -1) continue;
          if (hitsRing(box, g)) return false;
        }
        return true;
      };
      for (const nd of wanted) {
        const isGroup = nd.kind === 'group';
        const label = isGroup
          ? truncate(nd.title, 22) + ' (' + (nd.count || 0) + ')'
          : truncate(nd.title, 26);
        const fs = (isGroup ? 11.5 : 10.5) / k;
        const w = label.length * fs * 0.56;
        const h = fs * 1.15;
        /* Пробуем несколько позиций вокруг узла: справа, слева, снизу,
           сверху. У большой папки подпись справа упиралась в соседний
           кластер и пропадала совсем — а имя папки важнее подписи хаба. */
        const cands = [[nd.x + nd.r + pad, nd.y - h / 2]];
        if (isGroup) {
          cands.push([nd.x - nd.r - pad - w, nd.y - h / 2]);
          cands.push([nd.x - w / 2, nd.y + nd.r + pad]);
          cands.push([nd.x - w / 2, nd.y - nd.r - pad - h]);
        } else {
          cands.push([nd.x - nd.r - pad - w, nd.y - h / 2]);
        }
        let box = null;
        const forced = (focus && nd === focus) || (path && path.has(String(nd.id)));
        /* Подписи экранного кегля легко вылезают за край (на телефоне имя
           папки обрезалось). Прижимаем к свободной области и заново
           проверяем наложение уже на прижатой позиции. */
        const vp = viewport();
        const minXw = (vp.left - view.x) / k + 4 / k;
        const maxXw = (vp.left + vp.w - view.x) / k - 4 / k;
        for (const [bx, by] of cands) {
          const cx = Math.min(Math.max(bx, minXw), Math.max(minXw, maxXw - w));
          const b = { x: cx, y: by, w, h };
          if (forced || free(nd, b)) { box = b; break; }
        }
        if (!box) continue;
        boxes.push(box);
        const t = el('text', {
          x: box.x, y: box.y + h * 0.78,
          'font-size': fs,
          'font-weight': isGroup ? 600 : 400,
          fill: isGroup ? theme.labelStrong : theme.label,
          'pointer-events': isGroup ? 'auto' : 'none',
        });
        t.textContent = label;
        if (isGroup) {
          /* Имя папки — самый очевидный элемент для клика: у больших
             кластеров центр ореола занят заметками, и попасть в кружок
             папки мышью почти невозможно. Наведение не вешаем: hover
             пересобирает подписи, элемент под курсором исчезает, и клик
             не доходит. */
          t.style.cursor = 'pointer';
          t.addEventListener('click', ev => { ev.stopPropagation(); pick(nd); });
        }
        gLabels.appendChild(t);
      }
    } else if (gLabels.firstChild) {
      while (gLabels.firstChild) gLabels.removeChild(gLabels.firstChild);
    }
  }

  function hexA(hex, a) {    const h = String(hex || '#818cf8').replace('#', '');
    const v = h.length === 3 ? h.split('').map(c => c + c).join('') : h;
    const r = parseInt(v.slice(0, 2), 16) || 0;
    const g = parseInt(v.slice(2, 4), 16) || 0;
    const b = parseInt(v.slice(4, 6), 16) || 0;
    return `rgba(${r},${g},${b},${a})`;
  }

  /* ─────────────────────────── наведение и выбор ─────────────────────────── */

  function hover(nd, ev) {
    state.hovered = nd;
    state.neighbors.clear();
    if (nd) {
      if (nd.links) for (const rec of nd.links) {
        state.neighbors.add(rec.s.id);
        state.neighbors.add(rec.t.id);
      }
      if (nd.members) for (const m of nd.members) state.neighbors.add(m.id);
      if (state.onHover) state.onHover(nd, ev);
    }
    draw();
  }

  function pick(nd) {
    if (!nd) return;
    /* Неразрешённая ссылка — заметки ещё нет. В Obsidian клик по такой
       ссылке создаёт заметку, и это же делаем мы: иначе узел был
       тупиковым и только мешал. */
    if (nd.kind === 'unresolved') {
      if (state.onCreate && typeof state.onCreate === 'function') {
        state.onCreate(nd.data.target || nd.title, nd.id);
      }
      return;
    }
    state.selected = nd;
    hover(nd);
    if (state.onOpen && typeof state.onOpen === 'function') state.onOpen(nd.data, nd);
  }

  /* ─────────────────────────── панорама и зум ─────────────────────────── */

  /* Координаты сцены фиксированы (0..innerWidth/innerHeight), а панорама и
     зум применяются одним трансформом на общую группу. Так подписи и
     обводки масштабируются вместе с геометрией, а толщина линий не
     «жирнеет» при приближении. */
  const view = { x: 0, y: 0, k: 1 };
  let drag = null;
  let sceneG = null;

  function applyView() {
    if (!sceneG) return;
    sceneG.setAttribute('transform',
      `translate(${view.x} ${view.y}) scale(${view.k})`);
    // Обводка узлов не должна расти в пикселях при приближении.
    if (gNodes) gNodes.setAttribute('stroke-width', String(1 / view.k));
    if (gGroups) gGroups.setAttribute('stroke-width', String(1.6 / view.k));
  }

  function toWorld(ev) {
    const rect = svg.getBoundingClientRect();
    const src = ev.touches && ev.touches.length ? ev.touches[0] : ev;
    const px = (src.clientX || 0) - rect.left;
    const py = (src.clientY || 0) - rect.top;
    return { x: (px - view.x) / view.k, y: (py - view.y) / view.k };
  }

  /* Свободная от панелей часть окна: в ней центрируем граф, когда
     открыт навигатор слева или карточка заметки справа. */
  function viewport() {
    const i = state.insets || {};
    const left = Math.max(0, i.left || 0);
    const right = Math.max(0, i.right || 0);
    const top = Math.max(0, i.top || 0);
    const bottom = Math.max(0, i.bottom || 0);
    const w = Math.max(160, window.innerWidth - left - right);
    const h = Math.max(160, window.innerHeight - top - bottom);
    return { left, top, w, h, cx: left + w / 2, cy: top + h / 2 };
  }

  function clampZoom(k) {
    return Math.max(0.08, Math.min(6, k));
  }

  function nodeAt(w) {
    // Обратный порядок: узлы рисуются сверху, значит сверху и сверху
    // должны проверяться — иначе группа перехватывала бы клик по заметке.
    const list = state.nodes;
    for (let i = list.length - 1; i >= 0; i--) {
      const nd = list[i];
      if (!nd.visible) continue;
      if (nd.isGroup && !state.showGroups) continue;
      const dx = w.x - nd.x, dy = w.y - nd.y;
      if (dx * dx + dy * dy <= nd.r * nd.r * 1.3) return nd;
    }
    return null;
  }

  function onDown(ev) {
    const w = toWorld(ev);
    const nd = nodeAt(w);
    if (nd) {
      drag = { node: nd, moved: false, ox: w.x - nd.x, oy: w.y - nd.y };
      nd.fixed = true;
      svg.style.cursor = 'grabbing';
    } else {
      // Запоминаем экранные координаты начала: из них считаем сдвиг.
      const rect = svg.getBoundingClientRect();
      const src = ev.touches && ev.touches.length ? ev.touches[0] : ev;
      drag = {
        node: null, pan: true, moved: false,
        startX: (src.clientX || 0) - rect.left,
        startY: (src.clientY || 0) - rect.top,
        viewX: view.x, viewY: view.y,
      };
      svg.style.cursor = 'grabbing';
      if (ev.preventDefault) ev.preventDefault();
    }
  }

  function onMove(ev) {
    if (!drag) return;
    if (drag.node) {
      const w = toWorld(ev);
      drag.node.x = w.x - drag.ox;
      drag.node.y = w.y - drag.oy;
      drag.node.vx = 0;
      drag.node.vy = 0;
      drag.moved = true;
      draw();
      // Тянем узел — раскладка пересобирается, иначе он сразу прыгает
      // обратно под действием сил.
      alpha(Math.max(CONFIG.alphaMin, state.layoutTimer / CONFIG.relayoutFrames));
    } else if (drag.pan) {
      const rect = svg.getBoundingClientRect();
      const src = ev.touches && ev.touches.length ? ev.touches[0] : ev;
      const px = (src.clientX || 0) - rect.left;
      const py = (src.clientY || 0) - rect.top;
      view.x = drag.viewX + (px - drag.startX);
      view.y = drag.viewY + (py - drag.startY);
      if (Math.abs(px - drag.startX) > 2 || Math.abs(py - drag.startY) > 2) {
        drag.moved = true;
      }
      applyView();
    }
  }

  function onUp() {
    if (drag && drag.node) {
      drag.node.fixed = false;
      alpha(Math.max(CONFIG.alphaMin, state.layoutTimer / CONFIG.relayoutFrames));
    }
    drag = null;
    if (svg) svg.style.cursor = 'grab';
  }

  function onWheel(ev) {
    ev.preventDefault();
    // Зум к курсору: иначе граф уезжает из-под мыши и целиться в узел
    // приходится методом перебора.
    const before = toWorld(ev);
    const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
    const k = clampZoom(view.k * factor);
    view.k = k;
    const rect = svg.getBoundingClientRect();
    const src = ev.deltaMode === 1 ? null : ev;
    const px = (src.clientX != null ? src.clientX : (ev.clientX || 0)) - rect.left;
    const py = (src.clientY != null ? src.clientY : (ev.clientY || 0)) - rect.top;
    view.x = px - before.x * k;
    view.y = py - before.y * k;
    applyView();
    draw();   // набор подписей зависит от масштаба
  }

  function onBgClick(ev) {
    if (drag && drag.moved) return;
    const w = toWorld(ev);
    if (!nodeAt(w)) {
      state.selected = null;
      state.neighbors.clear();
      draw();
    }
  }

  function onDblClick(ev) {
    const w = toWorld(ev);
    const nd = nodeAt(w);
    if (nd) pick(nd);
  }

  /* ─────────────────────────── управление ─────────────────────────── */

  function setOption(key, value) {
    state[key] = value;
    if (key === 'showGroups' || key === 'showLabels') { render(); return; }
    if (key === 'hiddenLinkTypes') { state.hiddenTypes = new Set(value || []); render(); return; }
    if (key === 'mode') {
      /* Переключение раскладки — тоже смена сцены: колонки дерева при
         прежнем масштабе уезжали за правый край и «дерево» выглядело
         пустым. Вписываем сразу и ещё раз после остывания раскладки. */
      layoutTree();
      alpha(1);
      pendingFit = true;
      fit();
      return;
    }
    if (key === 'filterText' || key === 'onlyConnected' || key === 'sectionFilter') { applyFilter(); return; }
    if (key === 'insets') return;    // читается при следующем fit/focus
  }

  function applyFilter() {
    const q = String(state.filterText || '').trim().toLowerCase();
    const hasText = q.length > 0;
    for (const nd of state.nodes) {
      if (nd.isGroup) {
        // Группа видна, если видно хотя бы одно совпадение внутри неё.
        const anyMember = (nd.members || []).some(m => m.visible);
        nd.visible = !hasText && !state.onlyConnected ? true : anyMember;
        continue;
      }
      if (hasText) {
        const hay = (nd.title + ' ' + (nd.data.description || '') + ' ' +
                     (nd.data.tags || []).join(' ')).toLowerCase();
        nd.visible = hay.indexOf(q) !== -1;
      } else {
        nd.visible = true;
      }
      if (state.onlyConnected && nd.visible) {
        const conn = (nd.links || []).some(r => r.s.visible && r.t.visible);
        nd.visible = conn;
      }
    }
    /* Фильтр по папке. В 3D он прятал меши, а в 2D до этого только
       подсвечивал пункт легенды — граф не менялся вовсе. */
    if (state.sectionFilter) {
      const secName = state.sectionFilter;
      for (const nd of state.nodes) {
        if (nd.isGroup) {
          const own = nd.data.section === secName;
          const inside = (nd.members || []).some(m => m.data.section === secName);
          nd.visible = nd.visible && (own || inside);
          if (inside) {
            for (const m of nd.members || []) {
              if (m.data.section === secName) m.visible = true;
            }
          }
        } else {
          nd.visible = nd.visible && nd.data.section === secName;
        }
      }
    }
    render();
    alpha(Math.max(CONFIG.alphaMin, state.layoutTimer / CONFIG.relayoutFrames));
  }

  function focusNode(id) {
    const nd = state.byId.get(id);
    if (!nd) return false;
    // Переводим узел в центр свободной части экрана с поправкой на масштаб.
    const vp = viewport();
    view.x = vp.cx - nd.x * view.k;
    view.y = vp.cy - nd.y * view.k;
    state.selected = nd;
    hover(nd);
    applyView();
    return true;
  }

  /* Кнопки «+ / −» в доке. Масштабируем вокруг центра свободной области,
     а не угла окна: иначе граф уползает под панель. */
  function zoomBy(factor) {
    const vp = viewport();
    const k = clampZoom(view.k * factor);
    view.x = vp.cx - ((vp.cx - view.x) / view.k) * k;
    view.y = vp.cy - ((vp.cy - view.y) / view.k) * k;
    view.k = k;
    applyView();
    draw();   // набор подписей зависит от масштаба
  }

  function fitList(vis, pad) {
    if (!vis.length) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const nd of vis) {
      minX = Math.min(minX, nd.x - nd.r); maxX = Math.max(maxX, nd.x + nd.r);
      minY = Math.min(minY, nd.y - nd.r); maxY = Math.max(maxY, nd.y + nd.r);
    }
    const vp = viewport();
    const w = Math.max(1, maxX - minX + pad * 2);
    const h = Math.max(1, maxY - minY + pad * 2);
    const k = clampZoom(Math.min(2.2, Math.min(vp.w / w, vp.h / h)));
    view.k = k;
    // Центрируем в свободной части окна: сдвиг это минус центр сцены.
    view.x = vp.cx - ((minX + maxX) / 2) * k;
    view.y = vp.cy - ((minY + maxY) / 2) * k;
    applyView();
    draw();   // после вписывания масштаб другой — пересобираем подписи
  }

  function fit() {
    fitList(state.nodes.filter(n => n.visible && !n.isGroup), 70);
  }

  /* Вписать конкретную цепочку (например, найденный путь): вокруг неё
     остаётся больше воздуха, чтобы соседние кластеры не отвлекали. */
  function fitTo(ids) {
    const set = new Set((ids || []).map(String));
    if (!set.size) { fit(); return; }
    fitList(state.nodes.filter(n => set.has(String(n.id)) && n.visible && !n.isGroup), 120);
  }

  /* Путь: ids — узлы цепочки, pairs — пары [от, до] последовательных
     шагов. Связи ищем по тем же рёбрам, что уже лежат в графе, поэтому
     подсветка совпадает с тем, что нарисовано. */
  function setPath(ids, pairs) {
    if (!ids || !ids.length) { clearPath(); return false; }
    state.path = new Set(ids.map(String));
    const keys = new Set((pairs || []).map(p => p[0] + '→' + p[1]));
    state.pathLinks = new Set(state.links.filter(rec =>
      keys.has(rec.s.id + '→' + rec.t.id) || keys.has(rec.t.id + '→' + rec.s.id)));
    state.selected = null;
    state.hovered = null;
    state.neighbors.clear();
    draw();
    return true;
  }

  function clearPath() {
    state.path = null;
    state.pathLinks = null;
    draw();
  }

  function reheat() { alpha(1); }

  function stats() {
    return {
      nodes: state.nodes.filter(n => !n.isGroup).length,
      groups: state.nodes.filter(n => n.isGroup).length,
      links: state.links.length,
    };
  }

  /* ─────────────────────────── тема ─────────────────────────── */

  function applyGraph2DTheme() {
    /* Светлоту берём из --bg-deep темы, а не из backgroundColor у body:
       body прозрачен (фон рисует канвас/обёртка), поэтому проверка яркости
       фона body не срабатывала, и на светлых темах граф оставался с
       тёмной палитрой — подписи #dfe3f0 на фоне #eef1f6 почти не читались,
       а пастельные рёбра сливались с подложкой. */
    let lum = luminance(cssVar('--bg-deep', ''));
    if (lum == null) lum = luminance(cssVar('--bg-mid', ''));
    if (lum == null) lum = luminance(getComputedStyle(document.body).backgroundColor);
    const dark = lum == null ? true : lum < 0.55;

    theme.dark = dark;
    theme.nodeStroke = dark ? 'rgba(255,255,255,0.18)' : 'rgba(15,23,42,0.38)';
    theme.label = cssVar('--text-dim', dark ? '#9aa3bf' : '#475569');
    theme.labelStrong = cssVar('--text', dark ? '#dfe3f0' : '#0f172a');
    theme.nodeFill = dark ? (hex => hex) : (hex => shade(hex, 0.32));

    const pal = dark ? LINK_PALETTE.dark : LINK_PALETTE.light;
    for (const k in LINK_STYLE) {
      if (pal[k]) LINK_STYLE[k].color = pal[k];
    }

    if (gLinks) gLinks.setAttribute('color', theme.label);
    /* theme нужен и без SVG: по нему легенда страницы выбирает цвета
       точек, пока граф ещё не переключён в 2D. */
    if (svg) render();
  }

  function displayColor(hex) {
    return theme.nodeFill ? theme.nodeFill(hex) : hex;
  }

  function linkColor(type) {
    return (LINK_STYLE[type] || LINK_STYLE.semantic).color;
  }

  /* ─────────────────────────── публичный интерфейс ─────────────────────────── */

  global.Graph2D = {
    init,
    setData,
    setOption,
    focusNode,
    fit,
    fitTo,
    setPath,
    clearPath,
    reheat,
    zoomBy,
    stats,
    applyTheme: applyGraph2DTheme,
    displayColor,
    linkColor,
    LINK_STYLE,
  };
})(window);