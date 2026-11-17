#!/usr/bin/env python3
import os, sys, subprocess, glob, json, re, logging, socket, urllib.request, time, threading, shlex, asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger("infinity.aiengine")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from system_specs import get_system_specs
import swallow

router = APIRouter(prefix="/api/ai-engine", tags=["AI Engine"])

# Каталог с исполняемыми файлами движка (llama-server.exe, *.dll) и моделями gguf.
# В переносимой сборке он лежит рядом с кодом — <корень>/engine, поэтому путь
# задаётся через INFINITY_ENGINE_DIR (по умолчанию <корень>/engine).
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _engine_dir():
    env = os.environ.get("INFINITY_ENGINE_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "engine")
    except Exception:
        swallow.note('api._engine_dir:L28')
        return os.path.join(_APP_ROOT, "aiengine")


_AIENGINE_DIR = _engine_dir()
_SERVER_EXE = os.path.join(_AIENGINE_DIR, "llama-server.exe")
# Конфиг движка: сначала ищем рядом с бинарниками (движок/),
# иначе в каталоге кода — чтобы перенос папки не терял настройки.
if os.path.exists(os.path.join(_AIENGINE_DIR, "last_config.ini")):
    _CONFIG_FILE = os.path.join(_AIENGINE_DIR, "last_config.ini")
else:
    _CONFIG_FILE = os.path.join(_APP_ROOT, "aiengine", "last_config.ini")
# Модели gguf живут в <корень>/models/gguf (INFINITY_MODELS_DIR переопределяет).
if os.environ.get("INFINITY_MODELS_DIR"):
    _MODEL_DIR = os.path.join(os.path.abspath(os.environ["INFINITY_MODELS_DIR"]), "gguf")
else:
    try:
        from config_module import _root
        _MODEL_DIR = os.path.join(_root(), "models", "gguf")
    except Exception:
        swallow.note('api.<module>:L48')
        _MODEL_DIR = os.path.join(_AIENGINE_DIR, "model")
_ENGINE_LOG = os.path.join(_AIENGINE_DIR, "engine.log")
_ENGINE_LOG_MAX = 10 * 1024 * 1024

_engine_process = None
_engine_log_fh = None


class ConfigUpdate(BaseModel):
    MODEL: Optional[str] = None
    DRAFT_MODEL: Optional[str] = None
    SPEC_TYPE: Optional[str] = None
    CTX: Optional[int] = None
    CACHE_TYPE: Optional[str] = None
    MTP: Optional[str] = None
    MTP_MAX: Optional[int] = None
    FLASH_ATTN: Optional[str] = None
    NO_MMAP: Optional[str] = None
    VISION: Optional[str] = None
    BATCH: Optional[int] = None
    UBATCH: Optional[int] = None
    NGL: Optional[int] = None
    PORT: Optional[int] = None
    PARALLEL: Optional[int] = None
    MODEL_DIR: Optional[str] = None
    LOAD_MODE: Optional[str] = None
    THREADS: Optional[int] = None
    THREADS_BATCH: Optional[int] = None
    FIT: Optional[str] = None
    DEVICE: Optional[str] = None
    SPLIT_MODE: Optional[str] = None
    N_CPU_MOE: Optional[int] = None
    SPEC_DRAFT_N_MIN: Optional[int] = None
    SPEC_DRAFT_P_SPLIT: Optional[float] = None
    REASONING: Optional[str] = None
    REASONING_BUDGET: Optional[int] = None
    CACHE_RAM: Optional[int] = None
    CTX_CHECKPOINTS: Optional[int] = None
    TEMPERATURE: Optional[float] = None
    TOP_K: Optional[int] = None
    TOP_P: Optional[float] = None
    MIN_P: Optional[float] = None
    TYPICAL_P: Optional[float] = None
    REPEAT_PENALTY: Optional[float] = None
    REPEAT_LAST_N: Optional[int] = None
    FREQUENCY_PENALTY: Optional[float] = None
    PRESENCE_PENALTY: Optional[float] = None
    SEED: Optional[int] = None
    MIROSTAT: Optional[int] = None
    MIROSTAT_LR: Optional[float] = None
    MIROSTAT_ENT: Optional[float] = None
    DYNATEMP_RANGE: Optional[float] = None
    DYNATEMP_EXP: Optional[float] = None
    DRY_MULTIPLIER: Optional[float] = None
    DRY_BASE: Optional[float] = None
    DRY_ALLOWED_LENGTH: Optional[int] = None
    SAMPLER_SEQ: Optional[str] = None
    CUSTOM_CMD: Optional[str] = None
    # --- llama.cpp build >=10938: MoE / GPU ---
    CPU_MOE: Optional[str] = None
    N_CPU_FFN: Optional[int] = None
    TENSOR_SPLIT: Optional[str] = None
    MAIN_GPU: Optional[int] = None
    OVERRIDE_TENSOR: Optional[str] = None
    LAZY_MODE: Optional[str] = None
    NUMA: Optional[str] = None
    NO_KV_OFFLOAD: Optional[str] = None
    NO_REPACK: Optional[str] = None
    NO_HOST: Optional[str] = None
    SWA_FULL: Optional[str] = None
    OP_OFFLOAD: Optional[str] = None
    FIT_TARGET: Optional[str] = None
    FIT_CTX: Optional[int] = None
    # --- draft / MTP ---
    SPEC_DRAFT_NGL: Optional[int] = None
    SPEC_DRAFT_DEVICE: Optional[str] = None
    SPEC_DRAFT_N_CPU_MOE: Optional[int] = None
    SPEC_DRAFT_P_MIN: Optional[float] = None
    SPEC_DRAFT_CACHE_K: Optional[str] = None
    SPEC_DRAFT_CACHE_V: Optional[str] = None
    # --- ngram speculative decoding ---
    SPEC_NGRAM_MOD_N_MIN: Optional[int] = None
    SPEC_NGRAM_MOD_N_MAX: Optional[int] = None
    SPEC_NGRAM_MOD_N_MATCH: Optional[int] = None
    SPEC_NGRAM_SIMPLE_N: Optional[int] = None
    SPEC_NGRAM_SIMPLE_M: Optional[int] = None
    SPEC_NGRAM_SIMPLE_MIN_HITS: Optional[int] = None
    SPEC_NGRAM_MAP_K_N: Optional[int] = None
    SPEC_NGRAM_MAP_K_M: Optional[int] = None
    SPEC_NGRAM_MAP_K_MIN_HITS: Optional[int] = None
    SPEC_NGRAM_MAP_K4V_N: Optional[int] = None
    SPEC_NGRAM_MAP_K4V_M: Optional[int] = None
    SPEC_NGRAM_MAP_K4V_MIN_HITS: Optional[int] = None
    # --- extended sampling ---
    TOP_N_SIGMA: Optional[float] = None
    XTC_PROBABILITY: Optional[float] = None
    XTC_THRESHOLD: Optional[float] = None
    ADAPTIVE_TARGET: Optional[float] = None
    ADAPTIVE_DECAY: Optional[float] = None
    DRY_PENALTY_LAST_N: Optional[int] = None
    DRY_SEQUENCE_BREAKER: Optional[str] = None
    SAMPLERS: Optional[str] = None
    # --- reasoning / chat ---
    REASONING_EFFORT: Optional[str] = None
    REASONING_BUDGET_MESSAGE: Optional[str] = None
    REASONING_PRESERVE: Optional[str] = None
    JINJA: Optional[str] = None
    CHAT_TEMPLATE: Optional[str] = None
    # --- cache / server ---
    CACHE_REUSE: Optional[int] = None
    CACHE_PROMPT: Optional[str] = None
    CONTEXT_SHIFT: Optional[str] = None
    KV_UNIFIED: Optional[str] = None
    KV_UNIFIED_PER_SLOT: Optional[int] = None
    CACHE_IDLE_SLOTS: Optional[str] = None
    CHECKPOINT_MIN_STEP: Optional[int] = None
    CONT_BATCHING: Optional[str] = None
    WARMUP: Optional[str] = None
    LORA: Optional[str] = None
    LORA_SCALED: Optional[str] = None
    THREADS_HTTP: Optional[int] = None
    TIMEOUT: Optional[int] = None
    SPEC_DEFAULT: Optional[str] = None
    IGNORE_EOS: Optional[str] = None


def _is_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except:
        swallow.note('api._is_wsl:L180')
        return False

_IS_WSL = _is_wsl()

def _kill_engine():
    """Best-effort kill of any existing llama-server process (no error if none)."""
    global _engine_process, _engine_log_fh
    if _engine_process:
        try:
            _engine_process.terminate()
            _engine_process.wait(timeout=2)
        except Exception:
            swallow.note('api._kill_engine:L193')
            try:
                _engine_process.kill()
                _engine_process.wait(timeout=1)
            except Exception:
                swallow.note('api._kill_engine:L198')
                pass
        _engine_process = None
    if _engine_log_fh is not None:
        try:
            _engine_log_fh.close()
        except Exception:
            swallow.note('api._kill_engine:L205')
            pass
        _engine_log_fh = None
    try:
        if _IS_WSL:
            out = subprocess.check_output("pgrep -f 'llama-server' 2>/dev/null || true", shell=True, timeout=5).decode().strip()
            if out:
                for pid in out.splitlines():
                    try:
                        os.kill(int(pid), 9)
                    except Exception:
                        swallow.note('api._kill_engine:L216')
                        pass
        else:
            subprocess.run("taskkill /F /IM llama-server.exe", shell=True, timeout=5, stderr=subprocess.DEVNULL)
    except Exception:
        swallow.note('api._kill_engine:L221')
        pass

def _open_engine_log():
    """Open (and rotate if needed) the engine log file for appending."""
    global _engine_log_fh
    if _engine_log_fh is not None:
        try:
            _engine_log_fh.close()
        except Exception:
            swallow.note('api._open_engine_log:L231')
            pass
        _engine_log_fh = None
    try:
        if os.path.exists(_ENGINE_LOG) and os.path.getsize(_ENGINE_LOG) > _ENGINE_LOG_MAX:
            try:
                os.replace(_ENGINE_LOG, _ENGINE_LOG + ".1")
            except Exception:
                swallow.note('api._open_engine_log:L239')
                pass
        _engine_log_fh = open(_ENGINE_LOG, "ab", buffering=0)
    except Exception as e:
        logger.warning(f"Cannot open engine log {_ENGINE_LOG}: {e}")
        _engine_log_fh = None
    return _engine_log_fh

def _log_engine_line(msg: str):
    """Write a marker line into the raw engine log."""
    try:
        if _engine_log_fh is not None:
            _engine_log_fh.write(f"\n===== {msg} =====\n".encode("utf-8", errors="replace"))
    except Exception:
        swallow.note('api._log_engine_line:L253')
        pass

def _path_from_wsl(path: str) -> str:
    if not _IS_WSL:
        return path
    m = re.match(r'^/mnt/([a-z])/(.*)', path)
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).replace("/", "\\")
        return drive + ":\\" + rest
    return path


