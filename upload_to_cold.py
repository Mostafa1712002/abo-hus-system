"""Resumable bulk uploader for cold storage on the Linux server.

Mirrors the local source tree under REMOTE_ROOT on the SSH host.
Skips files that already exist remotely with the same byte size, so it is
safe to re-run after Ctrl+C / network drops.

Usage (from project venv):
    python upload_to_cold.py                  # full upload
    python upload_to_cold.py --series "NAME"  # only one top-level series folder
    python upload_to_cold.py --dry-run        # list what would be uploaded
"""

from __future__ import annotations

import argparse
import os
import socket
import stat
import sys
import time
from pathlib import Path

import paramiko
from tqdm import tqdm

# Ensure UTF-8 stdout/stderr on Windows so Arabic filenames print correctly
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

# ---------------------------------------------------------------- config
LOCAL_ROOT = Path(r"E:\فضيلة الشيخ أبي حفص\مرئيات")
REMOTE_ROOT = "/home/abuhafsi/videos_cold"
HOST = "213.239.209.167"
USER = "abuhafsi"
PORT = 22

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".flv", ".mov", ".m4v", ".webm",
              ".mpg", ".mpeg", ".ts", ".rmvb", ".rm", ".3gp", ".vob", ".ogv"}

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
                # race or already-exists
                pass


def collect_files(root: Path, only_series: str | None) -> list[Path]:
    if only_series:
        target = root / only_series
        if not target.exists():
            print(f"ERROR: series not found: {target}", file=sys.stderr)
            sys.exit(2)
        candidates = [p for p in target.rglob("*") if p.is_file()]
    else:
        candidates = [p for p in root.rglob("*") if p.is_file()]
    return [p for p in candidates if p.suffix.lower() in VIDEO_EXTS]


def upload_one(sftp: paramiko.SFTPClient, ssh: paramiko.SSHClient,
               local: Path, remote: str, local_size: int) -> str:
    """Upload one file. Returns one of: 'skipped', 'uploaded', 'failed'."""
    rsize = remote_size(sftp, remote)
    if rsize == local_size:
        return "skipped"
    # If a partial exists, paramiko's put will overwrite — fine.
    remote_mkdirs(sftp, os.path.dirname(remote))

    bar = tqdm(total=local_size, unit="B", unit_scale=True, unit_divisor=1024,
               desc=local.name[:40], leave=False)

    def cb(transferred, _total):
        bar.update(transferred - bar.n)

    try:
        sftp.put(str(local), remote, callback=cb, confirm=True)
    except Exception as e:
        bar.close()
        print(f"  ! upload failed: {e}", file=sys.stderr)
        return "failed"
    bar.close()
    return "uploaded"


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", help="Upload only this top-level series folder name")
    ap.add_argument("--dry-run", action="store_true", help="List actions without uploading")
    ap.add_argument("--max-retries", type=int, default=3)
    args = ap.parse_args()

    if not LOCAL_ROOT.exists():
        print(f"ERROR: local root not found: {LOCAL_ROOT}", file=sys.stderr)
        sys.exit(1)

    files = collect_files(LOCAL_ROOT, args.series)
    files.sort()
    total_bytes = sum(p.stat().st_size for p in files)
    print(f"Found {len(files)} video files. Total {total_bytes / 1e9:.2f} GB.")
    if not files:
        return

    if args.dry_run:
        for p in files[:20]:
            rel = p.relative_to(LOCAL_ROOT)
            print(f"  {rel}  ({p.stat().st_size / 1e6:.1f} MB)")
        if len(files) > 20:
            print(f"  ... and {len(files) - 20} more")
        return

    print(f"Connecting to {USER}@{HOST} ...")
    ssh, sftp = connect()
    remote_mkdirs(sftp, REMOTE_ROOT)

    uploaded = skipped = failed = 0
    bytes_done = 0
    t0 = time.time()
    try:
        for i, p in enumerate(files, 1):
            rel = p.relative_to(LOCAL_ROOT).as_posix()
            remote = f"{REMOTE_ROOT}/{rel}"
            sz = p.stat().st_size

            attempt = 0
            while True:
                attempt += 1
                try:
                    result = upload_one(sftp, ssh, p, remote, sz)
                    break
                except (paramiko.SSHException, socket.error, EOFError) as e:
                    print(f"  ! connection error ({e}); attempt {attempt}/{args.max_retries}",
                          file=sys.stderr)
                    if attempt >= args.max_retries:
                        result = "failed"
                        break
                    time.sleep(5 * attempt)
                    try:
                        sftp.close(); ssh.close()
                    except Exception:
                        pass
                    ssh, sftp = connect()

            tag = {"uploaded": "UP", "skipped": "SK", "failed": "FAIL"}[result]
            elapsed = max(time.time() - t0, 1e-3)
            mbps = (bytes_done / 1e6) / elapsed if bytes_done else 0
            print(f"[{i}/{len(files)}] {tag} {rel}  ({sz/1e6:.1f} MB) "
                  f"avg {mbps:.2f} MB/s")

            if result == "uploaded":
                uploaded += 1
                bytes_done += sz
            elif result == "skipped":
                skipped += 1
            else:
                failed += 1
    finally:
        try: sftp.close()
        except Exception: pass
        try: ssh.close()
        except Exception: pass

    elapsed = time.time() - t0
    print()
    print(f"=== Done in {elapsed/60:.1f} min ===")
    print(f"Uploaded: {uploaded}, Skipped: {skipped}, Failed: {failed}")
    print(f"Transferred: {bytes_done/1e9:.2f} GB "
          f"({(bytes_done/1e6)/max(elapsed,1):.2f} MB/s avg)")
    if failed:
        sys.exit(3)


if __name__ == "__main__":
    main()
