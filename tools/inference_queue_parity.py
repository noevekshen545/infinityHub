#!/usr/bin/env python3
"""Гейт inference.fallback=queue: очередь запросов при недоступном движке.

Что здесь доказывается, по порядку:

  1. санитайзер носит границы очереди (TTL, период, число заданий, размер
     файла) и те же числа читает сам модуль: панель показывает не то, что
     человек набрал, а то, с чем модуль заработает;
  2. выключенный рубильник = ровно прежний путь: файл очереди не создаётся,
     поток не поднимается, постановка отказывает, а текст ошибки в llm_service
     остаётся байт в байт тем, каким был до этого модуля;
  3. включённый рубильник: запрос ложится на диск, повтор узнаётся по
     отпечатку, картинка и слишком большой текст отказываются, пределы
     срезают очередь по количеству и по весу;
  4. исполнение: порядок FIFO, мёртвый движок не сжигает попытки остальных,
     просроченное отменяется, после пяти попыток задание бросается, счётчики
     точные, а сам проход очереди не ставит тот же запрос второй раз;
  5. переживание перезапуска: после очистки памяти задания читаются с диска;
  6. живые эндпоинты: без админа — 403, сохранение рубильника поднимает и
     гасит поток сразу, «Догнать сейчас» не вешает запрос, «Убрать» и «Стереть»
     работают, стоп-кран гасит поток и удаляет файл с чужим текстом;
  7. проводка: ветка ошибки вызывает очередь, ветка «прочее исключение» — нет,
     стартап guarded, у каждого нового маршрута есть проверка админа, панель
     отправляет все четыре числа и больше не врет про «не работает».

    python tools/inference_queue_parity.py

Весь конфиг и все файлы пишутся во временный каталог данных.
"""

import faulthandler
import json
import os
import shutil
import sys
import tempfile
import threading
import time

TMP_DATA = tempfile.mkdtemp(prefix="inference_queue_parity_")
os.environ["INFINITY_DATA_DIR"] = TMP_DATA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import client_offload as CO        # noqa: E402
import inference_queue as IQ       # noqa: E402

CHECKS = 0
PROBLEMS = []

faulthandler.dump_traceback_later(240, exit=True)


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        PROBLEMS.append("%s: %s" % (label, detail))
        print("  ✗ %s — %s" % (label, detail))


def inf_cfg(**kw):
    """Сохранить только блок inference: save() сливает глубоко.

    Память и файл очищаются вместе: секция обязана видеть ровно то, что
    положила она сама, иначе остаток предыдущей секции выглядел бы поведением
    модуля.
    """
    blk = {"fallback": "off", "queue_ttl_s": 1800, "queue_poll_s": 20,
           "queue_max_jobs": 100, "queue_max_bytes": 2097152}
    blk.update(kw)
    CO.save({"decentral": {"inference": blk}})
    IQ.reset_for_tests()
    IQ.purge()
    return blk


def qpath():
    return IQ._path()


print("1. санитайзер и согласованные границы")
d = CO._clean_decentral({})["inference"]
check("дефолты несут все границы очереди",
      set(d) == {"fallback", "queue_ttl_s", "queue_poll_s", "queue_max_jobs",
                 "queue_max_bytes"}, json.dumps(d))
check("дефолты совпадают с дефолтами модуля",
      (d["queue_ttl_s"], d["queue_poll_s"], d["queue_max_jobs"],
       d["queue_max_bytes"]) == (IQ.DEFAULT_TTL_S, IQ.DEFAULT_POLL_S,
                                 IQ.DEFAULT_MAX_JOBS, IQ.DEFAULT_MAX_BYTES),
      json.dumps(d))
c = CO._clean_decentral({"inference": {"fallback": "queue", "queue_ttl_s": 5,
                                       "queue_poll_s": 99999,
                                       "queue_max_jobs": 0,
                                       "queue_max_bytes": 1,
                                       "queue_bogus": 1}})["inference"]
check("границы сдвигаются к разрешённым, а не хранят мусор",
      (c["queue_ttl_s"], c["queue_poll_s"], c["queue_max_jobs"],
       c["queue_max_bytes"]) == (60, 3600, 1, 65536), json.dumps(c))
