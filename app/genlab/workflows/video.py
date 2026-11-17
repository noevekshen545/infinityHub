"""
GenLab — пресеты видео на встроенных нодах ComfyUI 0.39.

Что доступно локально (без облака и API-ключей):
  * Wan 2.1 — через UNETLoader + DualCLIPLoader + WanImageToVideo. Узел называется
    «ImageToVideo», но start_image у него необязательный: без него получается
    полноценный text-to-video (так сделан и официальный шаблон ComfyUI);
  * работа с готовым видео: LoadVideo → VideoTrim / ConcatenateVideo;
  * интерполяция кадров RIFE через FrameInterpolate;
  * сборка видео из кадров через CreateVideo → SaveVideo.

Требования к VRAM на 16 ГБ: 480p×81 кадр для Wan 2.1 (1.3B) идёт впритык,
поэтому по умолчанию стоит 480p и 49 кадров (~2 с при 24 fps).
"""

from ..graph import GraphBuilder, pick_model, ref, seed_value
from . import Field, register
from .image import DEFAULT_SAMPLER, DEFAULT_SCHEDULER, SAMPLERS, SCHEDULERS

VIDEO_FORMATS = ["auto", "mp4", "mkv", "webm"]

# Пресеты качества: (ширина, высота, кадры) — подобраны под 16 ГБ VRAM.
QUALITY = {
    "480p49": (832, 480, 49),
    "480p81": (832, 480, 81),
    "576p49": (1024, 576, 49),
    "720p49": (1280, 720, 49),
    "720p81": (1280, 720, 81),
}


WAN_VERSIONS = [
    ("2.1", "Wan 2.1 — DualCLIPLoader + WanImageToVideo"),
    ("2.2", "Wan 2.2 — один CLIPLoader + Wan22ImageToVideoLatent"),
]


def _models(folder):
    from ..graph import get_models
    return get_models(folder=folder) or []


def _wan_stack(b, params):
    """Общая часть Wan-пайплайна. Возвращает (model, clip, vae)."""
    unet = b.add("UNETLoader", {
        "unet_name": pick_model(params.get("unet"), _models("diffusion_models"), "unet"),
        "weight_dtype": params.get("weight_dtype") or "default",
    })
    model = b.add("ModelSamplingSD3", {
        "model": ref(unet), "shift": float(params.get("shift") or 8.0),
    })
    if _wan_version(params) == "2.2":
        # В Wan 2.2 используется один энкодер, а латент строится отдельной нодой.
        clip = b.add("CLIPLoader", {
            "clip_name": pick_model(params.get("clip1"), _models("text_encoders"), "clip"),
            "type": "wan",
        })
    else:
        clip = b.add("DualCLIPLoader", {
            "clip_name1": pick_model(params.get("clip1"), _models("text_encoders"), "clip"),
            "clip_name2": pick_model(params.get("clip2"), _models("text_encoders"), "clip"),
            "type": "wan",
        })
    vae = b.add("VAELoader", {
        "vae_name": pick_model(params.get("vae"), _models("vae"), "vae"),
    })
    return model, clip, vae


def _wan_version(params):
    """Из подписи поля «Версия Wan» достаём ключ 2.1 / 2.2."""
    label = params.get("wan_version") or ""
    for key, text in WAN_VERSIONS:
        if text == label:
            return key
    return "2.1"


def _wan_latent(b, params, vae, pos, neg, image_node=None, clip_vision=None):
    """Латент для генерации видео. Без image_node получается text-to-video.

    Возвращает (latent_link, positive_link, negative_link, w, h, length).
    Разные ветки Wan отличаются и загрузчиком CLIP, и способом построения
    латента, поэтому выбор версии спрятан здесь.
    """
    width, height, length = _quality(params)
    if _wan_version(params) == "2.2":
        # Wan 2.2: латент строится отдельной нодой, conditioning не меняется.
        inputs = {"vae": ref(vae), "width": width, "height": height,
                  "length": length, "batch_size": 1}
        if image_node:
            inputs["start_image"] = ref(image_node)
        node = b.add("Wan22ImageToVideoLatent", inputs)
        return ref(node, 0), ref(pos), ref(neg), width, height, length

    inputs = {"positive": ref(pos), "negative": ref(neg), "vae": ref(vae),
              "width": width, "height": height, "length": length, "batch_size": 1}
    if image_node:
        inputs["start_image"] = ref(image_node)
    if clip_vision:
        inputs["clip_vision_output"] = ref(clip_vision)
    node = b.add("WanImageToVideo", inputs)
    return ref(node, 2), ref(node, 0), ref(node, 1), width, height, length


