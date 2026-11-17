"""
GenLab — пресеты аудио на встроенных нодах ComfyUI 0.39.

Локально доступно:
  * TTS на локальном движке ByteDance Seed Audio (`ByteDanceSeedAudio`);
  * музыка: ACE-Step 1.5 — граф собран по официальному шаблону ComfyUI
    `audio_ace_step1_5_xl_base` (DualCLIPLoader(ace) → TextEncodeAceStepAudio1.5,
    негатив через ConditioningZeroOut, UNETLoader + ModelSamplingAuraFlow,
    латент EmptyAceStep1.5LatentAudio);
  * инструментальная обработка без нейросетей: LoadAudio → громкость,
    эквалайзер, обрезка, склейка → SaveAudioMP3/Opus/Advanced.

Важно: инструментальные пресеты работают вообще без моделей — ComfyUI тут
просто выполняет операции над файлами.
"""

from ..graph import GraphBuilder, pick_model, ref, seed_value
from . import Field, register

MUSIC_ENGINES = [("ace_step", "ACE-Step 1.5")]

SAVE_AUDIO_FORMATS = ["mp3", "opus", "flac", "wav", "pcm"]


def _models(folder):
    from ..graph import get_models
    return get_models(folder=folder) or []


def _node_exists(name):
    """True/False по схеме; None — схему получить не удалось (ComfyUI молчит)."""
    from ..graph import get_object_info
    info = get_object_info()
    if not info:
        return None
    return name in info


def _require_node(name, message):
    """Проверить ноду перед сборкой графа.

    None от _node_exists значит «не знаю»: ComfyUI не запущен или ещё
    поднимается. Тогда граф собирается как обычно — иначе пользователь
    увидел бы «ноды нет в сборке», хотя достаточно запустить движок.
    """
    if _node_exists(name) is False:
        raise ValueError(message)


def _save_audio(b, audio_link, params, prefix="GenLab/audio"):
    fmt = (params.get("format") or "mp3").lower()
    filename_prefix = params.get("filename_prefix") or prefix
    if fmt == "mp3":
        return b.add("SaveAudioMP3", {
            "audio": audio_link, "filename_prefix": filename_prefix,
            "quality": params.get("quality") or "192k",
        })
    if fmt == "opus":
        return b.add("SaveAudioOpus", {
            "audio": audio_link, "filename_prefix": filename_prefix,
            "quality": params.get("quality") or "192k",
        })
    if fmt == "flac":
        return b.add("SaveAudio", {
            "audio": audio_link, "filename_prefix": filename_prefix,
        })
    return b.add("SaveAudioAdvanced", {
        "audio": audio_link, "filename_prefix": filename_prefix, "format": fmt,
    })


# --------------------------------------------------------------------------
# TTS
# --------------------------------------------------------------------------

@register(
    "audio.tts", group="audio", title="Речь из текста (TTS)",
    icon="fa-comment-dots", cost="medium", outputs=("audio",),
    description="Синтез речи локальной моделью Seed Audio",
    fields=[
        Field.textarea("text", "Текст",
                       "Привет! Это генерация речи из текста.",
                       placeholder="Что произнести?"),
        Field.select("reference_mode", "Режим", "zero_shot",
                     options=["zero_shot", "clone"],
                     hint="clone — голос с референса из загруженного файла"),
        Field.file("reference", "Референс голоса", "audio/*",
                   when={"reference_mode": "clone"}),
        Field.number("speech_rate", "Темп речи", 0, min=-50, max=50, step=1),
        Field.number("pitch_rate", "Высота тона", 0, min=-50, max=50, step=1),
        Field.number("loudness_rate", "Громкость", 0, min=-50, max=50, step=1),
        Field.text("model", "Модель / референс", "",
                   hint="Для zero_shot — модель, для clone — загруженный файл"),
        Field.seed(),
        Field.select("format", "Формат", "mp3", options=SAVE_AUDIO_FORMATS),
    ],
)
def tts(params):
    """Синтез речи."""
    text = (params.get("text") or "").strip()
    if not text:
        raise ValueError("Введите текст для озвучки")
    _require_node("ByteDanceSeedAudio",
                  "Нода ByteDanceSeedAudio недоступна в этой сборке ComfyUI. "
                  "Для офлайн-озвучки используйте встроенный TTS InfinitySearch "
                  "(speech_engine: Silero / Piper / Qwen3-TTS)")
    mode = params.get("reference_mode") or "zero_shot"
    reference = (params.get("reference") or "").strip()
    if mode == "clone" and not reference:
        raise ValueError("Для режима clone нужен загруженный референс голоса")

    b = GraphBuilder()
    inputs = {
        "text_prompt": text,
        "reference_mode": mode,
        "sample_rate": 24000,
        "speech_rate": int(params.get("speech_rate") or 0),
        "loudness_rate": int(params.get("loudness_rate") or 0),
        "pitch_rate": int(params.get("pitch_rate") or 0),
        "seed": seed_value(params.get("seed")),
    }
    if reference:
        inputs["model"] = reference
    node = b.add("ByteDanceSeedAudio", inputs)
    _save_audio(b, ref(node), params, "GenLab/speech")
    return {"graph": b.build(), "meta": {"kind": "audio", "operation": "tts"},
            "ui_workflow": None}


