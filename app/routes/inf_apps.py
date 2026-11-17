"""
.inf-приложения (kind="app") — исполняемые файлы, которые открываются окном.

Формат .inf — это JSON с расширением .inf (тот же словарь, что extension.json).
Для kind="app" внутри лежат html/css/js и необязательный python. Ничего не
устанавливается в систему расширений: файл открывается, рисуется в iframe
окна WinBox, а Python (если есть) выполняется одноразовыми вызовами в
песочнице — по одному subprocess на действие пользователя, как у хуков
расширений. Долгоживущего процесса нет намеренно: зависшее .inf не должно
держать ресурсы сервера.

Развёртывание:
  * маленькое приложение (<= INLINE_MAX_BYTES) живёт в памяти, на диск не пишется;
  * большое и любое с Python распаковывается в data/inf_apps/<token>/ и
    отдаётся оттуда (иначе страницу пришлось бы каждый раз собирать заново).

token = sha256(содержимого)[:16]: стабилен между запусками, поэтому записанное
согласие на права переживает и перезапуск сервера, и переоткрытие файла.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
import swallow

logger = logging.getLogger("infinity")

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INF_CACHE_DIR = os.path.join(APP_DIR, "data", "inf_apps")
TRUST_FILE = os.path.join(INF_CACHE_DIR, "trust.json")

# Больше этого — раскладываем на диск. Раньше было 1 МБ; держим 4 МБ,
# потому что страница всё равно собирается строкой и уходит в ответ целиком.
INLINE_MAX_BYTES = 4 * 1024 * 1024
# Сколько распакованных приложений держим на диске.
CACHE_KEEP = 24
CACHE_MAX_AGE = 7 * 24 * 3600

# token -> {"dir": path|None, "info": {...}, "payload": {...}, "at": ts}
_MEMORY: Dict[str, Dict[str, Any]] = {}

router = APIRouter(tags=["INF Apps"])

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


# ─────────────────────────── права и доверие ───────────────────────────

def _load_trust() -> Dict[str, Any]:
    try:
        if os.path.isfile(TRUST_FILE):
            with open(TRUST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"inf_apps: cannot read trust file: {e}")
    return {}


def _save_trust(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(TRUST_FILE), exist_ok=True)
        with open(TRUST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"inf_apps: cannot write trust file: {e}")


def granted_permissions(token: str) -> List[str]:
    """Права, на которые пользователь уже согласился для этого .inf."""
    entry = _load_trust().get(token)
    if not isinstance(entry, dict):
        return []
    granted = entry.get("permissions")
    return [str(p) for p in granted] if isinstance(granted, list) else []


def remember_consent(token: str, permissions: List[str]) -> None:
    trust = _load_trust()
    trust[token] = {"permissions": [str(p) for p in permissions], "ts": time.time()}
    _save_trust(trust)


def revoke_consent(token: str) -> None:
    trust = _load_trust()
    if token in trust:
        trust.pop(token, None)
        _save_trust(trust)


def effective_permissions(payload: Dict[str, Any], token: str) -> List[str]:
    """Запрошенные .inf права, ограниченные тем, что реально разрешено.

    Если пользователь ещё не соглашался, остаются только базовые (storage,
    ui:modify) — окно покажет остальные и спросит.
    """
    from extension_system import INF_BASE_PERMISSIONS

    requested = payload.get("permissions")
    if not isinstance(requested, list):
        requested = ["storage"]
    requested = [str(p) for p in requested]

    trust = _load_trust().get(token)
    if not isinstance(trust, dict):
        return [p for p in requested if p in INF_BASE_PERMISSIONS]

    allowed = set(str(p) for p in (trust.get("permissions") or []))
    # Согласие всегда включает базовые — иначе приложение сломается на ровном месте.
    allowed.update(INF_BASE_PERMISSIONS)
    return [p for p in requested if p in allowed]


def pending_permissions(payload: Dict[str, Any], token: str) -> List[str]:
    """Расширенные права, по которым ещё нужно спросить пользователя."""
    from extension_system import INF_BASE_PERMISSIONS

    requested = payload.get("permissions")
    if not isinstance(requested, list):
        return []
    granted = set(effective_permissions(payload, token))
    return [str(p) for p in requested if str(p) not in granted
            and str(p) not in INF_BASE_PERMISSIONS]


# ─────────────────────────── развёртывание ────────────────────────────

def _token_for(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _prune_cache() -> None:
    """Оставить CACHE_KEEP последних приложений, старые вычистить."""
    try:
        if not os.path.isdir(INF_CACHE_DIR):
            return
        entries = []
        for name in os.listdir(INF_CACHE_DIR):
            full = os.path.join(INF_CACHE_DIR, name)
            if name == "trust.json" or not os.path.isdir(full):
                continue
            try:
                entries.append((os.path.getmtime(full), full))
            except OSError:
                swallow.note('inf_apps._prune_cache:L154')
                continue
        entries.sort(reverse=True)
        now = time.time()
        for idx, (mtime, full) in enumerate(entries):
            if idx >= CACHE_KEEP or now - mtime > CACHE_MAX_AGE:
                shutil.rmtree(full, ignore_errors=True)
    except Exception as e:
        logger.debug(f"inf_apps: cache prune failed: {e}")


def _deploy_to_disk(token: str, payload: Dict[str, Any]) -> Optional[str]:
    """Распаковать .inf в data/inf_apps/<token>/. None — если не вышло."""
    from extension_system import INF_FORMAT, INF_VERSION

    target = os.path.join(INF_CACHE_DIR, token)
    try:
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        os.makedirs(target, exist_ok=True)
        manifest = {
            "format": INF_FORMAT,
            "version": INF_VERSION,
            "kind": "app",
            "name": str(payload.get("name") or token),
            "icon": str(payload.get("icon") or "fa-box"),
            "description": str(payload.get("description") or ""),
            "permissions": [str(p) for p in (payload.get("permissions") or ["storage"])],
            "html": str(payload.get("html") or ""),
            "css": str(payload.get("css") or ""),
            "js": str(payload.get("js") or ""),
        }
        with open(os.path.join(target, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        py = payload.get("python")
        if isinstance(py, str) and py.strip():
            with open(os.path.join(target, "main.py"), "w", encoding="utf-8") as f:
                f.write(py)

        files = payload.get("files")
        if isinstance(files, dict):
            for rel, content in files.items():
                rel = str(rel).replace("\\", "/").lstrip("/")
                if not rel or rel.startswith("../") or "/../" in rel:
                    continue
                dest = os.path.abspath(os.path.join(target, rel))
                if not dest.startswith(os.path.abspath(target) + os.sep):
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(content if isinstance(content, str) else "")
        return target
    except Exception as e:
        logger.error(f"inf_apps: deploy to disk failed for {token}: {e}")
        shutil.rmtree(target, ignore_errors=True)
        return None


def load_app(raw: bytes, token: Optional[str] = None) -> Dict[str, Any]:
    """Разобрать .inf и подготовить к показу. Бросает ValueError."""
    from extension_system import parse_inf, inf_summary

    payload = parse_inf(raw)
    if payload.get("kind") != "app":
        raise ValueError("Это .inf-расширение (kind=extension), а не приложение")

    token = token or _token_for(raw)
    info = inf_summary(payload)
    has_python = bool(str(payload.get("python") or "").strip())

    cached = _MEMORY.get(token)
    if cached:
        return {"token": token, "dir": cached.get("dir"),
                "info": cached.get("info"), "payload": cached.get("payload")}

    # На диск кладём большое или то, у которого есть Python: для песочницы нужен
    # каталог с main.py, из памяти процесс не запустить.
    if len(raw) <= INLINE_MAX_BYTES and not has_python:
        _prune_cache()
        _MEMORY[token] = {"dir": None, "info": info, "payload": payload,
                          "at": time.time()}
        return {"token": token, "dir": None, "info": info, "payload": payload}

    target = _deploy_to_disk(token, payload)
    _prune_cache()
    if not target:
        # Не смогли — отдаём из памяти: лучше открыть в памяти, чем не открыть.
        _MEMORY[token] = {"dir": None, "info": info, "payload": payload,
                          "at": time.time()}
        return {"token": token, "dir": None, "info": info, "payload": payload}

    _MEMORY[token] = {"dir": target, "info": info, "payload": payload,
                      "at": time.time()}
    return {"token": token, "dir": target, "info": info, "payload": payload}


def _load_from_disk(token: str) -> Optional[Dict[str, Any]]:
    """Поднять приложение из кэша после перезапуска сервера."""
    if not _NAME_RE.match(token or ""):
        return None
    target = os.path.join(INF_CACHE_DIR, token)
    manifest_path = os.path.join(target, "manifest.json")
    if not os.path.isfile(manifest_path):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        swallow.note('inf_apps._load_from_disk:L263')
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "app":
        return None
    main_py = os.path.join(target, "main.py")
    if os.path.isfile(main_py):
        try:
            with open(main_py, "r", encoding="utf-8") as f:
                payload["python"] = f.read()
        except Exception:
            swallow.note('inf_apps._load_from_disk:L273')
            payload["python"] = ""
    else:
        payload["python"] = ""
    from extension_system import inf_summary
    return {"token": token, "dir": target, "info": inf_summary(payload),
            "payload": payload}


def resolve(token: str) -> Optional[Dict[str, Any]]:
    app = _MEMORY.get(token)
    if app:
        return app
    app = _load_from_disk(token)
    if app:
        _MEMORY[token] = {"dir": app["dir"], "info": app["info"],
                          "payload": app["payload"], "at": time.time()}
    return app


# ─────────────────────────── сборка страницы ──────────────────────────

def _esc(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _app_page(token: str, info: Dict[str, Any], payload: Dict[str, Any]) -> str:
    html = payload.get("html") or ""
    css = payload.get("css") or ""
    js = payload.get("js") or ""
    title = _esc(info.get("name") or "INF")
    has_python = bool(str(payload.get("python") or "").strip())
    perms = effective_permissions(payload, token)

    # Тело .inf необязательно: без него показываем заготовку, чтобы окно не было
    # пустым белым прямоугольником без объяснений.
    body = html.strip() or (
        '<div style="font-family:Inter,sans-serif;padding:32px;color:#e1e8f0">'
        '<h2 style="margin:0 0 8px">%s</h2>'
        '<p style="color:#8899aa">%s</p>'
        '<p style="color:#8899aa">В файле .inf нет поля <code>html</code>.</p>'
        '</div>'
    ) % (_esc(info.get("name") or ""), _esc(info.get("description") or ""))

    bridge = ""
    if has_python:
        bridge = (
            "\n<script>\n"
            "/* Мост в Python-часть .inf. Один вызов — один процесс песочницы:\n"
            "   состояние между вызовами не живёт, ровно как у хуков расширений. */\n"
            "window.INF = {\n"
            "  token: %s,\n"
            "  permissions: %s,\n"
            "  async call(fn, payload) {\n"
            "    const r = await fetch('/inf-app/' + this.token + '/call/' + encodeURIComponent(fn), {\n"
            "      method: 'POST',\n"
            "      headers: {'Content-Type': 'application/json'},\n"
            "      body: JSON.stringify(payload || {})\n"
            "    });\n"
            "    const d = await r.json().catch(() => ({error: 'Нет ответа сервера'}));\n"
            "    if (d && d.error) throw new Error(d.error);\n"
            "    return d ? d.result : null;\n"
            "  }\n"
            "};\n"
            "</script>\n"
        ) % (json.dumps(token), json.dumps(perms, ensure_ascii=False))

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="/static/css/font-awesome.min.css">
<style>
* {{ margin:0; padding:0; box-sizing:border-box }}
body {{ font-family:Inter,system-ui,sans-serif; background:var(--bg,#0f1419); color:var(--text,#e1e8f0); min-height:100vh }}
:root {{
  --bg:#0f1419; --bg2:#151b23; --bg3:#1c242f; --text:#e1e8f0; --text2:#8899aa;
  --accent:#6366f1; --border:#2a3548; --radius:12px;
}}
{css}
</style>
</head>
<body>
{body}
{bridge}<script>
{js}
</script>
</body>
</html>"""


