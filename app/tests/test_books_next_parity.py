# -*- coding: utf-8 -*-
"""Паритет классических страниц книг и их новых версий.

Библиотека 2.0 — основной `/books` (классическая версия вынесена в
`templates_backup`), генератор книг — `/bookgen/next`. Обе новые страницы —
полные копии со своим слоем оформления. Тест ловит случайное «урезание»
функционала при переписывании: набор id, функций, обработчиков и API-адресов
в новой версии обязан быть не меньше, чем в классической.

Тест не поднимает сервер: он сравнивает только шаблоны, поэтому работает
быстро и не зависит от данных пользователя.
"""

import os
import re

import pytest

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_APP_DIR, "templates")
_BACKUP = os.path.join(_APP_DIR, "templates_backup")

_ID_RE = re.compile(r'\bid="([A-Za-z0-9_\-]+)"')
# function name(...) — обычные объявления, включая async.
_FUNC_RE = re.compile(r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
# const name = function / const name = (...) => / const name = async (...) =>
_ASSIGN_FUNC_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
    r"(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][A-Za-z0-9_$]*\s*=>)"
)
_API_RE = re.compile(r"/api/[A-Za-z0-9_\-/]+")
_ONCLICK_RE = re.compile(r'\bon(?:click|change|input|submit|keydown|keyup)="([^"]+)"')
_LS_RE = re.compile(r"localStorage\.(?:getItem|setItem|removeItem)\(\s*['\"]([^'\"]+)")

# (классическая страница, она в templates_backup?, новая страница, маркер)
_CASES = (
    ("page_books.html", True, "page_books.html", "data-books-skin"),
    ("page_bookgen.html", False, "page_bookgen_next.html", "data-bookgen-skin"),
)


def _read(filename, backup=False):
    base = _BACKUP if backup else _TEMPLATES
    path = os.path.join(base, filename)
    if not os.path.exists(path) and backup:
        pytest.skip("templates_backup is not included in this repository")
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def _inventory(html):
    return {
        "ids": set(_ID_RE.findall(html)),
        "functions": set(_FUNC_RE.findall(html)) | set(_ASSIGN_FUNC_RE.findall(html)),
        "api": set(_API_RE.findall(html)),
        "onclick": set(_ONCLICK_RE.findall(html)),
        "storage": set(_LS_RE.findall(html)),
    }


@pytest.mark.parametrize("original,original_in_backup,copy,marker", _CASES)
def test_copy_keeps_page_identity(original, original_in_backup, copy, marker):
    src = _read(original, backup=original_in_backup)
    dst = _read(copy)

    assert marker in dst, "у новой страницы нет маркера скина"
    assert marker not in src, "классическая страница не должна помечаться скином"
    assert "/static/css/library_next.css" in dst
    assert "/static/js/library_next.js" in dst
    assert "/static/css/library_next.css" not in src
    assert "/static/js/library_next.js" not in src


@pytest.mark.parametrize("original,original_in_backup,copy,marker", _CASES)
def test_copy_is_feature_superset(original, original_in_backup, copy, marker):
    src = _inventory(_read(original, backup=original_in_backup))
    dst = _inventory(_read(copy))

    missing = {}
    for kind, old in src.items():
        lost = sorted(old - dst[kind])
        if lost:
            missing[kind] = lost

    assert not missing, (
        "в новой странице %s пропали элементы классической %s: %s"
        % (copy, original, missing)
    )


@pytest.mark.parametrize("original,original_in_backup,copy,marker", _CASES)
def test_copy_has_no_page_local_theme_palette(original, original_in_backup, copy, marker):
    """Палитры тем должны приходить из design.css/theme.js, а не из страницы.

    Если в копии снова появится `body.dark { --bg: … }`, пользовательская
    схема и акцент перестанут применяться — это и проверяем.
    """
    dst = _read(copy)
    palette = re.search(
        r"\nbody\.(?:light|dark|sepia|warm|paper|ocean|forest|midnight|rose)\s*\{",
        dst,
    )
    assert palette is None, "в копии осталась локальная палитра темы: %r" % (
        palette.group(0).strip() if palette else "",
    )
