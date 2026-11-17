// Альбомы и статьи канала сервера.
//
// Бэкенд общий с группами (social_group_albums.py и social_group_articles.py
// работают и с группой, и с каналом через social_scope), отличается только
// префикс маршрутов и то, что областью здесь является канал, а не группа.
//
// Отдельный файл, а не дописывание в social-articles.js: там областью всегда
// группа currentGroup, здесь — activeServer плюс activeChannel.
//
// Скрипт модульный, поэтому доступа к let/const страницы у него нет: всё
// нужное берётся из window.MESSAGES_ENV, который шаблон собирает геттерами.
const { $, api, toast, esc, openModal, closeModal } = window.MESSAGES_ENV;

const base = () => `/api/social/servers/${window.MESSAGES_ENV.activeServer}`
  + `/channels/${window.MESSAGES_ENV.activeChannel}`;
const chKind = { photo: 'image/*', video: 'video/*', audio: 'audio/*' };
let channelAlbum = null;
let channelAlbumKind = 'photo';

// Хелперы страницы живут как function-объявления и потому и так попадают в
// window. Явно выставляем те, что вызываются из onclick в разметке.
Object.assign(window, {
  openChannelAlbums, loadChannelAlbums, openChannelAlbumView, createChannelAlbum,
  pickChannelAlbumFiles, uploadChannelAlbumFiles, removeChannelAlbumPhoto,
  deleteChannelAlbum, closeChannelAlbumView,
  openChannelAlbumFrame, submitChannelAlbumCreate,
  openChannelArticles, openChannelArticle, createChannelArticle,
  editChannelArticle, saveChannelArticle, deleteChannelArticle,
});

// --- Альбомы ---

async function openChannelAlbums(kind) {
  if (!window.MESSAGES_ENV || !window.MESSAGES_ENV.activeChannel) return;
  channelAlbumKind = kind || channelAlbumKind;
  openModal('channelAlbumModal');
  await loadChannelAlbums();
}

const CH_KIND_META = {
  photo: { icon: 'fa-images', label: 'Фото' },
  video: { icon: 'fa-video', label: 'Видео' },
  audio: { icon: 'fa-music', label: 'Аудио' },
};

async function loadChannelAlbums() {
  const box = $('channelAlbumList');
  if (!box) return;
  box.className = '';
  box.innerHTML = '<div class="loading"><i class="fas fa-spinner"></i></div>';
  let albums;
  try {
    const r = await api(`${base()}/albums?kind=${channelAlbumKind}`);
    albums = r.albums || [];
  } catch (e) {
    box.innerHTML = `<div class="empty-state" style="padding:20px;color:var(--red)">${esc(e.message || e)}</div>`;
    return;
  }
  document.querySelectorAll('#channelAlbumKinds button').forEach(b =>
    b.classList.toggle('active', b.dataset.kind === channelAlbumKind));
  if (!albums.length) {
    box.innerHTML = '<div class="empty-state" style="padding:20px">Альбомов пока нет</div>';
    return;
  }
  /* Та же сетка карточек, что и в группах: раньше здесь был третий вариант
     разметки списка альбомов, собранный инлайном. */
  box.className = 'albums-grid';
  box.innerHTML = albums.map(a => {
    const km = CH_KIND_META[a.kind] || CH_KIND_META.photo;
    return `
      <div class="album-card--v2" onclick="openChannelAlbumView('${a.id}')" title="${esc(a.title)}">
        <div class="album-cover">
          ${a.cover_path
            ? `<img src="${esc(a.cover_path)}" alt="" loading="lazy">`
            : `<div class="album-cover-empty"><i class="fas ${km.icon}"></i></div>`}
          <span class="album-kind"><i class="fas ${km.icon}"></i>${km.label}</span>
        </div>
        <div class="album-info">
          <div class="album-title">${esc(a.title)}</div>
          <div class="album-sub"><span><i class="fas fa-images"></i> ${a.photo_count || 0}</span></div>
        </div>
      </div>`;
  }).join('');
}

