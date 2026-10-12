"""width_probe.py — does stereo width predict the user's letter grades?

Measured as the standard Mid/Side ratio:

    stereo_width_db = 20*log10( RMS(L-R) / RMS(L+R) )

Pure mono -> -inf. Typical stereo sits around -6 to -15 dB. Approaching 0 dB
means the sides carry as much energy as the centre: very wide.

This is a better tool than the Pearson L/R correlation already in the Defects
facet. Correlation answers "are the channels related" (a phase/wiring check);
M/S answers "how much of the signal is NOT in the centre", which is the thing
actually being perceived as width.

Reuses the exact tracks + window offsets already chosen by the main pipeline,
read from the feature JSONs, so results line up with the existing scores.

Resumable. Usage: python3 width_probe.py <features.json> <library_root> <out.json> [budget]
"""
import os
import sys
import json
import numpy as np
import soundfile as sf

WINDOW_SEC = 20.0

feats_path, lib_root, out = sys.argv[1], sys.argv[2], sys.argv[3]
budget = int(sys.argv[4]) if len(sys.argv) > 4 else 99

feats = json.load(open(feats_path))
res = json.load(open(out)) if os.path.exists(out) else {}

done = 0
for rec, f in feats.items():
    if "error" in f or rec in res or done >= budget:
        continue
    vals = []
    for s in f.get("sampled", []):
        p = os.path.join(lib_root, rec, s["track"])
        if not os.path.exists(p):
            continue
        for off in s["offsets"]:
            with sf.SoundFile(p) as fh:
                sr = fh.samplerate
                fh.seek(int(off * sr))
                x = fh.read(int(WINDOW_SEC * sr), dtype="float64", always_2d=True)
            if x.shape[0] < sr or x.shape[1] < 2:
                continue
            L, R = x[:, 0], x[:, 1]
            mid = (L + R) / 2.0
            side = (L - R) / 2.0
            rm = float(np.sqrt(np.mean(mid ** 2)))
            rs = float(np.sqrt(np.mean(side ** 2)))
            if rm > 1e-9:
                vals.append(20 * np.log10(max(rs, 1e-9) / rm))
    if vals:
        res[rec] = {"stereo_width_db": float(np.median(vals)), "n_windows": len(vals)}
        json.dump(res, open(out, "w"), indent=1)
        print(f"done {round(res[rec]['stereo_width_db'],1):>7} dB  {rec[:56]}", flush=True)
        done += 1

print(f"COMPLETE={len(res) >= len([k for k,v in feats.items() if 'error' not in v])}")
