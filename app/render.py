#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flibusta Offline Web — веб-приложение для дампа Флибусты (1.4+ ТБ)
Работает напрямую с ZIP-архивами, без распаковки.

Возможности:
- Многопоточное сканирование архивов
- FTS5-поиск (полнотекстовый, мгновенный)
- Обложки книг
- Чтение в браузере (FB2 → HTML)
- Веб-интерфейс для управления индексацией

Использование:
  pip install flask
  python flibusta_web.py scan /путь/к/архивам           # однопоточный импорт
  python flibusta_web.py scan-parallel /путь/к/архивам  # многопоточный импорт
  python flibusta_web.py index /путь/к/архивам          # FTS5-индексация
  python flibusta_web.py web                            # запуск веб-сервера
"""

import os
import re
import sys
import json as json_mod
import zipfile
import sqlite3
import threading
import base64
import html as html_mod
import xml.etree.ElementTree as ET
import concurrent.futures
from pathlib import Path
from io import BytesIO
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache

from flask import (Flask, render_template_string, request, redirect,
                   url_for, send_file, abort, flash, jsonify, Response)
import swallow

# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================
DB_PATH = os.environ.get('FLIBUSTA_DB_PATH') or "flibusta.db"
ARCHIVES_DIR = "archives"
PAGE_SIZE = 50
BATCH_SIZE = 500
MAX_WORKERS = 4

app = Flask(__name__)
app.secret_key = "flibusta_offline_secret"

GENRE_CATEGORIES = [
    ("sf", "Фантастика"),
    ("fantasy", "Фэнтези"),
    ("detective", "Детективы и Триллеры"),
    ("prose", "Проза"),
    ("love", "Любовные романы"),
    ("adv", "Приключения"),
    ("adventure", "Приключения"),
    ("sci", "Наука и Образование"),
    ("children", "Детская литература"),
    ("poetry", "Поэзия"),
    ("dramaturgy", "Драматургия"),
    ("humor", "Юмор"),
    ("nonfiction", "Non-fiction"),
    ("home", "Дом и Семья"),
    ("religion", "Религия и Эзотерика"),
    ("comp", "Компьютеры"),
    ("ref", "Справочная"),
    ("antique", "Античная литература"),
    ("military", "Военная литература"),
    ("russian", "Русская литература"),
    ("foreign", "Зарубежная литература"),
    ("story", "Рассказы"),
    ("novel", "Романы"),
    ("novella", "Повести"),
    ("essay", "Эссе"),
    ("article", "Статьи"),
    ("biography", "Биографии"),
    ("memoir", "Мемуары"),
    ("ya", "Подростковая"),
    ("litrpg", "ЛитРПГ"),
    ("fanfiction", "Фанфик"),
    ("popadantsy", "Попаданцы"),
    ("cyberpunk", "Киберпанк"),
    ("steampunk", "Стимпанк"),
    ("time_travel", "Путешествия во времени"),
    ("postapocalypse", "Постапокалипсис"),
]


def genre_category(code):
    for prefix, name in GENRE_CATEGORIES:
        if code == prefix or code.startswith(prefix + '_'):
            return name
    return "Разное"


RUSSIAN_GENRES = {
    "sf": "Научная фантастика",
    "sf_fantasy": "Фэнтези",
    "sf_horror": "Ужасы",
    "sf_heroic": "Героическая фантастика",
    "sf_detective": "Детективная фантастика",
    "sf_action": "Боевая фантастика",
    "sf_epic": "Эпическая фантастика",
    "sf_cyberpunk": "Киберпанк",
    "sf_space": "Космическая фантастика",
    "sf_social": "Социальная фантастика",
    "sf_humor": "Юмористическая фантастика",
    "sf_techno": "Технотриллер",
    "sf_alternative": "Альтернативная история",
    "sf_history": "Историческая фантастика",
    "sf_postapocalyptic": "Постапокалипсис",
    "sf_litrpg": "ЛитРПГ",
    "sf_magic": "Магический реализм",
    "detective": "Детектив",
    "detective_hard": "Крутой детектив",
    "detective_police": "Полицейский детектив",
    "detective_history": "Исторический детектив",
    "thriller": "Триллер",
    "adv_western": "Вестерн",
    "adv_history": "Исторические приключения",
    "adv_maritime": "Морские приключения",
    "adv_geo": "Географические приключения",
    "adv_indian": "Индейские приключения",
    "adv_animal": "Приключения о животных",
    "prose": "Проза",
    "prose_classic": "Классическая проза",
    "prose_history": "Историческая проза",
    "prose_contemporary": "Современная проза",
    "prose_su_classics": "Советская классическая проза",
    "prose_rus_classic": "Русская классическая проза",
    "love": "Любовные романы",
    "love_contemporary": "Современные любовные романы",
    "love_history": "Исторические любовные романы",
    "love_detective": "Любовный детектив",
    "love_short": "Короткие любовные романы",
    "love_erotica": "Эротика",
    "fantasy": "Фэнтези",
    "fantasy_fight": "Боевое фэнтези",
    "fantasy_dark": "Тёмное фэнтези",
    "fantasy_epic": "Эпическое фэнтези",
    "fantasy_history": "Историческое фэнтези",
    "fantasy_humor": "Юмористическое фэнтези",
    "fantasy_love": "Любовное фэнтези",
    "fantasy_urban": "Городское фэнтези",
    "fantasy_children": "Детское фэнтези",
    "fantasy_magic": "Магическое фэнтези",
    "sci_history": "История",
    "sci_philosophy": "Философия",
    "sci_religion": "Религия",
    "sci_psychology": "Психология",
    "sci_culture": "Культурология",
    "sci_business": "Бизнес",
    "sci_politics": "Политология",
    "sci_jurisprudence": "Юриспруденция",
    "sci_linguistic": "Лингвистика",
    "sci_medicine": "Медицина",
    "sci_tech": "Техническая литература",
    "sci_computer": "Компьютерная литература",
    "sci_economy": "Экономика",
    "sci_pedagogy": "Педагогика",
    "sci_sociology": "Социология",
    "sci_military": "Военная история",
    "sci_natural": "Естественные науки",
    "sci_popular": "Научно-популярная литература",
    "sci_biology": "Биология",
    "sci_chemistry": "Химия",
    "sci_physics": "Физика",
    "sci_math": "Математика",
    "sci_geology": "Геология",
    "sci_environment": "Экология",
    "sci_astronomy": "Астрономия",
    "sci_geography": "География",
    "children": "Детская литература",
    "children_poetry": "Детские стихи",
    "children_prose": "Детская проза",
    "children_sf": "Детская фантастика",
    "children_detective": "Детский детектив",
    "children_education": "Детская образовательная",
    "children_art": "Детское творчество",
    "poetry": "Поэзия",
    "poetry_classic": "Классическая поэзия",
    "poetry_modern": "Современная поэзия",
    "dramaturgy": "Драматургия",
    "dramaturgy_comedy": "Комедия",
    "dramaturgy_tragedy": "Трагедия",
    "dramaturgy_drama": "Драма",
    "antique": "Античная литература",
    "antique_antique": "Античная литература",
    "antique_european": "Европейская старинная литература",
    "antique_eastern": "Древневосточная литература",
    "antique_myth": "Мифы и легенды",
    "adventure": "Приключения",
    "military": "Военная проза",
    "military_history": "Военная история",
    "humor": "Юмор",
    "humor_prose": "Юмористическая проза",
    "humor_verse": "Юмористические стихи",
    "humor_fantasy": "Юмористическое фэнтези",
    "nonfiction": "Non-fiction",
    "nonfiction_biography": "Биографии и мемуары",
    "nonfiction_publicism": "Публицистика",
    "nonfiction_critic": "Критика",
    "nonfiction_reference": "Справочная литература",
    "nonfiction_design": "Дизайн",
    "nonfiction_art": "Искусство",
    "nonfiction_cinema": "Кино",
    "nonfiction_music": "Музыка",
    "nonfiction_photo": "Фотография",
    "nonfiction_media": "Медиа",
    "nonfiction_cooking": "Кулинария",
    "nonfiction_sport": "Спорт",
    "nonfiction_travel": "Путешествия",
    "nonfiction_home": "Дом и семья",
    "nonfiction_hobby": "Хобби",
    "nonfiction_garden": "Сад и огород",
    "nonfiction_health": "Здоровье",
    "nonfiction_psy": "Психология",
    "nonfiction_esoteric": "Эзотерика",
    "nonfiction_religion": "Религия",
    "nonfiction_selfhelp": "Самопомощь",
    "nonfiction_pets": "Домашние питомцы",
    "home": "Домоводство",
    "home_pets": "Домашние питомцы",
    "home_cooking": "Кулинария",
    "home_garden": "Сад и огород",
    "home_entertain": "Досуг",
    "home_sex": "Секс",
    "home_diy": "Сделай сам",
    "religion": "Религия",
    "religion_religion": "Религиозная литература",
    "religion_orthodoxy": "Православие",
    "religion_catholic": "Католицизм",
    "religion_islam": "Ислам",
    "religion_buddhism": "Буддизм",
    "religion_judaism": "Иудаизм",
    "religion_esoteric": "Эзотерика",
    "religion_self": "Самосовершенствование",
    "religion_theology": "Теология",
    "comp": "Компьютеры",
    "comp_hardware": "Компьютерное железо",
    "comp_soft": "Программное обеспечение",
    "comp_programming": "Программирование",
    "comp_net": "Сети",
    "comp_web": "Веб-разработка",
    "comp_os": "Операционные системы",
    "comp_db": "Базы данных",
    "comp_security": "Информационная безопасность",
    "comp_games": "Компьютерные игры",
    "ref_encyc": "Энциклопедии",
    "ref_dict": "Словари",
    "ref_guide": "Путеводители",
    "ref_phrase": "Разговорники",
    "ref_job": "Профессиональная литература",
    "ref_work": "Трудовые ресурсы",
    "reference": "Справочная литература",
    "sci": "Наука",
    "story": "Рассказ",
    "novel": "Роман",
    "novella": "Повесть",
    "essay": "Эссе",
    "article": "Статья",
    "biography": "Биография",
    "memoir": "Мемуары",
    "travel_notes": "Путевые заметки",
    "guide": "Руководство",
    "instruction": "Инструкция",
    "note": "Заметка",
    "report": "Репортаж",
    "review": "Обзор",
    "interview": "Интервью",
    "correspondence": "Переписка",
    "speech": "Речь",
    "sketch": "Очерк",
    "monography": "Монография",
    "thesis": "Диссертация",
    "diary": "Дневник",
    "poem": "Стихотворение",
    "play": "Пьеса",
    "fairy_tale": "Сказка",
    "myth": "Миф",
    "legend": "Легенда",
    "epic": "Эпос",
    "ballad": "Баллада",
    "fable": "Басня",
    "parable": "Притча",
    "short_story": "Новелла",
    "mystery": "Мистика",
    "psychological": "Психологический роман",
    "philosophical": "Философский роман",
    "satire": "Сатира",
    "absurd": "Абсурд",
    "parody": "Пародия",
    "grotesque": "Гротеск",
    "tragicomedy": "Трагикомедия",
    "romance": "Романтика",
    "erotica": "Эротика",
    "pornography": "Порнография",
    "bdsm": "BDSM",
    "ya": "Подростковая литература",
    "litrpg": "ЛитРПГ",
    "wuxia": "Уся",
    "xianxia": "Сянься",
    "fanfiction": "Фанфик",
    "gamefic": "Игровая литература",
    "popadantsy": "Попаданцы",
    "time_travel": "Путешествия во времени",
    "alternate_history": "Альтернативная история",
    "postapocalypse": "Постапокалипсис",
    "zombie": "Зомби-апокалипсис",
    "survival": "Выживание",
    "cyberpunk": "Киберпанк",
    "steampunk": "Стимпанк",
    "dieselpunk": "Дизельпанк",
    "clockpunk": "Клокпанк",
    "biopunk": "Биопанк",
    "nanopunk": "Нанопанк",
    "space_opera": "Космическая опера",
    "hard_sf": "Твёрдая научная фантастика",
    "soft_sf": "Мягкая научная фантастика",
    "social_sf": "Социальная фантастика",
    "apocalypse": "Апокалипсис",
    "utopia": "Утопия",
    "dystopia": "Антиутопия",
    "parallel_world": "Параллельные миры",
    "magic_world": "Магический мир",
    "litrpg_published": "ЛитРПГ (изданное)",
    "russian_fantasy": "Русское фэнтези",
    "russian_sf": "Русская фантастика",
    "foreign_fantasy": "Зарубежное фэнтези",
    "foreign_sf": "Зарубежная фантастика",
    "russian_detective": "Русский детектив",
    "foreign_detective": "Зарубежный детектив",
    "russian_prose": "Русская проза",
    "foreign_prose": "Зарубежная проза",
    "russian_love": "Русские любовные романы",
    "foreign_love": "Зарубежные любовные романы",
    "russian_adventure": "Русские приключения",
    "foreign_adventure": "Зарубежные приключения",
    "russian_military": "Русская военная проза",
    "foreign_military": "Зарубежная военная проза",
    "russian_sci": "Русская научная литература",
    "foreign_sci": "Зарубежная научная литература",
}

def genre_name(code):
    return RUSSIAN_GENRES.get(code, code)

# ============================================================================
# РАБОТА С БАЗОЙ ДАННЫХ
# ============================================================================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except (sqlite3.OperationalError, Exception):
        swallow.note('render.get_db:L363')
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-2000000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")
    return conn


@contextmanager
def db_connection():
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with db_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS archives (
                id INTEGER PRIMARY KEY,
                filename TEXT UNIQUE NOT NULL,
                size INTEGER,
                books_count INTEGER DEFAULT 0,
                base_path TEXT
            );

            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                file_path TEXT NOT NULL,
                archive_id INTEGER NOT NULL,
                file_size INTEGER,
                lang TEXT,
                series_id INTEGER,
                seq_number TEXT,
                description TEXT,
                annotation TEXT,
                year TEXT,
                extra TEXT DEFAULT '{}',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (archive_id) REFERENCES archives(id)
            );

            CREATE TABLE IF NOT EXISTS authors (
                id INTEGER PRIMARY KEY,
                full_name TEXT UNIQUE NOT NULL,
                first_name TEXT,
                last_name TEXT,
                middle_name TEXT
            );

            CREATE TABLE IF NOT EXISTS genres (
                id INTEGER PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                name TEXT
            );

            CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS book_authors (
                book_id INTEGER,
                author_id INTEGER,
                PRIMARY KEY (book_id, author_id)
            );

            CREATE TABLE IF NOT EXISTS book_genres (
                book_id INTEGER,
                genre_id INTEGER,
                PRIMARY KEY (book_id, genre_id)
            );

            CREATE TABLE IF NOT EXISTS covers (
                book_id INTEGER PRIMARY KEY,
                image_data BLOB,
                mime_type TEXT DEFAULT 'image/jpeg'
            );

            CREATE TABLE IF NOT EXISTS book_content (
                book_id INTEGER PRIMARY KEY,
                content TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_progress (
                id INTEGER PRIMARY KEY,
                archives_path TEXT,
                total_archives INTEGER DEFAULT 0,
                processed_archives INTEGER DEFAULT 0,
                total_books INTEGER DEFAULT 0,
                current_archive TEXT,
                status TEXT DEFAULT 'idle',
                started_at TEXT,
                updated_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_books_title ON books(title);
            CREATE INDEX IF NOT EXISTS idx_books_archive ON books(archive_id);
            CREATE INDEX IF NOT EXISTS idx_books_series ON books(series_id);
            CREATE INDEX IF NOT EXISTS idx_authors_name ON authors(full_name);
            CREATE INDEX IF NOT EXISTS idx_authors_last ON authors(last_name);
            CREATE INDEX IF NOT EXISTS idx_genres_code ON genres(code);
            CREATE INDEX IF NOT EXISTS idx_book_authors_book ON book_authors(book_id);
            CREATE INDEX IF NOT EXISTS idx_book_authors_author ON book_authors(author_id);
            CREATE INDEX IF NOT EXISTS idx_book_genres_book ON book_genres(book_id);
            CREATE INDEX IF NOT EXISTS idx_book_genres_genre ON book_genres(genre_id);
        """)
        # ALTER TABLE для существующих БД
        for alt in [
            "ALTER TABLE archives ADD COLUMN base_path TEXT",
            "ALTER TABLE books ADD COLUMN annotation TEXT",
            "ALTER TABLE books ADD COLUMN year TEXT",
            "ALTER TABLE books ADD COLUMN extra TEXT DEFAULT '{}'",
        ]:
            try:
                conn.execute(alt)
            except sqlite3.OperationalError:
                swallow.note('render.init_db:L484')
                pass
        # FTS5 — отдельно, т.к. VIRTUAL TABLE не поддерживается в executescript
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
                    title,
                    content_text,
                    tokenize='unicode61 remove_diacritics 1'
                )
            """)
        except sqlite3.OperationalError:
            swallow.note('render.init_db:L496')
            pass
        conn.commit()


def get_scan_progress():
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM scan_progress WHERE id = 1").fetchone()
    if row:
        return dict(row)
    return None


def update_scan_progress(path='', total=-1, processed=-1, books=-1,
                         current='', status='running', error=''):
    with db_connection() as conn:
        ts = datetime.now().isoformat()
        existing = conn.execute("SELECT * FROM scan_progress WHERE id=1").fetchone()
        if existing:
            path = path if path else existing['archives_path']
            total = total if total >= 0 else existing['total_archives']
            processed = processed if processed >= 0 else existing['processed_archives']
            books = books if books >= 0 else existing['total_books']
            current = current if current else existing['current_archive']
        conn.execute("""
            INSERT OR REPLACE INTO scan_progress
            (id, archives_path, total_archives, processed_archives, total_books,
             current_archive, status, updated_at, started_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT started_at FROM scan_progress WHERE id=1), ?))
        """, (path, total, processed, books, current, status, ts, ts))
        conn.commit()


def clear_scan_progress():
    with db_connection() as conn:
        conn.execute("DELETE FROM scan_progress WHERE id = 1")
        conn.commit()


# ============================================================================
# ПАРСЕР FB2
# ============================================================================

FB2_NS = {
    'fb': 'http://www.gribuser.ru/xml/fictionbook/2.0',
    'l': 'http://www.w3.org/1999/xlink',
}


def _f(elem, *paths):
    """find() with fallback — работает правильно даже для листовых узлов (bool=False)"""
    for p in paths:
        if isinstance(p, tuple):
            if len(p) == 2:
                r = elem.find(p[0], p[1])
            else:
                r = elem.find(p[0])
        else:
            r = elem.find(p)
        if r is not None:
            return r
    return None


def extract_body_text(root):
    texts = []
    for body in (root.findall('.//fb:body', FB2_NS) or root.findall('.//body') or []):
        for elem in body.iter():
            if elem.text:
                t = elem.text.strip()
                if t:
                    texts.append(t)
            if elem.tail:
                t = elem.tail.strip()
                if t:
                    texts.append(t)
    return ' '.join(texts)


def extract_cover_from_fb2(root):
    ti = _f(root, ('.//fb:title-info', FB2_NS), './/title-info')
    if ti is None:
        return None, None
    coverpage = _f(ti, ('fb:coverpage', FB2_NS), 'coverpage')
    if coverpage is None:
        return None, None
    img = _f(coverpage, ('fb:image', FB2_NS), 'image')
    if img is None:
        return None, None
    href = (img.get('{http://www.w3.org/1999/xlink}href', '')
            or img.get('l:href', '') or img.get('href', ''))
    if not href:
        return None, None
    img_id = href.lstrip('#')
    if not img_id:
        return None, None
    binary = _f(root, (f'.//fb:binary[@id="{img_id}"]', FB2_NS),
                f'.//binary[@id="{img_id}"]')
    if binary is not None and binary.text:
        try:
            data = base64.b64decode(binary.text.strip())
            mime = binary.get('content-type', 'image/jpeg')
            return data, mime
        except Exception:
            swallow.note('render.extract_cover_from_fb2:L601')
            pass
    return None, None


def parse_fb2(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        swallow.note('render.parse_fb2:L610')
        return None

    data = {
        'title': '',
        'authors': [],
        'genres': [],
        'series': None,
        'seq_number': None,
        'lang': None,
        'description': '',
        'body_text': '',
        'cover_data': None,
        'cover_mime': None,
        'extra': {},
    }

    ti = _f(root, ('.//fb:title-info', FB2_NS), './/title-info')

    if ti is not None:
        bt = _f(ti, ('fb:book-title', FB2_NS), 'book-title')
        if bt is not None and bt.text:
            data['title'] = bt.text.strip()

        for author in (ti.findall('fb:author', FB2_NS) or ti.findall('author') or []):
            fn = _f(author, ('fb:first-name', FB2_NS), 'first-name')
            ln = _f(author, ('fb:last-name', FB2_NS), 'last-name')
            mn = _f(author, ('fb:middle-name', FB2_NS), 'middle-name')
            parts = [
                ln.text.strip() if ln is not None and ln.text else '',
                fn.text.strip() if fn is not None and fn.text else '',
                mn.text.strip() if mn is not None and mn.text else '',
            ]
            full = ' '.join(p for p in parts if p)
            if full:
                data['authors'].append({
                    'full_name': full,
                    'first_name': parts[1],
                    'last_name': parts[0],
                    'middle_name': parts[2],
                })

        # Переводчики
        translators = []
        for tr in (ti.findall('fb:translator', FB2_NS) or ti.findall('translator') or []):
            fn = _f(tr, ('fb:first-name', FB2_NS), 'first-name')
            ln = _f(tr, ('fb:last-name', FB2_NS), 'last-name')
            tr_parts = [
                (ln.text.strip() if ln is not None and ln.text else ''),
                (fn.text.strip() if fn is not None and fn.text else ''),
            ]
            tr_full = ' '.join(p for p in tr_parts if p)
            if tr_full:
                translators.append(tr_full)
        if translators:
            data['extra']['translators'] = translators

        for genre in (ti.findall('fb:genre', FB2_NS) or ti.findall('genre') or []):
            if genre.text:
                data['genres'].append(genre.text.strip())

        seq = _f(ti, ('fb:sequence', FB2_NS), 'sequence')
        if seq is not None:
            data['series'] = seq.get('name', '').strip() or None
            data['seq_number'] = seq.get('number', '').strip() or None

        lang = _f(ti, ('fb:lang', FB2_NS), 'lang')
        if lang is not None and lang.text:
            data['lang'] = lang.text.strip()

        # Аннотация
        ann = _f(ti, ('fb:annotation', FB2_NS), 'annotation')
        if ann is not None:
            ann_texts = []
            for p in (ann.findall('.//fb:p', FB2_NS) or ann.findall('.//p') or []):
                if p.text:
                    ann_texts.append(p.text.strip())
            data['description'] = ' '.join(ann_texts)

        # Keywords
        kw = _f(ti, ('fb:keywords', FB2_NS), 'keywords')
        if kw is not None and kw.text:
            data['extra']['keywords'] = kw.text.strip()

        # Date
        dt = _f(ti, ('fb:date', FB2_NS), 'date')
        if dt is not None and dt.text:
            data['extra']['date'] = dt.text.strip()

    # publish-info
    pi = _f(root, ('.//fb:publish-info', FB2_NS), './/publish-info')
    if pi is not None:
        for field, key in [
            ('fb:publisher', 'publisher'), ('publisher', 'publisher'),
            ('fb:city', 'city'), ('city', 'city'),
            ('fb:year', 'year'), ('year', 'year'),
            ('fb:isbn', 'isbn'), ('isbn', 'isbn'),
        ]:
            el = _f(pi, (field, FB2_NS) if field.startswith('fb:') else (field,))
            if el is not None and el.text:
                data['extra'][key] = el.text.strip()

    # document-info
    di = _f(root, ('.//fb:document-info', FB2_NS), './/document-info')
    if di is not None:
        for field, key in [
            ('fb:id', 'doc_id'), ('id', 'doc_id'),
            ('fb:version', 'version'), ('version', 'version'),
            ('fb:program-used', 'program_used'), ('program-used', 'program_used'),
            ('fb:date', 'doc_date'), ('date', 'doc_date'),
        ]:
            el = _f(di, (field, FB2_NS) if field.startswith('fb:') else (field,))
            if el is not None and el.text:
                data['extra'][key] = el.text.strip()

    data['body_text'] = extract_body_text(root)
    data['cover_data'], data['cover_mime'] = extract_cover_from_fb2(root)

    return data


# ============================================================================
# FB2 → HTML конвертер для чтения в браузере
# ============================================================================

FB2_TO_HTML_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
:root { --bg: #fafaf7; --text: #1a1a1a; --heading: #2c3e50; }
body.dark { --bg: #1a1a2e; --text: #e2e8f0; --heading: #93c5fd; }
body.reading-mode {
    font-family: 'Georgia', 'Times New Roman', serif;
    background: var(--bg); color: var(--text); line-height: 1.8;
    max-width: 800px; margin: 0 auto; padding: 40px 30px; transition: background 0.2s, color 0.2s;
}
.reader-toolbar { position: fixed; top: 0; left: 0; right: 0; z-index: 999;
    background: rgba(30,41,59,0.95); color: white; padding: 8px 16px;
    display: flex; align-items: center; gap: 12px; font-family: sans-serif;
    font-size: 13px; backdrop-filter: blur(8px); }
.reader-toolbar button { background: rgba(255,255,255,0.1); border: none; color: white;
    padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.reader-toolbar button:hover { background: rgba(255,255,255,0.2); }
.reader-toolbar .spacer { flex: 1; }
body { padding-top: 44px; }
h1, h2, h3, h4, h5, h6 { font-weight: 600; margin: 1.5em 0 0.5em; color: #2c3e50; }
h1 { font-size: 1.8em; text-align: center; }
h2 { font-size: 1.4em; }
h3 { font-size: 1.2em; }
p { margin: 0.7em 0; text-indent: 1.5em; text-align: justify; }
p:first-of-type { text-indent: 0; }
.empty-line { height: 1em; }
.subtitle { font-style: italic; text-align: center; margin: 1em 0; }
.cite { font-style: italic; margin-left: 2em; color: #555; }
.text-author { text-align: right; font-style: italic; margin: 0.5em 0; }
.epigraph { margin: 1.5em 2em; font-style: italic; color: #555; }
.verse { margin: 1em 2em; white-space: pre-line; font-style: italic; }
.verse p { text-indent: 0; }
.poem-line { display: block; }
.section-title { margin-top: 2em; }
.image { text-align: center; margin: 1.5em 0; }
.image img { max-width: 100%; height: auto; border-radius: 4px; }
.notes { margin-top: 3em; padding-top: 2em; border-top: 1px solid #ddd; }
.note-link { color: #3498db; text-decoration: none; }
.note-link:hover { text-decoration: underline; }
.body-notes { margin-top: 2em; padding-top: 1em; border-top: 2px solid #ccc; }
"""


