"""
GenLab — построение и валидация API-графов ComfyUI.

ComfyUI принимает промпт в формате ``{"<node_id>": {"class_type": str,
"inputs": {...}}}``, где связь между нодами — это ``["<source_id>", <slot>]``,
а виджет — обычное скалярное значение. Этот модуль даёт:

  * удобные хелперы для конструирования графа;
  * validate_graph() — статическую проверку графа по схеме /object_info.

Валидация нужна по практической причине: у пользователя может быть не
установлено ни одной модели. Проверка схемы позволяет отсеять ошибки графа
до постановки в очередь и показать в UI точный список проблем, а не
«Traceback в ноде 12».
"""

import threading
import swallow

_lock = threading.RLock()
_object_info = {"value": None, "ts": 0.0}
# Одна запись на папку: catalog.folders_state() обходит все 11 папок подряд,
# и общий слот перезаписывался каждым вызовом — кэш не попадал никогда.
_models_cache = {}
_models_failed_at = [0.0]
# Неудача тоже стоит денег: отказ соединения с ComfyUI — это ~2 с на connect.
# Запоминаем момент отказа, чтобы серия вызовов (capabilities зовёт схему
# 15 раз) не платила эти секунды каждый раз.
_object_info_failed_at = [0.0]
_OBJECT_INFO_RETRY = 5.0


# --------------------------------------------------------------------------
# Хелперы построения
# --------------------------------------------------------------------------

def ref(node_id, slot=0):
    """Ссылка на выход ноды: ref("4", 1) → ["4", 1]."""
    return [str(node_id), int(slot)]


class GraphBuilder:
    """Накопитель нод. Идентификаторы выдаются по возрастанию, как в ComfyUI."""

    def __init__(self, start=1):
        self.nodes = {}
        self._next = int(start)

    def add(self, class_type, inputs=None, node_id=None):
        nid = str(node_id) if node_id is not None else str(self._next)
        if node_id is None:
            while nid in self.nodes:
                self._next += 1
                nid = str(self._next)
        try:
            self._next = max(self._next, int(nid)) + 1
        except (TypeError, ValueError):
            swallow.note('graph.add:L57')
            pass
        self.nodes[nid] = {"class_type": class_type, "inputs": dict(inputs or {})}
        return nid

    def link(self, node_id, **inputs):
        """Дописать связи в существующую ноду."""
        node = self.nodes.get(str(node_id))
        if node is None:
            raise KeyError("Нет ноды %s" % node_id)
        node["inputs"].update(inputs)
        return str(node_id)

    def build(self):
        return dict(self.nodes)

    def __len__(self):
        return len(self.nodes)


def seed_value(seed=None, limit=2 ** 53):
    """Случайный seed, если не задан (ComfyUI не любит значения вне int64)."""
    if seed in (None, "", -1, -1.0):
        import random
        return random.randint(0, limit - 1)
    try:
        return int(seed)
    except (TypeError, ValueError):
        swallow.note('graph.seed_value:L85')
        import random
        return random.randint(0, limit - 1)


def pick_model(requested, available, hint=""):
    """Выбрать вес: запрошенный, иначе единственный установленный, иначе ''.

    Возвращает '' когда подходящих моделей нет — вызывающий код передаёт пустую
    строку в граф, а валидатор подскажет пользователю, что нужно установить.
    """
    if requested and available and requested in available:
        return requested
    if requested and not available:
        return requested
    if available:
        return available[0]
    del hint
    return ""


# --------------------------------------------------------------------------
# Схема нод
# --------------------------------------------------------------------------

def get_object_info(max_age=60.0, refresh=False):
    """Кэшированный /object_info. None — если ComfyUI недоступен."""
    from . import service
    import time

    with _lock:
        if (not refresh and _object_info["value"] is not None
                and time.time() - _object_info["ts"] < max_age):
            return _object_info["value"]
        if not refresh and _object_info_failed_at[0]:
            if time.time() - _object_info_failed_at[0] < _OBJECT_INFO_RETRY:
                return None

    if not refresh and service.fast_unreachable():
        with _lock:
            _object_info_failed_at[0] = time.time()
        return None

    try:
        data = service.client().object_info(max_age=max_age)
    except Exception as e:
        from .comfy_client import _logger
        _logger().debug(f"[genlab] object_info недоступен: {e}")
        with _lock:
            _object_info_failed_at[0] = time.time()
        return None

    if isinstance(data, dict) and data:
        with _lock:
            _object_info["value"] = data
            _object_info["ts"] = time.time()
            _object_info_failed_at[0] = 0.0
    return data


