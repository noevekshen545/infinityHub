#!/usr/bin/env python3
"""Режим «глубокое исследование» (/api/ask-stream, /api/ask-database, mode=research).

Реализация лежала в do_POST внутри server_backup.py и никогда не исполнялась:
оба пути стоят в route_dispatch._EXACT, поэтому их разбирает FastAPI-хэндлер
из main.py, а ветки research в нём не было — режим молча превращался в обычный
ответ с одним поисковым контекстом.

Сюда вынесен сам конвейер (план → поиск по подзапросам → отчёт) без привязки к
HTTP: сетевые вызовы приходят в виде deps.complete/deps.search/deps.stream,
поэтому последовательность событий проверяется офлайн-тестом на заглушках.
"""

import logging

logger = logging.getLogger("infinity.deep_research")

# Клиент page_ask.html отдаёт ползунок 5..30 и по умолчанию просит 15.
DEFAULT_ITERATIONS = 15
MAX_ITERATIONS = 30
# Подзапрос короче этого порога — обрывок формата («1.»), а не вопрос.
MIN_SUBQUERY_LEN = 10
SEARCH_LIMIT = 5
# Один шаг исследования не должен занимать больше этого объёма контекста:
# 35 000 токенов у маленькой модели — потолок, а подзапросов до 30.
STEP_CONTEXT_MAX = 8000
# Список источников под отчётом: столько же, сколько показывал старый путь.
MAX_FINAL_SOURCES = 10

ANALYSIS_SYSTEM = "Ты исследовательский ассистент. Разбивай вопросы на подзапросы."
ANALYSIS_TEMPLATE = """Проанализируй вопрос и разбей его на ключевые аспекты, по которым нужно провести исследование.
Вопрос: {question}

Выдели {count} подзапросов, на которые нужно найти ответы для полного раскрытия темы.
Верни ответ в виде нумерованного списка, каждый подзапрос - одно предложение.

Пример формата:
1. Кто такой Пушкин - основная информация
2. Биография Пушкина - годы жизни, место рождения
3. Творчество Пушкина - основные произведения
"""

SYNTH_SYSTEM = "Ты исследователь. Кратко суммируй найденную информацию по запросу."
SYNTH_TEMPLATE = """Запрос: {query}

Найденная информация:
{context}

Краткое резюме:"""

FINAL_SYSTEM = ("Ты ведущий аналитик. Составляешь подробные исследовательские отчёты "
                "на русском языке. Используй всю доступную информацию.")
FINAL_TEMPLATE = ("# Глубокое исследование по теме: {question}\n\n"
                  "## Собранные данные:\n\n{data}\n\n"
                  "## Задание\nНа основе собранных данных составь подробный аналитический "
                  "отчёт. Ответь на русском языке. Включи все найденные факты, даты, имена. "
                  "Структурируй документ.")

REPORT_PREFIX = "📊 Исследование: "


def clamp_iterations(value, default=DEFAULT_ITERATIONS):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return 1
    return min(n, MAX_ITERATIONS)


def _research_cfg():
    """Секция research из конфига: «Настройки → Исследовательский режим».

    Клиент может не присылать модели и лимиты (старая вкладка с закешированной
    страницей, сторонний вызов API) — тогда значения берутся отсюда, а не из
    зашитых в код gemma-3-4b-it / 20000.
    """
    try:
        from config_module import get_config
        return get_config().get("research") or {}
    except Exception:
        return {}


def parse_sub_queries(text, limit, fallback_question):
    """Нумерованный список из ответа модели в чистый список подзапросов.

    Пустой или неразобранный ответ не считается ошибкой: исследование с одним
    исходным вопросом полезнее, чем молчаливый отказ."""
    queries = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line[0].isdigit() or line[0] in "-•":
            q = line.lstrip("1234567890.-•) ").strip()
            if len(q) > MIN_SUBQUERY_LEN:
                queries.append(q)
        if len(queries) >= limit:
            break
    if not queries:
        queries = [fallback_question]
    return queries[:limit]


def format_results(results):
    """Контекст шага — ровно та же разметка, что в обычном режиме /api/ask-stream."""
    parts = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        rtype = r.get("type", "jsonl")
        preview = r.get("preview", "") or ""
        if rtype == "zim" and r.get("full_content"):
            preview = r["full_content"][:1000]
        if rtype == "zim":
            parts.append("[ZIM: {0}]\n{1}".format(r.get("title", ""), preview))
        elif rtype == "note":
            parts.append("[Заметка: {0}]\n{1}".format(r.get("title", ""), preview))
        elif rtype == "ai_answer":
            parts.append("[AI память: {0}]\n{1}".format(r.get("title", ""), preview))
        elif rtype == "book":
            parts.append("[Книга: {0}]\n{1}".format(r.get("file", ""), preview))
        else:
            parts.append("[{0} #{1}] {2}: {3}".format(
                r.get("file", ""), r.get("index", ""), r.get("title", ""), preview))
    text = "\n\n".join(parts)
    return text[:STEP_CONTEXT_MAX] if text else "Нет данных в базе"


