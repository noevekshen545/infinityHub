"""Секция 'ENTERPRISE FEATURES' монолита `app/server_backup.py`.

Вынесена механически (`tools/_extract_section.py`): код ниже не правился,
имена из других секций монолита не импортируются, а связываются
хвостом монолита через `_bind()` (см. `_FOREIGN`): порядок хвостовых
ре-экспортов произвольный, и прямой импорт упёрся бы в частично
инициализированный модуль.
Монолит ре-экспортирует всё, что определено здесь, поэтому прежние
потребители (`import server`, `import server_backup`) работают без правок.
"""

# перенесено из головы монолита: эти имена секция использовала как импорты
import os, sys, json, math, argparse, time, re, io, zipfile, tempfile, base64, hashlib, logging, subprocess, select, mimetypes, configparser, shlex
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, quote
import urllib.request
import urllib.error
from io import BytesIO
import threading
from config_module import (get_config, get_api_key, load_config, reload_config,
                           normalize_openai_base)
import ask_modes

# эти имена нужны операторам верхнего уровня секции, поэтому импортируются
# сразу: все они определены в монолите ДО хвостовых ре-экспортов
from server_backup import (
    NOTES_DIR,
)
import swallow
# ===================== ENTERPRISE FEATURES =====================

_NOTES_EXPORT_DIR = os.path.join(os.path.expanduser(NOTES_DIR), "_exports")
_NOTES_TEMPLATES_FILE = os.path.expanduser("~/.infinity/notes_templates.json")

def notes_get_templates():
    """Get note templates."""
    if not os.path.exists(_NOTES_TEMPLATES_FILE):
        defaults = [
            {"id": "meeting", "name": "📋 Совещание", "content": "# Совещание\n\n**Дата:** {{date}}\n**Участники:** \n\n## Повестка\n\n\n## Решения\n\n\n## Задачи\n- [ ] \n"},
            {"id": "daily", "name": "📅 Ежедневный отчёт", "content": "# Ежедневный отчёт\n\n**Дата:** {{date}}\n\n## Сделано\n\n\n## В процессе\n\n\n## Планы на завтра\n\n"},
            {"id": "research", "name": "🔬 Исследование", "content": "# Исследование: \n\n**Дата:** {{date}}\n**Тема:** \n\n## Вопрос\n\n\n## Методология\n\n\n## Результаты\n\n\n## Выводы\n\n"},
            {"id": "meeting_notes", "name": "📝 Meeting Notes", "content": "# Meeting Notes\n\n**Date:** {{date}}\n**Attendees:** \n\n## Agenda\n\n\n## Action Items\n- [ ] \n"}
        ]
        os.makedirs(os.path.dirname(_NOTES_TEMPLATES_FILE), exist_ok=True)
        with open(_NOTES_TEMPLATES_FILE, "w", encoding="utf-8") as f:
            json.dump(defaults, f, ensure_ascii=False, indent=2)
        return {"templates": defaults}
    with open(_NOTES_TEMPLATES_FILE, encoding="utf-8") as f:
        return {"templates": json.load(f)}

