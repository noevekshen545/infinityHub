#!/usr/bin/env python3
"""
Режим «Custom» заметок: ИИ превращает текст заметки в переиспользуемый
интерактивный артефакт.

Замысел. Пользователь пишет в заметке обычный текст — например, список
позиций. Описывает в настройках режима, что нужно («это режим таблиц:
собери из текста интерактивную таблицу»). ИИ возвращает JSON с готовым
HTML-приложением. Приложение сохраняется на диск и показывается в
превью как sandbox-iframe, поэтому работает дальше без ИИ: искать, сортировать,
отмечать. Связь с заметкой — короткий маркер в тексте вида:

    [CUSTOM]my-table

Файл артефакта лежит рядом с заметками, но вне их обхода: папка
_artifacts_ пропускается как системная, поэтому микросервисы не сыплются
в список заметок и в поиск.

Безопасность. ИИ пишет HTML и JS, которые исполняются в браузере
пользователя, поэтому разметка чистится ДО сохранения: вырезаются
<script src> с внешними адресами, обращения к сети (fetch, XHR,
WebSocket, EventSource), доступ к top/parent/opener, localStorage,
cookie и data:-URI из внешних источников. iframe запускается с
sandbox="allow-scripts" — без allow-same-origin, поэтому скрипт внутри
не видит ни cookies, ни localStorage приложения и не может уйти на его
API.

Модуль не знает про FastAPI: чистые функции, их вызывает main.py.
"""

import json
import os
import re
import time
import unicodedata
import hashlib
import swallow

ARTIFACT_DIRNAME = "_artifacts_"
MAX_ARTIFACT_BYTES = 512 * 1024      # один микросервис — не больше 512 КБ
MAX_SOURCE_CHARS = 20000# сколько текста заметки уходит в ИИ
MAX_PROMPT_CHARS = 4000

# Бюджет генерации. Стандартный ответ — 2048 токенов, но приложение с
# данными заметки (десятки записей в JS + стили + разметка) в них не
# помещалось: модель обрывалась на середине массива — «price:2500,stoc»
# и дальше тишина. Отсюда большой лимит ответа; окно контекста должно быть
# заведомо больше промпта с примером плюс такого ответа.
ARTIFACT_MAX_TOKENS = 50000
ARTIFACT_CONTEXT_TOKENS = 65536

# ────────────────────────────  шаблоны режимов  ────────────────────────────
# Это подсказки для пользователя: он берёт нужный, переименовывает и
# дополняет под свою задачу. Сами режимы хранятся в заметке (поля
# custom_mode / custom_prompt), поэтому шаблоны — только стартовые точки.

