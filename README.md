# YouTube Auto Uploader (عربي)

أداة بتاخد فيديوهاتك من فولدر معين، وبتعملها كل ده تلقائياً:

- 🎙️ **تفريغ صوت** بـ Whisper محلي → SRT عربي
- 🤖 **توليد عنوان ووصف وفصول (chapters)** بـ Gemini AI (مجاني)
- 🖼️ **عمل Thumbnail** من أحسن frame في الفيديو + إضافة العنوان عليه
- ✂️ **قص أهم لحظات** من الفيديو وعمل **Shorts** عمودية (9:16) مع ترجمة
- ⬆️ **رفع للـ YouTube** كـ scheduled (فيديو في اليوم) مع كل البيانات

كل ده **مجاني تماماً** - مفيش API keys مدفوعة.

---

## 🚀 خطوات التشغيل (أول مرة)

### 1) ثبّت Python و ffmpeg

**Python 3.10+:**
- نزّله من https://www.python.org/downloads/
- وأنت بتنزله أهم حاجة: علّم على **"Add Python to PATH"** ✅

**ffmpeg:**
- نزّله من https://www.gyan.dev/ffmpeg/builds/ (اختار `ffmpeg-release-essentials.zip`)
- فك الضغط في `C:\ffmpeg`
- ضيف `C:\ffmpeg\bin` لـ PATH:
  - دوس Win + R → اكتب `sysdm.cpl`
  - Advanced → Environment Variables → Path → New → `C:\ffmpeg\bin`
- اقفل وافتح Terminal جديد، واكتب `ffmpeg -version` للتأكد

### 2) شغّل setup.bat

دوبل كليك على `setup.bat`. هيعمل:
- بيئة افتراضية (venv)
- تثبيت كل المكتبات المطلوبة
- ينسخ `config.example.json` لـ `config.json`

### 3) خد مفتاح Gemini API (مجاني)

- روح https://aistudio.google.com/apikey
- اعمل تسجيل دخول بحسابك جوجل
- اضغط **"Create API Key"** → انسخه
- افتح `config.json` بأي text editor
- حط المفتاح مكان `ضع_مفتاح_Gemini_هنا`

> **مهم:** الـ free tier فيه 15 طلب في الدقيقة و 1500 في اليوم. كافي جداً للأداة دي.

### 4) جهّز YouTube API

ده اللي بيخليك ترفع تلقائياً للقناة بتاعتك.

#### أ) اعمل مشروع في Google Cloud Console
- روح https://console.cloud.google.com/
- اعمل New Project → سميه "YouTube Uploader"

#### ب) فعّل YouTube Data API v3
- في الـ search bar فوق، اكتب "YouTube Data API v3" → اضغطه → **Enable**

#### ج) اعمل OAuth credentials
- روح **APIs & Services → OAuth consent screen**
  - اختار **External** → Create
  - حط أي اسم (مثلاً "YouTube Uploader") والإيميل بتاعك
  - اضغط Save
  - في **Test users**: ضيف الإيميل بتاع قناتك
- روح **APIs & Services → Credentials**
  - اضغط **Create Credentials → OAuth Client ID**
  - Application type: **Desktop app**
  - Name: أي اسم
  - **Create** → نزّل ملف JSON
  - سمّيه `client_secret.json` وحطه في فولدر `credentials/` جوه المشروع

> 💡 أول مرة هتشغل فيها الأداة، هتفتحلك المتصفح عشان توافق على الصلاحيات. بعد كده مش هتسأل تاني.

### 5) حط الفيديوهات وشغّل

- حط فيديوهاتك في فولدر `videos_input/`
- دوبل كليك على `run.bat` → هيعالج كل فيديو ويرفعه (فيديو لكل يوم)

أو ابقى مفتوح المراقبة دايماً (لما تحط فيديو جديد يرفعه تلقائي):
```
watch.bat
```

---

## 📋 الأوامر المتاحة

```bash
# معالجة كل الفيديوهات الموجودة (فيديو في اليوم)
python main.py run

# تجربة بدون رفع (يعمل thumbnail و metadata و shorts بس عشان تراجعهم)
python main.py run --dry-run

# رفع فيديو واحد محدد فوراً
python main.py upload "videos_input/myvideo.mp4"

# المراقبة المستمرة
python main.py watch
```

---

## ⚙️ الإعدادات المهمة في config.json

```json
"youtube": {
  "publish_hour_local": 18,        // الساعة اللي يتنشر فيها كل يوم
  "publish_minute_local": 0,
  "timezone": "Africa/Cairo",
  "default_privacy": "private",    // private حتى وقت النشر
  "schedule_publish": true         // نشر مجدول
}

"whisper": {
  "model_size": "medium"  // tiny=أسرع/أقل دقة، large-v3=أبطأ/أدق
}

"shorts": {
  "max_clips_per_video": 3,        // عدد الـ shorts من كل فيديو
  "burn_subtitles": true            // الترجمة محروقة في الـ short
}
```

