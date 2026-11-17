"""Видимость медиа по профилям: общедоступная библиотека + своё приватное.

Модель (решение пользователя 02.11): каждый профиль видит библиотеку
общедоступного профиля и свои private_*-каталоги; чужое приватное не видит
никто, кроме владельца. `profiles_module.get_effective_config` мержит ровно
так (шаг 4: база = media.* общедоступного, поверх — private_*), поэтому здесь
не появляется second source of truth — только чтение списков, префиксы путей и
объединение по всем профилям для prune.

Зачем отдельный модуль: три места (выдача, prune, маршруты) должны считать
видимость одинаково. Расхождение здесь и есть сегодняшняя дыра: выдача делала
`SELECT * FROM music_tracks` без фильтра, а prune удалял всё вне списков
СКВАНИРОВАВШЕГО профиля.
"""

import os
import time
import swallow

# Категория -> ключ в config["media"] и приватный ключ профиля
CATEGORIES = {
    "music": ("music_dirs", "private_music_dirs"),
    "video": ("video_dirs", "private_video_dirs"),
    "photos": ("photo_dirs", "private_photos_dirs"),
}

# Флаг включён по умолчанию (решение пользователя): поведение меняется с
# перезапуском. Выключается одной строкой media.profile_filter=false в конфиге
# профиля или общедоступного — точечный рубильник, а не удаление кода.
FILTER_DEFAULT = True

_CACHE_TTL = 5.0
_cache = {}


def norm(path):
    """Путь к сравнению: слеши, без хвостового разделителя, регистр диска."""
    p = str(path or "").replace("\\", "/").rstrip("/")
    return p.lower()


def _effective(profile_id=None):
    key = ("cfg", profile_id or "")
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    import profiles_module as PM
    try:
        cfg = PM.get_effective_config(profile_id) or {}
    except Exception:
        swallow.note('media_visibility._effective:L51')
        cfg = {}
    _cache[key] = (now, cfg)
    return cfg


def _raw_profile_config(profile_id):
    import profiles_module as PM
    try:
        return PM.get_profile_config(profile_id) or {}
    except Exception:
        swallow.note('media_visibility._raw_profile_config:L62')
        return {}


def _resolve(path):
    """Тот же резолв, что у индексатора: сначала токены, затем normalize.

    Профильные конфиги хранят пути переносимо (`${ROOT}/...`), а индексатор
    пишет в БД абсолютные, поэтому без разворота токена префикс не совпал бы
    ни с одной строкой (замер: `…/InfinitySearch/${ROOT}/data/home/torrents`).
    """
    import media_module as MM
    s = str(path or "")
    if "${ROOT}" in s or "${APP}" in s:
        try:
            import config_module as CM
            fn = getattr(CM, "_expand_path", None)
            if callable(fn):
                s = fn(s)
            else:
                import profiles_module as PM
                s = PM._expand_token(s)
        except Exception:
            swallow.note('media_visibility._resolve:L85')
            pass
    try:
        return MM.resolve_media_path(s)
    except Exception:
        swallow.note('media_visibility._resolve:L90')
        return os.path.abspath(s)


def filter_enabled(profile_id=None) -> bool:
    media = _effective(profile_id).get("media") or {}
    val = media.get("profile_filter", FILTER_DEFAULT)
    if isinstance(val, str):
        return val.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(val)


def visible_dirs(category, profile_id=None):
    """Каталоги, которые видит этот профиль: общедоступная база + своё.

    При выключенном флаге возвращаем пустой список — вызывающий обязан
    вести себя как раньше (без фильтра), а не «ничего не видно».
    """
    media_key, _priv_key = CATEGORIES[category]
    media = _effective(profile_id).get("media") or {}
    dirs = media.get(media_key) or []
    out = []
    for d in dirs:
        if not str(d or "").strip():
            continue
        out.append(_resolve(d))
    return out


