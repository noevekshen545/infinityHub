"""Автономные копии модулей: один самодостаточный HTML, который устройство
скачивает и читает без центрального ПК.

Service worker на телефоне не работает: он требует secure context, а устройство
ходит на http://192.168.x.x. Скачанный файл работает при любом origin и при
выключенном сервере.

Всё, что попадает в файл, экранируется: RSS, чужие fb2 и пользовательский текст
— это данные, а не разметка. Файл живёт на устройстве вне контроля сервера, и
дыру в нём потом не починить.

Слои разделены: здесь только сборка страницы и арифметика бюджета; где и как
читать данные — знает маршрут.
"""

import html
import json
import re
import time
from email.utils import parsedate_to_datetime
import swallow

MAX_LIMIT = 500
DEFAULT_LIMIT = 200
# Ограничения на размер одного куска текста в копии: без них файл становится
# больше того, что телефон реально открывает.
NOTE_MAX_CHARS = 20000
MSG_MAX_CHARS = 4000
BOOK_MAX_CHARS = 2000000
CONV_LIMIT = 50
MSG_PER_CONV = 300


def _text(value, limit=None):
    s = "" if value is None else str(value)
    if limit and len(s) > limit:
        s = s[:limit] + "…"
    return html.escape(s, quote=True)


def _strip_tags(value):
    """RSS content — это HTML; теги выкидываем целиком, оставляем текст.

    Тело <script>/<style> убираем вместе с тегом: как текст оно выглядело бы
    в копии мусором, а терять содержимое обычного тега было бы неверно.
    """
    s = str(value or "")
    out, i = [], 0
    while i < len(s):
        j = s.find("<", i)
        if j < 0:
            out.append(s[i:])
            break
        out.append(s[i:j])
        k = s.find(">", j)
        if k < 0:
            out.append(s[j:])   # одиночный «<» в тексте: он всё равно экранируется ниже
            break
        tag = re.match(r"<\s*(script|style)\b", s[j:], re.I)
        if tag:
            close = re.search(r"</\s*%s\s*>" % tag.group(1), s[k + 1:], re.I)
            i = (k + 1 + close.end()) if close else len(s)
            continue
        i = k + 1
    return " ".join("".join(out).split())


def _when(value):
    """Дата записи в человекочитаемый вид.

    В реальном потоке форм три: RSS-строка ("Sun, 11 Oct 2026 16:57:42 +0300"),
    строка unix-времени ("1791728650.56") и число. Неизвестное просто остаётся
    без подписи — копия не обязана падать из-за одного кривого фида.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        s = value.strip()
        try:
            value = float(s)
        except ValueError:
            swallow.note('offline_export._when:L81')
            try:
                return parsedate_to_datetime(s).strftime("%d.%m.%Y %H:%M")
            except (TypeError, ValueError):
                swallow.note('offline_export._when:L85')
                return ""
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        swallow.note('offline_export._when:L90')
        return ""


def _size(value):
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        swallow.note('offline_export._size:L98')
        return ""
    for unit, div in (("МБ", 1048576), ("КБ", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{int(n)} Б"


def _hay(parts, limit=1200):
    return _text(" ".join(str(p) for p in parts if p).lower(), limit)


# Маркеры вида @TITLE@, а не %s/{}: в CSS есть и % (width:100%), и {} —
# любой строковый форматитель спотыкается об их разметку.
_PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@TITLE@ — автономная копия</title>
<style>
:root{--bg:#121417;--fg:#e6e8ea;--mut:#98a2ad;--bd:#2a2f36;--ac:#5aa2ff}
@media (prefers-color-scheme: light){:root{--bg:#fff;--fg:#14181d;--mut:#5d6a76;--bd:#e2e6ea;--ac:#1565c0}}
*{box-sizing:border-box}
body{margin:0;padding:14px;background:var(--bg);color:var(--fg);
 font:15px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
header{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
 padding-bottom:10px;border-bottom:1px solid var(--bd);margin-bottom:12px;flex-wrap:wrap}
h1{font-size:17px;margin:0}
h2{font-size:15px;margin:0 0 6px}
.meta{color:var(--mut);font-size:12px}
.note{background:rgba(90,162,255,.12);border:1px solid var(--bd);border-radius:10px;
 padding:8px 10px;font-size:12px;color:var(--mut);margin-bottom:12px}
article{border:1px solid var(--bd);border-radius:12px;padding:12px;margin-bottom:10px}
.t{font-weight:600;margin:0 0 6px}
.s{color:var(--mut);font-size:12px;margin-bottom:6px}
.b{font-size:14px;white-space:pre-wrap}
.msg{padding:5px 0;border-top:1px solid var(--bd);white-space:pre-wrap}
.dim{color:var(--mut);font-style:italic;font-size:13px}
a{color:var(--ac);text-decoration:none}
input{width:100%;padding:9px 10px;margin-bottom:12px;border:1px solid var(--bd);
 border-radius:10px;background:transparent;color:var(--fg);font-size:14px}
</style></head><body>
<header><h1>@TITLE@</h1><div class="meta">копия от @MADE@ · записей @COUNT@</div></header>
<div class="note">Файл автономный: ПК включать не нужно. Обновите копию, когда сервер снова в сети.
@EXPIRES@</div>
<input id="q" type="search" placeholder="поиск по копии" oninput="filt()">
<div id="list">@ITEMS@</div>
<script type="application/json" id="data">@DATA@</script>
<script>
function filt(){
  var q=document.getElementById('q').value.toLowerCase();
  document.querySelectorAll('article').forEach(function(a){
    a.style.display = !q || (a.dataset.hay||'').indexOf(q)>=0 ? '' : 'none';
  });
}
</script>
</body></html>"""


