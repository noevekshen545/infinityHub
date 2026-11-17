/* ==========================================================================
   GenLab — редактор области изображения («выдели и скажи, что сделать»).

   Задача: выделить прямоугольником, лассо или кистью участок картинки,
   написать для него промпт и перерисовать только этот участок (инпейнт).
   Плюс простые операции без ИИ: яркость/контраст и обрезка по выделению.

   Как это работает без отдельного бэкенда:
     1. исходник кладём во вход ComfyUI   (POST /assets/{id}/stage)
     2. маску рисуем на канве и отдаём   (POST /upload, kind=mask)
     3. запускаем готовый пресет edit.inpaint (POST /generate)

   Маска строится ровно в пикселях исходника, поэтому движок ничего не
   масштабирует и не «съезжает» по краям выделения.

   Модуль самодостаточен: с основной страницей общается через window.GenLab
   (уведомления, галерея, очередь), а если его нет — просто логирует.
   ========================================================================== */

const API = '/api/genlab';
const INPAINT_PRESET = 'edit.inpaint';
const RED = 'rgba(244,63,94,0.45)';

/* --------------------------------------------------------------- мост */

function bridge() { return window.GenLab || {}; }

function toast(message, kind = 'info', ttl = 4200) {
  const b = bridge();
  if (typeof b.toast === 'function') return b.toast(message, kind, ttl);
  if (kind === 'error') console.error('[genlab/editor]', message);
  else console.log('[genlab/editor]', message);
}

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
    const e = new Error((data && data.error) || ('HTTP ' + res.status));
    e.status = res.status;
    e.data = data;
    throw e;
  }
  return data || {};
}

/* ---------------------------------------------------------------- DOM */