def _render_inline_children(elem):
    parts = []
    for child in elem:
        if not isinstance(child.tag, str):
            parts.append(child.tail or '')
            continue
        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag == 'strong' or tag == 'emphasis':
            inner = _render_inline_children(child) or (child.text or '')
            t = 'b' if tag == 'strong' else 'i'
            parts.append(f'<{t}>{inner}</{t}>')
        elif tag == 'a':
            href = child.get('{http://www.w3.org/1999/xlink}href', '') or child.get('href', '')
            inner = _render_inline_children(child) or (child.text or '')
            if href:
                parts.append(f'<a class="note-link" href="{html_mod.escape(href)}">{inner}</a>')
            else:
                parts.append(inner)
        elif tag == 'image':
            href = (child.get('{http://www.w3.org/1999/xlink}href', '')
                    or child.get('l:href', '') or child.get('href', ''))
            parts.append(f'[image: {href}]')
        elif tag == 'sup':
            inner = _render_inline_children(child) or (child.text or '')
            parts.append(f'<sup>{inner}</sup>')
        elif tag == 'sub':
            inner = _render_inline_children(child) or (child.text or '')
            parts.append(f'<sub>{inner}</sub>')
        elif tag == 'strikethrough':
            inner = _render_inline_children(child) or (child.text or '')
            parts.append(f'<s>{inner}</s>')
        else:
            parts.append(child.text or '')
        parts.append(child.tail or '')
    return ''.join(parts)


