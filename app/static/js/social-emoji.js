/* Shared emoji dataset for social module (chat, wall, groups). */
(function () {
  const SKIN = ['🏻', '🏼', '🏽', '🏾', '🏿'];
  const TONE_CAPABLE = new Set([
    '👋', '🤚', '🖐', '✋', '🖖', '👌', '🤌', '🤏', '✌', '🤞', '🫰', '🤟', '🤘', '🤙',
    '👈', '👉', '👆', '👇', '☝', '🫵', '👍', '👎', '✊', '👊', '🤛', '🤜', '👏', '🙌',
    '🫶', '👐', '🤲', '🤝', '🙏', '✍', '💅', '🤳', '💪', '🦵', '🦶', '👂', '🦻', '👃',
    '👶', '🧒', '👦', '👧', '🧑', '👱', '👨', '👩', '🧔', '👵', '🧓', '👴', '👲', '👳',
    '🧕', '👮', '🕵', '💂', '🥷', '👷', '🤴', '👸', '👰', '🤵', '👼', '🎅', '🤶', '🦸',
    '🦹', '🧙', '🧚', '🧛', '🧜', '🧝', '🧞', '🧟', '💆', '💇', '🚶', '🧍', '🏃', '💃',
    '🕺', '🧖', '🧗', '🏇', '🏌', '🏄', '🚣', '🏊', '⛹', '🏋', '🚴', '🚵', '🤸', '🤼',
    '🤽', '🤾', '🤹', '🧘', '🛀', '🛌', '👭', '👫', '👬', '💏', '💑', '👪', '🧑🤝🧑'
  ]);
  function stripVS(e) { return e.replace(/[\uFE0F\u200D]/g, '') }
  function expand(list) {
    const out = [];
    for (const e of list) {
      out.push(e);
      if (TONE_CAPABLE.has(stripVS(e))) for (const t of SKIN) out.push(stripVS(e) + t);
    }
    return out;
  }
  function flag(code) { code = code.toUpperCase(); return code.split('').map(c => String.fromCodePoint(0x1F1E6 + c.charCodeAt(0) - 65)).join('') }
  const COUNTRIES = [
    'AC', 'AD', 'AE', 'AF', 'AG', 'AI', 'AL', 'AM', 'AO', 'AQ', 'AR', 'AS', 'AT', 'AU', 'AW', 'AX', 'AZ',
    'BA', 'BB', 'BD', 'BE', 'BF', 'BG', 'BH', 'BI', 'BJ', 'BL', 'BM', 'BN', 'BO', 'BQ', 'BR', 'BS', 'BT',
    'BV', 'BW', 'BY', 'BZ', 'CA', 'CC', 'CD', 'CF', 'CG', 'CH', 'CI', 'CK', 'CL', 'CM', 'CN', 'CO', 'CP',
    'CR', 'CU', 'CV', 'CW', 'CX', 'CY', 'CZ', 'DE', 'DG', 'DJ', 'DK', 'DM', 'DO', 'DZ', 'EA', 'EC', 'EE',
    'EG', 'EH', 'ER', 'ES', 'ET', 'EU', 'FI', 'FJ', 'FK', 'FM', 'FO', 'FR', 'GA', 'GB', 'GD', 'GE', 'GF',
    'GG', 'GH', 'GI', 'GL', 'GM', 'GN', 'GP', 'GQ', 'GR', 'GS', 'GT', 'GU', 'GW', 'GY', 'HK', 'HM', 'HN',
    'HR', 'HT', 'HU', 'IC', 'ID', 'IE', 'IL', 'IM', 'IN', 'IO', 'IQ', 'IR', 'IS', 'IT', 'JE', 'JM', 'JO',
    'JP', 'KE', 'KG', 'KH', 'KI', 'KM', 'KN', 'KP', 'KR', 'KW', 'KY', 'KZ', 'LA', 'LB', 'LC', 'LI', 'LK',
    'LR', 'LS', 'LT', 'LU', 'LV', 'LY', 'MA', 'MC', 'MD', 'ME', 'MF', 'MG', 'MH', 'MK', 'ML', 'MM', 'MN',
    'MO', 'MP', 'MQ', 'MR', 'MS', 'MT', 'MU', 'MV', 'MW', 'MX', 'MY', 'MZ', 'NA', 'NC', 'NE', 'NF', 'NG',
    'NI', 'NL', 'NO', 'NP', 'NR', 'NU', 'NZ', 'OM', 'PA', 'PE', 'PF', 'PG', 'PH', 'PK', 'PL', 'PM', 'PN',
    'PR', 'PS', 'PT', 'PW', 'PY', 'QA', 'RE', 'RO', 'RS', 'RU', 'RW', 'SA', 'SB', 'SC', 'SD', 'SE', 'SG',
    'SH', 'SI', 'SJ', 'SK', 'SL', 'SM', 'SN', 'SO', 'SR', 'SS', 'ST', 'SV', 'SX', 'SY', 'SZ', 'TA', 'TC',
    'TD', 'TF', 'TG', 'TH', 'TJ', 'TK', 'TL', 'TM', 'TN', 'TO', 'TR', 'TT', 'TV', 'TW', 'TZ', 'UA', 'UG',
    'UM', 'US', 'UY', 'UZ', 'VA', 'VC', 'VE', 'VG', 'VI', 'VN', 'VU', 'WF', 'WS', 'XK', 'YE', 'YT', 'ZA',
    'ZM', 'ZW'
  ];

  const CATS = [
    { name: 'Смайлы', icon: '😀', emojis: [
      '😀', '😃', '😄', '😁', '😆', '😅', '🤣', '😂', '🙂', '🙃', '🫠', '😉', '😊', '😇', '🥰', '😍',
      '🤩', '😘', '😗', '😚', '😙', '🥲', '😋', '😛', '😜', '🤪', '😝', '🤑', '🤗', '🤭', '🫢', '🫣',
      '🤫', '🤔', '🫡', '🤐', '🤨', '😐', '😑', '😶', '🫥', '😶‍🌫️', '😏', '😒', '🙄', '😬', '😮‍💨', '😌',
      '😔', '😪', '🤤', '😴', '😷', '🤒', '🤕', '🤢', '🤮', '🤧', '🥵', '🥶', '🥴', '😵', '😵‍💫', '🤯',
      '🤠', '🥳', '🥸', '😎', '🤓', '🧐', '😕', '🫤', '😟', '🙁', '☹️', '😮', '😯', '😲', '😳', '🥺',
      '🥹', '😦', '😧', '😨', '😰', '😥', '😢', '😭', '😱', '😖', '😣', '😞', '😓', '😩', '😫', '🥱',
      '😤', '😡', '😠', '🤬', '😈', '👿', '💀', '☠️', '💩', '🤡', '👹', '👺', '👻', '👽', '👾', '🤖',
      '🎃', '😺', '😸', '😹', '😻', '😼', '😽', '🙀', '😿', '😾', '🫨', '🫥', '🫠', '🥹' ] },
    { name: 'Жесты', icon: '👋', tone: true, emojis: [
      '👋', '🤚', '🖐️', '✋', '🖖', '👌', '🤌', '🤏', '✌️', '🤞', '🫰', '🤟', '🤘', '🤙', '👈', '👉',
      '👆', '👇', '☝️', '🫵', '👍', '👎', '✊', '👊', '🤛', '🤜', '👏', '🙌', '🫶', '👐', '🤲', '🤝',
      '🙏', '✍️', '💅', '🤳', '💪', '🦾', '🦿', '🦵', '🦶', '👂', '🦻', '👃', '🧠', '🫀', '🫁', '🗣️',
      '👀', '👁️', '👅', '👄', '💋', '🩷', '🩸', '🩹', '🩻', '🦷', '🦴' ] },
    { name: 'Люди', icon: '🧑', tone: true, emojis: [
      '👶', '🧒', '👦', '👧', '🧑', '👱', '👨', '👩', '🧔', '👵', '🧓', '👴', '👲', '👳', '🧕', '🥷',
      '👮', '🕵️', '💂', '👷', '🤴', '👸', '👰', '🤵', '👼', '🎅', '🤶', '🦸', '🦹', '🧙', '🧚', '🧛',
      '🧜', '🧝', '🧞', '🧟', '💆', '💇', '🚶', '🧍', '🏃', '💃', '🕺', '🧖', '🧗', '🏇', '🏌️', '🏄',
      '🚣', '🏊', '⛹️', '🏋️', '🚴', '🚵', '🤸', '🤼', '🤽', '🤾', '🤹', '🧘', '🛀', '🛌', '🕴️', '🗣️',
      '👭', '👫', '👬', '💏', '💑', '👪', '🧑‍🤝‍🧑', '👩‍⚕️' ] },
    { name: 'Сердца', icon: '❤️', emojis: [
      '❤️', '🧡', '💛', '💚', '💙', '💜', '🖤', '🤍', '🤎', '💔', '❤️‍🔥', '❤️‍🩹', '❣️', '💕', '💞', '💓',
      '💗', '💖', '💘', '💝', '💟', '♥️', '💌', '💋', '💯', '💢', '💥', '💫', '💦', '💨', '🕳️', '💣',
      '💬', '🗨️', '🗯️', '💭', '💤', '🩵', '🩶', '🩷', '💮', '🙂', '💜' ] },
    { name: 'Животные', icon: '🐶', emojis: [
      '🐶', '🐱', '🐭', '🐹', '🐰', '🦊', '🐻', '🐼', '🐻‍❄️', '🐨', '🐯', '🦁', '🐮', '🐷', '🐽', '🐸',
      '🐵', '🙈', '🙉', '🙊', '🐒', '🐔', '🐧', '🐦', '🐤', '🐣', '🐥', '🦆', '🦅', '🦉', '🦇', '🐺',
      '🐗', '🐴', '🦄', '🐝', '🪱', '🐛', '🦋', '🐌', '🐞', '🐜', '🪰', '🪲', '🪳', '🦟', '🦗', '🕷️',
      '🕸️', '🦂', '🐢', '🐍', '🦎', '🦖', '🦕', '🐙', '🦑', '🦐', '🦞', '🦀', '🐡', '🐠', '🐟', '🐬',
      '🐳', '🐋', '🦈', '🐊', '🐅', '🐆', '🦓', '🦍', '🦧', '🦣', '🐘', '🦛', '🦏', '🐪', '🐫', '🦒',
      '🦘', '🦬', '🐃', '🐂', '🐄', '🐎', '🐖', '🐏', '🐑', '🦙', '🐐', '🦌', '🐕', '🐩', '🦮', '🐕‍🦺',
      '🐈', '🐈‍⬛', '🪶', '🦢', '🦩', '🦚', '🦜', '🦤', '🦃', '🐓', '🦭', '🦫', '🦦', '🦥', '🐿️', '🦔',
      '🦝', '🦨', '🦡', '🦂', '🪼', '🫎', '🫏', '🦛' ] },
    { name: 'Природа', icon: '🌿', emojis: [
      '🌵', '🎄', '🌲', '🌳', '🌴', '🪵', '🌱', '🌿', '☘️', '🍀', '🎍', '🪴', '🎋', '🍃', '🍂', '🍁',
      '🍄', '🐚', '🪨', '🌾', '💐', '🌷', '🌹', '🥀', '🌺', '🌸', '🌼', '🌻', '🌞', '🌝', '🌛', '🌜',
      '🌚', '🌕', '🌖', '🌗', '🌘', '🌑', '🌒', '🌓', '🌔', '🌙', '🌎', '🌍', '🌏', '🪐', '💫', '⭐',
      '🌟', '✨', '⚡', '☄️', '🔥', '🌈', '☀️', '🌤️', '⛅', '🌥️', '☁️', '🌦️', '🌧️', '⛈️', '🌩️', '🌨️',
      '❄️', '☃️', '⛄', '🌬️', '💨', '💧', '☔', '☂️', '🌊', '🌫️', '🌋', '⛰️', '🏔️', '🗻', '🏕️', '🏖️' ] },
    { name: 'Еда', icon: '🍕', emojis: [
      '🍏', '🍎', '🍐', '🍊', '🍋', '🍌', '🍉', '🍇', '🍓', '🫐', '🍈', '🍒', '🍑', '🥭', '🍍', '🥥',
      '🥝', '🍅', '🍆', '🥑', '🥦', '🥬', '🥒', '🌶️', '🫑', '🌽', '🥕', '🫒', '🧄', '🧅', '🥔', '🍠',
      '🥐', '🥯', '🍞', '🥖', '🥨', '🧀', '🥚', '🍳', '🧈', '🥞', '🧇', '🥓', '🥩', '🍗', '🍖', '🌭',
      '🍔', '🍟', '🍕', '🫓', '🥪', '🥙', '🧆', '🌮', '🌯', '🫔', '🥗', '🥘', '🫕', '🥫', '🍝', '🍜',
      '🍲', '🍛', '🍣', '🍱', '🥟', '🦪', '🍤', '🍙', '🍚', '🍘', '🍥', '🥠', '🥮', '🍢', '🍡', '🍧',
      '🍨', '🍦', '🥧', '🧁', '🍰', '🎂', '🍮', '🍭', '🍬', '🍫', '🍿', '🍩', '🍪', '🌰', '🥜', '🍯',
      '🥛', '🍼', '🫖', '☕', '🍵', '🧃', '🥤', '🧋', '🍶', '🍺', '🍻', '🥂', '🍷', '🥃', '🍸', '🍹',
      '🧉', '🍾', '🧊', '🫗', '🥄', '🍴', '🥣', '🍽️', '🫙' ] },
    { name: 'Активности', icon: '⚽', emojis: [
      '⚽', '🏀', '🏈', '⚾', '🥎', '🎾', '🏐', '🏉', '🥏', '🎱', '🪀', '🏓', '🏸', '🏒', '🏑', '🥍',
      '🏏', '🪃', '🥅', '⛳', '🪁', '🏹', '🎣', '🤿', '🥊', '🥋', '🎽', '🛹', '🛼', '🛷', '⛸️', '🥌',
      '🎿', '⛷️', '🏂', '🪂', '🏋️', '🤼', '🤸', '🤺', '⛹️', '🤾', '🏌️', '🏇', '🧘', '🏄', '🏊', '🤽',
      '🚣', '🧗', '🚵', '🚴', '🏆', '🥇', '🥈', '🥉', '🏅', '🎖️', '🏵️', '🎗️', '🎫', '🎟️', '🎪', '🤹',
      '🎭', '🩰', '🎨', '🎬', '🎤', '🎧', '🎼', '🎹', '🥁', '🪘', '🎷', '🎺', '🪗', '🎸', '🪕', '🎻',
      '🪈', '🎲', '♟️', '🎯', '🎳', '🎮', '🎰', '🧩', '🩹' ] },
    { name: 'Места', icon: '🏙', emojis: [
      '🗺️', '🗿', '🗽', '🗼', '🏰', '🏯', '🏟️', '🎡', '🎢', '🎠', '⛲', '⛱️', '🏖️', '🏝️', '🏜️', '🌋',
      '⛰️', '🏔️', '🗻', '🏕️', '⛺', '🛖', '🏠', '🏡', '🏘️', '🏚️', '🏗️', '🏭', '🏢', '🏬', '🏣', '🏤',
      '🏥', '🏦', '🏨', '🏪', '🏫', '🏩', '💒', '🏛️', '⛪', '🕌', '🕍', '🛕', '🕋', '⛩️', '🛤️', '🛣️',
      '🗾', '🌐', '🌍', '🌏', '🌎', '⛽', '🚧', '🚦', '🚥', '🚏', '🗳️', '🗼' ] },
    { name: 'Транспорт', icon: '🚗', emojis: [
      '🚗', '🚕', '🚙', '🚌', '🚎', '🏎️', '🚓', '🚑', '🚒', '🚐', '🛻', '🚚', '🚛', '🚜', '🛴', '🚲',
      '🛵', '🏍️', '🛺', '🚨', '🚔', '🚍', '🚘', '🚖', '🚡', '🚠', '🚟', '🚃', '🚋', '🚞', '🚝', '🚄',
      '🚅', '🚈', '🚂', '🚆', '🚇', '🚊', '🚉', '✈️', '🛫', '🛬', '🛩️', '💺', '🛰️', '🚀', '🛸', '🚁',
      '🛶', '⛵', '🚤', '🛥️', '🛳️', '⛴️', '🚢', '⚓', '🪝', '🛞', '🛟' ] },
    { name: 'Вещи', icon: '💻', emojis: [
      '⌚', '📱', '📲', '💻', '⌨️', '🖥️', '🖨️', '🖱️', '🖲️', '🕹️', '🗜️', '💽', '💾', '💿', '📀', '📼',
      '📷', '📸', '📹', '🎥', '📽️', '🎞️', '📞', '☎️', '📟', '📠', '📺', '📻', '🎙️', '🎚️', '🎛️', '🧭',
      '⏱️', '⏲️', '⏰', '🕰️', '⌛', '⏳', '📡', '🔋', '🔌', '💡', '🔦', '🕯️', '🪔', '🧯', '🛢️', '💸',
      '💵', '💴', '💶', '💷', '🪙', '💰', '💳', '💎', '⚖️', '🪜', '🧰', '🪛', '🔧', '🔨', '⚒️', '🛠️',
      '⛏️', '🪚', '🔩', '⚙️', '🪤', '🧱', '⛓️', '🧲', '🔫', '💣', '🧨', '🪓', '🔪', '🗡️', '⚔️', '🛡️',
      '🚬', '⚰️', '🪦', '⚱️', '🏺', '🔮', '📿', '🧿', '💈', '⚗️', '🧪', '🧫', '🧬', '🔬', '🔭', '📜',
      '📯', '📰', '📚', '📖', '🔖', '🧷', '📎', '📐', '📏', '🧮', '📌', '📍', '✂️', '🖊️', '🖋️', '✒️',
      '🖌️', '🖍️', '📝', '✏️', '🔍', '🔎', '🔏', '🔐', '🔒', '🔓', '🪝', '🧳', '🩸' ] },
    { name: 'Символы', icon: '✨', emojis: [
      '❤️', '💯', '💢', '💬', '💭', '💤', '♨️', '🎵', '🎶', '➕', '➖', '➗', '✖️', '💲', '💱', '™️',
      '©️', '®️', '‼️', '⁉️', '❓', '❔', '❕', '❗', '〰️', '➰', '➿', '🔱', '〽️', '⚜️', '✅', '❌',
      '❎', '🔟', '🔠', '🔡', '🔢', '🔣', '🔤', '🅰️', '🆎', '🅱️', '🆑', '🆒', '🆓', 'ℹ️', '🆔',
      'Ⓜ️', '🆕', '🆖', '🅾️', '🆗', '🅿️', '🆘', '🆙', '🆚', '🈁', '🈂️', '🈷️', '🈶', '🈯', '🉐',
      '🈹', '🈚', '🈲', '🉑', '🈸', '🈴', '🈳', '㊗️', '㊙️', '🈺', '🈵', '🔴', '🟠', '🟡', '🟢', '🔵',
      '🟣', '🟤', '⚫', '⚪', '🟥', '🟧', '🟨', '🟩', '🟦', '🟪', '🟫', '⬛', '⬜', '◼️', '◻️', '◾',
      '◽', '▪️', '▫️', '🔶', '🔷', '🔸', '🔹', '🔺', '🔻', '💠', '🔘', '🔲', '🔳', '⚪️', '⚫️', '🔈',
      '🔇', '🔉', '🔊', '🔔', '🔕', '📣', '📢', '♠️', '♥️', '♦️', '♣️', '🃏', '🎴', '🀄', '🕐', '🕑',
      '🕒', '🕓', '🕔', '🕕', '🕖', '🕗', '🕘', '🕙', '🕚', '🕛', '🕜', '🕝', '🕞', '🕟', '🕠', '🕡',
      '🕢', '🕣', '🕤', '🕥', '🕦', '🕧', '◀️', '▶️', '🔽', '🔼', '⬅️', '➡️', '⬆️', '⬇️' ] },
    { name: 'Зодиак', icon: '♈', emojis: [
      '♈', '♉', '♊', '♋', '♌', '♍', '♎', '♏', '♐', '♑', '♒', '♓', '⛎', '☀️', '🌙', '⭐', '☄️', '🌝' ] },
    { name: 'Флаги', icon: '🏁', emojis: COUNTRIES.map(flag) }
  ];

  window.SOCIAL_EMOJI_CATS = CATS;
  window.SOCIAL_EMOJI = {
    expand: expand,
    flag: flag
  };
  window.SOCIAL_EMOJI_prep = function () {
    return CATS.map(function (c) { return c.tone ? Object.assign({}, c, { emojis: expand(c.emojis) }) : c; });
  };
  window.SOCIAL_EMOJI_stickerPacks = function () {
    return fetch('/api/social/stickers/packs').then(function (r) { return r.json(); }).catch(function () { return []; });
  };
  function _esc(t) {
    return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  window.SOCIAL_EMOJI.makeWallPicker = function (cfg) {
    var panel = document.getElementById(cfg.panel);
    var body = document.getElementById(cfg.body);
    var target = document.getElementById(cfg.target);
    var chips = cfg.chips || [];
    var onSticker = cfg.onSticker || function () {};
    var state = { tab: 'emoji', cat: 0, packs: [], packIdx: 0, pending: null, packsLoaded: false };
    var cats = CATS.map(function (c) { return c.tone ? Object.assign({}, c, { emojis: expand(c.emojis) }) : c; });
    var uid = 'wp' + (window.__wpCount = (window.__wpCount || 0) + 1);
    window.__wpReg = window.__wpReg || {};
    window.__wpReg[uid] = function () { clearPending() };
    function renderChips() {
      for (var i = 0; i < chips.length; i++) {
        var h = document.getElementById(chips[i]);
        if (h) h.innerHTML = state.pending ? '<div class="wp-chip"><img src="' + _esc(state.pending) + '" alt=""> Стикер <button type="button" onclick="window.__wpReg&&window.__wpReg[\'' + uid + '\']&&window.__wpReg[\'' + uid + '\']()"><i class="fas fa-times"></i></button></div>' : '';
      }
    }
    function renderEmoji() {
      if (!cats.length) { body.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text3)">Нет данных</div>'; return }
      var c = cats[state.cat] || cats[0];
      body.innerHTML = '<div class="wp-cats">' + cats.map(function (x, i) {
        return '<button type="button" class="' + (i === state.cat ? 'active' : '') + '" data-cat="' + i + '" title="' + _esc(x.name) + '">' + x.icon + '</button>';
      }).join('') + '</div><div class="wp-grid">' + c.emojis.map(function (e) {
        return '<div class="wg">' + e + '</div>';
      }).join('') + '</div>';
    }
    function renderStickers() {
      if (!state.packs.length && !state.packsLoaded) { loadPacks().then(render); return }
      body.innerHTML = '<div class="wp-pack-row" data-create="1"><i class="fas fa-plus" style="color:var(--accent)"></i> Создать свой набор...</div>' +
        state.packs.map(function (p, i) {
          return '<div class="wp-pack-row ' + (i === state.packIdx ? 'active' : '') + '" data-pack="' + i + '">' + _esc(p.name) + '<span style="font-size:10px;color:var(--text3)">' + (p.stickers || []).length + ' шт.</span></div>';
        }).join('') +
        '<div class="wp-stickers">' + (state.packs[state.packIdx] && state.packs[state.packIdx].stickers || []).map(function (s) {
          return '<div class="ws" data-src="' + _esc(s.url) + '"><img src="' + _esc(s.url) + '" loading="lazy" alt=""></div>';
        }).join('') + '</div>';
    }
    function onBodyClick(ev) {
      if (!ev.target || !ev.target.closest) return;
      // Не даём клику внутри пикера всплыть до обработчика «клик вне окна»:
      // после перерисовки (смена категории/набора) исходная кнопка уже
      // отсоединена от DOM, closest('#...Picker') не находится, и окно
      // закрывалось прямо при переключении категории.
      var t = ev.target.closest('.wp-cats button');
      if (t) { ev.stopPropagation(); state.cat = +t.dataset.cat; render(); return; }
      t = ev.target.closest('.wg');
      if (t) { ev.stopPropagation(); insertEmoji(t.textContent); return; }
      t = ev.target.closest('.wp-pack-row[data-create]');
      if (t) { ev.stopPropagation(); createPack(); return; }
      t = ev.target.closest('.wp-pack-row[data-pack]');
      if (t) { ev.stopPropagation(); state.packIdx = +t.dataset.pack; render(); return; }
      t = ev.target.closest('.ws');
      if (t) { ev.stopPropagation(); setPending(t.dataset.src); }
    }
    function render() {
      if (state.tab === 'emoji') renderEmoji(); else renderStickers();
    }
    function insertEmoji(e) {
      var s = target.selectionStart ?? target.value.length;
      var en = target.selectionEnd ?? target.value.length;
      target.value = target.value.slice(0, s) + e + target.value.slice(en);
      target.focus(); target.selectionStart = target.selectionEnd = s + e.length;
    }
    function setPending(src) { state.pending = src; renderChips(); onSticker(src); }
    function clearPending() { state.pending = null; renderChips(); onSticker(null); }
    function toggle() {
      var willOpen = !panel.classList.contains('show');
      panel.classList.toggle('show');
      if (willOpen) {
        // При повторном открытии всегда начинаем с первой категории,
        // а не с той, что была выбрана в прошлый раз.
        state.cat = 0;
        render();
      } else if (!body.innerHTML) {
        render();
      }
    }
    function close() { panel.classList.remove('show') }
    function switchTab(t) {
      state.tab = t;
      panel.querySelectorAll('.wp-tabs button').forEach(function (b) { b.classList.toggle('active', b.dataset.wtab === t); });
      render();
    }
    function loadPacks() { return window.SOCIAL_EMOJI_stickerPacks().then(function (p) { state.packs = p; state.packsLoaded = true; }); }
    async function createPack() {
      var name = await InfinityDialogs.prompt({label:'Название нового набора стикеров:'});
      if (!name) return;
      fetch('/api/social/stickers/packs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          var input = document.createElement('input'); input.type = 'file'; input.accept = 'image/*'; input.multiple = true;
          input.onchange = function () {
            var fd = new FormData();
            for (var i = 0; i < input.files.length; i++) fd.append('files', input.files[i]);
            fetch('/api/social/stickers/packs/' + res.id + '/stickers', { method: 'POST', body: fd })
              .then(function () { return loadPacks(); })
              .then(render);
          };
          input.click();
        });
    }
    body.addEventListener('click', onBodyClick);
    return { toggle: toggle, close: close, switchTab: switchTab, render: render, setPending: setPending, clearPending: clearPending, insertEmoji: insertEmoji, get pending() { return state.pending } };
  };
})();