check("частая проверка движка невозможна: пол 15 с",
      CO._clean_decentral({"inference": {"queue_poll_s": 1}})["inference"]["queue_poll_s"] == 15
      and IQ.cfg()["poll_s"] >= 15, "")
check("посторонний ключ очереди исчезает",
      "queue_bogus" not in c, json.dumps(c))
check("неизвестный режим fallback становится off",
      CO._clean_decentral({"inference": {"fallback": "всё"}})["inference"]["fallback"] == "off", "")
CO.save({"decentral": {"inference": {"fallback": "queue", "queue_ttl_s": 300,
                                     "queue_poll_s": 30, "queue_max_jobs": 7,
                                     "queue_max_bytes": 400000}}})
live = IQ.cfg()
stored = CO.decentral()["inference"]
check("модуль читает ровно те числа, что сохранил санитайзер",
      (live["ttl_s"], live["poll_s"], live["max_jobs"], live["max_bytes"]) ==
      (stored["queue_ttl_s"], stored["queue_poll_s"], stored["queue_max_jobs"],
       stored["queue_max_bytes"]), json.dumps([live, stored]))

print("2. выключенный рубильник = прежний путь")
inf_cfg(fallback="off")
before = threading.enumerate()
r = IQ.enqueue([{"role": "user", "content": "текст"}], "model", "notes")
check("при выключенном флаге постановка отказывает",
      r.get("ok") is False and "выключен" in r.get("why", ""), json.dumps(r))
check("файл очереди не создан", os.path.exists(qpath()) is False, qpath())
check("поток при выключенном флаге не поднимается",
      IQ.start() is None and IQ.running() is False, "")
check("ни одного нового потока очереди",
      [t for t in threading.enumerate() if t not in before
       and "inference" in t.name] == [], str(threading.enumerate()))
dr = IQ.drain()
check("проход при выключенном флаге ничего не делает",
      dr.get("ok") is False and dr["done"] == 0, json.dumps(dr))
check("счётчики не тронуты",
      IQ._run["attempted"] == 0 and IQ._run["drained"] == 0, "")
st = IQ.status()
check("статус честный: выключено, пусто, потока нет",
      st["enabled"] is False and st["running"] is False and st["jobs"] == []
      and st["counts"]["total"] == 0 and st["file"] is False,
      json.dumps(st, ensure_ascii=False)[:300])
check("выключенный статус всё равно показывает числа, с которыми очередь заработает",
      (st["ttl_s"], st["poll_s"], st["max_jobs"], st["max_bytes"]) ==
      (IQ.DEFAULT_TTL_S, IQ.DEFAULT_POLL_S, IQ.DEFAULT_MAX_JOBS,
       IQ.DEFAULT_MAX_BYTES), json.dumps(st)[:200])

import modules.llm_service as LS   # noqa: E402

OLD_ERROR = ("Ошибка подключения к LM Studio: {reason}. Убедитесь что LM Studio "
             "запущен и модель загружена.")


def llm_error(reason="Connection refused"):
    LS.get_llm_url = lambda: "http://127.0.0.1:9/v1/chat/completions"
    LS.get_llm_api_key = lambda: ""
    LS.resolve_llm_model = lambda m: "тест-модель"
    return LS._lm_studio_request_inner([{"role": "user", "content": "привет"}])


got = llm_error()
check("при выключенном флаге текст ошибки байт в байт прежний",
      got == {"content": OLD_ERROR.format(reason=str(got["content"].split(": ", 1)[1]
                                           .split(". Убедитесь")[0])),
              "reasoning": ""} and "очереди" not in got["content"],
      str(got)[:240])
check("при выключенном флаге файл очереди по-прежнему не создан",
      os.path.exists(qpath()) is False, "")

print("3. включённый рубильник: копление")
inf_cfg(fallback="queue", queue_ttl_s=600, queue_max_jobs=100)
r = IQ.enqueue([{"role": "user", "content": "первый"}], "модель", "notes")
jid = r.get("id")
check("запрос принят и записан на диск",
      r.get("ok") is True and os.path.exists(qpath()) is True, json.dumps(r))
