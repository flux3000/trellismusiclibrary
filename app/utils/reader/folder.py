"""
Folder-name template reader (Resolver v2, chunk 6, 2026-10-04).

Trellis names a folder "Artist - Date - Venue - Location (Source)" (app/utils/folder_naming.py)
from metadata a person reviewed. A folder that already wears that name is evidence about the
show, independent of the info file: when the info text and the name say the same artist,
venue or city, the text was read right. This reads the name back; it never fills a field
by itself (the resolver uses it only to corroborate what another source states).

parse_template("Pat Metheny Group - 1979-06-14 - Stars - Philadelphia, PA (SBD)")
    -> {"artist": "Pat Metheny Group", "venue": "Stars", "city": "Philadelphia",
        "state": "PA", "place": "Philadelphia, PA"}

None when the name is not in the template. "Unknown Venue" and "Unknown Location" read as
absent. A taper's name ("gd77-05-08.sbd.miller.flac16") is not in the template.
"""
import re

_D = r"\d{4}(?:-(?:\d{2}|\?\?)(?:-(?:\d{2}|\?\?))?)?"
_DATE = rf"(?:{_D}(?: to {_D})?|Unknown Date)"
_TEMPLATE = re.compile(rf"^(?P<artist>.+?) - (?P<date>{_DATE}) - (?P<rest>.+)$")
_SRC_TAIL = re.compile(r"\s*\([^()]{1,24}\)\s*$")
_STATE = re.compile(r"^[A-Z]{2}$")


def parse_template(name):
    m = _TEMPLATE.match((name or "").strip())
    if not m:
        return None
    artist = m.group("artist").strip()
    rest = _SRC_TAIL.sub("", m.group("rest")).strip()
    if " - " not in rest:
        return None
    venue, place = rest.rsplit(" - ", 1)
    venue, place = venue.strip(), place.strip()
    out = {"artist": artist, "venue": None if venue == "Unknown Venue" else venue,
           "city": None, "state": None, "place": None}
    if place and place != "Unknown Location":
        out["place"] = place
        if "," in place:
            city, tail = (x.strip() for x in place.rsplit(",", 1))
            out["city"] = city or None
            if _STATE.match(tail):
                out["state"] = tail
        else:
            out["city"] = place
    return out
