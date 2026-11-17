#!/usr/bin/env python3
"""
site_downloader — рекурсивное скачивание сайта без внешнего wget.

Зачем он нужен: wget_module исторически запускал системный `wget`.
В переносимой сборке Windows такого бинарника нет, и кнопка
«Скачать сайт» в модуле HTML отдавала
`[WinError 2] Не удаётся найти указанный файл`.

Этот модуль решает ту же задачу на чистом Python (requests + BeautifulSoup):
  * обходит страницу, собирает ссылки (те же опции depth/no_parent/domains);
  * скачивает HTML и ресурсы страницы (img/css/js) рядом с ними;
  * переписывает ссылки в скачанных HTML на локальные (convert_links);
  * отдаёт прогресс и журнал в том же формате, который ждёт wget_module.
"""

import os
import re
import time
from urllib.parse import urljoin, urlparse, urldefrag
import swallow

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

RESOURCE_EXT = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.bmp', '.avif',
    '.css', '.js', '.mjs', '.json', '.woff', '.woff2', '.ttf', '.otf', '.eot',
    '.mp4', '.webm', '.mp3', '.ogg', '.wav', '.pdf',
}
HTML_EXT = {'.html', '.htm', '.xhtml'}

_SKIP_SCHEMES = ('mailto:', 'javascript:', 'data:', 'tel:', 'about:', 'ftp:')


def _norm(url):
    """Убрать якорь и обрезать пробелы."""
    url, _frag = urldefrag((url or '').strip())
    return url


def _same_site(url, root_host):
    host = (urlparse(url).hostname or '').lower()
    root = (root_host or '').lower()
    if not host or not root:
        return False
    return host == root or host.endswith('.' + root)


def _local_name(url, is_html=False):
    """Имя файла на диске из URL (без query/fragment), с защитой от пустых имён."""
    path = urlparse(url).path
    name = os.path.basename(path) or ''
    name = re.sub(r'[<>:"/\\|?*]+', '_', name)
    if not name:
        name = 'index.html' if is_html else 'file'
    if is_html and not os.path.splitext(name)[1]:
        name += '.html'
    return name


