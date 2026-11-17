"""Хранилище папок и категорий книг.

Перенесено из server_backup.py без изменения тел. Блокировки, модель LLM и
логгер читаются через _sb в момент вызова, поэтому состояние остаётся общим с
ещё не перенесенными функциями монолита; загрузка и сохранение файлов тоже
идут через _sb, пока их реализация не вынесена сюда же.
"""

import json
import re
import time
import urllib.error
import urllib.request

import server_backup as _sb
import swallow

def book_categories_list():
    """Получить все категории с книгами."""
    with _sb._categories_lock:
        return _sb._load_categories()

def book_categories_create(name, icon="📚"):
    """Создать категорию."""
    if not name or not name.strip():
        return {"error": "Введите название"}
    with _sb._categories_lock:
        data = _sb._load_categories()
        cat_id = f"cat_{int(time.time()*1000)}"
        data["categories"].append({
            "id": cat_id, "name": name.strip(), "icon": icon,
            "books": [], "order": len(data["categories"]), "created": time.time()
        })
        _sb._save_categories(data)
    return {"ok": True, "id": cat_id}

def book_categories_rename(cat_id, name):
    """Переименовать категорию."""
    if not name.strip():
        return {"error": "Введите название"}
    with _sb._categories_lock:
        data = _sb._load_categories()
        for c in data["categories"]:
            if c["id"] == cat_id:
                c["name"] = name.strip()
                _sb._save_categories(data)
                return {"ok": True}
    return {"error": "Категория не найдена"}

def book_categories_delete(cat_id):
    """Удалить категорию (книги переходят в uncategorized)."""
    with _sb._categories_lock:
        data = _sb._load_categories()
        for c in data["categories"]:
            if c["id"] == cat_id:
                data["uncategorized"].extend(c.get("books", []))
                data["categories"] = [x for x in data["categories"] if x["id"] != cat_id]
                _sb._save_categories(data)
                return {"ok": True}
    return {"error": "Категория не найдена"}

def book_categories_add_book(cat_id, book_path):
    """Добавить книгу в категорию."""
    with _sb._categories_lock:
        data = _sb._load_categories()
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
                _sb._save_categories(data)
                return {"ok": True}
    return {"error": "Категория не найдена"}

def book_categories_remove_book(cat_id, book_path):
    """Удалить книгу из категории (переходит в uncategorized)."""
    with _sb._categories_lock:
        data = _sb._load_categories()
        for c in data["categories"]:
            if c["id"] == cat_id and book_path in c.get("books", []):
                c["books"].remove(book_path)
                if book_path not in data.get("uncategorized", []):
                    data["uncategorized"].append(book_path)
                _sb._save_categories(data)
                return {"ok": True}
    return {"error": "Категория не найдена"}

def book_categories_reorder(cat_id, book_path, new_position):
    """Изменить порядок книги в категории."""
    with _sb._categories_lock:
        data = _sb._load_categories()
        for c in data["categories"]:
            if c["id"] == cat_id and book_path in c.get("books", []):
                c["books"].remove(book_path)
                pos = min(new_position, len(c["books"]))
                c["books"].insert(pos, book_path)
                _sb._save_categories(data)
                return {"ok": True}
    return {"error": "Категория не найдена"}

