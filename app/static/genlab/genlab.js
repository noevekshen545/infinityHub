/* ==========================================================================
   GenLab — клиентская логика (ES-модуль, без сборки).

   Разделы:
     1. Утилиты и состояние
     2. HTTP-клиент и уведомления
     3. Навигация и пресеты
     4. Форма параметров (строится из описания полей пресета)
     5. Галерея
     6. Холст результата (картинка / видео / звук / 3D)
     7. Задания и WebSocket
     8. ComfyUI: статус, старт/стоп, лог, каталог моделей
     9. Запуск

   Дизайн-токены берутся из design.css, поэтому отдельная тема не нужна:
   достаточно переключить класс body.dark на существующем переключателе.
   ========================================================================== */

const API = '/api/genlab';

/* ----------------------------------------------------------- 1. Утилиты */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

const state = {
  groups: [],
  presets: [],
  activeGroup: 'image',
  activePreset: null,
  fields: [],
  values: {},
  jobs: new Map(),
  assets: [],
  activeAsset: null,
  comfy: null,
  packs: [],
  capabilities: {},
  problems: [],
  galleryFilter: '',
  objectInfo: null,
};

function fmtBytes(bytes) {
  if (!bytes && bytes !== 0) return '—';
  let v = Number(bytes);
  const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + ' ' + units[i];
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(Number(ts) * 1000);
  return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

function debounce(fn, ms = 220) {
  let t = 0;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* -------------------------------------------------- 2. HTTP и уведомления */

async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(API + path, opts);
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok || (data && data.ok === false)) {
    const message = (data && data.error) || ('HTTP ' + res.status);
    const err = new Error(message);
    err.data = data;
    err.status = res.status;
    throw err;
  }
  return data || {};
}

function toast(message, kind = 'info', ttl = 4200) {
  const box = $('glToasts');
  const node = el('div', 'gl-toast gl-toast--' + kind, message);
  box.appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .2s';
    setTimeout(() => node.remove(), 220);
  }, ttl);
}

/* ------------------------------------------------------ 3. Навигация */

function renderNav() {
  const nav = $('glNav');
  nav.textContent = '';
  const extras = [
    { id: 'gallery', title: 'Галерея', icon: 'fa-images' },
    { id: 'models', title: 'Модели', icon: 'fa-database' },
  ];
  const items = [
    ...state.groups.map((g) => ({ id: g.id, title: g.title, icon: g.icon, count: g.count })),
    { id: '_sep', title: '' },
    ...extras,
  ];
  items.forEach((item) => {
    if (item.id === '_sep') { nav.appendChild(el('hr', 'gl-nav-sep')); return; }
    const btn = el('button', 'gl-nav-item');
    btn.type = 'button';
    btn.dataset.group = item.id;
    const icon = el('span', 'gl-nav-icon');
    icon.innerHTML = '<i class="fas ' + escapeHtml(item.icon || 'fa-cube') + '"></i>';
    btn.appendChild(icon);
    const label = el('span', 'gl-nav-label', item.title);
    btn.appendChild(label);
    if (item.count != null) btn.appendChild(el('span', 'gl-nav-count', item.count));
    btn.addEventListener('click', () => selectGroup(item.id));
    nav.appendChild(btn);
  });
  markNav();
}

function markNav() {
  document.querySelectorAll('.gl-nav-item').forEach((btn) => {
    btn.classList.toggle('is-active', btn.dataset.group === state.activeGroup);
  });
}

function selectGroup(groupId) {
  const previous = state.activeGroup;
  state.activeGroup = groupId;
  markNav();
  if (groupId === 'gallery') { renderGalleryView(); return; }
  if (groupId === 'models') {
    openModelsModal();
    if (previous !== 'models') selectGroup(previous);
    return;
  }

  const group = state.groups.find((g) => g.id === groupId);
  const list = state.presets.filter((p) => p.group === groupId);
  $('glViewTitle').textContent = (group && group.title) || groupId;
  $('glViewDesc').textContent = list.length + ' пресетов в этой категории';
  $('glPresetsTitle').textContent = 'Пресеты';
  renderPresets(list);
  if (list.length) selectPreset(list[0].id);
}

function previousGroup() {
  const ids = state.groups.map((g) => g.id);
  const idx = ids.indexOf(state.activeGroup);
  return ids[Math.max(0, idx - 1)] || ids[0];
}

/* ------------------------------------------------------ Пресеты */

function renderPresets(list) {
  const box = $('glPresets');
  box.textContent = '';
  if (!list.length) {
    box.appendChild(el('div', 'gl-presets-empty', 'В этой категории пока нет пресетов'));
    return;
  }
  list.forEach((preset) => {
    const btn = el('button', 'gl-preset');
    btn.type = 'button';
    btn.dataset.preset = preset.id;
    const icon = el('span', 'gl-preset-icon');
    icon.innerHTML = '<i class="fas ' + escapeHtml(preset.icon || 'fa-cube') + '"></i>';
    const body = el('div', 'gl-preset-body');
    body.appendChild(el('div', 'gl-preset-title', preset.title));
    body.appendChild(el('div', 'gl-preset-desc', preset.description || ''));
    const cost = el('span', 'gl-cost gl-cost--' + (preset.cost || 'medium'),
      preset.cost === 'low' ? 'быстро' : preset.cost === 'high' ? 'тяжело' : 'средне');
    body.appendChild(cost);
    btn.appendChild(icon);
    btn.appendChild(body);
    btn.addEventListener('click', () => selectPreset(preset.id));
    box.appendChild(btn);
  });
}

function selectPreset(presetId) {
  const preset = state.presets.find((p) => p.id === presetId);
  if (!preset) return;
  state.activePreset = preset;
  state.fields = preset.fields || [];
  state.values = {};
  state.fields.forEach((f) => { state.values[f.name] = f.default; });
  state.problems = [];

  $('glViewTitle').textContent = preset.title;
  $('glViewDesc').textContent = preset.description || '';
  $('glPresetCost').textContent = { low: 'быстро', medium: 'средне', high: 'тяжело' }[preset.cost] || '';
  $('glRunBtn').disabled = !state.comfy || !state.comfy.online;

  document.querySelectorAll('.gl-preset').forEach((node) => {
    node.classList.toggle('is-active', node.dataset.preset === presetId);
  });

  renderForm();
  renderQueueBar();
  paintEmptyState();
  loadModelsIntoForm();
}

function resetForm() {
  if (!state.activePreset) return;
  selectPreset(state.activePreset.id);
}

/* ------------------------------------------- 4. Форма параметров */

function renderForm() {
  const box = $('glForm');
  box.textContent = '';
  if (!state.activePreset) {
    box.appendChild(el('div', 'gl-presets-empty', 'Выберите пресет'));
    return;
  }
  state.fields.forEach((field) => box.appendChild(buildField(field)));
  applyConditions();
}

