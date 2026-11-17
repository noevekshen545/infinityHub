import server_backup as _original
import swallow

NOTES_DIR = _original.NOTES_DIR

chat_move_to_notes = _original.chat_move_to_notes
notes_auto_links = _original.notes_auto_links
notes_backlinks = _original.notes_backlinks
notes_daily = _original.notes_daily
notes_empty_trash = _original.notes_empty_trash
notes_from_template = _original.notes_from_template
notes_outline = _original.notes_outline
notes_properties = _original.notes_properties
notes_restore = _original.notes_restore
notes_search_all = _original.notes_search_all
notes_set_property = _original.notes_set_property
notes_templates_delete = _original.notes_templates_delete
notes_templates_list = _original.notes_templates_list
notes_templates_save = _original.notes_templates_save
notes_tags_rename = _original.notes_tags_rename
notes_trash = _original.notes_trash
notes_unresolved_links = _original.notes_unresolved_links
notes_list_view = _original.notes_list_view
notes_patch = _original.notes_patch
notes_comment_delete = _original.notes_comment_delete
notes_comment_list = _original.notes_comment_list
notes_comment_save = _original.notes_comment_save
notes_comment_update = _original.notes_comment_update
notes_create_folder = _original.notes_create_folder
notes_delete_article = _original.notes_delete_article
notes_delete_attachment = _original.notes_delete_attachment
notes_delete_folder = _original.notes_delete_folder
notes_export_all = _original.notes_export_all
notes_export_note = _original.notes_export_note
notes_fulltext_search = _original.notes_fulltext_search
notes_get_article = _original.notes_get_article
notes_get_attachment_text = _original.notes_get_attachment_text
notes_get_templates = _original.notes_get_templates
notes_graph_data = _original.notes_graph_data
notes_import_note = _original.notes_import_note
notes_list_all_attachments = _original.notes_list_all_attachments
notes_list_all_tags = _original.notes_list_all_tags
notes_list_articles = _original.notes_list_articles
notes_list_attachments = _original.notes_list_attachments
notes_list_by_tag = _original.notes_list_by_tag
notes_list_folders = _original.notes_list_folders
notes_move_article = _original.notes_move_article
notes_move_folder = _original.notes_move_folder
notes_rename_folder = _original.notes_rename_folder
notes_save_article = _original.notes_save_article
notes_search = _original.notes_search
notes_serve_attachment = _original.notes_serve_attachment
notes_tree_create = _original.notes_tree_create
notes_tree_delete = _original.notes_tree_delete
notes_tree_get = _original.notes_tree_get
notes_tree_move = _original.notes_tree_move
notes_tree_rename = _original.notes_tree_rename
notes_upload_attachment = _original.notes_upload_attachment
workspace_get_attachments = _original.workspace_get_attachments
workspace_get_folders_with_notes = _original.workspace_get_folders_with_notes
workspace_get_notes = _original.workspace_get_notes
workspace_get_state = _original.workspace_get_state
notes_version_delete = _original.notes_version_delete
notes_version_get = _original.notes_version_get
notes_version_list = _original.notes_version_list
notes_version_restore = _original.notes_version_restore
notes_version_save = _original.notes_version_save

plugins_list = _original.plugins_list

# --- GET route handlers ---

def _wants_notion(self):
    """Notion-слой заметок отключён.

    Раньше здесь был выбор по cookie notes_template и ?tpl=nt, и кнопка
    в настройках. Пользователь просил убрать Notion-стиль совсем: он не
    использовался, а выбор в настройках только занимал место и путал
    (страница и её правки разъезжались).

    Функция оставлена как заглушка, потому что её зовут два обработчика.
    Сами файлы page_notes_nt.html / page_notes_edit_nt.html на месте —
    если решение изменится, достаточно вернуть выбор здесь и в настройках.

    ?tpl=nt больше не переключает шаблон намеренно: иначе старая ссылка
    или сохранённая закладка снова приводила бы на неиспользуемую страницу.
    """
    return False

def _handle_notes(self, p, q):
    if _wants_notion(self):
        tmpl = getattr(_original, "PAGE_NOTES_NT", "") or ""
        if tmpl:
            self._html(tmpl)
            return
    self._html(_original.PAGE_NOTES)

def _handle_workspace(self, p, q):
    self._html(_original.PAGE_WORKSPACE)

def _handle_notes_edit(self, p, q):
    if _wants_notion(self):
        tmpl = getattr(_original, "PAGE_NOTES_EDIT_NT", "") or ""
        if tmpl:
            self._html(tmpl)
            return
    self._html(_original.PAGE_NOTES_EDIT)

def _handle_notes_graph(self, p, q):
    self._html(_original.PAGE_NOTES_GRAPH)

def _handle_notes_folders(self, p, q):
    self._json(notes_list_folders())

def _handle_notes_list(self, p, q):
    folder = q.get("folder", [""])[0]
    sort = q.get("sort", ["date"])[0]
    self._json(notes_list_articles(folder, sort))

