"""Zip each series folder under C:\\videos\\data (STORE) and upload to cold storage.

Videos are re-encoded to small H.264 .mp4 first (compress.py, ~70% smaller) to
spare the home uplink; once a zip is on cold storage the catalog rows are
repointed to the new member names on the VPS (scripts/local_rename_members.py).

Resumable: skips a series whose zip already exists remotely with the same size.
Remote: abuhafsi@213.239.209.167:22422 ~/videos_cold_zips/<series>.zip
"""
import json, shutil, subprocess, sys, zipfile, time
from pathlib import Path

from compress import compress

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
SRC = Path(r"C:\videos\data")
TMP = Path(r"C:\videos\_zips")
TC = Path(r"C:\videos\_tc")  # re-encoded copies, per folder
HOST = "abuhafsi@213.239.209.167"
PORT = "22422"
REMOTE = "videos_cold_zips"
SKIP = {"_duplicates", "الكتب مفرغة"}
VIDEO_EXT = {".avi", ".mp4", ".wmv", ".rmvb", ".rm", ".mkv", ".flv", ".mpg", ".mov", ".wma"}
SSH = ["ssh", "-o", "BatchMode=yes", "-p", PORT, HOST]
VPS = ["ssh", "-o", "BatchMode=yes", "root@178.104.122.98"]
# damaged audio track (audio_scan.py): not shipped; the DB rows were moved to
# 'source' (re-downloaded from the source channel) or 'broken' (lane waits)
BROKEN = {l.strip() for l in open(Path(__file__).with_name("broken_audio.txt"), encoding="utf-8") if l.strip()}


def remote_size(name):
    r = subprocess.run(SSH + [f"stat -c %s '{REMOTE}/{name}' 2>/dev/null || echo -1"],
                       capture_output=True, text=True, encoding="utf-8")
    try:
        return int(r.stdout.strip().splitlines()[-1])
    except Exception:
        return -1


def build_zip(d, files, name, zp):
    """Compress each video, zip the results; returns the member renames."""
    members, renames = [], []
    for f in files:
        out = TC / d.name / (f.stem + ".mp4")
        if compress(f, out):
            members.append(out)
            if out.name != f.name:
                renames.append({"zip": name, "old": f.name, "new": out.name})
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


def finish(d, name, zp, renames_file):
    """Zip is safely on cold storage: repoint the DB rows, drop local temp copies."""
    renames = json.load(open(renames_file, encoding="utf-8"))
    res = subprocess.run(VPS + ["cd /opt/abuhafs/youtube-auto-uploader && "
                                "venv/bin/python scripts/local_rename_members.py"],
                         input=json.dumps(renames, ensure_ascii=False), capture_output=True,
                         text=True, encoding="utf-8")
    print(f"   {res.stdout.strip()} {res.stderr.strip()[-200:]}")
    if res.returncode != 0:
        print(f"!! repoint failed for {name} — keeping {renames_file.name}")
        return
    zp.unlink()
    renames_file.unlink()
    shutil.rmtree(TC / d.name, ignore_errors=True)


def main():
    TMP.mkdir(exist_ok=True)
    subprocess.run(SSH + [f"mkdir -p {REMOTE}"], check=True)
    # lane order: weekly series first (smallest first so lanes start sooner), then the rest
    PRIORITY = ["اعتقاد لأهل السنة للإسماعيلي", "كتاب الرقاق", "كتاب القدر", "سنن الترمذي", "المدخل",
                "المتجر الرابح", "شرح كتاب فضائل الصحابة من صحيبح البخاري", "شرح كتاب طريق الوصول",
                "الفوائد", "فتح المغيث", "شرح جزءأبي عمروالداني"]
    rank = {n: i for i, n in enumerate(PRIORITY)}
    folders = sorted((d for d in SRC.iterdir() if d.is_dir() and d.name not in SKIP),
                     key=lambda d: (rank.get(d.name, 99), d.name))
    for d in folders:
        files = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXT
                       and f"{d.name}/{f.name}" not in BROKEN)
        if not files:
            print(f"-- skip (no videos): {d.name}")
            continue
        name = f"{d.name}.zip"
        zp = TMP / name
        renames_file = TMP / (name + ".renames.json")
        t0 = time.time()
        if not zp.exists() or not renames_file.exists():
            renames = build_zip(d, files, name, zp)
            json.dump(renames, open(renames_file, "w", encoding="utf-8"), ensure_ascii=False)
        size = zp.stat().st_size
        if remote_size(name) == size:
            print(f"== already uploaded: {name} ({size/1e9:.2f} GB)")
            finish(d, name, zp, renames_file)
            continue
        print(f">> uploading {name}: {len(files)} files, {size/1e9:.2f} GB", flush=True)
        r = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-P", PORT, str(zp),
                            f"{HOST}:{REMOTE}/{name}.part"])
        if r.returncode != 0:
            print(f"!! scp failed for {name} (rc={r.returncode}) — rerun to resume")
            continue
        subprocess.run(SSH + [f"mv '{REMOTE}/{name}.part' '{REMOTE}/{name}'"], check=True)
        rs = remote_size(name)
        if rs == size:
            dt = time.time() - t0
            print(f"OK {name} {size/1e9:.2f} GB in {dt/60:.1f} min ({size/dt/1e6:.1f} MB/s)")
            finish(d, name, zp, renames_file)
        else:
            print(f"!! size mismatch {name}: local {size} remote {rs}")
    print("DONE")


if __name__ == "__main__":
    main()
