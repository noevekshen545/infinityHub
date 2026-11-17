"""
GenLab — пресеты изображений и AI-редактирования.

Все графы собраны только из встроенных нод ComfyUI 0.39 (кастом-паки не
нужны). Схема сверена с живым /object_info, а не с документацией.

Общие узлы конвейера CheckpointLoaderSimple → CLIPTextEncode → KSampler →
VAEDecode → SaveImage вынесены в помощники, чтобы пресеты отличались только
содержательной частью.
"""

from .. import config as gconfig
from ..graph import GraphBuilder, pick_model, ref, seed_value
from . import Field, register

DEFAULT_SAMPLER = "euler"
DEFAULT_SCHEDULER = "normal"

SAMPLERS = [
    "euler", "euler_cfg_pp", "euler_ancestral", "euler_ancestral_cfg_pp",
    "heun", "heunpp2", "dpmpp_2s_ancestral", "dpmpp_2s_ancestral_cfg_pp",
    "dpmpp_sde", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu",
    "dpmpp_3m_sde", "dpmpp_3m_sde_gpu", "dpm_2", "dpm_2_ancestral",
    "deis", "uni_pc", "uni_pc_bh2", "ddim", "ddpm", "res_multistep",
    "lms", "heun", "sa_solver",
]

SCHEDULERS = ["normal", "karras", "exponential", "sgm_uniform", "simple",
              "ddim_uniform", "beta", "linear_quadratic", "kl_optimal"]

PREPROCESSORS = [
    ("none", "Без препроцессора (подать фото как есть)"),
    ("canny", "Canny — контуры"),
    ("depth", "Depth — карта глубины"),
    ("normal", "Normal — карта нормалей"),
    ("pose", "Pose — поза (OpenPose/DWPose)"),
    ("lineart", "Lineart — контурный рисунок"),
    ("seg", "Segmentation — сегментация"),
    ("blur", "Blur — размытие (для переноса стиля)"),
]


# --------------------------------------------------------------------------
# Помощники конвейера
# --------------------------------------------------------------------------

def _cfg():
    return gconfig.get_settings()


def _ckpt(params):
    """Выбрать чекпоинт: из параметров, из настроек, иначе первый установленный."""
    from ..graph import get_models
    available = get_models(folder="checkpoints") or []
    return pick_model(
        params.get("ckpt") or _cfg().get("default_checkpoint") or "",
        available, "checkpoint")


def _vae_name(params):
    """Выбрать отдельный VAE, если он задан (иначе берётся из чекпоинта)."""
    from ..graph import get_models
    available = get_models(folder="vae") or []
    return pick_model(params.get("vae") or _cfg().get("default_vae") or "",
                      available, "vae")


def _add_sampler_nodes(b, ckpt, params, latent, pos, neg,
                       prefix="GenLab/image"):
    """Добавить KSampler → VAEDecode → SaveImage.

    latent/pos/neg — готовые значения для входов (ref(...) или id ноды).
    """
    sampler = b.add("KSampler", {
        "model": ref(ckpt),
        "seed": seed_value(params.get("seed")),
        "steps": int(params.get("steps") or 25),
        "cfg": float(params.get("cfg") or 7.0),
        "sampler_name": params.get("sampler") or DEFAULT_SAMPLER,
        "scheduler": params.get("scheduler") or DEFAULT_SCHEDULER,
        "positive": pos,
        "negative": neg,
        "latent_image": latent,
        "denoise": float(params.get("denoise", 1.0)),
    })
    decoded = b.add("VAEDecode", {"samples": ref(sampler), "vae": ref(ckpt, 2)})
    saved = b.add("SaveImage", {"images": ref(decoded),
                                "filename_prefix": prefix})
    return sampler, decoded, saved


