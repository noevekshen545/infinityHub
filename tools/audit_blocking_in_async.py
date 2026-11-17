"""Инвентарь: блокирующие вызовы внутри async-эндпоинтов, включая транзитивные.

Почему это важно: `async def` выполняется ПРЯМО на потоке event loop. Любой
синхронный syscall в таком обработчике (соккет, sleep, subprocess, копирование
файлов) останавливает обслуживание ВСЕХ остальных запросов — так выглядит
«ватный» отклик при нулевой загрузке CPU. Обычные `def`-эндпоинты FastAPI
крутит в тредпуле, они не в счёт.

Главная ловушка — не прямые `time.sleep`, а async-эндпоинт, который зовёт
синхронную helper-функцию, а та уже держит сокет (именно так /status на
0.8 с вешал весь цикл). Поэтому сначала считаем «грязные» функции по всему
дереву (fixpoint), потом ищем async-функции, которые в них попадают.

Скрипт только читает код — ничего не меняет.
"""
import ast
import os
import sys

BLOCKING = {
    "time.sleep", "socket.create_connection", "socket.gethostbyname",
    "socket.socket", "urllib.request.urlopen", "urlopen",
    "subprocess.run", "subprocess.Popen", "subprocess.check_output",
    "subprocess.check_call", "os.system", "os.startfile",
    "requests.get", "requests.post", "requests.request",
}
# только то, что гарантированно стоит десятки/сотни миллисекунд: сеть,
# процессы, деревья файлов. Локальные sqlite/open/json.load исключены
# намеренно: с ними отчёт тонет в сотнях дешёвых эндпоинтов (замерено:
# 631 подозреваемых из 5573 функций, из них 359 — просто _get_db).
BLOCKING_PREFIX = ("shutil.copy", "shutil.rmtree", "shutil.move",
                   "os.remove", "os.replace", "os.startfile", "os.system")
# Замеренные проекдные «тяжеловесы»: примитивы тут те же, что выше, но вызываются
# они через своих обёрткy, которую прямой разбор имён не видит. Без этого списка
# инструмент пропустил все десять админ-хендлеров app/routes/recovery.py, где
# list_backups()/dir_size() стояли прямо на event loop (замерено 554 и 839 мс).
# Уточнение: дешёвые варианты вроде list_backups(with_integrity=False)=27 мс
# инструмент не различает — совпадение надо смотреть руками.
HEAVY = {"rc.list_backups", "rc.dir_size", "rc.check_integrity",
         "rc.backups_stats", "rc.core_status", "rc.prune_backups",
         "rc.maybe_snapshot", "rc.create_snapshot", "rc.verify_snapshot",
         "rc.delete_snapshot", "rc.diagnose", "rs.restore_to_slot"}
# заведомо не блокирующие «похожие» имена
SAFE = {"asyncio.sleep", "asyncio.to_thread", "anyio.to_thread",
        "run_in_executor", "to_thread"}
SKIP_DIRS = {"binaries", "static", "hermes_module", "node_modules",
             "trash", "backups", "__pycache__", "dist", "venv"}


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class Visitor(ast.NodeVisitor):
    """Собирает из функции: какие имена она зовёт и есть ли прямой блокирующий вызов."""

    def __init__(self):
        self.calls = set()
        self.direct = set()
        self.root = None

    def visit_FunctionDef(self, node):
        # тело вложенной функции — отдельная запись в funcs; относить её
        # блокировки к внешнему эндпоинту неверно (замерено на примере
        # startup_news_cache_warm: time.sleep стоит внутри _warm, который
        # уходит в поток, а не на event loop). Само рассматриваемое тело —
        # не «вложенное», оно обязано разбираться.
        if node is self.root:
            self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        name = dotted(node.func) or (
            node.func.id if isinstance(node.func, ast.Name) else None)
        wrapped = False
        if name:
            self.calls.add(name)
            if name in SAFE:
                # аргументы to_thread/run_in_executor выполняются не на event
                # loop, поэтому их вложенные блокирующие вызовы не подозрение
                wrapped = True
            elif name in BLOCKING or name in HEAVY:
                self.direct.add(name)
            elif name.endswith(".sleep") and name != "asyncio.sleep":
                self.direct.add(name)
            elif any(name.startswith(p) for p in BLOCKING_PREFIX):
                self.direct.add(name)
        if wrapped:
            return
        self.generic_visit(node)


