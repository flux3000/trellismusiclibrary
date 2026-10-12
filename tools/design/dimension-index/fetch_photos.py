#!/usr/bin/env python3
"""
Copies the mockup sample's pictures from the app's image store into ./photos/
so the HTML shows them. Run on the machine that holds the images (the Mac mini):

    python3 fetch_photos.py "$HOME/Library/Application Support/Trellis Music Library"

Reads photos_manifest.json, finds each file by its stored filename under
<data dir>/images/<kind>/, and copies it to photos/<kind>/<id>.<ext>.
Read-only on the app's data. Stdlib only.
"""
import json, os, shutil, sys
here = os.path.dirname(os.path.abspath(__file__))
data = sys.argv[1]
index = {}
for kind in ("artists", "musicians", "venues", "events"):
    for root, _, files in os.walk(os.path.join(data, "images", kind)):
        for f in files:
            index.setdefault((kind, f), os.path.join(root, f))
ok = miss = 0
for m in json.load(open(os.path.join(here, "photos_manifest.json"))):
    src = index.get((m["kind"], m["filename"]))
    if not src:
        miss += 1; print("missing", m["kind"], m["id"], m["filename"]); continue
    dst = os.path.join(here, "photos", m["kind"], f'{m["id"]}{os.path.splitext(src)[1] or m["ext"]}')
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst); ok += 1
print(f"copied {ok}, missing {miss}")
