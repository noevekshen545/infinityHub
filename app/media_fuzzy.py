# -*- coding: utf-8 -*-
"""Нечёткое сравнение для поиска по медиатеке.

Правила отсюда повторяет один-в-один матчер в app/templates/page_media.html:
страница ищет по уже скачанному индексу на клиенте, сервер судит те же запросы
на своих путях (/api/media/search, выдача qdrant). Расхождение двух реализаций
означало бы «в списке одно, в поиске другое», поэтому здесь нет ничего, чего
нет в JS, и наоборот.

Ключ рубильника — media_search_fuzzy (по умолчанию включён).
"""

import re
import unicodedata
from functools import lru_cache

FLAG_KEY = "media_search_fuzzy"
_OFF = ("0", "false", "off", "no", "")

# Латиница -> кириллица. Мультибуквенные сочетания отрабатывают раньше
# одиночных букв, иначе "sh" съест щ, а "shch" превратится в "щх".
_TRANS_MULTI = (
    ("shch", "щ"), ("sch", "щ"), ("tch", "ч"),
    ("zh", "ж"), ("ch", "ч"), ("sh", "ш"),
    ("yo", "е"), ("ye", "е"), ("yu", "ю"), ("ya", "я"), ("ie", "и"),
)
_TRANS_ONE = {
    "a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х",
    "i": "и", "j": "й", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о",
    "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в",
    "w": "в", "x": "кс", "z": "з",
}
# Соседние буквы кириллицы, после которых "c"/"y" читаются по-другому.
_TRANS_SOFT = "iey"
# Переключённая раскладка: набирают русское слово английскими клавишами.
_LAYOUT = {
    "q": "и", "w": "ц", "e": "у", "r": "к", "t": "е", "y": "н", "u": "г",
    "i": "ш", "o": "щ", "p": "з", "a": "ф", "s": "ы", "d": "в", "f": "а",
    "g": "п", "h": "р", "j": "о", "k": "л", "l": "д", "z": "я", "x": "ч",
    "c": "с", "v": "м", "b": "и", "n": "т", "m": "ь",
}

_ALLOWED = re.compile(r"[^0-9a-z\u0430-\u044f]+")
_WHITESPACE = re.compile(r"\s+")
_SPLIT = re.compile(r"[ ,;.\-–—_/\\()\[\]{}]+")


def norm(value):
    """Строчные буквы без диакритики, только латиница/кириллица/цифры."""
    if value is None:
        return ""
    text = str(value).lower().replace("ё", "е")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _ALLOWED.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def _translit_token(token):
    out = []
    i = 0
    n = len(token)
    while i < n:
        hit = None
        for src, dst in _TRANS_MULTI:
            if token.startswith(src, i):
                hit = (src, dst)
                break
        if hit:
            out.append(hit[1])
            i += len(hit[0])
            continue
        ch = token[i]
        if ch == "c":
            nxt = token[i + 1] if i + 1 < n else ""
            out.append("с" if nxt in _TRANS_SOFT else "к")
            i += 1
            continue
        if ch == "y":
            nxt = token[i + 1] if i + 1 < n else ""
            out.append("и" if nxt else "ы")
            i += 1
            continue
        dst = _TRANS_ONE.get(ch)
        if dst is None:
            out.append(ch)
        else:
            out.append(dst)
        i += 1
    return "".join(out)


def to_cyr(text):
    """Транслитерация латиницы в кириллицу; кириллица проходит без изменений."""
    return " ".join(
        _translit_token(part) if _has_latin(part) else part
        for part in norm(text).split(" ")
    )


def layout_to_cyr(text):
    """Клавиатурный сдвиг раскладки (набрано по-русски английскими клавишами)."""
    out = []
    for part in norm(text).split(" "):
        if not _has_latin(part):
            out.append(part)
            continue
        out.append("".join(_LAYOUT.get(ch, ch) for ch in part))
    return " ".join(out)


