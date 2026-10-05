"""
Chunk 7b driver (Resolver v2, 2026-10-05): fit the decoder weights on G1 and G3, then
recalibrate the confidence thresholds, with every reported number held out.

    python3 -m app.utils.resolver_eval --fit-weights [--folds 10] [--out DIR] [--write]

The corpus is G1 plus G3 (both library modes, leave-one-out libraries as in the harness).
Folds group by act (the normalised gold artist, billing variants together), so one act is
never in both the fitting and the held-out side. For every fold:

    1. the weights are fitted on the other folds (reader/fit.py),
    2. the fold's items run through resolve() with those weights: these are the OUT-OF-FOLD
       logits and margins. Calibration is fitted on them alone, never on logits the weights
       were trained to produce.

The hand-set weights go through the same procedure (nothing to fit; they are only
recalibrated) so the comparison is like for like. Confident-but-wrong is then measured by
replaying every item through resolve() + verdict() with its fold's weights and the
thresholds fitted on the other calibration folds.

--write puts the all-data fit in reader/weights_fitted.py and the hand-set-weights thresholds in reader/calibration.json
(--no-fit skips the fits: calibration only). The shipped reader/weights.py is the hand-set table and is never written here.
"""
import json
import time
from pathlib import Path

from app.utils import resolver_eval as ev
from app.utils.reader import calibrate as cal
from app.utils.reader import confidence as conf
from app.utils.reader import decode as dec
from app.utils.reader import fit as F

TIERS = ("G1", "G3")
POPS = ("none", "loo")
READER_DIR = Path(__file__).resolve().parent / "reader"


class _Ctx:
    def __init__(self, corpus_dir, atlas, k):
        self.corpus_dir, self.atlas, self.k = corpus_dir, atlas, k
        self.items = {t: ev.load_tier(t, corpus_dir)[0] for t in TIERS}
        self.meta = {t: ev.load_tier(t, corpus_dir)[1] for t in TIERS}
        self.by_id = {t: {str(it["id"]): it for it in self.items[t]} for t in TIERS}
        self.group = {(t, str(it["id"])): F.act_group(it.get("gold")) for t in TIERS for it in self.items[t]}
        self.wfold = F.assign_folds(list(self.group.values()), k)
        self.cfold = cal.assign_folds(list(self.group.values()), 5)

    def ids(self, tier, pred):
        return {i for (t, i), g in self.group.items() if t == tier and pred(g)}


def _run_groups(ctx, groups, models, calibrations=None):
    """groups: {key: {tier: set of ids}}; models: {key: Model}; calibrations: {key: calibration
    dict} or None for the shipped file. -> {(tier, pop): [item results]}"""
    out = {}
    try:
        for key, per_tier in groups.items():
            dec.set_weights(*models[key].tables())
            if calibrations is not None:
                conf.set_calibration(calibrations[key])
            for tier, ids in per_tier.items():
                if not ids:
                    continue
                for pop in POPS:
                    rep = ev.run_tier(tier, "resolve", corpus_dir=ctx.corpus_dir, library=pop, atlas=ctx.atlas,
                                      only_ids=ids)
                    out.setdefault((tier, pop), []).extend(rep["items"])
    finally:
        dec.set_weights()
        conf.set_calibration(None)
    return out


_FIT = {}


def _fit_job(f):
    """One fold's fit (f >= 0: every fold but f) or the all-rows fit (f = -1). Reads _FIT, which the
    parent fills before forking."""
    s = _FIT
    train = [x for x, fo in zip(s["inst"], s["folds"]) if f < 0 or fo != f] + s["anchors"]
    m, info = F.fit(train, s["hs"], epochs=s["epochs"])
    return f, m.to_json(), info


def _weight_fold_groups(ctx):
    return {f: {t: ctx.ids(t, lambda g, f=f: ctx.wfold[g] == f) for t in TIERS} for f in range(ctx.k)}


def _samples(ctx, results):
    samples = {f: [] for f in conf.CALIBRATED_FIELDS}
    for (tier, pop), res in sorted(results.items()):
        for r in res:
            group = ctx.group[(tier, r["id"])]
            for f in conf.CALIBRATED_FIELDS:
                j = r["judgments"][f]
                if j is None:
                    continue
                a = r["assess"].get(f) or {}
                has = bool(a.get("has_value"))
                samples[f].append({"item": f"{tier}:{r['id']}", "group": group, "pop": f"{tier}:{pop}",
                                   "logit": a.get("logit") if has else 0.0,
                                   "margin": a.get("margin") if has else 0.0,
                                   "has_value": has, "wrong": ev.is_wrong(f, j)})
    return samples


