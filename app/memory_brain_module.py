"""Memory Brain — модуль генерации графа знаний из .md файлов."""

import os, re, json, hashlib, time, threading
from pathlib import Path
from datetime import datetime
import swallow

SECTION_COLORS = {
    "PATTERNS": {"color": "#a78bfa", "hex": 0xa78bfa, "label": "Patterns",        "emoji": "🧩"},
    "PROJECT":  {"color": "#60a5fa", "hex": 0x60a5fa, "label": "Project Logic",   "emoji": "⚙️"},
    "LESSONS":  {"color": "#34d399", "hex": 0x34d399, "label": "Lessons Learned", "emoji": "📚"},
    "FACTS":    {"color": "#fbbf24", "hex": 0xfbbf24, "label": "Holographic",     "emoji": "🔮"},
    "HOLIDAYS": {"color": "#f472b6", "hex": 0xf472b6, "label": "Holidays",        "emoji": "🎄"},
    "GENERAL":  {"color": "#818cf8", "hex": 0x818cf8, "label": "General",         "emoji": "📄"},
    "NOTES":    {"color": "#22d3ee", "hex": 0x22d3ee, "label": "Notes",           "emoji": "📝"},
    "AI_KNOWLEDGE": {"color": "#f97316", "hex": 0xf97316, "label": "AI Knowledge", "emoji": "🤖"},
    "PERSONAL":     {"color": "#fb923c", "hex": 0xfb923c, "label": "Personal",     "emoji": "👤"},
}

def detect_section(filename, dirname):
    name_lower = filename.lower()
    if "pattern" in name_lower: return "PATTERNS"
    if "project" in name_lower or "logic" in name_lower: return "PROJECT"
    if "lesson" in name_lower: return "LESSONS"
    if "fact" in name_lower: return "FACTS"
    if "holiday" in name_lower or "праздник" in name_lower: return "HOLIDAYS"
    if "personal" in name_lower or "user" in name_lower: return "PERSONAL"
    dir_lower = dirname.lower()
    for key in ["PATTERNS", "PROJECT", "LESSONS", "FACTS", "HOLIDAYS", "NOTES", "AI_KNOWLEDGE"]:
        if key.lower() in dir_lower: return key
    return "GENERAL"

def extract_title(content, filename):
    match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if match: return match.group(1).strip()
    return Path(filename).stem.replace("-", " ").replace("_", " ").title()

def extract_subheadings(content):
    return re.findall(r'^#{2,3}\s+(.+)$', content, re.MULTILINE)

def extract_tags(content):
    tags = set()
    for match in re.finditer(r'(?<!\n)#(\w{3,})', content):
        tag = match.group(1).lower()
        if tag not in ('the', 'and', 'for', 'this', 'that', 'with'):
            tags.add(tag)
    return list(tags)[:10]

def extract_links(content):
    return [m.group(1).strip() for m in re.finditer(r'\[\[(.+?)\]\]', content)]

def get_first_paragraph(content):
    lines = content.split('\n')
    paragraph_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if paragraph_lines: break
            continue
        if stripped.startswith('#') or stripped.startswith('---'): continue
        paragraph_lines.append(stripped)
        if len(' '.join(paragraph_lines)) > 200: break
    result = ' '.join(paragraph_lines)
    return result[:300] + ('...' if len(result) > 300 else '')

def parse_md_file(filepath, wiki_dir):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    rel_path = os.path.relpath(filepath, wiki_dir)
    filename = os.path.basename(filepath)
    dirname  = os.path.dirname(rel_path)
    content_length = len(content)
    if content_length > 10000: size = 1.5
    elif content_length > 5000: size = 1.2
    elif content_length > 1000: size = 1.0
    else: size = 0.8
    node_id = hashlib.md5(rel_path.encode()).hexdigest()[:8]
    mtime = os.path.getmtime(filepath)
    section = detect_section(filename, dirname)
    sec_color = SECTION_COLORS.get(section, SECTION_COLORS["GENERAL"])
    return {
        "id": node_id,
        "title": extract_title(content, filename),
        "description": get_first_paragraph(content),
        "content": content,
        "section": section,
        "color": sec_color["color"],
        "filename": filename,
        "filepath": rel_path,
        "subheadings": extract_subheadings(content),
        "tags": extract_tags(content),
        "links": extract_links(content),
        "size": size,
        "word_count": len(content.split()),
        "char_count": content_length,
        "modified": datetime.fromtimestamp(mtime).isoformat(),
    }

