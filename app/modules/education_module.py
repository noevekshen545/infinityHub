#!/usr/bin/env python3
"""Education Module v2 — Infinity-powered course generation with SSE streaming."""
import json, os, uuid, time, threading, re, sys, asyncio, logging, hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, AsyncGenerator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server
from modules import llm_service
from modules import books_read
from modules import zim_store
import swallow

logger = logging.getLogger("infinity")


def _education_data_root():
    """Корень данных образования с учётом переносимой сборки.

    Раньше путь жёстко собирался как <app>/data/education, игнорируя
    INFINITY_DATA_DIR / <корень>/data — в переносимой сборке образование
    писало в app/data, а всё остальное в <корень>/data.

    Неразрушающе: если переносимого каталога ещё нет, а старый существует —
    продолжаем работать со старым (данные не теряются и не дублируются).
    """
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    legacy = Path(os.path.join(app_dir, "data", "education"))
    try:
        import profiles_module as _pm
        preferred = Path(_pm.DATA_DIR) / "education"
    except Exception:
        swallow.note('education_module._education_data_root:L33')
        return legacy
    if not preferred.exists() and legacy.exists():
        return legacy
    return preferred


EDUCATION_DIR = _education_data_root()
COURSES_DIR = EDUCATION_DIR / "courses"
NOTES_DIR = EDUCATION_DIR / "notes"
BUFFER_PATH = EDUCATION_DIR / "buffer.json"
PROFILES_DIR = EDUCATION_DIR / "profiles"
TEMPLATES_DIR = EDUCATION_DIR / "templates"
# Базовые (глобальные) каталоги профилей/шаблонов образования.
# Они НЕ зависят от set_education_dirs (который перенаправляет PROFILES_DIR/TEMPLATES_DIR
# на per-app-profile пути) и всегда указывают на data/education/profiles.
BASE_PROFILES_DIR = EDUCATION_DIR / "profiles"
BASE_TEMPLATES_DIR = EDUCATION_DIR / "templates"
UPLOADS_DIR = EDUCATION_DIR / "uploads"
FOLDERS_PATH = EDUCATION_DIR / "folders.json"
CHUNK_SIZE = 30000
MAX_RETRIES = 3

_pool = ThreadPoolExecutor(max_workers=2)

def _ensure_dirs():
    COURSES_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def set_education_dirs(profile_id=None):
    """Навести каталоги образования на профиль ('data/profiles/<id>/data/education')."""
    global COURSES_DIR, NOTES_DIR, BUFFER_PATH, PROFILES_DIR, UPLOADS_DIR, FOLDERS_PATH
    if profile_id:
        # per-profile каталог берём из profiles_module, чтобы он совпадал с
        # INFINITY_DATA_DIR/переносимой сборкой; старый путь — фолбэк
        base = None
        try:
            import profiles_module as _pm
            base = Path(_pm._profile_data_path(str(profile_id))) / "education"
        except Exception:
            swallow.note('education_module.set_education_dirs:L77')
            base = None
        if base is None:
            base = Path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "data", "profiles", str(profile_id), "data", "education"))
    else:
        base = EDUCATION_DIR
    COURSES_DIR = base / "courses"
    NOTES_DIR = base / "notes"
    BUFFER_PATH = base / "buffer.json"
    PROFILES_DIR = base / "profiles"
    UPLOADS_DIR = base / "uploads"
    FOLDERS_PATH = base / "folders.json"
    _ensure_dirs()

def _now(): return time.time()

_ERROR_PREFIXES = ("Ошибка подключения к LM Studio", "Ошибка LM Studio", "Ошибка подключения к Llama", "Ошибка:")

def _call_ai(system_prompt, user_prompt, max_tokens=4096, temperature=0.7, model=None, timeout=120):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    for attempt in range(MAX_RETRIES):
        future = _pool.submit(llm_service.lm_studio_request, messages=messages, max_tokens=max_tokens, temperature=temperature, stream=False, model=model)
        try:
            result = future.result(timeout=timeout)
        except Exception:
            raise ConnectionError(f"AI не отвечает (таймаут {timeout}с)")
        content = result.get("content", "") or result.get("reasoning", "")
        if content.strip():
            if content.startswith(_ERROR_PREFIXES):
                raise ConnectionError(content)
            return content
        time.sleep(1)
    raise ConnectionError("AI не отвечает после нескольких попыток")


def _extract_json(text):
    """Вытащить первый валидный JSON-объект/массив из текста.

    Сканирование учитывает строки JSON (двойные кавычки и экранирование),
    поэтому фигурные/квадратные скобки внутри строковых значений
    (например, Python-код со словарями в контенте урока) не ломают парсинг.
    """
    if not text:
        return None
    text = text.strip()
    m = re.search(r'```(?:json)?\s*([\[{].*?[\]}])\s*```', text, re.DOTALL)
    if m:
        text = m.group(1)

    # Стартовые позиции { или [ вне строк JSON
    starts = []
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if not in_str and ch in '{[':
            starts.append(i)

    for start in starts[:60]:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == '\\':
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch in '({[':
                depth += 1
            elif ch in ')}]':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        # Кандидат невалиден — пробуем следующую стартовую позицию
                        swallow.note('education_module._extract_json:L173')
                        break
        else:
            # Скобка не закрылась до конца текста — пробуем распарсить всё до конца
            if depth > 0:
                try:
                    return json.loads(text[start:])
                except Exception:
                    swallow.note('education_module._extract_json:L182')
                    pass
    return None

def _extract_json_dict(text):
    data = _extract_json(text)
    return data if isinstance(data, dict) else {}

def _extract_json_lenient(text):
    """Extract JSON even with unescaped newlines in string values."""
    result = _extract_json(text)
    if result:
        return result
    if not text:
        return None
    in_str = False
    esc = False
    fixed = []
    for ch in text:
        if esc:
            esc = False
            fixed.append(ch)
            continue
        if ch == '\\':
            esc = True
            fixed.append(ch)
            continue
        if ch == '"' and not esc:
            in_str = not in_str
        if in_str and ch in ('\n', '\r'):
            fixed.append('\\n')
            continue
        fixed.append(ch)
    return _extract_json(''.join(fixed))

_SYSTEM_PROMPT_PATTERNS = [
    r'^(Ты — (?:преподаватель|педагог|методист|библиограф|AI.?репетитор|эксперт|строгий преподаватель).*?\.)\s*',
    r'^(Ответь ТОЛЬКО JSON[^.]*\.)\s*',
]

