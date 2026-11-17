"""Маршруты ключевого перевода (модуль `i18n`).

Переводы приходят на страницу двумя дорогами. Токены в HTML подставляет слой
`app/i18n.Layer`, поэтому они всегда в самом ответе. А вызовы `__t('ключ',
'Оригинал')` живут в скриптах и рисуют текст уже в браузере: для них bootstrap
везёт ключи видимой страницы, а этот маршрут добирает то, что появилось после
дорисовки (строки списка, модальные окна, сообщения о статусе).

Язык берётся из профиля запроса, а не из параметра URL: чужой язык профилем не
выпрашивается, и карта не превращается в открытый переводчик по query-строке.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import i18n

router = APIRouter()


def _profile_pid(request):
    try:
        import profiles_module as PM
        return PM.request_profile(request)
    except Exception:
        return None


@router.get("/api/i18n/catalog")
async def api_i18n_catalog(request: Request):
    pid = _profile_pid(request)
    lang = i18n.profile_lang(pid)
    if not lang:
        # Выключенный модуль или русский профиль: карты нет, и страница живёт на
        # оригиналах — ровно как до включения рубильника.
        return JSONResponse({"lang": "ru", "d": {}}, status_code=200,
                            headers={"Cache-Control": "no-cache"})
    return JSONResponse({"lang": lang, "d": i18n.catalog(lang)},
                        headers={"Cache-Control": "no-cache"})
