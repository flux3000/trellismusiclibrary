"""
app/utils/musicbrainz.py — MusicBrainz artist lookup.

Fetches the structured facts that make an Artist page read like a music site
rather than a file listing: type (Group/Person), origin, active years, a
disambiguation phrase, and external links. Runs once when an Artist is
created (Ryan, 2026-08-07).

WHY THIS IS NOT "AI Assist"
---------------------------
MusicBrainz is a curated database, not a language model. There is no
hallucination surface: a field is either in the database or it isn't. That is
why these facts land on the record directly, while AI Assist's drafted bio
still requires a human to approve it. The two are deliberately separate
features with separate rules.

The risk here is different, and it is WRONG-ENTITY, not wrong-fact: several
real acts share a name. So the confidence gate below is strict, and anything
short of a clear winner is flagged for a human rather than guessed
(`mb_status='ambiguous'`). Ryan's rule: never auto-pick the top match.

MEMBERS ARE NEVER WRITTEN. MusicBrainz carries band membership with date
ranges that maps almost exactly onto our Membership stints — and that is
precisely why it stays read-only. Roster changes cascade into per-show
personnel resolution, and a silent write there is the exact failure mode fixed
in July (the Auto-Ingest members wipe). The member list is returned for display
with an explicit per-person Add; nothing here touches the DB.

OFFLINE IS A SUPPORTED STATE. Flux runs in a PyWebView shell on a single Mac
and is expected to work with no network. Every function here fails soft —
returns None or an empty result, never raises into a caller — so a lookup
failure can never block an ingest or a manual Artist create.
"""

import json
import time
import logging
import urllib.parse
import urllib.request
import urllib.error

from app.utils.net import SSL_CONTEXT, USER_AGENT

log = logging.getLogger(__name__)

_BASE = "https://musicbrainz.org/ws/2"

# MusicBrainz REQUIRES a descriptive User-Agent identifying the application and
# a contact. Requests with a generic agent are rejected or throttled hard.
# It lives in app/utils/net.py, with the rest of how this app presents itself
# on the wire — see that file for why it used to be wrong.

# Their published rate limit is 1 request/second on the free endpoint. We make
# at most a couple of calls per Artist creation, so a simple process-wide
# spacer is sufficient — no queue, no backoff ladder.
_MIN_INTERVAL = 1.1
_last_call = [0.0]

_TIMEOUT = 6.0          # short: a hung lookup must not stall an ingest job

# Confidence gate. MusicBrainz returns a 0-100 `score` per candidate.
#   - top score must clear MIN_SCORE at all, and
#   - it must beat the runner-up by MARGIN.
# The margin is the part that matters: "The Meters" scoring 100 with a
# tribute band right behind it at 98 is NOT a confident match, however high
# the top number looks on its own.
_MIN_SCORE = 88
_MARGIN = 12


# Circuit breaker. Offline, every call burns the full _TIMEOUT — and a bulk
# import creating 40 new Artists would then spend eight minutes of an ingest
# job waiting on DNS that is never going to answer. After this many consecutive
# failures the module stops trying for the life of the process; any success
# resets it. Deliberately process-scoped and not persisted: restarting the app
# is the natural "try again", and nothing should have to remember that Flux was
# once offline.
_MAX_CONSECUTIVE_FAILURES = 3
_failures = [0]


def tripped():
    return _failures[0] >= _MAX_CONSECUTIVE_FAILURES


def reset_breaker():
    """Clear the failure count — for an explicit user-initiated retry."""
    _failures[0] = 0


def enabled():
    """
    Whether lookups may run at all.

    Off under TESTING unconditionally: `resolve_or_create_artist()` is
    exercised throughout the test suite, and a unit test must never depend on
    a network round-trip to musicbrainz.org. Also honours a
    MUSICBRAINZ_ENABLED config flag so it can be switched off entirely.
    """
    try:
        from flask import current_app
        if current_app.config.get("TESTING"):
            return False
        return bool(current_app.config.get("MUSICBRAINZ_ENABLED", True))
    except Exception:                                        # noqa: BLE001
        # No app context (a bare script): allow it. Scripts that call this are
        # explicitly doing lookups.
        return True


