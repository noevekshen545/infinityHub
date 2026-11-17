# -*- coding: utf-8 -*-
"""Смысловая группировка заметок через движок laya.

Отдельный слой поверх графа: папки и теги описывают, где заметка лежит и
как размечена, а здесь классификатор относит текст к одной смысловой теме.
Результат считается одним пакетным проходом laya (decide_batch), поэтому
на корпусе из сотни заметок это не сотня запросов, а несколько пачек.

Кэш лежит вне каталога заметок (app/data/notes_semantic): если писать его
рядом, обход заметок принял бы служебный JSON за заметку. Подпись кэша —
id и updated всех заметок, поэтому любая правка делает кэш устаревшим,
но старые группы не подмешиваются в граф: лучше показать «нужно
пересчитать», чем раскрасить заметки по устаревшему смыслу.

Фича включается только флагом laya.features.notes_semantic_groups
(по умолчанию выключен) и не работает при выключенном движке.
"""

import hashlib
import json
import os
import threading
import time

CACHE_VERSION = 1
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "notes_semantic")
BATCH_SIZE = 24
MIN_CONFIDENCE = 0.30

# Темы намеренно общие: классификатор выбирает из закрытого списка, а не
# придумывает название. Ключ стабилен — по нему строится id узла графа.
THEMES = [
    {"key": "work", "label": "Работа", "color": "#6366f1",
     "desc": "работа, проекты, задачи, совещания, коллеги, клиенты"},
    {"key": "study", "label": "Учёба", "color": "#0ea5e9",
     "desc": "учёба, курсы, книги, исследования, конспекты, экзамены"},
    {"key": "tech", "label": "Технологии", "color": "#8b5cf6",
     "desc": "технологии, программирование, ИИ, компьютеры, гаджеты, софт"},
    {"key": "ideas", "label": "Идеи", "color": "#f59e0b",
     "desc": "идеи, планы, замыслы, концепции, гипотезы, черновики мыслей"},
    {"key": "personal", "label": "Личное", "color": "#ec4899",
     "desc": "личное, семья, отношения, дневник, чувства, воспоминания"},
    {"key": "health", "label": "Здоровье", "color": "#22c55e",
     "desc": "здоровье, спорт, питание, сон, медицина, самочувствие"},
    {"key": "finance", "label": "Финансы", "color": "#eab308",
     "desc": "деньги, финансы, покупки, бюджет, инвестиции, расходы"},
    {"key": "home", "label": "Дом и быт", "color": "#14b8a6",
     "desc": "дом, быт, ремонт, вещи, хозяйство, покупки для дома"},
    {"key": "travel", "label": "Путешествия", "color": "#06b6d4",
     "desc": "путешествия, поездки, места, отпуск, маршруты"},
    {"key": "creative", "label": "Творчество", "color": "#a855f7",
     "desc": "творчество, тексты, музыка, искусство, дизайн, съёмка"},
    {"key": "refs", "label": "Справочное", "color": "#64748b",
     "desc": "ссылки, справочные материалы, выдержки, инструкции, заметки на будущее"},
    {"key": "other", "label": "Разное", "color": "#94a3b8",
     "desc": "не подходит ни одна из перечисленных тем"},
]
THEME_BY_KEY = {t["key"]: t for t in THEMES}

_lock = threading.RLock()
_building = set()


def feature_enabled():
    try:
        import laya_client
        return bool(laya_client.feature("notes_semantic_groups"))
    except Exception:
        return False


def _cache_path(notes_dir):
    key = hashlib.sha1(os.path.normcase(os.path.abspath(notes_dir))
                       .encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, "groups_%s.json" % key)


def signature(notes):
    """Подпись корпуса: id + updated. Правка любой заметки её меняет."""
    parts = []
    for n in notes or []:
        parts.append("%s|%s" % (n.get("id") or "", n.get("updated") or 0))
    parts.sort()
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()


