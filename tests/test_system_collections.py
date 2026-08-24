"""
tests/test_system_collections.py — the "Full Library" system collection.

Whole-library sharing is a Collection whose membership is resolved by QUERY
rather than by `collection_recording` rows, so that peer sharing keeps exactly
one primitive (the Collection grant) and one authorization path.

WHAT THIS FILE IS DEFENDING

1. **The junction trap.** Three places used to answer "what is in this
   collection?" by reading `collection_recording` directly. A dynamic
   collection has ZERO junction rows, so any surviving junction read reports it
   as EMPTY — and an empty library is indistinguishable from a broken share,
   which is this project's most-repeated failure mode.

2. **The unpublished case, which cannot be caught by inspection.** Full Library
   is every PUBLISHED recording. Every recording in the real database is
   currently published, so a mistake here looks perfectly correct until the
   first show is moved out to Workshop and keeps streaming. It is only ever
   visible to a test that makes an unpublished row on purpose.

3. **The two authorization paths still agreeing.** `peer_can_access_recording_id`
   and `peer_visible_recording_ids` are separate implementations, taught about
   system collections separately and deliberately. The equivalence assertion is
   re-run here in a system-collection world.
"""

import pytest

from app.extensions import db as _db
from app.models.collection import Collection, CollectionRecording, SYSTEM_FULL_LIBRARY
from app.models.peer import Peer, CollectionGrant, PeerToken
from app.models.performance import Performance
from app.models.performer import Performer
from app.models.recording import Recording
from app.models.user import User
from app.utils.peer_access import (
    peer_can_access_recording_id,
    peer_visible_recording_ids,
)
from app.utils.peer_auth import generate_token, hash_secret


# ── World building ────────────────────────────────────────────────────────────

def _recording(label, published=True, favorite=False):
    performer = Performer(name=f"{label} Band")
    _db.session.add(performer)
    _db.session.flush()

    perf = Performance(performer_id=performer.id, start_year=1975,
                       start_month=6, start_day=1)
    _db.session.add(perf)
    _db.session.flush()

    rec = Recording(performance_id=perf.id, source="SBD", is_complete=True,
                    is_official=False, folder_path=f"{label}/1975",
                    is_published=published, is_favorite=favorite)
    _db.session.add(rec)
    _db.session.commit()
    return rec


def _full_library():
    col = Collection(name="Full Library", system_key=SYSTEM_FULL_LIBRARY)
    _db.session.add(col)
    _db.session.commit()
    return col


def _peer_granted(col, name="Matt"):
    peer = Peer(name=name)
    _db.session.add(peer)
    _db.session.flush()
    _db.session.add(CollectionGrant(peer_id=peer.id, collection_id=col.id))
    raw = generate_token()
    _db.session.add(PeerToken(peer_id=peer.id, token_hash=hash_secret(raw)))
    _db.session.commit()
    return peer, raw


def _login_admin(client, username="admin"):
    """Session login that PROVES it authenticated — see the long note in
    test_peer_sharing._login_as. A silent login failure here would read as a
    passing guard test."""
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None, f"no such user to log in as: {username!r}"
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    probe = client.get("/api/collections/")
    assert probe.status_code == 200, (
        f"login did not take: /api/collections/ returned {probe.status_code}")


@pytest.fixture()
def world(app):
    published_a = _recording("Alpha")
    published_b = _recording("Bravo")
    shelved = _recording("Shelved", published=False)
    col = _full_library()
    peer, raw = _peer_granted(col)
    return {"col": col, "peer": peer, "token": raw,
            "published": {published_a.id, published_b.id},
            "shelved": shelved.id}


# ── Membership resolution — the junction trap ─────────────────────────────────

def test_system_collection_is_not_empty(world):
    """The headline failure mode. A junction read returns [] here."""
    col = world["col"]
    assert col.is_system is True
    assert col.resolved_recording_ids(), "system collection resolved to nothing"
    assert col.recording_count > 0
    assert len(col.recordings) == col.recording_count


def test_recording_count_does_not_read_junction_rows(world):
    """`len(c.recording_links)` is 0 for a dynamic collection — the sidebar
    number and the peer's collection list both used to be computed that way."""
    col = world["col"]
    assert len(col.recording_links) == 0
    assert col.recording_count == len(col.resolved_recording_ids())


