# نظام إعادة رفع كتالوج abuhafs.info — التوثيق الكامل

**آخر تحديث:** 2026-06-19
**الهدف:** إعادة رفع ~2634 فيديو من قنوات الشيخ الأخرى (المفهرسة في موقع abuhafs.info) على قناتنا (الشيخ سامي العربي) بعد المعالجة الكاملة، سلسلة-بسلسلة، بشكل تلقائي.

---

## 1. نظرة عامة على المعمارية

```
  موقع abuhafs.info (كتالوج 2634 فيديو على قنوات أخرى + DB)
        │  (1) مزامنة الكتالوج من MySQL → جدول tracking
        ▼
  جدول catalog_videos في data/abuhafs.db (على الـ VPS)
        │  (2) download worker — loader.to (يتخطّى حجب YouTube للداتا سنتر)
        ▼
  /opt/abuhafs/catalog_workspace/<series_slug>/<yt_id>.mp4
        │  (3) full worker — المعالجة الكاملة
        ▼
  YouTube (main+intro+thumbnail+playlist) + 3 shorts (YT+FB+IG+TG)
  + Facebook main + Telegram (إعلان + اقتباسات)
        │  (4) cleanup — مسح الـ mp4 بعد الرفع
        ▼
  تحديث catalog_videos: process_status='uploaded', download_status='archived'
```

**كل العمل على الـ VPS:** `ssh root@7erfa-system.com` — `/opt/abuhafs/youtube-auto-uploader/`

---

## 2. البنية التحتية والوصول

| المورد | التفاصيل |
|---|---|
| **VPS (المعالجة)** | `ssh root@7erfa-system.com` — `/opt/abuhafs/youtube-auto-uploader/` — venv في `venv/` — 150GB قرص |
| **سيرفر الكتالوج (tarweig)** | `ssh -p 22422 abuhafsi@213.239.209.167` — **البورت 22422 مش 22** — cPanel، 1.1TB |
| **MySQL الكتالوج** | على tarweig: `mysql -u abuhafsi_root -p<password: see server config> abuhafsi_root` — جدول `videos` (2836 صف، فيه `video_id`=YouTube ID + `subtitle_srt` + `description_ar`) + `categories` (99 سلسلة فيديو) |
| **whisper service** | `https://whisper.markoum.com` (Vultr 209.250.235.9) — يحمّل من YouTube ويفرّغ (IP مش محجوب) — لم نعد نعتمد عليه للتفريغ لأن الـ SRT موجود في DB |
| **خدمة التحميل** | `loader.to` — `ajax/download.php?format=720&url=...` → poll `ajax/progress.php?id=...` → mp4 — تتخطّى حجب YouTube للداتا سنتر، مجانية |
| **Dashboard** | `https://abuhafs.7erfa-system.com/catalog` (محمي بـ login، auto-refresh 15ث، تقدّم كل سلسلة) |

> **مهم:** الـ VPS وسيرفر tarweig **الاتنين محجوبين** من تحميل YouTube مباشرة (datacenter IP → "Sign in to confirm you're not a bot"). لذلك نستخدم `loader.to` للتحميل.

---

## 3. جدول الـ tracking: `catalog_videos`

في `data/abuhafs.db` (sqlite). أهم الأعمدة:
- `youtube_id` (المصدر الأصلي), `youtube_url`, `title`, `series`, `series_slug`, `ord`, `duration`, `thumb`
- `download_status`: `pending` → `downloading` → `downloaded` → `archived` (بعد الرفع والمسح) | `failed` | `skipped`
- `mp4_path`, `download_bytes`, `download_quality`, `downloaded_at`, `download_error`
- `process_status`: `pending` → `uploaded` | `failed` | `skipped`
- `reupload_yt_id`: الـ YouTube ID الجديد بعد إعادة الرفع

**الاستيراد:** `scripts/import_catalog.py` (من JSON-lines مُصدّر من tarweig MySQL).

---

## 4. السكربتات (كلها في `scripts/`)

