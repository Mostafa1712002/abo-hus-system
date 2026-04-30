# تكامل Telegram Channel

## الهدف

نشر نفس المحتوى اللي بيتنشر على YouTube + Facebook + Instagram على قناة Telegram (`@abohafs_elaraby`) تلقائياً، كجزء من الـ pipeline (Phase 2).

## استراتيجية المحتوى على تليجرام

| نوع | المصدر | الحد |
|---|---|---|
| Shorts | clip path local | 50MB / video |
| Text Quotes | Gemini output | 3-5 / lecture |
| Full Video | (skipped — >50MB) | استبدله بـ post نصي + YT link |

ترتيب النشر لكل محاضرة على القناة:

1. **منشور إعلان المحاضرة** (نصي + رابط يوتيوب + هاشتاجز) — أول حاجة بعد ما YouTube يجهز.
2. **الـ Shorts** (3 فيديوهات قصيرة بالـ caption بتاعتها).
3. **الاقتباسات النصية** (3-5 منشورات نصية متباعدة بـ `delay_between_posts_seconds`).

## نطاق النشر

| المحتوى | YouTube | Facebook | Instagram | Telegram |
|---|---|---|---|---|
| **الفيديو الكامل** (~90 دقيقة) | Video | Video Post | — | **Text post (رابط YouTube)** — لا يُرفع الملف |
| **Shorts** (30-60 ثانية، 9:16) | Short | Reel | Reel | Video post |
| **Text Quotes** (1-3 جمل من المحاضرة) | — | — | — | **Standalone text post (3-5/lecture)** |

السبب في إن الفيديو الكامل بيتبعت كنص لا فيديو: حد الـ Telegram Bot API الرسمي 50MB لكل ملف، والمحاضرات عادة 100s of MB. المنشور النصي بيشارك العنوان + الوصف المختصر + رابط YouTube + الهاشتاجز (الـ link preview بيشتغل تلقائياً).

## محدوديات Telegram Bot API

| الـ endpoint | الحد |
|---|---|
| `sendVideo` (multipart) | **50 MB** لكل ملف |
| `sendDocument` | 50 MB كذلك (نفس القيد) |
| `sendMessage` (text) | **4096** حرف |
| `caption` (تحت الفيديو/الصورة) | **1024** حرف |
| `sendVideoNote` (round) | 50 MB، max 60s، لازم مربع |

> الـ self-hosted Bot API server (telegram-bot-api) بيرفع الحد لـ 2GB. شوف `send_video_via_local_bot_api` في `telegram_uploader.py`.

## البنية في الكود

### `src/telegram_uploader.py`

موديول lean بيستخدم `requests` على Bot API مباشرة (مفيش dependency جديدة).

| الدالة | الوصف |
|---|---|
| `load_telegram_credentials(path)` | يحمّل `credentials/telegram_credentials.json`. يتحقق من `validation_status: ok`. يرفع `FileNotFoundError` لو مش موجود. |
| `send_message(bot_token, chat_id, text, ...)` | يبعت رسالة نصية. يقص لـ 4096 حرف لو أطول. يرجع `message_id`. |
| `send_video(bot_token, chat_id, video_path, caption, ...)` | يرفع ملف فيديو multipart. لو `>50MB` يرفع `RuntimeError` واضح. caption يتقص لـ 1024. يرجع `message_id`. |
| `send_video_note(...)` | فيديو مدور (60s، مربع). مش مستخدم في الـ pipeline. |
| `get_channel_info(bot_token, chat_id)` | `getChat` — diagnostic. يرجع dict فيه title/id/type/member_count. |
| `send_video_via_local_bot_api(...)` | اختياري — يوجّه الطلب لـ self-hosted Bot API server (`http://localhost:8081` افتراضياً) لتجاوز حد 50MB. |

### `src/pipeline.py` — hooks

التكامل في 4 نقاط:

1. **`_TELEGRAM_HASHTAGS`** — pool هاشتاجز عربية (الـ underscores conventional في Telegram).
2. **`_build_telegram_caption(base_text, parent_url, is_short)`** — يبني caption بـ Telegram HTML (`<b>...</b>`) محدود بـ 1024 حرف.
3. **`_build_telegram_text_post(...)`** — يبني نص أطول (≤4096) للـ fallback لما الفيديو الكامل > 50MB.
4. **`_get_telegram_creds(cfg)`** — caching + graceful failure (لو الـ creds فشلت، Telegram بيتعطل بدون ما يكسر الـ pipeline).
5. **`_publish_full_video_to_telegram(cfg, video_path, title, description, yt_url, series)`** — يستخدم في `_process_one`. **لا يحاول رفع الملف**؛ يبعت منشور نصي فيه: header bold (`📖 محاضرة جديدة من سلسلة {series}`) + عنوان الفيديو + ملخص الوصف + رابط يوتيوب + هاشتاجز القناة. يرجع `message_id` أو None.
6. **`_publish_short_to_telegram(cfg, short_path, caption)`** — يستخدم في `_upload_shorts_for_video`. الـ shorts عادة 10-30MB فبتمر بدون مشاكل. يرجع `message_id` أو None.
7. **`_publish_quote_to_telegram(cfg, quote, series, parent_yt_url, speaker)`** — يستخدم في `_process_one` بعد رفع الـ shorts. يبعت اقتباس نصي مستقل على القناة. يرجع `message_id` أو None.