def _summ(results):
    """Per (tier, pop): aggregate, confidence breakdown and review reasons."""
    out = {}
    for (tier, pop), res in sorted(results.items()):
        agg = ev.aggregate(res)
        n = len(res)
        out[f"{tier}:{pop}"] = {
            "n": n, "fields": agg["fields"], "auto_ingest": agg["auto_ingest"],
            "breakdown": ev.conf_breakdown(res), "reasons": dict(ev.review_reasons(res)),
            "review": agg["auto_ingest"]["n_with_verdict"] - agg["auto_ingest"]["n_ingested"],
        }
    return out


def _calibrate_and_replay(ctx, models, oof, train_target):
    """Fit calibration on the out-of-fold samples of `oof`, replay every item with its fold's
    weights and the thresholds fitted without its calibration fold. -> (doc, held-out summary)"""
    samples = _samples(ctx, oof)
    n_items = len({s["item"] for ss in samples.values() for s in ss})
    notes = ["tags and folder-artist evidence (reader/confidence.py TAGS_SCORE, FOLDER_ARTIST_SCORE, "
             "DATE_WEIGHT['tags']): neither corpus has tags or artist folders (G3 carries the artist folder in its "
             "metadata, which is the answer key and is never shown to the reader), so those weights are set, not fitted",
             "the confirmed-artist source (Artist.confirmed_at) is simulated: G1 library rows count as confirmed, "
             "and in G3 loo the other shows' artists do only with --g3-confirmed yes (one reviewed show per act)"]
    doc, fold_models = cal.build_calibration(
        samples, ctx.cfold, k=5, train_target=train_target, corpus="G1+G3",
        corpus_note="G1 plus G3, populations tier x library mode (none, loo), logits out-of-fold for the weights",
        fold_key="gold artist, billing variants together (_act_key)",
        snapshot=ctx.meta["G1"].get("snapshot_date"), n_items=n_items,
        populations=[f"resolve, {t}, library {p}, real Atlas" for t in TIERS for p in POPS], notes=notes)
    groups, mods, cals = {}, {}, {}
    for wf in range(ctx.k):
        for cf in range(5):
            key = (wf, cf)
            per = {t: ctx.ids(t, lambda g, wf=wf, cf=cf: ctx.wfold[g] == wf and ctx.cfold[g] == cf) for t in TIERS}
            if not any(per.values()):
                continue
            groups[key] = per
            mods[key] = models[wf]
            cals[key] = {"fields": {name: ms[cf] for name, ms in fold_models.items()}}
    held = _run_groups(ctx, groups, mods, cals)
    return doc, _summ(held)


class _Stop(Exception):
    """The time budget of one call is used up; run again to continue from the cache."""