def download_site(url, output_dir, options=None, progress_cb=None, log_cb=None,
                  should_stop=None):
    """Скачать сайт рекурсивно. progress_cb(files, total_size, ...)."""
    import requests
    from bs4 import BeautifulSoup

    options = options or {}
    depth = int(options.get('depth', 5) or 5)
    no_parent = bool(options.get('no_parent', True))
    convert_links = bool(options.get('convert_links', True))
    page_requisites = bool(options.get('page_requisites', True))
    domains = options.get('domains') or ''
    extensions = options.get('extensions') or ''
    timeout = int(options.get('timeout', 20) or 20)
    tries = int(options.get('tries', 3) or 3)
    limit_rate = options.get('limit_rate') or ''
    ua = options.get('user_agent') or DEFAULT_UA

    headers = {
        'User-Agent': ua,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
    }

    root_host = urlparse(url).hostname or ''
    allow_hosts = {root_host}
    for d in re.split(r'[,\s]+', str(domains)):
        d = d.strip().lower()
        if d:
            allow_hosts.add(d)
    accept_exts = {e.lower() if e.startswith('.') else '.' + e.lower()
                   for e in re.split(r'[,\s]+', str(extensions)) if e.strip()}

    # ограничение скорости -> пауза между запросами
    delay = 0.0
    m = re.match(r'^(\d+)([kKmM])$', str(limit_rate))
    if m:
        n = int(m.group(1)) * (1024 if m.group(2).lower() == 'k' else 1024 * 1024)
        delay = min(2.0, max(0.05, 256 * 1024 / n))

    session = requests.Session()
    session.headers.update(headers)

    os.makedirs(output_dir, exist_ok=True)
    host_dir = os.path.join(output_dir, (root_host or 'site').replace(':', '_'))
    os.makedirs(host_dir, exist_ok=True)

    queue = [(_norm(url), 0)]
    seen = set()
    mapping = {}          # url -> локальный относительный путь
    files = 0
    total_size = 0
    errors = 0
    # Предохранители: без них крупный сайт (например python.org) обходился
    # десятки минут — очередь ссылок росла быстрее, чем файлы сохранялись.
    max_files = int(options.get('max_files', 300) or 300)
    max_pages = int(options.get('max_pages', 60) or 60)
    max_seconds = float(options.get('max_seconds', 600) or 600)
    t0 = time.time()
    pages_done = 0
    stopped_reason = ''

    def _log(msg):
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                swallow.note('site_downloader._log:L128')
                pass

    def _fetch(u):
        last = None
        for _ in range(max(1, tries)):
            if should_stop and should_stop():
                return None
            try:
                r = session.get(u, timeout=timeout, allow_redirects=True)
                if r.status_code == 200:
                    return r
                last = f"HTTP {r.status_code}"
            except Exception as e:
                swallow.note('site_downloader._fetch:L142', e)
                last = str(e)
            time.sleep(0.4)
        return None





    while queue:
        if should_stop and should_stop():
            _log('Остановлено пользователем')
            stopped_reason = 'cancelled'
            break
        if files >= max_files:
            _log(f'Достигнут лимит файлов ({max_files}) — остановлено')
            stopped_reason = 'max_files'
            break
        if pages_done >= max_pages:
            _log(f'Достигнут лимит страниц ({max_pages}) — остановлено')
            stopped_reason = 'max_pages'
            break
        if time.time() - t0 > max_seconds:
            _log(f'Достигнут лимит времени ({int(max_seconds)} с) — остановлено')
            stopped_reason = 'max_seconds'
            break
        cur, level = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)

        host_now = (urlparse(cur).hostname or '').lower()
        if host_now not in allow_hosts and not _same_site(cur, root_host):
            continue

        ext = os.path.splitext(urlparse(cur).path)[1].lower()
        is_html = ext in HTML_EXT or not ext

        if not is_html and not page_requisites:
            continue

        resp = _fetch(cur)
        if resp is None:
            errors += 1
            _log(f'Ошибка загрузки: {cur}')
            continue

        content = resp.content
        ctype = (resp.headers.get('content-type') or '').lower()
        if not is_html and 'html' not in ctype:
            # ресурс страницы — сохраняем как есть
            rel = os.path.join(host_dir, _local_name(cur))
            os.makedirs(os.path.dirname(rel), exist_ok=True)
            with open(rel, 'wb') as f:
                f.write(content)
            files += 1
            total_size += len(content)
            mapping[cur] = os.path.relpath(rel, output_dir).replace('\\', '/')
            if progress_cb:
                progress_cb(files, total_size)
            continue

        try:
            resp.encoding = resp.encoding or resp.apparent_encoding or 'utf-8'
        except Exception:
            swallow.note('site_downloader.download_site:L207')
            resp.encoding = 'utf-8'
        text = resp.text

        try:
            soup = BeautifulSoup(text, 'lxml')
        except Exception:
            swallow.note('site_downloader.download_site:L214')
            soup = BeautifulSoup(text, 'html.parser')

        # 1) ресурсы страницы (img/css/js) — рядом с HTML
        if page_requisites:
            for tag, attr in (('img', 'src'), ('script', 'src'),
                              ('link', 'href'), ('source', 'src'),
                              ('video', 'src'), ('audio', 'src')):
                for el in soup.find_all(tag):
                    src = el.get(attr)
                    if not src or src.startswith(_SKIP_SCHEMES):
                        continue
                    abs_u = _norm(urljoin(cur, src))
                    if not abs_u or abs_u in mapping or abs_u in seen:
                        continue
                    rext = os.path.splitext(urlparse(abs_u).path)[1].lower()
                    if rext and rext not in RESOURCE_EXT:
                        continue
                    rresp = _fetch(abs_u)
                    if rresp is None:
                        continue
                    rel = os.path.join(host_dir, _local_name(abs_u))
                    os.makedirs(os.path.dirname(rel), exist_ok=True)
                    with open(rel, 'wb') as f:
                        f.write(rresp.content)
                    files += 1
                    total_size += len(rresp.content)
                    seen.add(abs_u)
                    mapping[abs_u] = os.path.relpath(rel, output_dir).replace('\\', '/')
                    if convert_links:
                        el[attr] = mapping[abs_u]

        # 2) ссылки для обхода
        if level < depth:
            for a in soup.find_all('a'):
                href = a.get('href')
                if not href or href.startswith(_SKIP_SCHEMES):
                    continue
                abs_u = _norm(urljoin(cur, href))
                if not abs_u or abs_u in seen:
                    continue
                p = urlparse(abs_u)
                if p.scheme not in ('http', 'https'):
                    continue
                if (p.hostname or '').lower() not in allow_hosts \
                        and not _same_site(abs_u, host_now):
                    continue
                # --no-parent: не уходим выше исходного URL
                if no_parent and level == 0:
                    root_path = urlparse(_norm(url)).path or '/'
                    if not (p.path == root_path or p.path.startswith(root_path)):
                        continue
                aext = os.path.splitext(p.path)[1].lower()
                if accept_exts and aext and aext not in accept_exts:
                    continue
                queue.append((abs_u, level + 1))

        # 3) сохраняем HTML (после переписывания ссылок на локальные)
        if convert_links:
            for a in soup.find_all('a'):
                href = a.get('href')
                if not href:
                    continue
                abs_u = _norm(urljoin(cur, href))
                if abs_u in mapping:
                    a['href'] = mapping[abs_u]
        rel = os.path.join(host_dir, _local_name(cur, is_html=True))
        os.makedirs(os.path.dirname(rel), exist_ok=True)
        html_out = str(soup)
        with open(rel, 'w', encoding='utf-8') as f:
            f.write(html_out)
        size = len(html_out.encode('utf-8'))
        files += 1
        pages_done += 1
        total_size += size
        mapping[cur] = os.path.relpath(rel, output_dir).replace('\\', '/')
        if progress_cb:
            progress_cb(files, total_size)
        if delay:
            time.sleep(delay)

    _log(f'Готово: файлов {files}, страниц {pages_done}, размер {total_size // 1024} КБ'
         + (f', ошибок {errors}' if errors else '')
         + (f', остановлено: {stopped_reason}' if stopped_reason else ''))
    return {
        'ok': files > 0,
        'files': files,
        'pages': pages_done,
        'total_size': total_size,
        'errors': errors,
        'stopped': stopped_reason,
        'mapping': mapping,
    }