# --------------------------------------------------------------------------
# Музыка
# --------------------------------------------------------------------------

@register(
    "audio.music", group="audio", title="Генерация музыки (ACE-Step)",
    icon="fa-music", cost="high", outputs=("audio",),
    description="Полноценный трек по описанию: стиль, темп, тональность, текст",
    fields=[
        Field.select("engine", "Движок", "ACE-Step 1.5",
                     options=[t for _, t in MUSIC_ENGINES]),
        Field.textarea("prompt", "Описание трека",
                       "dreamy synthwave, warm analog pads, 100 bpm, instrumental"),
        Field.textarea("lyrics", "Текст песни", "",
                       placeholder="[verse]\nНочь превращается в свет…",
                       hint="Оставьте пустым для инструментала"),
        Field.number("seconds", "Длительность, с", 30, min=1, max=600, step=1),
        Field.number("bpm", "Темп", 100, min=30, max=240, step=1),
        Field.select("timesignature", "Размер", "4/4",
                     options=["4/4", "3/4", "6/8", "5/4"]),
        Field.text("language", "Язык текста", "en",
                   hint="en / ru / zh / ja — влияет на распознавание слов"),
        Field.text("keyscale", "Тональность", "C major"),
        Field.text("unet", "Модель (unet)", "",
                   hint="Файлы из models/diffusion_models"),
        Field.text("clip", "Text encoder", "",
                   hint="Загружается через DualCLIPLoader с типом ace"),
        Field.text("vae", "VAE", "", hint="Файлы из models/vae"),
        Field.checkbox("audio_codes", "Генерировать аудиокоды (медленнее)", False,
                       hint="LLM-этап: качественнее тексты, но заметно дольше"),
        Field.slider("cfg", "CFG", 5.0, 1.0, 15.0, 0.1),
        Field.slider("shift", "AuraFlow shift", 1.73, 0.0, 10.0, 0.01),
        Field.number("steps", "Шаги", 30, min=1, max=100, step=1),
        Field.slider("temperature", "Температура", 1.0, 0.1, 2.0, 0.05),
        Field.slider("top_p", "Top-p", 0.95, 0.1, 1.0, 0.01),
        Field.number("top_k", "Top-k", 50, min=1, max=1000, step=1),
        Field.seed(),
        Field.select("format", "Формат", "mp3", options=SAVE_AUDIO_FORMATS),
    ],
)
def music(params):
    """Музыка по текстовому описанию (ACE-Step 1.5).

    Граф повторяет официальный шаблон ComfyUI audio_ace_step1_5_xl_base:
    DualCLIPLoader(ace) даёт текстовый энкодер, TextEncodeAceStepAudio1.5 —
    кондишенинг, ConditioningZeroOut — негативный, UNETLoader с
    ModelSamplingAuraFlow — модель, EmptyAceStep1.5LatentAudio — латент.
    """
    engine = params.get("engine") or "ACE-Step 1.5"
    if "ACE-Step" not in engine:
        raise ValueError(
            "Поддерживается только ACE-Step 1.5: пайплайн собран по официальному "
            "шаблону ComfyUI. MiniMax Music и YuE 2 в ComfyUI собраны на "
            "подграфах — их проще запустить в самом ComfyUI")
    seconds = max(1, int(params.get("seconds") or 30))
    seed = seed_value(params.get("seed"))

    b = GraphBuilder()
    clip = b.add("DualCLIPLoader", {
        "clip_name1": pick_model(params.get("clip"), _models("text_encoders"), "clip"),
        "clip_name2": "", "type": "ace",
    })
    cond = b.add("TextEncodeAceStepAudio1.5", {
        "clip": ref(clip),
        "tags": params.get("prompt") or "",
        "lyrics": params.get("lyrics") or " ",
        "seed": seed,
        "bpm": float(params.get("bpm") or 100),
        "duration": float(seconds),
        "timesignature": params.get("timesignature") or "4/4",
        "language": params.get("language") or "en",
        "keyscale": params.get("keyscale") or "C major",
        "generate_audio_codes": bool(params.get("audio_codes", False)),
        "cfg_scale": float(params.get("cfg") or 5.0),
        "temperature": float(params.get("temperature") or 1.0),
        "top_p": float(params.get("top_p") or 0.95),
        "top_k": int(params.get("top_k") or 50),
        "min_p": float(params.get("min_p") or 0.05),
    })
    unet = b.add("UNETLoader", {
        "unet_name": pick_model(params.get("unet"), _models("diffusion_models"), "unet"),
        "weight_dtype": params.get("weight_dtype") or "default",
    })
    model = b.add("ModelSamplingAuraFlow", {
        "model": ref(unet), "shift": float(params.get("shift") or 1.73),
    })
    vae = b.add("VAELoader", {
        "vae_name": pick_model(params.get("vae"), _models("vae"), "vae"),
    })
    latent = b.add("EmptyAceStep1.5LatentAudio", {"seconds": seconds, "batch_size": 1})
    sampler = b.add("KSampler", {
        "model": ref(model),
        "seed": seed,
        "steps": int(params.get("steps") or 30),
        "cfg": float(params.get("cfg") or 5.0),
        "sampler_name": "euler", "scheduler": "normal",
        "positive": ref(cond),
        "negative": ref(b.add("ConditioningZeroOut", {"conditioning": ref(cond)})),
        "latent_image": ref(latent),
        "denoise": 1.0,
    })
    audio = b.add("VAEDecodeAudio", {"samples": ref(sampler), "vae": ref(vae)})
    _save_audio(b, ref(audio), params, "GenLab/music")
    return {"graph": b.build(),
            "meta": {"kind": "audio", "operation": "music",
                     "engine": "ace_step", "seconds": seconds},
            "ui_workflow": None}


