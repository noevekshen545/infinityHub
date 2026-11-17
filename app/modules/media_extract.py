"""Извлечение текста и метаданных из документов, картинок, аудио и видео.

Всё локально: Tesseract/DjVu — внешние бинарники, если установлены; PDF/DOCX/
EPUB/FB2 — через python-библиотеки; аудио-теги — mutagen; кадры видео — PyAV.
Модуль не знает про профили и флаги: только файл → текст/превью/кадры.
"""

import html
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
import swallow

logger = logging.getLogger("infinity.media_extract")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BASE = os.path.dirname(os.path.dirname(_ROOT))  # <корень сборки>
_PROJECT_BIN = [
    os.path.join(_BASE, "tools", "media_bin", "Library", "bin"),
    os.path.join(_BASE, "tools", "media_bin", "bin"),
    os.path.join(_ROOT, "tools", "media_bin", "Library", "bin"),
    os.path.join(_ROOT, "tools", "media_bin", "bin"),
    os.path.join(_ROOT, "tools", "djvu"),
]
_TESSDATA = [
    os.path.join(_BASE, "tools", "media_bin", "share", "tessdata"),
    os.path.join(_ROOT, "tools", "media_bin", "share", "tessdata"),
    r"C:\Program Files\Tesseract-OCR\tessdata",
    r"C:\Program Files (x86)\Tesseract-OCR\tessdata",
]

DOC_EXTENSIONS = {".pdf", ".docx", ".epub", ".fb2", ".rtf", ".djvu", ".djv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff",
                    ".gif", ".heic", ".avif"}
AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".opus", ".m4a", ".aac",
                    ".wma", ".aiff", ".ape", ".amr"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv",
                    ".m4v", ".mpg", ".mpeg", ".ts", ".3gp"}

ZST_SUFFIX = ".zst"

MAX_DOC_CHARS = 200_000
MAX_PDF_PAGES = 300
MAX_DJVU_PAGES = 200
OCR_TIMEOUT = 120
MAX_VIDEO_FRAMES = 120
THUMB_SIDE = 512


def logical_ext(path: str) -> str:
    """Расширение, которое было бы у файла без прозрачного сжатия: .docx.zst → .docx."""
    if path.lower().endswith(ZST_SUFFIX):
        path = path[:-len(ZST_SUFFIX)]
    return os.path.splitext(path)[1].lower()


@contextmanager
def materialized(path: str):
    """Дать обычный файл: компрессия файлового менеджера хранит .zst рядом.

    Сторонние читатели (PyMuPDF, python-docx, mutagen, PyAV, Tesseract) не знают
    про прозрачное сжатие и открывают файл по расширению, поэтому сжатый документ
    распаковывается во временный каталог только на время извлечения.
    """
    if not path.lower().endswith(ZST_SUFFIX):
        yield path
        return
    import compression_module as cz
    tmpdir = tempfile.mkdtemp(prefix="infinity_media_")
    out = os.path.join(tmpdir, "data" + (logical_ext(path) or ".bin"))
    try:
        cz.decompress_file_to(path, out)
        yield out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def kind_for(path: str) -> str:
    ext = logical_ext(path)
    if ext in DOC_EXTENSIONS:
        return "document"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return ""


_TOOL_LOCK = threading.Lock()
_TOOL_CACHE = {}          # (имена, каталоги) -> (путь, время поиска)
_TOOL_MISS_TTL = 30.0     # отрицательный итог держим недолго: поставили софт — увидят


def _tool(names, extra=()):
    """Найти бинарник, не пересканируя PATH на каждый вызов.

    py-spy на /api/media-index/status показал: 92% времени уходило в
    shutil.which(tesseract/djvutxt) — полный обход PATH с access-проверками
    на каждом опросе страницы. Положительный путь кэшируем бессрочно (одна
    проверка isfile на попадании), промах — на 30 с, чтобы установленный
    при работающем приложении инструмент появился без перезапуска.
    """
    key = (tuple(n for n in names if isinstance(n, str) and n), tuple(extra))
    hit = _TOOL_CACHE.get(key)
    if hit is not None:
        path, stamp = hit
        if path and os.path.isfile(path):
            return path
        if not path and time.time() - stamp < _TOOL_MISS_TTL:
            return ""
    found = ""
    for name in names:
        if not isinstance(name, str) or not name:
            continue
        if os.path.isfile(name):
            found = name
            break
    if not found:
        for name in names:
            if not isinstance(name, str) or not name:
                continue
            found = shutil.which(name) or ""
            if found:
                break
    if not found:
        for base in extra:
            if not isinstance(base, str) or not base:
                continue
            for name in names:
                if not isinstance(name, str) or not name:
                    continue
                cand = os.path.join(base, name)
                if os.path.isfile(cand):
                    found = cand
                    break
            if found:
                break
    with _TOOL_LOCK:
        _TOOL_CACHE[key] = (found, time.time())
    return found