def source_key(r):
    return (r.get("file", ""), r.get("index", ""), r.get("type", ""), r.get("note_id", ""))


def dedupe_sources(collected, limit=MAX_FINAL_SOURCES):
    """Поиск по разным подзапросам возвращает одни и те же документы."""
    seen = set()
    out = []
    for r in collected:
        if not isinstance(r, dict):
            continue
        k = source_key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def render_data(steps):
    chunks = []
    for st in steps:
        block = "### Запрос: {0}\n{1}\n".format(st["query"], st["findings"])
        srcs = [s for s in st.get("sources", []) if s]
        if srcs:
            block += "Источники: {0}\n".format(", ".join(srcs))
        chunks.append(block)
    return "\n---\n\n".join(chunks)


def fallback_report(question, steps):
    """Отчёт модели пуст или сеть отвалилась — отдаём собранное как есть."""
    lines = [REPORT_PREFIX + question, ""]
    for st in steps:
        lines.append("• {0}".format(st["query"]))
        if st["findings"]:
            lines.append("  {0}".format(st["findings"].replace("\n", " ").strip()))
    if not steps:
        lines.append("Данных найти не удалось.")
    return "\n".join(lines).strip()


def run(question, deps, *, research_model=None, final_model=None, temperature=0.7,
        max_final_tokens=20000, iterations=None):
    """Синхронный генератор событий SSE-контракта режима research.

    Каждый next() блокирует поток вызовом LLM — вызывающий код выводит его в
    thread pool (main._HANDLER_POOL) и отдаёт событие в стрим. Блокирующие
    сетевые вызовы живут в deps, сам конвейер от транспорта не зависит."""
    rc = _research_cfg()
    limit = clamp_iterations(iterations, rc.get("iterations") or DEFAULT_ITERATIONS)
    plan_model = research_model or rc.get("research_model") or None
    final_model = final_model or rc.get("final_model") or None
    try:
        max_final_tokens = int(max_final_tokens)
    except (TypeError, ValueError):
        max_final_tokens = 0
    if max_final_tokens <= 0:
        try:
            max_final_tokens = int(rc.get("max_final_tokens") or 20000)
        except (TypeError, ValueError):
            max_final_tokens = 20000

    yield {"research_step": "analysis"}
    planned = deps.complete(
        [{"role": "system", "content": ANALYSIS_SYSTEM},
         {"role": "user", "content": ANALYSIS_TEMPLATE.format(question=question, count=limit)}],
        model=plan_model, max_tokens=2048, temperature=0.5, context_length=35000)
    sub_queries = parse_sub_queries((planned or {}).get("content", ""), limit, question)
    logger.info("Исследование: %s подзапросов", len(sub_queries))

    yield {"research_step": "analysis_done", "sub_queries": sub_queries}

    steps = []
    collected = []
    total = len(sub_queries)
    for i, sq in enumerate(sub_queries, 1):
        yield {"research_step": "search", "step": i, "query": sq}
        try:
            results = deps.search(sq) or []
        except Exception as e:
            logger.warning("Исследование [%s/%s]: поиск упал: %s", i, total, e)
            results = []
        collected.extend(results)
        context = format_results(results)
        synthesized = deps.complete(
            [{"role": "system", "content": SYNTH_SYSTEM},
             {"role": "user", "content": SYNTH_TEMPLATE.format(query=sq, context=context)}],
            model=plan_model, max_tokens=512, temperature=0.3, context_length=35000)
        findings = ((synthesized or {}).get("content") or "").strip()
        src_names = [(r.get("file") or r.get("title") or "") for r in results[:3]]
        steps.append({"query": sq, "findings": findings, "sources": src_names,
                      "step": i, "total": total})
        yield {"research_step": "findings", "step": i, "query": sq, "findings": findings,
               "sources_count": len(results), "total": total}

    yield {"research_step": "report"}
    final_messages = [
        {"role": "system", "content": FINAL_SYSTEM},
        {"role": "user", "content": FINAL_TEMPLATE.format(
            question=question, data=render_data(steps))},
    ]
    full = ""
    for delta in deps.stream(final_messages, model=final_model or plan_model,
                             max_tokens=int(max_final_tokens), temperature=temperature,
                             context_length=int(max_final_tokens) + 5000):
        if not delta:
            continue
        full += delta
        yield {"research_step": "report_stream", "delta": delta}

    sources = dedupe_sources(collected)
    if not full.strip():
        full = fallback_report(question, steps)
        yield {"research_step": "report_stream", "delta": full}

    # Финальное событие то же, что у обычного режима, — клиент подставляет в
    # него session_id и рисует панель источников. session_id дописывает main.py:
    # идентификатор сессии знает только он. research_steps здесь для не-стримового
    # пути (файл/картинка в прикреплении), где клиент ждёт массив этапов.
    yield {"done": True, "type": "done", "delta": full, "sources": sources,
           "reasoning": "", "research_steps": steps}
