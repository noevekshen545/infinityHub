"""Файловое хранилище wiki-страниц (WIKI_MEMORY_DIR).

Вынесено из main.py без изменения логики: те же имена, то же поведение.
Используется маршрутами wiki, refleksiya-циклом и wikimemory-хелперами.
"""

import os
import re

from routes.legacy_common import WIKI_MEMORY_DIR, logger
import swallow


def _wiki_list_pages() -> list:
    """List all wiki pages with metadata."""
    pages = []
    if not os.path.isdir(WIKI_MEMORY_DIR):
        return pages
    for fname in sorted(os.listdir(WIKI_MEMORY_DIR)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(WIKI_MEMORY_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            title = ""
            tags = []
            created = ""
            updated = ""
            for line in content.split("\n"):
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            pages.append({
                "name": fname[:-3],
                "title": title or fname[:-3],
                "filename": fname,
                "size": len(content),
                "created": created,
                "updated": updated,
                "tags": tags,
                "path": fpath,
            })
        except Exception:
            swallow.note('wiki_store._wiki_list_pages:L44')
            pass
    return pages


def _wiki_read_page(name: str) -> dict | None:
    """Read a wiki page by name (without .md)."""
    for fname in os.listdir(WIKI_MEMORY_DIR):
        if fname.endswith(".md") and (fname[:-3] == name or fname.lower() == name.lower() + ".md"):
            fpath = os.path.join(WIKI_MEMORY_DIR, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            lines = content.split("\n")
            title = ""
            for line in lines:
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            # Extract wiki links [[Page Name]]
            links = re.findall(r'\[\[([^\]]+)\]\]', content)
            return {"name": fname[:-3], "title": title or fname[:-3], "content": content, "links": links}
    return None


def _wiki_save_page(name: str, content: str) -> bool:
    """Create or update a wiki page."""
    try:
        os.makedirs(WIKI_MEMORY_DIR, exist_ok=True)
        fname = name if name.endswith(".md") else name + ".md"
        fpath = os.path.join(WIKI_MEMORY_DIR, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        logger.warning(f"[Wiki] save failed: {e}")
        return False


def _wiki_delete_page(name: str) -> bool:
    """Delete a wiki page."""
    for fname in os.listdir(WIKI_MEMORY_DIR):
        if fname.endswith(".md") and (fname[:-3] == name or fname.lower() == name.lower() + ".md"):
            os.remove(os.path.join(WIKI_MEMORY_DIR, fname))
            return True
    return False


def _wiki_cleanup():
    """Delete low-quality wiki pages (too short, garbage STT transcribe results)."""
    deleted = 0
    for fname in list(os.listdir(WIKI_MEMORY_DIR)):
        if not fname.endswith(".md"):
            continue
        # Skip important wiki files
        if fname in ("BOOTSTRAP.md", "Deployment-Guide.md", "Meta-Patterns.md", "Holographic-Memory.md",
                     "Neural-Networks.md", "Lessons-Learned.md", "Patterns.md", "Personal.md", "Project-Logic.md",
                     "Праздники России и Православной церкви.md", "avatar-studio-structure.md"):
            continue
        fpath = os.path.join(WIKI_MEMORY_DIR, fname)
        try:
            size = os.path.getsize(fpath)
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            # Delete files that are garbage
            is_garbage = False
            if size < 50 and not content.startswith("#"):
                is_garbage = True  # Empty/almost empty non-wiki files
            if fname.startswith("voice_") and size < 100:
                is_garbage = True  # STT garbage (single word transcriptions)
            if fname.startswith("_") and size < 100:
                is_garbage = True  # Temp files
            if is_garbage:
                os.remove(fpath)
                deleted += 1
                logger.info(f"[Wiki] cleanup deleted garbage: {fname} ({size} bytes)")
        except Exception as e:
            logger.debug(f"[Wiki] cleanup skipped {fname}: {e}")
    return deleted


def _wiki_extract_links(content: str) -> list:
    """Extract [[links]] from markdown content."""
    return re.findall(r'\[\[([^\]]+)\]\]', content)


def _wiki_get_graph() -> dict:
    """Get nodes and links for graph visualization."""
    pages = _wiki_list_pages()
    nodes = []
    links = []
    for p in pages:
        node_id = p["name"]
        nodes.append({"id": node_id, "title": p["title"], "group": 1, "size": p.get("size", 0)})
        content = ""
        try:
            with open(p["path"], "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            swallow.note('wiki_store._wiki_get_graph:L142')
            pass
        for link_name in _wiki_extract_links(content):
            # Normalize link: find actual page name
            link_target = link_name.strip()
            links.append({"source": node_id, "target": link_target, "value": 1})
    return {"nodes": nodes, "links": links}


def _wiki_search(query: str) -> list:
    """Search wiki pages by keyword."""
    words = query.lower().split()
    hits = []
    pages = _wiki_list_pages()
    for p in pages:
        try:
            with open(p["path"], "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            swallow.note('wiki_store._wiki_search:L161')
            continue
        score = 0
        for w in words:
            if w in text.lower():
                score += text.lower().count(w)
        if score > 0:
            hits.append({"name": p["name"], "title": p["title"], "score": score, "snippet": text[:200].replace("\n", " ")})
    hits.sort(key=lambda x: -x["score"])
    return hits[:10]
