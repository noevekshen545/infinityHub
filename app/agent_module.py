#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_module — слой «памяти агента» и единая точка вызова LLM.

Почему файл существует
----------------------
Старый агент ASIO (agent_module + asio_core + asio_engine) выведен из
эксплуатации (config.json -> agent.legacy_asio.enabled = false), и модуль
был удалён из сборки. Но на `from agent_module import ...` продолжали
ссылаться живые части приложения:

    routes/extension_builder.py  — ИИ в билдере расширений (ai-generate,
                                   ai-build, ai-improve) — падал с
                                   ModuleNotFoundError
    search_v5.py                 — исправление опечаток в поиске
    modules/stats_recommend.py   — ИИ-рекомендации
    agent_scheduler.py           — авто-скиллы, планировщик задач
    conversation_manager.py      — сжатие core memory, индекс памяти
    main.py                      — консолидация памяти в граф
    autogen_agent.py             — legacy-обёртка process_message

Вместо того чтобы затыкать каждое место заглушкой, здесь реализовано то,
что от модуля реально требуется:

  * call_llm()           — рабочий вызов LLM через llm_service.lm_studio_request
                            (тот же движок, что у /ask и /chat)
  * core memory (L3)     — JSON-хранилище явно записанных фактов
  * TOOLS_DIR + реестр   — авто-скиллы пишутся и регистрируются по-настоящему
  * сессии агента        — load_session/_save_session
  * build_system_prompt  — промпт из core memory + релевантной памяти L2
  * process_message      — простой цикл агента с вызовом инструментов
  * consolidate_to_graph — перенос памяти в граф памяти

