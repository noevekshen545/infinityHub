"""Пользовательские вики: страницы Markdown в data-каталоге профиля.

Каждая вики лежит целиком в профиле автора. Приватность — изменяемый флаг
в wiki.json, поэтому снятие/установка флага не переносит данные. Чтение
публичной вики разрешено всем, запись — только владельцу; проверки делает
этот модуль, а не интерфейс.
"""

import html
import io
import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from html.parser import HTMLParser
from urllib.parse import quote
import swallow

logger = logging.getLogger("infinity.userwiki")

_WIKI_ID_RE = re.compile(r"^w[0-9a-f]{12}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]")

_VISIBILITIES = ("private", "public")
_LOCK = threading.RLock()
_WIKI_LOCKS = {}
_OWNER_CACHE = {}
_OWNER_TTL = 30.0

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


class WikiNotFound(FileNotFoundError):
    pass


class WikiAccessDenied(PermissionError):
    pass


def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = "".join(_TRANSLIT.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:60].strip("-") or "page")


def _data_dir(profile_id: str) -> str:
    import profiles_module as PM
    base = PM.get_profile_data_dir(profile_id)
    if not base:
        raise WikiAccessDenied("Профиль не найден")
    return os.path.join(base, "userwikis")


def _wiki_dir(profile_id: str, wiki_id: str) -> str:
    if not _WIKI_ID_RE.match(wiki_id or ""):
        raise WikiNotFound("Некорректный id вики")
    return os.path.join(_data_dir(profile_id), wiki_id)


def _lock_for(path: str):
    with _LOCK:
        lock = _WIKI_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _WIKI_LOCKS[path] = lock
        return lock