def book_categories_ai_sort():
    """AI сортировка ВСЕХ книг по категориям — пакетная обработка."""
    data = _sb._load_categories()
    all_books = _sb.find_books()

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
                bd = _sb.get_book_content(b["path"])
                title = bd.get("title", b["name"])
                author = bd.get("author", "")
                preview = (bd.get("full_text", "") or "")[:150]
                book_list.append({"title": title, "author": author, "preview": preview})
            except Exception:
                swallow.note('books_store.book_categories_ai_sort:L147')
                book_list.append({"title": b["name"], "author": "", "preview": ""})

        books_json = json.dumps(book_list, ensure_ascii=False)

        # Шаг 1 (опционально): классификация движком laya — без генерации.
        # Уверенно разобранные книги сразу раскладываются по категориям,
        # остальные уходят в LLM-путь ниже (он не меняется).
        if _sb._laya_books_enabled():
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
                _sb.logger.warning(f"laya book sort batch {batch_start} error: {e}")
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
                        swallow.note('books_store.book_categories_ai_sort:L180')
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
                    _sb._save_categories(data)
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
                "model": _sb.resolve_llm_model(_sb.LM_STUDIO_MODEL),
                "messages": [
                    {"role": "system", "content": "You respond with ONLY valid JSON. No text before or after. Use double quotes."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.1,
                "max_tokens": 1500
            }
            req = urllib.request.Request(
                _sb.get_llm_url(),
                data=json.dumps(body).encode('utf-8'),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            if _sb.get_llm_api_key():
                req.add_header("Authorization", f"Bearer {_sb.get_llm_api_key()}")

            _sb.logger.info(f"AI sort batch {batch_start}: отправка запроса к LLM...")
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode())

            answer = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            _sb.logger.info(f"AI sort batch {batch_start}: получен ответ ({len(answer)} символов)")
            answer = re.sub(r'^```json\s*', '', answer, flags=re.MULTILINE)
            answer = re.sub(r'\s*```\s*$', '', answer, flags=re.MULTILINE)
            answer = re.sub(r'^```\s*', '', answer, flags=re.MULTILINE)
            answer = re.sub(r'\s*```$', '', answer, flags=re.MULTILINE)
            answer = answer.strip()

            json_start = answer.find('[')
            json_end = answer.rfind(']') + 1

            if json_start < 0 or json_end <= json_start:
                _sb.logger.warning(f"AI sort batch {batch_start}: no JSON found")
                continue

            raw_json = answer[json_start:json_end]

            try:
                assignments = json.loads(raw_json)
            except json.JSONDecodeError:
                try:
                    assignments = json.loads(raw_json.replace("'", '"'))
                except Exception:
                    _sb.logger.warning(f"AI sort batch {batch_start}: JSON parse error")
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

            _sb._save_categories(data)

        except Exception as e:
            _sb.logger.warning(f"AI sort batch {batch_start} error: {e}")
            continue

    return {"ok": True, "sorted": total_sorted, "total": len(uncategorized)}

def book_folders_list():
    """Получить дерево папок с книгами."""
    with _sb._book_folders_lock:
        data = _sb._load_book_folders()
    folders = data.get("folders", [])
    books_map = data.get("books_in_folders", {})
    # Собираем книги для каждой папки (включая вложенные)
    def collect_books_for_folder(folder_id):
        books = books_map.get(folder_id, [])
        # Добавляем книги из дочерних папок
        for child in folders:
            if child.get("parent_id") == folder_id:
                books.extend(collect_books_for_folder(child["id"]))
        return books
    # Добавляем count в каждую папку
    for folder in folders:
        folder["book_count"] = len(collect_books_for_folder(folder["id"]))
    return {"folders": folders, "books_in_folders": books_map}

def book_folders_create(name, parent_id=None, icon="📁"):
    """Создать папку."""
    if not name or not name.strip():
        return {"error": "Введите название папки"}
    with _sb._book_folders_lock:
        data = _sb._load_book_folders()
        folder_id = f"bf_{int(time.time()*1000)}"
        folder = {"id": folder_id, "name": name.strip(), "icon": icon, "parent_id": parent_id, "created": time.time()}
        data["folders"].append(folder)
        _sb._save_book_folders(data)
    return {"ok": True, "id": folder_id, "folder": folder}

def book_folders_rename(folder_id, name):
    """Переименовать папку."""
    if not name.strip():
        return {"error": "Введите название"}
    with _sb._book_folders_lock:
        data = _sb._load_book_folders()
        for f in data["folders"]:
            if f["id"] == folder_id:
                f["name"] = name.strip()
                _sb._save_book_folders(data)
                return {"ok": True}
    return {"error": "Папка не найдена"}

def book_folders_move(folder_id, new_parent_id):
    """Переместить папку (изменить родителя)."""
    # Защита от циклов
    if folder_id == new_parent_id:
        return {"error": "Нельзя переместить папку в саму себя"}
    with _sb._book_folders_lock:
        data = _sb._load_book_folders()
        for f in data["folders"]:
            if f["id"] == folder_id:
                f["parent_id"] = new_parent_id
                _sb._save_book_folders(data)
                return {"ok": True}
    return {"error": "Папка не найдена"}

def book_folders_delete(folder_id):
    """Удалить папку (книги не удаляются)."""
    system_folders = {"root", "all", "uncategorized"}
    if folder_id in system_folders:
        return {"error": "Нельзя удалить системную папку"}
    with _sb._book_folders_lock:
        data = _sb._load_book_folders()
        data["folders"] = [f for f in data["folders"] if f["id"] != folder_id]
        # Перемещаем книги из удалённой папки в root
        if folder_id in data.get("books_in_folders", {}):
            del data["books_in_folders"][folder_id]
        _sb._save_book_folders(data)
    return {"ok": True}

def book_folders_add_book(folder_id, book_path):
    """Добавить книгу в папку."""
    if not folder_id or not book_path:
        return {"error": "Выберите папку и книгу"}
    with _sb._book_folders_lock:
        data = _sb._load_book_folders()
        if folder_id not in data["books_in_folders"]:
            data["books_in_folders"][folder_id] = []
        if book_path not in data["books_in_folders"][folder_id]:
            data["books_in_folders"][folder_id].append(book_path)
        _sb._save_book_folders(data)
    return {"ok": True}

def book_folders_remove_book(folder_id, book_path):
    """Удалить книгу из папки."""
    with _sb._book_folders_lock:
        data = _sb._load_book_folders()
        if folder_id in data["books_in_folders"]:
            data["books_in_folders"][folder_id] = [
                b for b in data["books_in_folders"][folder_id] if b != book_path
            ]
            _sb._save_book_folders(data)
    return {"ok": True}
