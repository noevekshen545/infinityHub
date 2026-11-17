"""Мемо сопоставления пути с маршрутами FastAPI.

Почему это нужно: `APIRouter.app` (fastapi/routing.py:2766) перебирает
`self.routes` линейно и каждому вызывает `route.matches(scope)`. В верхнем
списке этого приложения 468 маршрутов, зарегистрированных напрямую (`@app.get`),
и 37 `_IncludedRouter` от `include_router`. Вызов `matches` у `_IncludedRouter`
(:1801) прогоняет ПОЛНЫЙ поиск по внутренним маршрутам своего роутера и при этом
возвращает пустой child_scope, поэтому один проход по верхнему списку стоит
примерно столько же, сколько обход всей таблицы приложения. Найденный
`_IncludedRouter` затем в `handle` → `_handle_selected` (:1822) matching'ится
ещё раз — двойная работа.

Замеры:
  - профиль живого процесса под нагрузкой 222 req/с (py-spy --nonblocking, #33):
    1749 из 2308 сэмплов event loop (75.8 %) внутри этого цикла, ~3.4 мс CPU на
    запрос;
  - стена, один keep-alive клиент, простой сервер: /api/health 5.1 мс p50,
    заведомо несуществующий путь 30.9 мс p50 (полный проход + redirect_slashes
    + low-priority);
  - in-process, таблица из 1383 путей (без include_router): холодный проход
    среднее 1020 мкс, попадание в мемо 29 мкс.

Что делает мемо: помнит результат верхнего прохода по
(метод, путь, root_path) и на попадании повторяет ровно те действия, которые
сделал бы оригинальный код после найденного совпадения, — `scope.update` тем же
child_scope, `_route_selected` только когда включён телеметрический scope (иначе
это no-op, проверяется в telemetry/_api.py:243), затем `route.handle`. Ни порядок
перебора, ни «первый полный матч выигрывает», ни PARTIAL для 405 не меняются.

Отрицательный результат запоминается, но не отдаётся самостоятельно: 307
(redirect_slashes), low-priority маршруты и 404 порождает оригинальный код, мемо
лишь говорит «мой проход на этом пути уже был». Цена — один лишний проход на
самом первом запросе к новому пути (второй проход делает оригинальный код);
повторные запросы к тому же пути платят только оригинальный код, как и раньше.

Инвалидация: подпись таблицы = глобальный счётчик правок (его растит обёртка
`APIRouter._mark_routes_changed` — воронка, через которую проходят все способы
добавить маршрут, включая вложенные роутеры) плюс `(len(routes), id(routes[-1]))`
на случай прямых манипуляций `router.routes` в обход счётчика. Проверка подписи
стоит ~0.1 мкс; `_get_routes_version()` для этого обошёл бы всю таблицу
(замерено: 33 мкс на 465 маршрутах). Правка любого маршрута чистит мемо целиком —
дорого, но такое происходит только при регистрации, а не под нагрузкой.

Проверяется тестом tools/route_memo_parity.py — сравнение статуса, заголовков и
тела каждого ответа с оригинальным `APIRouter.app` на реальном FastAPI-приложении
с include_router, параметрическими путями, mount'ами, 405 и websocket.
"""
from collections import OrderedDict

from starlette.routing import Match, Mount
import swallow

try:
    from fastapi.routing import APIRouter
    from fastapi.telemetry._api import _route_selected
except ImportError:  # приложение без FastAPI: мемо просто не устанавливается
    swallow.note('route_memo.<module>:L56')
    APIRouter = None
    _route_selected = None

_ORIG_APP = None
_installed = False

# Счётчик правок таблицы маршрутов (см. _table_sig). Список, а не int, чтобы
# обёртка могла менять его без global-объявления в чужом модуле.
_GEN = [0]

# Sentinel: «этот путь уже проверяли, совпадений нет». None отличается от
# отсутствия ключа, чтобы не путать «не помню» с «помню, что нет».
_NO_MATCH = object()

# Потолок таблицы мемо: ключ — путь, а параметрические пути (/api/item/123)
# могут быть почти все разные. 20 000 записей ≈ несколько мегабайт.
_MAX_ENTRIES = 20000


