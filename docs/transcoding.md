# RMVB / RM Transcoding

YouTube does not accept `.rmvb`/`.rm` (RealMedia) for upload, and the rest of
the pipeline (ffmpeg thumbnail extraction, shorts cutter) is built around mp4-
compatible containers. Some old series in the backlog ship RealMedia only —
notably `شرح الرسالة` (23 × `.rmvb` + 1 nested `.rmvb` in
`ترجمةالشافعي/`).

The `transcode_rmvb.py` helper converts every `.rmvb`/`.rm` in a series folder
into an H.264 + AAC `.mp4` next to it, then the regular cold-storage uploader
picks the new `.mp4` files up automatically.

## What it does

For each `.rmvb` / `.rm` under `E:\فضيلة الشيخ أبي حفص\مرئيات\<series>\`:

* Skip if a sibling `.mp4` with the same stem already exists (idempotent).
* Run ffmpeg with this pipeline:

  ```text
  ffmpeg -y -i input.rmvb \
    -vf "scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos, \
         pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1" \
    -c:v libx264 -preset medium -crf 22 -profile:v high \
    -pix_fmt yuv420p -movflags +faststart \
    -c:a aac -b:a 128k -ar 44100 \
    -f mp4 output.mp4.tmp
  ```

* Atomic write: `name.mp4.tmp` → `os.replace` to `name.mp4` on success.
* `.rmvb` source is **kept** by default (use `--delete-rmvb` to remove it
  once the `.mp4` is verified non-empty).

The 320×240 source is upscaled to 1280×720 with lanczos and letter-/pillar-
boxed to preserve aspect. CRF 22 gives reasonable quality; output sizes for
شرح الرسالة hover around 60–80 % of source despite the upscale, since H.264
+ AAC is dramatically more efficient than rv40 + cook.

## Usage

```powershell
# Dry-run: list files, totals, planned outputs.
& venv\Scripts\python.exe transcode_rmvb.py --series "شرح الرسالة" --dry-run

# Actually transcode (~30-60 min for 23 files at medium preset on a typical
# CPU; ffmpeg is single-process, files run sequentially).
& venv\Scripts\python.exe transcode_rmvb.py --series "شرح الرسالة"

# Or just double-click:
transcode_all.bat

# Single-file (handy for spot-checks):
& venv\Scripts\python.exe transcode_rmvb.py --file "E:\...\الرسالة 7.rmvb"

# Across all series:
& venv\Scripts\python.exe transcode_rmvb.py --all
```

## After transcoding — uploading to cold storage

`upload_to_cold_zipped.py` now prefers the `.mp4` over the `.rmvb` source: if
both exist with the same stem in the same folder, only the `.mp4` is included
in the per-series zip. The `.rmvb` original stays on disk; if you want to
reclaim space, pass `--delete-rmvb` to the transcoder.

```powershell
# Run in parallel with an in-flight Wave 3 upload — they share bandwidth but
# CPU (transcoding bottleneck) and SFTP (upload bottleneck) are independent.
& venv\Scripts\python.exe upload_to_cold_zipped.py --series "شرح الرسالة"
```

## Caveats

* **320×240 → 720p**: the source is SD; lanczos upscale looks acceptable for
  a fixed-camera lecture but won't magically add detail.
* **Time**: ~30–60 minutes for the full شرح الرسالة series on a typical
  desktop CPU. The script processes files sequentially (single ffmpeg
  process at a time); good for keeping CPU pinned at ~one core without
  thrashing.
* **ffmpeg location**: tools resolve from `PATH` first, then fall back to the
  Gyan.FFmpeg winget install path. If neither works, install via
  `winget install Gyan.FFmpeg` or set up `ffmpeg` on `PATH`.

## Extension whitelists

The following extensions are now first-class video extensions across the
project (`upload_to_cold.py`, `upload_to_cold_zipped.py`, `src/wave_planner.py`,
`src/cleanup.py`, `src/cold_storage.py`, `main.py find_videos`):

```
.mp4 .mkv .avi .wmv .flv .mov .m4v .webm
.mpg .mpeg .ts .rmvb .rm .3gp .vob .ogv
```
