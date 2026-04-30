# Cold Storage Module

Fetches video files from a remote SSH server on demand, so the small VPS
running the upload pipeline doesn't have to host the entire archive.

Two modes are supported, picked via `cold_storage.type` in `config.json`:

| Mode       | Layout on cold server                              | Pros                                                   | Cons                                          |
| ---------- | -------------------------------------------------- | ------------------------------------------------------ | --------------------------------------------- |
| `raw`      | One file per video, mirrored under `videos_cold/`  | No local extraction step. Fetch one video, use it.     | Lots of small remote files; messy directory. |
| `zipped`   | One `<series>.zip` per series under `videos_cold_zips/` | Cleaner directory, one shared cache per series, simple bulk-upload story. | Need temp space for the zip; whole zip downloads at once. |

`zipped` is the current production default. `raw` is kept around as a
legacy / fallback mode and the module supports both.

---

## Architecture (zipped mode)

```
   +-----------------------------+
   | Local Windows box (E:\)     |   sources of truth: 1738 video files
   |  E:\...\مرئيات\<series>\... |   in 53 series folders.
   +-------------+---------------+
                 | upload_to_cold_zipped.py
                 |   - zip <series>.zip   (STORE, no compression — videos
                 |                         don't compress; we just bundle)
                 |   - SFTP/paramiko upload
                 |   - delete local zip on success
                 v
   +-----------------------------+
   |  Cold-storage server        |   abuhafsi@213.239.209.167
   |  videos_cold_zips/          |       <series>.zip
   |       <series>.zip          |       <series>.zip
   |       ...                   |       ...
   +-------------+---------------+
                 | SSH (scp/rsync) — per-series zip pulled lazily
                 v
   +-----------------------------+
   |  Processing host            |   7erfa-system.com
   |  /opt/abuhafs/cold_temp_zips/|       <series>.zip   (cached)
   |  /opt/abuhafs/videos_workspace/|     <series>/<file>.wmv  (extracted, transient)
   |                             |
   |  Pipeline:                  |
   |   1. ensure_local_from_zip()|       - fetch zip if not cached
   |   2. extract one video      |       - one file at a time
   |   3. process (YT/FB/IG/TG)  |
   |   4. delete extracted file  |
   |   5. when ALL series videos |
   |      are completed: drop zip|
   +-----------------------------+
```

## Architecture (raw mode — legacy)

```
            +-----------------------------+
            |  Cold-storage server        |   abuhafsi@213.239.209.167
            |  (abuhafs.info)             |   /home/abuhafsi/videos_cold/
            |                             |       شرح الرسالة/
            |  Holds the FULL backlog     |       شرح كتاب التوحيد/
            |  (TBs of .wmv/.mp4 files)   |       ...
            +-------------+---------------+
                          | SSH (scp/rsync)  — one file per fetch
                          v
            +-----------------------------+
            |  Processing host            |   7erfa-system.com
            |  (small VPS, ~30-60 GB)     |
            |                             |
            |  Pipeline pulls 1 video     |   /opt/abuhafs/videos_workspace/
            |  at a time, processes it,   |       (only ~1 video lives here at a time)
            |  uploads to YT/FB/IG/TG,    |
            |  then deletes the local     |
            |  copy.                      |
            +-----------------------------+
```

The pipeline keeps `pending.json` and `item.original_path` as the canonical
record, but only the **local** path is touched for ffmpeg / thumbnail / FB-upload
operations. If the local path doesn't exist, the cold-storage module is asked
to fetch it.

---

## Path mapping

`ColdStorage.remote_path_for(original_path)` accepts both Windows and Linux
input paths and resolves them to the absolute path on the cold-storage server.

The strategy: scan for one of these markers in the path

```
videos_workspace, videos_cold, videos_input, مرئيات
```

and treat everything **after** the last marker as the series-relative path.

| Input                                                                | Output                                          |
| -------------------------------------------------------------------- | ----------------------------------------------- |
| `E:\فضيلة الشيخ أبي حفص\مرئيات\شرح الرسالة\1.wmv`                    | `/home/abuhafsi/videos_cold/شرح الرسالة/1.wmv`  |
| `/opt/abuhafs/videos_workspace/شرح الرسالة/1.wmv`                    | `/home/abuhafsi/videos_cold/شرح الرسالة/1.wmv`  |
| `/random/path/seriesX/2.mp4` (no marker)                             | `/home/abuhafsi/videos_cold/seriesX/2.mp4` (uses last 2 components) |

The same logic produces `local_path_for(...)` for the destination of fetches:
`local_workspace + relative-portion`.

---

## Pipeline integration

In `_process_one` the pipeline branches on `cold.type`:

