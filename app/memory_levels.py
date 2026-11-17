"""
memory_levels.py — трёхуровневая память агента (Фаза 3 рефакторинга).

Уровни:
  L1 СЕССИЯ    — краткие итоги задач текущей сессии (session["task_summaries"])
                 + сеансовый RAG (rag_recall фильтрует по session_id).
  L2 ОБЩАЯ     — кросс-сессиональная семантическая память: ОТДЕЛЬНАЯ коллекция
                 Qdrant "agent_memory_global". Видна в веб-интерфейсе /qdrant,
                 но НЕ индексируется поисковой системой (поиск работает только
                 с коллекциями search_embeddings / search_all_semantic).
                 Фолбэк: JSONL-файл + поиск по ключевым словам.
  L3 ЯВНЫЕ ФАКТЫ — то, что агент записал сам через memory_store
                 ("меня зовут...", "я живу в...") → core_memory.json,
                 всегда имеют высший приоритет при формировании контекста.

Приоритет инъекции в промпт: L3 (всегда) → L1 (итоги задач сессии) →
L2 (релевантное из общей памяти по запросу).
"""

import os
import re
import json
import time
import logging
import swallow

logger = logging.getLogger("infinity")

# Коллекция глобальной памяти агента (не пересекается с поисковыми)
GLOBAL_COLLECTION = "agent_memory_global"

try:
    MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "agent_memory")
except Exception:
    swallow.note('memory_levels.<module>:L34')
    MEMORY_DIR = os.path.join(os.path.expanduser("~"), ".infinity", "agent_memory")
GLOBAL_JSONL = os.path.join(MEMORY_DIR, "global_memory.jsonl")


# ============================================================
# L2 — ОБЩАЯ ПАМЯТЬ (Qdrant + JSONL-фолбэк)
# ============================================================

def _qdrant_available():
    try:
        import qdrant_module as qm
        return qm.get_client() is not None
    except Exception:
        swallow.note('memory_levels._qdrant_available:L48')
        return False


def remember_global(text, kind="fact", session_id="", title=""):
    """Записать факт/итог в общую память (L2). kind: fact|task|lesson|preference."""
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
    meta = {"kind": kind, "session": str(session_id or ""), "time": time.time()}
    stored = {"qdrant": False}

    if _qdrant_available():
        try:
            import qdrant_module as qm
            qm.ensure_collection(GLOBAL_COLLECTION)
            res = qm.add_texts(GLOBAL_COLLECTION, [{
                "source": "agent_memory",
                "id": "mem_%s_%s" % (int(time.time() * 1000), abs(hash(text)) % 99999),
                "title": title or ("%s: %s" % (kind, text[:60])),
                "text": text[:4000],
                "extra": meta,
            }])
            stored["qdrant"] = res.get("indexed_items", 0) > 0
            if not stored["qdrant"]:
                logger.warning("[MEMORY-L2] qdrant upsert пустой: %s" % res.get("last_error"))
        except Exception as e:
            logger.warning("[MEMORY-L2] qdrant store failed: %s" % e)

    # JSONL — всегда (надёжный дубликат/фолбэк)
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        with open(GLOBAL_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps({"text": text[:4000], **meta}, ensure_ascii=False) + "\n")
        stored["file"] = True
    except Exception as e:
        logger.warning("[MEMORY-L2] jsonl store failed: %s" % e)

    return {"ok": True, **stored}


def recall_global(query, top_k=4):
    """Семантический поиск по общей памяти. Возвращает [{text, score, kind}]"""
    if not query:
        return []
    out = []
    if _qdrant_available():
        try:
            import qdrant_module as qm
            for r in qm.search(GLOBAL_COLLECTION, query, top_k * 2):
                pl = r.get("payload") or {}
                txt = pl.get("text", "")
                if txt:
                    out.append({"text": txt, "score": float(r.get("score", 0)),
                                "kind": (pl.get("extra") or {}).get("kind", "fact")})
        except Exception as e:
            logger.debug("[MEMORY-L2] qdrant recall failed: %s" % e)
    if not out:
        out = _jsonl_keyword_recall(query, top_k)
    return out[:top_k]


