#!/bin/bash
# Run prefill_metadata.py as N parallel shards (default 4); each shard has its own lock,
# so the 5-hourly cron never starts a second copy of a shard that is still running.
cd /opt/abuhafs/youtube-auto-uploader || exit 1
N=${1:-4}
for ((k = 0; k < N; k++)); do
  /usr/bin/flock -n "/tmp/prefill.$k.lock" env PYTHONIOENCODING=utf-8 \
    venv/bin/python scripts/prefill_metadata.py --shard "$k/$N" >> logs/prefill_metadata.log 2>&1 &
done
wait
