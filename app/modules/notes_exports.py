"""Экспорт заметки в PDF/DOCX и извлечение текста из PDF.

Перенесено из server_backup.py без изменения тел. Сторонние библиотеки (fpdf,
docx, pdfplumber, pdfminer, pypdf) импортируются внутри функций в try/except,
поэтому модулю нужен только logger через _sb: он общий с остальным кодом, и
предупреждение об ошибке экспорта должно попадать в тот же лог.
"""

import os

import server_backup as _sb
import swallow

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
        _sb.logger.warning(f"PDF export failed: {e}")
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
        _sb.logger.warning(f"DOCX export failed: {e}")
        return False

def _extract_text_from_pdf(filepath):
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            return '\n'.join(p.extract_text() or '' for p in pdf.pages)
    except ImportError:
        swallow.note('notes_exports._extract_text_from_pdf:L90')
        pass
    try:
        from pdfminer.high_level import extract_text
        return extract_text(filepath)
    except ImportError:
        swallow.note('notes_exports._extract_text_from_pdf:L96')
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(filepath)
        return '\n'.join(p.extract_text() or '' for p in reader.pages)
    except ImportError:
        swallow.note('notes_exports._extract_text_from_pdf:L103')
        pass
    return ""