const TEMPLATE = `
  <div class="gl-editor-top">
    <span class="gl-editor-title"><i class="fas fa-wand-magic-sparkles"></i> Редактор области</span>
    <div class="gl-tools" id="edTools">
      <button class="gl-tool" type="button" data-tool="select"><i class="fas fa-vector-square"></i> Выделение</button>
      <button class="gl-tool" type="button" data-tool="lasso"><i class="fas fa-draw-polygon"></i> Лассо</button>
      <button class="gl-tool" type="button" data-tool="brush"><i class="fas fa-paintbrush"></i> Кисть</button>
      <button class="gl-tool" type="button" data-tool="erase"><i class="fas fa-eraser"></i> Ластик</button>
      <button class="gl-tool" type="button" data-tool="pan"><i class="fas fa-hand"></i> Рука</button>
    </div>
    <div class="gl-zoom">
      <button class="gl-btn gl-btn-ghost" type="button" data-zoom="out" title="Уменьшить"><i class="fas fa-minus"></i></button>
      <span id="edZoom">100%</span>
      <button class="gl-btn gl-btn-ghost" type="button" data-zoom="in" title="Увеличить"><i class="fas fa-plus"></i></button>
      <button class="gl-btn gl-btn-ghost" type="button" data-zoom="fit" title="Вписать в окно"><i class="fas fa-expand"></i></button>
    </div>
    <span class="gl-spacer"></span>
    <button class="gl-btn gl-btn-ghost" type="button" id="edUndo" title="Отменить (Ctrl+Z)" disabled><i class="fas fa-rotate-left"></i></button>
    <button class="gl-btn gl-btn-ghost" type="button" id="edRedo" title="Повторить (Ctrl+Y)" disabled><i class="fas fa-rotate-right"></i></button>
    <span class="gl-edit-status" id="edStatus"></span>
    <button class="gl-icon-btn" type="button" id="edClose" title="Закрыть (Esc)"><i class="fas fa-xmark"></i></button>
  </div>

  <div class="gl-editor-main">
    <div class="gl-editor-stage" id="edStage">
      <div id="edFit" style="position:absolute;top:0;left:0;transform-origin:0 0">
        <canvas id="edImage" class="gl-edit-image"></canvas>
        <canvas id="edMask" class="gl-edit-mask"></canvas>
        <canvas id="edUi" class="gl-edit-mask"></canvas>
      </div>
      <div class="gl-edit-cursor" id="edCursor" hidden></div>
    </div>

    <aside class="gl-editor-side">
      <div class="gl-scroll">
        <section>
          <h4>Что делаем</h4>
          <select id="edMode" class="gl-editor-select">
            <option value="region">Изменить область (инпейнт)</option>
            <option value="expand">Расширить кадр (outpaint)</option>
            <option value="upscale">Увеличить / детализировать</option>
            <option value="controlnet">По структуре (ControlNet)</option>
            <option value="instruct">Правка командой</option>
            <option value="nobg">Убрать фон</option>
          </select>
          <p class="gl-hint" id="edModeHint"></p>
        </section>

        <section data-mode="region">
          <h4>Инструмент</h4>
          <label class="gl-field-label">Толщина кисти
            <span class="gl-field-num" id="edBrushVal">48</span>
          </label>
          <input type="range" id="edBrush" min="4" max="300" value="48" style="width:100%">
          <div class="gl-editor-actions">
            <button class="gl-btn gl-btn-ghost" type="button" id="edClear">Очистить маску</button>
            <button class="gl-btn gl-btn-ghost" type="button" id="edInvert">Инвертировать</button>
          </div>
          <p class="gl-hint">Кисть: <b>B</b>, ластик: <b>E</b>, выделение: <b>1</b>, лассо: <b>2</b>.
            Отмена — <b>Ctrl+Z</b>.</p>
        </section>

        <section data-mode="region">
          <h4>Что сделать в выделенной области</h4>
          <label class="gl-field-label">Промпт</label>
          <textarea id="edPrompt" rows="4" placeholder="например: убрать человека, дорисовать окно, сменить фон на закат…"></textarea>
          <label class="gl-field-label">Чего не должно быть</label>
          <input type="text" id="edNegative" placeholder="размытость, артефакты, швы">
          <label class="gl-field-label">Сила изменения <span class="gl-field-num" id="edDenoiseVal">0.75</span></label>
          <input type="range" id="edDenoise" min="0.05" max="1" step="0.05" value="0.75" style="width:100%">
          <label class="gl-field-label">Расширить маску, px</label>
          <input type="number" id="edGrow" min="0" max="256" value="8">
          <label class="gl-field-label">Мягкость края, px</label>
          <input type="number" id="edFeather" min="0" max="256" value="16">
          <label class="gl-field-label">Шагов</label>
          <input type="number" id="edSteps" min="1" max="100" value="20">
        </section>

        <section data-mode="expand">
          <h4>Расширение холста</h4>
          <label class="gl-field-label">Промпт</label>
          <textarea id="expPrompt" rows="3" placeholder="что продолжить за краями кадра"></textarea>
          <label class="gl-field-label">Чего не должно быть</label>
          <input type="text" id="expNegative" placeholder="швы, размытость">
          <div class="gl-grid2">
            <label class="gl-field-label">Слева, px<input type="number" id="expLeft" min="0" max="2048" value="0"></label>
            <label class="gl-field-label">Справа, px<input type="number" id="expRight" min="0" max="2048" value="512"></label>
            <label class="gl-field-label">Сверху, px<input type="number" id="expTop" min="0" max="2048" value="0"></label>
            <label class="gl-field-label">Снизу, px<input type="number" id="expBottom" min="0" max="2048" value="0"></label>
          </div>
          <label class="gl-field-label">Растушёвка, px</label>
          <input type="number" id="expFeather" min="0" max="512" value="64">
          <label class="gl-field-label">Сила изменения <span class="gl-field-num" id="expDenoiseVal">0.85</span></label>
          <input type="range" id="expDenoise" min="0.1" max="1" step="0.05" value="0.85" style="width:100%">
        </section>

        <section data-mode="upscale">
          <h4>Увеличение</h4>
          <label class="gl-field-label">Модель апскейла (models/upscale_models)</label>
          <input type="text" id="upModel" placeholder="например 4x-UltraSharp.pth — пусто = обычный ресайз">
          <label class="gl-field-label">Метод ресайза</label>
          <select id="upMethod" class="gl-editor-select">
            <option value="lanczos">lanczos</option>
            <option value="bicubic">bicubic</option>
            <option value="bilinear">bilinear</option>
            <option value="area">area</option>
            <option value="nearest-exact">nearest-exact</option>
          </select>
          <div class="gl-grid2">
            <label class="gl-field-label">Ширина<input type="number" id="upWidth" min="0" max="8192" step="8" value="0"></label>
            <label class="gl-field-label">Высота<input type="number" id="upHeight" min="0" max="8192" step="8" value="0"></label>
          </div>
          <label class="gl-field-label"><input type="checkbox" id="upUseModel" checked> Использовать модель апскейла</label>
          <p class="gl-hint">0×0 — пропорционально модели; иначе результат масштабируется до указанного размера.</p>
        </section>

        <section data-mode="controlnet">
          <h4>Управление структурой</h4>
          <label class="gl-field-label">Промпт</label>
          <textarea id="cnPrompt" rows="3" placeholder="что нарисовать по этой структуре"></textarea>
          <label class="gl-field-label">Чего не должно быть</label>
          <input type="text" id="cnNegative" placeholder="швы, артефакты">
          <label class="gl-field-label">Модель ControlNet (models/controlnet)</label>
          <input type="text" id="cnModel" placeholder="например control_v11p_sd15_canny.pth">
          <label class="gl-field-label">Препроцессор</label>
          <select id="cnPrep" class="gl-editor-select">
            <option value="Без препроцессора (подать фото как есть)">Без препроцессора</option>
            <option value="Canny — контуры">Canny — контуры</option>
            <option value="Depth — карта глубины">Depth — глубина</option>
            <option value="Normal — карта нормалей">Normal — нормали</option>
            <option value="Pose — поза (OpenPose/DWPose)">Pose — поза</option>
            <option value="Lineart — контурный рисунок">Lineart — линии</option>
            <option value="Segmentation — сегментация">Segmentation</option>
            <option value="Blur — размытие (для переноса стиля)">Blur — размытие</option>
          </select>
          <label class="gl-field-label">Сила <span class="gl-field-num" id="cnStrengthVal">0.80</span></label>
          <input type="range" id="cnStrength" min="0" max="2" step="0.05" value="0.8" style="width:100%">
          <div class="gl-grid2">
            <label class="gl-field-label">Ширина<input type="number" id="cnWidth" min="64" max="4096" step="8" value="1024"></label>
            <label class="gl-field-label">Высота<input type="number" id="cnHeight" min="64" max="4096" step="8" value="1024"></label>
          </div>
          <label class="gl-field-label">Denoise <span class="gl-field-num" id="cnDenoiseVal">1.00</span></label>
          <input type="range" id="cnDenoise" min="0.1" max="1" step="0.05" value="1" style="width:100%">
        </section>

        <section data-mode="instruct">
          <h4>Правка словами</h4>
          <label class="gl-field-label">Команда</label>
          <textarea id="inPrompt" rows="3" placeholder="turn it into a watercolor painting"></textarea>
          <label class="gl-field-label">Сила правки <span class="gl-field-num" id="inDenoiseVal">0.75</span></label>
          <input type="range" id="inDenoise" min="0.05" max="1" step="0.05" value="0.75" style="width:100%">
          <label class="gl-field-label">Похожесть на исходник <span class="gl-field-num" id="inCfgVal">1.5</span></label>
          <input type="range" id="inCfg" min="0.5" max="10" step="0.1" value="1.5" style="width:100%">
          <p class="gl-hint">Нужен чекпойнт Instruct-Pix2Pix.</p>
        </section>

        <section data-mode="nobg">
          <h4>Убрать фон</h4>
          <label class="gl-field-label">Модель (models/background_removal)</label>
          <input type="text" id="nbModel" placeholder="например u2net.pth">
          <p class="gl-hint">Результат — PNG с прозрачным фоном.</p>
        </section>

        <section data-mode="region">
          <h4>Без ИИ</h4>
          <label class="gl-field-label">Яркость <span class="gl-field-num" id="edBrightVal">0</span></label>
          <input type="range" id="edBright" min="-100" max="100" value="0" style="width:100%">
          <label class="gl-field-label">Контраст <span class="gl-field-num" id="edContrastVal">0</span></label>
          <input type="range" id="edContrast" min="-100" max="100" value="0" style="width:100%">
          <div class="gl-editor-actions">
            <button class="gl-btn gl-btn-ghost" type="button" id="edApply">Применить и сохранить</button>
            <button class="gl-btn gl-btn-ghost" type="button" id="edCrop" disabled>Обрезать по выделению</button>
          </div>
        </section>
      </div>

      <div class="gl-editor-foot">
        <button class="gl-btn gl-btn-primary" type="button" id="edRun">
          <i class="fas fa-wand-magic-sparkles"></i> Перерисовать область
        </button>
      </div>
    </aside>
  </div>`;