def _sanitize_content(text):
    if not text:
        return ""
    for pat in _SYSTEM_PROMPT_PATTERNS:
        text = re.sub(pat, '', text, flags=re.MULTILINE)
    text = re.sub(r'<(script|style|iframe|object|embed)[^>]*>.*?</\1>', ' ', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\b(?:function|var|let|const|document\.|window\.|console\.|alert\()\s*[^.\n]*?;', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text

def _clean_text(text):
    return _sanitize_content(text)


def _normalize_text(text):
    """Починить «мусор», который оставляют локальные LLM:
    двойное экранирование (\\n, \\t, \\", \\') вместо настоящих
    управляющих символов и лишние переводы строк."""
    if not isinstance(text, str) or not text:
        return text
    text = text.replace('\\n', '\n')
    text = text.replace('\\r', '\r')
    text = text.replace('\\t', '\t')
    text = text.replace('\\"', '"')
    text = text.replace("\\'", "'")
    text = text.replace('\\\\', '\\')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _normalize_data(data):
    """Рекурсивно применить _normalize_text ко всем строкам структуры."""
    if isinstance(data, dict):
        return {k: _normalize_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_normalize_data(v) for v in data]
    if isinstance(data, str):
        return _normalize_text(data)
    return data


def _fallback_objectives(title, content):
    """Если модель не вернула цели урока — собрать их из заголовков/абзацев."""
    headers = [h.strip() for h in re.findall(r'^#{1,4}\s+(.+)$', content or '', re.M)]
    headers = [h for h in headers if len(h) > 3 and len(h) < 80]
    if len(headers) >= 2:
        out = [f"Изучить: {h}" for h in headers[:6]]
        out.append("Выполнить практические задания урока")
        return out[:8]
    return [
        f"Понять ключевые понятия темы «{(title or 'урока').strip()[:50]}»",
        "Разобраться в основных принципах и примерах",
        "Выполнить практические задания урока",
        "Закрепить материал с помощью теста",
    ]


def _fallback_key_terms(title, content):
    """Если модель не вернула ключевые термины — извлечь их из текста урока."""
    terms = []
    seen = set()
    def _add(t):
        t = (t or '').strip().rstrip('.,;:!?')
        if 3 <= len(t) <= 60 and t.lower() not in seen:
            seen.add(t.lower())
            terms.append({"term": t, "definition": t})
    for m in re.finditer(r'\*\*(.+?)\*\*', content or ''):
        _add(m.group(1))
    for m in re.finditer(r'`([^`]+)`', content or ''):
        _add(m.group(1))
    if len(terms) < 3:
        headers = [h.strip() for h in re.findall(r'^#{1,4}\s+(.+)$', content or '', re.M)]
        for h in headers:
            _add(h.split(':')[0].split(' — ')[0].strip())
    if not terms:
        terms.append({"term": (title or 'Ключевое понятие')[:40], "definition": "Ключевое понятие урока"})
    return terms[:12]

_EDU_SUBJECTS = [
    "математика", "физика", "химия", "биология", "информатика", "история",
    "география", "литература", "философия", "языки", "экономика", "право",
    "медицина", "психология", "программирование", "архитектура", "дизайн",
    "маркетинг", "менеджмент", "статистика", "логика", "обществознание",
]


def _laya_education_enabled():
    """Включена ли фича классификации уроков движком laya."""
    try:
        import laya_client
    except Exception:
        swallow.note('education_module._laya_education_enabled:L314')
        return False
    try:
        return bool(laya_client.feature("education_auto_terms"))
    except Exception:
        swallow.note('education_module._laya_education_enabled:L319')
        return False


def _laya_classify_lesson(title, content):
    """Классифицировать урок: предмет и сложность (laya).

    Не генерирует текст — только метки, поэтому дополняет урок, а не
    заменяет сгенерированные цели и термины.
    """
    try:
        import laya_client
    except Exception:
        swallow.note('education_module._laya_classify_lesson:L332')
        return None
    try:
        text = ("%s\n%s" % (title or "", (content or "")[:1500])).strip()
        if len(text) < 20:
            return None
        res = laya_client.education_terms(text, subjects=_EDU_SUBJECTS)
        if not res:
            return None
        return res
    except Exception:
        swallow.note('education_module._laya_classify_lesson:L343')
        return None


def _norm_material(m, idx=0):
    """Привести материал к словарю.

    Буфер материалов наполняется из разных источников (загрузка файла,
    книга из коллекции, ZIM, произвольный JSON с фронта). Если туда
    попала строка или число, обращения m.get(...) падали с
    «'str' object has no attribute 'get'» и генерация падала целиком.
    """
    if isinstance(m, dict):
        return m
    if isinstance(m, str):
        t = m.strip()
        if t.startswith("{"):
            try:
                cand = json.loads(t)
                if isinstance(cand, dict):
                    return cand
            except Exception:
                swallow.note('education_module._norm_material:L365')
                pass
        return {"type": "book", "path": t, "file": t, "title": Path(t).name or f"Материал {idx + 1}"}
    return {"type": "book", "path": "", "file": "", "title": f"Материал {idx + 1}"}


def _norm_materials(materials):
    """Список материалов -> список словарей (пустые отбрасываются)."""
    if not isinstance(materials, list):
        return []
    out = []
    for i, m in enumerate(materials):
        nm = _norm_material(m, i)
        if nm.get("path") or nm.get("text") or nm.get("file"):
            out.append(nm)
    return out


def _extract_material_text(material):
    if not isinstance(material, dict):
        material = _norm_material(material)
    try:
        if material.get("type") == "book":
            fp = material.get("path", "")
            if not fp:
                inline = material.get("text")
                if isinstance(inline, str) and inline.strip():
                    return inline
                return "[Путь не указан]"
            if fp.startswith("collection://"):
                result = books_read.get_book_content(fp)
                if isinstance(result, dict):
                    if "error" in result:
                        return f"[Ошибка: {result['error']}]"
                    if "chapters" in result:
                        parts = [f"--- {c.get('title','')} ---\n{c.get('text','')}" for c in result["chapters"]]
                        return "\n\n".join(parts)
                    if "full_text" in result:
                        return result["full_text"]
                return str(result)
            if not os.path.exists(fp):
                return f"[Файл не найден: {fp}]"
            ext = os.path.splitext(fp)[1].lower()
            text_map = {
                '.txt': lambda: books_read._read_txt(fp),
                '.epub': lambda: books_read._read_epub(fp),
                '.fb2': lambda: books_read._read_fb2(fp),
                '.pdf': lambda: books_read._read_pdf(fp),
                '.docx': lambda: books_read._read_docx(fp),
            }
            reader = text_map.get(ext, lambda: books_read._read_book_file(fp, ext))
            result = reader()
            if isinstance(result, dict) and "chapters" in result:
                parts = []
                for c in result["chapters"]:
                    title = c.get('title', '')
                    text = c.get('text', '')
                    parts.append(f"--- {title} ---\n{text}")
                return "\n\n".join(parts)
            elif isinstance(result, dict) and "full_text" in result:
                return result["full_text"]
            return str(result)
        elif material.get("type") == "zim":
            fp = material.get("file", "")
            path = material.get("path", "")
            if not fp or not os.path.exists(fp):
                return "[ZIM файл не найден]"
            result = zim_store.zim_get_entry(fp, path)
            if isinstance(result, dict) and "content" in result:
                return result["content"]
            return str(result)
        # Текстовый материал (например, фрагменты из векторной базы) —
        # текст лежит прямо в записи, файла нет.
        inline = material.get("text")
        if isinstance(inline, str) and inline.strip():
            return inline
        return ""
    except Exception as e:
        swallow.note('education_module._extract_material_text:L443', e)
        return f"[Ошибка чтения: {e}]"

def _chunk_text(text, size=CHUNK_SIZE):
    paragraphs = text.split('\n')
    chunks = []
    current = []
    current_len = 0
    for p in paragraphs:
        plen = len(p) + 1
        if current_len + plen > size and current:
            chunks.append('\n'.join(current))
            current = []
            current_len = 0
        current.append(p)
        current_len += plen
    if current:
        chunks.append('\n'.join(current))
    return chunks if chunks else [text]

def _safe_id(value):
    """ID, пригодный как имя файла, или None.

    course_id / profile_id / template_id приходят из URL. Раньше они без
    проверки подставлялись в путь, поэтому `../..` выводил чтение, запись
    и — в delete_course() — УДАЛЕНИЕ за пределы каталога данных.
    """
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s or len(s) > 128:
        return None
    if s in (".", ".."):
        return None
    if not re.match(r"^[A-Za-z0-9._-]+$", s):
        return None
    return s


def _course_path(course_id):
    """Путь к JSON курса или None, если ID небезопасен."""
    cid = _safe_id(course_id)
    return COURSES_DIR / f"{cid}.json" if cid else None

def _notes_path(course_id):
    """Путь к JSON заметок курса или None, если ID небезопасен."""
    cid = _safe_id(course_id)
    return NOTES_DIR / f"{cid}_notes.json" if cid else None

def _save_course(course):
    _ensure_dirs()
    # План нормализуем при каждой записи: иначе фаза, которая делает
    # course["plan"]["bibliography"] = ..., падала на «'str' object does
    # not support item assignment», если модель вернула plan строкой.
    # _plan_dict отдаёт новый dict, поэтому подменять безопасно.
    if isinstance(course, dict):
        course["plan"] = _plan_dict(course.get("plan"))
    path = _course_path(course.get("id"))
    if path is None:
        raise ValueError("Недопустимый ID курса")
    path.write_text(json.dumps(course, ensure_ascii=False, indent=2), "utf-8")


_LESSON_DEFAULTS = {
    "content": "", "objectives": [], "key_terms": [],
    "test": {}, "quiz": [], "practice": "", "homework": "",
}


def _empty_lesson(title):
    """Новый урок с пустыми полями (mutable-поля — новые на каждый вызов)."""
    return {
        "title": title, "content": "", "objectives": [], "key_terms": [],
        "test": {}, "quiz": [], "practice": "", "homework": "",
    }


def _lesson_dict(lesson, idx=0):
    """Привести один урок к словарю с полным набором полей.

    ИИ иногда отдаёт уроки просто списком строк, а иногда — объектами
    без половины полей. Раньше l.get("title") на строке и отсутствие
    ключей роняли генерацию с «'str' object has no attribute 'get'»
    и KeyError соответственно.
    """
    if isinstance(lesson, str):
        return _empty_lesson(lesson.strip() or f"Урок {idx + 1}")
    if not isinstance(lesson, dict):
        return _empty_lesson(f"Урок {idx + 1}")
    out = _empty_lesson(f"Урок {idx + 1}")
    out.update(lesson)
    title = out.get("title")
    out["title"] = title.strip() if isinstance(title, str) and title.strip() else f"Урок {idx + 1}"
    for key in ("objectives", "key_terms", "quiz"):
        if not isinstance(out.get(key), list):
            out[key] = []
    for key in ("content", "practice", "homework"):
        if not isinstance(out.get(key), str):
            out[key] = "" if out.get(key) is None else str(out[key])
    if not isinstance(out.get("test"), dict):
        out["test"] = {}
    return out


def _plan_dict(plan):
    """Всегда вернуть plan как словарь с нормализованными уроками.

    ИИ иногда возвращает план строкой (или JSON-строкой), и обращения
    вида course["plan"].get("lessons") падали с
    «'str' object has no attribute 'get'». Если попалось не-словарь,
    пробуем разобрать как JSON, иначе отдаём пустой план.

    Дополнительно приводим «lessons» к списку словарей и «overview» к
    строке — иначе следующий уровень падал на .get() по элементу.
    """
    if isinstance(plan, dict):
        out = plan
    elif isinstance(plan, str):
        t = plan.strip()
        parsed = None
        if t.startswith("{"):
            try:
                cand = json.loads(t)
                if isinstance(cand, dict):
                    parsed = cand
            except Exception:
                swallow.note('education_module._plan_dict:L569')
                parsed = None
        out = parsed if parsed is not None else {"overview": plan}
    elif isinstance(plan, list):
        # совсем плохой ответ модели: список уроков вместо объекта
        out = {"lessons": plan}
    else:
        out = {"lessons": []}

    lessons = out.get("lessons")
    if isinstance(lessons, list):
        out["lessons"] = [_lesson_dict(l, i) for i, l in enumerate(lessons)]
    else:
        out["lessons"] = []

    overview = out.get("overview")
    if overview is None:
        out["overview"] = ""
    elif not isinstance(overview, str):
        out["overview"] = str(overview)

    if not isinstance(out.get("total_lessons"), int):
        try:
            out["total_lessons"] = int(out.get("total_lessons") or len(out["lessons"]))
        except (TypeError, ValueError):
            swallow.note('education_module._plan_dict:L594')
            out["total_lessons"] = len(out["lessons"])
    return out


def _course_lessons(course):
    """Безопасный доступ к урокам курса (plan всегда dict)."""
    if not isinstance(course, dict):
        return []
    lessons = _plan_dict(course.get("plan")).get("lessons", [])
    return lessons if isinstance(lessons, list) else []



# ── Profile & Template System ────────────────────────────────────
def load_profile(profile_id="universal"):
    pid = _safe_id(profile_id) or "universal"
    path = BASE_PROFILES_DIR / f"{pid}.json"
    if path.exists():
        try: return json.loads(path.read_text("utf-8"))
        except Exception as e:
            logger.warning(f"load_profile: ошибка чтения {path}: {e}")
    return {
        "id": pid, "name": pid.replace("_", " ").title(),
        "quiz_types": ["choice", "multiple", "fill_in", "ai_open"],
        "quiz_distribution": {"choice": 6, "multiple": 4, "fill_in": 2},
        "section_types": ["concept", "example", "practice", "summary"],
        "phase_prompts": {}, "template_ids": ["multiple_choice", "fill_in_blank", "ai_open"]
    }

def load_template(template_id):
    tid = _safe_id(template_id) or "multiple_choice"
    path = BASE_TEMPLATES_DIR / f"{tid}.json"
    if path.exists():
        try: return json.loads(path.read_text("utf-8"))
        except Exception as e:
            logger.warning(f"load_template: ошибка чтения {path}: {e}")
    return {"id": tid, "type": "choice", "prompt": "", "evaluation_prompt": ""}

def get_quiz_plan(profile_id, target_count):
    profile = load_profile(profile_id)
    dist = profile.get("quiz_distribution", {"choice": 3, "multiple": 2, "fill_in": 1})
    total_dist = sum(dist.values()) or 6
    ai_count = max(1, target_count // 5)
    remaining = target_count - ai_count
    plan = []
    for qtype, qcount in dist.items():
        count = max(1, round(remaining * qcount / total_dist))
        if count > 0: plan.append({"type": qtype, "count": count})
    total = sum(p["count"] for p in plan)
    if total < remaining and plan: plan[-1]["count"] += remaining - total
    plan.append({"type": "ai_open", "count": ai_count})
    return plan

def list_profiles():
    profiles = []
    if BASE_PROFILES_DIR.exists():
        for f in sorted(BASE_PROFILES_DIR.glob("*.json")):
            # Пропускать файлы банков тестов (*_tests.json)
            if f.name.endswith("_tests.json"):
                continue
            try:
                data = json.loads(f.read_text("utf-8"))
                profiles.append({"id": data.get("id", f.stem), "name": data.get("name", f.stem)})
            except:
                swallow.note('education_module.list_profiles:L659')
                profiles.append({"id": f.stem, "name": f.stem})
    if not profiles: profiles.append({"id": "universal", "name": "Universal"})
    return profiles


def _load_course(course_id):
    p = _course_path(course_id)
    if p is None:
        return None
    if p.exists():
        return json.loads(p.read_text("utf-8"))
    return None


# ========== PROFILE TEST BANKS (переиспользуемые банки тестов) ==========
def profile_tests_path(profile_id):
    pid = _safe_id(profile_id) or "universal"
    BASE_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    return BASE_PROFILES_DIR / f"{pid}_tests.json"


def list_profile_tests(profile_id):
    """Вернуть список сохранённых тестов банка профиля."""
    path = profile_tests_path(profile_id)
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            tests = data.get("tests", [])
            if isinstance(tests, list):
                return tests
        except Exception as e:
            logger.warning(f"list_profile_tests: ошибка {path}: {e}")
    return []


def save_profile_tests(profile_id, tests):
    """Добавить тесты в банк профиля (дедуп по тексту вопроса, лимит 200)."""
    if not isinstance(tests, list) or not tests:
        return {"ok": False, "error": "Пустой список тестов"}
    existing = list_profile_tests(profile_id)
    seen = set((t.get("question", "") or "").strip().lower() for t in existing if isinstance(t, dict))
    added = 0
    for t in tests:
        if not isinstance(t, dict):
            continue
        qt = (t.get("question", "") or "").strip()
        if not qt:
            continue
        key = qt.lower()
        if key in seen:
            continue
        seen.add(key)
        # Нормализуем обязательные поля
        t.setdefault("type", "choice")
        t.setdefault("options", t.get("options") or [])
        t.setdefault("correct", t.get("correct", 0))
        t.setdefault("explanation", "")
        t.setdefault("criteria", [])
        t = _normalize_data(t)
        if not _valid_q(t):
            continue
        # Стабильный id вопроса: <profile>_<номер в банке>. Существующие сохраняем.
        if not t.get("id"):
            t["id"] = f"{profile_id}_{len(existing) + 1}"
        existing.append(t)
        added += 1
    # Лимит банка
    existing = existing[-200:]
    path = profile_tests_path(profile_id)
    path.write_text(json.dumps({"profile_id": profile_id, "tests": existing}, ensure_ascii=False, indent=2), "utf-8")
    return {"ok": True, "added": added, "total": len(existing)}


def generate_profile_bank(profile_id, count=20, description=""):
    """Сгенерировать локальным ИИ банк из `count` интерактивных тестов для профиля.

    Возвращает список тестов (не сохраняет автоматически — сохранение через save_profile_tests).
    """
    profile = load_profile(profile_id)
    name = profile.get("name", profile_id)
    desc = profile.get("description", "") or ""
    qtypes = profile.get("quiz_types", ["choice", "multiple", "fill_in", "true_false", "ai_open"])
    type_hint = ", ".join(qtypes[:6])
    extra = f"\nДополнительные пожелания пользователя: {description}" if description else ""

    system = (
        "Ты — педагог-методист. Составь банк интерактивных тестов для учебного профиля. "
        "Ответь ТОЛЬКО JSON-массивом:\n"
        '[{"question": "...", "type": "choice", "options": ["A","B","C","D"], "correct": 0, "explanation": "..."}, '
        '{"question": "...", "type": "multiple", "options": ["A","B","C","D"], "correct": [0,2], "explanation": "..."}, '
        '{"question": "...", "type": "fill_in", "correct_answer": "...", "explanation": "..."}, '
        '{"question": "...", "type": "true_false", "correct": true, "explanation": "..."}, '
        '{"question": "...", "type": "matching", "pairs": [{"left":"термин","right":"определение"}], "explanation": "..."}, '
        '{"question": "...", "type": "ordering", "items": ["шаг1","шаг2"], "correct_order": [0,1], "explanation": "..."}, '
        '{"question": "...", "type": "ai_open", "criteria": ["критерий1"], "expected_points": 5, "hint": "подсказка"}]\n'
        f"Допустимые типы для этого профиля: {type_hint}."
    )
    user = (
        f"Профиль: {name}\n"
        f"Описание профиля: {desc}\n"
        f"Создай ровно {count} разнообразных тестов по тематике профиля, "
        f"равномерно распределяя типы вопросов. Каждый вопрос должен быть самодостаточным.\n"
        f"Для choice/multiple дай 3-5 вариантов, поле correct — индекс (или массив индексов). "
        f"Для fill_in — correct_answer (строка). Для true_false — correct (true/false). "
        f"Для ordering — correct_order (массив индексов правильного порядка)."
        f"{extra}\nТолько JSON-массив."
    )
    result = _call_ai(system, user, max_tokens=8192, temperature=0.7, timeout=180)
    tests = _extract_json_lenient(result)
    tests = tests if isinstance(tests, list) else []
    tests = [_normalize_data(t) for t in tests if isinstance(t, dict) and _valid_q(t)]
    return tests[:count]


def ensure_profile_bank(profile_id, count=20):
    """Гарантировать, что у профиля есть банк из `count` тестов (догенерировать при нехватке)."""
    existing = list_profile_tests(profile_id)
    if len(existing) >= count:
        return {"ok": True, "total": len(existing), "added": 0, "note": "уже заполнен"}
    need = count - len(existing)
    generated = generate_profile_bank(profile_id, need)
    res = save_profile_tests(profile_id, generated)
    return {"ok": True, "total": res.get("total", len(existing)), "added": res.get("added", 0)}


def _valid_q(q):
    """Валидация вопроса: непустой текст, корректные опции/индексы."""
    if not isinstance(q, dict):
        return False
    qt = q.get("question")
    if not isinstance(qt, str) or not qt.strip():
        return False
    if q.get("type") in ("choice", "multiple"):
        opts = q.get("options") or []
        if not isinstance(opts, list) or len(opts) < 2:
            return False
        if not all(isinstance(o, str) and o.strip() for o in opts):
            return False
        if q.get("type") == "choice":
            c = q.get("correct")
            if not isinstance(c, int) or c < 0 or c >= len(opts):
                return False
        else:
            cs = q.get("correct")
            if not isinstance(cs, list) or not cs or not all(isinstance(i, int) and 0 <= i < len(opts) for i in cs):
                return False
    return True


def _mix_profile_bank_into_quiz(profile_id, quiz, target, ratio=0.25):
    """Подмешать тесты из банка профиля в сгенерированный quiz (без дублей по тексту)."""
    bank = list_profile_tests(profile_id)
    if not bank or len(quiz) >= target:
        return quiz
    want = max(1, int(target * ratio))
    seen = set((q.get("question", "") or "").strip().lower() for q in quiz if isinstance(q, dict))
    added = 0
    for t in bank:
        if added >= want or len(quiz) >= target:
            break
        if not isinstance(t, dict):
            continue
        key = (t.get("question", "") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        quiz.append(t)
        added += 1
    return quiz


def add_lesson_tests(course_id, lesson_num, tests):
    """Добавить сгенерированные тесты в quiz конкретного урока и сохранить."""
    course = _load_course(course_id)
    if not course:
        return {"error": "Курс не найден"}
    lesson = get_lesson(course_id, lesson_num)
    if not lesson:
        return {"error": "Урок не найден"}
    idx = int(lesson_num)
    existing = lesson.get("quiz") or []
    seen = set((q.get("question", "") or "").strip().lower() for q in existing if isinstance(q, dict))
    added = 0
    for t in tests:
        if not isinstance(t, dict):
            continue
        key = (t.get("question", "") or "").strip().lower()
        if not key or key in seen:
            continue
        if not _valid_q(t):
            continue
        seen.add(key)
        existing.append(_normalize_data(t))
        added += 1
    lesson["quiz"] = existing
    course["plan"]["lessons"][idx] = lesson
    _save_course(course)
    return {"ok": True, "added": added, "total": len(existing)}

def _save_notes(course_id, notes_data):
    _ensure_dirs()
    p = _notes_path(course_id)
    if p is None:
        return
    p.write_text(json.dumps(notes_data, ensure_ascii=False, indent=2), "utf-8")

def _load_notes(course_id):
    p = _notes_path(course_id)
    if p is not None and p.exists():
        return json.loads(p.read_text("utf-8"))
    return {"notes": [], "overview": "", "total_chunks": 0, "processed_chunks": 0}

def list_courses():
    _ensure_dirs()
    courses = []
    for f in sorted(COURSES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text("utf-8"))
            plan = _plan_dict(data.get("plan"))
            lessons = plan.get("lessons", [])
            courses.append({
                "id": data["id"],
                "title": data.get("title", "Без названия"),
                "status": data.get("status", "unknown"),
                "query": data.get("user_query", ""),
                "created_at": data.get("created_at", 0),
                "progress": data.get("progress", {}),
                "materials_count": len(data.get("materials", [])),
                "lessons_count": len(lessons),
                "difficulty": data.get("difficulty", "beginner"),
                "error": data.get("error", ""),
                "ai_only": data.get("ai_only", False),
                "profile": data.get("profile", "universal"),
                "folder_id": data.get("folder_id") or None,
                "generation_progress": data.get("generation_progress", {})
            })
        except Exception as e:
            logger.warning(f"list_courses: ошибка чтения {f}: {e}")
    return courses


# ========== FOLDERS (группировка курсов) ==========

def _load_folders():
    """Загрузить список папок. Формат: [{"id": ..., "name": ..., "created_at": ...}, ...]"""
    try:
        if FOLDERS_PATH.exists():
            data = json.loads(FOLDERS_PATH.read_text("utf-8"))
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.warning(f"_load_folders: {e}")
    return []


def _save_folders(folders):
    FOLDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FOLDERS_PATH.write_text(json.dumps(folders, ensure_ascii=False, indent=2), "utf-8")


def list_folders():
    """Список папок с количеством курсов в каждой."""
    folders = _load_folders()
    counts = {}
    for c in list_courses():
        fid = c.get("folder_id")
        if fid:
            counts[fid] = counts.get(fid, 0) + 1
    result = []
    for f in folders:
        result.append({
            "id": f.get("id", ""),
            "name": f.get("name", "Папка"),
            "created_at": f.get("created_at", 0),
            "courses_count": counts.get(f.get("id", ""), 0)
        })
    return result


def create_folder(name):
    """Создать папку. Возвращает созданную папку."""
    _ensure_dirs()
    name = (name or "").strip()
    if not name:
        return {"error": "Укажите название папки"}
    if len(name) > 80:
        name = name[:80]
    folders = _load_folders()
    for f in folders:
        if f.get("name", "").lower() == name.lower():
            return {"error": "Папка с таким названием уже существует"}
    folder = {"id": str(uuid.uuid4()), "name": name, "created_at": _now()}
    folders.append(folder)
    _save_folders(folders)
    return {"ok": True, "folder": folder}


def rename_folder(folder_id, name):
    """Переименовать папку."""
    name = (name or "").strip()
    if not name:
        return {"error": "Укажите название папки"}
    if len(name) > 80:
        name = name[:80]
    folders = _load_folders()
    for f in folders:
        if f.get("id") == folder_id:
            f["name"] = name
            _save_folders(folders)
            return {"ok": True}
    return {"error": "Папка не найдена"}


def delete_folder(folder_id):
    """Удалить папку. Курсы из неё возвращаются в корень (без папки)."""
    folders = _load_folders()
    remaining = [f for f in folders if f.get("id") != folder_id]
    if len(remaining) == len(folders):
        return {"error": "Папка не найдена"}
    _save_folders(remaining)
    # Вернуть курсы этой папки в корень
    moved = 0
    for f in COURSES_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text("utf-8"))
            if data.get("folder_id") == folder_id:
                data["folder_id"] = None
                f.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
                moved += 1
        except Exception as e:
            logger.warning(f"delete_folder: ошибка обработки {f}: {e}")
    return {"ok": True, "courses_moved": moved}


def move_course(course_id, folder_id=None):
    """Переместить курс в папку (folder_id=None — в корень)."""
    course = _load_course(course_id)
    if not course:
        return {"error": "Курс не найден"}
    fid = folder_id or None
    if fid:
        folders = _load_folders()
        if not any(f.get("id") == fid for f in folders):
            return {"error": "Папка не найдена"}
    course["folder_id"] = fid
    course["updated_at"] = _now()
    _save_course(course)
    return {"ok": True}

def get_course(course_id):
    return _load_course(course_id)

def get_course_short(course_id):
    course = _load_course(course_id)
    if not course: return None
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    return {
        "id": course["id"],
        "title": course.get("title", ""),
        "status": course.get("status", ""),
        "query": course.get("user_query", ""),
        "created_at": course.get("created_at", 0),
        "progress": course.get("progress", {}),
        "materials_count": len(course.get("materials", [])),
        "lessons_count": len(lessons),
        "difficulty": course.get("difficulty", "beginner"),
        "error": course.get("error", ""),
        "settings": course.get("settings", {}),
        "plan": plan,
        "ai_only": course.get("ai_only", False),
        "profile": course.get("profile", "universal"),
        "chat_history": course.get("chat_history", [])[-20:],
        "generation_progress": course.get("generation_progress", {}),
    }

def get_full_lesson(course_id, lesson_num):
    """Return full lesson data including full content, homework, etc."""
    course = _load_course(course_id)
    if not course: return {"error": "Курс не найден"}
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    try: idx = int(lesson_num)
    except:
        swallow.note('education_module.get_full_lesson:L1044')
        return {"error": "Неверный номер урока"}
    if idx < 0 or idx >= len(lessons):
        return {"error": "Урок не найден"}
    l = lessons[idx]
    return {
        "title": l.get("title", ""),
        "content": l.get("content", ""),
        "objectives": l.get("objectives", []),
        "key_terms": l.get("key_terms", []),
        "test": l.get("test", {}),
        "quiz": l.get("quiz", []),
        "practice": l.get("practice", ""),
        "homework": l.get("homework", "")
    }

def get_lesson(course_id, lesson_num):
    course = _load_course(course_id)
    if not course or not course.get("plan"):
        return None
    lessons = _plan_dict(course.get("plan")).get("lessons", [])
    try:
        idx = int(lesson_num)
    except (ValueError, TypeError):
        swallow.note('education_module.get_lesson:L1068')
        return None
    if 0 <= idx < len(lessons):
        return lessons[idx]
    return None

def delete_course(course_id):
    p = _course_path(course_id)
    if p is None:
        return {"error": "Недопустимый ID курса"}
    if p.exists():
        p.unlink()
        np = _notes_path(course_id)
        if np is not None and np.exists(): np.unlink()
        return {"ok": True}
    return {"error": "Курс не найден"}

def delete_completed_courses():
    deleted = 0
    for f in COURSES_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text("utf-8"))
            if data.get("status") == "completed":
                f.unlink()
                np = _notes_path(data.get("id"))
                if np is not None and np.exists(): np.unlink()
                deleted += 1
        except Exception as e:
            logger.warning(f"delete_completed_courses: ошибка обработки {f}: {e}")
    return {"deleted": deleted}

def receive_material(material_data):
    _ensure_dirs()
    buffer = _load_buffer()
    buffer.append(_norm_material(material_data))
    _save_buffer(buffer)
    return {"ok": True, "count": len(buffer)}

def get_buffer_materials():
    return _load_buffer()

def clear_buffer():
    _ensure_dirs()
    BUFFER_PATH.write_text("[]", "utf-8")

# ── BUFFER (материалы, прикреплённые к будущему курсу) ────────────────
def _load_buffer():
    """Прочитать буфер материалов. Любая битая запись -> пустой буфер."""
    _ensure_dirs()
    if not BUFFER_PATH.exists():
        return []
    try:
        data = json.loads(BUFFER_PATH.read_text("utf-8"))
    except Exception as e:
        logger.warning(f"_load_buffer: битый буфер {BUFFER_PATH}: {e}")
        return []
    if isinstance(data, dict):
        # фронт иногда присылает {"materials": [...]}
        data = data.get("materials") or []
    if not isinstance(data, list):
        return []
    return [_norm_material(m, i) for i, m in enumerate(data)]


def _save_buffer(buffer):
    _ensure_dirs()
    if not isinstance(buffer, list):
        buffer = []
    BUFFER_PATH.write_text(json.dumps(buffer, ensure_ascii=False, indent=2), "utf-8")


def remove_buffer_item(index):
    buffer = _load_buffer()
    if 0 <= index < len(buffer):
        buffer.pop(index)
        _save_buffer(buffer)
        return {"ok": True}
    return {"error": "Index out of range"}

ALLOWED_EXTENSIONS = {'.txt', '.pdf', '.epub', '.fb2', '.docx', '.md'}

def upload_material(file_bytes, filename):
    _ensure_dirs()
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return {"error": f"Недопустимый формат: {ext}. Разрешены: {', '.join(sorted(ALLOWED_EXTENSIONS))}"}
    if len(file_bytes) > 100 * 1024 * 1024:
        return {"error": "Файл слишком большой (макс. 100 МБ)"}
    safe_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOADS_DIR / safe_name
    dest.write_bytes(file_bytes)
    material = {"type": "book", "path": str(dest), "title": filename, "file": str(dest)}
    buffer = _load_buffer()
    buffer.append(material)
    _save_buffer(buffer)
    return {"ok": True, "count": len(buffer), "material": material}

# ========== COURSE GENERATION (PHASED, INCREMENTAL) ==========

def create_course(materials, user_query, page_range=None, difficulty="beginner", ai_only=False,
                  num_lessons=None, tests_per_lesson=None, profile_id="universal", content_length="medium",
                  user_notes="", folder_id=None):
    _ensure_dirs()
    course_id = str(uuid.uuid4())
    course = {
        "id": course_id,
        "title": user_query[:80] if user_query else "Новый курс",
        "user_query": user_query or "",
        "user_notes": user_notes or "",
        "difficulty": difficulty,
        "ai_only": ai_only,
        "folder_id": folder_id or None,
        "settings": {
            "num_lessons": _clamp(num_lessons, 1, 100) if num_lessons else None,
            "tests_per_lesson": _clamp(tests_per_lesson, 1, 50) if tests_per_lesson else None,
            "content_length": content_length or "medium"
        },
        "profile": profile_id or "universal",
        "status": "queued",
        "created_at": _now(),
        "updated_at": _now(),
        "materials": _norm_materials(materials),
        "page_range": page_range or {},
        "analysis": "",
        "plan": None,
        "progress": {
            "current_lesson": 0,
            "completed_lessons": [],
            "total_score": 0,
            "max_score": 100,
            "quiz_scores": [],
            "exam_score": None,
            "exam_passed": False
        },
        "generation_progress": {
            "phase": "",
            "total_lessons": 0,
            "lessons_done": 0,
            "notes_done": 0,
            "notes_total": 0
        },
        "chat_history": [],
        "error": ""
    }
    _save_course(course)
    _save_notes(course_id, {"notes": [], "overview": "", "total_chunks": 0, "processed_chunks": 0})
    return course_id

def _clamp(val, lo, hi):
    if val is None: return None
    try: return max(lo, min(hi, int(val)))
    except:
        swallow.note('education_module._clamp:L1220')
        return lo

def _set_phase(course_id, phase, total_lessons=0, lessons_done=0):
    course = _load_course(course_id)
    if not course: return
    gp = course.setdefault("generation_progress", {})
    gp["phase"] = phase
    if total_lessons: gp["total_lessons"] = total_lessons
    if lessons_done: gp["lessons_done"] = lessons_done
    course["updated_at"] = _now()
    _save_course(course)
    return course

def pause_course(course_id):
    course = _load_course(course_id)
    if not course: return False
    if course.get("status") in ("ready", "completed", "error", "paused"): return False
    course["status"] = "paused"
    course["updated_at"] = _now()
    _save_course(course)
    return True

def resume_course(course_id):
    course = _load_course(course_id)
    if not course: return False
    if course.get("status") != "paused": return False
    course["status"] = "analyzing"
    course["updated_at"] = _now()
    _save_course(course)
    return True

# ── Phase: Extract notes from materials ──────────────────────────
def _phase_notes(course, course_id, query, materials) -> str:
    """Читает материалы, извлекает заметки через AI. Возвращает notes_text."""
    materials = _norm_materials(materials)
    gp = course.setdefault("generation_progress", {})
    gp["phase"] = "notes"
    gp["notes_done"] = 0
    gp["notes_total"] = 0
    _save_course(course)

    notes_data = _load_notes(course_id)
    all_chunks = []
    chunk_sources = []

    for i, m in enumerate(materials):
        text = _extract_material_text(m)
        title = m.get("title", f"Материал {i+1}")
        if not text or text.startswith("[Ошибка") or text.startswith("[Файл"):
            continue
        chunks = _chunk_text(text)
        for ci, chunk in enumerate(chunks):
            all_chunks.append(chunk)
            chunk_sources.append({"title": title, "idx": i, "chunk": ci})

    if not all_chunks:
        return ""

    total = len(all_chunks)
    gp["notes_total"] = total
    extracted_notes = []
    MAX_NOTES_CHARS = 25000

    for ci, (chunk, src) in enumerate(zip(all_chunks, chunk_sources)):
        sys_p = "Ты — AI-репетитор. Извлеки ключевую информацию: определения, правила, примеры, факты. Формат: короткие заметки на русском."
        user_p = f"Контекст: {query}\n\nТекст (часть {ci+1}/{total}) из «{src['title']}»:\n\n{chunk}\n\nИзвлеки учебную информацию:"
        note = _call_ai(sys_p, user_p, max_tokens=2048, temperature=0.3)
        if note.strip():
            extracted_notes.append({"source": src["title"], "chunk": ci, "text": note, "timestamp": _now()})
        gp["notes_done"] = ci + 1
        notes_data["notes"] = extracted_notes
        notes_data["processed_chunks"] = ci + 1
        _save_notes(course_id, notes_data)

    notes_text = "\n\n".join([f"[{n['source']}]: {n['text']}" for n in extracted_notes])
    if len(notes_text) > MAX_NOTES_CHARS:
        notes_text = notes_text[:MAX_NOTES_CHARS] + "\n\n[текст сокращён]"
    notes_data["overview"] = notes_text
    _save_notes(course_id, notes_data)
    return notes_text


# ── Phase: Extract notes v2 (Map-Reduce for 3+ materials) ────────
def _phase_notes_v2(course, course_id, query, materials) -> str:
    """Map-Reduce: per-material summary (parallel) → combined overview. For 3+ materials."""
    materials = _norm_materials(materials)
    gp = course.setdefault("generation_progress", {})
    gp["phase"] = "notes_v2"
    gp["notes_done"] = 0
    gp["notes_total"] = len(materials)
    _save_course(course)

    notes_data = _load_notes(course_id)

    # ── MAP: per-material summary (parallel via _pool) ──
    def _summarize_one(material):
        title = material.get("title", "Материал")
        text = _extract_material_text(material)
        if not text or text.startswith("[Ошибка") or text.startswith("[Файл"):
            return None
        chunks = _chunk_text(text)
        if not chunks:
            return None
        if len(chunks) == 1 and len(text) < 8000:
            sys_p = "Ты — методист. Составь краткий конспект материала: ключевые определения, правила, примеры, факты. Не более 2000 символов."
            user_p = f"Тема: {query}\n\nМатериал «{title}»:\n\n{text[:8000]}\n\nКонспект:"
            summary = _call_ai(sys_p, user_p, max_tokens=2048, temperature=0.3)
            return {"title": title, "summary": summary[:3000], "full": False} if summary.strip() else None
        chunk_summaries = []
        for ci, chunk in enumerate(chunks):
            sp = "Ты — методист. Извлеки ключевые факты, определения, примеры из фрагмента текста. Кратко."
            up = f"Тема: {query}\n\nТекст (ч. {ci+1}/{len(chunks)}) из «{title}»:\n\n{chunk}\n\nКлючевая информация:"
            note = _call_ai(sp, up, max_tokens=2048, temperature=0.3)
            if note.strip():
                chunk_summaries.append(note)
        if not chunk_summaries:
            return None
        combined_chunks = "\n\n".join(chunk_summaries)
        reduce_p = f"Тема: {query}\nМатериал: {title}\n\nВыдержки:\n{combined_chunks[:12000]}\n\nСоставь единый связный конспект (не более 3000 символов):"
        final = _call_ai("Ты — методист. Объедини выдержки в связный конспект.", reduce_p, max_tokens=3072, temperature=0.3)
        return {"title": title, "summary": final[:3000], "full": True} if final.strip() else None

    futures = []
    for m in materials:
        futures.append(_pool.submit(_summarize_one, m))
    summaries = []
    for i, f in enumerate(futures):
        try:
            result = f.result(timeout=300)
            if result:
                summaries.append(result)
        except Exception as e:
            logger.warning(f"_phase_notes_v2: ошибка материала {i}: {e}")
        gp["notes_done"] = i + 1
        _save_course(course)

    if not summaries:
        return ""

    # ── REDUCE: combine all summaries ──
    combined = "\n\n".join([f"[{s['title']}]: {s['summary']}" for s in summaries])
    notes_data["notes"] = [{"source": s["title"], "text": s["summary"], "timestamp": _now()} for s in summaries]
    notes_data["overview"] = combined
    notes_data["processed_chunks"] = len(summaries)
    notes_data["total_chunks"] = len(materials)
    _save_notes(course_id, notes_data)

    if len(combined) > 50000:
        reduce_sys = "Ты — методист. Составь общий конспект по всем материалам курса. Выдели общие темы, ключевые концепции."
        reduce_usr = f"Тема курса: {query}\n\nКонспекты материалов:\n{combined[:40000]}\n\nСоставь единый обзор (не более 5000 символов):"
        overview = _call_ai(reduce_sys, reduce_usr, max_tokens=4096, temperature=0.3)
        if overview.strip():
            combined = overview[:5000]
            notes_data["overview"] = combined
            _save_notes(course_id, notes_data)
    return combined


# ── Phase 0: Syllabus (overview + lesson titles) ─────────────────
def _phase_syllabus(course, course_id, query, difficulty, notes_text) -> int:
    """Генерирует overview и список уроков (только titles). Возвращает количество уроков."""
    diff_map = {"beginner": "для начинающих", "intermediate": "среднего уровня", "advanced": "продвинутый уровень"}
    diff_desc = diff_map.get(difficulty, "для начинающих")

    settings = course.get("settings") or {}
    want = settings.get("num_lessons")
    lesson_count_hint = f" из {want} уроков" if want else " из 5-10 уроков"
    profile = load_profile(course.get("profile", "universal"))
    prof_prompt = profile.get("phase_prompts", {}).get("syllabus", "")
    if prof_prompt: system = prof_prompt
    if want:
        lesson_range = f"Ровно {want} уроков."
    else:
        lesson_range = "Составь план курса из 5-10 уроков."

    system = (
        "Ты — профессиональный методист. Составь план учебного курса. "
        "Ответь ТОЛЬКО JSON, без лишнего текста.\n"
        '{"overview": "описание курса (2-3 абзаца)", "total_lessons": 8, '
        '"estimated_hours": 4, "difficulty": "' + difficulty + '", '
        '"lessons": [{"title": "Название урока"}, ...]}'
    )
    notes_ctx = f"Заметки из материалов:\n{notes_text[:15000]}\n\n" if notes_text else ""
    user_notes = course.get("user_notes", "")
    user_notes_ctx = f"Пожелания пользователя: {user_notes}\n\n" if user_notes else ""
    user = (
        f"Тема: {query}\nУровень: {diff_desc}\n"
        f"{notes_ctx}"
        f"{user_notes_ctx}"
        f"{lesson_range} Только JSON."
    )
    result = _call_ai(system, user, max_tokens=4096, temperature=0.7, timeout=180)
    plan = _extract_json_lenient(result)
    plan = plan if isinstance(plan, dict) else {"overview": "Курс", "total_lessons": 1, "lessons": [{"title": query[:80]}]}

    lessons = plan.get("lessons", [{"title": query[:80]}])
    if want:
        if len(lessons) < want:
            for i in range(len(lessons), want):
                lessons.append({"title": f"Урок {i+1}"})
        total = want
    else:
        total = min(len(lessons), 15)
    total = min(total, 100)
    lessons = lessons[:total]

    # Build empty lesson placeholders
    full_lessons = []
    for i, l in enumerate(lessons):
        # ИИ иногда отдаёт уроки просто списком строк, а не объектами.
        # Раньше l.get("title") на строке падал с
        # «'str' object has no attribute 'get'» и генерация обрывалась.
        if isinstance(l, dict):
            title = l.get("title") or f"Урок {i+1}"
        elif isinstance(l, str):
            title = l.strip() or f"Урок {i+1}"
        else:
            title = f"Урок {i+1}"
        full_lessons.append({
            "title": title,
            "content": "", "objectives": [], "key_terms": [],
            "test": {}, "quiz": [], "practice": "", "homework": ""
        })

    course = _load_course(course_id) or course
    course["plan"] = {
        "overview": plan.get("overview", "Курс"),
        "total_lessons": total,
        "difficulty": difficulty,
        "estimated_hours": plan.get("estimated_hours", 4),
        "lessons": full_lessons
    }
    course["progress"]["max_score"] = total * 10 + total * 5
    _set_phase(course_id, "syllabus", total)
    _save_course(course)
    return total

# ── Phase 1: Lessons (content, objectives, key_terms, practice) ──
def _phase_lesson(course, course_id, query, notes_text, lesson_idx, lesson):
    """Генерирует content для одного урока."""
    prev_summary = ""
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    if lesson_idx > 0 and lessons:
        prev = lessons[lesson_idx - 1]
        prev_summary = f"\nПредыдущий урок: {prev.get('title', '')} — {prev.get('content', '')[:500]}"

    settings = course.get("settings") or {}
    cl = settings.get("content_length", "medium")
    length_map = {"short": ("краткий (400-600 слов)", 4096, 4000),
                  "medium": ("подробный (800-1200 слов)", 6144, 8000),
                  "long": ("очень подробный (1500-2500 слов)", 8192, 16000),
                  "huge": ("максимально полный (3000-5000 слов)", 12288, 30000)}
    length_desc, max_tok, max_chars = length_map.get(cl, length_map["medium"])

    notes_ctx = f"Заметки из материалов:\n{notes_text[:10000]}\n\n" if notes_text else ""
    system = (
        "Ты — преподаватель-эксперт. Напиши содержание одного урока. "
        "Ответь ТОЛЬКО JSON:\n"
        '{"content": "подробное объяснение", '
        '"objectives": ["цель1", "цель2"], '
        '"key_terms": [{"term": "термин", "definition": "определение"}], '
        '"practice": "практическое задание"}'
    )
    user_notes = course.get("user_notes", "")
    user_notes_ctx = f"\nПожелания пользователя: {user_notes}" if user_notes else ""
    user = (
        f"Курс: {query}\nУрок {lesson_idx+1}: {lesson.get('title', '')}\n"
        f"{notes_ctx}{prev_summary}{user_notes_ctx}\n"
        f"Напиши {length_desc} урок с примерами. Разбей на разделы, используй форматирование."
    )
    result = _call_ai(system, user, max_tokens=max_tok, temperature=0.7, timeout=180)
    data = _extract_json_lenient(result)
    data = data if isinstance(data, dict) else {}

    content = data.get("content") or ""
    if not content or len(content) < 20:
        m = re.search(r'"content"\s*:\s*"(.*?)(?<!\\)"(?=\s*[,}])', result, re.DOTALL)
        if m:
            content = m.group(1)
        else:
            content = _sanitize_content(result)

    # Ретраи: если контент слишком короткий (обрыв) — пробуем запросить ещё раз.
    tries = 0
    while (not content or len(content) < 400) and tries < 2:
        tries += 1
        short_system = (
            "Ты — преподаватель-эксперт. Напиши содержание одного урока. "
            "Отвечай ТОЛЬКО валидным JSON без markdown-обёртки и комментариев.\n"
            '{"content": "подробное объяснение (НЕ менее 400 символов, используй \\n для переносов, '
            'заголовки с # и **жирный** текст)", '
            '"objectives": ["цель1", "цель2"], '
            '"key_terms": [{"term": "термин", "definition": "определение"}], '
            '"practice": "практическое задание"}'
        )
        short_user = (
            f"Курс: {query}\nУрок {lesson_idx+1}: {lesson.get('title', '')}\n"
            f"Замечание: предыдущий ответ был слишком коротким ({len(content) or 0} симв.). "
            f"Напиши {length_desc} урок с примерами. Разбей на разделы, используй форматирование. "
            f"Минимум 400 символов в поле content. Только JSON."
        )
        try:
            result2 = _call_ai(short_system, short_user, max_tokens=max_tok, temperature=0.6, timeout=180)
        except Exception:
            swallow.note('education_module._phase_lesson:L1526')
            break
        data2 = _extract_json_lenient(result2)
        data2 = data2 if isinstance(data2, dict) else {}
        c2 = data2.get("content") or ""
        if not c2 or len(c2) < 20:
            m2 = re.search(r'"content"\s*:\s*"(.*?)(?<!\\)"(?=\s*[,}])', result2, re.DOTALL)
            if m2:
                c2 = m2.group(1)
        if c2 and len(c2) >= len(content):
            content = c2
            data = data2

    content = _normalize_text(content)
    if not content or len(content) < 20:
        content = _normalize_text(_sanitize_content(result))

    lesson["content"] = content[:max_chars]
    lesson["objectives"] = _normalize_data(data.get("objectives", []))
    if not isinstance(lesson["objectives"], list) or not lesson["objectives"]:
        lesson["objectives"] = _fallback_objectives(lesson.get("title", ""), content)
    lesson["key_terms"] = data.get("key_terms", [])
    if not isinstance(lesson["key_terms"], list) or not lesson["key_terms"]:
        lesson["key_terms"] = _fallback_key_terms(lesson.get("title", ""), content)
    # Классификация урока (предмет/сложность) — опционально, laya.
    if _laya_education_enabled():
        _cls = _laya_classify_lesson(lesson.get("title", ""), content)
        if _cls:
            lesson["classification"] = _cls
    lesson["practice"] = _normalize_text(data.get("practice", ""))[:2000]
    if not lesson["practice"]:
        practice_hdr = re.search(r'^#{1,6}\s*(?:Практик|Задани|Упражн)[^\n]*\n(.*?)(?=\n#|\Z)', content, re.M | re.S)
        lesson["practice"] = _normalize_text(practice_hdr.group(1))[:1500] if practice_hdr else ""

    course["plan"]["lessons"][lesson_idx] = lesson
    gp = course.setdefault("generation_progress", {})
    gp["lessons_done"] = lesson_idx + 1
    course["updated_at"] = _now()
    _save_course(course)
    return lesson

# ── Phase 2: Quizzes ─────────────────────────────────────────────
def _phase_quiz(course, course_id, lesson_idx, lesson):
    """Генерирует quiz-вопросы для одного урока (20+ вопросов, AI open type)."""
    settings = course.get("settings") or {}
    tests_want = settings.get("tests_per_lesson") or 20
    target = _clamp(tests_want, 5, 50) or 20
    profile_id = course.get("profile", "universal")
    quiz_plan = get_quiz_plan(profile_id, target)
    generated = []
    seen_questions = set()
    existing = lesson.get("quiz") or []
    for q in existing:
        qt = q.get("question", "")[:60]
        if qt: seen_questions.add(qt)

    lesson_content = lesson.get("content", "")[:5000]
    content_sections = lesson.get("content_sections", [])
    extra_ctx = ""
    if content_sections:
        section_titles = [s.get("title", "") for s in content_sections[:5]]
        extra_ctx = "\nРазделы урока: " + ", ".join(section_titles)

    def _valid_q(q):
        """Валидация вопроса: непустой текст, корректные опции/индексы."""
        if not isinstance(q, dict):
            return False
        qt = q.get("question")
        if not isinstance(qt, str) or not qt.strip():
            return False
        if q.get("type") in ("choice", "multiple"):
            opts = q.get("options") or []
            if not isinstance(opts, list) or len(opts) < 2:
                return False
            if not all(isinstance(o, str) and o.strip() for o in opts):
                return False
            if q.get("type") == "choice":
                c = q.get("correct")
                if not isinstance(c, int) or c < 0 or c >= len(opts):
                    return False
            else:
                cs = q.get("correct")
                if not isinstance(cs, list) or not cs or not all(isinstance(i, int) and 0 <= i < len(opts) for i in cs):
                    return False
        return True

    consecutive_fails = 0
    for item in quiz_plan:
        if consecutive_fails >= 2:
            break
        qtype = item["type"]
        qcount = item["count"]
        for batch_start in range(0, qcount, 10):
            if consecutive_fails >= 2:
                break
            batch_count = min(5, qcount - batch_start)
            retries = 0
            while retries < 2:
                retries += 1
                prompt = _build_quiz_prompt(qtype, batch_count, lesson_content, extra_ctx, seen_questions)
                try:
                    result = _call_ai(prompt["system"], prompt["user"], max_tokens=4096, temperature=0.5, timeout=90)
                    batch = _extract_json_lenient(result)
                    if isinstance(batch, list) and len(batch) > 0:
                        break
                except Exception:
                    swallow.note('education_module._phase_quiz:L1632')
                    batch = None
                    continue
            if isinstance(batch, list) and len(batch) > 0:
                pure = []
                for q in batch:
                    if not isinstance(q, dict):
                        continue
                    q.setdefault("type", qtype)
                    q.setdefault("options", q.get("options") or [])
                    q.setdefault("correct", q.get("correct", 0))
                    q.setdefault("explanation", "")
                    q.setdefault("criteria", [])
                    q = _normalize_data(q)
                    qtext = q.get("question", "")
                    if not _valid_q(q):
                        continue
                    key = qtext[:60]
                    if key and key not in seen_questions:
                        seen_questions.add(key)
                        pure.append(q)
                if pure:
                    consecutive_fails = 0
                    generated.extend(pure)
                else:
                    consecutive_fails += 1
            else:
                consecutive_fails += 1

    if len(generated) < 5:
        fallback = _generate_fallback_quiz(lesson_content, lesson.get("title", ""))
        generated.extend(fallback)

    # Нормализация финального списка
    generated = [_normalize_data(q) for q in generated if isinstance(q, dict)]
    # Подмешать переиспользуемые тесты из банка профиля (если есть)
    generated = _mix_profile_bank_into_quiz(profile_id, generated, target)
    lesson["quiz"] = generated[:target]
    course["plan"]["lessons"][lesson_idx] = lesson
    course["updated_at"] = _now()
    _save_course(course)


def _build_quiz_prompt(qtype, count, content, extra_ctx, seen_questions):
    """Build system/user prompt for a specific quiz type."""
    seen_hint = ""
    if seen_questions:
        slist = list(seen_questions)[-10:]
        seen_hint = "\nНЕ повторяй эти вопросы: " + " | ".join(slist)

    if qtype == "ai_open":
        sys_p = (
            "Ты — педагог. Создай вопросы открытого типа, требующие развёрнутого ответа. "
            "Ответь ТОЛЬКО JSON-массив:\n"
            '[{"question": "вопрос", "type": "ai_open", '
            '"criteria": ["критерий1", "критерий2"], '
            '"expected_points": 5, '
            '"hint": "подсказка"}, ...]')
        usr = f"Тема:\n{content[:3000]}{extra_ctx}\n\nСоздай {count} открытых вопросов.{seen_hint}"
        return {"system": sys_p, "user": usr}

    sys_p = "Ты — педагог. Создай тестовые вопросы. Ответь ТОЛЬКО JSON-массив:\n"
    if qtype == "choice":
        sys_p += '[{"question": "...", "type": "choice", "options": ["A","B","C","D"], "correct": 0, "explanation": "..."}, ...]'
    elif qtype == "multiple":
        sys_p += '[{"question": "...", "type": "multiple", "options": ["A","B","C","D"], "correct": [0,2], "explanation": "..."}, ...]'
    elif qtype == "fill_in":
        sys_p += '[{"question": "...", "type": "fill_in", "correct_answer": "...", "explanation": "..."}, ...]'
    elif qtype == "matching":
        sys_p += '[{"question": "...", "type": "matching", "pairs": [{"left": "термин", "right": "определение"}], "explanation": "..."}, ...]'
    elif qtype == "ordering":
        sys_p += '[{"question": "...", "type": "ordering", "items": ["шаг1", "шаг2"], "correct_order": [0,1], "explanation": "..."}, ...]'
    elif qtype == "true_false":
        sys_p += '[{"question": "...", "type": "true_false", "correct": true, "explanation": "..."}, ...]'
    else:
        sys_p += '[{"question": "...", "type": "choice", "options": ["A","B","C","D"], "correct": 0, "explanation": "..."}, ...]'
    usr = f"Тема урока:\n{content[:4000]}{extra_ctx}\n\nСоздай {count} вопросов.{seen_hint}"
    return {"system": sys_p, "user": usr}


def _generate_fallback_quiz(content, title):
    """Generate minimal quiz when AI fails."""
    words = _sanitize_content(content).split()[:10] if content else []
    topic = title[:50] or "тему"
    return [
        {"question": f"Какова основная идея темы «{topic}»?", "type": "choice", "options": [f"Идея 1: {words[i] if i < len(words) else 'Вариант ' + str(i+1)}" for i in range(4)], "correct": 0, "explanation": "Проверьте определение в начале урока."},
        {"question": f"Верно ли, что «{topic}» — это ключевая тема урока?", "type": "true_false", "correct": True, "explanation": "Да, это центральная тема урока."},
        {"question": f"Объясните своими словами: что такое «{topic}»? Приведите пример.", "type": "ai_open", "criteria": ["Понимание ключевой идеи", "Пример из жизни или учёбы", "Логичность объяснения"], "expected_points": 5, "hint": f"Подумайте, что включает в себя понятие «{topic}»."},
        {"question": f"«{topic}» — это _____", "type": "fill_in", "correct_answer": topic, "explanation": "Это название изучаемой темы."},
        {"question": f"Какие аспекты включает тема «{topic}»?", "type": "multiple", "options": ["Основные определения", "Практические примеры", "Связанные концепции", "Исторические факты"], "correct": [0, 1, 2], "explanation": "Тема включает определения, примеры и связанные концепции."},
    ]

# ── Phase 3: Homework ────────────────────────────────────────────
def _phase_homework(course, course_id, lesson_idx, lesson):
    """Generates expanded homework + practice data for one lesson."""
    system = (
        "Ты — педагог. Составь расширенное домашнее задание. "
        "Ответь ТОЛЬКО JSON:\n"
        '{"exercises": [{"title": "Упражнение 1", "description": "описание", "expected_answer": "..."}], '
        '"mini_project": {"title": "...", "steps": ["шаг1"], "evaluation_criteria": ["критерий"]}, '
        '"case_studies": [{"scenario": "...", "questions": ["вопрос1"]}], '
        '"discussion_questions": ["вопрос1"], '
        '"homework_text": "полный текст ДЗ для отображения"}'
    )
    ctx = lesson.get("content", "")[:3000]
    content_sections = lesson.get("content_sections", [])
    section_summary = ""
    if content_sections:
        section_summary = "\nРазделы: " + ", ".join([s.get("title", "") for s in content_sections[:8]])

    user = (
        f"Урок: {lesson.get('title', '')}\n\n"
        f"Содержание:\n{ctx}\n{section_summary}\n\n"
        f"Составь расширенное ДЗ: 3-5 упражнений, 1 мини-проект, 1-2 кейса, "
        f"вопросы для обсуждения. Укажи ожидаемый формат ответа."
    )
    result = _call_ai(system, user, max_tokens=4096, temperature=0.5, timeout=90)
    data = _extract_json_lenient(result)
    data = data if isinstance(data, dict) else {}

    hw_text = data.get("homework_text") if data else ""
    if hw_text:
        hw_text = _normalize_text(hw_text)
    if not hw_text:
        parts = []
        if data:
            exercises = data.get("exercises", [])
            mini_project = data.get("mini_project", {})
            case_studies = data.get("case_studies", [])
            discussion = data.get("discussion_questions", [])
            if exercises:
                parts.append("Упражнения:")
                for i, ex in enumerate(exercises):
                    title = ex.get("title", f"Упражнение {i+1}") if isinstance(ex, dict) else ""
                    desc = ex.get("description", "") if isinstance(ex, dict) else ""
                    ans = ex.get("expected_answer", "") if isinstance(ex, dict) else ""
                    parts.append(f"{i+1}. {title}")
                    if desc: parts.append(f"   {desc}")
                    if ans: parts.append(f"   Ответ: {ans}")
            if mini_project and isinstance(mini_project, dict):
                mt = mini_project.get("title", "")
                parts.append(f"\nМини-проект: {mt}")
                for s in mini_project.get("steps", []):
                    parts.append(f"  - {s}")
            if case_studies:
                parts.append("\nКейсы:")
                for cs in case_studies:
                    if not isinstance(cs, dict):
                        continue
                    parts.append(f"  {cs.get('scenario', '')}")
                    for q in cs.get("questions", []):
                        parts.append(f"    - {q}")
            if discussion:
                parts.append("\nВопросы для обсуждения:")
                for q in discussion:
                    parts.append(f"  • {q}")
        if parts:
            hw_text = "\n".join(parts)
        else:
            hw_text = _sanitize_content(result)
            if len(hw_text) < 20:
                hw_text = _normalize_text(result)

    if not hw_text or len(hw_text) < 30:
        hw_text = (
            f"Домашнее задание по теме «{lesson.get('title', '')}»:\n\n"
            f"1. Повторите основные понятия урока.\n"
            f"2. Выполните упражнения из раздела «Практика»: {lesson.get('practice', '')[:300]}\n"
            f"3. Ответьте на вопросы теста в конце урока.\n"
            f"4. Придумайте собственный пример по теме и запишите его решение."
        )

    lesson["homework"] = _normalize_text(hw_text)[:5000]
    lesson["practice_data"] = _normalize_data({
        "exercises": data.get("exercises", []),
        "mini_project": data.get("mini_project", {}),
        "case_studies": data.get("case_studies", []),
        "discussion_questions": data.get("discussion_questions", [])
    })
    course["plan"]["lessons"][lesson_idx] = lesson
    course["updated_at"] = _now()
    _save_course(course)

# ── Phase 4: Final Exam ──────────────────────────────────────────
def _phase_exam(course, course_id):
    """Генерирует финальный экзамен по всем урокам."""
    lessons = _plan_dict(course.get("plan")).get("lessons", [])
    if not lessons:
        return
    try:
        summaries = "\n".join([
            f"Урок {i+1}. {l.get('title','')}: {(l.get('content') or '')[:300]}"
            for i, l in enumerate(lessons)
        ])
        system = (
            "Ты — педагог. Создай финальный экзамен по всему курсу. "
            "Ответь ТОЛЬКО JSON-массив:\n"
            '[{"question": "...", "type": "choice", '
            '"options": ["A","B","C","D"], "correct": 0, '
            '"explanation": "..."}, ...]\n'
            "1-2 вопроса на каждый урок."
        )
        user = f"Курс: {course.get('user_query', '')}\n\nУроки:\n{summaries[:8000]}\n\nСоздай экзамен."
        result = _call_ai(system, user, max_tokens=4096, temperature=0.5, timeout=90)
        questions = _extract_json_lenient(result)
        questions = questions if isinstance(questions, list) else []
        questions = [_normalize_data(q) for q in questions if isinstance(q, dict) and q.get("question")]
        course["plan"]["final_exam"] = {"questions": questions}
    except Exception as e:
        logger.warning(f"[education] _phase_exam: {e}")
        course["plan"]["final_exam"] = {"questions": []}
    course["updated_at"] = _now()
    _save_course(course)


def _phase_bibliography(course, course_id, query, notes_text, materials):
    """Generates bibliography / sources for the course."""
    try:
        system = (
            "Ты — библиограф. Составь список литературы и источников для курса. "
            "Ответь ТОЛЬКО JSON:\n"
            '{"sources_used": [{"title": "...", "author": "...", "type": "book|article|video"}], '
            '"recommended_reading": [{"title": "...", "author": "...", "reason": "..."}], '
            '"online_resources": [{"title": "...", "url": "...", "description": "..."}]}'
        )
        notes_ctx = f"Заметки из материалов:\n{notes_text[:5000]}\n\n" if notes_text else ""
        user = (
            f"Курс: {query}\n{notes_ctx}"
            f"Составь список использованных источников, рекомендованной литературы и онлайн-ресурсов."
        )
        result = _call_ai(system, user, max_tokens=3072, temperature=0.4, timeout=90)
        data = _extract_json_lenient(result)
        data = data if isinstance(data, dict) else {}
        course["plan"]["bibliography"] = _normalize_data({
            "sources_used": data.get("sources_used", []),
            "recommended_reading": data.get("recommended_reading", []),
            "online_resources": data.get("online_resources", [])
        })
    except Exception as e:
        logger.warning(f"[education] _phase_bibliography: {e}")
        course["plan"]["bibliography"] = {
            "sources_used": [],
            "recommended_reading": [],
            "online_resources": []
        }
    course["updated_at"] = _now()
    _save_course(course)


async def _backfill_missing_parts(course, course_id, query, notes_text):
    """Дозаполнить недостающие блоки (контент/тест/ДЗ/карточки) у всех уроков.

    Вызывается после основного цикла генерации, чтобы гарантировать,
    что у каждого урока есть контент, тест, ДЗ и карточки, даже если
    отдельные запросы падали или обрывались.
    """
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    if not lessons:
        return
    try:
        for idx in range(len(lessons)):
            current = _load_course(course_id) or {}
            ls = _plan_dict(current.get("plan")).get("lessons", [])
            if idx >= len(ls):
                break
            l = ls[idx]
            # 1. Контент урока
            if not l.get("content") or len(l.get("content") or "") < 100:
                try:
                    await asyncio.to_thread(_phase_lesson, current, course_id, query, notes_text, idx, l)
                except Exception as e:
                    server.logger.warning(f"[education] backfill контент урок {idx+1}: {e}")
                await asyncio.to_thread(_save_course, current)
            elif isinstance(l.get("content"), str) and ("\\n" in l["content"] or "\\\"" in l["content"]):
                l["content"] = _normalize_text(l["content"])
                await asyncio.to_thread(_save_course, current)
            # 2. Тест
            if not l.get("quiz"):
                try:
                    await asyncio.to_thread(_phase_quiz, current, course_id, idx, l)
                except Exception as e:
                    server.logger.warning(f"[education] backfill тест урок {idx+1}: {e}")
                await asyncio.to_thread(_save_course, current)
            # 3. ДЗ и практика
            if not l.get("homework") and not l.get("practice_data"):
                try:
                    await asyncio.to_thread(_phase_homework, current, course_id, idx, l)
                except Exception as e:
                    server.logger.warning(f"[education] backfill ДЗ урок {idx+1}: {e}")
                await asyncio.to_thread(_save_course, current)
            # 4. Карточки
            if not l.get("flashcards"):
                try:
                    await asyncio.to_thread(_regenerate_flashcards_internal, current, course_id, idx)
                except Exception as e:
                    server.logger.warning(f"[education] backfill карточки урок {idx+1}: {e}")
                await asyncio.to_thread(_save_course, current)
    except Exception as e:
        server.logger.warning(f"[education] _backfill_missing_parts: {e}")
    # Финальная нормализация контента (мусор от старых генераций)
    current = _load_course(course_id)
    if current:
        changed = False
        for l in _plan_dict(current.get("plan")).get("lessons", []):
            if isinstance(l.get("content"), str):
                norm = _normalize_text(l["content"])
                if norm != l["content"]:
                    l["content"] = norm
                    changed = True
        if changed:
            await asyncio.to_thread(_save_course, current)


# ========== SSE GENERATION STREAM ==========

async def stream_generate(course_id) -> AsyncGenerator[str, None]:
    course = _load_course(course_id)
    if not course:
        yield _sse_event("error", {"message": "Курс не найден"})
        return

    if course.get("status") in ("ready", "error", "completed"):
        yield _sse_event("done", {"course_id": course_id, "status": course["status"]})
        return

    course["status"] = "analyzing"
    course["updated_at"] = _now()
    gp = course.setdefault("generation_progress", {})
    _save_course(course)

    query = course.get("user_query", "")
    difficulty = course.get("difficulty", "beginner")
    ai_only = course.get("ai_only", False)
    materials = _norm_materials(course.get("materials"))
    prev_phase = gp.get("phase", "")

    yield _sse_event("resume", {
        "phase": prev_phase,
        "lessons_done": gp.get("lessons_done", 0),
        "total_lessons": gp.get("total_lessons", 0)
    })
    yield _sse_event("phase", {"phase": "start", "message": "Начинаю генерацию курса..."})

    def _check_paused():
        c = _load_course(course_id)
        return c and c.get("status") == "paused"

    try:
        # ── PREPARE NOTES ──
        if _check_paused():
            yield _sse_event("paused", {"course_id": course_id})
            return

        notes_text = ""
        notes_data = _load_notes(course_id)
        if notes_data.get("notes"):
            notes_text = notes_data.get("overview", "")
            if not notes_text:
                notes_text = "\n\n".join([f"[{n['source']}]: {n['text']}" for n in notes_data["notes"]])

        if not notes_text and materials and not ai_only:
            mat_count = len(materials)
            total_chars = sum(len(m.get("text", "")) for m in materials if isinstance(m, dict))
            use_v2 = mat_count >= 3 or total_chars > 1500000
            yield _sse_event("phase", {"phase": "notes", "message": "Анализ материалов..." if not use_v2 else "Суммаризация материалов (Map-Reduce)..."})
            for mi, m in enumerate(materials):
                yield _sse_event("progress", {"message": f"Чтение материала {mi+1}/{mat_count}: {m.get('title','')}", "current": mi, "total": mat_count, "phase": "notes"})
            yield _sse_event("progress", {"message": "Извлечение ключевой информации..." if not use_v2 else "Параллельная обработка материалов..."})
            if use_v2:
                notes_text = await asyncio.to_thread(_phase_notes_v2, course, course_id, query, materials)
            else:
                notes_text = await asyncio.to_thread(_phase_notes, course, course_id, query, materials)
            yield _sse_event("progress", {"message": "✓ Материалы проанализированы"})

        if ai_only and not notes_text:
            yield _sse_event("phase", {"phase": "notes", "message": "Подготовка..."})
            notes_text = ""
        _set_phase(course_id, "notes")
        if _check_paused():
            yield _sse_event("paused", {"course_id": course_id})
            return

        # ── PHASE 0: SYLLABUS ──
        if not _course_lessons(course):
            yield _sse_event("phase", {"phase": "syllabus", "message": "Составление плана курса..."})
            yield _sse_event("progress", {"message": "Определение структуры и тем уроков..."})
            total_lessons = await asyncio.to_thread(_phase_syllabus, course, course_id, query, difficulty, notes_text)
            course = _load_course(course_id) or course
            _ov = _plan_dict(course.get("plan")).get("overview", "") or ""
            yield _sse_event("syllabus", {"total": total_lessons, "overview": _ov[:200]})
            yield _sse_event("progress", {"message": f"✓ План составлен: {total_lessons} уроков"})
        else:
            total_lessons = len(_course_lessons(course))
        if _check_paused():
            yield _sse_event("paused", {"course_id": course_id})
            return

        # ── PHASE 1: LESSONS (per-lesson: content + quiz + homework + flashcards) ──
        plan = _plan_dict(course.get("plan"))
        lessons = plan.get("lessons", [])
        lessons_done = gp.get("lessons_done", 0)

        def _reload_lesson(_li):
            """Перезагрузить урок из сохранённого курса."""
            _c = _load_course(course_id) or {}
            _ls = _plan_dict(_c.get("plan")).get("lessons") or []
            if 0 <= _li < len(_ls):
                return _c, _ls[_li]
            return _c, lessons[_li] if _li < len(lessons) else {}

        for li in range(lessons_done, len(lessons)):
            lesson = _lesson_dict(lessons[li], li)
            lessons[li] = lesson
            title = lesson.get('title', '')

            if _check_paused():
                yield _sse_event("paused", {"course_id": course_id})
                return

            # 1a. Lesson content (изолировано — сбой не роняет курс)
            yield _sse_event("phase", {
                "phase": "lessons",
                "message": f"Урок {li+1}/{len(lessons)}: {title}",
                "current": li, "total": len(lessons)
            })
            try:
                yield _sse_event("progress", {"message": f"Генерация содержания урока «{title}»..."})
                await asyncio.to_thread(
                    _phase_lesson, course, course_id, query, notes_text, li, lesson
                ) if not lesson.get("content") else None
                course, lesson = _reload_lesson(li)
            except Exception as e:
                server.logger.warning(f"[education] урок {li+1} контент: {e}")
                course, lesson = _reload_lesson(li)
            if _check_paused():
                yield _sse_event("paused", {"course_id": course_id})
                return

            # 1b. Quiz for this lesson
            try:
                if not lesson.get("quiz"):
                    yield _sse_event("phase", {
                        "phase": "quizzes",
                        "message": f"Тест {li+1}/{len(lessons)}: {title}",
                        "current": li, "total": len(lessons)
                    })
                    yield _sse_event("progress", {"message": f"Генерация теста для «{title}»..."})
                    await asyncio.to_thread(_phase_quiz, course, course_id, li, lesson)
                    course, lesson = _reload_lesson(li)
            except Exception as e:
                server.logger.error(f"[education] урок {li+1} тест: {e}", exc_info=True)
                course, lesson = _reload_lesson(li)
            if _check_paused():
                yield _sse_event("paused", {"course_id": course_id})
                return

            # 1c. Homework for this lesson
            try:
                if not lesson.get("homework") and not lesson.get("practice_data"):
                    yield _sse_event("phase", {
                        "phase": "homework",
                        "message": f"ДЗ {li+1}/{len(lessons)}: {title}",
                        "current": li, "total": len(lessons)
                    })
                    yield _sse_event("progress", {"message": f"Генерация ДЗ для «{title}»..."})
                    await asyncio.to_thread(_phase_homework, course, course_id, li, lesson)
                    course, lesson = _reload_lesson(li)
            except Exception as e:
                server.logger.error(f"[lesson] урок {li+1} ДЗ: {e}", exc_info=True)
                course, lesson = _reload_lesson(li)
            if _check_paused():
                yield _sse_event("paused", {"course_id": course_id})
                return

            # 1d. Flashcards for this lesson
            try:
                if not lesson.get("flashcards"):
                    yield _sse_event("phase", {
                        "phase": "flashcards",
                        "message": f"Карточки {li+1}/{len(lessons)}: {title}",
                        "current": li, "total": len(lessons)
                    })
                    yield _sse_event("progress", {"message": f"Генерация карточек для «{title}»..."})
                    await asyncio.to_thread(_regenerate_flashcards_internal, course, course_id, li) if not lesson.get("flashcards") else None
                    course, lesson = _reload_lesson(li)
            except Exception as e:
                server.logger.error(f"[lesson] урок {li+1} карточки: {e}", exc_info=True)
                course, lesson = _reload_lesson(li)

            # Mark lesson done (content + quiz + hw + flashcards complete)
            gp["lessons_done"] = li + 1
            course["generation_progress"] = gp
            course["updated_at"] = _now()
            _save_course(course)

            yield _sse_event("lesson_done", {"index": li, "title": title, "quiz": bool(lesson.get("quiz")), "homework": bool(lesson.get("homework") or lesson.get("practice_data")), "flashcards": bool(lesson.get("flashcards"))})

        if _check_paused():
            yield _sse_event("paused", {"course_id": course_id})
            return

        # ── PHASE 1.5: BACKFILL — дозаполнить недостающие блоки у всех уроков ──
        await _backfill_missing_parts(course, course_id, query, notes_text)
        course = _load_course(course_id) or course

        # ── PHASE 4: FINAL EXAM ──
        yield _sse_event("phase", {"phase": "exam", "message": "Создание финального экзамена..."})
        yield _sse_event("progress", {"message": "Составление вопросов по всем урокам..."})
        await asyncio.to_thread(_phase_exam, course, course_id)
        yield _sse_event("progress", {"message": "✓ Финальный экзамен готов"})
        if _check_paused():
            yield _sse_event("paused", {"course_id": course_id})
            return

        # ── PHASE 5: BIBLIOGRAPHY ──
        if not _plan_dict(course.get("plan")).get("bibliography"):
            yield _sse_event("phase", {"phase": "bibliography", "message": "Составление списка литературы..."})
            yield _sse_event("progress", {"message": "Поиск источников и референсов..."})
            await asyncio.to_thread(_phase_bibliography, course, course_id, query, notes_text, materials)
            yield _sse_event("progress", {"message": "✓ Библиография готова"})

        # ── DONE ──
        course = _load_course(course_id) or course
        course["status"] = "ready"
        course["generation_progress"]["phase"] = "done"
        course["generation_progress"]["lessons_done"] = len(lessons)
        course["updated_at"] = _now()
        _save_course(course)

        yield _sse_event("phase", {"phase": "done", "message": "Курс готов!"})
        yield _sse_event("done", {"course_id": course_id, "status": "ready"})

    except Exception as e:
        server.logger.error(f"[education] stream_generate error: {e}", exc_info=True)
        course = _load_course(course_id)
        if course:
            course["status"] = "error"
            course["error"] = str(e)[:500]
            course["updated_at"] = _now()
            _save_course(course)
        yield _sse_event("error", {"message": str(e)[:300]})

def _sse_event(event_type, data):
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"

# ========== LESSON INTERACTIONS ==========

def submit_test_answer(course_id, lesson_num, user_answer, is_homework=False):
    course = _load_course(course_id)
    if not course or not course.get("plan"):
        return {"error": "Курс не найден"}
    lessons = _plan_dict(course.get("plan")).get("lessons", [])
    try: idx = int(lesson_num)
    except:
        swallow.note('education_module.submit_test_answer:L2187')
        return {"error": "Неверный номер урока"}
    if idx < 0 or idx >= len(lessons):
        return {"error": "Урок не найден"}
    lesson = lessons[idx]
    test = lesson.get("test", {})

    if is_homework:
        return _check_homework(course, idx, lesson, user_answer)

    system = (
        "Ты — строгий преподаватель. Проверь ответ ученика. "
        "Ответь JSON: {\"correct\": bool, \"score\": 0-10, \"feedback\": \"разбор\"}"
    )
    context = f"Вопрос: {test.get('question', '')}\nВарианты: {json.dumps(test.get('options', []), ensure_ascii=False)}\nПравильный ответ (индекс): {test.get('correct', '')}\nОтвет ученика: {user_answer}"
    try:
        result = _call_ai(system, context, max_tokens=1024, temperature=0.3)
    except ConnectionError as e:
        swallow.note('education_module.submit_test_answer:L2205', e)
        return {"correct": False, "score": 0, "feedback": f"❌ {e}"}

    evaluation = {"correct": False, "score": 0, "feedback": result[:300]}
    parsed = _extract_json_dict(result)
    if parsed:
        evaluation.update(parsed)

    _update_lesson_progress(course, idx, lesson, evaluation)
    return evaluation

def submit_quiz(course_id, lesson_num, answers):
    course = _load_course(course_id)
    if not course or not course.get("plan"):
        return {"error": "Курс не найден"}
    lessons = _plan_dict(course.get("plan")).get("lessons", [])
    try: idx = int(lesson_num)
    except:
        swallow.note('education_module.submit_quiz:L2223')
        return {"error": "Неверный номер урока"}
    if idx < 0 or idx >= len(lessons): return {"error": "Урок не найден"}
    lesson = lessons[idx]
    quiz = lesson.get("quiz", [])

    if not quiz:
        return {"error": "Нет вопросов для викторины"}

    results = []
    correct_count = 0
    for qi, q in enumerate(quiz):
        user_ans = answers.get(str(qi)) if isinstance(answers, dict) else (answers[qi] if qi < len(answers) else None)
        if user_ans is None:
            results.append({"question": qi, "correct": False, "score": 0, "feedback": "Нет ответа", "expected": q.get("correct", q.get("correct_answer", "")), "explanation": "Ответ не был предоставлен."})
            continue
        q_type = q.get("type", "choice")
        is_correct = False
        if q_type == "choice":
            is_correct = str(user_ans) == str(q.get("correct", -1))
        elif q_type == "multiple":
            correct_set = set(str(x) for x in (q.get("correct") or []))
            user_set = set(str(x) for x in (user_ans if isinstance(user_ans, (list, tuple)) else [user_ans]))
            is_correct = correct_set == user_set
        elif q_type == "fill_in":
            correct_answer = str(q.get("correct_answer", "")).lower().strip()
            is_correct = str(user_ans).lower().strip() == correct_answer
        elif q_type == "true_false":
            expected = str(q.get("correct", "")).lower().strip()
            given = str(user_ans).lower().strip()
            is_correct = given == expected or given == ("1" if expected == "true" else "0")
        elif q_type in ("matching", "ordering"):
            expected = [str(x) for x in (q.get("correct_order") or q.get("correct") or [])]
            given = [str(x) for x in (user_ans if isinstance(user_ans, (list, tuple)) else [user_ans])]
            is_correct = expected == given
        if is_correct:
            correct_count += 1
        expected_val = q.get("correct", q.get("correct_answer", ""))
        results.append({"question": qi, "correct": is_correct, "score": 10 if is_correct else 0, "expected": expected_val, "explanation": ""})

    total = len(quiz)
    score = int((correct_count / total) * 100) if total > 0 else 0
    passed = score >= 60

    wrong_results = [r for r in results if not r["correct"]]
    if wrong_results:
        wrong_details = []
        for r in wrong_results:
            qi = r["question"]
            q = quiz[qi]
            user_ans = answers.get(str(qi)) if isinstance(answers, dict) else (answers[qi] if qi < len(answers) else None)
            opts = q.get("options", [])
            opts_str = "\n".join(f"  {i}: {o}" for i, o in enumerate(opts)) if opts else ""
            correct_display = r["expected"]
            if q.get("type") == "multiple" and isinstance(r["expected"], (list, tuple)):
                correct_display = ", ".join(str(x) for x in r["expected"])
                correct_display = ", ".join(opts[int(x)] for x in r["expected"] if str(x).isdigit() and int(x) < len(opts)) if opts else correct_display
            elif q.get("type") == "choice" and opts and str(r["expected"]).isdigit():
                ci = int(r["expected"])
                correct_display = opts[ci] if ci < len(opts) else r["expected"]
            elif q.get("type") == "true_false":
                correct_display = "Верно" if str(r["expected"]).lower() in ("true", "1", "верно") else "Неверно"
            wrong_details.append(
                f"Вопрос {qi+1}: {q.get('question', '')}\n"
                f"  Тип: {q.get('type', 'choice')}\n"
                f"  Варианты: {opts_str}\n"
                f"  Ответ ученика: {user_ans}\n"
                f"  Правильный ответ: {correct_display}"
            )
        system = (
            "Ты — преподаватель. Объясни почему ответы ученика неправильные. "
            "Для каждого вопроса кратко и понятно объясни ошибку и суть правильного ответа. "
            "Ответь ТОЛЬКО JSON:\n"
            '{"explanations": [{"idx": 0, "explanation": "Потому что ..."}, ...]}'
        )
        user = "Неправильные ответы:\n\n" + "\n\n".join(wrong_details)
        try:
            ai_result = _call_ai(system, user, max_tokens=2048, temperature=0.3)
            parsed = _extract_json_dict(ai_result)
            if parsed and "explanations" in parsed:
                for exp in parsed["explanations"]:
                    ei = exp.get("idx")
                    if isinstance(ei, int) and 0 <= ei < len(results):
                        results[ei]["explanation"] = exp.get("explanation", "")
        except Exception:
            swallow.note('education_module.submit_quiz:L2308')
            pass

    for r in results:
        if not r["explanation"]:
            r["explanation"] = "Верно!" if r["correct"] else "Неправильно."

    if passed and idx not in course["progress"].get("completed_lessons", []):
        course.setdefault("progress", {}).setdefault("quiz_scores", []).append({"lesson": idx, "score": score, "correct": correct_count, "total": total, "timestamp": _now()})

    return {
        "results": results,
        "score": score,
        "correct_count": correct_count,
        "total": total,
        "passed": passed,
        "feedback": f"Правильно: {correct_count}/{total} ({score}%)"
    }

def submit_final_exam(course_id, answers):
    course = _load_course(course_id)
    if not course or not course.get("plan"): return {"error": "Курс не найден"}
    lessons = _plan_dict(course.get("plan")).get("lessons", [])

    all_questions = []
    for li, l in enumerate(lessons):
        quiz = l.get("quiz", [])
        for q in quiz:
            qc = dict(q)
            qc["lesson"] = li
            all_questions.append(qc)
        test = l.get("test", {})
        if test:
            tc = dict(test)
            tc["lesson"] = li
            all_questions.append(tc)

    if not all_questions:
        return {"error": "Нет вопросов для экзамена"}

    results = []
    correct_count = 0
    for qi, q in enumerate(all_questions):
        user_ans = answers.get(str(qi)) if isinstance(answers, dict) else (answers[qi] if qi < len(answers) else None)
        if user_ans is None:
            results.append({"question": qi, "correct": False, "score": 0, "expected": q.get("correct", q.get("correct_answer", "")), "explanation": "Ответ не был предоставлен."})
            continue
        is_correct = str(user_ans) == str(q.get("correct", q.get("correct_answer", "")))
        if is_correct: correct_count += 1
        expected_val = q.get("correct", q.get("correct_answer", ""))
        results.append({"question": qi, "correct": is_correct, "score": 10 if is_correct else 0, "expected": expected_val, "explanation": ""})

    total = len(all_questions)
    score = int((correct_count / total) * 100) if total > 0 else 0
    passed = score >= 70

    wrong_results = [r for r in results if not r["correct"]]
    if wrong_results:
        wrong_details = []
        for r in wrong_results:
            qi = r["question"]
            q = all_questions[qi]
            user_ans = answers.get(str(qi)) if isinstance(answers, dict) else (answers[qi] if qi < len(answers) else None)
            opts = q.get("options", [])
            opts_str = "\n".join(f"  {i}: {o}" for i, o in enumerate(opts)) if opts else ""
            correct_display = r["expected"]
            if q.get("type") == "multiple" and isinstance(r["expected"], (list, tuple)):
                correct_display = ", ".join(opts[int(x)] for x in r["expected"] if str(x).isdigit() and int(x) < len(opts)) if opts else ", ".join(str(x) for x in r["expected"])
            elif q.get("type") == "choice" and opts and str(r["expected"]).isdigit():
                ci = int(r["expected"])
                correct_display = opts[ci] if ci < len(opts) else r["expected"]
            elif q.get("type") == "true_false":
                correct_display = "Верно" if str(r["expected"]).lower() in ("true", "1", "верно") else "Неверно"
            wrong_details.append(
                f"Вопрос {qi+1} (урок {q.get('lesson',0)+1}): {q.get('question', '')}\n"
                f"  Тип: {q.get('type', 'choice')}\n"
                f"  Варианты: {opts_str}\n"
                f"  Ответ ученика: {user_ans}\n"
                f"  Правильный ответ: {correct_display}"
            )
        system = (
            "Ты — преподаватель. Объясни почему ответы ученика неправильные. "
            "Для каждого вопроса кратко и понятно объясни ошибку и суть правильного ответа. "
            "Ответь ТОЛЬКО JSON:\n"
            '{"explanations": [{"idx": 0, "explanation": "Потому что ..."}, ...]}'
        )
        user = "Неправильные ответы на экзамене:\n\n" + "\n\n".join(wrong_details)
        try:
            ai_result = _call_ai(system, user, max_tokens=2048, temperature=0.3)
            parsed = _extract_json_dict(ai_result)
            if parsed and "explanations" in parsed:
                for exp in parsed["explanations"]:
                    ei = exp.get("idx")
                    if isinstance(ei, int) and 0 <= ei < len(results):
                        results[ei]["explanation"] = exp.get("explanation", "")
        except Exception:
            swallow.note('education_module.submit_final_exam:L2404')
            pass

    for r in results:
        if not r["explanation"]:
            r["explanation"] = "Верно!" if r["correct"] else "Неправильно."

    course["progress"]["exam_score"] = score
    course["progress"]["exam_passed"] = passed

    if passed and len(course["progress"].get("completed_lessons", [])) >= len(lessons):
        course["status"] = "completed"

    _save_course(course)

    return {
        "score": score,
        "correct_count": correct_count,
        "total": total,
        "passed": passed,
        "results": results,
        "feedback": f"Экзамен {'сдан' if passed else 'не сдан'}! {correct_count}/{total} ({score}%)"
    }

def _check_homework(course, idx, lesson, answer):
    system = (
        "Ты — строгий преподаватель. Проверь домашнее задание ученика. "
        "Оцени по критериям: полнота, правильность, наличие примеров, глубина понимания. "
        "Ответь JSON: {\"score\": 0-10, \"feedback\": \"подробный разбор\", \"mistakes\": [\"ошибка1\", \"ошибка2\"]}"
    )
    user = f"Задание: {lesson.get('homework', '')}\n\nРешение ученика:\n{answer}\n\nПроверь и оцени:"
    try:
        result = _call_ai(system, user, max_tokens=1024, temperature=0.3)
    except ConnectionError as e:
        swallow.note('education_module._check_homework:L2438', e)
        return {"score": 0, "feedback": f"❌ {e}", "mistakes": []}
    evaluation = {"score": 5, "feedback": result[:500], "mistakes": []}
    parsed = _extract_json_dict(result)
    if parsed:
        evaluation.update(parsed)
    _update_lesson_progress(course, idx, lesson, evaluation)
    return evaluation

def _update_lesson_progress(course, idx, lesson, evaluation):
    score = evaluation.get("score", 0)
    correct = evaluation.get("correct", score >= 5)
    if correct or score >= 5:
        completed = course["progress"].get("completed_lessons", [])
        if idx not in completed:
            completed.append(idx)
            course["progress"]["completed_lessons"] = completed
            course["progress"]["total_score"] = course["progress"].get("total_score", 0) + score
            course["progress"]["current_lesson"] = idx + 1
            lesson["completed"] = True
            lesson["score"] = score
        lessons = _plan_dict(course.get("plan")).get("lessons", [])
        if len(completed) >= len(lessons):
            course["status"] = "learning"
    course["updated_at"] = _now()
    _save_course(course)

def ask_question(course_id, question):
    course = _load_course(course_id)
    if not course: return {"error": "Курс не найден"}
    context = ""
    if course.get("plan"):
        current = course["progress"].get("current_lesson", 0)
        lessons = _plan_dict(course.get("plan")).get("lessons", [])
        if 0 <= current < len(lessons):
            lesson = lessons[current]
            context = f"Текущий урок: {lesson.get('title', '')}\n{lesson.get('content', '')[:3000]}"

    system = "Ты — AI репетитор. Отвечай на вопросы по теме курса понятно, с примерами. Помоги ученику разобраться."
    notes_text = ""
    notes_data = _load_notes(course_id)
    if notes_data.get("notes"):
        recent = notes_data["notes"][-5:]
        notes_text = "\n".join([f"[{n['source']}]: {n['text'][:300]}" for n in recent])

    # Векторный индекс (если пользователь его включил и построил) даёт точные
    # фрагменты материалов — они точнее, чем первые 3000 символов анализа.
    vec_ctx = ""
    try:
        import modules.education_vector as _EV
        if _EV.is_enabled():
            vec_ctx = _EV.context_for(course_id, question, limit=5)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"education vector context: {e}")

    user = f"Контекст курса: {(course.get('analysis') or notes_text)[:3000]}\n\n{context}\n\nВопрос: {question}"
    if vec_ctx:
        user = (f"Фрагменты материалов курса (релевантные вопросу):\n{vec_ctx[:6000]}\n\n"
                + user)
    try:
        answer = _call_ai(system, user, max_tokens=2048)
    except ConnectionError as e:
        swallow.note('education_module.ask_question:L2500', e)
        answer = f"❌ {e}"

    course["chat_history"].append({"role": "user", "content": question, "time": _now()})
    course["chat_history"].append({"role": "assistant", "content": answer, "time": _now()})
    course["updated_at"] = _now()
    _save_course(course)
    return {"answer": answer}

def get_tts_text(course_id, lesson_num):
    lesson = get_lesson(course_id, lesson_num)
    if not lesson: return {"error": "Урок не найден"}
    text = lesson.get("content", "")
    text = _clean_text(text)
    return {"text": text[:3000]}

def get_progress(course_id):
    course = _load_course(course_id)
    if not course: return {"error": "Курс не найден"}
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    progress = course.get("progress", {})
    completed = progress.get("completed_lessons", [])
    total_lessons = len(lessons)
    done = len(completed)
    pct = int((done / total_lessons) * 100) if total_lessons > 0 else 0
    quiz_scores = progress.get("quiz_scores", [])
    avg_quiz = int(sum(q["score"] for q in quiz_scores) / len(quiz_scores)) if quiz_scores else 0

    return {
        "total_lessons": total_lessons,
        "completed": done,
        "percent": pct,
        "total_score": progress.get("total_score", 0),
        "max_score": progress.get("max_score", 100),
        "exam_score": progress.get("exam_score"),
        "exam_passed": progress.get("exam_passed", False),
        "avg_quiz_score": avg_quiz,
        "status": course.get("status", ""),
        "current_lesson": progress.get("current_lesson", 0)
    }

def extend_course(course_id, num_additional_lessons=3):
    """Add additional lessons to a completed/ready course and reset status to regenerate."""
    course = _load_course(course_id)
    if not course: return {"error": "Курс не найден"}
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    if not lessons:
        return {"error": "Нет уроков для расширения"}

    count = _clamp(num_additional_lessons, 1, 10) or 3
    placeholders = [{"title": f"Доп. урок {len(lessons)+i+1}", "content": "", "objectives": [], "key_terms": [], "test": {}, "quiz": [], "practice": "", "homework": ""} for i in range(count)]
    plan["lessons"] = lessons + placeholders
    plan["total_lessons"] = len(plan["lessons"])
    course["plan"] = plan

    gp = course.setdefault("generation_progress", {})
    gp["phase"] = "lessons"
    gp["lessons_done"] = len(lessons)
    gp["total_lessons"] = len(plan["lessons"])

    course["status"] = "analyzing"
    course["updated_at"] = _now()
    course.pop("error", None)
    _save_course(course)
    return {"id": course_id, "additional": count, "total": len(plan["lessons"])}


def _repair_worker(course_id):
    """Фоновая задача: нормализовать контент и догенерировать недостающие блоки."""
    try:
        course = _load_course(course_id)
        if not course:
            return
        query = course.get("user_query", "")
        notes_data = _load_notes(course_id)
        notes_text = notes_data.get("overview", "") or ""
        if not notes_text and notes_data.get("notes"):
            notes_text = "\n\n".join([f"[{n['source']}]: {n['text']}" for n in notes_data["notes"]])
        plan = _plan_dict(course.get("plan"))
        lessons = plan.get("lessons", [])

        def _reload_l(idx_):
            """Вернуть актуальный урок idx_ из перечитанного курса."""
            _c = _load_course(course_id) or course
            _ls = _plan_dict(_c.get("plan")).get("lessons", [])
            if idx_ < len(_ls):
                return _c, _ls[idx_]
            return _c, {}

        for idx in range(len(lessons)):
            course = _load_course(course_id) or course
            lessons = _plan_dict(course.get("plan")).get("lessons", [])
            if idx >= len(lessons):
                break
            l = _lesson_dict(lessons[idx], idx)
            old_content = l.get("content") or ""
            # 1. Слишком короткий или пустой контент → перегенерировать
            if not old_content or len(old_content) < 100:
                try:
                    _phase_lesson(course, course_id, query, notes_text, idx, l)
                except Exception as e:
                    logger.warning(f"[repair] контент урок {idx+1}: {e}")
                course, l = _reload_l(idx)
            # 2. Мусор в контенте → нормализовать
            content = l.get("content") or ""
            if isinstance(content, str) and content:
                norm = _normalize_text(content)
                if norm != content:
                    l["content"] = norm
            # 3. Цели/термины
            if not isinstance(l.get("objectives"), list) or not l.get("objectives"):
                l["objectives"] = _fallback_objectives(l.get("title", ""), content)
            if not isinstance(l.get("key_terms"), list) or not l.get("key_terms"):
                l["key_terms"] = _fallback_key_terms(l.get("title", ""), content)
            _plan_dict(course.get("plan"))["lessons"][idx] = l
            course["plan"] = _plan_dict(course.get("plan"))
            _save_course(course)
            # 4. Тест
            if not l.get("quiz"):
                try:
                    _phase_quiz(course, course_id, idx, l)
                except Exception as e:
                    logger.warning(f"[repair] тест урок {idx+1}: {e}")
            course, l = _reload_l(idx)
            # 5. ДЗ
            if not l.get("homework") and not l.get("practice_data"):
                try:
                    _phase_homework(course, course_id, idx, l)
                except Exception as e:
                    logger.warning(f"[repair] ДЗ урок {idx+1}: {e}")
            course, l = _reload_l(idx)
            # 6. Карточки
            if not l.get("flashcards"):
                try:
                    _regenerate_flashcards_internal(course, course_id, idx)
                except Exception as e:
                    logger.warning(f"[repair] карточки урок {idx+1}: {e}")

        # Финальный проход нормализации
        course = _load_course(course_id) or course
        lessons = _plan_dict(course.get("plan")).get("lessons", [])
        for l in lessons:
            if isinstance(l.get("content"), str):
                l["content"] = _normalize_text(l["content"])
        course["status"] = "ready"
        course["error"] = ""
        gp = course.setdefault("generation_progress", {})
        gp["phase"] = "done"
        gp["lessons_done"] = len(lessons)
        gp["total_lessons"] = len(lessons)
        course["updated_at"] = _now()
        _save_course(course)
    except Exception as e:
        logger.warning(f"[education] _repair_worker: {e}")
        course = _load_course(course_id)
        if course:
            course["status"] = "ready"
            course["error"] = ""
            _save_course(course)


def repair_course(course_id):
    """Запустить фоновую починку уже существующего курса.

    Нормализует «мусорный» контент существующих уроков и догенерирует
    недостающие тесты, ДЗ и карточки. Возвращает {ok} и работает в фоне.
    """
    try:
        course = _load_course(course_id)
        if not course:
            return {"error": "Курс не найден"}
        if course.get("status") in ("queued", "analyzing", "paused"):
            return {"error": "Курс ещё в стадии генерации", "status": course.get("status")}
        if not _course_lessons(course):
            return {"error": "Нет уроков для починки"}
        course["status"] = "analyzing"
        gp = course.setdefault("generation_progress", {})
        gp["phase"] = "repair"
        course["updated_at"] = _now()
        _save_course(course)
        threading.Thread(target=_repair_worker, args=(course_id,), daemon=True).start()
        return {"ok": True}
    except Exception as e:
        logger.warning(f"[education] repair_course: {e}")
        return {"error": str(e)[:300]}

def check_ai_answer(course_id, lesson_num, question_idx, answer):
    course = _load_course(course_id)
    if not course: return {"error": "Курс не найден"}
    lesson = get_lesson(course_id, lesson_num)
    if not lesson: return {"error": "Урок не найден"}
    quiz = lesson.get("quiz", [])
    if not quiz or question_idx is None or question_idx >= len(quiz):
        return {"error": "Вопрос не найден"}
    question = quiz[question_idx]
    system = (
        "Ты — преподаватель. Оцени ответ ученика на открытый вопрос. "
        "Ответь ТОЛЬКО JSON:\n"
        '{"score": 0-100, "strengths": ["сильная сторона"], '
        '"weaknesses": ["слабая сторона"], '
        '"improvements": ["что улучшить"], '
        '"corrected_answer": "исправленный ответ"}'
    )
    user = (
        f"Вопрос: {question.get('question', '')}\n"
        f"Критерии: {', '.join(question.get('criteria', []))}\n"
        f"Ответ ученика: {answer}\n\n"
        f"Оцени ответ, дай развёрнутую обратную связь."
    )
    result = _call_ai(system, user, max_tokens=2048, temperature=0.3)
    data = _extract_json_dict(result) or {"score": 50, "strengths": [], "weaknesses": [], "improvements": [], "corrected_answer": ""}
    return data


def regenerate_quiz(course_id, lesson_num):
    course = _load_course(course_id)
    if not course: return {"error": "Курс не найден"}
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    try: idx = int(lesson_num) - 1
    except:
        swallow.note('education_module.regenerate_quiz:L2723')
        return {"error": "Неверный номер урока"}
    if idx < 0 or idx >= len(lessons):
        return {"error": "Урок не найден"}
    lesson = lessons[idx]
    lesson["quiz"] = []
    _phase_quiz(course, course_id, idx, lesson)
    course = _load_course(course_id) or course
    return {"ok": True, "quiz": course["plan"]["lessons"][idx].get("quiz", [])}


def regenerate_homework(course_id, lesson_num):
    course = _load_course(course_id)
    if not course: return {"error": "Курс не найден"}
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    try: idx = int(lesson_num) - 1
    except:
        swallow.note('education_module.regenerate_homework:L2741')
        return {"error": "Неверный номер урока"}
    if idx < 0 or idx >= len(lessons):
        return {"error": "Урок не найден"}
    lesson = lessons[idx]
    lesson["homework"] = ""
    lesson["practice_data"] = {}
    _phase_homework(course, course_id, idx, lesson)
    course = _load_course(course_id) or course
    return {"ok": True, "homework": course["plan"]["lessons"][idx].get("homework", ""), "practice_data": course["plan"]["lessons"][idx].get("practice_data", {})}


def regenerate_practice(course_id, lesson_num):
    # Practice is part of homework generation
    return regenerate_homework(course_id, lesson_num)


def regenerate_flashcards(course_id, lesson_num):
    course = _load_course(course_id)
    if not course: return {"error": "Курс не найден"}
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    try: idx = int(lesson_num) - 1
    except:
        swallow.note('education_module.regenerate_flashcards:L2765')
        return {"error": "Неверный номер урока"}
    if idx < 0 or idx >= len(lessons):
        return {"error": "Урок не найден"}
    lesson = lessons[idx]
    content = lesson.get("content", "")[:3000]
    system = (
        "Ты — педагог. Составь 10 карточек для запоминания (флешкарт) по теме урока. "
        "Ответь ТОЛЬКО JSON, список:\n"
        '[{"front": "термин/вопрос", "back": "определение/ответ"}, ...]'
    )
    user = f"Тема: {lesson.get('title', '')}\n\nСодержание:\n{content}\n\nСоздай 10 флешкарт."
    result = _call_ai(system, user, max_tokens=4096, temperature=0.5, timeout=90)
    cards = _extract_json_lenient(result) or [{"front": "термин", "back": "определение"}]
    flashcards = cards if isinstance(cards, list) else cards.get("flashcards", [cards])
    flashcards = init_flashcard_sm2(flashcards)
    lesson["flashcards"] = flashcards
    course["plan"]["lessons"][idx] = lesson
    course["updated_at"] = _now()
    _save_course(course)
    return {"ok": True, "flashcards": flashcards}


def _regenerate_flashcards_internal(course, course_id, lesson_idx):
    """Internal flashcard generation used during SSE stream (no reload)."""
    lessons = _plan_dict(course.get("plan")).get("lessons", [])
    if lesson_idx < 0 or lesson_idx >= len(lessons):
        return
    lesson = lessons[lesson_idx]
    if lesson.get("flashcards"):
        return
    content = lesson.get("content", "")[:3000]
    system = (
        "Ты — педагог. Составь 10 карточек для запоминания (флешкарт) по теме урока. "
        "Ответь ТОЛЬКО JSON, список:\n"
        '[{"front": "термин/вопрос", "back": "определение/ответ"}, ...]'
    )
    user = f"Тема: {lesson.get('title', '')}\n\nСодержание:\n{content}\n\nСоздай 10 флешкарт."
    result = _call_ai(system, user, max_tokens=4096, temperature=0.5, timeout=90)
    cards = _extract_json_lenient(result) or [{"front": "термин", "back": "определение"}]
    flashcards = cards if isinstance(cards, list) else cards.get("flashcards", [cards])
    flashcards = init_flashcard_sm2(flashcards)
    lesson["flashcards"] = flashcards
    course["plan"]["lessons"][lesson_idx] = lesson
    course["updated_at"] = _now()
    _save_course(course)


def get_badge_data():
    _ensure_dirs()
    total = 0
    completed = 0
    total_score = 0
    for f in COURSES_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text("utf-8"))
            total += 1
            if data.get("status") == "completed":
                completed += 1
            total_score += data.get("progress", {}).get("total_score", 0)
        except Exception as e:
            logger.warning(f"get_badge_data: ошибка чтения {f}: {e}")
    return {"total_courses": total, "completed_courses": completed, "total_score": total_score}


