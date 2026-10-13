"""app/utils/top_shelf.py -- what goes on the Top Shelf (Ryan, 2026-10-13).

The viewer chooses the criteria in Settings; a recording that meets any of
them is in one shuffled pool. Stored per user in user_preference under
"top_shelf" as JSON. Live, published recordings only: the Top Shelf never
shows Albums.

  top_grades   A+ or A                     (default on)
  sbd, fm      SBD / FM sourced            (default on, so a brand-new,
                                            ungraded library still has a shelf)
  favorites    the viewer's starred shows  (default off)
  collections  every recording in the chosen collections (default off)

SBD and FM are the "randomly selected" criteria: a recording that is on the
shelf only through them, and was played in the last RECENT_DAYS, goes to the
back of the draw rather than out of it.
"""
import json
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.utils.prefs import get_pref, set_pref

PREF_KEY = "top_shelf"
RECENT_DAYS = 90
DEFAULTS = {"top_grades": True, "sbd": True, "fm": True,
            "favorites": False, "collections": False, "collection_ids": []}
_BOOLS = ("top_grades", "sbd", "fm", "favorites", "collections")


def _clean(data):
    out = dict(DEFAULTS)
    for k in _BOOLS:
        if isinstance(data.get(k), bool):
            out[k] = data[k]
    ids = data.get("collection_ids")
    if isinstance(ids, list):
        out["collection_ids"] = sorted({int(i) for i in ids
                                        if isinstance(i, int) or (isinstance(i, str) and i.isdigit())})
    return out


def get_settings(user_id):
    try:
        raw = json.loads(get_pref(user_id, PREF_KEY) or "{}")
    except ValueError:
        raw = {}
    return _clean(raw if isinstance(raw, dict) else {})


def set_settings(user_id, data):
    merged = {**get_settings(user_id), **(data or {})}
    clean = _clean(merged)
    set_pref(user_id, PREF_KEY, json.dumps(clean))
    return clean


def _source_is(rec, value):
    return (rec.source or "").strip().upper() == value


def pool(user_id, query, for_peer=False):
    """(recordings, demotable_ids). `query` is the eager-loaded Recording query
    to draw from (the caller adds card options).

    for_peer: the host's settings drawn for someone viewing the library
    (Ryan, 2026-10-13), Favorites included. Nothing is demoted: the host's
    listening is no guide to what the peer has heard."""
    from app.models.recording import Recording
    from app.models.collection import Collection

    s = get_settings(user_id)
    coll_ids = set()
    if s["collections"] and s["collection_ids"]:
        for c in db.session.query(Collection).filter(Collection.id.in_(s["collection_ids"])):
            coll_ids.update(r.id for r in c.recordings)

    conds = []
    if s["top_grades"]:
        conds.append(Recording.quality.in_(("A", "A+")))
    if s["sbd"] or s["fm"]:
        wanted = [v for v, on in (("SBD", s["sbd"]), ("FM", s["fm"])) if on]
        conds.append(db.func.upper(db.func.trim(Recording.source)).in_(wanted))
    if s["favorites"]:
        conds.append(Recording.is_favorite.is_(True))
    if coll_ids:
        conds.append(Recording.id.in_(coll_ids))
    if not conds:
        return [], set()

    recs = (query.filter(Recording.kind == "live", Recording.is_published.is_(True),
                         db.or_(*conds)).all())

    def via_other(r):
        return ((s["top_grades"] and r.quality in ("A", "A+"))
                or (s["favorites"] and r.is_favorite)
                or r.id in coll_ids)

    if for_peer:
        return recs, set()
    random_only = {r.id for r in recs if not via_other(r)}
    return recs, random_only & recently_played(user_id, random_only)


def recently_played(user_id, recording_ids):
    """The ids among `recording_ids` this user played in the last RECENT_DAYS."""
    if not recording_ids:
        return set()
    from app.models.play_log import PlayLog
    from app.models.track import Track
    since = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
    rows = (db.session.query(Track.recording_id)
            .join(PlayLog, PlayLog.track_id == Track.id)
            .filter(Track.recording_id.in_(recording_ids), PlayLog.user_id == user_id,
                    PlayLog.played_at >= since.replace(tzinfo=None))
            .distinct().all())
    return {rid for (rid,) in rows}