def _render(title, items, kept, made_at=None, valid_days=7):
    made_at = made_at or time.time()
    expires = ""
    if valid_days:
        until = made_at + valid_days * 86400
        expires = (" Обновление теряет смысл после "
                   f"{time.strftime('%d.%m.%Y', time.localtime(until))}.")
    data = json.dumps({"made_at": int(made_at), "items": kept}, ensure_ascii=False)
    data = data.replace("<", "\\u003c")   # </script> внутри JSON не должен закрывать блок
    vals = {
        "@TITLE@": _text(title),
        "@MADE@": _text(time.strftime("%d.%m.%Y %H:%M", time.localtime(made_at))),
        "@COUNT@": str(len(items)),
        "@EXPIRES@": _text(expires),
        "@ITEMS@": "".join(items),
        "@DATA@": data,
    }
    # one-проход, а не цепочка replace(...): заголовок вида "@DATA@" иначе
    # подставился бы повторно в уже вставленном тексте списка.
    return re.sub("|".join(map(re.escape, vals)), lambda m: vals[m.group(0)], _PAGE)


def _news_item(a):
    title = _text(a.get("title"), 300)
    body = _text(_strip_tags(a.get("content") or a.get("summary")), 4000)
    src = _text(a.get("source_name") or a.get("source_id"), 60)
    when = _text(_when(a.get("published") or a.get("fetched_at")))
    link = a.get("link") or ""
    safe_link = html.escape(str(link), quote=True) if str(link).startswith(("http://", "https://")) else ""
    hay = _hay([a.get("title"), a.get("source_name"), _strip_tags(a.get("content"))[:400]])
    tail = f'<div class="s">{src} · {when}</div>' if (src or when) else ""
    head = f'<a href="{safe_link}" rel="noreferrer">' if safe_link else ""
    close = "</a>" if safe_link else ""
    return (f'<article data-hay="{hay}"><div class="t">{head}{title}{close}</div>'
            f'{tail}<div class="b">{body}</div></article>')


def build_news(articles, made_at=None, valid_days=7, title="Новости"):
    """Собрать автономную страницу из списка статей."""
    items, kept = [], []
    for a in articles:
        if not isinstance(a, dict):
            continue
        items.append(_news_item(a))
        kept.append({"id": a.get("id"), "title": str(a.get("title") or "")[:300]})
        if len(items) >= MAX_LIMIT:
            break
    return _render(title, items, kept, made_at, valid_days)


def _note_item(n):
    title = _text(n.get("title") or "(без названия)", 300)
    tags = n.get("tags")
    tags = ", ".join(str(t) for t in tags[:8]) if isinstance(tags, (list, tuple)) else str(tags or "")[:80]
    meta = " · ".join(x for x in (_text(n.get("folder"), 60), _text(tags, 120),
                                  _text(_when(n.get("updated") or n.get("created")))) if x)
    hay = _hay([n.get("title"), str(n.get("content") or "")[:400]])
    return (f'<article data-hay="{hay}"><div class="t">{title}</div>'
            f'<div class="s">{meta}</div>'
            f'<div class="b">{_text(n.get("content"), NOTE_MAX_CHARS)}</div></article>')


