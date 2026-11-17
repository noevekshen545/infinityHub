"""
GenLab — обработка изображений на CPU (PIL).

Этот слой не зависит от ComfyUI: превью, миниатюры, композитинг слоёв,
маски и простые правки работают всегда. Нужен редактору, который должен
открываться даже когда генерация недоступна.
"""

import io
import os
import time
import uuid

from . import config as gconfig
from . import store
import swallow

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
    _HAS_PIL = True
except Exception:                                    # pragma: no cover
    swallow.note('imaging.<module>:L21')
    _HAS_PIL = False

MAX_SIDE = 8192


class ImageError(ValueError):
    """Ошибка обработки изображения с понятным текстом для UI."""


def _require_pil():
    if not _HAS_PIL:
        raise ImageError(
            "Pillow не установлен в окружении InfinitySearch — обработка "
            "изображений на CPU недоступна")


def _open(path_or_asset):
    """Принять путь, объект ассета или его id."""
    _require_pil()
    if isinstance(path_or_asset, dict):
        path = path_or_asset.get("path")
    elif isinstance(path_or_asset, str) and not os.path.isfile(path_or_asset) \
            and not os.path.isabs(path_or_asset):
        # Похоже на id ассета из галереи — ищем его в БД.
        asset = store.get_asset(path_or_asset)
        path = asset.get("path") if asset else None
    else:
        path = path_or_asset
    if not path or not os.path.isfile(path):
        raise ImageError("Файл не найден: %s" % (path or "пусто"))
    try:
        img = Image.open(path)
        img.load()
    except Exception as e:
        raise ImageError("Не удалось открыть изображение: %s" % e) from e
    if max(img.size) > MAX_SIDE:
        raise ImageError("Слишком большое изображение (%dx%d, максимум %d)"
                         % (img.size[0], img.size[1], MAX_SIDE))
    # Всегда RGBA: альфа-композиция требует одинакового режима у всех слоёв,
    # а у PNG без прозрачности режим RGB — приведение здесь снимает класс ошибок.
    return img.convert("RGBA")


def _out_dir():
    d = os.path.join(gconfig.output_dir(), "edit")
    os.makedirs(d, exist_ok=True)
    return d


def _save(img, prefix="edit", register=True, meta=None):
    name = "%s-%s.png" % (prefix, time.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:4])
    path = os.path.join(_out_dir(), name)
    img.save(path, "PNG")
    if not register:
        return path, name
    asset = store.add_asset(name=name, path=path, media_type="image",
                            origin="edit", meta=meta or {})
    return path, name, asset


def make_thumbnail(src, dst, size=320):
    """Миниатюра для галереи."""
    _require_pil()
    try:
        with Image.open(src) as img:
            img = img.convert("RGB")
            img.thumbnail((int(size), int(size)), Image.LANCZOS)
            img.save(dst, "WEBP", quality=82, method=4)
        return True
    except Exception:
        swallow.note('imaging.make_thumbnail:L92')
        return False


# --------------------------------------------------------------------------
# Маски
# --------------------------------------------------------------------------

