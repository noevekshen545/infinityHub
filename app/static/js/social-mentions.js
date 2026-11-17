/* Подсказки упоминаний @ для полей ввода соцсети.
 *
 * Раньше упоминаний в комментариях и ответах форума не было вовсе: можно
 * было вписать @вручную, но сервер находил пользователя только по точному
 * совпадению имени, и подсказок не существовало. Теперь при вводе «@»
 * под курсором появляется список — сначала участники контекста (группы),
 * затем остальные пользователи.
 *
 * Подключение (один раз на страницу):
 *   SocialMentions.attach(textarea, { groupId: () => currentGroup.id })
 *
 * На элемент навешиваются события input/keydown/blur и класс .has-mention,
 * вставка идёт по позиции курсора, поэтому работает и в середине текста.
 */
window.SocialMentions = (function () {
  'use strict';

  const API = '/api/social';
  const DEBOUNCE = 180;

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  let pop = null;          // общий попап: один на страницу достаточно
  let state = null;        // { ta, opts, items, active, from, to, timer }

  function ensurePop() {
    if (pop) return pop;
    pop = document.createElement('div');
    pop.className = 'mn-pop';
    pop.hidden = true;
    document.body.appendChild(pop);
    /* Клик по попапу не должен закрывать его: mousedown на textarea
       успевает снять фокус раньше, чем сработает click по пункту. */
    pop.addEventListener('mousedown', e => e.preventDefault());
    pop.addEventListener('click', e => {
      const b = e.target.closest('.mn-item');
      if (!b || !state) return;
      choose(state.items[parseInt(b.getAttribute('data-i'), 10) || 0]);
    });
    document.addEventListener('mousedown', e => {
      if (!pop || pop.hidden) return;
      if (state && e.target === state.ta) return;
      hide();
    });
    return pop;
  }

  /* Текст перед курсором ищем на @, за которым идёт начало имени.
     Имя заканчивается на пробеле, запятой, точке с запятой и т.п. —
     те же границы, что у серверной регулярки упоминаний. */
  function currentQuery(ta) {
    const upto = ta.value.slice(0, ta.selectionStart || 0);
    const m = upto.match(/(^|\s)@([^\s@,.:;!?()\[\]{}"']{0,40})$/);
    if (!m) return null;
    const at = upto.length - m[2].length - 1;
    return { from: at, to: ta.selectionStart || 0, q: m[2] };
  }

  function position(popEl, ta) {
    /* getBoundingClientRect у textarea не даёт позицию каретки, поэтому
       прямоугольник считаем приблизительно: строки одинаковой высоты,
       моноширинный расчёт даёт достаточную точность для попапа. */
    const rect = ta.getBoundingClientRect();
    const cs = getComputedStyle(ta);
    const lineH = parseFloat(cs.lineHeight) || 18;
    const padTop = parseFloat(cs.paddingTop) || 0;
    const upto = ta.value.slice(0, ta.selectionStart || 0);
    const lines = upto.split('\n');
    const line = lines.length - 1;
    const col = lines[lines.length - 1].length;
    const x = rect.left + padTop + col * 7.2;
    const y = rect.top + padTop + (line + 1) * lineH + 4;
    popEl.style.left = Math.min(x, window.innerWidth - 280) + 'px';
    popEl.style.top = Math.max(8, y) + 'px';
  }

  async function fetchUsers(q, opts) {
    const params = new URLSearchParams();
    params.set('q', q || '');
    if (opts && opts.groupId) {
      const g = typeof opts.groupId === 'function' ? opts.groupId() : opts.groupId;
      if (g) params.set('group_id', g);
    }
    try {
      const res = await fetch(`${API}/mention/suggest?${params}`, { credentials: 'same-origin' });
      if (!res.ok) return [];
      const data = await res.json();
      return data.users || [];
    } catch (e) {
      return [];
    }
  }

  function render(items) {
    if (!items.length) { hide(); return; }
    const p = ensurePop();
    p.innerHTML = items.map((u, i) => `
      <button class="mn-item${i === (state ? state.active : 0) ? ' is-active' : ''}" data-i="${i}" type="button">
        <span class="mn-ava">${u.avatar_path ? `<img src="${esc(u.avatar_path)}" alt="" loading="lazy">` : esc((u.display_name || '?')[0].toUpperCase())}</span>
        <span class="mn-name">${esc(u.display_name || u.profile_id)}</span>
      </button>`).join('');
    p.hidden = false;
    position(p, state.ta);
  }

  function hide() {
    if (pop) pop.hidden = true;
    if (state) state.items = [];
  }

  function choose(user) {
    if (!user || !state) return;
    const ta = state.ta;
    const before = ta.value.slice(0, state.from);
    const after = ta.value.slice(state.to);
    const name = user.display_name || user.profile_id;
    const ins = '@' + name + ' ';
    ta.value = before + ins + after;
    const caret = (state.from + ins.length);
    ta.focus();
    ta.setSelectionRange(caret, caret);
    /* Событие нужно вызвать вручную: присваивание value не меняет его,
       поэтому счётчик символов и авто-раст текста не обновились бы. */
    try { ta.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}
    hide();
  }

  function move(delta) {
    if (!state || !state.items.length) return;
    state.active = (state.active + delta + state.items.length) % state.items.length;
    render(state.items);
  }

  function attach(ta, opts) {
    if (!ta || ta.__mentionsAttached) return;
    ta.__mentionsAttached = true;
    opts = opts || {};
    ta.classList.add('has-mention');

    ta.addEventListener('input', () => {
      const hit = currentQuery(ta);
      clearTimeout(ta.__mnTimer);
      if (!hit) {
        if (state && state.ta === ta) hide();
        return;
      }
      state = { ta, opts, items: [], active: 0, from: hit.from, to: hit.to };
      /* Запрос откладывается: без этого на каждый символ уходил бы
         отдельный запрос к серверу. */
      ta.__mnTimer = setTimeout(async () => {
        const items = await fetchUsers(hit.q, opts);
        if (!state || state.ta !== ta) return;      // пользователь ушёл
        state.items = items;
        state.active = 0;
        render(items);
      }, DEBOUNCE);
    });

    ta.addEventListener('keydown', e => {
      const open = pop && !pop.hidden && state && state.ta === ta && state.items.length;
      if (open) {
        if (e.key === 'ArrowDown') { e.preventDefault(); move(1); return; }
        if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); return; }
        if (e.key === 'Enter' || e.key === 'Tab') {
          e.preventDefault();
          choose(state.items[state.active]);
          return;
        }
        if (e.key === 'Escape') { e.preventDefault(); hide(); return; }
      }
      /* @ — одиночная клавиша открывает список всех, как в Telegram. */
      if (e.key === '@' && !currentQuery(ta)) {
        const hit = currentQuery(ta) || { from: (ta.selectionStart || 0) - 1, to: ta.selectionStart || 0, q: '' };
        state = { ta, opts, items: [], active: 0, from: hit.from, to: hit.to };
        setTimeout(async () => {
          const items = await fetchUsers('', opts);
          if (!state || state.ta !== ta) return;
          state.items = items;
          state.active = 0;
          render(items);
        }, DEBOUNCE);
      }
    });

    ta.addEventListener('blur', () => setTimeout(hide, 160));
  }

  return { attach, hide, esc };
})();
