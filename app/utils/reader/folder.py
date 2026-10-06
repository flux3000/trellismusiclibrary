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


MIN_NAME_KEY = 4


def names_artist(folder_name, reading):
    """True when the free-form folder name contains the artist `reading` whole, on word
    boundaries, after the library's normalisation (accents, case, "&"/"and", punctuation,
    leading "The"). A reading shorter than MIN_NAME_KEY characters never counts. A name that
    does not contain the reading says nothing: folder names are often just dates."""
    from app.utils.reader.library import norm_key
    r = norm_key(reading)
    if len(r) < MIN_NAME_KEY or not any(c.isalpha() for c in r):
        return False
    f = norm_key(folder_name)
    return f" {r} " in f" {f} "


_MONTH = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_LEAD_STOP = re.compile(
    rf"\s+-\s+|\s+[\u2013\u2014]\s+|\s*;\s*|\s*\(|"
    rf"(?<![\w])(?:{_MONTH})\.?(?=\s+\d|\s*$|\s*,)|"       # a month name beside a day or year
    rf"(?<!\d)(?:19|20)\d{{2}}(?!\d)|"                          # a year
    rf"(?<!\d)\d{{1,2}}[/.\-]\d{{1,2}}[/.\-]\d{{2,4}}(?!\d)|"  # 05-08-77, 5/8/1977
    rf"(?<!\d)\d{{2}}-\d{{2}}-\d{{2}}(?!\d)", re.I)


def folder_lead(name):
    """The text a free-form folder name opens with, before the first date token or separator
    (" - ", ";", an en/em dash, a parenthesis) -- the artist, when the name starts with one:

        "Ella Fitzgerald & Joe Pass - 1984-05-03 - Teatro Tenda"  -> "Ella Fitzgerald & Joe Pass"
        "Boxcars July 25, 2014 Bicentennial Park Pavilion"        -> "Boxcars"
        "Herbie Hancock 1984-09-10 Blossom Music Center"          -> "Herbie Hancock"

    A trailing location with no date or separator before it stays in the lead
    ("Miles Davis Hempstead NY 1975 ..." -> "Miles Davis Hempstead NY"). None when nothing
    is left before the first stop."""
    t = (name or "").strip()
    m = _LEAD_STOP.search(t)
    lead = (t[:m.start()] if m else t).strip(" \t-,.")
    return lead or None


GROUP_WORDS = {"band", "trio", "quartet", "quintet", "sextet", "septet", "octet", "orchestra",
               "ensemble", "group", "unit", "project", "collective", "jazztet", "friends",
               "family", "brothers", "sisters", "boys", "players", "revue", "express", "allstars"}
