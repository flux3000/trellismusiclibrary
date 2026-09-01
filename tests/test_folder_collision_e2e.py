"""
tests/test_folder_collision_e2e.py — collision protection driven through the
REAL API endpoints against a REAL library directory, not the helpers in
isolation.

Ryan's 2026-09-01 report: two "Various Artists" recordings at Ryman
Auditorium, 1964, both ending up with unlabeled Source — their files ended up
merged into one folder. tests/test_folder_naming.py proves
unique_folder_name() and rename_recording_folder() dedupe correctly on their
own; this file proves the ENDPOINTS actually reach that protection, since a
helper that works and a caller that skips it look identical from the outside.

Each test asserts the strong property, not just the folder name: no folder
ever ends up holding both recordings' audio.
"""

import os

import pytest

from app.extensions import db as _db
from app.models.user import User
from app.models.performer import Performer
from app.models.venue import Venue
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.track import Track


BASE = "Various Artists - 1964 - Ryman Auditorium - Nashville, TN"


def _login_as(client, username="admin"):
    from app.extensions import login_manager
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def ryman(app, tmp_path):
    """
    Two recordings of ONE performance — Various Artists / 1964 / Ryman — whose
    canonical folder names differ only by the Source segment, each with a real
    folder and a real audio file on disk. Clearing the second's Source makes
    the two names identical, which is the collision under test.
    """
    app.config["LIBRARY_ROOT"] = str(tmp_path)

    performer = Performer(name="Various Artists")
    _db.session.add(performer)
    venue = Venue(name="Ryman Auditorium", city="Nashville", state="TN", country="US")
    _db.session.add(venue)
    _db.session.flush()

    perf = Performance(performer_id=performer.id, venue_id=venue.id, start_year=1964)
    _db.session.add(perf)
    _db.session.flush()

    made = {}
    for key, source, folder in (("plain", None,  BASE),
                                ("aud",   "AUD", f"{BASE} (AUD)")):
        rel = f"Various Artists/{folder}"
        (tmp_path / rel).mkdir(parents=True)
        (tmp_path / rel / "01 - Song.flac").write_bytes(key.encode())
        rec = Recording(performance_id=perf.id, source=source, folder_path=rel)
        _db.session.add(rec)
        _db.session.flush()
        _db.session.add(Track(recording_id=rec.id, track_number=1,
                              title="Song", file_path="01 - Song.flac"))
        made[key] = rec
    _db.session.commit()

    return {"root": tmp_path, "perf_id": perf.id,
            "plain_id": made["plain"].id, "aud_id": made["aud"].id}


def _audio_in(folder):
    return sorted(p.name for p in folder.iterdir() if p.suffix == ".flac")


def test_clearing_source_to_collide_does_not_merge_folders(client, ryman):
    """THE reported case, through PUT /api/recordings/<id>: the AUD recording
    loses its Source, so its canonical name becomes the plain recording's
    folder name exactly. It must be given its own disambiguated folder."""
    _login_as(client)
    root = ryman["root"]

    resp = client.put(f"/api/recordings/{ryman['aud_id']}", json={"source": None})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert "folder_rename_error" not in resp.get_json(), resp.get_json()

    artist_dir = root / "Various Artists"
    assert (artist_dir / BASE).is_dir()
    assert (artist_dir / f"{BASE} (2)").is_dir()

    # The strong assertion: neither folder holds both recordings' audio, and
    # each still holds exactly its own one file.
    assert _audio_in(artist_dir / BASE) == ["01 - Song.flac"]
    assert _audio_in(artist_dir / f"{BASE} (2)") == ["01 - Song.flac"]
    assert (artist_dir / BASE / "01 - Song.flac").read_bytes() == b"plain"
    assert (artist_dir / f"{BASE} (2)" / "01 - Song.flac").read_bytes() == b"aud"

    rec = _db.session.get(Recording, ryman["aud_id"])
    assert rec.folder_path == f"Various Artists/{BASE} (2)"


def test_performance_edit_renames_both_recordings_without_collision(client, ryman):
    """The other rename door — PUT /api/performances/<id>. One performance can
    hold several recordings, so a date edit renames them in a loop; the second
    one through must see the first one's brand-new folder."""
    _login_as(client)
    root = ryman["root"]

    # Both recordings lose the distinguishing Source first, so the date edit
    # recomputes two IDENTICAL canonical names in one request.
    _db.session.get(Recording, ryman["aud_id"]).source = None
    _db.session.commit()

    resp = client.put(f"/api/performances/{ryman['perf_id']}",
                      json={"start_year": 1965})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    artist_dir = root / "Various Artists"
    new_base = "Various Artists - 1965 - Ryman Auditorium - Nashville, TN"
    folders = sorted(p.name for p in artist_dir.iterdir() if p.is_dir())
    assert folders == [new_base, f"{new_base} (2)"], folders

    contents = [_audio_in(artist_dir / f) for f in folders]
    assert contents == [["01 - Song.flac"], ["01 - Song.flac"]], contents
    payloads = sorted((artist_dir / f / "01 - Song.flac").read_bytes() for f in folders)
    assert payloads == [b"aud", b"plain"]


def test_repeated_saves_do_not_grow_more_suffixes(client, ryman):
    """A collided recording keeps ITS folder on every later save — the name
    must not walk (2) → (3) → (4) just because the plain name stays taken."""
    _login_as(client)
    artist_dir = ryman["root"] / "Various Artists"

    for _ in range(3):
        resp = client.put(f"/api/recordings/{ryman['aud_id']}",
                          json={"source": None, "notes": "touch"})
        assert resp.status_code == 200

    folders = sorted(p.name for p in artist_dir.iterdir() if p.is_dir())
    assert folders == [BASE, f"{BASE} (2)"], folders


