"""Сколько стоит одна минута жизни планировщика снапшотов (по вызовам).

Замер in-process, копирует _scheduler_loop (:1490-1512): maybe_snapshot +
cleanup_orphans на каждом тике, prune_backups(dry_run) на каждом 10-м тике.
Всё только читает: ничего не создаёт и не удаляет.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(".")))
sys.path.insert(0, ".")

from app import recovery_core as rc  # noqa: E402


def ms(t0):
    return (time.perf_counter() - t0) * 1000.0


def timed(label, fn, n=3):
    ts = []
    res = None
    for _ in range(n):
        t0 = time.perf_counter()
        res = fn()
        ts.append(ms(t0))
    print("%-34s медиана %8.1f мс   (мин %6.1f / макс %7.1f)" %
          (label, sorted(ts)[len(ts) // 2], min(ts), max(ts)))
    return res


print("BACKUPS_DIR =", rc.BACKUPS_DIR)
names = [n for n in os.listdir(rc.BACKUPS_DIR)
         if not n.startswith("_") and os.path.isdir(os.path.join(rc.BACKUPS_DIR, n))]
print("снимков в каталоге:", len(names))

full = timed("list_backups() (целиком)", rc.list_backups)
cheap = timed("list_backups(with_integrity=False)", lambda: rc.list_backups(with_integrity=False))
newest = timed("_newest_committed_manifest() (тёплый)", rc._newest_committed_manifest)
timed("dir_size(BACKUPS_DIR)", lambda: rc.dir_size(rc.BACKUPS_DIR))
timed("cleanup_orphans()", rc.cleanup_orphans)

# один тик планировщика как он есть
def tick():
    rc.maybe_snapshot(reason="bench", kind="auto")
    rc.cleanup_orphans()


t = timed("тик: maybe_snapshot + cleanup_orphans", tick)
print("\nполям newest совпадают:",
      newest.get("id") == max((i for i in full if not i.get("broken")),
                              key=lambda x: x.get("ts", 0))["id"])
print("prune dry_run:")
p = rc.prune_backups(dry_run=True)
print("  удалений бы:", len(p.get("removed", [])),
      "kept", p.get("kept"), "total_mb", p.get("total_mb"),
      "dry_run", p.get("dry_run"))
for r in p.get("removed", [])[:5]:
    print("   ", r)
print("существует ли каталог после dry_run:",
      all(os.path.isdir(os.path.join(rc.BACKUPS_DIR, r["id"])) for r in p.get("removed", [])))
