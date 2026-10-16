"""
api/recordings.py — Recording endpoints.

Routes:
  GET  /api/recordings/<id>                 full recording detail (incl. analysis)
  GET  /api/recordings/favorites            starred recordings, for the sidebar
  POST /api/recordings/scan                 scan a folder, return suggestions (no DB write)
  PUT  /api/recordings/<id>                update recording metadata
  POST /api/recordings/<id>/write-tags     write Vorbis comments to FLAC files
  POST /api/recordings/<id>/info-file      save info-file text (DB, + disk if a .txt exists)
  DELETE /api/recordings/<id>[?delete_files=1]  delete the record, optionally the folder too
  POST /api/recordings/<id>/move           move the folder to Workshop/Backlog (from library, queue, workshop, backlog)
  POST /api/recordings/<id>/trash          Queue/Workshop/Backlog: folder to system Trash, then delete the record
  GET  /api/recordings/<id>/review         Review form data for a non-library recording
  PUT  /api/recordings/<id>/draft          Review form autosave (confirms nothing unless confirm:true)
  POST /api/recordings/open-folder         read a Workshop/Backlog folder with no recording, return its id
  POST /api/recordings/move-folder         move a folder row with no recording (reads it first)
  POST /api/recordings/<id>/reprocess      Analyze Audio: score + signals + librosa, any recording
  POST /api/recordings/<id>/verify-checksums  (re-)validate fingerprint checksums
"""

import json as _json
import os
import random
import subprocess
from datetime import datetime, timezone, date as _date
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from sqlalchemy.orm import selectinload

from app.utils.engineers import recording_engineer_payload, apply_engineer_names
from app.extensions import db
from app.models.recording import Recording, RecordingFingerprint
from app.models.collection import CollectionRecording
from app.models.recording_event import RecordingEvent
from app.models.bulk_ingest import BulkIngestItem
from app.models.track import Track
from app.models.performance import Performance
from app.models.artist import Artist
from app.models.play_log import PlayLog
from app.models.venue import Venue
from app.utils.ingest import (build_scan_payload, write_flac_tags, read_recording_tags,
                              desired_track_tags)
from app.utils.folder_naming import rename_recording_folder, unique_file_name
from app.utils import node_settings
from app.utils.paths import location_root, recording_folder_abs, queue_item_for
from app.utils.file_naming import rename_plan, TemplateError
from app.api.system import require_library
from app.utils.pruning import prune_after_recording_delete
from app.utils.serialize import (recording_row, derive_set_track_numbers, show_card_extras,
                                 _primary_recording_image_url)
from app.utils.paula import compute_paula_score
from app.utils.checksums import (
    discover_fingerprint_files, parse_checksum_file,
    match_entries_to_tracks, verify_track_checksum, FINGERPRINT_TYPE_PRIORITY,
    ChecksumMatchProxy,
)
from app.utils import musicbrainz
from app.models.recording_image import RecordingImage
from app.utils import entity_images as ei
from app import lomax
from app.utils.sources_plain import with_sources_plain as _sources_plain

bp = Blueprint("recordings", __name__)


# ── GET /api/recordings/recent ────────────────────────────────────────────────
# Virtual "Recently Added" view — not a stored grouping, just the N newest
# recordings by ingest timestamp. Always exactly correct, nothing to keep in sync.
#
# Shared by two callers: the List view's Recently Added table (no waveform —
# default, unaffected) and the Browse view's Recently Added card module
# (?waveform=1). Same endpoint, opt-in param, per the recording_row() design —
# see app/utils/serialize.py.

def _card_eager(query):
    """
    Eager-load everything `recording_row(card=True)` walks.

    Without this each card row lazy-loads Performance → Artist → Genre and
    → ArtistImage separately, which is an N+1 the moment a module shows more
    than a couple of cards. Defined once so every card-bearing endpoint loads
    the same set — the serializer's docstring names this as the caller's
    responsibility, and three copies of it would eventually disagree.
    """
    return query.options(
        selectinload(Recording.performance)
        .selectinload(Performance.artist)
        .selectinload(Artist.images),
        selectinload(Recording.performance)
        .selectinload(Performance.artist)
        .selectinload(Artist.genre),
        # Recording-level artwork -- image_url on every row is now
        # unconditional (not gated behind card=True), so this
        # eager-load has to ride along with every card-bearing query
        # too, same N+1 reasoning as the two above.
        selectinload(Recording.images),
        selectinload(Recording.performance)
        .selectinload(Performance.venue)
        .selectinload(Venue.images),
    )


def _image_chain_eager():
    """
    Eager-load what the image chain walks (own images, else the artist's --
    see serialize._primary_recording_image_url). recording_row() already
    reads Performance and Artist for the name, so loading them up front costs
    nothing extra and saves a lazy ArtistImage query per row.
    """
    return (
        selectinload(Recording.images),
        selectinload(Recording.performance)
        .selectinload(Performance.artist)
        .selectinload(Artist.images),
        selectinload(Recording.performance)
        .selectinload(Performance.venue)
        .selectinload(Venue.images),
    )


@bp.route("/card-extras")
@login_required
def card_extras():
    """Show-card extras for a page of list rows, so Card view can fill in what
    the flat list rows do not carry. Published recordings only."""
    ids = []
    for part in (request.args.get("ids") or "").split(",")[:200]:
        try:
            ids.append(int(part))
        except ValueError:
            continue
    if not ids:
        return jsonify({})
    recs = Recording.query.filter(Recording.id.in_(ids),
                                  Recording.is_published.is_(True)).all()
    return jsonify({str(r.id): show_card_extras(r) for r in recs})


@bp.route("/recent")
@login_required
def recent_recordings():
    limit = request.args.get("limit", 50, type=int) or 50
    limit = max(1, min(limit, 200))
    offset = max(0, request.args.get("offset", 0, type=int) or 0)
    waveform = request.args.get("waveform", "").lower() in ("1", "true", "yes")
    # `card=1` adds genre colour + primary image for Browse's Recently Added
    # row cards. Opt-in for the same reason as waveform: this endpoint also
    # backs the List view's flat table, which needs none of it.
    card = request.args.get("card", "").lower() in ("1", "true", "yes")
    query = Recording.query.options(*_image_chain_eager())
    if waveform:
        query = query.options(selectinload(Recording.tracks).selectinload(Track.analysis))
    if card:
        query = _card_eager(query)
    # Reverse chronological by ingest time — most recently added first. This is
    # the module's whole premise, so it is the query's order, not something the
    # client re-sorts.
    # A recording moved out to Backlog or Workshop keeps its row but is not
    # on the shelf (Ryan, 2026-10-13): it leaves Recently Added, Browse,
    # Albums and the Top Shelf.
    recs = (query.filter(Recording.is_published.is_(True))
            .order_by(Recording.created_at.desc()).offset(offset).limit(limit).all())
    return jsonify([recording_row(r, waveform=waveform, card=card) for r in recs])


# ── GET /api/recordings/recommended ───────────────────────────────────────────
# Browse view's "Recommended" module. Randomly-selected high-quality (A/A+ only
# — not A-) recordings, weighted toward ones absent from play_log, diverse by
# Artist (hard rule) and Genre (soft preference, degrades silently while
# genre_id is NULL/absent). Seeded by date so picks are stable within a day; the
# client's "Show me three more" control advances `reroll` to get a fresh draw.

def _recommended_pool_query():
    return (
        Recording.query
        .filter(Recording.quality.in_(("A", "A+")), Recording.kind == "live",
                Recording.is_published.is_(True))
        .options(
            selectinload(Recording.tracks).selectinload(Track.analysis),
            selectinload(Recording.performance),
        )
    )


def _genre_by_artist(artist_ids):
    """
    Best-effort {artist_id: genre_id} map for the diversity preference.
    Returns {} (no genre preference applied) if the query fails for any
    reason — in particular, `artist.genre_id` may not exist yet on the
    live DB (the genre migration had not been run as of this feature's
    build). Diversity-by-genre is a soft preference, never load-bearing, so
    failing this open (no genre data) rather than raising is the right call.
    """
    if not artist_ids:
        return {}
    try:
        return dict(
            db.session.query(Artist.id, Artist.genre_id)
            .filter(Artist.id.in_(artist_ids))
            .all()
        )
    except Exception:
        db.session.rollback()
        return {}


