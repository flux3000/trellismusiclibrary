"""utils/venues.py — placeholder-venue detection.

(and similar stand-ins like "TBD" or "N/A") were being resolved like any other
venue name — matched case-insensitively to one shared Venue row and reused
across every recording that didn't have a real venue yet. Since Venue is meant
to be one canonical physical place, this silently shared a single row's
city/state/country across shows that are, in reality, nowhere near each other —
see scripts/audit_placeholder_venues.py for the confirmed contamination (venue
#28, shared by a 1969 Denver-labeled show and a genuinely-Denver 1996 show).

A name on this list should be treated exactly like "no venue name provided
at all" everywhere a venue gets resolved or displayed as authoritative:
never create/match a Venue row for it, and let city/state/country fall
through to the Performance's own location fields instead (the same fallback
already used when there's no venue at all — see Performance's docstring).
"""

PLACEHOLDER_VENUE_NAMES = {"unknown venue", "unknown", "tbd", "n/a", "various"}


def is_placeholder_venue_name(name):
    """True if `name` (any case/whitespace) is a recognized placeholder, not
    a real venue name."""
    if not name:
        return False
    return name.strip().lower() in PLACEHOLDER_VENUE_NAMES