| السكربت | الوظيفة |
|---|---|
| `catalog_download_worker.py` | يسحب فيديوهات `pending` عبر loader.to → `catalog_workspace/`. `--series <slug>` / `--limit N` / `--dry-run`. حارس قرص (يقف عند 15GB فاضي). |
| `catalog_full_worker.py` | **المعالجة الكاملة** (الرئيسي). لكل فيديو: SRT من DB → Gemini metadata → intro → رفع YouTube+playlist+thumbnail → FB+TG رئيسي → 3 shorts (4 منصات) → اقتباسات TG → cleanup. `--series` / `--limit` / `--dry-run`. |
| `catalog_upload_worker.py` | نسخة بسيطة (main+intro+metadata بس) — **مهجورة**، الـ full worker بديلها. |
| `intro_prepend.py` | `prepend_intro(src, out)` — يضيف الـ intro (3 ثواني) في بداية الفيديو الرئيسي. |
| `branded_thumbnail.py` | `make_branded_thumbnail(title, out, template=None)` — thumbnail احترافي. |
| `gemini_web.py` | `generate_json(prompt)` — بديل Gemini API عبر المتصفح (لما الـ API يضرب 429). |

---

## 5. الـ intro (مقدمة موحّدة على كل فيديو)

- المصدر: أول 3 ثواني من فيديو YouTube `PAcy2-IAOJ0` (سلسلة العشر).
- الملف: `/opt/abuhafs/assets/intro.mp4` (1280x720, 30fps, h264+aac, 3.0s).
- `scripts/intro_prepend.py`: يعمل normalize (scale+pad للـ intro لمقاس الفيديو، يحافظ على دقة المحاضرة) + concat بإعادة ترميز. fallback للأصل لو فشل. يتعامل مع فيديوهات بدون صوت + metadata بايتس مش UTF-8.
- **يُطبّق على الفيديو الرئيسي فقط** (مش الـ shorts).

---

## 6. الـ Thumbnail الاحترافي

`scripts/branded_thumbnail.py` — يعيد إنتاج تصميم القناة المحترف: خلفية ثابتة (الشيخ + خلفية) + اللوجو + العنوان في صندوقين (كحلي `#0F2030` + أخضر `#5A8A3C`) بخط `Cairo-Bold`.

**نظام الـ templates (REGISTRY):** قائمة dicts، كل تصميم له:
- `image` (أي مقاس، يتعمله resize لـ 1280x720)، `box_side` (left/right)، `box_outer`/`box_inner_limit`/`box_min_extent`، `band_top`/`band_bottom`، `logo_safe`.
- التصميمان الحاليان: `thumb_base_ref.jpg` (مكتبة، صناديق شمال، لوجو يمين) + `thumb_base_ref2.jpg` (صناديق يمين، لوجو شمال).
- **التنويع تلقائي** (CRC32 للعنوان يختار template) — يتنقّل بين التصاميم.
- **توازن الكلمات:** يقسم كلمات العنوان بالنُص (round(n/2)) بين الصندوقين — متوازن (3/3 مش 5/1).

**إضافة تصميم جديد لاحقاً:** (1) حط الصورة في `/opt/abuhafs/assets/`، (2) أضف dict في `REGISTRY` بإحداثياته، (3) خلاص — التنويع يلتقطه تلقائياً.

> الفيديوهات القديمة منخفضة الجودة (240p) لا تصلح للقطات frame؛ لذلك نستخدم خلفية احترافية ثابتة + تغيير العنوان فقط.

---

## 7. توليد الـ metadata (Gemini)

