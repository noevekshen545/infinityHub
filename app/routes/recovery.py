"""Recovery Guard 3.0 — API настроек (Слой 1, guard.md §2).

GET  /api/admin/recovery/config        — текущие настройки + схема для UI
POST /api/admin/recovery/config        — частичное/полное обновление с валидацией
POST /api/admin/recovery/config/reset  — сброс к умолчаниям
GET  /api/admin/recovery/disk          — место на диске (кнопка «проверить место»)

Доступ — только админ (как остальные /api/admin/*): routes.admin._admin_ok.
Значения читаются с диска при каждом обращении (recovery_core.config_value),
поэтому применяются без рестарта сервера.

Только stdlib + fastapi (уже в проекте).
"""

import os
import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse

import recovery_core as rc
import recovery_slots as rs
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()


def _require_admin(request: Request) -> bool:
    """Ленивая админ-проверка: модуль грузится без server/admin_module,
    а тесты могут подменить функцию целиком."""
    from routes.admin import _admin_ok
    return _admin_ok(request)


def _auth_error():
    return JSONResponse({"error": "Доступ запрещён"}, status_code=403)


def _normalize(body):
    """Плоские dotted-ключи («security.ban_burst») -> вложенная структура."""
    clean = {}
    for key, val in body.items():
        if "." in str(key):
            rc._set_path(clean, str(key), val)
        else:
            clean[key] = val
    return clean


def _unknown_keys(obj, prefix=""):
    """Пути вида security.ban_burst, которых нет в схеме (опечатки UI).

    Ловит и мусорные вложенные скалярные ключи ({'security': 'oops'}).
    """
    unknown = []
    for key, val in (obj or {}).items():
        dotted = f"{prefix}{key}"
        if isinstance(val, dict):
            unknown += _unknown_keys(val, dotted + ".")
        elif dotted not in rc._CONFIG_SCHEMA:
            unknown.append(dotted)
    return unknown


@router.get("/api/admin/recovery/config")
def recovery_config_get(request: Request):
    """Текущие настройки + схема (тип/диапазон/варианты) для рендера UI."""
    if not _require_admin(request):
        return _auth_error()
    try:
        schema = {}
        for dotted, (kind, lo, hi, allowed) in rc._CONFIG_SCHEMA.items():
            schema[dotted] = {
                "type": kind,
                "min": lo,
                "max": hi,
                "choices": allowed,
                "default": rc._get_path(rc.DEFAULT_CONFIG, dotted),
            }
        return {
            "ok": True,
            "config": rc.load_config(),
            "defaults": rc.DEFAULT_CONFIG,
            "schema": schema,
        }
    except Exception as e:
        logger.error(f"recovery config GET error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/admin/recovery/config")
async def recovery_config_post(request: Request):
    """Обновить настройки. Тело — JSON (частичный патч, вложенный или
    с плоскими dotted-ключами). Невалидные значения НЕ применяются."""
    if not _require_admin(request):
        return _auth_error()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "errors": {"_body": "ожидается JSON"}},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "errors": {"_body": "ожидается объект"}},
                            status_code=400)
    body = _normalize(body)
    unknown = _unknown_keys(body)
    if unknown:
        return JSONResponse(
            {"ok": False,
             "errors": {u: "параметр отсутствует в схеме (опечатка?)"
                        for u in unknown}},
            status_code=400)
    ok, payload = rc.save_config(body)
    if not ok:
        return JSONResponse(payload, status_code=400)
    rc.audit("config_change", result="ok", keys=sorted(body))
    logger.info(f"[recovery] config updated: {sorted(body)}")
    payload["applied"] = "runtime"   # читается с диска — без рестарта
    return payload


@router.post("/api/admin/recovery/config/reset")
async def recovery_config_reset(request: Request):
    """Сброс к умолчаниям: файл настроек переписывается начисто."""
    if not _require_admin(request):
        return _auth_error()
    try:
        # os.remove и перезапись конфига — диск; в тред, как и остальное в файле
        def _reset():
            if os.path.isfile(rc.CONFIG_PATH):
                os.remove(rc.CONFIG_PATH)
            return rc.save_config({})

        ok, payload = await asyncio.to_thread(_reset)
        if not ok:
            return JSONResponse(payload, status_code=500)
        logger.info("[recovery] config reset to defaults")
        payload["applied"] = "runtime"
        return payload
    except Exception as e:
        logger.error(f"recovery config reset error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/admin/recovery/disk")
