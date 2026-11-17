#!/usr/bin/env python3
"""
Smart Search Engine v5 - Интеллектуальный поисковый движок

Концепция:
1. LLM генерирует поисковые запросы на RU и EN
2. Быстрый поиск по всем источникам (LIMITS: ZIM=3, other=5)
3. LLM оценивает релевантность и отбирает нужные источники
4. LLM формирует итоговый ответ на основе релевантных источников
"""

import os
import sys
import json
import re
import time
import logging
import threading
import asyncio
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from modules import search_unified
import swallow

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
logger = logging.getLogger("search_v5")

try:
    import server
    from server import (
        search_articles, ZIM_AVAILABLE, ZIM_DIRS, NOTES_DIR, AI_DIR,
        BOOKS_DIR, CHAT_DIR, get_zim_archive
    )
    from agent_module import call_llm
except ImportError as e:
    logger.error(f"Import error: {e}")
    raise


LIMITS = {
    "zim": 15,  # Было 5, теперь 15 с каждого файла
    "book": 5,
}

# Получаем конфиг напрямую
def _get_config():
    """config.json — рядом с модулем и в UTF-8.

    Путь был относительным («config.json»), а open — без encoding. На
    windows-локали cp1251 чтение падало на кириллице в конфиге, except
    проглатывал UnicodeDecodeError, и модуль молча жил с BOOKS_DIR='':
    индекс книг не строился никогда, а поиск по книгам отдавал ноль.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[V5] config.json не прочитан ({path}): {e}")
        return {}

_CONFIG = _get_config()
BOOKS_DIR = _CONFIG.get("books_dir", "")


def _expand(p):
    """Раскрыть ${ROOT}/${APP}, ~ и переменные окружения в пути из конфига.

    search_v5 читает app/config.json сырым, а config_module разворачивает
    токены только на своём пути — из-за этого ZIM_DIRS = ['${ROOT}/data/zim']
    оставался несуществующим каталогом, и поиск по ZIM в v5 не работал никогда.
    """
    s = str(p)
    try:
        from config_module import _expand_path
        return _expand_path(s) or s
    except Exception:
        swallow.note('search_v5._expand:L79')
        root = os.environ.get("INFINITY_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        s = s.replace("${ROOT}", root).replace("${APP}", os.path.dirname(os.path.abspath(__file__)))
        return os.path.abspath(os.path.expanduser(os.path.expandvars(s)))


def _norm_list(raw):
    out, seen = [], set()
    for d in raw or []:
        if not d:
            continue
        d = _expand(d)
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(d)
    return out


def zim_dirs():
    """Каталоги ZIM: эффективный конфиг профиля + то, чем сервер пользуется сейчас."""
    raw = list(_CONFIG.get("zim_dirs") or [])
    try:
        import server as _srv
        raw += list(getattr(_srv, "ZIM_DIRS", None) or [])
    except Exception:
        swallow.note('search_v5.zim_dirs:L105')
        pass
    return _norm_list(raw)


ZIM_DIRS = zim_dirs()


def _effective_dirs_for(profile_id):
    """Каталоги книг, которые видит конкретный профиль (общедоступные + свои)."""
    try:
        import profiles_module as _PM
        cfg = _PM.get_effective_config(profile_id) or {}
        return _norm_list([cfg.get("books_dir") or ""] +
                          list(cfg.get("books_dirs") or []))
    except Exception:
        swallow.note('search_v5._effective_dirs_for:L118')
        return []


def books_dirs():
    """Все каталоги книг ВСЕХ профилей — из них строится общий индекс.

    Раньше список брался у активного профиля (server.BOOKS_DIRS). API-запрос
    приходит с cookie профиля, но глобальное состояние сервера переключает
    только навигация документом, поэтому у «неактивного» профиля его личные
    книги в индекс не попадали. Теперь индекс — объединение каталогов всех
    профилей, а видимость режется при чтении по каталогам профиля запроса
    (search_books / search_books_fast). Публичные каталоги входят в конфиг
    каждого профиля, так что дубликаты снимает _norm_list.
    """
    raw = list(_CONFIG.get("books_dirs") or []) + [BOOKS_DIR]
    try:
        import profiles_module as _PM
        seen_ids = set()
        ids = [_PM.get_default_profile_id(), _PM.get_active_id()]
        ids += [p.get("id") for p in (_PM.list_profiles() or [])
                if isinstance(p, dict)]
        for pid in ids:
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            raw += _effective_dirs_for(pid)
    except Exception:
        swallow.note('search_v5.books_dirs:L143')
        pass
    try:
        import server as _srv
        raw += list(getattr(_srv, "BOOKS_DIRS", None) or [])
        raw += [getattr(_srv, "BOOKS_DIR", "")]
    except Exception:
        swallow.note('search_v5.books_dirs:L151')
        pass
    return _norm_list(raw)


def _under_any_root(path, roots):
    """Лежит ли путь внутри одного из каталогов (Windows-регистр не важен)."""
    try:
        target = os.path.normcase(os.path.realpath(path))
    except Exception:
        swallow.note('search_v5._under_any_root:L161')
        target = os.path.normcase(str(path or ""))
    for root in roots or []:
        if not root:
            continue
        try:
            base = os.path.normcase(os.path.realpath(root)).rstrip(os.sep)
        except Exception:
            swallow.note('search_v5._under_any_root:L169')
            base = os.path.normcase(str(root)).rstrip(os.sep)
        if base and (target == base or target.startswith(base + os.sep)):
            return True
    return False
# Fallback: если data_dirs пустой - используем стандартный путь
DATA_DIRS = _CONFIG.get("data_dirs", []) or ["/home/infinity/pars/habr/output/"]

# Модель по умолчанию - маленькая lm_studio (не огромная!)
DEFAULT_MODEL = _CONFIG.get("lm_studio", {}).get("model", "gemma-4-e2b-it")

logger.info(f"[V5] ZIM_DIRS: {ZIM_DIRS}")
logger.info(f"[V5] BOOKS_DIR: {BOOKS_DIR}")
logger.info(f"[V5] DEFAULT_MODEL: {DEFAULT_MODEL}")

BOOK_INDEX_FILE = os.path.expanduser("~/.infinity/book_index.json")
# Подпись формата индекса. Меняется, когда меняется сам сканер (несколько
# каталогов, .zst, объединение каталогов всех профилей): без неё устаревший
# индекс считается актуальным, потому что mtime папок с тех пор не менялись.
BOOK_INDEX_VERSION = 3
_book_index_cache = None
_book_index_lock = threading.Lock()


def _get_book_index() -> List[Dict]:
    """Получить индекс книг (из кэша или с диска)."""
    global _book_index_cache
    
    if _book_index_cache is not None:
        return _book_index_cache
    
    with _book_index_lock:
        if _book_index_cache is not None:
            return _book_index_cache
            
        if os.path.exists(BOOK_INDEX_FILE):
            try:
                with open(BOOK_INDEX_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                    _book_index_cache = data.get("books", [])
                    return _book_index_cache
            except Exception:
                swallow.note('search_v5._get_book_index:L167')
                pass
    
    return []


_FB2_ENCODING_RE = re.compile(rb'encoding="([^"]+)"', re.I)
_FB2_TITLE_RE = re.compile(r"<book-title>(.*?)</book-title>", re.S)
_FB2_AUTHOR_RE = re.compile(r"<author>(.*?)</author>", re.S)
_FB2_FIRST_RE = re.compile(r"<first-name>(.*?)</first-name>", re.S)
_FB2_LAST_RE = re.compile(r"<last-name>(.*?)</last-name>", re.S)
BOOK_META_HEAD_BYTES = 8192


def _read_fb2_meta(fp: str) -> Tuple[str, str]:
    """Название и автор из шапки fb2.

    Файлы в библиотеке подписаны номерами (110119.fb2), поэтому заголовок из
    имени файла ничего не ищет: источник «книги» отдавал только номера. Шапка
    fb2 стоит в начале файла, 8 КБ хватает с запасом, а весь разбор тома не
    нужен. Кодировку берём из XML-декларации: в этой же библиотеке 7 томов в
    windows-1251, и молча декодировать их как UTF-8 нельзя.

    fp — логический путь (без .zst): распакованные тома читаются через
    compression_module, иначе из упакованной библиотеки не брался ни один
    заголовок.
    """
    head = b""
    try:
        import compression_module as _cz
        f = _cz.open_binary(fp)
        try:
            head = f.read(BOOK_META_HEAD_BYTES)
        finally:
            try:
                f.close()
            except Exception:
                swallow.note('search_v5._read_fb2_meta:L204')
                pass
    except Exception:
        swallow.note('search_v5._read_fb2_meta:L207')
        try:
            with open(fp, "rb") as f:
                head = f.read(BOOK_META_HEAD_BYTES)
        except OSError:
            swallow.note('search_v5._read_fb2_meta:L212')
            return "", ""
    if not head:
        return "", ""
    decl = _FB2_ENCODING_RE.search(head[:120])
    enc = (decl.group(1).decode("ascii", "ignore").strip().lower() if decl else "utf-8")
    if enc in ("utf8", "utf-8"):
        enc = "utf-8-sig"
    try:
        text = head.decode(enc, "ignore")
    except (LookupError, UnicodeDecodeError):
        swallow.note('search_v5._read_fb2_meta:L223')
        text = head.decode("utf-8", "ignore")
    tm = _FB2_TITLE_RE.search(text)
    title = tm.group(1).strip() if tm else ""
    author = ""
    am = _FB2_AUTHOR_RE.search(text)
    if am:
        seg = am.group(1)
        first = _FB2_FIRST_RE.search(seg)
        last = _FB2_LAST_RE.search(seg)
        author = " ".join(p.group(1).strip() for p in (first, last) if p and p.group(1).strip())
    return title, author


def _scan_books_for_index() -> List[Dict]:
    """Сканировать все каталоги книг и создать индекс."""
    books = []

    roots = [d for d in books_dirs() if d and os.path.isdir(d)]
    if not roots:
        return books

    logger.info(f"[V5] Scanning books directories: {roots}")
    try:
        import compression_module as cz
    except Exception:
        swallow.note('search_v5._scan_books_for_index:L249')
        cz = None
    seen_files = set()

    for books_root in roots:
        for root, dirs, files in os.walk(books_root):
            dirs.sort()
            # packing-модуль хранит книги как <имя>.<ext>.zst. Без учёта
            # суффикса из всей библиотеки в индекс попадал один незапакованный
            # том — источник «книги» в /search-all молчал.
            plain = {f for f in files if not f.lower().endswith(".zst")}
            for f in sorted(files):
                real_fp = os.path.join(root, f)
                logical = f
                if cz is not None and f.lower().endswith(cz.ZST_SUFFIX):
                    if not cz.is_zst_file(real_fp):
                        continue
                    logical = f[:-len(cz.ZST_SUFFIX)]
                    if logical in plain:
                        continue
                fp = os.path.join(root, logical)
                ext = os.path.splitext(logical)[1].lower()
                if ext in ['.fb2', '.epub', '.pdf', '.djvu', '.doc', '.docx', '.txt', '.rtf', '.mobi', '.azw3']:
                    # Один и тот же файл из двух каталогов (вложенный или
                    # симлинк) не должен плодить дубликаты в выдаче.
                    try:
                        key = os.path.realpath(real_fp)
                    except Exception:
                        swallow.note('search_v5._scan_books_for_index:L277')
                        key = real_fp
                    if key in seen_files:
                        continue
                    seen_files.add(key)
                    rel = os.path.relpath(fp, books_root)
                    folder = os.path.dirname(rel) if os.path.dirname(rel) else ""

                    title = os.path.splitext(logical)[0]
                    author = ""

                    # Формат: "Название_Автор" или "Автор_Название"
                    # Определяем по длине - короткое (фамилия) = author, длинное = title
                    if '_' in title:
                        parts = title.rsplit('_', 1)
                        if len(parts) == 2:
                            part1, part2 = parts[0], parts[1]
                            # Если одна часть короткая (< 30 символов) - это автор
                            if len(part2) < 30 and len(part1) > 30:
                                title = part1
                                author = part2
                            elif len(part1) < 30 and len(part2) > 30:
                                title = part2
                                author = part1
                            else:
                                title = part2
                                author = part1

                    if ext == ".fb2":
                        # Шапка надёжнее имени файла: том подписан номером или
                        # «Фамилия_Имя» наоборот — из XML берём то, что внутри.
                        fb2_title, fb2_author = _read_fb2_meta(fp)
                        if fb2_title:
                            title = fb2_title
                        if fb2_author:
                            author = fb2_author

                    books.append({
                        # path — логический путь (без .zst), как его отдаёт
                        # find_books: страница книг по нему открывает том и
                        # сама разбирается с упаковкой.
                        "path": fp,
                        "rel_path": rel,
                        "title": title,
                        "author": author,
                        "folder": folder,
                        "format": ext[1:],
                        "size": (cz.size(fp) if cz is not None else
                                 (os.path.getsize(fp) if os.path.exists(fp) else 0)),
                        "modified": (cz.mtime(fp) if cz is not None else
                                     (os.path.getmtime(fp) if os.path.exists(fp) else 0))
                    })

    logger.info(f"[V5] Found {len(books)} books")
    return books


def _books_folders_mtime() -> float:
    """Максимум mtime всех каталогов книг — подпись индекса."""
    m = 0.0
    for d in books_dirs():
        try:
            if os.path.isdir(d):
                m = max(m, os.path.getmtime(d))
        except Exception:
            swallow.note('search_v5._books_folders_mtime:L342')
            continue
    return m


def _is_books_folder_changed() -> bool:
    """Проверить, изменились ли каталоги с книгами."""
    if not os.path.exists(BOOK_INDEX_FILE):
        return True

    try:
        with open(BOOK_INDEX_FILE, encoding="utf-8") as f:
            index_data = json.load(f)

        # Два основания перестроить индекс, помимо mtime папок: сменился
        # набор каталогов (добавился свой профиль) либо сам сканер (версия
        # формата). Иначе исправленный сканер вечно читал бы старый список
        # из одного тома — mtime с тех пор не менялся.
        if index_data.get("index_version") != BOOK_INDEX_VERSION:
            return True

        indexed_dirs = sorted(index_data.get("folders") or [])
        current_dirs = sorted(books_dirs())
        if "folders" not in index_data or indexed_dirs != current_dirs:
            return True

        indexed_mtime = index_data.get("folder_mtime", 0)
        return _books_folders_mtime() > indexed_mtime
    except Exception:
        swallow.note('search_v5._is_books_folder_changed:L371')
        return True


def create_book_index_if_needed() -> bool:
    """Создать индекс книг если нужно (вызывать при старте)."""
    roots = [d for d in books_dirs() if d and os.path.isdir(d)]
    if not roots:
        logger.warning(f"[V5] Books dir not found: {books_dirs()}")
        return False
    
    if not _is_books_folder_changed():
        books = _get_book_index()
        logger.info(f"[V5] Book index up to date, {len(books)} books loaded")
        return True
    
    logger.info(f"[V5] Building book index...")
    
    try:
        books = _scan_books_for_index()
        
        folder_mtime = _books_folders_mtime()
        folders = sorted(books_dirs())

        os.makedirs(os.path.dirname(BOOK_INDEX_FILE), exist_ok=True)
        index_data = {
            "last_updated": time.time(),
            "index_version": BOOK_INDEX_VERSION,
            "folder_mtime": folder_mtime,
            "folders": folders,
            "books": books
        }
        
        with open(BOOK_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        
        global _book_index_cache
        _book_index_cache = books
        
        logger.info(f"[V5] Book index created: {len(books)} books")
        return True
    except Exception as e:
        logger.error(f"[V5] Error creating book index: {e}")
        return False


def search_books_fast(query: str, limit: int = 5,
                      profile_id: str = "") -> List[Dict]:
    """Быстрый поиск по книгам через общий индекс, только видимые профилю.

    Индекс теперь объединяет каталоги всех профилей, поэтому без фильтра
    выдавали бы чужие личные книги. Пустой profile_id — активный профиль
    (прежнее поведение), и только если профиль не определяется вовсе,
    фильтр снимается, чтобы старый внутренний вызов не остался без книг.
    """
    ql = query.lower()
    books = _get_book_index()

    pid = str(profile_id or "").strip()
    if not pid:
        try:
            import profiles_module as _PM
            pid = _PM.get_active_id() or _PM.get_default_profile_id() or ""
        except Exception:
            swallow.note('search_v5.search_books_fast:L470')
    roots = _effective_dirs_for(pid) if pid else []
    if pid and not roots:
        return []

    matches = []
    for book in books:
        if pid and not _under_any_root(book.get("path") or book.get("rel_path") or "",
                                       roots):
            continue
        title = book.get("title", "").lower()
        author = book.get("author", "").lower()
        folder = book.get("folder", "").lower()
        
        score = 0
        match_type = None
        
        if ql in title:
            score = 100
            match_type = "title"
        elif ql in author:
            score = 80
            match_type = "author"
        elif ql in folder:
            score = 50
            match_type = "folder"
        
        if score > 0:
            book["score"] = score
            book["match_type"] = match_type
            matches.append(book)
    
    matches.sort(key=lambda x: (-x["score"], x.get("title", "")))
    return matches[:limit]


def get_book_context_for_llm(book: Dict) -> str:
    """Получить контекст книги для LLM (название + автор + папка + формат)."""
    title = book.get("title", "Неизвестно")
    author = book.get("author", "Неизвестно")
    folder = book.get("folder", "Корневая папка")
    fmt = book.get("format", "unknown")
    path = book.get("path", "")
    
    return f"""Книга: {title}
