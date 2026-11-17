"""
GenLab — пресеты 3D на встроенных нодах ComfyUI 0.39.

Локально доступно без кастом-паков:
  * Hunyuan3D v2 — картинка → воксельная форма → меш (GLB);
  * TRELLIS 2 — двухстадийная генерация формы и текстуры;
  * постобработка существующих GLB: ремеш, сглаживание, нормали, рендер
    превью (RemeshMesh / MeshSmoothNormals / RenderMesh / Get3DComponents).

Модели для генерации нужно положить вручную:
    models/diffusion_models — shape-модель
    models/text_encoders    — текстовый энкодер
    models/vae              — VAE
    models/clip_vision      — CLIP Vision
Они большие (5–12 ГБ каждая), поэтому модуль только проверяет их наличие и
подсказывает, что скачать — сам скачиватель отложен.
"""

from ..graph import GraphBuilder, pick_model, ref, seed_value
from . import Field, register
from .image import DEFAULT_SAMPLER, DEFAULT_SCHEDULER, SAMPLERS, SCHEDULERS

MESH_ALGORITHMS = ["surface net", "basic"]

# Сколько примерно весит каждый набор (для подсказок в UI).
MODEL_HINTS = {
    "ckpt": "models/checkpoints — чекпоинт Hunyuan3D (вес + CLIP Vision + VAE)",
    "shape": "models/diffusion_models — shape-модель",
    "texture": "models/diffusion_models — texture-модель (у TRELLIS отдельная)",
    "clip": "models/text_encoders — текстовый энкодер",
    "vae": "models/vae — VAE",
    "clip_vision": "models/clip_vision — CLIP Vision (обычно ~1 ГБ)",
}


def _models(folder):
    from ..graph import get_models
    return get_models(folder=folder) or []


def _require_image(params):
    image_name = params.get("image") or ""
    if not image_name:
        raise ValueError("Загрузите изображение — по нему строится 3D-модель")
    return image_name


@register(
    "model3d.hunyuan", group="model3d", title="Hunyuan3D v2 — картинка → 3D",
    icon="fa-cube", cost="high", outputs=("3d",),
    description="Реконструкция объёма и меша по одному изображению",
    fields=[
        Field.file("image", "Изображение", "image/*",
                   hint="Чем ровнее объект и однотоннее фон — тем лучше результат"),
        Field.text("ckpt", "Чекпоинт Hunyuan3D", "",
                   hint=MODEL_HINTS["ckpt"],
                   placeholder="hunyuan3d_v2_1.safetensors"),
        Field.select("crop", "Обрезка CLIP Vision", "none",
                     options=["none", "center"],
                     hint="none — вся картинка, center — центральный квадрат"),
        Field.select("algorithm", "Алгоритм меша", "surface net",
                     options=MESH_ALGORITHMS),
        Field.slider("threshold", "Порог извлечения поверхности", 0.6, 0.0, 1.0, 0.01),
        Field.number("resolution", "Разрешение вокселя", 3072, min=64, max=8192, step=64),
        Field.number("octree", "Разрешение октокта", 256, min=16, max=512, step=8),
        Field.number("num_chunks", "Порций декодирования", 8000, min=1000, max=500000,
                     step=1000, hint="Уменьшите, если кончается VRAM"),
        Field.slider("cfg", "CFG", 5.0, 1.0, 15.0, 0.1),
        Field.slider("shift", "AuraFlow shift", 1.73, 0.0, 10.0, 0.01),
        Field.number("steps", "Шаги", 30, min=1, max=100, step=1),
        Field.select("sampler", "Сэмплер", DEFAULT_SAMPLER, options=SAMPLERS),
        Field.select("scheduler", "Планировщик", DEFAULT_SCHEDULER, options=SCHEDULERS),
        Field.seed(),
    ],
)
def hunyuan(params):
    """Hunyuan3D v2: изображение → GLB.

    Граф повторяет официальный шаблон ComfyUI 3d_hunyuan3d_image_to_model:
    ImageOnlyCheckpointLoader отдаёт MODEL, CLIP_VISION и VAE одним файлом,
    затем ModelSamplingAuraFlow, Hunyuan3Dv2Conditioning и воксельная
    реконструкция через VAEDecodeHunyuan3D → VoxelToMesh → SaveGLB.
    """
    image_name = _require_image(params)
    b = GraphBuilder()
    ckpt = b.add("ImageOnlyCheckpointLoader", {
        "ckpt_name": pick_model(params.get("ckpt"), _models("checkpoints"), "ckpt"),
    })
    model = b.add("ModelSamplingAuraFlow", {
        "model": ref(ckpt, 0), "shift": float(params.get("shift") or 1.73),
    })
    load = b.add("LoadImage", {"image": image_name})
    cv_out = b.add("CLIPVisionEncode", {
        "clip_vision": ref(ckpt, 1), "image": ref(load),
        "crop": params.get("crop") or "none",
    })
    cond3d = b.add("Hunyuan3Dv2Conditioning", {"clip_vision_output": ref(cv_out)})
    latent = b.add("EmptyLatentHunyuan3Dv2", {
        "resolution": int(params.get("resolution") or 3072),
        "batch_size": 1,
    })
    sampler = b.add("KSampler", {
        "model": ref(model),
        "seed": seed_value(params.get("seed")),
        "steps": int(params.get("steps") or 30),
        "cfg": float(params.get("cfg") or 5.0),
        "sampler_name": params.get("sampler") or DEFAULT_SAMPLER,
        "scheduler": params.get("scheduler") or DEFAULT_SCHEDULER,
        "positive": ref(cond3d, 0), "negative": ref(cond3d, 1),
        "latent_image": ref(latent), "denoise": 1.0,
    })
    voxel = b.add("VAEDecodeHunyuan3D", {
        "samples": ref(sampler), "vae": ref(ckpt, 2),
        "num_chunks": int(params.get("num_chunks") or 8000),
        "octree_resolution": int(params.get("octree") or 256),
    })
    mesh = b.add("VoxelToMesh", {
        "voxel": ref(voxel),
        "algorithm": params.get("algorithm") or "surface net",
        "threshold": float(params.get("threshold", 0.6)),
    })
    b.add("SaveGLB", {"mesh": ref(mesh), "filename_prefix": "GenLab/3d/hunyuan"})
    return {"graph": b.build(),
            "meta": {"kind": "3d", "operation": "hunyuan3d", "source": image_name},
            "ui_workflow": None}