def _read_config():
    config = {
        "MODEL": "", "DRAFT_MODEL": "", "SPEC_TYPE": "draft-simple",
        "CTX": 4096, "CACHE_TYPE": "off", "MTP": "on", "MTP_MAX": 2,
        "FLASH_ATTN": "on", "NO_MMAP": "on", "VISION": "off",
        "BATCH": 512, "UBATCH": 512, "NGL": 99, "PORT": 8080, "PARALLEL": 1,
        "MODEL_DIR": "",
        "LOAD_MODE": "auto", "THREADS": -1, "THREADS_BATCH": -1, "FIT": "on",
        "DEVICE": "", "SPLIT_MODE": "", "N_CPU_MOE": 0,
        "SPEC_DRAFT_N_MIN": 0, "SPEC_DRAFT_P_SPLIT": 0.10,
        "REASONING": "auto", "REASONING_BUDGET": -1,
        "CACHE_RAM": -1, "CTX_CHECKPOINTS": 32,
        "TEMPERATURE": 0.8, "TOP_K": 40, "TOP_P": 0.95, "MIN_P": 0.05,
        "TYPICAL_P": 1.0, "REPEAT_PENALTY": 1.0, "REPEAT_LAST_N": 64,
        "FREQUENCY_PENALTY": 0.0, "PRESENCE_PENALTY": 0.0, "SEED": -1,
        "MIROSTAT": 0, "MIROSTAT_LR": 0.1, "MIROSTAT_ENT": 5.0,
        "DYNATEMP_RANGE": 0.0, "DYNATEMP_EXP": 1.0,
        "DRY_MULTIPLIER": 0.0, "DRY_BASE": 1.75, "DRY_ALLOWED_LENGTH": 2,
        "SAMPLER_SEQ": "",
        "CUSTOM_CMD": "",
        # --- llama.cpp build >=10938: MoE / GPU ---
        "CPU_MOE": "off", "N_CPU_FFN": 0, "TENSOR_SPLIT": "", "MAIN_GPU": 0,
        "OVERRIDE_TENSOR": "", "LAZY_MODE": "", "NUMA": "",
        "NO_KV_OFFLOAD": "off", "NO_REPACK": "off", "NO_HOST": "off",
        "SWA_FULL": "off", "OP_OFFLOAD": "on", "FIT_TARGET": "", "FIT_CTX": 0,
        # --- draft / MTP ---
        "SPEC_DRAFT_NGL": 0, "SPEC_DRAFT_DEVICE": "", "SPEC_DRAFT_N_CPU_MOE": 0,
        "SPEC_DRAFT_P_MIN": 0.0, "SPEC_DRAFT_CACHE_K": "", "SPEC_DRAFT_CACHE_V": "",
        # --- ngram speculative decoding ---
        "SPEC_NGRAM_MOD_N_MIN": 0, "SPEC_NGRAM_MOD_N_MAX": 0, "SPEC_NGRAM_MOD_N_MATCH": 0,
        "SPEC_NGRAM_SIMPLE_N": 0, "SPEC_NGRAM_SIMPLE_M": 0, "SPEC_NGRAM_SIMPLE_MIN_HITS": 0,
        "SPEC_NGRAM_MAP_K_N": 0, "SPEC_NGRAM_MAP_K_M": 0, "SPEC_NGRAM_MAP_K_MIN_HITS": 0,
        "SPEC_NGRAM_MAP_K4V_N": 0, "SPEC_NGRAM_MAP_K4V_M": 0, "SPEC_NGRAM_MAP_K4V_MIN_HITS": 0,
        # --- extended sampling ---
        "TOP_N_SIGMA": -1.0, "XTC_PROBABILITY": 0.0, "XTC_THRESHOLD": 0.1,
        "ADAPTIVE_TARGET": -1.0, "ADAPTIVE_DECAY": 0.9,
        "DRY_PENALTY_LAST_N": 0, "DRY_SEQUENCE_BREAKER": "", "SAMPLERS": "",
        # --- reasoning / chat ---
        "REASONING_EFFORT": "", "REASONING_BUDGET_MESSAGE": "", "REASONING_PRESERVE": "",
        "JINJA": "on", "CHAT_TEMPLATE": "",
        # --- cache / server ---
        "CACHE_REUSE": 0, "CACHE_PROMPT": "on", "CONTEXT_SHIFT": "off",
        "KV_UNIFIED": "", "KV_UNIFIED_PER_SLOT": 0, "CACHE_IDLE_SLOTS": "",
        "CHECKPOINT_MIN_STEP": 0, "CONT_BATCHING": "on", "WARMUP": "on",
        "LORA": "", "LORA_SCALED": "", "THREADS_HTTP": 0, "TIMEOUT": 0,
        "SPEC_DEFAULT": "off", "IGNORE_EOS": "off",
    }
    if not os.path.exists(_CONFIG_FILE):
        return config
    loaded = set()
    with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k in config:
                    loaded.add(k)
                    if isinstance(config[k], int):
                        try:
                            config[k] = int(v)
                        except ValueError:
                            swallow.note('api._read_config:L330')
                            pass
                    elif isinstance(config[k], float):
                        try:
                            config[k] = float(v)
                        except ValueError:
                            swallow.note('api._read_config:L336')
                            pass
                    else:
                        config[k] = v
    if "LOAD_MODE" not in loaded:
        # Миграция со старого NO_MMAP (--no-mmap теперь deprecated).
        config["LOAD_MODE"] = "none" if config.get("NO_MMAP") == "on" else "auto"

    # Пути, оставшиеся от Linux/WSL-версии сборки, на Windows не работают:
    #   MODEL=/mnt/e/models/.../model.gguf   — файла по такому пути нет
    #   MODEL_DIR=E:\ISTA-DASlab\...             — папка вне переносимой сборки
    # Раньше они молча оставались в конфиге, и запуск движка из UI падал с
    # «No model found». Теперь проверяем и очищаем то, чего на диске нет,
    # чтобы движок подхватил модель из models/gguf.
    if not _IS_WSL:
        model = (config.get("MODEL") or "").strip()
        if model and not _exists_any(model):
            logger.warning("[ai-engine] MODEL не найден (%s) — будет выбрана "
                           "модель из models/gguf", model)
            config["MODEL"] = ""
            _DIRTY["config"] = True
        mdir = (config.get("MODEL_DIR") or "").strip()
        if mdir and not os.path.isdir(mdir):
            logger.warning("[ai-engine] MODEL_DIR не найден (%s) — "
                           "используется models/gguf", mdir)
            config["MODEL_DIR"] = ""
            _DIRTY["config"] = True
        # Сохраняем очистку. Раньше путь правился только в памяти, файл
        # оставался прежним, и предупреждение повторялось на каждом чтении
        # конфига (раз в 1-2 секунды), забивая лог.
        if _DIRTY.pop("config", False):
            try:
                _write_config(config)
            except Exception as e:
                logger.warning("[ai-engine] не удалось сохранить конфиг: %s", e)
    return config


def _write_config(config: dict):
    content = "\n".join(f"{k}={v}" for k, v in config.items())
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(content)
        f.write("\n")


def _wsl_path(path):
    m = re.match(r'^([A-Za-z]):\\(.*)', path)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return path

# Флаг «конфиг правился в памяти и его надо записать на диск».
_DIRTY = {}

def _exists_any(path):
    if os.path.exists(path):
        return True
    if _IS_WSL:
        wsl = _wsl_path(path)
        return wsl != path and os.path.exists(wsl)
    return False