async function openChannelAlbumView(albumId) {
  try {
    if (window.SocialAlbumPlayer) SocialAlbumPlayer.stop();
    const a = await api(`${base()}/albums/${albumId}`);
    channelAlbum = a;
    $('channelAlbumTitle').textContent = a.title;
    const photos = a.photos || [];
    const accept = chKind[channelAlbumKind] || 'image/*';
    const tiles = window.SocialAlbumPlayer
      ? SocialAlbumPlayer.buildTiles(photos, { canEdit: a.can_edit, remove: 'removeChannelAlbumPhoto', onOpen: 'openChannelAlbumFrame' })
      : '';
    const box = $('channelAlbumBody');
    box.innerHTML = `
      <div style="display:flex;gap:8px;margin-bottom:10px;align-items:center">
        <div style="flex:1;font-size:11px;color:var(--text3)">${photos.length} шт.</div>
        ${a.can_edit ? `<button class="btn btn-secondary btn-sm" onclick="pickChannelAlbumFiles()"><i class="fas fa-plus"></i> Добавить</button>` : ''}
        ${a.can_edit ? `<button class="btn btn-secondary btn-sm" onclick="deleteChannelAlbum('${a.id}')" title="Удалить альбом"><i class="fas fa-trash"></i></button>` : ''}
      </div>
      <input type="file" id="channelAlbumFile" accept="${accept}" multiple style="display:none" onchange="uploadChannelAlbumFiles(this)">
      ${photos.length
        ? `<div class="album-photos">` + tiles + '</div>'
        : '<div class="empty-state" style="padding:20px"><i class="fas fa-images" style="opacity:.4"></i><p>Альбом пуст</p></div>'}`;
    if (window.SocialAlbumPlayer) SocialAlbumPlayer.mount(box, photos);
    openModal('channelAlbumViewModal');
  } catch (e) { toast('Не удалось открыть альбом: ' + (e.message || e), 'error'); }
}

/* Открытие кадра альбома канала в просмотрщике с панелью комментариев. */
function openChannelAlbumFrame(photoId, index) {
  if (!channelAlbum) return;
  SocialAlbumView.open(channelAlbum, {
    contextLabel: 'Альбом канала «' + (channelAlbum.title || '') + '»',
    onEditPhoto: pid => { if (window.SocialPhotoEditor) SocialPhotoEditor.open(pid, openChannelAlbumFrame); },
    onDeletePhoto: async pid => { await removeChannelAlbumPhoto(pid); },
    onShare: item => { if (window.SocialShare) SocialShare.openForPhoto(item, null); },
  }, index);
}

function createChannelAlbum() {
  const t = $('caTitle'); if (t) t.value = '';
  const k = $('caKind'); if (k) k.value = channelAlbumKind || 'photo';
  const p = $('caPrivacy'); if (p) p.value = '0';
  openModal('channelAlbumCreateModal');
}
function submitChannelAlbumCreate() {
  const title = ($('caTitle').value || '').trim();
  if (!title) { toast('Введите название альбома', 'error'); return; }
  api(`${base()}/albums`, {
    method: 'POST',
    body: JSON.stringify({
      title,
      kind: $('caKind').value,
      privacy: parseInt($('caPrivacy').value, 10)
    })
  }).then(() => {
    closeModal('channelAlbumCreateModal');
    toast('Альбом создан');
    loadChannelAlbums();
  }).catch(e => toast('Не удалось создать альбом: ' + (e.message || e), 'error'));
}

function pickChannelAlbumFiles() {
  const i = $('channelAlbumFile');
  if (i) i.click();
}

async function uploadChannelAlbumFiles(input) {
  const files = Array.from(input.files || []);
  if (!files.length || !channelAlbum) return;
  let done = 0;
  for (const f of files) {
    const fd = new FormData();
    fd.append('file', f);
    try {
      await api(`${base()}/albums/${channelAlbum.id}/photos/upload`, { method: 'POST', body: fd });
      done++;
    } catch (e) { toast('Не удалось загрузить ' + f.name + ': ' + (e.message || e), 'error'); }
  }
  input.value = '';
  if (done) toast('Загружено: ' + done);
  await openChannelAlbumView(channelAlbum.id);
  await loadChannelAlbums();
}