def _add_prompts(b, ckpt, params, reuse=None):
    """Позитивное и негативное условие. reuse — CLIP-нода для переиспользования."""
    positive = params.get("prompt") or ""
    negative = params.get("negative") or ""
    if reuse:
        b.link(reuse, text=positive)
        pos = reuse
    else:
        pos = b.add("CLIPTextEncode", {"text": positive, "clip": ref(ckpt, 1)})
    # Пустой негатив ComfyUI не любит — подставляем безобидный пробел.
    neg = b.add("CLIPTextEncode", {"text": negative or " ",
                                   "clip": ref(ckpt, 1)})
    return pos, neg


def _wrap(graph, meta=None):
    return {"graph": graph, "meta": meta or {}, "ui_workflow": None}


def _preprocess_hint(kind):
    return {
        "canny": "",
        "depth": "Требуется пак препроцессоров (MiDaS / Depth Anything)",
        "normal": "Требуется MiDaS / DSINE",
        "pose": "Требуется DWPose / OpenPose ControlNet",
        "lineart": "Требуется Lineart ControlNet",
        "seg": "Требуется SAM / seg ControlNet",
        "blur": "",
    }.get(kind, "")


# --------------------------------------------------------------------------
# Базовые поля (общие для всех пресетов)
# --------------------------------------------------------------------------

def _prompt_fields(extra=()):
    fields = [
        Field.textarea("prompt", "Промпт",
                       "фотореалистичная фотография, мягкий студийный свет, "
                       "высокая детализация, 35mm",
                       placeholder="Опишите, что должно быть на картинке"),
        Field.textarea("negative", "Негативный промпт",
                       "размытость, артефакты, лишние пальцы, текст, водяные знаки"),
    ]
    fields.extend(extra)
    return fields


def _sampler_fields(extra=()):
    return [
        Field.slider("cfg", "CFG", 7.0, 1.0, 20.0, 0.5,
                     hint="Выше — точнее с промптом, но может «пережалить»"),
        Field.slider("steps", "Шаги", 25, 1, 100, 1),
        Field.select("sampler", "Сэмплер", DEFAULT_SAMPLER, options=SAMPLERS),
        Field.select("scheduler", "Планировщик", DEFAULT_SCHEDULER, options=SCHEDULERS),
        Field.seed(),
    ] + list(extra)


# --------------------------------------------------------------------------
# Пресеты
# --------------------------------------------------------------------------

@register(
    "image.text2img", group="image", title="Текст → изображение",
    icon="fa-pen-to-square", cost="medium", outputs=("image",),
    description="Классическая генерация с нуля по текстовому описанию",
    fields=_prompt_fields() + [
        Field.number("width", "Ширина", 1024, min=64, max=4096, step=8),
        Field.number("height", "Высота", 1024, min=64, max=4096, step=8),
        Field.number("batch", "Количество", 1, min=1, max=8, step=1),
    ] + _sampler_fields([Field.slider("denoise", "Denoise", 1.0, 0.0, 1.0, 0.01)]),
)
def text2img(params):
    """Текст → изображение."""
    b = GraphBuilder()
    ckpt = b.add("CheckpointLoaderSimple", {"ckpt_name": _ckpt(params)})
    pos, neg = _add_prompts(b, ckpt, params)
    latent = b.add("EmptyLatentImage", {
        "width": int(params.get("width") or 1024),
        "height": int(params.get("height") or 1024),
        "batch_size": int(params.get("batch") or 1),
    })
    _add_sampler_nodes(b, ckpt, params, ref(latent), ref(pos), ref(neg))
    return _wrap(b.build(), {"kind": "image", "width": params.get("width"),
                             "height": params.get("height")})


