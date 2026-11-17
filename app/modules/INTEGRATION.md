# Интеграция Book Generator в Infinity Search

## Файлы

```
runjson/modules/
├── __init__.py                 # Инициализация пакета
├── book_generator.py           # Основной модуль генерации
├── book_generator_api.py       # API endpoints
└── INTEGRATION.md              # Этот файл

runjson/templates/
└── page_bookgen.html           # Страница интерфейса
```

## Быстрая интеграция

### 1. Добавить в server.py или main.py

```python
# В начале файла
from modules.book_generator_api import ROUTES

# Найдите место, где определяются другие маршруты, и добавьте:
ROUTES.extend([
    ("/api/bookgen/models", handle_models, "GET"),
    ("/api/bookgen/books", handle_list_books, "GET"),
    ("/api/bookgen/start", handle_start, "POST"),
    ("/api/bookgen/plan", handle_plan, "POST"),
    ("/api/bookgen/generate_page", handle_generate_page, "POST"),
    ("/api/bookgen/status/{book_id}", handle_status, "GET"),
    ("/api/bookgen/page/{book_id}/{page_num}", handle_page, "GET"),
    ("/api/bookgen/download/{book_id}/{format}", handle_download, "GET"),
    ("/api/bookgen/delete/{book_id}", handle_delete, "POST"),
    ("/bookgen", handle_page_view, "GET"),
])
```

### 2. Или использовать авто-регистрацию

```python
from modules.book_generator_api import register_routes
register_routes(app)
```

### 3. Добавить ссылку в навигацию

В page_books.html или другом шаблоне добавьте:

```html
<a class="btn" href="/bookgen">
    <i class="fa-solid fa-wand-magic-sparkles"></i>
    <span>Генератор книг</span>
</a>
```

## API Endpoints

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/bookgen/models` | Список LLM моделей |
| GET | `/api/bookgen/books` | Список книг |
| POST | `/api/bookgen/start` | Создать книгу |
| POST | `/api/bookgen/plan` | Генерация плана |
| POST | `/api/bookgen/generate_page` | Генерация главы |
| GET | `/api/bookgen/status/{id}` | Статус книги |
| GET | `/api/bookgen/page/{id}/{num}` | Получить главу |
| GET | `/api/bookgen/download/{id}/{fmt}` | Скачать (fb2/txt/md/plan) |
| POST | `/api/bookgen/delete/{id}` | Удалить книгу |
| GET | `/bookgen` | Страница интерфейса |

## Примеры запросов

```bash
# Список моделей
curl http://localhost:8080/api/bookgen/models

# Создание книги
curl -X POST http://localhost:8080/api/bookgen/start \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Тени древнего города",
    "author": "Иван Писатель",
    "theme": "Детектив в будущем",
    "genre": "Детектив",
    "page_count": 15
  }'

# Генерация плана
curl -X POST http://localhost:8080/api/bookgen/plan \
  -H "Content-Type: application/json" \
  -d '{"id": "abc123", "model": "gemma-4-e2b-it"}'

# Генерация главы
curl -X POST http://localhost:8080/api/bookgen/generate_page \
  -H "Content-Type: application/json" \
  -d '{"id": "abc123", "page": 1, "model": "gemma-4-e2b-it"}'

# Скачать FB2
curl http://localhost:8080/api/bookgen/download/abc123/fb2 --output book.fb2
```

## Конфигурация

Модуль автоматически использует настройки из `config.json`:

```json
{
  "books_dir": "/home/infinity/pars/books/",
  "lm_studio": {
    "url": "http://localhost:1234",
    "model": "gemma-4-e2b-it"
  }
}
```

## Структура сгенерированной книги

```
books_dir/
└── {book_id}/
    ├── meta.json          # Метаданные
    ├── scenario.md        # Сценарий
    ├── characters.md      # Персонажи
    ├── outline.md         # План по главам
    ├── pages/
    │   ├── page_0001.md   # Глава 1
    │   ├── page_0002.md   # Глава 2
    │   └── ...
    ├── book.fb2           # FB2 файл
    ├── book.txt           # TXT файл
    └── book.md            # MD файл
```

## Поддерживаемые жанры

| Жанр | FB2 genre |
|------|-----------|
| Фантастика | sci_fi |
| Фэнтези | fantasy |
| Детектив | detective |
| Роман | prose |
| Триллер | thriller |
| Хоррор | horror |
| Историческая проза | historical |
| Приключения | adventure |
| Художественная проза | prose |
