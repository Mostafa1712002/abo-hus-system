"""Import the user's local video archive into catalog_videos.

Each manifest row is a local file that lives inside a per-series zip on cold
storage (tarweig ~/videos_cold_zips/<folder>.zip). Rows get
download_status='local' and mp4_path='cold://<zip>/<member>'; scripts/local_feed.py
pulls one file at a time into the workspace right before processing.

youtube_id = the source-channel video id when the same episode exists there
(so catalog_full_worker finds its SRT in the site DB), else a synthetic
'L-<slug>-<n>' id (main video only, no SRT/shorts).

Usage: local_import.py manifest.json [--apply]
"""
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "abuhafs.db"


def main():
    manifest = json.load(open(sys.argv[1], encoding="utf-8"))
    apply = "--apply" in sys.argv
    if apply:
        bak = DB.with_name(f"abuhafs.db.bak.localimport.{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(DB, bak)
        print("backup:", bak)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    stats = {"insert": 0, "convert": 0, "already_uploaded": 0}
    for r in manifest:
        slug = r["slug"]
        if not slug:
            row = con.execute("SELECT series_slug FROM catalog_videos WHERE series=? LIMIT 1",
                              (r["series"],)).fetchone()
            slug = row["series_slug"]
        ref = f"cold://{r['zip']}/{r['member']}"
        ex = con.execute("SELECT * FROM catalog_videos WHERE youtube_id=?", (r["youtube_id"],)).fetchone()
        if ex and ex["process_status"] == "uploaded":
            stats["already_uploaded"] += 1
            print(f"  already on channel: {r['folder']} {r['ord']} -> {ex['reupload_yt_id']}")
            continue
        if ex:
            stats["convert"] += 1
            if apply:
                con.execute(
                    "UPDATE catalog_videos SET download_status='local', process_status='pending', "
                    "mp4_path=?, ord=?, download_bytes=?, download_error='', "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (ref, r["ord"], r["size"], ex["id"]))
        else:
            stats["insert"] += 1
            url = f"https://www.youtube.com/watch?v={r['youtube_id']}" if r["has_src"] else ""
            if apply:
                con.execute(
                    "INSERT INTO catalog_videos (youtube_id, youtube_url, title, series, series_slug, "
                    "ord, download_status, mp4_path, download_bytes, process_status) "
                    "VALUES (?,?,?,?,?,?, 'local', ?, ?, 'pending')",
                    (r["youtube_id"], url, r["title"], r["series"], slug, r["ord"], ref, r["size"]))
    if apply:
        con.commit()
    print(("APPLIED " if apply else "DRY-RUN ") + json.dumps(stats))
    for s in con.execute("SELECT series, series_slug, download_status, process_status, COUNT(*) n "
                         "FROM catalog_videos WHERE download_status='local' "
                         "GROUP BY 1,2,3,4 ORDER BY 2"):
        print("  ", dict(s))


if __name__ == "__main__":
    main()
