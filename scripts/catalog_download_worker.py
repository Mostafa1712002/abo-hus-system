"""Download catalog videos (from abuhafs.info) to the VPS with yt-dlp over Cloudflare WARP.

Pulls rows from catalog_videos where download_status='pending', downloads the
mp4 and updates the tracking row. Series-by-series, resumable, storage-guarded.

YouTube answers this datacenter IP with "Sign in to confirm you're not a bot",
and loader.to (the old workaround) has required a paid API key for server use
since 2026-10-27. So yt-dlp goes out through the local WARP client, which runs
in proxy-only mode (`warp-cli mode proxy`, SOCKS5 on 127.0.0.1:40000) and
leaves the box's own traffic untouched.

Usage:
  python catalog_download_worker.py --series <slug> [--limit N] [--format 720]
  python catalog_download_worker.py --limit 5        # next 5 pending (any series)
  python catalog_download_worker.py --series <slug> --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
DB = ROOT / "data" / "abuhafs.db"
WORKSPACE = Path("/opt/abuhafs/catalog_workspace")
MIN_FREE_GB = 10          # stop downloading if free space drops below this (buffer is only ~5 videos)
POLITE_DELAY = 4          # seconds between downloads
WARP_PROXY = "socks5://127.0.0.1:40000"


def _ytdlp(yt_id: str, out_path: Path, fmt: str) -> None:
    height = fmt if str(fmt).isdigit() else "720"
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-part", "--no-playlist",
           "--proxy", WARP_PROXY,
           "-f", f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={height}]+ba/b[height<={height}]",
           "--merge-output-format", "mp4", "-o", str(out_path),
           f"https://www.youtube.com/watch?v={yt_id}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"yt-dlp rc={r.returncode}: {(r.stderr or r.stdout).strip()[-250:]}")


def loader_download(yt_id: str, out_path: Path, fmt: str = "720") -> dict:
    """Download one video to out_path (name kept from the loader.to days; local_feed imports it)."""
    for attempt, wait in enumerate((20, 60, 0), 1):
        try:
            _ytdlp(yt_id, out_path, fmt)
            break
        except Exception:
            # --no-part leaves half-written stream files (x.f140.m4a ...) that make the
            # next try resume past their end (HTTP 416) — start clean
            for frag in out_path.parent.glob(out_path.stem + ".f*"):
                frag.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(wait)
    total = out_path.stat().st_size
    if total < 10000:
        raise RuntimeError(f"file too small ({total} bytes)")
    return {"job_id": "", "download_url": "", "bytes": total}


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
