"""
Переносимость: пути внутри сборки не должны зависеть от того, где она лежит.

Проблема, которую это чинит
---------------------------
Переносимая сборка копируется целиком (флешка, другой диск, вторая папка),
но конфиги хранили АБСОЛЮТНЫЕ пути. Из-за этого приложение, запущенное из
E:\\...\\dist\\InfinitySearch, продолжало читать данные из папки, из которой
собрали в первый раз (E:\\compilinfinity\\data). Пользователь переносит всю
папку — а заметки, расширения и приложения остаются в старом месте, и ФМ
открывается не там.

Что делает этот модуль
----------------------
1. `run_startup_migration()` — один раз за установку (метка в
   `data/.portable_migrated`):
   * проверяет `profiles_index.json` — `root_path` каждого профиля должен
     лежать внутри текущего `PROFILES_DIR`; если нет, пересчитываем путь
     и копируем содержимое старого root в новый (копия, источник не трогаем);
   * проверяет `config.json` каждого профиля — ключи из `PROFILE_DATA_DIRS`
     переписываются на текущие, если указывают в старую/чужую папку;
   * проверяет глобальные `apps_dirs` / `default_apps_dir` — если они
     смотрят в папку, которая сама является старой копией этой сборки,
     переводим на текущий корень.

2. Постоянная страховка (без метки, работает всегда):
   * `path_is_portable()` — путь внутри текущей сборки (кроме внешних
     библиотек вроде books_dir/zim_dirs — их не трогаем никогда).

Ничего не удаляется: старые папки остаются на месте как резервная копия.
"""

import os
import shutil
import swallow

_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _root():
    """Корень сборки — тот же, что у config_module."""
    try:
        from config_module import _root as cfg_root
        return os.path.normpath(cfg_root())
    except Exception:
        swallow.note('portable_migrate._root:L45')
        parent = os.path.dirname(_APP_DIR)
        for marker in ("models", "engine", "data"):
            if os.path.exists(os.path.join(parent, marker)):
                return parent
        return _APP_DIR


def _expand_token(path):
    """Развернуть «${ROOT}»/«${APP}» в абсолютный путь — только для сравнения.

    Без этого os.path.abspath получает строку, начинающуюся с «$», считает её
    относительной и приклеивает текущий каталог. Проверка «внутри ли сборки»
    отвечала неверно, и миграция при каждом запуске переписывала корректную
    переносимую запись как устаревшую.
    """
    s = str(path or "")
    if "${ROOT}" not in s and "${APP}" not in s:
        return s
    return s.replace("${ROOT}", _root()).replace("${APP}", _APP_DIR)


def _norm(path):
    try:
        return os.path.normpath(os.path.abspath(_expand_token(path))).replace("\\", "/").rstrip("/")
    except Exception:
        swallow.note('portable_migrate._norm:L71')
        return str(path)


def _portable(path):
    """Записать путь внутри сборки переносимой формой «${ROOT}/...».

    Всё, что лежит вне сборки (книги на другом диске, wiki, модели),
    config_module.to_portable оставляет как есть — переносить их нельзя.
    """
    try:
        from config_module import to_portable
        return to_portable(path)
    except Exception:
        swallow.note('portable_migrate._portable:L85')
        return path


def _inside(path, base):
    """path находится внутри base (или совпадает)."""
    p, b = _norm(path), _norm(base)
    return p == b or p.startswith(b + "/")


def path_is_portable(path):
    """Путь ведёт внутрь текущей сборки (а не в её старую копию)."""
    if not path:
        return False
    return _inside(path, _root())


def _marker_file():
    try:
        import profiles_module as pm
        return os.path.join(pm.DATA_DIR, ".portable_migrated")
    except Exception:
        swallow.note('portable_migrate._marker_file:L107')
        return os.path.join(_root(), "data", ".portable_migrated")


def _log(msg):
    try:
        import logging
        logging.getLogger("infinity.portable").warning(msg)
    except Exception:
        swallow.note('portable_migrate._log:L116')
        pass


