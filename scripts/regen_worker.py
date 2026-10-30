"""Autonomous worker that runs every 15 minutes via cron on VPS.

Responsibilities:
  1. Telegram queue: send any deferred TG posts whose publish_unix has arrived
  2. Shorts queue: upload each short to YouTube/FB Reel as its scheduled time
     approaches (15 min before parent publish), respecting daily quota budget
  3. Update queue state in-place after each successful upload

Quota budget: stop uploading YouTube shorts when est. daily usage > 8500
(leaves 1500 buffer for pipeline's regular calls).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/abuhafs/youtube-auto-uploader")
sys.stdout.reconfigure(encoding="utf-8")

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from src.config import Config
from src.facebook_uploader import upload_reel_to_page
from src.pipeline import _build_fb_reel_description, _build_telegram_main_text
from src.telegram_uploader import send_message
from src.youtube_uploader import upload_short

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
TG_QUEUE = ROOT / "data" / "deferred_tg.json"
SHORTS_QUEUE = ROOT / "data" / "regen_shorts_queue.json"
LOG_FILE = ROOT / "logs" / "regen_worker.log"

# Daily YouTube quota budget for shorts uploads (leave room for normal pipeline)
DAILY_SHORTS_BUDGET = 8500  # units
QUOTA_PER_SHORT = 1650

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("regen_worker")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _read_json(p: Path) -> list:
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(p: Path, data) -> None:
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def quota_used_today_estimate() -> int:
    """Estimate today's quota usage from shorts queue (since pipeline tracking
    is in-process). Counts shorts uploaded today × 1650."""
    today_utc = _utc_now().date()
    shorts = _read_json(SHORTS_QUEUE)
    n = 0
    for s in shorts:
        if not s.get("uploaded_yt"):
            continue
        ts = s.get("uploaded_yt_at")
        if not ts:
            continue
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        if d == today_utc:
            n += 1
    return n * QUOTA_PER_SHORT


def dispatch_telegram() -> None:
    queue = _read_json(TG_QUEUE)
    if not queue:
        return
    cfg = Config(str(ROOT / "config.json"))
    tg_creds = json.loads((ROOT / "credentials" / "telegram_credentials.json").read_text(encoding="utf-8"))
    bot = tg_creds["bot_token"]
    chat = tg_creds.get("channel_numeric_id") or tg_creds.get("channel_username")
    now = int(time.time())
    changed = False
    for item in queue:
        if item.get("sent"):
            continue
        if item.get("publish_unix", 0) > now:
            continue
        try:
            text = _build_telegram_main_text(
                title=item["title"],
                body=item["description"],
                yt_url=item["yt_url"],
                series="",
                next_video_teaser="",
            )
            msg_id = send_message(bot, chat, text, parse_mode="HTML")
            item["sent"] = True
            item["sent_msg_id"] = msg_id
            item["sent_at"] = _utc_now().isoformat().replace("+00:00", "Z")
            log.info(f"TG sent for {item['video_id']}: msg_id={msg_id}")
            changed = True
        except Exception as e:
            log.warning(f"TG send failed for {item['video_id']}: {e}")
    if changed:
        _write_json(TG_QUEUE, queue)


def get_youtube():
    creds = Credentials.from_authorized_user_file(str(ROOT / "credentials" / "token.json"))
    if creds.expired:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_shorts() -> None:
    queue = _read_json(SHORTS_QUEUE)
    if not queue:
        return
    cfg = Config(str(ROOT / "config.json"))
    used = quota_used_today_estimate()
    log.info(f"shorts: today quota used ~{used}/{DAILY_SHORTS_BUDGET}")
    if used >= DAILY_SHORTS_BUDGET:
        log.info("shorts: daily budget exhausted, skipping")
        return

    yt = None
    meta_creds = json.loads((ROOT / "credentials" / "meta_credentials.json").read_text(encoding="utf-8"))
    page = meta_creds["pages"][0]

    now = int(time.time())
    UPLOAD_LEAD_SEC = 30 * 60  # upload 30 min before publish_unix

    changed = False
    for s in queue:
        if s.get("uploaded_yt") and s.get("uploaded_fb_reel"):
            continue
        if s["publish_unix"] - UPLOAD_LEAD_SEC > now:
            continue  # not yet time to upload
        # Budget check before YT
        if used + QUOTA_PER_SHORT > DAILY_SHORTS_BUDGET:
            log.info(f"  budget would be exceeded for {s['short_id']}, deferring")
            break
        short_path = Path(s["mp4_path"])
        if not short_path.exists():
            log.warning(f"  missing file: {short_path}")
            continue
        title = s["title"]
        description = (
            f"{s.get('hook','')}\n\n"
            f"شاهد المحاضرة كاملة: {s['parent_yt_url']}\n\n"
            "#Shorts #فضيلة_الشيخ_أبو_حفص_الأثري #دروس_شرعية"
        )
        tags = ["Shorts", "دروس شرعية", "فضيلة الشيخ أبو حفص الأثري", "إسلاميات"]
        if not s.get("uploaded_yt"):
            try:
                yt = yt or get_youtube()
                publish_iso = dt.datetime.fromtimestamp(s["publish_unix"], dt.timezone.utc).isoformat().replace("+00:00", "Z")
                yt_id = upload_short(yt, file_path=short_path, title=title,
                                     description=description, tags=tags,
                                     privacy="private", publish_at=publish_iso,
                                     category_id="27")
                s["uploaded_yt"] = True
                s["yt_short_id"] = yt_id
                s["uploaded_yt_at"] = _utc_now().isoformat().replace("+00:00", "Z")
                log.info(f"  ✓ YT short: https://youtube.com/shorts/{yt_id}")
                used += QUOTA_PER_SHORT
                changed = True
            except Exception as e:
                log.warning(f"  ✗ YT short upload failed for {s['short_id']}: {e}")
                # Don't continue with FB until YT works
                continue
        if not s.get("uploaded_fb_reel"):
            try:
                caption = _build_fb_reel_description(short_description=description,
                                                     series="", next_video_teaser="")
                fb_id = upload_reel_to_page(page_id=page["page_id"],
                                            page_access_token=page["page_access_token"],
                                            video_path=short_path, description=caption)
                s["uploaded_fb_reel"] = True
                s["fb_reel_id"] = fb_id
                log.info(f"  ✓ FB Reel: {fb_id}")
                changed = True
            except Exception as e:
                log.warning(f"  ✗ FB Reel upload failed for {s['short_id']}: {e}")
    if changed:
        _write_json(SHORTS_QUEUE, queue)


def main() -> None:
    log.info("=== regen_worker tick ===")
    try:
        dispatch_telegram()
    except Exception as e:
        log.exception(f"dispatch_telegram error: {e}")
    try:
        upload_shorts()
    except Exception as e:
        log.exception(f"upload_shorts error: {e}")
    log.info("=== tick done ===\n")


if __name__ == "__main__":
    main()
