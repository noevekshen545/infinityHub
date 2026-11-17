/* Мастер первой настройки и интерактивный тур.
 *
 * Показываются один раз на профиль: отметки лежат в frontend state профиля
 * (routes/onboarding.py). Глобально выключаются в админ-панели. Внутри iframe
 * рабочей области не запускаются — только в верхнем документе.
 *
 * Тур и мастер не накладываются: тур стартует строго после того, как мастер
 * закрыт (или если мастер уже пройден).
 */
(function () {
  'use strict';

  if (window.top !== window.self) return;
  if (window.__infinityOnboarding) return;
  window.__infinityOnboarding = true;

  var state = null;
  var wizardEl = null;
  var tourEls = null;

  function isDesktopShell() {
    return !!(document.getElementById('siteFrame') &&
              document.querySelector('.taskbar'));
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    return fetch(path, opts).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  var ICONS = {
    spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M3 12h3M18 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/><circle cx="12" cy="12" r="3.2"/></svg>',
    folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
    chip: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg>',
    palette: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="8" cy="10" r="1.2" fill="currentColor"/><circle cx="12" cy="7.5" r="1.2" fill="currentColor"/><circle cx="16" cy="10" r="1.2" fill="currentColor"/><path d="M12 21c1.6 0 2-1 1.4-2.2-.5-1 .1-2.1 1.4-2.1h1.5"/></svg>',
    mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.6v.6"/></svg>',
    rocket: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4c3.5-.6 6 .4 6 .4s1 2.5.4 6c-.5 3-2.6 5.6-5.2 7l-3.6-3.6C13 11.2 15.6 9 14 4z"/><path d="M10.4 13.6 6.8 10c-1.6 2.6-2.4 4.6-2.4 6l3 3c1.4 0 3.4-.8 6-2.4z"/><circle cx="16" cy="8" r="1.3"/></svg>'
  };

  /* ===================== Общие помощники ===================== */

  function lockScroll(on) {
    try {
      document.documentElement.style.overflow = on ? 'hidden' : '';
    } catch (e) { /* страница может не иметь html */ }
  }

  function mark(step) {
    return api('/api/onboarding/done', {
      method: 'POST',
      body: JSON.stringify({ step: step })
    }).catch(function () { /* оффлайн — покажем в следующий раз */ });
  }

  function afterWizard() {
    wizardEl = null;
    lockScroll(false);
    if (isDesktopShell() && state && state.tour_enabled && !state.tour_done) {
      setTimeout(showTour, 420);
    }
  }

  /* ===================== Данные ===================== */

  function loadBootstrap() {
    return Promise.all([
      api('/api/config').catch(function () { return {}; }),
      api('/api/speech/engines').catch(function () { return {}; })
    ]).then(function (res) {
      return { cfg: (res[0] && res[0].config) || {}, speech: res[1] || {} };
    });
  }

  var THEME_NAMES = {
    light: 'Светлая', dark: 'Тёмная', sepia: 'Сепия', warm: 'Тёплая',
    paper: 'Бумага', ocean: 'Океан', forest: 'Лес', midnight: 'Полночь',
    rose: 'Розовая'
  };

  function themeList() {
    /* InfinityTheme.themes — массив строк ('dark', 'ocean' …), а не объектов. */
    var t = window.InfinityTheme && window.InfinityTheme.themes;
    if (t && t.length) {
      return t.map(function (x) {
        var id = typeof x === 'string' ? x : (x.id || x.name || '');
        return { id: id, name: THEME_NAMES[id] || (typeof x === 'object' && x.name) || id };
      });
    }
    return Object.keys(THEME_NAMES).map(function (id) {
      return { id: id, name: THEME_NAMES[id] };
    });
  }

  function themeSwatch(id) {
    var map = {
      light: '#f6f7fb', dark: '#1b1f2a', sepia: '#f3e9d7', warm: '#f7efe6',
      paper: '#faf8f2', ocean: '#e7f1f7', forest: '#e9f2ea',
      midnight: '#141a26', rose: '#fbeef1'
    };
    return map[id] || '#e8eaf0';
  }

  function accentPresets() {
    var p = window.InfinityTheme && window.InfinityTheme.accent &&
            window.InfinityTheme.accent.presets;
    if (p && p.length) return p.map(function (x) { return x.hex; });
    return ['#6366f1', '#4f46e5', '#2563eb', '#0891b2', '#059669',
            '#65a30d', '#d97706', '#ea580c', '#db2777', '#dc2626'];
  }

  function currentAccent() {
    try {
      if (window.InfinityTheme && window.InfinityTheme.accent) {
        return window.InfinityTheme.accent.get() || '';
      }
      return localStorage.getItem('infinity_accent') || '';
    } catch (e) { return ''; }
  }

  function currentTheme() {
    try {
      if (window.InfinityTheme && window.InfinityTheme.get) {
        return window.InfinityTheme.get() || 'light';
      }
      return localStorage.getItem('infinity_theme') || 'light';
    } catch (e) { return 'light'; }
  }

  /* ===================== Мастер ===================== */

  var draft = null;
  var stepIndex = 0;

  var STEPS = [
    { id: 'hello', title: 'Добро пожаловать' },
    { id: 'paths', title: 'Папки и данные' },
    { id: 'ai', title: 'ИИ-движок' },
    { id: 'look', title: 'Оформление' },
    { id: 'voice', title: 'Речь' },
    { id: 'ready', title: 'Готово' }
  ];

  function showWizard() {
    if (wizardEl) return;
    lockScroll(true);
    wizardEl = document.createElement('div');
    wizardEl.className = 'ob-overlay ob-root';
    wizardEl.setAttribute('role', 'dialog');
    wizardEl.setAttribute('aria-modal', 'true');
    wizardEl.setAttribute('aria-label', 'Мастер первой настройки');
    wizardEl.innerHTML =
      '<div class="ob-card">' +
        '<aside class="ob-rail">' +
          '<div class="ob-brand">' +
            '<div class="ob-brand-mark">' + ICONS.spark + '</div>' +
            '<div class="ob-brand-text"><b>Infinity Search</b><span>Первая настройка</span></div>' +
          '</div>' +
          '<ol class="ob-steps">' + STEPS.map(function (s, i) {
            return '<li class="ob-step" data-step="' + i + '">' +
                     '<span class="ob-step-dot">' + (i + 1) + '</span>' +
                     '<span>' + esc(s.title) + '</span></li>';
          }).join('') + '</ol>' +
          '<div class="ob-rail-foot">Мастер можно пропустить — всё это доступно позже в разделе «Настройки».</div>' +
        '</aside>' +
        '<section class="ob-main">' +
          '<div class="ob-progress"><i></i></div>' +
          '<div class="ob-body" id="obWizardBody"></div>' +
          '<footer class="ob-foot">' +
            '<button type="button" class="ob-btn ghost" id="obWizardSkip">Пропустить</button>' +
            '<div class="ob-foot-right">' +
              '<button type="button" class="ob-btn" id="obWizardBack">Назад</button>' +
              '<button type="button" class="ob-btn primary" id="obWizardNext">Далее</button>' +
            '</div>' +
          '</footer>' +
        '</section>' +
      '</div>';
    document.body.appendChild(wizardEl);

    wizardEl.addEventListener('click', function (e) {
      if (e.target === wizardEl) return; /* клик по фону не закрывает мастер */
    });
    wizardEl.querySelector('#obWizardSkip').addEventListener('click', finishWizard);
    wizardEl.querySelector('#obWizardBack').addEventListener('click', function () {
      captureStep();
      if (stepIndex > 0) { stepIndex--; renderStep(); }
    });
    wizardEl.querySelector('#obWizardNext').addEventListener('click', function () {
      captureStep();
      if (stepIndex < STEPS.length - 1) { stepIndex++; renderStep(); }
      else finishWizard();
    });
    document.addEventListener('keydown', wizardKeys);

    draft = {
      cfg: {}, speech: {},
      paths: { books: null, notes: null, zim: null },
      ai: { backend: null, url: null, model: null },
      look: { theme: null, accent: null },
      voice: { tts: null, stt: null }
    };
    stepIndex = 0;
    renderStep();

    loadBootstrap().then(function (data) {
      if (!wizardEl) return;
      captureStep();  /* не растерять введённое, пока грузился конфиг */
      draft.cfg = data.cfg || {};
      draft.speech = data.speech || {};
      renderStep();
    });
  }

  function wizardKeys(e) {
    if (!wizardEl) { document.removeEventListener('keydown', wizardKeys); return; }
    if (e.key === 'Escape') { closeWizard(true); }
    if (e.key === 'Enter' && !(e.target && /INPUT|SELECT|TEXTAREA/.test(e.target.tagName))) {
      e.preventDefault();
      if (stepIndex < STEPS.length - 1) { stepIndex++; renderStep(); }
      else finishWizard();
    }
  }

  function closeWizard(markDone) {
    if (!wizardEl) return;
    document.removeEventListener('keydown', wizardKeys);
    wizardEl.remove();
    if (markDone) { mark('wizard'); afterWizard(); }
    else { wizardEl = null; lockScroll(false); }
  }

  function finishWizard() {
    var saveBtn = wizardEl && wizardEl.querySelector('#obWizardNext');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Сохраняем…'; }
    saveWizard().catch(function (e) {
      console.warn('[onboarding] сохранение настроек:', e);
    }).then(function () {
      closeWizard(true);
    });
  }

  function renderStep() {
    if (!wizardEl) return;
    var body = wizardEl.querySelector('#obWizardBody');
    if (!body) return;
    var renderers = {
      hello: renderHello, paths: renderPaths, ai: renderAi,
      look: renderLook, voice: renderVoice, ready: renderReady
    };
    var step = STEPS[stepIndex];
    body.innerHTML = (renderers[step.id] || renderHello)();
    body.scrollTop = 0;

    var progress = wizardEl.querySelector('.ob-progress i');
    if (progress) progress.style.width = ((stepIndex + 1) / STEPS.length * 100) + '%';

    Array.prototype.forEach.call(wizardEl.querySelectorAll('.ob-step'), function (el) {
      var i = parseInt(el.getAttribute('data-step'), 10);
      el.classList.toggle('is-active', i === stepIndex);
      el.classList.toggle('is-done', i < stepIndex);
      if (i < stepIndex) el.querySelector('.ob-step-dot').innerHTML = ICONS.check;
      else el.querySelector('.ob-step-dot').textContent = String(i + 1);
    });

    var back = wizardEl.querySelector('#obWizardBack');
    if (back) back.style.visibility = stepIndex > 0 ? 'visible' : 'hidden';
    var next = wizardEl.querySelector('#obWizardNext');
    if (next) {
      next.disabled = false;
      next.textContent = stepIndex === STEPS.length - 1 ? 'Начать работу' : 'Далее';
    }
    wireStep(step.id);
  }

  function renderHello() {
    return '' +
      '<div class="ob-eyebrow">' + ICONS.spark + ' Быстрый старт</div>' +
      '<h2 class="ob-title">Настроим Infinity Search за минуту</h2>' +
      '<p class="ob-sub">Пять коротких шагов: где хранить данные, какой ИИ-движок использовать, как оформить интерфейс и каким голосом озвучивать тексты. Любой шаг можно пропустить — настройки останутся прежними.</p>' +
      '<div class="ob-hero">' +
        '<div class="ob-hero-card"><i>' + ICONS.folder + '</i><b>Пути</b><span>Книги, заметки и ZIM-библиотека</span></div>' +
        '<div class="ob-hero-card"><i>' + ICONS.chip + '</i><b>ИИ</b><span>Локальный движок или облако</span></div>' +
        '<div class="ob-hero-card"><i>' + ICONS.palette + '</i><b>Вид</b><span>Тема и акцентный цвет</span></div>' +
      '</div>' +
      '<div class="ob-hint">' + ICONS.info + '<span>Мастер показывается один раз для каждого профиля. Вернуться к настройкам можно в любой момент через меню «Настройки».</span></div>';
  }

  function renderPaths() {
    var cfg = draft.cfg || {};
    var zim = (cfg.zim_dirs && cfg.zim_dirs[0]) || '';
    var books = draft.paths.books !== null ? draft.paths.books : (cfg.books_dir || '');
    var notes = draft.paths.notes !== null ? draft.paths.notes : (cfg.notes_dir || '');
    if (draft.paths.zim !== null) zim = draft.paths.zim;
    return '' +
      '<div class="ob-eyebrow">' + ICONS.folder + ' Шаг 2</div>' +
      '<h2 class="ob-title">Где хранить данные</h2>' +
      '<p class="ob-sub">Укажите существующие каталоги. Пустые поля останутся со значениями по умолчанию.</p>' +
      '<div class="ob-grid">' +
        '<div class="ob-field ob-field-full"><label>Каталог книг</label>' +
          '<input class="ob-input" id="obBooksDir" type="text" placeholder="D:\\Books" value="' + esc(books) + '">' +
        '</div>' +
        '<div class="ob-field ob-field-full"><label>Каталог заметок</label>' +
          '<input class="ob-input" id="obNotesDir" type="text" placeholder="D:\\Notes" value="' + esc(notes) + '">' +
        '</div>' +
        '<div class="ob-field ob-field-full"><label>Каталог ZIM-библиотеки</label>' +
          '<input class="ob-input" id="obZimDir" type="text" placeholder="D:\\ZIM" value="' + esc(zim) + '">' +
          '<small>Оффлайн-энциклопедии Wikipedia и другие архивы .zim</small>' +
        '</div>' +
      '</div>' +
      '<div class="ob-hint">' + ICONS.info + '<span>Пути можно поменять позже в «Настройках → Пути». Индекс пересоберётся автоматически при поиске.</span></div>';
  }

  function renderAi() {
    var cfg = draft.cfg || {};
    var backend = draft.ai.backend || cfg.backend || 'llamacpp';
    var url = backend === 'lmstudio'
      ? ((cfg.lm_studio && cfg.lm_studio.url) || '')
      : ((cfg.llama_cpp && cfg.llama_cpp.url) || '');
    if (backend === 'custom') url = (cfg.custom && cfg.custom.url) || '';
    if (draft.ai.url !== null) url = draft.ai.url;
    var model = draft.ai.model !== null ? draft.ai.model : ((cfg.ai && cfg.ai.model) || '');
    return '' +
      '<div class="ob-eyebrow">' + ICONS.chip + ' Шаг 3</div>' +
      '<h2 class="ob-title">ИИ-движок</h2>' +
      '<p class="ob-sub">Кто будет отвечать на вопросы и писать тексты. По умолчанию — локальный llama.cpp, он уже есть в сборке.</p>' +
      '<div class="ob-grid">' +
        '<div class="ob-field ob-field-full"><label>Бэкенд</label>' +
          '<select class="ob-select" id="obBackend">' +
            '<option value="llamacpp"' + (backend === 'llamacpp' ? ' selected' : '') + '>llama.cpp (локально)</option>' +
            '<option value="lmstudio"' + (backend === 'lmstudio' ? ' selected' : '') + '>LM Studio (локально)</option>' +
            '<option value="custom"' + (backend === 'custom' ? ' selected' : '') + '>Свой OpenAI-совместимый сервер</option>' +
          '</select>' +
        '</div>' +
        '<div class="ob-field"><label>Адрес сервера</label>' +
          '<input class="ob-input" id="obAiUrl" type="text" placeholder="http://127.0.0.1:8080" value="' + esc(url) + '">' +
        '</div>' +
        '<div class="ob-field"><label>Модель (необязательно)</label>' +
          '<input class="ob-input" id="obAiModel" type="text" placeholder="по умолчанию" value="' + esc(model) + '">' +
        '</div>' +
      '</div>' +
      '<div class="ob-hint">' + ICONS.info + '<span>Если оставить адрес пустым, будет использован адрес выбранного бэкенда из конфигурации.</span></div>';
  }

  function renderLook() {
    var theme = currentTheme();
    var accent = currentAccent();
    var themes = themeList();
    return '' +
      '<div class="ob-eyebrow">' + ICONS.palette + ' Шаг 4</div>' +
      '<h2 class="ob-title">Оформление</h2>' +
      '<p class="ob-sub">Тема и цвет применяются сразу — можно посмотреть, как это выглядит, и переключить обратно.</p>' +
      '<div class="ob-field" style="margin-bottom:16px"><label>Тема</label>' +
        '<div class="ob-themes" id="obThemes">' + themes.map(function (t) {
          var id = t.id || t.name;
          return '<button type="button" class="ob-theme' + (id === theme ? ' is-active' : '') +
                 '" data-theme="' + esc(id) + '">' +
                 '<span class="ob-theme-swatch" style="background:' + esc(themeSwatch(id)) + '"></span>' +
                 '<span>' + esc(t.name || id) + '</span></button>';
        }).join('') + '</div>' +
      '</div>' +
      '<div class="ob-field"><label>Акцентный цвет</label>' +
        '<div class="ob-swatches" id="obSwatches">' + accentPresets().map(function (hex) {
          return '<button type="button" class="ob-swatch' + (hex === accent ? ' is-active' : '') +
                 '" data-hex="' + esc(hex) + '" title="' + esc(hex) + '" style="background:' + esc(hex) + '"></button>';
        }).join('') +
        '<input type="color" class="ob-color-custom" id="obAccentCustom" value="' + esc(accent || '#6366f1') + '" title="Свой цвет">' +
        '</div>' +
        '<small>Свой акцент перекрывает цвет темы во всём интерфейсе.</small>' +
      '</div>';
  }

  function engineOptions(selected, list) {
    return list.map(function (e) {
      return '<option value="' + esc(e.id) + '"' + (e.id === selected ? ' selected' : '') + '>' + esc(e.name) + '</option>';
    }).join('');
  }

  function renderVoice() {
    var sp = draft.speech || {};
    var tts = sp.tts || {};
    var stt = sp.stt || {};
    var ttsSel = draft.voice.tts || tts.selected || 'auto';
    var sttSel = draft.voice.stt || stt.selected || 'local';
    /* /api/speech/engines отдаёт engines массивом id; старый формат — объект
       {id: {available}}. Поддерживаем оба, иначе в списке остаётся только «Авто». */
    var TTS_NAMES = { silero: 'Silero', piper: 'Piper', qwen3: 'Qwen3-TTS', melo: 'MeloTTS', webspeech: 'Web Speech (в браузере)' };
    var STT_NAMES = { local: 'faster-whisper (локально)', openai: 'OpenAI Whisper (API)', web: 'Web Speech (браузер)' };
    function engineIds(v) {
      if (Array.isArray(v)) return v.slice();
      if (v && typeof v === 'object') {
        return Object.keys(v).filter(function (k) { return !v[k] || v[k].available !== false; });
      }
      return [];
    }
    var ttsList = [{ id: 'auto', name: 'Авто (лучший доступный)' }];
    engineIds(tts.engines).forEach(function (id) {
      ttsList.push({ id: id, name: TTS_NAMES[id] || id });
    });
    (tts.custom || []).forEach(function (e) {
      ttsList.push({ id: 'custom:' + e.id, name: 'Кастомный: ' + (e.name || e.id) });
    });

    var sttList = [];
    engineIds(stt.engines).forEach(function (id) {
      sttList.push({ id: id, name: STT_NAMES[id] || id });
    });
    (stt.custom || []).forEach(function (e) {
      sttList.push({ id: 'custom:' + e.id, name: 'Кастомный: ' + (e.name || e.id) });
    });

    return '' +
      '<div class="ob-eyebrow">' + ICONS.mic + ' Шаг 5</div>' +
      '<h2 class="ob-title">Речь</h2>' +
      '<p class="ob-sub">Озвучивание книг и распознавание голосовых команд. Движки, недоступные на этом компьютере, можно выбрать — сервер переключится, когда они появятся.</p>' +
      '<div class="ob-grid">' +
        '<div class="ob-field"><label>Синтез речи (TTS)</label>' +
          '<select class="ob-select" id="obTts">' + engineOptions(ttsSel, ttsList) + '</select>' +
        '</div>' +
        '<div class="ob-field"><label>Распознавание речи (STT)</label>' +
          '<select class="ob-select" id="obStt">' + engineOptions(sttSel, sttList) + '</select>' +
        '</div>' +
      '</div>' +
      '<div class="ob-hint">' + ICONS.info + '<span>Голоса, скорость и язык настраиваются отдельно в «Настройках → Речь».</span></div>';
  }

  function renderReady() {
    return '' +
      '<div class="ob-eyebrow">' + ICONS.rocket + ' Всё готово</div>' +
      '<h2 class="ob-title">Можно начинать</h2>' +
      '<p class="ob-sub">Настройки будут сохранены в профиль. Дальше покажем короткий тур по интерфейсу — он займёт меньше минуты.</p>' +
      '<div class="ob-hint">' + ICONS.check + '<span>Мастер больше не появится. Отключить или включить его снова можно в админ-панели.</span></div>';
  }

  function wireStep(id) {
    if (!wizardEl) return;
    if (id === 'ai') {
      var sel = wizardEl.querySelector('#obBackend');
      if (sel) sel.addEventListener('change', function () {
        var cfg = draft.cfg || {};
        var urlEl = wizardEl.querySelector('#obAiUrl');
        if (!urlEl) return;
        var v = sel.value;
        if (v === 'lmstudio') urlEl.value = (cfg.lm_studio && cfg.lm_studio.url) || 'http://localhost:1234';
        else if (v === 'llamacpp') urlEl.value = (cfg.llama_cpp && cfg.llama_cpp.url) || 'http://127.0.0.1:8080';
        else urlEl.value = (cfg.custom && cfg.custom.url) || '';
      });
    }
    if (id === 'look') {
      Array.prototype.forEach.call(wizardEl.querySelectorAll('.ob-theme'), function (b) {
        b.addEventListener('click', function () {
          var theme = b.getAttribute('data-theme');
          if (window.InfinityTheme && window.InfinityTheme.set) {
            try { window.InfinityTheme.set(theme); } catch (e) { /* тема останется прежней */ }
          } else {
            try { localStorage.setItem('infinity_theme', theme); } catch (e) {}
          }
          draft.look.theme = theme;
          Array.prototype.forEach.call(wizardEl.querySelectorAll('.ob-theme'), function (x) {
            x.classList.toggle('is-active', x === b);
          });
        });
      });
      Array.prototype.forEach.call(wizardEl.querySelectorAll('.ob-swatch'), function (b) {
        b.addEventListener('click', function () {
          applyAccent(b.getAttribute('data-hex'));
          Array.prototype.forEach.call(wizardEl.querySelectorAll('.ob-swatch'), function (x) {
            x.classList.toggle('is-active', x === b);
          });
          var custom = wizardEl.querySelector('#obAccentCustom');
          if (custom) custom.value = b.getAttribute('data-hex');
        });
      });
      var custom = wizardEl.querySelector('#obAccentCustom');
      if (custom) custom.addEventListener('input', function () {
        applyAccent(custom.value);
        Array.prototype.forEach.call(wizardEl.querySelectorAll('.ob-swatch'), function (x) {
          x.classList.remove('is-active');
        });
      });
      var cur = wizardEl.querySelector('.ob-swatch.is-active');
      if (cur && custom) custom.value = cur.getAttribute('data-hex');
    }
  }

  function applyAccent(hex) {
    draft.look.accent = hex;
    try {
      if (window.InfinityTheme && window.InfinityTheme.accent) {
        window.InfinityTheme.accent.set(hex);
      } else {
        localStorage.setItem('infinity_accent', hex);
        document.documentElement.style.setProperty('--accent', hex);
      }
    } catch (e) { /* декоративно */ }
  }

  /* Значения текущего шага переносим в draft: шаги перерисовываются,
     и без этого введённое на предыдущем шаге терялось бы к моменту «Готово». */
  function captureStep() {
    if (!wizardEl || !draft) return;
    var q = function (sel) { var el = wizardEl.querySelector(sel); return el ? el.value.trim() : null; };
    var v;
    if ((v = q('#obBooksDir')) !== null) draft.paths.books = v;
    if ((v = q('#obNotesDir')) !== null) draft.paths.notes = v;
    if ((v = q('#obZimDir')) !== null) draft.paths.zim = v;
    if ((v = q('#obBackend')) !== null) draft.ai.backend = v;
    if ((v = q('#obAiUrl')) !== null) draft.ai.url = v;
    if ((v = q('#obAiModel')) !== null) draft.ai.model = v;
    if ((v = q('#obTts')) !== null) draft.voice.tts = v;
    if ((v = q('#obStt')) !== null) draft.voice.stt = v;
  }

  function collectWizard() {
    if (!draft) return null;
    var out = {};
    if (draft.paths.books) out.books_dir = draft.paths.books;
    if (draft.paths.notes) out.notes_dir = draft.paths.notes;
    if (draft.paths.zim) out.zim_dirs = [draft.paths.zim];

    if (draft.ai.backend) {
      var backend = draft.ai.backend;
      var url = draft.ai.url || '';
      var model = draft.ai.model || '';
      out.backend = backend;
      if (backend === 'llamacpp' && url) out.llama_cpp = { url: url };
      else if (backend === 'lmstudio' && url) out.lm_studio = { url: url };
      else if (backend === 'custom' && url) out.custom = { url: url, model: model };
      if (model && backend !== 'custom') out.ai = { model: model };
    }

    if (draft.voice.tts || draft.voice.stt) {
      out.speech = {
        tts_engine: draft.voice.tts || 'auto',
        stt_engine: draft.voice.stt || 'local'
      };
    }
    return out;
  }

  function saveWizard() {
    captureStep();
    var delta = collectWizard();
    if (!delta) return Promise.resolve();
    delta.ui = { theme: draft.look.theme || currentTheme(), language: 'ru' };
    /* /api/config/save ждёт {config: {...}}: без обёртки сервер сохранял
       пустой объект и отвечал ok — настройки мастера молча терялись. */
    return api('/api/config/save', {
      method: 'POST',
      body: JSON.stringify({ config: delta })
    });
  }

  /* ===================== Тур ===================== */

  var tourSteps = [
    {
      sel: '#siteFrame',
      title: 'Рабочая область',
      text: 'Здесь открываются страницы, книги, заметки и приложения. Боковую панель можно свернуть, чтобы освободить место.'
    },
    {
      sel: '.taskbar-start',
      title: 'Меню приложений',
      text: 'Кнопка «Apps» открывает все установленные приложения, поиск по ним и папки. Приложения можно закреплять и перетаскивать.'
    },
    {
      sel: '#dashTrayBtn',
      title: 'Дашборд',
      text: 'Сводка по системе: погода, курсы, статистика библиотеки и быстрые действия — всё на одном экране.'
    },
    {
      sel: '#aibarTray',
      title: 'ИИ-агент',
      text: 'Ассистент всегда под рукой: задайте вопрос, попросите найти материал или запустить действие.'
    },
    {
      sel: '#notifyBell',
      title: 'Уведомления',
      text: 'Здесь появляются сообщения сервера, индексации и напоминания. Настройки — в меню приложений, иконка шестерёнки.'
    }
  ];

  function showTour() {
    if (tourEls || !isDesktopShell()) return;
    var steps = tourSteps.filter(function (s) {
      return !!document.querySelector(s.sel);
    });
    if (!steps.length) return;
    tourEls = { index: 0, steps: steps, ring: null, card: null };
    state.tour_running = true;

    var ring = document.createElement('div');
    ring.className = 'ob-tour-ring ob-root';
    var card = document.createElement('div');
    card.className = 'ob-tour-card ob-root';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-label', 'Обзор интерфейса');
    document.body.appendChild(ring);
    document.body.appendChild(card);
    tourEls.ring = ring;
    tourEls.card = card;

    document.addEventListener('keydown', tourKeys);
    window.addEventListener('resize', tourReposition);
    renderTourStep();
  }

  function tourKeys(e) {
    if (!tourEls) return;
    if (e.key === 'Escape') { endTour(true); }
    else if (e.key === 'ArrowRight') { tourNext(); }
    else if (e.key === 'ArrowLeft') { tourPrev(); }
  }

  function renderTourStep() {
    if (!tourEls) return;
    var t = tourEls;
    var step = t.steps[t.index];
    var last = t.index === t.steps.length - 1;
    var target = step.sel ? document.querySelector(step.sel) : null;

    t.card.innerHTML =
      '<div class="ob-tour-top">' +
        '<span class="ob-tour-chip">' + (t.index + 1) + ' / ' + t.steps.length + '</span>' +
        '<button type="button" class="ob-tour-close" title="Пропустить тур">&times;</button>' +
      '</div>' +
      '<h3 class="ob-tour-title">' + esc(step.title) + '</h3>' +
      '<p class="ob-tour-text">' + esc(step.text) + '</p>' +
      '<div class="ob-tour-foot">' +
        '<div class="ob-tour-dots">' + t.steps.map(function (_, i) {
          return '<span class="ob-tour-dot' + (i === t.index ? ' is-active' : '') + '"></span>';
        }).join('') + '</div>' +
        '<button type="button" class="ob-tour-btn" data-act="skip">Пропустить</button>' +
        (t.index > 0 ? '<button type="button" class="ob-tour-btn" data-act="prev">Назад</button>' : '') +
        '<button type="button" class="ob-tour-btn primary" data-act="next">' + (last ? 'Начать работу' : 'Далее') + '</button>' +
      '</div>';

    t.card.querySelector('.ob-tour-close').addEventListener('click', function () { endTour(true); });
    t.card.querySelectorAll('[data-act]').forEach(function (b) {
      b.addEventListener('click', function () {
        var act = b.getAttribute('data-act');
        if (act === 'skip') endTour(true);
        else if (act === 'prev') tourPrev();
        else if (act === 'next') tourNext();
      });
    });

    if (target) {
      try { target.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) {}
      t.ring.style.display = 'block';
      tourReposition();
    } else {
      t.ring.style.display = 'none';
      t.card.style.left = Math.max(12, (window.innerWidth - t.card.offsetWidth) / 2) + 'px';
      t.card.style.top = Math.max(12, (window.innerHeight - t.card.offsetHeight) / 2) + 'px';
    }
  }

  function tourReposition() {
    if (!tourEls) return;
    var t = tourEls;
    var step = t.steps[t.index];
    var el = step.sel ? document.querySelector(step.sel) : null;
    if (!el) return;
    var r = el.getBoundingClientRect();
    var pad = 6;
    t.ring.style.left = (r.left - pad) + 'px';
    t.ring.style.top = (r.top - pad) + 'px';
    t.ring.style.width = (r.width + pad * 2) + 'px';
    t.ring.style.height = (r.height + pad * 2) + 'px';

    var cw = t.card.offsetWidth;
    var ch = t.card.offsetHeight;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var left = Math.min(Math.max(12, r.left + r.width / 2 - cw / 2), vw - cw - 12);
    var top;
    if (r.bottom + 16 + ch <= vh - 12) top = r.bottom + 14;
    else if (r.top - 16 - ch >= 12) top = r.top - ch - 14;
    else top = Math.min(Math.max(12, vh - ch - 12), Math.max(12, r.bottom - ch - 14));
    t.card.style.left = left + 'px';
    t.card.style.top = top + 'px';
  }

  function tourNext() {
    if (!tourEls) return;
    if (tourEls.index < tourEls.steps.length - 1) {
      tourEls.index++;
      renderTourStep();
    } else endTour(true);
  }

  function tourPrev() {
    if (!tourEls) return;
    if (tourEls.index > 0) { tourEls.index--; renderTourStep(); }
  }

  function endTour(done) {
    if (!tourEls) return;
    document.removeEventListener('keydown', tourKeys);
    window.removeEventListener('resize', tourReposition);
    if (tourEls.ring) tourEls.ring.remove();
    if (tourEls.card) tourEls.card.remove();
    tourEls = null;
    if (done) mark('tour');
  }

  /* ===================== Старт ===================== */

  function boot() {
    /* Админ-панель — служебный экран, мастер и тур там не нужны. */
    if ((location.pathname || '').indexOf('/admin') === 0) return;
    api('/api/onboarding/state').then(function (s) {
      if (!s || !s.ok) return;
      state = s;
      if (s.wizard_enabled && !s.wizard_done) {
        showWizard();
      } else if (isDesktopShell() && s.tour_enabled && !s.tour_done) {
        setTimeout(showTour, 1100);
      }
    }).catch(function () { /* сервер старый или недоступен */ });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
