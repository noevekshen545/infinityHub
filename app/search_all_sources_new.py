#!/usr/bin/env python3
"""
Search All Sources - Универсальный поиск по всем источникам
Поиск как в Яндексе - без AI, просто находим и показываем ссылки
"""

import os
import json
import re
import logging
import threading
import time
from typing import List, Dict, Any, Optional
import urllib.parse

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import swallow

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
logger = logging.getLogger("search_all")

# BM25 опционально - если не установлен, используем fallback
try:
    from rank_bm25 import BM25Okapi
    BM25_AVAILABLE = True
except ImportError:
    BM25_AVAILABLE = False
    logger.warning("rank_bm25 not available, using fallback scoring")

# Конфигурация - читается динамически из CFG сервера
def _get_runtime_config():
    """Читать конфигурацию из CFG сервера (обновляется при изменении настроек)."""
    try:
        # Импортируем CFG из сервера - он обновляется в реальном времени
        import sys
        if 'server' in sys.modules:
            from server import CFG
            return CFG
    except Exception:
        swallow.note('search_all_sources_new._get_runtime_config:L41')
        pass
    
    # Fallback - читаем из config.json
    try:
        with open("config.json") as f:
            return json.load(f)
    except Exception:
        swallow.note('search_all_sources_new._get_runtime_config:L49')
        return {}

# Получить директории динамически
def _get_dirs():
    """Получить директории из runtime конфигурации."""
    cfg = _get_runtime_config()
    return {
        "zim_dirs": cfg.get("zim_dirs", []),
        "books_dir": cfg.get("books_dir", ""),
        "notes_dir": cfg.get("notes_dir", ""),
        "ai_dir": cfg.get("ai_dir", ""),
        "html_viewer_dirs": cfg.get("html_viewer_dirs", []),
        "data_dirs": cfg.get("data_dirs", []) or ["/home/infinity/pars/habr/output/"],
        "apps_dirs": cfg.get("apps_dirs", []),
    }

# Директории (будут получены динамически)
DIRS_CACHE = None

def get_dirs():
    """Кэшированное получение директорий."""
    global DIRS_CACHE
    if DIRS_CACHE is None:
        DIRS_CACHE = _get_dirs()
    return DIRS_CACHE

def refresh_dirs():
    """Обновить кэш директорий."""
    global DIRS_CACHE
    DIRS_CACHE = _get_dirs()
    return DIRS_CACHE

# Лимиты на источник
PER_SOURCE_LIMIT = 100

# Кэширование
_index_cache = {}
_cache_lock = threading.Lock()

# Score веса для ранжирования
SCORES = {
    "zim": 100,
    "book": 90,
    "note": 80,
    "ai": 70,
    "html": 60,
    "news": 50,
}

# Языковая конфигурация источников
SOURCE_LANGUAGES = {
    "en": {
        "zim": ["wikipedia_en", "ted", "stackexchange_en"],
        "book": ["en", "english"],  # Книги на английском
        "note": [],  # Заметки без языковой метки - пропускаем
        "ai": ["en"],  # AI ответы на английском
        "html": ["en", "english"],
        "news": ["en", "english"],  # Новости на английском
    },
    "ru": {
        "zim": ["wikipedia_ru", "wikihow_ru", "ru_stackexchange", "wikibooks_ru"],
        "book": ["ru", "russian"],  # Книги на русском
        "note": [],  # Заметки без языковой метки - пропускаем
        "ai": ["ru"],  # AI ответы на русском
        "html": ["ru", "russian"],
        "news": ["ru", "russian"],  # Новости на русском
    }
}


