"""Add source-channel episodes we don't have locally to the local-archive lanes.

Rows get download_status='source' (ignored by the global loader.to feeder and
by feed_and_advance's failed->pending reset); scripts/local_feed.py downloads
each one via loader.to when its lane reaches that episode (ORDER BY ord).

Also moves the existing سنن الترمذي catalog rows into the lane with a real ord:
منهج الترمذي 1..15 -> ord n-100 (first), شرح 1..28 -> ord n.

Usage: local_add_source.py source_add.json [--apply]
"""
import json
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "abuhafs.db"
TIRMIDHI = "snn-alamam-altrmthy"


def main():
    add = json.load(open(sys.argv[1], encoding="utf-8"))
    apply = "--apply" in sys.argv
    if apply:
        bak = DB.with_name(f"abuhafs.db.bak.sourceadd.{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(DB, bak)
        print("backup:", bak)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    ins = skipped = 0
    for a in add:
        ex = con.execute("SELECT series_slug, process_status FROM catalog_videos WHERE youtube_id=?",
                         (a["youtube_id"],)).fetchone()
        if ex:
            skipped += 1
            print(f"  exists already: {a['slug']} {a['ord']} {a['youtube_id']} -> {dict(ex)}")
            continue
        ins += 1
        if apply:
            con.execute(
                "INSERT INTO catalog_videos (youtube_id, youtube_url, title, series, series_slug, ord, "
                "download_status, process_status) VALUES (?,?,?,?,?,?, 'source', 'pending')",
                (a["youtube_id"], f"https://www.youtube.com/watch?v={a['youtube_id']}",
                 a["title"], a["series"], a["slug"], a["ord"]))
    # existing tirmidhi catalog rows -> lane, ordered
    moved = []
    for r in con.execute("SELECT id, title, download_status FROM catalog_videos WHERE series_slug=? "
                         "AND process_status='pending' AND download_status IN ('pending','failed')",
                         (TIRMIDHI,)).fetchall():
        t = r["title"]
        n = int(re.findall(r"\d+", t)[0])
        ord_ = n - 100 if ("منهج" in t) else n
        moved.append((ord_, t[:70]))
        if apply:
            con.execute("UPDATE catalog_videos SET download_status='source', ord=?, "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=?", (ord_, r["id"]))
    if apply:
        con.commit()
    print(("APPLIED" if apply else "DRY-RUN"), f"insert={ins} skipped_existing={skipped} tirmidhi_moved={len(moved)}")
    for o, t in sorted(moved)[:4] + sorted(moved)[-3:]:
        print(f"   ord {o:>4}  {t}")


if __name__ == "__main__":
    main()