- **الأساسي:** Gemini API (`gemini-2.5-flash`، مفاتيح rotation في `config.json`) — يولّد عنوان/وصف/chapters/shorts/quotes من الـ SRT.
- **القيد:** free-tier ~60 طلب/يوم (3 مفاتيح × 20). يتجدّد ~10:00 صباحاً القاهرة (midnight Pacific). الفيديوهات الطويلة بتاكل طلبات بسبب اقتطاع JSON عند 8192 token.
- **البديل (web fallback):** `scripts/gemini_web.py` — يشغّل `gemini.google.com` بـ Playwright (session من `credentials/yt_storage_state.json`؛ الدردشة مش "إجراء حسّاس" فمفيش تحدي من الـ datacenter IP) ويرجّع JSON. متوصّل في `_generate_with_retry` بالـ full worker: لما الـ API يضرب 429/exhausted → يستخدم الـ web (مجاناً، بحساب المستخدم — مفيش بطاقة للـ API المدفوع). ~15s/call.
- ⚠️ **معروف/قيد التنفيذ:** الـ web fallback حالياً بيشتغل بس مع أخطاء "rate limit" — **مش** مع الردود الفاضية/الناقصة. (يحتاج توسيع.)

---

## 8. الجدولة (crons) — `CRON_TZ=Africa/Cairo`

```
# التحميل (loader.to) كل 30 دقيقة، flock يمنع التداخل
*/30 * * * *  catalog_download_worker.py --series sbl-alslam-almosl-al-blogh-almram --limit 4
*/30 * * * *  catalog_download_worker.py --series slsl-alghzo-alfkry --limit 4

# المعالجة الكاملة — بعد تجديد Gemini (~10ص القاهرة)
0 11 * * *    catalog_full_worker.py --series sbl-alslam-almosl-al-blogh-almram --limit 5
0 13 * * *    catalog_full_worker.py --series slsl-alghzo-alfkry --limit 5
```
> الـ upload crons اتنقلت من 06:00/19:00 لـ 11:00/13:00 لأن حصّة Gemini المجانية بتتجدّد ~10ص القاهرة (الـ 06:00 كان بيفشل بـ 429).

**الـ shorts منفصلة في queue + dispatcher (2026-06-19):** الـ worker مابيرفعش الـ shorts inline — بيقطّعهم لـ `/opt/abuhafs/catalog_shorts/<main_id>/` ويضيفهم لجدول `catalog_shorts` (status=pending). سكربت `scripts/shorts_dispatcher.py` (cron كل ساعة :30، `--limit 3`) ينشرهم موزّعين على اليوم على YouTube+FB Reel+IG Reel+Telegram، مع **IG cap = 24/24h** (لو اتجاوز، يتخطّى IG ويكمّل YT+FB+TG). بيمسح ملف الـ short بعد النشر. ده يحل حد Instagram + أحسن للتفاعل. (الـ worker لسه بيمسح الـ mp4 الرئيسي الكبير، بس بيسيب الـ shorts الصغيرة لحد ما الـ dispatcher ينشرهم.)

**نموذج الـ 2-track:** سلسلة قديمة/كلاسيكية + جديدة/أقصر بالتوازي (زي شرح الرسالة + العبودية قبل كده). الحالي: **سُبل السلام** (216) + **الغزو الفكري** (10). لما سلسلة تخلص → نوجّه المسار لسلسلة جديدة (نغيّر الـ slug في الـ download + upload crons، ونحدّث الـ download workspace).

---

## 9. الـ Quota والحدود

- **YouTube:** اتزاد لـ **100,000 وحدة/يوم** (كان 10k) لمدة 6 شهور (موافقة Google 2026-06-19، project 296901012747). فيديو كامل = ~6400 (1 main + 3 shorts × 1600). يكفي ~15 فيديو/يوم. `DAILY_BUDGET=90000` في الـ full worker (يحمي الـ MAIN؛ الـ shorts بترفع جوه `_upload_shorts_for_video` فمش محكومة بالـ budget مباشرة). يتجدّد midnight Pacific (~07:00 UTC / 10:00 القاهرة).
- **Gemini:** البوتل نك الحقيقي دلوقتي (free-tier 60/يوم). الحل الجذري = Gemini API مدفوع (مفيش بطاقة حالياً) أو الـ web fallback.
- **التنظيف:** كل فيديو يتمسح بعد رفعه (الـ mp4)؛ المساحة تفضل صاحية. الـ download worker يقف عند 15GB فاضي.