def _select_diverse(candidates, limit, perf_by_rec, genre_by_artist):
    """
    Greedily pick up to `limit` recordings from `candidates` (already ordered
    by preference — unplayed first). Never two picks share an Artist — a
    hard rule, no exception: if the pool doesn't have `limit` distinct
    Artists to offer, the draw comes back short rather than repeating one.
    Prefers distinct Genres too, but relaxes that if the pool can't support
    it (soft preference, degrades silently while genre_id is NULL/absent).
    """
    picks = []
    used_artists = set()
    used_genres = set()
    deferred = []   # distinct-artist, but genre already used this draw

    for r in candidates:
        if len(picks) >= limit:
            break
        pid = perf_by_rec.get(r.id)
        if pid is not None and pid in used_artists:
            continue
        gid = genre_by_artist.get(pid) if pid is not None else None
        if gid is not None and gid in used_genres:
            deferred.append((r, pid, gid))
            continue
        picks.append(r)
        if pid is not None:
            used_artists.add(pid)
        if gid is not None:
            used_genres.add(gid)

    if len(picks) < limit:
        for r, pid, gid in deferred:
            if len(picks) >= limit:
                break
            if pid is not None and pid in used_artists:
                continue
            picks.append(r)
            if pid is not None:
                used_artists.add(pid)

    return picks[:limit]


def _diverse_sequence(candidates, perf_by_rec, genre_by_artist):
    """The WHOLE pool, ordered so no Artist comes round again until every
    other Artist has had a turn.

    This is what makes the Top Shelf a record bin rather than a single draw of
    six: the strip is now flipped through one tile at a time, left to right,
    until the pool runs out. A cursor into a stable sequence is the right shape
    for that — the alternative, asking the server for "one more, excluding
    these 47 ids", grows the request with every click and gives the diversity
    rule nothing to reason about.

    Built as repeated rounds of `_select_diverse`, so **round one is
    byte-for-byte the old six-tile draw** and the existing behaviour (and its
    tests) are a prefix of the new sequence rather than a special case of it.
    Rounds after the first relax nothing: each is again at most one recording
    per Artist.
    """
    seq = []
    remaining = list(candidates)
    while remaining:
        round_picks = _select_diverse(remaining, len(remaining),
                                      perf_by_rec, genre_by_artist)
        if not round_picks:
            break                      # nothing pickable; stop rather than spin
        seq.extend(round_picks)
        picked = {r.id for r in round_picks}
        remaining = [r for r in remaining if r.id not in picked]
    return seq


@bp.route("/recommended")
@login_required
def recommended_recordings():
    limit = request.args.get("limit", 3, type=int) or 3
    limit = max(1, min(limit, 24))
    reroll = request.args.get("reroll", 0, type=int) or 0
    # Cursor into the ordered sequence. Absent or 0 reproduces the old
    # single-draw behaviour exactly, which is why nothing else had to change:
    # offset 0 IS the first round of `_diverse_sequence`. Past the end of the
    # pool this returns `[]`, and that empty answer is what tells the Top Shelf
    # its bin is empty and the right-hand chevron should go.
    offset = max(0, request.args.get("offset", 0, type=int) or 0)

    # The viewer's own Top Shelf criteria (Settings), one pool. Only a
    # recording there through SBD/FM alone and played in the last 90 days is
    # moved to the back (app/utils/top_shelf.py).
    from app.utils import top_shelf
    base = _card_eager(Recording.query.options(
        selectinload(Recording.tracks).selectinload(Track.analysis),
        selectinload(Recording.performance)))
    pool, played_ids = top_shelf.pool(getattr(current_user, "id", None), base)
    if not pool:
        return jsonify([])

    perf_by_rec = {r.id: (r.performance.artist_id if r.performance else None) for r in pool}
    genre_by_artist = _genre_by_artist(
        {pid for pid in perf_by_rec.values() if pid is not None}
    )

    # Stable within a day; `reroll` (an incrementing counter kept client-side,
    # not persisted) is the escape hatch for "Show me three more".
    seed = f"{_date.today().isoformat()}:{reroll}"
    rnd = random.Random(seed)
    unplayed = [r for r in pool if r.id not in played_ids]
    played   = [r for r in pool if r.id in played_ids]
    rnd.shuffle(unplayed)
    rnd.shuffle(played)
    ordered = unplayed + played   # demoted, never excluded

    if offset:
        picks = _diverse_sequence(ordered, perf_by_rec,
                                  genre_by_artist)[offset:offset + limit]
    else:
        # The common case — the first paint — still runs the single-round
        # picker. Building the whole sequence to take the first six of it
        # would be the same answer for more work on every Browse load.
        picks = _select_diverse(ordered, limit, perf_by_rec, genre_by_artist)
    # card=True unconditionally — unlike /recent, this endpoint has exactly one
    # consumer (the Recommended module), so there is no flat-list caller to
    # protect and no reason to make it a parameter.
    return jsonify([recording_row(r, card=True) for r in picks])


# ── GET /api/recordings/on-this-day ───────────────────────────────────────────
# Browse view's "On This Day" module. Recordings whose performance date matches
# today's month/day, any year — no schema change, just a date-part match.
# Compact list, not cards, so no waveform. Hidden client-side when empty, which
# is most days.

@bp.route("/on-this-day")
@login_required
def on_this_day():
    # The client passes its OWN month/day. Using the server's UTC date was
    # wrong for anyone west of Greenwich: after 17:00 Pacific, UTC is already
    # tomorrow, so the module showed tomorrow's shows. There is no correct
    # server-side answer, because "today" is a property of where the reader is
    # standing. UTC stays as the fallback for a caller that says nothing.
    month = request.args.get("month", type=int)
    day   = request.args.get("day", type=int)
    if not (month and day and 1 <= month <= 12 and 1 <= day <= 31):
        today = datetime.now(timezone.utc).date()
        month, day = today.month, today.day
    recs = (
        Recording.query
        .options(*_image_chain_eager())
        .join(Performance, Recording.performance_id == Performance.id)
        .filter(Performance.start_month == month,
                Performance.start_day == day)
        .order_by(Performance.start_year.asc().nullslast())
        .all()
    )
    return jsonify([recording_row(r) for r in recs])


# ── GET /api/recordings/kind-counts ───────────────────────────────────────────
# {"live": n, "studio": n} over the whole library. Powers the Albums nav entry /
# page existence check: a live-only collector must not pay for or see an entry
# point to an empty page. ONE GROUP BY, not a per-row scan -- this can run on
# every sidebar render alongside list_artists above, which is held to the same
# bar.

@bp.route("/kind-counts")
@login_required
def kind_counts():
    rows = (
        db.session.query(Recording.kind, db.func.count(Recording.id))
        .filter(Recording.is_published.is_(True))
        .group_by(Recording.kind)
        .all()
    )
    counts = {"live": 0, "studio": 0}
    for kind, n in rows:
        if kind in counts:
            counts[kind] = n
    return jsonify(counts)


# ── GET /api/recordings/<id> ──────────────────────────────────────────────────

