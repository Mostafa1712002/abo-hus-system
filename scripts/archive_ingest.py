"""Unpack an archive zip onto the VPS and point the catalog rows at the files.

Since 2026-10-30 the local-archive videos live on this box (the tarweig
cold-storage account has a 10 GB quota). A zip uploaded to ARCHIVE_IN is
CRC-tested, extracted to ARCHIVE/<zip stem>/<member>, and the rows are
repointed from 'cold://<zip>/<member>' to 'file://<path>'. local_feed.py then
moves one file at a time into the workspace, so the archive shrinks as
episodes get published. The zip is deleted only after everything checks out.

stdin (optional JSON):
  {"renames": [{"old", "new"}], "mark": [{"youtube_id", "member"}]}
  renames: rows still on 'cold://<zip>/<old>' (pre-compression names) -> new member
  mark:    'source' rows (episodes taken from the source channel) -> 'local'
Prints one JSON line: {"ok": bool, ...}.
Usage: archive_ingest.py <zip path> [<zip name as referenced in the DB>]
"""
import json
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "abuhafs.db"
ARCHIVE = Path("/opt/abuhafs/archive")


def main():
    zp = Path(sys.argv[1])
    zip_name = sys.argv[2] if len(sys.argv) > 2 else zp.name
    extra = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
    # zipfile, not `unzip -t`: unzip exits non-zero on harmless "mismatching local
    # filename" warnings for the Arabic (UTF-8) member names written by Python
    try:
        with zipfile.ZipFile(zp) as z:
            bad_member = z.testzip()
    except zipfile.BadZipFile as e:
        bad_member = f"unreadable zip: {e}"
    if bad_member is not None:
        print(json.dumps({"ok": False, "error": f"CRC test failed: {bad_member}", "zip": zip_name},
                         ensure_ascii=False))
        return 1
    dest = ARCHIVE / Path(zip_name).stem
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zp) as z:
        members = {i.filename: i.file_size for i in z.infolist()}
        z.extractall(dest)
    bad = [m for m, size in members.items()
           if not (dest / m).exists() or (dest / m).stat().st_size != size]
    if bad:
        print(json.dumps({"ok": False, "error": "size mismatch after extract", "files": bad}, ensure_ascii=False))
        return 1
    con = sqlite3.connect(DB)
    ref = lambda m: f"file://{dest / m}"
    repointed = 0
    for m in members:  # rows already renamed to the compressed member name
        repointed += con.execute("UPDATE catalog_videos SET mp4_path=?, updated_at=CURRENT_TIMESTAMP "
                                 "WHERE mp4_path=?", (ref(m), f"cold://{zip_name}/{m}")).rowcount
    for r in extra.get("renames", []):
        repointed += con.execute("UPDATE catalog_videos SET mp4_path=?, updated_at=CURRENT_TIMESTAMP "
                                 "WHERE mp4_path=?", (ref(r["new"]), f"cold://{zip_name}/{r['old']}")).rowcount
    marked = 0
    for it in extra.get("mark", []):
        marked += con.execute(
            "UPDATE catalog_videos SET download_status='local', mp4_path=?, download_bytes=?, "
            "download_error='', updated_at=CURRENT_TIMESTAMP WHERE youtube_id=? AND download_status='source'",
            (ref(it["member"]), members.get(it["member"], 0), it["youtube_id"])).rowcount
    con.commit()
    zp.unlink()
    free = shutil.disk_usage(str(ARCHIVE)).free / 1e9
    print(json.dumps({"ok": True, "zip": zip_name, "files": len(members), "repointed": repointed,
                      "marked": marked, "free_gb": round(free, 1)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
