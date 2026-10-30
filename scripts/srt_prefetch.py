"""Prefetch SRTs from whisper.markoum.com for upcoming local-archive episodes.

Most local/source episodes have no subtitle_srt in the site DB, so
catalog_full_worker would upload them with the raw title and no shorts.
whisper is async: the first call queues the job ("Your request is in the
queue."), later calls return the SRT. This script asks for the next AHEAD
episodes (by ord) of every lane series and writes finished SRTs into the
worker's cache (/opt/abuhafs/catalog_meta/<yt>.srt), which fetch_srt reads
first. Synthetic 'L-' ids (files not on YouTube) are skipped.

Usage: srt_prefetch.py [AHEAD]   (cron, every 30 min)
"""
import json
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "abuhafs.db"
CACHE = Path("/opt/abuhafs/catalog_meta")
WHISPER = "https://whisper.markoum.com/whisper/youtube.php"
AHEAD = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def whisper(yt_id):
    q = urllib.parse.urlencode({"youtube": f"https://www.youtube.com/watch?v={yt_id}",
                                "lang": "ar", "onlyText": 0})
    with urllib.request.urlopen(f"{WHISPER}?{q}", timeout=120) as r:
        body = r.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except ValueError:
        return body if "-->" in body else "", body[:120]
    content = (data.get("content") or data.get("srt") or "") if isinstance(data, dict) else ""
    return (content if "-->" in content else ""), str(data)[:120]


def site_has_srt():
    """Video ids whose SRT already sits in the site DB (fetch_srt finds those itself)."""
    out = subprocess.run(["mysql", "--defaults-file=/root/.my.cnf", "abuhafs_info", "-N", "-e",
                          "SELECT video_id FROM videos WHERE LENGTH(subtitle_srt) > 100"],
                         capture_output=True, text=True).stdout
    return set(out.split())


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    have = site_has_srt()
    con = sqlite3.connect(DB)
    slugs = [r[0] for r in con.execute(
        "SELECT DISTINCT series_slug FROM catalog_videos WHERE process_status='pending' "
        "AND (download_status IN ('local','source') OR download_quality='local')")]
    for slug in slugs:
        rows = con.execute(
            "SELECT youtube_id, ord FROM catalog_videos WHERE series_slug=? AND process_status='pending' "
            "AND (download_status IN ('local','source') OR download_quality='local') "
            "AND youtube_id NOT LIKE 'L-%' ORDER BY ord, id", (slug,)).fetchall()
        asked = 0
        for yt, ord_ in rows:
            if asked >= AHEAD:
                break
            cache = CACHE / f"{yt}.srt"
            if yt in have or (cache.exists() and cache.stat().st_size > 50):
                continue
            asked += 1
            try:
                srt, info = whisper(yt)
            except Exception as e:
                print(f"{slug} {ord_} {yt}: error {e}")
                continue
            if srt:
                cache.write_text(srt, encoding="utf-8")
                print(f"{slug} {ord_} {yt}: SRT saved ({len(srt)} chars)")
            else:
                print(f"{slug} {ord_} {yt}: {info}")


if __name__ == "__main__":
    main()