def _load_json(filepath):
    try:
        with open(filepath, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        swallow.note('search_all_sources_new._load_json:L125')
        return None


def _search_in_text(query: str, text: str, query_words: List[str] = None) -> tuple:
    """Поиск текста с подсчётом совпадений и позиции."""
    if not query_words:
        query_words = query.lower().split()
    
    text_lower = text.lower()
    matches = 0
    first_pos = -1
    
    for word in query_words:
        pos = text_lower.find(word)
        if pos >= 0:
            matches += 1
            if first_pos == -1:
                first_pos = pos
    
    return matches, first_pos


def _create_result(source_type: str, title: str, url: str, preview: str = "", score: int = 0) -> Dict:
    """Создать результат поиска."""
    return {
        "type": source_type,
        "title": title[:200] if title else "Без названия",
        "url": url,
        "preview": preview[:500] if preview else "",
        "score": score
    }


# ===================== ПОИСК ПО ZIM =====================
def search_zim(query: str, limit: int = PER_SOURCE_LIMIT) -> List[Dict]:
    """Поиск по ZIM файлам через libzim (новый API). Приоритет большим файлам."""
    results = []
    query_words = query.lower().split()
    
    dirs = get_dirs()
    zim_dirs = dirs.get("zim_dirs", [])
    
    if not zim_dirs:
        return results
    
    try:
        import libzim
    except ImportError:
        logger.warning("libzim not available")
        return results
    
    # Собираем информацию о файлах и их размерах
    zim_files = []
    for zim_dir in zim_dirs:
        if not os.path.isdir(zim_dir):
            continue
        for fname in os.listdir(zim_dir):
            if not fname.endswith('.zim'):
                continue
            zim_path = os.path.join(zim_dir, fname)
            try:
                size = os.path.getsize(zim_path)
                zim_files.append((zim_path, fname, size))
            except Exception:
                swallow.note('search_all_sources_new.search_zim:L190')
                pass
    
    # Сортируем по размеру (большие файлы первыми)
    zim_files.sort(key=lambda x: x[2], reverse=True)
    
    for zim_path, fname, size in zim_files:
        try:
            arch = libzim.Archive(zim_path)
            
            # Размер файла как бонус к score (чем больше файл, тем важнее)
            size_bonus = int(size / (1024*1024*100))  # 100MB = 1 балл
            
            # Используем Searcher для поиска
            if arch.has_fulltext_index:
                searcher = libzim.Searcher(arch)
                q = libzim.Query()
                q = q.set_query(query)
                search_result = searcher.search(q)
                 
                # getResults(start, count) возвращает SearchResultSet
                result_set = search_result.getResults(0, limit)
                count = 0
                for r in result_set:
                    if count >= limit:
                        break
                    # r - это строка с заголовком статьи
                    title = str(r)
                    # Правильный URL для ZIM viewer с URL-encoding
                    url = f"/zim/view?file={zim_path}&name={fname}&path={urllib.parse.quote(title)}"
                    if title:
                        # Score = базовый + позиция + бонус за размер файла
                        score = SCORES["zim"] + (limit - count) * 10 + size_bonus
                        results.append(_create_result(
                            "zim", title, url, title[:300], score
                        ))
                        count += 1
            else:
                # Нет индекса - ищем по названиям через get_entry_by_id
                entry_count = min(arch.all_entry_count, 5000)
                for idx in range(entry_count):
                    try:
                        entry = arch.get_entry_by_id(idx)
                        if entry:
                            title = entry.get_title()
                            if title:
                                matches, pos = _search_in_text(query, title, query_words)
                                if matches > 0:
                                    score = SCORES["zim"] + matches * 10 - pos // 100 + size_bonus
                                    url = f"/zim/view?file={zim_path}&name={fname}&path={urllib.parse.quote(title)}"
                                    results.append(_create_result(
                                        "zim", title, url, title[:300], score
                                    ))
                                    if len(results) >= limit:
                                        break
                    except Exception:
                        swallow.note('search_all_sources_new.search_zim:L246')
                        continue
            
            if len(results) >= limit:
                break
                
        except Exception as e:
            logger.warning(f"Error reading ZIM {fname}: {e}")
            continue
    
    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ===================== ПОЛУЧЕНИЕ КОНТЕНТА СТАТЬИ =====================
def get_zim_article_content(zim_path: str, article_title: str) -> str:
    """Получить полный контент статьи из ZIM файла."""
    try:
        import libzim
        arch = libzim.Archive(zim_path)
        
        # Сначала по точному пути, затем по заголовку (путь и заголовок
        # в ZIM часто различаются — search_zim кодирует именно заголовок).
        entry = arch.get_entry_by_path(article_title)
        if not entry:
            try:
                entry = arch.get_entry_by_title(article_title)
            except Exception:
                swallow.note('search_all_sources_new.get_zim_article_content:L274')
                entry = None
        if entry:
            item = entry.get_item()
            if item:
                # content возвращает memoryview, нужно преобразовать
                content = bytes(item.content).decode('utf-8', errors='replace')
                # Убираем HTML теги для чистого текста
                content = re.sub(r'<[^>]+>', ' ', content)
                content = re.sub(r'\s+', ' ', content).strip()
                return content[:50000]  # Ограничиваем размер
        
        return ""
    except Exception as e:
        logger.warning(f"Error getting ZIM content: {e}")
        return ""


def get_article_content(url: str, article_type: str) -> str:
    """Получить контент статьи по URL и типу."""
    from urllib.parse import parse_qs, urlparse, unquote
    
    content = ""
    
    if article_type == "zim":
        # Парсим URL: /zim/view?file={path}&name={name}&path={title}
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            zim_path = params.get('file', [''])[0]
            article_title = params.get('path', [''])[0]
            # Декодируем URL-encoded title
            article_title = unquote(article_title)
            
            if zim_path and article_title:
                content = get_zim_article_content(zim_path, article_title)
        except Exception as e:
            logger.warning(f"Error parsing ZIM URL: {e}")
    
    elif article_type == "news":
        # Для новостей - получить из news_module
        try:
            import news_module
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            article_id = params.get('article', [''])[0]
            if article_id:
                article = news_module.get_article(article_id)
                if article:
                    content = article.get('content', '') or article.get('summary', '')
        except Exception as e:
            logger.warning(f"Error getting news content: {e}")
    
    elif article_type == "html" or article_type == "ai" or article_type == "article":
        # Получаем контент веб-страницы
        try:
            from urllib.request import urlopen, Request
            from urllib.parse import quote
            from html import unescape
            import re
            
            # Кодируем URL для безопасности
            encoded_url = quote(url, safe='/:?=&%')
            req = Request(encoded_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urlopen(req, timeout=15) as response:
                encoding = response.headers.get('content-type', 'utf-8')
                if 'charset' in encoding:
                    charset = encoding.split('charset=')[-1].split(';')[0].strip()
                else:
                    charset = 'utf-8'
                html = response.read().decode(charset, errors='ignore')
            
            # Удаляем HTML теги, оставляем текст
            # Убираем скрипты и стили
            html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<footer[^>]*>.*?</footer>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<header[^>]*>.*?</header>', '', html, flags=re.DOTALL | re.IGNORECASE)
            
            # Заменяем теги на переносы строк
            html = re.sub(r'<br\s*/?>','\n', html, flags=re.IGNORECASE)
            html = re.sub(r'</p>', '\n\n', html, flags=re.IGNORECASE)
            html = re.sub(r'</div>', '\n', html, flags=re.IGNORECASE)
            html = re.sub(r'</li>', '\n', html, flags=re.IGNORECASE)
            html = re.sub(r'<[^>]+>', ' ', html)
            
            # Очищаем HTML entities
            content = unescape(html)
            # Убираем лишние пробелы
            content = re.sub(r'\n\s*\n', '\n\n', content)
            content = re.sub(r'[ \t]+', ' ', content)
            content = content.strip()[:30000]  # Ограничиваем размер
            
            logger.info(f"Fetched {len(content)} chars from {url}")
        except Exception as e:
            logger.warning(f"Error fetching HTML content: {e}")
    
    # Для других типов - пока не реализовано
    return content


# ===================== ПОИСК ПО КНИГАМ =====================
def search_books(query: str, limit: int = PER_SOURCE_LIMIT) -> List[Dict]:
    """Поиск по книгам через индекс."""
    results = []
    query_words = query.lower().split()
    
    BOOK_INDEX_FILE = os.path.expanduser("~/.infinity/book_index.json")
    
    if not os.path.exists(BOOK_INDEX_FILE):
        return results
    
    books = _load_json(BOOK_INDEX_FILE)
    if not books:
        return results
    
    books_list = books.get("books", [])
    
    for book in books_list:
        title = book.get("title", "")
        author = book.get("author", "")
        folder = book.get("folder", "")
        
        search_text = f"{title} {author} {folder}"
        matches, pos = _search_in_text(query, search_text, query_words)
        
        if matches > 0:
            score = SCORES["book"] + matches * 10 - pos // 100
            if author:
                display_title = f"{title} - {author}"
            else:
                display_title = title
            
            results.append(_create_result(
                "book", display_title, f"/books?file={book.get('rel_path','')}", 
                f"Формат: {book.get('format','').upper()}", score
            ))
    
    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ===================== ПОИСК ПО ЗАМЕТКАМ =====================
def search_notes(query: str, limit: int = PER_SOURCE_LIMIT) -> List[Dict]:
    """Поиск по заметкам в notes_dir."""
    results = []
    query_words = query.lower().split()
    
    dirs = get_dirs()
    notes_dir = dirs.get("notes_dir", "")
    
    if not notes_dir or not os.path.isdir(notes_dir):
        return results
    
    for root, dirs, files in os.walk(notes_dir):
        for fname in files:
            if not fname.endswith('.json'):
                continue
            
            fpath = os.path.join(root, fname)
            note = _load_json(fpath)
            if not note:
                continue
            
            if not isinstance(note, dict):
                continue
            
            # Ищем в заголовке и тексте
            title = note.get("title", "") or note.get("name", "")
            content = note.get("content", "") or note.get("text", "")
            
            if not content:
                # Если нет content, пробуем другие поля
                content = json.dumps(note)
            
            search_text = f"{title} {content}"
            matches, pos = _search_in_text(query, search_text, query_words)
            
            if matches > 0:
                rel_path = os.path.relpath(fpath, notes_dir)
                score = SCORES["note"] + matches * 10 - pos // 100
                preview = content[:300] if content else ""
                
                results.append(_create_result(
                    "note", title, f"/notes?file={rel_path}", preview, score
                ))
                
                if len(results) >= limit:
                    return results[:limit]
    
    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ===================== ПОИСК ПО AI ОТВЕТАМ =====================
def search_ai_answers(query: str, limit: int = PER_SOURCE_LIMIT) -> List[Dict]:
    """Поиск по AI ответам в ai_dir."""
    results = []
    query_words = query.lower().split()
    
    dirs = get_dirs()
    ai_dir = dirs.get("ai_dir", "")
    
    if not ai_dir or not os.path.isdir(ai_dir):
        return results
    
    for fname in os.listdir(ai_dir):
        if not fname.startswith('ai_') or not fname.endswith('.json'):
            continue
        
        fpath = os.path.join(ai_dir, fname)
        ai_data = _load_json(fpath)
        if not ai_data:
            continue
        
        # Ищем в вопросе и ответе
        question = ai_data.get("question", "") or ai_data.get("prompt", "")
        answer = ai_data.get("answer", "") or ai_data.get("response", "")
        
        search_text = f"{question} {answer}"
        matches, pos = _search_in_text(query, search_text, query_words)
        
        if matches > 0:
            score = SCORES["ai"] + matches * 10 - pos // 100
            preview = answer[:300] if answer else ""
            title = question[:100] if question else fname
            
            results.append(_create_result(
                "ai", title, f"/ai_answers?file={fname}", preview, score
            ))
            
            if len(results) >= limit:
                return results[:limit]
    
    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ===================== ПОИСК ПО HTML ФАЙЛАМ =====================
def search_html(query: str, limit: int = PER_SOURCE_LIMIT) -> List[Dict]:
    """Поиск по HTML файлам."""
    results = []
    query_words = query.lower().split()
    
    dirs = get_dirs()
    html_dirs = dirs.get("html_viewer_dirs", [])
    apps_dirs = dirs.get("apps_dirs", [])
    
    # Объединяем HTML директории из обоих источников
    all_html_dirs = list(html_dirs) + list(apps_dirs)
    
    if not all_html_dirs:
        return results
    
    for html_dir in all_html_dirs:
        if not os.path.isdir(html_dir):
            continue
        
        for root, dirs, files in os.walk(html_dir):
            for fname in files:
                if not fname.lower().endswith(('.html', '.htm')):
                    continue
                
                fpath = os.path.join(root, fname)
                
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        content = f.read(50000)  # Читаем начало файла
                    
                    # Извлекаем title
                    title_match = re.search(r'<title>(.*?)</title>', content, re.I | re.S)
                    title = title_match.group(1).strip() if title_match else fname
                    
                    # Читаем body для поиска
                    body_match = re.search(r'<body[^>]*>(.*?)</body>', content, re.I | re.S)
                    body = body_match.group(1) if body_match else content
                    # Удаляем теги
                    body = re.sub(r'<[^>]+>', ' ', body)
                    body = re.sub(r'\s+', ' ', body).strip()
                    
                    search_text = f"{title} {body}"
                    matches, pos = _search_in_text(query, search_text, query_words)
                    
                    if matches > 0:
                        rel_path = os.path.relpath(fpath, html_dir)
                        score = SCORES["html"] + matches * 10 - pos // 100
                        
                        results.append(_create_result(
                            "html", title, f"/html_viewer?path={rel_path}", body[:300], score
                        ))
                        
                        if len(results) >= limit:
                            return results[:limit]
                            
                except Exception as e:
                    swallow.note('search_all_sources_new.search_html:L571', e)
                    continue
    
    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ===================== ПОИСК ПО НОВОСТЯМ =====================
def search_news(query: str, limit: int = PER_SOURCE_LIMIT) -> List[Dict]:
    """Поиск по новостям из news_module."""
    results = []
    query_words = query.lower().split()
    
    try:
        import news_module
    except ImportError:
        logger.warning("news_module not available")
        return results
    
    try:
        # Получаем все статьи (с фильтром по query)
        articles = news_module.get_articles(query=query, limit=limit)
        
        for article in articles:
            title = article.get("title", "")
            summary = article.get("summary", "")
            content = article.get("content", "")
            
            search_text = f"{title} {summary} {content}"
            matches, pos = _search_in_text(query, search_text, query_words)
            
            if matches > 0:
                article_id = article.get("id", "")
                score = SCORES["news"] + matches * 10 - pos // 100
                preview = summary[:300] if summary else content[:300]
                
                results.append(_create_result(
                    "news", title, f"/news?article={article_id}", preview, score
                ))
                
    except Exception as e:
        logger.warning(f"News search error: {e}")
    
    return results[:limit]


# ===================== ОСНОВНОЙ ПОИСК =====================
def search_all(query: str, limit: int = 500, language: str = "all") -> Dict[str, Any]:
    """Основная функция поиска по всем источникам.
    
    Args:
        query: Поисковый запрос
        limit: Максимальное количество результатов
        language: Язык источников ('all', 'en', 'ru')
    """
    if not query or len(query.strip()) < 2:
        return {"query": query, "total": 0, "results": []}
    
    query = query.strip()
    logger.info(f"[SearchAll] Query: {query}, Language: {language}")
    
    # Собираем результаты по типам
    results_by_type = {}
    
    # Определяем какие источники искать в зависимости от языка
    search_sources = ["zim", "book", "note", "ai", "html", "news"]
    
    # Параллельный поиск по всем источникам
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(search_zim, query): "zim",
            executor.submit(search_books, query): "book",
            executor.submit(search_notes, query): "note",
            executor.submit(search_ai_answers, query): "ai",
            executor.submit(search_html, query): "html",
            executor.submit(search_news, query): "news",
        }
        
        for future in as_completed(futures):
            source = futures[future]
            try:
                results = future.result()
                
                # Фильтруем по языку если указан
                if language != "all" and source in SOURCE_LANGUAGES.get(language, {}):
                    # Для ZIM - фильтруем по имени файла
                    if source == "zim":
                        allowed_patterns = SOURCE_LANGUAGES[language]["zim"]
                        results = [r for r in results if any(p in r.get("url", "").lower() for p in allowed_patterns)]
                    # Для других источников - оставляем все (недостаточно метаданных для точной фильтрации)
                    # AI ответы и новости можно фильтровать по наличию English/Russian в title
                    elif source == "ai":
                        results = results  # AI ответы - оставляем как есть
                    elif source == "news":
                        results = results  # Новости - оставляем как есть
                    elif source == "book":
                        results = results  # Книги - оставляем как есть
                
                logger.info(f"[SearchAll] {source}: {len(results)} results")
                results_by_type[source] = results
            except Exception as e:
                logger.warning(f"[SearchAll] {source} error: {e}")
                results_by_type[source] = []
    
    # Объединяем все результаты для BM25
    all_candidates = []
    for source_type, results in results_by_type.items():
        for r in results:
            r["source_type"] = source_type
            all_candidates.append(r)
    
    # Убираем дубликаты по URL
    seen_urls = set()
    unique_candidates = []
    for r in all_candidates:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_candidates.append(r)
    
    if not unique_candidates:
        return {"query": query, "total": 0, "results": [], "by_type": {}}
    
    # Ре-ранжирование
    query_words = query.lower().split()
    
    # BM25 если доступен
    if BM25_AVAILABLE:
        # Создаем корпус из title + preview
        corpus = []
        for r in unique_candidates:
            text = f"{r.get('title', '')} {r.get('preview', '')}"
            corpus.append(text.lower())
        
        try:
            bm25 = BM25Okapi(corpus)
            bm25_scores = bm25.get_scores(query_words)
            
            # Обновляем score с учетом BM25
            for i, r in enumerate(unique_candidates):
                bm25_score = bm25_scores[i]
                normalized_bm25 = bm25_score / max(bm25_scores) if max(bm25_scores) > 0 else 0
                
                # Бонус за совпадение в заголовке
                title_bonus = 0
                title_lower = r.get("title", "").lower()
                for word in query_words:
                    if word in title_lower:
                        title_bonus += 0.2
                
                r["score"] = int((normalized_bm25 * 0.7 + title_bonus * 0.3) * 1000)
        except Exception as e:
            logger.warning(f"BM25 error: {e}, using fallback")
            _apply_fallback_scoring(unique_candidates, query_words)
    else:
        # Fallback - простая сортировка по совпадениям
        _apply_fallback_scoring(unique_candidates, query_words)
    
    # Сортируем по финальному score
    unique_candidates.sort(key=lambda x: -x["score"])
    
    final_results = unique_candidates[:limit]
    
    # Группировка для статистики
    by_type = {}
    for r in final_results:
        t = r.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
    
    logger.info(f"[SearchAll] Total: {len(final_results)}, By type: {by_type}")
    
    return {
        "query": query,
        "total": len(final_results),
        "results": final_results,
        "by_type": by_type
    }


def _apply_fallback_scoring(results: List[Dict], query_words: List[str]) -> None:
    """Fallback scoring без BM25 - по количеству совпадений слов."""
    for r in results:
        title_lower = r.get("title", "").lower()
        preview_lower = r.get("preview", "").lower()
        
        # Считаем совпадения
        title_matches = sum(1 for w in query_words if w in title_lower)
        preview_matches = sum(1 for w in query_words if w in preview_lower)
        
        # Score = базовый + title*3 + preview
        base_score = r.get("score", 50)
        r["score"] = base_score + title_matches * 30 + preview_matches * 10


# ===================== СОХРАНЕНИЕ В AI DB =====================
def save_to_ai_db(question: str, answer: str, source_info: Dict = None) -> bool:
    """Сохранить вопрос и ответ в AI DB."""
    try:
        ai_dir = get_dirs().get("ai_dir", "")
        if not ai_dir:
            logger.warning("[AI DB] ai_dir не настроен — сохранение пропущено")
            return False
        os.makedirs(ai_dir, exist_ok=True)
        
        timestamp = int(time.time() * 1000)
        filename = f"ai_{timestamp}.json"
        filepath = os.path.join(ai_dir, filename)
        
        record = {
            "question": question,
            "answer": answer,
            "source_type": source_info.get("type", "unknown") if source_info else "unknown",
            "source_url": source_info.get("url", "") if source_info else "",
            "source_title": source_info.get("title", "") if source_info else "",
            "created": timestamp
        }
        
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        
        logger.info(f"[AI DB] Saved: {filename}")
        return True
    except Exception as e:
        logger.warning(f"[AI DB] Error saving: {e}")
        return False


# ===================== ТЕСТ =====================
if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "python"
    result = search_all(query, limit=50)
    print(f"\nFound: {result['total']} results")
    for r in result["results"][:10]:
        print(f"  [{r['type']}] {r['title'][:60]} - {r['score']}")