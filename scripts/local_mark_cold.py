"""Flip 'source' rows to 'local' once their files are on cold storage.

stdin: JSON list of {"youtube_id", "ref": "cold://<zip>/<member>", "size"}.
"""
import json
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "abuhafs.db"
items = json.load(sys.stdin)
con = sqlite3.connect(DB)
n = 0
for it in items:
    cur = con.execute(
        "UPDATE catalog_videos SET download_status='local', mp4_path=?, download_bytes=?, "
        "download_error='', updated_at=CURRENT_TIMESTAMP "
        "WHERE youtube_id=? AND download_status='source'",
        (it["ref"], it["size"], it["youtube_id"]))
    n += cur.rowcount
con.commit()
print(f"marked local: {n}/{len(items)}")