# --------------------------------------------------------------------------
# Перерисовка участка (ACE-Step Repainting)
# --------------------------------------------------------------------------

@register(
    "audio.repaint", group="audio", title="Перерисовать участок трека",
    icon="fa-wand-magic-sparkles", cost="high", outputs=("audio",),
    description="ACE-Step перегенерирует выбранный по времени отрезок, "
                "оставляя остальное нетронутым",
    fields=[
        Field.file("audio", "Исходный трек", "audio/*"),
        Field.number("start", "Начало участка, с", 0, min=0, max=36000, step=1,
                     hint="Секунда от начала, где начинается перерисовка"),
        Field.number("end", "Конец участка, с", 10, min=1, max=36000, step=1),
        Field.textarea("prompt", "Теги и описание",
                       "pop, warm piano, female vocal, 110 bpm"),
        Field.textarea("lyrics", "Текст (пусто — сохранить прежний вокал)", " "),
        Field.text("negative", "Чего избегать", "noise, distortion, silence"),
        Field.slider("variance", "Сила изменений", 0.5, 0.01, 1.0, 0.01,
                     hint="Ниже — ближе к оригиналу, выше — свободнее"),
        Field.number("steps", "Шагов", 60, min=1, max=200, step=1),
        Field.number("guidance", "Guidance", 15.0, min=0.0, max=200.0, step=0.1),
        Field.select("scheduler", "Планировщик", "euler",
                     options=["euler", "heun", "pingpong"]),
        Field.select("cfg_type", "Тип CFG", "apg",
                     options=["cfg", "apg", "cfg_star"]),
        Field.seed(),
        Field.select("format", "Формат", "mp3", options=SAVE_AUDIO_FORMATS),
        Field.text("quality", "Битрейт", "192k"),
    ],
)
def repaint(params):
    """Перерисовка отрезка трека нодами ACE-Step (комьюнити-пак).

    Официальные ноды ComfyUI умеют только генерацию целиком, а Repainting
    (шум + маска во времени) живёт в паке ComfyUI_ACE-Step. Граф собирается
    так же, как в их примере: загрузчик моделей → параметры → «Аудио» →
    Repainting → сохранение. Если ноды или веса не установлены, штатная
    проверка графа скажет, чего именно не хватает.
    """
    name = params.get("audio") or ""
    if not name:
        raise ValueError("Загрузите исходный трек")

    start = int(params.get("start") or 0)
    end = int(params.get("end") or 0)
    if end <= start:
        raise ValueError("Конец участка должен быть позже начала")
    seed = seed_value(params.get("seed"))
    tags = params.get("prompt") or ""
    lyrics = params.get("lyrics") or " "
    negative = params.get("negative") or ""

    b = GraphBuilder()

    models = b.add("ACEModelLoader", {
        "dcae_checkpoint": params.get("dcae") or "music_dcae_f8c8",
        "vocoder_checkpoint": params.get("vocoder") or "music_vocoder",
        "ace_step_checkpoint": params.get("ace_checkpoint") or "ace_step_transformer",
        "text_encoder_checkpoint": params.get("text_encoder") or "umt5-base",
        "cpu_offload": False,
        "torch_compile": False,
    })
    prompt = b.add("MultiLinePromptACES", {"multi_line_prompt": tags})
    lyrics_node = b.add("MultiLineLyrics", {"multi_line_prompt": lyrics})

    gen_params = b.add("GenerationParameters", {
        "audio_duration": 0.0,
        "infer_step": int(params.get("steps") or 60),
        "guidance_scale": float(params.get("guidance") or 15.0),
        "scheduler_type": params.get("scheduler") or "euler",
        "cfg_type": params.get("cfg_type") or "apg",
        "omega_scale": 10.0,
        "seed": seed,
        "guidance_interval": 0.5,
        "guidance_interval_decay": 0.0,
        "min_guidance_scale": 3,
        "use_erg_tag": True,
        "use_erg_lyric": False,
        "use_erg_diffusion": True,
        "oss_steps": "",
        "guidance_scale_text": 0.0,
        "guidance_scale_lyric": 0.0,
    })

    src = b.add("LoadAudio", {"audio": name})
    repaint = b.add("ACEStepRepainting", {
        "models": ref(models),
        "src_audio": ref(src),
        "prompt": ref(prompt),
        "negative_prompt": negative,
        "lyrics": ref(lyrics_node),
        "parameters": ref(gen_params),
        "repaint_start": start,
        "repaint_end": end,
        "repaint_variance": float(params.get("variance") or 0.5),
        "seed": seed,
        "overlapped_decode": False,
    })
    _save_audio(b, ref(repaint), params, "GenLab/repaint")
    return {"graph": b.build(),
            "meta": {"kind": "audio", "operation": "repaint",
                     "engine": "ace_step", "start": start, "end": end},
            "ui_workflow": None}