def tokenize(text):
    words = re.findall(r'[a-zA-Zа-яА-ЯёЁ]{4,}', text.lower())
    stopwords = {
        'что', 'это', 'как', 'для', 'при', 'все', 'так', 'его', 'её',
        'они', 'мы', 'вы', 'тот', 'или', 'но', 'да', 'нет',
        'был', 'была', 'были', 'будет', 'есть', 'ещё', 'уже', 'тоже',
        'очень', 'можно', 'нужно', 'надо', 'здесь', 'там', 'где',
        'когда', 'если', 'чтобы', 'потом', 'после', 'перед', 'между',
        'через', 'без', 'над', 'под', 'про',
        'the', 'and', 'for', 'this', 'that', 'with', 'from', 'have',
        'will', 'been', 'were', 'they', 'their', 'what', 'which',
        'about', 'would', 'could', 'should', 'there', 'here', 'into',
    }
    return set(w for w in words if w not in stopwords)

def build_links(nodes):
    links = []
    title_to_id = {n['title'].lower(): n['id'] for n in nodes}
    node_tokens = {n['id']: tokenize(n.get('content', '') + ' ' + n.get('title', '')) for n in nodes}
    for i, node_a in enumerate(nodes):
        for j, node_b in enumerate(nodes):
            if i >= j: continue
            weight = 0
            link_types = []
            for link_text in node_a.get('links', []):
                if link_text.lower() in title_to_id and title_to_id[link_text.lower()] == node_b['id']:
                    weight += 3
                    link_types.append('explicit')
            for link_text in node_b.get('links', []):
                if link_text.lower() in title_to_id and title_to_id[link_text.lower()] == node_a['id']:
                    weight += 3
                    link_types.append('explicit')
            common_tags = set(node_a.get('tags', [])) & set(node_b.get('tags', []))
            if common_tags:
                weight += len(common_tags) * 2
                link_types.append('tags')
            if node_a['section'] == node_b['section']:
                weight += 1
                link_types.append('section')
            if node_b['title'].lower() in node_a.get('content', '').lower():
                weight += 2
                link_types.append('mention')
            if node_a['title'].lower() in node_b.get('content', '').lower():
                weight += 2
                link_types.append('mention')
            tokens_a = node_tokens[node_a['id']]
            tokens_b = node_tokens[node_b['id']]
            if len(tokens_a) >= 3 and len(tokens_b) >= 3:
                union = tokens_a | tokens_b
                jaccard = len(tokens_a & tokens_b) / len(union) if union else 0
                if jaccard > 0.15:
                    weight += int(jaccard * 5)
                    link_types.append('semantic')
            if weight > 0:
                priority = ['explicit', 'tags', 'mention', 'section', 'semantic']
                primary = 'semantic'
                for p in priority:
                    if p in link_types:
                        primary = p
                        break
                links.append({
                    "source": node_a['id'],
                    "target": node_b['id'],
                    "weight": min(weight, 5),
                    "linkType": primary,
                })
    return links

def dedup_nodes(nodes):
    seen = {}
    for node in nodes:
        key = node['title'].lower()
        if key not in seen or node['char_count'] > seen[key]['char_count']:
            seen[key] = node
    removed = len(nodes) - len(seen)
    if removed > 0:
        print(f"  Removed {removed} duplicate node(s) by title")
    return list(seen.values())

def dedup_links(links):
    seen = set()
    unique = []
    for link in links:
        key = (link['source'], link['target'])
        rkey = (link['target'], link['source'])
        if key not in seen and rkey not in seen:
            seen.add(key)
            unique.append(link)
    removed = len(links) - len(unique)
    if removed > 0:
        print(f"  Removed {removed} duplicate link(s)")
    return unique

