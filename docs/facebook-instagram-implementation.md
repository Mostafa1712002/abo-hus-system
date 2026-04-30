# Facebook + Instagram — Implementation Reference

هذا المستند يصف التنفيذ الفعلي (الكود المكتوب) لتكامل النشر التلقائي على
Facebook Page و Instagram Reels داخل الـ pipeline. لو محتاج خلفية
عن الإعداد من جهة Meta نفسها (إنشاء App، توليد الـ token، الصلاحيات)،
راجع: `docs/facebook-instagram-integration.md`.

---

## 1. الموديولات الجديدة

### `src/facebook_uploader.py`

موديول مسؤول عن كل عمليات Facebook Page عبر Graph API v21.0.

```python
GRAPH_VERSION = "v21.0"
GRAPH_BASE = "https://graph.facebook.com/v21.0"
GRAPH_VIDEO_BASE = "https://graph-video.facebook.com/v21.0"

FB_DESC_MAX = 5000
UPLOAD_TIMEOUT = 600  # 10 min
```

#### `load_meta_credentials(credentials_path="credentials/meta_credentials.json") -> dict`

يقرأ ملف الـ credentials. لو الـ path نسبي بيدور أولاً في الـ cwd ثم
يـ fallback للمجلد الأب لـ `src/`. بيرفع `FileNotFoundError` لو الملف
مش موجود. يرجع الـ dict كما هو (app_id, app_secret, pages list, ...).

#### `upload_video_to_page(page_id, page_access_token, video_path, title, description, published=True, scheduled_publish_time=None) -> str`

يرفع فيديو long-form (16:9) لـ Facebook Page باستخدام multipart upload
عبر `POST /{page-id}/videos` على `graph-video.facebook.com`.

- بيقص الـ title لـ 255 حرف، الـ description لـ 5000 حرف.
- لو `scheduled_publish_time` محدد (Unix ts بالثواني) بيتعمل
  `published=false` تلقائياً ويُمرَّر `scheduled_publish_time`.
- بيستخدم `requests.post(..., files={"source": (...)} , timeout=600)`.
- يرجع `response.json()["id"]` (الـ FB video_id).

#### `upload_reel_to_page(page_id, page_access_token, video_path, description) -> str`

ينشر Reel (9:16) عبر **3-step Reels API**:

1. **start** — `POST /{page-id}/video_reels?upload_phase=start&access_token=...`
   - يرجع `video_id` و `upload_url`.
2. **upload** — `POST <upload_url>` ببنية binary مع headers:
   - `Authorization: OAuth <page_access_token>`
   - `offset: 0`
   - `file_size: <bytes>`
3. **finish** — `POST /{page-id}/video_reels?upload_phase=finish&video_id=<id>&video_state=PUBLISHED&description=<text>&access_token=...`

كل مرحلة بيتشيك على الـ status code وبيرفع exception لو فشلت.

#### `get_post_status(video_id, page_access_token) -> dict`

helper للـ debug/polling. بيقرأ
`GET /{video-id}?fields=id,status,published,permalink_url`.
لا يرفع exception (يرجع `{"error": ...}` لو فشل) لأنه استخدامه
debugging-only.

---

### `src/instagram_uploader.py`

ينشر Reels على Instagram Business Account المربوط بنفس الـ Page.
بيستخدم نفس `page_access_token`.

```python
IG_CAPTION_MAX = 2200
IG_REEL_MAX_BYTES = 100 * 1024 * 1024
POLL_TIMEOUT_SECONDS = 300  # 5 min
POLL_INTERVAL = 5
```

#### `upload_reel_to_instagram(ig_business_account_id, page_access_token, video_path, caption, cover_url=None, share_to_feed=True) -> str`

يرفع Reel على IG في **4 خطوات**:

1. **Create container (resumable)** —
   `POST /{ig-id}/media?media_type=REELS&upload_type=resumable&caption=...&share_to_feed=...&access_token=...`
   بيرجع `{"id": container_id, "uri": upload_uri}`.
2. **Upload binary** — `POST <upload_uri>` مع headers:
   - `Authorization: OAuth <page_token>`
   - `offset: 0`
   - `file_size: <bytes>`
   ملاحظة: ده بيستخدم نفس آلية الـ Facebook resumable upload عشان
   IG Reels محتاجة فيديو متخزن على Meta CDN قبل النشر — بدون الحاجة
   لـ S3/Cloudflare خارجي.
