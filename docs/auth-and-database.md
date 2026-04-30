# المصادقة وقاعدة البيانات — Auth & Database

تم إضافة تسجيل دخول للوحة التحكم وقاعدة بيانات SQLite كـ persistent store
بديل للـ `output/pending.json`. الـ JSON لسه شغّال كـ fallback خلال طور
الانتقال.

## المخطط (Schema)

ملف القاعدة: `data/abuhafs.db` (SQLite). يتم إنشاؤه تلقائيًا عند التشغيل.

| Table | الوصف |
|-------|-------|
| `users` | حسابات لوحة التحكم. الباسورد بـ `bcrypt`. |
| `videos` | السجل الرئيسي للفيديوهات (يعكس JSON القديم). يحتوي على `srt_text` لإعادة استخدام الترجمة لاحقًا. |
| `shorts` | الـ shorts المرتبطة بكل فيديو، مع IDs لـ YouTube/Facebook/Instagram/Telegram. |
| `quotes` | اقتباسات نصية (لتليجرام). |
| `srt_files` | جدول forward-compat لو احتجنا تخزين أكثر من نسخة SRT لكل فيديو. حاليًا الـ canonical SRT في `videos.srt_text`. |
| `logs` | سجل اختياري للأحداث (audit trail). |

التفاصيل في `src/db.py` (SQLAlchemy 2.x models).

## مسار تسجيل الدخول

1. أي طلب لـ `/` أو `/api/*` بدون session يُحوَّل إلى `/login?next=...`
2. الـ `POST /login` بياخد `email` و `password` ويتحقق من `bcrypt`.
3. عند النجاح: تُكتب `user_id` في الـ session cookie (موقعة بـ
   `itsdangerous`) ويتم redirect لـ `next`.
4. `GET /logout` يمسح الـ session ويُعيد التوجيه لـ `/login`.

### متغير بيئة مطلوب في الإنتاج

```bash
ABUHAFS_SESSION_SECRET=<random-32-byte-string>
```

لو غير معرّف، اللوحة تُولّد سرّاً عشوائياً مؤقتاً عند بدء التشغيل وتطبع
warning — الجلسات لن تنجو من إعادة تشغيل العملية. يفضّل ضبطه في وحدة الـ
systemd:

```ini
# /etc/systemd/system/abuhafs-dashboard.service
Environment=ABUHAFS_SESSION_SECRET=...
```

## إنشاء/تحديث مستخدم admin

```bash
# على الخادم:
cd /opt/abuhafs/youtube-auto-uploader
source venv/bin/activate
python -m src.db_migrate create-admin <email> <password> "<name>"

# مثال:
python -m src.db_migrate create-admin mostafa@midade.com 'StrongPass!23' Mostafa
```

نفس الأمر يحدّث الباسورد لو الإيميل موجود سابقًا.

## ترحيل البيانات الحالية

```bash
python -m src.db_migrate
```

* idempotent — آمن تشغيله أكتر من مرة.
* يقرأ `output/pending.json` ويضيف أي فيديوهات جديدة للـ DB.
* يربط ملف SRT الموجود في `output/srt/<base>.srt` (إن وُجد) ويخزنه في
  `videos.srt_text`.

## نسخة احتياطية

```bash
cp data/abuhafs.db data/abuhafs.db.bak.$(date +%Y%m%d-%H%M)
```

أو لو عاوز SQL dump:

```bash
sqlite3 data/abuhafs.db .dump > data/abuhafs.sql
```

## استعلامات لاحقة (Sample queries)

### قراءة SRT لفيديو معيّن

```python
from src.db import get_session_factory, Video

S = get_session_factory()
with S() as s:
    v = s.query(Video).filter_by(video_id="AHVXVhmkvpk").first()
    print(v.srt_text[:500])
```

### كل الفيديوهات اللي عندها SRT متخزّن

```python
with S() as s:
    rows = s.query(Video).filter(Video.srt_text.isnot(None)).all()
    for v in rows:
        print(v.video_id, v.title, len(v.srt_text or ""))
```

### بحث نصي بسيط في كل الـ SRTs

```python
from sqlalchemy import or_

with S() as s:
    rows = (
        s.query(Video)
        .filter(Video.srt_text.like("%الشافعي%"))
        .all()
    )
    for v in rows:
        print(v.video_id, "—", v.title)
```

### كل الـ shorts لفيديو معيّن

```python
with S() as s:
    v = s.query(Video).filter_by(video_id="AHVXVhmkvpk").first()
    for sh in v.shorts:
        print(sh.yt_short_id, sh.fb_reel_id, sh.ig_reel_id)
```

## Dual-write (لماذا لسه بنكتب JSON؟)

الـ `PendingTracker` بيكتب في الاتنين (DB + JSON) عند `add()` و`update()`.
السبب:

* أي cron job أو سكريبت قديم لسه بيقرأ من JSON يفضل شغّال بدون أي تعديل.
* لو حصل bug في DB layer، لسه عندنا الـ JSON كـ source of truth.
* انتقال تدريجي وآمن.

ممكن نشيل JSON بعد ما:

1. كل الكود اللي بيقرأ من tracker يبقى متأكد إنه شغّال على DB.
2. النسخ الاحتياطية تكون متظبّطة على ملف الـ DB.

ساعتها يبقى مجرد حذف للسطر `self.save()` في
`src/pending_tracker.py` وحذف الـ fallback في `load()`.

## إيقاف DB مؤقتًا (rollback)

في `config.json`:

```json
"persistence": {
  "use_database": false,
  "fallback_to_json": true
}
```

اللوحة هترجع تقرأ JSON فقط. لاحظ إن الـ login بيتطلب الـ DB — يعني لو
عطّلت DB، اللوحة تبقى inaccessible. الأفضل تترك `use_database=true` دايمًا
وتعمل rollback على مستوى الكود لو احتجت.