def parse(path):
    try:
        src = open(path, encoding="utf-8-sig").read()
        return ast.parse(src)
    except Exception as e:
        return e


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    why = None
    for a in sys.argv[1:]:
        if a.startswith("--why"):
            why = a.split("=", 1)[1] if "=" in a else None
    root = args[0] if args else "app"
    if os.path.isfile(root):
        # один файл: os.walk по файлу молчит, поэтому отдаём его сами
        def walk():
            yield (os.path.dirname(root) or ".",
                   [d for d in SKIP_DIRS],       # сам файл проверяем, его потом вырежет срез
                   [os.path.basename(root)])
    else:
        def walk():
            return os.walk(root)
    funcs = {}          # name -> (file, line, is_async, calls, direct)
    unparsed = []
    for dirpath, dirnames, filenames in walk():
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dirpath, f)
            tree = parse(p)
            if isinstance(tree, Exception):
                unparsed.append(f"{p}: {tree}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    v = Visitor()
                    v.root = node
                    v.visit(node)
                    funcs.setdefault((p, node.name), []).append(
                        (node.lineno, isinstance(node, ast.AsyncFunctionDef),
                         v.calls, v.direct))

    # Ключ — (файл, имя). Раньше ключом было только имя, и из-за этого
    # имя, совпадающее в двух модулях, «заражало» весь проект: 20 bookgen-
    # эндпоинтов числились блокирующими из-за однофамильца get_books_dir из
    # server/config.py. Разрешаем вызов так же, как это делает интерпретатор:
    # сначала функция этого же файла, иначе — единственный однофамилец в
    # проекте; если их несколько и выбор неоднозначен, считаем грязным любой
    # (консервативно) и относим в счётчик неоднозначных.
    by_short = {}
    for (p, name) in funcs:
        by_short.setdefault(name, []).append((p, name))

    def resolve(caller_file, call):
        """Куда ведёт вызов из функции файла caller_file.

        Точечные имена (`body.get`, `cfg.items`) не разбираем: без типов
        нельзя узнать, что за объект стоит слева, а сравнение по последнему
        сегменту давало 900 ложных срабатываний (`body.get` «находил»
        функцию get() в этом же файле). Такие вызовы считаются только если
        функция с точным именем объявлена в этом же файле.
        """
        if "." in call:
            if call.startswith("self."):
                # Метод класса: искать объявление с таким именем в этом же
                # файле — другие файлы метод принадлежать не могут.
                short = call[5:]
                return [(caller_file, short)] if (caller_file, short) in funcs else [], False
            same = [(caller_file, call)] if (caller_file, call) in funcs else []
            return same, False
        cands = by_short.get(call, [])
        same = [k for k in cands if k[0] == caller_file]
        if same:
            return same, False
        return cands, len(cands) > 1

    dirty = set()
    for key, defs in funcs.items():
        if any(direct for _, _, _, direct in defs):
            dirty.add(key)
    ambiguous = set()
    for _ in range(14):
        newly = set()
        for key, defs in funcs.items():
            if key in dirty:
                continue
            f = key[0]
            for _, _, calls, _ in defs:
                for c in calls - SAFE:
                    cands, amb = resolve(f, c)
                    if any(k in dirty for k in cands):
                        newly.add(key)
                        if amb:
                            ambiguous.add(key)
                        break
                if key in newly:
                    break
        if not newly:
            break
        dirty |= newly

    hits = []
    for (p, name), defs in funcs.items():
        for (line, is_async, calls, direct) in defs:
            if not is_async:
                continue
            bad = []
            for c in sorted(calls - SAFE):
                if c == name:
                    continue
                cands, _amb = resolve(p, c)
                if any(k in dirty for k in cands):
                    bad.append(c)
            if bad:
                hits.append(f"{p}:{line}: async {name}() -> " + ", ".join(bad[:4]))
            elif direct:
                hits.append(f"{p}:{line}: async {name}() -> {sorted(direct)[0]} (прямой)")

    print("\n".join(sorted(set(hits))))
    print(f"\nasync-эндпоинтов с блокирующим путём: {len(set(hits))}")
    print(f"всего функций в дереве: {len(funcs)}, «грязных» (блокируют): {len(dirty)}, "
          f"неоднозначных (однофамильцы): {len(ambiguous)}")
    if unparsed:
        print("не разобрано:", len(unparsed))
        for u in unparsed[:5]:
            print("   ", u)


if __name__ == "__main__":
    main()