def _model_dir(config=None):
    """Папка с моделями. Пустое MODEL_DIR = стандартная aiengine/model.
    Относительные пути резолвятся от корня проекта."""
    d = (config or _read_config()).get("MODEL_DIR", "") or ""
    d = d.strip().strip('"').strip("'")
    if not d:
        return _MODEL_DIR
    if not os.path.isabs(d):
        d = os.path.join(os.path.dirname(_AIENGINE_DIR), d)
    return d

def _build_command(config: dict):
    model = config.get("MODEL", "")
    if not model or not _exists_any(model):
        models = _list_models()
        if not models:
            return None
        model = models[0]

    if not _exists_any(model):
        return None

    model_win = _path_from_wsl(model)

    cmd = [
        _SERVER_EXE,
        "-m", model_win,
        "-ngl", str(config.get("NGL", 99)),
        "-c", str(config.get("CTX", 4096)),
        "-b", str(config.get("BATCH", 512)),
        "-ub", str(config.get("UBATCH", 512)),
        "--host", "0.0.0.0",
        "--port", str(config.get("PORT", 8080)),
        "--parallel", str(config.get("PARALLEL", 1))
    ]

    cmd.append("--slots")
    cmd.append("--metrics")

    if config.get("FLASH_ATTN") == "on":
        cmd.append("--flash-attn")
        cmd.append("on")
    if config.get("VISION") == "off":
        cmd.append("--no-mmproj")

    cache_type = config.get("CACHE_TYPE", "off")
    if cache_type != "off":
        cmd.append("--cache-type-k")
        cmd.append(cache_type)
        cmd.append("--cache-type-v")
        cmd.append(cache_type)

    load_mode = str(config.get("LOAD_MODE", "") or "").strip()
    if load_mode and load_mode != "auto":
        cmd.extend(["--load-mode", load_mode])

    threads = int(config.get("THREADS", -1) or -1)
    if threads > 0:
        cmd.extend(["-t", str(threads)])
    threads_batch = int(config.get("THREADS_BATCH", -1) or -1)
    if threads_batch > 0:
        cmd.extend(["-tb", str(threads_batch)])

    if str(config.get("FIT", "on") or "on") == "off":
        cmd.extend(["--fit", "off"])

    device = str(config.get("DEVICE", "") or "").strip()
    if device:
        cmd.extend(["--device", device])

    split_mode = str(config.get("SPLIT_MODE", "") or "").strip()
    if split_mode:
        cmd.extend(["--split-mode", split_mode])

    n_cpu_moe = int(config.get("N_CPU_MOE", 0) or 0)
    if n_cpu_moe > 0:
        cmd.extend(["--n-cpu-moe", str(n_cpu_moe)])

    reasoning = str(config.get("REASONING", "auto") or "auto").strip()
    if reasoning and reasoning != "auto":
        cmd.extend(["--reasoning", reasoning])
    reasoning_budget = int(config.get("REASONING_BUDGET", -1) or -1)
    if reasoning_budget >= 0:
        cmd.extend(["--reasoning-budget", str(reasoning_budget)])

    cache_ram = int(config.get("CACHE_RAM", -1) or -1)
    if cache_ram >= 0:
        cmd.extend(["--cache-ram", str(cache_ram)])

    ctx_checkpoints = int(config.get("CTX_CHECKPOINTS", 0) or 0)
    if ctx_checkpoints > 0:
        cmd.extend(["--ctx-checkpoints", str(ctx_checkpoints)])

    draft = config.get("DRAFT_MODEL", "")
    mtp = config.get("MTP", "off")
    mtp_max = config.get("MTP_MAX", 2)
    spec_type = str(config.get("SPEC_TYPE", "draft-simple") or "").strip()
    spec_standalone = ("draft-eagle3", "draft-dflash", "draft-dspark",
                       "ngram-simple", "ngram-map-k", "ngram-map-k4v",
                       "ngram-mod", "ngram-cache")

    if draft and _exists_any(draft):
        cmd.extend(["-md", _path_from_wsl(draft)])
        spec = spec_type if spec_type not in ("", "none") else "draft-simple"
        cmd.extend(["--spec-type", spec, "--spec-draft-n-max", str(mtp_max)])
        n_min = int(config.get("SPEC_DRAFT_N_MIN", 0) or 0)
        if n_min > 0:
            cmd.extend(["--spec-draft-n-min", str(n_min)])
        p_split = float(config.get("SPEC_DRAFT_P_SPLIT", 0.10) or 0.10)
        if p_split != 0.10:
            cmd.extend(["--spec-draft-p-split", str(p_split)])
    elif spec_type in spec_standalone:
        cmd.extend(["--spec-type", spec_type, "--spec-draft-n-max", str(mtp_max)])
    elif spec_type == "draft-mtp" or mtp == "on":
        cmd.extend(["--spec-type", "draft-mtp", "--spec-draft-n-max", str(mtp_max)])

    temperature = float(config.get("TEMPERATURE", 0.8) or 0.8)
    if temperature != 0.8:
        cmd.extend(["--temperature", str(temperature)])

    top_k = int(config.get("TOP_K", 40) or 40)
    if top_k != 40:
        cmd.extend(["--top-k", str(top_k)])

    top_p = float(config.get("TOP_P", 0.95) or 0.95)
    if top_p != 0.95:
        cmd.extend(["--top-p", str(top_p)])

    min_p = float(config.get("MIN_P", 0.05) or 0.05)
    if min_p != 0.05:
        cmd.extend(["--min-p", str(min_p)])

    typical_p = float(config.get("TYPICAL_P", 1.0) or 1.0)
    if typical_p != 1.0:
        cmd.extend(["--typical-p", str(typical_p)])

    repeat_penalty = float(config.get("REPEAT_PENALTY", 1.0) or 1.0)
    if repeat_penalty != 1.0:
        cmd.extend(["--repeat-penalty", str(repeat_penalty)])

    repeat_last_n = int(config.get("REPEAT_LAST_N", 64) or 64)
    if repeat_last_n != 64:
        cmd.extend(["--repeat-last-n", str(repeat_last_n)])

    frequency_penalty = float(config.get("FREQUENCY_PENALTY", 0.0) or 0.0)
    if frequency_penalty != 0.0:
        cmd.extend(["--frequency-penalty", str(frequency_penalty)])

    presence_penalty = float(config.get("PRESENCE_PENALTY", 0.0) or 0.0)
    if presence_penalty != 0.0:
        cmd.extend(["--presence-penalty", str(presence_penalty)])

    seed = int(config.get("SEED", -1) or -1)
    if seed != -1:
        cmd.extend(["--seed", str(seed)])

    mirostat = int(config.get("MIROSTAT", 0) or 0)
    if mirostat != 0:
        cmd.extend(["--mirostat", str(mirostat)])
        mirostat_lr = float(config.get("MIROSTAT_LR", 0.1) or 0.1)
        if mirostat_lr != 0.1:
            cmd.extend(["--mirostat-lr", str(mirostat_lr)])
        mirostat_ent = float(config.get("MIROSTAT_ENT", 5.0) or 5.0)
        if mirostat_ent != 5.0:
            cmd.extend(["--mirostat-ent", str(mirostat_ent)])

    dynatemp_range = float(config.get("DYNATEMP_RANGE", 0.0) or 0.0)
    if dynatemp_range != 0.0:
        cmd.extend(["--dynatemp-range", str(dynatemp_range)])
        dynatemp_exp = float(config.get("DYNATEMP_EXP", 1.0) or 1.0)
        if dynatemp_exp != 1.0:
            cmd.extend(["--dynatemp-exp", str(dynatemp_exp)])

    dry_multiplier = float(config.get("DRY_MULTIPLIER", 0.0) or 0.0)
    if dry_multiplier != 0.0:
        cmd.extend(["--dry-multiplier", str(dry_multiplier)])
        dry_base = float(config.get("DRY_BASE", 1.75) or 1.75)
        if dry_base != 1.75:
            cmd.extend(["--dry-base", str(dry_base)])
        dry_allowed = int(config.get("DRY_ALLOWED_LENGTH", 2) or 2)
        if dry_allowed != 2:
            cmd.extend(["--dry-allowed-length", str(dry_allowed)])

    sampler_seq = str(config.get("SAMPLER_SEQ", "") or "").strip()
    if sampler_seq:
        cmd.extend(["--sampler-seq", sampler_seq])

    # ============================================================
    # llama.cpp build >= 10938: новые параметры
    # ============================================================

    def _opt(key, flag, default=0):
        v = config.get(key, default)
        if v is None:
            return
        try:
            v = int(v)
        except (TypeError, ValueError):
            swallow.note('api._opt:L599')
            return
        if v > 0:
            cmd.extend([flag, str(v)])

    def _stropt(key, flag):
        v = str(config.get(key, "") or "").strip()
        if v:
            cmd.extend([flag, v])

    # --- MoE / GPU ---
    if str(config.get("CPU_MOE", "off") or "off") == "on":
        cmd.append("--cpu-moe")
    _opt("N_CPU_FFN", "--n-cpu-ffn")
    _stropt("TENSOR_SPLIT", "--tensor-split")
    main_gpu = int(config.get("MAIN_GPU", 0) or 0)
    if main_gpu > 0:
        cmd.extend(["--main-gpu", str(main_gpu)])
    _stropt("OVERRIDE_TENSOR", "--override-tensor")
    _stropt("LAZY_MODE", "--lazy-mode")
    _stropt("NUMA", "--numa")
    if str(config.get("NO_KV_OFFLOAD", "off") or "off") == "on":
        cmd.append("--no-kv-offload")
    if str(config.get("NO_REPACK", "off") or "off") == "on":
        cmd.append("--no-repack")
    if str(config.get("NO_HOST", "off") or "off") == "on":
        cmd.append("--no-host")
    if str(config.get("SWA_FULL", "off") or "off") == "on":
        cmd.append("--swa-full")
    if str(config.get("OP_OFFLOAD", "on") or "on") == "off":
        cmd.append("--no-op-offload")
    _stropt("FIT_TARGET", "--fit-target")
    fit_ctx = int(config.get("FIT_CTX", 0) or 0)
    if fit_ctx > 0:
        cmd.extend(["--fit-ctx", str(fit_ctx)])

    # --- draft / MTP ---
    draft_ngl = int(config.get("SPEC_DRAFT_NGL", 0) or 0)
    if draft_ngl > 0:
        cmd.extend(["--gpu-layers-draft", str(draft_ngl)])
    _stropt("SPEC_DRAFT_DEVICE", "--device-draft")
    draft_ncmoe = int(config.get("SPEC_DRAFT_N_CPU_MOE", 0) or 0)
    if draft_ncmoe > 0:
        cmd.extend(["--n-cpu-moe-draft", str(draft_ncmoe)])
    draft_p_min = float(config.get("SPEC_DRAFT_P_MIN", 0.0) or 0.0)
    if draft_p_min > 0.0:
        cmd.extend(["--spec-draft-p-min", str(draft_p_min)])
    _stropt("SPEC_DRAFT_CACHE_K", "--cache-type-k-draft")
    _stropt("SPEC_DRAFT_CACHE_V", "--cache-type-v-draft")

    # --- ngram speculative decoding ---
    _opt("SPEC_NGRAM_MOD_N_MIN", "--spec-ngram-mod-n-min")
    _opt("SPEC_NGRAM_MOD_N_MAX", "--spec-ngram-mod-n-max")
    _opt("SPEC_NGRAM_MOD_N_MATCH", "--spec-ngram-mod-n-match")
    _opt("SPEC_NGRAM_SIMPLE_N", "--spec-ngram-simple-size-n")
    _opt("SPEC_NGRAM_SIMPLE_M", "--spec-ngram-simple-size-m")
    _opt("SPEC_NGRAM_SIMPLE_MIN_HITS", "--spec-ngram-simple-min-hits")
    _opt("SPEC_NGRAM_MAP_K_N", "--spec-ngram-map-k-size-n")
    _opt("SPEC_NGRAM_MAP_K_M", "--spec-ngram-map-k-size-m")
    _opt("SPEC_NGRAM_MAP_K_MIN_HITS", "--spec-ngram-map-k-min-hits")
    _opt("SPEC_NGRAM_MAP_K4V_N", "--spec-ngram-map-k4v-size-n")
    _opt("SPEC_NGRAM_MAP_K4V_M", "--spec-ngram-map-k4v-size-m")
    _opt("SPEC_NGRAM_MAP_K4V_MIN_HITS", "--spec-ngram-map-k4v-min-hits")

    # --- расширенный сэмплинг ---
    top_n_sigma = float(config.get("TOP_N_SIGMA") if config.get("TOP_N_SIGMA") is not None else -1.0)
    if top_n_sigma >= 0.0:
        cmd.extend(["--top-n-sigma", str(top_n_sigma)])
    xtc_prob = float(config.get("XTC_PROBABILITY", 0.0) or 0.0)
    if xtc_prob > 0.0:
        cmd.extend(["--xtc-probability", str(xtc_prob)])
        xtc_thr = float(config.get("XTC_THRESHOLD", 0.1) or 0.1)
        if xtc_thr != 0.1:
            cmd.extend(["--xtc-threshold", str(xtc_thr)])
    adaptive_target = float(config.get("ADAPTIVE_TARGET") if config.get("ADAPTIVE_TARGET") is not None else -1.0)
    if adaptive_target >= 0.0:
        cmd.extend(["--adaptive-target", str(adaptive_target)])
        adaptive_decay = float(config.get("ADAPTIVE_DECAY", 0.9) or 0.9)
        if adaptive_decay != 0.9:
            cmd.extend(["--adaptive-decay", str(adaptive_decay)])
    dry_last_n = int(config.get("DRY_PENALTY_LAST_N", 0) or 0)
    if dry_last_n > 0:
        cmd.extend(["--dry-penalty-last-n", str(dry_last_n)])
    _stropt("DRY_SEQUENCE_BREAKER", "--dry-sequence-breaker")
    _stropt("SAMPLERS", "--samplers")

    # --- reasoning / chat ---
    reasoning_effort = str(config.get("REASONING_EFFORT", "") or "").strip()
    if reasoning_effort and reasoning_effort != "default":
        cmd.extend(["--reasoning-effort", reasoning_effort])
    _stropt("REASONING_BUDGET_MESSAGE", "--reasoning-budget-message")
    if str(config.get("REASONING_PRESERVE", "") or "") == "off":
        cmd.append("--no-reasoning-preserve")
    if str(config.get("JINJA", "on") or "on") == "off":
        cmd.append("--no-jinja")
    _stropt("CHAT_TEMPLATE", "--chat-template")

    # --- кеш / сервер ---
    cache_reuse = int(config.get("CACHE_REUSE", 0) or 0)
    if cache_reuse > 0:
        cmd.extend(["--cache-reuse", str(cache_reuse)])
    if str(config.get("CACHE_PROMPT", "on") or "on") == "off":
        cmd.append("--no-cache-prompt")
    if str(config.get("CONTEXT_SHIFT", "off") or "off") == "on":
        cmd.append("--context-shift")
    if str(config.get("KV_UNIFIED", "") or "") == "off":
        cmd.append("--no-kv-unified")
    _opt("KV_UNIFIED_PER_SLOT", "--kv-unified-per-slot")
    if str(config.get("CACHE_IDLE_SLOTS", "") or "") == "off":
        cmd.append("--no-cache-idle-slots")
    _opt("CHECKPOINT_MIN_STEP", "--checkpoint-min-step")
    if str(config.get("CONT_BATCHING", "on") or "on") == "off":
        cmd.append("--no-cont-batching")
    if str(config.get("WARMUP", "on") or "on") == "off":
        cmd.append("--no-warmup")
    _stropt("LORA", "--lora")
    _stropt("LORA_SCALED", "--lora-scaled")
    _opt("THREADS_HTTP", "--threads-http")
    timeout = int(config.get("TIMEOUT", 0) or 0)
    if timeout > 0:
        cmd.extend(["--timeout", str(timeout)])
    if str(config.get("SPEC_DEFAULT", "off") or "off") == "on":
        cmd.append("--spec-default")
    if str(config.get("IGNORE_EOS", "off") or "off") == "on":
        cmd.append("--ignore-eos")

    # --- кастомная команда (аргументы дописываются к llama-server.exe) ---
    custom_cmd = str(config.get("CUSTOM_CMD", "") or "").strip()
    if custom_cmd:
        try:
            parts = shlex.split(custom_cmd)
        except ValueError as e:
            logger.warning(f"Invalid CUSTOM_CMD (unbalanced quotes?), skipping: {e}")
            parts = []
        # защита: первая часть — опция; значения опций разрешаем,
        # но блокируем пути/exe (нельзя подменить запускаемую программу)
        safe = []
        expect_value = False
        skipped = []
        for p in parts:
            if expect_value:
                # это значение предыдущей опции
                if re.search(r'[\\/:]|\.(exe|bat|cmd|ps1|dll)$', p, re.IGNORECASE):
                    logger.warning(f"CUSTOM_CMD: blocked path-like value: {p}")
                    expect_value = False
                    continue
                safe.append(p)
                expect_value = False
            elif p.startswith("-"):
                safe.append(p)
                expect_value = ("=" not in p)
            else:
                skipped.append(p)
        if skipped:
            logger.warning(f"CUSTOM_CMD: skipped non-option tokens (must start with '-'): {skipped}")
        cmd.extend(safe)

    return cmd