MODE_TEMPLATES = [
    {
        "key": "table",
        "name": "Таблица",
        "icon": "fa-table-cells",
        "has_template": True,
        "prompt": (
            "Это режим таблиц. Возьми весь текст заметки и преврати его в интерактивную "
            "таблицу: каждая строка текста — строка таблицы, каждое поле — колонка. "
            "Добавь поиск по всем колонкам, сортировку по клику на заголовок и подсветку "
            "строк. Ширину колонок подбери сам, чтобы таблица читалась без горизонтальной "
            "прокрутки. Если в тексте есть разделители (табы, запятые, двоеточия, «-») — "
            "используй их как границы ячеек."
        ),
    },
    {
        "key": "database",
        "name": "База данных",
        "icon": "fa-database",
        "prompt": (
            "Это режим базы данных. Разбери текст заметки на записи и выдели в каждой "
            "поля (название, статус, дата, автор, любые другие). Построй карточки записей "
            "с поиском, фильтрами по каждому полю и сортировкой. Добавь счётчик записей "
            "и подсветку при поиске. Поля определяй по смыслу текста, не проси уточнений."
        ),
    },
    {
        "key": "cards",
        "name": "Карточки",
        "icon": "fa-layer-group",
        "has_template": True,
        "prompt": (
            "Это режим карточек. Разбей текст заметки на небольшие карточки — по одной "
            "мысли, задаче или факту на карточку. Внутри карточки выдели заголовок и "
            "текст. Сделай сетку, которая перестраивается по ширине окна. Добавь фильтр "
            "по первой букве или по тегу, если он есть в тексте."
        ),
    },
    {
        "key": "timeline",
        "name": "Таймлайн",
        "icon": "fa-timeline",
        "prompt": (
            "Это режим таймлайна. Найди в тексте события и даты, выстрой их "
            "хронологически и покажи вертикальной лентой: дата рядом, описание события "
            "напротив. Годы без конкретной даты собери вместе. Добавь фильтр по году."
        ),
    },
    {
        "key": "checklist",
        "name": "Чек-лист",
        "icon": "fa-list-check",
        "has_template": True,
        "prompt": (
            "Это режим чек-листа. Преврати каждую строку текста в пункт списка с "
            "флажком. Пункты должны запоминать состояние в рамках открытой страницы. "
            "Покажи прогресс: «сделано 3 из 12», отдельные блоки для выполненного и "
            "оставшегося. Добавь кнопку «Сбросить всё»."
        ),
    },
    {
        "key": "calculator",
        "name": "Калькулятор",
        "icon": "fa-calculator",
        "prompt": (
            "Это режим калькулятора. Найди в тексте формулы, расчёты или числовые "
            "задачи и построй блок, где результат пересчитывается сам при изменении "
            "исходных чисел. Реализуй вычисления на JS прямо в разметке, без внешних "
            "библиотек. Покажи исходную формулу и результат рядом."
        ),
    },
    {
        "key": "converter",
        "name": "Конвертер",
        "icon": "fa-right-left",
        "prompt": (
            "Это режим конвертера. Определи формат исходного текста в заметке и целевой "
            "формат, о котором сказано в описании режима, и сделай двусторонний "
            "конвертер: поле ввода, кнопка «преобразовать», поле результата и копирование. "
            "Поддержи обратное преобразование. Ошибки разбора показывай понятным текстом."
        ),
    },
    {
        "key": "diagram",
        "name": "Диаграмма",
        "icon": "fa-diagram-project",
        "prompt": (
            "Это режим диаграммы. Найди в тексте связи между объектами (что от чего "
            "зависит, что на что ссылается, что за чем следует) и построй схему: "
            "блоки-узлы и стрелки между ними. Раскладку сделай автоматической, чтобы "
            "схема не наезжала сама на себя. Добавь подсветку узла по наведению."
        ),
    },
    {
        "key": "stats",
        "name": "Статистика",
        "icon": "fa-chart-simple",
        "has_template": True,
        "prompt": (
            "Это режим статистики. Найди в тексте числа и разбей их на категории "
            "(например, по названию или по периоду). Построй сводку: итог по каждой "
            "категории, общее количество, долю каждой категории в процентах. Покажи это "
            "полосами, сортируй по убыванию и дай фильтр по категории. Не выдумывай "
            "чисел, которых нет в тексте."
        ),
    },
    {
        "key": "compare",
        "name": "Сравнение",
        "icon": "fa-scale-balanced",
        "prompt": (
            "Это режим сравнения. Раздели текст заметки на две или несколько сторон "
            "(варианты, продукты, периоды, варианты решения) и собери таблицу "
            "«параметр × вариант»: строки — признаки, столбцы — варианты. Недостающие "
            "значения помечай прочерком. Подсвечивай лучшее значение в каждой строке и "
            "дай переключатель, чтобы сравнивать не всё сразу."
        ),
    },
    {
        "key": "kanban",
        "name": "Канбан",
        "icon": "fa-table-columns",
        "has_template": True,
        "prompt": (
            "Это режим канбан-доски. Разбей текст заметки на задачи и собери колонки "
            "«К выполнению», «В работе», «Готово». Каждая задача — карточка с "
            "заголовком и кратким описанием. Карточки можно перетаскивать между "
            "колонками (drag-and-drop) или двигать кнопками. Внизу каждой колонки — "
            "счётчик задач. Не выдумывай задач, которых нет в тексте."
        ),
    },
    {
        "key": "glossary",
        "name": "Глоссарий",
        "icon": "fa-book-open",
        "prompt": (
            "Это режим глоссария. Найди в тексте термины и их определения и собери "
            "алфавитный указатель: слева — меню первых букв, справа — карточки "
            "«термин — определение». Добавь поиск с подсветкой совпадений. Термины "
            "бери из текста, не выдумывай новых."
        ),
    },
    {
        "key": "faq",
        "name": "FAQ",
        "icon": "fa-circle-question",
        "prompt": (
            "Это режим FAQ. Найди в тексте вопросы и ответы (или сформулируй их "
            "сам по содержанию) и собери раскрывающиеся аккордеоны: клик по "
            "вопросу раскрывает ответ. Открытым может быть только один раздел. "
            "Добавь поиск по вопросам и стрелки-подсказки у закрытых блоков."
        ),
    },
    {
        "key": "schedule",
        "name": "Расписание",
        "icon": "fa-calendar-days",
        "prompt": (
            "Это режим расписания. Найди в тексте дела и время/даты и собери "
            "недельную сетку: дни недели по колонкам, время — по строкам, события "
            "— блоки в ячейках. Добавь переключатель «неделя/день» и подсветку "
            "текущего дня. Если времени нет — сгруппируй по дням или порядку."
        ),
    },
    {
        "key": "habits",
        "name": "Привычки",
        "icon": "fa-calendar-check",
        "prompt": (
            "Это режим трекера привычек. Разбей текст на привычки и собери "
            "таблицу-календарь: строки — привычки, колонки — дни месяца, в "
            "ячейках — отметки. Покажи серии (streak) подряд идущих дней и "
            "процент выполнения по каждой привычке. Состояние ячеек — "
            "интерактивные, клик переключает отметку в рамках страницы."
        ),
    },
    {
        "key": "catalog",
        "name": "Каталог",
        "icon": "fa-boxes-stacked",
        "prompt": (
            "Это режим каталога. Разбей текст на товары, книги, фильмы или другие "
            "позиции и собери карточки: название, ключевые характеристики, цена или "
            "оценка, если они есть. Добавь фильтры по характеристикам, сортировку "
            "и счётчик. Карточек не больше 100."
        ),
    },
    {
        "key": "budget",
        "name": "Бюджет",
        "icon": "fa-coins",
        "prompt": (
            "Это режим бюджета. Найди в тексте доходы и расходы и собери свод: "
            "сверху — три показателя (доходы, расходы, остаток) карточками, ниже — "
            "две колонки транзакций с суммой и категорией. Суммы считай точно, "
            "не округляй. Добавь подсветку отрицательного остатка."
        ),
    },
    {
        "key": "contacts",
        "name": "Контакты",
        "icon": "fa-address-book",
        "prompt": (
            "Это режим телефонной книги. Разбей текст на контакты: имя, телефон, "
            "почта, компания, заметка. Собери карточки с поиском, алфавитным "
            "указателем слева и крупной карточкой выбранного контакта справа. "
            "Сортируй по имени, группируй по первой букве."
        ),
    },
    {
        "key": "recipe",
        "name": "Рецепт",
        "icon": "fa-bowl-food",
        "prompt": (
            "Это режим рецептов. Найди в тексте блюда и собери карточки: название, "
            "время приготовления, ингредиенты количеством и пошаговый способ "
            "приготовления. Шаги пронумеруй и выведи вертикальной лентой. Добавь "
            "переключатель порций с пересчётом ингредиентов в рамках страницы, "
            "если в тексте есть количества."
        ),
    },
    {
        "key": "steps",
        "name": "Инструкция",
        "icon": "fa-list-ol",
        "prompt": (
            "Это режим пошаговой инструкции. Найди в тексте порядок действий и "
            "выстрой его вертикальным степпером: номер, заголовок, описание, "
            "кнопки «Назад/Далее». Текущий шаг подсвечен, пройденные — галочкой. "
            "Прогресс показан полосой сверху. Шаги бери из текста, не выдумывай."
        ),
    },
    {
        "key": "mindmap",
        "name": "Интеллект-карта",
        "icon": "fa-share-nodes",
        "prompt": (
            "Это режим интеллект-карты. Найди в тексте центральную тему и ветви "
            "(подтемы) и собери схему: узел в центре, ветви-карточки вокруг, "
            "связи линиями. Узлы разворачиваются по клику, если у них есть "
            "дочерние элементы. Раскладку сделай автоматической, без пересечений."
        ),
    },
    {
        "key": "poll",
        "name": "Опрос",
        "icon": "fa-square-poll-horizontal",
        "prompt": (
            "Это режим опроса. Найди в тексте вопросы и варианты ответов и собери "
            "опрос: карточка вопроса, кнопки-варианты, после выбора — полосы "
            "результатов в процентах. Голос можно сменить, итоги пересчитываются "
            "сразу. Если в тексте нет вариантов — предложи «Да/Нет/Воздержался»."
        ),
    },
]