def _render_section_to_html(section, depth=0):
    parts = []
    for child in section:
        if not isinstance(child.tag, str):
            parts.append(child.tail or '')
            continue
        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag == 'title':
            title_parts = []
            for p in (child.findall('fb:p', FB2_NS) or child.findall('p') or []):
                title_parts.append(_render_inline_children(p) or (p.text or ''))
            title_text = ' '.join(title_parts) or (child.text or '')
            h = f'h{min(depth + 2, 6)}'
            parts.append(f'<{h} class="section-title">{html_mod.escape(title_text)}</{h}>')
        elif tag == 'p':
            text = _render_inline_children(child) or (child.text or '')
            if text.strip():
                parts.append(f'<p>{text}</p>')
        elif tag == 'subtitle':
            text = _render_inline_children(child) or (child.text or '')
            parts.append(f'<div class="subtitle">{text}</div>')
        elif tag == 'cite':
            text = _render_inline_children(child) or (child.text or '')
            parts.append(f'<div class="cite">{text}</div>')
        elif tag == 'text-author':
            text = _render_inline_children(child) or (child.text or '')
            parts.append(f'<div class="text-author">{text}</div>')
        elif tag == 'epigraph':
            text = _render_inline_children(child) or (child.text or '')
            parts.append(f'<div class="epigraph">{text}</div>')
        elif tag == 'empty-line':
            parts.append('<div class="empty-line"></div>')
        elif tag == 'v':
            verses = []
            for p in (child.findall('fb:p', FB2_NS) or child.findall('p') or []):
                v = _render_inline_children(p) or (p.text or '')
                if v.strip():
                    verses.append(f'<span class="poem-line">{v}</span>')
            if verses:
                parts.append(f'<div class="verse">{"<br>".join(verses)}</div>')
        elif tag == 'image':
            parts.append('[image]')
        elif tag == 'section':
            parts.append(_render_section_to_html(child, depth + 1))
        elif tag == 'poem':
            # FB2 poem tag — similar to verse
            stanzas = []
            for stanza in (child.findall('fb:stanza', FB2_NS) or child.findall('stanza') or
                          child.findall('fb:v', FB2_NS) or child.findall('v') or []):
                lines = []
                for p in (stanza.findall('fb:p', FB2_NS) or stanza.findall('p') or []):
                    v = _render_inline_children(p) or (p.text or '')
                    if v.strip():
                        lines.append(f'<span class="poem-line">{v}</span>')
                if lines:
                    stanzas.append('<br>'.join(lines))
            if stanzas:
                parts.append(f'<div class="verse">{"<br><br>".join(stanzas)}</div>')
        else:
            parts.append(child.text or '')
        parts.append(child.tail or '')
    return ''.join(parts)


def fb2_to_html(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        swallow.note('render.fb2_to_html:L882')
        return None

    title = ''
    ti = _f(root, ('.//fb:title-info', FB2_NS), './/title-info')
    if ti is not None:
        bt = _f(ti, ('fb:book-title', FB2_NS), 'book-title')
        if bt is not None and bt.text:
            title = bt.text.strip()

    bodies_html = []
    for body in (root.findall('.//fb:body', FB2_NS) or root.findall('.//body') or []):
        body_name = body.get('name', '') or ''
        sections_html = []
        for section in (body.findall('fb:section', FB2_NS) or body.findall('section') or []):
            sections_html.append(_render_section_to_html(section))
        cls = f'body-{body_name}' if body_name else 'body-main'
        extra_class = ' notes' if body_name == 'notes' else ''
        bodies_html.append(f'<div class="{cls}{extra_class}">{"".join(sections_html)}</div>')

    content = '\n'.join(bodies_html)
    title_esc = html_mod.escape(title)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_esc}</title>
<style>{FB2_TO_HTML_CSS}</style>
</head>
<body class="reading-mode" id="reader-body">
<div class="reader-toolbar">
  <button onclick="toggleTheme()" title="Сменить тему">🌓 Тема</button>
  <button onclick="changeFont(-1)" title="Уменьшить шрифт">А−</button>
  <button onclick="changeFont(1)" title="Увеличить шрифт">А+</button>
  <span class="spacer"></span>
  <button onclick="window.close()" title="Закрыть">✕</button>
</div>
<h1>{title_esc}</h1>
{content}
<script>
(function() {{
  var fs = localStorage.getItem('readerFontSize');
  var th = localStorage.getItem('readerTheme');
  if (fs) document.getElementById('reader-body').style.fontSize = fs + 'px';
  if (th === 'dark') document.getElementById('reader-body').classList.add('dark');
}})();
function toggleTheme() {{
  var b = document.getElementById('reader-body');
  b.classList.toggle('dark');
  localStorage.setItem('readerTheme', b.classList.contains('dark') ? 'dark' : 'light');
}}
function changeFont(delta) {{
  var b = document.getElementById('reader-body');
  var size = parseInt(localStorage.getItem('readerFontSize')) || 16;
  size = Math.min(28, Math.max(12, size + delta));
  b.style.fontSize = size + 'px';
  localStorage.setItem('readerFontSize', size);
}}
</script>
</body>
</html>"""


# ============================================================================
# СКАНЕР АРХИВОВ
# ============================================================================

def list_zip_files(dir_path):
    files = []
    try:
        for f in os.listdir(dir_path):
            if f.lower().endswith('.zip'):
                files.append(os.path.join(dir_path, f))
    except (PermissionError, OSError) as e:
        print(f"  Ошибка доступа к {dir_path}: {e}")
    return sorted(files)


def find_archive(filename):
    # 1. base_path из таблицы archives (самый надёжный)
    with db_connection() as conn:
        row = conn.execute(
            "SELECT base_path FROM archives WHERE filename = ?", (filename,)
        ).fetchone()
        if row and row['base_path']:
            p = os.path.join(row['base_path'], filename)
            if os.path.isfile(p):
                return p
    # 2. ARCHIVES_DIR
    direct = os.path.join(ARCHIVES_DIR, filename)
    if os.path.isfile(direct):
        return direct
    # 3. scan_progress (CLI-сканирование могло записать путь сюда)
    sp = get_scan_progress()
    if sp and sp['archives_path']:
        p = os.path.join(sp['archives_path'], filename)
        if os.path.isfile(p):
            return p
    # 4. Поиск рекурсивно в ARCHIVES_DIR
    for root_dir, _, files in os.walk(ARCHIVES_DIR):
        if filename in files:
            return os.path.join(root_dir, filename)
    return None


def scan_archives(archives_path, progress_callback=None, store_content=False):
    if not os.path.isdir(archives_path):
        msg = f"Папка не найдена: {archives_path}"
        print(f"  {msg}")
        if progress_callback:
            progress_callback(status='error', error=msg)
        return

    zip_files = list_zip_files(archives_path)
    if not zip_files:
        msg = f"ZIP-архивы не найдены в {archives_path}"
        print(f"  {msg}")
        if progress_callback:
            progress_callback(status='error', error=msg)
        return

    total_archives = len(zip_files)
    print(f"Найдено архивов: {total_archives}")
    init_db()

    # Сохраняем путь в scan_progress для find_archive
    update_scan_progress(path=archives_path, total=total_archives, status='running')

    processed = 0
    total_books = 0

    for idx, zip_path in enumerate(zip_files, 1):
        zname = os.path.basename(zip_path)
        print(f"\n[{idx}/{total_archives}] {zname}")
        if progress_callback:
            progress_callback(zname, idx - 1, total_archives,
                              total_books, 'scanning')

        with db_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM archives WHERE filename = ?", (zname,)
            ).fetchone()
            if existing:
                count = conn.execute(
                    "SELECT books_count FROM archives WHERE id = ?",
                    (existing['id'],)
                ).fetchone()['books_count']
                total_books += count
                processed += 1
                print(f"   уже импортирован ({count} книг), пропускаем")
                if progress_callback:
                    progress_callback(zname, processed,
                                      total_archives, total_books, 'skipped')
                continue

            conn.execute(
                "INSERT INTO archives (filename, size, base_path) VALUES (?, ?, ?)",
                (zname, os.path.getsize(zip_path), archives_path)
            )
            archive_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            books_in_archive = 0
            buffer = []

            try:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    for info in zf.infolist():
                        if not info.filename.lower().endswith(('.fb2', '.fb2.zip')):
                            continue
                        try:
                            xml_bytes = zf.read(info.filename)
                        except Exception as e:
                            print(f"   ошибка чтения {info.filename}: {e}")
                            continue

                        if info.filename.lower().endswith('.fb2.zip'):
                            try:
                                with zipfile.ZipFile(BytesIO(xml_bytes)) as inner:
                                    inner_names = inner.namelist()
                                    if inner_names:
                                        xml_bytes = inner.read(inner_names[0])
                            except Exception:
                                swallow.note('render.scan_archives:L1065')
                                pass

                        data = parse_fb2(xml_bytes)
                        if not data or not data['title']:
                            continue

                        buffer.append((data, info.filename, info.file_size, archive_id))
                        books_in_archive += 1

                        if len(buffer) >= BATCH_SIZE:
                            _insert_batch(conn, buffer)
                            buffer = []

                    if buffer:
                        _insert_batch(conn, buffer)

                conn.execute(
                    "UPDATE archives SET books_count = ? WHERE id = ?",
                    (books_in_archive, archive_id)
                )
                conn.commit()
                total_books += books_in_archive
                processed += 1
                print(f"   импортировано книг: {books_in_archive}")
                if progress_callback:
                    progress_callback(zname, processed,
                                      total_archives, total_books, 'completed')

            except zipfile.BadZipFile:
                print(f"   повреждённый архив, пропускаем")
                conn.execute("DELETE FROM archives WHERE id = ?", (archive_id,))
                conn.commit()
                if progress_callback:
                    progress_callback(zname, processed,
                                      total_archives, total_books, 'error')

    print(f"\n{'='*60}")
    print(f"Готово! Всего импортировано книг: {total_books}")
    if progress_callback:
        progress_callback('', processed, total_archives, total_books, 'completed')


def scan_archives_parallel(archives_path, progress_callback=None, store_content=False):
    if not os.path.isdir(archives_path):
        msg = f"Папка не найдена: {archives_path}"
        print(f"  {msg}")
        if progress_callback:
            progress_callback(status='error', error=msg)
        return

    zip_files = list_zip_files(archives_path)
    if not zip_files:
        msg = f"ZIP-архивы не найдены в {archives_path}"
        print(f"  {msg}")
        if progress_callback:
            progress_callback(status='error', error=msg)
        return

    total_archives = len(zip_files)
    print(f"Найдено архивов: {total_archives}")
    init_db()

    # Сохраняем путь в scan_progress для find_archive
    update_scan_progress(path=archives_path, total=total_archives, status='running')

    # Отфильтровываем уже обработанные архивы
    with db_connection() as conn:
        processed_set = set()
        for row in conn.execute("SELECT filename FROM archives").fetchall():
            processed_set.add(row['filename'])

    remaining = [z for z in zip_files if os.path.basename(z) not in processed_set]
    skipped = len(zip_files) - len(remaining)
    total_books_initial = 0
    if skipped > 0:
        with db_connection() as conn:
            for row in conn.execute("SELECT SUM(books_count) FROM archives").fetchall():
                total_books_initial = row[0] or 0

    print(f"Пропущено (уже импортировано): {skipped}")
    print(f"Осталось обработать: {len(remaining)}")

    total_books = total_books_initial
    processed = skipped
    processed_lock = threading.Lock()
    books_lock = threading.Lock()

    def process_archive(zip_path):
        nonlocal total_books, processed
        zname = os.path.basename(zip_path)
        print(f"  [{zname}] начало обработки")
        if progress_callback:
            with processed_lock:
                progress_callback(zname, processed,
                                  total_archives, total_books, 'scanning')

        with db_connection() as conn:
            conn.execute(
                "INSERT INTO archives (filename, size, base_path) VALUES (?, ?, ?)",
                (zname, os.path.getsize(zip_path), archives_path)
            )
            archive_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            books_in_archive = 0
            buffer = []

            try:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    for info in zf.infolist():
                        if not info.filename.lower().endswith(('.fb2', '.fb2.zip')):
                            continue
                        try:
                            xml_bytes = zf.read(info.filename)
                        except Exception:
                            swallow.note('render.process_archive:L1180')
                            continue

                        if info.filename.lower().endswith('.fb2.zip'):
                            try:
                                with zipfile.ZipFile(BytesIO(xml_bytes)) as inner:
                                    inner_names = inner.namelist()
                                    if inner_names:
                                        xml_bytes = inner.read(inner_names[0])
                            except Exception:
                                swallow.note('render.process_archive:L1190')
                                pass

                        data = parse_fb2(xml_bytes)
                        if not data or not data['title']:
                            continue

                        buffer.append((data, info.filename, info.file_size, archive_id))
                        books_in_archive += 1

                        if len(buffer) >= BATCH_SIZE:
                            _insert_batch(conn, buffer, store_content)
                            buffer = []

                    if buffer:
                        _insert_batch(conn, buffer, store_content)

                conn.execute(
                    "UPDATE archives SET books_count = ? WHERE id = ?",
                    (books_in_archive, archive_id)
                )
                conn.commit()

                with books_lock:
                    total_books += books_in_archive
                with processed_lock:
                    processed += 1

                print(f"  [{zname}] завершён: {books_in_archive} книг")
                if progress_callback:
                    with processed_lock:
                        progress_callback(zname, processed,
                                          total_archives, total_books, 'completed')

            except Exception as e:
                print(f"  [{zname}] ошибка: {e}")
                try:
                    conn.execute("DELETE FROM archives WHERE id = ?", (archive_id,))
                    conn.commit()
                except Exception:
                    swallow.note('render.process_archive:L1230')
                    pass
                with processed_lock:
                    processed += 1
                if progress_callback:
                    with processed_lock:
                        progress_callback(zname, processed,
                                          total_archives, total_books, 'error')

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_archive, zp): zp for zp in remaining}
        for future in concurrent.futures.as_completed(futures):
            future.result()

    print(f"\n{'='*60}")
    print(f"Готово! Всего книг: {total_books}")
    if progress_callback:
        progress_callback('', processed, total_archives, total_books, 'completed')


def _insert_batch(conn, buffer, store_content=False):
    for data, file_path, file_size, archive_id in buffer:
        series_id = None
        if data['series']:
            conn.execute(
                "INSERT OR IGNORE INTO series (name) VALUES (?)",
                (data['series'],)
            )
            series_id = conn.execute(
                "SELECT id FROM series WHERE name = ?", (data['series'],)
            ).fetchone()[0]

        extra_json = json_mod.dumps(data.get('extra', {}), ensure_ascii=False)
        conn.execute("""
            INSERT INTO books (title, file_path, archive_id, file_size,
                               lang, series_id, seq_number, description, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data['title'], file_path, archive_id, file_size,
            data['lang'], series_id, data['seq_number'], data['description'], extra_json
        ))
        book_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        for author in data['authors']:
            conn.execute(
                "INSERT OR IGNORE INTO authors (full_name, first_name, last_name, middle_name) "
                "VALUES (?, ?, ?, ?)",
                (author['full_name'], author['first_name'],
                 author['last_name'], author['middle_name'])
            )
            author_id = conn.execute(
                "SELECT id FROM authors WHERE full_name = ?",
                (author['full_name'],)
            ).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id) VALUES (?, ?)",
                (book_id, author_id)
            )

        for genre in data['genres']:
            conn.execute(
                "INSERT OR IGNORE INTO genres (code) VALUES (?)", (genre,)
            )
            genre_id = conn.execute(
                "SELECT id FROM genres WHERE code = ?", (genre,)
            ).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO book_genres (book_id, genre_id) VALUES (?, ?)",
                (book_id, genre_id)
            )

        # Сохраняем содержимое для FTS5 (только если store_content=True)
        if store_content and data['body_text']:
            conn.execute(
                "INSERT OR IGNORE INTO book_content (book_id, content) VALUES (?, ?)",
                (book_id, data['body_text'])
            )

        # Сохраняем обложку
        if data['cover_data']:
            conn.execute(
                "INSERT OR IGNORE INTO covers (book_id, image_data, mime_type) VALUES (?, ?, ?)",
                (book_id, data['cover_data'], data['cover_mime'])
            )


