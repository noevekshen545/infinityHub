"""Секция 'BOOK AI CHAT (РЕЖИМЫ)' монолита `app/server_backup.py`.

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
import swallow
# ===================== BOOK AI CHAT (РЕЖИМЫ) =====================

_book_bm25_cache = {}

def _build_book_bm25_index(book_path, chapters):
    """Создать in-memory BM25 индекс из глав книги."""
    mtime = "collection"
    if os.path.isfile(book_path):
        try:
            mtime = str(os.path.getmtime(book_path))
        except Exception:
            swallow.note('book_ai_chat._build_book_bm25_index:L36')
            pass
    cache_key = f"{book_path}:{mtime}"
    if cache_key in _book_bm25_cache:
        return _book_bm25_cache[cache_key]
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
        _book_bm25_cache[cache_key] = (bm25, chapter_texts, chapter_meta)
        return bm25, chapter_texts, chapter_meta
    except Exception:
        swallow.note('book_ai_chat._build_book_bm25_index:L58')
        return None, [], []


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
        if extract_best_passages and full_text:
            context = extract_best_passages(full_text, question, max_passages=10, max_chars_per_passage=2000)
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

        analysis_result = lm_studio_request([
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
            synthesis = lm_studio_request([
                {"role": "system", "content": "Кратко суммируй информацию по запросу из текста книги."},
                {"role": "user", "content": f"Запрос: {sq}\n\nТекст книги:\n{findings}\n\nРезюме:"}
            ], max_tokens=512, model=model, temperature=0.3)
            research_data.append({"query": sq, "findings": synthesis.get("content", "")})

        # Этап 3: финальный отчёт
        final_context = f"# Исследование по книге: {book_title}\n\n## Вопрос: {question}\n\n"
        for rd in research_data:
            final_context += f"## Подзапрос: {rd['query']}\n{rd['findings']}\n\n---\n\n"
        final_context += "\nНа основе собранных данных составь подробный ответ на исходный вопрос."

        result = lm_studio_request([
            {"role": "system", "content": "Ты аналитик. Составляй подробные ответы на основе исследования книги."},
            {"role": "user", "content": final_context}
        ], max_tokens=8192, model=model, temperature=temperature)

        answer = result.get("content", "")
        reasoning = result.get("reasoning", "")
        ai_save_answer(question, answer, f"full_study по книге {book_title}")
        return {"answer": answer, "reasoning": reasoning, "research_steps": research_data}

    # Стандартный поток для chapters / rag / smart_truncate / free
    prev_answers = ai_search_answers(question)
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
    result = lm_studio_request(messages, max_tokens=4096, model=model, temperature=temperature)
    answer = result.get("content", "")
    reasoning = result.get("reasoning", "")
    ai_save_answer(question, answer, f"{mode} по книге {book_title}")
    return {"answer": answer, "reasoning": reasoning}



# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'ai_save_answer',
    'ai_search_answers',
    'extract_best_passages',
    'get_book_content',
    'lm_studio_request',
    'os',
    're',
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
