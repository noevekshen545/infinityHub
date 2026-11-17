#!/usr/bin/env python3
"""
Загрузчик сайтов для модуля HTML.

Раньше здесь запускался системный `wget`. На Windows такого бинарника
нет, и кнопка «Скачать сайт» падала с
`[WinError 2] Не удаётся найти указанный файл`.

Теперь движок выбирается автоматически:
  * если в системе есть wget — используем его (поведение без изменений);
  * иначе — чистый Python-загрузчик site_downloader с теми же опциями
    и тем же форматом отчёта о прогрессе.
"""
import os
import re
import json
import shutil
import subprocess
import threading
import time
import swallow

DOWNLOADS_DIR = os.path.expanduser("~/html_downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Можно переопределить из сервера
def set_downloads_dir(path):
    global DOWNLOADS_DIR
    # Каталог мог прийти в WSL-виде (/mnt/e/...) — приводим к текущей ОС,
    # иначе загрузки уходили в несуществующий путь.
    try:
        from path_compat import to_native as _to_native
        if path:
            path = _to_native(path)
    except Exception:
        swallow.note('wget_module.set_downloads_dir:L35')
        pass
    DOWNLOADS_DIR = path
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Активные загрузки
_active_downloads = {}
_download_lock = threading.Lock()
# Отменённые задания (кнопка «Стоп»)
_cancelled = set()


def find_wget():
    """Путь к wget в системе или в комплекте (None, если wget нет)."""
    which = shutil.which("wget")
    if which:
        return which
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in (os.path.join("binaries", "wget", "wget.exe"),
                os.path.join("binaries", "wget", "wget")):
        p = os.path.join(here, rel)
        if os.path.isfile(p):
            return p
    return None


def wget_download(url, options=None):
    """
    Запустить wget для скачивания сайта.
    
    options:
      - depth: глубина рекурсии (по умолчанию 5)
      - wait: задержка между запросами (сек)
      - limit_rate: ограничение скорости (например '100k', '1m')
      - exclude: список паттернов для исключения
      - include: список паттернов для включения
      - convert_links: конвертировать ссылки (bool)
      - page_requisites: скачать ресурсы страницы (bool)
      - no_parent: не подниматься выше начального URL (bool)
      - domains: ограничить домены
      - output_dir: директория вывода
      - extensions: расширения файлов
      - timeout: таймаут запроса
      - tries: число попыток
      - user_agent: User-Agent
      - cookies: путь к cookies файлу
    """
    if options is None:
        options = {}

    job_id = str(int(time.time() * 1000))

    # Каталог вывода — нативный путь (config мог прийти в WSL-виде)
    output_dir = options.get('output_dir') or os.path.join(DOWNLOADS_DIR, f"site_{job_id}")
    try:
        from path_compat import to_native as _to_native
        output_dir = _to_native(output_dir)
    except Exception:
        swallow.note('wget_module.wget_download:L93')
        pass
    output_dir = os.path.abspath(output_dir)

    wget_bin = find_wget()

    if not wget_bin:
        # ── Движок №2: чистый Python ( wget в Windows отсутствует ) ──────
        with _download_lock:
            _active_downloads[job_id] = {
                "id": job_id,
                "url": url,
                "status": "running",
                "progress": 0,
                "current_url": url,
                "files_downloaded": 0,
                "total_size": 0,
                "output_dir": output_dir,
                "started_at": time.time(),
                "log": [],
                "engine": "python",
                "cmd": f"python site_downloader {url}"
            }

        def _log(msg):
            with _download_lock:
                job = _active_downloads.get(job_id)
                if job is not None:
                    job["log"].append(str(msg))
                    del job["log"][:-50]

        def _progress(files, size):
            with _download_lock:
                job = _active_downloads.get(job_id)
                if job is not None:
                    job["files_downloaded"] = files
                    job["total_size"] = size
                    # без общего числа файлов показываем « indeterminate »
                    job["progress"] = min(99, files * 5)

        def _stop():
            return job_id in _cancelled

        def run_python():
            try:
                from site_downloader import download_site
                res = download_site(
                    url, output_dir, options=options,
                    progress_cb=_progress, log_cb=_log, should_stop=_stop)
                with _download_lock:
                    job = _active_downloads.get(job_id)
                    if job is not None:
                        stop = res.get("stopped") or ""
                        if not res.get("ok"):
                            job["status"] = "error: не удалось скачать ни одного файла"
                        elif stop == "cancelled":
                            job["status"] = "cancelled"
                        elif stop:
                            # Сработал предохранитель (лимит файлов/страниц/времени)
                            job["status"] = "completed (ограничено: " + stop + ")"
                        else:
                            job["status"] = "completed"
                        job["progress"] = 100
                        job["finished_at"] = time.time()
                        job["files_downloaded"] = res.get("files", 0)
                        job["total_size"] = res.get("total_size", 0)
            except Exception as e:
                swallow.note('wget_module.run_python:L160', e)
                with _download_lock:
                    job = _active_downloads.get(job_id)
                    if job is not None:
                        job["status"] = f"error: {e}"
                        job["progress"] = 100
                        job["finished_at"] = time.time()
            finally:
                _cancelled.discard(job_id)

        threading.Thread(target=run_python, daemon=True,
                         name=f"site-dl-{job_id}").start()
        return {
            "ok": True,
            "job_id": job_id,
            "output_dir": output_dir,
            "cmd": f"python site_downloader {url}",
            "engine": "python"
        }

    cmd = [wget_bin]
    
    # Основные параметры
    cmd.extend(['--recursive', f'--level={options.get("depth", 5)}'])
    cmd.extend(['--page-requisites']) if options.get('page_requisites', True) else None
    cmd.extend(['--convert-links']) if options.get('convert_links', True) else None
    cmd.extend(['--no-parent']) if options.get('no_parent', True) else None
    cmd.extend(['--no-clobber'])
    # Используем --no-verbose чтобы видеть прогресс
    cmd.append('--no-verbose')
    
    # Задержка
    if options.get('wait'):
        cmd.extend(['--wait', str(options['wait'])])
    
    # Ограничение скорости
    if options.get('limit_rate'):
        cmd.extend(['--limit-rate', options['limit_rate']])
    
    # Домены
    if options.get('domains'):
        cmd.extend(['--domains', options['domains']])
    
    # Расширения
    if options.get('extensions'):
        cmd.extend(['--accept', options['extensions']])
    
    # Таймаут и попытки
    if options.get('timeout'):
        cmd.extend(['--timeout', str(options['timeout'])])
    if options.get('tries'):
        cmd.extend(['--tries', str(options['tries'])])
    
    # User-Agent
    if options.get('user_agent'):
        cmd.extend(['--user-agent', options['user_agent']])
    
    # Диретория вывода (уже вычислена выше, в нативном виде)
    cmd.extend(['--directory-prefix', output_dir])
    
    # URL
    cmd.append(url)
    
    with _download_lock:
        _active_downloads[job_id] = {
            "id": job_id,
            "url": url,
            "status": "running",
            "progress": 0,
            "current_url": "",
            "files_downloaded": 0,
            "total_size": 0,
            "output_dir": output_dir,
            "started_at": time.time(),
            "log": [],
            "cmd": ' '.join(cmd)
        }
    
    def run_wget():
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            files_downloaded = 0
            total_size = 0
            
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                
                with _download_lock:
                    if job_id in _active_downloads:
                        _active_downloads[job_id]["log"].append(line)
                        if len(_active_downloads[job_id]["log"]) > 50:
                            _active_downloads[job_id]["log"] = _active_downloads[job_id]["log"][-50:]
                
                # Парсим URL скачивания: "URL: https://example.com/page.html"
                url_match = re.search(r'URL:(\S+)', line)
                if url_match:
                    with _download_lock:
                        if job_id in _active_downloads:
                            _active_downloads[job_id]["current_url"] = url_match.group(1)
                
                # Парсим сохранение файла: "2026-04-05 13:33:52 URL:https://example.com/ [528] -> "file.html" [1]"
                if '->' in line and 'URL:' in line:
                    files_downloaded += 1
                    size_match = re.search(r'\[(\d+)\]', line)
                    if size_match:
                        try:
                            total_size += int(size_match.group(1))
                        except Exception:
                            swallow.note('wget_module.run_wget:L277')
                            pass
                    with _download_lock:
                        if job_id in _active_downloads:
                            _active_downloads[job_id]["files_downloaded"] = files_downloaded
                            _active_downloads[job_id]["total_size"] = total_size
                
                # FINISHED
                if 'FINISHED' in line:
                    with _download_lock:
                        if job_id in _active_downloads:
                            _active_downloads[job_id]["status"] = "completed"
                            _active_downloads[job_id]["progress"] = 100
                            _active_downloads[job_id]["finished_at"] = time.time()
            
            proc.wait()
            
            with _download_lock:
                if job_id in _active_downloads:
                    if proc.returncode == 0:
                        _active_downloads[job_id]["status"] = "completed"
                    else:
                        _active_downloads[job_id]["status"] = f"error: code {proc.returncode}"
                    _active_downloads[job_id]["progress"] = 100
                    _active_downloads[job_id]["finished_at"] = time.time()
                    
        except Exception as e:
            swallow.note('wget_module.run_wget:L304', e)
            with _download_lock:
                if job_id in _active_downloads:
                    _active_downloads[job_id]["status"] = f"error: {str(e)}"
    
    thread = threading.Thread(target=run_wget, daemon=True)
    thread.start()
    
    return {
        "ok": True,
        "job_id": job_id,
        "output_dir": output_dir,
        "cmd": ' '.join(cmd)
    }


def get_download_status(job_id):
    """Получить статус загрузки."""
    with _download_lock:
        job = _active_downloads.get(job_id)
        if not job:
            return {"error": "Загрузка не найдена"}
        
        # Подсчитываем файлы
        output_dir = job.get("output_dir", "")
        total_files = 0
        total_size = 0
        if os.path.isdir(output_dir):
            for root, dirs, files in os.walk(output_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        total_size += os.path.getsize(fp)
                    except Exception:
                        swallow.note('wget_module.get_download_status:L338')
                        pass
                    total_files += 1
        
        job["total_files"] = total_files
        job["total_size"] = total_size
        
        return job


def cancel_download(job_id):
    """Пометить задание как отменённое (оно остановится на следующей проверке)."""
    _cancelled.add(job_id)
    with _download_lock:
        job = _active_downloads.get(job_id)
        if job is not None and job.get("status") == "running":
            job["status"] = "cancelled"
            job["finished_at"] = time.time()
            return {"ok": True, "status": "cancelled"}
    return {"ok": True}


def list_downloads():
    """Список всех загрузок."""
    with _download_lock:
        return {"downloads": list(_active_downloads.values())}


def list_download_folders():
    """Список папок с загруженными сайтами."""
    folders = []
    if not os.path.isdir(DOWNLOADS_DIR):
        return {"folders": []}
    
    for entry in sorted(os.listdir(DOWNLOADS_DIR)):
        fp = os.path.join(DOWNLOADS_DIR, entry)
        if os.path.isdir(fp):
            total_files = 0
            total_size = 0
            html_files = []
            for root, dirs, files in os.walk(fp):
                for f in files:
                    fpath = os.path.join(root, f)
                    try:
                        total_size += os.path.getsize(fpath)
                    except Exception:
                        swallow.note('wget_module.list_download_folders:L384')
                        pass
                    total_files += 1
                    if f.lower().endswith(('.html', '.htm')):
                        rel = os.path.relpath(fpath, fp)
                        html_files.append({"path": rel, "size": os.path.getsize(fpath)})
            
            folders.append({
                "name": entry,
                "path": fp,
                "total_files": total_files,
                "total_size": total_size,
                "html_files": len(html_files),
                "html_list": html_files[:50]  # Первые 50
            })
    
    return {"folders": folders}


def delete_download(folder_name):
    """Удалить загруженный сайт."""
    fp = os.path.join(DOWNLOADS_DIR, folder_name)
    if os.path.isdir(fp):
        import shutil
        shutil.rmtree(fp)
        return {"ok": True}
    return {"error": "Папка не найдена"}
