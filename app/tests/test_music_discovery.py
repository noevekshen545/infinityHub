"""Обнаружение музыкальных папок: покрытие без потерь и без пересечений.

Причина набора проверок: обход дисков отвечает на вопрос «где музыка», и у него
два одинаково плохих провала — предложить ту же коллекцию дважды (индексация
пересчитает треки, в библиотеке поедут дубли) и не предложить часть треков
(тогда пользователь увидит «найдено 2400, в библиотеке 100»). Отдельный класс
ошибок: минуты работы обязаны отменяться, а уснувший сетевой диск или недоступная
папка не должны вешать или обрывать задачу.
"""
import os
import sys
import threading
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import music_discovery as md  # noqa: E402


def mk(root, folder, n, ext=".mp3"):
    path = os.path.join(root, *folder.split("/"))
    os.makedirs(path, exist_ok=True)
    for i in range(n):
        with open(os.path.join(path, "t%03d%s" % (i, ext)), "wb") as handle:
            handle.write(b"x" * 256)
    return path


def find(root, **kw):
    kw.setdefault("max_depth", 5)
    kw.setdefault("min_files", 10)
    return md.discover_music(roots=[root], **kw)


def names(res, root):
    return sorted(os.path.relpath(d["path"], root).replace(os.sep, "/")
                  for d in res["dirs"])


def test_collection_offered_once_from_the_top(tmp_path):
    """Коллекция = одна запись, а не диск + Music + каждый альбом."""
    root = str(tmp_path)
    mk(root, "Music/Rock/AlbumA", 12)
    mk(root, "Music/Rock/AlbumB", 9)
    mk(root, "Music/Jazz/Box", 30)
    res = find(root)
    assert names(res, root) == ["Music"]
    assert res["dirs"][0]["files"] == 51


def test_nothing_above_threshold_is_lost(tmp_path):
    root = str(tmp_path)
    mk(root, "Lossless", 11, ext=".flac")
    mk(root, "Flat", 15)
    mk(root, "Sound", 3)
    res = find(root)
    assert names(res, root) == ["Flat", "Lossless"]
    assert sum(d["files"] for d in res["dirs"]) == 26


def test_wrapper_folders_descend_instead_of_being_offered(tmp_path):
    root = str(tmp_path)
    mk(root, "profile/Music", 20)
    mk(root, "profile/Downloads/музыка", 12)
    assert names(find(root), root) == ["profile/Downloads/музыка", "profile/Music"]


def test_container_with_scattered_music_is_still_offered(tmp_path):
    """Обёртка не предлагается, пока внутри есть что предложить; если внутри
    порог не взят ни одной папкой — предлагается обёртка, иначе треки пропали бы."""
    root = str(tmp_path)
    mk(root, "profile/Music", 3)
    mk(root, "profile/misc/a", 3)
    mk(root, "profile/misc/b", 3)
    mk(root, "profile/misc/c", 3)
    res = find(root)
    assert names(res, root) == ["profile"]
    assert res["dirs"][0]["files"] == 12


def test_system_and_dependency_trees_are_not_walked(tmp_path):
    root = str(tmp_path)
    mk(root, "Windows/Music", 40)
    mk(root, "proj/node_modules/pkg", 50)
    mk(root, "AppData/Local/Music", 40)
    mk(root, "Real/Album", 12)
    assert names(find(root), root) == ["Real"]


def test_depth_budget_stops_the_descent(tmp_path):
    root = str(tmp_path)
    mk(root, "deep/a/b/c/Album", 25)
    assert find(root, max_depth=5)["dirs"] == []
    assert names(find(root, max_depth=6), root) == ["deep"]


def test_time_and_dir_budgets_report_why_they_stopped(tmp_path):
    root = str(tmp_path)
    mk(root, "Music/Rock/AlbumA", 12)
    assert "лимит времени" in find(root, time_budget=-1.0)["stopped"]
    res = find(root, max_dirs=2)
    assert "лимит каталогов" in res["stopped"]


def test_should_stop_is_honoured_and_progress_is_reported(tmp_path):
    root = str(tmp_path)
    mk(root, "Music/Rock/AlbumA", 12)
    visits = []
    res = md.discover_music(roots=[root], min_files=10, max_depth=5,
                            should_stop=lambda: len(visits) >= 3,
                            on_visit=lambda v, f: visits.append((v, f)))
    assert [v for v, _ in visits] == [1, 2, 3]
    assert res["stopped"] == "остановлено пользователем"


