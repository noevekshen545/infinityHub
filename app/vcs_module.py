"""Свой VCS приложения: контентно-адресуемые объекты, коммиты и ветки.

Зачем свой, а не системный git: приложение портативное, а агенту нужен
нативный инструмент, который живёт внутри профиля и не требует внешнего
бинарника. Набор операций повторяет привычный git там, где это нужно человеку
и агенту: status, commit, log, show, diff, branch, checkout, merge (только
fast-forward), revert, restore.

Устройство простое и проверяемое:

* ``objects/<aa>/<sha256>`` — zlib-сжатое содержимое. Ключ адреса — хеш,
  поэтому одинаковые данные не дублируются;
* tree-объект — JSON ``{"type": "tree", "entries": {path: blob_hash}}``;
* commit-объект — JSON с деревом, родителем, автором, сообщением и меткой
  сессии (по ней UI показывает коммиты выбранного чата);
* ``refs/heads/<name>`` и ``HEAD`` — как в git: обычные текстовые файлы;
* ``reflog.jsonl`` — журнал перемещений ссылок, чтобы откат ссылки было видно.

Версионируются не файлы проекта, а источники профиля (переписки и настройки);
их собирает :mod:`vcs_sources`. Модуль ничего не знает про профили: на вход
приходит словарь ``логический путь -> bytes``. Это позволяет позже добавить
файлы проекта, не меняя формат хранилища.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
import time
import zlib
from pathlib import Path

__all__ = [
    "VcsError", "Repository", "DEFAULT_BRANCH",
    "repo_at", "repo_for",
]

DEFAULT_BRANCH = "main"

# Диффы текстов показываем целиком только до этого размера: иначе ответ агента
# раздувается, а построчная разница на мегабайтах JSON ещё и считается долго.
MAX_DIFF_BYTES = 80 * 1024
MAX_DIFF_TOTAL = 300 * 1024
MAX_MESSAGE = 4000
MAX_LOG = 200

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}$")
_HEX_RE = re.compile(r"^[0-9a-f]{4,64}$")

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class VcsError(ValueError):
    """Ошибка операции VCS с понятным кодом для маршрута/действия."""

    def __init__(self, message, status=400, code="vcs_error", extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra or {}


def _lock_for(root: Path) -> threading.RLock:
    key = str(root)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


def _canon(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.tmp-%d-%d" % (path.name, os.getpid(),
                                           threading.get_ident()))
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _safe_relpath(path: str) -> str:
    """Логический путь внутри снимка: без абсолютов, ``..`` и обратных слэшей."""
    text = str(path or "").replace("\\", "/").strip()
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not text or text.startswith("/") or ".." in parts:
        raise VcsError("Недопустимый путь в снимке: %r" % (path,), 400,
                       "invalid_path")
    return "/".join(parts)


def _check_branch_name(name: str) -> str:
    text = str(name or "").strip()
    if (not _BRANCH_RE.match(text) or ".." in text or text.endswith("/")
            or text.endswith(".lock") or text.upper() == "HEAD"):
        raise VcsError("Недопустимое имя ветки: %r" % (name,), 400,
                       "invalid_branch")
    return text


def _decode_text(data: bytes):
    if data is None:
        return ""
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class Repository:
    """Одно хранилище (у профиля своё): объекты, ссылки и журнал."""

    def __init__(self, root):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.refs = self.root / "refs" / "heads"
        self.head_file = self.root / "HEAD"
        self.reflog_file = self.root / "reflog.jsonl"
        self._lock = _lock_for(self.root)
        with self._lock:
            self.objects.mkdir(parents=True, exist_ok=True)
            self.refs.mkdir(parents=True, exist_ok=True)
            if not self.head_file.exists():
                _atomic_write(self.head_file,
                              ("ref: refs/heads/%s\n" % DEFAULT_BRANCH).encode())

    # ── объекты ──────────────────────────────────────────────────────────

    def _object_path(self, obj_hash: str) -> Path:
        if not _HEX_RE.match(obj_hash or ""):
            raise VcsError("Некорректный id объекта: %r" % (obj_hash,), 400,
                           "invalid_object_id")
        return self.objects / obj_hash[:2] / obj_hash[2:]

    def put_blob(self, data: bytes) -> str:
        if not isinstance(data, (bytes, bytearray)):
            raise VcsError("Содержимое должно быть bytes", 400, "invalid_blob")
        raw = bytes(data)
        obj_hash = _hash(raw)
        path = self._object_path(obj_hash)
        if not path.exists():
            _atomic_write(path, zlib.compress(raw, 6))
        return obj_hash

    def get_blob(self, obj_hash: str) -> bytes:
        path = self._object_path(obj_hash)
        try:
            return zlib.decompress(path.read_bytes())
        except FileNotFoundError:
            raise VcsError("Объект не найден: %s" % obj_hash, 404,
                           "object_not_found")
        except zlib.error as exc:
            raise VcsError("Объект повреждён: %s (%s)" % (obj_hash, exc), 500,
                           "object_corrupt")

    def put_json(self, obj) -> str:
        return self.put_blob(_canon(obj))

    def get_json(self, obj_hash: str):
        try:
            obj = json.loads(self.get_blob(obj_hash).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VcsError("Объект не разбирается: %s (%s)" % (obj_hash, exc), 500,
                           "object_corrupt")
        if not isinstance(obj, dict):
            raise VcsError("Объект не того типа: %s" % obj_hash, 500,
                           "object_corrupt")
        return obj

    # ── ссылки и HEAD ─────────────────────────────────────────────────────

    def head_ref(self) -> str:
        text = ""
        try:
            text = self.head_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if text.startswith("ref: "):
            return text[5:].strip()
        return "refs/heads/%s" % DEFAULT_BRANCH

    def current_branch(self) -> str:
        ref = self.head_ref()
        prefix = "refs/heads/"
        return ref[len(prefix):] if ref.startswith(prefix) else ""

    def _ref_path(self, name: str) -> Path:
        return self.refs / _check_branch_name(name)

    def ref_commit(self, name: str):
        try:
            return self._ref_path(name).read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def head_commit(self):
        branch = self.current_branch()
        return self.ref_commit(branch) if branch else None

    def _write_ref(self, name: str, commit_id: str) -> None:
        _atomic_write(self._ref_path(name), (commit_id + "\n").encode())

    def _append_reflog(self, old, new, message, branch) -> None:
        row = {"ts": time.time(), "old": old, "new": new, "branch": branch,
               "message": str(message or "")[:200]}
        try:
            with open(self.reflog_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            # Журнал — удобство, а не источник истины: коммит не должен падать.
            pass

    # ── снимки ───────────────────────────────────────────────────────────

    def snapshot(self, files: dict) -> tuple[str, dict]:
        """Записать blobs и вернуть (tree_hash, {path: blob_hash})."""
        manifest = {}
        for raw_path, data in (files or {}).items():
            path = _safe_relpath(raw_path)
            if not isinstance(data, (bytes, bytearray)):
                raise VcsError("Содержимое %s должно быть bytes" % path, 400,
                               "invalid_blob")
            manifest[path] = self.put_blob(bytes(data))
        tree_hash = self.put_json({"type": "tree", "entries": manifest})
        return tree_hash, manifest

    def tree_manifest(self, tree_hash: str) -> dict:
        obj = self.get_json(tree_hash)
        if obj.get("type") != "tree" or not isinstance(obj.get("entries"), dict):
            raise VcsError("Объект не является деревом: %s" % tree_hash, 500,
                           "object_corrupt")
        return dict(obj["entries"])

    def commit_manifest(self, commit_ref: str = "") -> dict:
        commit = self.read_commit(commit_ref)
        return self.tree_manifest(commit["tree"])

    # ── коммиты ──────────────────────────────────────────────────────────

    def resolve_commit(self, ref: str = "") -> str:
        """Полный id коммита по ссылке, имени ветки, HEAD или префиксу."""
        text = str(ref or "").strip()
        if not text or text.upper() == "HEAD":
            current = self.head_commit()
            if not current:
                raise VcsError("В репозитории ещё нет коммитов", 404,
                               "no_commits")
            return current
        if text in ("@", "head"):
            return self.resolve_commit("")
        # Ветку проверяем файлом ссылки, а не branches(): тот читает сообщения
        # коммитов и снова зовёт resolve_commit — была бы рекурсия.
        try:
            ref_path = self._ref_path(text)
        except VcsError:
            ref_path = None
        if ref_path is not None and ref_path.exists():
            return self.ref_commit(text)
        if _HEX_RE.match(text):
            if len(text) == 64 and self._object_path(text).exists():
                return text
            prefix = text[:2]
            folder = self.objects / prefix
            if folder.is_dir():
                hits = [prefix + p.name for p in folder.iterdir()
                        if p.name.startswith(text[2:])]
                if len(hits) == 1:
                    return hits[0]
                if len(hits) > 1:
                    raise VcsError("Неоднозначный id коммита: %s" % text, 400,
                                   "ambiguous_commit")
        raise VcsError("Коммит не найден: %s" % ref, 404, "commit_not_found")

    def read_commit(self, ref: str = "") -> dict:
        commit_id = self.resolve_commit(ref)
        obj = self.get_json(commit_id)
        if obj.get("type") != "commit" or not isinstance(obj.get("tree"), str):
            raise VcsError("Объект не является коммитом: %s" % commit_id, 500,
                           "object_corrupt")
        obj["id"] = commit_id
        return obj

    def commit(self, message: str, files: dict, *, author: str = "",
               session_id: str = "", branch: str = "") -> dict:
        text = str(message or "").strip()
        if not text:
            raise VcsError("Нужно сообщение коммита", 400, "message_required")
        text = text[:MAX_MESSAGE]
        with self._lock:
            branch = _check_branch_name(branch or self.current_branch()
                                        or DEFAULT_BRANCH)
            parent = self.ref_commit(branch)
            tree_hash, manifest = self.snapshot(files)
            commit_obj = {
                "type": "commit",
                "tree": tree_hash,
                "parents": [parent] if parent else [],
                "author": str(author or "")[:120],
                "message": text,
                "session_id": str(session_id or "")[:200],
                "created_at": time.time(),
            }
            commit_id = self.put_json(commit_obj)
            self._write_ref(branch, commit_id)
            self._append_reflog(parent, commit_id, text, branch)
            return {
                "id": commit_id,
                "short": commit_id[:12],
                "branch": branch,
                "message": text,
                "author": commit_obj["author"],
                "session_id": commit_obj["session_id"],
                "created_at": commit_obj["created_at"],
                "parent": parent,
                "tree": tree_hash,
                "files": len(manifest),
                "empty": bool(parent and manifest == self.tree_manifest(
                    self.read_commit(parent)["tree"])),
            }

    def log(self, limit: int = 20, ref: str = "") -> list:
        try:
            limit = max(1, min(int(limit or 20), MAX_LOG))
        except (TypeError, ValueError):
            limit = 20
        commit_id = self.resolve_commit(ref) if ref else self.head_commit()
        rows = []
        seen = set()
        while commit_id and len(rows) < limit and commit_id not in seen:
            seen.add(commit_id)
            obj = self.read_commit(commit_id)
            rows.append({
                "id": commit_id,
                "short": commit_id[:12],
                "message": obj.get("message", ""),
                "author": obj.get("author", ""),
                "session_id": obj.get("session_id", ""),
                "created_at": obj.get("created_at", 0),
                "parents": obj.get("parents") or [],
            })
            parents = obj.get("parents") or []
            commit_id = parents[0] if parents else None
        return rows

    def show(self, ref: str = "") -> dict:
        commit = self.read_commit(ref)
        manifest = self.tree_manifest(commit["tree"])
        files = []
        total = 0
        for path in sorted(manifest):
            size = len(self.get_blob(manifest[path]))
            total += size
            files.append({"path": path, "size": size,
                          "hash": manifest[path], "short": manifest[path][:12]})
        return {
            "id": commit["id"],
            "short": commit["id"][:12],
            "message": commit.get("message", ""),
            "author": commit.get("author", ""),
            "session_id": commit.get("session_id", ""),
            "created_at": commit.get("created_at", 0),
            "parents": commit.get("parents") or [],
            "tree": commit["tree"],
            "files": files,
            "file_count": len(files),
            "total_bytes": total,
            "entries": manifest,
        }

    def status(self, files: dict) -> dict:
        """Сравнить снимок источников (``files``) с деревом HEAD."""
        manifest = {}
        for raw_path, data in (files or {}).items():
            path = _safe_relpath(raw_path)
            manifest[path] = _hash(bytes(data))
        head = self.head_commit()
        base = self.commit_manifest(head) if head else {}
        changes = []
        for path in sorted(set(base) | set(manifest)):
            old = base.get(path)
            new = manifest.get(path)
            if old == new:
                continue
            kind = "added" if old is None else "deleted" if new is None else "modified"
            changes.append({"path": path, "status": kind,
                            "old_hash": old, "hash": new,
                            "size": len(files[path]) if path in files else 0})
        return {
            "branch": self.current_branch(),
            "head": head,
            "head_short": (head or "")[:12],
            "clean": not changes,
            "changes": changes,
            "counts": {
                "added": sum(1 for c in changes if c["status"] == "added"),
                "modified": sum(1 for c in changes if c["status"] == "modified"),
                "deleted": sum(1 for c in changes if c["status"] == "deleted"),
            },
            "files": len(manifest),
        }

    def diff(self, from_ref: str = "", to_ref: str = "", files=None) -> dict:
        """Разница между коммитами; пустой ``to_ref`` — рабочие источники."""
        head = self.head_commit()
        left_ref = from_ref or head or ""
        right_ref = to_ref or ""
        left_commit = self.read_commit(left_ref) if left_ref else None
        left = self.tree_manifest(left_commit["tree"]) if left_commit else {}
        if right_ref:
            right_commit = self.read_commit(right_ref)
            right = self.tree_manifest(right_commit["tree"])
            work = {}
        else:
            # Для рабочего дерева объекты ещё не записаны: содержимое берём
            # прямо из files, иначе get_blob искал бы несуществующий объект.
            work = {_safe_relpath(path): bytes(data)
                    for path, data in (files or {}).items()}
            right = {path: _hash(data) for path, data in work.items()}
        changes = []
        budget = MAX_DIFF_TOTAL
        for path in sorted(set(left) | set(right)):
            old_hash, new_hash = left.get(path), right.get(path)
            if old_hash == new_hash:
                continue
            kind = "added" if old_hash is None else "deleted" if new_hash is None else "modified"
            old = self.get_blob(old_hash) if old_hash else b""
            if right_ref:
                new = self.get_blob(new_hash) if new_hash else b""
            else:
                new = work.get(path, b"")
            text, note, binary = self._unified(path, old, new, budget)
            budget = max(0, budget - len(text))
            changes.append({"path": path, "status": kind,
                            "diff": text, "note": note, "binary": binary,
                            "old_size": len(old), "size": len(new)})
        return {
            "from": (left_commit or {}).get("id"),
            "from_short": ((left_commit or {}).get("id") or "")[:12],
            "to": right_commit["id"] if right_ref else "worktree",
            "to_short": (right_commit["id"][:12] if right_ref else ""),
            "changes": changes,
            "count": len(changes),
        }

    @staticmethod
    def _unified(path: str, old: bytes, new: bytes, budget: int):
        old_text = _decode_text(old)
        new_text = _decode_text(new)
        if old_text is None or new_text is None:
            return "", "двоичные данные — дифф не строится", True
        if len(old) + len(new) > MAX_DIFF_BYTES or budget <= 0:
            return "", "файл слишком большой для диффа", False
        lines = difflib.unified_diff(
            old_text.splitlines(True), new_text.splitlines(True),
            fromfile="a/" + path, tofile="b/" + path, n=3)
        out = "".join(lines)
        if len(out) > min(MAX_DIFF_BYTES, budget):
            out = out[:min(MAX_DIFF_BYTES, budget)] + "\n… дифф усечён"
            return out, "дифф усечён", False
        return out, "", False

    # ── ветки ────────────────────────────────────────────────────────────

    def branches(self) -> list:
        rows = []
        try:
            names = sorted(p.name for p in self.refs.iterdir() if p.is_file())
        except OSError:
            names = []
        current = self.current_branch()
        for name in names:
            commit_id = self.ref_commit(name)
            message = ""
            if commit_id:
                try:
                    message = self.read_commit(commit_id).get("message", "")
                except VcsError:
                    message = ""
            rows.append({"name": name, "current": name == current,
                         "commit": commit_id,
                         "short": (commit_id or "")[:12],
                         "message": message})
        return rows

    def create_branch(self, name: str, at: str = "") -> dict:
        text = _check_branch_name(name)
        with self._lock:
            if self.ref_commit(text):
                raise VcsError("Ветка уже существует: %s" % text, 409,
                               "branch_exists")
            commit_id = self.resolve_commit(at)
            self._write_ref(text, commit_id)
            return {"name": text, "commit": commit_id, "short": commit_id[:12]}

    def checkout(self, name: str) -> dict:
        text = _check_branch_name(name)
        with self._lock:
            commit_id = self.ref_commit(text)
            if not commit_id:
                raise VcsError("Ветка не найдена: %s" % text, 404,
                               "branch_not_found")
            _atomic_write(self.head_file,
                          ("ref: refs/heads/%s\n" % text).encode())
            return {"branch": text, "head": commit_id, "short": commit_id[:12]}

    def delete_branch(self, name: str) -> dict:
        text = _check_branch_name(name)
        with self._lock:
            if text == self.current_branch():
                raise VcsError("Нельзя удалить текущую ветку", 409,
                               "branch_is_current")
            path = self._ref_path(text)
            if not path.exists():
                raise VcsError("Ветка не найдена: %s" % text, 404,
                               "branch_not_found")
            path.unlink()
            return {"deleted": text}

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        seen = set()
        stack = [descendant]
        while stack:
            current = stack.pop()
            if current == ancestor:
                return True
            if current in seen:
                continue
            seen.add(current)
            try:
                stack.extend(self.read_commit(current).get("parents") or [])
            except VcsError:
                return False
        return False

    def merge_fast_forward(self, name: str) -> dict:
        """Только fast-forward: без слияния деревьев и конфликтов."""
        text = _check_branch_name(name)
        with self._lock:
            target = self.ref_commit(text)
            if not target:
                raise VcsError("Ветка не найдена: %s" % text, 404,
                               "branch_not_found")
            current_branch = self.current_branch()
            current = self.ref_commit(current_branch)
            if current == target:
                return {"branch": current_branch, "up_to_date": True,
                        "head": current, "short": (current or "")[:12]}
            if current and not self._is_ancestor(current, target):
                raise VcsError(
                    "Ветки разошлись — нужно ручное слияние (fast-forward невозможен)",
                    409, "merge_not_fast_forward",
                    {"current": current, "target": target})
            self._write_ref(current_branch, target)
            self._append_reflog(current, target,
                                "fast-forward %s" % text, current_branch)
            return {"branch": current_branch, "merged": text,
                    "head": target, "short": target[:12]}

    def revert_to(self, ref: str, message: str = "", *, author: str = "",
                  session_id: str = "") -> dict:
        """Новый коммит с деревом указанного коммита (откат состояния)."""
        with self._lock:
            target = self.read_commit(ref)
            manifest = self.tree_manifest(target["tree"])
            text = str(message or "").strip()
            if not text:
                first = str(target.get("message", "")).splitlines()
                text = "Откат к %s%s" % (
                    target["id"][:12], (": " + first[0]) if first else "")
            files = {path: self.get_blob(obj_hash)
                     for path, obj_hash in manifest.items()}
            return self.commit(text, files, author=author,
                               session_id=session_id)

    def reflog(self, limit: int = 50) -> list:
        rows = []
        try:
            with open(self.reflog_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return []
        except OSError:
            return []
        return rows[-max(1, min(int(limit or 50), MAX_LOG)):][::-1]


def repo_at(root) -> Repository:
    return Repository(root)


def repo_for(profile_id: str) -> Repository:
    """Хранилище профиля: ``<профиль>/data/vcs``, без глобального активного."""
    pid = str(profile_id or "").strip()
    if not pid:
        raise VcsError("Не указан профиль", 400, "profile_required")
    import profiles_module as PM
    base = PM.get_profile_data_dir(pid)
    if not base:
        raise VcsError("Профиль не найден: %s" % pid, 404, "profile_not_found")
    return Repository(Path(base) / "vcs")
