#!/bin/bash
# Auto-advancing catalog feeder. Keeps a small "ready" buffer (downloaded+pending).
# When low: retry transient download failures, then download next pending rows.
# Workers order by series_slug,ord -> when one series empties, next series enters
# automatically. No manual cron edits when a series finishes.
cd /opt/abuhafs/youtube-auto-uploader || exit 1
export PYTHONIOENCODING=utf-8
READY=$(venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/abuhafs.db').execute(\"SELECT COUNT(*) FROM catalog_videos WHERE download_status='downloaded' AND process_status='pending'\").fetchone()[0])" 2>/dev/null)
[ -z "$READY" ] && exit 0
if [ "$READY" -lt 5 ]; then
  # ponytail: reset transient dl failures (loader.to timeout/502); truly-bad rows re-fail = minor waste, visible in log
  venv/bin/python -c "import sqlite3;d=sqlite3.connect('data/abuhafs.db');d.execute(\"UPDATE catalog_videos SET download_status='pending',download_error='' WHERE download_status='failed'\");d.commit()"
  venv/bin/python scripts/catalog_download_worker.py --limit 3 >> logs/catalog_download.log 2>&1
fi
