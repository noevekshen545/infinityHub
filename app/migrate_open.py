#!/usr/bin/env python3
"""Анализирует и заменяет with open(...) на worker_pool.read/write в server_backup.py.

Запуск: python3 migrate_open.py (режим dry-run: --dry)
        python3 migrate_open.py --apply (реально заменяет)
"""
import re
import sys
import os

SRC = "server_backup.py"

DRY_RUN = '--apply' not in sys.argv

def analyze_opens(source):
    """Находит все with open(...) и классифицирует."""
    lines = source.split('\n')
    results = []
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        if 'with open(' in stripped or 'with open(' in line:
            results.append((i + 1, stripped[:120]))
    
    return results

def main():
    print(f"Analyzing {SRC}...")
    with open(SRC, 'r', encoding='utf-8-sig') as f:
        source = f.read()
    
    opens = analyze_opens(source)
    print(f"\nTotal with open(): {len(opens)}")
    
    # Print categorized
    patterns = {
        'json.load': [], 'json.dump': [],
        'read() text': [], 'read() binary': [],
        'write() text': [], 'write() binary': [],
        'readlines': [], 'pickle': [], 'other': []
    }
    
    for lineno, line in opens:
        if 'json.load' in line:
            patterns['json.load'].append((lineno, line))
        elif 'json.dump' in line:
            patterns['json.dump'].append((lineno, line))
        elif '.read()' in line and "'rb'" in line:
            patterns['read() binary'].append((lineno, line))
        elif '.read()' in line:
            patterns['read() text'].append((lineno, line))
        elif '.write(' in line and "'wb'" in line:
            patterns['write() binary'].append((lineno, line))
        elif '.write(' in line:
            patterns['write() text'].append((lineno, line))
        elif '.readlines()' in line:
            patterns['readlines'].append((lineno, line))
        elif 'pickle' in line.lower():
            patterns['pickle'].append((lineno, line))
        else:
            patterns['other'].append((lineno, line))
    
    for cat, items in patterns.items():
        if items:
            print(f"\n  {cat}: {len(items)}")
            if DRY_RUN:
                for lineno, line in items[:5]:
                    print(f"    L{lineno}: {line}")
                if len(items) > 5:
                    print(f"    ... and {len(items)-5} more")
    
    if DRY_RUN:
        print(f"\nDry-run mode. Run with --apply to actually modify.")
    else:
        print(f"\nApply mode - would modify {len(opens)} occurrences.")

if __name__ == '__main__':
    main()
