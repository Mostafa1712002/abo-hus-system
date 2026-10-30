"""Download source-channel episodes on this PC (residential IP — loader.to and
YouTube block the VPS), zip them, upload to cold storage, then flip the
catalog rows from 'source' to 'local' so the daily lanes pick them up.

Each download is re-encoded to small H.264 (compress.py) before zipping to
spare the home uplink. Episodes ship in batches of BATCH per series, taken
round-robin across series in lane-need order, so every lane gets its next
episodes early instead of waiting for a whole series. Resumable: skips files
already downloaded/compressed and batches already on cold storage.
"""
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from compress import compress

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
HERE = Path(__file__).resolve().parent
ROWS = json.load(open(HERE / "source_rows.json", encoding="utf-8"))
DL = Path(r"C:\videos\source_dl")
TC = Path(r"C:\videos\source_tc")  # compressed copies that go into the zip
TMP = Path(r"C:\videos\_zips")
HOST, PORT, REMOTE = "abuhafsi@213.239.209.167", "22422", "videos_cold_zips"
COLD = ["ssh", "-o", "BatchMode=yes", "-p", PORT, HOST]
VPS = ["ssh", "-o", "BatchMode=yes", "root@178.104.122.98"]
BATCH = 10
SERIES = [  # slug, folder, file prefix  (order = which lane needs it first)
    ("snn-alamam-altrmthy", "سنن الترمذي", "الترمذي"),
    ("local-ktab-alrkak", "كتاب الرقاق", "الرقاق"),
    ("local-ktab-alkdr", "كتاب القدر", "القدر"),
    ("almdkhl-al-alsnn-alkbr-llamam-albyhky", "المدخل", "المدخل"),
    ("local-shrh-aatkad-ahl-alsna", "اعتقاد لأهل السنة للإسماعيلي", "اعتقاد أهل السنة"),
    ("local-almtgr-alrabh", "المتجر الرابح", "المتجر الرابح"),
]


def fname(prefix, ord_):
    if ord_ < 0:  # منهج الترمذي intro series stored as ord n-100
        return f"منهج الترمذي {ord_ + 100}.mp4"
    return f"{prefix} {ord_}.mp4"


def remote_size(name):
    r = subprocess.run(COLD + [f"stat -c %s '{REMOTE}/{name}' 2>/dev/null || echo -1"],
                       capture_output=True, text=True, encoding="utf-8")
    try:
        return int(r.stdout.strip().splitlines()[-1])
    except Exception:
        return -1


def download(yt_id, out):
    if out.exists() and out.stat().st_size > 1_000_000:
        return True
    for wait in (30, 90, 300, 0):  # YouTube throws sporadic 403s on media URLs
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-part",
                            "-f", "bv*[height<=720]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]",
                            "--merge-output-format", "mp4", "-o", str(out),
                            f"https://www.youtube.com/watch?v={yt_id}"])
        if r.returncode == 0 and out.exists():
            return True
        time.sleep(wait)
    return False


def ship(folder, prefix, zip_name, rows):
    """Download + compress + zip + upload one batch, then mark its rows local.
    Returns the rows that could not be downloaded."""
    zp = TMP / zip_name
    if not zp.exists() and remote_size(zip_name) > 0:
        print(f"== already on cold storage: {zip_name}")
        return []
    done, failed = [], []
    for r in rows:
        out = DL / folder / fname(prefix, r["ord"])
        out.parent.mkdir(parents=True, exist_ok=True)
        if download(r["youtube_id"], out):
            small = TC / folder / out.name
            done.append((r, small if compress(out, small) else out))
            print(f"  dl ok  {out.name}")
        else:
            print(f"  !! dl failed {r['youtube_id']} {out.name}")
            failed.append(r)
    if not done:
        return failed
    if not zp.exists():
        tmp = zp.with_suffix(".zip.tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for _, out in done:
                zf.write(out, arcname=out.name)
        tmp.rename(zp)
    size = zp.stat().st_size
    print(f">> uploading {zip_name}: {len(done)} files, {size/1e6:.0f} MB")
    rc = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-P", PORT, str(zp),
                         f"{HOST}:{REMOTE}/{zip_name}.part"]).returncode
    if rc != 0:
        print(f"!! scp failed {zip_name} — rerun to resume")
        return failed
    subprocess.run(COLD + [f"mv '{REMOTE}/{zip_name}.part' '{REMOTE}/{zip_name}'"], check=True)
    if remote_size(zip_name) != size:
        print(f"!! size mismatch {zip_name}")
        return failed
    items = [{"youtube_id": r["youtube_id"], "ref": f"cold://{zip_name}/{out.name}",
              "size": out.stat().st_size} for r, out in done]
    res = subprocess.run(VPS + ["cd /opt/abuhafs/youtube-auto-uploader && "
                                "venv/bin/python scripts/local_mark_cold.py"],
                         input=json.dumps(items, ensure_ascii=False), capture_output=True,
                         text=True, encoding="utf-8")
    print(f"OK {zip_name}: {res.stdout.strip()} {res.stderr.strip()[-200:]}")
    if res.returncode == 0:
        zp.unlink()
    return failed


def main():
    TMP.mkdir(exist_ok=True)
    queues = []
    for slug, folder, prefix in SERIES:
        rows = sorted((r for r in ROWS if r["series_slug"] == slug), key=lambda r: r["ord"])
        if slug == "almdkhl-al-alsnn-alkbr-llamam-albyhky":  # 40-60 went up as "المدخل - التأصيل.zip"
            rows = [r for r in rows if r["ord"] > 60]
        batches = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
        queues.append([(folder, prefix, f"{folder} - التأصيل {k + 1}.zip", b)
                       for k, b in enumerate(batches)])
    retry = {}
    for rnd in range(max(len(q) for q in queues)):
        for q in queues:
            if rnd < len(q):
                folder, prefix, _, _ = q[rnd]
                retry.setdefault((folder, prefix), []).extend(ship(*q[rnd]))
    for (folder, prefix), rows in retry.items():  # one last go for the failures
        if rows:
            left = ship(folder, prefix, f"{folder} - التأصيل اعادة.zip", rows)
            for r in left:
                print(f"!! still missing: {folder} ord={r['ord']} {r['youtube_id']}")
    print("DONE")


if __name__ == "__main__":
    main()