def _tree_copy_if_empty(src, dst, max_files=200000):
    """Скопировать из src в dst всё, чего в dst ещё НЕТ. Источник не трогаем.

    Имя функции оставлено прежним, но поведение изменилось намеренно.
    Раньше здесь стояло «копировать, только если dst пуст», и это молча
    теряло данные: в целевом каталоге уже лежал один служебный файл
    (например 1790942059457.json в notes), из-за чего перенос 11 заметок
    целиком пропускался — пользователь видел пустые заметки.

    Теперь сливаем пофайлово: существующие файлы НЕ перезаписываем
    (иначе свежая сборка затёрла бы более новые данные), недостающие
    копируем.
    """
    if not src or not os.path.isdir(src):
        return False
    try:
        if os.path.abspath(src) == os.path.abspath(dst):
            return False
    except Exception:
        swallow.note('portable_migrate._tree_copy_if_empty:L139')
        return False

    copied = 0
    for root, dirs, files in os.walk(src):
        # Служебные подкаталоги (корзина) не переносим — это временное.
        dirs[:] = [d for d in dirs if d not in (".trash", "__pycache__")]
        for name in files:
            s = os.path.join(root, name)
            rel = os.path.relpath(s, src)
            d = os.path.join(dst, rel)
            try:
                if os.path.exists(d):
                    continue          # уже есть — не трогаем
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)
                copied += 1
                if copied >= max_files:
                    _log(f"portable: остановка на {copied} файлах ({src})")
                    return True
            except Exception:
                swallow.note('portable_migrate._tree_copy_if_empty:L160')
                continue
    return copied > 0


# ─────────────────────── индекс профилей ───────────────────────

def _migrate_index():
    """Привести root_path профилей к текущему PROFILES_DIR."""
    try:
        import profiles_module as pm
    except Exception as e:
        swallow.note('portable_migrate._migrate_index:L172', e)
        _log(f"portable: profiles_module недоступен: {e}")
        return 0

    try:
        index = pm._load_index()
    except Exception as e:
        swallow.note('portable_migrate._migrate_index:L179', e)
        _log(f"portable: индекс профилей не прочитан: {e}")
        return 0

    changed = 0
    for p in index.get("profiles", []):
        pid = p.get("id")
        if not pid:
            continue
        cur = p.get("root_path")
        want = os.path.join(pm._profile_path(pid), "root")

        # Уже правильный путь — ничего не делаем.
        if cur and _norm(cur) == _norm(want):
            continue

        # Путь указывает в старую копию сборки (или вообще неизвестно куда) —
        # забираем данные оттуда, если своих ещё нет.
        if cur and os.path.isdir(cur) and not _inside(cur, pm.PROFILES_DIR):
            _log(f"portable: профиль '{pid}' root_path уводил в {cur} — переносим в {want}")
            _tree_copy_if_empty(cur, want)

        # Каталог отсутствует — создаём со стандартными подпапками.
        if not os.path.isdir(want):
            os.makedirs(want, exist_ok=True)
            for folder in ("Desktop", "Documents", "Media", "Downloads", "Pictures"):
                os.makedirs(os.path.join(want, folder), exist_ok=True)

        p["root_path"] = want
        changed += 1

    if changed:
        try:
            pm._save_index(index)
            _log(f"portable: исправлено профилей — {changed}")
        except Exception as e:
            swallow.note('portable_migrate._migrate_index:L215', e)
            _log(f"portable: индекс не сохранён: {e}")
    return changed


# ─────────────────────── конфиги профилей ───────────────────────

# Ключи, которые НЕ трогаем: это внешние библиотеки, а не данные сборки.
EXTERNAL_KEYS = {
    "books_dir", "zim_dirs", "collection_db_path", "private_books_dir",
    "private_collection_db_path", "private_zim_dirs",
    "lm_studio", "engine_dir", "models_dir", "tts_dir", "stt_dir",
}


def _is_stale_build_path(path):
    """Путь абсолютный и смотрит не в текущую сборку.

    Намеренно НЕ трогаем:
      * относительные пути («defaultapps») — config_module._expand_path уже
        разворачивает их от _root(), то есть они переносимы по определению;
      * плейсхолдеры «${ROOT}/...» и «${APP}/...» — это и есть переносимая
        запись, трогать её нельзя.
    Раньше обе проверки отсутствовали, и миграция превращала «${ROOT}/data/html»
    в абсолютный «<корень>\\html» — терялся подкаталог data.
    """
    if not path:
        return False
    raw = str(path)
    if "${ROOT}" in raw or "${APP}" in raw:
        return False
    if not os.path.isabs(raw):
        return False
    return not _inside(raw, _root())


def _looks_like_build_root(path):
    """Каталог с типовой структурой копии сборки: app/ и (data/ или engine/)."""
    try:
        if not path or not os.path.isdir(path):
            return False
        return (os.path.isdir(os.path.join(path, "app"))
                and (os.path.isdir(os.path.join(path, "data"))
                     or os.path.isdir(os.path.join(path, "engine"))))
    except Exception:
        swallow.note('portable_migrate._looks_like_build_root:L260')
        return False


