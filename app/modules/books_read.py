"""Книги: разбор форматов, поиск по корпусу, содержимое, страницы, вопрос к книге.

Перенесено из server_backup.py без изменения тел. BOOKS_DIR читается через _sb: его
переписывает apply_profile_globals при смене профиля. BOOK_EXTENSIONS,
BOOKS_AVAILABLE и _book_cache - словари монолита, обращаемся к ним по месту, чтобы
кэш разобранной книги оставался общим (в _sb._book_cache пишут те же ключи, что и
непересаженный код).
docx / etree / epub / ebooklib / BeautifulSoup / extract_best_passages берутся из _sb,
а не импортируются заново: монолит связывает их в try/except, и при отсутствии
библиотеки в нативном модуле был бы NameError там, где оригинал спокойно видит None.
_build_book_bm25_index (писатель индекса) перенесён сюда же и вызывается напрямую:
кэш _book_bm25_cache читается через _sb, чтобы совпадать с непересаженным кодом.
"""

import json
import math
import os
import re
import shutil
import tempfile
import zipfile

import server_backup as _sb
import swallow

try:
    from modules import books_meta as _bm
except Exception:  # pragma: no cover - запуск из другой директории
    try:
        import books_meta as _bm
    except Exception:
        swallow.note('books_read.<module>:L27')
        _bm = None

# Поля метаданных, которые читалка отдаёт наружу (модалка/панель книги).
_META_PASS_FIELDS = (
    'pub_date', 'pub_year', 'series', 'seq_number', 'publisher', 'city',
    'isbn', 'translator', 'keywords', 'doc_id', 'version', 'program_used',
    'doc_date', 'file_size', 'rating',
)


def _read_txt(fp):
    """Чтение TXT файла с автоопределением кодировки."""
    for enc in ['utf-8', 'cp1251', 'cp866', 'koi8-r', 'iso-8859-5', 'latin-1']:
        try:
            with open(fp, 'r', encoding=enc) as f:
                text = f.read()
                # Проверяем что текст читаемый (не мусор)
                if enc == 'utf-8' or (text and sum(1 for c in text if c.isalpha()) / max(1, len(text)) > 0.1):
                    return text
        except (UnicodeDecodeError, Exception):
            swallow.note('books_read._read_txt:L35')
            continue
    # Последняя попытка: читаем как бинарный и заменяем ошибки
    try:
        with open(fp, 'rb') as f:
            return f.read().decode('utf-8', errors='replace')
    except Exception:
        swallow.note('books_read._read_txt:L42')
        return ""

def _read_epub(fp):
    """Чтение EPUB файла."""
    if 'epub' not in _sb.BOOKS_AVAILABLE:
        return {"error": "ebooklib не установлен"}
    try:
        book = _sb.epub.read_epub(fp)
        chapters = []
        for item in book.get_items():
            if item.get_type() == _sb.ebooklib.ITEM_DOCUMENT:
                content = item.get_content().decode('utf-8', errors='replace')
                if 'bs4' in _sb.BOOKS_AVAILABLE:
                    soup = _sb.BeautifulSoup(content, 'html.parser')
                    text = soup.get_text(separator='\n', strip=True)
                else:
                    text = re.sub(r'<[^>]+>', ' ', content)
                    text = re.sub(r'\s+', ' ', text).strip()
                if text.strip():
                    title = item.get_name() or ''
                    chapters.append({"title": title, "text": text})
        title = book.get_metadata('DC', 'title')
        author = book.get_metadata('DC', 'creator')
        return {
            "title": title[0][0] if title else os.path.basename(fp),
            "author": author[0][0] if author else "",
            "chapters": chapters,
            "format": "epub"
        }
    except Exception as e:
        swallow.note('books_read._read_epub:L73', e)
        return {"error": str(e)}