def _atomic_write(path: str, data: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            swallow.note('userwiki_store._atomic_write:L92')
            pass
    os.replace(tmp, path)


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        swallow.note('userwiki_store._read_json:L102')
        return default


def _write_json(path: str, value):
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def _now() -> float:
    return time.time()


def _meta_path(wiki_dir: str) -> str:
    return os.path.join(wiki_dir, "wiki.json")


def _pages_path(wiki_dir: str) -> str:
    return os.path.join(wiki_dir, "pages.json")


def _load_meta(wiki_dir: str) -> dict:
    meta = _read_json(_meta_path(wiki_dir))
    if not isinstance(meta, dict):
        raise WikiNotFound("Вики не найдена")
    return meta


def _save_meta(wiki_dir: str, meta: dict):
    meta["updated"] = _now()
    _write_json(_meta_path(wiki_dir), meta)


def _load_pages(wiki_dir: str) -> list:
    data = _read_json(_pages_path(wiki_dir), default=None)
    if isinstance(data, dict) and isinstance(data.get("pages"), list):
        return data["pages"]
    pages = []
    pdir = os.path.join(wiki_dir, "pages")
    if os.path.isdir(pdir):
        for name in sorted(os.listdir(pdir)):
            if not name.endswith(".md"):
                continue
            slug = name[:-3]
            try:
                text = open(os.path.join(pdir, name), encoding="utf-8").read()
                st = os.stat(os.path.join(pdir, name))
            except OSError:
                swallow.note('userwiki_store._load_pages:L149')
                continue
            title = slug
            for line in text.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip() or slug
                    break
            pages.append({"slug": slug, "title": title, "tags": [],
                          "created": st.st_ctime, "updated": st.st_mtime,
                          "version": 1})
    if pages:
        _write_json(_pages_path(wiki_dir), {"pages": pages})
    return pages


def _save_pages(wiki_dir: str, pages: list):
    _write_json(_pages_path(wiki_dir), {"pages": pages})


def _page_path(wiki_dir: str, slug: str) -> str:
    if not _SLUG_RE.match(slug or ""):
        raise WikiNotFound("Некорректный адрес страницы")
    return os.path.join(wiki_dir, "pages", slug + ".md")


def _find_page(pages: list, slug: str):
    for p in pages:
        if p.get("slug") == slug:
            return p
    return None


def _unique_slug(pages: list, base: str) -> str:
    base = slugify(base)
    taken = {p.get("slug") for p in pages}
    if base not in taken:
        return base
    for i in range(2, 1000):
        cand = f"{base}-{i}"
        if cand not in taken:
            return cand
    return f"{base}-{uuid.uuid4().hex[:6]}"


def _invalidate_owner_cache():
    with _LOCK:
        _OWNER_CACHE.clear()


def _find_wiki(wiki_id: str, prefer_profile: str = ""):
    """Найти каталог вики по глобально уникальному id."""
    now = _now()
    with _LOCK:
        hit = _OWNER_CACHE.get(wiki_id)
        if hit and now - hit[2] < _OWNER_TTL and os.path.isdir(hit[0]):
            return hit[0], hit[1]

    import profiles_module as PM
    order = []
    if prefer_profile:
        order.append(prefer_profile)
    try:
        order += [p["id"] for p in PM.list_profiles() if p["id"] != prefer_profile]
    except Exception:
        swallow.note('userwiki_store._find_wiki:L213')
        pass

    for pid in order:
        try:
            d = _wiki_dir(pid, wiki_id)
        except Exception:
            swallow.note('userwiki_store._find_wiki:L220')
            continue
        if os.path.isfile(_meta_path(d)):
            with _LOCK:
                _OWNER_CACHE[wiki_id] = (d, pid, now)
            return d, pid
    raise WikiNotFound("Вики не найдена")


def _readable(profile_id: str, wiki_id: str):
    wiki_dir, owner = _find_wiki(wiki_id, profile_id)
    meta = _load_meta(wiki_dir)
    if owner != profile_id and meta.get("visibility") != "public":
        raise WikiAccessDenied("Вики доступна только владельцу")
    return wiki_dir, meta, owner


def _writable(profile_id: str, wiki_id: str):
    wiki_dir, owner = _find_wiki(wiki_id, profile_id)
    if owner != profile_id:
        raise WikiAccessDenied("Изменять вики может только владелец")
    return wiki_dir, _load_meta(wiki_dir), owner


def _public_meta(meta: dict, owner: str, profile_id: str,
                 pages_count=None) -> dict:
    pub = {k: meta.get(k) for k in (
        "id", "title", "description", "visibility", "search_index",
        "vector_index", "vector_scope", "created", "updated")}
    pub["owner"] = owner
    pub["can_edit"] = owner == profile_id
    if pages_count is not None:
        pub["pages_count"] = pages_count
    return pub


def list_visible(profile_id: str) -> list:
    """Свои вики + публичные вики других профилей."""
    import profiles_module as PM
    seen = set()
    out = []
    profiles = []
    if profile_id:
        profiles.append(profile_id)
    try:
        profiles += [p["id"] for p in PM.list_profiles() if p["id"] != profile_id]
    except Exception:
        swallow.note('userwiki_store.list_visible:L267')
        pass
    for pid in profiles:
        try:
            root = _data_dir(pid)
        except Exception:
            swallow.note('userwiki_store.list_visible:L273')
            continue
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not _WIKI_ID_RE.match(name) or name in seen:
                continue
            wiki_dir = os.path.join(root, name)
            meta = _read_json(_meta_path(wiki_dir))
            if not isinstance(meta, dict):
                continue
            if pid != profile_id and meta.get("visibility") != "public":
                continue
            seen.add(name)
            out.append(_public_meta(meta, pid, profile_id,
                                    len(_load_pages(wiki_dir))))
    out.sort(key=lambda m: (m.get("updated") or 0), reverse=True)
    return out


def create_wiki(profile_id: str, title: str, description: str = "",
                visibility: str = "private", search_index: bool = False,
                vector_index: bool = False) -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("Название вики не может быть пустым")
    if len(title) > 200:
        title = title[:200]
    visibility = "public" if str(visibility) == "public" else "private"

    wiki_id = "w" + uuid.uuid4().hex[:12]
    root = _data_dir(profile_id)
    wiki_dir = os.path.join(root, wiki_id)
    os.makedirs(os.path.join(wiki_dir, "pages"), exist_ok=True)
    os.makedirs(os.path.join(wiki_dir, "versions"), exist_ok=True)
    os.makedirs(os.path.join(wiki_dir, "assets"), exist_ok=True)

    now = _now()
    meta = {
        "schema": 1,
        "id": wiki_id,
        "owner": profile_id,
        "title": title,
        "description": (description or "").strip()[:2000],
        "visibility": visibility,
        "search_index": bool(search_index),
        "vector_index": bool(vector_index),
        "vector_scope": "shared" if visibility == "public" else "personal",
        "created": now,
        "updated": now,
    }
    _write_json(_meta_path(wiki_dir), meta)
    home = {"slug": "home", "title": "Главная", "tags": [],
            "created": now, "updated": now, "version": 1}
    _write_json(_pages_path(wiki_dir), {"pages": [home]})
    body = (meta["description"] or f"Добро пожаловать в «{title}».") + \
           "\n\nСоздайте первую страницу кнопкой «Новая страница»."
    _atomic_write(_page_path(wiki_dir, "home"), body)
    _invalidate_owner_cache()
    return _public_meta(meta, profile_id, profile_id)


def update_wiki(profile_id: str, wiki_id: str, patch: dict) -> dict:
    wiki_dir, meta, _owner = _writable(profile_id, wiki_id)
    with _lock_for(wiki_dir):
        for key in ("title", "description"):
            if key in patch:
                value = str(patch.get(key) or "").strip()
                if key == "title" and not value:
                    raise ValueError("Название вики не может быть пустым")
                meta[key] = value[:200 if key == "title" else 2000]
        if "visibility" in patch:
            vis = "public" if str(patch.get("visibility")) == "public" else "private"
            meta["visibility"] = vis
            meta["vector_scope"] = "shared" if vis == "public" else "personal"
        for key in ("search_index", "vector_index"):
            if key in patch:
                meta[key] = bool(patch.get(key))
        _save_meta(wiki_dir, meta)
    _invalidate_owner_cache()
    return _public_meta(meta, profile_id, profile_id)


def delete_wiki(profile_id: str, wiki_id: str):
    wiki_dir, _meta, _owner = _writable(profile_id, wiki_id)
    trash = os.path.join(_data_dir(profile_id), "trash")
    os.makedirs(trash, exist_ok=True)
    dest = os.path.join(trash, f"{wiki_id}-{int(_now())}")
    shutil.move(wiki_dir, dest)
    _invalidate_owner_cache()


def get_wiki(profile_id: str, wiki_id: str) -> dict:
    wiki_dir, meta, owner = _readable(profile_id, wiki_id)
    return _public_meta(meta, owner, profile_id, len(_load_pages(wiki_dir)))


def list_pages(profile_id: str, wiki_id: str) -> list:
    wiki_dir, _meta, _owner = _readable(profile_id, wiki_id)
    return sorted(_load_pages(wiki_dir),
                  key=lambda p: (p.get("updated") or 0), reverse=True)


def get_page(profile_id: str, wiki_id: str, slug: str) -> dict:
    wiki_dir, meta, owner = _readable(profile_id, wiki_id)
    pages = _load_pages(wiki_dir)
    page = _find_page(pages, slug)
    if page is None:
        raise WikiNotFound("Страница не найдена")
    try:
        content = open(_page_path(wiki_dir, slug), encoding="utf-8").read()
    except OSError:
        raise WikiNotFound("Страница не найдена")
    return {"wiki_id": meta["id"], "wiki_title": meta.get("title", ""),
            "owner": owner, "can_edit": owner == profile_id,
            "visibility": meta.get("visibility") or "private",
            "vector_scope": meta.get("vector_scope") or "personal",
            "page": dict(page), "content": content,
            "html": render_markdown(wiki_dir, meta, content)}


def preview(profile_id: str, wiki_id: str, text: str) -> str:
    """HTML для живого предпросмотра без записи страницы."""
    wiki_dir, meta, _owner = _readable(profile_id, wiki_id)
    return render_markdown(wiki_dir, meta, text or "")


def save_page(profile_id: str, wiki_id: str, slug: str = "", title: str = "",
              content: str = "", tags=None) -> dict:
    wiki_dir, meta, _owner = _writable(profile_id, wiki_id)
    with _lock_for(wiki_dir):
        pages = _load_pages(wiki_dir)
        page = _find_page(pages, slug) if slug else None
        now = _now()
        if page is None:
            title = (title or "").strip() or "Без названия"
            slug = _unique_slug(pages, slug or title)
            page = {"slug": slug, "title": title[:200], "tags": [],
                    "created": now, "created_ts": now, "version": 0}
            pages.append(page)
        else:
            if title and title.strip() and title.strip() != page.get("title"):
                page["title"] = title.strip()[:200]
        content = (content or "").replace("\r\n", "\n").replace("\r", "\n")
        old_path = _page_path(wiki_dir, page["slug"])
        version = int(page.get("version") or 0)
        if os.path.isfile(old_path) and version > 0:
            vdir = os.path.join(wiki_dir, "versions", page["slug"])
            os.makedirs(vdir, exist_ok=True)
            try:
                shutil.copy2(old_path, os.path.join(vdir, f"{version}.md"))
            except OSError:
                swallow.note('userwiki_store.save_page:L425')
                pass
        page["version"] = version + 1
        page["updated"] = now
        page["updated_ts"] = now
        if tags is not None:
            page["tags"] = [str(t)[:40] for t in tags][:50]
        _atomic_write(old_path, content)
        _save_pages(wiki_dir, pages)
    return {"page": dict(page), "content": content,
            "html": render_markdown(wiki_dir, meta, content)}


def delete_page(profile_id: str, wiki_id: str, slug: str):
    wiki_dir, _meta, _owner = _writable(profile_id, wiki_id)
    with _lock_for(wiki_dir):
        pages = _load_pages(wiki_dir)
        page = _find_page(pages, slug)
        if page is None:
            raise WikiNotFound("Страница не найдена")
        src = _page_path(wiki_dir, slug)
        trash = os.path.join(wiki_dir, "trash")
        os.makedirs(trash, exist_ok=True)
        if os.path.isfile(src):
            shutil.move(src, os.path.join(trash, f"{slug}-{int(_now())}.md"))
        pages = [p for p in pages if p.get("slug") != slug]
        _save_pages(wiki_dir, pages)


def rename_page(profile_id: str, wiki_id: str, slug: str, new_title: str) -> dict:
    wiki_dir, meta, _owner = _writable(profile_id, wiki_id)
    with _lock_for(wiki_dir):
        pages = _load_pages(wiki_dir)
        page = _find_page(pages, slug)
        if page is None:
            raise WikiNotFound("Страница не найдена")
        old_title = page.get("title") or slug
        new_title = (new_title or "").strip()
        if not new_title:
            raise ValueError("Название страницы не может быть пустым")
        new_slug = _unique_slug([p for p in pages if p is not page], new_title)
        src = _page_path(wiki_dir, slug)
        dst = _page_path(wiki_dir, new_slug)
        if os.path.isfile(src):
            os.replace(src, dst)
        page["slug"] = new_slug
        page["title"] = new_title[:200]
        page["updated"] = _now()
        _save_pages(wiki_dir, pages)
        # Обновляем ссылки на старое имя во всех страницах.
        for p in pages:
            if p.get("slug") == new_slug:
                continue
            path = _page_path(wiki_dir, p["slug"])
            try:
                text = open(path, encoding="utf-8").read()
            except OSError:
                swallow.note('userwiki_store.rename_page:L482')
                continue
            updated = re.sub(r"\[\[" + re.escape(old_title) + r"(\|[^\]]+)?\]\]",
                             lambda m: "[[" + new_title + (m.group(1) or "") + "]]",
                             text, flags=re.IGNORECASE)
            if updated != text:
                _atomic_write(path, updated)
    return {"page": dict(page), "slug": new_slug}


def page_history(profile_id: str, wiki_id: str, slug: str) -> list:
    wiki_dir, _meta, _owner = _readable(profile_id, wiki_id)
    pages = _load_pages(wiki_dir)
    page = _find_page(pages, slug)
    if page is None:
        raise WikiNotFound("Страница не найдена")
    out = []
    vdir = os.path.join(wiki_dir, "versions", slug)
    if os.path.isdir(vdir):
        for name in os.listdir(vdir):
            if name.endswith(".md") and name[:-3].isdigit():
                st = os.stat(os.path.join(vdir, name))
                out.append({"version": int(name[:-3]), "ts": st.st_mtime,
                            "current": False})
    out.append({"version": int(page.get("version") or 0),
                "ts": page.get("updated") or 0, "current": True})
    out.sort(key=lambda v: v["version"], reverse=True)
    return out


def read_version(profile_id: str, wiki_id: str, slug: str, version: int) -> str:
    wiki_dir, _meta, _owner = _readable(profile_id, wiki_id)
    path = os.path.join(wiki_dir, "versions", slug, f"{int(version)}.md")
    try:
        return open(path, encoding="utf-8").read()
    except OSError:
        raise WikiNotFound("Версия не найдена")


def restore_version(profile_id: str, wiki_id: str, slug: str, version: int) -> dict:
    text = read_version(profile_id, wiki_id, slug, version)
    wiki_dir, _meta, _owner = _writable(profile_id, wiki_id)
    pages = _load_pages(wiki_dir)
    page = _find_page(pages, slug)
    return save_page(profile_id, wiki_id, slug,
                     title=(page or {}).get("title", slug), content=text)


def iter_search_pages(profile_id: str):
    """Плоский список страниц всех видимых профилю вики (сырой Markdown).

    Видимость определяет list_visible: свои вики целиком, чужие — только
    публичные. Текст читаем напрямую из pages/, без рендера Markdown.
    """
    for meta in list_visible(profile_id):
        try:
            wiki_dir = _wiki_dir(meta["owner"], meta["id"])
        except Exception:
            swallow.note('userwiki_store.iter_search_pages:L540')
            continue
        for page in _load_pages(wiki_dir):
            try:
                text = open(_page_path(wiki_dir, page["slug"]),
                            encoding="utf-8").read()
            except OSError:
                swallow.note('userwiki_store.iter_search_pages:L547')
                continue
            yield {
                "wiki_id": meta["id"],
                "wiki_title": meta.get("title") or meta["id"],
                "owner": meta.get("owner") or "",
                "visibility": meta.get("visibility") or "private",
                "slug": page["slug"],
                "title": page.get("title") or page["slug"],
                "content": text,
                "updated": page.get("updated") or 0,
            }


def search_pages(profile_id: str, wiki_id: str, query: str, limit: int = 50) -> list:
    wiki_dir, _meta, _owner = _readable(profile_id, wiki_id)
    words = [w for w in re.findall(r"[a-zа-яё0-9-]+", (query or "").lower())
             if len(w) > 1]
    if not words:
        return []
    out = []
    for page in _load_pages(wiki_dir):
        try:
            text = open(_page_path(wiki_dir, page["slug"]), encoding="utf-8").read()
        except OSError:
            swallow.note('userwiki_store.search_pages:L572')
            continue
        title_l = (page.get("title") or "").lower()
        body_l = text.lower()
        score = sum(30 for w in words if w in title_l)
        score += sum(10 for w in words if w in body_l)
        if not score:
            continue
        pos = body_l.find(words[0])
        start = max(0, pos - 60)
        preview = re.sub(r"\s+", " ", text[start:start + 220]).strip()
        out.append({"path": page["slug"], "slug": page["slug"],
                    "title": page.get("title") or page["slug"],
                    "preview": preview, "score": score})
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


def backlinks(profile_id: str, wiki_id: str, slug: str) -> list:
    wiki_dir, _meta, _owner = _readable(profile_id, wiki_id)
    pages = _load_pages(wiki_dir)
    target = _find_page(pages, slug)
    if target is None:
        return []
    names = {slug.lower(), (target.get("title") or "").lower()}
    out = []
    for page in pages:
        if page.get("slug") == slug:
            continue
        try:
            text = open(_page_path(wiki_dir, page["slug"]), encoding="utf-8").read()
        except OSError:
            swallow.note('userwiki_store.backlinks:L604')
            continue
        for m in _WIKILINK_RE.finditer(text):
            if m.group(1).strip().lower() in names:
                out.append({"slug": page["slug"],
                            "title": page.get("title") or page["slug"]})
                break
    return out


def graph(profile_id: str, wiki_id: str) -> dict:
    wiki_dir, _meta, _owner = _readable(profile_id, wiki_id)
    pages = _load_pages(wiki_dir)
    by_name = {}
    for p in pages:
        by_name[p["slug"].lower()] = p["slug"]
        by_name[(p.get("title") or "").lower()] = p["slug"]
    nodes = [{"id": p["slug"], "title": p.get("title") or p["slug"]}
             for p in pages]
    edges = []
    for p in pages:
        try:
            text = open(_page_path(wiki_dir, p["slug"]), encoding="utf-8").read()
        except OSError:
            swallow.note('userwiki_store.graph:L628')
            continue
        for m in _WIKILINK_RE.finditer(text):
            dst = by_name.get(m.group(1).strip().lower())
            if dst and dst != p["slug"]:
                edges.append({"source": p["slug"], "target": dst})
    return {"nodes": nodes, "edges": edges}


def _link_map(wiki_dir: str) -> dict:
    out = {}
    for p in _load_pages(wiki_dir):
        out[p["slug"].lower()] = p["slug"]
        out[(p.get("title") or "").lower()] = p["slug"]
    return out


def _rewrite_assets(body: str, wiki_id: str) -> str:
    """Относительные картинки/ссылки ведут в /asset, иначе санитайзер их режет."""
    base = f"/api/userwiki/{quote(wiki_id)}/asset/"

    def _src(m):
        value = html.unescape(m.group(2)).strip()
        if not value or value.startswith(("/", "#", "?")) or \
                re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I):
            return m.group(0)
        value = value.lstrip("./")
        if value.startswith("assets/"):
            value = value[7:]
        return m.group(1) + base + quote(value) + m.group(3)

    body = re.sub(r'(<(?:img|audio|video|source)\b[^>]*?\bsrc=")([^"]*)(")',
                  _src, body, flags=re.I)
    body = re.sub(r'(<video\b[^>]*?\bposter=")([^"]*)(")',
                  _src, body, flags=re.I)
    body = re.sub(r'(<a\b[^>]*?\bhref=")(assets/[^"]*)(")',
                  lambda m: m.group(1) + base + quote(html.unescape(m.group(2))[7:]) + m.group(3),
                  body, flags=re.I)
    return body