async function removeChannelAlbumPhoto(photoId) {
  try {
    await api(`${base()}/albums/${channelAlbum.id}/photos/${photoId}`, { method: 'DELETE' });
    await openChannelAlbumView(channelAlbum.id);
    await loadChannelAlbums();
  } catch (e) { toast('Не удалось убрать: ' + (e.message || e), 'error'); }
}

function closeChannelAlbumView() {
  if (window.SocialAlbumPlayer) SocialAlbumPlayer.stop();
  closeModal('channelAlbumViewModal');
}

async function deleteChannelAlbum(albumId) {
  if (!confirm('Удалить альбом?')) return;
  try {
    await api(`${base()}/albums/${albumId}`, { method: 'DELETE' });
    if (window.SocialAlbumPlayer) SocialAlbumPlayer.stop();
    closeModal('channelAlbumViewModal');
    toast('Альбом удалён');
    await loadChannelAlbums();
  } catch (e) { toast('Не удалось удалить: ' + (e.message || e), 'error'); }
}

// --- Статьи ---
//
// Разметка рендерится и очищается так же, как в social-articles.js:
// сервер отдаёт Markdown обычным текстом, HTML строит браузер.

async function openChannelArticles() {
  if (!window.MESSAGES_ENV || !window.MESSAGES_ENV.activeChannel) return;
  openModal('channelArticleModal');
  const box = $('channelArticleList');
  box.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text3)"><i class="fas fa-spinner fa-spin"></i></div>';
  try {
    const r = await api(`${base()}/articles`);
    const list = r.articles || [];
    if (!list.length) {
      box.innerHTML = '<div class="empty-state" style="padding:20px">Статей пока нет</div>';
      return;
    }
    box.innerHTML = list.map(a => `
      <div style="display:flex;gap:12px;align-items:center;padding:10px;border:1px solid var(--border);border-radius:10px;margin-bottom:8px;cursor:pointer"
           onclick="openChannelArticle('${a.id}')">
        <div style="width:52px;height:52px;border-radius:8px;overflow:hidden;flex-shrink:0;background:var(--bg2);display:flex;align-items:center;justify-content:center">
          ${a.cover_image ? `<img src="${esc(a.cover_image)}" style="width:100%;height:100%;object-fit:cover">`
                          : '<i class="fas fa-file-alt" style="opacity:.35"></i>'}
        </div>
        <div style="flex:1;min-width:0">
          <div style="font-weight:600;font-size:13px">${esc(a.title)}</div>
          <div style="font-size:11px;color:var(--text3)">${a.views_count || 0} просм.${a.status === 'draft' ? ' · черновик' : ''}</div>
        </div>
        <i class="fas fa-chevron-right" style="color:var(--text3)"></i>
      </div>`).join('');
  } catch (e) {
    box.innerHTML = `<div style="padding:20px;color:var(--red);font-size:12px">${esc(e.message || e)}</div>`;
  }
}

async function openChannelArticle(articleId) {
  try {
    const a = await api(`${base()}/articles/${articleId}`);
    const body = (a.sections || []).map(s =>
      (s.title ? `<h3 style="font-size:15px;margin:16px 0 6px">${esc(s.title)}</h3>` : '')
      + renderArticleMarkdown(s.content)).join('');
    $('channelArticleBody').innerHTML = `
      <div style="max-width:640px;margin:0 auto">
        ${a.cover_image ? `<img src="${esc(a.cover_image)}" style="width:100%;max-height:220px;object-fit:cover;border-radius:12px;margin-bottom:12px">` : ''}
        <h2 style="font-size:19px;margin:0 0 4px">${esc(a.title)}</h2>
        <div style="font-size:11px;color:var(--text3);margin-bottom:12px">${a.views_count || 0} просм.</div>
        ${a.excerpt ? `<p style="color:var(--text2);font-style:italic">${esc(a.excerpt)}</p>` : ''}
        <div class="article-md">${body}</div>
      </div>`;
    $('channelArticleActions').innerHTML = a.can_edit
      ? `<button class="btn btn-secondary btn-sm" onclick="editChannelArticle()"><i class="fas fa-pen"></i> Править</button>
         <button class="btn btn-secondary btn-sm" onclick="deleteChannelArticle('${a.id}')"><i class="fas fa-trash"></i> Удалить</button>`
      : '';
    channelEditing = a;
    channelEditingId = a.id;
    openModal('channelArticleViewModal');
  } catch (e) { toast('Не удалось открыть статью: ' + (e.message || e), 'error'); }
}

