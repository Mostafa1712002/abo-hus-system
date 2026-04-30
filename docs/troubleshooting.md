# Troubleshooting

سجل المشاكل اللي حصلت أثناء التطوير وحلولها.

## 1. OAuth: `Access blocked — has not completed Google verification`

**السبب:** Sensitive scopes (zوي `youtube.upload`) محتاجة Google Verification في وضع Production. التطبيق Published لكن غير Verified.

**الحل:** ارجع التطبيق لـ **Testing mode**، وأضف الإيميل في **Test users**. وقت OAuth flow هتظهر شاشة "unverified app" — اضغط Advanced → Go to <app> (unsafe).

📍 https://console.cloud.google.com/apis/credentials/consent?project=mindful-acre-494908-q4

## 2. `ZoneInfoNotFoundError: 'Africa/Cairo'`

**السبب:** Python على Windows ما عندوش timezone data افتراضياً.

**الحل:** `pip install tzdata`. مضافة لـ `requirements.txt`.

## 3. `API_KEY_INVALID` على كل مفاتيح Gemini

**السبب:** مفاتيح Gemini القديمة على السيرفر منتهية/مُلغاة.

**الحل:** أنشئ مفاتيح جديدة من https://aistudio.google.com/apikey وحطها في `gemini_api_keys_rotation` array. الـ rotation logic بيدوّر تلقائياً على الـ triggers: `quota`, `rate`, `429`, `api_key_invalid`, `401`, `403`, `expired`.

## 4. `model gemini-2.0-flash-exp not found`

**السبب:** اسم الموديل القديم انتهى عمره الافتراضي.

**الحل:** الافتراضي دلوقتي `gemini-2.5-flash` (في `src/ai_generator.py` constructor).

## 5. `JSONDecodeError: Expecting ',' delimiter`

**السبب:** Gemini رجّع JSON معطوب أو مقطوع (max_output_tokens صغير).

**الحل:**
- رفع `max_output_tokens` من 4096 إلى 8192
- إضافة `json-repair` كـ fallback (متضافة لـ requirements.txt)
- لو فشل الإصلاح، الـ raw response بيتحفظ في `logs/gemini_bad_response.txt`

## 6. `UnicodeDecodeError: 'charmap' codec can't decode byte 0x81`

**السبب:** Python على Windows بيقرأ stdout/stderr الـ subprocess بـ cp1252 الافتراضي. ffmpeg بيُخرج رسائل بـ utf-8.

**الحل:** في `src/shorts_cutter.py`:
```python
subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
```

## 7. `playlist_id` فاضي رغم إضافة الكود

**السبب:** Phase 1 ركّض قبل ما الـ playlist code يتضاف. Phase 2 لم يكن بيعمل playlist linkage.

**الحل:** في `_process_one` (Phase 2) أضفنا:
```python
if series_key and not (item.playlist_id or "").strip():
    pid = get_or_create_playlist(youtube, series_key, pl_desc)
    if pid:
        add_video_to_playlist(youtube, item.video_id, pid)
        tracker.update(item.video_id, playlist_id=pid)
```

## 8. الـ tags بأندرسكور (`الإمام_الشافعي`)

**السبب:** Gemini كان بيرجع hashtags بصيغة underscore-joined (مناسبة لـ #hashtag في description) لكن نفسها بتترفع كـ YouTube tags.

**الحل:** `normalize_tags()` في `src/pipeline.py` بيستبدل `_` و `-` بـ space. الـ Gemini prompt اتحدّث برضو ليفصل بين hashtags (للـ description) و tags (مفصولة بـ spaces).

## 9. Two parallel processing runs على نفس الفيديو

**السبب:** المهمة المُجدوَلة كل 15 دقيقة + retry يدوي = duplicate uploads.

**الحل:** قبل أي retry يدوي، أوقف المهمة المُجدوَلة بـ:
```powershell
schtasks /Delete /TN "YouTubeProcessCaptions" /F
```
أو راجع `pending.json` للتأكد إن `status != processing` قبل التشغيل.

## 10. اسم القناة الخطأ ("نقال الخير" بدلاً من "الشيخ سامي العربي")

**السبب:** Hardcoded في الـ Gemini prompt و `pl_desc` و config.

**الحل:** كل الـ branding دلوقتي بيتسحب من `config.json → channel_branding.channel_name`. أي تغيير في اسم القناة مستقبلاً = تعديل في مكان واحد.
