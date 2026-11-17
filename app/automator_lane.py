"""Лента шагов: Python-код макроса -> карточки для интерфейса.

Главная причина выбрать Python под макросы: дерево даёт сам `ast`, своего
парсера и сериализатора нет. Здесь только разбор по шагам и человекочитаемая
подпись каждого шага — текст макроса остаётся источником истины, ленту из него
можно перестроить в любой момент (и правки в ленте — это правки строк кода).

Незнакомые вызовы подписываются исходной строкой, а не «неизвестный шаг»:
макрос может использовать обычный Python, и интерфейс обязан это показывать
честно.
"""

import ast
import swallow

# Подписываемые вызовы: (объект, метод) -> функция подписи от аргументов.
def _label_for(node):
    """Описание одного Call-шага или None, если описание — сам код."""
    target = _callee(node)
    if not target:
        return None
    object_name, method = target
    args = node.args
    text = _literal_text(args[0]) if args else ""

    if object_name == "k":
        if method == "press":
            return "клавиши: %s" % (text or "?")
        if method == "down":
            return "зажать: %s" % (text or "?")
        if method == "up":
            return "отжать: %s" % (text or "?")
        if method == "text":
            return "текст: «%s»" % _short(text, 40)
        if method == "release":
            return "отпустить модификаторы"
    if object_name == "m":
        if method == "click":
            return "клик%s" % _coords(args)
        if method in ("double", "right", "middle"):
            return "%s клик%s" % ({"double": "двойной", "right": "правый",
                                   "middle": "средний"}[method], _coords(args))
        if method == "move":
            return "курсор%s" % _coords(args)
        if method == "drag":
            return "тянуть%s →%s" % (_coords(args[:2]), _coords(args[2:4]))
        if method == "wheel":
            return "колесо: %s" % _literal_text(args[0]) if args else "колесо"
        if method == "pos":
            return "координаты курсора"
    if object_name == "w":
        if method in ("exists", "wait", "activate"):
            verb = {"exists": "проверить окно", "wait": "ждать окно",
                    "activate": "активировать окно"}[method]
            return "%s «%s»" % (verb, _short(text, 30))
        if method == "title":
            return "заголовок активного окна"
        if method == "center":
            return "центр окна «%s»" % _short(text, 30)
        if method == "list":
            return "список окон"
    if object_name == "clip":
        return "буфер: %s" % ("прочитать" if method == "get" else "записать")
    if object_name == "shot":
        if method == "pixel":
            return "цвет пикселя%s" % _coords(args)
        if method == "capture":
            return "снимок экрана"
        if method == "image":
            return "экран как изображение"
    if object_name == "app":
        return "запрос к приложению %s: %s" % (method.upper(), _short(text, 30))
    if object_name == "ui":
        if method == "step":
            return "шаг: %s" % _short(text, 40)
        if method == "log":
            return "в журнал: %s" % _short(text, 40)
        if method == "beep":
            return "сигнал"
    if object_name == "env" and method in ("locked", "cursor", "title"):
        return "состояние экрана: %s" % method
    if method == "sleep":
        return "пауза %s c" % text
    if method == "stop":
        return "остановить макрос"
    if method == "print":
        return "напечатать в журнал"
    return None


def _callee(node):
    """Call -> ("k", "press") для вызова атрибута или ("", "sleep") для функции."""
    function = node.func
    if isinstance(function, ast.Attribute):
        owner = function.value
        if isinstance(owner, ast.Name):
            return owner.id, function.attr
        return "", function.attr
    if isinstance(function, ast.Name):
        return "", function.id
    return None


def _literal_text(node):
    if node is None:
        return ""
    if isinstance(node, ast.Constant):
        return "" if node.value is None else str(node.value)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            parts.append(value.value if isinstance(value, ast.Constant) else "{…}")
        return "".join(parts)
    if isinstance(node, ast.UnaryOp):
        # -2 парсится как унарный минус над 2, а не как константа -2.
        sign = {ast.USub: "-", ast.UAdd: "+"}.get(type(node.op), "")
        return "%s%s" % (sign, _literal_text(node.operand))
    if isinstance(node, ast.BinOp):
        return "%s…" % _literal_text(node.left)
    return ""


def _short(value, limit):
    value = str(value or "")
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _coords(args):
    """Первые два числовых аргумента -> « в 100,200»."""
    numbers = []
    for argument in args[:2]:
        try:
            numbers.append(str(int(float(_literal_text(argument)))))
        except ValueError:
            swallow.note('automator_lane._coords:L134')
            return ""
    if len(numbers) == 2:
        return " в %s,%s" % (numbers[0], numbers[1])
    return ""