raw = json.load(open(qpath(), encoding="utf-8"))
ent = raw["jobs"].get(jid) or {}
check("запись полная: статус, попыток ноль, срок из конфига",
      ent.get("status") == "pending" and ent.get("attempts") == 0
      and abs((ent.get("expires_at") or 0) - (ent.get("queued_at") or 0) - 600) < 1
      and ent.get("module") == "notes" and ent.get("messages"),
      json.dumps(ent, ensure_ascii=False)[:260])
r2 = IQ.enqueue([{"role": "user", "content": "первый"}], "модель", "notes")
check("повтор того же запроса не плодит второе задание",
      r2.get("already") is True and r2.get("id") == jid, json.dumps(r2))
r2b = IQ.enqueue([{"role": "user", "content": "первый"}], "модель", "search")
check("модуль не входит в отпечаток: повтор из другого модуля — то же задание",
      r2b.get("already") is True and r2b.get("id") == jid
      and IQ.counts()["total"] == 1, json.dumps([r2b, IQ.counts()]))
check("другой запрос принят отдельно",
      IQ.enqueue([{"role": "user", "content": "второй"}], "модель", "books")
      .get("ok") is True and IQ.counts()["pending"] == 2, json.dumps(IQ.counts()))
check("запрос с картинкой в очередь не берётся",
      IQ.enqueue([{"role": "user", "content": "третья"}], "м", "н",
                 vision_image_base64="AAABBB")["ok"] is False
      and IQ.counts()["pending"] == 2, json.dumps(IQ.counts()))
check("слишком большой текст не берётся",
      IQ.enqueue([{"role": "user", "content": "х" * (IQ.MAX_PAYLOAD_BYTES + 10)}],
                 "м", "н")["ok"] is False, "")
check("отказ не дописал файл", IQ.counts()["total"] == 2, json.dumps(IQ.counts()))
inf_cfg(fallback="queue", queue_max_jobs=2)
for i in range(4):
    IQ.enqueue([{"role": "user", "content": "малый %d" % i}], "м", "н")
check("предел по числу заданий соблюдён", IQ.counts()["total"] <= 2,
      json.dumps(IQ.counts()))
inf_cfg(fallback="queue", queue_max_jobs=10, queue_max_bytes=65536)
IQ.enqueue([{"role": "user", "content": "лёгкий"}], "м", "н")
IQ.enqueue([{"role": "user", "content": "тяжёлый " + "ю" * 100000}], "м", "н")
sizes = sorted(len(json.dumps(e.get("messages") or "", ensure_ascii=False))
               for e in IQ._jobs.values())
check("по пределу веса уходит тяжёлое, а не лёгкое",
      len(sizes) == 1 and sizes[0] < 200
      and any("лёгкий" in json.dumps(e.get("messages") or "", ensure_ascii=False)
              for e in IQ._jobs.values()), json.dumps(sizes))
stq = IQ.status()
check("включённый статус отдаёт путь, файл и все пределы",
      stq["enabled"] is True and stq["file"] is True and stq["path"] == qpath()
      and stq["max_jobs"] == 10 and stq["max_attempts"] == IQ.MAX_ATTEMPTS,
      json.dumps(stq, ensure_ascii=False)[:260])

print("4. исполнение")
_REAL_EXECUTE = IQ._execute          # настоящий исполнитель: подмены секций 4 не переживёт
inf_cfg(fallback="queue", queue_ttl_s=600, queue_max_jobs=10)
calls = []


def exec_unreachable(ent):
    calls.append(ent["id"])
    return {"ok": False, "error": "Ошибка подключения к LM Studio: refused"}


def exec_ok(ent):
    calls.append(ent["id"])
    return {"ok": True, "content": "ответ для %s" % ent["id"], "reasoning": ""}


IQ.enqueue([{"role": "user", "content": "один"}], "м", "н")
IQ.enqueue([{"role": "user", "content": "два"}], "м", "н")
IQ._execute = exec_unreachable
res = IQ.drain(limit=4)
check("мёртвый движок останавливает проход, а не сжигает остальные попытки",
      res["tried"] == 1 and res["done"] == 0 and res["failed"] == 1
      and len(calls) == 1, json.dumps(res))
check("задание осталось ждать, попытка засчитана",
      IQ.counts()["pending"] == 2
      and sorted(e["attempts"] for e in IQ._jobs.values()) == [0, 1],
      json.dumps([e["attempts"] for e in IQ._jobs.values()]))