def render_markdown(wiki_dir: str, meta: dict, text: str) -> str:
    """Markdown → безопасный HTML. Сырой HTML не исполняется, кроме
    <audio>/<video> с вложениями assets/ (они проходят санитайзер)."""
    links = _link_map(wiki_dir)
    wiki_id = meta.get("id", "")
    # Сырой HTML по умолчанию экранируется, поэтому медиа-блоки временно
    # заменяем токенами, а после Markdown возвращаем и пропускаем через
    # санитайзер вместе с остальным телом.
    media = {}

    def _stash(m):
        token = "@@UWMEDIA%d@@" % len(media)
        media[token] = m.group(0)
        return token

    text = re.sub(r'<(audio|video)\b[^>]*>.*?</\1>', _stash,
                  text or "", flags=re.I | re.S)
    parts = []
    pos = 0
    for m in _WIKILINK_RE.finditer(text):
        parts.append(html.escape(text[pos:m.start()], quote=False))
        target = m.group(1).strip()
        alias = (m.group(2) or target).strip()
        slug = links.get(target.lower())
        if slug:
            url = f"/zim/view?wiki={quote(wiki_id)}&path={quote(slug)}"
            parts.append(f"[{html.escape(alias, quote=False)}]({url})")
        else:
            parts.append('<span class="wikilink-missing">' +
                         html.escape(alias, quote=False) + "</span>")
        pos = m.end()
    parts.append(html.escape(text[pos:], quote=False))
    escaped = "".join(parts)
    try:
        import markdown
        body = markdown.markdown(
            escaped,
            extensions=["extra", "sane_lists", "toc"],
            output_format="html5",
        )
    except Exception:
        swallow.note('userwiki_store.render_markdown:L694')
        body = "<p>" + escaped.replace("\n\n", "</p><p>") + "</p>"
    for token, tag in media.items():
        body = body.replace(token, tag)
    body = _rewrite_assets(body, wiki_id)
    return _sanitize_html(body)