def _remember(memo, key, value):
    # Вытесняем по LRU, а не очисткой всей таблицы: при длинной сессии с новыми
    # параметрическими путями полная очистка превратила бы мемо в чистый overhead
    # (каждый запрос платил бы и мой проход, и оригинальный). 10 % пачкой, чтобы
    # не вызывать popitem на каждой вставке.
    memo[key] = value
    if len(memo) > _MAX_ENTRIES:
        for _ in range(_MAX_ENTRIES // 10 + 1):
            memo.popitem(last=False)


def _copy_child(child_scope):
    """Свежая копия child_scope на каждый запрос.

    В child_scope лежат dict'ы (path_params у Route/Mount), которые обработчик
    может дополнить; отдать один и тот же объект двум запросам нельзя. Остальные
    значения (строки, классы, сами маршруты) неизменяемы — копируем неглубоко.
    """
    out = {}
    for k, v in child_scope.items():
        out[k] = dict(v) if isinstance(v, dict) else v
    return out


def _table_sig(self):
    """Подпись таблицы маршрутов; меняется при любой правке таблицы.

    `_GEN` растёт в обёртке `_mark_routes_changed` — единственной воронке, через
    которую проходят add_api_route/add_route/add_websocket_route/include_router/
    mount/frontend-маршруты любого роутера (включая вложенные, куда прямые
    вызовы доходят по классу). Поэтому проверка стоит ~0.1 мкс вместо
    `_get_routes_version()`, который для этого обходит все 1420 элементов.
    `(len, id(last))` догоняют прямые манипуляции `router.routes` в обход
    счётчика, удаление маршрута — длину списка.
    """
    routes = self.routes
    return (_GEN[0], len(routes), id(routes[-1]) if routes else 0)


def _make_memo_app(orig_app):
    async def memo_app(self, scope, receive, send):
        if scope["type"] != "http":
            # websocket и lifespan — целиком оригинальному коду
            await orig_app(self, scope, receive, send)
            return
        if "router" not in scope:
            scope["router"] = self

        memo = getattr(self, "_route_memo", None)
        sig = _table_sig(self)
        if memo is None or self._route_memo_sig != sig:
            memo = OrderedDict()
            self._route_memo = memo
            self._route_memo_sig = sig

        key = (scope.get("method"), scope.get("path"), scope.get("root_path", ""))
        hit = memo.get(key, _NO_MATCH)
        if hit is None:
            # Ранее этот путь ни с чем не совпал: 307/404 по-прежнему делает
            # оригинальный код, моего прохода не будет.
            await orig_app(self, scope, receive, send)
            return
        if hit is not _NO_MATCH:
            memo.move_to_end(key)
            route, child = hit
            scope.update(_copy_child(child))
            if scope.get("fastapi.telemetry") is not None:
                _route_selected(
                    scope=scope,
                    path=getattr(route, "path_format", None),
                    mount=isinstance(route, Mount),
                )
            await route.handle(scope, receive, send)
            return

        partial = None
        partial_child = None
        for route in self.routes:
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                _remember(memo, key, (route, _copy_child(child_scope)))
                scope.update(child_scope)
                _route_selected(
                    scope=scope,
                    path=getattr(route, "path_format", None),
                    mount=isinstance(route, Mount),
                )
                await route.handle(scope, receive, send)
                return
            if match == Match.PARTIAL and partial is None:
                partial = route
                partial_child = child_scope

        if partial is not None:
            _remember(memo, key, (partial, _copy_child(partial_child)))
            scope.update(partial_child)
            _route_selected(
                scope=scope,
                path=getattr(partial, "path_format", None),
                mount=isinstance(partial, Mount),
            )
            await partial.handle(scope, receive, send)
            return

        # Совпадений нет: redirect_slashes, low-priority и 404 — оригинальный код.
        _remember(memo, key, None)
        await orig_app(self, scope, receive, send)

    return memo_app


def install():
    """Подменить `APIRouter.app`. Вызывать ДО создания FastAPI-приложения.

    `Router.__init__` (starlette/routing.py:615) сохраняет self.middleware_stack
    = self.app, то есть связанный метод, и `Router.__call__` вызывает именно его.
    Поэтому подмена класса после создания приложения до вызова бы не дошла —
    экземпляры, созданные до install(), продолжают работать по оригинальному
    коду (медленнее, но корректно).

    Прежняя версия подменяла `starlette.routing.Router.app`, которую FastAPI
    переопределяет своим методом — мемо не вызывался никогда (0 из 2196 сэмплов
    профиля).
    """
    global _installed, _ORIG_APP
    if _installed or APIRouter is None:
        return
    _ORIG_APP = APIRouter.app
    APIRouter.app = _make_memo_app(_ORIG_APP)
    orig_mark = APIRouter._mark_routes_changed

    def _marked(self):
        _GEN[0] += 1
        return orig_mark(self)

    APIRouter._mark_routes_changed = _marked
    _installed = True