check("счётчик попыток точный: attempted равен числу вызовов движка",
      IQ._run["attempted"] == 1 and IQ._run["drained"] == 0,
      json.dumps({k: IQ._run[k] for k in ("attempted", "drained")}))
check("движок помечен недоступным", IQ._run["engine_up"] is False, "")
IQ._execute = exec_ok
res = IQ.drain(limit=4)
check("вернувшийся движок забирает очередь до конца",
      res["done"] == 2 and IQ.counts()["pending"] == 0
      and IQ.counts()["done"] == 2, json.dumps([res, IQ.counts()]))
check(".drained вырос ровно на число готовых", IQ._run["drained"] == 2,
      str(IQ._run["drained"]))
done_ent = [e for e in IQ._jobs.values() if e["status"] == "done"][0]
check("результат лежит в записи и забирается по id",
      IQ.result(done_ent["id"])["result"]["content"]
      == "ответ для %s" % done_ent["id"], "")
done_brief = [e for e in IQ.jobs() if e["status"] == "done"][0]
check("результат не пустой и показан в списке",
      done_brief["result_preview"].startswith("ответ для")
      and done_brief["result_bytes"] > 0,
      json.dumps(done_brief, ensure_ascii=False)[:200])
check("неизвестный id не притворяется заданием",
      IQ.result("q9_nosuch")["status"] == "unknown", "")
inf_cfg(fallback="queue")
IQ.enqueue([{"role": "user", "content": "упорный"}], "м", "н")
for e in IQ._jobs.values():
    e["attempts"] = IQ.MAX_ATTEMPTS
res = IQ.drain()
check("после пяти попыток задание бросается, а не вечно ждёт",
      IQ.counts()["error"] == 1 and res["tried"] == 0,
      json.dumps([res, IQ.counts()]))
inf_cfg(fallback="queue")
IQ.enqueue([{"role": "user", "content": "просрочен"}], "м", "н")
for e in IQ._jobs.values():
    e["expires_at"] = time.time() - 1
res = IQ.drain()
cancelled = [e for e in IQ._jobs.values() if e["status"] == "cancelled"]
check("просроченное отменяется с объяснением",
      IQ.counts()["cancelled"] == 1 and res["tried"] == 0
      and "протухло" in (cancelled[0].get("why") or ""),
      json.dumps([res, cancelled[:1]], ensure_ascii=False)[:300])
calls.clear()
check("пустая очередь ничего не вызывает",
      IQ.drain()["tried"] == 0 and not calls, str(calls))

print("5. очередь не крутит сама себя, файл переживает перезапуск")
inf_cfg(fallback="queue", queue_ttl_s=600)
LS.get_llm_url = lambda: "http://127.0.0.1:9/v1/chat/completions"
LS.get_llm_api_key = lambda: ""
LS.resolve_llm_model = lambda m: "тест-модель"
IQ.enqueue([{"role": "user", "content": "сквозной"}], "сквозь", "notes")
before_total = IQ.counts()["total"]
# Настоящий _execute, настоящий запрос, настоящая ветка URLError: круг срывает
# не совпадение отпечатка, а флаг исполнения — без него проход сам себе
# поставил бы задание поверх своего же.
IQ._execute = _REAL_EXECUTE
res = IQ.drain()
check("движок недоступен: задание всё ещё ждёт",
      IQ.counts()["pending"] == 1 and res["done"] == 0 and res["failed"] == 1,
      json.dumps([res, IQ.counts()]))
check("проход очереди не поставил тот же запрос второй раз",
      IQ.counts()["total"] == before_total
      and "lm_studio_request" not in [e.get("module") for e in IQ._jobs.values()],
      json.dumps([before_total, IQ.counts()]))
txt = llm_error()
check("снаружи очереди новый текст честно становится отдельным заданием",
      IQ.counts()["total"] == before_total + 1 and "id" in txt["content"],
      json.dumps(IQ.counts()))
txt2 = llm_error()
check("тот же текст, запрошенный снова, ссылается на то же задание",
      IQ.counts()["total"] == before_total + 1 and txt2 == txt,
      json.dumps([txt["content"][-60:], txt2["content"][-60:], IQ.counts()]))
check("включённый рубильник добавляет в ошибку строку про очередь",
      "сохранён в очереди" in txt["content"], txt["content"][:200])