def _jsonl_keyword_recall(query, top_k):
    """JSONL-фолбэк: поиск по ключевым словам с нормализацией запроса."""
    words = set(re.findall(r'[а-яёa-z0-9]{3,}', str(query).lower()))
    extra = set()
    for w in words:
        for suf in ("ать", "ить", "еть", "ами", "ов", "ев", "ой", "ые", "ый"):
            if w.endswith(suf) and len(w) > len(suf) + 2:
                extra.add(w[:-len(suf)])
    words |= extra
    scored = []
    try:
        with open(GLOBAL_JSONL, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    swallow.note('memory_levels._jsonl_keyword_recall:L129')
                    continue
                tl = rec.get("text", "").lower()
                score = sum(1 for w in words if w in tl)
                if score:
                    scored.append((score, rec))
    except Exception:
        swallow.note('memory_levels._jsonl_keyword_recall:L136')
        return []
    scored.sort(key=lambda x: (-x[0], -x[1].get("time", 0)))
    return [{"text": r.get("text", ""), "score": min(1.0, s / (len(words) or 1)),
             "kind": r.get("kind", "fact")} for s, r in scored[:top_k]]


# ============================================================
# L1 — ПАМЯТЬ СЕССИИ (итоги задач)
# ============================================================

def store_task_summary(session, task, summary):
    """Сохранить итог выполненной задачи: L1 (в сессию) + L2 (глобально)."""
    task = str(task or "").strip()[:300]
    summary = str(summary or "").strip()[:1500]
    if not summary:
        return
    try:
        sums = session.setdefault("task_summaries", [])
        sums.append({"task": task, "summary": summary[:600], "time": time.time()})
        session["task_summaries"] = sums[-20:]  # бюджет: последние 20 задач
    except Exception as e:
        logger.debug("[MEMORY-L1] store failed: %s" % e)
    try:
        remember_global("Задача «%s» — итог: %s" % (task[:120], summary[:500]),
                        kind="task", session_id=(session or {}).get("id", ""),
                        title="task: %s" % task[:60])
    except Exception:
        swallow.note('memory_levels.store_task_summary:L164')
        pass


def recall_session(session, max_items=5):
    """Недавние задачи этой сессии для контекста (L1)."""
    out = []
    try:
        for s in (session.get("task_summaries") or [])[-max_items:]:
            out.append("%s → %s" % (s.get("task", "")[:100], s.get("summary", "")[:200]))
    except Exception:
        swallow.note('memory_levels.recall_session:L175')
        pass
    return out


# ============================================================
# СБОРКА ЕДИНОГО КОНТЕКСТА ИЗ ТРЁХ УРОВНЕЙ
# ============================================================

def build_memory_context(query, session=None, core_ctx_fn=None):
    """Единый контекст памяти для системного промпта: L3 → L1 → L2.

    core_ctx_fn — get_memory_context из agent_module (L3-факты + уроки),
    передаётся колбэком во избежание циклического импорта.
    """
    blocks = []

    # --- L3: явные факты (высший приоритет) ---
    if callable(core_ctx_fn):
        try:
            c = core_ctx_fn(max_per=10, query=query)
            if c and c.strip():
                blocks.append(c.strip())
        except Exception:
            swallow.note('memory_levels.build_memory_context:L199')
            pass

    # --- L1: недавние задачи этой сессии ---
    if isinstance(session, dict):
        recent = recall_session(session, max_items=5)
        if recent:
            blocks.append("=== ЗАДАЧИ ЭТОЙ СЕССИИ (уже сделано) ===\n" +
                          "\n".join("- " + r for r in recent))

    # --- L2: релевантное из общей памяти ---
    global_hits = []
    try:
        global_hits = [h for h in recall_global(query, top_k=4) if h.get("score", 0) >= 0.3]
    except Exception:
        swallow.note('memory_levels.build_memory_context:L214')
        pass
    if global_hits:
        kind_label = {"task": "ПРОШЛАЯ ЗАДАЧА", "fact": "ОБЩИЙ ФАКТ",
                      "lesson": "УРОК", "preference": "ПРЕДПОЧТЕНИЕ"}
        lines = []
        seen = set()
        for h in global_hits:
            t = h["text"][:250]
            key = t.lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            lines.append("- [%s] %s" % (kind_label.get(h.get("kind", ""), "ПАМЯТЬ"), t))
        if lines:
            blocks.append("=== ИЗ ОБЩЕЙ ПАМЯТИ (все сессии) ===\n" + "\n".join(lines[:4]))

    if not blocks:
        return ""
    return "\n\n".join(blocks)


def memory_stats():
    """Статистика уровней памяти (для API/UI)."""
    st = {"l2_collection": GLOBAL_COLLECTION,
          "l2_backend": "qdrant" if _qdrant_available() else "jsonl",
          "l2_records_file": 0}
    try:
        with open(GLOBAL_JSONL, encoding="utf-8") as f:
            st["l2_records_file"] = sum(1 for ln in f if ln.strip())
    except Exception:
        swallow.note('memory_levels.memory_stats:L245')
        pass
    return st


def list_global(limit=50, q=""):
    """Последние записи общей памяти (для UI). q — фильтр по подстроке."""
    out = []
    try:
        with open(GLOBAL_JSONL, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    swallow.note('memory_levels.list_global:L262')
                    continue
                if q and q.lower() not in rec.get("text", "").lower():
                    continue
                rec["idx"] = len(out)
                out.append(rec)
    except FileNotFoundError:
        swallow.note('memory_levels.list_global:L269')
        return []
    except Exception as e:
        logger.debug("[MEMORY-L2] list failed: %s" % e)
        return out
    return out[-limit:][::-1]  # свежие сверху


def delete_global(ts):
    """Удалить запись по точному времени (полю time). Возвращает число удалённых."""
    removed = 0
    tmp = GLOBAL_JSONL + ".tmp"
    try:
        with open(GLOBAL_JSONL, encoding="utf-8") as f, open(tmp, "w", encoding="utf-8") as w:
            for ln in f:
                keep = True
                try:
                    if float(json.loads(ln).get("time", -1)) == float(ts):
                        keep = False
                except Exception:
                    swallow.note('memory_levels.delete_global:L289')
                    pass
                if keep:
                    w.write(ln)
                else:
                    removed += 1
        os.replace(tmp, GLOBAL_JSONL)
    except FileNotFoundError:
        swallow.note('memory_levels.delete_global:L297')
        return 0
    except Exception as e:
        logger.warning("[MEMORY-L2] delete failed: %s" % e)
        try:
            os.remove(tmp)
        except Exception:
            swallow.note('memory_levels.delete_global:L304')
            pass
    return removed

