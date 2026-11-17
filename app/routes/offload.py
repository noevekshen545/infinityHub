"""Client offload API: настройки офлоада, телеметрия, устройства-помощники."""

import asyncio
import json
import logging
import os
import threading
import time
import uuid

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

import client_offload
import helper_channel
import offload_baseline
import perf_metrics
import profile_paths
import tls_sidecar
from modules import books_read
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()
MAX_STATS_JOBS = 1000


def _admin_ok(request):
    import admin_module
    return admin_module.admin_request_ok(request)


def _helpers_on():
    """Разрешено ли вообще иметь помощников (общий рубильник + helpers)."""
    cfg = client_offload.load()
    return bool(cfg.get("enabled")) and bool((cfg.get("helpers") or {}).get("enabled"))


def _server_port():
    """Свой порт приложения — нужен, чтобы TLS-вход не начал проксировать сам
    на себя (цикл из двух тоннелей вешает весь вход)."""
    try:
        return int(os.environ.get("INFINITY_PORT") or 8281)
    except Exception:
        swallow.note('offload._server_port:L43')
        return 8281


def _tls_state(cfg=None):
    """Рубильник TLS + живой статус входа, одной строкой для панели."""
    t = ((cfg or client_offload.decentral()) or {}).get("tls") or {}
    port = int(t.get("port") or tls_sidecar.DEFAULT_PORT)
    st = tls_sidecar.status(port=port)
    st["enabled"] = bool(t.get("enabled"))
    if st.get("running") and port == _server_port():
        st["error"] = "порт совпадает с портом приложения — вход не поднят"
    return st


def _h2_state(cfg=None):
    """Рубильник HTTP/2 + живой статус слушателя и предпосылки к нему.

    Панели мало слова «включено»: здесь сразу видно, импортируется ли zttp,
    поддерживает ли uvicorn http2=True и что порт реально ответил по ALPN.
    """
    import h2_listener
    h = ((cfg or client_offload.decentral()) or {}).get("h2") or {}
    port = int(h.get("port") or h2_listener.DEFAULT_PORT)
    st = h2_listener.status(port=port)
    st["enabled"] = bool(h.get("enabled"))
    if st.get("running") and port == _server_port():
        st["error"] = "порт совпадает с портом приложения — вход не поднят"
    return st


def _entry_state(dec, h2st, tlsst):
    """Один взгляд на то, чем реально закрыт вход: два порта или один.

    Панель показывала два независимых статуса, и «оба включены, порты разные»
    ничем не отличалось от «оба включены, один вход». Здесь итог: какой порт
    слушает, кто его обслуживает и есть ли у телефона https вообще.
    """
    tport = int(((dec.get("tls") or {}).get("port")) or tls_sidecar.DEFAULT_PORT)
    hport = int(((dec.get("h2") or {}).get("port")) or h2st.get("default_port")
                or 8444)
    tls_on = bool((dec.get("tls") or {}).get("enabled"))
    h2_on = bool((dec.get("h2") or {}).get("enabled"))
    merged = bool(tls_on and h2_on and tport == hport)
    if merged:
        mode = ("один вход %d: TLS обслуживает HTTP/2-слушатель, тоннель не нужен"
                % tport)
    elif tls_on and h2_on:
        mode = "два входа: TLS на %d, HTTP/2 на %d" % (tport, hport)
    elif tls_on:
        mode = "тоннель TLS на %d (HTTP/1.1)" % tport
    elif h2_on:
        mode = "HTTP/2 на %d" % hport
    else:
        mode = "защищённого входа нет: только обычный http на %d" % _server_port()
    https_live = bool(h2st.get("running") and (merged or not tls_on)) or \
        bool(tlsst.get("running"))
    # Телефон ходит по адресу машины, а не по 127.0.0.1: панель открыта локально,
    # и подсказывать «https://127.0.0.1:порт» значило бы посоветовать телефону
    # идти к самому себе.
    lan = [a for a in (h2st.get("ips") or tlsst.get("ips") or [])
           if not str(a).startswith("127.")]
    return {"tls_port": tport, "h2_port": hport, "merged": merged,
            "port": tport if merged else None, "mode": mode,
            "host": lan[0] if lan else None,
            "error": h2st.get("error") or tlsst.get("error"),
            "tls_enabled": tls_on, "h2_enabled": h2_on,
            "tunnel_running": bool(tlsst.get("running")),
            "h2_running": bool(h2st.get("running")),
            "https_live": https_live,
            "served_by": ("h2" if merged else
                          ("тоннель" if tls_on and tlsst.get("running") else
                           ("h2" if h2st.get("running") else None)))}


@router.get("/api/offload/config")
def offload_config(request: Request):
    """Публичная конфигурация для клиентского рантайма (без секретов).

    ?device= — идентификатор устройства из client_offload.js: по нему сервер
    отдаёт этому устройству его же решение и отметки «это ты уже видел».
    Ответ зависит от клиента, поэтому no-store.
    """
    try:
        ip = request.client.host if request.client else ""
        cfg = client_offload.public_config(
            request.query_params.get("device"),
            client_offload.is_own_address(ip))
        return JSONResponse(cfg, headers={"Cache-Control": "no-store"})
    except Exception as e:
        logger.error(f"/api/offload/config error: {e}")
        return JSONResponse({"enabled": False, "tasks": {}}, status_code=200)


@router.post("/api/offload/helper/consent")
async def offload_helper_consent(request: Request):
    """Устройство ответило на баннер о помощи — запомнить по идентификатору.

    Публично на том же основании, что и WebSocket помощника: спросить
    разрешение можно только с клиента, а держатель LAN уже может и подключиться.
    Выключенные помощники сюда не пишут: конфиг «всё выключено» не должен
    обрастать новыми записями.
    """
    try:
        if not client_offload.enabled() or not _bool_helpers():
            return JSONResponse({"error": "помощники выключены"}, status_code=409)
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad request"}, status_code=400)
        ip = request.client.host if request.client else ""
        rec = client_offload.set_consent(
            body.get("device"), body.get("answer"),
            name=str(body.get("name") or ""), ua=request.headers.get("user-agent") or "",
            ip=ip)
        if rec is None:
            return JSONResponse({"error": "не сохранено"}, status_code=400)
        return {"ok": True, "consent": rec.get("consent")}
    except Exception as e:
        logger.debug(f"/api/offload/helper/consent error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/offload/helper/notice")
async def offload_helper_notice(request: Request):
    """Отметить показанное уведомление: каждое — ровно один раз на устройство."""
    try:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad request"}, status_code=400)
        ok = client_offload.mark_notice(body.get("device"), body.get("kind"))
        return {"ok": bool(ok)}
    except Exception as e:
        logger.debug(f"/api/offload/helper/notice error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/offload/stats")
async def offload_stats(request: Request):
    """Клиент отчитывается, сколько задач выполнил локально (для админки).

    Экономия сервера считается здесь, а не на клиенте: baseline знает только
    сервер. Оценка грубая — baseline измерен на фиксированном файле, а клиент
    обработал свой, другой величины.
    """
    try:
        body = await request.json()
        jobs = body.get("jobs") or []
        if not isinstance(jobs, list):
            return {"ok": True}
        for j in jobs[:50]:
            if not isinstance(j, dict):
                continue
            task = str(j.get("type") or "unknown")[:40]
            try:
                count = max(0, min(MAX_STATS_JOBS, int(j.get("count") or 1)))
                avg_ms = float(j.get("avg_ms") or 0.0)
            except Exception:
                swallow.note('offload.offload_stats:L200')
                continue
            baseline_ms = offload_baseline.baseline_for(task)
            saved = max(0.0, baseline_ms - avg_ms) * count
            for _ in range(count):
                perf_metrics.record_offload(task, bool(j.get("ok", True)),
                                            avg_ms, saved / count)
        return {"ok": True}
    except Exception as e:
        logger.debug(f"/api/offload/stats error: {e}")
        return {"ok": False}


# ── Админка ────────────────────────────────────────────────────────────────


@router.get("/api/admin/offload")
def admin_offload(request: Request):
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        helpers = client_offload.list_helpers()
        return {"ok": True, "config": client_offload.load(),
                "tasks": list(client_offload.VALID_TASKS),
                "helpers": helpers,
                "pending": [h for h in helpers if h.get("pending")]}
    except Exception as e:
        logger.error(f"/api/admin/offload error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/offload/helper")