@register(
    "image.img2img", group="image", title="Изображение → изображение",
    icon="fa-image", cost="medium", outputs=("image",),
    description="Перерисовка загруженного изображения по промпту",
    fields=_prompt_fields([
        Field.file("image", "Исходное изображение", "image/*",
                   hint="Сначала загрузите файл — он уйдёт в input ComfyUI"),
    ]) + _sampler_fields([
        Field.slider("denoise", "Сила изменения", 0.55, 0.0, 1.0, 0.01,
                     hint="0.2 — почти без изменений, 0.8 — полная перерисовка"),
    ]),
)
def img2img(params):
    """Изображение → изображение."""
    image_name = params.get("image") or ""
    if not image_name:
        raise ValueError("Загрузите исходное изображение")
    b = GraphBuilder()
    ckpt = b.add("CheckpointLoaderSimple", {"ckpt_name": _ckpt(params)})
    pos, neg = _add_prompts(b, ckpt, params)
    load = b.add("LoadImage", {"image": image_name})
    latent = b.add("VAEEncode", {"pixels": ref(load), "vae": ref(ckpt, 2)})
    _add_sampler_nodes(b, ckpt, params, ref(latent), ref(pos), ref(neg))
    return _wrap(b.build(), {"kind": "image", "source": image_name})


@register(
    "edit.inpaint", group="edit", title="Инпейнт (закрасить область)",
    icon="fa-eraser", cost="medium", outputs=("image",),
    description="Перерисовка только выбранной области по маске",
    fields=_prompt_fields([
        Field.file("image", "Изображение", "image/*"),
        Field.file("mask", "Маска (белое = перерисовать)", "image/*",
                   hint="Либо нарисуйте маску в редакторе — она сохранится как файл"),
    ]) + _sampler_fields([
        Field.slider("denoise", "Сила изменения", 0.8, 0.0, 1.0, 0.01),
        Field.checkbox("grow_mask", "Расширить маску", False),
        Field.number("grow", "Расширение, px", 24, min=0, max=512, step=1,
                     when={"grow_mask": True}),
        Field.slider("feather", "Мягкость края", 24, 0, 256, 1),
        Field.checkbox("noise_mask", "Добавить шум в маску", True),
    ]),
)
def inpaint(params):
    """Инпейнт по маске."""
    image_name = params.get("image") or ""
    mask_name = params.get("mask") or ""
    if not image_name:
        raise ValueError("Загрузите изображение")
    if not mask_name:
        raise ValueError("Нужна маска: загрузите файл или нарисуйте в редакторе")

    b = GraphBuilder()
    ckpt = b.add("CheckpointLoaderSimple", {"ckpt_name": _ckpt(params)})
    pos, neg = _add_prompts(b, ckpt, params)
    load = b.add("LoadImage", {"image": image_name})
    mask_src = b.add("LoadImage", {"image": mask_name})

    # Слот 1 у LoadImage — маска по альфа-каналу. Для отдельного файла маски
    # берём красный канал (в редакторе маска рисуется белым по чёрному).
    mask = b.add("ImageToMask", {"image": ref(mask_src), "channel": "red"})
    if params.get("grow_mask"):
        mask = b.add("GrowMask", {"mask": ref(mask), "expand": int(params.get("grow") or 24),
                                  "tapered_corners": True})
    feather = int(params.get("feather") or 0)
    if feather > 0:
        mask = b.add("FeatherMask", {"mask": ref(mask), "left": feather, "top": feather,
                                    "right": feather, "bottom": feather})

    cond = b.add("InpaintModelConditioning", {
        "positive": ref(pos), "negative": ref(neg), "vae": ref(ckpt, 2),
        "pixels": ref(load), "mask": ref(mask),
        "noise_mask": bool(params.get("noise_mask", True)),
    })
    sampler, decoded, _ = _add_sampler_nodes(b, ckpt, params, ref(cond, 2),
                                              ref(cond, 0), ref(cond, 1))
    del sampler, decoded
    return _wrap(b.build(), {"kind": "image", "operation": "inpaint"})


