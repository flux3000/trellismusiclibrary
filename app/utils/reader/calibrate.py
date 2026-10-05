"""
Fitting the confidence thresholds (Resolver v2, chunk 6, 2026-10-04).

Pure: no network, no filesystem except write_calibration(). Deterministic: no random
numbers, folds are assigned by sorting, ties are broken the same way every run.

Input, per field, is a list of samples, one per (item, population):

    {"item": id, "group": artist key, "pop": "none" | "loo", "logit": float,
     "margin": float, "has_value": bool, "wrong": bool}

`group` is the gold artist, so one act never sits in both the fitting folds and the
held-out fold (the corpus is full of one act recorded forty times; a split by item
would grade the fit on shows it has already seen).

Calibration is by fixed bins on the logit with the share of right values per bin made
monotone (pool-adjacent-violators). Then tau (on the calibrated probability) and m (on
the margin) are the pair that keeps the most right values confident while
confident-but-wrong stays under the target in EVERY population (library none and
leave-one-out are different populations: a threshold that holds in one and not the
other is not a threshold).

The reported number is the held-out one. The shipped thresholds are fitted on all of
the corpus.
"""
import json
import math
from datetime import date

from . import confidence as C

# Bin edges on the logit; None is open-ended.
EDGES = [None, 0, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 12.5, 13, 13.5, 14, 14.5, 15, 15.5, 16, 17, 18, 20, None]
M_GRID = [x / 2 for x in range(0, 25)]            # 0 .. 12 in half points
TARGET = 0.005                                    # decision 7: 0.5% per field
# The fit aims below the target so the held-out rate lands under it. The factor is a rule
# fixed before the held-out result was looked at again: half the gate.
TRAIN_TARGET = 0.5 * TARGET
# A field needs this many scored items in every population before a threshold means
# anything, and a threshold must keep this share of them confident and right; below that
# the field has no calibrated confident region (see the fallback in fit_model).
MIN_SCORED = 200
NEVER = 1000.0                                    # a floor no logit reaches
MIN_COVERAGE = 0.02


def assign_folds(groups, k):
    """{group: fold} balanced by group size, deterministic."""
    counts = {}
    for g in groups:
        counts[g] = counts.get(g, 0) + 1
    sizes = [0] * k
    out = {}
    for g in sorted(counts, key=lambda g: (-counts[g], str(g))):
        f = min(range(k), key=lambda i: (sizes[i], i))
        out[g] = f
        sizes[f] += counts[g]
    return out


def _bin_index(logit):
    for i in range(len(EDGES) - 1):
        lo, hi = EDGES[i], EDGES[i + 1]
        if (lo is None or logit >= lo) and (hi is None or logit < hi):
            return i
    return len(EDGES) - 2


def make_bins(samples):
    """Fixed bins with a monotone P(right): (right + 1) / (n + 2), then PAV."""
    n = [0] * (len(EDGES) - 1)
    right = [0] * (len(EDGES) - 1)
    for s in samples:
        if not s["has_value"]:
            continue
        i = _bin_index(s["logit"])
        n[i] += 1
        right[i] += 0 if s["wrong"] else 1
    blocks = []                                   # [weighted p, weight, [bin indexes]]
    for i in range(len(n)):
        blocks.append([(right[i] + 1) / (n[i] + 2), n[i] + 2, [i]])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            b = blocks.pop()
            a = blocks[-1]
            w = a[1] + b[1]
            a[0] = (a[0] * a[1] + b[0] * b[1]) / w
            a[1] = w
            a[2] = a[2] + b[2]
    p = [0.0] * len(n)
    for pv, _, idx in blocks:
        for i in idx:
            p[i] = pv
    return [{"lo": EDGES[i], "hi": EDGES[i + 1], "n": n[i], "right": right[i], "p": round(p[i], 4)}
            for i in range(len(n))]


def _p_of(bins, logit):
    return bins[_bin_index(logit)]["p"]


def _counts(samples, bins, tau, m, min_logit=None):
    """Per population: (scored, confident, confident_wrong, confident_right)."""
    out = {}
    for s in samples:
        c = out.setdefault(s["pop"], [0, 0, 0, 0])
        c[0] += 1
        if (s["has_value"] and _p_of(bins, s["logit"]) >= tau and s["margin"] >= m
                and (min_logit is None or s["logit"] >= min_logit)):
            c[1] += 1
            if s["wrong"]:
                c[2] += 1
            else:
                c[3] += 1
    return out