### Hook points داخل `_process_one`

```python
# بعد _publish_full_video_to_fb(...)
tg_main_message_id = _publish_full_video_to_telegram(
    cfg=cfg, video_path=Path(item.original_path),
    title=md.title, description=full_description, yt_url=yt_url,
)
```

### Hook points داخل `_upload_shorts_for_video`

```python
# بعد _publish_short_to_ig_reel(...)
tg_caption = _build_telegram_caption(base_text=clip_desc, parent_url=parent_url, is_short=True)
tg_msg_id = _publish_short_to_telegram(cfg=cfg, short_path=short_path, caption=tg_caption)
```

الـ message IDs بتتسجل في `tracker.metadata`:
- `tg_main_message_id: int` — للفيديو الكامل (أو الـ fallback النصي)
- `tg_short_message_ids: list[int]` — كل short

## Configuration

`config.json` فيه قسم `telegram`:

```json
"telegram": {
  "enabled": true,
  "publish_full_video": true,
  "publish_shorts": true,
  "credentials_file": "credentials/telegram_credentials.json",
  "send_text_fallback_for_large_videos": true,
  "max_video_size_mb": 50
}
```

| المفتاح | المعنى |
|---|---|
| `enabled` | لو `false`، Telegram بيتعطل تماماً (مفيش رفع، مفيش fallback). |
| `publish_full_video` | لو `false`، الفيديو الكامل ما بيتبعتش (الـ shorts بس). |
| `publish_shorts` | لو `false`، الـ shorts ما بتتبعتش. |
| `credentials_file` | المسار النسبي لملف الـ credentials. |
| `send_text_fallback_for_large_videos` | لو الفيديو > `max_video_size_mb` و ده `true`، بيبعت نص بدل الفيديو (مع رابط YouTube). لو `false`، بيتخطى. |
| `max_video_size_mb` | حد الحجم بالـ MB. الافتراضي 50 (حد الـ Bot API الرسمي). لو عندك local Bot API server حطه أعلى (مثلاً 1500). |

## التعطيل

ل تعطيل Telegram تماماً: `"enabled": false` في `config.json`.

ل تعطيل بس الفيديو الكامل (والإبقاء على الـ shorts): `"publish_full_video": false`.

ل تعطيل بس الـ shorts: `"publish_shorts": false`.

## التحويل لقناة تانية

البوت لازم يكون admin في القناة الجديدة وعنده صلاحية `Post messages`.

ال steps:
1. أضف البوت (`@samialarabi_publisher_bot`) كـ admin في القناة الجديدة.
2. خد الـ `chat_id` العددي للقناة (negative ID مثل `-1001234567890`). أسهل طريقة: ابعت رسالة في القناة وزور `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. حدّث `credentials/telegram_credentials.json`:
   ```json
   {
     "bot_token": "...",
     "channel_username": "@new_channel",
     "channel_numeric_id": -1009999999999,
     "validation_status": "ok"
   }
   ```
4. شغّل الـ diagnostic:
   ```bash
   python -c "from src.telegram_uploader import load_telegram_credentials, get_channel_info; c=load_telegram_credentials(); print(get_channel_info(c['bot_token'], c['channel_numeric_id']))"
   ```

ما فيش حاجة تتغير في الـ pipeline أو الـ config — بس الـ credentials.

## استكشاف الأعطال

| الخطأ | السبب المحتمل | الحل |
|---|---|---|
| `Forbidden: bot is not a member` | البوت مش admin في القناة | أضف البوت كـ admin |
| `Bad Request: chat not found` | الـ `chat_id` غلط | تأكد من الـ `channel_numeric_id` (لازم negative ويبدأ بـ `-100`) |
| `Request Entity Too Large` (413) | الفيديو > 50MB والـ fallback معطل | فعّل `send_text_fallback_for_large_videos`، أو شغّل local Bot API server |
| `Bad Request: VIDEO_TOO_BIG` (للـ video notes) | فيديو > 50MB أو > 60s أو مش مربع | استخدم `send_video` العادي |
| Caption يطلع مقصوص بـ `...` | Caption أطول من 1024 حرف | متوقع — `_build_telegram_caption` بيقص تلقائياً |

## ملاحظات تقنية

- الـ HTML parse mode بنستخدمه (مش MarkdownV2) عشان أبسط في الـ escaping (`<` `>` `&` بس).
- `_tg_escape()` بيهرب الأحرف دي. الباقي حرفي.
- `disable_web_page_preview=True` افتراضياً للرسايل النصية (بعدا في الـ fallback اللي بيخلي رابط YouTube يبان كبصمة).
- الـ uploads بـ timeout 600s (10 دقايق). الفيديوهات الـ shorts ≤30MB بترفع في ثواني.
- مفيش retry logic لـ Telegram (بخلاف Meta) — لو فشل بيتسجل warning بس وبيكمل. الـ pipeline ما بيقفش.
