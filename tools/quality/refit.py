#!/usr/bin/env python3
"""
refit.py — re-validate and re-calibrate the Listening Quality engine.

This exists because of what happened on 2026-07-31: the engine had been tuned
across three sessions against 13, then 20, then 24 hand-collected grades, while
the database quietly held 110. Reported fit was r = 0.861 / MAE 4.14; measured
against the full corpus it was r = 0.416 / MAE 8.21. The difference was pure
overfitting, and it was invisible because nobody could cheaply re-check.

So: cheap re-checking, on demand.

    python3 refit.py extract          # decode audio -> feature cache (slow, resumable)
    python3 refit.py report           # score the cache against grades (instant)
    python3 refit.py calibrate        # propose new CALIBRATION_* constants
    python3 refit.py features         # per-feature correlations, incl. per source

`report` and `calibrate` need NO audio decode — features and scoring are
separate modules precisely so weights can be retuned without touching a FLAC.
Run `extract` once after any change to quality_features.py; run the others
freely.

RUN THIS after grading a batch of recordings. The calibration constants are a
2-parameter fit and will drift as the corpus grows — especially at the bad end,
which is where it is currently thinnest (as of 2026-07-31: 92 green, 17 yellow,
4 red, so RED detection is not yet trustworthy).
"""

import os
import sys
import json
import time
import sqlite3
import unicodedata

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, _REPO)