# --------------------------------------------------------------------------
# Инструментальная обработка (модели не нужны)
# --------------------------------------------------------------------------

@register(
    "audio.process", group="audio", title="Обработка звука",
    icon="fa-sliders", cost="low", outputs=("audio",),
    description="Громкость, 3-полосный эквалайзер, обрезка",
    fields=[
        Field.file("audio", "Аудиофайл", "audio/*"),
        Field.slider("volume", "Громкость", 1.0, 0.0, 3.0, 0.05,
                     hint="1.0 — без изменений"),
        Field.checkbox("equalizer", "Эквалайзер", False),
        Field.slider("low_db", "Низкие, dB", 0.0, -24.0, 24.0, 0.5,
                     when={"equalizer": True}),
        Field.number("low_freq", "Низкие, Гц", 200, min=20, max=2000, step=10,
                     when={"equalizer": True}),
        Field.slider("mid_db", "Средние, dB", 0.0, -24.0, 24.0, 0.5,
                     when={"equalizer": True}),
        Field.number("mid_freq", "Средние, Гц", 1000, min=200, max=8000, step=50,
                     when={"equalizer": True}),
        Field.slider("high_db", "Верхние, dB", 0.0, -24.0, 24.0, 0.5,
                     when={"equalizer": True}),
        Field.number("high_freq", "Верхние, Гц", 4000, min=2000, max=16000, step=100,
                     when={"equalizer": True}),
        Field.checkbox("trim", "Обрезать", False),
        Field.number("start", "С начала, с", 0.0, min=0.0, max=36000.0, step=0.1,
                     when={"trim": True}),
        Field.number("duration", "Длительность, с", 30.0, min=0.1, max=36000.0,
                     step=0.1, when={"trim": True}),
        Field.select("format", "Формат", "mp3", options=SAVE_AUDIO_FORMATS),
        Field.text("quality", "Битрейт", "192k",
                   hint="Для mp3 и opus: 128k / 192k / 320k"),
    ],
)
def process(params):
    """Громкость + эквалайзер + обрезка."""
    name = params.get("audio") or ""
    if not name:
        raise ValueError("Загрузите аудиофайл")
    b = GraphBuilder()
    node = b.add("LoadAudio", {"audio": name})
    link = ref(node)

    volume = float(params.get("volume", 1.0))
    if abs(volume - 1.0) > 0.001:
        link = ref(b.add("AudioAdjustVolume", {"audio": link, "volume": volume}))

    if params.get("equalizer"):
        link = ref(b.add("AudioEqualizer3Band", {
            "audio": link,
            "low_gain_dB": float(params.get("low_db") or 0.0),
            "low_freq": float(params.get("low_freq") or 200),
            "mid_gain_dB": float(params.get("mid_db") or 0.0),
            "mid_freq": float(params.get("mid_freq") or 1000),
            "mid_q": float(params.get("mid_q") or 1.0),
            "high_gain_dB": float(params.get("high_db") or 0.0),
            "high_freq": float(params.get("high_freq") or 4000),
        }))

    if params.get("trim"):
        link = ref(b.add("TrimAudioDuration", {
            "audio": link,
            "start_index": float(params.get("start") or 0.0),
            "duration": float(params.get("duration") or 30.0),
        }))

    _save_audio(b, link, params, "GenLab/processed")
    return {"graph": b.build(),
            "meta": {"kind": "audio", "operation": "process"},
            "ui_workflow": None}