saved_ids = sorted(IQ._jobs)
IQ.reset_for_tests()
check("после очистки памяти задания читаются с диска",
      sorted(IQ.jobs(50), key=lambda e: e["id"]) and
      sorted(e["id"] for e in IQ.jobs(50)) == saved_ids,
      json.dumps([saved_ids, [e["id"] for e in IQ.jobs(50)]]))
check("срок и статус тоже пережили перезапуск",
      all(e["status"] == "pending" for e in IQ.jobs(50)), "")
gone = IQ.forget(saved_ids[0])
check("«Убрать» снимает одно задание и пишет файл меньше",
      gone["ok"] is True and sorted(e["id"] for e in IQ.jobs(50)) == saved_ids[1:],
      json.dumps(gone))
check("убранное не возвращается с диска",
      IQ.forget(saved_ids[0])["ok"] is False, "")
inf_cfg(fallback="queue")
_REAL_ENGINE_UP = IQ.engine_up
IQ.engine_up = lambda: False


def _qthreads():
    return [t for t in threading.enumerate() if "inference" in t.name]


_n0 = len(_qthreads())
IQ.start()
IQ.start()
IQ.start()
check("повторный старт не поднимает второй поток",
      len(_qthreads()) == _n0 + 1 and IQ.running() is True,
      str([t.name for t in _qthreads()]))
check("поток доживает до остановки и гасится ею",
      IQ.stop() is not None and IQ.running() is False
      and len(_qthreads()) == _n0, str([t.name for t in _qthreads()]))
IQ.enqueue([{"role": "user", "content": "на стирание"}], "м", "н")
CO.save({"decentral": {"inference": {"fallback": "off"}}})
check("выключенный рубильник не поднимает поток даже по просьбе",
      IQ.start() is None and IQ.running() is False, "")
st_off = IQ.status()
check("снятый рубильник не прячет оставшийся файл: панель обязана это увидеть",
      st_off["enabled"] is False and st_off["file"] is True
      and os.path.exists(qpath()) is True, json.dumps(st_off)[:220])
pur = IQ.purge()
check("«Стереть» удаляет и память, и файл",
      pur["file_removed"] is True and IQ.counts()["total"] == 0
      and os.path.exists(qpath()) is False, json.dumps(pur))

print("6. живые эндпоинты")
from fastapi import FastAPI                       # noqa: E402
from fastapi.testclient import TestClient         # noqa: E402
from routes import offload as RO                  # noqa: E402

app = FastAPI()
app.include_router(RO.router)
CLIENT = TestClient(app)
real_admin = RO._admin_ok
r = CLIENT.get("/api/admin/inference/queue")
check("состояние очереди только для админа", r.status_code == 403, str(r.status_code))
for path in ("/api/admin/inference/queue/drain", "/api/admin/inference/queue/forget",
             "/api/admin/inference/queue/purge"):
    check("без админа нельзя %s" % path.rsplit("/", 1)[1],
          CLIENT.post(path, json={"id": "q1_x"}).status_code == 403, path)
