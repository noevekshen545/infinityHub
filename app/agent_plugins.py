#!/usr/bin/env python3
"""
agent_plugins — самосоздаваемые плагины агента (DeepSeek Harness style).

Плагин = папка с манифестом + python-кодом:
    ~/.infinity/asio_agent_v4/plugins/<name>/plugin.json   # манифест
    ~/.infinity/asio_agent_v4/plugins/<name>/plugin.py     # инструменты

Код плагина объявляет функции вида:

    def tool_<имя>(args: str, context: dict = None) -> dict:
        '''Описание для промпта агента.'''
        ...
        return {"success": True, "output": "..."}

Агент может создавать плагины САМ через инструменты:
    [TOOL:plugin_create] {"name": "...", "description": "...", "code": "..."}
    [TOOL:plugin_list] / [TOOL:plugin_enable] / [TOOL:plugin_delete]

Hot-reload: изменение кода подхватывается без перезапуска сервера.
Безопасность: compile()-проверка + запрещённые паттерны.
"""
import os
import re
import json
import time
import uuid
import logging
import importlib.util
import threading
import sys
from typing import Dict, List, Any, Optional, Callable
import swallow

logger = logging.getLogger("infinity")

AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent_v4")
PLUGINS_DIR = os.path.join(AGENT_BASE, "plugins")
os.makedirs(PLUGINS_DIR, exist_ok=True)

_FORBIDDEN_PATTERNS = [
    (r"shutil\.rmtree\s*\(\s*[\"']/", "удаление корня"),
    (r"os\.system", "os.system запрещён в плагинах"),
    (r"\bsubprocess\b", "subprocess запрещён в плагинах"),
]

_lock = threading.RLock()
_registry: Dict[str, Dict[str, Any]] = {}
_loaded_modules: Dict[str, Any] = {}


def validate_plugin_code(code: str) -> tuple:
    if not code or not code.strip():
        return False, "пустой код"
    for pat, why in _FORBIDDEN_PATTERNS:
        if re.search(pat, code):
            return False, f"запрещённый паттерн: {why}"
    if not re.search(r"\bdef\s+tool_\w+\s*\(", code):
        return False, "нет ни одной функции tool_*"
    try:
        compile(code, "<plugin>", "exec")
    except SyntaxError as e:
        swallow.note('agent_plugins.validate_plugin_code:L62', e)
        return False, f"синтаксическая ошибка: {e}"
    return True, "ok"


def _plugin_dir(name: str) -> str:
    safe = re.sub(r"[^a-z0-9_\-]", "_", (name or "").lower())
    return os.path.join(PLUGINS_DIR, safe)


def _manifest_path(name: str) -> str:
    return os.path.join(_plugin_dir(name), "plugin.json")


def _code_path(name: str) -> str:
    return os.path.join(_plugin_dir(name), "plugin.py")


def _load_manifest(name: str) -> Optional[Dict]:
    fp = _manifest_path(name)
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[agent_plugins] manifest read error {name}: {e}")
        return None


def _extract_doc(code: str, func_name: str) -> str:
    m = re.search(
        r"def\s+" + re.escape(func_name) +
        r"\s*\(.*?\):\s*\n\s*(?:'''|\"\"\")(.*?)(?:'''|\"\"\")",
        code, re.S)
    if m:
        return m.group(1).strip().split("\n")[0][:200]
    return ""


def _bump(version: str) -> str:
    parts = version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except Exception:
        swallow.note('agent_plugins._bump:L108')
        return "1.0.1"


