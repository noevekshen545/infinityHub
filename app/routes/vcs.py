"""HTTP-API собственного VCS: коммиты переписок и настроек профиля.

Маршруты проверяют флаг ``modules.vcs.enabled`` в коде, а не только в UI:
выключенный модуль не должен ни читать, ни тем более восстанавливать данные.
Профиль берётся из запроса (cookie → токен агента → общедоступный); явный
``profile`` разрешён только тому, кому ``can_act_for_profile`` это позволяет.
Глобальный активный профиль сервера здесь не используется.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import module_flags
import profiles_module as PM
import vcs_module
import vcs_sources

logger = logging.getLogger("infinity")
router = APIRouter(prefix="/api/vcs", tags=["vcs"])

_DISABLED = "Свой VCS выключен (модули → «Свой VCS»)"


def _target(request: Request, profile: Optional[str] = None, payload=None) -> str:
    explicit = str(profile or "").strip()
    if not explicit and isinstance(payload, dict):
        explicit = str(payload.get("profile") or "").strip()
    if explicit:
        if not PM.profile_exists(explicit):
            raise HTTPException(status_code=404,
                                detail="Профиль не найден: %s" % explicit)
        if not PM.can_act_for_profile(request, explicit):
            raise HTTPException(status_code=403,
                                detail="Нет доступа к профилю: %s" % explicit)
        return explicit
    return PM.request_profile(request)


def _denied():
    return JSONResponse({"ok": False, "error": _DISABLED}, status_code=403)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (vcs_module.VcsError, vcs_sources.SourceError) as exc:
        return JSONResponse({"ok": False, "error": str(exc),
                             "code": getattr(exc, "code", "vcs_error"),
                             **getattr(exc, "extra", {})},
                            status_code=getattr(exc, "status", 400))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("vcs API error: %s", exc)
        return JSONResponse({"ok": False, "error": "внутренняя ошибка VCS"},
                            status_code=500)


def _repo(pid: str):
    return vcs_module.repo_for(pid)


@router.get("/status")
def vcs_status(request: Request, profile: Optional[str] = Query(None)):
    pid = _target(request, profile)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    def work():
        files, skipped = vcs_sources.collect_report(pid)
        st = _repo(pid).status(files)
        st["skipped"] = skipped
        st["profile"] = pid
        return st
    return _call(work)


@router.post("/commit")
def vcs_commit(request: Request, payload: dict = Body(default=None)):
    payload = payload or {}
    pid = _target(request, payload=payload)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    def work():
        files, skipped = vcs_sources.collect_report(pid)
        row = _repo(pid).commit(
            payload.get("message", ""), files,
            author=pid, session_id=str(payload.get("session_id") or ""),
            branch=str(payload.get("branch") or ""))
        row["profile"] = pid
        row["skipped"] = skipped
        return row
    return _call(work)


@router.get("/log")
def vcs_log(request: Request, limit: int = Query(20, ge=1, le=200),
            ref: str = Query(""), profile: Optional[str] = Query(None)):
    pid = _target(request, profile)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    def work():
        rows = _repo(pid).log(limit, ref)
        return {"profile": pid, "count": len(rows), "commits": rows}
    return _call(work)


@router.get("/show")
def vcs_show(request: Request, commit: str = Query(""),
             profile: Optional[str] = Query(None)):
    pid = _target(request, profile)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    def work():
        row = _repo(pid).show(commit)
        row["profile"] = pid
        return row
    return _call(work)


@router.get("/diff")
def vcs_diff(request: Request, from_ref: str = Query("", alias="from"),
             to_ref: str = Query("", alias="to"),
             profile: Optional[str] = Query(None)):
    pid = _target(request, profile)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    def work():
        files = vcs_sources.collect(pid)
        row = _repo(pid).diff(from_ref, to_ref, files)
        row["profile"] = pid
        return row
    return _call(work)


@router.post("/restore")
def vcs_restore(request: Request, payload: dict = Body(default=None)):
    payload = payload or {}
    pid = _target(request, payload=payload)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    def work():
        repo = _repo(pid)
        commit = str(payload.get("commit") or "")
        manifest_hashes = repo.commit_manifest(commit)
        manifest = {path: repo.get_blob(obj_hash)
                    for path, obj_hash in manifest_hashes.items()}
        applied = vcs_sources.restore(pid, manifest,
                                      prune=bool(payload.get("prune")))
        files, skipped = vcs_sources.collect_report(pid)
        st = repo.status(files)
        return {"profile": pid, "commit": repo.resolve_commit(commit),
                "restored": applied, "status": st, "skipped": skipped}
    return _call(work)


@router.get("/branches")
def vcs_branches(request: Request, profile: Optional[str] = Query(None)):
    pid = _target(request, profile)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    return _call(lambda: {"profile": pid, "branches": _repo(pid).branches()})


@router.post("/branch")
def vcs_branch(request: Request, payload: dict = Body(default=None)):
    payload = payload or {}
    pid = _target(request, payload=payload)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    return _call(lambda: {"profile": pid, "branch": _repo(pid).create_branch(
        payload.get("name", ""), str(payload.get("at") or ""))})


@router.post("/checkout")
def vcs_checkout(request: Request, payload: dict = Body(default=None)):
    payload = payload or {}
    pid = _target(request, payload=payload)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    return _call(lambda: {"profile": pid,
                          "checkout": _repo(pid).checkout(payload.get("name", ""))})


@router.post("/merge")
def vcs_merge(request: Request, payload: dict = Body(default=None)):
    payload = payload or {}
    pid = _target(request, payload=payload)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    return _call(lambda: {"profile": pid, "merge": _repo(pid).merge_fast_forward(
        payload.get("name", ""))})


@router.post("/revert")
def vcs_revert(request: Request, payload: dict = Body(default=None)):
    payload = payload or {}
    pid = _target(request, payload=payload)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    return _call(lambda: {"profile": pid, "commit": _repo(pid).revert_to(
        payload.get("commit", ""), str(payload.get("message") or ""),
        author=pid, session_id=str(payload.get("session_id") or ""))})


@router.post("/delete-branch")
def vcs_delete_branch(request: Request, payload: dict = Body(default=None)):
    payload = payload or {}
    pid = _target(request, payload=payload)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    return _call(lambda: {"profile": pid,
                          "deleted": _repo(pid).delete_branch(payload.get("name", ""))})


@router.get("/reflog")
def vcs_reflog(request: Request, limit: int = Query(50, ge=1, le=200),
               profile: Optional[str] = Query(None)):
    pid = _target(request, profile)
    if not module_flags.enabled_for("vcs", pid):
        return _denied()
    return _call(lambda: {"profile": pid, "rows": _repo(pid).reflog(limit)})
