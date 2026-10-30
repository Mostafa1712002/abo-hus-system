# منظومة أبي حفص — مشروعان منفصلان

_آخر تحقق: 2026-10-30_ — الاثنان على نفس السيرفر `178.104.122.98` (`7erfa-system.com`, `ssh root@...`).

| | 1) الموقع الرئيسي | 2) نظام الرفع (uploader) |
|---|---|---|
| الوظيفة | موقع **abuhafs.info** (فيديو/صوت/كتب/فتاوى/أحاديث...) | تقطيع ومعالجة ورفع الفيديوهات على YouTube + FB + IG + Telegram |
| التقنية | Laravel 12 + Filament (PHP 8.2) | Python (venv) + FastAPI dashboard |
| المحلي | `C:\Users\aldaa\data\projects\abuhafs` | `C:\Users\aldaa\data\projects\youtube-auto-uploader` |
| GitHub | `Mostafa1712002/abuhafs` | `Mostafa1712002/abo-hus-system` |
| الفرع الحيّ | **`live/abuhafs-info`** | `main` |
| على السيرفر | `/var/www/abuhafs.info` (www-data) | `/opt/abuhafs/youtube-auto-uploader` (root) |
| التشغيل | nginx + php8.2-fpm، cron `/etc/cron.d/abuhafs-info` | crontab الـ root + `abuhafs-dashboard.service` |
| الرابط | https://abuhafs.info (أدمن `/admin`) | https://abuhafs.7erfa-system.com/catalog |
| DB | MySQL `abuhafs_info` | SQLite `data/abuhafs.db` (جدول `catalog_videos`...) + `config.json` (gitignored) |

## العلاقة بينهما
- الموقع = **الكتالوج**: كل فيديوهات الشيخ (embed YouTube) + الـ SRT + الأوصاف.
- الـ uploader يقرأ الكتالوج (SRT/ترتيب السلاسل) ويعيد رفع السلاسل على قناتنا بعد المعالجة (intro + عنوان/وصف AI + shorts + اقتباسات) وينشرها على المنصات.
- الموقع بدوره يستورد يوميًا الرفعات الجديدة من YouTube (`youtube:import-playlists` 03:30).

## النشر
**الموقع:**
```bash
git push origin live/abuhafs-info
ssh root@178.104.122.98 "cd /var/www/abuhafs.info && sudo -u www-data git pull origin live/abuhafs-info && sudo -u www-data php8.2 artisan optimize:clear && sudo -u www-data php8.2 artisan optimize"
```
**الـ uploader:**
```bash
git push origin main
ssh root@178.104.122.98 "cd /opt/abuhafs/youtube-auto-uploader && git pull origin main"
# لو اتغيّر شكل config.json → عدّل config.json على السيرفر يدويًا (بعد backup)
# لو اتغيّر الـ dashboard → systemctl restart abuhafs-dashboard
```

تفاصيل الموقع: `docs/how-to-deploy-and-work.md`. تفاصيل الـ uploader: `youtube-auto-uploader/docs/`.
