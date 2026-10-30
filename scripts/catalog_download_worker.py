"""Download catalog videos (from abuhafs.info) to the VPS via loader.to.

Pulls rows from catalog_videos where download_status='pending', downloads the
mp4 with loader.to (bypasses YouTube's datacenter block), and updates the
tracking row. Series-by-series, resumable, storage-guarded.

Usage:
  python catalog_download_worker.py --series <slug> [--limit N] [--format 720]
  python catalog_download_worker.py --limit 5        # next 5 pending (any series)
  python catalog_download_worker.py --series <slug> --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
DB = ROOT / "data" / "abuhafs.db"
WORKSPACE = Path("/opt/abuhafs/catalog_workspace")
MIN_FREE_GB = 15          # stop downloading if free space drops below this
POLITE_DELAY = 4          # seconds between downloads (be nice to loader.to)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://loader.to/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def loader_download(yt_id: str, out_path: Path, fmt: str = "720") -> dict:
    yt_url = f"https://www.youtube.com/watch?v={yt_id}"
    api = "https://loader.to/ajax/download.php?" + urllib.parse.urlencode({"format": fmt, "url": yt_url})
    data = json.loads(_get(api))
    jid = data.get("id")
    if not jid:
        raise RuntimeError(f"no job id: {data}")
    # poll
    dl_url = ""
    waited = 0
    while waited < 2400:  # up to 40 min for very long lectures
        p = json.loads(_get("https://loader.to/ajax/progress.php?id=" + jid))
        dl_url = p.get("download_url") or ""
        if dl_url:
            break
        if p.get("success") == 0 and str(p.get("text", "")).lower() in ("error", "failed"):
            raise RuntimeError(f"job failed: {p}")
        time.sleep(5)
        waited += 5
    if not dl_url:
        raise TimeoutError("no download_url before timeout")
    # fetch file
    req = urllib.request.Request(dl_url, headers={"User-Agent": UA})
    total = 0
    with urllib.request.urlopen(req, timeout=900) as r, open(out_path, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    if total < 10000:
        raise RuntimeError(f"file too small ({total} bytes) — likely a download error page")
    return {"job_id": jid, "download_url": dl_url, "bytes": total}


def free_gb(path: Path) -> float:
    return shutil.disk_usage(str(path)).free / (1024 ** 3)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", help="series_slug to download")
    ap.add_argument("--limit", type=int, default=0, help="max videos this run (0=all in scope)")
    ap.add_argument("--format", default="720", help="loader.to format (720/1080/360/best)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    q = "SELECT * FROM catalog_videos WHERE download_status='pending'"
    params: list = []
    if args.series:
        q += " AND series_slug=?"
        params.append(args.series)
    q += " ORDER BY series_slug, ord, id"
    if args.limit:
        q += f" LIMIT {args.limit}"
    rows = cur.execute(q, params).fetchall()

    print(f"target videos: {len(rows)}  (format={args.format})")
    if args.dry_run:
        for r in rows[:30]:
            print(f"  [{r['series_slug']}] ord={r['ord']} {r['youtube_id']}  {r['title'][:55]}")
        return

    ok = fail = 0
    for r in rows:
        if free_gb(WORKSPACE) < MIN_FREE_GB:
            print(f"  ! disk guard: free<{MIN_FREE_GB}GB, stopping")
            break
        yt = r["youtube_id"]
        sdir = WORKSPACE / (r["series_slug"] or "misc")
        sdir.mkdir(parents=True, exist_ok=True)
        out = sdir / f"{yt}.mp4"
        cur.execute("UPDATE catalog_videos SET download_status='downloading', updated_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
        con.commit()
        t0 = time.time()
        try:
            info = loader_download(yt, out, args.format)
            # probe actual resolution
            cur.execute(
                """UPDATE catalog_videos SET download_status='downloaded', mp4_path=?,
                   download_bytes=?, download_quality=?, downloaded_at=CURRENT_TIMESTAMP,
                   download_error='', updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (str(out), info["bytes"], args.format, r["id"]),
            )
            con.commit()
            ok += 1
            mb = info["bytes"] / 1024 / 1024
            print(f"  ✓ {yt}  {mb:6.1f}MB  {time.time()-t0:5.0f}s  {r['title'][:45]}")
        except Exception as e:
            cur.execute(
                "UPDATE catalog_videos SET download_status='failed', download_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(e)[:300], r["id"]),
            )
            con.commit()
            if out.exists():
                out.unlink()
            fail += 1
            print(f"  ✗ {yt}  ERROR: {str(e)[:90]}")
        time.sleep(POLITE_DELAY)

    print(f"\ndone: ok={ok} fail={fail}  free={free_gb(WORKSPACE):.1f}GB")
    con.close()


if __name__ == "__main__":
    main()