def build_mask(payload):
    """Построить маску из фигуры редактора.

    payload: {width, height, points: [[x,y],...], brush: {x, y, r}, mode:
              'polygon'|'brush', invert: bool, feather: int}
    Белым отмечается область, которую нужно перерисовать.
    """
    _require_pil()
    width = int(payload.get("width") or 0)
    height = int(payload.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ImageError("Нужны размеры маски: width и height")
    if max(width, height) > MAX_SIDE:
        raise ImageError("Слишком большая маска (%dx%d)" % (width, height))

    img = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(img)

    if payload.get("mode") == "brush" or payload.get("points") is None:
        strokes = payload.get("strokes") or []
        if payload.get("brush"):
            strokes = list(strokes) + [payload["brush"]]
        for stroke in strokes:
            cx = float(stroke.get("x") or 0)
            cy = float(stroke.get("y") or 0)
            r = max(1.0, float(stroke.get("r") or 24))
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    else:
        points = [(float(p[0]), float(p[1])) for p in (payload.get("points") or [])
                  if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(points) < 3:
            raise ImageError("Для полигона нужно минимум 3 точки")
        draw.polygon(points, fill=255)

    feather = int(payload.get("feather") or 0)
    if feather > 0:
        img = img.filter(ImageFilter.GaussianBlur(radius=feather / 2.0))

    if payload.get("invert"):
        img = ImageOps.invert(img)

    path, name, asset = _save(img, prefix="mask", register=True,
                              meta={"kind": "mask"})
    return {"path": path, "name": name, "asset": asset,
            "width": width, "height": height}


def save_mask_over_asset(asset_id, payload):
    """Раскрасить маску по размеру исходного ассета и сохранить её файлом."""
    asset = store.get_asset(asset_id)
    if not asset:
        raise ImageError("Ассет не найден")
    src = _open(asset)
    merged = payload.get("mask_base64")
    if merged:
        try:
            raw = _decode_image_payload(merged)
            mask = raw.convert("L").resize(src.size, Image.LANCZOS)
        except ImageError:
            raise
        except Exception as e:
            raise ImageError("Не удалось прочитать маску: %s" % e) from e
    else:
        mask = build_mask(dict(payload, width=src.size[0],
                               height=src.size[1]))["asset"]["path"]
        mask = _open(mask).convert("L")
    path, name, saved = _save(mask.convert("RGB"), prefix="mask")
    return {"path": path, "name": name, "asset": saved}


def _decode_image_payload(value):
    _require_pil()
    import base64
    text = value.split(",", 1)[-1] if "," in value else value
    try:
        raw = base64.b64decode(text, validate=False)
        return Image.open(io.BytesIO(raw))
    except Exception as e:
        raise ImageError("Некорректное изображение в base64: %s" % e) from e


# --------------------------------------------------------------------------
# Композитинг слоёв
# --------------------------------------------------------------------------

BLEND_MODES = ("normal", "multiply", "screen", "overlay", "difference", "add")


def compose(payload):
    """Свести слои в одно изображение.

    payload: {layers: [{asset_id|path, x, y, scale, rotation, opacity,
                        blend, visible}], width, height, background}
    Масштаб задаётся в долях от исходного размера слоя, поэтому результат
    не зависит от того, на каком экране собиралась композиция.
    """
    _require_pil()
    layers = payload.get("layers") or []
    if not layers:
        raise ImageError("Нет ни одного слоя")

    base_size = None
    if payload.get("width") and payload.get("height"):
        base_size = (int(payload["width"]), int(payload["height"]))
    elif len(layers) == 1:
        base_size = _open(_layer_source(layers[0])).size

    canvas = None
    result = None
    for index, layer in enumerate(layers):
        if not layer.get("visible", True):
            continue
        img = _open(_layer_source(layer))
        if base_size is None:
            base_size = img.size
        if canvas is None:
            width = base_size[0] or img.size[0]
            height = base_size[1] or img.size[1]
            background = payload.get("background") or "transparent"
            if background == "transparent":
                canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            else:
                canvas = Image.new("RGBA", (width, height),
                                   _parse_color(background, (255, 255, 255, 255)))
            result = canvas

        scale = float(layer.get("scale") or 1.0)
        if scale > 0 and abs(scale - 1.0) > 1e-6:
            size = (max(1, int(img.size[0] * scale)),
                    max(1, int(img.size[1] * scale)))
            img = img.resize(size, Image.LANCZOS)
        rotation = float(layer.get("rotation") or 0.0)
        if abs(rotation) > 1e-6:
            img = img.rotate(rotation, resample=Image.BICUBIC, expand=True)

        opacity = float(layer.get("opacity", 1.0))
        if opacity < 1.0:
            alpha = img.getchannel("A").point(lambda v: int(v * max(0.0, min(1.0, opacity))))
            img.putalpha(alpha)

        mode = str(layer.get("blend") or "normal").lower()
        position = (int(layer.get("x") or 0), int(layer.get("y") or 0))
        if mode in ("normal", "") or mode not in BLEND_MODES:
            result.alpha_composite(img, dest=position)
        else:
            result = _blend(result, img, position, mode, opacity)
        del index

    if result is None:
        raise ImageError("Все слои скрыты — нечего собирать")
    path, name, asset = _save(result.convert("RGBA"), prefix="compose",
                              meta={"kind": "compose", "layers": len(layers)})
    return {"path": path, "name": name, "asset": asset,
            "width": result.size[0], "height": result.size[1]}


def _layer_source(layer):
    if layer.get("asset_id"):
        asset = store.get_asset(layer["asset_id"])
        if not asset:
            raise ImageError("Слой ссылается на несуществующий ассет")
        return asset
    if layer.get("path"):
        return layer
    raise ImageError("У слоя нет ни asset_id, ни path")


def _blend(base, top, position, mode, opacity):
    """Смешивание двух RGBA-слоёв через ImageChops. Базовый слой остаётся снизу.

    Поддерживаются только режимы, которые PIL умеет считать поканально:
    нелинейные (soft light и т.п.) требуют попиксельной формулы и тянут
    за собой numpy, поэтому их здесь нет.
    """
    from PIL import ImageChops

    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.alpha_composite(top, dest=position)
    fn = {
        "multiply": ImageChops.multiply,
        "screen": ImageChops.screen,
        "overlay": ImageChops.overlay,
        "difference": ImageChops.difference,
        "add": ImageChops.add,
    }.get(mode, ImageChops.multiply)
    blended = fn(base.convert("RGB"), layer.convert("RGB"))
    if opacity < 1.0:
        blended = Image.blend(base.convert("RGB"), blended,
                              max(0.0, min(1.0, opacity)))
    out = blended.convert("RGBA")
    out.putalpha(base.getchannel("A"))
    return out


def _parse_color(value, fallback=(255, 255, 255, 255)):
    from PIL import ImageColor
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        return tuple(int(v) for v in value) + ((255,) if len(value) == 3 else ())
    try:
        rgba = ImageColor.getrgb(value)
    except Exception:
        swallow.note('imaging._parse_color:L301')
        return fallback
    return rgba if len(rgba) == 4 else rgba + (255,)


# --------------------------------------------------------------------------
# Простые правки
# --------------------------------------------------------------------------

def adjust(payload):
    """Кадрирование, поворот, отражение, яркость, контраст, резкость, размытие."""
    _require_pil()
    source = payload.get("asset_id")
    if not source and payload.get("path"):
        source = payload
    img = _open(source)

    crop = payload.get("crop") or {}
    if crop.get("w") and crop.get("h"):
        x = max(0, int(crop.get("x") or 0))
        y = max(0, int(crop.get("y") or 0))
        box = (x, y, min(img.size[0], x + int(crop["w"])),
               min(img.size[1], y + int(crop["h"])))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ImageError("Пустая область кадрирования")
        img = img.crop(box)

    resize = payload.get("resize") or {}
    if resize.get("width") or resize.get("height"):
        target = (int(resize.get("width") or img.size[0]),
                  int(resize.get("height") or img.size[1]))
        img = img.resize(target, Image.LANCZOS)

    rotation = int(payload.get("rotate") or 0) % 360
    if rotation:
        img = img.rotate(-rotation, expand=True, fillcolor=(0, 0, 0, 0))

    flip = payload.get("flip") or ""
    if flip == "horizontal":
        img = ImageOps.mirror(img)
    elif flip == "vertical":
        img = ImageOps.flip(img)

    ops = []
    if payload.get("brightness"):
        delta = int(round(float(payload["brightness"]) * 255))
        img = img.point(lambda v, d=delta: max(0, min(255, v + d)))
        ops.append("brightness=%+d" % delta)
    if payload.get("contrast"):
        factor = 1.0 + float(payload["contrast"])
        img = ImageEnhance.Contrast(img).enhance(max(0.0, factor))
        ops.append("contrast=%.2f" % factor)
    if payload.get("saturation"):
        img = ImageEnhance.Color(img).enhance(max(0.0, 1.0 + float(payload["saturation"])))
        ops.append("saturation")
    if payload.get("sharpen"):
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=90, threshold=3))
        ops.append("sharpen")
    if payload.get("blur"):
        img = img.filter(ImageFilter.GaussianBlur(float(payload["blur"])))
        ops.append("blur=%.1f" % float(payload["blur"]))
    if payload.get("grayscale"):
        img = img.convert("LA").convert("RGBA")
        ops.append("grayscale")

    path, name, asset = _save(img, prefix="adjust", meta={"ops": ops})
    return {"path": path, "name": name, "asset": asset,
            "width": img.size[0], "height": img.size[1], "ops": ops}