"""Дифференциальный тест читалок индекса профилей: старое против снимка.

Сравнивает get_active_id / resolve_profile_id / profile_exists (и
access_module.get_profile_access) с версиями из снапшота, на одном корпусе
фикстур индекса. Оба варианта работают с ОДНИМ и тем же состоянием модуля:
файловая часть (os.stat, чтение json) заглушена, потому что она была ровно
тем, что мы убрали с горячего пути.

Каждая фикстура прогоняется дважды: со свежим `_snapshot_ts` (снимок отдаёт
кэш, stat не трогается) и с истёкшим (снимок идёт через _load_index, то есть
через stat и deepcopy). Это две разные ветки, и обе обязаны давать тот же
ответ, что и прежний код.

Отдельно проверяется, что читатели не мутируют кэш: `_index_snapshot` отдаёт
тот же объект, а не копию, и правка результата вызывающим кодом испортила бы
ответы всем последующим запросам.

    python tools/profile_read_parity.py
"""
import ast
import builtins
import copy
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

import profiles_module as PM        # noqa: E402
import access_module as AM          # noqa: E402

REF_BACKUP = os.path.join(ROOT, "backups", "pre_profilecache_20261024-192944",
                          "profiles_module.py")
FIXED_KEY = (1_700_000_000_000_000_000, 4096)


def extract(path, names):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    out = []
    for node in ast.parse(src).body:
        if getattr(node, "name", None) in names:
            out.append(ast.get_source_segment(src, node))
    return "\n".join(out)


class _Fallback(dict):
    """Свой слой + живые имена модуля: заглушки, поставленные в PM, видны и
    эталонным функциям, иначе они читали бы настоящий диск.

    Имя, которого нет в PM, ищется в builtins: exec-код эталона живёт в этом
    словаре и без этого не нашёл бы any(). Порядок важен — сначала PM, чтобы
    опечатка в имени функции модуля осталась ошибкой, а не тихо подменилась
    встроенной.
    """

    def __missing__(self, key):
        try:
            return getattr(PM, key)
        except AttributeError:
            return getattr(builtins, key)


DEFAULT = "Общедоступный"
FIXTURES = [
    {"profiles": [{"id": "a", "name": DEFAULT}, {"id": "b", "name": "B"}],
     "active": "a"},
    {"profiles": [{"id": "a", "name": DEFAULT, "blocked": True}, {"id": "b"}],
     "active": "a"},
    {"profiles": [], "active": None},
    {"profiles": [{"id": "a", "name": DEFAULT}], "active": "ghost"},
    {"profiles": [{"id": "a", "name": DEFAULT}, {"id": "a", "name": "dup"}],
     "active": "a"},
    {"profiles": [{"name": DEFAULT}, {"id": "c"}], "active": "c"},
    {"profiles": [{"id": "a", "name": DEFAULT}, {"id": "b", "blocked": True},
                  {"id": "d", "role": "admin", "blocked_modules": ["notes"]}],
     "active": "d"},
    {"profiles": [{"id": "a", "blocked": True}, {"id": "b", "blocked": True}],
     "active": "a"},
    {"profiles": [{"id": "b"}, {"id": "a", "name": DEFAULT}], "active": "b"},
    {"profiles": [{"id": "a", "name": DEFAULT, "groups": ["g1"]}], "active": "a"},
]
CANDIDATES = ["a", "b", "c", "d", "zz", "", None]
EXIST_QUERIES = ["a", "b", "c", "d", "zz", "", None]


def install_fixtures(fixture, fresh=True, extra_ns=()):
    """Состояние модуля = фикстура; файл и stat не трогаются.

    fresh=False роняет `_snapshot_ts` в прошлое, и снимок идёт через
    _load_index — та же ветка, что в живом процессе при истёкшем TTL.
    """
    PM._index_cache = fixture
    PM._index_cache_key = FIXED_KEY
    PM._snapshot_ts = time.monotonic() if fresh else 0.0
    for ns in extra_ns:
        # эталонный get_active_id держит собственное мемо, и запись ложится в
        # exec-словарь — обнулять его нужно на каждой фикстуре, иначе второй
        # прогон вернул бы ответ от предыдущей
        if "_active_id_cache" in ns:
            ns["_active_id_cache"] = None


def use_file_stubs():
    """Заглушки файловой части: возвращают фикстуру вместо чтения диска."""
    def _file_key(path):
        return FIXED_KEY

    def _read_json(path, default=None):
        return copy.deepcopy(PM._index_cache)

    def _ensure_dir():
        return None

    PM._file_key = _file_key
    PM._read_json = _read_json
    PM._ensure_dir = _ensure_dir


ORIGINALS = {k: getattr(PM, k) for k in ("_file_key", "_read_json", "_ensure_dir")}


def call(fn, *args):
    """Значение ИЛИ исключение — сравниваем исход, а не только успех.

    Без этого фикстура с профилем без ключа "id" уронила бы весь тест: и
    эталон, и новый код падают на ней с одинаковым KeyError (`p["id"]`), а
    различать «совпало» и «упало раньше, чем дошло до сравнения» надо.
    """
    try:
        return ("ok", fn(*args))
    except Exception as e:
        return ("exc", type(e).__name__, str(e))