_ALLOWED_TAGS = {
    "a", "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "code", "em", "strong",
    "b", "i", "del", "table", "thead", "tbody", "tr", "th", "td",
    "img", "span", "div", "audio", "video", "source",
}
_VOID_TAGS = {"br", "hr", "img", "source"}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "audio": {"src", "controls", "preload", "loop"},
    "video": {"src", "controls", "poster", "preload", "loop"},
    "source": {"src", "type"},
    "code": {"class"},
    "th": {"align", "colspan", "rowspan"},
    "td": {"align", "colspan", "rowspan"},
    "span": {"class"},
    "div": {"class"},
}


def _safe_url(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    low = v.lower()
    if low.startswith(("javascript:", "data:", "vbscript:")):
        return False
    if low.startswith(("http://", "https://", "mailto:")):
        return True
    if v.startswith(("/", "#", "?")):
        return True
    return False


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            return
        safe = []
        for key, value in attrs:
            key = key.lower()
            if key not in _ALLOWED_ATTRS.get(tag, set()):
                continue
            if key in ("href", "src") and not _safe_url(value):
                continue
            if tag in ("span", "div") and key == "class":
                if value not in ("wikilink-missing",):
                    continue
            safe.append((key, value))
        attr_text = "".join(
            f' {k}="{html.escape(str(v or ""), quote=True)}"' for k, v in safe)
        self.out.append(f"<{tag}{attr_text}>")
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID_TAGS or tag not in _ALLOWED_TAGS:
            return
        if tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))

    def close(self):
        super().close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")