def _throttle():
    gap = time.time() - _last_call[0]
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _last_call[0] = time.time()


def _get(path, params):
    """GET one MusicBrainz endpoint, returning parsed JSON or None.

    Never raises. Every failure mode — offline, DNS, 503, rate limit, malformed
    JSON — is the same outcome to the caller: no data, carry on.
    """
    params = dict(params or {})
    params["fmt"] = "json"
    url = f"{_BASE}/{path}?{urllib.parse.urlencode(params)}"
    if tripped():
        return None
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        _throttle()
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=SSL_CONTEXT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _failures[0] = 0
        return data
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ValueError, OSError) as e:
        _failures[0] += 1
        log.info("musicbrainz lookup failed (%s): %s", url, e)
        return None


def _area_name(artist):
    """Origin as a display string — 'New Orleans, US' where both are known.

    Prefers `begin-area` (where the act formed) over `area` (where it is now
    associated), because for a live-recording archive the formation city is the
    more meaningful fact.
    """
    begin = (artist.get("begin-area") or {}).get("name")
    area = (artist.get("area") or {}).get("name")
    parts = [p for p in (begin, area) if p]
    # De-duplicate the common case where both fields hold the same value.
    out, seen = [], set()
    for p in parts:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return ", ".join(out) or None


def _summarise(artist):
    """Flatten one MusicBrainz artist object into our stored shape."""
    life = artist.get("life-span") or {}
    return {
        "mbid":           artist.get("id"),
        "name":           artist.get("name"),
        "type":           artist.get("type"),           # Group / Person / ...
        "area":           _area_name(artist),
        "begin":          life.get("begin"),            # "1965" or "1965-03-01"
        "end":            life.get("end"),
        "ended":          bool(life.get("ended")),
        "disambiguation": artist.get("disambiguation") or None,
        "score":          artist.get("score"),
    }


def search_artist(name, limit=6):
    """
    Candidate matches for an artist name, best first.

    Returns a list of summary dicts (possibly empty). Used both by the
    automatic pass and by the manual "resolve this match" picker, so the two
    can never disagree about what the candidates are.
    """
    if not name or not name.strip():
        return []
    data = _get("artist/", {"query": f'artist:"{name.strip()}"', "limit": limit})
    if not data:
        return []
    return [_summarise(a) for a in data.get("artists", [])]


def classify(candidates):
    """
    Decide whether a candidate list is a confident match.

    Returns ('matched', candidate) | ('ambiguous', None) | ('none', None).

    Kept separate from search_artist() and free of I/O so the gate can be unit
    tested without touching the network — the thresholds are the part most
    likely to need tuning once real library names run through it.
    """
    if not candidates:
        return "none", None
    top = candidates[0]
    if (top.get("score") or 0) < _MIN_SCORE:
        return "ambiguous", None
    if len(candidates) > 1:
        runner_up = candidates[1].get("score") or 0
        if (top["score"] - runner_up) < _MARGIN:
            return "ambiguous", None
    return "matched", top


# Human-readable names for MusicBrainz's link relation types. Their raw values
# are inconsistent ("setlistfm" vs "official homepage" vs "IMDb"), and title-
# casing them mechanically produces "Setlistfm".
_LINK_LABELS = {
    "wikipedia": "Wikipedia", "wikidata": "Wikidata", "discogs": "Discogs",
    "official homepage": "Official site", "allmusic": "AllMusic",
    "setlistfm": "setlist.fm", "IMDb": "IMDb", "songkick": "Songkick",
    "bandcamp": "Bandcamp", "soundcloud": "SoundCloud", "youtube": "YouTube",
    "last.fm": "Last.fm", "social network": "Social", "fanpage": "Fan page",
    "lyrics": "Lyrics", "purchase for download": "Buy", "streaming": "Streaming",
    "free streaming": "Streaming", "VIAF": "VIAF", "BBC Music page": "BBC Music",
    "other databases": "Database",
}