def _first(node):
    """Текст подвыражения (условие, итерируемое) из самого дерева.

    `ast.unparse` восстановит узел как Python-код без исходных строк — лента
    строится по дереву, а не по тексту, и не обязана зависеть от разметки.
    """
    try:
        return ast.unparse(node)
    except Exception:
        swallow.note('automator_lane._first:L150')
        return ""


def _source(line, source_lines, limit=90):
    """Кусок исходника шага: для многострочных вызовов берём первую строку."""
    if not source_lines or not line or line > len(source_lines):
        return ""
    return _short(source_lines[line - 1].strip(), limit)


def lane(source: str) -> dict:
    """Код -> {"ok", "error", "steps": [...]}.

    Разбор по верхним операторам, вложенность — на один уровень тела цикла/if:
    лента обязана быть понятной, а не полным деревом. Ошибку синтаксиса отдаём
    одной строкой с номером: редактор показывает её вместо карточек.
    """
    text = str(source or "")
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        swallow.note('automator_lane.lane:L173', error)
        return {"ok": False,
                "error": "строка %s: %s" % (error.lineno or "?", error.msg),
                "steps": []}
    steps = []
    _walk_block(tree.body, 0, lines, steps)
    return {"ok": True, "error": "", "steps": steps, "count": len(steps)}


def _walk_block(body, indent, lines, steps):
    for node in body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            label = _label_for(node.value)
            code = _source(node.lineno, lines)
            steps.append({"line": node.lineno, "kind": "step", "indent": indent,
                          "label": label or code, "code": code})
            continue
        if isinstance(node, ast.Assign):
            names = ", ".join(_target_name(target) for target in node.targets)
            steps.append({"line": node.lineno, "kind": "assign", "indent": indent,
                          "label": "переменная: %s" % names,
                          "code": _source(node.lineno, lines)})
            continue
        if isinstance(node, (ast.For, ast.AsyncFor)):
            steps.append({"line": node.lineno, "kind": "block", "indent": indent,
                          "label": "цикл по %s" % _short(_first(node.iter), 60),
                          "code": _source(node.lineno, lines)})
            _walk_block(node.body, indent + 1, lines, steps)
            continue
        if isinstance(node, (ast.While,)):
            steps.append({"line": node.lineno, "kind": "block", "indent": indent,
                          "label": "пока: %s" % _short(_first(node.test), 60),
                          "code": _source(node.lineno, lines)})
            _walk_block(node.body, indent + 1, lines, steps)
            continue
        if isinstance(node, ast.If):
            steps.append({"line": node.lineno, "kind": "block", "indent": indent,
                          "label": "если: %s" % _short(_first(node.test), 60),
                          "code": _source(node.lineno, lines)})
            _walk_block(node.body, indent + 1, lines, steps)
            if node.orelse:
                steps.append({"line": node.orelse[0].lineno, "kind": "block",
                              "indent": indent, "label": "иначе",
                              "code": "else"})
                _walk_block(node.orelse, indent + 1, lines, steps)
            continue
        if isinstance(node, (ast.Try,)):
            steps.append({"line": node.lineno, "kind": "block", "indent": indent,
                          "label": "попытка / ошибка", "code": "try"})
            _walk_block(node.body, indent + 1, lines, steps)
            for handler in node.handlers:
                steps.append({"line": handler.lineno, "kind": "block", "indent": indent,
                              "label": "при ошибке", "code": "except"})
                _walk_block(handler.body, indent + 1, lines, steps)
            if node.finalbody:
                _walk_block(node.finalbody, indent + 1, lines, steps)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            steps.append({"line": node.lineno, "kind": "import", "indent": indent,
                          "label": "импорт: %s" % _source(node.lineno, lines),
                          "code": _source(node.lineno, lines)})
            continue
        if isinstance(node, ast.FunctionDef):
            steps.append({"line": node.lineno, "kind": "block", "indent": indent,
                          "label": "функция %s()" % node.name, "code": "def"})
            _walk_block(node.body, indent + 1, lines, steps)
            continue
        if isinstance(node, ast.With):
            steps.append({"line": node.lineno, "kind": "block", "indent": indent,
                          "label": "с контекстом", "code": _source(node.lineno, lines)})
            _walk_block(node.body, indent + 1, lines, steps)
            continue
        # Комментарий-строка и прочее: показываем код как есть.
        code = _source(getattr(node, "lineno", None), lines)
        if code:
            steps.append({"line": getattr(node, "lineno", None), "kind": "other",
                          "indent": indent, "label": code, "code": code})


def _target_name(target):
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Tuple):
        return "(".join(_target_name(e) for e in target.elts) + ")"
    return _short(_first(target), 40) or "?"
