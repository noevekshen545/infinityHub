"""Память чистки битого: отпечатки файлов, которые уже признаны битыми.

Чистка убирает строку индекса, но сам файл остаётся на диске (решение
владельца). Без памяти о приговоре индексатор через десять минут снова находит
файл в каталоге и добавляет строку — так битые песни «возвращались» после
каждого рестарта и каждого прогона автосинхронизации.

Отпечаток — путь + размер + время изменения. Совпал — файл пропускаем и не
тратим на него разборчик; не совпал (файл докачали, заменили, починили теги) —
индексируем заново и запись памяти снимаем. Таблица живёт в той же базе, что и
строки индекса: media.db для музыки и видео, photos.db для фото.
"""

import os

import swallow

TABLE = "autoclean_ignored"

DDL = (
    "CREATE TABLE IF NOT EXISTS autoclean_ignored ("
    " filepath TEXT PRIMARY KEY,"
    " cat TEXT NOT NULL,"
    " size INTEGER NOT NULL,"
    " mtime REAL NOT NULL,"
    " reason TEXT,"
    " added_at TEXT DEFAULT CURRENT_TIMESTAMP)"
)


def _stat(fp):
    try:
        st = os.stat(fp)
        return int(st.st_size), float(st.st_mtime)
    except OSError:
        return None


def ensure(conn):
    conn.execute(DDL)


def remember(conn, cat, items):
    """Запомнить приговоры. items = [(filepath, reason)]; вернуть их число.

    Файла нет — отпечатка нет: отсутствующий файл индексатор и так не найдёт, а
    блокировать путь на будущее нельзя. Диск могли просто отключить, и файл
    вернётся вместе с ним.
    """
    try:
        ensure(conn)
    except Exception:
        swallow.note('media_ignore.remember:L52')
        return 0
    saved = 0
    for fp, reason in items:
        st = _stat(fp)
        if st is None:
            continue
        try:
            conn.execute(
                "INSERT OR REPLACE INTO autoclean_ignored"
                " (filepath, cat, size, mtime, reason) VALUES (?, ?, ?, ?, ?)",
                (fp, cat, st[0], st[1], str(reason or "")))
            saved += 1
        except Exception:
            swallow.note('media_ignore.remember:L66')
    return saved


def snapshot(conn, cat):
    """{filepath: (size, mtime)} — снимок памяти для одного прохода."""
    try:
        ensure(conn)
        rows = conn.execute(
            "SELECT filepath, size, mtime FROM autoclean_ignored WHERE cat=?",
            (cat,)).fetchall()
        return {r[0]: (int(r[1]), float(r[2])) for r in rows}
    except Exception:
        swallow.note('media_ignore.snapshot:L80')
        return {}


def _same(entry, size, mtime):
    try:
        return int(size) == entry[0] and abs(float(mtime) - entry[1]) < 0.001
    except (TypeError, ValueError):
        return False


def blocked(memo, fp):
    """Файл под запретом, если отпечаток на диске совпал с запомненным.

    Отсутствующий файл тоже считается закрытым: индексировать нечего, а запись
    не стираем — диск могли отключить и вернуть вместе с файлом.
    """
    entry = memo.get(fp)
    if entry is None:
        return False
    st = _stat(fp)
    if st is None:
        return True
    return _same(entry, st[0], st[1])


def blocked_stat(memo, fp, size, mtime):
    """То же для вызывающего, у которого stat уже есть (скан фото)."""
    entry = memo.get(fp)
    if entry is None:
        return False
    return _same(entry, size, mtime)


def forget(conn, filepath):
    try:
        conn.execute("DELETE FROM autoclean_ignored WHERE filepath=?", (filepath,))
    except Exception:
        swallow.note('media_ignore.forget:L117')


def _norm(path):
    return str(path or "").replace("\\", "/").rstrip("/").lower()


def count(conn, cat, prefixes=None):
    """Сколько приговоров в памяти. prefixes — видимая профилю зона."""
    try:
        ensure(conn)
        if not prefixes:
            row = conn.execute(
                "SELECT COUNT(*) FROM autoclean_ignored WHERE cat=?", (cat,)).fetchone()
        else:
            zones = [p for p in (_norm(x) for x in prefixes) if p]
            rows = conn.execute(
                "SELECT filepath FROM autoclean_ignored WHERE cat=?", (cat,)).fetchall()
            return sum(1 for r in rows if _in_zones(r[0], zones))
        return int(row[0] if row else 0)
    except Exception:
        swallow.note('media_ignore.count:L134')
        return 0


def _in_zones(filepath, zones):
    fp = _norm(filepath)
    return any(fp == z or fp.startswith(z + "/") for z in zones)


def clear(conn, cat, prefixes=None):
    """Забыть приговоры категории (в зоне профиля, если задана)."""
    try:
        ensure(conn)
        if not prefixes:
            cur = conn.execute("DELETE FROM autoclean_ignored WHERE cat=?", (cat,))
            return int(cur.rowcount or 0)
        zones = [p for p in (_norm(x) for x in prefixes) if p]
        rows = conn.execute(
            "SELECT filepath FROM autoclean_ignored WHERE cat=?", (cat,)).fetchall()
        doomed = [r[0] for r in rows if _in_zones(r[0], zones)]
        for i in range(0, len(doomed), 200):
            batch = doomed[i:i + 200]
            conn.execute(
                "DELETE FROM autoclean_ignored WHERE filepath IN (%s)"
                % ",".join("?" for _ in batch), batch)
        return len(doomed)
    except Exception:
        swallow.note('media_ignore.clear:L155')
        return 0


def skip_paths(conn, cat, paths):
    """Убрать из списка файлы с уже осуждённым отпечатком.

    Заодно снимает устаревшие записи: файл изменился — значит, он снова
    обычный и должен быть проиндексирован.
    """
    memo = snapshot(conn, cat)
    if not memo:
        return list(paths)
    out = []
    stale = []
    for fp in paths:
        entry = memo.get(fp)
        if entry is None:
            out.append(fp)
            continue
        st = _stat(fp)
        if st is None:
            continue
        if _same(entry, st[0], st[1]):
            continue
        stale.append(fp)
        out.append(fp)
    if stale:
        try:
            for i in range(0, len(stale), 200):
                batch = stale[i:i + 200]
                conn.execute(
                    "DELETE FROM autoclean_ignored WHERE filepath IN (%s)"
                    % ",".join("?" for _ in batch), batch)
            conn.commit()
        except Exception:
            swallow.note('media_ignore.skip_paths:L190')
    return out