# ========== SM-2 SPACED REPETITION FOR FLASHCARDS ==========

def _sm2_calc(quality, prev_interval, prev_repetitions, prev_easiness):
    """SM-2 algorithm: returns (interval, repetitions, easiness, next_review_timestamp)."""
    quality = max(0, min(5, quality))
    ef = prev_easiness or 2.5
    ef = ef + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    if ef < 1.3:
        ef = 1.3

    if quality < 3:
        repetitions = 0
        interval = 1
    else:
        repetitions = (prev_repetitions or 0) + 1
        if repetitions == 1:
            interval = 1
        elif repetitions == 2:
            interval = 6
        else:
            interval = round((prev_interval or 1) * ef)

    next_review = int(_now() + interval * 86400)
    return interval, repetitions, round(ef, 2), next_review


def init_flashcard_sm2(flashcards):
    """Initialize SM-2 fields for newly generated flashcards."""
    for card in flashcards:
        if "sm2" not in card:
            card["sm2"] = {
                "interval": 0,
                "repetitions": 0,
                "easiness": 2.5,
                "next_review": 0,
                "last_reviewed": 0
            }
    return flashcards


def review_flashcard(course_id, lesson_num, card_idx, quality):
    """Submit a review for one flashcard. Returns updated card + next review info."""
    course = _load_course(course_id)
    if not course:
        return {"error": "Курс не найден"}
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    try:
        idx = int(lesson_num) - 1
    except (ValueError, TypeError):
        swallow.note('education_module.review_flashcard:L2880')
        return {"error": "Неверный номер урока"}
    if idx < 0 or idx >= len(lessons):
        return {"error": "Урок не найден"}

    cards = lessons[idx].get("flashcards", [])
    if card_idx < 0 or card_idx >= len(cards):
        return {"error": "Карточка не найдена"}

    card = cards[card_idx]
    sm2 = card.get("sm2", {"interval": 0, "repetitions": 0, "easiness": 2.5, "next_review": 0, "last_reviewed": 0})
    interval, reps, ef, next_review = _sm2_calc(
        quality, sm2["interval"], sm2["repetitions"], sm2["easiness"]
    )
    sm2["interval"] = interval
    sm2["repetitions"] = reps
    sm2["easiness"] = ef
    sm2["next_review"] = next_review
    sm2["last_reviewed"] = int(_now())
    card["sm2"] = sm2

    course["plan"]["lessons"][idx]["flashcards"][card_idx] = card
    course["updated_at"] = _now()
    _save_course(course)

    return {
        "ok": True,
        "card": card,
        "next_review": next_review,
        "interval_days": interval,
        "easiness": ef,
        "due_for_review": next_review <= _now()
    }