_ENGINE_HOST_CACHE = None

def _get_engine_host(port):
    """Try multiple hosts to reach the engine — WSL2 may not forward 127.0.0.1."""
    global _ENGINE_HOST_CACHE
    if _ENGINE_HOST_CACHE:
        return _ENGINE_HOST_CACHE
    candidates = ["127.0.0.1"]
    if _IS_WSL:
        candidates.append("localhost")
        try:
            with open("/etc/resolv.conf") as f:
                for line in f:
                    if line.startswith("nameserver"):
                        ip = line.strip().split()[1]
                        if ip not in candidates:
                            candidates.append(ip)
                        break
        except Exception:
            swallow.note('api._get_engine_host:L778')
            pass
    for host in candidates:
        try:
            # 0.2 с, а не 0.8: на этой машине порт выключенного движка не
            # отвечает RST, и каждое подключение ждало полный таймаут.
            # Локальный сервер принимает соединение мгновенно (backlog ядра
            # отвечает ещё до accept), поэтому 0.2 с с запасом.
            _probe_timeout = 0.2 if host in ("127.0.0.1", "localhost") else 0.8
            s = socket.create_connection((host, int(port)), timeout=_probe_timeout)
            s.close()
            _ENGINE_HOST_CACHE = host
            return host
        except Exception:
            swallow.note('api._get_engine_host:L792')
            continue
    return None

