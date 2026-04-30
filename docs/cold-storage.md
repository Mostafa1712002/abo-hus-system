# Cold Storage Module

Fetches video files from a remote SSH server on demand, so the small VPS
running the upload pipeline doesn't have to host the entire archive.

---

## Architecture

```
            +-----------------------------+
            |  Cold-storage server        |   abuhafsi@213.239.209.167
            |  (abuhafs.info)             |   /home/abuhafsi/videos_cold/
            |                             |       شرح الرسالة/
            |  Holds the FULL backlog     |       شرح كتاب التوحيد/
            |  (TBs of .wmv/.mp4 files)   |       ...
            +-------------+---------------+
                          | SSH (scp/rsync)
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

In `_process_one`:

```python
cold = ColdStorage.from_config(cfg)
original_local = item.original_path
fetched_from_cold = False
if cold.enabled and not cold.is_local(item.original_path):
    try:
        local_path = cold.ensure_local(item.original_path)
        original_local = str(local_path)
        fetched_from_cold = True
    except Exception as e:
        logger.warning(f"cold-storage fetch failed: {e}")  # graceful fallback
```

All file-read operations downstream (`make_thumbnail`, `cut_all_shorts`,
`_publish_full_video_to_fb`, `_publish_full_video_to_telegram`) use
`original_local` instead of `item.original_path`.

After the tracker is updated to `completed`, we run cleanup:

```python
if fetched_from_cold and original_local != item.original_path:
    cold.cleanup(original_local)
```

This deletes the file we just downloaded **and** prunes empty parent directories
(the series folder), so the workspace stays bounded to roughly one video at a
time.

If the cold-storage fetch fails, `original_local` stays equal to the original
path. Each downstream operation already guards itself with
`Path(original_local).exists()`, so the pipeline degrades gracefully: SRT
processing, AI metadata generation, YouTube metadata update, and Telegram
text posts still run; only the file-bound steps (thumbnail, shorts, FB full
video upload) are skipped.

---

## Disk space considerations

- The local workspace (`cfg.paths['videos_input']`) only ever holds the
  current in-flight video plus its derived shorts.
- Maximum simultaneous footprint: one full video (~1-3 GB for a long .wmv
  lecture) + a handful of 9:16 cut shorts (~30-50 MB each). Safe on a 30 GB
  VPS root volume; comfortable on 60 GB.
- After completion, `cold.cleanup(...)` removes the fetched file. The
  `cleanup.py` cron also wipes the `output/shorts/<base>/` subdir per the
  existing `keep_final_outputs` policy.

---

## Bulk-uploading videos to the cold server

From a local PC that has the videos (e.g. the user's Windows box with the
`E:\` drive):

```bash
# Linux / WSL / Git-Bash:
rsync -avh --progress \
    "/e/فضيلة الشيخ أبي حفص/مرئيات/" \
    abuhafsi@213.239.209.167:/home/abuhafsi/videos_cold/

# Windows PowerShell with scp:
scp -r "E:\فضيلة الشيخ أبي حفص\مرئيات\*" abuhafsi@213.239.209.167:/home/abuhafsi/videos_cold/
```

For incremental sync (recommended), use rsync with `--ignore-existing`:

```bash
rsync -avh --ignore-existing --progress \
    "/e/فضيلة الشيخ أبي حفص/مرئيات/" \
    abuhafsi@213.239.209.167:/home/abuhafsi/videos_cold/
```

---

## Configuration

Add to `config.json` on the **server** (leave `enabled: false` on local Windows
so videos still come from `E:\`):

```json
"cold_storage": {
  "enabled": true,
  "ssh_host": "abuhafsi@213.239.209.167",
  "ssh_remote_root": "/home/abuhafsi/videos_cold",
  "ssh_port": 22,
  "ssh_key": "/home/abuhafs/.ssh/id_ed25519",
  "fetch_method": "scp",
  "base_url": "https://abuhafs.info/cold-videos"
}
```

| Field             | Meaning                                                                |
| ----------------- | ---------------------------------------------------------------------- |
| `enabled`         | Master switch. When `false`, the module is a no-op.                    |
| `ssh_host`        | `user@host` — must accept passwordless key-based auth.                 |
| `ssh_remote_root` | Absolute path on the cold server containing the series subdirectories. |
| `ssh_port`        | Default 22.                                                            |
| `ssh_key`         | Optional path to a private key. If empty, falls back to system default.|
| `fetch_method`    | `scp` (default) or `rsync` (resumable, recommended for big files).     |
| `base_url`        | Reserved for an HTTP fallback fetch path; not yet wired in.            |

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
| SSH unreachable / wrong host     | `ensure_local` raises `FileNotFoundError`; pipeline logs a warning and continues.   |
| Remote file missing              | Same as above — pipeline skips file-bound steps but still updates YT metadata.      |
| Partial download (network drop)  | Empty/zero-byte local file is deleted on failure. Re-run reattempts the fetch.      |
| `scp` not on PATH                | `ensure_local` raises `RuntimeError("'scp' not found")`; install OpenSSH client.    |
| Disk full mid-fetch              | scp returns nonzero; partial file removed; `FileNotFoundError` raised.              |
| Cleanup fails (file in use)      | Logged as warning, ignored. Next `cleanup.py` cron pass should pick it up.          |

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
