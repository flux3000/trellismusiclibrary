"""
scripts/batch_analyze.py — Run Librosa analysis on every track in the library.

Skips tracks that already have a current-version analysis row.
Continues past individual failures so one bad file doesn't stall the whole run.

Usage:
    cd ~/Workshop/dev
    env/bin/python3 scripts/batch_analyze.py

Options (env vars):
    REANALYZE=1   — reprocess even tracks that already have analysis data
    DRY_RUN=1     — list what would be processed, don't actually run Librosa
"""

import os
import sys
import time
from pathlib import Path

# ── Bootstrap Flask app context ───────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from app import create_app
from app.extensions import db
from app.models.recording import Recording
from app.models.track import Track
from app.utils.analysis import (analyse_and_store_track, ANALYSIS_VERSION,
                                score_recording_non_music)
from config import Config
from datetime import datetime, timezone

REANALYZE = os.environ.get("REANALYZE", "0") == "1"
DRY_RUN   = os.environ.get("DRY_RUN",   "0") == "1"

app = create_app()


def run():
    library_root = str(Config.LIBRARY_ROOT)

    with app.app_context():
        recordings = (
            db.session.query(Recording)
            .order_by(Recording.id)
            .all()
        )

        total_recordings = len(recordings)
        total_tracks     = sum(len(r.tracks) for r in recordings)

        print(f"\n{'='*60}")
        print(f"  Flux Audio — Batch Analysis")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")
        print(f"  Library root : {library_root}")
        print(f"  Recordings   : {total_recordings}")
        print(f"  Tracks       : {total_tracks}")
        print(f"  Reanalyze    : {'yes (forced)' if REANALYZE else 'no (skip existing)'}")
        print(f"  Dry run      : {'yes' if DRY_RUN else 'no'}")
        print(f"{'='*60}\n")

        if DRY_RUN:
            for rec in recordings:
                print(f"  [{rec.id}] {rec.folder_path}  ({len(rec.tracks)} tracks)")
            print(f"\nDry run complete — {total_tracks} tracks would be processed.")
            return

        t_start        = time.time()
        track_ok       = 0
        track_skipped  = 0
        track_failed   = 0
        rec_count      = 0

        for rec in recordings:
            rec_count += 1
            folder_abs = os.path.join(library_root, rec.folder_path)
            print(f"\n[{rec_count}/{total_recordings}] {rec.folder_path}")

            if not os.path.isdir(folder_abs):
                print(f"  ⚠  Folder missing on disk — skipping all {len(rec.tracks)} tracks")
                track_failed += len(rec.tracks)
                continue

            for track in rec.tracks:
                abs_path = os.path.join(folder_abs, track.file_path)

                # The analyse + version-check + upsert logic lives in
                # app/utils/analysis.py (extracted 2026-08-07) so this script,
                # analyse_recording(), and the quality backfill all share one
                # copy and cannot drift as track_analysis gains columns.
                print(f"  ⏳ analyzing  {track.file_path}", end="", flush=True)
                t0 = time.time()
                status = analyse_and_store_track(track, abs_path, db.session,
                                                 reanalyze=REANALYZE)
                elapsed = time.time() - t0

                if status == "ok":
                    track_ok += 1
                    print(f"  ← ok ({elapsed:.1f}s)")
                elif status == "skipped":
                    track_skipped += 1
                    print("  ← skip (current)")
                elif status == "missing":
                    track_failed += 1
                    print("  ← MISSING on disk")
                else:
                    track_failed += 1
                    print(f"  ← FAILED ({elapsed:.1f}s)")

            # The non-music score is a comparison ACROSS this recording's
            # tracks, so it can only be computed once they have all been
            # written — analyse_recording() does the same thing at the same
            # point. Without this the script silently desynchronises the data:
            # store_track_analysis overwrites spectral_flatness and duration_s
            # with full-track values while non_music_score keeps a number
            # derived from the windowed ones it just replaced. Cheap enough to
            # run unconditionally (pure arithmetic over stored columns), which
            # also backfills every pre-existing recording on the first sweep.
            score_recording_non_music(rec, db.session)

        elapsed_total = time.time() - t_start
        mins, secs    = divmod(int(elapsed_total), 60)

        print(f"\n{'='*60}")
        print(f"  Done in {mins}m {secs}s")
        print(f"  Analyzed : {track_ok}")
        print(f"  Skipped  : {track_skipped}  (already current)")
        print(f"  Failed   : {track_failed}")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    run()
