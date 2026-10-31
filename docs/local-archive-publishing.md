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

## Compression before upload + damaged audio, 2026-10-30
- **Compression.** The home uplink is slow (~0.3 MB/s), so every file is now re-encoded on the PC before zipping (`pc/compress.py`: x264 CRF 27 veryfast, AAC 64k mono, `+faststart`). For these 320x240 lectures that's 65–80% smaller with no visible loss, at 100–200x realtime.
  - **Check.** An encode only counts if the output video **and audio** both cover at least 97% of the source duration; otherwise the original file ships.
  - **Archive repointing.** Archive files change extension (`.avi`/`.rmvb` → `.mp4`). Once the zip is on cold storage, `pc/zip_and_upload.py` sends the renames to `scripts/local_rename_members.py`, which repoints `mp4_path`.
  - **Source batching.** Source episodes ship in batches of 10 per series, `"<folder> - التأصيل <k>.zip"`, round-robin in lane-need order (الترمذي, الرقاق, القدر, المدخل 61+, الاعتقاد, المتجر).
- **Damaged audio.** `pc/audio_scan.py` found 35 archive AVIs whose MP3 track is corrupt: decoded start to finish, it covers only part of the lecture (or garbage). Seeking still plays in spots, so a player may hide it, but any ffmpeg re-encode (including `intro_prepend` on the VPS) produces broken audio. The list is in `pc/broken_audio.txt`; those files are not uploaded.
  - **17 with a source-channel copy:** moved to `download_status='source'`, so they get re-downloaded via `pc/source_pipeline.py`.
  - **18 with no copy anywhere (`L-` ids):** set to `download_status='broken'`. `local_feed.py` holds the lane at a broken row so episode order is kept until a good file is supplied or the row is skipped by hand.
  - **DB backup:** `abuhafs.db.bak.brokenaudio.*`.
- **2026-10-30, user decision:** the 18 'broken' rows were skipped (`download_status='skipped'`, `process_status='skipped'`; DB backup `abuhafs.db.bak.skipbroken.*`). The lanes go straight past them. Skipped: الاعتقاد 11, 19–21 · المدخل 68 · الترمذي 66–70, 73, 78, 79 · فضائل الصحابة 1–3, 5 · الرقاق 35.
- **2026-10-30, singles.** The user's rule is that anything already on our channel is deleted locally.
  - **Added** as series «دروس ومحاضرات متفرقة» (`local-mtfrkat`, appended to the 14:00 lane): المنهج السلفي, كلمة تونس.
  - **Deleted locally as duplicates of our channel:**
    - الإيمان لابن أبي شيبة: playlist «الإيمان للإمام ابن أبي شيبة», 5 parts, 5.47 h vs 5.76 h local. The row is skipped.
    - شرح مختصر الصارم المسلول: playlist «الصارم المسلول على شاتم الرسول ﷺ.», 4 parts, 3.34 h vs 3.96 h local.
  - **Held in `C:\videos\_review` pending the speaker check:**
    - تراجم: way2allah.com TV-studio recordings, which look like a different speaker.
    - الصحيفة والثلاثيات: no title card.

## Storage moved to the VPS; tarweig retired (2026-10-30)
- **Why:** the tarweig cPanel account `abuhafsi` is capped at **10 GB** and filled up. The server is being deleted on 2026-10-31.
- **New flow:** PC jobs (`pc/zip_and_upload.py`, `pc/source_pipeline.py`, run from `C:\videos\_pipeline`) upload zips to `/opt/abuhafs/archive_in` on the VPS.
  - `scripts/archive_ingest.py` CRC-tests each zip, unpacks it to `/opt/abuhafs/archive/<zip stem>/`, and repoints rows to `file://…`.
  - Renames are passed for archive zips and `mark` for source batches; then the zip is deleted.
  - Local files are deleted only after the ingest succeeds. `shipped.json` is the ledger.
- **Feeder:** `local_feed.py` moves `file://` episodes into the workspace, so the archive shrinks as lanes publish. `cold://` is still understood.
- **Migration:** the 10 zips already on tarweig were moved server to server (`scripts/migrate_cold_to_vps.sh`) and removed from tarweig.
  - The `~/mig` folder (pre-move DB dump, crontab, DNS zone) is backed up to `/root/tarweig_backup/mig`.
- **Main pipeline:** `config.json` `cold_storage` is disabled and the tarweig host removed (backup `config.json.bak.notarweig.*`).
  - Series now come from `/opt/abuhafs/videos_workspace` or `/opt/abuhafs/zips_archive/<series>.zip`.
- **شرح الرسالة:** complete (25/25 on the channel). Its 04:00 cron was removed; it had been failing daily on a missing tarweig zip.
- **pending.json:** the stale item (العبودية 1, `yDrUI6GxURc`) had been polling captions every 15 min for a video that is no longer on YouTube (4,500+ checks). It is marked completed.
- **2026-10-31, lane times.** The VPS cron ignores `CRON_TZ`, so all times are UTC; the lanes had been firing at 13:00–17:00 Cairo. They were moved to `0 9..13 * * *` UTC, which is 11:00–15:00 Cairo (EET).

## Downloads fixed: yt-dlp over Cloudflare WARP (2026-10-31)
- **Problem:** loader.to answers servers with `api_key_required` (paid key) since 2026-10-27, and YouTube bot-blocks this datacenter IP for every yt-dlp client.
- **Fix:** Cloudflare WARP is installed on the VPS in **proxy-only mode** (`warp-cli mode proxy`, SOCKS5 `127.0.0.1:40000`, Always On, `warp-svc` enabled). The box's own traffic is untouched; only yt-dlp goes through the proxy.
  - `scripts/catalog_download_worker.py` `loader_download()` now runs yt-dlp through that proxy, with 3 tries that clear stale `.f*` fragments between attempts. The name is kept because `local_feed.py` imports it.
  - Measured speed: a 1-hour 240p episode (~115 MB) downloads in ~25 s.
  - venv yt-dlp was upgraded to 2026.10.26.
- **Effect:**
  - The catalog re-upload (1,222 pending rows) works again through `feed_and_advance.sh` (every 30 min).
  - Source-channel episodes ('source' rows) are downloaded by the VPS itself when their lane reaches them. `pc/source_pipeline.py` is no longer needed (stopped 2026-10-31), which saves the home uplink.
- **Catalog crons:** they ran at 09 and 10 UTC, the same hours as the lanes. Those two moved to 14 and 15 UTC, giving catalog runs at 05–08, 14–15 and 18–21 UTC with 1 video each.
- **Disk guard:** `MIN_FREE_GB` was lowered from 15 to 10. The download buffer is only about 5 videos.
- **Debugging:** if downloads fail again, run `warp-cli --accept-tos status`, then `curl --socks5-hostname 127.0.0.1:40000 https://ifconfig.me`.
