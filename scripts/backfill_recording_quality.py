"""
scripts/backfill_recording_quality.py — analysis + quality for the back catalogue.

Two backfills in one pass, because both decode the same audio off the same NAS
and doing them separately means walking 544 folders twice:

  1. LIBROSA TRACK ANALYSIS (`track_analysis`) — the waveform peaks, RMS, noise
     floor, spectral cutoff, BPM. Early uploads skipped this: 286 of 9,243
     tracks have no row at all (14 recordings entirely unanalysed), and 776
     rows are still `analysis_version` 1 across 49 recordings. The waveform in
     particular is load-bearing — it feeds the recording page's wavesurfer
     banner, so a recording missing it renders a blank strip.

  2. LISTENING QUALITY (`recording_quality`) — the 1–100 listenability score.
     The engine only ever ran at INGEST time (triage → `quality_analysis`
     staging → `promote_to_recording`), so a row exists only for recordings
     ingested since that integration landed: 13 of 544 as of 2026-08-07.

Why (2) matters beyond tidiness: the Browse view's Recommended module draws
from `quality IN ('A','A+')` — 67 recordings Ryan graded by hand, which is a
selection-biased pool (you grade what you already care about, so the module
recommends your own curation back at you). A populated `recording_quality`
widens that pool to anything the engine scores GREEN, including recordings
nobody has listened to yet.

    python3 scripts/backfill_recording_quality.py --dry-run   # report only
    python3 scripts/backfill_recording_quality.py             # run both passes
    python3 scripts/backfill_recording_quality.py --limit 20  # try a slice
    python3 scripts/backfill_recording_quality.py --reanalyze # ignore existing
    python3 scripts/backfill_recording_quality.py --skip-analysis  # quality only
    python3 scripts/backfill_recording_quality.py --skip-quality   # librosa only

Resumable by design — safe to Ctrl-C and re-run. Each track and each recording
commits on its own (long-running analysis must not hold a SQLite write lock open
across the whole batch), and anything already carrying the current version is
skipped, so a resumed run picks up exactly where it stopped.

The two passes are independent: a recording whose librosa analysis fails still
gets a quality score, and vice versa. They read the same files but share no
code — `analysis.py` is librosa, `utils/quality/` deliberately is not.

TWO DELIBERATE DIFFERENCES from the triage-time path in `api/quality.py`:

  1. Source comes from `recording.source` (a curated DB field) rather than
     `guess_source_from_name()` on the folder. Triage has to guess because no
     Recording row exists yet; here one does. Source is the strongest single
     predictor in the model (CV r = +0.314 alone), so using the real value
     instead of a regex over a folder name is a straight accuracy gain.
  2. Failures are reported, never written. `recording_quality` has no error
     column (`quality_analysis` does, because the triage UI needs to explain an
     empty card). A recording that won't decode simply stays unscored, which
     reads correctly everywhere — "no score yet" is already the majority state.

Runtime ≈ 2 s/recording plus NAS read time, so budget 20–40 min for a full run
against /Volumes/music.
"""

import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.extensions import db
from app.models.recording import Recording
from app.models.quality import RecordingQuality
from app.models.track_analysis import TrackAnalysis
from app.utils import quality_store as qs
from app.utils.analysis import (analyse_and_store_track, ANALYSIS_VERSION,
                                score_recording_non_music)
from config import Config


def _is_current(row):
    """
    True if this row was produced by today's extractor AND today's scorer.

    Both constants are checked because they gate two different kinds of
    recompute: a stale `score_version` can be fixed by `rescore_quality.py`
    with no audio decode, while a stale `analysis_version` means the row
    predates a feature the current scoring reads and needs a real re-analysis —
    which is what this script does.
    """
    from app.utils.quality import (QUALITY_ANALYSIS_VERSION,
                                   QUALITY_SCORE_VERSION)
    return (row is not None
            and row.listening_quality is not None
            and str(row.analysis_version) == str(QUALITY_ANALYSIS_VERSION)
            and str(row.score_version) == str(QUALITY_SCORE_VERSION))


def _tracks_pending(recording, reanalyze=False):
    """
    Tracks in this recording lacking a current-version `track_analysis` row.

    Counted up front so the header can report the real workload, and so a
    recording that needs neither pass can be skipped without touching the NAS.
    """
    if reanalyze:
        return list(recording.tracks)
    done = {
        r.track_id for r in
        db.session.query(TrackAnalysis.track_id)
        .filter(TrackAnalysis.analysis_version == ANALYSIS_VERSION)
        .filter(TrackAnalysis.track_id.in_([t.id for t in recording.tracks]))
        .all()
    } if recording.tracks else set()
    return [t for t in recording.tracks if t.id not in done]


