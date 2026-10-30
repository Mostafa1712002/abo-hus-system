# نشر الأرشيف المحلي (C:\videos\data) على القناة

_بدأ العمل به 2026-10-30_

## المسار
```
C:\videos\data\<سلسلة>\  ──zip (STORE)──►  سيرفر التخزين: abuhafsi@213.239.209.167:22422 ~/videos_cold_zips/<سلسلة>.zip
                                              │  scripts/local_feed.py يسحب ملفًا واحدًا فقط (unzip -p عبر ssh)
                                              ▼
                     /opt/abuhafs/catalog_workspace/<slug>/<file>  ──►  catalog_full_worker.py --series <slug> --limit 1
                     (intro + YouTube + playlist + thumbnail + FB/IG/TG + shorts في طابور الـ dispatcher، ثم يُحذف الملف)
```
- سكربت الضغط والرفع من الجهاز: `pc/zip_and_upload.py`، يمكن استئنافه، ويتخطى أي zip موجود على السيرفر بنفس الحجم.
- الاستيراد: `scripts/local_import.py scripts/local_manifest.json --apply`. يأخذ backup من الـ DB تلقائيًا (`data/abuhafs.db.bak.localimport.*`).
- حالات الصف: `download_status='local'` و`mp4_path='cold://<zip>/<file>'` ← `downloaded` ← `archived`/`uploaded`.
- `youtube_id` = معرّف الحلقة على القناة المصدر إذا وُجدت هناك، فيُؤخذ التفريغ من DB الموقع ويُعمل منه عنوان AI وshorts واقتباسات. وإلا يكون `L-<slug>-<n>`، ويُرفع الفيديو الرئيسي فقط بعنوان ثابت.
- الداني رُبط بالكتالوج حسب `ord` وليس حسب المعرّف. صفوف الكتالوج لفتح المغيث وطريق الوصول والداني تحوّلت إلى النسخة المحلية، بناءً على قرار المستخدم.
- `config.json → cold_storage.ssh_port` صار 22422 (كان 22).

## المسارات اليومية (crontab الـ root، بتوقيت القاهرة، وتشترك في `/tmp/cat_up.lock`)
| الساعة | المسار (ينتقل للسلسلة التالية تلقائيًا عند انتهاء الحالية) |
|---|---|
| 11:00 | المدخل ← الفوائد |
| 12:00 | سنن الترمذي ← فتح المغيث |
| 13:00 | شرح اعتقاد أهل السنة (التسجيل الثاني) ← جزء أبي عمرو الداني |
| 14:00 | المتجر الرابح ← طريق الوصول ← فضائل الصحابة |
| 15:00 | الرقاق ← القدر |
اللوج: `logs/local_lanes.log` + `logs/catalog_full.log`. الـ backup: `/root/crontab.bak.locallanes.*`.

## لم يُستورد بعد
فيديوهات مفردة قد تكون مكررة مع ما سبق رفعه: الإيمان لابن أبي شيبة، المنهج السلفي، مختصر الصارم المسلول، الثلاثيات، صحيفة همام، تراجم، كلمة تونس. وكذلك ملف `الترمذي 67.WMA` الصوتي.

## الوضع لكل سلسلة (2026-10-30)
| السلسلة | منشور على قناتنا | في طابور الرفع (محلي) | في طابور الكتالوج | ناقص — موجود عند القناة المصدر | غير موجود في أي مكان |
|---|---|---|---|---|---|
| المدخل إلى السنن الكبرى | 1–39 | 51–56، 61–80 | — | 40–50، 57–60 | — |
| سنن الترمذي | — | 59–80 | منهج الترمذي 1–15 + 1–28 | 29–58 | — |
| شرح اعتقاد أهل السنة (التسجيل الثاني) | — | ترجمة + 1–22 | — | — | — |
| المتجر الرابح | — | ترجمة + 1–21، 23 | — | — | 22 |
| كتاب الرقاق | — | 19، 25–35 | — | 1–18، 20–24 | — |
| كتاب القدر | — | 1 | — | 2–3 | — |
| الفوائد | — | 1–31 | — | — | — |
| فتح المغيث | — | 1–38 | — | — | — |
| طريق الوصول | — | 1–7 | 4-1 | — | — |
| جزء أبي عمرو الداني | — | 1–30 | — | — | — |
| فضائل الصحابة | — | 1–5 | — | — | — |

بدون SRT، فيُرفع الفيديو الرئيسي فقط بدون shorts (42 فيديو): المدخل 68 و74–80، الترمذي 66–80، اعتقاد 11 و16–22، المتجر 5 و19–21 و23، الرقاق 35، فضائل الصحابة 1–5.

## الحلقات الناقصة من قناة التأصيل (أُضيفت 2026-10-30)
- 70 حلقة جديدة (المدخل 40–50 و57–60، الترمذي 29–58، الرقاق 1–18 و20–24، القدر 2–3)، ومعها صفوف الترمذي الـ 43 القديمة في الكتالوج. كلها دخلت المسارات بحالة `download_status='source'`، وهذه الحالة يتجاهلها التحميل العام وإعادة المحاولة في `feed_and_advance`.
- ترتيب الترمذي: «منهج الترمذي» 1–15 بقيمة `ord` تساوي n−100 (أي −99 حتى −85)، ثم الشرح من 1 حتى 80.
- السكربت: `scripts/local_add_source.py` (يأخذ backup باسم `abuhafs.db.bak.sourceadd.*`).
- **loader.to صار يطلب API key عند الطلب من السيرفرات (منذ ~2026-10-27 يرجّع 403 `api_key_required`)**، لذلك التحميل يتم على جهاز المستخدم:
  `pc/source_pipeline.py` يحمّل بـ yt-dlp إلى `C:\videos\source_dl\<سلسلة>\`، ثم يعمل zip باسم `<سلسلة> - التأصيل.zip` ويرفعه لسيرفر التخزين، ثم يشغّل `scripts/local_mark_cold.py` فيحوّل الصفوف من `source` إلى `local`.
- المسار ينتظر عند أي حلقة `source` لم تصل بعد، عمدًا، للحفاظ على ترتيب الدروس.

## ⚠️ إعادة رفع الكتالوج متوقفة
كل التحميلات من loader.to تفشل بخطأ 403 منذ 2026-10-27 (آخر تحميل ناجح 2026-10-27 10:33). فيه 1,222 صف pending لن تُحمَّل حتى يُحل الموضوع: API key مدفوع، أو التحميل من جهاز المستخدم، أو endpoint على whisper.markoum.com.

## SRT prefetch (whisper), added 2026-10-30
Most local/source episodes have no `subtitle_srt` in the site DB (المدخل, الرقاق, القدر, الاعتقاد,
المتجر, الفوائد, half of الترمذي), so the worker would upload them with the raw title and no shorts.
`scripts/srt_prefetch.py 3` (cron `15,45 * * * *`, log `logs/srt_prefetch.log`) asks
whisper.markoum.com (`/whisper/youtube.php`, async: the first call queues, later calls return the SRT)
for the next 3 episodes per lane series and writes the result into the worker cache
`/opt/abuhafs/catalog_meta/<yt>.srt`, which `fetch_srt` reads first. Skips ids that already have an
SRT on the site and synthetic `L-` ids (not on YouTube, still main video only).

First end-to-end lane run: 2026-10-30 المدخل 40 → https://youtu.be/jBsv6QTzj7E (no SRT yet, raw title).