def main():
    if not os.path.exists(REF_BACKUP):
        raise SystemExit(f"нет эталонного снапшота {REF_BACKUP}")
    names = {"get_active_id", "resolve_profile_id", "profile_exists"}
    ref_ns = _Fallback({"__builtins__": builtins})
    exec(compile(extract(REF_BACKUP, names), "<ref>", "exec"), ref_ns)
    # в модуле-этлоне мемо объявлено на уровне файла; из вырезанных функций мы
    # его получили только как global-ссылку, поэтому заводим вручную
    ref_ns["_active_id_cache"] = None
    use_file_stubs()

    problems = []
    checks = 0
    for i, fixture in enumerate(FIXTURES):
        for fresh in (True, False):
            tag = f"Fixture#{i} {'свежий' if fresh else 'истёкший'}"

            install_fixtures(copy.deepcopy(fixture), fresh=fresh, extra_ns=(ref_ns,))
            ref_active = call(ref_ns["get_active_id"])
            live_active = call(PM.get_active_id)
            checks += 1
            if ref_active != live_active:
                problems.append(f"{tag} get_active_id: {ref_active!r} != {live_active!r}")

            for cand in CANDIDATES:
                install_fixtures(copy.deepcopy(fixture), fresh=fresh, extra_ns=(ref_ns,))
                r = call(ref_ns["resolve_profile_id"], cand)
                install_fixtures(copy.deepcopy(fixture), fresh=fresh, extra_ns=(ref_ns,))
                n = call(PM.resolve_profile_id, cand)
                checks += 1
                if r != n:
                    problems.append(f"{tag} resolve({cand!r}): эталон {r!r}, теперь {n!r}")

            for q in EXIST_QUERIES:
                install_fixtures(copy.deepcopy(fixture), fresh=fresh, extra_ns=(ref_ns,))
                r = call(ref_ns["profile_exists"], q)
                install_fixtures(copy.deepcopy(fixture), fresh=fresh, extra_ns=(ref_ns,))
                n = call(PM.profile_exists, q)
                checks += 1
                if r != n:
                    problems.append(f"{tag} profile_exists({q!r}): {r!r} != {n!r}")

            # доступ профиля (роль/группы/заблокированные) — тоже с обоих вариантов
            for pid in ("a", "b", "d", "zz"):
                install_fixtures(copy.deepcopy(fixture), fresh=fresh, extra_ns=(ref_ns,))
                acc_res = call(AM.get_profile_access, pid)
                checks += 1
                if acc_res[0] == "exc":
                    problems.append(f"{tag} get_profile_access({pid}) бросает {acc_res[1:]}")
                    continue
                acc = acc_res[1]
                rec = next((p for p in fixture.get("profiles", []) if p.get("id") == pid),
                           None)
                expect_blocked = [m for m in ((rec or {}).get("blocked_modules") or [])
                                  if AM.is_known_module(m)]
                expect_groups = list((rec or {}).get("groups") or [])
                role = ((rec or {}).get("role") or "user").strip().lower()
                if role not in AM.VALID_ROLES:
                    role = "user"
                expect_admin = role == "admin"
                if (acc["blocked_modules"] != expect_blocked
                        or acc["groups"] != expect_groups
                        or acc["is_admin"] != expect_admin
                        or acc["role"] != role):
                    problems.append(
                        f"{tag} get_profile_access({pid}): "
                        f"получено {acc}, ожидалось role={role} groups={expect_groups} "
                        f"blocked={expect_blocked} is_admin={expect_admin}")

            # читатели не должны портить кэш: снимок = тот же объект
            install_fixtures(copy.deepcopy(fixture), fresh=fresh, extra_ns=(ref_ns,))
            before = copy.deepcopy(PM._index_cache)
            for cand in CANDIDATES:
                call(PM.resolve_profile_id, cand)
                call(PM.profile_exists, cand)
                call(PM.get_active_id)
                call(AM.get_profile_access, cand)
            checks += 1
            if PM._index_cache != before:
                problems.append(f"{tag}: читатели изменили кэш индекса")

    # запись индекса видна сразу, не дожидаясь TTL (имитация _save_index,
    # который присваивает _index_cache тем же словарём, что положил на диск)
    install_fixtures(copy.deepcopy(FIXTURES[0]), fresh=True)
    PM._index_cache = {"profiles": [{"id": "z", "name": DEFAULT}], "active": "z"}
    checks += 1
    if PM.get_active_id() != "z":
        problems.append("запись индекса не видна сразу (кэш не обновлён)")
    if PM.resolve_profile_id("zz") != "z":
        problems.append("запись индекса не видна в resolve_profile_id")

    for k, v in ORIGINALS.items():
        setattr(PM, k, v)
    PM._index_cache = None
    PM._index_cache_key = None

    print(f"проверок: {checks}, расхождений: {len(problems)}")
    for p in problems:
        print("  ", p)
    sys.exit(1 if problems else 0)


main()