def _read_fb2(fp):
    """Чтение FB2 файла с полной поддержкой всех элементов."""
    if 'lxml' not in _sb.BOOKS_AVAILABLE:
        return {"error": "lxml не установлен. Установите: pip install lxml"}
    try:
        tree = _sb.etree.parse(fp)
        ns = {'fb': 'http://www.gribuser.ru/xml/fictionbook/2.0'}

        # === Метаданные (title-info) ===
        title_el = tree.find('.//fb:title-info/fb:book-title', ns)
        title = title_el.text.strip() if title_el is not None and title_el.text else os.path.basename(fp)

        # Авторы
        authors = []
        for author_el in tree.findall('.//fb:title-info/fb:author', ns):
            parts = []
            for tag in ['fb:first-name', 'fb:middle-name', 'fb:last-name']:
                el = author_el.find(tag, ns)
                if el is not None and el.text:
                    parts.append(el.text.strip())
            if parts:
                authors.append(' '.join(parts))
        author = ', '.join(authors) if authors else ''

        # Аннотация
        annotation = ''
        ann_el = tree.find('.//fb:title-info/fb:annotation', ns)
        if ann_el is not None:
            annotation = _fb_extract_text_from_element(ann_el, ns)

        # Жанр, дата, язык, идентификаторы
        genre_els = tree.findall('.//fb:title-info/fb:genre', ns)
        genres = [el.text.strip() for el in genre_els if el.text]

        date_el = tree.find('.//fb:title-info/fb:date', ns)
        pub_date = date_el.text.strip() if date_el is not None and date_el.text else ''

        lang_el = tree.find('.//fb:title-info/fb:lang', ns)
        lang = lang_el.text.strip() if lang_el is not None and lang_el.text else 'ru'

        # Переводчики
        translators = []
        for tr_el in tree.findall('.//fb:title-info/fb:translator', ns):
            tr_parts = []
            for tag in ['fb:first-name', 'fb:middle-name', 'fb:last-name']:
                el = tr_el.find(tag, ns)
                if el is not None and el.text:
                    tr_parts.append(el.text.strip())
            if tr_parts:
                translators.append(' '.join(tr_parts))
        translator = ', '.join(translators) if translators else ''

        # Ключевые слова
        kw_el = tree.find('.//fb:title-info/fb:keywords', ns)
        keywords = kw_el.text.strip() if kw_el is not None and kw_el.text else ''

        # Серия
        seq_el = tree.find('.//fb:title-info/fb:sequence', ns)
        series_name = ''
        seq_number = ''
        if seq_el is not None:
            series_name = seq_el.get('name', '').strip()
            seq_number = seq_el.get('number', '').strip()

        # Publish-info
        publisher = ''
        pub_city = ''
        pub_year = ''
        isbn = ''
        pi_el = tree.find('.//fb:publish-info', ns)
        if pi_el is not None:
            for el in pi_el.findall('fb:publisher', ns):
                if el.text: publisher = el.text.strip()
            for el in pi_el.findall('fb:city', ns):
                if el.text: pub_city = el.text.strip()
            for el in pi_el.findall('fb:year', ns):
                if el.text: pub_year = el.text.strip()
            for el in pi_el.findall('fb:isbn', ns):
                if el.text: isbn = el.text.strip()

        # Document-info
        doc_id = ''
        version = ''
        program_used = ''
        doc_date = ''
        di_el = tree.find('.//fb:document-info', ns)
        if di_el is not None:
            for el in di_el.findall('fb:id', ns):
                if el.text: doc_id = el.text.strip()
            for el in di_el.findall('fb:version', ns):
                if el.text: version = el.text.strip()
            for el in di_el.findall('fb:program-used', ns):
                if el.text: program_used = el.text.strip()
            for el in di_el.findall('fb:date', ns):
                if el.text: doc_date = el.text.strip()

        # === Содержание (все body) ===
        chapters = []
        bodies = tree.findall('.//fb:body', ns)

        for body_idx, body in enumerate(bodies):
            # Название body (если есть)
            body_title_el = body.find('fb:title', ns)
            body_title = ''
            if body_title_el is not None:
                body_title = _fb_extract_text_from_element(body_title_el, ns)

            # Рекурсивно обрабатываем секции
            sections = body.findall('fb:section', ns)
            if sections:
                for section in sections:
                    _fb_process_section(section, ns, chapters, body_idx, body_title)
            else:
                # Если секций нет — извлекаем весь текст body
                body_text = _fb_extract_body_text(body, ns)
                if body_text.strip():
                    chapters.append({
                        "title": body_title or f"Часть {body_idx + 1}",
                        "text": body_text
                    })

        # Если main body пустой, пробуем notes body как fallback
        if not chapters:
            for body in bodies:
                body_text = _fb_extract_body_text(body, ns)
                if body_text.strip():
                    chapters.append({"title": "Текст", "text": body_text})

        # Извлечение встроенных изображений
        images_meta = {}
        for binary_el in tree.findall('.//fb:binary', ns):
            img_id = binary_el.get('id', '')
            img_type = binary_el.get('content-type', '')
            if img_id:
                images_meta[img_id] = {'content_type': img_type}

        try: file_size = os.path.getsize(fp)
        except Exception:
            swallow.note('books_read._read_fb2:L214')
            file_size = 0

        return {
            "title": title,
            "author": author,
            "annotation": annotation,
            "genres": genres,
            "pub_date": pub_date,
            "language": lang,
            "translator": translator,
            "keywords": keywords,
            "series": series_name,
            "seq_number": seq_number,
            "publisher": publisher,
            "city": pub_city,
            "pub_year": pub_year,
            "isbn": isbn,
            "doc_id": doc_id,
            "version": version,
            "program_used": program_used,
            "doc_date": doc_date,
            "file_size": file_size,
            "chapters": chapters,
            "images_meta": images_meta,
            "format": "fb2"
        }
    except Exception as e:
        swallow.note('books_read._read_fb2:L242', e)
        return {"error": str(e)}

def _fb_extract_text_from_element(el, ns):
    """Рекурсивное извлечение текста из FB2 элемента с сохранением форматирования."""
    parts = []

    def _extract(node):
        if hasattr(node, 'text') and node.text:
            parts.append(node.text)
        for child in node:
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag == 'image':
                href = child.get('{http://www.w3.org/1999/xlink}href', child.get('href', ''))
                if href and href.startswith('#'):
                    parts.append('{%%img:%s%%}' % href[1:])
            if tag in ('strong', 'b', 'emphasis'):
                parts.append('**')
            _extract(child)
            if tag in ('strong', 'b', 'emphasis'):
                parts.append('**')
            if tag == 'strikethrough':
                parts.append('~~')
            if hasattr(child, 'tail') and child.tail:
                parts.append(child.tail)
        if hasattr(node, 'tail') and node.tail:
            parts.append(node.tail)

    _extract(el)
    text = ''.join(parts)
    # Очистка множественных пробелов и пустых строк
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def _fb_extract_body_text(body, ns):
    """Извлечение текста из body с сохранением структуры."""
    parts = []

    def _walk(node, depth=0):
        tag = node.tag.split('}')[-1] if '}' in node.tag else node.tag

        if tag == 'title':
            # Заголовок секции
            title_text = _fb_extract_text_from_element(node, ns)
            if title_text:
                prefix = '#' * min(depth + 1, 4)
                parts.append(f"\n{prefix} {title_text}\n")
            return

        if tag == 'subtitle':
            subtitle_text = _fb_extract_text_from_element(node, ns)
            if subtitle_text:
                prefix = '#' * min(depth + 2, 5)
                parts.append(f"\n{prefix} {subtitle_text}\n")
            return

        if tag == 'p':
            p_text = _fb_extract_text_from_element(node, ns)
            if p_text:
                parts.append(p_text + '\n')
            return

        if tag in ('poem', 'cite'):
            if tag == 'poem':
                parts.append('\n---\n')
            poem_text = _fb_extract_text_from_element(node, ns)
            if poem_text:
                parts.append(poem_text + '\n---\n')
            return

        if tag == 'empty-line':
            parts.append('\n')
            return

        if tag == 'image':
            href = node.get('{http://www.w3.org/1999/xlink}href', node.get('href', ''))
            if href and href.startswith('#'):
                parts.append('\n{%%img:%s%%}\n' % href[1:])
            return

        if tag == 'section':
            for child in node:
                _walk(child, depth + 1)
            return

        # Рекурсия для остальных элементов
        for child in node:
            _walk(child, depth)

    for child in body:
        _walk(child, 0)

    return '\n'.join(p for p in parts if p.strip()).strip()