def get_due_flashcards(course_id):
    """Get all flashcards due for review across all lessons."""
    course = _load_course(course_id)
    if not course:
        return {"error": "Курс не найден"}
    now = _now()
    due = []
    plan = _plan_dict(course.get("plan"))
    for li, lesson in enumerate(plan.get("lessons", [])):
        for ci, card in enumerate(lesson.get("flashcards", [])):
            sm2 = card.get("sm2", {})
            next_review = sm2.get("next_review", 0)
            if next_review <= now or next_review == 0:
                due.append({
                    "lesson": li + 1,
                    "card_index": ci,
                    "front": card.get("front", ""),
                    "back": card.get("back", ""),
                    "sm2": sm2
                })
    return {"due": due, "total": len(due)}


def get_flashcard_stats(course_id):
    """Get SM-2 statistics for all flashcards in a course."""
    course = _load_course(course_id)
    if not course:
        return {"error": "Курс не найден"}
    total_cards = 0
    reviewed = 0
    mastered = 0
    learning = 0
    new_cards = 0
    now = _now()

    for lesson in _plan_dict(course.get("plan")).get("lessons", []):
        for card in lesson.get("flashcards", []):
            total_cards += 1
            sm2 = card.get("sm2", {})
            if not sm2 or sm2.get("last_reviewed", 0) == 0:
                new_cards += 1
                continue
            reviewed += 1
            if sm2.get("interval", 0) >= 21:
                mastered += 1
            elif sm2.get("repetitions", 0) >= 1:
                learning += 1

    return {
        "total_cards": total_cards,
        "reviewed": reviewed,
        "new": new_cards,
        "learning": learning,
        "mastered": mastered,
        "due": sum(1 for c in get_due_flashcards(course_id).get("due", [])) if total_cards > 0 else 0
    }