# ─────────────────────────── роуты ────────────────────────────────────

@router.get("/inf-app/{token}")
def inf_app_page(token: str):
    """Страница .inf-приложения (её и грузит iframe окна)."""
    app = resolve(token)
    if not app:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:24px;color:#e1e8f0'>"
            "<h2>Приложение не найдено</h2>"
            "<p style='color:#8899aa'>Откройте .inf-файл в файловом менеджере заново.</p>"
            "</body></html>", status_code=404)
    return HTMLResponse(_app_page(token, app["info"], app["payload"]))


@router.get("/inf-app/{token}/asset/{fname}")
def inf_app_asset(token: str, fname: str):
    """Статика из .inf (поле files) — только из каталога приложения."""
    app = resolve(token)
    if not app or not app.get("dir"):
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    if not _NAME_RE.match(fname):
        return JSONResponse({"ok": False, "error": "Bad name"}, status_code=400)
    base = os.path.abspath(app["dir"])
    target = os.path.abspath(os.path.join(base, fname))
    if not target.startswith(base + os.sep) or not os.path.isfile(target):
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    try:
        with open(target, "rb") as f:
            data = f.read()
    except Exception:
        return JSONResponse({"ok": False, "error": "Read error"}, status_code=500)
    ext = os.path.splitext(target)[1].lower()
    mime = {".css": "text/css", ".js": "application/javascript",
            ".json": "application/json", ".svg": "image/svg+xml",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "text/plain; charset=utf-8")
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "no-cache"})