@register(
    "edit.outpaint", group="edit", title="Апсайл / расширение холста",
    icon="fa-expand", cost="medium", outputs=("image",),
    description="Достраивает изображение за его границами (outpainting)",
    fields=_prompt_fields([
        Field.file("image", "Изображение", "image/*"),
    ]) + [
        Field.number("left", "Слева, px", 0, min=0, max=2048, step=8),
        Field.number("top", "Сверху, px", 0, min=0, max=2048, step=8),
        Field.number("right", "Справа, px", 512, min=0, max=2048, step=8),
        Field.number("bottom", "Снизу, px", 0, min=0, max=2048, step=8),
        Field.number("feathering", "Растушёвка, px", 64, min=0, max=512, step=1),
    ] + _sampler_fields([
        Field.slider("denoise", "Сила изменения", 0.85, 0.0, 1.0, 0.01),
    ]),
)
def outpaint(params):
    """Расширение холста с перерисовкой краёв."""
    image_name = params.get("image") or ""
    if not image_name:
        raise ValueError("Загрузите изображение")
    b = GraphBuilder()
    ckpt = b.add("CheckpointLoaderSimple", {"ckpt_name": _ckpt(params)})
    pos, neg = _add_prompts(b, ckpt, params)
    load = b.add("LoadImage", {"image": image_name})
    padded = b.add("ImagePadForOutpaint", {
        "image": ref(load),
        "left": int(params.get("left") or 0), "top": int(params.get("top") or 0),
        "right": int(params.get("right") or 0), "bottom": int(params.get("bottom") or 0),
        "feathering": int(params.get("feathering") or 64),
    })
    cond = b.add("InpaintModelConditioning", {
        "positive": ref(pos), "negative": ref(neg), "vae": ref(ckpt, 2),
        "pixels": ref(padded), "mask": ref(padded, 1),
        "noise_mask": True,
    })
    _add_sampler_nodes(b, ckpt, params, ref(cond, 2), ref(cond, 0), ref(cond, 1))
    return _wrap(b.build(), {"kind": "image", "operation": "outpaint"})


@register(
    "edit.upscale", group="edit", title="Апскейл 2×/4×",
    icon="fa-up-right-and-down-left-scale", cost="low", outputs=("image",),
    description="Увеличение разрешения нейросетевой моделью без потери деталей",
    fields=[
        Field.file("image", "Изображение", "image/*"),
        Field.text("model", "Модель апскейла", "", hint="Файлы из models/upscale_models"),
        Field.select("method", "Метод (если модель не выбрана)", "lanczos",
                     options=["nearest-exact", "bilinear", "area", "bicubic", "lanczos"]),
        Field.number("width", "Ширина результата", 0, min=0, max=8192, step=8,
                     hint="0 = взять пропорционально множителю модели"),
        Field.number("height", "Высота результата", 0, min=0, max=8192, step=8),
        Field.checkbox("use_model", "Использовать модель апскейла", True),
    ],
)
def upscale(params):
    """Апскейл: моделью апскейла либо обычным ресайзом."""
    image_name = params.get("image") or ""
    if not image_name:
        raise ValueError("Загрузите изображение")
    b = GraphBuilder()
    load = b.add("LoadImage", {"image": image_name})

    use_model = bool(params.get("use_model", True))
    model_name = (params.get("model") or "").strip()
    if use_model and model_name:
        loader = b.add("UpscaleModelLoader", {"model_name": model_name})
        node = b.add("ImageUpscaleWithModel",
                     {"upscale_model": ref(loader), "image": ref(load)})
    else:
        node = load

    width = int(params.get("width") or 0)
    height = int(params.get("height") or 0)
    if width > 0 or height > 0:
        node = b.add("ImageScale", {
            "image": ref(node),
            "upscale_method": params.get("method") or "lanczos",
            "width": width or 2048, "height": height or 2048, "crop": "disabled",
        })
    b.add("SaveImage", {"images": ref(node),
                        "filename_prefix": "GenLab/upscale"})
    return _wrap(b.build(), {"kind": "image", "operation": "upscale"})


