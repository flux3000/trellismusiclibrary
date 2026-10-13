"""Host Favorites (Ryan, 2026-10-13): a peer sees the host's starred,
published recordings it can see, as a list. The row's is_favorite stays the
viewer's own star (False on the wire)."""
from app.extensions import db as _db

from tests.test_studio_sharing import (
    _recording, _peer_granted_junction, _second_recording_same_artist, _auth,
)


def test_host_favorites_list_is_scoped_and_keeps_viewer_star(app):
    fav = _recording("HostFav")
    plain = _second_recording_same_artist(fav, "Plain", kind="live")
    hidden_fav = _recording("NotShared")
    for r in (fav, hidden_fav):
        r.is_favorite = True
    _db.session.commit()
    _col, _peer, raw = _peer_granted_junction(fav, also=(plain,))
    rows = app.test_client().get("/api/share/recordings/favorites",
                                 headers=_auth(raw)).get_json()
    assert [r["id"] for r in rows] == [fav.id]
    assert rows[0]["is_favorite"] is False
