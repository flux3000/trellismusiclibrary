"""
app/utils/health.py — ingest-readiness health score for a scanned folder.

compute_health(scan) is a PURE function over the scan payload produced by
POST /api/recordings/scan (suggestions.from_tags + from_info_file + audio_files).

Model — a transparent completeness ratio:

    score = 100 × (populated fields) / (total fields)

    Core fields (8):  Performer · Date · Venue · City · State · Country ·
                      Source · Lineage
    Track fields (N): one per audio file — the track's *real* title.

A field counts as populated when it has clean, non-null data after merging the
FLAC tags and the parsed info file. A track counts only when it has a *real*
title — generic placeholders ("Track 01", "Untitled", a bare number, a
disc/track code like "d1t02") do NOT count. Date is graded by precision
(full Y-M-D = 1.0, Y-M = 0.67, Y = 0.33).

Every track that lacks a real title, and every empty core field, pulls the
score down by exactly one field's worth — so the number always answers
"how much of this recording's metadata is actually filled in?"

Bands: >=85 green · 60-84 yellow · <60 red · no audio = 0/red.
"""

import re

# Titles that are placeholders, not real song names.
_PLACEHOLDER_WORDS = {"untitled", "unknown", "unknown title", "audio track", "track"}
# "track 01", "trk3", "t-01", "d1t02", "cd2 track 5", "01", "side a track 1", …
_PLACEHOLDER_RE = re.compile(
    r"^(?:(?:cd|disc|disk|side)\s*\w*\s*)?(?:track|trk|t|d\d+\s*t)\s*[-_. ]?\s*\d+$"
    r"|^\d+$",
    re.IGNORECASE,
)


def _f(dimension, delta, msg, ai_recoverable):
    """One factor row (delta is the points LOST, i.e. negative)."""
    return {"dimension": dimension, "delta": int(delta), "msg": msg,
            "ai_recoverable": ai_recoverable}


def _norm(v):
    return " ".join(str(v).strip().lower().split()) if v else ""


def _present(v):
    return bool(v and str(v).strip())


def _is_real_title(title):
    """True only for a genuine song/segment name (not a placeholder)."""
    t = _norm(title)
    if not t:
        return False
    if t in _PLACEHOLDER_WORDS:
        return False
    if _PLACEHOLDER_RE.match(t):
        return False
    return True


def _date_precision(tags, info):
    """Best available date precision: 3=full, 2=Y-M, 1=Y, 0=none."""
    prec = 0
    cd = (tags.get("concert_date") or "").strip()
    if cd:
        parts = cd.split("-")
        if len(parts) >= 3 and all(parts[:3]):
            prec = 3
        elif len(parts) == 2 and all(parts):
            prec = 2
        elif parts and parts[0]:
            prec = 1
    if info.get("day"):
        prec = max(prec, 3)
    elif info.get("month"):
        prec = max(prec, 2)
    elif info.get("year"):
        prec = max(prec, 1)
    return prec


def _real_title_count(tags, info, n):
    """How many of the n audio tracks have a real title (tags preferred, info fallback)."""
    tag_tracks  = tags.get("tracks") or []
    info_tracks = info.get("tracks") or []
    count = 0
    for i in range(n):
        title = tag_tracks[i].get("title") if i < len(tag_tracks) else ""
        if not _is_real_title(title) and i < len(info_tracks):
            title = info_tracks[i].get("title") or title
        if _is_real_title(title):
            count += 1
    return count


def compute_health(scan):
    """Score a scan payload. Returns {score, band, factors, populated, total}."""
    sugg = scan.get("suggestions") or {}
    tags = sugg.get("from_tags") or {}
    info = sugg.get("from_info_file") or {}
    audio_files = scan.get("audio_files") or []
    n_audio = scan.get("audio_file_count") or len(audio_files)

    factors = []
    populated = 0.0
    total = 0

    # ── Core fields (each worth one field; Date graded by precision) ───────────
    def core(name, present):
        nonlocal populated, total
        total += 1
        if present:
            populated += 1
        else:
            factors.append(_f("Identity", -1, "No %s" % name.lower(), True))

    core("Performer", _present(tags.get("artist") or info.get("artist")))

    total += 1
    prec = _date_precision(tags, info)
    populated += prec / 3.0
    if prec < 3:
        factors.append(_f("Identity", -1,
                          {2: "Date missing day", 1: "Year only", 0: "No date"}[prec], True))

    core("Venue",   _present(tags.get("venue")   or info.get("venue")))
    core("City",    _present(tags.get("city")    or info.get("city")))
    core("State",   _present(tags.get("state")   or info.get("state")))
    core("Country", _present(tags.get("country") or info.get("country")))
    core("Source",  _present(tags.get("source")  or info.get("source")))
    core("Lineage", _present(tags.get("lineage") or info.get("lineage")))

    # ── Track titles (one field per audio track) ──────────────────────────────
    named   = _real_title_count(tags, info, n_audio)
    total  += n_audio
    populated += named
    unnamed = n_audio - named
    if unnamed > 0:
        factors.append(_f("Tracks", -unnamed,
                          "%d of %d track%s lack a real title"
                          % (unnamed, n_audio, "" if n_audio == 1 else "s"), True))

    # ── Final ratio ───────────────────────────────────────────────────────────
    if n_audio == 0 or total == 0:
        score = 0
        # Say WHY there's no audio when we can (2026-08-26 — a .shn-only
        # folder used to just score 0 with an empty factors list, no
        # different from a genuinely empty directory). Not ai_recoverable:
        # this is a file-format problem, nothing the AI Research Assistant
        # can fill in.
        unsupported = scan.get("unsupported_audio") or []
        if unsupported:
            exts = sorted({u.get("ext", "") for u in unsupported if u.get("ext")})
            factors.append(_f(
                "Audio", 0,
                "%d file%s found in an unsupported format (%s) — not readable yet"
                % (len(unsupported), "" if len(unsupported) == 1 else "s",
                   ", ".join(exts)),
                False,
            ))
    else:
        score = max(0, min(100, round(100 * populated / total)))
    band = "green" if score >= 85 else "yellow" if score >= 60 else "red"

    factors.sort(key=lambda x: x["delta"])
    return {"score": score, "band": band, "factors": factors,
            "populated": round(populated, 2), "total": total,
            # Exposed so the Metadata Quality panel can state the track-title
            # position directly ("18 of 20 named") instead of re-deriving it by
            # parsing the factor message string (2026-08-02).
            "tracks_named": named, "tracks_total": n_audio,
            # 3=Y-M-D, 2=Y-M, 1=Y, 0=none. Same reason: the panel grades the
            # Date row by precision exactly as the score does.
            "date_precision": prec}