def _sanitize_html(value: str) -> str:
    parser = _Sanitizer()
    try:
        parser.feed(value or "")
        parser.close()
    except Exception:
        swallow.note('userwiki_store._sanitize_html:L791')
        return html.escape(value or "", quote=False)
    return "".join(parser.out)


def _safe_asset_name(name: str) -> str:
    """Имя файла в assets/: латиница, цифры, точка, дефис, подчёркивание.

    Кириллицу транслитерируем, пробелы сводим к дефису: тогда имя можно
    подставить в Markdown-ссылку без процентного кодирования.
    """
    name = os.path.basename((name or "").replace("\\", "/"))
    stem, ext = os.path.splitext(name)
    stem = "".join(_TRANSLIT.get(ch, ch) for ch in stem.lower())
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(" .-_")
    ext = re.sub(r"[^A-Za-z0-9.]+", "", ext)[:12]
    return (stem[:100].strip(" .-_") or "file") + ext


def asset_file(profile_id: str, wiki_id: str, name: str):
    wiki_dir, _meta, _owner = _readable(profile_id, wiki_id)
    base = os.path.abspath(os.path.join(wiki_dir, "assets"))
    # Имена, сохранённые до транслитерации, могут не совпасть с новым
    # санитайзером — пробуем исходный basename вторым кандидатом.
    raw = os.path.basename((name or "").replace("\\", "/"))
    for cand in (_safe_asset_name(name), raw):
        if not cand:
            continue
        path = os.path.abspath(os.path.join(base, cand))
        if os.path.commonpath([base, path]) != base:
            raise WikiAccessDenied("Некорректный путь файла")
        if os.path.isfile(path):
            return path, (mimetypes.guess_type(path)[0] or "application/octet-stream")
    raise WikiNotFound("Файл не найден")


