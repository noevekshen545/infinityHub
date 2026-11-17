"""Раскладка snippets у web-сборок: баг PhotoCraft 0.20.1.

Архив официального релиза держит snippet в корне (`snippets/eframe-.../`),
а wasm-bindgen glue `app/photocraft-web.js` импортирует `./snippets/...`.
Браузер ищет файл в `app/snippets/`, получает 404 и приложение висит на
«Loading» — проверено сетевым журналом живого приложения 04.11.
"""
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import apps_updater as U  # noqa: E402


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_snippet_копируется_рядом_с_модулем(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, "index.html"), "<html></html>")
    _write(os.path.join(root, "snippets", "eframe-1", "inline0.js"), "export {};")
    _write(os.path.join(root, "app", "photocraft-web.js"),
           'import * as m from "./snippets/eframe-1/inline0.js";\n')

    assert U._place_snippets_for_app_js(root) == 1
    dst = os.path.join(root, "app", "snippets", "eframe-1", "inline0.js")
    assert os.path.isfile(dst)
    # корневая раскладка не тронута
    assert os.path.isfile(os.path.join(root, "snippets", "eframe-1", "inline0.js"))


def test_без_импорта_ничего_не_копируется(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, "snippets", "s", "inline0.js"), "")
    _write(os.path.join(root, "app", "main.js"), "console.log(1);\n")

    assert U._place_snippets_for_app_js(root) == 0
    assert not os.path.exists(os.path.join(root, "app", "snippets"))


def test_повторный_прогон_идемпотентен(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, "snippets", "s", "inline0.js"), "")
    _write(os.path.join(root, "app", "main.js"), 'from "./snippets/s/inline0.js";\n')

    assert U._place_snippets_for_app_js(root) == 1
    assert U._place_snippets_for_app_js(root) == 0


def test_нет_корневых_snippets_ничего_не_делает(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, "app", "main.js"), 'from "./snippets/s/inline0.js";\n')
    assert U._place_snippets_for_app_js(root) == 0