@bp.route("/<int:recording_id>")
@login_required
def get_recording(recording_id):
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    def _analysis(ta):
        """
        Serialise a TrackAnalysis row, or None if the full analysis has not run.

        A "signals" row is a PARTIAL written during ingest — it carries the
        non-music measurements and nothing else. Returning it here would hand
        the UI a dict of nulls and an empty waveform for a recording whose
        analysis is merely deferred (Quick Add), which reads as "analysed, and
        everything about it is unknown" rather than "not analysed yet".
        """
        from app.utils.analysis import SIGNALS_ONLY_VERSION
        if ta is None or ta.analysis_version == SIGNALS_ONLY_VERSION:
            return None
        return {
            "sample_rate_hz":       ta.sample_rate_hz,
            "bit_depth":            ta.bit_depth,
            "bitrate_kbps":         ta.bitrate_kbps,
            "rms_db":               ta.rms_db,
            "peak_db":              ta.peak_db,
            "noise_floor_db":       ta.noise_floor_db,
            "dynamic_range_db":     ta.dynamic_range_db,
            "clipping_pct":         ta.clipping_pct,
            "dc_offset":            ta.dc_offset,
            "spectral_centroid_hz": ta.spectral_centroid_hz,
            "spectral_cutoff_hz":   ta.spectral_cutoff_hz,
            "spectral_flatness":    ta.spectral_flatness,
            "non_music_score":      ta.non_music_score,
            "waveform":             _json.loads(ta.waveform_json) if ta.waveform_json else [],
            "analyzed_at":          ta.analyzed_at.isoformat() if ta.analyzed_at else None,
        }

    # Rename Files staging (spec section 6.4): how many tracks the ACTIVE
    # global naming scheme would actually rename, with no disk access. A
    # bad custom template can't crash the recording page, so this fails
    # closed to 0 rather than surfacing the error here (Settings/the dialog
    # itself is where a bad template gets reported, via the preview
    # endpoint's 400).
    _file_handling = node_settings.get_file_handling()
    files_staged = 0
    try:
        _plan = rename_plan(rec, _file_handling["naming_scheme"],
                            _file_handling["naming_template"] or None)
        files_staged = sum(1 for _t, _cur, _new in _plan
                           if _new != _cur)
    except TemplateError:
        files_staged = 0

    _set_track_numbers = derive_set_track_numbers(rec.tracks)
    from app.utils.bulk_ingest import recording_format

    # Spec 6.4: "N discs" on the details line. Derived, not stored
    # (recording.disc_count is never a column -- spec section 1's field
    # table).
    _disc_numbers = [t.disc_number for t in rec.tracks if t.disc_number is not None]
    disc_count = max(_disc_numbers) if _disc_numbers else None

    return jsonify({
        "id":                   rec.id,
        "disc_count":           disc_count,
        "format":               recording_format(rec),
        "performance_id":       rec.performance_id,
        "title":                rec.title,
        "source":               rec.source,
        "lineage":              rec.lineage,
        "source_tag":           rec.source_tag,
        "etree_shnid":          rec.etree_shnid,
        # recording_engineer / transfer_engineer / mastering_engineer as {id, name}
        # or null; absent for a studio record (live recordings only).
        **recording_engineer_payload(rec),
        "files_staged":         files_staged,
        "quality":              rec.quality,
        "is_favorite":          bool(rec.is_favorite),
        "is_complete":          rec.is_complete,
        "is_official":          bool(rec.is_official),
        # Live show vs studio release.
        "kind":                 rec.kind,
        # MusicBrainz release lookup -- the eight columns, so the Release block
        # on View Recording can render them without a second endpoint.
        "mb_release_id":         rec.mb_release_id,
        "mb_release_group_id":   rec.mb_release_group_id,
        "mb_release_status":     rec.mb_release_status,
        "mb_release_type":       rec.mb_release_type,
        "mb_label":              rec.mb_label,
        "mb_catalog_number":     rec.mb_catalog_number,
        "mb_release_country":    rec.mb_release_country,
        "mb_release_checked_at": rec.mb_release_checked_at.isoformat() if rec.mb_release_checked_at else None,
        # Exposed so the page can SAY the show is out at the workbench. A
        # recording whose folder has been moved would otherwise look completely
        # normal and simply fail to play — the "empty state that is really a
        # failed fetch" trap, in its most confusing form.
        "is_published":         bool(rec.is_published),
        "location":            rec.location or "library",
        "info_file_content":    rec.info_file_content,
        "notes":                rec.notes,
        "ai_research":          lomax.latest_result("recording", rec.id, ("recording", "resolution")),
        # Ingest provenance for the Resolver tab. Owner-only: app/api/share.py
        # has its own serializer and deliberately does not carry this.
        "resolver_json":        _sources_plain(_json.loads(rec.resolver_json)) if rec.resolver_json else None,
        # Junction rows ONLY, deliberately. System collections are dynamic and
        # Full Library covers every published recording, so resolving them here
        # would stamp the same chip on all 580 recording pages — noise, and it
        # would read as a curation the user did not make. This is the one
        # membership reader that should NOT learn about system collections;
        # every other one lives on the Collection model.
        "collections": [
            {"id": l.collection.id, "name": l.collection.name}
            for l in db.session.query(CollectionRecording).filter_by(recording_id=rec.id).all()
        ],
        # Listening Quality reads FLAC only; the Quality tab offers Analyze
        # Audio only when there is something it can read.
        "audio_scorable": any(str(t.file_path or "").lower().endswith(".flac") for t in rec.tracks),
        "tracks": [
            {
                "id":              t.id,
                "track_number":    t.track_number,
                "title":           t.title,
                "set_number":      t.set_number,
                # Derived, not stored (spec section 5) -- recomputed here on
                # every read so an edited set_number never leaves a stale
                # per-set position behind.
                "set_track_number": _set_track_numbers.get(t.id),
                "disc_number":       t.disc_number,
                "disc_track_number": t.disc_track_number,
                "duration":     t.duration,
                "is_official":  bool(t.is_official),
                "flags":        _json.loads(t.flags) if t.flags else [],
                "songwriter":   t.songwriter,
                "notes":        t.notes,
                "stream_url":   f"/api/stream/{t.id}",
                "analysis":     _analysis(t.analysis),
                "checksum": {
                    "type":            t.checksum_type,
                    "expected":        t.expected_checksum,
                    "status":          t.checksum_status,
                    "verified_at":     t.checksum_verified_at.isoformat() if t.checksum_verified_at else None,
                } if t.checksum_type else None,
            }
            for t in rec.tracks
        ],
        "fingerprints": [
            {
                "type":     fp.fingerprint_type,
                "filename": fp.filename,
            }
            for fp in rec.fingerprints
        ],
        "events": [
            {
                "event_type": e.event_type,
                "note":       e.note,
                "created_at": e.created_at.isoformat(),
                "user_id":    e.user_id,
            }
            for e in rec.events
        ],
        # Recording-level artwork -- the full gallery (for the header
        # portrait + createPhotoGallery) plus a bare image_url matching every
        # list row's field, so the header can render before the gallery
        # component itself has loaded.
        "images":     [ei.image_payload(i, _RECORDING_IMG_URL) for i in rec.images],
        "image_url":  _primary_recording_image_url(rec),
    })


# ── GET /api/recordings/favorites ─────────────────────────────────────────────

@bp.route("/favorites")
@login_required
def favorite_recordings():
    """GET /api/recordings/favorites[?limit=N]

    Every starred recording, for the sidebar's Favorites list. Its own endpoint
    rather than a `?favorite=1` flag on /recent, because the two answer
    different questions and want different orders: /recent is "what arrived
    lately" and is inherently capped, this is "the shelf I keep coming back to"
    and wants to be complete.

    Ordered by artist then date — a Favorites list is browsed by looking for
    a name, not by when the star happened to be clicked. `is_favorite` carries
    no timestamp anyway, which is deliberate: it is a one-click reaction, not an
    event log (see Recording.is_favorite).

    The limit is a runaway guard, not a feature. 200 favourites in a sidebar is
    already unusable and would want its own page.
    """
    limit = request.args.get("limit", 200, type=int) or 200
    limit = max(1, min(limit, 500))
    # coalesce(sort_name, name), not sort_name: the column is NULL for every one
    # of the 179 artists in the library. Ordering on it alone would tie every
    # row and fall back to whatever order SQLite felt like. This expression is
    # the right one regardless of whether that backfill ever happens.
    sort_key = db.func.coalesce(Artist.sort_name, Artist.name)
    recs = _card_eager(
        Recording.query
        .filter(Recording.is_favorite.is_(True), Recording.is_published.is_(True))
        .join(Performance, Recording.performance_id == Performance.id)
        .outerjoin(Artist, Performance.artist_id == Artist.id)
        .order_by(sort_key.asc(),
                  Performance.start_year.asc(),
                  Performance.start_month.asc(),
                  Performance.start_day.asc())
    ).limit(limit).all()
    return jsonify([recording_row(r, card=True) for r in recs])


# ── POST /api/recordings/scan ─────────────────────────────────────────────────

@bp.route("/scan", methods=["POST"])
@login_required
def scan_recording():
    """Step 1 of ingest — non-destructive scan of a source folder.
    Returns two parallel metadata sets (from_tags, from_info_file) for
    the user to review field by field in the UI. Nothing is written to DB.
    Delegates to build_scan_payload() — the same function batch import uses,
    so a folder's health score never differs between the two flows.
    """
    from app.utils.debug_log import log_step

    data        = request.get_json()
    folder_path = data.get("folder_path", "").strip()
    job         = f"scan:{folder_path}"

    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({"error": "Invalid or inaccessible folder path"}), 400

    # Rescan: the reviewer edited the info file in the Details panel and asked
    # for the inference to be re-run over it. The edited text rides on the
    # request rather than being written to their disk first — "try again" must
    # not be a silent modification of the collector's source folder, and the
    # info file is the taper's own words.
    #
    # Absent key = an ordinary first scan, byte-for-byte as before. An empty
    # STRING is a real value (the reviewer cleared the box), so the test is on
    # presence, not truthiness.
    info_override = None
    if "info_file_content" in data:
        info_override = {
            "filename": (data.get("info_filename") or "").strip() or None,
            "content":  data.get("info_file_content") or "",
        }

    log_step(job, "request received",
             "POST /api/recordings/scan" + (" (rescan, edited info file)" if info_override else ""))
    resp = build_scan_payload(folder_path, info_override=info_override)
    if resp is None:
        return jsonify({"error": "No audio files found in folder"}), 422

    known_artists = [p.name for p in Artist.query.all()]
    known_venues = [
        {"name": v.name, "city": v.city, "state": v.state, "country": v.country}
        for v in Venue.query.all()
    ]
    log_step(job, "queried known artists/venues",
             f"{len(known_artists)} artists, {len(known_venues)} venues")
    resp["paula"] = compute_paula_score(resp, known_artists, known_venues)
    # Add Recording's title says "(In Workshop)" / "(In Backlog)".
    from app.utils.published import working_folder_name
    resp["working_folder"] = working_folder_name(folder_path)
    log_step(job, "response ready", "Paula scoring done")

    return jsonify(resp)