let dom = null;

function build() {
  if (dom) return dom;
  const root = document.createElement('div');
  root.id = 'glEdit';
  root.className = 'gl-editor';
  root.hidden = true;
  root.innerHTML = TEMPLATE;
  document.body.appendChild(root);

  const q = (id) => root.querySelector('#' + id);
  const image = q('edImage');
  const mask = q('edMask');
  const ui = q('edUi');
  dom = {
    root,
    stage: q('edStage'),
    fit: q('edFit'),
    zoom: q('edZoom'),
    status: q('edStatus'),
    image, mask, ui,
    imageCtx: image.getContext('2d'),
    maskCtx: mask.getContext('2d', { willReadFrequently: true }),
    uiCtx: ui.getContext('2d'),
    tools: q('edTools'),
    prompt: q('edPrompt'),
    negative: q('edNegative'),
    brush: q('edBrush'),
    brushVal: q('edBrushVal'),
    denoise: q('edDenoise'),
    denoiseVal: q('edDenoiseVal'),
    grow: q('edGrow'),
    feather: q('edFeather'),
    steps: q('edSteps'),
    bright: q('edBright'),
    brightVal: q('edBrightVal'),
    contrast: q('edContrast'),
    contrastVal: q('edContrastVal'),
    run: q('edRun'),
    crop: q('edCrop'),
    undo: q('edUndo'),
    redo: q('edRedo'),
    cursor: q('edCursor'),
    mode: q('edMode'),
    modeHint: q('edModeHint'),
    // Расширение кадра
    expPrompt: q('expPrompt'), expNegative: q('expNegative'),
    expLeft: q('expLeft'), expRight: q('expRight'),
    expTop: q('expTop'), expBottom: q('expBottom'),
    expFeather: q('expFeather'), expDenoise: q('expDenoise'), expDenoiseVal: q('expDenoiseVal'),
    // Апскейл
    upModel: q('upModel'), upMethod: q('upMethod'), upWidth: q('upWidth'),
    upHeight: q('upHeight'), upUseModel: q('upUseModel'),
    // ControlNet
    cnPrompt: q('cnPrompt'), cnNegative: q('cnNegative'), cnModel: q('cnModel'),
    cnPrep: q('cnPrep'), cnStrength: q('cnStrength'), cnStrengthVal: q('cnStrengthVal'),
    cnWidth: q('cnWidth'), cnHeight: q('cnHeight'),
    cnDenoise: q('cnDenoise'), cnDenoiseVal: q('cnDenoiseVal'),
    // Instruct Pix2Pix
    inPrompt: q('inPrompt'), inDenoise: q('inDenoise'), inDenoiseVal: q('inDenoiseVal'),
    inCfg: q('inCfg'), inCfgVal: q('inCfgVal'),
    // Убрать фон
    nbModel: q('nbModel'),
  };
  return dom;
}

/* ------------------------------------------------------------- сессия */

let session = null;

function status(text, kind = '') {
  if (!dom) return;
  dom.status.textContent = text || '';
  dom.status.className = 'gl-edit-status' + (kind ? ' is-' + kind : '');
}

/* -------------------------------------------------- геометрия и рисовка */

function applyTransform() {
  const s = session;
  dom.fit.style.transform = `translate(${s.tx}px, ${s.ty}px) scale(${s.scale})`;
  dom.zoom.textContent = Math.round(s.scale * 100) + '%';
}