@register(
    "edit.controlnet", group="edit", title="ControlNet",
    icon="fa-vector-square", cost="medium", outputs=("image",),
    description="Управление генерацией структурой: контуры, глубина, поза",
    fields=_prompt_fields([
        Field.file("image", "Управляющее изображение", "image/*"),
        Field.text("control_net", "Модель ControlNet", "",
                   hint="Файлы из models/controlnet"),
        Field.select("preprocessor", "Препроцессор", "canny",
                     options=[label for _, label in PREPROCESSORS]),
    ]) + [
        Field.slider("strength", "Сила", 0.8, 0.0, 2.0, 0.05),
        Field.slider("start_percent", "Начало, %", 0.0, 0.0, 1.0, 0.01),
        Field.slider("end_percent", "Конец, %", 1.0, 0.0, 1.0, 0.01),
        Field.number("width", "Ширина", 1024, min=64, max=4096, step=8),
        Field.number("height", "Высота", 1024, min=64, max=4096, step=8),
    ] + _sampler_fields([Field.slider("denoise", "Denoise", 1.0, 0.0, 1.0, 0.01)]),
)
def controlnet(params):
    """ControlNet по контурам / глубине / позе."""
    image_name = params.get("image") or ""
    if not image_name:
        raise ValueError("Загрузите управляющее изображение")
    cn_name = (params.get("control_net") or "").strip()
    if not cn_name:
        raise ValueError("Выберите модель ControlNet")

    kind = _kind_by_label(params.get("preprocessor"))
    b = GraphBuilder()
    ckpt = b.add("CheckpointLoaderSimple", {"ckpt_name": _ckpt(params)})
    pos, neg = _add_prompts(b, ckpt, params)
    load = b.add("LoadImage", {"image": image_name})
    hint = ""
    if kind == "canny":
        cond_image = b.add("Canny", {"image": ref(load), "low_threshold": 0.4,
                                     "high_threshold": 0.8})
    else:
        cond_image = load
        hint = _preprocess_hint(kind)

    cn = b.add("ControlNetLoader", {"control_net_name": cn_name})
    applied = b.add("ControlNetApplyAdvanced", {
        "positive": ref(pos), "negative": ref(neg), "control_net": ref(cn),
        "image": ref(cond_image),
        "strength": float(params.get("strength", 0.8)),
        "start_percent": float(params.get("start_percent", 0.0)),
        "end_percent": float(params.get("end_percent", 1.0)),
    })
    latent = b.add("EmptyLatentImage", {
        "width": int(params.get("width") or 1024),
        "height": int(params.get("height") or 1024), "batch_size": 1,
    })
    _add_sampler_nodes(b, ckpt, params, ref(latent), ref(applied, 0), ref(applied, 1))
    return _wrap(b.build(), {"kind": "image", "operation": "controlnet",
                             "hint": hint})


def _kind_by_label(label):
    for kind, text in PREPROCESSORS:
        if text == label:
            return kind
    return label or "none"


@register(
    "edit.instruct", group="edit", title="Instruct Pix2Pix",
    icon="fa-pen-ruler", cost="medium", outputs=("image",),
    description="Правка изображения словами-командами: «сделай зимой», «убери фон»",
    fields=[
        Field.file("image", "Изображение", "image/*"),
        Field.textarea("prompt", "Команда",
                       "make it winter, add snow on the ground",
                       placeholder="Например: turn it into a watercolor painting"),
    ] + _sampler_fields([
        Field.slider("denoise", "Сила правки", 0.75, 0.0, 1.0, 0.01),
        Field.number("image_cfg_scale", "Image CFG", 1.5, min=0.5, max=10.0, step=0.1,
                     hint="Насколько строго следовать исходной картинке"),
    ]),
)
def instruct(params):
    """Instruct Pix2Pix: редактирование текстовой командой."""
    image_name = params.get("image") or ""
    if not image_name:
        raise ValueError("Загрузите изображение")
    b = GraphBuilder()
    ckpt = b.add("CheckpointLoaderSimple", {"ckpt_name": _ckpt(params)})
    pos, neg = _add_prompts(b, ckpt, params)
    load = b.add("LoadImage", {"image": image_name})
    cond = b.add("InstructPixToPixConditioning", {
        "positive": ref(pos), "negative": ref(neg), "vae": ref(ckpt, 2),
        "pixels": ref(load),
    })
    sampler = b.add("KSampler", {
        "model": ref(ckpt), "seed": seed_value(params.get("seed")),
        "steps": int(params.get("steps") or 20),
        "cfg": float(params.get("cfg") or 7.5),
        "sampler_name": params.get("sampler") or DEFAULT_SAMPLER,
        "scheduler": params.get("scheduler") or DEFAULT_SCHEDULER,
        "positive": ref(cond, 0), "negative": ref(cond, 1),
        "latent_image": ref(cond, 2),
        "denoise": float(params.get("denoise", 0.75)),
    })
    decoded = b.add("VAEDecode", {"samples": ref(sampler), "vae": ref(ckpt, 2)})
    b.add("SaveImage", {"images": ref(decoded),
                        "filename_prefix": "GenLab/instruct"})
    return _wrap(b.build(), {"kind": "image", "operation": "instruct",
                             "image_cfg_scale": params.get("image_cfg_scale")})