def _fb_process_section(section, ns, chapters, body_idx, body_title, depth=0):
    """Рекурсивная обработка секций FB2."""
    # Заголовок секции
    title_el = section.find('fb:title', ns)
    section_title = ''
    if title_el is not None:
        section_title = _fb_extract_text_from_element(title_el, ns)

    # Подзаголовки
    subtitles = section.findall('fb:subtitle', ns)
    subtitle_text = '\n'.join(_fb_extract_text_from_element(s, ns) for s in subtitles if _fb_extract_text_from_element(s, ns))

    # Извлекаем текст секции (без вложенных section)
    section_text_parts = []

    for child in section:
        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag == 'section':
            continue  # вложенные секции обработаем отдельно
        if tag in ('title', 'subtitle'):
            continue  # уже обработали
        if tag == 'p':
            p_text = _fb_extract_text_from_element(child, ns)
            if p_text:
                section_text_parts.append(p_text)
        elif tag == 'poem':
            poem_text = _fb_extract_text_from_element(child, ns)
            if poem_text:
                section_text_parts.append(f"\n---\n{poem_text}\n---\n")
        elif tag == 'empty-line':
            section_text_parts.append('')
        elif tag == 'cite':
            cite_text = _fb_extract_text_from_element(child, ns)
            if cite_text:
                section_text_parts.append(f"\n> {cite_text}\n")
        elif tag == 'image':
            href = child.get('{http://www.w3.org/1999/xlink}href', child.get('href', ''))
            if href and href.startswith('#'):
                section_text_parts.append('{%%img:%s%%}' % href[1:])

    section_text = '\n'.join(p for p in section_text_parts if p.strip()).strip()

    # Формируем итоговый текст главы
    full_text = ''
    if section_title:
        prefix = '#' * min(depth + 1, 4)
        full_text += f"{prefix} {section_title}\n\n"
    if subtitle_text:
        full_text += f"{subtitle_text}\n\n"
    if section_text:
        full_text += section_text

    if full_text.strip():
        chapters.append({
            "title": section_title or f"Раздел {len(chapters) + 1}",
            "text": full_text.strip()
        })

    # Рекурсивно обрабатываем вложенные секции
    for child in section:
        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag == 'section':
            _fb_process_section(child, ns, chapters, body_idx, body_title, depth + 1)

def _how_to_install(pkgs: str) -> str:
    """Подсказка по установке внешней утилиты — для текущей ОС.

    Раньше все сообщения о недостающей утилите предлагали «sudo apt install ...».
    На Windows такой команды нет, и подсказка была просто неверной. Теперь на
    Windows показываем winget, на остальных системах — прежний apt-вариант.
    """
    pkgs = (pkgs or "").strip()
    if os.name == "nt":
        return (f" — winget install {pkgs} (либо установите утилиту вручную)"
                if pkgs else " — установите утилиту вручную или через winget")
    return f" (sudo apt install {pkgs})" if pkgs else ""