3. **Poll status** — `GET /{container_id}?fields=status_code,status&access_token=...`
   - `IN_PROGRESS` → استنى 5 ثواني وحاول تاني.
   - `FINISHED` → كمل للخطوة 4.
   - `ERROR` → ارفع `RuntimeError` بالرسالة.
   - بعد 5 دقايق بدون FINISHED → `TimeoutError`.
4. **Publish** — `POST /{ig-id}/media_publish?creation_id=<container_id>&access_token=...`
   بيرجع `{"id": media_id}`.

الـ caption بيتقص لـ 2199 حرف + `…` لو طوّل.

#### `check_publishing_limit(ig_business_account_id, page_access_token) -> dict`

`GET /{ig-id}/content_publishing_limit?fields=quota_usage,config`.
read-only، مفيد لمعرفة الكوتا اليومية (50 منشور/24 ساعة افتراضي،
سياستنا الفعلية: 100 يومياً حسب الـ config).

---

## 2. تكامل Pipeline

كل التعديلات في `src/pipeline.py`. تم إضافة:

### Helpers

- `_get_meta_creds(cfg)` — يحمّل meta_credentials.json مرة واحدة
  (cache في الـ module). بيرجع `None` لو الـ file مش موجود أو فاضي
  بدل ما يـ raise، عشان الفشل في تحميل الـ credentials لا يكسر
  الـ YouTube flow.
- `_build_reel_caption(base_text, parent_url)` — يبني caption لـ
  FB/IG Reel: نص الـ clip + `المحاضرة كاملةً: <yt-url>` + سطر
  hashtags ثابت (`#Reels #Shorts #إسلاميات #علم_شرعي ...`).
- `_publish_full_video_to_fb(cfg, video_path, title, description)` —
  wrapper بـ try/except حول `upload_video_to_page` بيتشيك على
  `cfg.facebook.enabled` و `publish_full_video`. بيرجع `fb_video_id`
  أو `None`.
- `_publish_short_to_fb_reel(cfg, short_path, caption)` — wrapper بـ
  try/except حول `upload_reel_to_page` بيتشيك على
  `cfg.facebook.enabled` و `publish_shorts_as_reels`.
- `_publish_short_to_ig_reel(cfg, short_path, caption)` — wrapper بـ
  try/except حول `upload_reel_to_instagram` بيتشيك على
  `cfg.instagram.enabled` و `publish_shorts_as_reels`.

### `_process_one`

بعد `update_video_metadata` و `set_thumbnail` للفيديو الكامل على
YouTube، بنرفع نفس الفيديو الكامل على Facebook Page (مش IG لأنه طويل):

```python
fb_main_video_id: str | None = None
if Path(item.original_path).exists():
    fb_main_video_id = _publish_full_video_to_fb(
        cfg=cfg,
        video_path=Path(item.original_path),
        title=md.title,
        description=full_description,
    )
```

ثم بعد قص الـ shorts، بنستدعي `_upload_shorts_for_video` (اللي بيـرفع
على YT + FB Reel + IG Reel). كل الـ IDs بترجع في tuple وبتتسجل في
`metadata_summary` تحت المفاتيح:

- `fb_main_video_id` (str)
- `fb_short_video_ids` (list[str])
- `ig_short_media_ids` (list[str])
- (بالإضافة لـ `short_video_ids` الموجود مسبقاً)

### `_upload_shorts_for_video`

بقى يرجع `tuple[list[str], list[str], list[str]]` (YT, FB, IG).
لكل short بعد رفعه على YouTube بنجاح:

1. نبني `reel_caption` من `clip.description` + `parent_url` + hashtags.
2. نستدعي `_publish_short_to_fb_reel` ونضيف الـ ID لـ `fb_uploaded`.
3. نستدعي `_publish_short_to_ig_reel` ونضيف الـ ID لـ `ig_uploaded`.

كل استدعاء معزول بـ try/except داخل الـ helper نفسه — فشل FB لا يمنع
IG، وفشل أي منهما لا يمنع YouTube.

---

## 3. إستراتيجية الـ Error Handling

| المستوى | السلوك عند الفشل |
|---|---|
| `load_meta_credentials` فاشل / الملف مش موجود | يـ logger.warning ويُرجع `None`، YouTube flow يكمل عادي. |
| `cfg.facebook.enabled = false` | الـ helper يرجع `None` فوراً بدون أي API call. |
| `cfg.instagram.enabled = false` | نفس الشيء. |
| فشل رفع الفيديو الكامل على FB | warning + كمل، YouTube + Shorts مش متأثرين. |
| فشل رفع short على FB Reel | warning، نكمل ونحاول IG. |
| فشل رفع short على IG Reel | warning، short لسه على YouTube + FB. |
| فشل أي short كله (حتى YouTube) | الـ short ده يتخطى، الباقيين يكملوا. |

