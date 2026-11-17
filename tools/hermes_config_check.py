#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка и починка config.yaml агента Hermes (включая профили).

Зачем
-----
Hermes при нечитаемом config.yaml не падает: он пишет в лог
«running on your last good settings» и работает на СТАРЫХ настройках. Снаружи
это выглядит как «поменял модель/провайдера — ничего не изменилось», а причина
видна только в логе.

Такое уже случалось дважды (файлы ``config.yaml.corrupt-*`` в каталоге Hermes).
Дефект один и тот же: у ключа стоит пустое значение, а само значение осталось
на следующей строке — валидный YAML так не пишется::

    model:
      default: ""
        E:\\models\\Qwen3.8.gguf

Проверка профилей
-----------------
У Hermes несколько конфигов: базовый (``HERMES_HOME/config.yaml``) и по
одному на именованный профиль (``HERMES_HOME/profiles/<name>/config.yaml``).
Ошибка в профиле не видна из базового файла, поэтому обходим все.

Запуск
------
    python tools/hermes_config_check.py
    python tools/hermes_config_check.py --fix
    python tools/hermes_config_check.py --root "D:\\portable\\.hermes-portable" --json

``--fix`` правит ТОЛЬКО известный дефект (пустое значение + продолжение) и
пишет файл лишь после успешного разбора результата; исходник сохраняется
рядом как ``config.yaml.corrupt-<дата>``. Остальные ошибки не трогаются:
лучше показать их человеку, чем угадывать намерение.

Код возврата: 0 — все конфиги читаются (или починены), 1 — остались ошибки.
"""

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP_DIR = os.path.join(ROOT, "app")

DEFAULT_ROOTS = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes"),
    os.path.join(APP_DIR, "hermes_module", ".hermes-portable"),
]


def _load_yaml(text):
    import yaml
    return yaml.safe_load(text)


def _parse_ok(text):
    try:
        _load_yaml(text)
        return True
    except Exception:
        return False


def _repair(text):
    """Починить ``key: ""`` + значение на следующей строке (см. docstring)."""
    sys.path.insert(0, APP_DIR)
    from hermes_module.integration import provider
    return provider.repair_folded_empty_scalars(text)


def _configs(root):
    """Все config.yaml профиля: базовый + profiles/<name>/config.yaml."""
    base = os.path.join(root, "config.yaml")
    if os.path.isfile(base):
        yield base, "default"
    profiles = os.path.join(root, "profiles")
    if not os.path.isdir(profiles):
        return
    for name in sorted(os.listdir(profiles)):
        path = os.path.join(profiles, name, "config.yaml")
        if os.path.isfile(path):
            yield path, name


def _describe(data):
    if not isinstance(data, dict):
        return {}
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    return {
        "model": model.get("default") or "",
        "provider": model.get("provider") or "",
    }


def check_file(path, fix=False):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    result = {"path": path, "ok": _parse_ok(text), "fixed": False}
    try:
        result.update(_describe(_load_yaml(text)))
    except Exception as exc:
        result["error"] = str(exc).splitlines()[0]
    if result["ok"] or not fix:
        return result

    repaired = _repair(text)
    if not _parse_ok(repaired):
        result["error"] = (result.get("error", "") +
                           " | автоматическая починка не помогла").strip(" |")
        return result

    backup = path + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(repaired)
    result.update({"ok": True, "fixed": True, "backup": backup,
                   "error": None})
    result.update(_describe(_load_yaml(repaired)))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверка config.yaml Hermes")
    parser.add_argument("--root", action="append", default=None,
                        help="каталог HERMES_HOME (можно несколько раз)")
    parser.add_argument("--fix", action="store_true",
                        help="починить известный дефект пустого значения")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    args = parser.parse_args(argv)

    roots = args.root or [r for r in DEFAULT_ROOTS if r and os.path.isdir(r)]
    if not roots:
        print("Не найдено ни одного каталога Hermes. Укажите --root.",
              file=sys.stderr)
        return 1

    report = []
    for root in roots:
        for path, profile in _configs(root):
            item = check_file(path, fix=args.fix)
            item["profile"] = profile
            report.append(item)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report:
            state = "OK"
            if item["fixed"]:
                state = "ИСПРАВЛЕН"
            elif not item["ok"]:
                state = "ОШИБКА"
            print(f"[{state}] {item['path']}")
            print(f"         профиль: {item['profile']}, "
                  f"модель: {item.get('model') or '—'}, "
                  f"провайдер: {item.get('provider') or '—'}")
            if item.get("error"):
                print(f"         {item['error']}")
            if item.get("backup"):
                print(f"         копия: {item['backup']}")
        bad = [i for i in report if not i["ok"]]
        print(f"\nВсего конфигов: {len(report)}, ошибок: {len(bad)}")
        if bad:
            print("Строки с ошибками — в выводе выше. Правьте вручную или "
                  "повторите с --fix.")

    return 0 if all(i["ok"] for i in report) else 1


if __name__ == "__main__":
    sys.exit(main())