def test_unreadable_directory_does_not_abort_the_walk(tmp_path, monkeypatch):
    root = str(tmp_path)
    mk(root, "Music", 12)
    broken = os.path.join(root, "inaccessible")
    os.makedirs(broken, exist_ok=True)
    real_scandir = os.scandir

    def scandir(path):
        if os.path.normcase(path) == os.path.normcase(broken):
            raise PermissionError(13, "отказано в доступе", path)
        return real_scandir(path)

    monkeypatch.setattr(md.os, "scandir", scandir)
    assert names(find(root), root) == ["Music"]


def test_junction_detection_uses_reparse_attributes(monkeypatch, tmp_path):
    class Reparse:
        st_reparse_tag = 0
        st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

    monkeypatch.setattr(md.os, "lstat", lambda path: Reparse())
    assert md._is_link(str(tmp_path)) is True

    class Plain(Reparse):
        st_file_attributes = 0x10

    monkeypatch.setattr(md.os, "lstat", lambda path: Plain())
    monkeypatch.setattr(md.os.path, "islink", lambda path: False)
    assert md._is_link(str(tmp_path)) is False

    def boom(path):
        raise OSError(22, "lstat не удалась")

    monkeypatch.setattr(md.os, "lstat", boom)
    assert md._is_link(str(tmp_path)) is False


def test_drive_root_is_never_offered(tmp_path):
    root = str(tmp_path)
    mk(root, "x", 30)
    assert names(find(root), root) == ["x"]


def test_drive_letters_use_mounted_volumes_and_drop_slow_ones(monkeypatch):
    volumes = [("C:\\", md.DRIVE_FIXED), ("N:\\", md.DRIVE_REMOTE),
               ("Z:\\", md.DRIVE_FIXED)]
    monkeypatch.setattr(md, "_windows_volumes", lambda: volumes)

    def isdir(path):
        if path == "Z:\\":
            time.sleep(1.2)  # уснувший USB: дольше порога отзывчивости
        return path in ("C:\\", "N:\\", "Z:\\")

    monkeypatch.setattr(md.os.path, "isdir", isdir)
    assert md.drive_letters() == ["C:\\"]
    assert md.drive_letters(include_remote=True) == ["C:\\", "N:\\"]


def test_drive_letters_fall_back_to_letter_probe(monkeypatch):
    monkeypatch.setattr(md, "_windows_volumes", lambda: None)
    monkeypatch.setattr(md.os.path, "isdir", lambda path: path == "C:\\")
    assert md.drive_letters() == ["C:\\"]


def test_discovery_job_reports_result(tmp_path):
    root = str(tmp_path)
    mk(root, "Music/Rock/AlbumA", 12)
    job = md.DiscoveryJob()
    assert job.start(roots=[root], min_files=10) is True
    for _ in range(200):
        if not job.active:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("задача не завершилась за 4 с")
    snap = job.snapshot()
    assert snap["active"] is False and snap["error"] is None
    assert snap["result"]["dirs"][0]["path"].endswith("Music")
    assert "Найдено папок" in snap["progress"]["message"]


def test_discovery_job_refuses_concurrent_run_and_survives_stop(monkeypatch):
    job = md.DiscoveryJob()
    started = threading.Event()

    def fake(**kwargs):
        started.set()
        while not kwargs["should_stop"]():
            time.sleep(0.02)
        return {"dirs": [], "visited": 7, "entries": 7,
                "stopped": "остановлено пользователем", "elapsed_ms": 1, "roots": []}

    monkeypatch.setattr(md, "discover_music", fake)
    assert job.start(roots=["D:/"]) is True
    assert started.wait(5) is True
    assert job.start(roots=["E:/"]) is False, "двойной обход даёт два разных ответа"
    assert job.snapshot()["active"] is True
    job.stop()
    for _ in range(250):
        if not job.active:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("остановка не подействовала")
    snap = job.snapshot()
    assert snap["active"] is False
    assert "прерван" in snap["progress"]["message"]


def test_discovery_job_reports_error_instead_of_traceback(monkeypatch):
    job = md.DiscoveryJob()

    def boom(**kwargs):
        raise RuntimeError("диск отвалился")

    monkeypatch.setattr(md, "discover_music", boom)
    job.start(roots=["Z:/"])
    for _ in range(250):
        if not job.active:
            break
        time.sleep(0.02)
    snap = job.snapshot()
    assert snap["error"] == "RuntimeError: диск отвалился"
    assert snap["result"] is None
    assert "Ошибка" in snap["progress"]["message"]


def test_audio_extension_set_covers_lossless_and_web(tmp_path):
    root = str(tmp_path)
    for i, ext in enumerate([".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac",
                             ".wma", ".opus", ".ape"]):
        mk(root, "AllFormats", 1, ext=ext)
    res = find(root, min_files=9)
    assert res["dirs"][0]["files"] == 9
    assert not md._is_audio("cover.jpg")
    assert md._is_audio("TRACK.FLAC")
