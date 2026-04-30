import logging

import requests
from src.facebook_uploader import load_meta_credentials
from src.log_setup import setup_logging

setup_logging("sync_ops.log")
log = logging.getLogger("check_ig")
log.info("=== check_ig starting ===")

creds = load_meta_credentials()
page = creds['pages'][0]
ig_id = page['instagram_business_account_id']
token = page['page_access_token']

print(f"IG Business ID: {ig_id}  username: {page.get('instagram_username')}")
print()
# Recent IG media
r = requests.get(
    f"https://graph.facebook.com/v21.0/{ig_id}/media",
    params={'access_token': token, 'fields': 'id,caption,media_type,media_product_type,permalink,timestamp', 'limit': 10},
    timeout=30,
)
data = r.json().get('data', [])
print(f"Recent IG media count: {len(data)}")
for m in data[:10]:
    cap = (m.get('caption') or '').replace('\n', ' ')[:80]
    print(f"  {m['id']}  |  {m.get('media_type', '?')}/{m.get('media_product_type', '?')}  |  {m.get('timestamp', '?')[:19]}")
    print(f"       {m.get('permalink', '?')}")
    print(f"       caption: {cap}")
    print()