def _is_port_open(port):
    return _get_engine_host(port) is not None

def _engine_props(host, port):
    try:
        req = urllib.request.Request(f"http://{host}:{port}/props", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        swallow.note('api._engine_props:L805')
        return None

def _check_engine(port):
    """Honest engine check. Returns (tcp_open, http_ready). Resets host cache on failure."""
    global _ENGINE_HOST_CACHE
    host = _get_engine_host(port)
    if not host:
        _ENGINE_HOST_CACHE = None
        return False, False
    props = _engine_props(host, port)
    if props is None:
        _ENGINE_HOST_CACHE = None
        return False, False
    return True, True


# Проба движка — блокирующий обход: при закрытом, но не отвечающем порту
# create_connection держит весь таймаут (0.8 с), а не получает мгновенный
# отказ. Эндпоинты статуса/статистики — async-функции, то есть вызываются
# прямо на потоке event loop: один такой проб на 830 мс останавливает обработку
# ВСЕХ остальных запросов (UI опрашивает /api/ai-engine/status каждые 3 с).
# Поэтому проба уходит в тред, а результат на коротком TTL переиспользуется:
# страничная загрузка дёргает status, stats и models разом.
_ENGINE_PROBE_TTL = 2.0
# Отрицательный результат живёт дольше. Замер на живой машине: connect к
# 127.0.0.1:8080 при выключенном движке не получает RST, а висит до таймаута
# (0.8 с в _get_engine_host). UI опрашивает /status каждые 3 с, то есть каждый
# второй опрос стоил 0.8 с ожидания в пуле потоков. Старт/стоп движка сами
# сбрасывают кэш через _forget_probe(), поэтому «движок выключен» можно
# помнить 4 с без риска показать устаревшее состояние после запуска.
_ENGINE_PROBE_TTL_DOWN = 4.0
# Пока ответ не старше этого возраста, его можно отдать сразу, а обновление
# запустить фоном. Так UI не ждёт таймаут соединения вообще, а «движок
# выключен» остаётся правдой с точностью до одного фонового опроса.
_ENGINE_PROBE_STALE_MAX = 60.0
_engine_probe_refresh = [False]
_engine_probe_gen = [0]
_engine_probe_cache = {}
_engine_probe_lock = threading.Lock()


def _refresh_probe_bg(port, gen):
    """Фоновое обновление устаревшей пробы; флаг не даёт плодить потоки."""
    try:
        res = _check_engine(port)
        with _engine_probe_lock:
            # Пока проба шла, движок могли запустить/остановить и сбросить
            # кэш (_forget_probe). Устаревший результат тогда не записываем.
            if gen == _engine_probe_gen[0]:
                _engine_probe_cache[port] = (time.monotonic(), res)
    except Exception:
        swallow.note('api._refresh_probe_bg:L857')
        pass
    finally:
        with _engine_probe_lock:
            _engine_probe_refresh[0] = False


def _probe_engine_blocking(port):
    now = time.monotonic()
    with _engine_probe_lock:
        hit = _engine_probe_cache.get(port)
        if hit is not None:
            res = hit[1]
            age = now - hit[0]
            ttl = _ENGINE_PROBE_TTL if (res and res[0]) else _ENGINE_PROBE_TTL_DOWN
            if age < ttl:
                return res
            if age < _ENGINE_PROBE_STALE_MAX and not _engine_probe_refresh[0]:
                # Устаревший, но ещё допустимый ответ отдаём сразу, а пробу
                # обновляем в фоне. Иначе каждый второй опрос /status ждал
                # таймаут connect к выключенному движку (0.2 с, до правки 0.8).
                _engine_probe_refresh[0] = True
                try:
                    threading.Thread(target=_refresh_probe_bg,
                                     args=(port, _engine_probe_gen[0]),
                                     name="engine-probe", daemon=True).start()
                except Exception:
                    swallow.note('api._probe_engine_blocking:L884')
                    _engine_probe_refresh[0] = False
                return res
    res = _check_engine(port)
    with _engine_probe_lock:
        _engine_probe_cache[port] = (time.monotonic(), res)
    return res


async def _probe_engine(port):
    return await asyncio.to_thread(_probe_engine_blocking, port)


def _forget_probe():
    """Сброс обоих кэшей пробы: после старта/стопа движка порт обязан
    проверяться заново, иначе 2 секунды врать про уже изменившееся состояние."""
    global _ENGINE_HOST_CACHE
    _ENGINE_HOST_CACHE = None
    with _engine_probe_lock:
        _engine_probe_gen[0] += 1
        _engine_probe_cache.clear()


async def _port_open_now(port):
    return await asyncio.to_thread(_is_port_open, port)


_LAST_STATUS = None

# ============================================================
# Активная генерация: какой модуль что генерирует прямо сейчас.
# Модули приложения дёргают engine_begin / engine_update_text / engine_end.
# ============================================================

_ACTIVE_LOCK = threading.Lock()
_ACTIVE_GEN = {
    "seq": None, "module": None, "model": None, "text": "", "prompt": "", "params": None,
    "started_at": None, "updated_at": None,
    "tps": None, "tokens": None, "tokens_prompt": None, "finished": False,
    "live_tokens": 0, "live_tps": None,
}
_ACTIVE_TEXT_MAX = 6000
_ACTIVE_STALE_SEC = 120

# ============================================================
# Журнал активности ИИ (кольцевой буфер).
# Каждая генерация = запись: промт, модель, параметры, живой текст, итоги.
# Опрашивается UI через /api/ai-engine/activity.
# ============================================================

_AILOG = []
_AILOG_SEQ = 0
_AILOG_MAX = 200
_AILOG_TEXT_MAX = 20000
_AILOG_PROMPT_MAX = 2000


def _ailog_append(entry: dict):
    global _AILOG, _AILOG_SEQ
    with _ACTIVE_LOCK:
        _AILOG_SEQ += 1
        entry["seq"] = _AILOG_SEQ
        _AILOG.append(entry)
        if len(_AILOG) > _AILOG_MAX:
            del _AILOG[0:len(_AILOG) - _AILOG_MAX]
    return entry["seq"]


def engine_begin(module="unknown", model="", prompt="", params=None):
    """Вызвать перед запросом к LLM.
    prompt — текст отправленного запроса (последнее user-сообщение).
    params — параметры генерации (temperature, max_tokens, ...)."""
    entry = {
        "seq": None,
        "ts": time.time(),
        "module": module or "unknown",
        "model": model or "",
        "prompt": (prompt or "")[:_AILOG_PROMPT_MAX],
        "params": params,
        "status": "running",
        "text": "",
        "tokens": None,
        "tokens_prompt": None,
        "tps": None,
        "live_tokens": 0,
        "live_tps": None,
        "started_at": time.time(),
        "updated_at": time.time(),
    }
    seq = _ailog_append(entry)
    with _ACTIVE_LOCK:
        _ACTIVE_GEN.update({
            "seq": seq,
            "module": entry["module"],
            "model": entry["model"],
            "text": "",
            "prompt": entry["prompt"],
            "params": entry.get("params"),
            "started_at": time.time(),
            "updated_at": time.time(),
            "tps": None, "tokens": None, "tokens_prompt": None, "finished": False,
            "live_tokens": 0, "live_tps": None,
        })
    return seq


def _ailog_current():
    """Найти текущую running-запись журнала (без захвата лока — вызывать из-под _ACTIVE_LOCK)."""
    seq = _ACTIVE_GEN.get("seq")
    if seq is None:
        return None
    for e in _AILOG:
        if e.get("seq") == seq and e.get("status") == "running":
            return e
    return None


def engine_update_text(chunk, token=False):
    """Докормить текст генерации (для живого отображения).
    token=True вызывается на каждый пришедший токен — считает живую скорость."""
    with _ACTIVE_LOCK:
        entry = None
        if chunk:
            cur = _ACTIVE_GEN["text"]
            _ACTIVE_GEN["text"] = (cur + str(chunk))[-_ACTIVE_TEXT_MAX:]
            entry = _ailog_current()
            if entry is not None:
                ecur = entry.get("text") or ""
                entry["text"] = (ecur + str(chunk))[-_AILOG_TEXT_MAX:]
        if token:
            _ACTIVE_GEN["live_tokens"] += 1
            if _ACTIVE_GEN["started_at"]:
                _ACTIVE_GEN["live_tps"] = _ACTIVE_GEN["live_tokens"] / max(0.1, time.time() - _ACTIVE_GEN["started_at"])
                if entry is None:
                    entry = _ailog_current()
                if entry is not None:
                    entry["live_tps"] = _ACTIVE_GEN["live_tps"]
            _ACTIVE_GEN["updated_at"] = time.time()
            if entry is not None:
                entry["live_tokens"] = _ACTIVE_GEN["live_tokens"]
                entry["updated_at"] = time.time()


def engine_end(final_text="", tps=None, tokens=None, tokens_prompt=None, error=None):
    """Вызвать после завершения запроса к LLM.
    tps/tokens берутся из timings ответа (predicted_per_second, predicted_n);
    если их нет — используем живые счётчики из стрима."""
    with _ACTIVE_LOCK:
        if final_text:
            cur = _ACTIVE_GEN["text"]
            _ACTIVE_GEN["text"] = (cur + str(final_text))[-_ACTIVE_TEXT_MAX:]
        if tps is not None:
            _ACTIVE_GEN["tps"] = tps
        elif _ACTIVE_GEN["live_tps"] is not None:
            _ACTIVE_GEN["tps"] = _ACTIVE_GEN["live_tps"]
        if tokens is not None:
            _ACTIVE_GEN["tokens"] = tokens
        elif _ACTIVE_GEN["live_tokens"]:
            _ACTIVE_GEN["tokens"] = _ACTIVE_GEN["live_tokens"]
        if tokens_prompt is not None:
            _ACTIVE_GEN["tokens_prompt"] = tokens_prompt
        _ACTIVE_GEN["finished"] = True
        _ACTIVE_GEN["updated_at"] = time.time()
        entry = _ailog_current()
        if entry is not None:
            if final_text:
                ecur = entry.get("text") or ""
                entry["text"] = (ecur + str(final_text))[-_AILOG_TEXT_MAX:]
            entry["tps"] = _ACTIVE_GEN["tps"]
            entry["tokens"] = _ACTIVE_GEN["tokens"]
            entry["tokens_prompt"] = _ACTIVE_GEN["tokens_prompt"]
            entry["live_tokens"] = _ACTIVE_GEN["live_tokens"]
            entry["live_tps"] = _ACTIVE_GEN["live_tps"]
            entry["status"] = "error" if error else "done"
            if error:
                entry["error"] = str(error)[:500]
            entry["updated_at"] = time.time()


def _active_snapshot():
    with _ACTIVE_LOCK:
        d = dict(_ACTIVE_GEN)
    if d["started_at"] and d["updated_at"] and (time.time() - d["updated_at"]) > _ACTIVE_STALE_SEC:
        d = {"seq": None, "module": None, "model": None, "text": "", "prompt": "", "params": None,
             "started_at": None, "updated_at": None,
             "tps": None, "tokens": None, "tokens_prompt": None, "finished": False,
             "live_tokens": 0, "live_tps": None}
    return d


# ============================================================
# Живой расчёт скорости/токенов по /metrics (Prometheus-текст).
# /slots в build 9840 отдаёт только {id, is_processing, n_ctx, speculative}.
# ============================================================

_METRICS_LAST = {"ts": 0.0, "pred": 0.0, "pred_sec": 0.0, "prompt": 0.0, "prompt_sec": 0.0}
_METRICS_BASELINE = {"pred": 0.0, "prompt": 0.0, "set": False}


def _fetch_metrics(host, port):
    try:
        req = urllib.request.Request(f"http://{host}:{port}/metrics")
        with urllib.request.urlopen(req, timeout=3) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        m = {}
        for line in text.splitlines():
            if line.startswith("llamacpp:"):
                key, _, val = line.partition(" ")
                if val:
                    try:
                        m[key] = float(val)
                    except ValueError:
                        swallow.note('api._fetch_metrics:L1096')
                        pass
        return m
    except Exception:
        swallow.note('api._fetch_metrics:L1100')
        return None


def _slot_short_name(path):
    if not path:
        return ""
    name = os.path.basename(str(path))
    if name.lower().endswith(".gguf"):
        name = name[:-5]
    return name


def _collect_engine_stats(host, port, fallback_model):
    """Опрос /slots + /metrics, возвращает (slots_out, live).
    live: {"processing": bool, "tps": float|None, "tokens": int, "tokens_prompt": int, "phase": str}"""
    global _METRICS_LAST, _METRICS_BASELINE

    now = time.time()
    slots_out = []
    processing = False
    try:
        req = urllib.request.Request(f"http://{host}:{port}/slots")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        raw_slots = data if isinstance(data, list) else data.get("slots", [])
        for s in raw_slots:
            sid = s.get("id_slot", s.get("id"))
            if sid is None:
                sid = 0
            is_proc = bool(s.get("is_processing"))
            processing = processing or is_proc
            predicted = s.get("predicted")
            slots_out.append({
                "id": sid, "id_slot": sid,
                "is_processing": is_proc,
                "state": s.get("state"),
                "phase": "generation" if is_proc else "idle",
                "speed": None,
                "tokens_generated": 0,
                "tokens_prompt": 0,
                "n_ctx": int(s.get("n_ctx") or 0),
                "model": s.get("model") or fallback_model,
                "prompt": "",
                "predicted_text": predicted if isinstance(predicted, str) else "",
            })
    except Exception:
        logger.warning("Failed to read /slots from engine", exc_info=True)

    live = {"processing": processing, "tps": None, "tokens": 0, "tokens_prompt": 0, "phase": "generation" if processing else "idle"}
    m = _fetch_metrics(host, port)
    if m is not None:
        pred = m.get("llamacpp:tokens_predicted_total", 0.0)
        pred_sec = m.get("llamacpp:tokens_predicted_seconds_total", 0.0)
        prompt = m.get("llamacpp:prompt_tokens_total", 0.0)
        prompt_sec = m.get("llamacpp:prompt_seconds_total", 0.0)

        if not _METRICS_BASELINE["set"]:
            _METRICS_BASELINE.update({"pred": pred, "prompt": prompt, "set": True})
        _METRICS_LAST, last = {"ts": now, "pred": pred, "pred_sec": pred_sec, "prompt": prompt, "prompt_sec": prompt_sec}, dict(_METRICS_LAST)

        if processing:
            live["tokens"] = int(pred - _METRICS_BASELINE["pred"])
            live["tokens_prompt"] = int(prompt - _METRICS_BASELINE["prompt"])
            if live["tokens"] < 0:
                _METRICS_BASELINE["pred"] = pred
                live["tokens"] = 0
            if live["tokens_prompt"] < 0:
                _METRICS_BASELINE["prompt"] = prompt
                live["tokens_prompt"] = 0
            if pred_sec > last["pred_sec"] and pred > last["pred"]:
                live["tps"] = (pred - last["pred"]) / (pred_sec - last["pred_sec"]) if (pred_sec - last["pred_sec"]) > 0 else None
            if live["tps"] is None:
                g = m.get("llamacpp:predicted_tokens_seconds", 0.0)
                live["tps"] = g if g > 0 else None
            live["phase"] = "generation"
        else:
            if pred != _METRICS_BASELINE["pred"] or prompt != _METRICS_BASELINE["prompt"]:
                _METRICS_BASELINE.update({"pred": pred, "prompt": prompt})
            live["tokens"] = int(pred - _METRICS_BASELINE["pred"])
            live["tokens_prompt"] = int(prompt - _METRICS_BASELINE["prompt"])
    return slots_out, live


def _queue_stats():
    """Состояние очереди инференса; ошибка импорта не должна ломать статус."""
    try:
        from modules.llm_service import inference_queue_stats
        return inference_queue_stats()
    except Exception as e:
        logger.warning(f"AI queue stats unavailable: {e}")
        return {"enabled": False, "waiting": 0, "active": 0, "served": 0,
                "current": None, "last_wait": 0.0, "max_wait": 0.0}


@router.get("/queue")
def engine_queue():
    """Очередь запросов к локальному движку (для админки и агента)."""
    return _queue_stats()


# Снимок слотов/метрик с TTL: /stats читает /slots и /metrics движка, а во
# время генерации эти пробы ждут ответа модели (на живой машине p95 1.5 с при
# опросе раз в 10 с из каждого окна). Свежий ответ переиспользуется короткий
# TTL, устаревший отдаётся сразу с фоновым обновлением — как у _probe_engine.
_STATS_TTL = 2.0
_STATS_STALE_MAX = 20.0
_stats_cache = {}            # port -> (monotonic_ts, slots_out, live)
_stats_refresh = [False]
_stats_lock = threading.Lock()


def _refresh_stats_bg(host, port, fallback_model):
    try:
        res = _collect_engine_stats(host, port, fallback_model)
        with _stats_lock:
            _stats_cache[port] = (time.monotonic(), res[0], res[1])
    except Exception:
        swallow.note('api._refresh_stats_bg:L1218')
        pass
    finally:
        with _stats_lock:
            _stats_refresh[0] = False


async def _slots_live(host, port, fallback_model):
    now = time.monotonic()
    with _stats_lock:
        hit = _stats_cache.get(port)
        if hit is not None:
            age = now - hit[0]
            if age < _STATS_TTL:
                return hit[1], hit[2]
            if age < _STATS_STALE_MAX and not _stats_refresh[0]:
                _stats_refresh[0] = True
                threading.Thread(target=_refresh_stats_bg,
                                 args=(host, port, fallback_model),
                                 name="engine-stats", daemon=True).start()
                return hit[1], hit[2]
    res = await asyncio.to_thread(_collect_engine_stats, host, port, fallback_model)
    with _stats_lock:
        _stats_cache[port] = (time.monotonic(), res[0], res[1])
    return res


@router.get("/stats")
async def engine_stats():
    config = _read_config()
    port = int(config.get("PORT", "8080"))
    running, ready = await _probe_engine(port)
    fallback_model = _slot_short_name(config.get("MODEL", ""))
    slots_out = []
    live = {"processing": False, "tps": None, "tokens": 0, "tokens_prompt": 0, "phase": "idle"}
    if running:
        host = await asyncio.to_thread(_get_engine_host, port)
        if host:
            slots_out, live = await _slots_live(host, port, fallback_model)
    else:
        with _stats_lock:
            _stats_cache.pop(port, None)
    active = _active_snapshot()

    if not slots_out:
        slots_out.append({
            "id": None, "id_slot": None, "is_processing": live["processing"],
            "state": None, "phase": live["phase"],
            "speed": live["tps"], "tokens_generated": live["tokens"],
            "tokens_prompt": live["tokens_prompt"], "n_ctx": 0,
            "model": active.get("model") or fallback_model,
            "prompt": "", "predicted_text": active.get("text") or "",
        })
    else:
        s0 = slots_out[0]
        s0["speed"] = live["tps"]
        s0["tokens_generated"] = live["tokens"]
        s0["tokens_prompt"] = live["tokens_prompt"]
        if not s0.get("model"):
            s0["model"] = fallback_model

    return {"slots": slots_out, "running": bool(running), "ready": bool(ready),
            "active": active, "live": live, "model": fallback_model,
            "queue": _queue_stats()}

@router.get("/status")
async def status():
    global _LAST_STATUS
    port = int(_read_config().get("PORT", "8080"))
    running, ready = await _probe_engine(port)
    state = (running, ready)
    if state != _LAST_STATUS:
        logger.info(f"Status check -> running={running}, ready={ready}")
        _LAST_STATUS = state
    return {"running": running, "ready": ready}


@router.get("/activity")
def engine_activity(after: int = 0):
    """Журнал активности ИИ (запросы/генерации). after — вернуть записи с seq > after."""
    with _ACTIVE_LOCK:
        entries = [dict(e) for e in _AILOG if e.get("seq", 0) > after]
    return {"entries": entries, "last_seq": _AILOG_SEQ}


def _read_engine_log_tail(tail: int = 300, after: int = 0):
    """Хвост raw-лога llama-server (aiengine/engine.log).
    after — байтовый оффсет для инкрементального чтения; 0 = отдать последние tail строк."""
    path = _ENGINE_LOG
    if not os.path.exists(path):
        return {"log": "", "offset": 0, "size": 0, "rotated": False}
    size = os.path.getsize(path)
    rotated = after > size and size > 0
    if after < 0:
        after = 0
    text = ""
    new_off = size
    try:
        if after == 0:
            with open(path, "rb") as f:
                f.seek(max(0, size - max(tail, 1) * 4000))
                data = f.read()
            lines = data.decode("utf-8", errors="replace").splitlines()
            if len(lines) > tail:
                lines = lines[-tail:]
            text = "\n".join(lines)
            if text:
                text += "\n"
        else:
            if after < size:
                with open(path, "rb") as f:
                    f.seek(after)
                    data = f.read()
                text = data.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"engine log read failed: {e}")
        return {"log": "", "offset": new_off, "size": size, "rotated": False, "error": str(e)[:200]}
    return {"log": text, "offset": new_off, "size": size, "rotated": rotated}