# ── PUT /api/recordings/<id> ──────────────────────────────────────────────────

@bp.route("/<int:recording_id>", methods=["PUT"])
@login_required
def update_recording(recording_id):
    """
    Update recording metadata in DB.
    Logs a metadata_updated event.
    Does NOT write FLAC tags — that is a separate deliberate action.
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    # TODO: validate archivist permission for this recording's artist

    data = request.get_json()
    updatable = ["title", "source", "lineage",
                 "quality", "is_complete", "notes", "info_file_content",
                 "source_tag"]
    for field in updatable:
        if field in data:
            setattr(rec, field, data[field])

    # kind -- live show vs studio release. Its own branch rather than the blind
    # setattr loop above: it needs validation, the updatable fields above never
    # do.
    if "kind" in data:
        if data["kind"] not in ("live", "studio"):
            return jsonify({"error": "kind must be 'live' or 'studio'"}), 400
        rec.kind = data["kind"]

    # etree_shnid is an Integer column; makeInlineEditable() sends a plain
    # string (or an empty one to clear it), so it needs its own coercion
    # rather than the blind setattr the string fields above get.
    if "etree_shnid" in data:
        raw = data["etree_shnid"]
        rec.etree_shnid = int(raw) if str(raw).strip() else None

    # Engineers: names (string; empty clears), resolved by name/alias or created.
    # Live recordings only -- apply_engineer_names ignores a studio record.
    apply_engineer_names(rec, data)

    # is_official — cascade True to all tracks; never force-cascade False
    if "is_official" in data:
        rec.is_official = bool(data["is_official"])
        if rec.is_official:
            for t in rec.tracks:
                t.is_official = True

    # is_favorite — coerced rather than assigned straight through, because the
    # column is NOT NULL and the star toggle is the one field likely to be sent
    # from a checkbox as "" or null.
    if "is_favorite" in data:
        rec.is_favorite = bool(data["is_favorite"])

    # Log the change
    event = RecordingEvent(
        recording_id = rec.id,
        user_id      = current_user.id,
        event_type   = "metadata_updated",
        note         = data.get("change_note"),
    )
    db.session.add(event)

    # Folder name follows the metadata — only Source lives on Recording and
    # feeds the folder name, but this runs on every save rather than gating
    # on "source" in data: it's a cheap no-op comparison when nothing
    # folder-relevant changed, and staying unconditional means one fewer
    # place this can drift out of sync with build_folder_name later growing a
    # new input field. Non-fatal: a rename failure never blocks the metadata
    # commit below. rename_folders governs this (spec section 1.1): off means
    # the folder name is never touched by a metadata save, however much the
    # metadata feeding build_folder_name has drifted.
    library_root  = current_app.config.get("LIBRARY_ROOT", "")
    rename_error  = (rename_recording_folder(rec, library_root)
                     if node_settings.get_file_handling()["rename_folders"]
                     else None)

    db.session.commit()

    resp = {"id": rec.id, "updated_at": rec.updated_at.isoformat()}
    if rename_error:
        resp["folder_rename_error"] = rename_error
    return jsonify(resp)


# ── MusicBrainz release lookup ── Mirrors app/api/artists.py's
# musicbrainz_lookup/candidates/resolve trio exactly -- automatic matching
# either lands outright (via try_match_release in the background worker) or
# defers to a human, and these three endpoints are how a human finishes the
# job for a studio recording. Admin-gated: this writes Performance dates and
# Track titles, not just a status flag.

def _require_admin_for_release():
    if getattr(current_user, "role", None) != "admin":
        return jsonify({"error": "Admin only"}), 403
    return None


@bp.route("/<int:recording_id>/release-candidates")
@login_required
def release_candidates(recording_id):
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    if rec.kind != "studio":
        return jsonify({"error": "Only studio recordings have a release lookup"}), 400
    forbidden = _require_admin_for_release()
    if forbidden:
        return forbidden
    if not musicbrainz.enabled():
        return jsonify({"error": "MusicBrainz lookups are disabled"}), 503

    artist_name = rec.performance.artist.name if rec.performance and rec.performance.artist else None
    # The Search row may carry edited Artist / Title values; blank or absent falls back to the recording's own.
    artist_name = (request.args.get("artist") or "").strip() or artist_name
    title = (request.args.get("title") or "").strip() or rec.title
    musicbrainz.reset_breaker()
    candidates = musicbrainz.search_release(artist_name, title)
    # A person is choosing: every same-titled release is shown, whatever its
    # track count. The count filter is for the automatic match only; here it
    # hid the full release of an incomplete Album (Solo Banjo Works, 2 of 24).
    _status, _best, ranked = musicbrainz.classify_release(candidates, None)
    return jsonify({"candidates": ranked})


@bp.route("/<int:recording_id>/release-link", methods=["POST"])
@login_required
def release_link(recording_id):
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    if rec.kind != "studio":
        return jsonify({"error": "Only studio recordings can be linked to a release"}), 400
    forbidden = _require_admin_for_release()
    if forbidden:
        return forbidden
    if not musicbrainz.enabled():
        return jsonify({"error": "MusicBrainz lookups are disabled"}), 503

    data = request.get_json() or {}
    mbid = (data.get("mbid") or "").strip()
    if not mbid:
        return jsonify({"error": "mbid is required"}), 400

    musicbrainz.reset_breaker()
    details = musicbrainz.link_release(rec, mbid)
    if not details:
        return jsonify({"error": "Could not fetch that MusicBrainz release"}), 502
    # A linked release longer than the folder marks the Album incomplete.
    if len(details.get("tracks") or []) > len(rec.tracks or []):
        rec.is_complete = False
    rec.completeness_checked_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"status": "linked", "mb_release_id": rec.mb_release_id,
                    "mb_release_group_id": rec.mb_release_group_id,
                    "mb_release_type": rec.mb_release_type,
                    "mb_label": rec.mb_label, "mb_catalog_number": rec.mb_catalog_number,
                    "mb_release_country": rec.mb_release_country})


@bp.route("/<int:recording_id>/release-unlink", methods=["POST"])
@login_required
def release_unlink(recording_id):
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    if rec.kind != "studio":
        return jsonify({"error": "Only studio recordings can be unlinked"}), 400
    forbidden = _require_admin_for_release()
    if forbidden:
        return forbidden

    musicbrainz.unlink_release(rec)
    db.session.commit()
    return jsonify({"status": "unlinked"})


# ── Recording-level artwork ────────────────
# Five standard photo routes, generated exactly as venues/musicians/events
# get them -- see app/utils/entity_images.py::register_image_routes(). The
# only wrinkle: images_dir_for keys on the recording's numeric id (image_dir()
# asserts on that for the 'recordings' kind), not a sanitized name.

_RECORDING_IMG_URL = "/api/recordings/images"


def _recording_images_dir(rec):
    return ei.image_dir("recordings", str(rec.id))


ei.register_image_routes(
    bp, parent_model=Recording, image_model=RecordingImage, url_prefix=_RECORDING_IMG_URL,
    images_dir_for=_recording_images_dir,
    login_required=login_required, require_library=require_library,
)


# ── POST /api/recordings/<id>/images/from-artist | from-venue ────────────────
# "Use the artist image" / "Use the venue image". Copies the source's primary
# image FILE into this recording's own image folder and makes the copy primary,
# so the recording keeps its picture if the artist's or venue's photo is later
# replaced or deleted -- a reference would silently change under it. Credit and
# caption travel with the copy: a Commons licence credit is part of the image.
# Idempotent: source_ref "<kind>-image:<id>" is the dedupe key, so choosing the
# same image twice re-promotes the existing copy instead of stacking
# duplicates.

def _copy_image_into_recording(rec, src, src_dir, kind):
    import secrets
    import shutil
    if not src:
        return jsonify({"error": f"This {kind} has no image"}), 404

    ref = f"{kind}-image:{src.id}"
    existing = next((i for i in rec.images if i.source_ref == ref), None)
    if existing:
        ei.set_primary(existing)
        db.session.commit()
        return jsonify(ei.image_payload(existing, _RECORDING_IMG_URL))

    src_path = src_dir / src.filename
    if not src_path.exists():
        return jsonify({"error": "Image file missing on disk"}), 404

    dest_dir = _recording_images_dir(rec)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"img_{secrets.token_hex(6)}{src.ext}"
    shutil.copyfile(src_path, dest_dir / fname)

    img = RecordingImage(
        recording_id=rec.id, filename=fname, ext=src.ext,
        sort_order=max((i.sort_order for i in rec.images), default=-1) + 1,
        origin=kind, source_ref=ref, caption=src.caption, credit=src.credit,
    )
    db.session.add(img)
    db.session.flush()
    ei.set_primary(img)
    db.session.commit()
    return jsonify(ei.image_payload(img, _RECORDING_IMG_URL))


@bp.route("/<int:recording_id>/images/from-artist", methods=["POST"])
@login_required
def use_artist_image(recording_id):
    from app.models.artist_image import ArtistImage
    from app.api.artists import _artist_images_dir
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    artist = rec.performance.artist if rec.performance else None
    src = ei.primary_for(ArtistImage, artist.id) if artist else None
    return _copy_image_into_recording(
        rec, src, _artist_images_dir(artist) if artist else None, "artist")


@bp.route("/<int:recording_id>/images/from-venue", methods=["POST"])
@login_required
def use_venue_image(recording_id):
    from app.models.venue_image import VenueImage
    from app.api.venues import _venue_images_dir
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    venue = rec.performance.venue if rec.performance else None
    src = ei.primary_for(VenueImage, venue.id) if venue else None
    return _copy_image_into_recording(
        rec, src, _venue_images_dir(venue) if venue else None, "venue")


# ── DELETE /api/recordings/<id> ──────────────────────────────────────────────

def _delete_tracks_of_recording(recording_id):
    """
    Delete every track of a recording along with its dependent child rows
    (track_analysis, play_log). Done in app code because SQLite FK cascades
    are not enforced on the existing schema — see delete_recording.
    """
    from app.models.track import Track
    from app.models.track_analysis import TrackAnalysis
    from app.models.play_log import PlayLog

    track_ids = [
        t.id for t in db.session.query(Track.id).filter_by(recording_id=recording_id).all()
    ]
    if track_ids:
        db.session.query(TrackAnalysis).filter(TrackAnalysis.track_id.in_(track_ids)).delete(
            synchronize_session=False)
        db.session.query(PlayLog).filter(PlayLog.track_id.in_(track_ids)).delete(
            synchronize_session=False)
        db.session.query(Track).filter(Track.id.in_(track_ids)).delete(
            synchronize_session=False)


@bp.route("/<int:recording_id>", methods=["DELETE"])
@login_required
def delete_recording(recording_id):
    """Delete a recording and all its child records (tracks, events, fingerprints).
    Then prune any performance/artist/canonical-artist left empty by the
    delete.

    `?delete_files=1` ALSO removes the recording's folder from disk. Default
    remains files-untouched: for a ROIO collector the tape is the irreplaceable
    thing and the database row is not, so destroying audio is never the default
    and never implicit.

    The guard is deliberately paranoid, in the same spirit as
    /api/quality/move. The folder is resolved server-side from LIBRARY_ROOT +
    the row's relative folder_path — the client cannot name a path — and then
    it must still realpath INSIDE LIBRARY_ROOT, be a directory, and not be a
    mount point or a filesystem root. A folder that fails any of those is left
    alone and reported back; the row is still deleted, because "I could not
    remove the audio" is not a reason to keep a library entry the user asked
    to be rid of.

    Files go first, on purpose. If rmtree fails the row survives, the response
    says why, and the recording is still there to try again — whereas deleting
    the row first would strip the only handle on the folder the moment the
    filesystem misbehaved.
    """
    import shutil
    from app.models.recording import RecordingFingerprint
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Recording not found"}), 404

    performance_id = rec.performance_id

    delete_files = request.args.get("delete_files") in ("1", "true", "yes")
    files_deleted, files_error = False, None
    if delete_files:
        library_root = os.path.realpath(location_root(rec.location))
        target = os.path.realpath(recording_folder_abs(rec) or library_root)
        if not library_root or not rec.folder_path:
            files_error = "No library folder recorded for this recording"
        elif not (target != library_root and target.startswith(library_root + os.sep)):
            files_error = "Recording folder does not resolve inside the library"
        elif not os.path.isdir(target):
            files_error = "Recording folder is not on disk"
        elif os.path.ismount(target) or os.path.dirname(target) == target:
            files_error = "Refusing to delete a mount point or filesystem root"
        else:
            try:
                shutil.rmtree(target)
                files_deleted = True
            except OSError as e:
                files_error = "Could not delete folder: %s" % e

    # Delete children explicitly, bottom-up. SQLite FK enforcement is off and
    # the existing tables were created without ON DELETE actions, so nothing
    # cascades for us — we clear every child of every track (analysis, play
    # logs) plus the recording's own children, or they orphan silently. Bulk
    # deletes (no ORM cascade) keep this predictable.
    _delete_tracks_of_recording(recording_id)
    db.session.query(RecordingFingerprint).filter_by(recording_id=recording_id).delete(
        synchronize_session=False)
    db.session.query(RecordingEvent).filter_by(recording_id=recording_id).delete(
        synchronize_session=False)
    db.session.query(Recording).filter_by(id=recording_id).delete(synchronize_session=False)
    db.session.flush()

    # The recording_image ROWS cascade with the recording, but the files they
    # point at live under DATA_DIR, not in any table -- nothing removes those
    # on its own (N5). Best-effort and swallowed like the audio rmtree above:
    # a leftover images folder for an id that no longer exists is clutter,
    # never a reason to fail a delete the user asked for.
    try:
        images_dir = ei.image_dir("recordings", str(recording_id))
        if images_dir.is_dir():
            shutil.rmtree(images_dir)
    except OSError:
        pass

    # Prune the now-empty chain above the recording.
    pruned = prune_after_recording_delete(performance_id)

    db.session.commit()

    return jsonify({"deleted": recording_id, "pruned": pruned,
                    "files_deleted": files_deleted, "files_error": files_error}), 200


# ── POST /api/recordings/<id>/info-file ──────────────────────────────────────

@bp.route("/<int:recording_id>/info-file", methods=["POST"])
@login_required
def save_recording_info_file(recording_id):
    """POST /api/recordings/<id>/info-file
    Body: { content }
    Returns: { ok, wrote_file, filename|null, reason|null }

    Persist edited info-file text to the recording row and, when the library
    folder already holds a text file, rewrite that file in place.

    It deliberately does NOT create a file when the folder has none. The
    database is the source of truth and a write into a collector's library is
    always an explicit act; inventing a filename for a folder that never
    carried an info file would be Trellis authoring content in someone else's
    archive. In that case the DB save still happens and the caller gets
    wrote_file=False with a reason the pane can show.

    The target file is picked with the same scoring scan_folder uses, and
    checksum lists are excluded by the same content sniff — so the file the
    pane was populated from is the file that gets rewritten, rather than an
    .md5 list that happens to sort first.
    """
    from app.utils.ingest import _score_text_file, fingerprint_type_for_file

    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    content = (request.get_json() or {}).get("content", "")
    if not isinstance(content, str):
        return jsonify({"error": "content must be a string"}), 400

    # DB first — it is the source of truth, and it must land even if the disk
    # write is impossible (unmounted volume, read-only media, missing folder).
    rec.info_file_content = content or None
    db.session.add(RecordingEvent(
        recording_id = rec.id,
        user_id      = current_user.id,
        event_type   = "metadata_updated",
        note         = "Edited info file",
    ))
    db.session.commit()

    folder = recording_folder_abs(rec)
    if not folder or not os.path.isdir(folder):
        return jsonify({"ok": True, "wrote_file": False, "filename": None,
                        "reason": "Recording folder is not reachable"})

    # Candidate = a .txt that is not a checksum list, best-scoring first.
    candidates = []
    for fname in os.listdir(folder):
        full = os.path.join(folder, fname)
        low  = fname.lower()
        if not low.endswith(".txt") or not os.path.isfile(full):
            continue
        if fingerprint_type_for_file(full, low):
            continue
        candidates.append((_score_text_file(fname), fname))
    if not candidates:
        return jsonify({"ok": True, "wrote_file": False, "filename": None,
                        "reason": "No info file in the recording folder"})

    target = sorted(candidates, key=lambda c: (-c[0], c[1]))[0][1]
    try:
        with open(os.path.join(folder, target), "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return jsonify({"ok": True, "wrote_file": False, "filename": target,
                        "reason": "Could not write file: %s" % e})

    return jsonify({"ok": True, "wrote_file": True, "filename": target, "reason": None})


# ── Moving, trashing and reviewing a recording that is not on the shelf ─────

def _working_source(rec):
    """(folder_abs, stop_at) for a recording that is not in the library, or (None, error)."""
    from app.utils import bulk_ingest_run as bir
    if rec.location == "queue":
        it = queue_item_for(rec)
        if it is None:
            return None, "This recording has no folder in the Queue"
        return bir.item_abs_path(it), bir.item_base(it.run.root)
    return recording_folder_abs(rec), location_root(rec.location)


def _event(rec, kind, note):
    db.session.add(RecordingEvent(recording_id=rec.id, user_id=current_user.id,
                                  event_type=kind, note=note))


def _relocate(rec, destination):
    """Move a non-library recording's whole folder (no flattening) into Workshop or
    Backlog: files first, then location and folder_path. A name collision gets " (2)".
    Returns (payload, status)."""
    from app.api.quality import move_folder_to_triage
    from app.utils import bulk_ingest_run as bir
    src, stop_at = _working_source(rec)
    if src is None:
        return {"error": stop_at}, 400
    it = queue_item_for(rec) if rec.location == "queue" else None
    if it is not None and bir.is_in_library(it.run.root):
        return {"error": "Items in the library are never moved"}, 409
    if it is not None and it.ingest_requested:
        return {"error": "This recording is already queued for import"}, 409
    payload, status = move_folder_to_triage(src, destination, stop_at=stop_at)
    if status != 200:
        return payload, status
    was = rec.location
    rec.location = destination
    rec.folder_path = os.path.basename(payload["moved_to"])
    for row in (db.session.query(BulkIngestItem)
                .filter(BulkIngestItem.recording_id == rec.id,
                        BulkIngestItem.status.notin_(("ingested", "moved")))):
        row.status, row.ingest_requested = "moved", False
    _event(rec, "moved_out_of_library" if was == "library" else "moved",
           f"Moved to {destination} as {rec.folder_path}")
    db.session.commit()
    return {"ok": True, "destination": destination, "moved_to_name": rec.folder_path}, 200


@bp.route("/<int:recording_id>/move", methods=["POST"])
@login_required
def move_recording_out(recording_id):
    """POST /api/recordings/<id>/move
    Body: { destination: "workshop" | "backlog" }
    Returns: { ok, destination, moved_to_name }

    Moves a recording's folder to Workshop or Backlog and sets its location. From the
    library that takes the show off the shelf (the record survives in full); from the
    Queue, Workshop or Backlog it moves the whole folder as it is. The id is the input
    and the path is derived server-side; the destination must be a configured working
    folder. Files move BEFORE the location changes, so a failed move leaves the
    recording where it was. Never overwrites: a collision gets " (2)", " (3)".
    """
    import shutil

    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    destination = ((request.get_json() or {}).get("destination") or "").strip().lower()
    triage_dirs = current_app.config.get("TRIAGE_DIRS", {})
    if destination not in triage_dirs:
        return jsonify({"error": f"Unknown destination {destination!r}; "
                                 f"expected one of {sorted(triage_dirs)}"}), 400
    if rec.location == destination:
        return jsonify({"error": f"This recording is already in {destination}"}), 409

    if rec.location != "library":
        body, code = _relocate(rec, destination)
        return jsonify(body), code

    library_root = os.path.realpath(location_root("library"))
    src = os.path.realpath(recording_folder_abs(rec) or library_root)
    if not library_root or not rec.folder_path:
        return jsonify({"error": "No library folder recorded for this recording"}), 400
    if not (src != library_root and src.startswith(library_root + os.sep)):
        return jsonify({"error": "Recording folder does not resolve inside the library"}), 403
    if not os.path.isdir(src):
        return jsonify({"error": "Recording folder is not on disk"}), 400
    if os.path.ismount(src) or os.path.dirname(src) == src:
        return jsonify({"error": "Refusing to move a mount point or filesystem root"}), 400

    dest_root = triage_dirs[destination]
    try:
        os.makedirs(dest_root, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"Cannot create {dest_root}: {e}"}), 500

    base, target, n = os.path.basename(src), None, 2
    target = os.path.join(dest_root, base)
    while os.path.exists(target):
        target = os.path.join(dest_root, f"{base} ({n})")
        n += 1

    try:
        shutil.move(src, target)
    except OSError as e:
        return jsonify({"error": f"Move failed: {e}"}), 500

    rec.location     = destination
    rec.folder_path  = os.path.basename(target)
    _event(rec, "moved_out_of_library", f"Moved to {destination} as {os.path.basename(target)}")
    db.session.commit()

    return jsonify({"ok": True, "destination": destination,
                    "moved_to_name": os.path.basename(target)})


@bp.route("/<int:recording_id>/trash", methods=["POST"])
@login_required
def trash_recording(recording_id):
    """POST /api/recordings/<id>/trash
    Returns: { ok }

    Trash for a recording anywhere: Queue, Workshop, Backlog or the library (View
    Recording's Actions menu, Ryan 2026-10-15). The folder goes to the system Trash
    FIRST; only then is the record deleted, and then whatever it created that nobody
    confirmed or enriched. A folder that is already gone does not keep the record.
    """
    from app.api.quality import trash_folder_path
    from app.utils.recording_delete import remove_recording
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    if rec.location == "library":
        # Same guards the old Delete used: inside the library, never the root.
        # And never a folder another recording also lives in: trashing it would
        # take that recording's files with it.
        library_root = os.path.realpath(location_root("library"))
        src = os.path.realpath(recording_folder_abs(rec) or library_root)
        if not rec.folder_path or not src.startswith(library_root + os.sep):
            return jsonify({"error": "This recording's folder is not inside the library"}), 400
        if (db.session.query(Recording.id)
                .filter(Recording.id != rec.id, Recording.location == "library",
                        Recording.folder_path == rec.folder_path).first()):
            return jsonify({"error": "Another recording shares this folder, so it was not moved to the Trash"}), 409
        stop_at = library_root
    else:
        src, stop_at = _working_source(rec)
    if src and os.path.isdir(src):
        payload, status = trash_folder_path(src, stop_at=stop_at)
        if status != 200:
            return jsonify(payload), status
    for row in (db.session.query(BulkIngestItem)
                .filter(BulkIngestItem.recording_id == rec.id,
                        BulkIngestItem.status.notin_(("ingested", "moved")))):
        row.status, row.ingest_requested = "moved", False
    remove_recording(rec.id)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/<int:recording_id>/review", methods=["GET"])
@login_required
def review_recording(recording_id):
    """GET /api/recordings/<id>/review
    The Review form's data for a recording that is not in the library: the scan of its
    folder (as POST /scan returns it) plus `draft`, what is saved on the recording."""
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    if rec.location == "library":
        return jsonify({"error": "This recording is in the library"}), 409
    folder, _stop = _working_source(rec)
    if not folder or not os.path.isdir(folder):
        return jsonify({"error": "The folder is not on disk"}), 404
    resp = build_scan_payload(folder)
    if resp is None:
        return jsonify({"error": "No audio files found in folder"}), 422
    known_artists = [p.name for p in Artist.query.all()]
    known_venues = [{"name": v.name, "city": v.city, "state": v.state, "country": v.country}
                    for v in Venue.query.all()]
    resp["paula"] = compute_paula_score(resp, known_artists, known_venues)
    from app.utils.published import working_folder_name
    resp["working_folder"] = working_folder_name(folder)
    resp["recording_id"] = rec.id
    resp["location"] = rec.location
    resp["folder_path"] = folder
    from app.api.ingest import recording_draft
    resp["draft"] = recording_draft(rec)
    return jsonify(resp)


@bp.route("/<int:recording_id>/draft", methods=["PUT"])
@login_required
def save_draft(recording_id):
    """PUT /api/recordings/<id>/draft
    Body: the Review form's payload (any subset of its keys), plus optional
    `confirm: true` (the person is leaving the form), `kind_set: true`.
    Returns: { ok, status, reasons }

    The Review form's autosave. Writes whatever is sent onto the recording (incomplete
    data is allowed: the Workshop is a workspace), re-runs a Queue row's verdict so a
    fixed Needs Review row becomes Ready, and confirms NOTHING: the act's confirmed
    mark, learned aliases and the Queue re-check wait for `confirm`, which the form
    sends when the person leaves, imports or moves.
    """
    from app.api.ingest import (save_recording_payload, mark_person_edited, confirm_signals_pre,
                                confirm_signals_post, KIND_EVENT, kind_chosen)
    from app.utils import bulk_ingest_run as bir
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    if rec.location == "library":
        return jsonify({"error": "This recording is in the library"}), 409
    data = request.get_json(silent=True) or {}
    confirm = bool(data.pop("confirm", False))
    kind_set = bool(data.pop("kind_set", False))
    data.pop("source_folder_path", None)             # the client never names a path
    data.pop("recording_id", None)
    data["unattended"] = not confirm
    folder, _stop = _working_source(rec)
    try:
        save_recording_payload(data, current_user.id, rec, location=rec.location, source_folder=folder)
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    mark_person_edited(rec, current_user.id)
    if kind_set and not kind_chosen(rec):
        db.session.add(RecordingEvent(recording_id=rec.id, user_id=current_user.id,
                                      event_type=KIND_EVENT, note=rec.kind))
    item = queue_item_for(rec) if rec.location == "queue" else None
    bir.refresh_item_from_recording(item, rec, kind_set=kind_set or kind_chosen(rec))
    if confirm:
        confirm_signals_pre(rec, data)
    db.session.commit()
    if confirm:
        confirm_signals_post(rec, data, folder)
    return jsonify({"ok": True, "status": item.status if item is not None else None,
                    "reasons": [r for r in (item.reason or "").split(",") if r] if item is not None else []})


def _folder_in(which, name):
    """Absolute path of a folder named in Downloads, Workshop or Backlog, or None.
    `name` is one folder name, never a path."""
    from app.utils.downloads_dir import downloads_dir
    if not name or name in (".", "..") or os.sep in name or "/" in name:
        return None
    root = (downloads_dir() if which == "downloads"
            else (current_app.config.get("TRIAGE_DIRS") or {}).get(which))
    if not root:
        return None
    path = os.path.join(str(root), name)
    return path if os.path.isdir(path) else None


@bp.route("/open-folder", methods=["POST"])
@login_required
def open_folder():
    """POST /api/recordings/open-folder   Body: { which: "workshop" | "backlog", name }
    Returns: { recording_id }

    A Workshop or Backlog folder with no recording (it was put there by hand, or before
    recordings were tracked) is read first, which creates its recording there; then the
    caller opens Review on it."""
    from app.api.ingest import read_folder_into_recording
    data = request.get_json(silent=True) or {}
    which = (data.get("which") or "").lower()
    if which not in ("workshop", "backlog"):
        return jsonify({"error": "Unknown folder"}), 400
    path = _folder_in(which, (data.get("name") or "").strip())
    if path is None:
        return jsonify({"error": "Folder not found"}), 404
    rec, out = read_folder_into_recording(path, which, current_user.id)
    if rec is None:
        return jsonify({"error": "This folder cannot be reviewed", "reasons": out.get("reasons")}), 422
    return jsonify({"recording_id": rec.id})


@bp.route("/move-folder", methods=["POST"])
@login_required
def move_folder():
    """POST /api/recordings/move-folder   Body: { which: "downloads"|"workshop"|"backlog", name, destination }
    Returns: { ok, recording_id|null }

    Move for a folder row that has no recording: the folder is read first (a readable
    one gets its recording), then moved whole to Workshop or Backlog and the recording
    placed there. A folder that cannot be read is still moved, with no recording."""
    from app.api.ingest import read_folder_into_recording
    from app.api.quality import move_folder_to_triage
    from app.utils.recording_delete import remove_recording
    data = request.get_json(silent=True) or {}
    which = (data.get("which") or "").lower()
    destination = (data.get("destination") or "").lower()
    if which not in ("downloads", "workshop", "backlog") or destination not in ("workshop", "backlog"):
        return jsonify({"error": "Unknown folder"}), 400
    if which == destination:
        return jsonify({"error": f"This folder is already in {destination}"}), 409
    path = _folder_in(which, (data.get("name") or "").strip())
    if path is None:
        return jsonify({"error": "Folder not found"}), 404
    rec, _out = read_folder_into_recording(path, which if which != "downloads" else destination,
                                           current_user.id)
    created = rec is not None and which == "downloads"
    stop_at = (current_app.config.get("TRIAGE_DIRS") or {}).get(which) if which != "downloads" else None
    payload, status = move_folder_to_triage(path, destination, stop_at=stop_at)
    if status != 200:
        if created:                                          # not moved: leave no row behind
            remove_recording(rec.id)
            db.session.commit()
        return jsonify(payload), status
    if rec is not None:
        rec.location, rec.folder_path = destination, os.path.basename(payload["moved_to"])
        _event(rec, "moved", f"Moved to {destination} as {rec.folder_path}")
        db.session.commit()
    return jsonify({"ok": True, "recording_id": rec.id if rec is not None else None})


# ── POST /api/recordings/<id>/write-tags ──────────────────────────────────────

@bp.route("/<int:recording_id>/write-tags", methods=["POST"])
@login_required
def write_tags(recording_id):
    """
    Write current DB metadata as Vorbis comments to every FLAC file in the
    recording. Existing tags are replaced entirely.

    This is a deliberate, explicit action — not triggered by metadata saves.
    Logs a tags_written event on full or partial success.

    Returns:
      200  { written: n, errors: [(filename, msg), ...] }
      404  if recording not found
      500  if all files failed
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    library_root = current_app.config.get("LIBRARY_ROOT", "")
    n_written, errors = write_flac_tags(rec, library_root)

    # Log even on partial success so the event trail is accurate
    if n_written > 0:
        note = f"{n_written} file(s) written"
        if errors:
            note += f"; {len(errors)} error(s): " + "; ".join(f[0] for f in errors)
        event = RecordingEvent(
            recording_id = rec.id,
            user_id      = current_user.id,
            event_type   = "tags_written",
            note         = note,
        )
        db.session.add(event)
        db.session.commit()

    if n_written == 0:
        return jsonify({"error": "No files written", "errors": errors}), 500

    return jsonify({"written": n_written, "errors": errors})