def _read_pdf(fp):
    """Чтение PDF файла с fallback на несколько библиотек."""
    errors = []

    # Попытка 1: pymupdf (fitz) — самый быстрый и качественный
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(fp)
        pages = []
        for i, page in enumerate(doc):
            text = page.get_text()
            if text.strip():
                pages.append({"title": f"Страница {i+1}", "text": text})
        doc.close()
        if pages:
            return {
                "title": os.path.basename(fp),
                "author": "",
                "chapters": pages,
                "format": "pdf"
            }
    except ImportError:
        errors.append("PyMuPDF не установлен (pip install pymupdf)")
    except Exception as e:
        errors.append(f"PyMuPDF: {e}")

    # Попытка 2: pdfplumber — хорошая точность извлечения
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(fp) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text()
                if text and text.strip():
                    pages.append({"title": f"Страница {i+1}", "text": text})
        if pages:
            return {
                "title": os.path.basename(fp),
                "author": "",
                "chapters": pages,
                "format": "pdf"
            }
    except ImportError:
        errors.append("pdfplumber не установлен (pip install pdfplumber)")
    except Exception as e:
        errors.append(f"pdfplumber: {e}")

    # Попытка 3: pypdf (современная замена PyPDF2)
    try:
        import pypdf
        pages = []
        with open(fp, 'rb') as f:
            reader = pypdf.PdfReader(f)
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append({"title": f"Страница {i+1}", "text": text})
        if pages:
            return {
                "title": os.path.basename(fp),
                "author": "",
                "chapters": pages,
                "format": "pdf"
            }
    except ImportError:
        errors.append("pypdf не установлен (pip install pypdf)")
    except Exception as e:
        errors.append(f"pypdf: {e}")

    # Попытка 4: PyPDF2 (старая библиотека)
    try:
        import PyPDF2
        pages = []
        with open(fp, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append({"title": f"Страница {i+1}", "text": text})
        if pages:
            _sb.BOOKS_AVAILABLE['pdf'] = True
            return {
                "title": os.path.basename(fp),
                "author": "",
                "chapters": pages,
                "format": "pdf"
            }
    except ImportError:
        errors.append("PyPDF2 не установлен (pip install PyPDF2)")
    except Exception as e:
        errors.append(f"PyPDF2: {e}")

    # Попытка 5: pdftotext subprocess (poppler-utils)
    if shutil.which('pdftotext'):
        try:
            import subprocess
            result = subprocess.run(
                ['pdftotext', '-layout', fp, '-'],
                capture_output=True, timeout=120
            )
            if result.returncode == 0 and result.stdout.strip():
                text = result.stdout.decode('utf-8', errors='replace')
                # Разбиваем на страницы по форм-фиду
                raw_pages = text.split('\f')
                pages = []
                for i, pg in enumerate(raw_pages):
                    pg = pg.strip()
                    if pg:
                        pages.append({"title": f"Страница {i+1}", "text": pg})
                if pages:
                    return {
                        "title": os.path.basename(fp),
                        "author": "",
                        "chapters": pages,
                        "format": "pdf"
                    }
        except Exception as e:
            errors.append(f"pdftotext: {e}")
    else:
        errors.append(f"pdftotext не найден{_how_to_install('poppler-utils')}")

    return {"error": f"Не удалось прочитать PDF. Попробуйте: pip install pymupdf pdfplumber pypdf. Ошибки: {'; '.join(errors)}"}

def _read_docx(fp):
    """Чтение DOCX файла."""
    if 'docx' not in _sb.BOOKS_AVAILABLE:
        return {"error": "python-docx не установлен"}
    try:
        doc = _sb.docx.Document(fp)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return {
            "title": os.path.basename(fp),
            "author": "",
            "chapters": [{"title": "Документ", "text": '\n'.join(paragraphs)}],
            "format": "docx"
        }
    except Exception as e:
        swallow.note('books_read._read_docx:L551', e)
        return {"error": str(e)}

def _read_djvu(fp):
    """Чтение DjVu файла с улучшенной кодировкой."""
    import subprocess
    errors = []

    # Попытка 1: djvutxt → iconv → UTF-8
    if shutil.which('djvutxt'):
        try:
            raw_bytes = subprocess.run(
                ['djvutxt', fp],
                capture_output=True, timeout=120
            ).stdout

            if raw_bytes and len(raw_bytes.strip()) > 20:
                full_text = None

                # Стратегия 1: пробуем прямое декодирование
                for enc in ['utf-8', 'cp1251', 'koi8-r', 'cp866', 'iso-8859-5']:
                    try:
                        decoded = raw_bytes.decode(enc, errors='strict')
                        if len(decoded.strip()) > 50:
                            alpha = sum(1 for c in decoded if c.isalpha() or c.isspace() or c in '.,;:!?-()—–«»"')
                            if alpha / max(1, len(decoded)) > 0.4:
                                full_text = decoded.strip()
                                break
                    except (UnicodeDecodeError, Exception):
                        swallow.note('books_read._read_djvu:L580')
                        continue

                # Стратегия 2: пробуем через iconv
                if full_text is None:
                    for from_enc in ['cp1251', 'koi8-r', 'cp866', 'iso-8859-5', 'latin-1']:
                        try:
                            result = subprocess.run(
                                ['iconv', '-f', from_enc, '-t', 'utf-8//IGNORE'],
                                input=raw_bytes, capture_output=True, timeout=30
                            )
                            if result.returncode == 0 and result.stdout:
                                decoded = result.stdout.decode('utf-8', errors='replace').strip()
                                if len(decoded) > 50:
                                    alpha = sum(1 for c in decoded if c.isalpha() or c.isspace())
                                    if alpha / max(1, len(decoded)) > 0.4:
                                        full_text = decoded
                                        break
                        except Exception:
                            swallow.note('books_read._read_djvu:L599')
                            continue

                # Стратегия 3: двойное декодирование (utf-8→latin1→cp1251)
                if full_text is None:
                    try:
                        tmp = raw_bytes.decode('utf-8', errors='replace')
                        full_text = tmp.encode('latin-1', errors='replace').decode('cp1251', errors='replace')
                    except Exception:
                        swallow.note('books_read._read_djvu:L608')
                        pass

                # Стратегия 4: fallback — utf-8 с заменой
                if full_text is None:
                    full_text = raw_bytes.decode('utf-8', errors='replace').strip()

                # Разбиваем на страницы
                raw_pages = full_text.split('\x0c')
                pages = []
                for i, pg in enumerate(raw_pages):
                    pg = pg.strip()
                    if pg and len(pg) > 10:
                        pages.append({"title": f"Страница {i+1}", "text": pg})
                if pages:
                    return {
                        "title": os.path.basename(fp),
                        "author": "",
                        "chapters": pages,
                        "format": "djvu"
                    }
                # Если страниц нет — весь текст одной главой
                if full_text and len(full_text) > 50:
                    return {
                        "title": os.path.basename(fp),
                        "author": "",
                        "chapters": [{"title": "Текст", "text": full_text}],
                        "format": "djvu"
                    }
            else:
                errors.append("djvutxt: пустой или слишком короткий вывод")
        except subprocess.TimeoutExpired:
            errors.append("djvutxt: таймаут (>120с)")
        except Exception as e:
            errors.append(f"djvutxt: {e}")
    else:
        errors.append(f"djvutxt не найден. Установите:{_how_to_install('djvulibre-bin')}")

    # Попытка 2: ddjvu → tesseract OCR
    if shutil.which('ddjvu') and shutil.which('tesseract'):
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                tmp_path = tmp.name
            subprocess.run(
                ['ddjvu', '-format=png', '-page=1', '-resolution=300', fp, tmp_path],
                capture_output=True, timeout=120
            )
            if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1000:
                result = subprocess.run(
                    ['tesseract', tmp_path, 'stdout', '-l', 'rus+eng'],
                    capture_output=True, timeout=120
                )
                if result.returncode == 0 and result.stdout.strip():
                    text = result.stdout.decode('utf-8', errors='replace').strip()
                    os.unlink(tmp_path)
                    if len(text) > 50:
                        return {
                            "title": os.path.basename(fp),
                            "author": "",
                            "chapters": [{"title": "Распознанный текст (OCR)", "text": text}],
                            "format": "djvu"
                        }
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception as e:
            errors.append(f"ddjvu+tesseract: {e}")
    elif shutil.which('ddjvu'):
        errors.append(f"tesseract не найден{_how_to_install('tesseract-ocr tesseract-ocr-rus')}")

    return {"error": f"Не удалось прочитать DjVu. Ошибки: {'; '.join(errors)}"}

def _read_rtf(fp):
    """Чтение RTF файла через striprtf или простой парсинг."""
    try:
        import re
        for enc in ['utf-8', 'cp1251', 'latin-1']:
            try:
                with open(fp, 'r', encoding=enc) as f:
                    raw = f.read()
                # Простое удаление RTF тегов
                text = re.sub(r'\\[a-z]+\d*\s?', ' ', raw)
                text = re.sub(r'[{}]', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                if text and len(text) > 10:
                    return {"title": os.path.basename(fp), "author": "", "chapters": [{"title": "Документ", "text": text}], "format": "rtf"}
            except UnicodeDecodeError:
                swallow.note('books_read._read_rtf:L695')
                continue
        return {"error": "Не удалось прочитать RTF файл"}
    except Exception as e:
        swallow.note('books_read._read_rtf:L699', e)
        return {"error": f"RTF: {e}"}

def _read_doc(fp):
    """Чтение DOC файла через antiword, catdoc или strings с правильной кодировкой."""
    import subprocess
    errors = []
    # Попытка 1: antiword с UTF-8
    if shutil.which('antiword'):
        try:
            result = subprocess.run(
                ['antiword', '-m', 'UTF-8.txt', fp],
                capture_output=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                text = result.stdout.decode('utf-8', errors='replace')
                if text.strip():
                    return {"title": os.path.basename(fp), "author": "", "chapters": [{"title": "Документ", "text": text.strip()}], "format": "doc"}
        except Exception as e:
            errors.append(f"antiword: {e}")
    # Попытка 2: catdoc
    if shutil.which('catdoc'):
        try:
            result = subprocess.run(
                ['catdoc', '-d', 'utf-8', fp],
                capture_output=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                text = result.stdout.decode('utf-8', errors='replace')
                if text.strip():
                    return {"title": os.path.basename(fp), "author": "", "chapters": [{"title": "Документ", "text": text.strip()}], "format": "doc"}
        except Exception as e:
            errors.append(f"catdoc: {e}")
    # Попытка 3: strings с правильной кодировкой CP1251
    try:
        result = subprocess.run(
            ['strings', '-n', '4', fp],
            capture_output=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            for enc in ['cp1251', 'utf-8', 'latin-1']:
                try:
                    text = result.stdout.decode(enc, errors='replace')
                    lines = []
                    for line in text.split('\n'):
                        line = line.strip()
                        if len(line) >= 4 and any(c.isalpha() for c in line):
                            alpha_ratio = sum(1 for c in line if c.isalpha() or c.isspace()) / len(line)
                            if alpha_ratio > 0.5:
                                lines.append(line)
                    if lines and len('\n'.join(lines)) > 50:
                        return {"title": os.path.basename(fp), "author": "", "chapters": [{"title": "Документ", "text": '\n'.join(lines)}], "format": "doc"}
                except Exception:
                    swallow.note('books_read._read_doc:L752')
                    continue
    except Exception as e:
        errors.append(f"strings: {e}")
    # Попытка 4: прямое чтение бинарного файла
    try:
        with open(fp, 'rb') as f:
            raw = f.read()
        for enc in ['cp1251', 'utf-8', 'latin-1']:
            try:
                text = raw.decode(enc, errors='replace')
                lines = []
                for line in text.split('\n'):
                    line = line.strip()
                    if len(line) >= 4:
                        alpha_ratio = sum(1 for c in line if c.isalpha() or c.isspace() or c in '.,;:!?-()') / len(line)
                        if alpha_ratio > 0.6:
                            lines.append(line)
                if lines and len('\n'.join(lines)) > 50:
                    return {"title": os.path.basename(fp), "author": "", "chapters": [{"title": "Документ", "text": '\n'.join(lines)}], "format": "doc"}
            except Exception:
                swallow.note('books_read._read_doc:L773')
                continue
    except Exception as e:
        errors.append(f"binary: {e}")
    return {"error": f"Не удалось прочитать DOC. Установите:{_how_to_install('antiword catdoc')}. Ошибки: {'; '.join(errors)}"}

def _calibry_read(fp, ext):
    """Чтение книги через Calibre (папка calibry/): конвертация в EPUB и чтение."""
    try:
        from server import calibry_backend as _cb
    except Exception:
        swallow.note('books_read._calibry_read:L784')
        try:
            import calibry_backend as _cb
        except Exception as _e:
            swallow.note('books_read._calibry_read:L788', _e)
            return {"error": f"Calibre не доступен ({_e})"}
    try:
        if not _cb.calibre_available():
            return {"error": f"Формат {ext} не поддерживается встроенной читалкой. "
                             "Поместите портабельный Calibre в папку calibry/ — книга откроется автоматически."}
        data = _cb.read_via_calibry(fp, ext)
        if isinstance(data, dict) and data.get('chapters'):
            return data
        return {"error": data.get('error', 'Ошибка конвертации через Calibre')}
    except Exception as e:
        swallow.note('books_read._calibry_read:L799', e)
        return {"error": f"Ошибка Calibre: {e}"}

def _read_book_file(fp, ext):
    """Чтение книги по расширению.

    Сжатая книга (.zst) распаковывается во временный файл: читалки разных
    форматов (ebooklib, PyPDF2, lxml, Calibre) требуют настоящий путь.
    """
    import compression_module as _cz
    ext = ext.lower()
    with _cz.temp_file(fp) as real:
        if ext == '.txt':
            text = _read_txt(real)
            return {"title": os.path.basename(fp), "author": "", "chapters": [{"title": "Текст", "text": text}], "format": "txt"}
        elif ext == '.epub':
            return _read_epub(real)
        elif ext == '.fb2':
            return _read_fb2(real)
        elif ext == '.pdf':
            return _read_pdf(real)
        elif ext == '.docx':
            return _read_docx(real)
        elif ext == '.doc':
            return _read_doc(real)
        elif ext == '.rtf':
            return _read_rtf(real)
        elif ext == '.djvu':
            data = _read_djvu(real)
            if 'error' in data:
                return _calibry_read(real, ext)
            return data
        return _calibry_read(real, ext)

def _read_zip_book(fp):
    """Чтение книги из ZIP архива."""
    try:
        with zipfile.ZipFile(fp, 'r') as zf:
            for name in zf.namelist():
                ext = os.path.splitext(name)[1].lower()
                if ext in _sb.BOOK_EXTENSIONS and ext != '.zip':
                    with zf.open(name) as inner:
                        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                        tmp.write(inner.read())
                        tmp.close()
                        result = _read_book_file(tmp.name, ext)
                        result['archive_name'] = name
                        os.unlink(tmp.name)
                        return result
            return {"error": "В архиве нет поддерживаемых файлов книг"}
    except Exception as e:
        swallow.note('books_read._read_zip_book:L850', e)
        return {"error": str(e)}

def _books_dir_with_content(candidates):
    """Первый каталог из кандидатов, где реально есть файлы книг.

    Зачем: значение books_dir приходит из нескольких источников (конфиг,
    BOOKS_DIR, профиль), и они могут расходиться — профильный конфиг, например,
    перетирает путь значением по умолчанию ${ROOT}/data/books. Раньше из-за
    этого библиотека книг показывалась пустой, хотя файлы лежали на диске.
    """
    import compression_module as _cz
    exts = tuple(_sb.BOOK_EXTENSIONS.keys())
    for d in candidates:
        if not d:
            continue
        d = os.path.abspath(os.path.expanduser(str(d)))
        if not os.path.isdir(d):
            continue
        try:
            for _root, _dirs, files in os.walk(d):
                for f in files:
                    logical = f[:-4] if f.lower().endswith(_cz.ZST_SUFFIX) else f
                    if os.path.splitext(logical)[1].lower() in exts:
                        return d
        except Exception:
            swallow.note('books_read._books_dir_with_content:L876')
            continue
    # Ничего не нашли — вернём первый существующий, чтобы поведение было
    # предсказуемым, а не зависело от порядка источников.
    for d in candidates:
        if d and os.path.isdir(os.path.abspath(os.path.expanduser(str(d)))):
            return os.path.abspath(os.path.expanduser(str(d)))
    return None

def find_books():
    """Найти все книги во всех каталогах библиотек (рекурсивно по подпапкам).

    Обход берёт BOOKS_DIRS — общедоступный каталог и свой у профиля. Один
    books_dir вмещает только путь, перекрывающий общедоступный, поэтому при
    своих книгах профиля общедоступная библиотека исчезала из списка и поиска.
    """
    books = []
    candidates = list(getattr(_sb, "BOOKS_DIRS", None) or []) or [_sb.BOOKS_DIR]
    # Пути книг, сохранённые в папках, — надёжный источник: они указывают
    # на каталог, где библиотека действительно лежит.
    try:
        data = _sb._load_book_folders() or {}
        for _paths in (data.get("books_in_folders") or {}).values():
            for p in (_paths or [])[:5]:
                parent = os.path.dirname(str(p))
                if parent:
                    candidates.insert(0, parent)
    except Exception:
        swallow.note('books_read.find_books:L904')
        pass
    for getter in ("load_config", "get_config"):
        try:
            import config_module as _cm
            cfg = getattr(_cm, getter)() or {}
            cfg_dir = cfg.get("books_dir")
            if cfg_dir:
                candidates.insert(0, cfg_dir)
            for cfg_d in (cfg.get("books_dirs") or []):
                if cfg_d:
                    candidates.append(cfg_d)
        except Exception:
            swallow.note('books_read.find_books:L917')
            pass
    preferred = _books_dir_with_content(candidates)
    if preferred:
        candidates.insert(0, preferred)
    import compression_module as _cz
    seen_roots = set()
    seen_files = set()
    for cand in candidates:
        if not cand:
            continue
        books_dir = os.path.abspath(os.path.expanduser(str(cand)))
        if books_dir in seen_roots or not os.path.isdir(books_dir):
            continue
        seen_roots.add(books_dir)
        for root, dirs, files in os.walk(books_dir):
            dirs.sort()
            present = {f for f in files if not f.lower().endswith(_cz.ZST_SUFFIX)}
            for f in sorted(files):
                if f.lower().endswith(_cz.ZST_SUFFIX):
                    if not _cz.is_zst_file(os.path.join(root, f)):
                        continue
                    f = f[:-4]
                    if f in present:
                        continue
                ext = os.path.splitext(f)[1].lower()
                if ext in _sb.BOOK_EXTENSIONS:
                    fp = os.path.join(root, f)
                    real = os.path.realpath(fp)
                    if real in seen_files:
                        continue
                    seen_files.add(real)
                    rel = os.path.relpath(fp, books_dir)
                    folder = os.path.dirname(rel) if os.path.dirname(rel) else ""
                    size = _cz.size(fp)
                    books.append({
                        "name": f,
                        "path": fp,
                        "rel_path": rel,
                        "folder": folder,
                        "size": size,
                        "format": ext[1:],
                        "type": "book"
                    })
    if not seen_roots:
        _sb.logger.warning(f"find_books: не найден каталог с книгами ({candidates})")
    if _bm is not None:
        try:
            _bm.enrich_books(books)
        except Exception as e:
            swallow.note('books_read.find_books.enrich:L975', e)
    _sb.logger.info(f"find_books: {sorted(seen_roots)} -> найдено {len(books)}")
    return books

def _media_book_content(fp, kind):
    """Содержимое аудиокниги или комикса/манги (без текстового разбора)."""
    payload = _bm.book_payload(fp)
    if kind == 'audiobook':
        chapters = (payload.get('audio') or {}).get('chapters') or []
        ch_out = [{
            'title': c.get('title') or ('Глава %d' % (i + 1)),
            'text': '', 'text_preview': '', 'start': float(c.get('start') or 0),
        } for i, c in enumerate(chapters)]
        if not ch_out:
            ch_out = [{'title': 'Аудио', 'text': '', 'text_preview': '', 'start': 0}]
        payload['chapters'] = ch_out
        payload['total_chapters'] = len(ch_out)
    else:
        try:
            pages = _bm.list_comic_pages(fp)
        except Exception as e:
            swallow.note('books_read._media_book_content:L1010', e)
            return {'error': f'Не удалось открыть архив с комиксом: {e}'}
        payload['chapters'] = [{
            'title': f'Страница {i + 1}', 'text': '', 'text_preview': '', 'page': i,
        } for i in range(len(pages))]
        payload['comic'] = {'pages': len(pages)}
        payload['total_chapters'] = len(pages) or 1
    payload.setdefault('full_text', '')
    payload.setdefault('images_meta', {})
    payload.setdefault('format', os.path.splitext(fp)[1].lstrip('.').lower())
    return payload


def _pass_meta_fields(data, out):
    """Перенести «прочие» поля метаданных в ответ читалки."""
    for key in _META_PASS_FIELDS:
        if key in data and data.get(key) not in (None, '', []):
            out[key] = data[key]
    return out


def get_book_content(fp, chapter_idx=None):
    """Получить содержимое книги."""
    if fp.startswith('collection://'):
        book_id = fp.replace('collection://', '', 1)
        try:
            book_id = int(book_id)
        except ValueError:
            swallow.note('books_read.get_book_content:L973')
            return {"error": "invalid collection book id"}
        try:
            import tempfile, zipfile
            from modules.collection_module import get_book as _cget_book, find_archive
            from io import BytesIO
            book = _cget_book(book_id)
            if not book:
                return {"error": "collection book not found"}
            archive = book.get('archive_filename')
            if not archive:
                return {"error": "archive not found"}
            archive_path = find_archive(archive, _sb.BOOKS_DIR)
            if not archive_path:
                return {"error": "archive file not found"}
            file_path = book['file_path']
            with zipfile.ZipFile(archive_path, 'r') as zf:
                try:
                    xml_bytes = zf.read(file_path)
                except KeyError:
                    swallow.note('books_read.get_book_content:L993')
                    for name in zf.namelist():
                        if name.lower().endswith(('.fb2', '.fb2.zip')):
                            xml_bytes = zf.read(name)
                            file_path = name
                            break
                    else:
                        return {"error": "fb2 not found in archive"}
                if file_path.lower().endswith('.fb2.zip'):
                    try:
                        with zipfile.ZipFile(BytesIO(xml_bytes)) as inner:
                            inner_names = inner.namelist()
                            if inner_names:
                                xml_bytes = inner.read(inner_names[0])
                    except Exception:
                        swallow.note('books_read.get_book_content:L1008')
                        pass
            with tempfile.NamedTemporaryFile(suffix='.fb2', delete=False) as tmp:
                tmp.write(xml_bytes)
                tmp_path = tmp.name
            try:
                data = _read_fb2(tmp_path)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    swallow.note('books_read.get_book_content:L1019')
                    pass
            if 'error' in data:
                return data
        except Exception as e:
            swallow.note('books_read.get_book_content:L1024', e)
            return {"error": str(e)}
    else:
        import compression_module as _cz
        ext = os.path.splitext(fp)[1].lower()
        if _bm is not None:
            kind = _bm.book_kind(fp)
            if kind in ('audiobook', 'comic', 'manga'):
                return _media_book_content(fp, kind)
        cache_key = f"{fp}:{_cz.mtime(fp)}"
        if cache_key in _sb._book_cache:
            data = _sb._book_cache[cache_key]
        else:
            if ext == '.zip':
                data = _read_zip_book(fp)
            else:
                data = _read_book_file(fp, ext)
            if 'error' not in data:
                _sb._book_cache[cache_key] = data
    if 'error' in data:
        return data
    # Ручные правки (sidecar) перекрывают значения, вычитанные из файла.
    if _bm is not None:
        try:
            data = _bm.apply_meta(fp, data)
        except Exception as e:
            swallow.note('books_read.get_book_content.meta:L1108', e)
    images_meta = data.get('images_meta', {}) or {}
    if chapter_idx is not None and 0 <= chapter_idx < len(data.get('chapters', [])):
        ch = data['chapters'][chapter_idx]
        out = {
            "title": data.get("title", ""),
            "author": data.get("author", ""),
            "format": data.get("format", ""),
            "chapter": ch,
            "chapter_idx": chapter_idx,
            "total_chapters": len(data['chapters']),
            "annotation": data.get("annotation", ""),
            "language": data.get("language", ""),
            "genres": data.get("genres", []),
            "images_meta": images_meta,
            "chapters": [{"title": c.get("title", ""), "text": c.get("text", ""), "text_preview": c.get("text", "")[:200]} for c in data.get('chapters', [])]
        }
        if data.get('meta_edited'):
            out['meta_edited'] = True
        return _pass_meta_fields(data, out)
    chapters = data.get('chapters', [])
    all_text = '\n\n'.join(ch.get('text', '') for ch in chapters)
    out = {
        "title": data.get("title", ""),
        "author": data.get("author", ""),
        "format": data.get("format", ""),
        "full_text": all_text,
        "total_chapters": len(chapters),
        "annotation": data.get("annotation", ""),
        "language": data.get("language", ""),
        "genres": data.get("genres", []),
        "images_meta": images_meta,
        "chapters": [{"title": c.get("title", ""), "text": c.get("text", ""), "text_preview": c.get("text", "")[:200]} for c in chapters]
    }
    if data.get('meta_edited'):
        out['meta_edited'] = True
    return _pass_meta_fields(data, out)

def search_books(query):
    """Поиск по всем книгам: названию, автору и содержимому."""
    results = []
    ql = query.lower()
    books = find_books()
    for book in books[:30]:
        try:
            data = get_book_content(book['path'])
            if 'error' in data:
                continue
            title = data.get('title', book['name'])
            author = data.get('author', '')
            text = data.get('full_text', '')
            match_type = None
            preview = ""
            if ql in title.lower():
                match_type = "title"
                preview = f"Название: {title}"
            elif author and ql in author.lower():
                match_type = "author"
                preview = f"Автор: {author}"
            elif ql in text.lower():
                match_type = "content"
                idx = text.lower().find(ql)
                start = max(0, idx - 100)
                end = min(len(text), idx + 200)
                preview = text[start:end]
            if match_type:
                results.append({
                    "file": book.get('rel_path', book['name']),
                    "title": title,
                    "author": author,
                    "preview": preview,
                    "type": "book",
                    "match_type": match_type,
                    "book_path": book['path'],
                    "format": book['format']
                })
        except Exception:
            swallow.note('books_read.search_books:L1111')
            continue
    results.sort(key=lambda x: (0 if x['match_type'] == 'title' else 1 if x['match_type'] == 'author' else 2))
    return results

def read_page(fp, page, pp=10):
    start = (page - 1) * pp
    items, total = [], 0
    try:
        with open(fp, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < start or i >= start + pp:
                    total += 1; continue
                try:
                    it = json.loads(line)
                    it["_index"] = i + 1
                    items.append(it); total += 1
                except Exception:
                    swallow.note('books_read.read_page:L1129')
                    continue
    except Exception:
        swallow.note('books_read.read_page:L1132')
        pass
    return {"items": items, "page": page, "per_page": pp,
            "total": total, "pages": max(1, math.ceil(total / pp))}

def get_item(fp, idx):
    try:
        with open(fp, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i == idx - 1:
                    it = json.loads(line)
                    it["_index"] = i + 1
                    return it
    except Exception:
        swallow.note('books_read.get_item:L1146')
        pass
    return None

def ask_book_question(book_path, question, history=None, mode="chapters", model=None, temperature=0.7, chapters_window=2, current_chapter=None, system_prompt=""):
    """Задать вопрос по книге с режимами: chapters, rag, smart_truncate, full_study, free."""
    book_data = get_book_content(book_path)
    if 'error' in book_data:
        return {"answer": f"Ошибка загрузки книги: {book_data.get('error', 'неизвестная ошибка')}", "reasoning": ""}
    chapters = book_data.get('chapters', [])
    full_text = book_data.get('full_text', '')
    book_title = book_data.get('title', '') or os.path.splitext(os.path.basename(book_path))[0]
    context = ""
    sources_info = ""
    if mode == "free":
        context = ""
        sources_info = "Свободный режим (без контекста)"
    elif mode == "chapters":
        if current_chapter is not None and chapters:
            start_idx = max(0, current_chapter - chapters_window)
            end_idx = min(len(chapters), current_chapter + chapters_window + 1)
            selected = chapters[start_idx:end_idx]
            context_parts = []
            for i, ch in enumerate(selected):
                ri = start_idx + i
                title = ch.get('title', f'Глава {ri+1}')
                text = ch.get('text', '')
                context_parts.append(f"[{title}]\n{text}")
            context = '\n\n'.join(context_parts)
            sources_info = f"Главы {start_idx+1}-{end_idx}"
        else:
            n = max(1, chapters_window * 2 + 1)
            selected = chapters[:n]
            context_parts = []
            for i, ch in enumerate(selected):
                title = ch.get('title', f'Глава {i+1}')
                text = ch.get('text', '')
                context_parts.append(f"[{title}]\n{text}")
            context = '\n\n'.join(context_parts)
            sources_info = f"Первые {len(selected)} глав"
    elif mode == "rag":
        if chapters:
            bm25, texts, meta = _build_book_bm25_index(book_path, chapters)
            if bm25 and texts:
                tokenized_q = re.findall(r'\w+', question.lower())
                scores = bm25.get_scores(tokenized_q)
                top_count = min(15, len(scores))
                top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_count]
                context_parts = []
                total_chars = 0
                max_context_chars = 100000
                for idx in top_indices:
                    if scores[idx] > 0:
                        title = meta[idx]['title']
                        text = texts[idx]
                        if total_chars + len(text) > max_context_chars:
                            remaining = max_context_chars - total_chars
                            if remaining > 200:
                                context_parts.append(f"[{title}] (релевантность: {scores[idx]:.2f})\n{text[:remaining]}")
                            break
                        context_parts.append(f"[{title}] (релевантность: {scores[idx]:.2f})\n{text}")
                        total_chars += len(text)
                if context_parts:
                    context = '\n\n'.join(context_parts)
                    sources_info = f"BM25: топ-{len(context_parts)} из {len(chapters)} глав (до {max_context_chars} симв.)"
                else:
                    selected = chapters[:3]
                    context_parts = [f"[{ch.get('title', f'Глава {i+1}')}]\n{ch.get('text', '')[:800]}" for i, ch in enumerate(selected)]
                    context = '\n\n'.join(context_parts)
                    sources_info = "Первые 3 главы (BM25 не нашёл релевантных)"
            else:
                context = full_text[:100000]
                sources_info = "Первые 100000 символов (BM25 недоступен)"
        else:
            context = full_text[:100000]
            sources_info = "Первые 100000 символов"
    elif mode == "smart_truncate":
        if _sb.extract_best_passages and full_text:
            context = _sb.extract_best_passages(full_text, question, max_passages=10, max_chars_per_passage=2000)
            if not context:
                context = full_text[:60000]
                sources_info = "Первые 60000 символов"
            else:
                sources_info = "Умная обрезка: до 10 пассажей"
        else:
            context = full_text[:60000]
            sources_info = "Первые 60000 символов"
    elif mode == "full_study":
        # Этап 1: генерация подзапросов
        analysis_prompt = f"""Проанализируй вопрос по книге и разбей его на ключевые аспекты для исследования.

Книга: {book_title}
Вопрос: {question}

Выдели 3-8 подзапросов для поиска по тексту книги.
Верни нумерованный список, каждый подзапрос — одно предложение."""

        analysis_result = _sb.lm_studio_request([
            {"role": "system", "content": "Разбивай вопросы на подзапросы для поиска по книге."},
            {"role": "user", "content": analysis_prompt}
        ], max_tokens=1024, model=model, temperature=0.5)

        sub_queries = []
        for line in analysis_result.get("content", "").split("\n"):
            line = line.strip()
            if line and (line[0].isdigit() or line.startswith("-") or line.startswith("•")):
                query = line.lstrip("1234567890.-•) ").strip()
                if query and len(query) > 10:
                    sub_queries.append(query)
        if not sub_queries:
            sub_queries = [question]
        sub_queries = sub_queries[:8]

        # Этап 2: поиск по каждому подзапросу
        bm25, texts, meta = _build_book_bm25_index(book_path, chapters) if chapters else (None, [], [])
        research_data = []
        for sq in sub_queries:
            findings = ""
            if bm25 and texts:
                tokenized_q = re.findall(r'\w+', sq.lower())
                scores = bm25.get_scores(tokenized_q)
                top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:3]
                for idx in top_idx:
                    if scores[idx] > 0:
                        title = meta[idx]['title']
                        findings += f"[{title}]\n{texts[idx][:500]}\n\n"
            if not findings and full_text:
                findings = full_text[:2000]
            synthesis = _sb.lm_studio_request([
                {"role": "system", "content": "Кратко суммируй информацию по запросу из текста книги."},
                {"role": "user", "content": f"Запрос: {sq}\n\nТекст книги:\n{findings}\n\nРезюме:"}
            ], max_tokens=512, model=model, temperature=0.3)
            research_data.append({"query": sq, "findings": synthesis.get("content", "")})

        # Этап 3: финальный отчёт
        final_context = f"# Исследование по книге: {book_title}\n\n## Вопрос: {question}\n\n"
        for rd in research_data:
            final_context += f"## Подзапрос: {rd['query']}\n{rd['findings']}\n\n---\n\n"
        final_context += "\nНа основе собранных данных составь подробный ответ на исходный вопрос."

        result = _sb.lm_studio_request([
            {"role": "system", "content": "Ты аналитик. Составляй подробные ответы на основе исследования книги."},
            {"role": "user", "content": final_context}
        ], max_tokens=8192, model=model, temperature=temperature)

        answer = result.get("content", "")
        reasoning = result.get("reasoning", "")
        _sb.ai_save_answer(question, answer, f"full_study по книге {book_title}")
        return {"answer": answer, "reasoning": reasoning, "research_steps": research_data}

    # Стандартный поток для chapters / rag / smart_truncate / free
    prev_answers = _sb.ai_search_answers(question)
    prev_context = ""
    if prev_answers:
        prev_context = "\n\nПредыдущие ответы:\n"
        for a in prev_answers[:2]:
            prev_context += f"- В: {a['question'][:80]}\n  О: {a['answer'][:150]}...\n"
    if mode == "free":
        messages = []
        if history:
            for msg in history[-15:]:
                messages.append({"role": msg["role"], "content": msg["content"][:500]})
        messages.append({"role": "user", "content": question})
    else:
        sp = system_prompt or "Ты Infinity AI. Анализируй текст книги и отвечай на русском языке. Используй только информацию из предоставленного контекста."
        messages = [{"role": "system", "content": sp}]
        if history:
            for msg in history[-10:]:
                messages.append({"role": msg["role"], "content": msg["content"][:500]})
        msg_content = f"Книга: {book_title}\n\n{sources_info}\n\n{context}{prev_context}\n\nВопрос: {question}"
        if not context:
            msg_content = f"Вопрос по книге {book_title}: {question}"
        messages.append({"role": "user", "content": msg_content})
    result = _sb.lm_studio_request(messages, max_tokens=4096, model=model, temperature=temperature)
    answer = result.get("content", "")
    reasoning = result.get("reasoning", "")
    _sb.ai_save_answer(question, answer, f"{mode} по книге {book_title}")
    return {"answer": answer, "reasoning": reasoning}

def get_nav(fp, idx):
    """Получает заголовки соседних статей для навигации."""
    prev_title = next_title = None
    try:
        with open(fp, encoding="utf-8") as f:
            lines = f.readlines()
            if idx > 1:
                try: prev_title = json.loads(lines[idx-2]).get("title","")[:60]
                except Exception:
                    swallow.note('books_read.get_nav:L1334')
                    pass
            if idx < len(lines):
                try: next_title = json.loads(lines[idx]).get("title","")[:60]
                except Exception:
                    swallow.note('books_read.get_nav:L1339')
                    pass
    except Exception:
        swallow.note('books_read.get_nav:L1342')
        pass
    return prev_title, next_title

def _build_book_bm25_index(book_path, chapters):
    """Создать in-memory BM25 индекс из глав книги."""
    mtime = "collection"
    if os.path.isfile(book_path):
        try:
            mtime = str(os.path.getmtime(book_path))
        except Exception:
            swallow.note('books_read._build_book_bm25_index:L1353')
            pass
    cache_key = f"{book_path}:{mtime}"
    if cache_key in _sb._book_bm25_cache:
        return _sb._book_bm25_cache[cache_key]
    chapter_texts = []
    chapter_meta = []
    for i, ch in enumerate(chapters):
        text = ch.get('text', '')
        title = ch.get('title', f'Глава {i+1}')
        if text.strip():
            chapter_texts.append(text)
            chapter_meta.append({'index': i, 'title': title})
    if not chapter_texts:
        return None, [], []
    tokenized = [re.findall(r'\w+', t.lower()) for t in chapter_texts]
    try:
        from rank_bm25 import BM25Okapi
        bm25 = BM25Okapi(tokenized)
        _sb._book_bm25_cache[cache_key] = (bm25, chapter_texts, chapter_meta)
        return bm25, chapter_texts, chapter_meta
    except Exception:
        swallow.note('books_read._build_book_bm25_index:L1375')
        return None, [], []