```python
cold = ColdStorage.from_config(cfg)
original_local = item.original_path
fetched_from_cold = False
if cold.enabled and not cold.is_local(item.original_path):
    try:
        if (cold.type or "raw").lower() == "zipped":
            video_filename = Path(item.original_path).name
            local_path = cold.ensure_local_from_zip(item.series, video_filename)
        else:
            local_path = cold.ensure_local(item.original_path)
        original_local = str(local_path)
        fetched_from_cold = True
    except Exception as e:
        logger.warning(f"cold-storage fetch failed: {e}")  # graceful fallback
```

All file-read operations downstream (`make_thumbnail`, `cut_all_shorts`,
`_publish_full_video_to_fb`, `_publish_full_video_to_telegram`) use
`original_local` instead of `item.original_path`.

After the tracker is updated to `completed`:

```python
if fetched_from_cold and original_local != item.original_path:
    cold.cleanup(original_local)            # raw OR zipped: drop the per-video copy
if (cold.type or "raw").lower() == "zipped":
    cold.cleanup_zip_if_series_done(item.series)  # zipped only: drop the cached zip
                                                  # iff every video in the series
                                                  # is in a terminal state (DB).
```

`cleanup_zip_if_series_done` checks the DB (with `pending.json` fallback) to
make sure no other videos in the same series are still pending. If they are,
the cached zip is left in place — better to use 5–15 GB of disk than to
re-download the same zip 30 times in a row.

If the cold-storage fetch fails, `original_local` stays equal to the original
path. Each downstream operation already guards itself with
`Path(original_local).exists()`, so the pipeline degrades gracefully: SRT
processing, AI metadata generation, YouTube metadata update, and Telegram
text posts still run; only the file-bound steps (thumbnail, shorts, FB full
video upload) are skipped.

---

## Disk space considerations

### Raw mode

- The local workspace (`cfg.paths['videos_input']`) only ever holds the
  current in-flight video plus its derived shorts.
- Maximum simultaneous footprint: one full video (~1-3 GB for a long .wmv
  lecture) + a handful of 9:16 cut shorts (~30-50 MB each). Safe on a 30 GB
  VPS root volume; comfortable on 60 GB.

### Zipped mode

- Two pieces of state live on the processing server:
  1. `cold_storage.local_temp_zips/<series>.zip` — the cached zip for the
     **currently active** series. Sized like the series itself (range:
     ~0.4 GB for `أسوان رحلة` up to ~19 GB for `سبل السلام`).
  2. `paths.videos_input/<series>/<file>.wmv` — the **single** extracted
     video the pipeline is currently working on.
- Worst-case footprint = `max(series_zip_size) + max(single_video_size)`.
  For our backlog the largest series is `سبل السلام` at ~19 GB; pick a
  processing host with at least ~30 GB free for safety.
- After every video is finished, `cold.cleanup_zip_if_series_done(series)`
  drops the zip + extracted dir if no more videos from that series remain
  pending. The daily `cleanup.py` cron does the same as a safety net.

---

## Bulk-uploading videos to the cold server

### Zipped (recommended)

`upload_to_cold_zipped.py` zips each series locally, uploads the zip via
SFTP/paramiko, then deletes the local zip. It's resumable: it skips series
whose zip is already on the server with the expected size.

```powershell
# zip + upload all series
& venv\Scripts\python.exe upload_to_cold_zipped.py

# zip + upload only series in Wave 3
& venv\Scripts\python.exe upload_to_cold_zipped.py --wave 3

# zip + upload one series
& venv\Scripts\python.exe upload_to_cold_zipped.py --series "أسوان رحلة"

# plan only (no zip, no upload)
& venv\Scripts\python.exe upload_to_cold_zipped.py --wave 3 --dry-run
```

The zip is built with `zipfile.ZIP_STORED` (no compression). Videos don't
compress meaningfully and STORE is much faster.

### Raw (legacy)

`upload_to_cold.py` mirrors the Windows tree directly under
`/home/abuhafsi/videos_cold/`. Still useful if you want to inspect or stream
single files without unpacking a zip.

```bash
# Linux / WSL / Git-Bash with rsync (resumable, recommended):
rsync -avh --progress \
    "/e/فضيلة الشيخ أبي حفص/مرئيات/" \
    abuhafsi@213.239.209.167:/home/abuhafsi/videos_cold/

# Windows PowerShell with scp:
scp -r "E:\فضيلة الشيخ أبي حفص\مرئيات\*" abuhafsi@213.239.209.167:/home/abuhafsi/videos_cold/

# Or the bundled paramiko-based script:
python upload_to_cold.py --series "<NAME>"
```