def _analyse(recording, folder_abs):
    """
    Score one recording. Returns (scored, features) or raises.

    Imported lazily — numpy/scipy/soundfile are heavyweight and only this path
    needs them.
    """
    from app.utils.quality import (extract_recording_features, score_recording,
                                   guess_source_from_name)

    features = extract_recording_features(folder_abs)
    if "error" in features:
        raise RuntimeError(features["error"])

    # Real DB source first; fall back to the folder-name guess only when the
    # field is blank (24 recordings are NULL, 13 are empty string). An
    # unreadable source is neutral in the model, not a penalty, so a miss here
    # costs accuracy but never fabricates a verdict.
    source = (recording.source or "").strip() or guess_source_from_name(
        Path(folder_abs).name)

    return score_recording(features, source=source), features


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be analysed without decoding audio")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N recordings (for a trial slice)")
    ap.add_argument("--reanalyze", action="store_true",
                    help="redo work that is already current")
    ap.add_argument("--skip-analysis", action="store_true",
                    help="skip the librosa/waveform pass")
    ap.add_argument("--skip-quality", action="store_true",
                    help="skip the listening-quality pass")
    args = ap.parse_args()

    do_analysis = not args.skip_analysis
    do_quality  = not args.skip_quality
    if not (do_analysis or do_quality):
        print("Nothing to do — both passes skipped.")
        return

    app = create_app()
    library_root = str(Config.LIBRARY_ROOT)

    with app.app_context():
        recordings = db.session.query(Recording).order_by(Recording.id).all()

        # Partition first so the header can state the real workload rather than
        # discovering it 300 folders in. A recording lands in `todo` if EITHER
        # pass has something to do for it.
        todo, skipped, pending_tracks = [], 0, 0
        for rec in recordings:
            need_q = do_quality and not _is_current(qs.get_for_recording(rec.id))
            tracks = _tracks_pending(rec, args.reanalyze) if do_analysis else []
            if args.reanalyze:
                need_q = do_quality
            if not need_q and not tracks:
                skipped += 1
                continue
            pending_tracks += len(tracks)
            todo.append((rec, tracks, need_q))

        if args.limit:
            todo = todo[:args.limit]
            pending_tracks = sum(len(t) for _, t, _ in todo)

        print(f"\n{'=' * 64}")
        print("  Flux Audio — analysis + listening-quality backfill")
        print(f"{'=' * 64}")
        print(f"  Library root    : {library_root}")
        print(f"  Recordings      : {len(recordings)}")
        print(f"  Nothing to do   : {skipped}")
        print(f"  To process      : {len(todo)}")
        print(f"  Tracks to analyse: {pending_tracks}"
              if do_analysis else "  Librosa pass    : skipped")
        print(f"  Quality pass    : {'on' if do_quality else 'skipped'}")
        print(f"  Mode            : {'DRY RUN' if args.dry_run else 'write'}")
        print(f"{'=' * 64}\n")

        if args.dry_run:
            for rec, tracks, need_q in todo[:40]:
                bits = []
                if tracks:
                    bits.append(f"{len(tracks)} tracks")
                if need_q:
                    bits.append("quality")
                print(f"  #{rec.id:<4} {', '.join(bits):<20} {rec.folder_path}")
            if len(todo) > 40:
                print(f"  … and {len(todo) - 40} more")
            return

        q_ok = q_failed = 0
        t_ok = t_skipped = t_missing = t_failed = 0
        missing_folder = 0
        started = time.time()

        for n, (rec, tracks, need_q) in enumerate(todo, 1):
            folder_abs = str(Path(library_root) / rec.folder_path)
            label = (f"[{n}/{len(todo)}] #{rec.id} "
                     f"{Path(rec.folder_path).name[:56]}")
            print(f"  {label}")

            if not Path(folder_abs).is_dir():
                # Not a failure worth alarm — a NAS that isn't mounted looks
                # exactly like this, and so does a folder renamed outside Flux.
                print("      ✗ folder not found")
                missing_folder += 1
                continue

            # ── Pass 1: librosa / waveform ───────────────────────────────────
            if tracks:
                counts = {"ok": 0, "skipped": 0, "missing": 0, "failed": 0}
                t0 = time.time()
                for track in tracks:
                    abs_path = str(Path(folder_abs) / track.file_path)
                    try:
                        status = analyse_and_store_track(
                            track, abs_path, db.session,
                            reanalyze=args.reanalyze)
                    except Exception as e:  # noqa: BLE001
                        db.session.rollback()
                        print(f"      ! {track.file_path}: {e}")
                        status = "failed"
                    counts[status] += 1

                t_ok      += counts["ok"]
                t_skipped += counts["skipped"]
                t_missing += counts["missing"]
                t_failed  += counts["failed"]
                detail = "  ".join(f"{k} {v}" for k, v in counts.items() if v)
                print(f"      ♪ analysis: {detail}  ({time.time() - t0:.0f}s)")

                # Recording-level, so it goes here and not in the track loop —
                # same reason analyse_recording() calls it after its own loop.
                # Runs even when every track was skipped, which is what
                # backfills non_music_score across a library whose rows were
                # written before the column existed.
                score_recording_non_music(rec, db.session)

            # ── Pass 2: listening quality ────────────────────────────────────
            if need_q:
                try:
                    t0 = time.time()
                    scored, features = _analyse(rec, folder_abs)

                    row = qs.get_for_recording(rec.id)
                    if row is None:
                        row = RecordingQuality(recording_id=rec.id)
                        db.session.add(row)
                    qs._apply_scores(row, scored, features)
                    db.session.commit()   # per recording — see docstring

                    lq = scored.get("listening_quality")
                    print(f"      ✓ quality: {lq:.0f}  ({rec.source or '—'})"
                          f"  {time.time() - t0:.1f}s")
                    q_ok += 1
                except Exception as e:  # noqa: BLE001
                    db.session.rollback()
                    print(f"      ✗ quality: {e}")
                    q_failed += 1

        elapsed = time.time() - started
        print(f"\n{'=' * 64}")
        if do_analysis:
            print(f"  tracks   — analysed {t_ok} · skipped {t_skipped} · "
                  f"missing {t_missing} · failed {t_failed}")
        if do_quality:
            print(f"  quality  — scored {q_ok} · failed {q_failed}")
        print(f"  folders missing on disk: {missing_folder}")
        print(f"  {elapsed / 60:.1f} min total")
        print(f"{'=' * 64}\n")
        print("  Verify:")
        print("    select count(*) from recording_quality;")
        print("    select analysis_version, count(*) from track_analysis "
              "group by 1;\n")


if __name__ == "__main__":
    main()
