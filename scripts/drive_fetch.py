#!/usr/bin/env python3
"""Background Google-Drive fetcher for the dashboard Media Library.

Spawned detached by `/media/drive`. Reads a job JSON, downloads via gdown,
updates the same JSON with status (downloading -> done|error). Runs under the
project venv so `gdown` is importable.
"""
import json
import os
import sys
from pathlib import Path


def main() -> None:
    jobfile = Path(sys.argv[1])
    job = json.loads(jobfile.read_text(encoding="utf-8"))

    def save(**kw):
        job.update(kw)
        jobfile.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")

    save(status="downloading", pid=os.getpid())
    try:
        import gdown  # provided by the venv

        file_id = job["file_id"]
        dest = job.get("dest") or ""
        if dest:
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            out = gdown.download(id=file_id, output=dest, quiet=True)
        else:
            d = job["dir"]
            Path(d).mkdir(parents=True, exist_ok=True)
            out = gdown.download(id=file_id, output=d + os.sep, quiet=True)

        if not out or not Path(out).is_file():
            save(status="error", message="gdown returned no file (private/quota/invalid id?)")
            return
        size = Path(out).stat().st_size
        save(
            status="done",
            dest=str(out),
            name=Path(out).name,
            size_mb=round(size / 1048576, 1),
        )
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
        save(status="error", message=str(e)[:300])


if __name__ == "__main__":
    main()