function editChannelArticle() {
  if (!channelEditing) return;
  $('channelArticleEditor').innerHTML = channelArticleEditorHtml(channelEditing);
  closeModal('channelArticleViewModal');
  openModal('channelArticleEditorModal');
}

async function createChannelArticle() {
  const title = await InfinityDialogs.prompt({label:'Заголовок статьи:'});
  if (!title || !title.trim()) return;
  api(`${base()}/articles`, {
    method: 'POST',
    body: JSON.stringify({
      title: title.trim(), status: 'published',
      sections: [{ title: '', content: '' }]
    })
  }).then(r => {
    toast('Статья создана');
    return api(`${base()}/articles/${r.article_id}`).then(a => {
      // Сразу открываем редактор с только что созданной статьёй: дописывать
      // текст удобнее, чем возвращаться в список и искать её заново.
      closeModal('channelArticleModal');
      openModal('channelArticleEditorModal');
      $('channelArticleEditor').innerHTML = channelArticleEditorHtml(a);
      channelEditingId = a.id;
      channelEditing = a;
    });
  }).catch(e => toast('Не удалось создать статью: ' + (e.message || e), 'error'));
}

let channelEditing = null;
let channelEditingId = null;

function channelArticleEditorHtml(a) {
  const sections = (a.sections && a.sections.length) ? a.sections : [{ title: '', content: '' }];
  return `
    <div style="max-width:680px;margin:0 auto">
      <input id="chArtTitle" placeholder="Заголовок" value="${esc(a.title)}" style="width:100%;padding:10px;border:1px solid var(--border);border-radius:8px;background:var(--bg2);color:var(--text);margin-bottom:8px">
      <textarea id="chArtContent" placeholder="Текст в Markdown: **жирный**, ## подзаголовок"
        style="width:100%;min-height:220px;padding:8px;border:1px solid var(--border);border-radius:8px;background:var(--bg2);color:var(--text);font-family:inherit;resize:vertical">${esc((sections[0] && sections[0].content) || '')}</textarea>
      <div style="font-size:11px;color:var(--text3);margin-top:6px">
        Редактор правит первую секцию; остальные секции статьи сохранятся без изменений.
      </div>
    </div>`;
}

async function saveChannelArticle() {
  const title = $('chArtTitle').value;
  const content = $('chArtContent').value;
  if (!title.trim()) return toast('Введите заголовок', 'error');
  // Редактор правит только первую секцию, остальные переносятся как есть:
  // иначе сохранение затёрло бы их содержимое.
  const rest = (channelEditing?.sections || []).slice(1);
  const body = JSON.stringify({
    title: title.trim(),
    sections: [{ title: channelEditing?.sections?.[0]?.title || '', content: content }]
      .concat(rest)
  });
  try {
    await api(`${base()}/articles/${channelEditingId}`, { method: 'PUT', body });
    toast('Статья сохранена');
    closeModal('channelArticleEditorModal');
    channelEditingId = null;
    await openChannelArticles();
  } catch (e) { toast('Не удалось сохранить: ' + (e.message || e), 'error'); }
}

async function deleteChannelArticle(articleId) {
  if (!confirm('Удалить статью?')) return;
  try {
    await api(`${base()}/articles/${articleId}`, { method: 'DELETE' });
    closeModal('channelArticleViewModal');
    toast('Статья удалена');
    await openChannelArticles();
  } catch (e) { toast('Не удалось удалить: ' + (e.message || e), 'error'); }
}