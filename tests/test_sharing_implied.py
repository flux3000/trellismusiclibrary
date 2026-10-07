"""
tests/test_sharing_implied.py: sharing is implied (Ryan, 2026-10-06).

The per-peer "Share my library" switch is gone, so every unrevoked peer must hold
an active grant to the Full Library collection: on creation, on a minted invite,
and, for peers made before the change, through the startup upgrade.
"""
from app.extensions import db as _db
from app.models.collection import Collection, SYSTEM_FULL_LIBRARY
from app.models.peer import Peer, CollectionGrant
from app.utils.schema_upgrades import ensure_full_library_grants
from tests.test_peer_sharing import _login_as


def _full_library():
    col = Collection(name="Full Library", system_key=SYSTEM_FULL_LIBRARY)
    _db.session.add(col)
    _db.session.commit()
    return col


def _active_grants(peer_id, col_id):
    return _db.session.query(CollectionGrant).filter_by(
        peer_id=peer_id, collection_id=col_id, revoked_at=None).count()


def test_creating_a_peer_grants_the_full_library(app):
    col = _full_library()
    client = app.test_client()
    _login_as(client)
    pid = client.post("/api/peers/", json={"name": "Matt"}).get_json()["id"]
    assert _active_grants(pid, col.id) == 1


def test_minting_an_invite_restores_a_missing_grant_once(app):
    col = _full_library()
    peer = Peer(name="Old Peer")
    _db.session.add(peer)
    _db.session.commit()
    client = app.test_client()
    _login_as(client)
    assert client.post(f"/api/peers/{peer.id}/invites", json={}).status_code == 201
    assert client.post(f"/api/peers/{peer.id}/invites", json={}).status_code == 201
    assert _active_grants(peer.id, col.id) == 1


def test_startup_upgrade_grants_unrevoked_peers_only(app):
    from datetime import datetime, timezone
    col = _full_library()
    live = Peer(name="Live")
    gone = Peer(name="Gone", revoked_at=datetime.now(timezone.utc))
    _db.session.add_all([live, gone])
    _db.session.commit()
    assert ensure_full_library_grants(_db.engine) == 1
    _db.session.expire_all()
    assert _active_grants(live.id, col.id) == 1
    assert _active_grants(gone.id, col.id) == 0
    assert ensure_full_library_grants(_db.engine) is None


def test_invite_stays_copyable_and_a_new_one_replaces_it(app):
    from app.models.peer import PeerInvite
    _full_library()
    client = app.test_client()
    _login_as(client)
    pid = client.post("/api/peers/", json={"name": "Matt"}).get_json()["id"]
    first = client.post(f"/api/peers/{pid}/invites", json={}).get_json()["code"]
    detail = client.get(f"/api/peers/{pid}").get_json()
    assert detail["current_invite"]["invite"].endswith(first)
    second = client.post(f"/api/peers/{pid}/invites", json={}).get_json()["code"]
    detail = client.get(f"/api/peers/{pid}").get_json()
    assert detail["current_invite"]["invite"].endswith(second)
    live = [i for i in _db.session.query(PeerInvite).filter_by(peer_id=pid) if i.is_valid()]
    assert len(live) == 1 and live[0].code == second