def get_models(max_age=60.0, folder=None):
    """Список весов ComfyUI (с кэшем по папке). None — если недоступно."""
    from . import service
    import time

    key = folder or "*"
    with _lock:
        cached = _models_cache.get(key)
        if cached and time.time() - cached.get("ts", 0) < max_age:
            return cached.get("value")
        if _models_failed_at[0] and time.time() - _models_failed_at[0] < _OBJECT_INFO_RETRY:
            # Каталог спрашивает ~11 папок подряд: без этого каждый отказ
            # ComfyUI стоил бы свои ~2 с ожидания соединения.
            return None

    if service.fast_unreachable():
        with _lock:
            _models_failed_at[0] = time.time()
        return None

    try:
        cli = service.client()
        value = cli.models(folder) if folder else cli.models()
    except Exception:
        swallow.note('graph.get_models:L169')
        with _lock:
            _models_failed_at[0] = time.time()
        return None
    with _lock:
        _models_cache[key] = {"ts": time.time(), "value": value}
        _models_failed_at[0] = 0.0
    return value


def invalidate_caches():
    with _lock:
        _object_info["value"] = None
        _object_info["ts"] = 0.0
        _object_info_failed_at[0] = 0.0
        _models_cache.clear()
        _models_failed_at[0] = 0.0


# --------------------------------------------------------------------------
# Валидация
# --------------------------------------------------------------------------

def _problem(severity, code, message, node_id=None, input_name=None, hint=None):
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "node_id": node_id,
        "input": input_name,
        "hint": hint,
    }


def _input_spec(info, name):
    spec = ((info.get("input") or {}).get("required") or {}).get(name)
    if spec is None:
        spec = ((info.get("input") or {}).get("optional") or {}).get(name)
    return spec


def _spec_options(spec):
    """Список допустимых значений виджета или None.

    Пустой список (модели не установлены, COMBO без источника) трактуется как
    «варианты неизвестны» — иначе валидатор ругался бы на всё подряд.
    """
    if isinstance(spec, (list, tuple)) and spec:
        head = spec[0]
        if isinstance(head, (list, tuple)):
            return [str(x) for x in head] or None
    return None


def _spec_meta(spec):
    if isinstance(spec, (list, tuple)) and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1]
    return {}


def _is_link(value):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and isinstance(value[0], (str, int))
            and isinstance(value[1], (int, float))
            and not isinstance(value[1], bool))


# Поля виджетов, значения которых — имена файлов моделей ComfyUI.
MODEL_INPUTS = {
    "ckpt_name": "checkpoints",
    "unet_name": "diffusion_models",
    "vae_name": "vae",
    "clip_name1": "text_encoders",
    "clip_name2": "text_encoders",
    "clip_name": "text_encoders",
    "lora_name": "loras",
    "control_net_name": "controlnet",
    "style_model_name": "style_models",
    "upscale_model_name": "upscale_models",
    "clip_vision_name": "clip_vision",
    "gligen_name": "gligen",
    "embedding": "embeddings",
    "audio_encoder": "audio_encoders",
    "background_removal_model": "background_removal",
    "frame_interpolation_model": "frame_interpolation",
    "audio": "input",          # LoadAudio: имя загруженного файла
    "image": "input",          # LoadImage
}