# ── Track FILE name collisions (2026-09-01) ───────────────────────────────────
# The folder cases above are about directories, where os.rename() at least
# refuses a non-empty target. Files have no such mercy: os.rename() REPLACES
# the destination silently, so a retitle that lands on a sibling's filename
# used to DELETE that sibling's audio outright.

@pytest.fixture()
def two_tracks(app, tmp_path):
    """One recording, one folder, two tracks whose filenames differ only by
    title — the shape a retitle can collapse."""
    app.config["LIBRARY_ROOT"] = str(tmp_path)

    performer = Performer(name="Various Artists")
    _db.session.add(performer)
    venue = Venue(name="Ryman Auditorium", city="Nashville", state="TN", country="US")
    _db.session.add(venue)
    _db.session.flush()
    perf = Performance(performer_id=performer.id, venue_id=venue.id, start_year=1964)
    _db.session.add(perf)
    _db.session.flush()

    rel = f"Various Artists/{BASE}"
    (tmp_path / rel).mkdir(parents=True)
    rec = Recording(performance_id=perf.id, source=None, folder_path=rel)
    _db.session.add(rec)
    _db.session.flush()

    ids = {}
    for num, title, payload in ((1, "Show Intro", b"first"),
                                (1, "Something Else", b"second")):
        fname = f"{num:02d} - {title}.flac"
        (tmp_path / rel / fname).write_bytes(payload)
        tr = Track(recording_id=rec.id, track_number=num, title=title, file_path=fname)
        _db.session.add(tr)
        _db.session.flush()
        ids[title] = tr.id
    _db.session.commit()

    return {"root": tmp_path, "rel": rel, "ids": ids}


def test_retitling_a_track_never_overwrites_a_sibling_file(client, two_tracks):
    """Retitle track "Something Else" to "Show Intro" — both are track 01, so
    the computed filename is byte-identical to the other track's. The other
    track's audio must survive."""
    _login_as(client)
    folder = two_tracks["root"] / two_tracks["rel"]

    resp = client.put(f"/api/tracks/{two_tracks['ids']['Something Else']}",
                      json={"title": "Show Intro"})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    # The original file is untouched, contents and all.
    assert (folder / "01 - Show Intro.flac").read_bytes() == b"first"
    # The renamed one took a disambiguated name instead of clobbering it.
    assert (folder / "01 - Show Intro (2).flac").read_bytes() == b"second"
    assert resp.get_json()["file_path"] == "01 - Show Intro (2).flac"
    # Nothing was lost: still two audio files.
    assert len(_audio_in(folder)) == 2


def test_retitling_a_track_to_a_free_name_keeps_the_plain_name(client, two_tracks):
    """No collision, no suffix — the common case must stay clean."""
    _login_as(client)
    folder = two_tracks["root"] / two_tracks["rel"]

    resp = client.put(f"/api/tracks/{two_tracks['ids']['Something Else']}",
                      json={"title": "Wabash Blues"})
    assert resp.status_code == 200
    assert resp.get_json()["file_path"] == "01 - Wabash Blues.flac"
    assert (folder / "01 - Wabash Blues.flac").read_bytes() == b"second"


# ── Shared-folder guard (2026-09-01) ──────────────────────────────────────────
# The ingest merge bug left Ryan's library with eight recordings pointing at
# ONE folder (146 files, eight tracks numbered 01). Renaming that folder to
# match any one of them would move the other seven's audio and strand their
# folder_path. The rename refuses instead — non-fatally, so the metadata save
# still lands.

def test_rename_refuses_to_move_a_folder_other_recordings_share(client, ryman):
    """Point both recordings at the SAME folder, then edit one so its
    canonical name changes. The folder must not move."""
    _login_as(client)
    root = ryman["root"]

    shared_rel = f"Various Artists/{BASE}"
    aud = _db.session.get(Recording, ryman["aud_id"])
    aud.folder_path = shared_rel          # the merged state, as on Ryan's disk
    _db.session.commit()

    resp = client.put(f"/api/recordings/{ryman['aud_id']}", json={"source": "SBD"})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    err = resp.get_json().get("folder_rename_error")
    assert err and "share this folder" in err, resp.get_json()

    # Disk untouched: the shared folder is still there under its own name, and
    # no "(SBD)" folder was created.
    artist_dir = root / "Various Artists"
    assert (artist_dir / BASE).is_dir()
    assert sorted(p.name for p in artist_dir.iterdir() if p.is_dir()) == \
        [BASE, f"{BASE} (AUD)"]

    # The metadata edit itself still committed — the guard is non-fatal.
    _db.session.expire_all()
    assert _db.session.get(Recording, ryman["aud_id"]).source == "SBD"
    # And its folder_path was NOT repointed at a folder that does not exist.
    assert _db.session.get(Recording, ryman["aud_id"]).folder_path == shared_rel


def test_unshared_folder_still_renames_normally(client, ryman):
    """The guard must not make every rename refuse — a recording that owns
    its folder outright still follows its metadata."""
    _login_as(client)
    artist_dir = ryman["root"] / "Various Artists"

    resp = client.put(f"/api/recordings/{ryman['aud_id']}", json={"source": "SBD"})
    assert resp.status_code == 200
    assert "folder_rename_error" not in resp.get_json(), resp.get_json()
    assert (artist_dir / f"{BASE} (SBD)").is_dir()