# ── POST /api/recordings/<id>/rename-files ────────────────────────────────────

@bp.route("/<int:recording_id>/rename-files", methods=["POST"])
@login_required
def rename_files_action(recording_id):
    """
    Rename this recording's audio files on disk to match a naming scheme
    (spec section 6.4/3.1 D12) -- an explicit action, allowed in either
    file-handling mode. {scheme, template} in the body; defaults to the
    active global scheme/template when omitted (the button's own default
    click, with no override chosen in the dialog).

    Flattens into the recording folder's root when flattens(scheme) is
    true, otherwise keeps each file's current subdir prefix and only
    substitutes the basename -- same rule ingest uses (resolve_ingest_
    file_path), applied here to files already sitting in the library.
    Collisions are disambiguated with unique_file_name(), same convention
    as every other rename in this app. Per-file failures are reported, not
    rolled back. Re-runs checksum matching afterwards (D7) since a rename
    changes what filenames the fingerprint-matching proxy should present.

    Returns:
      200  { renamed: n, errors: [(filename, msg), ...], plan: [...] }
      400  bad custom template
      404  recording not found
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    data     = request.get_json(silent=True) or {}
    scheme   = data.get("scheme")
    template = data.get("template")
    if not scheme:
        file_handling = node_settings.get_file_handling()
        scheme   = file_handling["naming_scheme"]
        template = template or file_handling["naming_template"] or None

    try:
        plan = rename_plan(rec, scheme, template)
    except TemplateError as e:
        return jsonify({"error": str(e)}), 400

    folder_abs   = recording_folder_abs(rec)

    renamed = 0
    errors  = []
    for track, current_rel, proposed_rel in plan:
        # rename_plan() already applies the flatten rule (spec section 4):
        # proposed_rel carries the current directory prefix when this scheme
        # doesn't flatten, so it's compared and used directly -- no second
        # dir-prefix step here (that used to double it up under 'original').
        new_rel = proposed_rel
        if new_rel == current_rel:
            continue

        old_abs     = os.path.join(folder_abs, current_rel)
        new_dir_rel = os.path.dirname(new_rel)
        new_dir_abs = os.path.join(folder_abs, new_dir_rel) if new_dir_rel else folder_abs
        try:
            os.makedirs(new_dir_abs, exist_ok=True)
            unique_name = unique_file_name(new_dir_abs, os.path.basename(new_rel),
                                           keep_abs=old_abs)
            final_rel = f"{new_dir_rel}/{unique_name}" if new_dir_rel else unique_name
            final_abs = os.path.join(folder_abs, final_rel)
            if not os.path.exists(old_abs):
                errors.append((track.file_path, "File not found on disk"))
                continue
            os.rename(old_abs, final_abs)
            track.file_path = final_rel
            renamed += 1
        except OSError as e:
            errors.append((track.file_path, str(e)))

    if renamed:
        db.session.add(RecordingEvent(
            recording_id = rec.id,
            user_id      = current_user.id,
            event_type   = "files_renamed",
            note         = f"{renamed} file(s) renamed",
        ))
    db.session.commit()

    checked, verified_at = _rematch_and_verify_checksums(rec, folder_abs)

    return jsonify({
        "renamed":     renamed,
        "errors":      errors,
        "checked":     checked,
        "verified_at": verified_at.isoformat(),
        "tracks": [
            {"id": t.id, "file_path": t.file_path,
             "checksum_status": t.checksum_status}
            for t in rec.tracks
        ],
    })


# ── POST /api/recordings/<id>/reveal ─────────────────────────────────────────

@bp.route("/<int:recording_id>/reveal", methods=["POST"])
@login_required
def reveal_folder(recording_id):
    """
    Open this recording's folder in Finder.

    The path itself never reaches the frontend — same File obfuscation rule
    as everything else in this module — so this resolves it server-side and
    shells out to `open`. Only makes sense on the single Mac this app already
    runs on (PyWebView, one machine, one library root); nothing here is
    reachable through the peer door.
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    folder_abs   = recording_folder_abs(rec)

    if not os.path.isdir(folder_abs):
        return jsonify({"error": "This recording's folder no longer exists on disk."}), 404

    try:
        subprocess.Popen(["open", folder_abs])
    except OSError as e:
        return jsonify({"error": f"Could not open Finder: {e}"}), 500

    return jsonify({"opened": True})