Автор: {author}
Категория: {folder}
Формат: {fmt}
Путь: {path}"""


def _extract_book_content_for_rag(book: Dict, question: str) -> str:
    """Извлечь релевантный контент из книги для RAG."""
    try:
        from server import get_book_content
        path = book.get("path", "")
        
        if not path or not os.path.exists(path):
            return ""
        
        data = get_book_content(path)
        
        if "error" in data:
            return get_book_context_for_llm(book)
        
        title = data.get("title", book.get("title", ""))
        author = data.get("author", book.get("author", ""))
        annotation = data.get("annotation", "")
        chapters = data.get("chapters", [])
        
        context_parts = [f"=== КНИГА: {title} ==="]
        if author:
            context_parts.append(f"Автор: {author}")
        
        if annotation:
            context_parts.append(f"\nАннотация:\n{annotation[:500]}")
        
        if chapters:
            chapter_titles = [c.get("title", "") for c in chapters[:10]]
            context_parts.append(f"\nОглавление ({len(chapters)} глав):\n" + "\n".join(f"- {t}" for t in chapter_titles))
            
            relevant_chapters = []
            ql = question.lower()
            
            for i, ch in enumerate(chapters[:20]):
                ch_title = ch.get("title", "").lower()
                ch_text = ch.get("text", "").lower()
                
                if ql in ch_title or ql in ch_text:
                    relevant_chapters.append((i, ch))
            
            if relevant_chapters:
                context_parts.append(f"\n=== РЕЛЕВАНТНЫЕ ГЛАВЫ ===")
                for idx, ch in relevant_chapters[:3]:
                    ch_title = ch.get("title", "")
                    ch_text = ch.get("text", "")[:1500]
                    context_parts.append(f"\n--- Глава {idx+1}: {ch_title} ---\n{ch_text}")
        
        return "\n".join(context_parts)
        
    except Exception as e:
        logger.warning(f"RAG extract error for {book.get('path', '')}: {e}")
        return get_book_context_for_llm(book)


def fix_spelling(question: str, model: str = None) -> str:
    """Исправляет только орфографические ошибки в запросе."""
    
    if not model:
        model = DEFAULT_MODEL
    
    prompt = f"""Исправь только орфографические ошибки в тексте. 