@router.get("/log")
def engine_raw_log(tail: int = 300, after: int = 0):
    """Raw-лог llama.cpp в реальном времени (stdout/stderr движка)."""
    tail = min(max(int(tail), 10), 5000)
    after = max(int(after), 0)
    return _read_engine_log_tail(tail, after)


@router.get("/config")
def get_config():
    return _read_config()


@router.post("/command-preview")
def command_preview(update: Optional[ConfigUpdate] = None):
    """Построить итоговую команду запуска llama-server из переданных настроек
    (без запуска движка). Возвращает готовую строку для копирования в консоль."""
    config = _read_config()
    if update:
        update_dict = {k: v for k, v in update.model_dump().items() if v is not None}
        config.update(update_dict)
    cmd = _build_command(config)
    if cmd is None:
        return {"ok": False, "command": "", "args": [],
                "message": "Модель не найдена — укажите модель в Launch-вкладке"}
    return {"ok": True, "command": subprocess.list2cmdline(cmd), "args": cmd}


@router.post("/config")
def save_config(update: ConfigUpdate):
    config = _read_config()
    update_dict = {k: v for k, v in update.model_dump().items() if v is not None}
    config.update(update_dict)
    _write_config(config)
    return {"ok": True, "config": config}


@router.get("/models")
def list_models(dir: str = ""):
    d = dir.strip() if dir else None
    models = _list_models(d) if d else _list_models()
    return {"models": models, "dir": d or _model_dir()}