def generate_graph(wiki_dir):
    wiki_path = Path(wiki_dir)
    if not wiki_path.exists():
        raise FileNotFoundError(f"Wiki directory not found: {wiki_dir}")
    md_files = []
    for pattern in ['*.md', '*.MD']:
        md_files.extend(wiki_path.glob(pattern))
    ALLOWED_SUBDIRS = {'patterns', 'playbooks', 'tools', 'project', 'lessons', 'personal', 'facts', 'holidays', 'notes', 'ai-knowledge'}
    for subdir in ALLOWED_SUBDIRS:
        subpath = wiki_path / subdir
        if subpath.exists():
            for pattern in ['*.md', '*.MD']:
                md_files.extend(subpath.glob(pattern))
    md_files = [f for f in md_files if not f.name.startswith('.')]
    print(f"Found {len(md_files)} markdown files in {wiki_dir}")
    nodes = []
    for filepath in sorted(md_files):
        try:
            node = parse_md_file(str(filepath), wiki_dir)
            nodes.append(node)
        except Exception as e:
            print(f"  Error parsing {filepath.name}: {e}")
    nodes = dedup_nodes(nodes)
    links = build_links(nodes)
    links = dedup_links(links)
    sections = {}
    for node in nodes:
        sec = node['section']
        if sec not in sections:
            sections[sec] = {
                "name": sec,
                "label": SECTION_COLORS.get(sec, SECTION_COLORS["GENERAL"])["label"],
                "color": SECTION_COLORS.get(sec, SECTION_COLORS["GENERAL"])["color"],
                "emoji": SECTION_COLORS.get(sec, SECTION_COLORS["GENERAL"])["emoji"],
                "nodes": [],
            }
        sections[sec]["nodes"].append(node["id"])
    for sec in sections:
        sections[sec]["count"] = len(sections[sec]["nodes"])
    stats = {
        "total_nodes": len(nodes), "total_links": len(links),
        "sections": len(sections),
        "total_words": sum(n["word_count"] for n in nodes),
        "total_chars": sum(n["char_count"] for n in nodes),
        "generated_at": datetime.now().isoformat(),
    }
    return {
        "metadata": {"generator": "Memory Brain v3.0", "wiki_dir": wiki_dir, "version": "3.0", "generated_at": datetime.now().isoformat()},
        "stats": stats, "sections": sections, "nodes": nodes, "links": links,
    }


# ─── Кэш графа ───────────────────────────────────────────────────────

def _wiki_dir_sig(wiki_dir):
    """Подпись каталога: относительный путь + mtime_ns + размер каждого файла.

    Обход стоит ~3.7 мс на каталоге из 26 файлов (замер), то есть на два
    порядка дешевле пересборки графа. Отдельно по *.md не делаем: лишние
    файлы просто вызывают редкую бесполезную пересборку, а пропущенное
    удаление .md — наоборот, протухший граф навсегда.
    """
    parts = []
    for root, subdirs, files in os.walk(wiki_dir):
        subdirs.sort()
        for f in sorted(files):
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
            except OSError:
                swallow.note('memory_brain_module._wiki_dir_sig:L261')
                continue
            parts.append((os.path.relpath(fp, wiki_dir), st.st_mtime_ns, st.st_size))
    return parts


# Одна запись [подпись, граф]: пересборка полная, история версий не нужна.
_graph_cache = []
_graph_lock = threading.Lock()


def graph_cached(wiki_dir):
    """generate_graph с переиспользованием результата, пока каталог не тронут.

    Один вызов стоил 187–409 мс живьём при 26 файлах; граф меняется только
    вместе с каталогом, поэтому при неизменной подписи отдаём тот же объект.
    Замок держим и на сборку: два одновременных промаха иначе собрали бы
    граф дважды (прогрев на старте дерёт этот путь пачкой запросов).
    Возвращаем общий объект — читатели его только сериализуют.
    """
    sig = _wiki_dir_sig(wiki_dir)
    with _graph_lock:
        if _graph_cache and _graph_cache[0] == sig:
            return _graph_cache[1]
        graph = generate_graph(wiki_dir)
        _graph_cache[:] = [sig, graph]
        return graph


# ─── CRUD операции ───────────────────────────────────────────────────

SECTION_DIRS = {
    'PATTERNS': 'patterns', 'PROJECT': 'project', 'LESSONS': 'lessons',
    'FACTS': 'facts', 'HOLIDAYS': 'holidays', 'PERSONAL': 'personal',
    'NOTES': 'notes', 'AI_KNOWLEDGE': 'ai-knowledge',
}