# ============================================================================
# FTS5 ИНДЕКСАЦИЯ
# ============================================================================

def build_fts_index(archives_path=None, progress_callback=None):
    print("Построение FTS5-индекса...")
    init_db()

    with db_connection() as conn:
        # Создаём FTS5 таблицу если её нет
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
                    title,
                    content_text,
                    tokenize='unicode61 remove_diacritics 1'
                )
            """)
        except sqlite3.OperationalError:
            swallow.note('render.build_fts_index:L1335')
            pass

        total = conn.execute("SELECT COUNT(*) FROM book_content").fetchone()[0]
        if total == 0:
            print("Нет содержимого для индексации. Сначала выполните scan или scan-parallel.")
            if progress_callback:
                progress_callback(status='error', error='Нет содержимого для индексации')
            return

        print(f"Всего записей для индексации: {total}")
        batch_size = 200
        offset = 0
        indexed = 0

        while offset < total:
            rows = conn.execute("""
                SELECT bc.book_id, b.title, bc.content
                FROM book_content bc
                JOIN books b ON b.id = bc.book_id
                LIMIT ? OFFSET ?
            """, (batch_size, offset)).fetchall()

            for row in rows:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO books_fts (rowid, title, content_text) VALUES (?, ?, ?)",
                        (row['book_id'], row['title'], row['content'])
                    )
                except Exception as e:
                    print(f"  ошибка индексации книги {row['book_id']}: {e}")

            conn.commit()
            indexed += len(rows)
            offset += batch_size
            pct = min(100, int(indexed / total * 100))
            print(f"  прогресс: {indexed}/{total} ({pct}%)")
            if progress_callback:
                progress_callback(status='indexing', total=total,
                                  processed=indexed)

    print("FTS5-индексация завершена!")
    if progress_callback:
        progress_callback(status='completed')


# ============================================================================
# ВЕБ-ИНТЕРФЕЙС
# ============================================================================

BASE_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} — Flibusta Offline</title>
<style>
  :root {
    --bg: #f0ebe3;
    --surface: #ffffff;
    --text: #1a1a2e;
    --text-secondary: #6b7280;
    --primary: #3b82f6;
    --primary-dark: #2563eb;
    --accent: #8b5cf6;
    --success: #10b981;
    --danger: #ef4444;
    --border: #e5e7eb;
    --radius: 12px;
    --shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.06);
    --shadow-lg: 0 10px 15px -3px rgba(0,0,0,0.1), 0 4px 6px -4px rgba(0,0,0,0.1);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans', sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.6; }
  header { background: linear-gradient(135deg, #1e293b, #334155); color: white; padding: 0 24px;
           display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;
           height: 60px; position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
  header h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }
  header nav { display: flex; gap: 4px; }
  header nav a { color: #cbd5e1; text-decoration: none; padding: 8px 14px; font-size: 14px;
                 font-weight: 500; border-radius: 8px; transition: all 0.15s; }
  header nav a:hover { color: white; background: rgba(255,255,255,0.1); }
  .container { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
  .search-box { background: var(--surface); padding: 20px; border-radius: var(--radius);
                box-shadow: var(--shadow); margin-bottom: 20px; }
  .search-box form { display: flex; gap: 10px; }
  .search-box input[type=text] { flex: 1; padding: 10px 16px; font-size: 15px;
                                  border: 2px solid var(--border); border-radius: 8px;
                                  outline: none; transition: border-color 0.15s; }
  .search-box input[type=text]:focus { border-color: var(--primary); }
  .search-box button { padding: 10px 24px; background: var(--primary); color: white;
                       border: none; border-radius: 8px; cursor: pointer; font-size: 15px;
                       font-weight: 600; transition: background 0.15s; white-space: nowrap; }
  .search-box button:hover { background: var(--primary-dark); }
  .book-list { background: var(--surface); border-radius: var(--radius); overflow: hidden;
               box-shadow: var(--shadow); }
  .book-item { padding: 12px 16px; border-bottom: 1px solid var(--border);
               display: flex; gap: 12px; align-items: center;
               transition: background 0.1s; }
  .book-item:hover { background: #f8fafc; }
  .book-item:last-child { border-bottom: none; }
  .book-cover-thumb { width: 40px; height: 56px; border-radius: 4px; object-fit: cover;
                      background: #f1f5f9; flex-shrink: 0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .book-info { flex: 1; min-width: 0; }
  .book-title { font-weight: 600; color: var(--text); text-decoration: none; font-size: 15px;
                line-height: 1.4; }
  .book-title:hover { color: var(--primary); }
  .book-meta { color: var(--text-secondary); font-size: 13px; margin-top: 2px; }
  .book-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .book-actions a { padding: 5px 10px; background: #f1f5f9; border-radius: 6px;
                    text-decoration: none; color: var(--text-secondary); font-size: 12px;
                    font-weight: 500; transition: all 0.15s; }
  .book-actions a:hover { background: var(--primary); color: white; }
  .pagination { text-align: center; margin: 24px 0; display: flex; justify-content: center; gap: 4px; }
  .pagination a { padding: 8px 14px; background: var(--surface); border: 1px solid var(--border);
                  text-decoration: none; color: var(--text-secondary); border-radius: 8px;
                  font-size: 14px; transition: all 0.15s; }
  .pagination a:hover { border-color: var(--primary); color: var(--primary); }
  .pagination a.active { background: var(--primary); color: white; border-color: var(--primary); }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
           gap: 12px; margin-bottom: 24px; }
  .stat-card { background: var(--surface); padding: 16px 20px; border-radius: var(--radius);
               box-shadow: var(--shadow); text-align: center; }
  .stat-card strong { display: block; font-size: 28px; font-weight: 800;
                       color: var(--primary); line-height: 1.2; }
  .stat-card span { font-size: 13px; color: var(--text-secondary); font-weight: 500; }
  .book-detail { background: var(--surface); padding: 32px; border-radius: var(--radius);
                 box-shadow: var(--shadow-lg); overflow: hidden; }
  .book-detail h2 { margin-bottom: 16px; font-size: 24px; color: var(--text); }
  .book-detail .meta-row { margin: 8px 0; font-size: 14px; }
  .book-detail .meta-row strong { display: inline-block; width: 120px; color: var(--text-secondary);
                                    font-weight: 500; }
  .book-detail .annotation { background: #f8fafc; padding: 16px 20px; border-radius: 8px;
                              margin: 16px 0; font-size: 14px; line-height: 1.7;
                              color: #374151; border-left: 3px solid var(--primary); }
  .cover-img { float: right; max-width: 200px; max-height: 320px;
               margin: 0 0 16px 24px; border-radius: 8px;
               box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
  @media (max-width: 600px) {
    .cover-img { float: none; display: block; max-width: 160px; margin: 0 auto 16px; }
  }
  .view-toggle { display: inline-flex; gap: 4px; margin-left: 12px; vertical-align: middle; }
  .view-toggle a { padding: 4px 10px; border: 1px solid var(--border); border-radius: 6px;
                   text-decoration: none; color: var(--text-secondary); font-size: 13px;
                   transition: all 0.15s; background: var(--surface); }
  .view-toggle a.active { background: var(--primary); color: white; border-color: var(--primary); }
  .book-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
               gap: 16px; background: transparent; box-shadow: none; }
  .book-grid .book-item { flex-direction: column; text-align: center; padding: 16px;
                          border: 1px solid var(--border); border-radius: var(--radius);
                          background: var(--surface); box-shadow: var(--shadow); }
  .book-grid .book-cover-thumb { width: 120px; height: 170px; margin: 0 auto 10px; }
  .book-grid .book-actions { margin-top: 8px; }
  .book-grid .book-meta { font-size: 12px; }
  .tag { display: inline-block; padding: 2px 10px; background: #eff6ff; color: var(--primary);
         border-radius: 20px; margin: 2px; font-size: 12px; text-decoration: none; font-weight: 500; }
  .tag:hover { background: var(--primary); color: white; }
  .flash { padding: 12px 20px; background: #fef2f2; color: var(--danger);
           border: 1px solid #fecaca; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }
  .empty { text-align: center; padding: 48px 20px; color: var(--text-secondary); }
  .empty::before { content: "📭"; display: block; font-size: 40px; margin-bottom: 12px; }
  h2 { font-size: 20px; font-weight: 700; margin-bottom: 16px; color: var(--text); }
  h3 { font-size: 16px; font-weight: 600; margin-bottom: 12px; color: var(--text); }
  .scan-page { background: var(--surface); border-radius: var(--radius); padding: 32px;
               box-shadow: var(--shadow); }
  .scan-page h2 { margin-bottom: 20px; }
  .form-group { margin-bottom: 16px; }
  .form-group label { display: block; margin-bottom: 6px; font-weight: 600; font-size: 14px; }
  .form-group input[type=text] { width: 100%; padding: 10px 14px; font-size: 15px;
                                  border: 2px solid var(--border); border-radius: 8px;
                                  outline: none; transition: border-color 0.15s; }
  .form-group input[type=text]:focus { border-color: var(--primary); }
  .btn { padding: 10px 24px; border: none; border-radius: 8px; cursor: pointer;
         font-size: 14px; font-weight: 600; text-decoration: none; display: inline-block;
         transition: all 0.15s; }
  .btn-primary { background: var(--primary); color: white; }
  .btn-primary:hover { background: var(--primary-dark); }
  .btn-primary:disabled { background: #93c5fd; cursor: not-allowed; }
  .btn-danger { background: var(--danger); color: white; }
  .btn-danger:hover { background: #dc2626; }
  .btn-success { background: var(--success); color: white; }
  .btn-success:hover { background: #059669; }
  .btn-accent { background: var(--accent); color: white; }
  .btn-accent:hover { background: #7c3aed; }
  .progress-section { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--border); }
  .progress-bar-outer { background: #f1f5f9; border-radius: 100px; height: 20px;
                         overflow: hidden; margin: 12px 0; }
  .progress-bar-inner { background: linear-gradient(90deg, var(--primary), var(--accent));
                         height: 100%; transition: width 0.5s ease; border-radius: 100px;
                         display: flex; align-items: center; justify-content: center; }
  .progress-bar-inner span { color: white; font-size: 11px; font-weight: 700; }
  .progress-info { color: var(--text-secondary); font-size: 14px; margin: 4px 0; }
  .progress-info strong { color: var(--text); }
  .status-badge { display: inline-block; padding: 2px 10px; border-radius: 20px;
                  font-size: 12px; font-weight: 600; }
  .status-idle { background: #f1f5f9; color: var(--text-secondary); }
  .status-running { background: #d1fae5; color: #065f46; }
  .status-completed { background: #d1fae5; color: #065f46; }
  .status-error { background: #fef2f2; color: var(--danger); }
  .snippet { color: var(--text-secondary); font-size: 13px; margin-top: 4px;
             line-height: 1.5; overflow: hidden; text-overflow: ellipsis;
             display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  .snippet b { color: var(--danger); }
  .filters { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
  .filters select { padding: 8px 12px; border: 2px solid var(--border); border-radius: 8px;
                     font-size: 14px; background: var(--surface); outline: none; }
  .filters select:focus { border-color: var(--primary); }
  .card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
               gap: 12px; margin-bottom: 24px; }
  .card { display: flex; flex-direction: column; align-items: center; padding: 20px 16px;
          background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
          text-decoration: none; transition: all 0.15s; box-shadow: var(--shadow); }
  .card:hover { transform: translateY(-2px); box-shadow: var(--shadow-lg); border-color: var(--primary); }
  .card-avatar { width: 56px; height: 56px; border-radius: 50%; background: var(--primary);
                 color: white; display: flex; align-items: center; justify-content: center;
                 font-size: 22px; font-weight: 700; margin-bottom: 10px; }
  .card-title { font-weight: 600; color: var(--text); font-size: 14px; text-align: center;
                line-height: 1.3; word-break: break-word; }
  .card-count { color: var(--text-secondary); font-size: 12px; margin-top: 4px; }
  .card-tag { padding: 14px 12px; }
  .genre-category { margin-bottom: 28px; }
  .genre-category-title { font-size: 17px; font-weight: 700; color: var(--text);
                           margin-bottom: 10px; padding-bottom: 6px; border-bottom: 2px solid var(--primary); }
  .meta-section { margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--border); }
  .all-books-link { display: block; text-align: center; margin-top: 16px; padding: 10px;
                     background: var(--surface); border-radius: var(--radius); font-weight: 600;
                     color: var(--primary); text-decoration: none; transition: background 0.15s; }
  .all-books-link:hover { background: #eff6ff; }
  .page-layout { display: flex; gap: 24px; align-items: flex-start; }
  .sidebar { width: 260px; flex-shrink: 0; position: sticky; top: 84px; max-height: calc(100vh - 100px);
             overflow-y: auto; background: var(--surface); border-radius: var(--radius);
             box-shadow: var(--shadow); padding: 12px 0; }
  .sidebar-title { font-size: 14px; font-weight: 700; padding: 8px 16px 12px; color: var(--text);
                   border-bottom: 1px solid var(--border); margin-bottom: 4px; }
  .sidebar summary { cursor: pointer; padding: 6px 16px; font-size: 13px; font-weight: 600;
                     color: var(--text-secondary); list-style: none; display: flex;
                     align-items: center; gap: 6px; transition: color 0.15s; user-select: none; }
  .sidebar summary::-webkit-details-marker { display: none; }
  .sidebar summary:hover { color: var(--text); background: #f8fafc; }
  .sidebar summary::before { content: '\\25B6'; font-size: 10px; transition: transform 0.2s; color: var(--primary); }
  .sidebar details[open] summary::before { transform: rotate(90deg); }
  .sidebar-genre-list { padding: 0 0 4px 0; }
  .sidebar-genre-item { display: block; padding: 4px 16px 4px 28px; font-size: 13px;
                         color: var(--text); text-decoration: none; transition: background 0.1s;
                         line-height: 1.4; }
  .sidebar-genre-item:hover { background: #eff6ff; color: var(--primary); }
  .sidebar-genre-count { color: var(--text-secondary); font-size: 11px; margin-left: 4px; }
  .main-content { flex: 1; min-width: 0; }
  @media (max-width: 800px) {
    .page-layout { flex-direction: column; }
    .sidebar { width: 100%; position: static; max-height: none; }
  }
</style>
</head>
<body>
<header>
  <h1>Flibusta Offline</h1>
  <nav>
    <a href="{{ url_for('index') }}">Главная</a>
    <a href="{{ url_for('search_page') }}">Поиск</a>
    <a href="{{ url_for('authors_list') }}">Авторы</a>
    <a href="{{ url_for('genres_list') }}">Жанры</a>
    <a href="{{ url_for('series_list') }}">Серии</a>
    <a href="{{ url_for('scan_page') }}" style="background:#10b981;color:white;">Индексация</a>
  </nav>
</header>
<div class="container">
  {% with messages = get_flashed_messages() %}
    {% if messages %}{% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endif %}
  {% endwith %}
  {% if sidebar %}
  <div class="page-layout">
    <div class="sidebar">
      <div class="sidebar-title">Жанры</div>
      {% for cat_name, cat_genres in sidebar %}
      <details>
        <summary>{{ cat_name }}</summary>
        <div class="sidebar-genre-list">
          {% for g in cat_genres %}
          <a class="sidebar-genre-item" href="{{ url_for('genre_books', genre_id=g.id) }}">
            {{ g.code|genre_name }}<span class="sidebar-genre-count">({{ g.books_count }})</span>
          </a>
          {% endfor %}
        </div>
      </details>
      {% endfor %}
    </div>
    <div class="main-content">
      {{ content|safe }}
    </div>
  </div>
  {% else %}
  {{ content|safe }}
  {% endif %}
</div>
</body>
</html>
"""