def _inside_old_build_copy(path):
    """Каталог реально лежит в корне (или data/) какой-то копии сборки.

    Одна лишь проверка имени (_looks_like_build_dir) ошибочно переносила
    пользовательскую папку E:/apps в ${ROOT}/apps — настройки переезжали молча
    (ловил тест test_persistence.test_corrupt_index_does_not_orphan_real_profiles).
    Настоящая старая копия отличается тем, что рядом с переносимым каталогом
    стоят app/ и data|engine/ сборки; у внешней папки пользователя их нет.
    """
    p = _norm(path)
    for _ in range(3):  # каталог может лежать на глубину <root>/data/<name>
        p = os.path.dirname(p)
        if not p:
            return False
        if _looks_like_build_root(p):
            return True
    return False


def _looks_like_build_dir(path):
    """Похоже ли это на каталог ВНУТРИ сборки (apps/html/defaultapps)?

    Отсекаем настоящие внешние библиотеки: важно имя конечного каталога.
    Иначе под правило попали бы books_dir, zim_dirs и любые пользовательские
    папки, которые мы переносить не вправе.
    """
    base = os.path.basename(_norm(path))
    return base in ("apps", "html", "defaultapps")


def _target_inside_root(rel_name, root):
    """Найти каталог с таким именем внутри сборки.

    Проверяем два типовых места: прямо в корне (apps, defaultapps) и в
    data/ (html, notes). Только так мы не выдумаем несуществующий путь:
    раньше html_viewer_dirs указывал на старую E:\\compilinfinity\\data\\html,
    а <корень>\\html не существует, и перенос молча пропускался.
    """
    for cand_rel in (rel_name, os.path.join("data", rel_name)):
        cand = os.path.join(root, cand_rel)
        if os.path.isdir(cand):
            # Переносимой формой: этот же конфиг будет работать после
            # переноса папки на другой диск.
            return _portable(cand)
    return None


def _root_key_target(key, root):
    """Куда переносить скалярный ключ вида default_apps_dir."""
    rel = "defaultapps"
    return _target_inside_root(rel, root)


def _fix_build_dirs(cfg, root, where):
    """Привести каталоги сборки (apps/html/defaultapps) к текущему корню.

    Правится словарь конфига НА МЕСТЕ. Переносим только тогда, когда
    эквивалентный каталог внутри сборки реально существует: иначе мы бы
    выдумали несуществующий путь вместо того, чтобы оставить как есть.
    """
    changed = 0
    for key in _ROOT_KEYS:
        cur = cfg.get(key)
        if isinstance(cur, str) and cur and _is_stale_build_path(cur):
            target = _root_key_target(key, root)
            if target:
                cfg[key] = target
                _log(f"portable: {where} {key}={cur} -> {target}")
                changed += 1

    for key in _ROOT_LIST_KEYS:
        lst = cfg.get(key)
        if not isinstance(lst, list):
            continue
        new = []
        touched = False
        for p in lst:
            if (isinstance(p, str) and _looks_like_build_dir(p)
                    and _is_stale_build_path(p) and _inside_old_build_copy(p)):
                target = _target_inside_root(os.path.basename(_norm(p)), root)
                if target:
                    new.append(target)
                    touched = True
                    continue
            new.append(p)
        if touched:
            cfg[key] = new
            _log(f"portable: {where} {key} -> {new}")
            changed += 1
    return changed