كل فشل بيتسجل في `metadata_summary` ضمنياً (الـ ID اللي فشل مش هيظهر
في الـ list). `pending_tracker` بيخزن الـ summary تحت المفتاح
`metadata`، فلو محتاجين retry لاحقاً ممكن نقارن `len(short_video_ids)`
بـ `len(fb_short_video_ids)`.

---

## 4. حدود ومحدوديات

| البند | YouTube | Facebook | Instagram |
|---|---|---|---|
| Aspect ratio (Reels) | 9:16 | 9:16 | 9:16 |
| مدة الـ Reel | ≤ 60s (Shorts) | ≤ 90s | ≤ 90s |
| حجم الـ Reel | لا يوجد عملي | لا يوجد عملي | ≤ 100 MB |
| Caption / Description | 5000 حرف | 5000 (بنقص لـ 5000) | 2200 (بنقص لـ 2199 + …) |
| Hashtags في الـ caption | ضمن الوصف | ضمن الوصف | حتى 30 hashtag |
| كوتا يومية للنشر | حسب YouTube | لا يوجد limit عملي | 100 منشور/24 ساعة |
| Token expiry | OAuth refresh تلقائي | Long-Lived Page Token (لا تنتهي) | نفس الـ Page Token |

---

## 5. كيفية تعطيل FB أو IG مستقلين

في `config.json`:

```json
"facebook": {
  "enabled": true,
  "publish_full_video": true,
  "publish_shorts_as_reels": true,
  "credentials_file": "credentials/meta_credentials.json"
},
"instagram": {
  "enabled": true,
  "publish_shorts_as_reels": true,
  "share_to_feed": true
}
```

سيناريوهات:

- **تعطيل Facebook كلياً**: `facebook.enabled = false`.
- **تعطيل IG كلياً**: `instagram.enabled = false`.
- **نشر الـ shorts فقط على FB دون الفيديو الكامل**:
  `facebook.publish_full_video = false`، `publish_shorts_as_reels = true`.
- **نشر الفيديو الكامل فقط على FB دون الـ Reels**:
  `facebook.publish_shorts_as_reels = false`.
- **نشر بدون مشاركة الـ Reel على IG feed** (بس على Reels tab):
  `instagram.share_to_feed = false`.

التعطيل لا يحتاج إعادة تشغيل خاصة — الـ flag بيتقرأ في كل
استدعاء داخل `_process_one`.

---

## 6. التحقق من التركيب (Verification)

تم تنفيذ الأوامر التالية بنجاح:

```bash
# 1. الـ imports
python -c "from src.facebook_uploader import upload_video_to_page, upload_reel_to_page, load_meta_credentials; \
           from src.instagram_uploader import upload_reel_to_instagram, check_publishing_limit; \
           from src.pipeline import _process_one, _upload_shorts_for_video; \
           print('imports OK')"
# → imports OK

# 2. تحميل الـ credentials
# → app_id: 1604484570638323
# → page: الشيخ سامي العربي
# → ig: abohafs.elaraby

# 3. quota check (live API)
# → {'data': [{'quota_usage': 0, 'config': {'quota_total': 100, 'quota_duration': 86400}}]}
```

quota الـ IG = 100 منشور/24 ساعة (مش 50 الافتراضي — Meta ضاعفته
للـ business accounts). الكوتا الحالية 0.

---

## 7. خطوات لاحقة (لم تُنفّذ — للاختبار اليدوي)

- [ ] تشغيل end-to-end على فيديو فعلي ومراجعة أن:
  - الفيديو الكامل ظهر على FB Page بالـ thumbnail الصحيح.
  - الـ shorts ظهرت كـ Reels على FB بترتيب صحيح.
  - الـ shorts ظهرت كـ Reels على IG بـ caption عربي مضبوط.
- [ ] التحقق من أن الـ `parent_url` (يوتيوب) قابل للنقر في caption الـ IG
      (عادةً IG لا يعمل للروابط في الـ caption hyperlink، لكنها تظهر كنص).
- [ ] التحقق من رفع فيديو طوله ~90 دقيقة (~500MB-1GB) لا يتجاوز الـ
      `UPLOAD_TIMEOUT=600s`؛ لو حصل، نزود الـ timeout أو نتحول لـ
      chunked upload لـ FB Page videos.