function fitToWindow() {
  const r = dom.stage.getBoundingClientRect();
  const s = session;
  const pad = 0.94;
  s.scale = Math.min((r.width * pad) / s.W, (r.height * pad) / s.H) || 1;
  s.tx = (r.width - s.W * s.scale) / 2;
  s.ty = (r.height - s.H * s.scale) / 2;
  applyTransform();
}

function zoomAt(factor, cx, cy) {
  const s = session;
  const next = Math.min(16, Math.max(0.05, s.scale * factor));
  const k = next / s.scale;
  s.tx = cx - (cx - s.tx) * k;
  s.ty = cy - (cy - s.ty) * k;
  s.scale = next;
  applyTransform();
}

function toImage(ev) {
  const r = dom.stage.getBoundingClientRect();
  const s = session;
  return {
    x: (ev.clientX - r.left - s.tx) / s.scale,
    y: (ev.clientY - r.top - s.ty) / s.scale,
  };
}

function refreshOverlay() {
  // Слой «ui» показывает маску полупрозрачным красным: сама маска лежит в
  // отдельной канве, а тон накладывается через source-in.
  const s = session;
  const ctx = dom.uiCtx;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, s.W, s.H);
  ctx.globalCompositeOperation = 'source-over';
  ctx.drawImage(dom.mask, 0, 0);
  ctx.globalCompositeOperation = 'source-in';
  ctx.fillStyle = RED;
  ctx.fillRect(0, 0, s.W, s.H);
  ctx.globalCompositeOperation = 'source-over';
}

function clearUi() {
  dom.uiCtx.setTransform(1, 0, 0, 1, 0, 0);
  dom.uiCtx.clearRect(0, 0, session.W, session.H);
}

function brushRadius() {
  const s = session;
  return Math.max(1, Number(dom.brush.value) / 2 / s.scale);
}

function paintDot(x, y, erase) {
  const ctx = dom.maskCtx;
  const r = brushRadius();
  ctx.globalCompositeOperation = erase ? 'destination-out' : 'source-over';
  ctx.fillStyle = '#fff';
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fill();
  ctx.globalCompositeOperation = 'source-over';
}

function paintLine(a, b, erase) {
  const ctx = dom.maskCtx;
  const r = brushRadius();
  ctx.globalCompositeOperation = erase ? 'destination-out' : 'source-over';
  ctx.strokeStyle = '#fff';
  ctx.lineWidth = r * 2;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.lineTo(b.x, b.y);
  ctx.stroke();
  ctx.globalCompositeOperation = 'source-over';
}

function fillRectMask(x0, y0, x1, y1, erase) {
  const ctx = dom.maskCtx;
  ctx.globalCompositeOperation = erase ? 'destination-out' : 'source-over';
  ctx.fillStyle = '#fff';
  ctx.fillRect(Math.min(x0, x1), Math.min(y0, y1),
               Math.abs(x1 - x0), Math.abs(y1 - y0));
  ctx.globalCompositeOperation = 'source-over';
}

function maskHasContent() {
  const s = session;
  const data = dom.maskCtx.getImageData(0, 0, s.W, s.H).data;
  for (let i = 3; i < data.length; i += 4) {
    if (data[i] > 8) return true;
  }
  return false;
}

/* ---------------------------------------------------------- история */

const HISTORY_LIMIT = 30;

function snapshotMask() {
  const c = document.createElement('canvas');
  c.width = session.W;
  c.height = session.H;
  c.getContext('2d').drawImage(dom.mask, 0, 0);
  return c;
}

function restoreMask(snap) {
  dom.maskCtx.setTransform(1, 0, 0, 1, 0, 0);
  dom.maskCtx.clearRect(0, 0, session.W, session.H);
  dom.maskCtx.drawImage(snap, 0, 0);
  refreshOverlay();
  paintHistoryButtons();
}

/* Запомнить состояние маски до изменения. Вызывается в начале каждой правки,
   поэтому «отменить» возвращает ровно предыдущий шаг. */
function pushUndo() {
  const s = session;
  s.redoStack.length = 0;
  s.undoStack.push(snapshotMask());
  while (s.undoStack.length > HISTORY_LIMIT) s.undoStack.shift();
  paintHistoryButtons();
}

function undo() {
  const s = session;
  if (!s || !s.undoStack.length) return;
  s.redoStack.push(snapshotMask());
  restoreMask(s.undoStack.pop());
}

function redo() {
  const s = session;
  if (!s || !s.redoStack.length) return;
  s.undoStack.push(snapshotMask());
  restoreMask(s.redoStack.pop());
}

function paintHistoryButtons() {
  if (!dom || !session) return;
  dom.undo.disabled = !session.undoStack.length;
  dom.redo.disabled = !session.redoStack.length;
}

/* Кружок под курсором показывает фактическую кисть (учитывает зум), поэтому
   не приходится целиться наугад. Рисуем его отдельным слоем в координатах
   сцены — канва разметки занята тонировкой маски и превью выделения. */
function updateCursor(ev) {
  if (!dom || !session) return;
  const show = (session.tool === 'brush' || session.tool === 'erase') && !session.panFrom;
  if (!show) { dom.cursor.hidden = true; return; }
  const r = dom.stage.getBoundingClientRect();
  const d = Math.max(4, Number(dom.brush.value));
  dom.cursor.hidden = false;
  dom.cursor.style.width = d + 'px';
  dom.cursor.style.height = d + 'px';
  dom.cursor.style.transform = 'translate(' + (ev.clientX - r.left - d / 2) + 'px,' +
                               (ev.clientY - r.top - d / 2) + 'px)';
}

