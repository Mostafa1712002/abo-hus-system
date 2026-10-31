#!/bin/bash
# One-off (2026-10-30): move the archive zips off the full tarweig account onto this box.
cd /opt/abuhafs/youtube-auto-uploader || exit 1
COLD=(ssh -o BatchMode=yes -p 22422 abuhafsi@213.239.209.167)
"${COLD[@]}" "ls videos_cold_zips/*.zip" | while IFS= read -r remote; do
  name=$(basename "$remote")
  echo "$(date "+%F %T") >> $name"
  scp -q -o BatchMode=yes -P 22422 "abuhafsi@213.239.209.167:videos_cold_zips/$name" "/opt/abuhafs/archive_in/$name" < /dev/null || { echo "!! fetch failed $name"; continue; }
  out=$(echo "{}" | venv/bin/python scripts/archive_ingest.py "/opt/abuhafs/archive_in/$name" "$name")
  echo "   $out"
  case "$out" in *"\"ok\": true"*) "${COLD[@]}" "rm -f \"videos_cold_zips/$name\"" < /dev/null && echo "   removed from tarweig";; *) echo "!! ingest failed $name";; esac
done
echo "$(date "+%F %T") MIGRATION DONE"