# ──────────────────────────────  промпт  ──────────────────────────────

ARTIFACT_INSTRUCTION = """Ты создаёшь мини-приложение (микросервис) для заметки.
Пользователь описал, что это за режим. Возьми текст заметки и сделай рабочее
интерактивное приложение: с поиском, сортировкой или фильтрами — как следует
из описания режима.

ОТВЕТЬ ТОЛЬКО JSON, без пояснений и без обрамления в ```:
{"title": "короткое название", "type": "table|db|cards|timeline|checklist|calc|converter|diagram|kanban|glossary|faq|schedule|habits|catalog|budget|contacts|recipe|steps|mindmap|poll|custom", "html": "<!DOCTYPE html>..."}
Если собрать JSON сложно — верни просто полный HTML-документ без обёртки,
он тоже принимается.

ТРЕБОВАНИЯ К РАЗМЕТКЕ (все обязательны):

Дизайн:
- Современный внешний вид: карточки со скруглением 14px, мягкие тени,
  тонкие границы 1px, отступы кратные 8px, плавные hover-состояния и
  переходы 150-250ms, интерактивные элементы с focus-кольцом.
- Палитра через CSS-переменные: --bg, --fg, --muted, --line, --acc (акцент).
  Тёмная и светлая темы через prefers-color-scheme.
- Типографика: системный стек system-ui; заголовок 18-19px полужирный,
  основной текст 14-15px, подписи 12-13px приглушённые (var(--muted)).
- Интерфейс на русском; ничего не вылезает за край на ширине 1024px.

Технические ограничения:
- полный документ от <!DOCTYPE html> до закрывающего </html>;
- стили только в <style>, логика только в <script> внутри документа;
- никаких внешних ресурсов: ни <script src>, ни внешних CSS, шрифтов и
  картинок — иконки рисуй inline-SVG или эмодзи;
- никаких fetch, XMLHttpRequest, WebSocket, import(), обращений к внешним
  API и к top/parent/opener, ни localStorage, cookie, postMessage;
- данные закладывай массивом прямо в разметку, не больше 100 записей:
  длинный ответ обрывается, лишнее без жалости режь;
- в JS-строках не должно быть подстроки </script> — экранируй как <\\/script>;
- Картинки в заметке приходят ссылками вида ![описание](url): сами
  изображения показывать нельзя (внешние ресурсы запрещены), работай с их
  описанием как с текстовыми данными.

Перед ответом проверь себя: все теги закрыты, <script> и </script> поровну,
кавычки и скобки в JS сбалансированы, ответ заканчивается </html>.

Готовый пример, на который ориентируйся по качеству разметки:
<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><style>
:root{--bg:#fff;--fg:#1f2937;--muted:#6b7280;--line:#e5e7eb;--acc:#4f46e5;--card:#fff;--r:14px;--sh:0 1px 3px rgba(16,24,40,.08),0 1px 2px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#15171c;--fg:#e8eaed;--muted:#9aa3af;--line:#2b2f37;--acc:#818cf8;--card:#1b1e24;--sh:0 1px 3px rgba(0,0,0,.4)}}
*{box-sizing:border-box} body{margin:0;padding:20px;background:var(--bg);color:var(--fg);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
h1{font-size:19px;font-weight:700;margin:0 0 4px} .sub{color:var(--muted);font-size:13px;margin-bottom:14px}
.bar{display:flex;gap:8px;margin-bottom:14px} input,select{font:inherit;padding:9px 12px;
  border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg)}
input{flex:1} input:focus{outline:2px solid var(--acc);outline-offset:-1px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
  box-shadow:var(--sh);padding:14px 16px;transition:transform .18s ease,box-shadow .18s ease}
.card:hover{transform:translateY(-2px);box-shadow:0 6px 16px rgba(16,24,40,.12)}
.card h3{margin:0 0 4px;font-size:15px} .card p{margin:0;color:var(--muted);font-size:13px}
.tag{display:inline-block;font-size:11px;font-weight:600;color:var(--acc);
  background:color-mix(in srgb,var(--acc) 12%,transparent);border-radius:999px;
  padding:2px 9px;margin-bottom:8px}
.count{color:var(--muted);font-size:13px;margin-left:auto;align-self:center}
.empty{padding:32px;text-align:center;color:var(--muted);font-size:14px}
</style></head><body>
<h1>Название</h1><div class="sub">Короткое описание данных</div>
<div class="bar"><input id="q" placeholder="Поиск"><span class="count" id="c"></span></div>
<div class="grid" id="grid"></div><div class="empty" id="empty" style="display:none">Ничего не найдено</div>
<script>
const DATA=[["Тег","Значение","Описание"],["Тег2","Значение2","Описание2"]]; // данные из заметки
const grid=document.getElementById("grid"),q=document.getElementById("q");
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]))}
function draw(){
  const t=q.value.trim().toLowerCase();
  const f=DATA.filter(r=>r.join(" ").toLowerCase().includes(t));
  grid.innerHTML=f.map(r=>'<div class="card"><span class="tag">'+esc(r[0])+'</span><h3>'+esc(r[1])+'</h3><p>'+esc(r[2])+'</p></div>').join("");
  document.getElementById("c").textContent=f.length+" из "+DATA.length;
  document.getElementById("empty").style.display=f.length?"none":"";
}
q.oninput=draw;
draw();
</script></body></html>

Этот пример — карточки с поиском. Для другого типа приложения сохрани ту же
структуру (переменные цветов, тёмная тема, hover-состояния, никаких внешних
ресурсов), но сделай разметку под описание режима."""


