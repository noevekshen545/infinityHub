"""Module Pages Router — GET /module/{path} для расширений с собственными UI-страницами."""
import os, json, logging
import html as _html_mod
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
import swallow

logger = logging.getLogger("infinity")
router = APIRouter()


def _get_mgr():
    from extension_system import get_manager
    return get_manager()


def _get_mgr_for_request(request=None):
    """Менеджер расширений каталога профиля из cookie запроса (fallback — активный).

    Раньше использовался глобальный get_manager(): при гонке профильного флипа
    глобальный BASE_DIR указывал на каталог чужого профиля, и страница модуля
    отвечала 404 "not found or disabled" даже для включённого расширения.
    """
    from extension_system import get_manager, get_manager_for
    try:
        import profiles_module as PM
        pid = None
        try:
            candidate = (request.cookies.get(PM.COOKIE_NAME, "") if request is not None else "") or ""
            pid = PM.resolve_profile_id(candidate) if candidate else None
        except Exception:
            swallow.note('module_pages._get_mgr_for_request:L30')
            pid = None
        cfg = PM.get_profile_config(pid) if pid else PM.get_active_config()
        ext_dir = (cfg or {}).get("extensions_dir", "") or ""
        if ext_dir and os.path.isdir(ext_dir):
            return get_manager_for(ext_dir)
    except Exception as e:
        logger.warning(f"Per-request extension manager fallback: {e}")
    return get_manager()


def _read_runtime_js():
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js", "extension_runtime.js")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.warning(f"Failed to read extension_runtime.js: {e}")
        return ""


@router.get("/module/{module_path:path}")
def serve_module_page(module_path: str, request: Request):
    try:
        mgr = _get_mgr_for_request(request)
        ext_name = module_path.split("/")[0]
        subpath = module_path[len(ext_name):].lstrip("/")

        ext = mgr.extensions.get(ext_name)
        if not ext or not ext.enabled:
            # Имя и путь приходят из URL: без escape это отражённый XSS.
            _name = _html_mod.escape(ext_name)
            return HTMLResponse(f"<html><body><h2>Extension '{_name}' not found or disabled</h2></body></html>", status_code=404)

        module_pages_raw = getattr(ext, "module_pages", None) or (ext.config or {}).get("module_pages", {})
        if not module_pages_raw:
            _name = _html_mod.escape(ext_name)
            return HTMLResponse(f"<html><body><h2>Extension '{_name}' has no module pages</h2></body></html>", status_code=404)

        # Support both old format {"/path": config} and new format {"pages": [{path:"", ...}]}
        pages_list = module_pages_raw
        if isinstance(module_pages_raw, dict) and "pages" in module_pages_raw:
            pages_list = module_pages_raw["pages"]
        if isinstance(pages_list, dict):
            pages_list = [{"path": k, **v} for k, v in pages_list.items()]

        # Find matching page config
        page_config = None
        for pg in pages_list:
            route = pg.get("path", "") if isinstance(pg, dict) else ""
            if route.lstrip("/") == subpath or (subpath == "" and route == "/"):
                page_config = pg
                break
        if not page_config:
            _name = _html_mod.escape(ext_name)
            _sub = _html_mod.escape(subpath)
            return HTMLResponse(f"<html><body><h2>Page '{_sub}' not found in extension '{_name}'</h2></body></html>", status_code=404)

        runtime_js = _read_runtime_js()
        frontend_js = ext.frontend_js or ""
        title = page_config.get("title", ext_name)
        icon = page_config.get("icon", "fa-puzzle-piece")
        custom_css = page_config.get("css", "")
        custom_html = page_config.get("html", "")
        template = page_config.get("template", "full")

        extra_head = ""
        if custom_css:
            extra_head = f"<style>{custom_css}</style>"

        html_head = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>""" + title + """ — Infinity</title>
