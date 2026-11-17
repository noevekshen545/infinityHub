"""
Провайдер LLM для Hermes Agent.

Задача модуля — не тащить вторую модель: агент должен работать на ТОМ ЖЕ
движке, что и всё приложение. В Infinity Search это OpenAI-совместимый
прокси main.py:

    POST http://127.0.0.1:<порт main.py>/openai/v1/chat/completions
    (проксирует на активный бэкенд из config.json: llamacpp / lm_studio /
     openai_compatible, включая стриминг)

Поэтому provider.ensure_provider() подставляет в .hermes-portable/config.yaml:

    model:
      default:    <модель из config.json>
      provider:   custom
      base_url:   http://127.0.0.1:<порт>/openai/v1
      api_mode:   chat_completions

Пользовательские правки конфига сохраняются: managed-блок перезаписывается,
остальное не трогаем (по образцу dsh-интеграции).
"""

import json
import os
import re

from . import config
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

# Блок, которым управляет модуль
_BEGIN = "# >>> managed by infinitysearch hermes_module >>>"
_END = "# <<< managed by infinitysearch hermes_module <<<"


def _model_from_config():
    """Активная модель приложения (по приоритету бэкендов в config.json).

    Читаем конфиг АКТИВНОГО профиля через config_module, а не app/config.json:
    облачный провайдер выбирается в профиле, и, пока агент брал глобальный
    файл, в config.yaml навсегда оставалось «local-model».
    """
    cfg = None
    try:
        from config_module import get_config
        cfg = get_config() or None
    except Exception:
        swallow.note('provider._model_from_config:L54')
        cfg = None
    if cfg is None:
        cfg_path = os.path.join(config.RUNJSON, "config.json")
        if not os.path.exists(cfg_path):
            return "local-model"
        try:
            with open(cfg_path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception:
            swallow.note('provider._model_from_config:L64')
            return "local-model"

    # backend=custom — активный облачный провайдер из настроек
    if (cfg.get("backend") or "") == "custom":
        return ((cfg.get("custom") or {}).get("model") or "").strip() or "local-model"

    provider = cfg.get("llm_provider") or {}
    ptype = provider.get("type") or (cfg.get("llm") or {}).get("type") or "local"
    if ptype == "local":
        return (provider.get("local") or {}).get("model") or "local-model"
    if ptype == "openai_compatible":
        return (provider.get("openai_compatible") or {}).get("model") or "local-model"
    if provider.get(ptype):
        return (provider[ptype] or {}).get("model") or "local-model"
    return (cfg.get("lm_studio") or {}).get("model") or "local-model"


def read_config_yaml():
    if not os.path.exists(config.CONFIG_YAML):
        return ""
    try:
        with open(config.CONFIG_YAML, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        swallow.note('provider.read_config_yaml:L89')
        return ""


def _strip_managed(text):
    """Убирает предыдущий managed-блок, остальное сохраняет как есть."""
    out, skipping = [], False
    for line in text.splitlines():
        if line.strip() == _BEGIN:
            skipping = True
            continue
        if skipping:
            if line.strip() == _END:
                skipping = False
            continue
        out.append(line)
    return "\n".join(out).strip()

def _context_length():
    """Длина контекста движка приложения (llama.cpp запущен с context_window).

    Агент на старте сам зондирует длину контекста у модели через /openai/v1
    и на этом зависает (лог: «Could not determine context length …»). Поэтому
    задаём значение явно, взяв его из настроек движка.
    """
    try:
        from aiengine import api as _api
        # _read_config() — синхронная (get_config() асинхронный, его здесь звать нельзя)
        cfg = _api._read_config() or {}
        for key in ("CTX", "ctx", "CONTEXT", "CONTEXT_WINDOW"):
            v = cfg.get(key)
            if v:
                return int(v)
    except Exception:
        swallow.note('provider._context_length:L123')
        pass
    return 0

def _replace_model_block(text, model, base_url):
    """Заменяет/создаёт секцию model: в YAML, не трогая остальные секции."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^model:\s*$", line):
            start = i
            break
    block = [
        "model:",
        f"  default: {_yaml_scalar(model)}",
        "  provider: custom",
        f"  base_url: {_yaml_scalar(base_url)}",
        "  api_mode: chat_completions",
    ]
    # Длина контекста: без неё агент на старте зондирует модель через
    # /openai/v1 и зависает (лог: "Could not determine context length").
    _ctx = _context_length()
    if _ctx:
        block.append(f"  context_length: {_ctx}")
    if start is None:
        return "\n".join(lines + [""] + block).strip()
    # конец секции model: — первая непустая строка уровня 0
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and not ln.startswith((" ", "\t", "#")):
            end = j
            break
    merged = lines[:start] + block + [""] + lines[end:]
    return "\n".join(merged).strip()


def _yaml_scalar(value):
    """Строка, безопасная в позиции значения YAML.

    Раньше значение вставлялось как есть: ``f"  default: {model}"``. Пока это
    был короткий id модели, всё работало; но в поле модели можно ввести путь
    вида ``E:\\models\\Qwen.gguf`` — и если в нём встретится ``#``, ``: `` или
    перевод строки (вставка из буфера), весь config.yaml переставал
    разбираться, а агент молча работал на «последних удачных настройках».

    Простые пути/id оставляем без кавычек (файл читаем глазами), всё
    остальное экранируем JSON-строкой: это подмножество YAML double-quoted
    скаляров, где кавычки, обратные слэши и переводы строк уже корректны.
    """
    text = "" if value is None else str(value)
    if (_PLAIN_SAFE_RE.match(text) and text == text.strip()
            and ": " not in text and " #" not in text
            and text.lower() not in _YAML_AMBIGUOUS):
        return text
    return json.dumps(text, ensure_ascii=False)


# Символы, с которыми plain-скаляр остаётся однозначным. Двоеточие разрешено
# только внутри (Windows-путь ``E:\...``), поэтому шаблон запрещает «: » —
# такую пару YAML прочитал бы как начало mapping.
_PLAIN_SAFE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 _.\\/:\-+()\[\]]*$")
_YAML_AMBIGUOUS = {"", "~", "null", "true", "false", "yes", "no", "on", "off",
                   "y", "n"}


def _parses_yaml(text):
    """Разбирается ли текст как YAML (None — если yaml недоступен)."""
    try:
        import yaml
    except Exception:
        swallow.note('provider._parses_yaml:L194')
        return None
    try:
        yaml.safe_load(text)
        return True
    except Exception:
        swallow.note('provider._parses_yaml:L200')
        return False


def repair_folded_empty_scalars(text):
    """Починить ``key: ""`` + значение на следующей строке.

    Ровно так выглядел повторяющийся баг в config.yaml:
        default: ""
          E:\\models\\model.gguf
    Валидный YAML так не пишется (у ключа уже есть пустое значение, и
    следующая строка оказывается «лишним» скаляром), но именно это получается,
    если записать пустую строку поверх многострочного plain-скаляра.
    Схлопываем в одну строку, сохраняя отступ и значение.
    """
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^(\s*)([^\s#][^:]*):\s*(""|\'\')\s*$', line)
        if m and i + 1 < len(lines):
            base_indent = len(m.group(1))
            parts = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                indent = len(nxt) - len(nxt.lstrip())
                tail = nxt.strip()
                # Продолжение — не ключ вида ``foo: bar`` и не элемент списка.
                is_scalar = (bool(tail) and not tail.endswith(":")
                             and not tail.startswith("- ")
                             and not re.match(r"^[^\s:][^:]*:(\s|$)", tail))
                if indent <= base_indent or not is_scalar:
                    break
                parts.append(tail)
                j += 1
            if parts:
                # Перенос строки в plain-скаляре YAML склеивает строки пробелом.
                out.append(f"{m.group(1)}{m.group(2)}: "
                           f"{_yaml_scalar(' '.join(parts))}")
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _quarantine_broken(old_text):
    """Сохранить нечитаемый config.yaml рядом, ничего не удаляя.

    Молчаливая перезапись «на лету» — это потеря пользовательских настроек:
    именно так из переносимого профиля исчезли все секции, кроме model.
    """
    try:
        import time as _time
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        path = f"{config.CONFIG_YAML}.corrupt-{stamp}"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(old_text)
        return path
    except Exception:
        swallow.note('provider._quarantine_broken:L262')
        return None


def build_config_yaml(model=None, base_url=None):
    model = model or _model_from_config()
    base_url = base_url or config.engine_base()
    text = _strip_managed(read_config_yaml())
    # Если файл уже был сломан, сначала пробуем починить известный дефект —
    # тогда сохранятся все остальные настройки агента, а не только model.
    if text and _parses_yaml(text) is False:
        repaired = repair_folded_empty_scalars(text)
        if _parses_yaml(repaired) is not False:
            text = repaired
    text = _replace_model_block(text, model, base_url)
    return f"{_BEGIN}\n{text}\n{_END}\n"


def ensure_provider(model=None, base_url=None):
    """Гарантирует, что config.yaml агента указывает на движок приложения.

    Возвращает (path, changed).
    """
    config.ensure_dirs()
    old_text = read_config_yaml()
    if old_text and _parses_yaml(old_text) is False:
        _quarantine_broken(old_text)
    wanted = build_config_yaml(model=model, base_url=base_url)
    # Никогда не пишем заведомо нечитаемый YAML: агент после такого файла
    # запускается на «последних удачных настройках» и молча игнорирует всё,
    # что пользователь поменял.
    if _parses_yaml(wanted) is False:
        raise RuntimeError(
            f"сгенерированный {config.CONFIG_YAML} не разбирается как YAML; "
            f"запись отменена (model={model!r})")
    if old_text == wanted:
        return config.CONFIG_YAML, False
    try:
        tmp = config.CONFIG_YAML + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(wanted)
        os.replace(tmp, config.CONFIG_YAML)
    except Exception as e:
        raise RuntimeError(f"не удалось записать {config.CONFIG_YAML}: {e}")
    return config.CONFIG_YAML, True


def current():
    """Текущее состояние провайдера (для /api/status)."""
    return {
        "base_url": config.engine_base(),
        "model": _model_from_config(),
        "config_yaml": config.CONFIG_YAML,
        "managed": _BEGIN in read_config_yaml(),
    }
