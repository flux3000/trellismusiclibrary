"""
app/utils/musicbrainz.py — MusicBrainz artist lookup.

Fetches the structured facts that make a Performer page read like a music site
rather than a file listing: type (Group/Person), origin, active years, a
disambiguation phrase, and external links. Runs once when a Performer is
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
failure can never block an ingest or a manual Performer create.
"""

import json
import time
import logging
import urllib.parse
import urllib.request
import urllib.error

from app.utils.net import SSL_CONTEXT

log = logging.getLogger(__name__)

_BASE = "https://musicbrainz.org/ws/2"

# MusicBrainz REQUIRES a descriptive User-Agent identifying the application and
# a contact. Requests with a generic agent are rejected or throttled hard.
_UA = "FluxAudio/1.0 ( https://github.com/flux3000/fluxaudio )"

# Their published rate limit is 1 request/second on the free endpoint. We make
# at most a couple of calls per Performer creation, so a simple process-wide
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
# import creating 40 new Performers would then spend eight minutes of an ingest
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

    Off under TESTING unconditionally: `resolve_or_create_performer()` is
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
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
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
    Candidate matches for a performer name, best first.

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


def apply_to_performer(performer, summary, links=None, status="matched"):
    """
    Copy a resolved MusicBrainz summary onto a Performer.

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
    performer.mbid              = summary.get("mbid")
    performer.mb_type           = summary.get("type")
    performer.mb_area           = summary.get("area")
    performer.mb_begin          = summary.get("begin")
    performer.mb_end            = summary.get("end")
    performer.mb_disambiguation = summary.get("disambiguation")
    performer.mb_links_json     = json.dumps(links or summary.get("links") or {})
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
    performer.mb_extra_json     = json.dumps({
        "name":    summary.get("name"),
        "related": summary.get("related") or [],
    })
    performer.mb_status         = status
    performer.mb_checked_at     = datetime.now(timezone.utc)
    return performer


def try_match_performer(performer):
    """
    The automatic pass: search, gate, and record the outcome.

    Always sets `mb_status` so the UI can tell "never looked" (None) from
    "looked and found nothing" ('none') from "needs you to choose"
    ('ambiguous'). That distinction is the whole reason the column exists —
    without it the performer page can't know whether to offer a Match button.

    Returns the status string, or None when lookups are disabled or the
    breaker has tripped — leaving `mb_status` NULL so the row is retried on a
    later run rather than being recorded as a genuine "no match". Never raises.
    """
    from datetime import datetime, timezone
    if not enabled() or tripped():
        return None
    try:
        candidates = search_artist(performer.name)
        status, best = classify(candidates)
        if status == "matched":
            details = lookup_details(best["mbid"]) or best
            apply_to_performer(performer, details, details.get("links"))
            return "matched"
        performer.mb_status     = status
        performer.mb_checked_at = datetime.now(timezone.utc)
        return status
    except Exception as e:                                   # noqa: BLE001
        # Belt and braces — _get already swallows network errors, but this runs
        # inside ingest and must not be able to fail it under any circumstance.
        log.warning("musicbrainz match failed for %r: %s", performer.name, e)
        return "none"
