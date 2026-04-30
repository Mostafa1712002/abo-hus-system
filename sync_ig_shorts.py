"""Upload the 3 missing shorts to Instagram as Reels."""
import json
import logging
import time
from pathlib import Path
from src.facebook_uploader import load_meta_credentials
from src.instagram_uploader import upload_reel_to_instagram
from src.pending_tracker import PendingTracker
from src.log_setup import setup_logging

setup_logging("sync_ops.log")
log = logging.getLogger("sync_ig_shorts")
log.info("=== sync_ig_shorts starting ===")

VIDEO_ID = "AHVXVhmkvpk"
SERIES = "شرح الرسالة"

# Load credentials
creds = load_meta_credentials()
page = creds['pages'][0]
ig_id = page['instagram_business_account_id']
token = page['page_access_token']

# Load metadata for captions
metadata = json.loads(Path("output/metadata/1.json").read_text(encoding="utf-8"))
clips = metadata.get("important_clips", [])

# IG captions (no YT link, brand hashtags)
IG_BRAND_HASHTAGS = [
    "#الشيخ_سامي_العربي", "#أبو_حفص_الأثري", "#علم_شرعي",
    "#دروس_شرعية", "#إسلاميات", "#السلف_الصالح", "#shorts", "#reels", "#فقه",
]

shorts_dir = Path("output/shorts/1")
short_files = sorted(shorts_dir.glob("1_short_*.mp4"))
print(f"Found {len(short_files)} short files\n")

ig_ids = []
for i, sp in enumerate(short_files):
    if i >= len(clips):
        clip_desc = ""
        clip_title = ""
    else:
        clip = clips[i]
        clip_desc = clip.get("description", "")
        clip_title = clip.get("suggested_short_title", "")

    caption = f"""{clip_desc}

⚡ من سلسلة "{SERIES}" — فضيلة الشيخ أبو حفص بن العربي الأثري

🌟 ترقبوا قريباً المزيد من دروس السلسلة بإذن الله

{' '.join(IG_BRAND_HASHTAGS)}"""[:2200]  # IG limit

    print(f"[{i+1}/{len(short_files)}] Uploading {sp.name}...")
    print(f"  Title: {clip_title[:60]}")
    try:
        media_id = upload_reel_to_instagram(
            ig_business_account_id=ig_id,
            page_access_token=token,
            video_path=sp,
            caption=caption,
            share_to_feed=True,
        )
        ig_ids.append(media_id)
        print(f"  ✅ Posted: media_id {media_id}")
    except Exception as e:
        print(f"  ❌ Failed: {e}")
        ig_ids.append(None)
    time.sleep(2)

# Update tracker
print(f"\nUpdating tracker with IG IDs: {ig_ids}")
t = PendingTracker(Path("output/pending.json"))
record = t.get(VIDEO_ID)
if record:
    metadata = record.metadata or {}
    metadata["ig_short_media_ids"] = [x for x in ig_ids if x]
    t.update(VIDEO_ID, metadata=metadata)
    print("✓ tracker updated")

print(f"\n✅ Done — {sum(1 for x in ig_ids if x)}/{len(short_files)} shorts on Instagram")
print("\nIG profile: https://www.instagram.com/abohafs.elaraby")