function buildField(field) {
  const wrap = el('div', 'gl-field');
  wrap.dataset.field = field.name;

  const value = state.values[field.name];
  const label = el('label', 'gl-label');
  label.appendChild(el('span', null, field.label || field.name));
  const valueTag = el('span', 'gl-label-value');
  label.appendChild(valueTag);
  wrap.appendChild(label);

  let control;
  switch (field.type) {
    case 'textarea':
      control = el('textarea', 'gl-textarea');
      control.value = value == null ? '' : value;
      control.addEventListener('input', () => setValue(field.name, control.value));
      break;

    case 'number':
      control = el('input', 'gl-input');
      control.type = 'number';
      if (field.min != null) control.min = field.min;
      if (field.max != null) control.max = field.max;
      if (field.step != null) control.step = field.step;
      control.value = value == null ? '' : value;
      control.addEventListener('input', () => setValue(field.name, numberOrNull(control.value)));
      break;

    case 'slider': {
      control = el('input', 'gl-range');
      control.type = 'range';
      control.min = field.min != null ? field.min : 0;
      control.max = field.max != null ? field.max : 1;
      control.step = field.step != null ? field.step : 0.01;
      control.value = value == null ? control.min : value;
      control.addEventListener('input', () => {
        const v = Number(control.value);
        setValue(field.name, v);
        valueTag.textContent = formatNumber(v);
      });
      valueTag.textContent = formatNumber(Number(control.value));
      break;
    }

    case 'select': {
      control = el('select', 'gl-select');
      const opts = field.options || [];
      opts.forEach((opt) => control.appendChild(new Option(opt, opt)));
      control.value = value == null ? '' : value;
      control.addEventListener('change', () => setValue(field.name, control.value));
      break;
    }

    case 'checkbox': {
      control = el('input');
      control.type = 'checkbox';
      control.checked = Boolean(value);
      control.addEventListener('change', () => setValue(field.name, control.checked));
      const row = el('label', 'gl-check');
      row.appendChild(control);
      row.appendChild(el('span', null, field.label || ''));
      wrap.textContent = '';
      wrap.appendChild(row);
      if (field.hint) wrap.appendChild(el('div', 'gl-hint', field.hint));
      wrap.dataset.field = field.name;
      return wrap;
    }

    case 'seed': {
      control = el('input', 'gl-input');
      control.type = 'number';
      control.min = -1;
      control.value = value == null ? -1 : value;
      control.addEventListener('input', () => setValue(field.name, numberOrNull(control.value)));
      const row = el('div', 'gl-seed-row');
      row.appendChild(control);
      const dice = el('button', 'gl-icon-btn');
      dice.type = 'button';
      dice.title = 'Случайный seed';
      dice.innerHTML = '<i class="fas fa-dice"></i>';
      dice.addEventListener('click', () => {
        const v = Math.floor(Math.random() * 2 ** 40);
        control.value = v;
        setValue(field.name, v);
        valueTag.textContent = String(v);
      });
      row.appendChild(dice);
      wrap.removeChild(label);
      wrap.insertBefore(row, wrap.firstChild);
      break;
    }

    case 'file': {
      wrap.textContent = '';
      const row = el('label', 'gl-file');
      const icon = el('i', 'fas fa-cloud-arrow-up');
      row.appendChild(icon);
      const name = el('span', 'gl-file-name', 'Выбрать файл…');
      row.appendChild(name);
      const input = el('input');
      input.type = 'file';
      input.accept = field.accept || '*/*';
      input.addEventListener('change', () => onPickFile(field, input.files[0], name));
      row.appendChild(input);
      wrap.appendChild(row);
      wrap.appendChild(el('div', 'gl-hint', field.hint || ('Форматы: ' + (field.accept || 'любые'))));
      wrap.dataset.field = field.name;
      return wrap;
    }

    case 'hidden':
      return wrap;

    default:
      control = el('input', 'gl-input');
      control.type = 'text';
      control.placeholder = field.placeholder || '';
      control.value = value == null ? '' : value;
      control.addEventListener('input', () => setValue(field.name, control.value));
  }

  wrap.appendChild(control);
  if (field.placeholder && field.type !== 'textarea') {
    control.placeholder = field.placeholder;
  }
  if (field.hint) wrap.appendChild(el('div', 'gl-hint', field.hint));
  return wrap;
}

function formatNumber(v) {
  if (!isFinite(v)) return '—';
  return Math.abs(v) >= 100 || Number.isInteger(v) ? String(v) : v.toFixed(2);
}

function numberOrNull(text) {
  const v = Number(text);
  return text === '' || Number.isNaN(v) ? null : v;
}

function setValue(name, value) {
  state.values[name] = value;
  state.problems = [];
  const box = $('glQueueBar');
  if (box) box.textContent = '';
}

async function onPickFile(field, file, nameNode) {
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  form.append('kind', field.accept && field.accept.includes('audio') ? 'audio'
    : field.accept && field.accept.includes('video') ? 'video' : 'image');
  nameNode.textContent = 'Загрузка…';
  try {
    const res = await api('/upload', { method: 'POST', body: form });
    const comfyName = res.comfy_name || file.name;
    state.values[field.name] = comfyName;
    nameNode.textContent = file.name;
    nameNode.classList.add('is-set');
    if (res.upload_error) {
      toast('Файл сохранён локально, но ComfyUI его не принял: ' + res.upload_error, 'warn', 7000);
    }
    toast('Файл загружен: ' + file.name, 'ok', 2600);
  } catch (e) {
    nameNode.textContent = 'Ошибка загрузки';
    toast('Не удалось загрузить файл: ' + e.message, 'error', 7000);
  }
}

/** Скрыть поля, у которых условие when не выполнено. */
function applyConditions() {
  state.fields.forEach((field) => {
    const wrap = document.querySelector('.gl-field[data-field="' + cssEscape(field.name) + '"]');
    if (!wrap) return;
    if (!field.when) { wrap.hidden = false; return; }
    let ok = true;
    Object.entries(field.when).forEach(([key, want]) => {
      if (state.values[key] !== want) ok = false;
    });
    wrap.hidden = !ok;
  });
}

