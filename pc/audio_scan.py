"""For every archive video: container duration vs. how many mp3/cook frames fail to decode."""
import re, subprocess, sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8")
EXT = {".avi", ".mp4", ".wmv", ".rmvb", ".rm", ".mkv", ".flv", ".mpg", ".mov"}
files = [f for d in Path(r"C:\videos\data").iterdir() if d.is_dir() and d.name not in ("_duplicates", "الكتب مفرغة")
         for f in d.iterdir() if f.suffix.lower() in EXT]

def scan(f):
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
                         capture_output=True, text=True).stdout.strip()
    r = subprocess.run(["ffmpeg", "-nostats", "-max_error_rate", "1", "-i", str(f), "-vn", "-map", "0:a:0", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    errs = len(re.findall(r"Header missing|Invalid data|error", r.stderr))
    t = re.findall(r"time=(\d+):(\d+):([\d.]+)", r.stderr)
    adur = int(t[-1][0]) * 3600 + int(t[-1][1]) * 60 + float(t[-1][2]) if t else 0
    try:
        d = float(dur)
    except ValueError:
        d = 0.0
    return f, d, adur, errs

bad = 0
with ThreadPoolExecutor(8) as ex:
    for f, dur, adur, errs in ex.map(scan, files):
        ok = errs < 50 and dur > 0 and abs(adur - dur) / dur < 0.03
        bad += not ok
        print(("ok  " if ok else "BAD ") + f"{f.parent.name}/{f.name}\tvideo={dur:.0f}s audio={adur:.0f}s errs={errs}")
print(f"TOTAL {len(files)} files, bad={bad}")
