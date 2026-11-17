"""Мемо отрисовки /api/extensions/builder-data.

Проверяет ровно то, ради чего мемо нужен: байты ответа совпадают с тем, что
отдавал прежний путь (фильтр по профилю + сериализация), а сброс происходит по
записи в источник, то есть свежесть не уезжает дальше прежнего TTL.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.encoders import jsonable_encoder  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

import routes.extension_builder as eb  # noqa: E402


def _source():
    """Минимальный набор как в реальном ответе: маршруты, хуки, кортежи вместо списков."""
    return {
        "modules": ["notes", "books", "agent"],
        "module_info": {"notes": "Заметки и знания"},
        "hooks_by_module": {"notes": [{"id": "on_save", "description": "После сохранения"}]},
        "permissions": [{"id": "fs.read", "description": "Чтение файлов"}],
        "icons": ("fa-book", "fa-sticky-note"),
        "api_routes": [
            {"path": "/api/notes/list", "methods": ["GET"]},
            {"path": "/api/admin/thing", "methods": ["POST"]},
        ],
        "module_prefixes": {"notes": ["/api/notes"]},
        "css_reference": {},
        "api_load_error": None,
        "api_source": "openapi",
    }


def _baseline_bytes(data, is_admin):
    """Как отдавал обработчик до мемо: jsonable_encoder + тот же dumps, что у Starlette."""
    return JSONResponse(jsonable_encoder(eb._filter_builder_data(data, is_admin))).body


def test_memo_otdaet_te_zhe_bayty_dlya_oboyh_profilev():
    eb._BUILDER_JSON_CACHE.clear()
    eb.BUILDER_DATA_CACHE_TS = 111.0
    for is_admin in (True, False):
        got = eb._builder_render_json(_source(), is_admin)
        assert got.body == _baseline_bytes(_source(), is_admin)
        assert got.media_type == "application/json"


def test_povtornyy_vyzov_ne_pereserializuet():
    eb._BUILDER_JSON_CACHE.clear()
    eb.BUILDER_DATA_CACHE_TS = 222.0
    src = _source()
    first = eb._builder_render_json(src, True).body
    second = eb._builder_render_json(src, True)
    assert second.body == first
    assert len(eb._BUILDER_JSON_CACHE) == 1


def test_zapisi_v_istochnike_sbrasyvayut_otrisovku():
    eb._BUILDER_JSON_CACHE.clear()
    eb.BUILDER_DATA_CACHE_TS = 333.0
    src = _source()
    old = eb._builder_render_json(src, True).body
    src["modules"].append("docker")
    eb.BUILDER_DATA_CACHE_TS = 444.0
    new = eb._builder_render_json(src, True).body
    assert new != old
    assert new == _baseline_bytes(src, True)
    assert len(eb._BUILDER_JSON_CACHE) == 1


def test_kluchi_admina_i_prostogo_profilya_ne_pereputyivayutsya():
    eb._BUILDER_JSON_CACHE.clear()
    eb.BUILDER_DATA_CACHE_TS = 555.0
    src = _source()
    admin = eb._builder_render_json(src, True).body
    plain = eb._builder_render_json(src, False).body
    assert admin != plain
    assert sorted(eb._BUILDER_JSON_CACHE) == [(555.0, False), (555.0, True)]


def test_gzip_otdaetsya_gotovym_i_sovpadaet_s_identity():
    """Клиент с Accept-Encoding: gzip получает то же тело, сжатое один раз.

    Раньше сжатием занимался внешний GZipMiddleware на каждый запрос — 180 мс
    даже при попадании в мемо. Теперь готовые байты лежат рядом и берутся
    повторно; распакованный ответ обязан совпасть с identity-телом.
    """
    import gzip

    eb._BUILDER_JSON_CACHE.clear()
    eb._BUILDER_GZIP_CACHE.clear()
    eb.BUILDER_DATA_CACHE_TS = 666.0
    src = _source()

    plain = eb._builder_render_json(src, True)
    packed = eb._builder_render_json(src, True, "gzip, deflate, br")
    assert packed.headers["content-encoding"] == "gzip"
    assert packed.headers["vary"] == "Accept-Encoding"
    assert gzip.decompress(packed.body) == plain.body
    # второй запрос берёт готовые байты, а не сжимает заново
    again = eb._builder_render_json(src, True, "gzip")
    assert again.body is packed.body
    # клиент без gzip получает несжатое тело, и оно остаётся тем же
    assert eb._builder_render_json(src, True, "").body == plain.body
