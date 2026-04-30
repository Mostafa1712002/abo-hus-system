# Cleanup Strategy — استراتيجية تنظيف القرص

سكريبت يومي بيشتغل من خلال Windows Task Scheduler عشان يفضي مساحة القرص بعد ما الفيديوهات تكتمل في الـ pipeline. الهدف: ما نخليش الـ intermediate files تتراكم لأسابيع وتاكل القرص.

---

## What gets deleted vs kept

| Path | بعد ما الفيديو يكتمل + publish_at يعدّي | لو cleanup.delete_intermediate_files | السبب |
|---|---|---|---|
| `<original_path>` (الفيديو الأصلي على E:\) | يتحذف لو `delete_after_completion=true` | — | الفيديو راح على يوتيوب وانتشر فعلياً |
| `output/srt/<base>.srt` | — | يتحذف | متعمل خلاص ما لهوش لزمة |
| `output/srt/<base>.clip_*.srt` | — | يتحذف | subtitles للـ shorts اللي اتقصت |
| `output/shorts/<base>/*.mp4` | — | يتحذف | الـ Shorts اترفعت على يوتيوب/FB/IG/تليجرام |
| `output/shorts/<base>/*.jpg` | — | يتحذف | thumbnails مؤقتة للـ Shorts |
| `output/metadata/<base>.json` | **محفوظ** | محفوظ | السجل التاريخي للـ AI metadata |
| `output/thumbnails/<base>.jpg` | **محفوظ** | محفوظ | thumbnail الفيديو الرئيسي (سجل) |

**القواعد الذهبية:**

- ما بنحذفش الأصل إلا لو `cleanup.delete_after_completion=true` **و** `status='completed'` **و** `publish_at` فات (يعني الفيديو فعلاً اتنشر، مش بس اتعالج).
- لو `original_path` على درايف مش متاح (E:\ مش متركبة)، السكريبت بيتخطى ويسجل warning، ما بيكرشش.
- لو ملف اتحذف يدوياً قبل ما السكريبت يشتغل، بيتم تخطّيه بصمت.

---

## Archive log format

كل entry بتترحّل من `pending.json` لـ append-only JSONL تحت:

```
output/archive/completed-YYYY-MM.jsonl
```

ملف لكل شهر عشان يفضل قابل للقراءة مع الزمن. كل سطر = JSON object واحد:

```json
{"video_id":"AHVXVhmkvpk","original_path":"E:\\...\\1.wmv","original_name":"1.wmv","series":"شرح الرسالة","uploaded_at":"...","status":"completed","completed_at":"...","title_updated":"الإمام الشافعي ...","publish_at":"...","playlist_id":"PLY...","metadata":{...},"archived_at":"2026-04-30T03:00:00+00:00","archive_reason":"completed"}
```

`archive_reason` بيكون:
- `completed` — كل اللي خلصوا الـ pipeline.
- `failed_old` — اللي حالتهم `failed` وأقدم من 14 يوم.

### كاڤيت

ملفات الأرشيف بتزيد للأبد. بس JSONL plain، فأي وقت تحب تختصرها:

```powershell
# آخر 100 سطر
Get-Content output\archive\completed-2026-04.jsonl -Tail 100

# عدد المؤرشفين هذا الشهر
(Get-Content output\archive\completed-2026-04.jsonl).Count

# دور على video_id معيّن
Select-String -Path output\archive\*.jsonl -Pattern "AHVXVhmkvpk"
```

بعد ما الملف يكبر سنة-سنتين، تقدر تضغطه (`Compress-Archive`) أو تنقله لـ cold storage.

---

## How to disable cleanup

في `config.json`:

```json
"cleanup": {
  "enabled": false
}
```

السكريبت هيخرج من غير ما يلمس حاجة. كمان، تقدر تعطّل أجزاء بعينها:

```json
"cleanup": {
  "enabled": true,
  "delete_after_completion": false,    // متلمسش الفيديوهات الأصلية
  "delete_intermediate_files": true,   // بس امسح الـ SRT/Shorts المؤقتة
  "keep_final_outputs": true,
  "cron_hour": 3
}
```

---

## How to manually run a dry-run

من غير ما تحذف أي حاجة، عشان تشوف اللي هيتعمل:

```powershell
C:\Users\aldaa\data\projects\youtube-auto-uploader\venv\Scripts\python.exe -c "
from src.config import Config
from src.cleanup import cleanup_completed_videos, cleanup_failed_old_videos
cfg = Config('config.json')
print('completed:', cleanup_completed_videos(cfg, dry_run=True))
print('failed:   ', cleanup_failed_old_videos(cfg, dry_run=True, age_days=14))
"
```

Output بيوضّح:
- `archived` — كام entry هينقل لـ archive.
- `videos_deleted` — كام أصل هيتحذف فعلياً.
- `space_freed_mb` — حجم المساحة المتوقعة.
- `errors` — أي مشاكل.

---

## Windows Task

| | |
|---|---|
| Task name | `AbuHafsCleanup` |
| Schedule | Daily at `cron_hour` (default 3 AM) |
| Action | `C:\Users\aldaa\data\projects\youtube-auto-uploader\cleanup.bat` |
| Log | `logs\cleanup.log` |

### إنشاء/إعادة إنشاء التاسك

```powershell
schtasks /Create /SC DAILY /ST 03:00 /TN "AbuHafsCleanup" `
  /TR "C:\Users\aldaa\data\projects\youtube-auto-uploader\cleanup.bat" /F
```

`/F` بيعمل overwrite للتاسك الموجود (idempotent).

### معاينة التاسك

```powershell
schtasks /Query /TN AbuHafsCleanup /FO LIST
```

### تشغيل يدوي فوري

```powershell
schtasks /Run /TN AbuHafsCleanup
```

### حذف التاسك

```powershell
schtasks /Delete /TN AbuHafsCleanup /F
```

---

## التفاعل مع `YouTubeProcessCaptions`

التاسكان مستقلين تماماً:

- `YouTubeProcessCaptions` — كل 15 دقيقة، بيكمل الـ pipeline (captions/AI/upload).
- `AbuHafsCleanup` — مرة في اليوم 3 صباحاً، بيرتب اللي خلص.

طول ما الـ cleanup بيشتغل بعد 3 ص (بعد `publish_hour_local=18`)، فالفيديوهات اللي اتنشرت في 6 م يومها هتتحذف صبح اليوم اللي بعده.

---

## Files added by this feature

- `src/cleanup.py` — الـ module الأساسي.
- `cleanup.bat` — launcher.
- `docs/cleanup-strategy.md` — هذا الملف.
- `output/archive/` — مجلد الأرشفة (يتعمل تلقائياً أول ما يحتاج).
- `logs/cleanup.log` — سجل التشغيل (يُكتب بكل run).
- إعدادات `cleanup` في `config.json` و `config.example.json`.
