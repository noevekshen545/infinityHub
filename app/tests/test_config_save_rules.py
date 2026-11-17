"""Правила сохранения конфига: что выживает из payload Настроек до мержа.

Причина набора — класс багов «поле в форме есть, но не сохраняется»:
значение доезжало до `_do_save_config`, а `strip_empty` вырезал его как
«поле не задано». Проверяются именно правила, а не весь путь записи:
полный путь трогает живые профили и в тестах недопустим.
"""
import ast
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import swallow

# routes/legacy_search_config_books.py тянет весь сервер при импорте, поэтому
# берём из него только чистые функции правил — без побочных эффектов.
_SRC = os.path.join(APP, "routes", "legacy_search_config_books.py")
_NS = {"swallow": swallow}
with open(_SRC, encoding="utf-8") as f:
    _tree = ast.parse(f.read())
_want = {"strip_empty", "CLEARABLE_PATHS", "CLEARABLE_DIR_NAMES",
         "CLEARABLE_MEDIA_NAMES"}
_body = [n for n in _tree.body
         if (isinstance(n, ast.FunctionDef) and n.name in _want)
         or (isinstance(n, ast.Assign)
             and any(getattr(t, "id", "") in _want for t in n.targets))]
exec(compile(ast.Module(body=_body, type_ignores=[]), _SRC, "exec"), _NS)

strip_empty = _NS["strip_empty"]


class TestStripEmpty:
    def test_api_key_cleared_on_purpose_survives(self):
        out = strip_empty({"lm_studio": {"api_key": "", "url": "http://x"}})
        assert out["lm_studio"]["api_key"] == ""
        assert out["lm_studio"]["url"] == "http://x"

    def test_empty_dir_cleared_on_purpose_survives(self):
        out = strip_empty({"zim_dirs": [], "books_dir": ""})
        assert out == {"zim_dirs": [], "books_dir": ""}

    def test_stt_auto_model_survives(self):
        # «Авто (по устройству)» — это пустая строка, а не отсутствие поля.
        out = strip_empty({"speech": {"stt": {"local_model": "",
                                              "local_language": "auto"}}})
        assert out["speech"]["stt"]["local_model"] == ""

    def test_unrelated_empty_is_dropped(self):
        # Тумблеры и числа, присланные пустыми, не должны затирать конфиг.
        out = strip_empty({"search": {"limit": "", "use_embeddings": None}})
        assert out == {"search": {}}

    def test_custom_section_kept_whole(self):
        out = strip_empty({"custom": {"url": "", "api_key": "", "model": ""}})
        assert "custom" in out

    def test_media_dirs_cleared_survive(self):
        out = strip_empty({"media": {"music_dirs": [], "video_dirs": []}})
        assert out["media"] == {"music_dirs": [], "video_dirs": []}
