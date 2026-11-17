#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Создать стандартные профили обучения (data/education/profiles/<id>.json).

Профиль влияет не только на тесты, но и на структуру курса:
  quiz_distribution — пропорции типов вопросов в тесте;
  section_types     — какие блоки будут в каждом уроке;
  phase_prompts     — доп. указания ИИ для фаз (syllabus и др.);
  template_ids      — шаблоны оформления тестов.

Запуск:  python app/create_learning_profiles.py [--force]
"""
import argparse
import json
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

from modules.education_module import BASE_PROFILES_DIR

# id, имя, описание, акцент (для quiz_distribution)
PROFILES = [
    # ---- Языки ----
    ("english", "Английский язык",
     "Английский язык: грамматика, лексика, произношение, письмо и разговорная практика.", "language"),
    ("german", "Немецкий язык",
     "Немецкий язык: грамматика (падежи, порядок слов), лексика, аудирование, письмо.", "language"),
    ("spanish", "Испанский язык",
     "Испанский язык: времена, субъюнктив, лексика, фонетика, чтение и разговор.", "language"),
    ("french", "Французский язык",
     "Французский язык: артикли, согласование, субъюнктив, лексика, аудирование.", "language"),
    ("chinese", "Китайский язык",
     "Китайский язык: тоны, пиньинь, иероглифы, грамматические частицы, базовые фразы.", "language"),
    ("japanese", "Японский язык",
     "Японский язык: хирагана и катокана, кандзи, частицы, глагольные формы, чтение.", "language"),

    # ---- Программирование ----
    ("python", "Python",
     "Python: синтаксис, типы данных, функции, ООП, исключения, работа с файлами и модулями.", "code"),
    ("javascript", "JavaScript",
     "JavaScript: язык браузера, асинхронность, DOM, модули, работа с массивами и API.", "code"),
    ("cpp", "C++",
     "C++: указатели, память, классы, шаблоны, STL, многопоточность.", "code"),
    ("sql", "SQL и базы данных",
     "SQL: SELECT, JOIN, GROUP BY, индексы, транзакции, проектирование схем БД.", "code"),
    ("algorithms", "Алгоритмы и структуры данных",
     "Алгоритмы: сортировки, обход графов, динамическое программирование, сложность.", "code"),

    # ---- Гуманитарные и естественные ----
    ("psychology", "Психология",
     "Психология: познание, память, эмоции, мотивация, социальные процессы, методы исследований.", "science"),
    ("philosophy", "Философия",
     "Философия: онтология, эпистемология, этика, логика, история мысли, аргументация.", "science"),
    ("history", "История",
     "История: хронология, ключевые события и личности, причины и следствия, источники.", "science"),
    ("economics", "Экономика",
     "Экономика: спрос и предложение, макро- и микроэкономика, рынки, финансы, модели.", "science"),
    ("biology", "Биология",
     "Биология: клетка, генетика, эволюция, анатомия, экосистемы, биохимия.", "science"),
    ("mathematics", "Математика",
     "Математика: алгебра, геометрия, теория вероятностей, статистика, математический анализ.", "science"),

    # ---- Базовый (используется, если профиль не выбран) ----
    ("universal", "Универсальный",
     "Универсальный профиль обучения: смешанные задачи, общая логика, основы критического мышления.", "general"),
]

# Пропорции типов вопросов — чем «техничнее» предмет, тем больше
# объективных и проверяемых форматов.
DISTRIBUTIONS = {
    "language": {"choice": 5, "multiple": 3, "fill_in": 4, "true_false": 2, "matching": 2, "ordering": 1},
    "code":     {"choice": 4, "multiple": 2, "fill_in": 3, "true_false": 1, "ordering": 3, "matching": 2},
    "science":  {"choice": 5, "multiple": 3, "fill_in": 2, "true_false": 2, "matching": 2, "ordering": 1},
    "general":  {"choice": 5, "multiple": 3, "fill_in": 3, "true_false": 3, "matching": 3, "ordering": 3},
}

QUIZ_TYPES = ["choice", "multiple", "fill_in", "true_false", "matching", "ordering", "ai_open"]


def build_profile(pid, name, desc, accent):
    return {
        "id": pid,
        "name": name,
        "description": desc,
        "icon": "graduation-cap",
        "quiz_types": QUIZ_TYPES,
        "quiz_distribution": DISTRIBUTIONS[accent],
        "section_types": ["concept", "example", "practice", "summary", "mistakes"],
        "phase_prompts": {
            "syllabus": (
                f"Профиль «{name}». {desc} "
                "Программа должна идти от простого к сложному, каждый модуль — "
                "с конкретными практическими заданиями и критериями освоения."
            ),
        },
        "template_ids": ["multiple_choice", "fill_in_blank", "ai_open"],
        "builtin": True,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="перезаписать существующие")
    args = ap.parse_args()

    BASE_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    created, skipped = 0, 0
    for pid, name, desc, accent in PROFILES:
        path = BASE_PROFILES_DIR / f"{pid}.json"
        if path.exists() and not args.force:
            print(f"  skip  {pid} (уже есть)")
            skipped += 1
            continue
        data = build_profile(pid, name, desc, accent)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        print(f"  create {pid:12s} {name}")
        created += 1
    print(f"\nГотово: создано {created}, пропущено {skipped}. Каталог: {BASE_PROFILES_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())