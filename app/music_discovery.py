"""Автообнаружение музыкальных папок на дисках машины.

Модуль только читает файловую систему: ничего не сохраняет и не индексирует.
Список найденных папок отдаётся наружу, а добавление в профиль и запуск
индексации остаются за существующей ручкой /api/media/rebuild-music — там уже
решено, что каталог пишется в private_music_dirs профиля и индексируется
эффективный список (свои + общедоступные).

Зачем отдельный модуль: обход всех дисков — это минуты работы и десятки тысяч
каталогов, поэтому он обязан быть отменяемым, с прогрессом и с жёсткими
потолками, а не «функцией внутри медиа-модуля, которая повесит запрос».
"""

from __future__ import annotations

import os
import string
import threading
import time
from typing import Dict, Iterable, List, Optional
import swallow

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".wma", ".opus", ".ape"}

# Папкой с музыкой считается каталог, где не меньше этого числа треков. Ниже —
# и «звуки Windows», и папки с одним демо-файлом.
MIN_FILES_DEFAULT = 10

# Потолки: без них обход «забытого» сетевого диска или дерева из сотен тысяч
# каталогов превращается в незавершаемую задачу.
MAX_DEPTH_DEFAULT = 6
MAX_DIRS_DEFAULT = 40000
TIME_BUDGET_DEFAULT = 180.0

SKIP_NAMES = {
    "$recycle.bin", "system volume information", "windows", "winnt", "recovery",
    "perflogs", "boot", "efi", "programdata", "node_modules", "__pycache__",
    ".git", ".venv", "venv", "site-packages", "build", "target",
    "appdata", "temp", "tmp", "inetpub", "bootrec", "windows.old",
    "softwaredistribution", "driverstore", "winstore", "assembly",
}
SKIP_PREFIXES = ("program files", "common files", "$", "dotnet")


DRIVE_FIXED, DRIVE_REMOVABLE, DRIVE_REMOTE, DRIVE_CDROM = 3, 2, 4, 5


def _windows_volumes() -> Optional[List[tuple]]:
    """Буквы реально смонтированных томов и их типы, без чтения содержимого.

    Перебирать все 26 букв нельзя: проверка isdir на «спящем» сетевом диске
    блокируется десятками секунд, и обход стартует с многоминутной паузы.
    """
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        swallow.note('music_discovery._windows_volumes:L57')
        return None
    volumes = []
    for index, letter in enumerate(string.ascii_uppercase):
        if not (mask >> index) & 1:
            continue
        root = letter + ":\\"
        try:
            kind = ctypes.windll.kernel32.GetDriveTypeW(root)
        except Exception:
            swallow.note('music_discovery._windows_volumes:L67')
            kind = 0
        volumes.append((root, kind))
    return volumes


def drive_letters(include_remote: bool = False) -> List[str]:
    """Диски, которые стоит обойти.

    Сетевые шары по умолчанию исключены: они и чужие, и висящие. Остальные
    всё равно проверяются на отзывчивость — уснувший USB-диск не должен
    съесть весь бюджет времени на первом же каталоге.
    """
    kinds = {DRIVE_FIXED, DRIVE_REMOVABLE, DRIVE_CDROM}
    if include_remote:
        kinds.add(DRIVE_REMOTE)
    volumes = _windows_volumes()
    if volumes is None:
        candidates = [letter + ":\\" for letter in string.ascii_uppercase]
    else:
        candidates = [root for root, kind in volumes if kind in kinds]
    found = []
    for root in candidates:
        t0 = time.time()
        try:
            ok = os.path.isdir(root)
        except OSError:
            swallow.note('music_discovery.drive_letters:L94')
            continue
        if ok and time.time() - t0 <= 1.0:
            found.append(root)
    return found


def _is_skipped(name: str) -> bool:
    low = name.lower()
    if low in SKIP_NAMES:
        return True
    return any(low.startswith(p) for p in SKIP_PREFIXES)


def _is_link(path: str) -> bool:
    """Junction/symlink: на Windows это основной способ зациклить обход."""
    try:
        st = os.lstat(path)
    except OSError:
        swallow.note('music_discovery._is_link:L113')
        return False
    if getattr(st, "st_reparse_tag", 0) or bool(getattr(st, "st_file_attributes", 0) & 0x400):
        return True
    try:
        return os.path.islink(path)
    except OSError:
        swallow.note('music_discovery._is_link:L120')
        return False