def all_indexable_dirs(category):
    """Объединение списков ВСЕХ профилей — для prune.

    Ключ к тому, почему prune больше не стирает чужие строки: «неактивная
    директория» — та, которой нет ни у одного профиля, а не та, которой нет у
    сканировавшего.
    """
    media_key, priv_key = CATEGORIES[category]
    import profiles_module as PM
    try:
        profiles = PM.list_profiles() or []
    except Exception:
        swallow.note('media_visibility.all_indexable_dirs:L131')
        profiles = []
    union = []
    seen = set()

    def add(p):
        n = norm(p)
        if p and n not in seen:
            seen.add(n)
            union.append(p)

    ids = [p.get("id") for p in profiles if isinstance(p, dict) and p.get("id")]
    for pid in ids:
        for d in visible_dirs(category, pid):
            add(d)
        # Собственные строки конфига тоже считаем индексируемыми: effective
        # снимает базу при выключенном флаге наследования, но приватное живёт и
        # в media.*, и в private_*.
        raw = _raw_profile_config(pid)
        for d in ((raw.get("media") or {}).get(media_key) or []):
            add(_resolve(d))
        for d in (raw.get(priv_key) or []):
            add(_resolve(d))
    # Общедоступный профиль мог быть недоступен list_profiles(): берём и его
    for d in visible_dirs(category, PM.get_default_profile_id()):
        add(d)
    return union


def own_dirs(category, profile_id=None):
    """Каталоги, которые принадлежат этому профилю, без унаследованных чужих.

    Для записи: `visible_dirs()` начинается с базы общедоступного профиля, и файл,
    положенный в неё, стал бы виден всем, а при снятии галки наследования исчез бы
    из собственного списка (`save_media_dirs` вычитает общедоступное). Общедоступный
    профиль — сам себе база, поэтому его `media.*` и есть свои.
    """
    import profiles_module as PM
    media_key, priv_key = CATEGORIES[category]
    pid = profile_id or active_profile_id()
    if not pid:
        return []
    raw = _raw_profile_config(pid)
    if PM.is_public_profile(pid):
        src = (raw.get("media") or {}).get(media_key) or []
    else:
        src = raw.get(priv_key) or []
        if not src:
            # Профиль заводил каталоги до модели private_*: его — всё, что не
            # общедоступная база.
            pub = {norm(d) for d in (PM.get_public_paths().get(media_key) or [])}
            src = [d for d in ((raw.get("media") or {}).get(media_key) or [])
                   if norm(d) not in pub]
    return [_resolve(d) for d in src if str(d or "").strip()]


def exclusive_own_dirs(category, profile_id=None):
    """Свои каталоги, которые не числятся своими ни у одного другого профиля.

    Нужна одному вызывающему — `media_transfer.resolve_target`: два профиля
    могли вписать в настройки один и тот же каталог (замер 04.11: три профиля
    объявляли своим `D:/Выкачка с ноута/музыка`), и загрузка в такой каталог
    попадала в библиотеку соседа. Личный каталог остаётся личным, только если
    он никому больше не обещан.
    """
    own = own_dirs(category, profile_id)
    if not own:
        return []
    import profiles_module as PM
    try:
        profiles = PM.list_profiles() or []
    except Exception:
        swallow.note('media_visibility.exclusive_own_dirs:L200')
        profiles = []
    pid = profile_id or active_profile_id()
    shared = set()
    for p in profiles:
        other = p.get("id") if isinstance(p, dict) else None
        if not other or other == pid:
            continue
        for d in own_dirs(category, other):
            shared.add(norm(d))
    return [d for d in own if norm(d) not in shared]


def is_visible(path, dirs):
    """Начинается ли путь с одного из каталогов (после нормализации)."""
    if not dirs:
        return True
    target = norm(path)
    return any(target == norm(d) or target.startswith(norm(d) + "/") for d in dirs)


def owned_by_other(path, profile_id) -> bool:
    """Загружен ли этот файл другим профилем (память — в media_owner).

    Каталог общий у двух профилей — это ещё не «файл общий»: загрузку одного
    из них второй обязан не видеть, даже когда путь лежит в его же списке.
    Проверка идёт до фильтра по каталогам, иначе «не видно в списке»
    оставалось бы «играется по прямой ссылке».
    """
    if not profile_id:
        return False
    try:
        import media_owner as MO
        for cat in CATEGORIES:
            owner = MO.owner_of(path, cat)
            if owner and owner != str(profile_id):
                return True
    except Exception:
        swallow.note('media_visibility.owned_by_other:L222')
    return False