def test_ordinary_collection_still_resolves_from_junction(app):
    rec = _recording("Curated")
    col = Collection(name="Curated Box")
    _db.session.add(col)
    _db.session.flush()
    _db.session.add(CollectionRecording(collection_id=col.id,
                                        recording_id=rec.id, order=0))
    _db.session.commit()

    assert col.is_system is False
    assert col.resolved_recording_ids() == {rec.id}
    assert col.recording_count == 1


# ── The unpublished case — invisible without a test ───────────────────────────

def test_unpublished_recording_is_not_in_full_library(world):
    """A show moved out to Workshop/Backlog must drop out of the shared set.

    ⚠ This is the assertion that cannot be replaced by looking at the app: every
    recording in the real database is published, so the bug this catches would
    stay hidden until the first Move — at which point a show whose folder has
    left LIBRARY_ROOT would still be offered to peers, and would browse but not
    play.
    """
    assert world["shelved"] not in world["col"].resolved_recording_ids()
    assert world["col"].resolved_recording_ids() >= world["published"]


def test_peer_cannot_reach_an_unpublished_recording(world):
    peer = world["peer"]
    assert peer_can_access_recording_id(peer, world["shelved"]) is False
    assert world["shelved"] not in peer_visible_recording_ids(peer)


def test_unpublishing_removes_it_from_the_peer_world(world):
    """Membership is live, not a snapshot taken when the grant was made."""
    peer = world["peer"]
    rid = next(iter(world["published"]))
    assert rid in peer_visible_recording_ids(peer)

    _db.session.get(Recording, rid).is_published = False
    _db.session.commit()
    _db.session.expire_all()

    assert rid not in world["col"].resolved_recording_ids()


# ── The dynamic promise ───────────────────────────────────────────────────────

def test_new_recording_appears_without_touching_the_grant(world):
    """The whole point: ingest a show, peers see it, nobody re-shares anything."""
    peer = world["peer"]
    before = peer_visible_recording_ids(peer)
    fresh = _recording("Latecomer")
    _db.session.expire_all()

    assert fresh.id not in before
    assert fresh.id in world["col"].resolved_recording_ids()


# ── Both authorization paths still agree ──────────────────────────────────────

def test_both_access_paths_agree_over_a_system_collection(world):
    """`peer_can_access_recording_id` (milestone 1, streaming) and
    `peer_visible_recording_ids` (milestone 2, browse) are separate
    implementations taught about system collections separately. Disagreement
    means a peer either sees something unplayable or plays something unlisted.

    Asserted over EVERY recording in the database, including the unpublished one.
    """
    peer = world["peer"]
    visible = peer_visible_recording_ids(peer)
    all_ids = [r.id for r in _db.session.query(Recording).all()]
    assert len(all_ids) >= 3
    for rid in all_ids:
        assert peer_can_access_recording_id(peer, rid) == (rid in visible), (
            f"paths disagree on recording {rid}")


def test_revoking_a_full_library_grant_empties_the_world(world):
    from datetime import datetime, timezone
    peer = world["peer"]
    assert peer_visible_recording_ids(peer)

    for grant in peer.grants:
        grant.revoked_at = datetime.now(timezone.utc)
    _db.session.commit()
    _db.session.expire_all()

    assert peer_visible_recording_ids(peer) == set()


# ── Fail closed ───────────────────────────────────────────────────────────────

def test_unknown_system_key_raises_rather_than_sharing_everything(app):
    """An unrecognised key must not degrade to 'everything'. If a future key is
    added to the model without a branch here, this is what says so."""
    col = Collection(name="Mystery", system_key="not_a_real_key")
    _db.session.add(col)
    _db.session.commit()

    with pytest.raises(ValueError):
        col.resolved_recording_ids()


# ── Peer-facing surface ───────────────────────────────────────────────────────

def test_system_collection_is_hidden_from_the_peer_collection_list(world, app):
    """A peer browsing the whole library should not also see a collection whose
    contents are exactly that library."""
    c = app.test_client()
    res = c.get("/api/share/collections",
                headers={"Authorization": f"Bearer {world['token']}"})
    assert res.status_code == 200
    assert res.get_json() == []


