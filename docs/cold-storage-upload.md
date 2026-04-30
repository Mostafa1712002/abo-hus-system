# Cold-Storage Bulk Upload (one-time)

Upload ~220 GB of video material from the local Windows PC to the Linux SSH
server so the rest of the pipeline can read from `videos_cold/`.

- **Source:** `E:\فضيلة الشيخ أبي حفص\مرئيات\`
- **Destination:** `abuhafsi@213.239.209.167:/home/abuhafsi/videos_cold/`
- **Tooling:** Python + paramiko (rsync is not installed on Windows; using a
  resumable Python script keeps everything self-contained inside this project's
  venv).

The remote path mirrors the local tree: every series folder under `مرئيات/`
becomes a folder directly under `videos_cold/`, with nested sub-folders
preserved (e.g. `videos_cold/أسوان رحلة/السد العالي/السد.avi`).

## Architecture

```
E:\...\مرئيات\<series>\...\*.mp4   ──►   /home/abuhafsi/videos_cold/<series>/...
        │                                          │
        └─── upload_to_cold.py (paramiko SFTP) ────┘
                ▲
                └── upload_to_cold.bat (Windows launcher, prompts before run)
```

`upload_to_cold.py`:

- Walks `LOCAL_ROOT` recursively, keeps only known video extensions
  (`.mp4 .mkv .avi .wmv .flv .mov .m4v .webm .mpg .mpeg .ts`).
- Connects once via SFTP, then for each file:
  1. `sftp.stat(remote)` — if the remote file exists with the **same byte
     size**, the file is skipped (`SK`).
  2. Otherwise creates the remote parent dirs and `sftp.put`s the file with
     a tqdm progress bar.
- On `SSHException` / socket / EOF errors it reconnects and retries
  (default 3 attempts per file, with backoff).
- Prints a per-file line: `[i/N] UP|SK|FAIL <path>  (size MB)  avg X MB/s`.

## Pre-flight checks

```bash
ssh abuhafsi@213.239.209.167 "df -h /home/abuhafsi"
# Need at least 250 GB free. As of 2026-04-30: 1.1 TB available.
```

```powershell
# In project venv
python -c "import paramiko; print(paramiko.__version__)"   # 4.0.0
python -c "import tqdm; print(tqdm.__version__)"           # 4.67.x
```

## Test upload (one small series)

The smallest non-empty folder is `كلمة العلامة المحدث أبى حفص فى تونس`
(1 file, ~94 MB). `أسوان رحلة` is a slightly larger but more representative
test (7 video files in nested sub-folders, ~390 MB).

```powershell
$env:PYTHONIOENCODING = "utf-8"
& venv\Scripts\python.exe upload_to_cold.py --series "أسوان رحلة"
```

Verify:

```bash
ssh abuhafsi@213.239.209.167 \
  "find '/home/abuhafsi/videos_cold/أسوان رحلة' -type f | head; \
   du -sh '/home/abuhafsi/videos_cold/أسوان رحلة'"
```

Re-running should print only `SK` lines (resumability sanity check).

## Full bulk upload

```bat
:: Double-click or from cmd:
upload_to_cold.bat
```

or directly:

```powershell
$env:PYTHONIOENCODING = "utf-8"
& venv\Scripts\python.exe upload_to_cold.py
```

CLI flags:

| Flag                 | Description                                       |
| -------------------- | ------------------------------------------------- |
| `--series "NAME"`    | Upload only one top-level series folder           |
| `--dry-run`          | List the first 20 files that would be uploaded   |
| `--max-retries N`    | Retries per file on connection error (default 3) |

## Resumable behaviour

- Ctrl+C is safe at any time — no rollback needed.
- A partially-uploaded file is detected by size mismatch and re-uploaded
  from scratch (paramiko's `sftp.put` overwrites). This is intentional —
  partial-resume mid-file would require server-side append support and adds
  failure modes; full re-upload of the one in-flight file is fast enough.
- Already-completed files (size matches) are skipped, so re-running after a
  drop only transfers what's new.

## Verify after upload

```bash
ssh abuhafsi@213.239.209.167 '
  echo "Top-level series:"
  ls /home/abuhafsi/videos_cold | wc -l
  echo
  echo "Total file count:"
  find /home/abuhafsi/videos_cold -type f | wc -l
  echo
  echo "Total size:"
  du -sh /home/abuhafsi/videos_cold
  echo
  echo "Tree (depth 2):"
  find /home/abuhafsi/videos_cold -maxdepth 2 -type d | head -30
'
```

Local-side tally for cross-check:

```powershell
Get-ChildItem -LiteralPath "E:\فضيلة الشيخ أبي حفص\مرئيات" -Recurse -File |
  Where-Object { $_.Extension -in '.mp4','.mkv','.avi','.wmv','.flv','.mov','.m4v','.webm' } |
  Measure-Object Length -Sum
```

The two counts (file totals, byte totals) should match.

## Cleanup

Once every video has been processed by the rest of the pipeline:

- **Option A (default, recommended for now):** keep `videos_cold/` as the
  authoritative archive on the server. Cheap at ~220 GB.
- **Option B (reclaim space):** delete `videos_cold/` once you have verified
  every series has its expected outputs in production:
  ```bash
  rm -rf /home/abuhafsi/videos_cold
  ```
  Do this only after confirming the pipeline no longer references it.

## Operational notes

- Arabic filenames need UTF-8 stdout. The script reconfigures
  `sys.stdout`/`stderr` to UTF-8 on Windows; the `.bat` also sets
  `PYTHONIOENCODING=utf-8` and `chcp 65001`.
- SSH auth uses the user's existing key (loaded via
  `ssh.load_system_host_keys()` + paramiko agent lookup). If pubkey auth is
  not set up, configure it once: `ssh-copy-id abuhafsi@213.239.209.167` (or
  drop a key into `~/.ssh/authorized_keys` on the server). The script does
  not prompt for a password.
- Remote directory creation is recursive and idempotent
  (`remote_mkdirs`), so partial trees are handled cleanly.