function cssEscape(name) {
  return String(name).replace(/["\\]/g, '\\$&');
}

/**
 * Подставить реальные списки весов в select-поля моделей.
 * Поле считается «полем модели», если его имя есть в MODEL_FOLDERS.
 */
const MODEL_FOLDERS = {
  ckpt: 'checkpoints',
  unet: 'diffusion_models',
  shape_model: 'diffusion_models',
  texture_model: 'diffusion_models',
  clip: 'text_encoders',
  clip1: 'text_encoders',
  clip2: 'text_encoders',
  vae: 'vae',
  lora: 'loras',
  control_net: 'controlnet',
  clip_vision: 'clip_vision',
};

async function loadModelsIntoForm() {
  const wanted = state.fields
    .map((f) => MODEL_FOLDERS[f.name])
    .filter(Boolean);
  if (!wanted.length) return;

  let lists;
  try {
    const res = await api('/models');
    lists = res.counts || {};
  } catch {
    return;
  }

  state.fields.forEach((field) => {
    const folder = MODEL_FOLDERS[field.name];
    if (!folder) return;
    const control = document.querySelector(
      '.gl-field[data-field="' + cssEscape(field.name) + '"] select');
    if (!control) return;

    const files = Array.isArray(lists[folder]) ? lists[folder] : [];
    const current = state.values[field.name] || control.value;
    control.textContent = '';
    if (!files.length) {
      control.appendChild(new Option('— в models/' + folder + ' нет файлов —', ''));
      control.disabled = true;
      state.values[field.name] = '';
      return;
    }
    control.disabled = false;
    control.appendChild(new Option('— выберите —', ''));
    files.forEach((f) => control.appendChild(new Option(f, f)));
    control.value = files.includes(current) ? current : '';
    state.values[field.name] = control.value || '';
  });
}

/* ------------------------------------------------------ 5. Галерея */

async function loadGallery(reset = true) {
  const params = new URLSearchParams();
  params.set('limit', '200');
  if (state.galleryFilter) params.set('search', state.galleryFilter);
  const res = await api('/assets?' + params.toString());
  state.assets = res.assets || [];
  $('glGalleryCount').textContent = res.total != null ? res.total : state.assets.length;
  renderGallery();
  if (reset) renderActiveAsset();
}

function renderGallery() {
  const box = $('glGallery');
  box.textContent = '';
  if (!state.assets.length) {
    box.appendChild(el('div', 'gl-gallery-empty',
      state.galleryFilter ? 'Ничего не найдено' : 'Здесь появятся результаты'));
    return;
  }
  state.assets.forEach((asset) => {
    const tile = el('div', 'gl-tile');
    tile.dataset.asset = asset.id;
    if (state.activeAsset && state.activeAsset.id === asset.id) tile.classList.add('is-active');

    if (asset.media_type === 'image') {
      const img = el('img');
      img.src = asset.preview_url;
      img.alt = asset.name;
      img.loading = 'lazy';
      tile.appendChild(img);
    } else {
      const ph = el('div', 'gl-empty');
      ph.style.padding = '28px 8px';
      const icons = { audio: 'fa-wave-square', video: 'fa-clapperboard', model3d: 'fa-cube', file: 'fa-file' };
      ph.innerHTML = '<div class="gl-empty-mark" style="width:40px;height:40px;font-size:15px;margin-bottom:6px">'
        + '<i class="fas ' + (icons[asset.media_type] || 'fa-file') + '"></i></div>';
      tile.appendChild(ph);
    }

    const badge = el('div', 'gl-tile-badge');
    badge.innerHTML = '<i class="fas ' + badgeIcon(asset.media_type) + '"></i> '
      + escapeHtml(fmtTime(asset.created_at));
    tile.appendChild(badge);

    const fav = el('button', 'gl-tile-fav' + (asset.favorite ? ' is-on' : ''));
    fav.type = 'button';
    fav.title = asset.favorite ? 'Убрать из избранного' : 'В избранное';
    fav.innerHTML = '<i class="fas fa-star"></i>';
    fav.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      try {
        await api('/assets/' + asset.id, { method: 'POST', body: { action: 'favorite' } });
        loadGallery(false);
      } catch (e) { toast(e.message, 'error'); }
    });
    tile.appendChild(fav);

    tile.addEventListener('click', () => openAsset(asset.id));
    box.appendChild(tile);
  });
}

function badgeIcon(mediaType) {
  return { image: 'fa-image', audio: 'fa-music', video: 'fa-film', model3d: 'fa-cube' }[mediaType] || 'fa-file';
}

function renderActiveAsset() {
  const editBtn = $('glEditBtn');
  if (!state.activeAsset) { if (editBtn) editBtn.hidden = true; $('glAudioEditBtn').hidden = true; return; }
  const canvas = $('glCanvas');
  const image = $('glResultImage');
  const video = $('glResultVideo');
  const audio = $('glResultAudio');
  const model = $('glResultModel');
  [image, video, audio, model].forEach((n) => n.classList.add('gl-hidden'));
  $('glEmpty').classList.add('gl-hidden');
  canvas.classList.remove('has-alpha');

  const url = state.activeAsset.url + '?t=' + Date.now();
  const kind = state.activeAsset.media_type;
  if (kind === 'image') {
    image.src = url;
    image.classList.remove('gl-hidden');
  } else if (kind === 'video') {
    video.src = url;
    video.classList.remove('gl-hidden');
  } else if (kind === 'audio') {
    audio.src = url;
    audio.classList.remove('gl-hidden');
  } else if (kind === 'model3d') {
    model.classList.remove('gl-hidden');
    mountViewer(model, url);
  } else {
    $('glEmpty').classList.remove('gl-hidden');
    $('glEmpty').querySelector('h2').textContent = 'Файл без предпросмотра';
    $('glEmpty').querySelector('p').textContent = state.activeAsset.name;
  }
  document.querySelectorAll('.gl-tile').forEach((t) => {
    t.classList.toggle('is-active', t.dataset.asset === state.activeAsset.id);
  });
  if (editBtn) editBtn.hidden = !(kind === 'image');
  $('glAudioEditBtn').hidden = !(kind === 'audio');
}

async function openAsset(assetId) {
  const found = state.assets.find((a) => a.id === assetId);
  if (!found) return;
  state.activeAsset = found;
  renderActiveAsset();
}

/* ------------------------------------------- 6. 3D-просмотр (three.js) */

let viewer = null;

async function mountViewer(canvas, url) {
  if (!viewer) {
    try {
      viewer = await createViewer(canvas);
    } catch (e) {
      toast('3D-просмотр недоступен: ' + e.message, 'warn', 6000);
      $('glResultModel').classList.add('gl-hidden');
      return;
    }
  }
  viewer.load(url);
}

