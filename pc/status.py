"""Write a full snapshot of the archive-publishing state to STATUS.md.

What is on cold storage (finished zips + in-flight .part), what each series
lane still has waiting on the VPS, what is left on this PC, and the last lines
of every job log. Run any time; safe to run while the jobs are running.
Usage: python status.py   (writes STATUS.md next to this file and prints it)
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
COLD = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-p", "22422", "abuhafsi@213.239.209.167"]
VPS = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "root@178.104.122.98"]
VPS_PY = r'''
import json, sqlite3
c = sqlite3.connect("/opt/abuhafs/youtube-auto-uploader/data/abuhafs.db")
rows = c.execute("""SELECT series, series_slug, download_status, process_status, COUNT(*), MIN(ord), MAX(ord)
  FROM catalog_videos WHERE series_slug IN (SELECT DISTINCT series_slug FROM catalog_videos
  WHERE download_status IN ('local','source','broken','skipped') OR download_quality='local')
  GROUP BY 1,2,3,4 ORDER BY 2,3,4""").fetchall()
nxt = c.execute("""SELECT series_slug, MIN(ord) FROM catalog_videos WHERE process_status='pending'
  AND download_status IN ('local','source','downloaded') GROUP BY 1""").fetchall()
print(json.dumps({"rows": rows, "next": nxt}, ensure_ascii=False))
'''


def sh(cmd, inp=None):
    try:
        r = subprocess.run(cmd, input=inp, capture_output=True, text=True, encoding="utf-8", timeout=120)
        return r.stdout.strip()
    except Exception as e:
        return f"(error: {e})"


def du(p):
    p = Path(p)
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def tail(p, n=6):
    p = Path(p)
    if not p.exists():
        return "(no log)"
    lines = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def main():
    out = [f"# Archive publishing status, {time.strftime('%Y-%m-%d %H:%M')} (PC time)", ""]
    out += ["## VPS archive (`/opt/abuhafs/archive`, unpacked episodes waiting for their lane)", "```",
            sh(VPS + ["df -h / | tail -1; echo; du -sh /opt/abuhafs/archive/* 2>/dev/null; echo; "
                      "echo inbox:; ls -l --time-style=+%m-%d_%H:%M /opt/abuhafs/archive_in/ | tail -n +2"]),
            "```", ""]
    out += ["## Lanes on the VPS (catalog_videos)",
            "| series | slug | download | process | n | ords |", "|---|---|---|---|---|---|"]
    try:
        db = json.loads(sh(VPS + ["python3 -"], VPS_PY).splitlines()[-1])
        for s, slug, ds, ps, n, a, b in db["rows"]:
            out.append(f"| {s} | {slug} | {ds} | {ps} | {n} | {a}–{b} |")
        out += ["", "Next pending ord per series: " +
                ", ".join(f"{slug}={o}" for slug, o in db["next"])]
    except Exception as e:
        out.append(f"(VPS query failed: {e})")
    out += ["", "## Left on this PC",
            *(f"- `{p}`: {du(p)/1e9:.2f} GB" for p in
              (r"C:\videos\data", r"C:\videos\source_dl", r"C:\videos\source_tc",
               r"C:\videos\_tc", r"C:\videos\_zips", r"C:\videos\_review")), ""]
    for name in ("zip_upload.log", "source_pipeline.log", "cleanup.log"):
        out += [f"## {name} (last lines)", "```", tail(HERE / "logs" / name), "```", ""]
    text = "\n".join(out)
    (HERE / "STATUS.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
