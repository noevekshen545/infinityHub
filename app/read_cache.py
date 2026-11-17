# -*- coding: utf-8 -*-
"""Кэш ответов «чтение дерева файлов», который сам себя инвалидирует.

Зачем это отдельным модулем
---------------------------
Список заметок и граф связей перечитывают ВСЕ json-файлы заметок на каждый
запрос: на замерах это 312 мс и 558 мс соответственно, и страница заметок
дёргает их при каждом открытии и каждом возврате на вкладку. Данные при этом
меняются только когда пользователь сам что-то сохранил — то есть в 99%
запросов ответ идентичен предыдущему.

Почему не TTL
-------------
Кэш «на 5 секунд» решает проблему загрузки, но создаёт другую: сохранил
заметку, вернулся в список — а её там ещё нет. Для заметок это выглядит как
потеря данных (и в старой версии графа такие жалобы уже были).

Здесь ключ кэша — сигнатура каталога: количество json-файлов, количество
подкаталогов, суммарный размер и максимальный mtime. Сохранение, удаление,
переименование и создание папки меняют хотя бы одно из этих чисел, поэтому
следующий же запрос пересоберёт ответ. Проверка сигнатуры — один обход
os.scandir без чтения содержимого, единицы миллисекунд против сотен на разбор
JSON.

Инвалидация по сигнатуре не покрывает единственный теоретический случай:
файл переписан так, что совпали и размер, и mtime (с точностью 100 нс на
NTFS). Практически это невозможно, а записи заметок идут через os.replace,
который всегда обновляет mtime.

Ответы разделяются между запросами и не копируются. Вызывающий код не должен
мутировать полученный объект — эндпоинты main.py его только сериализуют.
"""

import os
import threading
from collections import OrderedDict
import swallow


def dir_signature(root, suffix=".json"):
    """Дешёвая подпись дерева: (файлов, подкаталогов, байт, max mtime_ns).

    Читается только метаинформация: os.scandir отдаёт её из того же обхода
    каталога, отдельного stat на каждый файл не требуется.
    """
    if not root:
        return None
    try:
        if not os.path.isdir(root):
            return None
    except OSError:
        swallow.note('read_cache.dir_signature:L51')
        return None

    files = 0
    dirs = 0
    total = 0
    newest = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        swallow.note('read_cache.dir_signature:L67')
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        dirs += 1
                        stack.append(entry.path)
                        if st.st_mtime_ns > newest:
                            newest = st.st_mtime_ns
                    elif suffix is None or entry.name.endswith(suffix):
                        files += 1
                        total += st.st_size
                        if st.st_mtime_ns > newest:
                            newest = st.st_mtime_ns
        except OSError:
            swallow.note('read_cache.dir_signature:L80')
            continue
    return (files, dirs, total, newest)


class SignatureCache:
    """LRU-кэш, где попадание определяется сигнатурой каталога, а не временем.

    Ключ — (root, произвольный ключ запроса): список заметок в папке,
    параметры сортировки, id профиля и т.п. Значение возвращается как есть.
    """

    def __init__(self, limit=256):
        self._lock = threading.Lock()
        self._data = OrderedDict()
        self._limit = max(16, int(limit))
        # Для диагностики: сколько запросов обслужено из кэша.
        self.hits = 0
        self.misses = 0

    def get(self, root, key, compute, suffix=".json"):
        """Вернуть значение из кэша или посчитать и запомнить.

        compute вызывается ВНЕ блокировки: иначе медленный разбор заметок
        сериализовал бы все запросы к разным папкам. Два одновременных
        промаха по одному ключу просто посчитают ответ дважды — это дешевле,
        чем держать глобальный лок на время чтения диска.
        """
        signature = dir_signature(root, suffix=suffix)
        cache_key = (root or "", key)
        with self._lock:
            entry = self._data.get(cache_key)
            if entry is not None and entry[0] == signature:
                self._data.move_to_end(cache_key)
                self.hits += 1
                return entry[1]

        value = compute()

        with self._lock:
            self._data[cache_key] = (signature, value)
            self._data.move_to_end(cache_key)
            while len(self._data) > self._limit:
                self._data.popitem(last=False)
            self.misses += 1
        return value

    def invalidate(self, root=None):
        """Сбросить всё или только записи конкретного корня."""
        with self._lock:
            if root is None:
                self._data.clear()
                return
            for key in [k for k in self._data if k[0] == root]:
                self._data.pop(key, None)

    def stats(self):
        with self._lock:
            return {"entries": len(self._data), "hits": self.hits,
                    "misses": self.misses, "limit": self._limit}
