"""Галка «показывать данные общедоступного профиля» (в настройках каждого профиля).

Что здесь живёт
---------------
Общедоступный профиль — обычный профиль с правами «виден всем». Его набор
заметок/чатов/ответов/AI-ответов/вложений/расширений лежит в своих каталогах,
и по умолчанию другому профилю он не показывается: профиль видит только своё.
Галка включает показ общедоступного набора поверх собственного — список
складывается из двух каталогов, а запись по id находит файл там, где он лежит.

Ключ флага
----------
`public_data_enabled` в конфиге профиля: {категория: bool} или bool для всех.
Отсутствующий ключ = ВЫКЛ. Миграции старых конфигов нет намеренно: до этой
правки общедоступные заметки/чаты обычным профилям не показывались, так что
«нет ключа = выключено» сохраняет вид всех существующих профилей. Новому
профилю ключ пишется явно со всеми False (routes/profiles.py), чтобы галка не
само включалась.

Пути библиотек (книги/ZIM/HTML/apps/медиа) здесь не при чём: у них свой
механизм `public_paths_enabled` со включённым по умолчанию наследованием, и
снятая там галка уже убирает чужие каталоги из слияния конфигов. Касается он
этого модуля только в одном: seed_profile() выключает новому профилю оба
флага, чтобы чистый профиль не находил на полках чужие книги.

Вне запроса (фон, CLI, индексация) каталоги берутся из глобалов сервера, как
в profile_paths: флаг судит о чужом активном профиле, поэтому общедоступный
набор поверх не подмешивается.
"""

import os

import profile_paths
import swallow

# категория флага -> имя каталога в profile_paths.DIR_KEYS
DIR_OF = {
    "notes": "NOTES_DIR",
    "chat": "CHAT_DIR",
    "ai_answers": "AI_DIR",
    "attachments": "ATTACHMENTS_DIR",
    "extensions": "EXTENSIONS_DIR",
}
CATEGORIES = tuple(DIR_OF) + ("bookmarks",)

FLAG_KEY = "public_data_enabled"
# Флаг общедоступных путей библиотек: механизм чужой (profiles_module), но
# сеется вместе с этим — новому профилю выключены обе галки.
PATHS_FLAG_KEY = "public_paths_enabled"


def _profile_id():
    """Профиль запроса; вне запроса — None (флаг тогда не применяется)."""
    return profile_paths.current_profile_id()


def _public_id():
    try:
        import profiles_module
        return profiles_module.get_default_profile_id()
    except Exception:
        swallow.note('public_view._public_id:L61')
        return None


def enabled(category, profile_id=None):
    """Включён ли показ общедоступного набора этой категории профилю."""
    pid = profile_id or _profile_id()
    if not pid:
        return False
    pub = _public_id()
    if not pub or pid == pub:
        # Общедоступному профилю наследовать нечего: свои файлы и есть общие.
        return False
    cfg = profile_paths._config(pid)
    val = cfg.get(FLAG_KEY)
    if isinstance(val, bool):
        return val
    if isinstance(val, dict):
        return bool(val.get(category, False))
    return False


def own_dir(category):
    """Каталог категории для профиля запроса (или глобал вне запроса)."""
    import server_backup as _sb
    _pp = profile_paths.proxy(_sb)
    name = DIR_OF[category]
    if _profile_id():
        return profile_paths.dir_for(_profile_id(), name)
    return getattr(_sb, name, "")


def read_dirs(category):
    """Каталоги, которые профиль имеет право читать: свои + общедоступный."""
    own = own_dir(category)
    out = [own] if own else []
    if enabled(category):
        pub = profile_paths.dir_for(_public_id(), DIR_OF[category])
        if pub and os.path.normpath(pub) != os.path.normpath(own or ""):
            out.append(pub)
    return out


def read_file(category, filename):
    """Существующий файл категории: сначала в своём каталоге, затем общий."""
    for d in read_dirs(category):
        p = os.path.join(d, filename)
        if os.path.exists(p):
            return p
    return os.path.join(own_dir(category), filename)


def child_dir(category, subdir):
    """Каталог ``<каталог категории>/<subdir>`` там, где он уже есть.

    Вложения лежат по одному подкаталогу на заметку, поэтому «где папка
    заметки» — это вопрос, в каком из видимых наборов она создана: правка и
    загрузка обязана попасть туда же, куда смотрел список, а не создать пустую
    копию в своём наборе. Если папки нет нигде, отдаём свой каталог — там её и
    создаст вызывающий код.
    """
    dirs = read_dirs(category)
    own = dirs[0] if dirs else ""
    if not subdir or "/" in str(subdir) or "\\" in str(subdir) or ".." in str(subdir):
        # Пустой ответ — то же «недопустимый идентификатор», что отдаёт
        # _notes_child: вызывающий код проверяет путь и не создаёт папку.
        return ""
    own_path = os.path.join(own, str(subdir)) if own else ""
    if own_path and os.path.isdir(own_path):
        return own_path
    for d in dirs[1:]:
        p = os.path.join(d, str(subdir))
        if os.path.isdir(p):
            return p
    return own_path


def flags_for(profile_id):
    """Снимок всех категорий для страницы настроек (мастер-галка рисует по нему)."""
    return {c: bool(enabled(c, profile_id)) for c in CATEGORIES}


def seed_profile(profile_id):
    """Записать новому профилю все галки общедоступного выключенными.

    «По умолчанию отключено» держится на явном ключе, а не на изменении чтения
    отсутствующего: у существующих профилей ключа нет, и они продолжают видеть
    ровно то, что видели до правки. Путям библиотек это касается в ту же сторону:
    по решению «пиннинг текущих + OFF для новых» новому профилю общедоступные
    каталоги выключаются сразу, а старые читают прежний «ключа нет = ВКЛ».
    Возвращает True, если конфиг был дописан.
    """
    if not profile_id:
        return False
    try:
        import profiles_module
        cfg = profiles_module.get_profile_config(profile_id) or {}
        changed = False
        if cfg.get(FLAG_KEY) is None:
            cfg[FLAG_KEY] = {c: False for c in CATEGORIES}
            changed = True
        if cfg.get(PATHS_FLAG_KEY) is None:
            cfg[PATHS_FLAG_KEY] = {k: False for k in profiles_module.PUBLIC_FLAG_KEYS}
            changed = True
        if not changed:
            return False
        return bool(profiles_module.save_profile_config(profile_id, cfg))
    except Exception:
        swallow.note('public_view.seed_profile:L169')
        return False
