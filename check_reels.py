import logging
import requests, json
from src.facebook_uploader import load_meta_credentials
from src.log_setup import setup_logging

setup_logging("sync_ops.log")
log = logging.getLogger("check_reels")
log.info("=== check_reels starting ===")
creds = load_meta_credentials()
page = creds['pages'][0]
r = requests.get(
    f"https://graph.facebook.com/v21.0/{page['page_id']}/video_reels",
    params={'access_token': page['page_access_token'],
            'fields': 'id,description,created_time', 'limit': 10},
    timeout=30,
)
data = r.json().get('data', [])
print(f'Reels count: {len(data)}')
for reel in data[:10]:
    desc = (reel.get('description') or '')[:80].replace('\n', ' ')
    print(f"  {reel['id']}  |  {reel.get('created_time', '?')[:19]}  |  {desc}")