async function createViewer(canvas) {
  // three.js вендорен в app/vendor и раздаётся корнем сайта (/vendor/...),
  // поэтому путь абсолютный — относительный сломался бы в любой подстранице.
  const THREE = await import('/vendor/three/build/three.module.js');
  const { OrbitControls } = await import('/vendor/addons/controls/OrbitControls.js');
  const { GLTFLoader } = await import('/vendor/addons/loaders/GLTFLoader.js');

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xf7f8fa);
  const camera = new THREE.PerspectiveCamera(50, 1, 0.01, 5000);
  camera.position.set(2.2, 1.6, 2.6);

  const hemi = new THREE.HemisphereLight(0xffffff, 0xdfe3e8, 2.0);
  scene.add(hemi);
  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(3, 5, 2);
  scene.add(key);

  const grid = new THREE.GridHelper(10, 20, 0xd3d9e0, 0xe3e7ec);
  scene.add(grid);

  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;

  const root = new THREE.Group();
  scene.add(root);

  const loader = new GLTFLoader();
  let current = null;
  let spin = true;

  function resize() {
    const w = canvas.clientWidth || 640;
    const h = canvas.clientHeight || 480;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(canvas);
  resize();

  (function loop() {
    requestAnimationFrame(loop);
    if (spin && current) current.rotation.y += 0.004;
    controls.update();
    renderer.render(scene, camera);
  })();

  function load(src) {
    loader.load(src, (gltf) => {
      if (current) {
        root.remove(current);
        current.traverse((o) => { if (o.geometry) o.geometry.dispose(); });
      }
      current = gltf.scene;
      root.add(current);
      const box = new THREE.Box3().setFromObject(current);
      const size = box.getSize(new THREE.Vector3());
      const center = box.getCenter(new THREE.Vector3());
      const radius = Math.max(0.4, Math.max(size.x, size.y, size.z) / 2);
      camera.position.set(center.x + radius * 1.6, center.y + radius * 1.2, center.z + radius * 1.8);
      camera.near = radius / 100;
      camera.far = radius * 100;
      camera.updateProjectionMatrix();
      controls.target.copy(center);
      controls.update();
      grid.position.y = box.min.y;
    }, undefined, (err) => {
      toast('Не удалось загрузить 3D-модель: ' + err, 'error', 7000);
    });
  }

  return { load };
}

/* ------------------------------------------------- 7. Задания и WebSocket */

function renderJobs() {
  const list = Array.from(state.jobs.values()).slice(0, 40);
  const box = $('glJobs');
  box.textContent = '';
  if (!list.length) {
    box.appendChild(el('div', 'gl-muted', 'Заданий пока нет'));
    return;
  }
  list.forEach((job) => {
    const card = el('div', 'gl-jobcard');
    const head = el('div', 'gl-jobcard-head');
    const status = el('span', 'gl-status gl-status--' + job.status, statusLabel(job.status));
    head.appendChild(status);
    head.appendChild(el('span', 'gl-muted', fmtTime(job.created_at)));
    card.appendChild(head);
    card.appendChild(el('div', 'gl-jobcard-prompt',
      (job.params && job.params.prompt) || job.preset || ''));

    const bar = el('div', 'gl-job-bar');
    const fill = el('i');
    fill.style.width = (job.progress || 0) + '%';
    bar.appendChild(fill);
    card.appendChild(bar);

    const active = job.status === 'running' || job.status === 'queued';
    if (active || job.step || job.error) {
      const meta = el('div', 'gl-job-meta');
      meta.appendChild(el('span', 'gl-job-step', job.error || job.step || statusLabel(job.status)));
      if (active) meta.appendChild(el('span', 'gl-job-pct', (job.progress || 0) + '%'));
      card.appendChild(meta);
    }

    if (job.status === 'done') {
      const actions = el('div', 'gl-actions-row');
      const save = el('button', 'gl-btn gl-btn-ghost');
      save.innerHTML = '<i class="fas fa-download"></i> В галерею';
      save.addEventListener('click', () => saveOutputs(job.id));
      actions.appendChild(save);
      const again = el('button', 'gl-btn gl-btn-ghost');
      again.innerHTML = '<i class="fas fa-rotate-right"></i> Ещё раз';
      again.addEventListener('click', () => retryJob(job.id));
      actions.appendChild(again);
      card.appendChild(actions);
    } else if (active) {
      const actions = el('div', 'gl-actions-row');
      const cancel = el('button', 'gl-btn gl-btn-ghost');
      cancel.innerHTML = '<i class="fas fa-xmark"></i> Отменить';
      cancel.addEventListener('click', () => cancelJob(job.id));
      actions.appendChild(cancel);
      card.appendChild(actions);
    } else {
      const actions = el('div', 'gl-actions-row');
      const again = el('button', 'gl-btn gl-btn-ghost');
      again.innerHTML = '<i class="fas fa-rotate-right"></i> Повторить';
      again.addEventListener('click', () => retryJob(job.id));
      actions.appendChild(again);
      card.appendChild(actions);
    }
    box.appendChild(card);
  });
}

function statusLabel(status) {
  return { queued: 'очередь', running: 'генерация', done: 'готово', error: 'ошибка', cancelled: 'отменено' }[status] || status;
}

function renderQueueBar() {
  const box = $('glQueueBar');
  box.textContent = '';
  state.problems.forEach((problem) => {
    const node = el('div', 'gl-problem gl-problem--' + problem.severity);
    node.textContent = problem.message;
    if (problem.hint) {
      const hint = el('div', 'gl-problem-hint', problem.hint);
      node.appendChild(hint);
    }
    box.appendChild(node);
  });
  Array.from(state.jobs.values())
    .filter((j) => j.status === 'running' || j.status === 'queued')
    .slice(0, 4)
    .forEach((job) => {
      const node = el('div', 'gl-job');
      if (job.status === 'error') node.classList.add('is-error');
      if (job.status === 'cancelled') node.classList.add('is-cancelled');
      const icon = el('div', 'gl-job-icon');
      icon.innerHTML = '<i class="fas ' + (job.status === 'error' ? 'fa-triangle-exclamation' : 'fa-spinner fa-spin') + '"></i>';
      node.appendChild(icon);
      const main = el('div', 'gl-job-main');
      main.appendChild(el('div', 'gl-job-title', job.title || job.preset));
      main.appendChild(el('div', 'gl-job-step', job.step || statusLabel(job.status)));
      const bar = el('div', 'gl-job-bar');
      const fill = el('i');
      fill.style.width = (job.progress || 0) + '%';
      bar.appendChild(fill);
      main.appendChild(bar);
      node.appendChild(main);
      const actions = el('div', 'gl-job-actions');
      const cancel = el('button', 'gl-mini');
      cancel.innerHTML = '<i class="fas fa-xmark"></i>';
      cancel.title = 'Отменить';
      cancel.addEventListener('click', () => cancelJob(job.id));
      actions.appendChild(cancel);
      node.appendChild(actions);
      box.appendChild(node);
    });
}

function upsertJob(job) {
  if (!job || !job.id) return;
  state.jobs.set(job.id, Object.assign({}, state.jobs.get(job.id) || {}, job));
  renderJobs();
  renderQueueBar();
  updateQueueBadge();
  maybeAutoCollect(job);
}

/* Готовый результат сам появляется на холсте — чтобы сразу после генерации
   можно было выделить область и перерисовать её, без кнопки «В галерею».
   Сбор идемпотентен и на сервере, но всё равно помним уже собранные задания:
   upsertJob вызывается на каждом обновлении прогресса. */
const autoCollected = new Set();