def _engine():
    """
    Load the scoring engine WITHOUT importing the `app` package.

    `from app.utils.quality import ...` executes app/__init__.py, which pulls in
    Flask, flask_sqlalchemy and the whole web stack. This tool does arithmetic
    over a JSON cache; it has no business requiring a web framework, and making
    it independent means it still runs when the app environment is broken —
    which is exactly when you want to be able to check whether the engine is
    still behaving.
    """
    import importlib.util

    mods = {}
    for name in ("quality_features", "quality_scoring", "quality_interpret"):
        path = os.path.join(_REPO, "app", "utils", "quality", f"{name}.py")
        spec = importlib.util.spec_from_file_location(f"_rf_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods

CACHE = os.path.join(_HERE, "labelled_corpus.json")
DB = os.path.join(_REPO, "db", "trellis.db")
LIB = os.environ.get("LIBRARY_ROOT", "/Volumes/music/Flux Audio/Library")

# Letter grade -> points. Mid-band of each letter; the exact spacing matters
# less than the ordering, and this is the mapping every 2026-07-31 number was
# computed with, so changing it invalidates comparison with that session.
GRADE_POINTS = {"A+": 100, "A": 92, "A-": 85, "B+": 78,
                "B": 70, "B-": 63, "C": 55, "D": 45, "F": 30}


def _nfc(s):
    # macOS stores filenames decomposed, the DB composed. This join silently
    # broke the whole Guitar Trio corpus once already.
    return unicodedata.normalize("NFC", s or "")


def _graded_rows():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""SELECT id, folder_path, quality, source FROM recording
                   WHERE quality IS NOT NULL AND quality != ''""")
    rows = cur.fetchall()
    con.close()
    return rows


# ─────────────────────────────────────────────────────────────────────────────
def cmd_extract(budget=None):
    """Decode audio and cache raw features. Resumable — safe to re-run."""
    extract_recording_features = _engine()["quality_features"].extract_recording_features

    cache = {}
    if os.path.exists(CACHE):
        cache = json.load(open(CACHE))

    todo = [(rid, fp, q, src) for rid, fp, q, src in _graded_rows()
            if str(rid) not in cache]
    print(f"cached {len(cache)}  to do {len(todo)}")
    done = 0
    for rid, fp, q, src in todo:
        if budget and done >= budget:
            break
        path = os.path.join(LIB, fp)
        t0 = time.time()
        if not os.path.isdir(path):
            rec = {"error": "folder missing"}
        else:
            try:
                rec = extract_recording_features(path)
            except Exception as e:                            # noqa: BLE001
                rec = {"error": f"{type(e).__name__}: {e}"}
        rec.update(grade=q, source=src, folder_path=fp)
        cache[str(rid)] = rec
        json.dump(cache, open(CACHE, "w"), indent=1, default=str)
        print(f"  {time.time()-t0:5.1f}s [{q:3s}] {os.path.basename(fp)[:58]}",
              flush=True)
        done += 1
    left = len([1 for rid, *_ in _graded_rows() if str(rid) not in cache])
    print(f"COMPLETE={left == 0} remaining={left}")


def _load():
    if not os.path.exists(CACHE):
        sys.exit("No feature cache. Run:  python3 refit.py extract")
    cache = json.load(open(CACHE))
    rows = []
    for rid, v in cache.items():
        if "error" in v or v.get("grade") not in GRADE_POINTS:
            continue
        v["_y"] = GRADE_POINTS[v["grade"]]
        v["_id"] = rid
        rows.append(v)
    return rows


def _scored(rows):
    score_recording = _engine()["quality_scoring"].score_recording
    y, p, band, src = [], [], [], []
    for r in rows:
        s = score_recording(r, source=r.get("source"))
        if s.get("predicted_grade") is None:
            continue
        y.append(r["_y"])
        p.append(s["predicted_grade"])
        band.append(s["verdict_band"])
        src.append((r.get("source") or "?").upper()[:3])
    return np.array(y, float), np.array(p, float), band, src


# ─────────────────────────────────────────────────────────────────────────────
def cmd_report():
    rows = _load()
    y, p, band, src = _scored(rows)
    print(f"n = {len(y)}")
    print(f"  correlation  {np.corrcoef(y, p)[0, 1]:+.3f}")
    print(f"  MAE          {np.mean(np.abs(y - p)):.2f} grade points")
    print(f"  grade span   {y.min():.0f}-{y.max():.0f}"
          f"   model span {p.min():.1f}-{p.max():.1f}")

    print("\n  by source")
    for s in sorted(set(src)):
        m = [i for i, x in enumerate(src) if x == s]
        if len(m) < 6:
            continue
        yy, pp = y[m], p[m]
        print(f"    {s:4s} n={len(m):3d} corr={np.corrcoef(yy, pp)[0, 1]:+.3f}"
              f" MAE={np.mean(np.abs(yy - pp)):.2f}")

    def tb(v):
        return "green" if v >= 85 else ("yellow" if v >= 70 else "red")
    truth = [tb(v) for v in y]
    print("\n  three-band confusion        pred green yellow   red")
    for t in ("green", "yellow", "red"):
        r = [sum(1 for i in range(len(y)) if truth[i] == t and band[i] == q)
             for q in ("green", "yellow", "red")]
        print(f"    true {t:6s}                {r[0]:5d} {r[1]:6d} {r[2]:5d}"
              f"   (n={sum(r)})")
    acc = np.mean([truth[i] == band[i] for i in range(len(y))])
    base = truth.count("green") / len(truth)
    print(f"    exact {acc*100:.0f}%   always-green baseline {base*100:.0f}%")
    bad = [i for i in range(len(y)) if truth[i] == "red"]
    if bad:
        print(f"    RED recall {sum(1 for i in bad if band[i]=='red')}/{len(bad)}"
              f"   duds called GREEN {sum(1 for i in bad if band[i]=='green')}/{len(bad)}")
    if len(bad) < 15:
        print(f"\n  ⚠ only {len(bad)} RED-grade recordings. Band thresholds are"
              f" fitted to very few bad examples and RED is not yet reliable."
              f" Grade more poor-sounding material before trusting it.")


def cmd_calibrate():
    """Propose CALIBRATION_SLOPE / _INTERCEPT from the current cache."""
    sc = _engine()["quality_scoring"]
    score_recording = sc.score_recording

    rows = _load()
    raw, y = [], []
    for r in rows:
        s = score_recording(r, source=r.get("source"))
        if s.get("listening_quality") is None:
            continue
        raw.append(s["listening_quality"])
        y.append(r["_y"])
    raw, y = np.array(raw, float), np.array(y, float)
    a, b = np.polyfit(raw, y, 1)
    cal = a * raw + b
    cur = sc.CALIBRATION_SLOPE * raw + sc.CALIBRATION_INTERCEPT
    print(f"n = {len(y)}")
    print(f"  current   slope {sc.CALIBRATION_SLOPE:.4f}  intercept "
          f"{sc.CALIBRATION_INTERCEPT:.3f}   MAE {np.mean(np.abs(y-cur)):.2f}")
    print(f"  proposed  slope {a:.4f}  intercept {b:.3f}   MAE "
          f"{np.mean(np.abs(y-cal)):.2f}")
    if np.mean(np.abs(y - cal)) < np.mean(np.abs(y - cur)) - 0.05:
        print("\n  -> worth updating quality_scoring.py:")
        print(f"       CALIBRATION_SLOPE = {a:.4f}")
        print(f"       CALIBRATION_INTERCEPT = {b:.3f}")
    else:
        print("\n  -> current calibration is fine, leave it alone.")


def cmd_features():
    """Per-feature correlation with grade, overall and per source."""
    rows = _load()
    keys = [k for k in ("spectral_tilt_db_oct", "hf_energy_ratio_db",
                        "hf_edge_hz", "mid_snr_db", "crowd_snr_db",
                        "noise_nonstationarity_db", "modulation_index",
                        "crest_factor_db", "hum_ratio_db",
                        "presence_balance_db", "midrange_scoop_db")
            if any(r.get(k) is not None for r in rows)]
    y = np.array([r["_y"] for r in rows], float)
    print(f"n = {len(rows)}\n  {'feature':26s} {'all':>7s} {'SBD':>7s} "
          f"{'AUD':>7s} {'FM':>7s}")
    for k in keys:
        cells = []
        for grp in (None, "SBD", "AUD", "FM"):
            m = [i for i, r in enumerate(rows)
                 if r.get(k) is not None
                 and (grp is None or (r.get("source") or "").upper().startswith(grp))]
            if len(m) < 8:
                cells.append("     --")
                continue
            x = np.array([rows[i][k] for i in m], float)
            cells.append("     --" if x.std() == 0
                         else f"{np.corrcoef(x, y[m])[0,1]:+7.3f}")
        print(f"  {k:26s} " + " ".join(cells))
    print("\n  Reminder: a feature that looks strong in one column and weak in"
          "\n  another is the 2026-07-31 trap. Judge on the 'all' column and on"
          "\n  cross-validated gain, never on a single source or artist.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    arg = int(sys.argv[2]) if len(sys.argv) > 2 else None
    {"extract": lambda: cmd_extract(arg), "report": cmd_report,
     "calibrate": cmd_calibrate, "features": cmd_features}.get(
        cmd, lambda: sys.exit(__doc__))()