# ========== CROSSWORD GENERATOR ==========

def _generate_crossword_from_terms(terms, title=""):
    """Generate a simple crossword puzzle from key terms."""
    if not terms:
        return {"error": "Нет терминов для кроссворда"}
    words = []
    for t in terms[:12]:
        word = t.get("term", "") if isinstance(t, dict) else str(t)
        hint = t.get("definition", "") if isinstance(t, dict) else ""
        if word and len(word) >= 2:
            words.append({"word": word.upper(), "hint": hint, "placed": False})

    if not words:
        return {"error": "Нет подходящих терминов (мин. 2 буквы)"}

    grid_size = max(len(w["word"]) for w in words) + 4
    grid = [[" " for _ in range(grid_size)] for _ in range(grid_size)]
    placed = []
    rng_seed = int(_now() * 1000) % 1000

    words.sort(key=lambda w: len(w["word"]), reverse=True)
    for wi, w in enumerate(words):
        word = w["word"]
        if not word:
            continue
        placed_ok = False
        for attempt in range(50):
            direction = (wi + attempt + rng_seed) % 2
            if direction == 0:
                row = (attempt * 7 + rng_seed) % grid_size
                col = (attempt * 3 + rng_seed) % (grid_size - len(word))
                fits = True
                for i, ch in enumerate(word):
                    if grid[row][col + i] not in (" ", ch):
                        fits = False
                        break
                if fits:
                    for i, ch in enumerate(word):
                        grid[row][col + i] = ch
                    placed.append({"word": word, "hint": w["hint"], "row": row, "col": col, "direction": "across"})
                    placed_ok = True
                    break
            else:
                col = (attempt * 5 + rng_seed) % grid_size
                row = (attempt * 11 + rng_seed) % (grid_size - len(word))
                fits = True
                for i, ch in enumerate(word):
                    if grid[row + i][col] not in (" ", ch):
                        fits = False
                        break
                if fits:
                    for i, ch in enumerate(word):
                        grid[row + i][col] = ch
                    placed.append({"word": word, "hint": w["hint"], "row": row, "col": col, "direction": "down"})
                    placed_ok = True
                    break
    return {"grid": grid, "placed": placed, "size": grid_size, "title": title or "Кроссворд"}


