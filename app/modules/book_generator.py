#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Book Generator Module — генератор книг через LM Studio/OpenAI-совместимый API.
Интегрируется с Infinity Search.

Использование:
    from modules.book_generator import BookGenerator, Book
    
    gen = BookGenerator(books_dir="/path/to/books", lm_url="http://localhost:1234")
    book = gen.create_book(title="Тест", author="Автор", theme="Тема")
    book.generate_plan(model="gemma-4-e2b-it")
    content = book.generate_page(1, model="gemma-4-e2b-it")
    fb2_path = book.export_fb2()
"""

import json
import os
import uuid
import time
import html as html_module
import urllib.request
import urllib.error
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
import swallow

DEFAULT_BOOKS_DIR = Path("/home/infinity/pars/books/")
DEFAULT_MODEL = "gemma-4-e2b-it"


def load_app_config(config_path: Optional[str] = None) -> dict:
    if config_path is None:
        search_paths = [
            Path(__file__).parent.parent / "config.json",
            Path("/home/infinity/infinitysearch/runjson/config.json"),
            Path("config.json"),
        ]
        for p in search_paths:
            if p.exists():
                config_path = str(p)
                break

    if config_path and Path(config_path).exists():
        return json.loads(Path(config_path).read_text("utf-8"))
    return {}


def get_books_dir(config: Optional[dict] = None, profile_id: Optional[str] = None) -> Path:
    """Каталог книг: пер-профильный для BookGen ('data/profiles/<id>/data/bookgen')."""
    if profile_id:
        base = Path(__file__).resolve().parent.parent / "data" / "profiles" / str(profile_id) / "data" / "bookgen"
        base.mkdir(parents=True, exist_ok=True)
        return base
    if config is None:
        config = load_app_config()
    books_dir = config.get("books_dir", str(DEFAULT_BOOKS_DIR))
    return Path(books_dir)


def get_lm_config(config: Optional[dict] = None) -> tuple:
    if config is None:
        # Источник — конфиг АКТИВНОГО профиля (он приоритетнее app/config.json,
        # см. LAYERS.md): облачный провайдер выбирается именно в профиле.
        try:
            from config_module import get_config
            config = get_config() or {}
        except Exception:
            swallow.note('book_generator.get_lm_config:L71')
            config = load_app_config()
    backend = config.get("backend", "lmstudio")
    if backend == "llamacpp":
        lm_config = config.get("llama_cpp", {})
    elif backend == "custom":
        # Облачный провайдер из настроек: без этой ветки генератор книг уходил
        # в lm_studio.url (localhost:1234) и падал с «LLM недоступен».
        lm_config = config.get("custom", {})
    else:
        lm_config = config.get("lm_studio", {})
    if backend == "custom":
        try:
            from config_module import normalize_openai_base
            url = normalize_openai_base(lm_config.get("url", ""))
        except Exception:
            swallow.note('book_generator.get_lm_config:L87')
            url = (lm_config.get("url") or "").rstrip("/")
    else:
        url = lm_config.get("url", "http://localhost:8080" if backend == "llamacpp" else "http://localhost:1234")
    model = lm_config.get("model", "")
    api_key = lm_config.get("api_key", "")
    return url, model, api_key


def split_into_chunks(text: str, chunk_chars: int = 50000, overlap: int = 2000) -> list:
    """Режет текст на фрагменты по ~chunk_chars символов, стараясь рвать по границе абзаца."""
    text = (text or "").strip()
    chunks = []
    start = 0
    n = len(text)
    if n <= chunk_chars:
        return [text] if text else []
    while start < n:
        end = min(start + chunk_chars, n)
        if end < n:
            cut = text.rfind("\n\n", start + int(chunk_chars * 0.7), end)
            if cut == -1:
                cut = text.rfind(". ", start + int(chunk_chars * 0.7), end)
            if cut != -1:
                end = cut + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def extract_book_text(source: str, books_dir: Path) -> tuple:
    """Возвращает (полный_текст, название, автор) для книги-продолжения.
    source — имя файла в books_dir (.fb2/.txt/.md/.epub/.jsonl) или 'lib_<id>' из библиотеки."""
    source = (source or "").strip()
    if not source:
        raise ValueError("Не указан источник книги")

    if source.startswith("lib_"):
        lib_id = source[4:]
        try:
            from modules.collection_module import get_book_content
            content = None
            for bid in (lib_id, int(lib_id) if lib_id.isdigit() else None):
                if bid is None:
                    continue
                try:
                    content = get_book_content(bid)
                except Exception:
                    swallow.note('book_generator.extract_book_text:L139')
                    content = None
                if content:
                    break
            if not content:
                raise ValueError(f"Книга {lib_id} не найдена в библиотеке")
            text = content.get("full_text", "") or ""
            title = content.get("title", "") or lib_id
            author = content.get("author", "") or ""
            return text, title, author
        except ValueError:
            raise
        except Exception as e:
            raise RuntimeError(f"Ошибка чтения книги из библиотеки: {e}")

    file_path = Path(books_dir) / source
    if not file_path.exists() or not file_path.is_file():
        raise ValueError(f"Файл не найден: {source}")
    ext = file_path.suffix.lower()
    title = file_path.stem
    author = ""
    try:
        if ext == ".fb2":
            text = ""
            try:
                from modules.collection_module import parse_fb2
                data = parse_fb2(file_path.read_bytes())
            except Exception:
                swallow.note('book_generator.extract_book_text:L167')
                data = None
            if data:
                text = data.get("body_text", "") or ""
                title = data.get("title") or title
                author = ", ".join(a["full_name"] for a in data.get("authors", []))
                if not text:
                    text = "\n\n".join(
                        p.strip() for p in re.split(r"</p>|<section", data.get("body_text", ""))
                    ) if data.get("body_text") else ""
            if not text:
                text = _strip_fb2_xml(file_path.read_bytes())
        elif ext == ".epub":
            text = _extract_epub_text(file_path)
        elif ext == ".jsonl":
            parts = []
            for line in file_path.read_text("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    swallow.note('book_generator.extract_book_text:L190')
                    continue
                if isinstance(obj, dict):
                    t = obj.get("text") or obj.get("content") or obj.get("page")
                    if isinstance(t, str) and t.strip():
                        parts.append(t.strip())
            text = "\n\n".join(parts)
        elif ext in (".txt", ".md", ".json"):
            text = file_path.read_text("utf-8", errors="replace")
        else:
            raise ValueError(f"Формат {ext} не поддерживается для продолжения")
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"Ошибка чтения файла {source}: {e}")

    text = (text or "").strip()
    if not text:
        raise ValueError(f"В файле {source} нет текста")
    return text, title, author


def _strip_fb2_xml(xml_bytes: bytes) -> str:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return xml_bytes.decode("utf-8", errors="replace")
    parts = []
    for el in root.iter():
        if el.tag.split("}")[-1] == "p" and el.text:
            parts.append(el.text.strip())
    return "\n\n".join(parts)


def _extract_epub_text(file_path: Path) -> str:
    import zipfile
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self.skip = 0
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1
        def handle_endtag(self, tag):
            if tag in ("script", "style") and self.skip > 0:
                self.skip -= 1
            if tag in ("p", "div", "h1", "h2", "h3", "br", "li"):
                self.parts.append("\n")
        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    texts = []
    with zipfile.ZipFile(file_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith((".xhtml", ".html", ".htm", ".xml"))
                 and not n.startswith("META-INF") and not n.lower().endswith((".opf", ".ncx"))]
        for name in sorted(names):
            raw = zf.read(name)
            try:
                html = raw.decode("utf-8")
            except UnicodeDecodeError:
                html = raw.decode("cp1251", errors="replace")
            p = _TextExtractor()
            try:
                p.feed(html)
            except Exception:
                swallow.note('book_generator._extract_epub_text:L260')
                continue
            seg = " ".join(x.strip() for x in p.parts)
            seg = re.sub(r"\s+", " ", seg)
            if seg.strip():
                texts.append(seg.strip())
    return "\n\n".join(texts)


class LLMClient:
    def __init__(self, base_url: str = "http://localhost:1234", timeout: int = 600, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key

    def request(self, endpoint: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{endpoint}"
        data = json.dumps(payload).encode("utf-8") if payload else None
        req = urllib.request.Request(url, data=data, method="POST" if payload else "GET")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM недоступен: {e}. Проверьте сервер на {self.base_url}")
        except Exception as e:
            raise RuntimeError(f"Ошибка запроса: {e}")

    def list_models(self) -> list:
        try:
            data = self.request("/v1/models")
            return [m["id"] for m in data.get("data", [])]
        except Exception as e:
            print(f"[!] Ошибка получения моделей: {e}")
            return []

    def chat(self, model: str, messages: list, temperature: float = 0.85, max_tokens: int = 2048, retries: int = 3) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        last_err = "пустой ответ от модели"
        try:
            from aiengine.api import engine_begin, engine_update_text, engine_end
            engine_begin("book_generator", model)
        except Exception:
            swallow.note('book_generator.chat:L312')
            pass
        try:
            for attempt in range(retries):
                try:
                    data = self.request("/v1/chat/completions", payload)
                    msg = data["choices"][0]["message"]
                    content = msg.get("content") or msg.get("reasoning_content") or ""
                    if content and content.strip():
                        try:
                            engine_update_text(content)
                            timings = data.get("timings") or {}
                            engine_end("", tps=timings.get("predicted_per_second"),
                                       tokens=timings.get("predicted_n"),
                                       tokens_prompt=timings.get("prompt_n"))
                        except Exception:
                            swallow.note('book_generator.chat:L328')
                            pass
                        return content.strip()
                except Exception as e:
                    swallow.note('book_generator.chat:L332', e)
                    last_err = str(e)
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
        finally:
            try:
                engine_end()
            except Exception:
                swallow.note('book_generator.chat:L340')
                pass
        raise RuntimeError(f"Модель не ответила после {retries} попыток: {last_err}")


class Book:
    def __init__(self, book_id: str, books_dir: Path):
        self.id = book_id
        self.dir = books_dir / book_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "meta.json"
        self.scenario_path = self.dir / "scenario.md"
        self.characters_path = self.dir / "characters.md"
        self.outline_path = self.dir / "outline.md"
        self.draft_path = self.dir / "draft.md"
        self.world_path = self.dir / "world.md"
        self.study_path = self.dir / "study.md"
        self.pages_dir = self.dir / "pages"
        self.pages_dir.mkdir(exist_ok=True)
        self.proofreads_dir = self.dir / "proofreads"
        self.proofreads_dir.mkdir(exist_ok=True)
        self.fb2_path = self.dir / "book.fb2"
        self.txt_path = self.dir / "book.txt"
        self.md_path = self.dir / "book.md"

    def load_meta(self) -> dict:
        if self.meta_path.exists():
            return json.loads(self.meta_path.read_text("utf-8"))
        return {}

    def save_meta(self, meta: dict):
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")

    SYSTEM_WRITER = (
        "Ты — профессиональный писатель, редактор и литературный архитектор. "
        "Отвечай на русском языке, пиши развёрнуто и литературно."
    )

    def generate_plan(self, model: str, llm: LLMClient, force: bool = False):
        meta = self.load_meta()
        system = self.SYSTEM_WRITER
        errors = []

        if force or not self.scenario_path.exists() or not meta.get("scenario_ready"):
            try:
                scenario_prompt = self._build_scenario_prompt(meta)
                scenario = llm.chat(
                    model,
                    [{"role": "system", "content": system}, {"role": "user", "content": scenario_prompt}],
                    max_tokens=3000,
                )
                self.scenario_path.write_text(scenario, "utf-8")
                meta["scenario_ready"] = True
            except Exception as e:
                errors.append(f"scenario: {e}")

        if force or not self.characters_path.exists() or not meta.get("characters_ready"):
            try:
                chars_text = self._build_characters_text(meta)
                scenario_text = self.scenario_path.read_text("utf-8") if self.scenario_path.exists() else ""
                characters = llm.chat(
                    model,
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": f"Сценарий книги:\n{scenario_text}"},
                        {"role": "user", "content": chars_text},
                    ],
                    max_tokens=2500,
                )
                self.characters_path.write_text(characters, "utf-8")
                meta["characters_ready"] = True
            except Exception as e:
                errors.append(f"characters: {e}")

        if force or not self.outline_path.exists() or not meta.get("outline_ready"):
            try:
                scenario_text = self.scenario_path.read_text("utf-8") if self.scenario_path.exists() else ""
                chars_text = self.characters_path.read_text("utf-8") if self.characters_path.exists() else ""
                outline_text = self._build_outline_text(meta)
                outline = llm.chat(
                    model,
                    [
                        {"role": "system", "content": system},
                        {
                            "role": "user",
                            "content": f"Сценарий:\n{scenario_text}\n\nПерсонажи:\n{chars_text}",
                        },
                        {"role": "user", "content": outline_text},
                    ],
                    max_tokens=2500,
                )
                self.outline_path.write_text(outline, "utf-8")
                meta["outline_ready"] = True
            except Exception as e:
                errors.append(f"outline: {e}")

        self.save_meta(meta)

        if not self.world_path.exists() or self.world_path.stat().st_size == 0:
            try:
                self.generate_world(model, llm)
            except Exception:
                swallow.note('book_generator.generate_plan:L442')
                pass

        if errors:
            raise RuntimeError("Ошибки при генерации плана: " + "; ".join(errors))

    def regenerate_section(self, section: str, model: str, llm: LLMClient, instructions: str = "") -> str:
        """Regenerate a specific section: scenario | characters | outline."""
        meta = self.load_meta()
        system = self.SYSTEM_WRITER
        ref = self.get_reference_context()
        if section == "scenario":
            prompt = self._build_scenario_prompt(meta)
            if instructions:
                prompt += f"\n\nДополнительные указания: {instructions}"
            if ref:
                prompt += f"\n\n{ref}"
            text = llm.chat(model, [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ], max_tokens=3000)
            self.scenario_path.write_text(text, "utf-8")
            meta["scenario_ready"] = True
            self.save_meta(meta)
            return text
        if section == "characters":
            scenario = self.scenario_path.read_text("utf-8") if self.scenario_path.exists() else ""
            prompt = self._build_characters_text(meta)
            if instructions:
                prompt += f"\n\nДополнительные указания: {instructions}"
            if ref:
                prompt += f"\n\n{ref}"
            text = llm.chat(model, [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Сценарий:\n{scenario}"},
                {"role": "user", "content": prompt},
            ], max_tokens=2500)
            self.characters_path.write_text(text, "utf-8")
            meta["characters_ready"] = True
            self.save_meta(meta)
            return text
        if section == "outline":
            scenario = self.scenario_path.read_text("utf-8") if self.scenario_path.exists() else ""
            chars = self.characters_path.read_text("utf-8") if self.characters_path.exists() else ""
            prompt = self._build_outline_text(meta)
            if instructions:
                prompt += f"\n\nДополнительные указания: {instructions}"
            if ref:
                prompt += f"\n\n{ref}"
            text = llm.chat(model, [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Сценарий:\n{scenario}\n\nПерсонажи:\n{chars}"},
                {"role": "user", "content": prompt},
            ], max_tokens=2500)
            self.outline_path.write_text(text, "utf-8")
            meta["outline_ready"] = True
            self.save_meta(meta)
            return text
        raise ValueError(f"Unknown section: {section}")

    def ai_edit_section(self, section: str, current_text: str, instructions: str, model: str, llm: LLMClient) -> str:
        """Edit existing section text using AI based on instructions."""
        system = self.SYSTEM_WRITER + " Ты редактируешь существующий текст по указаниям автора."
        prompt = (
            f"Текущий текст ({section}):\n{current_text}\n\n"
            f"Указания автора: {instructions}\n\n"
            f"Верни ТОЛЬКО переработанный текст без комментариев и без вступления."
        )
        result = llm.chat(model, [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ], max_tokens=4096)
        if section == "scenario":
            self.scenario_path.write_text(result, "utf-8")
        elif section == "characters":
            self.characters_path.write_text(result, "utf-8")
        elif section == "outline":
            self.outline_path.write_text(result, "utf-8")
        return result

    def _build_scenario_prompt(self, meta: dict) -> str:
        ref = self.get_reference_context()
        ref_block = (f"\n\n{ref}\n\nОриентируйся на стиль, тон и композицию примера.") if ref else ""
        return (
            f"Напиши подробный сценарий/синопсис книги:\n"
            f"Тема: {meta.get('theme', 'без темы')}\n"
            f"Жанр: {meta.get('genre', 'художественная проза')}\n"
            f"Наброски: {meta.get('notes', '—')}\n"
            f"Глав: {meta.get('page_count', 10)}\n\n"
            f"Опиши завязку, повороты, кульминацию, развязку. "
            f"Разбей на {meta.get('page_count', 10)} глав с нумерацией."
            f"{ref_block}"
        )

    def _build_characters_text(self, meta: dict) -> str:
        notes = meta.get("notes", "").strip()
        notes_block = f"\n\nЗаметки и наброски автора (обязательно учти их):\n{notes}" if notes else ""
        return (
            f"Составь список персонажей для темы «{meta.get('theme', '')}»:\n"
            f"- Имя, возраст\n- Роль в сюжете\n- Характер\n- Мотивация\n- Предыстория\n- Внешность\n"
            f"{notes_block}"
        )

    def _build_outline_text(self, meta: dict) -> str:
        notes = meta.get("notes", "").strip()
        notes_block = f"\n\nЗаметки и наброски автора (обязательно учти их):\n{notes}" if notes else ""
        return (
            f"Ровно {meta.get('page_count', 10)} пунктов. Для каждой страницы "
            f"напиши 3-5 предложений. Нумеруй: «Страница 1: ...», «Страница 2: ...»"
            f"{notes_block}"
        )

    def page_file(self, num: int) -> Path:
        return self.pages_dir / f"page_{num:04d}.md"

    def save_page(self, num: int, content: str):
        """Manually save a page (from editor)."""
        meta = self.load_meta()
        total = meta.get("page_count", 1)
        if num < 1 or num > total:
            raise ValueError(f"Глава {num} вне диапазона 1..{total}")
        self.page_file(num).write_text(content, "utf-8")
        meta.setdefault("generated_pages", 0)
        if num > meta["generated_pages"]:
            meta["generated_pages"] = num
        if meta["generated_pages"] >= total:
            meta["finished"] = True
        meta["updated_at"] = datetime.now().isoformat()
        self.save_meta(meta)

    def update_page_count(self, new_count: int) -> dict:
        """Change total number of chapters. Allows expansion."""
        meta = self.load_meta()
        new_count = max(1, min(int(new_count), 500))
        meta["page_count"] = new_count
        if meta.get("generated_pages", 0) > new_count:
            meta["generated_pages"] = new_count
        meta["finished"] = meta.get("generated_pages", 0) >= new_count
        meta["updated_at"] = datetime.now().isoformat()
        self.save_meta(meta)
        return meta

    def _shift_pages_down(self, from_pos: int, to_pos: int):
        """Shift pages [from_pos..to_pos] down by 1 (rename files).
        Iterates from end to start to avoid overwriting."""
        for i in range(to_pos, from_pos - 1, -1):
            src = self.page_file(i)
            dst = self.page_file(i + 1)
            if src.exists():
                src.rename(dst)

    def _shift_pages_up(self, from_pos: int, to_pos: int):
        """Shift pages [from_pos..to_pos] up by 1 (rename files)."""
        for i in range(from_pos, to_pos + 1):
            src = self.page_file(i)
            dst = self.page_file(i - 1)
            if src.exists():
                src.rename(dst)

    def delete_page(self, num: int) -> dict:
        """Delete a chapter and shift all subsequent chapters up by one.
        Auto-renumbers everything. Decrements page_count by 1."""
        meta = self.load_meta()
        total = meta.get("page_count", 1)
        if num < 1 or num > total:
            raise ValueError(f"Глава {num} вне диапазона 1..{total}")
        # Книга без глав невалидна: update_page_count() затем поднял бы
        # page_count до 1, и «удалённая» глава вернулась бы пустой.
        if total <= 1:
            raise ValueError("Нельзя удалить единственную главу")

        pf = self.page_file(num)
        if pf.exists():
            pf.unlink()

        if num < total:
            self._shift_pages_up(num + 1, total)

        meta["page_count"] = total - 1
        gen = meta.get("generated_pages", 0)
        if num <= gen:
            meta["generated_pages"] = max(0, gen - 1)
        meta["finished"] = (meta.get("generated_pages", 0) >= meta["page_count"]) and meta["page_count"] > 0
        meta["updated_at"] = datetime.now().isoformat()
        self.save_meta(meta)
        return meta

    def move_page(self, from_num: int, to_num: int) -> dict:
        """Move chapter from from_num to to_num. Auto-renumbers all affected pages."""
        meta = self.load_meta()
        total = meta.get("page_count", 1)
        if from_num < 1 or from_num > total:
            raise ValueError(f"Исходная глава {from_num} вне диапазона 1..{total}")
        if to_num < 1 or to_num > total:
            raise ValueError(f"Целевая позиция {to_num} вне диапазона 1..{total}")
        if from_num == to_num:
            return meta

        src = self.page_file(from_num)
        had_content = src.exists()
        content = src.read_text("utf-8") if had_content else None

        if from_num < to_num:
            for i in range(from_num + 1, to_num + 1):
                s = self.page_file(i)
                d = self.page_file(i - 1)
                if s.exists():
                    s.rename(d)
        else:
            for i in range(from_num - 1, to_num - 1, -1):
                s = self.page_file(i)
                d = self.page_file(i + 1)
                if s.exists():
                    s.rename(d)

        if had_content:
            self.page_file(to_num).write_text(content, "utf-8")
        else:
            leftover = self.page_file(to_num)
            if leftover.exists():
                leftover.unlink()

        meta["updated_at"] = datetime.now().isoformat()
        self.save_meta(meta)
        return meta

    def insert_page(self, at_position: int, content: str = "") -> dict:
        """Insert a new (empty or with content) chapter at the given position.
        All chapters at >= at_position are shifted down by 1.
        page_count is incremented by 1. Renumbers everything."""
        meta = self.load_meta()
        total = meta.get("page_count", 0)
        if at_position < 1 or at_position > total + 1:
            raise ValueError(f"Позиция {at_position} вне диапазона 1..{total + 1}")

        if total > 0:
            self._shift_pages_down(at_position, total)

        if content:
            self.page_file(at_position).write_text(content, "utf-8")

        meta["page_count"] = total + 1
        gen = meta.get("generated_pages", 0)
        if at_position <= gen:
            meta["generated_pages"] = gen + 1
        meta["finished"] = False
        meta["updated_at"] = datetime.now().isoformat()
        self.save_meta(meta)
        return meta

    def set_reference_book(self, reference_book_id: str = "") -> dict:
        """Attach/detach a reference book (used as style/structure example)."""
        meta = self.load_meta()
        if reference_book_id and reference_book_id != self.id:
            if reference_book_id.startswith("lib_"):
                pass
            else:
                books_root = self.dir.parent
                ref_dir = books_root / reference_book_id
                if not (ref_dir / "meta.json").exists():
                    raise ValueError(f"Книга-пример {reference_book_id} не найдена")
        if reference_book_id:
            meta["reference_book_id"] = reference_book_id
        else:
            meta.pop("reference_book_id", None)
        meta["updated_at"] = datetime.now().isoformat()
        self.save_meta(meta)
        return meta

    def get_reference_context(self, max_chars: int = 1800) -> str:
        """Return context from the reference book based on reference_mode."""
        meta = self.load_meta()
        ref_id = meta.get("reference_book_id", "")
        if not ref_id:
            return ""
        ref_mode = meta.get("reference_mode", "style")

        if ref_id.startswith("lib_"):
            return self._get_library_reference_context(ref_id[4:], max_chars)

        books_root = self.dir.parent
        ref_dir = books_root / ref_id
        if not ref_dir.exists():
            return ""
        try:
            ref_meta = json.loads((ref_dir / "meta.json").read_text("utf-8"))
        except Exception:
            swallow.note('book_generator.get_reference_context:L729')
            return ""
        title = ref_meta.get('title', ref_id)
        parts = []
        if ref_mode == "continuation":
            parts.append(
                f"ПРОДОЛЖЕНИЕ КНИГИ-ОБРАЗЦА «{title}» "
                f"(автор {ref_meta.get('author', '—')}, жанр {ref_meta.get('genre', '—')}):"
            )
            page_files = sorted((ref_dir / "pages").glob("page_*.md")) if (ref_dir / "pages").exists() else []
            for pf in page_files:
                txt = pf.read_text("utf-8")[:max_chars // max(len(page_files), 1)]
                if txt.strip():
                    parts.append(txt)
        elif ref_mode == "custom":
            custom = meta.get("custom_prompt", "")
            if custom:
                parts.append(f"ПОЛЬЗОВАТЕЛЬСКИЙ ПРОМТ:\n{custom}")
            else:
                parts.append(
                    f"ОБРАЗЕЦ СТИЛЯ (книга-образец «{title}», "
                    f"автор {ref_meta.get('author', '—')}, жанр {ref_meta.get('genre', '—')}):"
                )
                for fname, label in (("scenario.md", "Синопсис"), ("outline.md", "План")):
                    f = ref_dir / fname
                    if f.exists():
                        txt = f.read_text("utf-8")[:max_chars // 2]
                        parts.append(f"--- {label} (фрагмент) ---\n{txt}")
        else:
            parts.append(
                f"ПРИМЕР СТИЛЯ/СТРУКТУРЫ (книга-образец «{title}», "
                f"автор {ref_meta.get('author', '—')}, жанр {ref_meta.get('genre', '—')}):"
            )
            for fname, label in (("scenario.md", "Синопсис"), ("outline.md", "План")):
                f = ref_dir / fname
                if f.exists():
                    txt = f.read_text("utf-8")[:max_chars // 2]
                    parts.append(f"--- {label} (фрагмент) ---\n{txt}")
        return "\n\n".join(parts)

    def _get_library_reference_context(self, lib_book_id: str, max_chars: int = 1800) -> str:
        try:
            from modules.collection_module import get_book_content, search_books
            content = get_book_content(lib_book_id, ch_idx=0)
            if isinstance(content, dict):
                text = content.get("text", "") or content.get("content", "") or str(content)
            else:
                text = str(content)
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n[---обрывы---]"
            return f"КНИГА-ОБРАЗЕЦ ИЗ БИБЛИОТЕКИ:\n{text}"
        except Exception:
            swallow.note('book_generator._get_library_reference_context:L781')
            return ""

    def ai_edit_page(self, num: int, instructions: str, model: str, llm: LLMClient,
                     selected_text: str = "") -> str:
        """Edit page using AI. If selected_text given - replaces only that part."""
        pf = self.page_file(num)
        if not pf.exists():
            raise ValueError(f"Глава {num} ещё не создана")
        current = pf.read_text("utf-8")
        meta = self.load_meta()
        system = (
            "Ты — профессиональный писатель и редактор. Редактируй художественный текст "
            "по указаниям автора. Сохраняй стиль, атмосферу и литературность. "
            "Отвечай ТОЛЬКО переработанным текстом, без вступлений и комментариев."
        )

        if selected_text and selected_text in current:
            prompt = (
                f"Контекст всей главы:\n{current}\n\n"
                f"Выделенный фрагмент:\n«{selected_text}»\n\n"
                f"Указания: {instructions}\n\n"
                f"Перепиши ТОЛЬКО выделенный фрагмент с учётом контекста. "
                f"Верни только новый текст фрагмента без кавычек и без вступления."
            )
            new_fragment = llm.chat(
                model,
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                temperature=0.85, max_tokens=2048,
            )
            new_content = current.replace(selected_text, new_fragment.strip(), 1)
        else:
            prompt = (
                f"Жанр: {meta.get('genre', '')}\n"
                f"Тема: {meta.get('theme', '')}\n\n"
                f"Текущий текст главы {num}:\n{current}\n\n"
                f"Указания автора: {instructions}\n\n"
                f"Перепиши главу полностью с учётом указаний. "
                f"Сохрани сюжет, длину (1500-2500 слов). Верни только новый текст без заголовков."
            )
            new_content = llm.chat(
                model,
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                temperature=0.9, max_tokens=4096,
            )

        pf.write_text(new_content, "utf-8")
        meta["updated_at"] = datetime.now().isoformat()
        self.save_meta(meta)
        return new_content

    def _get_context_window(self, num: int, window: int = 5) -> str:
        parts = []
        start = max(1, num - window)
        for i in range(start, num):
            pf = self.page_file(i)
            if pf.exists():
                content = pf.read_text("utf-8")
                preview = content[:200].replace("\n", " ")
                parts.append(f"--- Глава {i} (начало) ---\n{preview}\n")
                if len(content) > 200:
                    last = content[-500:].replace("\n", " ")
                    parts.append(f"--- Глава {i} (концовка) ---\n{last}\n")
        if not parts:
            return ""
        return "Контекст предыдущих глав:\n" + "\n".join(parts)

    def _update_draft(self, num: int, content: str, model: str, llm: LLMClient):
        try:
            existing = self.draft_path.read_text("utf-8") if self.draft_path.exists() else "Черновик пуст."
            prompt = (
                f"Это черновик книги, где мы сохраняем ключевые события, важные детали и "
                f"нити сюжета, чтобы ничего не забыть.\n\n"
                f"Текущий черновик:\n{existing}\n\n"
                f"Только что написана глава {num}:\n{content[:3000]}\n\n"
                f"Обнови черновик: добавь новые ключевые события этой главы, "
                f"важные детали, изменения в персонажах. Сохрани всё важное из старого черновика. "
                f"Формат: короткие тезисы, каждый с новой строки."
            )
            updated = llm.chat(model, [
                {"role": "system", "content": "Ты — ассистент писателя, ведёшь черновик книги. Отвечай коротко, по делу."},
                {"role": "user", "content": prompt},
            ], max_tokens=1500)
            self.draft_path.write_text(updated, "utf-8")
        except Exception:
            swallow.note('book_generator._update_draft:L866')
            pass

    def get_draft_context(self) -> str:
        if self.draft_path.exists():
            return "ЧЕРНОВИК (ключевые события):\n" + self.draft_path.read_text("utf-8")
        return ""

    def get_world_context(self) -> str:
        if self.world_path.exists():
            return "СЛОВАРЬ МИРА:\n" + self.world_path.read_text("utf-8")
        return ""

    def get_study(self) -> str:
        return self.study_path.read_text("utf-8") if self.study_path.exists() else ""

    def save_study(self, text: str):
        self.study_path.write_text(text or "", "utf-8")
        meta = self.load_meta()
        meta["study_ready"] = bool(text and text.strip())
        meta["updated_at"] = datetime.now().isoformat()
        self.save_meta(meta)

    def get_study_context(self, max_chars: int = 24000) -> str:
        study = self.get_study()
        if not study:
            return ""
        if len(study) > max_chars:
            study = study[:max_chars] + "\n\n[---анализ обрезан---]"
        return "ИЗУЧЕНИЕ КНИГИ (анализ для продолжения):\n" + study

    def study_book(self, source_text: str, model: str, llm: LLMClient,
                   chunk_chars: int = 50000, overlap: int = 2000,
                   max_chunks: int | None = None,
                   progress: callable = None) -> str:
        """Изучает полный текст книги скользящим окном chunk_chars символов (по умолчанию
        50 000) с нахлёстом, накопительно обновляя структурированный анализ книги.
        Результат сохраняется в study.md."""
        text = (source_text or "").strip()
        if not text:
            raise ValueError("Текст книги пуст — нечего изучать")
        chunks = split_into_chunks(text, chunk_chars=chunk_chars, overlap=overlap)
        if max_chunks:
            chunks = chunks[:max_chunks]
        if not chunks:
            raise ValueError("Не удалось разбить текст на фрагменты")

        meta = self.load_meta()
        meta["study_chunks_total"] = len(chunks)
        meta["study_progress"] = 0
        meta["study_status"] = "running"
        self.save_meta(meta)

        system = (
            "Ты — литературный аналитик. Ты внимательно читаешь книгу целиком, чтобы затем "
            "написать её правдоподобное продолжение. Ты ведёшь подробный структурированный "
            "анализ: сюжет и ключевые события, незакрытые сюжетные линии и крючки, "
            "персонажи (цели, мотивация, дуги развития, отношения), мир и его правила, "
            "стиль и тон повествования, важные детали и foreshadowing. Отвечай на русском, "
            "тезисно, без воды. Не выдумывай того, чего нет в тексте."
        )
        analysis = ""
        for i, chunk in enumerate(chunks, 1):
            prev_block = (
                f"АНАЛИЗ ПРОЧИТАННОГО РАНЕЕ:\n{analysis}\n\n" if analysis else
                "Это начало книги, прежнего анализа нет.\n\n"
            )
            prompt = (
                f"{prev_block}"
                f"ФРАГМЕНТ КНИГИ {i} из {len(chunks)} (продолжение текста):\n\n{chunk}\n\n"
                f"Задача: обнови общий анализ книги, учтя события этого фрагмента. "
                f"Сохрани всё важное из прежнего анализа (сжимай, но не теряй детали). "
                f"В конце держи раздел «Открытые вопросы и крючки» — всё неразрешённое. "
                f"Формат: короткие тезисы по разделам."
            )
            analysis = llm.chat(model, [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ], temperature=0.6, max_tokens=3000)
            if len(analysis) > 24000:
                analysis = analysis[:8000] + "\n\n[...]\n\n" + analysis[-16000:]
            meta = self.load_meta()
            meta["study_progress"] = i
            self.save_meta(meta)
            if progress:
                progress(i, len(chunks))

        if not analysis or not analysis.strip():
            raise RuntimeError("Изучение книги не дало результата")
        self.study_path.write_text(analysis, "utf-8")
        meta = self.load_meta()
        meta["study_ready"] = True
        meta["study_status"] = "done"
        meta["updated_at"] = datetime.now().isoformat()
        self.save_meta(meta)
        return analysis

    def generate_continuation_plan(self, model: str, llm: LLMClient,
                                   continuation_notes: str = "") -> dict:
        """Строит план продолжения (сценарий/персонажи/план по главам) на основе study.md."""
        study = self.get_study()
        if not study:
            raise RuntimeError("Сначала изучите книгу (study.md пуст)")
        meta = self.load_meta()
        notes_block = (
            f"\n\nПожелания автора по продолжению:\n{continuation_notes}"
            if continuation_notes.strip() else ""
        )
        system = self.SYSTEM_WRITER + " Ты пишешь продолжение существующей книги."

        scenario_prompt = (
            f"ИЗУЧЕНИЕ КНИГИ (полный анализ):\n{study}\n\n"
            f"Требуется продолжение этой книги (жанр: {meta.get('genre', '—')}).{notes_block}\n\n"
            f"Напиши подробный сценарий/синопсис ПРОДОЛЖЕНИЯ: завязка, развитие, повороты, "
            f"кульминация, развязка. Логично продолжай незакрытые линии и крючки из анализа. "
            f"Разбей на {meta.get('page_count', 10)} глав с нумерацией."
        )
        scenario = llm.chat(model, [
            {"role": "system", "content": system},
            {"role": "user", "content": scenario_prompt},
        ], max_tokens=3000)
        self.scenario_path.write_text(scenario, "utf-8")
        meta["scenario_ready"] = True

        characters_prompt = (
            f"ИЗУЧЕНИЕ КНИГИ:\n{study}\n\n"
            f"СЦЕНАРИЙ ПРОДОЛЖЕНИЯ:\n{scenario}\n\n"
            f"Составь список персонажей для продолжения: кого берём из книги (с состоянием "
            f"на конец книги), их роли в продолжении, новые персонажи при необходимости. "
            f"Для каждого: имя, возраст, роль, характер, мотивация, состояние."
        )
        characters = llm.chat(model, [
            {"role": "system", "content": system},
            {"role": "user", "content": characters_prompt},
        ], max_tokens=2500)
        self.characters_path.write_text(characters, "utf-8")
        meta["characters_ready"] = True

        outline_prompt = (
            f"ИЗУЧЕНИЕ КНИГИ:\n{study}\n\n"
            f"СЦЕНАРИЙ ПРОДОЛЖЕНИЯ:\n{scenario}\n\n"
            f"ПЕРСОНАЖИ:\n{characters}\n\n"
            f"Ровно {meta.get('page_count', 10)} пунктов плана по главам. Для каждой главы "
            f"3-5 предложений. Нумеруй: «Глава 1: ...», «Глава 2: ...»"
        )
        outline = llm.chat(model, [
            {"role": "system", "content": system},
            {"role": "user", "content": outline_prompt},
        ], max_tokens=2500)
        self.outline_path.write_text(outline, "utf-8")
        meta["outline_ready"] = True
        meta["updated_at"] = datetime.now().isoformat()
        self.save_meta(meta)
        return {"scenario": scenario, "characters": characters, "outline": outline}

    def generate_world(self, model: str, llm: LLMClient, force: bool = False):
        if not force and self.world_path.exists() and self.world_path.stat().st_size > 0:
            return
        meta = self.load_meta()
        scenario = self.scenario_path.read_text("utf-8") if self.scenario_path.exists() else ""
        characters = self.characters_path.read_text("utf-8") if self.characters_path.exists() else ""
        notes_block = f"Заметки автора:\n{meta.get('notes', '')}\n\n" if meta.get("notes", "").strip() else ""
        prompt = (
            f"Опиши мир книги:\nТема: {meta.get('theme', '')}\nЖанр: {meta.get('genre', '')}\n\n"
            f"{notes_block}"
            f"Сценарий:\n{scenario}\n\nПерсонажи:\n{characters}\n\n"
            f"Опиши: географию, магию/технологии, политику, культуру, историю мира, "
            f"важные локации. Формат: короткие разделы, каждый с заголовком."
        )
        try:
            world = llm.chat(model, [
                {"role": "system", "content": self.SYSTEM_WRITER},
                {"role": "user", "content": prompt},
            ], max_tokens=2500)
            self.world_path.write_text(world, "utf-8")
        except Exception:
            swallow.note('book_generator.generate_world:L1042')
            pass

    def generate_page(self, num: int, model: str, llm: LLMClient, force: bool = False) -> str:
        meta = self.load_meta()
        total = meta.get("page_count", 10)

        if not force and self.page_file(num).exists():
            return self.page_file(num).read_text("utf-8")

        system = (
            "Ты — профессиональный писатель. Пиши на русском, литературно. "
            "Длина страницы — 1500–2500 слов."
        )

        scenario = self.scenario_path.read_text("utf-8") if self.scenario_path.exists() else ""
        characters = self.characters_path.read_text("utf-8") if self.characters_path.exists() else ""
        outline = self.outline_path.read_text("utf-8") if self.outline_path.exists() else ""
        context_window = self._get_context_window(num, window=5)
        draft_context = self.get_draft_context()
        world_context = self.get_world_context()
        study_context = self.get_study_context()
        notes = meta.get("notes", "").strip()

        custom_prompt = meta.get("custom_prompt", "")
        ref_mode = meta.get("reference_mode", "style")
        ref = self.get_reference_context()

        if custom_prompt and ref_mode == "custom":
            prompt = custom_prompt.format(
                num=num, total=total,
                theme=meta.get("theme", ""),
                genre=meta.get("genre", ""),
                scenario=scenario,
                characters=characters,
                outline=outline,
                prev_context=context_window,
                ref=ref,
                draft=draft_context,
                world=world_context,
            )
        else:
            prompt = (
                f"Напиши ГЛАВУ {num} из {total}.\n\n"
                f"Тема: {meta.get('theme', '')}\nЖанр: {meta.get('genre', '')}\n\n"
            )
            if notes:
                prompt += f"Заметки и пожелания автора:\n{notes}\n\n"
            prompt += (
                f"Сценарий:\n{scenario}\n\n"
                f"Персонажи:\n{characters}\n\n"
                f"План:\n{outline}\n\n"
            )
            if study_context:
                prompt += f"{study_context}\n\n"
            if world_context:
                prompt += f"{world_context}\n\n"
            if draft_context:
                prompt += f"{draft_context}\n\n"
            if ref:
                prompt += f"{ref}\n\n"
            if context_window:
                prompt += f"{context_window}\n\n"
            prompt += (
                f"Напиши ТОЛЬКО текст главы {num} без заголовков. "
                f"Художественный стиль, диалоги, описания."
            )

        for attempt in range(3):
            try:
                content = llm.chat(model, [{"role": "system", "content": system}, {"role": "user", "content": prompt}], temperature=0.9 if attempt == 0 else 0.95, max_tokens=4096)
                word_count = len(content.split())
                if word_count >= 50:
                    break
            except Exception:
                if attempt == 2:
                    raise
                continue
        self.page_file(num).write_text(content, "utf-8")

        self._update_draft(num, content, model, llm)

        meta.setdefault("generated_pages", 0)
        meta["generated_pages"] = max(meta["generated_pages"], num)
        if num >= total:
            meta["finished"] = True
        meta["updated_at"] = datetime.now().isoformat()
        self.save_meta(meta)
        return content

    def export_fb2(self) -> Path:
        meta = self.load_meta()
        title = meta.get("title", "Без названия")
        author_name = meta.get("author", "Нейросеть")
        genre = self._normalize_genre(meta.get("genre", "prose"))
        date_str = datetime.now().strftime("%Y-%m-%d")
        book_id = meta.get("id", self.id)

        sections = ""
        for i in range(1, meta.get("page_count", 1) + 1):
            pf = self.page_file(i)
            if pf.exists():
                content = pf.read_text("utf-8")
                paragraphs = "\n".join(
                    f"<p>{html_module.escape(p.strip())}</p>"
                    for p in content.split("\n\n") if p.strip()
                )
                sections += f'<section><title><p>Глава {i}</p></title>{paragraphs}</section>\n'

        parts = author_name.split(" ", 1)
        first_name = html_module.escape(parts[0])
        last_name = html_module.escape(parts[1]) if len(parts) > 1 else ""

        fb2 = f'''<?xml version="1.0" encoding="UTF-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"
             xmlns:l="http://www.w3.org/1999/xlink">
  <description>
    <title-info>
      <genre>{html_module.escape(genre)}</genre>
      <author>
        <first-name>{first_name}</first-name>
        <last-name>{last_name}</last-name>
      </author>
      <book-title>{html_module.escape(title)}</book-title>
      <date>{date_str}</date>
      <lang>ru</lang>
    </title-info>
    <document-info>
      <author><nickname>Book Generator</nickname></author>
      <program-used>Infinity Book Generator</program-used>
      <date>{date_str}</date>
      <version>1.0</version>
    </document-info>
  </description>
  <body>
    <title><p>{html_module.escape(title)}</p></title>
    {sections}
  </body>
</FictionBook>'''

        self.fb2_path.write_text(fb2, "utf-8")
        return self.fb2_path

    def _normalize_genre(self, genre: str) -> str:
        genre_map = {
            "фантастика": "sci_fi",
            "научная фантастика": "sci_fi",
            "космическая фантастика": "sf_space",
            "киберпанк": "sf_cyberpunk",
            "постапокалипсис": "sf_postapocalyptic",
            "антиутопия": "sf_social",
            "стимпанк": "sf_steampunk",
            "фэнтези": "fantasy",
            "тёмное фэнтези": "sf_fantasy_dark",
            "героическое фэнтези": "sf_heroic",
            "городское фэнтези": "sf_fantasy_city",
            "магический реализм": "prose_magic",
            "детектив": "detective",
            "классический детектив": "det_classic",
            "иронический детектив": "det_irony",
            "политический детектив": "det_political",
            "шпионский детектив": "det_espionage",
            "роман": "prose",
            "любовный роман": "love",
            "исторический роман": "love_history",
            "современный любовный роман": "love_contemporary",
            "триллер": "thriller",
            "психологический триллер": "thriller_psychological",
            "юридический триллер": "thriller_legal",
            "медицинский триллер": "thriller_medical",
            "хоррор": "horror",
            "мистика": "horror_mystic",
            "ужасы и мистика": "horror",
            "историческая проза": "historical",
            "военная проза": "prose_military",
            "приключения": "adventure",
            "морские приключения": "adv_maritime",
            "приключения для детей": "child_adv",
            "вестерн": "adv_western",
            "художественная проза": "prose",
            "современная проза": "prose_contemporary",
            "классическая проза": "prose_classic",
            "юмор и сатира": "humor",
            "молодёжная проза": "child_prose",
            "сказка": "child_tale",
            "детская литература": "children",
            "поэзия": "poetry",
            "пьеса": "dramaturgy",
            "научпоп": "science",
            "биография": "nonf_biography",
            "публицистика": "nonf_publicism",
            "эзотерика": "religion_esoterics",
            "религия": "religion",
            "эпопея": "sf_epic",
        }
        return genre_map.get(genre.lower().strip().replace("  ", " "), "prose")

    def export_txt(self) -> Path:
        meta = self.load_meta()
        with open(self.txt_path, "w", encoding="utf-8") as f:
            f.write(f"{meta.get('title', 'Без названия')}\n")
            f.write(f"Автор: {meta.get('author', '—')}\n")
            f.write("=" * 40 + "\n\n")
            for i in range(1, meta.get("page_count", 1) + 1):
                pf = self.page_file(i)
                if pf.exists():
                    f.write(f"\n--- Глава {i} ---\n\n")
                    f.write(pf.read_text("utf-8"))
                    f.write("\n")
        return self.txt_path

    def export_md(self) -> Path:
        meta = self.load_meta()
        with open(self.md_path, "w", encoding="utf-8") as f:
            f.write(f"# {meta.get('title', 'Без названия')}\n\n")
            f.write(f"*{meta.get('author', '—')}*\n\n---\n\n")
            for i in range(1, meta.get("page_count", 1) + 1):
                pf = self.page_file(i)
                if pf.exists():
                    f.write(f"## Глава {i}\n\n")
                    f.write(pf.read_text("utf-8"))
                    f.write("\n\n---\n\n")
        return self.md_path

    def export_plan(self) -> Path:
        plan_path = self.dir / "plan.md"
        data = (
            f"# СЦЕНАРИЙ\n\n{self.scenario_path.read_text('utf-8') if self.scenario_path.exists() else ''}\n\n"
            f"# ПЕРСОНАЖИ\n\n{self.characters_path.read_text('utf-8') if self.characters_path.exists() else ''}\n\n"
            f"# ПЛАН\n\n{self.outline_path.read_text('utf-8') if self.outline_path.exists() else ''}"
        )
        plan_path.write_text(data, "utf-8")
        return plan_path

    def delete(self):
        if self.dir.exists():
            shutil.rmtree(self.dir)

    def save_to_library(self, library_dir: Path) -> Path:
        """Save FB2 file directly into the books library directory (configured books_dir)."""
        library_dir = Path(library_dir)
        library_dir.mkdir(parents=True, exist_ok=True)
        meta = self.load_meta()
        title = meta.get("title", "Без названия")
        author = meta.get("author", "Автор")
        # Generate safe filename
        safe = "".join(c if (c.isalnum() or c in " _-") else "_" for c in f"{author} — {title}")
        safe = safe.strip().replace("__", "_")[:120] or "book"
        target = library_dir / f"{safe}.fb2"
        # Avoid overwrite
        n = 1
        while target.exists():
            target = library_dir / f"{safe} ({n}).fb2"
            n += 1
        # Always regenerate to make sure it's fresh
        src = self.export_fb2()
        target.write_bytes(src.read_bytes())
        meta.setdefault("library_exports", [])
        meta["library_exports"].append({
            "path": str(target),
            "date": datetime.now().isoformat(),
        })
        self.save_meta(meta)
        return target

    def proofread(self, model: str, llm: LLMClient) -> list:
        meta = self.load_meta()
        total = meta.get("page_count", 0)
        scenario = self.scenario_path.read_text("utf-8") if self.scenario_path.exists() else ""
        characters = self.characters_path.read_text("utf-8") if self.characters_path.exists() else ""
        draft = self.draft_path.read_text("utf-8") if self.draft_path.exists() else ""
        all_issues = []

        for i in range(1, total + 1):
            pf = self.page_file(i)
            if not pf.exists():
                continue
            content = pf.read_text("utf-8")
            prompt = (
                f"Ты — профессиональный редактор. Проверь главу {i} на:\n"
                f"1. Логические ошибки и несоответствия\n"
                f"2. Противоречия с сюжетом и персонажами\n"
                f"3. Стилистические проблемы\n"
                f"4. Фактические ошибки\n\n"
                f"Сценарий:\n{scenario}\n\n"
                f"Персонажи:\n{characters}\n\n"
                f"Черновик:\n{draft}\n\n"
                f"Текст главы {i}:\n{content}\n\n"
                f"Если есть проблемы, для каждой напиши:\n"
                f"=== ПРОБЛЕМА ===\n"
                f"Тип: (логика/сюжет/персонаж/стиль/факт)\n"
                f"Серьёзность: (высокая/средняя/низкая)\n"
                f"Описание: ...\n"
                f"Оригинал: (цитата из текста)\n"
                f"Исправление: (предлагаемый новый текст)\n"
                f"=== КОНЕЦ ===\n\n"
                f"Если проблем нет, напиши 'ОК'."
            )
            try:
                result = llm.chat(model, [
                    {"role": "system", "content": "Ты — строгий литературный редактор. Анализируй текст и находи проблемы."},
                    {"role": "user", "content": prompt},
                ], max_tokens=3000)
            except Exception:
                swallow.note('book_generator.proofread:L1346')
                continue

            if result.strip() == "ОК":
                continue

            issues = self._parse_proofread_result(result, i)
            self._save_proofreads(i, issues)
            all_issues.extend(issues)

        return all_issues

    def _parse_proofread_result(self, text: str, page_num: int) -> list:
        issues = []
        blocks = text.split("=== ПРОБЛЕМА ===")
        for block in blocks:
            if "=== КОНЕЦ ===" not in block:
                continue
            inner = block.split("=== КОНЕЦ ===")[0].strip()
            lines = inner.split("\n")
            issue = {"page": page_num, "id": str(uuid.uuid4())[:8], "status": "pending"}
            for line in lines:
                if line.startswith("Тип:"):
                    issue["type"] = line.split(":", 1)[1].strip()
                elif line.startswith("Серьёзность:"):
                    issue["severity"] = line.split(":", 1)[1].strip()
                elif line.startswith("Описание:"):
                    issue["description"] = line.split(":", 1)[1].strip()
                elif line.startswith("Оригинал:"):
                    issue["original_text"] = line.split(":", 1)[1].strip()
                elif line.startswith("Исправление:"):
                    issue["suggested_text"] = line.split(":", 1)[1].strip()
            if "description" in issue:
                issues.append(issue)
        return issues

    def _save_proofreads(self, page_num: int, issues: list):
        path = self.proofreads_dir / f"page_{page_num:04d}.json"
        path.write_text(json.dumps(issues, ensure_ascii=False, indent=2), "utf-8")

    def load_proofreads(self) -> list:
        all_issues = []
        for f in sorted(self.proofreads_dir.glob("page_*.json")):
            try:
                issues = json.loads(f.read_text("utf-8"))
                all_issues.extend(issues)
            except Exception:
                swallow.note('book_generator.load_proofreads:L1393')
                pass
        return all_issues

    def apply_proofread(self, issue_id: str) -> bool:
        for f in self.proofreads_dir.glob("page_*.json"):
            try:
                issues = json.loads(f.read_text("utf-8"))
                for issue in issues:
                    if issue.get("id") == issue_id and issue.get("status") == "pending":
                        page_num = issue.get("page")
                        suggested = issue.get("suggested_text", "")
                        original = issue.get("original_text", "")
                        if page_num and suggested and original:
                            pf = self.page_file(page_num)
                            if pf.exists():
                                content = pf.read_text("utf-8")
                                if original in content:
                                    pf.write_text(content.replace(original, suggested, 1), "utf-8")
                                    issue["status"] = "accepted"
                                    f.write_text(json.dumps(issues, ensure_ascii=False, indent=2), "utf-8")
                                    return True
            except Exception:
                swallow.note('book_generator.apply_proofread:L1416')
                pass
        return False

    def reject_proofread(self, issue_id: str) -> bool:
        for f in self.proofreads_dir.glob("page_*.json"):
            try:
                issues = json.loads(f.read_text("utf-8"))
                for issue in issues:
                    if issue.get("id") == issue_id and issue.get("status") == "pending":
                        issue["status"] = "rejected"
                        f.write_text(json.dumps(issues, ensure_ascii=False, indent=2), "utf-8")
                        return True
            except Exception:
                swallow.note('book_generator.reject_proofread:L1430')
                pass
        return False

    def get_page_count_status(self) -> dict:
        meta = self.load_meta()
        existing_pages = []
        for i in range(1, meta.get("page_count", 1) + 1):
            pf = self.page_file(i)
            if pf.exists():
                txt = pf.read_text("utf-8")
                existing_pages.append({
                    "num": i,
                    "size": len(txt),
                    "words": len(txt.split()),
                    "exists": True,
                })
            else:
                existing_pages.append({"num": i, "exists": False, "size": 0, "words": 0})
        return {
            "page_count": meta.get("page_count", 0),
            "generated_pages": meta.get("generated_pages", 0),
            "pages": existing_pages,
        }


class BookGenerator:
    def __init__(
        self,
        books_dir: str | Path | None = None,
        lm_url: str | None = None,
        lm_model: str | None = None,
        lm_api_key: str | None = None,
        config_path: str | None = None,
    ):
        self.config = load_app_config(config_path)
        self.books_dir = Path(books_dir) if books_dir else get_books_dir(self.config)
        self.books_dir.mkdir(parents=True, exist_ok=True)

        _url, _model, _api_key = get_lm_config(self.config)
        lm_url = lm_url or _url
        self.model = lm_model or _model
        lm_api_key = lm_api_key or _api_key
        self.llm = LLMClient(base_url=lm_url, api_key=lm_api_key)

    def create_book(
        self,
        title: str = "Без названия",
        author: str = "Автор",
        theme: str = "",
        genre: str = "художественная проза",
        notes: str = "",
        page_count: int = 10,
        model: str = "",
        reference_book_id: str = "",
    ) -> Book:
        book_id = str(uuid.uuid4())[:8]
        book = Book(book_id, self.books_dir)

        meta = {
            "id": book_id,
            "title": title,
            "author": author,
            "theme": theme,
            "genre": genre,
            "notes": notes,
            "page_count": max(1, min(page_count, 200)),
            "model": model or self.model,
            "created_at": datetime.now().isoformat(),
            "scenario_ready": False,
            "characters_ready": False,
            "outline_ready": False,
            "generated_pages": 0,
            "finished": False,
        }
        if reference_book_id and reference_book_id != book_id:
            if reference_book_id.startswith("lib_") or (self.books_dir / reference_book_id / "meta.json").exists():
                meta["reference_book_id"] = reference_book_id
        book.save_meta(meta)
        return book

    def continue_book(
        self,
        source: str,
        title: str = "",
        author: str = "",
        theme: str = "",
        genre: str = "художественная проза",
        notes: str = "",
        page_count: int = 10,
        model: str = "",
        chunk_chars: int = 50000,
        max_chunks: int | None = None,
        progress: callable = None,
    ) -> Book:
        """Создаёт книгу-продолжение: изучает источник целиком (скользящим окном
        ~chunk_chars, по умолчанию 50 000 символов) и строит план продолжения."""
        model = model or self.model
        text, src_title, src_author = extract_book_text(source, self.books_dir)
        title = (title or src_title or "Продолжение книги").strip()
        author = (author or src_author or "Нейросеть").strip()
        theme = (theme or title).strip()
        book = self.create_book(
            title=title, author=author, theme=theme, genre=genre,
            notes=notes, page_count=page_count, model=model,
        )
        meta = book.load_meta()
        meta["continuation_source"] = source
        meta["study_source_title"] = src_title or ""
        meta["study_source_author"] = src_author or ""
        book.save_meta(meta)
        book.study_book(text, model, self.llm, chunk_chars=chunk_chars,
                        max_chunks=max_chunks, progress=progress)
        book.generate_continuation_plan(model, self.llm, continuation_notes=notes)
        return book

    def get_book(self, book_id: str) -> Book | None:
        # ID приходит из URL/JSON. Без проверки `../..` выводил за пределы
        # каталога книг, а delete_book() удалял бы чужую папку.
        if not book_id or not isinstance(book_id, str):
            return None
        if book_id != Path(book_id).name or book_id in (".", ".."):
            return None
        book = Book(book_id, self.books_dir)
        if book.meta_path.exists():
            return book
        return None

    def list_books(self) -> list:
        books = []
        for d in self.books_dir.iterdir():
            if d.is_dir() and (d / "meta.json").exists():
                try:
                    meta = json.loads((d / "meta.json").read_text("utf-8"))
                    books.append({"id": d.name, **meta})
                except (json.JSONDecodeError, IOError):
                    swallow.note('book_generator.list_books:L1566')
                    continue
        books.sort(key=lambda b: b.get("created_at", ""), reverse=True)
        return books

    def delete_book(self, book_id: str) -> bool:
        book = self.get_book(book_id)
        if book:
            book.delete()
            return True
        return False

    def list_models(self) -> list:
        return self.llm.list_models()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Book Generator")
    parser.add_argument("--books-dir", help="Директория для книг")
    parser.add_argument("--lm-url", help="URL LM Studio")
    parser.add_argument("--list-models", action="store_true", help="Список моделей")
    parser.add_argument("--list-books", action="store_true", help="Список книг")
    args = parser.parse_args()

    gen = BookGenerator(books_dir=args.books_dir, lm_url=args.lm_url)

    if args.list_models:
        models = gen.list_models()
        print("Доступные модели:")
        for m in models:
            print(f"  - {m}")
    elif args.list_books:
        books = gen.list_books()
        print(f"Книг найдено: {len(books)}")
        for b in books:
            status = "Готово" if b.get("finished") else f"{b.get('generated_pages', 0)}/{b.get('page_count', '?')}"
            print(f"  [{b['id']}] {b.get('title', 'Без названия')} — {status}")
    else:
        print("Book Generator готов к работе.")
        print(f"Директория книг: {gen.books_dir}")
        print(f"LM Studio: {gen.llm.base_url}")