def _wan_clip_vision(b, params, image_node):
    """Опциональный CLIP Vision — чтобы модель лучше держала исходную картинку."""
    if not params.get("use_clip_vision"):
        return None
    cv = b.add("CLIPVisionLoader", {
        "clip_name": pick_model(params.get("clip_vision"), _models("clip_vision"),
                                "clip_vision"),
    })
    return b.add("CLIPVisionEncode", {"clip_vision": ref(cv),
                                      "image": ref(image_node), "crop": "center"})


def _quality(params):
    preset = params.get("quality") or "480p49"
    w, h, length = QUALITY.get(preset, QUALITY["480p49"])
    return (int(params.get("width") or w), int(params.get("height") or h),
            int(params.get("length") or length))


def _video_tail(b, images_node, params):
    """Кадры → CreateVideo → SaveVideo. Возвращает id выходной ноды."""
    create = b.add("CreateVideo", {
        "images": ref(images_node),
        "fps": float(params.get("fps") or 24.0),
        "bit_depth": params.get("bit_depth") or "8",
    })
    save = b.add("SaveVideo", {
        "video": ref(create),
        "filename_prefix": params.get("filename_prefix") or "GenLab/video",
        "format": params.get("format") or "mp4",
    })
    return create, save


def _video_fields(extra=()):
    return [
        Field.textarea("prompt", "Промпт",
                       "cinematic aerial shot over a misty pine forest, "
                       "slow camera dolly forward, golden hour"),
        Field.textarea("negative", "Негативный промпт",
                       "blurry, distorted, watermark, text, low quality"),
    ] + list(extra) + [
        Field.select("quality", "Качество", "480p49",
                     options=list(QUALITY.keys()),
                     hint="81 кадр при 24 fps — это 3,4 с видео"),
        Field.number("length", "Кадров", 49, min=1, max=16384, step=4,
                     hint="Должно делиться на 4: 4n+1 (49, 81, 121…)"),
        Field.number("width", "Ширина", 0, min=0, max=1920, step=16,
                     hint="0 = значение из пресета качества"),
        Field.number("height", "Высота", 0, min=0, max=1080, step=16),
        Field.slider("shift", "Shift", 8.0, 1.0, 12.0, 0.1),
        Field.select("wan_version", "Версия Wan", WAN_VERSIONS[1][1],
                     options=[t for _, t in WAN_VERSIONS]),
        Field.text("unet", "Модель Wan (unet)", "",
                   hint="Файлы из models/diffusion_models"),
        Field.text("clip1", "Text encoder 1", "", hint="Файлы из models/text_encoders"),
        Field.text("clip2", "Text encoder 2", "", hint="models/text_encoders"),
        Field.text("vae", "VAE", "", hint="Файлы из models/vae"),
        Field.select("weight_dtype", "Точность весов", "default",
                     options=["default", "fp8_e4m3fn", "fp8_e4m3fn_fast",
                              "fp8_e5m2"],
                     hint="fp8 вдвое экономит VRAM"),
        Field.slider("cfg", "CFG", 5.0, 1.0, 10.0, 0.1),
        Field.number("steps", "Шаги", 20, min=1, max=100, step=1),
        Field.select("sampler", "Сэмплер", DEFAULT_SAMPLER, options=SAMPLERS),
        Field.select("scheduler", "Планировщик", DEFAULT_SCHEDULER, options=SCHEDULERS),
        Field.seed(),
        Field.number("fps", "FPS", 24, min=1, max=120, step=1),
        Field.select("format", "Формат", "mp4", options=VIDEO_FORMATS),
    ]


@register(
    "video.text2video", group="video", title="Текст → видео",
    icon="fa-clapperboard", cost="high", outputs=("video",),
    description="Генерация видео из текстового описания (Wan, локально)",
    fields=_video_fields(),
)
def text2video(params):
    """Текст → видео (Wan)."""
    b = GraphBuilder()
    model, clip, vae = _wan_stack(b, params)
    pos = b.add("CLIPTextEncode", {"text": params.get("prompt") or "",
                                   "clip": ref(clip)})
    neg = b.add("CLIPTextEncode", {"text": params.get("negative") or " ",
                                   "clip": ref(clip)})
    latent, pos_link, neg_link, width, height, length = _wan_latent(
        b, params, vae, pos, neg)
    sampler = b.add("KSampler", {
        "model": ref(model), "seed": seed_value(params.get("seed")),
        "steps": int(params.get("steps") or 20),
        "cfg": float(params.get("cfg") or 5.0),
        "sampler_name": params.get("sampler") or DEFAULT_SAMPLER,
        "scheduler": params.get("scheduler") or DEFAULT_SCHEDULER,
        "positive": pos_link, "negative": neg_link, "latent_image": latent,
        "denoise": 1.0,
    })
    decoded = b.add("VAEDecode", {"samples": ref(sampler), "vae": ref(vae)})
    _video_tail(b, decoded, params)
    return _wrap_video(b, params, width, height, length, "text2video")


