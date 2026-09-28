"""
tests/test_share_query_counts.py — query-count regression for the peer-facing
GET /api/share/artists/all-recordings route (the peer Library payload).

Before the fix, this route ran one query per artist for performances and then
lazy-loaded each performance's venue and recordings, and each recording's
tracks / quality_score, one at a time -- a query count that grows linearly
with the number of performances/recordings in the visible set. The fix
eager-loads everything the loop and recording_summary() touch with
selectinload chains off one Performance query, so the total is a small
constant regardless of N.
"""

from sqlalchemy import event

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.collection import Collection, CollectionRecording
from app.models.peer import Peer, CollectionGrant, PeerToken
from app.utils.peer_auth import generate_token, hash_secret


def _auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def _count_queries(fn):
    n = [0]

    def _tick(conn, cursor, statement, *a, **k):
        n[0] += 1
    event.listen(_db.engine, "before_cursor_execute", _tick)
    try:
        result = fn()
    finally:
        event.remove(_db.engine, "before_cursor_execute", _tick)
    return result, n[0]


def _seed_peer_library(n):
    """n performances, each its own artist, each with one recording, all
    granted to one peer through one collection. One artist per performance
    (rather than one artist for all of them) so the count exercises the
    per-artist loop too, not just the per-performance/recording one."""
    recs = []
    for i in range(n):
        artist = Artist(name=f"Query Band {i}")
        _db.session.add(artist)
        _db.session.flush()
        perf = Performance(artist_id=artist.id, start_year=2000, start_month=1,
                            start_day=(i % 28) + 1)
        _db.session.add(perf)
        _db.session.flush()
        rec = Recording(performance_id=perf.id, source="SBD", is_complete=True,
                        is_official=False, folder_path=f"QueryBand/{i}",
                        is_published=True, kind="live")
        _db.session.add(rec)
        recs.append(rec)
    _db.session.commit()

    col = Collection(name="Everything")
    _db.session.add(col)
    _db.session.flush()
    for rec in recs:
        _db.session.add(CollectionRecording(collection_id=col.id, recording_id=rec.id, order=0))
    peer = Peer(name="Matt")
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()
    return raw, recs


def _run(app, n):
    raw, recs = _seed_peer_library(n)
    client = app.test_client()
    resp, n_queries = _count_queries(
        lambda: client.get("/api/share/artists/all-recordings", headers=_auth(raw)))
    assert resp.status_code == 200
    body = resp.get_json()
    total_recs = sum(len(a["performances"][0]["recordings"]) for a in body)
    assert total_recs == n
    return n_queries


# Before this fix, this route ran roughly 5 queries per performance (a
# Performance query per artist, a Performance.recordings lazy-load, a
# Performance.venue lazy-load, and a Recording.tracks / Recording.
# quality_score lazy-load per recording via recording_summary()) -- about
# 200 queries for 40 performances, ~400 for 80, scaling linearly with N.
#
# After eager-loading everything with selectinload chains off one Performance
# query (this file's fix), the remaining count is a FIXED ~17 regardless of
# N: token/peer auth (5, incl. the post-commit attribute refresh
# resolve_peer_token's own commit forces -- unrelated to this bug, and
# peer_auth.py is out of scope here), the peer-visibility chain
# (6, now memoized per peer_access.py's existing g-cache pattern -- extended
# here to peer_visible_performance_ids/peer_visible_artist_ids, which is
# where a redundant duplicate query was coming from), the artist query (1),
# the performance query plus its three selectinload chains (5), and the
# batched image-url lookup (1). None of that scales with the size of the
# visible library -- the assertion below is that it stays FIXED, not that it
# hits an arbitrary small number that would require touching the shared,
# security-sensitive token-resolution path in peer_auth.py to shave further.
_FIXED_QUERY_CEILING = 20


def test_share_all_recordings_query_count_is_fixed_not_linear(app):
    """40 performances (each its own artist, one recording each): the total
    query count for the whole route must be a small fixed constant, not the
    ~200 the old per-performance/per-recording lazy-loads produced at this N."""
    n_40 = _run(app, 40)
    assert n_40 < _FIXED_QUERY_CEILING, (
        f"{n_40} queries for 40 performances -- expected a fixed small count")


def test_share_all_recordings_query_count_does_not_grow_with_n(app):
    """Doubling to 80 performances must NOT roughly double the query count
    the way the old per-performance/per-recording lazy-loads would have --
    it must stay under the same fixed ceiling as the 40 case."""
    n_80 = _run(app, 80)
    assert n_80 < _FIXED_QUERY_CEILING, (
        f"{n_80} queries for 80 performances -- query count is growing with N")