function hideCursor() {
  if (dom) dom.cursor.hidden = true;
}

/* ------------------------------------------------ указатель и жесты */

function onPointerDown(ev) {
  if (!session) return;
  dom.stage.setPointerCapture(ev.pointerId);
  const s = session;
  const pt = toImage(ev);

  if (s.tool === 'pan' || ev.button === 1) {
    s.panFrom = { x: ev.clientX, y: ev.clientY, tx: s.tx, ty: s.ty };
    dom.stage.classList.add('is-panning');
    return;
  }
  if (pt.x < -2 || pt.y < -2 || pt.x > s.W + 2 || pt.y > s.H + 2) return;

  s.drawing = true;
  pushUndo();
  const erase = s.tool === 'erase';
  if (s.tool === 'select') {
    s.start = pt;
    s.last = pt;
  } else if (s.tool === 'lasso') {
    s.points = [pt];
  } else if (s.tool === 'brush' || s.tool === 'erase') {
    s.last = pt;
    paintDot(pt.x, pt.y, erase);
    refreshOverlay();
  }
  ev.preventDefault();
}

function onPointerMove(ev) {
  if (!session) return;
  const s = session;
  updateCursor(ev);
  if (s.panFrom) {
    s.tx = s.panFrom.tx + (ev.clientX - s.panFrom.x);
    s.ty = s.panFrom.ty + (ev.clientY - s.panFrom.y);
    applyTransform();
    return;
  }
  if (!s.drawing) return;
  const pt = toImage(ev);

  if (s.tool === 'select') {
    clearUi();
    const ctx = dom.uiCtx;
    ctx.strokeStyle = 'rgba(59,110,245,0.95)';
    ctx.lineWidth = 1.5 / s.scale;
    ctx.setLineDash([6 / s.scale, 4 / s.scale]);
    ctx.strokeRect(s.start.x, s.start.y, pt.x - s.start.x, pt.y - s.start.y);
    s.last = pt;
  } else if (s.tool === 'lasso') {
    s.points.push(pt);
    clearUi();
    const ctx = dom.uiCtx;
    ctx.strokeStyle = 'rgba(59,110,245,0.95)';
    ctx.lineWidth = 1.5 / s.scale;
    ctx.beginPath();
    ctx.moveTo(s.points[0].x, s.points[0].y);
    for (const p of s.points) ctx.lineTo(p.x, p.y);
    ctx.stroke();
  } else if (s.tool === 'brush' || s.tool === 'erase') {
    paintLine(s.last, pt, s.tool === 'erase');
    s.last = pt;
    refreshOverlay();
  }
  ev.preventDefault();
}

function onPointerUp(ev) {
  if (!session) return;
  const s = session;
  if (s.panFrom) {
    s.panFrom = null;
    dom.stage.classList.remove('is-panning');
    return;
  }
  if (!s.drawing) return;
  s.drawing = false;
  clearUi();

  if (s.tool === 'select' && s.start && s.last) {
    const x0 = s.start.x, y0 = s.start.y, x1 = s.last.x, y1 = s.last.y;
    if (Math.abs(x1 - x0) > 2 && Math.abs(y1 - y0) > 2) {
      fillRectMask(x0, y0, x1, y1, false);
      s.cropRect = { x: x0, y: y0, w: Math.abs(x1 - x0), h: Math.abs(y1 - y0) };
      dom.crop.disabled = false;
    }
  } else if (s.tool === 'lasso' && s.points.length >= 3) {
    const ctx = dom.maskCtx;
    ctx.fillStyle = '#fff';
    ctx.beginPath();
    ctx.moveTo(s.points[0].x, s.points[0].y);
    for (const p of s.points) ctx.lineTo(p.x, p.y);
    ctx.closePath();
    ctx.fill();
    s.points = [];
  }
  refreshOverlay();
  ev.preventDefault();
}

function onWheel(ev) {
  if (!session) return;
  const r = dom.stage.getBoundingClientRect();
  zoomAt(ev.deltaY < 0 ? 1.12 : 1 / 1.12, ev.clientX - r.left, ev.clientY - r.top);
  ev.preventDefault();
}

/* ------------------------------------------------------ открытие/закрытие */

async function openEditor(assetId) {
  const assets = (bridge().state && bridge().state.assets) || [];
  const asset = assets.find((a) => a.id === assetId);
  if (!asset) { toast('Файл не найден в галерее', 'warn'); return; }
  if (asset.media_type && asset.media_type !== 'image') {
    toast('Редактировать область можно только у изображений', 'warn');
    return;
  }

  build();
  dom.root.hidden = false;
  status('Загрузка изображения…');

  let img;
  try {
    img = await loadImage(asset.url);
  } catch (e) {
    status('Не удалось открыть изображение: ' + e.message, 'bad');
    return;
  }

  const W = img.naturalWidth || img.width;
  const H = img.naturalHeight || img.height;

  session = {
    asset, W, H, scale: 1, tx: 0, ty: 0,
    tool: 'select', drawing: false, points: [], start: null, last: null,
    panFrom: null, cropRect: null, undoStack: [], redoStack: [],
    mode: dom.mode.value,
  };

  for (const c of [dom.image, dom.mask, dom.ui]) {
    c.width = W;
    c.height = H;
  }
  dom.imageCtx.drawImage(img, 0, 0, W, H);
  dom.maskCtx.setTransform(1, 0, 0, 1, 0, 0);
  dom.maskCtx.clearRect(0, 0, W, H);
  clearUi();
  hideCursor();
  paintHistoryButtons();

  dom.crop.disabled = true;
  dom.tools.querySelectorAll('.gl-tool').forEach((b) => {
    b.classList.toggle('is-active', b.dataset.tool === 'select');
  });
  dom.stage.classList.remove('is-pan', 'is-panning');
  status('');

  // Размеры по умолчанию под текущую картинку для апскейла и ControlNet.
  if (!dom.upWidth.value || Number(dom.upWidth.value) === 0) dom.upWidth.value = W * 2;
  if (!dom.upHeight.value || Number(dom.upHeight.value) === 0) dom.upHeight.value = H * 2;
  dom.cnWidth.value = W;
  dom.cnHeight.value = H;
  applyMode();
  fitToWindow();
  refreshOverlay();
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('файл недоступен'));
    img.src = url + (url.includes('?') ? '&' : '?') + 't=' + Date.now();
  });
}

