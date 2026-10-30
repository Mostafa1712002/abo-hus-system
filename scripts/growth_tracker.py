#!/usr/bin/env python3
"""Snapshot OUR channel + recent videos to data/growth_history.jsonl and print
week-over-week deltas. Run weekly via cron. No OAuth needed (Data API key)."""
import json, os, datetime, urllib.parse, urllib.request
CFG=json.load(open("config.json")); KEY=CFG["youtube_data_api_key"]
CH=CFG["youtube"]["channel_id"]; API="https://www.googleapis.com/youtube/v3/"
HIST="data/growth_history.jsonl"
def api(ep,**p):
    p["key"]=KEY
    with urllib.request.urlopen(API+ep+"?"+urllib.parse.urlencode(p),timeout=30) as r:
        return json.load(r)
def recent(n=50):
    pl=api("channels",part="contentDetails",id=CH)["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids,tok=[],None
    while len(ids)<n:
        d=api("playlistItems",part="contentDetails",playlistId=pl,maxResults=min(50,n-len(ids)),**({"pageToken":tok} if tok else {}))
        ids+=[it["contentDetails"]["videoId"] for it in d.get("items",[])]; tok=d.get("nextPageToken")
        if not tok: break
    return ids[:n]
ch=api("channels",part="statistics",id=CH)["items"][0]["statistics"]
ids=recent(50); vids={}
for i in range(0,len(ids),50):
    for v in api("videos",part="snippet,statistics",id=",".join(ids[i:i+50]))["items"]:
        s=v.get("statistics",{})
        vids[v["id"]]={"t":v["snippet"]["title"][:50],"pub":v["snippet"]["publishedAt"],
                       "v":int(s.get("viewCount",0)),"l":int(s.get("likeCount",0)),"c":int(s.get("commentCount",0))}
now=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
snap={"ts":now,"subs":int(ch.get("subscriberCount",0)),"total_views":int(ch.get("viewCount",0)),
      "videos_count":int(ch.get("videoCount",0)),"videos":vids}
prev=None
if os.path.exists(HIST):
    lines=[l for l in open(HIST) if l.strip()]
    if lines: prev=json.loads(lines[-1])
os.makedirs("data",exist_ok=True)
open(HIST,"a").write(json.dumps(snap,ensure_ascii=False)+"\n")
print(f"=== SNAPSHOT {now} ===")
print(f"subs={snap['subs']:,}  total_views={snap['total_views']:,}  videos={snap['videos_count']}")
if prev:
    print(f"\n=== Δ since {prev['ts']} ===")
    print(f"  subs {snap['subs']-prev['subs']:+,}   total_views {snap['total_views']-prev['total_views']:+,}   videos {snap['videos_count']-prev['videos_count']:+}")
    mov=[]
    for vid,d in vids.items():
        pv=prev["videos"].get(vid)
        if pv: mov.append((d["v"]-pv["v"],d["t"]))
    mov.sort(reverse=True)
    print("  top movers (views this period):")
    for dv,t in mov[:8]: print(f"    {dv:+,}  {t}")
else:
    print("\n(baseline saved — deltas appear on next run)")
    print("\nrecent-video view distribution (last 50):")
    vs=sorted((d["v"] for d in vids.values()),reverse=True)
    if vs:
        import statistics
        print(f"  max={vs[0]:,}  median={int(statistics.median(vs)):,}  min={vs[-1]:,}")