@register(
    "video.image2video", group="video", title="Картинка → видео",
    icon="fa-film", cost="high", outputs=("video",),
    description="Оживление статичной картинки: камера, движение, свет",
    fields=_video_fields([
        Field.file("image", "Первый кадр", "image/*"),
        Field.checkbox("use_clip_vision", "Учитывать изображение через CLIP Vision",
                       True, hint="Лучше следование промпту, но VRAM + ~1 ГБ"),
        Field.text("clip_vision", "CLIP Vision", "",
                   hint="Файлы из models/clip_vision", when={"use_clip_vision": True}),
    ]),
)
def image2video(params):
    """Картинка → видео (Wan i2v)."""
    image_name = params.get("image") or ""
    if not image_name:
        raise ValueError("Загрузите изображение — оно станет первым кадром")
    b = GraphBuilder()
    model, clip, vae = _wan_stack(b, params)
    pos = b.add("CLIPTextEncode", {"text": params.get("prompt") or "",
                                   "clip": ref(clip)})
    neg = b.add("CLIPTextEncode", {"text": params.get("negative") or " ",
                                   "clip": ref(clip)})
    load = b.add("LoadImage", {"image": image_name})
    cv_out = _wan_clip_vision(b, params, load)
    latent, pos_link, neg_link, width, height, length = _wan_latent(
        b, params, vae, pos, neg, image_node=load, clip_vision=cv_out)
    sampler = b.add("KSampler", {
        "model": ref(model), "seed": seed_value(params.get("seed")),
        "steps": int(params.get("steps") or 20),
        "cfg": float(params.get("cfg") or 5.0),
        "sampler_name": params.get("sampler") or DEFAULT_SAMPLER,
        "scheduler": params.get("scheduler") or DEFAULT_SCHEDULER,
        "positive": pos_link, "negative": neg_link, "latent_image": latent,
        "denoise": 1.0,
    })
    decoded = b.add("VAEDecode", {"samples": ref(sampler), "vae": ref(vae)})
    _video_tail(b, decoded, params)
    return _wrap_video(b, params, width, height, length, "image2video",
                       source=image_name)


@register(
    "video.interpolate", group="video", title="Интерполяция кадров (RIFE)",
    icon="fa-forward", cost="medium", outputs=("video",),
    description="Плавное замедление или ускорение готового видео в N раз",
    fields=[
        Field.file("video", "Видеофайл", "video/*"),
        Field.number("multiplier", "Множитель кадров", 2, min=2, max=8, step=1,
                     hint="2 = замедление вдвое с интерполяцией"),
        Field.text("model", "Модель RIFE", "", hint="Файлы из models/frame_interpolation"),
        Field.number("fps", "FPS на выходе", 30, min=1, max=120, step=1),
        Field.select("format", "Формат", "mp4", options=VIDEO_FORMATS),
    ],
)
def interpolate(params):
    """Интерполяция кадров через RIFE."""
    video_name = params.get("video") or ""
    if not video_name:
        raise ValueError("Загрузите видеофайл")
    b = GraphBuilder()
    load = b.add("LoadVideo", {"file": video_name})
    parts = b.add("GetVideoComponents", {"video": ref(load)})
    model = b.add("FrameInterpolationModelLoader", {
        "model_name": pick_model(params.get("model"),
                                 _models("frame_interpolation"), "rife"),
    })
    frames = b.add("FrameInterpolate", {
        "interp_model": ref(model), "images": ref(parts, 0),
        "multiplier": int(params.get("multiplier") or 2),
    })
    create = b.add("CreateVideo", {
        "images": ref(frames), "fps": float(params.get("fps") or 30.0),
        "bit_depth": "8",
    })
    b.add("SaveVideo", {"video": ref(create),
                        "filename_prefix": "GenLab/interpolated",
                        "format": params.get("format") or "mp4"})
    return {"graph": b.build(), "meta": {"kind": "video", "operation": "interpolate"},
            "ui_workflow": None}


@register(
    "video.concat", group="video", title="Склейка видео",
    icon="fa-object-group", cost="low", outputs=("video",),
    description="Объединение двух видеофайлов в один (порядок сохраняется)",
    fields=[
        Field.file("video1", "Первое видео", "video/*"),
        Field.file("video2", "Второе видео", "video/*"),
        Field.checkbox("with_audio", "Склеить со звуком", True),
        Field.select("codec", "Кодек", "auto", options=["auto", "h264", "av1"]),
        Field.select("format", "Формат", "mp4", options=VIDEO_FORMATS),
    ],
)
def concat(params):
    """Склейка двух видео."""
    v1 = params.get("video1") or ""
    v2 = params.get("video2") or ""
    if not v1 or not v2:
        raise ValueError("Нужны два видеофайла")
    b = GraphBuilder()
    a = b.add("LoadVideo", {"file": v1})
    c = b.add("LoadVideo", {"file": v2})
    joined = b.add("ConcatenateVideo", {
        "videos": [ref(a), ref(c)],
        "codec": params.get("codec") or "auto",
        "complete_audio": bool(params.get("with_audio", True)),
    })
    b.add("SaveVideo", {"video": ref(joined),
                        "filename_prefix": "GenLab/concat",
                        "format": params.get("format") or "mp4"})
    return {"graph": b.build(), "meta": {"kind": "video", "operation": "concat"},
            "ui_workflow": None}