function closeEditor() {
  if (!dom) return;
  dom.root.hidden = true;
  session = null;
}

/* --------------------------------------------------------- действия */

function clearMask() {
  pushUndo();
  dom.maskCtx.setTransform(1, 0, 0, 1, 0, 0);
  dom.maskCtx.clearRect(0, 0, session.W, session.H);
  session.cropRect = null;
  dom.crop.disabled = true;
  clearUi();
  refreshOverlay();
}

function invertMask() {
  // Инвертируем альфу напрямую: так маска остаётся чистой (белое по
  // прозрачному), без чёрного фона, который дал бы обычный xor по канве.
  const s = session;
  pushUndo();
  const ctx = dom.maskCtx;
  const d = ctx.getImageData(0, 0, s.W, s.H);
  const px = d.data;
  for (let i = 0; i < px.length; i += 4) {
    const on = px[i + 3] > 128 ? 0 : 255;
    px[i] = 255; px[i + 1] = 255; px[i + 2] = 255; px[i + 3] = on;
  }
  ctx.putImageData(d, 0, 0);
  refreshOverlay();
}

function maskBlob() {
  // Канва маски держится прозрачной (белое по прозрачному) — так удобно
  // тонировать её на экране. Движку же нужен обычный чёрно-белый PNG, где
  // белое = «перерисовать», поэтому на выгрузке кладём маску на чёрный фон.
  const s = session;
  const out = document.createElement('canvas');
  out.width = s.W;
  out.height = s.H;
  const ctx = out.getContext('2d');
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, s.W, s.H);
  ctx.drawImage(dom.mask, 0, 0);
  return new Promise((resolve, reject) => {
    out.toBlob((b) => (b ? resolve(b) : reject(new Error('маска не собралась'))), 'image/png');
  });
}

async function stageSource() {
  const res = await api('/assets/' + encodeURIComponent(session.asset.id) + '/stage',
                        { method: 'POST', body: {} });
  return res.comfy_name;
}

async function uploadMask() {
  const blob = await maskBlob();
  const form = new FormData();
  form.append('file', blob, 'region-mask.png');
  form.append('kind', 'mask');
  const res = await api('/upload', { method: 'POST', body: form });
  return res.comfy_name;
}

async function runInpaint() {
  if (!session) return;
  const prompt = dom.prompt.value.trim();
  if (!prompt) { toast('Опишите, что сделать в выделенной области', 'warn'); dom.prompt.focus(); return; }
  if (!maskHasContent()) { toast('Сначала выделите область или закрасьте её кистью', 'warn'); return; }

  const btn = dom.run;
  btn.disabled = true;
  try {
    status('Готовим исходник…');
    const sourceName = await stageSource();
    status('Отправляем маску…');
    const maskName = await uploadMask();
    status('Ставим задание…');
    const params = {
      image: sourceName,
      mask: maskName,
      prompt,
      denoise: Number(dom.denoise.value),
      grow_mask: Number(dom.grow.value) > 0,
      grow: Number(dom.grow.value) || 0,
      feather: Number(dom.feather.value) || 0,
      noise_mask: true,
      steps: Number(dom.steps.value) || 20,
    };
    const negative = dom.negative.value.trim();
    if (negative) params.negative = negative;

    const res = await api('/generate', {
      method: 'POST',
      body: { preset: INPAINT_PRESET, params, title: 'Ретушь области' },
    });
    const b = bridge();
    if (typeof b.upsertJob === 'function' && res.job) b.upsertJob(res.job);
    status('Задание в очереди — результат появится в галерее', 'ok');
    toast('Перерисовка области запущена', 'ok', 4000);
  } catch (e) {
    status('Ошибка: ' + e.message, 'bad');
    toast('Не удалось запустить перерисовку: ' + e.message, 'error', 9000);
  } finally {
    btn.disabled = false;
  }
}

const MODE_LABELS = {
  region: 'Перерисовать область',
  expand: 'Расширить кадр',
  upscale: 'Увеличить',
  controlnet: 'Сгенерировать по структуре',
  instruct: 'Выполнить правку',
  nobg: 'Убрать фон',
};

const MODE_HINTS = {
  region: 'Выделите участок кистью/лассо и опишите, что там должно быть.',
  expand: 'Кадр вырастет на указанные поля, движок дорисует края.',
  upscale: 'Без модели — обычный ресайз; с моделью — нейросетевой апскейл.',
  controlnet: 'Движок будет следовать структуре этой картинки (контуры, глубина, поза).',
  instruct: 'Опишите правку командой — меняется вся картинка.',
  nobg: 'Вырезает объект, фон станет прозрачным.',
};

