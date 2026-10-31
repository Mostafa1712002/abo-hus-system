"""Pick the next local-archive video for a lane and pull it from cold storage.

A lane is an ordered list of series slugs; the first slug that still has local
rows is the active one (so a lane advances to its next series automatically).
If that slug already has a downloaded+pending row it is reused (e.g. a retry);
otherwise the lowest-ord 'local' row is streamed out of its zip on cold storage
(`unzip -p` over ssh — no full zip on this box) into the catalog workspace.

Prints the slug to process on stdout (empty = lane finished).
Usage: local_feed.py <slug> [<slug> ...]
"""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "abuhafs.db"
sys.path.insert(0, str(ROOT / "scripts"))
from catalog_download_worker import loader_download  # noqa: E402
WORKSPACE = Path("/opt/abuhafs/catalog_workspace")
COLD = ["ssh", "-o", "BatchMode=yes", "-p", "22422", "abuhafsi@213.239.209.167"]
COLD_ZIPS = "videos_cold_zips"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    for slug in sys.argv[1:]:
        ready = con.execute(
            "SELECT id FROM catalog_videos WHERE series_slug=? AND download_status='downloaded' "
            "AND process_status='pending' AND mp4_path NOT LIKE 'cold://%' LIMIT 1", (slug,)).fetchone()
        if ready:
            print(slug)
            return
        row = con.execute(
            "SELECT * FROM catalog_videos WHERE series_slug=? AND download_status IN ('local','source','broken') "
            "AND process_status='pending' ORDER BY ord, id LIMIT 1", (slug,)).fetchone()
        if not row:
            continue
        if row["download_status"] == "broken":
            # local file with a damaged audio track and no copy anywhere else —
            # hold the lane here (keeps episode order) until someone supplies a fix
            log(f"feed {slug} ord={row['ord']}: waiting — {row['download_error']}")
            return
        dest_dir = WORKSPACE / slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        if row["download_status"] == "source":
            # episode we don't have locally — pull it from the source channel via loader.to
            dest = dest_dir / f"{row['youtube_id']}.mp4"
            log(f"feed {slug} ord={row['ord']}: loader.to {row['youtube_id']}")
            try:
                info = loader_download(row["youtube_id"], dest)
            except Exception as e:
                dest.unlink(missing_ok=True)
                con.execute("UPDATE catalog_videos SET download_error=?, updated_at=CURRENT_TIMESTAMP "
                            "WHERE id=?", (f"loader.to: {e}"[:300], row["id"]))
                con.commit()
                log(f"loader.to failed: {e} — will retry next run")
                return
            con.execute(
                "UPDATE catalog_videos SET download_status='downloaded', mp4_path=?, "
                "download_bytes=?, downloaded_at=CURRENT_TIMESTAMP, download_quality='720', "
                "download_error='', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(dest), info["bytes"], row["id"]))
            con.commit()
            print(slug)
            return
        if row["mp4_path"].startswith("file://"):
            # unpacked on this box by archive_ingest.py: move it, so the archive shrinks
            src = Path(row["mp4_path"][len("file://"):])
            dest = dest_dir / src.name
            log(f"feed {slug} ord={row['ord']}: {src}")
            if not src.exists():
                log("feed failed — archive file missing")
                return
            shutil.move(str(src), str(dest))
        else:
            zip_name, member = row["mp4_path"][len("cold://"):].split("/", 1)
            dest = dest_dir / member
            tmp = dest.with_name(dest.name + ".part")
            # quote for the remote shell; Arabic names have spaces
            remote = f"unzip -p '{COLD_ZIPS}/{zip_name}' '{member}'"
            log(f"feed {slug} ord={row['ord']}: {zip_name} :: {member}")
            with open(tmp, "wb") as fh:
                r = subprocess.run(COLD + [remote], stdout=fh)
            if r.returncode != 0 or tmp.stat().st_size < 1024 * 1024:
                tmp.unlink(missing_ok=True)
                log(f"feed failed (rc={r.returncode}) — zip not on cold storage yet?")
                return
            tmp.rename(dest)
        con.execute(
            "UPDATE catalog_videos SET download_status='downloaded', mp4_path=?, "
            "download_bytes=?, downloaded_at=CURRENT_TIMESTAMP, download_quality='local', "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(dest), dest.stat().st_size, row["id"]))
        con.commit()
        print(slug)
        return


if __name__ == "__main__":
    main()
