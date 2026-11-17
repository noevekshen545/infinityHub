/* Панель «Быстрые заметки» окна мини-приложения (Shift+Space).
 * Наполнение — общий QuickNotesUI из quick_notes_ui.js, здесь только
 * оболочка: шапка с «назад»/закреплением и вызов перезагрузки при открытии.
 */
(function () {
  'use strict';

  var box = null;
  var mounted = false;
  var panel = null;

  function markup() {
    return '' +
      '<div class="aibar-notes-head">' +
        '<button class="aibar-iconbtn aibar-notes-back" title="К запросу">' +
          '<i class="fas fa-arrow-left"></i></button>' +
        '<span class="aibar-notes-title"><i class="fas fa-pencil"></i> Быстрые заметки</span>' +
        '<span class="spacer"></span>' +
        '<button class="aibar-iconbtn aibar-notes-pin" title="Закрепить окно">' +
          '<i class="fas fa-thumbtack"></i></button>' +
      '</div>' +
      '<div class="aibar-notes-body"></div>';
  }

  function mount(host) {
    box = host;
    if (mounted) return box;
    box.innerHTML = markup();
    var body = box.querySelector('.aibar-notes-body');
    if (window.QuickNotesUI) {
      panel = window.QuickNotesUI.createPanel({ compact: true });
      body.appendChild(panel);
    } else {
      body.appendChild(document.createTextNode('Модуль заметок не загружен'));
    }
    mounted = true;
    return box;
  }

  function open() {
    if (panel && panel.reload) panel.reload();
    if (panel && panel.focusInput) setTimeout(panel.focusInput, 60);
  }

  function close() { /* поллингов нет — закрывать нечего */ }

  window.AiBarNotes = {
    mount: mount,
    open: open,
    close: close
  };
})();