@register(
    "model3d.trellis2", group="model3d", title="TRELLIS 2 — форма + текстура",
    icon="fa-gem", cost="high", outputs=("3d",),
    description="Двухстадийная генерация: сначала форма, затем текстура",
    fields=[
        Field.file("image", "Изображение", "image/*"),
        Field.text("shape_model", "Shape-модель", "", hint=MODEL_HINTS["shape"]),
        Field.text("texture_model", "Texture-модель", "",
                   hint=MODEL_HINTS["texture"]),
        Field.text("vae", "VAE", "", hint=MODEL_HINTS["vae"]),
        Field.text("clip_vision", "CLIP Vision", "", hint=MODEL_HINTS["clip_vision"]),
        Field.select("structure_res", "Разрешение структуры", "32",
                     options=["32", "64"], hint="64 — качественнее и тяжелее"),
        Field.slider("crease", "Угол складки, °", 60.0, 0.0, 180.0, 1.0),
        Field.slider("cfg", "CFG", 5.0, 1.0, 15.0, 0.1),
        Field.number("steps", "Шаги", 30, min=1, max=100, step=1),
        Field.seed(),
    ],
)
def trellis2(params):
    """TRELLIS 2: изображение → текстурированный GLB."""
    image_name = _require_image(params)
    b = GraphBuilder()
    shape_unet = b.add("UNETLoader", {
        "unet_name": pick_model(params.get("shape_model"),
                                _models("diffusion_models"), "shape"),
        "weight_dtype": params.get("weight_dtype") or "default",
    })
    tex_unet = b.add("UNETLoader", {
        "unet_name": pick_model(params.get("texture_model"),
                                _models("diffusion_models"), "texture"),
        "weight_dtype": params.get("weight_dtype") or "default",
    })
    vae = b.add("VAELoader", {
        "vae_name": pick_model(params.get("vae"), _models("vae"), "vae"),
    })
    cv = b.add("CLIPVisionLoader", {
        "clip_name": pick_model(params.get("clip_vision"),
                                _models("clip_vision"), "clip_vision"),
    })
    load = b.add("LoadImage", {"image": image_name})
    cv_out = b.add("CLIPVisionEncode", {"clip_vision": ref(cv), "image": ref(load),
                                        "crop": "none"})
    cond3d = b.add("Trellis2Conditioning", {"clip_vision_model": ref(cv),
                                            "image": ref(load)})

    # Стадия 1 — форма.
    shape_stage = b.add("Trellis2ShapeStage", {
        "positive": ref(cond3d, 0), "negative": ref(cond3d, 1),
        "voxel": ref(b.add("EmptyTrellis2LatentStructure", {"batch_size": 1})),
    })
    shape_sampler = b.add("KSampler", {
        "model": ref(shape_unet), "seed": seed_value(params.get("seed")),
        "steps": int(params.get("steps") or 30),
        "cfg": float(params.get("cfg") or 5.0),
        "sampler_name": DEFAULT_SAMPLER, "scheduler": DEFAULT_SCHEDULER,
        "positive": ref(shape_stage, 0), "negative": ref(shape_stage, 1),
        "latent_image": ref(shape_stage, 2), "denoise": 1.0,
    })
    voxel = b.add("VaeDecodeStructureTrellis2", {
        "samples": ref(shape_sampler), "vae": ref(vae),
        "resolution": params.get("structure_res") or "32",
    })

    # Стадия 2 — текстура.
    tex_stage = b.add("Trellis2TextureStage", {
        "positive": ref(shape_stage, 0), "negative": ref(shape_stage, 1),
        "shape_latent": ref(voxel),
    })
    tex_sampler = b.add("KSampler", {
        "model": ref(tex_unet), "seed": seed_value(params.get("seed")),
        "steps": int(params.get("steps") or 30),
        "cfg": float(params.get("cfg") or 5.0),
        "sampler_name": DEFAULT_SAMPLER, "scheduler": DEFAULT_SCHEDULER,
        "positive": ref(tex_stage, 0), "negative": ref(tex_stage, 1),
        "latent_image": ref(tex_stage, 2), "denoise": 1.0,
    })
    mesh_out = b.add("VaeDecodeShapeTrellis", {"samples": ref(tex_sampler),
                                               "vae": ref(vae)})
    colors = b.add("VaeDecodeTextureTrellis", {
        "samples": ref(tex_sampler), "vae": ref(vae),
        # VaeDecodeShapeTrellis отдаёт shape_subdivides на втором слоте, без него
        # декодирование текстуры не знает, какое разбиение сетки использовать.
        "shape_subdivides": ref(mesh_out, 1),
    })
    mesh = b.add("PaintMesh", {"mesh": ref(mesh_out, 0),
                               "voxel_colors": ref(colors)})
    mesh = b.add("MeshSmoothNormals", {"mesh": ref(mesh),
                                       "crease_angle": float(params.get("crease") or 60.0)})
    b.add("SaveGLB", {"mesh": ref(mesh), "filename_prefix": "GenLab/3d/trellis"})
    return {"graph": b.build(),
            "meta": {"kind": "3d", "operation": "trellis2", "source": image_name},
            "ui_workflow": None}