def save_node(wiki_dir, title, content, section="GENERAL", filename=""):
    if not filename:
        safe = re.sub(r'[^\wа-яА-ЯёЁ\s-]', '', title).strip().lower().replace(' ', '_')[:60]
        safe = re.sub(r'_+', '_', safe).strip('_')
        filename = (safe or 'node') + '.md'
    if not filename.endswith('.md'):
        filename += '.md'
    subdir = SECTION_DIRS.get(section, '')
    target_dir = os.path.join(wiki_dir, subdir) if subdir else wiki_dir
    os.makedirs(target_dir, exist_ok=True)
    filepath = os.path.join(target_dir, filename)
    if os.path.exists(filepath):
        base = filename[:-3]
        counter = 2
        while os.path.exists(os.path.join(target_dir, f"{base}-{counter}.md")):
            counter += 1
        filename = f"{base}-{counter}.md"
        filepath = os.path.join(target_dir, filename)
    md_content = f"# {title}\n\n{content}\n\n---\n*Сохранено {datetime.now().strftime('%Y-%m-%d %H:%M')}*"
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(md_content)
    print(f"Saved: {filepath}")
    return filepath, filename

def delete_node(wiki_dir, node_id="", filepath=""):
    target_path = None
    if filepath:
        candidate = os.path.join(wiki_dir, filepath)
        if os.path.exists(candidate):
            target_path = candidate
    if not target_path and node_id:
        for root, dirs, files in os.walk(wiki_dir):
            for fname in files:
                if fname.endswith('.md'):
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, wiki_dir)
                    check_id = hashlib.md5(rel.encode()).hexdigest()[:8]
                    if check_id == node_id:
                        target_path = full
                        break
            if target_path:
                break
    if not target_path or not os.path.exists(target_path):
        raise FileNotFoundError(f"Node not found: id={node_id}, filepath={filepath}")
    os.remove(target_path)
    rel = os.path.relpath(target_path, wiki_dir)
    print(f"Deleted: {rel}")
    return rel

def edit_node(wiki_dir, node_id="", filepath="", new_title="", new_section="", new_content="", new_tags=None):
    target_path = None
    if filepath and os.path.exists(os.path.join(wiki_dir, filepath)):
        target_path = os.path.join(wiki_dir, filepath)
    elif node_id:
        for root, dirs, files in os.walk(wiki_dir):
            for fname in files:
                if fname.endswith('.md'):
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, wiki_dir)
                    check_id = hashlib.md5(rel.encode()).hexdigest()[:8]
                    if check_id == node_id:
                        target_path = full
                        break
            if target_path:
                break
    if not target_path:
        raise FileNotFoundError(f"Node not found: id={node_id}")
    old_section = detect_section(os.path.basename(target_path), os.path.dirname(target_path))
    if new_section and new_section != old_section:
        subdir = SECTION_DIRS.get(new_section, '')
        new_dir = os.path.join(wiki_dir, subdir) if subdir else wiki_dir
        os.makedirs(new_dir, exist_ok=True)
        safe = re.sub(r'[^\wа-яА-ЯёЁ\s-]', '', new_title).strip().lower().replace(' ', '_')[:60]
        safe = re.sub(r'_+', '_', safe).strip('_')
        new_filename = (safe or 'node') + '.md'
        new_path = os.path.join(new_dir, new_filename)
        if os.path.exists(new_path) and new_path != target_path:
            base = safe or 'node'
            counter = 2
            while os.path.exists(os.path.join(new_dir, f"{base}-{counter}.md")):
                counter += 1
            new_filename = f"{base}-{counter}.md"
            new_path = os.path.join(new_dir, new_filename)
        updated = new_content
        if not updated.startswith('# ') and new_title:
            updated = f"# {new_title}\n\n" + updated
        with open(new_path, 'w', encoding='utf-8') as f:
            f.write(updated)
        if new_path != target_path and os.path.exists(target_path):
            os.remove(target_path)
        print(f"Moved: {os.path.relpath(target_path, wiki_dir)} -> {os.path.relpath(new_path, wiki_dir)}")
    else:
        updated = new_content
        if not updated.startswith('# ') and new_title:
            updated = f"# {new_title}\n\n" + updated
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(updated)
        print(f"Updated: {os.path.relpath(target_path, wiki_dir)}")