# Промпт для случая, когда у режима уже есть готовый HTML-шаблон. Модели
# не нужно придумывать приложение заново — она меняет в нём только данные.
# Это и быстрее, и не ломает то, что уже работает: вёрстка, поиск,
# сортировка и темы приезжают из шаблона неизменными.
#
# Сборка через склейку, а не str.format: в шаблоне полно фигурных скобок
# (CSS и JS), и format() падал бы на первой же переменной вида `{...}`.
TEMPLATE_INSTRUCTION = """У режима уже есть готовое приложение. Ниже дан его HTML целиком.

Твоя единственная задача — вписать в него ДАННЫЕ ИЗ ТЕКСТА ЗАМЕТКИ.

Правила:
- верни ТОТ ЖЕ документ целиком, от <!DOCTYPE html> до </html>;
- не меняй разметку, стили, логику, названия переменных и функций;
- меняй только содержимое массива/объекта с данными (DATA, ROWS, ITEMS и
  подобные) и подписи, которые выводят эти данные;
- не добавляй новых блоков, кнопок, скриптов и внешних ресурсов;
- ОТВЕТЬ ТОЛЬКО полным HTML-документом, без пояснений, без ``` и без JSON;
- данных в тексте нет — оставь массив пустым, но не удаляй его;
- картинки в тексте даны ссылками вида ![описание](url): внешние
  изображения запрещены, вписывай их описания как текстовые данные.

Готовый шаблон приложения:
"""