Не меняй смысл, не добавляй пояснения.

Текст: "{question}"

Верни исправленный текст БЕЗ кавычек и пояснений."""

    try:
        messages = [{"role": "user", "content": prompt}]
        result = call_llm(messages, model=model, temperature=0.1, max_tokens=128)
        corrected = result.strip().strip('"').strip("'")
        
        if corrected and len(corrected) > 2:
            return corrected
    except Exception as e:
        logger.warning(f"Spelling fix error: {e}")
    
    return question


def llm_generate_queries(question: str, model: str = None) -> Tuple[List[str], List[str]]:
    """Генерирует 1 поисковый запрос на русском и 1 на английском."""
    
    if not model:
        model = DEFAULT_MODEL
    
    prompt = f"""Создай ОДИН лучший поисковый запрос для поиска информации.

Запрос пользователя: "{question}"

Определи тип (фильм, книга, медицина, технологии и т.д.) и создай:
- 1 четкий запрос на РУССКОМ
- 1 четкий запрос на АНГЛИЙСКОМ

Верни JSON: {{"ru": "запрос", "en": "query"}}"""

    try:
        messages = [{"role": "user", "content": prompt}]
        result = call_llm(messages, model=model, temperature=0.3, max_tokens=256)
        
        json_match = re.search(r'\{[\s\S]*\}', result)
        if json_match:
            data = json.loads(json_match.group())
            ru = data.get("ru", [])
            en = data.get("en", [])
            
            if isinstance(ru, str):
                ru = [ru]
            if isinstance(en, str):
                en = [en]
                
            return ru[:1], en[:1]  # Только 1 запрос на язык
    except Exception as e:
        logger.warning(f"Query generation error: {e}")
    
    return [question], [question]


def quick_search_source(question: str, source_type: str, limit: int = 5) -> List[Dict]:
    """Быстрый поиск по одному источнику с лимитом."""
    try:
        source_filter = [source_type]
        results = search_unified.search_articles(
            question, 
            DATA_DIRS, 
            use_embeddings=False, 
            limit=limit * 3,
            source_filter=source_filter
        )
        
        limited_results = []
        for r in results[:limit]:
            r["source_type"] = source_type
            limited_results.append(r)
        
        return limited_results
    except Exception as e:
        logger.warning(f"Search error for {source_type}: {e}")
        return []


def search_zim_files(query: str, limit_per_file: int = 3) -> List[Dict]:
    """Поиск по всем ZIM файлам - по 3 результата с каждого."""
    results = []
    
    if not ZIM_AVAILABLE:
        return results
    
    zim_files = []
    for d in zim_dirs():
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.lower().endswith(".zim"):
                zim_files.append(os.path.join(d, f))
    
    if not zim_files:
        return results
    
    try:
        from libzim.reader import Archive, Entry
        from libzim.search import Query, Searcher
    except ImportError:
        logger.warning("libzim not available")
        return results
    
    def search_single_zim(fp):
        file_results = []
        try:
            archive = get_zim_archive(fp)
            if archive.has_fulltext_index:
                searcher = Searcher(archive)
                q = Query().set_query(query)
                search = searcher.search(q)
                estimated = search.getEstimatedMatches()
                max_results = min(limit_per_file, estimated) if estimated > 0 else limit_per_file
                
                for path in search.getResults(0, max_results):
                    try:
                        entry = archive.get_entry_by_path(path)
                        item = entry.get_item()
                        raw = bytes(item.content).decode("utf-8", errors="replace")
                        clean = re.sub(r'<[^>]+>', ' ', raw)
                        clean = re.sub(r'\s+', ' ', clean).strip()
                        
                        file_results.append({
                            "file": os.path.basename(fp),
                            "title": entry.title or path,
                            "preview": clean[:500],
                            "full_content": clean[:20000],  # Увеличено для лучшего RAG
                            "type": "zim",
                            "score": 1.0,
                            "zim_path": path,
                            "zim_file": fp,
                        })
                    except Exception:
                        swallow.note('search_v5.search_single_zim:L661')
                        continue
        except Exception:
            swallow.note('search_v5.search_single_zim:L664')
            pass
        return file_results
    
    with ThreadPoolExecutor(max_workers=min(7, len(zim_files))) as executor:
        futures = {executor.submit(search_single_zim, fp): fp for fp in zim_files}
        for future in as_completed(futures):
            try:
                file_results = future.result()
                fp = futures[future]
                if file_results:
                    logger.info(f"[V5] ZIM {os.path.basename(fp)}: {len(file_results)} results")
                results.extend(file_results)
            except Exception as e:
                logger.warning(f"[V5] ZIM search error: {e}")
                continue
    
    return results[:len(zim_files) * limit_per_file]


def quick_search_all_sources(question: str) -> Dict[str, List[Dict]]:
    """Быстрый поиск по ZIM и книгам - СВОЙ поиск, без старого search_articles."""
    all_results = {
        "zim": [],
        "book": [],
    }
    
    logger.info(f"[V5] Quick search: {question}")
    
    # ZIM - свой быстрый поиск через libzim
    zim_results = search_zim_files(question, limit_per_file=LIMITS["zim"])
    logger.info(f"[V5] ZIM libzim: {len(zim_results)} results")
    
    # Fallback на server.search_articles если libzim не нашел
    if not zim_results:
        logger.info(f"[V5] ZIM fallback to server.search_articles")
        try:
            zim_results = search_unified.search_articles(question, DATA_DIRS, use_embeddings=False, limit=LIMITS["zim"]*3, source_filter=None)
            for r in zim_results:
                r["type"] = "zim"
        except Exception as e:
            logger.warning(f"ZIM search error: {e}")
    
    all_results["zim"] = zim_results
    logger.info(f"[V5] ZIM total: {len(zim_results)} results")
    
    # Книги - свой быстрый поиск через индекс
    book_results = search_books_fast(question, limit=LIMITS["book"])
    all_results["book"] = book_results
    logger.info(f"[V5] BOOK: {len(book_results)} results")
    
    return all_results


def llm_filter_relevant(question: str, all_results: Dict[str, List[Dict]], model: str = None) -> Tuple[List[Dict], str]:
    """LLM оценивает релевантность и отбирает нужные источники."""
    
    if not model:
        model = DEFAULT_MODEL
    
    preview_parts = []
    total_count = 0
    
    for source_type, results in all_results.items():
        if not results:
            continue
        for r in results[:3]:
            title = r.get("title", "")[:80]
            # Полный контент для правильной оценки релевантности
            content = r.get("full_content", "") or r.get("preview", "")
            preview_parts.append(f"[{source_type}] {title}\n{content[:15000]}...")
            total_count += 1
    
    if not preview_parts:
        return [], "Результаты не найдены"
    
    preview_text = "\n\n".join(preview_parts)
    
    prompt = f"""Выбери САМЫЕ РЕЛЕВАНТНЫЕ статьи из результатов поиска.