INDEX_HTML = """
<div class="stats">
  <div class="stat-card"><strong>{{ stats.books }}</strong><span>книг</span></div>
  <div class="stat-card"><strong>{{ stats.authors }}</strong><span>авторов</span></div>
  <div class="stat-card"><strong>{{ stats.genres }}</strong><span>жанров</span></div>
  <div class="stat-card"><strong>{{ stats.series }}</strong><span>серий</span></div>
  <div class="stat-card"><strong>{{ stats.archives }}</strong><span>архивов</span></div>
</div>

<div class="search-box">
  <form method="get" action="{{ url_for('search') }}">
    <input type="text" name="q" placeholder="Поиск по названию, автору или содержанию..."
           value="{{ q|default('') }}" autofocus>
    <button type="submit">Найти</button>
  </form>
</div>

{% if recent %}
<h3>Последние добавленные
  <span class="view-toggle">
    <a href="?view=list" class="{% if view_mode=='list' %}active{% endif %}">Список</a>
    <a href="?view=grid" class="{% if view_mode=='grid' %}active{% endif %}">Сетка</a>
  </span>
</h3>
<div class="book-list {% if view_mode=='grid' %}book-grid{% endif %}">
  {% for b in recent %}
  <div class="book-item">
    {% if b.has_cover %}
    <img class="book-cover-thumb" src="{{ url_for('cover_image', book_id=b.id) }}" alt="">
    {% else %}
    <div class="book-cover-thumb" style="display:flex;align-items:center;justify-content:center;font-size:18px;">📖</div>
    {% endif %}
    <div class="book-info">
      <a class="book-title" href="{{ url_for('book_detail', book_id=b.id) }}">{{ b.title }}</a>
      <div class="book-meta">{{ b.authors }} {% if b.series %}· {{ b.series }}{% endif %}</div>
    </div>
    <div class="book-actions">
      <a href="{{ url_for('read_book', book_id=b.id) }}" target="_blank">Читать</a>
      <a href="{{ url_for('download_book', book_id=b.id) }}">Скачать</a>
    </div>
  </div>
  {% endfor %}
</div>
<a class="all-books-link" href="{{ url_for('books_list') }}">📚 Все книги ({{ stats.books }})</a>
{% endif %}
"""

SEARCH_HTML = """
<div class="search-box">
  <form method="get">
    <input type="text" name="q" value="{{ q }}" placeholder="Поиск по названию, автору или содержанию..." autofocus>
    <button type="submit">Найти</button>
  </form>
</div>

{% if books %}
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
  <p style="font-size:14px;color:var(--text-secondary);margin:0;">
    Найдено: <strong style="color:var(--text);">{{ total }}</strong>
    {% if fts %}<span style="margin-left:8px;padding:2px 8px;background:#eff6ff;color:var(--primary);border-radius:20px;font-size:12px;">FTS5</span>{% endif %}
  </p>
  <span class="view-toggle">
    <a href="?q={{ q }}&view=list" class="{% if view_mode=='list' %}active{% endif %}">Список</a>
    <a href="?q={{ q }}&view=grid" class="{% if view_mode=='grid' %}active{% endif %}">Сетка</a>
  </span>
</div>
<div class="book-list {% if view_mode=='grid' %}book-grid{% endif %}">
  {% for b in books %}
  <div class="book-item">
    {% if b.has_cover %}
    <img class="book-cover-thumb" src="{{ url_for('cover_image', book_id=b.id) }}" alt="">
    {% else %}
    <div class="book-cover-thumb" style="display:flex;align-items:center;justify-content:center;font-size:18px;">📖</div>
    {% endif %}
    <div class="book-info">
      <a class="book-title" href="{{ url_for('book_detail', book_id=b.id) }}">{{ b.title }}</a>
      <div class="book-meta">{{ b.authors }} {% if b.series %}· {{ b.series }}{% endif %}</div>
      {% if b.snippet %}<div class="snippet">{{ b.snippet|safe }}</div>{% endif %}
    </div>
    <div class="book-actions">
      <a href="{{ url_for('read_book', book_id=b.id) }}" target="_blank">Читать</a>
      <a href="{{ url_for('download_book', book_id=b.id) }}">Скачать</a>
    </div>
  </div>
  {% endfor %}
</div>
{{ pagination|safe }}
{% else %}
<div class="empty">Ничего не найдено по запросу «{{ q }}»</div>
{% endif %}
"""

