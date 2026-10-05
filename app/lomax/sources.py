"""
Trusted sources: a short shipped list of collector sources, plus the links the library
already holds for one act (its own resource rows and the MusicBrainz links worth reading).
They are listed to the model so it checks them before the open web.
"""
import json

from app.lomax.prompts import EvidenceSection

SHIPPED = (
    ("etreedb.org (formerly db.etree.org)", "https://etreedb.org", "etree show and setlist database"),
    ("Live Music Archive", "https://archive.org/details/etree", "archive.org etree collection, with taper info files"),
    ("setlist.fm", "https://www.setlist.fm", "setlists by artist and date"),
    ("Bluegrass Archive", "https://bluegrassarchive.com", "bluegrass shows and recordings"),
    ("gdarchive.net", "https://gdarchive.net", "Grateful Dead family recordings"),
    ("JerryBase", "https://jerrybase.com", "Jerry Garcia performances"),
    ("phish.net", "https://phish.net", "Phish setlists and show data"),
)

# MusicBrainz link labels (app/utils/musicbrainz.py _LINK_LABELS) that sell, stream or are social.
_SKIP_MB_LABELS = {"Streaming", "Buy", "Social", "Bandcamp", "SoundCloud", "YouTube", "Songkick"}


def artist_links(artist):
    """[{label, url, origin}] for one act: library resource rows first, then MusicBrainz links."""
    out, seen = [], set()
    if artist is None:
        return out
    for r in getattr(artist, "resources", None) or []:
        if r.url and r.url not in seen:
            seen.add(r.url)
            out.append({"label": r.label or r.url, "url": r.url, "origin": "library"})
    try:
        mb = json.loads(artist.mb_links_json) if artist.mb_links_json else {}
    except (ValueError, TypeError):
        mb = {}
    for label, url in (mb or {}).items():
        if label in _SKIP_MB_LABELS or not url or url in seen:
            continue
        seen.add(url)
        out.append({"label": label, "url": url, "origin": "musicbrainz"})
    return out


def trusted_section(artist=None):
    """The 'Trusted sources' evidence section: this act's own links, then the shipped list."""
    lines = []
    links = artist_links(artist)
    if links:
        lines.append("For this act:")
        lines += ["  %s: %s (%s)" % (l["label"], l["url"], l["origin"]) for l in links]
    lines.append("General collector sources:" if links else "Collector sources:")
    lines += ["  %s: %s (%s)" % s for s in SHIPPED]
    return EvidenceSection("trusted", lines)
