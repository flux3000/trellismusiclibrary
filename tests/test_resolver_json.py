"""
tests/test_resolver_json.py -- recording.resolver_json: the ingest resolver's
output saved at ingest time, served to the owner's View Recording page (the
Resolver tab) and never to peers.
"""
import json

from app.extensions import db as _db
from app.models.recording import Recording
from app.models.user import User

from tests.test_studio_sharing import _recording, _peer_granted_junction, _auth

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC


def _flac_with_tags(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    audio.save()


_INFO = '''Pat Metheny Group
June 14, 1979
Stars - Philadelphia, Pennsylvania, USA
Soundboard Recording
'''


def _login_as(client, username="admin"):
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None, f"no such user: {username}"
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return user


def _date_conflict_show(tmp_path):
    show = tmp_path / "import" / "Pat Metheny Group - 1979-06-14 - Stars - Philadelphia, PA (SBD)"
    for i in range(1, 3):
        _flac_with_tags(show / f"{i:02d}.flac", ARTIST="Pat Metheny Group", DATE="1979")
    (show / "info.txt").write_text(_INFO.replace("1979", "1980"))
    return show


def _conflict_dict():
    return {
        "date": {"value": {"year": 1979, "month": None, "day": None}, "source": "tags",
                 "candidates": {"tags": {"year": 1979, "month": None, "day": None},
                                "info": {"year": 1980, "month": 6, "day": 14}},
                 "conflict": True},
        "kind": "live", "duplicates": [], "status": "review",
        "reasons": ["conflict:date"],
        "tracks": [{"filename": "01.flac"}],
    }


def test_auto_confirm_stores_resolver_json_with_the_date_conflict(app, tmp_path, monkeypatch):
    from app.api import ingest
    library_root = tmp_path / "library"
    library_root.mkdir()
    app.config["LIBRARY_ROOT"] = str(library_root)
    show = _date_conflict_show(tmp_path)
    # A real conflict makes verdict() answer "review", so auto_confirm would
    # never ingest it; force the verdict so the storage path is exercised.
    monkeypatch.setattr(ingest, "verdict", lambda r: ("ingested", ["conflict:date"]))
    uid = _db.session.query(User).first().id

    out = ingest.auto_confirm(str(show), uid)
    assert out["status"] == "ingested", out
    rec = _db.session.get(Recording, out["result"]["recording_id"])
    stored = json.loads(rec.resolver_json)
    assert stored["date"]["conflict"] is True
    assert stored["date"]["candidates"]["tags"]["year"] == 1979
    assert stored["date"]["candidates"]["info"]["year"] == 1980
    assert stored["status"] == "ingested"
    assert stored["reasons"] == ["conflict:date"]
    assert "tracks" not in stored


def test_do_confirm_stores_posted_resolver_result_without_tracks(app, db, tmp_path):
    from app.api.ingest import _do_confirm
    src = tmp_path / "src"; src.mkdir()
    (src / "t01.flac").write_bytes(b"x" * 2000)
    lib = tmp_path / "lib"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id
    result = _do_confirm({
        "source_folder_path": str(src), "artist_name": "Resolver Act",
        "start_year": 1979, "source": "AUD",
        "tracks": [{"track_number": 1, "title": "One", "duration": 100, "filename": "t01.flac"}],
        "resolver_result": _conflict_dict(),
    }, uid, None)
    rec = _db.session.get(Recording, result["recording_id"])
    stored = json.loads(rec.resolver_json)
    assert stored["date"]["conflict"] is True
    assert "tracks" not in stored


def test_recording_detail_returns_resolver_json_and_null_for_legacy(app):
    rec = _recording("Resolver detail")
    client = app.test_client()
    _login_as(client)
    assert client.get(f"/api/recordings/{rec.id}").get_json()["resolver_json"] is None

    rec.resolver_json = json.dumps({"date": {"conflict": True}})
    _db.session.commit()
    body = client.get(f"/api/recordings/{rec.id}").get_json()
    assert body["resolver_json"]["date"]["conflict"] is True


def test_peers_do_not_receive_resolver_json(app):
    rec = _recording("Peer check")
    rec.resolver_json = json.dumps({"date": {"conflict": True, "marker": "zzq-marker"}})
    _db.session.commit()
    _col, _peer, raw = _peer_granted_junction(rec)
    res = app.test_client().get(f"/api/share/recordings/{rec.id}", headers=_auth(raw))
    assert res.status_code == 200
    assert "resolver_json" not in res.get_json()
    assert "zzq-marker" not in res.get_data(as_text=True)
