"""
app/utils/aliases.py -- learned aliases (Resolver v2, chunk 8).

When a person saves a recording whose venue or artist differs from what the
resolver read, the text the resolver quoted for that field becomes an alias of the
row they saved (models/alias.py), and the library index reads it from then on
(reader/library.py). Written from _do_confirm only, and only for a save a person
made: an unattended auto-ingest has nothing to learn from.

No alias is written when
  * the save is unattended (data["unattended"]);
  * the field equals the resolver's top candidate (nothing was corrected);
  * the resolver quoted no info text for the field;
  * the saved value is one an AI proposal offered and the person did not mark as
    accepted (data["ai_accepted"] lists the fields they did);
  * the alias is already stored for that row (re-saving is a no-op);
  * the quoted text is the exact name of a different existing row, of either kind
    (that row outranks any alias, so the alias would never be read), or already an
    alias of a different row of the same kind;
  * the quoted text is too generic to name one thing: under five characters, only
    generic venue or show words ("Theatre", "Park", "Live"), a date, a place
    ("Boston, MA", a state, a city), or text with a date or a track or runtime in it.

Events have no alias table (a data-model decision, not made here).
"""
import re

from app.extensions import db
from app.utils.reader.library import current_library, invalidate_library_cache, norm_key

_MAX_ALIAS = 255
_MIN_KEY = 5

# Words that say what kind of thing a line is, not which one.
_GENERIC_WORDS = {
    "live", "concert", "show", "set", "sets", "soundboard", "aud", "audience", "sbd", "fm",
    "matrix", "unknown", "tbd", "tour", "recording", "night", "early", "late", "first",
    "second", "encore", "disc", "cd", "part", "vol", "volume", "the", "and", "at", "in", "of",
    "a", "an", "for", "with", "from", "presents", "plus", "special", "guest", "guests",
}
_RUNTIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b|\(\s*\d+[:.]\d+\s*\)")
_TRACK_RE = re.compile(r"^\s*(?:d\d+\s*t\d+\b|(?:d\d+\s*)?(?:t|track\s*)?\d{1,3}\s*[.)\-:]\s*\S)", re.I)
_CITY_ST_RE = re.compile(r",\s*[A-Za-z]{2,3}\.?\s*$")


def _place_tokens():
    """Lower-case names and codes of US states, Canadian provinces and Australian states."""
    from app.utils.ingest import _US_STATES
    out = {k.lower() for k in _US_STATES}
    out |= {v["name"].lower() for v in _US_STATES.values()}
    out |= {"ontario", "quebec", "british columbia", "alberta", "manitoba", "saskatchewan",
            "nova scotia", "new brunswick", "newfoundland", "on", "qc", "bc", "ab", "mb", "sk",
            "ns", "nb", "nl", "pe", "nsw", "vic", "qld", "tas", "act", "nt", "new south wales",
            "victoria", "queensland", "tasmania", "western australia", "south australia",
            "usa", "us", "uk", "canada", "australia", "england", "scotland", "wales", "ireland"}
    return out


def _too_generic(quote):
    """True when `quote` cannot name one venue or act."""
    from app.utils.reader import place
    from app.utils.reader.dates import find_dates
    from app.utils.reader.features import VENUE_WORDS
    key = norm_key(quote)
    if len(key) < _MIN_KEY:
        return True
    words = re.findall(r"[^\W\d_]+", key)
    if not words or all(w in VENUE_WORDS or w in _GENERIC_WORDS for w in words):
        return True
    if find_dates(quote) or _RUNTIME_RE.search(quote) or _TRACK_RE.match(quote):
        return True
    if key in _place_tokens() or _CITY_ST_RE.search(quote.strip()):
        return True
    res = place.peel(quote.strip(), city_only=True)
    if res.recognised and (res.span[1] - res.span[0]) >= 0.8 * len(quote.strip()):
        return True
    return False


def _reading(resolver_result, field):
    """(top candidate value, quoted info text) the resolver gave `field`, from a
    Resolved.to_dict() or the scan's serialised `resolved` dict. Either may be None."""
    f = resolver_result.get(field) if isinstance(resolver_result, dict) else None
    if not isinstance(f, dict):
        return None, None
    quote = None
    for row in f.get("evidence") or []:
        if isinstance(row, dict) and row.get("source") == "info" and (row.get("text") or "").strip():
            quote = row["text"].strip()
            break
    return f.get("value"), quote


def _ai_offered(data, field, saved_key):
    """True when an AI proposal for `field` proposes the saved value and the person did
    not mark it accepted."""
    if field in (data.get("ai_accepted") or []):
        return False
    ai = data.get("ai_result")
    for p in (ai.get("proposals") if isinstance(ai, dict) else None) or []:
        if (isinstance(p, dict) and p.get("field") == field
                and norm_key(p.get("proposed")) == saved_key):
            return True
    return False


def _learn_one(model, fk, row, field, data, exact_ids, other_exact_ids):
    value, quote = _reading(data.get("resolver_result"), field)
    if not value or not quote or len(quote) > _MAX_ALIAS:
        return None
    saved_key = norm_key(row.name)
    akey = norm_key(quote)
    if not akey or akey == saved_key or norm_key(value) == saved_key:
        return None
    if _too_generic(quote):
        return None
    if _ai_offered(data, field, saved_key):
        return None
    if any(i != row.id for i in exact_ids(akey)) or other_exact_ids(akey):
        return None
    if (db.session.query(model.id)
            .filter(getattr(model, fk) != row.id, model.alias_key == akey).first()):
        return None
    if (db.session.query(model.id)
            .filter(getattr(model, fk) == row.id, model.alias_key == akey).first()):
        return None
    try:
        with db.session.begin_nested():
            db.session.add(model(**{fk: row.id}, alias=quote, alias_key=akey))
    except Exception:  # noqa: BLE001 -- a race on the unique constraint is the same no-op
        return None
    return quote


def learn_aliases(data, artist, venue):
    """Write the aliases a person's save teaches. Returns [(kind, alias text)] written.
    Never raises: learning is never worth failing a save for."""
    if data.get("unattended") or not isinstance(data.get("resolver_result"), dict):
        return []
    from app.models.alias import ArtistAlias, VenueAlias
    written = []
    try:
        ix = current_library()
        if artist is not None:
            a = _learn_one(ArtistAlias, "artist_id", artist, "artist", data, ix.exact_artist_ids,
                           ix.exact_venue_ids)
            if a:
                written.append(("artist", a))
        if venue is not None:
            v = _learn_one(VenueAlias, "venue_id", venue, "venue", data, ix.exact_venue_ids,
                           ix.exact_artist_ids)
            if v:
                written.append(("venue", v))
        if written:
            with db.session.begin_nested():
                db.session.flush()
            invalidate_library_cache()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return []
    return written


def confirmed_keys(data, artist, venue, event_name):
    """Normalised keys a person's save settles, for the importer's queue re-check: the
    saved name, the resolver's reading and the quoted text, per field."""
    rr = data.get("resolver_result") if isinstance(data.get("resolver_result"), dict) else {}
    out = {}
    for field, saved in (("artist", artist.name if artist is not None else None),
                         ("venue", venue.name if venue is not None else None),
                         ("event", event_name)):
        value, quote = _reading(rr, field)
        keys = {norm_key(x) for x in (saved, value, quote) if x}
        keys.discard("")
        if keys:
            out[field] = keys
    return out