BOOK_HTML = """
<div class="book-detail">
  {% if cover_url %}
  <img class="cover-img" src="{{ cover_url }}" alt="Обложка" onerror="this.style.display='none'">
  {% endif %}
  <h2>{{ book.title }}</h2>
  {% if book.description %}
  <div class="annotation">{{ book.description }}</div>
  {% endif %}

  <div class="meta-section">
    <div class="meta-row"><strong>Авторы:</strong>
      {% for a in authors %}
        <a class="tag" href="{{ url_for('author_books', author_id=a.id) }}">{{ a.full_name }}</a>
      {% endfor %}
    </div>
    {% if translators %}
    <div class="meta-row"><strong>Переводчики:</strong>
      {% for t in translators %}
        <span class="tag">{{ t }}</span>
      {% endfor %}
    </div>
    {% endif %}
    {% if genres %}
    <div class="meta-row"><strong>Жанры:</strong>
      {% for g in genres %}
        <a class="tag" href="{{ url_for('genre_books', genre_id=g.id) }}">{{ g.code|genre_name }}</a>
      {% endfor %}
    </div>
    {% endif %}
    {% if book.series %}
    <div class="meta-row"><strong>Серия:</strong>
      <a class="tag" href="{{ url_for('series_books', series_id=book.series_id) }}">
        {{ book.series }} {% if book.seq_number %}(№{{ book.seq_number }}){% endif %}
      </a>
    </div>
    {% endif %}
    {% if book.lang %}
    <div class="meta-row"><strong>Язык:</strong> {{ book.lang }}</div>
    {% endif %}
    {% if meta.year %}
    <div class="meta-row"><strong>Год:</strong> {{ meta.year }}</div>
    {% endif %}
    {% if meta.date %}
    <div class="meta-row"><strong>Дата:</strong> {{ meta.date }}</div>
    {% endif %}
    {% if meta.publisher %}
    <div class="meta-row"><strong>Издательство:</strong> {{ meta.publisher }}</div>
    {% endif %}
    {% if meta.city %}
    <div class="meta-row"><strong>Город:</strong> {{ meta.city }}</div>
    {% endif %}
    {% if meta.isbn %}
    <div class="meta-row"><strong>ISBN:</strong> {{ meta.isbn }}</div>
    {% endif %}
    {% if meta.keywords %}
    <div class="meta-row"><strong>Ключевые слова:</strong> {{ meta.keywords }}</div>
    {% endif %}
    {% if meta.doc_id %}
    <div class="meta-row"><strong>ID документа:</strong> {{ meta.doc_id }}</div>
    {% endif %}
    {% if meta.program_used %}
    <div class="meta-row"><strong>Программа:</strong> {{ meta.program_used }}</div>
    {% endif %}
    {% if meta.doc_date %}
    <div class="meta-row"><strong>Дата документа:</strong> {{ meta.doc_date }}</div>
    {% endif %}
    <div class="meta-row"><strong>Размер:</strong> {{ book.file_size|filesizeformat }}</div>
  </div>

  <div style="margin-top: 24px; display: flex; gap: 10px; flex-wrap: wrap;">
    <a href="{{ url_for('read_book', book_id=book.id) }}" target="_blank"
       class="btn btn-accent">Читать</a>
    <a href="{{ url_for('download_book', book_id=book.id) }}"
       class="btn btn-success">Скачать FB2</a>
  </div>
</div>
"""

AUTHORS_HTML = """
<h2>Авторы <span style="font-weight:400;font-size:16px;color:var(--text-secondary);">({{ total }})</span></h2>
<div class="card-grid">
  {% for a in authors %}
  <a class="card" href="{{ url_for('author_books', author_id=a.id) }}">
    <div class="card-avatar">{{ a.full_name[:1] }}</div>
    <div class="card-title">{{ a.full_name }}</div>
    <div class="card-count">{{ a.books_count }} книг</div>
  </a>
  {% endfor %}
</div>
{{ pagination|safe }}
"""

GENRES_HTML = """
<h2>Жанры</h2>
{% for cat_name, cat_genres in categories %}
<div class="genre-category">
  <h3 class="genre-category-title">{{ cat_name }}</h3>
  <div class="card-grid">
    {% for g in cat_genres %}
    <a class="card card-tag" href="{{ url_for('genre_books', genre_id=g.id) }}">
      <div class="card-title">{{ g.code|genre_name }}</div>
      <div class="card-count">{{ g.books_count }} книг</div>
    </a>
    {% endfor %}
  </div>
</div>
{% endfor %}
"""

SERIES_HTML = """
<h2>Серии <span style="font-weight:400;font-size:16px;color:var(--text-secondary);">({{ total }})</span></h2>
<div class="card-grid">
  {% for s in series %}
  <a class="card" href="{{ url_for('series_books', series_id=s.id) }}">
    <div class="card-avatar" style="background:var(--series-color, #8b5cf6);">{{ s.name[:1] }}</div>
    <div class="card-title">{{ s.name }}</div>
    <div class="card-count">{{ s.books_count }} книг</div>
  </a>
  {% endfor %}
</div>
{{ pagination|safe }}
"""

SCAN_HTML = """
<div class="scan-page">
  <h2>Индексация библиотеки</h2>

  <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px;">
    <form method="post" action="{{ url_for('scan_start') }}" id="scan-form" style="flex:1;min-width:300px;">
      <div class="form-group">
        <label>Путь к папке с ZIP-архивами:</label>
        <input type="text" name="path" id="scan-path" value="{{ current_path }}"
               placeholder="C:\\path\\to\\archives">
      </div>
      <div class="form-group" style="margin-top:6px;">
        <label style="display:flex;align-items:center;gap:6px;font-weight:400;cursor:pointer;">
          <input type="checkbox" name="store_content" value="1">
          Сохранять полный текст (нужно для FTS5-поиска)
        </label>
      </div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;">
        <button type="submit" id="scan-btn" class="btn btn-success"
                {% if is_running %}disabled{% endif %}>
          {% if resume_mode %}Продолжить сканирование{% else %}Сканировать (многопоточно){% endif %}
        </button>
        <button type="submit" formaction="{{ url_for('build_fts_route') }}"
                class="btn btn-primary" {% if is_running %}disabled{% endif %}>
          Построить FTS5-индекс
        </button>
      </div>
    </form>
    <form method="post" action="{{ url_for('clear_db') }}" style="align-self:flex-end;"
          onsubmit="return confirm('Очистить всю базу данных? Все проиндексированные книги будут удалены.');">
      <button type="submit" class="btn btn-danger"
              {% if is_running %}disabled{% endif %}>Очистить базу</button>
    </form>
    <form method="post" action="{{ url_for('cleanup_content') }}" style="align-self:flex-end;"
          onsubmit="return confirm('Удалить содержимое книг и FTS5-индекс? Поиск перестанет работать до перестроения индекса, но освободится ~95% места.');">
      <button type="submit" class="btn btn-danger"
              {% if is_running %}disabled{% endif %}>Освободить место (удалить текст книг)</button>
    </form>
  </div>

  <div class="progress-info" style="margin-bottom:16px;">
    Размер БД: <strong>{{ db_size }}</strong>
  </div>

  {% if has_progress %}
  <div class="progress-section">
    <h3>Прогресс</h3>
    <p class="progress-info">
      Статус: <span class="status-badge status-{{ status_class }}">{{ status_text }}</span>
    </p>
    <div class="progress-bar-outer">
      <div class="progress-bar-inner" style="width:{{ progress_pct }}%">
        <span>{{ progress_pct }}%</span>
      </div>
    </div>
    <p class="progress-info">
      <strong>{{ processed_archives }}</strong> / <strong>{{ total_archives }}</strong> архивов
      {% if remaining_archives > 0 %}({{ remaining_archives }} осталось){% endif %}
    </p>
    <p class="progress-info">Найдено книг: <strong>{{ total_books }}</strong></p>
    {% if current_archive %}
    <p class="progress-info">Текущий архив: <strong>{{ current_archive }}</strong></p>
    {% endif %}
    {% if error %}
    <p class="progress-info" style="color:#e74c3c;">Ошибка: {{ error }}</p>
    {% endif %}
    {% if is_running %}
    <form method="post" action="{{ url_for('scan_stop') }}" style="margin-top:15px;">
      <button type="submit" class="btn btn-danger">Остановить</button>
    </form>
    {% endif %}
  </div>
  {% endif %}
</div>

<script>
(function() {
  var form = document.getElementById('scan-form');
  var pathInput = document.getElementById('scan-path');
  var btns = form.querySelectorAll('button[type=submit]');

  function disableAll() {
    for (var i = 0; i < btns.length; i++) {
      btns[i].disabled = true;
      if (!btns[i].hasAttribute('formaction')) {
        btns[i].textContent = btns[i].textContent.replace('(многопоточно)', '').trim() + '...';
      }
    }
  }

  function checkStatus() {
    fetch('{{ url_for("scan_status_json") }}')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.running) {
          disableAll();
          setTimeout(function() { window.location.reload(); }, 2000);
        }
      });
  }

  checkStatus();

  form.addEventListener('submit', function(e) {
    var target = e.submitter || btns[0];
    if (!target.hasAttribute('formaction') && !pathInput.value.trim()) {
      e.preventDefault();
      alert('Укажите путь к папке с архивами');
      return false;
    }
    if (!target.hasAttribute('formaction')) {
      disableAll();
    }
  });
})();
</script>
"""


def render_page(title, content, **ctx):
    return render_template_string(BASE_HTML, title=title, content=content, **ctx)


def pagination_html(total, page, per_page, endpoint, **kwargs):
    if total <= per_page:
        return ""
    pages = (total + per_page - 1) // per_page
    html = ['<div class="pagination">']
    if page > 1:
        html.append(f'<a href="{url_for(endpoint, page=page-1, **kwargs)}"><</a>')
    start = max(1, page - 3)
    end = min(pages, page + 3)
    for p in range(start, end + 1):
        cls = 'active' if p == page else ''
        html.append(f'<a class="{cls}" href="{url_for(endpoint, page=p, **kwargs)}">{p}</a>')
    if page < pages:
        html.append(f'<a href="{url_for(endpoint, page=page+1, **kwargs)}">></a>')
    html.append('</div>')
    return ''.join(html)


# ============================================================================
# МАРШРУТЫ
# ============================================================================

@app.template_filter('filesizeformat')
def filesizeformat(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        swallow.note('render.filesizeformat:L2002')
        return '—'
    for unit in ['B', 'KB', 'MB', 'GB']:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


@app.template_filter('genre_name')
def genre_name_filter(code):
    return RUSSIAN_GENRES.get(code, code)


@app.route('/')
def index():
    view_mode = request.args.get('view', 'list')
    with db_connection() as conn:
        stats = {
            'books': conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
            'authors': conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0],
            'genres': conn.execute("SELECT COUNT(*) FROM genres").fetchone()[0],
            'series': conn.execute("SELECT COUNT(*) FROM series").fetchone()[0],
            'archives': conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0],
        }
        recent = conn.execute("""
            SELECT b.id, b.title, b.series_id, s.name as series,
                   GROUP_CONCAT(a.full_name, ', ') as authors,
                   CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
            FROM books b
            LEFT JOIN series s ON b.series_id = s.id
            LEFT JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN authors a ON a.id = ba.author_id
            LEFT JOIN covers c ON c.book_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC LIMIT 20
        """).fetchall()
        genres = conn.execute("""
            SELECT g.id, g.code, COUNT(bg.book_id) as books_count
            FROM genres g
            LEFT JOIN book_genres bg ON bg.genre_id = g.id
            GROUP BY g.id
            ORDER BY g.code
        """).fetchall()
    categories = {}
    for g in genres:
        cat = genre_category(g['code'])
        categories.setdefault(cat, []).append(g)
    sidebar = sorted(categories.items())
    content = render_template_string(INDEX_HTML, stats=stats, recent=recent, view_mode=view_mode)
    return render_page('Главная', content, stats=stats, sidebar=sidebar)


BOOKS_ALL_HTML = """
<h2>Все книги <span style="font-weight:400;font-size:16px;color:var(--text-secondary);">({{ total }})</span></h2>
<div class="book-list">
  {% for b in books %}
  <div class="book-item">
    {% if b.has_cover %}
    <img class="book-cover-thumb" src="{{ url_for('cover_image', book_id=b.id) }}" alt="">
    {% else %}
    <div class="book-cover-thumb" style="display:flex;align-items:center;justify-content:center;font-size:18px;">📖</div>
    {% endif %}
    <div class="book-info">
      <a class="book-title" href="{{ url_for('book_detail', book_id=b.id) }}">{{ b.title }}</a>
      <div class="book-meta">{{ b.authors }} {% if b.series %}· {{ b.series }}{% endif %}</div>
    </div>
    <div class="book-actions">
      <a href="{{ url_for('read_book', book_id=b.id) }}" target="_blank">Читать</a>
      <a href="{{ url_for('download_book', book_id=b.id) }}">Скачать</a>
    </div>
  </div>
  {% endfor %}
</div>
{{ pagination|safe }}
"""


