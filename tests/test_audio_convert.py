"""
tests/test_audio_convert.py — SHN/WAV detection, the conversion endpoint's
guards, and the Top Shelf's ordered sequence.

No ffmpeg is invoked anywhere here.  `convert_folder` is a thin loop around a
subprocess call; what is worth pinning is everything AROUND it — which folders
get offered a conversion at all, what the endpoint refuses, and the ordering
guarantee the Top Shelf's record bin now depends on.
"""

import os

import pytest

from app.extensions import db as _db
from app.models.performance import Performance
from app.models.artist import Artist
from app.models.recording import Recording
from app.models.track import Track
from app.models.user import User
from app.utils.audio_convert import (ORIGINALS_DIRNAME, convertible_files,
                                     detect_convertible)


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
    c = app.test_client()
    _login_as(c)
    return c


def _folder(tmp_path, *names):
    d = tmp_path / "show"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"\0" * 16)
    return str(d)


# ═════════════════════════════════════════════════════════════════════════════
# detect_convertible
# ═════════════════════════════════════════════════════════════════════════════
def test_shn_folder_is_offered_a_conversion(tmp_path):
    got = detect_convertible(_folder(tmp_path, "d1t01.shn", "d1t02.shn"))
    assert got == {"kind": "shn", "ext": ".shn", "exts": [".shn"], "count": 2}


def test_wav_folder_is_offered_a_conversion(tmp_path):
    got = detect_convertible(_folder(tmp_path, "01.wav", "02.wav", "03.wav"))
    assert got == {"kind": "wav", "ext": ".wav", "exts": [".wav"], "count": 3}


def test_flac_beside_unsupported_files_still_offers_a_conversion(tmp_path):
    """Mixed folder: the unsupported files are exactly what needs converting."""
    got = detect_convertible(_folder(tmp_path, "01.flac", "02.wav"))
    assert got["count"] == 1 and got["exts"] == [".wav"]


def test_originals_subfolder_is_not_counted(tmp_path):
    """Leftovers from an older build's _originals/ are not re-offered."""
    d = _folder(tmp_path, "01.flac")
    sub = os.path.join(d, ORIGINALS_DIRNAME)
    os.makedirs(sub)
    open(os.path.join(sub, "01.shn"), "wb").close()
    assert detect_convertible(d) is None


def test_shn_wins_over_wav_in_a_mixed_folder(tmp_path):
    got = detect_convertible(_folder(tmp_path, "a.shn", "b.wav"))
    assert got["kind"] == "shn" and got["exts"] == [".shn", ".wav"]


def test_ordinary_flac_folder_and_empty_folder_are_both_silent(tmp_path):
    assert detect_convertible(_folder(tmp_path, "01.flac")) is None
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert detect_convertible(str(empty)) is None


def test_convertible_files_is_stable_and_extension_scoped(tmp_path):
    d = _folder(tmp_path, "b.shn", "a.shn", "c.wav", "notes.txt")
    assert convertible_files(d, ".shn") == ["a.shn", "b.shn"]
    assert convertible_files(d, (".shn", ".wav")) == ["a.shn", "b.shn", "c.wav"]


# ═════════════════════════════════════════════════════════════════════════════
# convert_folder: level 8, originals deleted, failures keep their original
# ═════════════════════════════════════════════════════════════════════════════
def _fake_ffmpeg(monkeypatch, fail=()):
    """Stand in for ffmpeg: writes the output file (last arg) unless the input
    name is in `fail`. Returns the list of recorded argv lists."""
    import subprocess
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        src = cmd[cmd.index("-i") + 1]
        if os.path.basename(src) in fail:
            return subprocess.CompletedProcess(cmd, 1, "", "boom: bad input")
        with open(cmd[-1], "wb") as f:
            f.write(b"fLaC" + b"\0" * 8)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr("app.utils.audio_convert.subprocess.run", run)
    return calls