TEMPLATE_FOOTER = """

Текст заметки (источник данных):
"""


# Режим «Только текст»: ИИ правит текст заметки и возвращает текст, а не
# приложение. Поэтому здесь нет ни JSON-обёртки, ни требований к html —
# иначе модель пыталась бы собрать документ вместо того, чтобы
# отформатировать заметку.
TEXT_INSTRUCTION = """Ты редактируешь ТЕКСТ ЗАМЕТКИ. Приложение создавать не нужно.

Выполни то, что описано ниже, и верни ТОЛЬКО получившийся текст.

Требования к ответу:
- только сам текст, без вступлений («Вот результат», «Готово»);
- без обрамления в ``` и без markdown-блоков кода вокруг ответа;
- без пояснений, что и почему ты изменил;
- сохрани язык оригинала и общий смысл;
- markdown-разметку (заголовки #, списки -, **жирный**) используй, только
  если она уместна по описанию режима;
- не добавляй от себя фактов, которых не было в тексте;
- картинки вида ![alt](url) перенеси без изменений, если они есть.

Описание, что сделать с текстом:
"""


# Правка готового приложения. Отличие от TEMPLATE_INSTRUCTION: там модель
# заполняет данные в известном шаблоне, здесь она меняет само приложение по
# просьбе пользователя — добавляет элементы, переименовывает, правит
# оформление. Поэтому текст заметки здесь не нужен: материал — текущий HTML.
EDIT_INSTRUCTION = """Перед тобой ГОТОВОЕ HTML-приложение. Ты должен ИЗМЕНИТЬ его по просьбе пользователя.

Правила:
- верни ТОТ ЖЕ документ целиком, от <!DOCTYPE html> до </html>;
- измени то, что просит пользователь, и не трогай остальное;
- сохрани работоспособность: если меняешь разметку — поправь и скрипт,
  если меняешь данные — сохрани формат массива;
- не меняй структуру документа, способы подключения стилей и скриптов;
- стили только в <style>, без внешних CSS, шрифтов и картинок;
- вся логика только в <script> внутри документа, без внешних скриптов;
- никаких fetch, XMLHttpRequest, WebSocket, обращений к внешним API и к
  top/parent, никаких <script src>, localStorage и postMessage;
- интерфейс на русском, тёмная и светлая тема через prefers-color-scheme;
- ОТВЕТЬ ТОЛЬКО полным HTML-документом, без пояснений, без ``` и без JSON.

Просьба пользователя:
"""

# Второй проход сборки: черновик оборвался или не собрался — просим модель
# дописать его до полного рабочего документа. Одна попытка, без цикла:
# если и после неё разметка битая, честно возвращаем ошибку с черновиком.
REPAIR_INSTRUCTION = """Ты дописываешь ИНТЕРАКТИВНОЕ ПРИЛОЖЕНИЕ до полного рабочего документа.

Черновик приложения оборвался или не собрался. Твоя задача — завершить его:
- верни полный документ от <!DOCTYPE html> до закрывающего </html>;
- сохрани стили, логику и данные черновика, добавь только недостающее;
- закрой все теги, заверши скрипты, вызови отрисовку при загрузке;
- не оставляй комментарии-заглушки вместо кода, не обрезай данные без нужды.

Правила те же: стили только в <style>, логика только в <script> внутри
документа, без внешних ресурсов, fetch, localStorage и обращений к
top/parent, интерфейс на русском, тёмная и светлая тема через
prefers-color-scheme. Картинки в данных — ссылки вида ![описание](url):
внешние изображения запрещены, работай с их описанием.

ОТВЕТЬ ТОЛЬКО полным HTML-документом, без пояснений и без ```.

Черновик приложения (допиши его):
"""


