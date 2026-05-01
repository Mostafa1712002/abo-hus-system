"""Generate two thumbnails — one with the FSRCNN enhancement, one without —
so we can eyeball the quality jump. Saves into /tmp/thumb_before.jpg and
/tmp/thumb_after.jpg.
"""
from __future__ import annotations

import copy
import sys
import time
from pathlib import Path

from src.config import Config
from src.thumbnail import make_thumbnail


def main() -> int:
    cfg = Config("config.json")
    video = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "/opt/abuhafs/videos_workspace/شرح الرسالة/الرسالة 1.mp4"
    )
    title = sys.argv[2] if len(sys.argv) > 2 else "أسرار الرسالة"

    if not video.exists():
        print(f"NOT FOUND: {video}")
        return 1

    before = Path("/tmp/thumb_before.jpg")
    after = Path("/tmp/thumb_after.jpg")

    cfg_off = copy.deepcopy(cfg.thumbnail)
    cfg_off.setdefault("enhance", {})
    cfg_off["enhance"]["enabled"] = False

    cfg_on = copy.deepcopy(cfg.thumbnail)
    cfg_on.setdefault("enhance", {})
    cfg_on["enhance"]["enabled"] = True

    print(f"video: {video}")
    print(f"title: {title}\n")

    print("→ generating BEFORE (enhance OFF)...")
    t0 = time.time()
    make_thumbnail(video_path=video, title=title, output_path=before, config=cfg_off)
    print(f"  done in {time.time()-t0:.1f}s, size={before.stat().st_size//1024}KB")

    print("→ generating AFTER (enhance ON)...")
    t0 = time.time()
    make_thumbnail(video_path=video, title=title, output_path=after, config=cfg_on)
    print(f"  done in {time.time()-t0:.1f}s, size={after.stat().st_size//1024}KB")

    print(f"\nBefore: {before}")
    print(f"After:  {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
