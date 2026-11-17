"""Ответы AI: сохранение, поиск, список, правка, удаление.

Перенесено из server_backup.py без изменения тел. AI_DIR читается через _sb в
момент вызова: apply_profile_globals переписывает его при смене профиля, поэтому
копирование по значению заморозило бы каталог первого профиля.
"""

import json
import math
import os
import time

import server_backup as _sb
import profile_paths
import swallow
from modules import public_view

_pp = profile_paths.proxy(_sb)   # каталоги профиля запроса; вне запроса — глобалы _sb

def ai_save_answer(question, answer, source=""):
    """Сохранить ответ AI в базу знаний."""
    try:
        aid = f"ai_{int(time.time()*1000)}"
        fp = os.path.join(_pp.AI_DIR, f"{aid}.json")
        data = {
            "id": aid,
            "type": "ai_answer",
            "question": question,
            "answer": answer,
            "source": source,
            "created": time.time()
        }
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return aid
    except Exception:
        swallow.note('ai_answers.ai_save_answer:L36')
        return None

def ai_update_answer(aid, question, answer):
    """Обновить ответ AI."""
    fp = public_view.read_file("ai_answers", f"{aid}.json")
    if not os.path.exists(fp):
        return False
    try:
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        data["question"] = question
        data["answer"] = answer
        data["updated"] = time.time()
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        swallow.note('ai_answers.ai_update_answer:L54')
        return False

def ai_delete_answer(aid):
    """Удалить ответ AI."""
    fp = public_view.read_file("ai_answers", f"{aid}.json")
    if os.path.exists(fp):
        try:
            os.remove(fp)
            return True
        except Exception:
            swallow.note('ai_answers.ai_delete_answer:L65')
            return False
    return False

def ai_search_answers(query):
    """Поиск по предыдущим ответам AI."""
    results = []
    ql = query.lower()
    for d in public_view.read_dirs("ai_answers"):
        try:
            for f in os.listdir(d):
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        data = json.load(fh)
                    text = f"{data.get('question', '')} {data.get('answer', '')}"
                    if ql in text.lower():
                        results.append(data)
                except Exception:
                    swallow.note('ai_answers.ai_search_answers:L85')
                    continue
        except Exception:
            swallow.note('ai_answers.ai_search_answers:L88')
            pass
    results.sort(key=lambda x: x.get("created", 0), reverse=True)
    return results

def ai_list_answers(page=1, per_page=20):
    """Список всех ответов AI."""
    answers = []
    for d in public_view.read_dirs("ai_answers"):
        try:
            for f in os.listdir(d):
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        data = json.load(fh)
                    answers.append({
                        "id": data.get("id", ""),
                        "question": data.get("question", "")[:100],
                        "answer_preview": data.get("answer", "")[:150],
                        "created": data.get("created", 0)
                    })
                except Exception:
                    swallow.note('ai_answers.ai_list_answers:L111')
                    continue
        except Exception:
            swallow.note('ai_answers.ai_list_answers:L114')
            pass
    answers.sort(key=lambda x: x.get("created", 0), reverse=True)
    total = len(answers)
    start = (page - 1) * per_page
    return {
        "answers": answers[start:start + per_page],
        "total": total,
        "page": page,
        "pages": max(1, math.ceil(total / per_page))
    }
