/* Кликабельные строки в палитрах горячих клавиш.

   Палитра раньше была только справкой: на телефоне сочетание нажать нечем,
   хотя сама функция доступна. Теперь строка — кнопка, которая нажимает то
   же сочетание, что написано справа, поэтому поведение совпадает с
   клавиатурой один в один (ничего не дублируем и не расходимся).

   Разметка строки:
     data-key="ArrowRight"   — какую клавишу нажать
     data-code="ArrowRight"  — код клавиши (для клавиш без символа)
     data-ctrl="1" data-shift="1" data-alt="1" — модификаторы
     data-call="openSearchDialog()" — прямой вызов функции вместо клавиши
     data-close="#shortcutsModal"   — что закрыть перед действием
*/
(function () {
  'use strict';

  function closeTarget(sel) {
    if (!sel) return;
    var el;
    if (sel.charAt(0) === '#' || sel.charAt(0) === '.') {
      el = document.querySelector(sel);
    } else if (typeof window[sel] === 'function') {
      try { window[sel](); } catch (e) { /* палитра уже закрыта */ }
      return;
    }
    if (!el) return;
    el.classList.remove('active', 'open', 'show');
    el.setAttribute('aria-hidden', 'true');
  }

  window.runShortcutButton = function (btn) {
    if (!btn) return;
    closeTarget(btn.getAttribute('data-close'));

    var call = btn.getAttribute('data-call');
    if (call) {
      try { (new Function(call))(); } catch (e) { console.warn('shortcut call:', e); }
    }

    var key = btn.getAttribute('data-key');
    if (key === null || key === '') return;
    var ev = new KeyboardEvent('keydown', {
      key: key,
      code: btn.getAttribute('data-code') || '',
      ctrlKey: btn.getAttribute('data-ctrl') === '1',
      shiftKey: btn.getAttribute('data-shift') === '1',
      altKey: btn.getAttribute('data-alt') === '1',
      bubbles: true,
      cancelable: true
    });
    (document.body || document).dispatchEvent(ev);
  };
})();