async def recovery_disk(request: Request):
    """Место: свободно, занято снимками, лимит, остаток до чистки."""
    if not _require_admin(request):
        return _auth_error()
    try:
        # dir_size и полный обход снимков — тысячи stat-вызовов; на event loop
        # они встали бы поперёк всех параллельных запросов, поэтому в тред.
        used_bytes = await asyncio.to_thread(rc.dir_size, rc.BACKUPS_DIR)
        count = len(await asyncio.to_thread(rc.list_backups, False))
        cfg = rc.load_config()
        limit = int(cfg.get("max_total_mb", 500))
        used_mb = round(used_bytes / 1048576, 2)
        return {"ok": True, "free_mb": rc.free_mb(),
                "backups_mb": used_mb,
                "max_total_mb": limit,
                "headroom_mb": round(limit - used_mb, 2),
                "count": count,
                }
    except Exception as e:
        logger.error(f"recovery disk GET error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Снимки (guard.md §3): список, ручной снимок, пин, удаление, проверка
# ---------------------------------------------------------------------------

@router.get("/api/admin/recovery/status")
async def recovery_status(request: Request):
    """Сводка для вкладки «Обзор» (core_status)."""
    if not _require_admin(request):
        return _auth_error()
    try:
        return {"ok": True, **(await asyncio.to_thread(rc.core_status))}
    except Exception as e:
        logger.error(f"recovery status GET error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/admin/recovery/backups")
async def recovery_backups(request: Request):
    """Список ГОТОВЫХ снимков + статистика хранилища."""
    if not _require_admin(request):
        return _auth_error()
    try:
        # список с проверкой целостности и сводка считаются одним обходом,
        # иначе вкладка платит за полный stat-обход дважды подряд
        items = await asyncio.to_thread(rc.list_backups)
        return {"ok": True, "backups": items,
                "stats": rc.backups_stats(items)}
    except Exception as e:
        logger.error(f"recovery backups GET error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/admin/recovery/snapshot")
async def recovery_snapshot(request: Request):
    """Ручной снимок. Тело опционально: {"force": true} — минуя дедуп/интервал.

    Обычный режим идёт через maybe_snapshot: снимок не создаётся, если
    «ничего не изменилось» или прошло мало времени.
    """
    if not _require_admin(request):
        return _auth_error()
    try:
        try:
            body = await request.json()
        except Exception:
            swallow.note('recovery.recovery_snapshot:L215')
            body = {}
        force = bool(isinstance(body, dict) and body.get("force"))
        # ручной снимок — копирование дерева; на event loop это секунды простоя
        snap_id, reason = await asyncio.to_thread(
            lambda: rc.maybe_snapshot(reason="manual", kind="manual",
                                      force=force))
        if snap_id:
            logger.info(f"[recovery] manual snapshot {snap_id} (force={force})")
            return {"ok": True, "id": snap_id, "created": True}
        return {"ok": True, "created": False, "reason": reason}
    except Exception as e:
        logger.error(f"recovery snapshot POST error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
@router.post("/api/admin/recovery/backups/{snap_id}/pin")
async def recovery_pin(snap_id: str, request: Request):
    """Закрепить/снять пин: тело {"pinned": true|false} (по умолчанию true)."""
    if not _require_admin(request):
        return _auth_error()
    try:
        try:
            body = await request.json()
        except Exception:
            swallow.note('recovery.recovery_pin:L238')
            body = {}
        pinned = True if not isinstance(body, dict) or "pinned" not in body \
            else bool(body["pinned"])
        ok, msg = rc.pin_snapshot(snap_id, pinned)
        if not ok:
            return JSONResponse({"ok": False, "error": msg}, status_code=404)
        return {"ok": True, "id": snap_id, "pinned": pinned}
    except Exception as e:
        logger.error(f"recovery pin POST error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.delete("/api/admin/recovery/backups/{snap_id}")
async def recovery_delete(snap_id: str, request: Request):
    """Удалить снимок. Pinned не удаляются без force=true."""
    if not _require_admin(request):
        return _auth_error()
    try:
        force = False
        try:
            body = await request.json()
            force = bool(isinstance(body, dict) and body.get("force"))
        except Exception:
            swallow.note('recovery.recovery_delete:L262')
            pass
        # нужен только флаг pinned — проверка целостности каждого снимка тут
        # не спросили бы, а стоит она сотни миллисекунд на event loop
        items = {i["id"]: i for i in
                 await asyncio.to_thread(rc.list_backups, False)}
        if snap_id not in items:
            return JSONResponse({"ok": False, "error": "снимок не найден"},
                                status_code=404)
        if items[snap_id].get("pinned") and not force:
            return JSONResponse(
                {"ok": False, "error": "снимок закреплён — снимите пин "
                                       "или передайте force"},
                status_code=409)
        ok, msg = await asyncio.to_thread(rc.delete_snapshot, snap_id)
        if not ok:
            return JSONResponse({"ok": False, "error": msg}, status_code=404)
        logger.info(f"[recovery] snapshot {snap_id} deleted")
        return {"ok": True, "id": snap_id}
    except Exception as e:
        logger.error(f"recovery delete DELETE error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/admin/recovery/backups/{snap_id}/verify")
async def recovery_verify(snap_id: str, request: Request):
    """Проверить снимок: {"level": "L1"|"L2"|"L3"} (по умолчанию из конфига).

    L2/L3 — subprocess/smoke-boot, запрос может идти секунды.
    """
    if not _require_admin(request):
        return _auth_error()
    try:
        try:
            body = await request.json()
        except Exception:
            swallow.note('recovery.recovery_verify:L298')
            body = {}
        level = body.get("level") if isinstance(body, dict) else None
        level = level or rc.config_value("verify_on_create") or "L1"
        if level not in ("L1", "L2", "L3"):
            return JSONResponse(
                {"ok": False, "error": f"уровень L1/L2/L3, получено {level!r}"},
                status_code=400)
        snap_dir = rc.snapshot_path(snap_id)
        if not os.path.isdir(snap_dir):
            return JSONResponse({"ok": False, "error": "снимок не найден"},
                                status_code=404)
        # L2/L3 поднимает smoke-boot в subprocess — секунды; не на event loop
        res = await asyncio.to_thread(
            lambda: rc.verify_snapshot(snap_dir, level=level))
        rc.audit("verify", snap_id=snap_id,
                 result="ok" if res.get("ok") else "bad", level=level)
        return {"ok": True, "id": snap_id, "result": res}
    except Exception as e:
        logger.error(f"recovery verify POST error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------- слоты A/B --

@router.get("/api/admin/recovery/slots")
def recovery_slots_status(request: Request):
    """Статус A/B-слотов: активный, содержимое, quarantine (guard.md §4)."""
    if not _require_admin(request):
        return _auth_error()
    try:
        return {"ok": True, **rs.slots_status()}
    except Exception as e:
        logger.error(f"recovery slots GET error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/admin/recovery/slots/restore")
async def recovery_slots_restore(request: Request):
    """Восстановить КОД из снимка в неактивный слот.

    body: {"id": "<snap>", "canary": true, "slot": "a"|"b" (необяз.)}.
    L3-канарейка — по умолчанию вкл.; после успеха слот становится
    активным (гвард запустит main.py из него при следующем старте).
    Запрос может идти десятки секунд (распаковка + smoke-boot).
    """
    if not _require_admin(request):
        return _auth_error()
    try:
        try:
            body = await request.json()
        except Exception:
            swallow.note('recovery.recovery_slots_restore:L350')
            body = {}
        body = body if isinstance(body, dict) else {}
        snap_id = str(body.get("id") or "").strip()
        if not snap_id:
            return JSONResponse({"ok": False, "error": "нет id снимка"},
                                status_code=400)
        canary = bool(body.get("canary", True))
        slot = body.get("slot")
        # распаковка + L3-канарейка — десятки секунд; на event loop такой вызов
        # значит «сервер не отвечает» и guard бы его убил
        report = await asyncio.to_thread(
            lambda: rs.restore_to_slot(snap_id, slot=slot, canary=canary))
        if not report.get("ok"):
            status = 404 if report.get("stage") in (
                "integrity", "manifest") else 409
            return JSONResponse(report, status_code=status)
        rc.audit("restore", snap_id=snap_id,
                 result=f"slot={report.get('target_slot')}",
                 files=report.get("restored", 0))
        logger.info(f"[recovery] slot restore {snap_id} -> "
                    f"{report.get('target_slot')}")
        return report
    except Exception as e:
        logger.error(f"recovery slots restore POST error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/admin/recovery/slots/activate")
async def recovery_slots_activate(request: Request):
    """Переключить активный слот: {"slot": "a"|"b"}.

    Откат (A→B) и «откат отката» (B→A) — мгновенно, без распаковки.
    """
    if not _require_admin(request):
        return _auth_error()
    try:
        try:
            body = await request.json()
        except Exception:
            swallow.note('recovery.recovery_slots_activate:L390')
            body = {}
        slot = str((body or {}).get("slot") or "").strip()
        if slot not in rs.SLOT_NAMES:
            return JSONResponse(
                {"ok": False, "error": "slot — 'a' или 'b'"},
                status_code=400)
        res = rs.activate_slot(slot)
        if not res.get("ok"):
            return JSONResponse(res, status_code=409)
        logger.info(f"[recovery] slot {slot} activated")
        return res
    except Exception as e:
        logger.error(f"recovery slots activate POST error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ------------------------------------------- режим восстановления (§4.1) --

@router.get("/api/admin/recovery/recovery-mode")
def recovery_mode_status(request: Request):
    """Режим восстановления: режим, restore_lock, авто-откат, boot_ok.

    Плюс `auto_restore_suggested` — кандидат, предложенный режимом ask.
    """
    if not _require_admin(request):
        return _auth_error()
    try:
        state = rs._load_state()
        return {
            "ok": True,
            "mode": rs.recovery_mode_get(),
            "lock": rs.restore_lock_status(),
            "boot_ok": rs.boot_ok(),
            "auto_restore_mode": rc.config_value("auto_restore_mode", "ask"),
            "auto_restore_suggested": state.get("auto_restore_suggested"),
            "active_slot": rs.active_slot() or None,
        }
    except Exception as e:
        logger.error(f"recovery-mode GET error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/admin/recovery/recovery-mode")
async def recovery_mode_enable(request: Request):
    """Включить персистентный режим: {"id": "<snap>", "reason": "..."}.

    Каждый запуск гварда будет применять этот снимок, пока режим не
    снимут кнопкой (clear) после успешного старта.
    """
    if not _require_admin(request):
        return _auth_error()
    try:
        try:
            body = await request.json()
        except Exception:
            swallow.note('recovery.recovery_mode_enable:L446')
            body = {}
        body = body if isinstance(body, dict) else {}
        snap_id = str(body.get("id") or "").strip()
        if not snap_id:
            return JSONResponse({"ok": False, "error": "нет id снимка"},
                                status_code=400)
        res = rs.recovery_mode_set(snap_id,
                                   reason=str(body.get("reason") or "manual"))
        if not res.get("ok"):
            status = 404 if res.get("stage") == "integrity" else 409
            return JSONResponse(res, status_code=status)
        logger.info(f"[recovery] recovery mode ON: {snap_id}")
        return res
    except Exception as e:
        logger.error(f"recovery-mode POST error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/admin/recovery/recovery-mode/clear")
def recovery_mode_clear(request: Request):
    """Снять режим. Только после успешного старта (boot_ok) — §4.1."""
    if not _require_admin(request):
        return _auth_error()
    try:
        if not rs.boot_ok():
            return JSONResponse(
                {"ok": False,
                 "error": "режим снимается только после успешного старта "
                          "сервера (boot_ok)"},
                status_code=409)
        res = rs.recovery_mode_clear()
        logger.info("[recovery] recovery mode cleared")
        return res
    except Exception as e:
        logger.error(f"recovery-mode clear error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/admin/recovery/recovery-mode/unlock")
async def recovery_mode_unlock(request: Request):
    """Снять restore_lock (ручное подтверждение после 3 откатов / canary)."""
    if not _require_admin(request):
        return _auth_error()
    try:
        reason = "manual confirm via API"
        try:
            body = await request.json()
            if isinstance(body, dict) and body.get("reason"):
                reason = str(body["reason"])
        except Exception:
            swallow.note('recovery.recovery_mode_unlock:L497')
            pass
        res = rs.restore_unlock(reason)
        logger.info(f"[recovery] restore_lock cleared: {reason}")
        return {"ok": True, "lock": res}
    except Exception as e:
        logger.error(f"recovery-mode unlock error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ------------------------------------------- диагностика / чистка / журнал --

@router.get("/api/admin/recovery/diagnose")
async def recovery_diagnose(request: Request):
    """Гипотезы о причине сбоя с доказательствами и уверенностью (§4.5)."""
    if not _require_admin(request):
        return _auth_error()
    try:
        return {"ok": True, **(await asyncio.to_thread(rc.diagnose))}
    except Exception as e:
        logger.error(f"recovery diagnose error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/admin/recovery/prune")
async def recovery_prune(request: Request):
    """Прогнать умную чистку снимков по лимитам (§5). Тело: {"aggressive": true}."""
    if not _require_admin(request):
        return _auth_error()
    try:
        try:
            body = await request.json()
        except Exception:
            swallow.note('recovery.recovery_prune:L530')
            body = {}
        aggressive = bool(isinstance(body, dict) and body.get("aggressive"))
        report = await asyncio.to_thread(
            lambda: rc.prune_backups(aggressive=aggressive))
        rc.audit("prune", result="manual", removed=len(report.get("removed", [])))
        return {"ok": True, **report}
    except Exception as e:
        logger.error(f"recovery prune error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/admin/recovery/audit")
def recovery_audit(request: Request):
    """Журнал действий (§4.6): snapshot/verify/restore/pin/prune/ban/config."""
    if not _require_admin(request):
        return _auth_error()
    try:
        limit = int(request.query_params.get("limit", "200") or 200)
        return {"ok": True, "entries": rc.read_audit(limit)}
    except Exception as e:
        logger.error(f"recovery audit error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ------------------------------------------------ безопасность (§6.1) --

@router.get("/api/admin/security/status")
def security_status(request: Request):
    """Счётчики, заблокированные IP, paranoid, текущая конкурентность."""
    if not _require_admin(request):
        return _auth_error()
    try:
        import security_guard as sg
        return sg.status()
    except Exception as e:
        logger.error(f"security status error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/admin/security/unban")
async def security_unban(request: Request):
    """Разбанить IP/профиль вручную: {"key": "..."}."""
    if not _require_admin(request):
        return _auth_error()
    try:
        try:
            body = await request.json()
        except Exception:
            swallow.note('recovery.security_unban:L579')
            body = {}
        key = str((body or {}).get("key") or "").strip()
        if not key:
            return JSONResponse({"ok": False, "error": "нет key"}, status_code=400)
        import security_guard as sg
        res = sg.unban(key)
        rc.audit("unban", result=key)
        return res
    except Exception as e:
        logger.error(f"security unban error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ------------------------------------------------------- консоль Guard (UI) --

_RECOVERY_UI_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Recovery Guard — консоль</title>
<link rel="stylesheet" href="/static/css/recovery_ui.css"></head>
<body><div class="rg-wrap">
<div class="rg-head">
  <div><h1>🛡️ Recovery Guard 3.0</h1>
  <div class="sub">снимки · восстановление · диагностика · безопасность · нагрузка</div></div>
  <div class="rg-row">
    <label class="rg-muted"><input type="checkbox" id="rg-auto"> авто-refresh</label>
    <button class="rg-btn rg-ghost" id="rg-refresh">Обновить</button>
  </div>
</div>
<div class="rg-tabs">
  <button class="rg-tab active" data-tab="overview">Обзор</button>
  <button class="rg-tab" data-tab="diagnose">Диагностика</button>
  <button class="rg-tab" data-tab="snapshots">Снимки</button>
  <button class="rg-tab" data-tab="restore">Восстановление</button>
  <button class="rg-tab" data-tab="settings">Настройки</button>
  <button class="rg-tab" data-tab="security">Безопасность</button>
  <button class="rg-tab" data-tab="audit">Журнал</button>
  <button class="rg-tab" data-tab="load">Нагрузка</button>
</div>
<div class="rg-panel" data-panel="overview"></div>
<div class="rg-panel rg-hide" data-panel="diagnose"></div>
<div class="rg-panel rg-hide" data-panel="snapshots"></div>
<div class="rg-panel rg-hide" data-panel="restore"></div>
<div class="rg-panel rg-hide" data-panel="settings"></div>
<div class="rg-panel rg-hide" data-panel="security"></div>
<div class="rg-panel rg-hide" data-panel="audit"></div>
<div class="rg-panel rg-hide" data-panel="load"></div>
</div>
<div class="rg-toast" id="rg-toast"></div>
<script src="/static/js/recovery_ui.js?v=2"></script>
</body></html>"""


@router.get("/api/admin/recovery/ui")
def recovery_console(request: Request):
    """Веб-консоль Recovery Guard (8 вкладок, guard.md §7). Только админ."""
    if not _require_admin(request):
        return HTMLResponse(
            "<h1>403</h1><p>Нужны права администратора.</p>", status_code=403)
    return HTMLResponse(_RECOVERY_UI_HTML)

