"""
GenLab — реестр пресетов генерации.

Пресет — это функция ``params -> {"graph": dict, "meta": dict}``, которая
собирает API-промпт ComfyUI. Описание полей для UI задаётся рядом и
используется фронтом, чтобы построить форму без дублирования разметки.

Регистрация::

    @register(
        "image.text2img",
        group="image", title="Текст → изображение",
        fields=[Field.text("prompt", "Промпт"), ...],
    )
    def text2img(params):
        ...
"""

import threading

_lock = threading.RLock()
_REGISTRY = {}

GROUPS = {
    "image": {"title": "Изображения", "icon": "fa-image", "order": 1},
    "edit": {"title": "AI-редактор", "icon": "fa-wand-magic-sparkles", "order": 2},
    "video": {"title": "Видео", "icon": "fa-clapperboard", "order": 3},
    "audio": {"title": "Аудио", "icon": "fa-wave-square", "order": 4},
    "model3d": {"title": "3D", "icon": "fa-cube", "order": 5},
}


class Field:
    """Описание одного поля формы пресета."""

    TYPES = ("text", "textarea", "number", "slider", "select", "checkbox",
             "seed", "file", "color", "hidden")

    def __init__(self, name, label, type="text", default=None, **kw):
        self.name = name
        self.label = label
        self.type = type
        self.default = default
        self.options = kw.get("options")
        self.min = kw.get("min")
        self.max = kw.get("max")
        self.step = kw.get("step")
        self.hint = kw.get("hint")
        self.placeholder = kw.get("placeholder")
        self.accept = kw.get("accept")
        self.when = kw.get("when")

    # --- сокращённые конструкторы -------------------------------------

    @staticmethod
    def text(name, label, default="", **kw):
        return Field(name, label, "text", default, **kw)

    @staticmethod
    def textarea(name, label, default="", **kw):
        return Field(name, label, "textarea", default, **kw)

    @staticmethod
    def number(name, label, default=0, **kw):
        return Field(name, label, "number", default, **kw)

    @staticmethod
    def slider(name, label, default=0, min=0, max=1, step=0.01, **kw):
        return Field(name, label, "slider", default, min=min, max=max, step=step, **kw)

    @staticmethod
    def select(name, label, default="", **kw):
        return Field(name, label, "select", default, **kw)

    @staticmethod
    def checkbox(name, label, default=False, **kw):
        return Field(name, label, "checkbox", bool(default), **kw)

    @staticmethod
    def seed(name="seed", label="Seed"):
        return Field(name, label, "number", -1, min=-1, max=2 ** 53, step=1)

    @staticmethod
    def file(name, label, accept="image/*", **kw):
        return Field(name, label, "file", None, accept=accept, **kw)

    @staticmethod
    def hidden(name, default=None):
        return Field(name, name, "hidden", default)

    def to_dict(self):
        return {
            "name": self.name, "label": self.label, "type": self.type,
            "default": self.default, "options": self.options,
            "min": self.min, "max": self.max, "step": self.step,
            "hint": self.hint, "placeholder": self.placeholder,
            "accept": self.accept, "when": self.when,
        }


def register(preset_id, group="image", title="", description="", icon=None,
             fields=None, outputs=("image",), cost="medium"):
    """Декоратор регистрации пресета."""
    def deco(fn):
        meta = {
            "id": preset_id,
            "group": group,
            "group_title": (GROUPS.get(group) or {}).get("title", group),
            "title": title or preset_id,
            "description": description or (fn.__doc__ or "").strip().split("\n")[0],
            "icon": icon or (GROUPS.get(group) or {}).get("icon", "fa-cube"),
            "outputs": list(outputs),
            "cost": cost,          # low | medium | high — для подсказок по VRAM
            "fields": [f.to_dict() for f in (fields or [])],
            "fn": fn,
        }
        with _lock:
            _REGISTRY[preset_id] = meta
        return fn
    return deco


def get(preset_id):
    """Пресет по id либо None."""
    _ensure_loaded()
    with _lock:
        meta = _REGISTRY.get(preset_id)
    return meta["fn"] if meta else None


def get_meta(preset_id):
    _ensure_loaded()
    with _lock:
        meta = _REGISTRY.get(preset_id)
    return public_meta(meta) if meta else None


def public_meta(meta):
    if not meta:
        return None
    out = {k: meta.get(k) for k in (
        "id", "group", "group_title", "title", "description", "icon",
        "outputs", "cost")}
    out["fields"] = [dict(f) for f in meta.get("fields") or []]
    return out


def all_presets(group=None):
    """Список пресетов для UI, сгруппированный и отсортированный."""
    _ensure_loaded()
    with _lock:
        items = [public_meta(m) for m in _REGISTRY.values()]
    if group:
        items = [p for p in items if p["group"] == group]
    items.sort(key=lambda p: (GROUPS.get(p["group"], {}).get("order", 9), p["title"]))
    return items


def groups():
    """Группы пресетов, которые реально зарегистрированы."""
    _ensure_loaded()
    with _lock:
        used = {m["group"] for m in _REGISTRY.values()}
    return [
        {"id": gid, **meta, "count": len(all_presets(gid))}
        for gid, meta in sorted(GROUPS.items(), key=lambda kv: kv[1]["order"])
        if gid in used
    ]


_LOADED = False


def _ensure_loaded():
    """Ленивый импорт модулей с пресетами. Идемпотентно и безопасно."""
    global _LOADED
    with _lock:
        if _LOADED:
            return
        _LOADED = True
    for name in ("image", "video", "audio", "model3d"):
        try:
            __import__("%s.%s" % (__name__, name))
        except Exception as e:  # один сломанный модуль не роняет остальные
            import logging
            logging.getLogger("infinity.genlab").warning(
                f"[genlab] не удалось загрузить пресеты {name}: {e}")