def _fix_profile_config(pid):
    try:
        import profiles_module as pm
    except Exception:
        swallow.note('portable_migrate._fix_profile_config:L360')
        return 0

    cfg_path = pm._profile_config_path(pid)
    if not os.path.exists(cfg_path):
        return 0
    try:
        cfg = pm._read_json(cfg_path, default=None)
    except Exception:
        swallow.note('portable_migrate._fix_profile_config:L369')
        return 0
    if not isinstance(cfg, dict):
        return 0

    try:
        data_dirs = pm._setup_profile_data_dirs(pid)
    except Exception:
        swallow.note('portable_migrate._fix_profile_config:L377')
        data_dirs = {}

    changed = 0
    for key, want in data_dirs.items():
        if key in EXTERNAL_KEYS:
            continue
        cur = cfg.get(key) or ""
        if not cur:
            cfg[key] = _portable(want)
            changed += 1
            continue
        norm = _norm(cur)
        # Путь внутри текущей сборки, но не в каталоге этого профиля —
        # значит он указывает на каталог ДРУГОГО профиля или на старую копию.
        if not _inside(norm, pm.PROFILES_DIR) or _path_in_other_profile(norm, pid):
            if not _inside(norm, pm.PROFILES_DIR):
                _tree_copy_if_empty(cur, want)
            _log(f"portable: профиль '{pid}' {key} уводил в {cur} — ставим {want}")
            # В конфиг пишем переносимую форму: абсолютный путь запекает букву
            # диска, и после переноса сборки конфиг снова становится битым.
            cfg[key] = _portable(want)
            changed += 1

    # Каталоги сборки: профильный конфиг ПЕРЕКРЫВАЕТ глобальный
    # (config_module.load_config сливает pm.get_active_effective_config()),
    # поэтому именно здесь жило "<корень>\defaultapps".
    changed += _fix_build_dirs(cfg, _root(), f"профиль '{pid}'")

    if changed:
        try:
            pm._atomic_write_json(cfg_path, cfg)
        except Exception as e:
            swallow.note('portable_migrate._fix_profile_config:L410', e)
            _log(f"portable: config профиля {pid} не сохранён: {e}")
    return changed


def _path_in_other_profile(path, profile_id):
    try:
        import profiles_module as pm
        return pm._path_in_other_profile(path, profile_id)
    except Exception:
        swallow.note('portable_migrate._path_in_other_profile:L420')
        return None


def _migrate_profile_configs():
    try:
        import profiles_module as pm
        index = pm._load_index()
    except Exception:
        swallow.note('portable_migrate._migrate_profile_configs:L429')
        return 0
    total = 0
    for p in index.get("profiles", []):
        pid = p.get("id")
        if pid:
            total += _fix_profile_config(pid)
    return total


# ─────────────────────── глобальный конфиг ───────────────

# Каталоги, которые ВСЕГДА живут рядом с кодом.
_ROOT_KEYS = ("default_apps_dir",)
_ROOT_LIST_KEYS = ("apps_dirs", "html_viewer_dirs")


def _global_config_paths():
    """Куда писать глобальный конфиг (тот же выбор, что в load_config).

    Если задана INFINITY_CONFIG — пишем ТОЛЬКО в неё. Без этого тесты на
    песочнице переписывали настоящий app/config.json путями в temp.
    """
    env = os.environ.get("INFINITY_CONFIG")
    if env:
        return env
    local = os.path.join(_APP_DIR, "config.json")
    if os.path.exists(local):
        return local
    return None


def _fix_global_config():
    path = _global_config_paths()
    if not path or not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            import json
            cfg = json.load(f)
    except Exception as e:
        swallow.note('portable_migrate._fix_global_config:L470', e)
        _log(f"portable: глобальный конфиг не прочитан: {e}")
        return 0
    if not isinstance(cfg, dict):
        return 0

    changed = _fix_build_dirs(cfg, _root(), "глобальный")
    if changed:
        try:
            import json
            tmp = path + ".tmp_migrate"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            swallow.note('portable_migrate._fix_global_config:L485', e)
            _log(f"portable: глобальный конфиг не сохранён: {e}")
    return changed


# ─────────────────────── точка входа ───────────────────────

def run_startup_migration(force=False):
    """Разовая миграция при старте. Возвращает сводку (для логов)."""
    marker = _marker_file()
    if not force and os.path.exists(marker):
        return {"skipped": True}

    summary = {"index": 0, "profiles": 0, "global": 0}
    try:
        summary["index"] = _migrate_index()
        summary["profiles"] = _migrate_profile_configs()
        summary["global"] = _fix_global_config()
    except Exception as e:
        swallow.note('portable_migrate.run_startup_migration:L504', e)
        _log(f"portable: миграция прервана: {e}")
        return summary

    if any(summary.values()):
        _log(f"portable: миграция выполнена {summary}")
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("1")
    except Exception:
        swallow.note('portable_migrate.run_startup_migration:L515')
        pass
    return summary


def check_profile_root(profile_id):
    """Страховка для горячего вызова: путь root профиля внутри PROFILES_DIR?

    Используется get_profile_root() — раньше он возвращал записанный в
    индексе абсолютный путь как есть, из-за чего ФМ открывался на каталог
    старой копии сборки.
    """
    try:
        import profiles_module as pm
    except Exception:
        swallow.note('portable_migrate.check_profile_root:L530')
        return None
    profile_dir = pm._profile_path(profile_id)
    return os.path.join(profile_dir, "root")