@router.post("/inf-app/{token}/call/{fn}")
async def inf_app_call(token: str, fn: str, request: Request):
    """Вызов функции из Python-части .inf-приложения — всегда в песочнице.

    Одноразовый subprocess: отработал и умер. Так зависшее приложение не держит
    процессы сервера, а модель исполнения совпадает с хуками расширений.
    """
    app = resolve(token)
    if not app or not app.get("dir"):
        return JSONResponse({"ok": False, "error": "Приложение не найдено"}, status_code=404)
    if not str(app["payload"].get("python") or "").strip():
        return JSONResponse({"ok": False, "error": "У приложения нет Python-части"},
                            status_code=400)
    try:
        body = await request.json()
    except Exception:
        swallow.note('inf_apps.inf_app_call:L423')
        body = {}
    if not isinstance(body, dict):
        body = {"data": body}

    perms = effective_permissions(app["payload"], token)
    try:
        from extension_sandbox import SandboxProcess
        sb = SandboxProcess(f"inf_{token}", app["dir"], timeout=30)
        # Права .inf-приложения действуют только в рамках выданного набора:
        # чужие права расширений сюда не подмешиваются.
        sb.perms = list(perms)
        result = await asyncio.get_running_loop().run_in_executor(
            None, sb.execute_function, fn, body)
    except Exception as e:
        logger.error(f"inf_app_call error for {token}: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    if isinstance(result, dict) and result.get("error"):
        return JSONResponse({"ok": False, "error": result["error"]}, status_code=400)
    # execute_function возвращает конверт самого subprocess-протокола
    # ({"result": ...}). Здесь мы уже формируем свой ответ — разворачиваем
    # один раз, иначе JS получил бы {result:{result:...}}.
    if isinstance(result, dict) and set(result.keys()) == {"result"}:
        result = result["result"]
    return JSONResponse({"ok": True, "result": result})