RO._admin_ok = lambda request: True
try:
    inf_cfg(fallback="off")
    r = CLIENT.post("/api/admin/decentral/save",
                    json={"decentral": {"inference": {"fallback": "queue",
                                                      "queue_ttl_s": 900,
                                                      "queue_poll_s": 45,
                                                      "queue_max_jobs": 5,
                                                      "queue_max_bytes": 131072}}})
    j = r.json()
    check("применение рубильника поднимает поток без перезапуска",
          r.status_code == 200 and (j.get("inference") or {}).get("running") is True
          and IQ.running() is True, json.dumps(j, ensure_ascii=False)[:300])
    check("сохранённые границы дошли до модуля",
          (j["inference"]["ttl_s"], j["inference"]["poll_s"],
           j["inference"]["max_bytes"]) == (900, 45, 131072),
          json.dumps(j["inference"])[:200])
    IQ.stop()
    IQ.enqueue([{"role": "user", "content": "через панель"}], "м", "notes")
    r = CLIENT.get("/api/admin/decentral")
    gi = (r.json().get("inference") or {})
    check("GET состояния децентрализации включает очередь",
          gi.get("enabled") is True and gi.get("counts", {}).get("pending") == 1,
          json.dumps(gi, ensure_ascii=False)[:260])
    r = CLIENT.get("/api/admin/inference/queue")
    check("отдельный GET очереди отдаёт то же состояние",
          r.json()["queue"]["counts"]["pending"] == 1, "")
    IQ.engine_up = lambda: False
    real_drain = IQ.drain
    IQ.drain = lambda limit=4: time.sleep(1.2)     # проход, который не закончился
    try:
        r = CLIENT.post("/api/admin/inference/queue/drain", json={})
        kick = (r.json() or {}).get("kick") or {}
        check("«Догнать сейчас» отвечает сразу и не врёт о результате",
              r.status_code == 200 and kick.get("started") is True
              and kick.get("done") is None, json.dumps(r.json(), ensure_ascii=False)[:200])
        r = CLIENT.post("/api/admin/inference/queue/drain", json={})
        check("повтор во время прохода не запускает второй",
              (r.json().get("kick") or {}).get("already") is True,
              json.dumps(r.json())[:160])
    finally:
        time.sleep(1.4)
        IQ.drain = real_drain
    r = CLIENT.post("/api/admin/inference/queue/forget", json={"id": "нет-такого"})
    check("убрать то, чего нет, нельзя", r.status_code == 404, str(r.status_code))
    real_id = IQ.jobs()[0]["id"]
    r = CLIENT.post("/api/admin/inference/queue/forget", json={"id": real_id})
    check("«Убрать» из панели снимает задание",
          r.json().get("removed") == real_id and IQ.counts()["total"] == 0, "")
    r = CLIENT.post("/api/admin/inference/queue/forget", json={})
    check("пустой id не принимается", r.status_code == 400, str(r.status_code))
    IQ.enqueue([{"role": "user", "content": "на стирание"}], "м", "notes")
    r = CLIENT.post("/api/admin/inference/queue/purge", json={})
    check("«Стереть очередь» убирает файл с промптами",
          r.json()["purged"]["file_removed"] is True
          and os.path.exists(qpath()) is False, json.dumps(r.json())[:200])
    inf_cfg(fallback="queue")
    IQ.start()
    IQ.enqueue([{"role": "user", "content": "до стоп-крана"}], "м", "notes")
    r = CLIENT.post("/api/admin/decentral/revoke", json={})
    check("стоп-кран гасит поток очереди",
          r.status_code == 200 and IQ.running() is False, str(r.status_code))
    check("стоп-кран стирает файл очереди: чужой текст не остаётся",
          (r.json().get("inference_purged") or {}).get("file_removed") is True
          and os.path.exists(qpath()) is False
          and CO.decentral()["inference"]["fallback"] == "off",
          json.dumps(r.json(), ensure_ascii=False)[:260])
    r = CLIENT.post("/api/admin/decentral/save",
                    json={"decentral": {"inference": {"fallback": "off"}}})
    check("снятие рубильника гасит поток сразу",
          (r.json().get("inference") or {}).get("enabled") is False
          and IQ.running() is False, json.dumps(r.json())[:200])
    IQ.stop()
    IQ.purge()
finally:
    RO._admin_ok = real_admin

print("7. проводка")
llm_src = open(os.path.join(ROOT, "app", "modules", "llm_service.py"),
               encoding="utf-8").read()
main_src = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
route_src = open(os.path.join(ROOT, "app", "routes", "offload.py"),
                 encoding="utf-8").read()
page = open(os.path.join(ROOT, "app", "templates", "page_admin.html"),
            encoding="utf-8").read()
_err_i = llm_src.find("except urllib.error.URLError as e:")
_gen_i = llm_src.find("except Exception as e:", _err_i)
_hook_i = llm_src.find("_held = _queue_unavailable(", _err_i)
_ret_i = llm_src.find('return {"content": f"Ошибка подключения к LM Studio', _err_i)
check("копирование в очередь стоит в ветке «нет связи», до возврата ошибки",
      _err_i < _hook_i < _ret_i, "%d %d %d" % (_err_i, _hook_i, _ret_i))
check("прочие исключения очередь не трогают: их повторить нельзя",
      llm_src.count("_held = _queue_unavailable(") == 1
      and llm_src.find("_queue_unavailable(", _gen_i) < 0, "")
