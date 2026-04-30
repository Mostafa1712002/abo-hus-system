"""Resumable, per-series ZIP uploader for cold storage.

For each series folder under LOCAL_ROOT, this script:

    1. Computes the total bytes of all video files inside the series.
    2. Skips the series if a `<series>.zip` already exists on the cold server
       with a size within tolerance (resumable).
    3. Otherwise, builds `<series>.zip` in TEMP using STORE compression
       (no compression — videos don't compress, this is just bundling).
    4. Uploads the zip via SFTP/paramiko to
       `<ssh_host>:/home/abuhafsi/videos_cold_zips/<series>.zip`.
    5. Deletes the local zip after a successful upload.

The zip creation is atomic: it writes to `<series>.zip.tmp` first and renames
on success. The remote upload writes to `<series>.zip.part` and renames once
the SFTP transfer completes — so a network drop never leaves a half-zip
masquerading as a finished one on the cold server.

Usage:
    python upload_to_cold_zipped.py                    # all series
    python upload_to_cold_zipped.py --series "NAME"    # one series
    python upload_to_cold_zipped.py --wave 3           # wave 3 only
    python upload_to_cold_zipped.py --dry-run          # plan, don't upload
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import paramiko
from tqdm import tqdm

# Ensure UTF-8 stdout/stderr on Windows so Arabic filenames print correctly.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

# Make `from src...` imports work when run from project root.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.wave_planner import classify_wave  # noqa: E402

# ---------------------------------------------------------------- config
LOCAL_ROOT = Path(r"E:\فضيلة الشيخ أبي حفص\مرئيات")
REMOTE_ZIPS_ROOT = "/home/abuhafsi/videos_cold_zips"
HOST = "213.239.209.167"
USER = "abuhafsi"
PORT = 22

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".flv", ".mov", ".m4v", ".webm",
              ".mpg", ".mpeg", ".ts", ".rmvb", ".rm", ".3gp", ".vob", ".ogv"}

# Skip a remote zip whose size is within this fraction of the expected size.
# Zip-with-STORE adds ~30 bytes per file overhead + a central directory, so
# remote_size will be SLIGHTLY larger than the sum of inputs. Allow 1% slack.
REMOTE_SIZE_TOLERANCE = 0.02

logger = logging.getLogger("upload_to_cold_zipped")


# ---------------------------------------------------------------- helpers
def connect() -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, timeout=30, banner_timeout=30)
    sftp = ssh.open_sftp()
    sftp.get_channel().settimeout(60)
    return ssh, sftp


def remote_size(sftp: paramiko.SFTPClient, path: str) -> int | None:
    try:
        return sftp.stat(path).st_size
    except IOError:
        return None


def remote_mkdirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    """Recursively create remote directories (idempotent)."""
    parts = remote_dir.strip("/").split("/")
    cur = ""
    for p in parts:
        cur = cur + "/" + p
        try:
            sftp.stat(cur)
        except IOError:
            try:
                sftp.mkdir(cur)
            except IOError:
                pass


def collect_videos(series_dir: Path) -> list[Path]:
    """Return all video files under `series_dir` (recursive).

    Prefers a transcoded `.mp4` over a same-stem `.rmvb`/`.rm` source: if both
    sit next to each other in the same folder, only the `.mp4` ships in the
    zip (the zip is for upload-ready files). The original `.rmvb` stays on
    disk untouched.
    """
    if not series_dir.exists():
        return []
    files = [
        p for p in series_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    ]
    # Build (parent_dir, stem_lower) -> exts map so we can drop .rmvb/.rm
    # when an .mp4 sibling exists.
    by_stem: dict[tuple[str, str], set[str]] = {}
    for p in files:
        key = (str(p.parent), p.stem.lower())
        by_stem.setdefault(key, set()).add(p.suffix.lower())
    filtered: list[Path] = []
    for p in files:
        ext = p.suffix.lower()
        if ext in (".rmvb", ".rm"):
            siblings = by_stem.get((str(p.parent), p.stem.lower()), set())
            if ".mp4" in siblings:
                # An .mp4 transcode of this exact file exists; skip the source.
                continue
        filtered.append(p)
    return sorted(filtered)


def discover_series(root: Path, only_series: str | None,
                    only_wave: int | None) -> list[Path]:
    if not root.exists():
        return []
    if only_series:
        target = root / only_series
        if not target.exists():
            print(f"ERROR: series not found: {target}", file=sys.stderr)
            sys.exit(2)
        return [target]
    series = [p for p in root.iterdir() if p.is_dir()]
    if only_wave is not None:
        kept: list[Path] = []
        for p in series:
            try:
                if classify_wave(p) == only_wave:
                    kept.append(p)
            except OSError:
                continue
        series = kept
    series.sort(key=lambda p: p.name)
    return series


def make_zip_atomic(series_dir: Path, videos: list[Path],
                    zip_path: Path, total_bytes: int) -> int:
    """Write a STORE-mode zip of `videos` to `zip_path`.

    Writes to `zip_path.tmp` first, then renames to `zip_path` once finished.
    Returns the final zip size in bytes. Raises on any error.
    """
    tmp = zip_path.with_suffix(zip_path.suffix + ".tmp")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

    bar = tqdm(total=total_bytes, unit="B", unit_scale=True, unit_divisor=1024,
               desc=f"zip {series_dir.name[:30]}", leave=False)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as zf:
            for v in videos:
                # Path inside the zip is relative to the series directory, so
                # extracting `<series>.zip` gives back the original layout.
                arcname = v.relative_to(series_dir).as_posix()
                size = v.stat().st_size
                # Stream-copy so memory stays small even for >2 GB files.
                with v.open("rb") as src, zf.open(
                        zipfile.ZipInfo(arcname), "w", force_zip64=True) as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        bar.update(len(chunk))
                # tqdm sometimes drifts a few bytes if read returned short;
                # don't stress about exact frame ticks, the bar is informational.
                _ = size
    except Exception:
        bar.close()
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    bar.close()

    # Atomic rename on the same filesystem.
    if zip_path.exists():
        try:
            zip_path.unlink()
        except OSError:
            pass
    os.replace(tmp, zip_path)
    return zip_path.stat().st_size


def upload_zip(sftp: paramiko.SFTPClient, ssh: paramiko.SSHClient,
               local_zip: Path, remote_zip: str) -> str:
    """Upload `local_zip` to `remote_zip` via SFTP.

    Returns one of: 'uploaded' / 'failed'. Writes to `<remote>.part` and
    renames on success so a partial upload never masquerades as a finished
    zip on the cold server.
    """
    size = local_zip.stat().st_size
    remote_part = remote_zip + ".part"
    remote_mkdirs(sftp, os.path.dirname(remote_zip))

    bar = tqdm(total=size, unit="B", unit_scale=True, unit_divisor=1024,
               desc=f"up {local_zip.name[:30]}", leave=False)

    def cb(transferred, _total):
        bar.update(transferred - bar.n)

    try:
        sftp.put(str(local_zip), remote_part, callback=cb, confirm=True)
    except Exception as e:
        bar.close()
        print(f"  ! upload failed: {e}", file=sys.stderr)
        # Best-effort cleanup of the .part on the remote side.
        try:
            sftp.remove(remote_part)
        except IOError:
            pass
        return "failed"
    bar.close()

    # Rename .part -> final name on the remote.
    try:
        # If a stale final exists (from a previous interrupted run with a
        # different size) overwrite it.
        try:
            sftp.remove(remote_zip)
        except IOError:
            pass
        sftp.posix_rename(remote_part, remote_zip)  # atomic on POSIX
    except (AttributeError, IOError):
        # Older OpenSSH or non-POSIX server: fall back to plain rename.
        try:
            sftp.rename(remote_part, remote_zip)
        except IOError as e:
            print(f"  ! rename failed: {e}", file=sys.stderr)
            return "failed"
    return "uploaded"


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", help="Zip+upload only this series folder")
    ap.add_argument("--wave", type=int, choices=(1, 2, 3),
                    help="Restrict to series in this wave")
    ap.add_argument("--dry-run", action="store_true",
                    help="List actions without zipping/uploading")
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--temp-dir", default=None,
                    help="Override the temp dir used for zip staging")
    ap.add_argument("--keep-local-zip", action="store_true",
                    help="Don't delete the local zip after a successful upload")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not LOCAL_ROOT.exists():
        print(f"ERROR: local root not found: {LOCAL_ROOT}", file=sys.stderr)
        sys.exit(1)

    series_dirs = discover_series(LOCAL_ROOT, args.series, args.wave)
    if not series_dirs:
        print("No series matched.")
        return

    plans: list[tuple[Path, list[Path], int]] = []
    for sd in series_dirs:
        videos = collect_videos(sd)
        total = sum(v.stat().st_size for v in videos)
        plans.append((sd, videos, total))

    grand_total = sum(t for _, _, t in plans)
    print(f"Series matched: {len(plans)}")
    print(f"Total source bytes: {grand_total/1e9:.2f} GB across "
          f"{sum(len(v) for _, v, _ in plans)} videos.")

    if args.dry_run:
        for sd, videos, total in plans:
            try:
                w = classify_wave(sd)
            except OSError:
                w = -1
            print(f"  [W{w}] {sd.name}  ({len(videos)} files, "
                  f"{total/1e9:.2f} GB)")
        return

    if grand_total == 0:
        print("All matched series are empty.")
        return

    print(f"Connecting to {USER}@{HOST} ...")
    ssh, sftp = connect()
    remote_mkdirs(sftp, REMOTE_ZIPS_ROOT)

    temp_root = Path(args.temp_dir) if args.temp_dir else Path(
        tempfile.gettempdir())
    temp_root.mkdir(parents=True, exist_ok=True)

    uploaded = skipped = failed = 0
    bytes_uploaded = 0
    t0 = time.time()
    try:
        for i, (sd, videos, total_bytes) in enumerate(plans, 1):
            name = sd.name
            print()
            print(f"=== [{i}/{len(plans)}] {name} "
                  f"({len(videos)} files, {total_bytes/1e9:.2f} GB) ===")
            if not videos or total_bytes == 0:
                print("  (no video files — skip)")
                continue

            remote_zip = f"{REMOTE_ZIPS_ROOT}/{name}.zip"
            rsize = remote_size(sftp, remote_zip)
            if rsize is not None:
                # The zip will be slightly larger than total_bytes (zip
                # overhead). Treat anything within tolerance as "already
                # uploaded".
                expected_low = int(total_bytes * (1.0 - REMOTE_SIZE_TOLERANCE))
                expected_high = int((total_bytes + 1024 * len(videos))
                                    * (1.0 + REMOTE_SIZE_TOLERANCE))
                if expected_low <= rsize <= expected_high:
                    print(f"  SK already on cold server "
                          f"({rsize/1e9:.2f} GB)")
                    skipped += 1
                    continue
                print(f"  remote zip size {rsize} doesn't match expected "
                      f"~{total_bytes} — re-uploading.")

            local_zip = temp_root / f"{name}.zip"
            zt0 = time.time()
            try:
                final_size = make_zip_atomic(sd, videos, local_zip, total_bytes)
            except Exception as e:
                print(f"  ! zip creation failed: {e}", file=sys.stderr)
                failed += 1
                continue
            zt = time.time() - zt0
            print(f"  zipped: {final_size/1e9:.2f} GB in {zt:.1f}s "
                  f"({(final_size/1e6)/max(zt, 1e-3):.1f} MB/s)")

            attempt = 0
            ut0 = time.time()
            while True:
                attempt += 1
                try:
                    result = upload_zip(sftp, ssh, local_zip, remote_zip)
                    break
                except (paramiko.SSHException, socket.error, EOFError) as e:
                    print(f"  ! connection error ({e}); attempt "
                          f"{attempt}/{args.max_retries}", file=sys.stderr)
                    if attempt >= args.max_retries:
                        result = "failed"
                        break
                    time.sleep(5 * attempt)
                    try:
                        sftp.close(); ssh.close()
                    except Exception:
                        pass
                    ssh, sftp = connect()
            ut = time.time() - ut0

            if result == "uploaded":
                uploaded += 1
                bytes_uploaded += final_size
                mbps = (final_size / 1e6) / max(ut, 1e-3)
                print(f"  UP uploaded {final_size/1e9:.2f} GB in {ut:.1f}s "
                      f"({mbps:.2f} MB/s)")
                if not args.keep_local_zip:
                    try:
                        local_zip.unlink()
                    except OSError as e:
                        print(f"  warn: couldn't delete local zip: {e}",
                              file=sys.stderr)
            else:
                failed += 1
                print(f"  FAIL upload of {name}")
                # Leave the local zip behind so a re-run can pick it up.
    finally:
        try: sftp.close()
        except Exception: pass
        try: ssh.close()
        except Exception: pass

    elapsed = time.time() - t0
    print()
    print(f"=== Done in {elapsed/60:.1f} min ===")
    print(f"Uploaded: {uploaded}, Skipped: {skipped}, Failed: {failed}")
    if bytes_uploaded:
        print(f"Transferred: {bytes_uploaded/1e9:.2f} GB "
              f"({(bytes_uploaded/1e6)/max(elapsed, 1):.2f} MB/s avg)")
    if failed:
        sys.exit(3)


if __name__ == "__main__":
    main()
