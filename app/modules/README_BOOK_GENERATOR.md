# Book Generator — Генератор книг для Infinity Search

## Описание

Модуль генерации книг через LM Studio/OpenAI-совместимый API. Создаёт книги в формате FB2, TXT, MD.

## Структура файлов

```
runjson/modules/
├── __init__.py              # Инициализация пакета
├── book_generator.py        # Основной модуль генерации
└── book_generator_api.py    # API endpoints для интеграции
```

## Установка

Файлы уже находятся в `runjson/modules/`. Дополнительная установка не требуется.

## Интеграция с основным сервером

### Вариант 1: В main.py/server.py

```python
# Добавьте в начало файла
from modules.book_generator_api import register_book_routes

# После создания app
register_book_routes(app)
```

### Вариант 2: Прямое использование

```python
from modules.book_generator import BookGenerator

gen = BookGenerator()  # Автоматически загрузит настройки из config.json

# Создание книги
book = gen.create_book(
    title="Тени древнего города",
    author="Иван Писатель",
    theme="Детектив в будущем",
    genre="Детектив",
    page_count=15,
    model="gemma-4-e2b-it"
)

# Генерация плана
book.generate_plan("gemma-4-e2b-it", gen.llm)

# Генерация страниц
for i in range(1, 16):
    book.generate_page(i, "gemma-4-e2b-it", gen.llm)

# Экспорт в FB2
fb2_path = book.export_fb2()
print(f"Книга сохранена: {fb2_path}")
```

## API Endpoints

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/bookgen/models` | Список доступных LLM моделей |
| GET | `/api/bookgen/books` | Список всех сгенерированных книг |
| POST | `/api/bookgen/start` | Создать новую книгу |
| POST | `/api/bookgen/plan` | Сгенерировать план книги |
| POST | `/api/bookgen/generate_page` | Сгенерировать страницу |
| GET | `/api/bookgen/status/{id}` | Получить статус книги |
| GET | `/api/bookgen/download/{id}/{format}` | Скачать книгу (fb2/txt/md/plan) |
| POST | `/api/bookgen/delete/{id}` | Удалить книгу |

### Примеры запросов

```bash
# Список моделей
curl http://localhost:8080/api/bookgen/models

# Создание книги
curl -X POST http://localhost:8080/api/bookgen/start \
  -H "Content-Type: application/json" \
  -d '{"title": "Тест", "author": "Автор", "theme": "Тема", "page_count": 5}'

# Генерация плана
curl -X POST http://localhost:8080/api/bookgen/plan \
  -H "Content-Type: application/json" \
  -d '{"id": "abc123", "model": "gemma-4-e2b-it"}'

# Генерация страницы
curl -X POST http://localhost:8080/api/bookgen/generate_page \
  -H "Content-Type: application/json" \
  -d '{"id": "abc123", "page": 1, "model": "gemma-4-e2b-it"}'

# Скачать FB2
curl http://localhost:8080/api/bookgen/download/abc123/fb2 --output book.fb2
```

## Конфигурация

Модуль автоматически загружает настройки из `config.json`:

```json
{
  "books_dir": "/home/infinity/pars/books/",
  "lm_studio": {
    "url": "http://localhost:1234",
    "model": "gemma-4-e2b-it"
  }
}
```

## Формат FB2

Книги экспортируются в стандартном формате FictionBook 2.0:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"
             xmlns:l="http://www.w3.org/1999/xlink">
  <description>
    <title-info>
      <genre>detective</genre>
      <author>
        <first-name>Иван</first-name>
        <last-name>Писатель</last-name>
      </author>
      <book-title>Название</book-title>
      <date>2024-01-01</date>
      <lang>ru</lang>
    </title-info>
  </description>
  <body>
    <title><p>Название</p></title>
    <section>
      <title><p>Глава 1</p></title>
      <p>Текст главы...</p>
    </section>
  </body>
</FictionBook>
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

## CLI команды

```bash
# Список моделей
python -m modules.book_generator --list-models

# Список книг
python -m modules.book_generator --list-books

# С указанием директории
python -m modules.book_generator --books-dir /path/to/books --list-books
```

## Решение проблем

### LM Studio недоступен
Убедитесь, что LM Studio запущен и сервер включён (http://localhost:1234).

### Книги не появляются в библиотеке
Проверьте `books_dir` в config.json — должен совпадать с директорией библиотеки.

### Ошибки FB2
FB2 строго валидируется. Модуль автоматически экранирует спецсимволы.
