#!/usr/bin/env python3
"""
Infinity News Module — RSS + Full-Text Aggregator (FastAPI edition)
Сбор новостей из RSS, автоматическое обнаружение фидов, парсинг полных статей.
Включает APIRouter для лёгкого подключения к FastAPI.

UI: Infinity Search стиль — glass toolbar, article-viewer (боковой),
отдельная страница, grid/list режимы, TOC, reading mode, похожие новости.
"""

import os
import re
import json
import time
import hashlib
import logging
import socket
import threading
from pathlib import Path
from fastapi import APIRouter, Query, Request, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
import swallow

logger = logging.getLogger("infinity")

# ===================== КОНФИГУРАЦИЯ =====================
NEWS_DIR = os.path.expanduser("~/.infinity/news")
RSS_SOURCES_FILE = os.path.join(NEWS_DIR, "sources.json")
ARTICLES_DIR = os.path.join(NEWS_DIR, "articles")
MEDIA_DIR = os.path.join(NEWS_DIR, "media")
CATEGORIES_DIR = os.path.join(NEWS_DIR, "categories")
DOWNLOADS_DIR = os.path.join(NEWS_DIR, "downloads")
SETTINGS_FILE = os.path.join(NEWS_DIR, "settings.json")
RSS_CACHE_FILE = os.path.join(NEWS_DIR, "rss_cache.json")
USERS_DIR = os.path.join(NEWS_DIR, "users")
DIGEST_SETTINGS_FILE = os.path.join(NEWS_DIR, "digest_settings.json")

_USER_DATA_LOCK = threading.Lock()

for d in [NEWS_DIR, ARTICLES_DIR, MEDIA_DIR, CATEGORIES_DIR, DOWNLOADS_DIR, USERS_DIR]:
    os.makedirs(d, exist_ok=True)

def get_news_settings():
    default_settings = {
        "auto_cleanup_days": 30,
        "auto_cleanup_enabled": True,
        "download_images": True,
        "max_download_size_mb": 50,
        "auto_refresh_minutes": 10
    }
    return _load_json(SETTINGS_FILE, default_settings)

def save_news_settings(settings):
    return _save_json(SETTINGS_FILE, settings)

FEEDPARSER_AVAILABLE = False
try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    swallow.note('news_module.<module>:L60')
    pass

BEAUTIFULSOUP_AVAILABLE = False
try:
    from bs4 import BeautifulSoup
    import requests
    BEAUTIFULSOUP_AVAILABLE = True
except ImportError:
    swallow.note('news_module.<module>:L69')
    pass

NEWSPAPER_AVAILABLE = False
try:
    import newspaper
    NEWSPAPER_AVAILABLE = True
except ImportError:
    swallow.note('news_module.<module>:L77')
    pass

# ==================== РОУТЕР FASTAPI ====================
router = APIRouter(prefix="/api/news", tags=["news"])

# Статус последней выборки (для /api/news/fetch-status)
FETCH_STATUS = {"running": False, "finished_at": None, "results": []}

# ===================== ИСТОЧНИКИ RSS =====================