def validate_graph(graph, object_info=None, check_models=True):
    """Статически проверить API-промпт. Возвращает список проблем.

    Пустой список = граф корректен. Если ComfyUI недоступен и object_info не
    передан, возвращается одно предупреждение вместо ошибок.
    """
    problems = []
    if not isinstance(graph, dict) or not graph:
        return [_problem("error", "empty_graph", "Граф пустой")]

    if object_info is None:
        object_info = get_object_info()

    if not object_info:
        problems.append(_problem(
            "warning", "no_schema",
            "Схема нод недоступна (ComfyUI не отвечает) — граф не проверен",
            hint="Запустите ComfyUI и обновите схему",
        ))
        return problems

    known_models = {}
    if check_models:
        known_models = _load_model_names()

    for node_id, node in graph.items():
        if not isinstance(node, dict):
            problems.append(_problem("error", "bad_node",
                                     "Нода %s: ожидался объект" % node_id, node_id))
            continue
        class_type = node.get("class_type")
        if not class_type:
            problems.append(_problem("error", "no_class",
                                     "Нода %s: не указан class_type" % node_id, node_id))
            continue
        info = object_info.get(class_type)
        if info is None:
            problems.append(_problem(
                "error", "unknown_node",
                "Нода %s: класс «%s» не найден в ComfyUI" % (node_id, class_type),
                node_id,
                hint="Обновите список нод или установите кастом-пак",
            ))
            continue

        inputs = node.get("inputs")
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            problems.append(_problem("error", "bad_inputs",
                                     "Нода %s: inputs должен быть объектом" % node_id,
                                     node_id))
            continue

        required = ((info.get("input") or {}).get("required") or {})
        optional = ((info.get("input") or {}).get("optional") or {})

        for name in required:
            if name not in inputs or inputs[name] in (None, ""):
                problems.append(_missing_input_problem(
                    node_id, class_type, name, known_models))

        for name, value in inputs.items():
            spec = _input_spec(info, name)
            if name not in required and name not in optional:
                problems.append(_problem(
                    "warning", "unknown_input",
                    "Нода %s (%s): вход «%s» не описан в схеме" % (node_id, class_type, name),
                    node_id, name,
                ))
                continue
            if _is_link(value):
                problems.extend(_check_link(graph, node_id, name, value, object_info))
                continue
            problems.extend(_check_value(class_type, node_id, name, value, spec,
                                        known_models, info))

    problems.extend(_check_terminals(graph, object_info))
    return problems


MISSING_HINTS = {
    "ckpt_name": ("Не выбран чекпоинт",
                  "Установите вес в ComfyUI/models/checkpoints "
                  "или укажите его в настройках GenLab"),
    "unet_name": ("Не выбрана модель (unet)",
                  "Положите файл в ComfyUI/models/diffusion_models"),
    "vae_name": ("Не выбран VAE", "Положите файл в ComfyUI/models/vae"),
    "control_net_name": ("Не выбрана модель ControlNet",
                         "Положите файл в ComfyUI/models/controlnet"),
    "upscale_model": ("Не выбрана модель апскейла",
                      "Положите файл в ComfyUI/models/upscale_models"),
    "upscale_model_name": ("Не выбрана модель апскейла",
                           "Положите файл в ComfyUI/models/upscale_models"),
    "bg_removal_name": ("Не выбрана модель удаления фона",
                        "Положите файл в ComfyUI/models/background_removal"),
    "lora_name": ("Не выбран LoRA", "Положите файл в ComfyUI/models/loras"),
    "text": ("Промпт не заполнен", "Введите описание изображения или видео"),
    "prompt": ("Промпт не заполнен", "Введите описание"),
}


def _missing_input_problem(node_id, class_type, name, known_models):
    """Понятное сообщение вместо технического «нет обязательного входа»."""
    title, hint = MISSING_HINTS.get(name, (None, None))
    if name in MODEL_INPUTS and MODEL_INPUTS[name] not in ("input",):
        folder = MODEL_INPUTS[name]
        available = known_models.get(folder)
        if available is not None and not available:
            hint = (f"В ComfyUI/models/{folder} нет ни одного файла — "
                    f"модель нужно скачать")
            title = title or f"Не выбрана модель для «{name}»"
    if title is None:
        title = "Не передан обязательный вход «%s»" % name
    return _problem("error", "missing_input",
                    "Нода %s (%s): %s" % (node_id, class_type, title),
                    node_id, name, hint)