def _list_models(dir_=None):
    d = dir_ or _model_dir()
    if not d:
        return []
    files = glob.glob(os.path.join(d, "*.gguf"))
    if not files and _IS_WSL:
        w = _wsl_path(d)
        if w != d:
            files = glob.glob(os.path.join(w, "*.gguf"))
    files.sort(key=lambda x: os.path.getsize(x), reverse=True)
    return files


# --- watchdog / auto-restart ---

_WATCHDOG_INTERVAL = 15
_RESTART_BACKOFF = [30, 60, 120, 300]
_START_GRACE = 120
_watchdog_stop = threading.Event()
_watchdog_thread = None
_manual_stop = False
_restart_index = 0
_engine_seen_up = False
_engine_launch_ts = 0.0


def _start_watchdog():
    """Start the auto-restart watchdog thread (idempotent)."""
    global _watchdog_thread
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        return
    _watchdog_stop.clear()
    t = threading.Thread(target=_watchdog_loop, daemon=True, name="ai-engine-watchdog")
    t.start()
    _watchdog_thread = t


def _stop_watchdog():
    _watchdog_stop.set()


def _watchdog_loop():
    while not _watchdog_stop.is_set():
        _watchdog_stop.wait(_WATCHDOG_INTERVAL)
        if _watchdog_stop.is_set():
            return
        try:
            _watchdog_tick()
        except Exception:
            logger.exception("Watchdog tick failed")


def _watchdog_tick():
    global _restart_index, _engine_seen_up, _engine_launch_ts
    if _manual_stop:
        return
    config = _read_config()
    port = int(config.get("PORT", "8080"))
    running, _ = _check_engine(port)
    if running:
        _engine_seen_up = True
        _restart_index = 0
        return
    # Восстанавливаем движок, если он ожидался: либо мы его запускали,
    # либо он был жив, когда стартовал этот процесс сервера (после рестарта).
    if _engine_process is None and not _engine_seen_up:
        return
    if not _engine_seen_up and (time.time() - _engine_launch_ts) < _START_GRACE:
        return
    delay = _RESTART_BACKOFF[min(_restart_index, len(_RESTART_BACKOFF) - 1)]
    logger.warning(f"Engine is down (port {port} closed) — restarting in {delay}s (attempt {_restart_index + 1})")
    _watchdog_stop.wait(delay)
    if _watchdog_stop.is_set():
        return
    if _is_port_open(port):
        # Движок поднял кто-то другой — принимаем его и успокаиваемся.
        _engine_seen_up = True
        _restart_index = 0
        return
    try:
        _launch_engine(config, kill_first=False)
    except Exception as e:
        logger.error(f"Auto-restart failed: {e}")
        _restart_index += 1
    else:
        _restart_index += 1


def _launch_engine(config: dict, kill_first=True):
    """Launch the engine (kills existing first by default). Returns PID."""
    global _engine_process, _engine_seen_up, _engine_launch_ts
    cmd = _build_command(config)
    if cmd is None:
        model_dir = _model_dir(config)
        models_found = _list_models(model_dir)
        raise RuntimeError(f"No model found in {model_dir} (found {len(models_found)} .gguf files)")
    if kill_first:
        _kill_engine()
    _open_engine_log()
    logger.info(f"Starting engine with command: {' '.join(cmd)}")
    _log_engine_line(time.strftime("%Y-%m-%d %H:%M:%S") + " starting: " + " ".join(cmd))
    try:
        _engine_process = subprocess.Popen(
            cmd,
            cwd=_AIENGINE_DIR,
            stdout=_engine_log_fh if _engine_log_fh is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        )
    except Exception as e:
        logger.error(f"Failed to start engine: {e}", exc_info=True)
        raise
    _engine_seen_up = False
    _engine_launch_ts = time.time()
    logger.info(f"Engine started, PID: {_engine_process.pid}")
    _start_watchdog()
    return _engine_process.pid


@router.post("/start")
async def start_engine(update: Optional[ConfigUpdate] = None):
    global _ENGINE_HOST_CACHE, _manual_stop

    _forget_probe()
    port = int(_read_config().get("PORT", "8080"))
    if update and update.PORT is not None:
        port = update.PORT

    if await _port_open_now(port):
        raise HTTPException(400, "Engine already running on this port")

    # Пуск и остановка движка — это subprocess + wait(timeout=2) + обход
    # каталога моделей. На event loop такая остановка стоила всем остальным
    # запросам до 3 секунд простоя.
    await asyncio.to_thread(_kill_engine)
    _manual_stop = False

    config = _read_config()
    if update:
        update_dict = {k: v for k, v in update.model_dump().items() if v is not None}
        config.update(update_dict)
        _write_config(config)
        logger.info(f"Config updated from request: MODEL={update_dict.get('MODEL','')[:60]}")

    try:
        pid = await asyncio.to_thread(_launch_engine, config)
    except Exception as e:
        raise HTTPException(500, f"Failed to start engine: {e}")
    return {"ok": True, "message": "Engine started", "pid": pid}