def _load_cache(notes_dir):
    path = _cache_path(notes_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return None
        return data
    except Exception:
        return None


def _write_cache(notes_dir, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(notes_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _active_notes(notes_dir):
    try:
        from notes_graph_module import iter_notes, normalize_note
    except Exception:
        return []
    out = []
    for art, folder, fname in iter_notes(notes_dir, include_archived=False):
        n = normalize_note(art, folder, fname)
        if (n.get("content") or "").strip() or (n.get("title") or "").strip():
            out.append(n)
    return out


# Мемо статуса. Функция нужна только ради счётчиков, но чтобы их получить,
# приходится перечитать ВСЕ заметки (на живой базе 53 файла, 145 мс — в них
# лежат base64-картинки). Ответ меняется только при правке заметки или при
# пересборке кэша, поэтому ключ мемо — сигнатура каталога заметок плюс
# mtime/размер файла кэша: сохранение и пересборка сразу дают новый ключ.
_status_memo = None
_status_lock = threading.Lock()


def _cache_stamp(notes_dir):
    try:
        st = os.stat(_cache_path(notes_dir))
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _status_proof(notes_dir):
    import read_cache
    return (os.path.abspath(notes_dir or ""), read_cache.dir_signature(notes_dir),
            _cache_stamp(notes_dir))


def _status_compute(notes_dir):
    notes = _active_notes(notes_dir)
    sig = signature(notes)
    cache = _load_cache(notes_dir)
    fresh = bool(cache and cache.get("signature") == sig)
    groups = cache.get("groups") if isinstance(cache, dict) else []
    return {
        "ready": bool(cache),
        "fresh": fresh,
        "stale": bool(cache and not fresh),
        "generated": (cache or {}).get("generated") or 0,
        "notes": len(notes),
        "assigned": len((cache or {}).get("assignments") or {}),
        "groups": groups if isinstance(groups, list) else [],
        "engine": (cache or {}).get("engine") or "",
    }


def status(notes_dir):
    """Состояние кэша для страницы графа и админки."""
    global _status_memo
    proof = _status_proof(notes_dir)
    with _status_lock:
        memo = _status_memo
    # Флаг читается каждый раз: его переключают в настройках, и залипшее
    # значение врало бы в админке до следующей правки заметок.
    if memo is not None and memo[0] == proof:
        return dict(memo[1], feature=feature_enabled())
    value = _status_compute(notes_dir)
    with _status_lock:
        _status_memo = (proof, value)
    return dict(value, feature=feature_enabled())


def _topic_questions():
    criteria = {}
    for t in THEMES:
        criteria[t["key"]] = "%s — %s" % (t["label"], t["desc"])
    return {
        "topic": {
            "type": "choice",
            "instructions": "К какой смысловой теме относится эта заметка?",
            "criteria": criteria,
        },
    }


def _text_of(n):
    title = (n.get("title") or "").strip()
    body = (n.get("description") or n.get("content") or "").strip()
    return (title + "\n" + body[:900]).strip()


def _group_nodes(assignments, notes):
    by_theme = {}
    note_ids = set(n["id"] for n in notes)
    for nid, a in (assignments or {}).items():
        if nid not in note_ids:
            continue
        theme = a.get("theme") if isinstance(a, dict) else None
        if theme not in THEME_BY_KEY or theme == "other":
            continue
        by_theme.setdefault(theme, []).append(nid)
    out = []
    for key, members in sorted(by_theme.items()):
        if len(members) < 2:
            continue
        meta = THEME_BY_KEY[key]
        out.append({
            "id": "semantic:%s" % key,
            "key": key,
            "title": meta["label"],
            "color": meta["color"],
            "count": len(members),
            "members": sorted(members),
        })
    return out


def build(notes_dir, notes=None, force=False):
    """Пересчитать смысловые группы. Возвращает статус или {error}.

    force=False позволяет не тратить запросы, если кэш уже свежий.
    """
    if not feature_enabled():
        return {"error": "Смысловая группировка выключена в настройках laya",
                "feature": False}
    notes = list(notes if notes is not None else _active_notes(notes_dir))
    sig = signature(notes)
    with _lock:
        cache = _load_cache(notes_dir)
        if cache and cache.get("signature") == sig and not force:
            return status(notes_dir)
        if sig in _building:
            return {"error": "Пересчёт уже идёт", "busy": True}

        # Заметки без текста классифицировать нечем: они не попадают в
        # assignments и остаются вне смысловых групп.
        work = [n for n in notes if _text_of(n)]
        if not work:
            data = {
                "version": CACHE_VERSION, "signature": sig,
                "generated": time.time(), "engine": _engine_name(),
                "assignments": {}, "groups": [],
            }
            _write_cache(notes_dir, data)
            return status(notes_dir)

        _building.add(sig)
        try:
            import laya_client
        except Exception as e:
            _building.discard(sig)
            return {"error": "Движок laya недоступен: %s" % e, "feature": True}
        if not laya_client.is_enabled():
            _building.discard(sig)
            return {"error": "Движок laya выключен", "feature": True}

        questions = _topic_questions()
        assignments = {}
        errors = 0

        def run_states(states, depth=0):
            # Интерактивный бюджет 1.5 с рассчитан на одну короткую строку.
            # Пачка заметок на CPU идёт заметно дольше, поэтому таймаут
            # считается по объёму текста, а слишком медленная пачка один раз
            # дробится — одна длинная заметка не должна ронять пересчёт.
            budget = min(180.0, 30.0 + sum(len(s) for s in states) / 200.0)
            res = laya_client.decide_batch(states, questions, timeout=budget)
            if res is not None or depth >= 1 or len(states) <= 8:
                return res
            mid = len(states) // 2
            left = run_states(states[:mid], depth + 1)
            right = run_states(states[mid:], depth + 1)
            if left is None or right is None:
                return None
            return left + right

        try:
            for i in range(0, len(work), BATCH_SIZE):
                chunk = work[i:i + BATCH_SIZE]
                states = [_text_of(n) for n in chunk]
                results = run_states(states)
                if results is None:
                    errors += len(chunk)
                    continue
                for n, res in zip(chunk, results):
                    if not isinstance(res, dict):
                        errors += 1
                        continue
                    ans = laya_client.answers(res).get("topic") or {}
                    choice = laya_client.choice_of(ans)
                    conf = 0.0
                    try:
                        conf = float(ans.get("answer_confidence")
                                     or ans.get("confidence") or 0.0)
                    except Exception:
                        conf = 0.0
                    if choice not in THEME_BY_KEY or conf < MIN_CONFIDENCE:
                        choice = "other"
                    assignments[str(n["id"])] = {
                        "theme": choice,
                        "confidence": round(conf, 3),
                    }
        finally:
            _building.discard(sig)

        if not assignments:
            return {"error": "laya не ответила — группы не построены",
                    "feature": True, "errors": errors}

        groups = _group_nodes(assignments, work)
        data = {
            "version": CACHE_VERSION,
            "signature": sig,
            "generated": time.time(),
            "engine": _engine_name(),
            "assignments": assignments,
            "groups": groups,
        }
        _write_cache(notes_dir, data)
        out = status(notes_dir)
        out["errors"] = errors
        out["rebuilt"] = True
        return out


def _engine_name():
    try:
        import laya_client
        return laya_client._model() or "multilingual"
    except Exception:
        return ""


def graph_extra(notes_dir, notes):
    """Узлы и рёбра смысловых групп — только если кэш совпадает с корпусом.

    Возвращает (nodes, edges, meta) или (None, None, meta). Вызывается из
    graph_data(), поэтому не ходит в laya: граф не должен ждать инференс.
    """
    meta = {"enabled": feature_enabled(), "ready": False, "stale": False,
            "groups": 0, "generated": 0}
    if not meta["enabled"]:
        return None, None, meta
    cache = _load_cache(notes_dir)
    if not cache:
        return None, None, meta
    meta["generated"] = cache.get("generated") or 0
    sig = signature(notes)
    if cache.get("signature") != sig:
        meta["stale"] = True
        return None, None, meta

    nodes, edges = [], []
    note_ids = set(n["id"] for n in notes)
    for g in cache.get("groups") or []:
        members = [m for m in (g.get("members") or []) if m in note_ids]
        if len(members) < 2:
            continue
        meta["groups"] += 1
        gid = "semantic:%s" % g.get("key")
        meta_theme = THEME_BY_KEY.get(g.get("key")) or {}
        nodes.append({
            "id": gid,
            "kind": "group",
            "group_kind": "semantic",
            "title": g.get("title") or meta_theme.get("label") or g.get("key"),
            "section": "По смыслу",
            "color": g.get("color") or meta_theme.get("color") or "#60a5fa",
            "emoji": "",
            "size": max(2, min(6, len(members) // 3 + 1)),
            "count": len(members),
            "description": "Смысл: %d заметок" % len(members),
            "members": members,
        })
        for m in members:
            edges.append({"source": gid, "target": m, "type": "semantic",
                          "linkType": "semantic", "weight": 1.1})
    meta["ready"] = True
    return nodes, edges, meta