def tesseract_path() -> str:
    return _tool([os.environ.get("TESSERACT_CMD"), "tesseract.exe", "tesseract"],
                 _PROJECT_BIN + [r"C:\Program Files\Tesseract-OCR",
                                 r"C:\Program Files (x86)\Tesseract-OCR"])


def djvutxt_path() -> str:
    return _tool([os.environ.get("DJVUTXT_CMD"), "djvutxt.exe", "djvutxt"],
                 _PROJECT_BIN + [r"C:\Program Files\DjVuLibre",
                                 r"C:\Program Files (x86)\DjVuLibre"])


def ddjvu_path() -> str:
    return _tool([os.environ.get("DDJVU_CMD"), "ddjvu.exe", "ddjvu"],
                 _PROJECT_BIN + [r"C:\Program Files\DjVuLibre",
                                 r"C:\Program Files (x86)\DjVuLibre"])


def _ocr_env():
    env = dict(os.environ)
    for base in _TESSDATA:
        if os.path.isdir(base):
            env["TESSDATA_PREFIX"] = base
            break
    return env


def tools_status() -> dict:
    return {
        "tesseract": bool(tesseract_path()),
        "djvu": bool(djvutxt_path()),
        "pdf": _has("fitz"),
        "docx": _has("docx"),
        "epub": _has("ebooklib"),
    }


_HAS_CACHE = {}           # имя модуля -> (есть, время проверки)
_HAS_TTL = 300.0          # состав пакетов на ходу не меняется; редкий повтор — страховка


def _has(name: str) -> bool:
    hit = _HAS_CACHE.get(name)
    if hit is not None and time.time() - hit[1] < _HAS_TTL:
        return hit[0]
    try:
        __import__(name)
        ok = True
    except Exception:
        swallow.note('media_extract._has:L161')
        ok = False
    _HAS_CACHE[name] = (ok, time.time())
    return ok


_OCR_LANGS = None


def ocr_languages() -> str:
    global _OCR_LANGS
    if _OCR_LANGS is not None:
        return _OCR_LANGS
    exe = tesseract_path()
    if not exe:
        _OCR_LANGS = ""
        return _OCR_LANGS
    try:
        res = subprocess.run(
            [exe, "--list-langs"], capture_output=True,
            text=True, errors="replace", timeout=20, env=_ocr_env())
        out = (res.stdout or "") + "\n" + (res.stderr or "")
        langs = [ln.strip() for ln in out.splitlines()
                 if ln.strip() in ("rus", "eng", "osd")]
        _OCR_LANGS = "+".join(x for x in ("rus", "eng") if x in langs) or "eng"
    except Exception:
        swallow.note('media_extract.ocr_languages:L185')
        _OCR_LANGS = "eng"
    return _OCR_LANGS


def _low_priority():
    try:
        import sys
        if sys.platform == "win32":
            import ctypes
            h = ctypes.windll.kernel32.GetCurrentThread()
            ctypes.windll.kernel32.SetThreadPriority(h, 0x00010000)
    except Exception:
        swallow.note('media_extract._low_priority:L198')
        pass


def ocr_image_bytes(data: bytes) -> str:
    exe = tesseract_path()
    if not exe or not data:
        return ""
    try:
        res = subprocess.run(
            [exe, "stdin", "stdout", "-l", ocr_languages(), "--psm", "3"],
            input=data, capture_output=True, timeout=OCR_TIMEOUT,
            env=_ocr_env())
        text = (res.stdout or b"").decode("utf-8", errors="replace")
        return text.strip()
    except Exception as e:
        logger.warning("OCR: %s", e)
        return ""


def ocr_image(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return ocr_image_bytes(f.read())
    except OSError as e:
        logger.warning("OCR open %s: %s", path, e)
        return ""


def _clean(text: str) -> str:
    text = (text or "").replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_DOC_CHARS]