@router.post("/stop")
async def stop_engine():
    global _engine_process, _ENGINE_HOST_CACHE, _manual_stop

    _forget_probe()
    _manual_stop = True
    port = int(_read_config().get("PORT", "8080"))
    if not await _port_open_now(port):
        return {"ok": True, "message": "Engine not running"}

    await asyncio.to_thread(_kill_engine)
    # Прежний time.sleep(0.3) стоял на потоке event loop и тормозил все
    # параллельные запросы; waiting-версия отдаёт цикл другим корутине.
    await asyncio.sleep(0.3)

    _forget_probe()
    if await _port_open_now(port):
        logger.warning("Engine stop may have failed")
        return {"ok": True, "message": "Engine stop might have failed"}
    return {"ok": True, "message": "Engine stopped"}


@router.get("/specs")
def specs():
    return get_system_specs()


# ============================================================
# HuggingFace Model Hub
# ============================================================

import urllib.parse

_HUB_CACHE = {}
_HUB_CACHE_TTL = 300

_HUB_DOWNLOADS = {}
_HUB_DOWNLOAD_LOCK = threading.Lock()


def _hub_get_json(req):
    """Запрос к HF API с таймаутом 15 с.

    Синхронно и в своём потоке: на event loop такой хост — это «приложение
    молчит 15 секунд», а guard считает молчание дольше 4 с зависанием и
    убивает сервер вместе с ответом.
    """
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


@router.get("/hub/search")
async def hub_search(q: str = "", sort: str = "downloads", limit: int = 20):
    """Поиск GGUF моделей на HuggingFace."""
    limit = min(max(int(limit), 1), 50)
    cache_key = f"{q}:{sort}:{limit}"
    now = time.time()
    if cache_key in _HUB_CACHE:
        ts, data = _HUB_CACHE[cache_key]
        if now - ts < _HUB_CACHE_TTL:
            return data
    try:
        search_q = f"gguf {q}".strip()
        params = urllib.parse.urlencode({
            "search": search_q,
            "sort": sort,
            "direction": "-1",
            "limit": str(limit),
        })
        url = f"https://huggingface.co/api/models?{params}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "InfinitySearch-AIEngine/1.0",
        })
        raw = await asyncio.to_thread(_hub_get_json, req)
        models = []
        for m in raw:
            model_id = m.get("id", "")
            tags = m.get("tags", []) or []
            siblings = m.get("siblings", []) or []
            gguf_files = [s for s in siblings if s.get("rfilename", "").endswith(".gguf")]
            has_guf_tag = any("gguf" in t.lower() for t in tags)
            if not gguf_files and not has_guf_tag:
                continue
            total_size = sum(s.get("size", 0) or 0 for s in gguf_files)
            models.append({
                "id": model_id,
                "author": model_id.split("/")[0] if "/" in model_id else "",
                "name": model_id.split("/")[-1] if "/" in model_id else model_id,
                "downloads": m.get("downloads", 0),
                "likes": m.get("likes", 0),
                "lastModified": m.get("lastModified", ""),
                "tags": tags,
                "gguf_count": len(gguf_files) if gguf_files else None,
                "total_size": total_size,
                "pipeline_tag": m.get("pipeline_tag", ""),
            })
        result = {"models": models, "query": q, "sort": sort}
        _HUB_CACHE[cache_key] = (now, result)
        return result
    except Exception as e:
        logger.warning(f"Hub search failed: {e}")
        return {"models": [], "query": q, "sort": sort, "error": str(e)[:200]}


@router.get("/hub/model/{repo_id:path}")
async def hub_model_info(repo_id: str):
    """Подробная информация о модели: описание, GGUF файлы."""
    try:
        url = f"https://huggingface.co/api/models/{repo_id}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "InfinitySearch-AIEngine/1.0",
        })
        raw = await asyncio.to_thread(_hub_get_json, req)
        siblings = raw.get("siblings", []) or []
        gguf_files = []
        for s in siblings:
            fn = s.get("rfilename", "")
            if fn.endswith(".gguf"):
                gguf_files.append({
                    "filename": fn,
                    "size": s.get("size", 0),
                    "size_human": _human_size(s.get("size", 0)),
                })
        gguf_files.sort(key=lambda x: x["size"], reverse=True)
        card = raw.get("cardData") or {}
        return {
            "id": repo_id,
            "author": repo_id.split("/")[0] if "/" in repo_id else "",
            "name": repo_id.split("/")[-1] if "/" in repo_id else repo_id,
            "downloads": raw.get("downloads", 0),
            "likes": raw.get("likes", 0),
            "tags": raw.get("tags", []),
            "pipeline_tag": raw.get("pipeline_tag", ""),
            "lastModified": raw.get("lastModified", ""),
            "description": card.get("description", "") or raw.get("description", "") or "",
            "gguf_files": gguf_files,
        }
    except Exception as e:
        logger.warning(f"Hub model info failed for {repo_id}: {e}")
        raise HTTPException(502, f"Failed to fetch model info: {e}")


@router.get("/hub/download")
def hub_download(repo_id: str, filename: str):
    """Скачивание .gguf файла в папку моделей (в фоне)."""
    if not filename.endswith(".gguf"):
        raise HTTPException(400, "Only .gguf files can be downloaded")
    model_dir = _model_dir()
    os.makedirs(model_dir, exist_ok=True)
    dest = os.path.join(model_dir, os.path.basename(filename))
    if os.path.exists(dest):
        return {"ok": True, "message": "File already exists", "path": dest, "already": True}
    download_url = f"https://huggingface.co/{repo_id}/resolve/main/{urllib.parse.quote(filename)}"
    dl_id = f"{repo_id}/{filename}"
    with _HUB_DOWNLOAD_LOCK:
        if dl_id in _HUB_DOWNLOADS and _HUB_DOWNLOADS[dl_id].get("status") == "running":
            return {"ok": True, "message": "Download already in progress", "dl_id": dl_id}
        _HUB_DOWNLOADS[dl_id] = {"status": "running", "progress": 0, "downloaded": 0, "total": 0, "error": None}
    t = threading.Thread(target=_hub_download_thread, args=(download_url, dest, dl_id), daemon=True)
    t.start()
    return {"ok": True, "message": "Download started", "dl_id": dl_id, "dest": dest}


def _hub_download_thread(url, dest, dl_id):
    """Фоновое скачивание файла с прогрессом."""
    tmp = dest + ".downloading"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "InfinitySearch-AIEngine/1.0",
        })
        with urllib.request.urlopen(req, timeout=600) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            with _HUB_DOWNLOAD_LOCK:
                _HUB_DOWNLOADS[dl_id]["total"] = total
            downloaded = 0
            chunk_size = 1024 * 1024
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    with _HUB_DOWNLOAD_LOCK:
                        _HUB_DOWNLOADS[dl_id]["downloaded"] = downloaded
                        _HUB_DOWNLOADS[dl_id]["progress"] = (downloaded / total * 100) if total > 0 else 0
        os.replace(tmp, dest)
        with _HUB_DOWNLOAD_LOCK:
            _HUB_DOWNLOADS[dl_id]["status"] = "done"
            _HUB_DOWNLOADS[dl_id]["progress"] = 100
        logger.info(f"Hub download complete: {dest}")
    except Exception as e:
        logger.error(f"Hub download failed: {e}")
        with _HUB_DOWNLOAD_LOCK:
            _HUB_DOWNLOADS[dl_id]["status"] = "error"
            _HUB_DOWNLOADS[dl_id]["error"] = str(e)[:300]
        try:
            os.remove(tmp)
        except Exception:
            swallow.note('api._hub_download_thread:L1734')
            pass


@router.get("/hub/progress")
def hub_progress(dl_id: str = ""):
    """Прогресс скачивания."""
    if dl_id:
        with _HUB_DOWNLOAD_LOCK:
            info = _HUB_DOWNLOADS.get(dl_id)
        return info or {"status": "not_found"}
    with _HUB_DOWNLOAD_LOCK:
        return {"downloads": {k: v for k, v in _HUB_DOWNLOADS.items()}}


def _human_size(n):
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


# ============================================================
# Старт watchdog при импорте + «усыновление» уже запущенного движка.
# После перезапуска сервера движок (Windows-процесс) остаётся жив,
# но новый процесс про него не знает. Если он упадёт — watchdog
# должен его восстановить, поэтому считаем работающий движок «нашим».
# ============================================================
def _start_watchdog_at_import():
    global _engine_seen_up
    try:
        port = int(_read_config().get("PORT", "8080"))
        running, _ = _check_engine(port)
        if running:
            _engine_seen_up = True
            logger.info(f"AI engine already running on port {port} — adopted by watchdog")
    except Exception:
        swallow.note('api._start_watchdog_at_import:L1774')
        pass
    _start_watchdog()


_start_watchdog_at_import()