def test_convert_uses_level_8_and_deletes_originals(tmp_path, monkeypatch):
    from app.utils.audio_convert import convert_folder
    d = _folder(tmp_path, "01.wav", "02.shn", "03.flac")
    calls = _fake_ffmpeg(monkeypatch)
    res = convert_folder(d, "ffmpeg", (".wav", ".shn"))
    assert res["failed"] == [] and sorted(res["converted"]) == ["01.flac", "02.flac"]
    for c in calls:
        assert c[c.index("-compression_level") + 1] == "8"
        assert "-sample_fmt" not in c
    assert sorted(os.listdir(d)) == ["01.flac", "02.flac", "03.flac"]
    assert not os.path.exists(os.path.join(d, ORIGINALS_DIRNAME))


def test_mixed_folder_converts_only_the_unsupported_files(tmp_path, monkeypatch):
    from app.utils.audio_convert import convert_folder
    d = _folder(tmp_path, "01.flac", "02.wav")
    flac_before = open(os.path.join(d, "01.flac"), "rb").read()
    calls = _fake_ffmpeg(monkeypatch)
    convert_folder(d, "ffmpeg", (".wav",))
    assert len(calls) == 1
    assert open(os.path.join(d, "01.flac"), "rb").read() == flac_before


def test_a_failed_file_keeps_its_original(tmp_path, monkeypatch):
    from app.utils.audio_convert import convert_folder
    d = _folder(tmp_path, "01.wav", "02.wav")
    _fake_ffmpeg(monkeypatch, fail={"02.wav"})
    res = convert_folder(d, "ffmpeg", ".wav")
    assert res["converted"] == ["01.flac"]
    assert [f["name"] for f in res["failed"]] == ["02.wav"]
    assert sorted(os.listdir(d)) == ["01.flac", "02.wav"]


def test_existing_same_name_flac_keeps_the_original(tmp_path, monkeypatch):
    from app.utils.audio_convert import convert_folder
    d = _folder(tmp_path, "song.wav")
    flac = os.path.join(d, "song.flac")
    open(flac, "wb").write(b"different")
    calls = _fake_ffmpeg(monkeypatch)
    res = convert_folder(d, "ffmpeg", ".wav")
    assert calls == [] and res["converted"] == []
    assert [f["name"] for f in res["failed"]] == ["song.wav"]
    assert os.path.exists(os.path.join(d, "song.wav"))
    assert open(flac, "rb").read() == b"different"


def test_empty_output_counts_as_failure(tmp_path, monkeypatch):
    import subprocess
    from app.utils.audio_convert import convert_folder
    d = _folder(tmp_path, "01.wav")

    def run(cmd, **kw):
        open(cmd[-1], "wb").close()
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr("app.utils.audio_convert.subprocess.run", run)
    res = convert_folder(d, "ffmpeg", ".wav")
    assert res["converted"] == [] and os.path.exists(os.path.join(d, "01.wav"))
    assert not os.path.exists(os.path.join(d, "01.flac"))


def test_convert_is_allowed_for_a_library_folder(app, client, tmp_path, monkeypatch):
    """No bring-in-only gate server-side: a folder inside LIBRARY_ROOT converts."""
    import app.api.quality as q
    lib = tmp_path / "lib"
    show = lib / "Artist" / "show"
    show.mkdir(parents=True)
    (show / "01.wav").write_bytes(b"\0" * 16)
    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["IMPORT_ROOTS"] = [str(lib)]
    monkeypatch.setattr("app.utils.audio_convert.probe_decoder", lambda *a: True)
    monkeypatch.setattr(q.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda self: None})())
    r = client.post("/api/quality/convert", json={"folder_path": str(show)})
    assert r.status_code == 202
    q._CONVERT_JOBS.clear()


# ═════════════════════════════════════════════════════════════════════════════
# The endpoint's guards
# ═════════════════════════════════════════════════════════════════════════════
def test_convert_outside_the_import_roots_is_allowed_for_the_admin(app, client, tmp_path):
    # Ryan, 2026-10-02: IMPORT_ROOTS no longer binds the admin. The answer may
    # still be 400 (no ffmpeg in the test env) but never a roots refusal.
    d = _folder(tmp_path, "01.shn")
    r = client.post("/api/quality/convert", json={"folder_path": d})
    assert r.status_code != 403


