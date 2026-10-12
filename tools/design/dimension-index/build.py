#!/usr/bin/env python3
"""
Design aid, not app code. Reads db/_cowork.db READ-ONLY and writes the five
mockups next to this file. Run:  python3 build.py
Sample rule (all dimensions, no hand picking): rank rows by richness (has image
+ has text), take the 8 richest by lowest id, then fill to 25 with a systematic
sample (every k-th row, A-Z) of the remainder.
"""
import sqlite3, json, os, math, re, datetime
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "..", "..", "db", "_cowork.db")
c = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
c.row_factory = sqlite3.Row
Q = lambda s, *a: [dict(r) for r in c.execute(s, a)]
SNAP = datetime.datetime.fromtimestamp(os.path.getmtime(DB), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
N = 25
MANIFEST = []

def pick(rows, n=N, rich_n=8):
    key = lambda r: (r["name"] or "").lower()
    if len(rows) <= n:
        return sorted(rows, key=key)
    ranked = sorted(rows, key=lambda r: (-(bool(r.get("img")) + bool(r.get("text"))), r["id"]))
    rich = [r for r in ranked[:rich_n] if (r.get("img") or r.get("text"))]
    ids = {r["id"] for r in rich}
    rest = sorted([r for r in rows if r["id"] not in ids], key=key)
    k = n - len(rich)
    step = len(rest) / k
    fill = [rest[int(i * step)] for i in range(k)]
    return sorted(rich + fill, key=key)

def img_for(kind, table, col, pid, name):
    r = Q(f"select id,filename,ext,origin,credit from {table} where {col}=? order by is_primary desc, sort_order, id limit 1", pid)
    if not r: return None
    r = r[0]
    MANIFEST.append({"kind": kind, "id": pid, "name": name, "filename": r["filename"], "ext": r["ext"]})
    return {"ext": r["ext"], "origin": r["origin"], "credit": r["credit"]}

# per-performance recording stats
perf = Q("""select p.id pid,p.artist_id,p.venue_id,p.event_id,p.start_year y,p.start_month m,p.start_day d,
            p.city,p.state,p.country,(select count(*) from recording r where r.performance_id=p.id) nrec
            from performance p""")

def agg(rows_):
    ys = [r["y"] for r in rows_ if r["y"]]
    return {"recs": sum(r["nrec"] for r in rows_), "shows": len(rows_),
            "y0": min(ys) if ys else None, "y1": max(ys) if ys else None}

def top_artists(rows_, n=4):
    cnt = {}
    for r in rows_: cnt[r["artist_id"]] = cnt.get(r["artist_id"], 0) + r["nrec"]
    ids = [a for a, _ in sorted(cnt.items(), key=lambda kv: -kv[1])[:n]]
    return [{"id": i, "name": ANAME[i]} for i in ids]

ANAME = {r["id"]: r["name"] for r in Q("select id,name from artist")}
by_artist, by_venue, by_event = {}, {}, {}
for p in perf:
    by_artist.setdefault(p["artist_id"], []).append(p)
    if p["venue_id"]: by_venue.setdefault(p["venue_id"], []).append(p)
    if p["event_id"]: by_event.setdefault(p["event_id"], []).append(p)

COVERAGE = {}

# ---------------- Artists
arts = []
for a in Q("select a.*,g.name gname,g.color gcolor from artist a left join genre g on g.id=a.genre_id"):
    text = (a["bio"] or "").strip()
    if not text and a["dossier_json"]:
        try: text = (json.loads(a["dossier_json"]).get("biography") or "").strip()
        except Exception: pass
    mem = Q("select distinct m.name from membership s join musician m on m.id=s.musician_id where s.artist_id=? order by s.\"order\",m.name", a["id"])
    ag = agg(by_artist.get(a["id"], []))
    arts.append(dict(id=a["id"], name=a["name"], sort=a["sort_name"], text=text, genre=a["gname"], color=a["gcolor"],
                     area=a["mb_area"], members=[m["name"] for m in mem], nmembers=len(mem), **ag,
                     _img=bool(Q("select 1 from artist_image where artist_id=? limit 1", a["id"]))))
for r in arts: r["img"] = r.pop("_img")
COVERAGE["artists"] = dict(total=len(arts), image=sum(r["img"] for r in arts), text=sum(bool(r["text"]) for r in arts),
    both=sum(bool(r["img"] and r["text"]) for r in arts), genre=sum(bool(r["genre"]) for r in arts),
    area=sum(bool(r["area"]) for r in arts), members=sum(bool(r["members"]) for r in arts))
sa = pick(arts)
for r in sa: r["img"] = img_for("artists", "artist_image", "artist_id", r["id"], r["name"])

# ---------------- Musicians
mus = []
for m in Q("select * from musician"):
    mem = Q("select artist_id,instrument,start_year from membership where musician_id=?", m["id"])
    inst = []
    for x in mem:
        for i in (x["instrument"] or "").split(","):
            i = i.strip()
            if i and i not in inst: inst.append(i)
    aids = list(dict.fromkeys(x["artist_id"] for x in mem))
    recs = sum(sum(p["nrec"] for p in by_artist.get(a, [])) for a in aids)
    ys = [p["y"] for a in aids for p in by_artist.get(a, []) if p["y"]]
    aord = sorted(aids, key=lambda a: -sum(p["nrec"] for p in by_artist.get(a, [])))
    mus.append(dict(id=m["id"], name=m["name"], sort=m["sort_name"], text=(m["bio"] or "").strip(),
                    inst=inst, artists=[ANAME[a] for a in aord[:3]], nartists=len(aids), recs=recs,
                    y0=min(ys) if ys else None, y1=max(ys) if ys else None,
                    img=bool(Q("select 1 from musician_image where musician_id=? limit 1", m["id"]))))
COVERAGE["musicians"] = dict(total=len(mus), image=sum(r["img"] for r in mus), text=sum(bool(r["text"]) for r in mus),
    instrument=sum(bool(r["inst"]) for r in mus), on_record=sum(r["recs"] > 0 for r in mus),
    multi_artist=sum(r["nartists"] > 1 for r in mus))
sm = pick(mus)
for r in sm: r["img"] = img_for("musicians", "musician_image", "musician_id", r["id"], r["name"])

# ---------------- Venues
ven = []
for v in Q("select * from venue"):
    ps = by_venue.get(v["id"], []); ag = agg(ps)
    ven.append(dict(id=v["id"], name=v["name"], city=v["city"], state=v["state"], country=v["country"],
                    text=(v["bio"] or "").strip(), top=[t["name"] for t in top_artists(ps, 3)], nartists=len({p["artist_id"] for p in ps}),
                    **ag, img=bool(Q("select 1 from venue_image where venue_id=? limit 1", v["id"]))))
COVERAGE["venues"] = dict(total=len(ven), image=sum(r["img"] for r in ven), text=sum(bool(r["text"]) for r in ven),
    city=sum(bool(r["city"]) for r in ven), country=sum(bool(r["country"]) for r in ven),
    on_record=sum(r["recs"] > 0 for r in ven), multi_rec=sum(r["recs"] > 1 for r in ven))
sv = pick(ven)
for r in sv: r["img"] = img_for("venues", "venue_image", "venue_id", r["id"], r["name"])

# ---------------- Events
MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
def date_label(ps):
    ds = sorted((p["y"] or 0, p["m"] or 0, p["d"] or 0) for p in ps if p["y"])
    if not ds: return "", None, 0
    a, b = ds[0], ds[-1]
    def one(t): return f"{MON[t[1]]} {t[2]}" if t[1] and t[2] else (MON[t[1]] if t[1] else "")
    if a == b: lab = one(a)
    elif a[1] == b[1] and a[1] and a[2] and b[2]: lab = f"{MON[a[1]]} {a[2]}-{b[2]}"
    else: lab = (one(a) + " - " + one(b)).strip(" -")
    return lab, a[0], a[0] * 10000 + a[1] * 100 + a[2]
eve = []
for e in Q("select * from event"):
    ps = by_event.get(e["id"], []); ag = agg(ps)
    lab, yr, key = date_label(ps)
    vn = Q("select v.name,v.city,v.state,v.country from venue v where v.id=?", ps[0]["venue_id"]) if ps and ps[0]["venue_id"] else []
    place = e["city"] or (vn[0]["city"] if vn else None) or next((p["city"] for p in ps if p["city"]), None)
    st = e["state"] or (vn[0]["state"] if vn else None) or next((p["state"] for p in ps if p["state"]), None)
    eve.append(dict(id=e["id"], name=e["name"], text=(e["notes"] or "").strip(), venue=vn[0]["name"] if vn else None,
                    place=", ".join(x for x in [place, st] if x), date=lab, year=yr, key=key,
                    lineup=[t["name"] for t in top_artists(ps, 4)], nartists=len({p["artist_id"] for p in ps}), **ag,
                    img=bool(Q("select 1 from event_image where event_id=? limit 1", e["id"]))))
COVERAGE["events"] = dict(total=len(eve), image=sum(r["img"] for r in eve), text=sum(bool(r["text"]) for r in eve),
    venue=sum(bool(r["venue"]) for r in eve), place=sum(bool(r["place"]) for r in eve),
    year=sum(bool(r["year"]) for r in eve), day_precise=sum(bool(re.search(r"\d", r["date"] or "")) for r in eve),
    multi_artist=sum(r["nartists"] > 1 for r in eve))
se = pick(eve)
for r in se: r["img"] = img_for("events", "event_image", "event_id", r["id"], r["name"])

# ---------------- Genres
gen = []
for g in Q("select * from genre"):
    aa = [a for a in arts if a["genre"] == g["name"]]
    dec = {}
    for a in aa:
        for p in by_artist.get(a["id"], []):
            if p["y"]: dec[p["y"] // 10 * 10] = dec.get(p["y"] // 10 * 10, 0) + p["nrec"]
    faces = sorted(aa, key=lambda a: (-bool(a["img"]), -a["recs"]))[:8]
    fx = []
    for a in faces:
        im = Q("select 1 from artist_image where artist_id=? limit 1", a["id"])
        fx.append(dict(id=a["id"], name=a["name"], img=img_for("artists", "artist_image", "artist_id", a["id"], a["name"]) if im else None))
    gen.append(dict(id=g["id"], name=g["name"], color=g["color"], text=(g["description"] or "").strip(),
                    nartists=len(aa), recs=sum(a["recs"] for a in aa), dec=dec, faces=fx))
COVERAGE["genres"] = dict(total=len(gen), color=sum(bool(g["color"]) for g in gen), text=sum(bool(g["text"]) for g in gen),
    with_artists=sum(g["nartists"] > 0 for g in gen),
    artists_unassigned=Q("select count(*) n from artist where genre_id is null")[0]["n"],
    recs_no_genre_colour=Q("select count(*) n from recording r join performance p on p.id=r.performance_id join artist a on a.id=p.artist_id left join genre g on g.id=a.genre_id where g.color is null")[0]["n"],
    recs=Q("select count(*) n from recording")[0]["n"])

json.dump(dict(snapshot=SNAP, coverage=COVERAGE), open(os.path.join(HERE, "coverage.json"), "w"), indent=1)
json.dump(MANIFEST, open(os.path.join(HERE, "photos_manifest.json"), "w"), indent=1)

# ---------------- HTML
helper = open(os.path.join(HERE, "mock.js")).read()
PALS = "steely krauss monk joni hazel townes chet gram cale miles alice eno".split()
def page(fname, title, kind, data, cfg_js, extra_body=""):
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
@font-face{{font-family:'Archivo';font-weight:100 900;src:url('../../../app/static/fonts/archivo-latin-wght-normal.woff2') format('woff2-variations')}}
@font-face{{font-family:'IBM Plex Sans';font-weight:100 700;src:url('../../../app/static/fonts/ibm-plex-sans-latin-wght-normal.woff2') format('woff2-variations')}}
</style>
<link rel="stylesheet" href="../../../app/static/css/main.css">
<link rel="stylesheet" href="dimension-index.css">
</head><body class="dx-mock">
<div class="dx-mockbar"><select id="pal">{''.join(f'<option>{p}</option>' for p in PALS)}</select></div>
<main class="dx-page" id="page"></main>
<script>
const KIND={json.dumps(kind)};
const DATA={json.dumps(data, ensure_ascii=False)};
{helper}
{cfg_js}
</script></body></html>"""
    open(os.path.join(HERE, fname), "w").write(html)

page("artists.html", "Artists", "artists", sa, open(os.path.join(HERE, "cfg_artists.js")).read())
page("musicians.html", "Musicians", "musicians", sm, open(os.path.join(HERE, "cfg_musicians.js")).read())
page("venues.html", "Venues", "venues", sv, open(os.path.join(HERE, "cfg_venues.js")).read())
page("events.html", "Events", "events", se, open(os.path.join(HERE, "cfg_events.js")).read())
page("genres.html", "Genres", "genres", gen, open(os.path.join(HERE, "cfg_genres.js")).read())
print("snapshot", SNAP); print(json.dumps(COVERAGE, indent=1))