def build_notes(notes, made_at=None, valid_days=0, title="Заметки"):
    """Заметки: markdown остаётся текстом — размечать его в копии не нужно."""
    items, kept = [], []
    for n in notes:
        if not isinstance(n, dict):
            continue
        items.append(_note_item(n))
        kept.append({"id": n.get("id"), "title": str(n.get("title") or "")[:300]})
        if len(items) >= MAX_LIMIT:
            break
    return _render(title, items, kept, made_at, valid_days)


def _utf8_len(value):
    return len(str(value or "").encode("utf-8"))


def _utf8_clip(value, n_bytes):
    """Обрезать строку по байтам UTF-8, не разрывая многобайтовый символ.

    Бюджет копии — в байтах (его измеряет файл на устройстве), а текст
    русский: один символ весит два байта. Считать бюджет в символах — значит
    получить файл ровно вдвое больше обещанного.
    """
    if n_bytes <= 0:
        return ""
    raw = str(value or "").encode("utf-8")
    if len(raw) <= n_bytes:
        return str(value or "")
    return raw[:n_bytes].decode("utf-8", errors="ignore")


class SkipText(Exception):
    """Маршрут сознательно не стал читать текст этого тома.

    Причина (текст исключения) идёт в копию вместо текста: «не смогли
    разобрать», «кончилось время» и «формат читается минуту» — для читателя
    копии это разные советы, и подменять их одним «не вошёл в бюджет» значит
    увести человека не туда.
    """


def _book_item(b):
    title = _text(b.get("title") or b.get("name"), 300)
    meta = " · ".join(x for x in (_text(b.get("author"), 120),
                                  _text(b.get("format"), 20).upper(),
                                  _text(_size(b.get("size"))),
                                  _text(b.get("folder"), 80)) if x)
    text = b.get("text") or ""
    if text:
        body = _text(text)
        if b.get("text_cut"):
            # Обрывок посреди книги надо подписать: без пометы человек
            # решит, что так и было в исходнике.
            body += ('<div class="dim">Дальше текст не поместился в бюджет '
                     'копии — дочитать можно на ПК.</div>')
    elif b.get("text_note"):
        body = f'<div class="dim">{_text(b["text_note"])}</div>'
    elif b.get("text_error"):
        # Совершенно другой случай: файл на ПК есть, но сервер его не разобрал,
        # и совет «увеличьте бюджет» тут только увёл бы человека не туда.
        body = ('<div class="dim">Сервер не смог прочитать текст этой книги — '
                'в копии она осталась только списком.</div>')
    else:
        body = ('<div class="dim">Полный текст не вошёл в бюджет копии: увеличьте «Бюджет '
                'на устройство» в админке или берите копию реже, чтобы он был свободен.</div>')
    ann = _text(b.get("annotation"), 1500)
    ann = f'<div class="s">{ann}</div>' if ann else ""
    hay = _hay([b.get("title") or b.get("name"), b.get("author"), text[:300]], 600)
    return (f'<article data-hay="{hay}"><h2 class="t">{title}</h2>'
            f'<div class="s">{meta}</div>{ann}<div class="b">{body}</div></article>')