def _is_audio(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS


# Имена, которые почти всегда обёртка, а не музыкальная папка: предлагать их
# нельзя, надо идти внутрь. А имя ребёнка-обёртки — сигнал, что настоящая
# коллекция лежит на уровень ниже.
MUSIC_HINT_NAMES = {
    "music", "музыка", "musics", "songs", "песни", "audio", "аудио", "mp3",
    "flac", "ost", "soundtrack", "soundtracks", "albums", "альбомы",
}
CONTAINER_NAMES = {"users", "home", "root", "public", "profile", "profiles"}


def _basename(path: str) -> str:
    return os.path.basename(path.rstrip("\\/")) or path


def _is_container(path: str, children: Iterable[str]) -> bool:
    if _basename(path).lower() in CONTAINER_NAMES:
        return True
    return any(_basename(c).lower() in MUSIC_HINT_NAMES for c in children)


def discover_music(
    roots: Optional[Iterable[str]] = None,
    min_files: int = MIN_FILES_DEFAULT,
    max_depth: int = MAX_DEPTH_DEFAULT,
    max_dirs: int = MAX_DIRS_DEFAULT,
    time_budget: float = TIME_BUDGET_DEFAULT,
    should_stop: Optional[callable] = None,
    on_visit: Optional[callable] = None,
) -> Dict:
    """Найти папки с музыкой.

    Два прохода. Первый считает поддеревья: сколько аудиофайлов и байт под каждой
    папкой. Второй решает сверху вниз: первая папка (не корень диска и не
    обёртка вроде C:\\Users), в поддереве которой набрался порог, предлагается и
    туда больше не идём. Так от коллекции возвращается одна запись «Music»,
    пересечений в списке нет, и ни один трек не теряется: если внутри обёртки
    порог не взял ни один ребёнок, обёртка предлагается сама.

    Возврат: {"dirs": [{path, files, size_mb}], "visited": N, "entries": N,
    "stopped": None|reason, "elapsed_ms": N, "roots": [...]}.
    """
    start = time.time()
    root_list = [os.path.abspath(r) for r in (list(roots) if roots else drive_letters())]
    results: List[Dict] = []
    state = {"visited": 0, "entries": 0, "stopped": None}
    info: Dict[str, tuple] = {}

    def over_limit() -> bool:
        if state["stopped"] is not None:
            return True
        if state["visited"] >= max_dirs:
            state["stopped"] = "исчерпан лимит каталогов (%d)" % max_dirs
            return True
        if time.time() - start > time_budget:
            state["stopped"] = "исчерпан лимит времени (%.0f с)" % time_budget
            return True
        if should_stop is not None and should_stop():
            state["stopped"] = "остановлено пользователем"
            return True
        return False

    def collect(path: str, depth: int):
        """Первый проход: (треков_в_поддереве, байты_аудио_в_поддереве)."""
        if over_limit():
            return 0, 0
        state["visited"] += 1
        if on_visit is not None:
            on_visit(state["visited"], len(results))

        files = 0
        size = 0
        children: List[str] = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    state["entries"] += 1
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                    except OSError:
                        swallow.note('music_discovery.collect:L207')
                        continue
                    if is_dir:
                        if depth + 1 < max_depth and not _is_skipped(e.name) and not _is_link(e.path):
                            children.append(e.path)
                    elif _is_audio(e.name):
                        files += 1
                        try:
                            size += e.stat(follow_symlinks=False).st_size
                        except OSError:
                            swallow.note('music_discovery.collect:L217')
                            pass
        except OSError:
            swallow.note('music_discovery.collect:L220')
            info[path] = (files, size, ())
            return files, size

        for child in sorted(children, key=str.lower):
            sub_files, sub_size = collect(child, depth + 1)
            files += sub_files
            size += sub_size

        info[path] = (files, size, tuple(children))
        return files, size

    def offer(path: str, files: int, size: int) -> None:
        results.append({"path": path, "files": files,
                        "size_mb": int(size // (1024 * 1024))})

    def decide(path: str, depth: int) -> bool:
        """Второй проход: предложено ли что-то в поддереве этой папки."""
        files, size, children = info.get(path, (0, 0, ()))
        if depth >= 1 and files >= min_files and not _is_container(path, children):
            offer(path, files, size)
            return True

        found = False
        for child in sorted(children, key=str.lower):
            if decide(child, depth + 1):
                found = True
        if depth >= 1 and not found and files >= min_files:
            # порога хватило, но внутрь не спустилась ни одна запись: иначе
            # эти треки просто пропали бы из ответа
            offer(path, files, size)
            return True
        return found

    for root in root_list:
        if state["stopped"] is not None:
            break
        collect(root, 0)
        decide(root, 0)

    results.sort(key=lambda r: -r["files"])
    return {
        "dirs": results,
        "visited": state["visited"],
        "entries": state["entries"],
        "stopped": state["stopped"],
        "elapsed_ms": int((time.time() - start) * 1000),
        "roots": root_list,
    }


class DiscoveryJob:
    """Фоновая задача обнаружения: запуск, прогресс, отмена, результат.

    Вторая задача во время активной не запускается: два параллельных обхода
    удваивают нагрузку на диски и дают два разных ответа.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = False
        self.active = False
        self.progress = {"visited": 0, "found": 0, "message": "Ещё не запускалось"}
        self.result: Optional[Dict] = None
        self.error: Optional[str] = None

    def start(self, roots=None, min_files: int = MIN_FILES_DEFAULT) -> bool:
        with self._lock:
            if self.active:
                return False
            self.active = True
            self._stop = False
            self.result = None
            self.error = None
            self.progress = {"visited": 0, "found": 0, "message": "Обход дисков…"}
            threading.Thread(target=self._run, daemon=True,
                             kwargs={"roots": roots, "min_files": min_files}).start()
            return True

    def stop(self) -> None:
        self._stop = True

    def _on_visit(self, visited: int, found: int) -> None:
        self.progress = {"visited": visited, "found": found,
                         "message": "Просмотрено каталогов: %d, найдено папок: %d" % (visited, found)}

    def _run(self, roots, min_files) -> None:
        try:
            res = discover_music(roots=roots, min_files=min_files,
                                 should_stop=lambda: self._stop,
                                 on_visit=self._on_visit)
            self.result = res
            tail = (", обход прерван: " + res["stopped"]) if res["stopped"] else ""
            self.progress = {"visited": res["visited"], "found": len(res["dirs"]),
                             "message": "Найдено папок: %d%s" % (len(res["dirs"]), tail)}
        except Exception as exc:  # задача фоновая: наружу отдаём текст, а не трейсбек
            swallow.note('music_discovery._run:L316', exc)
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self.progress = {"visited": self.progress.get("visited", 0),
                             "found": 0, "message": "Ошибка: %s" % self.error}
        finally:
            self.active = False

    def snapshot(self) -> Dict:
        return {"active": self.active, "progress": dict(self.progress),
                "result": self.result, "error": self.error}


job = DiscoveryJob()
