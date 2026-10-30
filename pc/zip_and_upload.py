"""Zip each series folder under C:\\videos\\data (STORE) and upload to cold storage.

Resumable: skips a series whose zip already exists remotely with the same size.
Remote: abuhafsi@213.239.209.167:22422 ~/videos_cold_zips/<series>.zip
"""
import subprocess, sys, zipfile, os, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
SRC = Path(r"C:\videos\data")
TMP = Path(r"C:\videos\_zips")
HOST = "abuhafsi@213.239.209.167"
PORT = "22422"
REMOTE = "videos_cold_zips"
SKIP = {"_duplicates", "الكتب مفرغة"}
VIDEO_EXT = {".avi", ".mp4", ".wmv", ".rmvb", ".rm", ".mkv", ".flv", ".mpg", ".mov", ".wma"}
SSH = ["ssh", "-o", "BatchMode=yes", "-p", PORT, HOST]


def remote_size(name):
    r = subprocess.run(SSH + [f"stat -c %s '{REMOTE}/{name}' 2>/dev/null || echo -1"],
                       capture_output=True, text=True, encoding="utf-8")
    try:
        return int(r.stdout.strip().splitlines()[-1])
    except Exception:
        return -1


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
        files = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXT)
        if not files:
            print(f"-- skip (no videos): {d.name}")
            continue
        name = f"{d.name}.zip"
        zp = TMP / name
        t0 = time.time()
        if not zp.exists():
            tmp = zp.with_suffix(".zip.tmp")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for f in files:
                    zf.write(f, arcname=f.name)
            tmp.rename(zp)
        size = zp.stat().st_size
        if remote_size(name) == size:
            print(f"== already uploaded: {name} ({size/1e9:.2f} GB)")
            zp.unlink()
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
            zp.unlink()
            dt = time.time() - t0
            print(f"OK {name} {size/1e9:.2f} GB in {dt/60:.1f} min ({size/dt/1e6:.1f} MB/s)")
        else:
            print(f"!! size mismatch {name}: local {size} remote {rs}")
    print("DONE")


if __name__ == "__main__":
    main()