def fit_model(samples, train_target=TRAIN_TARGET):
    """{bins, tau, m} for one field."""
    bins = make_bins(samples)
    taus = sorted({b["p"] for b in bins if b["n"]} | {1.01})
    best = None
    pops = {}
    for s in samples:
        pops[s["pop"]] = pops.get(s["pop"], 0) + 1
    enough = bool(pops) and min(pops.values()) >= MIN_SCORED
    for tau in (taus if enough else []):
        for m in M_GRID:
            cnt = _counts(samples, bins, tau, m)
            if any(c[2] / c[0] > train_target for c in cnt.values() if c[0]):
                continue
            right = sum(c[3] for c in cnt.values())
            wrong = sum(c[2] for c in cnt.values())
            key = (right, -wrong, tau, m)
            if best is None or key > best[0]:
                best = (key, tau, m)
    if best is not None and best[0][0] >= MIN_COVERAGE * sum(pops.values()):
        return {"bins": bins, "tau": best[1], "m": best[2]}
    # Nothing the corpus measures stays under the target: no value read from the text
    # alone is confident. The floor sits just above the highest logit the corpus produced,
    # so only evidence the corpus does not contain (a tag every track agrees on, say) can
    # clear it. min_logit says so in the file; it is not a tuned number.
    top = max((s["logit"] for s in samples if s["has_value"]), default=None)
    # No data at all (Stage has no gold in G1): never confident.
    return {"bins": bins, "tau": 0.0, "m": 0.0,
            "min_logit": NEVER if top is None else math.floor(top * 2) / 2 + 1.0}


def evaluate(model, samples):
    """{pop: {n, confident, cbw, rate}} of `model` on `samples`."""
    cnt = _counts(samples, model["bins"], model["tau"], model["m"], model.get("min_logit"))
    return {pop: {"n": c[0], "confident": c[1], "wrong": c[2],
                  "rate": (c[2] / c[0]) if c[0] else None}
            for pop, c in sorted(cnt.items())}


def cross_validate(samples, folds, k=5, train_target=TRAIN_TARGET):
    """Held-out result of fitting on k-1 folds and scoring the rest. `folds` is
    {group: fold} (assign_folds, computed once over the corpus so every field and the
    production-path replay see the same split). Returns (per-fold models,
    {pop: {n, confident, wrong, rate}})."""
    models, tot = [], {}
    for f in range(k):
        train = [s for s in samples if folds[s["group"]] != f]
        test = [s for s in samples if folds[s["group"]] == f]
        model = fit_model(train, train_target)
        models.append(model)
        for pop, r in evaluate(model, test).items():
            t = tot.setdefault(pop, {"n": 0, "confident": 0, "wrong": 0})
            t["n"] += r["n"]
            t["confident"] += r["confident"]
            t["wrong"] += r["wrong"]
    for t in tot.values():
        t["rate"] = t["wrong"] / t["n"] if t["n"] else None
    return models, dict(sorted(tot.items()))


def build_calibration(samples_by_field, folds, *, k=5, train_target=TRAIN_TARGET, corpus="G1",
                      snapshot=None, n_items=None, populations=(), notes=(),
                      corpus_note="G1 only. G3 does not exist yet; chunk 7 recalibrates on G1 plus G3.",
                      fold_key="gold artist"):
    """The calibration.json document: a final model per field, fitted on everything, with
    the held-out numbers beside it."""
    fields, folds_models = {}, {}
    for fname, samples in samples_by_field.items():
        models, held = cross_validate(samples, folds, k, train_target)
        final = fit_model(samples, train_target)
        folds_models[fname] = models
        fields[fname] = {"tau": final["tau"], "m": final["m"],
                         **({"min_logit": final["min_logit"]} if "min_logit" in final else {}),
                         "bins": final["bins"],
                         "n_scored": len({(s["item"], s["pop"]) for s in samples}),
                         "in_sample": evaluate(final, samples), "held_out": held}
    doc = {"version": 1, "fit_date": date.today().isoformat(), "corpus": corpus,
           "corpus_note": corpus_note,
           "snapshot": snapshot, "n_items": n_items, "populations": list(populations),
           "method": {"bins": "fixed edges on the logit, share right per bin made monotone (PAV)",
                      "edges": EDGES, "thresholds": "tau on the calibrated probability and m on the margin; "
                      "most right values kept confident with confident-but-wrong under the target in every population",
                      "folds": k, "fold_key": fold_key, "target": TARGET,
                      "train_target": train_target,
                      "wrong": "artist: wrong or partial; other fields: wrong"},
           "none_scores": dict(C.NONE_SCORE),
           "tables": {"date_weight": dict(C.DATE_WEIGHT), "agree_bonus": dict(C.AGREE_BONUS),
                      "agree_cap": C.AGREE_CAP},
           "unmeasured": list(notes),
           "fields": fields}
    return doc, folds_models


def write_calibration(doc, path=None):
    p = path or C.CALIBRATION_PATH
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write("\n")