@register(
    "video.trim", group="video", title="Обрезка видео",
    icon="fa-scissors", cost="low", outputs=("video",),
    description="Вырезать фрагмент из видео без перекодирования лишнего",
    fields=[
        Field.file("video", "Видеофайл", "video/*"),
        Field.number("start", "Начало, с", 0.0, min=0.0, max=36000.0, step=0.1),
        Field.number("duration", "Длительность, с", 5.0, min=0.1, max=36000.0, step=0.1),
        Field.checkbox("strict", "Строго (не выходить за конец)", False),
        Field.select("format", "Формат", "mp4", options=VIDEO_FORMATS),
    ],
)
def trim(params):
    """Обрезка видео."""
    video_name = params.get("video") or ""
    if not video_name:
        raise ValueError("Загрузите видеофайл")
    b = GraphBuilder()
    load = b.add("LoadVideo", {"file": video_name})
    trimmed = b.add("VideoTrim", {
        "video": ref(load), "trim": [float(params.get("start") or 0.0),
                                     float(params.get("duration") or 5.0)],
        "strict_duration": bool(params.get("strict", False)),
    })
    b.add("SaveVideo", {"video": ref(trimmed),
                        "filename_prefix": "GenLab/trim",
                        "format": params.get("format") or "mp4"})
    return {"graph": b.build(), "meta": {"kind": "video", "operation": "trim"},
            "ui_workflow": None}


@register(
    "video.frames2video", group="video", title="Кадры → видео",
    icon="fa-images", cost="low", outputs=("video",),
    description="Склейка именованной последовательности кадров в видео",
    fields=[
        Field.file("frames", "Первый кадр последовательности", "image/*",
                   hint="Дальше кадры ищутся по шаблону имени: shot_00001.png → "
                        "shot_00002.png и так далее"),
        Field.number("count", "Количество кадров", 24, min=2, max=256, step=1,
                     hint="Больше 256 кадров лучше собирать через ffmpeg"),
        Field.number("fps", "FPS", 24, min=1, max=120, step=1),
        Field.select("format", "Формат", "mp4", options=VIDEO_FORMATS),
    ],
)
def frames2video(params):
    """Склейка кадров shot_00001.png, shot_00002.png… в видео.

    Последовательные имена выводятся из имени первого файла: та же расширка
    и та же цифровая часть, увеличенная на порядковый номер.
    """
    first = params.get("frames") or ""
    if not first:
        raise ValueError("Укажите первый кадр последовательности")

    names = _sequence_names(first, max(2, int(params.get("count") or 24)))

    b = GraphBuilder()
    prev = b.add("LoadImage", {"image": names[0]})
    for name in names[1:]:
        nxt = b.add("LoadImage", {"image": name})
        prev = b.add("ImageBatch", {"image1": ref(prev), "image2": ref(nxt)})
    create = b.add("CreateVideo", {
        "images": ref(prev), "fps": float(params.get("fps") or 24.0),
        "bit_depth": "8",
    })
    b.add("SaveVideo", {"video": ref(create),
                        "filename_prefix": "GenLab/frames",
                        "format": params.get("format") or "mp4"})
    return {"graph": b.build(),
            "meta": {"kind": "video", "operation": "frames2video",
                     "frames": len(names)},
            "ui_workflow": None}


def _sequence_names(first, count):
    """shot_00001.png + 3 → shot_00001.png, shot_00002.png, shot_00003.png, shot_00004.png."""
    import os
    import re

    stem, ext = os.path.splitext(first)
    match = re.search(r"(\d+)$", stem)
    if not match:
        raise ValueError(
            "В имени «%s» нет цифр в конце — нельзя понять нумерацию кадров" % first)
    digits = len(match.group(1))
    number = int(match.group(1))
    head = stem[: match.start()]
    return ["%s%0*d%s" % (head, digits, number + i, ext) for i in range(count)]


def _wrap_video(b, params, width, height, length, operation, source=None):
    return {
        "graph": b.build(),
        "meta": {
            "kind": "video",
            "operation": operation,
            "width": width,
            "height": height,
            "length": length,
            "fps": params.get("fps") or 24,
            "source": source,
        },
        "ui_workflow": None,
    }