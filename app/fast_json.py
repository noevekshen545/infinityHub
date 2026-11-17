"""JSON-ответы через orjson, когда он есть в сборке, иначе штатный JSONResponse.

OPT_NON_STR_KEYS сохраняет поведение json.dumps: словари с числовыми ключами
сериализуются, а не роняют ответ. Отключить: INFINITY_ORJSON=0.
"""

import os

from fastapi.responses import JSONResponse


def _enabled() -> bool:
    return os.environ.get("INFINITY_ORJSON", "1").strip().lower() not in (
        "0", "false", "no", "off")


try:
    if not _enabled():
        raise ImportError
    import orjson

    class FastJSONResponse(JSONResponse):
        def render(self, content) -> bytes:
            return orjson.dumps(content, option=orjson.OPT_NON_STR_KEYS)
except ImportError:
    FastJSONResponse = JSONResponse