def _check_link(graph, node_id, name, value, object_info):
    src_id, slot = str(value[0]), int(value[1])
    out = []
    if src_id not in graph:
        return [_problem(
            "error", "broken_link",
            "Нода %s: вход «%s» ссылается на несуществующую ноду %s"
            % (node_id, name, src_id),
            node_id, name,
        )]
    src = graph[src_id] or {}
    src_class = src.get("class_type")
    src_info = object_info.get(src_class) or {}
    names = src_info.get("output_name") or []
    outputs = src_info.get("output") or []
    if outputs and slot >= len(outputs):
        out.append(_problem(
            "error", "bad_slot",
            "Нода %s: вход «%s» берёт слот %s у ноды %s, а у неё выходов %s"
            % (node_id, name, slot, src_id, len(outputs)),
            node_id, name,
            hint="Ожидался %s" % ", ".join(str(n) for n in (names or outputs)),
        ))
    return out


def _check_value(class_type, node_id, name, value, spec, known_models, info):
    out = []
    options = _spec_options(spec)
    meta = _spec_meta(spec)

    if options is not None and isinstance(value, str) and value not in options:
        if len(options) <= 12:
            out.append(_problem(
                "warning", "bad_enum",
                "Нода %s (%s): «%s»=%s нет среди вариантов (%s)"
                % (node_id, class_type, name, value, ", ".join(options[:12])),
                node_id, name,
            ))
        else:
            out.append(_problem(
                "warning", "bad_enum",
                "Нода %s (%s): «%s»=%s нет среди %s доступных значений"
                % (node_id, class_type, name, value, len(options)),
                node_id, name,
            ))

    if isinstance(value, bool):
        return out

    if isinstance(value, (int, float)):
        lo = meta.get("min")
        hi = meta.get("max")
        if isinstance(lo, (int, float)) and value < lo:
            out.append(_problem(
                "warning", "below_min",
                "Нода %s (%s): «%s»=%s меньше минимума %s"
                % (node_id, class_type, name, value, lo),
                node_id, name,
            ))
        elif isinstance(hi, (int, float)) and value > hi:
            out.append(_problem(
                "warning", "above_max",
                "Нода %s (%s): «%s»=%s больше максимума %s"
                % (node_id, class_type, name, value, hi),
                node_id, name,
            ))

    folder = MODEL_INPUTS.get(name)
    if folder and isinstance(value, str) and value:
        if folder == "input":
            return out
        available = known_models.get(folder)
        if available is not None and value not in available:
            hint = "Файл не найден в ComfyUI/models/%s" % folder
            out.append(_problem(
                "warning", "model_missing",
                "Нода %s (%s): модель «%s» не установлена" % (node_id, class_type, value),
                node_id, name, hint,
            ))
    del info
    return out


def _check_terminals(graph, object_info):
    """Если нет выходных нод — результат ComfyUI не сохранит."""
    outputs = []
    for node_id, node in graph.items():
        info = object_info.get((node or {}).get("class_type")) or {}
        if info.get("output_node"):
            outputs.append(node_id)
    if not outputs:
        return [_problem(
            "error", "no_output_node",
            "В графе нет выходной ноды (SaveImage / SaveAudio / SaveGLB …) — "
            "результат не будет сохранён",
        )]
    return []


def _load_model_names():
    """{folder: [filenames]} по всем папкам моделей ComfyUI (может быть пусто)."""
    folders = get_models()
    if not isinstance(folders, list):
        return {}
    names = {}
    for folder in folders:
        if not isinstance(folder, str):
            continue
        files = get_models(folder=folder)
        if isinstance(files, list):
            names[folder] = [f for f in files if isinstance(f, str)]
    return names


def graph_summary(graph):
    """Краткое описание графа для UI (список нод с читаемыми именами)."""
    rows = []
    for node_id, node in (graph or {}).items():
        node = node or {}
        rows.append({
            "id": node_id,
            "class_type": node.get("class_type"),
            "inputs": sorted((node.get("inputs") or {}).keys()),
        })
    rows.sort(key=lambda r: (len(r["id"]), r["id"]))
    return rows


def output_names(graph):
    """Имена выходов нод, которые реально пишут файлы."""
    out = []
    for node_id, node in (graph or {}).items():
        node = node or {}
        cls = node.get("class_type") or ""
        if cls.startswith("Save") or cls in ("PreviewImage", "PreviewAudio",
                                             "Preview3D", "Preview3DAdvanced",
                                             "SaveImageWebsocket", "MeshToFile3D"):
            out.append({"id": node_id, "class_type": cls})
    return out