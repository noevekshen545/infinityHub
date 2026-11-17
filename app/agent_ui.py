#!/usr/bin/env python3
"""
agent_ui — управляемый UI-слой агента: слоты, тема и подтверждения.

Зачем: агент может менять СВОЙ интерфейс (добавить кнопку в сайдбар, повесить
панель/оверлей, сменить фон и акцент), но каждое изменение проходит через явное
подтверждение пользователя и полностью обратимо.

Два уровня вклада в UI. Файлы лежат рядом с плагином
(<plugin_dir>/ui.json, ui.css, ui.js):

    ui.json — ДЕКЛАРАТИВНЫЙ уровень (безопасный, JS со страницы не исполняется):
        {
          "slots": [
            {"slot": "sidebar.menu", "id": "focus", "label": "Фокус",
             "icon": "fa-bullseye", "order": 50, "title": "Подсказка",
             "action": {"type": "toast", "text": "Включено"}}
          ],
          "theme": {"--bg": {"light": "#ffffff", "dark": "#0a0b0e"}},
          "css": "/* необязательный CSS без скриптов */"
        }
      Разрешённые действия (SAFE_ACTIONS): toast, open_url, open_panel,
      send_message, copy_text, reset_ui, run_tool.

    ui.js / ui.css — СЫРОЙ уровень: произвольный JS на странице. Исполняется
      ТОЛЬКО после подтверждения пользователя, с явным предупреждением, и обязан
      регистрироваться через window.InfinityUI, чтобы откат был гарантирован.

Подтверждения (состояние в <plugin_dir>/ui_approval.json):
    approved_hash      — хеш содержимого, разрешённого пользователем;
    auto_approve       — «подтвердить все дальнейшие изменения этого плагина»;
    rejected_hash      — версия, которую пользователь отклонил.
  * approve(all=False) — разрешить только текущую версию;
  * approve(all=True)  — разрешить текущую и все будущие (без вопроса);
  * reject()           — текущая версия не применяется, ранее разрешённая
                         продолжает работать;
  * revoke()           — снять разрешение: UI плагина снова требует подтверждения.
Аудит всех решений: ~/.infinity/asio_agent_v4/ui_approvals.jsonl

Точка доверия: подтверждённый ui.js имеет полный доступ к странице — это НЕ
песочница. Гарантия = «без явного разрешения ничего не исполняется» + полный
откат всего, что зарегистрировано через InfinityUI + мгновенный отзыв.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional
import swallow

logger = logging.getLogger("infinity")

AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent_v4")
JOURNAL = os.path.join(AGENT_BASE, "ui_approvals.jsonl")
PKG_ROOT = os.path.dirname(os.path.abspath(__file__))

# Слоты страницы агента, куда разрешено вносить UI (см. page_agent.html).
UI_SLOTS = (
    "sidebar.header",     # иконки в шапке сайдбара
    "sidebar.menu",       # пункты меню (после «Новый чат»)
    "sidebar.menu.end",   # конец меню (после «Граф знаний»)
    "sidebar.footer",     # строка статуса/движка внизу сайдбара
    "composer.actions",   # кнопки рядом с полем ввода
    "overlay",            # плавающий слой поверх страницы
    "root",               # конец body
)

# Переменные темы, которые плагину разрешено менять (см. :root в page_agent.html).
THEME_VARS = (
    "--bg", "--bg-soft", "--panel", "--card",
    "--border", "--border2",
    "--text", "--text2", "--text3",
    "--hover", "--hover-soft",
    "--accent", "--accent-contrast",
    "--brand", "--brand-2", "--brand-soft", "--brand-border",
    "--user-bg", "--user-fg",
)

# Действия, доступные декларативному ui.json (без исполнения JS плагина).
SAFE_ACTIONS = ("toast", "open_url", "open_panel", "send_message",
                "copy_text", "reset_ui", "run_tool")

# Пометки риска для окна подтверждения (не блокируют, а предупреждают).
RISK_PATTERNS = (
    (r"\beval\s*\(", "eval() — исполнение строки как кода"),
    (r"new\s+Function\s*\(", "new Function() — динамическое исполнение кода"),
    (r"document\.cookie", "доступ к cookies страницы"),
    (r"localStorage|sessionStorage", "доступ к хранилищу браузера"),
    (r"\bfetch\s*\(|XMLHttpRequest|WebSocket|EventSource", "сетевые запросы со страницы"),
    (r"import\s*\(|\brequire\s*\(", "динамический импорт модулей"),
    (r"innerHTML\s*=|insertAdjacentHTML", "вставка HTML (риск XSS)"),
    (r"document\.write", "document.write"),
    (r"addEventListener\s*\(\s*['\"]key", "перехват клавиатуры"),
    (r"document\.documentElement\.style", "прямая правка :root вместо InfinityUI.theme"),
)

MAX_CSS = 100_000
MAX_JS = 200_000
MAX_DECL = 60_000

_lock = threading.RLock()
_VERBOSE = bool(os.environ.get("AGENT_UI_VERBOSE"))


def _journal(action: str, **meta: Any) -> None:
    """Пишет решение по UI в журнал аудита (не роняет вызывающего)."""
    try:
        os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
        rec = {"ts": time.time(), "action": action, **meta}
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        swallow.note('agent_ui._journal:L115')
        pass


def _safe(name: str) -> str:
    """Имя папки плагина: только a-z0-9_- (как в agent_plugins)."""
    return re.sub(r"[^a-z0-9_\-]", "_", (name or "").lower())


def _read(path: str, limit: int = 0) -> str:
    """Читает текстовый файл; при limit — обрезает (для тяжёлых вкладов)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        return text[:limit] if limit else text
    except Exception:
        swallow.note('agent_ui._read:L131')
        return ""


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _rm(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except Exception:
        swallow.note('agent_ui._rm:L146')
        return False


# ============================================================
# Обнаружение плагинов, у которых есть UI-часть
# ============================================================
def plugin_dirs() -> List[Dict[str, Any]]:
    """Каталоги плагинов, откуда берётся UI: [{dir, shipped}].

    Порядок как в движке: ядровые, профильные, пользовательские. Поздний
    каталог перекрывает ранний при совпадении id (как в asio_engine).
    """
    out: List[Dict[str, Any]] = []

    def add(path: str, shipped: bool) -> None:
        if not path:
            return
        ap = os.path.abspath(path)
        if any(os.path.abspath(o["dir"]) == ap for o in out):
            return
        out.append({"dir": ap, "shipped": shipped})

    try:
        import agent_plugins as _ap
        add(getattr(_ap, "PLUGINS_DIR", ""), False)
    except Exception as e:
        if _VERBOSE:
            logger.debug("[agent_ui] agent_plugins dirs unavailable: %s", e)
    try:
        import asio_engine
        for d in asio_engine.plugins_dir():
            core = os.path.basename(os.path.abspath(d)) == "core_plugins"
            add(d, core)
    except Exception as e:
        if _VERBOSE:
            logger.debug("[agent_ui] asio_engine dirs unavailable: %s", e)
    add(os.path.join(PKG_ROOT, "core_plugins"), True)
    return out


def find(name: str) -> Optional[Dict[str, Any]]:
    """Находит папку плагина по имени. Поздний каталог перекрывает ранний."""
    want = _safe(name)
    if not want:
        return None
    found: Optional[Dict[str, Any]] = None
    for entry in plugin_dirs():
        d = entry["dir"]
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except Exception:
            swallow.note('agent_ui.find:L200')
            continue
        for item in names:
            if _safe(item) != want:
                continue
            full = os.path.join(d, item)
            if os.path.isdir(full):
                found = {"name": want, "dir": full, "shipped": entry["shipped"]}
        # Поздний каталог перекрывает ранний — продолжаем, чтобы перезаписать.
    return found


def _manifest(plugin: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with open(os.path.join(plugin["dir"], "plugin.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        swallow.note('agent_ui._manifest:L218')
        return {}


def _ui_path(plugin: Dict[str, Any], filename: str) -> str:
    return os.path.join(plugin["dir"], filename)


def scan_risks(js: str) -> List[str]:
    """Перечисляет рискованные конструкции сырого ui.js для окна подтверждения."""
    out: List[str] = []
    for pattern, why in RISK_PATTERNS:
        try:
            if re.search(pattern, js):
                out.append(why)
        except Exception:
            swallow.note('agent_ui.scan_risks:L234')
            continue
    return out


def _norm_action(raw: Any) -> Optional[Dict[str, Any]]:
    """Приводит действие из ui.json к безопасному виду (или None)."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type", "")).strip()
    if kind not in SAFE_ACTIONS:
        return None
    action: Dict[str, Any] = {"type": kind}
    if kind == "toast":
        action["text"] = str(raw.get("text", ""))[:300]
        action["level"] = str(raw.get("level", "success"))[:20]
    elif kind == "open_url":
        action["url"] = str(raw.get("url", ""))[:2000]
    elif kind == "open_panel":
        action["panel"] = str(raw.get("panel", ""))[:60]
    elif kind == "send_message":
        action["text"] = str(raw.get("text", ""))[:2000]
    elif kind == "copy_text":
        action["text"] = str(raw.get("text", ""))[:4000]
    elif kind == "run_tool":
        action["tool"] = str(raw.get("tool", ""))[:80]
        action["args"] = str(raw.get("args", ""))[:2000]
    return action


def normalize_decl(raw: Any) -> Dict[str, Any]:
    """Проверяет декларацию ui.json. Возвращает {slots, theme, css, errors}."""
    errors: List[str] = []
    decl = raw if isinstance(raw, dict) else {}
    slots: List[Dict[str, Any]] = []
    for row in decl.get("slots") or []:
        if not isinstance(row, dict):
            errors.append("slots: строка должна быть объектом")
            continue
        slot = str(row.get("slot", "")).strip()
        if slot not in UI_SLOTS:
            errors.append("slot '" + slot + "' неизвестен; допустимы: " + ", ".join(UI_SLOTS))
            continue
        entry: Dict[str, Any] = {"slot": slot}
        ident = str(row.get("id", "")).strip() or ("ui-" + str(len(slots)))
        entry["id"] = re.sub(r"[^a-zA-Z0-9_\-]", "-", ident)[:60]
        entry["label"] = str(row.get("label", ""))[:120]
        entry["icon"] = str(row.get("icon", ""))[:60]
        entry["title"] = str(row.get("title", ""))[:200]
        try:
            entry["order"] = int(row.get("order", 100))
        except Exception:
            swallow.note('agent_ui.normalize_decl:L286')
            entry["order"] = 100
        action = _norm_action(row.get("action"))
        if action is None and row.get("action") is not None:
            errors.append("slots[" + entry["id"] + "]: action.type должен быть одним из "
                          + ", ".join(SAFE_ACTIONS))
        entry["action"] = action
        slots.append(entry)
    theme: Dict[str, Any] = {}
    for key, value in (decl.get("theme") or {}).items():
        if key not in THEME_VARS:
            errors.append("theme: переменная '" + str(key) + "' не разрешена")
            continue
        if isinstance(value, dict):
            theme[key] = {"light": str(value.get("light", ""))[:200],
                          "dark": str(value.get("dark", ""))[:200]}
        else:
            theme[key] = {"light": str(value)[:200], "dark": str(value)[:200]}
    return {"slots": slots, "theme": theme,
            "css": str(decl.get("css", ""))[:MAX_CSS], "errors": errors}


def bundle(plugin: Dict[str, Any]) -> Dict[str, Any]:
    """Собирает вклад плагина в UI: декларация + сырые css/js + хеш содержимого."""
    css = _read(_ui_path(plugin, "ui.css"), MAX_CSS)
    js = _read(_ui_path(plugin, "ui.js"), MAX_JS)
    decl_raw = _read(_ui_path(plugin, "ui.json"), MAX_DECL)
    decl = normalize_decl(json.loads(decl_raw)) if decl_raw.strip() else normalize_decl({})
    if decl["css"] and not css:
        css = decl["css"]
    payload = "\0".join((css, js, decl_raw))
    files = [n for n, t in (("ui.json", decl_raw), ("ui.css", css), ("ui.js", js)) if t]
    return {
        "name": plugin["name"],
        "dir": plugin["dir"],
        "shipped": bool(plugin.get("shipped")),
        "manifest": _manifest(plugin),
        "css": css,
        "js": js,
        "slots": decl["slots"],
        "theme": decl["theme"],
        "decl_errors": decl["errors"],
        "risks": scan_risks(js),
        "raw": bool(js),
        "files": files,
        "has_ui": bool(files),
        "hash": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
    }


# ============================================================
# Реестр подтверждений UI
# ============================================================
SNAPSHOT_DIR = ".ui_approved"
STATE_FILE = "ui_approval.json"


class UIRegistry:
    """Подтверждения UI по плагинам: что разрешено, что ждёт, что отклонено.

    Утверждённая версия копируется в <plugin_dir>/.ui_approved/, поэтому новая
    (ещё не подтверждённая) правка не ломает уже работающий интерфейс: страница
    продолжает получать снапшот, пока пользователь не решит судьбу изменения.
    """

    def __init__(self, base: Optional[str] = None):
        self.base = base or AGENT_BASE
        self.journal_path = JOURNAL if base is None else os.path.join(base, "ui_approvals.jsonl")

    # --- журнал/состояние ---
    def _log(self, action: str, **meta: Any) -> None:
        try:
            os.makedirs(os.path.dirname(self.journal_path), exist_ok=True)
            rec = {"ts": time.time(), "action": action, **meta}
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            swallow.note('agent_ui._log:L363')
            pass

    def _state_path(self, plugin: Dict[str, Any]) -> str:
        return os.path.join(plugin["dir"], STATE_FILE)

    def read_state(self, plugin: Dict[str, Any]) -> Dict[str, Any]:
        try:
            with open(self._state_path(plugin), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            swallow.note('agent_ui.read_state:L375')
            return {}

    def write_state(self, plugin: Dict[str, Any], data: Dict[str, Any]) -> None:
        try:
            with open(self._state_path(plugin), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[agent_ui] не удалось сохранить состояние %s: %s", plugin["name"], e)

    def _snapshot_path(self, plugin: Dict[str, Any], filename: str) -> str:
        return os.path.join(plugin["dir"], SNAPSHOT_DIR, filename)

    def snapshot_bundle(self, plugin: Dict[str, Any]) -> Dict[str, Any]:
        """Читает утверждённый снапшот как псевдо-плагин (для выдачи странице)."""
        snap = {"name": plugin["name"],
                "dir": os.path.join(plugin["dir"], SNAPSHOT_DIR),
                "shipped": plugin.get("shipped", False)}
        if not os.path.isdir(snap["dir"]):
            return {"name": plugin["name"], "has_ui": False, "css": "", "js": "",
                    "slots": [], "theme": {}, "risks": [], "raw": False,
                    "decl_errors": [], "files": [], "hash": ""}
        return bundle(snap)

    def _write_snapshot(self, plugin: Dict[str, Any], b: Dict[str, Any]) -> None:
        """Копирует текущие файлы плагина в снапшот утверждённой версии."""
        snap_dir = os.path.join(plugin["dir"], SNAPSHOT_DIR)
        os.makedirs(snap_dir, exist_ok=True)
        for filename in ("ui.json", "ui.css", "ui.js"):
            src = _ui_path(plugin, filename)
            dst = os.path.join(snap_dir, filename)
            if os.path.exists(src):
                _write(dst, _read(src))
            elif os.path.exists(dst):
                _rm(dst)

    # --- состояние одного плагина ---
    def _status(self, plugin: Dict[str, Any], b: Dict[str, Any],
                st: Dict[str, Any]) -> str:
        """none | pending | approved | rejected (core — всегда approved)."""
        if not b["has_ui"]:
            return "none"
        if b["shipped"]:
            return "approved"
        if st.get("approved_hash") == b["hash"]:
            return "approved"
        if st.get("auto_approve") and st.get("approved_hash") != b["hash"]:
            # «Подтвердить все дальнейшие изменения» — новое содержимое
            # утверждается молча, но снапшот обновляем, чтобы выдача совпадала.
            self._write_snapshot(plugin, b)
            st.update({"approved_hash": b["hash"], "approved_at": time.time()})
            self.write_state(plugin, st)
            self._log("auto-approve", plugin=plugin["name"], hash=b["hash"], actor="auto")
            return "approved"
        if st.get("rejected_hash") == b["hash"]:
            return "rejected"
        return "pending"

    def describe(self, name: str) -> Dict[str, Any]:
        """Состояние UI одного плагина (для инструментов агента и REST)."""
        plugin = find(name)
        if plugin is None:
            return {"ok": False, "error": f"плагин '{name}' не найден", "name": _safe(name)}
        b = bundle(plugin)
        st = self.read_state(plugin)
        status = self._status(plugin, b, st)
        return {
            "ok": True,
            "name": b["name"],
            "dir": b["dir"],
            "shipped": b["shipped"],
            "enabled": b["manifest"].get("enabled", True) is not False,
            "status": status,
            "hash": b["hash"],
            "has_ui": b["has_ui"],
            "raw": b["raw"],
            "files": b["files"],
            "slots": [row["slot"] for row in b["slots"]],
            "declared_slots": b["slots"],
            "theme": b["theme"],
            "theme_vars": sorted(b["theme"].keys()),
            "risks": b["risks"],
            "decl_errors": b["decl_errors"],
            "approved_hash": st.get("approved_hash"),
            "approved_at": st.get("approved_at"),
            "auto_approve": bool(st.get("auto_approve")),
            "rejected_hash": st.get("rejected_hash"),
            "rejected_at": st.get("rejected_at"),
            "reject_reason": st.get("reject_reason", ""),
        }

    # --- перечисление ---
    def _all_plugins(self) -> List[Dict[str, Any]]:
        """Все папки-плагины из известных каталогов (поздний каталог wins)."""
        seen: Dict[str, Dict[str, Any]] = {}
        for entry in plugin_dirs():
            d = entry["dir"]
            if not os.path.isdir(d):
                continue
            try:
                items = os.listdir(d)
            except Exception:
                swallow.note('agent_ui._all_plugins:L477')
                continue
            for item in sorted(items):
                if item.startswith("."):
                    continue
                full = os.path.join(d, item)
                if not os.path.isdir(full):
                    continue
                has_manifest = os.path.exists(os.path.join(full, "plugin.json"))
                has_ui = any(os.path.exists(os.path.join(full, f))
                             for f in ("ui.json", "ui.css", "ui.js"))
                if not (has_manifest or has_ui):
                    continue
                seen[_safe(item)] = {"name": _safe(item), "dir": full,
                                     "shipped": entry["shipped"]}
        return list(seen.values())

    def pending(self) -> List[Dict[str, Any]]:
        """Изменения UI, ждущие решения пользователя (новейшие первыми)."""
        out: List[Dict[str, Any]] = []
        for plugin in self._all_plugins():
            b = bundle(plugin)
            if not b["has_ui"] or plugin.get("shipped"):
                continue
            if b["manifest"].get("enabled", True) is False:
                continue
            st = self.read_state(plugin)
            if self._status(plugin, b, st) != "pending":
                continue
            created = 0.0
            for filename in b["files"]:
                try:
                    created = max(created, os.path.getmtime(_ui_path(plugin, filename)))
                except Exception:
                    swallow.note('agent_ui.pending:L511')
                    pass
            out.append({
                "name": b["name"],
                "description": str(b["manifest"].get("description", ""))[:300],
                "hash": b["hash"],
                "prev_hash": st.get("approved_hash"),
                "has_prev": bool(st.get("approved_hash")),
                "mode": "raw" if b["raw"] else "declarative",
                "files": b["files"],
                "slots": b["slots"],
                "slots_keys": [row["slot"] for row in b["slots"]],
                "theme": b["theme"],
                "theme_vars": sorted(b["theme"].keys()),
                "raw": b["raw"],
                "risks": b["risks"],
                "decl_errors": b["decl_errors"],
                "css_bytes": len(b["css"].encode("utf-8")),
                "js_bytes": len(b["js"].encode("utf-8")),
                "enabled": b["manifest"].get("enabled", True) is not False,
                "created_at": created,
            })
        out.sort(key=lambda row: row["created_at"], reverse=True)
        return out

    def registry(self) -> List[Dict[str, Any]]:
        """Вклады, РАЗРЕШЁННЫЕ к исполнению на странице (только подтверждённые).

        Источник для плагинов — снапшот утверждённой версии, поэтому ожидающая
        решения правка не ломает работающий интерфейс. Снапшот принимается лишь
        тогда, когда его хеш совпадает с `approved_hash` (защита от подмены).
        """
        out: List[Dict[str, Any]] = []
        for plugin in self._all_plugins():
            b = bundle(plugin)
            st = self.read_state(plugin)
            if b["manifest"].get("enabled", True) is False:
                continue
            if plugin.get("shipped"):
                if not b["has_ui"]:
                    continue
                served = b
            else:
                approved = st.get("approved_hash")
                served = self.snapshot_bundle(plugin)
                if not served.get("has_ui") or not approved or served["hash"] != approved:
                    continue
            out.append({
                "name": b["name"],
                "hash": served["hash"] or b["hash"],
                "status": "approved",
                "origin": "core" if plugin.get("shipped") else "plugin",
                "description": str(b["manifest"].get("description", ""))[:300],
                "raw": bool(served["raw"]),
                "slots": served["slots"],
                "theme": served["theme"],
                "css": served["css"],
                "js": served["js"],
                "risks": served["risks"],
                "decl_errors": served["decl_errors"],
                "auto_approve": bool(st.get("auto_approve")),
            })
        out.sort(key=lambda row: row["name"])
        return out

    # --- решения пользователя ---
    def approve(self, name: str, all_future: bool = False,
                actor: str = "user") -> Dict[str, Any]:
        """Разрешить текущую версию; all_future — и все дальнейшие."""
        plugin = find(name)
        if plugin is None:
            return {"ok": False, "error": f"плагин '{name}' не найден"}
        b = bundle(plugin)
        if not b["has_ui"]:
            return {"ok": False, "error": f"у плагина '{plugin['name']}' нет UI-части"}
        st = self.read_state(plugin)
        self._write_snapshot(plugin, b)
        for key in ("rejected_hash", "rejected_at", "reject_reason"):
            st.pop(key, None)
        st["approved_hash"] = b["hash"]
        st["approved_at"] = time.time()
        st["approved_files"] = b["files"]
        if all_future:
            st["auto_approve"] = True
        self.write_state(plugin, st)
        self._log("approve", plugin=b["name"], hash=b["hash"],
                  all_future=bool(all_future), actor=actor)
        return self.describe(plugin["name"])

    def reject(self, name: str, reason: str = "", actor: str = "user") -> Dict[str, Any]:
        """Отклонить текущую версию. Ранее утверждённая (снапшот) продолжает работать."""
        plugin = find(name)
        if plugin is None:
            return {"ok": False, "error": f"плагин '{name}' не найден"}
        b = bundle(plugin)
        st = self.read_state(plugin)
        if b["has_ui"]:
            st["rejected_hash"] = b["hash"]
            st["rejected_at"] = time.time()
            st["reject_reason"] = str(reason)[:300]
        st["auto_approve"] = False
        self.write_state(plugin, st)
        self._log("reject", plugin=b["name"], hash=b["hash"],
                  reason=str(reason)[:200], actor=actor)
        return self.describe(plugin["name"])

    def revoke(self, name: str, remove_files: bool = False,
               actor: str = "user") -> Dict[str, Any]:
        """Снять разрешение (и, по желанию, удалить UI-часть плагина)."""
        plugin = find(name)
        if plugin is None:
            return {"ok": False, "error": f"плагин '{name}' не найден"}
        snap_dir = os.path.join(plugin["dir"], SNAPSHOT_DIR)
        for filename in ("ui.json", "ui.css", "ui.js"):
            _rm(os.path.join(snap_dir, filename))
        try:
            os.rmdir(snap_dir)
        except Exception:
            swallow.note('agent_ui.revoke:L629')
            pass
        st = self.read_state(plugin)
        for key in ("approved_hash", "approved_at", "approved_files"):
            st.pop(key, None)
        st["auto_approve"] = False
        st["revoked_at"] = time.time()
        self.write_state(plugin, st)
        if remove_files:
            for filename in ("ui.json", "ui.css", "ui.js"):
                _rm(_ui_path(plugin, filename))
            self._log("ui-remove", plugin=plugin["name"], actor=actor)
        self._log("revoke", plugin=plugin["name"], remove_files=bool(remove_files),
                  actor=actor)
        return self.describe(plugin["name"])

    def forget(self, name: str) -> None:
        """Забывает состояние подтверждений (при удалении плагина)."""
        plugin = find(name)
        if plugin is None:
            return
        _rm(self._state_path(plugin))
        snap_dir = os.path.join(plugin["dir"], SNAPSHOT_DIR)
        for filename in ("ui.json", "ui.css", "ui.js"):
            _rm(os.path.join(snap_dir, filename))
        self._log("forget", plugin=plugin["name"])


# ============================================================
# Модульный фасад
# ============================================================
_registry: Optional[UIRegistry] = None


def get_registry() -> UIRegistry:
    """Единственный реестр на процесс (потокобезопасно)."""
    global _registry
    with _lock:
        if _registry is None:
            _registry = UIRegistry()
        return _registry


def _ensure_plugin(name: str, description: str = "") -> Dict[str, Any]:
    """Находит плагин или создаёт папку UI-only плагина в каталоге агента."""
    plugin = find(name)
    if plugin is not None:
        return plugin
    safe = _safe(name)
    if not safe:
        raise ValueError("пустое имя плагина")
    try:
        import agent_plugins as _ap
        base_dir = _ap.PLUGINS_DIR
    except Exception:
        swallow.note('agent_ui._ensure_plugin:L684')
        base_dir = os.path.join(AGENT_BASE, "plugins")
    plugin_dir = os.path.join(base_dir, safe)
    os.makedirs(plugin_dir, exist_ok=True)
    manifest_path = os.path.join(plugin_dir, "plugin.json")
    if not os.path.exists(manifest_path):
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({
                "name": safe,
                "description": description or f"UI-плагин {safe}",
                "version": "0.1.0",
                "enabled": True,
                "ui_only": True,
                "tools": [],
            }, f, ensure_ascii=False, indent=2)
    return {"name": safe, "dir": plugin_dir, "shipped": False}


def publish(name: str, description: str = "", ui_json: Any = None,
            css: Optional[str] = None, js: Optional[str] = None,
            actor: str = "agent") -> Dict[str, Any]:
    """Записать UI-часть плагина и поставить её в очередь на подтверждение.

    `ui_json` — декларация (dict или строка JSON), `css`/`js` — сырые вклады.
    Пустая строка удаляет соответствующий файл. Код НЕ исполняется, пока
    пользователь не разрешит: это только запись файлов и заявка на подтверждение.
    """
    try:
        plugin = _ensure_plugin(name, description)
    except Exception as e:
        swallow.note('agent_ui.publish:L714', e)
        return {"ok": False, "error": str(e)}
    if description:
        manifest_path = os.path.join(plugin["dir"], "plugin.json")
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
            if isinstance(manifest, dict):
                manifest["description"] = description or manifest.get("description", "")
                with open(manifest_path, "w", encoding="utf-8") as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)
        except Exception:
            swallow.note('agent_ui.publish:L726')
            pass
    decl_text = ""
    if isinstance(ui_json, (dict, list)):
        decl_text = json.dumps(ui_json, ensure_ascii=False, indent=2)
    elif isinstance(ui_json, str) and ui_json.strip():
        try:
            parsed = json.loads(ui_json)
        except Exception as e:
            swallow.note('agent_ui.publish:L735', e)
            return {"ok": False, "error": f"ui_json не является валидным JSON: {e}"}
        decl_text = json.dumps(parsed, ensure_ascii=False, indent=2)
    # None = «поле не указано, не трогать»; пустая строка = «удалить этот вклад».
    writes: List[tuple] = []
    if ui_json is not None:
        writes.append(("ui.json", decl_text, MAX_DECL))
    if css is not None:
        writes.append(("ui.css", css, MAX_CSS))
    if js is not None:
        writes.append(("ui.js", js, MAX_JS))
    for filename, text, limit in writes:
        target = os.path.join(plugin["dir"], filename)
        if str(text).strip():
            _write(target, str(text)[:limit])
        else:
            _rm(target)
    reg = get_registry()
    _log_publish = getattr(reg, "_log")
    _log_publish("publish", plugin=plugin["name"], actor=actor,
                 has_decl=bool(decl_text), has_css=bool(css), has_js=bool(js))
    state = reg.describe(plugin["name"])
    if not state.get("ok"):
        return state
    state["pending"] = state.get("status") == "pending"
    return state


def approved_file(name: str, kind: str) -> Optional[str]:
    """Содержимое, которое РАЗРЕШЕНО отдавать странице (иначе None).

    Источник — снапшот утверждённой версии, а не рабочие файлы плагина, чтобы
    неутверждённая правка никогда не попала в браузер.
    """
    reg = get_registry()
    plugin = find(name)
    if plugin is None:
        return None
    if not plugin.get("shipped"):
        state = reg.read_state(plugin)
        approved = state.get("approved_hash")
        served = reg.snapshot_bundle(plugin)
        if not approved or not served.get("has_ui") or served["hash"] != approved:
            return None
    else:
        served = bundle(plugin)
        if not served.get("has_ui"):
            return None
    if kind == "css":
        return served.get("css") or None
    if kind == "js":
        return served.get("js") or None
    if kind == "json":
        return json.dumps({"slots": served.get("slots", []),
                           "theme": served.get("theme", {}),
                           "name": served.get("name")}, ensure_ascii=False)
    return None


# Модульные обёртки над единственным реестром (для REST и инструментов).
def describe(name: str) -> Dict[str, Any]:
    """Состояние UI плагина (см. UIRegistry.describe)."""
    return get_registry().describe(name)


def pending() -> List[Dict[str, Any]]:
    """Изменения UI, ждущие решения пользователя."""
    return get_registry().pending()


def registry() -> List[Dict[str, Any]]:
    """Вклады, разрешённые к исполнению на странице."""
    return get_registry().registry()


def approve(name: str, all_future: bool = False, actor: str = "user") -> Dict[str, Any]:
    """Разрешить текущую версию UI (all_future — и все дальнейшие)."""
    return get_registry().approve(name, all_future=all_future, actor=actor)


def reject(name: str, reason: str = "", actor: str = "user") -> Dict[str, Any]:
    """Отклонить текущую версию UI."""
    return get_registry().reject(name, reason=reason, actor=actor)


def revoke(name: str, remove_files: bool = False, actor: str = "user") -> Dict[str, Any]:
    """Снять разрешение на UI (remove_files — удалить UI-часть)."""
    return get_registry().revoke(name, remove_files=remove_files, actor=actor)


def service_provider() -> Dict[str, Any]:
    """Провайдер для ctx.register('ctx.ui', ...) в ядре Cordis."""
    reg = get_registry()
    return {
        "slots": list(UI_SLOTS),
        "theme_vars": list(THEME_VARS),
        "actions": list(SAFE_ACTIONS),
        "describe": reg.describe,
        "pending": reg.pending,
        "registry": reg.registry,
        "publish": publish,
        "approve": reg.approve,
        "reject": reg.reject,
        "revoke": reg.revoke,
        "forget": reg.forget,
        "approved_file": approved_file,
        "description": "UI-слой плагинов: слоты, тема, подтверждения и откат",
    }


# ============================================================
# Инструменты агента
# ============================================================
def _parse_args(args: Any) -> Dict[str, Any]:
    """Аргументы инструмента: JSON-объект, JSON-строка или «k: v | k: v»."""
    if isinstance(args, dict):
        return args
    text = str(args or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"value": data}
    except Exception:
        swallow.note('agent_ui._parse_args:L859')
        pass
    out: Dict[str, Any] = {}
    for part in re.split(r"[|\n]", text):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        out[key.strip().lower()] = value.strip()
    return out


def _ui_help() -> str:
    return ("Формат: JSON {\"plugin\":\"имя\",\"description\":\"зачем\",\"ui_json\":{...},\"css\":\"...\",\"js\":\"...\"}\n"
            "Слоты: " + ", ".join(UI_SLOTS) + "\n"
            "Переменные темы: " + ", ".join(THEME_VARS) + "\n"
            "Действия ui.json: " + ", ".join(SAFE_ACTIONS))


def tool_ui_set(args: Any = "", **kw: Any) -> Dict[str, Any]:
    """Опубликовать UI-часть плагина (кнопки/тема/оверлей) — уходит на подтверждение."""
    data = _parse_args(args)
    name = str(data.get("plugin") or data.get("name") or data.get("id") or "").strip()
    if not name:
        return {"success": False, "output": "❌ Не указан plugin.\n" + _ui_help(),
                "tool": "ui_set"}
    ui_json = data.get("ui_json")
    if ui_json is None and ("slots" in data or "theme" in data):
        ui_json = {k: data[k] for k in ("slots", "theme", "css") if k in data}
    res = publish(
        name=name,
        description=str(data.get("description", "") or ""),
        ui_json=ui_json,
        css=data.get("css") if data.get("css") is not None else None,
        js=data.get("js") if data.get("js") is not None else None,
        actor="agent",
    )
    if not res.get("ok"):
        return {"success": False, "output": f"❌ {res.get('error')}\n" + _ui_help(),
                "tool": "ui_set"}
    status = res.get("status")
    where = []
    if res.get("slots"):
        where.append("слоты: " + ", ".join(res["slots"]))
    if res.get("theme_vars"):
        where.append("тема: " + ", ".join(res["theme_vars"]))
    if res.get("raw"):
        where.append("СЫРОЙ ui.js (полный доступ к странице)")
    detail = ("; ".join(where)) or "нет активных вкладов"
    if status == "pending":
        out = (f"⏳ UI плагина '{name}' сохранён и ЖДЁТ ПОДТВЕРЖДЕНИЯ пользователя "
               f"(hash {res.get('hash')}). Ничего не активировано.\n"
               f"Что предлагается: {detail}\n"
               + (f"Риски: {', '.join(res.get('risks') or [])}\n" if res.get("risks") else "")
               + "Сообщи пользователю: изменение интерфейса появится после нажатия "
                 "«Разрешить» в окне подтверждения (или «Разрешить все» — тогда "
                 "дальнейшие правки этого плагина больше не будут спрашивать). "
                 "Не вызывай ui_set повторно с тем же содержимым и не жди в этом ходу.")
    elif status == "approved":
        if res.get("auto_approve"):
            out = (f"✅ UI плагина '{name}' обновлён и СРАЗУ применён: у плагина есть "
                   f"разрешение на все дальнейшие изменения. ({detail})")
        else:
            out = f"✅ UI плагина '{name}' уже разрешён и активен. ({detail})"
    elif status == "rejected":
        out = (f"⛔ Пользователь уже отклонил ИМЕННО ЭТУ версию UI плагина '{name}'. "
               "Измени содержимое (иначе спроса не будет) или оставь как есть.")
    else:
        out = f"ℹ️ UI-часть плагина '{name}' удалена (нет активных вкладов)."
    if res.get("decl_errors"):
        out += "\n⚠️ Замечания к ui.json: " + "; ".join(res["decl_errors"])
    return {"success": True, "output": out, "tool": "ui_set",
            "ui_status": status, "ui_pending": status == "pending",
            "plugin": name, "hash": res.get("hash")}


def _format_state(st: Dict[str, Any]) -> str:
    lines = [f"UI плагина '{st.get('name')}': статус {st.get('status')}",
             f"  hash: {st.get('hash')}"
             + (f" (утверждён: {st.get('approved_hash')})" if st.get("approved_hash") else ""),
             f"  файлы: {', '.join(st.get('files') or []) or 'нет'}",
             f"  слоты: {', '.join(st.get('slots') or []) or 'нет'}",
             f"  тема: {', '.join(st.get('theme_vars') or []) or 'нет'}",
             f"  сырой JS: {'да' if st.get('raw') else 'нет'}",
             f"  авто-разрешение будущих версий: {'да' if st.get('auto_approve') else 'нет'}"]
    if st.get("risks"):
        lines.append("  риски: " + "; ".join(st["risks"]))
    if st.get("decl_errors"):
        lines.append("  замечания ui.json: " + "; ".join(st["decl_errors"]))
    if st.get("status") == "pending":
        lines.append("  → ждёт решения пользователя в окне подтверждения")
    elif st.get("status") == "rejected":
        lines.append(f"  → отклонено: {st.get('reject_reason') or 'без причины'}")
    return "\n".join(lines)


def tool_ui_status(args: Any = "", **kw: Any) -> Dict[str, Any]:
    """Статус UI-плагинов: разрешено / ждёт подтверждения / отклонено."""
    data = _parse_args(args)
    name = str(data.get("plugin") or data.get("name") or data.get("value") or "").strip()
    reg = get_registry()
    if name:
        st = reg.describe(name)
        if not st.get("ok"):
            return {"success": False, "output": f"❌ {st.get('error')}", "tool": "ui_status"}
        return {"success": True, "output": _format_state(st), "tool": "ui_status",
                "ui_status": st.get("status"), "plugin": st.get("name")}
    rows = [b for b in (bundle(p) for p in reg._all_plugins()) if b["has_ui"]]
    if not rows:
        return {"success": True, "output": "UI-части нет ни у одного плагина. "
                "Создай её через [TOOL:ui_set].\n" + _ui_help(), "tool": "ui_status"}
    lines = ["UI-плагины (%d):" % len(rows)]
    for b in rows:
        st = reg.describe(b["name"])
        mark = {"approved": "✅ разрешено", "pending": "⏳ ждёт подтверждения",
                "rejected": "⛔ отклонено", "none": "—"}.get(st.get("status"), "?")
        extra = []
        if st.get("shipped"):
            extra.append("core")
        if st.get("raw"):
            extra.append("raw-js")
        if st.get("auto_approve"):
            extra.append("авто-разрешение")
        lines.append(f"  {mark} | {b['name']} | hash {st.get('hash')}"
                     + (f" | {', '.join(extra)}" if extra else "")
                     + (f" | слоты: {', '.join(st.get('slots') or [])}" if st.get("slots") else "")
                     + (f" | тема: {', '.join(st.get('theme_vars') or [])}" if st.get("theme_vars") else ""))
    pend = reg.pending()
    if pend:
        lines.append("")
        lines.append("Ждут решения пользователя: " + ", ".join(p["name"] for p in pend))
        lines.append("Пользователь подтверждает их в окне на странице агента "
                     "(или в /agent-plugins): «Разрешить» / «Разрешить все» / «Отклонить».")
    return {"success": True, "output": "\n".join(lines), "tool": "ui_status",
            "pending_count": len(pend)}


def tool_ui_revert(args: Any = "", **kw: Any) -> Dict[str, Any]:
    """Снять разрешение и (по желанию) удалить UI-часть плагина."""
    data = _parse_args(args)
    name = str(data.get("plugin") or data.get("name") or data.get("value") or "").strip()
    if not name:
        return {"success": False, "output": "❌ Не указан plugin. " + _ui_help(),
                "tool": "ui_revert"}
    remove = str(data.get("remove_files", data.get("remove", ""))).strip().lower() in (
        "1", "true", "yes", "да")
    res = get_registry().revoke(name, remove_files=remove, actor="agent")
    if not res.get("ok"):
        return {"success": False, "output": f"❌ {res.get('error')}", "tool": "ui_revert"}
    if remove:
        out = f"✅ UI-часть плагина '{name}' удалена, интерфейс вернулся к исходному."
    else:
        out = (f"✅ Разрешение на UI плагина '{name}' снято: следующий показ/правка снова "
               "потребует подтверждения пользователя. На странице вклад снимут при "
               "следующем чтении реестра.")
    return {"success": True, "output": out, "tool": "ui_revert", "ui_status": res.get("status")}


UI_TOOLS = {
    "ui_set": tool_ui_set,
    "ui_status": tool_ui_status,
    "ui_revert": tool_ui_revert,
}

UI_TOOLS_PROMPT = """- ui_set: Изменить СВОЙ интерфейс (кнопка в сайдбаре, панель/оверлей, фон и цвета). Args: JSON {"plugin":"имя плагина","description":"зачем","ui_json":{"slots":[...],"theme":{...},"css":"..."},"css":"...","js":"..."}. ui_json — безопасный декларативный уровень (JS не исполняется): slots[].slot из списка слотов, action.type из списка действий, theme — только разрешённые переменные. css — стили. js — СЫРОЙ JavaScript на странице (полный доступ): используй только если декларации мало и обязательно регистрируй всё через window.InfinityUI, иначе откат не сработает. ВАЖНО: изменение применяется ТОЛЬКО после подтверждения пользователя (кнопки «Разрешить» / «Разрешить все» / «Отклонить» в окне на странице агента); в ответе придёт ui_pending=true — сообщи об этом пользователю и не повторяй вызов.
- ui_status: Статус UI-плагинов: что разрешено, что ждёт подтверждения, что отклонено. Args: JSON {"plugin":"имя"} (пусто = все).
- ui_revert: Снять разрешение и вернуть исходный интерфейс. Args: JSON {"plugin":"имя","remove_files":true} (remove_files=true — удалить UI-часть плагина)."""

UI_API_PROMPT = """window.InfinityUI — безопасный API твоего ui.js (всё возвращает handle и полностью откатывается):
  InfinityUI.add(slot, {id, label, icon, title, order, onClick}) -> {remove()}
  InfinityUI.slot(slot) -> HTMLElement | null            // контейнер слота
  InfinityUI.remove(slot, id)                            // снять свою кнопку
  InfinityUI.css(text) -> {remove()}                     // свои стили
  InfinityUI.theme.set({"--bg":{light:"#fff",dark:"#000"}}) / InfinityUI.theme.reset()
  InfinityUI.on("message"|"session"|"ready", fn) -> {off()}
  InfinityUI.toast("текст", "success"|"error"|"info")
  InfinityUI.reset()                                     // снять ВСЁ, что сделал плагин
Слоты: {slots}
Переменные темы: {vars}
Действия декларативного ui.json: {actions}"""


if __name__ == "__main__":
    import sys
    _reg = get_registry()
    if len(sys.argv) > 1 and sys.argv[1] == "pending":
        print(json.dumps(_reg.pending(), ensure_ascii=False, indent=2)[:4000])
    else:
        print(json.dumps(_reg.registry(), ensure_ascii=False, indent=2)[:4000])