/* Переключение режима: показываем только нужные секции и меняем подпись кнопки. */
function applyMode() {
  if (!session) return;
  const mode = dom.mode.value;
  session.mode = mode;
  dom.root.querySelectorAll('[data-mode]').forEach((sec) => {
    sec.hidden = sec.dataset.mode !== mode;
  });
  dom.modeHint.textContent = MODE_HINTS[mode] || '';
  dom.run.innerHTML = '<i class="fas fa-wand-magic-sparkles"></i> ' + (MODE_LABELS[mode] || 'Запустить');
  // Инструменты выделения нужны только для инпейнта.
  dom.tools.hidden = mode !== 'region';
  if (mode !== 'region') dom.stage.classList.add('is-panless');
  else dom.stage.classList.remove('is-panless');
}

async function runPreset(preset, params, title) {
  const res = await api('/generate', {
    method: 'POST',
    body: { preset, params, title },
  });
  const b = bridge();
  if (typeof b.upsertJob === 'function' && res.job) b.upsertJob(res.job);
  status('Задание в очереди — результат появится в галерее', 'ok');
  toast(title + ' запущено', 'ok', 4000);
}

async function runAction() {
  if (!session) return;
  const mode = session.mode || 'region';
  if (mode === 'region') return runInpaint();
  const btn = dom.run;
  btn.disabled = true;
  try {
    if (mode === 'expand') await runExpand();
    else if (mode === 'upscale') await runUpscale();
    else if (mode === 'controlnet') await runControlnet();
    else if (mode === 'instruct') await runInstruct();
    else if (mode === 'nobg') await runNobg();
  } catch (e) {
    status('Ошибка: ' + e.message, 'bad');
    toast(e.message, 'error', 9000);
  } finally {
    btn.disabled = false;
  }
}

async function runExpand() {
  const prompt = dom.expPrompt.value.trim();
  if (!prompt) { toast('Опишите, что продолжить за краями кадра', 'warn'); dom.expPrompt.focus(); return; }
  const left = Number(dom.expLeft.value) || 0;
  const top = Number(dom.expTop.value) || 0;
  const right = Number(dom.expRight.value) || 0;
  const bottom = Number(dom.expBottom.value) || 0;
  if (!left && !top && !right && !bottom) { toast('Укажите хотя бы одно поле расширения', 'warn'); return; }
  status('Готовим исходник…');
  const params = {
    image: await stageSource(),
    prompt, left, top, right, bottom,
    feathering: Number(dom.expFeather.value) || 0,
    denoise: Number(dom.expDenoise.value),
  };
  const neg = dom.expNegative.value.trim();
  if (neg) params.negative = neg;
  await runPreset('edit.outpaint', params, 'Расширение кадра');
}

async function runUpscale() {
  const useModel = dom.upUseModel.checked;
  const model = dom.upModel.value.trim();
  const width = Number(dom.upWidth.value) || 0;
  const height = Number(dom.upHeight.value) || 0;
  if (!(useModel && model) && !width && !height) {
    toast('Выберите модель апскейла или укажите размер', 'warn');
    return;
  }
  status('Готовим исходник…');
  const params = {
    image: await stageSource(),
    method: dom.upMethod.value,
    use_model: useModel,
    width, height,
  };
  if (model) params.model = model;
  await runPreset('edit.upscale', params, 'Апскейл');
}

async function runControlnet() {
  const cn = dom.cnModel.value.trim();
  if (!cn) { toast('Укажите модель ControlNet (models/controlnet)', 'warn'); dom.cnModel.focus(); return; }
  status('Готовим исходник…');
  const params = {
    image: await stageSource(),
    control_net: cn,
    preprocessor: dom.cnPrep.value,
    strength: Number(dom.cnStrength.value),
    width: Number(dom.cnWidth.value) || 1024,
    height: Number(dom.cnHeight.value) || 1024,
    denoise: Number(dom.cnDenoise.value),
  };
  const prompt = dom.cnPrompt.value.trim();
  if (prompt) params.prompt = prompt;
  const neg = dom.cnNegative.value.trim();
  if (neg) params.negative = neg;
  await runPreset('edit.controlnet', params, 'ControlNet');
}

async function runInstruct() {
  const cmd = dom.inPrompt.value.trim();
  if (!cmd) { toast('Впишите команду правки', 'warn'); dom.inPrompt.focus(); return; }
  status('Готовим исходник…');
  const params = {
    image: await stageSource(),
    prompt: cmd,
    denoise: Number(dom.inDenoise.value),
    image_cfg_scale: Number(dom.inCfg.value),
  };
  await runPreset('edit.instruct', params, 'Правка командой');
}

async function runNobg() {
  const model = dom.nbModel.value.trim();
  if (!model) { toast('Укажите модель удаления фона', 'warn'); dom.nbModel.focus(); return; }
  status('Готовим исходник…');
  await runPreset('edit.removebg', { image: await stageSource(), model }, 'Удаление фона');
}

