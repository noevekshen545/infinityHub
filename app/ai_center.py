"""AI-центр: каталог API агента, промты к ним и пользовательские API.

Каталог действий уже живёт в action_center (GET /api/actions). AI-центр
добавляет к нему то, чего там быть не может: текст инструкции, привязанный
к конкретному действию, и новые действия, заданные администратором без
правки кода.

Пользовательское действие — это HTTP-вызов собственного API приложения по
loopback. Адрес ограничен путём /api/... (никаких чужих хостов), а вызов
идёт от имени профиля, запустившего действие: ai_center выдаёт подписанный
токен этого профиля, поэтому действие не может достать чужие данные.

Хранилище — data/ai_center.json:
    {"version": 1,
     "prompts": {"calendar.event.create": "..."},
     "custom_actions": [{id, title, description, category, method, path,
                         params, examples, prompt, enabled}]}
"""

import json
import logging
import os
import re
import threading

import httpx
import swallow

logger = logging.getLogger("infinity")

_ID_RE = re.compile(r"^custom\.[a-z0-9][a-z0-9._-]{1,70}$")
_PATH_RE = re.compile(r"^/api/[A-Za-z0-9_./-]*$")
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_MAX_PROMPT = 4000
_MAX_PARAMS_JSON = 20000
_MAX_RESPONSE = 2 * 1024 * 1024

_LOCK = threading.Lock()
_CACHE = {"mtime": None, "data": None}
_CUSTOM_IDS = set()
_SYNCED = False


def ensure_synced():
    if not _SYNCED:
        sync()


def _path():
    import profiles_module as PM
    return os.path.join(PM.DATA_DIR, "ai_center.json")


def _blank():
    return {"version": 1, "prompts": {}, "custom_actions": []}


def _normalize(data):
    if not isinstance(data, dict):
        return _blank()
    prompts = data.get("prompts")
    if not isinstance(prompts, dict):
        prompts = {}
    actions = data.get("custom_actions")
    if not isinstance(actions, list):
        actions = []
    return {
        "version": 1,
        "prompts": {str(k): str(v) for k, v in prompts.items() if str(v).strip()},
        "custom_actions": [a for a in actions if isinstance(a, dict) and a.get("id")],
    }


def load():
    """Настройки AI-центра; кэш сверяется по mtime файла."""
    path = _path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        swallow.note('ai_center.load:L79')
        mtime = None
    with _LOCK:
        if _CACHE["data"] is not None and _CACHE["mtime"] == mtime:
            return _CACHE["data"]
    data = _blank()
    if mtime is not None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _normalize(json.load(f))
        except Exception as e:
            logger.warning(f"[ai-center] настройки не прочитаны: {e}")
    with _LOCK:
        _CACHE["mtime"] = mtime
        _CACHE["data"] = data
    return data


def save(data):
    data = _normalize(data)
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    with _LOCK:
        _CACHE["data"] = data
        try:
            _CACHE["mtime"] = os.path.getmtime(path)
        except OSError:
            swallow.note('ai_center.save:L110')
            _CACHE["mtime"] = None
    return data


# --------------------------------------------------------------------------
# Промты к действиям
# --------------------------------------------------------------------------
def prompt_for(action_id):
    return str(load()["prompts"].get(str(action_id or "")) or "")


def set_prompt(action_id, prompt):
    """Привязать инструкцию к действию; пустой текст — убрать."""
    aid = str(action_id or "").strip()
    if not aid:
        raise ValueError("Не указано действие")
    import action_center
    if aid not in action_center.ACTIONS and aid not in _CUSTOM_IDS:
        raise ValueError(f"Неизвестное действие: {aid}")
    text = str(prompt or "").strip()
    if len(text) > _MAX_PROMPT:
        raise ValueError(f"Промт длиннее {_MAX_PROMPT} символов")
    data = load()
    if text:
        data["prompts"][aid] = text
    else:
        data["prompts"].pop(aid, None)
    save(data)
    return text