def build_books(books, read_text=None, budget_bytes=20 * 1048576,
                made_at=None, valid_days=0, title="Книги"):
    """Книги: список всегда, полные тексты — пока копия влезает в бюджет.

    Текст читаем по порядку и останавливаемся на выбранном бюджете: иначе
    пришлось бы разобрать всю библиотеку, чтобы потом выбросить её часть.

    read_text(book) -> (title, author, annotation, text) | None. Кто читает —
    решает вызывающий: здесь нет импорта модулей приложения.
    """
    items, kept = [], []
    used = 0
    spent = False        # бюджет кончился: следующие тома читать уже незачем
    for b in books:
        if not isinstance(b, dict):
            continue
        rec = dict(b)
        if read_text is not None and not spent:
            try:
                got = read_text(b)
            except SkipText as s:
                swallow.note('offline_export.build_books:L312', s)
                items.append(_book_item({**rec, "text_note": str(s)}))
                kept.append({"id": rec.get("rel_path") or rec.get("path"),
                             "title": str(rec.get("title") or rec.get("name") or "")[:300],
                             "has_text": False})
                if len(items) >= MAX_LIMIT:
                    break
                continue
            except Exception:
                swallow.note('offline_export.build_books:L321')
                got = None      # один битый fb2 не должен обрывать всю копию
            text = ""
            body_bytes = 0
            whole = ""
            if got:
                title, author, annotation, whole = got
                whole = str(whole or "")
                room = budget_bytes - used
                clipped = _utf8_clip(whole[:BOOK_MAX_CHARS], room)
                # В копию ложится экранированный текст, а он длиннее сырого
                # (& -> &amp;). Считаем и срезаем по фактическим байтам файла,
                # иначе том ровно на остаток вытеснил бы копию за бюджет.
                # Срез делаем по сырому тексту, а экранируем уже его целиком,
                # поэтому оборванную сущность на срезе получить нельзя.
                body_bytes = _utf8_len(_text(clipped))
                while clipped and body_bytes > room:
                    clipped = _utf8_clip(clipped,
                                         int(_utf8_len(clipped) * room / body_bytes))
                    body_bytes = _utf8_len(_text(clipped))
                # Сравнение с исходной длиной, а не с обрезанной лимитом:
                # том может быть укорочен и лимитом на книгу, и бюджетом —
                # читателю важно знать в обоих случаях.
                rec["text_cut"] = _utf8_len(clipped) < _utf8_len(whole)
                text = clipped
                rec["title"] = rec.get("title") or title
                rec["author"] = author
                rec["annotation"] = annotation
            rec["text"] = text
            # Три разных случая пустого текста, и совет в копии у них разный:
            # остаток бюджета меньше одного символа ( spent, «не вошёл»),
            # разборка вернула пустоту или упала («не смог прочитать»), и
            # нормальный том, который целиком влез.
            if text:
                used += body_bytes
                spent = used >= budget_bytes
            elif whole:
                spent = True
            else:
                rec["text_error"] = True
        items.append(_book_item(rec))
        kept.append({"id": rec.get("rel_path") or rec.get("path"),
                     "title": str(rec.get("title") or rec.get("name") or "")[:300],
                     "has_text": bool(rec.get("text"))})
        if len(items) >= MAX_LIMIT:
            break
    return _render(title, items, kept, made_at, valid_days)


def _conv_item(c):
    name = _text(c.get("title") or "(диалог)", 200)
    members = c.get("members") or []
    who = ", ".join(str(m.get("display_name") or m.get("profile_id") or "")
                    for m in members[:8] if isinstance(m, dict))
    msgs_src = [m for m in (c.get("messages") or [])
                if isinstance(m, dict)][:MSG_PER_CONV]
    msgs = []
    for m in msgs_src:
        head = " · ".join(x for x in (_text(m.get("sender"), 80),
                                      _text(_when(m.get("created_at")))) if x)
        # Медиа в копию не едет: файлы живут на сервере и без него не отдаются.
        att = '<div class="dim">Вложение — только на ПК.</div>' if m.get("attachments") else ""
        msgs.append(f'<div class="msg"><div class="s">{head}</div>'
                    f'{_text(m.get("content"), MSG_MAX_CHARS)}{att}</div>')
    hay = _hay([c.get("title"), who,
                " ".join(str(m.get("content") or "")[:80] for m in msgs_src[:20])], 1500)
    return (f'<article data-hay="{hay}"><h2 class="t">{name}</h2>'
            f'<div class="s">{_text(who, 200)}</div>{"".join(msgs)}</article>')


def build_messages(conversations, made_at=None, valid_days=0, title="Сообщения"):
    """Диаологи с последними сообщениями."""
    items, kept = [], []
    for c in conversations:
        if not isinstance(c, dict):
            continue
        items.append(_conv_item(c))
        kept.append({"id": c.get("id"), "title": str(c.get("title") or "")[:200],
                     "messages": len(c.get("messages") or [])})
        if len(items) >= CONV_LIMIT:
            break
    return _render(title, items, kept, made_at, valid_days)
