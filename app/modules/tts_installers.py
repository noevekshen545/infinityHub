"""Установка голосовых движков: Piper (по голосу) и Silero v5.

Перенесено из server_backup.py без изменения тел. Флаги и версия
(SILERO_TTS_AVAILABLE, SILERO_MODEL_VERSION) берутся через _sb: их выставляет
_load_silero_v5 в монолите, и своя копия показала бы «не загружено» после
успешной установки. Сам загрузчик, каталог Piper и скачивание остаются в
монолите и зовутся через _sb. __name__ тоже подменён: тело берёт
sys.modules[__name__], и в новом модуле это был бы другой модуль.
"""

import os
import sys

import server_backup as _sb
import swallow

def handle_install_piper(voice=None):
    try:
        piper_dir = _piper_install_dir()
        os.makedirs(piper_dir, exist_ok=True)
        requested_voice = (voice or "").strip() or None
        piper_voice_catalog = {
            "ru_RU-ruslan-medium": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/ruslan/medium/ru_RU-ruslan-medium.onnx",
            "ru_RU-ruslan-medium.json": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/ruslan/medium/ru_RU-ruslan-medium.onnx.json",
            "ru_RU-irina-medium": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx",
            "ru_RU-irina-medium.json": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx.json",
            "ru_RU-dmitri-medium": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx",
            "ru_RU-dmitri-medium.json": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json",
        }
        if requested_voice not in piper_voice_catalog:
            requested_voice = "ru_RU-ruslan-medium"
        voice_id = requested_voice
        model_url = piper_voice_catalog[voice_id]
        model_path = os.path.join(piper_dir, voice_id + ".onnx")
        config_url = piper_voice_catalog.get(voice_id + ".json")
        config_path = os.path.join(piper_dir, voice_id + ".onnx.json")
        _piper_download(model_url, model_path)
        if config_url:
            _piper_download(config_url, config_path)
        try:
            from piper import PiperVoice as _PV
            me = sys.modules[_sb.__name__]
            voice_obj = _PV.load(model_path, config_path=config_path if os.path.exists(config_path) else None)
            me._PIPER_FAILED.discard(model_path)
            me._PIPER_VOICE_CACHE[model_path] = voice_obj
            me.PIPER_TTS_AVAILABLE = True
            me.PIPER_VOICE = voice_obj
            return {"ok": True, "voice": voice_id, "path": model_path,
                    "message": f"Piper голос {voice_id} установлен"}
        except Exception as e:
            swallow.note('tts_installers.handle_install_piper:L50', e)
            return {"error": f"Модель скачана, но ошибка загрузки: {e}"}
    except Exception as e:
        _sb.logger.error(f"install-piper error: {e}", exc_info=True)
        return {"error": str(e)}

def handle_install_silero():
    try:
        me = sys.modules[_sb.__name__]
        _sb._load_silero_v5()
        if _sb.SILERO_TTS_AVAILABLE:
            return {"ok": True, "version": _sb.SILERO_MODEL_VERSION, "speakers": _sb._list_silero_speakers(), "message": f"Silero {_sb.SILERO_MODEL_VERSION} загружен из локальной папки"}
        else:
            return {"error": "Не удалось загрузить Silero. Проверьте наличие models/silero/silero_hub"}
    except Exception as e:
        _sb.logger.error(f"install-silero error: {e}", exc_info=True)
        return {"error": str(e)}

def _piper_download(url, dest, temp_suffix='.part'):
    """Скачать файл в dest через urllib, не оставляя обрезанных моделей.

    Раньше установка вызывала внешний wget, которого на Windows обычно нет:
    кнопка «Установить Piper» всегда падала, а недокачанный файл мог остаться
    на диске и ломать загрузку голоса (INVALID_PROTOBUF). Пишем во временный
    файл и переименовываем только после успешной загрузки.
    """
    import urllib.request
    tmp = dest + temp_suffix
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, 'wb') as out:
            total = 0
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
        if total < 1000:
            raise RuntimeError(f'файл слишком мал ({total} байт)')
        os.replace(tmp, dest)
        return total
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            swallow.note('tts_installers._piper_download:L95')
            pass
        raise

def _piper_install_dir():
    """Куда класть скачанные голоса.

    В frozen-сборке _get_piper_model_dir() указывает внутрь _MEIPASS — это
    временная папка распаковки, запись туда теряется при выходе. Поэтому
    скачанные голоса всегда идут в пользовательский кэш; встроенные модели
    остаются в дистрибутиве, а поиск смотрит обе папки.
    """
    cache_dir = _sb._get_piper_cache_dir()
    if getattr(sys, 'frozen', False):
        return cache_dir
    project_dir = _sb._get_piper_model_dir()
    try:
        os.makedirs(project_dir, exist_ok=True)
        probe = os.path.join(project_dir, '.write-test')
        with open(probe, 'wb') as f:
            f.write(b'1')
        os.remove(probe)
        return project_dir
    except Exception:
        swallow.note('tts_installers._piper_install_dir:L119')
        return cache_dir