def test_peer_can_still_stream_through_a_full_library_grant(world, app):
    """Hiding it from the list must not have hidden the access it confers."""
    peer = world["peer"]
    for rid in world["published"]:
        assert peer_can_access_recording_id(peer, rid) is True


# ── Owner-side guards ─────────────────────────────────────────────────────────

def test_system_collection_cannot_be_deleted(world, app):
    """Deleting it would revoke every Streamer at once, silently."""
    c = app.test_client()
    _login_admin(c)
    res = c.delete(f"/api/collections/{world['col'].id}")
    assert res.status_code == 409
    assert _db.session.get(Collection, world["col"].id) is not None


def test_system_collection_membership_cannot_be_hand_edited(world, app):
    c = app.test_client()
    _login_admin(c)
    rid = next(iter(world["published"]))

    add = c.post(f"/api/collections/{world['col'].id}/recordings",
                 json={"recording_id": rid})
    assert add.status_code == 409

    remove = c.delete(f"/api/collections/{world['col'].id}/recordings/{rid}")
    assert remove.status_code == 409


def test_system_collection_can_still_be_renamed(world, app):
    """The label is cosmetic — only membership and deletion are protected."""
    c = app.test_client()
    _login_admin(c)
    res = c.put(f"/api/collections/{world['col'].id}",
                json={"name": "Everything I Have"})
    assert res.status_code == 200
    _db.session.expire_all()
    assert _db.session.get(Collection, world["col"].id).name == "Everything I Have"


def test_owner_collection_list_reports_the_dynamic_count(world, app):
    c = app.test_client()
    _login_admin(c)
    res = c.get("/api/collections/")
    assert res.status_code == 200
    row = next(r for r in res.get_json() if r["id"] == world["col"].id)
    assert row["is_system"] is True
    assert row["system_key"] == SYSTEM_FULL_LIBRARY
    assert row["recording_count"] == world["col"].recording_count > 0


# ── The owner's star must not travel ──────────────────────────────────────────

def test_owner_favorites_do_not_reach_a_peer(app):
    """`is_favorite` means "the VIEWER starred this" everywhere in the UI.

    Passing the OWNER's value through would scatter Ryan's private bookmarks
    across Matt's screen looking, to Matt, like his own — and it would do it
    through the recording payload even with the Favorites nav section removed,
    which is exactly how this nearly shipped (2026-08-24).

    Asserted across every share endpoint that serialises a recording, because
    the leak is per-serialiser, not per-page.
    """
    starred = _recording("Starred", favorite=True)
    plain = _recording("Plain")
    col = _full_library()

    # A curated collection too, so the collection-detail path is covered.
    curated = Collection(name="Curated")
    _db.session.add(curated)
    _db.session.flush()
    _db.session.add(CollectionRecording(collection_id=curated.id,
                                        recording_id=starred.id, order=0))
    _db.session.commit()

    peer, raw = _peer_granted(col)
    c = app.test_client()
    h = {"Authorization": f"Bearer {raw}"}

    assert starred.is_favorite is True, "fixture must actually be starred"

    def stars(payload):
        """Every is_favorite value anywhere in a nested payload."""
        found = []
        def walk(o):
            if isinstance(o, dict):
                if "is_favorite" in o:
                    found.append(o["is_favorite"])
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(payload)
        return found

    for path in [
        f"/api/share/recordings/{starred.id}",
        "/api/share/recordings/recent?card=1",
        "/api/share/recordings/recent",
        f"/api/share/collections/{curated.id}",
        "/api/share/performers/all-recordings",
    ]:
        res = c.get(path, headers=h)
        assert res.status_code == 200, f"{path} returned {res.status_code}"
        values = stars(res.get_json())
        assert all(v is False for v in values), (
            f"{path} leaked the owner's star: {values}")


def test_share_door_has_no_favorites_endpoint(app):
    """Removed deliberately rather than left dormant (2026-08-24).

    An unused endpoint that exposes owner-side data is precisely what gets
    wired up later by someone who does not know why it existed.
    """
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert not any("share" in r and "favorites" in r for r in rules), (
        "a favorites route reappeared on the share door")