_DEFAULT_SOURCES = [
    {"id": "habr", "name": "Habr", "url": "https://habr.com/ru/rss/best/daily/", "category": "tech", "region": "russian", "enabled": True, "icon": "💻"},
    {"id": "vc", "name": "VC.ru", "url": "https://vc.ru/rss", "category": "tech", "region": "russian", "enabled": True, "icon": "📱"},
    {"id": "reddit_tech", "name": "Reddit Technology", "url": "https://www.reddit.com/r/technology/.rss", "category": "tech", "region": "international", "enabled": True, "icon": "🔴"},
    {"id": "arstechnica", "name": "Ars Technica", "url": "https://arstechnica.com/feed/", "category": "tech", "region": "international", "enabled": True, "icon": "🤖"},
    {"id": "techcrunch", "name": "TechCrunch", "url": "https://techcrunch.com/feed/", "category": "tech", "region": "international", "enabled": True, "icon": "🚀"},
    {"id": "theverge", "name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "category": "tech", "region": "international", "enabled": True, "icon": "⚡"},
    {"id": "wired", "name": "Wired", "url": "https://www.wired.com/feed/rss", "category": "tech", "region": "international", "enabled": True, "icon": "🌐"},
    {"id": "hackernews", "name": "Hacker News", "url": "https://hnrss.org/frontpage", "category": "tech", "region": "international", "enabled": True, "icon": "🔥"},
    {"id": "lenta", "name": "Lenta.ru", "url": "https://lenta.ru/rss", "category": "news", "region": "russian", "enabled": True, "icon": "📰"},
    {"id": "ria", "name": "RIA Novosti", "url": "https://ria.ru/export/rss2/archive/index.xml", "category": "news", "region": "russian", "enabled": True, "icon": "🌍"},
    {"id": "tass", "name": "TASS", "url": "https://tass.ru/rss/v2.xml", "category": "news", "region": "russian", "enabled": True, "icon": "📡"},
    {"id": "rbc", "name": "RBC", "url": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss", "category": "news", "region": "russian", "enabled": True, "icon": "💼"},
    {"id": "kommersant", "name": "Kommersant", "url": "https://www.kommersant.ru/RSS/news.xml", "category": "news", "region": "russian", "enabled": True, "icon": "📊"},
    {"id": "interfax", "name": "Interfax", "url": "https://www.interfax.ru/rss.asp", "category": "news", "region": "russian", "enabled": True, "icon": "📰"},
    {"id": "bbc_world", "name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "category": "news", "region": "international", "enabled": True, "icon": "🌍"},
    {"id": "bloomberg", "name": "Bloomberg", "url": "https://feeds.bloomberg.com/markets/news.rss", "category": "news", "region": "international", "enabled": True, "icon": "💰"},
    {"id": "aljazeera", "name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "news", "region": "international", "enabled": True, "icon": "📡"},
    {"id": "apnews", "name": "AP News", "url": "https://feeds.feedburner.com/AssociatedPress/", "category": "news", "region": "international", "enabled": True, "icon": "📰"},
    {"id": "theguardian", "name": "The Guardian", "url": "https://www.theguardian.com/world/rss", "category": "news", "region": "international", "enabled": True, "icon": "📋"},
    {"id": "nature", "name": "Nature", "url": "https://www.nature.com/nature.rss", "category": "science", "region": "international", "enabled": True, "icon": "🔬"},
    {"id": "science_daily", "name": "Science Daily", "url": "https://www.sciencedaily.com/rss/all.xml", "category": "science", "region": "international", "enabled": True, "icon": "🧪"},
    {"id": "new_scientist", "name": "New Scientist", "url": "https://www.newscientist.com/feed/home", "category": "science", "region": "international", "enabled": True, "icon": "🌟"},
    {"id": "phys_org", "name": "Phys.org", "url": "https://phys.org/rss-feed/", "category": "science", "region": "international", "enabled": True, "icon": "⚛️"},
    {"id": "national_geo", "name": "National Geographic", "url": "https://www.nationalgeographic.com/rss/", "category": "science", "region": "international", "enabled": False, "icon": "🌎"},
    {"id": "sciencemag", "name": "Science Magazine", "url": "https://www.sciencemag.org/rss/news_current.xml", "category": "science", "region": "international", "enabled": True, "icon": "🔬"},
]
def reset_default_sources():
    _save_json(RSS_SOURCES_FILE, _DEFAULT_SOURCES)
def get_rss_sources():
    sources = _load_json(RSS_SOURCES_FILE, None)
    if not isinstance(sources, list):
        reset_default_sources()
        return [dict({**s, 'type': s.get('type', 'rss')}) for s in _DEFAULT_SOURCES]
    return [dict({**s, 'type': s.get('type', 'rss')}) for s in sources]

def add_rss_source(name, url, category="news", icon="📰", color=None, source_type="rss"):
    sources = get_rss_sources()
    source_id = hashlib.md5(url.encode()).hexdigest()[:12]
    source = {"id": source_id, "name": name, "url": url, "category": category, "enabled": True, "icon": icon, "type": source_type if source_type in ('rss', 'site') else 'rss', "added_at": time.time()}
    if color:
        source['color'] = color
    sources.append(source)
    _save_json(RSS_SOURCES_FILE, sources)
    return {"ok": True, "id": source_id}

def update_source_category(source_id, category):
    sources = get_rss_sources()
    for s in sources:
        if s['id'] == source_id:
            s['category'] = category
            s['updated_at'] = time.time()
            _save_json(RSS_SOURCES_FILE, sources)
            return {"ok": True}
    return {"ok": False}

def remove_rss_source(source_id):
    sources = get_rss_sources()
    sources = [s for s in sources if s['id'] != source_id]
    _save_json(RSS_SOURCES_FILE, sources)
    return {"ok": True}

def toggle_source(source_id, enabled):
    sources = get_rss_sources()
    for s in sources:
        if s['id'] == source_id:
            s['enabled'] = enabled
    _save_json(RSS_SOURCES_FILE, sources)
    return {"ok": True}

# ===================== ПЕР-ПОЛЬЗОВАТЕЛЬСКИЕ ИСТОЧНИКИ =====================

def _user_sources(uid):
    """Пер-пользовательские настройки источников: {enabled, removed, custom, categories}."""
    data = _load_json(os.path.join(_user_dir(uid), 'sources.json'), None)
    if not isinstance(data, dict):
        return {"enabled": {}, "removed": [], "custom": [], "categories": {}}
    return data

def _save_user_sources(uid, data):
    with _USER_DATA_LOCK:
        return _save_json(os.path.join(_user_dir(uid), 'sources.json'), data)

def get_user_sources(uid='default'):
    """Каталог источников с пер-пользовательскими настройками (вкл/выкл, удалённые, кастомные)."""
    uid = _sanitize_uid(uid or 'default')
    data = _user_sources(uid)
    removed = set(data.get('removed', []) or [])
    enabled = data.get('enabled', {}) or {}
    categories = data.get('categories', {}) or {}
    sources = []
    for s in get_rss_sources():
        sid = s['id']
        if sid in removed:
            continue
        s2 = dict(s)
        if sid in enabled:
            s2['enabled'] = bool(enabled[sid])
        if sid in categories:
            s2['category'] = categories[sid]
        sources.append(s2)
    for c in data.get('custom', []) or []:
        sources.append(dict(c))
    return sources

# ===================== ПОИСК И ОБНАРУЖЕНИЕ =====================

def search_rss_feeds(query):
    known_feeds = [
        {"name": "TechCrunch", "url": "https://techcrunch.com/feed/", "category": "tech"},
        {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "category": "tech"},
        {"name": "Ars Technica", "url": "https://arstechnica.com/feed/", "category": "tech"},
        {"name": "BBC News", "url": "https://feeds.bbci.co.uk/news/rss.xml", "category": "news"},
        {"name": "CNN", "url": "https://rss.cnn.com/rss/edition.rss", "category": "news"},
        {"name": "Wired", "url": "https://www.wired.com/feed/rss", "category": "tech"},
        {"name": "Engadget", "url": "https://www.engadget.com/rss.xml", "category": "tech"},
        {"name": "Lifehacker", "url": "https://lifehacker.com/rss", "category": "lifestyle"},
        {"name": "Hacker News", "url": "https://hnrss.org/frontpage", "category": "tech"},
        {"name": "GitHub Blog", "url": "https://github.blog/feed/", "category": "tech"},
        {"name": "Smashing Magazine", "url": "https://www.smashingmagazine.com/feed/", "category": "tech"},
        {"name": "Meduza", "url": "https://meduza.io/rss/all", "category": "news"},
        {"name": "RBC", "url": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss", "category": "news"},
        {"name": "Kommersant", "url": "https://www.kommersant.ru/RSS/news.xml", "category": "news"},
    ]
    q = query.lower()
    return [f for f in known_feeds if q in f['name'].lower() or q in f['category'].lower()]

def discover_rss_from_url(url):
    try:
        import urllib.request
        from html.parser import HTMLParser
        class RSSFinder(HTMLParser):
            def __init__(self):
                super().__init__()
                self.feeds = []
            def handle_starttag(self, tag, attrs):
                d = dict(attrs)
                if tag == 'link' and ('rss' in d.get('type','') or 'atom' in d.get('type','')):
                    href = d.get('href','')
                    if href.startswith('/'):
                        from urllib.parse import urlparse
                        p = urlparse(url)
                        href = f"{p.scheme}://{p.netloc}{href}"
                    self.feeds.append({"url": href, "title": d.get('title','RSS/Atom Feed')})
        req = urllib.request.Request(url, headers=_plain_ua_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode('utf-8', errors='replace')
        finder = RSSFinder()
        finder.feed(html)
        if not finder.feeds:
            from urllib.parse import urlparse
            p = urlparse(url)
            base = f"{p.scheme}://{p.netloc}"
            for path in ['/rss', '/feed', '/rss.xml', '/atom.xml', '/news/feed']:
                full = base + path
                try:
                    urllib.request.urlopen(urllib.request.Request(full, headers=_plain_ua_headers()), timeout=5)
                    finder.feeds.append({"url": full, "title": f"Auto-detected {path}"})
                    break
                except Exception:
                    swallow.note('news_module.discover_rss_from_url:L248')
                    continue
        return finder.feeds
    except Exception as e:
        logger.error(f"RSS discovery error: {e}")
        return []

# ===================== ЗАГРУЗКА НОВОСТЕЙ =====================

def _detect_language(text):
    """Определяет язык текста (ru/en/other) по первому значащему слову."""
    if not text:
        return 'other'
    clean = re.sub(r'[^a-zA-Zа-яА-ЯёЁ]', '', text[:500]).strip()
    if not clean:
        return 'other'
    cyrillic = sum(1 for c in clean if 'а' <= c.lower() <= 'я' or c == 'ё' or c == 'Ё')
    latin = sum(1 for c in clean if 'a' <= c.lower() <= 'z')
    if cyrillic > latin * 0.5:
        return 'ru'
    if latin > cyrillic * 0.5:
        return 'en'
    return 'other'

def _fetch_full_article(entry, download_media=True):
    raw_title = entry.get('title', '')
    raw_summary = entry.get('summary', '')
    article = {
        'title': raw_title,
        'summary': raw_summary[:500],
        'content': '',
        'link': entry.get('link', ''),
        'published': entry.get('published', ''),
        'author': entry.get('author', ''),
        'media': [],
        'lang': _detect_language(f"{raw_title} {raw_summary}"),
        'fetched_at': time.time()
    }
    content_fields = entry.get('content', [])
    if content_fields:
        article['content'] = content_fields[0].get('value', '')
    if not article['content'] and 'content_encoded' in entry:
        article['content'] = entry['content_encoded']
    if not article['content']:
        article['content'] = entry.get('summary', '')

    media_urls = set()
    for mc in entry.get('media_content', []):
        if isinstance(mc, dict):
            url = mc.get('url', '')
            if url: media_urls.add(url)
    mt = entry.get('media_thumbnail', [])
    if isinstance(mt, list):
        for t in mt:
            if isinstance(t, dict):
                url = t.get('url', '')
                if url: media_urls.add(url)
    for enc in entry.get('enclosures', []):
        if isinstance(enc, dict):
            url = enc.get('href', '') or enc.get('url', '')
            if url and any(ext in url.lower() for ext in ['.jpg','.jpeg','.png','.gif','.webp','.avif']):
                media_urls.add(url)
    for link in entry.get('links', []):
        if isinstance(link, dict) and link.get('rel') in ('enclosure', 'image', 'preview'):
            url = link.get('href', '')
            if url: media_urls.add(url)
    summary = entry.get('summary', '')
    if summary and not media_urls:
        try:
            from bs4 import BeautifulSoup as _BSS
            _s = _BSS(summary, 'html.parser')
            _img = _s.find('img')
            if _img and _img.get('src'):
                media_urls.add(_img['src'])
        except Exception:
            swallow.note('news_module._fetch_full_article:L323')
            pass
    if media_urls:
        article['media'] = [{"type": "image", "url": url} for url in media_urls]

    if NEWSPAPER_AVAILABLE and article['link'] and len(article['content']) < 500:
        try:
            a = newspaper.article(article['link'], language='ru')
            a.download()
            a.parse()
            if a.text and len(a.text) > len(article.get('content','')):
                article['content'] = a.text
                if a.top_image:
                    article['media'] = [m for m in article.get('media',[]) if m.get('type')!='image']
                    article['media'].append({"type":"image","url":a.top_image})
                for img in a.images:
                    if img not in [m['url'] for m in article['media']]:
                        article['media'].append({"type":"image","url":img})
                if a.authors:
                    article['author'] = a.authors[0]
        except Exception as e:
            logger.debug(f"newspaper fallback failed: {e}")

    if BEAUTIFULSOUP_AVAILABLE and article['link'] and len(article['content']) < 200:
        bs_result = _fetch_page_with_beautifulsoup(article['link'])
        bs_content = bs_result.get('content', '') if isinstance(bs_result, dict) else bs_result
        if bs_content and len(bs_content) > len(article.get('content','')):
            article['content'] = bs_content
            if isinstance(bs_result, dict) and bs_result.get('image') and not article.get('media'):
                article['media'] = [{"type": "image", "url": bs_result['image']}]

    if article['content']:
        article['content'] = re.sub(r'<script[^>]*>.*?</script>', '', article['content'], flags=re.DOTALL)
        article['content'] = re.sub(r'<style[^>]*>.*?</style>', '', article['content'], flags=re.DOTALL)
        article['content'] = re.sub(r'<iframe[^>]*>.*?</iframe>', '', article['content'], flags=re.DOTALL)

    if article['content'] and len(article['content']) > len(article.get('summary', '')):
        plain = re.sub(r'<[^>]+>', ' ', article['content'])
        plain = re.sub(r'\s+', ' ', plain).strip()
        if plain:
            article['summary'] = _build_excerpt(plain, 5, 500)

    return article

def _fetch_page_with_beautifulsoup(url):
    try:
        logger.debug(f"Fetching page: {url}")
        import bs4
        import requests as _req
        import re as _re

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        }
        session = _req.Session()
        resp = session.get(url, headers=headers, timeout=20, allow_redirects=True)
        logger.debug(f"Got response: {resp.status_code}")

        if resp.status_code != 200:
            return ''

        if resp.headers.get('content-type', '').startswith('text/html'):
            try:
                resp.encoding = resp.apparent_encoding or 'utf-8'
            except Exception:
                swallow.note('news_module._fetch_page_with_beautifulsoup:L390')
                resp.encoding = 'utf-8'
        else:
            resp.encoding = 'utf-8'

        html = resp.text
        logger.debug(f"HTML length: {len(html)}")

        try:
            soup = bs4.BeautifulSoup(html, 'lxml')
        except Exception as e:
            logger.debug(f"lxml failed, using html.parser: {e}")
            soup = bs4.BeautifulSoup(html, 'html.parser')
        logger.debug("Soup created")

        og_description = ''
        og_title = ''
        og_image_meta = ''
        try:
            og_tag = soup.find('meta', property='og:description')
            if og_tag:
                og_description = og_tag.get('content', '')
            og_t = soup.find('meta', property='og:title')
            if og_t and og_t.get('content'):
                og_title = og_t['content'].strip()[:200]
            if not og_title and soup.title and soup.title.get_text(strip=True):
                og_title = soup.title.get_text(strip=True)[:200]
            if not og_title and soup.h1 and soup.h1.get_text(strip=True):
                og_title = soup.h1.get_text(strip=True)[:200]
            og_i = soup.find('meta', property='og:image')
            if og_i and og_i.get('content'):
                og_image_meta = og_i['content']
        except Exception:
            swallow.note('news_module._fetch_page_with_beautifulsoup:L423')
            pass

        content_patterns = [
            'article-body', 'article-content', 'article-text', 'article_text',
            'topic-body', 'topic-content', 'topic-text', 'topic_text',
            'post-body', 'post-content', 'post-text', 'post_text',
            'item-body', 'item-content', 'item-text', 'item_text',
            'story-body', 'story-content', 'story-text', 'story_text',
            'entry-body', 'entry-content', 'entry-text', 'entry_text',
            'news-body', 'news-content', 'news-text', 'news_text',
            'text-body', 'text-content', 'full-text', 'fulltext',
            'content-body', 'content-text', 'main-content', 'main-content',
            'page-content', 'post-body', 'copy-text',
            'b-article', 'b-post', 'b-item', 'b-story',
            'js-article', 'js-post', 'js-item', 'js-content',
            'node-body', 'field-body',
        ]
        generic_patterns = _re.compile(
            r'article|content|post|entry|text|body|topic|item|story|news|editorial',
            _re.I
        )

        # Протекция контента: найти кандидата ДО удаления шума, чтобы
        # шумовая чистка не удалила сам контейнер статьи (например,
        # класс "tm-page__main_has-sidebar" содержит слово "sidebar").
        protected_ids = set()
        try:
            probe = soup.find('article') or soup.find('main')
            if not probe:
                for pattern in content_patterns:
                    found = soup.find('div', class_=_re.compile(_re.escape(pattern), _re.I))
                    if found and len(found.get_text(strip=True)) > 200:
                        probe = found
                        break
            if not probe:
                for div in soup.find_all('div'):
                    cls = str(div.get('class', []) or [])
                    tid = str(div.get('id', '') or '')
                    if generic_patterns.search(cls) or generic_patterns.search(tid):
                        if len(div.get_text(strip=True)) > 500 and len(div.find_all('p')) >= 2:
                            probe = div
                            break
            if probe:
                p = probe
                while p is not None:
                    protected_ids.add(id(p))
                    p = p.parent
                logger.debug(f"Protected container: {probe.name} {str(probe.get('class', []) or [])[:60]}")
        except Exception as e:
            logger.debug(f"Protection detection error: {e}")

        noise_selectors = [
            'script', 'style', 'noscript', 'iframe', 'form', 'nav', 'header', 'footer',
            'aside', 'meta', 'link', 'input', 'button', 'svg', 'canvas'
        ]
        for tag in soup.find_all(noise_selectors):
            if id(tag) in protected_ids:
                continue
            tag.decompose()
        logger.debug("Noise tags removed")

        noise_patterns = [
            'cookie', 'popup', 'modal', 'banner', 'subscribe', 'newsletter', 'promo',
            'advertisement', 'commercial', 'social', 'share', 'comments', 'related',
            'sidebar', 'widget', 'menu', 'nav', 'footer', 'header', 'author-bio',
            'rating', 'pagination', 'tags', 'similar', 'recommended', 'popular',
            'trending', 'sticky', 'fixed', 'cookie', 'gdpr', 'consent'
        ]

        for tag in soup.find_all(['div', 'section', 'span', 'ul']):
            try:
                if id(tag) in protected_ids:
                    continue
                # decompose() родителя обнуляет attrs у детей, а список find_all() собран заранее:
                # такой тег уже удалён из дерева, и .get() на него падает AttributeError
                if tag.attrs is None:
                    continue
                cls = str(tag.get('class', []) or []).lower()
                tid = str(tag.get('id', '') or '').lower()
                combined = cls + ' ' + tid
                if any(p in combined for p in noise_patterns):
                    tag.decompose()
            except Exception as e:
                swallow.note('news_module._fetch_page_with_beautifulsoup:L507', e)
                pass

        logger.debug("Noise divs removed")

        target = None

        try:
            article_tag = soup.find('article')
            if article_tag:
                target = article_tag
                logger.debug("Found article tag")

            if not target:
                main_tag = soup.find('main')
                if main_tag:
                    target = main_tag
                    logger.debug("Found main tag")

            if not target:
                for pattern in content_patterns:
                    try:
                        found = soup.find('div', class_=_re.compile(_re.escape(pattern), _re.I))
                        if found:
                            text_len = len(found.get_text(strip=True))
                            if text_len > 200:
                                target = found
                                logger.debug(f"Found content pattern: {pattern}")
                                break
                    except Exception:
                        swallow.note('news_module._fetch_page_with_beautifulsoup:L537')
                        pass

            if not target:
                candidates = []
                for div in soup.find_all('div'):
                    try:
                        cls = str(div.get('class', []) or [])
                        tid = str(div.get('id', '') or '')
                        if generic_patterns.search(cls) or generic_patterns.search(tid):
                            text_len = len(div.get_text(strip=True))
                            p_count = len(div.find_all('p'))
                            if text_len > 500 and p_count >= 2:
                                candidates.append({
                                    'element': div,
                                    'text_len': text_len,
                                    'p_count': p_count,
                                    'class': cls
                                })
                    except Exception:
                        swallow.note('news_module._fetch_page_with_beautifulsoup:L557')
                        pass
                if candidates:
                    candidates.sort(key=lambda x: (x['p_count'], x['text_len']), reverse=True)
                    target = candidates[0]['element']
                    logger.debug(f"Found generic div with {candidates[0]['p_count']} paragraphs")

            if not target:
                candidates = []
                for div in soup.find_all('div'):
                    try:
                        text = div.get_text(strip=True)
                        p_tags = div.find_all('p')
                        meaningful_ps = [p for p in p_tags if len(p.get_text(strip=True)) > 50]
                        text_len = len(text)
                        if text_len > 300 and len(meaningful_ps) >= 2:
                            candidates.append({
                                'element': div,
                                'text_len': text_len,
                                'p_count': len(meaningful_ps),
                                'score': len(meaningful_ps) * 100 + text_len
                            })
                    except Exception:
                        swallow.note('news_module._fetch_page_with_beautifulsoup:L580')
                        pass
                if candidates:
                    candidates.sort(key=lambda x: x['score'], reverse=True)
                    target = candidates[0]['element']
                    logger.debug(f"Found max text div with {candidates[0]['p_count']} paragraphs")

            if not target:
                body_tag = soup.find('body')
                target = body_tag if body_tag else soup
                logger.debug("Using body or soup as fallback")

        except Exception as e:
            logger.debug(f"Content search error: {e}")
            target = soup

        logger.debug(f"Target selected: {target is not None}")

        text_elements = []

        for elem in target.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote', 'pre', 'dd']):
            try:
                text = elem.get_text(separator=' ', strip=True)
                text = _re.sub(r'\s+', ' ', text)

                if len(text) < 25:
                    continue
                if text.startswith(('http', 'www.', '@', '//')):
                    continue

                skip_phrases = ['read more', 'learn more', 'click here', 'read now', 'follow us',
                               'subscribe', 'share this', 'подпишитесь', 'читайте также',
                               'рейтинг', 'голосовать', 'vote', 'please wait', 'loading']
                if any(phrase in text.lower() for phrase in skip_phrases):
                    continue

                if elem.name == 'li':
                    text = '• ' + text
                elif elem.name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                    text = '\n\n' + text + '\n\n'
                elif elem.name == 'blockquote':
                    text = '\n> ' + text + '\n'

                text_elements.append(text)
            except Exception as e:
                swallow.note('news_module._fetch_page_with_beautifulsoup:L625', e)
                pass

        logger.debug(f"Extracted {len(text_elements)} text elements")

        content = '\n'.join(text_elements)

        if len(content.strip()) < 300:
            if og_description and len(og_description) > 50:
                content = og_description

        if len(content.strip()) < 150:
            all_text = target.get_text(separator='\n', strip=True)
            lines = [l.strip() for l in all_text.split('\n') if len(l.strip()) > 40]
            filtered_lines = []
            for line in lines:
                if not any(p in line.lower() for p in noise_patterns):
                    if not line.startswith(('Cookie', 'Copyright', '©', 'Privacy', 'Terms')):
                        filtered_lines.append(line)
            if filtered_lines:
                content = '\n\n'.join(filtered_lines[:80])

        content = _re.sub(r'\n{4,}', '\n\n', content)
        content = _re.sub(r'[ \t]+', ' ', content)
        content = _re.sub(r'\n\s*\n', '\n\n', content)
        content = content.strip()

        if not content:
            logger.debug(f"Content empty after extraction for {url}")
            return {'content': '', 'image': ''}

        og_image = og_image_meta
        try:
            if not og_image:
                og_img_tag = soup.find('meta', property='og:image')
                if og_img_tag and og_img_tag.get('content'):
                    og_image = og_img_tag['content']
            if not og_image:
                og_img_tag = soup.find('meta', attrs={'name': 'twitter:image'})
                if og_img_tag and og_img_tag.get('content'):
                    og_image = og_img_tag['content']
            if not og_image:
                first_img = soup.find('img')
                if first_img and first_img.get('src') and not first_img['src'].startswith('data:'):
                    og_image = first_img['src']
                    if og_image.startswith('/'):
                        from urllib.parse import urlparse as _up
                        p = _up(url)
                        og_image = f"{p.scheme}://{p.netloc}{og_image}"
        except Exception:
            swallow.note('news_module._fetch_page_with_beautifulsoup:L675')
            pass

        logger.debug(f"Successfully extracted {len(content)} chars, image={og_image[:80] if og_image else 'none'} from {url}")
        return {'content': content[:15000], 'image': og_image, 'title': og_title}

    except _req.exceptions.Timeout:
        logger.debug(f"Timeout fetching {url}")
        return {'content': '', 'image': ''}
    except _req.exceptions.RequestException as e:
        logger.debug(f"Request error for {url}: {e}")
        return {'content': '', 'image': ''}
    except Exception as e:
        import traceback
        logger.debug(f"BeautifulSoup fetch error for {url}: {e}")
        logger.debug(traceback.format_exc())
        return {'content': '', 'image': ''}

def _download_media_files(article, article_id):
    media_dir = os.path.join(MEDIA_DIR, article_id)
    os.makedirs(media_dir, exist_ok=True)
    downloaded = []
    for i, m in enumerate(article.get('media', [])[:5]):
        try:
            url = m['url']
            ext = os.path.splitext(url.split('?')[0])[1] or '.jpg'
            filepath = os.path.join(media_dir, f"media_{i}{ext}")
            import urllib.request
            req = urllib.request.Request(url, headers=_plain_ua_headers())
            with urllib.request.urlopen(req, timeout=15) as resp:
                with open(filepath, 'wb') as f:
                    f.write(resp.read())
            m['local_path'] = filepath
            downloaded.append(m)
        except Exception as e:
            logger.debug(f"Media download failed: {url[:50]} - {e}")
    return downloaded

def _page_headers():
    """Заголовки, которыми сайт не отклоняет запрос (406/403 от антиботов)."""
    return {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
        'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                   'image/avif,image/webp,*/*;q=0.8'),
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
    }


def _plain_ua_headers():
    """Браузерный User-Agent, но БЕЗ Accept-Encoding — для urllib.

    Тот же UA, что и у _page_headers(): короткий «Mozilla/5.0» часть сайтов
    (Reddit в первую очередь) считает ботом и отвечает 403. Accept-Encoding
    добавляем нарочно не здесь: urllib не распаковывает gzip/br, и feedparser
    или BeautifulSoup получили бы сжатые байты вместо текста.
    """
    return {'User-Agent': _page_headers()['User-Agent']}


def fetch_page_bytes(url, timeout=12):
    """Скачать байты страницы с несколькими попытками.

    Раньше был один запрос с «голым» User-Agent: агрессивные сайты
    (New Scientist и ряд CDN) отвечали 406/403, и кнопки «Скачать
    страницу» / «Вся страница» в новостях показывали ошибку, хотя
    текст статьи уже был получен из RSS.

    Порядок попыток:
      1) обычный GET с браузерными заголовками;
      2) то же + Referer на корень сайта (многие антиботы требуют его);
      3) GET без сжатия (Accept-Encoding: identity) — часть CDN ломается
         на brotli/gzip и отвечает ошибкой;
      4) «кеш-обход»: добавляем к URL случайный query-параметр.

    Таймаут по умолчанию 12 с на попытку: четыре попытки по 30 с держали
    кнопку «Скачать страницу» почти две минуты на заблокированном сайте.

    Возвращает (bytes, content_type) либо (None, '').
    """
    import requests as _req
    import random as _random
    from urllib.parse import urlsplit, urlunsplit

    def _get(u, headers, to=timeout):
        resp = _req.get(u, headers=headers, timeout=to, allow_redirects=True)
        data = resp.content
        ctype = resp.headers.get('content-type', '')
        return data, ctype, resp.status_code

    # попытка 1 — базовые заголовки
    base_headers = _page_headers()
    try:
        data, ctype, code = _get(url, base_headers)
        if code == 200 and data:
            return data, ctype
        last_code = code
    except Exception as e:
        logger.debug(f"[download] attempt 1 failed: {e}")
        last_code = getattr(getattr(e, 'response', None), 'status_code', 0)

    # попытка 2 — с Referer
    try:
        parts = urlsplit(url)
        referer = urlunsplit((parts.scheme, parts.netloc, '/', '', ''))
        h2 = dict(base_headers)
        h2['Referer'] = referer
        h2['Sec-Fetch-Site'] = 'same-origin'
        data, ctype, code = _get(url, h2)
        if code == 200 and data:
            return data, ctype
        last_code = code
    except Exception as e:
        logger.debug(f"[download] attempt 2 failed: {e}")

    # попытка 3 — без сжатия
    try:
        h3 = dict(base_headers)
        h3['Accept-Encoding'] = 'identity'
        data, ctype, code = _get(url, h3)
        if code == 200 and data:
            return data, ctype
        last_code = code
    except Exception as e:
        logger.debug(f"[download] attempt 3 failed: {e}")

    # попытка 4 — обход кеша
    try:
        parts = urlsplit(url)
        q = parts.query
        sep = '&' if q else '?'
        bust = f"{q}{sep}_={int(time.time())}{_random.randint(1000, 9999)}"
        u4 = urlunsplit((parts.scheme, parts.netloc, parts.path, bust, parts.fragment))
        data, ctype, code = _get(u4, base_headers)
        if code == 200 and data:
            return data, ctype
        last_code = code
    except Exception as e:
        logger.debug(f"[download] attempt 4 failed: {e}")

    logger.debug(f"[download] page not downloadable: {url} (last status {last_code})")
    return None, ''


def _render_article_snapshot(article, url):
    """Запасной вариант «страницы»: сверстанная из уже сохранённой статьи.

    Если сайт режет запросы ботом (HTTP 406/403), текст статьи всё равно
    есть — из RSS. Собираем из него полноценный HTML, чтобы кнопка
    «Скачать страницу» работала, а не возвращала ошибку.
    """
    from html import escape
    title = escape(article.get('title') or 'Статья')
    source = escape(article.get('source') or '')
    content = article.get('content') or article.get('summary') or ''
    img = ''
    for m in (article.get('media') or [])[:1]:
        if isinstance(m, dict) and m.get('url'):
            img = f'<img src="{escape(m["url"])}" alt="" style="max-width:100%">'
            break
    body = ''.join(f'<p>{escape(p)}</p>'
                   for p in [x.strip() for x in content.split('\n') if x.strip()])
    return (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        f'<title>{title}</title>'
        '<style>body{font:16px/1.6 system-ui,sans-serif;max-width:760px;'
        'margin:32px auto;padding:0 16px;color:#1a1a1a}'
        'img{max-width:100%}footer{color:#777;font-size:13px;margin-top:24px}</style>'
        f'</head><body><h1>{title}</h1>{img}<article>{body}</article>'
        f'<footer>{source} · <a href="{escape(url)}">источник</a></footer>'
        '</body></html>'
    ).encode('utf-8')


def download_page_html(url, article_id=None):
    """Скачивает HTML страницы целиком (с запасным вариантом из текста статьи)"""
    settings = get_news_settings()
    download_images = settings.get('download_images', True)
    max_size_mb = settings.get('max_download_size_mb', 50)

    article_id = article_id or hashlib.md5(url.encode()).hexdigest()[:16]
    download_dir = os.path.join(DOWNLOADS_DIR, article_id)
    os.makedirs(download_dir, exist_ok=True)

    try:
        from bs4 import BeautifulSoup

        headers = _page_headers()

        # 12 с на попытку: четыре попытки по 30 с держали кнопку
        # «Скачать страницу» почти две минуты на заблокированном сайте.
        html_content, _ctype = fetch_page_bytes(url, timeout=12)
        fallback = False

        if not html_content:
            # Сайт не отдаёт страницу боту (406/403) — собираем её из текста
            # статьи, который уже сохранён из RSS. Кнопка «Скачать страницу»
            # и просмотр во фрейме продолжают работать.
            art = get_article(article_id) or {}
            html_content = _render_article_snapshot(art, url)
            fallback = True
            download_images = False
            if not html_content:
                return {"ok": False, "error": "Не удалось загрузить страницу"}

        if len(html_content) > max_size_mb * 1024 * 1024:
            return {"ok": False, "error": f"Страница слишком большая (> {max_size_mb}MB)"}

        base_url = '/'.join(url.split('/')[:3])

        if download_images:
            from urllib.parse import urljoin
            # Без этого имени каждая картинка давала NameError, который
            # проглатывался голым except ниже: файлы никогда не сохранялись.
            import requests as _req
            try:
                soup = BeautifulSoup(html_content, 'lxml')
            except Exception:
                swallow.note('news_module.download_page_html:L900')
                soup = BeautifulSoup(html_content, 'html.parser')
            images_dir = os.path.join(download_dir, 'images')
            os.makedirs(images_dir, exist_ok=True)

            img_count = 0
            for img in soup.find_all('img'):
                img_url = img.get('src') or img.get('data-src') or img.get('data-lazy')
                if not img_url:
                    continue

                img_url = urljoin(base_url, img_url)

                if img_url.startswith('data:'):
                    continue

                try:
                    img_resp = _req.get(img_url, headers=headers, timeout=10, stream=True)
                    if img_resp.status_code == 200:
                        content_type = img_resp.headers.get('content-type', '')
                        if 'image' in content_type or any(img_url.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg']):
                            ext = os.path.splitext(img_url.split('?')[0])[1] or '.jpg'
                            if not ext or len(ext) > 5:
                                ext = '.jpg'
                            img_filename = f"image_{img_count}{ext}"
                            img_path = os.path.join(images_dir, img_filename)

                            with open(img_path, 'wb') as f:
                                for chunk in img_resp.iter_content(chunk_size=8192):
                                    f.write(chunk)

                            img['src'] = f"images/{img_filename}"
                            img_count += 1
                    img_resp.close()
                except Exception:
                    swallow.note('news_module.download_page_html:L935')
                    pass

            html_content = str(soup).encode('utf-8')

        html_path = os.path.join(download_dir, 'index.html')
        with open(html_path, 'wb') as f:
            f.write(html_content)

        return {
            "ok": True,
            "path": html_path,
            "article_id": article_id,
            # Округляем до 0.1 КБ: маленькие страницы показывали «0 КБ»
            # и в UI это выглядело как сбой загрузки.
            "size_kb": round(len(html_content) / 1024, 1),
            "fallback": fallback
        }

    except Exception as e:
        swallow.note('news_module.download_page_html:L955', e)
        return {"ok": False, "error": str(e)}

def get_downloaded_page(article_id):
    """Возвращает путь к скачанной странице"""
    html_path = os.path.join(DOWNLOADS_DIR, article_id, 'index.html')
    if os.path.exists(html_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            return f.read()
    return None

def cleanup_old_downloads():
    """Удаляет старые скачанные страницы"""
    settings = get_news_settings()

    if not settings.get('auto_cleanup_enabled', True):
        return {"cleaned": 0, "message": "Автоочистка отключена"}

    days = settings.get('auto_cleanup_days', 30)
    if days <= 0:
        return {"cleaned": 0, "message": "Автоочистка отключена"}

    current_time = time.time()
    cleaned = 0

    for folder in os.listdir(DOWNLOADS_DIR):
        folder_path = os.path.join(DOWNLOADS_DIR, folder)
        if not os.path.isdir(folder_path):
            continue

        folder_mtime = os.path.getmtime(folder_path)
        age_days = (current_time - folder_mtime) / (24 * 3600)

        if age_days > days:
            try:
                import shutil
                shutil.rmtree(folder_path)
                cleaned += 1
            except Exception:
                swallow.note('news_module.cleanup_old_downloads:L994')
                pass

    return {"cleaned": cleaned, "message": f"Удалено {cleaned} старых страниц"}

def _load_rss_cache():
    return _load_json(RSS_CACHE_FILE, {})

def _save_rss_cache(cache):
    _save_json(RSS_CACHE_FILE, cache)

def _backoff_map(cache):
    """Счётчики неудач в том же rss_cache.json: {'_backoff': {source_id: {fails, until}}}."""
    st = cache.get('_backoff')
    if not isinstance(st, dict):
        st = {}
        cache['_backoff'] = st
    return st


# Источник, который не отвечает несколько раз подряд, замолкает на время:
# мёртвый URL (reutersagency.com, nationalgeographic.com/rss) или фид, который
# наша сеть не пропускает (BBC, Google News), иначе опрашиваются вечно и
# каждые 10 минут выдают пачку ERROR в лог и сетевой шторм.
_BACKOFF_AFTER_FAILS = 3
_BACKOFF_BASE_S = 3600
_BACKOFF_MAX_S = 6 * 3600


def _remember_feed_failure(cache, src_id, now):
    st = _backoff_map(cache)
    rec = st.get(src_id) if isinstance(st.get(src_id), dict) else {}
    fails = int(rec.get('fails', 0)) + 1
    until = 0
    if fails >= _BACKOFF_AFTER_FAILS:
        step = _BACKOFF_BASE_S * (2 ** min(fails - _BACKOFF_AFTER_FAILS, 4))
        until = now + min(step, _BACKOFF_MAX_S)
    st[src_id] = {'fails': fails, 'until': until}
    _save_rss_cache(cache)
    return st[src_id]


def _forget_feed_failure(cache, src_id):
    st = _backoff_map(cache)
    if src_id in st:
        st.pop(src_id, None)
        _save_rss_cache(cache)


def fetch_feed_articles(source, limit=50, download_media=True):
    if not FEEDPARSER_AVAILABLE:
        return {"error": "feedparser не установлен"}
    src_id = source['id']
    now = time.time()
    cache = _load_rss_cache()
    rec = _backoff_map(cache).get(src_id)
    if isinstance(rec, dict) and now < rec.get('until', 0):
        return {"ok": True, "articles": 0, "paused": True,
                "retry_in": int(rec['until'] - now)}
    try:
        import urllib.request as _ureq
        cached = cache.get(src_id, {}) if isinstance(cache.get(src_id, {}), dict) else {}
        # Короткий «Mozilla/5.0» Reddit и часть сайтов считают бот-запросом и
        # отдают 403. Accept-Encoding в _plain_ua_headers() нет сознательно:
        # urllib его не распаковывает, и gzip-ответ feedparser прочитал бы как мусор.
        headers = _plain_ua_headers()
        headers["Accept"] = ("application/rss+xml, application/atom+xml, "
                             "application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5")
        headers["Accept-Language"] = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
        etag = cached.get('etag')
        if etag:
            headers['If-None-Match'] = etag
        modified = cached.get('modified')
        if modified:
            headers['If-Modified-Since'] = modified
        conditional = [k for k in ('If-None-Match', 'If-Modified-Since') if k in headers]

        def _read_and_parse(hdrs):
            with _ureq.urlopen(_ureq.Request(source['url'], headers=hdrs), timeout=10) as resp:
                raw = resp.read()
                new_etag = resp.headers.get('ETag')
                new_modified = resp.headers.get('Last-Modified')
                if new_etag or new_modified:
                    cache[src_id] = {'etag': new_etag, 'modified': new_modified}
                    _save_rss_cache(cache)
                return feedparser.parse(raw)

        try:
            feed = _read_and_parse(headers)
        except _ureq.HTTPError as e:
            if e.code == 304:
                logger.info(f"RSS not modified (304): {source['name']}")
                _forget_feed_failure(cache, src_id)
                return {"ok": True, "articles": 0, "not_modified": True}
            if e.code in (404, 410) and conditional:
                # Часть редакций отвечает 404 на условный запрос с протухшим
                # ETag вместо честного 304. Кэш забыт, один чистый повтор.
                logger.info(f"RSS {source['name']}: HTTP {e.code} на условный запрос — повторяю без If-*")
                cache.pop(src_id, None)
                _save_rss_cache(cache)
                feed = _read_and_parse({k: v for k, v in headers.items() if k not in conditional})
            else:
                raise
        if not feed.entries:
            return {"error": "Нет записей", "articles": 0}
        articles = []
        for entry in feed.entries[:limit]:
            article_id = hashlib.md5(entry.get('link', str(time.time())).encode()).hexdigest()[:16]
            # Страница статьи качается отдельно на каждую запись. Уже
            # сохранённое не перекачиваем: иначе каждые 10 минут уходят
            # сотни запросов на текст, который лежит на диске.
            article_path = os.path.join(ARTICLES_DIR, f"{article_id}.json")
            if os.path.exists(article_path):
                continue
            article = _fetch_full_article(entry, download_media)
            article['id'] = article_id
            article['source_id'] = src_id
            article['source_name'] = source['name']
            article['source_icon'] = source.get('icon', '📰')
            article['category'] = source.get('category', 'news')
            if download_media and article.get('media'):
                article['media'] = _download_media_files(article, article_id)
            _save_json(article_path, article)
            _articles_ingest(os.path.basename(article_path), article)
            articles.append(article)
        _forget_feed_failure(cache, src_id)
        return {"ok": True, "articles": len(articles), "feed_title": feed.get('feed',{}).get('title','')}
    except Exception as e:
        rec = _remember_feed_failure(cache, src_id, time.time())
        if rec['until']:
            logger.warning(f"Feed fetch error {source['name']}: {e} — пауза "
                           f"{int((rec['until'] - time.time()) / 60)} мин после {rec['fails']} неудач")
        else:
            logger.error(f"Feed fetch error {source['name']}: {e}")
        return {"error": str(e), "articles": 0}

def fetch_all_feeds(limit=50, download_media=True):
    results = []
    for source in get_rss_sources():
        if not source.get('enabled', True):
            continue
        try:
            if source.get('type', 'rss') == 'site':
                res = fetch_site_articles(source, min(limit, 30), download_media)
            else:
                res = fetch_feed_articles(source, limit, download_media)
        except Exception as e:
            swallow.note('news_module.fetch_all_feeds:L1141', e)
            res = {"error": str(e), "articles": 0}
        res['source'] = source['name']
        results.append(res)
    return results

# ===================== ЗАГРУЗКА САЙТОВ (НЕ RSS) =====================

def _title_is_junk(title):
    t = str(title or '').strip()
    if not t:
        return True
    if len(t) < 15:
        return True
    low = t.lower()
    if re.search(r'\d+\s*(минут\w*|час\w*|дн\w+|секунд\w*|недел\w*)\s*назад', low):
        return True
    if low.startswith(('комментари', 'голос', 'просмотр', 'подписаться', 'войти', 'регистрация', 'в избран')):
        return True
    if re.search(r'^[\d\W\s]{1,20}$', t):
        return True
    return False

def _fetch_article_from_url(url, link_title=''):
    """Извлекает статью напрямую с веб-страницы (заголовок, текст, медиа)."""
    article = {
        'title': link_title,
        'summary': '',
        'content': '',
        'link': url,
        'published': '',
        'author': '',
        'media': [],
        'fetched_at': time.time(),
    }
    if NEWSPAPER_AVAILABLE:
        try:
            a = newspaper.article(url, language='ru')
            a.download()
            a.parse()
            text = (a.text or '').strip()
            if text:
                article['content'] = '<p>' + '</p>\n\n<p>'.join(
                    p for p in re.split(r'\n+', text) if p.strip()
                ) + '</p>'
                if (a.title or '').strip():
                    article['title'] = a.title.strip()
                if a.authors:
                    article['author'] = a.authors[0]
                if a.publish_date:
                    article['published'] = str(a.publish_date)
                if a.top_image:
                    article['media'].append({"type": "image", "url": a.top_image})
                for img in list(a.images)[:5]:
                    if img not in [m['url'] for m in article['media']]:
                        article['media'].append({"type": "image", "url": img})
        except Exception as e:
            logger.debug(f"newspaper site fetch failed {url}: {e}")

    if not article['content'] or len(article['content']) < 200:
        try:
            br = _fetch_page_with_beautifulsoup(url)
            content = br.get('content', '') if isinstance(br, dict) else br
            if content and len(content) > len(article.get('content', '')):
                article['content'] = content
            if isinstance(br, dict) and br.get('image') and not article.get('media'):
                article['media'].append({"type": "image", "url": br['image']})
            if isinstance(br, dict) and br.get('title') and not _title_is_junk(br['title']):
                if not article.get('title') or _title_is_junk(article['title']):
                    article['title'] = br['title']
            if not article.get('title'):
                try:
                    import requests as _req
                    import bs4
                    res = _req.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
                    res.encoding = res.apparent_encoding or 'utf-8'
                    s = bs4.BeautifulSoup(res.text, 'html.parser')
                    og = s.find('meta', property='og:title')
                    if og and og.get('content'):
                        article['title'] = og['content']
                    elif s.title and s.title.get_text(strip=True):
                        article['title'] = s.title.get_text(strip=True)[:200]
                    elif s.h1 and s.h1.get_text(strip=True):
                        article['title'] = s.h1.get_text(strip=True)[:200]
                except Exception as e:
                    logger.debug(f"site title fetch failed {url}: {e}")
        except Exception as e:
            logger.debug(f"site content fetch failed {url}: {e}")

    if not article['title'] or _title_is_junk(article['title']):
        if article.get('summary'):
            s = article['summary'].split('.')[0].strip()[:100]
            if len(s) >= 15 and not _title_is_junk(s):
                article['title'] = s
    if not article['title'] or _title_is_junk(article['title']):
        article['title'] = (url.rsplit('/', 1)[-1].replace('-', ' ').replace('_', ' ').strip() or url)

    if article['content']:
        article['content'] = re.sub(r'<script[^>]*>.*?</script>', '', article['content'], flags=re.DOTALL)
        article['content'] = re.sub(r'<style[^>]*>.*?</style>', '', article['content'], flags=re.DOTALL)
        article['content'] = re.sub(r'<iframe[^>]*>.*?</iframe>', '', article['content'], flags=re.DOTALL)
        plain = re.sub(r'<[^>]+>', ' ', article['content'])
        plain = re.sub(r'\s+', ' ', plain).strip()
        if plain:
            article['summary'] = _build_excerpt(plain, 5, 500)

    article['lang'] = _detect_language(f"{article['title']} {article.get('summary', '')}")
    return article

def fetch_site_articles(source, limit=30, download_media=True):
    """Собирает статьи с обычного сайта: парсит ссылки с главной и загружает каждую."""
    if not BEAUTIFULSOUP_AVAILABLE:
        return {"error": "BeautifulSoup не установлен", "articles": 0}
    try:
        import requests as _req
        import bs4
        from urllib.parse import urljoin, urlparse

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        }
        resp = _req.get(source.get('url'), headers=headers, timeout=20, allow_redirects=True)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "articles": 0}
        try:
            resp.encoding = resp.apparent_encoding or 'utf-8'
        except Exception:
            swallow.note('news_module.fetch_site_articles:L1270')
            resp.encoding = 'utf-8'

        try:
            soup = bs4.BeautifulSoup(resp.text, 'lxml')
        except Exception:
            swallow.note('news_module.fetch_site_articles:L1276')
            soup = bs4.BeautifulSoup(resp.text, 'html.parser')

        page = urlparse(source.get('url'))
        base_domain = page.netloc.lower().replace('www.', '')

        noise_paths = ('/tag/', '/category/', '/categories/', '/login', '/register', '/signin',
                       '/signup', '/search', '/author/', '/about', '/contact', '/privacy',
                       '/terms', '/rss', '/feed', '/archive/', '/sitemap', '/policies',
                       '/comments', '/hubs', '/awards', '/profile', '/users/', '/vacanc',
                       '/meetings', '/qa/', '/polls', '/feedback', '/help', '/settings',
                       '/kek', '/auth', '/docs/', '/sandbox', '/legal', '/faq', '/career',
                       '/jobs', '/advert', '/partner', '/special', '/events', '/newsletters')
        skip_exts = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.mp4', '.pdf', '.zip', '.css', '.js', '.xml')

        links = []
        seen_links = set()
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            if not href or href.startswith(('#', 'mailto:', 'javascript:', 'tel:')):
                continue
            abs_url = urljoin(source.get('url'), href).split('#')[0]
            up = urlparse(abs_url)
            if up.scheme not in ('http', 'https'):
                continue
            domain = up.netloc.lower().replace('www.', '')
            if domain != base_domain:
                continue
            if abs_url in seen_links:
                continue
            low = up.path.lower()
            if any(abs_url.lower().endswith(ext) for ext in skip_exts):
                continue
            if any(sp in low for sp in noise_paths):
                continue
            last_seg = up.path.rstrip('/').rsplit('/', 1)[-1].lower()
            if last_seg in ('articles', 'posts', 'news', 'blog', 'blogs', 'companies',
                            'tags', 'category', 'categories', 'archive', 'page', 'index',
                            'authors', 'search', 'feed', 'rss', 'forum', 'forums',
                            'video', 'videos', 'photos', 'images', 'topics', 'topic',
                            'about', 'home', 'main', 'review', 'reviews'):
                continue
            if up.query and not up.path:
                continue
            seen_links.add(abs_url)
            score = 0
            if any(sp in low for sp in ('/news', '/article', '/story', '/post', '/entry')):
                score += 2
            if re.search(r'/(19|20)\d{2}/', low):
                score += 2
            if re.search(r'\d{2}[-\/]\d{2}[-\/]\d{2,4}', low):
                score += 2
            if len(a.get_text(strip=True)) > 15:
                score += 1
            score += min(len(low), 8)
            links.append({'url': abs_url, 'score': score, 'title': a.get_text(strip=True)})

        links.sort(key=lambda x: -x['score'])
        selected = []
        seen_url = set()
        for l in links:
            if l['url'] in seen_url:
                continue
            seen_url.add(l['url'])
            selected.append(l)
            if len(selected) >= limit:
                break

        src_id = source['id']
        articles = []
        for link in selected:
            art = _fetch_article_from_url(link['url'], link.get('title', ''))
            if not art.get('content') or len(art.get('content')) < 300:
                continue
            if not art.get('title'):
                art['title'] = link.get('title', 'Без заголовка')
            article_id = hashlib.md5(link['url'].encode()).hexdigest()[:16]
            art['id'] = article_id
            art['source_id'] = src_id
            art['source_name'] = source.get('name', '')
            art['source_icon'] = source.get('icon', '🌐')
            art['category'] = source.get('category', 'news')
            if download_media and art.get('media'):
                art['media'] = _download_media_files(art, article_id)
            _save_json(os.path.join(ARTICLES_DIR, f"{article_id}.json"), art)
            _articles_ingest(f"{article_id}.json", art)
            articles.append(art)
            if len(articles) >= limit:
                break

        return {"ok": True, "articles": len(articles), "source": source.get('name', ''), "type": "site"}
    except Exception as e:
        logger.error(f"Site fetch error {source.get('name')}: {e}")
        return {"error": str(e), "articles": 0}

_auto_refresh_started = False

def start_auto_refresh(interval_minutes=None):
    """Запускает фоновое автообновление RSS (однократно).

    Раньше каждый вызов порождал новый поток с бесконечным циклом — при
    ленивом запуске из обработчика это плодило бы дубликаты. Теперь
    идемпотентно, как start_digest_scheduler().
    """
    global _auto_refresh_started
    if _auto_refresh_started:
        return False
    _auto_refresh_started = True
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _fetch_source(src):
        if src.get('type', 'rss') == 'site':
            return fetch_site_articles(src, min(10, 30), False)
        return fetch_feed_articles(src, 10, False)
    def _auto_fetch():
        while True:
            interval = interval_minutes or get_news_settings().get('auto_refresh_minutes') or 10
            time.sleep(interval * 60)
            logger.info(f"Автообновление ({interval} мин)...")
            sources = [s for s in get_rss_sources() if s.get('enabled', True)]
            with ThreadPoolExecutor(max_workers=min(len(sources), 4)) as pool:
                futures = {pool.submit(_fetch_source, s): s for s in sources}
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        logger.error(f"Auto-fetch error: {e}")
            logger.info("Автообновление завершено")
    threading.Thread(target=_auto_fetch, daemon=True, name="NewsAutoRefresh").start()
    logger.info("Планировщик автообновления новостей запущен")
    return True

# ===================== ЧТЕНИЕ НОВОСТЕЙ =====================

def _norm_article_link(link):
    """Нормализует URL статьи для дедупликации."""
    if not link:
        return ''
    return str(link).strip().strip('/').lower()

# ---- Кэш скана статей: не читать и не парсить все JSON-файлы на каждый запрос ----
_articles_scan_cache = {"sig": None, "gen": None, "articles": None}
# fname -> (mtime_ns, size, статья) — разобранный файл повторно не читается.
_articles_parsed = {}
_articles_scan_lock = threading.Lock()
# Поколение набора статей. Горячий запрос сверяет его с кэшем и НЕ трогает
# диск: на живой базе 43 426 файлов обход каталога стоит 120 мс, и именно он
# давал всплески /api/news/stats до 350 мс каждые несколько секунд. Поколение
# растёт, когда набор изменился: наша запись статьи (см. _articles_ingest) или
# расхождение, найденное фоновой ревизией (внешние правки каталога).
_articles_gen = 0
# Ревизия каталога раз в минуту в фоне: ловит удалённые/перезаписанные извне
# файлы, которые наш код не видел. В запросах скана нет вовсе.
_REVISE_INTERVAL = 60.0
_revise_lock = threading.Lock()
_revise_next = 0.0
# Сборка списка статей — одна за раз (см. _all_articles_cached).
_articles_build_lock = threading.Lock()
# Прогресс холодной сборки: пока идёт первый проход по тысячам файлов,
# запросы получают уже разобранную часть вместо многосекундного ожидания.
# Обновляется под _articles_scan_lock.
_articles_progress = {"done": 0, "total": 0, "articles": []}
_SIG_TTL = 3.0   # сек; список файлов + stat тысяч статей — дорогая операция
_COLD_WAIT_S = 20.0   # сколько ждать первую сборку, прежде чем отдать частичное
# Один обход каталога на всех. Без этого каждый поток делал свой полный
# scandir: замер 2026-11-02 — 8 одновременных вызовов = 8 сканов, каждый
# растягивался с 54 мс до 1,8 с, и всплеск бил по ВСЕМ ручкам (даже
# /api/health 427 мс), а не только по новостям.
_sig_build_lock = threading.Lock()
_SIG_WAIT_S = 10.0   # дольше держать запрос на чужом скане нечего
_sig_cache = {"at": 0.0, "sig": None}
_STATS_TTL = 10.0
_stats_cache = {"at": 0.0, "data": None}

def _articles_dir_sig():
    """Сигнатура каталога статей (имя, mtime, size) — дёшево vs полный парсинг.

    Результат кэшируется на _SIG_TTL: /api/news/stats и списки статей дергают
    сигнатуру часто и раньше делали os.listdir + os.stat по каждому файлу
    (тысячи системных вызовов на запрос).

    scandir вместо listdir+stat: DirEntry.stat() на Windows берёт mtime и размер
    из уже прочитанного потока поиска по каталогу, отдельный системный запрос не
    делается. Замер
    на живом каталоге (28 675 файлов): 2.59 с → 0.082 с, список сигнатур
    бит-в-бит совпадает; симлинков в каталоге нет, а follow-семантика та же
    (d.stat() по умолчанию = os.stat(path)).
    """
    now = time.monotonic()
    with _articles_scan_lock:
        if _sig_cache["sig"] is not None and now - _sig_cache["at"] < _SIG_TTL:
            return _sig_cache["sig"]
        stale = _sig_cache["sig"]          # что уже известно на этот момент
    # Сканирует ровно один поток. Остальные ждут его результат: отдать им
    # устаревшую подпись нельзя (новые статьи остались бы невидимы дольше
    # TTL), а считать свой скан — это и был всплеск выше. Ждём не дольше
    # _SIG_WAIT_S: после этого честнее посчитать самому, чем держать запрос.
    if not _sig_build_lock.acquire(timeout=_SIG_WAIT_S):
        return stale if stale is not None else []
    entries = None
    try:
        with _articles_scan_lock:
            # Пока ждали, скан мог завершить другой поток: его результат и
            # есть актуальный, второй обход не нужен.
            if _sig_cache["sig"] is not None and time.monotonic() - _sig_cache["at"] < _SIG_TTL:
                return _sig_cache["sig"]
        entries = _sig_scan()
        return entries
    finally:
        if entries is not None:
            with _articles_scan_lock:
                _sig_cache["sig"] = entries
                _sig_cache["at"] = time.monotonic()
        _sig_build_lock.release()


def _sig_scan():
    """Один обход каталога статей: [(имя, mtime_ns, размер)] по возрастанию имени."""
    entries = []
    try:
        with os.scandir(ARTICLES_DIR) as it:
            for d in it:
                if not d.name.endswith('.json'):
                    continue
                try:
                    st = d.stat()
                    entries.append((d.name, st.st_mtime_ns, st.st_size))
                except OSError:
                    swallow.note('news_module._sig_scan:L1504')
                    pass
    except OSError:
        swallow.note('news_module._sig_scan:L1507')
        pass
    entries.sort()
    return entries

# ---- Индекс разобранных статей на диске ------------------------------------
# Холодный процесс раньше заново читал и парсил каждый файл: на живой базе
# (12,5 тыс. файлов, 57 МБ) первый проход держал запросы 144–503 с. Индекс
# хранит ровно то, что уже разобрано, вместе с mtime/size каждого файла,
# поэтому после рестарта читаются только изменившиеся файлы. Файл пишется в
# фоне и не чаще раза в полчаса: запись 50 МБ на каждом автообновлении была
# бы дороже самого парсинга.
_ARTICLES_INDEX_FILE = os.path.join(NEWS_DIR, "articles_index.json")
_INDEX_VERSION = 1
_INDEX_SAVE_MIN_INTERVAL = 1800.0
_index_state = {"loaded": False, "saving": False, "last_save": 0.0}


def _articles_index_load():
    """Разобранные статьи с диска: fname -> (mtime_ns, size, статья)."""
    try:
        with open(_ARTICLES_INDEX_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        swallow.note('news_module._articles_index_load:L1531')
        return {}
    except Exception as e:
        logger.debug(f"[news] индекс статей не прочитан: {e}")
        return {}
    if not isinstance(raw, dict) or raw.get("version") != _INDEX_VERSION:
        return {}
    items = raw.get("items")
    if not isinstance(items, dict):
        return {}
    out = {}
    for fname, rec in items.items():
        # Проверяем форму, а не доверяем файлу: битая запись должна просто
        # выпасть и перечитаться из настоящего JSON статьи.
        if not (isinstance(fname, str) and isinstance(rec, list) and len(rec) == 3):
            continue
        mtime_ns, size, article = rec
        if not isinstance(mtime_ns, int) or not isinstance(size, int):
            continue
        if not isinstance(article, dict):
            continue
        out[fname] = (mtime_ns, size, article)
    return out


def _articles_index_save_async():
    """Сохранить индекс в фоне; повторный запуск при занятой записи пропускается."""
    now = time.time()
    with _articles_scan_lock:
        if _index_state["saving"]:
            return
        if now - _index_state["last_save"] < _INDEX_SAVE_MIN_INTERVAL:
            return
        snap = {fname: [rec[0], rec[1], rec[2]]
                for fname, rec in _articles_parsed.items()}
        _index_state["saving"] = True
        _index_state["last_save"] = now

    def _go():
        tmp = _ARTICLES_INDEX_FILE + ".tmp"
        try:
            os.makedirs(os.path.dirname(_ARTICLES_INDEX_FILE), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": _INDEX_VERSION, "saved_at": now,
                           "items": snap}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _ARTICLES_INDEX_FILE)
        except Exception as e:
            logger.debug(f"[news] индекс статей не записан: {e}")
        finally:
            with _articles_scan_lock:
                _index_state["saving"] = False

    threading.Thread(target=_go, daemon=True, name="news-index-save").start()


def _articles_build():
    """Один проход по каталогу: перечитать изменившиеся статьи и собрать список.

    Инкрементально: перечитываем только те файлы, у которых изменились mtime
    или размер. Прежняя версия заново парсила все 28 тысяч JSON при любом
    новом фиде — замер на живом каталоге: 15.5 с первого запроса после
    обновления ленты вместо 3.2 с уже прогретым кэшем.

    Первый проход в процессе подхватывает индекс, записанный прошлым
    процессом, и потому читает с диска только изменившиеся файлы.
    """
    # Поколение снимается ДО скана: статья, записанная во время прохода,
    # даст _articles_gen больше сохранённого, и следующий запрос пересоберёт
    # список. Иначе кэш считал бы себя актуальным без только что сохранённой
    # статьи.
    with _articles_scan_lock:
        gen0 = _articles_gen
    if not _index_state["loaded"]:
        _index_state["loaded"] = True
        seed = _articles_index_load()          # чтение диска — вне замка
        if seed:
            with _articles_scan_lock:
                for fname, rec in seed.items():
                    _articles_parsed.setdefault(fname, rec)
            logger.info(f"[news] индекс статей: {len(seed)} файлов уже разобрано")
    sig = _articles_dir_sig()
    total = len(sig)
    with _articles_scan_lock:
        _articles_progress.update({"done": 0, "total": total, "articles": []})
    articles, parsed = [], {}
    t0 = last_pub = time.monotonic()
    for i, (fname, mtime_ns, size) in enumerate(sig, 1):
        old = _articles_parsed.get(fname)
        if old is not None and old[0] == mtime_ns and old[1] == size:
            article = old[2]
        else:
            article = _load_json(os.path.join(ARTICLES_DIR, fname))
        # Пустой результат (_load_json отдаёт {} для битого файла) тоже
        # индексируется: иначе такой файл перечитывается на каждом проходе.
        parsed[fname] = (mtime_ns, size, article)
        if article:
            articles.append(article)
        if i % 256 == 0 or i == total:
            now = time.monotonic()
            # Долгая сборка отдаёт уже разобранное: вкладка, открытая в это
            # время, не должна висеть до конца прохода.
            if now - last_pub >= 2.0:
                last_pub = now
                snapshot = sorted(articles,
                                  key=lambda a: a.get('fetched_at', 0), reverse=True)
                with _articles_scan_lock:
                    _articles_progress["done"] = i
                    _articles_progress["articles"] = snapshot
    articles.sort(key=lambda a: a.get('fetched_at', 0), reverse=True)
    with _articles_scan_lock:
        _articles_parsed.clear()
        _articles_parsed.update(parsed)
        _articles_scan_cache["sig"] = sig
        _articles_scan_cache["gen"] = gen0
        _articles_scan_cache["articles"] = articles
        _articles_progress.update({"done": total, "total": total, "articles": []})
    logger.debug(f"[news] сборка статей: {total} файлов за "
                 f"{time.monotonic() - t0:.2f} с")
    _articles_index_save_async()
    return articles


def _all_articles_cached():
    """Список всех статей; перестройка — ровно одна на изменение каталога.

    Без single-flight холодный процесс платил за каждый concurrent вход своим
    полным проходом: замер 2026-10-26 на pid 10372 (~3 мин после старта) —
    первый `/api/offline/news` 206 862 мс, в py-spy три потока одновременно
    внутри `_all_articles_cached → _load_json` (стартовый прогрев
    `news-cache-warm`, воркер запроса и `NewsAutoRefresh`). В прогретом
    процессе те же 349 010 байт стоят 131–264 мс.

    Кто приходит, когда сборка уже идёт и прежний список есть, — получает
    прежний, а не ждёт до конца прохода: лента на минуту старше для этой
    страницы ничего не теряет, а зависшая вкладка — теряет. Если прежнего
    списка нет вовсе (холодный процесс), ждать обязательно: пустая лента
    вместо настоящих статей была бы потерянной функцией.

    Актуальность определяется поколением, а не сканом каталога: на живой базе
    (43 426 файлов) обход стоил 120 мс на каждый запрос, из-за чего лента
    регулярно вылезала за 200 мс. Диск трогает только сборка и фоновая
    ревизия — по одному разу на изменение.
    """
    _maybe_revise_articles()   # ленивый пинок сверки: проверка — два сравнения
    with _articles_scan_lock:
        if (_articles_scan_cache["articles"] is not None
                and _articles_scan_cache["gen"] == _articles_gen):
            return list(_articles_scan_cache["articles"])
        prev = _articles_scan_cache["articles"]
    hold = True
    if prev is not None:
        hold = _articles_build_lock.acquire(blocking=False)
        if not hold:
            return list(prev)
    else:
        # Холодный процесс: ждём первую сборку, но не бесконечно. Как только
        # сборка опубликовала разобранную часть, отдаём её, а совсем без
        # данных — пустой список по истечении _COLD_WAIT_S. Бесконечное
        # ожидание здесь означало вкладку, висящую 144–503 с.
        deadline = time.monotonic() + _COLD_WAIT_S
        hold = False
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            if _articles_build_lock.acquire(timeout=min(2.0, remain)):
                hold = True
                break
            with _articles_scan_lock:
                partial = list(_articles_progress["articles"])
            if partial:
                return partial
        if not hold:
            with _articles_scan_lock:
                return list(_articles_progress["articles"])
    try:
        with _articles_scan_lock:
            if (_articles_scan_cache["articles"] is not None
                    and _articles_scan_cache["gen"] == _articles_gen):
                return list(_articles_scan_cache["articles"])
        return list(_articles_build())
    finally:
        if hold:
            _articles_build_lock.release()


def _maybe_revise_articles():
    """Раз в _REVISE_INTERVAL запустить сверку каталога с кэшем — в фоне.

    Нужна только для изменений, которых наш код не совершал (файл удалили
    или переписали снаружи). Свои записи кэш обновляет через
    _articles_ingest без скана, поэтому в запросах диск не трогается вовсе.
    """
    global _revise_next
    now = time.monotonic()
    if _revise_next == 0.0:
        # Первую сверку откладываем на интервал: сразу после старта кэш
        # только что собран из каталога, а фоновый обход рядом со стартовым
        # прогревом забирал бы GIL у первых запросов.
        _revise_next = now + _REVISE_INTERVAL
        return
    if now < _revise_next:
        return
    if not _revise_lock.acquire(blocking=False):
        return
    _revise_next = now + _REVISE_INTERVAL
    threading.Thread(target=_articles_revise_task, daemon=True,
                     name="NewsArticlesRevise").start()


def _articles_revise_task():
    """Обёртка фоновой ревизии: снять _revise_lock и не дать потоку умереть."""
    try:
        _articles_revise()
    except Exception as e:
        logger.debug("[news] фоновая ревизия статей: %s", e)
    finally:
        _revise_lock.release()


def _articles_revise():
    """Сверить каталог с кэшем; при расхождении — пересобрать список."""
    global _articles_gen
    sig = _sig_scan()
    with _articles_scan_lock:
        if _articles_scan_cache["sig"] == sig:
            return
        # Набор изменился не через наш код: новое поколение заставит
        # пересчитаться и статистику, даже если список пересоберётся позже.
        _articles_gen += 1
    if not _articles_build_lock.acquire(blocking=False):
        return
    try:
        _articles_build()
    finally:
        _articles_build_lock.release()


def _stats_ingest_locked(old, new, sources_map, sources_sig, gen):
    """Дельта агрегатов статистики на одну статью. Вызывать под _articles_scan_lock.

    Копия вместо правки на месте: читатели берут ссылку из _stats_cache и
    копируют словари без замка, а менять размер словаря у них под руками
    значило бы «dictionary changed size during iteration».
    """
    s = _stats_cache["data"]
    if s is None:
        return
    s = {"total_articles": s["total_articles"],
         "categories": dict(s["categories"]),
         "sources": dict(s["sources"]),
         "regions": dict(s["regions"])}
    for art, sign in ((old, -1), (new, 1)):
        if not art:
            continue
        s["total_articles"] += sign
        cat = art.get('category', 'news')
        s["categories"][cat] = s["categories"].get(cat, 0) + sign
        if s["categories"][cat] <= 0:
            s["categories"].pop(cat, None)
        sid = art.get('source_id', '')
        if sid:
            s["sources"][sid] = s["sources"].get(sid, 0) + sign
            if s["sources"][sid] <= 0:
                s["sources"].pop(sid, None)
            reg = sources_map.get(sid, {}).get('region', 'other')
            s["regions"][reg] = s["regions"].get(reg, 0) + sign
            if s["regions"][reg] <= 0:
                s["regions"].pop(reg, None)
    _stats_cache.update({"at": time.monotonic(), "data": s,
                         "sig": (gen, sources_sig)})


def _articles_ingest(fname, article):
    """Порционно вставить/обновить статью в прогретом кэше — без скана каталога.

    Автообновление и ручная подкачка сохраняют статьи пачками. Прежний путь
    после каждой записи ждал истечения _SIG_TTL и платил 120 мс на обход
    43 тыс. файлов. Здесь статья встаёт на своё место в уже отсортированном
    списке, а счётчики статистики правятся на дельту — то есть новые новости
    добавляются в прогретый сервер по одной, без общего пересчёта.
    """
    global _articles_gen
    if not article:
        return
    try:
        st = os.stat(os.path.join(ARTICLES_DIR, fname))
    except OSError:
        swallow.note('news_module._articles_ingest:L1821')
        return
    rec = (st.st_mtime_ns, st.st_size, article)
    with _articles_scan_lock:
        _articles_parsed[fname] = rec
        hot = (_articles_scan_cache["articles"] is not None
               and _articles_scan_cache["gen"] == _articles_gen)
        _articles_gen += 1
        # Своё поколение, а не глобальное: параллельная запись могла поднять
        # счётчик, пока мы шли к замку сборки. Привязать кэш к чужому номеру
        # значило бы объявить список актуальным без чужой статьи.
        my_gen = _articles_gen
    if not hot:
        # Кэш ещё холодный: файл уже на диске, обычная сборка его возьмёт,
        # а расхождение поколений заставит её начаться.
        return
    if not _articles_build_lock.acquire(blocking=False):
        return   # идёт сборка — она либо увидит файл, либо список пересоберётся
    try:
        # Источники и их сигнатура — до замка: это чтение файла, а под
        # _articles_scan_lock держать ввод-вывод нельзя.
        with _articles_scan_lock:
            need_stats = _stats_cache["data"] is not None
        sources_map, sources_sig = {}, None
        if need_stats:
            try:
                sources_map = {x['id']: x for x in get_rss_sources()}
            except Exception:
                swallow.note('news_module._articles_ingest:L1849')
                sources_map = {}
            sources_sig = _sources_sig()
        with _articles_scan_lock:
            arts = _articles_scan_cache["articles"]
            if arts is None:
                return
            old = None
            for i, a in enumerate(arts):
                if a.get("id") == article.get("id"):
                    old = arts.pop(i)
                    break
            ts = article.get("fetched_at") or 0
            pos = len(arts)
            while pos > 0 and (arts[pos - 1].get("fetched_at") or 0) < ts:
                pos -= 1
            arts.insert(pos, article)
            _articles_scan_cache["gen"] = my_gen
            if need_stats:
                _stats_ingest_locked(old, article, sources_map, sources_sig,
                                     my_gen)
    finally:
        _articles_build_lock.release()


def get_articles(category=None, source_id=None, limit=100, offset=0, query=None, region=None):
    """Первые (offset+limit) статьи после фильтра и дедупликации по ссылке.

    Замер 2026-11-02 на живой базе (42 981 статья): прежняя версия делала ДВА
    полных прохода по всему списку — собирала отфильтрованный список, потом
    дедуплицировала его целиком, — и на каждый `/api/news/articles` уходило
    206–242 мс. Но за ответом нужны только первые offset+limit уникальных
    записей, а список уже отсортирован по fetched_at: дальше идти незачем.
    Фильтр и дедупликация свёрнуты в один проход с ранним выходом, порядок и
    первый-по-ссылке ровно те же, что раньше.
    """
    need = offset + limit
    if need <= 0:
        return []
    # sources_map нужен только фильтру по региону, а get_rss_sources() читает
    # и разбирает JSON источников — на каждый обычный запрос это впустую.
    sources_map = None
    q = query.lower() if query else None
    seen = set()
    out = []
    for article in _all_articles_cached():
        if category and article.get('category') != category:
            continue
        if source_id and article.get('source_id') != source_id:
            continue
        if region:
            if sources_map is None:
                sources_map = {s['id']: s for s in get_rss_sources()}
            src = sources_map.get(article.get('source_id'), {})
            if src.get('region') != region:
                continue
        if q:
            full = f"{article.get('title','')} {article.get('summary','')} {article.get('content','')}".lower()
            if q not in full:
                continue
        key = _norm_article_link(article.get('link'))
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(article)
        if len(out) >= need:
            break
    return out[offset:]

def get_article(article_id):
    return _load_json(os.path.join(ARTICLES_DIR, f"{article_id}.json"))

def _sources_sig():
    """(mtime_ns, размер) файла источников.

    Нужен потому, что sources/regions в статистике считаются не только от статей,
    но и от того, какие фиды включены и в каком регионе: без этой половины ключа
    переиспользование по одному каталогу статей врало бы после правок источников.
    """
    try:
        st = os.stat(RSS_SOURCES_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        swallow.note('news_module._sources_sig:L1933')
        return None

def _stats_view(s):
    return {"total_articles": s["total_articles"],
            "categories": dict(s["categories"]),
            "sources": dict(s["sources"]),
            "regions": dict(s["regions"])}


def _stats_compute():
    sources_map = {s['id']: s for s in get_rss_sources()}
    stats = {"total_articles": 0, "categories": {},
             "sources": {}, "regions": {}}
    for article in _all_articles_cached():
        stats["total_articles"] += 1
        cat = article.get('category', 'news')
        stats["categories"][cat] = stats["categories"].get(cat, 0) + 1
        src_id = article.get('source_id', '')
        if src_id:
            stats["sources"][src_id] = stats["sources"].get(src_id, 0) + 1
            src = sources_map.get(src_id, {})
            reg = src.get('region', 'other')
            stats["regions"][reg] = stats["regions"].get(reg, 0) + 1
    return stats


_stats_build_lock = threading.Lock()
_stats_next_kick = 0.0


def _stats_refresh_in_background(sig):
    """Пересчитать агрегаты не в заявителе, а в одном фоновом проходе.

    Полная агрегация 28 тысяч словарей внутри запроса при параллельных окнах
    держала слот десятками секунд: в журнале за сессию /api/news/stats — 91
    запрос и 5 766 с удержания (p50 33 с), /api/news/articles — 114 запросов
    и 7 165 с. Прежние агрегаты при этом есть и не пустые, поэтому показать
    их и обновить следом — не потеря функции, а задержка ровно на один пересчёт.
    """
    global _stats_next_kick
    now = time.monotonic()
    if now < _stats_next_kick:
        return
    if not _stats_build_lock.acquire(blocking=False):
        return   # пересчёт уже идёт — заводить второй смысла нет
    _stats_next_kick = now + 5.0

    def _run():
        global _stats_next_kick
        try:
            stats = _stats_compute()
            with _articles_scan_lock:
                # Пока шла холодная сборка, список статей мог быть частичным.
                # Кэшировать такой агрегат нельзя: при неизменённом поколении
                # частичные числа остались бы навсегда.
                if (_articles_scan_cache["articles"] is not None
                        and _articles_scan_cache["gen"] == sig[0]):
                    _stats_cache.update({"at": time.monotonic(),
                                         "data": stats, "sig": sig})
        except Exception as e:
            _stats_next_kick = time.monotonic() + 30.0
            logger.warning("[news] фоновый пересчёт статистики не удался: %s", e)
        finally:
            _stats_build_lock.release()

    threading.Thread(target=_run, daemon=True, name="NewsStatsRefresh").start()


def get_article_stats():
    # Статистика нужна для виджета (опрос раз в 5 мин) — 10с кэша достаточно,
    # зато не пересчитываем тысячи словарей на каждый запрос. Ключ — поколение
    # набора статей и сигнатура источников: скана каталога здесь нет, он стоил
    # 120 мс и давал всплески на каждом истечении TTL.
    now = time.monotonic()
    with _articles_scan_lock:
        cached = dict(_stats_cache)
        gen = _articles_gen
        complete = _articles_scan_cache["articles"] is not None
    s = cached["data"]
    if s is not None and now - cached["at"] < _STATS_TTL:
        return _stats_view(s)
    sig = (gen, _sources_sig())
    if s is None:
        # Холодный процесс показать нечего: считаем здесь. Отдать пустую
        # статистику вместо настоящих счётчиков было бы потерянной функцией.
        stats = _stats_compute()
        with _articles_scan_lock:
            # Кэшируем только полный агрегат: во время холодной сборки
            # _all_articles_cached мог отдать часть, и такие числа при
            # неизменённом поколении застряли бы в кэше навсегда.
            if (_articles_scan_cache["articles"] is not None
                    and _articles_gen == gen):
                _stats_cache.update({"at": time.monotonic(),
                                     "data": stats, "sig": sig})
        s = stats
    elif cached.get("sig") == sig:
        # TTL истёк, но с тех пор ни набор статей, ни источники не
        # менялись: агрегаты те же. Без этой ветки каждые 10 с
        # прогонялся полный цикл по десяткам тысяч словарей (замер в живом
        # процессе: всплеск 0.31 с CPU на один /api/news/stats).
        with _articles_scan_lock:
            _stats_cache.update({"at": time.monotonic(), "data": s, "sig": sig})
    elif complete:
        _stats_refresh_in_background(sig)
    return _stats_view(s)

def get_categories():
    custom_cats = _load_json(os.path.join(CATEGORIES_DIR, 'custom.json'), [])
    custom_map = {c['id']: c for c in custom_cats}
    stats = get_article_stats()
    cat_counts = stats['categories']
    cats = {}
    for s in get_rss_sources():
        cat_id = s.get('category', 'news')
        if cat_id not in cats:
            if cat_id in custom_map:
                cats[cat_id] = custom_map[cat_id].copy()
                cats[cat_id]['count'] = cat_counts.get(cat_id, 0)
                cats[cat_id]['is_custom'] = True
            else:
                cats[cat_id] = {"id": cat_id, "name": cat_id, "count": cat_counts.get(cat_id, 0), "icon": s.get('icon','📰'), "color":"#58a6ff", "is_custom": False}
    for c in custom_cats:
        if c['id'] not in cats:
            cats[c['id']] = {**c, 'count': cat_counts.get(c['id'],0), 'is_custom': True}
    for cat_id, count in cat_counts.items():
        if cat_id not in cats:
            cats[cat_id] = {"id": cat_id, "name": cat_id, "count": count, "icon":"📰","color":"#58a6ff","is_custom": False}
    return sorted(cats.values(), key=lambda x: (not x.get('is_custom'), x['name']))

def add_custom_category(name, icon="📁", color="#58a6ff"):
    custom_cats = _load_json(os.path.join(CATEGORIES_DIR, 'custom.json'), [])
    cat_id = hashlib.md5(f"{name}{time.time()}".encode()).hexdigest()[:12]
    custom_cats.append({"id": cat_id, "name": name, "icon": icon, "color": color, "created_at": time.time()})
    _save_json(os.path.join(CATEGORIES_DIR, 'custom.json'), custom_cats)
    return {"ok": True, "id": cat_id}

def delete_custom_category(cat_id):
    custom_cats = _load_json(os.path.join(CATEGORIES_DIR, 'custom.json'), [])
    before = len(custom_cats)
    custom_cats = [c for c in custom_cats if c['id'] != cat_id]
    if len(custom_cats) < before:
        _save_json(os.path.join(CATEGORIES_DIR, 'custom.json'), custom_cats)
        sources = get_rss_sources()
        for s in sources:
            if s.get('category') == cat_id:
                s['category'] = 'news'
        _save_json(RSS_SOURCES_FILE, sources)
        return {"ok": True}
    return {"ok": False}

def get_related_articles(article_id, category=None, limit=4):
    """Похожие статьи той же категории — из прогретого списка.

    Прежняя версия читала и разбирала ВСЕ json-файлы каталога на каждый
    запрос: на живой базе (43 426 файлов) `/api/news/article/related`
    выполнялся 19.9 с. Список уже разобран и отсортирован по fetched_at,
    поэтому фильтрация по категории сохраняет прежний порядок.
    """
    current = get_article(article_id)
    if not current:
        return []
    cat = category or current.get('category', 'news')
    articles = [a for a in _all_articles_cached()
                if a and a.get('id') != article_id and a.get('category') == cat]
    return articles[:limit]

# ===================== ПЕРСОНАЛИЗАЦИЯ: ЛАЙКИ / ЧТЕНИЯ / УМНАЯ ПОДБОРКА =====================

_UID_RE = re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')

_RU_STOPWORDS = set("""
и в во не на я ты он она оно мы вы они это как так что бы по с из у к о а но да нет если при для от до об уже ещё еще только же более менее самый при почти вся во всем всех всего она его ее их этим этот эти та то тем своим самим ваш вашему вами наших моя мои мой мое им же ну вот или над под без между перед после около вместо
a an the and or but if then else for with without from of to in on at by as is are was were be been being do does did have has had it its this that these those we you they he she them their our not no so very just can will would could should may might must about into through over under again further once here there when where why how all any both each few more most other some such only own same than too now
""".split())

_EN_STOPWORDS = _RU_STOPWORDS

def _sanitize_uid(uid):
    """Нормализует идентификатор пользователя (защита от path traversal)."""
    uid = (uid or '').strip()
    if not uid or not _UID_RE.match(uid):
        return 'default'
    return uid


def _profile_uid(uid, request=None):
    """Разделить персональные данные новостей по профилю браузера.

    Для не-default профилей uid получает префикс профиля:
        <profile_id>_<uid>
    Так лайки/чтения/дайджест одного профиля не видны другим профилям.
    """
    uid = _sanitize_uid(uid)
    try:
        if request is not None:
            import profiles_module as PM
            profile = PM.resolve_profile_id(
                request.cookies.get(PM.COOKIE_NAME, "") or "")
            if profile and profile != "default":
                uid = f"{_sanitize_uid(profile)}_{uid}"
    except Exception:
        swallow.note('news_module._profile_uid:L2135')
        pass
    return uid

def _user_dir(uid):
    uid = _sanitize_uid(uid)
    d = os.path.join(USERS_DIR, uid)
    os.makedirs(d, exist_ok=True)
    return d

def _get_likes(uid):
    return _load_json(os.path.join(_user_dir(uid), 'likes.json'), {})

def _save_likes(uid, data):
    with _USER_DATA_LOCK:
        return _save_json(os.path.join(_user_dir(uid), 'likes.json'), data)

def _get_reads(uid):
    return _load_json(os.path.join(_user_dir(uid), 'reads.json'), {})

def _save_reads(uid, data):
    with _USER_DATA_LOCK:
        return _save_json(os.path.join(_user_dir(uid), 'reads.json'), data)

def set_article_like(uid, article_id, liked=True):
    """Ставит/снимает лайк статьи (история лайков сохраняется)."""
    uid = _sanitize_uid(uid)
    if not article_id:
        return {"ok": False, "error": "Нет article_id"}
    likes = _get_likes(uid)
    existing = likes.get(article_id) or {}
    existing['active'] = bool(liked)
    if not existing.get('liked_at'):
        existing['liked_at'] = time.time()
    likes[article_id] = existing
    _save_likes(uid, likes)
    active = sum(1 for v in likes.values() if v.get('active'))
    return {"ok": True, "liked": bool(liked), "total": active}

def record_article_read(uid, article_id):
    """Фиксирует факт прочтения статьи."""
    uid = _sanitize_uid(uid)
    if not article_id:
        return {"ok": False}
    reads = _get_reads(uid)
    rec = reads.get(article_id) or {"read_count": 0}
    rec['read_count'] = rec.get('read_count', 0) + 1
    rec['read_at'] = time.time()
    reads[article_id] = rec
    _save_reads(uid, reads)
    return {"ok": True}

def reset_user_data(uid):
    """Полностью очищает персональные данные (лайки и чтения)."""
    uid = _sanitize_uid(uid)
    with _USER_DATA_LOCK:
        _save_json(os.path.join(_user_dir(uid), 'likes.json'), {})
        _save_json(os.path.join(_user_dir(uid), 'reads.json'), {})
    return {"ok": True}

def _iter_articles():
    """Итерируется по всем сохранённым статьям (из прогретого списка).

    Чтение и разбор каждого файла каталога на вызов стоили секунды: дайджест
    на живой базе (43 426 статей) собирался 21 с. Список уже разобран в
    _all_articles_cached и обновляется при записи статей.
    """
    return iter(_all_articles_cached())

def _tokenize(text, min_len=3):
    """Упрощённая токенизация без стоп-слов (ru/en)."""
    if not text:
        return []
    words = re.findall(r'[a-zа-яё]{3,}', text.lower(), flags=re.IGNORECASE)
    return [w for w in words if w not in _RU_STOPWORDS and w not in _EN_STOPWORDS]

_HALF_LIFE = 14 * 86400  # 14 дней — полураспад интереса

def _decay(ts, now):
    age = max(0, now - (ts or now))
    return 0.5 ** (age / _HALF_LIFE)

def build_user_interest(uid='default'):
    """Собирает профиль интересов из лайков (вес 3) и чтений (вес 1) с затуханием."""
    uid = _sanitize_uid(uid)
    now = time.time()
    likes = _get_likes(uid)
    reads = _get_reads(uid)
    sources_map = {s['id']: s for s in get_rss_sources()}

    weighted = {}
    for aid, rec in likes.items():
        if rec.get('active'):
            weighted[aid] = weighted.get(aid, 0.0) + 3.0 * _decay(rec.get('liked_at'), now)
    for aid, rec in reads.items():
        weighted[aid] = weighted.get(aid, 0.0) + 1.0 * _decay(rec.get('read_at'), now)

    cat_w, src_w, reg_w, lang_w, tok_w = {}, {}, {}, {}, {}
    for aid, w in weighted.items():
        art = get_article(aid)
        if not art:
            continue
        cat = art.get('category') or 'news'
        cat_w[cat] = cat_w.get(cat, 0.0) + w
        sid = art.get('source_id') or ''
        if sid:
            src_w[sid] = src_w.get(sid, 0.0) + w
            reg = sources_map.get(sid, {}).get('region', '')
            if reg:
                reg_w[reg] = reg_w.get(reg, 0.0) + w
        lang = art.get('lang') or 'other'
        lang_w[lang] = lang_w.get(lang, 0.0) + w
        for t in _tokenize(f"{art.get('title','')} {art.get('summary','')}"):
            tok_w[t] = tok_w.get(t, 0.0) + w

    return {
        'categories': cat_w,
        'sources': src_w,
        'regions': reg_w,
        'langs': lang_w,
        'keywords': tok_w,
        'liked_count': sum(1 for v in likes.values() if v.get('active')),
        'read_count': len(reads),
    }

def _top_weighted(mapping, n=5):
    return [{'name': k, 'weight': round(v, 2)} for k, v in sorted(mapping.items(), key=lambda x: -x[1])[:n]]

_REGION_NAMES = {"russian": "Россия", "international": "Мир", "other": "Другое"}

def _interest_summary(int_, sources_map):
    """Собирает компактную сводку интересов для UI (имена вместо id)."""
    src_list = _top_weighted(int_.get('sources', {}))
    for item in src_list:
        src = sources_map.get(item['name'], {})
        item['name'] = src.get('name', item['name'])
    reg_list = _top_weighted(int_.get('regions', {}))
    for item in reg_list:
        item['name'] = _REGION_NAMES.get(item['name'], item['name'])
    return {
        'categories': _top_weighted(int_.get('categories', {})),
        'sources': src_list,
        'regions': reg_list,
        'keywords': _top_weighted(int_.get('keywords', {}), 12),
        'liked_count': int_.get('liked_count', 0),
        'read_count': int_.get('read_count', 0),
    }

def _score_article(art, cats, srcs, regs, langs, top_tokens, sources_map, now):
    """Считает персональный скор статьи и причины рекомендации."""
    reasons = []
    score = 0.0
    cat = art.get('category') or 'news'
    cw = cats.get(cat, 0.0)
    if cw > 0:
        score += min(cw, 4.0)
        reasons.append(f"Категория «{cat}» в ваших интересах")
    sid = art.get('source_id') or ''
    sw = srcs.get(sid, 0.0)
    if sw > 0:
        score += min(sw, 4.0) * 0.8
        src_name = art.get('source_name') or sources_map.get(sid, {}).get('name', 'этот источник')
        reasons.append(f"Вы часто читаете «{src_name}»")
    reg = sources_map.get(sid, {}).get('region', '') if sid else ''
    if reg:
        rw = regs.get(reg, 0.0)
        if rw > 0:
            score += min(rw, 4.0) * 0.5
    lang = art.get('lang') or 'other'
    lw = langs.get(lang, 0.0)
    if lw > 0:
        score += min(lw, 4.0) * 0.4
    # Токенизация нужна только ради пересечения с интересами. У профиля без
    # истории интересов (частый случай) top_tokens пуст, и разбор заголовка
    # с аннотацией у десятков тысяч статей был чистой потерей: 0.5 с из 0.6 с
    # всего расчёта подборки.
    overlap = []
    if top_tokens:
        text_tokens = set(_tokenize(f"{art.get('title','')} {art.get('summary','')}"))
        overlap = [t for t in top_tokens if t in text_tokens]
    if overlap:
        score += min(len(overlap), 10) * 0.6
        reasons.append("Похожие темы: " + ', '.join(overlap[:3]))
    age_days = max(0, (now - (art.get('fetched_at') or now)) / 86400)
    score += 0.5 * (2.71828 ** (-age_days / 3))
    if age_days < 1:
        reasons.append("Свежая новость")
    return score, reasons, overlap

def _build_excerpt(text, sentences=5, max_chars=400):
    """Строит выдержку из первых N предложений текста."""
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', ' ', str(text))
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return ''
    parts = [p.strip() for p in re.split(r'(?<=[.!?…])\s+', text) if p.strip()]
    res = []
    chars = 0
    for p in parts:
        if len(res) >= sentences:
            break
        if chars + len(p) > max_chars and res:
            break
        res.append(p)
        chars += len(p)
    excerpt = ' '.join(res)
    if not excerpt and parts:
        excerpt = parts[0][:max_chars]
    return excerpt

def _article_excerpt(art, sentences=5, max_chars=400):
    """Выдержка статьи: контент → сводка."""
    if not art:
        return ''
    return _build_excerpt(art.get('content') or art.get('summary') or '', sentences, max_chars)

def _plain_article_text(art):
    """Полный текст статьи без HTML-тегов."""
    raw = art.get('content') or ''
    if not raw:
        raw = art.get('summary') or ''
    plain = re.sub(r'<[^>]+>', ' ', str(raw))
    return re.sub(r'\s+', ' ', plain).strip()

def _laya_profile_text(int_, sources_map):
    """Текстовое описание интересов пользователя (уходит в инструкции laya)."""
    cats = [c['name'] for c in _top_weighted(int_.get('categories', {}), 5)]
    srcs = []
    for s in _top_weighted(int_.get('sources', {}), 5):
        srcs.append(sources_map.get(s['name'], {}).get('name') or s['name'])
    kws = [k['name'] for k in _top_weighted(int_.get('keywords', {}), 12)]
    parts = []
    if cats:
        parts.append("любимые темы: " + ", ".join(cats))
    if srcs:
        parts.append("частые источники: " + ", ".join(srcs))
    if kws:
        parts.append("ключевые слова: " + ", ".join(kws))
    return "; ".join(parts) or "интересы не определены"


def _laya_rerank(scored, int_, sources_map, top_n=25):
    """Семантический переранкер (laya) поверх базового скора.

    Базовый алгоритм (_score_article) — лексический: он не понимает, что
    «нейросеть» и «ИИ» — одно и то же, и не видит интерес к англоязычным
    статьям. laya добавляет семантику и кросс-язычность.

    Возвращает (scored, laya_info). Если фича выключена, движок недоступен или
    ответ не пришёл — возвращается исходный scored, то есть поведение
    полностью совпадает с текущим.
    """
    try:
        import laya_client
    except Exception:
        swallow.note('news_module._laya_rerank:L2392')
        return scored, {}
    if not laya_client.feature("news_semantic") or not scored:
        return scored, {}
    cand = scored[:top_n]
    profile = _laya_profile_text(int_, sources_map)
    topics = [c['name'] for c in _top_weighted(int_.get('categories', {}), 8)] or ['новости']
    states = []
    for _s, _r, _o, art in cand:
        states.append(("%s. %s" % (art.get('title', ''), (art.get('summary') or '')[:400])).strip())
    questions = {
        "topic": {
            "type": "choice",
            "instructions": "К какой теме относится эта новость?",
            "criteria": {t: t for t in topics},
        },
        "relevance": {
            "type": "score",
            "instructions": ("Насколько новость соответствует интересам пользователя? "
                             "Интересы: " + profile),
            "criteria": ["не соответствует", "слабо", "средне", "сильно", "полностью"],
        },
        "is_ad": {
            "type": "noul",
            "instructions": "Это реклама или пресс-релиз?",
        },
    }
    try:
        results = laya_client.decide_batch(states, questions)
    except Exception as e:
        logger.debug("[laya] rerank failed: %s", e)
        return scored, {}
    if not results or len(results) != len(cand):
        return scored, {}
    max_base = max((c[0] for c in cand), default=0.0) or 1.0
    info = {}
    out = []
    for (base, reasons, overlap, art), res in zip(cand, results):
        ans = (res or {}).get("answers") or {}
        rel_ans = ans.get("relevance") or {}
        try:
            rel = float(rel_ans.get("score") or 0.0) / 4.0
        except Exception:
            swallow.note('news_module._laya_rerank:L2435')
            rel = 0.0
        topic = (ans.get("topic") or {}).get("choice")
        ad = (ans.get("is_ad") or {}).get("noul")
        try:
            conf = float(rel_ans.get("answer_confidence") or rel_ans.get("confidence") or 0.0)
        except Exception:
            swallow.note('news_module._laya_rerank:L2442')
            conf = 0.0
        final = (base / max_base) * 0.6 + rel * 0.4
        if ad is not None and ad >= 0.7:
            final *= 0.3
        new_reasons = list(reasons)
        if topic:
            new_reasons.append("Тема: %s" % topic)
        aid = art.get('id')
        if aid:
            info[aid] = {"base": round(base, 3), "laya": round(final, 3),
                         "relevance": round(rel, 2), "topic": topic,
                         "is_ad": ad, "confidence": round(conf, 3)}
        out.append((final, new_reasons, overlap, art))
    # Хвост (кандидаты за top_n) сохраняем как есть, но с базовым скором.
    out.extend(scored[top_n:])
    out.sort(key=lambda x: -x[0])
    return out, info


_digest_cache = {}
_digest_lock = threading.Lock()
_DIGEST_TTL = 300.0


def _interests_sig(uid):
    """Отпечаток интересов пользователя: лайки и чтения — второй вход подборки.

    Поколение статей покрывает только корпус. Оно не меняется, когда
    пользователь лайкает статью, а подборка от этого меняется сразу.
    """
    sig = []
    for name in ("likes.json", "reads.json"):
        try:
            st = os.stat(os.path.join(_user_dir(uid), name))
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            swallow.note('news_module._interests_sig:L2479')
            sig.append(None)
    return tuple(sig)


def compute_digest(uid='default', window_days=7, limit=10):
    """Персональная подборка; повторный запрос отдаётся из кэша.

    Каждый вход в подборку заново скорил десятки тысяч статей и дергал
    семантический переранкер: на живой базе 21 с до кэша статей и ~2.4 с
    после него. Ключ включает поколение статей, сигнатуру источников и
    отпечаток лайков/чтений, поэтому подборка меняется сразу, как меняются
    интересы, и не устаревает молча.
    """
    uid = _sanitize_uid(uid)
    window_days = max(1, min(int(window_days or 7), 90))
    limit = max(1, min(int(limit or 10), 50))
    key = (uid, window_days, limit, _articles_gen, _sources_sig(),
           _interests_sig(uid))
    with _digest_lock:
        entry = _digest_cache.get(key)
        if entry is not None and time.monotonic() - entry["at"] < _DIGEST_TTL:
            return entry["data"]
    data = _compute_digest(uid, window_days, limit)
    with _digest_lock:
        _digest_cache[key] = {"at": time.monotonic(), "data": data}
        while len(_digest_cache) > 32:
            _digest_cache.pop(next(iter(_digest_cache)))
    return data


def _compute_digest(uid='default', window_days=7, limit=10):
    """Строит персональную умную подборку новостей по интересам пользователя."""
    uid = _sanitize_uid(uid)
    window_days = max(1, min(int(window_days or 7), 90))
    limit = max(1, min(int(limit or 10), 50))
    int_ = build_user_interest(uid)
    cats, srcs, regs, langs, tokw = int_['categories'], int_['sources'], int_['regions'], int_['langs'], int_['keywords']
    sources_map = {s['id']: s for s in get_user_sources(uid)}
    now = time.time()
    cutoff = now - window_days * 86400
    top_tokens = [t for t, w in sorted(tokw.items(), key=lambda x: -x[1])[:40]]

    interest = _interest_summary(int_, sources_map)

    read_ids = set(_get_reads(uid).keys())
    like_ids = {k for k, v in _get_likes(uid).items() if v.get('active')}

    scored = []
    seen_link = set()
    for art in _iter_articles():
        if not art or not art.get('title') or not art.get('id'):
            continue
        aid = art['id']
        if aid in read_ids or aid in like_ids:
            continue
        # Список отсортирован по fetched_at по убыванию — дальше окна идти
        # незачем. Прежний `continue` пролистывал весь корпус (43 тыс.)
        # ради тех же 12 тыс. свежих.
        if (art.get('fetched_at') or 0) < cutoff:
            break
        link_key = _norm_article_link(art.get('link'))
        if link_key and link_key in seen_link:
            continue
        if link_key:
            seen_link.add(link_key)
        sid_art = art.get('source_id') or ''
        if sid_art and not sources_map.get(sid_art, {}).get('enabled', True):
            continue
        score, reasons, overlap = _score_article(art, cats, srcs, regs, langs, top_tokens, sources_map, now)
        scored.append((score, reasons, overlap, art))

    scored.sort(key=lambda x: -x[0])
    max_score = scored[0][0] if scored else 0.0

    # Семантический переранкер (laya) — только если включён в конфиге.
    # При недоступности движка scored остаётся прежним.
    scored, laya_info = _laya_rerank(scored, int_, sources_map)

    cat_counts, src_counts = {}, {}
    selected = []
    for s, reasons, overlap, art in scored:
        cat = art.get('category') or 'news'
        sid = art.get('source_id') or ''
        if cat_counts.get(cat, 0) >= 3:
            continue
        if sid and src_counts.get(sid, 0) >= 2:
            continue
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if sid:
            src_counts[sid] = src_counts.get(sid, 0) + 1
        excerpt = _article_excerpt(art)
        item = {
            'article': art,
            'score': round(s / max_score * 100) if max_score else 0,
            'reasons': reasons[:3],
            'matched_keywords': overlap[:6],
            'excerpt': excerpt,
            'has_full': len(_plain_article_text(art)) > len(excerpt),
        }
        if laya_info.get(art.get('id')):
            item['laya'] = laya_info.get(art.get('id'))
        selected.append(item)
        if len(selected) >= limit:
            break

    if not selected:
        fallback = [a for a in _iter_articles()
                    if a and a.get('title') and a.get('id')
                    and a['id'] not in read_ids and a['id'] not in like_ids
                    and (a.get('fetched_at') or 0) >= cutoff]
        fallback.sort(key=lambda a: -(a.get('fetched_at') or 0))
        seen_fb = set()
        unique_fb = []
        for a in fallback:
            k = _norm_article_link(a.get('link'))
            if k and k in seen_fb:
                continue
            if k:
                seen_fb.add(k)
            unique_fb.append(a)
        selected = []
        for a in unique_fb[:limit]:
            excerpt = _article_excerpt(a)
            selected.append({
                'article': a,
                'score': 0,
                'reasons': [],
                'matched_keywords': [],
                'excerpt': excerpt,
                'has_full': len(_plain_article_text(a)) > len(excerpt),
            })

    return {
        'digest': selected,
        'interest': interest,
        'generated_at': now,
        'window_days': window_days,
    }

# ===================== РАСПИСАНИЕ УТРЕННЕГО ДАЙДЖЕСТА =====================

def get_digest_settings(uid='default'):
    """Возвращает настройки расписания дайджеста для пользователя."""
    uid = _sanitize_uid(uid)
    all_s = _load_json(DIGEST_SETTINGS_FILE, {}) or {}
    s = all_s.get(uid, {}) or {}
    return {
        'uid': uid,
        'enabled': bool(s.get('enabled', False)),
        'time': s.get('time', '08:00'),
        'interval_days': max(1, min(int(s.get('interval_days', 1) or 1), 30)),
        'window_days': max(1, min(int(s.get('window_days', 1) or 1), 90)),
        'limit': max(1, min(int(s.get('limit', 8) or 8), 30)),
        'last_run': s.get('last_run', ''),
    }

def save_digest_settings(uid='default', enabled=None, time_str=None, interval_days=None,
                         window_days=None, limit=None):
    """Сохраняет настройки расписания дайджеста для пользователя."""
    uid = _sanitize_uid(uid)
    with _USER_DATA_LOCK:
        all_s = _load_json(DIGEST_SETTINGS_FILE, {}) or {}
        s = dict(all_s.get(uid, {}) or {})
        if enabled is not None:
            s['enabled'] = bool(enabled)
        if time_str is not None and re.match(r'^\d{1,2}:\d{2}$', str(time_str).strip()):
            s['time'] = str(time_str).strip()
        if interval_days is not None:
            s['interval_days'] = max(1, min(int(interval_days), 30))
        if window_days is not None:
            s['window_days'] = max(1, min(int(window_days), 90))
        if limit is not None:
            s['limit'] = max(1, min(int(limit), 30))
        all_s[uid] = s
        _save_json(DIGEST_SETTINGS_FILE, all_s)
    return get_digest_settings(uid)

def _clean_digest_text(text):
    """Минимальная очистка ответа LLM для дайджеста: не трогает списки и жирный."""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text.strip()

def _is_llm_error_text(text):
    """True, если LLM вернула сообщение об ошибке вместо полезного текста."""
    t = text.strip().lower()
    return t.startswith(('ошибка', 'error', 'ошибка ai', 'ошибка lm studio',
                         'ошибка подключения', 'ошибка:', 'ошибка: module'))

def _digest_llm_call(prompt):
    """Запрос дайджеста к LLM напрямую (без переводческой чистки).

    Возвращает готовый текст дайджеста или '' при сбое/пустом ответе.
    """
    try:
        import server as _srv
        result = _srv.lm_studio_request(
            [
                {"role": "system", "content": "Ты — новостной ассистент. Составляй краткие дайджесты новостей на русском языке строго по переданному списку: 5-8 пунктов, один-два предложения на пункт. Не придумывай новости, не добавляй код, пояснения, markdown-разметку или нумерацию вне пунктов. Верни только сам дайджест."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=4096,
            enable_thinking=False,
        )
        content = _clean_digest_text(result.get('content') or '')
        reasoning = _clean_digest_text(result.get('reasoning') or '')
        if len(content) > 20:
            text = content
        elif len(reasoning) > 20:
            text = reasoning
        else:
            text = content or reasoning
        if len(text) > 20 and not _is_llm_error_text(text):
            return text
        logger.warning(
            f"AI-дайджест пустой/ошибочный ответ: content_len={len(result.get('content') or '')}, "
            f"reasoning_len={len(result.get('reasoning') or '')}, raw={str(result)[:300]}"
        )
    except Exception as e:
        logger.warning(f"AI-дайджест недоступен: {e}")
    return ""

def _build_digest_text(items):
    """Строит текст дайджеста: AI (если LLM доступна) с локальным fallback."""
    if not items:
        return "Сегодня нет свежих новостей по вашим интересам.", 'empty'
    parts = []
    for i, it in enumerate(items[:10], 1):
        a = it.get('article') or {}
        summary = re.sub(r'<[^>]+>', ' ', a.get('summary') or a.get('content') or '')
        summary = re.sub(r'\s+', ' ', summary).strip()[:220]
        parts.append(f"{i}. {a.get('title', '')} — {summary}")
    prompt = ("Составь персональный дайджест новостей на русском языке: "
              "5-8 маркированных пунктов, по одному-двум предложениям на пункт, "
              "кратко о главном в каждой новости. Верни только дайджест.\n\n"
              + '\n'.join(parts))
    text = _digest_llm_call(prompt)
    if text:
        return text, 'ai'
    local = ["Локальная сводка (LLM недоступна):"]
    for i, it in enumerate(items[:8], 1):
        a = it.get('article') or {}
        local.append(f"{i}. {a.get('title', '')}")
    return '\n'.join(local), 'local'

def _build_digest_text_fast(items):
    """Шаблонный текст дайджеста — вообще без обращения к LLM.

    Тот же набор статей, что и у обычного дайджеста, но текст собирается из
    заголовков и summary самих статей: ни большой модели, ни времени на неё.
    """
    if not items:
        return "Сегодня нет свежих новостей по вашим интересам."
    lines = ["Краткая сводка по вашим интересам:"]
    for i, it in enumerate(items[:10], 1):
        a = it.get('article') or {}
        summary = re.sub(r'<[^>]+>', ' ', a.get('summary') or a.get('content') or '')
        summary = re.sub(r'\s+', ' ', summary).strip()[:200]
        title = (a.get('title') or '').strip()
        src = a.get('source_name') or ''
        head = "%d. %s" % (i, title) + (f" ({src})" if src else "")
        lines.append(head + (f" — {summary}" if summary else ""))
    return "\n".join(lines)


def build_fast_briefing(uid='default', window_days=1, limit=10):
    """Утренний брифинг без большой LLM.

    Отбор статей делает существующий бесплатный скоринг (лайки/прочтения,
    свежесть, ключевые слова), а если включён laya.news_semantic — ещё и
    семантический переранкер. Текст собирается шаблоном, поэтому ни GPU, ни
    LLM не нужны.
    """
    digest = compute_digest(uid, window_days, limit)
    items = digest.get('digest') or []
    out_items = []
    for it in items:
        a = it.get('article') or {}
        row = {
            'id': a.get('id', ''),
            'title': a.get('title', ''),
            'source_name': a.get('source_name', ''),
            'link': a.get('link', ''),
            'excerpt': it.get('excerpt', ''),
            'score': it.get('score', 0),
            'reasons': it.get('reasons', []),
        }
        if it.get('laya'):
            row['laya'] = it['laya']
        out_items.append(row)
    return {
        'ok': True,
        'mode': 'fast',
        'text': _build_digest_text_fast(items),
        'items': out_items,
        'interest': digest.get('interest'),
        'generated_at': digest.get('generated_at'),
        'window_days': digest.get('window_days'),
    }


def _run_digest_for_uid(uid, mark_run=True):
    """Генерирует утренний дайджест для uid и отправляет уведомление в desktop."""
    settings = get_digest_settings(uid)
    if not settings['enabled']:
        return {"ok": False, "error": "Расписание отключено"}
    digest = compute_digest(uid, settings['window_days'], settings['limit'])
    items = digest.get('digest', [])
    text, mode = _build_digest_text(items)

    body_lines = []
    for i, it in enumerate(items[:5], 1):
        a = it.get('article') or {}
        body_lines.append(f"{i}. {a.get('title', '')[:120]}")
    if len(items) > 5:
        body_lines.append(f"…и ещё {len(items) - 5} новостей")
    body = '\n'.join(body_lines) or 'Новостей пока нет'

    notified = False
    try:
        from modules.notification_engine import add_notification
        items_data = []
        for it in items[:8]:
            a = it.get('article') or {}
            items_data.append({
                'id': a.get('id', ''),
                'title': a.get('title', ''),
                'image': (a.get('media') or [{}])[0].get('url', '') if a.get('media') else '',
                'link': a.get('link', ''),
                'excerpt': it.get('excerpt', ''),
            })
        payload = json.dumps({
            "title": "🌅 Утренний дайджест",
            "body": body,
            "items": items_data,
            "aiText": text,
            "mode": mode,
        }, ensure_ascii=False)
        add_notification(text=payload, module="news", type="news-update")
        notified = True
    except Exception as e:
        logger.error(f"[DIGEST] Не удалось отправить уведомление: {e}")

    if mark_run:
        with _USER_DATA_LOCK:
            all_s = _load_json(DIGEST_SETTINGS_FILE, {}) or {}
            s = dict(all_s.get(uid, {}) or {})
            s['last_run'] = time.strftime('%Y-%m-%d')
            all_s[uid] = s
            _save_json(DIGEST_SETTINGS_FILE, all_s)

    return {
        "ok": True,
        "uid": uid,
        "items": len(items),
        "mode": mode,
        "notified": notified,
        "time": settings['time'],
        "digest": digest,
        "text": text,
    }

def _digest_scheduler_loop():
    """Фоновый поток: раз в минуту проверяет расписания всех пользователей."""
    while True:
        time.sleep(60)
        try:
            now_str = time.strftime('%H:%M', time.localtime())
            all_s = _load_json(DIGEST_SETTINGS_FILE, {}) or {}
            for uid, s in all_s.items():
                if not s.get('enabled'):
                    continue
                if s.get('time') != now_str:
                    continue
                interval = max(1, min(int(s.get('interval_days', 1) or 1), 30))
                last = s.get('last_run', '')
                if last:
                    try:
                        from datetime import date
                        delta = (date.today() - date.fromisoformat(last)).days
                    except Exception:
                        swallow.note('news_module._digest_scheduler_loop:L2863')
                        delta = 999
                    if delta < interval:
                        continue
                logger.info(f"[DIGEST] Планировщик: утренний дайджест для {uid}")
                try:
                    _run_digest_for_uid(uid)
                except Exception as e:
                    logger.error(f"[DIGEST] Ошибка генерации для {uid}: {e}")
        except Exception as e:
            logger.error(f"[DIGEST] Ошибка планировщика: {e}")

_digest_scheduler_started = False

def start_digest_scheduler():
    """Запускает фоновый планировщик утреннего дайджеста (однократно)."""
    global _digest_scheduler_started
    with _USER_DATA_LOCK:
        if _digest_scheduler_started:
            return False
        _digest_scheduler_started = True
    threading.Thread(target=_digest_scheduler_loop, daemon=True, name="NewsDigestScheduler").start()
    logger.info("Планировщик утреннего дайджеста запущен")
    return True

# ===================== ВСПОМОГАТЕЛЬНЫЕ =====================

def _load_json(filepath, default=None):
    if default is None:
        default = {}
    # на Windows подмена файла (os.replace в _save_json) на мгновение отдаёт
    # PermissionError, а незакрытая запись — пустой файл: и то и другое лечится
    # коротким повтором, а не молчаливым уходом на default с потерью данных
    for attempt in range(4):
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if filepath.endswith("custom.json") and not isinstance(data, list):
                    return []
                return data
            return default
        except (PermissionError, json.JSONDecodeError) as e:
            if attempt == 3:
                swallow.note('news_module._load_json:L2901', e)
                break
            time.sleep(0.05 * (attempt + 1))
        except Exception as e:
            swallow.note('news_module._load_json:L2911', e)
            break
    return default

def _save_json(filepath, data):
    tmp = None
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        # запись в тот же файл с заменой: читатель никогда не увидит пустой или
        # оборванный файл (иначе json.load даёт JSONDecodeError «char 0» и берёт default,
        # а следующий overwrite теряет настоящие данные)
        tmp = f"{filepath}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, filepath)
        tmp = None
        return True
    except Exception as e:
        logger.error(f"Save JSON error: {e}")
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                swallow.note('news_module._save_json:L2934')
                pass
        return False

# ===================== HTML-ШАБЛОН =====================

PAGE_NEWS_TEMPLATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "page_news.html")
_I18N_SCRIPT_TAG = '<script src="/static/js/i18n.js"></script>'
_PAGE_NEWS_FALLBACK = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Новости — Infinity</title>
</head>
<body style="font-family:sans-serif;padding:40px;text-align:center">
<h2>&#9888;&#65039; Шаблон новостей не найден</h2>
<p>Отсутствует файл templates/page_news.html</p>
</body>
</html>"""

def _load_news_page():
    """Загружает страницу новостей из templates/page_news.html (единый источник UI).

    Общий слой (design.css, тема, панель окна, диалоги) подключается здесь же:
    лента 2.0 собирает палитру из design.css, и /api/news/, отданный без него,
    выглядел бы как неокрашенная разметка. inject() идемпотентен по маркерам,
    поэтому повторная обработка страницы безопасна.
    """
    try:
        with open(PAGE_NEWS_TEMPLATE_FILE, "r", encoding="utf-8") as f:
            html = f.read()
        if _I18N_SCRIPT_TAG not in html and "</head>" in html:
            html = html.replace("</head>", _I18N_SCRIPT_TAG + "\n</head>")
        try:
            import webui_assets
            html = webui_assets.inject(html)
        except Exception as e:
            logger.warning(f"Общий слой для страницы новостей не подключён: {e}")
        return html
    except Exception as e:
        logger.warning(f"Не удалось загрузить шаблон новостей: {e}")
        return _PAGE_NEWS_FALLBACK

PAGE_NEWS = _load_news_page()

# ===================== FASTAPI ОБРАБОТЧИКИ =====================

@router.get("/articles")
async def api_articles(
    request: Request,
    category: str = Query(None),
    source_id: str = Query(None),
    limit: int = Query(50),
    offset: int = Query(0),
    q: str = Query(None, alias="q"),
    region: str = Query(None)
):
    # Чтение/фильтрация всех статей — блокирующая операция, выносим из
    # event loop, чтобы не замораживать HTTP-сервер.
    import asyncio
    articles = await asyncio.to_thread(
        get_articles, category, source_id, limit, offset, q, region
    )
    # Фильтр по пер-профильным источникам: показываем только включённые
    uid = _profile_uid(None, request)
    try:
        enabled_ids = {s['id'] for s in get_user_sources(uid) if s.get('enabled', True)}
        articles = [a for a in articles if not a.get('source_id') or a['source_id'] in enabled_ids]
    except Exception:
        swallow.note('news_module.api_articles:L2992')
        pass
    return {"articles": articles}

@router.get("/article")
def api_article(id: str = Query(...)):
    article = get_article(id)
    if not article:
        return JSONResponse({"error": "Article not found"}, status_code=404)
    return article

@router.get("/article/related")
async def api_related_articles(id: str = Query(...), limit: int = Query(4)):
    import asyncio
    article = await asyncio.to_thread(get_article, id)
    if not article:
        return {"articles": []}
    related = await asyncio.to_thread(
        get_related_articles, id, article.get('category'), limit
    )
    return {"articles": related}

@router.get("/stats")
async def api_stats():
    # Тяжёлое чтение тысяч JSON-файлов — НЕ в event loop (иначе зависает
    # весь сервер), а в рабочем потоке.
    import asyncio
    return await asyncio.to_thread(get_article_stats)

@router.get("/categories")
def api_categories():
    return {"categories": get_categories()}

@router.post("/categories/add")
async def api_add_category(request: Request):
    data = await request.json()
    return add_custom_category(data['name'], data.get('icon','📁'), data.get('color','#58a6ff'))

@router.post("/categories/delete")
async def api_delete_category(request: Request):
    data = await request.json()
    return delete_custom_category(data['id'])

@router.get("/sources")
def api_sources(request: Request):
    uid = _profile_uid(None, request)
    return {"sources": get_user_sources(uid)}

@router.post("/sources/add")
async def api_add_source(request: Request):
    d = await request.json()
    uid = _profile_uid(None, request)
    name = d.get('name', '').strip()
    url = (d.get('url') or '').strip()
    if not name or not url:
        return {"ok": False, "error": "Укажите название и URL"}
    data = _user_sources(uid)
    known = {s.get('url', '').strip().lower() for s in get_user_sources(uid)}
    if url.lower() in known:
        return {"ok": False, "error": "Источник уже добавлен"}
    source_id = hashlib.md5(url.encode()).hexdigest()[:12]
    source = {"id": source_id, "name": name, "url": url,
              "category": d.get('category', 'news'), "enabled": True,
              "icon": d.get('icon', '📰'), "type": 'site' if d.get('type') == 'site' else 'rss',
              "added_at": time.time()}
    if d.get('color'):
        source['color'] = d['color']
    data.setdefault('custom', []).append(source)
    _save_user_sources(uid, data)
    return {"ok": True, "id": source_id}

@router.get("/sources/probe")
def api_probe_source(url: str = Query(...)):
    """Определяет тип источника по URL: rss (найден фид) или site."""
    feeds = discover_rss_from_url(url)
    if feeds:
        return {"type": "rss", "feed_url": feeds[0]['url'], "title": feeds[0].get('title', ''), "feeds": feeds}
    return {"type": "site", "feed_url": None, "title": None, "feeds": []}

@router.post("/sources/remove")
async def api_remove_source(request: Request):
    data = await request.json()
    uid = _profile_uid(None, request)
    us = _user_sources(uid)
    sid = data['id']
    if sid in {s['id'] for s in get_rss_sources()}:
        removed = set(us.get('removed', []) or [])
        removed.add(sid)
        us['removed'] = sorted(removed)
    us['custom'] = [c for c in us.get('custom', []) if c.get('id') != sid]
    _save_user_sources(uid, us)
    return {"ok": True}

@router.post("/sources/remove-batch")
async def api_remove_sources_batch(request: Request):
    data = await request.json()
    ids = data.get('ids', [])
    uid = _profile_uid(None, request)
    us = _user_sources(uid)
    removed = set(us.get('removed', []) or [])
    removed.update(ids)
    us['removed'] = sorted(removed)
    us['custom'] = [c for c in us.get('custom', []) if c.get('id') not in ids]
    _save_user_sources(uid, us)
    return {"ok": True, "removed": len(ids)}

@router.post("/sources/remove-all")
def api_remove_all_sources(request: Request):
    uid = _profile_uid(None, request)
    us = _user_sources(uid)
    us['removed'] = sorted(s['id'] for s in get_rss_sources())
    us['custom'] = []
    us['enabled'] = {}
    _save_user_sources(uid, us)
    return {"ok": True}

@router.post("/sources/reset-defaults")
def api_reset_defaults(request: Request):
    uid = _profile_uid(None, request)
    _save_user_sources(uid, {"enabled": {}, "removed": [], "custom": [], "categories": {}})
    return {"ok": True}

@router.post("/sources/toggle")
async def api_toggle_source(request: Request):
    data = await request.json()
    uid = _profile_uid(None, request)
    sid = data.get('source_id', data.get('id', ''))
    if not sid:
        return {"ok": False, "error": "Нет id источника"}
    us = _user_sources(uid)
    us.setdefault('enabled', {})[sid] = bool(data.get('enabled', False))
    _save_user_sources(uid, us)
    return {"ok": True}

@router.post("/sources/update-category")
async def api_update_source_category(request: Request):
    data = await request.json()
    uid = _profile_uid(None, request)
    sid = data.get('id', '')
    category = (data.get('category') or 'news').strip()
    if not sid:
        return {"ok": False}
    us = _user_sources(uid)
    us.setdefault('categories', {})[sid] = category
    _save_user_sources(uid, us)
    return {"ok": True}

@router.post("/fetch")
async def api_fetch_all(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    limit = data.get('limit', 50)
    download_media = data.get('download_media', False)
    FETCH_STATUS['running'] = True
    FETCH_STATUS['finished_at'] = None
    FETCH_STATUS['results'] = []
    def _run():
        try:
            FETCH_STATUS['results'] = fetch_all_feeds(limit, download_media) or []
        except Exception as e:
            swallow.note('news_module._run:L3151', e)
            FETCH_STATUS['results'] = [{"source": "*", "ok": False, "error": str(e), "articles": 0}]
        finally:
            FETCH_STATUS['running'] = False
            FETCH_STATUS['finished_at'] = time.time()
    background_tasks.add_task(_run)
    return {"ok": True, "message": "Обновление запущено в фоновом режиме"}

@router.get("/fetch-status")
def api_fetch_status():
    return dict(FETCH_STATUS)

@router.post("/fetch-source")
async def api_fetch_source(request: Request, background_tasks: BackgroundTasks):
    """Скачать статьи одного источника (для сайтов — парсинг сайта, для RSS — больше записей)."""
    data = await request.json()
    sid = data.get('id', '')
    sources = get_rss_sources()
    source = next((s for s in sources if s.get('id') == sid), None)
    if not source:
        return {"ok": False, "error": "Источник не найден"}
    if not source.get('enabled', True):
        return {"ok": False, "error": "Источник отключён"}
    is_site = source.get('type', 'rss') == 'site'
    limit_raw = data.get('limit', 25)
    try:
        limit = int(limit_raw or 0)
    except (TypeError, ValueError):
        swallow.note('news_module.api_fetch_source:L3179')
        limit = 25
    if is_site:
        limit = 200 if limit <= 0 else max(1, min(limit, 200))
    else:
        limit = 500 if limit <= 0 else max(1, min(limit, 500))
    FETCH_STATUS['running'] = True
    FETCH_STATUS['finished_at'] = None
    FETCH_STATUS['results'] = []
    def _run():
        try:
            if is_site:
                res = fetch_site_articles(source, limit, False)
            else:
                res = fetch_feed_articles(source, limit, False)
            if isinstance(res, dict):
                res = dict(res)
                res.setdefault('source', source.get('name', ''))
                res.setdefault('type', 'site' if is_site else 'rss')
            FETCH_STATUS['results'] = [res or {}]
        except Exception as e:
            swallow.note('news_module._run:L3200', e)
            FETCH_STATUS['results'] = [{"source": source.get('name', ''), "ok": False, "error": str(e), "articles": 0}]
        finally:
            FETCH_STATUS['running'] = False
            FETCH_STATUS['finished_at'] = time.time()
    background_tasks.add_task(_run)
    return {"ok": True, "message": f"Загрузка «{source.get('name', '')}» начата"}

@router.get("/discover")
def api_discover(url: str = Query(...)):
    feeds = discover_rss_from_url(url)
    return {"feeds": feeds}

@router.get("/search")
def api_search_feeds(q: str = Query(...)):
    results = search_rss_feeds(q)
    return {"results": results}

@router.get("/preferences")
def api_preferences(uid: str = Query(None), request: Request = None):
    """Возвращает лайки и чтения пользователя (для рендера сердечек)."""
    uid = _profile_uid(uid, request)
    likes = _get_likes(uid)
    reads = _get_reads(uid)
    int_ = build_user_interest(uid)
    sources_map = {s['id']: s for s in get_rss_sources()}
    return {
        "uid": uid,
        "likes": [k for k, v in likes.items() if v.get('active')],
        "reads": list(reads.keys()),
        "interest": _interest_summary(int_, sources_map),
    }

@router.post("/like")
async def api_like_article(request: Request):
    """Поставить/снять лайк статьи."""
    data = await request.json()
    return set_article_like(_profile_uid(data.get('uid'), request), data.get('article_id'), data.get('liked', True))

@router.post("/read")
async def api_mark_read(request: Request):
    """Зафиксировать прочтение статьи."""
    data = await request.json()
    return record_article_read(_profile_uid(data.get('uid'), request), data.get('article_id'))

@router.get("/digest")
def api_digest(uid: str = Query(None), window_days: int = Query(7), limit: int = Query(10),
                     request: Request = None):
    """Персональная умная подборка новостей по интересам."""
    return compute_digest(_profile_uid(uid, request), window_days, limit)

@router.get("/digest/fast")
async def api_digest_fast(uid: str = Query(None), window_days: int = Query(1), limit: int = Query(10),
                          request: Request = None):
    """Быстрый утренний брифинг без большой LLM (шаблон + локальный скоринг)."""
    try:
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(build_fast_briefing, _profile_uid(uid, request),
                                       window_days, limit)
    except Exception as e:
        logger.error(f"fast digest error: {e}")
        return {"ok": False, "error": str(e)}

@router.post("/digest/reset")
async def api_digest_reset(request: Request):
    """Очистить персональные данные (лайки и чтения)."""
    data = await request.json()
    return reset_user_data(_profile_uid(data.get('uid'), request))

@router.get("/digest/schedule")
def api_digest_schedule(uid: str = Query(None), request: Request = None):
    """Настройки расписания утреннего дайджеста."""
    return get_digest_settings(_profile_uid(uid, request))

@router.post("/digest/schedule")
async def api_digest_schedule_save(request: Request):
    """Сохранить настройки расписания утреннего дайджеста."""
    data = await request.json()
    return save_digest_settings(
        _profile_uid(data.get('uid'), request),
        enabled=data.get('enabled'),
        time_str=data.get('time'),
        interval_days=data.get('interval_days'),
        window_days=data.get('window_days'),
        limit=data.get('limit'),
    )

@router.post("/digest/run-now")
async def api_digest_run_now(request: Request):
    """Немедленно сгенерировать утренний дайджест (тест/проверка)."""
    data = await request.json()
    uid = _profile_uid(data.get('uid'), request)
    settings = get_digest_settings(uid)
    if not settings['enabled']:
        return {"ok": False, "error": "Сначала включите расписание в настройках"}
    return _run_digest_for_uid(uid)

@router.post("/digest/generate-ai")
async def api_digest_generate_ai(request: Request):
    """Генерирует AI-дайджест напрямую через LLM (без переводческой чистки)."""
    data = await request.json()
    prompt = (data.get('text') or '').strip()
    if not prompt:
        return JSONResponse({"ok": False, "error": "Нет текста для дайджеста"}, status_code=400)
    text = _digest_llm_call(prompt)
    if text:
        return {"ok": True, "translated": text, "mode": "ai"}
    return {"ok": False, "translated": "", "mode": "local", "error": "LLM вернула пустой ответ"}

@router.get("/", response_class=HTMLResponse)
def news_page():
    return _load_news_page()

@router.get("/article/fetch-content")
def api_fetch_article_content(id: str = Query(...)):
    """Принудительно загружает полный контент статьи"""
    article = get_article(id)
    if not article:
        return JSONResponse({"error": "Not found"}, status_code=404)

    link = article.get('link')
    if not link:
        return {"ok": False, "error": "Нет ссылки на статью"}

    bs_result = _fetch_page_with_beautifulsoup(link)
    content = bs_result.get('content', '') if isinstance(bs_result, dict) else bs_result
    image = bs_result.get('image', '') if isinstance(bs_result, dict) else ''
    if content and len(content) > 100:
        article['content'] = content
        if image and not article.get('media'):
            article['media'] = [{"type": "image", "url": image}]
        _save_json(os.path.join(ARTICLES_DIR, f"{id}.json"), article)
        _articles_ingest(f"{id}.json", article)
        return {"ok": True, "content": content[:5000], "length": len(content), "image": image}

    return {"ok": False, "error": "Не удалось загрузить контент"}

@router.post("/article/download")
async def api_download_page(request: Request):
    """Скачивает HTML страницу целиком"""
    data = await request.json()
    id = data.get('id')

    article = get_article(id)
    if not article:
        return JSONResponse({"error": "Not found"}, status_code=404)

    link = article.get('link')
    if not link:
        return {"ok": False, "error": "Нет ссылки на статью"}

    result = download_page_html(link, id)
    return result

@router.get("/article/page-html")
def api_get_page_html(id: str = Query(...)):
    """Возвращает HTML скачанной страницы"""
    html = get_downloaded_page(id)
    if html:
        return HTMLResponse(html)
    return JSONResponse({"error": "Страница не скачана"}, status_code=404)

@router.get("/settings")
def api_get_settings():
    """Получить настройки"""
    return get_news_settings()

@router.post("/settings")
async def api_save_settings(request: Request):
    """Сохранить настройки"""
    data = await request.json()
    save_news_settings(data)
    return {"ok": True}

@router.post("/settings/cleanup")
def api_cleanup_downloads():
    """Запустить очистку старых скачиваний"""
    result = cleanup_old_downloads()
    return result
