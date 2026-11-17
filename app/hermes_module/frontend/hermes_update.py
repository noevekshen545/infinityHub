"""Обновление Hermes с GitHub Releases без потери интеграции и настроек.

Ставится только официальный релизный тег NousResearch/hermes-agent. Установка
повторяет проверенный ручной сценарий: backend переименовывается в резервную
копию, на его место копируются исходники, применяются локальные патчи Infinity
и всё проверяется реальным HermesAgent.exe. Любая ошибка — возврат бэкапа.

Не трогаются: .hermes-portable/ (config.yaml, .env, сессии, память, канбан,
скилы), plugins/, integration/, frontend/, hermes/HermesAgent.exe и app/config.json.
Шлюз обновление не перезапускает: его поднимает пользователь кнопкой.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent          # app/hermes_module
BACKEND = ROOT / "backend"
STAGING_ROOT = ROOT / ".update-staging"
BACKUPS = ROOT.parent.parent / "_backups"               # как у update_hermes.ps1
EXE = ROOT.parent.parent / "hermes" / "HermesAgent.exe"
STATE_FILE = ROOT / "hermes_update.json"
LOG_FILE = ROOT / "logs" / "hermes_update.log"

REPO = "NousResearch/hermes-agent"
GIT_URL = f"https://github.com/{REPO}.git"
API = f"https://api.github.com/repos/{REPO}"
TAG_RE = re.compile(r"^v?[0-9][0-9A-Za-z.+-]{0,63}$")
BACKUP_RE = re.compile(r"^hermes-backend-[0-9]{8}-[0-9]{6}(?:-[a-z-]+)?$")

EXCLUDE_DIRS = {
    "tests", "tests-js", "website", ".github", "evals", "contributors",
    "node_modules", "__pycache__", ".git", ".venv", "venv",
    "hermes_agent.egg-info", ".pytest_cache", "out",
}
EXCLUDE_FILES = {
    "activate", "activate.bat", "activate.csh", "activate.fish", "activate.nu",
    "activate.ps1", "activate.tcsh", "deactivate", "deactivate.bat", "deactivate.ps1",
    "pydoc.bat",
}

_LOCK = threading.RLock()
_BUSY = False
_PROGRESS: List[str] = []
_CONFIG = {
    "enabled": False,
    "auto_install": True,
    "check_interval_h": 24,
    "last_check": 0,
}
_STATE: Dict[str, Any] = {
    "installed": None,
    "latest": None,
    "staged": None,
    "last_result": None,
    "history": [],
}


# --- состояние -----------------------------------------------------------------
def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load() -> None:
    saved = _read_json(STATE_FILE)
    for key in ("installed", "latest", "staged", "last_result"):
        if isinstance(saved.get(key), dict):
            _STATE[key] = saved[key]
    if isinstance(saved.get("history"), list):
        _STATE["history"] = saved["history"][-20:]
    cfg = saved.get("config")
    if isinstance(cfg, dict):
        _CONFIG.update({k: cfg[k] for k in _CONFIG if k in cfg})
    if not _STATE["installed"]:
        _STATE["installed"] = {"tag": _manifest_version(), "commit": None, "installed_at": None}


def _save() -> None:
    _write_json(STATE_FILE, {
        "config": _CONFIG,
        "installed": _STATE["installed"],
        "latest": _STATE["latest"],
        "staged": _STATE["staged"],
        "last_result": _STATE["last_result"],
        "history": _STATE["history"][-20:],
    })


def _manifest_version() -> str:
    """Версия из app.json: backend/pyproject.toml у Hermes всегда 0.0.0."""
    data = _read_json(ROOT / "app.json")
    v = str(data.get("version") or "").strip()
    if not v:
        return "неизвестно"
    return v if v.lower().startswith("v") else "v" + v


def get_config() -> dict:
    _load()
    return dict(_CONFIG)


def set_config(patch: dict) -> dict:
    with _LOCK:
        _load()
        if "enabled" in patch:
            _CONFIG["enabled"] = bool(patch["enabled"])
        if "auto_install" in patch:
            _CONFIG["auto_install"] = bool(patch["auto_install"])
        if "check_interval_h" in patch:
            try:
                _CONFIG["check_interval_h"] = max(1, min(168, int(patch["check_interval_h"])))
            except (TypeError, ValueError):
                raise ValueError("check_interval_h: не число")
        _save()
        return dict(_CONFIG)


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    _PROGRESS.append(line)
    del _PROGRESS[:-300]
    print("[hermes-update] " + str(msg), flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE.with_suffix(".log.1"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def is_busy() -> bool:
    return _BUSY


def snapshot() -> dict:
    with _LOCK:
        _load()
        return {
            "enabled": _CONFIG["enabled"],
            "auto_install": _CONFIG["auto_install"],
            "check_interval_h": _CONFIG["check_interval_h"],
            "last_check": _CONFIG["last_check"],
            "installed": _STATE["installed"],
            "latest": _STATE["latest"],
            "staged": _usable_staged(),
            "last_result": _STATE["last_result"],
            "history": list(reversed(_STATE["history"][-8:])),
            "busy": _BUSY,
            "progress": _PROGRESS[-60:],
            "backups": list_backups(),
        }


# --- версии и GitHub -----------------------------------------------------------
def _vtuple(tag: str) -> Optional[tuple]:
    m = re.search(r"(\d+(?:\.\d+)*)", str(tag or ""))
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def _newer(latest: str, installed: str) -> bool:
    a, b = _vtuple(latest), _vtuple(installed)
    if a is None:
        return False
    if b is None:
        return True
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _gh_json(url: str, timeout: float = 25) -> dict:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "InfinitySearch-Hermes-Updater",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("релиз или тег не найден на GitHub")
        if e.code == 403:
            raise RuntimeError("GitHub ограничил частоту запросов, повторите позже")
        raise RuntimeError(f"GitHub ответил {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"нет связи с GitHub: {e.reason}")


def _release_sha(tag: str) -> Optional[str]:
    try:
        return str(_gh_json(f"{API}/commits/{tag}").get("sha") or "") or None
    except RuntimeError:
        return None


def check(force: bool = False) -> dict:
    with _LOCK:
        _load()
        installed = (_STATE.get("installed") or {}).get("tag") or _manifest_version()
        last = float(_CONFIG.get("last_check") or 0)
        cached = _STATE.get("latest")
        if not force and cached and time.time() - last < 600:
            latest = cached
        else:
            latest = None
    if latest is None:
        rel = _gh_json(f"{API}/releases/latest")
        tag = str(rel.get("tag_name") or "").strip()
        if not TAG_RE.match(tag):
            raise RuntimeError("GitHub вернул неожиданный тег релиза")
        latest = {
            "tag": tag,
            "name": rel.get("name") or tag,
            "published_at": rel.get("published_at"),
            "html_url": rel.get("html_url"),
            "notes": str(rel.get("body") or "")[:4000],
            "prerelease": bool(rel.get("prerelease")),
            "commit": _release_sha(tag),
        }
        with _LOCK:
            _STATE["latest"] = latest
            _CONFIG["last_check"] = time.time()
            _save()
    return {
        "installed": installed,
        "latest": latest,
        "newer": _newer((latest or {}).get("tag", ""), installed),
    }


# --- скачивание ----------------------------------------------------------------
def _staging_dir(tag: str) -> Path:
    return STAGING_ROOT / tag


def _usable_staged() -> Optional[dict]:
    st = _STATE.get("staged")
    if not isinstance(st, dict):
        return None
    path = Path(str(st.get("path") or ""))
    if not path.is_dir() or not (path / "hermes_cli" / "main.py").is_file():
        return None
    return st


def _run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def download(tag: Optional[str] = None) -> dict:
    global _BUSY
    with _LOCK:
        _load()
        if not _CONFIG["enabled"]:
            raise RuntimeError("обновления выключены рубильником")
        if _BUSY:
            raise RuntimeError("уже выполняется другая операция")
        if not tag:
            tag = str((_STATE.get("latest") or {}).get("tag") or "")
        if not TAG_RE.match(tag or ""):
            raise RuntimeError("некорректный тег версии")
        if not EXE.is_file():
            raise RuntimeError(f"не найден HermesAgent.exe: {EXE}")
        git = shutil.which("git")
        if not git:
            raise RuntimeError("git не установлен — скачивание невозможно")
        _BUSY = True
    target = _staging_dir(tag)
    try:
        log(f"скачиваю {tag} из {GIT_URL}")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        STAGING_ROOT.mkdir(parents=True, exist_ok=True)
        r = _run([git, "clone", "--depth", "1", "--single-branch",
                  "--branch", tag, GIT_URL, str(target)], timeout=1800)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip()[-700:]
            raise RuntimeError(f"git clone не удался: {tail}")
        head = _run([git, "-C", str(target), "rev-parse", "HEAD"], timeout=60).stdout.strip()
        expected = _release_sha(tag)
        if expected and head.lower() != expected.lower():
            raise RuntimeError(f"коммит тега не совпал: {head[:12]} != {expected[:12]}")
        for rel in ("hermes_cli/main.py", "gateway/run.py", "pyproject.toml"):
            if not (target / rel).is_file():
                raise RuntimeError(f"в архиве нет {rel}")
        shutil.rmtree(target / ".git", ignore_errors=True)
        size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
        with _LOCK:
            _STATE["staged"] = {
                "tag": tag, "commit": head or expected, "path": str(target),
                "downloaded_at": time.time(), "bytes": size,
            }
            _save()
            staged = dict(_STATE["staged"])
        log(f"скачано: {tag} ({head[:12] or 'sha?'}), {size / 1048576:.1f} МБ")
        return staged
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    finally:
        with _LOCK:
            _BUSY = False


# --- установка -----------------------------------------------------------------
def _copy_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            if name in EXCLUDE_FILES or name.endswith((".pyc", ".pyo")):
                continue
            shutil.copy2(Path(root) / name, dst / rel / name)


def _verify() -> None:
    r = _run([str(EXE), "-m", "integration.update_helper"], cwd=ROOT, timeout=300)
    if r.returncode != 0:
        tail = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()[-1800:]
        raise RuntimeError("проверка HermesAgent.exe не прошла:\n" + tail)
    log("проверка HermesAgent.exe пройдена")


def _set_app_version(tag: str) -> None:
    path = ROOT / "app.json"
    data = _read_json(path)
    if not data:
        return
    data["version"] = tag[1:] if tag.lower().startswith("v") else tag
    _write_json(path, data)


def install_staged(force: bool = False) -> dict:
    global _BUSY
    with _LOCK:
        _load()
        if not _CONFIG["enabled"]:
            raise RuntimeError("обновления выключены рубильником")
        if _BUSY:
            raise RuntimeError("уже выполняется другая операция")
        st = _usable_staged()
        if not st:
            raise RuntimeError("нет скачанной версии — сначала «Скачать»")
        tag = str(st["tag"])
        installed = (_STATE.get("installed") or {}).get("tag") or _manifest_version()
        if not force and not _newer(tag, installed) and tag != installed:
            raise RuntimeError(f"скачанная версия {tag} не новее установленной {installed}")
        src = Path(st["path"])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = BACKUPS / f"hermes-backend-{stamp}"
        BACKUPS.mkdir(parents=True, exist_ok=True)
        _BUSY = True
    moved = False
    try:
        log(f"установка {tag}: backend -> {backup.name}")
        os.replace(BACKEND, backup)
        moved = True
        log("копирую исходники в backend")
        _copy_tree(src, BACKEND)
        try:
            from . import updater as _updater
        except ImportError:
            import updater as _updater
        n = _updater.apply_local_patches()
        log(f"локальных патчей применено: {n}")
        log("проверяю импорты в замороженном рантайме")
        _verify()
    except Exception as exc:
        log(f"ОШИБКА: {exc}")
        if moved:
            try:
                if BACKEND.exists():
                    shutil.rmtree(BACKEND)
                if backup.exists():
                    os.replace(backup, BACKEND)
                    log("backend восстановлен из резервной копии")
            except Exception as rexc:  # noqa: BLE001
                log(f"КРИТИЧНО: откат не удался: {rexc}; бэкап: {backup}")
                raise RuntimeError(f"установка не удалась и откат не удался: {rexc}. "
                                   f"Верните вручную из {backup}") from exc
        with _LOCK:
            _STATE["last_result"] = {"ok": False, "tag": tag, "at": time.time(),
                                     "error": str(exc), "backup": str(backup)}
            _STATE["history"].append(_STATE["last_result"])
            _save()
        raise
    else:
        with _LOCK:
            _STATE["installed"] = {
                "tag": tag, "commit": st.get("commit"),
                "installed_at": time.time(), "backup": str(backup),
            }
            _STATE["staged"] = None
            _STATE["last_result"] = {"ok": True, "tag": tag, "at": time.time(),
                                     "backup": str(backup)}
            _STATE["history"].append(_STATE["last_result"])
            _save()
        _set_app_version(tag)
        shutil.rmtree(src, ignore_errors=True)
        log(f"установлено {tag}; шлюз остановлен — нужен ручной перезапуск")
        return {"tag": tag, "backup": str(backup), "restart_required": True}
    finally:
        with _LOCK:
            _BUSY = False


# --- резервные копии -----------------------------------------------------------
def list_backups() -> List[dict]:
    out: List[dict] = []
    try:
        for p in BACKUPS.iterdir():
            if p.is_dir() and BACKUP_RE.match(p.name):
                try:
                    out.append({"name": p.name, "at": p.stat().st_mtime,
                                "bytes": sum(f.stat().st_size for f in p.rglob("*") if f.is_file())})
                except OSError:
                    out.append({"name": p.name, "at": p.stat().st_mtime, "bytes": 0})
    except OSError:
        pass
    return sorted(out, key=lambda x: x["at"], reverse=True)


def rollback(name: str) -> dict:
    global _BUSY
    if not BACKUP_RE.match(name or ""):
        raise RuntimeError("некорректное имя резервной копии")
    src = BACKUPS / name
    if not src.is_dir():
        raise RuntimeError("резервная копия не найдена")
    with _LOCK:
        _load()
        if not _CONFIG["enabled"]:
            raise RuntimeError("обновления выключены рубильником")
        if _BUSY:
            raise RuntimeError("уже выполняется другая операция")
        _BUSY = True
        try:
            current = BACKUPS / ("hermes-backend-" + time.strftime("%Y%m%d-%H%M%S") + "-replaced")
            log(f"откат на {name}")
            if BACKEND.exists():
                os.replace(BACKEND, current)
            try:
                os.replace(src, BACKEND)
            except Exception:
                if current.exists():
                    os.replace(current, BACKEND)
                raise
            _STATE["installed"] = {"tag": _manifest_version(), "commit": None,
                                   "installed_at": time.time(), "restored_from": name}
            _STATE["last_result"] = {"ok": True, "rollback": name, "at": time.time()}
            _STATE["history"].append(_STATE["last_result"])
            _save()
            log(f"откат выполнен; текущий backend сохранён как {current.name}")
            return {"ok": True, "restored": name, "previous": current.name,
                    "restart_required": True}
        finally:
            with _LOCK:
                _BUSY = False


_load()