---

## 📁 هيكل المخرجات

```
output/
├── srt/                # ملفات الترجمة .srt
├── thumbnails/         # صور الغلاف
├── metadata/           # JSON فيه العنوان والوصف والفصول
├── shorts/             # فولدر لكل فيديو فيه shorts بتاعته
└── uploaded/           # الفيديوهات اللي اترفعت بنجاح
```

---

## ❓ مشاكل شائعة

**❌ "ffmpeg مش متثبت"**
ارجع لخطوة 1 وتأكد إنه في PATH.

**❌ "فشل رفع الـ thumbnail"**
لازم القناة تكون موثّقة في يوتيوب (بفون). الفيديو هيترفع طبيعي بس صورة الغلاف مش هتتحدث.

**❌ "quota exceeded"**
يوتيوب مديك 10,000 quota في اليوم. كل فيديو يأخد ~1600. يعني تقدر ترفع 6 فيديوهات في اليوم بحد أقصى.

**❌ Whisper بطيء جداً**
- لو عندك GPU NVIDIA: ثبّت CUDA + `pip install torch --index-url https://download.pytorch.org/whl/cu121`
- لو لأ: غيّر `model_size` لـ `"small"` أو `"base"` بدل `"medium"`

**❌ النص العربي في الـ Thumbnail مكسور**
نزّل خط عربي زي [Cairo](https://fonts.google.com/specimen/Cairo) أو [Tajawal](https://fonts.google.com/specimen/Tajawal):
- ضع الملف في `fonts/Cairo-Bold.ttf`
- اتأكد إن المسار في `config.json` صح

---

## 🎯 جدولة فيديو في اليوم

الأداة تلقائياً بتعمل كده:
- لو عندك 7 فيديوهات في `videos_input/` وشغلت `run`
- هتتجدول واحد كل يوم في الوقت اللي حددته في config
- (الأول النهاردة، التاني بكرة، التالت بعد بكرة... الخ)

تقدر تستخدم Windows Task Scheduler لتشغيل `run.bat` تلقائي كل أسبوع مثلاً.

---

## 🛠️ هيكل المشروع

```
youtube-auto-uploader/
├── main.py                  # نقطة الدخول
├── setup.bat / run.bat      # سكريبتات تشغيل لويندوز
├── config.json              # إعداداتك
├── requirements.txt
├── credentials/
│   └── client_secret.json   # من Google Cloud
├── videos_input/            # ضع فيديوهاتك هنا
├── output/                  # المخرجات
├── src/
│   ├── transcribe.py        # Whisper
│   ├── thumbnail.py         # PIL + ffmpeg
│   ├── ai_generator.py      # Gemini
│   ├── shorts_cutter.py     # ffmpeg
│   ├── youtube_uploader.py  # YouTube API
│   ├── pipeline.py          # الـ orchestration
│   └── config.py
└── logs/
```

---

استمتع! ولو عندك مشكلة افتح `logs/uploader.log` وتلاقي تفاصيل أكتر.

---

## English Quick Reference

Multi-platform auto-uploader for Arabic religious video content. Takes raw video files, generates SRT subtitles via local Whisper, AI-generates titles/descriptions/chapters via Gemini, builds branded thumbnails, cuts vertical 9:16 Shorts/Reels, and publishes scheduled uploads to YouTube, Facebook, Instagram, and Telegram.

### Supported platforms
- YouTube (full videos + scheduled publish)
- Facebook (videos + Reels)
- Instagram (Reels)
- Telegram (videos + text quotes)

### Quick setup
1. `git clone https://github.com/Mostafa1712002/abo-hus-system.git`
2. Run `setup.bat` to create venv and install dependencies
3. Copy `config.example.json` to `config.json` and fill in API keys
4. Place credentials in `credentials/`:
   - `client_secret.json` (Google Cloud OAuth desktop client)
   - `meta_credentials.json` (Facebook/Instagram long-lived token)
   - `telegram_credentials.json` (Telegram bot token + chat IDs)
5. Drop videos into the configured `videos_input` path
6. Run `run.bat` (single batch) or `watch.bat` (continuous monitoring)

### Tech stack
- Python 3.10+
- faster-whisper (local STT)
- Google Gemini API (text generation)
- google-api-python-client (YouTube Data API v3)
- Meta Graph API (Facebook + Instagram)
- python-telegram-bot
- Pillow + ffmpeg (thumbnails + Shorts)
- Flask (dashboard)

See `docs/` for full architecture, integration guides, and the master plan.

