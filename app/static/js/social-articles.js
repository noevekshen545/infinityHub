// Статьи групп и каналов (задача 1.6).
//
// Отдельный файл по той же причине, что и social-lightbox.js: шаблон
// page_social_friends.html уже большой, а здесь своя логика и своя
// зависимость — Markdown-рендеринг.
//
// Разметка приходит с сервера обычным текстом и превращается в HTML
// уже здесь, поэтому санитизация обязательна: иначе автор статьи смог бы
// вставить произвольный HTML и выполнить скрипт у всех читателей.
// DOMPurify чистит результат marked, оставляя безопасные теги.
import { marked } from 'marked';
import DOMPurify from 'dompurify';

marked.setOptions({ breaks: true, gfm: true });

window.renderArticleMarkdown = function (text) {
  if (!text) return '';
  return DOMPurify.sanitize(marked.parse(String(text)));
};

window.articleStatusLabel = function (s) {
  return { draft: 'Черновик', published: 'Опубликована',
           review: 'На проверке', rejected: 'Отклонена' }[s] || s;
};

window.articleDate = function (ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleDateString(
    'ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
};

const A = () => `${window.SOCIAL_API}/groups/${window.currentGroup.id}/articles`;

async function loadGroupArticles() {
  const box = document.getElementById('article-list');
  if (!box || !window.currentGroup) return;
  box.innerHTML = '<div class="loading"><i class="fas fa-spinner"></i></div>';
  try {
    const r = await window.api(A());
    const list = r.articles || [];
    // Писать статьи может любой участник, поэтому кнопка не скрывается.
    const btn = document.getElementById('gaArticleCreate');
    if (btn) btn.style.display = '';
    if (!list.length) {
      box.innerHTML = '<div class="empty-state" style="padding:24px">'
        + '<i class="fas fa-newspaper" style="font-size:28px;opacity:.4"></i>'
        + '<p>Статей пока нет</p></div>';
      return;
    }
    box.innerHTML = list.map(a => `
      <div style="display:flex;gap:12px;align-items:center;padding:10px;border:1px solid var(--border);border-radius:10px;margin-bottom:8px;cursor:pointer"
           onclick="openArticleView('${a.id}')">
        <div style="width:56px;height:56px;border-radius:8px;overflow:hidden;flex-shrink:0;background:var(--bg2);display:flex;align-items:center;justify-content:center">
          ${a.cover_image
            ? `<img src="${escapeHtml(a.cover_image)}" style="width:100%;height:100%;object-fit:cover">`
            : '<i class="fas fa-file-alt" style="opacity:.35"></i>'}
        </div>
        <div style="flex:1;min-width:0">
          <div style="font-weight:600;font-size:13px">${escapeHtml(a.title)}</div>
          <div style="font-size:11px;color:var(--text3)">${window.articleStatusLabel(a.status)}
            · ${a.views_count || 0} просм. · ${window.articleDate(a.updated_at)}</div>
        </div>
        <i class="fas fa-chevron-right" style="color:var(--text3)"></i>
      </div>`).join('');
  } catch (e) {
    box.innerHTML = `<div class="empty-state" style="padding:20px;color:var(--red)">${escapeHtml(e.message)}</div>`;
  }
}

window.loadGroupArticles = loadGroupArticles;

window.openArticleView = async function (articleId) {
  try {
    const a = await window.api(`${A()}/${articleId}`);
    const body = (a.sections || []).map(s =>
      (s.title ? `<h3 style="font-size:15px;margin:16px 0 6px">${escapeHtml(s.title)}</h3>` : '')
      + window.renderArticleMarkdown(s.content)).join('');
    window.openModal('article-view-modal');
    document.getElementById('article-view-body').innerHTML = `
      <div style="max-width:680px;margin:0 auto">
        ${a.cover_image ? `<img src="${escapeHtml(a.cover_image)}" style="width:100%;max-height:280px;object-fit:cover;border-radius:12px;margin-bottom:12px">` : ''}
        <h2 style="font-size:20px;margin:0 0 4px">${escapeHtml(a.title)}</h2>
        <div style="font-size:11px;color:var(--text3);margin-bottom:12px">
          ${window.articleStatusLabel(a.status)} · ${a.views_count || 0} просм.
          · ${window.articleDate(a.created_at)}</div>
        ${a.excerpt ? `<p style="color:var(--text2);font-style:italic">${escapeHtml(a.excerpt)}</p>` : ''}
        <div class="article-md">${body}</div>
      </div>`;
    const actions = document.getElementById('article-view-actions');
    actions.innerHTML = a.can_edit
      ? `<button class="btn btn-secondary btn-sm" onclick="openArticleEdit('${a.id}')"><i class="fas fa-pen"></i> Править</button>
         <button class="btn btn-secondary btn-sm" onclick="deleteArticle('${a.id}')"><i class="fas fa-trash"></i> Удалить</button>`
      : '';
  } catch (e) { window.toast('Не удалось открыть статью: ' + e.message, 'error'); }
};

window.openArticleCreate = function () {
  window.articleEditingId = null;
  window.articleDraft = { title: '', excerpt: '', cover_image: '',
                          status: 'draft', sections: [{ title: '', content: '' }] };
  renderArticleEditor();
  window.openModal('article-editor-modal');
};

window.openArticleEdit = async function (articleId) {
  try {
    const a = await window.api(`${A()}/${articleId}`);
    window.articleEditingId = articleId;
    window.articleDraft = {
      title: a.title, excerpt: a.excerpt || '', cover_image: a.cover_image || '',
      status: a.status, sections: a.sections.length ? a.sections : [{ title: '', content: '' }]
    };
    renderArticleEditor();
    window.openModal('article-editor-modal');
  } catch (e) { window.toast('Не удалось загрузить статью: ' + e.message, 'error'); }
};

function renderArticleEditor() {
  const d = window.articleDraft;
  document.getElementById('article-editor').innerHTML = `
    <div style="max-width:720px;margin:0 auto">
      <input id="artTitle" placeholder="Заголовок" value="${escapeHtml(d.title)}" style="width:100%;padding:10px;border:1px solid var(--border);border-radius:8px;background:var(--bg2);color:var(--text);margin-bottom:8px">
      <input id="artExcerpt" placeholder="Краткое описание (необязательно)" value="${escapeHtml(d.excerpt)}" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:8px;background:var(--bg2);color:var(--text);margin-bottom:8px">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
        <button class="btn btn-secondary btn-sm" onclick="pickArticleCover()"><i class="fas fa-image"></i> Обложка</button>
        <span style="font-size:11px;color:var(--text3)">${d.cover_image ? 'Обложка выбрана' : 'Обложка не выбрана'}</span>
        <div style="flex:1"></div>
        <select id="artStatus">
          ${['draft', 'published'].map(s =>
            `<option value="${s}"${d.status === s ? ' selected' : ''}>${window.articleStatusLabel(s)}</option>`).join('')}
        </select>
      </div>
      <div id="artSections"></div>
      <button class="btn btn-secondary btn-sm" onclick="addArticleSection()" style="margin-top:8px"><i class="fas fa-plus"></i> Секция</button>
    </div>`;
  renderArticleSections();
}

window.renderArticleEditor = renderArticleEditor;

function renderArticleSections() {
  const d = window.articleDraft;
  document.getElementById('artSections').innerHTML = d.sections.map((s, i) => `
    <div style="border:1px solid var(--border);border-radius:10px;padding:10px;margin-bottom:8px">
      <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
        <input placeholder="Заголовок секции" value="${escapeHtml(s.title)}"
               oninput="articleSectionField(${i},'title',this.value)" style="flex:1;padding:6px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text)">
        <button class="btn btn-secondary btn-sm" onclick="previewArticleSection(${i})" title="Предпросмотр разметки"><i class="fas fa-eye"></i></button>
        ${d.sections.length > 1 ? `<button class="btn btn-secondary btn-sm" onclick="removeArticleSection(${i})"><i class="fas fa-times"></i></button>` : ''}
      </div>
      <textarea placeholder="Текст в Markdown: **жирный**, *курсив*, ## подзаголовок"
                oninput="articleSectionField(${i},'content',this.value)"
                style="width:100%;min-height:140px;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);font-family:inherit;resize:vertical">${escapeHtml(s.content)}</textarea>
    </div>`).join('');
}

window.articleSectionField = function (i, field, value) {
  window.articleDraft.sections[i][field] = value;
};

// План требовал предпросмотра Markdown без редактора — он сделан
// поверх готовой очистки, поэтому в предпросмотре и при чтении статьи
// разметка выглядит одинаково.
window.previewArticleSection = function (i) {
  const html = window.renderArticleMarkdown(window.articleDraft.sections[i].content);
  window.openModal('article-view-modal');
  document.getElementById('article-view-body').innerHTML =
    `<div class="article-md" style="max-width:680px;margin:0 auto">${html}</div>`;
};

window.addArticleSection = function () {
  if (window.articleDraft.sections.length >= 50)
    return window.toast('Максимум 50 секций', 'error');
  window.articleDraft.sections.push({ title: '', content: '' });
  renderArticleSections();
};

window.removeArticleSection = function (i) {
  window.articleDraft.sections.splice(i, 1);
  renderArticleSections();
};

window.pickArticleCover = function () {
  const i = document.getElementById('article-cover-input');
  if (i) i.click();
};

window.uploadArticleCover = async function (input) {
  const f = (input.files || [])[0];
  if (!f) return;
  const fd = new FormData();
  fd.append('file', f);
  try {
    const r = await window.api(`${A()}/upload`, { method: 'POST', body: fd });
    window.articleDraft.cover_image = r.path;
    const n = document.getElementById('artCoverName');
    if (n) n.textContent = 'Обложка выбрана';
  } catch (e) { window.toast('Не удалось загрузить обложку: ' + e.message, 'error'); }
  input.value = '';
};

window.saveArticle = async function () {
  const d = window.articleDraft;
  d.title = document.getElementById('artTitle').value;
  d.excerpt = document.getElementById('artExcerpt').value;
  d.status = document.getElementById('artStatus').value;
  if (!d.title.trim()) return window.toast('Введите заголовок', 'error');
  try {
    if (window.articleEditingId) {
      await window.api(`${A()}/${window.articleEditingId}`, { method: 'PUT', body: JSON.stringify(d) });
      window.toast('Статья сохранена');
    } else {
      await window.api(A(), { method: 'POST', body: JSON.stringify(d) });
      window.toast('Статья создана');
    }
    window.articleEditingId = null;
    window.closeModal('article-editor-modal');
    loadGroupArticles();
  } catch (e) { window.toast('Не удалось сохранить: ' + e.message, 'error'); }
};

window.deleteArticle = async function (articleId) {
  if (!confirm('Удалить статью?')) return;
  try {
    await window.api(`${A()}/${articleId}`, { method: 'DELETE' });
    window.closeModal('article-view-modal');
    window.toast('Статья удалена');
    loadGroupArticles();
  } catch (e) { window.toast('Не удалось удалить: ' + e.message, 'error'); }
};