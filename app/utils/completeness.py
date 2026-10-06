"""
Metadata completeness band for the import queue (2026-10-05).

A band read off the resolver's Resolved: is the recording described, whatever the sources
that described it. (Paula's score measures corroboration, so an untagged collection read
Low even when complete.) Five things count: artist, a full date, venue, location (a city
plus a state or country) and a real title on every audio track. Source and lineage do not.

    A studio record counts only artist, date (a year is enough) and tracks.

    green  (High)    all five present
    yellow (Medium)  exactly one of venue, location, track titles missing
    red    (Low)     two or more missing, or the artist or the date missing

Bands use the same tokens as app/utils/health.py (green / yellow / red).
"""
from app.utils.health import RATING, _is_real_title

FIELDS = ("artist", "date", "venue", "location", "tracks")
RANK = {"green": 2, "yellow": 1, "red": 0}      # for sorting: High > Medium > Low


def _band(missing):
    if not missing:
        return "green"
    if len(missing) >= 2 or "artist" in missing or "date" in missing:
        return "red"
    return "yellow"


def missing_fields(resolved):
    """The names in FIELDS the Resolved lacks (venue and location are not asked of a studio record)."""
    if resolved is None:
        return list(FIELDS)
    studio = getattr(resolved, "kind", None) == "studio"
    d = resolved.date.value or {}
    tracks = resolved.tracks or []
    out = []
    if not resolved.artist.value:
        out.append("artist")
    if not (d.get("year") if studio else (d.get("year") and d.get("month") and d.get("day"))):
        out.append("date")
    if not studio and not resolved.venue.value:
        out.append("venue")
    if not studio and not (resolved.city.value and (resolved.state.value or resolved.country.value)):
        out.append("location")
    if not tracks or not all(_is_real_title(t.get("title")) for t in tracks):
        out.append("tracks")
    return out


def band_from_meta(meta, kind=None):
    """The same band from a stored queue row's `meta` blob (bulk_ingest_run.process): used once
    for rows extracted before the band was a completeness band. None when nothing was read. The blob lists only titled
    tracks, so every audio file needs a real title among them."""
    meta = meta or {}
    if not any(meta.get(k) for k in ("artist", "date_text", "venue", "city", "track_count")):
        return None                       # nothing was read for this row: no band
    studio = kind == "studio"
    dt = str(meta.get("date_text") or "")
    full = len(dt) == 10 and dt[4] == "-" and dt[7] == "-"
    try:
        n = int(meta.get("track_count") or 0)
    except (TypeError, ValueError):
        n = 0
    real = sum(1 for t in (meta.get("tracks") or []) if isinstance(t, dict) and _is_real_title(t.get("title")))
    miss = []
    if not meta.get("artist"):
        miss.append("artist")
    if not (dt[:4].isdigit() if studio else full):
        miss.append("date")
    if not studio and not meta.get("venue"):
        miss.append("venue")
    if not studio and not (meta.get("city") and (meta.get("state") or meta.get("country"))):
        miss.append("location")
    if not n or real < n:
        miss.append("tracks")
    return _band(miss)


def completeness_band(resolved):
    """"green" | "yellow" | "red" for a Resolved (None reads red)."""
    return _band(missing_fields(resolved))


def completeness(resolved):
    band = completeness_band(resolved)
    return {"band": band, "rating": RATING[band], "rank": RANK[band], "missing": missing_fields(resolved)}