def generate_crossword(course_id, lesson_num):
    """Generate crossword from lesson key_terms."""
    course = _load_course(course_id)
    if not course:
        return {"error": "Курс не найден"}
    lesson = get_lesson(course_id, lesson_num)
    if not lesson:
        return {"error": "Урок не найден"}
    terms = lesson.get("key_terms", [])
    if not terms:
        return {"error": "В уроке нет ключевых терминов"}
    crossword = _generate_crossword_from_terms(terms, lesson.get("title", ""))
    return crossword


# ========== TIMED QUIZ ==========

def set_quiz_timer(course_id, minutes):
    """Set a time limit for quizzes in a course (0 = no limit)."""
    course = _load_course(course_id)
    if not course:
        return {"error": "Курс не найден"}
    minutes = max(0, min(180, int(minutes)))
    settings = course.setdefault("settings", {})
    settings["quiz_time_limit"] = minutes
    course["updated_at"] = _now()
    _save_course(course)
    return {"ok": True, "time_limit_minutes": minutes}


# ========== EXPORT ==========

def export_course(course_id, fmt="json"):
    """Export course in JSON or TXT format."""
    course = _load_course(course_id)
    if not course:
        return {"error": "Курс не найден"}
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])

    if fmt == "json":
        export = {
            "title": course.get("title", ""),
            "difficulty": course.get("difficulty", ""),
            "query": course.get("user_query", ""),
            "overview": plan.get("overview", ""),
            "lessons": []
        }
        for li, l in enumerate(lessons):
            export["lessons"].append({
                "num": li + 1,
                "title": l.get("title", ""),
                "content": l.get("content", ""),
                "objectives": l.get("objectives", []),
                "key_terms": l.get("key_terms", []),
                "quiz": [
                    {"question": q.get("question", ""), "type": q.get("type", ""),
                     "options": q.get("options", []), "correct": q.get("correct", q.get("correct_answer", "")),
                     "explanation": q.get("explanation", "")}
                    for q in (l.get("quiz") or [])
                ],
                "homework": l.get("homework", ""),
                "practice": l.get("practice", "")
            })
        return {"content": json.dumps(export, ensure_ascii=False, indent=2), "filename": f"{course['title'][:50]}.json"}

    if fmt == "txt":
        lines = []
        lines.append(f"Курс: {course.get('title', '')}")
        lines.append(f"Уровень: {course.get('difficulty', '')}")
        lines.append(f"Описание: {plan.get('overview', '')}")
        lines.append("=" * 60)
        for li, l in enumerate(lessons):
            lines.append(f"\n{'=' * 60}")
            lines.append(f"УРОК {li + 1}: {l.get('title', '')}")
            lines.append(f"{'=' * 60}")
            lines.append(l.get("content", ""))
            for qi, q in enumerate(l.get("quiz") or []):
                lines.append(f"\n  Вопрос {qi+1}: {q.get('question', '')}")
                for oi, o in enumerate(q.get("options", [])):
                    lines.append(f"    {oi}. {o}")
            if l.get("homework"):
                lines.append(f"\n  ДЗ: {l.get('homework', '')[:200]}")
        content = "\n".join(lines)
        return {"content": content, "filename": f"{course['title'][:50]}.txt"}

    return {"error": f"Неизвестный формат: {fmt}"}