def build_text_prompt(mode_name, mode_prompt, source_text):
    """Промпт режима «Только текст»: ИИ правит текст, приложения нет."""
    mode_prompt = (mode_prompt or "").strip()[:MAX_PROMPT_CHARS] or (
        "Приведи текст в порядок, не меняя смысла."
    )
    source_text = (source_text or "").strip()[:MAX_SOURCE_CHARS]
    return (
        "Название режима: %s.\n\n" % (mode_name or "Custom")
        + TEXT_INSTRUCTION + mode_prompt + "\n\n"
        + "ТЕКСТ ЗАМЕТКИ:\n" + source_text
    )


def build_edit_prompt(edit_request, current_html):
    """Промпт правки готового приложения по просьбе пользователя."""
    request = (edit_request or "").strip()[:MAX_PROMPT_CHARS] or (
        "Улучши приложение, не меняя его назначение."
    )
    html = (current_html or "").strip()
    return EDIT_INSTRUCTION + request + "\n\nТЕКУЩИЙ HTML ПРИЛОЖЕНИЯ:\n" + html


def strip_answer_wrapper(raw):
    """Убрать вокруг ответа модели то, что не является самим результатом.

    Модель почти всегда добавляет вступление («Вот отформатированный
    текст:») или обрамляет ответ в ```. В текстовом режиме это особенно
    заметно: блок ``` попадёт в заметку как код, а вступление — как мусор.
    Поэтому и то и другое срезается, а если осталось одно вступление —
    берём текст после первого перевода строки.
    """
    if not raw:
        return ""
    text = str(raw).strip()

    # ```lang ... ``` или ``` ... ```
    fence = re.match(r"^```[a-zA-Z0-9_-]*\s*\n([\s\S]*?)\n?```\s*$", text)
    if fence:
        return fence.group(1).strip()

    # Незакрытый блок: модель начала с ``` и не закрыла.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        return text.strip()

    # Вступление одной строкой: «Вот результат:\n<текст>». Убираем, только
    # если строка похожа на вступление (кончается двоеточием) — иначе можно
    # срезать первую строку самого текста.
    lines = text.split("\n")
    if len(lines) > 1:
        first = lines[0].strip()
        if first.endswith(":") and len(first) < 120:
            return "\n".join(lines[1:]).strip()

    return text


def build_prompt(mode_name, mode_prompt, source_text, template_html=""):
    """Полный промпт для ИИ: описание режима + текст заметки.

    Если у режима есть готовый HTML-шаблон, задача сужается до «впиши
    данные»: приложение уже собрано и проверено, и каждый новый запуск
    не рискует получить другую вёрстку.
    """
    mode_prompt = (mode_prompt or "").strip()[:MAX_PROMPT_CHARS] or (
        "Сделай из текста заметки структурированное интерактивное приложение."
    )
    source_text = (source_text or "").strip()[:MAX_SOURCE_CHARS]

    tpl = (template_html or "").strip()
    if tpl:
        return (
            "Название режима: %s.\n\n"
            "ОПИСАНИЕ РЕЖИМА:\n%s\n\n"
            % (mode_name or "Custom", mode_prompt)
            + TEMPLATE_INSTRUCTION + tpl
            + TEMPLATE_FOOTER + source_text
        )

    return (
        f"Название режима: {mode_name or 'Custom'}.\n\n"
        f"ОПИСАНИЕ РЕЖИМА (что нужно сделать):\n{mode_prompt}\n\n"
        f"ТЕКСТ ЗАМЕТКИ (материал):\n{source_text}\n\n"
        f"{ARTIFACT_INSTRUCTION}"
    )


# ──────────────────────  разбор ответа и чистка  ──────────────────────

_FENCE_RE = re.compile(r"```(?:json|html|javascript|js)?\s*([\s\S]*?)```", re.IGNORECASE)


def parse_artifact(raw):
    """Достать {title, type, html} из ответа модели.

    Модель почти всегда оборачивает JSON в ```-блок или добавляет текст
    вокруг, поэтому сначала пробуем fenced-блок, потом первый сбалансированный
    объект. Если JSON не вышло (у небольших моделей это обычное дело —
    длинный пример в промпте съедает их внимание), берём сам документ из
    ответа: приложение пригодно и без обёртки JSON.

    Возвращает (dict | None, текст ошибки).
    """
    if not raw:
        return None, "Пустой ответ модели"
    text = str(raw).strip()

    candidates = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(text)

    for cand in candidates:
        obj = _first_json_object(cand)
        if isinstance(obj, dict):
            html = obj.get("html") or obj.get("code") or obj.get("body") or ""
            if isinstance(html, str) and "<" in html:
                return {
                    "title": str(obj.get("title") or _title_guess(html))[:120],
                    "type": str(obj.get("type") or "custom")[:32],
                    "html": html,
                }, ""

    # Запасной путь: в ответе есть готовый документ, но JSON вокруг него
    # не выдержал. Само приложение при этом никуда не делось.
    doc = _extract_html_document(text)
    if doc:
        return {"title": _title_guess(doc)[:120], "type": "custom", "html": doc}, ""

    return None, "Не удалось разобрать ответ модели как JSON с полем html"


