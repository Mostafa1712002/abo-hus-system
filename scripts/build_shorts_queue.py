"""Build a queue file for the autonomous worker to upload all 18 shorts
over the next ~4 days. Each short is scheduled to publish 30 minutes after
its parent main video goes live on YouTube."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

VIDEO_DIR = Path("/tmp/regen_videos")
SHORTS_DIR = Path("/tmp/regen_shorts")
SHORTS_INDEX = SHORTS_DIR / "shorts_index.json"
METADATA = Path("/tmp/metadata_unlisted.json")
SCHEDULE_BACKUP = Path("/tmp/regen_schedule_backup.json")
QUEUE_OUT = Path("/opt/abuhafs/youtube-auto-uploader/data/regen_shorts_queue.json")


def main() -> None:
    index = json.loads(SHORTS_INDEX.read_text(encoding="utf-8"))
    metadata = {v["video_id"]: v for v in json.loads(METADATA.read_text(encoding="utf-8"))}
    schedules = {"lbCW0L29C8Q": "2026-05-18T17:00:00Z"}
    if SCHEDULE_BACKUP.exists():
        for e in json.loads(SCHEDULE_BACKUP.read_text(encoding="utf-8")):
            schedules[e["video_id"]] = e["publishAt"]

    # Order shorts by parent video's publish time, then short index
    enriched = []
    for s in index:
        parent_iso = schedules.get(s["parent_video_id"])
        if not parent_iso:
            print(f"  warn: no schedule for parent {s['parent_video_id']}")
            continue
        parent_dt = dt.datetime.fromisoformat(parent_iso.replace("Z", "+00:00"))
        # Shorts publish 30/60/90 min after parent (3 shorts per parent)
        short_dt = parent_dt + dt.timedelta(minutes=30 * s["short_idx"])
        enriched.append({
            "short_id": Path(s["mp4"]).stem,
            "parent_video_id": s["parent_video_id"],
            "parent_yt_url": f"https://youtu.be/{s['parent_video_id']}",
            "parent_thumbnail_url": f"https://i.ytimg.com/vi/{s['parent_video_id']}/maxresdefault.jpg",
            "mp4_path": s["mp4"],
            "jpg_path": s["jpg"],
            "title": s["title"],
            "hook": s["hook"],
            "publish_at_iso": short_dt.isoformat().replace("+00:00", "Z"),
            "publish_unix": int(short_dt.timestamp()),
            "uploaded_yt": False,
            "uploaded_fb_reel": False,
            "uploaded_ig_reel": False,
            "sent_telegram": False,
            "yt_short_id": None,
            "fb_reel_id": None,
            "ig_reel_id": None,
            "tg_msg_id": None,
        })
    enriched.sort(key=lambda x: x["publish_unix"])

    QUEUE_OUT.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_OUT.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"queue saved: {QUEUE_OUT} ({len(enriched)} shorts)")
    for s in enriched:
        print(f"  {s['publish_at_iso']}  {s['short_id']:30}  {s['title'][:50]}")


if __name__ == "__main__":
    main()