def stream_allowed(path, profile_id=None) -> bool:
    """Пустить ли этот профиль к потоку файла.

    Скрытый от профиля трек не должен играться по прямому пути: иначе «не видно»
    превращается в «видно, но спрятано в UI». При этом файл вне библиотек всех
    профилей остаётся доступным — это легальный локальный поток (например,
    только что скачанный торрентом MKV, см. /api/media/stream), и чужую
    приватность он не трогает.
    """
    if not filter_enabled(profile_id):
        return True
    if owned_by_other(path, profile_id):
        return False
    dirs = [d for cat in CATEGORIES for d in visible_dirs(cat, profile_id)]
    if dirs and is_visible(path, dirs):
        return True
    if not dirs and not profile_id:
        # профиль не передан (старый внутренний вызов) — ведём себя как раньше
        return True
    # Профиль без каталогов не получает всё подряд: файл вне библиотек всех
    # профилей — легальный локальный поток (торрент, только что скачанный
    # файл); файл внутри чьей-то библиотеки — скрыт.
    for cat in CATEGORIES:
        lib = all_indexable_dirs(cat)
        # Пустой список — не «везде»: is_visible(путь, []) отвечает True, и без
        # этой проверки профиль без каталогов закрывал вообще все локальные
        # потоки, включая только что скачанный торрент.
        if lib and is_visible(path, lib):
            return False
    return True


def active_profile_id():
    """Профиль, чьи настройки сейчас применены серверу (глобальное состояние)."""
    import profiles_module as PM
    try:
        return PM.get_active_id() or PM.get_default_profile_id()
    except Exception:
        swallow.note('media_visibility.active_profile_id:L224')
        return None


def is_known_profile(profile_id) -> bool:
    """Есть ли такой профиль (или это общедоступный)."""
    import profiles_module as PM
    if not profile_id:
        return False
    try:
        if profile_id == PM.get_default_profile_id():
            return True
        return any(p.get("id") == profile_id for p in (PM.list_profiles() or []))
    except Exception:
        swallow.note('media_visibility.is_known_profile:L238')
        return False


def sql_filter(category, profile_id=None):
    """(SQL-фрагмент, params) для WHERE по видимым каталогам, или (None, []).

    Путь в БД хранится с флексами (см. индексатор), поэтому префикс строим
    так же. ESCAPE нужен, потому что в путях бывают `_` и `%` — без него они
    стали бы масками и пустили чужие каталоги.

    Профиль, которого нет в индексе (стёртая кука, чужой id в запросе), не
    видит ничего: для него «нет каталогов» — это не пустой конфиг, а чужое
    имя, и отдавать всю таблицю значило бы открыть приватную библиотеку.
    Реальный профиль без единого каталога тоже не видит ничего: раньше ему
    отдавалась вся таблица, и новый/пустой профиль показывал в списке треки,
    загруженные в другом профиле (это и был замер «песня из test1 видна
    test2»). Общедоступная база попадает в список через effective-конфиг, так
    что честный профиль без своих каталогов всё равно видит её.
    Профиль None означает «активный» — его по-прежнему не фильтруем, чтобы
    старые внутренние вызовы без профиля не остались с пустой библиотекой.

    Сверху кладётся слой владельца (media_owner): каталог может быть общим у
    двух профилей, а загруженный в него файл — принадлежать одному.
    """
    if not filter_enabled(profile_id):
        return None, []
    dirs = visible_dirs(category, profile_id)
    if not dirs:
        if profile_id is None:
            return None, []
        return "0", []
    parts = []
    params = []
    for d in dirs:
        prefix = str(d).replace("\\", "/").rstrip("/") + "/"
        esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        parts.append("filepath LIKE ? ESCAPE '\\'")
        params.append(esc + "%")
    cond = "(" + " OR ".join(parts) + ")"
    try:
        import media_owner as MO
        own_cond, own_params = MO.sql_clause(category, profile_id)
    except Exception:
        swallow.note('media_visibility.sql_filter:L341')
        own_cond, own_params = "", []
    if own_cond:
        cond = "(" + cond + " AND " + own_cond + ")"
        params = params + list(own_params)
    return cond, params


def profile_of_request(request):
    """Профиль вызывающего: cookie → токен агента → общедоступный.

    Гость из LAN без куки попадает на общедоступный профиль и видит только
    общедоступную библиотеку — это его же решение («LAN-гостям только
    общедоступное»). Берём готовый резолв у profiles_module: он знает и про
    токен агента, и про заблокированные профили, и не подмешивает активный
    профиль сервера (иначе чужая приватная библиотека утёк бы гостю).
    """
    import profiles_module as PM
    try:
        return PM.request_profile(request)
    except Exception:
        swallow.note('media_visibility.profile_of_request:L287')
        return PM.resolve_profile_id("")


def invalidate(profile_id=None):
    """Сброс кэша конфигов — после сохранения настроек медиа/профиля."""
    if profile_id is None:
        _cache.clear()
    else:
        _cache.pop(("cfg", profile_id or ""), None)