def _pdf(path: str, want_ocr: bool) -> str:
    import fitz
    out = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            if i >= MAX_PDF_PAGES:
                break
            try:
                txt = page.get_text("text") or ""
            except Exception:
                swallow.note('media_extract._pdf:L244')
                txt = ""
            if len(txt.strip()) < 40 and want_ocr:
                try:
                    png = page.get_pixmap(dpi=200).tobytes("png")
                    txt = ocr_image_bytes(png) or txt
                except Exception as e:
                    logger.warning("PDF OCR page %s: %s", i, e)
            if txt.strip():
                out.append(txt)
            if sum(len(x) for x in out) >= MAX_DOC_CHARS:
                break
    return _clean("\n\n".join(out))


def _docx(path: str) -> str:
    import docx
    d = docx.Document(path)
    parts = [p.text for p in d.paragraphs if p.text and p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return _clean("\n".join(parts))


def _epub(path: str) -> str:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    book = epub.read_epub(path, options={"ignore_ncx": True})
    parts = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        try:
            soup = BeautifulSoup(item.get_content(), "html.parser")
            txt = soup.get_text("\n")
            if txt.strip():
                parts.append(txt)
        except Exception:
            swallow.note('media_extract._epub:L284')
            continue
        if sum(len(x) for x in parts) >= MAX_DOC_CHARS:
            break
    return _clean("\n\n".join(parts))


def _fb2(path: str) -> str:
    import xml.etree.ElementTree as ET
    tree = ET.parse(path)
    parts = []
    for el in tree.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("p", "v", "subtitle", "title", "section"):
            if el.text and el.text.strip():
                parts.append(el.text.strip())
            for ch in el:
                if ch.tail and ch.tail.strip():
                    parts.append(ch.tail.strip())
    return _clean("\n".join(parts))


_RTF_CTRL = re.compile(r"\\[a-zA-Z]+-?\d* ?|[{}]|\\'[0-9a-fA-F]{2}|\\[^a-zA-Z]")


def _rtf(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read().decode("cp1251", errors="replace")

    def repl(m):
        s = m.group(0)
        if s.startswith("\\'") and len(s) == 4:
            try:
                return bytes([int(s[2:], 16)]).decode("cp1251", errors="replace")
            except ValueError:
                swallow.note('media_extract.repl:L319')
                return ""
        if s == "\\par" or s == "\\line":
            return "\n"
        return ""

    return _clean(_RTF_CTRL.sub(repl, raw))


def _djvu(path: str, want_ocr: bool) -> str:
    exe = djvutxt_path()
    if exe:
        try:
            res = subprocess.run([exe, path], capture_output=True,
                                 timeout=OCR_TIMEOUT)
            txt = (res.stdout or b"").decode("utf-8", errors="replace")
            if txt.strip():
                return _clean(txt)
        except Exception as e:
            logger.warning("djvutxt %s: %s", path, e)
    dd = ddjvu_path()
    if not (want_ocr and dd):
        return ""
    parts = []
    tmpdir = tempfile.mkdtemp(prefix="djvu_")
    try:
        for page in range(1, MAX_DJVU_PAGES + 1):
            out = os.path.join(tmpdir, "p%d.tiff" % page)
            try:
                res = subprocess.run(
                    [dd, "-format=tiff", "-page=%d" % page, path, out],
                    capture_output=True, timeout=OCR_TIMEOUT,
                    env=_ocr_env())
            except Exception:
                swallow.note('media_extract._djvu:L353')
                break
            if not os.path.isfile(out) or res.returncode != 0:
                break
            txt = ocr_image(out)
            if txt:
                parts.append(txt)
            os.remove(out)
            if sum(len(x) for x in parts) >= MAX_DOC_CHARS:
                break
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return _clean("\n\n".join(parts))


def extract_document(path: str, want_ocr: bool = True) -> str:
    ext = logical_ext(path)
    with materialized(path) as real:
        try:
            if ext == ".pdf":
                return _pdf(real, want_ocr)
            if ext == ".docx":
                return _docx(real)
            if ext == ".epub":
                return _epub(real)
            if ext == ".fb2":
                return _fb2(real)
            if ext == ".rtf":
                return _rtf(real)
            if ext in (".djvu", ".djv"):
                return _djvu(real, want_ocr)
        except Exception as e:
            logger.warning("extract %s: %s", path, e)
    return ""


_TAG_KEYS = ("title", "artist", "album", "albumartist", "genre", "date",
             "composer", "performer", "comment")


def audio_meta(path: str) -> dict:
    with materialized(path) as real:
        return _audio_meta(real)


def _audio_meta(path: str) -> dict:
    info = {"text": "", "duration": 0.0}
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        if f is None:
            return info
        vals = []
        for key in _TAG_KEYS:
            try:
                v = f.get(key)
            except Exception:
                swallow.note('media_extract._audio_meta:L410')
                v = None
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v if x)
            if v and str(v).strip():
                vals.append(str(v).strip())
        try:
            info["duration"] = float(f.info.length or 0)
        except Exception:
            swallow.note('media_extract._audio_meta:L419')
            pass
        info["text"] = _clean(" — ".join(dict.fromkeys(vals)))
    except Exception as e:
        logger.warning("audio tags %s: %s", path, e)
    return info


def _suggest_step(duration: float) -> float:
    if duration <= 0:
        return 10.0
    return max(3.0, float(duration) / MAX_VIDEO_FRAMES)


def video_keyframes(path: str, out_dir: str, stem: str) -> list:
    with materialized(path) as real:
        return _video_keyframes(real, out_dir, stem)


def _video_keyframes(path: str, out_dir: str, stem: str) -> list:
    """Кадры равномерно по времени; превью 512 px пишутся в out_dir."""
    try:
        import av
    except Exception:
        swallow.note('media_extract._video_keyframes:L443')
        return []
    frames = []
    try:
        with av.open(path) as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                return []
            duration = 0.0
            try:
                duration = float(container.duration or 0) / 1_000_000.0
            except Exception:
                swallow.note('media_extract._video_keyframes:L455')
                pass
            if duration <= 0:
                try:
                    duration = float(stream.duration * stream.time_base)
                except Exception:
                    swallow.note('media_extract._video_keyframes:L461')
                    duration = 0.0
            step = _suggest_step(duration)
            targets = []
            t = 0.0
            while len(targets) < MAX_VIDEO_FRAMES and (duration <= 0 or t < duration):
                targets.append(t)
                t += step
                if duration <= 0 and len(targets) >= 12:
                    break
            tb = float(stream.time_base or 0.001)
            for i, target in enumerate(targets):
                try:
                    container.seek(int(target / tb), stream=stream, backward=True)
                    frame = None
                    for f in container.decode(video=stream.index):
                        if f.pts is None:
                            continue
                        ft = float(f.pts * f.time_base)
                        if ft + 0.05 >= target:
                            frame = f
                            break
                    if frame is None:
                        continue
                    img = frame.to_image()
                    img.thumbnail((THUMB_SIDE, THUMB_SIDE))
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    thumb = os.path.join(out_dir, "%s_%03d.jpg" % (stem, i))
                    img.save(thumb, "JPEG", quality=82)
                    frames.append({"t": round(target, 1), "thumb": thumb})
                except Exception:
                    swallow.note('media_extract._video_keyframes:L493')
                    continue
    except Exception as e:
        logger.warning("video frames %s: %s", path, e)
    return frames


def image_thumb(path: str, out_path: str) -> bool:
    try:
        from PIL import Image, ImageOps
        with materialized(path) as real, Image.open(real) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((THUMB_SIDE, THUMB_SIDE))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            img.save(out_path, "JPEG", quality=82)
        return True
    except Exception as e:
        logger.warning("thumb %s: %s", path, e)
        return False


def extract(path: str, kind: str, want_ocr: bool = True) -> dict:
    """Возвращает {text, preview, thumb, frames, duration, tags}."""
    out = {"text": "", "preview": "", "thumb": "", "frames": [],
           "duration": 0.0, "tags": ""}
    if kind == "document":
        out["text"] = extract_document(path, want_ocr)
    elif kind == "image":
        if want_ocr:
            with materialized(path) as real:
                out["text"] = _clean(ocr_image(real))
    elif kind == "audio":
        meta = audio_meta(path)
        out["text"] = meta["text"]
        out["duration"] = meta["duration"]
        out["tags"] = meta["text"]
    elif kind == "video":
        base = os.path.basename(path)
        if base.lower().endswith(ZST_SUFFIX):
            base = base[:-len(ZST_SUFFIX)]
        out["text"] = _clean(os.path.splitext(base)[0])
    out["preview"] = " ".join(out["text"].split())[:300]
    return out
