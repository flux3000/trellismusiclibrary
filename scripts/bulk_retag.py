"""
scripts/bulk_retag.py — rewrite every recording's FLAC tags under the
2026-09-16 taxonomy (see "Artist-Musician Rename and FLAC Tag Taxonomy — Design
Spec v1", §3).

A loop over write_flac_tags(), the same function the Write Tags button calls.
Nothing about WHAT gets written lives here.

Safety, in order of what matters:
  * Dry run is the default. It checks every folder and file exists and reports
    what a real run would touch; it writes nothing, to disk or database.
  * The audio is verified untouched: each file's STREAMINFO MD5 (the decoded-
    audio hash FFP is built from) is read before and after the write. A
    mismatch stops the run.
  * Resumable. Each finished recording gets a `tags_written` RecordingEvent,
    committed immediately, and recordings with one dated on or after --since
    are skipped. Ctrl-C loses at most the recording in progress.
  * Whole-file MD5s will no longer match. Accepted (Ryan, 2026-09-09).

Must run on the Mac with the library mounted; a Cowork session cannot write
the FLACs. Quit Trellis first so nobody edits metadata mid-run.

    python3 scripts/bulk_retag.py                  # dry run
    python3 scripts/bulk_retag.py --apply --limit 5
    python3 scripts/bulk_retag.py --apply
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mutagen.flac import FLAC

from app import create_app
from app.extensions import db
from app.models.recording import Recording
from app.models.recording_event import RecordingEvent
from app.models.user import User
from app.utils.ingest import write_flac_tags


def audio_md5s(recording, root):
    """{file_path: STREAMINFO md5} for every track; None where unreadable."""
    out = {}
    for t in recording.tracks:
        try:
            out[t.file_path] = FLAC(str(root / recording.folder_path / t.file_path)).info.md5_signature
        except Exception:
            out[t.file_path] = None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write tags (default: dry run)")
    ap.add_argument("--limit", type=int, help="stop after this many recordings")
    ap.add_argument("--since", default="2026-09-16",
                    help="skip recordings with a tags_written event on/after this date")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        root = Path(app.config["LIBRARY_ROOT"])
        if not root.is_dir():
            sys.exit(f"Library not reachable at {root}. Mount it first.")

        since = datetime.fromisoformat(args.since)
        done = {rid for (rid,) in db.session.query(RecordingEvent.recording_id)
                .filter(RecordingEvent.event_type == "tags_written",
                        RecordingEvent.created_at >= since)}
        owner = (db.session.query(User).filter_by(role="admin").order_by(User.id).first())
        if owner is None:
            sys.exit("No admin user to attribute the events to.")

        todo = [r for r in db.session.query(Recording)
                .filter(Recording.is_published.is_(True)).order_by(Recording.id)
                if r.id not in done]
        if args.limit:
            todo = todo[:args.limit]

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = Path(app.config["DB_PATH"]).resolve().parent / f"retag-report-{stamp}.jsonl"
        n_files = sum(len(r.tracks) for r in todo)
        print(f"{'APPLY' if args.apply else 'DRY RUN'} — library {root}")
        print(f"{len(done)} already retagged since {args.since}; {len(todo)} to do ({n_files} files)")
        print(f"report: {report_path}\n")

        totals = {"ok": 0, "partial": 0, "failed": 0, "missing": 0}
        started = time.time()
        with open(report_path, "w") as report:
            for i, rec in enumerate(todo, 1):
                folder = root / rec.folder_path
                missing = [t.file_path for t in rec.tracks if not (folder / t.file_path).is_file()]
                row = {"recording_id": rec.id, "folder": rec.folder_path,
                       "tracks": len(rec.tracks), "missing": missing}

                if not args.apply:
                    status = "missing" if missing else "ok"
                else:
                    before = audio_md5s(rec, root)
                    n, errors = write_flac_tags(rec, str(root))
                    after = audio_md5s(rec, root)
                    changed = [f for f in before if before[f] and before[f] != after.get(f)]
                    row.update(written=n, errors=errors, audio_changed=changed)
                    if changed:
                        report.write(json.dumps(row) + "\n")
                        sys.exit(f"\nSTOP: audio hash changed in recording {rec.id}: {changed}")
                    if n:
                        note = f"bulk retag: {n} file(s) written"
                        if errors:
                            note += f"; {len(errors)} error(s)"
                        db.session.add(RecordingEvent(recording_id=rec.id, user_id=owner.id,
                                                      event_type="tags_written", note=note))
                        db.session.commit()
                    status = "ok" if n and not errors else ("partial" if n else "failed")

                totals[status] += 1
                row["status"] = status
                report.write(json.dumps(row) + "\n")
                report.flush()
                if status != "ok" or i % 25 == 0 or i == len(todo):
                    rate = i / max(time.time() - started, 1e-6)
                    eta = (len(todo) - i) / rate / 60 if rate else 0
                    print(f"[{i}/{len(todo)}] {status:8} {rec.folder_path}"
                          + (f"  ({len(missing)} missing)" if missing else "")
                          + f"  ~{eta:.0f} min left")

        print(f"\n{totals}")
        if not args.apply:
            print("Dry run. Nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main()
