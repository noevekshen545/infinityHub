"""Вторая половина «настройки сохраняются»: что форма пишет, GET обязан вернуть.

Проба формы (`app/tools/_settings_payload_probe.py`) доказывает, что значение
доезжает до payload. Дальше его принимает `_do_save_config` (мерж без whitelist,
правила — в test_config_save_rules.py), а вот отдаёт его страница через фиксированный
список секций в `api_config_get`. Если секции там нет, запись в файл есть, а поле
после перезагрузки пустое — ровно тот баг, который уже случался с `custom`
(комментарий в маршруте). Тест сверяет множества без живого сервера и без записи
в профили.
"""
import ast
import os

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROUTE = os.path.join(APP, "routes", "legacy_search_config_books.py")
_PROBE = os.path.join(APP, "tools", "_settings_payload_probe.py")

# Свои пути профиля форма пишет как private_*, а GET возвращает их внутри
# own_paths и другими именами. Расширение списка = осознанный шаг: новый
# private_* без сюда записанного алиаса тест валит, а не молча пропускает.
OWN_ALIASES = {
    "private_books_dir": "books_dir",
    "private_zim_dirs": "zim_dirs",
    "private_html_viewer_dirs": "html_viewer_dirs",
    "private_apps_dirs": "apps_dirs",
    "private_collection_db_path": "collection_db_path",
    "private_music_dirs": "music_dirs",
    "private_video_dirs": "video_dirs",
    "private_photos_dirs": "photo_dirs",
}


def _parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read())


def _get_sections():
    """Верхнеуровневые ключи config из api_config_get + ключи own_paths."""
    fn = next(n for n in _parse(_ROUTE).body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "api_config_get")
    top, own = set(), set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "config" for t in node.targets):
            top |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].slice, ast.Constant)):
            key = node.targets[0].slice.value
            if isinstance(key, str):
                top.add(key)
                if key == "own_paths":
                    # own_paths собирается вложенным литералом внутри _exp_paths(...)
                    for sub in ast.walk(node.value):
                        if isinstance(sub, ast.Dict):
                            own |= {k.value for k in sub.keys
                                    if isinstance(k, ast.Constant)}
    return top, own


def _payload_sections():
    cases = next(ast.literal_eval(n.value) for n in _parse(_PROBE).body
                 if isinstance(n, ast.Assign)
                 and getattr(n.targets[0], "id", "") == "CASES")
    return {c[1][0] for c in cases}, len(cases)


def _own_paths_aliases_route():
    """алиасы теста должны совпадать с реальными ключами own_paths на маршруте"""
    _, own = _get_sections()
    assert own, "на маршруте не найден литерал own_paths"
    missing = {k: v for k, v in OWN_ALIASES.items() if v not in own}
    assert not missing, "own_paths на маршруте не отдаёт: " + ", ".join(
        "%s -> %s" % (k, v) for k, v in missing.items())


def _sections_echoed(section):
    top, _ = _get_sections()
    return section in top or OWN_ALIASES.get(section) is not None


class TestEchoGetConfig:
    def test_kazhdaya_sekciya_payloadavozvraetsya_getom(self):
        sections, n = _payload_sections()
        assert n >= 50, "проба формы стала короче — проверяем не то"
        not_echoed = sorted(s for s in sections if not _sections_echoed(s))
        assert not not_echoed, ("GET /api/config не вернёт эти секции, поле "
                                "после перезагрузки будет пустым: "
                                + ", ".join(not_echoed))

    def test_get_otdaet_sekcii_formy_rechnyu_i_poisk(self):
        top, _ = _get_sections()
        # Секции, которые страница перечитывает после записи, но которые не
        # лежат в CASES пробой (проверяются другими тестами) — не должны
        # исчезнуть с маршрута.
        for must in ("speech", "search", "stats", "ui", "research", "media",
                     "custom", "custom_providers", "custom_active",
                     "public_paths_enabled", "own_paths"):
            assert must in top, "секция %s пропала из ответа GET /api/config" % must

    def test_svoi_puti_prozhodyan_cherez_own_paths(self):
        _own_paths_aliases_route()