# ========== GAMIFICATION ==========

_GAMIFICATION_FILE = EDUCATION_DIR / "gamification.json"


def _load_gamification():
    if _GAMIFICATION_FILE.exists():
        try:
            return json.loads(_GAMIFICATION_FILE.read_text("utf-8"))
        except:
            swallow.note('education_module._load_gamification:L3132')
            pass
    return {
        "total_courses_started": 0,
        "total_courses_completed": 0,
        "total_quizzes_passed": 0,
        "total_flashcards_reviewed": 0,
        "streak_current": 0,
        "streak_longest": 0,
        "last_active_day": 0,
        "xp": 0,
        "badges": []
    }


def _save_gamification(data):
    _ensure_dirs()
    _GAMIFICATION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def get_gamification_stats():
    return _load_gamification()


def _track_activity():
    """Track daily activity for streaks. Called on course access."""
    data = _load_gamification()
    today = int(_now() / 86400)
    if data["last_active_day"] == today:
        return data
    if data["last_active_day"] == today - 1:
        data["streak_current"] += 1
        if data["streak_current"] > data["streak_longest"]:
            data["streak_longest"] = data["streak_current"]
    elif data["last_active_day"] < today - 1:
        data["streak_current"] = 1
    data["last_active_day"] = today
    _save_gamification(data)
    return data


