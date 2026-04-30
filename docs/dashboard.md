# لوحة التحكم — Dashboard

لوحة مراقبة محلية للـ pipeline (YouTube + Facebook + Instagram + Telegram).
مبنية بـ FastAPI + Jinja2 + HTMX + Tailwind CDN.

## التشغيل

```cmd
dashboard.bat
```

ثم افتح: <http://127.0.0.1:8000/>

السكربت يقوم بـ:

1. تفعيل `venv`
2. تشغيل `uvicorn src.dashboard_app:app --reload --host 127.0.0.1 --port 8000`

## البنية

```
src/dashboard_app.py        # تطبيق FastAPI
templates/dashboard.html    # القالب الرئيسي (RTL, Tailwind, HTMX)
static/                     # ملفات ثابتة (logo.png ...)
dashboard.bat               # مُشغّل
```

البيانات تُقرأ مباشرة من `output/pending.json` عبر `PendingTracker`،
وعرض المكتبة من `src/wave_planner.py` (يقرأ مسار `paths.videos_input`
من `config.json`)، والسجلات من `logs/process_check.log` و `logs/uploader.log`.

## المسارات (Routes)

### HTML
| Route | الوصف |
|-------|-------|
| `GET /` | الواجهة الرئيسية |

### API JSON
| Route | الوصف |
|-------|-------|
| `GET /api/stats` | إحصائيات: pending, processing, completed, failed, today_completed |
| `GET /api/videos?status=&series=&limit=` | كل الفيديوهات مع روابط المنصات |
| `GET /api/video/{video_id}` | تفاصيل فيديو واحد + روابط |
| `GET /api/recent?limit=20` | آخر المكتمل/الفاشل |
| `GET /api/library` | تقسيم الموجات (Wave 3/2/1) |
| `GET /api/logs?n=200` | آخر N سطر من ملفي السجل |
| `POST /api/retry/{video_id}` | إعادة محاولة لفيديو فاشل (status -> uploaded) |
| `GET /api/docs` | OpenAPI Swagger UI |

### HTMX Partials (تُستخدم داخليًا فقط)
- `/partials/stats` (تحديث كل 15ث)
- `/partials/library` (تحديث كل 60ث)
- `/partials/active` (تحديث كل 15ث)
- `/partials/recent` (تحديث كل 15ث)
- `/partials/failed` (تحديث كل 30ث)
- `/partials/logs?n=100` (تحديث كل 5ث)

## أقسام الواجهة

1. **رأس** — اسم القناة + ساعة حية.
2. **بطاقات الإحصائيات** — في الانتظار / قيد المعالجة / مكتمل اليوم / فاشل.
3. **نظرة عامة على المكتبة** — جدول لكل موجة: عدد السلاسل، عدد الفيديوهات،
   تم رفعها، الباقي، شريط تقدّم.
4. **قائمة الانتظار النشطة** — الفيديوهات التي حالتها
   `uploaded | captions_ready | processing` مع عداد محاولات الترجمة.
5. **آخر الفيديوهات (٢٠)** — جدول بالعنوان، السلسلة، الحالة، الوقت،
   وروابط للمنصات (YT, SH, FB, IG, TG).
6. **الفاشلة** — تظهر إن وُجدت، مع زر "إعادة المحاولة" يستدعي
   `POST /api/retry/{video_id}`.
7. **السجل المباشر** — مربعا monospace لـ `process_check.log` و
   `uploader.log` (آخر 100 سطر).

## التوسيع

- **سياسات حالة جديدة**: عدّل `STATUS_LABEL_AR` في `dashboard_app.py`.
- **روابط منصات**: عدّل `_video_links()` و `_platform_links_html()`.
- **تحديثات أسرع/أبطأ**: غيّر القيمة في `hx-trigger="every Xs"` داخل
  `templates/dashboard.html`.
- **زيادة اللوقات المعروضة**: غيّر `?n=100` في `hx-get` للسجل.

## النشر إلى `abuhafs.7erfa-system.com` (مرجعي — خارج النطاق الحالي)

> **ملاحظة:** هذه اللوحة الآن **بدون مصادقة**. قبل النشر يجب إضافة
> طبقة auth (Basic auth خلف nginx، أو OAuth، أو مفتاح مشترك).

### تشغيل عبر gunicorn + uvicorn worker

```bash
pip install gunicorn uvicorn[standard]
gunicorn src.dashboard_app:app \
  -k uvicorn.workers.UvicornWorker \
  -w 2 \
  -b 127.0.0.1:8000 \
  --access-logfile - \
  --error-logfile -
```

### مقتطف systemd

```ini
# /etc/systemd/system/abuhafs-dashboard.service
[Unit]
Description=Abuhafs Pipeline Dashboard
After=network.target

[Service]
Type=simple
User=abuhafs
WorkingDirectory=/srv/abuhafs/youtube-auto-uploader
Environment="PATH=/srv/abuhafs/youtube-auto-uploader/venv/bin"
ExecStart=/srv/abuhafs/youtube-auto-uploader/venv/bin/gunicorn \
  src.dashboard_app:app -k uvicorn.workers.UvicornWorker \
  -w 2 -b 127.0.0.1:8000
Restart=always

[Install]
WantedBy=multi-user.target
```

### مقتطف nginx

```nginx
server {
    listen 443 ssl http2;
    server_name abuhafs.7erfa-system.com;

    # ssl_certificate ... ;
    # ssl_certificate_key ... ;

    # Basic auth (مؤقت لحين تركيب OAuth)
    auth_basic           "Abuhafs Dashboard";
    auth_basic_user_file /etc/nginx/.htpasswd-abuhafs;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # SSE/HTMX-friendly
        proxy_buffering off;
        proxy_read_timeout 60s;
    }

    location /static/ {
        alias /srv/abuhafs/youtube-auto-uploader/static/;
        expires 7d;
        access_log off;
    }
}

server {
    listen 80;
    server_name abuhafs.7erfa-system.com;
    return 301 https://$host$request_uri;
}
```

### htpasswd سريع

```bash
sudo apt-get install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd-abuhafs admin
```

## الملاحظات

- اللوحة **للقراءة فقط** عدا `POST /api/retry/{video_id}` (يعيد فيديوًا فاشلًا
  لحالة `uploaded` ليلتقطه الـ pipeline من جديد).
- ملفات السجل تُقرأ بنمط tail آمن (deque) — لا تُغلق المتعقبين الذين
  يكتبون عليها. لكن على Windows لو الملف مفتوح للكتابة بأقفال حصرية فقد
  ترى محتوى متأخرًا قليلاً، وهذا مقبول لشاشة مراقبة.
- التحديث المباشر يعتمد على HTMX polling (5–60ث حسب القسم). لا توجد WebSockets
  للحفاظ على البساطة.
