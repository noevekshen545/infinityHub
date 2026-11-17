#!/usr/bin/env python3
"""Векторный индекс материалов обучения.

Опциональная функция: по умолчанию ВЫКЛЮЧЕНА и включена быть не может без
явного согласия в интерфейсе (settings.json рядом с индексами). Все публичные
функции проверяют флаг и возвращают {"ok": False, "error": "disabled"}, поэтому
выключенная функция не читает материалы и не считает эмбеддинги.

Индекс локальный: фрагменты текста + векторы в education/vector/courses.
Векторы считает qdrant_module (та же ONNX-модель, что у «Векторов»), сервер
Qdrant не требуется. Поиск — косинус по нормализованным векторам.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

import qdrant_module
import swallow

MAX_CHUNKS = 800
CHUNK_CHARS = 900
CHUNK_OVERLAP = 120
MIN_SCORE = 0.20
EMBED_BATCH = 32

_STATE = {
    "building": False,
    "course_id": "",
    "processed": 0,
    "total": 0,
    "started": 0,
    "finished": 0,
    "error": "",
    "truncated": False,
}
_STATE_LOCK = threading.Lock()


def _edu():
    import modules.education_module as EDU
    return EDU


def _root() -> Path:
    return Path(_edu().COURSES_DIR).parent / "vector"


def _courses_dir() -> Path:
    p = _root() / "courses"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _settings_path() -> Path:
    return _root() / "settings.json"


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_settings() -> dict:
    try:
        d = json.loads(_settings_path().read_text(encoding="utf-8"))
    except Exception:
        swallow.note('education_vector.load_settings:L71')
        d = {}
    return {"enabled": bool(d.get("enabled", False))}


def set_enabled(enabled: bool) -> dict:
    data = load_settings()
    data["enabled"] = bool(enabled)
    data["updated_at"] = time.time()
    _atomic_write_json(_settings_path(), data)
    return data


def is_enabled() -> bool:
    return load_settings()["enabled"]


def _disabled() -> dict:
    return {"ok": False, "error": "disabled",
            "message": "Векторный поиск выключен. Включите его в окне «Векторный поиск»."}


def _safe_course_id(course_id) -> str:
    return _edu()._safe_id(course_id) or ""


def _meta_path(course_id: str) -> Path:
    return _courses_dir() / f"{course_id}.meta.json"


def _index_path(course_id: str) -> Path:
    return _courses_dir() / f"{course_id}.index.json"


def _normalize(vec):
    n = sum(x * x for x in vec) ** 0.5
    if n <= 0:
        return [0.0] * len(vec)
    return [round(float(x) / n, 5) for x in vec]


def _split_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP):
    text = re.sub(r"\r\n?", "\n", text or "")
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    out = []
    start, n = 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            floor = start + size // 2
            cut = max(text.rfind(". ", floor, end), text.rfind("\n", floor, end),
                      text.rfind(" ", floor, end))
            if cut > start:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


def _collect_chunks(course: dict):
    """(source, kind, text) для материалов и уроков курса."""
    EDU = _edu()
    items = []
    for m in (course.get("materials") or []):
        if not isinstance(m, dict):
            continue
        title = m.get("title") or m.get("name") or "Материал"
        try:
            txt = EDU._extract_material_text(m) or ""
        except Exception:
            swallow.note('education_vector._collect_chunks:L147')
            continue
        if not txt or txt.startswith("[Ошибка"):
            continue
        for c in _split_text(txt):
            items.append((str(title), "material", c))

    plan = EDU._plan_dict(course.get("plan"))
    lessons = plan.get("lessons") or []
    for i, lesson in enumerate(lessons):
        title = lesson.get("title") or f"Урок {i + 1}"
        txt = f"{title}\n\n{lesson.get('content') or ''}"
        for c in _split_text(txt):
            items.append((f"Урок {i + 1}: {title}", "lesson", c))

    analysis = (course.get("analysis") or "").strip()
    if analysis:
        for c in _split_text(analysis)[:20]:
            items.append(("Анализ курса", "analysis", c))

    truncated = False
    if len(items) > MAX_CHUNKS:
        step = len(items) / float(MAX_CHUNKS)
        items = [items[int(i * step)] for i in range(MAX_CHUNKS)]
        truncated = True
    return items, truncated


def _build_worker(course_id: str) -> None:
    try:
        course = _edu()._load_course(course_id)
        if not course:
            raise RuntimeError("Курс не найден")
        items, truncated = _collect_chunks(course)
        if not items:
            raise RuntimeError("В курсе нет текста для индексации")
        with _STATE_LOCK:
            _STATE.update({"total": len(items), "processed": 0,
                           "truncated": truncated, "error": ""})

        model = qdrant_module.get_embed_model()
        dim = qdrant_module.get_embed_dim()
        chunks = []
        for i in range(0, len(items), EMBED_BATCH):
            batch = items[i:i + EMBED_BATCH]
            vectors = qdrant_module.embed_texts([t for _, _, t in batch],
                                                op_label="индекс обучения")
            if not vectors or len(vectors) != len(batch):
                raise RuntimeError(qdrant_module.get_last_embed_error()
                                   or "Сервис эмбеддингов вернул неполный ответ")
            for (source, kind, text), vec in zip(batch, vectors):
                if not vec:
                    raise RuntimeError(qdrant_module.get_last_embed_error()
                                       or "Не удалось посчитать эмбеддинг")
                if len(vec) != dim:
                    dim = len(vec)
                chunks.append({"source": source, "kind": kind, "text": text,
                               "v": _normalize(vec)})
            with _STATE_LOCK:
                _STATE["processed"] = min(i + len(batch), len(items))

        meta = {
            "course_id": course_id,
            "title": course.get("title") or "Курс",
            "built_at": time.time(),
            "model": model,
            "dim": dim,
            "chunk_chars": CHUNK_CHARS,
            "chunks": len(chunks),
            "truncated": truncated,
            "text_chars": sum(len(c["text"]) for c in chunks),
        }
        _atomic_write_json(_index_path(course_id), {"chunks": chunks})
        _atomic_write_json(_meta_path(course_id), meta)
        with _STATE_LOCK:
            _STATE["finished"] = time.time()
    except Exception as e:  # noqa: BLE001
        swallow.note('education_vector._build_worker:L224', e)
        with _STATE_LOCK:
            _STATE["error"] = str(e)
    finally:
        with _STATE_LOCK:
            _STATE["building"] = False


def start_build(course_id: str) -> dict:
    if not is_enabled():
        return _disabled()
    cid = _safe_course_id(course_id)
    if not cid:
        return {"ok": False, "error": "Некорректный id курса"}
    if not _edu()._load_course(cid):
        return {"ok": False, "error": "Курс не найден"}
    with _STATE_LOCK:
        if _STATE["building"]:
            return {"ok": False, "error": "busy",
                    "message": "Индекс уже строится", "course_id": _STATE["course_id"]}
        _STATE.update({"building": True, "course_id": cid, "processed": 0,
                       "total": 0, "started": time.time(), "finished": 0,
                       "error": "", "truncated": False})
    threading.Thread(target=_build_worker, args=(cid,), daemon=True).start()
    return {"ok": True, "started": True, "course_id": cid}


def get_build_state() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)


def load_meta(course_id: str):
    cid = _safe_course_id(course_id)
    if not cid:
        return None
    try:
        return json.loads(_meta_path(cid).read_text(encoding="utf-8"))
    except Exception:
        swallow.note('education_vector.load_meta:L263')
        return None


def list_indexes():
    out = []
    for p in _courses_dir().glob("*.meta.json"):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(m, dict) and m.get("course_id"):
                out.append(m)
        except Exception:
            swallow.note('education_vector.list_indexes:L275')
            continue
    out.sort(key=lambda m: m.get("built_at") or 0, reverse=True)
    return out


def status(course_id: str = "") -> dict:
    enabled = is_enabled()
    data = {
        "ok": True,
        "enabled": enabled,
        "building": get_build_state()["building"],
        "state": get_build_state(),
        "model": qdrant_module.get_embed_model(),
        "max_chunks": MAX_CHUNKS,
        "indexes": list_indexes() if enabled else [],
    }
    if course_id:
        data["index"] = load_meta(course_id) if enabled else None
    return data


def search(course_id: str, query: str, limit: int = 6) -> dict:
    if not is_enabled():
        return _disabled()
    cid = _safe_course_id(course_id)
    if not cid:
        return {"ok": False, "error": "Некорректный id курса"}
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "Пустой запрос"}
    try:
        limit = max(1, min(20, int(limit)))
    except Exception:
        swallow.note('education_vector.search:L309')
        limit = 6
    meta = load_meta(cid)
    if not meta:
        return {"ok": False, "error": "not_indexed",
                "message": "Индекс этого курса ещё не построен"}
    try:
        idx = json.loads(_index_path(cid).read_text(encoding="utf-8"))
        chunks = idx.get("chunks") or []
    except Exception as e:  # noqa: BLE001
        swallow.note('education_vector.search:L319', e)
        return {"ok": False, "error": f"Индекс повреждён: {e}"}
    if not chunks:
        return {"ok": False, "error": "not_indexed",
                "message": "Индекс пуст — перестройте его"}

    qv = qdrant_module.embed_query(query)
    if not qv:
        return {"ok": False, "error": "embed_failed",
                "details": qdrant_module.get_last_embed_error() or "",
                "message": "Не удалось посчитать эмбеддинг запроса"}
    qn = _normalize(qv)
    if len(qn) != len(chunks[0].get("v") or []):
        return {"ok": False, "error": "dim_mismatch",
                "message": "Модель эмбеддингов изменилась — перестройте индекс"}

    scored = []
    for ch in chunks:
        v = ch.get("v") or []
        if len(v) != len(qn):
            continue
        score = sum(a * b for a, b in zip(qn, v))
        scored.append((score, ch))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = [{"source": ch.get("source") or "",
                "kind": ch.get("kind") or "",
                "text": ch.get("text") or "",
                "score": round(score * 100, 1)}
               for score, ch in scored[:limit]]
    return {"ok": True, "query": query, "results": results, "meta": meta}


def context_for(course_id: str, query: str, limit: int = 5, min_score: float = MIN_SCORE):
    """Готовый текстовый контекст для AI-ответа или '' если ничего не нашлось."""
    res = search(course_id, query, limit)
    if not res.get("ok"):
        return ""
    rows = [r for r in res.get("results") or [] if r.get("score", 0) >= min_score * 100]
    if not rows:
        return ""
    parts = [f"[{r['source']}] {r['text'][:700]}" for r in rows]
    return "\n\n---\n".join(parts)


def purge(course_id: str = "", all_indexes: bool = False) -> dict:
    if all_indexes:
        removed = 0
        for p in list(_courses_dir().glob("*.json")):
            try:
                p.unlink()
                removed += 1
            except OSError:
                swallow.note('education_vector.purge:L371')
                pass
        return {"ok": True, "removed": removed, "all": True}
    cid = _safe_course_id(course_id)
    if not cid:
        return {"ok": False, "error": "Некорректный id курса"}
    removed = 0
    for p in (_meta_path(cid), _index_path(cid)):
        try:
            if p.exists():
                p.unlink()
                removed += 1
        except OSError:
            swallow.note('education_vector.purge:L384')
            pass
    return {"ok": True, "removed": removed, "course_id": cid}
