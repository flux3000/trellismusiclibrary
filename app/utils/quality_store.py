"""
utils/quality_store.py — the ONLY place that reads or writes Listening Quality rows.

Two jobs, both of which exist because getting them wrong is silent rather than
loud:

1. **NFC normalisation.**  macOS hands out decomposed (NFD) filenames; SQLite
   stores whatever it was given.  `"Lucía" != "Lucía"` byte-for-byte, so a raw
   path compare silently returns nothing for any accented name and the caller
   concludes "not analysed yet".  Every path in and out of these tables goes
   through `norm_path()` here.  Do not hand-roll a query against
   `QualityAnalysis.folder_path` elsewhere.

2. **Staging → permanent promotion.**  The staging key (folder path) stops
   existing the moment a Move ingest finishes, so the copy has to happen as part
   of the commit, not lazily afterwards.
"""

import json
import os
import unicodedata

from app.extensions import db
from app.models.quality import QualityAnalysis, RecordingQuality

# Triage vocabulary — kept here so the API, the UI and the tests all agree.
TRIAGE_PENDING  = "pending"
TRIAGE_ACCEPTED = "accepted"
TRIAGE_REJECTED = "rejected"
TRIAGE_STATUSES = (TRIAGE_PENDING, TRIAGE_ACCEPTED, TRIAGE_REJECTED)


def norm_path(path):
    """
    Canonical form for any path used as a database key.

    NFC-normalised and trailing-slash-stripped.  The normalisation is the
    load-bearing part (see module docstring); the slash strip just stops
    "/a/b" and "/a/b/" becoming two rows for one folder.
    """
    if not path:
        return path
    p = unicodedata.normalize("NFC", str(path))
    return p.rstrip("/") or "/"


def _dump(value):
    """JSON-encode a payload column, tolerating numpy scalars via default=str."""
    if value is None:
        return None
    return json.dumps(value, default=str)


def _load(text, fallback):
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return fallback


def _apply_scores(row, scored, features):
    """
    Copy an engine result onto a row carrying the score columns.

    `scored` is `score_recording()`'s output; `features` the raw feature dict.
    Shared by staging writes and permanent promotion so the two can never
    populate a different subset of columns.
    """
    row.listening_quality   = scored.get("listening_quality")
    row.score_tone          = scored.get("score_tone")
    row.score_noise         = scored.get("score_noise")
    row.score_dynamics      = scored.get("score_dynamics")
    row.technical_deduction = scored.get("technical_deduction") or 0.0
    row.score_version       = scored.get("score_version")

    row.technical_issues_json = _dump(scored.get("technical_issues") or [])
    row.flags_json            = _dump(scored.get("flags") or [])

    if features is not None:
        row.features_json    = _dump(features)
        row.sampled_json     = _dump(features.get("sampled"))
        row.analysis_version = features.get("analysis_version")
    return row


# ═════════════════════════════════════════════════════════════════════════════
# Staging (pre-ingest)
# ═════════════════════════════════════════════════════════════════════════════
def get_staging(folder_path):
    """One staging row by folder path, or None."""
    return (db.session.query(QualityAnalysis)
            .filter(QualityAnalysis.folder_path == norm_path(folder_path))
            .first())


def list_staging(source_dir):
    """
    Every NOT-YET-INGESTED staging row under one scanned directory, best
    score first.

    Rows that failed to analyse sort last rather than being hidden — a folder
    that errored is something the user needs to see, not something to quietly
    drop from the list.

    Excludes rows already promoted to a Recording (`recording_id` set) —
    Ryan, 2026-08-09: staging rows are keyed by folder path and deliberately
    outlive the folder (a Move ingest relocates the source out from under its
    own row), so re-triaging the SAME parent directory later — e.g. after
    ingesting one show from it and downloading three new ones — was
    resurfacing every old, already-ingested show right alongside the new
    ones, each showing "Source folder no longer exists on disk". Softening
    that to just skip the concern (same day, earlier fix) wasn't enough —
    an ingested row has nothing left to triage and should not be IN this
    list at all. It is still reachable from the library itself; this table's
    job ends the moment a folder becomes a Recording.
    """
    rows = (db.session.query(QualityAnalysis)
            .filter(QualityAnalysis.source_dir == norm_path(source_dir))
            .filter(QualityAnalysis.recording_id.is_(None))
            .all())
    return sorted(
        rows,
        key=lambda r: (r.listening_quality is None, -(r.listening_quality or 0)),
    )