# Relation types that describe another ACT rather than a person's membership.
# Worth surfacing on an archive page: they're how you navigate between related
# recordings ("this act renamed itself into that one").
_ARTIST_REL_LABELS = {
    "collaboration":  "Collaborated with",
    "is person":      "Is",
    "artist rename":  "Renamed",
    "subgroup":       "Subgroup of",
    "supporting musician": "Supported",
    "tribute":        "Tribute to",
    "founder":        "Founded",
}


def lookup_details(mbid):
    """
    Full detail for a known MBID — external links, band members, related acts.

    LINKS ARE THE POINT. Aliases, community tags and gender were fetched here
    briefly on 2026-08-07 and cut the same day: none of it was interesting on
    the page, and fetching data nothing displays is pure cost. If alias-based
    name reconciliation ever becomes a feature, add `+aliases` back to `inc`
    then — not speculatively now.

    `members` and `related` are returned FOR DISPLAY ONLY. See the module
    docstring: nothing in this file may write to Membership.
    """
    if not mbid:
        return None
    data = _get(f"artist/{mbid}", {"inc": "url-rels+artist-rels"})
    if not data:
        return None

    links, members, related = {}, [], []
    for rel in data.get("relations", []) or []:
        rtype = rel.get("type")
        if rel.get("url"):
            label = _LINK_LABELS.get(rtype)
            # Unknown relation types are skipped rather than shown raw:
            # MusicBrainz exposes dozens, most of them catalogue plumbing
            # ("BookBrainz", "IMSLP") that means nothing on this page.
            if label and label not in links:
                links[label] = rel["url"]["resource"]
        elif rel.get("artist"):
            a = rel["artist"]
            if rtype == "member of band":
                members.append({
                    "name":      a.get("name"),
                    "mbid":      a.get("id"),
                    "begin":     rel.get("begin") or None,
                    "end":       rel.get("end") or None,
                    "ended":     bool(rel.get("ended")),
                    "instrument": ", ".join(rel.get("attributes") or []) or None,
                })
            elif rtype in _ARTIST_REL_LABELS:
                related.append({
                    "name":     a.get("name"),
                    "mbid":     a.get("id"),
                    "relation": _ARTIST_REL_LABELS[rtype],
                })

    out = _summarise(data)
    out["links"]    = links
    out["members"]  = members
    out["related"]  = related
    return out


def apply_to_artist(artist, summary, links=None, status="matched"):
    """
    Copy a resolved MusicBrainz summary onto an Artist.

    `status` records HOW the link happened and must stay honest:
        'matched' — the confidence gate picked it with no human involved
        'linked'  — a human chose it from the candidate list
    The page labels these differently ("Matched automatically" vs "Linked by
    you"), and claiming the former when a person did the work is a small lie
    that makes every other automatic claim less believable.

    Does NOT commit — the caller owns the transaction, matching every other
    mutation helper in the app. Does not touch name, bio, genre or members:
    those are Ryan's fields, and MusicBrainz is not allowed to overwrite a
    human's curation.
    """
    from datetime import datetime, timezone
    artist.mbid              = summary.get("mbid")
    artist.mb_type           = summary.get("type")
    artist.mb_area           = summary.get("area")
    artist.mb_begin          = summary.get("begin")
    artist.mb_end            = summary.get("end")
    artist.mb_disambiguation = summary.get("disambiguation")
    artist.mb_links_json     = json.dumps(links or summary.get("links") or {})
    # Trimmed 2026-08-07 to what the page actually shows. Aliases, community
    # tags and gender were fetched, stored and displayed for one afternoon;
    # Ryan cut the display, so fetching them was pure cost. `related` is kept
    # only because it costs nothing extra (same artist-rels call as members).
    # `name` is MusicBrainz's spelling of the act, kept because the panel shows
    # WHICH entry we linked to — ours may differ ("Meters" vs "The Meters") and
    # that difference is the whole point of showing it.
    #
    # `links` stay STORED but are no longer displayed (Ryan, 2026-08-07): their
    # job is telling future ingest/enrichment jobs where to look for information
    # about this act, not giving the user a list to read.
    artist.mb_extra_json     = json.dumps({
        "name":    summary.get("name"),
        "related": summary.get("related") or [],
    })
    artist.mb_status         = status
    artist.mb_checked_at     = datetime.now(timezone.utc)
    return artist