@app.route('/books')
def books_list():
    page = int(request.args.get('page', 1))
    with db_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        books = conn.execute("""
            SELECT b.id, b.title, GROUP_CONCAT(a.full_name, ', ') as authors,
                   CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
            FROM books b
            LEFT JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN authors a ON a.id = ba.author_id
            LEFT JOIN covers c ON c.book_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT ? OFFSET ?
        """, (PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
    pg = pagination_html(total, page, PAGE_SIZE, 'books_list')
    content = render_template_string(BOOKS_ALL_HTML, books=books, total=total, pagination=pg)
    return render_page('Все книги', content)


@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    page = int(request.args.get('page', 1))
    view_mode = request.args.get('view', 'list')
    if not q:
        return redirect(url_for('index'))

    fts_used = False
    with db_connection() as conn:
        # Пробуем FTS5
        try:
            fts_available = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='books_fts'"
            ).fetchone() is not None

            if fts_available:
                count_row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM books_fts"
                ).fetchone()
                fts_has_data = count_row and count_row['cnt'] > 0
            else:
                fts_has_data = False
        except Exception:
            swallow.note('render.search:L2125')
            fts_has_data = False

        if fts_has_data:
            try:
                terms = ' AND '.join(f'"{w}"' for w in q.strip().split() if w)
                if not terms:
                    terms = f'"{q}"'

                total = conn.execute("""
                    SELECT COUNT(*) as cnt
                    FROM books_fts
                    WHERE books_fts MATCH ?
                """, (terms,)).fetchone()['cnt']

                books_data = conn.execute("""
                    SELECT f.rowid,
                           snippet(books_fts, 0, '<b>', '</b>', '...', 48) as title_snippet,
                           snippet(books_fts, 1, '<b>', '</b>', '...', 64) as content_snippet
                    FROM books_fts f
                    WHERE books_fts MATCH ?
                    ORDER BY rank
                    LIMIT ? OFFSET ?
                """, (terms, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()

                book_ids = [b['rowid'] for b in books_data]
                snippet_map = {}
                for b in books_data:
                    snip = b['content_snippet'] or b['title_snippet'] or ''
                    # Убираем лишние многоточия
                    snippet_map[b['rowid']] = snip

                if book_ids:
                    placeholders = ','.join('?' * len(book_ids))
                    books = conn.execute(f"""
                        SELECT b.id, b.title, b.series_id, s.name as series,
                               GROUP_CONCAT(a.full_name, ', ') as authors,
                               CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
                        FROM books b
                        LEFT JOIN series s ON b.series_id = s.id
                        LEFT JOIN book_authors ba ON ba.book_id = b.id
                        LEFT JOIN authors a ON a.id = ba.author_id
                        LEFT JOIN covers c ON c.book_id = b.id
                        WHERE b.id IN ({placeholders})
                        GROUP BY b.id
                    """, book_ids).fetchall()
                else:
                    books = []

                fts_used = True

                # Сортируем в том же порядке, что и FTS5 результаты
                id_order = {bid: i for i, bid in enumerate(book_ids)}
                books = sorted(books, key=lambda x: id_order.get(x['id'], 999))

                books_list = []
                for b in books:
                    bd = dict(b)
                    bd['snippet'] = snippet_map.get(b['id'], '')
                    books_list.append(bd)
                books = books_list

                pg = pagination_html(total, page, PAGE_SIZE, 'search', q=q)
                content = render_template_string(SEARCH_HTML, books=books, q=q,
                                                  total=total, pagination=pg, fts=True,
                                                  view_mode=view_mode)
                return render_page(f'Поиск: {q}', content, q=q)

            except Exception as e:
                print(f"FTS5 search error: {e}, falling back to LIKE")
                fts_has_data = False

        # Fallback на LIKE
        total = conn.execute("""
            SELECT COUNT(DISTINCT b.id) FROM books b
            LEFT JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN authors a ON a.id = ba.author_id
            WHERE b.title LIKE ? OR a.full_name LIKE ?
        """, (f'%{q}%', f'%{q}%')).fetchone()[0]

        books = conn.execute("""
            SELECT b.id, b.title, b.series_id, s.name as series,
                   GROUP_CONCAT(a.full_name, ', ') as authors,
                   CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
            FROM books b
            LEFT JOIN series s ON b.series_id = s.id
            LEFT JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN authors a ON a.id = ba.author_id
            LEFT JOIN covers c ON c.book_id = b.id
            WHERE b.title LIKE ? OR a.full_name LIKE ?
            GROUP BY b.id
            ORDER BY b.title
            LIMIT ? OFFSET ?
        """, (f'%{q}%', f'%{q}%', PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()

    pg = pagination_html(total, page, PAGE_SIZE, 'search', q=q)
    content = render_template_string(SEARCH_HTML, books=books, q=q, total=total, pagination=pg, fts=False, view_mode=view_mode)
    return render_page(f'Поиск: {q}', content, q=q)


@app.route('/search-page')
def search_page():
    return redirect(url_for('search'))


@app.route('/book/<int:book_id>')
def book_detail(book_id):
    with db_connection() as conn:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if not book:
            abort(404)
        archive = conn.execute(
            "SELECT filename FROM archives WHERE id = ?", (book['archive_id'],)
        ).fetchone()
        authors = conn.execute("""
            SELECT a.* FROM authors a
            JOIN book_authors ba ON ba.author_id = a.id
            WHERE ba.book_id = ?
        """, (book_id,)).fetchall()
        genres = conn.execute("""
            SELECT g.* FROM genres g
            JOIN book_genres bg ON bg.genre_id = g.id
            WHERE bg.book_id = ?
        """, (book_id,)).fetchall()
        has_cover = conn.execute(
            "SELECT 1 FROM covers WHERE book_id = ?", (book_id,)
        ).fetchone() is not None

    meta = {}
    translators = []
    try:
        extra = json_mod.loads(book['extra'] or '{}')
        meta = {k: v for k, v in extra.items() if v}
        translators = meta.pop('translators', [])
    except (json_mod.JSONDecodeError, TypeError):
        swallow.note('render.book_detail:L2260')
        pass

    cover_url = url_for('cover_image', book_id=book_id) if has_cover else None
    content = render_template_string(BOOK_HTML, book=book, archive=archive,
                                      authors=authors, genres=genres, cover_url=cover_url,
                                      meta=meta, translators=translators)
    return render_page(book['title'], content)


@app.route('/cover/<int:book_id>')
def cover_image(book_id):
    with db_connection() as conn:
        cover = conn.execute(
            "SELECT image_data, mime_type FROM covers WHERE book_id = ?",
            (book_id,)
        ).fetchone()
    if not cover:
        abort(404)
    return Response(cover['image_data'], mimetype=cover['mime_type'])


@app.route('/download/<int:book_id>')
def download_book(book_id):
    with db_connection() as conn:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if not book:
            abort(404)
        archive = conn.execute(
            "SELECT filename FROM archives WHERE id = ?", (book['archive_id'],)
        ).fetchone()

    archive_path = find_archive(archive['filename'])
    if not archive_path:
        flash(f"Архив {archive['filename']} не найден на диске")
        return redirect(url_for('book_detail', book_id=book_id))

    try:
        with zipfile.ZipFile(archive_path, 'r') as zf:
            fb2_data = zf.read(book['file_path'])
    except Exception as e:
        swallow.note('render.download_book:L2301', e)
        flash(f"Ошибка чтения: {e}")
        return redirect(url_for('book_detail', book_id=book_id))

    filename = re.sub(r'[^\w\-.]', '_', book['title']) + '.fb2'
    return send_file(BytesIO(fb2_data), as_attachment=True,
                     download_name=filename, mimetype='application/xml')


@app.route('/read/<int:book_id>')
def read_book(book_id):
    with db_connection() as conn:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if not book:
            abort(404)
        archive = conn.execute(
            "SELECT filename FROM archives WHERE id = ?", (book['archive_id'],)
        ).fetchone()

    archive_path = find_archive(archive['filename'])
    if not archive_path:
        flash(f"Архив {archive['filename']} не найден на диске")
        return redirect(url_for('book_detail', book_id=book_id))

    try:
        with zipfile.ZipFile(archive_path, 'r') as zf:
            xml_bytes = zf.read(book['file_path'])
    except Exception as e:
        swallow.note('render.read_book:L2329', e)
        flash(f"Ошибка чтения: {e}")
        return redirect(url_for('book_detail', book_id=book_id))

    html_content = fb2_to_html(xml_bytes)
    if html_content is None:
        flash("Ошибка конвертации FB2 в HTML")
        return redirect(url_for('book_detail', book_id=book_id))

    return Response(html_content, mimetype='text/html; charset=utf-8')


@app.route('/authors')
def authors_list():
    page = int(request.args.get('page', 1))
    with db_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
        authors = conn.execute("""
            SELECT a.id, a.full_name, COUNT(ba.book_id) as books_count
            FROM authors a
            LEFT JOIN book_authors ba ON ba.author_id = a.id
            GROUP BY a.id
            ORDER BY a.last_name, a.full_name
            LIMIT ? OFFSET ?
        """, (PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
    pg = pagination_html(total, page, PAGE_SIZE, 'authors_list')
    content = render_template_string(AUTHORS_HTML, authors=authors, total=total, pagination=pg)
    return render_page('Авторы', content, stats={'authors': total})


@app.route('/author/<int:author_id>')
def author_books(author_id):
    page = int(request.args.get('page', 1))
    with db_connection() as conn:
        author = conn.execute("SELECT * FROM authors WHERE id = ?", (author_id,)).fetchone()
        if not author:
            abort(404)
        total = conn.execute("""
            SELECT COUNT(*) FROM book_authors WHERE author_id = ?
        """, (author_id,)).fetchone()[0]
        books = conn.execute("""
            SELECT b.id, b.title, b.series_id, s.name as series,
                   CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
            FROM books b
            JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN series s ON b.series_id = s.id
            LEFT JOIN covers c ON c.book_id = b.id
            WHERE ba.author_id = ?
            ORDER BY b.title
            LIMIT ? OFFSET ?
        """, (author_id, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()

    pg = pagination_html(total, page, PAGE_SIZE, 'author_books', author_id=author_id)
    books_html = []
    for b in books:
        bc = f'<img class="book-cover-thumb" src="{url_for("cover_image", book_id=b["id"])}" alt="">' if b['has_cover'] else '<div class="book-cover-thumb" style="display:flex;align-items:center;justify-content:center;font-size:18px;">📖</div>'
        books_html.append(f"""
        <div class="book-item">
          {bc}
          <div class="book-info">
            <a class="book-title" href="{url_for('book_detail', book_id=b["id"])}">{b["title"]}</a>
            <div class="book-meta">{b["series"] or ''}</div>
          </div>
        </div>""")
    afn = author['full_name']
    content = f"<h2>{afn}</h2><p>Книг: {total}</p>"
    content += f"<div class='book-list'>{''.join(books_html)}</div>{pg}"
    return render_page(afn, content)


@app.route('/genres')
def genres_list():
    with db_connection() as conn:
        genres = conn.execute("""
            SELECT g.id, g.code, COUNT(bg.book_id) as books_count
            FROM genres g
            LEFT JOIN book_genres bg ON bg.genre_id = g.id
            GROUP BY g.id
            ORDER BY g.code
        """).fetchall()
    categories = {}
    for g in genres:
        cat = genre_category(g['code'])
        categories.setdefault(cat, []).append(g)
    categories = sorted(categories.items())
    content = render_template_string(GENRES_HTML, categories=categories)
    return render_page('Жанры', content)


@app.route('/genre/<int:genre_id>')
def genre_books(genre_id):
    page = int(request.args.get('page', 1))
    with db_connection() as conn:
        genre = conn.execute("SELECT * FROM genres WHERE id = ?", (genre_id,)).fetchone()
        if not genre:
            abort(404)
        total = conn.execute(
            "SELECT COUNT(*) FROM book_genres WHERE genre_id = ?", (genre_id,)
        ).fetchone()[0]
        books = conn.execute("""
            SELECT b.id, b.title, GROUP_CONCAT(a.full_name, ', ') as authors,
                   CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
            FROM books b
            JOIN book_genres bg ON bg.book_id = b.id
            LEFT JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN authors a ON a.id = ba.author_id
            LEFT JOIN covers c ON c.book_id = b.id
            WHERE bg.genre_id = ?
            GROUP BY b.id
            ORDER BY b.title
            LIMIT ? OFFSET ?
        """, (genre_id, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()

    pg = pagination_html(total, page, PAGE_SIZE, 'genre_books', genre_id=genre_id)
    books_html = []
    for b in books:
        bc = f'<img class="book-cover-thumb" src="{url_for("cover_image", book_id=b["id"])}" alt="">' if b['has_cover'] else '<div class="book-cover-thumb" style="display:flex;align-items:center;justify-content:center;font-size:18px;">📖</div>'
        books_html.append(f"""
        <div class="book-item">
          {bc}
          <div class="book-info">
            <a class="book-title" href="{url_for('book_detail', book_id=b["id"])}">{b["title"]}</a>
            <div class="book-meta">{b["authors"] or ''}</div>
          </div>
        </div>""")
    gn = genre_name(genre['code'])
    content = f"<h2>Жанр: {gn}</h2><p>Книг: {total}</p>"
    content += f"<div class='book-list'>{''.join(books_html)}</div>{pg}"
    return render_page(gn, content)


@app.route('/series')
def series_list():
    page = int(request.args.get('page', 1))
    with db_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        series = conn.execute("""
            SELECT s.id, s.name, COUNT(b.id) as books_count
            FROM series s
            LEFT JOIN books b ON b.series_id = s.id
            GROUP BY s.id
            ORDER BY s.name
            LIMIT ? OFFSET ?
        """, (PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
    pg = pagination_html(total, page, PAGE_SIZE, 'series_list')
    content = render_template_string(SERIES_HTML, series=series, total=total, pagination=pg)
    return render_page('Серии', content)


@app.route('/series/<int:series_id>')
def series_books(series_id):
    with db_connection() as conn:
        series = conn.execute("SELECT * FROM series WHERE id = ?", (series_id,)).fetchone()
        if not series:
            abort(404)
        books = conn.execute("""
            SELECT b.id, b.title, b.seq_number,
                   GROUP_CONCAT(a.full_name, ', ') as authors,
                   CASE WHEN c.book_id IS NOT NULL THEN 1 ELSE 0 END as has_cover
            FROM books b
            LEFT JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN authors a ON a.id = ba.author_id
            LEFT JOIN covers c ON c.book_id = b.id
            WHERE b.series_id = ?
            GROUP BY b.id
            ORDER BY CAST(b.seq_number AS INTEGER), b.title
        """, (series_id,)).fetchall()

    books_html = []
    for b in books:
        sn = b['seq_number']
        num = f"N{sn} " if sn else ""
        bc = f'<img class="book-cover-thumb" src="{url_for("cover_image", book_id=b["id"])}" alt="">' if b['has_cover'] else '<div class="book-cover-thumb" style="display:flex;align-items:center;justify-content:center;font-size:18px;">📖</div>'
        books_html.append(f"""
        <div class="book-item">
          {bc}
          <div class="book-info">
            <a class="book-title" href="{url_for('book_detail', book_id=b["id"])}">
              {num}{b["title"]}
            </a>
            <div class="book-meta">{b["authors"] or ''}</div>
          </div>
        </div>""")
    sn = series['name']
    content = f"<h2>Серия: {sn}</h2><p>Книг: {len(books)}</p>"
    content += f"<div class='book-list'>{''.join(books_html)}</div>"
    return render_page(sn, content)


# ============================================================================
# МАРШРУТЫ ИНДЕКСАЦИИ
# ============================================================================

# Глобальная переменная для фонового потока
_scan_thread = None
_scan_stop_flag = threading.Event()


def _progress_callback(*args, **kwargs):
    if _scan_stop_flag.is_set():
        raise KeyboardInterrupt("Scan stopped by user")

    # Позиционная форма: (archive_name, processed, total_archives, total_books, status)
    if args:
        archive_name = args[0] or ''
        processed = int(args[1]) if len(args) > 1 else 0
        total_archives = int(args[2]) if len(args) > 2 else 0
        total_books = int(args[3]) if len(args) > 3 else 0
        status = args[4] if len(args) > 4 else 'running'
    else:
        status = kwargs.get('status', 'running')
        archive_name = kwargs.get('archive_name', '') or kwargs.get('current_archive', '') or ''
        processed = int(kwargs.get('processed_archives', 0) or kwargs.get('processed', 0) or 0)
        total_archives = int(kwargs.get('total_archives', 0) or kwargs.get('total', 0) or 0)
        total_books = int(kwargs.get('total_books', 0) or 0)

    if status == 'error':
        error_msg = kwargs.get('error', '') or (args[5] if len(args) > 5 else '')
        update_scan_progress(status='error', error=error_msg)
    elif status == 'completed':
        update_scan_progress(status='completed', total=total_archives,
                             processed=processed, books=total_books)
    elif status == 'indexing':
        if total_archives > 0:
            pct = int(processed / total_archives * 100) if total_archives > 0 else 0
            update_scan_progress(
                status='indexing',
                total_archives=total_archives,
                processed_archives=processed,
                total_books=0,
                current_archive=f"FTS5: {pct}%"
            )
        else:
            update_scan_progress(status='completed', total=0, processed=0, books=0)
    else:
        p = get_scan_progress()
        current_path = p['archives_path'] if p else ''
        update_scan_progress(
            path=current_path,
            total=total_archives,
            processed=processed,
            books=total_books,
            current=archive_name,
            status='running'
        )


def _run_scan(path, store_content=False):
    global _scan_thread
    try:
        update_scan_progress(path=path, status='running')
        scan_archives_parallel(path, progress_callback=_progress_callback, store_content=store_content)
        p = get_scan_progress()
        if p and p['status'] != 'error':
            update_scan_progress(status='completed')
    except KeyboardInterrupt:
        swallow.note('render._run_scan:L2585')
        update_scan_progress(status='idle', current='(остановлен пользователем)')
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        update_scan_progress(status='error', error=str(e))
        print(f"Scan error: {e}")
        print(tb)
    finally:
        _scan_thread = None


@app.route('/admin/scan')
def scan_page():
    progress = get_scan_progress()
    is_running = _scan_thread is not None and _scan_thread.is_alive()

    if progress and not is_running:
        has_progress = progress['status'] in ('running', 'completed', 'error')
    else:
        has_progress = progress is not None and progress['status'] != 'idle'

    ctx = {
        'current_path': progress['archives_path'] if progress else '',
        'is_running': is_running,
        'has_progress': has_progress,
        'resume_mode': False,
    }

    if progress:
        ctx['total_archives'] = progress['total_archives'] or 0
        ctx['processed_archives'] = progress['processed_archives'] or 0
        ctx['total_books'] = progress['total_books'] or 0
        ctx['current_archive'] = progress['current_archive'] or ''
        ctx['error'] = progress.get('error', '') or ''
        ctx['remaining_archives'] = max(0, ctx['total_archives'] - ctx['processed_archives'])

        if ctx['total_archives'] > 0:
            ctx['progress_pct'] = min(100, int(ctx['processed_archives'] / ctx['total_archives'] * 100))
        else:
            ctx['progress_pct'] = 0

        status = progress['status'] or 'idle'
        if is_running:
            ctx['status_text'] = 'Идёт сканирование...'
            ctx['status_class'] = 'running'
        elif status == 'completed':
            ctx['status_text'] = 'Завершено'
            ctx['status_class'] = 'completed'
        elif status == 'error':
            ctx['status_text'] = 'Ошибка'
            ctx['status_class'] = 'error'
        elif status == 'idle' and ctx['total_archives'] > 0:
            ctx['status_text'] = 'Прервано (можно продолжить)'
            ctx['status_class'] = 'idle'
            ctx['resume_mode'] = True
        else:
            ctx['status_text'] = 'Ожидание'
            ctx['status_class'] = 'idle'
    else:
        ctx.update({
            'total_archives': 0, 'processed_archives': 0, 'total_books': 0,
            'current_archive': '', 'error': '', 'remaining_archives': 0,
            'progress_pct': 0, 'status_text': 'Нет данных', 'status_class': 'idle',
        })

    ctx['db_size'] = db_size_str()
    content = render_template_string(SCAN_HTML, **ctx)
    return render_page('Индексация', content)


@app.route('/admin/scan/start', methods=['POST'])
def scan_start():
    global _scan_thread, _scan_stop_flag

    if _scan_thread and _scan_thread.is_alive():
        flash("Сканирование уже запущено")
        return redirect(url_for('scan_page'))

    path = request.form.get('path', '').strip()
    store_content = request.form.get('store_content') == '1'

    if not path:
        progress = get_scan_progress()
        if progress and progress['archives_path']:
            path = progress['archives_path']
        else:
            flash("Укажите путь к папке с архивами")
            return redirect(url_for('scan_page'))

    if not os.path.isdir(path):
        flash(f"Папка не найдена: {path}")
        return redirect(url_for('scan_page'))

    _scan_stop_flag.clear()
    _scan_thread = threading.Thread(target=_run_scan, args=(path, store_content), daemon=True)
    _scan_thread.start()

    return redirect(url_for('scan_page'))


@app.route('/admin/scan/stop', methods=['POST'])
def scan_stop():
    global _scan_stop_flag
    _scan_stop_flag.set()
    flash("Сканирование остановлено")
    return redirect(url_for('scan_page'))


@app.route('/admin/clear-db', methods=['POST'])
def clear_db():
    global _scan_thread
    if _scan_thread and _scan_thread.is_alive():
        flash("Сначала дождитесь завершения сканирования")
        return redirect(url_for('scan_page'))
    with db_connection() as conn:
        conn.executescript("""
            DROP TABLE IF EXISTS books_fts;
            DROP TABLE IF EXISTS book_content;
            DROP TABLE IF EXISTS covers;
            DROP TABLE IF EXISTS book_genres;
            DROP TABLE IF EXISTS book_authors;
            DROP TABLE IF EXISTS books;
            DROP TABLE IF EXISTS genres;
            DROP TABLE IF EXISTS series;
            DROP TABLE IF EXISTS authors;
            DROP TABLE IF EXISTS archives;
            DROP TABLE IF EXISTS scan_progress;
        """)
    init_db()
    flash("База данных очищена")
    return redirect(url_for('scan_page'))


def db_size_str():
    try:
        size = os.path.getsize(DB_PATH)
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
    except OSError:
        swallow.note('render.db_size_str:L2728')
        return "—"


@app.route('/admin/cleanup-content', methods=['POST'])
def cleanup_content():
    if _scan_thread and _scan_thread.is_alive():
        flash("Сначала дождитесь завершения сканирования")
        return redirect(url_for('scan_page'))
    with db_connection() as conn:
        conn.executescript("""
            DROP TABLE IF EXISTS book_content;
            DROP TABLE IF EXISTS books_fts;
        """)
        try:
            conn.execute("DROP TABLE IF EXISTS books_fts_data")
            conn.execute("DROP TABLE IF EXISTS books_fts_idx")
            conn.execute("DROP TABLE IF EXISTS books_fts_content")
            conn.execute("DROP TABLE IF EXISTS books_fts_docsize")
            conn.execute("DROP TABLE IF EXISTS books_fts_config")
        except Exception:
            swallow.note('render.cleanup_content:L2749')
            pass
    # VACUUM to reclaim space
    with db_connection() as conn:
        conn.execute("VACUUM")
    flash(f"Содержимое книг удалено. Освобождено место. Текущий размер: {db_size_str()}")
    return redirect(url_for('scan_page'))


@app.route('/admin/scan/status')
def scan_status_json():
    progress = get_scan_progress()
    is_running = _scan_thread is not None and _scan_thread.is_alive()

    data = {
        'running': is_running,
        'status': progress['status'] if progress else 'idle',
        'total_archives': progress['total_archives'] if progress else 0,
        'processed_archives': progress['processed_archives'] if progress else 0,
        'total_books': progress['total_books'] if progress else 0,
        'current_archive': progress['current_archive'] if progress else '',
        'error': progress.get('error', '') if progress else '',
    }
    if data['total_archives'] > 0:
        data['progress_pct'] = int(data['processed_archives'] / data['total_archives'] * 100)
    else:
        data['progress_pct'] = 0

    return jsonify(data)


@app.route('/admin/build-fts', methods=['POST'])
def build_fts_route():
    global _scan_thread, _scan_stop_flag

    if _scan_thread and _scan_thread.is_alive():
        flash("Сначала дождитесь завершения сканирования")
        return redirect(url_for('scan_page'))

    _scan_stop_flag.clear()
    _scan_thread = threading.Thread(target=_run_fts, daemon=True)
    _scan_thread.start()

    flash("Построение FTS5-индекса запущено")
    return redirect(url_for('scan_page'))


def _run_fts():
    global _scan_thread
    try:
        build_fts_index(progress_callback=_progress_callback)
        p = get_scan_progress()
        if p and p['status'] != 'error':
            update_scan_progress(status='completed')
    except Exception as e:
        update_scan_progress(status='error', error=str(e))
        print(f"FTS5 error: {e}")
    finally:
        _scan_thread = None


# ============================================================================
# ТОЧКА ВХОДА
# ============================================================================

if __name__ == '__main__':
    # Parse --db <path> before the command
    _db_path_arg = None
    _argv = []
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == '--db' and i + 1 < len(sys.argv):
            _db_path_arg = sys.argv[i + 1]
            i += 2
        else:
            _argv.append(sys.argv[i])
            i += 1
    if _db_path_arg:
        DB_PATH = os.path.abspath(_db_path_arg)
    sys.argv = [sys.argv[0]] + _argv

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == 'scan':
        if len(sys.argv) < 3:
            print("Укажите путь к папке с архивами:")
            sys.exit(1)
        init_db()
        update_scan_progress(path=sys.argv[2], status='running')
        scan_archives(sys.argv[2])
        update_scan_progress(status='completed')

    elif cmd == 'scan-parallel':
        if len(sys.argv) < 3:
            print("Укажите путь к папке с архивами:")
            sys.exit(1)
        init_db()
        update_scan_progress(path=sys.argv[2], status='running')
        scan_archives_parallel(sys.argv[2])
        update_scan_progress(status='completed')

    elif cmd == 'index':
        init_db()
        archives_path = sys.argv[2] if len(sys.argv) > 2 else None
        build_fts_index(archives_path)

    elif cmd == 'web':
        import sys as _sys
        _sys.stdout.reconfigure(encoding='utf-8')
        print("Initializing database...", flush=True)
        init_db()
        print("Database OK", flush=True)
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
        print(f"Starting server on http://localhost:{port}", flush=True)
        app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
        print("Server stopped.", flush=True)

    else:
        print(f"Неизвестная команда: {cmd}")
        print(__doc__)
