"""Repoint local rows after their files were re-encoded to .mp4 before zipping.

stdin: JSON list of {"zip", "old", "new"} member names. Only rows still waiting
on cold storage (mp4_path='cold://<zip>/<old>') are touched.
"""
import json
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "abuhafs.db"

con = sqlite3.connect(DB)
n = 0
for m in json.load(sys.stdin):
    n += con.execute(
        "UPDATE catalog_videos SET mp4_path=?, updated_at=CURRENT_TIMESTAMP WHERE mp4_path=?",
        (f"cold://{m['zip']}/{m['new']}", f"cold://{m['zip']}/{m['old']}")).rowcount
con.commit()
print(f"repointed: {n}")