<link rel="stylesheet" href="/static/css/font-awesome.min.css">
<link rel="stylesheet" href="/static/fonts/google-fonts.css">
""" + extra_head + """
<style>
* { margin:0; padding:0; box-sizing:border-box }
body { font-family:'Inter',sans-serif; background:var(--bg,#0f1419); color:var(--text,#e1e8f0); min-height:100vh }
:root { --bg:#0f1419; --bg2:#151b23; --bg3:#1c242f; --text:#e1e8f0; --text2:#8899aa; --accent:#6366f1; --border:#2a3548; --radius:12px; --glass:rgba(255,255,255,0.04); --glass-border:rgba(255,255,255,0.08); --hover:rgba(255,255,255,0.06); }
.nav { display:flex; align-items:center; gap:12px; padding:12px 24px; background:var(--bg2); border-bottom:1px solid var(--border); }
.nav .back { display:inline-flex;align-items:center;gap:6px;color:var(--text2);text-decoration:none;font-size:13px;font-weight:500;padding:6px 12px;border-radius:8px;transition:all 0.2s }
.nav .back:hover { background:var(--hover);color:var(--text) }
.nav .title { font-size:16px;font-weight:700;display:flex;align-items:center;gap:8px }
.nav .title i { color:var(--accent) }
.module-content { padding:24px; max-width:1200px; margin:0 auto }
</style>
</head>
<body>"""

        if template == "full":
            custom_html = custom_html or f"""<div class="module-content">
  <div class="ext-module-page" id="ext-module-{ext_name}">
    <div style="text-align:center;padding:60px 20px;color:var(--text2)">
      <i class="fas {icon}" style="font-size:48px;color:var(--accent);margin-bottom:16px;display:block"></i>
      <h2 style="color:var(--text);margin-bottom:8px">{title}</h2>
      <p>Расширение загружено. Используйте API ExtensionRuntime для управления страницей.</p>
    </div>
  </div>
</div>"""
        elif template == "panel":
            custom_html = custom_html or f"""<div class="module-content">
  <div class="ext-module-panel" id="ext-module-{ext_name}" style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:24px">
    <h2 style="margin-bottom:16px"><i class="fas {icon}" style="color:var(--accent);margin-right:8px"></i>{title}</h2>
    {custom_html}
  </div>
</div>"""

        html = html_head + custom_html + f"""
<script>{runtime_js}</script>
<script>
(function(){{
    var RT = window.ExtensionRuntime;
    if(RT){{ RT.log('[ModulePage] Loaded {ext_name}/{subpath}'); }}
}})();
</script>
<script>{frontend_js}</script>
</body>
</html>"""

        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Module page error: {e}")
        return HTMLResponse(f"<html><body><h2>Error loading module page: {_html_mod.escape(str(e))}</h2></body></html>", status_code=500)


@router.get("/api/extensions/module-pages")
def list_module_pages(request: Request):
    try:
        mgr = _get_mgr_for_request(request)
        pages = []
        for ext_name, ext in mgr.extensions.items():
            if not ext.enabled:
                continue
            module_pages_raw = getattr(ext, "module_pages", None) or (ext.config or {}).get("module_pages", {})
            pages_list = module_pages_raw
            if isinstance(module_pages_raw, dict) and "pages" in module_pages_raw:
                pages_list = module_pages_raw["pages"]
            if isinstance(pages_list, dict):
                pages_list = [{"path": k, **v} for k, v in pages_list.items()]
            for pg in pages_list:
                route = pg.get("path", "") if isinstance(pg, dict) else ""
                pages.append({
                    "extension": ext_name,
                    "path": f"/module/{ext_name}{'/' if not route.startswith('/') else ''}{route.lstrip('/')}",
                    "title": pg.get("title", ext_name),
                    "icon": pg.get("icon", "fa-puzzle-piece"),
                    "template": pg.get("template", "full")
                })
        return {"pages": pages}
    except Exception as e:
        logger.error(f"List module pages error: {e}")
        return {"pages": []}