def rescore_stored(dry_run=False):
    """
    Re-score every stored row from its CACHED features — no audio decode.

    This is the payoff of keeping extraction and scoring in separate modules,
    and until 2026-07-31 it was only a claim: `_is_current()` documented that a
    `score_version` bump "is handled by re-scoring stored features", but nothing
    actually did it, so a weight change left every existing row displaying a
    number from the old engine forever.

    Only rows whose `analysis_version` is current can be re-scored: an older
    extraction predates some feature the new scoring reads, and inventing a
    value for it would be worse than leaving the row alone. Those are reported
    as `stale_features` and need a real re-analysis.

    Returns a summary dict; `dry_run=True` reports without writing.
    """
    from app.utils.quality import (score_recording, guess_source_from_name,
                                   QUALITY_ANALYSIS_VERSION)

    out = {"rescored": 0, "stale_features": 0, "no_features": 0, "changed": []}

    for model in (QualityAnalysis, RecordingQuality):
        for row in db.session.query(model).all():
            feats = _load(row.features_json, None)
            if not feats:
                out["no_features"] += 1
                continue
            if row.analysis_version != QUALITY_ANALYSIS_VERSION:
                out["stale_features"] += 1
                continue

            # Staging rows know only a folder name; permanent rows can use the
            # Recording's own `source`, which is authoritative once ingested.
            source = None
            if isinstance(row, RecordingQuality) and row.recording is not None:
                source = row.recording.source
            if not source:
                source = guess_source_from_name(getattr(row, "name", None)
                                                or getattr(row, "folder_path", ""))

            before = row.listening_quality
            scored = score_recording(feats, source=source)
            if not dry_run:
                _apply_scores(row, scored, feats)
            after = scored.get("listening_quality")
            out["rescored"] += 1
            if before is not None and after is not None and abs(before - after) >= 0.05:
                out["changed"].append({
                    "id": row.id, "before": before, "after": after,
                    "name": getattr(row, "name", None),
                })

    if not dry_run:
        db.session.commit()
    return out


def upsert_staging(folder_path, *, source_dir=None, name=None,
                   scored=None, features=None, error=None):
    """
    Create or update the staging row for one folder.

    Re-analysis updates in place rather than inserting: the folder is the
    identity.  An existing triage decision is deliberately PRESERVED across
    re-analysis — the user rejected the recording, not the measurement, and
    silently resetting that to pending would throw away their judgement.
    """
    key = norm_path(folder_path)
    row = get_staging(key)
    if row is None:
        row = QualityAnalysis(folder_path=key, triage_status=TRIAGE_PENDING)
        db.session.add(row)

    if source_dir is not None:
        row.source_dir = norm_path(source_dir)
    row.name = name or os.path.basename(key)
    row.error = error

    if scored is not None:
        _apply_scores(row, scored, features)

    db.session.commit()
    return row


def set_triage(folder_path, status):
    """
    Accept / reject / reset one folder.  Returns the row, or None if unknown.

    Raises ValueError on an unrecognised status rather than writing junk into a
    column the UI filters on.
    """
    if status not in TRIAGE_STATUSES:
        raise ValueError(f"unknown triage status {status!r}; "
                         f"expected one of {TRIAGE_STATUSES}")
    row = get_staging(folder_path)
    if row is None:
        return None
    row.triage_status = status
    db.session.commit()
    return row


def prune_staging(source_dir=None, *, include_rejected=False):
    """
    Drop staging rows that have served their purpose.

    Rejections are KEPT by default: the point of remembering them is that a
    re-scan of the same directory doesn't re-offer material already turned down.
    Pass `include_rejected=True` to forget them entirely.
    """
    q = db.session.query(QualityAnalysis)
    if source_dir is not None:
        q = q.filter(QualityAnalysis.source_dir == norm_path(source_dir))
    if include_rejected:
        q = q.filter(QualityAnalysis.recording_id.isnot(None) |
                     (QualityAnalysis.triage_status == TRIAGE_REJECTED))
    else:
        q = q.filter(QualityAnalysis.recording_id.isnot(None))
    n = q.delete(synchronize_session=False)
    db.session.commit()
    return n