@register(
    "model3d.remesh", group="model3d", title="Ремеш и рендер GLB",
    icon="fa-cubes", cost="low", outputs=("3d", "image"),
    description="Оптимизация готовой 3D-модели и рендер превью",
    fields=[
        Field.file("model", "3D-файл (GLB/OBJ/PLY)", ".glb,.obj,.ply,.stl"),
        Field.checkbox("remesh", "Ремеш (упрощение топологии)", False),
        Field.number("face_count", "Целевое число полигонов", 50000, min=100,
                     max=2000000, step=1000, when={"remesh": True}),
        Field.checkbox("smooth", "Сгладить нормали", True),
        Field.slider("crease", "Угол складки, °", 60.0, 0.0, 180.0, 1.0,
                     when={"smooth": True}),
        Field.checkbox("preview", "Отрендерить превью", False),
        Field.number("preview_width", "Ширина превью", 768, min=64, max=4096, step=8,
                     when={"preview": True}),
        Field.number("preview_height", "Высота превью", 768, min=64, max=4096, step=8,
                     when={"preview": True}),
    ],
)
def remesh(params):
    """Постобработка 3D-модели без диффузии."""
    model_file = params.get("model") or ""
    if not model_file:
        raise ValueError("Укажите 3D-файл")
    b = GraphBuilder()
    loaded = b.add("Load3D", {
        "model_file": model_file, "image": "example.png",
        "width": 512, "height": 512,
    })
    components = b.add("Get3DComponents", {"model_3d": ref(loaded, 6)})
    mesh = components

    if params.get("remesh"):
        mesh = b.add("DecimateMesh", {
            "mesh": ref(components), "target_face_count": int(params.get("face_count") or 50000),
            "placement_mode": "midpoint",
        })
    if params.get("smooth"):
        mesh = b.add("MeshSmoothNormals", {
            "mesh": ref(mesh), "crease_angle": float(params.get("crease") or 60.0),
        })

    b.add("SaveGLB", {"mesh": ref(mesh), "filename_prefix": "GenLab/3d/remesh"})

    if params.get("preview"):
        shot = b.add("RenderMesh", {
            "mesh": ref(mesh),
            "mode": "solid",
            "width": int(params.get("preview_width") or 768),
            "height": int(params.get("preview_height") or 768),
            "background": "#ffffff",
        })
        b.add("SaveImage", {"images": ref(shot, 0),
                            "filename_prefix": "GenLab/3d/preview"})
    return {"graph": b.build(),
            "meta": {"kind": "3d", "operation": "remesh", "source": model_file},
            "ui_workflow": None}