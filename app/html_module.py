#!/usr/bin/env python3
"""
Модуль для работы с HTML файлами.
Сканирование, индексация, поиск HTML страниц.
"""

import os
import re
import json
import time
import hashlib
from html.parser import HTMLParser
import swallow


class HTMLMetaParser(HTMLParser):
    """Парсер для извлечения метаданных из HTML."""
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self.in_title = False
        self.in_meta = False
        self.meta_name = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            name = attrs_dict.get("name", attrs_dict.get("property", "")).lower()
            if name in ("description", "og:description"):
                self.description = attrs_dict.get("content", "")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title = data.strip()


def extract_html_meta(filepath):
    """Извлечь метаданные из HTML файла."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(50000)  # Первые 50KB для метаданных
        parser = HTMLMetaParser()
        parser.feed(content)
        return parser.title, parser.description
    except Exception:
        swallow.note('html_module.extract_html_meta:L52')
        return "", ""


def extract_html_text(filepath, max_chars=5000):
    """Извлечь текстовое содержимое HTML файла."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Удаляем скрипты и стили
        content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
        content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL)
        # Удаляем HTML теги
        text = re.sub(r'<[^>]+>', ' ', content)
        # Нормализуем пробелы
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except Exception:
        swallow.note('html_module.extract_html_text:L70')
        return ""


def find_html_files(dirs):
    """Найти все HTML файлы в указанных директориях."""
    results = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, subdirs, files in os.walk(d):
            for fname in files:
                if fname.lower().endswith(('.html', '.htm')):
                    fpath = os.path.join(root, fname)
                    rel_path = os.path.relpath(fpath, d)
                    title, desc = extract_html_meta(fpath)
                    size = os.path.getsize(fpath)
                    mtime = os.path.getmtime(fpath)
                    results.append({
                        "path": fpath,
                        "rel_path": rel_path,
                        "dir": d,
                        "filename": fname,
                        "title": title or fname,
                        "description": desc,
                        "size": size,
                        "mtime": mtime,
                        "type": "html"
                    })
    results.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return results


def build_html_tree(dirs):
    """Построить дерево папок с HTML файлами."""
    tree = {"name": "HTML файлы", "children": [], "files": []}
    
    for d in dirs:
        if not os.path.isdir(d):
            continue
        dir_name = os.path.basename(d.rstrip('/'))
        dir_node = {"name": dir_name, "path": d, "children": [], "files": []}
        
        for root, subdirs, files in os.walk(d):
            rel_root = os.path.relpath(root, d)
            
            # Находим родительскую ноду
            parent = dir_node
            if rel_root != '.':
                parts = rel_root.split(os.sep)
                for part in parts:
                    found = None
                    for child in parent["children"]:
                        if child["name"] == part:
                            found = child
                            break
                    if not found:
                        found = {"name": part, "children": [], "files": []}
                        parent["children"].append(found)
                    parent = found
            
            # Добавляем HTML файлы
            for fname in sorted(files):
                if fname.lower().endswith(('.html', '.htm')):
                    fpath = os.path.join(root, fname)
                    title, desc = extract_html_meta(fpath)
                    size = os.path.getsize(fpath)
                    mtime = os.path.getmtime(fpath)
                    parent["files"].append({
                        "path": fpath,
                        "filename": fname,
                        "title": title or fname,
                        "description": desc,
                        "size": size,
                        "mtime": mtime,
                        "type": "html"
                    })
        
        tree["children"].append(dir_node)
    
    return tree


def search_html_files(query, dirs, limit=50):
    """Поиск по HTML файлам."""
    results = find_html_files(dirs)
    if not query:
        return results[:limit]
    
    query_lower = query.lower()
    scored = []
    for item in results:
        # Извлекаем текст для поиска
        text = extract_html_text(item["path"], max_chars=10000)
        text_lower = text.lower()
        title_lower = item["title"].lower()
        
        score = 0
        if query_lower in title_lower:
            score += 100
        if query_lower in item.get("description", "").lower():
            score += 50
        if query_lower in text_lower:
            score += 10
        
        if score > 0:
            preview_start = text_lower.find(query_lower)
            if preview_start >= 0:
                start = max(0, preview_start - 100)
                end = min(len(text), preview_start + len(query) + 200)
                preview = text[start:end]
            else:
                preview = text[:300]
            
            scored.append({
                **item,
                "score": score,
                "preview": preview + ("..." if len(text) > 300 else ""),
                "full_text": text[:5000]
            })
    
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


# Кэш для быстрого доступа
_html_cache = {}
_html_cache_time = 0
_html_cache_ttl = 300  # 5 минут


def get_html_files_cached(dirs, force=False):
    """Получить HTML файлы с кэшированием."""
    global _html_cache, _html_cache_time
    now = time.time()
    cache_key = "|".join(sorted(dirs))
    
    if not force and cache_key in _html_cache and (now - _html_cache_time) < _html_cache_ttl:
        return _html_cache[cache_key]
    
    _html_cache[cache_key] = find_html_files(dirs)
    _html_cache_time = now
    return _html_cache[cache_key]