function maybeAutoCollect(job) {
  if (!job || job.status !== 'done' || autoCollected.has(job.id)) return;
  autoCollected.add(job.id);
  (async () => {
    try {
      const res = await api('/jobs/' + job.id + '/outputs');
      if (!(res.saved || []).length) return;
      await loadGallery(false);
      state.activeAsset = res.saved[0];
      renderActiveAsset();
    } catch { /* тихо: файл можно сохранить и вручную кнопкой */ }
  })();
}

function updateQueueBadge() {
  const active = Array.from(state.jobs.values())
    .filter((j) => j.status === 'running' || j.status === 'queued').length;
  $('glQueue').hidden = active === 0;
  $('glQueueBadge').textContent = String(active);
  $('glQueueText').textContent = active === 1 ? 'задание в работе' : 'заданий в работе';
}

async function cancelJob(jobId) {
  try {
    await api('/jobs/' + jobId + '/cancel', { method: 'POST' });
    toast('Задание отменено', 'ok', 2500);
  } catch (e) { toast(e.message, 'error'); }
}

async function retryJob(jobId) {
  const job = state.jobs.get(jobId);
  if (!job) return;
  try {
    const res = await api('/generate', {
      method: 'POST',
      body: { preset: job.preset, params: job.params || {}, title: job.title || job.preset },
    });
    if (res.job) upsertJob(res.job);
    toast('Задание запущено повторно', 'ok', 2500);
  } catch (e) { toast('Не удалось повторить: ' + e.message, 'error', 7000); }
}

async function saveOutputs(jobId) {
  try {
    const res = await api('/jobs/' + jobId + '/outputs');
    toast('Сохранено в галерею: ' + (res.saved || []).length, 'ok');
    if ((res.errors || []).length) {
      toast('Часть файлов не удалось скачать: ' + res.errors.join('; '), 'warn', 8000);
    }
    await loadGallery();
    if ((res.saved || []).length) {
      state.activeAsset = res.saved[0];
      renderActiveAsset();
    }
  } catch (e) { toast(e.message, 'error', 7000); }
}

/* ------------------------------------------------ 8. ComfyUI и модели */

function paintComfy(comfy) {
  state.comfy = comfy;
  const dot = $('glComfyDot');
  const label = $('glComfyLabel');
  dot.className = 'gl-dot';

  if (!comfy.comfy_available) {
    dot.classList.add('is-offline');
    label.textContent = 'ComfyUI не найден';
  } else if (comfy.online) {
    const busy = (comfy.jobs && (comfy.jobs.running || 0)) > 0;
    dot.classList.add(busy ? 'is-busy' : 'is-online');
    const vram = comfy.devices && comfy.devices[0] && comfy.devices[0].vram_total
      ? ' · ' + fmtBytes(comfy.devices[0].vram_total) : '';
    label.textContent = 'ComfyUI ' + (comfy.version || '') + vram;
  } else {
    dot.classList.add('is-offline');
    label.textContent = 'ComfyUI выключен';
  }

  $('glRunBtn').disabled = !(comfy && comfy.online);
  paintEmptyState();
}

async function refreshStatus() {
  try {
    const res = await api('/status');
    paintComfy(res);
  } catch (e) {
    paintComfy({ comfy_available: false, online: false });
  }
}

async function refreshJobs() {
  try {
    const res = await api('/jobs?limit=60');
    (res.jobs || []).forEach((job) => upsertJob(job));
  } catch { /* тихо: WS всё равно пришлёт обновления */ }
}

function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let ws;
  try {
    ws = new WebSocket(proto + '//' + location.host + API + '/ws');
  } catch {
    setTimeout(connectWs, 4000);
    return;
  }
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleWs(msg);
  };
  ws.onclose = () => { setTimeout(connectWs, 4000); };
}

function handleWs(msg) {
  switch (msg.event) {
    case 'hello':
      paintComfy(msg.data || {});
      break;
    case 'job':
      upsertJob(msg.data);
      break;
    case 'comfy':
      paintComfy(msg.data || {});
      break;
    case 'comfy_event': {
      const type = (msg.data || {}).type;
      if (type === 'execution_error') {
        const data = msg.data.data || {};
        toast('ComfyUI: ' + (data.exception_message || 'ошибка выполнения'), 'error', 9000);
      }
      break;
    }
    case 'queue':
      updateQueueBadge();
      break;
    case 'log':
      break;
    default:
      break;
  }
}

async function comfyAction(action) {
  try {
    const res = await api('/comfy/' + action, { method: 'POST' });
    paintComfy(res);
    toast(res.message || 'Готово', res.started === false && action !== 'stop' ? 'warn' : 'ok');
  } catch (e) { toast(e.message, 'error', 7000); }
}

function openModal(title, html) {
  $('glModalTitle').textContent = title;
  $('glModalBody').innerHTML = html;
  $('glModal').hidden = false;
}

function closeModal() { $('glModal').hidden = true; }

async function openStatusModal() {
  let comfy = state.comfy || {};
  try { comfy = await api('/status'); paintComfy(comfy); } catch { /* показать как есть */ }
  const devices = (comfy.devices || []).map((d) => {
    const used = d.vram_total ? fmtBytes(d.vram_total - (d.vram_free || 0)) : '—';
    return '<dt>' + escapeHtml(d.name || 'GPU') + '</dt><dd>' + used
      + ' занято из ' + fmtBytes(d.vram_total) + '</dd>';
  }).join('');
  const jobs = comfy.jobs || {};
  const html = `
    <dl class="gl-kv">
      <dt>ComfyUI</dt><dd>${comfy.online ? 'запущен, версия ' + escapeHtml(comfy.version || '?') : 'не отвечает'}</dd>
      <dt>Адрес</dt><dd>${escapeHtml(comfy.base_url || '—')}</dd>
      <dt>Каталог</dt><dd>${escapeHtml(comfy.comfy_root || '—')}</dd>
      <dt>Python / Torch</dt><dd>${escapeHtml(comfy.python || '—')} / ${escapeHtml(comfy.pytorch || '—')}</dd>
      <dt>В очереди ComfyUI</dt><dd>${comfy.queue_remaining == null ? '—' : comfy.queue_remaining}</dd>
      <dt>Задания GenLab</dt><dd>${Object.entries(jobs).map(([k, v]) => k + ': ' + v).join(' · ') || 'нет'}</dd>
      <dt>WebSocket</dt><dd>${comfy.ws_available ? (comfy.ws_connected ? 'подключён' : 'не подключён') : 'библиотека websockets недоступна — опрос вместо WS'}</dd>
      ${devices}
    </dl>
    ${comfy.error ? '<div class="gl-note gl-note--warn" style="margin-top:14px">' + escapeHtml(comfy.error) + '</div>' : ''}
    <div class="gl-actions-row">
      <button class="gl-btn gl-btn-primary" data-comfy="start"><i class="fas fa-play"></i> Запустить</button>
      <button class="gl-btn gl-btn-ghost" data-comfy="stop"><i class="fas fa-stop"></i> Остановить</button>
      <button class="gl-btn gl-btn-ghost" data-comfy="restart"><i class="fas fa-rotate"></i> Перезапустить</button>
      <button class="gl-btn gl-btn-ghost" data-comfy="free"><i class="fas fa-broom"></i> Освободить VRAM</button>
    </div>`;
  openModal('Состояние ComfyUI', html);
  $('glModalBody').querySelectorAll('[data-comfy]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      await comfyAction(btn.dataset.comfy);
      setTimeout(openStatusModal, 600);
    });
  });
}