@register(
    "audio.merge", group="audio", title="Склейка и микс аудио",
    icon="fa-layer-group", cost="low", outputs=("audio",),
    description="Последовательная склейка треков или сведение в один файл",
    fields=[
        Field.file("audio1", "Трек 1", "audio/*"),
        Field.file("audio2", "Трек 2", "audio/*"),
        Field.select("mode", "Режим", "concat",
                     options=["concat", "add", "average", "multiply"],
                     hint="concat — один за другим; остальные — наложение"),
        Field.select("format", "Формат", "mp3", options=SAVE_AUDIO_FORMATS),
    ],
)
def merge(params):
    """Склейка или микс двух треков."""
    a1 = params.get("audio1") or ""
    a2 = params.get("audio2") or ""
    if not a1 or not a2:
        raise ValueError("Нужны два аудиофайла")
    b = GraphBuilder()
    first = b.add("LoadAudio", {"audio": a1})
    second = b.add("LoadAudio", {"audio": a2})
    mode = params.get("mode") or "concat"
    if mode == "concat":
        node = b.add("AudioConcat", {"audio1": ref(first), "audio2": ref(second),
                                     "direction": "backward"})
    else:
        node = b.add("AudioMerge", {"audio1": ref(first), "audio2": ref(second),
                                    "merge_method": mode})
    _save_audio(b, ref(node), params, "GenLab/merged")
    return {"graph": b.build(),
            "meta": {"kind": "audio", "operation": "merge", "mode": mode},
            "ui_workflow": None}


@register(
    "audio.sfx", group="audio", title="Звуки из описания",
    icon="fa-volume-high", cost="high", outputs=("audio",),
    description="Генерация звуковых эффектов и атмосфер (нужна локальная модель)",
    fields=[
        Field.textarea("prompt", "Описание звука",
                       "heavy rain on a metal roof with distant thunder"),
        Field.number("seconds", "Длительность, с", 5, min=1, max=120, step=1),
        Field.select("format", "Формат", "mp3", options=SAVE_AUDIO_FORMATS),
    ],
)
def sfx(params):
    """Генерация звукового эффекта."""
    text = (params.get("prompt") or "").strip()
    if not text:
        raise ValueError("Опишите звук")
    _require_node("SoniloTextToMusic",
                  "Нода SoniloTextToMusic недоступна. Эффекты можно собрать из "
                  "библиотеки или использовать облачные ноды в самом ComfyUI")
    b = GraphBuilder()
    node = b.add("SoniloTextToMusic", {
        "prompt": text,
        "duration": float(params.get("seconds") or 5.0),
        "seed": seed_value(params.get("seed")),
    })
    _save_audio(b, ref(node), params, "GenLab/sfx")
    return {"graph": b.build(), "meta": {"kind": "audio", "operation": "sfx"},
            "ui_workflow": None}