> **درس مهم:** الـ download لازم يحمّل **السلاسل النشطة بس** (`--series`)، مش بالترتيب الأبجدي. في 2026-06-16 حمّل 678 فيديو من سلاسل غلط وملا الهارد — مسحناهم ورجّعناهم pending (loader.to يعيد تحميلهم مجاناً وقت دورهم).

---

## 10. ظهور السلاسل للزوار (مهم)

عشان السلسلة تظهر للناس لازم **حاجتين**:
1. **الفيديوهات public** (الـ full worker بيعملها public بعد الرفع؛ النسخة القديمة كانت بتسيبها private).
2. **الـ playlist public** — `get_or_create_playlist` في `src/youtube_uploader.py` اتصلّح ليعمل playlists public افتراضياً (كانت unlisted). كل playlists السلاسل اتحوّلت public في 2026-06-19.

---

## 11. الحالة (2026-06-19)

| | العدد |
|---|---|
| إجمالي الكتالوج | 2634 |
| مرفوع (uploaded) | 7 |
| skipped (مرفوع قبل كده: العبودية/الرسالة) | 40 |
| pending | 2587 |
| السلاسل النشطة | سُبل السلام (216) + الغزو الفكري (10) |

**سلاسل خلصت سابقاً** (من الأرشيف المحلي، مش الكتالوج): العبودية 14/14، شرح الرسالة.

---

## 11.5 إصلاحات مهمة (2026-06-19 مساءً)

- **صيغة SRT (نقطة vs فاصلة) — سبب جذري لـ shorts=0:** بعض السلاسل (مثل أنواع الاختلاف) الـ `subtitle_srt` فيها التوقيتات بصيغة VTT بالنقطة (`00:00:02.399`) بدل صيغة SRT القياسية بالفاصلة (`00:00:02,399`). الـ parser بتاعنا بيقرا الفاصلة بس → كان بيعتبر الـ SRT فاضي → عنوان عام + 0 shorts. **الإصلاح:** `_normalize_srt` في `catalog_full_worker.py` بيحوّل النقطة لفاصلة في كل التوقيتات (على الجلب والـ cache). متأكد: بعد الإصلاح المدة=602ث والنص=3761 حرف لفيديو 4wWghGJRKMU.
- **تقسيم Gemini:** `GeminiGenerator.generate_split` — 3 طلبات (core / shorts / quotes) بدل واحد كبير، يمنع الاقتطاع. `catalog_full_worker._generate_split` يستخدمه (API per-sub-call + web fallback).
- **توسيع الـ web fallback:** يشتغل دلوقتي مع الردود الفاضية/الناقصة مش بس rate-limit.
- **الـ web fallback بقى مجهول (anonymous) — حل دائم:** بدل ما نعتمد على جلسة مسجّلة دخول (بتـ logout وتنتهي)، الـ `gemini_web.py` بيدعم `GEMINI_WEB_ANON=1` → tab مجهول جديد كل طلب (مفيش session نصيانها). اتأكد: anonymous Gemini web بيرجّع JSON صالح. الـ upload crons بتمرّر `GEMINI_WEB_ANON=1`. (لو الوضع المجهول ضرب rate limit، نرجّع للـ session المسجّل دخول.)

## 12. مهام مفتوحة / TODO

1. **توسيع الـ Gemini web fallback** ليشتغل مع الردود الفاضية/الناقصة (مش بس 429) — يوقف إنتاج فيديوهات بعنوان عام بدون shorts.
2. **إعادة رفع أنواع الاختلاف (6)** بالمعالجة الكاملة + الـ thumbnail الجديد (موقوفة لحد إصلاح #1).
3. **Gemini مدفوع** لاستغلال الـ 100k quota بالكامل (~15 فيديو/يوم) بدون اقتطاع — يحتاج وسيلة دفع.
4. تنويع تصاميم الـ thumbnail أكتر (المستخدم هيجيب صور إضافية).
5. توجيه المسارات لسلاسل جديدة لما الحالية تخلص.