Запрос: "{question}"

Результаты ({total_count} шт):
{preview_text}

Выбери ТОЛЬКО 1-2 самые релевантные статьи (где больше всего информации по запросу).
Верни JSON:
{{"selected": [
  {{"source": "zim", "title": "точное название статьи", "relevant": true/false, "reason": "кратко почему релевантна"}},
  ...
]}}

ВАЖНО: Выбери МАКСИМУМ 2 статьи! Лучше меньше но качественнее!"""

    try:
        messages = [{"role": "user", "content": prompt}]
        result = call_llm(messages, model=model, temperature=0.2, max_tokens=1024)
        
        json_match = re.search(r'\{[\s\S]*\}', result)
        if json_match:
            data = json.loads(json_match.group())
            selected = data.get("selected", [])
            
            relevant_results = []
            for item in selected:
                if item.get("relevant", False):
                    source = item.get("source", "")
                    title = item.get("title", "")
                    
                    # Ищем по точному совпадению или части title
                    for r in all_results.get(source, []):
                        r_title = r.get("title", "")
                        if r_title == title or title in r_title or r_title in title:
                            r["llm_reason"] = item.get("reason", "")
                            relevant_results.append(r)
                            break
                    
                    # Если не нашли по title - берем первый попавшийся
                    if not any(title in r.get("title", "") for r in relevant_results):
                        for r in all_results.get(source, []):
                            if r not in relevant_results:
                                relevant_results.append(r)
                                break
            
            # Fallback: если LLM не вернул ничего - берем top результаты
            filter_status = "OK"
            if not relevant_results:
                for results in all_results.values():
                    relevant_results.extend(results[:2])
                filter_status = "fallback"
            
            return relevant_results[:2], filter_status or "OK"
    except Exception as e:
        logger.warning(f"LLM filter error: {e}")
    
    # Fallback: берем первые результаты из каждого типа
    flat_results = []
    for results in all_results.values():
        flat_results.extend(results[:2])
    return flat_results[:2], "fallback"


def llm_generate_answer(question: str, relevant_results: List[Dict], model: str = None, use_rag: bool = True) -> str:
    """LLM генерирует итоговый ответ на основе релевантных источников."""
    
    if not model:
        model = DEFAULT_MODEL
    
    context_parts = []
    for i, r in enumerate(relevant_results, 1):
        source = r.get("type", "unknown")
        
        if source == "book" and use_rag:
            book_content = _extract_book_content_for_rag(r, question)
            context_parts.append(f"Источник {i} [КНИГА]:\n{book_content[:20000]}")
        else:
            title = r.get("title", "")[:100]
            content = r.get("full_content", "") or r.get("preview", "")
            context_parts.append(f"Источник {i} [{source}]: {title}\n{content[:20000]}")
    
    context = "\n\n---\n\n".join(context_parts)
    
    prompt = f"""Ответь на вопрос КРАТКО И КОНКРЕТНО на основе найденной информации.

