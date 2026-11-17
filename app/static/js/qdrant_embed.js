// Эмбеддинг-карточка Qdrant: статус модели, переключение CPU/GPU,
// скачивание и живой индикатор «что модель делает прямо сейчас».
// Выделено в отдельный файл, чтобы сбои здесь не ломали граф/коллекции.
(function () {
  function el(id) { return document.getElementById(id); }

  function loadEmbedInfo() {
    api('/api/qdrant/embed-info').then(function (r) { return r.json(); }).then(function (d) {
      var b = el('embStatus');
      if (d.loaded) { b.className = 'badge ok'; b.innerHTML = '<i class="fas fa-circle"></i> Модель активна'; }
      else if (d.present) { b.className = 'badge ok'; b.innerHTML = '<i class="fas fa-circle"></i> Модель загружена локально'; }
      else if (d.fastembed_available === false) { b.className = 'badge off'; b.innerHTML = '<i class="fas fa-circle"></i> fastembed недоступен'; }
      else { b.className = 'badge off'; b.innerHTML = '<i class="fas fa-circle"></i> Модель не скачана'; }
      el('embModel').textContent = d.model + (d.local_only ? ' · офлайн' : '');
      el('embCache').value = d.cache_dir || '';
      el('embLocalOnly').checked = !!d.local_only;
      var dev = el('embDevice');
      if (dev) { dev.value = (d.device === 'gpu') ? 'gpu' : 'cpu'; }
      var cuda = el('embCuda');
      if (cuda) {
        if (d.device === 'gpu' && !d.cuda_available) { cuda.className = 'badge off'; cuda.innerHTML = '<i class="fas fa-circle"></i> CUDA: недоступен здесь'; }
        else if (d.cuda_available) { cuda.className = 'badge ok'; cuda.innerHTML = '<i class="fas fa-circle"></i> CUDA: доступен'; }
        else { cuda.className = 'badge off'; cuda.innerHTML = '<i class="fas fa-circle"></i> CUDA: нет'; }
      }
      var m = el('embMsg');
      if (d.last_error && !d.present) { m.textContent = '⚠ ' + d.last_error; m.style.color = '#ef4444'; }
      else { m.textContent = ''; m.style.color = ''; }
      updateLive(d);
    }).catch(function (e) { console.error(e); });
  }

  function updateLive(d) {
    var live = el('embLive');
    if (!live) return;
    var parts = [];
    if (d.busy) {
      parts.push('● РАБОТАЕТ: ' + (d.last_op || 'эмбеддинг'));
      if (d.current_text) { parts.push('  → «' + d.current_text + '»'); }
      if (d.total) { parts.push('  [' + (d.processed || 0) + '/' + d.total + ']'); }
    } else {
      parts.push('○ ожидание');
      if (d.last_op) { parts.push('(последнее: ' + d.last_op + ', ' + (d.last_duration || 0) + 'с)'); }
    }
    if (d.last_error) { parts.push('⚠ ' + d.last_error); }
    live.textContent = parts.join('\n');
    live.style.color = d.busy ? 'var(--accent)' : 'var(--text)';
  }

  function downloadModel() {
    var btn = el('btnDownloadModel');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Скачивание…';
    el('embProg').style.display = 'block';
    el('embProg').firstElementChild.style.width = '0%';
    api('/api/qdrant/download-model', { method: 'POST' }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { el('embMsg').textContent = 'Ошибка: ' + d.error; el('embMsg').style.color = '#ef4444'; btn.disabled = false; btn.innerHTML = '<i class="fas fa-download"></i> Скачать модель'; return; }
      pollEmbedDownload(btn);
    }).catch(function (e) {
      el('embMsg').textContent = 'Ошибка запроса: ' + e;
      btn.disabled = false; btn.innerHTML = '<i class="fas fa-download"></i> Скачать модель';
    });
  }

  function pollEmbedDownload(btn) {
    var iv = setInterval(function () {
      api('/api/qdrant/download-model/status').then(function (r) { return r.json(); }).then(function (s) {
        var pct = (s.progress != null) ? s.progress : (s.running ? 50 : 100);
        el('embProg').firstElementChild.style.width = pct + '%';
        var sz = s.total ? (' (' + Math.round((s.downloaded || 0) / 1048576) + '/' + Math.round(s.total / 1048576) + ' МБ)') : '';
        var m = el('embMsg');
        m.textContent = (s.stage || '') + sz;
        m.style.color = '';
        if (!s.running) {
          clearInterval(iv);
          btn.disabled = false; btn.innerHTML = '<i class="fas fa-download"></i> Скачать модель';
          el('embProg').style.display = 'none';
          if (s.error) { m.textContent = 'Ошибка: ' + s.error; m.style.color = '#ef4444'; }
          else { m.textContent = '✓ Модель скачана, включён офлайн-режим'; m.style.color = ''; }
          loadEmbedInfo();
          loadStatus();
        }
      }).catch(function () { clearInterval(iv); btn.disabled = false; btn.innerHTML = '<i class="fas fa-download"></i> Скачать модель'; });
    }, 1000);
  }

  function saveEmbedCfg() {
    var localOnly = el('embLocalOnly').checked;
    var device = el('embDevice') ? el('embDevice').value : 'cpu';
    api('/api/qdrant/embed-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ embed_local_only: localOnly, embed_device: device })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { alert('Ошибка: ' + d.error); } else { loadEmbedInfo(); }
    }).catch(function (e) { alert('Ошибка: ' + e); });
  }

  // Живой опрос состояния (занятость / обрабатываемый текст) раз в 1.5с
  setInterval(function () {
    api('/api/qdrant/embed-info').then(function (r) { return r.json(); }).then(updateLive).catch(function () {});
  }, 1500);

  window.loadEmbedInfo = loadEmbedInfo;
  window.downloadModel = downloadModel;
  window.pollEmbedDownload = pollEmbedDownload;
  window.saveEmbedCfg = saveEmbedCfg;

  loadEmbedInfo();
})();
