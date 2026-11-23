#!/bin/bash
# Run a Claude batch script as N parallel shards (default 4); each shard has its own lock,
# so the hourly cron never starts a second copy of a shard that is still running.
# Usage: prefill_lanes.sh [N] [script-name]   (script defaults to prefill_metadata)
cd /opt/abuhafs/youtube-auto-uploader || exit 1
N=${1:-4}; S=${2:-prefill_metadata}; L=${S%%_*}
for ((k = 0; k < N; k++)); do
  /usr/bin/flock -n "/tmp/$L.$k.lock" env PYTHONIOENCODING=utf-8 \
    venv/bin/python "scripts/$S.py" --shard "$k/$N" >> "logs/$S.log" 2>&1 &
done
wait