def run(corpus_dir=None, atlas="real", k=10, out_dir=None, write=False, epochs=F.EPOCHS, train_target=None,
        budget=130, no_fit=False):
    """Resumable: every stage is cached under out_dir/cache, and the call stops once `budget`
    seconds are spent (exit code 3); run it again until it prints done. (The bridge ends a
    call after 180 s and takes its background processes with it.)"""
    import pickle
    import resource
    t0 = time.time()
    train_target = cal.TRAIN_TARGET if train_target is None else train_target
    ev.set_atlas_mode(atlas)
    ctx = _Ctx(corpus_dir, atlas, k)
    out = Path(out_dir or ".")
    cache = out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    log = lambda *a: print(f"[{time.time() - t0:6.0f}s {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024}MB]",
                           *a, flush=True)
    hs = F.Model.handset()

    def stage(name, fn):
        p = cache / f"{name}.pkl"
        if p.exists():
            return pickle.loads(p.read_bytes())
        if time.time() - t0 > budget:
            raise _Stop(name)
        v = fn()
        p.write_bytes(pickle.dumps(v))
        log("stage", name, "done")
        return v

    def fits():
        from app.atlas.lookup import current_atlas
        ev.set_library_mode("loo", corpus_dir=corpus_dir)
        inst, per_tier, tier_of = [], {}, {}
        for t in TIERS:
            i, s = F.build_instances(ctx.items[t], hs, library_for=ev.library_for,
                                     atlas=current_atlas() if atlas != "none" else None)
            inst += i
            for it in ctx.items[t]:
                for mode in ("loo", "none"):
                    tier_of[f"{it['id']}:{mode}"] = t
            per_tier[t] = {"items": len(ctx.items[t]), "instances": len(i), "skipped_items": len(set(s)),
                           "skipped_ids": sorted(set(s))}
        log("instances", len(inst), "skipped items", {t: v["skipped_items"] for t, v in per_tier.items()})
        fold_of = lambda rid: ctx.wfold[ctx.group[(tier_of[rid], rid.rsplit(":", 1)[0])]]
        anchors = F.anchor_instances(hs)        # hand-written cases pinned by tests: in every fold's training
        log("anchor instances", len(anchors))
        _FIT.update(inst=inst, folds=[fold_of(x[2]) for x in inst], anchors=anchors, hs=hs, epochs=epochs)
        jobs = list(range(k)) + [-1]            # -1: every row, the shipped fit
        try:
            import multiprocessing as mp
            with mp.get_context("fork").Pool(min(4, mp.cpu_count())) as pool:
                outs = pool.map(_fit_job, jobs)
        except (ValueError, OSError):           # no fork on this platform: one after the other
            outs = [_fit_job(j) for j in jobs]
        models = {f: m for f, m, _ in outs if f >= 0}
        infos = {f: i for f, _, i in outs if f >= 0}
        fm, finfo = next((F.Model.from_json(m), i) for f, m, i in outs if f < 0)
        return {"models": models, "infos": infos, "final": fm.to_json(), "final_info": finfo, "alignment": per_tier}
    groups = _weight_fold_groups(ctx)
    hs_models = {f: hs for f in range(k)}
    if no_fit:      # the hand-set weights ship: only the calibration is (re)built
        fitted = {"models": {}, "infos": {}, "final": hs.to_json(), "final_info": {}, "alignment": {}}
        models, final = hs_models, hs
        oof_hs = stage("oof_hs", lambda: _run_groups(ctx, groups, hs_models))
        oof_fit = oof_hs
        doc_hs, held_hs = stage("cal_hs", lambda: _calibrate_and_replay(ctx, hs_models, oof_hs, train_target))
        doc_fit, held_fit = doc_hs, held_hs
    else:
        fitted = stage("fits", fits)
        models = {int(f): F.Model.from_json(m) for f, m in fitted["models"].items()}
        final = F.Model.from_json(fitted["final"])
        oof_hs = stage("oof_hs", lambda: _run_groups(ctx, groups, hs_models))
        oof_fit = stage("oof_fit", lambda: _run_groups(ctx, groups, models))
        doc_hs, held_hs = stage("cal_hs", lambda: _calibrate_and_replay(ctx, hs_models, oof_hs, train_target))
        doc_fit, held_fit = stage("cal_fit", lambda: _calibrate_and_replay(ctx, models, oof_fit, train_target))

    def timed():
        dec.set_weights(*final.tables())
        conf.set_calibration(doc_hs)
        res = {}
        try:
            for t in TIERS:
                res[t] = ev.run_tier(t, "resolve", corpus_dir=corpus_dir, library="loo", atlas=atlas)["timing"]
        finally:
            dec.set_weights()
            conf.set_calibration(None)
        return res
    timing = stage("timing", timed)

    report = {
        "k": k, "epochs": epochs, "train_target": train_target, "alignment": fitted["alignment"],
        "fold_info": fitted["infos"], "final_info": fitted["final_info"],
        "oof_handset_current_calibration": _summ(oof_hs), "oof_fitted_current_calibration": _summ(oof_fit),
        "handset_recalibrated": held_hs, "fitted_recalibrated": held_fit,
        "top_changes": F.top_changes(hs, final, 15), "timing": timing,
        "thresholds_handset": {f: {"tau": m["tau"], "m": m["m"], **({"min_logit": m["min_logit"]} if "min_logit" in m else {})}
                               for f, m in doc_hs["fields"].items()},
        "thresholds_fitted": {f: {"tau": m["tau"], "m": m["m"], **({"min_logit": m["min_logit"]} if "min_logit" in m else {})}
                              for f, m in doc_fit["fields"].items()},
        "calibration_held_out_fitted": {f: m["held_out"] for f, m in doc_fit["fields"].items()},
        "calibration_held_out_handset": {f: m["held_out"] for f, m in doc_hs["fields"].items()},
    }
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    (out / "final_model.json").write_text(json.dumps(final.to_json()), encoding="utf-8")
    (out / "calibration_fitted.json").write_text(json.dumps(doc_fit, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "calibration_handset.json").write_text(json.dumps(doc_hs, indent=1, ensure_ascii=False), encoding="utf-8")
    if write:
        if not no_fit:
            F.write_weights(final, READER_DIR / "weights_fitted.py")
        cal.write_calibration(doc_hs)       # the shipped weights are the hand-set ones
        log("wrote reader/calibration.json" + ("" if no_fit else " and reader/weights_fitted.py"))
    log("done")
    return report


def main(args):
    ev.set_g3_confirmed(getattr(args, "g3_confirmed", "yes") == "yes")
    try:
        run(args.corpus, atlas="real", k=args.folds, out_dir=args.out, write=args.write, epochs=args.epochs,
            train_target=args.train_target, no_fit=getattr(args, "no_fit", False))
    except _Stop as stop:
        print(f"stopped before stage {stop}: time budget used, run the same command again")
        return 3
    return 0
