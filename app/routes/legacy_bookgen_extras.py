"""legacy_bookgen_extras — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import JSONResponse
from pathlib import Path
import io
import shutil
import uuid
from fastapi import APIRouter, File, UploadFile
from routes.legacy_common import Request, StreamingResponse, _req_profile, _run_sync, httpx, json, logger, re, time

import os
import swallow

# секция читает файлы из каталога приложения (app/), а не из routes/
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

router = APIRouter()


def _safe_child(root: Path, name: str):
    """Путь внутри root или None, если name пытается выйти наружу."""
    try:
        root_r = Path(root).resolve()
        p = (root_r / str(name)).resolve()
        p.relative_to(root_r)
        return p
    except Exception:
        swallow.note('legacy_bookgen_extras._safe_child:L31')
        return None


@router.post("/api/bookgen/upload")
async def api_bookgen_upload(request: Request, file: UploadFile = File(...)):
    try:
        from modules.book_generator import BookGenerator, get_books_dir
        books_dir = get_books_dir(profile_id=_req_profile(request))
        books_dir.mkdir(parents=True, exist_ok=True)
        filename = file.filename or "uploaded.txt"
        ext = Path(filename).suffix.lower()
        content_bytes = await file.read()
        allowed = {".fb2", ".txt", ".md", ".epub", ".jsonl", ".json"}
        if ext not in allowed:
            return JSONResponse({"error": f"Формат {ext} не поддерживается. Используйте: {', '.join(sorted(allowed))}"}, status_code=400)
        safe_name = re.sub(r'[^\w\.\-]', '_', filename)[:128]
        save_path = books_dir / safe_name
        n = 1
        # Раньше в ветке совпадения имени брался неочищенный stem: имя вида
        # `../x.txt` уводило запись за пределы каталога книг.
        safe_stem = Path(safe_name).stem or "book"
        while save_path.exists():
            save_path = books_dir / f"{safe_stem}_{n}{ext}"
            n += 1
        save_path.write_bytes(content_bytes)
        return {"ok": True, "path": str(save_path), "filename": save_path.name, "size": len(content_bytes)}
    except Exception as e:
        logger.error(f"bookgen upload error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/bookgen/library")
def api_bookgen_library(request: Request):
    try:
        from modules.book_generator import get_books_dir
        books_dir = get_books_dir(profile_id=_req_profile(request))
        files = []
        if books_dir.exists():
            for f in sorted(books_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if f.is_file() and f.suffix.lower() in (".fb2", ".txt", ".md", ".epub", ".jsonl"):
                    files.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "ext": f.suffix.lower()[1:],
                        "modified": f.stat().st_mtime,
                    })
        return {"files": files, "dir": str(books_dir)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/bookgen/library/{filename:path}")
def api_bookgen_library_read(request: Request, filename: str):
    try:
        from modules.book_generator import get_books_dir
        books_dir = get_books_dir(profile_id=_req_profile(request))
        file_path = _safe_child(books_dir, filename)
        if file_path is None:
            return JSONResponse({"error": "Недопустимое имя файла"}, status_code=400)
        if not file_path.exists() or not file_path.is_file():
            return JSONResponse({"error": "Файл не найден"}, status_code=404)
        safe_name = re.sub(r'[^\w\.\-]', '_', file_path.stem)[:80] or "book"
        data = file_path.read_bytes()
        ext = file_path.suffix.lower()
        mime_map = {".fb2": "application/fictionbook+xml", ".txt": "text/plain", ".md": "text/markdown", ".epub": "application/epub+zip", ".jsonl": "application/jsonl", ".json": "application/json"}
        return StreamingResponse(
            io.BytesIO(data),
            media_type=mime_map.get(ext, "application/octet-stream") + "; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}{file_path.suffix}"',
                "Content-Length": str(len(data)),
            }
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.delete("/api/bookgen/library/{filename:path}")
def api_bookgen_library_delete(request: Request, filename: str):
    try:
        from modules.book_generator import get_books_dir
        books_dir = get_books_dir(profile_id=_req_profile(request))
        file_path = _safe_child(books_dir, filename)
        if file_path is None:
            return JSONResponse({"error": "Недопустимое имя файла"}, status_code=400)
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
            return {"ok": True}
        return JSONResponse({"error": "Файл не найден"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# EasyDiffusion (генерация картинок) вынесен в trash/easydiffusion/ — за картинки отвечает genlab.

# --- Character & Location Preservation ---
CHARACTER_SHEETS_DIR = Path(_APP_DIR) / "data" / "characters"
LOCATION_SHEETS_DIR = Path(_APP_DIR) / "data" / "locations"

@router.post("/api/character/save")
async def api_character_save(request: Request):
    try:
        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            return JSONResponse({"error": "Имя персонажа обязательно"}, status_code=400)
        CHARACTER_SHEETS_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[^\w\-]', '_', name)[:64]
        path = CHARACTER_SHEETS_DIR / f"{safe}.json"
        sheet = {
            "name": name,
            "description": body.get("description", ""),
            "appearance": body.get("appearance", ""),
            "personality": body.get("personality", ""),
            "role": body.get("role", ""),
            "seed": body.get("seed", abs(hash(name)) % 2147483647),
            "created": time.time(),
            "updated": time.time(),
        }
        path.write_text(json.dumps(sheet, ensure_ascii=False, indent=2), "utf-8")
        return {"ok": True, "sheet": sheet}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/character/list")
def api_character_list():
    try:
        CHARACTER_SHEETS_DIR.mkdir(parents=True, exist_ok=True)
        chars = []
        for f in sorted(CHARACTER_SHEETS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text("utf-8"))
                chars.append(data)
            except Exception:
                swallow.note('legacy_bookgen_extras.api_character_list:L288')
                pass
        return {"characters": chars}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.delete("/api/character/{name}")
def api_character_delete(name: str):
    try:
        safe = re.sub(r'[^\w\-]', '_', name)[:64]
        path = CHARACTER_SHEETS_DIR / f"{safe}.json"
        if path.exists():
            path.unlink()
            return {"ok": True}
        return JSONResponse({"error": "Персонаж не найден"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/location/save")
async def api_location_save(request: Request):
    try:
        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            return JSONResponse({"error": "Название локации обязательно"}, status_code=400)
        LOCATION_SHEETS_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[^\w\-]', '_', name)[:64]
        path = LOCATION_SHEETS_DIR / f"{safe}.json"
        sheet = {
            "name": name,
            "description": body.get("description", ""),
            "atmosphere": body.get("atmosphere", ""),
            "architecture": body.get("architecture", ""),
            "seed": body.get("seed", abs(hash(name)) % 2147483647),
            "created": time.time(),
            "updated": time.time(),
        }
        path.write_text(json.dumps(sheet, ensure_ascii=False, indent=2), "utf-8")
        return {"ok": True, "sheet": sheet}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/location/list")
def api_location_list():
    try:
        LOCATION_SHEETS_DIR.mkdir(parents=True, exist_ok=True)
        locs = []
        for f in sorted(LOCATION_SHEETS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text("utf-8"))
                locs.append(data)
            except Exception:
                swallow.note('legacy_bookgen_extras.api_location_list:L340')
                pass
        return {"locations": locs}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.delete("/api/location/{name}")
def api_location_delete(name: str):
    try:
        safe = re.sub(r'[^\w\-]', '_', name)[:64]
        path = LOCATION_SHEETS_DIR / f"{safe}.json"
        if path.exists():
            path.unlink()
            return {"ok": True}
        return JSONResponse({"error": "Локация не найдена"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# --- Manga Creation ---
MANGA_DIR = Path(_APP_DIR) / "data" / "manga"

@router.post("/api/manga/create")
async def api_manga_create(request: Request):
    try:
        body = await request.json()
        title = body.get("title", "Манга без названия")
        MANGA_DIR.mkdir(parents=True, exist_ok=True)
        manga_id = str(uuid.uuid4())[:8]
        manga_dir = MANGA_DIR / manga_id
        manga_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": manga_id,
            "title": title,
            "author": body.get("author", "Автор"),
            "genre": body.get("genre", "манга"),
            "description": body.get("description", ""),
            "panels_per_page": body.get("panels_per_page", 4),
            "pages": [],
            "characters": body.get("characters", []),
            "created": time.time(),
            "updated": time.time(),
        }
        (manga_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
        return {"ok": True, "id": manga_id, "meta": meta}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/manga/list")
def api_manga_list():
    try:
        MANGA_DIR.mkdir(parents=True, exist_ok=True)
        mangas = []
        for d in sorted(MANGA_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir():
                meta_path = d / "meta.json"
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text("utf-8"))
                        mangas.append(meta)
                    except Exception:
                        swallow.note('legacy_bookgen_extras.api_manga_list:L400')
                        pass
        return {"mangas": mangas}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/manga/{manga_id}")
def api_manga_get(manga_id: str):
    try:
        manga_dir = _safe_child(MANGA_DIR, manga_id)
        if manga_dir is None:
            return JSONResponse({"error": "Недопустимый ID"}, status_code=400)
        meta_path = manga_dir / "meta.json"
        if not meta_path.exists():
            return JSONResponse({"error": "Манга не найдена"}, status_code=404)
        return json.loads(meta_path.read_text("utf-8"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/manga/{manga_id}/page")
async def api_manga_add_page(manga_id: str, request: Request):
    try:
        body = await request.json()
        manga_dir = _safe_child(MANGA_DIR, manga_id)
        if manga_dir is None:
            return JSONResponse({"error": "Недопустимый ID"}, status_code=400)
        meta_path = manga_dir / "meta.json"
        if not meta_path.exists():
            return JSONResponse({"error": "Манга не найдена"}, status_code=404)
        meta = json.loads(meta_path.read_text("utf-8"))
        page = {
            "num": len(meta["pages"]) + 1,
            "panels": body.get("panels", []),
            "description": body.get("description", ""),
        }
        meta["pages"].append(page)
        meta["updated"] = time.time()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
        return {"ok": True, "page": page}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/manga/{manga_id}/generate")
async def api_manga_generate_panels(manga_id: str, request: Request):
    try:
        from modules.book_generator import BookGenerator, get_books_dir, get_lm_config
        body = await request.json()
        manga_dir = _safe_child(MANGA_DIR, manga_id)
        if manga_dir is None:
            return JSONResponse({"error": "Недопустимый ID"}, status_code=400)
        meta_path = manga_dir / "meta.json"
        if not meta_path.exists():
            return JSONResponse({"error": "Манга не найдена"}, status_code=404)
        meta = json.loads(meta_path.read_text("utf-8"))
        scene = body.get("scene", "")
        lm_url, lm_model, lm_api_key = get_lm_config()
        gen = BookGenerator(lm_url=lm_url, lm_model=lm_model, lm_api_key=lm_api_key)
        model = body.get("model", lm_model)
        if not model:
            return JSONResponse({"error": "Модель не указана"}, status_code=400)
        prompt = (
            f"Ты — сценарист манги. Придумай {meta.get('panels_per_page', 4)} панелей для сцены:\n"
            f"Название манги: {meta.get('title', '')}\n"
            f"Жанр: {meta.get('genre', 'манга')}\n"
            f"Описание сцены: {scene}\n\n"
            f"Для каждой панели напиши:\n"
            f"- PANEL 1: [описание кадра, ракурс, персонажи, фон]\n"
            f"- TEXT: [текст баллуна / мысли / повествования]\n"
            f"- PROMPT: [английский промт для Stable Diffusion, 50-100 слов]\n\n"
            f"Формат: строго разделяй панели пустой строкой."
        )
        result = gen.llm.chat(model, [
            {"role": "system", "content": "Ты — профессиональный сценарист манги. Отвечай структурно, на русском."},
            {"role": "user", "content": prompt},
        ], max_tokens=4096)
        page = {
            "num": len(meta["pages"]) + 1,
            "panels": [],
            "scene": scene,
            "generated_text": result,
            "description": scene,
        }
        panels = []
        current_panel = {}
        for line in result.split("\n"):
            line = line.strip()
            if not line:
                if current_panel:
                    panels.append(current_panel)
                    current_panel = {}
                continue
            if line.startswith("PANEL"):
                if current_panel:
                    panels.append(current_panel)
                current_panel = {"num": len(panels) + 1, "description": line, "text": "", "prompt": ""}
            elif line.startswith("TEXT:") and current_panel:
                current_panel["text"] = line[5:].strip()
            elif line.startswith("PROMPT:") and current_panel:
                current_panel["prompt"] = line[7:].strip()
        if current_panel:
            panels.append(current_panel)
        page["panels"] = panels
        meta["pages"].append(page)
        meta["updated"] = time.time()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
        return {"ok": True, "page": page}
    except Exception as e:
        logger.error(f"manga generate error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.delete("/api/manga/{manga_id}")
async def api_manga_delete(manga_id: str):
    try:
        manga_dir = _safe_child(MANGA_DIR, manga_id)
        if manga_dir is None:
            return JSONResponse({"error": "Недопустимый ID"}, status_code=400)
        if manga_dir.exists() and manga_dir.is_dir():
            await _run_sync(shutil.rmtree, manga_dir)
            return {"ok": True}
        return JSONResponse({"error": "Манга не найдена"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