async def admin_offload_helper(request: Request):
    """Одобрить / отклонить / удалить устройство-помощника, снять отметки показов."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        body = await request.json()
        action = str(body.get("action") or "").strip().lower()
        device_id = str(body.get("id") or "").strip()
        if not device_id or action not in ("approve", "reject", "remove", "reask", "resume"):
            return JSONResponse({"error": "bad request"}, status_code=400)
        ws = client_offload.get_helper_ws(device_id)
        if action == "approve":
            rec = client_offload.approve_helper(device_id)
            if rec is None:
                return JSONResponse({"error": "устройство не найдено"}, status_code=404)
            if ws is not None:
                try:
                    # Тот же event loop (uvicorn), что и у WS: можно писать напрямую.
                    await ws.send_json({"action": "approved", "state": "approved"})
                except Exception:
                    swallow.note('offload.admin_offload_helper:L252')
                    pass
        elif action == "reject":
            rec = client_offload.reject_helper(device_id)
            if rec is None:
                return JSONResponse({"error": "устройство не найдено"}, status_code=404)
            if ws is not None:
                try:
                    await ws.send_json({"action": "rejected", "state": "rejected"})
                    await ws.close(code=1000)
                except Exception:
                    swallow.note('offload.admin_offload_helper:L263')
                    pass
        elif action == "reask":
            # Отметка «уже показывали» вечная, поэтому сбрасывает её только
            # администратор: без этой кнопки проигнорированный баннер означал
            # бы, что устройство молчит навсегда.
            if not client_offload.reset_notices(device_id):
                return JSONResponse({"error": "устройство не найдено"}, status_code=404)
        elif action == "resume":
            # Пауза после серии отказов снимается вручную: администратор мог
            # починить Wi-Fi или обновить страницу на телефоне, и ждать
            # оставшиеся минуты незачем.
            if client_offload.resume_helper(device_id) is None:
                return JSONResponse({"error": "устройство не найдено"}, status_code=404)
        else:
            client_offload.remove_helper(device_id)
            if ws is not None:
                try:
                    await ws.close(code=1000)
                except Exception:
                    swallow.note('offload.admin_offload_helper:L283')
                    pass
        return {"ok": True, "helpers": client_offload.list_helpers()}
    except Exception as e:
        logger.error(f"/api/admin/offload/helper error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/offload/save")
async def admin_offload_save(request: Request):
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        body = await request.json()
        cfg = client_offload.save(body if isinstance(body, dict) else {})
        return {"ok": True, "config": cfg}
    except Exception as e:
        logger.error(f"/api/admin/offload/save error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Рубильники децентрализации ─────────────────────────────────────────────


@router.get("/api/admin/decentral")
def admin_decentral_get(request: Request):
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        helpers = client_offload.list_helpers()
        hcfg = client_offload.load().get("helpers") or {}
        out = {"ok": True,
               "decentral": client_offload.decentral(),
               "offload_enabled": bool(client_offload.load().get("enabled")),
               "helpers_enabled": bool(hcfg.get("enabled")),
               # Панели нужен этот флаг вместе с рубильником уведомлений:
               # «не показывать» + «спрашивать» = ни один помощник не появится,
               # и про это нельзя молчать.
               "require_consent": bool(hcfg.get("require_consent", True)),
               "dispatch_live": client_offload.dispatch_enabled(),
               "helpers_total": len(helpers),
               "helpers_approved": len([h for h in helpers if h.get("approved")])}
        out["tls"] = _tls_state(out["decentral"])
        # HTTP/2-вход приводится в соответствие прямо здесь: объект приложения
        # доступен только внутри запроса, а без него второй слушатель не
        # поднять. Флаг, включённый в панели, начинает работать сразу, а не
        # после рестарта. Сходимость одна: трогаем только при включённом
        # рубильнике и только если вход ещё не поднят.
        try:
            import h2_listener
            dec = out["decentral"]
            h = dec.get("h2") or {}
            if h.get("enabled") and not h2_listener.running():
                h2_listener.apply(dec, app=request.app, server_port=_server_port())
                tls_sidecar.apply(dec, server_port=_server_port())
            out["h2"] = _h2_state(dec)
            out["entry"] = _entry_state(dec, out["h2"], out["tls"])
        except Exception as e:
            swallow.note('offload.admin_decentral_get:L341', e)
            out["h2"] = {"error": str(e)}
        # Анонсы: живой статус потока плюс кандидаты, которых эта машина
        # реально услышала. Добавить соседа из этого списка можно только
        # отдельным действием, поэтому список — информация, а не настройка.
        try:
            import mesh_discovery
            st = mesh_discovery.status()
            for c in st["candidates"]:
                c["in_grid"] = mesh_discovery.in_grid(c["url"])
            out["discovery"] = st
        except Exception as e:
            swallow.note('offload.admin_decentral_get:L353', e)
            out["discovery"] = {"error": str(e)}
        # Перехват роли сервера: кого выберет правило доверия, если свой сервер
        # не поднимется. Выбор делает оболочка по этим же файлам — панель
        # показывает, что она увидит, не выдумывая отдельного ответа.
        try:
            import mesh_failover
            out["failover"] = mesh_failover.status()
        except Exception as e:
            swallow.note('offload.admin_decentral_get:L362', e)
            out["failover"] = {"error": str(e)}
        # Личность узла: свой id, отпечаток, закреплённые ключи соседей и
        # помощники с уровнем подтверждения. Панель обязана показывать это до
        # того, как человек включит «не пускать неподтверждённых», иначе
        # второй рубильник снимался бы вслепую.
        try:
            import mesh_identity
            out["identity"] = mesh_identity.status()
        except Exception as e:
            swallow.note('offload.admin_decentral_get:L372', e)
            out["identity"] = {"error": str(e)}
        # Репликация записей: режим, что обходится, сколько ключей известно и
        # надгробий, куда кладутся копии перезаписываемого.
        try:
            import mesh_sync
            out["sync"] = mesh_sync.status()
        except Exception as e:
            swallow.note('offload.admin_decentral_get:L380', e)
            out["sync"] = {"error": str(e)}
        # Очередь инференса: сколько запросов ждёт возвращения движка и какие
        # именно. Статус сам движок не проверяет — иначе панель зависала бы на
        # таймауте подключения каждый раз, когда её открыли при выключенном
        # LM Studio; здесь показывается последний известный ответ потока.
        try:
            import inference_queue
            out["inference"] = inference_queue.status()
        except Exception as e:
            swallow.note('offload.admin_decentral_get:L390', e)
            out["inference"] = {"error": str(e)}
        # Блок «Серверные вычисления»: таблица того, что считает сервер, и
        # последнего замера на этом железе. Сетью здесь не пахнет — соседи
        # проверяются отдельной кнопкой, иначе панель висела бы до таймаута
        # каждого из них.
        try:
            import compute_grid
            out["compute"] = {"inventory": compute_grid.inventory(),
                              "probe": compute_grid.load_probe(),
                              "probing": compute_grid.probe_running()}
        except Exception as e:
            swallow.note('offload.admin_decentral_get:L402', e)
            out["compute"] = {"error": str(e)}
        # Что реально ушло помощникам и во сколько это обошлось: панель
        # обязана показывать и отрицательную экономию, иначе рубильник
        # выглядит полезным до того, как его проверили.
        try:
            import dispatch_jobs
            out["dispatch"] = dispatch_jobs.summary()
        except Exception as e:
            swallow.note('offload.admin_decentral_get:L411', e)
            out["dispatch"] = {"error": str(e)}
        return out
    except Exception as e:
        logger.error(f"/api/admin/decentral error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/compute/probe")
def admin_compute_probe(request: Request):
    """Замерить на этой машине, что даёт больше ядер и больше машин.

    Как и эталон офлоада, прогон сознательно грузит CPU несколько секунд,
    поэтому только по кнопке и только в фоне.
    """
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        import compute_grid
        return compute_grid.run_probe_async()
    except Exception as e:
        logger.error(f"/api/admin/compute/probe error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/compute/peers/test")
async def admin_compute_peers_test(request: Request):
    """Проверить соседей: жив ли нода, что отвечает и с какой задержкой."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        import compute_grid
        # Синхронные HTTP-выходы в сеть на цикле событий остановили бы весь
        # сервер на сумму таймаутов всех соседей.
        return {"ok": True, "peers": await asyncio.to_thread(compute_grid.peers_status)}
    except Exception as e:
        logger.error(f"/api/admin/compute/peers/test error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _first_photo_file():
    """Первый реальный файл фототеки для проверки раздачи."""
    try:
        import photo_module
        from pathlib import Path
        for d in (photo_module.get_photo_dirs() or []):
            try:
                p = Path(photo_module.ensure_local_path(d))
                if not p.exists():
                    continue
                for f in p.rglob('*'):
                    if f.suffix.lower() in photo_module.PHOTO_EXTENSIONS and f.is_file():
                        return str(f)
            except Exception:
                swallow.note('offload._first_photo_file:L465')
                continue
    except Exception as e:
        logger.debug("[dispatch] поиск файла для сравнения: %s" % e)
    return None


@router.post("/api/admin/dispatch/parity")
async def admin_dispatch_parity(request: Request):
    """Одна и та же картинка: здесь через Pillow и у помощника в браузере.

    Кнопка работает и при выключенной раздаче — это проверка до включения, а
    не после. В библиотеку ничего не записывается: сравнение не имеет права
    менять данные ради отчёта.
    """
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            swallow.note('offload.admin_dispatch_parity:L486')
            body = {}
        body = body if isinstance(body, dict) else {}
        import dispatch_jobs
        import helper_channel
        import photo_module

        path = str(body.get("path") or "").strip()
        if not path or not os.path.isfile(path):
            path = await asyncio.to_thread(_first_photo_file)
        if not path:
            return {"ok": False, "error": "в библиотеке нет ни одного файла"}

        payload, expect = await asyncio.to_thread(photo_module.thumb_job, path)
        if not payload or not expect:
            return {"ok": False, "error": "файл не читается как картинка",
                    "file": os.path.basename(path)}

        def _local():
            t0 = time.perf_counter()
            raw = photo_module.local_thumb_bytes(path)
            return raw, round((time.perf_counter() - t0) * 1000.0, 1)

        local_bytes, local_ms = await asyncio.to_thread(_local)

        devices = dispatch_jobs.pick_devices("image_webp",
                                             str(body.get("device") or "") or None)
        if not devices:
            return {"ok": False, "file": os.path.basename(path),
                    "local_ms": local_ms, "local_bytes": len(local_bytes),
                    "expect": expect,
                    "error": "нет одобренного помощника в сети, который умеет image_webp",
                    "hint": "Раздача не трогала библиотеку: сравнение можно сделать "
                            "только когда устройство подключено и одобрено."}

        did = devices[0]["id"]
        res = await helper_channel.submit(did, "image_webp", payload, timeout_ms=15000)
        if not res.get("ok"):
            return {"ok": False, "file": os.path.basename(path), "device_id": did,
                    "local_ms": local_ms, "local_bytes": len(local_bytes),
                    "error": res.get("error") or "помощник не ответил"}
        raw, w, h, why = dispatch_jobs.validate_image(res.get("result"), "image/webp", expect)
        if raw is None:
            return {"ok": False, "file": os.path.basename(path), "device_id": did,
                    "local_ms": local_ms, "local_bytes": len(local_bytes),
                    "error": "результат помощника не принят: %s" % why,
                    "expect": expect}
        diff = round(100.0 * (len(raw) - len(local_bytes)) / max(1, len(local_bytes)), 1)
        return {"ok": True, "file": os.path.basename(path), "device_id": did,
                "device_name": devices[0].get("name"),
                "local_ms": local_ms, "local_bytes": len(local_bytes),
                "roundtrip_ms": res.get("roundtrip_ms"), "helper_ms": res.get("helper_ms"),
                "helper_bytes": len(raw), "helper_dims": [w, h],
                "local_dims": [expect.get("width"), expect.get("height")],
                "size_diff_percent": diff,
                "same_bytes": raw == local_bytes,
                "verdict": ("браузер масштабирует drawImage, Pillow — LANCZOS: байты "
                            "разными и должны быть; важны формат, размер и качество. "
                            "Экономия сервера: %s мс из %s мс."
                            % (round(local_ms - float(res.get("roundtrip_ms") or 0), 1), local_ms))}
    except Exception as e:
        logger.error(f"/api/admin/dispatch/parity error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/decentral/save")
async def admin_decentral_save(request: Request):
    """Сохранить только блок decentral. Остальные настройки не трогаются:
    save() сливает патч, а не перезаписывает файл."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            swallow.note('offload.admin_decentral_save:L561')
            body = {}
        block = (body or {}).get("decentral")
        if not isinstance(block, dict):
            return JSONResponse({"error": "нет блока decentral"}, status_code=400)
        cfg = client_offload.save({"decentral": block})
        out = {"ok": True, "decentral": cfg.get("decentral") or {},
               "dispatch_live": client_offload.dispatch_enabled()}
        # Оба защищённых входа приводятся в соответствие сразу: рубильник,
        # который ждёт перезапуска, выглядит сломанным. Порядок важный: сначала
        # h2 (он может занять порт тоннеля либо освободить его), потом тоннель —
        # иначе при выключении h2 тоннель спотыкался бы ещё занятый порт.
        # Целостность одна: вызов с ГОЛЫМ блоком {enabled, port} читался
        # модулями как «флаг выключен» и гасил уже поднятый вход при любом
        # сохранении панели, поэтому сюда передаётся весь блок decentral.
        import h2_listener
        dec = out["decentral"]
        app_port = _server_port()
        for name, blk in (("tls", dec.get("tls")), ("h2", dec.get("h2"))):
            p = int((blk or {}).get("port") or 0)
            if (blk or {}).get("enabled") and p and p == app_port:
                out[name + "_error"] = (
                    "Порт совпадает с портом приложения (%d): вход подняли бы "
                    "на самом себе. Укажи другой." % app_port)
                client_offload.save({"decentral": {name: {"enabled": False}}})
                dec = client_offload.decentral()
                out["decentral"] = dec
        hres = tres = None
        try:
            hres = h2_listener.apply(dec, app=request.app, server_port=app_port)
        except Exception as e:
            swallow.note('offload.admin_decentral_save:L592', e)
            hres = {"error": str(e)}
        try:
            tres = tls_sidecar.apply(dec, server_port=app_port)
        except Exception as e:
            swallow.note('offload.admin_decentral_save:L597', e)
            tres = {"error": str(e)}
        out["h2"] = _h2_state(dec)
        out["tls"] = _tls_state(dec)
        out["entry"] = _entry_state(dec, out["h2"], out["tls"])
        for res, key in ((hres, "h2_error"), (tres, "tls_error")):
            if (res or {}).get("error"):
                out[key] = res["error"]
            elif (res or {}).get("merged_from"):
                out["entry_merged"] = ("защищённый вход и HTTP/2 теперь на одном "
                                       "порту %d" % out["entry"]["port"])
        # Анонсы тоже приводятся в соответствие сразу: галочка «находить
        # соседей» обязана начать слушать порт, а не через перезапуск.
        try:
            import mesh_discovery
            out["discovery"] = mesh_discovery.apply_state()
        except Exception as e:
            swallow.note('offload.admin_decentral_save:L614', e)
            out["discovery"] = {"error": str(e)}
        # Очередь инференса приводится тем же порядком, что и анонсы: рубильник
        # «копить запросы» обязан поднять поток сразу, а снятие — погасить, иначе
        # он крутился бы при выключенном флаге и трогал диск.
        try:
            import inference_queue
            out["inference"] = inference_queue.apply_state()
        except Exception as e:
            swallow.note('offload.admin_decentral_save:L623', e)
            out["inference"] = {"error": str(e)}
        # Репликация — тем же порядком: рубильник write_mode обязан поднять
        # поток сразу, а снятие — погасить, иначе поток крутился бы с
        # выключенным флагом и трогал диск.
        try:
            import mesh_sync
            out["sync"] = mesh_sync.apply_state()
        except Exception as e:
            swallow.note('offload.admin_decentral_save:L632', e)
            out["sync"] = {"error": str(e)}
        # Соседи, которые не прошли проверку сети, исчезают из конфига молча —
        # со стороны это выглядит как баг сохранения, поэтому разницу показываем.
        sent = (((block.get("grid") or {}) if isinstance(block.get("grid"), dict)
                 else {}).get("peers")) or []
        sent = [str(x) for x in sent if str(x).strip()]
        stored = ((out["decentral"].get("grid") or {}).get("peers")) or []
        if len(stored) < len(sent):
            out["grid_rejected"] = {"sent": sent, "stored": stored,
                                    "hint": "Разрешены только адреса из локальной сети "
                                            "(127.*, 10.*, 192.168.*, 172.16-31.*, "
                                            "169.254.*) и localhost: без пути,query "
                                            "и учётных данных"}
        # Адрес вернулся в сетку — вернётся и доверие: отзыв должно быть можно
        # отменить тем же способом, которым соседа добавляют, иначе это точка
        # невозврата, а не настройка.
        try:
            import mesh_identity
            back = []
            for p in (stored or []):
                back += mesh_identity.unrevoke_peer(url=str(p))
            if back:
                out["peers_returned"] = back
        except Exception as e:
            swallow.note('offload.admin_decentral_save:L657', e)
            out["peers_returned_error"] = str(e)
        return out
    except Exception as e:
        logger.error(f"/api/admin/decentral/save error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/admin/mesh/discovery")
def admin_mesh_discovery(request: Request):
    """Состояние анонсов и список найденных узлов для панели.

    Кандидаты — только то, что услышала эта машина. Ни один адрес отсюда не
    попадает в сетку: решение «раздавать поиск соседу» принимает администратор
    кнопкой, и путь добавления проходит через проверку адреса соседей.
    """
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        import mesh_discovery
        st = mesh_discovery.status()
        for c in st["candidates"]:
            c["in_grid"] = mesh_discovery.in_grid(c["url"])
        return {"ok": True, "discovery": st}
    except Exception as e:
        logger.error(f"/api/admin/mesh/discovery error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/mesh/discovery/add")
async def admin_mesh_discovery_add(request: Request):
    """Явное действие администратора: кандидат становится соседом."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            swallow.note('offload.admin_mesh_discovery_add:L695')
            body = {}
        node = str((body or {}).get("node") or "")[:40]
        import mesh_discovery, compute_grid
        found = [c for c in mesh_discovery.candidates(include_stale=True)
                 if c["node"] == node]
        if not found:
            return JSONResponse({"error": "кандидат не найден — его не "
                                          "слышала эта машина"}, status_code=404)
        cand = found[0]
        url = client_offload.clean_peer_url(cand["url"])
        if not url:
            return JSONResponse({"error": "адрес кандидата не прошёл проверку "
                                          "локальной сети"}, status_code=400)
        grid = (client_offload.decentral().get("grid") or {})
        peers = list(grid.get("peers") or [])
        if url in peers:
            return {"ok": True, "already": True, "peers": peers,
                    "discovery": mesh_discovery.status()}
        cap = max(1, min(client_offload.MAX_PEERS,
                         int(grid.get("max_peers") or 4)))
        if len(peers) >= cap:
            return JSONResponse({"error": "в сетке уже %d сосед(ов), предел %d — "
                                          "сначала убери адрес из списка"
                                          % (len(peers), cap)}, status_code=409)
        cfg = client_offload.save({"decentral": {"grid": {"peers": peers + [url]}}})
        mesh_discovery.mark_added(node)
        # Явное «добавить» от администратора снимает метку отзыва: иначе узел,
        # возвращённый в сетку, вечно получал отказ по отозванному ключу.
        returned = []
        try:
            import mesh_identity
            returned = mesh_identity.unrevoke_peer(node=node, url=url)
        except Exception:
            swallow.note('offload.admin_mesh_discovery_add:L729')
            pass
        stored = ((cfg.get("decentral") or {}).get("grid") or {}).get("peers") or []
        return {"ok": True, "added": url, "peers": stored,
                "returned": returned,
                "check": compute_grid.probe_peer(
                    url, timeout_ms=int(grid.get("peer_timeout_ms") or 1500)),
                "discovery": mesh_discovery.status()}
    except Exception as e:
        logger.error(f"/api/admin/mesh/discovery/add error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/mesh/discovery/forget")
async def admin_mesh_discovery_forget(request: Request):
    """Забыть кандидата. Соседа из сетки это не убирает — там свой список."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            swallow.note('offload.admin_mesh_discovery_forget:L751')
            body = {}
        node = str((body or {}).get("node") or "")[:40]
        import mesh_discovery
        return {"ok": True, "forgotten": mesh_discovery.forget(node),
                "discovery": mesh_discovery.status()}
    except Exception as e:
        logger.error(f"/api/admin/mesh/discovery/forget error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/decentral/revoke")
async def admin_decentral_revoke(request: Request):
    """Стоп-кран: все рубильники децентрализации выключаются разом,
    одобрения устройств отзываются, соединения закрываются.

    Помощники остаются в реестре с state=rejected, чтобы их история квот и
    задержек не потерялась; подключиться заново они смогут только после
    нового одобрения.
    """
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        cfg = client_offload.save({"decentral": client_offload.decentral_off()})
        # Стоп-кран обязан гасить и HTTPS-вход: «вся децентрализация выключена»
        # не может означать «но в сети висит ещё один открытый порт».
        tls_sidecar.apply({}, server_port=_server_port())
        # Стоп-кран гасит и h2-слушатель: открытый порт, оставшийся после
        # «всё выключено», — это дыра, а не недосмотр интерфейса.
        try:
            import h2_listener
            h2_listener.apply({}, server_port=_server_port())
        except Exception as _h2e:
            logger.debug("h2-вход после отзыва не погас: %s", _h2e)
        # Стоп-кран выключает и анонсы, и стирает список кандидатов: «вся
        # децентрализация выключена» не может означать «поток всё ещё слает
        # пакеты в сеть, а найденные адреса лежат на диске».
        try:
            import mesh_discovery
            mesh_discovery.stop()
            mesh_discovery.purge()
        except Exception as _de:
            logger.debug("анонсы после отзыва не погашены: %s", _de)
        # Стоп-кран гасит поток очереди и стирает её файл: в очереди лежат
        # промпты пользователя, а «вся децентрализация выключена» не может
        # означать «на диске остался чужой текст, который ещё и догонится».
        try:
            import inference_queue
            inference_queue.stop()
            out_q = inference_queue.purge()
        except Exception as _qe:
            out_q = {"error": str(_qe)}
            logger.debug("очередь инференса после отзыва не снята: %s", _qe)
        # Стоп-кран гасит и репликацию, и стирает её состояние: в снимке
        # лежат имена и версии файлов пользователя, а в бэкапах — содержимое
        # перезаписанных заметок. «Всё отозвано» не может означать «на диске
        # остались копии того, что уже ни с кем не согласовывается».
        try:
            import mesh_sync
            mesh_sync.stop()
            out_sync = mesh_sync.purge()
        except Exception as _se:
            out_sync = {"error": str(_se)}
            logger.debug("репликация после отзыва не снята: %s", _se)
        revoked = 0
        for h in client_offload.list_helpers(include_rejected=False):
            did = h.get("id")
            ws = client_offload.get_helper_ws(did)
            if ws is not None:
                try:
                    # Секрет личности устройству тоже не нужен: «отозвано»
                    # означает, что с этого момента оно ничего не подтверждает.
                    await ws.send_json({"action": "mesh_revoke", "device": did})
                except Exception:
                    swallow.note('offload.admin_decentral_revoke:L825')
                    pass
                try:
                    # Именно "rejected": этот сигнал клиент уже понимает и
                    # на нём останавливает помощника сам. Новое действие для
                    # существующих страниц было бы мёртвой строкой в протоколе.
                    await ws.send_json({"action": "rejected", "state": "rejected"})
                except Exception:
                    logger.debug("не удалось сообщить помощнику %s об отзыве", did)
                try:
                    await ws.close(code=1000)
                except Exception:
                    logger.debug("не удалось закрыть соединение помощника %s", did)
            if client_offload.reject_helper(did) is not None:
                revoked += 1
        # mesh.identity: секреты помощников и закреплённые ключи соседов уходят
        # вместе с одобрениями. Иначе «всё отозвано» оставляло бы на сервере
        # ключи, которыми чужое устройство подтверждает себя как своё.
        mesh_purged = {}
        try:
            import mesh_identity
            mesh_purged = mesh_identity.purge_all()
        except Exception as _me:
            logger.debug("mesh-секреты после отзыва не стёрты: %s", _me)
        logger.warning("Децентрализация отозвана: рубильники выключены, "
                       "одобрений отозвано: %d", revoked)
        return {"ok": True, "decentral": cfg.get("decentral") or {},
                "revoked_devices": revoked, "mesh_purged": mesh_purged,
                "inference_purged": out_q,
                "dispatch_live": client_offload.dispatch_enabled()}
    except Exception as e:
        logger.error(f"/api/admin/decentral/revoke error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Репликация записей (mesh.write_mode=lww) ───────────────────────────────


def _sync_guard(request, path, params):
    """Право соседа прочитать эту машину: адрес из сетки и, при включённой
    личности, корректная подпись.

    Порядок не меняется на обратный нигде: проверка адреса раньше подписи
    означает, что чужой хост не получает ни одного ответа этого модуля."""
    import mesh_sync
    ip = request.client.host if request.client else ""
    verdict = mesh_sync.incoming_ok(request.method, path, params,
                                   dict(request.headers), ip)
    if verdict.get("ok"):
        return None
    reason = str(verdict.get("reason") or "отказано")
    logger.info("[sync] отказ %s от %s: %s", path, ip, reason)
    return JSONResponse({"error": reason}, status_code=403)


@router.get("/api/mesh/sync/manifest")
def mesh_sync_manifest(request: Request):
    """Список ключей и версий этой машины для соседней копии.

    Только имена и mtime: содержимого здесь нет ни байта. Отдаётся исключительно
    узлам из decentral.grid.peers, и ровно то, что стоит в sync_modules."""
    try:
        deny = _sync_guard(request, "/api/mesh/sync/manifest", {})
        if deny is not None:
            return deny
        import mesh_sync
        return JSONResponse(mesh_sync.manifest())
    except Exception as e:
        logger.error(f"/api/mesh/sync/manifest error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/mesh/sync/record")
def mesh_sync_record(request: Request):
    """Одна запись по ключу. Ключ проверяется на принадлежность разрешённым
    каталогям до любого чтения с диска, поэтому «..» и путь наружу здесь не
    работают по построению, а не по случайности."""
    try:
        params = dict(request.query_params)
        deny = _sync_guard(request, "/api/mesh/sync/record", params)
        if deny is not None:
            return deny
        import mesh_sync
        rec = mesh_sync.record(params.get("key") or "")
        if not rec.get("ok"):
            return JSONResponse({"error": rec.get("why") or "нет записи"},
                                status_code=404)
        return JSONResponse(rec)
    except Exception as e:
        logger.error(f"/api/mesh/sync/record error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/mesh/sync/run")
async def admin_mesh_sync_run(request: Request):
    """Один проход вручную: «проверить, что сетка реально отдаёт мои файлы».

    Сетевой обход может занять минуты на большой библиотеке, поэтому проход
    идёт в фоне, а ответ приходит сразу — вешать страницу на чужой диск
    нельзя. Статус после нажатия обновляется сама панель."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        import mesh_sync
        if not mesh_sync.enabled():
            return JSONResponse({"error": "mesh.write_mode выключен — "
                                          "синхронизации нет"}, status_code=400)
        if mesh_sync.pass_running():
            return {"ok": True, "started": False, "why": "проход уже идёт"}
        peers = mesh_sync.peers()
        if not peers:
            return JSONResponse({"error": "в decentral.grid.peers нет соседей — "
                                          "синхронизироваться не с кем"},
                                status_code=400)

        def _bg():
            try:
                mesh_sync.pass_once()
            except Exception as e:
                logger.debug("[sync] ручной проход: %s", e)

        threading.Thread(target=_bg, daemon=True, name="infinity-mesh-sync-run").start()
        return {"ok": True, "started": True, "peers": len(peers)}
    except Exception as e:
        logger.error(f"/api/admin/mesh/sync/run error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/mesh/sync/purge")
async def admin_mesh_sync_purge(request: Request):
    """Стереть состояние репликации: снимок версий, надгробия и бэкапы.

    Заметки и статьи не удаляются — уходят только служебные копии модуля.
    Нужено после опытов с двумя копиями и перед «больше не синхронизироваться»."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        import mesh_sync
        return {"ok": True, "purged": mesh_sync.purge(),
                "sync": mesh_sync.status()}
    except Exception as e:
        logger.error(f"/api/admin/mesh/sync/purge error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/mesh/peer/revoke")
async def admin_mesh_peer_revoke(request: Request):
    """Отозвать одного соседа: убрать из сетки, забыть его ключ и перестать
    пускать его к своим файлам.

    Сосед — это машина, которой доверяют читать заметки. Удалять адрес из
    списка, оставляя закреплённый ключ и одобрение, значило бы «я перестал
    делиться», тогда как механизм всё ещё готов пускать."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            swallow.note('offload.admin_mesh_peer_revoke:L984')
            body = {}
        raw = str((body or {}).get("peer") or (body or {}).get("url") or "").strip()
        node = str((body or {}).get("node") or "").strip()
        if not raw and not node:
            return JSONResponse({"error": "не указан узел"}, status_code=400)
        url = client_offload.clean_peer_url(raw) if raw else ""
        if raw and not url:
            return JSONResponse({"error": "адрес не похож на соседа"}, status_code=400)
        grid = (client_offload.decentral().get("grid") or {})
        peers = [str(p) for p in (grid.get("peers") or [])]
        left = [p for p in peers if p.rstrip("/") != url.rstrip("/")] if url else list(peers)
        removed = len(left) < len(peers)
        cfg = client_offload.save({"decentral": {"grid": {"peers": left}}})
        forgotten = {}
        try:
            import mesh_identity
            if not node and url:
                # Узел помнят по ключу, а ключ закреплялся под id; если id не
                # прислали, ищем закрепление по адресу, чтобы не оставить
                # висящий чужой публичный ключ.
                for p, rec in mesh_identity.pinned_peers().items():
                    if str(rec.get("url") or "").rstrip("/") == url.rstrip("/"):
                        node = p
                        break
            if node:
                forgotten["peer_key"] = mesh_identity.revoke_peer(node)
            elif url:
                # Адреса нет ни в одном закреплении: значит, ключ этого соседа
                # мы ещё не видели. Сказать «отозвано» здесь — соврать.
                forgotten["peer_key"] = False
            forgotten["revoked"] = list(mesh_identity.revoked_peers().values())
        except Exception as e:
            swallow.note('offload.admin_mesh_peer_revoke:L1017', e)
            forgotten["error"] = str(e)
        try:
            import mesh_discovery
            forgotten["discovery"] = mesh_discovery.forget(str(node or url))
        except Exception:
            swallow.note('offload.admin_mesh_peer_revoke:L1023')
            pass
        # Отзыв по одному id без адреса — это тоже отзыв: ключ соседа стёрт, и
        # панель обязана видеть, что именно произошло, а не «успех» без разбора.
        return {"ok": True, "removed": removed, "peer": url, "node": node,
                "peers": left, "forgotten": forgotten,
                "decentral": cfg.get("decentral") or {}}
    except Exception as e:
        logger.error(f"/api/admin/mesh/peer/revoke error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/mesh/peer/unrevoke")
async def admin_mesh_peer_unrevoke(request: Request):
    """Вернуть отозванного соседа: снять метку отзыва.

    Без этого шага отзыв был бы точкой невозврата: метка не даёт ключу
    закрепиться заново, а значит машину, которой когда-то не доверили, нельзя
    вернуть тем же способом, которым её добавляли."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            swallow.note('offload.admin_mesh_peer_unrevoke:L1048')
            body = {}
        node = str((body or {}).get("node") or "").strip()
        raw = str((body or {}).get("url") or (body or {}).get("peer") or "").strip()
        url = client_offload.clean_peer_url(raw) if raw else ""
        if not node and not url:
            return JSONResponse({"error": "не указан узел"}, status_code=400)
        import mesh_identity
        back = mesh_identity.unrevoke_peer(node=node or None, url=url or None)
        return {"ok": True, "returned": back,
                "revoked": list(mesh_identity.revoked_peers().values()),
                "note": ("метка снята: следующий подписанный запрос соседа "
                         "закрепит его ключ заново" if back else
                         "такого отозванного узла не было")}
    except Exception as e:
        logger.error(f"/api/admin/mesh/peer/unrevoke error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Очередь инференса ───────────────────────────────────────────────────────


@router.get("/api/admin/inference/queue")
def admin_inference_queue(request: Request):
    """Что копится в очереди и догоняется ли. Только для админа: в записях
    лежат пользовательские промпты."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        import inference_queue
        return {"ok": True, "queue": inference_queue.status()}
    except Exception as e:
        logger.error(f"/api/admin/inference/queue error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/inference/queue/drain")
async def admin_inference_queue_drain(request: Request):
    """Догнать движок сейчас, не дожидаясь следующего хода потока.

    Ответ возвращается сразу: генерация занимает минуты, и вешать на неё
    запрос панели означало бы зависшую страницу.
    """
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        import inference_queue
        res = inference_queue.drain_now()
        return {"ok": True, "kick": res, "queue": inference_queue.status()}
    except Exception as e:
        logger.error(f"/api/admin/inference/queue/drain error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/inference/queue/forget")
async def admin_inference_queue_forget(request: Request):
    """Убрать одно задание: надобность отпала, а текст его остаётся на диске."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            swallow.note('offload.admin_inference_queue_forget:L1111')
            body = {}
        jid = str((body or {}).get("id") or "")[:40]
        if not jid:
            return JSONResponse({"error": "нет id задания"}, status_code=400)
        import inference_queue
        res = inference_queue.forget(jid)
        if not res.get("ok"):
            return JSONResponse({"error": "задание не найдено", "id": jid},
                                status_code=404)
        return {"ok": True, "removed": jid, "queue": inference_queue.status()}
    except Exception as e:
        logger.error(f"/api/admin/inference/queue/forget error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/inference/queue/purge")
async def admin_inference_queue_purge(request: Request):
    """Стереть очередь целиком вместе с файлом.

    Здесь чужой текст: промпты, которые не удалось отдать движку. Оставлять их
    «на всякий случай» после того, как человек нажал «стереть», нельзя.
    """
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        import inference_queue
        res = inference_queue.purge()
        return {"ok": True, "purged": res, "queue": inference_queue.status()}
    except Exception as e:
        logger.error(f"/api/admin/inference/queue/purge error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Защищённый вход: сертификат и автостарт ────────────────────────────────


@router.get("/api/tls/ca.pem")
def tls_ca_pem():
    """Публичная часть сертификата: её телефон и ставит в доверие.

    Без авторизации сознательно: сертификат не секрет — любой TLS-клиент
    получает его при рукопожатии в открытом виде. Наружу уходит только .pem с
    публичным ключом; приватный ключ приложение не отдаёт ни при каких
    условиях и лежит он отдельным файлом, который этим маршрутом не читается.
    """
    try:
        pem = tls_sidecar.ca_pem()
        if not pem:
            return JSONResponse({"error": "сертификат ещё не выпущен"}, status_code=404)
        return Response(content=pem, media_type="application/x-pem-file",
                        headers={"Content-Disposition":
                                 'attachment; filename="infinity-lan-ca.pem"',
                                 "Cache-Control": "no-store"})
    except Exception as e:
        logger.error(f"/api/tls/ca.pem error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _tls_autostart():
    """Поднять вход после старта приложения, если рубильник включён.

    Иначе после перезагрузки ПК телефон, закладкой которому стал
    https://…:8443, попадал бы в закрытый порт, пока кто-нибудь не зайдёт в
    админку. Таймер, а не прямой вызов на импорте: порт должен открываться,
    когда приложение уже принимает запросы, и сбой здесь не имеет права
    уронить старт сервера.
    """
    try:
        tls_sidecar.apply(client_offload.decentral(), server_port=_server_port())
    except Exception as e:
        logger.error(f"TLS autostart failed: {e}")


def _schedule_tls_autostart():
    try:
        t = ((client_offload.decentral() or {}).get("tls") or {})
        if not t.get("enabled"):
            return          # выключенный рубильник не должен поднимать поток и порт
        import threading
        threading.Timer(4.0, _tls_autostart).start()
    except Exception as e:
        logger.error(f"TLS autostart schedule failed: {e}")


_schedule_tls_autostart()


# ── Эталонная стоимость задач на сервере ───────────────────────────────────

_baseline_task = {"id": None, "started_at": 0.0}


@router.get("/api/admin/offload/baseline")
def admin_offload_baseline_get(request: Request):
    """Что задачи стоят на сервере + статус последнего замера."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        out = offload_baseline.describe()
        run = {}
        if _baseline_task["id"]:
            try:
                import worker_pool
                run = worker_pool.task_status(_baseline_task["id"]) or {}
            except Exception:
                swallow.note('offload.admin_offload_baseline_get:L1217')
                run = {}
        out["run"] = {k: run.get(k) for k in ("status", "progress", "message", "error")
                      if run}
        return out
    except Exception as e:
        logger.error(f"/api/admin/offload/baseline error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/offload/baseline")
def admin_offload_baseline_run(request: Request):
    """Запустить замер. Тяжело и по-настоящему грузит CPU — только по кнопке."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        try:
            import worker_pool
        except Exception:
            # Без фонового пула нечего гонять 10-секундный прогон по запросу.
            return JSONResponse({"error": "фоновые задачи недоступны"},
                                status_code=503)
        if _baseline_task["id"]:
            try:
                st = (worker_pool.task_status(_baseline_task["id"]) or {}).get("status")
            except Exception:
                swallow.note('offload.admin_offload_baseline_run:L1243')
                st = None
            if st == "running":
                return {"ok": True, "task_id": _baseline_task["id"],
                        "already_running": True}

        def _job():
            res = offload_baseline.run_all(repeats=3)
            offload_baseline.save(res)
            return len(res.get("tasks") or {})

        _baseline_task["id"] = worker_pool.background_task(_job)
        _baseline_task["started_at"] = time.time()
        return {"ok": True, "task_id": _baseline_task["id"],
                "hint": "Прогон грузит CPU и занимает ~10 секунд"}
    except Exception as e:
        logger.error(f"/api/admin/offload/baseline run error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def _run_test_job(device_id, task, timeout=6.0):
    """Отправить помощнику тестовую задачу и дождаться результата.

    Транспорт живёт в helper_channel: тест и настоящая раздача обязаны идти
    одним кодом, иначе «проверка помощника» подтверждает не тот путь, по
    которому потом поедут данные.
    """
    return await helper_channel.submit(device_id, task, "infinity",
                                       timeout_ms=int(timeout * 1000))


@router.post("/api/admin/offload/test")
async def admin_offload_test(request: Request):
    """Проверить помощников: разослать тестовую задачу и собрать ответы."""
    try:
        if not _admin_ok(request):
            return JSONResponse({"error": "Доступ запрещён"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            swallow.note('offload.admin_offload_test:L1283')
            body = {}
        task = str((body or {}).get("task") or "hash_sha256")
        if task not in client_offload.VALID_TASKS:
            return JSONResponse({"error": "неизвестная задача"}, status_code=400)
        helpers = [h for h in client_offload.list_helpers() if h.get("approved")]
        if not helpers:
            return {"ok": True, "results": [],
                    "hint": "Нет одобренных устройств-помощников"}
        results = []
        for h in helpers:
            if not h.get("online"):
                results.append({"device": h["id"], "ok": False, "error": "не в сети"})
                continue
            if task not in (h.get("tasks") or []):
                results.append({"device": h["id"], "ok": False,
                                "error": f"устройство не разрешило {task}"})
                continue
            res = await _run_test_job(h["id"], task)
            client_offload.helper_event(h["id"], bool(res.get("ok")))
            # Панель показывает «ms» как время ответа: это roundtrip сервера,
            # а не только счёт помощника — иначе цифра выдавала бы скорость
            # сети за скорость устройства.
            res.setdefault("ms", res.get("roundtrip_ms"))
            results.append({"device": h["id"], "name": h.get("name"),
                            **{k: v for k, v in res.items() if k not in ("job_id", "device_id")}})
        return {"ok": True, "results": results, "task": task}
    except Exception as e:
        logger.error(f"/api/admin/offload/test error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Офлайн-оболочка страниц (рубильник «децентрализация → офлайн») ─────────

_SW_OFFLINE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "static", "sw_offline.js")


@router.get("/sw_offline.js")
def sw_offline_file():
    """Service worker отдаётся с корня, иначе его scope не покроет /news.

    Файл читаем на каждый запрос и ставим no-store: правка офлайн-оболочки
    должна доходить до телефона без пересборки, а устаревший sw.js в кэше
    браузера означал бы, что рубильник в админке перестал его слушаться.
    """
    try:
        with open(_SW_OFFLINE_PATH, "r", encoding="utf-8") as f:
            body = f.read()
    except OSError:
        swallow.note('offload.sw_offline_file:L1333')
        return Response("", status_code=404, media_type="text/plain")
    return Response(body, media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})


@router.get("/api/offline/news")
async def offline_news_snapshot(request: Request, limit: int = 200, inline: int = 0):
    """Один самодостаточный HTML со статьями: телефон читает его без ПК.

    Рубильник проверяется здесь, а не только в интерфейсе: выключенный
    офлайн обязан не отдавать данные, иначе настройка — украшение.
    """
    if not client_offload.offline_enabled("news"):
        return JSONResponse({"error": "локальные копии выключены"}, status_code=403)
    try:
        import news_module
        import offline_export

        n = max(1, min(offline_export.MAX_LIMIT, int(limit or 200)))
        budget = _offline_budget()
        arts = await asyncio.to_thread(news_module.get_articles, None, None, n, 0, None, None)
        body = offline_export.build_news(arts)
        # Копия не должна весить больше бюджета устройства: ужимаем число
        # записей, а не текст, чтобы страница осталась читаемой.
        while len(body.encode("utf-8")) > budget * 0.8 and n > 20:
            n = max(20, n // 2)
            arts = arts[:n]
            body = offline_export.build_news(arts)
        return _offline_response(body, "news", inline)
    except Exception as e:
        logger.error(f"/api/offline/news error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _offline_budget():
    """Байтовый лимит копии из админки: файл не должен быть больше того,
    что пользователь разрешил занимать на устройстве."""
    cfg = (client_offload.decentral().get("offline") or {})
    return max(1, int(cfg.get("storage_mb") or 20)) * 1048576


def _offline_response(body, kind, inline):
    name = f"infinity-{kind}-{time.strftime('%Y%m%d-%H%M')}.html"
    disp = "inline" if inline else "attachment"
    return Response(body, media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": f'{disp}; filename="{name}"',
                             "Cache-Control": "no-store"})


@router.get("/api/offline/notes", dependencies=[Depends(profile_paths.bind)])
async def offline_notes_snapshot(request: Request, limit: int = 200, inline: int = 0):
    """Автономная копия заметок.

    Список берётся ровно тот, что видит страница: list_notes сам ставит
    закреплённые и избранные наверх, а корзину и архив не показывает.
    """
    if not client_offload.offline_enabled("notes"):
        return JSONResponse({"error": "локальные копии выключены"}, status_code=403)
    try:
        import notes_model_module
        import offline_export
        import server

        notes_dir = profile_paths.current("NOTES_DIR", server)
        n = max(1, min(offline_export.MAX_LIMIT, int(limit or 200)))
        budget = _offline_budget()
        res = await asyncio.to_thread(notes_model_module.list_notes, notes_dir,
                                      "", "date", "active", "", "", n)
        rows = (res or {}).get("articles") or []
        body = offline_export.build_notes(rows)
        while len(body.encode("utf-8")) > budget * 0.8 and n > 20:
            n = max(20, n // 2)
            rows = rows[:n]
            body = offline_export.build_notes(rows)
        return _offline_response(body, "notes", inline)
    except Exception as e:
        logger.error(f"/api/offline/notes error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# Замер на реальной библиотеке: 49 fb2 разбираются за 2.7 с, один PDF — за
# 60.8 с. Копия книг отдаётся по кнопке в обычном запросе, и держать на одном
# томе минуту нельзя (это и есть тот самый CPU, который мы разгружаем), поэтому
# медленные форматы в копию текстом не берутся, а остальные ограничены
# дедлайном: он не прерывает начатое чтение, но обрывает очередь за ним.
_SLOW_PARSE_EXT = (".pdf",)
TEXT_READ_DEADLINE_S = 20


def _read_book_for_copy(fp, ext):
    """Полный текст книги для копии.

    get_book_content не берём: он держит весь текст каждого тома в
    _book_cache без ограничения размера, и одна скачанная копия библиотеки
    освобождалась бы только перезапуском. _read_book_file — та же разборка
    файлов, но без мемоизации.
    """
    import server

    data = books_read._read_book_file(fp, ext)
    if not isinstance(data, dict) or data.get("error"):
        return None
    text = "\n\n".join(str((c or {}).get("text") or "") for c in (data.get("chapters") or []))
    return (data.get("title") or "", data.get("author") or "",
            data.get("annotation") or "", text)


@router.get("/api/offline/books")
async def offline_books_snapshot(request: Request, limit: int = 200, inline: int = 0):
    """Автономная копия библиотеки: список всегда, тексты — сколько влезло.

    Библиотека заведомо больше бюджета устройства, поэтому тексты читаются
    по порядку до бюджета, а не выбираются наугад: пользователю важнее
    договорить начатую книгу, чем получить обрывки всех сразу. Форматы, разбор
    которых дольше самого запроса (PDF), текстом не берутся — причина
    подписывается в копии, том остаётся в списке.
    """
    if not client_offload.offline_enabled("books"):
        return JSONResponse({"error": "локальные копии выключены"}, status_code=403)
    try:
        import offline_export
        import server

        n = max(1, min(offline_export.MAX_LIMIT, int(limit or 200)))
        budget = _offline_budget()
        books = await asyncio.to_thread(books_read.find_books)
        books = books[:n]
        deadline = time.monotonic() + TEXT_READ_DEADLINE_S

        def _read(b):
            fp = b.get("path") or ""
            ext = os.path.splitext(fp)[1].lower()
            if ext in _SLOW_PARSE_EXT:
                raise offline_export.SkipText(
                    f"Текст {ext.upper()[1:]} сервер разбирает слишком долго "
                    "для одного запроса — в копию том лёг списком, читается на ПК")
            if time.monotonic() > deadline:
                raise offline_export.SkipText(
                    "Время на разбор текстов для копии истекло — том лёг списком")
            return _read_book_for_copy(fp, ext)

        # 0.92: в файл, помимо текста, ложатся разметка статьи, аннотации и
        # оболочка страницы. Текст считается по экранированным байтам
        # (build_books), поэтому на остальную вёрстку хватает этого запаса, а
        # копия остаётся ровно в обещанном пользователю бюджете.
        body = await asyncio.to_thread(offline_export.build_books, books, _read,
                                       int(budget * 0.92))
        if len(body.encode("utf-8")) > budget:
            # Тексты не влезли даже ужатые — отдаём список: он маленький,
            # пустая страница вместо него была бы хуже.
            body = await asyncio.to_thread(offline_export.build_books, books, None, 0)
            while len(body.encode("utf-8")) > budget and n > 20:
                n = max(20, n // 2)
                body = offline_export.build_books(books[:n], None, 0)
        return _offline_response(body, "books", inline)
    except Exception as e:
        logger.error(f"/api/offline/books error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


_EXPIRED = "(m.ttl IS NULL OR m.ttl=0 OR m.created_at+m.ttl > ?)"


def _read_messages_for_copy(db_path, profile_id, conv_limit, msg_per_conv):
    """Диалоги и их последние сообщения — только чтение.

    Функции social_module для этого не годятся: get_messages при каждом
    вызове удаляет просроченные ttl-строки и пишет last_read_at, то есть
    «безобидное» скачивание копии изменило бы базу и сбросило непрочитанное.
    mode=ro сюда не ставим: у WAL-базы без -shm файла такой открытие
    отказывается работать, а запись здесь исключена тем, что запросов на
    запись нет.
    """
    import sqlite3

    if not db_path or not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        now = time.time()
        convs = [dict(r) for r in conn.execute(
            f"SELECT c.id, c.title, c.is_group, "
            f"(SELECT created_at FROM social_messages m WHERE m.conversation_id=c.id "
            f" AND {_EXPIRED} ORDER BY created_at DESC LIMIT 1) AS last_at "
            f"FROM social_conversations c "
            f"JOIN social_conversation_members cm ON c.id = cm.conversation_id "
            f"WHERE cm.profile_id = ? "
            f"ORDER BY cm.is_pinned DESC, last_at DESC LIMIT ?",
            [now, profile_id, max(1, int(conv_limit))]).fetchall()]
        if not convs:
            return []
        ph = ",".join(["?"] * len(convs))
        others = conn.execute(
            f"SELECT cm.conversation_id, cm.profile_id, u.display_name "
            f"FROM social_conversation_members cm "
            f"LEFT JOIN social_users u ON cm.profile_id = u.profile_id "
            f"WHERE cm.conversation_id IN ({ph})", [c["id"] for c in convs]).fetchall()
        members_by = {}
        for r in others:
            members_by.setdefault(r["conversation_id"], []).append(dict(r))
        out = []
        for c in convs:
            ms = members_by.get(c["id"]) or []
            # У диалога один-на-один title пустой: страница показывает имя
            # собеседника, и копии нужно то же самое.
            names = [str(m.get("display_name") or m.get("profile_id") or "")
                     for m in ms if m.get("profile_id") != profile_id]
            msgs = conn.execute(
                f"SELECT m.content, m.created_at, m.attachments, "
                f"COALESCE(u.display_name, m.sender_id) AS sender "
                f"FROM social_messages m LEFT JOIN social_users u ON m.sender_id = u.profile_id "
                f"WHERE m.conversation_id = ? AND {_EXPIRED} "
                f"ORDER BY m.created_at DESC LIMIT ?",
                [c["id"], now, msg_per_conv]).fetchall()
            out.append({"id": c["id"],
                        "title": c.get("title") or ", ".join(nm for nm in names if nm),
                        "members": ms,
                        "messages": [_message_row(m) for m in reversed(msgs)]})
        return out
    finally:
        conn.close()


def _message_row(m):
    """Строка сообщения для копии.

    attachments в базе — это '[]' у большинства сообщений: для копии это не
    вложение, а пустой список, и печатать на него «только на ПК» значит
    забить заглушками всю переписку.
    """
    raw = m["attachments"]
    att = str(raw or "").strip()
    return {"sender": m["sender"], "content": m["content"],
            "created_at": m["created_at"],
            "attachments": "" if att in ("", "[]", "null") else att}


@router.get("/api/offline/feed")
async def offline_feed_snapshot(request: Request, limit: int = 50, inline: int = 0):
    """Автономная копия переписки текущего профиля.

    Медиа в копию не едет: файлы лежат на ПК и без него не отдаются, поэтому
    вместо них остаётся честная пометка.
    """
    if not client_offload.offline_enabled("feed"):
        return JSONResponse({"error": "локальные копии выключены"}, status_code=403)
    try:
        import offline_export
        import social_module

        profile_id = social_module._get_active_profile(request)
        n = max(1, min(offline_export.CONV_LIMIT, int(limit or offline_export.CONV_LIMIT)))
        budget = _offline_budget()
        convs = await asyncio.to_thread(
            _read_messages_for_copy, social_module.DB_PATH, profile_id, n,
            offline_export.MSG_PER_CONV)
        body = offline_export.build_messages(convs)
        # Сначала режем диалоги, потом сообщения внутри: список тем важнее,
        # чем хвосты самой длинной переписки.
        while len(body.encode("utf-8")) > budget * 0.8 and n > 1:
            n = max(1, n // 2)
            convs = convs[:n]
            body = offline_export.build_messages(convs)
        k = offline_export.MSG_PER_CONV
        while len(body.encode("utf-8")) > budget * 0.8 and k > 20:
            k = max(20, k // 2)
            for c in convs:
                c["messages"] = c["messages"][:k]
            body = offline_export.build_messages(convs)
        return _offline_response(body, "feed", inline)
    except Exception as e:
        logger.error(f"/api/offline/feed error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── WebSocket помощника ────────────────────────────────────────────────────


async def _mesh_handshake(websocket, did):
    """Подтверждение личности помощника (decentral.mesh.identity).

    Три состояния, и первое — ровно прежний код: при выключенном флаге здесь
    не происходит ничего, ни одного лишнего сообщения и ни одного ожидания.
    Включённый флаг выдаёт устройствам секрет и просит подтвердить его
    подписью; отсеивает неподтверждённых только второй рубильник — required.
    """
    try:
        import mesh_identity
    except Exception:
        swallow.note('offload._mesh_handshake:L1624')
        return True
    if not mesh_identity.enabled():
        return True
    try:
        secret = mesh_identity.issue_secret(did)
        if secret:
            await websocket.send_json({"action": "mesh_key", "device": did,
                                       "secret": secret})
        nonce = mesh_identity.begin_challenge(did)
        if not nonce:
            return True
        await websocket.send_json({"action": "mesh_challenge", "device": did,
                                   "nonce": nonce})
        if not mesh_identity.required():
            return True
        # Ждём ровно одно подтверждение: поллинг состояния и ping идут после
        # hello, раньше них помощник ничего успевает прислать, а старому
        # клиенту, который не знает про подписи, required и не нужен.
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            msg = json.loads(raw)
        except Exception:
            swallow.note('offload._mesh_handshake:L1647')
            msg = {}
        if msg.get("action") == "mesh_proof":
            verdict = mesh_identity.verify_proof(did, msg.get("nonce"), msg.get("mac"))
        else:
            verdict = {"ok": False, "state": "unsigned",
                       "reason": "клиент не подтвердил личность"}
        client_offload.note_identity(did, verdict.get("state") or "failed")
        return bool(verdict.get("ok"))
    except Exception as e:
        logger.debug("[mesh] handshake %s: %s", did, e)
        # Сломанная проверка не должна ронять то, что она не защищала;
        # но если required включён, без проверки пускать нельзя.
        return not mesh_identity.required()


@router.websocket("/api/offload/helper/ws")
async def offload_helper_ws(websocket: WebSocket):
    cfg = client_offload.load()
    helpers_cfg = cfg.get("helpers") or {}
    q = websocket.query_params
    consent = (q.get("consent") or "") == "1"
    if not cfg.get("enabled") or not helpers_cfg.get("enabled"):
        await websocket.close(code=1008)
        return
    if helpers_cfg.get("require_consent", True) and not consent:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    device_id = (q.get("device") or "").strip()[:80] or uuid.uuid4().hex[:16]
    tasks = [t.strip() for t in (q.get("tasks") or "").split(",") if t.strip()]
    info = {
        "name": q.get("name") or "",
        "ua": websocket.headers.get("user-agent") or "",
        "ip": websocket.client.host if websocket.client else "",
        "tasks": tasks,
    }
    did, err, state = client_offload.register_helper(websocket, device_id, info)
    if err:
        try:
            await websocket.send_json({"action": "error", "error": err,
                                       "state": state})
        except Exception:
            swallow.note('offload.offload_helper_ws:L1690')
            pass
        await websocket.close(code=1008)
        return
    try:
        await websocket.send_json({"action": "hello", "state": state,
                                   "tasks": client_offload.public_config().get("helper_tasks") or []})
        # Сокет живёт на этом loop; синхронный код (скан фототеки в потоке)
        # попадает к нему только через эту привязку.
        helper_channel.bind_loop(device_id=did)
        if not await _mesh_handshake(websocket, did):
            try:
                await websocket.send_json({"action": "error", "state": "identity_failed",
                                           "error": "личность устройства не подтверждена"})
            except Exception:
                swallow.note('offload.offload_helper_ws:L1705')
                pass
            await websocket.close(code=1008)
            return
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                swallow.note('offload.offload_helper_ws:L1714')
                continue
            action = msg.get("action")
            if action == "ping":
                client_offload.touch_helper(did)
                await websocket.send_json({"action": "pong",
                                           "state": client_offload.helper_state(did)})
            elif action == "state":
                await websocket.send_json({"action": "state",
                                           "state": client_offload.helper_state(did)})
            elif action == "job_result":
                helper_channel.resolve(msg.get("job_id"), msg)
            elif action == "stats":
                client_offload.touch_helper(did)
            elif action == "mesh_proof":
                # Подтверждение прилетело уже в рабочем цикле: required выключен,
                # поэтому соединение не прерывается, а отметка просто сохраняется.
                import mesh_identity
                verdict = mesh_identity.verify_proof(did, msg.get("nonce"), msg.get("mac"))
                client_offload.note_identity(did, verdict.get("state") or "failed")
    except WebSocketDisconnect:
        swallow.note('offload.offload_helper_ws:L1735')
        pass
    except Exception as e:
        logger.debug(f"[offload] helper ws error {did}: {e}")
    finally:
        helper_channel.unbind_loop(did)
        client_offload.unregister_helper(did)
