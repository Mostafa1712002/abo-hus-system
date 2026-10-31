# Publishing the local archive: how to check and how to resume

Everything here is safe to rerun. Each script skips work that is already done.

## Where things stand
- `STATUS.md` is refreshed every 10 minutes while the jobs run. It also refreshes on demand with `python status.py`.
  - It lists the VPS archive (unpacked episodes waiting for their lane), the state of each series lane, and what is left on this PC.
  - It ends with the last lines of every log.
- `logs/` holds the job logs: `zip_upload.log` and `source_pipeline.log`.
  - The `*.tarweig-phase.log` files cover the earlier cold-storage phase.
- On the VPS:
  - `/opt/abuhafs/youtube-auto-uploader/logs/local_lanes.log` records the daily YouTube uploads (11:00–15:00 Cairo).
  - `logs/srt_prefetch.log` is the whisper log, which is on hold.

## The 2 jobs (run from this folder: `cd C:\videos\_pipeline`)
1. `python -u zip_and_upload.py >> logs\zip_upload.log 2>&1`
   - Handles the user's archive in `C:\videos\data`.
   - Steps: compress, zip one folder at a time, then upload to the VPS inbox `/opt/abuhafs/archive_in`.
   - On the VPS, `scripts/archive_ingest.py` CRC-tests the zip, unpacks it into `/opt/abuhafs/archive/<folder>/`, and repoints the DB rows.
   - Only after that does it delete the zip, the compressed copies and the originals from this PC.
2. `python -u source_pipeline.py >> logs\source_pipeline.log 2>&1`
   - Handles episodes taken from قناة التأصيل: yt-dlp download, compress, then ship in batches of 10 (`<folder> - التأصيل <k>.zip`).
   - Upload and ingest work the same way as job 1; the ingest marks the rows `local`.
   - The local copies are then deleted.
- `shipped.json` lists every zip already unpacked on the VPS. Both jobs skip those, and folders on that list are never rebuilt.

## If something was interrupted (PC restart, network drop, Claude Code closed)
- **Restart:** rerun jobs 1 and 2. A zip that was mid-upload is re-sent from the beginning; the leftover `.part` on the server is deleted first.
- **Uploaded but not ingested:** if a zip is in `/opt/abuhafs/archive_in` but its rows were not repointed, run on the VPS: `echo '{}' | venv/bin/python scripts/archive_ingest.py /opt/abuhafs/archive_in/<zip> <zip>`. Pass the renames or mark JSON if needed; see the script docstring.
- **Check:** `python status.py`, then look at `STATUS.md`.

## Decisions already made (2026-10-30)
- **Compression.** Videos are compressed before upload (x264 CRF 27 + AAC 64k mono, 65–80% smaller). The server copy is the compressed one.
- **Damaged audio.** 35 archive files have a damaged audio track (`broken_audio.txt`). They are not uploaded and stay on the PC.
  - 17 are re-downloaded from the source channel.
  - 18 are skipped in the schedule.
- **Already on our channel.** الإيمان لابن أبي شيبة and الصارم المسلول were deleted locally.
- **On hold.** `C:\videos\_review` (تراجم, الصحيفة والثلاثيات) is held until the speaker is confirmed.
- **Whisper SRT.** Postponed. Episodes publish with their raw titles.

## Storage moved to the VPS (2026-10-30, 20:30)
- **Why:** the tarweig account (`abuhafsi`) is capped at **10 GB** and filled up.
- **Migration:** all 10 zips were moved server to server (`scripts/migrate_cold_to_vps.sh`, log `logs/migrate_cold.log` on the VPS) and deleted from tarweig.
- **Feeding:** `local_feed.py` now moves `file://` episodes out of `/opt/abuhafs/archive`, so the archive shrinks as the lanes publish.
- **Old refs:** `cold://` refs (tarweig) are still supported, in case any are left.

## Source episodes are downloaded on the VPS (2026-10-31)
- yt-dlp works on the VPS again through Cloudflare WARP (proxy-only mode).
- Episodes from قناة التأصيل that are still 'source' (الترمذي 16–63, الرقاق 22–34, الاعتقاد 9) are downloaded by the lane itself when their turn comes.
- **Do not rerun `source_pipeline.py`.** Only `zip_and_upload.py` (the user's own archive) is still needed from this PC.
