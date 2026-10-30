#!/usr/bin/env python3
"""When do competitors post + when do their posts get views.
Buckets recent videos by hour (UTC+3 Gulf) & weekday. Usage: posting_times.py <ids|file.txt> [--per N]"""
import json, os, sys, urllib.parse, urllib.request
from collections import defaultdict
from statistics import median
CFG=json.load(open("config.json"));KEY=CFG["youtube_data_api_key"]
API="https://www.googleapis.com/youtube/v3/"
def api(ep,**p):
    p["key"]=KEY
    with urllib.request.urlopen(API+ep+"?"+urllib.parse.urlencode(p),timeout=30) as r:
        return json.load(r)
def resolve(tok):
    tok=tok.strip().rstrip("/")
    import re
    m=re.search(r"/channel/(UC[\w-]{22})",tok) or re.match(r"(UC[\w-]{22})$",tok)
    if m: return m.group(1)
    m=re.search(r"@([\w.\-]+)",tok)
    if m:
        d=api("channels",part="id",forHandle="@"+m.group(1))
        if d.get("items"): return d["items"][0]["id"]
    return None
def recent(cid,n):
    up=api("channels",part="contentDetails",id=cid)
    if not up.get("items"): return []
    pl=up["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids,tok=[],None
    while len(ids)<n:
        d=api("playlistItems",part="contentDetails",playlistId=pl,maxResults=min(50,n-len(ids)),**({"pageToken":tok} if tok else {}))
        ids+=[it["contentDetails"]["videoId"] for it in d.get("items",[])]
        tok=d.get("nextPageToken")
        if not tok: break
    return ids[:n]
def vids(ids):
    out=[]
    for i in range(0,len(ids),50):
        out+=api("videos",part="snippet,statistics",id=",".join(ids[i:i+50])).get("items",[])
    return out
WD=["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]  # Mon=0
def parse(iso):  # 2024-06-01T13:20:00Z -> (weekday, hour) in UTC+3
    import datetime
    d=datetime.datetime.strptime(iso,"%Y-%m-%dT%H:%M:%SZ")+datetime.timedelta(hours=3)
    return d.weekday(), d.hour

argv=sys.argv[1:]; per=30
if "--per" in argv: per=int(argv[argv.index("--per")+1])
toks=[a for a in argv if not a.startswith("--") and a!=str(per)]
tokens=[]
for a in toks:
    if a.endswith(".txt") and os.path.exists(a): tokens+=[l.strip() for l in open(a) if l.strip()]
    else: tokens.append(a)

hour_cnt=defaultdict(int); hour_views=defaultdict(list)
wd_cnt=defaultdict(int); wd_views=defaultdict(list); nvid=0; nch=0
for t in tokens:
    cid=t if t.startswith("UC") else resolve(t)
    if not cid: continue
    vs=vids(recent(cid,per))
    if not vs: continue
    nch+=1
    for v in vs:
        pub=v["snippet"].get("publishedAt");
        if not pub: continue
        wd,hr=parse(pub); views=int(v.get("statistics",{}).get("viewCount",0))
        hour_cnt[hr]+=1; hour_views[hr].append(views)
        wd_cnt[wd]+=1; wd_views[wd].append(views); nvid+=1

print(f"### {nch} channels, {nvid} videos — times in UTC+3 (الخليج) ###\n")
print("== متى ينشرون (histogram بالساعة) ==")
mx=max(hour_cnt.values()) if hour_cnt else 1
for h in range(24):
    c=hour_cnt.get(h,0); bar="█"*int(30*c/mx)
    mv=int(median(hour_views[h])) if hour_views.get(h) else 0
    print(f"  {h:02d}:00  {c:>3}  {bar:<30} median_views={mv:,}")
print("\n== أفضل ساعات (median views، أدنى 5 فيديو) ==")
best=sorted(((int(median(v)),h,len(v)) for h,v in hour_views.items() if len(v)>=5),reverse=True)
for mv,h,n in best[:6]: print(f"  {h:02d}:00  median_views={mv:,}  (n={n})")
print("\n== باليوم ==")
mxw=max(wd_cnt.values()) if wd_cnt else 1
for d in range(7):
    c=wd_cnt.get(d,0); mv=int(median(wd_views[d])) if wd_views.get(d) else 0
    print(f"  {WD[d]:<8} {c:>3}  {'█'*int(25*c/mxw):<25} median_views={mv:,}")
