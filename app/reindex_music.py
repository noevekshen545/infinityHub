#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlite3

db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'media.db')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Clear old index
conn.execute('DELETE FROM music_tracks')
conn.execute('DELETE FROM listening_history')
conn.execute("DELETE FROM favorites WHERE type = 'track'")
conn.commit()

# Get saved dirs
cursor = conn.cursor()
cursor.execute("SELECT value FROM settings WHERE key = 'music_dirs'")
row = cursor.fetchone()
conn.close()

if row:
    dirs = [d.strip() for d in row['value'].split(',') if d.strip()]
    print(f"Directories: {dirs}")
else:
    dirs = []
    print("No saved directories found")

if dirs:
    print("Importing media_module...")
    import media_module as MM
    print(f"Rebuilding index for {len(dirs)} directories...")
    result = MM.rebuild_music_index(dirs)
    print(f"Done! Indexed {len(result)} tracks")
