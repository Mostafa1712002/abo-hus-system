"""Download source-channel episodes on this PC (residential IP — loader.to and
YouTube block the VPS), zip per series, upload to cold storage, then flip the
catalog rows from 'source' to 'local' so the daily lanes pick them up.

Series are handled in lane-need order; resumable (skips files already
downloaded, zips already on cold storage).
"""
import json
import subprocess
import sys
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
HERE = Path(__file__).resolve().parent
ROWS = json.load(open(HERE / "source_rows.json", encoding="utf-8"))
DL = Path(r"C:\videos\source_dl")
TMP = Path(r"C:\videos\_zips")
HOST, PORT, REMOTE = "abuhafsi@213.239.209.167", "22422", "videos_cold_zips"
COLD = ["ssh", "-o", "BatchMode=yes", "-p", PORT, HOST]
VPS = ["ssh", "-o", "BatchMode=yes", "root@178.104.122.98"]
SERIES = [  # slug, folder, file prefix  (order = which lane needs it first)
    ("almdkhl-al-alsnn-alkbr-llamam-albyhky", "المدخل", "المدخل"),
    ("local-ktab-alrkak", "كتاب الرقاق", "الرقاق"),
    ("local-ktab-alkdr", "كتاب القدر", "القدر"),
    ("snn-alamam-altrmthy", "سنن الترمذي", "الترمذي"),
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
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-part",
                        "-f", "bv*[height<=720]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]",
                        "--merge-output-format", "mp4", "-o", str(out),
                        f"https://www.youtube.com/watch?v={yt_id}"])
    return r.returncode == 0 and out.exists()


def main():
    TMP.mkdir(exist_ok=True)
    for slug, folder, prefix in SERIES:
        rows = [r for r in ROWS if r["series_slug"] == slug]
        d = DL / folder
        d.mkdir(parents=True, exist_ok=True)
        done = []
        for r in rows:
            out = d / fname(prefix, r["ord"])
            if download(r["youtube_id"], out):
                done.append((r, out))
                print(f"  dl ok  {out.name}")
            else:
                print(f"  !! dl failed {r['youtube_id']} {out.name}")
        zip_name = f"{folder} - التأصيل.zip"
        zp = TMP / zip_name
        if not zp.exists():
            tmp = zp.with_suffix(".zip.tmp")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for _, out in done:
                    zf.write(out, arcname=out.name)
            tmp.rename(zp)
        size = zp.stat().st_size
        if remote_size(zip_name) != size:
            print(f">> uploading {zip_name}: {len(done)} files, {size/1e9:.2f} GB")
            rc = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-P", PORT, str(zp),
                                 f"{HOST}:{REMOTE}/{zip_name}.part"]).returncode
            if rc != 0:
                print(f"!! scp failed {zip_name} — rerun to resume")
                continue
            subprocess.run(COLD + [f"mv '{REMOTE}/{zip_name}.part' '{REMOTE}/{zip_name}'"], check=True)
            if remote_size(zip_name) != size:
                print(f"!! size mismatch {zip_name}")
                continue
        zp.unlink()
        items = [{"youtube_id": r["youtube_id"], "ref": f"cold://{zip_name}/{out.name}",
                  "size": out.stat().st_size} for r, out in done]
        res = subprocess.run(VPS + ["cd /opt/abuhafs/youtube-auto-uploader && "
                                    "venv/bin/python scripts/local_mark_cold.py"],
                             input=json.dumps(items, ensure_ascii=False), capture_output=True,
                             text=True, encoding="utf-8")
        print(f"OK {zip_name}: {res.stdout.strip()} {res.stderr.strip()[-200:]}")
    print("DONE")


if __name__ == "__main__":
    main()