def try_match_artist(artist):
    """
    The automatic pass: search, gate, and record the outcome.

    Always sets `mb_status` so the UI can tell "never looked" (None) from
    "looked and found nothing" ('none') from "needs you to choose"
    ('ambiguous'). That distinction is the whole reason the column exists —
    without it the artist page can't know whether to offer a Match button.

    Returns the status string, or None when lookups are disabled or the
    breaker has tripped — leaving `mb_status` NULL so the row is retried on a
    later run rather than being recorded as a genuine "no match". Never raises.
    """
    from datetime import datetime, timezone
    if not enabled() or tripped():
        return None
    try:
        candidates = search_artist(artist.name)
        status, best = classify(candidates)
        if status == "matched":
            details = lookup_details(best["mbid"]) or best
            apply_to_artist(artist, details, details.get("links"))
            return "matched"
        artist.mb_status     = status
        artist.mb_checked_at = datetime.now(timezone.utc)
        return status
    except Exception as e:                                   # noqa: BLE001
        # Belt and braces — _get already swallows network errors, but this runs
        # inside ingest and must not be able to fail it under any circumstance.
        log.warning("musicbrainz match failed for %r: %s", artist.name, e)
        return "none"


# ─────────────────────────────────────────────────────────────────────────
# Release lookup (Studio Records spec v1, section 2)
#
# A studio recording's ALBUM tag (Recording.title) plus its artist name is
# searched against MusicBrainz's release/ endpoint, the SAME confidence gate
# as the artist lookup above decides confidence, and a track-count check
# narrows further -- a release whose total track count is more than one off
# from the recording's own track count is dropped as a candidate before the
# gate ever sees it. Everything this section fills is fill-if-null: a value
# the collector's own tags or a human already set is never touched.
# ─────────────────────────────────────────────────────────────────────────

# release-group primary types MusicBrainz returns, kept verbatim (Album,
# Live, Compilation, EP, Single, Other, ...) -- see section 2's "Release
# type and the live/studio call": a Live release type does NOT flip
# Recording.kind. That is a human decision for a later pass.


def _summarise_release(release):
    """Flatten one MusicBrainz release object into our stored shape."""
    rg = release.get("release-group") or {}
    media = release.get("media") or []

    track_count = release.get("track-count")
    if track_count is None and media:
        track_count = sum(m.get("track-count") or 0 for m in media)

    label_info = (release.get("label-info") or [{}])[0] or {}
    label = (label_info.get("label") or {}).get("name")

    return {
        "mbid":             release.get("id"),
        "title":            release.get("title"),
        "release_group_id": rg.get("id"),
        "release_type":     rg.get("primary-type"),
        "label":            label,
        "catalog_number":   label_info.get("catalog-number"),
        "date":             release.get("date") or None,
        "country":          release.get("country") or None,
        "track_count":      track_count,
        "score":            release.get("score"),
    }


