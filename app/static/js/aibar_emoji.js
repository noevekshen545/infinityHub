/* Эмодзи в окне палитры.
 *
 * Кнопка смайлика появляется рядом с микрофоном и в поле сообщений
 * мессенджера. Сетка открывается у того поля, которое сейчас в фокусе,
 * и вставляет символ прямо в текст.
 */
(function () {
  'use strict';

  /* Наборы подобраны так, чтобы все знаки были в системном шрифте
     эмодзи: новинки Unicode 13–15 и составные (ZWJ) на части машин
     рисуются квадратами. */
  var SETS = {
    'смайлы': ('😀 😃 😄 😁 😆 😅 🤣 😂 🙂 🙃 😉 😊 😇 🥰 😍 🤩 😘 😗 😚 ' +
      '😙 😋 😛 😜 🤪 😝 🤑 🤗 🤭 🤔 🤐 🤨 😐 😑 😶 😏 😒 🙄 😬 ' +
      '🤥 😌 😔 😪 🤤 😴 😷 🤒 🤕 🤢 🤮 🤧 🥵 🥶 🥴 😵 🤯 🤠 🥳 🥸 ' +
      '😎 🤓 🧐 😕 😟 🙁 ☹️ 😮 😯 😲 😳 🥺 😦 😧 😨 😰 😥 😢 😭 😱 😖 😣 ' +
      '😞 😓 😩 😫 🥱 😤 😡 😠 🤬 😈 👿 💀 💩 🤡 👹 👺 👻 👽 🤖 😺 😸 😹 😻 😼 😽 🙀 😿 😾'),
    'люди': ('👋 🤚 🖐 ✋ 🖖 👌 🤌 🤏 ✌️ 🤞 🤟 🤘 🤙 👈 👉 👆 👎 👍 ' +
      '✊ 👊 🤛 🤜 👏 🙌 👐 🤲 🤝 🙏 ✍️ 💅 🤳 💪 🦾 🦿 🦵 🦶 👂 👃 🧠 👀 👁 ' +
      '👅 👄 💋 🧑 👶 👦 👧 👨 👩 👴 🧓 👵 🙍 🙎 🙅 🙆 💁 🙋 🧏 🙇 🤦 🤷'),
    'звери': ('🐶 🐱 🐭 🐹 🐰 🦊 🐻 🐼 🐨 🐯 🦁 🐮 🐷 🐸 🐵 🙈 🙉 🙊 🐔 ' +
      '🐧 🐦 🐤 🦆 🦅 🦉 🦇 🐺 🐗 🐴 🦄 🐝 🐛 🦋 🐌 🐞 🐜 🦂 🐢 🐍 🦎 🦖 ' +
      '🐅 🐆 🦓 🦍 🐘 🦛 🐪 🐫 🦒 🦘 🐃 🐄 🐎 🐏 🐑 🐐 🦌 🐕 🐩 🦮 🐈 🐓 🦃 ' +
      '🦚 🦜 🦢 🕊 🐇 🦝 🦨 🦡 🦫 🦥 🐁 🐀 🐿 🦔'),
    'еда': ('🍏 🍎 🍐 🍊 🍋 🍌 🍉 🍇 🍓 🫐 🍈 🍒 🍑 🥭 🍍 🥥 🥝 🍅 🍆 🥑 ' +
      '🥦 🥬 🥒 🌶 🫑 🌽 🥕 🧄 🧅 🥔 🍠 🥐 🥯 🍞 🥖 🧀 🍳 🧈 🥞 🥓 🥩 🍗 ' +
      '🍖 🌭 🍔 🍟 🍕 🥪 🌮 🌯 🥙 🧆 🥘 🍝 🍜 🍲 🍛 🍣 🍱 🥟 🦪 🍤 🍙 🍚 ' +
      '🍘 🍥 🥠 🥮 🍢 🍡 🍧 🍨 🍦 🥧 🧁 🍰 🎂 🍮 🍭 🍬 🍫 🍿 🍩 🍪 🌰 🥜 ' +
      '🍯 🥛 🍼 ☕ 🍵 🧃 🥤 🧋 🍶 🍺 🍻 🥂 🍷 🥃 🍸 🍹 🍾'),
    'активы': ('⚽ 🏀 🏈 ⚾ 🥎 🎾 🏐 🏉 🥏 🎱 🏓 🏸 🏒 🏑 🥍 🏏 ⛳ 🏹 🎣 ' +
      '🤿 🥊 🥋 🎽 🛹 🛼 🛷 ⛸ 🥌 🎿 🏂 ⛹ ⛼ 🏋️ 🚴 🧗 🏊 🚣 🏄 ' +
      '🚵 🏆 🥇 🥈 🥉 🎯 🎲 🎮 🎰 🎨 🎬 🎤 🎧 🎼 🎹 🥁'),
    'предметы': ('💡 🔦 🕯 🪔 💰 💳 💎 ⚖️ 🧰 🔧 🔨 ⚒ 🛠 ⛏ 🧱 ⛓ 🧲 🔫 💣 ' +
      '🧨 🔪 🗡 ⚔️ 🛡 ⚰️ 🏺 🔮 📿 🧿 💈 ⚗️ 🔭 🔬 🕳 💊 💉 🩸 🧬 🦠 🧫 🧪 🌡 ' +
      '🧹 🧺 🧻 🚽 🚰 🚿 🛁 🧼 🪥 🧽 🧴 🛎 🔑 🗝 🚪 🪑 🛋 🛏 🖼 🪟 🛍 🛒 ' +
      '🎁 🎈 🎏 🎀 🪄 🪅 🎊 🎉 🎎 🏮 🎐 🧧 ✉️ 📩 📨 📧 💌 📥 📤 📦 🏷 📪 📫 📬 ' +
      '📭 📮 📜 📃 📄 📑 📊 📈 📉 🗒 🗓 📆 📅 🗑 📇 🗃 🗳 🗄 📋 📁 📂 🗞 📰 ' +
      '📓 📔 📒 📕 📗 📘 📙 📚 📖 🔖 🧷 🔗 📎 🖇 📐 📏 🧮 📌 📍 ✂️ 🖊 🖋 ✒️ ' +
      '🖌 🖍 📝 ✏️ 🔍 🔎 🔏 🔐 🔒 🔓'),
    'символы': ('❤️ 🧡 💛 💚 💙 💜 🖤 🤍 🤎 💔 ❣️ 💕 💞 💓 💗 💖 💘 💝 💟 ☮️ ' +
      '✝️ ☪️ 🕉 ☸️ ✡️ 🔯 🕎 ☯️ ☦️ 🛐 ⛎ ♈ ♉ ♊ ♋ ♌ ♍ ♎ ♏ ♐ ♑ ♒ ♓ ❗ ❕ ' +
      '❓ 💯 ✅ ❌ ⭕ 🚫 ⚠️ ♻️ 🔱 ⚜️ 🔰 ♾️ 🆗 🆒 🆕 🆓 0️⃣ 1️⃣ 2️⃣ 3️⃣ 4️⃣ 5️⃣ ' +
      '6️⃣ 7️⃣ 8️⃣ 9️⃣ 🔔 🔕 🎵 🎶 💤 💢 💬 🗨 🗯 🔍 🔎 🔏 💡 ⭐ 🌟 ✨ ⚡ 🔥 ' +
      '💥 💫 💦 ☄ 🌪 🌈 ☀ 🌙 🌎 🌏 🌐 🌀')
  };

  var TABS = Object.keys(SETS);
  var pop = null, current = null, anchor = null, tab = 0;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /* Поле, в которое вставляем: то, что сейчас в фокусе, иначе — строка
     запроса агента. */
  function targetInput() {
    var a = document.activeElement;
    if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA') &&
        (a.classList.contains('aibar-input') || a.classList.contains('aibar-msgs-input'))) {
      return a;
    }
    if (current && current._emojiTarget) return current._emojiTarget;
    var ms = document.querySelector('.aibar-msgs-input');
    if (ms) return ms;
    return document.getElementById('aibarInput2') || document.getElementById('aibarInput');
  }

  function insert(text) {
    var input = targetInput();
    if (!input) return;
    var v = input.value || '';
    var pos = input.selectionStart;
    if (pos === null || pos === undefined) pos = v.length;
    var before = v.slice(0, pos), after = v.slice(input.selectionEnd || pos);
    input.value = before + text + after;
    var caret = pos + text.length;
    try { input.setSelectionRange(caret, caret); input.focus(); } catch (e) { /* */ }
  }

  function build() {
    pop = el('div', 'aibar-emoji-pop');
    var tabs = el('div', 'aibar-emoji-tabs');
    TABS.forEach(function (name, i) {
      var b = el('button', 'aibar-emoji-tab' + (i === 0 ? ' on' : ''), name);
      b.type = 'button';
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        tab = i;
        render();
      });
      tabs.appendChild(b);
    });
    var grid = el('div', 'aibar-emoji-grid');
    pop.appendChild(tabs);
    pop.appendChild(grid);
    document.body.appendChild(pop);
    pop.__grid = grid;
    render();
    setTimeout(function () {
      document.addEventListener('click', function close(e) {
        if (pop.contains(e.target)) return;
        closePop();
      });
    }, 30);
  }

  function render() {
    var grid = pop.__grid;
    var tabs = pop.querySelectorAll('.aibar-emoji-tab');
    for (var i = 0; i < tabs.length; i++) tabs[i].classList.toggle('on', i === tab);
    var items = (SETS[TABS[tab]] || '').split(' ');
    grid.innerHTML = items.map(function (e) {
      return '<button type="button" class="aibar-emoji-cell">' + esc(e) + '</button>';
    }).join('');
    grid.querySelectorAll('.aibar-emoji-cell').forEach(function (b) {
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        insert(b.textContent);
      });
    });
  }

  function openPop(btn) {
    if (!pop) build();
    anchor = btn;
    current = btn;
    var r = btn.getBoundingClientRect();
    pop.classList.add('show');
    var w = pop.offsetWidth || 320, h = pop.offsetHeight || 260;
    var left, top;
    if (!r.width && !r.height) {
      /* Кнопка скрыта (например, строка подсказок в свёрнутой панели) —
         показываем сетку у правого нижнего края окна. */
      left = Math.max(8, window.innerWidth - w - 12);
      top = Math.max(8, window.innerHeight - h - 12);
    } else {
      left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - w - 8));
      top = r.top - h - 8;
      if (top < 8) top = Math.min(r.bottom + 8, window.innerHeight - h - 8);
    }
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
  }

  function closePop() {
    if (pop) pop.classList.remove('show');
    current = null;
  }

  /* Кнопка смайлика — рядом с микрофоном в строке подсказок. */
  function boot() {
    document.querySelectorAll('.aibar-hint').forEach(function (hint) {
      if (hint.querySelector('.aibar-emoji-btn')) return;
      var btn = el('button', 'aibar-tool aibar-emoji-btn');
      btn.type = 'button';
      btn.title = 'Эмодзи';
      btn.innerHTML = '<i class="fa-regular fa-face-smile"></i>';
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        if (pop && pop.classList.contains('show') && current === btn) { closePop(); return; }
        openPop(btn);
      });
      var voice = hint.querySelector('.aibar-voice');
      if (voice) voice.parentNode.insertBefore(btn, voice);
      else hint.appendChild(btn);
    });
  }

  window.AiBarEmoji = {
    boot: boot,
    open: openPop,
    close: closePop,
    insert: insert
  };
})();
