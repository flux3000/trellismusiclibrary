"""
track_variance.py — diagnostic: how much do the quality features vary from
track to track WITHIN one recording?

Answers the sampling question directly. If a feature's spread across a whole
show is large, sampling a handful of tracks estimates it poorly and the
feature should be downweighted (or the sampling widened).

Resumable: appends to the JSON after each track so it can run under a short
execution timeout.

Usage: python3 track_variance.py <recording_folder> <out.json> [max_per_run]
"""
import os
import sys
import glob
import json
import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
from app.utils.quality import (                            # noqa: E402
    analyse_window, read_window, window_offsets,
)

folder, out = sys.argv[1], sys.argv[2]
budget = int(sys.argv[3]) if len(sys.argv) > 3 else 99

res = json.load(open(out)) if os.path.exists(out) else {}
done = 0
# Recursive: same bug quality_features.py hit — a root-only glob silently
# skips any recording still carrying CD1/ CD2/ disc subdirs (pre-flatten-policy
# ingests, e.g. 1979 Balboa Jazz Club). Path-string sort keeps discs in order.
for p in sorted(glob.glob(os.path.join(folder, "**", "*.flac"), recursive=True)):
    nm = os.path.basename(p)
    if nm in res or done >= budget:
        continue
    dur = sf.info(p).duration
    ws = []
    for o in window_offsets(dur):
        x, sr = read_window(p, o)
        if len(x) > sr:
            ws.append(analyse_window(x, sr))
    if not ws:
        continue
    med = lambda k: float(np.median([w[k] for w in ws]))
    res[nm] = {
        "duration_min": round(dur / 60, 1),
        "mid_snr_db": med("mid_snr_db"),
        "presence_balance_db": med("presence_balance_db"),
        "midrange_scoop_db": med("midrange_scoop_db"),
        "spectral_tilt_db_oct": med("spectral_tilt_db_oct"),
        "crest_factor_db": med("crest_factor_db"),
        "rms_db": med("rms_db"),
        "hf_edge_hz": med("hf_edge_hz"),
        "hf_energy_ratio_db": med("hf_energy_ratio_db"),
        "clipping_pct": float(np.max([w["clipping_pct"] for w in ws])),
    }
    json.dump(res, open(out, "w"), indent=1)
    print(f"done {nm}", flush=True)
    done += 1

total = len(glob.glob(os.path.join(folder, "**", "*.flac"), recursive=True))
print(f"COMPLETE={len(res) >= total} {len(res)}/{total}")
