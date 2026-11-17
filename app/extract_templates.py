#!/usr/bin/env python3
"""Извлекает PAGE_* HTML-строки из server_backup.py в файлы templates/page_*.html
и создаёт server/templates_static.py для загрузки из файлов.

Запуск: python3 extract_templates.py
"""
import ast
import os

SRC = "server_backup.py"
TPL_DIR = "templates"
TPL_MODULE = "server/templates_static.py"

# PAGE_* имена, которые имеют реальный HTML контент (не пустые и не динамические)
# Определяется по результатам AST-анализа
REAL_PAGES = {
    "PAGE_AGENT": "page_agent.html",
    "PAGE_AI_ANSWERS": "page_ai_answers.html",
    "PAGE_ARTICLE": "page_article.html",
    "PAGE_ASK": "page_ask.html",
    "PAGE_BOOKS": "page_books.html",
    "PAGE_INDEX": "page_index.html",
    "PAGE_INDEX_V2": "page_index_v2.html",
    "PAGE_LIST": "page_list.html",
    "PAGE_NOTES": "page_notes.html",
    "PAGE_NOTES_EDIT": "page_notes_edit.html",
    "PAGE_NOTES_GRAPH": "page_notes_graph.html",
    "PAGE_SETTINGS": "page_settings.html",
    "PAGE_ZIM": "page_zim.html",
    "PAGE_ZIM_VIEW": "page_zim_view.html",
}

def extract_pages():
    """Извлекает PAGE_* константы из AST и записывает в template файлы."""
    with open(SRC, 'r', encoding='utf-8-sig') as f:
        source = f.read()
    
    tree = ast.parse(source)
    
    # Собираем все PAGE_* присваивания с реальными строками
    # Ищем последнее присваивание для каждого имени (чтобы не взять промежуточное "")
    page_values = {}
    page_lines = {}
    
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith('PAGE_'):
                    name = target.id
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        val = node.value.value
                        if val:  # только непустые
                            page_values[name] = val
                            page_lines[name] = (node.lineno, node.end_lineno)
    
    os.makedirs(TPL_DIR, exist_ok=True)
    
    extracted = []
    for name, fname in REAL_PAGES.items():
        filepath = os.path.join(TPL_DIR, fname)
        if os.path.exists(filepath):
            existing_size = os.path.getsize(filepath)
            print(f"SKIP (exists): {name} -> {fname} ({existing_size} bytes)")
            extracted.append((name, fname, existing_size))
            continue
        if name in page_values:
            value = page_values[name]
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(value)
            print(f"OK: {name} ({len(value)} chars) -> {fname}")
            extracted.append((name, fname, len(value)))
        else:
            print(f"SKIP: {name} — не найдена в AST (возможно динамическая)")
    
    return extracted

def write_tpl_module(extracted):
    """Создаёт server/templates_static.py"""
    lines = []
    lines.append('"""Загрузка PAGE_* HTML-шаблонов из файлов templates/*.html"""')
    lines.append('import os')
    lines.append('')
    lines.append('_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")')
    lines.append('')
    lines.append('')
    lines.append('def _load_template(name):')
    lines.append('    path = os.path.join(_TEMPLATES_DIR, name)')
    lines.append('    if os.path.exists(path):')
    lines.append('        with open(path, "r", encoding="utf-8") as f:')
    lines.append('            return f.read()')
    lines.append('    return ""')
    lines.append('')
    for name, fname, size in extracted:
        lines.append(f'{name} = _load_template({fname!r})')
    lines.append('')
    
    with open(TPL_MODULE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"\nCreated: {TPL_MODULE}")

def main():
    print("Extracting PAGE_* templates from server_backup.py...")
    extracted = extract_pages()
    
    print(f"\nExtracted {len(extracted)} templates")
    total = sum(s for _,_,s in extracted)
    print(f"Total HTML chars: {total}")
    
    write_tpl_module(extracted)
    
    print("\nNext: update server/__init__.py to override PAGE_* names from templates_static")

if __name__ == '__main__':
    main()