# --------------------------------------------------------------------------
# Пользовательские действия
# --------------------------------------------------------------------------
def _clean_params(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("params: нужен объект {имя: описание}")
    if len(json.dumps(raw, ensure_ascii=False)) > _MAX_PARAMS_JSON:
        raise ValueError("params: слишком большой объект")
    out = {}
    allowed = ("string", "int", "bool", "date", "time", "enum", "list")
    for name, spec in raw.items():
        key = str(name or "").strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$", key):
            raise ValueError(f"params: некорректное имя '{name}'")
        if not isinstance(spec, dict):
            raise ValueError(f"params.{key}: нужен объект")
        kind = str(spec.get("type") or "string").lower()
        if kind not in allowed:
            raise ValueError(f"params.{key}: тип {kind} не поддерживается")
        row = {
            "type": kind,
            "description": str(spec.get("description") or "")[:300],
            "required": bool(spec.get("required")),
            "default": spec.get("default"),
            "choices": spec.get("choices") if isinstance(spec.get("choices"), list) else None,
            "min": spec.get("min") if isinstance(spec.get("min"), int) else None,
            "max": spec.get("max") if isinstance(spec.get("max"), int) else None,
        }
        if kind == "enum" and not row["choices"]:
            raise ValueError(f"params.{key}: для enum нужен choices")
        out[key] = row
    return out


def clean_custom(raw):
    """Проверить и нормализовать пользовательское действие."""
    if not isinstance(raw, dict):
        raise ValueError("Нужен объект действия")
    aid = str(raw.get("id") or "").strip().lower()
    if not _ID_RE.match(aid):
        raise ValueError("id: латиница, начинается с custom. (например custom.weather.now)")
    method = str(raw.get("method") or "GET").strip().upper()
    if method not in _METHODS:
        raise ValueError("method: " + ", ".join(_METHODS))
    path = str(raw.get("path") or "").strip()
    if "?" in path:
        raise ValueError("path: параметры передавайте через params, а не через ?…")
    if not _PATH_RE.match(path) or "//" in path or ".." in path or "\\" in path:
        raise ValueError("path: только внутренний путь вида /api/module/…")
    if path.rstrip("/") in ("/api/ai-center",) or path.startswith("/api/ai-center/"):
        raise ValueError("path: AI-центр не может вызывать сам себя")
    title = str(raw.get("title") or "").strip()[:120]
    if not title:
        raise ValueError("title: укажите название")
    description = str(raw.get("description") or "").strip()[:1000]
    category = str(raw.get("category") or "custom").strip().lower()[:40] or "custom"
    if not re.match(r"^[a-z0-9_-]+$", category):
        raise ValueError("category: латиница, цифры, дефис")
    examples = raw.get("examples")
    if not isinstance(examples, list):
        examples = []
    examples = [e for e in examples[:10] if isinstance(e, dict)]
    prompt = str(raw.get("prompt") or "").strip()[: _MAX_PROMPT]
    return {
        "id": aid,
        "title": title,
        "description": description,
        "category": category,
        "method": method,
        "path": path,
        "params": _clean_params(raw.get("params")),
        "examples": examples,
        "prompt": prompt,
        "enabled": bool(raw.get("enabled", True)),
    }


async def _run_http(spec, profile_id, params):
    """Выполнить пользовательское действие от имени профиля."""
    import action_center
    import profiles_module as PM
    import server

    port = int((getattr(server, "CFG", {}).get("server") or {}).get("port") or 8080)
    url = f"http://127.0.0.1:{port}{spec['path']}"
    headers = {"Accept": "application/json"}
    token = PM.make_agent_token(profile_id) if profile_id else ""
    if token:
        headers["X-Infinity-Profile-Token"] = token
    kwargs = {}
    if spec["method"] == "GET":
        kwargs["params"] = params
    else:
        kwargs["json"] = params
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            resp = await client.request(spec["method"], url, headers=headers, **kwargs)
    except Exception as e:
        raise action_center.ActionError(f"API недоступен: {e}", 502)

    if len(resp.content) > _MAX_RESPONSE:
        raise action_center.ActionError("Ответ API слишком большой", 502)
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        try:
            body = resp.json()
        except Exception:
            swallow.note('ai_center._run_http:L251')
            body = resp.text[:100000]
    else:
        body = resp.text[:100000]

    if resp.status_code >= 400:
        text = ""
        if isinstance(body, dict):
            text = str(body.get("error") or body.get("detail") or "")
        else:
            text = str(body or "")
        raise action_center.ActionError(
            text[:500] or f"API ответил {resp.status_code}",
            400 if resp.status_code < 500 else 502,
            {"upstream_status": resp.status_code})
    return body


def _register(spec):
    import action_center as AC
    aid = spec["id"]
    if aid in AC.ACTIONS and aid not in _CUSTOM_IDS:
        raise ValueError(f"Действие уже существует: {aid}")

    async def handler(ctx, params, _spec=spec):
        return await _run_http(_spec, ctx.get("profile_id") or "", params)

    AC.ACTIONS[aid] = AC.Action(
        id=aid,
        title=spec["title"],
        description=spec["description"],
        category=spec["category"],
        params=spec["params"],
        handler=handler,
        examples=list(spec.get("examples") or []),
        read_only=(spec["method"] == "GET"),
    )
    _CUSTOM_IDS.add(aid)


def _unregister(aid):
    import action_center as AC
    if aid in _CUSTOM_IDS:
        AC.ACTIONS.pop(aid, None)
        _CUSTOM_IDS.discard(aid)


def sync():
    """Привести реестр действий в соответствие с файлом настроек."""
    global _SYNCED
    _SYNCED = True
    data = load()
    wanted = {a["id"]: a for a in data["custom_actions"] if a.get("enabled", True)}
    for aid in list(_CUSTOM_IDS):
        if aid not in wanted:
            _unregister(aid)
    for spec in wanted.values():
        try:
            clean = clean_custom(spec)
            _register(clean)
        except Exception as e:
            logger.warning(f"[ai-center] действие {spec.get('id')} не загружено: {e}")


def add_custom(raw):
    import action_center
    spec = clean_custom(raw)
    if spec["id"] in _CUSTOM_IDS or spec["id"] in action_center.ACTIONS:
        raise ValueError(f"Действие уже существует: {spec['id']}")
    data = load()
    data["custom_actions"].append(spec)
    if spec["prompt"]:
        data["prompts"][spec["id"]] = spec["prompt"]
    save(data)
    if spec["enabled"]:
        _register(spec)
    return spec


def update_custom(action_id, raw):
    aid = str(action_id or "").strip().lower()
    data = load()
    found = None
    for i, spec in enumerate(data["custom_actions"]):
        if spec.get("id") == aid:
            found = i
            break
    if found is None:
        raise ValueError(f"Пользовательское действие не найдено: {aid}")
    merged = dict(data["custom_actions"][found])
    merged.update(raw if isinstance(raw, dict) else {})
    merged["id"] = aid
    spec = clean_custom(merged)
    data["custom_actions"][found] = spec
    if spec["prompt"]:
        data["prompts"][aid] = spec["prompt"]
    else:
        data["prompts"].pop(aid, None)
    save(data)
    _unregister(aid)
    if spec["enabled"]:
        _register(spec)
    return spec


def delete_custom(action_id):
    aid = str(action_id or "").strip().lower()
    data = load()
    before = len(data["custom_actions"])
    data["custom_actions"] = [s for s in data["custom_actions"] if s.get("id") != aid]
    if len(data["custom_actions"]) == before:
        raise ValueError(f"Пользовательское действие не найдено: {aid}")
    data["prompts"].pop(aid, None)
    save(data)
    _unregister(aid)
    return aid


# --------------------------------------------------------------------------
# Обзор для админ-панели
# --------------------------------------------------------------------------
def overview():
    import action_center
    ensure_synced()
    data = load()
    prompts = data["prompts"]
    custom = [a for a in data["custom_actions"] if a.get("id")]
    custom_by_id = {a["id"]: a for a in custom}
    actions = []
    seen = set()
    for row in action_center.catalog():
        item = dict(row)
        item["prompt"] = prompts.get(row["id"], "")
        item["custom"] = row["id"] in _CUSTOM_IDS
        item["enabled"] = True
        stored = custom_by_id.get(row["id"])
        if stored:
            # Редактору нужны method/path: в Action они не хранятся.
            item["method"] = stored.get("method") or "GET"
            item["path"] = stored.get("path") or ""
        seen.add(row["id"])
        actions.append(item)

    # Выключенное своё API не регистрируется в action_center, поэтому в
    # catalog() его нет. Без этой ветки админка теряла такую запись: её нельзя
    # было ни увидеть, ни включить обратно, ни удалить.
    for spec in custom:
        aid = spec.get("id")
        if not aid or aid in seen:
            continue
        method = (spec.get("method") or "GET").upper()
        actions.append({
            "id": aid,
            "title": spec.get("title") or aid,
            "description": spec.get("description") or "",
            "category": spec.get("category") or "custom",
            "read_only": method == "GET",
            "params": spec.get("params") or {},
            "examples": spec.get("examples") or [],
            "prompt": prompts.get(aid, ""),
            "custom": True,
            "enabled": False,
            "method": method,
            "path": spec.get("path") or "",
        })
        seen.add(aid)

    actions.sort(key=lambda a: (a["category"], a["id"]))
    return {
        "actions": actions,
        "custom_actions": custom,
        "categories": sorted({a["category"] for a in actions}),
        "count": len(actions),
    }
