"""Compress, zip and upload each series folder under C:\\videos\\data to the VPS.

Videos are re-encoded to small H.264 .mp4 first (compress.py, ~70% smaller) to
spare the home uplink. One zip per folder goes to /opt/abuhafs/archive_in on the
VPS, where scripts/archive_ingest.py CRC-tests and unpacks it into
/opt/abuhafs/archive/<folder>/ and repoints the catalog rows (old .avi/.rmvb
member names -> the compressed .mp4). Only after that succeeds are the zip, the
compressed copies and the originals deleted from this PC.

Resumable: shipped.json lists the zips already ingested on the VPS, and those
folders are never rebuilt. (Until 2026-10-30 the zips went to the tarweig cold
storage, whose account is capped at 10 GB; they were moved to the VPS.)
"""
import json, shutil, subprocess, sys, zipfile, time
from pathlib import Path

from compress import compress

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
HERE = Path(__file__).resolve().parent
SRC = Path(r"C:\videos\data")
TMP = Path(r"C:\videos\_zips")
TC = Path(r"C:\videos\_tc")  # re-encoded copies, per folder
HOST = "root@178.104.122.98"
INBOX = "/opt/abuhafs/archive_in"
APP = "/opt/abuhafs/youtube-auto-uploader"
SKIP = {"_duplicates", "الكتب مفرغة"}
VIDEO_EXT = {".avi", ".mp4", ".wmv", ".rmvb", ".rm", ".mkv", ".flv", ".mpg", ".mov", ".wma"}
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4", HOST]
LEDGER = HERE / "shipped.json"
# damaged audio track (audio_scan.py): not shipped; the DB rows were moved to
# 'source' (re-downloaded from the source channel) or skipped
BROKEN = {l.strip() for l in open(HERE / "broken_audio.txt", encoding="utf-8") if l.strip()}


def shipped():
    return set(json.load(open(LEDGER, encoding="utf-8"))) if LEDGER.exists() else set()


def mark_shipped(name):
    s = shipped() | {name}
    json.dump(sorted(s), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def build_zip(d, files, name, zp):
    """Compress each video, zip the results; returns the member renames."""
    members, renames = [], []
    for f in files:
        out = TC / d.name / (f.stem + ".mp4")
        if compress(f, out):
            members.append(out)
            if out.name != f.name:
                renames.append({"old": f.name, "new": out.name})
        else:
            members.append(f)
    src_mb = sum(f.stat().st_size for f in files) / 1e6
    out_mb = sum(m.stat().st_size for m in members) / 1e6
    print(f"   compressed {d.name}: {src_mb:.0f} MB -> {out_mb:.0f} MB")
    tmp = zp.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for m in members:
            zf.write(m, arcname=m.name)
    tmp.rename(zp)
    return renames


def upload(zp, name):
    """scp to the inbox as .part, then rename. Returns True when the full file is there."""
    part = f"{INBOX}/{name}.part"
    # scp (SFTP mode) overwrites a leftover .part in place without truncating
    subprocess.run(SSH + [f"mkdir -p {INBOX}; rm -f '{part}'"])
    for wait in (60, 180, 600, 0):  # ride out short network drops
        r = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30",
                            "-o", "ServerAliveCountMax=4", str(zp), f"{HOST}:{part}"])
        if r.returncode == 0:
            break
        print(f"   scp rc={r.returncode}, retrying in {wait}s")
        time.sleep(wait)
    if r.returncode != 0:
        return False
    out = subprocess.run(SSH + [f"mv '{part}' '{INBOX}/{name}' && stat -c %s '{INBOX}/{name}'"],
                         capture_output=True, text=True, encoding="utf-8").stdout.strip()
    return out.endswith(str(zp.stat().st_size))


def ingest(name, payload):
    res = subprocess.run(SSH + [f"cd {APP} && venv/bin/python scripts/archive_ingest.py "
                                f"'{INBOX}/{name}' '{name}'"],
                         input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                         text=True, encoding="utf-8")
    line = (res.stdout.strip().splitlines() or ["{}"])[-1]
    print(f"   ingest: {line} {res.stderr.strip()[-200:]}")
    try:
        return json.loads(line).get("ok") is True
    except ValueError:
        return False


def main():
    TMP.mkdir(exist_ok=True)
    # lane order: weekly series first, then the rest
    PRIORITY = ["اعتقاد لأهل السنة للإسماعيلي", "كتاب الرقاق", "كتاب القدر", "سنن الترمذي", "المدخل",
                "المتجر الرابح", "شرح كتاب فضائل الصحابة من صحيبح البخاري", "شرح كتاب طريق الوصول",
                "الفوائد", "فتح المغيث", "شرح جزءأبي عمروالداني"]
    rank = {n: i for i, n in enumerate(PRIORITY)}
    folders = sorted((d for d in SRC.iterdir() if d.is_dir() and d.name not in SKIP),
                     key=lambda d: (rank.get(d.name, 99), d.name))
    for d in folders:
        name = f"{d.name}.zip"
        if name in shipped():
            print(f"== already on the VPS: {name}")
            continue
        files = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXT
                       and f"{d.name}/{f.name}" not in BROKEN)
        if not files:
            print(f"-- skip (no videos): {d.name}")
            continue
        zp = TMP / name
        renames_file = TMP / (name + ".renames.json")
        t0 = time.time()
        if not zp.exists() or not renames_file.exists():
            renames = build_zip(d, files, name, zp)
            json.dump(renames, open(renames_file, "w", encoding="utf-8"), ensure_ascii=False)
        renames = [{"old": r["old"], "new": r["new"]} for r in json.load(open(renames_file, encoding="utf-8"))]
        size = zp.stat().st_size
        print(f">> uploading {name}: {len(files)} files, {size/1e9:.2f} GB", flush=True)
        if not upload(zp, name):
            print(f"!! upload failed for {name} — rerun to resume")
            continue
        dt = time.time() - t0
        print(f"OK {name} {size/1e9:.2f} GB in {dt/60:.1f} min ({size/dt/1e6:.2f} MB/s)")
        if not ingest(name, {"renames": renames}):
            print(f"!! ingest failed for {name} — local files kept")
            continue
        mark_shipped(name)
        # safely unpacked on the VPS: drop the zip, the compressed copies and the originals
        zp.unlink()
        renames_file.unlink()
        shutil.rmtree(TC / d.name, ignore_errors=True)
        for f in files:
            f.unlink(missing_ok=True)
        print(f"   deleted {len(files)} originals from {d}")
    print("DONE")


if __name__ == "__main__":
    main()