---

## Configuration

Add to `config.json` on the **server** (leave `enabled: false` on local Windows
so videos still come from `E:\`):

```json
"cold_storage": {
  "enabled": true,
  "type": "zipped",
  "ssh_host": "abuhafsi@213.239.209.167",
  "ssh_remote_root": "/home/abuhafsi/videos_cold",
  "ssh_remote_zips_root": "/home/abuhafsi/videos_cold_zips",
  "local_temp_zips": "/opt/abuhafs/cold_temp_zips",
  "ssh_port": 22,
  "ssh_key": "/home/abuhafs/.ssh/id_ed25519",
  "fetch_method": "scp",
  "base_url": "https://abuhafs.info/cold-videos"
}
```

| Field                     | Meaning                                                                |
| ------------------------- | ---------------------------------------------------------------------- |
| `enabled`                 | Master switch. When `false`, the module is a no-op.                    |
| `type`                    | `"zipped"` (one zip per series) or `"raw"` (one file per video).       |
| `ssh_host`                | `user@host` — must accept passwordless key-based auth.                 |
| `ssh_remote_root`         | Absolute path on the cold server for raw-mode files.                   |
| `ssh_remote_zips_root`    | Absolute path on the cold server for zipped-mode `<series>.zip` files. |
| `local_temp_zips`         | Local directory used to cache downloaded zips. Auto-created.           |
| `ssh_port`                | Default 22.                                                            |
| `ssh_key`                 | Optional path to a private key. If empty, falls back to system default.|
| `fetch_method`            | `scp` (default) or `rsync` (resumable, recommended for big files).     |
| `base_url`                | Reserved for an HTTP fallback fetch path; not yet wired in.            |

### Switching between modes

Just flip `cold_storage.type` between `"zipped"` and `"raw"` in `config.json`
and restart the service. The two modes are independent — they use separate
remote roots (`ssh_remote_root` vs `ssh_remote_zips_root`) so existing
content under either tree is preserved when toggling.

---

## Wave planner fallback

`src/wave_planner.py` exposes:

- `get_series_in_wave_with_cold_fallback(cfg, wave)` — tries the local
  `videos_input` first; if empty, lists series directories on the cold server.
- `find_videos_in_series_with_cold_fallback(cfg, series_name)` — same idea,
  but returns absolute paths (local **or** remote).

When the wave planner returns remote paths, the pipeline must call
`ColdStorage.ensure_local(path)` before processing each one. `_process_one`
already does this transparently because it inspects `is_local(item.original_path)`.

> **Note:** wave classification (`classify_wave`) uses folder mtime, which
> we can't easily get over ssh, so the cold-fallback path skips wave
> filtering and returns *all* remote series. The caller is expected to
> filter as needed (or just process them all).

---

## Failure modes and recovery

| Failure                          | Behavior                                                                            |
| -------------------------------- | ----------------------------------------------------------------------------------- |
| `cold_storage` section missing   | `from_config()` returns a no-op instance; pipeline behaves as before.               |
| SSH unreachable / wrong host     | `ensure_local*` raises `FileNotFoundError`; pipeline logs a warning and continues.  |
| Remote file/zip missing          | Same as above — pipeline skips file-bound steps but still updates YT metadata.      |
| Partial download (network drop)  | Zero-byte / `.part` file is deleted on failure. Re-run reattempts the fetch.        |
| `scp` not on PATH                | `ensure_local*` raises `RuntimeError("'scp' not found")`; install OpenSSH client.   |
| Disk full mid-fetch              | scp returns nonzero; partial file removed; `FileNotFoundError` raised.              |
| Cleanup fails (file in use)      | Logged as warning, ignored. Next `cleanup.py` cron pass should pick it up.          |
| Zipped: video missing inside zip | `ensure_local_from_zip` raises `FileNotFoundError`; pipeline skips file-bound steps.|
| Zipped: zip cache survives crash | A `.part` file is left next to the cache; the next fetch deletes it and retries.   |

---

## Caveats

- **Windows scp:** if you ever flip `enabled: true` in the local Windows
  config, you need OpenSSH client on PATH (`Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0`)
  and a working `~/.ssh/known_hosts` entry for `213.239.209.167`. Otherwise scp
  will fail interactively. The recommended setup is **only** the server has
  `enabled: true`.
- **Arabic filenames in scp:** scp parses the `host:` part itself but passes
  the rest to a remote shell. We single-quote the remote path inside the
  ssh-spec, so paths with spaces and Arabic characters round-trip safely.
- **Resumable fetches:** for >1 GB videos, prefer `fetch_method: "rsync"` —
  it uses `--partial --inplace` and survives flaky links.