def _export_note_as_pdf(title, content, filepath):
    try:
        from fpdf import FPDF
        from fpdf.enums import XPos, YPos
        pdf = FPDF()
        pdf.add_page()
        font_name = "Helvetica"
        has_bold = False
        for fp, fp_bold in [("/mnt/c/Windows/Fonts/arial.ttf", "/mnt/c/Windows/Fonts/arialbd.ttf"),
                            ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
                            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                            ("/System/Library/Fonts/Arial.ttf", None)]:
            if os.path.exists(fp):
                pdf.add_font("UniFont", "", fp)
                if fp_bold and os.path.exists(fp_bold):
                    pdf.add_font("UniFont", "B", fp_bold)
                    has_bold = True
                font_name = "UniFont"
                break
        bold_style = "B" if has_bold else ""
        pdf.set_font(font_name, bold_style, 16)
        pdf.multi_cell(0, 10, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)
        pdf.set_font(font_name, "", 11)
        for line in content.split('\n'):
            line = line.strip()
            if not line:
                pdf.ln(4)
            elif line.startswith('## '):
                pdf.set_font(font_name, bold_style, 13)
                pdf.multi_cell(0, 8, line[3:], new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_font(font_name, "", 11)
            elif line.startswith('### '):
                pdf.set_font(font_name, bold_style, 12)
                pdf.multi_cell(0, 7, line[4:], new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_font(font_name, "", 11)
            elif line.startswith('- '):
                pdf.cell(5, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.multi_cell(0, 6, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            else:
                pdf.multi_cell(0, 6, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.output(filepath)
        return True
    except Exception as e:
        logger.warning(f"PDF export failed: {e}")
        return False

def _export_note_as_docx(title, content, filepath):
    try:
        from docx import Document
        from docx.shared import Pt
        doc = Document()
        heading = doc.add_heading(title, level=1)
        for line in content.split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.startswith('## '):
                doc.add_heading(line[3:], level=2)
            elif line.startswith('### '):
                doc.add_heading(line[4:], level=3)
            elif line.startswith('- '):
                p = doc.add_paragraph(line, style='List Bullet')
            else:
                doc.add_paragraph(line)
        doc.save(filepath)
        return True
    except Exception as e:
        logger.warning(f"DOCX export failed: {e}")
        return False

def notes_export_note(nid, format="markdown"):
    """Export a note."""
    os.makedirs(_NOTES_EXPORT_DIR, exist_ok=True)
    # Get note content
    note = None
    notes_dir = os.path.expanduser(NOTES_DIR) if NOTES_DIR else os.path.expanduser("~/notes")
    for root, dirs, files in os.walk(notes_dir):
        for f in files:
            if f == f"{nid}.json":
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    note = json.load(fh)
                break
        if note:
            break
    if not note:
        return {"error": "Заметка не найдена"}
    
    title = note.get("title", "export")
    content = note.get("content", "")
    timestamp = int(time.time())
    safe = re.sub(r'[\\/*?:"<>|]', '_', title)[:50]
    
    if format == "markdown":
        filename = f"{safe}_{timestamp}.md"
        filepath = os.path.join(_NOTES_EXPORT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n")
            f.write(content)
        return {"ok": True, "path": filepath, "filename": filename}
    elif format == "html":
        filename = f"{safe}_{timestamp}.html"
        filepath = os.path.join(_NOTES_EXPORT_DIR, filename)
        html_content = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{title}</title>
<style>body{{font-family:system-ui,max-width:800px;margin:0 auto;padding:20px}}h1{{border-bottom:1px solid #eee}}pre{{background:#f5f5f5;padding:16px;border-radius:8px}}code{{background:#f5f5f5;padding:2px 6px;border-radius:4px}}</style>
</head><body><h1>{title}</h1><div>{content.replace(chr(10), '<br>')}</div></body></html>"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_content)
        return {"ok": True, "path": filepath, "filename": filename}
    elif format == "pdf":
        filename = f"{safe}_{timestamp}.pdf"
        filepath = os.path.join(_NOTES_EXPORT_DIR, filename)
        ok = _export_note_as_pdf(title, content, filepath)
        if ok:
            return {"ok": True, "path": filepath, "filename": filename}
        return {"error": "Не удалось создать PDF. Установите: pip install fpdf2"}
    elif format == "docx":
        filename = f"{safe}_{timestamp}.docx"
        filepath = os.path.join(_NOTES_EXPORT_DIR, filename)
        ok = _export_note_as_docx(title, content, filepath)
        if ok:
            return {"ok": True, "path": filepath, "filename": filename}
        return {"error": "Не удалось создать DOCX. Установите: pip install python-docx"}
    return {"error": "Неизвестный формат"}

def notes_export_all(format="markdown", path=None):
    """Export all notes."""
    export_dir = os.path.expanduser(path.strip()) if path else _NOTES_EXPORT_DIR
    logger.info(f"notes_export_all: format={format}, path={path}, export_dir={export_dir}")
    os.makedirs(export_dir, exist_ok=True)
    notes_dir = os.path.expanduser(NOTES_DIR) if NOTES_DIR else os.path.expanduser("~/notes")
    logger.info(f"notes_export_all: notes_dir={notes_dir}, isdir={os.path.isdir(notes_dir)}")
    if not os.path.isdir(notes_dir):
        logger.warning(f"notes_export_all: notes_dir not found: {notes_dir}")
        return {"ok": False, "error": f"Директория заметок не найдена: {notes_dir}"}
    count = 0
    err_count = 0
    for root, dirs, files in os.walk(notes_dir):
        for f in files:
            if f.endswith(".json") and not f.startswith("_"):
                try:
                    fp_in = os.path.join(root, f)
                    with open(fp_in, encoding="utf-8") as fh:
                        note = json.load(fh)
                    title = note.get("title", "export")
                    content = note.get("content", "")
                    ts = int(time.time())
                    safe = re.sub(r'[\\/*?:"<>|]', '_', title)[:50]
                    if format == "markdown":
                        fn = f"{safe}_{ts}_{count}.md"
                        fp = os.path.join(export_dir, fn)
                        with open(fp, "w", encoding="utf-8") as out:
                            out.write(f"# {title}\n\n{content}")
                    elif format == "html":
                        fn = f"{safe}_{ts}_{count}.html"
                        fp = os.path.join(export_dir, fn)
                        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>{title}</title></head><body><h1>{title}</h1><div>{content.replace(chr(10), '<br>')}</div></body></html>"""
                        with open(fp, "w", encoding="utf-8") as out:
                            out.write(html)
                    elif format == "json":
                        fn = f"{safe}_{ts}_{count}.json"
                        fp = os.path.join(export_dir, fn)
                        with open(fp, "w", encoding="utf-8") as out:
                            json.dump(note, out, ensure_ascii=False, indent=2)
                    elif format == "pdf":
                        fn = f"{safe}_{ts}_{count}.pdf"
                        fp = os.path.join(export_dir, fn)
                        if not _export_note_as_pdf(title, content, fp):
                            err_count += 1
                            logger.warning(f"notes_export_all: PDF export failed for {f}")
                            continue
                    elif format == "docx":
                        fn = f"{safe}_{ts}_{count}.docx"
                        fp = os.path.join(export_dir, fn)
                        if not _export_note_as_docx(title, content, fp):
                            err_count += 1
                            logger.warning(f"notes_export_all: DOCX export failed for {f}")
                            continue
                    count += 1
                except Exception as e:
                    err_count += 1
                    logger.warning(f"notes_export_all: error exporting {f}: {e}")
    if count == 0:
        return {"ok": True, "count": 0, "path": os.path.abspath(export_dir), "warning": "Не найдено заметок для экспорта. Убедитесь, что в директории есть .json файлы заметок."}
    logger.info(f"notes_export_all: exported {count} notes, errors={err_count} to {export_dir}")
    return {"ok": True, "count": count, "errors": err_count, "path": os.path.abspath(export_dir)}

def _extract_text_from_pdf(filepath):
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            return '\n'.join(p.extract_text() or '' for p in pdf.pages)
    except ImportError:
        swallow.note('enterprise._extract_text_from_pdf:L246')
        pass
    try:
        from pdfminer.high_level import extract_text
        return extract_text(filepath)
    except ImportError:
        swallow.note('enterprise._extract_text_from_pdf:L252')
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(filepath)
        return '\n'.join(p.extract_text() or '' for p in reader.pages)
    except ImportError:
        swallow.note('enterprise._extract_text_from_pdf:L259')
        pass
    return ""

def notes_import_note(filename, content, folder=None, is_binary=False):
    """Import a note from file content."""
    title = os.path.splitext(os.path.basename(filename))[0]
    
    if is_binary:
        ext = os.path.splitext(filename)[1].lower()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        try:
            raw = base64.b64decode(content)
            tmp.write(raw)
            tmp.close()
            if ext == '.pdf':
                content = _extract_text_from_pdf(tmp.name)
            elif ext in ('.doc', '.docx'):
                try:
                    import docx
                    doc = docx.Document(tmp.name)
                    content = '\n'.join(p.text for p in doc.paragraphs)
                except Exception as e:
                    logger.warning(f"notes_import_note: docx parse error: {e}")
                    content = ""
            if not content:
                content = f"[Импортирован файл: {filename}]"
        except Exception as e:
            logger.warning(f"notes_import_note: binary import error: {e}")
            content = f"[Ошибка импорта файла: {filename}]"
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                swallow.note('enterprise.notes_import_note:L293')
                pass
    else:
        # Strip any HTML tags if it's html
        if filename.endswith(".html"):
            content = re.sub(r'<[^>]+>', ' ', content)
            content = content.strip()
        # Parse markdown title from first line
        if filename.endswith(".md") or filename.endswith(".markdown"):
            lines = content.split('\n', 1)
            if lines[0].startswith('# '):
                title = lines[0][2:].strip()
                content = lines[1] if len(lines) > 1 else ''
    
    note_id = str(int(time.time() * 1000))
    folder = folder or "inbox"
    note = {
        "id": note_id,
        "title": title,
        "content": content.strip(),
        "folder": folder,
        "created": int(time.time()),
        "updated": int(time.time()),
        "tags": []
    }
    notes_dir = os.path.expanduser(NOTES_DIR) if NOTES_DIR else os.path.expanduser("~/notes")
    fp = os.path.join(notes_dir, folder, f"{note_id}.json")
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(note, f, ensure_ascii=False, indent=2)
    return {"ok": True, "id": note_id, "title": title}

def notes_fulltext_search(query, limit=20):
    """Full-text search across all notes."""
    results = []
    ql = query.lower()
    notes_dir = os.path.expanduser(NOTES_DIR) if NOTES_DIR else os.path.expanduser("~/notes")
    for root, dirs, files in os.walk(notes_dir):
        for f in files:
            if not f.endswith(".json") or f.startswith("_"):
                continue
            fp = os.path.join(root, f)
            try:
                with open(fp, encoding="utf-8") as fh:
                    note = json.load(fh)
                title = note.get("title", "")
                content = note.get("content", "")
                if ql in title.lower() or ql in content.lower():
                    idx = content.lower().find(ql)
                    preview_start = max(0, idx - 100)
                    preview_end = min(len(content), idx + len(query) + 100)
                    preview = content[preview_start:preview_end]
                    results.append({
                        "id": note.get("id", f.replace(".json", "")),
                        "title": title,
                        "folder": note.get("folder", "inbox"),
                        "preview": preview,
                        "content": content[:2000],
                        "updated": note.get("updated", 0),
                        "score": 1.0,
                        "rank": 1.0,
                    })
            except Exception:
                swallow.note('enterprise.notes_fulltext_search:L356')
                continue
    results.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return results[:limit]



# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'base64',
    'json',
    'logger',
    'os',
    're',
    'tempfile',
    'time',
)

def _bind(namespace=None):
    if namespace is None:
        import server_backup as _sb
        namespace = vars(_sb)
    _g = globals()
    for _n in _FOREIGN:
        if _n not in _g and _n in namespace:
            _g[_n] = namespace[_n]
    return [n for n in _FOREIGN if n not in globals()]