# ── GET /api/recordings/<id>/tags ─────────────────────────────────────────────

@bp.route("/<int:recording_id>/tags")
@login_required
def get_recording_file_tags(recording_id):
    """
    Return the actual on-disk Vorbis comments for every FLAC file in the
    recording. Powers the "File Tags" viewer so the effect of "Write Tags to
    Files" is visible. File paths are never exposed.
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    library_root = current_app.config.get("LIBRARY_ROOT", "")
    tracks = read_recording_tags(rec, library_root)
    # What Write Tags to Files would put on each file, for the Current/After
    # view. Same unwrapping as the on-disk read (one value is a bare string).
    staged = {t.id: {k: (v[0] if isinstance(v, list) and len(v) == 1 else v)
                     for k, v in tags.items()}
              for t, tags in desired_track_tags(rec)}
    for entry in tracks:
        entry["staged"] = staged.get(entry["track_id"])
    return jsonify({
        "recording_id": rec.id,
        "tracks":       tracks,
    })


# ── POST /api/recordings/<id>/reprocess ───────────────────────────────────────

@bp.route("/<int:recording_id>/reprocess", methods=["POST"])
@login_required
def reprocess_recording(recording_id):
    """
    Analyze Audio: the whole audio pass (Listening Quality score, non-music
    signal, Full librosa analysis) for ANY recording, live or album. Albums are
    never auto-scored at ingest, so this is the only way one gets a score.
    reanalyze=True: the user asked, so tracks with a current analysis are redone.

    Route name kept (api.js and the endpoint's callers are unchanged); only the
    work behind it changed from librosa-only to the full pass.

    Returns:
      200  { analysed: n, errors: [str, ...], score, signals, analysis,
             listening_quality }   (the part fields are "ok"/"skipped"/"failed")
      404  if recording not found
      500  if no part of the pass succeeded
    """
    from app.api.ingest import run_audio_pass, drop_queued_audio
    from app.models.track_analysis import TrackAnalysis
    from app.utils.quality_store import get_for_recording

    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    res = run_audio_pass(recording_id, score=True, reanalyze=True)
    # The audio lane may hold a queued pass for this recording; it would
    # redo what was just done.
    drop_queued_audio(recording_id)
    errors = res["errors"]
    n_ok = (db.session.query(TrackAnalysis.id)
            .join(Track, Track.id == TrackAnalysis.track_id)
            .filter(Track.recording_id == recording_id).count())
    row = get_for_recording(recording_id)

    # Log the event (same event type as before: history readers key on it)
    db.session.add(RecordingEvent(
        recording_id = recording_id,
        user_id      = current_user.id,
        event_type   = "reprocessed",
        note         = f"audio pass: score {res['score']}, signals {res['signals']}, "
                       f"analysis {res['analysis']}" + (
            f"; {len(errors)} error(s)" if errors else ""
        ),
    ))
    db.session.commit()

    if "ok" not in (res["score"], res["signals"], res["analysis"]):
        return jsonify({"error": "Analysis failed", "errors": errors}), 500

    return jsonify({
        "analysed": n_ok, "errors": errors,
        "score": res["score"], "signals": res["signals"], "analysis": res["analysis"],
        "listening_quality": row.listening_quality if row is not None else None,
    })


# ── POST /api/recordings/<id>/verify-checksums ───────────────────────────────

def _rematch_and_verify_checksums(rec, folder_abs):
    """
    (Re-)validate `rec`'s fingerprint checksums against the audio files
    currently at `folder_abs`. Shared by verify_checksums() (below) and the
    Rename Files action (spec section 3.1 D7: a rename "re-runs checksum
    matching"), since both need the identical scoped-matching logic and
    neither should drift from the other.

    Also opportunistically discovers fingerprint files that were copied into
    the library with this recording but never parsed into RecordingFingerprint
    rows (covers shows ingested before this feature existed).

    Returns: (checked_count, verified_at_datetime). Commits.
    """
    # Collect into a local list rather than re-reading rec.fingerprints after
    # adding to it — the relationship collection was already cached by the
    # line above and db.session.flush() doesn't invalidate that cache, so a
    # freshly-discovered row wouldn't show up in it this same request.
    all_fingerprints = list(rec.fingerprints)
    known_rel_paths  = {(fp.rel_path or fp.filename) for fp in all_fingerprints}
    for found in discover_fingerprint_files(folder_abs):
        if found.get("rel_path", found["filename"]) in known_rel_paths:
            continue
        try:
            with open(os.path.join(folder_abs, found["rel_path"]),
                      "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            content = None
        new_fp = RecordingFingerprint(
            recording_id     = rec.id,
            fingerprint_type = found["type"],
            filename         = found["filename"],
            content          = content,
            rel_path         = found["rel_path"],
        )
        db.session.add(new_fp)
        all_fingerprints.append(new_fp)

    # Same FINGERPRINT_TYPE_PRIORITY tie-break as ingest (see
    # api/ingest.py _do_confirm) when more than one fingerprint file
    # is present: ffp, then md5, then st5.
    fingerprints = sorted(all_fingerprints,
                          key=lambda fp: FINGERPRINT_TYPE_PRIORITY.get(fp.fingerprint_type, 9))
    now = datetime.now(timezone.utc)
    checked = 0
    for fp in fingerprints:
        if not fp.content:
            continue
        # Scope candidates to this fingerprint file's own directory (spec
        # section 3.1 D7) -- a nested-per-disc checksum file's bare "01.flac"
        # is not unique across discs once matching falls back to original
        # names, so an unscoped match could hand one disc's checksum to
        # another disc's track. Try scoping by original_file_path's own
        # directory first (that's what a fingerprint file generated against
        # the ORIGINAL source layout lists), then by the track's current
        # file_path directory (a fingerprint file backfilled against what's
        # actually in the library today); an empty rel_path (fingerprint at
        # the recording root, or a pre-this-feature row with none stored)
        # considers every track, same as before scoping existed.
        fp_dir = os.path.dirname(fp.rel_path or "").replace(os.sep, "/")
        if fp_dir:
            candidates = [
                t for t in rec.tracks
                if os.path.dirname(t.original_file_path or "").replace(os.sep, "/") == fp_dir
            ]
            if not candidates:
                candidates = [
                    t for t in rec.tracks
                    if os.path.dirname(t.file_path or "").replace(os.sep, "/") == fp_dir
                ]
            if not candidates:   # scoping found nothing usable -- fall back
                candidates = list(rec.tracks)
        else:
            candidates = list(rec.tracks)

        # Try matching against ORIGINAL filenames first (what a fingerprint
        # file shipped at/near ingest time lists), then fall back to the
        # CURRENT file_path (a backfill checksum file generated fresh
        # against what's actually in the library today, spec section 3.1
        # D7) -- whichever pass actually finds anything wins.
        entries = parse_checksum_file(fp.content)
        orig_proxies = [ChecksumMatchProxy(t, t.original_file_path)
                        for t in candidates if t.original_file_path]
        matches = match_entries_to_tracks(entries, orig_proxies) if orig_proxies else {}
        if not matches:
            cur_proxies = [ChecksumMatchProxy(t, t.file_path) for t in candidates]
            matches = match_entries_to_tracks(entries, cur_proxies)
        for proxy, expected in matches.items():
            track    = proxy.real
            abs_path = os.path.join(folder_abs, track.file_path)
            track.checksum_type        = fp.fingerprint_type
            track.expected_checksum    = expected
            track.checksum_status      = verify_track_checksum(abs_path, fp.fingerprint_type, expected)
            track.checksum_verified_at = now
            checked += 1
    db.session.commit()
    return checked, now


@bp.route("/<int:recording_id>/verify-checksums", methods=["POST"])
@login_required
def verify_checksums(recording_id):
    """
    (Re-)validate this recording's fingerprint checksums against the audio
    files currently sitting in the library. Safe to call any time — nothing
    here depends on the original source folder still existing. See
    _rematch_and_verify_checksums() above for the actual matching logic,
    shared with the Rename Files action.
    """
    rec = db.session.get(Recording, recording_id)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    folder_abs   = recording_folder_abs(rec)
    checked, now = _rematch_and_verify_checksums(rec, folder_abs)

    return jsonify({
        "verified_at": now.isoformat(),
        "checked":     checked,
        "tracks": [
            {"id": t.id, "checksum_type": t.checksum_type,
             "checksum_status": t.checksum_status, "expected_checksum": t.expected_checksum}
            for t in rec.tracks
        ],
    })