def save_plugin(name: str, description: str, code: str,
                created_by: str = "agent", ui_css: str = "",
                ui_js: str = "") -> Dict[str, Any]:
    """Создать/обновить плагин. Возвращает {"success", "manifest"} или ошибку."""
    ok, why = validate_plugin_code(code)
    if not ok:
        return {"success": False, "error": f"код не прошёл проверку: {why}"}

    d = _plugin_dir(name)
    os.makedirs(d, exist_ok=True)

    tool_names = re.findall(r"\bdef\s+(tool_\w+)\s*\(", code)
    prev = _load_manifest(name) or {}
    manifest = {
        "name": name,
        "description": description,
        "version": _bump(prev.get("version", "1.0.0")) if prev else "1.0.0",
        "enabled": prev.get("enabled", True),
        "tools": [{"name": t,
                   "description": _extract_doc(code, t)} for t in tool_names],
        "created_by": prev.get("created_by", created_by),
        "created_at": prev.get("created_at", time.time()),
        "updated_at": time.time(),
        "has_ui": prev.get("has_ui", False),
    }
    with open(_manifest_path(name), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(_code_path(name), "w", encoding="utf-8") as f:
        f.write(code)
    has_ui_before = bool(ui_css or ui_js)
    if ui_css:
        with open(os.path.join(_plugin_dir(name), "ui.css"),
                  "w", encoding="utf-8") as f:
            f.write(ui_css)
    if ui_js:
        with open(os.path.join(_plugin_dir(name), "ui.js"),
                  "w", encoding="utf-8") as f:
            f.write(ui_js)
    manifest["has_ui"] = has_ui_before

    reload_plugin(name)
    logger.info(f"[agent_plugins] saved plugin '{name}' ({len(tool_names)} tools)")
    return {"success": True, "manifest": manifest}


def delete_plugin(name: str) -> Dict[str, Any]:
    import shutil
    d = _plugin_dir(name)
    if not os.path.isdir(d):
        return {"success": False, "error": "плагин не найден"}
    # Забываем подтверждения UI до удаления папки (иначе состояние останется висеть).
    try:
        import agent_ui
        agent_ui.get_registry().forget(name)
    except Exception:
        swallow.note('agent_plugins.delete_plugin:L167')
        pass
    with _lock:
        _loaded_modules.pop(name, None)
        _registry.pop(name, None)
    shutil.rmtree(d, ignore_errors=True)
    logger.info(f"[agent_plugins] deleted plugin '{name}'")
    return {"success": True}


def set_enabled(name: str, enabled: bool) -> Dict[str, Any]:
    m = _load_manifest(name)
    if not m:
        return {"success": False, "error": "плагин не найден"}
    m["enabled"] = bool(enabled)
    with open(_manifest_path(name), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    if not enabled:
        with _lock:
            _loaded_modules.pop(name, None)
            _registry.pop(name, None)
    else:
        reload_plugin(name)
    return {"success": True, "manifest": m}


def reload_plugin(name: str) -> Optional[Dict[str, Any]]:
    """(Пере)загрузить модуль плагина и зарегистрировать инструменты."""
    m = _load_manifest(name)
    if not m or not m.get("enabled", True):
        return None
    code_path = _code_path(name)
    if not os.path.exists(code_path):
        return None
    try:
        mod_name = (f"agent_plugin_{name.replace('-', '_')}_"
                    f"{uuid.uuid4().hex[:6]}")
        spec = importlib.util.spec_from_file_location(mod_name, code_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        with _lock:
            _loaded_modules[name] = mod
            tools = {}
            for t in m.get("tools", []):
                fn = getattr(mod, t["name"], None)
                if callable(fn):
                    tools[t["name"]] = fn
            _registry[name] = {"manifest": m, "tools": tools,
                               "module": mod, "loaded_at": time.time()}
        logger.info(f"[agent_plugins] loaded '{name}' ({len(tools)} tools)")
        return _registry.get(name)
    except Exception as e:
        logger.error(f"[agent_plugins] reload '{name}' error: {e}",
                     exc_info=True)
        return None


def load_all() -> int:
    count = 0
    if not os.path.isdir(PLUGINS_DIR):
        return 0
    for d in os.listdir(PLUGINS_DIR):
        full = os.path.join(PLUGINS_DIR, d)
        if os.path.isdir(full) and os.path.exists(
                os.path.join(full, "plugin.json")):
            if reload_plugin(d):
                count += 1
    logger.info(f"[agent_plugins] loaded {count} plugins")
    return count


def get_tool_fn(tool_name: str) -> Optional[Callable]:
    # Lazy discovery: Cordis-плагины доступны даже если startup hook не сработал.
    if not _registry:
        try:
            load_all()
        except Exception as e:
            logger.debug(f"[CORDIS] lazy discovery failed: {e}")
    with _lock:
        for info in _registry.values():
            fn = info["tools"].get(tool_name)
            if fn is not None:
                return fn
    return None


def all_plugin_tools_description() -> str:
    if not _registry:
        try: load_all()
        except Exception:
            swallow.note('agent_plugins.all_plugin_tools_description:L258')
            pass
    lines = []
    with _lock:
        for name, info in _registry.items():
            mdesc = info["manifest"].get("description", "")
            for t in info["manifest"].get("tools", []):
                lines.append(f"- {t['name']}: {t.get('description', '')} "
                             f"(плагин: {name} — {mdesc})")
    return "\n".join(lines)


def list_plugins() -> List[Dict[str, Any]]:
    result = []
    if not os.path.isdir(PLUGINS_DIR):
        return result
    for d in os.listdir(PLUGINS_DIR):
        m = _load_manifest(d)
        if not m:
            continue
        p = dict(m)
        p["dir"] = d
        p["loaded"] = d in _registry
        p["code"] = get_plugin_code(d)
        result.append(p)
    result.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return result


def get_plugin_code(name: str) -> Optional[str]:
    try:
        with open(_code_path(name), encoding="utf-8") as f:
            return f.read()
    except Exception:
        swallow.note('agent_plugins.get_plugin_code:L292')
        return None


# ===================== UI-СЛОЙ ПЛАГИНОВ =====================

def _ui_path(name: str, kind: str) -> str:
    return os.path.join(_plugin_dir(name),
                        "ui.css" if kind == "css" else "ui.js")


def list_ui_plugins() -> List[Dict[str, Any]]:
    """Плагины с UI-частью (для loader'а на страницах)."""
    out = []
    for p in list_plugins():
        if not p.get("enabled"):
            continue
        has_css = os.path.exists(_ui_path(p["name"], "css"))
        has_js = os.path.exists(_ui_path(p["name"], "js"))
        if has_css or has_js or p.get("has_ui"):
            out.append({"name": p["name"],
                        "description": p.get("description", ""),
                        "has_css": has_css, "has_js": has_js})
    return out


def get_ui_file(name: str, kind: str) -> Optional[str]:
    """kind: 'css' | 'js'. Путь строго внутри папки плагина."""
    if kind not in ("css", "js"):
        return None
    fp = _ui_path(name, kind)
    base = os.path.abspath(_plugin_dir(name))
    if not os.path.abspath(fp).startswith(base):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            return f.read()
    except Exception:
        swallow.note('agent_plugins.get_ui_file:L330')
        return None


def save_ui_files(name: str, ui_css: str = "", ui_js: str = "") -> Dict:
    m = _load_manifest(name)
    if not m:
        return {"success": False, "error": "плагин не найден"}
    if ui_css:
        with open(_ui_path(name, "css"), "w", encoding="utf-8") as f:
            f.write(ui_css)
    if ui_js:
        with open(_ui_path(name, "js"), "w", encoding="utf-8") as f:
            f.write(ui_js)
    m["has_ui"] = True
    with open(_manifest_path(name), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    return {"success": True}

# ===================== HERMES-САМООБУЧЕНИЕ =====================

def _get_chain_stats() -> Dict[str, Any]:
    try:
        from agent_intelligence import CHAINING_FILE
        if os.path.exists(CHAINING_FILE):
            with open(CHAINING_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[agent_plugins] chain stats error: {e}")
    return {}


def learning_suggestions() -> List[Dict[str, Any]]:
    """Цепочки-кандидаты: успех >= 70%, использований >= 2."""
    try:
        from agent_intelligence import get_best_tool_chain
    except Exception:
        swallow.note('agent_plugins.learning_suggestions:L367')
        return []
    chains = _get_chain_stats()
    out = []
    for task_type in chains.keys():
        chain = get_best_tool_chain(task_type)
        if not chain:
            continue
        plugin_name = f"workflow_{re.sub(r'[^a-z0-9_]', '_', task_type.lower())[:30]}"
        already = os.path.exists(_manifest_path(plugin_name))
        stats = chains.get(task_type, {}).get(" → ".join(chain), {})
        out.append({
            "task_type": task_type,
            "chain": chain,
            "uses": stats.get("uses", 0),
            "success_rate": round(stats.get("successes", 0) /
                                  max(1, stats.get("uses", 1)), 2),
            "plugin_name": plugin_name,
            "already_learned": already,
        })
    return out


def learn_workflow_plugin(task_type: str) -> Dict[str, Any]:
    """Создать workflow-плагин из лучшей цепочки (Hermes-style)."""
    from agent_intelligence import get_best_tool_chain
    chain = get_best_tool_chain(task_type)
    if not chain:
        return {"success": False,
                "output": f"нет достаточно успешной цепочки для '{task_type}'"}
    plugin_name = f"workflow_{re.sub(r'[^a-z0-9_]', '_', task_type.lower())[:30]}"
    steps_json = json.dumps(chain, ensure_ascii=False)
    tool_name = "tool_" + plugin_name
    chain_desc = " -> ".join(chain)
    code = (
        "# Автосгенерировано системой самообучения (Hermes-style)\n"
        f"def {tool_name}(args, context=None):\n"
        f'    """Выученный workflow [{task_type}]: {chain_desc}. '
        'Args: описание задачи."""\n'
        "    try:\n"
        "        import json as _json\n"
        "        from agent_tools import execute_tool\n"
        f"        steps = _json.loads({steps_json!r})\n"
        "        session = (context or {}).get('session') or {}\n"
        "        folder = (context or {}).get('folder')\n"
        "        url = (context or {}).get('lm_studio_url')\n"
        "        results = []\n"
        "        for step_tool in steps:\n"
        "            r = execute_tool(step_tool, str(args), session,\n"
        "                             folder=folder, lm_studio_url=url)\n"
        "            results.append((step_tool, bool(r and r.get(\"success\")),\n"
        "                            str((r or {}).get(\"output\", \"\"))[:300]))\n"
        "        ok_all = all(x[1] for x in results)\n"
        "        summary = chr(10).join(a + \": \" + c for a, b, c in results)\n"
        "        return {\"success\": ok_all, \"output\": summary}\n"
        "    except Exception as e:\n"
        "        return {\"success\": False, \"output\": \"workflow error: \" + str(e)}\n"
    )
    res = save_plugin(name=plugin_name,
                      description=f"Выученный workflow '{task_type}': "
                                  f"{' -> '.join(chain)}",
                      code=code, created_by="self-learning")
    if not res.get("success"):
        return {"success": False, "output": res.get("error")}
    tools = [t["name"] for t in res["manifest"]["tools"]]
    return {"success": True,
            "output": f"Выучен workflow '{task_type}'! Плагин '{plugin_name}', "
                      f"инструмент: {', '.join(tools)}. "
                      f"Цепочка: {' -> '.join(chain)}."}


def tool_plugin_learn(args: str = "", **kw) -> Dict[str, Any]:
    """Без args — кандидаты; с {"task_type"} — выучить в плагин."""
    args = (args or "").strip()
    if not args or args in ("list", "{}"):
        sugg = learning_suggestions()
        if not sugg:
            return {"success": True, "output":
                    "Пока нет цепочек для обучения. Запустите несколько задач."}
        lines = ["Кандидаты на обучение:"]
        for s_ in sugg:
            mark = " [уже выучен]" if s_["already_learned"] else ""
            lines.append(f"- {s_['task_type']}: {' -> '.join(s_['chain'])} "
                         f"(исп. {s_['uses']}, успех "
                         f"{int(s_['success_rate']*100)}%){mark}")
        lines.append('Выучить: {"task_type": "..."}')
        return {"success": True, "output": "\n".join(lines)}
    data = _parse_json_args(args)
    tt = str(data.get("task_type") or data.get("name") or "").strip()
    if not tt:
        tt = args.strip().strip('"').strip("{").strip("}").replace(
            '"task_type":', "").strip()
    return learn_workflow_plugin(tt)

# ===================== МЕТА-ИНСТРУМЕНТЫ ДЛЯ АГЕНТА =====================

def _parse_json_args(args: str) -> Dict:
    try:
        data = json.loads(args)
        return data if isinstance(data, dict) else {"name": args.strip()}
    except Exception:
        swallow.note('agent_plugins._parse_json_args:L468')
        return {"name": (args or "").strip()}


def tool_plugin_create(args: str, **kw) -> Dict[str, Any]:
    """Агент создаёт себе плагин. args = JSON
    {"name","description","code"}"""
    try:
        data = json.loads(args)
    except Exception as e:
        swallow.note('agent_plugins.tool_plugin_create:L478', e)
        return {"success": False, "output": f"неверный JSON: {e}"}
    name = re.sub(r"[^a-z0-9_\-]", "_",
                  str(data.get("name", "")).lower())[:40]
    if not name:
        return {"success": False, "output": "укажите name"}
    res = save_plugin(name=name,
                      description=str(data.get("description", ""))[:500],
                      code=str(data.get("code", "")),
                      ui_css=str(data.get("ui_css", ""))[:20000],
                      ui_js=str(data.get("ui_js", ""))[:50000])
    if not res.get("success"):
        return {"success": False, "output": res.get("error")}
    tools = [t["name"] for t in res["manifest"]["tools"]]
    ui_note = ""
    if data.get("ui_css") or data.get("ui_js"):
        ui_note = (" UI-часть сохранена — стили/скрипт подключатся на "
                   "страницах с loader'ом после обновления.")
    return {"success": True, "output":
            f"Плагин '{name}' создан и активен. Инструменты: "
            f"{', '.join(tools)}. Вызывайте их как обычные инструменты."
            + ui_note}


def tool_plugin_list(args: str = "", **kw) -> Dict[str, Any]:
    plugs = list_plugins()
    if not plugs:
        return {"success": True, "output": "Плагинов пока нет. "
                "Создайте через plugin_create."}
    lines = []
    for p in plugs:
        state = "вкл" if p.get("enabled") else "выкл"
        tools = ", ".join(t["name"] for t in p.get("tools", []))
        lines.append(f"[{state}] {p['name']} v{p.get('version')} — "
                     f"{p.get('description', '')} [{tools}]")
    return {"success": True, "output": "\n".join(lines)}


def tool_plugin_enable(args: str, **kw) -> Dict[str, Any]:
    data = _parse_json_args(args)
    enabled = bool(data.get("enabled", True))
    res = set_enabled(str(data.get("name", "")), enabled)
    if not res.get("success"):
        return {"success": False, "output": res.get("error")}
    return {"success": True,
            "output": f"Плагин '{data.get('name')}' → "
                      f"{'включён' if enabled else 'выключен'}"}


def tool_plugin_delete(args: str, **kw) -> Dict[str, Any]:
    data = _parse_json_args(args)
    res = delete_plugin(str(data.get("name", "")))
    if not res.get("success"):
        return {"success": False, "output": res.get("error")}
    return {"success": True, "output": "Плагин удалён."}


META_TOOLS: Dict[str, Callable] = {
    "plugin_create": tool_plugin_create,
    "plugin_list": tool_plugin_list,
    "plugin_enable": tool_plugin_enable,
    "plugin_delete": tool_plugin_delete,
    "plugin_learn": tool_plugin_learn,
}

META_TOOLS_PROMPT = """- plugin_create: Создать себе новый плагин с инструментами. Args: JSON {"name":"имя","description":"что делает","code":"python-код с функциями def tool_<имя>(args, context=None): ... return {'success': True, 'output': '...'}","ui_css":"(опционально) CSS для страниц приложения","ui_js":"(опционально) JS: кнопки/панели UI"}. UI-часть (ui_css/ui_js) НЕ активируется сразу: она ждёт подтверждения пользователя в окне «Разрешить / Разрешить все / Отклонить» на странице агента (в ответе придёт ui_pending=true). Менять интерфейс удобнее через [TOOL:ui_set]. Запрещены subprocess/os.system.
- plugin_list: Список твоих плагинов и их инструментов.
- plugin_enable: Вкл/выкл плагин. Args: JSON {"name":"...","enabled":true}
- plugin_delete: Удалить плагин. Args: JSON {"name":"..."}
- plugin_learn: Самообучение (Hermes). Без аргументов — показать удачные цепочки инструментов; Args JSON {"task_type":"тип задачи"} — создать из неё workflow-плагин."""


if __name__ == "__main__":
    load_all()
    print(json.dumps(list_plugins(), ensure_ascii=False, indent=2)[:2000])



