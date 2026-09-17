"""
utils/format.py — Shared formatting helpers for API responses.

Single source of truth for turning the DB's split year/month/day columns into
a display string. Used by every endpoint that serialises a performance date
(musicians, performances, venues, events, debug) so the format never drifts.

Note: folder_naming.py has its own date formatter that emits a filesystem
placeholder ("Unknown Date") instead of None — that's intentional and separate,
because a folder name must always be a non-empty string.
"""


def format_partial_date(year, month, day):
    """
    Build an ISO-ish date string from nullable year/month/day integers.

      (1977, 5, 8) -> "1977-05-08"
      (1977, 5, None) -> "1977-05"
      (1977, None, None) -> "1977"
      (None, ...) -> None        (year is required for a meaningful date)
    """
    if not year:
        return None
    if month and day:
        return f"{year}-{month:02d}-{day:02d}"
    if month:
        return f"{year}-{month:02d}"
    return str(year)
