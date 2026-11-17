"""Univer Router — офисные документы (Таблицы/Документы/Слайды) внутри модуля Notes.

Статический фронтенд собирается в static/univer (index.html + assets/main.js + assets/worker.js).
Документы хранятся в <NOTES_DIR>/_office/<id>.json и могут прикрепляться к заметкам
в виде markdown-ссылки, ведущей обратно на редактор.
"""
import os
import json
import time
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from modules import notes_store
import profile_paths
import server
import swallow

logger = logging.getLogger("infinity")

# bind: каталоги данных берутся из профиля запроса, а не из глобалов сервера.
router = APIRouter(dependencies=[Depends(profile_paths.bind)])
_pp = profile_paths.proxy(server)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_INDEX = os.path.join(_BASE_DIR, "static", "univer", "index.html")


def _office_dir():
    d = os.path.join(_pp.NOTES_DIR, "_office")
    os.makedirs(d, exist_ok=True)
    return d


def _all_docs():
    d = _office_dir()
    docs = []
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as fh:
                art = json.load(fh)
            docs.append({
                "id": art.get("id", fn.replace(".json", "")),
                "type": art.get("type", "sheets"),
                "title": art.get("title", "Без названия"),
                "updated": art.get("updated", 0),
                "created": art.get("created", 0),
            })
        except Exception:
            swallow.note('univer._all_docs:L47')
            continue
    docs.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return docs


@router.get("/univer")
@router.get("/univer/{otype}")
def univer_page(otype: str = ""):
    if not os.path.exists(_STATIC_INDEX):
        return JSONResponse(content={"error": "Univer frontend not built. Run `npm run build` in univer-app."}, status_code=503)
    return FileResponse(_STATIC_INDEX)


@router.get("/api/univer/list")
def univer_list():
    try:
        return {"docs": _all_docs()}
    except Exception as e:
        logger.error(f"univer list error: {e}")
        return {"docs": []}


@router.post("/api/univer/save")
async def univer_save(request: Request):
    try:
        body = await request.json()
        otype = body.get("type", "sheets")
        if otype not in ("sheets", "docs", "slides"):
            otype = "sheets"
        title = (body.get("title") or "Без названия").strip() or "Без названия"
        data = body.get("data")
        if data is None:
            return JSONResponse(content={"error": "no data"}, status_code=400)
        doc_id = body.get("id") or f"off_{int(time.time()*1000)}"
        art = {
            "id": doc_id,
            "type": otype,
            "title": title,
            "data": data,
            "created": body.get("created", time.time()),
            "updated": time.time(),
        }
        d = _office_dir()
        with open(os.path.join(d, f"{doc_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(art, fh, ensure_ascii=False, indent=2)
        return {"id": doc_id}
    except Exception as e:
        logger.error(f"univer save error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/api/univer/load")
def univer_load(id: str = ""):
    if not id:
        return JSONResponse(content={"error": "no id"}, status_code=400)
    try:
        fp = os.path.join(_office_dir(), f"{id}.json")
        if not os.path.exists(fp):
            return JSONResponse(content={"error": "not found"}, status_code=404)
        with open(fp, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        logger.error(f"univer load error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/api/univer/attach")
async def univer_attach(request: Request):
    try:
        body = await request.json()
        doc_id = body.get("doc_id", "")
        note_id = body.get("note_id", "")
        if not doc_id or not note_id:
            return JSONResponse(content={"error": "doc_id and note_id required"}, status_code=400)

        import server
        docs = _all_docs()
        doc = next((x for x in docs if x["id"] == doc_id), None)
        title = doc["title"] if doc else doc_id
        dtype = doc["type"] if doc else "sheets"

        art = notes_store.notes_get_article(note_id)
        if not art or art.get("error"):
            return JSONResponse(content={"error": "note not found"}, status_code=404)

        link = f"\n\n📎 [{title}](/univer?doc={doc_id}&type={dtype})\n"
        content = art.get("content", "") or ""
        if link.strip() not in content:
            art["content"] = content + link
        notes_store.notes_save_article(art)
        return {"ok": True}
    except Exception as e:
        logger.error(f"univer attach error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)