def test_convert_refuses_a_missing_folder(app, client):
    r = client.post("/api/quality/convert",
                    json={"folder_path": "/nope/not/here"})
    assert r.status_code == 400


def test_convert_status_of_an_unknown_job_is_404(app, client):
    assert client.get("/api/quality/convert/deadbeef").status_code == 404


def test_convert_requires_login(app):
    c = app.test_client()
    r = c.post("/api/quality/convert", json={"folder_path": "/tmp"})
    assert r.status_code in (401, 403)


# ═════════════════════════════════════════════════════════════════════════════
# Top Shelf: the ordered sequence behind the record bin
# ═════════════════════════════════════════════════════════════════════════════
def _make_top(db, artist_name, n):
    p = Artist(name=artist_name)
    db.session.add(p); db.session.flush()
    out = []
    for i in range(n):
        perf = Performance(artist_id=p.id, start_year=1990 + i,
                           start_month=1, start_day=1)
        db.session.add(perf); db.session.flush()
        rec = Recording(performance_id=perf.id, source="SBD", quality="A",
                        folder_path=f"{artist_name}/{i}")
        db.session.add(rec); db.session.flush()
        db.session.add(Track(recording_id=rec.id, track_number=1, title="One",
                             duration=100, file_path="01.flac"))
        out.append(rec.id)
    return out


def test_offset_walks_the_pool_without_repeating(app, client, db):
    """
    The bin deals each record once. Flipping right N times must never hand
    back something already on the shelf — that is the whole promise of
    'one pass, no repeats'.
    """
    _make_top(db, "Act One", 2)
    _make_top(db, "Act Two", 2)
    _make_top(db, "Act Three", 1)
    db.session.commit()

    seen = []
    for off in range(12):
        r = client.get(f"/api/recordings/recommended?limit=1&offset={off}")
        assert r.status_code == 200
        got = r.get_json()
        if not got:
            break
        seen.append(got[0]["id"])

    assert len(seen) == len(set(seen)), "a recording was dealt twice"
    assert len(seen) == 5, "the whole A/A+ pool should be reachable"


def test_the_bin_runs_out_and_says_so(app, client, db):
    """
    An empty answer past the end is what makes the right-hand chevron
    disappear. If this ever started wrapping around instead, the control would
    never go away and the shelf would silently repeat itself.
    """
    _make_top(db, "Only Act", 1)
    db.session.commit()
    r = client.get("/api/recordings/recommended?limit=1&offset=50")
    assert r.status_code == 200
    assert r.get_json() == []


def test_offset_zero_is_still_the_old_single_draw(app, client, db):
    """
    Round one of the sequence IS the historic six-tile draw, which is why the
    existing diversity test still holds. Four shows by one act, and at most one
    of them may appear.
    """
    made = _make_top(db, "Prolific", 4)
    db.session.commit()
    r = client.get("/api/recordings/recommended?limit=3&offset=0")
    ids = [x["id"] for x in r.get_json()]
    assert len(set(ids) & set(made)) <= 1


def test_a_artist_does_not_come_round_until_the_others_have(app, client, db):
    """
    The bin is rounds, not a shuffled list: with two acts of two shows each,
    the first two flips must be different acts.
    """
    a = set(_make_top(db, "Act A", 2))
    b = set(_make_top(db, "Act B", 2))
    db.session.commit()

    first_two = []
    for off in (0, 1):
        got = client.get(f"/api/recordings/recommended?limit=1&offset={off}").get_json()
        assert got
        first_two.append(got[0]["id"])

    assert (first_two[0] in a) != (first_two[1] in a), \
        "both of the first two flips came from the same artist"
    assert len(set(first_two) & (a | b)) == 2