def _handle_notes_get(self, p, q):
    nid = q.get("id", [""])[0]
    self._json(notes_get_article(nid))

def _handle_notes_search(self, p, q):
    query = q.get("q", [""])[0]
    self._json(notes_search(query))

def _handle_notes_attachments(self, p, q):
    nid = q.get("note", [""])[0]
    self._json(notes_list_attachments(nid))

def _handle_notes_attachments_all(self, p, q):
    self._json(notes_list_all_attachments())

def _handle_notes_plugins(self, p, q):
    self._json(plugins_list())

def _handle_notes_graph_data(self, p, q):
    self._json(notes_graph_data())

def _handle_notes_tags(self, p, q):
    self._json(notes_list_all_tags())

def _handle_notes_by_tag(self, p, q):
    tag = q.get("tag", [""])[0]
    self._json(notes_list_by_tag(tag))

def _handle_notes_tree(self, p, q):
    self._json(notes_tree_get())

def _handle_workspace_state(self, p, q):
    folder = q.get("folder", [""])[0]
    self._json(workspace_get_state(folder))

def _handle_workspace_notes(self, p, q):
    folder = q.get("folder", [""])[0]
    self._json(workspace_get_notes(folder))

def _handle_workspace_attachments(self, p, q):
    folder = q.get("folder", [""])[0]
    self._json(workspace_get_attachments(folder))

def _handle_workspace_folders(self, p, q):
    self._json(workspace_get_folders_with_notes())

def _handle_version_list(self, p, q):
    nid = q.get("id", [""])[0]
    self._json(notes_version_list(nid))

def _handle_version_get(self, p, q):
    nid = q.get("id", [""])[0]
    vn = int(q.get("v", ["0"])[0])
    self._json(notes_version_get(nid, vn))

def _handle_comment_list(self, p, q):
    nid = q.get("note", [""])[0]
    self._json(notes_comment_list(nid))

def _handle_notes_backlinks(self, p, q):
    self._json(notes_backlinks(q.get("id", [""])[0]))

def _handle_notes_unresolved(self, p, q):
    self._json(notes_unresolved_links())

def _handle_notes_outline(self, p, q):
    self._json(notes_outline(q.get("id", [""])[0]))

def _handle_notes_properties(self, p, q):
    self._json(notes_properties(q.get("id", [""])[0]))

def _handle_notes_view(self, p, q):
    self._json(notes_list_view(
        folder=q.get("folder", [""])[0],
        sort=q.get("sort", ["date"])[0],
        view=q.get("view", ["active"])[0],
        tag=q.get("tag", [""])[0],
        query=q.get("q", [""])[0],
    ))

def _handle_notes_search_all(self, p, q):
    self._json(notes_search_all(q.get("q", [""])[0]))

def _handle_notes_templates(self, p, q):
    self._json(notes_templates_list())

def _handle_notes_daily(self, p, q):
    self._json(notes_daily(q.get("date", [""])[0] or None,
                           q.get("folder", ["inbox"])[0]))

def _handle_excalidraw(self, p, q):
    import os
    excalidraw_path = os.path.join(os.path.dirname(os.path.abspath(_original.__file__)), "static", "excalidraw", "index.html")
    if os.path.exists(excalidraw_path):
        with open(excalidraw_path, encoding="utf-8") as f:
            content = f.read()
            content = content.replace('src="/assets/', 'src="/static/excalidraw/assets/')
            content = content.replace('href="/assets/', 'href="/static/excalidraw/assets/')
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
    else:
        self._html("<h1>Excalidraw не найден</h1><p>Запустите: cd excalidraw-app && npm run build</p>")

_CUSTOM_TPL = {}  # путь -> (mtime, размер, html)

def _custom_page_html():
    """HTML Custom-страницы: читаем шаблон с диска (кэш по mtime/размеру).

    templates_static загружает page_*.html один раз при импорте, поэтому
    любая правка page_notes_custom.html до перезапуска сервера не попадала
    в ответ — браузер получал устаревшую копию и падал на скриптах,
    которых в новой версии уже нет. Если файл недоступен — отдаём
    константу из server_backup, как раньше.
    """
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(_original.__file__)),
                        "templates", "page_notes_custom.html")
    try:
        st = os.stat(path)
        hit = _CUSTOM_TPL.get(path)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        _CUSTOM_TPL[path] = (st.st_mtime, st.st_size, html)
        return html
    except Exception:
        swallow.note('notes._custom_page_html:L248')
        return getattr(_original, "PAGE_NOTES_CUSTOM", "")

def _handle_notes_custom(self, p, q):
    """Режим Custom — минималистичная страница одного поля ввода.

    Отдельный шаблон, а не модальное окно в редакторе: режим должен
    выглядеть как отдельный рабочий стол (только текст, две кнопки
    слева вверху), а не как ещё один блок среди панелей. Настройки режима
    хранятся в самой заметке — в полях custom_mode / custom_prompt, то
    есть в той же YAML-шапке, что и остальные свойства.
    """
    self._html(_custom_page_html())