def save_asset(profile_id: str, wiki_id: str, filename: str, data: bytes) -> dict:
    wiki_dir, _meta, _owner = _writable(profile_id, wiki_id)
    base = os.path.join(wiki_dir, "assets")
    os.makedirs(base, exist_ok=True)
    name = _safe_asset_name(filename)
    stem, ext = os.path.splitext(name)
    path = os.path.join(base, name)
    i = 2
    while os.path.exists(path):
        name = f"{stem}-{i}{ext}"
        path = os.path.join(base, name)
        i += 1
    with open(path, "wb") as f:
        f.write(data)
    return {"name": name,
            "url": f"/api/userwiki/{wiki_id}/asset/{quote(name)}",
            "size": len(data),
            "mimetype": mimetypes.guess_type(path)[0] or "application/octet-stream"}


def export_wiki(profile_id: str, wiki_id: str) -> bytes:
    wiki_dir, meta, _owner = _readable(profile_id, wiki_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(wiki_dir):
            for name in files:
                path = os.path.join(root, name)
                rel = os.path.relpath(path, wiki_dir).replace("\\", "/")
                if rel.startswith("versions/") or "/trash/" in "/" + rel:
                    continue
                zf.write(path, "userwiki/" + rel)
    return buf.getvalue()


def import_wiki(profile_id: str, data: bytes, filename: str = "") -> dict:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        raise ValueError("Это не ZIP-архив вики")
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not any(n.endswith("wiki.json") for n in names):
        raise ValueError("В архиве нет wiki.json")
    base = "userwiki/"
    meta_name = next(n for n in names if n.endswith("wiki.json"))
    root = meta_name[: -len("wiki.json")]
    if not root.startswith(base):
        base = root
    meta = json.loads(zf.read(meta_name).decode("utf-8", "replace"))
    title = str(meta.get("title") or filename or "Импортированная вики")[:200]
    created = create_wiki(profile_id, title, str(meta.get("description") or ""),
                          "private", False, False)
    wiki_dir = _wiki_dir(profile_id, created["id"])
    pages = []
    now = _now()
    for name in names:
        if not name.endswith(".md") or "/pages/" not in name:
            continue
        rel = name[len(root):] if name.startswith(root) else name
        if not rel.startswith("pages/"):
            continue
        slug = slugify(os.path.basename(rel)[:-3])
        if not slug or _find_page(pages, slug):
            continue
        text = zf.read(name).decode("utf-8", "replace")
        title_line = slug
        for line in text.splitlines():
            if line.startswith("# "):
                title_line = line[2:].strip() or slug
                text = text.replace(line, "", 1).lstrip("\n")
                break
        page = {"slug": slug, "title": title_line[:200], "tags": [],
                "created": now, "updated": now, "version": 1}
        _atomic_write(_page_path(wiki_dir, slug), text)
        pages.append(page)
    if not pages:
        pages = [{"slug": "home", "title": "Главная", "tags": [],
                  "created": now, "updated": now, "version": 1}]
        _atomic_write(_page_path(wiki_dir, "home"), "Импортированная вики пуста.")
    _save_pages(wiki_dir, pages)
    return get_wiki(profile_id, created["id"])
