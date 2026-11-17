#!/usr/bin/env python3
import os, sys, platform, subprocess, re, json
import swallow

# Скрытие консольных окон (Windows). Все запросы ниже идут через
# shell=True — без CREATE_NO_WINDOW каждый вызов wmic/nvidia-smi открывал
# чёрное консольное окно поверх приложения. На POSIX флаг не принимается.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _hidden():
    return {"creationflags": _NO_WINDOW} if os.name == "nt" else {}

_SPECS_CACHE = None

def get_system_specs(force=False):
    global _SPECS_CACHE
    if _SPECS_CACHE and not force:
        return _SPECS_CACHE

    ram_total_gb = _get_ram_gb()
    cpu_name, cpu_cores, cpu_threads = _get_cpu_info()
    gpu_list = _get_gpu_info()
    ram_available_gb = _get_ram_available_gb()

    recommendation = _recommend_model(ram_total_gb, ram_available_gb, cpu_threads, gpu_list)

    result = {
        "ram": {
            "total_gb": round(ram_total_gb, 1),
            "available_gb": round(ram_available_gb, 1)
        },
        "cpu": {
            "name": cpu_name,
            "cores": cpu_cores,
            "threads": cpu_threads
        },
        "gpu": gpu_list,
        "recommendation": recommendation
    }

    _SPECS_CACHE = result
    return result


def _get_ram_gb():
    try:
        import psutil
        return psutil.virtual_memory().total / (1024**3)
    except ImportError:
        swallow.note('system_specs._get_ram_gb:L50')
        pass
    try:
        out = subprocess.check_output(
            "wmic OS get TotalVisibleMemorySize /value",
            shell=True, stderr=subprocess.DEVNULL, timeout=5, **_hidden()
        ).decode("utf-8", errors="replace")
        m = re.search(r'TotalVisibleMemorySize[=\s]+(\d+)', out)
        if m:
            return int(m.group(1)) / 1024 / 1024
    except Exception:
        swallow.note('system_specs._get_ram_gb:L61')
        pass
    return 16.0


def _get_ram_available_gb():
    try:
        import psutil
        return psutil.virtual_memory().available / (1024**3)
    except ImportError:
        swallow.note('system_specs._get_ram_available_gb:L71')
        pass
    return _get_ram_gb() * 0.5


def _get_cpu_info():
    name = platform.processor() or "Unknown"
    cores = os.cpu_count() or 4
    threads = cores
    try:
        import psutil
        cores = psutil.cpu_count(logical=False) or cores
        threads = psutil.cpu_count(logical=True) or threads
    except ImportError:
        swallow.note('system_specs._get_cpu_info:L85')
        pass
    try:
        out = subprocess.check_output(
            "wmic CPU get Name /value",
            shell=True, stderr=subprocess.DEVNULL, timeout=5, **_hidden()
        ).decode("utf-8", errors="replace")
        m = re.search(r'Name[=\s]+(.+)', out)
        if m:
            name = m.group(1).strip()
    except Exception:
        swallow.note('system_specs._get_cpu_info:L96')
        pass
    return name, cores, threads


def _get_gpu_info():
    gpus = []
    try:
        out = subprocess.check_output(
            "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits",
            shell=True, stderr=subprocess.DEVNULL, timeout=10, **_hidden()
        ).decode("utf-8", errors="replace")
        for line in out.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                gpus.append({
                    "name": parts[0],
                    "vram_gb": round(float(parts[1]) / 1024, 1) if parts[1].replace('.','',1).isdigit() else 0,
                    "driver": parts[2] if len(parts) > 2 else "",
                    "vendor": "nvidia"
                })
    except Exception:
        swallow.note('system_specs._get_gpu_info:L118')
        pass
    if not gpus:
        try:
            out = subprocess.check_output(
                "wmic PATH Win32_VideoController GET Name,AdapterRAM /format:csv",
                shell=True, stderr=subprocess.DEVNULL, timeout=5, **_hidden()
            ).decode("utf-8", errors="replace")
            for line in out.strip().split("\n"):
                if "Name" in line:
                    continue
                parts = line.split(",")
                if len(parts) >= 3:
                    name = parts[-2].strip() if len(parts) >= 2 else parts[-1].strip()
                    ram_bytes = parts[-1].strip() if len(parts) >= 3 else "0"
                    vram_gb = 0
                    if ram_bytes.replace('.','',1).isdigit():
                        vram_gb = round(float(ram_bytes) / (1024**3), 1)
                    if name and "RDP" not in name and name != parts[-2]:
                        gpus.append({
                            "name": name,
                            "vram_gb": vram_gb,
                            "vendor": "unknown"
                        })
        except Exception:
            swallow.note('system_specs._get_gpu_info:L143')
            pass
    if not gpus:
        gpus.append({"name": "Unknown", "vram_gb": 0, "vendor": "unknown"})
    return gpus


def _recommend_model(ram_gb, ram_avail_gb, cpu_threads, gpu_list):
    has_nvidia = any(g.get("vendor") == "nvidia" for g in gpu_list)
    max_vram = max((g.get("vram_gb", 0) for g in gpu_list), default=0)

    effective_ram = ram_avail_gb
    if max_vram >= 6 and has_nvidia:
        effective_ram += max_vram * 0.7

    if effective_ram < 4:
        max_params = "1B"
        quant = "Q4_K_M"
        ctx = 2048
        desc = "Очень мало памяти. Рекомендуются модели до 1B параметров."
    elif effective_ram < 8:
        max_params = "3B"
        quant = "Q4_K_M"
        ctx = 4096
        desc = "Малый объём памяти. Рекомендуются модели до 3B."
    elif effective_ram < 16:
        max_params = "7B"
        quant = "Q4_K_M"
        ctx = 8192
        desc = "Средний объём памяти. Рекомендуются модели до 7B."
    elif effective_ram < 32:
        max_params = "14B"
        quant = "Q4_K_M"
        ctx = 16384
        desc = "Хороший объём памяти. Рекомендуются модели до 14B."
    elif effective_ram < 48:
        max_params = "30B"
        quant = "Q4_K_M"
        ctx = 32768
        desc = "Большой объём памяти. Рекомендуются модели до 30B."
    else:
        max_params = "35B+"
        quant = "Q4_K_M"
        ctx = 65536
        desc = "Отличный объём памяти. Можно запускать модели 35B+."

    if cpu_threads >= 16:
        desc += " Многоядерный CPU — хорошая производительность."
    elif cpu_threads >= 8:
        desc += " CPU со средним числом ядер."
    else:
        desc += " CPU с ограниченным числом ядер."

    if has_nvidia and max_vram >= 8:
        desc += " NVIDIA GPU с CUDA значительно ускорит инференс."
        if ctx > 32768:
            ctx = 32768
    elif has_nvidia and max_vram >= 4:
        desc += " Есть NVIDIA GPU — частичная выгрузка на GPU возможна."
    else:
        desc += " Рекомендуется использовать CPU (no-mmap)."

    return {
        "max_params": max_params,
        "quant": quant,
        "context": ctx,
        "description": desc,
        "gpu_layers": 99 if (has_nvidia and max_vram >= 8) else (20 if (has_nvidia and max_vram >= 4) else 0),
        "flash_attn": True,
        "cache_type": "q4_0" if effective_ram > 8 else "off"
    }


if __name__ == "__main__":
    print(json.dumps(get_system_specs(force=True), ensure_ascii=False, indent=2))