async function applyAdjust() {
  if (!session) return;
  const brightness = Number(dom.bright.value) / 100;
  const contrast = Number(dom.contrast.value) / 100;
  if (!brightness && !contrast) { toast('Измените яркость или контраст', 'warn'); return; }
  try {
    status('Применяем…');
    const res = await api('/edit/adjust', {
      method: 'POST',
      body: { asset_id: session.asset.id, brightness, contrast },
    });
    toast('Сохранено: ' + res.name, 'ok', 4000);
    const b = bridge();
    if (typeof b.loadGallery === 'function') await b.loadGallery();
    closeEditor();
  } catch (e) {
    status('Ошибка: ' + e.message, 'bad');
    toast(e.message, 'error', 8000);
  }
}

async function cropBySelection() {
  if (!session || !session.cropRect) return;
  const c = session.cropRect;
  try {
    status('Обрезаем…');
    const res = await api('/edit/adjust', {
      method: 'POST',
      body: {
        asset_id: session.asset.id,
        crop: { x: Math.round(c.x), y: Math.round(c.y),
                w: Math.round(c.w), h: Math.round(c.h) },
      },
    });
    toast('Сохранено: ' + res.name, 'ok', 4000);
    const b = bridge();
    if (typeof b.loadGallery === 'function') await b.loadGallery();
    closeEditor();
  } catch (e) {
    status('Ошибка: ' + e.message, 'bad');
    toast(e.message, 'error', 8000);
  }
}

function setTool(tool) {
  if (!session) return;
  session.tool = tool;
  dom.tools.querySelectorAll('.gl-tool').forEach((b) => {
    b.classList.toggle('is-active', b.dataset.tool === tool);
  });
  dom.stage.classList.toggle('is-pan', tool === 'pan');
  hideCursor();
}

function bind() {
  const d = build();

  d.root.querySelector('#edClose').addEventListener('click', closeEditor);
  d.tools.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-tool]');
    if (btn) setTool(btn.dataset.tool);
  });

  d.root.querySelector('[data-zoom="in"]').addEventListener('click', () => zoomAt(1.25, stageCenter().x, stageCenter().y));
  d.root.querySelector('[data-zoom="out"]').addEventListener('click', () => zoomAt(0.8, stageCenter().x, stageCenter().y));
  d.root.querySelector('[data-zoom="fit"]').addEventListener('click', fitToWindow);

  d.stage.addEventListener('pointerdown', onPointerDown);
  d.stage.addEventListener('pointermove', onPointerMove);
  d.stage.addEventListener('pointerup', onPointerUp);
  d.stage.addEventListener('pointercancel', onPointerUp);
  d.stage.addEventListener('pointerleave', hideCursor);
  d.stage.addEventListener('wheel', onWheel, { passive: false });

  d.brush.addEventListener('input', () => { d.brushVal.textContent = d.brush.value; });
  d.denoise.addEventListener('input', () => { d.denoiseVal.textContent = Number(d.denoise.value).toFixed(2); });
  d.bright.addEventListener('input', () => { d.brightVal.textContent = d.bright.value; });
  d.contrast.addEventListener('input', () => { d.contrastVal.textContent = d.contrast.value; });
  d.expDenoise.addEventListener('input', () => { d.expDenoiseVal.textContent = Number(d.expDenoise.value).toFixed(2); });
  d.cnStrength.addEventListener('input', () => { d.cnStrengthVal.textContent = Number(d.cnStrength.value).toFixed(2); });
  d.cnDenoise.addEventListener('input', () => { d.cnDenoiseVal.textContent = Number(d.cnDenoise.value).toFixed(2); });
  d.inDenoise.addEventListener('input', () => { d.inDenoiseVal.textContent = Number(d.inDenoise.value).toFixed(2); });
  d.inCfg.addEventListener('input', () => { d.inCfgVal.textContent = Number(d.inCfg.value).toFixed(1); });
  d.mode.addEventListener('change', applyMode);

  d.root.querySelector('#edClear').addEventListener('click', clearMask);
  d.root.querySelector('#edInvert').addEventListener('click', invertMask);
  d.undo.addEventListener('click', undo);
  d.redo.addEventListener('click', redo);
  d.crop.addEventListener('click', cropBySelection);
  d.root.querySelector('#edApply').addEventListener('click', applyAdjust);
  d.run.addEventListener('click', runAction);

  document.addEventListener('keydown', (ev) => {
    if (!session) return;
    if (ev.key === 'Escape') { closeEditor(); return; }
    if (ev.target && /INPUT|TEXTAREA/.test(ev.target.tagName)) return;
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === 'z' || ev.key === 'Z')) {
      ev.preventDefault();
      if (ev.shiftKey) redo(); else undo();
      return;
    }
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === 'y' || ev.key === 'Y')) {
      ev.preventDefault();
      redo();
      return;
    }
    if (ev.ctrlKey || ev.metaKey) return;
    const map = {
      '1': 'select', '2': 'lasso', '3': 'brush', '4': 'erase', '5': 'pan',
      b: 'brush', B: 'brush', e: 'erase', E: 'erase', p: 'pan', P: 'pan',
    };
    if (map[ev.key]) setTool(map[ev.key]);
  });

  window.addEventListener('resize', () => { if (session) fitToWindow(); });
}

function stageCenter() {
  const r = dom.stage.getBoundingClientRect();
  return { x: r.width / 2, y: r.height / 2 };
}

/* -------------------------------------------------------------- экспорт */

window.GenLabEditor = {
  open: openEditor,
  close: closeEditor,
  ready: false,
};

document.addEventListener('DOMContentLoaded', () => {
  try {
    build();
    bind();
    window.GenLabEditor.ready = true;
  } catch (e) {
    console.error('[genlab/editor] не удалось инициализировать:', e);
  }
});