def search_release(artist_name, title, limit=8):
    """
    Candidate release matches for an artist name + album title, best first.

    Returns a list of summary dicts (possibly empty). `inc` asks for exactly
    what classify_release()/apply_to_recording() need: release-group (for
    the primary type) and label-info (label name + catalog number); date,
    country and track-count/media come back on a release search by default.
    Same `_throttle`/`_get` plumbing as search_artist() -- one shared
    breaker and rate limiter for every MusicBrainz call this app makes.
    """
    if not artist_name or not artist_name.strip() or not title or not title.strip():
        return []
    # Lucene special characters `"` and `\` inside a quoted phrase must be
    # escaped, or a title like Say "Hi" produces a malformed query and a 400
    # from MusicBrainz that the breaker counts as a failure (N3).
    def _escape(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')
    query = 'artist:"%s" AND release:"%s"' % (
        _escape(artist_name.strip()), _escape(title.strip()))
    data = _get("release/", {"query": query, "limit": limit,
                             "inc": "labels+release-groups"})
    if not data:
        return []
    return [_summarise_release(r) for r in data.get("releases", [])]


def classify_release(candidates, track_count=None):
    """
    Decide whether a release candidate list is a confident match.

    Returns (status, winner_or_None, ranked) where `ranked` is the candidate
    list actually considered (after the track-count filter below), for the
    human picker to show. `ranked` keeps MusicBrainz's original order so the
    picker still shows every edition, best first.

    MusicBrainz scores are relative to the SEARCH's own top hit, and a
    release search returns every edition of an album (US CD, UK LP,
    remaster) as separate candidates each scoring near the top score. Gating
    on releases directly means an album with several editions always reads
    as ambiguous (each edition suppresses the others' margin) while an album
    with exactly one edition can pass on a single unrelated hit. So the gate
    runs on RELEASE GROUPS: each group's best-scoring release stands in for
    the group, the score gate (_MIN_SCORE, _MARGIN) runs across groups, and
    only once a winning group is chosen do we pick a release inside it --
    preferring the one whose track count matches the recording, else the
    earliest-dated release (S6).

    The track-count filter still runs first, same as before: a candidate
    whose track_count differs from the recording's by more than one is
    dropped before grouping ever sees it.
    """
    if not candidates:
        return "none", None, []

    ranked = candidates
    if track_count:
        ranked = [c for c in candidates
                 if c.get("track_count") is None
                 or abs(c["track_count"] - track_count) <= 1]
        if not ranked:
            return "none", None, []

    groups = {}
    group_order = []
    for c in ranked:
        key = c.get("release_group_id") or ("_ungrouped", c.get("mbid"))
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(c)

    group_best = [(key, max(groups[key], key=lambda c: c.get("score") or 0))
                  for key in group_order]
    group_best.sort(key=lambda kb: kb[1].get("score") or 0, reverse=True)

    top_key, top_best = group_best[0]
    if (top_best.get("score") or 0) < _MIN_SCORE:
        return "ambiguous", None, ranked
    if len(group_best) > 1:
        runner_up = group_best[1][1].get("score") or 0
        if (top_best["score"] - runner_up) < _MARGIN:
            return "ambiguous", None, ranked

    winning_group = groups[top_key]
    exact = [c for c in winning_group if c.get("track_count") == track_count] \
        if track_count else []
    if exact:
        winner = exact[0]
    else:
        dated = [c for c in winning_group if c.get("date")]
        winner = min(dated, key=lambda c: c["date"]) if dated else top_best
    return "matched", winner, ranked


def lookup_release(mbid):
    """
    Full detail for a known release MBID -- track titles and positions, so
    apply_to_recording() can fill untitled tracks.

    `inc=recordings+media+labels+release-groups` pulls the medium/track
    structure (recordings gives each track's title), label-info and the
    release-group's primary type in one call. Track position is
    renumbered CONTINUOUSLY across media in the order MusicBrainz returns
    them, matching Recording.tracks' own continuous track_number (a
    disc lives in a subfolder or a filename here, never a separate
    numbering scheme -- see project_multi_disc_detection.md).
    """
    if not mbid:
        return None
    data = _get("release/%s" % mbid,
               {"inc": "recordings+media+labels+release-groups"})
    if not data:
        return None

    summary = _summarise_release(data)
    tracks = []
    position = 0
    for medium in data.get("media") or []:
        for t in medium.get("tracks") or []:
            position += 1
            rec_title = (t.get("recording") or {}).get("title")
            tracks.append({"position": position, "title": rec_title or t.get("title")})
    summary["tracks"] = tracks
    return summary


def _parse_partial_date(date_str):
    """'1977', '1977-05', '1977-05-08' -> (year, month, day), each or None.

    MusicBrainz does emit zero-padded and malformed partial dates
    ('1977-00-00', '1977-13-40'). '00' and any out-of-range part are
    invalid; if EITHER month or day is invalid, both come back None (a
    date whose month or day cannot be trusted is not a date we can trust
    the other half of either) -- year is unaffected either way (B3).
    """
    if not date_str:
        return None, None, None
    parts = date_str.split("-")

    def _year_or_none(s):
        return int(s) if s and s.isdigit() else None

    def _bounded(s, lo, hi):
        """(value_or_None, was_present_and_valid)."""
        if not s or not s.isdigit():
            return None, s is None or s == ""
        n = int(s)
        return (n, True) if lo <= n <= hi else (None, False)

    year = _year_or_none(parts[0]) if len(parts) > 0 else None

    month, month_ok = (_bounded(parts[1], 1, 12) if len(parts) > 1
                       else (None, True))
    day, day_ok = (_bounded(parts[2], 1, 31) if len(parts) > 2
                   else (None, True))
    if not month_ok or not day_ok:
        month, day = None, None
    return year, month, day


def apply_to_recording(rec, release, status="matched"):
    """
    Copy a resolved MusicBrainz release onto a Recording, fill-if-null only.

    Never overwrites a value the collector's tags or a person already
    supplied:
      - Performance.start_year/month/day from the release date, one field
        at a time, only where that field is still null. A year-only
        release date fills the year and leaves month/day untouched.
      - Track.title, only for a track whose title is null or empty, matched
        by CONTINUOUS position across media, and only when the release's
        total track count equals the recording's own track count -- a
        mismatch means the position mapping cannot be trusted.
      - The eight mb_release_* columns and `mb_release_status = status`,
        always (this is Trellis's own bookkeeping, not the collector's
        data).

    Writes one RecordingEvent 'mb_release_matched' naming every field
    actually filled -- omitted when nothing was (e.g. every field was
    already set). Does NOT commit -- the caller owns the transaction, same
    as apply_to_artist().
    """
    from datetime import datetime, timezone

    filled = []

    perf = rec.performance
    if perf is not None:
        year, month, day = _parse_partial_date(release.get("date"))
        # A studio ingest always creates its own Performance (Ryan,
        # 2026-09-27) -- but apply_to_recording() is also called from
        # scripts and tests against older data, so guard against a
        # Performance that is still shared by more than one studio
        # recording: writing this release's date onto it would leak onto
        # every other recording that shares the row (B1). scripts/
        # migrate_studio_records.py splits these apart for existing data.
        studio_siblings = [r for r in (perf.recordings or []) if r.kind == "studio"]
        shared = len(studio_siblings) > 1
        if shared:
            if year is not None or month is not None or day is not None:
                filled.append("skipped: shared performance")
        else:
            year_was_null = perf.start_year is None
            if year_was_null and year is not None:
                perf.start_year = year
                filled.append("start_year")
            # Month/day describe THIS release's date, not a year the
            # collector's own tags already set. Filling them beneath a year
            # we did not just fill ourselves fabricates a full date out of an
            # unrelated edition's release day (B2) -- e.g. a 1990 reissue's
            # month/day landing under a collector-tagged 1977. Only fill
            # when the year is null (and we are filling it now) or the
            # release's own year matches the year already on the
            # Performance.
            year_matches = year is not None and perf.start_year == year
            if year_was_null or year_matches:
                if perf.start_month is None and month is not None:
                    perf.start_month = month
                    filled.append("start_month")
                if perf.start_day is None and day is not None:
                    perf.start_day = day
                    filled.append("start_day")

    release_tracks = release.get("tracks") or []
    rec_tracks = sorted(rec.tracks or [], key=lambda t: t.track_number)
    if release_tracks and len(release_tracks) == len(rec_tracks):
        titles_by_position = {t["position"]: t.get("title") for t in release_tracks}
        for position, track in enumerate(rec_tracks, start=1):
            if track.title and track.title.strip():
                continue
            new_title = titles_by_position.get(position)
            if new_title:
                track.title = new_title
                filled.append("track_%d_title" % position)

    rec.mb_release_id         = release.get("mbid")
    rec.mb_release_group_id   = release.get("release_group_id")
    rec.mb_release_status     = status
    rec.mb_release_type       = release.get("release_type")
    rec.mb_label              = release.get("label")
    rec.mb_catalog_number     = release.get("catalog_number")
    rec.mb_release_country    = release.get("country")
    rec.mb_release_checked_at = datetime.now(timezone.utc)

    if filled:
        from app.extensions import db
        from app.models.recording_event import RecordingEvent
        from app.models.user import User
        # No browser session in the background worker to take a user_id
        # from -- same "the install's owner" query bulk_ingest_run.py's
        # _owner_user_id() uses for exactly the same reason.
        owner = db.session.query(User).filter_by(role="admin", is_active=True).first()
        if owner is not None:
            db.session.add(RecordingEvent(
                recording_id = rec.id,
                user_id      = owner.id,
                event_type   = "mb_release_matched",
                note         = "filled: " + ", ".join(filled),
            ))
    return rec


def try_match_release(rec):
    """
    The automatic pass for one studio recording: search, gate, and record
    the outcome. Mirrors try_match_artist() exactly -- same enabled()/
    tripped() guards, same "always set the status column" contract, same
    never-raises promise.

    A no-op (returns None, mb_release_status untouched) for anything that
    is not kind == 'studio' -- live recordings are never looked up (section
    2, "Never for live recordings").
    """
    from datetime import datetime, timezone
    if rec.kind != "studio":
        return None
    if not enabled() or tripped():
        return None
    try:
        artist_name = rec.performance.artist.name if rec.performance and rec.performance.artist else None
        track_count = len(rec.tracks or [])
        candidates = search_release(artist_name, rec.title)
        status, best, _ranked = classify_release(candidates, track_count)
        if status == "matched":
            details = lookup_release(best["mbid"]) or best
            apply_to_recording(rec, details, status="matched")
            return "matched"
        rec.mb_release_status     = status
        rec.mb_release_checked_at = datetime.now(timezone.utc)
        return status
    except Exception as e:                                   # noqa: BLE001
        log.warning("musicbrainz release match failed for recording %r: %s",
                   getattr(rec, "id", None), e)
        # Must set the status column even on the exception path (same
        # contract as the happy path above) -- otherwise enqueue_followups()'s
        # "mb_release_status IS NULL" query re-selects this recording and
        # retries it, forever, on every boot (S5).
        # "error", not "none": "none" means MusicBrainz answered with no
        # candidates; an exception means we never got an answer. The Release
        # block treats both as "not linked" and offers Find release.
        rec.mb_release_status     = "error"
        rec.mb_release_checked_at = datetime.now(timezone.utc)
        return "error"


def link_release(rec, mbid):
    """
    The human path: a person picked a candidate from the picker (S5). Looks
    up full detail and applies it with status 'linked' -- the page tells
    'Matched automatically' from 'Linked by you' apart precisely on this
    column, so it must stay honest.

    Returns the release detail dict on success, None if the lookup failed
    (offline, bad mbid) -- rec is left untouched in that case.
    """
    details = lookup_release(mbid)
    if not details:
        return None
    apply_to_recording(rec, details, status="linked")
    return details


def unlink_release(rec):
    """
    A human explicitly removing a release link (S6). Clears the eight
    mb_release_* columns and sets mb_release_status = 'unlinked' rather
    than NULL, so the follow-up pass's "mb_release_status IS NULL" query
    never re-queues this recording -- an explicit unlink must stick, not
    silently get looked up again on the next enqueue_followups() run.

    Filled Performance dates and Track titles are NOT reverted: once
    applied they are the collector's own data to edit, same as any other
    field a human can change on View Recording.
    """
    from datetime import datetime, timezone
    from app.extensions import db
    from app.models.recording_event import RecordingEvent
    from app.models.user import User

    rec.mb_release_id         = None
    rec.mb_release_group_id   = None
    rec.mb_release_type       = None
    rec.mb_label               = None
    rec.mb_catalog_number      = None
    rec.mb_release_country     = None
    rec.mb_release_status      = "unlinked"
    rec.mb_release_checked_at  = datetime.now(timezone.utc)

    owner = db.session.query(User).filter_by(role="admin", is_active=True).first()
    if owner is not None:
        db.session.add(RecordingEvent(
            recording_id = rec.id,
            user_id      = owner.id,
            event_type   = "mb_release_unlinked",
            note         = None,
        ))
    return rec