# ═════════════════════════════════════════════════════════════════════════════
# Permanent (post-ingest)
# ═════════════════════════════════════════════════════════════════════════════
def promote_to_recording(folder_path, recording_id, *, commit=True):
    """
    Copy a staging row's analysis onto the permanent per-recording row.

    Called from the ingest commit path.  MUST happen there and not later: after
    a Move ingest the source folder is gone, taking the staging row's key with
    it, so there is no second chance to make this association.

    Returns the RecordingQuality row, or None if the folder was never analysed
    (perfectly legal — ingest does not require a quality pass).
    """
    staging = get_staging(folder_path)
    if staging is None:
        return None

    if staging.listening_quality is None:
        # Never actually scored (e.g. bulk-ingested through Review & Ingest
        # with skip_analysis) -- back-link the staging row so a re-scan
        # still says "already ingested", but do NOT create a null-score
        # RecordingQuality row. enqueue_followups()'s "no RecordingQuality
        # row yet" outer join is how an unscored bulk-ingested recording
        # gets picked up for scoring; a null-score row would look scored
        # and the recording would never be queued (S4a).
        staging.recording_id = recording_id
        if commit:
            db.session.commit()
        return None

    row = (db.session.query(RecordingQuality)
           .filter(RecordingQuality.recording_id == recording_id)
           .first())
    if row is None:
        row = RecordingQuality(recording_id=recording_id)
        db.session.add(row)

    # Copy every score column across verbatim. Features come from the stored
    # JSON rather than being recomputed — the whole point is that no audio is
    # touched here.
    row.listening_quality     = staging.listening_quality
    row.score_tone            = staging.score_tone
    row.score_noise           = staging.score_noise
    row.score_dynamics        = staging.score_dynamics
    row.technical_deduction   = staging.technical_deduction
    row.features_json         = staging.features_json
    row.technical_issues_json = staging.technical_issues_json
    row.flags_json            = staging.flags_json
    row.sampled_json          = staging.sampled_json
    row.analysis_version      = staging.analysis_version
    row.score_version         = staging.score_version

    # Back-link the staging row so a re-scan can say "already ingested".
    staging.recording_id = recording_id

    if commit:
        db.session.commit()
    return row


def get_for_recording(recording_id):
    return (db.session.query(RecordingQuality)
            .filter(RecordingQuality.recording_id == recording_id)
            .first())


def upsert_for_recording(recording_id, scored, features):
    """
    Create or update the permanent row for an already-ingested recording,
    scored directly with no staging row to promote from.

    Bulk Ingest follow-up path (chunk 6): a bulk-ingested recording never
    went through triage, so there is no QualityAnalysis row for
    promote_to_recording() to copy -- this writes the same score columns
    straight onto the permanent row via the same _apply_scores() helper, so
    the two write paths can never populate a different subset of columns.
    """
    row = (db.session.query(RecordingQuality)
           .filter(RecordingQuality.recording_id == recording_id)
           .first())
    if row is None:
        row = RecordingQuality(recording_id=recording_id)
        db.session.add(row)
    _apply_scores(row, scored, features)
    db.session.commit()
    return row


# ═════════════════════════════════════════════════════════════════════════════
# Serialisation
# ═════════════════════════════════════════════════════════════════════════════
def serialize(row, *, include_features=False):
    """
    One shape for both tables, so the triage UI and the Fidelity tab can share
    a renderer.  `include_features` is opt-in because the raw feature dict is
    large and only the Advanced Metrics panel wants it.
    """
    if row is None:
        return None

    # verdict_band / predicted_grade are DERIVED here rather than stored as
    # columns. They are pure functions of the composite, so deriving them means
    # retuning a band threshold or the calibration constants takes effect
    # immediately across every existing row — no migration, no re-analysis, and
    # no chance of a stored band disagreeing with the score beside it.
    from app.utils.quality import verdict_band, predicted_grade

    out = {
        "listening_quality":   row.listening_quality,
        "verdict_band":        verdict_band(row.listening_quality),
        "predicted_grade":     (round(predicted_grade(row.listening_quality), 1)
                                if row.listening_quality is not None else None),
        "score_tone":          row.score_tone,
        "score_noise":         row.score_noise,
        "score_dynamics":      row.score_dynamics,
        "technical_deduction": row.technical_deduction or 0.0,
        "technical_issues":    _load(row.technical_issues_json, []),
        "flags":               _load(row.flags_json, []),
        "sampled":             _load(row.sampled_json, []),
        "analysis_version":    row.analysis_version,
        "score_version":       row.score_version,
    }

    # Staging-only fields.
    if isinstance(row, QualityAnalysis):
        out.update({
            "folder_path":   row.folder_path,
            "source_dir":    row.source_dir,
            "name":          row.name,
            "triage_status": row.triage_status,
            "recording_id":  row.recording_id,
            "error":         row.error,
        })
    else:
        out["recording_id"] = row.recording_id

    if include_features:
        out["features"] = _load(row.features_json, {})

    return out