@register(
    "edit.removebg", group="edit", title="Убрать фон",
    icon="fa-eraser", cost="low", outputs=("image", "mask"),
    description="Вырезает объект, оставляя прозрачный фон",
    fields=[
        Field.file("image", "Изображение", "image/*"),
        Field.text("model", "Модель удаления фона", "",
                   hint="Файлы из models/background_removal (например u2net.pth)"),
    ],
)
def removebg(params):
    """Удаление фона с применением маски к исходной картинке."""
    image_name = params.get("image") or ""
    if not image_name:
        raise ValueError("Загрузите изображение")
    b = GraphBuilder()
    load = b.add("LoadImage", {"image": image_name})
    model_name = (params.get("model") or "").strip()
    if not model_name:
        raise ValueError("Укажите модель удаления фона")
    bg_model = b.add("LoadBackgroundRemovalModel", {"bg_removal_name": model_name})
    mask = b.add("RemoveBackground", {"bg_removal_model": ref(bg_model),
                                       "image": ref(load)})
    out = b.add("ImageCompositeMasked", {
        "destination": ref(load), "source": ref(load), "x": 0, "y": 0,
        "resize_source": False, "mask": ref(mask),
    })
    b.add("SaveImage", {"images": ref(out), "filename_prefix": "GenLab/nobg"})
    return _wrap(b.build(), {"kind": "image", "operation": "removebg"})


@register(
    "edit.detail", group="edit", title="Детализация портрета",
    icon="fa-user-astronaut", cost="low", outputs=("image",),
    description="Увеличение портрета в 2–4 раза с лёгким усилением резкости",
    fields=[
        Field.file("image", "Портрет", "image/*"),
        Field.number("factor", "Множитель", 2, min=2, max=4, step=1),
        Field.select("method", "Метод", "lanczos",
                     options=["nearest-exact", "bilinear", "area", "bicubic", "lanczos"]),
        Field.checkbox("sharpen", "Подчеркнуть резкость", True),
    ],
)
def detail(params):
    """Апскейл портрета обычным ресайзом + опциональная резкость."""
    image_name = params.get("image") or ""
    if not image_name:
        raise ValueError("Загрузите портрет")
    b = GraphBuilder()
    load = b.add("LoadImage", {"image": image_name})
    node = b.add("ImageScaleBy", {
        "image": ref(load),
        "upscale_method": params.get("method") or "lanczos",
        "scale_by": float(max(1, int(params.get("factor") or 2))),
    })
    if params.get("sharpen"):
        node = b.add("ImageSharpen", {"image": ref(node), "sharpen_radius": 2,
                                      "sigma": 1.0, "alpha": 0.4})
    b.add("SaveImage", {"images": ref(node), "filename_prefix": "GenLab/portrait"})
    return _wrap(b.build(), {"kind": "image", "operation": "detail"})