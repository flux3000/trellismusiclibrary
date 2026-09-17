"""
Extract raw quality features for every recording in a Artist folder.

Resumable: writes results after each recording and skips anything already
present, so it can be run repeatedly under a short execution timeout.

Usage: python3 run_extract.py <artist_folder> <out.json> [max_per_run]
"""
import os
import sys
import json
import time

# Engine lives in app/utils/quality/ since 2026-07-30 — repo root on sys.path.
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
from app.utils.quality import extract_recording_features   # noqa: E402

base = sys.argv[1]
out = sys.argv[2]
budget = int(sys.argv[3]) if len(sys.argv) > 3 else 99

res = {}
if os.path.exists(out):
    try:
        res = json.load(open(out))
    except Exception:
        res = {}

done = 0
for d in sorted(os.listdir(base)):
    p = os.path.join(base, d)
    if not os.path.isdir(p) or d in res:
        continue
    if done >= budget:
        break
    t = time.time()
    res[d] = extract_recording_features(p)
    json.dump(res, open(out, "w"), indent=1, default=str)
    print(f"done ({time.time()-t:.1f}s) {d[:64]}", flush=True)
    done += 1

remaining = len([d for d in os.listdir(base)
                 if os.path.isdir(os.path.join(base, d)) and d not in res])
print(f"COMPLETE={remaining == 0} remaining={remaining}")