async function openLogModal() {
  openModal('Лог ComfyUI', '<pre class="gl-log" id="glLogBox">Загрузка…</pre>');
  const box = $('glLogBox');
  try {
    const res = await api('/comfy/log?tail=300');
    box.textContent = (res.lines || []).join('\n') || 'Лог пуст';
  } catch (e) {
    box.textContent = 'Не удалось прочитать лог: ' + e.message;
  }
  box.scrollTop = box.scrollHeight;
}

async function openModelsModal() {
  openModal('Модели, паки и пути', '<p class="gl-muted">Загрузка…</p>');
  let snap, paths, packInfo;
  try {
    [snap, paths, packInfo] = await Promise.all([
      api('/catalog'), api('/paths'), api('/packs')]);
  } catch (e) {
    $('glModalBody').innerHTML = '<div class="gl-note gl-note--bad">' + escapeHtml(e.message) + '</div>';
    return;
  }
  state.packs = packInfo.packs || [];

  const rows = Object.values(snap.folders || {}).map((f) => `
    <tr>
      <td>${escapeHtml(f.title)}<div class="gl-muted" style="font-size:11px">${escapeHtml(f.hint)}</div></td>
      <td class="gl-num">${f.count}</td>
      <td class="gl-num">${fmtBytes(f.size)}</td>
    </tr>`).join('');

  const ready = Object.entries(snap.readiness || {}).map(([group, info]) => `
    <li style="margin:3px 0">
      <b>${escapeHtml(groupTitle(group))}</b> —
      ${info.ready
        ? '<span style="color:var(--gl-success)">готово</span>'
        : 'не хватает: ' + info.missing.map((m) => escapeHtml(m.label)).join(', ')}
    </li>`).join('');

  const roots = (paths.models_roots || []).map((r) =>
    '<code style="font-size:11px">' + escapeHtml(r) + '</code>').join('<br>');

  $('glModalBody').innerHTML = `
    <div class="gl-note gl-note--warn">${escapeHtml(snap.note || '')}</div>

    <h4 style="margin:18px 0 6px;font-size:13px">Папки</h4>
    <div class="gl-paths">
      <label class="gl-label"><span>Корень ComfyUI</span></label>
      <div class="gl-path-row">
        <input class="gl-input" id="glPathComfy" value="${escapeHtml(paths.comfy_root)}"
               placeholder="C:\\ComfyUI_windows_portable">
        <button class="gl-btn gl-btn-ghost" id="glPathComfyAuto" type="button">Авто</button>
      </div>
      <div class="gl-hint">${paths.comfy_ok
        ? 'Найден: ' + escapeHtml(paths.comfy_main)
        : '⚠ ComfyUI здесь не найден — нужен каталог с ComfyUI\\main.py и python_embeded\\python.exe'}</div>

      <label class="gl-label" style="margin-top:12px"><span>Папка с моделями</span></label>
      <div class="gl-path-row">
        <input class="gl-input" id="glPathModels" value="${escapeHtml(paths.models_dir)}"
               placeholder="Оставьте пустым, чтобы использовать папку ComfyUI">
        <button class="gl-btn gl-btn-ghost" id="glPathModelsDefault" type="button">Как у ComfyUI</button>
      </div>
      <div class="gl-hint">
        Можно указать любую папку, в том числе на другом диске: внутри сами создадутся
        подпапки checkpoints, loras, vae, controlnet и т.д. ComfyUI подключит её через
        extra_model_paths.yaml, его установка при этом не меняется.
      </div>
      <div class="gl-hint">Поиск моделей: ${roots || '—'}</div>

      <div class="gl-actions-row">
        <button class="gl-btn gl-btn-primary" id="glPathSave" type="button">
          <i class="fas fa-check"></i> Применить пути
        </button>
        ${paths.comfy_ok ? `<button class="gl-btn gl-btn-ghost" id="glPathRestart" type="button">
          <i class="fas fa-rotate"></i> Перезапустить ComfyUI</button>` : ''}
      </div>
      <div class="gl-hint">Свободно на диске моделей: ${fmtBytes(paths.free_space)}</div>
    </div>

    <div id="glPacksBlock">${packsHtml(packInfo)}</div>

    <h4 style="margin:18px 0 6px;font-size:13px">Готовность по категориям</h4>
    <ul style="margin:0;padding-left:18px;font-size:12.5px">${ready}</ul>

    <h4 style="margin:18px 0 6px;font-size:13px">Модели (${snap.totals.files} файлов, ${fmtBytes(snap.totals.bytes)})</h4>
    <table class="gl-table">
      <thead><tr><th>Папка</th><th style="text-align:right">Файлов</th><th style="text-align:right">Размер</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  bindPathControls(paths);
  bindPackControls();
}

const PACK_STATE_LABEL = {
  active: ['работает', 'ok'],
  absent: ['не установлен', 'off'],
  disabled: ['отключён', 'off'],
  broken: ['не загрузился', 'bad'],
  needs_restart: ['нужен перезапуск', 'warn'],
  unknown: ['неизвестно', 'off'],
};

function packsHtml(packInfo) {
  const packs = packInfo.packs || [];
  const state = packInfo.state || {};
  const rows = packs.map((p) => {
    const [label, tone] = PACK_STATE_LABEL[p.state] || [p.state, 'off'];
    const actions = [];
    if (p.state === 'absent') {
      actions.push(`<button class="gl-mini" data-pack-install="${p.id}" title="Установить"><i class="fas fa-download"></i></button>`);
    } else if (p.state === 'active' || p.state === 'needs_restart') {
      actions.push(`<button class="gl-mini" data-pack-disable="${p.id}" title="Отключить"><i class="fas fa-toggle-off"></i></button>`);
    } else if (p.state === 'disabled') {
      actions.push(`<button class="gl-mini" data-pack-enable="${p.id}" title="Включить"><i class="fas fa-toggle-on"></i></button>`);
    }
    if (p.by_us) {
      actions.push(`<button class="gl-mini" data-pack-remove="${p.id}" title="Убрать (папка сохранится в _backups)"><i class="fas fa-trash"></i></button>`);
    }
    return `<tr>
      <td>${escapeHtml(p.title)}
        ${p.required ? '<span class="gl-tag gl-tag--req">нужен</span>' : ''}
        <div class="gl-muted" style="font-size:11px">${escapeHtml(p.why)}</div></td>
      <td><span class="gl-tag gl-tag--${tone}">${label}</span></td>
      <td class="gl-num">${actions.join('')}</td>
    </tr>`;
  }).join('');

  const progress = state.running
    ? `<div class="gl-note gl-note--warn" style="margin:10px 0">Установка: ${escapeHtml(state.pack || '')} — ${escapeHtml(state.step || '')}</div>`
    : (state.error ? `<div class="gl-note gl-note--bad" style="margin:10px 0">${escapeHtml(state.error)}</div>` : '');

  const needInstall = (packInfo.missing_required || []).length;
  return `
    <h4 style="margin:18px 0 6px;font-size:13px">Дополнительные наборы нод
      ${needInstall ? `<span class="gl-tag gl-tag--warn">не хватает: ${needInstall}</span>` : ''}</h4>
    <div class="gl-hint" style="margin-bottom:8px">
      Это код нод, а не модели — ничего не скачивается в папку моделей.
      Зависимости ставятся с пинами на уже установленный torch, чтобы не сломать CUDA-сборку.
      Отключение и удаление не удаляют файлы: папка переименовывается или переносится в _backups.
    </div>
    ${progress}
    <table class="gl-table">
      <thead><tr><th>Набор</th><th>Состояние</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="gl-actions-row">
      <button class="gl-btn gl-btn-primary" id="glPacksInstallAll" type="button"
              ${needInstall ? '' : 'disabled'}>
        <i class="fas fa-download"></i> Установить недостающие
      </button>
      <button class="gl-btn gl-btn-ghost" id="glPacksRefresh" type="button">
        <i class="fas fa-rotate"></i> Обновить
      </button>
    </div>`;
}

function bindPathControls() {
  $('glPathSave').addEventListener('click', async () => {
    const body = {
      comfy_root: $('glPathComfy').value.trim(),
      models_dir: $('glPathModels').value.trim(),
    };
    try {
      const res = await api('/paths', { method: 'POST', body });
      toast(res.message, 'ok', 6000);
      state.objectInfo = null;
      await refreshStatus();
      await openModelsModal();
      if (res.restart_needed) {
        toast('Перезапустите ComfyUI, чтобы он увидел новую папку моделей', 'warn', 9000);
      }
    } catch (e) {
      toast(e.message, 'error', 9000);
    }
  });
  $('glPathComfyAuto').addEventListener('click', () => { $('glPathComfy').value = ''; });
  $('glPathModelsDefault').addEventListener('click', () => { $('glPathModels').value = ''; });
  const restart = $('glPathRestart');
  if (restart) restart.addEventListener('click', async () => {
    await comfyAction('restart');
    setTimeout(openModelsModal, 1500);
  });
}

function bindPackControls() {
  const all = $('glPacksInstallAll');
  if (all) all.addEventListener('click', async () => {
    try {
      const res = await api('/packs/install', { method: 'POST', body: {} });
      toast(res.message, 'ok', 5000);
      watchPacks();
    } catch (e) { toast(e.message, 'error', 8000); }
  });
  const refresh = $('glPacksRefresh');
  if (refresh) refresh.addEventListener('click', () => openModelsModal());

  document.querySelectorAll('[data-pack-install]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        const res = await api('/packs/install',
          { method: 'POST', body: { id: btn.dataset.packInstall } });
        toast(res.message, 'ok', 4000);
        watchPacks();
      } catch (e) { toast(e.message, 'error', 8000); }
    });
  });
  const act = async (attr, path) => {
    document.querySelectorAll('[' + attr + ']').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const id = btn.getAttribute(attr);
        try {
          const res = await api('/packs/' + encodeURIComponent(id) + path,
            { method: 'POST', body: {} });
          toast(res.message, 'ok', 4000);
          await openModelsModal();
        } catch (e) { toast(e.message, 'error', 8000); }
      });
    });
  };
  act('data-pack-disable', '/disable');
  act('data-pack-enable', '/enable');
  act('data-pack-remove', '/remove');
}

let packWatchTimer = null;
function watchPacks() {
  clearInterval(packWatchTimer);
  packWatchTimer = setInterval(async () => {
    try {
      const res = await api('/packs/state');
      const block = $('glPacksBlock');
      if (block) block.innerHTML = packsHtml({ packs: res.packs, state: res.state,
                                               missing_required: null });
      bindPackControls();
      if (!res.state.running) {
        clearInterval(packWatchTimer);
        packWatchTimer = null;
        toast('Пакеты обновлены. Перезапустите ComfyUI, чтобы ноды появились.', 'ok', 8000);
        await refreshStatus();
      }
    } catch (e) {
      clearInterval(packWatchTimer);
      packWatchTimer = null;
    }
  }, 2500);
}

function groupTitle(groupId) {
  const group = state.groups.find((g) => g.id === groupId);
  return group ? group.title : groupId;
}

/* ------------------------------------------------------- 9. Запуск */

async function runGeneration() {
  if (!state.activePreset) return;
  const params = {};
  state.fields.forEach((field) => {
    if (field.type === 'hidden') return;
    const value = state.values[field.name];
    if (value === undefined || value === '' || value === null) return;
    params[field.name] = value;
  });

  const btn = $('glRunBtn');
  btn.disabled = true;
  try {
    const res = await api('/generate', {
      method: 'POST',
      body: { preset: state.activePreset.id, params, title: state.activePreset.title },
    });
    upsertJob(res.job);
    toast('Задание поставлено в очередь', 'ok', 2600);
  } catch (e) {
    toast('Не удалось запустить: ' + e.message, 'error', 9000);
  } finally {
    setTimeout(() => { btn.disabled = !(state.comfy && state.comfy.online); }, 400);
  }
}

async function validatePreset() {
  if (!state.activePreset) return;
  const params = {};
  state.fields.forEach((field) => {
    if (field.type === 'hidden') return;
    const value = state.values[field.name];
    if (value === undefined || value === '' || value === null) return;
    params[field.name] = value;
  });
  try {
    const res = await api('/validate', { method: 'POST', body: { preset: state.activePreset.id, params } });
    state.problems = res.problems || [];
    renderQueueBar();
    const errors = state.problems.filter((p) => p.severity === 'error');
    if (!errors.length) {
      toast('Граф корректен: ' + (res.nodes || []).length + ' нод'
        + (state.problems.length ? ', предупреждений: ' + state.problems.length : ''), 'ok', 5000);
    } else {
      toast('Ошибок в графе: ' + errors.length, 'error', 7000);
    }
  } catch (e) {
    // Сеть или сервер упали — в отличие от «не хватает файла» это уже сбой.
    const message = String(e.message || '');
    state.problems = [{
      severity: 'error', code: 'request',
      message: /Пресет не собрал граф/.test(message)
        ? message.replace(/^Пресет не собрал граф:\s*/, '')
        : 'Проверка не удалась: ' + message,
      hint: /Загрузите|Нужна|Укажите/.test(message)
        ? 'Заполните обязательные поля пресета' : null,
    }];
    renderQueueBar();
  }
}

/* ------------------------------------------------------- Инициализация */

/* Тема — общая для всего приложения (static/js/theme.js, ключ infinity_theme,
   9 вариантов). Кнопка не «включает тёмную», а листает тот же список, что и
   остальные страницы, поэтому выбор в GenLab совпадает с главной. */
const THEME_LABELS = {
  light: 'Светлая', dark: 'Тёмная', sepia: 'Сепия', warm: 'Тёплая',
  paper: 'Бумага', ocean: 'Океан', forest: 'Лес', midnight: 'Полночь',
  rose: 'Роза',
};
const THEME_ICONS = {
  light: 'fa-sun', dark: 'fa-moon', sepia: 'fa-scroll', warm: 'fa-fire',
  paper: 'fa-file', ocean: 'fa-water', forest: 'fa-tree', midnight: 'fa-star',
  rose: 'fa-rose',
};

function themeApi() { return window.InfinityTheme || null; }

function currentTheme() {
  const api = themeApi();
  if (api && typeof api.get === 'function') {
    try { return api.get(); } catch { /* ниже — запасной путь */ }
  }
  const match = (document.body.className || '').match(
    /\b(light|dark|sepia|warm|paper|ocean|forest|midnight|rose)\b/);
  return match ? match[1] : 'light';
}

function paintThemeBtn() {
  const btn = $('glThemeBtn');
  if (!btn) return;
  const t = currentTheme();
  btn.innerHTML = '<i class="fas ' + (THEME_ICONS[t] || 'fa-sun') + '"></i>';
  btn.title = 'Тема: ' + (THEME_LABELS[t] || t) + ' — нажмите, чтобы сменить';
}

function cycleTheme() {
  const api = themeApi();
  const all = (api && api.themes) || Object.keys(THEME_LABELS);
  if (api && typeof api.set === 'function') {
    const i = all.indexOf(currentTheme());
    api.set(all[(i + 1) % all.length]);
  } else {
    // theme.js не загрузился — не оставляем кнопку мёртвой
    const t = currentTheme() === 'light' ? 'dark' : 'light';
    document.body.classList.remove('light', 'dark');
    document.body.classList.add(t);
  }
  paintThemeBtn();
}

const EMPTY_HINTS = {
  base: 'Здесь появится результат. Параметры заданы справа — нажмите «Создать».',
  offline: 'ComfyUI не запущен. Откройте статус в левом нижнем углу и запустите его.',
  noModels: 'Пока нет результатов. Сначала установите модели в ComfyUI — '
    + 'список папок и недостающих весов показывает раздел «Модели».',
};

function paintEmptyState() {
  const title = $('glEmpty').querySelector('h2');
  const text = $('glEmpty').querySelector('p');
  const offline = state.comfy && !state.comfy.online;
  const hasModels = !state.comfy || state.comfy.online;
  title.textContent = offline ? 'ComfyUI не запущен' : 'Пока пусто';
  if (offline) text.textContent = EMPTY_HINTS.offline;
  else if (!hasModels) text.textContent = EMPTY_HINTS.noModels;
  else text.textContent = state.activePreset ? EMPTY_HINTS.base
    : 'Выберите пресет слева — параметры появятся справа.';
}

function renderGalleryView() {
  $('glViewTitle').textContent = 'Галерея';
  $('glViewDesc').textContent = 'Все результаты: изображения, видео, звук и 3D';
  $('glPresetsTitle').textContent = 'Пресеты';
  markNav();
  loadGallery();
}

function bind() {
  $('glRunBtn').addEventListener('click', runGeneration);
  $('glResetBtn').addEventListener('click', resetForm);
  $('glValidateBtn').addEventListener('click', validatePreset);
  $('glRefreshBtn').addEventListener('click', async () => {
    await Promise.all([refreshStatus(), refreshJobs(), loadGallery(false)]);
    toast('Обновлено', 'ok', 1800);
  });
  $('glComfyBtn').addEventListener('click', openStatusModal);
  $('glLogsBtn').addEventListener('click', openLogModal);
  $('glThemeBtn').addEventListener('click', cycleTheme);
  $('glSearch').addEventListener('input', debounce((ev) => {
    state.galleryFilter = ev.target.value.trim();
    loadGallery();
  }, 320));
  $('glClearJobsBtn').addEventListener('click', () => {
    Array.from(state.jobs.entries()).forEach(([id, job]) => {
      if (job.status === 'done' || job.status === 'error' || job.status === 'cancelled') {
        state.jobs.delete(id);
      }
    });
    renderJobs();
    updateQueueBadge();
  });

  $('glEditBtn').addEventListener('click', () => {
    if (!state.activeAsset) return;
    const editor = window.GenLabEditor;
    if (!editor || !editor.ready) {
      toast('Редактор области ещё загружается, попробуйте через мгновение', 'warn');
      return;
    }
    editor.open(state.activeAsset.id);
  });

  $('glAudioEditBtn').addEventListener('click', () => {
    if (!state.activeAsset) return;
    const editor = window.GenLabAudio;
    if (!editor || !editor.ready) {
      toast('Звуковой редактор ещё загружается, попробуйте через мгновение', 'warn');
      return;
    }
    editor.open(state.activeAsset.id);
  });

  $('glKeysBtn').addEventListener('click', () => {
    const keys = window.GenLabMidi;
    if (!keys || !keys.ready) { toast('Клавиатура ещё загружается', 'warn'); return; }
    keys.open();
  });

  document.querySelectorAll('[data-close]').forEach((node) => {
    node.addEventListener('click', closeModal);
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') closeModal();
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) runGeneration();
  });
}

async function boot() {
  bind();
  connectWs();

  try {
    const res = await api('/presets');
    state.groups = res.groups || [];
    state.presets = res.presets || [];
  } catch (e) {
    toast('Не удалось загрузить пресеты: ' + e.message, 'error', 9000);
  }
  renderNav();
  if (state.groups.length) selectGroup(state.groups[0].id);

  await Promise.all([refreshStatus(), refreshJobs(), loadGallery()]);
  setInterval(refreshStatus, 20000);
  setInterval(refreshJobs, 15000);

  paintThemeBtn();
  const themes = themeApi();
  if (themes && typeof themes.onChange === 'function') themes.onChange(paintThemeBtn);
}

document.addEventListener('DOMContentLoaded', boot);

/* Мост для редактора области (editor.js): он живёт отдельным модулем, чтобы
   не тянуть холст в основной бандл, и берёт отсюда только то, что ему нужно —
   уведомления, галерею, очередь и текущее состояние. */
window.GenLab = {
  api, toast, state, $, el, escapeHtml,
  openAsset, loadGallery, refreshJobs, refreshStatus, upsertJob,
};