check("помощник очереди молчит при выключенном флаге",
      "if not inference_queue.enabled():" in llm_src
      and 'return ""' in llm_src[llm_src.find("def _queue_unavailable"):
                                llm_src.find("def _lm_studio_request_inner")], "")
check("сама очередь не переписывает конфиг",
      "client_offload.save" not in open(os.path.join(ROOT, "app",
                                                     "inference_queue.py"),
                                        encoding="utf-8").read().split('"""', 2)[2],
      "")
check("поток запускается только при включённом рубильнике",
      "async def startup_inference_queue" in main_src
      and main_src.find("if inference_queue.enabled()")
      < main_src.find("inference_queue.start()", main_src.find("startup_inference_queue"))
      + 200, "")
_rev_i = route_src.find("async def admin_decentral_revoke(")
_rev = route_src[_rev_i:route_src.find("@router", _rev_i + 10)]
check("стоп-кран гасит поток и стирает файл внутри себя",
      "inference_queue.stop()" in _rev and "inference_queue.purge()" in _rev
      and "inference_purged" in _rev, _rev[:200])
_new = [p for p in ("/api/admin/inference/queue", "/api/admin/inference/queue/drain",
                    "/api/admin/inference/queue/forget", "/api/admin/inference/queue/purge")
        if ('"%s"' % p) in route_src]
check("все четыре маршрута на месте", len(_new) == 4, json.dumps(_new))
check("у каждого нового маршрута есть проверка админа",
      route_src.count("if not _admin_ok(request)") >=
      route_src.count('@router.get("/api/admin/inference')
      + route_src.count('@router.post("/api/admin/inference'),
      str(route_src.count("if not _admin_ok(request)")))
check("панель отправляет все четыре границы",
      all(k in page for k in ("dcInfTtl", "dcInfPoll", "dcInfMaxJobs", "dcInfMaxKb"))
      and page.count("queue_ttl_s: num(") == 1
      and page.count("queue_max_bytes: num('dcInfMaxKb', 2048) * 1024") == 1, "")
check("панель читает их обратно",
      page.count("val('dcInfTtl', inf.queue_ttl_s)") == 1
      and "val('dcInfMaxKb', Math.round((inf.queue_max_bytes" in page, "")
_i = page.find('id="dcInfFallback"')
_row = page[max(0, page.rfind("<label", 0, _i)):_i]
check("режим очереди больше не помечен «не работает»",
      "не работает" not in _row, _row[-200:])
check("локальная маленькая модель не вернулась в список обещанием",
      page.count('<option value="local_small">') == 0
      and "Маленькая модель" in page, "")
check("состояние очереди рисуется в панели",
      "_renderInferenceQueue(r.inference || {})" in page
      and "id=\"iqLive\"" in page and "id=\"iqTable\"" in page, "")
_verdicts = page[page.find("const IQ_VERDICT"):page.find("function _renderInferenceQueue")]
check("каждый статус задания имеет честное слово в панели",
      all('"%s"' % s in _verdicts or "'%s'" % s in _verdicts
          for s in ("pending", "running", "done", "cancelled", "error")),
      _verdicts[:200])
check("кнопка «Догнать» отключается, пока проход идёт",
      "btn.disabled = !!st.kicking" in page, "")

# Живой прогон 2026-10-31: движок отвечал, а очередь считала его мёртвым —
# проверка живости получала полный адрес чата и дописывала к нему /v1/models.
_seen = []
_real_reach = LS._llm_endpoint_reachable
LS.get_llm_url = lambda: "http://127.0.0.1:8080/v1/chat/completions"
LS.get_llm_api_key = lambda: ""
LS._llm_endpoint_reachable = lambda base, *a: (_seen.append(base), True)[1]
try:
    _up = _REAL_ENGINE_UP()
finally:
    LS._llm_endpoint_reachable = _real_reach
check("живость движка проверяется по базовому адресу, а не по адресу чата",
      _up is True and _seen == ["http://127.0.0.1:8080"], json.dumps(_seen))

print()
print("проверок: %d, проблем: %d" % (CHECKS, len(PROBLEMS)))
for p in PROBLEMS:
    print("  ✗ " + p)
shutil.rmtree(TMP_DATA, ignore_errors=True)
sys.exit(1 if PROBLEMS else 0)