def _has_latin(token):
    return any("a" <= ch <= "z" for ch in token)


def tokens(text):
    return [w for w in _SPLIT.split(text) if w]


@lru_cache(maxsize=8192)
def query_forms(query):
    """Токены запроса во всех формах, в которых его стоит понимать."""
    base = norm(query)
    if not base:
        return []
    forms = [base]
    cyr = to_cyr(base)
    if cyr != base:
        forms.append(cyr)
    laid = layout_to_cyr(base)
    if laid and laid not in forms:
        forms.append(laid)
    return [tok for tok in (tokens(f) for f in forms) if tok]


@lru_cache(maxsize=32768)
def text_forms(text):
    base = norm(text)
    if not base:
        return []
    forms = [base]
    cyr = to_cyr(base)
    if cyr != base:
        forms.append(cyr)
    return [tok for tok in (tokens(f) for f in forms) if tok]


@lru_cache(maxsize=65536)
def damerau(a, b):
    """Оптимальная строковая дистанция (DL c транспозициями соседних букв)."""
    la, lb = len(a), len(b)
    if not la or not lb:
        return la or lb
    prev2 = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            val = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            if (
                i > 1
                and j > 1
                and a[i - 1] == b[j - 2]
                and a[i - 2] == b[j - 1]
            ):
                trans = prev2[j - 2] + cost
                if trans < val:
                    val = trans
            cur[j] = val
        prev2 = prev
        prev = cur
    return prev[lb]


def _max_distance(q_len):
    if q_len <= 3:
        return 0
    if q_len <= 7:
        return 1
    return 2


def token_score(qt, tt):
    """Насколько токен текста подходит к токену запроса, 0 если никак."""
    if not qt or not tt:
        return 0.0
    if qt == tt:
        return 1.0
    maxd = _max_distance(len(qt))
    if not maxd:
        if tt.startswith(qt):
            return 0.85
        return 0.0
    if abs(len(qt) - len(tt)) > maxd:
        return 0.0
    d = damerau(qt, tt)
    if d <= maxd:
        return 0.90 if d == 1 else 0.70
    if tt.startswith(qt):
        return 0.85
    if len(qt) >= 4 and tt.find(qt) > 0:
        return 0.70
    if len(qt) >= 4 and qt.startswith(tt):
        return 0.60
    return 0.0


def _phrase_score(qtoks, ttoks):
    """Все токены запроса обязаны зацепиться; оценка — по слабейшему."""
    worst = None
    for qt in qtoks:
        best = 0.0
        for tt in ttoks:
            s = token_score(qt, tt)
            if s > best:
                best = s
            if best == 1.0:
                break
        if best <= 0.0:
            return 0.0
        worst = best if worst is None else min(worst, best)
    return worst or 0.0


def score(query, text, weight=1.0):
    """Оценка соответствия поля text запросу query, 0..weight."""
    nq = norm(query)
    nt = norm(text)
    if not nq or not nt:
        return 0.0
    if len(nq) >= 3 and nq in nt:
        return 1.0 * weight
    qforms = query_forms(nq)
    tforms = text_forms(nt)
    if not qforms or not tforms:
        return 0.0
    best = 0.0
    for qtoks in qforms:
        for ttoks in tforms:
            s = _phrase_score(qtoks, ttoks)
            if s > best:
                best = s
    return best * weight


def score_fields(query, fields):
    """fields — последовательность (текст, вес). Итог — сильнейшее поле."""
    best = 0.0
    for text, weight in fields:
        s = score(query, text, weight)
        if s > best:
            best = s
    return best


def enabled():
    """Рубильник нечёткого поиска. Ключа в настройках нет — значит включён."""
    try:
        from media_module import get_setting_value
        raw = get_setting_value(FLAG_KEY, "1")
    except Exception:
        return True
    return str(raw if raw is not None else "1").strip().lower() not in _OFF
