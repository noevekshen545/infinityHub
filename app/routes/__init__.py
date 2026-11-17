"""Infinity Search FastAPI route modules.

Каждый модуль экспортирует `router` (APIRouter) с группой маршрутов.
Подключаются в main.py через app.include_router().
"""

import sys, os

# Добавляем корень проекта в путь (чтобы import server работал)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