ВОПРОС: {question}

НАЙДЕННАЯ ИНФОРМАЦИЯ:
{context}

Требования к ответу:
1. Ответь КОНКРЕТНО на вопрос (не общими фразами)
2. Если вопрос про персонажа - дай ИМЯ персонажа и описание
3. Если вопрос про фильм/книгу - дай основную информацию
4. Максимум 5-6 предложений
5. Укажи источник"""

    try:
        messages = [{"role": "user", "content": prompt}]
        answer = call_llm(messages, model=model, temperature=0.7, max_tokens=2048)
        return answer
    except Exception as e:
        logger.error(f"LLM answer error: {e}")
        return f"Ошибка генерации ответа: {e}"


def smart_search(question: str, model: str = None, use_rag: bool = True) -> Dict[str, Any]:
    """
    Основная функция умного поиска v5.
    Порядок:
    1. Поиск с оригинальным запросом (исправление только орфографии)
    2. Поиск с LLM-сформированными запросами
    """
    if not model:
        model = DEFAULT_MODEL
    
    start_time = time.time()
    logger.info(f"[V5] Smart search START: {question}, model={model}")
    
    try:
        # ЭТАП 1: Исправление орфографии и поиск
        corrected = fix_spelling(question, model)
        logger.info(f"[V5] Corrected query: {corrected}")
        
        all_results = {}
        
        logger.info(f"[V5] === ПОИСК 1: ОРИГИНАЛ + ИСПРАВЛЕННЫЙ ===")
        
        # Поиск с оригинальным запросом
        source_results = quick_search_all_sources(question)
        for source_type, results in source_results.items():
            for r in results:
                r["query_lang"] = "original"
            all_results[source_type] = results
        
        # Fallback: если ZIM пустой - используем server.search_articles
        if not all_results.get("zim"):
            logger.info(f"[V5] ZIM empty, using server.search_articles fallback")
            try:
                zim_fallback = search_unified.search_articles(question, DATA_DIRS, use_embeddings=False, limit=15, source_filter=["zim"])
                for r in zim_fallback:
                    r["type"] = "zim"
                    r["source_type"] = "zim"
                all_results["zim"] = zim_fallback
            except Exception as e:
                logger.warning(f"ZIM fallback error: {e}")
        
        # Поиск с исправленным запросом (если отличается)
        if corrected.lower() != question.lower():
            source_results = quick_search_all_sources(corrected)
            for source_type, results in source_results.items():
                existing = all_results.get(source_type, [])
                existing_ids = {r.get("title", "")[:50] for r in existing}
                for r in results:
                    if r.get("title", "")[:50] not in existing_ids:
                        r["query_lang"] = "corrected"
                        existing.append(r)
                all_results[source_type] = existing
        
        logger.info(f"[V5] After original+corrected: {sum(len(v) for v in all_results.values())} results")
        
        # ЭТАП 2: LLM-сформированные запросы
        ru_queries, en_queries = llm_generate_queries(question, model)
        logger.info(f"[V5] Generated queries - RU: {ru_queries}, EN: {en_queries}")
        
        logger.info(f"[V5] === ПОИСК 2: LLM ЗАПРОСЫ (РУ) ===")
        for q in ru_queries:
            source_results = quick_search_all_sources(q)
            
            for source_type, results in source_results.items():
                existing = all_results.get(source_type, [])
                existing_ids = {r.get("title", "")[:50] for r in existing}
                
                for r in results:
                    if r.get("title", "")[:50] not in existing_ids:
                        r["query_lang"] = "ru"
                        existing.append(r)
                
                all_results[source_type] = existing
        
        logger.info(f"[V5] After RU: {sum(len(v) for v in all_results.values())} results")
        
        logger.info(f"[V5] === ПОИСК 3: LLM ЗАПРОСЫ (EN) ===")
        for q in en_queries:
            source_results = quick_search_all_sources(q)
            
            for source_type, results in source_results.items():
                existing = all_results.get(source_type, [])
                existing_ids = {r.get("title", "")[:50] for r in existing}
                
                for r in results:
                    if r.get("title", "")[:50] not in existing_ids:
                        r["query_lang"] = "en"
                        existing.append(r)
                
                all_results[source_type] = existing
        
        logger.info(f"[V5] After EN: {sum(len(v) for v in all_results.values())} results")
        
        for source_type in all_results:
            seen = set()
            unique = []
            for r in all_results[source_type]:
                key = r.get("title", "")[:50]
                if key not in seen:
                    seen.add(key)
                    unique.append(r)
            all_results[source_type] = unique[:LIMITS.get(source_type, 5) * 3]
        
        logger.info(f"[V5] Total results: {sum(len(v) for v in all_results.values())}")
        
        relevant_results, filter_status = llm_filter_relevant(question, all_results, model)
        logger.info(f"[V5] Relevant: {len(relevant_results)}, status: {filter_status}")
        
        if not relevant_results:
            return {
                "answer": "К сожалению, по вашему запросу ничего релевантного не найдено.",
                "sources": [],
                "query_time": round(time.time() - start_time, 2),
                "status": "no_results"
            }
        
        answer = llm_generate_answer(question, relevant_results, model)
        
        # Формируем sources с полями для UI
        sources = []
        for r in relevant_results[:10]:
            s = {
                "title": r.get("title", "")[:80],
                "type": r.get("type", "unknown"),
                "score": r.get("score", 0),
            }
            # Добавляем поля для ZIM
            if r.get("type") == "zim":
                s["file"] = r.get("file", "")
                s["zim_file"] = r.get("zim_file", "")
                s["zim_path"] = r.get("zim_path", "")
            # Добавляем поля для книг
            elif r.get("type") == "book":
                s["file"] = r.get("file", "")
                s["book_path"] = r.get("path", r.get("book_path", ""))
            # Добавляем поля для заметок
            elif r.get("type") == "note":
                s["note_id"] = r.get("note_id", "")
            sources.append(s)
        
        logger.info(f"[V5] Built sources: {len(sources)}, sample: {sources[0] if sources else 'none'}")
        
        logger.info(f"[V5] Returning: answer={answer[:100] if answer else 'empty'}, sources={len(sources)}")
        return {
            "answer": answer,
            "sources": sources,
            "query_time": round(time.time() - start_time, 2),
            "status": "success"
        }
        
    except Exception as e:
        logger.error(f"[V5] Smart search error: {e}")
        import traceback
        traceback.print_exc()
        return {
            "answer": f"Ошибка поиска: {e}",
            "sources": [],
            "query_time": round(time.time() - start_time, 2),
            "status": "error"
        }


async def smart_search_async(question: str, model: str = None) -> Dict[str, Any]:
    """Асинхронная версия smart_search для интеграции с FastAPI."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, smart_search, question, model)


if __name__ == "__main__":
    test_question = "кто такой юрий гагарин"
    result = smart_search(test_question)
    print(json.dumps(result, ensure_ascii=False, indent=2))