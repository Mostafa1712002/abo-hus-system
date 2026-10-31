"""Delete local copies of videos that are safely on cold storage.

A zip counts as safe only when it is complete on cold storage (no .part) and
passes a full CRC test there (zipfile.testzip). Source files are deleted only
when a member of a safe zip has their name and exact size (members are STORED,
so the size is the file size); archive originals are matched by folder + stem,
because the member is the re-encoded .mp4.

  source zips  "<folder> - التأصيل*.zip": the downloaded original
               (source_dl) and the compressed copy (source_tc) are deleted.
  archive zips "<folder>.zip": the compressed copy (_tc) is deleted; with
               --archive the original under C:\\videos\\data is deleted too
               (the cold copy of a re-encoded file is the smaller .mp4).

Verified zips are cached in verified_zips.json so each is CRC-tested once.
Usage: cleanup_uploaded.py [--archive] [--dry-run]
"""
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
HERE = Path(__file__).resolve().parent
CACHE = HERE / "verified_zips.json"
VIDEO_EXT = {".avi", ".mp4", ".wmv", ".rmvb", ".rm", ".mkv", ".flv", ".mpg", ".mov", ".wma"}
COLD = ["ssh", "-o", "BatchMode=yes", "-p", "22422", "abuhafsi@213.239.209.167"]
DATA, DL, STC, ATC = (Path(p) for p in (r"C:\videos\data", r"C:\videos\source_dl",
                                       r"C:\videos\source_tc", r"C:\videos\_tc"))
REMOTE_PY = r'''
import json, sys, zipfile, glob, os
skip = set(json.loads(sys.argv[1]))
out = {}
for p in sorted(glob.glob(os.path.expanduser("~/videos_cold_zips/*.zip"))):
    name = os.path.basename(p)
    if name in skip:
        continue
    try:
        with zipfile.ZipFile(p) as z:
            bad = z.testzip()
            out[name] = {"ok": bad is None, "members": {i.filename: i.file_size for i in z.infolist()}}
    except Exception as e:
        out[name] = {"ok": False, "error": str(e)}
print(json.dumps(out, ensure_ascii=False))
'''


def main():
    archive = "--archive" in sys.argv
    dry = "--dry-run" in sys.argv
    cache = json.load(open(CACHE, encoding="utf-8")) if CACHE.exists() else {}
    r = subprocess.run(COLD + ["python3 - " + "'" + json.dumps(list(cache), ensure_ascii=False) + "'"],
                       input=REMOTE_PY, capture_output=True, text=True, encoding="utf-8")
    fresh = json.loads(r.stdout.strip().splitlines()[-1])
    for name, info in fresh.items():
        print(("verified " if info["ok"] else "!! NOT OK ") + name + (" " + info.get("error", "") if not info["ok"] else ""))
        if info["ok"]:
            cache[name] = info["members"]
    if not dry:
        json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    freed = 0
    for name, members in cache.items():
        source = " - التأصيل" in name
        folder = name.split(" - التأصيل")[0] if source else name[:-4]
        for member, size in members.items():
            if source:
                # member is the compressed copy or the download itself: match on size
                shipped = [STC / folder / member, DL / folder / member]
                if not any(f.exists() and f.stat().st_size == size for f in shipped):
                    continue
                victims = {f for f in shipped if f.exists()}
            else:
                # member is "<stem>.mp4" re-encoded from the original "<stem>.<ext>" (the
                # temp copy is already gone, so no size to compare): the CRC-tested zip
                # holding that episode is the proof. Same folder + same stem = same episode.
                tc = ATC / folder / member
                victims = {tc} if tc.exists() and tc.stat().st_size == size else set()
                if archive and (DATA / folder).is_dir():
                    victims |= {f for f in (DATA / folder).iterdir()
                                if f.is_file() and f.stem == Path(member).stem
                                and f.suffix.lower() in VIDEO_EXT}
            for f in sorted(victims):
                freed += f.stat().st_size
                print(("would delete " if dry else "deleted ") + str(f))
                if not dry:
                    f.unlink()
    print(f"{'would free' if dry else 'freed'} {freed/1e9:.2f} GB")


if __name__ == "__main__":
    main()