def _add_xp(amount):
    data = _load_gamification()
    data["xp"] = data.get("xp", 0) + amount
    _check_badges(data)
    _save_gamification(data)
    return data["xp"]


def _check_badges(data):
    badges = data.get("badges", [])
    existing = {b["id"] for b in badges}
    new_badges = []

    checks = [
        ("first_course", "🎓 Первый шаг", "Начать первый курс", data.get("total_courses_started", 0) >= 1),
        ("five_courses", "📚 Эрудит", "Начать 5 курсов", data.get("total_courses_started", 0) >= 5),
        ("ten_courses", "🏆 Полиглот", "Начать 10 курсов", data.get("total_courses_started", 0) >= 10),
        ("completed_first", "✅ Выпускник", "Закончить первый курс", data.get("total_courses_completed", 0) >= 1),
        ("completed_five", "🎓 Магистр", "Закончить 5 курсов", data.get("total_courses_completed", 0) >= 5),
        ("quiz_master", "🧠 Знаток", "Правильно ответить на 50 вопросов", data.get("total_quizzes_passed", 0) >= 50),
        ("streak_3", "🔥 Огонёк", "3 дня подряд", data.get("streak_longest", 0) >= 3),
        ("streak_7", "🔥 Неделя", "7 дней подряд", data.get("streak_longest", 0) >= 7),
        ("streak_30", "🔥 Марафонец", "30 дней подряд", data.get("streak_longest", 0) >= 30),
        ("xp_100", "⭐ Новичок", "100 XP", data.get("xp", 0) >= 100),
        ("xp_1000", "⭐ Опытный", "1000 XP", data.get("xp", 0) >= 1000),
        ("flashcards_100", "🃏 Картограф", "Повторить 100 карточек", data.get("total_flashcards_reviewed", 0) >= 100),
    ]
    for bid, bname, bdesc, condition in checks:
        if condition and bid not in existing:
            new_badges.append({"id": bid, "name": bname, "description": bdesc, "unlocked_at": _now()})
            existing.add(bid)
    data["badges"] = badges + new_badges


# ========== EXPORT: ANKI ==========

def export_to_anki(course_id):
    """Export all flashcards from a course to .apkg using genanki."""
    try:
        import genanki
    except ImportError:
        swallow.note('education_module.export_to_anki:L3214')
        return {"error": "genanki не установлен. Установите: pip install genanki"}

    course = _load_course(course_id)
    if not course:
        return {"error": "Курс не найден"}
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    if not lessons:
        return {"error": "Нет уроков для экспорта"}

    all_cards = []
    for li, lesson in enumerate(lessons):
        for ci, card in enumerate(lesson.get("flashcards") or []):
            front = card.get("front", "") or card.get("term", "")
            back = card.get("back", "") or card.get("definition", "")
            if front and back:
                all_cards.append({
                    "front": front, "back": back,
                    "lesson": li + 1, "lesson_title": lesson.get("title", f"Урок {li+1}")
                })

    if not all_cards:
        return {"error": "Нет флешкарт для экспорта"}

    model_id = int(hashlib.md5(f"infinity_edu_{course_id}".encode()).hexdigest()[:8], 16)
    model = genanki.Model(
        model_id, "Infinity Education Model",
        fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Source"}],
        templates=[{
            "name": "Card 1",
            "qfmt": "{{Front}}<br><small style='color:#888'>{{Source}}</small>",
            "afmt": "{{Front}}<hr id=answer>{{Back}}<br><small style='color:#888'>{{Source}}</small>",
        }],
        css=".card { font-family: 'Inter', Arial; font-size: 16px; text-align: center; color: #111; padding: 20px }"
    )

    deck_id = int(hashlib.md5(f"infinity_deck_{course_id}".encode()).hexdigest()[:8], 16)
    deck = genanki.Deck(deck_id, course.get("title", "Infinity Course"))

    for card in all_cards:
        note = genanki.Note(
            model=model,
            fields=[card["front"], card["back"], f"{course.get('title','')} — {card['lesson_title']}"]
        )
        deck.add_note(note)

    package = genanki.Package(deck)
    cid = _safe_id(course_id)
    if not cid:
        return {"error": "Недопустимый ID курса"}
    out_path = os.path.join(str(EDUCATION_DIR), f"{cid}.apkg")
    package.write_to_file(out_path)
    return {"ok": True, "path": out_path, "cards": len(all_cards), "filename": f"{course['title'][:50]}.apkg"}


# ========== EXPORT: PDF ==========

def export_to_pdf(course_id):
    """Export full course to PDF using weasyprint."""
    try:
        from weasyprint import HTML
    except ImportError:
        swallow.note('education_module.export_to_pdf:L3277')
        return {"error": "weasyprint не установлен. Установите: pip install weasyprint"}

    course = _load_course(course_id)
    if not course:
        return {"error": "Курс не найден"}
    plan = _plan_dict(course.get("plan"))
    lessons = plan.get("lessons", [])
    if not lessons:
        return {"error": "Нет уроков для экспорта"}

    def _esc_html(text):
        if not text: return ""
        return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("\n", "<br>"))

    title = _esc_html(course.get("title", "Курс"))
    overview = _esc_html(plan.get("overview", ""))
    diff = course.get("difficulty", "")

    html_parts = [f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
  @page {{ margin: 2cm; @bottom-center {{ content: counter(page) " / " counter(pages); font-size: 10px; color: #888; }} }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; font-size: 12pt; line-height: 1.6; color: #222; max-width: 700px; margin: 0 auto; }}
  h1 {{ font-size: 22pt; margin-bottom: 4px; }}
  h2 {{ font-size: 16pt; border-bottom: 2px solid #4f46e5; padding-bottom: 4px; margin-top: 24px; color: #4f46e5; }}
  h3 {{ font-size: 13pt; margin-top: 16px; color: #333; }}
  .meta {{ color: #666; font-size: 11pt; }}
  .lesson {{ page-break-before: always; }}
  .objectives li {{ margin-bottom: 4px; }}
  .terms {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }}
  .term {{ background: #f0f0ff; border: 1px solid #ddd; border-radius: 6px; padding: 6px 10px; font-size: 11pt; }}
  .quiz-item {{ background: #f9f9f9; border: 1px solid #eee; border-radius: 8px; padding: 12px; margin: 8px 0; }}
  .quiz-q {{ font-weight: 600; margin-bottom: 6px; }}
  .quiz-opt {{ padding: 4px 0; font-size: 11pt; color: #555; }}
  .correct {{ color: #059669; font-weight: 600; }}
  @media print {{ .lesson {{ page-break-before: always; }} }}
</style></head><body>
<h1>{title}</h1>
<p class="meta">Уровень: {diff}</p>
<p>{overview}</p>
<p class="meta">Уроков: {len(lessons)}</p>
<hr>"""]

    for li, lesson in enumerate(lessons):
        lt = _esc_html(lesson.get("title", f"Урок {li+1}"))
        content = _esc_html(lesson.get("content", ""))
        objectives = lesson.get("objectives", [])
        terms = lesson.get("key_terms", [])

        html_parts.append(f'<div class="lesson">')
        html_parts.append(f'<h2>Урок {li+1}: {lt}</h2>')

        if objectives:
            html_parts.append('<h3>Цели урока</h3><ul class="objectives">')
            for obj in objectives:
                html_parts.append(f'<li>{_esc_html(obj)}</li>')
            html_parts.append('</ul>')

        if terms:
            html_parts.append('<h3>Ключевые термины</h3><div class="terms">')
            for t in terms:
                term = _esc_html(t.get("term", "") if isinstance(t, dict) else str(t))
                defin = _esc_html(t.get("definition", "") if isinstance(t, dict) else "")
                html_parts.append(f'<div class="term"><b>{term}</b>: {defin}</div>')
            html_parts.append('</div>')

        html_parts.append(f'<div class="content">{content}</div>')

        quiz = lesson.get("quiz", [])
        if quiz:
            html_parts.append('<h3>Тест</h3>')
            for qi, q in enumerate(quiz):
                qq = _esc_html(q.get("question", ""))
                qt = q.get("type", "choice")
                html_parts.append(f'<div class="quiz-item"><div class="quiz-q">{qi+1}. {qq}</div>')
                opts = q.get("options", [])
                for oi, o in enumerate(opts):
                    is_correct = str(oi) == str(q.get("correct", "")) if qt == "choice" else False
                    cls = ' class="correct"' if is_correct else ""
                    html_parts.append(f'<div{cls}>{chr(65+oi)}. {_esc_html(o)}</div>')
                expl = q.get("explanation", "")
                if expl:
                    html_parts.append(f'<div style="font-size:10pt;color:#888;margin-top:4px">{_esc_html(expl)}</div>')
                html_parts.append('</div>')

        homework = lesson.get("homework", "")
        if homework:
            html_parts.append(f'<h3>Домашнее задание</h3><div>{_esc_html(homework[:2000])}</div>')

        html_parts.append('</div>')

    html_parts.append('</body></html>')
    html_content = "\n".join(html_parts)

    try:
        cid = _safe_id(course_id)
        if not cid:
            return {"error": "Недопустимый ID курса"}
        out_path = os.path.join(str(EDUCATION_DIR), f"{cid}.pdf")
        HTML(string=html_content).write_pdf(out_path)
        return {"ok": True, "path": out_path, "filename": f"{course['title'][:50]}.pdf"}
    except Exception as e:
        swallow.note('education_module.export_to_pdf:L3380', e)
        return {"error": f"PDF генерация ошибка: {e}"}
