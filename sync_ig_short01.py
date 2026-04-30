"""Upload the re-cut short_01 to IG (the only one that failed earlier)."""
import json
import logging
from pathlib import Path
from src.facebook_uploader import load_meta_credentials
from src.instagram_uploader import upload_reel_to_instagram
from src.pending_tracker import PendingTracker
from src.log_setup import setup_logging

setup_logging("sync_ops.log")
log = logging.getLogger("sync_ig_short01")
log.info("=== sync_ig_short01 starting ===")

VIDEO_ID = "AHVXVhmkvpk"
SERIES = "شرح الرسالة"

creds = load_meta_credentials()
page = creds['pages'][0]

metadata = json.loads(Path("output/metadata/1.json").read_text(encoding="utf-8"))
clip = metadata["important_clips"][0]

caption = f"""{clip['description']}

⚡ من سلسلة "{SERIES}" — فضيلة الشيخ أبو حفص بن العربي الأثري

🌟 ترقبوا قريباً المزيد من دروس السلسلة بإذن الله

#الشيخ_سامي_العربي #أبو_حفص_الأثري #علم_شرعي #دروس_شرعية #إسلاميات #السلف_الصالح #تربية_الأبناء #shorts #reels"""[:2200]

short_path = Path("output/shorts/1/1_short_01.mp4")
print(f"Uploading {short_path.name} ({short_path.stat().st_size / 1024 / 1024:.1f} MB)...")
print(f"Title: {clip['suggested_short_title']}")

try:
    media_id = upload_reel_to_instagram(
        ig_business_account_id=page['instagram_business_account_id'],
        page_access_token=page['page_access_token'],
        video_path=short_path,
        caption=caption,
        share_to_feed=True,
    )
    print(f"✅ Posted: https://www.instagram.com/p/{media_id}")

    # Update tracker
    t = PendingTracker(Path("output/pending.json"))
    record = t.get(VIDEO_ID)
    if record:
        md = record.metadata or {}
        ig_ids = md.get("ig_short_media_ids", [])
        ig_ids = [media_id] + ig_ids  # prepend at index 0
        md["ig_short_media_ids"] = ig_ids
        t.update(VIDEO_ID, metadata=md)
        print(f"✓ tracker updated. IG IDs: {ig_ids}")
except Exception as e:
    print(f"❌ Failed: {e}")
