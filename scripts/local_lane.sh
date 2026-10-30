#!/bin/bash
# One local-archive upload for a lane: feed the next file from cold storage,
# then run the full catalog pipeline (intro, YouTube, playlist, FB/IG/TG, shorts)
# for that series. Shares /tmp/cat_up.lock with the catalog crons.
# Usage: local_lane.sh <slug> [<slug> ...]   (ordered: lane advances when a series ends)
cd /opt/abuhafs/youtube-auto-uploader || exit 1
export PYTHONIOENCODING=utf-8 GEMINI_WEB_ANON=1
exec 9>/tmp/cat_up.lock
flock -w 3300 9 || { echo "$(date '+%F %T') lock busy, skip"; exit 0; }
SLUG=$(venv/bin/python scripts/local_feed.py "$@")
if [ -z "$SLUG" ]; then echo "$(date '+%F %T') lane [$*]: nothing to do"; exit 0; fi
echo "$(date '+%F %T') lane -> $SLUG"
venv/bin/python scripts/catalog_full_worker.py --series "$SLUG" --limit 1