Тяжёлые зависимости не импортируются на уровне модуля: `server` и
`memory_levels` подтягиваются лениво, чтобы не было циклических импортов
(server -> main -> agent_module -> server).
"""

import json
import os
import re
import sys
import time
import uuid
import logging
import swallow

logger = logging.getLogger("infinity")

# ─────────────────────────────────────────────────────────────
# Каталоги
# ─────────────────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _bundle_root() -> str:
    """Корень переносимой сборки (папка, где лежат app/, data/, models/)."""
    parent = os.path.dirname(APP_DIR)
    for marker in ("data", "models", "engine"):
        if os.path.isdir(os.path.join(parent, marker)):
            return parent
    return APP_DIR


ROOT = _bundle_root()
AGENT_BASE = os.path.join(APP_DIR, "data", "agent")
MEMORY_BASE = os.path.join(ROOT, "data", "agent_memory")

CORE_MEMORY_FILE = os.path.join(MEMORY_BASE, "core_memory.json")
TOOLS_DIR = os.path.join(AGENT_BASE, "tools")
SESSIONS_DIR = os.path.join(MEMORY_BASE, "sessions")
TOOLS_REGISTRY = os.path.join(AGENT_BASE, "tools_registry.json")

for _d in (AGENT_BASE, TOOLS_DIR, MEMORY_BASE, SESSIONS_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception:
        swallow.note('agent_module.<module>:L78')
        pass


# ─────────────────────────────────────────────────────────────
# LLM
# ─────────────────────────────────────────────────────────────
def merge_system_messages(messages):
    """Склеить system-сообщения в ОДНО первое сообщение диалога.

    Перенесено из исходного агента (runjson/agent_module.py, функция
    merge_system_messages). Это не косметика: chat-шаблоны локальных моделей
    на llama.cpp допускают только один system-блок В НАЧАЛЕ диалога. Второе
    system-сообщение в любом месте даёт
        «Jinja Exception: System message must be at the beginning» → HTTP 500.
    Порядок остальных сообщений (user/assistant/tool) сохраняется,
    пустые system отбрасываются.
    """
    if not isinstance(messages, list) or not messages:
        return messages
    sys_parts, rest = [], []
    had_system = False
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            had_system = True
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                sys_parts.append(c)
        else:
            rest.append(m)
    if not sys_parts:
        return rest if had_system else messages
    m0 = messages[0] if isinstance(messages[0], dict) else None
    if len(sys_parts) == 1 and m0 and m0.get("role") == "system":
        return messages
    return [{"role": "system", "content": "\n\n".join(sys_parts)}] + rest


def get_llm_config():
    """Провайдер LLM из config.json -> llm_provider.

    Раздел llm_provider при переносе сборки на Windows был потерян, и весь
    код генерации (билдер расширений, рекомендации, правка опечаток) стал
    работать только через локальную модель с маленьким окном вывода. Исходная
    схема (см. конфиг старой сборки) поддерживала два провайдера —
    «local» и «openai_compatible» — с цепочкой отката из нескольких моделей.
    Здесь эта схема восстановлена поверх llm_service.lm_studio_request.
    """
    try:
        from config_module import get_config
        cfg = get_config() or {}
    except Exception:
        swallow.note('agent_module.get_llm_config:L130')
        cfg = {}
    pc = cfg.get("llm_provider") or {}
    ptype = pc.get("type") or ""
    if not ptype:
        # Старый формат: backend -> провайдер
        ptype = "local" if cfg.get("backend", "llamacpp") in ("llamacpp", "lmstudio") else "openai_compatible"
    out = {"type": ptype}
    out.update(pc.get(ptype) or {})
    out["type"] = ptype
    # backend=custom (настройка «Облачный провайдер»): секции llm_provider в
    # профиле нет, и openai_compatible оставался без base_url → весь код
    # генерации падал с «в провайдерe не задан base_url». Берём параметры из
    # активной записи списка custom_providers.
    if out["type"] == "openai_compatible" and not out.get("base_url") and not out.get("url"):
        try:
            import server
            from modules import llm_text
            base = llm_text.get_llm_base_url() if server.LLM_BACKEND == "custom" else ""
        except Exception:
            swallow.note('agent_module.get_llm_config:L150')
            base = ""
        if base:
            out["base_url"] = base.rstrip("/") + "/v1"
            out["api_key"] = out.get("api_key") or server.CUSTOM_API_KEY
            out["model"] = out.get("model") or server.CUSTOM_MODEL
    return out


def _chat_local(messages, model, temperature, max_tokens, **_):
    """Локальный движок (llama.cpp / LM Studio) — через modules.llm_service."""
    from modules import llm_service
    res = llm_service.lm_studio_request(
        messages, max_tokens=max_tokens, model=model,
        temperature=temperature)
    if isinstance(res, dict):
        content = res.get("content", "") or ""
        # Reasoning-модель (Qwen3 и т.п.) может потратить весь max_tokens на
        # размышление и вернуть пустой content. Раньше билдер расширений
        # получал пустую строку и падал с «Не удалось извлечь JSON».
        if not content.strip():
            content = res.get("reasoning", "") or ""
        return content
    return str(res or "")


def _chat_openai_compatible(cfg, messages, model, temperature, max_tokens, **_):
    """Внешний OpenAI-совместимый провайдер (OpenRouter и т.п.)."""
    import json as _json
    import urllib.request

    base = (cfg.get("base_url") or cfg.get("url") or "").rstrip("/")
    if not base:
        raise RuntimeError("в провайдере не задан base_url")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {
        "model": model, "messages": messages,
        "max_tokens": int(max_tokens or 4096),
        "temperature": temperature,
    }
    if cfg.get("reasoning_enabled"):
        payload["reasoning"] = {"enabled": True}
    headers = {"Content-Type": "application/json"}
    key = cfg.get("api_key") or os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        url, data=_json.dumps(payload).encode("utf-8"), headers=headers)
    timeout = int(cfg.get("timeout", 300) or 300)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode("utf-8", "replace"))
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"пустой ответ провайдера: {str(data)[:200]}")
    return (choices[0].get("message") or {}).get("content", "") or ""


def call_llm(messages, model=None, temperature=0.7, max_tokens=2048,
             context_length=None, system_prompt=None, retries=1):
    """Единый синхронный вызов LLM. Возвращает строку ответа.

    Поддерживает два провайдера (config.json -> llm_provider):
      * local                — локальный llama.cpp / LM Studio через server
      * openai_compatible    — OpenRouter и подобные (base_url + api_key)

    Для openai_compatible работает цепочка отката available_models: если
    модель не ответила, берётся следующая. Это то, чего не хватало билдеру
    расширений — одна слабая квантованная модель роняла всю генерацию.

    Сообщения склеиваются в один system-блок (см. merge_system_messages).
    """
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    messages = list(messages or [])
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}] + messages
    messages = merge_system_messages(messages)
    if not messages:
        return ""

    cfg = get_llm_config()
    ptype = cfg.get("type", "local")

    if ptype == "openai_compatible":
        chain = [model] if model else []
        chain += [m for m in (cfg.get("available_models") or []) if m not in chain]
        if not chain:
            chain = [cfg.get("model")]
        chain = [m for m in chain if m]
        # Лимит вывода у облака часто задан жёстче — не обрезаем его нашим 2048.
        eff_max = int(cfg.get("max_tokens") or max_tokens or 4096)
        last = None
        for m in chain:
            try:
                logger.info("[agent_module] LLM %s (%s)", ptype, m)
                return _chat_openai_compatible(cfg, messages, m, temperature, eff_max)
            except Exception as e:
                last = e
                logger.warning("[agent_module] модель %s не ответила: %s", m, e)
        raise last or RuntimeError("нет доступных моделей у провайдера")

    last = None
    for attempt in range(max(1, retries)):
        try:
            text = _chat_local(messages, model, temperature, max_tokens)
            if text and not text.startswith("Ошибка AI"):
                return text
            last = RuntimeError(text or "пустой ответ")
        except Exception as e:
            last = e
            logger.warning("[agent_module] LLM попытка %d не удалась: %s",
                           attempt + 1, e)
    if last:
        raise last
    return ""


def chat(messages, **kw):
    """Словарный вариант call_llm — возвращает {content, reasoning}."""
    reasoning = kw.pop("reasoning", "")
    return {"content": call_llm(messages, **kw), "reasoning": reasoning}


def llm_available(timeout: float = 2.0) -> bool:
    """Доступен ли движок — чтобы UI не предлагал ИИ, когда его нет.

    Проверяем тот самый URL, куда реально ходит llm_service.lm_studio_request
    (llm_service.get_llm_url()), а не захардкоженный порт 8080: в портативной
    сборке backend по умолчанию llamacpp, но порт и адрес задаёт config.json
    и могут отличаться.
    """
    try:
        import urllib.request
        from modules import llm_service
        url = llm_service.get_llm_url()
        if url.endswith("/chat/completions"):
            url = url[: -len("/chat/completions")].rstrip("/")
        base = url if url.count("/") >= 2 else url.split("/v1")[0]
        with urllib.request.urlopen(base + "/models", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        swallow.note('agent_module.llm_available:L291')
        return False
# ─────────────────────────────────────────────────────────────
# L3 — явные факты (core memory)
# ─────────────────────────────────────────────────────────────
def load_core_memory() -> dict:
    """Загрузить core memory (категория -> список записей)."""
    if not os.path.exists(CORE_MEMORY_FILE):
        return {}
    try:
        with open(CORE_MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("[MEMORY-L3] не прочитать core memory: %s", e)
        return {}


def save_core_memory(memory: dict) -> bool:
    """Сохранить core memory атомарно (через временный файл)."""
    tmp = CORE_MEMORY_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(CORE_MEMORY_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(memory or {}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CORE_MEMORY_FILE)
        return True
    except Exception as e:
        logger.error("[MEMORY-L3] не сохранить core memory: %s", e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            swallow.note('agent_module.save_core_memory:L324')
            pass
        return False


def memory_store(text, kind="fact", title=""):
    """Записать явный факт в core memory и продублировать в L2."""
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "пустой текст"}
    memory = load_core_memory()
    entry = {"content": text, "kind": kind, "title": title, "time": time.time()}
    memory.setdefault(kind, []).append(entry)
    ok = save_core_memory(memory)
    try:
        import memory_levels
        memory_levels.remember_global(text, kind=kind)
    except Exception:
        swallow.note('agent_module.memory_store:L342')
        pass
    return {"ok": ok, "count": len(memory.get(kind, []))}


# ─────────────────────────────────────────────────────────────
# Реестр инструментов (авто-скиллы)
# ─────────────────────────────────────────────────────────────
def _load_registry() -> dict:
    if not os.path.exists(TOOLS_REGISTRY):
        return {}
    try:
        with open(TOOLS_REGISTRY, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        swallow.note('agent_module._load_registry:L358')
        return {}


def register_tool(name: str, description: str = "", path: str = ""):
    """Зарегистрировать инструмент агента (авто-скилл)."""
    reg = _load_registry()
    reg[str(name)] = {
        "name": str(name),
        "description": str(description or ""),
        "path": str(path or ""),
        "registered": time.time(),
    }
    try:
        os.makedirs(os.path.dirname(TOOLS_REGISTRY), exist_ok=True)
        tmp = TOOLS_REGISTRY + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TOOLS_REGISTRY)
    except Exception as e:
        logger.error("[TOOLS] не сохранить реестр: %s", e)
        return False
    return True


def list_tools():
    """Список зарегистрированных инструментов."""
    out = []
    for name, meta in _load_registry().items():
        fp = meta.get("path", "")
        out.append({
            "name": name,
            "description": meta.get("description", ""),
            "path": fp,
            "exists": bool(fp) and os.path.exists(fp),
        })
    return out


def run_tool(name: str, **kwargs):
    """Выполнить зарегистрированный инструмент и вернуть его вывод."""
    import subprocess
    meta = _load_registry().get(str(name))
    if not meta:
        return {"ok": False, "error": f"инструмент '{name}' не зарегистрирован"}
    path = meta.get("path", "")
    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"файл инструмента не найден: {path}"}
    cmd = [sys.executable, path] if path.endswith(".py") else [path]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            input=json.dumps(kwargs) if kwargs else "",
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                           if os.name == "nt" else 0),
        )
        return {"ok": r.returncode == 0, "stdout": r.stdout,
                "stderr": r.stderr, "code": r.returncode}
    except Exception as e:
        swallow.note('agent_module.run_tool:L417', e)
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# Сессии
# ─────────────────────────────────────────────────────────────
def _session_path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(session_id or "default"))[:80]
    return os.path.join(SESSIONS_DIR, safe + ".json")


def load_session(session_id: str = "default") -> dict:
    """Загрузить состояние сессии агента."""
    fp = _session_path(session_id)
    if os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("messages", [])
                d.setdefault("task_summaries", [])
                return d
        except Exception:
            swallow.note('agent_module.load_session:L441')
            pass
    return {"id": str(session_id), "messages": [],
            "task_summaries": [], "created": time.time()}


def _save_session(session_id: str, session: dict) -> bool:
    """Сохранить состояние сессии (историческое имя — импортируется снаружи)."""
    fp = _session_path(session_id)
    try:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fp)
        return True
    except Exception as e:
        logger.error("[AGENT] не сохранить сессию: %s", e)
        return False


def save_session(session_id: str, session: dict) -> bool:
    return _save_session(session_id, session)


# ─────────────────────────────────────────────────────────────
# Контекст и системный промпт
# ─────────────────────────────────────────────────────────────
def get_memory_context(query: str = "", session_id: str = "", top_k: int = 4) -> str:
    """Собрать текстовый контекст памяти: L3 (всегда) + L2 (по запросу)."""
    parts = []
    for cat, items in load_core_memory().items():
        if not isinstance(items, list):
            continue
        for it in items[-20:]:
            txt = (it.get("content") or it.get("summary") or "") if isinstance(it, dict) else str(it)
            if txt:
                parts.append(f"[{cat}] {txt}")

    try:
        import memory_levels
        for r in (memory_levels.recall_global(query, top_k=top_k) if query else []):
            if r.get("text"):
                parts.append(f"[{r.get('kind', 'fact')}] {r['text']}")
    except Exception:
        swallow.note('agent_module.get_memory_context:L486')
        pass

    if session_id:
        for s in (load_session(session_id).get("task_summaries") or [])[-5:]:
            txt = s.get("summary") if isinstance(s, dict) else str(s)
            if txt:
                parts.append(f"[итог задачи] {txt}")
    return "\n".join(parts)


SYSTEM_PROMPT = (
    "Ты — Infinity Agent, встроенный помощник приложения Infinity Search.\n"
    "Отвечай по-русски, структурированно и по делу. Опирайся на факты из "
    "контекста памяти, если они есть. Не выдумывай то, чего в контексте нет."
)


def build_system_prompt(query: str = "", session_id: str = "") -> str:
    """Системный промпт агента вместе с релевантным контекстом памяти."""
    ctx = get_memory_context(query, session_id)
    return SYSTEM_PROMPT if not ctx else f"{SYSTEM_PROMPT}\n\n=== Контекст памяти ===\n{ctx}"


# ─────────────────────────────────────────────────────────────
# Консолидация памяти в граф
# ─────────────────────────────────────────────────────────────
def consolidate_to_graph(limit: int = 200) -> dict:
    """Перенести накопленные факты в граф памяти (memory_graph)."""
    texts = []
    for items in load_core_memory().values():
        if not isinstance(items, list):
            continue
        for it in items:
            txt = (it.get("content") or it.get("summary") or "") if isinstance(it, dict) else str(it)
            if txt:
                texts.append(txt)
    if not texts:
        return {"ok": True, "nodes": 0, "note": "core memory пуста"}
    try:
        import memory_graph
        res = memory_graph.extract_knowledge_to_graph("\n".join(texts[-limit:]))
        return {"ok": True, "nodes": len(texts[-limit:]), "result": res}
    except Exception as e:
        logger.warning("[MEMORY-GRAPH] консолидация не удалась: %s", e)
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# Простой цикл агента
# ─────────────────────────────────────────────────────────────
def process_message(session_id: str, message: str, use_tools: bool = True,
                    max_iterations: int = 4, **kwargs) -> dict:
    """Обработать сообщение агентом.

    Полноценный автономный агент живёт в модуле Infinity Agent (/agentai) — он
    вызывается по HTTP. Здесь — лёгкий локальный цикл: ответ модели плюс,
    при явной команде /run <инструмент>, запуск зарегистрированного скилла.
    """
    message = str(message or "").strip()
    if not message:
        return {"ok": False, "error": "пустое сообщение"}

    sess = load_session(session_id)
    sess["messages"].append({"role": "user", "content": message,
                             "time": time.time()})

    tools = list_tools() if use_tools else []
    tool_hint = (("\n\nДоступные инструменты: " + ", ".join(t["name"] for t in tools))
                 if tools else "")
    messages = [
        {"role": "system",
         "content": build_system_prompt(message, session_id) + tool_hint},
        {"role": "user", "content": message},
    ]

    try:
        answer = call_llm(messages, temperature=0.5, max_tokens=2048)
    except Exception as e:
        logger.error("[AGENT] LLM недоступен: %s", e)
        return {"ok": False, "error": f"Движок ИИ недоступен: {e}"}

    result = {"ok": True, "response": answer, "session_id": session_id}

    m = re.match(r"^\s*/run\s+([A-Za-z0-9_.-]+)\s*(.*)$", message, re.S)
    if m and use_tools:
        name, argstr = m.group(1), m.group(2).strip()
        args = {}
        if argstr.startswith("{"):
            try:
                args = json.loads(argstr)
            except Exception:
                swallow.note('agent_module.process_message:L578')
                args = {"input": argstr}
        elif argstr:
            args = {"input": argstr}
        result["tool"] = run_tool(name, **args)

    sess["messages"].append({"role": "assistant", "content": answer,
                             "time": time.time()})
    sess["task_summaries"].append({
        "summary": message[:200], "time": time.time(),
        "id": uuid.uuid4().hex[:8],
    })
    sess["updated"] = time.time()
    _save_session(session_id, sess)
    return result


def new_session_id(prefix: str = "s") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


if __name__ == "__main__":
    print("agent_module: LLM доступен —", llm_available())
    print("core memory  :", len(load_core_memory()), "категорий")
    print("инструменты  :", len(list_tools()))
    print("каталоги     :\n  CORE_MEMORY_FILE =", CORE_MEMORY_FILE,
          "\n  TOOLS_DIR        =", TOOLS_DIR)
