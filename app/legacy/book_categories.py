"""Секция 'КАТЕГОРИИ КНИГ (с AI сортировкой)' монолита `app/server_backup.py`.

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
# ===================== КАТЕГОРИИ КНИГ (с AI сортировкой) =====================

_BOOK_CATEGORIES_FILE = os.path.expanduser("~/.infinity/book_categories.json")
_categories_lock = threading.Lock()

def _load_categories():
    """Загрузить категории книг."""
    try:
        if os.path.exists(_BOOK_CATEGORIES_FILE):
            with open(_BOOK_CATEGORIES_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        swallow.note('book_categories._load_categories:L37')
        pass
    return {"categories": [], "uncategorized": []}

def _save_categories(data):
    """Сохранить категории."""
    try:
        os.makedirs(os.path.dirname(_BOOK_CATEGORIES_FILE), exist_ok=True)
        with open(_BOOK_CATEGORIES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving categories: {e}")
        return False

def book_categories_list():
    """Получить все категории с книгами."""
    with _categories_lock:
        return _load_categories()

def book_categories_create(name, icon="📚"):
    """Создать категорию."""
    if not name or not name.strip():
        return {"error": "Введите название"}
    with _categories_lock:
        data = _load_categories()
        cat_id = f"cat_{int(time.time()*1000)}"
        data["categories"].append({
            "id": cat_id, "name": name.strip(), "icon": icon,
            "books": [], "order": len(data["categories"]), "created": time.time()
        })
        _save_categories(data)
    return {"ok": True, "id": cat_id}

def book_categories_rename(cat_id, name):
    """Переименовать категорию."""
    if not name.strip():
        return {"error": "Введите название"}
    with _categories_lock:
        data = _load_categories()
        for c in data["categories"]:
            if c["id"] == cat_id:
                c["name"] = name.strip()
                _save_categories(data)
                return {"ok": True}
    return {"error": "Категория не найдена"}

def book_categories_delete(cat_id):
    """Удалить категорию (книги переходят в uncategorized)."""
    with _categories_lock:
        data = _load_categories()
        for c in data["categories"]:
            if c["id"] == cat_id:
                data["uncategorized"].extend(c.get("books", []))
                data["categories"] = [x for x in data["categories"] if x["id"] != cat_id]
                _save_categories(data)
                return {"ok": True}
    return {"error": "Категория не найдена"}

def book_categories_add_book(cat_id, book_path):
    """Добавить книгу в категорию."""
    with _categories_lock:
        data = _load_categories()
        # Удаляем из других категорий
        for c in data["categories"]:
            if book_path in c.get("books", []):
                c["books"].remove(book_path)
        # Удаляем из uncategorized
        if book_path in data.get("uncategorized", []):
            data["uncategorized"].remove(book_path)
        # Добавляем в целевую
        for c in data["categories"]:
            if c["id"] == cat_id:
                if "books" not in c:
                    c["books"] = []
                if book_path not in c["books"]:
                    c["books"].append(book_path)
                _save_categories(data)
                return {"ok": True}
    return {"error": "Категория не найдена"}

def book_categories_remove_book(cat_id, book_path):
    """Удалить книгу из категории (переходит в uncategorized)."""
    with _categories_lock:
        data = _load_categories()
        for c in data["categories"]:
            if c["id"] == cat_id and book_path in c.get("books", []):
                c["books"].remove(book_path)
                if book_path not in data.get("uncategorized", []):
                    data["uncategorized"].append(book_path)
                _save_categories(data)
                return {"ok": True}
    return {"error": "Категория не найдена"}

def book_categories_reorder(cat_id, book_path, new_position):
    """Изменить порядок книги в категории."""
    with _categories_lock:
        data = _load_categories()
        for c in data["categories"]:
            if c["id"] == cat_id and book_path in c.get("books", []):
                c["books"].remove(book_path)
                pos = min(new_position, len(c["books"]))
                c["books"].insert(pos, book_path)
                _save_categories(data)
                return {"ok": True}
    return {"error": "Категория не найдена"}

def _laya_books_enabled():
    """Доступен ли движок laya для классификации книг (и включена ли фича)."""
    try:
        import laya_client
    except Exception:
        swallow.note('book_categories._laya_books_enabled:L149')
        return False
    try:
        return bool(laya_client.feature("books_classify"))
    except Exception:
        swallow.note('book_categories._laya_books_enabled:L154')
        return False


def book_categories_ai_sort():
    """AI сортировка ВСЕХ книг по категориям — пакетная обработка."""
    data = _load_categories()
    all_books = find_books()

    uncategorized = []
    already_categorized = set()
    for c in data.get("categories", []):
        already_categorized.update(c.get("books", []))

    for book in all_books:
        if book["path"] not in already_categorized:
            uncategorized.append(book)

    if not uncategorized:
        return {"ok": True, "message": "Все книги уже распределены", "sorted": 0}

    if not data.get("categories"):
        return {"error": "Сначала создайте категории"}

    cat_names = [c["name"] for c in data["categories"]]
    cats_json = json.dumps(cat_names, ensure_ascii=False)

    BATCH_SIZE = 20
    total_sorted = 0

    for batch_start in range(0, len(uncategorized), BATCH_SIZE):
        batch = uncategorized[batch_start:batch_start + BATCH_SIZE]

        book_list = []
        for b in batch:
            try:
                bd = get_book_content(b["path"])
                title = bd.get("title", b["name"])
                author = bd.get("author", "")
                preview = (bd.get("full_text", "") or "")[:150]
                book_list.append({"title": title, "author": author, "preview": preview})
            except Exception:
                swallow.note('book_categories.book_categories_ai_sort:L196')
                book_list.append({"title": b["name"], "author": "", "preview": ""})

        books_json = json.dumps(book_list, ensure_ascii=False)

        # Шаг 1 (опционально): классификация движком laya — без генерации.
        # Уверенно разобранные книги сразу раскладываются по категориям,
        # остальные уходят в LLM-путь ниже (он не меняется).
        if _laya_books_enabled():
            try:
                import laya_client
                states = ["%s. %s. %s" % (b.get("title", ""), b.get("author", ""),
                                          (b.get("preview") or "")[:300]) for b in book_list]
                questions = {
                    "category": {
                        "type": "choice",
                        "instructions": "К какой категории относится эта книга?",
                        "criteria": {n: n for n in cat_names},
                    }
                }
                _laya_results = laya_client.decide_batch(states, questions)
            except Exception as e:
                logger.warning(f"laya book sort batch {batch_start} error: {e}")
                _laya_results = None
            if _laya_results and len(_laya_results) == len(batch):
                cat_map_laya = {c["name"].lower(): c["id"] for c in data["categories"]}
                resolved_paths = set()
                for b, res in zip(batch, _laya_results):
                    ans = ((res or {}).get("answers") or {}).get("category") or {}
                    cname = str(ans.get("choice") or "").strip()
                    try:
                        conf = float(ans.get("answer_confidence") or ans.get("confidence") or 0.0)
                    except Exception:
                        swallow.note('book_categories.book_categories_ai_sort:L229')
                        conf = 0.0
                    if not cname or conf < 0.5:
                        continue
                    cat_id = cat_map_laya.get(cname.lower())
                    if not cat_id:
                        continue
                    for c in data["categories"]:
                        if c["id"] == cat_id:
                            if "books" not in c:
                                c["books"] = []
                            if b["path"] not in c["books"]:
                                c["books"].append(b["path"])
                                total_sorted += 1
                            break
                    resolved_paths.add(b["path"])
                if resolved_paths:
                    _save_categories(data)
                    keep = [i for i, b in enumerate(batch) if b["path"] not in resolved_paths]
                    batch = [batch[i] for i in keep]
                    book_list = [book_list[i] for i in keep]
                    books_json = json.dumps(book_list, ensure_ascii=False)
                    if not book_list:
                        continue

        prompt = (
            f"You are a book categorizer. Distribute books into categories.\n"
            f"Categories: {cats_json}\n"
            f"Books: {books_json}\n\n"
            f"Respond with ONLY a JSON array of objects.\n"
            f'Each object: {{"book":"exact book title","category":"exact category name"}}\n'
            f"No markdown, no explanations, no code blocks. Just the JSON array."
        )

        try:
            # Прямой запрос к LM Studio
            body = {
                "model": resolve_llm_model(LM_STUDIO_MODEL),
                "messages": [
                    {"role": "system", "content": "You respond with ONLY valid JSON. No text before or after. Use double quotes."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.1,
                "max_tokens": 1500
            }
            req = urllib.request.Request(
                get_llm_url(),
                data=json.dumps(body).encode('utf-8'),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            if get_llm_api_key():
                req.add_header("Authorization", f"Bearer {get_llm_api_key()}")

            logger.info(f"AI sort batch {batch_start}: отправка запроса к LLM...")
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode())

            answer = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            logger.info(f"AI sort batch {batch_start}: получен ответ ({len(answer)} символов)")
            answer = re.sub(r'^```json\s*', '', answer, flags=re.MULTILINE)
            answer = re.sub(r'\s*```\s*$', '', answer, flags=re.MULTILINE)
            answer = re.sub(r'^```\s*', '', answer, flags=re.MULTILINE)
            answer = re.sub(r'\s*```$', '', answer, flags=re.MULTILINE)
            answer = answer.strip()

            json_start = answer.find('[')
            json_end = answer.rfind(']') + 1

            if json_start < 0 or json_end <= json_start:
                logger.warning(f"AI sort batch {batch_start}: no JSON found")
                continue

            raw_json = answer[json_start:json_end]

            try:
                assignments = json.loads(raw_json)
            except json.JSONDecodeError:
                try:
                    assignments = json.loads(raw_json.replace("'", '"'))
                except Exception:
                    logger.warning(f"AI sort batch {batch_start}: JSON parse error")
                    continue

            if not isinstance(assignments, list):
                continue

            cat_map = {}
            for c in data["categories"]:
                cat_map[c["name"].lower()] = c["id"]

            for a in assignments:
                if not isinstance(a, dict):
                    continue
                book_name = str(a.get("book", "")).strip().lower()
                cat_name = str(a.get("category", "")).strip()

                cat_id = cat_map.get(cat_name.lower())
                if not cat_id:
                    for cn, cid in cat_map.items():
                        if cn in cat_name.lower() or cat_name.lower() in cn:
                            cat_id = cid
                            break

                if cat_id and book_name:
                    for b in batch:
                        if b["name"].lower() == book_name or book_name in b["name"].lower() or b["name"].lower() in book_name:
                            for c in data["categories"]:
                                if c["id"] == cat_id:
                                    if "books" not in c:
                                        c["books"] = []
                                    if b["path"] not in c["books"]:
                                        c["books"].append(b["path"])
                                        total_sorted += 1
                                    break
                            break

            _save_categories(data)

        except Exception as e:
            logger.warning(f"AI sort batch {batch_start} error: {e}")
            continue

    return {"ok": True, "sorted": total_sorted, "total": len(uncategorized)}



# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'LM_STUDIO_MODEL',
    'find_books',
    'get_book_content',
    'get_llm_api_key',
    'get_llm_url',
    'json',
    'logger',
    'os',
    're',
    'resolve_llm_model',
    'threading',
    'time',
    'urllib',
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
