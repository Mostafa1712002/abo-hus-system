# Reliability — حماية الـ pipeline من المشاكل اللي حصلت

ملخص الإجراءات اللي اتعملت بعد حادثة العبودية 5 (الفيديو اتنشر public بعنوان `[قيد المعالجة]` لأن Gemini quota خلصت).

## 1. Queue-safe publish (commit `74351cf`)

**قبل**: الـ pipeline كان يرفع الفيديو على YouTube بـ `publishAt` مجدول مسبقاً. لو الـ Gemini فشل بعدها → الفيديو ينشر بعنوان `[قيد المعالجة]`.

**بعد**:
- الفيديو يترفع بـ `private` بدون `publishAt`
- الـ `publish_at` المقصود يتسجل في الـ DB بس
- بعد ما كل الـ steps تنجح (Gemini + shorts + FB + IG + TG) → الـ pipeline يطبق `publishAt` على YouTube
- لو الموعد الأصلي فات (مثلاً Gemini رجع بعد 24 ساعة) → بنزحزحه لقدام يوم/يومين تلقائياً

## 2. Quota retry (في نفس الـ commit)

**قبل**: أي خطأ في الـ Gemini → `status=failed` نهائياً، محتاج تدخل يدوي.

**بعد**:
- لو الخطأ feature بـ rate-limit / quota → `status=uploaded` يفضل (نفس الـ status اللي بيخلي الـ cron يعيد المحاولة)
- الـ cron كل 15 دقيقة بيحاول تاني → مع جدولة Gemini الـ daily reset، بيرجع شغّال أوتوماتيك

## 3. FB resumable upload (commit جديد)

**قبل**: الفيديو الأكبر من ~100MB يرجع 413 من FB API.

**بعد**:
- الـ uploader بيشيك الحجم تلقائياً
- > 95MB → 3-phase resumable upload (start → transfer chunks → finish)
- Tested على فيديو 220MB العبودية 5 → نجح في 1:30 دقيقة

## 4. Telegram alerts (commit جديد)

**قبل**: لازم تشيك اللوج عشان تعرف لو حصل فشل.

**بعد**:
- `src/alerts.py` يرسل رسالة على Telegram لما يحصل:
  - **Quota error** (warning): "Gemini quota خلصت — هيعيد المحاولة"
  - **Other error** (error): "فشل في الـ pipeline — يحتاج تدخل يدوي"
- بـ HTML formatting + رابط الفيديو + الـ error message
- No-op لو `admin_chat_id` مش مضبوط في الـ config (مفيش spam في حالة عدم الإعداد)

### إعداد الـ admin_chat_id

1. ابدأ private chat مع البوت `@<bot_username>` (ابعتله أي رسالة)
2. افتح: `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`
3. شوف `result[0].message.chat.id` (رقم زي `123456789`)
4. ضيفه في `config.json`:
   ```json
   "telegram": {
     "admin_chat_id": "123456789"
   }
   ```
5. خلاص — أي failure هيوصلك على Telegram

## 5. Gemini Paid Tier (recommended next step — لسه محتاج credit card)

الـ free tier محدود جداً:
- 5 طلبات / دقيقة
- 250 طلب / يوم
- Upgrade لـ paid: 1000 RPM + لا حد يومي، تكلفة < $5/شهر

### خطوات الترقية

1. روح لـ https://aistudio.google.com/apikey
2. اضغط على **"Set up Billing"** (في الـ project المرتبط بالـ key)
3. هيحولك لـ Google Cloud Console — ضيف credit card
4. ارجع لـ aistudio + اضغط على الـ project → الـ tier هيظهر "Paid Tier 1"
5. الـ key الموجود سيشتغل تلقائياً (مش محتاج تعمل key جديد)
6. ضيف الـ key الـ paid في `config.json` تحت `gemini_api_keys_rotation` كأول key

**ملحوظة**: الـ free keys هتفضل في الـ rotation كـ fallback، فلو عاوز تشيلهم متشيلش — خليهم.

## ملخص الحالة بعد الإجراءات

| السيناريو | قبل | بعد |
|---|---|---|
| Gemini quota خلصت | ينشر `[قيد المعالجة]` public | يفضل private + retry تلقائي + Telegram alert |
| FB main video > 100MB | 413 — fail | resumable upload — نجاح |
| فشل في الـ pipeline | لازم تشيك اللوج يدوياً | Telegram alert فوراً |
| Gemini RPM hit | فشل عدد كبير من shorts | retry تلقائي |
| Quota daily exhausted | كل اليوم عاطل | بيرجع تلقائياً عند الـ reset |