def _extract_html_document(text):
    """Вытащить HTML-документ из ответа, где JSON не распарсился."""
    low = text.lower()
    start = low.find("<!doctype html")
    if start == -1:
        start = low.find("<html")
    if start == -1:
        return ""
    end = low.rfind("</html>")
    if end == -1:
        end = len(text) - 1
    else:
        end += len("</html>")
    doc = text[start:end]
    # Обрезаем хвост, если после документа модель дописала JSON-обёртку.
    fence = doc.find("```")
    if fence != -1:
        doc = doc[:fence]
    return doc.strip()


def _title_guess(html):
    """Заголовок из первого <h1>/<title>, если модель не назвала приложение."""
    m = re.search(r"<h1[^>]*>([^<]{1,80})</h1>", html, re.IGNORECASE)
    if not m:
        m = re.search(r"<title[^>]*>([^<]{1,80})</title>", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return "Приложение"


# Ответы, которыми сервер сообщает о недоступности движка. Их нельзя
# скармливать разбору JSON: получилось бы «не удалось разобрать ответ»,
# хотя настоящая причина — выключенный или упавший llama.cpp.
_UNAVAILABLE_MARKERS = (
    "winerror 10061", "winerror 10060", "connection refused",
    "подключение не установлено", "connectionerror", "connection aborted",
    "недоступен", "unreachable", "timed out", "таймаут",
)


def looks_unavailable(raw):
    """Похоже ли, что модель ответила ошибкой, а не результатом."""
    low = str(raw or "").lower()
    if not low:
        return False
    if len(low) > 400:
        return False
    if "<html" in low or "<!doctype" in low:
        return False
    return any(marker in low for marker in _UNAVAILABLE_MARKERS)


def looks_truncated(raw):
    """Оборвался ли ответ модели на полуслове.

    Обрезанный документ опаснее отсутствия документа: разметка вроде бы
    на месте, а <script> обрывается посреди массива и приложение падает с
    молчаливой ошибкой в консоли. Ловим это до сохранения.
    """
    text = str(raw or "")
    if not text.strip():
        return True
    low = text.lower()
    if "<!doctype html" in low or "<html" in low:
        return "</html>" not in low          # документ не закрыт
    # JSON-обёртка: считаем баланс, игнорируя строки.
    return _brace_balance(text) > 0


def _brace_balance(text):
    """Сколько фигурных скобок не закрыто (строки и кавычки не считаем)."""
    depth = 0
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    return depth


def _first_json_object(text):
    """Первый сбалансированный { ... } в строке, с учётом строк и кавычек."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        swallow.note('custom_mode_module._first_json_object:L750')
                        break
        start = text.find("{", start + 1)
    return None


# Запрещённые конструкции. Проверяются до сохранения на диск.
_NETWORK_PATTERNS = [
    (r"\bfetch\s*\(", "fetch()"),
    (r"\bXMLHttpRequest\b", "XMLHttpRequest"),
    (r"\bWebSocket\b", "WebSocket"),
    (r"\bEventSource\b", "EventSource"),
    (r"\bnavigator\s*\.\s*sendBeacon\b", "sendBeacon"),
    (r"\bimport\s*\(", "динамический import()"),
    (r"\bnew\s+Worker\b", "Web Worker"),
    (r"\bnew\s+SharedWorker\b", "SharedWorker"),
    (r"\bdocument\s*\.\s*cookie\b", "document.cookie"),
    (r"\blocalStorage\b", "localStorage"),
    (r"\bsessionStorage\b", "sessionStorage"),
    (r"\bindexedDB\b", "indexedDB"),
    (r"\btop\s*\.\s*location\b", "top.location"),
    (r"\bparent\s*\.\s*postMessage\b", "parent.postMessage"),
    (r"\bwindow\s*\.\s*opener\b", "window.opener"),
    (r"\bwindow\s*\.\s*top\b", "window.top"),
    (r"\bpostMessage\s*\(", "postMessage()"),
    (r"\bsrcdoc\b", "srcdoc"),
]
_REMOTE_SRC_RE = re.compile(
    r"""<\s*(script|iframe|link|img|embed|object|source)\b[^>]*?"""
    r"""\b(src|href|data)\s*=\s*["']?\s*(?:https?:|//|javascript:)""",
    re.IGNORECASE,
)
_REMOTE_IMPORT_RE = re.compile(
    r"""<\s*script\b[^>]*\bsrc\s*=\s*["'][^"']+["']""", re.IGNORECASE)


def sanitize_html(html):
    """Почистить сгенерированную разметку. Возвращает (html, список запретов)."""
    if not isinstance(html, str):
        return "", ["html не строка"]
    out = html
    blocked = []

    # 1. Внешние ресурсы и javascript:-ссылки.
    for m in list(_REMOTE_IMPORT_RE.finditer(out)):
        blocked.append("внешний <script src>")
    out = _REMOTE_IMPORT_RE.sub("<script>/* внешний скрипт удалён */", out)
    for m in list(_REMOTE_SRC_RE.finditer(out)):
        blocked.append("внешний ресурс (%s)" % m.group(1).lower())
    out = _REMOTE_SRC_RE.sub(r'<\1 data-blocked-src="1"', out)

    # 2. Обращения к сети и к родительскому окну — режем сам вызов.
    #    Замена намеренно НЕ содержит исходного имени: иначе строка
    #    «fetch(...)» снова окажется в тексте и при беглой проверке
    #    покажется, что запрет не сработал.
    for pattern, label in _NETWORK_PATTERNS:
        rx = re.compile(pattern)
        if rx.search(out):
            blocked.append(label)
            out = rx.sub("/*заблокировано*/", out)

    # 3. Стили не должны уводить страницу наружу.
    out = re.sub(r"@import\s+[^;]+;?", "", out, flags=re.IGNORECASE)

    out = out.strip()
    if len(out.encode("utf-8", errors="ignore")) > MAX_ARTIFACT_BYTES:
        out = out.encode("utf-8", errors="ignore")[:MAX_ARTIFACT_BYTES].decode(
            "utf-8", errors="ignore"
        )
        blocked.append("обрезано по размеру")
    return out, blocked


# ──────────────────────────────  хранилище  ──────────────────────────────

def artifacts_root(notes_dir):
    return os.path.join(notes_dir, ARTIFACT_DIRNAME)


def artifact_dir(notes_dir, note_id):
    """Каталог артефактов заметки. Имя — только безопасные символы."""
    safe = _slug(note_id or "note")
    return os.path.join(artifacts_root(notes_dir), safe)


def _slug(text):
    """Имя файла из произвольного заголовка.

    Ограничение раньше было [a-z0-9], и кириллица попадала в него целиком
    мимо: любое русское название давало пустую строку, а следующий за ним
    fallback «item» означал, что второе приложение затирало первое. Поэтому
    оставляем буквы и цифры любого алфавита — Windows их нормально
    принимает, — а латиницу дополнительно схлопываем в транслит, чтобы
    имена оставались читаемыми. Если символов не нашлось, берём хэш:
    он уникален и не даёт коллизий.
    """
    raw = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    if not raw:
        raw = "art"
    # Транслит только для читаемости латинских имён; кириллица остаётся.
    table = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    out = []
    for ch in raw:
        out.append(table.get(ch, ch))
    s = "".join(out)
    s = re.sub(r"[^0-9a-zA-ZЀ-ӿ]+", "-", s).strip("-").lower()
    # Повторные дефисы и ограничение длины — иначе в Windows ломается путь.
    s = re.sub(r"-{2,}", "-", s)[:60].strip("-")
    if not s:
        s = "art-" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
    return s


def save_artifact(notes_dir, note_id, slug, title, html):
    """Записать артефакт на диск. Возвращает относительный маркер [CUSTOM]..."""
    slug = _slug(slug) or "artifact"
    directory = artifact_dir(notes_dir, note_id)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, slug + ".html")
    doc = html
    if "<!DOCTYPE" not in doc[:200].upper():
        doc = ("<!DOCTYPE html>\n<html lang=\"ru\"><head><meta charset=\"utf-8\">"
               "</head><body>\n" + doc + "\n</body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    meta = {
        "title": title or slug,
        "slug": slug,
        "note_id": str(note_id or ""),
        "updated": time.time(),
    }
    with open(os.path.join(directory, slug + ".json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    return slug


def read_artifact(notes_dir, note_id, slug):
    """Прочитать HTML артефакта. Возвращает строку или None."""
    slug = _slug(slug)
    path = os.path.join(artifact_dir(notes_dir, note_id), slug + ".html")
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        swallow.note('custom_mode_module.read_artifact:L902')
        return None


def list_artifacts(notes_dir, note_id):
    directory = artifact_dir(notes_dir, note_id)
    out = []
    try:
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                swallow.note('custom_mode_module.list_artifacts:L917')
                continue
    except OSError:
        swallow.note('custom_mode_module.list_artifacts:L920')
        return []
    return out


# Маркер в тексте заметки: [CUSTOM]my-table
MARKER_RE = re.compile(r"\[CUSTOM\]([A-Za-z0-9_\-]+)")


def marker(slug):
    return "[CUSTOM]%s" % _slug(slug)


def find_markers(text):
    """Все микросервисы, упомянутые в тексте заметки."""
    return list(dict.fromkeys(MARKER_RE.findall(text or "")))